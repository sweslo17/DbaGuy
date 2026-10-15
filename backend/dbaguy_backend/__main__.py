import json
import os
import socket
import sys
import threading
from collections.abc import Callable

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


# How long a graceful shutdown may take before the process exits anyway. The shell waits longer than this before it
# kills the backend (src-tauri/src/backend.rs), so the forced exit below still gets to close the tunnels.
_SHUTDOWN_DEADLINE_S = 5.0


def _force_exit(close_tunnels: Callable[[], None]) -> None:
    """Last resort when a graceful shutdown hangs: stop the SSH tunnels, then exit without further cleanup."""
    logger.warning("Shutdown took longer than {}s; exiting now", _SHUTDOWN_DEADLINE_S)
    try:
        close_tunnels()
    except Exception:
        logger.exception("Closing SSH tunnels before the forced exit failed")
    os._exit(0)


def _watch_stdin(server: uvicorn.Server, close_tunnels: Callable[[], None]) -> None:
    """Exit when the parent process closes our stdin (parent died or quit)."""

    def run() -> None:
        try:
            for _ in sys.stdin:
                pass
        except (OSError, ValueError):
            pass
        logger.info("Parent closed stdin; shutting down")
        server.should_exit = True
        killer = threading.Timer(_SHUTDOWN_DEADLINE_S, _force_exit, args=(close_tunnels,))
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
        _watch_stdin(server, app.state.container.tunnels.close_all)
    logger.info("Listening on {}:{}", settings.host, port)
    sys.stdout.write(json.dumps({"event": "ready", "port": port}) + "\n")
    sys.stdout.flush()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
