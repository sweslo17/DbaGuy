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
            return _harmless_pg_setting(sql, dialect)
        return False

    @staticmethod
    def _confirmation_reasons(classification: Classification, level: SafetyLevel) -> list[GuardReason]:
        if level == "relaxed":
            return []
        dangers = [GuardReason(reason) for reason in classification.dangerous_reasons]
        if level == "strict" and classification.kind != StatementKind.READ:
            return dangers or [_STRICT_REASON[classification.kind]]
        return dangers


def _setting_name(token: Token) -> str:
    """A configuration parameter's name as PostgreSQL compares it: case-insensitive, quotes or not; "" if not a name."""
    if token.kind == TokenKind.WORD:
        return token.text.lower()
    return token.text.strip('"').lower() if token.kind == TokenKind.QUOTED else ""


def _harmless_pg_setting(sql: str, dialect: Dialect) -> bool:
    """One PostgreSQL statement that changes only a harmless setting: `SET [SESSION|LOCAL] <name> {TO|=} <value>`,
    `SET [SESSION|LOCAL] TIME ZONE <value>` or `RESET <name>`, `RESET TIME ZONE`.

    Never SET TRANSACTION, SET SESSION CHARACTERISTICS, SET ROLE, SET SESSION AUTHORIZATION, RESET ALL or a setting
    outside the list (notably transaction_read_only and default_transaction_read_only).
    """
    if dialect != Dialect.POSTGRES:
        return False
    spans = split(sql, dialect)
    if len(spans) != 1:
        return False
    tokens = [t for t in tokenize(spans[0].text, dialect) if t.kind not in INSIGNIFICANT]
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in tokens]
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
