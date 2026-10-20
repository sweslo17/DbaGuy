"""The SELECT behind the table-data view: a relation from the object tree, plus an optional filter and sort; and the
SELECT a dump (and later a copy) reads a relation's rows with."""

from collections.abc import Sequence

from pydantic import BaseModel

from ..adapters.columns import TableColumn
from ..domain.errors import ValidationFailed
from ..domain.metadata import NodeKind, PathSegment
from .dialects import Dialect, qualified, quote_ident
from .lexer import INSIGNIFICANT, TokenKind, tokenize
from .tsql import statements

_RELATION_KINDS = frozenset({NodeKind.TABLE, NodeKind.VIEW, NodeKind.MATERIALIZED_VIEW, NodeKind.PARTITION})
_COLLECTIONS = frozenset({NodeKind.COLLECTION})
_TRAILING = INSIGNIFICANT | {TokenKind.SEMI}
# What a filter is, and the one text of every refusal that it is more (a dump's own check says it too)
FILTER_NOT_A_CONDITION = "The filter must be a condition on the table's rows (what follows WHERE), and nothing else"
# Words that end a WHERE clause, outside parentheses: another clause of the SELECT (GROUP BY, HAVING, WINDOW, QUALIFY,
# ORDER BY, LIMIT, OFFSET, FETCH, FOR UPDATE / FOR XML / FOR JSON, INTO, RETURNING) or another query joined to it
# (UNION, INTERSECT, EXCEPT). A filter is one condition (Task 9 fix round 2), so none may stand at depth 0. Inside
# parentheses they are the filter's own (`substring(x FOR 3)`, `x IN (SELECT … ORDER BY y LIMIT 1)`); quoted they are
# names, in strings and comments text: the lexer tells them apart
_ENDS_A_CONDITION = frozenset({
    "UNION", "INTERSECT", "EXCEPT", "FOR", "INTO", "ORDER", "LIMIT", "OFFSET", "FETCH", "GROUP", "HAVING", "WINDOW",
    "QUALIFY", "RETURNING",
})
# … and the dialects' own, where the word is reserved there (so never an unquoted name): MySQL's LOCK IN SHARE MODE,
# PROCEDURE ANALYSE, MariaDB's MINUS, MySQL's and T-SQL's OPTION (T-SQL's query hints). PostgreSQL and SQLite take
# `option`, `procedure`, `lock` and `minus` as plain column names.
_ENDS_A_CONDITION_IN: dict[Dialect, frozenset[str]] = {
    Dialect.MYSQL: frozenset({"LOCK", "PROCEDURE", "MINUS", "OPTION"}),
    Dialect.TSQL: frozenset({"OPTION"}),
}


class SortKey(BaseModel):
    column: str
    descending: bool = False


def _last_named(path: Sequence[PathSegment], kinds: frozenset[NodeKind]) -> str:
    """The name of the innermost segment of one of `kinds` (a table's column or index sits below it)."""
    name = next((segment.name for segment in reversed(path) if segment.kind in kinds), None)
    if name is None:
        raise ValidationFailed("This object has no data to browse", detail={"path": [s.model_dump() for s in path]})
    return name


def relation_from_path(path: Sequence[PathSegment]) -> tuple[str | None, str]:
    """(schema, name) of the table-like node a tree path points at (also from one of its columns or indexes)."""
    name = _last_named(path, _RELATION_KINDS)
    schema = next((segment.name for segment in reversed(path) if segment.kind == NodeKind.SCHEMA), None)
    return schema, name


def _single_expression(where: str | None, dialect: Dialect) -> str | None:
    """The filter rebuilt from its tokens so that it cannot leave its place in our SELECT.

    - Trailing `;`, whitespace and comments are dropped; any other `;` outside literals would start a second statement.
    - Every comment becomes one space, so what we send never depends on where the database ends a comment
      (PostgreSQL ends `--` at CR as well as LF).
    - An unterminated string, quoted identifier or block comment would swallow the ORDER BY appended after it: rejected.
    - MySQL executable comments (`/*! … */`, `/*+ … */`) are code to the server, not comments: turning one into
      a space would silently change the filter, and passing it through would run code (a `;` included) that looks
      like a comment: rejected.
    - T-SQL needs no `;` between statements: a filter the T-SQL statement cutter splits in two is rejected.
    """
    tokens = tokenize(where or "", dialect)
    if not all(token.terminated for token in tokens):
        raise ValidationFailed("The filter has an unterminated string, quoted name or comment")
    if any(token.kind == TokenKind.EXEC_COMMENT for token in tokens):
        raise ValidationFailed("Executable comments (/*! … */, /*+ … */) are not allowed in a filter")
    while tokens and tokens[-1].kind in _TRAILING:
        tokens.pop()
    if any(token.kind == TokenKind.SEMI for token in tokens):
        raise ValidationFailed("A filter must be a single expression")
    # T-SQL needs no `;` before a second statement: `id = 1 DELETE FROM t` would run the DELETE.
    if dialect is Dialect.TSQL and len(statements([t for t in tokens if t.kind not in INSIGNIFICANT])) > 1:
        raise ValidationFailed("A filter must be a single expression: this one holds another T-SQL statement")
    if not _one_condition(tokens, dialect):
        raise ValidationFailed(FILTER_NOT_A_CONDITION)
    return "".join(" " if token.kind == TokenKind.COMMENT else token.text for token in tokens).strip() or None


def _one_condition(tokens: list, dialect: Dialect) -> bool:
    """Whether the filter's tokens stay one condition: never a `)` it did not open (`x) UNION … WHERE (1` balances
    again, so balance alone is not enough), and no word that ends a WHERE clause outside parentheses."""
    ends = _ENDS_A_CONDITION | _ENDS_A_CONDITION_IN.get(dialect, frozenset())
    depth = 0
    for token in tokens:
        if token.kind is TokenKind.LPAREN:
            depth += 1
        elif token.kind is TokenKind.RPAREN:
            depth -= 1
            if depth < 0:
                return False
        elif token.kind is TokenKind.WORD and depth == 0 and token.upper in ends:
            return False
    return True


def _sqlpp_browse_select(
    path: Sequence[PathSegment], where: str | None, order_by: Sequence[SortKey], count: bool
) -> str:
    """D-04-11: a collection is read in a query tab whose context is its scope, so the name is not qualified, and each
    document comes with its key (B18: quoted only where SQL++ needs it). No filter, sort or count here: that is SQL++
    the user writes in the tab."""
    collection = _last_named(path, _COLLECTIONS)
    if count or order_by or (where or "").strip():
        raise ValidationFailed("Filtering and sorting a collection is done in SQL++ in a query tab")
    return f"SELECT META(d).id, *\nFROM {quote_ident(Dialect.SQLPP, collection)} AS d"


def build_browse_select(
    dialect: Dialect, path: Sequence[PathSegment], where: str | None, order_by: Sequence[SortKey], count: bool = False
) -> str:
    if dialect is Dialect.SQLPP:
        return _sqlpp_browse_select(path, where, order_by, count)
    schema, name = relation_from_path(path)
    sql = f"SELECT {'count(*)' if count else '*'} FROM {qualified(dialect, schema, name)}"
    if condition := _single_expression(where, dialect):
        sql += f"\nWHERE {condition}"
    if order_by and not count:
        keys = ", ".join(f"{quote_ident(dialect, key.column)} {'DESC' if key.descending else 'ASC'}" for key in order_by)
        sql += f"\nORDER BY {keys}"
    return sql


def check_filter(dialect: Dialect, where: str | None) -> str | None:
    """The table-data tab's filter as a dump or copy would send it (one expression, as browsing checks it), or None
    when it is empty; ValidationFailed otherwise. A job checks it before it sends anything."""
    return _single_expression(where, dialect)


def build_count_select(
    dialect: Dialect, schema: str | None, name: str, where: str | None = None, *, only: bool = False,
) -> str:
    """How many rows a dump or copy of a relation would read (with the filter): what a table with no column it can
    write is restored as, a row of defaults each (06a Task 7 review)."""
    return "SELECT COUNT(*) " + _rows_of(dialect, schema, name, where, only)


def _rows_of(dialect: Dialect, schema: str | None, name: str, where: str | None, only: bool) -> str:
    """`FROM [ONLY] relation` and the filter's WHERE: the rows a dump or copy reads."""
    sql = f"FROM {'ONLY ' if only else ''}{qualified(dialect, schema, name)}"
    if condition := _single_expression(where, dialect):
        sql += f"\nWHERE {condition}"
    return sql


def build_copy_select(
    dialect: Dialect, schema: str | None, name: str, columns: Sequence[TableColumn], where: str | None = None, *,
    only: bool = False,
) -> str:
    """The SELECT a dump or copy reads a relation's rows with (spec ⑥ D-06-6): the columns it writes, in their order,
    each through its read expression and named as itself (PostgreSQL money as numeric, MySQL FLOAT as a DOUBLE, SQL
    Server's 7th fractional digit as text), the table-data tab's filter when given (one expression, as browsing
    checks it), and PostgreSQL's FROM ONLY for an inheritance parent's own rows. Raises ValidationFailed when no
    column is written or the filter is not one expression."""
    picked = []
    for column in columns:
        if not column.copied:
            continue
        quoted = quote_ident(dialect, column.name)
        picked.append(f"{column.read_as.format(quoted)} AS {quoted}" if column.read_as else quoted)
    if not picked:
        raise ValidationFailed(f"{name} has no column a dump or copy can write")
    return f"SELECT {', '.join(picked)} " + _rows_of(dialect, schema, name, where, only)
