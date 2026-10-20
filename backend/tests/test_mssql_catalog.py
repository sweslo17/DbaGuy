"""The parts of the SQL Server catalog code that need no server: names, types and the DDL pieces the dev seed cannot reach
(computed columns, composite keys, SET NULL, descending and included index columns)."""

import pytest

from dbaguy_backend.adapters.mssql import catalog
from dbaguy_backend.adapters.mssql.introspection import (
    _body_lines, _Column, _column_line, _foreign_key_clause, _index_statement, _table_ddl, _type_detail, _Withheld)


def column(**overrides) -> _Column:
    fields = dict(
        name="c", type_name="int", max_length=4, precision=10, scale=0, nullable=True, identity=False, seed=None,
        increment=None, computed=None, persisted=None, default_name=None, default=None, user_schema=None,
        collation=None, database_collation="SQL_Latin1_General_CP1_CI_AS", primary_key=False, comment=None,
        is_computed=False, has_default=False, type_hidden=False, default_system_named=None)
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
    assert _column_line(column(computed="([a]+[b])", is_computed=True, persisted=False), True) == "[c] AS ([a]+[b])"
    persisted = column(computed="([a]+[b])", is_computed=True, persisted=True)
    assert _column_line(persisted, True) == "[c] AS ([a]+[b]) PERSISTED"
    assert _column_line(column(computed="([a]+[b])", is_computed=True, persisted=True, nullable=False), True) == (
        "[c] AS ([a]+[b]) PERSISTED NOT NULL")
    with_default = column(name="at", type_name="datetime2", scale=3, has_default=True, default="(sysutcdatetime())",
                          default_name="at_df", default_system_named=False)
    assert _column_line(with_default, True) == "[at] datetime2(3) NULL CONSTRAINT [at_df] DEFAULT (sysutcdatetime())"
    assert _column_line(with_default, False) == "[at] datetime2(3) NULL DEFAULT (sysutcdatetime())"  # a table type
    made_up = column(name="at", has_default=True, default="((5))", default_name="DF__t__at__3E52440B", default_system_named=True)
    assert _column_line(made_up, True) == "[at] int NULL DEFAULT ((5))"  # a name the server made up is left out
    own = column(name="v", type_name="varchar", max_length=10, collation="Latin1_General_BIN", nullable=False)
    assert _column_line(own, True) == "[v] varchar(10) COLLATE Latin1_General_BIN NOT NULL"
    assert _column_line(column(collation="SQL_Latin1_General_CP1_CI_AS"), True) == "[c] int NULL"  # the database's own
    alias = column(name="e", type_name="email_address", user_schema="my]schema", nullable=False)
    assert _column_line(alias, True) == "[e] [my]]schema].[email_address] NOT NULL"
    assert alias.info().type_name == "email_address"


def test_column_lines_keep_sparse_columns_column_sets_masks_rowguidcol_and_replication():  # 06a Task 7
    assert _column_line(column(name="a", sparse=True), True) == "[a] int SPARSE NULL"
    assert _column_line(column(name="cs", type_name="xml", max_length=-1, column_set=True), True) == (
        "[cs] xml COLUMN_SET FOR ALL_SPARSE_COLUMNS")
    masked = column(name="m", type_name="nvarchar", max_length=40, masking='partial(1, "X", 0)')
    assert _column_line(masked, True) == """[m] nvarchar(20) MASKED WITH (FUNCTION = N'partial(1, "X", 0)') NULL"""
    guid = column(name="g", type_name="uniqueidentifier", max_length=16, nullable=False, rowguidcol=True)
    assert _column_line(guid, True) == "[g] uniqueidentifier NOT NULL ROWGUIDCOL"
    identity = column(name="id", nullable=False, identity=True, seed="1", increment="1",
                      identity_not_for_replication=True)
    assert _column_line(identity, True) == "[id] int IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL"
    assert _body_lines([column(name="a")], [], [("chk", "([a]>(0))", False, False, True, True)], True) == [
        "[a] int NULL", "CONSTRAINT [chk] CHECK NOT FOR REPLICATION ([a]>(0))"]


@pytest.mark.parametrize("hidden", [
    dict(is_computed=True, computed=None, persisted=False),  # a computed column whose expression the account cannot read
    dict(has_default=True, default=None, default_name="df", default_system_named=False),
    dict(type_hidden=True, type_name="nvarchar", max_length=400),  # a user type the account cannot see
    dict(identity=True, seed=None, increment=None),
])
def test_a_hidden_definition_is_withheld_never_written_as_none(hidden):
    with pytest.raises(_Withheld):
        _column_line(column(**hidden), True)


def test_a_hidden_column_still_has_its_base_type_for_the_tree():
    assert column(type_name="nvarchar", max_length=400, type_hidden=True).info().type_name == "nvarchar(200)"


def test_a_hidden_check_definition_is_withheld():
    keys = [("pk", 1, "CLUSTERED", "a", 0, False)]
    # (name, definition, is_system_named, is_disabled, is_not_trusted, is_not_for_replication)
    assert _body_lines([column(name="a")], keys, [("chk", "([a]>(0))", False, False, False, False)], True) == [
        "[a] int NULL", "CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([a])", "CONSTRAINT [chk] CHECK ([a]>(0))"]
    with pytest.raises(_Withheld):
        _body_lines([column(name="a")], keys, [("chk", None, False, False, False, False)], True)


def test_system_made_constraint_names_are_left_out_of_the_body():
    keys = [("PK__t__3213E83F", 1, "CLUSTERED", "a", 0, True), ("uq", 0, "NONCLUSTERED", "b", 0, False)]
    checks = [("CK__t__a__403A8C7D", "([a]>(0))", True, False, False, False)]
    assert _body_lines([column(name="a")], keys, checks, True) == [
        "[a] int NULL", "PRIMARY KEY CLUSTERED ([a])", "CONSTRAINT [uq] UNIQUE NONCLUSTERED ([b])", "CHECK ([a]>(0))"]
    assert _body_lines([column(name="a")], keys, checks, False) == [  # a table type names nothing
        "[a] int NULL", "PRIMARY KEY CLUSTERED ([a])", "UNIQUE NONCLUSTERED ([b])", "CHECK ([a]>(0))"]


def test_a_table_with_a_hidden_foreign_key_has_no_ddl():
    # OBJECT_NAME is NULL: not visible. (…, is_system_named, is_disabled, is_not_trusted, is_not_for_replication)
    hidden_key = [("fk", "a", None, None, None, "NO_ACTION", "NO_ACTION", False, False, False, False)]
    with pytest.raises(_Withheld):
        _table_ddl("dbo", "t", [column(name="a")], [], [], hidden_key, [])
    with pytest.raises(_Withheld):
        _foreign_key_clause("fk", hidden_key)


def test_foreign_key_clause_keeps_key_order_and_actions():
    parts = [("fk", "a", "dbo", "parent", "x", "SET_NULL", "SET_DEFAULT", False, False, False, False),
             ("fk", "b", "dbo", "parent", "y", "SET_NULL", "SET_DEFAULT", False, False, False, False)]
    assert _foreign_key_clause("fk", parts) == (
        "CONSTRAINT [fk] FOREIGN KEY ([a], [b]) REFERENCES [dbo].[parent] ([x], [y]) ON DELETE SET NULL ON UPDATE SET DEFAULT")
    made_up = [("FK__t__a__412EB0B6", "a", "dbo", "parent", "x", "NO_ACTION", "NO_ACTION", True, False, False, True)]
    assert _foreign_key_clause("FK__t__a__412EB0B6", made_up) == (
        "FOREIGN KEY ([a]) REFERENCES [dbo].[parent] ([x]) ON DELETE NO ACTION ON UPDATE NO ACTION NOT FOR REPLICATION")
    assert _foreign_key_clause("FK__t__a__412EB0B6", made_up, keep_name=True).startswith(  # a dump's (H7)
        "CONSTRAINT [FK__t__a__412EB0B6] FOREIGN KEY ([a])")


def test_index_statements():
    # (name, is_unique, type_desc, filter, column, descending, included, is_primary_key, is_unique_constraint,
    #  is_system_named, has_filter)
    f = "([n]>(0))"
    plain = [("ix", 1, "NONCLUSTERED", f, "a", 0, 0, 0, 0, None, 1), ("ix", 1, "NONCLUSTERED", f, "b", 1, 0, 0, 0, None, 1),
             ("ix", 1, "NONCLUSTERED", f, "c", 0, 1, 0, 0, None, 1)]
    assert _index_statement("s", "t", "ix", plain) == (
        "CREATE UNIQUE NONCLUSTERED INDEX [ix] ON [s].[t] ([a], [b] DESC) INCLUDE ([c]) WHERE ([n]>(0));")
    clustered = [("cx", 0, "CLUSTERED", None, "a", 0, 0, 0, 0, None, 0)]
    assert _index_statement("s", "t", "cx", clustered) == "CREATE CLUSTERED INDEX [cx] ON [s].[t] ([a]);"
    unique_constraint = [("uq", 1, "NONCLUSTERED", None, "a", 0, 0, 0, 1, False, 0)]
    assert _index_statement("s", "t", "uq", unique_constraint) == (
        "ALTER TABLE [s].[t] ADD CONSTRAINT [uq] UNIQUE NONCLUSTERED ([a]);")
    made_up = [("PK__t__3213E83F", 1, "CLUSTERED", None, "a", 0, 0, 1, 0, True, 0)]
    assert _index_statement("s", "t", "PK__t__3213E83F", made_up) == "ALTER TABLE [s].[t] ADD PRIMARY KEY CLUSTERED ([a]);"


def test_a_filtered_index_whose_filter_is_hidden_is_withheld():
    hidden = [("ix", 0, "NONCLUSTERED", None, "a", 0, 0, 0, 0, None, 1)]  # has_filter, but no text to give
    with pytest.raises(_Withheld):
        _index_statement("s", "t", "ix", hidden)


def test_type_details():
    assert _type_detail(True, None, -1, 0, 0) == "table type"
    assert _type_detail(False, "nvarchar", 400, 0, 0) == "nvarchar(200)"
    assert _type_detail(False, "decimal", 9, 18, 2) == "decimal(18,2)"
    assert _type_detail(False, None, 8, 0, 0) == "CLR type"
