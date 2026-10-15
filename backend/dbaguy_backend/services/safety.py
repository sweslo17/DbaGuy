from enum import StrEnum

from pydantic import BaseModel

from ..domain.profiles import SafetyLevel
from ..sql.classifier import Classification, StatementKind, classify
from ..sql.dialects import Dialect
from ..sql.lexer import TokenKind, tokenize

READ_ONLY = "read_only"


class GuardAction(StrEnum):
    RUN = "run"
    CONFIRM = "confirm"
    BLOCK = "block"


class GuardDecision(BaseModel):
    action: GuardAction
    reasons: list[str] = []
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
            return GuardDecision(action=GuardAction.BLOCK, reasons=[READ_ONLY], classification=classification)
        reasons = self._confirmation_reasons(classification, level)
        if reasons and not confirmed:
            return GuardDecision(action=GuardAction.CONFIRM, reasons=reasons, classification=classification)
        return GuardDecision(action=GuardAction.RUN, classification=classification)

    @staticmethod
    def _allowed_when_read_only(sql: str, classification: Classification, dialect: Dialect) -> bool:
        if classification.kind == StatementKind.READ:
            return True
        if classification.kind == StatementKind.TRANSACTION:
            return not any(t.kind == TokenKind.WORD and t.upper == "WRITE" for t in tokenize(sql, dialect))
        return False

    @staticmethod
    def _confirmation_reasons(classification: Classification, level: SafetyLevel) -> list[str]:
        if level == "relaxed":
            return []
        if level == "strict" and classification.kind != StatementKind.READ:
            return list(classification.dangerous_reasons) or [f"strict_{classification.kind}"]
        return list(classification.dangerous_reasons)
