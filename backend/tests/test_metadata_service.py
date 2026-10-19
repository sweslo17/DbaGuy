import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.domain.errors import QueryFailed, SessionBusy, WriteOutcomeUnknown
from dbaguy_backend.domain.metadata import MetadataRequest, PathSegment
from dbaguy_backend.domain.profiles import ProfileSecretsInput, ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.metadata_service import MetadataService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, requires_pg, scaled

pytestmark = requires_pg


def metadata_service(tmp_path, **options):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({
        "profile": {"engine": "postgres", "name": "pg", "host": PG_HOST, "port": PG_PORT, "database": "dbaguy",
                    "username": PG_USER, "ssl": {"mode": "disable"}},
        "secrets": ProfileSecretsInput(password=PG_PASSWORD).model_dump(),
    }))
    registry = AdapterRegistry([PostgresAdapter()])
    connections = ConnectionService(registry, profiles, SshTunnelManager(write_askpass_script(tmp_path)))
    return MetadataService(registry, connections, profiles, **options), entry.profile.id


@pytest.fixture
def service_and_profile(tmp_path):
    service, pid = metadata_service(tmp_path)
    yield service, pid
    service.close_all()


WRITE_TIMEOUT_S = 0.5


@pytest.fixture
def quick_writes(tmp_path):
    """Writes wait WRITE_TIMEOUT_S for their answer; reads keep the worker's 120 s."""
    service, pid = metadata_service(tmp_path, write_timeout=WRITE_TIMEOUT_S)
    yield service, pid
    service.close_all()


def seg(kind, name):
    return PathSegment(kind=kind, name=name)


def test_routes_each_database_to_its_own_connection(service_and_profile):
    service, pid = service_and_profile
    databases = [n.name for n in service.children(MetadataRequest(profile_id=pid))]
    assert "analytics" in databases
    path = [seg("database", "analytics"), seg("schema", "public"), seg("folder", "tables")]
    assert [n.name for n in service.children(MetadataRequest(profile_id=pid, path=path))] == ["page_views"]


def test_reconnects_after_server_drops_connection(service_and_profile):
    service, pid = service_and_profile
    request = MetadataRequest(profile_id=pid, path=[seg("database", "dbaguy")])
    assert service.children(request)
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                         autocommit=True, application_name="pytest-admin") as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'DbaGuy'")
    assert service.children(request)  # transparently reconnected


def test_without_retry_a_failure_is_raised_once_and_never_sent_again(service_and_profile):  # D-04-9, ruling C26
    service, pid = service_and_profile
    calls = []

    def failing(connection):
        calls.append(connection)
        raise RuntimeError("the answer was lost")

    with pytest.raises(WriteOutcomeUnknown):  # it started: it may have been applied (ruling C42)
        service.with_connection(pid, "dbaguy", failing, retry=False)
    assert len(calls) == 1
    with pytest.raises(QueryFailed):
        service.with_connection(pid, "dbaguy", failing)
    assert len(calls) == 3  # the default reconnects once and runs it again


def test_a_write_given_up_on_while_queued_never_runs(quick_writes):  # ruling C42
    service, pid = quick_writes
    reading, release, wrote = threading.Event(), threading.Event(), []

    def slow_read(connection):
        reading.set()
        release.wait(scaled(30))
        return "read"

    with ThreadPoolExecutor(1) as pool:
        read = pool.submit(service.with_connection, pid, "dbaguy", slow_read)
        try:
            assert reading.wait(scaled(30))
            with pytest.raises(QueryFailed, match="nothing was sent"):
                service.with_connection(pid, "dbaguy", wrote.append, retry=False)
        finally:
            release.set()
        assert read.result(scaled(30)) == "read"  # the read kept its connection
    # Queued behind the abandoned write on the same connection: once it answers, the write's turn has come and gone.
    assert service.with_connection(pid, "dbaguy", lambda connection: "next") == "next"
    assert wrote == []


def test_a_write_that_times_out_after_it_started_is_unknown_and_runs_once(quick_writes):  # ruling C42
    service, pid = quick_writes
    calls = []

    def slow_write(connection):
        calls.append(connection)
        time.sleep(WRITE_TIMEOUT_S + 1)  # still running when its caller stops waiting

    with pytest.raises(WriteOutcomeUnknown, match=f"No answer within {WRITE_TIMEOUT_S:g} s: the write may or may not"):
        service.with_connection(pid, "dbaguy", slow_write, retry=False)
    assert service.with_connection(pid, "dbaguy", lambda connection: "next") == "next"  # a new connection
    assert len(calls) == 1


def test_a_timeout_the_write_raises_itself_is_not_reported_as_the_wait(service_and_profile):  # fix round 2
    service, pid = service_and_profile

    def own_timeout(connection):
        raise TimeoutError("the driver's own")

    with pytest.raises(WriteOutcomeUnknown, match="^The write failed after it started: ") as raised:
        service.with_connection(pid, "dbaguy", own_timeout, retry=False)
    assert "No answer within" not in raised.value.message


def queued_behind_a_close(service, pid, monkeypatch, pool, submit):
    """`submit()` queues a call on the connection while a read holds it; the connection is then closed (invalidate) and
    replaced by a new one before the queued call gets its turn. Answers the queued call's future and the new connection
    (as `fn` sees it)."""
    reading, queued, closing, release = (threading.Event() for _ in range(4))
    put, begin_close = SessionWorker._put, SessionWorker._begin_close

    def slow_read(connection):
        reading.set()
        release.wait(scaled(30))
        return "read"

    read = pool.submit(service.with_connection, pid, "dbaguy", slow_read)
    assert reading.wait(scaled(30))
    monkeypatch.setattr(SessionWorker, "_put", lambda self, task: (put(self, task), queued.set())[0])
    monkeypatch.setattr(SessionWorker, "_begin_close", lambda self: (begin_close(self), closing.set())[0])
    future = submit()
    assert queued.wait(scaled(30))  # it waits behind the read
    invalidated = pool.submit(service.invalidate, pid)  # closing waits for the read to end
    assert closing.wait(scaled(30))
    replacement = service.with_connection(pid, "dbaguy", lambda connection: connection)  # the key's connection now
    release.set()
    assert read.result(scaled(30)) == "read"
    invalidated.result(scaled(30))
    return future, replacement


def test_a_queued_write_whose_connection_is_closed_was_not_sent(service_and_profile, monkeypatch):  # fix round 2, P2
    """The connection a write is queued on is closed (invalidate, the idle reap, another call's drop): the worker
    refuses the write (SessionBusy), which is answered as "nothing was sent", not as a busy session. It is not resent
    on the connection opened meanwhile, and that connection is not dropped."""
    service, pid = service_and_profile
    wrote = []
    with ThreadPoolExecutor(3) as pool:
        write, replacement = queued_behind_a_close(
            service, pid, monkeypatch, pool,
            lambda: pool.submit(service.with_connection, pid, "dbaguy", wrote.append, retry=False))
        with pytest.raises(QueryFailed, match="^The connection was closed before the write started: nothing was sent"):
            write.result(scaled(30))
    assert service.with_connection(pid, "dbaguy", lambda connection: connection) is replacement  # still open
    assert wrote == []


def test_a_read_queued_on_a_connection_closed_meanwhile_runs_once_on_the_new_one(service_and_profile, monkeypatch):  # P1
    service, pid = service_and_profile
    ran = []

    def queued_read(connection):
        ran.append(connection)
        return connection

    with ThreadPoolExecutor(3) as pool:
        read, replacement = queued_behind_a_close(
            service, pid, monkeypatch, pool, lambda: pool.submit(service.with_connection, pid, "dbaguy", queued_read))
        assert read.result(scaled(30)) is replacement
    assert ran == [replacement]


def test_a_busy_answer_of_the_read_itself_is_not_retried(service_and_profile):  # P1
    """Only a connection that is no longer the key's is a reason to go again: from the current one, SessionBusy is the
    read's own answer."""
    service, pid = service_and_profile
    calls = []

    def busy(connection):
        calls.append(connection)
        raise SessionBusy("its own answer")

    with pytest.raises(SessionBusy, match="its own answer"):
        service.with_connection(pid, "dbaguy", busy)
    assert len(calls) == 1


def test_a_late_drop_never_closes_the_connection_that_replaced_it(service_and_profile, monkeypatch):  # P1
    """A write fails after it started, on a connection closed and replaced while it ran: its drop comes late and leaves
    the new connection open."""
    service, pid = service_and_profile
    started, closing, fail = threading.Event(), threading.Event(), threading.Event()
    begin_close = SessionWorker._begin_close

    def failing_write(connection):
        started.set()
        fail.wait(scaled(30))
        raise RuntimeError("the answer was lost")

    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(service.with_connection, pid, "dbaguy", failing_write, retry=False)
        assert started.wait(scaled(30))
        monkeypatch.setattr(SessionWorker, "_begin_close", lambda self: (begin_close(self), closing.set())[0])
        invalidated = pool.submit(service.invalidate, pid)  # closing waits for the write to end
        assert closing.wait(scaled(30))
        replacement = service.with_connection(pid, "dbaguy", lambda connection: connection)
        fail.set()
        with pytest.raises(WriteOutcomeUnknown):
            write.result(scaled(30))
        invalidated.result(scaled(30))
    assert service.with_connection(pid, "dbaguy", lambda connection: connection) is replacement  # still open
