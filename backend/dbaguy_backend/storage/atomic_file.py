import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from loguru import logger


@contextmanager
def atomic_text_file(target: Path) -> Iterator[TextIO]:
    """Write `target` through a temporary file next to it: the finished file replaces any old one in one step, or
    nothing changes.

    The temporary file is `.<name>.<random>.part` in the same folder (so the final rename is atomic), created
    exclusively: two writers never share one, and a `.part` file this call did not create is never truncated or
    deleted. Any error (including one raised by the caller's block) removes this call's partial file and leaves
    `target` as it was.
    """
    part = target.parent / f".{target.name}.{secrets.token_hex(6)}.part"
    fp = part.open("x", encoding="utf-8", newline="")
    try:
        with fp:
            yield fp
        os.replace(part, target)
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove the partial file {}", part, exc_info=True)
        raise
