"""What grid editing needs to know about a SQL Server table (spec ⑤ §3.2.2): user tables only, each column's kind and
whether it can be written and compared exactly, whether its text is Unicode (N'…' only where needed, A8), and the key.
One batch on the metadata connection (READ UNCOMMITTED, LOCK_TIMEOUT 5000: a table locked by an uncommitted DDL
answers LOCKED_MESSAGE, CLAUDE.md); names come from catalog-view joins, never COL_NAME / OBJECT_NAME."""

from ...domain.edits import EditColumn, EditTarget, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import driver
from .catalog import batch, object_id_sql, result_sets, type_sql
from .types import category_for_type

_OBJECT = "SELECT type FROM sys.objects WHERE object_id = {o}"
# sys.tables' temporal_type (2016), is_node / is_edge (2017) and ledger_type (2022), read through dynamic SQL so that an
# older server answers 0 instead of failing the whole batch with 207 (invalid column name)
_FLAGS = """
DECLARE @o int = {o}, @q nvarchar(max) = N'SELECT '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'temporal_type') IS NULL THEN N'0' ELSE N'temporal_type' END + N', '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'is_node') IS NULL THEN N'0, 0' ELSE N'is_node, is_edge' END + N', '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'ledger_type') IS NULL THEN N'0' ELSE N'ledger_type' END
  + N' FROM sys.tables WHERE object_id = @o';
EXEC sys.sp_executesql @q, N'@o int', @o = @o
"""
HISTORY_TABLE = "History table of a system-versioned table: read-only here"
GRAPH_TABLE = "Graph node and edge tables are read-only here"
LEDGER_TABLE = "Ledger tables are read-only here"
# An alias type is read through its base type; a CLR type (hierarchyid, geometry, geography) has no base row
_COLUMNS = """
SELECT c.name, t.name, COALESCE(st.name, t.name), t.is_assembly_type, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END,
       c.generated_always_type
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id AND st.is_user_defined = 0
WHERE c.object_id = {o} ORDER BY c.column_id
"""
# Key columns only (not INCLUDE ones); no filtered, disabled or hypothetical index (a Tuning Advisor leftover: unique
# in the catalog, but it enforces nothing)
_KEYS = """
SELECT i.name, i.is_primary_key, c.name
FROM sys.indexes i
JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
JOIN sys.columns c ON c.object_id = k.object_id AND c.column_id = k.column_id
WHERE i.object_id = {o} AND (i.is_primary_key = 1 OR i.is_unique = 1) AND i.has_filter = 0 AND i.is_disabled = 0
  AND i.is_hypothetical = 0 AND k.is_included_column = 0 AND k.key_ordinal > 0
ORDER BY i.name, k.key_ordinal
"""
# 402, sql_variant: 0 rows; json and vector (2025) have no = (G9)
_NOT_EXACT = frozenset({"float", "real", "text", "ntext", "image", "xml", "sql_variant", "json", "vector"})
_FRACTIONS = frozenset({"datetime2", "datetimeoffset", "time"})  # the driver reads microseconds: scale 7 never matches
_NON_UNICODE = frozenset({"char", "varchar", "text"})


def _column(
    name: str, type_name: str, base: str, assembly: bool, max_length: int, precision: int, scale: int,
    nullable: bool, identity: bool, computed: bool, default: int, generated_always: int,
) -> EditColumn:
    if computed:
        reason: str | None = "computed column"
    elif identity:
        reason = "IDENTITY column"
    elif base == "timestamp":
        reason = "rowversion column"
    elif generated_always:
        reason = "period column (system-versioned)"
    elif base == "sql_variant" or assembly:
        reason = f"{type_name} values are not edited here"
    else:
        reason = None
    exact = not (base in _NOT_EXACT or assembly or (base in _FRACTIONS and scale > 6))
    return EditColumn(
        name=name, type_name=type_sql(type_name, max_length, precision, scale),
        kind=edit_kind(category_for_type(base)), nullable=bool(nullable),
        has_default=bool(default or identity or computed or generated_always) or base == "timestamp",
        writable=reason is None, read_only_reason=reason, compare="equals" if exact else "none",
        unicode=base not in _NON_UNICODE,
    )


def table_flags_reason(temporal_type: int, is_node: bool, is_edge: bool, ledger_type: int) -> str | None:
    """Why a user table is read-only here, from its sys.tables flags (G9); None when nothing stops editing. A history
    table is temporal_type 1; the current table of a system-versioned pair (2) stays editable, with its period columns
    read-only."""
    if temporal_type == 1:
        return HISTORY_TABLE
    if is_node or is_edge:
        return GRAPH_TABLE
    if ledger_type:
        return LEDGER_TABLE
    return None


def edit_target(conn: driver.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, flags, columns, keys = result_sets(
        conn, batch(_OBJECT.format(o=o), _FLAGS.format(o=o), _COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
    if object_type != "U":
        return read_only_relation(path, VIEWS if object_type == "V" else "This object is read-only here")
    if reason := table_flags_reason(*(flags[0] if flags else (0, False, False, 0))):
        return read_only_relation(path, reason)
    parts: dict[str, tuple[bool, list[str]]] = {}
    for index, primary, column in keys:
        parts.setdefault(index, (bool(primary), []))[1].append(column)
    candidates = [KeyCandidate(index, tuple(cols), primary) for index, (primary, cols) in parts.items()]
    return build_target(path, [_column(*row) for row in columns], candidates)
