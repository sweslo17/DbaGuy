from dataclasses import dataclass
from uuid import UUID

from ..domain.errors import PasswordRequired
from ..domain.profiles import AnyProfile, ProfileEntry, ProfileSecretsInput, ProfileUpsertRequest
from ..storage.profile_store import ProfileStore
from ..storage.secret_store import InMemorySecretStore, SecretKind, SecretStore

_INPUT_FIELDS = ((SecretKind.PASSWORD, "password"), (SecretKind.SSH_PASSWORD, "ssh_password"))


@dataclass(frozen=True)
class ResolvedSecrets:
    password: str | None = None
    ssh_password: str | None = None


class ProfileService:
    """Profiles plus their secrets. Secrets go to the persistent store only when save_password is on."""

    def __init__(self, store: ProfileStore, secrets: SecretStore) -> None:
        self._store = store
        self._secrets = secrets
        self._unlocked = InMemorySecretStore()

    def list_entries(self) -> list[ProfileEntry]:
        return [self._entry(p) for p in self._store.list()]

    def entry(self, profile_id: UUID) -> ProfileEntry:
        return self._entry(self._store.get(profile_id))

    def get(self, profile_id: UUID) -> AnyProfile:
        return self._store.get(profile_id)

    def create(self, request: ProfileUpsertRequest) -> ProfileEntry:
        profile = self._store.create(request.profile)
        self._apply_secrets(profile, request.secrets)
        return self._entry(profile)

    def update(self, profile_id: UUID, request: ProfileUpsertRequest) -> ProfileEntry:
        profile = self._store.update(profile_id, request.profile)
        self._apply_secrets(profile, request.secrets)
        return self._entry(profile)

    def delete(self, profile_id: UUID) -> None:
        self._store.delete(profile_id)
        for kind in SecretKind:
            self._secrets.delete(profile_id, kind)
            self._unlocked.delete(profile_id, kind)

    def unlock(self, profile_id: UUID, secrets: ProfileSecretsInput) -> None:
        """Remember secrets for this app run only."""
        self._store.get(profile_id)
        for kind, field in _INPUT_FIELDS:
            value = getattr(secrets, field)
            if value:
                self._unlocked.set(profile_id, kind, value)

    def resolve_secrets(self, profile: AnyProfile) -> ResolvedSecrets:
        def lookup(kind: SecretKind) -> str | None:
            return self._unlocked.get(profile.id, kind) or (
                self._secrets.get(profile.id, kind) if profile.save_password else None
            )

        password = lookup(SecretKind.PASSWORD)
        ssh_password = lookup(SecretKind.SSH_PASSWORD)
        if not profile.save_password and password is None:
            raise PasswordRequired(f"Enter the password for '{profile.name}'")
        return ResolvedSecrets(password=password, ssh_password=ssh_password)

    def _apply_secrets(self, profile: AnyProfile, secrets: ProfileSecretsInput) -> None:
        if not profile.save_password:
            for kind in SecretKind:
                self._secrets.delete(profile.id, kind)
            # Unsaved passwords are never persisted; only kinds sent in this request replace the unlocked copy.
            for kind, field in _INPUT_FIELDS:
                if getattr(secrets, field) is not None:
                    self._unlocked.delete(profile.id, kind)
            return
        # Saved secrets are authoritative, so no copy unlocked earlier in this run may override them.
        for kind in SecretKind:
            self._unlocked.delete(profile.id, kind)
        for kind, field in _INPUT_FIELDS:
            value = getattr(secrets, field)
            if value is None:
                continue
            if value == "":
                self._secrets.delete(profile.id, kind)
            else:
                self._secrets.set(profile.id, kind, value)

    def _entry(self, profile: AnyProfile) -> ProfileEntry:
        return ProfileEntry(
            profile=profile,
            has_password=self._secrets.get(profile.id, SecretKind.PASSWORD) is not None,
            has_ssh_password=self._secrets.get(profile.id, SecretKind.SSH_PASSWORD) is not None,
        )
