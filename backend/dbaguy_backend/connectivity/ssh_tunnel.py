import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from loguru import logger

from ..adapters.base import Endpoint
from ..domain.errors import TunnelFailed
from ..domain.profiles import SshConfig

_ASKPASS = "#!/bin/sh\nprintf '%s\\n' \"$DBAGUY_ASKPASS_SECRET\"\n"


def write_askpass_script(directory: Path) -> Path:
    """ssh calls SSH_ASKPASS for the password; the secret only travels in the child's environment."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "askpass.sh"
    # Atomic replace: concurrent first starts must never let ssh run a half-written script.
    temp = directory / f".askpass.{os.getpid()}.{threading.get_ident()}.tmp"
    temp.write_text(_ASKPASS)
    temp.chmod(0o700)
    temp.replace(path)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_accepts(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class _TunnelKey:
    """Everything a tunnel depends on: a tunnel is only shared by callers that would start the very same one."""

    profile_id: UUID
    ssh: str  # the whole SshConfig, as canonical JSON
    password: str  # keyed fingerprint of the SSH password ("" without one); the password itself is never kept
    target_host: str
    target_port: int
    nonce: str = ""  # set for a tunnel nobody else may share (connection tests)


_WAIT_SLACK = 5.0  # extra time a waiter gives the caller that is starting the tunnel (it may also have to stop ssh)
_MAX_ATTEMPTS = 3


@dataclass(eq=False)  # tunnels are compared by identity: a stale holder must never affect a replacement
class _Tunnel:
    local_port: int = 0
    process: subprocess.Popen | None = None
    refcount: int = 1
    stopped: bool = False
    error: TunnelFailed | None = None
    ready: threading.Event = field(default_factory=threading.Event)  # set once starting succeeded or failed
    drain: threading.Thread | None = None
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=50))

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def is_dead(self) -> bool:
        """Finished starting but unusable (start failed or ssh exited since)."""
        return self.ready.is_set() and (self.error is not None or not self.is_running())


class TunnelLease:
    """One reference to a shared tunnel. release() is idempotent and only ever affects the tunnel it was taken from."""

    def __init__(self, endpoint: Endpoint, release: Callable[[], None]) -> None:
        self.endpoint = endpoint
        self._release = release
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()


class SshTunnelManager:
    """Local port forwards through the system `ssh` (honours ~/.ssh/config, agent, ProxyJump).

    The lock only guards the bookkeeping: starting ssh happens outside it, behind a per-key placeholder that other
    callers of the same key wait on, so a slow handshake never blocks other profiles, releases or close_all.
    """

    def __init__(
        self,
        askpass_path: Path,
        ready_timeout: float = 10.0,
        extra_options: list[str] | None = None,
        ssh_binary: str = "ssh",
    ) -> None:
        self._askpass = askpass_path
        self._ready_timeout = ready_timeout
        self._extra = list(extra_options or [])
        self._ssh = ssh_binary
        self._tunnels: dict[_TunnelKey, _Tunnel] = {}
        self._lock = threading.Lock()
        self._fingerprint_key = secrets.token_bytes(32)  # per process: fingerprints are useless outside it

    def lease(
        self,
        profile_id: UUID,
        ssh: SshConfig,
        target_host: str,
        target_port: int,
        password: str | None,
        shared: bool = True,
    ) -> TunnelLease:
        """Take a reference whose release() is bound to this very tunnel. `shared=False` always starts a new one."""
        key = self._key(profile_id, ssh, target_host, target_port, password, shared)
        tunnel = self._acquire(key, ssh, password)
        return TunnelLease(self._endpoint(tunnel, target_host), lambda: self._release(key, tunnel))

    def acquire(
        self, profile_id: UUID, ssh: SshConfig, target_host: str, target_port: int, password: str | None
    ) -> Endpoint:
        key = self._key(profile_id, ssh, target_host, target_port, password)
        return self._endpoint(self._acquire(key, ssh, password), target_host)

    def release(
        self, profile_id: UUID, ssh: SshConfig, target_host: str, target_port: int, password: str | None
    ) -> None:
        """Release one reference of the tunnel currently registered for these settings."""
        self._release(self._key(profile_id, ssh, target_host, target_port, password))

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tunnels.values() if t.ready.is_set() and t.error is None and t.is_running())

    def close_all(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tunnel in tunnels:
            try:
                self._stop(tunnel)
            except Exception:
                logger.exception("Failed to stop an SSH tunnel on port {}", tunnel.local_port)

    def _key(
        self,
        profile_id: UUID,
        ssh: SshConfig,
        target_host: str,
        target_port: int,
        password: str | None,
        shared: bool = True,
    ) -> _TunnelKey:
        fingerprint = (
            hmac.new(self._fingerprint_key, password.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
            if password
            else ""
        )
        return _TunnelKey(
            profile_id=profile_id,
            ssh=ssh.model_dump_json(),
            password=fingerprint,
            target_host=target_host,
            target_port=target_port,
            nonce="" if shared else uuid4().hex,
        )

    @staticmethod
    def _endpoint(tunnel: _Tunnel, target_host: str) -> Endpoint:
        return Endpoint(host="127.0.0.1", port=tunnel.local_port, tls_server_name=target_host)

    def _acquire(self, key: _TunnelKey, ssh: SshConfig, password: str | None) -> _Tunnel:
        for _ in range(_MAX_ATTEMPTS):
            with self._lock:
                tunnel = self._tunnels.get(key)
                starter = tunnel is None or tunnel.is_dead()
                if starter:
                    tunnel = self._tunnels[key] = _Tunnel()  # reserve the key; replaces a dead tunnel
                else:
                    tunnel.refcount += 1
            if starter:
                self._run_start(key, tunnel, ssh, password)
                return tunnel
            if not tunnel.ready.wait(self._ready_timeout + _WAIT_SLACK):
                self._release(key, tunnel)
                raise TunnelFailed(f"Timed out waiting for the SSH tunnel to {ssh.host}")
            if tunnel.error is not None:
                raise TunnelFailed(tunnel.error.message)  # the starter already removed the reservation
            if not tunnel.is_dead():
                return tunnel
            self._release(key, tunnel)  # ssh exited right after the start: take over and start a fresh one
        raise TunnelFailed(f"SSH tunnel to {ssh.host} keeps dropping")

    def _release(self, key: _TunnelKey, tunnel: _Tunnel | None = None) -> None:
        """Drop one reference. With `tunnel` given, a tunnel that was replaced meanwhile is left alone."""
        with self._lock:
            current = self._tunnels.get(key)
            if current is None or (tunnel is not None and current is not tunnel):
                return
            current.refcount -= 1
            if current.refcount > 0:
                return
            del self._tunnels[key]
        self._stop(current)
        logger.info("Closed SSH tunnel to {}:{}", key.target_host, key.target_port)

    def _run_start(self, key: _TunnelKey, tunnel: _Tunnel, ssh: SshConfig, password: str | None) -> None:
        """Start ssh for a reserved tunnel (outside the lock), then wake everyone waiting on it."""
        try:
            self._start(tunnel, ssh, key.target_host, key.target_port, password)
        except BaseException as exc:
            tunnel.error = exc if isinstance(exc, TunnelFailed) else TunnelFailed(f"SSH tunnel failed: {exc}")
            with self._lock:
                if self._tunnels.get(key) is tunnel:
                    del self._tunnels[key]
            self._stop(tunnel)
            tunnel.ready.set()
            raise
        tunnel.ready.set()

    def _command(self, ssh: SshConfig, local_port: int, target_host: str, target_port: int) -> list[str]:
        cmd = [
            self._ssh, *self._extra,  # extra options first: ssh uses the first value it sees
            "-N", "-L", f"127.0.0.1:{local_port}:{target_host}:{target_port}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if ssh.port:
            cmd += ["-p", str(ssh.port)]
        if ssh.user:
            cmd += ["-l", ssh.user]
        if ssh.identity_file:
            cmd += ["-i", os.path.expanduser(ssh.identity_file), "-o", "IdentitiesOnly=yes"]
        if ssh.auth == "password":
            cmd += [
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1",
            ]
        else:
            cmd += ["-o", "BatchMode=yes"]
        cmd.append(ssh.host)
        return cmd

    def _start(self, tunnel: _Tunnel, ssh: SshConfig, target_host: str, target_port: int, password: str | None) -> None:
        if not ssh.host:
            raise TunnelFailed("SSH host is required")
        env = os.environ.copy()
        if ssh.auth == "password":
            if not password:
                raise TunnelFailed("SSH password is required")
            if not self._askpass.exists():
                write_askpass_script(self._askpass.parent)
            env.update({
                "SSH_ASKPASS": str(self._askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": env.get("DISPLAY", ":0"),
                "DBAGUY_ASKPASS_SECRET": password,
            })
        tunnel.local_port = _free_port()
        logger.info(
            "Opening SSH tunnel via {} to {}:{} (local port {})", ssh.host, target_host, target_port, tunnel.local_port
        )
        process = subprocess.Popen(
            self._command(ssh, tunnel.local_port, target_host, target_port),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, start_new_session=True, text=True,
        )
        with self._lock:
            tunnel.process = process
        tunnel.drain = threading.Thread(target=self._drain, args=(tunnel, process), name="ssh-stderr", daemon=True)
        tunnel.drain.start()
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if tunnel.stopped:  # close_all() ran while we were starting
                raise TunnelFailed("SSH tunnel was closed while it was starting")
            if process.poll() is not None:
                tunnel.drain.join(timeout=1)  # let the drain thread read the last lines
                logger.warning("SSH tunnel to {} failed", ssh.host)
                raise TunnelFailed(f"SSH tunnel failed: {self._error_text(tunnel)}")
            if _port_accepts(tunnel.local_port):
                return
            time.sleep(0.1)
        raise TunnelFailed(f"SSH tunnel to {ssh.host} timed out: {self._error_text(tunnel)}")

    @staticmethod
    def _drain(tunnel: _Tunnel, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            line = line.rstrip()
            if line:
                tunnel.stderr_tail.append(line)
                logger.debug("[ssh] {}", line)

    @staticmethod
    def _error_text(tunnel: _Tunnel) -> str:
        returncode = tunnel.process.returncode if tunnel.process is not None else None
        return "\n".join(tunnel.stderr_tail) or f"ssh exited with code {returncode}"

    def _stop(self, tunnel: _Tunnel) -> None:
        with self._lock:
            tunnel.stopped = True  # a start still in flight notices this and terminates its own process
            process = tunnel.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
