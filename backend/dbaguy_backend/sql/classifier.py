import threading
import time
from types import ModuleType

from loguru import logger

# The models live in the domain (the execution protocol carries them); re-exported here for the sql/ callers.
from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import Dialect
from .lexer import HINT_COMMENT_START, INSIGNIFICANT, VERSION_COMMENT_STARTS, Token, TokenKind, tokenize
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
# Keywords whose meaning differs by dialect, consulted before _KEYWORD_KIND (spec §3.4). MySQL's RESET is never a
# session setting: RESET MASTER, RESET REPLICA, RESET PERSIST act on the server (sqlglot reads them as an alias).
_DIALECT_KEYWORD_KIND: dict[Dialect, dict[str, StatementKind]] = {
    Dialect.MYSQL: {"LOAD": StatementKind.WRITE, "HANDLER": StatementKind.WRITE, "RESET": StatementKind.UNKNOWN},
}
# MySQL: sqlglot reads every `START …` as a transaction, and so does _KEYWORD_KIND (START REPLICA, START
# GROUP_REPLICATION, …). A statement counts as a TRANSACTION only when it begins with one of these; anything else is
# UNKNOWN (R-03a-T4). A dialect without an entry (PostgreSQL, SQLite) has no restriction.
_TRANSACTION_STARTS: dict[Dialect, tuple[tuple[str, ...], ...]] = {
    Dialect.MYSQL: (
        ("BEGIN",), ("START", "TRANSACTION"), ("COMMIT",), ("ROLLBACK",), ("SAVEPOINT",), ("RELEASE", "SAVEPOINT"),
    ),
}
# The keywords that open an EXPLAIN. In MySQL DESCRIBE and DESC are synonyms: `DESCRIBE ANALYZE DELETE …` runs the
# DELETE, while `DESC t` and `DESCRIBE t` only list the columns (no ANALYZE follows).
_EXPLAIN_KEYWORDS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("EXPLAIN",),
    Dialect.SQLITE: ("EXPLAIN",),
    Dialect.MYSQL: ("EXPLAIN", "DESCRIBE", "DESC"),
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
# What follows DROP inside an ALTER when it loses rows or the keys and constraints that guard them (R-03a-F1).
# `ALTER COLUMN a DROP DEFAULT|NOT NULL|EXPRESSION|IDENTITY` loses neither.
_ALTER_DROPPED = frozenset({"PARTITION", "PRIMARY", "COLUMN", "INDEX", "KEY", "FOREIGN", "CONSTRAINT", "CHECK"})


def _has_version_comment(tokens: list[Token]) -> bool:
    """MySQL runs the text of `/*! … */` as code (MariaDB that of `/*M! … */` too), so nothing in it can be judged
    (spec §3.4). `/*+ … */` holds only optimizer hints, which never change what a statement does."""
    return any(t.kind == TokenKind.EXEC_COMMENT and t.text.startswith(VERSION_COMMENT_STARTS) for t in tokens)


def _is_hint(token: Token) -> bool:
    """A MySQL optimizer hint `/*+ … */`: for the server a plain comment wherever it does not follow SELECT, INSERT,
    UPDATE or DELETE, such as between the words of EXPLAIN."""
    return token.kind == TokenKind.EXEC_COMMENT and token.text.startswith(HINT_COMMENT_START)


def _words(tokens: list[Token]) -> list[str]:
    return [t.upper for t in tokens if t.kind == TokenKind.WORD]


def _takes_row_locks(words: list[str]) -> bool:
    """FOR UPDATE, FOR SHARE, FOR NO KEY UPDATE, FOR KEY SHARE, or MySQL's LOCK IN SHARE MODE."""
    return any(
        (a == "FOR" and b in ("UPDATE", "SHARE", "NO", "KEY")) or (a, b) == ("LOCK", "IN")
        for a, b in zip(words, words[1:])
    )


def _begins_a_transaction_statement(words: list[str], dialect: Dialect) -> bool:
    starts = _TRANSACTION_STARTS.get(dialect)
    return starts is None or any(tuple(words[:len(start)]) == start for start in starts)


def _writes_server_file(words: list[str]) -> bool:
    """SELECT … INTO OUTFILE|DUMPFILE writes a file on the database server, which a READ ONLY session allows."""
    return any(a == "INTO" and b in ("OUTFILE", "DUMPFILE") for a, b in zip(words, words[1:]))


def _alter_reasons(words: list[str]) -> list[DangerReason]:
    """ALTER … DROP COLUMN|INDEX|KEY|PRIMARY KEY|FOREIGN KEY|CONSTRAINT|CHECK|PARTITION and ALTER … TRUNCATE PARTITION:
    as destructive as DROP and TRUNCATE (MySQL purges partitioned tables this way), so confirmed alike (R-03a-F1)."""
    if words[:1] != ["ALTER"]:
        return []
    pairs = set(zip(words, words[1:]))
    reasons = [DangerReason.ALTER_DROP] if any(a == "DROP" and b in _ALTER_DROPPED for a, b in pairs) else []
    if ("TRUNCATE", "PARTITION") in pairs:
        reasons.append(DangerReason.TRUNCATE)
    return reasons


def _lexical(sql: str, dialect: Dialect) -> Classification:
    tokens = tokenize(sql, dialect)
    words = _words(tokens)
    if not words:
        return Classification(kind=StatementKind.UNKNOWN)
    first = words[0]
    if first == "PRAGMA":  # `PRAGMA x = y` sets, `PRAGMA x` reads (an "=" inside a string or comment does not count)
        assigns = any(t.kind == TokenKind.OTHER and t.text == "=" for t in tokens)
        return Classification(kind=StatementKind.SESSION if assigns else StatementKind.READ)
    kind = _DIALECT_KEYWORD_KIND.get(dialect, {}).get(first) or _KEYWORD_KIND.get(first, StatementKind.UNKNOWN)
    if kind == StatementKind.READ and first in ("SELECT", "WITH"):
        # Data-modifying CTEs, locking reads and file writes are writes even when sqlglot could not parse them.
        has_dml = any(w in ("INSERT", "UPDATE", "DELETE", "MERGE") for w in words[1:])
        if has_dml or _takes_row_locks(words) or _writes_server_file(words):
            kind = StatementKind.WRITE
    elif kind == StatementKind.SESSION and first == "SET" and _takes_row_locks(words):
        kind = StatementKind.WRITE  # SET @x = (SELECT … FOR UPDATE)
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
    tokens = [t for t in tokenize(sql, dialect) if t.kind not in INSIGNIFICANT and not _is_hint(t)]
    if not tokens or tokens[0].upper not in _EXPLAIN_KEYWORDS[dialect]:
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
    while i < len(tokens):
        word = tokens[i].upper
        if word == "FORMAT" and i + 1 < len(tokens) and tokens[i + 1].text == "=":
            i += 3  # MySQL: EXPLAIN [ANALYZE] FORMAT = TREE|JSON|TRADITIONAL …
        elif word in (*_ANALYZE, "VERBOSE", "QUERY", "PLAN"):
            analyze = analyze or word in _ANALYZE
            i += 1
        else:
            break
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
        return [rules.classify_expression(node, dialect) for node in rules.parse(sql, dialect)]
    except rules.EXPECTED_FAILURES as exc:
        logger.debug("sqlglot could not parse the statement ({}); using the keyword rules", type(exc).__name__)
    except Exception as exc:  # e.g. a dialect missing from an app bundle, or a sqlglot bug
        logger.warning("sqlglot failed on a statement ({}); using the keyword rules", type(exc).__name__)
    return None


def _classify_statement(sql: str, dialect: Dialect, has_body: bool = False) -> Classification:
    """One statement as the splitter sees it; `has_body`: it holds a routine or trigger body."""
    tokens = tokenize(sql, dialect)
    if _has_version_comment(tokens):
        return Classification(kind=StatementKind.UNKNOWN)
    words = _words(tokens)
    result = _classify_text(sql, dialect, has_body)
    if result.kind == StatementKind.TRANSACTION and not _begins_a_transaction_statement(words, dialect):
        return Classification(kind=StatementKind.UNKNOWN)
    # R-03a-F1: the words decide after whichever rules did. sqlglot 30.23 cannot parse `ALTER TABLE t DROP PARTITION p`
    # and reads `TRUNCATE PARTITION` as a Command, so both reach the keyword rules, which judge only the first word.
    if missing := [r for r in _alter_reasons(words) if r not in result.dangerous_reasons]:
        result = Classification(kind=result.kind, dangerous_reasons=[*result.dangerous_reasons, *missing])
    # R-03a-P7: decided after sqlglot too, as a floor. sqlglot 30.23 cannot parse INTO OUTFILE (so _lexical sees it
    # today), but a sqlglot that parses it as exp.Into would make it a plain READ for MySQL (INTO_CREATES_A_TABLE
    # excludes it). A floor, not an early return: `CREATE PROCEDURE … INTO OUTFILE` stays DDL, and a DDL result is
    # what refreshes the schema afterwards.
    if (
        dialect is Dialect.MYSQL
        and _writes_server_file(words)
        and _SEVERITY.index(result.kind) < _SEVERITY.index(StatementKind.WRITE)
    ):
        return Classification(kind=StatementKind.WRITE, dangerous_reasons=result.dangerous_reasons)
    return result


def _classify_text(sql: str, dialect: Dialect, has_body: bool) -> Classification:
    """The rules for one statement without a version comment: EXPLAIN ANALYZE unwrapped, then sqlglot, then keywords."""
    while (inner := _explain_inner(sql, dialect)) is not None:  # EXPLAIN ANALYZE runs the statement it explains
        sql = inner
    found = _from_sqlglot(sql, dialect)
    if not found:
        return _lexical(sql, dialect)
    if len(found) == 1:
        return found[0] or _lexical(sql, dialect)
    if has_body:  # sqlglot does not know routine/trigger bodies and cuts them at their inner `;`
        return _lexical(sql, dialect)
    # The splitter saw one statement where sqlglot sees several: judge every one, never just the first.
    results = [c or Classification(kind=StatementKind.UNKNOWN) for c in found]
    if dialect in _TRANSACTION_STARTS:  # the caller's leading-words check can vouch for the first statement only
        unknown = Classification(kind=StatementKind.UNKNOWN)
        results = [unknown if r.kind == StatementKind.TRANSACTION else r for r in results]
    return _combine(results)


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
    if not spans:
        return _classify_statement(sql, dialect)
    # The statement text, not the input: a MySQL `DELIMITER $$` line and its terminator are not part of the statement.
    results = [_classify_statement(span.text, dialect, span.has_body) for span in spans]
    return results[0] if len(results) == 1 else _combine(results)


def warm_up() -> None:
    """Import sqlglot and parse one statement per dialect, so the first real classification does not pay for it."""
    started = time.perf_counter()
    for dialect in Dialect:
        _from_sqlglot("SELECT 1", dialect)
    logger.debug("SQL classifier warmed up in {:.0f} ms", (time.perf_counter() - started) * 1000)
