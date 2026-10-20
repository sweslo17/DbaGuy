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
from .lexer import INSIGNIFICANT, Token, TokenKind, first_token, scan
from .splitter import _MODULE_HEAD, _defines_module  # the splitter's own rule for a T-SQL module's definition
from .tsql import STATEMENT_STARTS, statements

# The first words of a transaction statement in any dialect: only a statement that starts with one is looked at closer
_TRANSACTION_WORDS = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "SAVE"})
# MySQL commits the open transaction before these run, even when they then fail (M8): an all-or-nothing run refuses
# them before they are sent (pre-flight H1). CREATE / DROP TEMPORARY TABLE does not commit.
_MYSQL_COMMITS = frozenset({"ALTER", "ANALYZE", "CACHE", "CHANGE", "CHECK", "CREATE", "DROP", "FLUSH", "GRANT",
                            "INSTALL", "LOAD", "LOCK", "OPTIMIZE", "RENAME", "REPAIR", "RESET", "REVOKE", "TRUNCATE",
                            "UNINSTALL", "UNLOCK"})
# A procedure runs statements the file does not show: it may commit, roll back, or commit part of its work
_CALLS = frozenset({"CALL", "EXEC", "EXECUTE"})
# T-SQL reserves these words: outside strings, comments and [names] a batch holds them only as transaction statements
# (BEGIN TRAN, COMMIT, ROLLBACK, SAVE TRAN), wherever they stand in it. The text is lexed only when it holds one of
# them at all: a batch of INSERTs whose data says "transaction" is not (a lexing pass costs as much as the split's).
_TSQL_TRANSACTION = re.compile(r"\b(?:COMMIT|ROLLBACK)\b|\b(?:BEGIN|SAVE)\s+(?:TRAN|TRANSACTION|DISTRIBUTED)\b",
                               re.IGNORECASE)
_TSQL_OPENS = {"BEGIN": frozenset({"TRAN", "TRANSACTION", "DISTRIBUTED"}), "SAVE": frozenset({"TRAN", "TRANSACTION"})}
_MAY_COMMIT = re.compile(r"\b(?:COMMIT|CALL|EXEC|EXECUTE)\b", re.IGNORECASE)
_VERSION_COMMENT = "/*!"
_VERSION = re.compile(r"/\*!\d*")


def statement_head(text: str, dialect: Dialect) -> str:
    """The text as the server reads its start: inside a leading MySQL version comment (`/*!40000 ALTER TABLE t … */`,
    which mysqldump writes and the server runs), the comment's own text."""
    token = first_token(text, dialect)
    if token is None or token.kind != TokenKind.EXEC_COMMENT or not token.text.startswith(_VERSION_COMMENT):
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
    head = list(islice(significant, _MODULE_HEAD))
    return None if _defines_module(head) else chain(head, significant)


def implicit_commit_word(text: str, dialect: Dialect) -> str | None:
    """MySQL: the first word of a statement before which the server commits the open transaction (DDL, LOCK TABLES,
    GRANT, …), or None (every other dialect: None)."""
    if dialect is not Dialect.MYSQL:
        return None
    words = first_words(text, dialect)
    if words and words[0] in _MYSQL_COMMITS and words[1:2] != ["TEMPORARY"]:
        return words[0]
    return None


def calls_a_procedure(text: str, dialect: Dialect) -> bool:
    """The statement runs code the file does not show: CALL, EXEC or EXECUTE first, or (T-SQL) a batch that starts with
    a procedure's name, which T-SQL runs as EXEC."""
    words = first_words(text, dialect, 1)
    if not words:
        return False
    if words[0] in _CALLS:
        return True
    return dialect is Dialect.TSQL and words[0] not in STATEMENT_STARTS and words[0] != "WITH"


def may_commit(text: str, dialect: Dialect) -> bool:
    """Whether running the statement may have committed the transaction around it: it holds COMMIT, or calls a
    procedure (a T-SQL batch: EXEC anywhere in it)."""
    if calls_a_procedure(text, dialect):
        return True
    if _MAY_COMMIT.search(text) is None:
        return False
    words = (token.upper for token in scan(text, dialect) if token.kind == TokenKind.WORD)
    tsql = dialect is Dialect.TSQL
    return any(word == "COMMIT" or (tsql and word in ("EXEC", "EXECUTE")) for word in words)


def runs_several(text: str, dialect: Dialect) -> bool:
    """Whether the statement runs several statements that each take effect on their own (statement by statement): a
    procedure call, or a T-SQL batch of more than one statement (a module's definition is one)."""
    if calls_a_procedure(text, dialect):
        return True
    if dialect is not Dialect.TSQL or (significant := _tsql_body(text)) is None:
        return False
    return len(statements(list(significant))) > 1
