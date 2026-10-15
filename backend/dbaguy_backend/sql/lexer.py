import re
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

    @property
    def upper(self) -> str:
        return self.text.upper()


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
_DOLLAR_PARAM = re.compile(r"\$\d+")
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_PUNCT = {";": TokenKind.SEMI, ".": TokenKind.DOT, ",": TokenKind.COMMA, "(": TokenKind.LPAREN, ")": TokenKind.RPAREN}


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) > 127


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def _scan_string(text: str, i: int, backslash: bool) -> int:
    """i points at the opening quote; returns the index after the closing quote (or len(text))."""
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
            return i + 1
        i += 1
    return n


def tokenize(text: str, dialect: Dialect) -> list[Token]:
    """Lossless tokenizer: the concatenated token texts equal the input. Never raises."""
    rules = RULES[dialect]
    quotes = dict(rules.identifier_quotes)
    tokens: list[Token] = []
    n = len(text)
    i = 0

    def add(kind: TokenKind, start: int, end: int) -> None:
        tokens.append(Token(kind, start, end, text[start:end]))

    while i < n:
        ch = text[i]
        start = i
        if ch.isspace():
            while i < n and text[i].isspace():
                i += 1
            add(TokenKind.SPACE, start, i)
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            add(TokenKind.COMMENT, start, i)
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                elif rules.nested_block_comments and text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                else:
                    i += 1
            add(TokenKind.COMMENT, start, i)
        elif ch == "'":
            i = _scan_string(text, i, backslash=False)
            add(TokenKind.STRING, start, i)
        elif ch in quotes:
            close = quotes[ch]
            i += 1
            while i < n:
                if text[i] == close:
                    if close != "]" and i + 1 < n and text[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            add(TokenKind.QUOTED, start, i)
        elif ch == "$" and rules.dollar_quotes and (param := _DOLLAR_PARAM.match(text, i)):
            i = param.end()
            add(TokenKind.PARAM, start, i)
        elif ch == "$" and rules.dollar_quotes and (tag := _DOLLAR_TAG.match(text, i)) and not (
            i > 0 and _ident_char(text[i - 1])
        ):
            close_at = text.find(tag.group(0), tag.end())
            i = n if close_at < 0 else close_at + len(tag.group(0))
            add(TokenKind.STRING, start, i)
        elif _ident_start(ch):
            while i < n and _ident_char(text[i]):
                i += 1
            if text[start:i] in rules.escape_string_prefixes and i < n and text[i] == "'":
                i = _scan_string(text, i, backslash=True)
                add(TokenKind.STRING, start, i)
            else:
                add(TokenKind.WORD, start, i)
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            match = _NUMBER.match(text, i)
            i = match.end() if match else i + 1
            add(TokenKind.NUMBER, start, i)
        else:
            i += 1
            add(_PUNCT.get(ch, TokenKind.OTHER), start, i)
    return tokens


def first_keyword(text: str, dialect: Dialect) -> str:
    for token in tokenize(text, dialect):
        if token.kind == TokenKind.WORD:
            return token.upper
        if token.kind not in (TokenKind.SPACE, TokenKind.COMMENT):
            return ""
    return ""
