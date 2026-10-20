"""What a PostgreSQL dump writes and in which order (spec ⑥ §3.3.3, R-06-2): a scratch schema with every kind of
object, created partly out of order so that only the catalog's dependencies can order it; what is not written is
counted (pre-flight H6, P1, P4). Restores: the catalog's objects run into a scratch database the way Task 8's
DumpScript writes them, twice with `drop_first`, and both databases are compared through an independent pg_catalog
inventory and the rows' md5 (tests/pg_inventory.py, P6) — the review's cases and five-schema database
(tests/pg_dump_cases.py) included. The INSERT round trip is test_dump.py's. Scratch: `zz_06_dumpcat_*` schemas,
dropped with what is in them, and `zz_06_restore_*` / `zz_06_dumpcat_*` databases, dropped by name."""

import pytest

from dbaguy_backend.adapters.postgres.dump import RLS_SNAPSHOT, SESSION, SNAPSHOT_TEXT
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound
from tests.data_support import dropping, scratch_database, scratch_name
from tests.edit_support import scratch_db
from tests.pg_dump_cases import CASES, NASTY
from tests.pg_inventory import catalog_in, differences, inventory, restore
from tests.support import PG_USER, requires_pg

pytestmark = requires_pg


@pytest.fixture
def schema(tmp_path):
    db = scratch_db("postgres", tmp_path)
    try:
        with dropping(db) as made:
            name = scratch_name("dumpcat")
            made.append(("SCHEMA", name))
            s = f'"{name}"'
            db.execute(
                f"CREATE SCHEMA {s}",
                f"CREATE TYPE {s}.mood AS ENUM ('a', 'b')",
                f"CREATE DOMAIN {s}.pos AS int CHECK (VALUE > 0)",
                f"CREATE TYPE {s}.pair AS (x int, y text)",
                f"CREATE SEQUENCE {s}.counter START 100",
                f"CREATE TABLE {s}.parent (id serial PRIMARY KEY, m {s}.mood, p {s}.pos)",
                f'CREATE TABLE {s}."Child Rows" (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, '
                f"parent_id int REFERENCES {s}.parent (id), pr {s}.pair, note text)",
                f'CREATE INDEX child_note ON {s}."Child Rows" (note)',
                f"CREATE TABLE {s}.events (id int, at date, v text, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)",
                f"CREATE TABLE {s}.events_2026 PARTITION OF {s}.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
                f"CREATE INDEX events_v ON {s}.events (v)",
                f"CREATE FUNCTION {s}.add1(i int) RETURNS int LANGUAGE sql AS 'SELECT i + 1'",
                f"CREATE FUNCTION {s}.parents() RETURNS SETOF {s}.parent LANGUAGE sql AS 'SELECT * FROM {s}.parent'",
                # H4: a SQL-standard body is parsed at CREATE, whatever check_function_bodies says
                f"CREATE FUNCTION {s}.n_parents() RETURNS bigint LANGUAGE sql BEGIN ATOMIC SELECT count(*) FROM "
                f"{s}.parent; END",
                f"CREATE FUNCTION {s}.twice(i int) RETURNS int LANGUAGE sql RETURN i * 2",  # reads no relation
                f"CREATE FUNCTION {s}.touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
                f"CREATE TRIGGER touch BEFORE INSERT ON {s}.parent FOR EACH ROW EXECUTE FUNCTION {s}.touch()",
                f"CREATE VIEW {s}.v_top AS SELECT 1 AS x",  # older than what it will use
                f"CREATE VIEW {s}.v_base AS SELECT id AS y FROM {s}.parent",
                f"CREATE OR REPLACE VIEW {s}.v_top AS SELECT y AS x FROM {s}.v_base",
                f"CREATE MATERIALIZED VIEW {s}.mv AS SELECT count(*) AS n FROM {s}.parent",
                f"CREATE VIEW {s}.v_n AS SELECT {s}.n_parents() AS n",  # a view on a routine that reads a table
                f"CREATE AGGREGATE {s}.total(int) (sfunc = int4pl, stype = int)",  # P1: written now
                f"INSERT INTO {s}.parent (m, p) VALUES ('a', 1), ('b', 2)",
                f"SELECT setval('{s}.counter', 123)",
            )
            yield db, name
    finally:
        db.close()


def catalog(db, scope: DumpScope, kinds: DumpObjectKinds | None = None, ddl: bool = True):
    conn = db.open()
    try:
        conn.run("SET search_path = ''").close()  # the dump session's setting (P5)
        return conn.dump_catalog(scope, kinds or DumpObjectKinds(), ddl=ddl)
    finally:
        conn.close()


def names(objects, schema: str) -> list[tuple[str, str]]:
    return [(item.kind, item.name.replace(f"{schema}.", "")) for item in objects]


def round_trip(source: str, scope: DumpScope, kinds: DumpObjectKinds | None = None):
    """The catalog of `source`, run twice with drop_first into a scratch database (the second run drops what the
    first made, H7), and the inventory differences: (catalog, failures of each run, differences)."""
    found = catalog_in(source, scope, kinds)
    with scratch_database("postgres", "restore") as (target, _):
        failures = [restore(source, target, found) for _ in range(2)]
        changed = differences(inventory(source), inventory(target))
    return found, failures, changed


def test_a_schema_dump_orders_every_object_by_its_dependencies(schema):
    db, name = schema
    found = catalog(db, DumpScope(kind="schema", schema_name=name))
    assert names(found.pre, name) == [
        ("schema", name), ("type", "mood"), ("domain", "pos"), ("type", "pair"), ("sequence", "counter"),
        ("sequence", "parent_id_seq"), ("function", "add1(i integer)"), ("function", "twice(i integer)"),
        ("function", "touch()"), ("aggregate", "total(integer)"), ("table", "parent"), ("table", "Child Rows"),
        ("table", "events"), ("table", "events_2026"),
    ]
    # H4: the routines that read a relation come with the views, merged by creation order: parents() and n_parents()
    # are older than every view, v_top waits for v_base, v_n for n_parents()
    assert names(found.post, name) == [
        ("sequence value", "counter"), ("sequence value", "parent_id_seq"), ("sequence owner", "parent_id_seq"),
        ("sequence value", "Child Rows_id_seq"), ("index", "child_note"), ("index", "events_v"),
        ("foreign key", "Child Rows.Child Rows_parent_id_fkey"), ("function", "parents()"),
        ("function", "n_parents()"), ("view", "v_base"), ("view", "v_top"), ("materialized view", "mv"),
        ("refresh", "mv"),  # written by a data-only dump alone (Task 9 fix L3)
        ("view", "v_n"), ("trigger", "parent.touch"),
    ]
    assert [(t.name.replace(f"{name}.", ""), t.only) for t in found.tables] == [
        ("parent", True), ("Child Rows", True), ("events", False)]  # a partition's rows come with its parent's
    assert found.not_included == ("owners and privileges",)
    assert found.snapshot_text == SNAPSHOT_TEXT and not found.warnings
    # M10: a login that row-level security would show some rows to fails instead of dumping them
    assert SESSION.setup.index("SET row_security = off") < SESSION.setup.index(
        "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY") == len(SESSION.setup) - 1
    statements = {(item.kind, item.name.replace(f"{name}.", "")): item.statements for item in (*found.pre, *found.post)}
    assert statements[("sequence", "counter")] == (
        f"CREATE SEQUENCE {name}.counter\n    AS bigint\n    INCREMENT BY 1\n    MINVALUE 1\n    "
        "MAXVALUE 9223372036854775807\n    START WITH 100\n    CACHE 1\n    NO CYCLE",)
    assert statements[("sequence value", "counter")] == (f"SELECT pg_catalog.setval('{name}.counter', 123, true)",)
    assert statements[("sequence value", "Child Rows_id_seq")] == (
        f"""SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('{name}."Child Rows"', 'id'), 1, false)""",)
    assert "REFERENCES" not in statements[("table", "Child Rows")][0]  # foreign keys come after the data
    assert statements[("index", "events_v")] == (f"CREATE INDEX events_v ON {name}.events USING btree (v)",)  # not ONLY
    assert statements[("table", "events_2026")][0].startswith(f"CREATE TABLE {name}.events_2026 PARTITION OF {name}.events")
    assert "BEGIN ATOMIC" in statements[("function", "n_parents()")][0]
    assert statements[("aggregate", "total(integer)")] == (
        f"CREATE AGGREGATE {name}.total(integer) (\n    SFUNC = int4pl,\n    STYPE = integer\n)",)
    assert statements[("view", "v_top")] == (f"CREATE VIEW {name}.v_top AS\n SELECT y AS x\n   FROM {name}.v_base",)
    assert all(item.section == "data" for item in found.post if item.kind == "sequence value")
    assert [t.references for t in found.tables] == [(), (f"{name}.parent",), ()]  # M9: a data-only dump's order
    drops = {item.name.replace(f"{name}.", ""): item.drop for item in (*found.pre, *found.post) if item.drop}
    assert drops["Child Rows.Child Rows_parent_id_fkey"] == (  # H7: a drop_first run drops it before any table
        f'ALTER TABLE IF EXISTS ONLY {name}."Child Rows" DROP CONSTRAINT IF EXISTS "Child Rows_parent_id_fkey"')
    assert "events_2026" not in drops and drops["events"] == f"DROP TABLE IF EXISTS {name}.events"
    assert drops["total(integer)"] == f"DROP AGGREGATE IF EXISTS {name}.total(integer)"


def test_a_tables_dump_brings_what_its_tables_need_and_leaves_out_foreign_keys_to_others(schema):
    db, name = schema
    found = catalog(db, DumpScope(kind="tables", schema_name=name, names=["Child Rows", "events"]))
    assert names(found.pre, name) == [("schema", name), ("type", "pair"), ("table", "Child Rows"), ("table", "events"),
                                      ("table", "events_2026")]
    assert names(found.post, name) == [("sequence value", "Child Rows_id_seq"), ("index", "child_note"),
                                       ("index", "events_v")]
    assert found.warnings == (f"Foreign key Child Rows_parent_id_fkey of {name}.Child Rows is left out: it references "
                              f"{name}.parent, which is not in this dump",)
    assert found.not_included == ("owners and privileges", "1 foreign keys to tables not in the dump")
    one = catalog(db, DumpScope(kind="tables", schema_name=name, names=["parent"]))
    assert ("function", "touch()") in names(one.pre, name) and ("trigger", "parent.touch") in names(one.post, name)
    assert ("sequence", "parent_id_seq") in names(one.pre, name) and ("type", "mood") in names(one.pre, name)
    assert ("domain", "pos") in names(one.pre, name) and ("function", "add1(i integer)") not in names(one.pre, name)
    with pytest.raises(NotFound):
        catalog(db, DumpScope(kind="tables", schema_name=name, names=["nope"]))


def test_left_out_kinds_and_a_plan_without_definitions(schema):
    db, name = schema
    kinds = DumpObjectKinds(views=False, sequences=False, types=False, routines=False, triggers=False)
    found = catalog(db, DumpScope(kind="schema", schema_name=name), kinds)
    pre = names(found.pre, name)
    assert ("sequence", "counter") not in pre and ("function", "add1(i integer)") not in pre
    assert ("sequence", "parent_id_seq") in pre and ("type", "mood") in pre  # what its tables need stays
    assert not [item for item in found.post if item.kind in ("view", "materialized view", "trigger", "function")]
    assert found.not_included == (  # H6: what was not chosen is said too
        "owners and privileges", "4 views and materialized views (not chosen)", "1 triggers (not chosen)",
        "1 sequences (not chosen)", "6 functions and procedures (not chosen)")
    full = catalog(db, DumpScope(kind="schema", schema_name=name))
    plan = catalog(db, DumpScope(kind="schema", schema_name=name), ddl=False)
    assert names(plan.pre, name) == names(full.pre, name) and names(plan.post, name) == names(full.post, name)
    assert all(not item.statements for item in (*plan.pre, *plan.post))
    assert [t.name for t in plan.tables] == [t.name for t in full.tables]
    assert plan.not_included == full.not_included and plan.warnings == full.warnings


RICH = [
    "CREATE SCHEMA app",
    "COMMENT ON SCHEMA app IS 'the app'",
    "CREATE TYPE app.kind AS ENUM ('x', 'y')",
    "COMMENT ON TYPE app.kind IS 'a kind'",
    "CREATE DOMAIN app.qty AS int NOT NULL CHECK (VALUE >= 0)",  # NOT NULL once (PostgreSQL 17)
    "COMMENT ON CONSTRAINT qty_check ON DOMAIN app.qty IS 'not negative'",
    "CREATE FUNCTION app.is_even(i int) RETURNS bool LANGUAGE sql IMMUTABLE RETURN i % 2 = 0",
    "CREATE DOMAIN app.even AS int CHECK (app.is_even(VALUE))",  # after the function, not with the types
    "CREATE TYPE app.labelled AS (n int, label text COLLATE \"C\")",  # P4: an attribute's COLLATE
    "COMMENT ON COLUMN app.labelled.label IS 'the label'",
    "CREATE SEQUENCE app.counter START 5",
    "COMMENT ON SEQUENCE app.counter IS 'it''s counting'",
    "SELECT setval('app.counter', 42)",
    "CREATE TABLE app.kid (id int PRIMARY KEY, mom_id int)",  # older than its parent (H7, M9)
    "COMMENT ON INDEX app.kid_pkey IS 'the key index'",  # P4: a constraint's index
    "CREATE TABLE app.mom (id int PRIMARY KEY, q app.qty, e app.even, l app.labelled)",
    "CREATE FUNCTION app.moms() RETURNS bigint LANGUAGE sql STABLE BEGIN ATOMIC SELECT count(*) FROM app.mom; END",
    "CREATE TABLE app.numbered (n bigint DEFAULT app.moms())",  # a default on a routine that reads a table
    "ALTER TABLE app.kid ADD CONSTRAINT kid_mom FOREIGN KEY (mom_id) REFERENCES app.mom (id)",
    "COMMENT ON CONSTRAINT kid_mom ON app.kid IS 'its mother'",
    "CREATE TABLE app.acct (id int GENERATED BY DEFAULT AS IDENTITY (START WITH 100 INCREMENT BY 10) PRIMARY KEY, "
    "owner text COLLATE \"C\" NOT NULL DEFAULT current_user, k app.kind, n int) WITH (fillfactor = 70)",
    "COMMENT ON CONSTRAINT acct_pkey ON app.acct IS 'the key'",
    "CREATE INDEX acct_owner ON app.acct (owner)",
    "COMMENT ON INDEX app.acct_owner IS 'by owner'",
    "ALTER TABLE app.acct REPLICA IDENTITY FULL", "ALTER TABLE app.acct CLUSTER ON acct_owner",
    "ALTER TABLE app.acct ALTER COLUMN n SET STATISTICS 400",
    "CREATE STATISTICS app.acct_stats (ndistinct) ON owner, k FROM app.acct",
    "ALTER STATISTICS app.acct_stats SET STATISTICS 300",
    "COMMENT ON STATISTICS app.acct_stats IS 'together'",
    "ALTER TABLE app.acct ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE app.acct FORCE ROW LEVEL SECURITY",
    "CREATE POLICY mine ON app.acct USING (owner = current_user)",
    f"CREATE POLICY dev_reads ON app.acct AS RESTRICTIVE FOR SELECT TO {PG_USER} USING (n IS NOT NULL)",
    "COMMENT ON POLICY mine ON app.acct IS 'own rows'",
    "CREATE RULE no_delete AS ON DELETE TO app.acct DO INSTEAD NOTHING",
    "ALTER TABLE app.acct DISABLE RULE no_delete",
    "CREATE FUNCTION app.stamp() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
    "CREATE TRIGGER stamp BEFORE INSERT ON app.acct FOR EACH ROW EXECUTE FUNCTION app.stamp()",
    "ALTER TABLE app.acct DISABLE TRIGGER stamp",
    "COMMENT ON TRIGGER stamp ON app.acct IS 'stamps'",
    "CREATE TABLE app.pt (id int PRIMARY KEY, acct_id int REFERENCES app.acct (id), v text) PARTITION BY RANGE (id)",
    "CREATE TABLE app.pt_1 PARTITION OF app.pt FOR VALUES FROM (0) TO (100)",
    "ALTER TABLE app.pt_1 ADD CONSTRAINT pt_1_small CHECK (id < 50)",  # the partition's own
    "ALTER TABLE app.pt_1 ALTER COLUMN v SET NOT NULL", "ALTER TABLE app.pt_1 ALTER COLUMN v SET DEFAULT 'one'",  # P4
    "CREATE FUNCTION app.acct_count() RETURNS bigint LANGUAGE sql BEGIN ATOMIC SELECT count(*) FROM app.acct; END",
    "COMMENT ON FUNCTION app.acct_count() IS 'how many'",
    "CREATE VIEW app.v_count AS SELECT app.acct_count() AS n",
    "CREATE TRIGGER v_insert INSTEAD OF INSERT ON app.v_count FOR EACH ROW EXECUTE FUNCTION app.stamp()",
    "CREATE VIEW app.v_acct WITH (security_barrier = true) AS SELECT id, n FROM app.acct WHERE n > 0 "
    "WITH CASCADED CHECK OPTION",
    "ALTER VIEW app.v_acct ALTER COLUMN n SET DEFAULT 1",  # P4
    "COMMENT ON VIEW app.v_acct IS 'positive'",
    "COMMENT ON COLUMN app.v_acct.n IS 'the n'",
    "CREATE MATERIALIZED VIEW app.mv AS SELECT k, count(*) AS c FROM app.acct GROUP BY k",
    "CREATE UNIQUE INDEX mv_k ON app.mv (k)",
    # P3: a default and a check on a routine that reads a view come after the views
    "CREATE FUNCTION app.seen() RETURNS bigint LANGUAGE sql STABLE BEGIN ATOMIC SELECT count(*) FROM app.v_acct; END",
    "CREATE TABLE app.late (n bigint DEFAULT app.seen(), m bigint CHECK (m >= app.seen() - 1000000))",
    # H7: their DROPs come before those of the routine and the view they use
    "CREATE POLICY big ON app.acct FOR DELETE USING (app.acct_count() > 0)",
    "CREATE RULE mom_also AS ON INSERT TO app.mom DO ALSO INSERT INTO app.v_acct (id, n) VALUES (NEW.id, 1)",
    "INSERT INTO app.mom VALUES (1, 3, 2, ROW(1, 'b')), (2, 0, 4, NULL)",  # (the rule adds to app.acct)
    "INSERT INTO app.kid VALUES (10, 1), (11, 2)",
    "INSERT INTO app.acct (owner, k, n) VALUES ('ann', 'x', 5), ('bob', 'y', -1)",
    "INSERT INTO app.pt (id, acct_id, v) VALUES (3, NULL, 'x')",
    "INSERT INTO app.late (m) VALUES (7)",
    "ALTER TABLE app.acct ADD CONSTRAINT n_positive CHECK (n > 0) NOT VALID",  # bob's -1 breaks it
    "REFRESH MATERIALIZED VIEW app.mv",  # a restore computes it from the rows (a stale one comes back fresh)
]


def test_a_drop_first_dump_restores_twice_and_reads_back_the_same():
    """H4–H7 and P2–P4 on a server, checked through the independent inventory and the rows: a routine with a
    SQL-standard body over a table and a view on it, a partitioned table's foreign key, a child table older than its
    parent, row-level security and its policies, rules, a materialized view's index, a view's options and column
    default, INSTEAD OF and disabled triggers, extended statistics and targets, identity options, replica identity,
    CLUSTER ON, NOT VALID, a partition's own check, NOT NULL and default, a default and a check held back until the
    views, and comments on everything."""
    with scratch_database("postgres", "dumpcat") as (source, admin):
        admin.run_in(source, *RICH)
        found, failures, changed = round_trip(source, DumpScope(kind="schema", schema_name="app"))
        assert failures == [[], []]
        assert changed == {}
        assert names(found.pre, "app") == [
            ("schema", "app"), ("type", "kind"), ("domain", "qty"), ("type", "labelled"), ("sequence", "counter"),
            ("function", "is_even(i integer)"), ("domain", "even"), ("function", "stamp()"),
            ("table", "kid"), ("table", "mom"), ("function", "moms()"), ("table", "numbered"), ("table", "acct"),
            ("table", "pt"), ("table", "pt_1"), ("table", "late")]
        assert names(found.post, "app") == [
            ("sequence value", "counter"), ("sequence value", "acct_id_seq"), ("index", "acct_owner"),
            ("foreign key", "kid.kid_mom"), ("foreign key", "pt.pt_acct_id_fkey"),
            ("check constraint", "acct.n_positive"), ("table settings", "acct"), ("function", "acct_count()"),
            ("view", "v_count"), ("view", "v_acct"), ("materialized view", "mv"), ("refresh", "mv"),
            ("function", "seen()"),
            ("column default", "late.n"), ("check constraint", "late.late_m_check"), ("index", "mv_k"),
            ("trigger", "acct.stamp"), ("trigger", "v_count.v_insert"), ("rule", "acct.no_delete"),
            ("rule", "mom.mom_also"), ("statistics", "acct_stats"), ("row-level security", "acct"),
            ("policy", "acct.mine"), ("policy", "acct.dev_reads"), ("policy", "acct.big")]
        text = {(item.kind, item.name.replace("app.", "")): item.statements for item in (*found.pre, *found.post)}
        assert text[("table", "acct")][0] == (
            "CREATE TABLE app.acct (\n    id integer GENERATED BY DEFAULT AS IDENTITY (INCREMENT BY 10 START WITH "
            "100),\n    owner text COLLATE pg_catalog.\"C\" DEFAULT CURRENT_USER NOT NULL,\n    k app.kind,\n"
            "    n integer,\n    CONSTRAINT acct_pkey PRIMARY KEY (id)\n) WITH (fillfactor=70)")
        assert text[("table", "acct")][1:] == ("ALTER TABLE ONLY app.acct ALTER COLUMN n SET STATISTICS 400",
                                               "COMMENT ON CONSTRAINT acct_pkey ON app.acct IS 'the key'")
        assert text[("table", "pt_1")] == (
            "CREATE TABLE app.pt_1 PARTITION OF app.pt\n    FOR VALUES FROM (0) TO (100)",
            "ALTER TABLE ONLY app.pt_1 ADD CONSTRAINT pt_1_small CHECK ((id < 50))",
            "ALTER TABLE ONLY app.pt_1 ALTER COLUMN v SET NOT NULL",
            "ALTER TABLE ONLY app.pt_1 ALTER COLUMN v SET DEFAULT 'one'::text")
        assert text[("table", "late")][0] == "CREATE TABLE app.late (\n    n bigint,\n    m bigint\n)"
        assert text[("column default", "late.n")] == ("ALTER TABLE ONLY app.late ALTER COLUMN n SET DEFAULT app.seen()",)
        assert text[("foreign key", "pt.pt_acct_id_fkey")][0] == (  # H5: not ONLY on a partitioned table
            "ALTER TABLE app.pt ADD CONSTRAINT pt_acct_id_fkey FOREIGN KEY (acct_id) REFERENCES app.acct(id)")
        assert text[("table settings", "acct")] == ("ALTER TABLE app.acct REPLICA IDENTITY FULL",
                                                    "ALTER TABLE app.acct CLUSTER ON acct_owner")
        assert text[("view", "v_acct")] == (  # non-pretty (P6), its options and its column default (P4)
            "CREATE VIEW app.v_acct WITH (security_barrier=true) AS\n SELECT id,\n    n\n   FROM app.acct\n"
            "  WHERE (n > 0)\nWITH CASCADED CHECK OPTION", "ALTER VIEW app.v_acct ALTER COLUMN n SET DEFAULT 1",
            "COMMENT ON VIEW app.v_acct IS 'positive'", "COMMENT ON COLUMN app.v_acct.n IS 'the n'")
        assert text[("type", "labelled")] == (
            "CREATE TYPE app.labelled AS (\n    n integer,\n    label text COLLATE pg_catalog.\"C\"\n)",
            "COMMENT ON COLUMN app.labelled.label IS 'the label'")
        assert text[("domain", "qty")] == ("CREATE DOMAIN app.qty AS integer NOT NULL\n"
                                           "    CONSTRAINT qty_check CHECK ((VALUE >= 0))",
                                           "COMMENT ON CONSTRAINT qty_check ON DOMAIN app.qty IS 'not negative'")
        assert text[("schema", "app")] == ("CREATE SCHEMA IF NOT EXISTS app", "COMMENT ON SCHEMA app IS 'the app'")
        assert text[("table", "kid")][-1] == "COMMENT ON INDEX app.kid_pkey IS 'the key index'"
        assert text[("statistics", "acct_stats")][1] == "ALTER STATISTICS app.acct_stats SET STATISTICS 300"
        assert text[("policy", "acct.dev_reads")] == (
            f"CREATE POLICY dev_reads ON app.acct AS RESTRICTIVE FOR SELECT TO {PG_USER} USING ((n IS NOT NULL))",)
        assert found.not_included == ("owners and privileges", f"roles: 1 row-level security policies name "
                                      f"{PG_USER}, which must exist where the file is run")
        assert found.snapshot_text == SNAPSHOT_TEXT + RLS_SNAPSHOT and not found.warnings  # the dev login bypasses
        assert [(t.name.replace("app.", ""), t.only, t.references) for t in found.tables] == [
            ("kid", True, ("app.mom",)), ("mom", True, ()), ("numbered", True, ()), ("acct", True, ()),
            ("pt", False, ("app.acct",)), ("late", True, ())]


# What the inventory cannot see come back, per case: S19's inheritance, a typed table's OF, an extension's own
# comment, and a column typed as a view's row type (not written, P3)
KNOWN = {
    "inherit_multi": {("column", "c.ab.x"), ("column", "c.ab.y"), ("constraint", "c.ab.a_x_check"),
                      ("relation", "c.ab")},
    "replica_identity_and_typed": {("relation", "c.typed")},
    "comments_left": {("comment", "extension citext")},
    "table_column_of_view_rowtype": {("column", "c.t.x")},
}
# Counted and named, not written (P1): the file needs them where it is run
NAMED = {
    "operator_in_index": "view c.v uses operator c.===(integer,integer): not included",
    "ts_config_generated": "table c.t uses text search configuration c.ts: not included",
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_case_of_the_review_restores_twice_and_reads_back_the_same(case):
    with scratch_database("postgres", "dumpcat") as (source, admin):
        admin.run_in(source, *CASES[case])
        if case in NAMED:
            found = catalog_in(source, DumpScope(kind="database"))
            assert NAMED[case] in found.not_included
            return
        found, failures, changed = round_trip(source, DumpScope(kind="database"))
        assert failures == [[], []], case
        assert set(changed) == KNOWN.get(case, set()), changed
    if case == "table_column_of_view_rowtype":
        assert "1 columns typed as a view's row type" in found.not_included and found.warnings
    if case == "replica_identity_and_typed":
        assert "typed tables (1 come back as tables of their own, not OF their type)" in found.not_included


def test_scopes_bring_what_their_triggers_call_and_count_what_they_read_elsewhere():
    """P4: a `tables` scope writes the function its trigger's WHEN calls; a schema scope counts the tables and views
    it reads from other schemas, and says which partitions are out of reach."""
    with scratch_database("postgres", "dumpcat") as (source, admin):
        admin.run_in(
            source,
            "CREATE SCHEMA s1", "CREATE SCHEMA s2", "CREATE TABLE s1.t (id int PRIMARY KEY)",
            "CREATE VIEW s2.v AS SELECT id FROM s1.t",
            "CREATE FUNCTION s2.w(i int) RETURNS bool LANGUAGE sql IMMUTABLE RETURN i > 0",
            "CREATE FUNCTION s2.tf() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
            "CREATE TABLE s2.x (n int)",
            "CREATE TRIGGER tw BEFORE INSERT ON s2.x FOR EACH ROW WHEN (s2.w(NEW.n)) EXECUTE FUNCTION s2.tf()",
            "CREATE TABLE s2.pt (k int) PARTITION BY LIST (k)",
            "CREATE TABLE s1.pt_1 PARTITION OF s2.pt FOR VALUES IN (1)",
        )
        x = catalog_in(source, DumpScope(kind="tables", schema_name="s2", names=["x"]))
        assert names(x.pre, "s2") == [("schema", "s2"), ("function", "w(i integer)"), ("function", "tf()"),
                                      ("table", "x")]
        assert names(x.post, "s2") == [("trigger", "x.tw")] and x.not_included == ("owners and privileges",)
        whole = catalog_in(source, DumpScope(kind="schema", schema_name="s2"))
        assert whole.not_included == ("owners and privileges", "1 tables and views from schemas not in the dump")
        assert whole.warnings == ("Partitions of s2.pt are not in this dump (another schema, or a foreign table): "
                                  "their rows are, and need those partitions where the file is run",)


def test_the_reviews_five_schema_database_restores_twice_and_reads_back_the_same():
    """Quoted and unicode names, a 5,000-label enum, domains over domains, ranges, nested composites, a cycle of
    foreign keys, five-level partitions, recursive views, routines that read tables and views — the review's own
    database. What stays different: S19's inheritance, and an attached partition whose columns are in another order
    than its parent's (PARTITION OF takes the parent's)."""
    with scratch_database("postgres", "dumpcat") as (source, admin):
        admin.run_in(source, *NASTY)
        found, failures, changed = round_trip(source, DumpScope(kind="database"))
        assert failures == [[], []]
        assert set(changed) == {("column", "app.kid.id"), ("column", "app.kid.t"), ("constraint", "app.kid.base_t_check"),
                                ("relation", "app.kid"), ("column", "app.l_b.k"), ("column", "app.l_b.n"),
                                ("data", "app.l_b")}
        assert found.not_included == ("owners and privileges", "48 extension objects",
                                      "inheritance (1 child tables come back as tables of their own)")


def test_the_catalog_sends_the_same_queries_whatever_the_number_of_objects():
    """P5: a few queries per kind of object, never one per object (10,022 for 1,000 tables before)."""
    def schema(n: int) -> list[str]:
        made = [f"CREATE SCHEMA s{n}"]
        for i in range(1, n + 1):
            reference = f", ref int REFERENCES s{n}.t{i - 1} (id)" if i > 1 else ""
            made += [f"CREATE TABLE s{n}.t{i} (id serial PRIMARY KEY, v text COLLATE \"C\"{reference})",
                     f"CREATE INDEX ON s{n}.t{i} (v)", f"CREATE FUNCTION s{n}.f{i}(x int) RETURNS int LANGUAGE sql RETURN x",
                     f"CREATE VIEW s{n}.v{i} AS SELECT id FROM s{n}.t{i}", f"CREATE TYPE s{n}.e{i} AS ENUM ('a')",
                     f"CREATE DOMAIN s{n}.d{i} AS int CHECK (VALUE > {i})", f"COMMENT ON TABLE s{n}.t{i} IS 't'"]
        return made
    with scratch_database("postgres", "dumpcat") as (source, admin):
        admin.run_in(source, *schema(3), *schema(60))
        counts = {}
        for n in (3, 60):
            for ddl in (True, False):
                sent: list = []
                catalog_in(source, DumpScope(kind="schema", schema_name=f"s{n}"), ddl=ddl, count=sent)
                counts[(n, ddl)] = sent[0]
        assert counts[(3, True)] == counts[(60, True)] <= 45 and counts[(3, False)] == counts[(60, False)] <= 30, counts


def test_what_no_dump_writes_is_counted_in_its_header():
    """H6's minimum: everything of the scope not written is counted — extension objects, what the dump uses from an
    extension (L6) or a schema it does not hold (P4), inheritance (S19), foreign tables, operators and text search
    objects (P1); a whole database's event triggers, publications, large objects and casts. Range types (never their
    constructors), collations and aggregates are written (P1). A scratch database holds them all."""
    with scratch_database("postgres", "dumpcat") as (database, admin):
        admin.run_in(
            database,
            "CREATE SCHEMA ext", "CREATE EXTENSION citext WITH SCHEMA ext", "CREATE SCHEMA s",
            "CREATE TABLE s.base (id int PRIMARY KEY, email ext.citext)",
            "CREATE TABLE s.kid (extra text) INHERITS (s.base)",
            "CREATE AGGREGATE s.total(int) (sfunc = int4pl, stype = int)",
            "CREATE TYPE s.span AS RANGE (subtype = int4)",
            "CREATE FUNCTION s.same(a int, b int) RETURNS bool LANGUAGE sql IMMUTABLE RETURN a = b",
            "CREATE OPERATOR s.=== (leftarg = int, rightarg = int, function = s.same)",
            'CREATE COLLATION s.c_copy FROM "C"',
            "CREATE TEXT SEARCH CONFIGURATION s.ts (COPY = pg_catalog.simple)",
            "CREATE FOREIGN DATA WRAPPER zz_06_fdw", "CREATE SERVER zz_06_srv FOREIGN DATA WRAPPER zz_06_fdw",
            "CREATE FOREIGN TABLE s.far (a int) SERVER zz_06_srv",
            "CREATE FUNCTION s.on_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$BEGIN END$$",
            "CREATE EVENT TRIGGER zz_06_on_ddl ON ddl_command_start EXECUTE FUNCTION s.on_ddl()",
            "CREATE PUBLICATION zz_06_pub", "SELECT lo_create(0)",
            "CREATE CAST (int AS s.span) WITH INOUT",
            "CREATE SCHEMA other", "CREATE TYPE other.mood AS ENUM ('a')", "CREATE SEQUENCE other.seq",
            "CREATE TABLE other.far_away (n int)",
            "CREATE TABLE s.uses_other (m other.mood, n bigint DEFAULT nextval('other.seq'), r s.span_multirange)",
            "CREATE VIEW s.reads_other AS SELECT n FROM other.far_away",
        )
        found = catalog_in(database, DumpScope(kind="schema", schema_name="s"))
        assert found.not_included == (
            "owners and privileges", "1 foreign tables", "1 extension types (from citext)",
            "1 types from schemas not in the dump", "1 tables and views from schemas not in the dump",
            "1 sequences from schemas not in the dump", "1 operators", "1 text search configurations",
            "inheritance (1 child tables come back as tables of their own)")
        assert [(t.name, t.only) for t in found.tables] == [("s.base", True), ("s.kid", True), ("s.uses_other", True)]
        written = [(item.kind, item.name) for item in found.pre]
        assert [name for kind, name in written if kind == "function"] == [
            "s.same(a integer, b integer)", "s.on_ddl()"]  # not the range's constructors
        assert {("type", "s.span"), ("collation", "s.c_copy"), ("aggregate", "s.total(integer)")} <= set(written)
        kid = next(item for item in found.pre if item.name == "s.kid")
        assert "INHERITS" not in kid.statements[0] and "id integer NOT NULL" in kid.statements[0]  # S19
        one = catalog_in(database, DumpScope(kind="tables", schema_name="s", names=["base"]))
        assert one.not_included == ("owners and privileges", "1 extension types (from citext)",
                                    "1 inheritance child tables not in the dump (their rows are left out: FROM ONLY)")
        other = catalog_in(database, DumpScope(kind="tables", schema_name="s", names=["uses_other"]))
        assert ("type", "s.span") in [(item.kind, item.name) for item in other.pre]  # its multirange column needs it
        assert other.not_included == ("owners and privileges", "1 types from schemas not in the dump",
                                      "1 sequences from schemas not in the dump")
        whole = catalog_in(database, DumpScope(kind="database"))
        assert ("extension", "citext") in [(item.kind, item.name) for item in whole.pre]
        assert whole.scope == f"database {database}"
        extension_objects = [what for what in whole.not_included if what.endswith(" extension objects")]
        assert len(extension_objects) == 1 and int(extension_objects[0].split()[0]) > 10
        assert whole.not_included[-5:] == ("1 event triggers", "1 publications", "1 large objects", "1 casts",
                                           "inheritance (1 child tables come back as tables of their own)")
        assert not [what for what in whole.not_included if "extension types" in what or "schemas not" in what]
        assert ("type", "other.mood") in [(item.kind, item.name) for item in whole.pre]
