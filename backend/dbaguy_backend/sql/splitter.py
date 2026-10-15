from dataclasses import dataclass

from .dialects import RULES, Dialect
from .lexer import Token, TokenKind, tokenize

_INSIGNIFICANT = (TokenKind.SPACE, TokenKind.COMMENT)


@dataclass(frozen=True, slots=True)
class StatementSpan:
    start: int
    end: int
    terminator_end: int
    text: str


class _BodyTracker:
    """Tracks routine/trigger bodies whose inner semicolons must not split the statement."""

    def __init__(self, kinds: frozenset[str]) -> None:
        self._kinds = kinds
        self.reset()

    def reset(self) -> None:
        self.depth = 0
        self._case_depth = 0
        self._first_word = ""
        self._prev_word = ""
        self._is_trigger = False

    def feed(self, token: Token) -> None:
        if token.kind != TokenKind.WORD:
            return
        word = token.upper
        if self.depth == 0:
            if not self._first_word:
                self._first_word = word
            if word == "TRIGGER" and self._first_word == "CREATE":
                self._is_trigger = True
            if word == "BEGIN" and "trigger" in self._kinds and self._is_trigger:
                self.depth = 1
            elif word == "ATOMIC" and self._prev_word == "BEGIN" and "begin_atomic" in self._kinds:
                self.depth = 1
        elif word == "CASE":
            self._case_depth += 1
        elif word == "END":
            if self._case_depth:
                self._case_depth -= 1
            else:
                self.depth -= 1
        self._prev_word = word


def _emit(spans: list[StatementSpan], text: str, tokens: list[Token], terminator_end: int | None) -> None:
    significant = [t for t in tokens if t.kind not in _INSIGNIFICANT]
    if not significant:
        return
    start, end = significant[0].start, significant[-1].end
    spans.append(StatementSpan(start, end, terminator_end if terminator_end is not None else end, text[start:end]))


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    tracker = _BodyTracker(RULES[dialect].block_bodies)
    spans: list[StatementSpan] = []
    current: list[Token] = []
    for token in tokenize(text, dialect):
        if token.kind == TokenKind.SEMI and tracker.depth == 0:
            _emit(spans, text, current, token.end)
            current = []
            tracker.reset()
            continue
        current.append(token)
        tracker.feed(token)
    _emit(spans, text, current, None)
    return spans
