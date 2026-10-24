"""Copy's type map and value conversions (spec ⑥ §3.5.2–3.5.3, 06c Task 1): pure."""

from decimal import Decimal

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.adapters.families import NamedType, of_name
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.sql.type_map import (
    DIGITS_AS_TEXT, UTC_NOTE, RowFailure, conversion, incompatible, planned_column, target_type, to_utc,
    type_problem, with_minute_offset,
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


def test_a_type_with_no_counterpart_is_a_problem() -> None:
    for family in ("spatial", "variant"):
        mapped = target_type(col(family, "binary", type_name="geography", name="g"), MS, PG)
        assert mapped.type_sql is None
        assert mapped.problem == "g (geography) has no PostgreSQL type: choose one, or leave the column out"
    assert target_type(col("decimal", "number", precision=70, scale=2), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=20, scale=31), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=39, scale=0), PG, MS).problem is not None
    assert target_type(col("decimal", "number", precision=38, scale=0), PG, MS).type_sql == "decimal(38,0)"


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


def test_text_and_binary_never_go_into_each_other() -> None:
    assert incompatible(col("binary", "binary", name="b"), col("text", "text", name="t", type_name="text"))
    assert incompatible(col("varchar", "text", name="v"), col("binary", "binary", name="b", type_name="bytea"))
    assert incompatible(col("binary", "binary"), col("binary", "binary")) is None
    assert incompatible(col("int32", "number"), col("binary", "binary")) is None  # the server decides


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
])
def test_a_types_text_names_its_family(dialect, text, category, expected) -> None:
    assert of_name(dialect, text, category) == expected


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
    with pytest.raises(RowFailure, match="holds 5, which is neither 0 nor 1"):
        convert(col("bool", "number"), col("bool", "bool"), MY, PG, 5)
    assert convert(col("bool", "bool"), col("bool", "number"), PG, MY, True) == 1
    assert convert(col("bool", "bool"), col("text", "text"), MS, PG, False) == "false"
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
