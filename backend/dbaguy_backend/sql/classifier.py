import threading
import time
from types import ModuleType

from loguru import logger

# The models live in the domain (the execution protocol carries them); re-exported here for the sql/ callers.
from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import Dialect
from .lexer import INSIGNIFICANT, TokenKind, tokenize
from .splitter import split

__all__ = ["Classification", "DangerReason", "StatementKind", "classify", "warm_up"]

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
    reasons: list[DangerReason] = []
    if first == "DROP":
        reasons.append(DangerReason.DROP)
    elif first == "TRUNCATE":
        reasons.append(DangerReason.TRUNCATE)
    elif first in ("UPDATE", "DELETE") and "WHERE" not in words:
        reasons.append(DangerReason.UPDATE_WITHOUT_WHERE if first == "UPDATE" else DangerReason.DELETE_WITHOUT_WHERE)
    return Classification(kind=kind, dangerous_reasons=reasons)


def _explain_inner(sql: str, dialect: Dialect) -> str | None:
    """For EXPLAIN ANALYZE ..., return the analysed statement text; None for plain EXPLAIN."""
    tokens = [t for t in tokenize(sql, dialect) if t.kind not in INSIGNIFICANT]
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


_rules_lock = threading.Lock()


def _sqlglot_rules() -> ModuleType:
    """The sqlglot half of the classifier, imported on first use (sqlglot is slow to import).

    One importer at a time: two threads importing sqlglot's interdependent modules at once (the start-up warm-up and
    a first request) could see one of them half-initialised.
    """
    with _rules_lock:
        from . import sqlglot_rules
    return sqlglot_rules


def _from_sqlglot(sql: str, dialect: Dialect) -> list[Classification | None] | None:
    """One classification per statement sqlglot finds in `sql` (None where the syntax tree says nothing useful, e.g.
    a Command); None when sqlglot cannot parse `sql`, so the keyword rules decide instead.

    Logs the exception type only: sqlglot's messages quote the statement.
    """
    try:
        rules = _sqlglot_rules()
    except Exception as exc:  # e.g. an app bundle without sqlglot: a packaging bug, not a statement problem
        logger.warning("sqlglot is unavailable ({}); classifying by keywords only", type(exc).__name__)
        return None
    try:
        return [rules.classify_expression(node) for node in rules.parse(sql, dialect)]
    except rules.EXPECTED_FAILURES as exc:
        logger.debug("sqlglot could not parse the statement ({}); using the keyword rules", type(exc).__name__)
    except Exception as exc:  # e.g. a dialect missing from an app bundle, or a sqlglot bug
        logger.warning("sqlglot failed on a statement ({}); using the keyword rules", type(exc).__name__)
    return None


def _classify_statement(sql: str, dialect: Dialect) -> Classification:
    while (inner := _explain_inner(sql, dialect)) is not None:  # EXPLAIN ANALYZE runs the statement it explains
        sql = inner
    found = _from_sqlglot(sql, dialect)
    if found is not None and len(found) == 1 and found[0] is not None:
        return found[0]
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


def warm_up() -> None:
    """Import sqlglot and parse one statement per dialect, so the first real classification does not pay for it."""
    started = time.perf_counter()
    for dialect in Dialect:
        _from_sqlglot("SELECT 1", dialect)
    logger.debug("SQL classifier warmed up in {:.0f} ms", (time.perf_counter() - started) * 1000)
