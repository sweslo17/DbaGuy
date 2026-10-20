"""PostgreSQL dump tests' independent check (06a Task 5 review, pre-flight P6), adopted from the reviewer's harness: a
catalog's objects are run into a scratch database the way sql/dump_script.py writes them (restore settings, the
`drop_first` DROPs, the objects before the data, the rows, the objects after it), and both databases are compared
through an inventory of pg_catalog and an md5 of every table's rows — never through the dump's own reader, which
cannot see what it never reads. The rows travel by COPY (the dump's INSERTs are Task 8's)."""

import time

import psycopg
from psycopg import sql

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.dump import DumpCatalog
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.postgres.dump import SESSION
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_pg_profile

RESTORE = ("SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false")


def connect(database: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=database,
                           autocommit=autocommit)


def catalog_in(database: str, scope: DumpScope, kinds: DumpObjectKinds | None = None, *, ddl: bool = True,
               count: list[int] | None = None) -> DumpCatalog:
    """The catalog of `database`, read the way the dump's own session reads it; `count` gets the number of queries
    the read sent."""
    conn = PostgresAdapter().open(make_pg_profile(database=database), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
    try:
        for statement in SESSION.setup:
            conn.run(statement).close()
        original = psycopg.Cursor.execute
        calls = [0]

        def counting(self, *args, **kwargs):
            calls[0] += 1
            return original(self, *args, **kwargs)

        psycopg.Cursor.execute = counting
        try:
            started = time.perf_counter()
            found = conn.dump_catalog(scope, kinds or DumpObjectKinds(), ddl=ddl)
            took = time.perf_counter() - started
        finally:
            psycopg.Cursor.execute = original
        if count is not None:
            count[:] = [calls[0], took]
        return found
    finally:
        conn.close()


def steps(found: DumpCatalog, drop_first: bool = True, data: bool = True) -> list[tuple[str, object]]:
    """(sql | data, payload) in DumpScript's order."""
    out: list[tuple[str, object]] = [("sql", s) for s in RESTORE]
    if drop_first:
        out += [("sql", item.drop) for item in (*reversed(found.post), *reversed(found.pre)) if item.drop]
    out += [("sql", s) for item in found.pre for s in item.statements]
    if data:
        out += [("data", table) for table in found.tables]
    return out + [("sql", s) for item in found.post for s in item.statements]


def _copy(src: psycopg.Connection, dst: psycopg.Connection, table) -> None:
    columns = [c for c in table.columns if c.copied]
    if not columns:
        return
    names = sql.SQL(", ").join(sql.Identifier(c.name) for c in columns)
    target = sql.Identifier(table.schema, table.table)
    only = sql.SQL("ONLY ") if table.only else sql.SQL("")
    with src.cursor().copy(sql.SQL("COPY (SELECT {} FROM {}{}) TO STDOUT").format(names, only, target)) as out:
        with dst.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, names)) as into:
            for chunk in out:
                into.write(chunk)


def restore(src_db: str, dst_db: str, found: DumpCatalog, *, all_or_nothing: bool = True, drop_first: bool = True,
            data: bool = True) -> list[tuple[str, str]]:
    """The failures, (statement, SQLSTATE: message). All or nothing ends in a rollback when there is one (each step
    runs in a savepoint only so that every failure is reported)."""
    failures = []
    with connect(src_db) as src, connect(dst_db, autocommit=not all_or_nothing) as dst:
        for what, payload in steps(found, drop_first, data):
            try:
                if all_or_nothing:
                    dst.execute("SAVEPOINT step")
                if what == "sql":
                    dst.execute(payload)
                else:
                    _copy(src, dst, payload)
                if all_or_nothing:
                    dst.execute("RELEASE SAVEPOINT step")
            except psycopg.Error as exc:
                failures.append((payload if what == "sql" else f"<data {payload.name}>",
                                 f"{exc.sqlstate}: {str(exc).splitlines()[0]}"))
                if all_or_nothing:
                    dst.execute("ROLLBACK TO SAVEPOINT step")
        if all_or_nothing:
            dst.rollback() if failures else dst.commit()
    return failures


_USER = "n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'"
_NOT_EXT = "NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.objid = {oid} AND e.deptype = 'e')"
INVENTORY = {
    "relation": f"""
SELECT n.nspname || '.' || c.relname, c.relkind, c.relpersistence, c.reloptions, c.relrowsecurity,
       c.relforcerowsecurity, c.relispartition, pg_get_expr(c.relpartbound, c.oid), pg_get_partkeydef(c.oid),
       c.relreplident, c.reloftype::regtype::text, c.relispopulated,
       (SELECT array_agg(h.inhparent::regclass::text ORDER BY h.inhseqno) FROM pg_inherits h WHERE h.inhrelid = c.oid),
       CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid) END, (SELECT amname FROM pg_am WHERE oid = c.relam),
       (SELECT array_agg(o) FROM unnest((SELECT t.reloptions FROM pg_class t WHERE t.oid = c.reltoastrelid)) o)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER} AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c') AND {_NOT_EXT.format(oid='c.oid')}
""",
    "column": f"""
SELECT n.nspname || '.' || c.relname || '.' || a.attname, a.attnum - (SELECT count(*) FROM pg_attribute x
         WHERE x.attrelid = a.attrelid AND x.attisdropped AND x.attnum < a.attnum),
       format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attidentity,
       a.attgenerated, (SELECT nn.nspname || '.' || co.collname FROM pg_collation co JOIN pg_namespace nn ON
       nn.oid = co.collnamespace WHERE co.oid = a.attcollation), a.attstorage, a.attcompression,
       a.attstattarget, a.attoptions, a.attislocal, a.attinhcount
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE {_USER} AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c') AND a.attnum > 0 AND NOT a.attisdropped
  AND {_NOT_EXT.format(oid='c.oid')}
""",
    "constraint": f"""
SELECT n.nspname || '.' || COALESCE(c.relname, t.typname) || '.' || k.conname, k.contype,
       pg_get_constraintdef(k.oid), k.convalidated, k.condeferrable, k.condeferred, k.conislocal, k.coninhcount,
       k.connoinherit
FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
LEFT JOIN pg_class c ON c.oid = k.conrelid LEFT JOIN pg_type t ON t.oid = k.contypid
WHERE {_USER} AND {_NOT_EXT.format(oid='COALESCE(c.oid, t.oid)')}
""",
    "index": f"""
SELECT n.nspname || '.' || c.relname, pg_get_indexdef(i.indexrelid), i.indisclustered, i.indisreplident, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER} AND {_NOT_EXT.format(oid='i.indrelid')}
""",
    "trigger": f"""
SELECT n.nspname || '.' || c.relname || '.' || t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER} AND NOT t.tgisinternal
""",
    "rule": f"""
SELECT n.nspname || '.' || c.relname || '.' || r.rulename, pg_get_ruledef(r.oid), r.ev_enabled
FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER} AND r.rulename <> '_RETURN'
""",
    "policy": f"""
SELECT n.nspname || '.' || c.relname || '.' || p.polname, p.polcmd, p.polpermissive,
       ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE r::regrole::text END FROM unnest(p.polroles) r),
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER}
""",
    "statistics": f"""
SELECT n.nspname || '.' || s.stxname, pg_get_statisticsobjdef(s.oid), s.stxstattarget
FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace WHERE {_USER}
""",
    "routine": f"""
SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', p.prokind,
       CASE WHEN p.prokind <> 'a' THEN pg_get_functiondef(p.oid) END,
       (SELECT row(a.aggkind, a.aggtransfn, a.aggtranstype::regtype, a.aggfinalfn, a.aggfinalextra, a.aggfinalmodify,
                   a.agginitval, a.aggcombinefn, a.aggsortop::regoperator, p.proparallel)::text
        FROM pg_aggregate a WHERE a.aggfnoid = p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE {_USER} AND {_NOT_EXT.format(oid='p.oid')}
""",
    "type": f"""
SELECT n.nspname || '.' || t.typname, t.typtype, format_type(t.typbasetype, t.typtypmod), t.typnotnull,
       pg_get_expr(t.typdefaultbin, 0),
       (SELECT nn.nspname || '.' || co.collname FROM pg_collation co JOIN pg_namespace nn ON nn.oid = co.collnamespace
        WHERE co.oid = t.typcollation),
       (SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid),
       (SELECT array_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod) || ' ' || a.attcollation::regcollation::text
                         ORDER BY a.attnum) FROM pg_attribute a
        WHERE a.attrelid = t.typrelid AND t.typtype = 'c' AND a.attnum > 0 AND NOT a.attisdropped),
       (SELECT row(r.rngsubtype::regtype, r.rngcollation::regcollation, (SELECT opcname FROM pg_opclass
                   WHERE oid = r.rngsubopc), r.rngcanonical, r.rngsubdiff, r.rngmultitypid::regtype)::text
        FROM pg_range r WHERE r.rngtypid = t.oid)
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE {_USER} AND t.typtype IN ('e', 'd', 'c', 'r', 'm', 'b')
  AND (t.typtype <> 'c' OR (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
  AND {_NOT_EXT.format(oid='t.oid')}
""",
    "sequence": f"""
SELECT n.nspname || '.' || c.relname, s.seqtypid::regtype, s.seqstart, s.seqincrement, s.seqmax, s.seqmin,
       s.seqcache, s.seqcycle, c.relpersistence,
       (SELECT d.refobjid::regclass::text || '.' || d.refobjsubid || d.deptype::text FROM pg_depend d
        WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
          AND d.deptype IN ('a', 'i'))
FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER}
""",
    "comment": """
SELECT (pg_identify_object(d.classoid, d.objoid, d.objsubid)).type || ' ' ||
       (pg_identify_object(d.classoid, d.objoid, d.objsubid)).identity, d.description
FROM pg_description d WHERE d.objoid >= 16384
  AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.objid = d.objoid AND e.deptype = 'e')
""",
    "collation": f"""
SELECT n.nspname || '.' || c.collname, (to_jsonb(c) - 'oid' - 'collnamespace' - 'collowner' - 'collversion')::text
FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace WHERE {_USER}
""",
    "extension": "SELECT extname, extnamespace::regnamespace::text, extversion FROM pg_extension",
}


def inventory(database: str) -> dict[tuple[str, str], tuple]:
    """Every user object's catalog row, every table's row count and rows' md5, every sequence's value."""
    found: dict[tuple[str, str], tuple] = {}
    with connect(database) as conn:
        conn.execute("SET search_path = ''")
        for kind, query in INVENTORY.items():
            for row in conn.execute(query):
                found[(kind, row[0])] = tuple(row[1:])
        tables = conn.execute(f"""SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {_USER} AND c.relkind IN ('r', 'm') AND c.relispopulated AND {_NOT_EXT.format(oid='c.oid')}""").fetchall()
        for schema, table in tables:
            rows = conn.execute(sql.SQL("SELECT count(*), md5(string_agg(t::text, E'\\n' ORDER BY t::text)) "
                                        "FROM ONLY {} t").format(sql.Identifier(schema, table))).fetchone()
            found[("data", f"{schema}.{table}")] = tuple(rows)
        for schema, name in conn.execute(f"""SELECT n.nspname, c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_USER} AND c.relkind = 'S'""").fetchall():
            value = conn.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(schema, name)))
            found[("sequence value", f"{schema}.{name}")] = tuple(value.fetchone())
    return found


def differences(a: dict, b: dict) -> dict[tuple[str, str], tuple]:
    """key → (source, restored) wherever they differ ('<missing>' on the side without it)."""
    return {key: (a.get(key, "<missing>"), b.get(key, "<missing>")) for key in sorted(set(a) | set(b), key=str)
            if a.get(key) != b.get(key)}
