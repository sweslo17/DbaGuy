"""The SQL a grid commit sends (spec ⑤ §3.3, D-05-3): order, shape, problems, warnings, the script, per dialect."""

import time

import pytest

from dbaguy_backend.domain.edits import EditChanges, EditColumn, EditTarget
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.dml import build_plan
from tests.support import scaled

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE


def path(*segments: tuple[str, str]) -> list[PathSegment]:
    return [PathSegment(kind=kind, name=name) for kind, name in segments]


PG_PATH = path(("database", "dbaguy"), ("schema", "public"), ("folder", "tables"), ("table", "orders"))
MY_PATH = path(("schema", "dbaguy"), ("folder", "tables"), ("table", "orders"))
TS_PATH = path(("database", "dbaguy"), ("schema", "dbo"), ("folder", "tables"), ("table", "orders"))
LITE_PATH = path(("schema", "main"), ("folder", "tables"), ("table", "orders"))


def column(name, kind="text", compare="as_text", **fields) -> EditColumn:
    defaults = {"type_name": kind, "nullable": True, "has_default": False, "writable": True}
    return EditColumn(name=name, kind=kind, compare=compare, **{**defaults, **fields})


def pg_target(**fields) -> EditTarget:
    columns = [
        column("id", "number", type_name="bigint", nullable=False, has_default=True),
        column("label", nullable=False),
        column("price", "number", "none", type_name="double precision"),
        column("flag", "bool"),
        column("made", "datetime", type_name="timestamp with time zone", has_default=True),
        column("total", "number", type_name="numeric", writable=False, read_only_reason="generated column",
               has_default=True),
        column("order", "number", type_name="integer"),
    ]
    return EditTarget(**{"path": PG_PATH, "editable": True, "key": ["id"], "key_source": "primary_key",
                         "columns": columns, **fields})


def changes(**lists) -> EditChanges:
    return EditChanges.model_validate(lists)


def value(text):
    return {"kind": "value", "text": text}


NULL, DEFAULT = {"kind": "null"}, {"kind": "default"}


def test_deletes_then_updates_then_inserts_in_staging_order():  # D-05-3
    plan = build_plan(PG, pg_target(), changes(
        inserts=[{"id": "i:1", "values": {"label": value("new")}}],
        updates=[{"id": "u:2", "key": {"id": 2}, "set": {"label": value("b2")}, "original": {"label": "b"}},
                 {"id": "u:1", "key": {"id": 1}, "set": {"label": value("a2")}, "original": {"label": "a"}}],
        deletes=[{"id": "d:3", "key": {"id": 3}}],
    ))
    assert [(s.kind, s.change_id) for s in plan.statements] == [
        ("delete", "d:3"), ("update", "u:2"), ("update", "u:1"), ("insert", "i:1")]
    assert [s.sql for s in plan.statements] == [
        "DELETE FROM public.orders WHERE id = 3",
        "UPDATE public.orders SET label = 'b2' WHERE id = 2 AND label::text = 'b'",
        "UPDATE public.orders SET label = 'a2' WHERE id = 1 AND label::text = 'a'",
        "INSERT INTO public.orders (label) VALUES ('new')",
    ]
    assert (plan.begin, plan.commit, plan.problems) == ("BEGIN", "COMMIT", [])
    assert plan.counts.model_dump() == {"updates": 2, "inserts": 1, "deletes": 1}
    assert plan.script == "BEGIN;\n" + "".join(f"{s.sql};\n" for s in plan.statements) + "COMMIT;"
    assert plan.statements[0].row == "id = 3" and plan.statements[3].row is None


def test_an_update_checks_the_originals_it_can_and_lists_the_rest():  # R-05-5, ruling E5, E22
    plan = build_plan(PG, pg_target(), changes(updates=[{
        "id": "u:1", "key": {"id": "9007199254740993"},
        "set": {"label": value("x"), "price": value("2.5"), "flag": NULL, "order": value("7")},
        "original": {"label": {"$type": "text", "preview": "ab", "size": 70_000, "truncated": True}, "price": 1.5,
                     "flag": True, "order": None},
    }]))
    [statement] = plan.statements
    assert statement.sql == (
        'UPDATE public.orders SET label = \'x\', price = 2.5, flag = NULL, "order" = 7 '
        "WHERE id = 9007199254740993 AND flag::text = 'true' AND \"order\" IS NULL"
    )
    assert [(u.column, u.reason) for u in statement.unchecked] == [
        ("label", "only a preview of the value was loaded"), ("price", "double precision values cannot be compared exactly")]
    assert [(w.code, w.columns) for w in plan.warnings] == [("unchecked_columns", ["label", "price"])]


def test_unchecked_columns_are_warned_per_statement():  # spec §3.3.3 (逐句), pre-flight L4
    plan = build_plan(PG, pg_target(), changes(updates=[
        {"id": "u:1", "key": {"id": 1}, "set": {"price": value("2")}, "original": {"price": 1.5}},
        {"id": "u:2", "key": {"id": 2}, "set": {"label": value("x")}, "original": {"label": "a"}},
        {"id": "u:3", "key": {"id": 3}, "set": {"price": value("3"), "label": value("y")},
         "original": {"price": 2.5, "label": {"$type": "text", "preview": "b", "size": 70_000, "truncated": True}}},
    ]))
    assert [(w.code, w.change_id, w.columns, w.message) for w in plan.warnings] == [
        ("unchecked_columns", "u:1", ["price"], "Not checked for changes made since loading: price"),
        ("unchecked_columns", "u:3", ["price", "label"], "Not checked for changes made since loading: price, label"),
    ]
    assert [[u.column for u in s.unchecked] for s in plan.statements] == [["price"], [], ["price", "label"]]


def test_text_no_literal_can_carry_is_a_problem_or_unchecked_never_an_error():  # ruling E16, E19, pre-flight L6
    columns = [column("code", compare="equals", nullable=False), column("note", compare="equals")]
    no_escapes = EditTarget(path=MY_PATH, editable=True, key=["code"], key_source="primary_key", columns=columns,
                            backslash_escapes=False)
    plan = build_plan(MY, no_escapes, changes(
        updates=[{"id": "u:1", "key": {"code": "k"}, "set": {"note": value("b")}, "original": {"note": "a\x00"}},
                 {"id": "u:2", "key": {"code": "k"}, "set": {"note": value("b\x00")}, "original": {"note": "a"}}],
        deletes=[{"id": "d:1", "key": {"code": "k\x00"}}],
    ))
    assert [(p.change_id, p.column, p.code) for p in plan.problems] == [
        ("d:1", "code", "key_incomplete"), ("u:2", "note", "invalid_text")]
    clean = build_plan(MY, no_escapes, changes(updates=[
        {"id": "u:1", "key": {"code": "k"}, "set": {"note": value("b")}, "original": {"note": "a\x00"}}]))
    [statement] = clean.statements
    assert statement.sql == "UPDATE dbaguy.orders SET note = 'b' WHERE code = 'k'"
    assert [(u.column, u.reason) for u in statement.unchecked] == [
        ("note", "the loaded value holds a character that cannot be sent")]
    escapes = build_plan(MY, no_escapes.model_copy(update={"backslash_escapes": True}), changes(updates=[
        {"id": "u:1", "key": {"code": "k\x00"}, "set": {"note": value("b")}, "original": {"note": "a\x00"}}]))
    assert escapes.statements[0].sql == "UPDATE dbaguy.orders SET note = 'b' WHERE code = 'k\\0' AND note = 'a\\0'"
    surrogate = build_plan(PG, pg_target(), changes(updates=[
        {"id": "u:1", "key": {"id": 1}, "set": {"label": value("x")}, "original": {"label": "é\ud83d"}}]))
    assert [(u.column, u.reason) for u in surrogate.statements[0].unchecked] == [
        ("label", "the loaded value holds a character that cannot be sent")]


def test_a_key_or_original_without_a_term_says_why():  # ruling E4, G2 (`literals.unmatched_reason`)
    pg = pg_target(columns=[column("id", "number", nullable=False), column("doc", "json"), column("g", writable=False)])
    nul_doc = {"$type": "json", "text": '"a\x00"'}
    json_nul = build_plan(PG, pg, changes(updates=[
        {"id": "u:1", "key": {"id": 1}, "set": {"doc": value("{}")}, "original": {"doc": nul_doc}}]))
    assert [(u.column, u.reason) for u in json_nul.statements[0].unchecked] == [
        ("doc", "the loaded value holds a character that cannot be sent")]
    read_only = build_plan(PG, pg, changes(updates=[
        {"id": "u:1", "key": {"id": 1}, "set": {"g": value("x")}, "original": {"g": "y"}}]))
    assert [p.message for p in read_only.problems] == ["g cannot be written: read-only here"]
    keyed = [column("code", compare="equals"), column("note", compare="equals")]
    no_escapes = EditTarget(path=MY_PATH, editable=True, key=["code"], key_source="primary_key", columns=keyed,
                            backslash_escapes=False)
    nul_key = build_plan(MY, no_escapes, changes(deletes=[{"id": "d:1", "key": {"code": "k\x00"}}]))
    assert [(p.code, p.message) for p in nul_key.problems] == [
        ("key_incomplete", "This row cannot be identified: its key column code holds a character that cannot be sent")]
    lite = EditTarget(path=LITE_PATH, editable=True, key=["code"], key_source="primary_key", columns=keyed)
    null_key = build_plan(LITE, lite, changes(deletes=[{"id": "d:1", "key": {"code": None}},
                                                      {"id": "d:2", "key": {}}]))
    assert [(p.change_id, p.code, p.message) for p in null_key.problems] == [
        ("d:1", "key_incomplete", "This row cannot be identified: its key column code is NULL"),
        ("d:2", "key_incomplete", "This row cannot be identified: the change gives no value for its key column code")]


def test_a_key_column_set_is_pinned_by_its_key_only():  # CR-4
    plan = build_plan(PG, pg_target(), changes(updates=[
        {"id": "u:1", "key": {"id": 2}, "set": {"id": value("20")}, "original": {"id": 2}}]))
    assert plan.statements[0].sql == "UPDATE public.orders SET id = 20 WHERE id = 2"


def move(change_id, old, new):
    return {"id": change_id, "key": {"id": old}, "set": {"id": value(new)}, "original": {"id": old}}


COLLISION = "This change moves a row onto the key of another changed row: commit these changes separately"


def test_a_swap_of_keys_is_refused():  # ruling G7: after a concurrent delete it would commit a wrong result
    plan = build_plan(PG, pg_target(), changes(updates=[move("u:1", 1, "2"), move("u:2", 2, "1")]))
    assert [(p.change_id, p.column, p.code, p.message) for p in plan.problems] == [
        ("u:2", None, "key_collision", COLLISION)]
    assert plan.statements == [] and plan.script == ""
    big = build_plan(PG, pg_target(), changes(updates=[  # a key beyond 2^53 held as text meets the typed one
        move("u:1", 1, "9007199254740993"), move("u:2", "9007199254740993", "1")]))
    assert [(p.change_id, p.code) for p in big.problems] == [("u:2", "key_collision")]
    edited_after = build_plan(PG, pg_target(), changes(updates=[  # the row moved onto is edited later: on the move
        move("u:1", 1, "2"),
        {"id": "u:2", "key": {"id": 2}, "set": {"label": value("x")}, "original": {"label": "b"}}]))
    assert [(p.change_id, p.code) for p in edited_after.problems] == [("u:1", "key_collision")]
    composite = EditTarget(path=PG_PATH, editable=True, key=["a", "b"], key_source="primary_key",
                           columns=[column("a", "number", nullable=False), column("b", nullable=False), column("n")])
    plan = build_plan(PG, composite, changes(updates=[  # the key column not set keeps its original term
        {"id": "u:1", "key": {"a": 1, "b": "y"}, "set": {"n": value("z")}, "original": {"n": "w"}},
        {"id": "u:2", "key": {"a": 1, "b": "x"}, "set": {"b": value("y")}, "original": {"b": "x"}}]))
    assert [(p.change_id, p.code) for p in plan.problems] == [("u:2", "key_collision")]
    chain = build_plan(PG, pg_target(), changes(updates=[move("u:1", 1, "2"), move("u:2", 2, "3")]))
    assert [(p.change_id, p.code) for p in chain.problems] == [("u:1", "key_collision")]  # the move onto 2, not 2→3
    cycle = build_plan(PG, pg_target(), changes(updates=[move("u:1", 1, "2"), move("u:2", 2, "3"), move("u:3", 3, "1")]))
    assert [p.change_id for p in cycle.problems] == ["u:1", "u:2", "u:3"]


def test_a_move_onto_a_deleted_rows_key_is_allowed():  # D-05-3: the DELETE runs first, so its key can be reused
    plan = build_plan(PG, pg_target(), changes(deletes=[{"id": "d:1", "key": {"id": 2}}], updates=[move("u:1", 1, "2")]))
    assert plan.problems == [] and [s.sql for s in plan.statements] == [
        "DELETE FROM public.orders WHERE id = 2", "UPDATE public.orders SET id = 2 WHERE id = 1"]


def test_a_move_onto_an_unchanged_rows_key_is_left_to_the_server():  # its unique check refuses it: rollback
    # u:1 moves onto key 2, a row the plan does not touch; u:2 sets its own key to itself, which moves nothing
    plan = build_plan(PG, pg_target(), changes(updates=[
        move("u:1", 1, "2"),
        {"id": "u:2", "key": {"id": 3}, "set": {"id": value("3"), "label": value("c2")},
         "original": {"id": 3, "label": "c"}}]))  # a key set to itself moves nothing
    assert plan.problems == [] and [s.sql for s in plan.statements] == [
        "UPDATE public.orders SET id = 2 WHERE id = 1",
        "UPDATE public.orders SET id = 3, label = 'c2' WHERE id = 3 AND label::text = 'c'"]


def test_an_insert_leaves_defaults_out_and_an_all_default_row_is_default_values():
    plan = build_plan(PG, pg_target(), changes(inserts=[
        {"id": "i:1", "values": {"id": DEFAULT, "label": value("a"), "made": DEFAULT, "total": DEFAULT}},
        {"id": "i:2", "values": {"id": DEFAULT}},
    ]))
    assert [s.sql for s in plan.statements] == [
        "INSERT INTO public.orders (label) VALUES ('a')", "INSERT INTO public.orders DEFAULT VALUES"]
    assert [(w.code, w.change_id, w.columns) for w in plan.warnings] == [("missing_required", "i:2", ["label"])]
    my = build_plan(MY, pg_target(path=MY_PATH), changes(inserts=[{"id": "i:1", "values": {}}]))
    assert my.statements[0].sql == "INSERT INTO dbaguy.orders () VALUES ()"


def test_problems_are_per_cell_and_leave_no_statement():  # spec §3.3.3, ruling E8
    plan = build_plan(PG, pg_target(), changes(
        updates=[{"id": "u:1", "key": {"id": 1},
                  "set": {"label": NULL, "total": value("1"), "gone": value("x"), "price": DEFAULT, "flag": value("?")},
                  "original": {"label": "a", "total": "1", "gone": "x", "price": 1.0, "flag": True}}],
        deletes=[{"id": "d:1", "key": {"id": {"$type": "text", "preview": "1", "size": 70_000, "truncated": True}}},
                 {"id": "d:2", "key": {}}],
    ))
    assert sorted((p.change_id, p.column, p.code) for p in plan.problems) == [
        ("d:1", "id", "key_incomplete"), ("d:2", "id", "key_incomplete"),
        ("u:1", "flag", "invalid_bool"), ("u:1", "gone", "unknown_column"), ("u:1", "label", "not_nullable"),
        ("u:1", "price", "default_not_allowed"), ("u:1", "total", "read_only_column"),
    ]
    assert plan.statements == [] and plan.script == "" and (plan.begin, plan.commit) == ("BEGIN", "COMMIT")
    assert plan.counts.model_dump() == {"updates": 1, "inserts": 0, "deletes": 2}


def test_a_non_transactional_target_is_warned_first():  # D-05-6
    plan = build_plan(MY, pg_target(path=MY_PATH, transactional=False), changes(deletes=[{"id": "d:1", "key": {"id": 1}}]))
    assert [w.code for w in plan.warnings] == ["non_transactional"]
    assert (plan.begin, plan.commit, plan.statements[0].sql) == (
        "START TRANSACTION", "COMMIT", "DELETE FROM dbaguy.orders WHERE id = 1")


def test_each_dialect_writes_its_own_literals_and_frame():  # D-05-2, D-05-4, D-05-13
    ts_columns = [column("code", compare="equals", unicode=False, nullable=False), column("note", compare="equals"),
                  column("made", "datetime", compare="equals")]
    ts = EditTarget(path=TS_PATH, editable=True, key=["code"], key_source="primary_key", columns=ts_columns)
    plan = build_plan(TS, ts, changes(updates=[{
        "id": "u:1", "key": {"code": "k42"}, "set": {"note": value("café"), "made": value("2026-10-19 12:00:00")},
        "original": {"note": "50%", "made": "2026-10-18 01:02:03"},
    }]))
    assert plan.statements[0].sql == (
        "UPDATE dbo.orders SET note = N'café', made = N'2026-10-19T12:00:00' "
        "WHERE code = 'k42' AND note = N'50%' AND made = N'2026-10-18T01:02:03'"
    )
    assert plan.script.startswith("SET NOCOUNT OFF;\nBEGIN TRANSACTION\nGO\nUPDATE") and plan.script.endswith(
        "COMMIT TRANSACTION\nGO")
    my_columns = [column("id", "number", compare="equals", nullable=False), column("path", compare="equals")]
    for escapes, literal in ((True, "'C:\\\\x'"), (False, "'C:\\x'")):
        my = EditTarget(path=MY_PATH, editable=True, key=["id"], key_source="primary_key", columns=my_columns,
                        backslash_escapes=escapes)
        sql = build_plan(MY, my, changes(updates=[
            {"id": "u:1", "key": {"id": 1}, "set": {"path": value("C:\\x")}, "original": {"path": "C:\\x"}}])).statements[0].sql
        assert sql == f"UPDATE dbaguy.orders SET path = {literal} WHERE id = 1 AND path = {literal}"
    lite_columns = [column("id", "number", compare="equals", nullable=False), column("any", "other", compare="equals")]
    lite = EditTarget(path=LITE_PATH, editable=True, key=["id"], key_source="primary_key", columns=lite_columns)
    plan = build_plan(LITE, lite, changes(
        updates=[{"id": "u:1", "key": {"id": 1}, "set": {"any": value("x")}, "original": {"any": "y"}}],
        inserts=[{"id": "i:1", "values": {"id": value("5"), "any": value("42")}}, {"id": "i:2", "values": {}}]))
    # OR ABORT: a constraint's own ON CONFLICT REPLACE or IGNORE never applies (05a final review I1)
    assert (plan.begin, [s.sql for s in plan.statements]) == ("BEGIN IMMEDIATE", [
        "UPDATE OR ABORT main.orders SET any = 'x' WHERE id = 1 AND any = 'y'",
        "INSERT OR ABORT INTO main.orders (id, any) VALUES (5, 42)",
        "INSERT OR ABORT INTO main.orders DEFAULT VALUES"])


def test_a_tsql_commit_writes_a_backslash_before_a_line_break_so_the_server_keeps_it():  # 06c Task 5b
    # SQL Server reads `\<LF>` in a string literal as a line continuation and drops both: the SET value, the original
    # (a false conflict otherwise: the WHERE would hold a different text) and the key all go through quote_literal
    columns = [column("code", compare="equals", unicode=False, nullable=False), column("note", compare="equals")]
    ts = EditTarget(path=TS_PATH, editable=True, key=["code"], key_source="primary_key", columns=columns)
    plan = build_plan(TS, ts, changes(updates=[{
        "id": "u:1", "key": {"code": "k\\\n1"}, "set": {"note": value("new\\\r\ntext")},
        "original": {"note": "old\\\nline"},
    }]))
    max_n, max_plain = "CAST(N'' AS NVARCHAR(MAX))", "CAST('' AS VARCHAR(MAX))"
    assert plan.problems == []
    assert plan.statements[0].sql == (
        f"UPDATE dbo.orders SET note = CONCAT({max_n}, N'new\\', N'\r\ntext') "
        f"WHERE code = CONCAT({max_plain}, 'k\\', '\n1') AND note = CONCAT({max_n}, N'old\\', N'\nline')"
    )
    other = build_plan(PG, pg_target(), changes(updates=[{
        "id": "u:1", "key": {"id": 1}, "set": {"label": value("a\\\nb")}, "original": {"label": "c\\\nd"}}]))
    assert other.statements[0].sql == "UPDATE public.orders SET label = 'a\\\nb' WHERE id = 1 AND label::text = 'c\\\nd'"


def test_names_are_quoted_where_the_dialect_needs_it():  # Review Focus 2
    columns = [column("Key Col", "number", compare="equals", nullable=False), column("we]ird", compare="equals"),
               column("order", "number", compare="equals")]
    for dialect, schema, table, expected in (
        (PG, "Sales", "Order Items",
         'UPDATE "Sales"."Order Items" SET "we]ird" = \'v\', "order" = 1 WHERE "Key Col" = 1 AND "we]ird" = \'w\' '
         'AND "order" = 2'),
        (TS, "dbo", "Order Items",
         "UPDATE dbo.[Order Items] SET [we]]ird] = N'v', [order] = 1 WHERE [Key Col] = 1 AND [we]]ird] = N'w' "
         "AND [order] = 2"),
        (MY, "dbaguy", "it`s",
         "UPDATE dbaguy.`it``s` SET `we]ird` = 'v', `order` = 1 WHERE `Key Col` = 1 AND `we]ird` = 'w' AND `order` = 2"),
    ):
        target_path = path(("schema", schema), ("folder", "tables"), ("table", table))
        target = EditTarget(path=target_path, editable=True, key=["Key Col"], key_source="primary_key", columns=columns)
        plan = build_plan(dialect, target, changes(updates=[{
            "id": "u:1", "key": {"Key Col": 1}, "set": {"we]ird": value("v"), "order": value("1")},
            "original": {"we]ird": "w", "order": 2}}]))
        assert plan.statements[0].sql == expected


def test_a_target_that_is_not_editable_is_refused():
    with pytest.raises(ValueError):
        build_plan(PG, EditTarget(path=PG_PATH, editable=False, reason="Views are read-only here"),
                   changes(deletes=[{"id": "d:1", "key": {"id": 1}}]))


def test_a_thousand_changes_build_well_within_300_ms():  # spec §7
    updates = [{"id": f"u:{i}", "key": {"id": i}, "set": {"label": value(f"v{i}"), "flag": value("t")},
                "original": {"label": f"o{i}", "flag": False}} for i in range(1000)]
    request = changes(updates=updates)
    started = time.perf_counter()
    plan = build_plan(PG, pg_target(), request)
    assert time.perf_counter() - started < scaled(0.3) and len(plan.statements) == 1000
