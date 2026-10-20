"""What grid editing needs to know about a SQL Server table (spec ⑤ §3.2.2): user tables only, each column's kind and
whether it can be written and compared exactly, whether its text is Unicode (N'…' only where needed, A8), and the key.
One batch on the metadata connection (READ UNCOMMITTED, LOCK_TIMEOUT 5000: a table locked by an uncommitted DDL
answers LOCKED_MESSAGE, CLAUDE.md); names come from catalog-view joins, never COL_NAME / OBJECT_NAME."""

from ...domain.edits import EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import driver
from .catalog import batch, object_id_sql, result_sets
from .columns import COLUMNS, column as table_column

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
        conn, batch(_OBJECT.format(o=o), _FLAGS.format(o=o), COLUMNS.format(o=o), _KEYS.format(o=o)))
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
    return build_target(path, [table_column(*row).edit_column() for row in columns], candidates)
