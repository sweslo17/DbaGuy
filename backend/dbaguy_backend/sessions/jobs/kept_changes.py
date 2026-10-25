"""MySQL: changes a ROLLBACK cannot undo (a table whose engine is not transactional: MyISAM, MEMORY, …; an engine the
server does not list counts as one). Run SQL file and Copy data both ask the server the same two things (DRY): whether
the last statement's warnings say a ROLLBACK left changes (1196, ruling R3), and whether one table's engine can roll
back now (a copy's table: its CREATE TABLE took the server's default engine, or the table changed since the preview,
06c Task 4 review I-1). Each is one statement through the job's own path, on the worker thread."""

from loguru import logger

from ...adapters.base import StatementError
from ...domain.data_jobs import MAX_JOB_ERRORS
from ...sql.dialects import Dialect, quote_literal
from .context import JobContext

KEPT = "Some tables cannot roll back (non-transactional): their changes stayed"  # ruling R3
SHOW_WARNINGS = "SHOW WARNINGS"
NOT_ROLLED_BACK = "1196"  # MySQL: "Some non-transactional changed tables couldn't be rolled back"


def table_rolls_back_sql(schema: str | None, name: str) -> str:
    """Whether a table's engine can roll back: one row, YES or NO (none: no such table). `schema` None: the session's
    database."""
    where = quote_literal(Dialect.MYSQL, schema) if schema else "DATABASE()"
    return ("SELECT COALESCE(e.TRANSACTIONS, 'NO') FROM information_schema.TABLES t "
            "LEFT JOIN information_schema.ENGINES e ON e.ENGINE = t.ENGINE "
            f"WHERE t.TABLE_SCHEMA = {where} AND t.TABLE_NAME = {quote_literal(Dialect.MYSQL, name)}")


def kept_changes(ctx: JobContext, what: str) -> bool:
    """MySQL: the warnings of the last statement (a ROLLBACK, or a deadlock's victim) hold 1196. When they cannot be
    read: False (logged with its code; `what` names the job in the log)."""
    try:
        result = ctx.run(SHOW_WARNINGS)
        try:
            rows = result.fetch(MAX_JOB_ERRORS)
        finally:
            ctx.close(result)
    except StatementError as exc:
        logger.info("[{}] the warnings of the rollback could not be read ({})", what, exc.code)
        return False
    return any(len(row) > 1 and str(row[1]) == NOT_ROLLED_BACK for row in rows)


def table_rolls_back(ctx: JobContext, schema: str | None, name: str) -> bool | None:
    """MySQL: whether the table's engine can roll back, as the server says now; None when it finds no such table. A
    failure to read (a Cancel included) is the caller's, as any of its statements."""
    result = ctx.run(table_rolls_back_sql(schema, name))
    try:
        rows = result.fetch(1)
        while result.fetch(1):
            pass
    finally:
        ctx.close(result)
    return str(rows[0][0]).upper() == "YES" if rows else None
