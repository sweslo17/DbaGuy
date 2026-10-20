"""What a SQL Server dump writes, and in which order (spec ⑥ §3.3.3). Read in a SNAPSHOT transaction only when the
database allows it (A4; its view starts at the first read, which is the catalog's, S13); otherwise each table is read
in its turn at READ COMMITTED (A S9: the dev database, and most others, do not allow it), and the header says so. The
catalog views themselves are not versioned: DDL another session commits during a SNAPSHOT dump fails the dump's read of
that table (3961). Catalog calls never take the metadata session's READ UNCOMMITTED.

Order (the dependency rules are dump_graph.py's). Before the data: schemas, user types (alias and table types, a
table type's own indexes in its body), sequences (each at the value it gives next, A S11), then the tables (without
their foreign keys, non-constraint indexes, the CHECKs they do not enforce and the defaults and checks that wait for the
views) together with the functions their computed columns, defaults and checks call, and the views an inline or
schema-bound one of those binds (S7, review S1), in one dependency order. After the data: identity counters (RESEED to
where the source was, never back: S8, pre-flight M8), indexes, foreign keys (named, so `drop_first` can drop them: H7;
untrusted ones WITH NOCHECK and disabled ones disabled again: L13), then views (an indexed one with its indexes, review
S3), functions and synonyms by sys.sql_expression_dependencies, procedures (deferred name resolution: any order), the
CHECKs the tables do not enforce and the defaults and checks whose function reads a table, view or synonym (WITH CHECK,
with the rows in: review S1), security policies, and triggers on tables and views (a disabled one disabled again, its
first / last order set again: H6). Each module is its own batch, under the QUOTED_IDENTIFIER and ANSI_NULLS it was
created with, and under the name it has now (sp_rename leaves the old one in its text: L13).

Left out, counted and named (H6, S9, review S1, S2): system-versioned, graph and ledger tables and tables with a
period (a plain copy would not be one), modules whose definition is hidden (encrypted, or no VIEW DEFINITION), modules
that run as a database user the file does not create (EXECUTE AS 'user'), tables whose definition cannot be read, and
everything that needs one of those, transitively. Counted: CLR types and modules, database triggers, indexes DbaGuy
does not write (columnstore, XML, spatial, hash), index and table options, partitioning, full-text indexes,
statistics, extended properties, XML schema collections and the like."""

from decimal import Decimal
from itertools import islice

from loguru import logger

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound
from ...sql.dialects import Dialect
from ...sql.lexer import INSIGNIFICANT, Token, TokenKind, scan
from ...sql.scope import unquote
from ..columns import TableColumn
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, shown_names
from . import driver
from .catalog import batch, by_object, chunks, id_list, id_sources, is_system_schema, literal, q, result_sets, rows
from .columns import columns_of
from .dump_graph import FUNCTIONS, Graph, Object, Use
from .editing import table_flags_of, table_flags_reason
from .introspection import (
    EXACT, TableRows, TableStatements, sequences_at_next_value, synonyms_of, table_rows_of, types_of, view_indexes_of)

# L8, review S5: a login whose user options turn IMPLICIT_TRANSACTIONS on would open a transaction with the dump's
# first read, which then nests the snapshot's (3951) or outlives a per-table dump: off before anything is read
_IMPLICIT_OFF = "SET IMPLICIT_TRANSACTIONS OFF"
SNAPSHOT = DumpSession(setup=(_IMPLICIT_OFF, "SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "BEGIN TRANSACTION"),
                       teardown=("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION",  # review S6: 3961 / 3951 end it first
                                 "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"),
                       consistency="snapshot", changed_codes=frozenset({"3961"}))  # DDL after the snapshot began
PER_TABLE = DumpSession(setup=(_IMPLICIT_OFF,), teardown=(), consistency="per_table")
SNAPSHOT_TEXT = "one consistent snapshot (SNAPSHOT isolation)"
PER_TABLE_TEXT = "not one consistent snapshot: each table was read in its turn (READ COMMITTED)"
PER_TABLE_WARNING = ("Not a consistent snapshot: the database does not allow SNAPSHOT isolation, so each table is read "
                     "in its turn")
TEMPORAL = "system-versioned, graph or ledger tables"
HIDDEN_MODULES = "modules whose definition cannot be read (encrypted, or no VIEW DEFINITION)"
UNREADABLE_TABLES = "tables whose definition cannot be read"
RUNS_AS = "modules that run as a database user (EXECUTE AS 'user')"
NEEDING = "views, functions, synonyms, triggers and security policies that need what the dump leaves out"

_SNAPSHOT_ALLOWED = f"{_IMPLICIT_OFF};\nSELECT snapshot_isolation_state FROM sys.databases WHERE name = DB_NAME()"
_START = f"""
{_SNAPSHOT_ALLOWED};
SELECT CONVERT(nvarchar(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation')), HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UNMASK');
SELECT name, schema_id FROM sys.schemas ORDER BY schema_id
"""
# Creation order: object ids are not (they wrap), create_date ties within a batch
_OBJECTS = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type), o.parent_object_id,
       CASE WHEN o.type = 'U' AND EXISTS (SELECT 1 FROM sys.columns c WHERE c.object_id = o.object_id
                                          AND c.generated_always_type <> 0) THEN 1 ELSE 0 END,
       CASE WHEN OBJECTPROPERTY(o.object_id, 'IsSchemaBound') = 1 THEN 1 ELSE 0 END
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.schema_id IN ({schemas}) AND o.is_ms_shipped = 0 AND (o.parent_object_id = 0 OR o.type = 'TR')
ORDER BY o.create_date, o.object_id
"""
# Subqueries giving at least the scope's objects of a kind (catalog.id_sources; a schema or database dump): a list of
# a thousand literal ids takes the server ~0.1 s to compile per statement. No quote in any: dynamic SQL takes them.
_WITHIN = {
    "tables": "SELECT object_id FROM sys.tables WHERE is_ms_shipped = 0 AND schema_id IN ({schemas})",
    "views": "SELECT object_id FROM sys.views WHERE schema_id IN ({schemas})",
    "sequences": "SELECT object_id FROM sys.sequences WHERE schema_id IN ({schemas})",
    "synonyms": "SELECT object_id FROM sys.synonyms WHERE schema_id IN ({schemas})",
    "modules": "SELECT m.object_id FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id "
               "WHERE o.schema_id IN ({schemas})",
    "objects": "SELECT object_id FROM sys.objects WHERE schema_id IN ({schemas})",
    "types": "SELECT user_type_id FROM sys.types WHERE is_user_defined = 1 AND schema_id IN ({schemas})",
}
# A ledger table's view (`t_Ledger`, SQL Server 2022's ledger_view_id) goes with its table, which is left out
_LEDGER_VIEWS = """
DECLARE @views nvarchar(max) = CASE WHEN COL_LENGTH(N'sys.tables', N'ledger_view_id') IS NULL THEN N'SELECT 0 WHERE 1 = 0'
    ELSE N'SELECT ledger_view_id FROM sys.tables WHERE ledger_view_id IS NOT NULL AND schema_id IN ({schemas})' END;
EXEC (@views)
"""
# What each object of the scope reads; a default's or check's row names its table and itself. Classes 1 only (6 is a
# type, whose id is a user_type_id).
_DEPENDS = """
SELECT DISTINCT CASE WHEN o.type IN ('C', 'D') THEN o.parent_object_id ELSE o.object_id END,
       CASE WHEN o.type IN ('C', 'D') THEN o.name END, CASE WHEN o.type IN ('C', 'D') THEN RTRIM(o.type) END,
       d.referenced_id, RTRIM(r.type), SCHEMA_NAME(r.schema_id), r.name, r.is_ms_shipped
FROM sys.sql_expression_dependencies d
JOIN sys.objects o ON o.object_id = d.referencing_id
JOIN sys.objects r ON r.object_id = d.referenced_id
WHERE d.referencing_class = 1 AND d.referenced_class = 1 AND o.schema_id IN ({schemas})
"""
_TYPES = """
SELECT t.user_type_id, s.name, t.name, t.is_assembly_type FROM sys.types t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_user_defined = 1 AND t.schema_id IN ({schemas}) ORDER BY t.user_type_id
"""
# Whether each module's definition can be read, who it runs as (execute_as_principal_id: NULL caller, -2 owner, else a
# database principal: SELF or a named user), and what each synonym names
_MODULE_META = """
SELECT m.object_id, CASE WHEN m.definition IS NULL THEN 1 ELSE 0 END, m.execute_as_principal_id,
       USER_NAME(m.execute_as_principal_id)
FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id WHERE o.schema_id IN ({schemas});
SELECT object_id, OBJECT_ID(base_object_name) FROM sys.synonyms WHERE schema_id IN ({schemas})
"""
_HEADS = "SELECT object_id, LEFT(definition, 4000) FROM sys.sql_modules WHERE object_id IN ({ids})"
# The user types the dump's tables, its modules' parameters and their table types' columns use, by user
_USED_TYPES = """
SELECT DISTINCT x.object_id, t.user_type_id, SCHEMA_NAME(t.schema_id), t.name FROM (
    SELECT object_id, user_type_id FROM sys.columns WHERE object_id IN ({ids})
    UNION ALL SELECT object_id, user_type_id FROM sys.parameters WHERE object_id IN ({ids})
    UNION ALL SELECT p.object_id, c.user_type_id FROM sys.parameters p
              JOIN sys.table_types tt ON tt.user_type_id = p.user_type_id
              JOIN sys.columns c ON c.object_id = tt.type_table_object_id WHERE p.object_id IN ({ids})
) x JOIN sys.types t ON t.user_type_id = x.user_type_id WHERE t.is_user_defined = 1
"""
# With no last_value (no row went in since the table was made or truncated) the next id is IDENT_CURRENT: the seed,
# or what a RESEED set
_IDENTITY = """
SELECT object_id, CAST(last_value AS nvarchar(40)), CAST(increment_value AS nvarchar(40)), CAST(seed_value AS
       nvarchar(40)), CASE WHEN last_value IS NULL THEN CAST(IDENT_CURRENT(QUOTENAME(OBJECT_SCHEMA_NAME(object_id))
       + N'.' + QUOTENAME(OBJECT_NAME(object_id))) AS nvarchar(40)) END
FROM sys.identity_columns WHERE object_id IN ({ids})
"""
# Catalog views, not sys.dm_db_partition_stats (VIEW DATABASE STATE): one IN_ROW_DATA unit per partition holds its rows
_SIZES = """
SELECT p.object_id, SUM(CASE WHEN a.type = 1 THEN p.rows ELSE 0 END), SUM(a.used_pages) * 8192
FROM sys.partitions p JOIN sys.allocation_units a ON a.container_id = p.partition_id
WHERE p.index_id IN (0, 1) AND p.object_id IN ({ids}) GROUP BY p.object_id
"""
# What a table has that its DDL does not write, one count each (in _TABLE_COUNTED's order)
_TABLE_COUNTS = """
SELECT COUNT(*) FROM sys.indexes WHERE object_id IN ({ids}) AND type NOT IN (0, 1, 2);
SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id IN ({ids}) AND i.type IN (0, 1, 2) AND (i.is_disabled = 1
  OR i.ignore_dup_key = 1 OR i.fill_factor NOT IN (0, 100) OR i.is_padded = 1 OR i.allow_row_locks = 0
  OR i.allow_page_locks = 0 OR EXISTS (SELECT 1 FROM sys.partitions p WHERE p.object_id = i.object_id
                                       AND p.index_id = i.index_id AND p.data_compression <> 0));
SELECT COUNT(DISTINCT i.object_id) FROM sys.indexes i JOIN sys.data_spaces d ON d.data_space_id = i.data_space_id
WHERE d.type = 'PS' AND i.object_id IN ({ids});
SELECT COUNT(*) FROM sys.tables WHERE object_id IN ({ids}) AND is_memory_optimized = 1;
SELECT COUNT(*) FROM sys.fulltext_indexes WHERE object_id IN ({ids});
SELECT COUNT(*) FROM sys.stats WHERE object_id IN ({ids}) AND user_created = 1;
SELECT COUNT(*) FROM sys.columns WHERE object_id IN ({ids}) AND is_filestream = 1;
SELECT COUNT(*) FROM sys.columns WHERE object_id IN ({ids}) AND encryption_type IS NOT NULL;
SELECT COUNT(*) FROM sys.columns WHERE object_id IN ({ids}) AND xml_collection_id <> 0;
SELECT COUNT(*) FROM sys.columns c WHERE c.object_id IN ({ids}) AND (c.rule_object_id <> 0 OR c.default_object_id IN
  (SELECT object_id FROM sys.objects WHERE type = 'D' AND parent_object_id = 0));
SELECT COUNT(*) FROM sys.columns WHERE object_id IN ({ids}) AND is_masked = 1
"""
_TABLE_COUNTED = (
    "indexes DbaGuy does not write (columnstore, XML, spatial, hash)",
    "indexes and heaps whose options are not written (IGNORE_DUP_KEY, fill factor, compression, lock options, "
    "disabled)",
    "partitioned tables (they come back unpartitioned: partition schemes and filegroups are not written)",
    "memory-optimized tables (they come back as disk tables)",
    "full-text indexes",
    "statistics made with CREATE STATISTICS",
    "FILESTREAM columns (they come back as varbinary(max))",
    "Always Encrypted columns",
    "typed xml columns (their XML schema collections are not written: they come back untyped)",
    "columns with a rule or a default bound with sp_bindrule / sp_bindefault",
)
_MASKED = len(_TABLE_COUNTED)  # the last count: masked columns (written, but read masked without UNMASK)
# Of an object, a column, a parameter or an index (classes 1, 2, 7); of a type (6); of a schema (3)
_PROPERTIES = "SELECT COUNT(*) FROM sys.extended_properties WHERE class IN (1, 2, 7) AND major_id IN ({ids})"
_OTHER_PROPERTIES = """
SELECT COUNT(*) FROM sys.extended_properties WHERE (class = 6 AND major_id IN ({types}))
    OR (class = 3 AND major_id IN ({schemas}))
"""
_SCOPE_COUNTS = """
SELECT COUNT(*) FROM sys.xml_schema_collections WHERE schema_id IN ({schemas});
SELECT COUNT(*) FROM sys.extended_properties WHERE class = 0;
SELECT COUNT(*) FROM sys.triggers WHERE parent_class = 0;
SELECT COUNT(*) FROM sys.assemblies WHERE is_user_defined = 1;
SELECT COUNT(*) FROM sys.fulltext_catalogs
"""
_SCOPE_COUNTED = ("XML schema collections", "extended properties of the database", "database triggers", "assemblies",
                  "full-text catalogs")  # the last four: a database dump only
_MODULES = """
SELECT object_id, {definition}, uses_ansi_nulls, uses_quoted_identifier FROM sys.sql_modules
WHERE object_id IN ({ids})
"""
_TRIGGERS = """
SELECT object_id, is_disabled FROM sys.triggers WHERE object_id IN ({ids});
SELECT object_id, type_desc, is_first FROM sys.trigger_events WHERE object_id IN ({ids}) AND (is_first = 1 OR is_last = 1)
ORDER BY object_id, type
"""
_POLICIES = """
SELECT p.object_id, p.is_enabled, p.is_schema_bound, p.is_not_for_replication, pr.predicate_type_desc,
       pr.predicate_definition, pr.target_object_id, pr.operation_desc
FROM sys.security_policies p JOIN sys.security_predicates pr ON pr.object_id = p.object_id
WHERE p.object_id IN ({ids}) ORDER BY p.object_id, pr.security_predicate_id
"""
_MODULE_KINDS = {"V": ("view", "VIEW"), "FN": ("function", "FUNCTION"), "IF": ("function", "FUNCTION"),
                 "TF": ("function", "FUNCTION"), "P": ("procedure", "PROCEDURE"), "SN": ("synonym", "SYNONYM"),
                 "TR": ("trigger", "TRIGGER")}
# Counted where they are: a CLR module's assembly, and the rest, are nothing a catalog query writes back
_CLR_MODULES = "CLR functions, procedures, aggregates and triggers"
_RULES = "rules and defaults made with CREATE RULE / CREATE DEFAULT"
_COUNTED_OBJECTS = {"FS": _CLR_MODULES, "FT": _CLR_MODULES, "PC": _CLR_MODULES, "AF": _CLR_MODULES,
                    "TA": _CLR_MODULES, "R": _RULES, "D": _RULES, "ET": "external tables", "SQ": "Service Broker queues"}
_CLR = frozenset({"FS", "FT", "PC", "AF", "TA"})
_HEAD_WORDS = frozenset({"VIEW", "PROC", "PROCEDURE", "FUNCTION", "TRIGGER"})
_HEAD_TOKENS = 64
_RUNNERS = frozenset({"CALLER", "SELF", "OWNER"})  # EXECUTE AS that needs no user where the file is run
_DBO = 1  # the database principal dbo, in every database


def session(conn: driver.Connection) -> DumpSession:
    found = rows(conn, _SNAPSHOT_ALLOWED)
    return SNAPSHOT if found and found[0][0] == 1 else PER_TABLE


def _drop(qualified: str, code: str, statement: str) -> str:
    """`drop_first`'s DROP of one object: `statement` when the object (`qualified`, of sys.objects type `code`) is
    there."""
    return f"IF OBJECT_ID({literal(qualified)}, N'{code}') IS NOT NULL {statement}"


def runs_as(head: str) -> str | None:
    """Who a module runs as, from its head (`WITH EXECUTE AS …`): CALLER, SELF, OWNER or the user's name; None when it
    says nothing. The first `EXEC[UTE] AS` followed by one of those or a string: a body's `EXECUTE AS USER = …` is a
    statement, not the module's clause."""
    tokens = [token for token in scan(head, Dialect.TSQL) if token.kind not in INSIGNIFICANT]
    for i in range(len(tokens) - 2):
        if tokens[i].upper in ("EXEC", "EXECUTE") and tokens[i + 1].upper == "AS":
            who = tokens[i + 2]
            if who.kind == TokenKind.WORD and who.upper in _RUNNERS:
                return who.upper
            if who.kind == TokenKind.STRING:
                text = who.text[1:] if who.text[:1] in "Nn" else who.text
                return text[1:-1].replace("''", "'")
    return None


def _module_statements(definition: str, ansi_nulls: bool, quoted_identifier: bool) -> tuple[str, ...]:
    """A module's definition as the server keeps it (to its last newline: the restored text is the same text), under
    the QUOTED_IDENTIFIER and ANSI_NULLS it was created with (the header sets both ON): each SET a batch of its own,
    before, and back to ON after."""
    before = [f"SET {name} OFF" for name, on in (("QUOTED_IDENTIFIER", quoted_identifier), ("ANSI_NULLS", ansi_nulls))
              if not on]
    return (*before, definition, *(statement.replace(" OFF", " ON") for statement in before))


def _name_at(tokens: list[Token], i: int) -> tuple[tuple[int, int], list[str], int] | None:
    """The multi-part name starting at tokens[i]: its span in the text, its parts unquoted, the index after it."""
    if i >= len(tokens) or tokens[i].kind not in (TokenKind.WORD, TokenKind.QUOTED):
        return None
    start, parts = tokens[i].start, [unquote(tokens[i])]
    end = tokens[i].end
    i += 1
    while i + 1 < len(tokens) and tokens[i].kind == TokenKind.DOT and tokens[i + 1].kind in (TokenKind.WORD,
                                                                                             TokenKind.QUOTED):
        parts.append(unquote(tokens[i + 1]))
        end = tokens[i + 1].end
        i += 2
    return (start, end), parts, i


def _is(parts: list[str], schema: str, name: str) -> bool:
    return len(parts) >= 2 and parts[-2].casefold() == schema.casefold() and parts[-1].casefold() == name.casefold()


def as_named(definition: str, schema: str, name: str, parent: tuple[str, str] | None = None) -> tuple[str, bool]:
    """A module's definition under its name now, schema-qualified, and a trigger's under its table's (`parent`):
    sp_rename leaves sys.sql_modules' text as it was created (pre-flight L13), and an unqualified name would land in
    the default schema of whoever runs the file. (the text, whether a name differed from the object's own)."""
    tokens = [token for token in islice(scan(definition, Dialect.TSQL), _HEAD_TOKENS) if token.kind not in INSIGNIFICANT]
    words = [token.upper if token.kind == TokenKind.WORD else "" for token in tokens]
    i = 3 if words[:3] == ["CREATE", "OR", "ALTER"] else 1
    if words[:1] != ["CREATE"] or i >= len(words) or words[i] not in _HEAD_WORDS:
        return definition, False
    edits: list[tuple[tuple[int, int], str]] = []
    renamed = False
    found = _name_at(tokens, i + 1)
    targets = [(found, schema, name)]
    if found is not None and parent is not None and found[2] < len(words) and words[found[2]] == "ON":
        targets.append((_name_at(tokens, found[2] + 1), *parent))
    for target, want_schema, want_name in targets:
        if target is None:
            return definition, False
        span, parts, _ = target
        if not _is(parts, want_schema, want_name):
            edits.append((span, f"{q(want_schema)}.{q(want_name)}"))
            renamed = renamed or parts[-1].casefold() != want_name.casefold() or (
                len(parts) >= 2 and parts[-2].casefold() != want_schema.casefold())
    for (start, end), text in reversed(edits):
        definition = definition[:start] + text + definition[end:]
    return definition, renamed


def unwrapped(definition: str) -> str:
    """A predicate as CREATE SECURITY POLICY takes it: sys.security_predicates keeps it in one more pair of
    parentheses, `([s].[f]([id]))`, which the statement refuses (102)."""
    tokens = [token for token in scan(definition, Dialect.TSQL) if token.kind not in INSIGNIFICANT]
    if len(tokens) < 2 or tokens[0].kind != TokenKind.LPAREN or tokens[-1].kind != TokenKind.RPAREN:
        return definition
    depth = 0
    for i, token in enumerate(tokens):
        depth += (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN)
        if depth == 0:
            return definition[tokens[0].end:tokens[-1].start].strip() if i == len(tokens) - 1 else definition
    return definition


def next_id(last: str | None, increment: str, seed: str, current: str | None) -> str | None:
    """The id a table's next row gets: after its last value; with none (no row went in since it was made or
    truncated) IDENT_CURRENT, its seed or what a RESEED set. None for a counter still at its seed: nothing to write."""
    if last is not None:
        return str(EXACT.add(Decimal(last), Decimal(increment)))
    if current is None or Decimal(current) == Decimal(seed):
        return None
    return current


def _reseed(qname: str, following: str, increment: str) -> str:
    """S8 and pre-flight M8: DBCC CHECKIDENT … RESEED n makes n the next id of a table no row was inserted into, and
    n + increment otherwise. A restored table with no rows (the source's were deleted) gets the source's next id
    (`following`); one the rows went into is moved only forward: past the largest id written, which a per-table read
    may hold beyond the counter it read first, it stays."""
    step = Decimal(increment)
    before = EXACT.subtract(Decimal(following), step)
    name = literal(qname)
    return (f"IF (SELECT last_value FROM sys.identity_columns WHERE object_id = OBJECT_ID({name})) IS NULL\n"
            f"    DBCC CHECKIDENT ({name}, RESEED, {following}) WITH NO_INFOMSGS\n"
            f"ELSE IF IDENT_CURRENT({name}) {'<' if step > 0 else '>'} {before}\n"
            f"    DBCC CHECKIDENT ({name}, RESEED, {before}) WITH NO_INFOMSGS")


class _Reader:
    """One catalog read; `ddl` False (a dump plan) lists the same objects without their definitions."""

    def __init__(self, conn: driver.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.scope, self.kinds, self.ddl = conn, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}
        self.warnings: list[str] = []
        self.objects: dict[int, Object] = {}  # the scope's schemas' objects, in creation order
        self.position: dict[int, int] = {}  # an object's place in that order
        self.graph = Graph({}, ())
        self.hidden: dict[int, str] = {}  # left out for what it is (or, "needs", what it needs): never written
        self.synonyms: dict[int, str] = {}
        self.view_indexes: dict[int, list[str]] = {}
        self.schema_ids = "NULL"  # the scope's schemas, an id_list
        self.snapshot = False
        self.collation: str | None = None

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def label(self, object_id: int) -> str:
        return self.graph.label(object_id)

    def within(self, kind: str, chosen: bool = True) -> str | None:
        """catalog.id_sources' subquery for `kind`: in a schema or database dump that writes all of them (`chosen`);
        a `tables` dump, or one that writes only some, lists its ids."""
        return _WITHIN[kind].format(schemas=self.schema_ids) if chosen and not self.named else None

    def said(self, what: str, why: str, names: list[str]) -> None:
        """Objects left out, counted (H6) and named (review S1)."""
        self.leave_out(what, len(names))
        if names:
            self.warnings.append(f"Not written, {why}: {shown_names(names)}")

    # ---- the read ----

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        snapshot, settings, schema_rows = result_sets(self.conn, _START)
        self.snapshot = bool(snapshot) and snapshot[0][0] == 1
        self.collation, unmask = settings[0] if settings else (None, 1)
        schemas = self._schemas(schema_rows)
        ids = self.schema_ids = id_list(schemas.values())
        objects, flags, uses, type_rows, ledger_views, meta, bases = result_sets(self.conn, batch(
            _OBJECTS.format(schemas=ids), table_flags_of(_WITHIN["tables"].format(schemas=ids)),
            _DEPENDS.format(schemas=ids), _TYPES.format(schemas=ids), _LEDGER_VIEWS.format(schemas=ids),
            _MODULE_META.format(schemas=ids)))
        ledger = {int(view) for (view,) in ledger_views}
        self.objects = {int(row[0]): Object(int(row[0]), row[1], row[2], row[3], int(row[4]), bool(row[5]),
                                            bool(row[6])) for row in objects if int(row[0]) not in ledger}
        self.position = {object_id: i for i, object_id in enumerate(self.objects)}
        self.graph = Graph(self.objects, (Use(int(row[0]), int(row[3]), row[4], row[5], row[6], bool(row[7]), row[1],
                                              row[2]) for row in uses),
                           {int(synonym): int(base) for synonym, base in bases if base is not None})
        self.hidden = dict.fromkeys(ledger, "ledger")  # a ledger table's view goes with it
        picked = self._picked()
        if not self.named:
            for obj in picked:
                if obj.type in _COUNTED_OBJECTS:
                    self.leave_out(_COUNTED_OBJECTS[obj.type])
        flags_of = {int(row[0]): tuple(row[1:]) for row in flags}
        candidates = [obj for obj in picked if obj.type == "U" and self._dumped_table(obj, flags_of.get(obj.object_id))]
        self._hide_modules(meta)
        later = self.graph.later(obj.object_id for obj in candidates)
        parts = self._table_statements(candidates, type_rows, later)
        needing = self.graph.spread(self.hidden, {obj.object_id for obj in candidates})
        self._say_needing(needing)
        tables = [obj for obj in candidates if obj.object_id not in self.hidden]
        table_ids = {obj.object_id for obj in tables}
        early = self.graph.early(table_ids, later) - self.hidden.keys()
        policies = self._policies(table_ids)
        views = {obj.object_id for obj in picked if obj.type == "V" and (self.kinds.views or self.named)
                 and obj.object_id not in self.hidden} | {m for m in early if self.objects[m].type == "V"}
        triggers = self._triggers_of(table_ids | views)
        modules = self._modules(picked, early, policies, triggers)
        sequences = self._sequences_of(table_ids)
        written = table_ids | set(modules) | {obj.object_id for obj in sequences} | {policy for policy, _ in policies}
        if needed := self.graph.needs(written, self.hidden, self.position):  # review S1: after _readable
            self.leave_out("objects the dump's tables, views and functions need (from other schemas, not chosen or "
                           "left out)", len(needed))
            self.warnings.append(f"Not in this dump, and needed where it is run: {shown_names(needed)}")
        columns, identities, sizes, masked = self._table_details(tables)
        types = self._types(type_rows, [*table_ids, *modules])
        if masked and not unmask:
            self.warnings.append(f"{masked:,} masked columns are read masked (this login has no UNMASK permission): "
                                 "the dump holds the masks, not the values")
        self._count_properties(schemas, [*written, *triggers], types)
        if self.scope.kind != "tables":
            self._count_scope(schemas)
        texts = self._module_texts([*modules, *triggers])
        if self.ddl:
            self.synonyms = synonyms_of(self.conn, {m: (self.objects[m].schema, self.objects[m].name)
                                                    for m in modules if self.objects[m].type == "SN"},
                                        self.within("synonyms", self.kinds.views))
            self.view_indexes = view_indexes_of(self.conn, {m: (self.objects[m].schema, self.objects[m].name)
                                                            for m in modules if self.objects[m].type == "V"},
                                                self.within("views"))
        pre: list[DumpObject] = [self._schema(name) for name in schemas if name != "dbo"]
        pre += types.values()
        pre += self._sequences(sequences)
        pre += self._tables_and_early(tables, [m for m in modules if m in early], parts, texts)
        late = [m for m in modules if m not in early]
        post = [self._identity(obj, *identities[obj.object_id]) for obj in tables if obj.object_id in identities]
        post += [DumpObject("index", obj.label, (index,) if self.ddl else ())
                 for obj in tables for index in parts[obj.object_id].indexes]
        post += self._foreign_keys(tables, parts)
        post += [item for module in self._module_order(late) if (item := self._module(module, texts)) is not None]
        post += self._constraints(tables, parts)
        post += [self._policy(policy, predicates) for policy, predicates in policies]
        post += self._triggers(triggers, texts)
        by_name = {(obj.schema, obj.name): obj for obj in tables}  # review S4: once, not per table
        data = [self._data(obj, columns.get(obj.object_id, []), sizes.get(obj.object_id), parts[obj.object_id],
                           by_name) for obj in tables]
        return pre, data, post

    def _schemas(self, schema_rows: list[tuple]) -> dict[str, int]:
        found = {name: int(schema_id) for name, schema_id in schema_rows if not is_system_schema(name, schema_id)}
        if self.scope.kind == "database":
            return found
        if self.scope.schema_name not in found:
            raise NotFound(f"Schema {self.scope.schema_name} not found")
        return {self.scope.schema_name: found[self.scope.schema_name]}

    def _picked(self) -> list[Object]:
        """The objects the scope chooses, in creation order: every one, or (a `tables` scope) the named tables and
        views. What they need (functions, synonyms, sequences, types), their triggers and their security policies
        come from the whole schema."""
        objects = list(self.objects.values())
        if not self.named:
            return objects
        wanted = set(self.scope.names)
        picked = [obj for obj in objects if obj.name in wanted and obj.type in ("U", "V")]
        if missing := wanted - {obj.name for obj in picked}:
            raise NotFound(f"Not found in {self.scope.schema_name}: {', '.join(sorted(missing))}")
        return picked

    def _dumped_table(self, obj: Object, flags: tuple | None) -> bool:
        """System-versioned, graph and ledger tables are left out: a plain table would not be one (G9, S9); so is a
        table with a period whose versioning is off (its GENERATED ALWAYS columns cannot be written back)."""
        temporal, is_node, is_edge, ledger = flags or (0, False, False, 0)
        if table_flags_reason(temporal, is_node, is_edge, ledger) is not None or temporal == 2:
            self.leave_out(TEMPORAL)
            self.warnings.append(f"{obj.label} is left out: system-versioned, graph and ledger tables are not dumped")
        elif obj.generated:
            self.leave_out("tables with a period (GENERATED ALWAYS columns)")
            self.warnings.append(f"{obj.label} is left out: its GENERATED ALWAYS columns (a period) cannot be written "
                                 "back")
        else:
            return True
        self.hidden[obj.object_id] = "table"
        return False

    def _hide_modules(self, meta: list[tuple]) -> None:
        """What no file can carry is left out (and, through Graph.spread, what needs it): CLR modules (counted where
        they are), modules whose definition is hidden, and (review S2) modules that run as a database user — the file
        does not create users, and CREATE fails without one (15151, which also ends the run's transaction)."""
        for obj in self.objects.values():
            if obj.type in _CLR:
                self.hidden[obj.object_id] = "clr"
        hidden_text, as_user = [], {}
        for object_id, unreadable, principal, user in meta:
            obj = self.objects.get(int(object_id))
            if obj is None:
                continue
            if unreadable:
                self.hidden[obj.object_id] = "definition"
                hidden_text.append(obj.label)
            elif principal is not None and int(principal) not in (-2, _DBO):  # owner, dbo: there wherever it runs
                as_user[obj.object_id] = user or str(principal)
        heads = {int(object_id): head for source in chunks(as_user) for object_id, head in rows(
            self.conn, _HEADS.format(ids=source))}
        runners = []
        for object_id, user in as_user.items():
            if runs_as(heads.get(object_id) or "") != "SELF":  # SELF is whoever creates it
                self.hidden[object_id] = "runs as"
                runners.append(f"{self.objects[object_id].label} (as {user})")
        if not self.named:  # a tables dump names only those it needs (Graph.spread)
            self.said(HIDDEN_MODULES, "their definition cannot be read (encrypted, or no VIEW DEFINITION)", hidden_text)
            self.said(RUNS_AS, "they run as a database user the file does not create (EXECUTE AS)", runners)

    def _table_statements(self, candidates: list[Object], type_rows: list[tuple],
                          later: dict[int, frozenset[str]]) -> dict[int, TableStatements]:
        """Each table's statements, the defaults and checks of `later` added after the views. A table whose definition
        cannot be read, or that uses a CLR type, is left out (and what needs it, by Graph.spread)."""
        named = {obj.object_id: (obj.schema, obj.name) for obj in candidates}
        found_rows: dict[int, TableRows] = table_rows_of(self.conn, named, self.within("tables"))
        clr = {(schema, name) for _, schema, name, assembly in type_rows if assembly}
        parts: dict[int, TableStatements] = {}
        for obj in candidates:
            table = found_rows.get(obj.object_id)
            if table is None:  # dropped while the dump read the catalog
                self.hidden[obj.object_id] = "gone"
                continue
            parts[obj.object_id] = table.statements(later.get(obj.object_id, frozenset()))
            self.leave_out("definitions hidden from this login (indexes, foreign keys, checks, defaults)",
                           parts[obj.object_id].hidden)
            if parts[obj.object_id].create is None:
                self.hidden[obj.object_id] = "unreadable"
                self.leave_out(UNREADABLE_TABLES)
                self.warnings.append(f"The definition of {obj.label} cannot be read (hidden from this login, or a "
                                     "default bound with sp_bindefault): the table is left out")
            elif used := sorted(f"{schema}.{name}" for schema, name in table.user_types() & clr):
                self.hidden[obj.object_id] = "unreadable"
                self.leave_out(UNREADABLE_TABLES)
                self.warnings.append(f"Not written, their definition needs what the dump leaves out: {obj.label} "
                                     f"(needs CLR type {', '.join(used)})")
        return parts

    def _say_needing(self, needing: dict[int, str]) -> None:
        """Review S1: what Graph.spread left out, counted and named (a table counts as one whose definition cannot be
        read)."""
        tables = [f"{self.label(i)} (needs {need})" for i, need in needing.items() if self.objects[i].type == "U"]
        others = [f"{self.label(i)} (needs {need})" for i, need in needing.items() if self.objects[i].type != "U"]
        self.said(UNREADABLE_TABLES, "their definition needs what the dump leaves out", tables)
        self.said(NEEDING, "they need what the dump leaves out", others)

    def _modules(self, picked: list[Object], early: set[int], policies: list, triggers: list[int]) -> list[int]:
        """The views, functions, synonyms and procedures written, in creation order: those chosen, the ones the tables
        call (S7), and the functions and synonyms the views, security policies, triggers and other modules written
        call (a `tables` dump brings them too). Never one the dump leaves out."""
        chosen: set[int] = set(early)
        for obj in picked:
            if obj.object_id in self.hidden:
                continue
            if obj.type == "V":
                wanted = self.kinds.views or self.named
            elif obj.type == "SN":
                wanted = self.kinds.views and not self.named
            elif obj.type in FUNCTIONS or obj.type == "P":
                wanted = self.kinds.routines and not self.named
            else:
                continue
            if wanted:
                chosen.add(obj.object_id)
        roots = chosen | {policy for policy, _ in policies} | set(triggers)
        chosen |= self.graph.closure(roots, FUNCTIONS | {"SN"}, set(self.hidden))
        if not self.named:
            def unchosen(kinds: frozenset[str]) -> int:
                return sum(obj.type in kinds and obj.object_id not in chosen and obj.object_id not in self.hidden
                           for obj in picked)
            self.leave_out("views (not chosen)", unchosen(frozenset({"V"})))
            self.leave_out("synonyms (not chosen)", unchosen(frozenset({"SN"})))
            self.leave_out("functions and procedures (not chosen)", unchosen(FUNCTIONS | {"P"}))
        return [obj.object_id for obj in self.objects.values() if obj.object_id in chosen]

    def _module_order(self, modules: list[int]) -> list[int]:
        """Views, functions and synonyms by their dependencies (creation order otherwise), procedures after them."""
        return [*self.graph.order([m for m in modules if self.objects[m].type != "P"]),
                *self.graph.order([m for m in modules if self.objects[m].type == "P"])]

    def _sequences_of(self, tables: set[int]) -> list[Object]:
        """The sequences the tables' defaults use (always), and the others when chosen."""
        candidates = [obj for obj in self.objects.values() if obj.type == "SO"]
        chosen = not self.named and self.kinds.sequences
        used = set() if chosen else self.graph.used_by(tables)  # review S4: once, not per sequence
        found = [obj for obj in candidates if chosen or obj.object_id in used]
        if not self.named and not chosen:
            self.leave_out("sequences (not chosen)", len(candidates) - len(found))
        return found

    def _table_details(self, tables: list[Object]):
        """Each written table's columns, identity, size and what its DDL does not write: one batch each, per ID_CHUNK
        tables in a `tables` dump. The counts need exactly the written tables: the scope's, but those left out."""
        named = {obj.object_id: (obj.schema, obj.name) for obj in tables}
        within = self.within("tables")
        found_columns = columns_of(self.conn, list(named), within)
        identities: dict[int, tuple[str, str]] = {}
        sizes: dict[int, tuple] = {}
        masked = 0
        left = [obj.object_id for obj in self.objects.values() if obj.type == "U" and obj.object_id not in named]
        if within is not None and left:  # never NOT IN (NULL): it matches nothing
            within += f" AND object_id NOT IN ({id_list(left)})"
        for source in id_sources(named, within):
            identity, size, *counts = result_sets(self.conn, batch(
                _IDENTITY.format(ids=source), _SIZES.format(ids=source), _TABLE_COUNTS.format(ids=source)))
            identities |= {int(object_id): (following, increment) for object_id, last, increment, seed, current
                           in identity if int(object_id) in named
                           and (following := next_id(last, increment, seed, current)) is not None}
            sizes |= {int(row[0]): tuple(row[1:]) for row in size if int(row[0]) in named}
            for what, found in zip(_TABLE_COUNTED, counts, strict=False):
                self.leave_out(what, int(found[0][0]))
            masked += int(counts[_MASKED][0][0])
        return found_columns, identities, sizes, masked

    def _types(self, type_rows: list[tuple], users: list[int]) -> dict[int, DumpObject]:
        """User types in user_type_id order (an alias type before the table types that use it): those chosen, and
        the ones the tables and the modules' parameters use. A CLR type is counted; a type from another schema that
        is used is named."""
        used: dict[int, str] = {}
        wanted = set(users)
        for source in id_sources(users, self.within("objects")):
            used |= {int(type_id): f"{schema}.{name}" for user, type_id, schema, name in rows(
                self.conn, _USED_TYPES.format(ids=source)) if int(user) in wanted}
        scope = {int(row[0]): row for row in type_rows}
        if missing := [label for type_id, label in used.items() if type_id not in scope]:
            self.leave_out("types from schemas not in the dump", len(missing))
            self.warnings.append(f"Types from schemas not in this dump, and needed where it is run: "
                                 f"{shown_names(missing)}")
        picked: dict[int, tuple[str, str]] = {}
        for type_id, schema, name, assembly in type_rows:
            if type_id not in used and (self.named or not self.kinds.types):
                if not self.named:
                    self.leave_out("types (not chosen)")
                continue
            if assembly:
                self.leave_out("CLR types")
                if type_id in used:
                    self.warnings.append(f"CLR type {schema}.{name} is used here and not dumped")
                continue
            picked[int(type_id)] = (schema, name)
        texts = types_of(self.conn, picked, self.within("types", self.kinds.types)) if self.ddl else {}
        found: dict[int, DumpObject] = {}
        for type_id, (schema, name) in picked.items():
            qname = f"{q(schema)}.{q(name)}"
            statements: tuple[str, ...] = ()
            if self.ddl:
                if (text := texts.get(type_id)) is None:
                    self.leave_out("types whose definition cannot be read")
                    continue
                statements = (text.rstrip(";"),)
            found[type_id] = DumpObject("type", f"{schema}.{name}", statements,
                                        f"IF TYPE_ID({literal(qname)}) IS NOT NULL DROP TYPE {qname}")
        return found

    def _module_texts(self, modules: list[int]) -> dict[int, tuple]:
        """(definition, ANSI_NULLS, QUOTED_IDENTIFIER) of each module; a plan reads only whether one is hidden."""
        definition = "definition" if self.ddl else "CASE WHEN definition IS NULL THEN NULL ELSE N'' END"
        wanted = set(modules)
        every = self.kinds.views and self.kinds.routines and self.kinds.triggers
        return {int(row[0]): tuple(row[1:]) for source in id_sources(modules, self.within("modules", every))
                for row in rows(self.conn, _MODULES.format(definition=definition, ids=source))
                if int(row[0]) in wanted}

    def _count_properties(self, schemas: dict[str, int], written: list[int], types: dict[int, DumpObject]) -> None:
        """Extended properties (MS_Description and the like) of what the dump writes (of the scope's objects, in a
        schema or database dump): none is written."""
        count = sum(int(rows(self.conn, _PROPERTIES.format(ids=source))[0][0])
                    for source in id_sources(written, self.within("objects")))
        schema_ids = id_list(schemas.values()) if self.scope.kind != "tables" else "NULL"
        count += int(rows(self.conn, _OTHER_PROPERTIES.format(types=id_list(types), schemas=schema_ids))[0][0])
        self.leave_out("extended properties (descriptions)", count)

    def _count_scope(self, schemas: dict[str, int]) -> None:
        counts = result_sets(self.conn, _SCOPE_COUNTS.format(schemas=id_list(schemas.values())))
        for i, (what, found) in enumerate(zip(_SCOPE_COUNTED, counts, strict=True)):
            if i == 0 or self.scope.kind == "database":
                self.leave_out(what, int(found[0][0]))

    # ---- objects ----

    def _schema(self, name: str) -> DumpObject:
        create = f"CREATE SCHEMA {q(name)}"
        return DumpObject("schema", name, (f"IF SCHEMA_ID({literal(name)}) IS NULL EXEC({literal(create)})",)
                          if self.ddl else ())

    def _sequences(self, sequences: list[Object]) -> list[DumpObject]:
        texts = sequences_at_next_value(self.conn, {obj.object_id: (obj.schema, obj.name) for obj in sequences},
                                        self.within("sequences", self.kinds.sequences)) if self.ddl else {}
        found = []
        for obj in sequences:
            statements: tuple[str, ...] = ()
            if self.ddl:
                if (text := texts.get(obj.object_id)) is None:
                    continue
                statements = (text.statement,)
                if text.exhausted:
                    self.warnings.append(f"Sequence {obj.label} has given its last value: the one written gives it "
                                         "once more")
            found.append(DumpObject("sequence", obj.label, statements,
                                    _drop(obj.qualified, "SO", f"DROP SEQUENCE {obj.qualified}")))
        return found

    def _tables_and_early(self, tables: list[Object], early: list[int], parts: dict[int, TableStatements],
                          texts: dict[int, tuple]) -> list[DumpObject]:
        """The tables and what they call, in one dependency order (creation order otherwise): a function before the
        table whose column calls it, a schema-bound one after the table and the view it reads."""
        keys = {obj.object_id for obj in tables} | set(early)
        found = []
        for key in self.graph.order([i for i in self.objects if i in keys]):
            obj = self.objects[key]
            if obj.type == "U":
                found.append(DumpObject("table", obj.label, (parts[obj.object_id].create,) if self.ddl else (),
                                        _drop(obj.qualified, "U", f"DROP TABLE {obj.qualified}")))
            elif (item := self._module(obj.object_id, texts)) is not None:
                found.append(item)
        return found

    def _module(self, object_id: int, texts: dict[int, tuple]) -> DumpObject | None:
        obj = self.objects[object_id]
        label, word = _MODULE_KINDS[obj.type]
        drop = _drop(obj.qualified, obj.type, f"DROP {word} {obj.qualified}")
        if obj.type == "SN":
            if self.ddl and object_id not in self.synonyms:  # dropped while the dump read the catalog
                return None
            return DumpObject(label, obj.label, (self.synonyms[object_id],) if self.ddl else (), drop)
        statements = self._definition(obj, texts)
        if statements is None:
            return None
        return DumpObject(label, obj.label, (*statements, *self.view_indexes.get(object_id, ())), drop)

    def _definition(self, obj: Object, texts: dict[int, tuple], parent: Object | None = None) -> tuple[str, ...] | None:
        """The module's statements; None when its definition went hidden after the catalog said it was readable."""
        definition, ansi_nulls, quoted_identifier = texts.get(obj.object_id, (None, True, True))
        if definition is None:
            self.leave_out(HIDDEN_MODULES)
            return None
        if not self.ddl:
            return ()
        definition, renamed = as_named(definition, obj.schema, obj.name,
                                       (parent.schema, parent.name) if parent is not None else None)
        if renamed:
            self.warnings.append(f"{obj.label} was renamed after it was created (sp_rename): its definition is written "
                                 "under the names it has now")
        return _module_statements(definition, ansi_nulls, quoted_identifier)

    def _identity(self, obj: Object, following: str, increment: str) -> DumpObject:
        return DumpObject("identity value", obj.label,
                          (_reseed(obj.qualified, following, increment),) if self.ddl else (), section="data")

    def _foreign_keys(self, tables: list[Object], parts: dict[int, TableStatements]) -> list[DumpObject]:
        dumped = {(obj.schema, obj.name) for obj in tables}
        found = []
        for obj in tables:
            for key in parts[obj.object_id].foreign_keys:
                schema, table = key.references or (None, None)
                if (schema, table) not in dumped:
                    self.warnings.append(f"A foreign key of {obj.label} is left out: it references {schema}.{table}, "
                                         "which is not in this dump")
                    self.leave_out("foreign keys to tables not in the dump")
                    continue
                name = f"{q(obj.schema)}.{q(key.name)}"
                found.append(DumpObject("foreign key", obj.label, key.statements if self.ddl else (),
                                        _drop(name, "F", f"ALTER TABLE {obj.qualified} DROP CONSTRAINT {q(key.name)}")))
        return found

    def _constraints(self, tables: list[Object], parts: dict[int, TableStatements]) -> list[DumpObject]:
        """The checks the tables do not enforce or trust, and the defaults and checks whose function reads a table,
        view or synonym: after the views and procedures, with every row in (review S1). Each has its DROP, which a
        `drop_first` file runs before those of the functions it calls (3729 otherwise)."""
        return [DumpObject(kind, obj.label, item.statements if self.ddl else (),
                           _drop(f"{q(obj.schema)}.{q(item.name)}", code,
                                 f"ALTER TABLE {obj.qualified} DROP CONSTRAINT {q(item.name)}"))
                for obj in tables for kind, code, items in (("check constraint", "C", parts[obj.object_id].checks),
                                                            ("default", "D", parts[obj.object_id].defaults))
                for item in items]

    def _policies(self, tables: set[int]) -> list[tuple[int, list[tuple]]]:
        """Security policies (row-level security) whose predicates are all on dumped tables: written, with the
        functions they call; one on a dumped table and another is counted and named (in a schema or database dump,
        any other). A filter predicate also means the dump holds only the rows this login may see."""
        candidates = [obj.object_id for obj in self.objects.values() if obj.type == "SP"
                      and obj.object_id not in self.hidden]
        predicates = by_object([row for chunk in chunks(candidates) for row in rows(self.conn, _POLICIES.format(
            ids=chunk))])
        found = []
        for policy in candidates:
            targets = {int(row[5]) for row in predicates.get(policy, [])}
            if not targets <= tables:
                if targets & tables or not self.named:
                    self.leave_out("security policies on tables not all in this dump")
                    self.warnings.append(f"Security policy {self.label(policy)} is left out: it filters tables not all "
                                         "in this dump")
                continue
            found.append((policy, predicates.get(policy, [])))
            for enabled, _, _, kind, _, target, _ in predicates.get(policy, []):
                if enabled and kind == "FILTER":
                    self.warnings.append(f"Row-level security ({self.label(policy)}) filters {self.label(int(target))}: "
                                         "the dump holds only the rows this login may see")
        return found

    def _policy(self, policy: int, predicates: list[tuple]) -> DumpObject:
        obj = self.objects[policy]
        statements: tuple[str, ...] = ()
        if self.ddl and predicates:
            enabled, schema_bound, not_for_replication = predicates[0][:3]
            adds = ",\n".join(f"ADD {kind} PREDICATE {unwrapped(definition)} ON {self.objects[int(target)].qualified}"
                              + (f" {operation}" if operation else "")
                              for _, _, _, kind, definition, target, operation in predicates)
            statements = (f"CREATE SECURITY POLICY {obj.qualified}\n{adds}\n"
                          f"WITH (STATE = {'ON' if enabled else 'OFF'}, SCHEMABINDING = {'ON' if schema_bound else 'OFF'})"
                          + (" NOT FOR REPLICATION" if not_for_replication else ""),)
        return DumpObject("security policy", obj.label, statements,
                          _drop(obj.qualified, "SP", f"DROP SECURITY POLICY {obj.qualified}"))

    def _triggers_of(self, parents: set[int]) -> list[int]:
        """The triggers of the dumped tables and views (a view's: INSTEAD OF, pre-flight H6), in creation order. A
        schema or database dump counts the others: those not chosen, and those of a table or view it leaves out
        (one left out for itself is counted for that)."""
        every = [obj for obj in self.objects.values() if obj.type == "TR" and obj.object_id not in self.hidden]
        found = [obj.object_id for obj in every if obj.parent in parents]
        if not self.named:
            self.leave_out("triggers of tables and views not in the dump", len(every) - len(found))
        if not self.kinds.triggers:
            if not self.named:
                self.leave_out("triggers (not chosen)", len(found))
            return []
        return found

    def _triggers(self, triggers: list[int], texts: dict[int, tuple]) -> list[DumpObject]:
        states: dict[int, bool] = {}
        orders: dict[int, list[tuple]] = {}
        for source in id_sources(triggers, self.within("objects")):
            disabled, firsts = result_sets(self.conn, _TRIGGERS.format(ids=source))
            states |= {int(object_id): bool(flag) for object_id, flag in disabled}
            for object_id, event, first in firsts:
                orders.setdefault(int(object_id), []).append((event, first))
        found = []
        for trigger in triggers:
            obj = self.objects[trigger]
            parent = self.objects[obj.parent]
            statements = self._definition(obj, texts, parent)
            if statements is None:
                continue
            if self.ddl:
                if states.get(trigger):
                    statements += (f"DISABLE TRIGGER {obj.qualified} ON {parent.qualified}",)
                statements += tuple(f"EXEC sys.sp_settriggerorder @triggername = {literal(obj.qualified)}, "
                                    f"@order = N'{'First' if first else 'Last'}', @stmttype = N'{event}'"
                                    for event, first in orders.get(trigger, []))
            found.append(DumpObject("trigger", obj.label, statements))
        return found

    def _data(self, obj: Object, columns: list[TableColumn], sizes: tuple | None, parts: TableStatements,
              by_name: dict[tuple[str, str], Object]) -> DumpTable:
        estimate, size = sizes if sizes is not None else (None, None)
        references = {by_name[key.references].label for key in parts.foreign_keys
                      if key.references in by_name and by_name[key.references].object_id != obj.object_id}
        return DumpTable(schema=obj.schema, table=obj.name, name=obj.label, columns=tuple(columns),
                         rows_estimate=int(estimate) if estimate is not None else None,
                         bytes_estimate=int(size) if size is not None else None, references=tuple(sorted(references)))


def dump_catalog(
    conn: driver.Connection, scope: DumpScope, kinds: DumpObjectKinds, server: str, database: str, *, ddl: bool = True,
) -> DumpCatalog:
    reader = _Reader(conn, scope, kinds, ddl)
    pre, tables, post = reader.read()
    consistency = "snapshot" if reader.snapshot else "per_table"
    where = f"database {database}" + (f" · schema {scope.schema_name}" if scope.schema_name else "")
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items())]
    if reader.collation:
        left_out.append(f"the database collation ({reader.collation}): columns that use it take the collation of the "
                        "database the file is run in")
    warnings = list(reader.warnings)
    if consistency == "per_table":
        warnings.insert(0, PER_TABLE_WARNING)
    logger.debug("SQL Server dump catalog of {}: {} objects before the data, {} tables, {} after", where, len(pre),
                 len(tables), len(post))
    return DumpCatalog(server=server, scope=where, consistency=consistency, pre=tuple(pre), tables=tuple(tables),
                       post=tuple(post), not_included=tuple(left_out), warnings=tuple(warnings),
                       snapshot_text=SNAPSHOT_TEXT if consistency == "snapshot" else PER_TABLE_TEXT)
