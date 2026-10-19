"""The buckets, scopes and collections of a Couchbase schema snapshot, in the three lookups SQL++ completion walks:
`b.` → its scopes, `b.s.` → its collections. Pure; names keep the snapshot's order, each once."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..domain.schema import SnapshotKeyspace


@dataclass(frozen=True)
class KeyspaceTree:
    buckets: tuple[str, ...]
    scopes: Mapping[str, tuple[str, ...]]  # bucket → its scopes
    collections: Mapping[tuple[str, str], tuple[str, ...]]  # (bucket, scope) → its collections
    system_scopes: frozenset[tuple[str, str]]  # the (bucket, scope) pairs of system keyspaces (`_system`)

    @classmethod
    def of(cls, keyspaces: Iterable[SnapshotKeyspace]) -> "KeyspaceTree":
        buckets: dict[str, None] = {}  # dicts as ordered sets
        scopes: dict[str, dict[str, None]] = {}
        collections: dict[tuple[str, str], dict[str, None]] = {}
        system: set[tuple[str, str]] = set()
        for keyspace in keyspaces:
            buckets.setdefault(keyspace.bucket)
            scopes.setdefault(keyspace.bucket, {}).setdefault(keyspace.scope)
            collections.setdefault((keyspace.bucket, keyspace.scope), {}).setdefault(keyspace.collection)
            if keyspace.system:
                system.add((keyspace.bucket, keyspace.scope))
        return cls(
            buckets=tuple(buckets),
            scopes=MappingProxyType({bucket: tuple(names) for bucket, names in scopes.items()}),
            collections=MappingProxyType({pair: tuple(names) for pair, names in collections.items()}),
            system_scopes=frozenset(system),
        )

    def is_system(self, bucket: str, scope: str) -> bool:
        return (bucket, scope) in self.system_scopes


NO_KEYSPACES = KeyspaceTree.of(())  # a relational snapshot, or no snapshot yet
