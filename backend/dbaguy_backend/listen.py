"""The listening sockets: the API's (a random port) and the MCP server's (a fixed one, spec ⑧ R-08-2)."""

import errno
import os
import socket

# Where another program can listen on the MCP port while a SO_REUSEADDR bind of 127.0.0.1 still succeeds (ruling M1):
# macOS then sends 127.0.0.1's traffic to DbaGuy (0.0.0.0, dual-stack [::]), and `localhost`'s to the other program
# ([::], [::1]).
_ELSEWHERE = ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::"), (socket.AF_INET6, "::1"))


def listen_socket(host: str, port: int, *, alone: bool = False) -> socket.socket:
    """A socket bound to (host, port) and listening; OSError (EADDRINUSE: another program holds the port). `alone`
    (the MCP port): also EADDRINUSE when another program listens on the port at 0.0.0.0, [::] or [::1]."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        if alone and held_elsewhere(port):
            raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def held_elsewhere(port: int) -> bool:
    """Whether another program listens on `port` at a wildcard or IPv6 loopback address: a probe bind there (with
    SO_REUSEADDR, closed at once) fails with EADDRINUSE. SO_REUSEADDR keeps DbaGuy's own TIME_WAIT connections from
    counting; a probe without it would fail for 30 s after every restart. Our own 127.0.0.1 listener never counts."""
    for family, address in _ELSEWHERE:
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:  # no IPv6: nothing can listen there
            continue
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((address, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
        finally:
            probe.close()
    return False
