import pytest

from dbaguy_backend.domain.classification import Classification, DangerReason, GuardReason, StatementKind
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
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "WITH x AS (SELECT 1) SELECT set_config('default_transaction_read_only','off',false)",
    "SELECT pg_catalog.set_config('a.b', 'c', false)",
    'SELECT "set_config"(\'a.b\',\'c\',false)',
    "EXPLAIN ANALYZE SELECT set_config('a.b','c',false)",
    "SELECT * FROM (SELECT set_config('a.b','c',false)) s",
])
def test_read_only_blocks_everything_that_can_write(sql):
    assert decide(sql, "relaxed", read_only=True, confirmed=True) == (A.BLOCK, ["read_only"])


@pytest.mark.parametrize("sql", ["SELECT 1", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN SELECT 1", "SHOW search_path"])
def test_read_only_allows_reads_and_plain_transactions(sql):
    assert decide(sql, "standard", read_only=True)[0] == A.RUN


def test_strict_read_only_runs_reads_and_confirms_transactions():
    assert decide("SELECT 1", "strict", read_only=True) == (A.RUN, [])
    assert decide("BEGIN", "strict", read_only=True) == (A.CONFIRM, ["strict_transaction"])


def test_set_config_runs_when_the_profile_is_not_read_only():
    assert decide("SELECT set_config('a.b', 'c', false)") == (A.RUN, [])


def test_profiles_default_to_standard():
    assert SqliteProfileDraft(engine="sqlite", name="x", path="/tmp/x.db").safety == "standard"


def test_guard_reasons_are_the_danger_reasons_plus_the_strict_and_read_only_ones():
    strict = {"strict_write", "strict_ddl", "strict_transaction", "strict_session", "strict_unknown"}
    assert {r.value for r in GuardReason} == {r.value for r in DangerReason} | strict | {"read_only"}


@pytest.mark.parametrize("kind", [k for k in StatementKind if k != StatementKind.READ])
def test_strict_confirms_every_kind_but_read_with_a_typed_reason(kind):
    decision = policy.decide("x", Classification(kind=kind), PG, "strict", read_only=False)
    assert decision.action == A.CONFIRM and decision.reasons == [GuardReason(f"strict_{kind}")]
    assert all(isinstance(reason, GuardReason) for reason in decision.reasons)


def test_guard_reasons_are_plain_strings_on_the_wire():
    decision = policy.check("UPDATE t SET x = 1", PG, "standard", read_only=False)
    assert decision.model_dump(mode="json")["reasons"] == ["update_without_where"]
    assert isinstance(decision.reasons[0], GuardReason)
