import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from .dialects import RULES, Dialect


class TokenKind(StrEnum):
    WORD = "word"
    QUOTED = "quoted"
    STRING = "string"
    NUMBER = "number"
    COMMENT = "comment"
    SPACE = "space"
    SEMI = "semi"
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
_SKIPPED = frozenset({TokenKind.SPACE, TokenKind.COMMENT})


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) > 127


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def _scan_string(text: str, i: int, backslash: bool) -> tuple[int, bool]:
    """i points at the opening quote; returns (index after the closing quote or len(text), whether it closed)."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1, True
        i += 1
    return n, False


def _scan(text: str, dialect: Dialect) -> Iterator[Token]:
    """Yields tokens lazily; the concatenated token texts equal the input. Never raises."""
    rules = RULES[dialect]
    quotes = dict(rules.identifier_quotes)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        start = i
        terminated = True
        if ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            kind = TokenKind.SPACE
        elif text.startswith("--", i):
            end = rules.line_comment_end.search(text, i)
            i = n if end is None else end.start()
            kind = TokenKind.COMMENT
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif rules.nested_block_comments and text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    i += 1
            terminated = depth == 0
            kind = TokenKind.COMMENT
        elif ch == "'":
            i, terminated = _scan_string(text, i, backslash=False)
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
        elif ch == "$" and rules.dollar_quotes and (param := _DOLLAR_PARAM.match(text, i)):
            i = param.end()
            kind = TokenKind.PARAM
        elif ch == "$" and rules.dollar_quotes and (tag := _DOLLAR_TAG.match(text, i)) and not (
            i > 0 and _ident_char(text[i - 1])
        ):
            close_at = text.find(tag.group(0), tag.end())
            terminated = close_at >= 0
            i = n if close_at < 0 else close_at + len(tag.group(0))
            kind = TokenKind.STRING
        elif _ident_start(ch):
            while i < n and _ident_char(text[i]):
                i += 1
            if text[start:i] in rules.escape_string_prefixes and i < n and text[i] == "'":
                i, terminated = _scan_string(text, i, backslash=True)
                kind = TokenKind.STRING
            else:
                kind = TokenKind.WORD
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            match = _NUMBER.match(text, i)
            i = match.end() if match else i + 1
            kind = TokenKind.NUMBER
        else:
            i += 1
            kind = _PUNCT.get(ch, TokenKind.OTHER)
        yield Token(kind, start, i, text[start:i], terminated)


def tokenize(text: str, dialect: Dialect) -> list[Token]:
    """Lossless tokenizer: the concatenated token texts equal the input. Never raises."""
    return list(_scan(text, dialect))


def first_token(text: str, dialect: Dialect) -> Token | None:
    """First token that is neither whitespace nor a comment; stops scanning there. None if there is none."""
    return next((token for token in _scan(text, dialect) if token.kind not in _SKIPPED), None)


def first_keyword(text: str, dialect: Dialect) -> str:
    """Upper-cased text of the first token when that token is a WORD; "" otherwise
    (empty text, "(", a quoted identifier or a string)."""
    token = first_token(text, dialect)
    return token.upper if token is not None and token.kind == TokenKind.WORD else ""
