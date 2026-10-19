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


# Reserved in SQL++ 7.6 yet accepted by the server as plain names (field, alias, keyspace), so their case matters.
_NAME_LIKE_RESERVED: dict[Dialect, frozenset[str]] = {
    Dialect.SQLPP: frozenset({"cycle", "default", "sequence", "user", "users", "vector"}),
}
# A word right before `(` is a function name, except where it names a keyspace: `INSERT INTO Users (KEY, VALUE)`.
_NOT_A_CALL_AFTER = frozenset({"INTO", "ON", "FROM", "JOIN", "UPDATE", "MERGE", "KEYSPACE"})


def _neighbours(tokens: list[Token]) -> tuple[list[Token | None], list[Token | None]]:
    """For each token, the previous and the next token that is not a comment (one pass each way, no slicing)."""
    before: list[Token | None] = []
    last: Token | None = None
    for t in tokens:
        before.append(last)
        if t.kind not in _LAYOUT_ONLY:
            last = t
    after: list[Token | None] = [None] * len(tokens)
    last = None
    for i in range(len(tokens) - 1, -1, -1):
        after[i] = last
        if tokens[i].kind not in _LAYOUT_ONLY:
            last = tokens[i]
    return before, after


def _case_free(word: Token, previous: Token | None, following: Token | None, dialect: Dialect) -> bool:
    """A word whose case means nothing: everywhere but in dialects with case-sensitive names, where only reserved
    words that cannot be names and function names (a word right before `(` that does not name a keyspace) are free."""
    if dialect not in CASE_SENSITIVE_IDENTIFIERS:
        return True
    if is_reserved(dialect, word.text) and word.text.lower() not in _NAME_LIKE_RESERVED.get(dialect, ()):
        return True
    if following is None or following.kind != TokenKind.LPAREN:
        return False
    return previous is None or previous.kind != TokenKind.DOT and not (
        previous.kind == TokenKind.WORD and previous.text.upper() in _NOT_A_CALL_AFTER
    )


def _same(a: Token, b: Token, previous: Token | None, following: Token | None, dialect: Dialect) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind in _LAYOUT_ONLY:
        return _squeezed(a.text) == _squeezed(b.text)
    if a.kind == TokenKind.WORD and _case_free(a, previous, following, dialect):
        return a.text.upper() == b.text.upper()
    return a.text == b.text


def first_difference(dialect: Dialect, original: str, formatted: str) -> int | None:
    """The code-point offset in `original` of the first significant token the formatter changed (or `len(original)`
    when it only added tokens); None when the texts differ only in whitespace, word case and comment layout."""
    before, after = _significant(original, dialect), _significant(formatted, dialect)
    previous, following = _neighbours(before)
    for i in range(min(len(before), len(after))):
        if not _same(before[i], after[i], previous[i], following[i], dialect):
            return before[i].start
    if len(before) > len(after):
        return before[len(after)].start
    return len(original) if len(after) > len(before) else None
