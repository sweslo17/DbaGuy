import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from loguru import logger


@contextmanager
def atomic_text_file(target: Path) -> Iterator[TextIO]:
    """Write `target` through `<target>.part`: the finished file replaces any old one in one step, or nothing changes.

    Any error (including one raised by the caller's block) removes the partial file and leaves `target` as it was.
    Opening the part is the only step that creates it, so a failure before that never deletes a file this call did
    not write.
    """
    part = target.parent / f"{target.name}.part"
    fp = part.open("w", encoding="utf-8", newline="")
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
