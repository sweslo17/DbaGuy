"""A dump (spec ⑥ §3.3) on the job's own session: the engine's output settings and snapshot (`Dumping.dump_session`),
the catalog read inside it (`dump_catalog`), then the file — header, settings, the objects before the data, each
table's rows as multi-row INSERTs read a chunk at a time, the objects after the data — written through a temporary
file (`atomic_text_file`): a dump that fails or is cancelled leaves no file and keeps any old one (R-06-12, CR-4).

Memory stays bounded by the rows' size, not their number (pre-flight M12, Task 9 fix M1): a table's first read takes
one row, each next one as many as fit FETCH_CHARS of row text at the size of the largest row seen so far (at most
FETCH_ROWS), and the adapters read no rows ahead of a job's fetch (RunOptions.describe False). A table's rows are read
until the result says `[]`: a short chunk may come before an error later in the set (fix H1). The disk is never filled
to its last byte (M14): below DISK_FLOOR_BYTES free the dump stops, every DISK_CHECK_CHARS written. A row too long to
restore is said once per table (R-06-11, M11). A table with no column a dump can write keeps its rows as rows of
defaults (06a Task 7 review). A data-only dump writes the tables a table's foreign keys reference first (M9), and
says what may still fail where the restore checks foreign keys (a cycle, a table that references itself: fix M4). A
filtered file says so in its header, and when it also drops and recreates its table, its outcome says that restoring it
replaces the table with the filter's rows (06a fix wave F2).

A filter is a condition and nothing else (fix M2): every SELECT built from it must be a plain read by the classifier
the read-only allow-list uses, and pass the session's gate, or nothing is sent (`invalid_filter`).

A dump is a read: no History record (like an export), and a read-only connection may dump. Logs: INFO the scope, the
file and the counts, WARNING a dump that is not one snapshot and what it leaves out (L14); never a value."""

import errno
import os
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from loguru import logger

from ...adapters.base import INVALID_TEXT, Dumping, InvalidText, StatementCancelled, StatementError
from ...adapters.columns import TableColumn
from ...adapters.dump import DumpCatalog, DumpSession, DumpTable, parents_first
from ...domain.data_jobs import BE_DUMPED, JobObject, cannot
from ...domain.errors import DomainError
from ...domain.execution import DumpCommand
from ...domain.profiles import Engine
from ...services.safety import GuardAction, is_plain_read
from ...sql.bulk_insert import InsertBatcher, size_warning
from ...sql.dialects import Dialect
from ...sql.dump_script import (
    RESTORE_BACKSLASH_ESCAPES, RESTORE_CHECKS_FOREIGN_KEYS, DumpScript, default_rows_text, restore_warnings,
    statement_end, table_sql,
)
from ...sql.select_builder import FILTER_NOT_A_CONDITION, build_copy_select, build_count_select, check_filter
from ...storage.atomic_file import atomic_text_file
from .context import Job, JobContext, JobReport

DUMP_IO = "dump_io"
FETCH_ROWS = 1000  # rows read at a time at most (the export's chunk)
FETCH_CHARS = 32 * 1_048_576  # … and about this much row text (pre-flight M12)
FIRST_FETCH = 1  # a table's first read, before the size of its rows is known (Task 9 fix M1)
# The free space is looked at every this much written (pre-flight M14): at most 64 MiB of bytes (4 a character, Task 9
# fix L6) between two looks, under DISK_FLOOR_BYTES
DISK_CHECK_CHARS = 16 * 1_048_576
DISK_FLOOR_BYTES = 256 * 1_048_576  # below this much free, the dump stops before the disk is full
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")
FK_CYCLE = "Foreign keys between these tables form a cycle: a data-only restore may fail"
SELF_REFERENCE = "{} references itself: a data-only restore may fail unless its rows load in order"  # fix M4
NOT_UNICODE = ("holds text that is not valid Unicode (half of a surrogate pair), which a UTF-8 file cannot hold. "
               "Nothing was written")
NOT_UTF8 = "holds text that is not valid UTF-8, which a UTF-8 file cannot hold. Nothing was written"  # fix L2
CATALOG_NOT_UTF8 = f"A name or definition in this database {NOT_UTF8}"
INVALID_FILTER = "invalid_filter"  # Task 9 fix M2; its text is select_builder's FILTER_NOT_A_CONDITION, never the value


def scope_text(command: DumpCommand) -> str:
    scope = command.scope
    if scope.kind == "database":
        return "the database"
    if scope.kind == "schema":
        return str(scope.schema_name)
    return f"{scope.schema_name}.{scope.names[0]}" if len(scope.names) == 1 else f"{len(scope.names)} tables"


def _is_database_file(target: Path, source_file: Path | None) -> bool:
    """A SQLite database (its header), one of its side files, or the file the dump reads: replacing it with SQL text
    would lose the database while its own connection still holds it open (pre-flight M13)."""
    if target.name.endswith(SQLITE_SIDE_FILES):
        return True
    try:
        if not target.is_file():
            return False
        source = source_file.expanduser() if source_file is not None else None
        if source is not None and source.exists() and os.path.samefile(target, source):
            return True
        with target.open("rb") as file:
            return file.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False  # it cannot be read: not known to be a database (writing it says what is wrong, if anything)


def target_problem(path: str, source_file: Path | None = None) -> str | None:
    """Why a dump cannot write `path` (an `invalid_path` refusal), or None. `source_file`: the SQLite file dumped."""
    target = Path(path).expanduser()
    if not target.is_absolute() or target.is_dir() or not target.parent.is_dir():
        return f"Choose a file in an existing folder to dump into: {path}"
    if _is_database_file(target, source_file):
        return f"{path} is a SQLite database file: choose another file to dump into"
    return None


def data_order(dialect: Dialect, tables: tuple[DumpTable, ...]) -> tuple[list[DumpTable], list[str]]:
    """A data-only dump's tables, parents first (pre-flight M9), and what may still fail where the restore checks
    foreign keys as rows load (RESTORE_CHECKS_FOREIGN_KEYS): a cycle, and each table that references itself (Task 9
    fix M4). The dump's outcome and its plan say the same."""
    ordered, cycle = parents_first(tables)
    if dialect not in RESTORE_CHECKS_FOREIGN_KEYS:
        return ordered, []
    said = [FK_CYCLE] if cycle else []
    return ordered, said + [SELF_REFERENCE.format(table.name) for table in ordered if table.self_referencing]


def _first_unicode_problem(text: str) -> int | None:
    """Where `text` holds what UTF-8 cannot (a lone surrogate), or None."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return exc.start
    return None


class _Changed(StatementError):
    """A table that DDL changed after the snapshot began, said by its name."""


class _DiskFloor(OSError):
    """The dump stopped itself before the disk was full (pre-flight M14)."""

    def __init__(self) -> None:
        super().__init__(errno.ENOSPC, f"Disk almost full (less than {DISK_FLOOR_BYTES // 1_048_576} MiB free), so "
                                       "the dump stopped before filling it")


class DumpJob(Job):
    kind = "dump"

    def __init__(self, command: DumpCommand, connection_name: str, engine: Engine, *,
                 source_file: Path | None = None) -> None:
        self._command = command
        self._name = connection_name
        self._engine = Engine(engine)
        self._source_file = source_file  # SQLite: the file the dump reads (never its target)

    @property
    def title(self) -> str:
        return f"Dump {scope_text(self._command)} of {self._name} to {Path(self._command.path).name}"

    def run(self, ctx: JobContext) -> JobReport:
        """What is refused before anything is sent is `not_started` (the worker's); then `completed` or `failed`."""
        if (problem := target_problem(self._command.path, self._source_file)) is not None:
            raise StatementError(problem, code="invalid_path")
        connection = ctx.connection
        if not isinstance(connection, Dumping):
            raise StatementError(cannot(self._engine, BE_DUMPED), code="not_supported")
        self._check_filter(ctx)
        try:
            session = connection.dump_session(self._command.scope)  # SQL Server reads whether SNAPSHOT is allowed
        except DomainError as exc:
            raise StatementError(exc.message, code=exc.code) from None
        try:
            try:
                for sql in session.setup:
                    ctx.drain(ctx.run(sql))
            except StatementError as exc:
                return self._failed(ctx, exc)
            return self._dump(ctx, connection, session, Path(self._command.path).expanduser())
        finally:
            self._end_session(ctx, session.teardown)

    def _check_filter(self, ctx: JobContext) -> None:
        """Before anything is sent: the filter is one condition (as browsing checks it), and the SELECT it makes only
        reads (fix M2)."""
        command = self._command
        if command.filter is None:
            return
        try:
            check_filter(ctx.dialect, command.filter)
            scope = command.scope
            self._read_only_filter(ctx, build_count_select(ctx.dialect, scope.schema_name, scope.names[0],
                                                           command.filter))
        except DomainError as exc:
            raise StatementError(exc.message, code=INVALID_FILTER) from None

    def _read_only_filter(self, ctx: JobContext, sql: str) -> str:
        """`sql`, a SELECT built from the filter, when it is a plain read on any profile (the read-only allow-list's
        classifier) and the session's gate lets it through (a read-only profile can never send more): else nothing of
        it is sent. Without a filter every SELECT is DbaGuy's own."""
        if self._command.filter is None:
            return sql
        if not is_plain_read(sql, ctx.dialect):
            raise StatementError(FILTER_NOT_A_CONDITION, code=INVALID_FILTER)
        if ctx.hooks.gate(sql).action != GuardAction.RUN:
            raise StatementError(f"This connection is read-only: {FILTER_NOT_A_CONDITION}", code="read_only")
        return sql

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """A dump only reads: whatever broke, nothing was written (the temporary file is gone)."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", path=self._command.path,
                                     cancelled=isinstance(exc, StatementCancelled)), error)

    def _dump(self, ctx: JobContext, connection: Dumping, session: DumpSession, target: Path) -> JobReport:
        command = self._command
        objects: list[JobObject] = []
        rows = 0
        catalog: DumpCatalog | None = None
        try:
            catalog = connection.dump_catalog(command.scope, command.objects)
            if command.filter is not None:
                for sql in session.filter_setup:
                    ctx.drain(ctx.run(sql))
            script = DumpScript(ctx.dialect, self._engine, catalog, content=command.content,
                                drop_first=command.drop_first, filter=command.filter,
                                created=datetime.now().astimezone().isoformat("T", "seconds"))
            tables, said = data_order(ctx.dialect, catalog.tables) if not script.schema else (list(catalog.tables), [])
            warnings = [*restore_warnings(catalog, command.content, command.drop_first, command.filter),
                        *catalog.warnings, *said]
            self._log_start(catalog, target)
            with atomic_text_file(target) as file:
                writer = _Writer(file, ctx, target.parent)
                ctx.progress.stage = "schema"
                ctx.tick(force=True)
                writer.write(script.head())
                writer.write(script.pre())
                if script.data:
                    ctx.progress.stage = "data"
                    for table in tables:
                        count = self._table(ctx, writer, script, catalog, session, table, warnings)
                        rows += count
                        objects.append(JobObject(kind="table", name=table.name, rows=count))
                ctx.progress.stage, ctx.progress.object = "post_data", None
                ctx.tick(force=True)
                writer.write(script.post())
                writer.write(script.tail(rows))
        except InvalidText:  # in the catalog (a table's rows are named in _table)
            return self._failed(ctx, StatementError(CATALOG_NOT_UTF8, code=INVALID_TEXT))
        except StatementError as exc:
            if exc.code in session.changed_codes and not isinstance(exc, _Changed | StatementCancelled):
                exc = StatementError(f"A table was changed while the dump ran: dump it again\n{exc.message}",
                                     code=exc.code)
            return self._failed(ctx, exc)
        except DomainError as exc:  # the schema or a table is gone, or a scope the engine does not dump
            return self._failed(ctx, StatementError(exc.message, code=exc.code))
        except UnicodeEncodeError:  # in a name or a definition (a table's rows are named in _table)
            return self._failed(ctx, StatementError(f"{self._culprit(ctx, catalog)} {NOT_UNICODE}", code=INVALID_TEXT))
        except OSError as exc:
            why = exc.strerror if isinstance(exc, _DiskFloor) else (
                "Disk full" if exc.errno == errno.ENOSPC else (exc.strerror or str(exc)))
            return self._failed(ctx, StatementError(f"Cannot write {command.path}: {why}. Nothing was written",
                                                    code=DUMP_IO))
        return JobReport(ctx.outcome("completed", rows=rows, bytes=target.stat().st_size, path=str(target),
                                     objects=objects, warnings=warnings))

    def _table(
        self, ctx: JobContext, writer: "_Writer", script: DumpScript, catalog: DumpCatalog, session: DumpSession,
        table: DumpTable, warnings: list[str],
    ) -> int:
        """One table's rows; returns how many."""
        dialect = ctx.dialect
        ctx.progress.object = table.name
        ctx.tick(force=True)
        writer.write(script.table_head(table))
        columns = [column for column in table.columns if column.copied]
        try:
            if columns:
                count = self._rows(ctx, writer, catalog, table, columns, warnings)
            else:  # only rowversion, computed or sql_variant columns: as many rows of defaults
                count = self._count(ctx, table)
                for text in default_rows_text(dialect, table, count):
                    writer.write(text)
                ctx.progress.rows += count
        except InvalidText as exc:  # fix L2: the driver's own text quotes the value
            where = f"{table.name}.{exc.column}" if exc.column else table.name
            raise StatementError(f"{where} {NOT_UTF8}", code=INVALID_TEXT) from None
        except StatementError as exc:
            if exc.code in session.changed_codes and not isinstance(exc, StatementCancelled):
                raise _Changed(f"{table.name} was changed while the dump ran: dump it again\n{exc.message}",
                               code=exc.code) from None
            raise
        writer.write(script.table_tail(table, count))
        ctx.tick(force=True)
        return count

    def _rows(self, ctx: JobContext, writer: "_Writer", catalog: DumpCatalog, table: DumpTable,
              columns: list[TableColumn], warnings: list[str]) -> int:
        """The table's rows as INSERTs, read in chunks sized by the rows' text (pre-flight M12)."""
        dialect = ctx.dialect
        # MySQL's literals are written for the sql_mode the header sets (backslash escapes on), never the source's
        escapes = RESTORE_BACKSLASH_ESCAPES if dialect is Dialect.MYSQL else None
        batcher = InsertBatcher(dialect, table_sql(dialect, table), columns, backslash_escapes=escapes)
        sql = build_copy_select(dialect, table.schema, table.table, table.columns, self._command.filter,
                                only=table.only)
        warned = False
        count = 0
        end = statement_end(dialect)

        def write(statement: str | None) -> None:
            nonlocal warned
            if statement is None:
                return
            if not warned and (said := size_warning(dialect, table.name, statement, catalog.max_statement_bytes)):
                warnings.append(said)
                warned = True
            try:
                writer.write(statement)  # and its terminator on its own: a large statement is not copied again
                writer.write(end)
            except UnicodeEncodeError:
                offset = _first_unicode_problem(statement)
                column = batcher.column_at(statement, offset) if offset is not None else None
                where = f"{table.name}.{column}" if column is not None else f"A name of {table.name}"
                raise StatementError(f"{where} {NOT_UNICODE}", code=INVALID_TEXT) from None

        size, largest = FIRST_FETCH, 0
        result = ctx.run(self._read_only_filter(ctx, sql))
        try:
            # Until `[]`: a short chunk may come before an error later in the set (fix H1); a result that knows it is
            # over says `[]` without a round trip
            while chunk := result.fetch(size):
                for row in chunk:
                    before = batcher.row_chars
                    write(batcher.add(row))
                    largest = max(largest, batcher.row_chars - before)
                count += len(chunk)
                ctx.progress.rows += len(chunk)
                ctx.raise_if_cancelled()
                ctx.tick()
                size = max(1, min(FETCH_ROWS, FETCH_CHARS // max(largest, 1)))  # fix M1: the largest row, not the mean
        finally:
            ctx.close(result)
        write(batcher.flush())
        return count

    def _count(self, ctx: JobContext, table: DumpTable) -> int:
        sql = build_count_select(ctx.dialect, table.schema, table.table, self._command.filter, only=table.only)
        result = ctx.run(self._read_only_filter(ctx, sql))
        try:
            found = result.fetch(1)
            while result.fetch(1):  # to its `[]`, so an error after the row is not lost (fix H1)
                pass
        finally:
            ctx.close(result)
        return int(found[0][0]) if found and found[0][0] is not None else 0

    def _failed(self, ctx: JobContext, error: StatementError) -> JobReport:
        """Nothing was written (the temporary file is gone), and an old file of that name is as it was: `failed`, or
        `not_started` when not even the session's first setting was sent (a Cancel before it)."""
        return JobReport(ctx.outcome("failed" if ctx.sent else "not_started",
                                     cancelled=isinstance(error, StatementCancelled), path=self._command.path), error)

    @staticmethod
    def _culprit(ctx: JobContext, catalog: DumpCatalog | None) -> str:
        """What holds the text that is not Unicode: the first object whose name or definition does, else the table
        being written."""
        if catalog is not None:
            for item in (*catalog.pre, *catalog.post):
                if _first_unicode_problem("".join((item.name, *item.statements))) is not None:
                    return f"The {item.kind} {item.name}"
            for table in catalog.tables:
                names = (table.name, *(column.name for column in table.columns))
                if _first_unicode_problem("".join(names)) is not None:
                    return f"A name of {table.name}"
        return ctx.progress.object or "The catalog"

    def _log_start(self, catalog: DumpCatalog, target: Path) -> None:
        scope = scope_text(self._command)
        logger.info("[{}] dump of {} ({}) to {}: {} object(s) before the data, {} table(s), {} after", self._name,
                    scope, self._command.content, target, len(catalog.pre), len(catalog.tables), len(catalog.post))
        if catalog.consistency == "per_table":  # D-06-12
            logger.warning("[{}] dump of {}: not one consistent snapshot, each table is read in its turn", self._name,
                           scope)
        if left_out := catalog.not_included[1:]:  # pre-flight L14: the counts the header gives (never a value)
            logger.warning("[{}] dump of {}: left out {}", self._name, scope, "; ".join(left_out))

    @staticmethod
    def _end_session(ctx: JobContext, teardown: Iterable[str]) -> None:
        """End the snapshot. A Cancel never reaches it; a connection that is gone, or one the setup never reached (a
        Cancel before it), has nothing to end."""
        ctx.stop_cancels()
        if not ctx.sent:
            return
        for sql in teardown:
            try:
                ctx.drain(ctx.run(sql))
            except StatementError as exc:
                logger.info("[dump] ending the dump's snapshot failed ({})", exc.code)
                return


class _Writer:
    """Writes text to the dump's file, counts it for the progress (characters: about the bytes), and stops the dump
    before the disk is full (pre-flight M14)."""

    def __init__(self, file: TextIO, ctx: JobContext, folder: Path) -> None:
        self._file = file
        self._ctx = ctx
        self._folder = folder
        self._next_check = 0  # the first write looks too: a disk that is full already writes nothing

    def write(self, text: str) -> None:
        if self._ctx.progress.bytes >= self._next_check:
            self._next_check = self._ctx.progress.bytes + DISK_CHECK_CHARS
            self._check_free()
        self._file.write(text)
        self._ctx.progress.bytes += len(text)

    def _check_free(self) -> None:
        try:
            free = shutil.disk_usage(self._folder).free
        except OSError as exc:
            logger.debug("[dump] free space of {} unknown ({})", self._folder, exc)
            return
        if free < DISK_FLOOR_BYTES:
            raise _DiskFloor()
