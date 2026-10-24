"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts, and Show in Finder for what a dump
wrote. No job runs here. Run SQL file's inspection opens no connection at all (a read-only profile is refused first,
R-06-6), nor does the excerpt of an error's lines (no profile: it shows a file a run already read, 06b B2); a dump's
plan reads the catalog on the metadata connection, without definitions (a read-only profile may dump).

Logs: INFO the profile and the path of each inspection, plan and reveal, the path and line of an excerpt; never the
file's contents."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from loguru import logger

from ..adapters.base import (
    CONNECTION_LOST, INVALID_TEXT, DatabaseAdapter, DatabaseConnection, Dumping, InvalidText, StatementError,
)
from ..adapters.dump import DumpCatalog
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import (
    BE_DUMPED, ENGINE_NAMES, RUN_SQL_FILES, DumpContent, DumpHeader, DumpObjectKinds, DumpPlan, DumpPlanObject,
    DumpScope, FileExcerpt, FileInspection, cannot, cannot_read,
)
from ..domain.errors import FileNotFound, NotFound, NotSupported, QueryFailed, ReadOnlyProfile, StorageError
from ..domain.profiles import AnyProfile, Engine, SqliteProfile
from ..sessions.jobs.dump import data_order, target_problem
from ..sessions.jobs.registry import JobRegistry
from ..sql.dump_header import CUT_SHORT, END_BYTES, cut_short, filtered_rows, parse_header
from ..sql.dump_script import not_included, restore_warnings
from ..sql.statement_stream import UTF16_MESSAGE, starts_with_utf16_bom
from .metadata_service import MetadataService
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
EXCERPT_BEFORE = 20  # lines shown before the one asked for (06b B2)
EXCERPT_LINES = 60
EXCERPT_BYTES = 64 * 1024
_READ_CHUNK = 1 << 20
_UTF8_BOM = b"\xef\xbb\xbf"
LOW_DISK_BYTES = 1 << 30  # CR-4: below this much free space a dump's plan warns, whatever its estimate
_GIB = 1 << 30
_COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
_HEX_BLOB = "mysqldump: use --hex-blob"
_PLAN_NOT_UTF8 = ("A name or definition in this database holds text that is not valid UTF-8: a dump cannot write "
                  "it to a UTF-8 file")  # Task 9 fix L2: never the driver's text, which quotes it
# The parts of another object, which a dump plan's list leaves to it (DumpPlan)
_PARTS = frozenset({"index", "foreign key", "check constraint", "default", "column default", "table settings",
                    "trigger state", "sequence owner", "sequence value", "identity value", "row-level security",
                    "setting"})


def _final(message: str, code: str | None) -> QueryFailed:
    """A plan's catalog read that the server refused (a lock it waited for too long, say): the answer, with the server's
    code, never a reconnect and a second wait (the metadata connection resends only on a lost connection)."""
    error = QueryFailed(message)
    if code:
        error.code = code
    return error


def _open_in_finder(path: Path) -> None:
    subprocess.run(["/usr/bin/open", "-R", str(path)], check=False, timeout=10)


def _whole_characters(data: bytes) -> bytes:
    """`data` without a UTF-8 character that its end cuts in two (bytes that are not UTF-8 are left as they are)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:  # the last byte that is not a continuation: ASCII, or a character's first byte
            need = 2 if 0xC2 <= byte <= 0xDF else 3 if 0xE0 <= byte <= 0xEF else 4 if 0xF0 <= byte <= 0xF4 else 1
            return data[:-back] if need > back else data
    return data


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
    def __init__(
        self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService, jobs: JobRegistry,
        reveal: Callable[[Path], None] = _open_in_finder,
    ) -> None:
        self._profiles = profiles
        self._registry = registry
        self._metadata = metadata
        self._jobs = jobs
        self._reveal = reveal

    def dump_plan(
        self, profile_id: UUID, database: str | None, scope: DumpScope, content: DumpContent, kinds: DumpObjectKinds,
        path: str | None, *, drop_first: bool = False, filter: str | None = None,
    ) -> DumpPlan:
        """What a dump of `scope` would write (the objects under their own names, DumpPlan), what its header says is
        left out, its consistency and its warnings: a filtered file that drops and recreates its table (fix wave F2),
        a `drop_first` file whose DROPs objects outside it block (06b I2), the catalog's, a data-only dump's
        foreign-key cycle, and for `path` a file the dump would refuse (M13) or little free disk where it goes (CR-4:
        never a refusal). The filter and `drop_first` are only looked at for those warnings: the dump checks the
        filter before anything is sent."""
        profile, _ = self._admit(profile_id, BE_DUMPED, "dump")

        def read(connection: DatabaseConnection) -> DumpCatalog:
            if not isinstance(connection, Dumping):
                raise NotSupported(cannot(profile.engine, BE_DUMPED))
            try:
                return connection.dump_catalog(scope, kinds, ddl=False)
            except InvalidText:
                raise _final(_PLAN_NOT_UTF8, INVALID_TEXT) from None
            except StatementError as exc:
                if exc.code == CONNECTION_LOST:
                    raise  # the metadata connection reconnects once
                raise _final(exc.message, exc.code) from exc  # the server's answer: never sent again

        catalog = self._metadata.with_connection(profile_id, database, read)
        estimates = {table.name: table.rows_estimate for table in catalog.tables}
        warnings = [*restore_warnings(catalog, content, drop_first, filter), *catalog.warnings]
        if content == "data":
            tables, said = data_order(self._registry.get(profile.engine).dialect, catalog.tables)
            objects = [DumpPlanObject(kind="table", name=table.name, rows_estimate=table.rows_estimate)
                       for table in tables]
            # what only a data-only dump writes (PostgreSQL's REFRESH MATERIALIZED VIEW, fix L3)
            objects += [DumpPlanObject(kind=item.kind, name=item.name) for item in catalog.post
                        if item.section == "data_only"]
            warnings += said
        else:
            rows = content == "both"  # a schema-only dump writes no rows
            named = [item for item in (*catalog.pre, *catalog.post)
                     if item.section == "schema" and item.kind not in _PARTS]
            objects = [DumpPlanObject(kind=item.kind, name=item.name,
                                      rows_estimate=estimates.get(item.name) if rows and item.kind == "table" else None)
                       for item in named]
        if path is not None:
            warnings += self._target_warnings(profile, catalog, content, path)
        logger.info("Dump plan for {}: {} object(s), {} table(s), {}", profile.name, len(objects), len(catalog.tables),
                    catalog.consistency)
        return DumpPlan(objects=objects, not_included=not_included(catalog), consistency=catalog.consistency,
                        warnings=warnings)

    @staticmethod
    def _target_warnings(profile: AnyProfile, catalog: DumpCatalog, content: DumpContent, path: str) -> list[str]:
        """A file the dump would refuse (pre-flight M13), and CR-4's free space: a warning when the destination's disk
        has less than LOW_DISK_BYTES free, or less than the dump may take (about twice the tables' size on disk: an
        INSERT's text is longer than the rows it holds; a SQLite file's size when the catalog has no sizes)."""
        source = Path(profile.path) if isinstance(profile, SqliteProfile) else None
        if (problem := target_problem(path, source)) is not None:
            return [problem]
        if content == "schema":
            return []
        folder = Path(path).expanduser().parent
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return []
        estimate = 2 * sum(table.bytes_estimate or 0 for table in catalog.tables)
        if not estimate and source is not None and source.expanduser().is_file():
            estimate = 2 * source.expanduser().stat().st_size
        if free >= max(estimate, LOW_DISK_BYTES):
            return []
        need = f" (the dump may need about {estimate / _GIB:.1f} GB)" if estimate > free else ""
        return [f"Only {free / _GIB:.1f} GB free where the dump is written{need}"]

    def reveal(self, path: str) -> None:
        """Show in Finder: only a file a completed dump of this backend run wrote, and only while it is there."""
        target = Path(path).expanduser()
        if not self._jobs.dumped(str(target)) or not target.is_file():
            raise NotFound(f"{path} is not a file a dump of this run wrote")
        logger.info("Showing {} in Finder", target)
        self._reveal(target)

    def excerpt(self, path: str, line: int) -> FileExcerpt:
        """The lines of the file at `path` around `line` (06b B2): from EXCERPT_BEFORE lines before it, or from the
        line itself when those lines fill more than half of EXCERPT_BYTES (long INSERT lines: the line asked for is
        always shown, with room, pre-flight M1 and review L2), at most EXCERPT_LINES lines and EXCERPT_BYTES. Lines end
        at LF as a run counts them; a CR before it is not shown (review L1). A UTF-16 file is read as inspect reads it.
        No connection: what Run SQL file's dialog shows of an error's place."""
        target = Path(path).expanduser()
        if not target.is_absolute() or not target.is_file():
            raise FileNotFound(path)
        first = max(1, line - EXCERPT_BEFORE)
        try:
            with target.open("rb") as file:
                if starts_with_utf16_bom(file.read(2)):
                    file.seek(0)
                    head = file.read(EXCERPT_BYTES + 1)
                    lines = head[:EXCERPT_BYTES].decode("utf-16", "replace").split("\n")[first - 1:]
                    found = self._window(target, lines, first, line, more=len(head) > EXCERPT_BYTES)
                    logger.info("Excerpt of {} (UTF-16) at line {:,}", target, line)
                    return found
                file.seek(0)
                body = self._from_line(file, first)
                # the window is full and the line asked for starts late in it (a file that ends inside it has fewer
                # lines: its tail is shown)
                if body is not None and len(body) > EXCERPT_BYTES and self._starts_late(body, line - first):
                    first = line  # the lines before it are long (INSERT lines): start at the line itself
                    file.seek(0)
                    body = self._from_line(file, first)
        except OSError as exc:
            raise StorageError(cannot_read(path, exc)) from None
        logger.info("Excerpt of {} at line {:,}", target, line)
        if body is None:  # the file has fewer lines
            return FileExcerpt(path=str(target), first_line=first, text="", found=False, truncated=False)
        more = len(body) > EXCERPT_BYTES
        body = body[:EXCERPT_BYTES]
        if first == 1 and body.startswith(_UTF8_BOM):
            body = body[len(_UTF8_BOM):]
        if more:
            body = _whole_characters(body)  # review L3: not a character cut in two at the window's end
        return self._window(target, body.decode("utf-8", "replace").split("\n"), first, line, more=more)

    @staticmethod
    def _window(target: Path, lines: list[str], first: int, line: int, *, more: bool) -> FileExcerpt:
        """The excerpt of `lines` (the file's text from line `first` on, split at LF; `more`: the file goes on after
        them). A CR right before an LF is not shown (CRLF); any other CR stays in its line, as the run reads it."""
        ended = len(lines) - 1  # the lines an LF ended
        if not more and lines and lines[-1] == "":
            lines.pop()  # after the file's last line end: no line
            ended = len(lines)
        shown, truncated = (lines[:EXCERPT_LINES], True) if len(lines) > EXCERPT_LINES else (lines, more)
        text = "\n".join(piece[:-1] if index < ended and piece.endswith("\r") else piece
                         for index, piece in enumerate(shown))
        return FileExcerpt(path=str(target), first_line=first, text=text, found=line - first < len(lines),
                           truncated=truncated)

    @staticmethod
    def _starts_late(body: bytes, ends: int) -> bool:
        """Whether the line after the first `ends` LFs of `body` starts past half of EXCERPT_BYTES, or not in it."""
        at = -1
        for _ in range(ends):
            at = body.find(b"\n", at + 1, EXCERPT_BYTES)
            if at < 0:
                return True
        return at + 1 > EXCERPT_BYTES // 2

    def _from_line(self, file: BinaryIO, line: int) -> bytes | None:
        """EXCERPT_BYTES + 1 bytes from the start of `line` on; None when the file has fewer lines before it."""
        head, skipped = self._skip_lines(file, line - 1)
        if skipped < line - 1:
            return None
        body = head[:EXCERPT_BYTES + 1]
        if len(body) <= EXCERPT_BYTES:
            body += file.read(EXCERPT_BYTES + 1 - len(body))
        return body

    @staticmethod
    def _skip_lines(file: BinaryIO, count: int) -> tuple[bytes, int]:
        """Read past `count` line ends: what was read after the last of them, and how many there were (fewer: the file
        ended first)."""
        seen = 0
        while seen < count:
            chunk = file.read(_READ_CHUNK)
            if not chunk:
                return b"", seen
            ends = chunk.count(b"\n")
            if seen + ends < count:
                seen += ends
                continue
            at = -1
            for _ in range(count - seen):
                at = chunk.index(b"\n", at + 1)
            return chunk[at + 1:], count
        return b"", seen

    def inspect(self, profile_id: UUID, path: str) -> FileInspection:
        """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
        the run will stop at or should be told (another engine's dump, objects dropped first, only a filter's rows of a
        table, not UTF-8 or UTF-16, COPY, a dump cut short)."""
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
        if header is not None and header.filtered_table is not None:  # fix wave F2
            warnings.append(filtered_rows(header.filtered_table, replaces=header.drop_first))
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
