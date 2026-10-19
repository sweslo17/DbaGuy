"""Which query nodes a Couchbase profile can use (spec §3.6.3): the cluster's nodeServices map, asked through a seed's
manager port (works for limited users; anonymous gets 401), or else the seeds themselves. Pure parsing plus one small
blocking GET per seed; the connection itself is the adapter's."""

import http.client
import io
import json
import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .http import READ_CHUNK, _connect, failure_reason
from .settings import CouchbaseSettings

DISCOVERY_TIMEOUT_S = 3.0  # for the whole exchange with one seed: connect, TLS, request and answer
NODE_SERVICES_PATH = "/pools/default/nodeServices"
MAX_ANSWER_BYTES = 1024 * 1024  # a map of hundreds of nodes is a few hundred KB; a manager port that streams is not one
MAX_CANDIDATES = 64  # a cluster has dozens of query nodes at most; a map is the cluster's word, not a reason to try 20,000
# What a host name or address of a node may be made of (IPv6 and its zone id included). A map is data from the network:
# a host that holds a space, a newline or an `@` would be a header line, a part of a URL or a log line.
_HOST_NAME = re.compile(r"[A-Za-z0-9._:%-]{1,253}")
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


def _node(host: Any, port: Any) -> QueryNode | None:
    """The node, or None when `host` is no host name or `port` no TCP port (an entry that cannot be used)."""
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        return None
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    return QueryNode(host, port) if _HOST_NAME.fullmatch(host) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _external(entry: Any) -> Mapping[str, Any]:
    """`alternateAddresses.external` of a node entry: how a client outside the cluster's network reaches the node."""
    return _mapping(_mapping(_mapping(entry).get("alternateAddresses")).get("external"))


def parse_node_services(body: Any, asked_host: str, tls: bool) -> list[QueryNode]:
    """The nodes that run the query service, the asked node first (the others in the map's order). A node entry without
    `hostname` is the node that was asked (A8: the dev cluster's single node has none). When the asked host is the
    external hostname of some node, every node is addressed by its external hostname, with the ports its external
    address changes (the SDK's network=auto rule). An entry whose host is no host name is dropped, a node is listed
    once, and the list stops at MAX_CANDIDATES. Raises ValueError when `body` is not a nodeServices map."""
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
        node = _node(host, ports.get(service))
        if node is None:
            continue  # no query service on this node, or an entry that cannot be used
        (asked if entry.get("thisNode") is True or node.host == asked_host else others).append(node)
    return list(dict.fromkeys(asked + others))[:MAX_CANDIDATES]  # once each, the asked node first


def _left(deadline: float) -> float:
    """What is left of the deadline; TimeoutError when it has passed."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("timed out")
    return left


class _DeadlineReader(io.RawIOBase):
    """The bytes http.client reads, each read bounded by what is left of ONE deadline: a socket timeout restarts with
    every byte, so a server that sends one byte a second would otherwise hold the connection as long as it likes."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._sock, self._deadline = sock, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self._sock.settimeout(_left(self._deadline))
        return self._sock.recv_into(buffer)


class _Reply:
    """What `http.client.HTTPResponse` reads from instead of a socket: it only asks for a file."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock, self._deadline = sock, deadline

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline))


def _read_body(response: http.client.HTTPResponse) -> bytes:
    body = bytearray()
    while chunk := response.read1(READ_CHUNK):
        body += chunk
        if len(body) > MAX_ANSWER_BYTES:
            raise DiscoveryFailed("the answer is too large")
    if response.length:  # a Content-Length answer that ended early: read1() returns b"" there instead of raising
        raise DiscoveryFailed("the answer was cut short")
    return bytes(body)


def _fetcher(tls: ssl.SSLContext | None, auth: str) -> Fetch:
    def fetch(host: str, port: int) -> bytes:
        if not _HOST_NAME.fullmatch(host):  # the request is written by hand: no header line from a host name
            raise DiscoveryFailed("invalid host name")
        deadline = time.monotonic() + DISCOVERY_TIMEOUT_S  # read at call time
        request = (
            f"GET {NODE_SERVICES_PATH} HTTP/1.1\r\nHost: {QueryNode(host, port).label}\r\n"
            f"Authorization: {auth}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        # Task 7's connect: one deadline over every address (name resolution stays blocking). An IPv6 host stays without
        # brackets for `getaddrinfo`.
        sock = _connect(host, port, DISCOVERY_TIMEOUT_S, lambda: False)
        try:
            if tls is not None:
                sock.settimeout(_left(deadline))  # the handshake runs within the time that is left
                sock = tls.wrap_socket(sock, server_hostname=host)
            sock.settimeout(_left(deadline))
            sock.sendall(request)
            response = http.client.HTTPResponse(_Reply(sock, deadline), method="GET")  # type: ignore[arg-type]
            response.begin()
            if response.status != 200:
                raise DiscoveryFailed(f"HTTP {response.status}")
            return _read_body(response)
        finally:
            sock.close()

    return fetch


def _shown(seed: str) -> str:
    """A seed as the reason may show it: a host name, not whatever a profile field held (a newline is a log line)."""
    return seed if _HOST_NAME.fullmatch(seed) else "(invalid host)"


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
            reasons.append(f"{_shown(seed)}: {failure_reason(exc)}")
            continue
        if nodes:
            return nodes, None
        reasons.append(f"{_shown(seed)}: no query node in the cluster map")
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
