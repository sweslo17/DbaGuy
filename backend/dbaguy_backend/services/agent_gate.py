"""The agent gate (spec ⑧ D-08-2, CR-08-1, CR-08-4, CR-08-6): what an MCP agent's statement must be before anything is
sent. Pure. In order: text a driver can take whole; exactly one statement; a first word on the dialect's allow-list; no
MySQL executable comment; no function on the deny-list (a PostgreSQL `U&"…"` name decoded first, with its UESCAPE); a
read by the classifier and the read-only allow-list (`is_plain_read`); MySQL: no INTO. A server's read-only transaction
stops writes to the database; these rules close what it does not stop: the server's files and programs, other sessions,
session locks, SQL run from text, settings. The deny-list is checked before the classifier (fix round 1): it names what
a call reaches, and SQL Server's remote and file readers, which no server-side read-only stops, are refused by name
whatever the classifier makes of them.

The deny-list matches names only: a user-defined function that wraps a denied call is not caught (spec §9). Session
locks it takes are released by the read frame's cleanup (D-08-3); a separate read-only login is the full protection.
The gate does not bound time either: `pg_sleep`, `SLEEP` and `BENCHMARK` pass, and Task 6's read frame bounds them
(`statement_timeout`, `max_execution_time`, the call's timeout).

Function names are compared as the server reads them: an unquoted name without regard to case; a quoted PostgreSQL name
exactly (`"PG_TERMINATE_BACKEND"` names another function, which does not exist); every other dialect's quoted names
without regard to case too. A schema-qualified call is judged by its last part."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ..domain.classification import Classification, StatementKind
from ..domain.well_formed import LONE_SURROGATE
from ..sql.classifier import classify
from ..sql.dialects import EXPLAIN_HEADS, Dialect
from ..sql.lexer import INSIGNIFICANT, Token, TokenKind, tokenize
from ..sql.splitter import split
from ..sql.tsql import statements
from .safety import SafetyPolicy

AgentPurpose = Literal["query", "explain"]

# CR-08-6: what an agent's statement may start with. EXPLAIN / DESCRIBE come from the explain tool only.
ALLOWED_STARTS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("SELECT", "WITH", "VALUES", "TABLE", "SHOW"),
    Dialect.MYSQL: ("SELECT", "WITH", "VALUES", "TABLE", "SHOW"),
    Dialect.SQLITE: ("SELECT", "WITH", "VALUES"),
    Dialect.TSQL: ("SELECT", "WITH"),
    Dialect.SQLPP: ("SELECT", "WITH"),
}
_EXPLAIN_WORDS = frozenset({"EXPLAIN", "DESCRIBE", "DESC"})

# The deny-list (D-08-2 ⑤): lowercase names, a trailing `*` matching any name with that prefix, each with what it does.
_SERVER_FILES, _KILLS, _LOCKS, _SQL_TEXT = "server files", "kills sessions", "locks", "runs SQL from text"
_SERVER, _SETTINGS, _CONNECTIONS = "controls the server", "changes settings", "opens other connections"
_LOADS_CODE = "loads code"
DENIED_FUNCTIONS: dict[Dialect, tuple[tuple[str, str], ...]] = {
    Dialect.POSTGRES: (
        ("pg_terminate_backend", _KILLS), ("pg_cancel_backend", _KILLS),
        ("pg_advisory_*", _LOCKS), ("pg_try_advisory_*", _LOCKS),
        ("pg_reload_conf", _SERVER), ("pg_rotate_logfile", _SERVER), ("pg_switch_wal", _SERVER),
        ("pg_promote", _SERVER), ("pg_create_*", _SERVER), ("pg_drop_*", _SERVER), ("pg_replication_*", _SERVER),
        ("pg_log_*", _SERVER), ("set_config", _SETTINGS), ("dblink*", _CONNECTIONS), ("pg_file_*", _SERVER_FILES),
        # Ruling J1 (pre-flight notes): server side effects a read-only transaction does not stop, for any login
        ("pg_stat_reset*", _SERVER), ("pg_backup_*", _SERVER), ("pg_start_backup", _SERVER),
        ("pg_stop_backup", _SERVER), ("pg_wal_replay_*", _SERVER), ("pg_logical_emit_message", _SERVER),
        ("pg_read_file", _SERVER_FILES), ("pg_read_binary_file", _SERVER_FILES), ("pg_ls_*", _SERVER_FILES),
        ("pg_stat_file", _SERVER_FILES), ("lo_*", _SERVER_FILES),
        ("query_to_xml", _SQL_TEXT), ("query_to_xmlschema", _SQL_TEXT), ("query_to_xml_and_xmlschema", _SQL_TEXT),
        ("cursor_to_xml", _SQL_TEXT), ("ts_stat", _SQL_TEXT), ("ts_rewrite", _SQL_TEXT),
    ),
    Dialect.MYSQL: (
        ("get_lock", _LOCKS), ("release_lock", _LOCKS), ("release_all_locks", _LOCKS), ("load_file", _SERVER_FILES),
    ),
    Dialect.SQLITE: (("load_extension", _LOADS_CODE), ("fts3_tokenizer", _LOADS_CODE)),
    # Ruling J1 (pre-flight notes): catalog functions that read the server's own files (logs, traces, audits)
    Dialect.TSQL: (
        ("fn_dblog", _SERVER_FILES), ("fn_dump_dblog", _SERVER_FILES), ("fn_get_audit_file", _SERVER_FILES),
        ("fn_xe_file_target_read_file", _SERVER_FILES), ("fn_trace_gettable", _SERVER_FILES),
        # fix round 1 (review I1): SQL Server has no server-side read-only, so these are refused by name too, not only
        # because the classifier calls them UNKNOWN
        ("openrowset", _SERVER_FILES), ("openquery", _CONNECTIONS), ("opendatasource", _CONNECTIONS),
    ),
    Dialect.SQLPP: (("curl", "reaches the network"), ("advisor", "starts index advisor sessions")),
}
_QUOTED_NAMES_KEEP_CASE = frozenset({Dialect.POSTGRES})
_HEX = re.compile(r"[0-9A-Fa-f]+")


class AgentRefusal(StrEnum):
    INVALID_TEXT = "invalid_text"
    EMPTY = "empty"
    SEVERAL_STATEMENTS = "several_statements"
    EXPLAIN_ADDED = "explain_added"
    NOT_ALLOWED_START = "not_allowed_start"
    EXECUTABLE_COMMENT = "executable_comment"
    NOT_A_READ = "not_a_read"
    DENIED_FUNCTION = "denied_function"
    UNREADABLE_NAME = "unreadable_name"
    INTO = "into"


@dataclass(frozen=True)
class AgentVerdict:
    refusal: AgentRefusal | None = None
    detail: str = ""  # the word, the function and what it does, the kind: for the message
    classification: Classification | None = None
    starts: str = ""  # NOT_ALLOWED_START: what may start a statement

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    def message(self) -> str:
        """§4: `DbaGuy runs exactly one read statement per call: <why>`."""
        return f"DbaGuy runs exactly one read statement per call: {self._why()}"

    def _why(self) -> str:
        match self.refusal:
            case AgentRefusal.INVALID_TEXT:
                return "the text holds a character a driver cannot send (U+0000 or half of a surrogate pair)"
            case AgentRefusal.EMPTY:
                return "there is no statement"
            case AgentRefusal.SEVERAL_STATEMENTS:
                return "several statements"
            case AgentRefusal.EXPLAIN_ADDED:
                return f"{self.detail} is added by the explain tool"
            case AgentRefusal.NOT_ALLOWED_START:
                return f"starts with {self.detail}: only {self.starts} are run"
            case AgentRefusal.EXECUTABLE_COMMENT:
                return "executable comments (/*! … */, /*+ … */) are not allowed"
            case AgentRefusal.NOT_A_READ:
                return f"not a read ({self.detail})"
            case AgentRefusal.DENIED_FUNCTION | AgentRefusal.UNREADABLE_NAME:
                return f"{self.detail} is not allowed"
            case AgentRefusal.INTO:
                return "INTO is not allowed"
        return "allowed"


def _refuse(refusal: AgentRefusal, detail: str = "", classification: Classification | None = None,
            starts: str = "") -> AgentVerdict:
    return AgentVerdict(refusal, detail, classification, starts)


def agent_read(sql: str, dialect: Dialect, purpose: AgentPurpose = "query") -> AgentVerdict:
    """D-08-2: allowed, or why not. `explain`: `sql` is one of EXPLAIN_HEADS followed by a statement this gate allows
    as a query."""
    if "\x00" in sql or LONE_SURROGATE.search(sql):
        return _refuse(AgentRefusal.INVALID_TEXT)
    tokens = _one_statement(sql, dialect)
    if isinstance(tokens, AgentVerdict):
        return tokens
    if purpose == "explain":
        rest = _after_explain_head(tokens, dialect)
        if rest is None:
            return _refuse(AgentRefusal.NOT_ALLOWED_START, tokens[0].text[:40],
                           starts="the explain tool's own EXPLAIN")
        return agent_read(sql[rest:], dialect, "query")
    return _query(sql, tokens, dialect)


def _one_statement(sql: str, dialect: Dialect) -> list[Token] | AgentVerdict:
    """The statement's significant tokens (a trailing `;` dropped) when `sql` is exactly one statement: one span, no `;`
    inside it, nothing significant outside it but a `;` (a GO or DELIMITER line is), one T-SQL statement."""
    spans = split(sql, dialect)
    if not spans:
        return _refuse(AgentRefusal.EMPTY)
    if len(spans) > 1:
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    span = spans[0]
    significant = [t for t in tokenize(sql, dialect) if t.kind not in INSIGNIFICANT]
    inside = [t for t in significant if span.start <= t.start < span.end]
    if any(t.kind != TokenKind.SEMI for t in significant if not span.start <= t.start < span.end):
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    while inside and inside[-1].kind == TokenKind.SEMI:
        inside.pop()
    if not inside:
        return _refuse(AgentRefusal.EMPTY)
    if any(t.kind == TokenKind.SEMI for t in inside):
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    if dialect == Dialect.TSQL and len(statements(inside)) != 1:
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    return inside


def _after_explain_head(tokens: list[Token], dialect: Dialect) -> int | None:
    """Where the statement after the explain tool's head starts, or None when `tokens` does not start with one."""
    for head in EXPLAIN_HEADS[dialect]:
        words = head.split()
        if len(tokens) > len(words) and all(t.upper == word for t, word in zip(tokens, words, strict=False)):
            return tokens[len(words)].start
    return None


def _query(sql: str, tokens: list[Token], dialect: Dialect) -> AgentVerdict:
    first = tokens[0]
    word = first.upper if first.kind == TokenKind.WORD else ""
    if word in _EXPLAIN_WORDS:
        return _refuse(AgentRefusal.EXPLAIN_ADDED, word)
    allowed = ALLOWED_STARTS[dialect]
    if word not in allowed:
        return _refuse(AgentRefusal.NOT_ALLOWED_START, word or first.text[:40], starts=", ".join(allowed))
    if any(t.kind == TokenKind.EXEC_COMMENT for t in tokens):
        return _refuse(AgentRefusal.EXECUTABLE_COMMENT)
    if (denied := _denied_call(tokens, dialect)) is not None:
        return denied
    classification = classify(sql, dialect)
    if classification.kind != StatementKind.READ or not SafetyPolicy._allowed_when_read_only(
            sql, classification, dialect):
        return _refuse(AgentRefusal.NOT_A_READ, classification.kind.value, classification)
    if dialect == Dialect.MYSQL and any(t.kind == TokenKind.WORD and t.upper == "INTO" for t in tokens):
        return _refuse(AgentRefusal.INTO, "", classification)
    return AgentVerdict(classification=classification)


def _denied_call(tokens: list[Token], dialect: Dialect) -> AgentVerdict | None:
    """The first call of a denied function: a name (or a PostgreSQL `U&"…"` name and its UESCAPE) right before `(`."""
    denied = DENIED_FUNCTIONS[dialect]
    if not denied:
        return None
    i = 0
    while i < len(tokens):
        name, after = _name_at(tokens, i, dialect)
        if after > i and after < len(tokens) and tokens[after].kind == TokenKind.LPAREN:
            if name is None:
                return _refuse(AgentRefusal.UNREADABLE_NAME, "a U& name that cannot be read")
            if (what := _denied_as(name, denied)) is not None:
                return _refuse(AgentRefusal.DENIED_FUNCTION, f"{name} ({what})")
        i = max(after, i + 1)
    return None


def _denied_as(name: str, denied: tuple[tuple[str, str], ...]) -> str | None:
    for pattern, what in denied:
        if (name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern):
            return what
    return None


def _name_at(tokens: list[Token], i: int, dialect: Dialect) -> tuple[str | None, int]:
    """The function name a call at `tokens[i]` would use, as the deny-list is written (lowercase unless a quoted
    PostgreSQL name), and the index after it; (None, i) when no name starts there; (None, j) for a U& name that cannot
    be decoded."""
    token = tokens[i]
    if (dialect == Dialect.POSTGRES and token.kind == TokenKind.WORD and token.upper == "U" and i + 2 < len(tokens)
            and tokens[i + 1].text == "&" and tokens[i + 1].start == token.end
            and tokens[i + 2].kind == TokenKind.QUOTED and tokens[i + 2].start == tokens[i + 1].end):
        body, after, escape = tokens[i + 2], i + 3, "\\"
        if after + 1 < len(tokens) and tokens[after].upper == "UESCAPE" and tokens[after + 1].kind == TokenKind.STRING:
            escape = tokens[after + 1].text[1:-1].replace("''", "'")
            after += 2
        return _unicode_name(_unquoted(body), escape), after
    if token.kind == TokenKind.WORD:
        return token.text.lower(), i + 1
    if token.kind == TokenKind.QUOTED:
        name = _unquoted(token)
        return (name if dialect in _QUOTED_NAMES_KEEP_CASE else name.lower()), i + 1
    return None, i


def _unquoted(token: Token) -> str:
    """A quoted identifier's name: its quotes off, a doubled closing quote as one."""
    close = token.text[-1] if token.terminated else ""
    body = token.text[1:-1] if token.terminated else token.text[1:]
    return body.replace(close * 2, close) if close else body


def _unicode_name(body: str, escape: str) -> str | None:
    """A PostgreSQL `U&"…"` name decoded: `<e>XXXX`, `<e>+XXXXXX` and `<e><e>` (the escape character, `\\` unless
    UESCAPE names another). None when PostgreSQL would refuse it, or the escape is not one character."""
    if len(escape) != 1 or escape in "+'\"" or escape.isspace() or _HEX.fullmatch(escape):
        return None
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != escape:
            out.append(char)
            i += 1
            continue
        if body[i + 1:i + 2] == escape:
            out.append(escape)
            i += 2
            continue
        digits = 6 if body[i + 1:i + 2] == "+" else 4
        start = i + (2 if digits == 6 else 1)
        code = body[start:start + digits]
        if len(code) != digits or not _HEX.fullmatch(code) or int(code, 16) > 0x10FFFF:
            return None
        out.append(chr(int(code, 16)))
        i = start + digits
    return "".join(out)
