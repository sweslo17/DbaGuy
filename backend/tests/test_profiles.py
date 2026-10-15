import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from loguru import logger
from pydantic import ValidationError

from dbaguy_backend.domain.errors import PasswordRequired, ProfileNotFound, ProfileStoreError
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


def write_profiles(path: Path, entries: list) -> None:
    path.write_text(json.dumps({"version": 1, "profiles": entries}), "utf-8")


def stored_entries(path: Path) -> list:
    return json.loads(path.read_text("utf-8"))["profiles"]


PG_ID, LITE_ID = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def future_pg_entry() -> dict:
    """A profile as a newer build might write it: fields this build does not know, top-level and nested."""
    return {
        "id": PG_ID, "engine": "postgres", "name": "Future PG", "host": "db", "port": 5432,
        "kerberos": {"realm": "EXAMPLE"},
        "ssl": {"mode": "require", "crl_file": "/etc/crl.pem"},
        "ssh": {"enabled": True, "host": "bastion", "jump_hosts": ["a", "b"]},
    }


def lite_entry() -> dict:
    return {"id": LITE_ID, "engine": "sqlite", "name": "Notes", "path": "/tmp/notes.db"}


def test_unknown_stored_fields_hide_no_profile(tmp_path):
    path = profiles_file(tmp_path)
    write_profiles(path, [future_pg_entry(), lite_entry()])
    profiles = ProfileStore(path).list()
    assert [p.name for p in profiles] == ["Future PG", "Notes"]
    assert profiles[0].ssl.mode == "require" and profiles[0].ssh.host == "bastion"


def test_unknown_stored_fields_survive_a_rewrite(tmp_path):
    path = profiles_file(tmp_path)
    write_profiles(path, [future_pg_entry(), lite_entry()])
    store = ProfileStore(path)
    store.create(sqlite_draft("Other"))  # rewrites the whole file
    store.update(UUID(PG_ID), pg_draft(name="Renamed PG", ssl={"mode": "disable"}, ssh={"enabled": True, "host": "b2"}))
    by_id = {e["id"]: e for e in stored_entries(path)}
    future = by_id[PG_ID]
    assert (future["name"], future["ssl"]["mode"], future["ssh"]["host"]) == ("Renamed PG", "disable", "b2")
    assert future["kerberos"] == {"realm": "EXAMPLE"}
    assert future["ssl"]["crl_file"] == "/etc/crl.pem"
    assert future["ssh"]["jump_hosts"] == ["a", "b"]
    assert lite_entry().items() <= by_id[LITE_ID].items()


def test_api_input_still_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ProfileUpsertRequest.model_validate({"profile": {"engine": "postgres", "name": "x", "host": "h", "ssl": {"crl": 1}}})
    with pytest.raises(ValidationError):
        ProfileUpsertRequest.model_validate({"profile": {"engine": "postgres", "name": "x", "host": "h", "ssh": {"jump": 1}}})


def test_an_invalid_entry_is_skipped_logged_without_contents_and_kept(tmp_path):
    path = profiles_file(tmp_path)
    broken = {"id": PG_ID, "engine": "postgres", "name": "Secret Prod", "host": "", "note": "do-not-log-me"}
    write_profiles(path, [broken, lite_entry(), "not even an object"])
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        store = ProfileStore(path)
        assert [p.name for p in store.list()] == ["Notes"]
        store.list()  # reported once, not on every read
    finally:
        logger.remove(sink)
    assert len(messages) == 2, messages
    assert "#0" in messages[0] and PG_ID in messages[0] and "#2" in messages[1]
    assert not any("do-not-log-me" in m or "Secret Prod" in m for m in messages)

    created = store.create(sqlite_draft("New"))  # each save writes the invalid entries back untouched
    store.delete(created.id)
    entries = stored_entries(path)
    assert len(entries) == 3
    assert entries[0] == broken and entries[2] == "not even an object"
    assert lite_entry().items() <= entries[1].items()


def test_corrupt_files_get_unique_backups(tmp_path):
    path = profiles_file(tmp_path)
    for _ in range(2):  # twice within the same second
        path.write_text("{not json")
        assert ProfileStore(path).list() == []
    backups = sorted(tmp_path.glob("profiles.json.corrupt-*"))
    assert len(backups) == 2 and all(b.read_text() == "{not json" for b in backups)


def test_a_corrupt_file_that_cannot_be_moved_aside_is_never_overwritten(tmp_path, monkeypatch):
    path = profiles_file(tmp_path)
    path.write_text("{not json")

    def refuse(self, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "replace", refuse)
    store = ProfileStore(path)
    with pytest.raises(ProfileStoreError, match="could not be moved aside"):
        store.list()
    with pytest.raises(ProfileStoreError):
        store.create(sqlite_draft())
    assert path.read_text() == "{not json"


def test_an_unreadable_file_is_a_store_error(tmp_path, monkeypatch):
    path = profiles_file(tmp_path)
    write_profiles(path, [lite_entry()])

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(ProfileStoreError, match="Permission denied"):
        ProfileStore(path).list()


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


def test_update_with_saved_password_replaces_unlocked_password(tmp_path):
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = service.create(ProfileUpsertRequest(profile=pg_draft(save_password=False)))
    pid = entry.profile.id
    service.unlock(pid, ProfileSecretsInput(password="old-pw"))
    service.update(
        pid,
        ProfileUpsertRequest(profile=pg_draft(save_password=True), secrets=ProfileSecretsInput(password="new-pw")),
    )
    assert service.resolve_secrets(service.get(pid)).password == "new-pw"


def test_update_with_saved_ssh_password_replaces_unlocked_ssh_password(tmp_path):
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = service.create(ProfileUpsertRequest(profile=pg_draft(save_password=False)))
    pid = entry.profile.id
    service.unlock(pid, ProfileSecretsInput(ssh_password="old-ssh"))
    service.update(
        pid,
        ProfileUpsertRequest(
            profile=pg_draft(save_password=True), secrets=ProfileSecretsInput(ssh_password="new-ssh")
        ),
    )
    assert service.resolve_secrets(service.get(pid)).ssh_password == "new-ssh"


def test_update_without_password_keeps_unlocked_password_for_unsaved_profile(tmp_path):
    service = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = service.create(ProfileUpsertRequest(profile=pg_draft(save_password=False)))
    pid = entry.profile.id
    service.unlock(pid, ProfileSecretsInput(password="old-pw"))
    service.update(pid, ProfileUpsertRequest(profile=pg_draft(name="B", save_password=False)))
    assert service.resolve_secrets(service.get(pid)).password == "old-pw"
