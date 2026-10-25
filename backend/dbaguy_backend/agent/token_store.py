"""Where the MCP server's bearer token is kept (spec ⑧ R-08-3): the Keychain, beside the saved passwords (same service,
account `mcp:token`), or memory (tests, and a sidecar given DBAGUY_MCP_TOKEN, which never touches the Keychain)."""

import threading
from typing import Protocol

import keyring
from keyring.errors import KeyringError

from ..domain.errors import SecretStoreError

TOKEN_ACCOUNT = "mcp:token"


class TokenStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...


class KeyringTokenStore:
    """The token as a generic password of `service` (the data dir's Keychain service, `keychain_service`). The keyring
    backend is the one KeyringSecretStore set for the process."""

    def __init__(self, service: str) -> None:
        self.service = service

    def get(self) -> str | None:
        try:
            return keyring.get_password(self.service, TOKEN_ACCOUNT)
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain read failed: {exc}") from exc

    def set(self, value: str) -> None:
        try:
            keyring.set_password(self.service, TOKEN_ACCOUNT, value)
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain write failed: {exc}") from exc


class InMemoryTokenStore:
    def __init__(self, value: str | None = None) -> None:
        self._value = value
        self._lock = threading.Lock()

    def get(self) -> str | None:
        with self._lock:
            return self._value

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value
