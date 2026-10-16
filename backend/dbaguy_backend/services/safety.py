from enum import StrEnum

from pydantic import BaseModel

from ..domain.classification import Classification, GuardReason, StatementKind
from ..domain.profiles import SafetyLevel
from ..sql.classifier import classify
from ..sql.dialects import Dialect
from ..sql.lexer import TokenKind, tokenize

_SET_CONFIG = "SET_CONFIG"  # a SELECT can call it to switch default_transaction_read_only off
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
        return False

    @staticmethod
    def _confirmation_reasons(classification: Classification, level: SafetyLevel) -> list[GuardReason]:
        if level == "relaxed":
            return []
        dangers = [GuardReason(reason) for reason in classification.dangerous_reasons]
        if level == "strict" and classification.kind != StatementKind.READ:
            return dangers or [_STRICT_REASON[classification.kind]]
        return dangers
