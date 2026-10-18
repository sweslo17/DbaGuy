"""Stand-ins for the Couchbase query service: a local HTTP/1.1 server with scripted replies, and a canned response."""

import socket
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
        with conn:
            try:  # the whole loop (P45): a client that abandons a response (reset, broken pipe) leaves no thread warning
                reader = conn.makefile("rb")
                while reader.readline():  # the request line; empty when the client left
                    headers = {}
                    while (line := reader.readline()) not in (b"\r\n", b"\n", b""):
                        name, _, value = line.decode().partition(":")
                        headers[name.strip().lower()] = value.strip()
                    self.bodies.append(reader.read(int(headers.get("content-length", "0"))))
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
