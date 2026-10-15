"""Schema-aware completion and quick describe for half-typed SQL (spec §3.4).

Lexer-based: only the statement under the cursor is looked at, nothing is parsed fully, and neither entry point
raises. Pure: no I/O, and SQL text is never logged above DEBUG. All offsets are Python code points.
"""
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import chain
from typing import Literal

from loguru import logger
from pydantic import BaseModel

from ..domain.schema import RelationKind, SchemaSnapshot, SnapshotRelation, SnapshotSchema
from .dialects import RULES, Dialect, qualified, quote_ident
from .lexer import Token, TokenKind, tokenize
from .splitter import split

CompletionKind = Literal[
    "keyword", "schema", "table", "view", "materialized_view", "column", "alias", "function", "join"
]
_Context = Literal["start", "clause", "relations", "columns", "join"]
MAX_ITEMS = 300
_JOIN_BOOST = 3
_SYSTEM_BOOST = -1
_PREFIX_BOOST, _SUBSTRING_BOOST = 2, 1
# Tie-break after the boost. Relation kinds share a rank so a short view is not buried under every table.
_KIND_ORDER: dict[str, int] = {
    "join": 0, "alias": 1, "column": 2, "table": 3, "view": 3, "materialized_view": 3, "schema": 4, "function": 5,
    "keyword": 6,
}


class CompletionItem(BaseModel):
    label: str
    kind: CompletionKind
    detail: str | None = None
    apply: str | None = None  # text to insert when it differs from the label (quoted identifiers, join conditions)
    boost: int = 0


class CompletionResult(BaseModel):
    start: int  # code-point offsets into the full text: the items replace text[start:end]
    end: int
    items: list[CompletionItem]


class DescribeColumn(BaseModel):
    name: str
    type_name: str
    is_primary_key: bool
    references: str | None = None  # "<ref_schema>.<ref_table>(<ref_column>)"


class DescribeResult(BaseModel):
    schema_name: str
    name: str
    kind: RelationKind
    columns: list[DescribeColumn]


# --- keywords ----------------------------------------------------------------------------------------------------

_STARTERS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "CREATE", "ALTER", "DROP", "EXPLAIN", "BEGIN", "COMMIT",
             "ROLLBACK")
_CLAUSES = (
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT JOIN", "INNER JOIN", "RIGHT JOIN", "FULL JOIN", "CROSS JOIN", "GROUP BY",
    "ORDER BY", "HAVING", "LIMIT", "OFFSET", "AND", "OR", "NOT", "IN", "IS", "NULL", "AS", "ON", "DISTINCT", "CASE",
    "WHEN", "THEN", "ELSE", "END", "UNION", "ALL", "ASC", "DESC", "RETURNING", "SET", "VALUES", "LIKE", "BETWEEN",
    "EXISTS",
)
STATEMENT_KEYWORDS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: _STARTERS,
    Dialect.SQLITE: (*_STARTERS, "PRAGMA"),
}
CLAUSE_KEYWORDS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: (*_CLAUSES, "ILIKE"),
    Dialect.SQLITE: (*_CLAUSES, "GLOB"),
}

# Words that introduce a relation; UPDATE does not after FOR / FOR NO KEY / ON CONFLICT DO.
_RELATION_KEYWORDS = frozenset({"FROM", "JOIN", "UPDATE", "INTO", "TABLE"})
_NOT_A_TABLE_UPDATE = frozenset({"FOR", "KEY", "DO"})
_BEFORE_NAME = frozenset({"ONLY", "LATERAL", "IF", "NOT", "EXISTS"})  # FROM ONLY t, DROP TABLE IF EXISTS t
_COLUMN_CLAUSES = frozenset(
    "SELECT WHERE HAVING SET BY RETURNING WHEN THEN ELSE CASE DISTINCT USING ON".split()
)
_ALIAS_STOP = frozenset(
    """ON WHERE JOIN LEFT RIGHT INNER OUTER FULL CROSS NATURAL GROUP ORDER LIMIT OFFSET SET USING UNION EXCEPT
    INTERSECT WINDOW HAVING VALUES RETURNING LATERAL SELECT FROM AS FOR DEFAULT WITH FETCH TABLESAMPLE WITHOUT
    STRICT ADD ALTER DROP RENAME""".split()
)
_IDENT = (TokenKind.WORD, TokenKind.QUOTED)
_SKIPPED = (TokenKind.SPACE, TokenKind.COMMENT)
_NO_ITEMS_INSIDE = (TokenKind.STRING, TokenKind.COMMENT, TokenKind.QUOTED)
_CLOSING_QUOTE = {opening: closing for rules in RULES.values() for opening, closing in rules.identifier_quotes}


# --- candidates --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A completion item before filtering; `lower` is precomputed because every keystroke filters on it."""

    label: str
    kind: CompletionKind
    detail: str | None
    apply: str | None
    boost: int
    lower: str

    @classmethod
    def of(cls, label: str, kind: CompletionKind, detail: str | None = None, apply: str | None = None,
           boost: int = 0) -> "_Candidate":
        return cls(label, kind, detail, None if apply == label else apply, boost, label.lower())

    def item(self, boost: int) -> CompletionItem:
        return CompletionItem(label=self.label, kind=self.kind, detail=self.detail, apply=self.apply, boost=boost)


def _by_length(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    """Shorter, then alphabetical: the natural order that filtering keeps within one boost and kind."""
    return sorted(candidates, key=lambda c: (len(c.label), c.label))


def _kind_word(kind: str) -> str:
    return kind.replace("_", " ")


def _column_candidates(dialect: Dialect, relation: SnapshotRelation) -> list[_Candidate]:
    """In table order, which is what people expect to scan."""
    return [
        _Candidate.of(c.name, "column", f"{relation.name} · {c.type_name}", quote_ident(dialect, c.name))
        for c in relation.columns
    ]


_KEYWORD_CANDIDATES: dict[tuple[Dialect, str], list[_Candidate]] = {
    (dialect, group): _by_length(_Candidate.of(word, "keyword") for word in dict.fromkeys(words))
    for group, table in (("start", STATEMENT_KEYWORDS), ("clause", CLAUSE_KEYWORDS))
    for dialect, words in table.items()
}


# --- snapshot index ----------------------------------------------------------------------------------------------

_Resolved = tuple[SnapshotSchema, SnapshotRelation]


def _prefer_exact[T](found: list[T], name: str, name_of: Callable[[T], str]) -> T | None:
    return next((item for item in found if name_of(item) == name), found[0] if found else None)


class SnapshotIndex:
    """Lookups and completion candidates for one snapshot, built once; the snapshot must not change afterwards."""

    def __init__(self, snapshot: SchemaSnapshot, dialect: Dialect) -> None:
        self.snapshot = snapshot
        self.dialect = dialect
        self.search_path_lower = [name.lower() for name in snapshot.search_path]
        self._path_rank = {name: rank for rank, name in reversed(list(enumerate(self.search_path_lower)))}
        self.schemas_by_lower: dict[str, list[SnapshotSchema]] = {}
        self.relations_by_lower_name: dict[str, list[_Resolved]] = {}
        for schema in snapshot.schemas:
            self.schemas_by_lower.setdefault(schema.name.lower(), []).append(schema)
            for relation in schema.relations:
                self.relations_by_lower_name.setdefault(relation.name.lower(), []).append((schema, relation))
        self.functions = sorted({name for schema in snapshot.schemas for name in schema.functions})
        self.relation_candidates = _by_length(
            self._relation_candidate(schema, relation) for schema in snapshot.schemas for relation in schema.relations
        )
        self.schema_candidates = _by_length(
            _Candidate.of(s.name, "schema", "schema", quote_ident(dialect, s.name), _SYSTEM_BOOST if s.system else 0)
            for s in snapshot.schemas
        )
        self.function_candidates = _by_length(
            _Candidate.of(name, "function", "function", quote_ident(dialect, name)) for name in self.functions
        )
        self._relations_of_schema = {
            schema.name: _by_length(
                _Candidate.of(r.name, r.kind, _kind_word(r.kind), quote_ident(dialect, r.name))
                for r in schema.relations
            )
            for schema in snapshot.schemas
        }

    def schema(self, name: str) -> SnapshotSchema | None:
        return _prefer_exact(self.schemas_by_lower.get(name.lower(), []), name, lambda s: s.name)

    def in_search_path(self, schema: str) -> bool:
        return schema.lower() in self._path_rank

    def resolve(self, schema: str | None, name: str) -> _Resolved | None:
        """Case-insensitive, exact case preferred. Unqualified: the search_path schemas first (exact case, then
        path order), then any other schema (user before system), so describe and alias columns still work for
        e.g. pg_catalog tables."""
        found = self.relations_by_lower_name.get(name.lower(), [])
        if schema is not None:
            target = self.schema(schema)
            return _prefer_exact([p for p in found if p[0] is target], name, lambda p: p[1].name)
        return min(found, key=lambda pair: self._unqualified_rank(pair, name), default=None)

    def relations_of(self, schema: SnapshotSchema) -> list[_Candidate]:
        return self._relations_of_schema.get(schema.name, [])

    def _unqualified_rank(self, pair: _Resolved, name: str) -> tuple[bool, bool, int]:
        schema, relation = pair
        rank = self._path_rank.get(schema.name.lower())
        outside = rank is None
        return outside, relation.name != name, int(schema.system) if outside else rank

    def _relation_candidate(self, schema: SnapshotSchema, relation: SnapshotRelation) -> _Candidate:
        """Bare name when an unqualified reference reaches this relation; `schema.name` otherwise."""
        resolved = self.resolve(None, relation.name) if self.in_search_path(schema.name) else None
        bare = resolved is not None and resolved[1] is relation
        label = relation.name if bare else f"{schema.name}.{relation.name}"
        apply = qualified(self.dialect, None if bare else schema.name, relation.name)
        return _Candidate.of(label, relation.kind, f"{_kind_word(relation.kind)} in {schema.name}", apply,
                             _SYSTEM_BOOST if schema.system else 0)


# --- statement and tokens ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScopeRelation:
    schema: str | None
    name: str | None  # None for a subquery: only its alias is known
    alias: str | None
    position: int  # offset of the reference in the statement
    group: int  # offset of the innermost "(" around the reference, -1 at the top level: a FROM and its JOINs share it


def _statement_bounds(text: str, cursor: int, dialect: Dialect) -> tuple[int, int]:
    """The statement around the cursor, or the gap (whitespace, comments, stray `;`) between two statements.

    An unterminated last statement owns everything after it, so `SELECT * FROM t |` is still that statement.
    """
    spans = split(text, dialect)
    gap_start = 0
    for position, span in enumerate(spans):
        if cursor < span.start:
            return gap_start, span.start
        unterminated_last = position == len(spans) - 1 and span.terminator_end == span.end
        end = len(text) if unterminated_last else span.terminator_end
        if cursor <= end:
            return span.start, end
        gap_start = end
    return gap_start, len(text)


def _significant(tokens: Iterable[Token]) -> list[Token]:
    return [token for token in tokens if token.kind not in _SKIPPED]


def _unquote(token: Token) -> str:
    """`"a""b"` → `a"b`, `[a b]` → `a b`, `` `a` `` → `a`; an unterminated quote yields what was typed so far."""
    text = token.text
    close = _CLOSING_QUOTE.get(text[:1]) if token.kind == TokenKind.QUOTED else None
    if close is None:
        return text
    inner = text[1:-1] if len(text) > 1 and text.endswith(close) else text[1:]
    return inner if close == "]" else inner.replace(close * 2, close)


def _token_before(tokens: list[Token], offset: int) -> Token | None:
    """The token that contains `offset` or ends exactly at it (the character just typed belongs to it)."""
    return next((token for token in tokens if token.start < offset <= token.end), None)


def _is_closed(token: Token, dialect: Dialect) -> bool:
    """Whether a string, comment or quoted identifier is terminated: an open one swallows anything appended."""
    return tokenize(token.text + "\n", dialect)[0].end == len(token.text)


def _inside_literal(token: Token | None, offset: int, dialect: Dialect) -> bool:
    if token is None or token.kind not in _NO_ITEMS_INSIDE:
        return False
    if offset < token.end or token.text.startswith("--"):  # a line comment runs up to (not over) the newline
        return True
    return not _is_closed(token, dialect)


def _matching_paren(tokens: list[Token], i: int) -> int:
    """Index after the parenthesis that closes the one at `i` (len(tokens) when it is never closed)."""
    depth = 0
    for j in range(i, len(tokens)):
        if tokens[j].kind == TokenKind.LPAREN:
            depth += 1
        elif tokens[j].kind == TokenKind.RPAREN:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(tokens)


def _word(tokens: list[Token], i: int) -> str:
    return tokens[i].upper if 0 <= i < len(tokens) and tokens[i].kind == TokenKind.WORD else ""


def _introduces_relation(tokens: list[Token], i: int) -> bool:
    word = _word(tokens, i)
    return word in _RELATION_KEYWORDS and not (word == "UPDATE" and _word(tokens, i - 1) in _NOT_A_TABLE_UPDATE)


# --- scope -------------------------------------------------------------------------------------------------------


def _read_alias(tokens: list[Token], i: int) -> tuple[str | None, int]:
    if _word(tokens, i) == "AS":
        i += 1
    if i < len(tokens) and (tokens[i].kind == TokenKind.QUOTED or
                            (tokens[i].kind == TokenKind.WORD and tokens[i].upper not in _ALIAS_STOP)):
        return _unquote(tokens[i]), i + 1
    return None, i


def _read_relation(tokens: list[Token], i: int, out: list[_ScopeRelation], group: int) -> int | None:
    """One `[ONLY] [schema.]name [(…)] [[AS] alias]` or `(subquery) [AS] alias` at `i`; returns the index after it,
    or None when there is no relation there."""
    while _word(tokens, i) in _BEFORE_NAME:
        i += 1
    if i >= len(tokens):
        return None
    position = tokens[i].start
    if tokens[i].kind == TokenKind.LPAREN:
        alias, i = _read_alias(tokens, _matching_paren(tokens, i))
        if alias is not None:
            out.append(_ScopeRelation(None, None, alias, position, group))
        return i
    if tokens[i].kind not in _IDENT or _word(tokens, i) in _ALIAS_STOP:
        return None
    parts = [_unquote(tokens[i])]
    i += 1
    while i + 1 < len(tokens) and tokens[i].kind == TokenKind.DOT and tokens[i + 1].kind in _IDENT:
        parts.append(_unquote(tokens[i + 1]))
        i += 2
    if i < len(tokens) and tokens[i].kind == TokenKind.DOT:
        return None  # `FROM sales.` is still being typed: `sales` is a schema, not a relation
    if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:  # function arguments or an INSERT column list
        i = _matching_paren(tokens, i)
    alias, i = _read_alias(tokens, i)
    out.append(_ScopeRelation(parts[-2] if len(parts) > 1 else None, parts[-1], alias, position, group))
    return i


def _read_relations(tokens: list[Token], i: int, out: list[_ScopeRelation], allow_list: bool, group: int) -> None:
    """The relation(s) after FROM/JOIN/…; FROM takes a comma-separated list."""
    while (after := _read_relation(tokens, i, out, group)) is not None:
        if not (allow_list and after < len(tokens) and tokens[after].kind == TokenKind.COMMA):
            return
        i = after + 1


def _scope(tokens: list[Token]) -> list[_ScopeRelation]:
    """Relations referenced anywhere in the statement (subqueries and text after the cursor included)."""
    out: list[_ScopeRelation] = []
    open_parens: list[int] = []
    for i, token in enumerate(tokens):
        if token.kind == TokenKind.LPAREN:
            open_parens.append(token.start)
        elif token.kind == TokenKind.RPAREN and open_parens:
            open_parens.pop()
        elif _introduces_relation(tokens, i):
            group = open_parens[-1] if open_parens else -1
            _read_relations(tokens, i + 1, out, allow_list=token.upper == "FROM", group=group)
    return out


def _enclosing_group(before: list[Token]) -> int:
    """The `_ScopeRelation.group` of a position after `before`."""
    open_parens: list[int] = []
    for token in before:
        if token.kind == TokenKind.LPAREN:
            open_parens.append(token.start)
        elif token.kind == TokenKind.RPAREN and open_parens:
            open_parens.pop()
    return open_parens[-1] if open_parens else -1


def _find_alias(scope: list[_ScopeRelation], name: str) -> _ScopeRelation | None:
    lower = name.lower()
    return next((ref for ref in scope if ref.alias is not None and ref.alias.lower() == lower), None)


def _find_in_scope(scope: list[_ScopeRelation], name: str) -> _ScopeRelation | None:
    """An alias first, then a relation referenced by that name."""
    lower = name.lower()
    by_name = (ref for ref in scope if ref.name is not None and ref.name.lower() == lower)
    return _find_alias(scope, name) or next(by_name, None)


def _resolve_ref(index: SnapshotIndex, ref: _ScopeRelation) -> _Resolved | None:
    return index.resolve(ref.schema, ref.name) if ref.name is not None else None


def _reference_name(dialect: Dialect, ref: _ScopeRelation) -> str:
    """How the statement refers to a relation: its alias, else its own name."""
    return quote_ident(dialect, ref.alias if ref.alias is not None else ref.name or "")


# --- context -----------------------------------------------------------------------------------------------------


def _context(before: list[Token]) -> _Context:
    """Clause the cursor is in, from the last clause keyword at the cursor's parenthesis depth (AND/OR/NOT and other
    words are passed over): "relations", "clause" (a relation was just named), "join" (right after ON), "columns"
    (also inside an unclosed parenthesis) or "start" (no clause yet)."""
    depth = 0
    for j in range(len(before) - 1, -1, -1):
        token = before[j]
        if token.kind == TokenKind.SEMI:
            return "start"
        if token.kind == TokenKind.RPAREN:
            depth += 1
        elif token.kind == TokenKind.LPAREN:
            if depth == 0:
                return "columns"
            depth -= 1
        elif depth == 0 and token.kind == TokenKind.WORD:
            last = before[-1]
            if _introduces_relation(before, j):
                naming = last is token or last.kind == TokenKind.COMMA or last.upper in _BEFORE_NAME
                return "relations" if naming else "clause"
            if token.upper == "ON" and last is token:
                return "join"
            if token.upper in _COLUMN_CLAUSES:
                return "columns"
    return "start"


def _qualifier(before: list[Token]) -> list[str]:
    """`a.b.` right before the partial word → ["a", "b"]; [] when the word is not qualified."""
    parts: list[str] = []
    j = len(before) - 1
    while j >= 1 and before[j].kind == TokenKind.DOT and before[j - 1].kind in _IDENT:
        parts.insert(0, _unquote(before[j - 1]))
        j -= 2
    return parts


# --- candidate sources -------------------------------------------------------------------------------------------


def _qualified_candidates(parts: list[str], scope: list[_ScopeRelation], index: SnapshotIndex) -> list[_Candidate]:
    """`x.`: an alias or relation in scope → its columns; a schema → its relations; a relation → its columns."""
    if len(parts) == 1:
        ref = _find_in_scope(scope, parts[0])
        if ref is not None:
            resolved = _resolve_ref(index, ref)
            return _column_candidates(index.dialect, resolved[1]) if resolved else []
        schema = index.schema(parts[0])
        if schema is not None:
            return index.relations_of(schema)
    resolved = index.resolve(parts[-2] if len(parts) > 1 else None, parts[-1])
    return _column_candidates(index.dialect, resolved[1]) if resolved else []


def _scope_candidates(scope: list[_ScopeRelation], index: SnapshotIndex) -> list[_Candidate]:
    """Aliases (and unaliased relation names) of the statement, then the columns of every relation, once each."""
    names: dict[str, _Candidate] = {}
    columns: list[_Candidate] = []
    seen: set[int] = set()
    for ref in scope:
        resolved = _resolve_ref(index, ref)
        if ref.alias is not None:
            detail = resolved[1].name if resolved else ref.name or "subquery"
            names.setdefault(ref.alias.lower(), _Candidate.of(ref.alias, "alias", detail,
                                                              quote_ident(index.dialect, ref.alias)))
        elif resolved is not None:
            relation = resolved[1]
            names.setdefault(relation.name.lower(), _Candidate.of(
                relation.name, relation.kind, f"{_kind_word(relation.kind)} in {resolved[0].name}",
                quote_ident(index.dialect, relation.name)))
        if resolved is not None and id(resolved[1]) not in seen:
            seen.add(id(resolved[1]))
            columns.extend(_column_candidates(index.dialect, resolved[1]))
    return [*names.values(), *columns]


def _fk_pairs(dialect: Dialect, src: _Resolved, src_ref: str, dst: _Resolved,
              dst_ref: str) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """(FK name, [(`src_ref.column`, `dst_ref.ref_column`), …]) for every FK of `src` that references `dst`."""
    for fk in src[1].foreign_keys:
        if (fk.ref_schema, fk.ref_table) == (dst[0].name, dst[1].name):
            yield fk.name, [
                (f"{src_ref}.{quote_ident(dialect, column)}", f"{dst_ref}.{quote_ident(dialect, ref_column)}")
                for column, ref_column in zip(fk.columns, fk.ref_columns)
            ]


def _join_candidate(name: str, pairs: Iterable[tuple[str, str]]) -> _Candidate:
    condition = " AND ".join(f"{left} = {right}" for left, right in pairs)
    return _Candidate(condition, "join", name, condition, _JOIN_BOOST, condition.lower())


def _join_conditions(target: _ScopeRelation, other: _ScopeRelation, index: SnapshotIndex) -> Iterator[_Candidate]:
    """FKs between the relation being joined and one already in the statement, either direction; the joined
    relation's side is written first."""
    joined, existing = _resolve_ref(index, target), _resolve_ref(index, other)
    if joined is None or existing is None:
        return
    t, o = _reference_name(index.dialect, target), _reference_name(index.dialect, other)
    for name, pairs in _fk_pairs(index.dialect, joined, t, existing, o):
        yield _join_candidate(name, pairs)
    for name, pairs in _fk_pairs(index.dialect, existing, o, joined, t):
        yield _join_candidate(name, ((mine, theirs) for theirs, mine in pairs))


def _join_candidates(scope: list[_ScopeRelation], before: list[Token], index: SnapshotIndex) -> list[_Candidate]:
    """FK join conditions for the relation just before the cursor's ON, against the relations before it in the same
    FROM (relations inside subqueries are not visible there)."""
    offset, group = before[-1].start, _enclosing_group(before)
    earlier = sorted((ref for ref in scope if ref.group == group and ref.position < offset), key=lambda r: r.position)
    if not earlier:
        return []
    target = earlier[-1]
    found: dict[str, _Candidate] = {}
    for other in earlier[:-1]:
        for candidate in _join_conditions(target, other, index):
            found.setdefault(candidate.label, candidate)
    return list(found.values())


def _keywords(dialect: Dialect, group: Literal["start", "clause"]) -> list[_Candidate]:
    return _KEYWORD_CANDIDATES[(dialect, group)]


def _candidates(before: list[Token], statement: list[Token], dialect: Dialect,
                index: SnapshotIndex | None) -> Iterable[_Candidate]:
    """Everything that fits at the cursor, unfiltered. `before` are the significant tokens before the partial word,
    `statement` all significant tokens of the statement."""
    parts = _qualifier(before)
    if parts:
        return _qualified_candidates(parts, _scope(statement), index) if index is not None else []
    context = _context(before)
    if context in ("start", "clause"):
        return _keywords(dialect, context)
    if index is None:
        return _keywords(dialect, "clause") if context != "relations" else []
    if context == "relations":
        return chain(index.relation_candidates, index.schema_candidates)
    scope = _scope(statement)
    joins = _join_candidates(scope, before, index) if context == "join" else []
    return chain(joins, _scope_candidates(scope, index), index.function_candidates, _keywords(dialect, "clause"))


def _filter(candidates: Iterable[_Candidate], prefix: str) -> list[CompletionItem]:
    """Prefix matches (+2) before substring matches (+1); then kind, then each source's own order. At most 300."""
    needle = prefix.lower()
    matched: list[tuple[int, _Candidate]] = []
    for candidate in candidates:
        if candidate.lower.startswith(needle):
            matched.append((candidate.boost + _PREFIX_BOOST, candidate))
        elif needle in candidate.lower:
            matched.append((candidate.boost + _SUBSTRING_BOOST, candidate))
    matched.sort(key=lambda m: (-m[0], _KIND_ORDER[m[1].kind]))  # stable: keeps the natural order within a tie
    return [candidate.item(boost) for boost, candidate in matched[:MAX_ITEMS]]


# --- entry points ------------------------------------------------------------------------------------------------


def _guarded[T](operation: str, cursor: int, default: T, run: Callable[[], T]) -> T:
    """Runs on every keystroke: a bug must degrade to "no suggestions", never fail the request."""
    try:
        return run()
    except Exception as exc:
        where = traceback.extract_tb(exc.__traceback__)[-1:]
        location = f"{where[0].name}:{where[0].lineno}" if where else "?"
        logger.error("{} failed at offset {} ({} in {}); returning nothing", operation, cursor, type(exc).__name__,
                     location)
        logger.opt(exception=exc).debug("{} failure details", operation)
        return default


def complete(text: str, cursor: int, dialect: Dialect, index: SnapshotIndex | None) -> CompletionResult:
    """Completions at `cursor`; without an index only keywords. Never raises."""
    cursor = min(max(cursor, 0), len(text))
    empty = CompletionResult(start=cursor, end=cursor, items=[])
    return _guarded("Completion", cursor, empty, lambda: _complete(text, cursor, dialect, index))


def _complete(text: str, cursor: int, dialect: Dialect, index: SnapshotIndex | None) -> CompletionResult:
    base, end = _statement_bounds(text, cursor, dialect)
    tokens = tokenize(text[base:end], dialect)
    offset = cursor - base
    at = _token_before(tokens, offset)
    if _inside_literal(at, offset, dialect):
        return CompletionResult(start=cursor, end=cursor, items=[])
    word_start = at.start if at is not None and at.kind == TokenKind.WORD else offset
    significant = _significant(tokens)
    before = [token for token in significant if token.end <= word_start]
    items = _filter(_candidates(before, significant, dialect, index), text[base + word_start:cursor])
    return CompletionResult(start=base + word_start, end=cursor, items=items)


def describe_at(text: str, cursor: int, dialect: Dialect, index: SnapshotIndex | None) -> DescribeResult | None:
    """The relation named (directly or through an alias) by the identifier at or just before `cursor`."""
    if index is None:
        return None
    cursor = min(max(cursor, 0), len(text))
    return _guarded("Describe", cursor, None, lambda: _describe(text, cursor, dialect, index))


def _identifier_at(tokens: list[Token], offset: int) -> int | None:
    """Index of the identifier under the cursor, else of the one ending right before it."""
    under = next((j for j, t in enumerate(tokens) if t.start <= offset < t.end and t.kind in _IDENT), None)
    return under if under is not None else next(
        (j for j, t in enumerate(tokens) if t.end == offset and t.kind in _IDENT), None
    )


def _describe(text: str, cursor: int, dialect: Dialect, index: SnapshotIndex) -> DescribeResult | None:
    base, end = _statement_bounds(text, cursor, dialect)
    tokens = _significant(tokenize(text[base:end], dialect))
    j = _identifier_at(tokens, cursor - base)
    if j is None:
        return None
    name = _unquote(tokens[j])
    scope = _scope(tokens)
    if j >= 2 and tokens[j - 1].kind == TokenKind.DOT and tokens[j - 2].kind in _IDENT:
        left = _unquote(tokens[j - 2])
        if _find_alias(scope, left) is not None:
            return None  # `alias.column`: the identifier is a column, not a relation
        resolved = index.resolve(left, name)
    else:
        ref = _find_in_scope(scope, name)
        resolved = _resolve_ref(index, ref) if ref is not None else index.resolve(None, name)
    return _describe_relation(*resolved) if resolved is not None else None


def _describe_relation(schema: SnapshotSchema, relation: SnapshotRelation) -> DescribeResult:
    references: dict[str, str] = {}
    for fk in relation.foreign_keys:
        for column, ref_column in zip(fk.columns, fk.ref_columns):
            references.setdefault(column, f"{fk.ref_schema}.{fk.ref_table}({ref_column})")
    primary_key = set(relation.primary_key)
    return DescribeResult(
        schema_name=schema.name, name=relation.name, kind=relation.kind,
        columns=[
            DescribeColumn(name=c.name, type_name=c.type_name, is_primary_key=c.name in primary_key,
                           references=references.get(c.name))
            for c in relation.columns
        ],
    )
