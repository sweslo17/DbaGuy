"""Stand-ins for the Couchbase query service: a local HTTP/1.1 server with scripted replies, and a canned response."""

import select
import socket
import ssl
import threading
from dataclasses import dataclass


@dataclass
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "application/json"
    close: bool = False  # say `Connection: close` and close afterwards
    drop_after: bool = False  # close the connection after this reply without saying so (an idle keep-alive timeout)
    hold: threading.Event | None = None  # send the first half, then wait until set (a long query)
    before_head: threading.Event | None = None  # send nothing until set (a long aggregate: no status line yet)
    chunked: bool = True  # False: a `Content-Length` body (what the server sends for a small answer)
    cut: bool = False  # close the connection after the first half (a node that died mid-answer)


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n" if data else b""


class FakeQueryServer:
    """Serves each request with the next Reply (chunked unless told otherwise), kept alive; counts connections and
    records request bodies."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.bodies: list[bytes] = []
        self.connections = 0
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        # The reader closes with the connection: a file object left open would keep the descriptor (and the client's
        # wait) alive after the handler ends.
        with conn, conn.makefile("rb") as reader:
            try:  # the whole loop (P45): a client that abandons a response (reset, broken pipe) leaves no thread warning
                while reader.readline():  # the request line; empty when the client left
                    headers = {}
                    while (line := reader.readline()) not in (b"\r\n", b"\n", b""):
                        name, _, value = line.decode().partition(":")
                        headers[name.strip().lower()] = value.strip()
                    self.bodies.append(reader.read(int(headers.get("content-length", "0"))))
                    if not self.replies:
                        return  # the script ran out: the connection closes, and the client fails at once
                    reply = self.replies.pop(0)
                    if reply.before_head is not None:
                        reply.before_head.wait(10)
                    framing = "Transfer-Encoding: chunked" if reply.chunked else f"Content-Length: {len(reply.body)}"
                    head = (f"HTTP/1.1 {reply.status} Reason\r\nContent-Type: {reply.content_type}\r\n"
                            f"{framing}\r\n{'Connection: close' + chr(13) + chr(10) if reply.close else ''}\r\n")
                    frame = _chunk if reply.chunked else bytes
                    half = len(reply.body) // 2
                    conn.sendall(head.encode() + frame(reply.body[:half]))
                    if reply.cut:
                        return
                    if reply.hold is not None:
                        reply.hold.wait(10)
                    conn.sendall(frame(reply.body[half:]) + (b"0\r\n\r\n" if reply.chunked else b""))
                    if reply.close or reply.drop_after:
                        return
            except OSError:
                return

    def close(self) -> None:
        self._server.close()


class PlainHttpPort:
    """A port that speaks plain HTTP only, like the query service's 8093: whatever arrives (a TLS hello, say) is answered
    with `400 Bad Request` and the connection closes. A TLS client reads that answer as a TLS record and fails with
    OpenSSL's WRONG_VERSION_NUMBER, as it does against the real port."""

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def close(self) -> None:
        self._server.close()


def cert_error(code: int) -> ssl.SSLCertVerificationError:
    """A certificate verification failure with OpenSSL's verify code (62: host name, 20: unknown issuer, …)."""
    error = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed ({code})")
    error.verify_code = code
    return error


def ssl_error(reason: str | None) -> ssl.SSLError:
    error = ssl.SSLError(1, f"[SSL: {reason}] {reason}")
    if reason is not None:
        error.reason = reason
    return error


class FailingContext:
    """Stands in for an ssl.SSLContext whose handshake fails with `error` (the TCP connect itself works)."""

    def __init__(self, error: ssl.SSLError) -> None:
        self.error = error

    def wrap_socket(self, sock, **kwargs):
        raise self.error


class FullListenQueue:
    """A local port whose listen queue is full, so a new connect to it waits (the kernel drops its SYN) until it times
    out: a host that does not answer, without the network. On macOS `listen(1)` plus a connection nobody accepts fills
    it; the fill goes on until a connect stays pending."""

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._fillers: list[socket.socket] = []
        for _ in range(16):
            filler = socket.socket()
            filler.setblocking(False)
            filler.connect_ex(("127.0.0.1", self.port))
            self._fillers.append(filler)
            if not select.select([], [filler], [], 0.2)[1]:
                return  # this connect waits: so will the next one
        self.close()
        raise RuntimeError("the listen queue did not fill up")

    def close(self) -> None:
        for filler in self._fillers:
            filler.close()
        self._server.close()


class FakeResponse:
    """A QueryResponse made of canned chunks (result tests without a server)."""

    def __init__(self, chunks: list[bytes], status: int = 200, content_type: str = "application/json") -> None:
        self._chunks = list(chunks)
        self.status, self.reason, self.content_type = status, "Reason", content_type
        self.done = False
        self.abandoned = False
        self.fail_with: Exception | None = None

    def read_chunk(self) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        if not self._chunks:
            self.done = True
            return b""
        return self._chunks.pop(0)

    def abandon(self) -> None:
        self.abandoned = not self.done
