import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import Endpoint
from ..domain.errors import TunnelFailed
from ..domain.profiles import SshConfig

_ASKPASS = "#!/bin/sh\nprintf '%s\\n' \"$DBAGUY_ASKPASS_SECRET\"\n"


def write_askpass_script(directory: Path) -> Path:
    """ssh calls SSH_ASKPASS for the password; the secret only travels in the child's environment."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "askpass.sh"
    path.write_text(_ASKPASS)
    path.chmod(0o700)
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


@dataclass
class _Tunnel:
    process: subprocess.Popen
    local_port: int
    refcount: int = 1
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=50))


class SshTunnelManager:
    """Local port forwards through the system `ssh` (honours ~/.ssh/config, agent, ProxyJump)."""

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
        self._tunnels: dict[tuple[UUID, str, int], _Tunnel] = {}
        self._lock = threading.Lock()

    def acquire(
        self, profile_id: UUID, ssh: SshConfig, target_host: str, target_port: int, password: str | None
    ) -> Endpoint:
        key = (profile_id, target_host, target_port)
        with self._lock:
            tunnel = self._tunnels.get(key)
            if tunnel is not None and tunnel.process.poll() is None:
                tunnel.refcount += 1
            else:
                tunnel = self._start(ssh, target_host, target_port, password)
                self._tunnels[key] = tunnel
            return Endpoint(host="127.0.0.1", port=tunnel.local_port, tls_server_name=target_host)

    def release(self, profile_id: UUID, target_host: str, target_port: int) -> None:
        key = (profile_id, target_host, target_port)
        with self._lock:
            tunnel = self._tunnels.get(key)
            if tunnel is None:
                return
            tunnel.refcount -= 1
            if tunnel.refcount > 0:
                return
            del self._tunnels[key]
        self._stop(tunnel)
        logger.info("Closed SSH tunnel to {}:{}", target_host, target_port)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tunnels.values() if t.process.poll() is None)

    def close_all(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tunnel in tunnels:
            self._stop(tunnel)

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

    def _start(self, ssh: SshConfig, target_host: str, target_port: int, password: str | None) -> _Tunnel:
        if not ssh.host:
            raise TunnelFailed("SSH host is required")
        env = os.environ.copy()
        if ssh.auth == "password":
            if not password:
                raise TunnelFailed("SSH password is required")
            env.update({
                "SSH_ASKPASS": str(self._askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": env.get("DISPLAY", ":0"),
                "DBAGUY_ASKPASS_SECRET": password,
            })
        local_port = _free_port()
        logger.info("Opening SSH tunnel via {} to {}:{} (local port {})", ssh.host, target_host, target_port, local_port)
        process = subprocess.Popen(
            self._command(ssh, local_port, target_host, target_port),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, start_new_session=True, text=True,
        )
        tunnel = _Tunnel(process=process, local_port=local_port)
        threading.Thread(target=self._drain, args=(tunnel,), name="ssh-stderr", daemon=True).start()
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                time.sleep(0.1)  # let the drain thread read the last lines
                logger.warning("SSH tunnel to {} failed", ssh.host)
                raise TunnelFailed(f"SSH tunnel failed: {self._error_text(tunnel)}")
            if _port_accepts(local_port):
                return tunnel
            time.sleep(0.1)
        self._stop(tunnel)
        raise TunnelFailed(f"SSH tunnel to {ssh.host} timed out: {self._error_text(tunnel)}")

    @staticmethod
    def _drain(tunnel: _Tunnel) -> None:
        assert tunnel.process.stderr is not None
        for line in tunnel.process.stderr:
            line = line.rstrip()
            if line:
                tunnel.stderr_tail.append(line)
                logger.debug("[ssh] {}", line)

    @staticmethod
    def _error_text(tunnel: _Tunnel) -> str:
        return "\n".join(tunnel.stderr_tail) or f"ssh exited with code {tunnel.process.returncode}"

    @staticmethod
    def _stop(tunnel: _Tunnel) -> None:
        if tunnel.process.poll() is None:
            tunnel.process.terminate()
            try:
                tunnel.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                tunnel.process.kill()
