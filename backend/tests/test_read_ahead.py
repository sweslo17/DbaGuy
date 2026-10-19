"""The stall clock of a paused result (R-04a-T11a/T11b, A23): when the server may be held back because DbaGuy stopped
reading, and when that ends."""

from dbaguy_backend.adapters.base import ReadAhead
from dbaguy_backend.adapters.base import ReadAheadState as S
from dbaguy_backend.sessions.read_ahead import StallClock

MIB = 1024 * 1024


def test_the_stall_starts_where_reading_stopped():
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.READING, 1 * MIB), now=10.0)
    assert clock.since is None  # still reading: the server is not held back
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=12.0)
    assert clock.since == 12.0
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=30.0)  # nothing read since: the same stall
    assert clock.since == 12.0


def test_a_small_refill_keeps_the_stall_and_a_large_drain_ends_it():  # A23: 128 KiB does not restart the server's clock
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.READING, 8 * MIB + 128 * 1024), now=20.0)  # a Load more's refill
    clock.update(ReadAhead(S.FULL, 8 * MIB + 128 * 1024), now=20.1)
    assert clock.since == 10.0
    clock.update(ReadAhead(S.READING, 24 * MIB), now=30.0)  # 16 MiB read since the stall began
    assert clock.since is None
    clock.update(ReadAhead(S.FULL, 24 * MIB), now=31.0)
    assert clock.since == 31.0


def test_a_full_step_that_drained_enough_ends_the_old_stall_and_starts_a_new_one():  # B2 applies to FULL too
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.FULL, 24 * MIB), now=30.0)  # 16 MiB read since, and full again: a new stall
    assert clock.since == 30.0


def test_a_new_stall_counts_its_drain_from_where_it_began():  # B2 after a reset: a small refill keeps the new stall
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.READING, 24 * MIB), now=30.0)  # unblocked
    clock.update(ReadAhead(S.FULL, 32 * MIB), now=31.0)  # stopped again
    clock.update(ReadAhead(S.READING, 32 * MIB + 128 * 1024), now=40.0)  # a Load more's refill
    assert clock.since == 31.0


def test_the_end_means_no_stall():
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.DONE, 8 * MIB), now=11.0)
    assert clock.since is None


def test_an_empty_socket_changes_nothing():  # B42: draining the socket is not the server's write completing (A23)
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.WAITING, 1 * MIB), now=5.0)
    assert clock.since is None  # no stall in progress: an empty socket does not start one
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.WAITING, 8 * MIB + 64 * 1024), now=20.0)
    assert clock.since == 10.0  # a stall in progress: an empty socket does not end it
    clock.update(ReadAhead(S.FULL, 8 * MIB + 64 * 1024), now=21.0)
    assert clock.since == 10.0


def test_enough_bytes_drained_end_the_stall_even_on_an_empty_socket():  # B44: a byte fact, not the emptiness signal
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.WAITING, 9 * MIB), now=20.0)  # a small Load more, then an empty socket
    assert clock.since == 10.0
    clock.update(ReadAhead(S.WAITING, 24 * MIB), now=30.0)  # a Load more drained 16 MiB, then an empty socket
    assert clock.since is None
    clock.update(ReadAhead(S.WAITING, 24 * MIB), now=40.0)  # and an empty socket still never starts one
    assert clock.since is None


def test_without_read_ahead_the_stall_starts_at_the_first_pause_and_never_restarts():  # R-04a-T11b
    clock = StallClock(reset_bytes=None)
    clock.update(ReadAhead(S.FULL), now=10.0)
    clock.update(ReadAhead(S.FULL), now=50.0)
    assert clock.since == 10.0
