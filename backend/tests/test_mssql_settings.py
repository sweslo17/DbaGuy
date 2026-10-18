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
    assert params.tds_section == TdsSection("db.example.com", 1433)  # every login names a section of DbaGuy's file
    assert params.kwargs == {
        "server": params.tds_section.name, "port": "1433", "user": "me", "password": "pw", "database": "app",
        "appname": "DbaGuy", "charset": "UTF-8", "tds_version": "7.3", "encryption": "request",
        "conn_properties": SESSION_DEFAULTS,
    }
    assert "IMPLICIT_TRANSACTIONS" not in SESSION_DEFAULTS and "QUOTED_IDENTIFIER ON" in SESSION_DEFAULTS
    assert "CURSOR_CLOSE_ON_COMMIT" not in SESSION_DEFAULTS  # SSMS's default, not pymssql's
    assert connect_params(profile(), DIRECT, None, "other").kwargs["database"] == "other"
    assert connect_params(profile(database=None), DIRECT, None, None).kwargs["database"] == ""
    with pytest.raises(ConnectionFailed, match="tds_version"):
        connect_params(profile(options={"tds_version": "9.9"}), DIRECT, None, None)


@pytest.mark.parametrize("mode, encryption", [("disable", "off"), ("prefer", "request"), ("require", "require")])
def test_modes_without_verification(mode, encryption):
    params = connect_params(profile(ssl=SslConfig(mode=mode, ca_file="/tmp/ca.pem")), DIRECT, None, None)
    assert (params.kwargs["encryption"], params.tds_section) == (encryption, TdsSection("db.example.com", 1433))
    assert params.kwargs["server"] == params.tds_section.name


def test_verifying_modes_use_a_freetds_section():
    full = connect_params(profile(ssl=SslConfig(mode="verify-full", ca_file="~/ca.pem")), DIRECT, None, None)
    assert full.tds_section == TdsSection("db.example.com", 1433, os.path.expanduser("~/ca.pem"), True)
    assert full.kwargs["encryption"] == "require"
    assert full.kwargs["server"] == full.tds_section.name != "db.example.com"  # no login without its section
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
    assert TdsSection("h", 1433).name.endswith(":1433") and TdsSection("h", 1434).name.endswith(":1434")
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
    ("false", None, "prefer"), ("no", None, "prefer"), ("optional", None, "prefer"), ("false", "true", "prefer"),
    ("true", None, "verify-full"), ("yes", "false", "verify-full"), ("mandatory", "no", "verify-full"),
    ("true", "true", "require"), ("Yes", "YES", "require"), ("mandatory", "true", "require"),
])
def test_the_encrypt_table(encrypt, trust, mode):
    parts = [f"encrypt={encrypt}"] + ([f"trustServerCertificate={trust}"] if trust else [])
    assert parse_mssql_url("sqlserver://h;" + ";".join(parts)).profile.ssl.mode == mode
    assert parse_mssql_url("mssql://h/db?" + "&".join(parts)).profile.ssl.mode == mode


@pytest.mark.parametrize("url, mode", [  # R-03b-T6b: a pasted URL never imports weaker TLS than it asks for
    ("sqlserver://h;trustServerCertificate=true", "require"),  # encrypted, unverified: what the JDBC drivers mean by it
    ("mssql://h/db?trustServerCertificate=true", "require"),
    ("mssql://h/db?trust_server_certificate=yes", "require"),
    ("sqlserver://h;databaseName=d", "require"),  # the JDBC drivers' default is encrypt=true
    ("sqlserver://h:1433;databaseName=d;trustServerCertificate=false", "require"),
    ("sqlserver://h;", "require"),
    ("sqlserver://host:1433", "require"),  # R-03b-T6f: any sqlserver:// URL, properties or not
    ("sqlserver://host", "require"),
    ("sqlserver://me@h?database=d", "require"),  # go-mssqldb's form
    ("sqlserver://me:pw@h:1433?database=d&app+name=x", "require"),
    ("SQLSERVER://host:1433", "require"),
    ("mssql://h/db", "prefer"),  # SQLAlchemy's URL asks for nothing
    ("mssql://h/db?trustServerCertificate=false", "prefer"),
    ("mssql+pymssql://me@h:1433/db", "prefer"),
    ("mssql://h", "prefer"),
])
def test_no_encrypt_key(url, mode):
    assert parse_mssql_url(url).profile.ssl.mode == mode


@pytest.mark.parametrize("url, mode", [
    ("sqlserver://host:1433", "disable"), ("sqlserver://host:1433", "prefer"),
    ("sqlserver://host?database=d", "verify-full"), ("mssql://h/db", "require"),
])
def test_an_explicit_encrypt_key_wins_over_the_scheme_default(url, mode):
    key = {"disable": "disable", "prefer": "false", "verify-full": "true", "require": "require"}[mode]
    separator = "&" if "?" in url else "?"
    assert parse_mssql_url(f"{url}{separator}encrypt={key}").profile.ssl.mode == mode


def test_a_jdbc_url_without_properties_asks_for_encryption_after_the_registry_strips_the_prefix():
    # R-03b-T6f: `jdbc:sqlserver://host:1433` has no `;`, so after the strip it is told apart by its scheme alone.
    from types import SimpleNamespace

    from dbaguy_backend.adapters.mssql.settings import URL_SCHEMES
    from dbaguy_backend.adapters.registry import AdapterRegistry

    adapter = SimpleNamespace(engine="mssql", url_schemes=URL_SCHEMES, parse_url=parse_mssql_url)
    registry = AdapterRegistry([adapter])
    for pasted in ("jdbc:sqlserver://host:1433", "JDBC:sqlserver://host", " jdbc:sqlserver://host:1433 "):
        assert registry.parse_url(pasted).profile.ssl.mode == "require"
    assert registry.parse_url("mssql://host:1433/db").profile.ssl.mode == "prefer"


@pytest.mark.parametrize("value, mode", [("off", "disable"), ("request", "prefer"), ("require", "require"),
                                         ("OFF", "disable"), ("Request", "prefer")])
def test_pymssqls_own_encryption_values_are_accepted(value, mode):
    assert parse_mssql_url(f"mssql+pymssql://u@h/db?encryption={value}").profile.ssl.mode == mode
    assert parse_mssql_url(f"sqlserver://h;encryption={value}").profile.ssl.mode == mode


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


def test_unknown_url_keys_are_dropped_and_only_their_number_is_logged(logs):
    parsed = parse_mssql_url("sqlserver://h;databaseName=d;applicationName=zzname;loginTimeout=5")
    assert parsed.profile.options == {} and parsed.profile.database == "d"
    text = "\n".join(logs)
    assert "Dropping 2 SQL Server URL parameters" in text
    assert "applicationName" not in text and "loginTimeout" not in text and "zzname" not in text
    parse_mssql_url("mssql://u:pw@h/db?zq9third=x")
    assert "zq9third" not in "\n".join(logs)  # never names, with or without a password in the URL


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
    # R-03b-T6a: the two forms mixed in one URL (the review's reproductions first)
    f"mssql://h/db?encrypt=true;password={SECRET}",
    f"sqlserver://h:1433?databaseName=d;user=u;password={SECRET}",
    f"mssql://h/db?user=u;password={SECRET}",
    f"mssql://h/db?database=d;user=u;password={SECRET}&encrypt=true",
    f"mssql://h/db?tds_version=7.4;password={SECRET}",
    f"mssql://h/db?x;password={SECRET}",
    f"mssql://h/db?encrypt=true&x;password={SECRET}",
    f"sqlserver://h;databaseName=d?encrypt=true;password={SECRET}",
    f"sqlserver://h;databaseName=d&encrypt=true&password={SECRET}",
    f"sqlserver://h;user=u&password={SECRET}",
    f"sqlserver://h;password={SECRET}&encrypt=true;databaseName=d",
    f"mssql://h/db&password={SECRET}",
    f"mssql://h&password={SECRET}/db",
    f"mssql://h/db?encrypt={SECRET}",  # an unknown encrypt value is never echoed, whatever it is
    f"mssql://h/db?tds_version={SECRET}",
    f"sqlserver://h;tdsVersion={SECRET};password=x",
    # fullwidth separators typed by an IME
    f"sqlserver://h\uff1bdatabaseName=d\uff1buser=u\uff1bpassword={SECRET}",
    f"sqlserver://h;databaseName=d\uff1bpassword={SECRET}",
    f"mssql://h/db?encrypt=true\uff06password={SECRET}",
    f"sqlserver://h;databaseName=d\uff1fencrypt=true;password={SECRET}",
    # brace-quoted values
    f"sqlserver://h;password={{{SECRET}",  # not closed
    f"sqlserver://h;password={{{SECRET}}}x;user=u",  # text after the closing brace
    f"sqlserver://h;user={{u;password={SECRET}",
    f"sqlserver://h;password={{{SECRET}}};encrypt=bogus",
    f"sqlserver://h;password={{{SECRET}}};instanceName=SQLEXPRESS",
    f"sqlserver://h/SQLEXPRESS;password={SECRET}",
    f"mssql://u:{SECRET}@h/db?instanceName=SQLEXPRESS",
    # R-03b-T6g (a): "?" as a separator, ASCII and lookalikes (fullwidth ？, U+2047 ⁇, small ﹖, ⁈)
    f"mssql://h/db?user=me?password={SECRET}",
    f"sqlserver://h?databaseName=d?user=u?password={SECRET}",
    f"mssql://h/db\uff1fpassword={SECRET}",
    f"mssql://h/db?user=u\uff1fpassword={SECRET}",
    f"mssql://h/db?user=u\u2047password={SECRET}",
    f"mssql://h/db\u2047password={SECRET}",
    f"mssql://h/db?user=u\ufe56password={SECRET}",
    f"mssql://h/db?database=d\u2048password={SECRET}",
    f"mssql://h/db?user=u\uff1dpassword={SECRET}",  # fullwidth ＝
    f"mssql://h/db?database=password={SECRET}",  # a key=value in a value
    f"sqlserver://h;databaseName=password={SECRET}",
    # (b): an ADO.NET connection string after a scheme, and an authority user name that is the rest of a URL
    f"mssql://Password={SECRET};Server=h",
    f"sqlserver://Password={SECRET}",
    f"mssql://Password={SECRET}",
    f"mssql://Server=h:1433;Password={SECRET};Database=d",
    f"mssql://u&password={SECRET}@h/db",
    f"mssql://u\uff06password={SECRET}@h/db",
    f"mssql://u;password={SECRET}@h/db",
    f"mssql://u?password={SECRET}@h/db",
    f"mssql://u=password={SECRET}@h/db",
    # (c): brace-quoted values that are not the password
    f"sqlserver://h;databaseName={{d;password={SECRET}}}",
    f"sqlserver://h;user={{u;password={{{SECRET}}}",
    f"sqlserver://h;user={{u&password={SECRET}}};databaseName=d",
    f"sqlserver://h;databaseName={{password={SECRET}}}",
    # (d): percent-encoded separators
    f"mssql://h/db?user=u%3Bpassword%3D{SECRET}",
    f"mssql://u%26password%3D{SECRET}@h/db",
    f"mssql://h/db%3Bpassword%3D{SECRET}",
    f"mssql://h?database=d%3Bpassword%3D{SECRET}",
    f"sqlserver://h;databaseName=d%3Bpassword%3D{SECRET}",
    f"mssql://h/db?user=u%EF%BC%9Bpassword%3D{SECRET}",
    f"mssql://h/db?user=u%3Fpassword%3D{SECRET}",
    # (e): "@" in the path
    f"mssql://u:{SECRET}/X@h/db",
    f"mssql://u:{SECRET}/a/b@h/db",
    f"sqlserver://u:{SECRET}/X@h;databaseName=d",
    f"mssql://h/db%40{SECRET}",
    # R-03b-T6h: a password with digits first and a raw ?, # or ;k=v ends the authority early (digits become the port)
    f"mssql://u:12345?{SECRET}@h/db",
    f"mssql://u:12345#{SECRET}@h/db",
    f"mssql://u:12345;{SECRET}=y@h/db",
    f"sqlserver://u:12345?{SECRET}@h:1433/db",
    f"mssql://u:12345;ab={SECRET}@h/db",
    f"mssql://u:12345?ab={SECRET}@h/db",
    # double encoding
    f"mssql://h/db?user=u%253Bpassword%253D{SECRET}",
    f"mssql://u%2526password%253D{SECRET}@h/db",
    f"mssql://h/db%253Bpassword%253D{SECRET}",
    f"sqlserver://h;databaseName=d%25253Bpassword%25253D{SECRET}",
])
def test_a_password_in_a_url_is_never_echoed(url, logs):
    try:
        parsed = parse_mssql_url(url)
    except Exception as exc:  # urllib's and pydantic's own errors quote the input: only ValidationFailed may come out
        assert type(exc) is ValidationFailed, type(exc)
        assert SECRET not in str(exc) and SECRET not in exc.message
    else:  # an import that goes through keeps the password out of every field that is stored in plain text
        assert SECRET not in parsed.profile.model_dump_json()
    assert SECRET not in "\n".join(logs)


def test_a_malformed_authority_says_what_to_do():
    for url in (f"mssql://root:{SECRET}＠db.example.com:1433/app", f"mssql://u:{SECRET}/x@host/db",
                f"mssql://u:{SECRET}@[::1/db"):
        with pytest.raises(ValidationFailed, match="Percent-encode"):
            parse_mssql_url(url)


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
        TdsSection("db.example.com", 1433).name, "me", "pw", "request")
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
    assert params.tds_section == TdsSection("db.example.com", 1433) and "/tmp/c.pem" not in str(params.kwargs)
    assert any("does not use client certificates" in line for line in logs)


def test_a_named_instance_is_refused_whichever_side_names_it():
    with pytest.raises(ConnectionFailed, match="Named instances"):
        connect_params(profile(host="db\\inst"), TUNNEL, None, None)  # through a tunnel the endpoint is local
    with pytest.raises(ConnectionFailed, match="Named instances"):
        connect_params(profile(), Endpoint("db\\inst", 1433), None, None)


# ---- fix round 1: mixed forms, braces, instances, aliases ----


@pytest.mark.parametrize("url", [
    "mssql://h/db?encrypt=true;password=x",  # the review's reproductions
    "sqlserver://h:1433?databaseName=d;user=u;password=x",
    "sqlserver://h;databaseName=d&encrypt=true",
    "sqlserver://h;databaseName=d?encrypt=true",
    "mssql://h/db&encrypt=true",
    "mssql://h/db?encrypt=true＆x=1",  # fullwidth ＆
])
def test_a_url_that_mixes_the_two_forms_is_refused_with_one_fixed_text(url):
    with pytest.raises(ValidationFailed) as info:
        parse_mssql_url(url)
    assert info.value.message == "This URL mixes the ?a=b&c=d and ;a=b forms. Use one form."


def test_only_the_password_may_hold_the_other_forms_separators():
    assert parse_mssql_url("mssql://u@h/db?password=a;b&encrypt=true").password == "a;b"
    assert parse_mssql_url("mssql://u@h/db?password=a?b&encrypt=true").password == "a?b"
    assert parse_mssql_url("sqlserver://h;password=a&b?c;user=u").password == "a&b?c"
    assert parse_mssql_url("sqlserver://h;PWD=a&b;user=u").password == "a&b"
    assert parse_mssql_url("sqlserver://h;password={a;b=c&d?e};user=u").password == "a;b=c&d?e"
    assert parse_mssql_url("mssql://u:a%3Bb%3Dc@h/db").password == "a;b=c"  # the authority's password: anything
    assert parse_mssql_url("mssql://h/db?password=a%3Bb%3Dc").password == "a;b=c"


@pytest.mark.parametrize("url", [  # R-03b-T6g (d): a percent-encoded separator is a separator in every part but the password
    "mssql://h?database=a%3Bb", "mssql://h?database=a%26b", "mssql://h?database=a%3Fb", "mssql://h?database=a%3Db",
    "mssql://h/a%26b", "mssql://h/a%3Bb", "mssql://h/a%3Db", "mssql://u%3Bx@h/db", "mssql://u%3Dx@h/db",
    "mssql://h/db?user=u%3Bpassword%3DX", "sqlserver://h;databaseName=d%3Bx", "sqlserver://h;user=u%26x",
    "mssql://h/db?user=u%EF%BC%9Bpassword%3DX",  # a percent-encoded fullwidth ；
    "mssql://h/db?applicationName=a%3Bb",  # even a key that is dropped
])
def test_percent_encoded_separators_are_refused_outside_the_password(url):
    with pytest.raises(ValidationFailed, match=REFUSED_AS_PAIR_OR_NAME):
        parse_mssql_url(url)


def test_a_fullwidth_semicolon_is_malformed_text_not_a_separator():
    with pytest.raises(ValidationFailed, match="malformed"):
        parse_mssql_url("sqlserver://h；databaseName=d")
    with pytest.raises(ValidationFailed, match="malformed"):
        parse_mssql_url("mssql://h/db?database=d；")


@pytest.mark.parametrize("url, password, user, database", [
    ("sqlserver://h;password={a;b=c};user=u", "a;b=c", "u", None),
    ("sqlserver://h;user=u;password={a;b=c}", "a;b=c", "u", None),
    ("sqlserver://h;password={a}}b};user=u", "a}b", "u", None),  # }} is a literal }
    ("sqlserver://h;password={}}};user=u", "}", "u", None),
    ("sqlserver://h;password={x&y?z};databaseName={my db}", "x&y?z", None, "my db"),
    ("sqlserver://h;password= {a;b} ;user=u", "a;b", "u", None),  # blanks around a braced value
    ("sqlserver://h;password={ spaced };user=u", " spaced ", "u", None),  # inside the braces: as written
    ("sqlserver://h;password={a;b}", "a;b", None, None),  # the last property
    ("sqlserver://h;password={{x};user=u", "{x", "u", None),  # a { inside is a plain character
])
def test_jdbc_brace_quoted_values(url, password, user, database):
    parsed = parse_mssql_url(url)
    assert (parsed.password, parsed.profile.username, parsed.profile.database) == (password, user, database)


@pytest.mark.parametrize("url", ["sqlserver://h;password={a;user=u", "sqlserver://h;password={a}b;user=u",
                                 "sqlserver://h;password={a} b;user=u"])
def test_malformed_braces_are_refused(url):
    with pytest.raises(ValidationFailed, match=r"not closed|key=value"):
        parse_mssql_url(url)


@pytest.mark.parametrize("url", [
    "sqlserver://h;instanceName=SQLEXPRESS", "sqlserver://h;INSTANCENAME=x;databaseName=d",
    "sqlserver://h;instance_name=x", "mssql://h/db?instanceName=SQLEXPRESS", "mssql://h/db?instance-name=x",
    "sqlserver://h;instanceName={SQL EXPRESS}",
    "sqlserver://h/SQLEXPRESS",  # go-mssqldb: the path is the instance
    "sqlserver://me@h/SQLEXPRESS?database=d", "sqlserver://h:1433/SQLEXPRESS;user=u",
    "sqlserver://h\\SQLEXPRESS?database=d",
])
def test_named_instances_are_refused(url):  # R-03b-T6c
    with pytest.raises(ValidationFailed, match="Named instances"):
        parse_mssql_url(url)


@pytest.mark.parametrize("url, database", [
    ("mssql://h/db", "db"), ("mssql+pymssql://h/db", "db"),  # SQLAlchemy: the path is the database
    ("sqlserver://h?database=db", "db"), ("sqlserver://h/?database=db", "db"), ("sqlserver://h;databaseName=db", "db"),
    ("sqlserver://h;instanceName=;databaseName=db", "db"),  # a blank instance asks for nothing
])
def test_where_the_path_is_the_database(url, database):
    assert parse_mssql_url(url).profile.database == database


def test_connection_string_aliases_of_user_and_password():
    jdbc = parse_mssql_url("sqlserver://h;User ID=me;Pwd=secret")
    assert (jdbc.profile.username, jdbc.password) == ("me", "secret")
    url = parse_mssql_url("mssql://h/db?uid=me&pwd=secret")
    assert (url.profile.username, url.password) == ("me", "secret")
    assert parse_mssql_url("sqlserver://h;UID=a;PWD=b").password == "b"
    assert parse_mssql_url("sqlserver://h;user_id=a;pass-word=b").profile.username == "a"  # one folded key
    both = parse_mssql_url("mssql://x:y@h/db?uid=a&pwd=b")
    assert (both.profile.username, both.password) == ("x", "y")  # the authority still wins


def test_tds_version_is_checked_at_import_with_fixed_text():
    for url in ("mssql://h/db?tds_version=8.0", "sqlserver://h;tdsVersion=7", "mssql://h/db?tds_version=7.4.1"):
        with pytest.raises(ValidationFailed) as info:
            parse_mssql_url(url)
        assert info.value.message == "tds_version must be one of 7.0-7.4"
    assert parse_mssql_url("mssql://h/db?tds_version=%207.3%20").profile.options == {"tds_version": "7.3"}
    assert parse_mssql_url("mssql://h/db?tds_version=").profile.options == {}  # blank asks for nothing


def test_connect_params_error_texts_never_hold_a_value():
    with pytest.raises(ConnectionFailed) as info:
        connect_params(profile(options={"tds_version": SECRET}), DIRECT, None, None)
    assert info.value.message == "tds_version must be one of 7.0-7.4"


# ---- fix round 1: DbaGuy's freetds.conf section for every login ----


def test_every_login_names_a_section_that_carries_the_target():
    ipv6 = connect_params(profile(host="::1"), Endpoint("::1", 1433), "pw", None)
    assert ipv6.tds_section == TdsSection("::1", 1433)
    # pymssql adds ":port" to a server string only when it has no ":"; an IPv6 host has some. The login names the
    # section instead (host and port are its lines), and the port is in the name FreeTDS looks up first.
    assert ipv6.kwargs["server"] == ipv6.tds_section.name and ipv6.kwargs["server"].endswith(":1433")
    assert ipv6.kwargs["port"] == "1433"
    tunnel = connect_params(profile(ssl=SslConfig(mode="verify-ca", ca_file="/tmp/ca.pem")), TUNNEL, None, None)
    assert tunnel.kwargs["server"] == tunnel.tds_section.name and tunnel.kwargs["port"] == "52001"
    assert tunnel.tds_section.host == "127.0.0.1"


def test_an_ipv6_host_and_a_section_without_a_ca_are_written_as_host_and_port_only(tmp_path):
    config = FreeTdsConfig(tmp_path / "freetds.conf")
    name = config.section_for(TdsSection("::1", 1433))
    text = config.path.read_text()
    section = text[text.index(f"[{name}]"):]
    assert "\thost = ::1\n" in section and "\tport = 1433\n" in section and "\ttds version = 7.4\n" in section
    # the login's own `encryption` argument decides: no CA, nothing to verify, no hostname check to ask for
    assert "ca file" not in section and "check certificate hostname" not in section and "encryption" not in section


@pytest.mark.parametrize("value", ["a#b", "a;b", "a  b", " a", "a ", "a\tb", "a\x0bb", "a  b"])
def test_values_freetds_conf_cannot_carry_are_refused_with_fixed_text(tmp_path, value):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    config = FreeTdsConfig(tmp_path / "freetds.conf")
    for target in (TdsSection(value, 1433), TdsSection("h", 1433, value, True)):
        with pytest.raises(ConnectionFailed) as info:
            config.section_for(target)
        assert info.value.message == (  # fixed text, whatever the value
            "The host name or CA file path holds a '#', a ';' or repeated white space, which freetds.conf cannot carry"
        )
    assert not config.path.exists()
    config.section_for(TdsSection("host with one space", 1433, str(ca), True))  # a single inner space is fine


def test_a_section_is_known_only_once_the_file_holds_it(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    config = FreeTdsConfig(blocker / "freetds.conf")
    target = TdsSection("h", 1433)
    with pytest.raises(ConnectionFailed, match="Cannot write DbaGuy's freetds.conf") as info:
        config.section_for(target)
    assert str(blocker) in info.value.message and isinstance(info.value.__cause__, OSError)
    assert config.render() == freetds._render({})  # nothing was registered by the failed write
    blocker.unlink()
    assert config.section_for(target) == target.name  # the retry writes it
    assert config.path.read_text().count(f"[{target.name}]") == 1


def test_an_unwritable_location_fails_the_first_login_not_the_app(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("FREETDSCONF", "before")
    monkeypatch.setattr(freetds, "_active", None)
    monkeypatch.setattr(freetds, "_location", None)
    freetds.use(blocker / "freetds.conf")
    with pytest.raises(ConnectionFailed, match="Cannot write DbaGuy's freetds.conf"):
        freetds.active()
    assert os.environ["FREETDSCONF"] == "before" and freetds._active is None
    blocker.unlink()
    assert freetds.active().path == blocker / "freetds.conf"  # the next login retries


def test_the_freetds_docstring_says_what_the_sections_are_for():
    assert "never reads another file" in freetds.__doc__ and "[global]" in freetds.__doc__


# ---- fix round 2 (R-03b-T6g): every part of a URL but the password is checked ----

NUMERIC_SECRET = "12345"  # all digits, so that it also reads as a port
REFUSED_AS_PAIR_OR_NAME = "mixes the|A name in this URL"


@pytest.mark.parametrize("url", [
    f"mssql://u:{NUMERIC_SECRET}/X@h/db",  # a "/" in the password ended the authority: host u, port 12345
    f"mssql://me:{NUMERIC_SECRET}/abc@h:1433/db",
    f"sqlserver://u:{NUMERIC_SECRET}/X@h;databaseName=d",
    f"mssql://u:{NUMERIC_SECRET}/a/b@h/db",
])
def test_a_numeric_password_with_a_slash_does_not_split_into_host_port_and_database(url, logs):
    with pytest.raises(ValidationFailed) as info:
        parse_mssql_url(url)
    assert NUMERIC_SECRET not in info.value.message and NUMERIC_SECRET not in "\n".join(logs)
    assert "malformed" in info.value.message  # the authority ended early: the message says what to do


@pytest.mark.parametrize("url", [
    "mssql://h/db?user=me?password=x", "sqlserver://h?databaseName=d?user=u?password=x",
    "mssql://h/db\uff1fpassword=x", "mssql://h/db?user=u\uff1fpassword=x", "mssql://h/db?user=u\u2047password=x",
    "mssql://h/db\u2047password=x", "mssql://h/db?database=d\ufe56x",
    "mssql://u&password=x@h/db", "mssql://u=x@h/db",
    "mssql://u\uff06x@h/db", "mssql://u%26x@h/db",
    "mssql://h/db?database=password=x", "sqlserver://h;databaseName=password=x", "sqlserver://h;serverName=a=b",
    "sqlserver://h;databaseName={d;password=x}", "sqlserver://h;user={u;password={x}",
    "sqlserver://h;user={u&x}", "sqlserver://h;databaseName={d?x}", "sqlserver://h;applicationName={a;b}",
])
def test_every_part_but_the_password_is_checked_for_separators(url):
    with pytest.raises(ValidationFailed, match=REFUSED_AS_PAIR_OR_NAME):
        parse_mssql_url(url)


@pytest.mark.parametrize("url", [
    "mssql://Password=x;Server=h", "sqlserver://Password=x", "mssql://Password=x", "mssql://Server=h:1433;Password=x",
    "mssql://h%3Dx/db", "mssql://h\uff1dx/db",  # = percent-encoded, fullwidth
])
def test_a_host_that_holds_an_assignment_is_refused_as_malformed(url):
    with pytest.raises(ValidationFailed, match="malformed"):
        parse_mssql_url(url)


@pytest.mark.parametrize("url", ["mssql://h/db%40x", "mssql://h/x@y", "mssql://h/x%40y", "mssql://h/x\uff20y"])
def test_an_at_sign_in_the_path_is_refused(url):
    with pytest.raises(ValidationFailed):
        parse_mssql_url(url)


def test_ordinary_names_still_import_after_the_stricter_checks():
    parsed = parse_mssql_url("mssql://me%40corp.com:p%40ss@db.example.com:1433/My%20Db?encrypt=true&trustServerCertificate=yes")
    assert (parsed.profile.username, parsed.profile.database, parsed.password) == ("me@corp.com", "My Db", "p@ss")
    domain = parse_mssql_url("sqlserver://h;user=CORP\\me;password=x;databaseName=sales-2026_q1")
    assert (domain.profile.username, domain.profile.database) == ("CORP\\me", "sales-2026_q1")
    assert parse_mssql_url("sqlserver://h;databaseName={my db};password={a}}b}").profile.database == "my db"
    assert parse_mssql_url("sqlserver://h;applicationName={abc==};databaseName=d").profile.database == "d"  # dropped key


# ---- fix round 3 (R-03b-T6h): a password with digits first, double encoding, clearer messages ----

NAME_MESSAGE = "A name in this URL holds ; & ? = or @. Type it in the connection dialog instead."
MIXED_MESSAGE = "This URL mixes the ?a=b&c=d and ;a=b forms. Use one form."
PREFIX_SECRET = "12345"  # digits first, so that it reads as a port once a raw ?, # or ;k=v ends the authority early
TAIL_SECRET = "Qz9tail"


@pytest.mark.parametrize("url", [
    f"mssql://u:{PREFIX_SECRET}?{TAIL_SECRET}@h/db",  # the review's four shapes first
    f"mssql://u:{PREFIX_SECRET}#{TAIL_SECRET}@h/db",
    f"mssql://u:{PREFIX_SECRET};{TAIL_SECRET}=y@h/db",
    f"sqlserver://u:{PREFIX_SECRET}?{TAIL_SECRET}@h:1433/db",
    f"mssql://me:{PREFIX_SECRET}?{TAIL_SECRET}=Y@h:1433/db",  # the value of a dropped key
    f"sqlserver://u:{PREFIX_SECRET};{TAIL_SECRET}=y@h:1433;databaseName=d",
    f"mssql://u:{PREFIX_SECRET};ab={TAIL_SECRET}@h/db",
    f"mssql://u:{PREFIX_SECRET}#frag?{TAIL_SECRET}@h/db",
    f"mssql://u:{PREFIX_SECRET}?%40{TAIL_SECRET}@h/db",
    f"mssql://u:{PREFIX_SECRET}?{TAIL_SECRET}＠h/db",  # a fullwidth ＠
    f"mssql://u:{PREFIX_SECRET}?{TAIL_SECRET}@h/db?encrypt=true",
    f"mssql://u:{PREFIX_SECRET}/{TAIL_SECRET}@h/db",  # round 2's slash, with a digits-first password too
    f"sqlserver://u:{PREFIX_SECRET}？{TAIL_SECRET}@h/db",
])
def test_a_password_that_starts_with_digits_never_lands_in_port_and_host(url, logs):
    with pytest.raises(ValidationFailed) as info:  # every one of them: no half-import with the digits as the port
        parse_mssql_url(url)
    for secret in (PREFIX_SECRET, TAIL_SECRET):
        assert secret not in info.value.message and secret not in "\n".join(logs)
    assert info.value.message in (MALFORMED_TEXT, INVALID_PORT_TEXT, MIXED_MESSAGE, NAME_MESSAGE)


MALFORMED_TEXT = "The host or port part of the URL is malformed. Percent-encode special characters in the user name and password."
INVALID_PORT_TEXT = "Invalid port in the SQL Server URL. Percent-encode special characters in the user name and password."


def test_the_password_and_the_user_may_hold_an_at_sign_and_so_may_a_database():
    url = parse_mssql_url("mssql://h/db?user=me@corp.com&password=p@ss")
    assert (url.profile.username, url.password, url.profile.database) == ("me@corp.com", "p@ss", "db")
    jdbc = parse_mssql_url("sqlserver://h;user=me@corp.com;password=p@ss;databaseName=sales@2026")
    assert (jdbc.profile.username, jdbc.password, jdbc.profile.database) == ("me@corp.com", "p@ss", "sales@2026")
    assert parse_mssql_url("mssql://h?database=a@b").profile.database == "a@b"
    braced = parse_mssql_url("sqlserver://h;user=me@corp.com;password={p@ss;1}")
    assert (braced.profile.username, braced.password) == ("me@corp.com", "p@ss;1")
    encoded = parse_mssql_url(f"mssql://me%40corp.com:{PREFIX_SECRET}%3F{TAIL_SECRET}@h/db")  # properly encoded: fine
    assert (encoded.profile.host, encoded.profile.username, encoded.password, encoded.profile.port) == (
        "h", "me@corp.com", f"{PREFIX_SECRET}?{TAIL_SECRET}", None)


@pytest.mark.parametrize("url", [
    "mssql://u:12345?X@h/db", "mssql://u:12345#X@h/db", "mssql://u:12345;X=y@h/db",
    "mssql://h/db#me@x", "mssql://h/db?x@y", "mssql://h/db?x@y=1", "sqlserver://h;x@y=1",
    "mssql://h/db?applicationName=me@x", "sqlserver://h;applicationName=me@x",  # the value of a dropped key
    "sqlserver://h;applicationName={me@x}", "sqlserver://h;serverName=a@b", "mssql://h/db?trustServerCertificate=a@b",
])
def test_an_at_sign_outside_the_names_and_the_password_is_malformed(url):
    with pytest.raises(ValidationFailed) as info:
        parse_mssql_url(url)
    assert info.value.message == MALFORMED_TEXT


@pytest.mark.parametrize("url", [  # R-03b-T6h (2): decoded again and again, a bounded number of times
    "mssql://h/db?user=u%253Bpassword%253DX", "mssql://h/db?user=u%25253Bx", "mssql://h?database=a%253Bb",
    "mssql://h/db%253Bx", "mssql://u%2526x@h/db", "sqlserver://h;databaseName=a%253Bb",
    "mssql://h/db?applicationName=a%253Bb", "mssql://h/db%2540x",
])
def test_double_encoded_separators_are_refused_outside_the_password(url):
    with pytest.raises(ValidationFailed, match=REFUSED_AS_PAIR_OR_NAME):
        parse_mssql_url(url)


def test_the_password_is_decoded_once_and_a_deep_encoding_ends():
    assert parse_mssql_url("mssql://h/db?password=a%253Bb").password == "a%3Bb"
    assert parse_mssql_url("mssql://u:a%253Bb@h/db").password == "a%3Bb"
    assert parse_mssql_url("sqlserver://h;password=a%253Bb").password == "a%253Bb"  # JDBC values are not decoded
    deep = "%" + "25" * 60 + "3B"  # far more levels than the check decodes: it stops, and imports what it is given
    assert parse_mssql_url(f"mssql://h?database={deep}").profile.database.startswith("%25")


@pytest.mark.parametrize("url", [
    "mssql://h/R%26D", "mssql://h/R&D", "mssql://h/a%3Bb", "mssql://h/a%3Fb", "mssql://h/a%3Db", "mssql://h/db%40x",
    "sqlserver://h;databaseName=a%3Bb", "sqlserver://h;databaseName=a%26b", "mssql://h?database=a%3Bb",
    "mssql://h?database=a=b", "mssql://h/db?user=u%3Bx", "mssql://u&x@h/db", "mssql://u%3Dx@h/db",
    "sqlserver://h;user={u&x}", "mssql://h/db?applicationName=a%3Bb",
])
def test_a_single_name_that_holds_a_separator_says_to_use_the_dialog(url):
    with pytest.raises(ValidationFailed) as info:
        parse_mssql_url(url)
    assert info.value.message == NAME_MESSAGE


@pytest.mark.parametrize("url", [
    "mssql://h/db?encrypt=true;password=x", "mssql://h/db?user=u%3Bpassword%3DX", "mssql://u&password=x@h/db",
    "sqlserver://h;databaseName=d&encrypt=true", "sqlserver://h;databaseName={d;password=x}", "mssql://h/db&encrypt=true",
    "mssql://h/db?x;password=x",  # a key that holds a separator
])
def test_a_pair_inside_a_value_says_the_forms_are_mixed(url):
    with pytest.raises(ValidationFailed) as info:
        parse_mssql_url(url)
    assert info.value.message == MIXED_MESSAGE
