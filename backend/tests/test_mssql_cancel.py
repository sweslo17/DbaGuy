"""CancelControl's decisions (R-03b-T8a/d/e/h/i, F1, F2) without a database: a socket pair stands in for the
connection's socket and a fake records the driver's cancel()."""

import socket
import threading
import time

import pytest

from dbaguy_backend.adapters.mssql.cancel import (
    ABORTED_MESSAGE, BUSY_DATA_DROPPED_MESSAGE, BUSY_DATA_WARNING, BUSY_DROPPED_MESSAGE, BUSY_WARNING, DOUBLE_CLICK_S,
    GRACE_S, UNDOING_WARNING, WEDGE_S, CallKind, CancelControl, CancelPending,
)


class FakeDriver:
    def __init__(self) -> None:
        self.cancels: list[int] = []  # the thread of each cancel()

    def cancel(self) -> None:
        self.cancels.append(threading.get_ident())

    cancel_across_threads = cancel


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


def test_a_double_click_never_drops_but_a_second_cancel_a_second_later_does(unix_pair):  # R-03b-T8i
    control, driver = control_on(unix_pair[0], may_hold_transaction=lambda: False)  # nothing to lose (R-03b-F1)
    control.force_cancel()  # nothing sent yet: an ordinary cancel
    assert control.cancel_requested and control.aborted is None
    with control.call(CallKind.STOP):  # the worker's own dbcancel(), reading a stream up to the acknowledgement
        time.sleep(0.1)
        control.force_cancel()  # a double click: the cancel that went out is younger than WEDGE_S
        assert control.aborted is None
        time.sleep(WEDGE_S)
        control.force_cancel()
    assert control.aborted == "forced by a second cancel" and dropped(unix_pair[1])
    assert control.aborted_message == ABORTED_MESSAGE and driver.cancels == []  # never a cancel() from this thread


def test_a_second_cancel_while_the_server_undoes_the_statement_warns_first(unix_pair):  # R-03b-F1
    """The worker's own cancel reads up to the acknowledgement, which comes once the server has undone the statement.
    A transaction may be open: the second Cancel warns, the next one (not a double click) drops."""
    warnings: list[str] = []
    control, driver = control_on(unix_pair[0], may_hold_transaction=lambda: True, warn=warnings.append)
    control.cancel()
    with control.call(CallKind.STOP):
        time.sleep(0.1)
        control.force_cancel()  # a double click
        assert control.aborted is None and warnings == []
        time.sleep(WEDGE_S)
        control.force_cancel()
        assert warnings == [UNDOING_WARNING] and control.aborted is None  # told, nothing rolled back
        control.force_cancel()  # straight after the warning: the rest of a double click
        assert warnings == [UNDOING_WARNING] and control.aborted is None
        time.sleep(DOUBLE_CLICK_S)
        control.force_cancel()
    assert control.aborted == "forced by a second cancel" and control.aborted_message == ABORTED_MESSAGE
    assert dropped(unix_pair[1]) and warnings == [UNDOING_WARNING] and driver.cancels == []


def test_closing_the_session_after_the_warning_drops_at_once(unix_pair):  # R-03b-F1: a cancel from a message
    warnings: list[str] = []
    control, driver = control_on(unix_pair[0], may_hold_transaction=lambda: True, warn=warnings.append)
    with control.call(CallKind.EXECUTE):
        control.cancel()
        control.on_message()  # the cancel goes out from the message handler; the acknowledgement does not come
        time.sleep(WEDGE_S)
        control.force_cancel()
        assert warnings == [UNDOING_WARNING] and control.aborted is None
        control.abort()  # the tab closes: no second warning, no wait
    assert control.aborted == "forced by closing the session" and dropped(unix_pair[1]) and len(driver.cancels) == 1


def test_a_second_cancel_soon_after_a_cancel_from_a_message_waits_too(unix_pair):  # R-03b-T8i
    control, driver = control_on(unix_pair[0])
    with control.call(CallKind.EXECUTE):
        control.cancel()
        control.on_message()  # the cancel goes out from the message handler
        control.force_cancel()
        assert control.aborted is None and len(driver.cancels) == 1


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
    """Keeps data arriving on the connection's socket, read as the worker reads it (a NOCOUNT OFF loop never lets it
    go quiet; nor do the bytes of one big value). As a context manager it chatters in the background."""

    def __init__(self, pair) -> None:
        self._reader, self._sender = pair
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._send, daemon=True), threading.Thread(target=self._read, daemon=True)]

    def _send(self) -> None:
        while not self._stop.is_set():
            try:
                self._sender.sendall(b"x" * 256)
            except OSError:  # the connection was dropped
                return
            time.sleep(0.005)

    def _read(self) -> None:
        self._reader.settimeout(0.05)
        while not self._stop.is_set():
            try:
                if self._reader.recv(65536) == b"":
                    return
            except TimeoutError:
                pass

    def __enter__(self) -> "Chatter":
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(1)

    def run_until(self, done, limit: float) -> float:
        """Chatter until `done()` or `limit` seconds; returns the time taken."""
        with self:
            started = time.monotonic()
            while not done() and time.monotonic() - started < limit:
                time.sleep(0.005)
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


def test_a_cancel_right_after_the_warning_is_a_double_click(tcp_pair):  # R-03b-T8i applied to the warning
    warnings: list[str] = []
    control, _ = control_on(tcp_pair[0], may_hold_transaction=lambda: True, warn=warnings.append)
    with control.call(CallKind.NEXT_SET), Chatter(tcp_pair):
        control.cancel()
        started = time.monotonic()
        while not warnings and time.monotonic() - started < WEDGE_S + 1:
            time.sleep(0.005)
        control.force_cancel()  # a click that lands as the warning appears: the user has not read it
        assert warnings == [BUSY_WARNING] and control.aborted is None
        time.sleep(DOUBLE_CLICK_S)
        control.force_cancel()
        assert control.aborted == "forced by a second cancel"


@pytest.mark.parametrize("holds", [False, True])
def test_a_repeated_cancel_during_one_big_value_follows_the_warn_then_force_rule(tcp_pair, holds):  # R-03b-F2
    """The bytes of one big value count as progress, so the canceller never reaches its deadline. A repeated Cancel a
    second after the flag gives up on the row: dropped when nothing can be lost, else warned first."""
    warnings: list[str] = []
    control, driver = control_on(tcp_pair[0], may_hold_transaction=lambda: holds, warn=warnings.append)
    with control.call(CallKind.ROWS), Chatter(tcp_pair):
        control.cancel()
        time.sleep(0.3)
        control.force_cancel()  # a double click
        assert control.aborted is None and warnings == []
        time.sleep(WEDGE_S)
        control.force_cancel()
        if holds:
            assert warnings == [BUSY_DATA_WARNING] and control.aborted is None
            time.sleep(DOUBLE_CLICK_S)
            control.force_cancel()
            assert control.aborted_message == ABORTED_MESSAGE
        else:
            assert control.aborted_message == BUSY_DATA_DROPPED_MESSAGE
        assert control.aborted == "forced by a second cancel"
    assert driver.cancels == [] and warnings == ([BUSY_DATA_WARNING] if holds else [])


def test_the_bytes_of_one_big_row_count_as_progress(tcp_pair):  # 300 MB in one value: no row completes for seconds
    warnings: list[str] = []
    control, driver = control_on(tcp_pair[0], may_hold_transaction=lambda: False, warn=warnings.append)
    with control.call(CallKind.ROWS):  # no row completes, but its bytes keep arriving
        control.cancel()
        Chatter(tcp_pair).run_until(lambda: False, WEDGE_S + 0.5)
        assert control.aborted is None and warnings == [] and driver.cancels == []
    # the row is complete: the worker sees the flag before the next one


@pytest.mark.parametrize("holds, expected", [(False, "dropped"), (True, "warned")])
def test_a_row_read_without_a_safe_moment_is_not_blamed_on_row_counts(unix_pair, holds, expected):
    """Without the socket's byte counter (an AF_UNIX pair here) a stalled row read finds no safe moment: the text
    names the data the server keeps sending, not row counts."""
    warnings: list[str] = []
    control, _ = control_on(unix_pair[0], may_hold_transaction=lambda: holds, warn=warnings.append)
    with control.call(CallKind.ROWS):
        control.cancel()
        started = time.monotonic()
        while control.aborted is None and not warnings and time.monotonic() - started < WEDGE_S + 1:
            time.sleep(0.02)
        if expected == "dropped":
            assert control.aborted_message == BUSY_DATA_DROPPED_MESSAGE and warnings == []
        else:
            assert warnings == [BUSY_DATA_WARNING] and control.aborted is None
            control.abort()


def test_a_failing_transaction_check_counts_as_a_transaction(tcp_pair):
    def broken() -> bool:
        raise RuntimeError("no state")

    warnings: list[str] = []
    control, _ = control_on(tcp_pair[0], may_hold_transaction=broken, warn=warnings.append)
    with control.call(CallKind.NEXT_SET):
        control.cancel()
        Chatter(tcp_pair).run_until(lambda: bool(warnings), WEDGE_S + 1)
        assert warnings == [BUSY_WARNING] and control.aborted is None  # warned, never dropped by itself
        control.abort()


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
