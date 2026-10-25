import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .adapters.couchbase.adapter import CouchbaseAdapter
from .adapters.mssql import freetds
from .adapters.mssql.adapter import MssqlAdapter
from .adapters.mysql.adapter import MysqlAdapter
from .adapters.postgres.adapter import PostgresAdapter
from .adapters.registry import AdapterRegistry
from .adapters.sqlite.adapter import SqliteAdapter
from .agent.host import AgentHost
from .agent.login import LoginRegistry
from .agent.queries import QueryTools
from .agent.reader import AgentReader
from .agent.sessions import AgentSessions
from .agent.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
from .agent.tools import AgentTools
from .config import Settings, default_data_dir
from .connectivity.ssh_tunnel import SshTunnelManager
from .domain.agent import APP_MCP_PORT, DEV_MCP_PORT
from .services.connection_service import ConnectionService
from .services.couchbase_service import CouchbaseService
from .services.data_service import DataService
from .services.document_service import DocumentService
from .services.edit_service import EditService
from .services.field_sample_service import FieldSampleService
from .services.history_service import HistoryService
from .services.metadata_service import MetadataService
from .services.profile_service import ProfileService
from .services.safety import SafetyPolicy
from .services.schema_cache import SchemaCache
from .services.scripts_service import ScriptsService
from .services.settings_service import SettingsService
from .services.transfer_service import TransferService
from .services.workspace_service import WorkspaceService
from .sessions.hooks import SessionHooksFactory
from .sessions.jobs.registry import JobRegistry
from .sessions.manager import SessionManager
from .sql.classifier import warm_up
from .storage.paths import default_scripts_dir, history_file, profiles_file, settings_file, workspace_file
from .storage.profile_store import ProfileStore
from .storage.secret_store import APP_KEYCHAIN_SERVICE, DEV_KEYCHAIN_SERVICE, KeyringSecretStore, SecretStore

# also the resolution of the idle-transaction warning and rollback, and of closing idle paused results
_HOUSEKEEPING_INTERVAL_S = 15


def default_registry(freetds_conf: Path | None = None) -> AdapterRegistry:
    """`freetds_conf` is where SQL Server logins get DbaGuy's freetds.conf. It is only recorded here: the file is
    written by the first SQL Server connect, so building the app never writes into the data dir (R-03b-P9)."""
    if freetds_conf is not None:
        freetds.use(freetds_conf)
    return AdapterRegistry([PostgresAdapter(), MysqlAdapter(), MssqlAdapter(), CouchbaseAdapter(), SqliteAdapter()])


def is_app_data_dir(data_dir: Path) -> bool:
    return data_dir.resolve() == default_data_dir().resolve()


def keychain_service(data_dir: Path) -> str:
    """The Keychain service for passwords saved with this data dir. Only the app's own data dir uses the app's service;
    any other (scripts/dev.sh's dev/data, tests, ad-hoc sidecars) gets a separate one, so a dev run never reads,
    overwrites or leaves behind entries of the user's real connections."""
    return APP_KEYCHAIN_SERVICE if is_app_data_dir(data_dir) else DEV_KEYCHAIN_SERVICE


def default_mcp_port(data_dir: Path) -> int:
    """The MCP server's port when the settings name none (R-08-2): the app's and any other data dir's never collide."""
    return APP_MCP_PORT if is_app_data_dir(data_dir) else DEV_MCP_PORT


def _token_store(settings: Settings, secret_store: SecretStore | None) -> TokenStore:
    """In memory with DBAGUY_MCP_TOKEN, and whenever the secrets are not the Keychain's (tests pass an in-memory
    store): only a real run keeps the token in the Keychain."""
    if settings.mcp_token is not None or secret_store is not None:
        return InMemoryTokenStore(settings.mcp_token)
    return KeyringTokenStore(keychain_service(settings.data_dir))


def _keychain_store(data_dir: Path) -> KeyringSecretStore:
    service = keychain_service(data_dir)
    logger.info("Saved passwords use Keychain service {}", service)
    return KeyringSecretStore(service)


class AppContainer:
    """Builds and owns every long-lived service."""

    def __init__(
        self, settings: Settings, secret_store: SecretStore | None = None, registry: AdapterRegistry | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self.settings = settings
        data_dir = settings.data_dir
        self.registry = registry or default_registry(data_dir / "freetds" / "freetds.conf")
        secrets = secret_store or _keychain_store(data_dir)
        self.profiles = ProfileService(ProfileStore(profiles_file(data_dir)), secrets)
        # `settings` is the process Settings (token, data dir); `app_settings` is the user's preferences.
        self.app_settings = SettingsService(settings_file(data_dir), default_scripts_dir(data_dir))
        self.workspace = WorkspaceService(workspace_file(data_dir))
        self.scripts = ScriptsService(self.app_settings.scripts_root)
        self.tunnels = SshTunnelManager(data_dir / "bin" / "askpass.sh")
        self.connections = ConnectionService(self.registry, self.profiles, self.tunnels)
        self.metadata = MetadataService(self.registry, self.connections, self.profiles)
        self.schema_cache = SchemaCache(self.metadata, self.registry, self.profiles)
        self.couchbase = CouchbaseService(self.registry, self.profiles, self.schema_cache, self.metadata)
        self.documents = DocumentService(self.metadata, self.profiles, self.registry)
        self.edits = EditService(self.metadata, self.profiles, self.registry)
        self.field_samples = FieldSampleService(self.metadata, self.app_settings)
        self.jobs = JobRegistry()
        self.data = DataService(self.profiles, self.registry, self.metadata, self.jobs)
        self.transfers = TransferService(self.profiles, self.registry, self.metadata, self.schema_cache)
        self.history = HistoryService(history_file(data_dir))
        self.policy = SafetyPolicy()
        hooks = SessionHooksFactory(
            self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
            self.transfers.plans,
        )
        self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
        self.agent_login = LoginRegistry(self.metadata, self.registry)
        self.agent_sessions = AgentSessions(self.sessions, self.agent_login)
        self.agent_reader = AgentReader(self.agent_sessions)
        self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache, self.agent_login)
        self.agent_queries = QueryTools(self.agent_tools, self.agent_reader, self.history, self.field_samples,
                                        self.app_settings)
        self.agent_host = AgentHost(
            self.agent_tools, self.agent_queries, token_store or _token_store(settings, secret_store),
            default_mcp_port(data_dir), on_stop=self._close_agents,
        )
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

    def start(self) -> None:
        self._housekeeper = threading.Thread(target=self._housekeep, name="housekeeping", daemon=True)
        self._housekeeper.start()
        # sqlglot is imported on first use, off the start-up path; load it now, before the first statement needs it.
        threading.Thread(
            target=self._guarded, args=("Warming up the SQL classifier", warm_up), name="classifier-warm-up",
            daemon=True,
        ).start()
        self._guarded("Starting the MCP server", lambda: self.agent_host.start_later(self.app_settings.get().mcp))

    def _close_agents(self) -> None:
        """The MCP server stopped: nothing an agent opened stays open."""
        self.agent_sessions.close()
        self.metadata.close_all(agents_only=True)

    def _housekeep(self) -> None:
        while not self._stop.wait(_HOUSEKEEPING_INTERVAL_S):
            self._housekeep_once()

    def _housekeep_once(self) -> None:
        # Independent tasks: a failing session reaper must not starve the metadata reaper, and so on.
        self._guarded("Session reaping", self.sessions.reap_idle)
        self._guarded("Metadata reaping", self.metadata.reap_idle)
        self._guarded("Idle-transaction check", self._check_idle_transactions)
        self._guarded("Paused-result check", self.sessions.check_paused_results)

    def _check_idle_transactions(self) -> None:
        safety = self.app_settings.get().safety  # read on every pass: a changed setting applies without a restart
        self.sessions.check_idle_transactions(
            safety.idle_transaction_warn_minutes * 60, safety.idle_transaction_rollback_minutes * 60
        )

    def close(self) -> None:
        """Stop everything in dependency order: sessions and metadata hold tunnels, so tunnels go last; sessions
        write to the history, so it closes after them."""
        self._stop.set()
        self._guarded("Stopping the MCP server", self.agent_host.stop)
        self._guarded("Closing sessions", self.sessions.close_all)
        self._guarded("Closing metadata connections", self.metadata.close_all)
        self._guarded("Closing SSH tunnels", self.tunnels.close_all)
        self._guarded("Closing history", self.history.close)

    @staticmethod
    def _guarded(what: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception:
            logger.exception("{} failed", what)
