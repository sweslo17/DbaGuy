"""SQL++ completion (spec §3.12, SQL++ Should 1): keyspace paths from the schema snapshot, field paths from a manual
sample and from the results seen in the tab, back-ticks where a name needs them.

The statement's FROM lists are read here (`statement_keyspace` answers the grid's "Open document…" with the one
keyspace a statement reads), not by `scope.py` (ruling B21): its alias rules are made for the other
dialects, and NEST / UNNEST / LET among its stop words would break PostgreSQL's `FROM unnest(…) AS u`. The fields of a
keyspace exist only when the user sampled it: this module only reads that cache, it never starts a sample. Pure: no
I/O, SQL text is never logged above DEBUG. Offsets are Python code points, as in `complete()`.

A statement is read as a whole, subqueries and CTE bodies included, the way `scope.py` reads it for the other dialects:
"reads exactly one keyspace" counts every FROM term in it, and an alias is the one the innermost group (parenthesis)
around the cursor defines, else the first one of that name anywhere.

Inside a back-ticked name (`` `trav| `` or the auto-closed `` `|` ``) the typed text is the prefix and the items replace
the whole name, quotes included, with the name quoted as `quote_ident` says: only names are offered there.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..domain.couchbase import DEFAULT, FieldPath, Keyspace
from ..domain.execution import QueryContext
from .completion import (
    SYSTEM_BOOST, Candidate, CompletionKind, CompletionResult, SnapshotIndex, by_length, clause_context,
    filter_candidates, guarded, keywords, qualifier, significant, word_at_cursor,
)
from .dialects import Dialect, quote_ident
from .keyspaces import NO_KEYSPACES, KeyspaceTree
from .lexer import Token, TokenKind, tokenize
from .scope import IDENT, unquote, word

SQLPP = Dialect.SQLPP
FieldLookup = Callable[[Keyspace], Sequence[FieldPath] | None]  # a keyspace's sampled field paths; None: not sampled

DEFAULT_NAMESPACE = "default"  # the namespace of the buckets; `system` holds the catalogs (`system:keyspaces`)
SEEN_BOOST = 0  # a field seen in results follows the sampled ones (the same kind); every keyword comes after both
_KEYSPACE_KEYWORDS = frozenset({"FROM", "JOIN", "NEST", "UPDATE", "INTO", "USING"})
_NOT_AN_ALIAS = frozenset(
    """USE ON WHERE JOIN LEFT INNER NEST UNNEST LET LETTING GROUP ORDER LIMIT OFFSET SET UNSET RETURNING VALUES SELECT
    FROM UNION INTERSECT EXCEPT KEYS USING""".split()
)
# The words that begin a clause: a comma continues a FROM list only when the nearest of them is FROM.
_CLAUSE_START = frozenset(
    "FROM SELECT WHERE GROUP ORDER HAVING LIMIT OFFSET LET LETTING SET UNSET RETURNING VALUES".split()
)
_OPENING, _CLOSING = "([{", ")]}"


# --- the statement's keyspace references ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Reference:
    """One `path [AS] alias` of a FROM list, or `(subquery) alias`."""

    parts: tuple[str, ...]  # the dotted path as written, unquoted (after any `namespace:`); () for a subquery
    alias: str | None
    group: int  # offset of the innermost "(" around the reference, -1 at the top level (as `ScopeRelation.group`)
    namespace: str | None = None  # the `ns` of `ns:path`, as written, unquoted

    @property
    def name(self) -> str | None:
        """What the statement calls it: the alias, else the last part of the path."""
        return self.alias if self.alias is not None else self.parts[-1] if self.parts else None

    @property
    def detail(self) -> str:
        if not self.parts:
            return "subquery"
        if self.alias is None:
            return "keyspace"
        path = ".".join(self.parts)
        return path if self.namespace is None else f"{self.namespace}:{path}"

    @property
    def names_collection(self) -> bool:
        """Whether the path is a collection of a bucket, to be resolved under the context. A name with a namespace
        (`system:keyspaces`, `system:indexes`) is a path of its own: never a collection of the context's scope. Only
        `default:` names the buckets, and only with a full `bucket.scope.collection`: a shorter path after it is not
        resolved as a relative name would be (`default:app` is a bucket, not a collection of the context's scope)."""
        return bool(self.parts) and (
            self.namespace is None or (self.namespace == DEFAULT_NAMESPACE and len(self.parts) == 3)
        )


@dataclass(frozen=True, slots=True)
class _Parens:
    """Where the parentheses of a token list are open, from one pass: matching them again for each of thousands of
    parentheses in a pasted statement would be quadratic."""

    group: list[int]  # per token: the offset of the innermost "(" open before it, -1 at the top level
    after: dict[int, int]  # per "(" (its token index): the index after its ")", the end when it never closes
    chain: list[int]  # the "(" still open at the end, innermost first, then -1: the groups a cursor there is in

    @classmethod
    def of(cls, tokens: list[Token]) -> "_Parens":
        group: list[int] = []
        after: dict[int, int] = {}
        open_parens: list[int] = []  # token indexes
        for i, token in enumerate(tokens):
            group.append(tokens[open_parens[-1]].start if open_parens else -1)
            if token.kind == TokenKind.LPAREN:
                open_parens.append(i)
            elif token.kind == TokenKind.RPAREN and open_parens:
                after[open_parens.pop()] = i + 1
        after.update((i, len(tokens)) for i in open_parens)
        return cls(group, after, [*(tokens[i].start for i in reversed(open_parens)), -1])


@dataclass(slots=True)
class _Level:
    """What one bracket depth of a statement has shown so far."""

    clause: str = ""  # its last clause word
    query: bool = False  # it is a query: it has a SELECT, or opened with FROM (`(FROM a SELECT …)`)
    first: bool = True  # nothing yet after its opening bracket


def _is_bracket(token: Token, brackets: str) -> bool:
    return token.kind in (TokenKind.LPAREN, TokenKind.RPAREN, TokenKind.OTHER) and token.text in brackets


def _leads(tokens: list[Token]) -> list[int]:
    """The indexes of the tokens after which a keyspace is named: the keywords, and a comma that separates the terms of
    a FROM list, which is a comma whose nearest clause word at its own bracket depth is FROM (`SELECT a, b` and
    `USE KEYS ["x", "y"]` are not). Only the statement itself and its subqueries name keyspaces: the FROM of
    `EXTRACT(year FROM d)` and the USING of `USE INDEX (ix USING GSI)` do not. One pass: a statement can hold thousands
    of commas."""
    leads: list[int] = []
    levels = [_Level()]  # the statement's own level first
    for i, token in enumerate(tokens):
        level = levels[-1]
        if _is_bracket(token, _OPENING):
            level.first = False
            levels.append(_Level())
            continue
        if _is_bracket(token, _CLOSING):
            if len(levels) > 1:
                levels.pop()
            continue
        upper = token.upper if token.kind == TokenKind.WORD else ""
        if upper == "SELECT" or (upper == "FROM" and level.first):
            level.query = True
        if len(levels) == 1 or level.query:
            if token.kind == TokenKind.COMMA and level.clause == "FROM":
                leads.append(i)
            elif upper in _CLAUSE_START:
                level.clause = upper
            if upper in _KEYSPACE_KEYWORDS and not (upper == "UPDATE" and word(tokens, i - 1) == "THEN"):
                leads.append(i)  # MERGE … THEN UPDATE SET names no keyspace
        level.first = False
    return leads


def _read_alias(tokens: list[Token], i: int) -> str | None:
    if word(tokens, i) == "AS":
        i += 1
    if i < len(tokens) and (tokens[i].kind == TokenKind.QUOTED or
                            (tokens[i].kind == TokenKind.WORD and tokens[i].upper not in _NOT_AN_ALIAS)):
        return unquote(tokens[i])
    return None


def _read_reference(tokens: list[Token], i: int, group: int, parens: _Parens) -> _Reference | None:
    """The reference that starts at `i`; None when there is none (a path still being typed, `FROM a.`, is none)."""
    if i >= len(tokens):
        return None
    if tokens[i].kind == TokenKind.LPAREN:  # a subquery names nothing but its alias
        alias = _read_alias(tokens, parens.after[i])
        return _Reference((), alias, group) if alias is not None else None
    if tokens[i].kind not in IDENT or word(tokens, i) in _NOT_AN_ALIAS:
        return None
    parts = [unquote(tokens[i])]
    namespace = None
    i += 1
    if i < len(tokens) and tokens[i].kind == TokenKind.OTHER and tokens[i].text == ":":  # `namespace:path`
        if i + 1 >= len(tokens) or tokens[i + 1].kind not in IDENT:
            return None  # a namespace still being typed, as a path ending in a dot is
        namespace, parts = parts[0], [unquote(tokens[i + 1])]
        i += 2
    while i + 1 < len(tokens) and tokens[i].kind == TokenKind.DOT and tokens[i + 1].kind in IDENT:
        parts.append(unquote(tokens[i + 1]))
        i += 2
    if i < len(tokens) and tokens[i].kind == TokenKind.DOT:
        return None
    return _Reference(tuple(parts), _read_alias(tokens, i), group, namespace)


def _references(tokens: list[Token]) -> list[_Reference]:
    """Every keyspace reference of the statement, in the order written."""
    parens = _Parens.of(tokens)
    found = (_read_reference(tokens, i + 1, parens.group[i], parens) for i in _leads(tokens))
    return [reference for reference in found if reference is not None]


def _alias_at(name: str, references: list[_Reference], before: list[Token]) -> _Reference | None:
    """What `name` stands for at the end of `before`: the reference of that name in the innermost group around it that
    has one (an inner `FROM orders u` hides an outer `FROM users u`), else the first of that name anywhere."""
    named = [reference for reference in references if reference.name == name]
    for group in _Parens.of(before).chain:
        for reference in named:
            if reference.group == group:
                return reference
    return named[0] if named else None


def _keyspace_of(reference: _Reference | None, context: QueryContext | None) -> Keyspace | None:
    """The keyspace a reference names under the tab's context, as the server reads it with that `query_context`: a path
    of three parts as written; one part a collection of the context's scope (`_default` of its bucket without one); two
    parts `scope.collection` of the context's bucket under a bucket-only context (ruling B47), and none otherwise
    (under a scope the server calls it ambiguous, and without a context there is no bucket). A name with a namespace
    is no relative name (see `_Reference.names_collection`): `system:keyspaces` names no collection."""
    match reference.parts if reference is not None and reference.names_collection else ():
        case (bucket, scope, collection):
            names = bucket, scope, collection
        case (scope, collection) if context is not None and context.scope is None:
            names = context.bucket, scope, collection
        case (collection,) if context is not None:
            names = context.bucket, context.scope or DEFAULT, collection
        case (bucket,):
            names = bucket, DEFAULT, DEFAULT
        case _:
            return None
    try:
        return Keyspace(bucket=names[0], scope=names[1], collection=names[2])
    except ValueError:  # an empty or overlong name
        return None


def _only_keyspace(references: list[_Reference], context: QueryContext | None) -> Keyspace | None:
    """The keyspace a statement reads when it reads exactly one: a single FROM term (subqueries counted, a subquery
    with no FROM of its own is none), resolved as `_keyspace_of` says; never a guess between two."""
    return _keyspace_of(references[0], context) if len(references) == 1 else None


def statement_keyspace(text: str, context: QueryContext | None) -> Keyspace | None:
    """The keyspace the statement `text` reads when it reads exactly one, resolved under the run's `context` as the
    server resolves it; None for none, for several, or for a name that cannot be resolved (a relative name without a
    context, `scope.collection` under a scope, a subquery in FROM). Whatever names completion would offer fields of."""
    return _only_keyspace(_references(significant(tokenize(text, SQLPP))), context)


# --- candidates ---------------------------------------------------------------------------------------------------


def _named(names: Iterable[str], kind: CompletionKind, detail: str, boost: int = 0) -> list[Candidate]:
    return by_length(Candidate.of(name, kind, detail, quote_ident(SQLPP, name), boost) for name in names)


def _collections(tree: KeyspaceTree, bucket: str, scope: str) -> list[Candidate]:
    boost = SYSTEM_BOOST if tree.is_system(bucket, scope) else 0
    return _named(tree.collections.get((bucket, scope), ()), "collection", f"collection in {bucket}.{scope}", boost)


def _scopes(tree: KeyspaceTree, bucket: str) -> list[Candidate]:
    return by_length(
        Candidate.of(scope, "scope", f"scope in {bucket}", quote_ident(SQLPP, scope),
                     SYSTEM_BOOST if tree.is_system(bucket, scope) else 0)
        for scope in tree.scopes.get(bucket, ())
    )


def _keyspace_candidates(parts: list[str], tree: KeyspaceTree, context: QueryContext | None) -> list[Candidate]:
    """Where a keyspace is named: the context's collections and every bucket; after `b.` the scopes of b, after
    `b.s.` its collections. Under a bucket-only context `s.` is also a scope of the context's bucket (ruling B47)."""
    match parts:
        case []:
            here = _collections(tree, context.bucket, context.scope or DEFAULT) if context is not None else []
            return [*here, *_named(tree.buckets, "bucket", "bucket")]
        case [first]:
            in_bucket = context is not None and context.scope is None
            return [*_scopes(tree, first), *(_collections(tree, context.bucket, first) if in_bucket else [])]
        case [bucket, scope]:
            return _collections(tree, bucket, scope)
        case _:
            return []


def _field_detail(path: FieldPath, collection: str) -> str:
    kinds = "|".join(kind for kind, _ in sorted(path.types.items(), key=lambda item: (-item[1], item[0])))
    return f"{collection} · {kinds} · {path.percent:g}%"


def _field_candidates(keyspace: Keyspace | None, fields: FieldLookup, below: list[str]) -> list[Candidate]:
    """The sampled keys one level below the path `below` of a keyspace, in the order the sample first saw them. A path
    through an array element (`items[0].x`) is not completed."""
    paths = fields(keyspace) if keyspace is not None else None
    if keyspace is None or not paths:
        return []
    depth = len(below)
    below_here = (path for path in paths if len(path.path) == depth + 1 and path.path[:depth] == below)
    return [
        Candidate.of(key, "field", _field_detail(path, keyspace.collection), quote_ident(SQLPP, key))
        for path in below_here if (key := path.path[depth]) is not None  # None: an array element
    ]


def _reference_candidates(references: list[_Reference]) -> list[Candidate]:
    """What can be written before a dot: each alias, and each keyspace referred to by its own name, once."""
    found: dict[str, Candidate] = {}
    for reference in references:
        if (name := reference.name) is not None and name not in found:
            found[name] = Candidate.of(name, "alias", reference.detail, quote_ident(SQLPP, name))
    return list(found.values())


def _seen_candidates(seen_fields: Sequence[str], offered: set[str]) -> list[Candidate]:
    return [
        Candidate.of(name, "field", "seen in results", quote_ident(SQLPP, name), SEEN_BOOST)
        for name in dict.fromkeys(seen_fields) if name and name not in offered
    ]


# --- entry points -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Known:
    """What a completion knows besides the text: the keyspaces, the tab's context and the fields."""

    tree: KeyspaceTree
    context: QueryContext | None
    fields: FieldLookup
    seen_fields: Sequence[str]


def _qualified_fields(parts: list[str], before: list[Token], statement: list[Token],
                      known: _Known) -> list[Candidate]:
    """`a.` / `a.b.`: the sampled fields below `b` of the keyspace `a` stands for."""
    reference = _alias_at(parts[0], _references(statement), before)
    return _field_candidates(_keyspace_of(reference, known.context), known.fields, parts[1:])


def _column_candidates(statement: list[Token], known: _Known) -> list[Candidate]:
    """No qualifier where a column goes: the aliases, the fields of the keyspace the statement reads when it reads
    exactly one (never a guess between two), the fields seen in results, then the keywords."""
    references = _references(statement)
    aliases = _reference_candidates(references)
    only = _only_keyspace(references, known.context)
    sampled = _field_candidates(only, known.fields, [])
    offered = {candidate.label for candidate in (*aliases, *sampled)}
    return [*aliases, *sampled, *_seen_candidates(known.seen_fields, offered), *keywords(SQLPP, "clause")]


def _candidates(before: list[Token], statement: list[Token], known: _Known) -> Iterable[Candidate]:
    """Everything that fits where `before` ends, unfiltered. `statement` are all the significant tokens of the
    statement."""
    parts = qualifier(before)
    lead = before[:len(before) - 2 * len(parts)]  # what comes before `a.b.`
    if lead and _leads(lead)[-1:] == [len(lead) - 1]:
        return _keyspace_candidates(parts, known.tree, known.context)
    if parts:
        return _qualified_fields(parts, before, statement, known)
    if before and before[-1].kind == TokenKind.DOT:
        return []  # `a.items[0].`: only a path of plain keys goes on
    context, _ = clause_context(before)
    if context == "start":
        return keywords(SQLPP, "start")
    if context in ("clause", "relations"):  # "relations" here is a keyspace position this module does not know
        return keywords(SQLPP, "clause")
    return _column_candidates(statement, known)


def complete_sqlpp(text: str, cursor: int, index: SnapshotIndex | None, context: QueryContext | None,
                   fields: FieldLookup, seen_fields: Sequence[str]) -> CompletionResult:
    """Completions at `cursor`, from the snapshot's keyspaces (`index`), the tab's `context`, the sampled `fields` of
    a keyspace (only read: a sample is made by the user) and the field names `seen_fields` in recent results. Never
    raises."""
    cursor = min(max(cursor, 0), len(text))
    empty = CompletionResult(start=cursor, end=cursor, items=[])
    known = _Known(index.keyspaces if index is not None else NO_KEYSPACES, context, fields, seen_fields)
    return guarded("SQL++ completion", cursor, empty, lambda: _complete(text, cursor, known))


def _complete(text: str, cursor: int, known: _Known) -> CompletionResult:
    typed = word_at_cursor(text, cursor, SQLPP, quoted_names=True)
    if typed is None:
        return CompletionResult(start=cursor, end=cursor, items=[])
    candidates = _candidates(typed.before, typed.statement, known)
    if typed.quoted:  # a back-ticked word is a name, never a keyword
        candidates = (candidate for candidate in candidates if candidate.kind != "keyword")
    return CompletionResult(start=typed.start, end=typed.end, items=filter_candidates(candidates, typed.prefix))
