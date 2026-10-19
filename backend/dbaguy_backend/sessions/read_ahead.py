"""When a paused result may be holding the server back (R-04a-T11a/T11b, A23).

A server that streams an answer stalls once DbaGuy stops reading it, and ends a request stalled for too long (Couchbase:
77–86 s). The worker closes a paused result cleanly before that (`paused_result_max_idle_s`), counting from where the
stall began: from where its read-ahead stopped (`ReadAheadState.FULL`), or, for an engine that does not read ahead,
from the first pause."""

from ..adapters.base import ReadAhead, ReadAheadState

# A stall ends once this many times the engine's read-ahead budget of answer bytes has been read since it began, by any
# path and whatever the step's state (B44). A23: on 7.6.12 a 128 KiB read after a stall did not restart the server's
# 77 s clock, about 5.5 MiB read in small steps and 8 MiB at once did; twice the 8 MiB budget leaves room for a setup
# with bigger buffers.
STALL_RESET_FACTOR = 2
# How soon a read-ahead that found nothing on the socket (`ReadAheadState.WAITING`) looks again, when no task is queued.
READ_AHEAD_POLL_S = 0.25


class StallClock:
    """Since when the server of one paused result may have been held back, step by step of its read-ahead.

    `DONE` ends a stall. Having read `reset_bytes` or more since the stall began ends it, in any step state (the server
    has been unblocked, B2; `received` counts every path, a Load more's `fetch()` too, so a `WAITING` step after a
    large Load more ends it as well, B44). Only a `FULL` with no stall in progress starts one. An emptied socket on its
    own (`WAITING`) neither starts nor ends a stall: the server's pending write may still be held (B42). So a result
    that never fills its budget never stalls. `reset_bytes` None (an engine without read-ahead): the stall starts at the
    first `FULL` and never ends, so the idle limit counts from the first pause (R-04a-T11b)."""

    def __init__(self, reset_bytes: int | None) -> None:
        self._reset_bytes = reset_bytes
        self.since: float | None = None  # when the server may have been held back since; None: it is not
        self._received_at_stall = 0  # `ReadAhead.received` when the stall began

    def update(self, step: ReadAhead, now: float) -> None:
        if step.state is ReadAheadState.DONE:
            self.since = None
            return
        if (
            self.since is not None and self._reset_bytes is not None
            and step.received - self._received_at_stall >= self._reset_bytes
        ):
            self.since = None
        if step.state is ReadAheadState.FULL and self.since is None:
            self.since, self._received_at_stall = now, step.received
