"""The copy matrix's tables (06c Task 5, spec ⑥ A9): one type-complete table per source engine, and how a value is
compared once copied — by what it means (its family), never by how each driver prints it: a decimal by its value, a
time zone's value by its instant and, where the target keeps it, its offset, a char by its text without the padding, a
float32 as a float32, JSON by its exact text. A value the copy changes on purpose (§3.5.3) compares as its conversion
says."""

import re
import struct
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

# Per source engine: the table (its {t}), its rows, and each column's family. Values the matrix copies everywhere: no
# PostgreSQL infinity, no MySQL zero date, no time past 24 hours (their own tests show their rows fail).
TYPES: dict[str, tuple[str, list[str], dict[str, str]]] = {
    "postgres": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b boolean, i2 smallint, i8 bigint, n numeric(38,10), nn numeric,
        m money, f4 real, f8 double precision, c char(5), vc varchar(10), t text, by bytea, d date, tm time(6),
        ts timestamp(6), tstz timestamptz, tmtz timetz, iv interval, u uuid, j json, jb jsonb, x xml, arr int[])""",
        ["""INSERT INTO {t} VALUES (1, true, -32768, 9223372036854775807, 1234567890123456789012345678.0123456789,
            1.10, 1234.56, 1.1, 0.1, 'ab', 'é😀''\\', 'line1
line2', '\\x00ff10'::bytea, '2026-10-05', '12:34:56.123456', '2026-10-05 12:34:56.123456',
            '2026-10-05 12:34:56.123456+08', '12:34:56+05:30', '1 day 02:03:04',
            'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '{{"b": 1.10, "a": [1, 2]}}', '{{"b": 1.10, "a": [1, 2]}}',
            '<a>x</a>', '{{1,2,NULL}}')""",
         "INSERT INTO {t} (id, vc, t) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, false, 32767, -9223372036854775808, -0.0000000001, 0, -0.01, -3.4e38,
            1e-300, '', ' x ', 'é\r\n', ''::bytea, '0001-01-01', '00:00:00', '1999-12-31 23:59:59',
            '1970-01-01 00:00:00-05:30',
            '23:59:59-08', '-1 days', '00000000-0000-0000-0000-000000000000', '[]', '{{}}', '<a>y</a>', '{{}}')"""],
        {"id": "int32", "b": "bool", "i2": "int16", "i8": "int64", "n": "decimal", "nn": "decimal", "m": "money",
         "f4": "float32", "f8": "float64", "c": "char", "vc": "varchar", "t": "text", "by": "binary", "d": "date",
         "tm": "time", "ts": "timestamp", "tstz": "timestamp_tz", "tmtz": "time_tz", "iv": "interval", "u": "uuid",
         "j": "json", "jb": "json", "x": "text", "arr": "other"},
    ),
    "mysql": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b tinyint(1), ti tinyint, i8 bigint, u8 bigint unsigned,
        n decimal(30,10), f4 float, f8 double, c char(5), vc varchar(10), t text, bn binary(4), vb varbinary(10),
        bl blob, d date, tm time(6), y year, dt datetime(6), ts timestamp(6) NULL, j json, e enum('a','b'),
        s set('x','y'), bt bit(4))""",
        ["""INSERT INTO {t} VALUES (1, 1, -128, 9223372036854775807, 18446744073709551615,
            12345678901234567890.0123456789, 0.1234567, 0.1, 'ab', 'é😀''\\\\', 'line1\\nline2', X'00ff', X'00ff',
            X'0001', '2026-10-05', '12:34:56.123456', 2026, '2026-10-05 12:34:56.123456', '2026-10-05 12:34:56.123456',
            '{{"b": 1.10, "a": [1, 2]}}', 'b', 'x,y', b'1010')""",
         "INSERT INTO {t} (id, vc, t) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, 0, 127, -9223372036854775808, 0, -0.0000000001, -1.5, 1e-300, '', ' x ',
            'é\\r\\n', X'', X'', X'', '1000-01-01', '00:00:00', 1901, '1999-12-31 23:59:59', '1970-01-02 00:00:00',
            '[]', 'a', '', b'0')"""],
        {"id": "int32", "b": "bool", "ti": "int16", "i8": "int64", "u8": "uint64", "n": "decimal", "f4": "float32",
         "f8": "float64", "c": "char", "vc": "varchar", "t": "text", "bn": "binary", "vb": "binary", "bl": "binary",
         "d": "date", "tm": "duration", "y": "year", "dt": "timestamp", "ts": "timestamp", "j": "json", "e": "text",
         "s": "text", "bt": "int64"},
    ),
    "mssql": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b bit, ti tinyint, i8 bigint, n decimal(38,10), m money,
        f4 real, f8 float, c char(5), nc nchar(5), vc varchar(10), nvc nvarchar(10), t nvarchar(max), bn binary(4),
        vb varbinary(10), d date, tm time(7), dt datetime, sdt smalldatetime, dt2 datetime2(7), dto datetimeoffset(7),
        u uniqueidentifier, x xml)""",
        ["""INSERT INTO {t} VALUES (1, 1, 255, 9223372036854775807, 1234567890123456789012345678.0123456789,
            1234.5678, 1.1, 0.1, 'ab', N'é😀', 'a''\\', N'é😀''\\', N'line1' + CHAR(10) + N'line2', 0x00ff,
            0x00ff,
            '2026-10-05', '12:34:56.123456', '2026-10-05T12:34:56.123', '2026-10-05T12:34:00',
            '2026-10-05T12:34:56.123456', '2026-10-05T12:34:56.123456+08:00', 'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11',
            N'<a>x</a>')""",
         "INSERT INTO {t} (id, vc, nvc, t) VALUES (2, '', N'', N'')",
         """INSERT INTO {t} VALUES (3, 0, 0, -9223372036854775808, -0.0000000001, -922337203685477.5808, -1.5,
            1e-300, '', N'', ' x ', N' é' + CHAR(13) + CHAR(10) + N' ', N'', 0x, 0x, '0001-01-01', '00:00:00',
            '1753-01-01T00:00:00', '1900-01-01T00:00:00', '0001-01-01T00:00:00', '1970-01-01T00:00:00-05:30',
            '00000000-0000-0000-0000-000000000000', N'<a/>')"""],
        {"id": "int32", "b": "bool", "ti": "int16", "i8": "int64", "n": "decimal", "m": "money", "f4": "float32",
         "f8": "float64", "c": "char", "nc": "char", "vc": "varchar", "nvc": "varchar", "t": "text", "bn": "binary",
         "vb": "binary", "d": "date", "tm": "time", "dt": "timestamp", "sdt": "timestamp", "dt2": "timestamp",
         "dto": "timestamp_tz", "u": "uuid", "x": "text"},
    ),
    "sqlite": (
        """CREATE TABLE {t} (id INTEGER PRIMARY KEY, i INTEGER, r REAL, tx TEXT, bl BLOB, nm NUMERIC, un,
        bo BOOLEAN, d DATE, dt DATETIME, j JSON, vc VARCHAR(10), dc DECIMAL(10,2))""",
        ["""INSERT INTO {t} VALUES (1, 9223372036854775807, 0.1, 'é😀''\\', X'00ff', 7, '1.10', 1, '2026-10-05',
            '2026-10-05 12:34:56', '{{"b": 1.10}}', 'abc', 12.30)""",
         "INSERT INTO {t} (id, tx, vc) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, -9223372036854775808, -1.5, ' x\r\ny ', X'', -2.5, 'text', 0, '1999-12-31',
            '1999-12-31 23:59:59', '[]', '', 0)"""],
        {"id": "int64", "i": "int64", "r": "float64", "tx": "text", "bl": "binary", "nm": "decimal", "un": "other",
         "bo": "bool", "d": "date", "dt": "timestamp", "j": "json", "vc": "varchar", "dc": "decimal"},
    ),
}
# SQL Server's 7th fractional digit (spec §3.5.3, 06c Task 1), which the six-digit times above do not reach: kept in
# full where the target holds it (SQL Server; SQLite's TEXT) and rounded by the target's server to six digits where it
# does not (PostgreSQL, MySQL), the preview saying so. No tie (servers round a half differently); row 2 carries into the
# next day and has an offset west of UTC (row 1's is east).
SEVENTH: tuple[str, list[str]] = (
    "CREATE TABLE {t} (id int PRIMARY KEY, tm time(7), dt2 datetime2(7), dto datetimeoffset(7))",
    ["INSERT INTO {t} VALUES (1, '12:34:56.1234567', '2026-10-05T12:34:56.1234567', "
     "'2026-10-05T12:34:56.1234567+05:30')",
     "INSERT INTO {t} VALUES (2, '00:00:00.0000001', '1999-12-31T23:59:59.9999996', "
     "'1970-01-01T00:00:00.0000004-05:30')"],
)
# What SQL Server (read as `CONVERT(varchar(40), …, 126)`: the driver itself reads microseconds) and SQLite give back
SEVENTH_KEPT: list[tuple[str, str, str]] = [
    ("12:34:56.1234567", "2026-10-05T12:34:56.1234567", "2026-10-05T12:34:56.1234567+05:30"),
    ("00:00:00.0000001", "1999-12-31T23:59:59.9999996", "1970-01-01T00:00:00.0000004-05:30"),
]
# What PostgreSQL and MySQL give back, as `canon` reads a time, a timestamp and a timestamp_tz: microseconds, rounded
SEVENTH_ROUNDED: list[tuple[int, datetime, datetime]] = [
    (45_296_123_457, datetime(2026, 10, 5, 12, 34, 56, 123457), datetime(2026, 10, 5, 7, 4, 56, 123457, tzinfo=UTC)),
    (0, datetime(2000, 1, 1), datetime(1970, 1, 1, 5, 30, tzinfo=UTC)),
]
# A backslash right before a line break, which a T-SQL string literal reads as a line continuation and removes with the
# break (found by this matrix, 06c Task 5 fix round 1: `quote_literal` writes it as it is, so SQL Server loses it);
# a lone CR is kept, a backslash alone or before a space too. From SQLite (no escapes to confuse) into each target.
BACKSLASH_BREAKS: tuple[str, list[str]] = (
    "CREATE TABLE {t} (id INTEGER PRIMARY KEY, tx TEXT)",
    ["INSERT INTO {t} VALUES (1, 'a\\\nb')", "INSERT INTO {t} VALUES (2, 'a\\\r\nb')",
     "INSERT INTO {t} VALUES (3, 'a\\\rb')", "INSERT INTO {t} VALUES (4, 'a\\b')",
     "INSERT INTO {t} VALUES (5, 'a\\ \nb')"],
)
_TIME = re.compile(r"(-)?(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")
_INTERVAL = re.compile(r"(?:(-?\d+) days?)?\s*(?:(-)?(\d+):(\d{2}):(\d{2})(?:\.(\d+))?)?")
# How a test reads a column with its engine's own driver where the driver's own type would hide the value: an array as
# PostgreSQL's text, money as numeric (psycopg has no money loader), JSON as text (psycopg parses it, which hides `1.10`
# and the order of the keys), a set and a bit as MySQL's text and number, a MySQL FLOAT as a DOUBLE (its text protocol
# sends 6 digits: the copy reads it so too, 06a pre-flight M7)
READ_AS: dict[str, dict[str, str]] = {
    "postgres": {"arr": "{}::text", "m": "{}::numeric", "j": "{}::text", "jb": "{}::text"},
    "mysql": {"s": "CAST({} AS CHAR)", "bt": "{} + 0", "j": "CAST({} AS CHAR)", "f4": "({} * 1E0)"},
}
_OFFSET = re.compile(r"([+-]\d{2})(?::?(\d{2}))?$")


def _micros(text: str) -> int:
    found = _TIME.fullmatch(text.strip())
    assert found is not None, text
    sign, hours, minutes, seconds, fraction = found.groups()
    micro = round(int((fraction or "0")[:7].ljust(7, "0")) / 10)
    total = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1_000_000 + micro
    return -total if sign else total


def _clock(value: Any) -> int:
    """A time of day or a duration as microseconds."""
    if isinstance(value, timedelta):
        return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    if isinstance(value, time):
        return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond
    text = str(value)
    return _micros(_OFFSET.sub("", text) if re.search(r"\d[+-]\d{2}(:?\d{2})?$", text) else text)


def _interval(value: Any) -> int:
    """PostgreSQL's interval (a timedelta, or its text `1 day 02:03:04`, `-1 days`) as microseconds."""
    if isinstance(value, timedelta):
        return _clock(value)
    found = _INTERVAL.fullmatch(str(value).strip())
    assert found is not None, value
    days, sign, hours, minutes, seconds, fraction = found.groups()
    clock = 0
    if hours is not None:
        clock = _micros(f"{hours}:{minutes}:{seconds}" + (f".{fraction}" if fraction else ""))
    return int(days or 0) * 86_400_000_000 + (-clock if sign else clock)


def _naive(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).replace("T", " ")
    head, _, fraction = text.partition(".")
    return datetime.fromisoformat(head + (f".{fraction[:6].ljust(6, '0')}" if fraction else ""))


def _instant(value: Any) -> datetime:
    """A time zone's value as its UTC instant; a naive one (MySQL's datetime, which the copy writes in UTC) is UTC."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).replace("T", " ")
    found = _OFFSET.search(text)
    if found is None:
        return _naive(text).replace(tzinfo=UTC)
    hours, minutes = int(found[1]), int(found[2] or 0) * (1 if found[1][0] == "+" else -1)
    local = _naive(text[:found.start()].strip())
    return (local - timedelta(hours=hours, minutes=minutes)).replace(tzinfo=UTC)


def _offset_minutes(value: Any) -> int | None:
    """The UTC offset a time or a date and time carries (a value or its text), in minutes; None when it carries none."""
    if isinstance(value, time | datetime):
        delta = value.utcoffset()
        return None if delta is None else delta // timedelta(minutes=1)
    found = _OFFSET.search(str(value).strip())
    if found is None:
        return None
    return (-1 if found[1][0] == "-" else 1) * (abs(int(found[1])) * 60 + int(found[2] or 0))


def compared_as(family: str, source: str, target: str) -> str:
    """The family a column is compared as once copied from `source` into `target` (§3.5.2, §3.5.3). A time zone's
    offset is part of the value where the target keeps it, so a lost one fails: a PostgreSQL `timetz` into PostgreSQL
    or SQLite's text, a SQL Server `datetimeoffset` into SQL Server or SQLite's text. Where the target drops it (a
    `timetz` into MySQL's TIME or SQL Server's time, with the preview's note) only the clock is compared; a PostgreSQL
    `timestamptz` has no offset of its own to keep (it reads in the session's zone)."""
    if family == "time_tz" and target in ("postgres", "sqlite"):
        return "time_tz_kept"
    if family == "timestamp_tz" and source == "mssql" and target in ("mssql", "sqlite"):
        return "timestamp_tz_kept"
    return family


# What MySQL's JSON column makes of another engine's JSON text, which the preview says (`MYSQL_JSON_NOTE`: it orders the
# keys, so `{"b": 1.10, "a": [1, 2]}` becomes `{"a": [1, 2], "b": 1.1}`, and rewrites decimals): the three rows' text
# the copy holds, by source engine and column. Every other target holds the source's JSON text as it is: PostgreSQL's
# json, SQL Server's and SQLite's text keep every character, and none of the rows has keys out of order for jsonb to
# reorder or a duplicate key for it to drop (MySQL's JSON gives its own text already normalised).
MYSQL_JSON: dict[tuple[str, str], list[str | None]] = {
    ("postgres", "j"): ['{"a": [1, 2], "b": 1.1}', None, "[]"],
    ("postgres", "jb"): ['{"a": [1, 2], "b": 1.1}', None, "{}"],
    ("sqlite", "j"): ['{"b": 1.1}', None, "[]"],
}


def expected(source: str, target: str, column: str, family: str, row: int, was: Any) -> Any:
    """What a copied value should read back as: the source's own value, except where the target changes it by design
    and the preview says so (MySQL's JSON)."""
    if family == "json" and target == "mysql" and source != "mysql":
        return MYSQL_JSON[(source, column)][row]
    return was


def canon(value: Any, family: str) -> Any:
    """What a value means, for comparing a source's value with its copy."""
    if value is None:
        return None
    match family:
        case "bool":
            return value.lower() in ("t", "true", "1") if isinstance(value, str) else bool(int(value))
        case "int16" | "int32" | "int64" | "uint64" | "year":
            return int.from_bytes(value, "big") if isinstance(value, bytes) else int(value)
        case "decimal" | "money":
            return Decimal(str(value)).normalize()
        case "float32":
            return struct.unpack("f", struct.pack("f", float(value)))[0]
        case "float64":
            return float(value)
        case "char":
            return str(value).rstrip(" ")
        case "binary":
            return bytes(value)
        case "date":
            return value.isoformat() if isinstance(value, date) else str(value)[:10]
        case "time" | "duration" | "time_tz":
            return _clock(value)
        case "time_tz_kept":
            return _clock(value), _offset_minutes(value)
        case "interval":
            return _interval(value)
        case "timestamp":
            return _naive(value)
        case "timestamp_tz":
            return _instant(value)
        case "timestamp_tz_kept":
            return _instant(value), _offset_minutes(value)
        case "uuid":
            return str(value).lower() if not isinstance(value, UUID) else str(value)
        case "json":  # the exact text: `1.10`, the order of the keys and every space count (READ_AS reads it as text)
            assert isinstance(value, str), f"JSON read back as {type(value).__name__}: read it as text (READ_AS)"
            return value
        case "other":
            return value.decode() if isinstance(value, bytes) else str(value)
        case _:
            return str(value)
