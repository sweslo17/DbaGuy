import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dbaguy_backend.adapters.base import DatabaseAdapter, ParsedUrl
from dbaguy_backend.adapters.positions import offset_to_line_col
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sql_text import first_keyword, qualified_pg, quote_ident_pg, quote_literal
from dbaguy_backend.adapters.values import MAX_SAFE_INT, TEXT_LIMIT, encode_value
from dbaguy_backend.domain.errors import UnsupportedEngine, ValidationFailed
from dbaguy_backend.domain.execution import ClientCommand, ServerEvent
from dbaguy_backend.domain.profiles import Engine, SqliteProfileDraft
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


def test_quote_helpers():
    assert quote_ident_pg("users") == "users"
    assert quote_ident_pg("Users") == '"Users"'
    assert quote_ident_pg("order") == '"order"'
    assert quote_ident_pg("Order Items") == '"Order Items"'
    assert quote_ident_pg('a"b') == '"a""b"'
    assert qualified_pg("sales", "orders") == "sales.orders"
    assert quote_literal("it's") == "'it''s'"


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
