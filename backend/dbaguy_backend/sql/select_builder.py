"""The SELECT behind the table-data view: a relation from the object tree, plus an optional filter and sort."""

from collections.abc import Sequence

from pydantic import BaseModel

from ..domain.errors import ValidationFailed
from ..domain.metadata import NodeKind, PathSegment
from .dialects import Dialect, qualified, quote_ident
from .lexer import TokenKind, tokenize

_RELATION_KINDS = frozenset({NodeKind.TABLE, NodeKind.VIEW, NodeKind.MATERIALIZED_VIEW, NodeKind.PARTITION})
_INSIGNIFICANT = frozenset({TokenKind.SPACE, TokenKind.COMMENT})


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
    """The filter without trailing `;` and comments; a `;` anywhere else (outside literals) would be a second statement."""
    text = (where or "").strip()
    tokens = [token for token in tokenize(text, dialect) if token.kind not in _INSIGNIFICANT]
    while tokens and tokens[-1].kind == TokenKind.SEMI:
        tokens.pop()
    if any(token.kind == TokenKind.SEMI for token in tokens):
        raise ValidationFailed("A filter must be a single expression")
    return text[: tokens[-1].end] if tokens else None


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
