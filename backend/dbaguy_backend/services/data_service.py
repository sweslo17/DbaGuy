"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts, and Show in Finder for what a dump
wrote. No job runs here. Run SQL file's inspection opens no connection at all (a read-only profile is refused first,
R-06-6); a dump's plan reads the catalog on the metadata connection, without definitions (a read-only profile may dump).

Logs: INFO the profile and the path of each inspection, plan and reveal; never the file's contents."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, Dumping, InvalidText, StatementError
from ..adapters.dump import DumpCatalog
from ..adapters.registry import AdapterRegistry
from ..adapters.base import CONNECTION_LOST, INVALID_TEXT
from ..domain.data_jobs import (
    BE_DUMPED, ENGINE_NAMES, RUN_SQL_FILES, DumpContent, DumpHeader, DumpObjectKinds, DumpPlan, DumpPlanObject,
    DumpScope, FileInspection, cannot, cannot_read,
)
from ..domain.errors import FileNotFound, NotFound, NotSupported, QueryFailed, ReadOnlyProfile, StorageError
from ..domain.profiles import AnyProfile, Engine, SqliteProfile
from ..sessions.jobs.dump import data_order, target_problem
from ..sessions.jobs.registry import JobRegistry
from ..sql.dump_header import CUT_SHORT, END_BYTES, cut_short, parse_header
from ..sql.dump_script import not_included
from ..sql.statement_stream import UTF16_MESSAGE, starts_with_utf16_bom
from .metadata_service import MetadataService
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
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
        path: str | None,
    ) -> DumpPlan:
        """What a dump of `scope` would write (the objects under their own names, DumpPlan), what its header says is
        left out, its consistency and its warnings: the catalog's, a data-only dump's foreign-key cycle, and for `path`
        a file the dump would refuse (M13) or little free disk where it goes (CR-4: never a refusal)."""
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
        warnings = list(catalog.warnings)
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
