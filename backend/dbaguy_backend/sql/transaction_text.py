"""What a statement's text tells about the transaction around it, for Run SQL file (spec ⑥ R-06-7, R-06-12): whether
it controls the transaction itself, whether MySQL commits before running it, whether it may commit (or apply part of
its work) where the text cannot show it. Only words outside strings and comments count. Pure.

The answers decide how a run that stops is reported, so where the text cannot tell they lean to "it may": a run then
says `unknown` rather than `rolled_back` (Q3)."""

import re
from collections.abc import Iterator
from itertools import chain, islice

from .classifier import is_transaction_statement
from .dialects import Dialect
from .lexer import INSIGNIFICANT, VERSION_COMMENT_STARTS, Token, TokenKind, first_token, scan
from .splitter import MODULE_HEAD, defines_module  # the splitter's own rule for a T-SQL module's definition
from .tsql import STATEMENT_STARTS, statements

# The first words of a transaction statement in any dialect: only a statement that starts with one is looked at closer
_TRANSACTION_WORDS = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "SAVE"})
# MySQL commits the open transaction before these run, even when they then fail (M8): an all-or-nothing run refuses
# them before they are sent (pre-flight H1). CREATE / DROP TEMPORARY TABLE does not commit. Two-word ones: replication
# control and SET PASSWORD (ruling R1: START REPLICA failed with 1227 after committing the INSERT before it).
_MYSQL_COMMITS = frozenset({"ALTER", "ANALYZE", "CACHE", "CHANGE", "CHECK", "CREATE", "DROP", "FLUSH", "GRANT",
                            "IMPORT", "INSTALL", "LOAD", "LOCK", "OPTIMIZE", "RENAME", "REPAIR", "RESET", "REVOKE",
                            "TRUNCATE", "UNINSTALL", "UNLOCK"})
_MYSQL_COMMIT_PAIRS = frozenset({("START", "REPLICA"), ("START", "SLAVE"), ("STOP", "REPLICA"), ("STOP", "SLAVE"),
                                 ("SET", "PASSWORD")})
_AUTOCOMMIT = re.compile(r"\bautocommit\b", re.IGNORECASE)
# A procedure runs statements the file does not show: it may commit, roll back, or commit part of its work. So does a
# PostgreSQL DO block (ruling R6: `DO $$ … COMMIT … $$` statement by statement)
_CALLS = frozenset({"CALL", "EXEC", "EXECUTE"})
# A statement that ends the transaction even when one is open after it (COMMIT AND CHAIN, MySQL's completion_type
# CHAIN, and MySQL's START TRANSACTION, which commits the one before): T-SQL's COMMIT inside a nested BEGIN TRAN only
# counts down, so it is not here
_ENDS = {Dialect.POSTGRES: frozenset({"COMMIT", "END", "ROLLBACK", "ABORT"}),
         Dialect.MYSQL: frozenset({"COMMIT", "ROLLBACK", "START", "BEGIN"}),
         Dialect.SQLITE: frozenset({"COMMIT", "END", "ROLLBACK"})}
# T-SQL reserves these words: outside strings, comments and [names] a batch holds them only as transaction statements
# (BEGIN TRAN, COMMIT, ROLLBACK, SAVE TRAN), wherever they stand in it. The text is lexed only when it holds one of
# them at all: a batch of INSERTs whose data says "transaction" is not (a lexing pass costs as much as the split's).
_TSQL_TRANSACTION = re.compile(r"\b(?:COMMIT|ROLLBACK)\b|\b(?:BEGIN|SAVE)\s+(?:TRAN|TRANSACTION|DISTRIBUTED)\b",
                               re.IGNORECASE)
_TSQL_OPENS = {"BEGIN": frozenset({"TRAN", "TRANSACTION", "DISTRIBUTED"}), "SAVE": frozenset({"TRAN", "TRANSACTION"})}
# The statements that write rows (documents) one at a time where a failed one keeps part of its work (writes_rows),
# and the reads a WITH clause can lead to instead
_WRITES = {Dialect.MYSQL: frozenset({"INSERT", "REPLACE", "UPDATE", "DELETE"}),
           Dialect.SQLPP: frozenset({"INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE"})}
# The first two words that make a statement such a write, or not one, whatever its first word says (06b fix wave):
# MySQL's LOAD DATA and LOAD XML; SQL++'s EXECUTE FUNCTION (a JavaScript function may write documents), and its
# UPDATE STATISTICS, which writes optimizer statistics, not documents (its ANALYZE spelling starts with no write)
_WRITE_PAIRS = {Dialect.MYSQL: {("LOAD", "DATA"): True, ("LOAD", "XML"): True},
                Dialect.SQLPP: {("EXECUTE", "FUNCTION"): True, ("UPDATE", "STATISTICS"): False}}
_READ_VERBS = frozenset({"SELECT", "TABLE", "VALUES"})
_MAY_COMMIT = re.compile(r"\b(?:COMMIT|CALL|EXEC|EXECUTE)\b", re.IGNORECASE)
_TSQL_MAY_COMMIT = re.compile(r"\b(?:COMMIT|EXEC|EXECUTE)\b", re.IGNORECASE)  # data saying "call" is not lexed
_VERSION = re.compile(r"/\*M?!\d*")  # the opening of a version comment (lexer.VERSION_COMMENT_STARTS) and its version


def statement_head(text: str, dialect: Dialect) -> str:
    """The text as the server reads its start: inside a leading MySQL version comment (`/*!40000 ALTER TABLE t … */`,
    which mysqldump writes and the server runs), the comment's own text."""
    token = first_token(text, dialect)
    if token is None or token.kind != TokenKind.EXEC_COMMENT or not token.text.startswith(VERSION_COMMENT_STARTS):
        return text
    inner = _VERSION.sub("", token.text, count=1)
    return inner[:-2] if token.terminated else inner


def first_words(text: str, dialect: Dialect, count: int = 2) -> list[str]:
    """The first words of a statement, upper-cased, up to the first token that is not a word (inside a leading
    version comment, its words)."""
    words: list[str] = []
    for token in scan(statement_head(text, dialect), dialect):
        if token.kind in INSIGNIFICANT:
            continue
        if token.kind != TokenKind.WORD or len(words) == count:
            break
        words.append(token.upper)
    return words


def transaction_word(text: str, dialect: Dialect) -> str | None:
    """The word of the transaction statement the file runs itself (`COMMIT`), or None. A T-SQL batch is looked at
    whole (`INSERT …; COMMIT` is one batch), except a module's definition, whose body runs later."""
    if dialect is Dialect.TSQL:
        return _tsql_transaction_word(text)
    head = statement_head(text, dialect)
    words = first_words(head, dialect, 1)
    if words and words[0] in _TRANSACTION_WORDS and is_transaction_statement(head, dialect):
        return words[0]
    return None


def _tsql_transaction_word(text: str) -> str | None:
    if _TSQL_TRANSACTION.search(text) is None:  # most batches: no lexing
        return None
    significant = _tsql_body(text)
    if significant is None:
        return None  # CREATE PROCEDURE … COMMIT …: defined now, run later
    previous = ""
    for token in significant:
        word = token.upper if token.kind == TokenKind.WORD else ""
        if word in ("COMMIT", "ROLLBACK"):
            return word
        if word in _TSQL_OPENS.get(previous, ()):
            return previous if previous == "SAVE" else f"{previous} {word}"
        previous = word
    return None


def _tsql_body(text: str) -> Iterator[Token] | None:
    """A T-SQL batch's significant tokens, read lazily; None for a module's definition (its body runs later)."""
    significant = (token for token in scan(text, Dialect.TSQL) if token.kind not in INSIGNIFICANT)
    head = list(islice(significant, MODULE_HEAD))
    return None if defines_module(head) else chain(head, significant)


def implicit_commit_word(text: str, dialect: Dialect) -> str | None:
    """MySQL: the first word(s) of a statement before which the server commits the open transaction (DDL, LOCK TABLES,
    GRANT, START REPLICA, SET PASSWORD, …), or None (every other dialect: None)."""
    if dialect is not Dialect.MYSQL:
        return None
    words = first_words(text, dialect)
    if words and words[0] in _MYSQL_COMMITS and words[1:2] != ["TEMPORARY"]:
        return words[0]
    if tuple(words) in _MYSQL_COMMIT_PAIRS:
        return " ".join(words)
    return None


def sets_autocommit(text: str, dialect: Dialect) -> bool:
    """MySQL: a SET of autocommit, which commits the open transaction when it turns autocommit on (from off)."""
    if dialect is not Dialect.MYSQL:
        return False
    head = statement_head(text, dialect)
    return first_words(head, dialect, 1) == ["SET"] and _AUTOCOMMIT.search(head) is not None


def calls_a_procedure(text: str, dialect: Dialect) -> bool:
    """The statement runs code the file does not show: CALL, EXEC or EXECUTE first, a PostgreSQL DO block, or (T-SQL)
    a batch that starts with a procedure's name, which T-SQL runs as EXEC."""
    words = first_words(text, dialect, 1)
    if not words:
        return False
    if words[0] in _CALLS or (dialect is Dialect.POSTGRES and words[0] == "DO"):
        return True
    return dialect is Dialect.TSQL and words[0] not in STATEMENT_STARTS and words[0] != "WITH"


def ends_the_transaction(text: str, dialect: Dialect) -> bool:
    """Whether the statement ended the transaction open before it, even when one is open after it (ruling R5: COMMIT
    AND CHAIN, MySQL's completion_type=CHAIN or START TRANSACTION inside one)."""
    words = first_words(text, dialect, 3)
    return (bool(words) and words[0] in _ENDS.get(dialect, ()) and "TO" not in words  # not ROLLBACK [WORK] TO x
            and transaction_word(text, dialect) is not None)


def may_commit(text: str, dialect: Dialect) -> bool:
    """Whether running the statement may have committed the transaction around it: it holds COMMIT, or calls a
    procedure (a T-SQL batch: EXEC anywhere in it)."""
    if calls_a_procedure(text, dialect):
        return True
    if (_TSQL_MAY_COMMIT if dialect is Dialect.TSQL else _MAY_COMMIT).search(text) is None:
        return False
    words = (token.upper for token in scan(text, dialect) if token.kind == TokenKind.WORD)
    tsql = dialect is Dialect.TSQL
    return any(word == "COMMIT" or (tsql and word in ("EXEC", "EXECUTE")) for word in words)


def writes_rows(text: str, dialect: Dialect) -> bool:
    """Whether the statement writes rows (documents) one at a time, so that what it wrote before it failed may stay:
    MySQL's INSERT, REPLACE, UPDATE, DELETE, LOAD DATA or XML and WITH … UPDATE / DELETE, on a table that cannot roll
    back (MyISAM, 06b review M2); SQL++'s INSERT, UPSERT, UPDATE, DELETE, MERGE and EXECUTE FUNCTION, each document on
    its own (M-c), but not UPDATE STATISTICS. Every other dialect: False (every table rolls a failed statement back)."""
    writes = _WRITES.get(dialect)
    if writes is None:
        return False
    words = first_words(text, dialect)
    if not words:
        return False
    if (pair := _WRITE_PAIRS[dialect].get(tuple(words[:2]))) is not None:
        return pair
    if words[0] in writes:
        return True
    if words[0] != "WITH":
        return False
    depth = 0  # the verb after the WITH clause: the first of them outside its parentheses
    for token in scan(statement_head(text, dialect), dialect):
        if token.kind == TokenKind.LPAREN:
            depth += 1
        elif token.kind == TokenKind.RPAREN:
            depth -= 1
        elif depth == 0 and token.kind == TokenKind.WORD and (token.upper in writes or token.upper in _READ_VERBS):
            return token.upper in writes
    return False


def runs_several(text: str, dialect: Dialect) -> bool:
    """Whether the statement runs several statements that each take effect on their own (statement by statement): a
    procedure call, or a T-SQL batch of more than one statement (a module's definition is one)."""
    if calls_a_procedure(text, dialect):
        return True
    if dialect is not Dialect.TSQL or (significant := _tsql_body(text)) is None:
        return False
    return len(statements(list(significant))) > 1
