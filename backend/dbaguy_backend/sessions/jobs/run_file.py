"""Run SQL file (spec ⑥ §3.4): a file read as statements (`StatementStream`) and sent one at a time on the job's own
session. All or nothing: the dialect's transaction frame around everything, and any stop before the COMMIT rolls it
back. Statement by statement: each takes effect on its own; an error stops the run or is skipped. Every way a run ends
is classified here (R-06-12) and nothing is ever sent twice.

Not classified (R-06-6): the user confirmed the whole file, so each statement passes only the read-only gate
(`gate_confirmed`). An all-or-nothing run refuses, before sending them, the file's own transaction statements and (MySQL)
the statements MySQL commits around, SET autocommit and procedure calls (pre-flight H1, rulings R1, R7); a transaction
that ends anyway stops the run (`partial` when only MySQL's commit can have ended it, `unknown` otherwise, H3). When a
statement fails, what it may have done to the transaction decides between `rolled_back` and `unknown`
(`sql/transaction_text.py`; MySQL: any failure that ended it but a deadlock, R1; SQL Server: a trigger's COMMIT, 3609,
R2; a COMMIT and a new transaction inside a batch, told by CURRENT_TRANSACTION_ID(), R7). A ROLLBACK that left a table's
changes (MySQL MyISAM) makes any outcome `unknown` (R3). Statement by statement, the file's own open transaction is
followed (H2, R5): a run that stops inside it, or ends with it open, rolls it back and says which statements that undid.
A run from statement N still sends the session settings before N (R4).

Logs: INFO the run's start (path, size, mode); the worker logs its end. A skipped error's text only at DEBUG (it echoes
literals, D-06-12)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from loguru import logger

from ...adapters.base import RunOptions, StatementCancelled, StatementError
from ...domain.data_jobs import MAX_JOB_ERRORS, JobError, JobOutcomeKind, StoppedAt, cannot_read
from ...domain.errors import READ_ONLY_MESSAGE
from ...domain.execution import MessageSeverity, RunFileCommand
from ...services.safety import GuardAction
from ...sql.dialects import Dialect, quote_ident, transaction_frame
from ...sql.dump_header import CUT_SHORT, END_BYTES, cut_short, parse_header
from ...sql.session_settings import is_session_setting
from ...sql.statement_stream import (
    FILE_NOT_UTF8, MAX_STATEMENT_CHARS, UTF16_MESSAGE, FileStatement, StatementStream, StreamError,
)
from ...sql.transaction_text import (
    calls_a_procedure, ends_the_transaction, implicit_commit_word, may_commit, runs_several, sets_autocommit,
    transaction_word,
)
from ..edit_apply import TRANSACTION_ENDED as ENDED_INSIDE  # SQL Server 3609, as ⑤ reads it
from .context import JOB_ROLLBACK_FAILED, Job, JobContext, JobReport

CONNECTION_LOST = "connection_lost"
READ_ONLY = "read_only"
UNSUPPORTED_COPY = "unsupported_copy"
TRANSACTION_IN_FILE = "transaction_in_file"
TRANSACTION_ENDED = "transaction_ended"
IMPLICIT_COMMIT = "implicit_commit"
PROCEDURE_CALL = "procedure_call"
PSQL_META_COMMAND = "psql_meta_command"
FILE_IO = "file_io"
FILE_TRUNCATED = "file_truncated"
COPY_HINT = "pg_dump: use --inserts, or run this file with psql"
HEX_BLOB_HINT = "mysqldump writes binary columns as raw bytes unless it is given --hex-blob"
ROLLED_BACK = "The transaction was rolled back: nothing was applied"
KEPT = "Some tables cannot roll back (non-transactional): their changes stayed"  # ruling R3
# A job session of SQL Server: a login whose `user options` turn implicit transactions on would make each statement
# by statement open one, and an all-or-nothing BEGIN nest (pre-flight L8)
_NO_IMPLICIT_TRANSACTIONS = "SET IMPLICIT_TRANSACTIONS OFF"
_TRANSACTION_ID = "SELECT CURRENT_TRANSACTION_ID()"  # SQL Server 2016+: another id = another transaction (R7)
_SHOW_WARNINGS = "SHOW WARNINGS"
_UNLOCK_TABLES = "UNLOCK TABLES"
_NOT_ROLLED_BACK = "1196"  # MySQL: "Some non-transactional changed tables couldn't be rolled back"
_DEADLOCK = "1213"  # MySQL: InnoDB rolled the whole transaction back (R1: the one failure that ends it known undone)
# psql meta-commands pg_dump 17 writes around its plain output (A P8): skipped, with a message
_SKIPPED_META = frozenset({"\\restrict", "\\unrestrict"})
_FATAL = frozenset({UNSUPPORTED_COPY, PSQL_META_COMMAND, READ_ONLY})  # a skip-and-continue run stops at these too


class _Stop(Exception):
    """The run stops at `statement` (None: before any statement of the file): why; whether the statement reached the
    server (`sent`: it failed or was cancelled there), whether the link died while it ran (`lost`), whether another
    transaction began inside it (`reopened`), and the outcome when the reason itself decides it."""

    def __init__(self, error: StatementError, statement: FileStatement | None, *, sent: bool = False,
                 lost: bool = False, reopened: bool = False, outcome: JobOutcomeKind | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.statement = statement
        self.sent = sent
        self.lost = lost
        self.reopened = reopened
        self.outcome = outcome


def _reworded(error: StatementError, message: str) -> StatementError:
    """`error` with another text; a cancel stays a cancel."""
    if isinstance(error, StatementCancelled):
        return StatementCancelled(message)
    return StatementError(message, code=error.code, position=error.position)


def summary(command: RunFileCommand, size: int) -> str:
    """The History record's text (D-06-9): what ran, never the file's statements."""
    mode = "all or nothing" if command.mode == "all_or_nothing" else (
        "statement by statement, " + ("skipping errors" if command.on_error == "continue" else "stopping at an error"))
    start = f", from statement {command.from_statement:,}" if command.from_statement > 1 else ""
    return f"-- Run SQL file {command.path} ({size / 1_048_576:.1f} MB), {mode}{start}"


class RunFileJob(Job):
    kind = "run_file"

    def __init__(self, command: RunFileCommand, connection_name: str) -> None:
        self._command = command
        self._name = connection_name

    @property
    def title(self) -> str:
        return f"Run {Path(self._command.path).name} on {self._name}"

    def run(self, ctx: JobContext) -> JobReport:
        command = self._command
        path = self._check(ctx)
        try:
            file = path.open("rb")
        except OSError as exc:
            raise StatementError(cannot_read(command.path, exc), code=FILE_IO) from None
        with file:
            size, truncated = self._look(file)
            warnings = []
            if truncated:  # a DbaGuy dump without its last line (pre-flight M15)
                if command.mode == "all_or_nothing":
                    raise StatementError(CUT_SHORT, code=FILE_TRUNCATED)
                warnings.append(CUT_SHORT)
            ctx.progress.stage, ctx.progress.total_bytes = "statements", size
            ctx.begin_record(summary(command, size))
            logger.info("[{}] run of {} ({:,} bytes) {}, from statement {}", self._name, path, size, command.mode,
                        command.from_statement)
            stream = StatementStream(file.read, ctx.dialect, max_statement_chars=MAX_STATEMENT_CHARS)
            return _Run(ctx, command, warnings).go(stream)

    def _check(self, ctx: JobContext) -> Path:
        """What refuses the run before anything is sent: the job then never started."""
        command = self._command
        path = Path(command.path).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise StatementError(f"{command.path} is not a file", code="file_not_found")
        if command.mode == "all_or_nothing" and not ctx.capabilities.transactions:
            raise StatementError("This connection has no transactions: run the file statement by statement",
                                 code="not_supported")
        if command.query_context is not None and not ctx.capabilities.query_context:
            raise StatementError("This engine does not take a query context", code="unsupported_option")
        if command.search_path is not None and ctx.dialect is not Dialect.POSTGRES:
            raise StatementError("Only PostgreSQL takes a search path", code="unsupported_option")
        if ctx.hooks.read_only():
            raise StatementError(READ_ONLY_MESSAGE, code=READ_ONLY)
        return path

    def _look(self, file: BinaryIO) -> tuple[int, bool]:
        """The file's size, and whether it is a DbaGuy dump cut short: its first and last bytes (then back to its
        start)."""
        try:
            size = os.fstat(file.fileno()).st_size
            head = file.read(END_BYTES)
            if size > END_BYTES:
                file.seek(size - END_BYTES)
                end = file.read(END_BYTES)
            else:
                end = head
            file.seek(0)
        except OSError as exc:
            raise StatementError(cannot_read(self._command.path, exc), code=FILE_IO) from None
        first = head.decode("utf-8", "replace")
        return size, parse_header(first) is not None and cut_short(first, end.decode("utf-8", "replace"))


class _Run:
    """One run's state and the outcome table of R-06-12."""

    def __init__(self, ctx: JobContext, command: RunFileCommand, warnings: list[str]) -> None:
        self.ctx = ctx
        self.command = command
        self.dialect = ctx.dialect
        self.all_or_nothing = command.mode == "all_or_nothing"
        self.options = RunOptions(describe=False, query_context=command.query_context)
        self.done = 0  # statements sent and finished
        self.rows = 0
        self.bytes = 0
        self.errors: list[JobError] = []
        self.error_count = 0
        self.read = 0  # the number of the last statement read
        self.last_line = 0  # the line of the last statement read
        self.warnings = warnings  # for the outcome, as they come up
        self.settings_sent = 0  # session settings before from_statement, sent all the same (R4)
        self.kept = False  # a ROLLBACK left some tables' changes (MySQL, non-transactional tables; R3)
        # Statement by statement: where the file's own open transaction began (its statements take effect only at its
        # COMMIT), and the counts from before it (pre-flight H2)
        self.tx_from: FileStatement | None = None
        self.done_before_tx = 0
        self.rows_before_tx = 0

    def go(self, stream: StatementStream) -> JobReport:
        try:
            self._begin()
            for statement in self._statements(stream):
                self._one(statement)
        except _Stop as stop:
            return self._stopped(stop)
        if self.all_or_nothing:
            return self._commit()
        return self._statements_done()

    # ---- the statements ----

    def _begin(self) -> None:
        """The session's settings, then (all or nothing) the transaction: a failure here applied nothing of the
        file."""
        setup = []
        if self.dialect is Dialect.TSQL:
            setup.append(_NO_IMPLICIT_TRANSACTIONS)
        if self.command.search_path is not None:
            setup.append(f"SET search_path TO {quote_ident(Dialect.POSTGRES, self.command.search_path)}")
        if self.all_or_nothing:
            setup.append(transaction_frame(self.dialect).begin)
        for sql in setup:
            try:
                self.ctx.drain(self.ctx.run(sql, self.options))
            except StatementError as exc:
                raise _Stop(exc, None) from None

    def _statements(self, stream: StatementStream) -> Iterator[FileStatement]:
        """The file's statements, from `from_statement` on; a file that cannot be read on stops the run there."""
        statements = iter(stream)
        while True:
            try:
                statement = next(statements)
            except StopIteration:
                return
            except StreamError as exc:
                message = exc.message
                if exc.code == FILE_NOT_UTF8 and message != UTF16_MESSAGE and self.dialect is Dialect.MYSQL:
                    message += f" ({HEX_BLOB_HINT})"
                raise _Stop(StatementError(message, code=exc.code), self._at(exc.line)) from None
            except OSError as exc:
                error = StatementError(cannot_read(self.command.path, exc), code=FILE_IO)
                raise _Stop(error, self._at(self.last_line + 1)) from None
            self.last_line = statement.line
            if statement.meta:
                if self.read + 1 >= self.command.from_statement:  # a psql line before the start was dealt with then
                    self._meta(statement)
                continue
            self.read = statement.index
            if statement.index >= self.command.from_statement:
                yield statement
                continue
            # read, never sent (only so that what follows is split as the whole file splits it), unless it sets up
            # the session: the job's session is a fresh one (R4)
            if is_session_setting(statement.text, self.dialect):
                self._resend(statement)
            self.bytes = self.ctx.progress.bytes = statement.end_byte
            self.ctx.progress.line = statement.line
            self.ctx.tick()
            try:
                self.ctx.raise_if_cancelled()  # a long skip still stops at a Cancel (pre-flight M4)
            except StatementCancelled as exc:
                raise _Stop(exc, None) from None  # nothing from from_statement on was sent

    def _resend(self, statement: FileStatement) -> None:
        """A session setting before from_statement (R4): sent as the file would, with the same refusals. Its failure
        stops the run before any statement it was asked to run."""
        try:
            self._admit(statement)
            self.ctx.drain(self.ctx.run(statement.text, self.options))
        except StatementError as exc:
            raise _Stop(exc, None) from None
        self.settings_sent += 1

    def _at(self, line: int) -> FileStatement:
        """Where a run stops that has no statement of its own (a line that is not text): the statement after the last
        one read (pre-flight M3)."""
        return FileStatement(index=self.read + 1, line=line, text="", end_byte=self.bytes)

    def _meta(self, statement: FileStatement) -> None:
        word = statement.text.split(None, 1)[0]
        if word in _SKIPPED_META:
            self.ctx.message(MessageSeverity.INFO, f"Skipped line {statement.line:,}: {word} is a psql command")
            return
        error = StatementError(f"Line {statement.line:,} is a psql command ({word}): run this file with psql",
                               code=PSQL_META_COMMAND)
        raise _Stop(error, self._at(statement.line))  # not a statement: stopped before the next one

    def _admit(self, statement: FileStatement) -> None:
        """What a statement passes before it is sent: a Cancel between statements, the read-only gate, and (all or
        nothing) what would end the transaction."""
        self.ctx.raise_if_cancelled()  # a Cancel between statements: this one is never sent
        if (decision := self.ctx.hooks.gate(statement.text)).action != GuardAction.RUN:
            kind = decision.classification.kind
            raise StatementError(f"This connection is read-only: {kind} statements are not allowed", code=READ_ONLY)
        if self.all_or_nothing:
            self._refuse_what_ends_the_transaction(statement)

    def _one(self, statement: FileStatement) -> None:
        ctx = self.ctx
        rows_before = self.rows
        try:
            self._admit(statement)
            before = self._transaction_id() if statement.repeat and self._watched(statement) else None
        except StatementError as exc:
            raise _Stop(exc, statement) from None
        if statement.repeat == 0:  # T-SQL `GO 0`: never sent, so not counted (pre-flight L7)
            self.bytes = statement.end_byte
            self._progress(statement)
            return
        runs = 0
        for _ in range(statement.repeat):  # T-SQL `GO n`
            try:
                affected = ctx.drain(ctx.run(statement.text, self.options))
            except StatementError as exc:
                if isinstance(exc, StatementCancelled) and exc.completed:
                    # the server finished it before the cancel could stop it (R8): applied; the Cancel, still asked,
                    # stops the run at the next statement
                    runs += 1
                    break
                self._failed(exc, statement, runs, rows_before, self._reopened(before))
                return
            runs += 1
            self.rows += max(affected or 0, 0)
        self.done += 1
        self.bytes = statement.end_byte
        reopened = self._reopened(before)
        if self.all_or_nothing:
            self._check_transaction(statement, reopened)
        else:
            self._track_transaction(statement, rows_before, reopened)
        self._progress(statement)

    def _refuse_what_ends_the_transaction(self, statement: FileStatement) -> None:
        """All or nothing: the file's own transaction statements are never sent, nor (MySQL) a statement MySQL commits
        the open transaction before, even when it then fails (pre-flight H1, R1), SET autocommit, which commits it when
        it turns autocommit on, and a procedure call or prepared statement, which may commit inside (R7)."""
        line, text, dialect = statement.line, statement.text, self.dialect
        if (word := transaction_word(text, dialect)) is not None:
            raise StatementError(
                f"The file controls its own transactions (line {line:,}: {word}): run it statement by statement",
                code=TRANSACTION_IN_FILE)
        if (word := implicit_commit_word(text, dialect)) is not None:
            raise StatementError(
                f"Line {line:,} ({word}) commits MySQL's transaction, so this file cannot run all or nothing: run it "
                "statement by statement", code=IMPLICIT_COMMIT)
        if sets_autocommit(text, dialect):
            raise StatementError(
                f"Line {line:,} sets autocommit, and turning it on commits MySQL's transaction, so this file cannot "
                "run all or nothing: run it statement by statement", code=IMPLICIT_COMMIT)
        if dialect is Dialect.MYSQL and calls_a_procedure(text, dialect):
            raise StatementError(
                f"Line {line:,} calls a procedure or a prepared statement, which may commit MySQL's transaction inside "
                "it, so this file cannot run all or nothing: run it statement by statement", code=PROCEDURE_CALL)

    def _check_transaction(self, statement: FileStatement, reopened: bool) -> None:
        """All or nothing, after a statement: the transaction must still be open, and still the same one. Only MySQL's
        commit around a statement is known to be a commit (S16, pre-flight H3); anything else may have committed or
        rolled back."""
        open_now = self._open()
        if open_now is None or (open_now and not reopened):
            return
        line = statement.line
        outcome: JobOutcomeKind = "unknown"
        if not open_now and self.dialect is Dialect.MYSQL and not calls_a_procedure(statement.text, self.dialect):
            message = (f"The transaction ended at line {line:,} (MySQL commits around DDL): the statements before it "
                       "are applied; run the rest statement by statement")
            outcome = "partial"
        elif not open_now:
            message = (f"The transaction ended at line {line:,} (a statement in the file committed or rolled it "
                       "back): what ran before it may or may not be applied; run the rest statement by statement")
        else:
            message = (f"The transaction ended and another began inside line {line:,} (a procedure or dynamic SQL): "
                       "what ran before it may or may not be applied; run the rest statement by statement")
        raise _Stop(StatementError(message, code=TRANSACTION_ENDED), statement, sent=True, outcome=outcome)

    def _failed(self, exc: StatementError, statement: FileStatement, runs: int, rows_before: int,
                reopened: bool) -> None:
        """A statement that failed: the run stops, or (skip and continue) the error is kept and the run goes on."""
        if runs:  # `GO n`: some of its runs went through (pre-flight L7)
            exc = _reworded(exc, f"{exc.message} (after {runs:,} of {statement.repeat:,} runs)")
        if exc.code == UNSUPPORTED_COPY:
            exc = StatementError(f"{exc.message}\n{COPY_HINT}", code=UNSUPPORTED_COPY)
        lost = exc.code == CONNECTION_LOST
        if self.all_or_nothing or self.command.on_error == "stop" or lost or exc.code in _FATAL \
                or isinstance(exc, StatementCancelled):
            raise _Stop(exc, statement, sent=True, lost=lost, reopened=reopened)
        if self._aborted():  # PostgreSQL, or SQL Server's XACT_STATE() = -1: every statement after it would fail too
            raise _Stop(_reworded(exc, f"{exc.message}\nThis error aborted the file's own transaction: nothing after "
                                       "it can run until that transaction ends"), statement, sent=True)
        logger.debug("[run] statement {} at line {} failed and was skipped: {}", statement.index, statement.line,
                     exc.message)
        self.error_count += 1
        if len(self.errors) < MAX_JOB_ERRORS:
            self.errors.append(JobError(line=statement.line, statement=statement.index, code=exc.code,
                                        message=exc.message))
        self._track_after_failure(statement, exc, rows_before, reopened)
        self.bytes = statement.end_byte
        self._progress(statement)

    def _progress(self, statement: FileStatement) -> None:
        progress = self.ctx.progress
        progress.statements, progress.line, progress.bytes, progress.rows = (
            self.done, statement.line, statement.end_byte, self.rows)
        progress.errors = self.error_count
        self.ctx.tick()

    # ---- the transaction's state ----

    def _open(self) -> bool | None:
        """Whether a transaction is open now (None: it cannot tell)."""
        try:
            return self.ctx.connection.in_transaction()
        except StatementError:
            return None

    def _aborted(self) -> bool:
        try:
            return self.ctx.connection.transaction_failed()
        except StatementError:
            return False

    def _ended_inside(self, error: StatementError) -> bool:
        """A server error that says the transaction ended inside the statement, maybe committing (SQL Server 3609: a
        trigger's COMMIT; R2, as ⑤)."""
        return error.code in ENDED_INSIDE.get(self.dialect, ())

    def _gone_unknown(self, statement: FileStatement, error: StatementError) -> bool:
        """The transaction is gone after `statement` failed: whether what it held may have been committed. MySQL: any
        failure but a deadlock (R1: START REPLICA, SET PASSWORD commit first and then fail); elsewhere: a statement
        that may commit, or a trigger that ended it."""
        if self.dialect is Dialect.MYSQL:
            return error.code != _DEADLOCK
        return may_commit(statement.text, self.dialect) or self._ended_inside(error)

    def _open_after_failure(self, statement: FileStatement, error: StatementError) -> bool | None:
        """Whether a transaction is open after `statement` failed or was cancelled (None: it cannot tell). MySQL's
        adapter reads its status flags again after a server error (R-03a-T9b), never after a cancel (KILL QUERY), where
        they still say what held before the statement: MySQL's own rules decide there."""
        if self.dialect is Dialect.MYSQL:
            if implicit_commit_word(statement.text, self.dialect) is not None:
                return False  # it committed before the statement ran (M8), however the statement ended
            if isinstance(error, StatementCancelled) and calls_a_procedure(statement.text, self.dialect):
                return None  # a procedure stopped midway may have committed or rolled back
        return self._open()

    def _watched(self, statement: FileStatement) -> bool:
        """SQL Server, inside a transaction: a batch that may commit and begin another (EXEC, sp_executesql) is told by
        the transaction's id before and after it (R7)."""
        return self.dialect is Dialect.TSQL and (self.all_or_nothing or self.tx_from is not None) \
            and may_commit(statement.text, self.dialect)

    def _transaction_id(self) -> int | None:
        """CURRENT_TRANSACTION_ID(), or None when it cannot be read (a Cancel still stops the run)."""
        try:
            result = self.ctx.run(_TRANSACTION_ID)
            try:
                rows = result.fetch(1)
            finally:
                self.ctx.close(result)
        except StatementCancelled:
            raise
        except StatementError as exc:
            logger.debug("[run] CURRENT_TRANSACTION_ID() could not be read ({})", exc.code)
            return None
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def _reopened(self, before: int | None) -> bool:
        """Whether a transaction is open after a watched batch, and another one than before it (R7). One that ended
        is the ended paths' to tell."""
        if before is None or self._open() is not True:
            return False
        try:
            after = self._transaction_id()
        except StatementCancelled:
            return False  # the batch has run: a Cancel now stops the run at the next statement
        return after is not None and after != before

    def _track_transaction(self, statement: FileStatement, rows_before: int, reopened: bool) -> None:
        """Statement by statement, after a statement that ran: where the file's own open transaction began (pre-flight
        H2). No round trip (SQL Server: the state its next statement reads anyway)."""
        open_now = self._open()
        if open_now is False:
            self.tx_from = None  # the file committed or rolled back its own transaction: its own doing
            return
        if open_now is None:
            return
        if self.tx_from is None:
            self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done - 1, rows_before
        elif reopened:  # the batch ended the file's transaction and began another (R7)
            self.warnings.append(self._may_have_ended(self.tx_from, statement))
            self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done, self.rows
        elif ends_the_transaction(statement.text, self.dialect):  # COMMIT AND CHAIN: the one before is over (R5)
            self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done, self.rows

    def _track_after_failure(self, statement: FileStatement, error: StatementError, rows_before: int,
                             reopened: bool) -> None:
        """Skip and continue, after a statement that failed: the file's transaction may have ended with it (a deadlock,
        XACT_ABORT, MySQL's commit around DDL) or begun in it (a T-SQL batch's BEGIN TRAN before its error)."""
        open_now = self._open_after_failure(statement, error)
        if open_now:
            if self.tx_from is None:
                self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done, rows_before
            elif reopened:
                self.warnings.append(self._may_have_ended(self.tx_from, statement))
                self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done, rows_before
            return
        if self.tx_from is None:
            return
        if open_now is None:  # left to the end of the run, which reads the state again
            self.warnings.append(self._may_have_ended(self.tx_from, statement))
            return
        tx, self.tx_from = self.tx_from, None
        self.warnings += self._ended_in(tx, statement, error)

    def _ended_in(self, tx: FileStatement, failed: FileStatement, error: StatementError | None) -> list[str]:
        """The file's transaction from `tx` was gone after `failed` failed: committed before it (MySQL), maybe
        committed by it, or rolled back by the server (its statements are then not applied)."""
        if implicit_commit_word(failed.text, self.dialect) is not None:
            return [f"Line {failed.line:,} committed the file's transaction from line {tx.line:,} before it failed "
                    "(MySQL commits around DDL)"]
        if error is None or self._gone_unknown(failed, error):
            return [self._may_have_ended(tx, failed)]
        self.done, self.rows = self.done_before_tx, self.rows_before_tx
        return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) ended when line "
                f"{failed.line:,} failed: the server rolled it back, so the statements from there on are not applied; "
                f"run again from statement {tx.index:,}"]

    @staticmethod
    def _may_have_ended(tx: FileStatement, failed: FileStatement) -> str:
        return (f"Line {failed.line:,} may have committed or rolled back the file's transaction from line {tx.line:,} "
                f"(statement {tx.index:,}): the statements from there on may or may not be applied")

    def _settle_file_transaction(self, failed: FileStatement | None = None,
                                 error: StatementError | None = None) -> list[str]:
        """Statement by statement, once the run stops (at `failed`, when a statement failed there) or reads the file to
        its end: a transaction still open is rolled back, never committed on the file's behalf (S14), and the
        statements it held are not counted as applied (H2), unless a table could not roll back (R3)."""
        tx, self.tx_from = self.tx_from, None
        open_now = self._open_after_failure(failed, error) if failed is not None and error is not None else self._open()
        if open_now is False:
            return [] if tx is None or failed is None else self._ended_in(tx, failed, error)
        kept = self._rollback()  # open, or it cannot tell: never left behind
        if tx is None:
            if open_now is None:
                return []
            if kept:
                return ["The file left a transaction open: it was rolled back"]
            return ["The file left a transaction open: it was rolled back, so the statements in it are not applied"]
        if open_now is None:
            return [self._may_have_ended(tx, failed)] if failed is not None else []
        if kept:  # what stayed is not known statement by statement: the counts stay, the outcome is unknown
            return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) was still open: it was "
                    "rolled back"]
        self.done, self.rows = self.done_before_tx, self.rows_before_tx
        return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) was still open: it was rolled "
                f"back, so the statements from there on are not applied; run again from statement {tx.index:,}"]

    # ---- the outcome ----

    def _report(self, outcome: JobOutcomeKind, error: StatementError | None = None, *, cancelled: bool = False,
                stopped: FileStatement | None = None, warnings: list[str] | None = None) -> JobReport:
        said = [*self.warnings, *(warnings or [])]
        if self.kept and outcome != "not_started":  # R3: a ROLLBACK left some changes, whatever the run said
            outcome = "unknown"
            said.append(KEPT)
        if self.settings_sent:
            said.append(f"{self.settings_sent:,} session settings before statement {self.command.from_statement:,} "
                        "were run again (SET, USE, PRAGMA); the other statements before it were skipped")
        at = StoppedAt(line=stopped.line, statement=stopped.index) if stopped is not None else None
        return JobReport(
            self.ctx.outcome(outcome, cancelled=cancelled, rows=self.rows, bytes=self.bytes, statements=self.done,
                             path=self.command.path, errors=self.errors, error_count=self.error_count,
                             stopped_at=at, warnings=said),
            error, schema_changed=self.ctx.sent,
        )

    def _stopped(self, stop: _Stop) -> JobReport:
        error, statement = stop.error, stop.statement
        cancelled = isinstance(error, StatementCancelled)
        if statement is None:  # the session's setup or BEGIN failed, or a Cancel while skipping: none of the file ran
            self._rollback()
            return self._report("not_started", error, cancelled=cancelled)
        if self.all_or_nothing:
            outcome, error = self._unwound(stop)
            return self._report(outcome, error, cancelled=cancelled, stopped=statement)
        if stop.lost:  # the link died while this statement ran: it may have taken effect
            return self._report("unknown", error, stopped=statement, warnings=self._lost_in_file_transaction(statement))
        warnings = []
        if stop.reopened and self.tx_from is not None:  # the file's transaction ended inside it, another began (R7)
            warnings.append(self._may_have_ended(self.tx_from, statement))
            self.tx_from, self.done_before_tx, self.rows_before_tx = statement, self.done, self.rows
        if stop.sent and self.tx_from is None and runs_several(statement.text, self.dialect):
            warnings.append(f"Line {statement.line:,} runs several statements (a batch or a procedure), each taking "
                            "effect on its own: those that ran before it stopped may be applied")
        warnings += self._settle_file_transaction(statement if stop.sent else None, error)
        outcome: JobOutcomeKind = "stopped"
        if stop.sent and self._ended_inside(error):  # a trigger committed inside it: it may have taken effect (R2)
            outcome = "unknown"
            error = _reworded(error, f"{error.message}\nA trigger ended the transaction inside line {statement.line:,}: "
                                     "what it did may be applied")
        self._unlock_tables()
        return self._report(outcome, error, cancelled=cancelled, stopped=statement, warnings=warnings)

    def _unwound(self, stop: _Stop) -> tuple[JobOutcomeKind, StatementError]:
        """All or nothing, stopped before the COMMIT: rolled back, unless the statement that stopped it may have
        committed while the transaction is gone (pre-flight H1, R1, R2), or ended it and began another (R7)."""
        error, statement = stop.error, stop.statement
        assert statement is not None
        line = statement.line
        if stop.outcome is not None:  # the transaction ended after a statement that ran (partial or unknown)
            self._rollback()  # nothing after that statement is open, but never leave one behind
            return stop.outcome, error
        if stop.lost:  # the server rolls back what the connection had open as it ends (no ROLLBACK can reach it)
            if may_commit(statement.text, self.dialect):
                return "unknown", _reworded(error, f"{error.message}\nThe connection was lost while line {line:,} "
                                                   "ran, which may commit: what ran before it may be applied")
            return "rolled_back", error
        if stop.reopened:
            self._rollback()
            return "unknown", _reworded(error, f"{error.message}\nThe transaction ended and another began inside line "
                                               f"{line:,}: what ran before it may or may not be applied")
        if stop.sent:
            if self._ended_inside(error):
                self._rollback()
                return "unknown", _reworded(error, f"{error.message}\nA trigger ended the transaction inside line "
                                                   f"{line:,}: what ran before it may be applied")
            open_now = self._open_after_failure(statement, error)
            if open_now is not True and (open_now is None or self._gone_unknown(statement, error)):
                self._rollback()
                why = ("The transaction had ended when line {:,} failed" if open_now is False
                       else "Line {:,} may have ended the transaction (a procedure stopped midway)").format(line)
                return "unknown", _reworded(error, f"{error.message}\n{why}: what ran before it may be applied")
        self._rollback()  # a transaction the server ended itself (a deadlock, XACT_ABORT) has nothing left to undo
        return "rolled_back", error

    def _lost_in_file_transaction(self, statement: FileStatement) -> list[str]:
        """Statement by statement, the link died while `statement` ran inside the file's own transaction: the server
        rolls that back as the connection ends, unless the statement was what committed it."""
        tx, self.tx_from = self.tx_from, None
        if tx is None:
            return []
        if may_commit(statement.text, self.dialect):
            return [self._may_have_ended(tx, statement)]
        self.done, self.rows = self.done_before_tx, self.rows_before_tx
        return [f"The connection was lost inside the file's transaction from line {tx.line:,} (statement "
                f"{tx.index:,}): the server rolls it back, so the statements from there on are not applied; run again "
                f"from statement {tx.index:,}"]

    def _commit(self) -> JobReport:
        """COMMIT, once nothing stopped the run (⑤ §3.5 step 7): no Cancel reaches it, and an aborted, vanished or
        refused transaction is rolled back (or unknown when the answer to COMMIT was lost)."""
        ctx, frame = self.ctx, transaction_frame(self.ctx.dialect)
        ctx.progress.stage = "commit"
        ctx.tick(force=True)
        cancelled = ctx.stop_cancels()
        connection = ctx.connection
        try:
            if cancelled:
                self._rollback()
                return self._report("rolled_back", StatementCancelled(), cancelled=True)
            if connection.transaction_failed():
                self._rollback()
                return self._report("rolled_back", StatementError(ROLLED_BACK, code="transaction_failed"))
            if not connection.in_transaction():  # each statement was checked: nothing known ended it
                return self._report("unknown", StatementError(
                    "The transaction ended before the COMMIT: what ran in it may or may not be applied",
                    code=TRANSACTION_ENDED))
        except StatementError as exc:
            self._rollback()
            return self._report("rolled_back", exc)
        try:
            ctx.drain(ctx.run(frame.commit, self.options))
        except StatementError as exc:
            if exc.code == CONNECTION_LOST:
                return self._report("unknown", exc)
            self._rollback()  # SQLite keeps the transaction after a busy COMMIT (⑤ A14)
            return self._report("rolled_back", StatementError(f"{exc.message}\n{ROLLED_BACK}", code=exc.code))
        try:
            left_open = connection.in_transaction()
        except StatementError as exc:
            return self._report("unknown", exc)
        if left_open:  # nested @@TRANCOUNT, or MySQL completion_type=CHAIN: what COMMIT did cannot be told (⑤ G11)
            self._rollback()
            return self._report("unknown", StatementError(
                "COMMIT returned but a transaction is still open: it was rolled back; what was applied is unknown",
                code="commit_unconfirmed"))
        return self._report("completed")

    def _statements_done(self) -> JobReport:
        """Statement by statement, every statement read: a transaction the file left open is rolled back, as other
        clients do at the end of a file (said in the outcome, never committed on the file's behalf)."""
        warnings = []
        if self.read < self.command.from_statement:
            warnings.append(f"The file has {self.read:,} statements: none from statement "
                            f"{self.command.from_statement:,} on was run")
        warnings += self._settle_file_transaction()
        return self._report("completed", warnings=warnings)

    def _rollback(self) -> bool:
        """End the run's transaction once it stopped; no Cancel may reach the ROLLBACK. A connection that is gone
        has none to end (the server rolled it back). Whether the ROLLBACK left some tables' changes (MySQL: warning
        1196, a non-transactional table such as MyISAM; R3)."""
        self.ctx.stop_cancels()
        try:
            self.ctx.connection.rollback()
        except StatementError as exc:
            logger.info("[run] the rollback after a stopped run failed ({})", exc.code)
            if self._open():  # still there and still open: the server ends it when the job's session closes
                self.ctx.message(MessageSeverity.WARNING, JOB_ROLLBACK_FAILED)
            return False
        if self.dialect is Dialect.MYSQL and self._rollback_kept_changes():
            self.kept = True
            return True
        return False

    def _rollback_kept_changes(self) -> bool:
        """MySQL: the warnings of the ROLLBACK just sent hold 1196."""
        try:
            result = self.ctx.run(_SHOW_WARNINGS)
            try:
                rows = result.fetch(MAX_JOB_ERRORS)
            finally:
                self.ctx.close(result)
        except StatementError as exc:
            logger.info("[run] the warnings of the rollback could not be read ({})", exc.code)
            return False
        return any(len(row) > 1 and str(row[1]) == _NOT_ROLLED_BACK for row in rows)

    def _unlock_tables(self) -> None:
        """MySQL, statement by statement, stopped: the file's LOCK TABLES … WRITE would hold its tables until the
        session closes (R9). Sent after any ROLLBACK: UNLOCK TABLES commits a transaction that LOCK TABLES is part of."""
        if self.dialect is not Dialect.MYSQL:
            return
        self.ctx.stop_cancels()
        try:
            self.ctx.close(self.ctx.run(_UNLOCK_TABLES))
        except StatementError as exc:
            logger.info("[run] UNLOCK TABLES after a stopped run failed ({})", exc.code)
