import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.couchbase.nodes import (
    DiscoveryFailed, QueryNode, candidate_nodes, discover, parse_node_services,
)
from dbaguy_backend.adapters.couchbase.settings import (
    connection_settings, endpoint_target, parse_couchbase_url, request_context, seed_hosts, tls_context,
)
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from tests.support import COUCHBASE_PASSWORD, couchbase_ca_file, make_couchbase_profile, requires_couchbase

AUTH = "Basic eA=="


def settings_of(**overrides):
    profile = make_couchbase_profile(**overrides)
    host, port = endpoint_target(profile)
    return connection_settings(profile, Endpoint(host, port), None)


def warnings_of(action):
    """The WARNING messages `action()` logged."""
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        action()
    finally:
        logger.remove(sink)
    return seen


@pytest.mark.parametrize("url, host, port, database, user, password, ssl_fields", [
    ("couchbase://cb1,cb2/travel-sample", "cb1,cb2", None, "travel-sample", None, None, ("disable", None)),
    ("couchbase://u:p%40ss@cb1:11210,cb2:11210/dbaguy.app", "cb1,cb2", None, "dbaguy.app", "u", "p@ss", ("disable", None)),
    ("couchbases://cb1/b?certpath=/tmp/ca.pem", "cb1", None, "b", None, None, ("verify-full", "/tmp/ca.pem")),
    ("couchbases://cb1?ssl=no_verify", "cb1", None, None, None, None, ("require", None)),
    ("couchbase://[::1]:8091/b", "[::1]", None, "b", None, None, ("disable", None)),
    ("http://h:8093", "h", 8093, None, None, None, ("disable", None)),
    ("HTTPS://user@h:18093/b.s", "h", 18093, "b.s", "user", None, ("verify-full", None)),
    ("couchbases://cb1/b?truststorepath=/tmp/t.pem&network=external", "cb1", None, "b", None, None, ("verify-full", "/tmp/t.pem")),
    # a port only on some hosts, a password with colons, a trailing slash, a back-ticked bucket whose name holds a dot
    ("couchbase://u:a:b:c@cb1,cb2:11210/", "cb1,cb2", None, None, "u", "a:b:c", ("disable", None)),
    ("couchbase://cb1/%60my.bucket%60", "cb1", None, "`my.bucket`", None, None, ("disable", None)),
    ("couchbase://cb1/b/", "cb1", None, "b", None, None, ("disable", None)),
    ("https://h", "h", None, None, None, None, ("verify-full", None)),
])
def test_url_import(url, host, port, database, user, password, ssl_fields):
    parsed = parse_couchbase_url(url)
    p = parsed.profile
    assert (p.engine, p.host, p.port, p.database, p.username, parsed.password, (p.ssl.mode, p.ssl.ca_file)) == (
        "couchbase", host, port, database, user, password, ssl_fields,
    )


def test_a_couchbase_url_never_gives_the_query_port():
    for url in ("couchbase://h:11210", "couchbase://h:8091", "couchbases://h:11207", "couchbase://a:8093,b:8093"):
        assert parse_couchbase_url(url).profile.port is None


def test_import_names_a_profile_after_its_first_host_and_database():
    assert parse_couchbase_url("couchbase://cb1,cb2/b").profile.name == "cb1/b"
    assert parse_couchbase_url("couchbase://[::1]").profile.name == "[::1]"


def test_dropped_url_parameters_are_named_never_their_values():
    seen = warnings_of(lambda: parse_couchbase_url("couchbase://u:secret@cb1/b?network=external&kv_timeout=secret2"))
    assert any("kv_timeout" in m and "network" in m for m in seen) and not any("secret" in m for m in seen)


def test_a_parameter_key_that_is_not_a_name_is_not_logged():
    seen = warnings_of(lambda: parse_couchbase_url("couchbase://cb1/b?pa$$w0rd%20x&ok_key=1"))
    assert any("ok_key" in m for m in seen) and not any("pa$$" in m or "w0rd" in m for m in seen)


@pytest.mark.parametrize("url", [
    "couchbase://", "couchbase:///b", "http://a,b:8093", "couchbase://h:x/b",
    "couchbase://h:70000/b", "couchbase://h:0", "couchbase://h:\u00b2/b", "couchbase://\uff18\uff10:\uff18\uff10/b",
    "couchbase://[::1/b", "couchbase://[::1]x/b", "couchbase://::1/b", "couchbase://[]/b", "couchbase://a,,b/b",
    "couchbase://h/b/c", "https://h:18093/ui/index.html", "cb://h", "h:8093",
])
def test_malformed_urls(url):
    with pytest.raises(ValidationFailed):
        parse_couchbase_url(url)


@pytest.mark.parametrize("url", [
    "couchbase://u:secret@[::1/b", "couchbase://u:secret@h:x/b", "couchbase://u:secret@a,,b/b",
    "http://u:secret@a,b:8093", "couchbase://u:secret@h/b/c",
    "u:secret@h", "couchbase://u:secret@h:99999",
])
def test_a_refused_url_never_repeats_its_text(url):
    with pytest.raises(ValidationFailed) as refused:
        parse_couchbase_url(url)
    assert "secret" not in refused.value.message and "h:x" not in refused.value.message


def test_seed_hosts():
    assert seed_hosts(" cb1, cb2 ") == ["cb1", "cb2"]
    assert seed_hosts("[::1],cb2") == ["::1", "cb2"]
    for empty in ("a,,b", "a,", ",a", " ", "[]", "a,[ ]"):
        with pytest.raises(ValidationFailed):
            seed_hosts(empty)


def test_endpoint_target():
    assert endpoint_target(make_couchbase_profile(host="cb1,cb2")) == ("cb1", 8093)
    assert endpoint_target(make_couchbase_profile(host="cb1", ssl=SslConfig(mode="verify-ca"))) == ("cb1", 18093)
    assert endpoint_target(make_couchbase_profile(host="cb1", port=9000)) == ("cb1", 9000)
    ssh = SshConfig(enabled=True, host="bastion")
    assert endpoint_target(make_couchbase_profile(host="cb1", ssh=ssh)) == ("cb1", 8093)
    with pytest.raises(ValidationFailed, match="one node"):
        endpoint_target(make_couchbase_profile(host="cb1,cb2", ssh=ssh))


def test_the_http_layer_gets_ipv6_hosts_without_brackets():  # R-04a-C18
    assert endpoint_target(make_couchbase_profile(host="[::1]")) == ("::1", 8093)
    assert settings_of(host="[fd00::1],cb2").seeds == ("fd00::1", "cb2")


@pytest.mark.parametrize("mode, verify, hostname", [
    ("require", ssl.CERT_NONE, False), ("verify-ca", ssl.CERT_REQUIRED, False), ("verify-full", ssl.CERT_REQUIRED, True),
])
def test_tls_contexts(mode, verify, hostname):
    context = tls_context(SslConfig(mode=mode))
    assert (context.verify_mode, context.check_hostname) == (verify, hostname)


def test_tls_settings_that_cannot_work():
    assert tls_context(SslConfig(mode="disable")) is None and tls_context(SslConfig(mode="prefer")) is None
    with pytest.raises(ConnectionFailed):
        tls_context(SslConfig(mode="verify-ca", ca_file="/nonexistent/ca.pem"))
    with pytest.raises(ConnectionFailed, match="key"):
        tls_context(SslConfig(mode="verify-full", cert_file="/tmp/c.pem"))


def test_a_tls_file_error_names_the_file_kind_never_the_path(tmp_path):
    broken = tmp_path / "secret-dir-name.pem"
    broken.write_text("not a certificate")
    cases = [
        SslConfig(mode="verify-ca", ca_file=str(tmp_path / "secret-dir-name-missing.pem")),
        SslConfig(mode="verify-ca", ca_file=str(broken)),
        SslConfig(mode="require", cert_file=str(broken), key_file=str(broken)),
    ]
    messages = []
    for config in cases:
        with pytest.raises(ConnectionFailed) as failed:
            tls_context(config)
        messages.append(failed.value.message)
    assert not any("secret-dir-name" in m for m in messages)
    assert "CA" in messages[0] and "CA" in messages[1] and "client" in messages[2]


def test_a_client_key_with_a_passphrase_is_refused_without_a_prompt(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "client.pem", tmp_path / "client.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=test", "-passout", "pass:hunter2"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    with pytest.raises(ConnectionFailed, match="passphrase") as refused:  # OpenSSL would ask on the terminal otherwise
        tls_context(SslConfig(mode="require", cert_file=str(cert), key_file=str(key)))
    assert "hunter2" not in refused.value.message


def test_a_ca_file_means_nothing_without_certificate_checks():
    context = tls_context(SslConfig(mode="require", ca_file="/nonexistent/ca.pem"))  # never read
    assert context.verify_mode == ssl.CERT_NONE


def test_discovery_rules():
    assert settings_of().discover and settings_of(port=8093).discover
    assert not settings_of(port=9000).discover
    assert not settings_of(options={"discover_nodes": "false"}).discover
    assert settings_of(options={"manager_port": "9091"}).manager_port == 9091
    assert settings_of(ssl=SslConfig(mode="require")).manager_port == 18091
    tunnel = connection_settings(
        make_couchbase_profile(host="cb1", ssh=SshConfig(enabled=True, host="bastion"), ssl=SslConfig(mode="verify-full")),
        Endpoint("127.0.0.1", 54321, tls_server_name="cb1"), None,
    )
    assert (tunnel.seeds, tunnel.port, tunnel.server_hostname, tunnel.discover, tunnel.tunnel) == (
        ("127.0.0.1",), 54321, "cb1", False, True,
    )


def test_a_tunnel_never_discovers_even_when_its_local_port_is_the_default():
    for port, mode in ((8093, "disable"), (18093, "verify-ca")):
        profile = make_couchbase_profile(host="cb1", ssh=SshConfig(enabled=True, host="bastion"), ssl=SslConfig(mode=mode))
        assert not connection_settings(profile, Endpoint("127.0.0.1", port, tls_server_name="cb1"), None).discover


def test_the_default_port_depends_on_the_tls_mode_for_discovery():
    assert not settings_of(ssl=SslConfig(mode="require"), port=8093).discover  # 8093 is plain HTTP; not this mode's port
    assert settings_of(ssl=SslConfig(mode="require"), port=18093).discover
    assert settings_of(ssl=SslConfig(mode="prefer")).discover and settings_of(ssl=SslConfig(mode="prefer")).port == 8093


def test_settings_carry_the_profile_facts():
    secure = settings_of(read_only=True, username="reader", ssl=SslConfig(mode="verify-ca"))
    assert (secure.read_only, secure.username, secure.tls_mode, secure.port, secure.tls is not None) == (
        True, "reader", "verify-ca", 18093, True,
    )
    plain = settings_of(ssl=SslConfig(mode="prefer"))
    assert (plain.read_only, plain.tls_mode, plain.tls, plain.tunnel, plain.server_hostname) == (
        False, "prefer", None, False, None,
    )


def test_options_and_context():
    assert settings_of().default_context == QueryContext(bucket="dbaguy", scope="app")
    assert connection_settings(make_couchbase_profile(), Endpoint("127.0.0.1", 8093), "travel-sample").default_context == (
        QueryContext(bucket="travel-sample")
    )
    assert settings_of(database=None).default_context is None
    for bad in ({"manager_port": "x"}, {"discover_nodes": "maybe"}, {"manager_port": "70000"}, {"manager_port": "0"},
                {"manager_port": "\u00b2"}):
        with pytest.raises(ConnectionFailed):
            settings_of(options=bad)
    with pytest.raises(ConnectionFailed, match="query context"):
        settings_of(database="`unclosed")
    with pytest.raises(ConnectionFailed, match="user name"):
        settings_of(username=None)
    with pytest.raises(ConnectionFailed, match="user name"):
        settings_of(username="  ")
    seen = warnings_of(lambda: settings_of(options={"kv_timeout": "5"}))
    assert any("kv_timeout" in m for m in seen)
    assert request_context(QueryContext(bucket="travel-sample", scope="inventory")) == "default:`travel-sample`.inventory"


def test_option_values_are_never_echoed():
    for options in ({"manager_port": "hunter2"}, {"discover_nodes": "hunter2"}):
        with pytest.raises(ConnectionFailed) as bad:
            settings_of(options=options)
        assert "hunter2" not in bad.value.message and next(iter(options)) in bad.value.message
    with pytest.raises(ConnectionFailed) as bad:
        settings_of(database="`hunter2")
    assert "hunter2" not in bad.value.message
    seen = warnings_of(lambda: settings_of(options={"kv_timeout": "hunter2"}))
    assert not any("hunter2" in m for m in seen)


def test_option_spellings():
    assert not settings_of(options={"discover_nodes": " FALSE "}).discover
    assert settings_of(options={"Discover_Nodes": "True"}).discover
    assert settings_of(options={"MANAGER_PORT": " 9091"}).manager_port == 9091
    # a blank value is "not set" (the dialog leaves a field empty), not an error
    blank = settings_of(options={"manager_port": "", "discover_nodes": " "})
    assert (blank.manager_port, blank.discover) == (8091, True)
    assert warnings_of(lambda: settings_of(options={"manager_port": "9091", "discover_nodes": "true"})) == []


SERVICES = {"nodesExt": [
    {"services": {"mgmt": 8091, "kv": 11210}, "hostname": "kv-only"},
    {"services": {"n1ql": 8093, "n1qlSSL": 18093}, "hostname": "q2"},
    {"services": {"n1ql": 8093, "n1qlSSL": 18093, "mgmt": 8091}, "thisNode": True},
]}


def test_query_nodes_with_the_asked_node_first():
    assert parse_node_services(SERVICES, "seed", tls=False) == [QueryNode("seed", 8093), QueryNode("q2", 8093)]
    assert parse_node_services(SERVICES, "seed", tls=True) == [QueryNode("seed", 18093), QueryNode("q2", 18093)]


def test_the_asked_node_is_found_by_name_too():
    body = {"nodesExt": [
        {"services": {"n1ql": 8093}, "hostname": "q1"},
        {"services": {"n1ql": 8093}, "hostname": "q2"},
        {"services": {"n1ql": 8093}, "hostname": "q3"},
    ]}
    assert parse_node_services(body, "q2", tls=False) == [QueryNode("q2", 8093), QueryNode("q1", 8093), QueryNode("q3", 8093)]
    assert parse_node_services(body, "elsewhere", tls=False) == [QueryNode("q1", 8093), QueryNode("q2", 8093), QueryNode("q3", 8093)]


def test_external_addresses_when_the_seed_is_one():
    body = {"nodesExt": [
        {"services": {"n1ql": 8093}, "hostname": "10.0.0.1", "thisNode": True,
         "alternateAddresses": {"external": {"hostname": "ext1", "ports": {"n1ql": 31093}}}},
        {"services": {"n1ql": 8093}, "hostname": "10.0.0.2",
         "alternateAddresses": {"external": {"hostname": "ext2", "ports": {"n1ql": 32093}}}},
    ]}
    assert parse_node_services(body, "ext1", tls=False) == [QueryNode("ext1", 31093), QueryNode("ext2", 32093)]
    assert parse_node_services(body, "10.0.0.1", tls=False) == [QueryNode("10.0.0.1", 8093), QueryNode("10.0.0.2", 8093)]
    assert QueryNode("fd00::1", 8093).label == "[fd00::1]:8093"
    assert QueryNode("cb1", 8093).label == "cb1:8093"


def test_an_external_address_without_ports_keeps_the_internal_ones():
    """Couchbase lists only the ports that differ in `alternateAddresses.external.ports`."""
    body = {"nodesExt": [
        {"services": {"n1ql": 8093, "n1qlSSL": 18093}, "hostname": "10.0.0.1",
         "alternateAddresses": {"external": {"hostname": "ext1"}}},
        {"services": {"n1ql": 8093, "n1qlSSL": 18093}, "hostname": "10.0.0.2", "thisNode": True,
         "alternateAddresses": {"external": {"hostname": "ext2", "ports": {"n1qlSSL": 31094}}}},
    ]}
    assert parse_node_services(body, "ext2", tls=False) == [QueryNode("ext2", 8093), QueryNode("ext1", 8093)]
    assert parse_node_services(body, "ext2", tls=True) == [QueryNode("ext2", 31094), QueryNode("ext1", 18093)]


def test_nodes_that_cannot_be_used_are_skipped():
    body = {"nodesExt": [
        {"services": {"n1ql": "8093"}, "hostname": "string-port"},
        {"services": {"n1ql": True}, "hostname": "bool-port"},
        {"services": {"n1ql": 0}, "hostname": "zero-port"},
        {"services": {"n1ql": 70000}, "hostname": "big-port"},
        {"services": {"n1ql": 8093}, "hostname": 7},
        {"services": {"n1ql": 8093}, "hostname": ""},
        {"services": "none", "hostname": "no-services"},
        "not an entry",
        {"services": {"n1ql": 8093}, "hostname": "[fd00::2]"},
        {"services": {"n1ql": 8093}, "hostname": "good"},
    ]}
    assert parse_node_services(body, "seed", tls=False) == [QueryNode("fd00::2", 8093), QueryNode("good", 8093)]


@pytest.mark.parametrize("body", [None, [], "x", {}, {"nodesExt": None}, {"nodesExt": {}}, {"nodes": []}])
def test_a_body_that_is_not_a_node_services_map(body):
    with pytest.raises(ValueError):
        parse_node_services(body, "seed", tls=False)


def test_a_map_without_a_query_service_gives_no_nodes():
    assert parse_node_services({"nodesExt": [{"services": {"kv": 11210}, "hostname": "a"}]}, "a", tls=False) == []
    assert parse_node_services({"nodesExt": []}, "a", tls=False) == []


def test_discovery_tries_each_seed_then_reports():
    def fetch(host, port):
        if host == "a":
            raise ConnectionRefusedError()
        if host == "b":
            raise DiscoveryFailed("HTTP 401")
        return json.dumps(SERVICES).encode()

    assert discover(["a", "c"], 8091, tls=None, auth=AUTH, fetch=fetch) == ([QueryNode("c", 8093), QueryNode("q2", 8093)], None)
    assert discover(["a", "b"], 8091, tls=None, auth=AUTH, fetch=fetch) == ([], "a: refused; b: HTTP 401")
    nodes, how = candidate_nodes(settings_of(host="a,b"), AUTH, fetch=fetch)
    assert nodes == [QueryNode("a", 8093), QueryNode("b", 8093)] and "discovery failed" in how
    assert candidate_nodes(settings_of(host="x", port=9000), AUTH, fetch=fetch)[0] == [QueryNode("x", 9000)]


def test_discovery_asks_the_manager_port_of_each_seed_and_reads_the_tls_ports_with_a_context():
    asked = []

    def fetch(host, port):
        asked.append((host, port))
        return json.dumps(SERVICES).encode()

    nodes, how = candidate_nodes(settings_of(host="a,b", ssl=SslConfig(mode="require")), AUTH, fetch=fetch)
    assert asked == [("a", 18091)] and nodes == [QueryNode("a", 18093), QueryNode("q2", 18093)]
    assert how == "2 query node(s) from the cluster map"
    asked.clear()
    candidate_nodes(settings_of(host="a,b", options={"manager_port": "9091"}), AUTH, fetch=fetch)
    assert asked == [("a", 9091)]


@pytest.mark.parametrize("answer", [
    b"", b"not json", b"\xff\xfe", b"[]", b'{"nodesExt": 3}', b'{"nodesExt": [{"services": {"kv": 1}, "hostname": "a"}]}',
])
def test_an_answer_that_is_no_node_map_falls_back_to_the_seeds(answer):
    nodes, how = candidate_nodes(settings_of(host="a,b"), AUTH, fetch=lambda host, port: answer)
    assert nodes == [QueryNode("a", 8093), QueryNode("b", 8093)] and how.startswith("the given hosts (discovery failed: ")


def test_a_discovery_failure_is_logged_as_info_with_the_reason():
    seen = []
    sink = logger.add(lambda m: seen.append((m.record["level"].name, m.record["message"])), level="INFO")
    try:
        candidate_nodes(settings_of(host="a"), AUTH, fetch=lambda host, port: (_ for _ in ()).throw(TimeoutError()))
    finally:
        logger.remove(sink)
    assert [(level, message) for level, message in seen if "discovery failed" in message] == [
        ("INFO", "Couchbase node discovery failed (a: timed out); trying the given hosts"),
    ]


def test_no_discovery_through_a_tunnel_or_on_a_port_of_ones_own():
    def fetch(host, port):
        raise AssertionError("asked the manager")

    tunnel = connection_settings(
        make_couchbase_profile(host="cb1", ssh=SshConfig(enabled=True, host="bastion")),
        Endpoint("127.0.0.1", 54321, tls_server_name="cb1"), None,
    )
    nodes, how = candidate_nodes(tunnel, AUTH, fetch=fetch)
    assert (nodes, how) == ([QueryNode("127.0.0.1", 54321)], "the SSH tunnel")
    nodes, how = candidate_nodes(settings_of(host="lb", port=9000), AUTH, fetch=fetch)
    assert (nodes, how) == ([QueryNode("lb", 9000)], "the given hosts (no discovery)")
    assert candidate_nodes(settings_of(host="lb", options={"discover_nodes": "false"}), AUTH, fetch=fetch)[0] == [
        QueryNode("lb", 8093),
    ]


class _ManagerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.requests.append((self.path, self.headers.get("Authorization")))
        status, body = server.answer
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Manager(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that stops reading (the answer was too large) is no news
        pass


@pytest.fixture
def manager():
    server = _Manager(("127.0.0.1", 0), _ManagerHandler)
    server.requests, server.answer = [], (200, json.dumps(SERVICES).encode())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_the_default_fetch_sends_basic_auth_to_the_node_services_path(manager):
    port = manager.server_address[1]
    found = discover(["127.0.0.1"], port, tls=None, auth=AUTH)
    assert found == ([QueryNode("127.0.0.1", 8093), QueryNode("q2", 8093)], None)
    assert manager.requests == [("/pools/default/nodeServices", AUTH)]


def test_the_default_fetch_reports_a_status_that_is_not_200(manager):
    manager.answer = (401, b"Unauthorized")
    assert discover(["127.0.0.1"], manager.server_address[1], tls=None, auth=AUTH) == ([], "127.0.0.1: HTTP 401")


def test_the_default_fetch_does_not_read_an_endless_answer(manager):
    manager.answer = (200, b" " * (2 * 1024 * 1024))
    nodes, reason = discover(["127.0.0.1"], manager.server_address[1], tls=None, auth=AUTH)
    assert nodes == [] and "too large" in reason


@requires_couchbase
def test_discovery_against_the_dev_cluster(tmp_path):  # A8 (one node)
    from dbaguy_backend.adapters.couchbase.http import basic_auth

    auth = basic_auth("dbaguy", COUCHBASE_PASSWORD)
    assert candidate_nodes(settings_of(), auth)[0] == [QueryNode("127.0.0.1", 8093)]
    tls = settings_of(ssl=SslConfig(mode="verify-ca", ca_file=str(couchbase_ca_file(tmp_path))))
    assert candidate_nodes(tls, auth)[0] == [QueryNode("127.0.0.1", 18093)]


@requires_couchbase
def test_discovery_with_a_wrong_password_falls_back_to_the_seeds():
    from dbaguy_backend.adapters.couchbase.http import basic_auth

    nodes, how = candidate_nodes(settings_of(), basic_auth("dbaguy", "wrong"))
    assert nodes == [QueryNode("127.0.0.1", 8093)] and "HTTP 401" in how


@requires_couchbase
def test_a_limited_user_discovers_nodes_too():
    from dbaguy_backend.adapters.couchbase.http import basic_auth

    nodes, how = candidate_nodes(settings_of(username="dbaguy_reader"), basic_auth("dbaguy_reader", COUCHBASE_PASSWORD))
    assert nodes == [QueryNode("127.0.0.1", 8093)] and how == "1 query node(s) from the cluster map"


@requires_couchbase
@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_the_tls_context_trusts_the_cluster_ca_for_loopback(mode, tmp_path):  # A11: the SAN holds 127.0.0.1
    import socket

    config = SslConfig(mode=mode, ca_file=str(couchbase_ca_file(tmp_path)))
    context = tls_context(config)
    with socket.create_connection(("127.0.0.1", 18093), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            assert tls.version() is not None


@requires_couchbase
def test_the_system_cas_do_not_trust_the_cluster_certificate():
    import socket

    context = tls_context(SslConfig(mode="verify-ca"))
    with socket.create_connection(("127.0.0.1", 18093), timeout=10) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            context.wrap_socket(raw, server_hostname="127.0.0.1")

