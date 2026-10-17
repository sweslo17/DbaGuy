"""The classifier's sqlglot half: parse a statement and read its kind and dangers from the syntax tree.

Importing sqlglot takes about 190 ms, so `classifier` imports this module on first use (the app warms it up in the
background). The sqlglot dialects are imported here by name on purpose: sqlglot itself looks a dialect name up with
`importlib.import_module`, which PyInstaller cannot follow, so a packaged app would miss them and silently fall back
to the keyword rules.
"""

import logging

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect
from sqlglot.dialects.mysql import MySQL
from sqlglot.dialects.postgres import Postgres
from sqlglot.dialects.sqlite import SQLite
from sqlglot.errors import ParseError, TokenError

from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import Dialect

# sqlglot warns "'<sql text>' contains unsupported syntax" whenever it falls back to a Command, which is routine here
# (CALL, LOCK, REPLACE INTO, ...) and would put statement text into the log above DEBUG.
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

SQLGLOT_DIALECT: dict[Dialect, type[SqlglotDialect]] = {
    Dialect.POSTGRES: Postgres, Dialect.SQLITE: SQLite, Dialect.MYSQL: MySQL,
}
# Text sqlglot does not understand (RecursionError: absurd nesting). Anything else is unexpected, e.g. a sqlglot bug.
EXPECTED_FAILURES = (ParseError, TokenError, RecursionError)
_DML = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
# What an ALTER can drop: a column, index, key or constraint is an exp.Drop; a primary key and a partition have their
# own nodes (R-03a-F1).
_ALTER_DROPS = (exp.Drop, exp.DropPrimaryKey, exp.DropPartition)
# SELECT … INTO <table>; MySQL's INTO only fills variables or writes a file
INTO_CREATES_A_TABLE: frozenset[Dialect] = frozenset({Dialect.POSTGRES, Dialect.SQLITE})


def parse(sql: str, dialect: Dialect) -> list[exp.Expression]:
    """Every statement sqlglot finds in `sql` (empty ones left out). Raises when it cannot parse it."""
    return [e for e in sqlglot.parse(sql, read=SQLGLOT_DIALECT[dialect]) if e is not None]


def _unfiltered_dml(node: exp.Expression) -> list[DangerReason]:
    """Dangerous reasons for every UPDATE/DELETE without WHERE in `node`, including ones inside CTEs.

    The UPDATE action of MERGE ... WHEN MATCHED is modelled as an exp.Update without WHERE; it is not a statement.
    """
    reasons: list[DangerReason] = []
    for child in node.walk():
        if isinstance(child.parent, exp.When):
            continue
        if isinstance(child, exp.Update) and not child.args.get("where"):
            reasons.append(DangerReason.UPDATE_WITHOUT_WHERE)
        elif isinstance(child, exp.Delete) and not child.args.get("where"):
            reasons.append(DangerReason.DELETE_WITHOUT_WHERE)
    return list(dict.fromkeys(reasons))


def classify_expression(node: exp.Expression, dialect: Dialect) -> Classification | None:
    """The classification of one parsed statement; None when the tree says nothing useful (e.g. a Command)."""
    if isinstance(node, exp.Drop):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=[DangerReason.DROP])
    if isinstance(node, exp.TruncateTable):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=[DangerReason.TRUNCATE])
    if isinstance(node, exp.Alter):
        drops = any(isinstance(child, _ALTER_DROPS) for child in node.walk() if child is not node)
        return Classification(kind=StatementKind.DDL, dangerous_reasons=[DangerReason.ALTER_DROP] if drops else [])
    if isinstance(node, exp.Create):
        return Classification(kind=StatementKind.DDL)
    if isinstance(node, _DML):
        return Classification(kind=StatementKind.WRITE, dangerous_reasons=_unfiltered_dml(node))
    if isinstance(node, (exp.Transaction, exp.Commit, exp.Rollback)):
        return Classification(kind=StatementKind.TRANSACTION)
    if isinstance(node, (exp.Set, exp.Use)):
        # SET @x = (SELECT … FOR UPDATE) takes row locks, like any locking read
        locks = node.find(exp.Lock) is not None
        return Classification(kind=StatementKind.WRITE if locks else StatementKind.SESSION)
    if isinstance(node, exp.Query):
        if node.find(exp.Into) and dialect in INTO_CREATES_A_TABLE:
            return Classification(kind=StatementKind.DDL)
        writes = any(isinstance(child, _DML) for child in node.walk())
        locks = node.find(exp.Lock) is not None  # FOR UPDATE/SHARE, also inside a CTE or subquery
        if writes or locks:
            return Classification(kind=StatementKind.WRITE, dangerous_reasons=_unfiltered_dml(node))
        return Classification(kind=StatementKind.READ)
    return None
