import os
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mssql import freetds
from dbaguy_backend.adapters.mssql.freetds import FreeTdsConfig, TdsSection
from dbaguy_backend.adapters.mssql.settings import SESSION_DEFAULTS, connect_params, parse_mssql_url
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.profiles import ServerProfile, SslConfig

DIRECT = Endpoint("db.example.com", 1433)
TUNNEL = Endpoint("127.0.0.1", 52001, tls_server_name="db.example.com")


def profile(**overrides) -> ServerProfile:
    data = {"id": uuid4(), "name": "m", "engine": "mssql", "host": "db.example.com", "database": "app", "username": "me"}
    return ServerProfile(**{**data, **overrides})


@pytest.fixture
def logs():
    """Every log line written during the test, DEBUG and up."""
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    logger.remove(sink)


def test_parse_url_forms():
    url = parse_mssql_url("mssql://me:p%40ss@db.example.com:1444/app?encrypt=true&TrustServerCertificate=true")
    draft = url.profile
    assert (draft.engine, draft.host, draft.port, draft.database, draft.username) == ("mssql", "db.example.com", 1444, "app", "me")
    assert url.password == "p@ss" and draft.ssl.mode == "require" and draft.name == "db.example.com/app"
    jdbc = parse_mssql_url("sqlserver://db.example.com:1433;databaseName=app;user=me;password=x;encrypt=true")
    assert (jdbc.profile.host, jdbc.profile.port, jdbc.profile.database, jdbc.profile.username, jdbc.password) == (
        "db.example.com", 1433, "app", "me", "x")
    assert jdbc.profile.ssl.mode == "verify-full"  # encrypt=true without trustServerCertificate: verify the certificate
    go = parse_mssql_url("sqlserver://me:pw@db.example.com?database=app&encrypt=disable")
    assert (go.profile.database, go.profile.port, go.profile.ssl.mode) == ("app", None, "disable")
    alchemy = parse_mssql_url("mssql+pymssql://me@h:1433/app?charset=utf8&tds_version=7.3")
    assert alchemy.profile.options == {"tds_version": "7.3"}  # charset is DbaGuy's to set: dropped
    assert parse_mssql_url("sqlserver://h;encrypt=false").profile.ssl.mode == "prefer"
    named = parse_mssql_url("sqlserver://;serverName=h;portNumber=1500;databaseName=d").profile
    assert (named.host, named.port, named.database) == ("h", 1500, "d")


@pytest.mark.parametrize("url, message", [
    ("mssql:///app", "needs a host"),
    ("sqlserver://h\\inst;databaseName=d", "Named instances"),
    ("mssql://h%5Cinst/app", "Named instances"),
    ("mssql://h/app?encrypt=strict", "strict"),
    ("mssql://h/app?encrypt=bogus", "encrypt"),
    ("sqlserver://h:abc;databaseName=d", "port"),
    ("sqlserver://h;integratedSecurity=true", "SQL login"),
    ("sqlserver://h;databaseName", "key=value"),
])
def test_parse_url_errors(url, message):
    with pytest.raises(ValidationFailed, match=message):
        parse_mssql_url(url)


def test_connect_params_keep_the_target_and_dbaguys_settings():
    options = {"tds_version": "7.3", "charset": "latin1", "timeout": "5", "conn_properties": "SET IMPLICIT_TRANSACTIONS ON"}
    params = connect_params(profile(options=options), DIRECT, "pw", None)
    assert params.kwargs == {
        "server": "db.example.com", "port": "1433", "user": "me", "password": "pw", "database": "app",
        "appname": "DbaGuy", "charset": "UTF-8", "tds_version": "7.3", "encryption": "request",
        "conn_properties": SESSION_DEFAULTS,
    }
    assert params.tds_section is None
    assert "IMPLICIT_TRANSACTIONS" not in SESSION_DEFAULTS and "QUOTED_IDENTIFIER ON" in SESSION_DEFAULTS
    assert "CURSOR_CLOSE_ON_COMMIT" not in SESSION_DEFAULTS  # SSMS's default, not pymssql's
    assert connect_params(profile(), DIRECT, None, "other").kwargs["database"] == "other"
    assert connect_params(profile(database=None), DIRECT, None, None).kwargs["database"] == ""
    with pytest.raises(ConnectionFailed, match="tds_version"):
        connect_params(profile(options={"tds_version": "9.9"}), DIRECT, None, None)


@pytest.mark.parametrize("mode, encryption", [("disable", "off"), ("prefer", "request"), ("require", "require")])
def test_modes_without_verification(mode, encryption):
    params = connect_params(profile(ssl=SslConfig(mode=mode, ca_file="/tmp/ca.pem")), DIRECT, None, None)
    assert (params.kwargs["encryption"], params.tds_section) == (encryption, None)


def test_verifying_modes_use_a_freetds_section():
    full = connect_params(profile(ssl=SslConfig(mode="verify-full", ca_file="~/ca.pem")), DIRECT, None, None)
    assert full.tds_section == TdsSection("db.example.com", 1433, os.path.expanduser("~/ca.pem"), True)
    assert full.kwargs["encryption"] == "require"
    ca_only = connect_params(profile(ssl=SslConfig(mode="verify-ca", ca_file="/tmp/ca.pem")), TUNNEL, None, None)
    assert ca_only.tds_section == TdsSection("127.0.0.1", 52001, "/tmp/ca.pem", False)


def test_tls_settings_that_cannot_work_fail_before_connecting():
    with pytest.raises(ConnectionFailed, match="SSH tunnel"):
        connect_params(profile(ssl=SslConfig(mode="verify-full", ca_file="/tmp/ca.pem")), TUNNEL, None, None)
    with pytest.raises(ConnectionFailed, match="CA file"):
        connect_params(profile(ssl=SslConfig(mode="verify-ca")), DIRECT, None, None)
    with pytest.raises(ConnectionFailed, match="Named instances"):
        connect_params(profile(host="db\\inst"), Endpoint("db\\inst", 1433), None, None)


def test_freetds_sections(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    config = FreeTdsConfig(tmp_path / "freetds.conf")
    target = TdsSection("db.example.com", 1433, str(ca), True)
    name = config.section_for(target)
    text = (tmp_path / "freetds.conf").read_text()
    assert f"[{name}]" in text and f"\tca file = {ca}" in text and "\tcheck certificate hostname = yes" in text
    assert "\tencryption = require" in text and "\thost = db.example.com" in text and "\tport = 1433" in text
    assert config.section_for(target) == name  # the same target reuses its section
    other = config.section_for(TdsSection("127.0.0.1", 52001, str(ca), False))
    assert other != name and f"[{name}]" in (tmp_path / "freetds.conf").read_text()  # earlier sections stay
    with pytest.raises(ConnectionFailed, match="CA file not found"):
        config.section_for(TdsSection("h", 1433, str(tmp_path / "missing.pem"), True))
    with pytest.raises(ConnectionFailed, match="line break"):
        config.section_for(TdsSection("h", 1433, str(ca) + "\nhost = evil", True))


def test_the_freetds_file_is_written_on_first_use_only(tmp_path, monkeypatch):  # R-03b-P9
    monkeypatch.setenv("FREETDSCONF", "before")  # recorded, so the variable is restored after the test
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    target = tmp_path / "sub" / "freetds.conf"
    freetds.use(target)
    assert not target.exists() and os.environ["FREETDSCONF"] == "before"  # recording writes nothing
    config = freetds.active()
    assert os.environ["FREETDSCONF"] == str(target) and "[global]" in target.read_text()
    assert freetds.active() is config  # idempotent


# ---- beyond the brief's tests: the rules that are easy to get wrong ----


def test_freetds_active_is_one_file_for_concurrent_first_logins(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    freetds.use(tmp_path / "freetds.conf")
    configs, barrier = [], threading.Barrier(8)

    def first_login() -> None:
        barrier.wait()
        configs.append(freetds.active())

    threads = [threading.Thread(target=first_login) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(configs) == 8 and all(config is configs[0] for config in configs)


def test_freetds_without_a_recorded_location_uses_a_temporary_directory(monkeypatch):
    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    config = freetds.active()
    try:
        assert config.path.name == "freetds.conf" and config.path.is_file()
        assert os.environ["FREETDSCONF"] == str(config.path)
    finally:
        config.path.unlink()
        config.path.parent.rmdir()


def test_the_last_recorded_location_wins_before_the_first_use(tmp_path, monkeypatch):
    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    freetds.use(tmp_path / "first" / "freetds.conf")
    freetds.use(tmp_path / "second" / "freetds.conf")
    assert freetds.active().path == tmp_path / "second" / "freetds.conf"
    assert not (tmp_path / "first").exists()


def test_a_section_name_follows_the_whole_target():
    names = {
        TdsSection("h", 1433, "/ca.pem", True).name, TdsSection("h", 1434, "/ca.pem", True).name,
        TdsSection("h2", 1433, "/ca.pem", True).name, TdsSection("h", 1433, "/other.pem", True).name,
        TdsSection("h", 1433, "/ca.pem", False).name,
    }
    assert len(names) == 5 and all(name.startswith("dbaguy_") for name in names)
    assert TdsSection("h", 1433, "/ca.pem", True).name == TdsSection("h", 1433, "/ca.pem", True).name


@pytest.mark.parametrize("spelling", ["TrustServerCertificate", "trust_server_certificate", "trust-server-certificate",
                                      "trustservercertificate", "TRUSTSERVERCERTIFICATE"])
def test_keys_fold_case_hyphens_and_underscores(spelling):
    jdbc = parse_mssql_url(f"sqlserver://h;encrypt=true;{spelling}=true")
    query = parse_mssql_url(f"mssql://h/db?encrypt=true&{spelling}=yes")
    assert jdbc.profile.ssl.mode == "require" and query.profile.ssl.mode == "require"


@pytest.mark.parametrize("spelling", ["tds_version", "tdsVersion", "TDS-Version", "tdsversion"])
def test_the_tds_version_key_is_one_option_in_both_forms(spelling):
    assert parse_mssql_url(f"mssql://h/db?{spelling}=7.2").profile.options == {"tds_version": "7.2"}
    assert parse_mssql_url(f"sqlserver://h;{spelling}=7.2").profile.options == {"tds_version": "7.2"}


@pytest.mark.parametrize("encrypt, trust, mode", [
    ("disable", None, "disable"), ("DISABLE", "true", "disable"),
    ("false", None, "prefer"), ("no", None, "prefer"), ("optional", None, "prefer"),
    ("true", None, "verify-full"), ("yes", "false", "verify-full"), ("mandatory", "no", "verify-full"),
    ("true", "true", "require"), ("Yes", "YES", "require"), ("mandatory", "true", "require"),
    (None, None, "prefer"), (None, "true", "prefer"),  # trustServerCertificate alone asks for nothing
])
def test_the_encrypt_table(encrypt, trust, mode):
    parts = [f"encrypt={encrypt}"] if encrypt else []
    parts += [f"trustServerCertificate={trust}"] if trust else []
    assert parse_mssql_url("sqlserver://h;" + ";".join(parts)).profile.ssl.mode == mode
    assert parse_mssql_url("mssql://h/db?" + "&".join(parts)).profile.ssl.mode == mode


def test_encryption_is_another_spelling_of_the_encrypt_key():
    assert parse_mssql_url("mssql://h/db?encryption=true&trustServerCertificate=true").profile.ssl.mode == "require"
    assert parse_mssql_url("sqlserver://h;encryption=disable").profile.ssl.mode == "disable"


@pytest.mark.parametrize("url", [
    "sqlserver://h;integratedSecurity=true", "sqlserver://h;IntegratedSecurity=yes", "mssql://h/db?integrated_security=true",
    "sqlserver://h;authentication=ActiveDirectoryPassword", "sqlserver://h;authentication=ActiveDirectoryIntegrated",
    "mssql://h/db?authentication=ActiveDirectoryMSI",
])
def test_windows_and_azure_ad_logins_are_refused(url):
    with pytest.raises(ValidationFailed, match="use a SQL login"):
        parse_mssql_url(url)


@pytest.mark.parametrize("url", [
    "sqlserver://h;integratedSecurity=false", "sqlserver://h;authentication=SqlPassword",
    "mssql://h/db?authentication=sqlpassword", "sqlserver://h;authentication=;integratedSecurity=",
])
def test_sql_logins_pass_through(url):
    assert parse_mssql_url(url).profile.host == "h"


def test_an_authority_wins_over_the_query_and_the_path_over_database():
    parsed = parse_mssql_url("mssql://a:b@h/db1?user=u&password=p&database=db2&username=u2")
    assert (parsed.profile.username, parsed.password, parsed.profile.database) == ("a", "b", "db1")
    filled = parse_mssql_url("mssql://h?user=u&password=p&database=d")
    assert (filled.profile.username, filled.password, filled.profile.database) == ("u", "p", "d")


def test_the_address_wins_over_servername_and_portnumber():
    parsed = parse_mssql_url("sqlserver://real:1433;serverName=other;portNumber=9999")
    assert (parsed.profile.host, parsed.profile.port) == ("real", 1433)
    host_only = parse_mssql_url("sqlserver://real;portNumber=1500")
    assert (host_only.profile.host, host_only.profile.port) == ("real", 1500)


def test_servername_and_portnumber_are_jdbc_only():
    assert parse_mssql_url("mssql://h/db?serverName=other&portNumber=9999").profile.port is None
    with pytest.raises(ValidationFailed, match="needs a host"):
        parse_mssql_url("mssql:///db?serverName=other")


def test_a_jdbc_url_may_end_in_a_semicolon_and_carry_ipv6():
    parsed = parse_mssql_url("sqlserver://[::1]:1444;databaseName=d;user=u;password=p;")
    assert (parsed.profile.host, parsed.profile.port, parsed.profile.database) == ("::1", 1444, "d")
    assert parse_mssql_url("sqlserver://h;;databaseName=d;;").profile.database == "d"
    assert parse_mssql_url("mssql://[2001:db8::1]:1433/d").profile.host == "2001:db8::1"


def test_url_values_are_unquoted_but_never_form_decoded_and_jdbc_values_are_kept_as_written():
    assert parse_mssql_url("mssql://u@h/db?password=a%2Bb+c").password == "a+b+c"  # %-decoded, "+" stays
    assert parse_mssql_url("sqlserver://h;password=a%2Bb;user=u").password == "a%2Bb"
    assert parse_mssql_url("sqlserver://h;password=100%;user=u").password == "100%"
    assert parse_mssql_url("sqlserver://h;password= pw ;user=u").password == "pw"  # stripped


def test_urls_that_name_no_usable_port_are_refused():
    for url in ("mssql://h:0/db", "sqlserver://h:0;databaseName=d", "sqlserver://h;portNumber=0", "sqlserver://h;portNumber=70000",
                "sqlserver://h;portNumber=abc", "mssql://h:99999/db"):
        with pytest.raises(ValidationFailed, match="Invalid port"):
            parse_mssql_url(url)


def test_unknown_url_keys_are_dropped_and_logged_by_name_only(logs):
    parsed = parse_mssql_url("sqlserver://h;databaseName=d;applicationName=zz;loginTimeout=5")
    assert parsed.profile.options == {} and parsed.profile.database == "d"
    text = "\n".join(logs)
    assert "applicationName" in text and "loginTimeout" in text and "zz" not in text.replace("applicationName", "")


# ---- leak hunt: the password never reaches a message or a log ----

SECRET = "hunter2zq"


@pytest.mark.parametrize("url", [
    f"mssql://root:{SECRET}＠db.example.com:1433/app",  # a fullwidth "@" typed by an IME
    f"sqlserver://root:{SECRET}＠db.example.com:1433;databaseName=app",
    f"mssql://u:{SECRET}/x@host/db",  # a slash in the password ends the authority: the rest is read as a port
    f"mssql://u:{SECRET}?x@host/db",
    f"mssql://u:{SECRET}#x@host/db",
    f"mssql://u:{SECRET}@host:{SECRET}/db",  # a port that holds the password
    f"mssql://u:{SECRET}@host:99999/db",
    f"mssql://u:{SECRET}@host:0/db",
    f"mssql://u:{SECRET}@[::1/db",  # unclosed IPv6 bracket
    f"mssql://u:{SECRET}@[::1]x/db",
    f"mssql://u:{SECRET}@[{SECRET}]:1433/db",
    f"sqlserver://[::1;user=u;password={SECRET}",
    f"sqlserver://h:abc;user=u;password={SECRET}",
    f"sqlserver://h;user=u;password={SECRET};databaseName",  # a property without =
    f"sqlserver://h;password={SECRET}x;y",  # a ';' inside the password, JDBC form
    f"sqlserver://h;password={SECRET};encrypt=strict",
    f"sqlserver://h;password={SECRET};encrypt=bogus",
    f"sqlserver://h;password={SECRET};integratedSecurity=true",
    f"sqlserver://h;password={SECRET};authentication=ActiveDirectoryPassword",
    f"sqlserver://h\\inst;password={SECRET}",
    f"mssql://u:{SECRET}@h%5Cinst/db",
    f"mssql://u:{SECRET};x@h/db",  # a ';' inside the password, URL form: read as JDBC, the "port" is `{SECRET}`
    f"sqlserver://u:{SECRET}@;serverName=h",
    f"mssql://u:{SECRET}@/db",
    f"mssql://:{SECRET}@/db",
    f"sqlserver://;password={SECRET}",
    f"sqlserver://h;portNumber=x;password={SECRET}",
    f"sqlserver://h:70000;password={SECRET}",
])
def test_a_password_in_a_url_is_never_echoed(url, logs):
    try:
        parse_mssql_url(url)
    except Exception as exc:  # urllib's and pydantic's own errors quote the input: only ValidationFailed may come out
        assert type(exc) is ValidationFailed, type(exc)
        assert SECRET not in str(exc) and SECRET not in exc.message
    assert SECRET not in "\n".join(logs)


def test_a_malformed_authority_says_what_to_do():
    for url in (f"mssql://root:{SECRET}＠db.example.com:1433/app", f"mssql://u:{SECRET}/x@host/db",
                f"mssql://u:{SECRET}@[::1/db"):
        with pytest.raises(ValidationFailed, match="Percent-encode"):
            parse_mssql_url(url)


def test_dropped_property_names_are_not_logged_when_the_url_carries_a_password(logs):
    # A ';' or '&' inside a password splits it into pieces that look like unknown properties: their names may be
    # fragments of the password, so with a password in the URL only the count is logged.
    parse_mssql_url(f"sqlserver://h;password={SECRET};zq9fragment=x;encrypt=true")
    parse_mssql_url(f"mssql://h/db?password={SECRET}&zq9other=x")
    parse_mssql_url("mssql://u:pw@h/db?zq9third=x")
    text = "\n".join(logs)
    assert "zq9fragment" not in text and "zq9other" not in text and SECRET not in text
    assert "zq9third" in text  # an authority password is not split by '&' or ';': names are safe again


def test_connect_params_errors_and_logs_never_hold_the_password(logs):
    cases = [
        (profile(host="db\\inst"), Endpoint("db\\inst", 1433)),
        (profile(ssl=SslConfig(mode="verify-full", ca_file="/tmp/ca.pem")), TUNNEL),
        (profile(ssl=SslConfig(mode="verify-ca")), DIRECT),
        (profile(options={"tds_version": "9.9"}), DIRECT),
    ]
    for case, endpoint in cases:
        with pytest.raises(ConnectionFailed) as info:
            connect_params(case, endpoint, SECRET, None)
        assert SECRET not in info.value.message
    connect_params(profile(options={"password": SECRET, "zz": "1"}, ssl=SslConfig(cert_file="/c", key_file="/k")), DIRECT, SECRET, None)
    assert SECRET not in "\n".join(logs)


# ---- connect_params: options and client certificates ----


def test_options_are_an_allow_list_of_tds_version(logs):
    options = {"TDS_Version": " 7.1 ", "login_timeout": "1", "server": "evil", "port": "1", "user": "evil", "password": "evil",
               "encryption": "off", "appname": "x", "dsn": "x", "conn_properties": "x", "readonly": "true"}
    params = connect_params(profile(options=options), DIRECT, "pw", None)
    assert params.kwargs["tds_version"] == "7.1"
    assert (params.kwargs["server"], params.kwargs["user"], params.kwargs["password"], params.kwargs["encryption"]) == (
        "db.example.com", "me", "pw", "request")
    assert not {"login_timeout", "dsn", "readonly"} & params.kwargs.keys()
    text = "\n".join(logs)
    assert "login_timeout" in text and "conn_properties" in text and "evil" not in text


def test_the_default_tds_version_is_7_4():
    assert connect_params(profile(), DIRECT, None, None).kwargs["tds_version"] == "7.4"


@pytest.mark.parametrize("version", ["7.0", "7.1", "7.2", "7.3", "7.4"])
def test_every_supported_tds_version_passes(version):
    assert connect_params(profile(options={"tds_version": version}), DIRECT, None, None).kwargs["tds_version"] == version


@pytest.mark.parametrize("version", ["", "8.0", "7.5", "7", "74", "auto", "7.4.1"])
def test_other_tds_versions_fail_before_connecting(version):
    with pytest.raises(ConnectionFailed, match="7.0-7.4"):
        connect_params(profile(options={"tds_version": version}), DIRECT, None, None)


def test_client_certificates_are_ignored_with_a_warning(logs):
    ssl = SslConfig(mode="require", cert_file="/tmp/c.pem", key_file="/tmp/k.pem")
    params = connect_params(profile(ssl=ssl), DIRECT, None, None)
    assert params.tds_section is None and "/tmp/c.pem" not in str(params.kwargs)
    assert any("does not use client certificates" in line for line in logs)


def test_a_named_instance_is_refused_whichever_side_names_it():
    with pytest.raises(ConnectionFailed, match="Named instances"):
        connect_params(profile(host="db\\inst"), TUNNEL, None, None)  # through a tunnel the endpoint is local
    with pytest.raises(ConnectionFailed, match="Named instances"):
        connect_params(profile(), Endpoint("db\\inst", 1433), None, None)
