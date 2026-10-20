"""/edits/target and /edits/preview over HTTP (spec ⑤ §3.4, §4): the routes, their refusals, the plan they leave and
what reaches the log. SQLite files only: no dev database needed."""

import re
import sqlite3

import pytest
from loguru import logger

from dbaguy_backend.domain.edits import COMMIT_TOO_LARGE, PLAN_TOO_LARGE, VALUE_ACCEPTED_CHARS
from dbaguy_backend.services.edit_plans import EditPlanStore
from tests.test_api import AUTH, client  # noqa: F401  (fixture)


def path(name: str, kind: str = "table") -> list[dict]:
    folder = {"table": "tables", "view": "views"}[kind]
    return [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": folder}, {"kind": kind, "name": name}]


def post(client, route: str, **body):
    return client.post(f"/edits/{route}", json=body, headers=AUTH)


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def value(text: str) -> dict:
    return {"kind": "value", "text": text}


ONE_DELETE = {"deletes": [{"id": "d:1", "key": {"id": 1}}]}


@pytest.fixture
def lite(client, tmp_path):
    file = tmp_path / "edits.db"
    conn = sqlite3.connect(file)
    conn.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT NOT NULL, price REAL,
                            made DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE loose (a TEXT, b TEXT);
        CREATE VIEW items_v AS SELECT * FROM items;
        INSERT INTO items (id, label, price) VALUES (1, 'a', 1.5), (2, 'b', 2.5);
    """)
    conn.close()
    body = {"profile": {"engine": "sqlite", "name": "lite", "path": str(file)}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"], file


def test_the_target_of_a_table_a_view_and_a_table_without_a_key(client, lite):
    pid, _ = lite
    target = post(client, "target", profile_id=pid, path=path("items")).json()
    assert (target["editable"], target["key"], target["key_source"]) == (True, ["id"], "primary_key")
    assert {c["name"]: (c["kind"], c["compare"], c["has_default"]) for c in target["columns"]} == {
        "id": ("number", "equals", True), "label": ("text", "equals", False), "price": ("number", "none", False),
        "made": ("datetime", "equals", True)}
    assert post(client, "target", profile_id=pid, path=path("items_v", "view")).json()["reason"] == (
        "Views are read-only here")
    assert post(client, "target", profile_id=pid, path=path("loose")).json()["editable"] is False


def test_a_preview_shows_the_statements_and_leaves_a_plan(client, lite):  # spec §3.4, CR-2
    pid, file = lite
    changes = {
        "updates": [{"id": "u:1", "key": {"id": 1}, "set": {"label": value("A"), "price": value("3")},
                     "original": {"label": "a", "price": 1.5}}],
        "inserts": [{"id": "i:1", "values": {"label": value("c")}}],
        "deletes": [{"id": "d:2", "key": {"id": 2}}],
    }
    preview = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert re.fullmatch(r"[0-9a-f]{32}", preview["plan_id"])
    assert (preview["begin"], preview["commit"], preview["problems"]) == ("BEGIN IMMEDIATE", "COMMIT", [])
    assert [(s["change_id"], s["sql"]) for s in preview["statements"]] == [
        ("d:2", "DELETE FROM main.items WHERE id = 2"),
        ("u:1", "UPDATE OR ABORT main.items SET label = 'A', price = 3 WHERE id = 1 AND label = 'a'"),
        ("i:1", "INSERT OR ABORT INTO main.items (label) VALUES ('c')"),
    ]
    assert preview["statements"][1]["unchecked"] == [{"column": "price", "reason": "REAL values cannot be compared exactly"}]
    assert [w["code"] for w in preview["warnings"]] == ["unchecked_columns"]
    assert preview["counts"] == {"updates": 1, "inserts": 1, "deletes": 1}
    assert preview["script"] == (
        "BEGIN IMMEDIATE;\n" + "".join(f"{s['sql']};\n" for s in preview["statements"]) + "COMMIT;")
    again = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert again["plan_id"] != preview["plan_id"]  # a preview writes nothing and can be asked again
    assert sqlite3.connect(file).execute("SELECT id, label FROM items ORDER BY id").fetchall() == [(1, "a"), (2, "b")]


def test_problems_come_back_per_cell_without_a_plan(client, lite):  # spec §3.3.3
    pid, _ = lite
    changes = {"updates": [{"id": "u:1", "key": {"id": 1},
                            "set": {"label": {"kind": "null"}, "price": value("cheap"), "gone": value("x")},
                            "original": {"label": "a", "price": 1.5, "gone": "x"}}]}
    preview = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert (preview["plan_id"], preview["statements"], preview["script"]) == (None, [], "")
    assert sorted((p["change_id"], p["column"], p["code"]) for p in preview["problems"]) == [
        ("u:1", "gone", "unknown_column"), ("u:1", "label", "not_nullable"), ("u:1", "price", "invalid_number")]


def test_refusals(client, lite):  # spec §3.4, §4
    pid, file = lite
    read_only = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "ro", "path": str(file),
                                                           "read_only": True}}, headers=AUTH).json()["profile"]["id"]
    target = post(client, "target", profile_id=read_only, path=path("items")).json()
    assert (target["editable"], target["reason"]) == (False, "This connection is read-only")
    assert error_of(post(client, "preview", profile_id=read_only, path=path("items"), changes=ONE_DELETE)) == (
        403, "read_only")
    view = post(client, "preview", profile_id=pid, path=path("items_v", "view"), changes=ONE_DELETE)
    assert error_of(view) == (422, "not_editable") and view.json()["error"]["message"] == "Views are read-only here"
    couchbase = client.post("/profiles", json={"profile": {"engine": "couchbase", "name": "cb", "host": "127.0.0.1"}},
                            headers=AUTH).json()["profile"]["id"]
    assert error_of(post(client, "target", profile_id=couchbase, path=path("items"))) == (422, "not_editable")
    locked = client.post("/profiles", json={"profile": {
        "engine": "postgres", "name": "pg", "host": "127.0.0.1", "port": 1, "username": "u", "save_password": False,
    }}, headers=AUTH).json()["profile"]["id"]
    pg_path = [{"kind": "database", "name": "db"}, {"kind": "schema", "name": "public"},
               {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "t"}]
    assert error_of(post(client, "preview", profile_id=locked, path=pg_path, changes=ONE_DELETE)) == (
        428, "password_required")  # nothing was opened: the unlock dialog, then the same preview again
    assert error_of(post(client, "preview", profile_id=pid, path=path("items"), changes={})) == (
        422, "validation_failed")  # nothing to commit


def test_values_never_reach_the_info_log(client, lite):  # spec §5
    pid, _ = lite
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        changes = {"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"label": value("zz-marker-new")},
                                "original": {"label": "zz-marker-old"}}]}
        post(client, "preview", profile_id=pid, path=path("items"), changes=changes)
    finally:
        logger.remove(sink)
    assert not [text for level, text in records if level >= 20 and "zz-marker" in text]
    assert [text for level, text in records if level >= 20 and text.startswith("Edit preview for lite on main.items")]


def test_a_commit_of_more_than_64_mib_is_refused_before_anything_is_read(client, lite):  # pre-flight L15
    pid, _ = lite
    big = value("x" * VALUE_ACCEPTED_CHARS)  # 17 of the 4 MiB the request accepts for one value: 68 MiB
    changes = {"inserts": [{"id": f"i:{i}", "values": {"label": big}} for i in range(17)]}
    response = post(client, "preview", profile_id=pid, path=path("items"), changes=changes)
    assert error_of(response) == (422, "validation_failed")
    detail = response.json()["error"]["detail"]
    # the text itself in detail[0].msg, without pydantic's "Value error, " (05a review M5)
    assert [(error["type"], error["msg"]) for error in detail] == [("commit_too_large", COMMIT_TOO_LARGE)]
    assert "input" not in detail[0] and "xxxx" not in response.text  # the 422 never echoes the values


def test_statements_too_large_to_keep_are_refused_and_leave_no_plan(client, lite):  # L15: the store's own bound
    pid, _ = lite
    edits = client.app.state.container.edits
    edits.plans = EditPlanStore(max_chars=50)  # "DELETE FROM main.items WHERE id = 1" and the script need more
    response = post(client, "preview", profile_id=pid, path=path("items"), changes=ONE_DELETE)
    assert error_of(response) == (422, "validation_failed") and response.json()["error"]["message"] == PLAN_TOO_LARGE
    assert "detail" not in response.json()["error"]  # this one's text is error.message (05a review M5)
    edits.plans = EditPlanStore()
    assert post(client, "preview", profile_id=pid, path=path("items"), changes=ONE_DELETE).json()["plan_id"]


def test_a_read_only_profile_is_answered_without_connecting(client):  # ruling E20: no connection, so no password asked
    read_only = client.post("/profiles", json={"profile": {
        "engine": "postgres", "name": "pg-ro", "host": "127.0.0.1", "port": 1, "username": "u",
        "save_password": False, "read_only": True,
    }}, headers=AUTH).json()["profile"]["id"]
    pg_path = [{"kind": "database", "name": "db"}, {"kind": "schema", "name": "public"},
               {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "t"}]
    target = post(client, "target", profile_id=read_only, path=pg_path)
    assert target.status_code == 200 and (target.json()["editable"], target.json()["reason"]) == (
        False, "This connection is read-only")
    assert error_of(post(client, "preview", profile_id=read_only, path=pg_path, changes=ONE_DELETE)) == (
        403, "read_only")
