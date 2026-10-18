"""Which query nodes a Couchbase profile can use (spec §3.6.3): the cluster's nodeServices map, asked through a seed's
manager port (works for limited users; anonymous gets 401), or else the seeds themselves. Pure parsing plus one small
blocking GET per seed; the connection itself is the adapter's."""

import http.client
import json
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .http import failure_reason
from .settings import CouchbaseSettings

DISCOVERY_TIMEOUT_S = 3.0
NODE_SERVICES_PATH = "/pools/default/nodeServices"
MAX_ANSWER_BYTES = 1024 * 1024  # a map of hundreds of nodes is a few hundred KB; a manager port that streams is not one
Fetch = Callable[[str, int], bytes]  # (host, manager port) -> the nodeServices body


class DiscoveryFailed(Exception):
    """The manager answered, but not with a usable nodeServices map."""


@dataclass(frozen=True)
class QueryNode:
    host: str  # an IPv6 address without brackets (R-04a-C18)
    port: int

    @property
    def label(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _external(entry: Any) -> Mapping[str, Any]:
    """`alternateAddresses.external` of a node entry: how a client outside the cluster's network reaches the node."""
    return _mapping(_mapping(_mapping(entry).get("alternateAddresses")).get("external"))


def parse_node_services(body: Any, asked_host: str, tls: bool) -> list[QueryNode]:
    """The nodes that run the query service, the asked node first (the others in the map's order). A node entry without
    `hostname` is the node that was asked (A8: the dev cluster's single node has none). When the asked host is the
    external hostname of some node, every node is addressed by its external hostname, with the ports its external
    address changes (the SDK's network=auto rule). Raises ValueError when `body` is not a nodeServices map."""
    entries = body.get("nodesExt") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise ValueError("not a nodeServices map")
    service = "n1qlSSL" if tls else "n1ql"
    use_external = any(_external(entry).get("hostname") == asked_host for entry in entries)
    asked: list[QueryNode] = []
    others: list[QueryNode] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        services = _mapping(entry.get("services"))
        alternate = _external(entry)
        if use_external and alternate.get("hostname"):
            # `ports` lists only the services whose external port differs from the internal one
            host, ports = alternate["hostname"], {**services, **_mapping(alternate.get("ports"))}
        else:
            host, ports = entry.get("hostname", asked_host), services
        port = ports.get(service)
        if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
            continue  # no query service on this node, or an entry that cannot be used
        node = QueryNode(host[1:-1] if host.startswith("[") and host.endswith("]") else host, port)
        (asked if entry.get("thisNode") is True or node.host == asked_host else others).append(node)
    return asked + others


def _fetcher(tls: ssl.SSLContext | None, auth: str) -> Fetch:
    def fetch(host: str, port: int) -> bytes:
        # An IPv6 host stays without brackets: `getaddrinfo` takes it so, and http.client adds them to the Host header.
        conn = (
            http.client.HTTPSConnection(host, port, timeout=DISCOVERY_TIMEOUT_S, context=tls) if tls is not None
            else http.client.HTTPConnection(host, port, timeout=DISCOVERY_TIMEOUT_S)
        )
        try:
            conn.request("GET", NODE_SERVICES_PATH, headers={"Authorization": auth})
            response = conn.getresponse()
            body = response.read(MAX_ANSWER_BYTES + 1)
        finally:
            conn.close()
        if response.status != 200:
            raise DiscoveryFailed(f"HTTP {response.status}")
        if len(body) > MAX_ANSWER_BYTES:
            raise DiscoveryFailed("the answer is too large")
        return body

    return fetch


def discover(
    seeds: Sequence[str], manager_port: int, *, tls: ssl.SSLContext | None, auth: str, fetch: Fetch | None = None
) -> tuple[list[QueryNode], str | None]:
    """The nodes from the first seed that answers with a usable map, else `[]` and why each seed did not (the INFO line,
    never the request: no header, no body)."""
    fetch = fetch or _fetcher(tls, auth)
    reasons: list[str] = []
    for seed in seeds:
        try:
            nodes = parse_node_services(json.loads(fetch(seed, manager_port)), seed, tls is not None)
        except Exception as exc:  # unreachable, 401, a timeout, not JSON …: the next seed, then the seeds themselves
            reasons.append(f"{seed}: {failure_reason(exc)}")
            continue
        if nodes:
            return nodes, None
        reasons.append(f"{seed}: no query node in the cluster map")
    return [], "; ".join(reasons)


def candidate_nodes(settings: CouchbaseSettings, auth: str, fetch: Fetch | None = None) -> tuple[list[QueryNode], str]:
    """The nodes to try, in order, and how they were found (for the INFO log). Discovery never stops a connection: any
    failure falls back to the seeds themselves, each at the profile's query port."""
    seeds = [QueryNode(host, settings.port) for host in settings.seeds]
    if not settings.discover:
        return seeds, "the SSH tunnel" if settings.tunnel else "the given hosts (no discovery)"
    nodes, failure = discover(settings.seeds, settings.manager_port, tls=settings.tls, auth=auth, fetch=fetch)
    if nodes:
        return nodes, f"{len(nodes)} query node(s) from the cluster map"
    logger.info("Couchbase node discovery failed ({}); trying the given hosts", failure)
    return seeds, f"the given hosts (discovery failed: {failure})"
