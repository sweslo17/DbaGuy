from uuid import uuid4

import pytest
from loguru import logger
from mysql.connector.constants import ClientFlag

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.settings import connect_params, kill_params, parse_mysql_url, requires_tls
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.profiles import ServerProfile, SslConfig

DIRECT = Endpoint("db.example.com", 3306)
TUNNEL = Endpoint("127.0.0.1", 52001, tls_server_name="db.example.com")


def profile(**overrides) -> ServerProfile:
    data = {"id": uuid4(), "name": "m", "engine": "mysql", "host": "db.example.com", "database": "app", "username": "me"}
    return ServerProfile(**{**data, **overrides})


def test_parse_url():
    parsed = parse_mysql_url("mysql://me:p%40ss@db.example.com:3307/app?ssl-mode=REQUIRED&connection_timeout=5&serverTimezone=UTC")
    draft = parsed.profile
    assert (draft.engine, draft.host, draft.port, draft.database, draft.username) == ("mysql", "db.example.com", 3307, "app", "me")
    assert parsed.password == "p@ss" and draft.ssl.mode == "require"
    assert draft.options == {"connection_timeout": "5"}  # serverTimezone is JDBC-only: dropped
    jdbc = parse_mysql_url("mysql://db/app?user=u&password=x&useSSL=false")
    assert (jdbc.profile.username, jdbc.password, jdbc.profile.ssl.mode, jdbc.profile.options) == ("u", "x", "disable", {})
    alt = parse_mysql_url("mysql+pymysql://u@h/db?sslmode=verify-full&ssl-ca=/tmp/ca.pem")
    assert (alt.profile.ssl.mode, alt.profile.ssl.ca_file) == ("verify-full", "/tmp/ca.pem")
    assert parse_mysql_url("mysql://h/db?ssl_mode=VERIFY_IDENTITY").profile.ssl.mode == "verify-full"


def test_parse_url_details():
    bare = parse_mysql_url("mysql://h")
    assert (bare.profile.name, bare.profile.database, bare.profile.port, bare.profile.username, bare.password) == (
        "h", None, None, None, None)
    named = parse_mysql_url("mysql://h/my%20db")
    assert (named.profile.name, named.profile.database) == ("h/my db", "my db")
    # an explicit ssl-mode wins over the older useSSL flag; useSSL=true alone means require
    assert parse_mysql_url("mysql://h/d?useSSL=false&sslMode=PREFERRED").profile.ssl.mode == "prefer"
    assert parse_mysql_url("mysql://h/d?useSSL=true").profile.ssl.mode == "require"
    # the authority wins over the query for the same part
    both = parse_mysql_url("mysql://a:b@h/d?user=u&password=p")
    assert (both.profile.username, both.password) == ("a", "b")
    cert = parse_mysql_url("mysql://h/d?ssl-cert=/c.pem&ssl_key=/k.pem")
    assert (cert.profile.ssl.cert_file, cert.profile.ssl.key_file) == ("/c.pem", "/k.pem")


def test_parse_url_keeps_only_driver_options_and_never_the_connection_keys():
    parsed = parse_mysql_url(
        "mysql://good/db?host=evil&port=1&init_command=SET%20x%3D1&allow_local_infile=true&password1=x&ssl_ca=/x"
        "&get_warnings=true&charset=latin1&compress=true&nonsense=1"
    )
    assert parsed.profile.host == "good" and parsed.profile.port is None
    assert parsed.profile.options == {"charset": "latin1", "compress": "true"}
    assert parsed.password is None


@pytest.mark.parametrize("url, message", [
    ("mysql:///app", "needs a host"),
    ("mysql://h:abc/app", "port"),
    ("mysql://h:99999999/app", "port"),
    ("mysql://h:0/app", "port"),
    ("mysql://[bad/app", "host or port part of the URL is malformed"),
    ("mysql://h/app?ssl-mode=bogus", "ssl-mode"),
])
def test_parse_url_errors(url, message):
    with pytest.raises(ValidationFailed, match=message):
        parse_mysql_url(url)


def test_connect_params_keep_the_target_and_the_safety_settings():
    options = {"host": "evil", "init_command": "SET SESSION TRANSACTION READ WRITE", "allow_local_infile": "true",
               "connection_timeout": "5", "auth_plugin": "mysql_native_password", "compress": "true",
               "get_warnings": "true", "raise_on_warnings": "true"}
    params = connect_params(profile(options=options), DIRECT, "pw", None)
    assert (params["host"], params["port"], params["user"], params["password"], params["database"]) == (
        "db.example.com", 3306, "me", "pw", "app")
    assert "init_command" not in params and params["allow_local_infile"] is False
    assert "get_warnings" not in params and "raise_on_warnings" not in params  # R-03a-P7: warnings stay one message
    assert params["connection_timeout"] == 5 and params["auth_plugin"] == "mysql_native_password" and params["compress"] is True
    assert params["use_pure"] is True and params["autocommit"] is True
    assert params["client_flags"] == [-ClientFlag.MULTI_STATEMENTS]
    assert connect_params(profile(), DIRECT, None, "other")["database"] == "other"
    assert "database" not in connect_params(profile(database=None), DIRECT, None, None)


def test_the_drivers_other_spellings_of_reserved_keys_are_dropped_too(tmp_path):
    option_file = tmp_path / "my.cnf"
    option_file.write_text("[client]\ninit_command=SET @probe=1\nhost=evil\n")
    options = {"read_default_file": str(option_file), "username": "root", "webauthn_callback": "os.system"}
    params = connect_params(profile(options=options), DIRECT, "pw", None)
    assert not {"read_default_file", "username", "webauthn_callback", "option_files"} & params.keys()
    assert (params["user"], params["host"]) == ("me", "db.example.com")
    query = f"read_default_file={option_file}&username=root&webauthn_callback=os.system&compress=true"
    assert parse_mysql_url(f"mysql://good/db?{query}").profile.options == {"compress": "true"}


def test_option_values_are_coerced_and_reserved_keys_are_matched_case_insensitively():
    options = {"CHARSET": "latin1", "read_timeout": "-1", "use_unicode": "FALSE", "collation": "007x", "HOST": "evil",
               "Init_Command": "SELECT 1"}
    params = connect_params(profile(options=options), DIRECT, None, None)
    assert params["read_timeout"] == -1 and params["use_unicode"] is False and params["collation"] == "007x"
    assert params["CHARSET"] == "latin1"  # unknown to the driver as written: it rejects it at connect, not here
    assert params["host"] == "db.example.com"
    assert not {"HOST", "Init_Command", "init_command"} & params.keys()


def test_connect_timeout_is_the_driver_alias_of_connection_timeout():
    # mysql-connector drops `connect_timeout` when `connection_timeout` is also given, and a default always is
    assert connect_params(profile(options={"connect_timeout": "7"}), DIRECT, None, None)["connection_timeout"] == 7
    assert "connect_timeout" not in connect_params(profile(options={"connect_timeout": "7"}), DIRECT, None, None)


def test_reserved_options_are_logged_by_name_only():
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        connect_params(profile(options={"password": "hunter2-secret", "init_command": "SET x=1", "charset": "utf8mb4"}),
                       DIRECT, None, None)
        parse_mysql_url("mysql://h/d?passwd=hunter2-secret&serverTimezone=UTC")
    finally:
        logger.remove(sink)
    text = "".join(messages)
    assert "password" in text and "init_command" in text and "serverTimezone" in text
    assert "hunter2-secret" not in text and "SET x=1" not in text


@pytest.mark.parametrize("mode, expected", [
    ("disable", {"ssl_disabled": True}),
    ("prefer", {}),
    ("require", {}),
    ("verify-ca", {"ssl_verify_cert": True, "ssl_ca": "/tmp/ca.pem"}),
    ("verify-full", {"ssl_verify_cert": True, "ssl_verify_identity": True, "ssl_ca": "/tmp/ca.pem"}),
])
def test_tls_modes(mode, expected):
    params = connect_params(profile(ssl=SslConfig(mode=mode, ca_file="/tmp/ca.pem")), DIRECT, None, None)
    assert {k: v for k, v in params.items() if k.startswith("ssl_")} == expected
    assert requires_tls(SslConfig(mode=mode)) == (mode in ("require", "verify-ca", "verify-full"))


def test_tls_client_certificate_and_system_trust_store():
    ssl = SslConfig(mode="require", cert_file="~/c.pem", key_file="~/k.pem")
    params = connect_params(profile(ssl=ssl), DIRECT, None, None)
    assert params["ssl_cert"].endswith("/c.pem") and not params["ssl_cert"].startswith("~")
    assert params["ssl_key"].endswith("/k.pem") and not params["ssl_key"].startswith("~")
    no_ca = connect_params(profile(ssl=SslConfig(mode="verify-ca")), DIRECT, None, None)
    assert no_ca["ssl_verify_cert"] is True and "ssl_ca" not in no_ca  # the driver then uses the system trust store
    off = connect_params(profile(ssl=SslConfig(mode="disable", cert_file="/c.pem")), DIRECT, None, None)
    assert "ssl_cert" not in off and off["ssl_disabled"] is True


def test_tls_settings_that_cannot_work_fail_before_connecting():
    with pytest.raises(ConnectionFailed, match="SSH tunnel"):
        connect_params(profile(ssl=SslConfig(mode="verify-full")), TUNNEL, None, None)
    assert connect_params(profile(ssl=SslConfig(mode="verify-ca")), TUNNEL, None, None)["ssl_verify_cert"] is True
    with pytest.raises(ConnectionFailed, match="certificate and its key"):
        connect_params(profile(ssl=SslConfig(mode="require", cert_file="/tmp/c.pem")), DIRECT, None, None)
    with pytest.raises(ConnectionFailed, match="certificate and its key"):
        connect_params(profile(ssl=SslConfig(mode="verify-ca", key_file="/tmp/k.pem")), DIRECT, None, None)


def test_kill_params():
    params = connect_params(profile(), DIRECT, "pw", None)
    kill = kill_params(params)
    assert kill["connection_timeout"] == 3 and "database" not in kill and kill["password"] == "pw"
    assert "conn_attrs" not in kill and kill["client_flags"] == params["client_flags"]
    assert params["connection_timeout"] == 10  # the original is untouched
    assert kill["client_flags"] is not params["client_flags"]


def test_every_connection_gets_its_own_conn_attrs():
    # the driver adds its own entries to the dict it is given
    first = connect_params(profile(), DIRECT, None, None)
    first["conn_attrs"]["_client_name"] = "x"
    assert connect_params(profile(), DIRECT, None, None)["conn_attrs"] == {"program_name": "DbaGuy"}


@pytest.mark.parametrize("query, mode", [
    ("useSSL=true&requireSSL=true&verifyServerCertificate=true", "verify-ca"),
    ("verifyServerCertificate=true", "verify-ca"),
    ("VERIFYSERVERCERTIFICATE=TRUE&useSSL=true", "verify-ca"),
    ("useSSL=true&requireSSL=true", "require"),
    ("requireSSL=true", "require"),
    ("REQUIRE_SSL=True", "require"),
    ("useSSL=true", "require"),
    ("useSSL=yes&verifyServerCertificate=yes", "verify-ca"),  # Connector/J booleans also take yes/no
    ("requireSSL=YES", "require"),
    ("useSSL=no", "disable"),
    ("useSSL=true&verifyServerCertificate=false", "require"),
    ("useSSL=false&requireSSL=true&verifyServerCertificate=true", "disable"),
    ("useSSL=false", "disable"),
    ("requireSSL=false&verifyServerCertificate=false", "prefer"),  # nothing asked for: the default
    ("sslMode=PREFERRED&verifyServerCertificate=true&requireSSL=true", "prefer"),  # an explicit mode wins
])
def test_jdbc_tls_switches_never_import_weaker_than_asked(query, mode):
    parsed = parse_mysql_url(f"mysql://h/db?{query}")
    assert parsed.profile.ssl.mode == mode
    assert parsed.profile.options == {}  # the switches are consumed, not kept as driver options


def test_tls_files_and_the_libpq_spelling_of_the_ca():
    parsed = parse_mysql_url("mysql://h/db?sslmode=verify-ca&sslrootcert=/tmp/root.pem")
    assert (parsed.profile.ssl.mode, parsed.profile.ssl.ca_file) == ("verify-ca", "/tmp/root.pem")


def test_passwd_and_username_are_the_query_spellings_of_password_and_user():
    parsed = parse_mysql_url("mysql://h/db?username=u&passwd=x&connection_timeout=5")
    assert (parsed.profile.username, parsed.password, parsed.profile.options) == ("u", "x", {"connection_timeout": "5"})
    both = parse_mysql_url("mysql://a:b@h/db?username=u&passwd=x")
    assert (both.profile.username, both.password) == ("a", "b")


@pytest.mark.parametrize("url", [
    "mysql://h,h2/db",
    "mysql://h1:3306,h2:3307/db",
    "mysql://u:p@h1,h2:3306/db",
    "mysql://address=(host=h)(port=3306)/db",
    "mysql://u:p@address=(host=h)(port=3306)/db",
    "mysql://(host=h,port=3306)/db",
    "mysql://address=(host=h)(port=3306)(user=u)(password=hunter2@x)/db",
])
def test_host_lists_are_rejected_without_echoing_the_authority(url):
    with pytest.raises(ValidationFailed, match="one host") as info:
        parse_mysql_url(url)
    assert "hunter2" not in info.value.message and "host=h" not in info.value.message


def test_user_info_may_hold_the_characters_host_lists_use():
    parsed = parse_mysql_url("mysql://u:p(w,d@host/db")
    assert (parsed.profile.host, parsed.password) == ("host", "p(w,d")


@pytest.mark.parametrize("url", ["mysql://root:hunter2\uff20db.example.com:3306/app", "mysql://u:hunter2/x@host/db"])
def test_a_password_that_breaks_the_authority_is_never_echoed_and_the_message_says_what_to_do(url):
    with pytest.raises(ValidationFailed, match="Percent-encode") as info:
        parse_mysql_url(url)
    assert "hunter2" not in info.value.message


def test_more_options_the_driver_could_use_to_redirect_or_read_files_are_reserved():
    options = {"dns_srv": "true", "failover": "[{}]", "openid_token_file": "/etc/passwd", "compress": "true"}
    params = connect_params(profile(options=options), DIRECT, None, None)
    assert not {"dns_srv", "failover", "openid_token_file"} & params.keys() and params["compress"] is True
    query = "dns_srv=true&failover=x&openid_token_file=/etc/passwd&compress=true"
    assert parse_mysql_url(f"mysql://good/db?{query}").profile.options == {"compress": "true"}


def test_list_options_are_given_in_the_drivers_bracketed_string_form():
    params = connect_params(profile(options={"tls_versions": "[TLSv1.2,TLSv1.3]"}), DIRECT, None, None)
    assert params["tls_versions"] == "[TLSv1.2,TLSv1.3]"  # the driver parses this form itself
