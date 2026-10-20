"""SQL Server dump tests' independent check (06a Task 7, as Task 5's pg_inventory): a catalog is written by
sql/dump_script.DumpScript (the header, restore settings, the `drop_first` DROPs, the objects before the data, each
table's IDENTITY_INSERT and rows, the objects after it; a GO line after each batch) and run into a scratch database
through Run SQL file, and both databases are compared through an inventory of sys.* read as sa and a hash of every
table's rows — never through the dump's own reader, which cannot see what it never reads. The rows travel by INSERT …
SELECT across databases on the server (the dump's own INSERTs from the rows it reads are Task 9's)."""

import time
from pathlib import Path

from pymssql import _mssql

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.dump import DumpCatalog
from dbaguy_backend.adapters.mssql import driver
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sql.dialects import Dialect, force_quote_ident
from dbaguy_backend.sql.dump_script import DumpScript, insert_text, table_sql
from tests.data_support import DataApp
from tests.support import MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, MSSQL_SA_PASSWORD, make_mssql_profile, mssql_connect


def q(name: str) -> str:
    return force_quote_ident(Dialect.TSQL, name)


def catalog_in(database: str, scope: DumpScope, kinds: DumpObjectKinds | None = None, *, ddl: bool = True,
               count: list | None = None, setup: bool = True) -> DumpCatalog:
    """The catalog of `database` as the dump's own session reads it (its setup first, its teardown after); `count`
    gets the number of round trips the read sent and how long it took."""
    conn = MssqlAdapter().open(make_mssql_profile(database=database), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD,
                               None)
    try:
        session = conn.dump_session(scope)
        for statement in session.setup if setup else ():
            conn.run(statement).close()
        calls = [0]
        originals = driver.GuardedConnection.execute_query, driver.GuardedConnection.execute_non_query

        def counting(original):
            def call(self, sql):
                calls[0] += 1
                return original(self, sql)
            return call

        driver.GuardedConnection.execute_query = counting(originals[0])
        driver.GuardedConnection.execute_non_query = counting(originals[1])
        try:
            started = time.perf_counter()
            found = conn.dump_catalog(scope, kinds or DumpObjectKinds(), ddl=ddl)
            took = time.perf_counter() - started
        finally:
            driver.GuardedConnection.execute_query, driver.GuardedConnection.execute_non_query = originals
        if count is not None:
            count[:] = [calls[0], took]
        for statement in session.teardown if setup else ():
            conn.run(statement).close()
        return found
    finally:
        conn.close()


def copy_rows(found: DumpCatalog, source: str) -> dict[str, str]:
    """Each table's copied columns, copied on the server from `source` (table name → INSERT … SELECT)."""
    statements = {}
    for table in found.tables:
        if columns := [column for column in table.columns if column.copied]:
            names = ", ".join(q(column.name) for column in columns)
            target = table_sql(Dialect.TSQL, table)
            statements[table.name] = f"INSERT INTO {target} ({names}) SELECT {names} FROM {q(source)}.{target}"
    return statements


def script(found: DumpCatalog, rows: dict[str, str], *, drop_first: bool = True) -> str:
    """The dump's text as DumpScript writes it, each table's rows (`rows`) between its table_head (IDENTITY_INSERT ON)
    and table_tail."""
    text = DumpScript(Dialect.TSQL, Engine.MSSQL, found, content="both", drop_first=drop_first,
                      created="2026-10-20T00:00:00Z")
    body = text.head() + text.pre()
    for table in found.tables:
        body += text.table_head(table) + (insert_text(Dialect.TSQL, rows[table.name]) if table.name in rows else "")
        body += text.table_tail(table, 0)
    return body + text.post() + text.tail(0)


def restore(app: DataApp, body: dict, path: Path, mode: str = "all_or_nothing", times: int = 2) -> None:
    """Run the file `times` times through Run SQL file (a `drop_first` file replaces what the run before made)."""
    profile = app.add_profile(body)
    for _ in range(times):
        tab = app.job_session(profile)
        ran = tab.run(lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode))
        assert (ran.outcome.outcome, ran.outcome.error_count, ran.error) == ("completed", 0, None), (
            ran.outcome.errors, ran.error, ran.outcome.stopped_at)
        app.close_sessions()


def _rows(conn, sql: str) -> list[tuple]:
    conn.execute_query(sql)
    found = [tuple(row) for row in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE)]
    while conn.nextresult():
        found.extend(tuple(row) for row in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE))
    return found


def sa_rows(database: str, sql: str) -> list[tuple]:
    conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database)
    try:
        return _rows(conn, sql)
    finally:
        conn.close()


_USER = "o.is_ms_shipped = 0"
_SYSTEM_NAMED = "CASE WHEN {t}.is_system_named = 1 THEN N'<system>' ELSE {t}.name END"
_INVENTORY = {
    "objects": f"""SELECT RTRIM(o.type), SCHEMA_NAME(o.schema_id), o.name,
        COALESCE(OBJECT_SCHEMA_NAME(o.parent_object_id) + N'.' + OBJECT_NAME(o.parent_object_id), N'')
        FROM sys.objects o WHERE {_USER} AND o.type NOT IN ('PK', 'UQ', 'C', 'D', 'F', 'IT', 'S', 'TT', 'EC')""",
    "columns": f"""SELECT COALESCE(SCHEMA_NAME(tt.schema_id) + N'.' + tt.name, OBJECT_SCHEMA_NAME(c.object_id) + N'.'
        + OBJECT_NAME(c.object_id)), c.column_id, c.name, TYPE_NAME(c.user_type_id), c.max_length, c.precision,
        c.scale, c.is_nullable, c.collation_name, c.is_identity, c.is_computed, c.is_sparse, c.is_column_set,
        c.is_rowguidcol, c.is_masked, cc.definition, cc.is_persisted, CAST(ic.seed_value AS nvarchar(40)),
        CAST(ic.increment_value AS nvarchar(40)), ic.is_not_for_replication, mc.masking_function,
        c.generated_always_type, c.is_filestream, c.xml_collection_id
        FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id
        LEFT JOIN sys.table_types tt ON tt.type_table_object_id = c.object_id
        LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        LEFT JOIN sys.masked_columns mc ON mc.object_id = c.object_id AND mc.column_id = c.column_id
        WHERE {_USER} AND o.type IN ('U', 'V', 'TT', 'IF', 'TF')""",
    "defaults": f"""SELECT OBJECT_SCHEMA_NAME(d.parent_object_id) + N'.' + OBJECT_NAME(d.parent_object_id),
        COL_NAME(d.parent_object_id, d.parent_column_id), d.definition, {_SYSTEM_NAMED.format(t='d')}
        FROM sys.default_constraints d""",
    "checks": f"""SELECT COALESCE(SCHEMA_NAME(tt.schema_id) + N'.' + tt.name, OBJECT_SCHEMA_NAME(k.parent_object_id)
        + N'.' + OBJECT_NAME(k.parent_object_id)), k.definition, k.is_disabled, k.is_not_trusted,
        k.is_not_for_replication, {_SYSTEM_NAMED.format(t='k')}
        FROM sys.check_constraints k LEFT JOIN sys.table_types tt ON tt.type_table_object_id = k.parent_object_id""",
    "indexes": f"""SELECT COALESCE(SCHEMA_NAME(tt.schema_id) + N'.' + tt.name, OBJECT_SCHEMA_NAME(i.object_id) + N'.'
        + OBJECT_NAME(i.object_id)), CASE WHEN kc.is_system_named = 1 THEN N'<system>' ELSE i.name END, i.type_desc,
        i.is_unique, i.is_primary_key, i.is_unique_constraint, i.filter_definition, i.is_disabled,
        (SELECT STRING_AGG(COL_NAME(x.object_id, x.column_id) + CASE WHEN x.is_descending_key = 1 THEN N' DESC'
                ELSE N'' END + CASE WHEN x.is_included_column = 1 THEN N' INCLUDE' ELSE N'' END, N', ')
                WITHIN GROUP (ORDER BY x.is_included_column, x.key_ordinal, x.index_column_id)
         FROM sys.index_columns x WHERE x.object_id = i.object_id AND x.index_id = i.index_id)
        FROM sys.indexes i JOIN sys.objects o ON o.object_id = i.object_id
        LEFT JOIN sys.table_types tt ON tt.type_table_object_id = i.object_id
        LEFT JOIN sys.key_constraints kc ON kc.parent_object_id = i.object_id AND kc.unique_index_id = i.index_id
        WHERE (o.is_ms_shipped = 0 OR tt.user_type_id IS NOT NULL) AND i.type > 0""",
    "foreign keys": """SELECT f.name, OBJECT_SCHEMA_NAME(f.parent_object_id) + N'.' + OBJECT_NAME(f.parent_object_id),
        OBJECT_SCHEMA_NAME(f.referenced_object_id) + N'.' + OBJECT_NAME(f.referenced_object_id),
        f.delete_referential_action_desc, f.update_referential_action_desc, f.is_disabled, f.is_not_trusted,
        f.is_not_for_replication,
        (SELECT STRING_AGG(COL_NAME(x.parent_object_id, x.parent_column_id) + N'>'
                + COL_NAME(x.referenced_object_id, x.referenced_column_id), N', ') WITHIN GROUP (ORDER BY
                x.constraint_column_id) FROM sys.foreign_key_columns x WHERE x.constraint_object_id = f.object_id)
        FROM sys.foreign_keys f""",
    "modules": f"""SELECT SCHEMA_NAME(o.schema_id), o.name, m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier,
        m.is_schema_bound FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id WHERE {_USER}""",
    "triggers": """SELECT OBJECT_SCHEMA_NAME(t.object_id) + N'.' + t.name, OBJECT_SCHEMA_NAME(t.parent_id) + N'.'
        + OBJECT_NAME(t.parent_id), t.is_disabled, t.is_instead_of_trigger, e.type_desc, e.is_first, e.is_last
        FROM sys.triggers t JOIN sys.trigger_events e ON e.object_id = t.object_id WHERE t.parent_class = 1""",
    "sequences": """SELECT SCHEMA_NAME(schema_id), name, TYPE_NAME(user_type_id), CAST(increment AS nvarchar(40)),
        CAST(minimum_value AS nvarchar(40)), CAST(maximum_value AS nvarchar(40)), is_cycling, is_cached, cache_size,
        CAST(CASE WHEN last_used_value IS NULL THEN CAST(start_value AS decimal(38, 0))
                  WHEN CAST(current_value AS decimal(38, 0)) + CAST(increment AS decimal(38, 0))
                       BETWEEN CAST(minimum_value AS decimal(38, 0)) AND CAST(maximum_value AS decimal(38, 0))
                  THEN CAST(current_value AS decimal(38, 0)) + CAST(increment AS decimal(38, 0))
                  WHEN is_cycling = 1 THEN CASE WHEN CAST(increment AS decimal(38, 0)) > 0
                       THEN CAST(minimum_value AS decimal(38, 0)) ELSE CAST(maximum_value AS decimal(38, 0)) END
             END AS nvarchar(40))
        FROM sys.sequences""",  # the value each gives next; NULL: none left
    "synonyms": "SELECT SCHEMA_NAME(schema_id), name, base_object_name FROM sys.synonyms",
    "types": """SELECT SCHEMA_NAME(t.schema_id), t.name, t.is_table_type, TYPE_NAME(t.system_type_id), t.max_length,
        t.precision, t.scale, t.is_nullable FROM sys.types t WHERE t.is_user_defined = 1""",
    "identity": f"""SELECT OBJECT_SCHEMA_NAME(c.object_id) + N'.' + OBJECT_NAME(c.object_id), c.name,
        CAST(c.last_value AS nvarchar(40)) FROM sys.identity_columns c JOIN sys.objects o ON o.object_id = c.object_id
        WHERE {_USER} AND o.type = 'U'""",
    "policies": """SELECT SCHEMA_NAME(p.schema_id) + N'.' + p.name, p.is_enabled, p.is_schema_bound,
        pr.predicate_type_desc, pr.predicate_definition, OBJECT_SCHEMA_NAME(pr.target_object_id) + N'.'
        + OBJECT_NAME(pr.target_object_id), pr.operation_desc
        FROM sys.security_policies p JOIN sys.security_predicates pr ON pr.object_id = p.object_id""",
    "schemas": """SELECT name FROM sys.schemas WHERE schema_id BETWEEN 5 AND 16383 AND name NOT IN (N'guest')""",
}


def inventory(database: str) -> dict[str, list[tuple]]:
    """What a restore must give back, read as sa from sys.*, each part sorted. A module's text is compared without
    the whitespace at its ends: Run SQL file sends a T-SQL batch from its first token to its last (the splitter's
    rule), so the newline before a GO line does not reach the restored definition."""
    conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database)
    try:
        found = {part: _rows(conn, sql) for part, sql in _INVENTORY.items()}
    finally:
        conn.close()
    found["modules"] = [(*row[:2], row[2].strip() if row[2] is not None else None, *row[3:])
                        for row in found["modules"]]
    return {part: sorted(rows, key=repr) for part, rows in found.items()}


_TABLES = """
SELECT o.object_id, SCHEMA_NAME(o.schema_id), o.name FROM sys.tables o
WHERE o.is_ms_shipped = 0 AND o.temporal_type = 0 AND o.is_node = 0 AND o.is_edge = 0 AND o.ledger_type = 0
"""
# Every column but rowversion (189: the server's own) and sql_variant (98: a dump cannot carry it, its header says so)
_HASHED = """
SELECT name, system_type_id, CASE WHEN user_type_id IN (128, 129, 130) OR system_type_id = 240 THEN 1 ELSE 0 END
FROM sys.columns WHERE object_id = {o} AND system_type_id NOT IN (98, 189) ORDER BY column_id
"""


def data(database: str) -> dict[str, tuple]:
    """Each table's row count and an order-free sum of its rows' hashes (FOR JSON of every column a dump carries)."""
    conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database)
    try:
        found = {}
        for object_id, schema, name in _rows(conn, _TABLES):
            columns = _rows(conn, _HASHED.format(o=object_id))
            if not columns:
                continue
            picked = ", ".join(f"CAST(t.{q(column)} AS varbinary(max)) AS {q(column)}" if clr else
                               f"t.{q(column)}" for column, _, clr in columns)
            row = f"(SELECT {picked} FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES)"
            found[f"{schema}.{name}"] = _rows(conn, (
                "SELECT COUNT(*), CAST(SUM(h) AS nvarchar(40)) FROM (SELECT CAST(CONVERT(bigint, SUBSTRING("
                f"HASHBYTES('SHA2_256', {row}), 1, 7)) AS decimal(38, 0)) AS h FROM {q(schema)}.{q(name)} t) x"))[0]
        return found
    finally:
        conn.close()


def differences(source: dict, restored: dict) -> dict[str, tuple[list, list]]:
    """The parts of two inventories that differ: (only in the source, only in the restore)."""
    found = {}
    for part in source:
        left, right = set(map(repr, source[part])), set(map(repr, restored[part]))
        if left != right:
            found[part] = (sorted(left - right), sorted(right - left))
    return found
