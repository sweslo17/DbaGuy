"""Suite-wide safety net: a test that hangs (a lost cancel, a missed wake-up) fails with every thread's stack instead of
stalling the whole run. The slowest test takes about 11 s; the limit leaves room for a slow machine."""

import faulthandler
import signal
import sys

import pytest

TEST_TIMEOUT_S = 120


@pytest.fixture(autouse=True)
def _test_timeout():
    def expired(signum, frame):
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        raise TimeoutError(f"test exceeded {TEST_TIMEOUT_S} s (the stack of every thread is above)")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(TEST_TIMEOUT_S)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
