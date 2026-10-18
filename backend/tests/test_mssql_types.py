from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from dbaguy_backend.adapters.mssql.types import ColumnKind, convert_row, described_kind, guessed_kinds


@pytest.mark.parametrize("value, expected", [
    (Decimal("0E-10"), "0.0000000000"),
    (Decimal("12345678901234567890.1234567890"), "12345678901234567890.1234567890"),
    (datetime(2026, 10, 18, 12, 34, 56, 123000), "2026-10-18 12:34:56.123"),
    (datetime(2026, 10, 18, 12, 34, 56), "2026-10-18 12:34:56"),
    (datetime(2026, 10, 18, 12, 34, 56, 123456), "2026-10-18 12:34:56.123456"),
    (datetime(2026, 10, 18, 12, 34, 56, 123456, tzinfo=timezone(timedelta(hours=8))), "2026-10-18 12:34:56.123456+08:00"),
    (date(2026, 10, 18), "2026-10-18"),
    (time(12, 34, 56, 123456), "12:34:56.123456"),
    (UUID("b9f3c1d2-0000-4000-8000-000000000001"), "B9F3C1D2-0000-4000-8000-000000000001"),
    (True, True), (9223372036854775807, 9223372036854775807), (1.5, 1.5), (b"*\x00\x00\x00", b"*\x00\x00\x00"),
    ("中文", "中文"), (None, None),
])
def test_conversion(value, expected):
    assert convert_row([value]) == [expected]


@pytest.mark.parametrize("name, category", [
    ("decimal(38,10)", "number"), ("bigint", "number"), ("money", "number"), ("bit", "bool"),
    ("datetimeoffset(7)", "datetime"), ("date", "datetime"), ("nvarchar(max)", "text"), ("uniqueidentifier", "text"),
    ("xml", "text"), ("varbinary(max)", "binary"), ("sql_variant", "binary"), ("timestamp", "binary"),
    ("geometry", "binary"), ("json", "json"), ("vector(3)", "other"),
])
def test_described_kinds(name, category):
    assert described_kind(name) == ColumnKind(name, category)


def test_guessed_kinds():
    header = [("a", 3), ("b", 2), ("c", 2), ("d", 1), ("e", 4), ("f", 2)]
    rows = [(None, None, b"x", None, None, None),
            (True, datetime(2026, 1, 1, tzinfo=timezone.utc), b"y", "s", None, None)]
    assert [(k.name, k.category) for k in guessed_kinds(header, rows)] == [
        ("bit", "bool"), ("datetimeoffset", "datetime"), ("varbinary", "binary"), ("nvarchar", "text"),
        ("datetime", "datetime"), ("unknown", "other"),
    ]
