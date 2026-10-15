"""In-memory stand-ins for a database connection, used to test worker/manager behaviour deterministically."""

import threading
from typing import Any

from dbaguy_backend.adapters.base import DatabaseConnection, StatementCancelled, StatementResult
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
    """`run("rows N")` returns N rows; `run("block")` waits until cancel() (then raises StatementCancelled)."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.cancel_calls = 0
        self.closed = False
        self.blocked = threading.Event()  # set while a "block" statement is waiting
        self._unblock = threading.Event()

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

    def run(self, sql: str) -> StatementResult:
        self.runs.append(sql)
        if sql == "block":
            self.blocked.set()
            assert self._unblock.wait(10), "block was never cancelled"
            raise StatementCancelled()
        return FakeResult(int(sql.split()[1]))

    def in_transaction(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._unblock.set()

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
