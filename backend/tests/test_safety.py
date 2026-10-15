import pytest

from dbaguy_backend.domain.profiles import SqliteProfileDraft
from dbaguy_backend.services.safety import GuardAction as A, SafetyPolicy
from dbaguy_backend.sql.dialects import Dialect

PG = Dialect.POSTGRES
policy = SafetyPolicy()


def decide(sql, level="standard", read_only=False, confirmed=False):
    decision = policy.check(sql, PG, level, read_only, confirmed)
    return decision.action, decision.reasons


@pytest.mark.parametrize(("sql", "level", "expected"), [
    ("SELECT 1", "standard", (A.RUN, [])),
    ("UPDATE t SET x = 1", "standard", (A.CONFIRM, ["update_without_where"])),
    ("UPDATE t SET x = 1 WHERE id = 1", "standard", (A.RUN, [])),
    ("DELETE FROM t", "standard", (A.CONFIRM, ["delete_without_where"])),
    ("DROP TABLE t", "standard", (A.CONFIRM, ["drop"])),
    ("DROP TABLE t", "relaxed", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "standard", (A.RUN, [])),
    ("INSERT INTO t VALUES (1)", "strict", (A.CONFIRM, ["strict_write"])),
    ("DROP TABLE t", "strict", (A.CONFIRM, ["drop"])),
    ("frobnicate the database", "strict", (A.CONFIRM, ["strict_unknown"])),
    ("SELECT 1", "strict", (A.RUN, [])),
])
def test_levels(sql, level, expected):
    assert decide(sql, level) == expected


def test_confirmation_lets_it_run():
    assert decide("DROP TABLE t", confirmed=True) == (A.RUN, [])


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "DROP TABLE t", "SET default_transaction_read_only = off", "RESET ALL",
    "BEGIN READ WRITE", "START TRANSACTION READ WRITE", "EXPLAIN ANALYZE DELETE FROM t",
    "SELECT * FROM t FOR UPDATE", "frobnicate",
])
def test_read_only_blocks_everything_that_can_write(sql):
    assert decide(sql, "relaxed", read_only=True, confirmed=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", ["SELECT 1", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN SELECT 1", "SHOW search_path"])
def test_read_only_allows_reads_and_plain_transactions(sql):
    assert decide(sql, "standard", read_only=True)[0] == A.RUN


def test_strict_read_only_runs_reads_and_confirms_transactions():
    assert decide("SELECT 1", "strict", read_only=True) == (A.RUN, [])
    assert decide("BEGIN", "strict", read_only=True) == (A.CONFIRM, ["strict_transaction"])


def test_profiles_default_to_standard():
    assert SqliteProfileDraft(engine="sqlite", name="x", path="/tmp/x.db").safety == "standard"
