import threading
import time
from uuid import UUID

import pytest
from loguru import logger

from tests.test_api import AUTH, client, sqlite_profile_id  # noqa: F401  (fixtures)

EMOJI_SQL = "SELECT '😀'; SELECT * FROM t WHERE t.i"
WARNING = logger.level("WARNING").no
ERROR = logger.level("ERROR").no
DEBUG = logger.level("DEBUG").no


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


@pytest.fixture
def log_records():
    records = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink_id)


@pytest.fixture
def unreachable_profile_id(client, tmp_path):
    body = {"profile": {"engine": "sqlite", "name": "gone", "path": str(tmp_path / "missing" / "gone.db")}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def labels(response):
    return [item["label"] for item in response.json()["items"]]


def test_split_uses_utf16_offsets(client):
    statements = client.post("/sql/split", json={"text": EMOJI_SQL, "dialect": "sqlite"}, headers=AUTH).json()["statements"]
    assert statements[0] == {"start": 0, "end": 11, "terminator_end": 12}  # '😀' is two UTF-16 units
    assert statements[1]["start"] == 13 and statements[1]["end"] == utf16_len(EMOJI_SQL)


def test_complete_after_an_astral_character(client, sqlite_profile_id):
    cursor = utf16_len(EMOJI_SQL)
    body = {"profile_id": sqlite_profile_id, "text": EMOJI_SQL, "cursor": cursor}
    result = client.post("/sql/complete", json=body, headers=AUTH).json()
    assert [i["label"] for i in result["items"]] == ["id"]
    assert (result["start"], result["end"]) == (cursor - 1, cursor)


def test_complete_without_a_profile_gives_keywords(client):
    result = client.post("/sql/complete", json={"dialect": "postgres", "text": "SEL", "cursor": 3}, headers=AUTH)
    assert "SELECT" in labels(result)
    assert client.post("/sql/complete", json={"text": "SEL", "cursor": 3}, headers=AUTH).status_code == 422


def test_cursor_beyond_the_text_clamps_and_a_negative_one_is_rejected(client):
    body = {"dialect": "postgres", "text": "SEL", "cursor": 99}
    result = client.post("/sql/complete", json=body, headers=AUTH)
    assert result.status_code == 200 and "SELECT" in labels(result)
    assert (result.json()["start"], result.json()["end"]) == (0, 3)
    body["cursor"] = -1
    assert client.post("/sql/complete", json=body, headers=AUTH).status_code == 422


def test_cursor_inside_a_surrogate_pair_snaps_to_the_character(client):
    body = {"dialect": "sqlite", "text": EMOJI_SQL, "cursor": 9}  # between the two halves of '😀', inside a literal
    result = client.post("/sql/complete", json=body, headers=AUTH).json()
    assert result == {"start": 8, "end": 8, "items": []}


def test_describe(client, sqlite_profile_id):
    body = {"profile_id": sqlite_profile_id, "text": "SELECT * FROM t", "cursor": 15}
    found = client.post("/sql/describe", json=body, headers=AUTH).json()
    assert found["name"] == "t" and [c["name"] for c in found["columns"]] == ["id", "v"]
    body["cursor"] = 2
    assert client.post("/sql/describe", json=body, headers=AUTH).json() is None
    del body["profile_id"]
    assert client.post("/sql/describe", json=body, headers=AUTH).status_code == 422
    body["cursor"] = -1
    assert client.post("/sql/describe", json={**body, "profile_id": sqlite_profile_id}, headers=AUTH).status_code == 422


def test_analyze(client, sqlite_profile_id):
    body = {"profile_id": sqlite_profile_id, "statements": ["SELECT 1", "DELETE FROM t"]}
    decisions = client.post("/sql/analyze", json=body, headers=AUTH).json()
    assert [d["action"] for d in decisions] == ["run", "confirm"]
    assert decisions[1]["reasons"] == ["delete_without_where"]


def test_format_rows_and_browse_select(client, sqlite_profile_id):
    body = {"dialect": "sqlite", "kind": "insert", "columns": [{"name": "id", "category": "number"}],
            "rows": [[1], [2]], "options": {"table_name": "t"}}
    assert client.post("/sql/format-rows", json=body, headers=AUTH).json()["text"].splitlines() == [
        "INSERT INTO t (id) VALUES (1);", "INSERT INTO t (id) VALUES (2);",
    ]
    path = [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "t"}]
    body = {"profile_id": sqlite_profile_id, "path": path, "where": "id > 1", "order_by": [{"column": "v", "descending": True}]}
    assert client.post("/sql/browse-select", json=body, headers=AUTH).json()["sql"] == (
        "SELECT * FROM main.t\nWHERE id > 1\nORDER BY v DESC"
    )
    body["where"] = "1=1; DROP TABLE t"
    assert client.post("/sql/browse-select", json=body, headers=AUTH).status_code == 422


def test_sql_endpoints_need_the_token(client):
    assert client.post("/sql/split", json={"text": "SELECT 1", "dialect": "sqlite"}).status_code == 401


def test_an_unreachable_profile_still_gets_keywords(client, unreachable_profile_id):
    body = {"profile_id": unreachable_profile_id, "text": "SEL", "cursor": 3}
    result = client.post("/sql/complete", json=body, headers=AUTH)
    assert result.status_code == 200 and "SELECT" in labels(result)
    described = client.post("/sql/describe", json={**body, "text": "SELECT * FROM t", "cursor": 15}, headers=AUTH)
    assert described.status_code == 200 and described.json() is None


def test_completion_does_not_wait_for_a_snapshot_another_request_is_building(client, sqlite_profile_id, monkeypatch):
    metadata = client.app.state.container.metadata
    build_started, finish_build = threading.Event(), threading.Event()
    real = metadata.with_connection

    def slow_build(*args, **kwargs):
        build_started.set()
        assert finish_build.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(metadata, "with_connection", slow_build)
    builder = threading.Thread(target=lambda: client.app.state.container.schema_cache.get(UUID(sqlite_profile_id)))
    builder.start()
    try:
        assert build_started.wait(timeout=10)
        body = {"profile_id": sqlite_profile_id, "text": "SELECT * FROM t WHERE t.i", "cursor": 25}
        started = time.monotonic()
        result = client.post("/sql/complete", json=body, headers=AUTH)
        assert time.monotonic() - started < 5  # a keystroke never queues behind the build
        assert result.status_code == 200 and "id" not in labels(result)  # keywords only, for now
    finally:
        finish_build.set()
        builder.join(timeout=10)
    assert labels(client.post("/sql/complete", json=body, headers=AUTH)) == ["id"]


def test_describe_waits_for_a_snapshot_another_request_is_building(client, sqlite_profile_id, monkeypatch):
    # Describe is an explicit user action (F1 / alt-click): an answer is worth a short wait, unlike a keystroke.
    metadata = client.app.state.container.metadata
    build_started, finish_build = threading.Event(), threading.Event()
    real = metadata.with_connection

    def slow_build(*args, **kwargs):
        build_started.set()
        assert finish_build.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(metadata, "with_connection", slow_build)
    builder = threading.Thread(target=lambda: client.app.state.container.schema_cache.get(UUID(sqlite_profile_id)))
    builder.start()
    answers = []
    body = {"profile_id": sqlite_profile_id, "text": "SELECT * FROM t", "cursor": 15}
    describer = threading.Thread(target=lambda: answers.append(client.post("/sql/describe", json=body, headers=AUTH)))
    try:
        assert build_started.wait(timeout=10)
        describer.start()
        describer.join(timeout=0.5)
        assert describer.is_alive()  # waiting for the snapshot instead of answering "nothing found"
    finally:
        finish_build.set()
        builder.join(timeout=10)
        describer.join(timeout=10)
    assert answers[0].status_code == 200 and answers[0].json()["name"] == "t"


def test_an_unreachable_profile_warns_once_not_on_every_keystroke(client, unreachable_profile_id, log_records):
    def warnings():
        return [r for r in log_records if r["level"].no >= WARNING]

    body = {"profile_id": unreachable_profile_id, "text": "SEL", "cursor": 3}
    client.post("/sql/complete", json=body, headers=AUTH)
    first = len(warnings())
    assert first >= 1  # spec §5: a failed snapshot build is logged
    for cursor in (2, 1, 3):
        client.post("/sql/complete", json={**body, "cursor": cursor}, headers=AUTH)
    client.post("/sql/describe", json=body, headers=AUTH)
    assert len(warnings()) == first


def test_sql_text_is_only_logged_at_debug(client, sqlite_profile_id, log_records):
    secret = "SELECT payroll_marker FROM salaries"
    body = {"profile_id": sqlite_profile_id, "text": secret, "cursor": len(secret)}
    client.post("/sql/split", json={"text": secret, "dialect": "sqlite"}, headers=AUTH)
    client.post("/sql/analyze", json={"profile_id": sqlite_profile_id, "statements": [secret]}, headers=AUTH)
    client.post("/sql/complete", json=body, headers=AUTH)
    client.post("/sql/describe", json=body, headers=AUTH)
    path = [{"kind": "table", "name": "t"}]
    client.post("/sql/browse-select", json={"profile_id": sqlite_profile_id, "path": path, "where": "payroll_marker = 1"},
                headers=AUTH)
    assert any(r["level"].no == DEBUG and "/sql/complete" in r["message"] for r in log_records)  # calls are logged
    assert not [r for r in log_records if r["level"].no > DEBUG and "payroll_marker" in r["message"]]


@pytest.mark.parametrize("cell", [
    {"$type": "bytes"}, {"$type": "bytes", "preview": "ab"}, {"$type": "bytes", "preview": 5, "size": "x"},
    {"$type": "bytes", "preview": "ab", "size": None}, {"$type": "text"}, {"$type": "text", "truncated": True},
    {"$type": "text", "preview": "abc", "size": None}, {"$type": "other"}, {"size": 1},
])
def test_malformed_grid_cells_are_a_422_for_every_format(client, log_records, cell):
    columns = [{"name": "v", "category": "text"}]
    for kind in ("tsv", "csv", "json", "jsonl", "markdown", "insert", "in_list"):
        body = {"dialect": "postgres", "kind": kind, "columns": columns, "rows": [["fine"], [cell]]}
        response = client.post("/sql/format-rows", json=body, headers=AUTH)
        assert response.status_code == 422, kind
        error = response.json()["error"]
        assert error["code"] == "validation_failed" and "Row index 1, column index 0" in error["message"], kind
    assert not [r for r in log_records if r["level"].no >= ERROR]  # a client mistake is not a server error


def test_a_snapshot_index_bug_is_logged_and_completion_falls_back_to_keywords(
    client, sqlite_profile_id, log_records, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("index bug")

    monkeypatch.setattr("dbaguy_backend.services.schema_cache.SnapshotIndex", broken)
    body = {"profile_id": sqlite_profile_id, "text": "SEL", "cursor": 3}
    result = client.post("/sql/complete", json=body, headers=AUTH)
    assert result.status_code == 200 and "SELECT" in labels(result)
    errors = [r for r in log_records if r["level"].no >= ERROR]
    assert len(errors) == 1 and "completion index" in errors[0]["message"]
    assert errors[0]["exception"] is not None and errors[0]["exception"].type is RuntimeError  # with the traceback


def test_value_condition_endpoint(client):
    body = {"dialect": "mysql", "column": "order", "value": "it's", "category": "text"}
    assert client.post("/sql/value-condition", json=body, headers=AUTH).json() == {"condition": "`order` = 'it''s'"}
    preview = {**body, "value": {"$type": "text", "preview": "x", "size": 70000, "truncated": True}}
    assert client.post("/sql/value-condition", json=preview, headers=AUTH).json() == {"condition": None}
    assert client.post("/sql/value-condition", json={**body, "extra": 1}, headers=AUTH).status_code == 422
