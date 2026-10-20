"""What a PostgreSQL dump writes, and in which order (spec ⑥ §3.3.3), read inside the dump's snapshot on the native
loaders. The dump session's `search_path = ''` (P5) makes format_type, defaults and every pg_get_* function qualify
names.

Order. Before the data: schemas and extensions, then in one dependency order (pg_depend; otherwise this order, by
oid): types (enums first), sequences, the routines that read no relation, and the tables (a partition after its
parent) without their foreign keys and NOT VALID constraints, with their comments. A routine's body is not checked
(the header's check_function_bodies = false, P9) unless it is SQL-standard (BEGIN ATOMIC, RETURN …: parsed at CREATE,
pre-flight H4). A routine that reads a relation — its signature uses a row type (P9: 42704) or its SQL-standard body a
table (H4: 42P01) — waits for that relation: among the tables when a table's default, check or index needs it,
otherwise after the data. After the data: setval and OWNED BY, the tables' indexes, foreign keys and NOT VALID
constraints, then views, materialized views and the routines that read a relation in one dependency order (pg_depend
through pg_rewrite), the materialized views' indexes, triggers on tables and views (with a table's trigger state),
rules, extended statistics, row-level security and its policies (H6). What the catalog cannot order (a cycle) follows
in creation order (oid).

Every object that another can depend on, or that can depend on a view or a routine (a foreign key, a trigger, a rule,
a policy, statistics), has its DROP … IF EXISTS: a `drop_first` file drops the objects after the data first, newest
first, then the others (H7). A `tables` scope brings each table's partitions, the sequences its columns own or use,
the types and routines its columns, defaults, checks and indexes use, and its triggers with their functions. Every
object of the scope that is not written is counted in `not_included`, and so is what the dump uses and does not create
(an extension's or another schema's types, functions and sequences; H6, L6)."""

from collections import defaultdict
from dataclasses import dataclass

import psycopg

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound
from ...sql.dialects import Dialect, qualified, quote_ident, quote_literal
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering
from . import columns as pg_columns
from .ddl import sequence_ddl, storage_options, table_parts, type_ddl

SESSION = DumpSession(
    # row_security = off (pre-flight M10): a login that RLS would show only some rows to gets an error instead
    setup=("SET DateStyle = ISO, MDY", "SET IntervalStyle = postgres", "SET extra_float_digits = 3",
           "SET search_path = ''", "SET TimeZone = 'UTC'", "SET row_security = off",
           "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY"),
    teardown=("ROLLBACK",), consistency="snapshot",
)
SNAPSHOT_TEXT = "one consistent snapshot (REPEATABLE READ)"
RLS_SNAPSHOT = "; row_security off: row-level security hides no rows"  # when a dumped table has it (M10)
_FIRST_USER_OID = 16384  # FirstNormalObjectId: what initdb made (pg_catalog, information_schema) is below
_NOT_EXTENSION = ("NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = '{catalog}'::regclass AND e.objid = {oid} "
                  "AND e.deptype = 'e')")
_SCHEMAS = ("SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%%' "
            "AND n.nspname <> 'information_schema' ORDER BY n.nspname")
_SCHEMA = "SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname = %(name)s"
# `bypass`: this login reads every row whatever the policies say (superuser, BYPASSRLS, an owner without FORCE)
_RELATIONS = f"""
SELECT c.oid, n.nspname::text, c.relname::text, c.relkind::text, c.relispartition,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END,
       CASE WHEN c.relkind IN ('r', 'm') THEN pg_total_relation_size(c.oid) END,
       c.relrowsecurity, c.relforcerowsecurity, c.reloptions, c.relispopulated,
       (SELECT r.rolsuper OR r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user)
         OR (pg_has_role(c.relowner, 'USAGE') AND NOT c.relforcerowsecurity)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relnamespace = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND {_NOT_EXTENSION.format(catalog="pg_class", oid="c.oid")}
ORDER BY c.oid
"""
# Partitions and inheritance children of the scope's relations, wherever they live, and their parents
_INHERITS = """
SELECT h.inhrelid, h.inhparent, c.relispartition FROM pg_inherits h JOIN pg_class c ON c.oid = h.inhrelid
WHERE c.relkind IN ('r', 'p', 'f') AND (h.inhparent = ANY(%(relations)s) OR h.inhrelid = ANY(%(relations)s))
"""
# A sequence a column owns: a serial's (deptype a) or an identity's (i, which the table's DDL creates itself)
_OWNED_SEQUENCES = """
SELECT d.objid, d.deptype::text, d.refobjid, a.attname::text
FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
  AND d.objid IN (SELECT oid FROM pg_class WHERE relkind = 'S') AND d.refobjid = ANY(%(tables)s)
"""
_ANY_OWNED = ("SELECT objid FROM pg_depend WHERE classid = 'pg_class'::regclass AND deptype IN ('a', 'i') "
              "AND objid = ANY(%(oids)s)")
# Enums, domains and standalone composites, and (to be counted as left out) range and base types; never an array type,
# a multirange (a range makes its own), nor a relation's row type
_TYPES = f"""
SELECT t.oid, n.nspname::text, t.typname::text, t.typtype::text
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typnamespace = ANY(%(schemas)s) AND t.typisdefined AND {_NOT_EXTENSION.format(catalog="pg_type", oid="t.oid")}
  AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'b' AND t.typelem = 0)
       OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))
ORDER BY t.typtype <> 'e', t.oid
"""
# Every type of the scope's schemas, to read a dependency on an array type as one on its element, on a multirange as
# one on its range (PostgreSQL 14+), and on a row type as one on its relation
_TYPE_LINKS = """
SELECT t.oid, CASE WHEN t.typcategory = 'A' THEN t.typelem{multirange} ELSE 0 END, t.typrelid
FROM pg_type t WHERE t.typnamespace = ANY(%(schemas)s)
"""
_MULTIRANGE = "\n                   WHEN t.typtype = 'm' THEN COALESCE((SELECT r.rngtypid FROM pg_range r " \
              "WHERE r.rngmultitypid = t.oid), 0)"
# Never a routine another object made for itself (deptype i: a range type's constructors)
_ROUTINES = f"""
SELECT p.oid, n.nspname::text, p.proname::text, p.prokind::text, pg_get_function_identity_arguments(p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.pronamespace = ANY(%(schemas)s) AND {_NOT_EXTENSION.format(catalog="pg_proc", oid="p.oid")}
  AND NOT EXISTS (SELECT 1 FROM pg_depend i WHERE i.classid = 'pg_proc'::regclass AND i.objid = p.oid AND i.deptype = 'i')
ORDER BY p.oid
"""
# What an object of the scope uses (deptype n; the rest are an object's own parts), each object read as the one a dump
# writes: a column default, a constraint, a rule or an index as its relation or domain, a composite's relation as its
# type. `phase`: create — needed when the owner is created; index — by its relation's index; later — by its foreign
# key, policy, statistics or rule (written after everything they could use)
_DEPENDS = """
SELECT CASE WHEN c.relkind = 'c' OR k.contypid <> 0 OR d.classid = 'pg_type'::regclass THEN 't'
            WHEN c.relkind = 'S' THEN 's' WHEN d.classid = 'pg_proc'::regclass THEN 'f' ELSE 'r' END,
       CASE WHEN c.relkind = 'c' THEN c.reltype WHEN c.relkind IN ('i', 'I') THEN i.indrelid
            WHEN c.oid IS NOT NULL OR d.classid IN ('pg_type'::regclass, 'pg_proc'::regclass) THEN d.objid
            WHEN a.oid IS NOT NULL THEN a.adrelid WHEN k.contypid <> 0 THEN k.contypid WHEN k.oid IS NOT NULL THEN k.conrelid
            WHEN w.oid IS NOT NULL THEN w.ev_class WHEN p.oid IS NOT NULL THEN p.polrelid ELSE s.stxrelid END,
       CASE WHEN c.relkind IN ('i', 'I') THEN 'index'
            WHEN p.oid IS NOT NULL OR s.oid IS NOT NULL OR k.contype = 'f' OR w.rulename <> '_RETURN' THEN 'later'
            ELSE 'create' END,
       CASE WHEN d.refclassid = 'pg_class'::regclass THEN 'c' WHEN d.refclassid = 'pg_type'::regclass THEN 't'
            ELSE 'f' END,
       d.refobjid
FROM pg_depend d
LEFT JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
LEFT JOIN pg_index i ON c.relkind IN ('i', 'I') AND i.indexrelid = c.oid
LEFT JOIN pg_attrdef a ON d.classid = 'pg_attrdef'::regclass AND a.oid = d.objid
LEFT JOIN pg_constraint k ON d.classid = 'pg_constraint'::regclass AND k.oid = d.objid
LEFT JOIN pg_rewrite w ON d.classid = 'pg_rewrite'::regclass AND w.oid = d.objid
LEFT JOIN pg_policy p ON d.classid = 'pg_policy'::regclass AND p.oid = d.objid
LEFT JOIN pg_statistic_ext s ON d.classid = 'pg_statistic_ext'::regclass AND s.oid = d.objid
WHERE d.deptype = 'n' AND d.refobjid = ANY(%(refs)s)
  AND d.refclassid IN ('pg_class'::regclass, 'pg_type'::regclass, 'pg_proc'::regclass)
  AND d.classid IN ('pg_class'::regclass, 'pg_type'::regclass, 'pg_proc'::regclass, 'pg_attrdef'::regclass,
                    'pg_constraint'::regclass, 'pg_rewrite'::regclass, 'pg_policy'::regclass,
                    'pg_statistic_ext'::regclass)
"""
_EXTENSIONS = """
SELECT e.extname::text, n.nspname::text FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extnamespace = ANY(%(schemas)s) AND e.extname <> 'plpgsql' ORDER BY e.oid
"""
_EXTENSION_OBJECTS = """
SELECT count(*) FROM pg_depend d WHERE d.deptype = 'e' AND (
    (d.classid = 'pg_class'::regclass AND d.objid IN (SELECT oid FROM pg_class WHERE relnamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_proc'::regclass AND d.objid IN (SELECT oid FROM pg_proc WHERE pronamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_type'::regclass
     AND d.objid IN (SELECT oid FROM pg_type WHERE typnamespace = ANY(%(schemas)s) AND typelem = 0)))
"""
# What the dump's objects use that it does not create (pre-flight L6, H6): a type, function or sequence of an extension
# not in the dump, or of a schema not in it. The users: the relations' columns, defaults, checks, indexes, view
# queries, policies and statistics, the triggers written, the routines' signatures and bodies, the domains' base types
# and checks; an array type counts as its element
_USED_ELSEWHERE = f"""
WITH used AS (
    SELECT DISTINCT x.refclassid, COALESCE((SELECT NULLIF(t.typelem, 0) FROM pg_type t
                                            WHERE x.refclassid = 'pg_type'::regclass AND t.oid = x.refobjid
                                              AND t.typcategory = 'A'), x.refobjid) AS oid
    FROM pg_depend x
    WHERE x.deptype = 'n' AND x.refobjid >= {_FIRST_USER_OID}
      AND (x.refclassid IN ('pg_type'::regclass, 'pg_proc'::regclass)
           OR (x.refclassid = 'pg_class'::regclass AND (SELECT relkind FROM pg_class WHERE oid = x.refobjid) = 'S'))
      AND ((x.classid = 'pg_class'::regclass
            AND (x.objid = ANY(%(relations)s)
                 OR x.objid IN (SELECT indexrelid FROM pg_index WHERE indrelid = ANY(%(relations)s))))
        OR (x.classid = 'pg_attrdef'::regclass
            AND x.objid IN (SELECT oid FROM pg_attrdef WHERE adrelid = ANY(%(relations)s)))
        OR (x.classid = 'pg_constraint'::regclass AND x.objid IN (
            SELECT oid FROM pg_constraint WHERE conrelid = ANY(%(relations)s) OR contypid = ANY(%(types)s)))
        OR (x.classid = 'pg_rewrite'::regclass
            AND x.objid IN (SELECT oid FROM pg_rewrite WHERE ev_class = ANY(%(relations)s)))
        OR (x.classid = 'pg_proc'::regclass AND x.objid = ANY(%(routines)s))
        OR (x.classid = 'pg_type'::regclass AND x.objid = ANY(%(types)s))
        OR (x.classid = 'pg_trigger'::regclass AND x.objid = ANY(%(triggers)s))
        OR (x.classid = 'pg_policy'::regclass AND x.objid IN (SELECT oid FROM pg_policy WHERE polrelid = ANY(%(relations)s)))
        OR (x.classid = 'pg_statistic_ext'::regclass
            AND x.objid IN (SELECT oid FROM pg_statistic_ext WHERE stxrelid = ANY(%(relations)s))))
)
SELECT e.extname::text, CASE u.refclassid WHEN 'pg_type'::regclass THEN 'types' WHEN 'pg_proc'::regclass
                                          THEN 'functions' ELSE 'sequences' END, count(*)
FROM used u
LEFT JOIN pg_depend m ON m.classid = u.refclassid AND m.objid = u.oid AND m.deptype = 'e'
LEFT JOIN pg_extension e ON e.oid = m.refobjid
WHERE CASE WHEN e.oid IS NULL THEN u.oid <> ALL(%(scope)s) ELSE e.extname <> ALL(%(extensions)s) END
GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2 DESC
"""
# Indexes no constraint owns; a partition's index attached to its parent's is made again by the parent's, written ON
# (not ON ONLY) so that it reaches every partition
_INDEXES = """
SELECT i.indexrelid, i.indrelid, n.nspname::text, c.relname::text,
       CASE WHEN %(ddl)s THEN pg_get_indexdef(i.indexrelid) END
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indrelid = ANY(%(relations)s)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid)
  AND NOT EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = i.indexrelid)
ORDER BY i.indexrelid
"""
# A partition's copy of its parent's foreign key has a parent (conparentid): the parent's makes it again
_FOREIGN_KEYS = """
SELECT k.oid, k.conrelid, k.confrelid, k.conname::text, CASE WHEN %(ddl)s THEN pg_get_constraintdef(k.oid) END,
       k.confrelid::regclass::text
FROM pg_constraint k WHERE k.contype = 'f' AND k.conrelid = ANY(%(tables)s) AND k.conparentid = 0 ORDER BY k.oid
"""
# NOT VALID constraints: written inline they would be validated against rows that may break them (H6); a partition's
# own only (its parent's come with PARTITION OF)
_NOT_VALID = """
SELECT k.oid, k.conrelid, k.conname::text, CASE WHEN %(ddl)s THEN pg_get_constraintdef(k.oid) END
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE k.contype = 'c' AND NOT k.convalidated AND k.conrelid = ANY(%(tables)s)
  AND (NOT c.relispartition OR (k.conislocal AND k.coninhcount = 0))
ORDER BY k.oid
"""
# A partition's copy of its parent's trigger has a parent (tgparentid, 13+): the parent's makes it again
_TRIGGERS = """
SELECT t.oid, t.tgrelid, t.tgname::text, t.tgfoid, t.tgenabled::text,
       CASE WHEN %(ddl)s THEN pg_get_triggerdef(t.oid, false) END
FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid = ANY(%(relations)s){parent} ORDER BY t.oid
"""
_RULES = """
SELECT r.oid, r.ev_class, r.rulename::text, r.ev_enabled::text, CASE WHEN %(ddl)s THEN pg_get_ruledef(r.oid) END
FROM pg_rewrite r WHERE r.ev_class = ANY(%(relations)s) AND r.rulename <> '_RETURN' ORDER BY r.oid
"""
_STATISTICS = """
SELECT s.oid, s.stxrelid, n.nspname::text, s.stxname::text, CASE WHEN %(ddl)s THEN pg_get_statisticsobjdef(s.oid) END
FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace
WHERE s.stxrelid = ANY(%(relations)s) ORDER BY s.oid
"""
_POLICIES = """
SELECT p.oid, p.polrelid, p.polname::text, p.polcmd::text, p.polpermissive, 0 = ANY(p.polroles),
       ARRAY(SELECT r.rolname::text FROM unnest(p.polroles) WITH ORDINALITY u (oid, i)
             JOIN pg_roles r ON r.oid = u.oid ORDER BY u.i),
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p WHERE p.polrelid = ANY(%(tables)s) ORDER BY p.oid
"""
_COMMENTS = f"""
SELECT (SELECT relname FROM pg_class WHERE oid = d.classoid)::text, d.objoid, d.objsubid,
       (SELECT attname FROM pg_attribute WHERE attrelid = d.objoid AND attnum = d.objsubid AND d.objsubid > 0)::text,
       d.description
FROM pg_description d WHERE d.objoid >= {_FIRST_USER_OID}
"""
# Objects of the scope's schemas no dump writes (H6): counted for the header
_OTHER_OBJECTS = (
    ("operators", "pg_operator", "oprnamespace"), ("operator classes", "pg_opclass", "opcnamespace"),
    ("operator families", "pg_opfamily", "opfnamespace"), ("collations", "pg_collation", "collnamespace"),
    ("conversions", "pg_conversion", "connamespace"), ("text search configurations", "pg_ts_config", "cfgnamespace"),
    ("text search dictionaries", "pg_ts_dict", "dictnamespace"), ("text search parsers", "pg_ts_parser", "prsnamespace"),
    ("text search templates", "pg_ts_template", "tmplnamespace"),
)
_OTHER_COUNTS = "SELECT " + ", ".join(
    f"(SELECT count(*) FROM {table} o WHERE o.{column} = ANY(%(schemas)s) AND "
    f"{_NOT_EXTENSION.format(catalog=table, oid='o.oid')})" for _, table, column in _OTHER_OBJECTS)
# A whole database's objects that belong to no schema (H6)
_DATABASE_OBJECTS = ("event triggers", "publications", "large objects", "casts")
_DATABASE_COUNTS = f"""
SELECT (SELECT count(*) FROM pg_event_trigger o WHERE {_NOT_EXTENSION.format(catalog="pg_event_trigger", oid="o.oid")}),
       (SELECT count(*) FROM pg_publication), (SELECT count(*) FROM pg_largeobject_metadata),
       (SELECT count(*) FROM pg_cast o WHERE o.oid >= {_FIRST_USER_OID}
          AND {_NOT_EXTENSION.format(catalog="pg_cast", oid="o.oid")}
          AND NOT EXISTS (SELECT 1 FROM pg_depend i WHERE i.classid = 'pg_cast'::regclass AND i.objid = o.oid
                          AND i.deptype = 'i'))  -- not a range's own cast to its multirange
"""
_LEFT_OUT_TYPES = {"r": "range types", "b": "base types"}
_ROUTINE_KIND = {"f": "function", "w": "function", "p": "procedure"}
_POLICY_COMMAND = {"*": "ALL", "r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}
_STATE = {"D": "DISABLE", "R": "ENABLE REPLICA", "A": "ENABLE ALWAYS"}  # tgenabled / ev_enabled other than O
_TABLE_KINDS = ("r", "p")
_VIEW_KINDS = ("v", "m")


def _q(*parts: str) -> str:
    return ".".join(quote_ident(Dialect.POSTGRES, part) for part in parts)


def _literal(text: str) -> str:
    return quote_literal(Dialect.POSTGRES, text)


@dataclass(frozen=True)
class _Relation:
    """One row of _RELATIONS."""

    oid: int
    schema: str
    name: str
    relkind: str
    partition: bool
    rows: int | None
    size: int | None
    rls: bool
    force_rls: bool
    options: list[str] | None
    populated: bool
    bypass: bool

    @property
    def key(self) -> str:
        return f"{'s' if self.relkind == 'S' else 'r'}{self.oid}"

    @property
    def label(self) -> str:  # as the file names it
        return f"{self.schema}.{self.name}"

    @property
    def sql(self) -> str:
        return qualified(Dialect.POSTGRES, self.schema, self.name)


class _Reader:
    """One catalog read; `ddl` False (a dump plan) reads which objects there are, not their definitions. Objects are
    keyed `r<oid>` (a relation), `s<oid>` (a sequence), `t<oid>` (a type), `f<oid>` (a routine)."""

    def __init__(self, conn: psycopg.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.scope, self.kinds, self.ddl = conn, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.version = conn.info.server_version
        self.left_out: dict[str, int] = {}  # counted for the header's Not included
        self.noted: list[str] = []  # said in it as they are
        self.warnings: list[str] = []
        self.relations: dict[int, _Relation] = {}
        self.types: dict[int, tuple[str, str, str]] = {}  # oid → (schema, name, typtype)
        self.routines: dict[int, tuple] = {}  # oid → _ROUTINES row
        self.parents: dict[int, list[int]] = defaultdict(list)  # relation → the relations it inherits from
        self.partition_of: dict[int, int] = {}  # partition → its parent
        self.owners: dict[int, tuple[int, str]] = {}  # sequence → (table, column) it belongs to
        self.identity: set[int] = set()  # sequences of identity columns
        self.comments: dict[tuple[str, int], list[tuple[int, str | None, str]]] = defaultdict(list)
        self.create: dict[str, set[str]] = defaultdict(set)  # key → what it needs when it is created
        self.indexed: dict[str, set[str]] = defaultdict(set)  # relation key → what its indexes use
        self.uses: dict[str, set[str]] = defaultdict(set)  # key → everything it uses (created or later)
        self.dumped: set[int] = set()  # the tables written
        self.references: dict[int, set[int]] = defaultdict(set)  # table → the dumped tables its foreign keys reference
        self.scope_oids: list[int] = []  # every relation, type and routine of the scope's schemas

    # -- reading helpers

    def _ddl(self, *statements: str) -> tuple[str, ...]:
        return statements if self.ddl else ()

    def rows(self, sql: str, **params: object) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(sql, params)]

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def comment(self, catalog: str, oid: int, target: str, columns_of: str | None = None) -> tuple[str, ...]:
        """COMMENT ON `target` (`TYPE public.mood`) when the object has one; `columns_of`: its columns' too (a
        view's)."""
        if not self.ddl:
            return ()
        found = []
        for subid, column, text in sorted(self.comments.get((catalog, oid), ()), key=lambda c: c[0]):
            if subid == 0:
                found.append(f"COMMENT ON {target} IS {_literal(text)}")
            elif columns_of is not None and column is not None:
                found.append(f"COMMENT ON COLUMN {columns_of}.{_q(column)} IS {_literal(text)}")
        return tuple(found)

    def label_of(self, oid: int) -> str:
        if oid in self.relations:
            return self.relations[oid].label
        return self.rows("SELECT %(oid)s::regclass::text", oid=oid)[0][0]

    # -- the read

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        schemas = self._schemas()
        schema_oids = [oid for oid, _ in schemas]
        self.relations = {row[0]: _Relation(*row) for row in self.rows(_RELATIONS, schemas=schema_oids)}
        self._family()
        picked = self._picked()
        tables = [r for r in picked if r.relkind in _TABLE_KINDS]
        self.dumped = {r.oid for r in tables}
        views = [r for r in picked if r.relkind in _VIEW_KINDS and (self.kinds.views or self.named)]
        if not self.named and not self.kinds.views:
            self.leave_out("views and materialized views (not chosen)", sum(r.relkind in _VIEW_KINDS for r in picked))
        self.leave_out("foreign tables", sum(r.relkind == "f" for r in picked))
        self.types = {row[0]: row[1:] for row in self.rows(_TYPES, schemas=schema_oids)}
        self.routines = {row[0]: row for row in self.rows(_ROUTINES, schemas=schema_oids)}
        self._dependencies(schema_oids)
        if self.ddl:
            for catalog, oid, subid, column, text in self.rows(_COMMENTS):
                self.comments[(catalog, oid)].append((subid, column, text))
        written = [*tables, *views]
        triggers = self._triggers(written)
        sequences = self._sequences(tables)
        needed = self._needed(written, triggers, sequences)
        types = self._written_types(needed)
        routines = self._written_routines(needed)
        late = self._late(routines)
        # A routine that reads a relation and that a table's default, check or index (or a domain's check) needs is
        # made among the tables, after what it reads; the others wait for the views
        needs = self._closure({r.key for r in tables} | {f"t{oid}" for oid in types}, index=True)
        among_tables = [oid for oid in routines if oid in late and f"f{oid}" in needs]
        extensions = [] if self.named else self.rows(_EXTENSIONS, schemas=schema_oids)
        pre = [DumpObject("schema", name, self._ddl(f"CREATE SCHEMA IF NOT EXISTS {_q(name)}")) for _, name in schemas]
        pre += [DumpObject("extension", name,
                           self._ddl(f"CREATE EXTENSION IF NOT EXISTS {_q(name)} WITH SCHEMA {_q(schema)}"))
                for name, schema in extensions]
        if not self.named:
            self.leave_out("extension objects", self.rows(_EXTENSION_OBJECTS, schemas=schema_oids)[0][0])
        self._used_elsewhere(written, types, routines, [row[0] for row in triggers], [name for name, _ in extensions])
        pre += self._pre(types, sequences, [oid for oid in routines if oid not in late], tables, among_tables)
        after_data = [oid for oid in routines if oid in late and oid not in among_tables]
        post = [*self._sequence_values(sequences), *self._indexes(tables), *self._foreign_keys(tables),
                *self._not_valid(tables), *self._views_and_routines(views, after_data),
                *self._indexes([r for r in views if r.relkind == "m"]), *(self._trigger(row) for row in triggers),
                *self._rules(written), *self._statistics(written), *self._row_security(tables)]
        self._others(schema_oids)
        return pre, self._data(tables), post

    def _schemas(self) -> list[tuple[int, str]]:
        if self.scope.kind == "database":
            return self.rows(_SCHEMAS)
        found = self.rows(_SCHEMA, name=self.scope.schema_name)
        if not found:
            raise NotFound(f"Schema {self.scope.schema_name} not found")
        return found

    def _family(self) -> None:
        for child, parent, is_partition in self.rows(_INHERITS, relations=list(self.relations)):
            self.parents[child].append(parent)
            if is_partition:
                self.partition_of[child] = parent

    def _root(self, oid: int) -> int:
        """The dumped table whose SELECT reads a partition's rows: its topmost dumped ancestor (itself when its parent
        is not in the dump)."""
        while self.partition_of.get(oid) in self.dumped:
            oid = self.partition_of[oid]
        return oid

    def _picked(self) -> list[_Relation]:
        """The relations of the scope; a `tables` scope's partitioned tables bring their partitions."""
        if not self.named:
            return [r for r in self.relations.values() if r.relkind != "S"]
        wanted = set(self.scope.names)
        picked = [r for r in self.relations.values() if r.name in wanted and r.relkind != "S"]
        if missing := wanted - {r.name for r in picked}:
            raise NotFound(f"Not found in {self.scope.schema_name}: {', '.join(sorted(missing))}")
        children = defaultdict(list)
        for child, parent in self.partition_of.items():
            children[parent].append(child)
        have = {r.oid for r in picked}
        todo = [r.oid for r in picked if r.relkind == "p"]
        while todo:
            for child in children[todo.pop()]:
                if child not in have and child in self.relations:
                    have.add(child)
                    todo.append(child)
        return sorted((self.relations[oid] for oid in have), key=lambda r: r.oid)

    # -- dependencies

    def _key_of(self, ref_class: str, oid: int, links: dict[int, tuple[int, int]]) -> str | None:
        """A referenced object as the object a dump writes: an array type as its element, a row type as its
        relation; None when it is not one of the scope's."""
        if ref_class == "f":
            return f"f{oid}" if oid in self.routines else None
        if ref_class == "c":
            return self.relations[oid].key if oid in self.relations else None
        for _ in range(8):  # an array of a domain over an array …
            if oid in self.types:
                return f"t{oid}"
            element, relation = links.get(oid, (0, 0))
            if relation:
                return self.relations[relation].key if relation in self.relations else None
            if not element:
                return None
            oid = element
        return None

    def _dependencies(self, schema_oids: list[int]) -> None:
        sql = _TYPE_LINKS.format(multirange=_MULTIRANGE if self.version >= 140000 else "")
        links = {oid: (element, relation) for oid, element, relation in self.rows(sql, schemas=schema_oids)}
        self.scope_oids = [*self.relations, *self.routines, *links]  # every type is in links
        if not self.scope_oids:
            return
        for owner_kind, owner, phase, ref_class, ref in self.rows(_DEPENDS, refs=self.scope_oids):
            owner_key = self.relations[owner].key if owner_kind == "r" and owner in self.relations else \
                f"{owner_kind}{owner}"
            target = self._key_of(ref_class, ref, links)
            if target is None or target == owner_key:
                continue
            self.uses[owner_key].add(target)
            if phase == "create":
                self.create[owner_key].add(target)
            elif phase == "index":
                self.indexed[owner_key].add(target)

    def _closure(self, roots: set[str], index: bool = False, every: bool = False) -> set[str]:
        """The types, routines and sequences `roots` use, however deep (a domain's check, a signature, a SQL-standard
        body), never a relation: what they need when created (`index`: and their indexes), or (`every`) at all."""
        seen: set[str] = set()
        todo = list(roots)
        while todo:
            key = todo.pop()
            found = self.uses.get(key, set()) if every else self.create.get(key, set())
            if index:
                found = found | self.indexed.get(key, set())
            for ref in found:
                if ref[0] != "r" and ref not in seen:
                    seen.add(ref)
                    todo.append(ref)
        return seen

    def _needed(self, written: list[_Relation], triggers: list[tuple], sequences: list[int]) -> set[str]:
        """What the dump writes, and what that uses: its relations, their triggers' functions, its sequences, and in a
        schema or database scope the chosen kinds."""
        roots = {r.key for r in written} | {f"f{row[3]}" for row in triggers} | {f"s{oid}" for oid in sequences}
        if not self.named:
            if self.kinds.types:
                roots |= {f"t{oid}" for oid in self.types}
            if self.kinds.routines:
                roots |= {f"f{oid}" for oid, row in self.routines.items() if row[3] != "a"}
        return roots | self._closure(roots, every=True)

    def _written_types(self, needed: set[str]) -> list[int]:
        """The types the dump writes, in _TYPES order; range and base types are counted instead."""
        found, not_chosen = [], 0
        for oid, (_, _, typtype) in self.types.items():
            if f"t{oid}" not in needed:
                not_chosen += 1
            elif typtype in _LEFT_OUT_TYPES:
                self.leave_out(_LEFT_OUT_TYPES[typtype])
            else:
                found.append(oid)
        if not self.named and not self.kinds.types:
            self.leave_out("types (not chosen)", not_chosen)
        return found

    def _written_routines(self, needed: set[str]) -> list[int]:
        found, not_chosen = [], 0
        for oid, row in self.routines.items():
            if row[3] == "a":
                if not self.named or f"f{oid}" in needed:
                    self.leave_out("aggregates")
            elif f"f{oid}" in needed:
                found.append(oid)
            else:
                not_chosen += 1
        if not self.named and not self.kinds.routines:
            self.leave_out("functions and procedures (not chosen)", not_chosen)
        return found

    def _late(self, routines: list[int]) -> set[int]:
        """The routines that read a relation when they are created (a row type in their signature, P9; a SQL-standard
        body over a table, H4), or call one that does: they wait for it."""
        callers: dict[str, list[int]] = defaultdict(list)
        for oid in routines:
            for ref in self.create.get(f"f{oid}", ()):
                callers[ref].append(oid)
        late = {oid for oid in routines if any(ref[0] == "r" for ref in self.create.get(f"f{oid}", ()))}
        todo = list(late)
        while todo:
            for caller in callers[f"f{todo.pop()}"]:
                if caller not in late:
                    late.add(caller)
                    todo.append(caller)
        return late

    def _used_elsewhere(self, written: list[_Relation], types: list[int], routines: list[int], triggers: list[int],
                        extensions: list[str]) -> None:
        """What the dump's objects use and it does not create, counted: an extension's types and functions (L6), and
        the types, functions and sequences of schemas not in the dump. The file needs them where it is run."""
        if not (written or types or routines):
            return
        found: dict[tuple[bool, str], tuple[int, list[str]]] = {}
        for extension, what, count in self.rows(
                _USED_ELSEWHERE, relations=[r.oid for r in written], types=types, routines=routines,
                triggers=triggers, scope=self.scope_oids, extensions=extensions):
            total, names = found.get((extension is None, what), (0, []))
            found[(extension is None, what)] = (total + count, [*names, *([extension] if extension else [])])
        for (elsewhere, what), (count, names) in found.items():
            self.leave_out(f"{what} from schemas not in the dump" if elsewhere
                           else f"extension {what} (from {', '.join(names)})", count)

    # -- before the data

    def _pre(self, types: list[int], sequences: list[int], routines: list[int], tables: list[_Relation],
             among_tables: list[int]) -> list[DumpObject]:
        """Types, sequences, routines and tables in one dependency order: a partition after its parent, a type or a
        table after the routine its check or default calls, a routine that reads a relation after it (merged with the
        tables by creation order)."""
        among = sorted([*((r.oid, r.key) for r in tables), *((oid, f"f{oid}") for oid in among_tables)])
        keys = [*(f"t{oid}" for oid in types), *(f"s{oid}" for oid in sequences), *(f"f{oid}" for oid in routines),
                *(key for _, key in among)]
        ordering = Ordering({key: set(self.create.get(key, ())) for key in keys})
        for r in tables:
            if r.oid in self.partition_of:
                ordering.depends.setdefault(r.key, set()).add(f"r{self.partition_of[r.oid]}")
        made = {"t": self._type, "s": self._sequence, "f": lambda oid: self._routine(self.routines[oid]),
                "r": lambda oid: self._table(self.relations[oid])}
        return [made[key[0]](int(key[1:])) for key in ordering.order(keys)]

    def _type(self, oid: int) -> DumpObject:
        schema, name, typtype = self.types[oid]
        qname = _q(schema, name)
        word = "DOMAIN" if typtype == "d" else "TYPE"
        statements = (type_ddl(self.conn, qname).rstrip(";"), *self.comment("pg_type", oid, f"{word} {qname}")) \
            if self.ddl else ()
        return DumpObject(word.lower(), f"{schema}.{name}", statements, f"DROP {word} IF EXISTS {qname}")

    def _sequences(self, tables: list[_Relation]) -> list[int]:
        """Standalone sequences (unless left out), and those a dumped table's serial owns or its defaults use; never an
        identity's, which the table's DDL makes (its value is set after the data all the same)."""
        table_oids = [r.oid for r in tables]
        owned = self.rows(_OWNED_SEQUENCES, tables=table_oids) if table_oids else []
        self.owners = {sequence: (table, column) for sequence, _, table, column in owned}
        self.identity = {sequence for sequence, deptype, _, _ in owned if deptype == "i"}
        used = {sequence for sequence, deptype, _, _ in owned if deptype == "a"}
        used |= {int(ref[1:]) for r in tables for ref in self.uses.get(r.key, ()) if ref[0] == "s"}
        used &= set(self.relations)  # one of another schema is counted with what the dump uses elsewhere
        self.identity &= set(self.relations)
        if self.named:
            return sorted(used - self.identity)
        sequences = [r.oid for r in self.relations.values() if r.relkind == "S"]
        anyone_owns = {oid for (oid,) in self.rows(_ANY_OWNED, oids=sequences)} if sequences else set()
        standalone = set(sequences) - anyone_owns
        self.leave_out("sequences owned by tables not in the dump", len(anyone_owns - used - self.identity))
        if not self.kinds.sequences:
            self.leave_out("sequences (not chosen)", len(standalone - used))
            return sorted(used - self.identity)
        return sorted((used | standalone) - self.identity)

    def _sequence(self, oid: int) -> DumpObject:
        relation = self.relations[oid]
        statements = (sequence_ddl(self.conn, relation.sql).rstrip(";"),
                      *self.comment("pg_class", oid, f"SEQUENCE {relation.sql}")) if self.ddl else ()
        return DumpObject("sequence", relation.label, statements, f"DROP SEQUENCE IF EXISTS {relation.sql}")

    def _routine(self, row: tuple) -> DumpObject:
        oid, schema, name, kind, arguments = row
        word = "PROCEDURE" if kind == "p" else "FUNCTION"
        signature = f"{_q(schema, name)}({arguments})"
        statements: tuple[str, ...] = ()
        if self.ddl:
            (definition,) = self.rows("SELECT pg_get_functiondef(%(oid)s)", oid=oid)[0]
            statements = (definition.rstrip().rstrip(";"), *self.comment("pg_proc", oid, f"{word} {signature}"))
        return DumpObject(_ROUTINE_KIND[kind], f"{schema}.{name}({arguments})", statements,
                          f"DROP {word} IF EXISTS {signature}")

    def _table(self, relation: _Relation) -> DumpObject:
        statements: tuple[str, ...] = ()
        if self.ddl:
            parts = table_parts(self.conn, relation.schema, relation.name)
            statements = (parts.create(foreign_keys=False, not_valid=False),
                          *parts.partition_constraints(not_valid=False), *parts.comments(),
                          *(comment for c in parts.constraints if c.validated and c.kind != "f"
                            for comment in self.comment("pg_constraint", c.oid,
                                                        f"CONSTRAINT {_q(c.name)} ON {relation.sql}")))
        parent = self.partition_of.get(relation.oid)
        if parent is not None and parent not in self.dumped:
            self.warnings.append(f"{relation.label} is a partition of {self.label_of(parent)}, which is not in this "
                                 "dump: the file restores it only where that table exists")
        drop = None if parent in self.dumped else f"DROP TABLE IF EXISTS {relation.sql}"  # a partition goes with it
        return DumpObject("table", relation.label, statements, drop)

    # -- after the data

    def _sequence_values(self, sequences: list[int]) -> list[DumpObject]:
        """setval for every sequence of the dump, an identity's too (P2: rows written with their ids leave it where it
        was), and OWNED BY for a serial's."""
        found = []
        for oid in [*sequences, *sorted(self.identity)]:
            relation = self.relations[oid]
            statements: list[str] = []
            if self.ddl:
                last_value, called = self.rows(f"SELECT last_value, is_called FROM {relation.sql}")[0]
                if oid in self.identity:  # its name is the server's to choose: found again through the column
                    table, column = self.owners[oid]
                    target = (f"pg_catalog.pg_get_serial_sequence({_literal(self.relations[table].sql)}, "
                              f"{_literal(column)})")
                else:
                    target = _literal(relation.sql)
                statements.append(f"SELECT pg_catalog.setval({target}, {last_value}, {'true' if called else 'false'})")
            found.append(DumpObject("sequence value", relation.label, tuple(statements), section="data"))
            if oid in self.owners and oid not in self.identity:
                table, column = self.owners[oid]
                found.append(DumpObject("sequence owner", relation.label, self._ddl(
                    f"ALTER SEQUENCE {relation.sql} OWNED BY {self.relations[table].sql}.{_q(column)}")))
        return found

    def _indexes(self, relations: list[_Relation]) -> list[DumpObject]:
        if not relations:
            return []
        found = []
        for oid, _, schema, name, definition in self.rows(_INDEXES, relations=[r.oid for r in relations], ddl=self.ddl):
            statements = (definition.replace(" ON ONLY ", " ON ", 1),
                          *self.comment("pg_class", oid, f"INDEX {_q(schema, name)}")) if self.ddl else ()
            found.append(DumpObject("index", f"{schema}.{name}", statements))
        return found

    def _foreign_keys(self, tables: list[_Relation]) -> list[DumpObject]:
        """Only those whose referenced table is in the dump (§3.3.1): one to a table left out fails on an empty
        database, and rolls an all-or-nothing run back. The header lists the others. A partitioned table's is added
        without ONLY (H5), and each has its DROP (H7: a `drop_first` run drops them before any table)."""
        if not tables:
            return []
        found = []
        for oid, table, referenced, name, definition, referenced_name in self.rows(
                _FOREIGN_KEYS, tables=sorted(self.dumped), ddl=self.ddl):
            relation = self.relations[table]
            if referenced not in self.dumped:
                target = self.relations[referenced].label if referenced in self.relations else referenced_name
                self.warnings.append(f"Foreign key {name} of {relation.label} is left out: it references {target}, "
                                     "which is not in this dump")
                self.leave_out("foreign keys to tables not in the dump")
                continue
            if self._root(referenced) != self._root(table):  # M9: what a data-only dump writes first
                self.references[self._root(table)].add(self._root(referenced))
            only = "" if relation.relkind == "p" else "ONLY "
            statements = (f"ALTER TABLE {only}{relation.sql} ADD CONSTRAINT {_q(name)} {definition}",
                          *self.comment("pg_constraint", oid, f"CONSTRAINT {_q(name)} ON {relation.sql}")) \
                if self.ddl else ()
            found.append(DumpObject(
                "foreign key", f"{relation.label}.{name}", statements,
                f"ALTER TABLE IF EXISTS {only}{relation.sql} DROP CONSTRAINT IF EXISTS {_q(name)}"))
        return found

    def _not_valid(self, tables: list[_Relation]) -> list[DumpObject]:
        """NOT VALID checks, after the data: inline, CREATE TABLE would validate them against rows that may break
        them."""
        if not tables:
            return []
        found = []
        for oid, table, name, definition in self.rows(_NOT_VALID, tables=[r.oid for r in tables], ddl=self.ddl):
            relation = self.relations[table]
            only = "" if relation.relkind == "p" else "ONLY "
            statements = (f"ALTER TABLE {only}{relation.sql} ADD CONSTRAINT {_q(name)} {definition}",
                          *self.comment("pg_constraint", oid, f"CONSTRAINT {_q(name)} ON {relation.sql}")) \
                if self.ddl else ()
            found.append(DumpObject("check constraint", f"{relation.label}.{name}", statements))
        return found

    def _views_and_routines(self, views: list[_Relation], routines: list[int]) -> list[DumpObject]:
        """Views, materialized views and the routines that read a relation in one dependency order (H4), merged by
        oid: a view after the view or routine it reads, a routine after the relation its signature or body uses."""
        merged = sorted([*((r.oid, r.key) for r in views), *((oid, f"f{oid}") for oid in routines)])
        keys = [key for _, key in merged]
        ordering = Ordering({key: set(self.create.get(key, ())) for key in keys})
        return [self._view(self.relations[int(key[1:])]) if key[0] == "r" else self._routine(self.routines[int(key[1:])])
                for key in ordering.order(keys)]

    def _view(self, relation: _Relation) -> DumpObject:
        materialized = relation.relkind == "m"
        head = "MATERIALIZED VIEW" if materialized else "VIEW"
        statements: tuple[str, ...] = ()
        if self.ddl:
            (definition,) = self.rows("SELECT pg_get_viewdef(%(oid)s::oid, true)", oid=relation.oid)[0]
            options = [o for o in relation.options or () if not o.startswith("check_option=")]
            check = next((o.partition("=")[2] for o in relation.options or () if o.startswith("check_option=")), None)
            storage = f" WITH ({', '.join(storage_options(options))})" if options else ""
            statement = f"CREATE {head} {relation.sql}{storage} AS\n{definition.rstrip().rstrip(';')}"
            if check:  # H6: CHECK OPTION is a view option, not part of the view's definition
                statement += f"\nWITH {check.upper()} CHECK OPTION"
            if materialized and not relation.populated:
                statement += "\nWITH NO DATA"
            statements = (statement, *self.comment("pg_class", relation.oid, f"{head} {relation.sql}", relation.sql))
        return DumpObject(head.lower(), relation.label, statements, f"DROP {head} IF EXISTS {relation.sql}")

    def _triggers(self, relations: list[_Relation]) -> list[tuple]:
        """The triggers on the dump's tables and views (a view's INSTEAD OF, H6), unless left out."""
        if not relations:
            return []
        sql = _TRIGGERS.format(parent=" AND t.tgparentid = 0" if self.version >= 130000 else "")
        found = self.rows(sql, relations=[r.oid for r in relations], ddl=self.ddl)
        if self.kinds.triggers:
            return found
        self.leave_out("triggers (not chosen)", len(found))
        return []

    def _trigger(self, row: tuple) -> DumpObject:
        oid, table, name, _, enabled, definition = row
        relation = self.relations[table]
        statements: tuple[str, ...] = ()
        if self.ddl:
            state = _STATE.get(enabled) if relation.relkind in _TABLE_KINDS else None  # a view's is always on
            statements = (definition, *([f"ALTER TABLE {relation.sql} {state} TRIGGER {_q(name)}"] if state else []),
                          *self.comment("pg_trigger", oid, f"TRIGGER {_q(name)} ON {relation.sql}"))
        return DumpObject("trigger", f"{relation.label}.{name}", statements,
                          f"DROP TRIGGER IF EXISTS {_q(name)} ON {relation.sql}")

    def _rules(self, relations: list[_Relation]) -> list[DumpObject]:
        if not relations:
            return []
        found = []
        for oid, table, name, enabled, definition in self.rows(_RULES, relations=[r.oid for r in relations],
                                                               ddl=self.ddl):
            relation = self.relations[table]
            statements: tuple[str, ...] = ()
            if self.ddl:
                state = _STATE.get(enabled) if relation.relkind in _TABLE_KINDS else None
                statements = (definition.rstrip().rstrip(";"),
                              *([f"ALTER TABLE {relation.sql} {state} RULE {_q(name)}"] if state else []),
                              *self.comment("pg_rewrite", oid, f"RULE {_q(name)} ON {relation.sql}"))
            found.append(DumpObject("rule", f"{relation.label}.{name}", statements,
                                    f"DROP RULE IF EXISTS {_q(name)} ON {relation.sql}"))
        return found

    def _statistics(self, relations: list[_Relation]) -> list[DumpObject]:
        if not relations:
            return []
        found = []
        for oid, _, schema, name, definition in self.rows(_STATISTICS, relations=[r.oid for r in relations],
                                                          ddl=self.ddl):
            statements = (definition, *self.comment("pg_statistic_ext", oid, f"STATISTICS {_q(schema, name)}")) \
                if self.ddl else ()
            found.append(DumpObject("statistics", f"{schema}.{name}", statements,
                                    f"DROP STATISTICS IF EXISTS {_q(schema, name)}"))
        return found

    def _row_security(self, tables: list[_Relation]) -> list[DumpObject]:
        """ENABLE (and FORCE) ROW LEVEL SECURITY and the policies (H6), last: the rows are in by then. A policy names
        roles, which the header notes: they must exist where the file is run."""
        found = [DumpObject("row-level security", r.label, self._ddl(
            f"ALTER TABLE {r.sql} ENABLE ROW LEVEL SECURITY",
            *([f"ALTER TABLE {r.sql} FORCE ROW LEVEL SECURITY"] if r.force_rls else []))) for r in tables if r.rls]
        self.warnings += [f"Row-level security on {r.label}: this login is not its owner, a superuser or BYPASSRLS, so "
                          "reading its rows fails (a dump reads with row_security off: it never leaves rows out "
                          "silently)" for r in tables if r.rls and not r.bypass]
        if not tables:
            return found
        roles: dict[str, None] = {}
        naming = 0
        for oid, table, name, command, permissive, public, names, using, check in self.rows(
                _POLICIES, tables=[r.oid for r in tables]):
            relation = self.relations[table]
            if names:
                naming += 1
                roles.update(dict.fromkeys(names))
            to = "PUBLIC" if public else ", ".join(quote_ident(Dialect.POSTGRES, role) for role in names)
            statement = (f"CREATE POLICY {_q(name)} ON {relation.sql} AS {'PERMISSIVE' if permissive else 'RESTRICTIVE'}"
                         f" FOR {_POLICY_COMMAND[command]} TO {to}")
            statement += f" USING ({using})" if using is not None else ""
            statement += f" WITH CHECK ({check})" if check is not None else ""
            found.append(DumpObject("policy", f"{relation.label}.{name}", self._ddl(
                statement, *self.comment("pg_policy", oid, f"POLICY {_q(name)} ON {relation.sql}")),
                f"DROP POLICY IF EXISTS {_q(name)} ON {relation.sql}"))
        if naming:
            shown = ", ".join([*list(roles)[:10], *(["…"] if len(roles) > 10 else [])])
            self.noted.append(f"roles: {naming:,} row-level security policies name {shown}, which must exist where "
                              "the file is run")
        return found

    # -- what is left out, and the data

    def _others(self, schema_oids: list[int]) -> None:
        """What the scope holds that no dump writes: inheritance (S19), partitions out of reach, the schemas' other
        objects and a database's own (H6)."""
        children = [oid for oid in self.parents if oid not in self.partition_of]
        self.leave_out("inheritance child tables not in the dump (their rows are left out: FROM ONLY)",
                       sum(1 for oid in children if oid not in self.dumped and set(self.parents[oid]) & self.dumped))
        if inheriting := sum(1 for oid in children if oid in self.dumped):  # S19: no INHERITS is written
            self.noted.append(f"inheritance ({inheriting:,} child tables come back as tables of their own)")
        cut_off = sorted({self.relations[parent].label for oid, parent in self.partition_of.items()
                          if parent in self.dumped and oid not in self.dumped})
        if cut_off:
            self.warnings.append(f"Partitions of {', '.join(cut_off)} are not in this dump (another schema, or a "
                                 "foreign table): their rows are, and need those partitions where the file is run")
        if self.named:
            return
        for (what, _, _), count in zip(_OTHER_OBJECTS, self.rows(_OTHER_COUNTS, schemas=schema_oids)[0]):
            self.leave_out(what, count)
        if self.scope.kind == "database":
            for what, count in zip(_DATABASE_OBJECTS, self.rows(_DATABASE_COUNTS)[0]):
                self.leave_out(what, count)

    def _data(self, tables: list[_Relation]) -> list[DumpTable]:
        """The tables whose rows are written, in creation order: a partition's rows come with its parent's (a
        partitioned table's estimates are its partitions')."""
        found = []
        for r in tables:
            if self._root(r.oid) != r.oid:
                continue
            rows, size = r.rows, r.size
            if r.relkind == "p":
                parts = [p for p in tables if p.relkind == "r" and self._root(p.oid) == r.oid]
                known = [p.rows for p in parts if p.rows is not None]
                rows, size = (sum(known) if known else None), sum(p.size or 0 for p in parts)
            found.append(DumpTable(
                schema=r.schema, table=r.name, name=r.label, columns=tuple(pg_columns.read_columns(self.conn, r.oid)),
                rows_estimate=rows, bytes_estimate=size, only=r.relkind == "r",
                references=tuple(self.relations[oid].label for oid in sorted(self.references.get(r.oid, ())))))
        return found


def dump_catalog(
    conn: psycopg.Connection, scope: DumpScope, kinds: DumpObjectKinds, server: str, database: str, *, ddl: bool = True,
) -> DumpCatalog:
    reader = _Reader(conn, scope, kinds, ddl)
    pre, tables, post = reader.read()
    where = f"database {database}" + (f" · schema {scope.schema_name}" if scope.schema_name else "")
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items()), *reader.noted]
    snapshot = SNAPSHOT_TEXT + (RLS_SNAPSHOT if any(item.kind == "row-level security" for item in post) else "")
    return DumpCatalog(server=server, scope=where, consistency="snapshot", pre=tuple(pre), tables=tuple(tables),
                       post=tuple(post), not_included=tuple(left_out), warnings=tuple(reader.warnings),
                       snapshot_text=snapshot)
