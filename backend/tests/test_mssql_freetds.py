"""Logins through DbaGuy's freetds.conf against the dev SQL Server: that a verifying login cannot fall back to an
unverified one, that the user's own FreeTDS files never apply, and that an IPv6 host gets its port."""

import shutil
import socket
import subprocess
import threading

import pytest
from pymssql import _mssql

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mssql import freetds
from dbaguy_backend.adapters.mssql.settings import connect_params
from dbaguy_backend.domain.profiles import SslConfig
from tests.support import (
    MSSQL_HOST,
    MSSQL_PASSWORD,
    MSSQL_PORT,
    MSSQL_SA_PASSWORD,
    make_mssql_profile,
    mssql_connect,
    requires_mssql,
)

DEV = Endpoint(MSSQL_HOST, MSSQL_PORT)


@pytest.fixture
def own_freetds(tmp_path, monkeypatch):
    """A fresh DbaGuy freetds.conf for one test; FREETDSCONF is restored afterwards."""
    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    freetds.use(tmp_path / "dbaguy" / "freetds.conf")
    return freetds.active()


def login(endpoint: Endpoint = DEV, **profile_overrides) -> _mssql.MSSQLConnection:
    """What the adapter does before it connects: the params, the section registered, the login."""
    params = connect_params(make_mssql_profile(**profile_overrides), endpoint, MSSQL_PASSWORD, None)
    freetds.active().section_for(params.tds_section)
    return mssql_connect(**params.kwargs)


def is_encrypted(spid: int) -> bool:
    """Whether the session's connection is encrypted, as `sa` (the dbaguy login cannot see sys.dm_exec_connections)."""
    sa = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
    try:
        return sa.execute_scalar(f"SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = {int(spid)}") == "TRUE"
    finally:
        sa.close()


def an_unrelated_ca(directory) -> str:
    """A well-formed CA certificate that did not sign the dev server's certificate."""
    openssl = shutil.which("openssl")
    assert openssl, "openssl is needed to make a CA certificate for the test"
    ca = directory / "unrelated-ca.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(directory / "unrelated-ca.key"),
         "-out", str(ca), "-days", "1", "-subj", "/CN=not-the-servers-ca"],
        check=True, capture_output=True,
    )
    return str(ca)


@requires_mssql
@pytest.mark.parametrize("mode, encrypted", [("disable", False), ("prefer", False), ("require", True)])
def test_logins_without_verification_go_through_their_own_section(own_freetds, mode, encrypted):
    conn = login(ssl=SslConfig(mode=mode))
    try:
        assert conn.execute_scalar("SELECT DB_NAME()") == "dbaguy"
        assert is_encrypted(conn.execute_scalar("SELECT @@SPID")) is encrypted  # `request` encrypts the login only
    finally:
        conn.close()
    assert "[dbaguy_" in own_freetds.path.read_text()


@requires_mssql
@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
@pytest.mark.parametrize("ca_kind", ["a CA that did not sign the certificate", "a file that is no certificate"])
def test_a_verifying_login_with_the_wrong_ca_fails_instead_of_connecting_unverified(own_freetds, tmp_path, mode, ca_kind):
    # R-03b-T6d. The control: the same target logs in when nothing is verified, so the failure below is the CA's.
    login(ssl=SslConfig(mode="require")).close()
    if ca_kind.startswith("a CA"):
        ca = an_unrelated_ca(tmp_path)
    else:
        bogus = tmp_path / "bogus.pem"
        bogus.write_text("-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")
        ca = str(bogus)
    with pytest.raises(_mssql.MSSQLException):
        login(ssl=SslConfig(mode=mode, ca_file=ca)).close()
    assert ca in own_freetds.path.read_text()  # the login did name its section: the failure is the verification


@requires_mssql
def test_the_users_own_freetds_files_never_apply(own_freetds, tmp_path, monkeypatch):
    # R-03b-T6e. FreeTDS looks for the server's section in several files; $FREETDS/etc/freetds.conf stands in for
    # ~/.freetds.conf, a later file of the same search. Its [global] sets a CA file that cannot verify anything.
    other = tmp_path / "other"
    (other / "etc").mkdir(parents=True)
    bogus = tmp_path / "bogus.pem"
    bogus.write_text("-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")
    (other / "etc" / "freetds.conf").write_text(f"[global]\n\tca file = {bogus}\n")
    monkeypatch.setenv("FREETDS", str(other))
    # The control: a login that names no DbaGuy section reads that file and cannot get its encrypted connection.
    with pytest.raises(_mssql.MSSQLException):
        mssql_connect(server="localhost", encryption="require")
    # Every DbaGuy login names a section, in the `server:port` form FreeTDS looks up before it reads any other file.
    conn = login(ssl=SslConfig(mode="require"))
    try:
        spid = conn.execute_scalar("SELECT @@SPID")
        monkeypatch.delenv("FREETDS")  # the check below is a login of the test's own, without a DbaGuy section
        assert is_encrypted(spid)
    finally:
        conn.close()


def test_an_ipv6_host_logs_in_at_the_sections_port(own_freetds):
    """pymssql adds ":port" to a server only when it has no ":", which an IPv6 host has; the section carries both."""
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        listener.bind(("::1", 0))
    except OSError:
        listener.close()
        pytest.skip("no IPv6 loopback")
    listener.listen(1)
    listener.settimeout(10)
    seen: list[tuple[str, int]] = []

    def accept() -> None:
        try:
            conn, address = listener.accept()
            seen.append(address[:2])
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(_mssql.MSSQLException):  # the listener hangs up: only where the login went matters
            login(Endpoint("::1", port), host="::1", port=port)
        thread.join(10)
    finally:
        listener.close()
    assert [host for host, _ in seen] == ["::1"]
