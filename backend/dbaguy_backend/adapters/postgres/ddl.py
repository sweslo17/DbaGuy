"""A PostgreSQL object's DDL. The object tree shows one (`object_detail`); a dump reads thousands at once, so the
`*_of` readers take many oids and send a fixed number of queries whatever their number (pre-flight P5). `pretty`
False is the dump's: pg_get_* without their pretty mode, whose dropped parentheses and casts may not read back the
same (P6)."""

import re
from dataclasses import dataclass, field

import psycopg

from ...domain.errors import NotFound, ValidationFailed
from ...domain.metadata import NodeKind, ObjectDetail, PathSegment
from ..sql_text import qualified_pg, quote_ident_pg, quote_literal_pg
from .introspection import PgColumn, columns_of, relation_oid, table_columns

_TABLES_SQL = """
SELECT c.oid, n.nspname::text, c.relname::text, c.relkind::text, c.relispartition, pg_get_partkeydef(c.oid),
       obj_description(c.oid, 'pg_class'), h.inhparent, h.inhparent::regclass::text, pg_get_expr(c.relpartbound, c.oid),
       c.relpersistence::text, c.reloptions, (SELECT t.reloptions FROM pg_class t WHERE t.oid = c.reltoastrelid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_inherits h ON h.inhrelid = c.oid AND c.relispartition
WHERE c.oid = ANY(%(oids)s)
"""
# A partition's own constraints only: not those PARTITION OF brings from its parent (attached keys, inherited checks),
# nor its foreign keys (a dump adds those after the data)
_CONSTRAINTS_SQL = """
SELECT k.conrelid, k.conname::text, pg_get_constraintdef(k.oid, %(pretty)s), k.contype::text, k.convalidated, k.oid
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE k.conrelid = ANY(%(oids)s) AND k.contype IN ('p', 'u', 'c', 'f', 'x')
  AND (NOT c.relispartition OR (k.contype <> 'f' AND k.conparentid = 0 AND k.conislocal AND k.coninhcount = 0))
ORDER BY k.conrelid, CASE k.contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 WHEN 'f' THEN 3 ELSE 4 END, k.conname
"""
_EXTRA_INDEXES_SQL = """
SELECT i.indrelid, pg_get_indexdef(i.indexrelid) FROM pg_index i
WHERE i.indrelid = ANY(%(oids)s)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid)
ORDER BY 1, 2
"""
# An identity column's sequence (deptype i): its options, which CREATE TABLE writes when they are not the defaults
_IDENTITY_SQL = """
SELECT a.attrelid, a.attname::text, s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcache, s.seqcycle,
       sn.nspname::text, sc.relname::text, tn.nspname::text, tc.relname::text
FROM pg_attribute a
JOIN pg_class tc ON tc.oid = a.attrelid JOIN pg_namespace tn ON tn.oid = tc.relnamespace
JOIN pg_depend d ON d.refclassid = 'pg_class'::regclass AND d.refobjid = a.attrelid AND d.refobjsubid = a.attnum
                AND d.classid = 'pg_class'::regclass AND d.deptype = 'i'
JOIN pg_sequence s ON s.seqrelid = d.objid
JOIN pg_class sc ON sc.oid = s.seqrelid JOIN pg_namespace sn ON sn.oid = sc.relnamespace
WHERE a.attrelid = ANY(%(oids)s) AND a.attidentity <> ''
"""
_SEQUENCES_SQL = """
SELECT s.seqrelid, n.nspname::text, c.relname::text, format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement,
       s.seqmin, s.seqmax, s.seqcache, s.seqcycle, c.relpersistence::text
FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE s.seqrelid = ANY(%(oids)s)
"""
# A collation as [schema, name] when it is not the one the base type (or subtype, attribute type) has itself
_COLLATION = ("(SELECT CASE WHEN {collation} <> 0 AND {collation} <> {own} THEN ARRAY[cn.nspname::text, "
              "co.collname::text] END FROM pg_collation co JOIN pg_namespace cn ON cn.oid = co.collnamespace "
              "WHERE co.oid = {collation})")
_TYPES_SQL = f"""
SELECT t.oid, n.nspname::text, t.typname::text, t.typtype::text, format_type(t.typbasetype, t.typtypmod), t.typnotnull,
       pg_get_expr(t.typdefaultbin, 0), t.typrelid,
       {_COLLATION.format(collation="t.typcollation",
                          own="(SELECT b.typcollation FROM pg_type b WHERE b.oid = t.typbasetype)")}
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.oid = ANY(%(oids)s)
"""
_ENUM_LABELS_SQL = "SELECT enumtypid, enumlabel FROM pg_enum WHERE enumtypid = ANY(%(oids)s) ORDER BY 1, enumsortorder"
# A domain's checks; its NOT NULL is typnotnull's (PostgreSQL 17 also lists it as a constraint: written twice, CREATE
# DOMAIN fails)
_DOMAIN_CONSTRAINTS_SQL = """
SELECT contypid, oid, conname::text, pg_get_constraintdef(oid, %(pretty)s), convalidated FROM pg_constraint
WHERE contypid = ANY(%(oids)s) AND contype = 'c' ORDER BY contypid, conname
"""
_ATTRIBUTES_SQL = f"""
SELECT t.oid, a.attname::text, format_type(a.atttypid, a.atttypmod),
       {_COLLATION.format(collation="a.attcollation", own="(SELECT x.typcollation FROM pg_type x WHERE x.oid = a.atttypid)")}
FROM pg_type t JOIN pg_attribute a ON a.attrelid = t.typrelid
WHERE t.oid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY t.oid, a.attnum
"""
_RANGES_SQL = f"""
SELECT r.rngtypid, format_type(r.rngsubtype, NULL),
       CASE WHEN NOT o.opcdefault THEN ARRAY[on_.nspname::text, o.opcname::text] END,
       {_COLLATION.format(collation="r.rngcollation", own="(SELECT x.typcollation FROM pg_type x WHERE x.oid = r.rngsubtype)")},
       CASE WHEN r.rngcanonical <> 0 THEN r.rngcanonical::regproc::text END,
       CASE WHEN r.rngsubdiff <> 0 THEN r.rngsubdiff::regproc::text END, {{multirange}}
FROM pg_range r JOIN pg_opclass o ON o.oid = r.rngsubopc JOIN pg_namespace on_ ON on_.oid = o.opcnamespace
WHERE r.rngtypid = ANY(%(oids)s)
"""
_MULTIRANGE_NAME = "r.rngmultitypid::regtype::text"
_INTEGER_RANGES = {"smallint": (-(2**15), 2**15 - 1), "integer": (-(2**31), 2**31 - 1),
                   "bigint": (-(2**63), 2**63 - 1)}
_PLAIN_OPTION = re.compile(r"[A-Za-z0-9_.+-]+", re.ASCII)  # a storage parameter's value that needs no quotes
_NAME_BYTES = 63  # NAMEDATALEN - 1: a longer default identity sequence name is shortened by the server


def _one(conn: psycopg.Connection, sql: str, params: dict, what: str):
    row = conn.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        raise NotFound(f"{what} not found")
    return row


def _column_def(col: PgColumn, identity: str = "", with_default: bool = True) -> str:
    parts = [quote_ident_pg(col.name), col.type]
    if col.collation:
        parts.append(f"COLLATE {col.collation}")
    if col.generated in ("s", "v"):
        parts.append(f"GENERATED ALWAYS AS ({col.default}) {'STORED' if col.generated == 's' else 'VIRTUAL'}")
    elif col.identity == "a":
        parts.append("GENERATED ALWAYS AS IDENTITY" + identity)
    elif col.identity == "d":
        parts.append("GENERATED BY DEFAULT AS IDENTITY" + identity)
    elif col.default is not None and with_default:
        parts.append(f"DEFAULT {col.default}")
    if col.not_null and not col.identity:
        parts.append("NOT NULL")
    return " ".join(parts)


def identity_options(column_type: str, start: int, increment: int, minimum: int, maximum: int, cache: int,
                     cycle: bool, sequence: str | None = None) -> str:
    """` (START WITH 100 INCREMENT BY 10)`: the options of an identity's sequence that are not the defaults CREATE
    TABLE gives it (pre-flight H6), its SEQUENCE NAME when it is not the server's own choice (P4), or ''. No AS: the
    column's type is the sequence's (P6), so the defaults are the column type's."""
    low, high = _INTEGER_RANGES.get(column_type, (None, None))
    default_min, default_max = (1, high) if increment > 0 else (low, -1)
    options = [f"SEQUENCE NAME {sequence}"] if sequence else []
    if increment != 1:
        options.append(f"INCREMENT BY {increment}")
    if minimum != default_min:
        options.append(f"MINVALUE {minimum}")
    if maximum != default_max:
        options.append(f"MAXVALUE {maximum}")
    if start != (minimum if increment > 0 else maximum):
        options.append(f"START WITH {start}")
    if cache != 1:
        options.append(f"CACHE {cache}")
    if cycle:
        options.append("CYCLE")
    return f" ({' '.join(options)})" if options else ""


def storage_options(reloptions: list[str] | None, prefix: str = "") -> tuple[str, ...]:
    """`fillfactor=70` as WITH (…) writes it: the value quoted when it is not a plain word or number; `prefix` is
    `toast.` for the TOAST table's."""
    found = []
    for option in reloptions or ():
        name, _, value = option.partition("=")
        value = value if _PLAIN_OPTION.fullmatch(value) else quote_literal_pg(value)
        found.append(f"{prefix}{name}={value}")
    return tuple(found)


@dataclass(frozen=True)
class TableConstraint:
    name: str
    definition: str
    kind: str  # contype: p, u, c, f, x
    validated: bool  # False: NOT VALID (rows may break it: a dump adds it after the data)
    oid: int


@dataclass(frozen=True)
class Held:
    """What a dump holds back from a table's CREATE TABLE (pre-flight P3): the defaults and checks on a routine that
    reads a view (written after the views), the columns typed as a view's row type (not written)."""

    columns: frozenset[str] = frozenset()
    defaults: frozenset[str] = frozenset()
    constraints: frozenset[str] = frozenset()


NOTHING_HELD = Held()


@dataclass(frozen=True)
class TableParts:
    """A table's DDL as parts: the tree shows them joined (`table_ddl`), a dump writes its foreign keys and its NOT
    VALID constraints after the data (spec ⑥ §3.3.3)."""

    qname: str
    columns: list[PgColumn]
    constraints: list[TableConstraint]  # primary key first; a partition's own only (`partition_constraints`)
    partition_of: str | None  # a partition's CREATE TABLE … PARTITION OF …
    relkind: str
    partkey: str | None
    unlogged: bool
    indexes: list[str]  # the indexes no constraint owns, as CREATE INDEX statements
    comment: str | None
    identity: dict[str, str] = field(default_factory=dict)  # column → its identity's options (`identity_options`)
    options: tuple[str, ...] = ()  # storage parameters, as WITH (…) writes them (the TOAST table's too)
    own: list[str] = field(default_factory=list)  # a partition's own NOT NULL and DEFAULT, as ALTER TABLE (P4)

    @property
    def _only(self) -> str:
        return "ONLY " if self.relkind == "r" else ""  # a partitioned table's goes to its partitions too

    def create(self, foreign_keys: bool = True, not_valid: bool = True, held: Held = NOTHING_HELD) -> str:
        """CREATE TABLE, without its `;`. `foreign_keys` False leaves them out, `not_valid` False the NOT VALID
        constraints (a dump adds both after the data: written inline, a NOT VALID check is validated, H6); `held`
        what a dump writes later or not at all (P3)."""
        storage = f" WITH ({', '.join(self.options)})" if self.options else ""
        if self.partition_of is not None:
            return self.partition_of + storage
        body = [_column_def(c, self.identity.get(c.name, ""), c.name not in held.defaults) for c in self.columns
                if c.name not in held.columns]
        body += [f"CONSTRAINT {quote_ident_pg(c.name)} {c.definition}" for c in self.constraints
                 if (foreign_keys or c.kind != "f") and (not_valid or c.validated) and c.name not in held.constraints]
        statement = f"CREATE {'UNLOGGED ' if self.unlogged else ''}TABLE {self.qname} (\n    " + ",\n    ".join(body) + "\n)"
        return statement + (f" PARTITION BY {self.partkey}" if self.relkind == "p" else "") + storage

    def partition_constraints(self, not_valid: bool = True, held: Held = NOTHING_HELD) -> list[str]:
        """A partition's own constraints (PARTITION OF brings only its parent's), as ALTER TABLE without `;`."""
        if self.partition_of is None:
            return []
        return [f"ALTER TABLE {self._only}{self.qname} ADD CONSTRAINT {quote_ident_pg(c.name)} {c.definition}"
                for c in self.constraints if (not_valid or c.validated) and c.name not in held.constraints]

    def column_settings(self, held: Held = NOTHING_HELD) -> list[str]:
        """A partition's own NOT NULL and DEFAULT, and every column's statistics target, storage and compression
        (P4), as ALTER TABLE without `;`."""
        found = list(self.own)
        for c in self.columns:
            if c.name in held.columns:
                continue
            column = f"ALTER TABLE {self._only}{self.qname} ALTER COLUMN {quote_ident_pg(c.name)}"
            found += [f"{column} SET STATISTICS {c.statistics}"] if c.statistics is not None else []
            found += [f"{column} SET STORAGE {c.storage}"] if c.storage else []
            found += [f"{column} SET COMPRESSION {c.compression}"] if c.compression else []
        return found

    def comments(self, held: Held = NOTHING_HELD) -> list[str]:
        """COMMENT ON the table and its columns, without their `;`."""
        found = [f"COMMENT ON TABLE {self.qname} IS {quote_literal_pg(self.comment)}"] if self.comment else []
        return found + [f"COMMENT ON COLUMN {self.qname}.{quote_ident_pg(c.name)} IS {quote_literal_pg(c.comment)}"
                        for c in self.columns if c.comment and c.name not in held.columns]


def _own_columns(qname: str, relkind: str, columns: list[PgColumn], parent: list[PgColumn]) -> list[str]:
    """What a partition's columns hold that PARTITION OF does not give them: a NOT NULL or a DEFAULT of its own."""
    theirs = {c.name: c for c in parent}
    only = "ONLY " if relkind == "r" else ""
    found = []
    for c in columns:
        inherited = theirs.get(c.name)
        if inherited is None or c.generated or c.identity:
            continue
        column = f"ALTER TABLE {only}{qname} ALTER COLUMN {quote_ident_pg(c.name)}"
        if c.not_null and not inherited.not_null:
            found.append(f"{column} SET NOT NULL")
        if c.default is not None and c.default != inherited.default:
            found.append(f"{column} SET DEFAULT {c.default}")
    return found


def table_parts_of(conn: psycopg.Connection, oids: list[int], *, pretty: bool = True) -> dict[int, TableParts]:
    """The parts of every table in `oids`, in six queries."""
    if not oids:
        return {}
    rows = {row[0]: row for row in conn.execute(_TABLES_SQL, {"oids": list(oids)})}
    parents = [row[7] for row in rows.values() if row[7] is not None]
    columns = columns_of(conn, sorted({*rows, *parents}))
    constraints: dict[int, list[TableConstraint]] = {oid: [] for oid in rows}
    for oid, *row in conn.execute(_CONSTRAINTS_SQL, {"oids": list(rows), "pretty": pretty}):
        constraints[oid].append(TableConstraint(*row))
    indexes: dict[int, list[str]] = {oid: [] for oid in rows}
    for oid, definition in conn.execute(_EXTRA_INDEXES_SQL, {"oids": list(rows)}):
        indexes[oid].append(definition)
    identity: dict[int, dict[str, str]] = {oid: {} for oid in rows}
    if any(c.identity for oid in rows for c in columns[oid]):
        for oid, name, *options, seq_schema, seq_name, schema, table in conn.execute(_IDENTITY_SQL, {"oids": list(rows)}):
            types = {c.name: c.type for c in columns[oid]}
            default = f"{table}_{name}_seq"
            own_name = seq_schema == schema and seq_name == default and len(default.encode()) <= _NAME_BYTES
            identity[oid][name] = identity_options(types[name], *options,
                                                   sequence=None if own_name else qualified_pg(seq_schema, seq_name))
    found = {}
    for oid, (_, schema, name, relkind, is_partition, partkey, comment, parent, parent_name, bound, persistence,
              reloptions, toast) in rows.items():
        qname = qualified_pg(schema, name)
        partition_of = None
        if is_partition:
            sub_partitioning = f" PARTITION BY {partkey}" if relkind == "p" else ""  # a partition can be partitioned
            partition_of = f"CREATE TABLE {qname} PARTITION OF {parent_name}\n    {bound}{sub_partitioning}"
        found[oid] = TableParts(
            qname=qname, columns=columns[oid], constraints=constraints[oid], partition_of=partition_of,
            relkind=relkind, partkey=partkey, unlogged=persistence == "u", indexes=indexes[oid], comment=comment,
            identity=identity[oid], options=storage_options(reloptions) + storage_options(toast, "toast."),
            own=_own_columns(qname, relkind, columns[oid], columns.get(parent, [])) if is_partition else [],
        )
    return found


def table_parts(conn: psycopg.Connection, schema: str, name: str) -> TableParts:
    oid = relation_oid(conn, qualified_pg(schema, name))
    return table_parts_of(conn, [oid])[oid]


def _qualified(parts: list[str] | None) -> str | None:
    """[schema, name] as `schema.name`, quoted where needed (sql/dialects.py)."""
    return qualified_pg(*parts) if parts else None


def table_ddl(conn: psycopg.Connection, schema: str, name: str) -> tuple[str, list[PgColumn], str | None]:
    parts = table_parts(conn, schema, name)
    blocks = [parts.create() + ";"]
    if parts.indexes:
        blocks.append("\n".join(f"{d};" for d in parts.indexes))
    if comments := parts.comments():
        blocks.append("\n".join(f"{c};" for c in comments))
    return "\n\n".join(blocks), parts.columns, parts.comment


@dataclass(frozen=True)
class TypeParts:
    """A type's DDL: its CREATE, and a domain's NOT VALID checks, which a dump adds after the data (P2)."""

    create: str
    not_valid: list[tuple[int, str]] = field(default_factory=list)  # (oid, ALTER DOMAIN … ADD CONSTRAINT … NOT VALID)
    constraints: list[tuple[int, str]] = field(default_factory=list)  # a domain's checks (oid, name): their comments
    relation: int = 0  # a composite's pg_class: its attributes' comments


def type_parts_of(conn: psycopg.Connection, oids: list[int], *, pretty: bool = True) -> dict[int, TypeParts]:
    """The DDL of every enum, domain, composite and range type in `oids`, in at most five queries."""
    if not oids:
        return {}
    rows = {row[0]: row for row in conn.execute(_TYPES_SQL, {"oids": list(oids)})}
    kinds = {typtype for _, _, _, typtype, *_ in rows.values()}
    labels: dict[int, list[str]] = {oid: [] for oid in rows}
    if "e" in kinds:
        for oid, label in conn.execute(_ENUM_LABELS_SQL, {"oids": list(rows)}):
            labels[oid].append(quote_literal_pg(label))
    checks: dict[int, list[tuple[int, str, str, bool]]] = {oid: [] for oid in rows}
    if "d" in kinds:
        for oid, *check in conn.execute(_DOMAIN_CONSTRAINTS_SQL, {"oids": list(rows), "pretty": pretty}):
            checks[oid].append(tuple(check))
    attributes: dict[int, list[str]] = {oid: [] for oid in rows}
    if "c" in kinds:
        for oid, name, type_, collation in conn.execute(_ATTRIBUTES_SQL, {"oids": list(rows)}):
            collation = _qualified(collation)
            attributes[oid].append(f"{quote_ident_pg(name)} {type_}" + (f" COLLATE {collation}" if collation else ""))
    ranges: dict[int, tuple] = {}
    if "r" in kinds:
        multirange = _MULTIRANGE_NAME if conn.info.server_version >= 140000 else "NULL"
        ranges = {row[0]: row[1:] for row in conn.execute(_RANGES_SQL.format(multirange=multirange),
                                                          {"oids": list(rows)})}
    found = {}
    for oid, (_, schema, name, typtype, base, not_null, default, typrelid, collation) in rows.items():
        qname, collation = qualified_pg(schema, name), _qualified(collation)
        if typtype == "e":
            found[oid] = TypeParts(f"CREATE TYPE {qname} AS ENUM ({', '.join(labels[oid])})")
        elif typtype == "d":
            statement = f"CREATE DOMAIN {qname} AS {base}" + (f" COLLATE {collation}" if collation else "")
            statement += (f" DEFAULT {default}" if default is not None else "") + (" NOT NULL" if not_null else "")
            statement += "".join(f"\n    CONSTRAINT {quote_ident_pg(n)} {d}" for _, n, d, valid in checks[oid] if valid)
            later = [(c, f"ALTER DOMAIN {qname} ADD CONSTRAINT {quote_ident_pg(n)} {d}")
                     for c, n, d, valid in checks[oid] if not valid]  # pg_get_constraintdef says NOT VALID
            found[oid] = TypeParts(statement, later, [(c, n) for c, n, _, _ in checks[oid]])
        elif typtype == "c":
            found[oid] = TypeParts(f"CREATE TYPE {qname} AS (\n    " + ",\n    ".join(attributes[oid]) + "\n)",
                                   relation=typrelid)
        elif typtype == "r" and oid in ranges:
            subtype, opclass, range_collation, canonical, diff, multirange = ranges[oid]
            opclass, range_collation = _qualified(opclass), _qualified(range_collation)
            options = [f"SUBTYPE = {subtype}"]
            options += [f"SUBTYPE_OPCLASS = {opclass}"] if opclass else []
            options += [f"COLLATION = {range_collation}"] if range_collation else []
            options += [f"CANONICAL = {canonical}"] if canonical else []
            options += [f"SUBTYPE_DIFF = {diff}"] if diff else []
            options += [f"MULTIRANGE_TYPE_NAME = {multirange}"] if multirange else []
            found[oid] = TypeParts(f"CREATE TYPE {qname} AS RANGE (\n    " + ",\n    ".join(options) + "\n)")
    return found


def type_ddl(conn: psycopg.Connection, qname: str) -> str:
    """The tree's DDL of a type: a domain's NOT VALID checks follow as ALTER DOMAIN (P2)."""
    (oid,) = _one(conn, "SELECT to_regtype(%(q)s)::oid", {"q": qname}, qname)
    parts = type_parts_of(conn, [oid]).get(oid)
    if parts is None:
        (typtype,) = _one(conn, "SELECT typtype::text FROM pg_type WHERE oid = %(oid)s", {"oid": oid}, qname)
        raise ValidationFailed(f"No DDL available for type kind '{typtype}'")
    return "\n".join(f"{statement};" for statement in (parts.create, *(s for _, s in parts.not_valid)))


def sequence_ddl_of(conn: psycopg.Connection, oids: list[int]) -> dict[int, str]:
    """CREATE SEQUENCE (UNLOGGED when it is) of every sequence in `oids`, in one query, without `;`."""
    if not oids:
        return {}
    return {
        oid: (f"CREATE {'UNLOGGED ' if persistence == 'u' else ''}SEQUENCE {qualified_pg(schema, name)}\n    AS {type_}\n"
              f"    INCREMENT BY {inc}\n    MINVALUE {minv}\n    MAXVALUE {maxv}\n    START WITH {start}\n"
              f"    CACHE {cache}\n    {'CYCLE' if cycle else 'NO CYCLE'}")
        for oid, schema, name, type_, start, inc, minv, maxv, cache, cycle, persistence
        in conn.execute(_SEQUENCES_SQL, {"oids": list(oids)})
    }


def sequence_ddl(conn: psycopg.Connection, qname: str) -> str:
    (oid,) = _one(conn, "SELECT s.seqrelid FROM pg_sequence s WHERE s.seqrelid = to_regclass(%(q)s)", {"q": qname}, qname)
    return sequence_ddl_of(conn, [oid])[oid] + ";"


def _view_ddl(conn: psycopg.Connection, qname: str, materialized: bool) -> str:
    (definition,) = _one(conn, "SELECT pg_get_viewdef(to_regclass(%(q)s), true)", {"q": qname}, qname)
    keyword = "CREATE MATERIALIZED VIEW" if materialized else "CREATE OR REPLACE VIEW"
    return f"{keyword} {qname} AS\n{definition.rstrip().rstrip(';')};"


def _function_ddl(conn: psycopg.Connection, schema: str, signature: str) -> str:
    name, _, args = signature.partition("(")
    regproc = f"{qualified_pg(schema, name)}({args}"
    (definition,) = _one(
        conn, "SELECT pg_get_functiondef(p) FROM to_regprocedure(%(sig)s) p", {"sig": regproc}, signature
    )
    return definition.rstrip() + ";"


def object_detail(conn: psycopg.Connection, path: list[PathSegment]) -> ObjectDetail:
    rel = path[1:]
    if not rel:
        raise ValidationFailed("No details available for a database")
    last = path[-1]
    schema = rel[0].name
    base = {"path": path, "kind": last.kind, "name": last.name}
    if last.kind == NodeKind.SCHEMA:
        return ObjectDetail(**base, ddl=f"CREATE SCHEMA {quote_ident_pg(schema)};")
    qname = qualified_pg(schema, last.name)
    match last.kind:
        case NodeKind.TABLE | NodeKind.PARTITION:
            ddl, columns, comment = table_ddl(conn, schema, last.name)
            return ObjectDetail(**base, ddl=ddl, ddl_is_generated=True, columns=[c.info() for c in columns], comment=comment)
        case NodeKind.VIEW | NodeKind.MATERIALIZED_VIEW:
            columns = [c.info() for c in table_columns(conn, relation_oid(conn, qname))]
            return ObjectDetail(**base, ddl=_view_ddl(conn, qname, last.kind == NodeKind.MATERIALIZED_VIEW), columns=columns)
        case NodeKind.FUNCTION | NodeKind.PROCEDURE:
            return ObjectDetail(**base, ddl=_function_ddl(conn, schema, last.name))
        case NodeKind.SEQUENCE:
            return ObjectDetail(**base, ddl=sequence_ddl(conn, qname))
        case NodeKind.TYPE:
            return ObjectDetail(**base, ddl=type_ddl(conn, qname))
        case NodeKind.INDEX:
            (definition,) = _one(conn, "SELECT pg_get_indexdef(to_regclass(%(q)s))", {"q": qname}, qname)
            return ObjectDetail(**base, ddl=f"{definition};")
        case NodeKind.TRIGGER:
            table = qualified_pg(schema, rel[-3].name)  # rel[-2] is the folder, rel[-3] the owning object
            (definition,) = _one(
                conn,
                "SELECT pg_get_triggerdef(t.oid, true) FROM pg_trigger t WHERE t.tgrelid = to_regclass(%(t)s) AND t.tgname = %(n)s",
                {"t": table, "n": last.name},
                last.name,
            )
            return ObjectDetail(**base, ddl=f"{definition};")
        case NodeKind.COLUMN:
            table_oid = relation_oid(conn, qualified_pg(schema, rel[-3].name))
            columns = [c.info() for c in table_columns(conn, table_oid) if c.name == last.name]
            if not columns:
                raise NotFound(f"Column {last.name} not found")
            return ObjectDetail(**base, columns=columns)
    raise ValidationFailed(f"No details available for {last.kind}")
