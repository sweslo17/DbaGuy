from uuid import uuid4

import pytest

from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import TunnelFailed
from dbaguy_backend.domain.profiles import SshConfig
from tests.support import BASTION_PORT, PG_PASSWORD, make_pg_profile, requires_bastion

TEST_SSH_OPTIONS = ["-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]


def manager(tmp_path) -> SshTunnelManager:
    return SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)


def bastion(**overrides) -> SshConfig:
    return SshConfig(**{"enabled": True, "host": "127.0.0.1", "port": BASTION_PORT, "user": "dev", "auth": "password", **overrides})


def test_password_required(tmp_path):
    with pytest.raises(TunnelFailed, match="SSH password is required"):
        manager(tmp_path).acquire(uuid4(), bastion(), "postgres", 5432, None)


@requires_bastion
def test_password_tunnel_to_postgres(tmp_path):
    tunnels = manager(tmp_path)
    pid = uuid4()
    endpoint = tunnels.acquire(pid, bastion(), "postgres", 5432, "dev")
    try:
        assert endpoint.host == "127.0.0.1" and endpoint.tls_server_name == "postgres"
        conn = PostgresAdapter().open(make_pg_profile(host="postgres", port=5432), endpoint, PG_PASSWORD, None)
        assert conn.run("SELECT 1").fetch(1) == [[1]]
        conn.close()
        assert tunnels.acquire(pid, bastion(), "postgres", 5432, "dev").port == endpoint.port  # shared, ref-counted
        tunnels.release(pid, bastion(), "postgres", 5432, "dev")
        assert tunnels.active_count() == 1
    finally:
        tunnels.release(pid, bastion(), "postgres", 5432, "dev")
    assert tunnels.active_count() == 0


@requires_bastion
def test_wrong_password_fails_with_ssh_error(tmp_path):
    with pytest.raises(TunnelFailed, match="(?i)permission denied"):
        manager(tmp_path).acquire(uuid4(), bastion(), "postgres", 5432, "wrong")


def test_askpass_script_is_written_lazily_for_password_auth(tmp_path):
    askpass = tmp_path / "bin" / "askpass.sh"
    tunnels = SshTunnelManager(askpass, ssh_binary="/usr/bin/false")  # ssh exits at once: no network needed
    with pytest.raises(TunnelFailed):
        tunnels.acquire(uuid4(), bastion(), "postgres", 5432, "dev")
    assert askpass.exists() and askpass.stat().st_mode & 0o700 == 0o700
