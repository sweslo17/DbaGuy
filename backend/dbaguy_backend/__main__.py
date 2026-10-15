import json
import os
import socket
import sys
import threading

import uvicorn
from loguru import logger

from .app import create_app
from .config import Settings
from .logging_setup import setup_logging


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _watch_stdin(server: uvicorn.Server) -> None:
    """Exit when the parent process closes our stdin (parent died or quit)."""

    def run() -> None:
        try:
            for _ in sys.stdin:
                pass
        except (OSError, ValueError):
            pass
        logger.info("Parent closed stdin; shutting down")
        server.should_exit = True
        killer = threading.Timer(5.0, lambda: os._exit(0))
        killer.daemon = True
        killer.start()

    threading.Thread(target=run, name="stdin-watchdog", daemon=True).start()


def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.log_dir if settings.log_to_file else None)
    app = create_app(settings)
    sock = _bind(settings.host, settings.port)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    if settings.watch_stdin:
        _watch_stdin(server)
    logger.info("Listening on {}:{}", settings.host, port)
    sys.stdout.write(json.dumps({"event": "ready", "port": port}) + "\n")
    sys.stdout.flush()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
