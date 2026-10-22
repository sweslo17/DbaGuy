"""Run SQL file's inspection (spec ⑥ §3.2): what the dialog learns from a file before it runs, without connecting:
size, first lines, a DbaGuy dump's header (a hint, never a permission) and the warnings. Pure (a SQLite profile; no
database is opened)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.adapters.base import AdapterCapabilities
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.errors import FileNotFound, NotSupported, ReadOnlyProfile
from dbaguy_backend.domain.profiles import Engine, ProfileUpsertRequest
from dbaguy_backend.services.data_service import PREVIEW_LINES
from dbaguy_backend.sql.dump_header import CUT_SHORT, complete_line, cut_short, header_lines, parse_header
from dbaguy_backend.sql.statement_stream import UTF16_MESSAGE
from dbaguy_backend.storage.secret_store import InMemorySecretStore

FIXTURES = Path(__file__).parent / "fixtures" / "run_file"


@pytest.fixture
def container(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore())


def profile(container, tmp_path, **fields):
    body = {"profile": {"engine": "sqlite", "name": "local", "path": str(tmp_path / "x.db"), **fields}}
    return container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id


def header(engine=Engine.POSTGRES, drop_first=False, filtered=None) -> str:
    return "\n".join(header_lines(
        engine=engine, server="PostgreSQL 17.11", scope="database dbaguy · schema public",
        created="2026-10-20T12:34:56+08:00", content="schema and data · 2 tables", snapshot="one consistent snapshot",
        not_included=["owners and privileges"], drop_first=drop_first, filtered=filtered,
    )) + "\n"


def test_a_header_is_read_back_as_written():
    found = parse_header("﻿" + header(drop_first=True) + "SET x = 1;\n-- Options: not a header line\n")
    assert (found.format, found.engine, found.server, found.scope, found.drop_first) == (
        1, Engine.POSTGRES, "PostgreSQL 17.11", "database dbaguy · schema public", True)
    assert len(found.lines) == 7 and found.lines[0] == "-- DbaGuy SQL dump · format 1"
    assert parse_header("-- MySQL dump 10.13\n") is None
    assert parse_header("-- DbaGuy SQL dump · format 1\n-- Engine: oracle · x · y\n") is None


def test_the_inspection_of_a_dbaguy_dump_written_for_another_engine(container, tmp_path):
    path = tmp_path / "d.sql"
    path.write_text(header(drop_first=True) + "CREATE TABLE t (a int);\n" + complete_line(0, 1) + "\n")
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert (found.size, found.header.engine, found.preview_truncated) == (path.stat().st_size, Engine.POSTGRES, False)
    assert found.preview.endswith("CREATE TABLE t (a int);\n-- Dump complete: 0 rows in 1 tables\n")
    assert found.warnings == ["Written for PostgreSQL, the target is SQLite", "This dump drops and recreates its objects"]


@pytest.mark.parametrize(("drop_first", "said"), [
    (True, "The file holds only the rows of main.t that match a filter: restoring it drops the table and replaces it "
           "with those rows"),
    (False, "The file holds only the rows of main.t that match a filter"),
])
def test_a_filtered_dump_says_whose_rows_it_holds(container, tmp_path, drop_first, said):  # fix wave F2
    path = tmp_path / "d.sql"
    path.write_text(header(Engine.SQLITE, drop_first, ("main.t", "secret = 'x'\n*/ DROP")) + complete_line(1, 1) + "\n")
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert (found.header.filtered, found.header.filtered_table) == (True, "main.t")
    assert found.warnings == ["This dump drops and recreates its objects", said] if drop_first else [said]
    assert not any("secret" in warning for warning in found.warnings)  # the filter's text is in the preview only


def test_the_preview_is_the_first_lines_and_says_when_there_is_more(container, tmp_path):
    path = tmp_path / "long.sql"
    path.write_text("".join(f"INSERT INTO t VALUES ({i});\n" for i in range(PREVIEW_LINES + 50)))
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert found.preview.count("\n") == PREVIEW_LINES and found.preview_truncated and found.header is None


def test_what_the_inspection_warns_about(container, tmp_path):
    pid = profile(container, tmp_path)
    empty = tmp_path / "empty.sql"
    empty.write_bytes(b"")
    assert container.data.inspect(pid, str(empty)).warnings == ["The file is empty"]
    latin = tmp_path / "latin.sql"
    latin.write_bytes(b"SELECT 1;\nSELECT '\xe9';\n")
    found = container.data.inspect(pid, str(latin))
    assert found.warnings == ["Line 2 is not UTF-8 text: a run stops there"] and "�" in found.preview
    cut = tmp_path / "cut.sql"  # a character cut by the preview's byte limit is not an error
    cut.write_bytes(b"-- " + b"x" * (64 * 1024 - 4) + "中".encode() + b"\n")
    assert container.data.inspect(pid, str(cut)).warnings == []


def test_a_missing_file_or_a_read_only_connection_is_refused(container, tmp_path):
    with pytest.raises(FileNotFound):
        container.data.inspect(profile(container, tmp_path), str(tmp_path / "missing.sql"))
    with pytest.raises(FileNotFound):
        container.data.inspect(profile(container, tmp_path), "relative.sql")
    path = tmp_path / "x.sql"
    path.write_text("SELECT 1;\n")
    with pytest.raises(ReadOnlyProfile):
        container.data.inspect(profile(container, tmp_path, read_only=True), str(path))


def test_the_inspect_route(container, tmp_path):
    path = tmp_path / "x.sql"
    path.write_text("SELECT 1;\n")
    pid = str(profile(container, tmp_path))
    settings = container.settings
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
        answer = client.post("/data/run-file/inspect", json={"profile_id": pid, "path": str(path)}, headers=auth)
        assert (answer.status_code, answer.json()["preview"]) == (200, "SELECT 1;\n")
        missing = client.post("/data/run-file/inspect", json={"profile_id": pid, "path": "/nope.sql"}, headers=auth)
        assert (missing.status_code, missing.json()["error"]["code"], missing.json()["error"]["message"]) == (
            404, "file_not_found", "/nope.sql is not a file")
        engines = {e["engine"]: e["run_file"] for e in client.get("/system/engines", headers=auth).json()}
        assert engines == {"postgres": True, "mysql": True, "mssql": True, "couchbase": True, "sqlite": True}


def test_a_utf16_file_is_named_as_such_and_previewed_as_text(container, tmp_path):  # pre-flight L5
    path = tmp_path / "ssms.sql"
    path.write_bytes("SET ANSI_NULLS ON\r\nGO\r\n".encode("utf-16"))
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert (found.warnings, found.preview, found.header) == ([UTF16_MESSAGE], "SET ANSI_NULLS ON\r\nGO\r\n", None)


@pytest.mark.parametrize("fixture", ["pg_dump_inserts.sql", "mysqldump.sql"])
def test_a_dump_cut_short_is_told_by_its_missing_last_line(container, tmp_path, fixture):  # pre-flight M15
    whole = (FIXTURES / fixture).read_bytes()
    pid = profile(container, tmp_path)
    complete, cut = tmp_path / "complete.sql", tmp_path / "cut.sql"
    complete.write_bytes(whole)
    cut.write_bytes(whole[:whole.index(b"INSERT INTO")])  # a copy that stopped at a statement boundary
    assert CUT_SHORT not in container.data.inspect(pid, str(complete)).warnings
    assert CUT_SHORT in container.data.inspect(pid, str(cut)).warnings


def test_which_files_are_cut_short():
    dbaguy = header()
    assert cut_short(dbaguy, "INSERT INTO t VALUES (1);\n")
    assert not cut_short(dbaguy, "INSERT INTO t VALUES (1);\n" + complete_line(1, 1) + "\n")
    assert not cut_short("INSERT INTO t VALUES (1);\n", "")  # no tool named itself: nothing to tell
    assert cut_short("--\n-- PostgreSQL database cluster dump\n--\n", "-- PostgreSQL database dump complete\n")
    assert not cut_short("-- MariaDB dump 10.19  Distrib 10.11.6-MariaDB\n", "-- Dump completed\n")


def test_an_engine_without_the_tool_is_refused(tmp_path):  # pre-flight L4: §4's sentence
    class NoRun(SqliteAdapter):
        capabilities = AdapterCapabilities(row_editing=True)

    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore(), registry=AdapterRegistry([NoRun()]))
    path = tmp_path / "x.sql"
    path.write_text("SELECT 1;\n")
    with pytest.raises(NotSupported, match="^SQLite connections cannot run SQL files$"):
        container.data.inspect(profile(container, tmp_path), str(path))


def test_an_excerpt_shows_the_lines_around_one_line(container, tmp_path):  # 06b B2
    path = tmp_path / "e.sql"
    path.write_text("".join(f"SELECT {n};\n" for n in range(1, 101)))
    found = container.data.excerpt(str(path), 50)
    assert (found.first_line, found.found, found.truncated) == (30, True, True)
    assert found.text.splitlines()[0] == "SELECT 30;" and found.text.splitlines()[20] == "SELECT 50;"
    assert len(found.text.splitlines()) == 60
    start = container.data.excerpt(str(path), 3)
    assert (start.first_line, start.text.splitlines()[0]) == (1, "SELECT 1;")
    end = container.data.excerpt(str(path), 100)
    assert (end.first_line, end.found, end.truncated, end.text.splitlines()[-1]) == (80, True, False, "SELECT 100;")
    beyond = container.data.excerpt(str(path), 101)
    assert (beyond.found, beyond.text.splitlines()[-1]) == (False, "SELECT 100;")
    far = container.data.excerpt(str(path), 500)
    assert (far.found, far.text) == (False, "")


def test_an_excerpt_always_shows_its_line_after_long_lines(container, tmp_path):  # 06b B2, pre-flight M1
    path = tmp_path / "m.sql"
    path.write_text("\n".join(f"INSERT INTO t VALUES {','.join(['(1)'] * 25_000)};" for _ in range(30)) + "\n")
    got = container.data.excerpt(str(path), 25)
    assert (got.first_line, got.found, got.truncated) == (25, True, True) and got.text.startswith("INSERT")
    assert container.data.excerpt(str(path), 31).found is False
    short = tmp_path / "s.sql"  # a file that ends inside the window has fewer lines: its tail, never a read again
    short.write_text("".join(f"SELECT {n};\n" for n in range(1, 101)))
    tail = container.data.excerpt(str(short), 110)
    assert (tail.first_line, tail.found, tail.text.splitlines()[-1]) == (90, False, "SELECT 100;")


def test_an_excerpt_takes_any_bytes_and_stops_at_its_size(container, tmp_path):  # 06b B2
    path = tmp_path / "b.sql"
    path.write_bytes(b"\xef\xbb\xbfSELECT 1;\nSELECT '\xff';\n" + b"x" * 100_000 + b"\nSELECT 3;\n")
    found = container.data.excerpt(str(path), 2)
    assert found.text.startswith("SELECT 1;\nSELECT '�';\nxxx") and found.truncated
    assert len(found.text.encode()) <= 64 * 1024 + 2  # the replacement character is longer than the byte it replaces
    with pytest.raises(FileNotFound):
        container.data.excerpt(str(tmp_path / "missing.sql"), 1)
    with pytest.raises(FileNotFound):
        container.data.excerpt("relative.sql", 1)


def test_the_excerpt_route(container, tmp_path):  # 06b B2
    path = tmp_path / "r.sql"
    path.write_text("SELECT 1;\n")
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        answer = client.post("/data/run-file/excerpt", json={"path": str(path), "line": 1}, headers=auth)
        assert (answer.status_code, answer.json()["text"], answer.json()["found"]) == (200, "SELECT 1;", True)
        missing = client.post("/data/run-file/excerpt", json={"path": str(tmp_path / "no.sql"), "line": 1},
                              headers=auth)
        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "file_not_found")
        zero = client.post("/data/run-file/excerpt", json={"path": str(path), "line": 0}, headers=auth)
        assert zero.status_code == 422
        huge = client.post("/data/run-file/excerpt", json={"path": str(path), "line": 2**31}, headers=auth)
        assert huge.status_code == 422  # pre-flight L2: bounded
