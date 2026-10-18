"""CancelControl's decisions (R-03b-T8a/d/e) without a database: a socket pair stands in for the connection's socket
and a fake records the driver's cancel()."""

import socket
import threading
import time

import pytest

from dbaguy_backend.adapters.mssql.cancel import (
    ABORTED_MESSAGE, BUSY_DROPPED_MESSAGE, BUSY_WARNING, GRACE_S, WEDGE_S, CallKind, CancelControl, CancelPending,
)


class FakeDriver:
    def __init__(self) -> None:
        self.cancels: list[int] = []  # the thread of each cancel()

    def cancel(self) -> None:
        self.cancels.append(threading.get_ident())


@pytest.fixture
def unix_pair():
    left, right = socket.socketpair()
    yield left, right
    left.close()
    right.close()


@pytest.fixture
def tcp_pair():
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    yield client, server
    for sock in (client, server, listener):
        sock.close()


def control_on(sock: socket.socket, **options) -> tuple[CancelControl, FakeDriver]:
    driver = FakeDriver()
    control = CancelControl(driver, spid=1, **options)  # type: ignore[arg-type]
    control.note_socket(sock.fileno())
    control.begin_statement()
    return control, driver


def dropped(peer: socket.socket) -> bool:
    peer.settimeout(1)
    return peer.recv(1) == b""  # the other end was shut down


def test_a_cancel_outside_a_statement_is_ignored(unix_pair):
    control, _ = control_on(unix_pair[0])
    control.end_statement()
    control.cancel()
    assert not control.cancel_requested


def test_a_pending_cancel_keeps_the_batch_from_starting(unix_pair):  # R-03b-P5
    control, driver = control_on(unix_pair[0])
    control.cancel()
    with pytest.raises(CancelPending):
        with control.call(CallKind.EXECUTE):
            pass
    with control.call(CallKind.ROWS):  # rows are still read (and stopped by the flag), the worker's own stop runs
        pass
    assert driver.cancels == []


def test_a_second_cancel_while_the_workers_own_cancel_runs_drops_the_connection(unix_pair):
    control, driver = control_on(unix_pair[0])
    control.force_cancel()  # nothing sent yet: an ordinary cancel
    assert control.cancel_requested and control.aborted is None
    with control.call(CallKind.STOP):  # the worker's own dbcancel(), waiting for the server's acknowledgement
        control.force_cancel()
    assert control.aborted == "forced by a second cancel" and dropped(unix_pair[1])
    assert driver.cancels == []  # never a cancel() from this thread


def test_closing_the_session_drops_a_call_in_progress_and_only_flags_otherwise(unix_pair):
    control, _ = control_on(unix_pair[0])
    control.abort()  # no driver call: the flag stops the worker before its next one
    assert control.cancel_requested and control.aborted is None
    with control.call(CallKind.ROWS):
        control.abort()
    assert control.aborted == "forced by closing the session" and dropped(unix_pair[1]) and control.unusable


def test_a_cancel_reaches_a_waiting_worker_across_threads_once_the_socket_is_quiet(tcp_pair):
    control, driver = control_on(tcp_pair[0])
    with control.call(CallKind.NEXT_SET) as call:  # the worker waits for the next result set: the flag cannot reach it
        started = time.monotonic()
        control.cancel()
        while call.across_done_at is None and time.monotonic() - started < 2:
            time.sleep(0.01)
    assert call.attention == "across threads" and len(driver.cancels) == 1
    assert driver.cancels[0] != threading.get_ident() and call.across_sent_at - started >= GRACE_S
    assert control.aborted is None


class Chatter:
    """Keeps data arriving on the connection's socket (a NOCOUNT OFF loop never lets it go quiet)."""

    def __init__(self, pair) -> None:
        self._reader, self._sender = pair
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._send, daemon=True)

    def _send(self) -> None:
        while not self._stop.is_set():
            self._sender.sendall(b"x" * 256)
            time.sleep(0.005)

    def run_until(self, done, limit: float) -> float:
        """Keep reading what arrives (as the worker does) until `done()` or `limit` seconds; returns the time taken."""
        self._thread.start()
        started = time.monotonic()
        while not done() and time.monotonic() - started < limit:
            self._reader.recv(65536)
        self._stop.set()
        return time.monotonic() - started


def test_a_busy_connection_without_a_safe_moment_is_dropped_when_no_transaction_can_be_open(tcp_pair):
    control, driver = control_on(tcp_pair[0], may_hold_transaction=lambda: False)
    with control.call(CallKind.NEXT_SET):
        control.cancel()
        took = Chatter(tcp_pair).run_until(lambda: control.aborted is not None, WEDGE_S + 2)
    assert control.aborted and "no transaction to lose" in control.aborted and driver.cancels == []
    assert control.aborted_message == BUSY_DROPPED_MESSAGE and WEDGE_S <= took < WEDGE_S + 1


def test_a_busy_connection_that_may_hold_a_transaction_is_warned_and_waits_for_a_second_cancel(tcp_pair):  # T8h
    warnings: list[str] = []
    control, driver = control_on(tcp_pair[0], may_hold_transaction=lambda: True, warn=warnings.append)
    with control.call(CallKind.NEXT_SET):
        control.cancel()
        Chatter(tcp_pair).run_until(lambda: False, WEDGE_S + 1)
        assert warnings == [BUSY_WARNING] and control.aborted is None  # told once, never dropped by itself
        control.cancel()  # a timeout after the warning is a first Cancel only
        assert control.aborted is None
        control.force_cancel()  # the user's next Cancel
    assert control.aborted == "forced by a second cancel" and control.aborted_message == ABORTED_MESSAGE
    assert warnings == [BUSY_WARNING] and driver.cancels == []


def test_rows_that_keep_coming_need_no_cancel_across_threads(tcp_pair):
    control, driver = control_on(tcp_pair[0])
    with control.call(CallKind.ROWS) as call:
        control.cancel()
        for _ in range(10):  # the worker reads a row now and then; it sees the flag before the next one
            call.rows += 1
            time.sleep(GRACE_S / 5)
    time.sleep(GRACE_S * 2)
    assert driver.cancels == [] and control.aborted is None


def test_a_message_sends_the_pending_cancel_from_the_workers_thread(unix_pair):
    control, driver = control_on(unix_pair[0])
    with control.call(CallKind.EXECUTE):
        control.on_message()  # no cancel pending: nothing
        assert driver.cancels == []
        control.cancel()
        control.on_message()
        control.on_message()  # one attention per call
    assert driver.cancels == [threading.get_ident()]
