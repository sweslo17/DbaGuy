import threading
from collections.abc import Callable

from loguru import logger

from .adapters.postgres.adapter import PostgresAdapter
from .adapters.registry import AdapterRegistry
from .adapters.sqlite.adapter import SqliteAdapter
from .config import Settings
from .connectivity.ssh_tunnel import SshTunnelManager
from .services.connection_service import ConnectionService
from .services.metadata_service import MetadataService
from .services.profile_service import ProfileService
from .services.schema_cache import SchemaCache
from .services.scripts_service import ScriptsService
from .services.settings_service import SettingsService
from .services.workspace_service import WorkspaceService
from .sessions.manager import SessionManager
from .storage.paths import default_scripts_dir, profiles_file, settings_file, workspace_file
from .storage.profile_store import ProfileStore
from .storage.secret_store import KeyringSecretStore, SecretStore

_HOUSEKEEPING_INTERVAL_S = 30


def default_registry() -> AdapterRegistry:
    return AdapterRegistry([PostgresAdapter(), SqliteAdapter()])


class AppContainer:
    """Builds and owns every long-lived service."""

    def __init__(
        self, settings: Settings, secret_store: SecretStore | None = None, registry: AdapterRegistry | None = None
    ) -> None:
        self.settings = settings
        self.registry = registry or default_registry()
        data_dir = settings.data_dir
        self.profiles = ProfileService(ProfileStore(profiles_file(data_dir)), secret_store or KeyringSecretStore())
        # `settings` is the process Settings (token, data dir); `app_settings` is the user's preferences.
        self.app_settings = SettingsService(settings_file(data_dir), default_scripts_dir(data_dir))
        self.workspace = WorkspaceService(workspace_file(data_dir))
        self.scripts = ScriptsService(self.app_settings.scripts_root)
        self.tunnels = SshTunnelManager(data_dir / "bin" / "askpass.sh")
        self.connections = ConnectionService(self.registry, self.profiles, self.tunnels)
        self.sessions = SessionManager(self.connections, self.profiles)
        self.metadata = MetadataService(self.registry, self.connections, self.profiles)
        self.schema_cache = SchemaCache(self.metadata, self.registry, self.profiles)
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

    def start(self) -> None:
        self._housekeeper = threading.Thread(target=self._housekeep, name="housekeeping", daemon=True)
        self._housekeeper.start()

    def _housekeep(self) -> None:
        while not self._stop.wait(_HOUSEKEEPING_INTERVAL_S):
            # Independent tasks: a failing session reaper must not starve the metadata reaper.
            self._guarded("Session reaping", self.sessions.reap_idle)
            self._guarded("Metadata reaping", self.metadata.reap_idle)

    def close(self) -> None:
        """Stop everything in dependency order: sessions and metadata hold tunnels, so tunnels go last."""
        self._stop.set()
        self._guarded("Closing sessions", self.sessions.close_all)
        self._guarded("Closing metadata connections", self.metadata.close_all)
        self._guarded("Closing SSH tunnels", self.tunnels.close_all)

    @staticmethod
    def _guarded(what: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception:
            logger.exception("{} failed", what)
