"""What grid editing needs to know about a PostgreSQL table (spec ⑤ §3.2.2): its kind, how each column is written and
compared, and the key a row is identified by. Three catalog queries on the metadata connection (native loaders); the
first also reads the session's standard_conforming_strings, which the literals of sql/dialects.py assume on (G3).

An editable table's target also says whether its tab may browse it ordered by the key (`stable_order`, 05b K13): the
unsorted order is the heap's, where an UPDATE moves the row, so the row just edited leaves its place on the re-read. The
browse's own SELECT with that ORDER BY is planned (EXPLAIN, never run): an ordered index scan streams the first page at
once, but a Sort waits for the whole table first (minutes on one larger than effective_cache_size), so only a plan with
no Sort, or a sort of few rows (a small table, which the planner sorts because that is cheapest), gives the order."""

import psycopg
from loguru import logger
from psycopg import pq

from ...domain.edits import EditTarget
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import SortKey, build_browse_select, relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .columns import read_columns
from .introspection import relation_oid

# standard_conforming_strings rides along (no round trip of its own): off, a backslash in '…' escapes (ruling G3)
_RELKIND = "SELECT relkind::text, current_setting('standard_conforming_strings') FROM pg_class WHERE oid = %(oid)s"
# Key columns only (INCLUDE columns come after indnkeyatts); no partial, expression or invalid index.
_KEYS = """
SELECT ic.relname::text, i.indisprimary, array_agg(a.attname::text ORDER BY k.ord)
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, ord)
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %(oid)s AND i.indisvalid AND k.ord <= i.indnkeyatts
  AND (i.indisprimary OR (i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL))
GROUP BY ic.relname, i.indisprimary
"""
_STRINGS_NOT_STANDARD = "standard_conforming_strings is off: values with backslashes would be changed"
_READ_ONLY = {"v": VIEWS, "m": "Materialized views are read-only here", "f": "Foreign tables are read-only here"}
# A plan that sorts at most this many rows (the planner's estimate) still starts the first page at once: a small table
_SMALL_SORT_ROWS = 10_000
_SORTS = frozenset({"Sort", "Incremental Sort"})
# The planning, and a wait for the table's lock (an uncommitted ALTER in another session), never hold the metadata
# connection longer than this: the tab waits at most 2 s for the target anyway, then browses unsorted. Both limits, in
# a transaction block of their own so that SET LOCAL holds on every version: an older server (before 13) arms
# statement_timeout once for the whole message, before the SET LOCAL ahead of the EXPLAIN; lock_timeout is read when
# the lock wait starts, on every version.
_PLAN_TIMEOUT_MS = 1_000
_PLAN_LIMITS = f"SET LOCAL statement_timeout = {_PLAN_TIMEOUT_MS}; SET LOCAL lock_timeout = {_PLAN_TIMEOUT_MS}"


def edit_target(conn: psycopg.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    oid = relation_oid(conn, qualified(Dialect.POSTGRES, schema, name))
    row = conn.execute(_RELKIND, {"oid": oid}).fetchone()
    relkind, standard_strings = row if row is not None else ("", None)
    if relkind not in ("r", "p"):
        return read_only_relation(path, _READ_ONLY.get(relkind, "This object is read-only here"))
    if standard_strings != "on":  # fail closed: only the setting the literals are written for
        return read_only_relation(path, _STRINGS_NOT_STANDARD)
    read = read_columns(conn, oid)
    composite = {column.name for column in read if column.family == "composite"}  # `=` on it is 0A000 (ruling E4)
    candidates = [
        KeyCandidate(index, tuple(columns), primary)
        for index, primary, columns in conn.execute(_KEYS, {"oid": oid})
        if not composite.intersection(columns)
    ]
    target = build_target(path, [column.edit_column() for column in read], candidates)
    if not target.editable:
        return target
    return target.model_copy(update={"stable_order": _stable_order(conn, path, target.key)})


def ordered_plan(explained: object) -> bool:
    """Whether a plan (the value of EXPLAIN (FORMAT JSON)) gives its first rows at once in the order asked for: no Sort
    or Incremental Sort node anywhere in its tree, or none that sorts more than _SMALL_SORT_ROWS (the root's estimate:
    every sort below it sorts a part of those rows). An ordered index scan, a Merge Append or Gather Merge over ordered
    scans all qualify. Pure; a value of any other shape is False."""
    if not (isinstance(explained, list) and explained and isinstance(explained[0], dict)):
        return False
    root = explained[0].get("Plan")
    if not isinstance(root, dict):
        return False
    pending: list[object] = [root]
    sorts = False
    while pending:  # iterative: however deep a plan nests
        node = pending.pop()
        children = node.get("Plans", []) if isinstance(node, dict) else None
        if not isinstance(children, list):
            return False
        sorts = sorts or node.get("Node Type") in _SORTS
        pending.extend(children)
    rows = root.get("Plan Rows")
    return not sorts or (type(rows) in (int, float) and rows <= _SMALL_SORT_ROWS)


def _stable_order(conn: psycopg.Connection, path: list[PathSegment], key: list[str]) -> list[str] | None:
    """`key` when the browse ordered by it plans as `ordered_plan` wants, else None. Planned only on an auto-commit
    connection between statements: inside a transaction a failed EXPLAIN would abort it, and without auto-commit
    psycopg would open one first and leave it open, holding the table's lock with the limits still set. Any failure is
    None, logged with its code only."""
    if not conn.autocommit or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        return None
    select = build_browse_select(Dialect.POSTGRES, path, None, [SortKey(column=column) for column in key])
    # One round trip (the simple protocol takes the statements as one message). The limits last until the block ends:
    # COMMIT once planned; after an error or a timeout the server skips the rest and leaves the block aborted, and the
    # ROLLBACK ends it, so nothing of them (or of the table's lock) outlives the plan either way.
    try:
        cursor = conn.execute(f"BEGIN; {_PLAN_LIMITS}; EXPLAIN (FORMAT JSON) {select}; COMMIT")
        while cursor.description is None and cursor.nextset():
            pass  # past BEGIN and the SETs: the EXPLAIN is the one statement here that returns rows
        row = cursor.fetchone() if cursor.description is not None else None
    except psycopg.Error as exc:
        logger.debug("Stable order not planned: {}", exc.sqlstate or type(exc).__name__)
        _end_block(conn)
        return None
    return list(key) if row is not None and ordered_plan(row[0]) else None


def _end_block(conn: psycopg.Connection) -> None:
    """Rolls back the plan's block left open by its failure; a connection that is gone has nothing to roll back."""
    if conn.info.transaction_status == pq.TransactionStatus.IDLE:
        return
    try:
        conn.execute("ROLLBACK")
    except psycopg.Error as exc:
        logger.debug("Stable order's block not rolled back: {}", exc.sqlstate or type(exc).__name__)
