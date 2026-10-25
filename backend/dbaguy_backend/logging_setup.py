import logging
import sys
from pathlib import Path

from loguru import logger

_FORMAT = "{time:HH:mm:ss.SSS} | {level: <7} | {name}:{line} | {message}"


class InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn, fastapi) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level: str, log_dir: Path | None) -> None:
    """stderr plus, with `log_dir`, a rotating backend.log, both at `level`.

    The file only gets SQL text (DEBUG) when DEBUG is asked for explicitly (dev.sh does). No `enqueue`: loguru's
    file sink is thread-safe on its own, and enqueue would start a multiprocessing resource tracker process.
    """
    # diagnose=False: loguru's default prints frame locals in tracebacks, which can include passwords.
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FORMAT, backtrace=False, diagnose=False)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "backend.log",
            level=level.upper(),
            format=_FORMAT,
            rotation="10 MB",
            retention=5,
            backtrace=False,
            diagnose=False,
        )
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False
    # mysql-connector logs its authentication plugin choice at INFO, three lines per connect.
    logging.getLogger("mysql.connector").setLevel(logging.WARNING)
    # The MCP SDK (spec ⑧ D-08-9) logs what clients send (session ids, a tool's failure text) at INFO.
    logging.getLogger("mcp").setLevel(logging.WARNING)
