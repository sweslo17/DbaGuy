from dataclasses import dataclass

from .dialects import RULES, Dialect
from .lexer import INSIGNIFICANT, Token, TokenKind, tokenize

_ROUTINES = frozenset({"FUNCTION", "PROCEDURE"})
_TEMPORARY = frozenset({"TEMP", "TEMPORARY"})
_HEAD = 4  # CREATE OR REPLACE FUNCTION: the longest statement head the body rules read


@dataclass(frozen=True, slots=True)
class StatementSpan:
    start: int
    end: int
    terminator_end: int
    text: str
    has_body: bool = False  # a routine or trigger body whose inner `;` did not end the statement


class _BodyTracker:
    """Tracks routine and trigger bodies, whose inner semicolons do not end the statement:

    - PostgreSQL: `CREATE [OR REPLACE] FUNCTION|PROCEDURE ... BEGIN ATOMIC ... END`, with BEGIN and ATOMIC adjacent;
    - SQLite: `CREATE [TEMP|TEMPORARY] TRIGGER ... BEGIN ... END`.

    Anything else (a column named begin, atomic or trigger, a transaction's BEGIN) opens nothing. Fed the significant
    tokens of one statement.
    """

    def __init__(self, kinds: frozenset[str]) -> None:
        self._kinds = kinds
        self.reset()

    def reset(self) -> None:
        self.depth = 0
        self.has_body = False
        self._case_depth = 0
        self._head: list[str] = []  # the statement's first significant tokens: upper-cased words, "" for others
        self._prev: Token | None = None

    def feed(self, token: Token) -> None:
        word = token.upper if token.kind == TokenKind.WORD else ""
        if len(self._head) < _HEAD:
            self._head.append(word)
        if word:
            self._word(word)
        self._prev = token

    def _word(self, word: str) -> None:
        if self.depth == 0:
            if self._opens_body(word):
                self.depth = 1
                self.has_body = True
        elif word == "CASE":
            self._case_depth += 1
        elif word == "END":
            if self._case_depth:
                self._case_depth -= 1
            else:
                self.depth -= 1

    def _opens_body(self, word: str) -> bool:
        if word == "ATOMIC" and "begin_atomic" in self._kinds:
            after_begin = self._prev is not None and self._prev.kind == TokenKind.WORD and self._prev.upper == "BEGIN"
            return after_begin and self._creates_routine()
        if word == "BEGIN" and "trigger" in self._kinds:
            return self._creates_trigger()
        return False

    def _head_word(self, index: int) -> str:
        return self._head[index] if index < len(self._head) else ""

    def _creates_routine(self) -> bool:
        """CREATE [OR REPLACE] FUNCTION|PROCEDURE"""
        created = self._head_word(3) if self._head[1:3] == ["OR", "REPLACE"] else self._head_word(1)
        return self._head_word(0) == "CREATE" and created in _ROUTINES

    def _creates_trigger(self) -> bool:
        """CREATE [TEMP|TEMPORARY] TRIGGER"""
        created = self._head_word(2) if self._head_word(1) in _TEMPORARY else self._head_word(1)
        return self._head_word(0) == "CREATE" and created == "TRIGGER"


def _emit(
    spans: list[StatementSpan], text: str, tokens: list[Token], terminator_end: int | None, has_body: bool
) -> None:
    significant = [t for t in tokens if t.kind not in INSIGNIFICANT]
    if not significant:
        return
    start, end = significant[0].start, significant[-1].end
    terminator = terminator_end if terminator_end is not None else end
    spans.append(StatementSpan(start, end, terminator, text[start:end], has_body))


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    tracker = _BodyTracker(RULES[dialect].block_bodies)
    spans: list[StatementSpan] = []
    current: list[Token] = []
    for token in tokenize(text, dialect):
        if token.kind == TokenKind.SEMI and tracker.depth == 0:
            _emit(spans, text, current, token.end, tracker.has_body)
            current = []
            tracker.reset()
            continue
        current.append(token)
        if token.kind not in INSIGNIFICANT:
            tracker.feed(token)
    _emit(spans, text, current, None, tracker.has_body)
    return spans
