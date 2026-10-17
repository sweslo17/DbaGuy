"""The relations a statement refers to: FROM/JOIN/… references with their aliases, and its CTEs.

Lexical and tolerant of half-typed SQL; never parses fully. Used by completion and describe. Pure.
"""
from dataclasses import dataclass

from ..domain.schema import SnapshotColumn, SnapshotRelation
from .dialects import RULES
from .lexer import Token, TokenKind

IDENT = (TokenKind.WORD, TokenKind.QUOTED)
BEFORE_NAME = frozenset({"ONLY", "LATERAL", "IF", "NOT", "EXISTS"})  # FROM ONLY t, DROP TABLE IF EXISTS t
# Words that introduce a relation; UPDATE does not after FOR / FOR NO KEY / ON CONFLICT DO.
_RELATION_KEYWORDS = frozenset({"FROM", "JOIN", "UPDATE", "INTO", "TABLE"})
_NOT_A_TABLE_UPDATE = frozenset({"FOR", "KEY", "DO"})
_ALIAS_STOP = frozenset(
    """ON WHERE JOIN LEFT RIGHT INNER OUTER FULL CROSS NATURAL GROUP ORDER LIMIT OFFSET SET USING UNION EXCEPT
    INTERSECT WINDOW HAVING VALUES RETURNING LATERAL SELECT FROM AS FOR DEFAULT WITH FETCH TABLESAMPLE WITHOUT
    STRICT ADD ALTER DROP RENAME USE FORCE IGNORE STRAIGHT_JOIN PARTITION""".split()  # USE…PARTITION: MySQL hints
)
_CLOSING_QUOTE = {opening: closing for rules in RULES.values() for opening, closing in rules.identifier_quotes}
# Select-list parsing for CTE output names.
_SELECT_LIST_END = frozenset(
    "FROM INTO WHERE GROUP HAVING WINDOW ORDER LIMIT OFFSET UNION INTERSECT EXCEPT FETCH FOR".split()
)
_OPERATOR_WORDS = frozenset(
    "NOT AND OR IS IN LIKE ILIKE GLOB BETWEEN CASE WHEN THEN ELSE DISTINCT ALL ANY SOME EXISTS COLLATE ESCAPE".split()
)
_NOT_AN_ALIAS = frozenset({"AS", "END", "NULL", "TRUE", "FALSE"})
_OPERAND_END = (TokenKind.WORD, TokenKind.QUOTED, TokenKind.RPAREN, TokenKind.NUMBER, TokenKind.STRING)


@dataclass(frozen=True, slots=True)
class ScopeRelation:
    schema: str | None
    name: str | None  # None for a subquery: only its alias is known
    alias: str | None
    position: int  # offset of the reference in the statement
    group: int  # offset of the innermost "(" around the reference, -1 at the top level: a FROM and its JOINs share it


@dataclass(frozen=True, slots=True)
class Cte:
    relation: SnapshotRelation  # kind "table"; columns from the explicit list or the body's select list, untyped
    body_start: int  # offset just inside its "("
    body_end: int | None  # offset of its ")"; None while it is not closed yet

    def contains(self, offset: int) -> bool:
        return self.body_start <= offset and (self.body_end is None or offset <= self.body_end)


@dataclass(frozen=True, slots=True)
class Scope:
    relations: list[ScopeRelation]
    ctes: list[Cte]

    def find_alias(self, name: str) -> ScopeRelation | None:
        lower = name.lower()
        return next((ref for ref in self.relations if ref.alias is not None and ref.alias.lower() == lower), None)

    def find(self, name: str) -> ScopeRelation | None:
        """An alias first, then a relation referenced by that name."""
        lower = name.lower()
        by_name = (ref for ref in self.relations if ref.name is not None and ref.name.lower() == lower)
        return self.find_alias(name) or next(by_name, None)

    def cte(self, name: str) -> SnapshotRelation | None:
        lower = name.lower()
        return next((cte.relation for cte in self.ctes if cte.relation.name.lower() == lower), None)


# --- tokens ------------------------------------------------------------------------------------------------------


def unquote(token: Token) -> str:
    """`"a""b"` → `a"b`, `[a b]` → `a b`, `` `a` `` → `a`; an unterminated quote yields what was typed so far."""
    text = token.text
    close = _CLOSING_QUOTE.get(text[:1]) if token.kind == TokenKind.QUOTED else None
    if close is None:
        return text
    inner = text[1:-1] if len(text) > 1 and text.endswith(close) else text[1:]
    return inner if close == "]" else inner.replace(close * 2, close)


def word(tokens: list[Token], i: int) -> str:
    return tokens[i].upper if 0 <= i < len(tokens) and tokens[i].kind == TokenKind.WORD else ""


def introduces_relation(tokens: list[Token], i: int) -> bool:
    keyword = word(tokens, i)
    return keyword in _RELATION_KEYWORDS and not (keyword == "UPDATE" and word(tokens, i - 1) in _NOT_A_TABLE_UPDATE)


def enclosing_group(tokens: list[Token]) -> int:
    """The `ScopeRelation.group` of a position right after `tokens`."""
    open_parens: list[int] = []
    for token in tokens:
        if token.kind == TokenKind.LPAREN:
            open_parens.append(token.start)
        elif token.kind == TokenKind.RPAREN and open_parens:
            open_parens.pop()
    return open_parens[-1] if open_parens else -1


def _closing_paren(tokens: list[Token], i: int) -> int | None:
    """Index of the parenthesis that closes the one at `i`, None when it is never closed."""
    depth = 0
    for j in range(i, len(tokens)):
        if tokens[j].kind == TokenKind.LPAREN:
            depth += 1
        elif tokens[j].kind == TokenKind.RPAREN:
            depth -= 1
            if depth == 0:
                return j
    return None


def _after_parens(tokens: list[Token], i: int) -> int:
    close = _closing_paren(tokens, i)
    return len(tokens) if close is None else close + 1


# --- FROM / JOIN / UPDATE / INTO / TABLE references --------------------------------------------------------------


def _read_alias(tokens: list[Token], i: int) -> tuple[str | None, int]:
    if word(tokens, i) == "AS":
        i += 1
    if i < len(tokens) and (tokens[i].kind == TokenKind.QUOTED or
                            (tokens[i].kind == TokenKind.WORD and tokens[i].upper not in _ALIAS_STOP)):
        return unquote(tokens[i]), i + 1
    return None, i


def _read_relation(tokens: list[Token], i: int, out: list[ScopeRelation], group: int) -> int | None:
    """One `[ONLY] [schema.]name [(…)] [[AS] alias]` or `(subquery) [AS] alias` at `i`; returns the index after it,
    or None when there is no relation there."""
    while word(tokens, i) in BEFORE_NAME:
        i += 1
    if i >= len(tokens):
        return None
    position = tokens[i].start
    if tokens[i].kind == TokenKind.LPAREN:
        alias, i = _read_alias(tokens, _after_parens(tokens, i))
        if alias is not None:
            out.append(ScopeRelation(None, None, alias, position, group))
        return i
    if tokens[i].kind not in IDENT or word(tokens, i) in _ALIAS_STOP:
        return None
    parts = [unquote(tokens[i])]
    i += 1
    while i + 1 < len(tokens) and tokens[i].kind == TokenKind.DOT and tokens[i + 1].kind in IDENT:
        parts.append(unquote(tokens[i + 1]))
        i += 2
    if i < len(tokens) and tokens[i].kind == TokenKind.DOT:
        return None  # `FROM sales.` is still being typed: `sales` is a schema, not a relation
    if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:  # function arguments or an INSERT column list
        i = _after_parens(tokens, i)
    alias, i = _read_alias(tokens, i)
    out.append(ScopeRelation(parts[-2] if len(parts) > 1 else None, parts[-1], alias, position, group))
    return i


def _read_relations(tokens: list[Token], i: int, out: list[ScopeRelation], allow_list: bool, group: int) -> None:
    """The relation(s) after FROM/JOIN/…; FROM takes a comma-separated list."""
    while (after := _read_relation(tokens, i, out, group)) is not None:
        if not (allow_list and after < len(tokens) and tokens[after].kind == TokenKind.COMMA):
            return
        i = after + 1


def _references(tokens: list[Token]) -> list[ScopeRelation]:
    """Every relation reference in the statement, subqueries and CTE bodies included."""
    out: list[ScopeRelation] = []
    open_parens: list[int] = []
    for i, token in enumerate(tokens):
        if token.kind == TokenKind.LPAREN:
            open_parens.append(token.start)
        elif token.kind == TokenKind.RPAREN and open_parens:
            open_parens.pop()
        elif introduces_relation(tokens, i):
            group = open_parens[-1] if open_parens else -1
            _read_relations(tokens, i + 1, out, allow_list=token.upper == "FROM", group=group)
    return out


# --- CTEs --------------------------------------------------------------------------------------------------------


def _select_items(body: list[Token]) -> list[list[Token]]:
    """The comma-separated items of the body's top-level select list (without DISTINCT [ON (…)] / ALL)."""
    items: list[list[Token]] = []
    current: list[Token] | None = None  # None until the top-level SELECT
    depth = 0
    for i, token in enumerate(body):
        depth += (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN)
        if current is None:
            if depth == 0 and word(body, i) == "SELECT":
                current = []
            continue
        if depth == 0 and (token.kind == TokenKind.SEMI or word(body, i) in _SELECT_LIST_END):
            break
        if depth == 0 and token.kind == TokenKind.COMMA:
            items.append(current)
            current = []
        else:
            current.append(token)
    if current is None:
        return []
    items.append(current)
    first = items[0]
    if word(first, 0) in ("DISTINCT", "ALL"):
        first = first[1:]
        if word(first, 0) == "ON" and len(first) > 1 and first[1].kind == TokenKind.LPAREN:
            first = first[_after_parens(first, 1):]
        items[0] = first
    return items


def _is_dotted_name(item: list[Token]) -> bool:
    """`col`, `t.col`, `s.t.col`."""
    return len(item) % 2 == 1 and all(
        token.kind in IDENT if position % 2 == 0 else token.kind == TokenKind.DOT
        for position, token in enumerate(item)
    )


def _output_name(item: list[Token]) -> str | None:
    """`expr AS name` / `expr name` → name, `col` / `t.col` → col; None for `*`, `t.*` and unaliased expressions."""
    if not item or item[-1].kind not in IDENT:
        return None
    if _is_dotted_name(item) or word(item, len(item) - 2) == "AS":
        return unquote(item[-1])
    previous = item[-2]
    bare_alias = (
        previous.kind in _OPERAND_END and word(item, len(item) - 2) not in _OPERATOR_WORDS
        and word(item, len(item) - 1) not in _NOT_AN_ALIAS
    )
    return unquote(item[-1]) if bare_alias else None


def _read_cte(tokens: list[Token], i: int, out: list[Cte]) -> int | None:
    """One `name [(col, …)] AS [NOT] [MATERIALIZED] (body)` at `i`; returns the index after it, None if there is
    none there (e.g. `WITH TIME ZONE`, `WITH ORDINALITY`, `WITH (fillfactor = 70)`)."""
    if i >= len(tokens) or tokens[i].kind not in IDENT:
        return None
    name, i = unquote(tokens[i]), i + 1
    columns: list[str] | None = None
    if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:
        close = _closing_paren(tokens, i)
        if close is None:
            return None
        columns = [unquote(token) for token in tokens[i + 1:close] if token.kind in IDENT]
        i = close + 1
    if word(tokens, i) != "AS":
        return None
    i += 1
    if word(tokens, i) == "NOT":
        i += 1
    if word(tokens, i) == "MATERIALIZED":
        i += 1
    if i >= len(tokens) or tokens[i].kind != TokenKind.LPAREN:
        return None
    close = _closing_paren(tokens, i)
    body = tokens[i + 1:close] if close is not None else tokens[i + 1:]
    names = columns if columns is not None else [n for item in _select_items(body) if (n := _output_name(item))]
    relation = SnapshotRelation(
        name=name, kind="table", columns=[SnapshotColumn(name=n, type_name="") for n in dict.fromkeys(names)]
    )
    out.append(Cte(relation, tokens[i].end, tokens[close].start if close is not None else None))
    return len(tokens) if close is None else close + 1


def find_ctes(tokens: list[Token]) -> list[Cte]:
    """The CTEs of every `WITH [RECURSIVE] …` list in the statement."""
    out: list[Cte] = []
    for i in range(len(tokens)):
        if word(tokens, i) == "WITH":
            start = i + 2 if word(tokens, i + 1) == "RECURSIVE" else i + 1
            while (after := _read_cte(tokens, start, out)) is not None:
                if not (after < len(tokens) and tokens[after].kind == TokenKind.COMMA):
                    break
                start = after + 1
    return out


def _body_around(ctes: list[Cte], offset: int) -> Cte | None:
    """The innermost CTE body containing `offset`."""
    return max((cte for cte in ctes if cte.contains(offset)), key=lambda cte: cte.body_start, default=None)


def scope_at(tokens: list[Token], offset: int) -> Scope:
    """Relations visible at `offset` of a statement (its significant tokens): inside a CTE body only that body's
    references, elsewhere none from any CTE body. CTE names are visible everywhere in the statement."""
    ctes = find_ctes(tokens)
    here = _body_around(ctes, offset)
    return Scope([ref for ref in _references(tokens) if _body_around(ctes, ref.position) is here], ctes)
