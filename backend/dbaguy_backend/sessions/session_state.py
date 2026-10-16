import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.execution import SessionStateEvent


@dataclass(frozen=True)
class IdleStretch:
    seconds: float
    token: int  # changes with any activity: equal tokens mean the session stayed idle in between


class SessionStateTracker:
    """A worker's last known session state plus the bookkeeping for idle transactions.

    Written by the worker thread, read from any thread. Its lock is a leaf: never held across a driver call, an event
    delivery or another lock, so readers (e.g. `attach` on the event loop) never wait behind a cancel.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: SessionStateEvent | None = None
        self._busy = False  # a request is running (a paused result is not: it only holds the transaction open)
        self._activity = 0
        self._idle_since = time.monotonic()
        self._warned = False

    def activity(self, busy: bool) -> None:
        """A request started or stopped running."""
        with self._lock:
            self._busy = busy
            self._touch()

    def update(
        self, autocommit: bool, in_transaction: bool, failed: bool, began_at: datetime | None
    ) -> SessionStateEvent:
        """Record the state after a request or command; `began_at` is when that request started."""
        with self._lock:
            previous = self._state.transaction_started_at if self._state is not None else None
            started_at = None if not in_transaction else previous or began_at or datetime.now(UTC)
            self._state = SessionStateEvent(
                autocommit=autocommit, in_transaction=in_transaction, transaction_failed=failed,
                transaction_started_at=started_at,
            )
            self._touch()
            return self._state

    def snapshot(self) -> SessionStateEvent | None:
        """The last state with a fresh `idle_seconds`; None before the first update."""
        with self._lock:
            if self._state is None:
                return None
            return self._state.model_copy(update={"idle_seconds": self._idle_seconds()})

    def idle_stretch(self) -> IdleStretch | None:
        """How long the open transaction has been idle; None when there is none or a request is running."""
        with self._lock:
            idle = self._idle_seconds()
            return None if idle is None else IdleStretch(seconds=idle, token=self._activity)

    def claim_warning(self, token: int) -> bool:
        """True once per idle stretch: the caller then sends the idle warning."""
        with self._lock:
            if token != self._activity or self._warned:
                return False
            self._warned = True
            return True

    def unchanged_since(self, token: int) -> bool:
        with self._lock:
            return token == self._activity

    def _touch(self) -> None:
        self._activity += 1
        self._idle_since = time.monotonic()
        self._warned = False

    def _idle_seconds(self) -> float | None:
        if self._state is None or not self._state.in_transaction or self._busy:
            return None
        return time.monotonic() - self._idle_since
