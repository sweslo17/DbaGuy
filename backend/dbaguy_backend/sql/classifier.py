import logging
from enum import StrEnum

import sqlglot
from loguru import logger
from pydantic import BaseModel
from sqlglot import exp

from .dialects import SQLGLOT_NAME, Dialect
from .lexer import TokenKind, tokenize
from .splitter import split

# sqlglot warns "'<sql text>' contains unsupported syntax" whenever it falls back to a Command, which is routine here
# (CALL, LOCK, REPLACE INTO, ...) and would put statement text into the log above DEBUG.
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


class StatementKind(StrEnum):
    READ = "read"
    WRITE = "write"
    DDL = "ddl"
    TRANSACTION = "transaction"
    SESSION = "session"
    UNKNOWN = "unknown"


class Classification(BaseModel):
    kind: StatementKind
    dangerous_reasons: list[str] = []


_KEYWORD_KIND: dict[str, StatementKind] = {
    **dict.fromkeys(
        ("SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "FETCH"), StatementKind.READ
    ),
    **dict.fromkeys(
        ("INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "REPLACE", "UPSERT", "CALL", "DO", "LOCK"), StatementKind.WRITE
    ),
    **dict.fromkeys(
        ("CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "GRANT", "REVOKE", "REINDEX", "VACUUM", "ANALYZE",
         "CLUSTER", "REFRESH", "SECURITY", "RENAME", "IMPORT"),
        StatementKind.DDL,
    ),
    **dict.fromkeys(
        ("BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END", "ABORT"), StatementKind.TRANSACTION
    ),
    **dict.fromkeys(
        ("SET", "RESET", "USE", "ATTACH", "DETACH", "LISTEN", "UNLISTEN", "NOTIFY", "DISCARD", "DEALLOCATE"),
        StatementKind.SESSION,
    ),
}
_DML = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
# Least to most cautious: when a script mixes kinds, the strictest one decides (unknown is the most cautious).
_SEVERITY = (
    StatementKind.READ,
    StatementKind.TRANSACTION,
    StatementKind.SESSION,
    StatementKind.WRITE,
    StatementKind.DDL,
    StatementKind.UNKNOWN,
)
_ANALYZE = ("ANALYZE", "ANALYSE")  # PostgreSQL accepts both spellings; EXPLAIN ANALYZE really executes the statement


def _lexical(sql: str, dialect: Dialect) -> Classification:
    tokens = tokenize(sql, dialect)
    words = [t.upper for t in tokens if t.kind == TokenKind.WORD]
    if not words:
        return Classification(kind=StatementKind.UNKNOWN)
    first = words[0]
    if first == "PRAGMA":  # `PRAGMA x = y` sets, `PRAGMA x` reads (an "=" inside a string or comment does not count)
        assigns = any(t.kind == TokenKind.OTHER and t.text == "=" for t in tokens)
        return Classification(kind=StatementKind.SESSION if assigns else StatementKind.READ)
    kind = _KEYWORD_KIND.get(first, StatementKind.UNKNOWN)
    if kind == StatementKind.READ and first in ("SELECT", "WITH"):
        # Data-modifying CTEs and locking reads are writes even when sqlglot could not parse them.
        has_dml = any(w in ("INSERT", "UPDATE", "DELETE", "MERGE") for w in words[1:])
        locks = any(a == "FOR" and b in ("UPDATE", "SHARE", "NO", "KEY") for a, b in zip(words, words[1:]))
        if has_dml or locks:
            kind = StatementKind.WRITE
    reasons: list[str] = []
    if first == "DROP":
        reasons.append("drop")
    elif first == "TRUNCATE":
        reasons.append("truncate")
    elif first in ("UPDATE", "DELETE") and "WHERE" not in words:
        reasons.append("update_without_where" if first == "UPDATE" else "delete_without_where")
    return Classification(kind=kind, dangerous_reasons=reasons)


def _explain_inner(sql: str, dialect: Dialect) -> str | None:
    """For EXPLAIN ANALYZE ..., return the analysed statement text; None for plain EXPLAIN."""
    tokens = [t for t in tokenize(sql, dialect) if t.kind not in (TokenKind.SPACE, TokenKind.COMMENT)]
    if not tokens or tokens[0].upper != "EXPLAIN":
        return None
    i, analyze = 1, False
    if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:  # EXPLAIN (ANALYZE, BUFFERS) ...
        depth = 0
        while i < len(tokens):
            if tokens[i].kind == TokenKind.LPAREN:
                depth += 1
            elif tokens[i].kind == TokenKind.RPAREN:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            elif tokens[i].upper in _ANALYZE:
                analyze = True
            i += 1
    while i < len(tokens) and tokens[i].upper in (*_ANALYZE, "VERBOSE", "QUERY", "PLAN"):
        analyze = analyze or tokens[i].upper in _ANALYZE
        i += 1
    if not analyze or i >= len(tokens):
        return None
    return sql[tokens[i].start:]


def _unfiltered_dml(node: exp.Expression) -> list[str]:
    """Dangerous reasons for every UPDATE/DELETE without WHERE in `node`, including ones inside CTEs.

    The UPDATE action of MERGE ... WHEN MATCHED is modelled as an exp.Update without WHERE; it is not a statement.
    """
    reasons: list[str] = []
    for child in node.walk():
        if isinstance(child.parent, exp.When):
            continue
        if isinstance(child, exp.Update) and not child.args.get("where"):
            reasons.append("update_without_where")
        elif isinstance(child, exp.Delete) and not child.args.get("where"):
            reasons.append("delete_without_where")
    return list(dict.fromkeys(reasons))


def _from_expression(node: exp.Expression) -> Classification | None:
    if isinstance(node, exp.Drop):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["drop"])
    if isinstance(node, exp.TruncateTable):
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["truncate"])
    if isinstance(node, exp.Alter):
        drops = any(isinstance(child, exp.Drop) for child in node.walk() if child is not node)
        return Classification(kind=StatementKind.DDL, dangerous_reasons=["alter_drop"] if drops else [])
    if isinstance(node, exp.Create):
        return Classification(kind=StatementKind.DDL)
    if isinstance(node, _DML):
        return Classification(kind=StatementKind.WRITE, dangerous_reasons=_unfiltered_dml(node))
    if isinstance(node, (exp.Transaction, exp.Commit, exp.Rollback)):
        return Classification(kind=StatementKind.TRANSACTION)
    if isinstance(node, (exp.Set, exp.Use)):
        return Classification(kind=StatementKind.SESSION)
    if isinstance(node, exp.Query):
        if node.find(exp.Into):  # SELECT ... INTO creates a table
            return Classification(kind=StatementKind.DDL)
        writes = any(isinstance(child, _DML) for child in node.walk())
        locks = node.find(exp.Lock) is not None  # FOR UPDATE/SHARE, also inside a CTE or subquery
        if writes or locks:
            return Classification(kind=StatementKind.WRITE, dangerous_reasons=_unfiltered_dml(node))
        return Classification(kind=StatementKind.READ)
    return None


def _parse_single(sql: str, dialect: Dialect) -> exp.Expression | None:
    """The one statement sqlglot understands in `sql`; None when it is empty, several, a Command or unparseable."""
    try:
        parsed = [e for e in sqlglot.parse(sql, read=SQLGLOT_NAME[dialect]) if e is not None]
    except Exception as exc:  # ParseError, TokenError, RecursionError on absurd nesting, a sqlglot bug: all fall back
        logger.debug("sqlglot could not parse the statement ({}); using the lexical fallback", type(exc).__name__)
        return None
    if len(parsed) != 1 or isinstance(parsed[0], exp.Command):
        return None
    return parsed[0]


def _classify_statement(sql: str, dialect: Dialect) -> Classification:
    while (inner := _explain_inner(sql, dialect)) is not None:  # EXPLAIN ANALYZE runs the statement it explains
        sql = inner
    node = _parse_single(sql, dialect)
    if node is not None and (result := _from_expression(node)) is not None:
        return result
    return _lexical(sql, dialect)


def _combine(results: list[Classification]) -> Classification:
    return Classification(
        kind=max((r.kind for r in results), key=_SEVERITY.index),
        dangerous_reasons=list(dict.fromkeys(reason for r in results for reason in r.dangerous_reasons)),
    )


def classify(sql: str, dialect: Dialect) -> Classification:
    """Kind of the statement plus the reasons it is dangerous. Never raises; unknown syntax is `unknown`.

    Text holding several statements is classified statement by statement and combined (strictest kind, union of
    reasons), so a dangerous statement cannot hide behind a harmless first one.
    """
    spans = split(sql, dialect)
    if len(spans) > 1:
        return _combine([_classify_statement(span.text, dialect) for span in spans])
    return _classify_statement(sql, dialect)
