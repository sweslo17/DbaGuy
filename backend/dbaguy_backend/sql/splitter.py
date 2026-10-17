import re
from dataclasses import dataclass

from .dialects import RULES, BodyRule, ClientCommand, Dialect
from .lexer import INSIGNIFICANT, Token, TokenKind, scan

_ROUTINES = frozenset({"FUNCTION", "PROCEDURE"})
_TEMPORARY = frozenset({"TEMP", "TEMPORARY"})
_HEAD = 4  # CREATE OR REPLACE FUNCTION: the longest statement head the body rules read
_COMPOUND_OBJECTS = frozenset({"PROCEDURE", "FUNCTION", "TRIGGER", "EVENT"})
_UNCOUNTED_BLOCKS = frozenset({"IF", "LOOP", "WHILE", "REPEAT"})  # their END … closes no counted block
_PROGRAM_HEAD = 16  # significant tokens: CREATE OR REPLACE DEFINER = `u` @ `h` SQL SECURITY INVOKER PROCEDURE …
_DELIMITER_ARGUMENT = re.compile(r"[ \t]+(\S+)[^\n]*")  # after the word DELIMITER: the terminator, then the line


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

    def __init__(self, rule: BodyRule) -> None:
        self._rule = rule
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
        if word == "ATOMIC" and self._rule is BodyRule.BEGIN_ATOMIC:
            after_begin = self._prev is not None and self._prev.kind == TokenKind.WORD and self._prev.upper == "BEGIN"
            return after_begin and self._creates_routine()
        if word == "BEGIN" and self._rule is BodyRule.TRIGGER:
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


def _creates_stored_program(head: list[Token]) -> bool:
    """CREATE [OR REPLACE] [DEFINER = account] [SQL SECURITY {DEFINER|INVOKER}] PROCEDURE|FUNCTION|TRIGGER|EVENT.
    The account is user, 'user'@'host', `user`@`host`, user@host or CURRENT_USER[()], in any quoting."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in head]
    if words[:1] != ["CREATE"]:
        return False
    i = 1
    if words[i:i + 2] == ["OR", "REPLACE"]:
        i += 2
    if words[i:i + 1] == ["DEFINER"]:  # skip "=" and the account, up to the next keyword this grammar allows
        i = next((j for j in range(i + 1, len(words)) if words[j] in _COMPOUND_OBJECTS or words[j] == "SQL"), len(words))
    if words[i:i + 2] == ["SQL", "SECURITY"]:
        i += 3
    return i < len(words) and words[i] in _COMPOUND_OBJECTS


class _CompoundTracker:
    """MySQL stored-program bodies, nested blocks included.

    BEGIN and CASE open a block and END closes one. `END IF|LOOP|WHILE|REPEAT` closes a block that was never counted
    (IF() and REPEAT() are also functions), so the END before such a word is taken back. `END CASE` closes the CASE
    that its END already closed. Labels (`lbl: BEGIN … END lbl`) need nothing special. Only the first BEGIN of a
    stored program opens a body. A word right after a dot is a name, never a keyword (`NEW.begin`, `ev.end`), and
    executable comments are skipped in the program's head (`CREATE /*!50003 DEFINER=… */ PROCEDURE`). A routine
    without BEGIN … END whose statement uses an unqualified column named `begin`, or a body that uses one named `end`,
    still merges with what follows; the server then rejects the merged text (multi-statements are off), so nothing
    runs.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.has_body = False
        self._head: list[Token] = []
        self._after_end = False
        self._after_dot = False

    def feed(self, token: Token) -> None:
        if len(self._head) < _PROGRAM_HEAD and token.kind != TokenKind.EXEC_COMMENT:
            self._head.append(token)
        after_end, after_dot = self._after_end, self._after_dot
        self._after_end, self._after_dot = False, token.kind == TokenKind.DOT
        word = "" if after_dot or token.kind != TokenKind.WORD else token.upper
        if after_end and word in _UNCOUNTED_BLOCKS:
            self.depth += 1  # that END closed an IF/LOOP/WHILE/REPEAT, not a counted block
        elif after_end and word == "CASE":
            pass  # END CASE: its END already closed the CASE
        elif self.depth == 0:
            if word == "BEGIN" and _creates_stored_program(self._head):
                self.depth, self.has_body = 1, True
        elif word in ("BEGIN", "CASE"):
            self.depth += 1
        elif word == "END":
            self.depth -= 1
            self._after_end = True


def _tracker(rule: BodyRule) -> _BodyTracker | _CompoundTracker:
    return _CompoundTracker() if rule is BodyRule.COMPOUND else _BodyTracker(rule)


def _emit(
    spans: list[StatementSpan], text: str, tokens: list[Token], terminator_end: int | None, has_body: bool
) -> None:
    significant = [t for t in tokens if t.kind not in INSIGNIFICANT]
    if not significant:
        return
    start, end = significant[0].start, significant[-1].end
    terminator = terminator_end if terminator_end is not None else end
    spans.append(StatementSpan(start, end, terminator, text[start:end], has_body))


def _delimiter_line(text: str, token: Token, current: list[Token]) -> re.Match[str] | None:
    """A MySQL `DELIMITER x` line: the word DELIMITER is first on its line and no statement has started."""
    if token.kind != TokenKind.WORD or token.upper != "DELIMITER":
        return None
    if any(t.kind not in INSIGNIFICANT for t in current):
        return None
    line_start = text.rfind("\n", 0, token.start) + 1
    if text[line_start:token.start].strip(" \t"):
        return None
    return _DELIMITER_ARGUMENT.match(text, token.end)


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    rules = RULES[dialect]
    commands = rules.client_command is ClientCommand.DELIMITER
    spans: list[StatementSpan] = []
    delimiter: str | None = None  # set by a DELIMITER line; None: `;` ends statements (outside bodies)
    start = 0
    while True:
        tracker = _tracker(rules.body_rule)
        current: list[Token] = []
        restart: int | None = None
        for token in scan(text, dialect, start, delimiter):
            if commands and (line := _delimiter_line(text, token, current)):
                delimiter = None if line.group(1) == ";" else line.group(1)
                restart = line.end()  # the line is consumed: it belongs to no statement and never reaches the server
                break
            if token.kind == TokenKind.DELIMITER or (
                delimiter is None and token.kind == TokenKind.SEMI and tracker.depth == 0
            ):
                _emit(spans, text, current, token.end, tracker.has_body)
                current, tracker = [], _tracker(rules.body_rule)
                continue
            current.append(token)
            if token.kind not in INSIGNIFICANT:
                tracker.feed(token)  # also under a custom delimiter: has_body tells the classifier about bodies
        if restart is None:
            _emit(spans, text, current, None, tracker.has_body)
            return spans
        start = restart
