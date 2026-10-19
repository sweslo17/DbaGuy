"""SQL Server grid editing (spec ⑤): the edit target from the catalog views. Scratch objects only (zz_05a_*)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from dbaguy_backend.domain.errors import NotFound
from tests.edit_support import MssqlScratch, scratch
from tests.support import requires_mssql

pytestmark = requires_mssql


@pytest.fixture
def ms():
    db = MssqlScratch()
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_a_table_with_a_primary_key_and_every_kind_of_column(ms):
    db, connection = ms
    with scratch(db) as make:
        alias = make("TYPE", "CREATE TYPE {t} FROM nvarchar(200)")
        table = make("TABLE", "CREATE TABLE {t} (id int IDENTITY PRIMARY KEY, code varchar(20) NOT NULL, "
                              "name nvarchar(50), note nvarchar(max), amount decimal(10,2), f float, r real, at datetime, "
                              "at7 datetime2(7), at3 datetime2(3), dto datetimeoffset, tm time, u uniqueidentifier, "
                              "flag bit, data varbinary(16), old text, oldn ntext, img image, x xml, v sql_variant, "
                              f"h hierarchyid, mail {db.table(alias)}, total AS (amount * 2), ver rowversion)")
        period = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, v int, "
                               "vf datetime2 GENERATED ALWAYS AS ROW START NOT NULL, "
                               "vt datetime2 GENERATED ALWAYS AS ROW END NOT NULL, PERIOD FOR SYSTEM_TIME (vf, vt))")
        target = connection.edit_target(db.path(table))
        versioned = connection.edit_target(db.path(period))
    assert (target.editable, target.key, target.key_source) == (True, ["id"], "primary_key")
    assert {c.name: (c.kind, c.compare, c.writable, c.unicode) for c in target.columns} == {
        "id": ("number", "equals", False, True), "code": ("text", "equals", True, False),
        "name": ("text", "equals", True, True), "note": ("text", "equals", True, True),
        "amount": ("number", "equals", True, True), "f": ("number", "none", True, True),
        "r": ("number", "none", True, True), "at": ("datetime", "equals", True, True),
        "at7": ("datetime", "none", True, True), "at3": ("datetime", "equals", True, True),
        "dto": ("datetime", "none", True, True), "tm": ("datetime", "none", True, True),
        "u": ("text", "equals", True, True), "flag": ("bool", "equals", True, True),
        "data": ("binary", "equals", True, True), "old": ("text", "none", True, False),
        "oldn": ("text", "none", True, True), "img": ("binary", "none", True, True), "x": ("text", "none", True, True),
        "v": ("binary", "none", False, True), "h": ("binary", "none", False, True), "mail": ("text", "equals", True, True),
        "total": ("number", "equals", False, True), "ver": ("binary", "equals", False, True),
    }
    by_name = {c.name: c for c in target.columns}
    assert [by_name[n].read_only_reason for n in ("id", "total", "ver", "v", "h")] == [
        "IDENTITY column", "computed column", "rowversion column", "sql_variant values are not edited here",
        "hierarchyid values are not edited here"]
    assert [by_name[n].type_name for n in ("code", "name", "amount", "at7", "mail")] == [
        "varchar(20)", "nvarchar(50)", "decimal(10,2)", "datetime2(7)", alias]
    assert [c.name for c in target.columns if c.has_default] == ["id", "total", "ver"]
    assert {c.name: (c.writable, c.read_only_reason) for c in versioned.columns if c.name in ("vf", "vt")} == {
        "vf": (False, "period column (system-versioned)"), "vt": (False, "period column (system-versioned)")}


def test_keys_views_and_missing_tables(ms):  # ruling E4
    db, connection = ms
    with scratch(db) as make:
        unique = make("TABLE", "CREATE TABLE {t} (a int NOT NULL, b int NOT NULL, c int NULL, d int NOT NULL, "
                               "e int NOT NULL)")
        qualified = db.table(unique)
        db.execute(
            f"CREATE UNIQUE INDEX u_abd ON {qualified} (a, b, d)",
            f"CREATE UNIQUE INDEX u_c ON {qualified} (c)",  # a nullable column: not a key
            f"CREATE UNIQUE INDEX u_d ON {qualified} (d) WHERE d > 0",  # filtered
            f"CREATE UNIQUE INDEX u_ea ON {qualified} (e, a) INCLUDE (b)",  # INCLUDE is not part of the key
            f"CREATE UNIQUE INDEX u_b ON {qualified} (b)",
            f"ALTER INDEX u_b ON {qualified} DISABLE",  # a disabled index guarantees nothing
        )
        floaty = make("TABLE", "CREATE TABLE {t} (f float PRIMARY KEY, v int)")
        loose = make("TABLE", "CREATE TABLE {t} (a int, b int)")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT a, b FROM {db.table(loose)}")
        found = {name: connection.edit_target(db.path(name)) for name in (unique, floaty, loose)}
        found[view] = connection.edit_target(db.path(view, "view"))
    assert (found[unique].key, found[unique].key_source) == (["e", "a"], "unique")
    assert found[floaty].reason == "The key column f (float) cannot be matched exactly: a row cannot be identified"
    assert (found[loose].reason, found[view].reason) == (NO_KEY, VIEWS)
    with pytest.raises(NotFound):
        connection.edit_target(db.path("zz_05a_never_there"))


def test_a_hypothetical_index_is_not_a_key(ms):  # a Tuning Advisor leftover: unique in the catalog, enforces nothing
    db, connection = ms
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (a int NOT NULL, b int NOT NULL)")
        db.execute(f"CREATE UNIQUE INDEX u_a ON {db.table(table)} (a) WITH STATISTICS_ONLY = -1")
        target = connection.edit_target(db.path(table))
    assert target.reason == NO_KEY
