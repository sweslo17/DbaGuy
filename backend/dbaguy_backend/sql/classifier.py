import threading
import time
from collections.abc import Iterator
from types import ModuleType

from loguru import logger

# The models live in the domain (the execution protocol carries them); re-exported here for the sql/ callers.
from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import Dialect
from .lexer import (
    HINT_COMMENT_START, INSIGNIFICANT, VERSION_COMMENT_STARTS, Token, TokenKind, tokenize, with_ascii_spaces,
)
from .splitter import split
from .tsql import statements

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
    Dialect.TSQL: {  # spec §3.4
        **dict.fromkeys(
            ("DECLARE", "PRINT", "WAITFOR", "RAISERROR", "THROW", "OPEN", "CLOSE", "DEALLOCATE"), StatementKind.READ
        ),
        # a procedure may write: EXEC / EXECUTE / sp_executesql (and a bare procedure name, which no table knows)
        **dict.fromkeys(("EXEC", "EXECUTE", "DBCC", "BACKUP", "RESTORE", "KILL"), StatementKind.UNKNOWN),
        **dict.fromkeys(("BULK", "WRITETEXT", "UPDATETEXT"), StatementKind.WRITE),  # text/image writes (R-03b-P2)
        "SAVE": StatementKind.TRANSACTION, "DENY": StatementKind.DDL,
        # Words _KEYWORD_KIND (or PRAGMA's rule) reads as a read, transaction or session statement that start no T-SQL
        # statement: as the first word of a batch, such a name runs the procedure it names (plan Ruling 8).
        **dict.fromkeys(
            ("SHOW", "EXPLAIN", "DESCRIBE", "DESC", "PRAGMA", "START", "SAVEPOINT", "RELEASE", "ABORT", "RESET",
             "ATTACH", "DETACH", "LISTEN", "UNLISTEN", "NOTIFY", "DISCARD"),
            StatementKind.UNKNOWN,
        ),
    },
}
# MySQL: sqlglot reads every `START …` as a transaction, and so does _KEYWORD_KIND (START REPLICA, START
# GROUP_REPLICATION, …). A statement counts as a TRANSACTION only when it begins with one of these; anything else is
# UNKNOWN (R-03a-T4). A dialect without an entry (PostgreSQL, SQLite) has no restriction.
_TRANSACTION_STARTS: dict[Dialect, tuple[tuple[str, ...], ...]] = {
    Dialect.MYSQL: (
        ("BEGIN",), ("START", "TRANSACTION"), ("COMMIT",), ("ROLLBACK",), ("SAVEPOINT",), ("RELEASE", "SAVEPOINT"),
    ),
    Dialect.TSQL: (
        ("BEGIN", "TRAN"), ("BEGIN", "TRANSACTION"), ("BEGIN", "DISTRIBUTED"), ("COMMIT",), ("ROLLBACK",), ("SAVE",),
    ),
}
# The keywords that open an EXPLAIN. In MySQL DESCRIBE and DESC are synonyms: `DESCRIBE ANALYZE DELETE …` runs the
# DELETE, while `DESC t` and `DESCRIBE t` only list the columns (no ANALYZE follows).
_EXPLAIN_KEYWORDS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("EXPLAIN",),
    Dialect.SQLITE: ("EXPLAIN",),
    Dialect.MYSQL: ("EXPLAIN", "DESCRIBE", "DESC"),
    Dialect.TSQL: (),  # T-SQL has no EXPLAIN: plans come from SET SHOWPLAN_* / SET STATISTICS (SESSION statements)
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
# T-SQL control flow: the statements inside are cut out and judged on their own, so these words read and write nothing.
_TSQL_CONTROL = frozenset({"IF", "ELSE", "WHILE", "BREAK", "CONTINUE", "GOTO", "RETURN"})
# BEGIN / END are control flow only as a block or TRY/CATCH (R-03b-P2); BEGIN TRAN|TRANSACTION|DISTRIBUTED keep their
# transaction kind; any other BEGIN x / END x (BEGIN DIALOG, END CONVERSATION, …) is UNKNOWN.
_TSQL_BLOCK_WORDS = frozenset({"", "TRY", "CATCH"})
_TSQL_BEGIN_TRANSACTION = frozenset({"TRAN", "TRANSACTION", "DISTRIBUTED"})
# A floor over every word of a T-SQL batch (strings, comments and [names] are not words): whatever the cuts merged,
# no write can pass as something milder. Kinds only; the dangerous reasons come from the statements.
_TSQL_FLOOR: dict[str, StatementKind] = {
    **dict.fromkeys(
        ("INSERT", "UPDATE", "DELETE", "MERGE", "BULK", "WRITETEXT", "UPDATETEXT",
         # Table hints that take or keep locks (R-03b-T3b), as FOR UPDATE / FOR SHARE in PostgreSQL and MySQL
         "UPDLOCK", "XLOCK", "TABLOCKX", "HOLDLOCK", "SERIALIZABLE", "REPEATABLEREAD"),
        StatementKind.WRITE,
    ),
    # ADD and TRIGGER are reserved words only DDL uses: ADD SIGNATURE, ADD SENSITIVITY CLASSIFICATION and ENABLE |
    # DISABLE TRIGGER are accepted after IF, ELSE or WHILE, and ADD … after an unterminated statement too.
    **dict.fromkeys(
        ("CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "DENY", "ADD", "TRIGGER"), StatementKind.DDL
    ),
    **dict.fromkeys(
        ("EXEC", "EXECUTE", "DBCC", "BACKUP", "RESTORE", "KILL", "SHUTDOWN", "RECONFIGURE", "CHECKPOINT", "OPENROWSET",
         "OPENQUERY", "OPENDATASOURCE", "SETUSER"),
        StatementKind.UNKNOWN,
    ),
}
# The same floor for words that write only together ("(" stands for an opening parenthesis). NEXT VALUE FOR advances a
# sequence, inside any SELECT or SET. Service Broker statements write (R-03b-P2), and only their phrases count: alone,
# send, receive, conversation and dialog are plausible column names (R-03b-T3c). A RECEIVE that starts a statement is
# its own piece (tsql._starts), which no rule knows: UNKNOWN.
_TSQL_FLOOR_PHRASES: dict[tuple[str, ...], StatementKind] = dict.fromkeys(
    (("NEXT", "VALUE", "FOR"), ("SEND", "ON", "CONVERSATION"), ("END", "CONVERSATION"), ("BEGIN", "DIALOG"),
     ("BEGIN", "CONVERSATION"), ("MOVE", "CONVERSATION"), ("GET", "CONVERSATION", "GROUP"), ("WAITFOR", "(", "RECEIVE")),
    StatementKind.WRITE,
)
# Where a floor word is neither a statement nor a hint. Before JOIN or UNION, MERGE is a query hint (INNER MERGE JOIN,
# OPTION (MERGE UNION); a MERGE statement's target cannot be named JOIN or UNION, both reserved). After ISOLATION
# LEVEL, SERIALIZABLE is an isolation level (SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, which the allow-list
# allows); LEVEL alone is not enough, since an index may be named level (WITH (INDEX = level SERIALIZABLE)).
_TSQL_FLOOR_NOT_BEFORE: dict[str, frozenset[str]] = {"MERGE": frozenset({"JOIN", "UNION"})}
_TSQL_FLOOR_NOT_AFTER: dict[str, tuple[str, ...]] = {"SERIALIZABLE": ("ISOLATION", "LEVEL")}


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
    overrides = _DIALECT_KEYWORD_KIND.get(dialect, {})
    if first == "PRAGMA" and first not in overrides:
        # `PRAGMA x = y` sets, `PRAGMA x` reads (an "=" inside a string or comment does not count)
        assigns = any(t.kind == TokenKind.OTHER and t.text == "=" for t in tokens)
        return Classification(kind=StatementKind.SESSION if assigns else StatementKind.READ)
    kind = overrides.get(first) or _KEYWORD_KIND.get(first, StatementKind.UNKNOWN)
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


def _tsql_rule(words: list[str]) -> Classification | None:
    """T-SQL statements decided before sqlglot (spec §3.4); None when the general rules decide."""
    first = words[0] if words else ""
    second = words[1] if len(words) > 1 else ""
    if first in _TSQL_CONTROL:
        return Classification(kind=StatementKind.READ)  # IF, ELSE, WHILE, …: their statements are separate pieces
    if first in ("BEGIN", "END"):
        if second in _TSQL_BLOCK_WORDS and len(words) <= 2:
            return Classification(kind=StatementKind.READ)  # BEGIN, END, BEGIN TRY, END CATCH
        if first == "BEGIN" and second in _TSQL_BEGIN_TRANSACTION:
            return None  # BEGIN TRAN …: the transaction rules decide
        return Classification(kind=StatementKind.UNKNOWN)  # BEGIN DIALOG, END CONVERSATION, …: fail closed
    if first == "SET" and len(words) > 1 and words[1].startswith("@") and not words[1].startswith("@@"):
        return Classification(kind=StatementKind.READ)  # SET @var = …: a variable, not a session setting
    if words[:2] == ["UPDATE", "STATISTICS"]:
        return Classification(kind=StatementKind.WRITE)  # maintenance; it has no WHERE to miss
    return None


def _tsql_unfiltered_dml(tokens: list[Token]) -> list[DangerReason]:
    """The danger reason of a T-SQL UPDATE or DELETE, also one after a CTE (WITH … DELETE), that has no WHERE at
    parenthesis depth 0: a WHERE inside a subquery or a CTE filters nothing. Decided on the words, whatever sqlglot
    makes of the rest: it cannot parse `OPTION (USE HINT …)`, nor `OPTION (MAXDOP 1)` after a CTE (self-check)."""
    depth, dml = 0, ""
    for k, token in enumerate(t for t in tokens if t.kind not in INSIGNIFICANT):
        word = token.upper if token.kind == TokenKind.WORD else ""
        depth = max(depth + (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN), 0)
        if depth or not word:
            continue
        if k == 0 and word not in ("UPDATE", "DELETE", "WITH"):
            return []
        if not dml and word in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"):
            if word not in ("UPDATE", "DELETE"):
                return []
            dml = word
        elif dml and word == "WHERE":
            return []
    reasons = {"UPDATE": DangerReason.UPDATE_WITHOUT_WHERE, "DELETE": DangerReason.DELETE_WITHOUT_WHERE}
    return [reasons[dml]] if dml else []


_SELECT_FAMILY = ("SELECT", "WITH")


def _tsql_selects_into_a_table(tokens: list[Token]) -> bool:
    """A SELECT … INTO that creates a table, in one T-SQL statement (`tokens`: its significant tokens): an INTO not
    right after INSERT or MERGE and not followed by an @variable, in a statement whose first word (also after an opening
    parenthesis) is SELECT or WITH, or inside a parenthesis that opens with one: T-SQL allows INTO in no subquery, so
    that is a statement in parentheses, which the cuts leave in the piece before it (`IF 1 = 1 (SELECT * INTO t2 …)`).
    Decided on the words, whatever sqlglot makes of the rest: it cannot parse `OPTION (USE HINT …)` or `CHANGETABLE(…)`
    (R-03b-P2, self-check)."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in tokens]
    statement = next((t.upper for t in tokens if t.kind != TokenKind.LPAREN), "") in _SELECT_FAMILY
    groups: list[bool] = []  # per open parenthesis: whether it opens with SELECT or WITH
    for k, token in enumerate(tokens):
        if token.kind == TokenKind.LPAREN:
            groups.append(k + 1 < len(words) and words[k + 1] in _SELECT_FAMILY)
        elif token.kind == TokenKind.RPAREN:
            groups = groups[:-1]
        elif words[k] == "INTO" and (statement or any(groups)) and not (k and words[k - 1] in ("INSERT", "MERGE")):
            following = tokens[k + 1] if k + 1 < len(tokens) else None
            if following is None or following.kind != TokenKind.WORD or not following.text.startswith("@"):
                return True  # not FETCH … INTO @x, OUTPUT … INTO @t
    return False


def _tsql_floor_kinds(tokens: list[Token], pieces: list[list[Token]]) -> Iterator[StatementKind]:
    """The floor kinds of a T-SQL batch (`tokens`: its significant tokens; `pieces`: them cut into statements): those
    of its words and phrases, and DDL for a SELECT … INTO that creates a table."""
    words = [t.upper if t.kind == TokenKind.WORD else "(" if t.kind == TokenKind.LPAREN else "" for t in tokens]
    for k, word in enumerate(words):
        after = _TSQL_FLOOR_NOT_AFTER.get(word)
        exempt = (k + 1 < len(words) and words[k + 1] in _TSQL_FLOOR_NOT_BEFORE.get(word, ())) or (
            after is not None and tuple(words[max(k - len(after), 0):k]) == after
        )
        if word in _TSQL_FLOOR and not exempt:
            yield _TSQL_FLOOR[word]
        for phrase, kind in _TSQL_FLOOR_PHRASES.items():
            if tuple(words[k:k + len(phrase)]) == phrase:
                yield kind
    if any(_tsql_selects_into_a_table(piece) for piece in pieces):
        yield StatementKind.DDL  # whatever kind sqlglot or a control-flow rule gave its piece


def _classify_tsql_batch(sql: str, has_body: bool) -> Classification:
    """A T-SQL batch (the text between GO lines): every statement judged, with or without `;` between them, the
    strictest wins, and no word of the batch may be stricter than that (the floor). A batch that defines a procedure,
    function, trigger or view is that one DDL statement: its body runs later, not now."""
    if has_body:
        return _classify_single(sql, Dialect.TSQL, has_body=True)
    tokens = [t for t in tokenize(sql, Dialect.TSQL) if t.kind not in INSIGNIFICANT]
    pieces = statements(tokens)
    results = [_classify_single(sql[p[0].start:p[-1].end], Dialect.TSQL) for p in pieces]
    combined = _combine(results) if results else Classification(kind=StatementKind.UNKNOWN)
    floor = max((*_tsql_floor_kinds(tokens, pieces), StatementKind.READ), key=_SEVERITY.index)
    if _SEVERITY.index(floor) > _SEVERITY.index(combined.kind):
        return Classification(kind=floor, dangerous_reasons=combined.dangerous_reasons)
    return combined


def _classify_statement(sql: str, dialect: Dialect, has_body: bool = False) -> Classification:
    """One span as the splitter sees it: a statement, or for T-SQL a whole batch."""
    if dialect is Dialect.TSQL:
        return _classify_tsql_batch(sql, has_body)
    return _classify_single(sql, dialect, has_body)


def _classify_single(sql: str, dialect: Dialect, has_body: bool = False) -> Classification:
    """One statement; `has_body`: it holds a routine or trigger body."""
    tokens = tokenize(sql, dialect)
    if _has_version_comment(tokens):
        return Classification(kind=StatementKind.UNKNOWN)
    words = _words(tokens)
    tsql = dialect is Dialect.TSQL and not has_body
    if tsql and (decided := _tsql_rule(words)) is not None:
        return decided
    # sqlglot's tokenizer does not know U+200B, which SQL Server reads as a space
    result = _classify_text(with_ascii_spaces(sql) if dialect is Dialect.TSQL else sql, dialect, has_body)
    if result.kind == StatementKind.TRANSACTION and not _begins_a_transaction_statement(words, dialect):
        return Classification(kind=StatementKind.UNKNOWN)
    # R-03a-F1: the words decide after whichever rules did. sqlglot 30.23 cannot parse `ALTER TABLE t DROP PARTITION p`
    # and reads `TRUNCATE PARTITION` as a Command, so both reach the keyword rules, which judge only the first word.
    # The same holds for T-SQL's unfiltered UPDATE/DELETE.
    implied = [*_alter_reasons(words), *(_tsql_unfiltered_dml(tokens) if tsql else [])]
    if missing := [r for r in implied if r not in result.dangerous_reasons]:
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
    return result  # T-SQL's table-creating INTO is part of the batch's floor (_tsql_floor_kinds)


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
