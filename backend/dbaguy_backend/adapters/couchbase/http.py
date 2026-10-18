"""The query service over HTTP/1.1 with the standard library (spec §3.4.1): one keep-alive connection at a time to one
query node, request bodies assembled as text (parameter literals embedded unchanged), and a shutdown any thread may call
to stop the request at once (the server marks it `closed`, survey §3.7).

Threads: the session's worker thread sends, reads and closes. Another thread only calls `HttpChannel.shutdown()`, which
acts on a duplicate of the TCP socket taken right after connecting and kept until close(): http.client may close its own
socket at any time, and a descriptor number closed and reused meanwhile would let a late cancel shut down an unrelated
connection (ruling P3). SSLSocket.shutdown() is never called from another thread (spec §3.4.1): shutting down the TCP
duplicate ends a blocked TLS read within a millisecond (probe A10 held, so ruling P41's fallback is not used).

A connect polls for a cancel every 50 ms (name resolution stays blocking), so a node that does not answer never holds
a cancel for CONNECT_TIMEOUT_S (ruling R-04a-T7a).

The server sends no status line until its first output is ready (a long aggregate: minutes), so `NodeClient.send()` can
block that long; a cancel during that wait shuts the channel down like any other (the caller gets the channel from
`attach` before anything is sent, and arms its own timer before calling `send`)."""

import base64
import errno
import http.client
import json
import os
import re
import selectors
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from dbaguy_backend.adapters.sockets import enable_keepalive

READ_CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 10.0  # connect and TLS handshake; reads afterwards block with no timeout (a cancel ends them)
_CONNECT_POLL_S = 0.05  # how often a connect in progress looks for a cancel (ruling R-04a-T7a)
_IN_PROGRESS = frozenset({errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY})
KEEPALIVE_REUSE_S = 30.0  # a connection idle for longer is replaced rather than reused
QUERY_PATH = "/query/service"

_NOT_CONTEXT_ID = re.compile(r"[^A-Za-z0-9_-]")
_CONTEXT_ID_LIMIT = 64  # longer ids are cut by the server; `/` and `"` are refused (error 1110)

SocketFactory = Callable[[str, int, float], socket.socket]  # (host, port, timeout): a connected socket (tests)


def client_context_id(ident: str) -> str:
    """`dbaguy-` + any id string (Task 11 passes `uuid4().hex`, P30, P47), reduced to what the server accepts."""
    return ("dbaguy-" + _NOT_CONTEXT_ID.sub("", ident))[:_CONTEXT_ID_LIMIT]


def basic_auth(user: str, password: str) -> str:
    """The `Authorization` header value. Never logged."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


@dataclass(frozen=True)
class QueryRequest:
    """One request. `named` / `positional` hold JSON literals as text, sent exactly as given."""

    statement: str
    client_context_id: str
    query_context: str | None = None
    readonly: bool = False
    named: Mapping[str, str] = field(default_factory=dict)  # not `= {}`: a dataclass refuses a mutable default (P39)
    positional: Sequence[str] = ()
    profile: str | None = None
    preserve_expiry: bool = False


def build_body(request: QueryRequest) -> bytes:
    """The body as text, so parameter literals go out exactly as given (a number never passes through float; R-04-10).
    Never `timeout` (it keeps counting while a result is paused, R-04-5) nor `scan_consistency`. A lone surrogate in
    any text raises UnicodeEncodeError."""
    parts = [f'"statement":{json.dumps(request.statement, ensure_ascii=False)}',
             f'"client_context_id":{json.dumps(request.client_context_id)}']
    if request.query_context is not None:
        parts.append(f'"query_context":{json.dumps(request.query_context, ensure_ascii=False)}')
    if request.readonly:
        parts.append('"readonly":true')
    parts.extend(f"{json.dumps('$' + name, ensure_ascii=False)}:{literal}" for name, literal in request.named.items())
    if request.positional:
        parts.append('"args":[' + ",".join(request.positional) + "]")
    if request.profile is not None:
        parts.append(f'"profile":{json.dumps(request.profile)}')
    if request.preserve_expiry:
        parts.append('"preserve_expiry":true')
    return ("{" + ",".join(parts) + "}").encode("utf-8")


class ChannelError(Exception):
    """The connection failed. `before_response`: no byte of the answer had arrived (what a kept-alive connection the
    server closed meanwhile looks like); `tls`: setting up TLS failed (handshake, certificate)."""

    def __init__(self, message: str, *, before_response: bool = False, tls: bool = False) -> None:
        super().__init__(message)
        self.before_response, self.tls = before_response, tls


# What a kept-alive connection the server has closed meanwhile raises before any byte of the answer (RemoteDisconnected
# is a ConnectionResetError; SSLEOFError is a TLS peer that closed without close_notify).
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
          ssl.SSLEOFError)
_FAILURES = (http.client.HTTPException, OSError, ValueError)  # ssl.SSLError is an OSError


def failure_reason(exc: BaseException) -> str:
    """A short reason a connection failed, for messages and logs (never the request or credentials)."""
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "unknown host"
    return str(exc) or type(exc).__name__


def _endpoint_label(host: str, port: int) -> str:
    """`host:port`, an IPv6 address in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _connect(host: str, port: int, timeout: float, cancelled: Callable[[], bool]) -> socket.socket:
    """socket.create_connection that a cancel ends within _CONNECT_POLL_S (R-04a-T7a): each address is connected without
    blocking and polled, with `cancelled()` checked in between. `timeout` bounds the whole attempt over every address;
    name resolution stays blocking. The socket comes back with `timeout` set: the TLS handshake runs within it."""
    deadline = time.monotonic() + timeout
    error: OSError | None = None
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            _connect_one(sock, address, deadline, cancelled)
        except OSError as exc:
            sock.close()
            if isinstance(exc, TimeoutError):
                raise  # the time is up for every address
            error = exc  # refused, unreachable: the next address
            continue
        except BaseException:
            sock.close()
            raise
        sock.settimeout(timeout)
        return sock
    raise error or OSError(errno.EADDRNOTAVAIL, "no address to connect to")


def _connect_one(sock: socket.socket, address: Any, deadline: float, cancelled: Callable[[], bool]) -> None:
    sock.setblocking(False)
    code = sock.connect_ex(address)
    with selectors.DefaultSelector() as selector:
        selector.register(sock, selectors.EVENT_WRITE)
        while code in _IN_PROGRESS:
            if cancelled():
                raise ChannelError("cancelled while connecting", before_response=True)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out")
            if selector.select(min(_CONNECT_POLL_S, left)):
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if code:
        raise OSError(code, os.strerror(code))  # ECONNREFUSED comes out as ConnectionRefusedError


class _Connection(http.client.HTTPConnection):
    """http.client's connection over the socket its HttpChannel opens."""

    def __init__(self, channel: "HttpChannel") -> None:
        super().__init__(channel.host, channel.port)
        self._channel = channel

    def connect(self) -> None:
        self.sock = self._channel.open_socket()


class HttpChannel:
    """One TCP (TLS) connection to a query node. Dead once shut down, failed or closed: NodeClient opens another.
    `used`: a request went out on it; `idle_since`: when its last response was read to the end (None while one is in
    flight)."""

    def __init__(self, host: str, port: int, *, tls: ssl.SSLContext | None, server_hostname: str | None,
                 socket_factory: SocketFactory | None) -> None:
        self.host, self.port = host, port
        self._tls, self._server_hostname, self._socket_factory = tls, server_hostname or host, socket_factory
        self._lock = threading.Lock()  # a leaf: guards _handle and _shut, held for a shutdown() syscall at most
        self._handle: socket.socket | None = None  # the duplicate a shutdown acts on
        self._shut = False  # shutdown() was called
        self.conn = _Connection(self)
        self.used = False
        self.idle_since: float | None = None
        self.dead = False

    def open_socket(self) -> socket.socket:
        """Worker thread (from http.client's connect): the connected socket, TLS set up; the duplicate kept. A cancel
        while connecting ends the connect within _CONNECT_POLL_S (a test's socket factory: once it returns)."""
        if self._socket_factory is not None:
            sock = self._socket_factory(self.host, self.port, CONNECT_TIMEOUT_S)
        else:
            sock = _connect(self.host, self.port, CONNECT_TIMEOUT_S, lambda: self.shut_down)
        try:
            enable_keepalive(sock)
            with suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # as http.client's own connect()
            handle = sock.dup()
            with self._lock:
                if self._shut:  # a cancel came while connecting
                    handle.close()
                    raise ChannelError("cancelled while connecting", before_response=True)
                self._handle = handle
            if self._tls is not None:
                # the handshake runs within the connect timeout
                sock = self._tls.wrap_socket(sock, server_hostname=self._server_hostname)
            sock.settimeout(None)  # reads wait as long as a paused result needs; a cancel shuts the socket down
            logger.debug("Connected to the query node {} ({})", _endpoint_label(self.host, self.port),
                         "TLS" if self._tls is not None else "plain")
            return sock
        except BaseException:
            sock.close()
            raise

    def shutdown(self) -> None:
        """Any thread: ends whatever the worker is blocked on (sending, waiting for the status line, reading the body)
        at once, and a connect in progress within _CONNECT_POLL_S. The socket objects themselves are left to the
        worker."""
        with self._lock:
            self._shut = True
            if self._handle is not None:
                with suppress(OSError):
                    self._handle.shutdown(socket.SHUT_RDWR)

    @property
    def shut_down(self) -> bool:
        """shutdown() was called (a cancel, or an abandoned response)."""
        with self._lock:
            return self._shut

    def close(self) -> None:
        """Worker thread."""
        self.dead = True
        with self._lock:
            handle, self._handle = self._handle, None
        with suppress(Exception):
            self.conn.close()
        if handle is not None:
            handle.close()

    def reusable(self, now: float) -> bool:
        if self.dead or self.shut_down:
            return False
        return not self.used or (self.idle_since is not None and now - self.idle_since < KEEPALIVE_REUSE_S)


class QueryResponse:
    """The answer to one request, read in chunks by the worker thread. `done`: read to the end (the connection may then
    carry the next request)."""

    def __init__(self, channel: HttpChannel, response: http.client.HTTPResponse, clock: Callable[[], float]) -> None:
        self._channel, self._response, self._clock = channel, response, clock
        self.status, self.reason = response.status, response.reason
        self.content_type = response.getheader("Content-Type") or ""
        self.done = False
        self._failed: ChannelError | None = None  # raised by every later read: a failed body never ends cleanly

    def read_chunk(self) -> bytes:
        """The next bytes of the body (up to READ_CHUNK), b"" at its end. ChannelError when the connection fails."""
        if self._failed is not None:
            raise self._failed
        if self.done:
            return b""
        try:
            data = self._response.read1(READ_CHUNK)
        except _FAILURES as exc:
            error = ChannelError(failure_reason(exc))
            self._stop(error)
            raise error from exc
        if not data:
            if self._response.length:  # a Content-Length body that ended early: http.client's read1() does not raise
                error = ChannelError(failure_reason(http.client.IncompleteRead(b"", self._response.length)))
                self._stop(error)
                raise error
            self.done = True
            if self._response.will_close:
                self._channel.close()
            else:
                self._channel.idle_since = self._clock()
        return data

    def abandon(self) -> None:
        """Stop reading: unless the body was read to its end, the connection is shut down and closed, which stops the
        request on the server at once (`closed`)."""
        if not self.done:
            self._channel.shutdown()
            self._stop(ChannelError("the response was abandoned"))

    def _stop(self, error: ChannelError) -> None:
        self._failed = self._failed or error
        self._channel.close()
        with suppress(Exception):
            self._response.close()  # its file object holds the socket open too (after `Connection: close`)


class NodeClient:
    """Requests to one query node over one HttpChannel at a time (D-04-14): reused while its last response was read to
    the end less than KEEPALIVE_REUSE_S ago, replaced otherwise. A read (`retry_safe`) that meets a kept-alive
    connection the server has closed, before any byte of the answer, is sent once more on a new one; nothing else is
    resent, and never a request whose channel a cancel shut down (R-04a-C2)."""

    def __init__(self, host: str, port: int, *, auth: str, user_agent: str, tls: ssl.SSLContext | None,
                 server_hostname: str | None = None, socket_factory: SocketFactory | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.host, self.port = host, port
        self.label = _endpoint_label(host, port)
        self._headers = {"Authorization": auth, "Content-Type": "application/json", "User-Agent": user_agent}
        self._tls, self._server_hostname = tls, server_hostname
        self._socket_factory = socket_factory  # None: the cancellable connect
        self._clock = clock
        self._channel: HttpChannel | None = None

    def send(self, body: bytes, *, retry_safe: bool, attach: Callable[[HttpChannel], None]) -> QueryResponse:
        """POSTs `body` and waits for the status line. `attach(channel)` is called before anything is sent on a channel
        (the caller records what a cancel shuts down); whatever it raises propagates and nothing is sent."""
        channel = self._channel_for_request()
        reused = channel.used
        try:
            return self._post(channel, body, attach)
        except ChannelError as exc:
            if not (reused and retry_safe and exc.before_response) or channel.shut_down:
                raise  # a write, a fresh connection, a failure mid-answer, or a cancel: never resent
            logger.info("The server closed the kept-alive connection to {}; sending the read again", self.label)
            return self._post(self._channel_for_request(), body, attach)

    def _channel_for_request(self) -> HttpChannel:
        if self._channel is None or not self._channel.reusable(self._clock()):
            if self._channel is not None:
                self._channel.close()
                self._channel = None
            try:
                self._channel = HttpChannel(self.host, self.port, tls=self._tls,
                                            server_hostname=self._server_hostname, socket_factory=self._socket_factory)
            except http.client.InvalidURL as exc:  # a host http.client refuses (a space, a control character)
                raise ChannelError("invalid host name") from exc
        return self._channel

    def _post(self, channel: HttpChannel, body: bytes, attach: Callable[[HttpChannel], None]) -> QueryResponse:
        attach(channel)  # raises when a cancel came first: nothing is sent
        reused = channel.used
        channel.used, channel.idle_since = True, None
        try:
            channel.conn.request("POST", QUERY_PATH, body=body, headers=self._headers)
            response = channel.conn.getresponse()
        except ChannelError:  # cancelled while connecting
            channel.close()
            raise
        except _FAILURES as exc:
            channel.close()
            # TLS failed only while setting it up, and not because a cancel cut the handshake: an error on a connection
            # that has answered before is a lost connection
            tls = isinstance(exc, ssl.SSLError) and not reused and not channel.shut_down
            raise ChannelError(failure_reason(exc), before_response=isinstance(exc, _STALE), tls=tls) from exc
        return QueryResponse(channel, response, self._clock)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
