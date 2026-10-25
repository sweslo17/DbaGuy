"""What keeps a MySQL session read-only and readable as DbaGuy's lexer reads it (spec ⑧ D-08-1, D-08-5, CR-08-5,
CR-08-11).

A stored function can switch its session to READ WRITE from inside a SELECT, and neither a ROLLBACK nor the end of the
statement switches it back (M3). The session's default changes at once; the access mode of the statements that follow
changes at the next COMMIT or ROLLBACK, typed or sent by a Rollback button, and from then on a function that writes
runs (probed: `SELECT f_rw(); ROLLBACK; SELECT f_writes()` writes a row on a session that was READ ONLY). So every
statement a read-only session runs outside a transaction is preceded by a look at the session (`ReadOnlyGuard`), for a
user's read-only tab and an agent's session alike. A transaction needs no look: its access mode was fixed when it
began, and an agent's own START TRANSACTION READ ONLY holds (M3e).

An agent's session also leaves out the sql_mode flags that make the server read strings and quotes unlike the lexer
(H2), and refuses to go on if they are back (a function cannot bring them back: MySQL restores sql_mode when a routine
returns, so the look is a backstop).

The look must not be blindable by what it looks at. A function can set `sql_select_limit = 0`, after which a plain
SELECT returns no row: the look read that as "nothing to put back" and stayed blind for the rest of the connection
(fix round 1). Every probe has an explicit LIMIT 1, which the server lets override `sql_select_limit` (probed on 8.4),
reads sql_mode as bytes (a function can set `character_set_results = ucs2`, which put a NUL between the letters of
the text), and takes nothing but exactly one row of two readable values: anything else is StatementNotSent
`read_only_state_unreadable`, on a tab and on an agent's session, and the user reconnects.

The look costs one round trip per statement outside a transaction: the pure driver does not read the session-state
tracking an OK packet can carry (A14, checked on mysql-connector 26.7.0: `parse_ok` stops at the info text). It also
ends the diagnostics area, so a diagnostic statement is sent without it (P11); `SELECT @@warning_count`,
`@@error_count` and `FOUND_ROWS()` read the look's own state (a known limitation)."""

import re
from typing import NoReturn

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...sql.dialects import Dialect, quote_literal
from ...sql.lexer import INSIGNIFICANT, TokenKind, scan
from ..base import StatementNotSent
from . import catalog

SESSION_CHANGED = "session_changed"
SESSION_CHANGED_MESSAGE = "The connection's sql_mode changed: the call was not run and the connection was reset"
READ_ONLY_STATE_UNREADABLE = "read_only_state_unreadable"
READ_ONLY_STATE_UNREADABLE_MESSAGE = "The session's read-only state could not be read: reconnect"

# NO_BACKSLASH_ESCAPES and ANSI_QUOTES (and ANSI, which turns ANSI_QUOTES back on): with them, `'a\' INTO @x -- '` and
# `"a\" INTO @x -- "` hide an INTO from the lexer that the server runs (H2)
LEXER_UNSAFE_MODES = frozenset({"NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI"})

READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"  # at connect (`MysqlAdapter.open`), and when put back
# What the look asks. The access mode's variable was renamed in 5.7.20 (`tx_read_only` is gone in 8.0.3), so a server
# that does not know the first name is asked the second; one that knows neither (not seen) is asked for sql_mode
# alone, and its access mode is not re-checked (said once), rather than every statement of the tab failing with 1193.
# LIMIT 1: an explicit LIMIT overrides sql_select_limit, which a function can set to 0. The mode as bytes: what
# character_set_results recodes is text, not binary strings.
_MODE = "CAST(@@SESSION.sql_mode AS BINARY)"
_STATES = (
    f"SELECT @@SESSION.transaction_read_only, {_MODE} LIMIT 1",
    f"SELECT @@SESSION.tx_read_only, {_MODE} LIMIT 1",
    f"SELECT NULL, {_MODE} LIMIT 1",
)
_MODE_LIST = re.compile(r"[A-Za-z0-9_,]*")  # what a sql_mode value is made of
_UNKNOWN_SYSTEM_VARIABLE = 1193
# Diagnostic statements: they write nothing, and the look before them would empty what they report (any other
# statement, a SELECT included, ends the diagnostics area: SHOW WARNINGS came back empty, P11)
_DIAGNOSTIC_HEADS = frozenset({("SHOW", "WARNINGS"), ("SHOW", "ERRORS"), ("SHOW", "COUNT"), ("GET", "DIAGNOSTICS"),
                               ("GET", "CURRENT"), ("GET", "STACKED")})


def lexer_safe(sql_mode: str) -> str:
    """`sql_mode` without LEXER_UNSAFE_MODES, the rest in its order."""
    return ",".join(mode for mode in sql_mode.split(",") if mode and mode.upper() not in LEXER_UNSAFE_MODES)


def unsafe_modes(sql_mode: str) -> list[str]:
    return [mode for mode in sql_mode.split(",") if mode.upper() in LEXER_UNSAFE_MODES]


def _head(sql: str) -> tuple[str, ...]:
    """The statement's first two words, upper-cased; "" for a token that is not a word (an executable comment, a
    parenthesis: never a diagnostic statement). Reads no more of the text than that."""
    words: list[str] = []
    for token in scan(sql, Dialect.MYSQL):
        if token.kind in INSIGNIFICANT:
            continue
        words.append(token.upper if token.kind == TokenKind.WORD else "")
        if len(words) == 2:
            break
    return tuple(words)


class ReadOnlyGuard:
    """Before a statement of a read-only session (D-08-5), on the session's worker thread, outside a transaction: if a
    function switched the session to READ WRITE, `SET SESSION TRANSACTION READ ONLY` puts it back (a WARNING). On an
    agent's session (`agent`) a lexer-unsafe sql_mode raises StatementNotSent `session_changed` instead, with nothing
    sent (CR-08-5): the reader closes that session, and the next call opens a clean one. Not before a diagnostic
    statement (SHOW WARNINGS, GET DIAGNOSTICS…): it writes nothing, and the look would clear its answer (P11)."""

    def __init__(self, conn: MySQLConnection, thread_id: int, *, agent: bool) -> None:
        self._conn = conn
        self._thread_id = thread_id
        self._agent = agent
        self._state = 0  # the index in _STATES this server answers

    def before_statement(self, sql: str, in_transaction: bool) -> None:
        if in_transaction or _head(sql) in _DIAGNOSTIC_HEADS:
            return
        read_only, sql_mode = self._look()
        if self._agent and (unsafe := unsafe_modes(sql_mode)):
            logger.warning("MySQL thread {}: an agent's session has {} in its sql_mode: refused", self._thread_id,
                           ", ".join(unsafe))
            raise StatementNotSent(SESSION_CHANGED_MESSAGE, code=SESSION_CHANGED)
        if read_only is not None and not read_only:
            self._conn.cmd_query(READ_ONLY_SESSION)
            logger.warning("MySQL thread {}: a read-only session was switched to READ WRITE; restored", self._thread_id)

    def _look(self) -> tuple[bool | None, str]:
        """(read-only?, sql_mode) of the session; the first None when the server has no variable to read it from.
        Exactly one row of readable values, or StatementNotSent: no answer is not "nothing to put back"."""
        while True:
            try:
                rows = catalog.query(self._conn, _STATES[self._state])
            except mysql.connector.Error as exc:
                if exc.errno != _UNKNOWN_SYSTEM_VARIABLE or self._state == len(_STATES) - 1:
                    raise
                self._state += 1
                if self._state == len(_STATES) - 1:
                    logger.warning("MySQL thread {}: this server has no transaction_read_only or tx_read_only: "
                                   "READ ONLY is not re-checked on this session", self._thread_id)
                continue
            return self._read(rows)

    def _read(self, rows: list[tuple]) -> tuple[bool | None, str]:
        if len(rows) != 1 or len(rows[0]) != 2:
            self._unreadable("an answer that is not one row of two values")
        flag, mode = rows[0]
        if not isinstance(mode, str) or not _MODE_LIST.fullmatch(mode):
            self._unreadable("a sql_mode that is not a list of modes")
        if self._state == len(_STATES) - 1:  # the form without an access mode
            if flag is not None:
                self._unreadable("an access mode where there is none")
            return None, mode
        if str(flag) not in ("0", "1"):
            self._unreadable("an access mode that is neither 0 nor 1")
        return str(flag) == "1", mode

    def _unreadable(self, found: str) -> NoReturn:
        logger.warning("MySQL thread {}: the look at the session found {}: the statement is refused", self._thread_id,
                       found)
        raise StatementNotSent(READ_ONLY_STATE_UNREADABLE_MESSAGE, code=READ_ONLY_STATE_UNREADABLE)


def make_agent_sql_mode(conn: MySQLConnection) -> None:
    """At an agent connection's open, after the profile's own settings: its sql_mode without the unsafe flags."""
    row = catalog.first_row(conn, "SELECT @@SESSION.sql_mode LIMIT 1")  # as the look's probes (T7 re-review)
    current = str(row[0]) if row and row[0] is not None else ""
    if unsafe_modes(current):
        conn.cmd_query(f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, lexer_safe(current))}")
