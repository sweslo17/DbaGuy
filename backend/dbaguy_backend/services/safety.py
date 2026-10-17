from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel

from ..domain.classification import Classification, GuardReason, StatementKind
from ..domain.profiles import SafetyLevel
from ..sql.classifier import classify
from ..sql.dialects import Dialect
from ..sql.lexer import INSIGNIFICANT, Token, TokenKind, tokenize
from ..sql.splitter import split

_SET_CONFIG = "SET_CONFIG"  # a SELECT can call it to switch default_transaction_read_only off
# Settings a read-only PostgreSQL session may still change: they shape how statements run and look, never whether
# they may write. (READ ONLY is re-asserted before every statement and a manual-mode BEGIN is READ ONLY anyway.)
_PG_HARMLESS_SETTINGS = frozenset({
    "search_path", "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout", "timezone",
    "datestyle", "intervalstyle", "work_mem", "application_name", "client_min_messages", "extra_float_digits",
    "bytea_output",
})
# At the strict level every statement that is not a read is confirmed, naming its kind.
_STRICT_REASON: dict[StatementKind, GuardReason] = {
    StatementKind.WRITE: GuardReason.STRICT_WRITE,
    StatementKind.DDL: GuardReason.STRICT_DDL,
    StatementKind.TRANSACTION: GuardReason.STRICT_TRANSACTION,
    StatementKind.SESSION: GuardReason.STRICT_SESSION,
    StatementKind.UNKNOWN: GuardReason.STRICT_UNKNOWN,
}


class GuardAction(StrEnum):
    RUN = "run"
    CONFIRM = "confirm"
    BLOCK = "block"


class GuardDecision(BaseModel):
    action: GuardAction
    reasons: list[GuardReason] = []
    classification: Classification


class SafetyPolicy:
    """Decides, per statement, whether it runs, needs the user's confirmation first, or is blocked."""

    def check(
        self, sql: str, dialect: Dialect, level: SafetyLevel, read_only: bool, confirmed: bool = False
    ) -> GuardDecision:
        return self.decide(sql, classify(sql, dialect), dialect, level, read_only, confirmed)

    def decide(
        self, sql: str, classification: Classification, dialect: Dialect, level: SafetyLevel, read_only: bool,
        confirmed: bool = False,
    ) -> GuardDecision:
        if read_only and not self._allowed_when_read_only(sql, classification, dialect):
            return GuardDecision(
                action=GuardAction.BLOCK, reasons=[GuardReason.READ_ONLY], classification=classification
            )
        reasons = self._confirmation_reasons(classification, level)
        if reasons and not confirmed:
            return GuardDecision(action=GuardAction.CONFIRM, reasons=reasons, classification=classification)
        return GuardDecision(action=GuardAction.RUN, classification=classification)

    @staticmethod
    def _allowed_when_read_only(sql: str, classification: Classification, dialect: Dialect) -> bool:
        tokens = tokenize(sql, dialect)
        # set_config() can turn default_transaction_read_only off from inside a SELECT: block it wherever it appears.
        if any(t.kind in (TokenKind.WORD, TokenKind.QUOTED) and t.upper.strip('"') == _SET_CONFIG for t in tokens):
            return False
        if classification.kind == StatementKind.READ:
            return True
        if classification.kind == StatementKind.TRANSACTION:
            return not any(t.kind == TokenKind.WORD and t.upper == "WRITE" for t in tokens)
        if classification.kind == StatementKind.SESSION:
            check = _HARMLESS_SESSION.get(dialect)
            return check is not None and check(sql)
        return False

    @staticmethod
    def _confirmation_reasons(classification: Classification, level: SafetyLevel) -> list[GuardReason]:
        if level == "relaxed":
            return []
        dangers = [GuardReason(reason) for reason in classification.dangerous_reasons]
        if level == "strict" and classification.kind != StatementKind.READ:
            return dangers or [_STRICT_REASON[classification.kind]]
        return dangers


def _single_statement_tokens(sql: str, dialect: Dialect) -> list[Token] | None:
    """The significant tokens of `sql` when it is exactly one statement, else None.

    A `;` still inside the span (a MySQL custom DELIMITER line can make that happen) means more than one statement,
    whatever the splitter says; only trailing ones are dropped.
    """
    spans = split(sql, dialect)
    if len(spans) != 1:
        return None
    tokens = [t for t in tokenize(spans[0].text, dialect) if t.kind not in INSIGNIFICANT]
    while tokens and tokens[-1].kind == TokenKind.SEMI:
        tokens.pop()
    return None if any(t.kind == TokenKind.SEMI for t in tokens) else tokens


def _keywords(tokens: list[Token]) -> list[str]:
    """One entry per token: its upper-cased text if it is a word, "" otherwise (so positions line up with `tokens`)."""
    return [t.upper if t.kind == TokenKind.WORD else "" for t in tokens]


def _setting_name(token: Token) -> str:
    """A configuration parameter's name as PostgreSQL compares it: case-insensitive, quotes or not; "" if not a name."""
    if token.kind == TokenKind.WORD:
        return token.text.lower()
    return token.text.strip('"').lower() if token.kind == TokenKind.QUOTED else ""


def _harmless_pg_setting(sql: str) -> bool:
    """One PostgreSQL statement that changes only a harmless setting: `SET [SESSION|LOCAL] <name> {TO|=} <value>`,
    `SET [SESSION|LOCAL] TIME ZONE <value>` or `RESET <name>`, `RESET TIME ZONE`.

    Never SET TRANSACTION, SET SESSION CHARACTERISTICS, SET ROLE, SET SESSION AUTHORIZATION, RESET ALL or a setting
    outside the list (notably transaction_read_only and default_transaction_read_only).
    """
    tokens = _single_statement_tokens(sql, Dialect.POSTGRES)
    if tokens is None:
        return False
    words = _keywords(tokens)
    if words[:1] == ["RESET"]:
        named = len(tokens) == 2 and _setting_name(tokens[1]) in _PG_HARMLESS_SETTINGS
        return named or words[1:] == ["TIME", "ZONE"]
    if words[:1] != ["SET"]:
        return False
    rest = 2 if words[1:2] in (["SESSION"], ["LOCAL"]) else 1
    if words[rest:rest + 2] == ["TIME", "ZONE"]:
        return len(tokens) > rest + 2  # a value follows
    if rest >= len(tokens) or _setting_name(tokens[rest]) not in _PG_HARMLESS_SETTINGS:
        return False
    assign = tokens[rest + 1] if rest + 1 < len(tokens) else None
    assigns = assign is not None and (assign.upper == "TO" or (assign.kind == TokenKind.OTHER and assign.text == "="))
    return assigns and len(tokens) > rest + 2  # a value follows


# Session variables that shape how statements run or look, never whether they may write.
_MYSQL_HARMLESS_VARIABLES = frozenset({
    # NO_BACKSLASH_ESCAPES and ANSI_QUOTES make the lexer (which assumes the default mode) and the server disagree
    # about strings; that is accepted (plan Ruling 25).
    "sql_mode",
    "time_zone",
    "autocommit",  # the adapter recalibrates its autocommit state from the server
    # Not character_set_results (R-03a-F3): the adapter always decodes results as UTF-8, so another charset garbles
    # every result until it is reset.
    "group_concat_max_len", "max_execution_time", "sql_select_limit", "lc_time_names",
})
_MYSQL_SESSION_SCOPES = frozenset({"SESSION", "LOCAL"})


def _top_level_items(tokens: list[Token]) -> list[list[Token]]:
    """`tokens` split at commas outside parentheses."""
    items: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        depth += (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN)
        if token.kind == TokenKind.COMMA and depth == 0:
            items.append([])
        else:
            items[-1].append(token)
    return items


def _harmless_mysql_assignment(tokens: list[Token]) -> bool:
    """One item of a MySQL SET: NAMES …, CHARACTER SET …, CHARSET …, a user variable (`@x = …`), or one of the
    harmless session variables (`[SESSION|LOCAL] name`, `@@name`, `@@session.name`, `@@local.name`), with `=` or `:=`."""
    words = _keywords(tokens)
    if words[:1] in (["NAMES"], ["CHARSET"]):
        return len(tokens) > 1
    if words[:2] == ["CHARACTER", "SET"]:
        return len(tokens) > 2
    operator = next((k for k, t in enumerate(tokens) if t.kind == TokenKind.OTHER and t.text == "="), None)
    if operator is None or operator + 1 >= len(tokens):
        return False  # not an assignment, or nothing assigned
    target = tokens[:operator - 1] if operator > 0 and tokens[operator - 1].text == ":" else tokens[:operator]
    if target and target[0].upper in _MYSQL_SESSION_SCOPES:
        target = target[1:]
    name = "".join(t.text for t in target).lower()
    if name.startswith("@") and not name.startswith("@@"):
        return True  # a user variable
    for prefix in ("@@session.", "@@local.", "@@"):
        if name.startswith(prefix):
            name = name.removeprefix(prefix)
            break
    return name in _MYSQL_HARMLESS_VARIABLES


def _harmless_mysql_session(sql: str) -> bool:
    """One MySQL statement that only shapes how statements run or look (spec §3.4): `USE db`, or a SET whose every
    item is harmless. The only thing it allows about transactions is `SET [SESSION|LOCAL] TRANSACTION READ ONLY`,
    which can only make things stricter; never `SET … TRANSACTION …` otherwise, transaction_read_only, GLOBAL or
    PERSIST scopes, or other system variables."""
    tokens = _single_statement_tokens(sql, Dialect.MYSQL)
    if tokens is None:
        return False
    words = _keywords(tokens)
    if words[:1] == ["USE"]:
        return len(tokens) == 2 and tokens[1].kind in (TokenKind.WORD, TokenKind.QUOTED)
    if words[:1] != ["SET"] or any(t.kind == TokenKind.EXEC_COMMENT for t in tokens):
        return False
    scoped = 2 if words[1:2] and words[1] in _MYSQL_SESSION_SCOPES else 1
    if words[scoped:] == ["TRANSACTION", "READ", "ONLY"]:
        return True
    if "TRANSACTION" in words:
        return False
    return all(_harmless_mysql_assignment(item) for item in _top_level_items(tokens[1:]))


_HARMLESS_SESSION: dict[Dialect, Callable[[str], bool]] = {
    Dialect.POSTGRES: _harmless_pg_setting,
    Dialect.MYSQL: _harmless_mysql_session,
}  # a dialect without an entry (SQLite) allows no SESSION statement on a read-only connection
