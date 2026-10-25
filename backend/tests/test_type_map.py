"""Copy's type map and value conversions (spec ⑥ §3.5.2–3.5.3, 06c Task 1): pure."""

import time
from decimal import Decimal

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.adapters.families import NamedType, of_name
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.sql.type_map import (
    DIGITS_AS_TEXT, FRACTION_NOTE, MYSQL_JSON_NOTE, MYSQL_TIMESTAMP_READ, MYSQL_TIMESTAMP_WRITTEN, QUERY_FLOAT,
    QUERY_FRACTION, QUERY_MONEY, UTC_NOTE, RowFailure, conversion, incompatible, planned_column, query_read_note,
    target_type, to_utc, type_problem, unmappable, with_minute_offset,
)

PG, MY, MS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE
ENGINES = (PG, MY, MS, LITE)


def col(family: str, kind: str = "text", type_name: str | None = None, **fields) -> TableColumn:
    return TableColumn(name=fields.pop("name", "c"), type_name=family if type_name is None else type_name,
                       family=family,  # type: ignore[arg-type]
                       nullable=fields.pop("nullable", True), has_default=False, kind=kind,  # type: ignore[arg-type]
                       compare="equals", writable=True, **fields)


# §3.5.2, row by row: (family, fields) → PostgreSQL, MySQL, SQL Server, SQLite
TABLE = [
    (("bool", "bool"), {}, ("boolean", "tinyint(1)", "bit", "INTEGER")),
    (("int16", "number"), {}, ("smallint", "smallint", "smallint", "INTEGER")),
    (("int32", "number"), {}, ("integer", "int", "int", "INTEGER")),
    (("int64", "number"), {}, ("bigint", "bigint", "bigint", "INTEGER")),
    (("uint64", "number"), {}, ("numeric(20)", "bigint unsigned", "decimal(20,0)", "TEXT")),
    (("decimal", "number"), {"precision": 12, "scale": 3}, ("numeric(12,3)", "decimal(12,3)", "decimal(12,3)", "TEXT")),
    (("decimal", "number"), {}, ("numeric", "decimal(65,30)", "decimal(38,10)", "TEXT")),
    (("money", "number"), {}, ("numeric(19,4)", "decimal(19,4)", "money", "TEXT")),
    (("float32", "number"), {}, ("real", "float", "real", "REAL")),
    (("float64", "number"), {}, ("double precision", "double", "float", "REAL")),
    (("char", "text"), {"length": 5}, ("char(5)", "char(5)", "nchar(5)", "TEXT")),
    (("char", "text"), {"length": 300}, ("char(300)", "varchar(300)", "nchar(300)", "TEXT")),
    (("varchar", "text"), {"length": 10}, ("varchar(10)", "varchar(10)", "nvarchar(20)", "TEXT")),
    (("varchar", "text"), {"length": 3000}, ("varchar(3000)", "varchar(3000)", "nvarchar(max)", "TEXT")),
    (("varchar", "text"), {"length": 20000}, ("varchar(20000)", "longtext", "nvarchar(max)", "TEXT")),
    (("text", "text"), {}, ("text", "longtext", "nvarchar(max)", "TEXT")),
    (("binary", "binary"), {"length": 16}, ("bytea", "varbinary(16)", "varbinary(16)", "BLOB")),
    (("binary", "binary"), {}, ("bytea", "longblob", "varbinary(max)", "BLOB")),
    (("date", "datetime"), {}, ("date", "date", "date", "TEXT")),
    (("time", "datetime"), {}, ("time", "time(6)", "time(7)", "TEXT")),
    (("duration", "datetime"), {}, ("interval", "time(6)", "time(7)", "TEXT")),
    (("year", "datetime"), {}, ("smallint", "year", "smallint", "INTEGER")),
    (("timestamp", "datetime"), {}, ("timestamp", "datetime(6)", "datetime2(7)", "TEXT")),
    (("timestamp_tz", "datetime"), {}, ("timestamptz", "datetime(6)", "datetimeoffset(7)", "TEXT")),
    (("time_tz", "datetime"), {}, ("timetz", "time(6)", "time(7)", "TEXT")),
    (("interval", "datetime"), {}, ("interval", "varchar(64)", "nvarchar(64)", "TEXT")),
    (("uuid", "text"), {}, ("uuid", "char(36)", "uniqueidentifier", "TEXT")),
    (("json", "json"), {}, ("jsonb", "json", "nvarchar(max)", "TEXT")),
    (("xml", "text"), {}, ("xml", "longtext", "xml", "TEXT")),
    (("enum", "text"), {}, ("text", "varchar(255)", "nvarchar(255)", "TEXT")),
    (("other", "other"), {}, ("text", "longtext", "nvarchar(max)", "TEXT")),
]


@pytest.mark.parametrize(("family", "fields", "expected"), TABLE)
def test_every_cell_of_the_type_table_from_another_engine(family, fields, expected) -> None:
    for target, wanted in zip(ENGINES, expected, strict=True):
        source = next(engine for engine in ENGINES if engine is not target)  # another engine than the target
        if family[0] == "json" and target is PG:
            source = MY  # json from PostgreSQL keeps its own json / jsonb
        assert target_type(col(*family, **fields), source, target).type_sql == wanted, (family, target)


def test_what_the_preview_says_of_a_lossy_mapping() -> None:
    assert target_type(col("uint64", "number"), MY, LITE).note == DIGITS_AS_TEXT
    assert target_type(col("decimal", "number"), PG, MY).note.startswith("more than 30 digits after the point")
    assert target_type(col("timestamp_tz", "datetime"), PG, MY).note == "converted to UTC: the offset is not kept"
    assert "UTF-16" in target_type(col("varchar", "text", length=10), PG, MS).note
    assert target_type(col("json", "json"), MS, PG).note == "jsonb orders the keys and keeps the last of duplicate keys"
    assert target_type(col("json", "json", type_name="json"), PG, PG).type_sql == "json"
    assert target_type(col("other", "other", type_name="integer[]"), PG, MY).note == "PostgreSQL's text of the value"
    assert target_type(col("money", "number", type_name="money"), PG, PG).type_sql == "money"
    for source in (PG, MS, LITE):  # pre-flight review I2: MySQL's JSON normalises what it is given
        assert target_type(col("json", "json"), source, MY).note == MYSQL_JSON_NOTE
    assert "doubles: choose longtext" in MYSQL_JSON_NOTE
    assert target_type(col("json", "json"), MY, MY).note is None  # MySQL's own JSON is already normal
    assert target_type(col("json", "json"), PG, LITE).note is None  # text keeps it as it is


def test_a_type_with_no_counterpart_is_a_problem() -> None:
    for family in ("spatial", "variant"):
        mapped = target_type(col(family, "binary", type_name="geography", name="g"), MS, PG)
        assert mapped.type_sql is None
        assert mapped.problem == "g (geography) has no PostgreSQL type: choose one, or leave the column out"
    assert target_type(col("decimal", "number", precision=70, scale=2), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=20, scale=31), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=39, scale=0), PG, MS).problem is not None
    assert target_type(col("decimal", "number", precision=38, scale=0), PG, MS).type_sql == "decimal(38,0)"


def test_a_numeric_with_a_negative_scale_or_more_scale_than_digits_is_for_postgresql_only() -> None:
    for precision, scale in ((5, -2), (3, 5)):  # PostgreSQL 15 takes both; MySQL and SQL Server refuse the CREATE
        column = col("decimal", "number", type_name=f"numeric({precision},{scale})", precision=precision, scale=scale)
        for target in (MY, MS):
            mapped = target_type(column, PG, target)
            assert (mapped.type_sql, mapped.problem) == (None, unmappable(column, target))
        assert target_type(column, MS, PG).type_sql == f"numeric({precision},{scale})"
        assert target_type(column, PG, LITE).type_sql == "TEXT"
    assert unmappable(column, MY) == "c (numeric(3,5)) has no MySQL type: choose one, or leave the column out"
    for target, wanted in ((MY, "decimal(5,5)"), (MS, "decimal(5,5)")):  # as many digits after the point as digits
        assert target_type(col("decimal", "number", precision=5, scale=5), PG, target).type_sql == wanted


def test_the_same_engine_keeps_the_declared_type_unless_it_is_the_databases_own() -> None:
    assert target_type(col("varchar", "text", type_name="character varying(10)"), PG, PG).type_sql == \
        "character varying(10)"
    assert target_type(col("enum", "text", type_name="enum('a','b')"), MY, MY).type_sql == "enum('a','b')"
    assert target_type(col("other", "other", type_name=""), LITE, LITE).type_sql == ""
    domain = target_type(col("varchar", "text", type_name="email", length=40, custom_type=True), PG, PG)
    assert (domain.type_sql, domain.note) == ("varchar(40)", "email is the source database's own type: the new "
                                                             "column is varchar(40)")
    assert target_type(col("enum", "text", type_name="mood", custom_type=True), PG, PG).type_sql == "text"
    assert target_type(col("spatial", "binary", type_name="geometry", custom_type=True), PG, PG).type_sql == "geometry"
    # money stays money, a domain over it too (spec §3.5.2: money from PostgreSQL to PostgreSQL), and the domain is
    # named as every other own type is (06c Task 1 N3)
    cash = target_type(col("money", "number", type_name="cash", custom_type=True), PG, PG)
    assert (cash.type_sql, cash.note) == ("money", "cash is the source database's own type: the new column is money")
    assert target_type(col("money", "number", type_name="money"), PG, PG).note is None
    # a domain over json becomes jsonb: its base is not kept, and the preview says what jsonb does (review Minor 2)
    doc = target_type(col("json", "json", type_name="doc", custom_type=True), PG, PG)
    assert (doc.type_sql, doc.note) == (
        "jsonb", "doc is the source database's own type: the new column is jsonb; "
                 "jsonb orders the keys and keeps the last of duplicate keys")
    # SQL Server's alias over nvarchar(n) stays nvarchar(n): n is already UTF-16 units there (review Minor 7)
    for family, fields, wanted in (
            ("varchar", {"length": 10}, "nvarchar(10)"), ("varchar", {"length": 4000}, "nvarchar(4000)"),
            ("char", {"length": 5}, "nchar(5)"), ("varchar", {"length": 100, "unicode": False}, "varchar(100)"),
            ("char", {"length": 5, "unicode": False}, "char(5)"), ("varchar", {"length": 8000, "unicode": False},
                                                                  "varchar(8000)")):
        alias = target_type(col(family, "text", type_name="zz_alias", custom_type=True, **fields), MS, MS)
        assert alias.type_sql == wanted and "UTF-16" not in (alias.note or ""), (family, fields)


def test_text_and_binary_never_go_into_each_other() -> None:
    assert incompatible(col("binary", "binary", name="b"), col("text", "text", name="t", type_name="text"))
    assert incompatible(col("varchar", "text", name="v"), col("binary", "binary", name="b", type_name="bytea"))
    assert incompatible(col("binary", "binary"), col("binary", "binary")) is None
    # nothing but bytes goes into a binary column: a server would store a number's or a date's own bytes, silently
    bytea = col("binary", "binary", name="b", type_name="bytea")
    for family, kind in (("int32", "number"), ("decimal", "number"), ("float64", "number"), ("year", "datetime"),
                         ("bool", "bool"), ("date", "datetime"), ("time", "datetime"), ("timestamp", "datetime"),
                         ("timestamp_tz", "datetime"), ("duration", "datetime"), ("other", "other")):
        assert incompatible(col(family, kind, name="x"), bytea), family
    assert incompatible(col("int32", "number", name="x", type_name="int"), bytea) == (
        "x (int) does not go into b (bytea), a binary column: only binary data does")
    for family in ("spatial", "variant", "rowversion"):  # read as bytes: they may go into bytes
        assert incompatible(col(family, "binary"), bytea) is None, family
    assert incompatible(col("int32", "number"), col("other", "other")) is None  # an untyped column holds anything


@pytest.mark.parametrize(("text", "problem"), [
    ("int", None), ("varchar(20)", None), ("numeric(10, 2)", None), ("double precision", None),
    ("timestamp with time zone", None), ("nvarchar(max)", None), ("enum('a','b')", None), ("int unsigned", None),
    ("", "it is empty"), ("int; DROP TABLE t", "it has a ;"), ("int -- x", "it has a comment"),
    ("int /*! x */", "it has a comment"), ("int, x int", "it has a comma outside parentheses"),
    ("int NOT NULL", "NOT is not part of a type"), ("int DEFAULT 1", "DEFAULT is not part of a type"),
    ("int REFERENCES t(id)", "REFERENCES is not part of a type"), ("varchar(20", "it leaves a parenthesis open"),
    ("int)", "it closes a parenthesis it did not open"), ("nvarchar('a", "it has an unterminated string, quoted "
                                                                         "name or comment"),
    ("x" * 201, "it is longer than 200 characters"), ("(1)", "it does not name a type"),
])
def test_a_typed_type_is_one_type_and_nothing_else(text, problem) -> None:
    assert type_problem(MY, text) == problem


@pytest.mark.parametrize(("dialect", "text", "problem"), [
    (PG, '"Mood"', None), (PG, 'public."Mood"', None), (PG, '"Mood"[]', None), (MS, "[dbo].[Alias]", None),
    (MY, "`Alias`", None), (PG, '"Mood" NOT NULL', "NOT is not part of a type"),
    (PG, '"Mood', "it has an unterminated string, quoted name or comment"),
    (MS, "[dbo].[Alias", "it has an unterminated string, quoted name or comment"),
    (PG, "'text'", "it does not name a type"),
])
def test_a_quoted_type_name_is_a_type(dialect, text, problem) -> None:
    assert type_problem(dialect, text) == problem


@pytest.mark.parametrize(("dialect", "text", "category", "expected"), [
    (PG, "int4", ColumnCategory.NUMBER, NamedType("int32")),
    (PG, "numeric(10,2)", ColumnCategory.NUMBER, NamedType("decimal", precision=10, scale=2)),
    (PG, "varchar(5)", ColumnCategory.TEXT, NamedType("varchar", length=5)),
    (PG, "timestamp with time zone", None, NamedType("timestamp_tz")),
    (PG, "int4[]", ColumnCategory.OTHER, NamedType("other")),
    (PG, "mood", ColumnCategory.TEXT, NamedType("text")),  # an enum's name: by the grid's category
    (MY, "bigint unsigned", ColumnCategory.NUMBER, NamedType("uint64")),
    (MY, "decimal", ColumnCategory.NUMBER, NamedType("decimal")),
    (MY, "tinyint(1)", None, NamedType("bool")),
    (MY, "time", ColumnCategory.DATETIME, NamedType("duration")),
    (MS, "nvarchar(max)", ColumnCategory.TEXT, NamedType("text")),
    (MS, "datetimeoffset(7)", ColumnCategory.DATETIME, NamedType("timestamp_tz", scale=7)),
    (MS, "float", None, NamedType("float64")),
    (LITE, "VARCHAR(10)", ColumnCategory.TEXT, NamedType("varchar", length=10)),
    (LITE, "", ColumnCategory.OTHER, NamedType("other")),
    # whatever the spaces between the words and around the parentheses (made single before the match, review I3)
    (PG, "  double   precision ", None, NamedType("float64")),
    (PG, " numeric ( 10 , 2 ) ", ColumnCategory.NUMBER, NamedType("decimal", precision=10, scale=2)),
    (PG, "timestamp  with\ttime  zone", None, NamedType("timestamp_tz")),
    (MY, "bigint  unsigned", ColumnCategory.NUMBER, NamedType("uint64")),
    (MY, "tinyint(1 )", None, NamedType("bool")),
])
def test_a_types_text_names_its_family(dialect, text, category, expected) -> None:
    assert of_name(dialect, text, category) == expected


@pytest.mark.parametrize("text", [
    "int" + " " * 190 + ".", "int" + " " * 190 + '"x', "int" + " " * 5000 + "(", " ".join(["a"] * 90) + " !",
    "x" * 5000 + "!", "numeric(" + "1," * 90, "a" + "\t " * 90 + "$",
])
def test_a_type_nothing_reads_is_refused_at_once_whatever_it_is_made_of(text) -> None:
    """Review I3: a long run of spaces and a character the pattern rejects once took 16 seconds (and the sidecar's
    other sessions with it: the match holds the GIL)."""
    started = time.perf_counter()
    named = of_name(PG, text)
    assert time.perf_counter() - started < 0.1
    assert named.family == "other"


def test_a_new_columns_kind_is_what_its_engine_reads() -> None:
    assert planned_column("b", "boolean", PG).kind == "bool"
    assert planned_column("b", "tinyint(1)", MY).kind == "number"
    assert planned_column("b", "INTEGER", LITE).kind == "number"
    assert planned_column("t", "datetime2(7)", MS).kind == "datetime"
    assert planned_column("d", "decimal(12,3)", MY).precision == 12


def convert(source: TableColumn, target: TableColumn, source_dialect: Dialect, target_dialect: Dialect, value):
    done = conversion(source, target, source_dialect, target_dialect).convert
    return value if done is None else done(value)


def test_a_time_zones_value_goes_to_utc_where_the_column_has_none_never_rounded() -> None:
    tz, plain = col("timestamp_tz", "datetime"), col("timestamp", "datetime")
    assert convert(tz, plain, PG, MY, "2026-10-05 12:34:56.123456+08") == "2026-10-05 04:34:56.123456"
    assert convert(tz, plain, MS, PG, "2026-10-05T12:34:56.1234567+05:30") == "2026-10-05 07:04:56.1234567"
    assert convert(tz, plain, PG, MY, "2026-01-01 00:30:00-01") == "2026-01-01 01:30:00"
    assert convert(tz, plain, PG, MY, None) is None
    assert to_utc("1900-01-01 00:00:00+08:05:43") == "1899-12-31 15:54:17"
    assert UTC_NOTE in conversion(tz, plain, PG, MY).notes


def test_a_time_zones_offset_is_written_as_hours_and_minutes() -> None:
    tz, text = col("timestamp_tz", "datetime"), col("text", "text")
    assert convert(tz, col("timestamp_tz", "datetime"), PG, MS, "2026-10-05 04:34:56+00") == "2026-10-05 04:34:56+00:00"
    assert convert(tz, text, PG, LITE, "2026-10-05 04:34:56.5+05:30") == "2026-10-05 04:34:56.5+05:30"
    assert with_minute_offset("2026-10-05T04:34:56Z") == "2026-10-05T04:34:56+00:00"
    assert with_minute_offset("1900-01-01 00:00:00+08:05:43") == "1899-12-31 15:54:17+00:00"  # no minute offset: UTC
    assert convert(col("time_tz", "datetime"), col("time", "datetime"), PG, MS, "12:34:56+05:30") == "12:34:56"
    # MySQL's TIME is a duration's family: the offset goes too (the 4×4 matrix found it)
    assert convert(col("time_tz", "datetime"), col("duration", "datetime"), PG, MY, "12:34:56+05:30") == "12:34:56"
    assert convert(col("time_tz", "datetime"), text, PG, LITE, "12:34:56+05") == "12:34:56+05:00"


def test_the_seventh_digit_of_a_sql_server_time_is_rounded_elsewhere_and_the_preview_says_so() -> None:
    """Review I1: the copy keeps all seven digits as text, and PostgreSQL and MySQL round the seventh away unasked."""
    for family in ("time", "timestamp", "timestamp_tz"):
        into = col(family, "datetime")
        for target in (PG, MY):
            seven = conversion(col(family, "datetime", scale=7), into, MS, target)
            assert FRACTION_NOTE in seven.notes, (family, target)
            assert FRACTION_NOTE not in conversion(col(family, "datetime", scale=6), into, MS, target).notes
            assert FRACTION_NOTE not in conversion(col(family, "datetime"), into, MS, target).notes
        for target in (MS, LITE):  # SQL Server holds seven, SQLite's text holds as many as it is given
            assert FRACTION_NOTE not in conversion(col(family, "datetime", scale=7), into, MS, target).notes
    assert FRACTION_NOTE not in conversion(col("date", "datetime", scale=7), col("date", "datetime"), MS, PG).notes
    assert "rounded" in FRACTION_NOTE and "7" in FRACTION_NOTE


@pytest.mark.parametrize(("value", "reason"), [
    ("infinity", "is 'infinity', which MySQL cannot hold"),
    ("-infinity", "is '-infinity', which MySQL cannot hold"),
    ("0044-03-15 12:00:00 BC", "is a date before the year 1 (BC), which MySQL cannot hold"),
])
def test_postgresqls_special_dates_fail_their_row_on_another_engine(value, reason) -> None:
    with pytest.raises(RowFailure) as failed:
        convert(col("timestamp", "datetime"), col("timestamp", "datetime"), PG, MY, value)
    assert failed.value.reason == reason
    assert convert(col("timestamp", "datetime"), col("timestamp", "datetime"), PG, PG, value) == value


def test_mysqls_zero_dates_and_long_times_fail_their_row_elsewhere() -> None:
    with pytest.raises(RowFailure, match="zero part"):
        convert(col("date", "datetime"), col("date", "datetime"), MY, PG, "0000-00-00")
    assert convert(col("date", "datetime"), col("date", "datetime"), MY, MY, "0000-00-00") == "0000-00-00"
    duration, time = col("duration", "datetime"), col("time", "datetime")
    assert convert(duration, time, MY, MS, "23:59:59.000000") == "23:59:59.000000"
    for value in ("24:00:00", "-01:00:00", "838:59:59.000000"):
        with pytest.raises(RowFailure, match="24 hours or more"):
            convert(duration, time, MY, MS, value)
    assert convert(duration, col("interval", "datetime"), MY, PG, "-838:59:59.000000") == "-838:59:59.000000"


def test_true_and_false_become_what_the_target_column_holds() -> None:
    assert convert(col("bool", "number"), col("bool", "bool"), MY, PG, 1) is True
    assert convert(col("bool", "number"), col("bool", "bool"), LITE, PG, 0) is False
    with pytest.raises(RowFailure) as failed:
        convert(col("bool", "number"), col("bool", "bool"), MY, PG, 5)
    # the reason never quotes the value (Task 9 docs review M5): a number, by its kind only
    assert failed.value.reason == "holds a number that is neither 0 nor 1 (true or false)"
    assert convert(col("bool", "bool"), col("bool", "number"), PG, MY, True) == 1
    assert convert(col("bool", "bool"), col("text", "text"), MS, PG, False) == "false"
    assert convert(col("bool", "bool"), col("text", "text"), PG, LITE, True) == "true"
    # a MySQL TINYINT(1) and a SQLite bool column are read as integers: 0 and 1 are false and true, as the spec says
    text = col("text", "text")
    assert [convert(col("bool", "number"), text, MY, PG, value) for value in (0, 1, 5, -1)] == [
        "false", "true", "5", "-1"]
    assert [convert(col("bool", "number"), text, LITE, MS, value) for value in (0, 1)] == ["false", "true"]
    said = conversion(col("bool", "number"), text, MY, LITE).notes
    assert said == ("true and false as text; other numbers as written",)
    assert convert(col("bool", "bool"), col("bool", "bool"), LITE, LITE, 5) == 5  # the same engine: as read


def test_numbers_into_text_keep_every_digit_and_non_finite_numbers_fail_where_they_cannot_be_held() -> None:
    text = col("text", "text")
    assert convert(col("uint64", "number"), text, MY, LITE, 18446744073709551615) == "18446744073709551615"
    assert convert(col("decimal", "number"), text, MS, LITE, Decimal("1E-7")) == "0.0000001"
    assert convert(col("float64", "number"), text, LITE, PG, 0.1) == "0.1"
    for value in (float("inf"), float("nan"), "NaN", "Infinity"):
        with pytest.raises(RowFailure, match="not a finite number"):
            convert(col("float64", "number"), col("float64", "number"), LITE, MY, value)
    assert convert(col("float64", "number"), col("float64", "number"), PG, LITE, float("inf")) == float("inf")
    with pytest.raises(RowFailure, match="is NaN, which SQLite cannot hold"):  # it would store NULL
        convert(col("float64", "number"), col("float64", "number"), PG, LITE, float("nan"))
    assert conversion(col("int32", "number"), col("int32", "number"), PG, MY).notes == ()  # an integer is finite
    assert conversion(col("float64", "number"), col("float64", "number"), MS, MY).notes == ()  # SQL Server holds none
    finite = conversion(col("decimal", "number"), col("decimal", "number"), PG, MS)  # PostgreSQL's numeric holds NaN
    assert finite.notes == ("NaN and infinity fail their row",)


def test_a_nul_or_binary_data_in_text_fails_its_row_where_it_cannot_be_written() -> None:
    with pytest.raises(RowFailure, match="NUL character"):
        convert(col("text", "text"), col("text", "text"), MS, PG, "a\x00b")
    assert convert(col("text", "text"), col("text", "text"), MS, MY, "a\x00b") == "a\x00b"  # MySQL writes \0
    with pytest.raises(RowFailure, match="NUL character"):
        conversion(col("text", "text"), col("text", "text"), MS, MY, backslash_escapes=False).convert("a\x00b")
    with pytest.raises(RowFailure, match="binary data"):
        convert(col("other", "other"), col("text", "text"), LITE, PG, b"\x00")


def test_mysqls_timestamp_is_said_to_go_as_utc_except_between_two() -> None:
    """06c final review I-1: the copy reads and writes MySQL's TIMESTAMP in UTC (its sessions' time_zone is +00:00),
    whatever zone the grid shows it in; a MySQL TIMESTAMP into a MySQL TIMESTAMP is exact."""
    stamp = col("timestamp", "datetime", type_name="timestamp(3)")
    for target, into in ((LITE, col("text", "text", type_name="TEXT")), (PG, col("timestamp", "datetime")),
                         (MS, col("timestamp", "datetime", type_name="datetime2(7)")),
                         (MY, col("timestamp", "datetime", type_name="datetime(6)"))):  # a DATETIME: shifted too
        assert MYSQL_TIMESTAMP_READ in conversion(stamp, into, MY, target).notes, target
    assert conversion(stamp, col("timestamp", "datetime", type_name="TIMESTAMP"), MY, MY).notes == ()
    query = col("timestamp", "datetime", type_name="timestamp")  # a query's column: the protocol's name
    assert MYSQL_TIMESTAMP_READ in conversion(query, col("text", "text"), MY, LITE).notes
    # into a MySQL TIMESTAMP: a value without a zone is taken as UTC, one with a zone goes as its UTC time
    into = col("timestamp", "datetime", type_name="timestamp")
    for source, dialect in ((col("timestamp", "datetime"), PG), (col("timestamp_tz", "datetime"), PG),
                            (col("timestamp", "datetime", type_name="datetime"), MY)):
        assert MYSQL_TIMESTAMP_WRITTEN in conversion(source, into, dialect, MY).notes, (source.type_name, dialect)
    assert UTC_NOTE in conversion(col("timestamp_tz", "datetime"), into, PG, MY).notes
    assert MYSQL_TIMESTAMP_WRITTEN not in conversion(col("date", "datetime"), into, PG, MY).notes
    # SQL Server's `timestamp` is a rowversion, and no other engine's TIMESTAMP is MySQL's
    for source, dialect in ((col("rowversion", "binary", type_name="timestamp"), MS),
                            (col("timestamp", "datetime", type_name="timestamp"), PG)):
        said = conversion(source, col("text", "text"), dialect, MY).notes
        assert MYSQL_TIMESTAMP_READ not in said and MYSQL_TIMESTAMP_WRITTEN not in said, dialect
    assert "20:00 at +08:00 is copied as 12:00" in MYSQL_TIMESTAMP_READ


def test_a_querys_column_says_what_the_grids_read_of_it_loses() -> None:
    """06c final review M-4: a query's result is read again as the grid reads it, not with a table's `read_as`."""
    text = col("text", "text")
    assert query_read_note(col("float32", "number", type_name="float"), text, MY, LITE) == QUERY_FLOAT
    assert query_read_note(col("float64", "number", type_name="double"), text, MY, LITE) is None
    for name, scale in (("datetime2(7)", 7), ("time(7)", 7), ("datetimeoffset(7)", 7), ("datetime2", None)):
        family = {"datetime2": "timestamp", "time": "time", "datetimeoffset": "timestamp_tz"}[name.split("(")[0]]
        assert query_read_note(col(family, "datetime", type_name=name, scale=scale), text, MS, PG) == QUERY_FRACTION
    assert query_read_note(col("timestamp", "datetime", type_name="datetime2(3)", scale=3), text, MS, PG) is None
    assert query_read_note(col("timestamp", "datetime", type_name="datetime"), text, MS, PG) is None
    money = col("money", "other", type_name="money")
    assert query_read_note(money, col("decimal", "number"), PG, MY) == QUERY_MONEY
    assert query_read_note(money, col("money", "other", type_name="money"), PG, PG) is None  # it reads its own text
    assert query_read_note(money, text, PG, PG) == QUERY_MONEY


def test_sql_servers_new_text_column_that_is_not_unicode_is_known_as_such() -> None:
    """06c final review M-6: the preview says a code page's note of a new SQL Server column of char, varchar or text."""
    assert [planned_column("c", type_sql, MS).unicode for type_sql in (
        "varchar(10)", "char(3)", "text", "nvarchar(max)", "nchar(2)", "ntext", "int")] == [
        False, False, False, True, True, True, True]
    assert planned_column("c", "varchar(10)", PG).unicode and planned_column("c", "text", MY).unicode
