"""The query service over HTTP/1.1 with the standard library (spec §3.4.1): one keep-alive connection at a time to one
query node, request bodies assembled as text (parameter literals embedded unchanged, each checked to be one JSON value),
and a shutdown any thread may call to stop the request at once (the server marks it `closed`, survey §3.7).

Threads: the session's worker thread sends, reads and closes. Another thread only calls `HttpChannel.shutdown()`, which
acts on a duplicate of the TCP socket taken right after connecting and kept until close(): http.client may close its own
socket at any time, and a descriptor number closed and reused meanwhile would let a late cancel shut down an unrelated
connection (ruling P3). SSLSocket.shutdown() is never called from another thread (spec §3.4.1): shutting down the TCP
duplicate ends a blocked TLS read within a millisecond (probe A10 held, so ruling P41's fallback is not used).

A connect polls for a cancel every 50 ms (name resolution stays blocking), so a node that does not answer never holds
a cancel for CONNECT_TIMEOUT_S (ruling R-04a-T7a).

The server sends no status line until its first output is ready (a long aggregate: minutes), so `NodeClient.send()` can
block that long; a cancel during that wait shuts the channel down like any other (the caller gets the channel from
`attach` before anything is sent, and arms its own timer before calling `send`). A caller that cannot be cancelled by
anyone (the internal metadata queries) passes `read_timeout`: no bytes for that long ends the request (R-04a-F3).

A paused result reads ahead without waiting (result.py): `QueryResponse.ready()` says whether a read would wait, and a
chunked body is decoded here (`ChunkedBody`), never by http.client, whose chunked reading waits for the next chunk's
size line (ruling B33)."""

import base64
import errno
import http.client
import io
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
from enum import StrEnum
from typing import Any

from loguru import logger

from dbaguy_backend.adapters.sockets import enable_keepalive
from dbaguy_backend.sql.jsontext import NotJson, value_end

READ_CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 10.0  # connect and TLS handshake; reads afterwards block with no timeout (a cancel ends them),
# unless the request asks for one (`NodeClient.send(read_timeout=...)`, the internal queries)
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
    timeout: str | None = None  # a server duration such as "30s"; only the internal metadata queries set one (R-04a-F3)


_JSON_SPACE = re.compile(r"[ \t\n\r]*")


class LiteralNotJson(ValueError):
    """A parameter literal that is not exactly one JSON value (ruling C39): embedded as it is, it would end its value
    early and add fields of its own to the body (`1, "readonly": false`). `label`: `$name` or `$n`, never the value."""

    def __init__(self, label: str) -> None:
        super().__init__(f"Parameter {label} is not one JSON value")
        self.label = label


def _one_value(label: str, literal: str) -> str:
    """`literal` when it is one JSON value with only JSON whitespace around it, else LiteralNotJson. Checked by the C
    decoder, which builds no tree (`jsontext.value_end`): a 20 MiB document in about 0.1 s."""
    start = _JSON_SPACE.match(literal).end()
    try:
        end = value_end(literal, start)
    except (json.JSONDecodeError, NotJson, RecursionError):
        raise LiteralNotJson(label) from None
    if _JSON_SPACE.match(literal, end).end() != len(literal):
        raise LiteralNotJson(label)
    return literal


def build_body(request: QueryRequest) -> bytes:
    """The body as text, so parameter literals go out exactly as given (a number never passes through float; R-04-10).
    `timeout` only when the request has one: a user's statement never does (it keeps counting while a result is paused,
    R-04-5), and `scan_consistency` is never sent. A literal that is not one JSON value raises LiteralNotJson (C39), a
    lone surrogate in any text UnicodeEncodeError: nothing is sent."""
    parts = [f'"statement":{json.dumps(request.statement, ensure_ascii=False)}',
             f'"client_context_id":{json.dumps(request.client_context_id)}']
    if request.query_context is not None:
        parts.append(f'"query_context":{json.dumps(request.query_context, ensure_ascii=False)}')
    if request.readonly:
        parts.append('"readonly":true')
    parts.extend(f"{json.dumps('$' + name, ensure_ascii=False)}:{_one_value('$' + name[:40], literal)}"
                 for name, literal in request.named.items())
    if request.positional:
        parts.append('"args":[' + ",".join(_one_value(f"${index}", literal)
                                           for index, literal in enumerate(request.positional, start=1)) + "]")
    if request.profile is not None:
        parts.append(f'"profile":{json.dumps(request.profile)}')
    if request.preserve_expiry:
        parts.append('"preserve_expiry":true')
    if request.timeout is not None:
        parts.append(f'"timeout":{json.dumps(request.timeout)}')
    return ("{" + ",".join(parts) + "}").encode("utf-8")


class TlsFailure(StrEnum):
    """What a failed TLS setup says about the cause, for the hint a message adds (R-04a-F1)."""

    HOSTNAME = "hostname"  # the certificate is valid but does not name the host the client asked for
    ISSUER = "issuer"  # its issuer is unknown: no CA file, or not the cluster's
    NOT_TLS = "not_tls"  # the port answered something that is not TLS (a plain-HTTP port: 8093)
    OTHER = "other"  # anything else: the OpenSSL text alone


# OpenSSL verify codes (`SSLCertVerificationError.verify_code`): 62 is a host name that the certificate does not hold,
# 64 the same for an IP address (Python verifies an address, as the dev cluster's `127.0.0.1`, as an IP); 18 and 19 a
# self-signed certificate, 20 and 21 an issuer the client has no certificate for.
_HOSTNAME_MISMATCH = frozenset({62, 64})
_UNKNOWN_ISSUER = frozenset({18, 19, 20, 21})
_WRONG_VERSION = "WRONG_VERSION_NUMBER"  # also what a TLS hello answered by a plain HTTP error page looks like


def tls_failure(exc: ssl.SSLError) -> TlsFailure:
    """The cause of a TLS setup failure, from the exception's own fields (never from its text)."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        code = getattr(exc, "verify_code", None)  # always set by OpenSSL; a hand-made exception may lack it
        if code in _HOSTNAME_MISMATCH:
            return TlsFailure.HOSTNAME
        return TlsFailure.ISSUER if code in _UNKNOWN_ISSUER else TlsFailure.OTHER
    return TlsFailure.NOT_TLS if getattr(exc, "reason", None) == _WRONG_VERSION else TlsFailure.OTHER


class ChannelError(Exception):
    """The connection failed. `before_response`: no byte of the answer had arrived (what a kept-alive connection the
    server closed meanwhile looks like); `tls_failure`: setting up TLS failed (handshake, certificate), and why."""

    def __init__(self, message: str, *, before_response: bool = False, tls_failure: TlsFailure | None = None) -> None:
        super().__init__(message)
        self.before_response, self.tls_failure = before_response, tls_failure

    @property
    def tls(self) -> bool:
        return self.tls_failure is not None


# What a kept-alive connection the server has closed meanwhile raises before any byte of the answer (RemoteDisconnected
# is a ConnectionResetError; SSLEOFError is a TLS peer that closed without close_notify).
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
          ssl.SSLEOFError)
_FAILURES = (http.client.HTTPException, OSError, ValueError)  # ssl.SSLError is an OSError


# http.client errors whose text repeats what the peer sent (the status line, the protocol version) or the host: the
# exact types only, because RemoteDisconnected is also a BadStatusLine and its text is fixed.
_PEER_TEXT_ERRORS: dict[type[BaseException], str] = {
    http.client.BadStatusLine: "the server's answer is not valid HTTP",
    http.client.UnknownProtocol: "the server speaks an unknown HTTP version",
    http.client.InvalidURL: "invalid host name",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_REASON_LIMIT = 200


def failure_reason(exc: BaseException) -> str:
    """A short reason a connection failed, for messages and logs (never the request or credentials). One line of at most
    _REASON_LIMIT characters, and never text the peer chose: a hostile node's status line must not reach the log."""
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "unknown host"
    if fixed := _PEER_TEXT_ERRORS.get(type(exc)):
        return fixed
    return _CONTROL_CHARS.sub(" ", str(exc)).strip()[:_REASON_LIMIT] or type(exc).__name__


def _endpoint_label(host: str, port: int) -> str:
    """`host:port`, an IPv6 address in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
_CHUNK_LINE_LIMIT = 4096  # a size line (with extensions) or a trailer line longer than this is not the query service


class ChunkedBody:
    """The chunked transfer coding (RFC 9112 §7.1) decoded from whatever bytes have arrived. A size line or a CRLF cut
    between two reads waits here for the rest, so a read never asks the socket for more than it holds (http.client's
    own decoding reads a size line to its end, which blocks when the server has sent a chunk and not the next one).
    ValueError, with a fixed text (never the peer's bytes), when the framing is broken. Bytes after the end are not the
    query service either, but the body before them is whole: they are dropped and `extra` says so."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._left = 0  # data bytes still to come in the current chunk
        self._state = "size"  # size | data | crlf | trailer | done
        self.done = False  # the last chunk and the trailer have been read
        self.extra = False  # bytes came after the end (dropped): the connection must not carry another request

    def feed(self, data: bytes) -> bytes:
        """The body bytes `data` completes (possibly none: framing only)."""
        self._buf += data
        out = bytearray()
        while True:
            if self._state == "data":
                take = min(self._left, len(self._buf))
                out += self._buf[:take]
                del self._buf[:take]
                self._left -= take
                if self._left:
                    return bytes(out)
                self._state = "crlf"
            elif self._state == "crlf":
                if len(self._buf) < 2:
                    return bytes(out)
                if self._buf[:2] != b"\r\n":
                    raise ValueError("a chunk is not followed by CRLF")
                del self._buf[:2]
                self._state = "size"
            elif self._state in ("size", "trailer"):
                end = self._buf.find(b"\n")
                if end < 0:
                    if len(self._buf) > _CHUNK_LINE_LIMIT:
                        raise ValueError("a chunk line is too long")
                    return bytes(out)
                line = bytes(self._buf[:end]).rstrip(b"\r")
                del self._buf[:end + 1]
                if self._state == "trailer":
                    if not line:
                        self._state, self.done = "done", True
                    continue
                size = line.split(b";", 1)[0].strip()
                if not _CHUNK_SIZE.fullmatch(size):
                    raise ValueError("a chunk size is not a hexadecimal number")
                self._left = int(size, 16)
                self._state = "data" if self._left else "trailer"
            else:  # done
                if self._buf:
                    self.extra = True
                    self._buf.clear()
                return bytes(out)


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
        # The socket open_socket() returned (worker thread only): http.client drops `conn.sock` after a `Connection:
        # close` answer while the response still reads from it, and readable() must look at that one
        self._sock: socket.socket | None = None
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
            self._sock = sock
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

    def readable(self) -> bool:
        """Worker thread: whether a read would not wait. True when bytes are on the socket, decrypted TLS bytes are
        pending, or the socket is gone or failed (the read then fails at once). With TLS, bytes on the socket may be
        part of a record whose rest is still in flight: a read then waits for that rest only."""
        sock = self._sock
        if sock is None:
            return True
        try:
            if isinstance(sock, ssl.SSLSocket) and sock.pending():
                return True
            with selectors.DefaultSelector() as selector:
                selector.register(sock, selectors.EVENT_READ)
                return bool(selector.select(0))
        except (OSError, ValueError):  # closed (fileno -1) or failed
            return True

    def close(self) -> None:
        """Worker thread."""
        self.dead = True
        self._sock = None
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
    carry the next request). A chunked body (every big answer) is decoded by ChunkedBody over the socket's reader; a
    Content-Length body by http.client's read1()."""

    def __init__(self, channel: HttpChannel, response: http.client.HTTPResponse, clock: Callable[[], float]) -> None:
        self._channel, self._response, self._clock = channel, response, clock
        self.status, self.reason = response.status, response.reason
        self.content_type = response.getheader("Content-Type") or ""
        self.done = False
        self._failed: ChannelError | None = None  # raised by every later read: a failed body never ends cleanly
        self._chunked = ChunkedBody() if response.chunked else None
        self._unread = True  # no read yet: http.client's reader may hold body bytes it read with the headers

    def ready(self) -> bool:
        """Worker thread: whether a read would not wait. Exact after the first read, which (as every read) empties
        http.client's buffered reader. A Content-Length body http.client has read to its end returns b"" at once."""
        return (self.done or self._failed is not None or self._unread
                or (self._chunked is None and self._response.isclosed()) or self._channel.readable())

    def read_chunk(self, wait: bool = True) -> bytes:
        """The next bytes of the body, b"" at its end (`done`). ChannelError when the connection fails. `wait=False`
        (a read-ahead, called only when `ready()`): one read of what is there, which may bring chunk framing only and
        then returns b"" with `done` still False."""
        if self._failed is not None:
            raise self._failed
        while not self.done:
            data = self._read_once()
            if data or not wait:
                return data
        return b""

    def _read_once(self) -> bytes:
        try:
            size = max(READ_CHUNK, io.DEFAULT_BUFFER_SIZE)  # at least the reader's buffer: each read empties it
            if self._chunked is None:
                data = self._response.read1(size)
            else:
                raw = self._response.fp.read1(size)
                if not raw:
                    raise http.client.IncompleteRead(b"")
                data = self._chunked.feed(raw)
        except _FAILURES as exc:  # ChunkedBody's ValueError too (a fixed text)
            error = ChannelError(failure_reason(exc))
            self._stop(error)
            raise error from exc
        self._unread = False
        if self._chunked is None and not data and self._response.length:
            # a Content-Length body that ended early: http.client's read1() does not raise
            error = ChannelError(failure_reason(http.client.IncompleteRead(b"", self._response.length)))
            self._stop(error)
            raise error
        if self._chunked.done if self._chunked is not None else not data:
            self.done = True
            if self._chunked is not None:
                self._response.close()  # as http.client does after the last chunk: the connection takes a new request
            if self._chunked is not None and self._chunked.extra:
                # the answer is whole, but what came after it would be read as the start of the next answer
                logger.warning("The query node {} sent bytes after the end of an answer; its connection is not reused",
                               _endpoint_label(self._channel.host, self._channel.port))
                self._channel.close()
            elif self._response.will_close:
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

    def send(self, body: bytes, *, retry_safe: bool, attach: Callable[[HttpChannel], None],
             read_timeout: float | None = None) -> QueryResponse:
        """POSTs `body` and waits for the status line. `attach(channel)` is called before anything is sent on a channel
        (the caller records what a cancel shuts down); whatever it raises propagates and nothing is sent. `read_timeout`:
        seconds without a byte, while waiting for the status line or reading the body, before the request fails with a
        ChannelError (None: wait as long as it takes, a cancel ends it). It is a socket timeout, so every byte restarts
        it: it ends a stall, not a slow answer."""
        channel = self._channel_for_request()
        reused = channel.used
        try:
            return self._post(channel, body, attach, read_timeout)
        except ChannelError as exc:
            if not (reused and retry_safe and exc.before_response) or channel.shut_down:
                raise  # a write, a fresh connection, a failure mid-answer, or a cancel: never resent
            logger.info("The server closed the kept-alive connection to {}; sending the read again", self.label)
            return self._post(self._channel_for_request(), body, attach, read_timeout)

    def answered_within(self, seconds: float) -> bool:
        """The next request would go out on a kept-alive channel whose last answer was read to its end at most `seconds`
        ago, so the server is unlikely to have closed it meanwhile (ruling C40). False when it would open a new one."""
        channel, now = self._channel, self._clock()
        if channel is None or channel.idle_since is None or not channel.reusable(now):
            return False
        return now - channel.idle_since <= seconds

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

    def _post(self, channel: HttpChannel, body: bytes, attach: Callable[[HttpChannel], None],
              read_timeout: float | None) -> QueryResponse:
        attach(channel)  # raises when a cancel came first: nothing is sent
        reused = channel.used
        channel.used, channel.idle_since = True, None
        try:
            # Set for every request, before the body goes out on a kept-alive channel (it may carry an internal
            # query's deadline into a user's statement) and again once request() has connected a new one
            if channel.conn.sock is not None:
                channel.conn.sock.settimeout(read_timeout)
            channel.conn.request("POST", QUERY_PATH, body=body, headers=self._headers)
            if channel.conn.sock is not None:
                channel.conn.sock.settimeout(read_timeout)
            response = channel.conn.getresponse()
        except ChannelError:  # cancelled while connecting
            channel.close()
            raise
        except _FAILURES as exc:
            channel.close()
            # TLS failed only while setting it up, and not because a cancel cut the handshake: an error on a connection
            # that has answered before is a lost connection
            failure = tls_failure(exc) if isinstance(exc, ssl.SSLError) and not reused and not channel.shut_down else None
            raise ChannelError(failure_reason(exc), before_response=isinstance(exc, _STALE),
                               tls_failure=failure) from exc
        return QueryResponse(channel, response, self._clock)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
