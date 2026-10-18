"""Did the formatter change what a statement means? (spec §3.3.7; R-04-12) sql-formatter splits `'it''s'` into two
strings for n1ql, and nothing stops a future formatter from doing worse in any dialect, so its output replaces the
text only when the significant tokens are the same. Pure; offsets are code points."""

from .dialects import CASE_SENSITIVE_IDENTIFIERS, Dialect, is_reserved
from .lexer import Token, TokenKind, tokenize

_LAYOUT_ONLY = frozenset({TokenKind.COMMENT, TokenKind.EXEC_COMMENT})


def _significant(text: str, dialect: Dialect) -> list[Token]:
    return [t for t in tokenize(text, dialect) if t.kind != TokenKind.SPACE]


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _case_free(tokens: list[Token], i: int, dialect: Dialect) -> bool:
    """A word whose case means nothing: everywhere but in dialects with case-sensitive names, where only reserved
    words and function names (a word right before `(`) are free."""
    if dialect not in CASE_SENSITIVE_IDENTIFIERS or is_reserved(dialect, tokens[i].text):
        return True
    following = next((t for t in tokens[i + 1:] if t.kind not in _LAYOUT_ONLY), None)
    return following is not None and following.kind == TokenKind.LPAREN


def _same(before: list[Token], after: list[Token], i: int, dialect: Dialect) -> bool:
    a, b = before[i], after[i]
    if a.kind != b.kind:
        return False
    if a.kind in _LAYOUT_ONLY:
        return _squeezed(a.text) == _squeezed(b.text)
    if a.kind == TokenKind.WORD and _case_free(before, i, dialect):
        return a.text.upper() == b.text.upper()
    return a.text == b.text


def first_difference(dialect: Dialect, original: str, formatted: str) -> int | None:
    """The code-point offset in `original` of the first significant token the formatter changed (or `len(original)`
    when it only added tokens); None when the texts differ only in whitespace, word case and comment layout."""
    before, after = _significant(original, dialect), _significant(formatted, dialect)
    for i in range(min(len(before), len(after))):
        if not _same(before, after, i, dialect):
            return before[i].start
    if len(before) > len(after):
        return before[len(after)].start
    return len(original) if len(after) > len(before) else None
