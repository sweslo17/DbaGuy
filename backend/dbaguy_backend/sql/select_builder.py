"""The SELECT behind the table-data view: a relation from the object tree, plus an optional filter and sort."""

from collections.abc import Sequence

from pydantic import BaseModel

from ..domain.errors import ValidationFailed
from ..domain.metadata import NodeKind, PathSegment
from .dialects import Dialect, qualified, quote_ident
from .lexer import INSIGNIFICANT, TokenKind, tokenize

_RELATION_KINDS = frozenset({NodeKind.TABLE, NodeKind.VIEW, NodeKind.MATERIALIZED_VIEW, NodeKind.PARTITION})
_TRAILING = INSIGNIFICANT | {TokenKind.SEMI}


class SortKey(BaseModel):
    column: str
    descending: bool = False


def relation_from_path(path: Sequence[PathSegment]) -> tuple[str | None, str]:
    """(schema, name) of the table-like node a tree path points at (also from one of its columns or indexes)."""
    name = next((segment.name for segment in reversed(path) if segment.kind in _RELATION_KINDS), None)
    if name is None:
        raise ValidationFailed("This object has no data to browse", detail={"path": [s.model_dump() for s in path]})
    schema = next((segment.name for segment in reversed(path) if segment.kind == NodeKind.SCHEMA), None)
    return schema, name


def _single_expression(where: str | None, dialect: Dialect) -> str | None:
    """The filter rebuilt from its tokens so that it cannot leave its place in our SELECT.

    - Trailing `;`, whitespace and comments are dropped; any other `;` outside literals would start a second statement.
    - Every comment becomes one space, so what we send never depends on where the database ends a comment
      (PostgreSQL ends `--` at CR as well as LF).
    - An unterminated string, quoted identifier or block comment would swallow the ORDER BY appended after it: rejected.
    """
    tokens = tokenize(where or "", dialect)
    if not all(token.terminated for token in tokens):
        raise ValidationFailed("The filter has an unterminated string, quoted name or comment")
    while tokens and tokens[-1].kind in _TRAILING:
        tokens.pop()
    if any(token.kind == TokenKind.SEMI for token in tokens):
        raise ValidationFailed("A filter must be a single expression")
    return "".join(" " if token.kind == TokenKind.COMMENT else token.text for token in tokens).strip() or None


def build_browse_select(
    dialect: Dialect, path: Sequence[PathSegment], where: str | None, order_by: Sequence[SortKey], count: bool = False
) -> str:
    schema, name = relation_from_path(path)
    sql = f"SELECT {'count(*)' if count else '*'} FROM {qualified(dialect, schema, name)}"
    if condition := _single_expression(where, dialect):
        sql += f"\nWHERE {condition}"
    if order_by and not count:
        keys = ", ".join(f"{quote_ident(dialect, key.column)} {'DESC' if key.descending else 'ASC'}" for key in order_by)
        sql += f"\nORDER BY {keys}"
    return sql
