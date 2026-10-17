import pytest
from mysql.connector.constants import FieldFlag, FieldType

from dbaguy_backend.adapters.mysql.types import column_meta, row_converter

UTF8MB4 = 255


def desc(field_type, flags=0, charset=UTF8MB4, name="c"):
    return (name, field_type, None, None, None, None, 1, flags, charset)


@pytest.mark.parametrize("description, raw, value, type_name, category", [
    (desc(FieldType.LONGLONG, FieldFlag.UNSIGNED), b"18446744073709551615", 18446744073709551615, "bigint unsigned", "number"),
    (desc(FieldType.TINY), b"1", 1, "tinyint", "number"),
    (desc(FieldType.DOUBLE), b"1.5", 1.5, "double", "number"),
    (desc(FieldType.NEWDECIMAL), b"12345678901234567890.1234567890", "12345678901234567890.1234567890", "decimal", "number"),
    (desc(FieldType.BIT, FieldFlag.UNSIGNED), b"\xa5", 165, "bit", "number"),
    (desc(FieldType.DATE), b"0000-00-00", "0000-00-00", "date", "datetime"),
    (desc(FieldType.TIME), b"-838:59:59", "-838:59:59", "time", "datetime"),
    (desc(FieldType.YEAR, FieldFlag.UNSIGNED | FieldFlag.ZEROFILL), b"2026", "2026", "year", "datetime"),
    (desc(FieldType.JSON), b'{"a": 1}', '{"a": 1}', "json", "json"),
    (desc(FieldType.BLOB, charset=63), b"\xde\xad", b"\xde\xad", "blob", "binary"),
    (desc(FieldType.BLOB), "中文".encode(), "中文", "text", "text"),
    (desc(FieldType.VAR_STRING, charset=63), b"\x01\x02", b"\x01\x02", "varbinary", "binary"),
    (desc(FieldType.STRING, FieldFlag.ENUM), b"happy", "happy", "enum", "text"),
    (desc(FieldType.STRING, FieldFlag.SET), b"a,b", "a,b", "set", "text"),
    (desc(FieldType.VAR_STRING), b"\xff", "�", "varchar", "text"),  # never raises on bad UTF-8
])
def test_conversion_table(description, raw, value, type_name, category):
    meta = column_meta(description)
    assert (meta.type_name, meta.category) == (type_name, category)
    assert row_converter([description])([raw]) == [value]
    assert row_converter([description])([None]) == [None]


def test_bytearray_input_is_converted_like_bytes():
    assert row_converter([desc(FieldType.LONG), desc(FieldType.VAR_STRING)])([bytearray(b"7"), bytearray(b"x")]) == [7, "x"]
