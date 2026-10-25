"""The SQL Server dump's dependency rules (adapters/mssql/dump_graph.py, review S1): what an object the dump leaves out
takes out with it, which defaults and checks wait for the views, what must exist before the tables. Pure."""

from dbaguy_backend.adapters.mssql.dump import runs_as
from dbaguy_backend.adapters.mssql.dump_graph import Graph, Object, Use

KINDS = {"t": "U", "lookup": "U", "temporal": "U", "v": "V", "v2": "V", "secret": "V", "pure": "FN", "reader": "FN",
         "calls_reader": "FN", "bound": "FN", "itvf": "IF", "proc": "P", "trig": "TR", "syn": "SN", "policy": "SP",
         "enc": "FN", "checked": "U", "defaulted": "U", "computed": "U"}
IDS = {name: i for i, name in enumerate(KINDS, 1)}


def graph(*edges: tuple, bases: dict | None = None) -> Graph:
    """`edges`: (referencing, referenced[, constraint, constraint type]) by name."""
    objects = {IDS[n]: Object(IDS[n], "s", n, k, 0, schema_bound=n == "bound") for n, k in KINDS.items()}
    uses = [Use(IDS[a], IDS[b], KINDS[b], "s", b, False, *rest) for a, b, *rest in edges]
    return Graph(objects, uses, {IDS[a]: IDS[b] for a, b in (bases or {}).items()})


def names(found) -> dict:
    by_id = {i: n for n, i in IDS.items()}
    return {by_id[i]: need for i, need in found.items()}


def test_what_needs_a_left_out_object_goes_with_it_transitively():
    g = graph(("v", "temporal"), ("v2", "v"), ("itvf", "v"), ("bound", "itvf"), ("reader", "v"), ("proc", "v"),
              ("syn", "v"), bases={"syn": "v"})
    hidden = {IDS["temporal"]: "table"}
    found = g.spread(hidden, set())
    # a view, an inline or schema-bound function and a synonym go; a function that resolves names when it runs and
    # a procedure stay (they are created without it, and fail only when they run)
    assert names(found) == {"v": "s.temporal", "v2": "s.v", "itvf": "s.v", "bound": "s.itvf", "syn": "s.v"}
    assert set(hidden) == {IDS[n] for n in ("temporal", "v", "v2", "itvf", "bound", "syn")}


def test_a_table_whose_check_or_computed_column_reaches_a_left_out_object_goes_and_a_trigger_too():
    g = graph(("reader", "secret"), ("calls_reader", "reader"), ("checked", "calls_reader", "ck", "C"),
              ("computed", "reader"), ("defaulted", "reader", "df", "D"), ("trig", "calls_reader"),
              ("policy", "calls_reader"))
    found = g.spread({IDS["secret"]: "definition"}, {IDS["checked"], IDS["computed"], IDS["defaulted"]})
    # a default runs only when an INSERT leaves its column out: a default whose function reaches it stays; one whose
    # function is itself left out goes (CREATE TABLE needs it)
    assert names(found) == {"checked": "s.calls_reader", "computed": "s.reader", "trig": "s.calls_reader",
                            "policy": "s.calls_reader"}
    g = graph(("defaulted", "enc", "df", "D"))
    assert names(g.spread({IDS["enc"]: "definition"}, {IDS["defaulted"]})) == {"defaulted": "s.enc"}


def test_defaults_and_checks_whose_function_reads_a_relation_wait_for_the_views():
    g = graph(("checked", "checked", "ck_self", "C"),  # a check's own column: no read
              ("checked", "pure", "ck_pure", "C"), ("checked", "calls_reader", "ck_reads", "C"),
              ("calls_reader", "reader"), ("reader", "lookup"),
              ("defaulted", "itvf", "df_view", "D"), ("itvf", "v"), ("defaulted", "pure", "df_pure", "D"))
    assert g.later([IDS["checked"], IDS["defaulted"]]) == {IDS["checked"]: frozenset({"ck_reads"}),
                                                          IDS["defaulted"]: frozenset({"df_view"})}


def test_before_the_tables_come_the_functions_they_call_and_the_views_a_bound_one_binds():
    g = graph(("computed", "bound"), ("bound", "v"), ("v", "t"), ("checked", "pure", "ck", "C"),
              ("checked", "calls_reader", "ck_late", "C"), ("calls_reader", "reader"), ("reader", "v2"),
              ("pure", "itvf"))
    later = g.later([IDS["computed"], IDS["checked"]])
    early = g.early({IDS["computed"], IDS["checked"]}, later)
    # the late check's functions wait; a function that is not bound does not take the views it reads
    assert early == {IDS["bound"], IDS["v"], IDS["pure"], IDS["itvf"]}


def test_what_the_written_objects_need_is_named():
    g = graph(("v", "temporal"), ("proc", "temporal"), ("proc", "lookup"), ("reader", "lookup"))
    written = {IDS["v"], IDS["proc"], IDS["reader"]}
    position = {i: i for i in IDS.values()}
    # a binding read of anything not written; any read of what the dump leaves out
    assert g.needs(written, {IDS["temporal"]: "table"}, position) == [
        "s.v needs s.temporal", "s.proc needs s.temporal"]


def test_who_a_module_runs_as():
    assert runs_as("CREATE PROCEDURE p WITH EXECUTE AS 'zz_runner' AS SELECT 1") == "zz_runner"
    assert runs_as("CREATE PROCEDURE p WITH EXECUTE AS N'o''brien' AS SELECT 1") == "o'brien"
    assert runs_as("CREATE FUNCTION f () RETURNS int WITH SCHEMABINDING, EXECUTE AS SELF AS BEGIN RETURN 1 END") == "SELF"
    assert runs_as("CREATE PROC p WITH EXEC AS OWNER AS SELECT 1") == "OWNER"
    assert runs_as("CREATE PROCEDURE p AS EXECUTE AS USER = 'x'; SELECT 1; REVERT") is None  # a statement, not a clause
    assert runs_as("-- EXECUTE AS 'nobody'\nCREATE PROCEDURE p WITH EXECUTE AS CALLER AS SELECT 1") == "CALLER"
