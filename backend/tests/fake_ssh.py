"""A stand-in for the `ssh` binary so tunnel lifecycle tests are deterministic and need no bastion.

The fake parses `-L 127.0.0.1:<port>:<host>:<port>` like ssh does and then behaves according to the target host name:
- `<host>.started` is touched as soon as the process runs (tests wait for it instead of sleeping), and
  `<host>.pid` holds its process id;
- a host starting with "gated" does not listen until the file `<host>.gate` exists;
- a host containing "fail" prints an SSH-looking error and exits 255 instead of listening;
- any other host listens on the local port (connections queue in the backlog) until it is terminated.
"""

import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPT = """#!{python}
import os, socket, sys, time

args = sys.argv[1:]
_, port, host, _ = args[args.index("-L") + 1].split(":")
directory = os.environ["FAKE_SSH_DIR"]
with open(os.path.join(directory, host + ".pid.tmp"), "w") as pid_file:
    pid_file.write(str(os.getpid()))
os.replace(os.path.join(directory, host + ".pid.tmp"), os.path.join(directory, host + ".pid"))
open(os.path.join(directory, host + ".started"), "w").close()
if host.startswith("gated"):
    while not os.path.exists(os.path.join(directory, host + ".gate")):
        time.sleep(0.01)
if "fail" in host:
    sys.stderr.write("fake ssh: Permission denied (publickey,password).\\n")
    sys.stderr.flush()
    sys.exit(255)
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", int(port)))
sock.listen(16)
time.sleep(3600)
"""


class FakeSsh:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.binary = directory / "fake-ssh"
        self.binary.write_text(_SCRIPT.format(python=sys.executable))
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)

    def open_gate(self, host: str) -> None:
        (self.directory / f"{host}.gate").touch()

    def wait_started(self, host: str, timeout: float = 5.0) -> None:
        wait_until(lambda: (self.directory / f"{host}.started").exists(), timeout)

    def pid(self, host: str) -> int:
        self.wait_started(host)
        return int((self.directory / f"{host}.pid").read_text())


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else (a recycled pid)
    return True


def wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll a condition that another thread/process is about to make true (bounded, no fixed sleeps in tests)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def accepts_connections(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def run_in_thread(fn) -> tuple[threading.Thread, list]:
    """Run fn in a thread; the list receives ("ok", value) or ("error", exception)."""
    outcome: list = []

    def target() -> None:
        try:
            outcome.append(("ok", fn()))
        except BaseException as exc:
            outcome.append(("error", exc))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch) -> FakeSsh:
    directory = tmp_path / "fake-ssh"
    directory.mkdir()
    monkeypatch.setenv("FAKE_SSH_DIR", str(directory))
    return FakeSsh(directory)
