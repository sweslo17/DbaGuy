import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from loguru import logger

_NAME_MAX_BYTES = 255  # the longest file name Linux and macOS file systems accept


def _part_path(target: Path) -> Path:
    """`.<name>.<random>.part` next to `target`, with `<name>` cut short so that a target name that is itself valid
    never makes the temporary name too long."""
    suffix = f".{secrets.token_hex(6)}.part"
    room = _NAME_MAX_BYTES - len(".") - len(suffix)
    stem = os.fsencode(target.name)[:room].decode("utf-8", "ignore")
    return target.parent / f".{stem}{suffix}"


@contextmanager
def atomic_text_file(target: Path) -> Iterator[TextIO]:
    """Write `target` through a temporary file next to it: the finished file replaces any old one in one step, or
    nothing changes.

    The temporary file is `.<name>.<random>.part` in the same folder (so the final rename is atomic; `<name>` is
    cut short for names that are almost too long), created exclusively: two writers never share one, and a `.part`
    file this call did not create is never truncated or deleted. Any error (including one raised by the caller's
    block) removes this call's partial file and leaves `target` as it was. The file's bytes reach the disk before the
    rename (06a pre-flight L9): a system crash right after it never leaves an empty file in place of the old one.
    """
    part = _part_path(target)
    fp = part.open("x", encoding="utf-8", newline="")
    try:
        with fp:
            yield fp
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(part, target)
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove the partial file {}", part, exc_info=True)
        raise
