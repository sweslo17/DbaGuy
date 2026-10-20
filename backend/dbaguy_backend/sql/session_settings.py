"""The session settings a run from statement N still sends from the part it skips (Run SQL file, ruling R4): a job
runs on a fresh session, so a `SET search_path`, `SET NAMES` or `SET TIME_ZONE` before N would otherwise be lost and
the rest would run differently (rows in another schema, TIMESTAMPs read in another zone). A fixed allow-list, per
dialect; everything else before N is read, never sent. Never a setting that outlives the session (GLOBAL, PERSIST, a
password) or belongs to one transaction. Pure."""

import re

from .dialects import Dialect
from .lexer import INSIGNIFICANT, TokenKind, tokenize
from .transaction_text import first_words, statement_head
from .tsql import statements

# PostgreSQL: SET [SESSION] …, never SET LOCAL or what only the current transaction takes
_PG_NOT = frozenset({"LOCAL", "TRANSACTION", "CONSTRAINTS"})
# pg_dump's `SELECT pg_catalog.set_config('search_path', '', false)`: false = for the session
_PG_SET_CONFIG = re.compile(
    r"SELECT\s+(?:pg_catalog\s*\.\s*)?set_config\s*\(\s*'(?:[^']|'')*'\s*,\s*'(?:[^']|'')*'\s*,\s*false\s*\)",
    re.IGNORECASE)
# MySQL: never what outlives the session or touches an account, nor the next transaction's characteristics (refused
# inside one, 1568)
_MYSQL_NOT = re.compile(r"\b(?:GLOBAL|PERSIST|PERSIST_ONLY|PASSWORD|DEFAULT\s+ROLE)\b|@@(?:GLOBAL|PERSIST)",
                        re.IGNORECASE)
_SQLITE_PRAGMA = re.compile(r"PRAGMA\s+(?:foreign_keys|defer_foreign_keys|recursive_triggers)\s*(?:=|\()",
                            re.IGNORECASE)
_ON_OFF = frozenset({"ON", "OFF"})


def is_session_setting(text: str, dialect: Dialect) -> bool:
    """Whether a statement only sets up the session, so a run that skips it must still send it."""
    match dialect:
        case Dialect.POSTGRES:
            words = first_words(text, dialect, 2)
            if words[:1] == ["SET"]:
                return words[1:2] != [] and words[1] not in _PG_NOT
            return _PG_SET_CONFIG.fullmatch(text.strip()) is not None
        case Dialect.MYSQL:
            head = statement_head(text, dialect)
            words = first_words(head, dialect, 2)
            if words[:1] == ["USE"]:
                return True
            return words[:1] == ["SET"] and words[1:2] != ["TRANSACTION"] and _MYSQL_NOT.search(head) is None
        case Dialect.TSQL:
            return _tsql_settings(text)
        case Dialect.SQLITE:
            return _SQLITE_PRAGMA.match(text.strip()) is not None
    return False


def _tsql_settings(text: str) -> bool:
    """Every statement of the batch is `SET <option …> ON|OFF` or `USE`: `SET ANSI_NULLS ON`, `SET IDENTITY_INSERT t
    ON`, a batch of several such lines."""
    if first_words(text, Dialect.TSQL, 1) not in (["SET"], ["USE"]):
        return False  # most batches: no lexing
    significant = [token for token in tokenize(text, Dialect.TSQL) if token.kind not in INSIGNIFICANT]
    for statement in statements(significant):
        head = statement[0].upper if statement[0].kind == TokenKind.WORD else ""
        last = statement[-1].upper if statement[-1].kind == TokenKind.WORD else ""
        if not (head == "USE" or (head == "SET" and len(statement) > 2 and last in _ON_OFF)):
            return False
    return True

