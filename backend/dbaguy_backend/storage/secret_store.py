import sys
import threading
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from ..domain.errors import SecretStoreError


class SecretKind(StrEnum):
    PASSWORD = "password"
    SSH_PASSWORD = "ssh_password"


class SecretStore(Protocol):
    def get(self, profile_id: UUID, kind: SecretKind) -> str | None: ...
    def set(self, profile_id: UUID, kind: SecretKind, value: str) -> None: ...
    def delete(self, profile_id: UUID, kind: SecretKind) -> None: ...


def _account(profile_id: UUID, kind: SecretKind) -> str:
    return f"profile:{profile_id}:{kind.value}"


class KeyringSecretStore:
    """Secrets in the macOS Keychain (service `app.dbaguy`)."""

    SERVICE = "app.dbaguy"

    def __init__(self) -> None:
        if sys.platform == "darwin":
            # Backend auto-discovery fails inside a PyInstaller bundle, so set it explicitly.
            from keyring.backends import macOS

            keyring.set_keyring(macOS.Keyring())

    def get(self, profile_id: UUID, kind: SecretKind) -> str | None:
        try:
            return keyring.get_password(self.SERVICE, _account(profile_id, kind))
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain read failed: {exc}") from exc

    def set(self, profile_id: UUID, kind: SecretKind, value: str) -> None:
        try:
            keyring.set_password(self.SERVICE, _account(profile_id, kind), value)
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain write failed: {exc}") from exc

    def delete(self, profile_id: UUID, kind: SecretKind) -> None:
        try:
            keyring.delete_password(self.SERVICE, _account(profile_id, kind))
        except PasswordDeleteError:
            pass
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain delete failed: {exc}") from exc


class InMemorySecretStore:
    """Process-local secrets: tests, and passwords the user chose not to save."""

    def __init__(self) -> None:
        self._values: dict[tuple[UUID, SecretKind], str] = {}
        self._lock = threading.Lock()

    def get(self, profile_id: UUID, kind: SecretKind) -> str | None:
        with self._lock:
            return self._values.get((profile_id, kind))

    def set(self, profile_id: UUID, kind: SecretKind, value: str) -> None:
        with self._lock:
            self._values[(profile_id, kind)] = value

    def delete(self, profile_id: UUID, kind: SecretKind) -> None:
        with self._lock:
            self._values.pop((profile_id, kind), None)
