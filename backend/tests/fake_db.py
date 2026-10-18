"""In-memory stand-ins for a database connection, used to test worker/manager behaviour deterministically."""

import threading
from typing import Any

from dbaguy_backend.adapters.base import (
    DatabaseConnection,
    RunOptions,
    StatementCancelled,
    StatementError,
    StatementResult,
    transaction_open_error,
)
from dbaguy_backend.domain.execution import ColumnCategory, ColumnMeta, ServerMessage
from dbaguy_backend.domain.metadata import ObjectDetail, ObjectNode, PathSegment, ServerInfo
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.domain.schema import SchemaSnapshot


class FakeResult(StatementResult):
    def __init__(self, rows: int) -> None:
        self._rows = list(range(rows))
        self.closed = False

    def columns(self) -> list[ColumnMeta]:
        return [ColumnMeta(name="n", type_name="int", category=ColumnCategory.NUMBER)]

    def fetch(self, n: int) -> list[list[Any]]:
        taken, self._rows = self._rows[:n], self._rows[n:]
        return [[value] for value in taken]

    def affected_rows(self) -> int | None:
        return None

    def next_result(self) -> bool:
        return False

    def take_messages(self) -> list[ServerMessage]:
        return []

    def close(self) -> None:
        self.closed = True


class FakeConnection(DatabaseConnection):
    """`run()` words: `rows N` returns N rows; `block` waits until cancel() (then raises StatementCancelled); `hold`
    waits for a forced cancel or an abort (a statement a plain cancel cannot stop);
    `fail MSG` raises StatementError (and aborts an open transaction, like PostgreSQL); `begin` opens a transaction;
    anything else returns an empty result. In manual-commit mode (`auto` False) every statement opens a transaction
    first, like the real adapters."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.options: list[RunOptions | None] = []  # what each run() received, in order
        self.cancel_calls = 0
        self.interruptions: list[str] = []  # "cancel", "force", "abort", in order
        self._forced = threading.Event()
        self.closed = False
        self.blocked = threading.Event()  # set while a "block" statement is waiting
        self._unblock = threading.Event()
        self.tx = False
        self.auto = True
        self.commits = 0
        self.rollbacks = 0
        self.failed = False  # the open transaction is aborted (set by `fail` inside a transaction)

    def server_info(self) -> ServerInfo:
        return ServerInfo(engine=Engine.SQLITE, version="fake")

    def current_database(self) -> str | None:
        return None

    def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
        raise NotImplementedError

    def object_detail(self, path: list[PathSegment]) -> ObjectDetail:
        raise NotImplementedError

    def schema_snapshot(self) -> SchemaSnapshot:
        raise NotImplementedError

    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        """`rows N` → N rows; `block` → waits for cancel(); `fail MSG` → StatementError; `begin` opens a
        transaction; anything else → an empty result."""
        self.runs.append(sql)
        self.options.append(options)
        if not self.auto:
            self.tx = True  # manual mode: the adapters BEGIN before the statement, even a SELECT that then pauses
        word, _, arg = sql.partition(" ")
        if word == "hold":
            self.blocked.set()
            assert self._forced.wait(10), "hold was never forced"
            self._forced.clear()
            raise StatementError("Connection to the server was lost", code="connection_lost")
        if word == "block":
            self.blocked.set()
            assert self._unblock.wait(10), "block was never cancelled"
            self._unblock.clear()  # one fake connection can serve several `block` statements in a test
            raise StatementCancelled()
        if word == "fail":
            self.failed = self.tx
            raise StatementError(arg or "boom", code="42000")
        if word == "begin":
            self.tx = True
        return FakeResult(int(arg) if word == "rows" else 0)

    def in_transaction(self) -> bool:
        return self.tx

    def transaction_failed(self) -> bool:
        return self.tx and self.failed

    def autocommit(self) -> bool:
        return self.auto

    def set_autocommit(self, enabled: bool) -> None:
        if self.tx and enabled != self.auto:
            raise transaction_open_error()
        self.auto = enabled

    def commit(self) -> None:
        if not self.tx:  # like the real adapters, nothing to do (and nothing counted) without an open transaction
            return
        if self.failed:  # like PostgreSQL: COMMIT of an aborted transaction silently rolls it back
            self.rollback()
            return
        self.commits += 1
        self.tx = False

    def rollback(self) -> None:
        if self.tx:
            self.rollbacks += 1
            self.tx = False
            self.failed = False

    def _interrupt(self, kind: str) -> None:
        self.cancel_calls += 1
        self.interruptions.append(kind)
        self._unblock.set()
        if kind != "cancel":
            self._forced.set()

    def cancel(self) -> None:
        self._interrupt("cancel")

    def force_cancel(self) -> None:
        self._interrupt("force")

    def abort(self) -> None:
        self._interrupt("abort")

    def close(self) -> None:
        self.closed = True


class FakeLease:
    """What ConnectionService.open() returns: a connection plus close()."""

    def __init__(self, connection: DatabaseConnection | None = None) -> None:
        self.connection = connection or FakeConnection()
        self.closed = threading.Event()

    def close(self) -> None:
        self.connection.close()
        self.closed.set()
