"""What a statement's text tells about the transaction around it (Run SQL file, spec ⑥ R-06-7; pre-flight H1, H3):
the answers that decide between `rolled_back` and `unknown`, so a wrong "no" here is a run that lies."""

import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.transaction_text import (
    calls_a_procedure, ends_the_transaction, first_words, implicit_commit_word, may_commit, runs_several,
    sets_autocommit, statement_head, transaction_word,
)

PG, MYSQL, TSQL, SQLITE, SQLPP = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE, Dialect.SQLPP


def test_a_leading_version_comment_is_read_as_the_statement_it_holds():
    assert statement_head("/*!40000 ALTER TABLE `t` DISABLE KEYS */", MYSQL) == " ALTER TABLE `t` DISABLE KEYS "
    assert first_words("/*!50003 CREATE*/ /*!50017 DEFINER=`u`@`%`*/ /*!50003 TRIGGER x */", MYSQL) == ["CREATE"]
    assert first_words("-- c\nINSERT INTO t VALUES (1)", PG) == ["INSERT", "INTO"]
    assert first_words("(SELECT 1)", PG) == []
    assert statement_head("/*+ hint */ SELECT 1", MYSQL) == "/*+ hint */ SELECT 1"  # a hint is not a version comment


@pytest.mark.parametrize(("text", "dialect", "word"), [
    ("COMMIT", PG, "COMMIT"), ("END", PG, "END"), ("ABORT", PG, "ABORT"), ("BEGIN TRANSACTION", SQLITE, "BEGIN"),
    ("END TRANSACTION", SQLITE, "END"), ("SAVEPOINT a", SQLITE, "SAVEPOINT"), ("RELEASE a", SQLITE, "RELEASE"),
    ("START TRANSACTION", MYSQL, "START"), ("/*!40101 COMMIT */", MYSQL, "COMMIT"), ("ROLLBACK", MYSQL, "ROLLBACK"),
    ("BEGIN TRANSACTION", TSQL, "BEGIN TRANSACTION"), ("SET NOCOUNT OFF;\nBEGIN TRAN", TSQL, "BEGIN TRAN"),
    ("INSERT t VALUES (1)\nCOMMIT", TSQL, "COMMIT"), ("SAVE TRANSACTION s", TSQL, "SAVE"),
    ("BEGIN TRY SELECT 1/0 END TRY BEGIN CATCH IF @@TRANCOUNT > 0 ROLLBACK END CATCH", TSQL, "ROLLBACK"),
    # not the file's own transaction control
    ("begin_x", SQLITE, None), ("SELECT 'COMMIT'", PG, None), ("DO $$ BEGIN COMMIT; END $$", PG, None),
    ("SELECT [commit], N'ROLLBACK' FROM t -- COMMIT", TSQL, None), ("BEGIN TRY SELECT 1 END TRY BEGIN CATCH END CATCH",
                                                                    TSQL, None),
    ("CREATE PROCEDURE p AS BEGIN TRAN; COMMIT", TSQL, None), ("CREATE OR ALTER TRIGGER t ON x AFTER INSERT AS ROLLBACK",
                                                               TSQL, None),
    ("UPDATE t SET savepoint = 1", SQLITE, None),
])
def test_the_files_own_transaction_statements(text, dialect, word):
    assert transaction_word(text, dialect) == word


@pytest.mark.parametrize(("text", "word"), [
    ("CREATE TABLE t (a int)", "CREATE"), ("DROP TABLE IF EXISTS `t`", "DROP"), ("LOCK TABLES `t` WRITE", "LOCK"),
    ("UNLOCK TABLES", "UNLOCK"), ("TRUNCATE t", "TRUNCATE"), ("/*!40000 ALTER TABLE `t` DISABLE KEYS */", "ALTER"),
    ("/*!50003 CREATE*/ /*!50017 DEFINER=`u`@`%`*/ /*!50003 TRIGGER x BEFORE INSERT ON t FOR EACH ROW SET @a = 1 */",
     "CREATE"),
    ("GRANT SELECT ON t TO u", "GRANT"),
    ("START REPLICA", "START REPLICA"), ("START SLAVE", "START SLAVE"), ("STOP REPLICA", "STOP REPLICA"),
    ("STOP SLAVE", "STOP SLAVE"), ("SET PASSWORD FOR 'x'@'%' = 'y'", "SET PASSWORD"), ("IMPORT TABLE FROM 'f'", "IMPORT"),
    ("START TRANSACTION", None), ("SET NAMES utf8mb4", None),
    ("CREATE TEMPORARY TABLE t (a int)", None), ("DROP TEMPORARY TABLE t", None), ("INSERT INTO t VALUES (1)", None),
    ("/*!40101 SET NAMES utf8mb4 */", None), ("SET autocommit = 1", None),
])
def test_what_mysql_commits_before_it_runs(text, word):
    assert implicit_commit_word(text, MYSQL) == word
    assert implicit_commit_word(text, PG) is None


@pytest.mark.parametrize(("text", "dialect", "calls", "commits", "several"), [
    ("CALL p()", MYSQL, True, True, True), ("CALL p()", PG, True, True, True), ("EXECUTE s", MYSQL, True, True, True),
    ("EXEC dbo.p", TSQL, True, True, True), ("sp_rename 'a', 'b'", TSQL, True, True, True),
    ("SET XACT_ABORT ON\nEXEC p", TSQL, False, True, True), ("INSERT t VALUES (1)\nINSERT t VALUES (2)", TSQL,
                                                              False, False, True),
    ("INSERT t VALUES (1); COMMIT", TSQL, False, True, True), ("WITH x AS (SELECT 1 a) SELECT * FROM x", TSQL,
                                                              False, False, False),
    ("CREATE PROCEDURE p AS INSERT t VALUES (1); COMMIT", TSQL, False, True, False),
    ("INSERT INTO t VALUES ('COMMIT')", MYSQL, False, False, False), ("INSERT INTO t VALUES (1)", PG, False, False, False),
    ("SELECT RAW 1", SQLPP, False, False, False),
    ("DO $$ BEGIN INSERT INTO t VALUES (1); COMMIT; END $$", PG, True, True, True),  # R6
    ("INSERT INTO t VALUES ('call me')", TSQL, False, False, False),
])
def test_what_may_commit_or_apply_part_of_its_work(text, dialect, calls, commits, several):
    assert (calls_a_procedure(text, dialect), may_commit(text, dialect), runs_several(text, dialect)) == (
        calls, commits, several)


@pytest.mark.parametrize(("text", "sets"), [
    ("SET autocommit = 1", True), ("SET SESSION autocommit=0", True), ("SET @@autocommit = 1", True),
    ("/*!40101 SET autocommit=1 */", True), ("SET @x = 1", False), ("SELECT @@autocommit", False),
])
def test_what_sets_autocommit(text, sets):  # MySQL: its own refusal all or nothing
    assert sets_autocommit(text, MYSQL) is sets
    assert sets_autocommit(text, PG) is False


@pytest.mark.parametrize(("text", "dialect", "ends"), [
    ("COMMIT AND CHAIN", PG, True), ("COMMIT", MYSQL, True), ("START TRANSACTION", MYSQL, True),
    ("ROLLBACK AND CHAIN", PG, True), ("END", SQLITE, True),
    ("ROLLBACK TO SAVEPOINT a", PG, False), ("ROLLBACK WORK TO a", MYSQL, False), ("BEGIN", PG, False),
    ("SAVEPOINT a", PG, False), ("COMMIT TRANSACTION", TSQL, False),  # T-SQL: an inner COMMIT only counts down
    ("COMMIT_x", PG, False),
])
def test_what_ends_the_transaction_before_it_even_when_one_is_open_after_it(text, dialect, ends):  # R5
    assert ends_the_transaction(text, dialect) is ends
