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
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "backend.log",
            level="DEBUG",
            format=_FORMAT,
            rotation="10 MB",
            retention=5,
            enqueue=True,
        )
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False
