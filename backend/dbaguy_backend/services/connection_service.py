import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, Endpoint, StatementError
from ..adapters.registry import AdapterRegistry
from ..connectivity.ssh_tunnel import SshTunnelManager
from ..domain.errors import DomainError, ValidationFailed
from ..domain.profiles import AnyProfile, ConnectionTestRequest, ConnectionTestResult, ServerProfile, profile_from_draft
from .profile_service import ProfileService, ResolvedSecrets


def _noop() -> None:
    return None


@dataclass
class ConnectionLease:
    """An open connection plus whatever it holds (an SSH tunnel)."""

    connection: DatabaseConnection
    release: Callable[[], None] = field(default=_noop)

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception as exc:
            logger.warning("Error while closing connection: {}", exc)
        finally:
            self.release()


class ConnectionService:
    def __init__(self, registry: AdapterRegistry, profiles: ProfileService, tunnels: SshTunnelManager) -> None:
        self._registry = registry
        self._profiles = profiles
        self._tunnels = tunnels

    def open(
        self, profile: AnyProfile, database: str | None = None, secrets: ResolvedSecrets | None = None
    ) -> ConnectionLease:
        secrets = secrets if secrets is not None else self._profiles.resolve_secrets(profile)
        adapter = self._registry.get(profile.engine)
        endpoint, release = self._endpoint(adapter, profile, secrets)
        try:
            connection = adapter.open(profile, endpoint, secrets.password, database)
        except BaseException:
            release()
            raise
        return ConnectionLease(connection, release)

    def _endpoint(
        self, adapter: DatabaseAdapter, profile: AnyProfile, secrets: ResolvedSecrets
    ) -> tuple[Endpoint | None, Callable[[], None]]:
        if not isinstance(profile, ServerProfile):
            return None, _noop
        port = profile.port or adapter.default_port
        if port is None:
            raise ValidationFailed("A port is required")
        if not profile.ssh.enabled:
            return Endpoint(profile.host, port), _noop
        endpoint = self._tunnels.acquire(profile.id, profile.ssh, profile.host, port, secrets.ssh_password)
        return endpoint, lambda: self._tunnels.release(profile.id, profile.host, port)

    def test(self, request: ConnectionTestRequest) -> ConnectionTestResult:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        profile = profile_from_draft(request.profile, request.existing_id or uuid4())
        try:
            lease = self.open(profile, secrets=self._test_secrets(request))
            try:
                info = lease.connection.server_info()
            finally:
                lease.close()
        except (DomainError, StatementError) as exc:
            logger.info("Connection test for '{}' failed: {}", profile.name, exc.message)
            return ConnectionTestResult(ok=False, message=exc.message, elapsed_ms=elapsed())
        return ConnectionTestResult(
            ok=True, message=f"Connected to {info.version}", server_version=info.version, elapsed_ms=elapsed()
        )

    def _test_secrets(self, request: ConnectionTestRequest) -> ResolvedSecrets:
        stored = ResolvedSecrets()
        if request.existing_id is not None:
            try:
                stored = self._profiles.resolve_secrets(self._profiles.get(request.existing_id))
            except DomainError:
                stored = ResolvedSecrets()
        given = request.secrets
        return ResolvedSecrets(
            password=given.password if given.password is not None else stored.password,
            ssh_password=given.ssh_password if given.ssh_password is not None else stored.ssh_password,
        )
