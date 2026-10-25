"""The pipe between a copy's source and its sink (spec ⑥ R-06-8, 06c Task 3): bounded, ordered, and each side stops on
its own Cancel or on the other side's end. Pure: two threads, no database."""

import threading
import time

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.sessions.jobs.pipe import PIPE_CHUNKS, PipeClosed, RowPipe, SourceFailed


def never() -> None:
    return None


def test_the_rows_arrive_in_order_and_the_end_says_so() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.put([[2], [3]], never)
    pipe.finish()
    assert [pipe.take(never), pipe.take(never), pipe.take(never)] == [[[1]], [[2], [3]], None]


def test_a_full_pipe_holds_the_source_back_until_the_sink_takes() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    for n in range(PIPE_CHUNKS):
        pipe.put([[n]], never)
    put = threading.Event()

    def source() -> None:
        pipe.put([[99]], never)
        put.set()

    thread = threading.Thread(target=source)
    thread.start()
    assert not put.wait(0.2)  # the third chunk waits: the pipe holds PIPE_CHUNKS
    assert pipe.take(never) == [[0]]
    assert put.wait(2)
    thread.join(2)


def test_the_sources_error_comes_after_the_rows_put_before_it() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.fail(StatementError("division by zero", code="22012"))
    assert pipe.take(never) == [[1]]
    with pytest.raises(SourceFailed) as failed:
        pipe.take(never)
    assert (failed.value.code, failed.value.message) == ("source_failed", "Reading public.t failed: division by zero")
    assert failed.value.cause.code == "22012"


def test_a_sink_that_stops_stops_the_source_at_its_next_chunk() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.close()
    with pytest.raises(PipeClosed):
        pipe.put([[2]], never)


def test_each_side_waiting_on_the_other_sees_its_own_cancel() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    asked = threading.Event()

    def cancel() -> None:
        if asked.is_set():
            raise StatementCancelled()

    threading.Timer(0.1, asked.set).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        pipe.take(cancel)  # empty: the sink waits, and its Cancel ends the wait
    assert time.monotonic() - started < 2
    for n in range(PIPE_CHUNKS):
        pipe.put([[n]], never)
    with pytest.raises(StatementCancelled):
        pipe.put([[9]], cancel)  # full: the source waits, and its Cancel ends the wait
