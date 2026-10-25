"""The listening sockets: the API's (a random port) and the MCP server's (a fixed one, spec ⑧ R-08-2)."""

import socket


def listen_socket(host: str, port: int) -> socket.socket:
    """A socket bound to (host, port) and listening; OSError (EADDRINUSE: another program holds the port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock
