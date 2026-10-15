import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dbaguy_backend.domain.errors import PasswordRequired, ProfileNotFound
from dbaguy_backend.domain.profiles import (
    ProfileSecretsInput,
    ProfileUpsertRequest,
    ServerProfileDraft,
    SqliteProfileDraft,
)
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore, SecretKind

SECRET = "s3cret-value-123"


def pg_draft(name: str = "Local PG", **overrides) -> ServerProfileDraft:
    data = {"engine": "postgres", "name": name, "host": "127.0.0.1", "port": 5432, "database": "app", "username": "me"}
    data.update(overrides)
    return ServerProfileDraft.model_validate(data)


def sqlite_draft(name: str = "Notes") -> SqliteProfileDraft:
    return SqliteProfileDraft.model_validate({"engine": "sqlite", "name": name, "path": "/tmp/notes.db"})


def test_store_roundtrip(tmp_path):
    store = ProfileStore(profiles_file(tmp_path))
    pg = store.create(pg_draft(folder="Work"))
    lite = store.create(sqlite_draft())
    assert [p.name for p in store.list()] == ["Notes", "Local PG"]  # "" folder sorts before "work"
    assert store.get(pg.id).host == "127.0.0.1"
    updated = store.update(pg.id, pg_draft(name="Renamed"))
    assert updated.id == pg.id and updated.name == "Renamed"
    store.delete(lite.id)
    assert [p.name for p in store.list()] == ["Renamed"]
    with pytest.raises(ProfileNotFound):
        store.get(lite.id)
    on_disk = json.loads(profiles_file(tmp_path).read_text())
    assert on_disk["version"] == 1 and len(on_disk["profiles"]) == 1


def test_corrupted_file_is_backed_up(tmp_path):
    path = profiles_file(tmp_path)
    path.write_text("{not json")
    store = ProfileStore(path)
    assert store.list() == []
    assert list(tmp_path.glob("profiles.json.corrupt-*"))
    store.create(sqlite_draft())
    assert len(store.list()) == 1


def test_discriminated_union_parsing():
    req = ProfileUpsertRequest.model_validate({"profile": {"engine": "sqlite", "name": "x", "path": "/tmp/a.db"}})
    assert isinstance(req.profile, SqliteProfileDraft)
    req = ProfileUpsertRequest.model_validate({"profile": {"engine": "postgres", "name": "x", "host": "h"}})
    assert isinstance(req.profile, ServerProfileDraft)
    with pytest.raises(ValidationError):
        ProfileUpsertRequest.model_validate({"profile": {"engine": "postgres", "name": "x", "host": "h", "bogus": 1}})


def test_service_secrets_lifecycle(tmp_path):
    secrets = InMemorySecretStore()
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), secrets)
    entry = service.create(ProfileUpsertRequest(profile=pg_draft(), secrets=ProfileSecretsInput(password=SECRET)))
    pid = entry.profile.id
    assert entry.has_password and not entry.has_ssh_password
    assert SECRET not in profiles_file(tmp_path).read_text()
    assert service.resolve_secrets(service.get(pid)).password == SECRET

    service.update(pid, ProfileUpsertRequest(profile=pg_draft(name="B")))  # None keeps the password
    assert service.entry(pid).has_password
    service.update(pid, ProfileUpsertRequest(profile=pg_draft(name="B"), secrets=ProfileSecretsInput(password="")))
    assert not service.entry(pid).has_password

    service.update(pid, ProfileUpsertRequest(profile=pg_draft(), secrets=ProfileSecretsInput(password=SECRET)))
    service.delete(pid)
    assert secrets.get(pid, SecretKind.PASSWORD) is None


def test_unsaved_password_requires_unlock(tmp_path):
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = service.create(
        ProfileUpsertRequest(profile=pg_draft(save_password=False), secrets=ProfileSecretsInput(password=SECRET))
    )
    profile = service.get(entry.profile.id)
    assert not entry.has_password  # never stored when save_password is False
    with pytest.raises(PasswordRequired):
        service.resolve_secrets(profile)
    service.unlock(profile.id, ProfileSecretsInput(password=SECRET))
    assert service.resolve_secrets(profile).password == SECRET


def test_unknown_profile_id(tmp_path):
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    with pytest.raises(ProfileNotFound):
        service.get(uuid4())
