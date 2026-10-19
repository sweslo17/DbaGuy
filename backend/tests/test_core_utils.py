import socket
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dbaguy_backend.adapters.base import DatabaseAdapter, ParsedUrl
from dbaguy_backend.adapters.positions import offset_to_line_col
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sockets import enable_keepalive
from dbaguy_backend.adapters.sql_text import first_keyword, qualified_pg, quote_ident_pg, quote_literal_pg
from dbaguy_backend.adapters.values import MAX_SAFE_INT, MISSING, TEXT_LIMIT, JsonText, encode_value
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.errors import UnsupportedEngine, ValidationFailed
from dbaguy_backend.domain.execution import ClientCommand, ServerEvent
from dbaguy_backend.domain.profiles import Engine, ServerProfileDraft, SqliteProfileDraft
from dbaguy_backend.sql.dialects import Dialect
from pydantic import TypeAdapter


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (42, 42),
        (MAX_SAFE_INT, MAX_SAFE_INT),
        (MAX_SAFE_INT + 1, str(MAX_SAFE_INT + 1)),
        (-(MAX_SAFE_INT + 1), str(-(MAX_SAFE_INT + 1))),
        (1.5, 1.5),
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        (Decimal("1.10"), "1.10"),
        ("中文", "中文"),
        (uuid.UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "2026-01-02T03:04:05+00:00"),
        ([1, 2], "[1, 2]"),
        ({"a": "中"}, '{"a": "中"}'),
    ],
)
def test_encode_value(value, expected):
    assert encode_value(value) == expected


def test_encode_bytes_and_long_text():
    assert encode_value(b"\xde\xad") == {"$type": "bytes", "size": 2, "preview": "dead"}
    assert encode_value(memoryview(b"\x01" * 100))["preview"] == "01" * 64
    long = "x" * (TEXT_LIMIT + 10)
    encoded = encode_value(long)
    assert encoded["$type"] == "text" and encoded["truncated"] is True
    assert encoded["size"] == TEXT_LIMIT + 10 and len(encoded["preview"]) == TEXT_LIMIT


def test_offset_to_line_col():
    assert offset_to_line_col("SELECT * FROM nope", 15) == (1, 15)
    assert offset_to_line_col("SELECT 1;\nSELECT * FROM nope", 25) == (2, 15)
    sql = "SELECT '中文'\nFROM nope"
    assert offset_to_line_col(sql, sql.index("nope") + 1) == (2, 6)
    assert offset_to_line_col("abc", 999) == (1, 4)


def test_error_columns_count_utf16_units():
    assert offset_to_line_col("SELECT '😀', nope", 13) == (1, 14)  # 'n' is the 13th character, 14th UTF-16 unit
    assert offset_to_line_col("a\n😀b", 4) == (2, 3)
    assert offset_to_line_col("SELECT x", 8) == (1, 8)


def test_quote_helpers():
    assert quote_ident_pg("users") == "users"
    assert quote_ident_pg("Users") == '"Users"'
    assert quote_ident_pg("order") == '"order"'
    assert quote_ident_pg("Order Items") == '"Order Items"'
    assert quote_ident_pg('a"b') == '"a""b"'
    assert qualified_pg("sales", "orders") == "sales.orders"
    assert quote_literal_pg("it's") == "'it''s'"


def test_first_keyword():
    assert first_keyword("  -- note\n /* block */ select 1") == "select"
    assert first_keyword("/* a /* b */ c */ select 1") == "select"  # PostgreSQL nests block comments
    assert first_keyword("(select 1)") == "("
    assert first_keyword("WITH x AS (SELECT 1) SELECT * FROM x") == "with"
    assert first_keyword("") == ""


def test_ws_unions_parse_by_type():
    command = TypeAdapter(ClientCommand).validate_python({"type": "execute", "request_id": "r1", "sql": "select 1"})
    assert command.page_size == 500
    event = TypeAdapter(ServerEvent).validate_python({"type": "finished", "request_id": "r1", "elapsed_ms": 3, "in_transaction": False})
    assert event.cancelled is False


class FakeAdapter(DatabaseAdapter):
    engine = Engine.SQLITE
    url_schemes = ("fake",)
    default_port = None
    dialect = Dialect.SQLITE

    def parse_url(self, url):
        return ParsedUrl(profile=SqliteProfileDraft(engine="sqlite", name="fake", path=url))

    def open(self, profile, endpoint, password, database):
        raise NotImplementedError


def test_registry_dispatch():
    registry = AdapterRegistry([FakeAdapter()])
    assert registry.get(Engine.SQLITE).engine == Engine.SQLITE
    assert registry.get("sqlite").url_schemes == ("fake",)
    assert registry.parse_url("fake:///tmp/x").profile.name == "fake"
    with pytest.raises(UnsupportedEngine):
        registry.get(Engine.POSTGRES)
    with pytest.raises(ValidationFailed):
        registry.parse_url("nope://x")
    assert registry.engines()[0].engine == Engine.SQLITE


def test_registry_strips_a_jdbc_prefix_before_choosing_the_adapter():
    seen = []

    class Recording(FakeAdapter):
        def parse_url(self, url):
            seen.append(url)
            return super().parse_url(url)

    registry = AdapterRegistry([Recording()])
    assert registry.parse_url("  JDBC:fake:///tmp/x ").profile.name == "fake"
    assert seen == ["fake:///tmp/x"]
    with pytest.raises(ValidationFailed):
        registry.parse_url("jdbc:nope://x")


def test_default_registry_reads_jdbc_urls():
    registry = default_registry()
    assert registry.parse_url("jdbc:postgresql://me@h:5433/app").profile.port == 5433
    assert registry.parse_url("jdbc:sqlite:/tmp/x.db").profile.path == "/tmp/x.db"
    mysql = registry.parse_url("jdbc:mysql://me@h:3307/app?useSSL=false").profile
    assert (mysql.engine, mysql.port, mysql.database, mysql.ssl.mode) == ("mysql", 3307, "app", "disable")
    mssql = registry.parse_url("jdbc:sqlserver://h:1444;databaseName=app;encrypt=false").profile
    assert (mssql.engine, mssql.port, mssql.database, mssql.ssl.mode) == ("mssql", 1444, "app", "prefer")


@pytest.mark.parametrize("url", [
    "postgres://h:abc/db",  # not a number
    "postgresql://h:99999999/db",  # out of range
    "postgres://[bad/db",  # malformed authority
    "jdbc:postgresql://h:abc/db",
    "mysql://[bad/db",
    "jdbc:mysql://[bad/db",
])
def test_default_registry_turns_a_malformed_url_into_a_validation_error(url):
    with pytest.raises(ValidationFailed, match="host or port part of the URL is malformed"):
        default_registry().parse_url(url)


def test_registry_turns_a_rejected_draft_into_a_validation_error_that_names_only_the_field():
    class Rejecting(FakeAdapter):
        def parse_url(self, url):
            # pydantic rejects it, and its own text quotes the offending input
            return ParsedUrl(profile=ServerProfileDraft(engine="postgres", name="x", host="h", database=["hunter2"]))

    with pytest.raises(ValidationFailed, match="invalid value for 'database'") as info:
        AdapterRegistry([Rejecting()]).parse_url("fake://h")
    assert "hunter2" not in info.value.message


@pytest.mark.parametrize("url", [
    "postgres://root:hunter2\uff20db.example.com:5432/app",  # a fullwidth "＠" typed by an IME
    "postgres://root:hunter2/x@db.example.com/app",  # an unencoded "/" cuts the authority short
])
def test_a_password_in_a_malformed_url_is_not_echoed(url):
    with pytest.raises(ValidationFailed, match="Percent-encode") as info:
        default_registry().parse_url(url)
    assert "hunter2" not in info.value.message


@pytest.mark.parametrize("pasted", [
    "password=hunter2 host=h:5432",  # a libpq key=value string
    "Password=hunter2;Server=tcp:h,1433",  # an ADO.NET connection string
])
def test_an_unsupported_paste_never_echoes_what_precedes_the_colon(pasted):
    with pytest.raises(ValidationFailed) as error:
        default_registry().parse_url(pasted)
    assert "hunter2" not in str(error.value)
    assert str(error.value) == "Unsupported URL scheme"


def test_an_unsupported_scheme_that_looks_like_one_is_named():
    with pytest.raises(ValidationFailed, match="Unsupported URL scheme 'redis'"):
        default_registry().parse_url("redis://h")


def test_encode_json_text_and_missing():
    assert encode_value(JsonText('{"a": 1e400}')) == {"$type": "json", "text": '{"a": 1e400}'}
    assert encode_value(MISSING) == {"$type": "missing"}
    long = JsonText("[" + "1," * TEXT_LIMIT + "1]")
    cell = encode_value(long)
    assert cell["$type"] == "text" and cell["truncated"] and cell["size"] == len(long.text)


def test_enable_keepalive_sets_the_timers():
    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    try:
        enable_keepalive(client)
        assert client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            assert client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE) == 30
    finally:
        client.close()
        server.close()


def test_missing_stays_the_singleton_through_copies_and_pickle():
    import copy
    import pickle

    from dbaguy_backend.adapters.values import MISSING, MissingType, encode_value

    for value in (MissingType(), copy.copy(MISSING), copy.deepcopy([MISSING])[0], pickle.loads(pickle.dumps(MISSING))):
        assert value is MISSING
        assert encode_value(value) == {"$type": "missing"}
