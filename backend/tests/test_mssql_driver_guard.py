"""The in-driver count (R-03b-F5, F6) without a database: dbclose() never runs while a call is inside the driver, and
no second thread ever enters it."""

import socket
import threading
import time

import pytest

from dbaguy_backend.adapters.mssql import driver
from dbaguy_backend.adapters.mssql.adapter import MssqlConnection
from dbaguy_backend.adapters.mssql.driver import GuardedConnection


class FakeRaw:
    """pymssql's connection: each call can be held inside "the driver" until released."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.release = threading.Event()
        self.entered = threading.Event()

    def _held(self, name: str) -> None:
        self.log.append(f"enter {name}")
        self.entered.set()
        self.release.wait(5)
        self.log.append(f"leave {name}")

    def execute_query(self, sql: str) -> None:
        self._held("execute_query")

    def cancel(self) -> None:
        self._held("cancel")

    def get_iterator(self, row_format: int):
        def rows():
            yield (1,)
            self._held("row 2")
            yield (2,)
        return rows()

    def set_msghandler(self, handler) -> None:
        self.log.append("set_msghandler")

    def close(self) -> None:
        self.log.append("close")


def in_thread(fn) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("call", ["execute_query", "cancel", "row read"])
def test_close_waits_for_a_call_inside_the_driver_then_frees_it(call):
    raw = FakeRaw()
    conn = GuardedConnection(raw)
    work = {
        "execute_query": lambda: conn.execute_query("SELECT 1"),
        "cancel": conn.cancel,  # the worker's own dbcancel, reading to the acknowledgement
        "row read": lambda: [row for _, row in zip(range(2), conn.get_iterator(driver.ROW))],  # blocks in row 2
    }[call]
    worker = in_thread(work)
    assert raw.entered.wait(2) and conn.busy
    closed: list[bool] = []
    closer = in_thread(lambda: closed.append(conn.close(wait_s=3)))
    time.sleep(0.2)
    assert "close" not in raw.log and not closed  # never freed under the call
    raw.release.set()
    worker.join(2)
    closer.join(2)
    assert closed == [True] and raw.log[-1] == "close" and raw.log.count("close") == 1
    assert raw.log.index("close") > max(i for i, entry in enumerate(raw.log) if entry.startswith("leave"))


def test_a_call_that_never_leaves_leaks_the_connection_instead_of_freeing_it():
    raw = FakeRaw()
    conn = GuardedConnection(raw)
    worker = in_thread(lambda: conn.execute_query("WAITFOR DELAY '01:00:00'"))
    assert raw.entered.wait(2)
    assert conn.close(wait_s=0.2) is False and "close" not in raw.log  # leaked, never freed
    with pytest.raises(driver.DriverError):
        conn.execute_query("SELECT 1")  # nothing enters once closing began
    assert raw.log == ["enter execute_query"]
    raw.release.set()
    worker.join(2)
    assert conn.close() is True and raw.log.count("close") == 1  # the call left: now it can be freed
    assert conn.close() is True and raw.log.count("close") == 1  # idempotent


def test_a_second_thread_never_reaches_the_driver_while_another_is_inside_not_even_to_cancel():  # R-03b-F6
    raw = FakeRaw()
    conn = GuardedConnection(raw)
    worker = in_thread(lambda: conn.execute_query("SELECT 1"))
    assert raw.entered.wait(2)
    with pytest.raises(driver.DriverError):
        conn.execute_query("SELECT 2")  # two threads in FreeTDS for one connection: refused before the driver
    with pytest.raises(driver.DriverError):
        next(conn.get_iterator(driver.ROW))
    with pytest.raises(driver.DriverError):
        conn.cancel()  # a dbcancel() from another thread frees what the worker is still converting: refused too
    assert raw.log == ["enter execute_query"]
    assert not hasattr(conn, "cancel_across_threads")
    raw.release.set()
    worker.join(2)
    conn.execute_query("SELECT 3")  # once it left, another thread may use the connection
    assert not conn.busy


def test_closing_an_mssql_connection_under_a_read_on_another_thread_drops_the_socket_first():
    """A reader blocked on the socket (as FreeTDS is) leaves the driver once the socket is shut down; only then is the
    connection freed. In the app the worker closes its own connection, so this is the guard's structural case."""
    mine, peer = socket.socketpair()

    class BlockedOnSocket(FakeRaw):
        def execute_query(self, sql: str) -> None:
            self.log.append("enter execute_query")
            self.entered.set()
            mine.recv(1)  # returns b"" once the socket is shut down
            self.log.append("leave execute_query")

    raw = BlockedOnSocket()
    connection = MssqlConnection(GuardedConnection(raw), spid=7, database="dbaguy", read_only=False,
                                 state=(0, 0, False), socket_fd=mine.fileno())
    reader = in_thread(lambda: connection._conn.execute_query("SELECT 1"))
    assert raw.entered.wait(2)
    connection.close()
    reader.join(2)
    assert raw.log[-2:] == ["leave execute_query", "close"]
    peer.settimeout(1)
    assert peer.recv(1) == b""  # the socket was shut down
    mine.close()
    peer.close()
