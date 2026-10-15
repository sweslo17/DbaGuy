"""Tunnel lifecycle races, exercised with a fake `ssh` (see tests/fake_ssh.py): no bastion needed, no fixed sleeps."""

from uuid import uuid4

import pytest

from dbaguy_backend.adapters.base import DatabaseAdapter, Endpoint
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import TunnelFailed
from dbaguy_backend.domain.profiles import Engine, ServerProfile, SshConfig
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService, ResolvedSecrets
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection
from tests.fake_ssh import accepts_connections, fake_ssh, run_in_thread, wait_until  # noqa: F401  (fixture)

SSH = SshConfig(enabled=True, host="fake-bastion", auth="agent_or_key")


@pytest.fixture
def tunnels(tmp_path, fake_ssh):
    manager = SshTunnelManager(write_askpass_script(tmp_path), ssh_binary=str(fake_ssh.binary))
    yield manager
    manager.close_all()


def finishes(fn, timeout: float = 3.0):
    """Run fn in a thread; (True, outcome) if it returned in time, (False, None) if it is still blocked."""
    thread, outcome = run_in_thread(fn)
    thread.join(timeout)
    return (not thread.is_alive()), (outcome[0] if outcome else None)


class RecordingAdapter(DatabaseAdapter):
    engine = Engine.POSTGRES
    url_schemes = ("postgres",)
    default_port = 5432

    def parse_url(self, url):
        raise NotImplementedError

    def open(self, profile, endpoint: Endpoint | None, password, database):
        return FakeConnection()


def tunnelled_profile() -> ServerProfile:
    return ServerProfile(id=uuid4(), engine="postgres", name="tunnelled", host="target", ssh=SSH)


def test_stale_release_leaves_the_replacement_tunnel_alone(tmp_path, tunnels):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    service = ConnectionService(AdapterRegistry([RecordingAdapter()]), profiles, tunnels)
    profile, secrets = tunnelled_profile(), ResolvedSecrets(password="x")
    first = service.open(profile, secrets=secrets)
    second = service.open(profile, secrets=secrets)  # shares the first tunnel (refcount 2)
    tunnel = tunnels._tunnels[(profile.id, "target", 5432)]
    tunnel.process.kill()
    tunnel.process.wait()
    replacement = service.open(profile, secrets=secrets)  # ssh died: a fresh tunnel replaces the dead one
    assert tunnels.active_count() == 1

    first.close()
    second.close()  # both hold the dead tunnel; neither may touch the replacement
    assert tunnels.active_count() == 1
    assert accepts_connections(tunnels._tunnels[(profile.id, "target", 5432)].local_port)
    first.close()  # releasing twice is harmless as well
    assert tunnels.active_count() == 1

    replacement.close()
    assert tunnels.active_count() == 0


def test_slow_start_does_not_block_other_keys_or_bookkeeping(tunnels, fake_ssh):
    slow, slow_outcome = run_in_thread(lambda: tunnels.acquire(uuid4(), SSH, "gated-slow", 5432, None))
    fake_ssh.wait_started("gated-slow")  # ssh is running but not forwarding yet

    done, outcome = finishes(lambda: (tunnels.acquire(uuid4(), SSH, "quick", 5432, None), tunnels.active_count()))
    assert done, "another key had to wait for the slow ssh handshake"
    assert outcome[1][1] == 1  # only "quick" is up; the starting tunnel is not counted
    assert finishes(lambda: tunnels.release(uuid4(), "unknown", 1))[0]
    assert slow.is_alive()

    fake_ssh.open_gate("gated-slow")
    slow.join(5)
    assert slow_outcome[0][0] == "ok"


def test_callers_of_the_same_key_share_one_start(tunnels, fake_ssh):
    profile_id = uuid4()
    key = (profile_id, "gated-shared", 5432)
    starter, starter_outcome = run_in_thread(lambda: tunnels.lease(profile_id, SSH, "gated-shared", 5432, None))
    fake_ssh.wait_started("gated-shared")
    waiter, waiter_outcome = run_in_thread(lambda: tunnels.lease(profile_id, SSH, "gated-shared", 5432, None))
    wait_until(lambda: tunnels._tunnels[key].refcount == 2)  # the waiter has joined the placeholder

    fake_ssh.open_gate("gated-shared")
    starter.join(5)
    waiter.join(5)
    first, second = starter_outcome[0][1], waiter_outcome[0][1]
    assert first.endpoint.port == second.endpoint.port
    assert tunnels.active_count() == 1
    first.release()
    assert tunnels.active_count() == 1
    second.release()
    assert tunnels.active_count() == 0


def test_failed_start_wakes_waiters_with_the_error_and_frees_the_key(tunnels, fake_ssh):
    profile_id = uuid4()
    key = (profile_id, "gated-fail", 5432)
    starter, starter_outcome = run_in_thread(lambda: tunnels.acquire(profile_id, SSH, "gated-fail", 5432, None))
    fake_ssh.wait_started("gated-fail")
    waiter, waiter_outcome = run_in_thread(lambda: tunnels.acquire(profile_id, SSH, "gated-fail", 5432, None))
    wait_until(lambda: tunnels._tunnels[key].refcount == 2)

    fake_ssh.open_gate("gated-fail")
    starter.join(5)
    waiter.join(5)
    for outcome in (starter_outcome[0], waiter_outcome[0]):
        assert outcome[0] == "error" and isinstance(outcome[1], TunnelFailed)
        assert "Permission denied" in outcome[1].message
    assert tunnels._tunnels == {} and tunnels.active_count() == 0
    with pytest.raises(TunnelFailed, match="Permission denied"):  # the key was free: a fresh start was attempted
        tunnels.acquire(profile_id, SSH, "gated-fail", 5432, None)


def test_close_all_during_a_start_stops_that_ssh(tunnels, fake_ssh):
    starter, outcome = run_in_thread(lambda: tunnels.acquire(uuid4(), SSH, "gated-closing", 5432, None))
    fake_ssh.wait_started("gated-closing")
    process = next(iter(tunnels._tunnels.values())).process

    assert finishes(tunnels.close_all)[0], "close_all had to wait for the starting tunnel"
    starter.join(5)
    assert outcome[0][0] == "error" and "closed while it was starting" in outcome[0][1].message
    assert process.wait(5) is not None
    assert tunnels._tunnels == {}


def test_close_all_continues_after_a_failing_stop(tunnels, monkeypatch):
    tunnels.acquire(uuid4(), SSH, "one", 1, None)
    tunnels.acquire(uuid4(), SSH, "two", 2, None)
    real_stop, stopped = tunnels._stop, []

    def flaky_stop(tunnel):
        stopped.append(tunnel)
        if len(stopped) == 1:
            raise OSError("boom")
        real_stop(tunnel)

    monkeypatch.setattr(tunnels, "_stop", flaky_stop)
    tunnels.close_all()  # must not raise, and must still reach the second tunnel
    assert len(stopped) == 2 and stopped[1].process.poll() is not None
    real_stop(stopped[0])
