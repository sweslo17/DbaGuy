from typing import Any


class DomainError(Exception):
    """Base error carrying an API error code and HTTP status."""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, detail: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Unauthorized(DomainError):
    code = "unauthorized"
    status = 401


class NotFound(DomainError):
    code = "not_found"
    status = 404


class ProfileNotFound(NotFound):
    code = "profile_not_found"


class SessionNotFound(NotFound):
    code = "session_not_found"


class ValidationFailed(DomainError):
    code = "validation_failed"
    status = 422


class UnsupportedEngine(ValidationFailed):
    code = "unsupported_engine"


class ConnectionFailed(DomainError):
    code = "connection_failed"
    status = 502


class TunnelFailed(ConnectionFailed):
    code = "tunnel_failed"


class SessionBusy(DomainError):
    code = "session_busy"
    status = 409


class SecretStoreError(DomainError):
    code = "secret_store_error"
    status = 500


class QueryFailed(DomainError):
    code = "query_failed"
    status = 400
