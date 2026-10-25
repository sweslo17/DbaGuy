"""The SQL a copy adds around its rows (06c Task 2): pure."""

from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.sql.transfer_sql import (
    advance_sequence, create_table, delete_rows, identity_switch, read_settings, set_setting, summary,
)


def test_a_new_table_has_its_columns_not_null_and_the_sources_key_only() -> None:
    assert create_table(Dialect.TSQL, "dbo", "order", [("id", "bigint", False), ("note", "nvarchar(max)", True)],
                        ["id"]) == "CREATE TABLE dbo.[order] (\n  id bigint NOT NULL,\n  note nvarchar(max),\n  PRIMARY KEY (id)\n)"
    assert create_table(Dialect.SQLITE, "main", "t", [("u", "", True)], []) == 'CREATE TABLE main.t (\n  u\n)'


def test_the_statements_around_the_rows() -> None:
    assert delete_rows(Dialect.MYSQL, "dbaguy", "t") == "DELETE FROM dbaguy.t"
    assert identity_switch(Dialect.TSQL, "dbo", "t") == ("SET IDENTITY_INSERT dbo.t ON", "SET IDENTITY_INSERT dbo.t OFF")
    assert identity_switch(Dialect.POSTGRES, "public", "t") is None
    assert advance_sequence("public", "Order", "id").splitlines() == [
        "SELECT setval(s.seq, GREATEST(s.top, COALESCE(pg_sequence_last_value(s.seq), s.top)))",
        """FROM (SELECT pg_get_serial_sequence('public."Order"', 'id')::regclass AS seq,""",
        """             (SELECT max(id) FROM public."Order") AS top) s""",
        "WHERE s.seq IS NOT NULL AND s.top IS NOT NULL"]


def test_a_sources_settings_are_read_set_and_set_back() -> None:
    assert read_settings(Dialect.POSTGRES) == "SELECT current_setting('DateStyle'), current_setting('IntervalStyle')"
    assert read_settings(Dialect.MYSQL) == "SELECT @@SESSION.net_write_timeout, @@SESSION.time_zone"
    assert read_settings(Dialect.TSQL) is None
    assert set_setting(Dialect.POSTGRES, "DateStyle", "ISO, MDY") == "SET DateStyle = 'ISO, MDY'"
    assert set_setting(Dialect.MYSQL, "net_write_timeout", "3600") == "SET SESSION net_write_timeout = 3600"
    assert set_setting(Dialect.MYSQL, "time_zone", "SYSTEM") == "SET SESSION time_zone = 'SYSTEM'"


def test_the_history_record_says_what_went_where_and_the_statements() -> None:
    assert summary("Local PG", "Dev MySQL", [("public.orders", "orders", "create")], Dialect.MYSQL,
                   ["CREATE TABLE orders (\n  id int\n)"]) == (
        "-- Copy data: Local PG public.orders → Dev MySQL orders (create)\nCREATE TABLE orders (\n  id int\n);")
    assert summary("A", "B", [("a", "a", "append"), ("b", "b", "append")], Dialect.TSQL, []) == (
        "-- Copy data: A → B, 2 tables\n--   a → a (append)\n--   b → b (append)")
