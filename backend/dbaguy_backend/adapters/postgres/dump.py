"""What a PostgreSQL dump writes, and in which order (spec ⑥ §3.3.3), read inside the dump's snapshot on the native
loaders. The dump session's `search_path = ''` (P5) makes format_type, defaults and every pg_get_* function qualify
names. Every kind of object is read in one query whatever its number (pre-flight P5, `dump_queries.py`).

Order. Before the data: schemas and extensions, then in one dependency order (pg_depend; otherwise this order, by
oid): collations, types (enums first; ranges too), sequences, the routines that read no relation (aggregates
included), and the tables (a partition after its parent) without their foreign keys and NOT VALID constraints, with
their column settings and comments. A routine's body is not checked (the header's check_function_bodies = false, P9)
unless it is SQL-standard (BEGIN ATOMIC, RETURN …: parsed at CREATE, H4). A routine that reads a relation — its
signature uses a row type (P9: 42704) or its SQL-standard body a table (H4: 42P01) — waits for that relation: among the
tables when a table's default, check or index needs it, otherwise after the data. One that reads a view waits for the
views, and so do the table defaults and checks that call it (P3: added with ALTER TABLE after the views); a column
typed as a view's row type is not written. After the data: setval and OWNED BY, the tables' indexes, foreign keys,
NOT VALID constraints (tables' and domains'), replica identity and CLUSTER ON, then views, materialized views and the
routines that read a relation in one dependency order (pg_depend through pg_rewrite), the held-back defaults and
checks, the materialized views' indexes, triggers on tables and views (with a table's or a partition's trigger
state), rules, extended statistics, row-level security and its policies (H6). What the catalog cannot order (a cycle)
follows in creation order (oid).

Every object that another can depend on, or that can depend on a view or a routine (a foreign key, a trigger, a rule,
a policy, statistics), has its DROP … IF EXISTS: a `drop_first` file drops the objects after the data first, newest
first, then the others (H7). A `tables` scope brings each table's partitions, the sequences its columns own or use,
the collations, types and routines its columns, defaults, checks, indexes and triggers use, and its triggers. Every
object of the scope that is not written is counted in `not_included` — operators, operator classes and text search
objects with the object that uses each (P1) — and so is what the dump uses and does not create (an extension's or
another schema's types, functions, sequences, tables and views; H6, L6, P4)."""

import re
from collections import defaultdict
from dataclasses import dataclass, replace

import psycopg

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound
from ...sql.dialects import Dialect, qualified, quote_ident, quote_literal
from ..columns import TableColumn
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering
from . import columns as pg_columns
from . import dump_queries as q
from .ddl import NOTHING_HELD, Held, TableParts, TypeParts, sequence_ddl_of, storage_options, table_parts_of, \
    type_parts_of

SESSION = DumpSession(
    # row_security = off (pre-flight M10): a login that RLS would show only some rows to gets an error instead
    setup=("SET DateStyle = ISO, MDY", "SET IntervalStyle = postgres", "SET extra_float_digits = 3",
           "SET search_path = ''", "SET TimeZone = 'UTC'", "SET row_security = off",
           "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY"),
    teardown=("ROLLBACK",), consistency="snapshot",
    # pre-flight L11: a filter reads as the table-data tab does (its unqualified function, a citext column's `=`); the
    # catalog's text is read by then, and without a filter the rows stay read under '' (regclass values in full)
    filter_setup=("SET search_path TO DEFAULT",),
)
SNAPSHOT_TEXT = "one consistent snapshot (REPEATABLE READ)"
RLS_SNAPSHOT = "; row_security off: row-level security hides no rows"  # when a dumped table has it (M10)
VIEW_ROW_TYPE = "column typed as a view's row type"  # not written (P3): the file creates tables before views
NAMED_USES = 10  # what uses an object no dump writes: named up to this many, the rest counted
_PUBLIC_COMMENT = ("public", "standard public schema")  # the server's own: COMMENT ON it needs its owner
_SEQUENCE_VALUES_CHUNK = 200  # sequences read per UNION ALL
_ON_ONLY = re.compile(r'^(CREATE (?:UNIQUE )?INDEX (?:"(?:[^"]|"")*"|[^\s"]+) ON )ONLY ')  # P6: never the name
_TYPE_WORDS = {"d": "domain", "e": "type", "c": "type", "r": "type"}
_ROUTINE_KIND = {"f": "function", "w": "function", "p": "procedure", "a": "aggregate"}
_ROUTINE_WORD = {"f": "FUNCTION", "w": "FUNCTION", "p": "PROCEDURE", "a": "AGGREGATE"}
_RELATION_WORDS = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}
_POLICY_COMMAND = {"*": "ALL", "r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}
_STATE = {"D": "DISABLE", "R": "ENABLE REPLICA", "A": "ENABLE ALWAYS"}  # tgenabled / ev_enabled other than O
_MODIFY = {"r": "READ_ONLY", "s": "SHAREABLE", "w": "READ_WRITE"}
_PARALLEL = {"s": "SAFE", "r": "RESTRICTED"}
_TABLE_KINDS = ("r", "p")
_VIEW_KINDS = ("v", "m")


def _q(*parts: str) -> str:
    return ".".join(quote_ident(Dialect.POSTGRES, part) for part in parts)


def _literal(text: str) -> str:
    return quote_literal(Dialect.POSTGRES, text)


@dataclass(frozen=True)
class _Relation:
    """One row of q.RELATIONS."""

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
    replident: str  # d (default), n (nothing), f (full), i (an index's)
    replident_index: str | None
    clustered_index: str | None
    typed: bool  # CREATE TABLE … OF a type

    @property
    def key(self) -> str:
        return f"{'s' if self.relkind == 'S' else 'r'}{self.oid}"

    @property
    def label(self) -> str:  # as the file names it
        return f"{self.schema}.{self.name}"

    @property
    def sql(self) -> str:
        return qualified(Dialect.POSTGRES, self.schema, self.name)

    @property
    def only(self) -> str:
        return "" if self.relkind == "p" else "ONLY "  # a partitioned table's goes to its partitions (H5)


def _collation_ddl(qname: str, row: dict) -> str:
    """CREATE COLLATION from pg_collation's row: ICU, libc or builtin (P1)."""
    provider = row.get("collprovider")
    if provider == "i":
        locale = row.get("colllocale") or row.get("colliculocale") or row.get("collcollate")
        options = ["provider = icu", f"locale = {_literal(locale)}"]
        options += [f"rules = {_literal(row['collicurules'])}"] if row.get("collicurules") else []
    elif provider == "b":
        options = ["provider = builtin", f"locale = {_literal(row.get('colllocale') or '')}"]
    elif row.get("collcollate") == row.get("collctype"):
        options = ["provider = libc", f"locale = {_literal(row.get('collcollate') or '')}"]
    else:
        options = ["provider = libc", f"lc_collate = {_literal(row.get('collcollate') or '')}",
                   f"lc_ctype = {_literal(row.get('collctype') or '')}"]
    if not row.get("collisdeterministic", True):
        options.append("deterministic = false")
    return f"CREATE COLLATION {qname} ({', '.join(options)})"


def _aggregate_ddl(signature: str, row: tuple) -> str:
    """CREATE AGGREGATE from pg_aggregate (P1): only what is not the default."""
    (kind, transfn, transtype, transspace, finalfn, finalextra, finalmodify, combinefn, serialfn, deserialfn,
     initval, mtransfn, minvtransfn, mtranstype, mtransspace, mfinalfn, mfinalextra, mfinalmodify, minitval, sortop,
     parallel) = row
    options = [f"SFUNC = {transfn}", f"STYPE = {transtype}"]
    options += [f"SSPACE = {transspace}"] if transspace else []
    options += [f"FINALFUNC = {finalfn}"] if finalfn else []
    options += ["FINALFUNC_EXTRA"] if finalextra else []
    if finalmodify != ("r" if kind == "n" else "w"):  # the default: READ_ONLY, an ordered-set aggregate's READ_WRITE
        options.append(f"FINALFUNC_MODIFY = {_MODIFY[finalmodify]}")
    options += [f"COMBINEFUNC = {combinefn}"] if combinefn else []
    options += [f"SERIALFUNC = {serialfn}"] if serialfn else []
    options += [f"DESERIALFUNC = {deserialfn}"] if deserialfn else []
    options += [f"INITCOND = {_literal(initval)}"] if initval is not None else []
    if mtransfn:
        options += [f"MSFUNC = {mtransfn}", f"MINVFUNC = {minvtransfn}", f"MSTYPE = {mtranstype}"]
        options += [f"MSSPACE = {mtransspace}"] if mtransspace else []
        options += [f"MFINALFUNC = {mfinalfn}"] if mfinalfn else []
        options += ["MFINALFUNC_EXTRA"] if mfinalextra else []
        options += [f"MFINALFUNC_MODIFY = {_MODIFY[mfinalmodify]}"] if mfinalmodify != "r" else []
        options += [f"MINITCOND = {_literal(minitval)}"] if minitval is not None else []
    options += [f"SORTOP = OPERATOR({sortop[0]}.{sortop[1]})"] if sortop else []
    options += [f"PARALLEL = {_PARALLEL[parallel]}"] if parallel in _PARALLEL else []
    options += ["HYPOTHETICAL"] if kind == "h" else []
    return f"CREATE AGGREGATE {signature} (\n    " + ",\n    ".join(options) + "\n)"


class _Reader:
    """One catalog read; `ddl` False (a dump plan) reads which objects there are, not their definitions. Objects are
    keyed `r<oid>` (a relation), `s<oid>` (a sequence), `t<oid>` (a type), `f<oid>` (a routine), `k<oid>` (a
    collation), `g<oid>` (a trigger: what its WHEN uses), `x<oid>` (an object no dump writes: an operator, …)."""

    def __init__(self, conn: psycopg.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.scope, self.kinds, self.ddl = conn, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.version = conn.info.server_version
        self.left_out: dict[str, int] = {}  # counted for the header's Not included
        self.noted: list[str] = []  # said in it as they are
        self.warnings: list[str] = []
        self.schemas: dict[int, str] = {}
        self.relations: dict[int, _Relation] = {}
        self.types: dict[int, tuple[str, str, str]] = {}  # oid → (schema, name, typtype)
        self.routines: dict[int, tuple] = {}  # oid → q.ROUTINES row
        self.collations: dict[int, tuple[str, str, dict]] = {}  # oid → (schema, name, pg_collation row)
        self.others: dict[int, tuple[int, str]] = {}  # oid → (q.OTHER_KINDS index, identity)
        self.parents: dict[int, list[int]] = defaultdict(list)  # relation → the relations it inherits from
        self.partition_of: dict[int, int] = {}  # partition → its parent
        self.owners: dict[int, tuple[int, str]] = {}  # sequence → (table, column) it belongs to
        self.identity: set[int] = set()  # sequences of identity columns
        self.comments: dict[tuple[str, int], list[tuple[int, str | None, str]]] = defaultdict(list)
        self.create: dict[str, set[str]] = defaultdict(set)  # key → what it needs when it is created
        self.indexed: dict[str, set[str]] = defaultdict(set)  # relation key → what its indexes use
        self.uses: dict[str, set[str]] = defaultdict(set)  # key → everything it uses (created or later)
        self.parts: dict[tuple[str, str], set[str]] = defaultdict(set)  # (table key, a:/d:/k: part) → what it uses
        # key → part ('' for the object itself) → what it needs when created: a table's held-back parts drop out (P3)
        self.create_parts: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.triggers_by_oid: dict[int, str] = {}
        self.dumped: set[int] = set()  # the tables written
        self.references: dict[int, set[int]] = defaultdict(set)  # table → the dumped tables its foreign keys reference
        self.scope_oids: list[int] = []  # every object of the scope's schemas a dependency can name
        self.held: dict[int, Held] = {}  # table → what its CREATE TABLE holds back (P3)
        # Definitions, read in one query per kind before the objects are made (P5)
        self.table_parts: dict[int, TableParts] = {}
        self.type_parts: dict[int, TypeParts] = {}
        self.sequence_text: dict[int, str] = {}
        self.functions: dict[int, str] = {}
        self.aggregates: dict[int, tuple] = {}
        self.views: dict[int, str] = {}
        self.view_defaults: dict[int, list[tuple[str, str]]] = defaultdict(list)
        self.sequence_values: dict[int, tuple[int, bool]] = {}
        self.constraint_indexes: dict[int, list[tuple[int, str, str]]] = defaultdict(list)

    # -- reading helpers

    def _ddl(self, *statements: str) -> tuple[str, ...]:
        return statements if self.ddl else ()

    def rows(self, sql: str, **params: object) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(sql, params)]

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def comment(self, catalog: str, oid: int, target: str, columns_of: str | None = None) -> tuple[str, ...]:
        """COMMENT ON `target` (`TYPE public.mood`) when the object has one; `columns_of`: its columns' too (a view's,
        a composite type's)."""
        if not self.ddl:
            return ()
        found = []
        for subid, column, text in sorted(self.comments.get((catalog, oid), ()), key=lambda c: c[0]):
            if subid == 0:
                found.append(f"COMMENT ON {target} IS {_literal(text)}")
            elif columns_of is not None and column is not None:
                found.append(f"COMMENT ON COLUMN {columns_of}.{_q(column)} IS {_literal(text)}")
        return tuple(found)

    def column_comments(self, catalog: str, oid: int, of: str) -> tuple[str, ...]:
        """COMMENT ON COLUMN for the columns (a composite's attributes) of the object `of`, not the object's own."""
        return tuple(c for c in self.comment(catalog, oid, of, of) if c.startswith("COMMENT ON COLUMN "))

    def label_of(self, oid: int) -> str:
        if oid in self.relations:
            return self.relations[oid].label
        return self.rows("SELECT %(oid)s::regclass::text", oid=oid)[0][0]

    def describe(self, key: str) -> str:
        """`view app.v`, `function app.f(integer)`: an object the dump writes, as the header names it."""
        oid = int(key[1:])
        if key[0] == "r" and oid in self.relations:
            relation = self.relations[oid]
            return f"{_RELATION_WORDS.get(relation.relkind, 'relation')} {relation.label}"
        if key[0] == "t" and oid in self.types:
            schema, name, typtype = self.types[oid]
            return f"{_TYPE_WORDS.get(typtype, 'type')} {schema}.{name}"
        if key[0] == "f" and oid in self.routines:
            _, schema, name, kind, arguments = self.routines[oid]
            return f"{_ROUTINE_KIND[kind]} {schema}.{name}({arguments})"
        return f"trigger {self.triggers_by_oid.get(oid, oid)}" if key[0] == "g" else key

    # -- the read

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        schemas = self._schemas()
        self.schemas = dict(schemas)
        schema_oids = [oid for oid, _ in schemas]
        self.relations = {row[0]: _Relation(*row) for row in self.rows(q.RELATIONS, schemas=schema_oids)}
        self._family()
        picked = self._picked()
        tables = [r for r in picked if r.relkind in _TABLE_KINDS]
        self.dumped = {r.oid for r in tables}
        views = [r for r in picked if r.relkind in _VIEW_KINDS and (self.kinds.views or self.named)]
        if not self.named and not self.kinds.views:
            self.leave_out("views and materialized views (not chosen)", sum(r.relkind in _VIEW_KINDS for r in picked))
        self.leave_out("foreign tables", sum(r.relkind == "f" for r in picked))
        self.types = {row[0]: row[1:] for row in self.rows(q.TYPES, schemas=schema_oids)}
        self.routines = {row[0]: row for row in self.rows(q.ROUTINES, schemas=schema_oids)}
        self.collations = {oid: (schema, name, row) for oid, schema, name, row
                           in self.rows(q.COLLATIONS, schemas=schema_oids)}
        self.others = {oid: (kind, identity) for kind, oid, identity in self.rows(q.OTHERS, schemas=schema_oids)}
        self._dependencies(schema_oids)
        if self.ddl:
            for catalog, oid, subid, column, text in self.rows(q.COMMENTS):
                self.comments[(catalog, oid)].append((subid, column, text))
        written = [*tables, *views]
        triggers = self._triggers(written)
        self.triggers_by_oid = {row[0]: f"{row[2]} on {self.relations[row[1]].label}" for row in triggers}
        sequences = self._sequences(tables)
        needed = self._needed(written, triggers, sequences)
        collations = self._written_collations(needed)
        types = self._written_types(needed)
        routines = self._written_routines(needed)
        late = self._late(routines)
        reads_view = self._reads_view(routines)
        self._hold(tables, reads_view)
        # A routine that reads a relation and that a table's default, check or index (or a domain's check) needs is
        # made among the tables, after what it reads; the others, and those that read a view, wait for the views
        needs = self._closure({r.key for r in tables} | {f"t{oid}" for oid in types}, index=True)
        among_tables = [oid for oid in routines if oid in late and oid not in reads_view and f"f{oid}" in needs]
        extensions = [] if self.named else self.rows(q.EXTENSIONS, schemas=schema_oids)
        if not self.named:
            self.leave_out("extension objects", self.rows(q.EXTENSION_OBJECTS, schemas=schema_oids)[0][0])
        self._used_elsewhere(written, types, routines, [row[0] for row in triggers], [name for name, _ in extensions])
        self._prefetch(tables, views, types, sequences, routines)
        pre = [self._schema(oid, name) for oid, name in schemas]
        pre += [DumpObject("extension", name,
                           self._ddl(f"CREATE EXTENSION IF NOT EXISTS {_q(name)} WITH SCHEMA {_q(schema)}"))
                for name, schema in extensions]
        pre += self._pre(collations, types, sequences, [oid for oid in routines if oid not in late], tables,
                         among_tables)
        after_data = [oid for oid in routines if oid in late and oid not in among_tables]
        post = [*self._sequence_values(sequences), *self._indexes(tables), *self._foreign_keys(tables),
                *self._not_valid(tables), *self._domain_not_valid(types), *self._table_settings(tables),
                *self._views_and_routines(views, after_data), *self._held_back(tables),
                *self._indexes([r for r in views if r.relkind == "m"]), *(self._trigger(row) for row in triggers),
                *self._trigger_states(tables), *self._rules(written), *self._statistics(written),
                *self._row_security(tables)]
        self._uses_of_others(written, types, routines, triggers)
        self._others(schema_oids)
        return pre, self._data(tables), post

    def _schemas(self) -> list[tuple[int, str]]:
        if self.scope.kind == "database":
            return self.rows(q.SCHEMAS)
        found = self.rows(q.SCHEMA, name=self.scope.schema_name)
        if not found:
            raise NotFound(f"Schema {self.scope.schema_name} not found")
        return found

    def _family(self) -> None:
        for child, parent, is_partition in self.rows(q.INHERITS, relations=list(self.relations)):
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
        """A referenced object as the object a dump writes: an array type as its element, a multirange as its range,
        a row type as its relation; None when it is not one of the scope's."""
        if ref_class == "f":
            return f"f{oid}" if oid in self.routines else None
        if ref_class == "c":
            return self.relations[oid].key if oid in self.relations else None
        if ref_class == "C":
            return f"k{oid}" if oid in self.collations else None
        if ref_class == "x":
            return f"x{oid}" if oid in self.others else None
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
        sql = q.TYPE_LINKS.format(multirange=q.MULTIRANGE_LINK if self.version >= 140000 else "")
        links = {oid: (element, relation) for oid, element, relation in self.rows(sql, schemas=schema_oids)}
        self.scope_oids = [*self.relations, *self.routines, *self.collations, *self.others, *links]  # links: every type
        if not self.scope_oids:
            return
        for owner_kind, owner, phase, ref_class, ref, part in self.rows(q.DEPENDS, refs=self.scope_oids):
            owner_key = self.relations[owner].key if owner_kind == "r" and owner in self.relations else \
                f"{owner_kind}{owner}"
            target = self._key_of(ref_class, ref, links)
            if target is None or target == owner_key:
                continue
            self.uses[owner_key].add(target)
            if part is not None:
                self.parts[(owner_key, part)].add(target)
            if phase == "create":
                self.create[owner_key].add(target)
                self.create_parts[owner_key][part or ""].add(target)
            elif phase == "index":
                self.indexed[owner_key].add(target)

    def _create_refs(self, key: str) -> set[str]:
        """What `key` needs when it is created: a table's, without what its CREATE TABLE holds back (P3)."""
        held = self.held.get(int(key[1:])) if key[0] == "r" else None
        if held is None:
            return self.create.get(key, set())
        skip = {*(f"a:{c}" for c in held.columns), *(f"d:{c}" for c in held.defaults),
                *(f"k:{c}" for c in held.constraints)}
        return {ref for part, refs in self.create_parts.get(key, {}).items() if part not in skip for ref in refs}

    def _closure(self, roots: set[str], index: bool = False, every: bool = False) -> set[str]:
        """The collations, types, routines and sequences `roots` use, however deep (a domain's check, a signature, a
        SQL-standard body), never a relation: what they need when created (`index`: and their indexes; never through
        what a table holds back), or (`every`) at all."""
        seen: set[str] = set()
        todo = list(roots)
        while todo:
            key = todo.pop()
            found = set(self.uses.get(key, ())) if every else set(self._create_refs(key))
            if index:
                found |= self.indexed.get(key, set())
            for ref in found:
                if ref[0] not in "rx" and ref not in seen:
                    seen.add(ref)
                    todo.append(ref)
        return seen

    def _needed(self, written: list[_Relation], triggers: list[tuple], sequences: list[int]) -> set[str]:
        """What the dump writes, and what that uses: its relations, their triggers (their functions and what their
        WHEN calls, P4), its sequences, and in a schema or database scope the chosen kinds."""
        roots = {r.key for r in written} | {f"f{row[3]}" for row in triggers} | {f"g{row[0]}" for row in triggers}
        roots |= {f"s{oid}" for oid in sequences}
        if not self.named:
            if self.kinds.types:
                roots |= {f"t{oid}" for oid in self.types} | {f"k{oid}" for oid in self.collations}
            if self.kinds.routines:
                roots |= {f"f{oid}" for oid in self.routines}
        return roots | self._closure(roots, every=True)

    def _written_collations(self, needed: set[str]) -> list[int]:
        found = [oid for oid in self.collations if f"k{oid}" in needed]
        if not self.named and not self.kinds.types:
            self.leave_out("collations (not chosen)", len(self.collations) - len(found))
        return found

    def _written_types(self, needed: set[str]) -> list[int]:
        """The types the dump writes, in q.TYPES order; base types are counted instead."""
        found, not_chosen = [], 0
        for oid, (_, _, typtype) in self.types.items():
            if f"t{oid}" not in needed:
                not_chosen += 1
            elif typtype == "b":
                self.leave_out("base types")
            else:
                found.append(oid)
        if not self.named and not self.kinds.types:
            self.leave_out("types (not chosen)", not_chosen)
        return found

    def _written_routines(self, needed: set[str]) -> list[int]:
        found = [oid for oid in self.routines if f"f{oid}" in needed]
        if not self.named and not self.kinds.routines:
            self.leave_out("functions and procedures (not chosen)", len(self.routines) - len(found))
        return found

    def _spread(self, routines: list[int], seeds: set[int]) -> set[int]:
        """`seeds` and every routine of `routines` that calls one of them when it is created, however deep."""
        callers: dict[str, list[int]] = defaultdict(list)
        for oid in routines:
            for ref in self.create.get(f"f{oid}", ()):
                callers[ref].append(oid)
        found, todo = set(seeds), list(seeds)
        while todo:
            for caller in callers[f"f{todo.pop()}"]:
                if caller not in found:
                    found.add(caller)
                    todo.append(caller)
        return found

    def _late(self, routines: list[int]) -> set[int]:
        """The routines that read a relation when they are created (a row type in their signature, P9; a SQL-standard
        body over a table, H4), or call one that does: they wait for it."""
        return self._spread(routines, {oid for oid in routines
                                       if any(ref[0] == "r" for ref in self.create.get(f"f{oid}", ()))})

    def _reads_view(self, routines: list[int]) -> set[int]:
        """The routines that read a view or a materialized view (or call one that does): they come after the views,
        and so do the table defaults and checks that call them (P3)."""
        def is_view(ref: str) -> bool:
            return ref[0] == "r" and self.relations[int(ref[1:])].relkind in _VIEW_KINDS
        return self._spread(routines, {oid for oid in routines if any(map(is_view, self.create.get(f"f{oid}", ())))})

    def _hold(self, tables: list[_Relation], reads_view: set[int]) -> None:
        """What each table's CREATE TABLE holds back (P3): a default or a check on a routine that reads a view (added
        after the views), a column typed as a view's row type (not written: counted and said)."""
        later = {f"f{oid}" for oid in reads_view}
        parts_of: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
        for (key, part), refs in self.parts.items():
            parts_of[key].append((part, refs))
        for r in tables:
            columns, defaults, constraints = set(), set(), set()
            for part, refs in parts_of.get(r.key, ()):
                kind, name = part.split(":", 1)
                if kind == "a" and any(ref[0] == "r" and self.relations[int(ref[1:])].relkind in _VIEW_KINDS
                                       for ref in refs):
                    columns.add(name)
                elif kind in "dk" and (refs & later or self._closure(refs) & later):
                    (defaults if kind == "d" else constraints).add(name)
            if columns or defaults or constraints:
                self.held[r.oid] = Held(frozenset(columns), frozenset(defaults), frozenset(constraints))
            for name in sorted(columns):
                self.leave_out("columns typed as a view's row type")
                self.warnings.append(f"Column {r.label}.{name} is typed as a view's row type: it is not in this dump "
                                     "(the file creates its tables before its views)")

    def _used_elsewhere(self, written: list[_Relation], types: list[int], routines: list[int], triggers: list[int],
                        extensions: list[str]) -> None:
        """What the dump's objects use and it does not create, counted: an extension's types and functions (L6), and
        the types, functions, sequences, tables and views of schemas not in the dump (P4). The file needs them where
        it is run."""
        if not (written or types or routines):
            return
        found: dict[tuple[bool, str], tuple[int, list[str]]] = {}
        for extension, what, count in self.rows(
                q.USED_ELSEWHERE, relations=[r.oid for r in written], types=types, routines=routines,
                triggers=triggers, scope=self.scope_oids, extensions=extensions):
            total, names = found.get((extension is None, what), (0, []))
            found[(extension is None, what)] = (total + count, [*names, *([extension] if extension else [])])
        for (elsewhere, what), (count, names) in found.items():
            self.leave_out(f"{what} from schemas not in the dump" if elsewhere
                           else f"extension {what} (from {', '.join(names)})", count)

    def _uses_of_others(self, written: list[_Relation], types: list[int], routines: list[int],
                        triggers: list[tuple]) -> None:
        """P1: an operator, operator class or text search object the dump does not write, named with what uses it
        ("view app.v uses operator app.===(integer,integer)"): the file needs it where it is run."""
        users = [*(r.key for r in written), *(f"t{oid}" for oid in types), *(f"f{oid}" for oid in routines),
                 *(f"g{row[0]}" for row in triggers)]
        named = [f"{self.describe(user)} uses {q.OTHER_KINDS[self.others[int(ref[1:])][0]][1]} "
                 f"{self.others[int(ref[1:])][1]}: not included"
                 for user in users for ref in sorted(self.uses.get(user, ())) if ref[0] == "x"]
        self.noted += named[:NAMED_USES]
        if len(named) > NAMED_USES:
            self.noted.append(f"{len(named) - NAMED_USES:,} more uses of operators, operator classes or text search "
                              "objects that are not included")

    # -- the definitions, one query per kind (P5)

    def _prefetch(self, tables: list[_Relation], views: list[_Relation], types: list[int], sequences: list[int],
                  routines: list[int]) -> None:
        if not self.ddl:
            return
        self.table_parts = table_parts_of(self.conn, [r.oid for r in tables], pretty=False)
        self.type_parts = type_parts_of(self.conn, types, pretty=False)
        self.sequence_text = sequence_ddl_of(self.conn, sequences)
        plain = [oid for oid in routines if self.routines[oid][3] != "a"]
        aggregates = [oid for oid in routines if self.routines[oid][3] == "a"]
        self.functions = dict(self.rows(q.FUNCTION_DEFINITIONS, oids=plain)) if plain else {}
        self.aggregates = {row[0]: row[1:] for row in self.rows(q.AGGREGATES, oids=aggregates)} if aggregates else {}
        if views:
            self.views = dict(self.rows(q.VIEW_DEFINITIONS, oids=[r.oid for r in views]))
            for oid, column, default in self.rows(q.VIEW_DEFAULTS, views=[r.oid for r in views]):
                self.view_defaults[oid].append((column, default))
        if tables:
            for table, index, schema, name in self.rows(q.CONSTRAINT_INDEXES, tables=[r.oid for r in tables]):
                self.constraint_indexes[table].append((index, schema, name))
        values = sorted({*sequences, *self.identity})
        for start in range(0, len(values), _SEQUENCE_VALUES_CHUNK):
            sql = " UNION ALL ".join(f"SELECT {oid}::oid, last_value, is_called FROM {self.relations[oid].sql}"
                                     for oid in values[start:start + _SEQUENCE_VALUES_CHUNK])
            self.sequence_values.update({oid: (last, called) for oid, last, called in self.rows(sql)})

    # -- before the data

    def _schema(self, oid: int, name: str) -> DumpObject:
        comments = [c for c in self.comment("pg_namespace", oid, f"SCHEMA {_q(name)}")
                    if (name, self._comment_text("pg_namespace", oid)) != _PUBLIC_COMMENT]
        return DumpObject("schema", name, self._ddl(f"CREATE SCHEMA IF NOT EXISTS {_q(name)}", *comments))

    def _comment_text(self, catalog: str, oid: int) -> str | None:
        return next((text for subid, _, text in self.comments.get((catalog, oid), ()) if subid == 0), None)

    def _pre(self, collations: list[int], types: list[int], sequences: list[int], routines: list[int],
             tables: list[_Relation], among_tables: list[int]) -> list[DumpObject]:
        """Collations, types, sequences, routines and tables in one dependency order: a partition after its parent, a
        type or a table after the routine its check or default calls, a routine that reads a relation after it (merged
        with the tables by creation order)."""
        among = sorted([*((r.oid, r.key) for r in tables), *((oid, f"f{oid}") for oid in among_tables)])
        keys = [*(f"k{oid}" for oid in collations), *(f"t{oid}" for oid in types), *(f"s{oid}" for oid in sequences),
                *(f"f{oid}" for oid in routines), *(key for _, key in among)]
        ordering = Ordering({key: set(self._create_refs(key)) for key in keys})
        for r in tables:
            if r.oid in self.partition_of:
                ordering.depends.setdefault(r.key, set()).add(f"r{self.partition_of[r.oid]}")
        made = {"k": self._collation, "t": self._type, "s": self._sequence,
                "f": lambda oid: self._routine(self.routines[oid]), "r": lambda oid: self._table(self.relations[oid])}
        return [made[key[0]](int(key[1:])) for key in ordering.order(keys)]

    def _collation(self, oid: int) -> DumpObject:
        schema, name, row = self.collations[oid]
        qname = _q(schema, name)
        return DumpObject("collation", f"{schema}.{name}", self._ddl(
            _collation_ddl(qname, row), *self.comment("pg_collation", oid, f"COLLATION {qname}")),
            f"DROP COLLATION IF EXISTS {qname}")

    def _type(self, oid: int) -> DumpObject:
        schema, name, typtype = self.types[oid]
        qname = _q(schema, name)
        word = "DOMAIN" if typtype == "d" else "TYPE"
        statements: tuple[str, ...] = ()
        if self.ddl:
            parts = self.type_parts[oid]
            constraints = tuple(c for constraint, cname in parts.constraints
                                for c in self.comment("pg_constraint", constraint,
                                                      f"CONSTRAINT {_q(cname)} ON DOMAIN {qname}"))
            attributes = self.column_comments("pg_class", parts.relation, qname) if parts.relation else ()
            statements = (parts.create, *self.comment("pg_type", oid, f"{word} {qname}"), *constraints, *attributes)
        return DumpObject(word.lower(), f"{schema}.{name}", statements, f"DROP {word} IF EXISTS {qname}")

    def _sequences(self, tables: list[_Relation]) -> list[int]:
        """Standalone sequences (unless left out), and those a dumped table's serial owns or its defaults use; never an
        identity's, which the table's DDL makes (its value is set after the data all the same)."""
        table_oids = [r.oid for r in tables]
        owned = self.rows(q.OWNED_SEQUENCES, tables=table_oids) if table_oids else []
        self.owners = {sequence: (table, column) for sequence, _, table, column in owned}
        self.identity = {sequence for sequence, deptype, _, _ in owned if deptype == "i"}
        used = {sequence for sequence, deptype, _, _ in owned if deptype == "a"}
        used |= {int(ref[1:]) for r in tables for ref in self.uses.get(r.key, ()) if ref[0] == "s"}
        used &= set(self.relations)  # one of another schema is counted with what the dump uses elsewhere
        self.identity &= set(self.relations)
        if self.named:
            return sorted(used - self.identity)
        sequences = [r.oid for r in self.relations.values() if r.relkind == "S"]
        anyone_owns = {oid for (oid,) in self.rows(q.ANY_OWNED, oids=sequences)} if sequences else set()
        standalone = set(sequences) - anyone_owns
        self.leave_out("sequences owned by tables not in the dump", len(anyone_owns - used - self.identity))
        if not self.kinds.sequences:
            self.leave_out("sequences (not chosen)", len(standalone - used))
            return sorted(used - self.identity)
        return sorted((used | standalone) - self.identity)

    def _sequence(self, oid: int) -> DumpObject:
        relation = self.relations[oid]
        statements = (self.sequence_text[oid], *self.comment("pg_class", oid, f"SEQUENCE {relation.sql}")) \
            if self.ddl else ()
        return DumpObject("sequence", relation.label, statements, f"DROP SEQUENCE IF EXISTS {relation.sql}")

    def _routine(self, row: tuple) -> DumpObject:
        oid, schema, name, kind, arguments = row
        word = _ROUTINE_WORD[kind]
        signature = f"{_q(schema, name)}({arguments})"
        statements: tuple[str, ...] = ()
        if self.ddl:
            definition = _aggregate_ddl(signature, self.aggregates[oid]) if kind == "a" else \
                self.functions[oid].rstrip().rstrip(";")
            statements = (definition, *self.comment("pg_proc", oid, f"{word} {signature}"))
        return DumpObject(_ROUTINE_KIND[kind], f"{schema}.{name}({arguments})", statements,
                          f"DROP {word} IF EXISTS {signature}")

    def _table(self, relation: _Relation) -> DumpObject:
        statements: tuple[str, ...] = ()
        held = self.held.get(relation.oid, NOTHING_HELD)
        if self.ddl:
            parts = self.table_parts[relation.oid]
            constraint_comments = [comment for c in parts.constraints if c.validated and c.kind != "f"
                                   and c.name not in held.constraints
                                   for comment in self.comment("pg_constraint", c.oid,
                                                               f"CONSTRAINT {_q(c.name)} ON {relation.sql}")]
            index_comments = [comment for index, schema, name in self.constraint_indexes.get(relation.oid, ())
                              for comment in self.comment("pg_class", index, f"INDEX {_q(schema, name)}")]
            statements = (parts.create(foreign_keys=False, not_valid=False, held=held),
                          *parts.partition_constraints(not_valid=False, held=held), *parts.column_settings(held),
                          *parts.comments(held), *constraint_comments, *index_comments)
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
                last_value, called = self.sequence_values[oid]
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
        for oid, _, schema, name, definition in self.rows(q.INDEXES, relations=[r.oid for r in relations],
                                                          ddl=self.ddl):
            statements = (_ON_ONLY.sub(r"\1", definition, count=1),
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
                q.FOREIGN_KEYS, tables=sorted(self.dumped), ddl=self.ddl):
            relation = self.relations[table]
            if referenced not in self.dumped:
                target = self.relations[referenced].label if referenced in self.relations else referenced_name
                self.warnings.append(f"Foreign key {name} of {relation.label} is left out: it references {target}, "
                                     "which is not in this dump")
                self.leave_out("foreign keys to tables not in the dump")
                continue
            if self._root(referenced) != self._root(table):  # M9: what a data-only dump writes first
                self.references[self._root(table)].add(self._root(referenced))
            statements = (f"ALTER TABLE {relation.only}{relation.sql} ADD CONSTRAINT {_q(name)} {definition}",
                          *self.comment("pg_constraint", oid, f"CONSTRAINT {_q(name)} ON {relation.sql}")) \
                if self.ddl else ()
            found.append(DumpObject(
                "foreign key", f"{relation.label}.{name}", statements,
                f"ALTER TABLE IF EXISTS {relation.only}{relation.sql} DROP CONSTRAINT IF EXISTS {_q(name)}"))
        return found

    def _not_valid(self, tables: list[_Relation]) -> list[DumpObject]:
        """NOT VALID checks, after the data: inline, CREATE TABLE would validate them against rows that may break
        them."""
        if not tables:
            return []
        found = []
        for oid, table, name, definition in self.rows(q.NOT_VALID, tables=[r.oid for r in tables], ddl=self.ddl):
            relation = self.relations[table]
            statements = (f"ALTER TABLE {relation.only}{relation.sql} ADD CONSTRAINT {_q(name)} {definition}",
                          *self.comment("pg_constraint", oid, f"CONSTRAINT {_q(name)} ON {relation.sql}")) \
                if self.ddl else ()
            found.append(DumpObject("check constraint", f"{relation.label}.{name}", statements))
        return found

    def _domain_not_valid(self, types: list[int]) -> list[DumpObject]:
        """A domain's NOT VALID checks, after the data (P2: CREATE DOMAIN cannot say NOT VALID)."""
        domains = [oid for oid in types if self.types[oid][2] == "d"]
        if not domains:
            return []
        found = []
        statements_of = {constraint: statement for oid in domains for constraint, statement
                         in self.type_parts[oid].not_valid} if self.ddl else {}
        for constraint, domain, name in self.rows(q.DOMAIN_NOT_VALID, types=domains):
            schema, type_name, _ = self.types[domain]
            statements = (statements_of[constraint], *self.comment(
                "pg_constraint", constraint, f"CONSTRAINT {_q(name)} ON DOMAIN {_q(schema, type_name)}")) \
                if self.ddl else ()
            found.append(DumpObject("check constraint", f"{schema}.{type_name}.{name}", statements))
        return found

    def _table_settings(self, tables: list[_Relation]) -> list[DumpObject]:
        """REPLICA IDENTITY and CLUSTER ON (P4), once the indexes they name are there."""
        found = []
        for r in tables:
            statements = []
            if r.replident == "i" and r.replident_index:
                statements.append(f"ALTER TABLE {r.sql} REPLICA IDENTITY USING INDEX {_q(r.replident_index)}")
            elif r.replident in ("f", "n"):
                statements.append(f"ALTER TABLE {r.sql} REPLICA IDENTITY {'FULL' if r.replident == 'f' else 'NOTHING'}")
            if r.clustered_index:
                statements.append(f"ALTER TABLE {r.sql} CLUSTER ON {_q(r.clustered_index)}")
            if statements:
                found.append(DumpObject("table settings", r.label, self._ddl(*statements)))
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
            options = [o for o in relation.options or () if not o.startswith("check_option=")]
            check = next((o.partition("=")[2] for o in relation.options or () if o.startswith("check_option=")), None)
            storage = f" WITH ({', '.join(storage_options(options))})" if options else ""
            statement = f"CREATE {head} {relation.sql}{storage} AS\n{self.views[relation.oid].rstrip().rstrip(';')}"
            if check:  # H6: CHECK OPTION is a view option, not part of the view's definition
                statement += f"\nWITH {check.upper()} CHECK OPTION"
            if materialized and not relation.populated:
                statement += "\nWITH NO DATA"
            defaults = [f"ALTER VIEW {relation.sql} ALTER COLUMN {_q(column)} SET DEFAULT {default}"  # P4
                        for column, default in self.view_defaults.get(relation.oid, ())]
            statements = (statement, *defaults,
                          *self.comment("pg_class", relation.oid, f"{head} {relation.sql}", relation.sql))
        return DumpObject(head.lower(), relation.label, statements, f"DROP {head} IF EXISTS {relation.sql}")

    def _held_back(self, tables: list[_Relation]) -> list[DumpObject]:
        """The defaults and checks a table's CREATE TABLE held back (P3), once the views their routines read are
        there; their DROPs come before those of the routines (H7)."""
        found = []
        for r in tables:
            held = self.held.get(r.oid)
            if held is None:
                continue
            parts = self.table_parts.get(r.oid)
            for name in sorted(held.defaults):
                default = next((c.default for c in parts.columns if c.name == name), None) if parts else None
                found.append(DumpObject("column default", f"{r.label}.{name}", self._ddl(
                    f"ALTER TABLE {r.only}{r.sql} ALTER COLUMN {_q(name)} SET DEFAULT {default}"),
                    f"ALTER TABLE IF EXISTS {r.only}{r.sql} ALTER COLUMN {_q(name)} DROP DEFAULT"))
            for name in sorted(held.constraints):
                constraint = next((c for c in parts.constraints if c.name == name), None) if parts else None
                found.append(DumpObject("check constraint", f"{r.label}.{name}", self._ddl(
                    f"ALTER TABLE {r.only}{r.sql} ADD CONSTRAINT {_q(name)} {constraint.definition if constraint else ''}",
                    *(self.comment("pg_constraint", constraint.oid, f"CONSTRAINT {_q(name)} ON {r.sql}")
                      if constraint else ())),
                    f"ALTER TABLE IF EXISTS {r.only}{r.sql} DROP CONSTRAINT IF EXISTS {_q(name)}"))
        return found

    def _triggers(self, relations: list[_Relation]) -> list[tuple]:
        """The triggers on the dump's tables and views (a view's INSTEAD OF, H6), unless left out."""
        if not relations:
            return []
        sql = q.TRIGGERS.format(parent=" AND t.tgparentid = 0" if self.version >= 130000 else "")
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

    def _trigger_states(self, tables: list[_Relation]) -> list[DumpObject]:
        """A partition's copy of a trigger in a state of its own (P4), once its parent's trigger made it."""
        if not tables or not self.kinds.triggers or self.version < 130000:
            return []
        return [DumpObject("trigger state", f"{self.relations[table].label}.{name}", self._ddl(
            f"ALTER TABLE {self.relations[table].sql} {_STATE.get(enabled, 'ENABLE')} TRIGGER {_q(name)}"))
            for table, name, enabled in self.rows(q.TRIGGER_STATES, tables=[r.oid for r in tables])]

    def _rules(self, relations: list[_Relation]) -> list[DumpObject]:
        if not relations:
            return []
        found = []
        for oid, table, name, enabled, definition in self.rows(q.RULES, relations=[r.oid for r in relations],
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
        sql = q.STATISTICS.format(target=q.STATISTICS_TARGET if self.version >= 130000 else "NULL")
        found = []
        for oid, _, schema, name, definition, target in self.rows(sql, relations=[r.oid for r in relations],
                                                                  ddl=self.ddl):
            qname = _q(schema, name)
            statements = (definition, *([f"ALTER STATISTICS {qname} SET STATISTICS {target}"] if target is not None
                                        else []),
                          *self.comment("pg_statistic_ext", oid, f"STATISTICS {qname}")) if self.ddl else ()
            found.append(DumpObject("statistics", f"{schema}.{name}", statements, f"DROP STATISTICS IF EXISTS {qname}"))
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
                q.POLICIES, tables=[r.oid for r in tables]):
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
        """What the scope holds that no dump writes: inheritance (S19), typed tables, partitions out of reach, the
        schemas' operators and text search objects, and a database's own objects (H6, P4)."""
        children = [oid for oid in self.parents if oid not in self.partition_of]
        self.leave_out("inheritance child tables not in the dump (their rows are left out: FROM ONLY)",
                       sum(1 for oid in children if oid not in self.dumped and set(self.parents[oid]) & self.dumped))
        if inheriting := sum(1 for oid in children if oid in self.dumped):  # S19: no INHERITS is written
            self.noted.append(f"inheritance ({inheriting:,} child tables come back as tables of their own)")
        if typed := sum(1 for oid in self.dumped if self.relations[oid].typed):  # P4: no OF is written
            self.noted.append(f"typed tables ({typed:,} come back as tables of their own, not OF their type)")
        cut_off = sorted({self.relations[parent].label for oid, parent in self.partition_of.items()
                          if parent in self.dumped and oid not in self.dumped})
        if cut_off:
            self.warnings.append(f"Partitions of {', '.join(cut_off)} are not in this dump (another schema, or a "
                                 "foreign table): their rows are, and need those partitions where the file is run")
        if self.named:
            return
        for kind, (plural, *_) in enumerate(q.OTHER_KINDS):
            self.leave_out(plural, sum(1 for other_kind, _ in self.others.values() if other_kind == kind))
        if self.scope.kind == "database":
            for what, count in zip(q.DATABASE_OBJECTS, self.rows(q.DATABASE_COUNTS)[0]):
                self.leave_out(what, count)

    def _data(self, tables: list[_Relation]) -> list[DumpTable]:
        """The tables whose rows are written, in creation order: a partition's rows come with its parent's (a
        partitioned table's estimates are its partitions'). A column the table's DDL held back is not copied (P3)."""
        roots = [r for r in tables if self._root(r.oid) == r.oid]
        columns = pg_columns.read_columns_of(self.conn, [r.oid for r in roots])
        found = []
        for r in roots:
            rows, size = r.rows, r.size
            if r.relkind == "p":
                parts = [p for p in tables if p.relkind == "r" and self._root(p.oid) == r.oid]
                known = [p.rows for p in parts if p.rows is not None]
                rows, size = (sum(known) if known else None), sum(p.size or 0 for p in parts)
            held = self.held.get(r.oid, NOTHING_HELD).columns
            found.append(DumpTable(
                schema=r.schema, table=r.name, name=r.label, columns=tuple(_held_column(c, held) for c in columns[r.oid]),
                rows_estimate=rows, bytes_estimate=size, only=r.relkind == "r",
                references=tuple(self.relations[oid].label for oid in sorted(self.references.get(r.oid, ())))))
        return found


def _held_column(column: TableColumn, held: frozenset[str]) -> TableColumn:
    return replace(column, copied=False, not_copied_reason=VIEW_ROW_TYPE) if column.name in held else column


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
