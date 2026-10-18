"""The parts of the SQL Server catalog code that need no server: names, types and the DDL pieces the dev seed cannot reach
(computed columns, composite keys, SET NULL, descending and included index columns)."""

import pytest

from dbaguy_backend.adapters.mssql import catalog
from dbaguy_backend.adapters.mssql.introspection import (
    _Column, _column_line, _foreign_key_clause, _index_statement, _type_detail)


def column(**overrides) -> _Column:
    fields = dict(
        name="c", type_name="int", max_length=4, precision=10, scale=0, nullable=True, identity=False, seed=None,
        increment=None, computed=None, persisted=None, default_name=None, default=None, user_schema=None,
        collation=None, database_collation="SQL_Latin1_General_CP1_CI_AS", primary_key=False, comment=None)
    fields.update(overrides)
    return _Column(**fields)


@pytest.mark.parametrize("args, expected", [
    (("int", 4, 10, 0), "int"),
    (("nvarchar", 100, 0, 0), "nvarchar(50)"),  # max_length counts bytes: two per character
    (("nvarchar", -1, 0, 0), "nvarchar(max)"),
    (("nchar", 20, 0, 0), "nchar(10)"),
    (("varchar", 50, 0, 0), "varchar(50)"),
    (("varchar", -1, 0, 0), "varchar(max)"),
    (("varbinary", -1, 0, 0), "varbinary(max)"),
    (("binary", 16, 0, 0), "binary(16)"),
    (("decimal", 17, 38, 10), "decimal(38,10)"),
    (("numeric", 9, 9, 2), "numeric(9,2)"),
    (("datetime2", 8, 27, 7), "datetime2(7)"),
    (("datetimeoffset", 10, 34, 3), "datetimeoffset(3)"),
    (("time", 5, 16, 0), "time(0)"),
    (("float", 8, 53, 0), "float"),
    (("float", 8, 24, 0), "float(24)"),
    (("real", 4, 24, 0), "real"),
    (("email_address", 400, 0, 0), "email_address"),  # an alias keeps its name
    (("sql_variant", 8016, 0, 0), "sql_variant"),
])
def test_type_sql(args, expected):
    assert catalog.type_sql(*args) == expected


@pytest.mark.parametrize("name, schema_id, system", [
    ("dbo", 1, False), ("sales", 5, False), ("sys", 4, True), ("INFORMATION_SCHEMA", 3, True), ("guest", 2, True),
    ("db_owner", 16384, True), ("db_denydatawriter", 16393, True), ("someone", 16400, False),  # roles end at 16399
])
def test_system_schemas(name, schema_id, system):
    assert catalog.is_system_schema(name, schema_id) is system


def test_names_are_quoted_and_literals_doubled():
    assert catalog.q("a]b") == "[a]]b]" and catalog.q("select") == "[select]" and catalog.q("客戶") == "[客戶]"
    assert catalog.literal("it's") == "N'it''s'"
    assert catalog.object_id_sql("sa]les", "o'rder") == "OBJECT_ID(N'[sa]]les].[o''rder]')"
    assert catalog.name_literal("dbo", "客戶") == "N'[dbo].[客戶]'"


def test_column_lines():
    assert _column_line(column(name="id", nullable=False, identity=True, seed="1", increment="1"), True) == (
        "[id] int IDENTITY(1, 1) NOT NULL")
    assert _column_line(column(computed="([a]+[b])", persisted=True), True) == "[c] AS ([a]+[b]) PERSISTED"
    assert _column_line(column(computed="([a]+[b])", persisted=False), True) == "[c] AS ([a]+[b])"
    with_default = column(name="at", type_name="datetime2", scale=3, default="(sysutcdatetime())", default_name="at_df")
    assert _column_line(with_default, True) == "[at] datetime2(3) NULL CONSTRAINT [at_df] DEFAULT (sysutcdatetime())"
    assert _column_line(with_default, False) == "[at] datetime2(3) NULL DEFAULT (sysutcdatetime())"  # a table type
    own = column(name="v", type_name="varchar", max_length=10, collation="Latin1_General_BIN", nullable=False)
    assert _column_line(own, True) == "[v] varchar(10) COLLATE Latin1_General_BIN NOT NULL"
    assert _column_line(column(collation="SQL_Latin1_General_CP1_CI_AS"), True) == "[c] int NULL"  # the database's own
    alias = column(name="e", type_name="email_address", user_schema="my]schema", nullable=False)
    assert _column_line(alias, True) == "[e] [my]]schema].[email_address] NOT NULL"
    assert alias.info().type_name == "email_address"


def test_foreign_key_clause_keeps_key_order_and_actions():
    parts = [("fk", "a", "dbo", "parent", "x", "SET_NULL", "SET_DEFAULT"), ("fk", "b", "dbo", "parent", "y", "SET_NULL", "SET_DEFAULT")]
    assert _foreign_key_clause("fk", parts) == (
        "CONSTRAINT [fk] FOREIGN KEY ([a], [b]) REFERENCES [dbo].[parent] ([x], [y]) ON DELETE SET NULL ON UPDATE SET DEFAULT")


def test_index_statements():
    # (name, is_unique, type_desc, filter, column, descending, included, is_primary_key, is_unique_constraint)
    plain = [("ix", 1, "NONCLUSTERED", "([n]>(0))", "a", 0, 0, 0, 0), ("ix", 1, "NONCLUSTERED", "([n]>(0))", "b", 1, 0, 0, 0),
             ("ix", 1, "NONCLUSTERED", "([n]>(0))", "c", 0, 1, 0, 0)]
    assert _index_statement("s", "t", "ix", plain) == (
        "CREATE UNIQUE NONCLUSTERED INDEX [ix] ON [s].[t] ([a], [b] DESC) INCLUDE ([c]) WHERE ([n]>(0));")
    clustered = [("cx", 0, "CLUSTERED", None, "a", 0, 0, 0, 0)]
    assert _index_statement("s", "t", "cx", clustered) == "CREATE CLUSTERED INDEX [cx] ON [s].[t] ([a]);"
    unique_constraint = [("uq", 1, "NONCLUSTERED", None, "a", 0, 0, 0, 1)]
    assert _index_statement("s", "t", "uq", unique_constraint) == (
        "ALTER TABLE [s].[t] ADD CONSTRAINT [uq] UNIQUE NONCLUSTERED ([a]);")


def test_type_details():
    assert _type_detail(True, None, -1, 0, 0) == "table type"
    assert _type_detail(False, "nvarchar", 400, 0, 0) == "nvarchar(200)"
    assert _type_detail(False, "decimal", 9, 18, 2) == "decimal(18,2)"
    assert _type_detail(False, None, 8, 0, 0) == "CLR type"
