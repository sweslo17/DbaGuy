"""The Couchbase object tree from system catalogs (spec §3.8): buckets › scopes › collections › indexes,
generated DDL and the schema snapshot. Every name goes in as a named parameter; read-only queries only (the caller sends
readonly=true). Pure apart from the Query callable.

What the user sees is what the server's privilege filter lets through: a limited user's `system:buckets`,
`system:scopes`, `system:keyspaces` and `system:indexes` hold only what it may read (indexes: only with the
index-listing privilege, which `query_select` and `data_reader` do not give, A18), so no REST endpoint (403 for such
users) is needed.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ...domain.couchbase import DEFAULT
from ...domain.errors import NotFound
from ...domain.metadata import NodeKind, ObjectDetail, ObjectNode, PathSegment, make_node
from ...domain.schema import SchemaSnapshot, SnapshotKeyspace
from ...sql.dialects import Dialect, force_quote_ident, quote_literal

# Runs one read-only statement with named parameters (name without `$` → JSON literal text) and returns the decoded
# result rows.
Query = Callable[[str, Mapping[str, str]], list[Any]]

SYSTEM_SCOPE = "_system"  # never listed by any catalog on 7.6.12 (R-04a-C5): handled for the servers that do list it

_BUCKETS = "SELECT RAW b.name FROM system:buckets AS b ORDER BY b.name"
_BUCKET_EXISTS = "SELECT RAW b.name FROM system:buckets AS b WHERE b.name = $b LIMIT 1"
_SCOPES = "SELECT RAW s.name FROM system:scopes AS s WHERE s.`bucket` = $b ORDER BY s.name"
_SCOPE_EXISTS = "SELECT RAW s.name FROM system:scopes AS s WHERE s.`bucket` = $b AND s.name = $s LIMIT 1"
# No `namespace` predicate in the keyspace queries (P35): every row of system:keyspaces is in the one data namespace
# `default` (the 7.x rows carry it as both `namespace` and `namespace_id`, A22), so there is nothing to filter. `scope`
# and `bucket` are back-ticked as keywords are; `namespace` would be too (R-04a-C6) if a query named it.
_COLLECTIONS = "SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` = $b AND k.`scope` = $s ORDER BY k.name"
_COLLECTION_EXISTS = (
    "SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` = $b AND k.`scope` = $s AND k.name = $c LIMIT 1"
)
# The default collection is the bucket's own keyspace: the row has no `bucket` field and the bucket's name.
_BUCKET_KEYSPACE = "SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` IS MISSING AND k.name = $b LIMIT 1"
_INDEXES = (
    "SELECT RAW i FROM system:indexes AS i WHERE i.bucket_id = $b AND i.scope_id = $s AND i.keyspace_id = $c "
    "ORDER BY i.name"
)
# The indexes of `_default._default` have no bucket_id or scope_id: their keyspace_id is the bucket.
_DEFAULT_INDEXES = (
    "SELECT RAW i FROM system:indexes AS i WHERE i.keyspace_id = $b AND i.bucket_id IS MISSING ORDER BY i.name"
)
_KEYSPACES = (
    "SELECT k.`bucket` AS b, k.`scope` AS s, k.name AS n FROM system:keyspaces AS k "
    "ORDER BY k.`bucket`, k.`scope`, k.name"
)

_BUCKET_COMMENT = (
    "Buckets are created and managed in the Couchbase Web Console: cluster management is not part of DbaGuy"
)
# Scopes and collections that no CREATE statement makes (P9): the default scope exists with its bucket and cannot be
# dropped; the default collection cannot be created again (it can only be dropped, and then it stays gone); the system
# scope and its collections belong to the server.
_BUILT_IN_SCOPE_DDL = {
    DEFAULT: "-- the default scope exists with its bucket: it cannot be created or dropped",
    SYSTEM_SCOPE: "-- the system scope belongs to the server: it cannot be created or dropped",
}
_DEFAULT_COLLECTION_DDL = "-- the default collection exists with its bucket: it cannot be created again once dropped"
_SYSTEM_COLLECTION_DDL = "-- a collection of the system scope belongs to the server: it cannot be created or dropped"
_NO_SUCH_OBJECT = "No such object"


def _ident(name: str) -> str:
    return force_quote_ident(Dialect.SQLPP, name)


def _keyspace(bucket: str, scope: str, collection: str) -> str:
    return f"{_ident(bucket)}.{_ident(scope)}.{_ident(collection)}"


def _named(*names: str) -> dict[str, str]:
    """The query parameters `$b`, `$s`, `$c` for the bucket, scope and collection names given (a prefix of them), each
    as a JSON string literal."""
    return {key: quote_literal(Dialect.SQLPP, name) for key, name in zip("bsc", names, strict=False)}


def _is_default_collection(scope: str, collection: str) -> bool:
    return scope == DEFAULT and collection == DEFAULT


def _state(index: Mapping[str, Any]) -> str:
    return str(index.get("state") or "unknown")


def index_detail(index: Mapping[str, Any]) -> str:
    state = _state(index)
    return f"primary · {state}" if index.get("is_primary") else state


def index_ddl(index: Mapping[str, Any], bucket: str, scope: str, collection: str) -> str:
    """CREATE [PRIMARY] INDEX from a system:indexes row (/indexStatus is 403 for limited users, survey §5.2): key,
    condition and partition expressions as the server wrote them; WITH only for what differs from the default."""
    on = _keyspace(bucket, scope, collection)
    if index.get("is_primary"):
        text = f"CREATE PRIMARY INDEX {_ident(index['name'])} ON {on}"
    else:
        text = f"CREATE INDEX {_ident(index['name'])} ON {on}({', '.join(index.get('index_key') or [])})"
    if index.get("partition"):
        text += f" PARTITION BY {index['partition']}"
    if index.get("condition"):
        text += f" WHERE {index['condition']}"
    options: dict[str, Any] = {}
    if index.get("state") == "deferred":
        options["defer_build"] = True
    replicas = (index.get("metadata") or {}).get("num_replica")
    if type(replicas) is int and replicas > 0:  # not a bool: True is an int
        options["num_replica"] = replicas
    if options:
        text += f" WITH {json.dumps(options)}"
    return text + ";"


def _index_comment(index: Mapping[str, Any]) -> str:
    """State, last scan and engine of an index, as the server reports them (a part it did not report is left out)."""
    scanned = (index.get("metadata") or {}).get("last_scan_time")
    using = index.get("using")
    parts = (_state(index), f"last scanned {scanned}" if scanned else None, f"using {using}" if using else None)
    return " · ".join(part for part in parts if part)


# ---------------------------------------------------------------------------------------------------------------------
# Existence. Whether the path a call names still exists is asked only once an answer came back empty (a listing) or
# is the answer itself (a detail), so the common call is one round trip: an empty answer is a stale path or a level
# with nothing to show, and only the parent tells which.


def _require_bucket(query: Query, bucket: str) -> None:
    if not query(_BUCKET_EXISTS, _named(bucket)):
        raise NotFound(f"Bucket '{bucket}' not found")


def _require_scope(query: Query, bucket: str, scope: str) -> None:
    if scope == DEFAULT:  # system:scopes never lists it: it exists as long as the bucket does
        _require_bucket(query, bucket)
    elif not query(_SCOPE_EXISTS, _named(bucket, scope)):
        raise NotFound(f"Scope '{scope}' not found")


def _require_collection(query: Query, bucket: str, scope: str, collection: str) -> None:
    if _is_default_collection(scope, collection):
        found = query(_BUCKET_KEYSPACE, _named(bucket))
    else:
        found = query(_COLLECTION_EXISTS, _named(bucket, scope, collection))
    if not found:
        raise NotFound(f"Collection '{collection}' not found")


def _index_rows(query: Query, bucket: str, scope: str, collection: str) -> list[Mapping[str, Any]]:
    if _is_default_collection(scope, collection):
        return query(_DEFAULT_INDEXES, _named(bucket))
    return query(_INDEXES, _named(bucket, scope, collection))


# ---------------------------------------------------------------------------------------------------------------------
# Children


def _buckets(query: Query, path: list[PathSegment]) -> list[ObjectNode]:
    return [make_node(path, NodeKind.BUCKET, name, has_children=True) for name in query(_BUCKETS, {})]


def _scopes(query: Query, path: list[PathSegment]) -> list[ObjectNode]:
    bucket = path[0].name
    listed = query(_SCOPES, _named(bucket))
    if not listed:
        _require_bucket(query, bucket)  # a bucket always has its default scope, so empty can mean a bucket that is gone
    names = [DEFAULT, *(name for name in listed if name not in (DEFAULT, SYSTEM_SCOPE))]
    if SYSTEM_SCOPE in listed:
        names.append(SYSTEM_SCOPE)
    return [
        make_node(path, NodeKind.SCOPE, name, has_children=True, system=name == SYSTEM_SCOPE) for name in names
    ]


def _collections(query: Query, path: list[PathSegment]) -> list[ObjectNode]:
    bucket, scope = path[0].name, path[1].name
    names = [DEFAULT] if scope == DEFAULT and query(_BUCKET_KEYSPACE, _named(bucket)) else []  # always first
    names = list(dict.fromkeys([*names, *query(_COLLECTIONS, _named(bucket, scope))]))  # once, whoever lists it
    if not names:
        _require_scope(query, bucket, scope)
    return [
        make_node(path, NodeKind.COLLECTION, name, has_children=True, system=scope == SYSTEM_SCOPE) for name in names
    ]


def _indexes(query: Query, path: list[PathSegment]) -> list[ObjectNode]:
    bucket, scope, collection = (segment.name for segment in path)
    rows = _index_rows(query, bucket, scope, collection)
    if not rows:  # a collection that has none is fine; one that is gone is not
        _require_collection(query, bucket, scope, collection)
    return [
        make_node(path, NodeKind.INDEX, row["name"], has_children=False, detail=index_detail(row)) for row in rows
    ]


_CHILDREN: dict[tuple[str, ...], Callable[[Query, list[PathSegment]], list[ObjectNode]]] = {
    (): _buckets,
    (NodeKind.BUCKET,): _scopes,
    (NodeKind.BUCKET, NodeKind.SCOPE): _collections,
    (NodeKind.BUCKET, NodeKind.SCOPE, NodeKind.COLLECTION): _indexes,
}


def list_children(query: Query, path: list[PathSegment]) -> list[ObjectNode]:
    """The nodes right under `path` (`[]` = the buckets). A path of a shape the tree has no level for, or one whose
    object is gone, is NotFound."""
    handler = _CHILDREN.get(tuple(segment.kind for segment in path))
    if handler is None:
        raise NotFound(_NO_SUCH_OBJECT)
    return handler(query, path)


# ---------------------------------------------------------------------------------------------------------------------
# Details: DDL is always generated (Couchbase keeps none), so every detail says so.


def _detail(path: list[PathSegment], ddl: str | None, comment: str | None = None) -> ObjectDetail:
    last = path[-1]
    return ObjectDetail(path=path, kind=last.kind, name=last.name, ddl=ddl, ddl_is_generated=True, comment=comment)


def _bucket_detail(query: Query, path: list[PathSegment]) -> ObjectDetail:
    _require_bucket(query, path[0].name)
    return _detail(path, None, _BUCKET_COMMENT)


def _scope_detail(query: Query, path: list[PathSegment]) -> ObjectDetail:
    bucket, scope = path[0].name, path[1].name
    _require_scope(query, bucket, scope)
    return _detail(path, _BUILT_IN_SCOPE_DDL.get(scope) or f"CREATE SCOPE {_ident(bucket)}.{_ident(scope)};")


def _collection_detail(query: Query, path: list[PathSegment]) -> ObjectDetail:
    bucket, scope, collection = (segment.name for segment in path)
    indexes = _index_rows(query, bucket, scope, collection)
    if not indexes:
        _require_collection(query, bucket, scope, collection)
    if _is_default_collection(scope, collection):
        create = _DEFAULT_COLLECTION_DDL
    elif scope == SYSTEM_SCOPE:
        create = _SYSTEM_COLLECTION_DDL
    else:
        create = f"CREATE COLLECTION {_keyspace(bucket, scope, collection)};"
    return _detail(path, "\n\n".join([create, *(index_ddl(row, bucket, scope, collection) for row in indexes)]))


def _index_detail(query: Query, path: list[PathSegment]) -> ObjectDetail:
    bucket, scope, collection, name = (segment.name for segment in path)
    row = next((row for row in _index_rows(query, bucket, scope, collection) if row.get("name") == name), None)
    if row is None:
        raise NotFound(f"Index '{name}' not found")
    return _detail(path, index_ddl(row, bucket, scope, collection), _index_comment(row))


_DETAILS: dict[tuple[str, ...], Callable[[Query, list[PathSegment]], ObjectDetail]] = {
    (NodeKind.BUCKET,): _bucket_detail,
    (NodeKind.BUCKET, NodeKind.SCOPE): _scope_detail,
    (NodeKind.BUCKET, NodeKind.SCOPE, NodeKind.COLLECTION): _collection_detail,
    (NodeKind.BUCKET, NodeKind.SCOPE, NodeKind.COLLECTION, NodeKind.INDEX): _index_detail,
}


def object_detail(query: Query, path: list[PathSegment]) -> ObjectDetail:
    handler = _DETAILS.get(tuple(segment.kind for segment in path))
    if handler is None:
        raise NotFound(_NO_SUCH_OBJECT)
    return handler(query, path)


# ---------------------------------------------------------------------------------------------------------------------
# Snapshot


def build_snapshot(query: Query) -> SchemaSnapshot:
    """Every keyspace the user can see, from one query (ruling P7); the bucket-level row is `_default._default`."""
    keyspaces = []
    for row in query(_KEYSPACES, {}):
        bucket, scope, name = row.get("b"), row.get("s"), row["n"]
        if bucket is None:  # the bucket's own keyspace: its default collection
            bucket, scope, name = name, DEFAULT, DEFAULT
        keyspaces.append(SnapshotKeyspace(bucket=bucket, scope=scope, collection=name, system=scope == SYSTEM_SCOPE))
    # The renamed bucket-level rows moved, so the server's order is not the order of the new names
    keyspaces.sort(key=lambda k: (k.bucket, k.scope, k.collection))
    logger.debug("Couchbase schema snapshot: {} keyspaces", len(keyspaces))
    return SchemaSnapshot(
        database=None, default_schema=None, search_path=[], schemas=[], keyspaces=keyspaces,
        built_at=datetime.now(UTC),
    )
