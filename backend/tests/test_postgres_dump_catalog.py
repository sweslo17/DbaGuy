"""What a PostgreSQL dump writes and in which order (spec ⑥ §3.3.3, R-06-2): a scratch schema with every kind of
object, created partly out of order so that only the catalog's dependencies can order it; what is not written is
counted (pre-flight H6). A `drop_first` script built from the catalog in the order Task 8's DumpScript writes it runs
twice into a scratch database and reads back the same (H4, H5, H6, H7); the data round trip is test_dump.py's.
Scratch: `zz_06_dumpcat_*` schemas, dropped with what is in them, and `zz_06_restore_*` / `zz_06_dumpcat_*`
databases, dropped by name."""

import psycopg
import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.postgres.dump import RLS_SNAPSHOT, SESSION, SNAPSHOT_TEXT
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound
from tests.data_support import dropping, scratch_database, scratch_name
from tests.edit_support import scratch_db
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_pg_profile, requires_pg

pytestmark = requires_pg

# The restore settings sql/dump_script.py writes after the header (spec §3.3.3)
RESTORE = ("SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false")


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
                f"CREATE AGGREGATE {s}.total(int) (sfunc = int4pl, stype = int)",
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


def catalog_in(database: str, scope: DumpScope, ddl: bool = True):
    """The catalog of a scratch database, read the way the dump's own session reads it."""
    conn = PostgresAdapter().open(make_pg_profile(database=database), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        for statement in SESSION.setup:
            conn.run(statement).close()
        return conn.dump_catalog(scope, DumpObjectKinds(), ddl=ddl)
    finally:
        conn.close()


def names(objects, schema: str) -> list[tuple[str, str]]:
    return [(item.kind, item.name.replace(f"{schema}.", "")) for item in objects]


def script(found) -> list[str]:
    """A `drop_first` dump of `found` without its data, in the order Task 8's DumpScript writes it: the restore
    settings, the DROPs (those of the objects after the data first, newest first), then the objects."""
    drops = [item.drop for item in (*reversed(found.post), *reversed(found.pre)) if item.drop]
    return [*RESTORE, *drops, *(s for item in (*found.pre, *found.post) for s in item.statements)]


def restore(database: str, statements: list[str]) -> None:
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=database,
                         autocommit=True) as conn:
        for statement in statements:
            try:
                conn.execute(statement)
            except psycopg.Error as exc:
                raise AssertionError(f"{statement[:300]}\n→ {exc}") from exc


def test_a_schema_dump_orders_every_object_by_its_dependencies(schema):
    db, name = schema
    found = catalog(db, DumpScope(kind="schema", schema_name=name))
    assert names(found.pre, name) == [
        ("schema", name), ("type", "mood"), ("domain", "pos"), ("type", "pair"), ("sequence", "counter"),
        ("sequence", "parent_id_seq"), ("function", "add1(i integer)"), ("function", "twice(i integer)"),
        ("function", "touch()"), ("table", "parent"), ("table", "Child Rows"), ("table", "events"),
        ("table", "events_2026"),
    ]
    # H4: the routines that read a relation come with the views, merged by creation order: parents() and n_parents()
    # are older than every view, v_top waits for v_base, v_n for n_parents()
    assert names(found.post, name) == [
        ("sequence value", "counter"), ("sequence value", "parent_id_seq"), ("sequence owner", "parent_id_seq"),
        ("sequence value", "Child Rows_id_seq"), ("index", "child_note"), ("index", "events_v"),
        ("foreign key", "Child Rows.Child Rows_parent_id_fkey"), ("function", "parents()"),
        ("function", "n_parents()"), ("view", "v_base"), ("view", "v_top"), ("materialized view", "mv"),
        ("view", "v_n"), ("trigger", "parent.touch"),
    ]
    assert [(t.name.replace(f"{name}.", ""), t.only) for t in found.tables] == [
        ("parent", True), ("Child Rows", True), ("events", False)]  # a partition's rows come with its parent's
    assert found.not_included == ("owners and privileges", "1 aggregates")
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
    assert all(item.section == "data" for item in found.post if item.kind == "sequence value")
    assert [t.references for t in found.tables] == [(), (f"{name}.parent",), ()]  # M9: a data-only dump's order
    drops = {item.name.replace(f"{name}.", ""): item.drop for item in (*found.pre, *found.post) if item.drop}
    assert drops["Child Rows.Child Rows_parent_id_fkey"] == (  # H7: a drop_first run drops it before any table
        f'ALTER TABLE IF EXISTS ONLY {name}."Child Rows" DROP CONSTRAINT IF EXISTS "Child Rows_parent_id_fkey"')
    assert "events_2026" not in drops and drops["events"] == f"DROP TABLE IF EXISTS {name}.events"


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
        "1 sequences (not chosen)", "1 aggregates", "5 functions and procedures (not chosen)")
    full = catalog(db, DumpScope(kind="schema", schema_name=name))
    plan = catalog(db, DumpScope(kind="schema", schema_name=name), ddl=False)
    assert names(plan.pre, name) == names(full.pre, name) and names(plan.post, name) == names(full.post, name)
    assert all(not item.statements for item in (*plan.pre, *plan.post))
    assert [t.name for t in plan.tables] == [t.name for t in full.tables]
    assert plan.not_included == full.not_included and plan.warnings == full.warnings


def test_a_drop_first_dump_restores_twice_and_reads_back_the_same(tmp_path):
    """H4–H7 on a server: a routine with a SQL-standard body over a table and a view on it, a partitioned table's
    foreign key, a child table older than its parent, and what H6 found left out — row-level security and its
    policies, a rule, a materialized view's index, a view's options, an INSTEAD OF trigger, a disabled trigger,
    extended statistics, identity options — with their comments, NOT VALID and a partition's own check."""
    db = scratch_db("postgres", tmp_path)
    try:
        with dropping(db) as made, scratch_database("postgres", "restore") as (database, _):
            name = scratch_name("dumpcat")
            made.append(("SCHEMA", name))
            s = f'"{name}"'
            db.execute(
                f"CREATE SCHEMA {s}",
                f"CREATE TYPE {s}.kind AS ENUM ('x', 'y')",
                f"COMMENT ON TYPE {s}.kind IS 'a kind'",
                f"CREATE DOMAIN {s}.qty AS int NOT NULL CHECK (VALUE >= 0)",  # NOT NULL once (PostgreSQL 17)
                f"CREATE FUNCTION {s}.is_even(i int) RETURNS bool LANGUAGE sql IMMUTABLE RETURN i % 2 = 0",
                f"CREATE DOMAIN {s}.even AS int CHECK ({s}.is_even(VALUE))",  # after the function, not with the types
                f"CREATE SEQUENCE {s}.counter START 5",
                f"COMMENT ON SEQUENCE {s}.counter IS 'it''s counting'",
                f"SELECT setval('{s}.counter', 42)",
                f"CREATE TABLE {s}.kid (id int PRIMARY KEY, mom_id int)",  # older than its parent (H7, M9)
                f"CREATE TABLE {s}.mom (id int PRIMARY KEY, q {s}.qty, e {s}.even)",
                f"CREATE FUNCTION {s}.moms() RETURNS bigint LANGUAGE sql STABLE BEGIN ATOMIC SELECT count(*) FROM "
                f"{s}.mom; END",
                f"CREATE TABLE {s}.numbered (n bigint DEFAULT {s}.moms())",  # a default on a routine that reads a table
                f"ALTER TABLE {s}.kid ADD CONSTRAINT kid_mom FOREIGN KEY (mom_id) REFERENCES {s}.mom (id)",
                f"COMMENT ON CONSTRAINT kid_mom ON {s}.kid IS 'its mother'",
                f"CREATE TABLE {s}.acct (id int GENERATED BY DEFAULT AS IDENTITY (START WITH 100 INCREMENT BY 10) "
                f"PRIMARY KEY, owner text COLLATE \"C\" NOT NULL DEFAULT current_user, k {s}.kind, n int) "
                "WITH (fillfactor = 70)",
                f"COMMENT ON CONSTRAINT acct_pkey ON {s}.acct IS 'the key'",
                f"ALTER TABLE {s}.acct ADD CONSTRAINT n_positive CHECK (n > 0) NOT VALID",
                f"CREATE INDEX acct_owner ON {s}.acct (owner)",
                f"COMMENT ON INDEX {s}.acct_owner IS 'by owner'",
                f"CREATE STATISTICS {s}.acct_stats (ndistinct) ON owner, k FROM {s}.acct",
                f"COMMENT ON STATISTICS {s}.acct_stats IS 'together'",
                f"ALTER TABLE {s}.acct ENABLE ROW LEVEL SECURITY",
                f"ALTER TABLE {s}.acct FORCE ROW LEVEL SECURITY",
                f"CREATE POLICY mine ON {s}.acct USING (owner = current_user)",
                f"CREATE POLICY dev_reads ON {s}.acct AS RESTRICTIVE FOR SELECT TO {PG_USER} USING (n IS NOT NULL)",
                f"COMMENT ON POLICY mine ON {s}.acct IS 'own rows'",
                f"CREATE RULE no_delete AS ON DELETE TO {s}.acct DO INSTEAD NOTHING",
                f"ALTER TABLE {s}.acct DISABLE RULE no_delete",
                f"CREATE FUNCTION {s}.stamp() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
                f"CREATE TRIGGER stamp BEFORE INSERT ON {s}.acct FOR EACH ROW EXECUTE FUNCTION {s}.stamp()",
                f"ALTER TABLE {s}.acct DISABLE TRIGGER stamp",
                f"COMMENT ON TRIGGER stamp ON {s}.acct IS 'stamps'",
                f"CREATE TABLE {s}.pt (id int PRIMARY KEY, acct_id int REFERENCES {s}.acct (id)) PARTITION BY RANGE (id)",
                f"CREATE TABLE {s}.pt_1 PARTITION OF {s}.pt FOR VALUES FROM (0) TO (100)",
                f"ALTER TABLE {s}.pt_1 ADD CONSTRAINT pt_1_small CHECK (id < 50)",  # the partition's own
                f"CREATE FUNCTION {s}.acct_count() RETURNS bigint LANGUAGE sql BEGIN ATOMIC SELECT count(*) FROM "
                f"{s}.acct; END",
                f"COMMENT ON FUNCTION {s}.acct_count() IS 'how many'",
                f"CREATE VIEW {s}.v_count AS SELECT {s}.acct_count() AS n",
                f"CREATE TRIGGER v_insert INSTEAD OF INSERT ON {s}.v_count FOR EACH ROW EXECUTE FUNCTION {s}.stamp()",
                f"CREATE VIEW {s}.v_acct WITH (security_barrier = true) AS SELECT id, n FROM {s}.acct WHERE n > 0 "
                "WITH CASCADED CHECK OPTION",
                f"COMMENT ON VIEW {s}.v_acct IS 'positive'",
                f"COMMENT ON COLUMN {s}.v_acct.n IS 'the n'",
                f"CREATE MATERIALIZED VIEW {s}.mv AS SELECT k, count(*) AS c FROM {s}.acct GROUP BY k",
                f"CREATE UNIQUE INDEX mv_k ON {s}.mv (k)",
                # H7: their DROPs come before those of the routine and the view they use
                f"CREATE POLICY big ON {s}.acct FOR DELETE USING ({s}.acct_count() > 0)",
                f"CREATE RULE mom_also AS ON INSERT TO {s}.mom DO ALSO INSERT INTO {s}.v_acct (id, n) VALUES (NEW.id, 1)",
            )
            scope = DumpScope(kind="schema", schema_name=name)
            found = catalog(db, scope)
            assert names(found.pre, name) == [
                ("schema", name), ("type", "kind"), ("domain", "qty"), ("sequence", "counter"),
                ("function", "is_even(i integer)"), ("domain", "even"), ("function", "stamp()"), ("table", "kid"),
                ("table", "mom"), ("function", "moms()"), ("table", "numbered"), ("table", "acct"), ("table", "pt"),
                ("table", "pt_1")]
            assert names(found.post, name) == [
                ("sequence value", "counter"), ("sequence value", "acct_id_seq"), ("index", "acct_owner"),
                ("foreign key", "kid.kid_mom"), ("foreign key", "pt.pt_acct_id_fkey"),
                ("check constraint", "acct.n_positive"), ("function", "acct_count()"), ("view", "v_count"),
                ("view", "v_acct"), ("materialized view", "mv"), ("index", "mv_k"), ("trigger", "acct.stamp"),
                ("trigger", "v_count.v_insert"), ("rule", "acct.no_delete"), ("rule", "mom.mom_also"),
                ("statistics", "acct_stats"), ("row-level security", "acct"), ("policy", "acct.mine"),
                ("policy", "acct.dev_reads"), ("policy", "acct.big")]
            text = {(item.kind, item.name.replace(f"{name}.", "")): item.statements for item in (*found.pre, *found.post)}
            q = name  # a scratch name needs no quotes
            assert text[("table", "acct")][0] == (
                f"CREATE TABLE {q}.acct (\n    id integer GENERATED BY DEFAULT AS IDENTITY (INCREMENT BY 10 START WITH "
                f"100),\n    owner text COLLATE pg_catalog.\"C\" DEFAULT CURRENT_USER NOT NULL,\n    k {q}.kind,\n"
                f"    n integer,\n    CONSTRAINT acct_pkey PRIMARY KEY (id)\n) WITH (fillfactor=70)")
            assert text[("table", "acct")][1:] == (f"COMMENT ON CONSTRAINT acct_pkey ON {q}.acct IS 'the key'",)
            assert text[("table", "pt_1")] == (
                f"CREATE TABLE {q}.pt_1 PARTITION OF {q}.pt\n    FOR VALUES FROM (0) TO (100)",
                f"ALTER TABLE ONLY {q}.pt_1 ADD CONSTRAINT pt_1_small CHECK (id < 50)")
            assert text[("foreign key", "pt.pt_acct_id_fkey")][0] == (  # H5: not ONLY on a partitioned table
                f"ALTER TABLE {q}.pt ADD CONSTRAINT pt_acct_id_fkey FOREIGN KEY (acct_id) REFERENCES {q}.acct(id)")
            assert text[("check constraint", "acct.n_positive")] == (
                f"ALTER TABLE ONLY {q}.acct ADD CONSTRAINT n_positive CHECK ((n > 0)) NOT VALID",)
            assert text[("view", "v_acct")] == (
                f"CREATE VIEW {q}.v_acct WITH (security_barrier=true) AS\n SELECT id,\n    n\n   FROM {q}.acct\n"
                "  WHERE n > 0\nWITH CASCADED CHECK OPTION",
                f"COMMENT ON VIEW {q}.v_acct IS 'positive'", f"COMMENT ON COLUMN {q}.v_acct.n IS 'the n'")
            assert text[("trigger", "acct.stamp")][1:] == (
                f"ALTER TABLE {q}.acct DISABLE TRIGGER stamp", f"COMMENT ON TRIGGER stamp ON {q}.acct IS 'stamps'")
            assert text[("trigger", "v_count.v_insert")][0].startswith(f"CREATE TRIGGER v_insert INSTEAD OF INSERT ON {q}.v_count")
            assert text[("rule", "acct.no_delete")][1] == f"ALTER TABLE {q}.acct DISABLE RULE no_delete"
            assert text[("row-level security", "acct")] == (
                f"ALTER TABLE {q}.acct ENABLE ROW LEVEL SECURITY", f"ALTER TABLE {q}.acct FORCE ROW LEVEL SECURITY")
            assert text[("policy", "acct.mine")] == (
                f"CREATE POLICY mine ON {q}.acct AS PERMISSIVE FOR ALL TO PUBLIC USING ((owner = CURRENT_USER))",
                f"COMMENT ON POLICY mine ON {q}.acct IS 'own rows'")
            assert text[("policy", "acct.dev_reads")] == (
                f"CREATE POLICY dev_reads ON {q}.acct AS RESTRICTIVE FOR SELECT TO {PG_USER} USING ((n IS NOT NULL))",)
            assert text[("sequence", "counter")][1] == f"COMMENT ON SEQUENCE {q}.counter IS 'it''s counting'"
            assert text[("sequence value", "acct_id_seq")] == (
                f"SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('{q}.acct', 'id'), 100, false)",)
            assert found.not_included == ("owners and privileges", f"roles: 1 row-level security policies name "
                                          f"{PG_USER}, which must exist where the file is run")
            assert found.snapshot_text == SNAPSHOT_TEXT + RLS_SNAPSHOT and not found.warnings  # the dev login bypasses
            assert [(t.name.replace(f"{name}.", ""), t.only, t.references) for t in found.tables] == [
                ("kid", True, (f"{name}.mom",)), ("mom", True, ()), ("numbered", True, ()), ("acct", True, ()),
                ("pt", False, (f"{name}.acct",))]
            assert text[("domain", "qty")] == (f"CREATE DOMAIN {q}.qty AS integer NOT NULL\n"
                                               "    CONSTRAINT qty_check CHECK (VALUE >= 0)",)
            for _ in range(2):  # H7: the second run drops what the first made, foreign keys first
                restore(database, script(found))
            again = catalog_in(database, scope)
            assert [(item.kind, item.name, item.statements, item.drop, item.section) for item in again.pre] == \
                [(item.kind, item.name, item.statements, item.drop, item.section) for item in found.pre]
            assert [(item.kind, item.name, item.statements, item.drop, item.section) for item in again.post] == \
                [(item.kind, item.name, item.statements, item.drop, item.section) for item in found.post]
            assert again.not_included == found.not_included and again.tables == found.tables
    finally:
        db.close()


def test_what_no_dump_writes_is_counted_in_its_header():
    """H6's minimum: everything of the scope not written is counted — extension objects, what the dump uses from an
    extension (L6) or a schema it does not hold, inheritance (S19), foreign tables, aggregates, range types (never
    their constructors), operators, collations, text search objects; a whole database's event triggers, publications,
    large objects and casts. A scratch database holds them all."""
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
            "CREATE TABLE s.uses_other (m other.mood, n bigint DEFAULT nextval('other.seq'), r s.span_multirange)",
        )
        found = catalog_in(database, DumpScope(kind="schema", schema_name="s"))
        assert found.not_included == (
            "owners and privileges", "1 foreign tables", "1 range types", "1 aggregates",
            "1 extension types (from citext)", "1 types from schemas not in the dump",
            "1 sequences from schemas not in the dump", "1 operators", "1 collations", "1 text search configurations",
            "inheritance (1 child tables come back as tables of their own)")
        assert [(t.name, t.only) for t in found.tables] == [("s.base", True), ("s.kid", True), ("s.uses_other", True)]
        assert [item.name for item in found.pre if item.kind == "function"] == [
            "s.same(a integer, b integer)", "s.on_ddl()"]  # not the range's constructors
        kid = next(item for item in found.pre if item.name == "s.kid")
        assert "INHERITS" not in kid.statements[0] and "id integer NOT NULL" in kid.statements[0]  # S19
        one = catalog_in(database, DumpScope(kind="tables", schema_name="s", names=["base"]))
        assert one.not_included == ("owners and privileges", "1 extension types (from citext)",
                                    "1 inheritance child tables not in the dump (their rows are left out: FROM ONLY)")
        other = catalog_in(database, DumpScope(kind="tables", schema_name="s", names=["uses_other"]))
        assert other.not_included == ("owners and privileges", "1 range types",  # its multirange column needs it
                                      "1 types from schemas not in the dump", "1 sequences from schemas not in the dump")
        whole = catalog_in(database, DumpScope(kind="database"))
        assert ("extension", "citext") in [(item.kind, item.name) for item in whole.pre]
        assert whole.scope == f"database {database}"
        extension_objects = [what for what in whole.not_included if what.endswith(" extension objects")]
        assert len(extension_objects) == 1 and int(extension_objects[0].split()[0]) > 10
        assert whole.not_included[-5:] == ("1 event triggers", "1 publications", "1 large objects", "1 casts",
                                           "inheritance (1 child tables come back as tables of their own)")
        assert not [what for what in whole.not_included if "extension types" in what or "schemas not" in what]
        assert ("type", "other.mood") in [(item.kind, item.name) for item in whole.pre]
