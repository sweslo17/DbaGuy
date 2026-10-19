"""Socket options shared by adapters that own their TCP socket (MySQL's driver socket, Couchbase's HTTP connection)."""

import socket

from loguru import logger

KEEPALIVE_IDLE_S, KEEPALIVE_INTERVAL_S, KEEPALIVE_PROBES = 30, 10, 3


def enable_keepalive(sock: socket.socket) -> None:
    """TCP keepalive (30 s idle, 10 s interval, 3 probes), as PostgreSQL's connection uses: an idle tunnel or NAT does
    not drop the connection unnoticed. Best effort."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPALIVE", KEEPALIVE_IDLE_S), ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_S),
                            ("TCP_KEEPCNT", KEEPALIVE_PROBES)):
            if (option := getattr(socket, name, None)) is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as exc:
        logger.debug("TCP keepalive not set: {}", exc)
