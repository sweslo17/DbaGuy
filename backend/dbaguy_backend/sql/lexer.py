import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from .dialects import RULES, Dialect, LexRules


class TokenKind(StrEnum):
    WORD = "word"
    QUOTED = "quoted"
    STRING = "string"
    NUMBER = "number"
    COMMENT = "comment"
    EXEC_COMMENT = "exec_comment"  # MySQL /*! … */, /*+ … */ and MariaDB /*M! … */: never INSIGNIFICANT
    SPACE = "space"
    SEMI = "semi"
    DELIMITER = "delimiter"  # a custom terminator from a MySQL DELIMITER line (only when scan() is given one)
    DOT = "dot"
    COMMA = "comma"
    LPAREN = "lparen"
    RPAREN = "rparen"
    PARAM = "param"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    text: str
    terminated: bool = True  # False for a string, quoted identifier, dollar quote or block comment that never closes

    @property
    def upper(self) -> str:
        return self.text.upper()


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_DOLLAR_PARAM = re.compile(r"\$\d+")
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_PUNCT = {";": TokenKind.SEMI, ".": TokenKind.DOT, ",": TokenKind.COMMA, "(": TokenKind.LPAREN, ")": TokenKind.RPAREN}
# Tokens that never change what a statement means: skipped wherever statements are read word by word.
INSIGNIFICANT = frozenset({TokenKind.SPACE, TokenKind.COMMENT})
# Executable comments (EXEC_COMMENT): the server runs the text of a version comment as code, MySQL's `/*! … */` and
# MariaDB's `/*M! … */` alike (MySQL itself skips the latter; DbaGuy treats MariaDB as MySQL). `/*+ … */` holds
# optimizer hints. Prefixes are case-sensitive, as in the servers.
VERSION_COMMENT_STARTS = ("/*!", "/*M!")
HINT_COMMENT_START = "/*+"
_EXECUTABLE_COMMENT_STARTS = (*VERSION_COMMENT_STARTS, HINT_COMMENT_START)


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) > 127


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def _scan_string(text: str, i: int, quote: str, backslash: bool) -> tuple[int, bool]:
    """i points at the opening quote; returns (index after the closing quote or len(text), whether it closed).
    A doubled quote stands for one; with `backslash`, a backslash escapes the next character."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1, True
        i += 1
    return n, False


def _starts_line_comment(text: str, i: int, rules: LexRules) -> bool:
    if text[i] == "#":
        return rules.hash_comments
    if not text.startswith("--", i):
        return False
    if not rules.dash_comment_needs_space:
        return True
    # The server wants an ASCII space or control character (0x00-0x20, 0x7f) or the end: not U+00A0, not U+2003.
    following = text[i + 2:i + 3]
    return following == "" or ord(following) <= 0x20 or ord(following) == 0x7F


def _cut_at_delimiter(text: str, begin: int, end: int, delimiter: str) -> int:
    """`end`, or where the first `delimiter` starting in (begin, end) begins: the token stops there."""
    cut = text.find(delimiter, begin + 1, end + len(delimiter) - 1)
    return end if cut < 0 else cut


def scan(text: str, dialect: Dialect, start: int = 0, delimiter: str | None = None) -> Iterator[Token]:
    """Tokens of text[start:], lazily; their concatenated texts equal text[start:]. Never raises.

    `delimiter`: a custom statement terminator set by a MySQL `DELIMITER` line. Outside strings, quoted names and
    comments it is one DELIMITER token wherever it begins, even inside what would otherwise be a word or a number
    (`END$$`), which is how the mysql client sees it.
    """
    rules = RULES[dialect]
    quotes = dict(rules.identifier_quotes)
    delimiter = delimiter or None  # an empty terminator would match everywhere without advancing
    n = len(text)
    i = start
    while i < n:
        ch = text[i]
        begin = i
        terminated = True
        if delimiter is not None and text.startswith(delimiter, i):
            i += len(delimiter)
            kind = TokenKind.DELIMITER
        elif ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            kind = TokenKind.SPACE
        elif _starts_line_comment(text, i, rules):
            end = rules.line_comment_end.search(text, i)
            i = n if end is None else end.start()
            kind = TokenKind.COMMENT
        elif text.startswith("/*", i):
            executable = rules.executable_comments and text.startswith(_EXECUTABLE_COMMENT_STARTS, i)
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif rules.nested_block_comments and text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    i += 1
            terminated = depth == 0
            kind = TokenKind.EXEC_COMMENT if executable else TokenKind.COMMENT
        elif ch in rules.string_quotes:
            i, terminated = _scan_string(text, i, ch, rules.backslash_escapes)
            kind = TokenKind.STRING
        elif ch in quotes:
            close = quotes[ch]
            i += 1
            terminated = False
            while i < n:
                if text[i] == close:
                    if close != "]" and i + 1 < n and text[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    terminated = True
                    break
                i += 1
            kind = TokenKind.QUOTED
        elif ch == "$" and rules.dollar_params and (param := _DOLLAR_PARAM.match(text, i)):
            i = param.end()
            kind = TokenKind.PARAM
        elif ch == "$" and rules.dollar_quotes and (tag := _DOLLAR_TAG.match(text, i)) and not (
            i > 0 and _ident_char(text[i - 1])
        ):
            close_at = text.find(tag.group(0), tag.end())
            terminated = close_at >= 0
            i = n if close_at < 0 else close_at + len(tag.group(0))
            kind = TokenKind.STRING
        elif ch == rules.variable_prefix:  # "" never equals a character
            while i < n and text[i] == ch:  # @ or @@
                i += 1
            while i < n and _ident_char(text[i]):
                i += 1
            if delimiter is not None:
                i = _cut_at_delimiter(text, begin, i, delimiter)
            kind = TokenKind.WORD
        elif _ident_start(ch):
            while i < n and _ident_char(text[i]):
                i += 1
            if delimiter is not None:
                i = _cut_at_delimiter(text, begin, i, delimiter)
            if text[begin:i] in rules.escape_string_prefixes and i < n and text[i] == "'":
                i, terminated = _scan_string(text, i, "'", backslash=True)
                kind = TokenKind.STRING
            else:
                kind = TokenKind.WORD
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            match = _NUMBER.match(text, i)
            i = match.end() if match else i + 1
            if delimiter is not None:
                i = _cut_at_delimiter(text, begin, i, delimiter)
            kind = TokenKind.NUMBER
        else:
            i += 1
            kind = _PUNCT.get(ch, TokenKind.OTHER)
        yield Token(kind, begin, i, text[begin:i], terminated)


def tokenize(text: str, dialect: Dialect) -> list[Token]:
    """Lossless tokenizer: the concatenated token texts equal the input. Never raises."""
    return list(scan(text, dialect))


def first_token(text: str, dialect: Dialect) -> Token | None:
    """First token that is neither whitespace nor a comment; stops scanning there. None if there is none.
    A MySQL executable comment (`/*! … */`, `/*+ … */`) is code, so it can be that token."""
    return next((token for token in scan(text, dialect) if token.kind not in INSIGNIFICANT), None)


def first_keyword(text: str, dialect: Dialect) -> str:
    """Upper-cased text of the first token when that token is a WORD; "" otherwise
    (empty text, "(", a quoted identifier, a string or a MySQL executable comment:
    `first_keyword("/*! x */ select 1", MYSQL)` is "")."""
    token = first_token(text, dialect)
    return token.upper if token is not None and token.kind == TokenKind.WORD else ""
