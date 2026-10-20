"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts. No job runs here, and Run SQL file's
inspection opens no connection at all (a read-only profile is refused first, R-06-6).

Logs: INFO the profile and the path of each inspection; never the file's contents."""

import re
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import ENGINE_NAMES, RUN_SQL_FILES, DumpHeader, FileInspection, cannot, cannot_read
from ..domain.errors import FileNotFound, NotSupported, ReadOnlyProfile, StorageError
from ..domain.profiles import AnyProfile, Engine
from ..sql.dump_header import CUT_SHORT, END_BYTES, cut_short, parse_header
from ..sql.statement_stream import UTF16_MESSAGE, starts_with_utf16_bom
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
_COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
_HEX_BLOB = "mysqldump: use --hex-blob"


def _decoded(head: bytes) -> tuple[str, int | None]:
    """The head as text, and the 1-based line of its first byte that is not UTF-8 (None: it all is). A character cut
    at the end of the head is not an error."""
    try:
        return head.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 3 and exc.reason == "unexpected end of data":
            return head[:exc.start].decode("utf-8", "replace"), None
        return head.decode("utf-8", "replace"), head.count(b"\n", 0, exc.start) + 1


class DataService:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._profiles = profiles
        self._registry = registry

    def inspect(self, profile_id: UUID, path: str) -> FileInspection:
        """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
        the run will stop at or should be told (another engine's dump, objects dropped first, not UTF-8 or UTF-16,
        COPY, a dump cut short)."""
        profile, _ = self._admit(profile_id, RUN_SQL_FILES, "run_file")
        if profile.read_only:
            raise ReadOnlyProfile()
        target = Path(path).expanduser()
        if not target.is_absolute() or not target.is_file():
            raise FileNotFound(path)
        try:
            with target.open("rb") as file:
                size = target.stat().st_size
                head = file.read(PREVIEW_BYTES)
                if size > len(head):
                    file.seek(max(size - END_BYTES, 0))
                    end = file.read(END_BYTES)
                else:
                    end = head[-END_BYTES:]
        except OSError as exc:
            raise StorageError(cannot_read(path, exc)) from None
        utf16 = starts_with_utf16_bom(head)
        text, bad_line = (head.decode("utf-16", "replace"), None) if utf16 else _decoded(head)
        lines = text.splitlines(keepends=True)
        preview = "".join(lines[:PREVIEW_LINES])
        truncated = len(lines) > PREVIEW_LINES or size > len(head)
        header = None if utf16 else parse_header(text)
        warnings = self._warnings(profile.engine, size, header, preview, bad_line)
        if utf16:
            warnings.append(UTF16_MESSAGE)  # pre-flight L5: the run stops at its first line
        elif cut_short(text, end.decode("utf-8", "replace")):
            warnings.append(CUT_SHORT)  # pre-flight M15
        logger.info("Inspected {} ({:,} bytes) for a run on {}", target, size, profile.name)
        return FileInspection(path=str(target), size=size, header=header, preview=preview.lstrip("﻿"),
                              preview_truncated=truncated, warnings=warnings)

    @staticmethod
    def _warnings(engine: Engine, size: int, header: DumpHeader | None, preview: str, bad_line: int | None) -> list[str]:
        warnings = []
        if size == 0:
            warnings.append("The file is empty")
        if header is not None and header.engine != engine:
            warnings.append(f"Written for {ENGINE_NAMES[header.engine]}, the target is {ENGINE_NAMES[Engine(engine)]}")
        if header is not None and header.drop_first:
            warnings.append("This dump drops and recreates its objects")
        if bad_line is not None:
            hint = f" ({_HEX_BLOB})" if engine == Engine.MYSQL else ""
            warnings.append(f"Line {bad_line:,} is not UTF-8 text: a run stops there{hint}")
        if engine == Engine.POSTGRES:
            for number, line in enumerate(preview.splitlines(), start=1):
                if _COPY_FROM_STDIN.match(line):
                    warnings.append(f"Line {number:,} copies data from stdin, which only psql can do: a run stops "
                                    "there (pg_dump: use --inserts)")
                    break
        return warnings

    def _admit(self, profile_id: UUID, action: str, capability: str) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not getattr(adapter.capabilities, capability):
            raise NotSupported(cannot(profile.engine, action))
        return profile, adapter
