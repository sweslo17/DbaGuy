"""The session settings a run from statement N still sends from the part it skips (Run SQL file, ruling R4): a fixed
allow-list per dialect, never what outlives the session or belongs to one transaction."""

import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.session_settings import is_session_setting

PG, MYSQL, TSQL, SQLITE, SQLPP = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE, Dialect.SQLPP


@pytest.mark.parametrize(("text", "dialect", "sent"), [
    # PostgreSQL: SET [SESSION] and pg_dump's set_config(…, false)
    ("SET search_path TO zz_06_s", PG, True), ("SET SESSION statement_timeout = 0", PG, True),
    ("SET client_encoding = 'UTF8'", PG, True), ("SET ROLE dbaguy", PG, True),
    ("SELECT pg_catalog.set_config('search_path', '', false)", PG, True),
    ("SELECT set_config('a.b', 'it''s', false)", PG, True),
    ("SET LOCAL lock_timeout = 0", PG, False), ("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", PG, False),
    ("SET CONSTRAINTS ALL DEFERRED", PG, False), ("SELECT pg_catalog.set_config('search_path', '', true)", PG, False),
    ("SELECT pg_catalog.setval('s', 3, true)", PG, False), ("INSERT INTO t VALUES (1)", PG, False),
    # MySQL: SET [SESSION], SET NAMES, SET CHARACTER SET, version comments, USE; never GLOBAL / PERSIST / PASSWORD
    ("SET NAMES utf8mb4", MYSQL, True), ("SET CHARACTER SET utf8mb4", MYSQL, True),
    ("/*!40103 SET TIME_ZONE='+00:00' */", MYSQL, True), ("/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='' */", MYSQL,
                                                             True),
    ("SET SESSION sql_mode = 'ANSI_QUOTES'", MYSQL, True), ("SET @saved_cs_client = @@character_set_client", MYSQL, True),
    ("USE `dbaguy`", MYSQL, True), ("SET autocommit = 0", MYSQL, True),
    ("SET GLOBAL max_connections = 10", MYSQL, False), ("SET @@GLOBAL.sql_mode = ''", MYSQL, False),
    ("SET PERSIST max_connections = 10", MYSQL, False), ("SET PERSIST_ONLY x = 1", MYSQL, False),
    ("SET PASSWORD = 'x'", MYSQL, False), ("SET DEFAULT ROLE ALL TO u", MYSQL, False),
    ("SET TRANSACTION READ ONLY", MYSQL, False), ("/*!40000 ALTER TABLE `t` DISABLE KEYS */", MYSQL, False),
    ("LOCK TABLES `t` WRITE", MYSQL, False),
    # N3: a right side that calls something may write; SET RESOURCE GROUP … FOR moves other threads
    ("SET @x = (SELECT zz_06_f())", MYSQL, False), ("SET @y = zz_06_f()", MYSQL, False),
    ("SET SESSION sql_mode = CONCAT(@@sql_mode, ',ANSI')", MYSQL, False),
    ("/*!40101 SET @x = zz_06_f() */", MYSQL, False), ("SET @note = 'f(x)'", MYSQL, True),
    ("SET RESOURCE GROUP rg FOR 17, 18", MYSQL, False), ("SET RESOURCE GROUP rg", MYSQL, True),
    ("/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */", MYSQL, True),
    # T-SQL: SET <option> ON|OFF and USE, every statement of the batch
    ("SET ANSI_NULLS ON", TSQL, True), ("SET QUOTED_IDENTIFIER ON\nSET ANSI_PADDING ON", TSQL, True),
    ("SET IDENTITY_INSERT [dbo].[t] ON", TSQL, True), ("USE [dbaguy]", TSQL, True),
    ("SET NOCOUNT ON\nINSERT INTO t VALUES (1)", TSQL, False), ("SET DATEFORMAT dmy", TSQL, False),
    ("SET TRANSACTION ISOLATION LEVEL SNAPSHOT", TSQL, False), ("SET @x = 1", TSQL, False),
    ("SELECT 'SET ANSI_NULLS ON'", TSQL, False),
    # SQLite: three PRAGMAs
    ("PRAGMA foreign_keys=OFF", SQLITE, True), ("PRAGMA defer_foreign_keys = ON", SQLITE, True),
    ("PRAGMA recursive_triggers(1)", SQLITE, True), ("PRAGMA journal_mode=WAL", SQLITE, False),
    ("PRAGMA foreign_keys", SQLITE, False),
    # SQL++: none
    ("SET x = 1", SQLPP, False),
])
def test_what_a_run_from_statement_n_still_sends(text, dialect, sent):
    assert is_session_setting(text, dialect) is sent
