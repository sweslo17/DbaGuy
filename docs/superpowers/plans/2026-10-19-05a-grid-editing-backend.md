# 05a: Grid editing — backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The backend contract of grid editing for PostgreSQL, MySQL, SQL Server and SQLite: tell how a table's rows are identified and written (`POST /edits/target`), turn a tab's staged changes into the exact DML a commit will send, checked per cell (`POST /edits/preview`, a one-time plan), and apply that plan on the tab's own session in one explicit transaction (WS `apply_edits`), every statement matching exactly the rows it must, the outcome told as committed, rolled_back, partial or unknown and never resent; History keeps what was sent.

**Architecture:** Pure layers first. The cell model and literal rules of copy / Filter by this value move from `sql/formatting.py` to `sql/literals.py` unchanged (Task 2), then gain the per-column rules a DML needs: typed input → literal with per-cell problems, key and original-value conditions (`=`, PostgreSQL `::text`, `IS NULL`), T-SQL `N` only where it is needed, MySQL backslashes per `sql_mode` (Task 3). `sql/dml.py` builds the plan text — DELETE, UPDATE, INSERT between the dialect's transaction frame — with problems and warnings (Task 4). Each relational adapter answers `RowEditing.edit_target(path)` from its catalog through the engine-neutral `adapters/editing.build_target` (Tasks 5–6). `EditService` reads the target on the metadata connection, builds and stores a plan (`EditPlanStore`, single use, 15 min) and serves the two routes (Task 7). `SessionWorker._apply_edits` sends the plan's texts and only those, classifying the outcome with the pure `sessions/edit_apply.EditProgress` (Task 8). Live tests on scratch tables prove it per engine (Tasks 9–10); the frozen sidecar does it again and the measurements close (Task 11).

**Tech Stack:** Python 3.13, FastAPI, pydantic v2 (`JsonValue`), loguru; psycopg 3.3.6, mysql-connector-python 26.7.0 (pure), pymssql 2.4.4 (`_mssql`), sqlite3; pytest against the dev containers (PostgreSQL 17.11, MySQL 8.4.11, SQL Server 2022 CU27, SQLite 3.53.4). No new dependency. The frontend only gets regenerated API types.

**Spec:** docs/superpowers/specs/2026-10-19-05-grid-editing-design.md — §11 item 1 (05a): T1 probes A1, A2, A3, A5, A8; `sql/literals.py` extracted from `formatting.py`; `sql/dml.py`; the dialect's transaction frame, DEFAULT VALUES and backslash option; `domain/edits.py`; `RowEditing` and capability `row_editing`; the four engines' `edit_target`; `matched_rows()` (MySQL's info); `EditService`, the plan store, `/edits/target`, `/edits/preview`; the worker's `_apply_edits`, `ApplyEditsCommand`, `EditOutcomeEvent`, hooks, History; `npm run gen:api`; unit and integration tests; A4, A6, A7, A9, A13–A15; the frozen sidecar's preview and apply on every engine. Binding: R-05-1…R-05-12, D-05-1…D-05-15, §3.1–§3.5, §4, §5, §6, §7, §10, and the coordinator review CR-1…CR-4 (CR-2: random uuid4 plan ids bound to profile and database; CR-3: two tabs on one table are guarded by the original-value check; CR-4: key columns may be edited, the WHERE uses the loaded key). Not in 05a: the frontend (§3.6) and the workspace `staged_edits` model (§3.7), both 05b.

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 05a serves it, or avoids regressing it |
|---|---|
| §8 Grid 內直接編輯 → Must (S1) | The whole backend loop of "stage → preview the DML → one commit": `/edits/target`, `/edits/preview`, `apply_edits` (Tasks 3–8), live on all four relational engines (Tasks 9–10) and in the frozen sidecar (Task 11). It replaces the open-table → filter → edit → save of Navicat, DBeaver and Beekeeper (§8 "現在用的工具"). |
| §2 Q1 輕、快、低記憶體 | The preview is built on the request thread from a target of three catalog queries, never cached, never a result copy (Tasks 4, 7; 1,000 changes in < 300 ms, Task 4's test). The apply is one round trip per statement (SQL Server three, measured as A7 in Task 11). No new Python or frontend dependency; Task 11 re-measures the sidecar size and startup. |
| §2 Q2 穩定、不丟工作 | A failure never leaves a half-applied transactional table; a non-transactional one says exactly which changes took effect (`partial`); a COMMIT whose answer was lost is `unknown` and is never sent again; a plan runs at most once (Tasks 8–10). Keeping staged edits across a restart is 05b. |
| §2 Q3 可信任 | What is sent is what was previewed: the worker runs only the stored plan's texts (Task 8). Every UPDATE checks the original values of the columns it sets (PostgreSQL `::text`, exact), every statement must match exactly one row (a DELETE at most one), counts that cannot be read fail closed, read-only is refused twice (policy and server), and no value or key reaches a log above DEBUG (Tasks 3, 4, 8). |
| §2 Q4 聚焦 80% | Only table-data tabs; no query-result editing, views, keyless tables, Couchbase, pasting or type-to-edit (spec §1 out of scope; `row_editing` False for Couchbase, Task 5). |
| §2 Q5 多引擎從第一天 | One flow, one plan builder and one outcome table for the four engines; engine knowledge only in `edit_target` and the literal table (Tasks 3–6); the common flows run on every engine (Task 9). |
| §2 Q6 macOS 慣例 | Nothing to do in the backend beyond giving ⌘S's preview its exact text (Task 7) and each column's `kind` for Return's editor (Task 5); the keys are 05b. |
| §3 M7 值的呈現正確 | NULL and the empty string stay apart, numbers travel as text (a bigint beyond 2⁵³ as a key or a value), decimals, dates, UUIDs and JSON go as typed, binary as hex, DEFAULT only for a new row (Tasks 3, 9, 10). |
| §3 M10 錯誤 | The server's own text and code, the statement that failed and the change it belongs to; problems per cell with fixed texts (Tasks 4, 8). |
| §3 M11 安全與交易 | One explicit transaction per commit on the tab's own session; DELETE first in the plan; read-only blocked by the policy before anything is sent and again by the server (MySQL 1792, PostgreSQL READ ONLY, SQLite `mode=ro`); no transaction is ever left open (Tasks 4, 8–10). |

## Global Constraints

- Scope is spec §11 item 1 (05a) on branch `feat/05-grid-editing` (the spec and its coordinator review end at 8b0da80). No frontend code beyond the generated API types and the type exports in `src/lib/api/types.ts`; no workspace `staged_edits` (spec §3.7, 05b). No merge to `main` in this plan: ⑤ merges after 05b's final review.
- Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`), **no new Python dependency** and no new frontend dependency.
- Binding values, each defined once, in the file named:
  - `domain/edits.py`: `MAX_CHANGES = 10_000` (changes in one commit, D-05-11), `MAX_VALUE_CHARS = 1_048_576` (one value, D-05-11), `VALUE_ACCEPTED_CHARS = 4 * MAX_VALUE_CHARS` (beyond it the request is invalid, ruling E8), `PLAN_TTL_S = 900`, `MAX_PLANS = 32` (D-05-7), `PLAN_EXPIRED = "plan_expired"`, `PLAN_MISMATCH = "plan_mismatch"`.
  - `sql/dialects.py`: the transaction frames (D-05-4, verbatim) PostgreSQL `BEGIN` / `COMMIT`; MySQL `START TRANSACTION` / `COMMIT`; SQL Server `SET NOCOUNT OFF;\nBEGIN TRANSACTION` / `COMMIT TRANSACTION`; SQLite `BEGIN IMMEDIATE` / `COMMIT`. An all-default row: `DEFAULT VALUES`, MySQL `() VALUES ()`.
  - `sessions/edit_apply.py`: the outcome codes `edit_conflict`, `row_count_unknown` and the messages of spec §4 (verbatim below).
- From the spec, verbatim where it binds this plan:
  - R-05-5: 「每句 UPDATE／INSERT 必須剛好 1 列，否則整批 rollback：UPDATE 0 列＝載入後被改或刪……；> 1 列＝key 不唯一。DELETE 0 列不算失敗：該列已不存在……；> 1 列才失敗。」
  - R-05-6: 「COMMIT 送出之前的任何失敗（錯誤、衝突、取消、斷線）都是 rolled_back（非交易引擎為 partial）……COMMIT 送出之後 Cancel 不再傳到 driver。COMMIT 自己回錯誤……也是 rolled_back；只有連線在 COMMIT 途中斷掉（`connection_lost`）或 worker 意外例外才是 unknown。……從不自動重送。」
  - R-05-7: 「plan 存在記憶體（單次使用、15 分鐘、綁定 profile＋database＋path），worker 只執行 plan 裡的文字……worker 仍以 `SafetyPolicy` 檢查每一句（唯讀 → BLOCK）」. CR-2: 「plan id 是隨機的 UUID（`uuid4`）……plan 只能被同一個 profile＋database 的 session 取走」.
  - D-05-5: 「讀不到列數（`None`／-1）→ fail closed：rollback，「無法確認改了幾列，什麼都沒套用」。」
  - §3.5 event order: `started` → `affected`×n → [`error`] → `edit_outcome` → `session_state` → `finished`.
  - §4 messages: UPDATE 0 rows `Row <key> was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied`; > 1 `Row <key> matched N rows: its key is not unique. Nothing was applied`; `Could not confirm how many rows changed: nothing was applied`; `Cancelled: nothing was applied`; a failed COMMIT: the server's text + `The transaction was rolled back: nothing was applied`; preview read-only `This connection is read-only`; expired `The preview has expired: preview the changes again`.
  - §5: INFO the preview's profile, table (schema.name), counts, problem count, ms; the apply's counts, outcome, ms; a conflict's statement index and kind (never a key value). WARNING unknown, partial, unreadable counts. DEBUG every statement (with values; the worker's existing `_log_statement`). Never at INFO or above: values, key values, the server's error text (it echoes literals; codes only).
- Literals and identifiers only from `sql/dialects.py` (through `sql/literals.py`); `sql/` stays pure (no I/O, no logging); positions in UTF-16 through `adapters/positions.offset_to_line_col` / `sql/offsets.py` only.
- Threads (spec §3.4, CLAUDE.md): `edit_target` runs on the profile's metadata worker inside `MetadataService.with_connection` (a read: its one reconnect is fine); the plan is built on the request thread; the apply runs only on the tab session's worker thread, through `_run_statement` (armed cancel window). SQL Server: every driver call happens on the worker thread; the apply adds no cross-thread driver call (a Cancel reaches the adapter only through the existing `SessionWorker.cancel` → `connection.cancel()`, which for SQL Server only flags or uses `CancelControl`'s rules).
- Dev databases (connection details in CLAUDE.md 測試資料庫): tests and probes create only `zz_05a_*` objects (probes while planning used `zz_plan05a_*`), through `tests/edit_support.scratch()`, dropped by exact name when the block ends (planning's prototype runs of these tests used the same `zz_05a_*` names; none was left behind); SQLite uses a file in the test's `tmp_path`. Seeded tables are never written (reading `big_table` is not needed either), **Couchbase is never used**, every engine's seed stays exactly as it was. Never touch `~/Library/Application Support/DbaGuy` (not even `ls`); probes against the frozen sidecar use a temp `DBAGUY_DATA_DIR` and profiles with `"save_password": false`. **Never kill processes by pattern**: stop only what you started, by PID. The repo is public: only the fake dev credentials.
- `cd backend && .venv/bin/pytest -q` is green after every task with the dev containers up; a task report never shows a skipped `requires_pg` / `requires_mysql` / `requires_mssql` test. Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties): wait, or `docker compose -f dev/docker-compose.yml restart bastion`.
- After any backend model or API change: `npm run gen:api`, commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts` (never hand-edited), add the type exports to `src/lib/api/types.ts`; `npm run typecheck && npm run build` pass.
- Commit after each task, staging only that task's files by path (never `git add -A`; if `git status` shows files staged by someone else, commit with `git commit -m … -- <paths>`). The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.

## Review Focus

1. **Text the server would read differently from what was typed** — `it's`, a backslash (MySQL with and without `NO_BACKSLASH_ESCAPES`), `%` (pymssql), a newline, an emoji, U+0000, a lone surrogate, non-ASCII text into a non-Unicode SQL Server column — is stored exactly or refused on its cell, never silently changed. → Task 3 `test_input_literals`, `test_text_that_cannot_be_sent_is_a_problem`; Task 9 `test_a_commit_applies_deletes_updates_and_inserts_in_that_order` (every engine); Task 10 `test_backslashes_are_stored_as_typed_in_both_sql_modes`, `test_a_varchar_key_goes_without_n_and_unicode_text_with_it`.
2. **Names that need quoting** — a reserved word (`order`), mixed case, a space, `]` or a back-tick in a table or column name — are quoted in SET, VALUES, the key and the original-value conditions alike. → Task 4 `test_names_are_quoted_where_the_dialect_needs_it`; Task 9's shared table has a column named `order` on every engine.
3. **Keys and originals JavaScript cannot hold** — a bigint beyond 2⁵³ that the grid holds as a string, decimals with trailing zeros, SQLite integers in a column without a declared type — match exactly, so no false conflict and no missed row. → Task 3 `test_match_conditions`; Task 9 `test_a_key_beyond_two_to_the_53_matches_exactly` (every engine), `test_a_big_integer_in_a_column_without_a_type_is_found`; Task 10 `test_a_decimal_set_to_an_equal_value_is_matched_not_a_conflict`.
4. **The same plan applied twice** — a double Commit, a resent WebSocket command, another tab of the same profile — runs once; the second apply is `plan_expired` and sends nothing. → Task 7 `test_a_plan_is_taken_once_and_only_by_its_profile_and_database`; Task 8 `test_an_expired_plan_sends_nothing`; Task 9 `test_a_plan_is_applied_once`.
5. **A stop at any point leaves nothing half-applied on a transactional table and says what took effect otherwise** — an error in statement k, a cancel while a statement waits for a row lock, a COMMIT the server refuses, the connection lost at COMMIT, a trigger that skips the row or answers with a result set. → Task 8 (fakes, one test per outcome row); Task 9 `test_an_error_half_way_applies_nothing`, `test_cancel_while_waiting_for_a_lock_rolls_back`, `test_a_deferred_constraint_failing_at_commit_rolls_back`; Task 10 `test_a_partial_apply_on_myisam_says_what_took_effect`, `test_a_transaction_ending_error_rolls_back`.

---

## File Structure

```
backend/dbaguy_backend/
  domain/edits.py                 (new) EditKind, Compare, EditColumn, EditTarget, CellInput, RowUpdate, RowInsert,
                                  RowDelete, EditChanges, EditProblem, UncheckedColumn, PlannedStatement, EditWarning,
                                  EditCounts, EditPreview, EditPlan, PlanUnavailable, edit_kind(), kind_category(),
                                  the binding values
  domain/execution.py             ApplyEditsCommand (client), EditOutcome, EditOutcomeEvent (server)
  domain/metadata.py              path_database()
  domain/profiles.py              EngineInfo.row_editing
  domain/errors.py                NotEditable
  sql/literals.py                 (new; Task 2 moves cells, CellLiterals, value_condition here unchanged) LiteralStyle,
                                  ValueProblem, input_literal(), match_condition(), cell_text()
  sql/formatting.py               imports the moved names (behaviour unchanged)
  sql/dialects.py                 quote_literal / datetime_literal / json_literal options; TransactionFrame,
                                  transaction_frame(), insert_defaults(), script_text()
  sql/dml.py                      (new) DmlPlan, build_plan()
  adapters/base.py                AdapterCapabilities.row_editing, RowEditing, StatementResult.matched_rows()
  adapters/editing.py             (new) KeyCandidate, build_target(), read_only_relation(), the reasons
  adapters/postgres/{editing.py (new), adapter.py, loaders.py}
  adapters/sqlite/{editing.py (new), adapter.py}
  adapters/mysql/{editing.py (new), adapter.py, types.py}      (+ _VerbatimCursor.ok_info, rows_matched())
  adapters/mssql/{editing.py (new), adapter.py, types.py}
  services/edit_plans.py          (new) EditPlanStore
  services/edit_service.py        (new) EditService
  services/metadata_service.py    _call uses path_database()
  sessions/edit_apply.py          (new) EditReport, EditProgress: the outcome table of spec §3.5, pure
  sessions/worker.py              WorkerHooks.take_edit_plan; _apply_edits and its helpers; _start_request(before_finished)
  sessions/hooks.py               SessionHooksFactory(…, plans)
  api/edits.py                    (new) POST /edits/target, POST /edits/preview
  app.py, container.py            the router; AppContainer.edits; hooks get the plan store
backend/tests/
  edit_support.py                 (new) ScratchDb per engine, scratch(), items_table(); Task 9 adds EditApp, Tab,
                                  Applied and the change builders
  sql/test_literals.py, sql/test_dml.py, test_editing_target.py, test_edit_plans.py, test_edits_api.py,
  test_edit_models.py, test_edit_apply.py, test_edit_worker.py, test_edit_flows.py,
  test_postgres_edits.py, test_sqlite_edits.py, test_mysql_edits.py, test_mssql_edits.py   (new)
  test_dev_env.py, test_api.py    (extended)
src/lib/api/{openapi.json, schema.d.ts}   regenerated;  src/lib/api/types.ts   the new exports
CLAUDE.md, docs/superpowers/specs/2026-10-19-05-grid-editing-design.md
```

Task order follows dependencies. The probes and the scratch helpers (1) first: their results decide the frame and `matched_rows`. The pure refactor (2) lands before any new behaviour, then the literal rules (3) and the plan builder (4). The adapters answer targets (5: the contract, PostgreSQL, SQLite; 6: MySQL, SQL Server). The service and routes (7) need targets and plans; the worker (8) needs plans. Live flows (9: every engine's shared flows, PostgreSQL and SQLite specifics; 10: MySQL and SQL Server specifics) need everything; the packaged check, measurements and docs (11) close.

| # | Task | Suggested implementer |
|---|---|---|
| 1 | Probes A1, A2, A3, A5, A8; scratch-object helpers | Sonnet |
| 2 | `sql/literals.py` extracted from `formatting.py` (pure refactor) | Sonnet |
| 3 | Edit models, dialect options and the literal rules | Opus (cross-engine SQL) |
| 4 | The plan builder `sql/dml.py` | Opus (cross-engine SQL) |
| 5 | `RowEditing`, `row_editing`, `matched_rows()`; PostgreSQL and SQLite targets | Opus (catalogs) |
| 6 | MySQL and SQL Server targets; MySQL's matched rows | Opus (catalogs, driver) |
| 7 | `EditService`, the plan store, `/edits/target`, `/edits/preview` | Sonnet |
| 8 | The worker's apply, the WS protocol, hooks, History | Opus (transactions, concurrency) |
| 9 | Live flows on every engine; PostgreSQL and SQLite specifics | Opus (transactions, cross-engine) |
| 10 | MySQL and SQL Server specifics | Opus (transactions, driver) |
| 11 | Frozen-sidecar check, A6, A7, CLAUDE.md and the spec | Sonnet |

---
### Task 1: Probes A1, A2, A3, A5, A8 and the scratch-object helpers

**Files:**
- Create: `backend/tests/edit_support.py`
- Modify: `backend/tests/test_dev_env.py`
- Modify (only when a probe result differs from the table): this plan's "Assumptions recorded during execution" table
- Scratchpad only, not committed: `<scratchpad>/t1_probe.py`

**Interfaces:**
- Consumes: `tests.support` (`PG_*`, `MYSQL_*`, `MSSQL_*` constants, `mysql_driver_kwargs`, `mssql_connect`, `make_pg_profile`, `make_mysql_profile`, `make_mssql_profile`, `unique_name`, `requires_pg`, `requires_mysql`, `requires_mssql`); the adapters' `open()`; `dbaguy_backend.sql.dialects.force_quote_ident`.
- Produces (`tests/edit_support.py`, used by Tasks 5, 6, 9, 10, 11):
  - `SCRATCH_PREFIX = "zz_05a_"`; `ITEM_ROWS = ((1, "a", 1, None), (2, "b", 2, "x"), (3, "c", 3, "y"))`.
  - `class ScratchDb(ABC)` with attributes `engine: str`, `dialect: Dialect`, `database: str | None` (the explorer path's database segment), `schema: str`, the shared table's types `bigint`, `label`, `int`, `note`, `table_options: str`; methods `execute(*statements: str) -> None` (auto-commit, own driver), `rows(sql: str) -> list[tuple]`, `profile(**fields) -> dict` (a `ProfileUpsertRequest` body with the password in `secrets`), `open(**fields) -> DatabaseConnection` (the adapter's own connection), `holding(sql: str)` (a context manager: `sql` inside an open transaction on a second connection, rolled back at the end), `waiting() -> bool` (a statement of the dev login waits for a lock now), `fill(table: str, count: int, start: int) -> None` (`count` generated rows `(n, 'row n', n, NULL)` from `start`), `q(name) -> str`, `table(name) -> str` (schema-qualified, always quoted), `path(name, kind="table") -> list[PathSegment]` (kind `table`, `view` or `materialized_view`), `drop(kind, name) -> None`, `close() -> None`. Implementations `PgScratch`, `MysqlScratch`, `MssqlScratch`, `SqliteScratch(directory: Path)`.
  - `ENGINES` (pytest params `postgres`, `mysql`, `mssql`, `sqlite`, each server engine with its `requires_*` mark) and `SERVER_ENGINES` (the first three); `scratch_db(engine: str, directory: Path) -> ScratchDb`.
  - `scratch(db: ScratchDb, before_drop: Callable[[], None] | None = None)` — a context manager yielding `make(kind: str, ddl: str) -> str`: creates one object named `unique_name(SCRATCH_PREFIX + kind.lower().replace(" ", "_"))` (in `ddl`, `{t}` is replaced by its qualified name and `{n}` by its bare name) and returns the bare name; on exit runs `before_drop` (Task 9 passes "close every session", so no open transaction or paused result can hold a lock the DROP waits for), then drops every object by exact name, newest first, all attempted before one assertion.
  - `items_table(db, make, rows=ITEM_ROWS) -> str` — the shared table `(id <bigint> PRIMARY KEY, label <label> NOT NULL, "order" <int> NULL, note <note> NULL)` (`order` is a reserved word everywhere, so every statement must quote it), filled with `rows` in chunks of 500.

- [ ] **Step 1: Write the helpers' test** — append to `backend/tests/test_dev_env.py` (imports at the top: `import pytest`, `from tests.edit_support import ENGINES, SCRATCH_PREFIX, items_table, scratch, scratch_db`):

```python
@pytest.mark.parametrize("engine", ENGINES)
def test_scratch_objects_are_made_and_dropped_by_exact_name(engine, tmp_path):
    db = scratch_db(engine, tmp_path)
    try:
        with scratch(db) as make:
            table = items_table(db, make)
            assert table.startswith(SCRATCH_PREFIX + "table_")
            assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "a"), (2, "b"), (3, "c")]
            db.fill(db.table(table), 1000, 100)
            assert db.rows(f"SELECT COUNT(*) FROM {db.table(table)}") == [(1003,)]
        with pytest.raises(Exception):  # the driver's own "no such table" error
            db.rows(f"SELECT 1 FROM {db.table(table)}")
    finally:
        db.close()
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_dev_env.py -k scratch_objects`
Expected: FAIL (ModuleNotFoundError: `tests.edit_support`).

- [ ] **Step 3: Write the helpers** — `backend/tests/edit_support.py`:

```python
"""Grid-editing tests (05a): scratch objects on the dev databases, made and dropped by exact name.

Every object a test makes is named `zz_05a_<kind>_<random>` (several runs may share a server) and dropped by that name
when its `scratch()` block ends, whatever the test did. Seeded tables are never written, Couchbase is never used, and
each engine's seed is left exactly as it was. SQLite works on a file in the test's tmp_path. Set-up and checks go
through each engine's own driver, never through the code under test."""

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import mysql.connector
import psycopg
import pytest
from pymssql import _mssql

from dbaguy_backend.adapters.base import DatabaseConnection, Endpoint
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.sql.dialects import force_quote_ident
from tests.support import (
    MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, MSSQL_SA_PASSWORD, MSSQL_USER, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT,
    MYSQL_USER, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_mssql_profile, make_mysql_profile, make_pg_profile,
    mssql_connect, mysql_driver_kwargs, requires_mssql, requires_mysql, requires_pg, unique_name,
)

SCRATCH_PREFIX = "zz_05a_"
ITEM_ROWS: tuple[tuple[Any, ...], ...] = ((1, "a", 1, None), (2, "b", 2, "x"), (3, "c", 3, "y"))
_CHUNK = 500  # rows per INSERT (SQL Server takes at most 1,000 in one VALUES list)


class ScratchDb(ABC):
    """A dev database reached with its own driver in auto-commit mode."""

    engine: str
    dialect: Dialect
    database: str | None  # the explorer path's database segment; None when the engine's paths have none
    schema: str
    bigint: str  # the shared table's types (`items_table`)
    label: str
    int: str
    note: str
    table_options = ""

    @abstractmethod
    def execute(self, *statements: str) -> None: ...

    @abstractmethod
    def rows(self, sql: str) -> list[tuple]: ...

    @abstractmethod
    def profile(self, **fields: Any) -> dict:
        """A ProfileUpsertRequest body for this database, password in `secrets`; `fields` go over the profile's."""

    @abstractmethod
    def open(self, **fields: Any) -> DatabaseConnection:
        """The adapter's own connection to this database, as a metadata connection gets it."""

    @abstractmethod
    def holding(self, sql: str) -> AbstractContextManager[None]:
        """`sql` inside an open transaction on a second connection, rolled back when the block ends: its row locks are
        held meanwhile."""

    @abstractmethod
    def fill(self, table: str, count: int, start: int) -> None:
        """`count` generated rows `(n, 'row n', n, NULL)` of the shared table shape, n from `start`."""

    def waiting(self) -> bool:
        """Whether a statement of the dev login waits for a lock now."""
        raise NotImplementedError(f"{self.engine} has no lock waits to watch")

    def close(self) -> None:
        return None

    def q(self, name: str) -> str:
        return force_quote_ident(self.dialect, name)

    def table(self, name: str) -> str:
        return f"{self.q(self.schema)}.{self.q(name)}"

    def path(self, name: str, kind: str = "table") -> list[PathSegment]:
        head = [] if self.database is None else [PathSegment(kind="database", name=self.database)]
        folder = {"table": "tables", "view": "views", "materialized_view": "materialized_views"}[kind]
        return [*head, PathSegment(kind="schema", name=self.schema), PathSegment(kind="folder", name=folder),
                PathSegment(kind=kind, name=name)]

    def drop(self, kind: str, name: str) -> None:
        self.execute(f"DROP {kind} IF EXISTS {self.table(name)}")

    def literal(self, value: Any) -> str:
        """A plain literal for set-up SQL (ASCII text, numbers, NULL only)."""
        if value is None:
            return "NULL"
        return str(value) if isinstance(value, int) else "'" + str(value).replace("'", "''") + "'"


class PgScratch(ScratchDb):
    engine, dialect, database, schema = "postgres", Dialect.POSTGRES, "dbaguy", "public"
    bigint, label, int, note = "bigint", "varchar(100)", "integer", "text"

    def __init__(self) -> None:
        self._conn = self._connect(autocommit=True)

    @staticmethod
    def _connect(autocommit: bool) -> psycopg.Connection:
        return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                               autocommit=autocommit)

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute(statement)

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(row) for row in self._conn.execute(sql).fetchall()]

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "postgres", "name": "pg edits", "host": PG_HOST, "port": PG_PORT, "database": "dbaguy",
                   "username": PG_USER, "ssl": {"mode": "disable"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": PG_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return PostgresAdapter().open(make_pg_profile(**fields), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = self._connect(autocommit=False)
        try:
            conn.execute(sql)
            yield
        finally:
            conn.rollback()
            conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(f"INSERT INTO {table} (id, label, {self.q('order')}) "
                     f"SELECT g, 'row ' || g, g FROM generate_series({start}, {start + count - 1}) g")

    def waiting(self) -> bool:
        found = self.rows("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                          "AND wait_event_type = 'Lock'")
        return found[0][0] > 0

    def close(self) -> None:
        self._conn.close()


class MysqlScratch(ScratchDb):
    engine, dialect, database, schema = "mysql", Dialect.MYSQL, None, "dbaguy"
    bigint, label, int, note = "BIGINT", "VARCHAR(100)", "INT", "TEXT"
    table_options = " ENGINE=InnoDB"

    def __init__(self) -> None:
        # TLS (the kwargs' default) fills the server's caching_sha2 cache, so the `ssl: disable` profile below logs in
        self._conn = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))

    def execute(self, *statements: str) -> None:
        for statement in statements:
            cursor = self._conn.cursor()
            try:
                cursor.execute(statement)
                if cursor.with_rows:
                    cursor.fetchall()
            finally:
                cursor.close()

    def rows(self, sql: str) -> list[tuple]:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "mysql", "name": "mysql edits", "host": MYSQL_HOST, "port": MYSQL_PORT,
                   "database": "dbaguy", "username": MYSQL_USER, "ssl": {"mode": "disable"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": MYSQL_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return MysqlAdapter().open(make_mysql_profile(**fields), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = mysql.connector.connect(**mysql_driver_kwargs())
        try:
            conn.start_transaction()
            cursor = conn.cursor()
            cursor.execute(sql)
            cursor.close()
            yield
        finally:
            conn.rollback()
            conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(
            f"SET SESSION cte_max_recursion_depth = {count + 1}",
            f"INSERT INTO {table} (id, label, {self.q('order')}) WITH RECURSIVE s (n) AS "
            f"(SELECT {start} UNION ALL SELECT n + 1 FROM s WHERE n < {start + count - 1}) "
            "SELECT n, CONCAT('row ', n), n FROM s",
        )

    def waiting(self) -> bool:
        found = self.rows("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() "
                          "AND COMMAND = 'Query' AND INFO LIKE 'UPDATE %zz_05a_%'")
        return found[0][0] > 0

    def close(self) -> None:
        self._conn.close()


class MssqlScratch(ScratchDb):
    engine, dialect, database, schema = "mssql", Dialect.TSQL, "dbaguy", "dbo"
    bigint, label, int, note = "bigint", "nvarchar(100)", "int", "nvarchar(max)"

    def __init__(self) -> None:
        self._conn = mssql_connect()

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute_non_query(statement)

    def rows(self, sql: str) -> list[tuple]:
        return self._rows(self._conn, sql)

    @staticmethod
    def _rows(conn: _mssql.MSSQLConnection, sql: str) -> list[tuple]:
        conn.execute_query(sql)
        found = [tuple(row) for row in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE)]
        while conn.nextresult():
            for _ in conn.get_iterator(_mssql.ROW_FORMAT_TUPLE):
                pass
        return found

    def profile(self, **fields: Any) -> dict:
        profile = {"engine": "mssql", "name": "mssql edits", "host": MSSQL_HOST, "port": MSSQL_PORT,
                   "database": "dbaguy", "username": MSSQL_USER, "ssl": {"mode": "prefer"}}
        return {"profile": {**profile, **fields}, "secrets": {"password": MSSQL_PASSWORD}}

    def open(self, **fields: Any) -> DatabaseConnection:
        return MssqlAdapter().open(make_mssql_profile(**fields), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = mssql_connect()
        try:
            conn.execute_non_query(f"BEGIN TRANSACTION; {sql}")
            yield
        finally:
            conn.execute_non_query("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(
            f"WITH s AS (SELECT TOP ({count}) {start - 1} + ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n "
            "FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c) "
            f"INSERT INTO {table} (id, label, {self.q('order')}) SELECT n, CONCAT(N'row ', n), n FROM s"
        )

    def waiting(self) -> bool:
        # dm_exec_requests shows other sessions only with VIEW SERVER STATE, which the dbaguy login lacks: ask as sa
        sa = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database="master")
        try:
            found = self._rows(sa, "SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id <> 0 "
                                   "AND database_id = DB_ID(N'dbaguy')")
        finally:
            sa.close()
        return found[0][0] > 0

    def close(self) -> None:
        self._conn.close()


class SqliteScratch(ScratchDb):
    engine, dialect, database, schema = "sqlite", Dialect.SQLITE, None, "main"
    bigint, label, int, note = "INTEGER", "TEXT", "INTEGER", "TEXT"

    def __init__(self, directory: Path) -> None:
        self.file = directory / "edits.db"
        self._conn = sqlite3.connect(self.file, isolation_level=None)

    def execute(self, *statements: str) -> None:
        for statement in statements:
            self._conn.execute(statement)

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(row) for row in self._conn.execute(sql).fetchall()]

    def profile(self, **fields: Any) -> dict:
        return {"profile": {"engine": "sqlite", "name": "sqlite edits", "path": str(self.file), **fields}}

    def open(self, **fields: Any) -> DatabaseConnection:
        profile = SqliteProfile(id=uuid4(), engine="sqlite", name="s", path=str(self.file), **fields)
        return SqliteAdapter().open(profile, None, None, None)

    @contextmanager
    def holding(self, sql: str) -> Iterator[None]:
        conn = sqlite3.connect(self.file, isolation_level=None, timeout=0.1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql)
            yield
        finally:
            conn.execute("ROLLBACK")
            conn.close()

    def fill(self, table: str, count: int, start: int) -> None:
        self.execute(f"WITH RECURSIVE s (n) AS (SELECT {start} UNION ALL SELECT n + 1 FROM s WHERE n < "
                     f"{start + count - 1}) INSERT INTO {table} (id, label, {self.q('order')}) "
                     "SELECT n, 'row ' || n, n FROM s")

    def close(self) -> None:
        self._conn.close()


ENGINES = (
    pytest.param("postgres", marks=requires_pg, id="postgres"),
    pytest.param("mysql", marks=requires_mysql, id="mysql"),
    pytest.param("mssql", marks=requires_mssql, id="mssql"),
    pytest.param("sqlite", id="sqlite"),
)
SERVER_ENGINES = ENGINES[:3]


def scratch_db(engine: str, directory: Path) -> ScratchDb:
    if engine == "sqlite":
        return SqliteScratch(directory)
    return {"postgres": PgScratch, "mysql": MysqlScratch, "mssql": MssqlScratch}[engine]()


@contextmanager
def scratch(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[Callable[[str, str], str]]:
    """`make(kind, ddl)` creates one object and returns its bare name: in `ddl`, `{t}` is its qualified name and `{n}`
    its bare name. On exit `before_drop` runs first (close the sessions that could hold a lock the DROP waits for),
    then every object is dropped by exact name, newest first; every drop is tried before one assertion."""
    made: list[tuple[str, str]] = []

    def make(kind: str, ddl: str) -> str:
        name = unique_name(SCRATCH_PREFIX + kind.lower().replace(" ", "_"))
        made.append((kind, name))
        db.execute(ddl.replace("{t}", db.table(name)).replace("{n}", name))
        return name

    try:
        yield make
    finally:
        failures = []
        if before_drop is not None:
            try:
                before_drop()
            except Exception as exc:
                failures.append(f"before_drop: {exc}")
        for kind, name in reversed(made):
            try:
                db.drop(kind, name)
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        assert not failures, "dropping scratch objects failed: " + "; ".join(failures)


def items_table(db: ScratchDb, make: Callable[[str, str], str], rows: Sequence[tuple] = ITEM_ROWS) -> str:
    """The table the shared flows use: a bigint key, a NOT NULL label, a nullable int named `order` (reserved in every
    dialect) and a nullable note."""
    name = make("TABLE", f"CREATE TABLE {{t}} (id {db.bigint} PRIMARY KEY, label {db.label} NOT NULL, "
                         f"{db.q('order')} {db.int} NULL, note {db.note} NULL){db.table_options}")
    for start in range(0, len(rows), _CHUNK):
        values = ", ".join("(" + ", ".join(db.literal(v) for v in row) + ")" for row in rows[start:start + _CHUNK])
        db.execute(f"INSERT INTO {db.table(name)} (id, label, {db.q('order')}, note) VALUES {values}")
    return name
```

- [ ] **Step 4: Run it to see it pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_dev_env.py`
Expected: PASS (four `scratch_objects` cases, none skipped with the containers up).

- [ ] **Step 5: Run the probes.** Save as `<scratchpad>/t1_probe.py` and run from the repository root with `backend/.venv/bin/python <scratchpad>/t1_probe.py <scratchpad>` (about 5 seconds; it makes only `zz_05a_t1_*` tables, dropped by exact name, and a SQLite file in the scratchpad):

```python
"""05a Task 1 probes against the dev databases: A1 (MySQL OK-packet info), A2 (SQL Server NOCOUNT and the frame), A3
(SQL Server trigger counts), A5 (BEGIN / COMMIT through run() and the adapters' transaction state), A8 (SQL Server: a
varchar key written without N keeps its index seek)."""
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "backend")
from dbaguy_backend.adapters.base import Endpoint, StatementError  # noqa: E402
from dbaguy_backend.adapters.mssql.adapter import MssqlAdapter  # noqa: E402
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter  # noqa: E402
from dbaguy_backend.adapters.postgres.adapter import PostgresAdapter  # noqa: E402
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter  # noqa: E402
from dbaguy_backend.domain.profiles import SqliteProfile  # noqa: E402
from tests.support import (  # noqa: E402
    MSSQL_HOST, MSSQL_PASSWORD, MSSQL_PORT, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, PG_HOST, PG_PASSWORD, PG_PORT,
    make_mssql_profile, make_mysql_profile, make_pg_profile, mssql_connect,
)

scratch = Path(sys.argv[1])


def run(conn, sql):
    """affected_rows of the first set, plus (has columns, affected) of every later set when there are any."""
    result = conn.run(sql)
    rows, later = result.affected_rows(), []
    while result.next_result():
        later.append((result.columns() is not None, result.affected_rows()))
    result.close()
    return (rows, later) if later else rows


my = MysqlAdapter().open(make_mysql_profile(), Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None)
raw = my._conn
try:
    raw.cmd_query("DROP TABLE IF EXISTS zz_05a_t1_my")
    raw.cmd_query("CREATE TABLE zz_05a_t1_my (id INT PRIMARY KEY, d DECIMAL(30,10), v VARCHAR(20)) ENGINE=InnoDB")
    raw.cmd_query("INSERT INTO zz_05a_t1_my VALUES (1, 1.10, 'a'), (2, 2.5, 'b'), (3, 3, 'c')")
    for sql in ("UPDATE zz_05a_t1_my SET v = 'a' WHERE id = 1", "UPDATE zz_05a_t1_my SET d = 1.1 WHERE id = 1",
                "UPDATE zz_05a_t1_my SET v = 'x' WHERE id IN (2, 3)", "UPDATE zz_05a_t1_my SET v = 'x' WHERE id = 99",
                "INSERT INTO zz_05a_t1_my VALUES (4, 4, 'd')", "INSERT INTO zz_05a_t1_my (id) VALUES (5), (6)",
                "DELETE FROM zz_05a_t1_my WHERE id = 6", "DELETE FROM zz_05a_t1_my WHERE id = 99"):
        answer = raw.cmd_query(sql)
        print("A1", sql, "|", {k: answer.get(k) for k in ("affected_rows", "info_msg")})
    run(my, "START TRANSACTION")
    print("A5 mysql START TRANSACTION in_transaction", my.in_transaction(), "autocommit", my.autocommit())
    run(my, "UPDATE zz_05a_t1_my SET v = 'y' WHERE id = 1")
    run(my, "COMMIT")
    print("A5 mysql COMMIT in_transaction", my.in_transaction())
    run(my, "START TRANSACTION")
    try:
        run(my, "INSERT INTO zz_05a_t1_my VALUES (1, 1, 'dup')")
    except StatementError as exc:
        print("A5 mysql duplicate", exc.code, "in_transaction", my.in_transaction())
    my.rollback()
    print("A5 mysql rollback() in_transaction", my.in_transaction())
finally:
    raw.cmd_query("DROP TABLE IF EXISTS zz_05a_t1_my")
    my.close()

pg = PostgresAdapter().open(make_pg_profile(), Endpoint(PG_HOST, PG_PORT), PG_PASSWORD, None)
try:
    run(pg, "DROP TABLE IF EXISTS zz_05a_t1_pg")
    run(pg, "CREATE TABLE zz_05a_t1_pg (id int PRIMARY KEY, v text)")
    run(pg, "INSERT INTO zz_05a_t1_pg VALUES (1, 'a')")
    run(pg, "BEGIN")
    print("A5 pg BEGIN in_transaction", pg.in_transaction(), "failed", pg.transaction_failed())
    print("A5 pg same-value UPDATE rows", run(pg, "UPDATE zz_05a_t1_pg SET v = 'a' WHERE id = 1"))
    run(pg, "COMMIT")
    print("A5 pg COMMIT in_transaction", pg.in_transaction())
    run(pg, "BEGIN")
    try:
        run(pg, "INSERT INTO zz_05a_t1_pg VALUES (1, 'dup')")
    except StatementError as exc:
        print("A5 pg duplicate", exc.code, "in_transaction", pg.in_transaction(), "failed", pg.transaction_failed())
    pg.rollback()
    print("A5 pg rollback() in_transaction", pg.in_transaction())
finally:
    run(pg, "DROP TABLE IF EXISTS zz_05a_t1_pg")
    pg.close()

ms = MssqlAdapter().open(make_mssql_profile(), Endpoint(MSSQL_HOST, MSSQL_PORT), MSSQL_PASSWORD, None)
TABLES = ("zz_05a_t1_ms", "zz_05a_t1_ms_log", "zz_05a_t1_ms_io", "zz_05a_t1_coll")
try:
    for t in TABLES:
        run(ms, f"DROP TABLE IF EXISTS dbo.{t}")
    run(ms, "CREATE TABLE dbo.zz_05a_t1_ms (id int PRIMARY KEY, v nvarchar(20))")
    run(ms, "CREATE TABLE dbo.zz_05a_t1_ms_log (n int)")
    run(ms, "CREATE TABLE dbo.zz_05a_t1_ms_io (id int PRIMARY KEY, v nvarchar(20))")
    run(ms, "INSERT INTO dbo.zz_05a_t1_ms VALUES (1, N'a'), (2, N'b')")
    run(ms, "INSERT INTO dbo.zz_05a_t1_ms_io VALUES (1, N'a'), (2, N'b')")
    run(ms, "CREATE TRIGGER dbo.zz_05a_t1_after ON dbo.zz_05a_t1_ms AFTER UPDATE AS "
            "INSERT INTO dbo.zz_05a_t1_ms_log VALUES (1), (2), (3)")
    run(ms, "SET NOCOUNT ON")  # what a server whose `user options` turn NOCOUNT on gives every session
    print("A2 NOCOUNT ON: UPDATE rows", run(ms, "UPDATE dbo.zz_05a_t1_ms SET v = N'x' WHERE id = 1"))
    run(ms, "SET NOCOUNT OFF;\nBEGIN TRANSACTION")
    print("A5 mssql frame in_transaction", ms.in_transaction(), "failed", ms.transaction_failed())
    print("A2 framed UPDATE rows", run(ms, "UPDATE dbo.zz_05a_t1_ms SET v = N'y' WHERE id = 1"))
    print("A3 AFTER trigger inserting 3 rows: UPDATE rows", run(ms, "UPDATE dbo.zz_05a_t1_ms SET v = N'z' WHERE id = 2"))
    run(ms, "COMMIT TRANSACTION")
    print("A5 mssql COMMIT TRANSACTION in_transaction", ms.in_transaction())
    for body, label in (("DECLARE @x int", "does nothing"),
                        ("UPDATE dbo.zz_05a_t1_ms_io SET v = v", "updates every row (2)"),
                        ("SET NOCOUNT ON; UPDATE dbo.zz_05a_t1_ms_io SET v = v", "NOCOUNT ON inside"),
                        ("UPDATE t SET v = i.v FROM dbo.zz_05a_t1_ms_io t JOIN inserted i ON i.id = t.id; SELECT 1 AS x",
                         "updates, then returns a result set")):
        run(ms, "DROP TRIGGER IF EXISTS dbo.zz_05a_t1_instead")
        run(ms, f"CREATE TRIGGER dbo.zz_05a_t1_instead ON dbo.zz_05a_t1_ms_io INSTEAD OF UPDATE AS {body}")
        print("A3 INSTEAD OF,", label, "| 1-row UPDATE ->", run(ms, "UPDATE dbo.zz_05a_t1_ms_io SET v = N'q' WHERE id = 1"),
              "| 0-row UPDATE ->", run(ms, "UPDATE dbo.zz_05a_t1_ms_io SET v = N'q' WHERE id = 9"))
    run(ms, "SET NOCOUNT OFF;\nBEGIN TRANSACTION")
    try:
        run(ms, "UPDATE dbo.zz_05a_t1_ms SET v = N'q' WHERE id = CONVERT(int, 'x')")
    except StatementError as exc:
        print("A15 transaction-ending error", exc.code, "in_transaction", ms.in_transaction())
    ms.rollback()
    print("A15 rollback() in_transaction", ms.in_transaction())
    run(ms, "CREATE TABLE dbo.zz_05a_t1_coll (vc varchar(20) COLLATE SQL_Latin1_General_CP1_CI_AS PRIMARY KEY, v int)")
    run(ms, "INSERT INTO dbo.zz_05a_t1_coll SELECT TOP 2000 'k' + CAST(ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS "
            "varchar(10)), 1 FROM sys.all_objects a CROSS JOIN sys.all_objects b")
    plain = mssql_connect()
    try:
        plain.execute_non_query("SET SHOWPLAN_XML ON")
        for literal in ("'k42'", "N'k42'"):
            plain.execute_query(f"UPDATE dbo.zz_05a_t1_coll SET v = 2 WHERE vc = {literal} AND v = 1")
            xml = "".join(str(row[0]) for row in plain)
            print("A8", literal, "physical ops:", sorted({p.split('"')[0] for p in xml.split('PhysicalOp="')[1:]}))
        plain.execute_non_query("SET SHOWPLAN_XML OFF")
    finally:
        plain.close()
finally:
    for t in TABLES:
        run(ms, f"DROP TABLE IF EXISTS dbo.{t}")
    ms.close()

path = scratch / f"t1_{uuid4().hex[:6]}.db"
sqlite3.connect(path).executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a');")
lite = SqliteAdapter().open(SqliteProfile(id=uuid4(), engine="sqlite", name="s", path=str(path)), None, None, None)
try:
    run(lite, "BEGIN IMMEDIATE")
    print("A5 sqlite BEGIN IMMEDIATE in_transaction", lite.in_transaction())
    print("A5 sqlite same-value UPDATE rows", run(lite, "UPDATE t SET v = 'a' WHERE id = 1"))
    run(lite, "COMMIT")
    print("A5 sqlite COMMIT in_transaction", lite.in_transaction())
    lite.cancel()  # nothing runs: it must not stop the next statement
    print("A5 sqlite statement after an idle cancel", run(lite, "UPDATE t SET v = 'b' WHERE id = 1"))
    run(lite, "BEGIN IMMEDIATE")
    try:
        run(lite, "INSERT INTO t VALUES (1, 'dup')")
    except StatementError as exc:
        print("A5 sqlite duplicate", exc.code, "in_transaction", lite.in_transaction())
    lite.rollback()
    print("A5 sqlite rollback() in_transaction", lite.in_transaction())
finally:
    lite.close()
    path.unlink()
print("done")
```

Expected (what planning saw on 2026-10-19 with `zz_plan05a_*` tables; the table below has the details): A1 `Rows matched: 1  Changed: 0` for both same-value UPDATEs with `affected_rows` 0, `Rows matched: 2  Changed: 2`, `Rows matched: 0`, `info_msg` None for the one-row INSERT and both DELETEs, `Records: 2  Duplicates: 0` for the two-row INSERT; A2 `None` under NOCOUNT ON and `1` framed; A3 `1` with the AFTER trigger, INSTEAD OF `1` / `0` for every body except the one returning a result set (`None` / `None`); A5 every `in_transaction` True after the frame and False after COMMIT or `rollback()`, PostgreSQL's duplicate `23505` with `failed True`, MySQL `1062`, SQLite `SQLITE_CONSTRAINT_PRIMARYKEY` with the transaction still open, the same-value UPDATE `1` on PostgreSQL and SQLite; A15 code `245` and `in_transaction False`; A8 `'k42'` → `['Clustered Index Seek', 'Clustered Index Update']`, `N'k42'` → `['Clustered Index Scan', 'Clustered Index Update']`; `done`.

- [ ] **Step 6: Record the results.** If a line differs from the table, write the result over that row's Result column and apply its Effect (each row says what changes for both outcomes); otherwise leave the plan as it is.

- [ ] **Step 7: Run the whole backend suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green, no `requires_*` test skipped.

```bash
git add backend/tests/edit_support.py backend/tests/test_dev_env.py
git commit -m "test: 05a probes (A1 A2 A3 A5 A8) and scratch-object helpers for the grid-editing tests"
```

(This plan file too, only if Step 6 changed it.)

---
### Task 2: `sql/literals.py` extracted from `formatting.py` (pure refactor)

**Files:**
- Create: `backend/dbaguy_backend/sql/literals.py`
- Modify: `backend/dbaguy_backend/sql/formatting.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (`sql/literals.py`; Task 3 extends it, `formatting.py` imports it):
  - `Source = Literal["encoded", "raw"]`; `HEX_BYTES` (compiled `(?:[0-9a-fA-F]{2})*`).
  - the cell model: `Binary(hex: str, truncated: bool = False)`, `Truncated(preview: str)`, `Json(text: str)`, `MissingCell`, `MISSING_CELL`, `Cell = None | bool | int | float | str | Binary | Truncated | Json | MissingCell`, `MalformedCell(Exception)`.
  - `normalise_raw(value: Any) -> Cell`, `normalise_encoded(value: Any) -> Cell` (raises `MalformedCell`), `NORMALISERS: dict[Source, Callable[[Any], Cell]]`.
  - `CellLiterals(dialect: Dialect)` with `truncated: int` and `__call__(cell: Cell, category: ColumnCategory) -> str` (was `_SqlLiterals.__call__(cell, column: FormatColumn)`; it only ever read `column.category`).
  - `value_condition(dialect: Dialect, column: str, value: Any, category: ColumnCategory) -> str | None` (moved; `formatting` re-exports it, so `from dbaguy_backend.sql.formatting import value_condition` keeps working).

No behaviour changes: every existing test passes unchanged, and no test is added or edited.

- [ ] **Step 1: Record the baseline**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_formatting.py tests/test_couchbase_literals.py tests/test_sql_api.py tests/test_export.py`
Expected: PASS; note the count.

- [ ] **Step 2: Create `backend/dbaguy_backend/sql/literals.py`** — the cell model, normalisers, `CellLiterals` and `value_condition` moved from `formatting.py`, renamed public, the rest verbatim:

```python
"""Grid cells and SQL literals: one normalised shape for a cell, whether a raw driver value (export) or what
`encode_row` sent the grid (copy, Filter by this value, the grid's DML), and how a cell is written as a SQL literal or
matched in a WHERE (spec ⑤ D-05-1). Pure: no I/O; quoting and literal syntax come from `dialects` only."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..adapters.values import MISSING, JsonText, encode_value
from ..domain.column_category import ColumnCategory
from ..domain.errors import ValidationFailed
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, json_literal, quote_ident, quote_literal,
    zero_date_as_text,
)

Source = Literal["encoded", "raw"]
HEX_BYTES = re.compile(r"(?:[0-9a-fA-F]{2})*")


@dataclass(frozen=True, slots=True)
class Binary:
    hex: str  # complete value, or only the grid's preview when `truncated`
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Truncated:
    """Text the grid cut at its size limit; only the preview is known."""

    preview: str


@dataclass(frozen=True, slots=True)
class Json:
    """A JSON value as its text (Couchbase objects, arrays, exact numbers): written as it is, never re-encoded."""

    text: str


class MissingCell:
    """SQL++ MISSING: the row has no such key. Empty in text formats, a left-out key in JSON (R-04-8)."""

    __slots__ = ()


MISSING_CELL = MissingCell()
Cell = None | bool | int | float | str | Binary | Truncated | Json | MissingCell


def normalise_raw(value: Any) -> Cell:
    if isinstance(value, JsonText):  # before encode_value, which would cut a long document for the grid
        return Json(value.text)
    if value is MISSING:
        return MISSING_CELL
    if isinstance(value, str):  # before encode_value, which would cut long text for the grid
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return Binary(bytes(value).hex())
    return encode_value(value)  # scalars unchanged; big ints, Decimal, dates, UUID, list/dict become text


class MalformedCell(Exception):
    """An encoded cell that is not shaped like `encode_value`'s output. The reason never contains the cell."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _encoded_binary(cell: dict[str, Any]) -> Binary:
    preview, size = cell.get("preview"), cell.get("size")
    if not (isinstance(preview, str) and HEX_BYTES.fullmatch(preview) and _is_int(size) and size >= 0):
        raise MalformedCell("a bytes cell needs a preview of whole hex bytes and a size of at least 0")
    return Binary(preview, truncated=len(preview) < 2 * size)  # preview is hex: 2 chars a byte


def _encoded_text(cell: dict[str, Any]) -> str | Truncated:
    preview, size, truncated = cell.get("preview"), cell.get("size"), cell.get("truncated", False)
    if not (isinstance(preview, str) and _is_int(size) and isinstance(truncated, bool)):
        raise MalformedCell("a text cell needs a text preview, a size and, optionally, a true/false truncated flag")
    return Truncated(preview) if truncated else preview


def _encoded_json(cell: dict[str, Any]) -> Json:
    text = cell.get("text")
    if not isinstance(text, str):
        raise MalformedCell("a json cell needs its text")
    return Json(text)


def normalise_encoded(value: Any) -> Cell:
    """Raises `MalformedCell`: the only objects an encoded cell can be are `encode_value`'s bytes, text, json and
    missing cells."""
    if isinstance(value, dict):
        match value.get("$type"):
            case "bytes":
                return _encoded_binary(value)
            case "text":
                return _encoded_text(value)
            case "json":
                return _encoded_json(value)
            case "missing":
                return MISSING_CELL
            case _:
                raise MalformedCell("an object cell must be a bytes, text, json or missing cell")
    return normalise_raw(value)


NORMALISERS: dict[Source, Callable[[Any], Cell]] = {"encoded": normalise_encoded, "raw": normalise_raw}


class CellLiterals:
    """Renders cells as SQL literals; a cell cut by the grid cannot be restored, so it becomes NULL and is counted."""

    def __init__(self, dialect: Dialect) -> None:
        self._dialect = dialect
        self.truncated = 0

    def __call__(self, cell: Cell, category: ColumnCategory) -> str:
        match cell:
            case None:
                return "NULL"
            case MissingCell():
                return "MISSING"
            case Json():
                return json_literal(self._dialect, cell.text)
            case bool():
                return bool_literal(self._dialect, cell)
            case Truncated() | Binary(truncated=True):
                self.truncated += 1
                return "NULL"
            case Binary():
                return bytes_literal(self._dialect, cell.hex)
            case int() | float():
                return str(cell)
            case _ if category == ColumnCategory.DATETIME:
                return datetime_literal(self._dialect, cell)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else quote_literal(self._dialect, cell)


def value_condition(dialect: Dialect, column: str, value: Any, category: ColumnCategory) -> str | None:
    """The "Filter by this value" condition (spec §3.7) for one encoded grid cell, in the dialect's syntax.

    The database decides what matches, by the column's collation: MySQL's default collations ignore case, accents and
    trailing spaces, so the filter can return more rows than the one clicked. A JSON cell is compared as JSON where the
    dialect needs it said (MySQL), and a MySQL date with a zero part as text (R-03a-T9c). None when the cell is only a
    preview (cut text or bytes): no condition can be built from it."""
    name = quote_ident(dialect, column)
    if value is None:
        return f"{name} IS NULL"
    try:
        cell = normalise_encoded(value)
    except MalformedCell as malformed:
        raise ValidationFailed(f"Not a grid cell: {malformed}") from None
    if cell is MISSING_CELL:
        return f"{name} IS MISSING"
    if isinstance(cell, Truncated) or (isinstance(cell, Binary) and cell.truncated):
        return None
    if isinstance(cell, Json):
        return f"{name} = {json_literal(dialect, cell.text)}"
    if category == ColumnCategory.JSON and isinstance(cell, str):
        return f"{name} = {json_literal(dialect, cell)}"
    if category == ColumnCategory.DATETIME and isinstance(cell, str) and (text := zero_date_as_text(dialect, name, cell)):
        return f"{text} = {quote_literal(dialect, cell)}"
    return f"{name} = {CellLiterals(dialect)(cell, category)}"
```

- [ ] **Step 3: Make `formatting.py` use it.** Delete exactly the definitions that moved — `Source`, `_HEX_BYTES`, `_Binary`, `_Truncated`, `_Json`, `_MissingCell`, `_MISSING_CELL`, `Cell`, `_normalise_raw`, `_MalformedCell`, `_is_int`, `_encoded_binary`, `_encoded_text`, `_encoded_json`, `_normalise_encoded`, `_NORMALISERS`, `_SqlLiterals`, `value_condition` — and keep everything else where it is (`_ELLIPSIS`, `_JSON_NUMBER`, `_NEWLINES`, `_TSV_SPECIAL`, `_UNSUPPORTED`, `RAW_JSON_FORMATS`, `FormatResult`, `unsupported_reason`, `_normalise_row`, `_cells`, every renderer, writer and entry point). Rename the uses: `_Binary`→`Binary`, `_Truncated`→`Truncated`, `_Json`→`Json`, `_MissingCell`→`MissingCell`, `_MISSING_CELL`→`MISSING_CELL`, `_MalformedCell`→`MalformedCell`, `_NORMALISERS`→`NORMALISERS`, `_SqlLiterals`→`CellLiterals`; the three literal calls pass the category: `_InsertWriter._write_row` → `self._literals(cell, column.category)`, `_in_list` → `literals(cells[0], columns[0].category)`. The import block becomes:

```python
import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from decimal import Decimal
from io import StringIO
from typing import Any, TextIO

from pydantic import BaseModel

from ..domain.column_category import ColumnCategory
from ..domain.errors import UnsupportedFormat, ValidationFailed
from ..domain.formats import FormatColumn, FormatKind, RowFormatOptions
from .dialects import Dialect, in_list, qualified, quote_ident
from .jsontext import one_line, reject_constant
from .literals import (
    MISSING_CELL, NORMALISERS, Binary, Cell, CellLiterals, Json, MalformedCell, MissingCell, Source, Truncated,
    value_condition,
)
```

`__all__` keeps `value_condition` (re-exported). Nothing else changes: the writers, `RawJsonWriter`, `format_rows`, `open_writer`, `open_raw_json_writer` and `_in_list` stay as they are.

- [ ] **Step 4: Run the same tests and the whole suite**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_formatting.py tests/test_couchbase_literals.py tests/test_sql_api.py tests/test_export.py && .venv/bin/pytest -q`
Expected: the same count as Step 1, all passing; the whole suite green. `git diff --stat backend/tests` is empty.

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/sql/literals.py backend/dbaguy_backend/sql/formatting.py
git commit -m "refactor(sql): cells and their literals move to sql/literals.py (no behaviour change)"
```

---
### Task 3: Edit models, dialect options and the literal rules

**Files:**
- Create: `backend/dbaguy_backend/domain/edits.py`
- Modify: `backend/dbaguy_backend/domain/metadata.py` (`path_database`)
- Modify: `backend/dbaguy_backend/sql/dialects.py`
- Modify: `backend/dbaguy_backend/sql/literals.py`
- Create: `backend/tests/sql/test_literals.py`, `backend/tests/test_edit_models.py`

**Interfaces:**
- Consumes: Task 2 (`sql/literals.py`: the cell model, `CellLiterals`, `value_condition`); `sql/jsontext.json_problem`, `LONE_SURROGATE`; `adapters/positions.offset_to_line_col`; `adapters/values.MAX_SAFE_INT`.
- Produces:
  - `domain/edits.py` — every model of the edit contract (code below): `EditKind`, `Compare`, `EditColumn`, `EditTarget`, `CellInput`, `RowUpdate`, `RowInsert`, `RowDelete`, `EditChanges`, `EditProblemCode`, `EditProblem`, `UncheckedColumn`, `PlannedStatement`, `EditWarningCode`, `EditWarning`, `EditCounts`, `EditPreview`, `EditPlan`, `PlanUnavailable(code: str, message: str)`, `edit_kind(category: ColumnCategory) -> EditKind`, `kind_category(kind: EditKind) -> ColumnCategory`, and the binding values (`MAX_CHANGES`, `MAX_VALUE_CHARS`, `VALUE_ACCEPTED_CHARS`, `PLAN_TTL_S`, `MAX_PLANS`, `PLAN_EXPIRED`, `PLAN_MISMATCH`, `PLAN_EXPIRED_MESSAGE`, `PLAN_MISMATCH_MESSAGE`).
  - `domain/metadata.path_database(path: Sequence[PathSegment]) -> str | None`.
  - `sql/dialects.py`: `quote_literal(dialect, value, *, national: bool = True, backslash_escapes: bool | None = None) -> str`; `datetime_literal(dialect, text, *, backslash_escapes: bool | None = None) -> str`; `json_literal(dialect, text, *, national: bool = True, backslash_escapes: bool | None = None) -> str`; `TransactionFrame(begin: str, commit: str)`, `transaction_frame(dialect) -> TransactionFrame` (ValueError for SQL++); `insert_defaults(dialect, table_sql: str) -> str`; `script_text(dialect, statements: Sequence[str]) -> str`.
  - `sql/literals.py`: `LiteralStyle(national: bool = True, backslash_escapes: bool | None = None)` with `quote(dialect, text)`, `json(dialect, text)`, `datetime(dialect, text)`; `DEFAULT_STYLE`; `CellLiterals(dialect, style=DEFAULT_STYLE)`; `value_condition(dialect, column, value, category, style=DEFAULT_STYLE)`; `ValueProblem(code: EditProblemCode, message: str)`; `input_literal(dialect, column: EditColumn, cell: CellInput, style: LiteralStyle) -> str`; `match_condition(dialect, column: EditColumn, value: JsonValue, style: LiteralStyle, *, key: bool) -> str | None`; `cell_text(value: JsonValue, kind: EditKind) -> str`.

Rules (spec §3.3.3, D-05-2, D-05-13; rulings E5, E7, E16, E17):
- A new value is read by the column's kind: **number** — stripped, `NUMERIC_LITERAL`, bare; PostgreSQL also `NaN`, `Infinity`, `-Infinity` (any case) as quoted literals; else `invalid_number`. **bool** — `true t 1 yes` / `false f 0 no` (any case, stripped) → `bool_literal`; else `invalid_bool`. **binary** — stripped, an optional `0x`, whole hex bytes (empty allowed) → `bytes_literal` with lower-case digits; else `invalid_hex`. **json** — `json_problem` (else `invalid_json` "Not valid JSON at line L, column C: <reason>") then a quoted literal (MySQL: plain `'…'`, the JSON column parses it). **datetime** — `datetime_literal` (T-SQL `N'…T…'`). **text, other** — quoted; SQLite `other` (no declared type) takes a numeric text bare. NULL → `NULL`; DEFAULT has no literal (ValueError: the plan leaves the column out).
- Quoted text first passes `invalid_text`: a lone surrogate anywhere (defensive: the request model already refuses one with a 422 that names the cell, ruling E19); U+0000 in PostgreSQL, and in MySQL without backslash escapes (E16). Above `MAX_VALUE_CHARS` → `value_too_large`. A problem message is fixed text and never quotes the value.
- T-SQL `N`: a non-Unicode column (`EditColumn.unicode` False → `LiteralStyle(national=False)`) gets plain `'…'` for ASCII text and `N'…'` otherwise (E17); datetime literals keep `N`.
- Match terms: `NULL` → `c IS NULL` whatever the compare (E5); a preview-only cell (cut text, partial bytes) or a string holding a lone surrogate (E19) → None; a key always `=`; an original by its compare — `equals` as `value_condition` writes it (MySQL JSON `CAST(… AS JSON)`, zero dates `CAST(c AS CHAR)`), `as_text` → `c::text = '<the server's text>'` (bool `'true'`/`'false'`, bytea `'\x…'`), `none` → None. SQLite `other` with an integer string beyond ±(2⁵³−1) → `c IN (n, 'n')` (E7). A malformed cell is `ValidationFailed` (as `value_condition`).
- With the default style, `CellLiterals` and `value_condition` write exactly what they wrote before (the existing formatting tests guard it).

- [ ] **Step 1: Write the failing tests** — `backend/tests/sql/test_literals.py`:

```python
"""Literals of grid edits (spec ⑤ §3.3.3, D-05-2, D-05-13): typed values, keys and original values per dialect × kind,
and the dialect's transaction frame, all-default row and script text."""

import pytest

from dbaguy_backend.domain.edits import MAX_VALUE_CHARS, CellInput, EditColumn
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.sql.dialects import (
    Dialect, TransactionFrame, datetime_literal, insert_defaults, json_literal, quote_literal, script_text,
    transaction_frame,
)
from dbaguy_backend.sql.literals import LiteralStyle, ValueProblem, cell_text, input_literal, match_condition

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE
PLAIN = LiteralStyle()
NO_BACKSLASH = LiteralStyle(backslash_escapes=False)
VARCHAR = LiteralStyle(national=False)
BYTES = {"$type": "bytes", "size": 2, "preview": "00ff"}
PARTIAL_BYTES = {"$type": "bytes", "size": 100, "preview": "00" * 64}
CUT_TEXT = {"$type": "text", "preview": "abc", "size": 70_000, "truncated": True}


def col(kind="text", compare="equals", name="c", **fields) -> EditColumn:
    return EditColumn(name=name, type_name=kind, kind=kind, nullable=True, has_default=False, writable=True,
                      compare=compare, **fields)


def value(text: str) -> CellInput:
    # model_construct: a lone surrogate never passes the model (ruling E19); the builder still refuses it itself
    return CellInput.model_construct(kind="value", text=text)


@pytest.mark.parametrize(("dialect", "kind", "text", "style", "expected"), [
    (PG, "number", " 42 ", PLAIN, "42"),
    (PG, "number", "9007199254740993", PLAIN, "9007199254740993"),
    (MY, "number", "1.10", PLAIN, "1.10"),
    (TS, "number", "-1e3", PLAIN, "-1e3"),
    (PG, "number", "NaN", PLAIN, "'NaN'"),
    (PG, "number", "-infinity", PLAIN, "'-Infinity'"),
    (PG, "text", "it's", PLAIN, "'it''s'"),
    (MY, "text", "C:\\tmp\\", PLAIN, "'C:\\\\tmp\\\\'"),
    (MY, "text", "C:\\tmp\\", NO_BACKSLASH, "'C:\\tmp\\'"),
    (TS, "text", "50% done", PLAIN, "N'50% done'"),
    (TS, "text", "k42", VARCHAR, "'k42'"),
    (TS, "text", "café", VARCHAR, "N'café'"),
    (LITE, "text", "line 1\nline 2 😀", PLAIN, "'line 1\nline 2 😀'"),
    (PG, "bool", " Yes ", PLAIN, "TRUE"),
    (MY, "bool", "F", PLAIN, "FALSE"),
    (TS, "bool", "1", PLAIN, "1"),
    (LITE, "bool", "false", PLAIN, "0"),
    (PG, "binary", "0xABcd", PLAIN, "'\\xabcd'::bytea"),
    (MY, "binary", "00ff", PLAIN, "X'00ff'"),
    (TS, "binary", "0x", PLAIN, "0x"),
    (LITE, "binary", "", PLAIN, "X''"),
    (PG, "json", '{"a": [1, 2.50]}', PLAIN, "'{\"a\": [1, 2.50]}'"),
    (MY, "json", '{"a": 1}', PLAIN, "'{\"a\": 1}'"),
    (TS, "json", '{"a": 1}', PLAIN, "N'{\"a\": 1}'"),
    (TS, "datetime", "2026-10-19 12:34:56.123", PLAIN, "N'2026-10-19T12:34:56.123'"),
    (MY, "datetime", "0000-00-00", PLAIN, "'0000-00-00'"),
    (PG, "datetime", "2026-10-19 12:34:56+08", PLAIN, "'2026-10-19 12:34:56+08'"),
    (LITE, "other", " 42 ", PLAIN, "42"),
    (LITE, "other", "x42", PLAIN, "'x42'"),
    (PG, "other", "{1,2}", PLAIN, "'{1,2}'"),
    (MY, "text", "a\x00b", PLAIN, "'a\\0b'"),
    (TS, "text", "a\x00", PLAIN, "N'a' + CAST(NCHAR(0) AS NVARCHAR(MAX)) + N''"),
    (LITE, "text", "a\x00", PLAIN, "'a' || char(0) || ''"),
])
def test_input_literals(dialect, kind, text, style, expected):  # Review Focus 1
    assert input_literal(dialect, col(kind), value(text), style) == expected


def test_null_is_null_and_default_has_no_literal():
    assert input_literal(PG, col("number"), CellInput(kind="null"), PLAIN) == "NULL"
    with pytest.raises(ValueError):
        input_literal(PG, col("number"), CellInput(kind="default"), PLAIN)


@pytest.mark.parametrize(("dialect", "kind", "text", "style", "code"), [
    (PG, "number", "12a", PLAIN, "invalid_number"),
    (MY, "number", "NaN", PLAIN, "invalid_number"),
    (TS, "number", "", PLAIN, "invalid_number"),
    (PG, "bool", "maybe", PLAIN, "invalid_bool"),
    (MY, "binary", "abc", PLAIN, "invalid_hex"),
    (TS, "binary", "0xzz", PLAIN, "invalid_hex"),
    (PG, "json", "[1,", PLAIN, "invalid_json"),
    (PG, "text", "a\x00b", PLAIN, "invalid_text"),
    (MY, "text", "a\x00b", NO_BACKSLASH, "invalid_text"),
    (LITE, "text", "half \ud83d", PLAIN, "invalid_text"),
    (TS, "datetime", "\udfff", PLAIN, "invalid_text"),
])
def test_text_that_cannot_be_sent_is_a_problem(dialect, kind, text, style, code):  # Review Focus 1
    with pytest.raises(ValueProblem) as raised:
        input_literal(dialect, col(kind), value(text), style)
    assert raised.value.code == code


@pytest.mark.parametrize("kind", ["number", "bool", "binary", "json"])
def test_a_problem_never_quotes_the_value(kind):  # spec §4: DbaGuy's own texts never echo values
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col(kind), value("zz-secret-7"), PLAIN)
    assert "zz-secret" not in raised.value.message


def test_invalid_json_says_where():
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col("json"), value('{\n  "a": 1,\n}'), PLAIN)
    assert raised.value.message == "Not valid JSON at line 2, column 9: Illegal trailing comma before end of object"


def test_a_value_beyond_the_limit_is_too_large():
    with pytest.raises(ValueProblem) as raised:
        input_literal(PG, col("text"), value("x" * (MAX_VALUE_CHARS + 1)), PLAIN)
    assert raised.value.code == "value_too_large"


@pytest.mark.parametrize(("dialect", "column", "cell", "key", "style", "expected"), [
    (PG, col("number", "as_text", "id"), 42, True, PLAIN, "id = 42"),
    (PG, col("number", "as_text", "id"), "9007199254740993", True, PLAIN, "id = 9007199254740993"),
    (PG, col("number", "as_text", "n"), "1.1000000000", False, PLAIN, "n::text = '1.1000000000'"),
    (PG, col("bool", "as_text", "b"), True, False, PLAIN, "b::text = 'true'"),
    (PG, col("binary", "as_text", "by"), BYTES, False, PLAIN, "by::text = '\\x00ff'"),
    (PG, col("json", "as_text", "j"), '{"b": 1,  "a": 2}', False, PLAIN, "j::text = '{\"b\": 1,  \"a\": 2}'"),
    (PG, col("text", "equals", "code"), "ab   ", False, PLAIN, "code = 'ab   '"),
    (PG, col("text", "as_text", "order"), "x", False, PLAIN, "\"order\"::text = 'x'"),
    (PG, col("number", "none", "f"), None, False, PLAIN, "f IS NULL"),
    (PG, col("number", "none", "f"), 1.5, False, PLAIN, None),
    (PG, col("text", "as_text", "t"), CUT_TEXT, False, PLAIN, None),
    (PG, col("binary", "as_text", "by"), PARTIAL_BYTES, True, PLAIN, None),
    (PG, col("text", "as_text", "t"), "half \ud83d", True, PLAIN, None),
    (MY, col("json", "equals", "doc"), '{"a": 1}', False, PLAIN, "doc = CAST('{\"a\": 1}' AS JSON)"),
    (MY, col("datetime", "equals", "d"), "0000-00-00", False, PLAIN, "CAST(d AS CHAR) = '0000-00-00'"),
    (MY, col("number", "equals", "amount"), "1.10", False, PLAIN, "amount = 1.10"),
    (MY, col("text", "equals", "path"), "C:\\x", False, NO_BACKSLASH, "path = 'C:\\x'"),
    (MY, col("text", "equals", "order"), "x", True, PLAIN, "`order` = 'x'"),
    (TS, col("text", "equals", "vc", unicode=False), "k42", True, VARCHAR, "vc = 'k42'"),
    (TS, col("text", "equals", "nv"), "k42", True, PLAIN, "nv = N'k42'"),
    (TS, col("text", "equals", "vc", unicode=False), "é", True, VARCHAR, "vc = N'é'"),
    (TS, col("datetime", "equals", "dt"), "2026-10-19 12:34:56.123", False, PLAIN, "dt = N'2026-10-19T12:34:56.123'"),
    (TS, col("bool", "equals", "flag"), True, False, PLAIN, "flag = 1"),
    (TS, col("binary", "equals", "vb"), BYTES, True, PLAIN, "vb = 0x00ff"),
    (TS, col("number", "equals", "order"), 7, True, PLAIN, "[order] = 7"),
    (LITE, col("number", "equals", "i"), 42, True, PLAIN, "i = 42"),
    (LITE, col("number", "equals", "i"), "9007199254740993", True, PLAIN, "i = 9007199254740993"),
    (LITE, col("text", "equals", "t"), "42", False, PLAIN, "t = '42'"),
    (LITE, col("other", "equals", "o"), "42", False, PLAIN, "o = '42'"),
    (LITE, col("other", "equals", "o"), 42, False, PLAIN, "o = 42"),
    (LITE, col("other", "equals", "o"), "9007199254740993", True, PLAIN, "o IN (9007199254740993, '9007199254740993')"),
    (LITE, col("number", "none", "r"), 1.5, False, PLAIN, None),
])
def test_match_conditions(dialect, column, cell, key, style, expected):  # Review Focus 2, 3
    assert match_condition(dialect, column, cell, style, key=key) == expected


def test_a_cell_that_is_not_a_grid_cell_is_refused():
    with pytest.raises(ValidationFailed):
        match_condition(PG, col(), {"$type": "nope"}, PLAIN, key=True)


def test_cell_text_shows_a_key_as_a_message_would():
    assert [cell_text(None, "number"), cell_text(42, "number"), cell_text("9007199254740993", "number")] == [
        "NULL", "42", "9007199254740993"]
    assert [cell_text("it's", "text"), cell_text(BYTES, "binary"), cell_text(True, "bool")] == ["'it's'", "0x00ff", "true"]
    assert cell_text("x" * 100, "text") == "'" + "x" * 60 + "…'"


def test_quote_literal_options():  # D-05-2 (A8), D-05-13, ruling E16
    assert quote_literal(TS, "k42", national=False) == "'k42'"
    assert quote_literal(MY, "a\\b", backslash_escapes=False) == "'a\\b'"
    assert quote_literal(MY, "a\\b") == "'a\\\\b'"
    with pytest.raises(ValueError):
        quote_literal(MY, "a\x00", backslash_escapes=False)
    assert json_literal(MY, '{"p": 1}', backslash_escapes=False) == "CAST('{\"p\": 1}' AS JSON)"
    assert datetime_literal(TS, "2026-10-19 01:02:03") == "N'2026-10-19T01:02:03'"


def test_transaction_frames_defaults_and_scripts():  # D-05-4, spec §3.4
    assert transaction_frame(PG) == TransactionFrame("BEGIN", "COMMIT")
    assert transaction_frame(MY) == TransactionFrame("START TRANSACTION", "COMMIT")
    assert transaction_frame(TS) == TransactionFrame("SET NOCOUNT OFF;\nBEGIN TRANSACTION", "COMMIT TRANSACTION")
    assert transaction_frame(LITE) == TransactionFrame("BEGIN IMMEDIATE", "COMMIT")
    with pytest.raises(ValueError):
        transaction_frame(Dialect.SQLPP)
    assert insert_defaults(PG, "public.t") == "INSERT INTO public.t DEFAULT VALUES"
    assert insert_defaults(MY, "dbaguy.t") == "INSERT INTO dbaguy.t () VALUES ()"
    assert script_text(PG, ["BEGIN", "DELETE FROM t WHERE id = 1", "COMMIT"]) == (
        "BEGIN;\nDELETE FROM t WHERE id = 1;\nCOMMIT;")
    assert script_text(TS, ["SET NOCOUNT OFF;\nBEGIN TRANSACTION", "DELETE FROM t WHERE id = 1", "COMMIT TRANSACTION"]) == (
        "SET NOCOUNT OFF;\nBEGIN TRANSACTION\nGO\nDELETE FROM t WHERE id = 1\nGO\nCOMMIT TRANSACTION\nGO")
```

`backend/tests/test_edit_models.py`:

```python
"""The shape of grid-edit requests (spec ⑤ §3.3.1, D-05-11, ruling E8): refused as a whole (422) when malformed."""

import pytest
from pydantic import ValidationError

from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.edits import MAX_CHANGES, CellInput, EditChanges, edit_kind, kind_category
from dbaguy_backend.domain.metadata import PathSegment, path_database


def test_a_cell_input_has_text_only_as_a_value():
    assert CellInput(kind="value", text="").text == ""
    for bad in ({"kind": "value"}, {"kind": "null", "text": "x"}, {"kind": "default", "text": ""}):
        with pytest.raises(ValidationError):
            CellInput.model_validate(bad)


def test_an_update_sets_a_column_and_gives_each_its_original():
    ok = {"id": "u:1", "key": {"id": 1}, "set": {"a": {"kind": "null"}}, "original": {"a": "x"}}
    EditChanges.model_validate({"updates": [ok]})
    for bad in ({**ok, "set": {}}, {**ok, "original": {}}):
        with pytest.raises(ValidationError):
            EditChanges.model_validate({"updates": [bad]})


def test_changes_are_counted_unique_and_not_empty():
    deletes = [{"id": f"d:{i}", "key": {"id": i}} for i in range(MAX_CHANGES)]
    assert len(EditChanges.model_validate({"deletes": deletes}).deletes) == MAX_CHANGES
    for bad in ({"deletes": [*deletes, {"id": "d:x", "key": {"id": -1}}]},
                {"deletes": [deletes[0], deletes[0]]}, {}):
        with pytest.raises(ValidationError):
            EditChanges.model_validate(bad)


def test_a_lone_surrogate_is_refused_on_its_cell():  # ruling E19: pydantic refuses it in any str field
    bad = {"inserts": [{"id": "i:1", "values": {"label": {"kind": "value", "text": "half \ud83d"}}}]}
    with pytest.raises(ValidationError) as raised:
        EditChanges.model_validate(bad)
    assert raised.value.errors()[0]["loc"] == ("inserts", 0, "values", "label", "text")


def test_an_edit_kind_comes_from_the_grid_category():
    assert [edit_kind(c) for c in (ColumnCategory.NUMBER, ColumnCategory.JSON, ColumnCategory.MIXED)] == [
        "number", "json", "other"]
    assert kind_category("other") == ColumnCategory.OTHER and kind_category("datetime") == ColumnCategory.DATETIME


def test_path_database_is_the_first_database_segment():
    database = PathSegment(kind="database", name="dbaguy")
    schema = PathSegment(kind="schema", name="app")
    assert (path_database([database, schema]), path_database([schema]), path_database([])) == ("dbaguy", None, None)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_literals.py tests/test_edit_models.py`
Expected: FAIL (ImportError: `dbaguy_backend.domain.edits`).

- [ ] **Step 3: Write `backend/dbaguy_backend/domain/edits.py`:**

```python
"""Grid editing (spec ⑤ §3): how a table's rows are identified and written (EditTarget), the changes a tab stages
(EditChanges), what a preview shows (EditPreview) and the one-time plan a session applies (EditPlan)."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .column_category import ColumnCategory
from .metadata import PathSegment

EditKind = Literal["number", "text", "bool", "datetime", "binary", "json", "other"]
Compare = Literal["equals", "as_text", "none"]

MAX_CHANGES = 10_000  # D-05-11: changes (rows) in one commit
MAX_VALUE_CHARS = 1_048_576  # D-05-11: one value; beyond it the preview reports `value_too_large`
VALUE_ACCEPTED_CHARS = 4 * MAX_VALUE_CHARS  # beyond this the request itself is invalid (422), ruling E8
PLAN_TTL_S = 15 * 60  # D-05-7
MAX_PLANS = 32  # D-05-7
PLAN_EXPIRED = "plan_expired"
PLAN_MISMATCH = "plan_mismatch"
PLAN_EXPIRED_MESSAGE = "The preview has expired: preview the changes again"
PLAN_MISMATCH_MESSAGE = "This preview was made for another connection or database: preview the changes again"

_KIND_OF: dict[ColumnCategory, EditKind] = {
    ColumnCategory.NUMBER: "number", ColumnCategory.TEXT: "text", ColumnCategory.BOOL: "bool",
    ColumnCategory.DATETIME: "datetime", ColumnCategory.BINARY: "binary", ColumnCategory.JSON: "json",
}
_CATEGORY_OF: dict[EditKind, ColumnCategory] = {kind: category for category, kind in _KIND_OF.items()}


def edit_kind(category: ColumnCategory) -> EditKind:
    """How a column's new values are read, from the grid category each engine already gives its types (DRY)."""
    return _KIND_OF.get(category, "other")


def kind_category(kind: EditKind) -> ColumnCategory:
    return _CATEGORY_OF.get(kind, ColumnCategory.OTHER)


class EditColumn(BaseModel):
    name: str
    type_name: str  # as declared: numeric(10,2), varchar(20), datetime2(7)
    kind: EditKind
    nullable: bool
    has_default: bool  # a default, identity, auto_increment or generated value: an INSERT may leave it out
    writable: bool
    read_only_reason: str | None = None  # "generated column", "IDENTITY column", "rowversion column", …
    compare: Compare  # how an original value is checked; none: not checked (listed per statement)
    unicode: bool = True  # SQL Server: False for char / varchar / text (ASCII text then goes without N)


class EditTarget(BaseModel):
    path: list[PathSegment]
    editable: bool
    reason: str | None = None  # why not, for the toolbar
    key: list[str] = Field(default_factory=list)  # in index order
    key_source: Literal["primary_key", "unique"] | None = None
    columns: list[EditColumn] = Field(default_factory=list)
    transactional: bool = True  # MySQL: the table's engine supports transactions
    backslash_escapes: bool = True  # MySQL: the session's sql_mode has no NO_BACKSLASH_ESCAPES


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


ChangeId = Annotated[str, Field(min_length=1, max_length=100)]  # made by the client ("u:…", "i:…", "d:…"), echoed


class CellInput(_Input):
    kind: Literal["value", "null", "default"]
    text: str | None = Field(default=None, max_length=VALUE_ACCEPTED_CHARS)  # with kind "value" only

    @model_validator(mode="after")
    def _text_with_a_value_only(self) -> Self:
        if (self.kind == "value") != (self.text is not None):
            raise ValueError("a value needs its text; null and default take none")
        return self


class RowUpdate(_Input):
    id: ChangeId
    key: dict[str, JsonValue]  # the key cells as the grid loaded them (encode_value's output)
    set: dict[str, CellInput] = Field(min_length=1)
    original: dict[str, JsonValue]  # each set column's cell as loaded

    @model_validator(mode="after")
    def _an_original_per_column(self) -> Self:
        if self.set.keys() - self.original.keys():
            raise ValueError("every column set needs its original value")
        return self


class RowInsert(_Input):
    id: ChangeId
    values: dict[str, CellInput] = Field(default_factory=dict)  # a column left out takes its default


class RowDelete(_Input):
    id: ChangeId
    key: dict[str, JsonValue]


class EditChanges(_Input):
    updates: list[RowUpdate] = Field(default_factory=list)
    inserts: list[RowInsert] = Field(default_factory=list)
    deletes: list[RowDelete] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counted_and_unique(self) -> Self:
        ids = [change.id for change in (*self.deletes, *self.updates, *self.inserts)]
        if not ids:
            raise ValueError("nothing to commit")
        if len(ids) > MAX_CHANGES:
            raise ValueError(f"at most {MAX_CHANGES:,} changes are committed at once")
        if len(set(ids)) != len(ids):
            raise ValueError("change ids must be unique")
        return self


EditProblemCode = Literal[
    "invalid_number", "invalid_json", "invalid_hex", "invalid_bool", "invalid_text", "value_too_large",
    "read_only_column", "unknown_column", "not_nullable", "key_incomplete", "default_not_allowed",
]


class EditProblem(BaseModel):
    change_id: str
    column: str | None = None
    code: EditProblemCode
    message: str  # fixed text: never the value


class UncheckedColumn(BaseModel):
    column: str
    reason: str


class PlannedStatement(BaseModel):
    change_id: str
    kind: Literal["update", "insert", "delete"]
    sql: str  # exactly what the worker sends
    unchecked: list[UncheckedColumn] = Field(default_factory=list)  # set columns whose original is not checked
    row: str | None = None  # the key as the user's messages show it (`id = 42`); never logged


EditWarningCode = Literal["unchecked_columns", "non_transactional", "missing_required"]


class EditWarning(BaseModel):
    code: EditWarningCode
    message: str
    change_id: str | None = None
    columns: list[str] = Field(default_factory=list)


class EditCounts(BaseModel):
    updates: int = 0
    inserts: int = 0
    deletes: int = 0


class EditPreview(BaseModel):
    plan_id: str | None  # None: problems; nothing can be applied
    begin: str
    statements: list[PlannedStatement]  # empty when there are problems (ruling E8)
    commit: str
    problems: list[EditProblem]
    warnings: list[EditWarning]
    counts: EditCounts
    script: str  # begin, statements and commit as one text (Copy, History); "" with problems


class EditPlan(BaseModel):
    """What a session applies: the previewed texts, nothing else (R-05-7). Taken once, by a session of the same profile
    and, when the path names one, the same database (CR-2, ruling E3)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    profile_id: UUID
    database: str | None  # the path's database segment; None: any session of the profile
    table: str  # as the statements write it (logs)
    begin: str
    statements: tuple[PlannedStatement, ...]
    commit: str
    transactional: bool
    script: str


class PlanUnavailable(Exception):
    """A plan the session cannot take: expired (or never made, or taken already) or made for another connection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
```

And in `domain/metadata.py` (add `from collections.abc import Sequence`):

```python
def path_database(path: Sequence[PathSegment]) -> str | None:
    """The database a tree path starts with (engines with one connection per database), or None."""
    head = path[0] if path else None
    return head.name if head is not None and head.kind == NodeKind.DATABASE else None
```

- [ ] **Step 4: Extend `sql/dialects.py`.** Replace `quote_literal`, `datetime_literal`, `json_literal` with the option-taking versions and add the frame, the all-default row and the script text (add `from collections.abc import Sequence`):

```python
def quote_literal(
    dialect: Dialect, value: str, *, national: bool = True, backslash_escapes: bool | None = None
) -> str:
    """A string literal that the dialect reads back as exactly `value`: quotes are doubled, and where backslash
    escapes exist (MySQL's default sql_mode), backslashes are doubled too. T-SQL literals carry the N prefix. A NUL
    character never stays raw (`_NUL_IN_LITERAL`): an escape, or a concatenation that keeps the text one expression.
    SQL++: the value as a JSON string (`json.dumps`), which is also how the server reads a "…" literal (D-04-15).

    `national` False leaves T-SQL's N out (ASCII text for a non-Unicode column keeps its index seek, ⑤ D-05-2, A8).
    `backslash_escapes` overrides the dialect's rule (False: a MySQL session with NO_BACKSLASH_ESCAPES, ⑤ D-05-13),
    where MySQL's `\\0` cannot write a NUL: ValueError then."""
    if _JSON_STRINGS[dialect]:
        return json.dumps(value, ensure_ascii=False)
    escapes = RULES[dialect].backslash_escapes if backslash_escapes is None else backslash_escapes
    if "\x00" in value and dialect is Dialect.MYSQL and not escapes:
        raise ValueError("A NUL character cannot be written in a MySQL literal without backslash escapes")
    if escapes:
        value = value.replace("\\", "\\\\")
    literal = (_STRING_PREFIXES[dialect] if national else "") + "'" + value.replace("'", "''") + "'"
    nul = _NUL_IN_LITERAL[dialect]
    return literal if nul is None else literal.replace("\x00", nul)


def datetime_literal(dialect: Dialect, text: str, *, backslash_escapes: bool | None = None) -> str:
    """A date/time value given as the grid's text. SQL Server reads `2026-10-18 12:34:56` by the login's DATEFORMAT
    (a dmy language such as British fails with error 242) but `2026-10-18T12:34:56` the same everywhere."""
    if _ISO_DATETIME[dialect] and (found := _DATE_SPACE_TIME.match(text)):
        text = f"{found.group(1)}T{found.group(2)}{text[found.end():]}"
    return quote_literal(dialect, text, backslash_escapes=backslash_escapes)


def json_literal(dialect: Dialect, text: str, *, national: bool = True, backslash_escapes: bool | None = None) -> str:
    """A JSON document given as its text, written so that the dialect compares it as JSON (SQL++: the text itself,
    JSON being SQL++ syntax)."""
    template = _JSON_LITERALS[dialect]
    if template is None:
        return text
    return template.format(quote_literal(dialect, text, national=national, backslash_escapes=backslash_escapes))


@dataclass(frozen=True)
class TransactionFrame:
    """The texts around a grid commit (⑤ D-05-4): sent through run() like its statements, so they are in the preview."""

    begin: str
    commit: str


_TRANSACTION_FRAMES: dict[Dialect, TransactionFrame | None] = {
    Dialect.POSTGRES: TransactionFrame("BEGIN", "COMMIT"),
    Dialect.MYSQL: TransactionFrame("START TRANSACTION", "COMMIT"),
    # NOCOUNT OFF: on a server whose `user options` turn NOCOUNT on, no statement would report its rows (A2)
    Dialect.TSQL: TransactionFrame("SET NOCOUNT OFF;\nBEGIN TRANSACTION", "COMMIT TRANSACTION"),
    # IMMEDIATE: the write lock first, so a database another connection is writing fails before the first statement
    Dialect.SQLITE: TransactionFrame("BEGIN IMMEDIATE", "COMMIT"),
    Dialect.SQLPP: None,  # no grid editing (R-05-12)
}
_ALL_DEFAULTS: dict[Dialect, str | None] = {  # an INSERT of a row whose every column takes its default
    Dialect.POSTGRES: "DEFAULT VALUES", Dialect.SQLITE: "DEFAULT VALUES", Dialect.TSQL: "DEFAULT VALUES",
    Dialect.MYSQL: "() VALUES ()", Dialect.SQLPP: None,
}


def transaction_frame(dialect: Dialect) -> TransactionFrame:
    frame = _TRANSACTION_FRAMES[dialect]
    if frame is None:
        raise ValueError(f"{dialect} has no grid editing")
    return frame


def insert_defaults(dialect: Dialect, table_sql: str) -> str:
    rest = _ALL_DEFAULTS[dialect]
    if rest is None:
        raise ValueError(f"{dialect} has no grid editing")
    return f"INSERT INTO {table_sql} {rest}"


def script_text(dialect: Dialect, statements: Sequence[str]) -> str:
    """Statements as one text to copy or keep in History: `;` after each, or a `GO` line after each T-SQL batch."""
    if RULES[dialect].client_command is ClientCommand.GO:
        return "".join(f"{statement}\nGO\n" for statement in statements).rstrip("\n")
    return "\n".join(f"{statement};" for statement in statements)
```

- [ ] **Step 5: Extend `sql/literals.py`.** `LiteralStyle` and `DEFAULT_STYLE` go right after `NORMALISERS` (before `CellLiterals`, whose default argument they are); `CellLiterals` and `value_condition` are replaced; the rest is appended (imports added: `from ..adapters.positions import offset_to_line_col`, `from ..adapters.values import MAX_SAFE_INT`, `from ..domain.edits import MAX_VALUE_CHARS, CellInput, EditColumn, EditKind, EditProblemCode, kind_category`, `from pydantic import JsonValue`, `from .jsontext import LONE_SURROGATE, json_problem`). `CellLiterals` takes a style and `value_condition` delegates to the equality shared with the DML:

```python
@dataclass(frozen=True, slots=True)
class LiteralStyle:
    """A column's literal options beyond its dialect (⑤ D-05-2, D-05-13). `national` False: a non-Unicode SQL Server
    column, whose ASCII text goes without the N prefix so a varchar key keeps its index seek (A8; non-ASCII text keeps
    N'…', correctness first, ruling E17). `backslash_escapes` False: MySQL with NO_BACKSLASH_ESCAPES; None: the
    dialect's own rule. The default style writes what copy and Filter by this value always wrote."""

    national: bool = True
    backslash_escapes: bool | None = None

    def quote(self, dialect: Dialect, text: str) -> str:
        return quote_literal(dialect, text, national=self.national or not text.isascii(),
                             backslash_escapes=self.backslash_escapes)

    def json(self, dialect: Dialect, text: str) -> str:
        return json_literal(dialect, text, national=self.national or not text.isascii(),
                            backslash_escapes=self.backslash_escapes)

    def datetime(self, dialect: Dialect, text: str) -> str:
        return datetime_literal(dialect, text, backslash_escapes=self.backslash_escapes)


DEFAULT_STYLE = LiteralStyle()


class CellLiterals:
    """Renders cells as SQL literals; a cell cut by the grid cannot be restored, so it becomes NULL and is counted."""

    def __init__(self, dialect: Dialect, style: LiteralStyle = DEFAULT_STYLE) -> None:
        self._dialect = dialect
        self._style = style
        self.truncated = 0

    def __call__(self, cell: Cell, category: ColumnCategory) -> str:
        match cell:
            case None:
                return "NULL"
            case MissingCell():
                return "MISSING"
            case Json():
                return self._style.json(self._dialect, cell.text)
            case bool():
                return bool_literal(self._dialect, cell)
            case Truncated() | Binary(truncated=True):
                self.truncated += 1
                return "NULL"
            case Binary():
                return bytes_literal(self._dialect, cell.hex)
            case int() | float():
                return str(cell)
            case _ if category == ColumnCategory.DATETIME:
                return self._style.datetime(self._dialect, cell)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else self._style.quote(self._dialect, cell)


def _cell(value: Any) -> Cell:
    try:
        return normalise_encoded(value)
    except MalformedCell as malformed:
        raise ValidationFailed(f"Not a grid cell: {malformed}") from None


def _partial(cell: Cell) -> bool:
    """Only a preview of the value is known (text cut at TEXT_LIMIT, bytes beyond BYTES_PREVIEW)."""
    return isinstance(cell, Truncated) or (isinstance(cell, Binary) and cell.truncated)


def _equality(dialect: Dialect, name: str, cell: Cell, category: ColumnCategory, style: LiteralStyle) -> str:
    if isinstance(cell, Json):
        return f"{name} = {style.json(dialect, cell.text)}"
    if category == ColumnCategory.JSON and isinstance(cell, str):
        return f"{name} = {style.json(dialect, cell)}"
    if category == ColumnCategory.DATETIME and isinstance(cell, str) and (text := zero_date_as_text(dialect, name, cell)):
        return f"{text} = {style.quote(dialect, cell)}"
    return f"{name} = {CellLiterals(dialect, style)(cell, category)}"


def value_condition(
    dialect: Dialect, column: str, value: Any, category: ColumnCategory, style: LiteralStyle = DEFAULT_STYLE
) -> str | None:
    """(docstring unchanged)"""
    name = quote_ident(dialect, column)
    if value is None:
        return f"{name} IS NULL"
    cell = _cell(value)
    if cell is MISSING_CELL:
        return f"{name} IS MISSING"
    if _partial(cell):
        return None
    return _equality(dialect, name, cell, category, style)


# --- grid edits (spec ⑤ §3.3.3) -----------------------------------------------------------------------------------


class ValueProblem(Exception):
    """A typed value that cannot become a literal: its problem code and a fixed message that never quotes it."""

    def __init__(self, code: EditProblemCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_TRUE = frozenset({"true", "t", "1", "yes"})
_FALSE = frozenset({"false", "f", "0", "no"})
_PG_SPECIAL_NUMBERS = {"nan": "NaN", "infinity": "Infinity", "-infinity": "-Infinity"}
_INTEGER = re.compile(r"-?[0-9]+")
_SHOWN = 60  # characters of a key value a message shows


def _sendable(dialect: Dialect, text: str, style: LiteralStyle) -> None:
    """Text that can go into a quoted literal (`invalid_text`, spec §3.3.3, ruling E16)."""
    if not text.isascii() and LONE_SURROGATE.search(text):
        raise ValueProblem("invalid_text", "The value holds a character that is not valid text (half of an emoji?)")
    if "\x00" in text and (dialect is Dialect.POSTGRES or (dialect is Dialect.MYSQL and style.backslash_escapes is False)):
        raise ValueProblem("invalid_text", "The value holds a NUL character (U+0000), which cannot be written here")


def _number(dialect: Dialect, text: str) -> str:
    value = text.strip()
    if NUMERIC_LITERAL.fullmatch(value):
        return value
    if dialect is Dialect.POSTGRES and (special := _PG_SPECIAL_NUMBERS.get(value.lower())):
        return quote_literal(dialect, special)
    raise ValueProblem("invalid_number", "Not a number")


def _bool(dialect: Dialect, text: str) -> str:
    word = text.strip().lower()
    if word in _TRUE or word in _FALSE:
        return bool_literal(dialect, word in _TRUE)
    raise ValueProblem("invalid_bool", "Not true or false (true, false, t, f, 1, 0, yes, no)")


def _hex(dialect: Dialect, text: str) -> str:
    digits = text.strip()
    if digits[:2].lower() == "0x":
        digits = digits[2:]
    if not HEX_BYTES.fullmatch(digits):
        raise ValueProblem("invalid_hex", "Not hex digits (two per byte, an optional 0x in front)")
    return bytes_literal(dialect, digits.lower())


def _json(text: str) -> None:
    problem = json_problem(text)
    if problem is not None:
        line, column = offset_to_line_col(text, problem.offset + 1)
        raise ValueProblem("invalid_json", f"Not valid JSON at line {line}, column {column}: {problem.reason}")


def input_literal(dialect: Dialect, column: EditColumn, cell: CellInput, style: LiteralStyle) -> str:
    """The literal a typed value is sent as (spec §3.3.3, R-05-8): NULL, or the text read by the column's kind.
    Raises ValueProblem for a value the kind refuses. DEFAULT is never a literal: the plan leaves the column out."""
    if cell.kind == "null":
        return "NULL"
    if cell.kind == "default" or cell.text is None:
        raise ValueError("DEFAULT has no literal")
    text = cell.text
    if len(text) > MAX_VALUE_CHARS:
        raise ValueProblem("value_too_large", f"A value is limited to {MAX_VALUE_CHARS:,} characters")
    match column.kind:
        case "number":
            return _number(dialect, text)
        case "bool":
            return _bool(dialect, text)
        case "binary":
            return _hex(dialect, text)
        case "json":
            _json(text)
            _sendable(dialect, text, style)
            return style.quote(dialect, text)
        case "datetime":
            _sendable(dialect, text, style)
            return style.datetime(dialect, text)
        case "other" if dialect is Dialect.SQLITE and NUMERIC_LITERAL.fullmatch(text.strip()):
            return text.strip()  # no affinity: a number is stored as a number (spec §3.3.3)
        case _:
            _sendable(dialect, text, style)
            return style.quote(dialect, text)


def _server_text(cell: Cell) -> str:
    """A cell as the server's text output wrote it (PostgreSQL's `as_text` check, A4)."""
    match cell:
        case bool():
            return "true" if cell else "false"
        case Binary():
            return "\\x" + cell.hex
        case Json():
            return cell.text
        case _:
            return str(cell)


def match_condition(
    dialect: Dialect, column: EditColumn, value: JsonValue, style: LiteralStyle, *, key: bool
) -> str | None:
    """The WHERE term that pins `column` to `value`, an encoded grid cell (spec §3.3.3, D-05-2): `c = <literal>` for a
    key (always `=`, so an index can be used) and for an `equals` original; `c::text = '<text>'` for an `as_text` one
    (PostgreSQL: exactly the text the session loaded, A4); `c IS NULL` for NULL whatever the compare (ruling E5). None
    when no exact term exists: the cell is only a preview, or the column's compare is `none`."""
    name = quote_ident(dialect, column.name)
    if value is None:
        return f"{name} IS NULL"
    cell = _cell(value)
    if cell is MISSING_CELL or _partial(cell):
        return None
    if isinstance(cell, str) and not cell.isascii() and LONE_SURROGATE.search(cell):
        return None  # no driver can send it (ruling E19): a key is then incomplete, an original unchecked
    compare = "equals" if key else column.compare
    if compare == "none":
        return None
    if compare == "as_text":
        return f"{name}::text = {quote_literal(dialect, _server_text(cell))}"
    if (dialect is Dialect.SQLITE and column.kind == "other" and isinstance(cell, str) and _INTEGER.fullmatch(cell)
            and abs(int(cell)) > MAX_SAFE_INT):
        # ruling E7: no affinity, and encode_value sent this integer as text; it may be stored as either
        return f"{name} IN ({cell}, {quote_literal(dialect, cell)})"
    return _equality(dialect, name, cell, kind_category(column.kind), style)


def _cut(text: str) -> str:
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "…"


def cell_text(value: JsonValue, kind: EditKind) -> str:
    """A key cell as the user's messages show it (`42`, `'abc'`, `NULL`, `0x00ff`): never logged."""
    if value is None:
        return "NULL"
    try:
        cell = normalise_encoded(value)
    except MalformedCell:
        return "?"
    match cell:
        case bool():
            return "true" if cell else "false"
        case int() | float():
            return str(cell)
        case Binary():
            return "0x" + _cut(cell.hex) + ("…" if cell.truncated else "")
        case Truncated():
            return "'" + _cut(cell.preview) + "…'"
        case Json():
            return _cut(cell.text)
        case str() if kind == "number" and NUMERIC_LITERAL.fullmatch(cell):
            return cell
        case str():
            return "'" + _cut(cell) + "'"
        case _:
            return "?"
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_literals.py tests/test_edit_models.py tests/sql/test_formatting.py tests/test_couchbase_literals.py`
Expected: PASS (the formatting and Couchbase literal tests unchanged: the default style writes what it always wrote).

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/domain/edits.py backend/dbaguy_backend/domain/metadata.py \
  backend/dbaguy_backend/sql/dialects.py backend/dbaguy_backend/sql/literals.py \
  backend/tests/sql/test_literals.py backend/tests/test_edit_models.py
git commit -m "feat(sql): grid-edit models and literals — typed values, key and original checks, frames per dialect"
```

---
### Task 4: The plan builder `sql/dml.py`

**Files:**
- Create: `backend/dbaguy_backend/sql/dml.py`
- Create: `backend/tests/sql/test_dml.py`

**Interfaces:**
- Consumes: Task 3 (`domain/edits` models; `literals.LiteralStyle`, `ValueProblem`, `input_literal`, `match_condition`, `cell_text`; `dialects.transaction_frame`, `insert_defaults`, `script_text`); `sql/select_builder.relation_from_path` (the same table name the browse SELECT uses); `dialects.qualified`, `quote_ident`.
- Produces: `@dataclass(frozen=True) class DmlPlan` with `table: str`, `begin: str`, `statements: list[PlannedStatement]`, `commit: str`, `problems: list[EditProblem]`, `warnings: list[EditWarning]`, `counts: EditCounts`, `script: str`; `build_plan(dialect: Dialect, target: EditTarget, changes: EditChanges) -> DmlPlan` (ValueError for a target that is not editable). Task 7 turns it into an `EditPreview` and an `EditPlan`.

Rules (spec §3.3, D-05-3; rulings E5, E8, E18, E22):
- Statements: every DELETE, then every UPDATE, then every INSERT, each group in the request's order; one statement per change, on one line:
  `DELETE FROM <table> WHERE <key terms>`;
  `UPDATE <table> SET <c> = <new>, … WHERE <key terms> AND <original terms>` (only the columns set; a set column that is part of the key gets no original term: its key term pins it, CR-4);
  `INSERT INTO <table> (<c>, …) VALUES (<new>, …)` with the DEFAULT columns left out; every column DEFAULT → `insert_defaults` (`DEFAULT VALUES`, MySQL `() VALUES ()`).
  `<table>` is `qualified(dialect, schema, name)` of `relation_from_path(target.path)`. Each column's literals use `LiteralStyle(national=column.unicode, backslash_escapes=None if target.backslash_escapes else False)`.
- Key terms: `match_condition(…, key=True)` for every key column, in key order; a key cell missing, NULL or only a preview → `key_incomplete` on that column. `PlannedStatement.row` is `", ".join(f"{name} = {cell_text(cell, kind)}")` over the key.
- Original terms: `match_condition(…, key=False)`; None → the column goes into `unchecked` with the reason `<type_name> values cannot be compared exactly` (compare `none`) or `only a preview of the value was loaded` (E22).
- Problems (`EditProblem{change_id, column, code, message}`), every one collected: `unknown_column` "No column <c> in this table (it may have been dropped or renamed)"; `read_only_column` "<c> cannot be written: <reason>"; `default_not_allowed` "DEFAULT applies only to a new row" (an UPDATE); `not_nullable` "<c> cannot be NULL"; `key_incomplete` "This row cannot be identified: its key column <c> was not loaded completely"; and every `ValueProblem`. With any problem: `statements=[]`, `script=""` (E8); `begin`, `commit`, `counts`, `warnings` still filled.
- Warnings, in this order: `non_transactional` (the target cannot roll back) "This table's storage engine cannot roll back: if a statement fails, the statements before it stay applied"; `unchecked_columns` (once, every column some statement does not check, sorted) "Not checked for changes made since loading: <c>, …"; `missing_required` per INSERT (writable, NOT NULL, no default, given no value) "New row: no value for <c>, … (NOT NULL, no default); the server will probably refuse it".
- `script` = `script_text(dialect, [begin, *statement texts, commit])`.

- [ ] **Step 1: Write the failing tests** — `backend/tests/sql/test_dml.py`:

```python
"""The SQL a grid commit sends (spec ⑤ §3.3, D-05-3): order, shape, problems, warnings, the script, per dialect."""

import time

import pytest

from dbaguy_backend.domain.edits import EditChanges, EditColumn, EditTarget
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.dml import build_plan
from tests.support import scaled

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE


def path(*segments: tuple[str, str]) -> list[PathSegment]:
    return [PathSegment(kind=kind, name=name) for kind, name in segments]


PG_PATH = path(("database", "dbaguy"), ("schema", "public"), ("folder", "tables"), ("table", "orders"))
MY_PATH = path(("schema", "dbaguy"), ("folder", "tables"), ("table", "orders"))
TS_PATH = path(("database", "dbaguy"), ("schema", "dbo"), ("folder", "tables"), ("table", "orders"))
LITE_PATH = path(("schema", "main"), ("folder", "tables"), ("table", "orders"))


def column(name, kind="text", compare="as_text", **fields) -> EditColumn:
    defaults = {"type_name": kind, "nullable": True, "has_default": False, "writable": True}
    return EditColumn(name=name, kind=kind, compare=compare, **{**defaults, **fields})


def pg_target(**fields) -> EditTarget:
    columns = [
        column("id", "number", type_name="bigint", nullable=False, has_default=True),
        column("label", nullable=False),
        column("price", "number", "none", type_name="double precision"),
        column("flag", "bool"),
        column("made", "datetime", type_name="timestamp with time zone", has_default=True),
        column("total", "number", type_name="numeric", writable=False, read_only_reason="generated column",
               has_default=True),
        column("order", "number", type_name="integer"),
    ]
    return EditTarget(**{"path": PG_PATH, "editable": True, "key": ["id"], "key_source": "primary_key",
                         "columns": columns, **fields})


def changes(**lists) -> EditChanges:
    return EditChanges.model_validate(lists)


def value(text):
    return {"kind": "value", "text": text}


NULL, DEFAULT = {"kind": "null"}, {"kind": "default"}


def test_deletes_then_updates_then_inserts_in_staging_order():  # D-05-3
    plan = build_plan(PG, pg_target(), changes(
        inserts=[{"id": "i:1", "values": {"label": value("new")}}],
        updates=[{"id": "u:2", "key": {"id": 2}, "set": {"label": value("b2")}, "original": {"label": "b"}},
                 {"id": "u:1", "key": {"id": 1}, "set": {"label": value("a2")}, "original": {"label": "a"}}],
        deletes=[{"id": "d:3", "key": {"id": 3}}],
    ))
    assert [(s.kind, s.change_id) for s in plan.statements] == [
        ("delete", "d:3"), ("update", "u:2"), ("update", "u:1"), ("insert", "i:1")]
    assert [s.sql for s in plan.statements] == [
        "DELETE FROM public.orders WHERE id = 3",
        "UPDATE public.orders SET label = 'b2' WHERE id = 2 AND label::text = 'b'",
        "UPDATE public.orders SET label = 'a2' WHERE id = 1 AND label::text = 'a'",
        "INSERT INTO public.orders (label) VALUES ('new')",
    ]
    assert (plan.begin, plan.commit, plan.problems) == ("BEGIN", "COMMIT", [])
    assert plan.counts.model_dump() == {"updates": 2, "inserts": 1, "deletes": 1}
    assert plan.script == "BEGIN;\n" + "".join(f"{s.sql};\n" for s in plan.statements) + "COMMIT;"
    assert plan.statements[0].row == "id = 3" and plan.statements[3].row is None


def test_an_update_checks_the_originals_it_can_and_lists_the_rest():  # R-05-5, ruling E5, E22
    plan = build_plan(PG, pg_target(), changes(updates=[{
        "id": "u:1", "key": {"id": "9007199254740993"},
        "set": {"label": value("x"), "price": value("2.5"), "flag": NULL, "order": value("7")},
        "original": {"label": {"$type": "text", "preview": "ab", "size": 70_000, "truncated": True}, "price": 1.5,
                     "flag": True, "order": None},
    }]))
    [statement] = plan.statements
    assert statement.sql == (
        'UPDATE public.orders SET label = \'x\', price = 2.5, flag = NULL, "order" = 7 '
        "WHERE id = 9007199254740993 AND flag::text = 'true' AND \"order\" IS NULL"
    )
    assert [(u.column, u.reason) for u in statement.unchecked] == [
        ("label", "only a preview of the value was loaded"), ("price", "double precision values cannot be compared exactly")]
    assert [(w.code, w.columns) for w in plan.warnings] == [("unchecked_columns", ["label", "price"])]


def test_a_key_column_set_is_pinned_by_its_key_only():  # CR-4
    plan = build_plan(PG, pg_target(), changes(updates=[
        {"id": "u:1", "key": {"id": 2}, "set": {"id": value("20")}, "original": {"id": 2}}]))
    assert plan.statements[0].sql == "UPDATE public.orders SET id = 20 WHERE id = 2"


def test_an_insert_leaves_defaults_out_and_an_all_default_row_is_default_values():
    plan = build_plan(PG, pg_target(), changes(inserts=[
        {"id": "i:1", "values": {"id": DEFAULT, "label": value("a"), "made": DEFAULT, "total": DEFAULT}},
        {"id": "i:2", "values": {"id": DEFAULT}},
    ]))
    assert [s.sql for s in plan.statements] == [
        "INSERT INTO public.orders (label) VALUES ('a')", "INSERT INTO public.orders DEFAULT VALUES"]
    assert [(w.code, w.change_id, w.columns) for w in plan.warnings] == [("missing_required", "i:2", ["label"])]
    my = build_plan(MY, pg_target(path=MY_PATH), changes(inserts=[{"id": "i:1", "values": {}}]))
    assert my.statements[0].sql == "INSERT INTO dbaguy.orders () VALUES ()"


def test_problems_are_per_cell_and_leave_no_statement():  # spec §3.3.3, ruling E8
    plan = build_plan(PG, pg_target(), changes(
        updates=[{"id": "u:1", "key": {"id": 1},
                  "set": {"label": NULL, "total": value("1"), "gone": value("x"), "price": DEFAULT, "flag": value("?")},
                  "original": {"label": "a", "total": "1", "gone": "x", "price": 1.0, "flag": True}}],
        deletes=[{"id": "d:1", "key": {"id": {"$type": "text", "preview": "1", "size": 70_000, "truncated": True}}},
                 {"id": "d:2", "key": {}}],
    ))
    assert sorted((p.change_id, p.column, p.code) for p in plan.problems) == [
        ("d:1", "id", "key_incomplete"), ("d:2", "id", "key_incomplete"),
        ("u:1", "flag", "invalid_bool"), ("u:1", "gone", "unknown_column"), ("u:1", "label", "not_nullable"),
        ("u:1", "price", "default_not_allowed"), ("u:1", "total", "read_only_column"),
    ]
    assert plan.statements == [] and plan.script == "" and (plan.begin, plan.commit) == ("BEGIN", "COMMIT")
    assert plan.counts.model_dump() == {"updates": 1, "inserts": 0, "deletes": 2}


def test_a_non_transactional_target_is_warned_first():  # D-05-6
    plan = build_plan(MY, pg_target(path=MY_PATH, transactional=False), changes(deletes=[{"id": "d:1", "key": {"id": 1}}]))
    assert [w.code for w in plan.warnings] == ["non_transactional"]
    assert (plan.begin, plan.commit, plan.statements[0].sql) == (
        "START TRANSACTION", "COMMIT", "DELETE FROM dbaguy.orders WHERE id = 1")


def test_each_dialect_writes_its_own_literals_and_frame():  # D-05-2, D-05-4, D-05-13
    ts_columns = [column("code", compare="equals", unicode=False, nullable=False), column("note", compare="equals"),
                  column("made", "datetime", compare="equals")]
    ts = EditTarget(path=TS_PATH, editable=True, key=["code"], key_source="primary_key", columns=ts_columns)
    plan = build_plan(TS, ts, changes(updates=[{
        "id": "u:1", "key": {"code": "k42"}, "set": {"note": value("café"), "made": value("2026-10-19 12:00:00")},
        "original": {"note": "50%", "made": "2026-10-18 01:02:03"},
    }]))
    assert plan.statements[0].sql == (
        "UPDATE dbo.orders SET note = N'café', made = N'2026-10-19T12:00:00' "
        "WHERE code = 'k42' AND note = N'50%' AND made = N'2026-10-18T01:02:03'"
    )
    assert plan.script.startswith("SET NOCOUNT OFF;\nBEGIN TRANSACTION\nGO\nUPDATE") and plan.script.endswith(
        "COMMIT TRANSACTION\nGO")
    my_columns = [column("id", "number", compare="equals", nullable=False), column("path", compare="equals")]
    for escapes, literal in ((True, "'C:\\\\x'"), (False, "'C:\\x'")):
        my = EditTarget(path=MY_PATH, editable=True, key=["id"], key_source="primary_key", columns=my_columns,
                        backslash_escapes=escapes)
        sql = build_plan(MY, my, changes(updates=[
            {"id": "u:1", "key": {"id": 1}, "set": {"path": value("C:\\x")}, "original": {"path": "C:\\x"}}])).statements[0].sql
        assert sql == f"UPDATE dbaguy.orders SET path = {literal} WHERE id = 1 AND path = {literal}"
    lite_columns = [column("id", "number", compare="equals", nullable=False), column("any", "other", compare="equals")]
    lite = EditTarget(path=LITE_PATH, editable=True, key=["id"], key_source="primary_key", columns=lite_columns)
    plan = build_plan(LITE, lite, changes(inserts=[{"id": "i:1", "values": {"id": value("5"), "any": value("42")}}]))
    assert (plan.begin, plan.statements[0].sql) == ("BEGIN IMMEDIATE", "INSERT INTO main.orders (id, any) VALUES (5, 42)")


def test_names_are_quoted_where_the_dialect_needs_it():  # Review Focus 2
    columns = [column("Key Col", "number", compare="equals", nullable=False), column("we]ird", compare="equals"),
               column("order", "number", compare="equals")]
    for dialect, schema, table, expected in (
        (PG, "Sales", "Order Items",
         'UPDATE "Sales"."Order Items" SET "we]ird" = \'v\', "order" = 1 WHERE "Key Col" = 1 AND "we]ird" = \'w\' '
         'AND "order" = 2'),
        (TS, "dbo", "Order Items",
         "UPDATE dbo.[Order Items] SET [we]]ird] = N'v', [order] = 1 WHERE [Key Col] = 1 AND [we]]ird] = N'w' "
         "AND [order] = 2"),
        (MY, "dbaguy", "it`s",
         "UPDATE dbaguy.`it``s` SET `we]ird` = 'v', `order` = 1 WHERE `Key Col` = 1 AND `we]ird` = 'w' AND `order` = 2"),
    ):
        target_path = path(("schema", schema), ("folder", "tables"), ("table", table))
        target = EditTarget(path=target_path, editable=True, key=["Key Col"], key_source="primary_key", columns=columns)
        plan = build_plan(dialect, target, changes(updates=[{
            "id": "u:1", "key": {"Key Col": 1}, "set": {"we]ird": value("v"), "order": value("1")},
            "original": {"we]ird": "w", "order": 2}}]))
        assert plan.statements[0].sql == expected


def test_a_target_that_is_not_editable_is_refused():
    with pytest.raises(ValueError):
        build_plan(PG, EditTarget(path=PG_PATH, editable=False, reason="Views are read-only here"),
                   changes(deletes=[{"id": "d:1", "key": {"id": 1}}]))


def test_a_thousand_changes_build_well_within_300_ms():  # spec §7
    updates = [{"id": f"u:{i}", "key": {"id": i}, "set": {"label": value(f"v{i}"), "flag": value("t")},
                "original": {"label": f"o{i}", "flag": False}} for i in range(1000)]
    request = changes(updates=updates)
    started = time.perf_counter()
    plan = build_plan(PG, pg_target(), request)
    assert time.perf_counter() - started < scaled(0.3) and len(plan.statements) == 1000
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_dml.py`
Expected: FAIL (ModuleNotFoundError: `dbaguy_backend.sql.dml`).

- [ ] **Step 3: Write `backend/dbaguy_backend/sql/dml.py`:**

```python
"""The SQL a grid commit sends (spec ⑤ §3.3, D-05-3): one statement per staged row — every DELETE, then every UPDATE,
then every INSERT, each group in staging order — between the dialect's transaction frame. Values, keys and original
values go through `literals`, names through `dialects`. Pure: the preview builds it on the request thread, and the
worker later sends exactly these texts (R-05-7)."""

from dataclasses import dataclass

from ..domain.edits import (
    CellInput, EditChanges, EditColumn, EditCounts, EditProblem, EditProblemCode, EditTarget, EditWarning,
    PlannedStatement, RowDelete, RowInsert, RowUpdate, UncheckedColumn,
)
from .dialects import Dialect, insert_defaults, qualified, quote_ident, script_text, transaction_frame
from .literals import LiteralStyle, ValueProblem, cell_text, input_literal, match_condition
from .select_builder import relation_from_path

_UNKNOWN_COLUMN = "No column {name} in this table (it may have been dropped or renamed)"
_READ_ONLY_COLUMN = "{name} cannot be written: {reason}"
_NOT_NULLABLE = "{name} cannot be NULL"
_DEFAULT_ONLY_NEW = "DEFAULT applies only to a new row"
_KEY_INCOMPLETE = "This row cannot be identified: its key column {name} was not loaded completely"
_PREVIEW_ONLY = "only a preview of the value was loaded"
_NOT_EXACT = "{type} values cannot be compared exactly"
_NON_TRANSACTIONAL = (
    "This table's storage engine cannot roll back: if a statement fails, the statements before it stay applied"
)
_UNCHECKED = "Not checked for changes made since loading: {columns}"
_MISSING_REQUIRED = "New row: no value for {columns} (NOT NULL, no default); the server will probably refuse it"


@dataclass(frozen=True)
class DmlPlan:
    table: str
    begin: str
    statements: list[PlannedStatement]
    commit: str
    problems: list[EditProblem]
    warnings: list[EditWarning]
    counts: EditCounts
    script: str


class _Builder:
    def __init__(self, dialect: Dialect, target: EditTarget) -> None:
        if not target.editable:
            raise ValueError("this table cannot be edited")
        self.dialect = dialect
        self.target = target
        schema, name = relation_from_path(target.path)
        self.table = qualified(dialect, schema, name)
        self.columns = {column.name: column for column in target.columns}
        escapes = None if target.backslash_escapes else False
        self.styles = {c.name: LiteralStyle(national=c.unicode, backslash_escapes=escapes) for c in target.columns}
        self.problems: list[EditProblem] = []

    def _problem(self, change_id: str, column: str | None, code: EditProblemCode, message: str) -> None:
        self.problems.append(EditProblem(change_id=change_id, column=column, code=code, message=message))

    def _column(self, change_id: str, name: str) -> EditColumn | None:
        column = self.columns.get(name)
        if column is None:
            self._problem(change_id, name, "unknown_column", _UNKNOWN_COLUMN.format(name=name))
        return column

    def _key_terms(self, change_id: str, key: dict) -> list[str] | None:
        terms = []
        for name in self.target.key:
            cell = key.get(name)
            term = None if cell is None else match_condition(
                self.dialect, self.columns[name], cell, self.styles[name], key=True)
            if term is None:
                self._problem(change_id, name, "key_incomplete", _KEY_INCOMPLETE.format(name=name))
                return None
            terms.append(term)
        return terms

    def _row(self, key: dict) -> str:
        return ", ".join(f"{name} = {cell_text(key.get(name), self.columns[name].kind)}" for name in self.target.key)

    def _literal(self, change_id: str, column: EditColumn, cell: CellInput) -> str | None:
        """A new value's literal, or None after recording why it has none."""
        if not column.writable:
            self._problem(change_id, column.name, "read_only_column",
                          _READ_ONLY_COLUMN.format(name=column.name, reason=column.read_only_reason))
            return None
        if cell.kind == "null" and not column.nullable:
            self._problem(change_id, column.name, "not_nullable", _NOT_NULLABLE.format(name=column.name))
            return None
        try:
            return input_literal(self.dialect, column, cell, self.styles[column.name])
        except ValueProblem as problem:
            self._problem(change_id, column.name, problem.code, problem.message)
            return None

    def delete(self, change: RowDelete) -> PlannedStatement | None:
        terms = self._key_terms(change.id, change.key)
        if terms is None:
            return None
        sql = f"DELETE FROM {self.table} WHERE {' AND '.join(terms)}"
        return PlannedStatement(change_id=change.id, kind="delete", sql=sql, row=self._row(change.key))

    def update(self, change: RowUpdate) -> PlannedStatement | None:
        terms = self._key_terms(change.id, change.key)
        complete = terms is not None
        sets: list[str] = []
        checks: list[str] = []
        unchecked: list[UncheckedColumn] = []
        for name, cell in change.set.items():
            column = self._column(change.id, name)
            if column is None:
                complete = False
                continue
            if cell.kind == "default":
                self._problem(change.id, name, "default_not_allowed", _DEFAULT_ONLY_NEW)
                complete = False
                continue
            literal = self._literal(change.id, column, cell)
            if literal is None:
                complete = False
                continue
            sets.append(f"{quote_ident(self.dialect, name)} = {literal}")
            if name in self.target.key:
                continue  # its key term pins it (CR-4)
            original = change.original[name]
            term = match_condition(self.dialect, column, original, self.styles[name], key=False)
            if term is None:
                reason = _NOT_EXACT.format(type=column.type_name) if column.compare == "none" else _PREVIEW_ONLY
                unchecked.append(UncheckedColumn(column=name, reason=reason))
            else:
                checks.append(term)
        if not complete or terms is None:
            return None
        sql = f"UPDATE {self.table} SET {', '.join(sets)} WHERE {' AND '.join([*terms, *checks])}"
        return PlannedStatement(change_id=change.id, kind="update", sql=sql, unchecked=unchecked,
                                row=self._row(change.key))

    def insert(self, change: RowInsert) -> tuple[PlannedStatement | None, list[str]]:
        """The statement (None with problems) and the required columns the row gives no value."""
        names: list[str] = []
        literals: list[str] = []
        complete = True
        for name, cell in change.values.items():
            column = self._column(change.id, name)
            if column is None:
                complete = False
                continue
            if cell.kind == "default":
                continue
            literal = self._literal(change.id, column, cell)
            if literal is None:
                complete = False
                continue
            names.append(quote_ident(self.dialect, name))
            literals.append(literal)
        given = {name for name, cell in change.values.items() if cell.kind != "default"}
        missing = [c.name for c in self.target.columns
                   if c.writable and not c.nullable and not c.has_default and c.name not in given]
        if not complete:
            return None, missing
        if names:
            sql = f"INSERT INTO {self.table} ({', '.join(names)}) VALUES ({', '.join(literals)})"
        else:
            sql = insert_defaults(self.dialect, self.table)
        return PlannedStatement(change_id=change.id, kind="insert", sql=sql), missing


def build_plan(dialect: Dialect, target: EditTarget, changes: EditChanges) -> DmlPlan:
    builder = _Builder(dialect, target)
    frame = transaction_frame(dialect)
    statements: list[PlannedStatement] = []
    missing_warnings: list[EditWarning] = []
    for delete in changes.deletes:
        if (statement := builder.delete(delete)) is not None:
            statements.append(statement)
    for update in changes.updates:
        if (statement := builder.update(update)) is not None:
            statements.append(statement)
    for insert in changes.inserts:
        statement, missing = builder.insert(insert)
        if statement is not None:
            statements.append(statement)
        if missing:
            missing_warnings.append(EditWarning(code="missing_required", change_id=insert.id, columns=missing,
                                                message=_MISSING_REQUIRED.format(columns=", ".join(missing))))
    warnings: list[EditWarning] = []
    if not target.transactional:
        warnings.append(EditWarning(code="non_transactional", message=_NON_TRANSACTIONAL))
    if unchecked := sorted({u.column for s in statements for u in s.unchecked}):
        warnings.append(EditWarning(code="unchecked_columns", columns=unchecked,
                                    message=_UNCHECKED.format(columns=", ".join(unchecked))))
    warnings += missing_warnings
    counts = EditCounts(updates=len(changes.updates), inserts=len(changes.inserts), deletes=len(changes.deletes))
    if builder.problems:
        return DmlPlan(builder.table, frame.begin, [], frame.commit, builder.problems, warnings, counts, "")
    script = script_text(dialect, [frame.begin, *(s.sql for s in statements), frame.commit])
    return DmlPlan(builder.table, frame.begin, statements, frame.commit, [], warnings, counts, script)
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_dml.py tests/sql/test_literals.py`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/sql/dml.py backend/tests/sql/test_dml.py
git commit -m "feat(sql): the grid commit's plan — DELETE, UPDATE, INSERT with key and original checks, per-cell problems"
```

---
### Task 5: `RowEditing`, `row_editing`, `matched_rows()`; the PostgreSQL and SQLite targets

**Files:**
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/domain/profiles.py`
- Create: `backend/dbaguy_backend/adapters/editing.py`
- Create: `backend/dbaguy_backend/adapters/postgres/editing.py`; modify `adapters/postgres/loaders.py`, `adapters/postgres/adapter.py`
- Create: `backend/dbaguy_backend/adapters/sqlite/editing.py`; modify `adapters/sqlite/adapter.py`
- Create: `backend/tests/test_editing_target.py`, `backend/tests/test_postgres_edits.py`, `backend/tests/test_sqlite_edits.py`
- Modify: `backend/tests/test_api.py` (`test_engines`)
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: Task 1 (`tests/edit_support`: `PgScratch`, `SqliteScratch`, `scratch`); Task 3 (`EditColumn`, `EditTarget`, `edit_kind`, `Compare`, `EditKind`); `sql/select_builder.relation_from_path`; `adapters/postgres/introspection.relation_oid`; `adapters/sql_text.quote_ident_sqlite`.
- Produces:
  - `adapters/base.py`: `AdapterCapabilities.row_editing: bool = False`; `StatementResult.matched_rows() -> int | None` (default `affected_rows()`); `class RowEditing(ABC)` with `edit_target(self, path: list[PathSegment]) -> EditTarget`; `DatabaseAdapter.info()` passes `row_editing`.
  - `domain/profiles.EngineInfo.row_editing: bool`.
  - `adapters/editing.py`: `NO_KEY`, `VIEWS`, `KEY_NOT_EXACT` (texts below), `KeyCandidate(name: str, columns: tuple[str, ...], primary: bool)`, `read_only_relation(path, reason) -> EditTarget`, `build_target(path, columns, candidates, *, transactional=True, backslash_escapes=True) -> EditTarget`.
  - `adapters/postgres/loaders.category_for_type_name(name: str) -> ColumnCategory`; `adapters/postgres/editing.edit_target(conn, path) -> EditTarget`; `PostgresConnection(DatabaseConnection, RowEditing).edit_target(path)` (checks the path's database first, as `object_detail` does); PostgreSQL's capability `row_editing=True`.
  - `adapters/sqlite/editing.affinity_kind(declared: str) -> tuple[EditKind, Compare]`, `edit_target(conn, path) -> EditTarget`; `SqliteConnection(DatabaseConnection, RowEditing).edit_target(path)` (sqlite3 errors translated as elsewhere); SQLite's `capabilities = AdapterCapabilities(row_editing=True)`.

Rules (spec §3.2; rulings E4, E6, E23):
- `build_target` picks the primary key, else the unique key on NOT NULL columns with the fewest columns, ties by name — the first whose every column has a compare other than `none`. Reasons, verbatim: `No primary key or unique key on NOT NULL columns: a row cannot be identified` (`NO_KEY`); `The key column <c> (<type>) cannot be matched exactly: a row cannot be identified`; `Views are read-only here` (`VIEWS`); `Materialized views are read-only here`; `Foreign tables are read-only here`; `Virtual tables are read-only here`. A view or another non-table is `editable=False`, never an error; a missing table is `NotFound`.
- PostgreSQL (`relkind` r, p only): kind from `_NAME_CATEGORY` by the type's name (a domain by its base type, one level, E23); `money` is `other`; compare `none` for float4 / float8, `equals` for bpchar, `as_text` for everything else; `attgenerated` → "generated column", `attidentity = 'a'` → "identity column (GENERATED ALWAYS)" (not writable); `has_default` = a default, an identity or a generated value. Keys: only key columns (not INCLUDE), no partial, expression or invalid index; a key on a composite-type column is never a candidate (`=` against it fails with 0A000, measured while planning).
- SQLite: tables only; kind by the declared type's affinity, refined inside NUMERIC (E6); generated columns (hidden 2 / 3) not writable; the INTEGER PRIMARY KEY is never NULL and gets a value when left out; keys: the PRIMARY KEY (it may hold NULL in SQLite: such a row is `key_incomplete` at the preview), else a non-partial unique index whose parts are all columns.

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_editing_target.py`:

```python
"""Which key identifies a row (spec ⑤ R-05-2, ruling E4): the engine-neutral choice among an engine's candidates."""

from dbaguy_backend.adapters.editing import NO_KEY, KeyCandidate, build_target
from dbaguy_backend.domain.edits import EditColumn
from dbaguy_backend.domain.metadata import PathSegment

PATH = [PathSegment(kind="schema", name="s"), PathSegment(kind="folder", name="tables"), PathSegment(kind="table", name="t")]


def column(name, nullable=False, compare="equals", type_name="int") -> EditColumn:
    return EditColumn(name=name, type_name=type_name, kind="number", nullable=nullable, has_default=False,
                      writable=True, compare=compare)


def test_the_primary_key_comes_first():
    columns = [column("id"), column("code")]
    target = build_target(PATH, columns, [KeyCandidate("u_code", ("code",), False), KeyCandidate("pk", ("id",), True)])
    assert (target.editable, target.key, target.key_source, target.columns) == (True, ["id"], "primary_key", columns)


def test_else_the_unique_key_on_not_null_columns_with_the_fewest_columns_then_by_name():
    columns = [column("a"), column("b"), column("c"), column("d", nullable=True)]
    candidates = [KeyCandidate("u_abc", ("a", "b", "c"), False), KeyCandidate("u_d", ("d",), False),
                  KeyCandidate("u_cb", ("c", "b"), False), KeyCandidate("u_ab", ("a", "b"), False)]
    target = build_target(PATH, columns, candidates)
    assert (target.key, target.key_source) == (["a", "b"], "unique")


def test_a_key_that_cannot_be_matched_exactly_gives_way_or_says_why():
    floaty = column("f", compare="none", type_name="double precision")
    assert build_target(PATH, [floaty, column("n")], [KeyCandidate("pk", ("f",), True),
                                                      KeyCandidate("u_n", ("n",), False)]).key == ["n"]
    refused = build_target(PATH, [floaty], [KeyCandidate("pk", ("f",), True)])
    assert not refused.editable and refused.reason == (
        "The key column f (double precision) cannot be matched exactly: a row cannot be identified")


def test_a_primary_key_may_hold_null_and_no_candidate_means_no_key():
    assert build_target(PATH, [column("code", nullable=True)], [KeyCandidate("pk", ("code",), True)]).key == ["code"]
    assert build_target(PATH, [column("a", nullable=True)], [KeyCandidate("u", ("a",), False)]).reason == NO_KEY
    assert build_target(PATH, [column("a")], [KeyCandidate("u", ("gone",), False)]).reason == NO_KEY
    target = build_target(PATH, [column("a")], [], transactional=False, backslash_escapes=False)
    assert (target.editable, target.reason, target.transactional, target.backslash_escapes) == (False, NO_KEY, False, False)
```

`backend/tests/test_postgres_edits.py` (Task 9 appends its live applies):

```python
"""PostgreSQL grid editing (spec ⑤): the edit target read from the catalog. Scratch objects only (zz_05a_*)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from dbaguy_backend.domain.errors import NotFound, ValidationFailed
from dbaguy_backend.domain.metadata import PathSegment
from tests.edit_support import PgScratch, scratch
from tests.support import requires_pg

pytestmark = requires_pg


@pytest.fixture
def pg():
    db = PgScratch()
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def described(target):
    return {c.name: (c.kind, c.compare, c.writable) for c in target.columns}


def test_a_table_with_a_primary_key_and_every_kind_of_column(pg):
    db, connection = pg
    with scratch(db) as make:
        domain = make("DOMAIN", "CREATE DOMAIN {t} AS integer CHECK (VALUE > 0)")
        composite = make("TYPE", "CREATE TYPE {t} AS (a int, b text)")
        table = make("TABLE", f"""CREATE TABLE {{t}} (
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, n numeric(30,10), f double precision, r real,
            t text NOT NULL, c char(5), b boolean, by bytea, j json, jb jsonb, x xml, p point, u uuid, m money,
            a int[], ts timestamptz DEFAULT now(), d {db.table(domain)}, k {db.table(composite)},
            g int GENERATED ALWAYS AS (1) STORED, ia int GENERATED ALWAYS AS IDENTITY)""")
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.key_source) == (True, ["id"], "primary_key")
    assert described(target) == {
        "id": ("number", "as_text", True), "n": ("number", "as_text", True), "f": ("number", "none", True),
        "r": ("number", "none", True), "t": ("text", "as_text", True), "c": ("text", "equals", True),
        "b": ("bool", "as_text", True), "by": ("binary", "as_text", True), "j": ("json", "as_text", True),
        "jb": ("json", "as_text", True), "x": ("text", "as_text", True), "p": ("other", "as_text", True),
        "u": ("text", "as_text", True), "m": ("other", "as_text", True), "a": ("other", "as_text", True),
        "ts": ("datetime", "as_text", True), "d": ("number", "as_text", True), "k": ("other", "as_text", True),
        "g": ("number", "as_text", False), "ia": ("number", "as_text", False),
    }
    by_name = {c.name: c for c in target.columns}
    assert (by_name["g"].read_only_reason, by_name["ia"].read_only_reason) == (
        "generated column", "identity column (GENERATED ALWAYS)")
    assert [c.name for c in target.columns if c.has_default] == ["id", "ts", "g", "ia"]
    assert (by_name["t"].nullable, by_name["n"].nullable, by_name["n"].type_name) == (False, True, "numeric(30,10)")


def test_the_unique_key_with_the_fewest_not_null_columns_identifies_rows(pg):
    db, connection = pg
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (a int NOT NULL, b int NOT NULL, c int NOT NULL, d int, "
                              "e int NOT NULL, f int NOT NULL)")
        qualified = db.table(table)
        db.execute(
            f"CREATE UNIQUE INDEX {table}_abc ON {qualified} (a, b, c)",
            f"CREATE UNIQUE INDEX {table}_d ON {qualified} (d)",  # a nullable column: not a key
            f"CREATE UNIQUE INDEX {table}_e ON {qualified} (e) WHERE e > 0",  # partial
            f"CREATE UNIQUE INDEX {table}_x ON {qualified} ((a + 1))",  # an expression
            f"CREATE UNIQUE INDEX {table}_fe ON {qualified} (f, e) INCLUDE (d)",  # INCLUDE is not part of the key
        )
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.key_source) == (True, ["f", "e"], "unique")


def test_a_key_that_cannot_be_matched_exactly_gives_way_or_says_why(pg):  # ruling E4
    db, connection = pg
    with scratch(db) as make:
        floaty = make("TABLE", "CREATE TABLE {t} (f double precision PRIMARY KEY, n int NOT NULL UNIQUE)")
        only = make("TABLE", "CREATE TABLE {t} (f double precision PRIMARY KEY, v text)")
        composite = make("TYPE", "CREATE TYPE {t} AS (a int, b text)")
        keyed = make("TABLE", f"CREATE TABLE {{t}} (k {db.table(composite)} PRIMARY KEY, v text)")
        assert connection.edit_target(db.path(floaty)).key == ["n"]
        refused = connection.edit_target(db.path(only))
        on_composite = connection.edit_target(db.path(keyed))
    assert not refused.editable and refused.reason == (
        "The key column f (double precision) cannot be matched exactly: a row cannot be identified")
    assert (on_composite.editable, on_composite.reason) == (False, NO_KEY)


def test_views_partitions_tables_without_a_key_and_missing_tables(pg):
    db, connection = pg
    with scratch(db) as make:
        loose = make("TABLE", "CREATE TABLE {t} (a int, b text)")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT * FROM {db.table(loose)}")
        matview = make("MATERIALIZED VIEW", f"CREATE MATERIALIZED VIEW {{t}} AS SELECT * FROM {db.table(loose)}")
        parent = make("TABLE", "CREATE TABLE {t} (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)")
        child = make("TABLE", f"CREATE TABLE {{t}} PARTITION OF {db.table(parent)} "
                              "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')")
        partition = [*db.path(parent), PathSegment(kind="folder", name="partitions"),
                     PathSegment(kind="partition", name=child)]
        assert connection.edit_target(db.path(loose)).reason == NO_KEY
        assert connection.edit_target(db.path(view, "view")).reason == VIEWS
        assert connection.edit_target(db.path(matview, "materialized_view")).reason == (
            "Materialized views are read-only here")
        assert connection.edit_target(db.path(parent)).key == ["id", "at"]
        assert connection.edit_target(partition).key == ["id", "at"]
    with pytest.raises(NotFound):
        connection.edit_target(db.path("zz_05a_never_there"))
    with pytest.raises(ValidationFailed):  # the connection is bound to dbaguy (CLAUDE.md: one connection per database)
        connection.edit_target([PathSegment(kind="database", name="analytics"), *db.path(loose)[1:]])
```

`backend/tests/test_sqlite_edits.py` (Task 9 appends its live applies):

```python
"""SQLite grid editing (spec ⑤): the edit target from the schema (affinity, keys, views, virtual tables)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from tests.edit_support import SqliteScratch, scratch


@pytest.fixture
def lite(tmp_path):
    db = SqliteScratch(tmp_path)
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_columns_take_their_kind_from_the_declared_type(lite):  # ruling E6
    db, connection = lite
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, name VARCHAR(20) NOT NULL, body TEXT, data BLOB, "
                              "anything, price REAL, amount DECIMAL(10,2), made DATETIME DEFAULT CURRENT_TIMESTAMP, "
                              "ok BOOLEAN, doc JSON, total INT GENERATED ALWAYS AS (id * 2) VIRTUAL)")
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.key_source) == (True, ["id"], "primary_key")
    assert {c.name: (c.kind, c.compare, c.writable, c.has_default) for c in target.columns} == {
        "id": ("number", "equals", True, True), "name": ("text", "equals", True, False),
        "body": ("text", "equals", True, False), "data": ("binary", "equals", True, False),
        "anything": ("other", "equals", True, False), "price": ("number", "none", True, False),
        "amount": ("number", "equals", True, False), "made": ("datetime", "equals", True, True),
        "ok": ("bool", "equals", True, False), "doc": ("json", "equals", True, False),
        "total": ("number", "equals", False, True),
    }
    assert [c.name for c in target.columns if not c.nullable] == ["id", "name"]


def test_keys_views_and_virtual_tables(lite):
    db, connection = lite
    with scratch(db) as make:
        no_rowid = make("TABLE", "CREATE TABLE {t} (code TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        unique = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL, b TEXT NOT NULL, c TEXT, UNIQUE (a, b))")
        partial = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL, c TEXT)")
        db.execute(f'CREATE UNIQUE INDEX "{partial}_a" ON {db.q(partial)} (a) WHERE a > \'\'')
        expression = make("TABLE", "CREATE TABLE {t} (a TEXT NOT NULL)")
        db.execute(f'CREATE UNIQUE INDEX "{expression}_x" ON {db.q(expression)} (lower(a))')
        nullable_pk = make("TABLE", "CREATE TABLE {t} (code TEXT PRIMARY KEY, v TEXT)")
        real_pk = make("TABLE", "CREATE TABLE {t} (r REAL PRIMARY KEY)")
        loose = make("TABLE", "CREATE TABLE {t} (a, b)")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT * FROM {db.table(loose)}")
        fts = make("TABLE", "CREATE VIRTUAL TABLE {t} USING fts5(body)")
        found = {name: connection.edit_target(db.path(name))
                 for name in (no_rowid, unique, partial, expression, nullable_pk, real_pk, loose, fts)}
        found[view] = connection.edit_target(db.path(view, "view"))
    assert found[no_rowid].key == ["code"]
    assert (found[unique].key, found[unique].key_source) == (["a", "b"], "unique")
    assert found[partial].reason == NO_KEY and found[expression].reason == NO_KEY and found[loose].reason == NO_KEY
    assert found[nullable_pk].key == ["code"]  # SQLite lets this key hold NULL: such a row is key_incomplete
    assert found[real_pk].reason == "The key column r (REAL) cannot be matched exactly: a row cannot be identified"
    assert (found[view].reason, found[fts].reason) == (VIEWS, "Virtual tables are read-only here")
```

In `backend/tests/test_api.py::test_engines`, after the loop:

```python
    assert engines["postgres"]["row_editing"] and engines["sqlite"]["row_editing"]
    assert not engines["couchbase"]["row_editing"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_editing_target.py tests/test_postgres_edits.py tests/test_sqlite_edits.py tests/test_api.py::test_engines`
Expected: FAIL (ImportError: `dbaguy_backend.adapters.editing`).

- [ ] **Step 3: The contract.** In `adapters/base.py` (import `from ..domain.edits import EditTarget`):

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    ...  # existing fields unchanged
    read_ahead_bytes: int | None = None
    row_editing: bool = False  # a table-data grid can be edited (spec ⑤): the connection is RowEditing
```

In `StatementResult`, before `presentation`:

```python
    def matched_rows(self) -> int | None:
        """How many rows the DML statement matched (spec ⑤ D-05-5), an UPDATE whose new values equal the old ones
        included. The default is affected_rows(); an engine whose count is rows changed overrides it (MySQL). None: not
        known (the apply then fails closed)."""
        return self.affected_rows()
```

Before `class DatabaseAdapter`:

```python
class RowEditing(ABC):
    """A connection whose engine can tell how a table's rows are identified and written (capability `row_editing`,
    spec ⑤ §3.2). The profile's metadata connection answers it, on its worker thread like every method."""

    @abstractmethod
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        """A view or another non-table, or a table without a usable key: `editable=False` with the reason, never an
        error. NotFound when the table does not exist."""
```

`DatabaseAdapter.info()` adds `row_editing=caps.row_editing`; `domain/profiles.EngineInfo` gains `row_editing: bool  # a table-data grid can be edited (spec ⑤)` after `plan_format`.

- [ ] **Step 4: The engine-neutral key rule** — `backend/dbaguy_backend/adapters/editing.py`:

```python
"""The engine-neutral rule of grid editing's target (spec ⑤ §3.2.1, R-05-2, ruling E4): which key identifies a row,
given the columns and key candidates an engine read from its catalog. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.edits import EditColumn, EditTarget
from ..domain.metadata import PathSegment

NO_KEY = "No primary key or unique key on NOT NULL columns: a row cannot be identified"
VIEWS = "Views are read-only here"
KEY_NOT_EXACT = "The key column {name} ({type}) cannot be matched exactly: a row cannot be identified"


@dataclass(frozen=True)
class KeyCandidate:
    name: str  # the index or constraint: ties between unique keys are broken by it
    columns: tuple[str, ...]  # in index order
    primary: bool


def read_only_relation(path: Sequence[PathSegment], reason: str) -> EditTarget:
    """A view or another object that is not a table: shown read-only, with the reason (not an error, spec §3.2.1)."""
    return EditTarget(path=list(path), editable=False, reason=reason)


def build_target(
    path: Sequence[PathSegment], columns: Sequence[EditColumn], candidates: Sequence[KeyCandidate], *,
    transactional: bool = True, backslash_escapes: bool = True,
) -> EditTarget:
    """The primary key, else the unique key on NOT NULL columns with the fewest columns (then by name): the first whose
    every column can be matched exactly (compare not `none`, ruling E4). A primary key may hold NULL (SQLite): such a
    row is refused by the preview (`key_incomplete`). Not editable, with the reason, when no candidate qualifies."""
    by_name = {column.name: column for column in columns}
    ordered = sorted(candidates, key=lambda key: (not key.primary, len(key.columns), key.name))
    usable = [
        key for key in ordered
        if key.columns and all(name in by_name for name in key.columns)
        and (key.primary or not any(by_name[name].nullable for name in key.columns))
    ]
    common = {"path": list(path), "columns": list(columns), "transactional": transactional,
              "backslash_escapes": backslash_escapes}
    for key in usable:
        if all(by_name[name].compare != "none" for name in key.columns):
            source = "primary_key" if key.primary else "unique"
            return EditTarget(editable=True, key=list(key.columns), key_source=source, **common)
    if usable:
        column = next(by_name[name] for name in usable[0].columns if by_name[name].compare == "none")
        return EditTarget(editable=False, reason=KEY_NOT_EXACT.format(name=column.name, type=column.type_name), **common)
    return EditTarget(editable=False, reason=NO_KEY, **common)
```

- [ ] **Step 5: PostgreSQL.** In `adapters/postgres/loaders.py`, before `category_for_oid`:

```python
def category_for_type_name(name: str) -> ColumnCategory:
    """The grid category of a built-in type by its pg_type name (grid editing reads columns from the catalog)."""
    return _NAME_CATEGORY.get(name, ColumnCategory.OTHER)
```

`backend/dbaguy_backend/adapters/postgres/editing.py`:

```python
"""What grid editing needs to know about a PostgreSQL table (spec ⑤ §3.2.2): its kind, how each column is written and
compared, and the key a row is identified by. Three catalog queries on the metadata connection (native loaders)."""

import psycopg

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .introspection import relation_oid
from .loaders import category_for_type_name

_RELKIND = "SELECT relkind::text FROM pg_class WHERE oid = %(oid)s"
# A domain is read through its base type (one level, ruling E23); typtype 'c' marks a composite type (ruling E4).
_COLUMNS = """
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef, a.attidentity::text,
       a.attgenerated::text, COALESCE(b.typname, t.typname)::text, COALESCE(b.typtype, t.typtype)::text
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_type b ON t.typtype = 'd' AND b.oid = t.typbasetype
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
# Key columns only (INCLUDE columns come after indnkeyatts); no partial, expression or invalid index.
_KEYS = """
SELECT ic.relname::text, i.indisprimary, array_agg(a.attname::text ORDER BY k.ord)
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k (attnum, ord)
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %(oid)s AND i.indisvalid AND k.ord <= i.indnkeyatts
  AND (i.indisprimary OR (i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL))
GROUP BY ic.relname, i.indisprimary
"""
_READ_ONLY = {"v": VIEWS, "m": "Materialized views are read-only here", "f": "Foreign tables are read-only here"}
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
_PADDED = frozenset({"bpchar"})  # ::text drops the padding the loaded value has; `=` ignores it (A4)


def _column(
    name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
    typtype: str,
) -> tuple[EditColumn, bool]:
    """The column, and whether its type is composite (a key on it cannot be written as `c = '…'`, 0A000)."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _PADDED else "as_text"
    reason = "generated column" if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
    column = EditColumn(
        name=name, type_name=type_name, kind=kind, nullable=nullable,
        has_default=has_default or bool(identity) or bool(generated), writable=reason is None,
        read_only_reason=reason, compare=compare,
    )
    return column, typtype == "c"


def edit_target(conn: psycopg.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    oid = relation_oid(conn, qualified(Dialect.POSTGRES, schema, name))
    row = conn.execute(_RELKIND, {"oid": oid}).fetchone()
    relkind = row[0] if row is not None else ""
    if relkind not in ("r", "p"):
        return read_only_relation(path, _READ_ONLY.get(relkind, "This object is read-only here"))
    read = [_column(*row) for row in conn.execute(_COLUMNS, {"oid": oid})]
    composite = {column.name for column, is_composite in read if is_composite}
    candidates = [
        KeyCandidate(index, tuple(columns), primary)
        for index, primary, columns in conn.execute(_KEYS, {"oid": oid})
        if not composite.intersection(columns)
    ]
    return build_target(path, [column for column, _ in read], candidates)
```

In `adapters/postgres/adapter.py`: import `RowEditing` (from `..base`), `EditTarget` (from `...domain.edits`) and `editing` (`from . import ddl, editing, introspection`); `class PostgresConnection(DatabaseConnection, RowEditing)`; before `schema_snapshot`:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        self._check_database(path)
        return editing.edit_target(self._conn, path)
```

and `capabilities = AdapterCapabilities(per_database_connection=True, cancel_aborts_transaction=True, row_editing=True)`.

- [ ] **Step 6: SQLite** — `backend/dbaguy_backend/adapters/sqlite/editing.py`:

```python
"""What grid editing needs to know about a SQLite table (spec ⑤ §3.2.2): tables only (no view, no virtual table), each
column read by its declared type's affinity (ruling E6), the primary key or a unique index on NOT NULL columns. The
rowid is never a key: `SELECT *` does not return it."""

import sqlite3

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from ..sql_text import quote_ident_sqlite as q

_GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
_COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?) ORDER BY cid'
_INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
_INDEX_COLUMNS = "SELECT name FROM pragma_index_info(?, ?) ORDER BY seqno"


def affinity_kind(declared: str) -> tuple[EditKind, Compare]:
    """SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT; BLOB or no type; REAL, FLOA, DOUB; else NUMERIC
    — and inside NUMERIC what the declared name says: DATE / TIME, BOOL, JSON (ruling E6). REAL affinity holds floating
    point, which is not compared."""
    upper = declared.upper()
    if "INT" in upper:
        return "number", "equals"
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        return "text", "equals"
    if "BLOB" in upper:
        return "binary", "equals"
    if not upper.strip():
        return "other", "equals"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "number", "none"
    if "DATE" in upper or "TIME" in upper:
        return "datetime", "equals"
    if "BOOL" in upper:
        return "bool", "equals"
    if "JSON" in upper:
        return "json", "equals"
    return "number", "equals"


def edit_target(conn: sqlite3.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    schema = schema or "main"
    found = conn.execute(
        f"SELECT type, sql FROM {q(schema)}.sqlite_master WHERE name = ? AND type IN ('table', 'view')", (name,)
    ).fetchone()
    if found is None:
        raise NotFound(f"Table {name} not found")
    kind, ddl = found
    if kind == "view":
        return read_only_relation(path, VIEWS)
    if (ddl or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
        return read_only_relation(path, "Virtual tables are read-only here")
    rows = conn.execute(_COLUMNS, (name, schema)).fetchall()
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in rows if pk > 0)]
    # INTEGER PRIMARY KEY is the rowid: never NULL, assigned when left out
    rowid = key[0] if len(key) == 1 and next(t for c, t, *_ in rows if c == key[0]).upper() == "INTEGER" else None
    columns = []
    for column, declared, notnull, default, _, hidden in rows:
        kind_of, compare = affinity_kind(declared or "")
        generated = hidden in _GENERATED
        columns.append(EditColumn(
            name=column, type_name=declared or "", kind=kind_of, nullable=not notnull and column != rowid,
            has_default=default is not None or generated or column == rowid, writable=not generated,
            read_only_reason="generated column" if generated else None, compare=compare,
        ))
    candidates = [KeyCandidate("PRIMARY KEY", tuple(key), True)] if key else []
    for index, unique, origin, partial in conn.execute(_INDEXES, (name, schema)).fetchall():
        if not unique or partial or origin == "pk":
            continue
        parts = [part for (part,) in conn.execute(_INDEX_COLUMNS, (index, schema)).fetchall()]
        if parts and None not in parts:  # an expression has no column name
            candidates.append(KeyCandidate(index, tuple(parts), False))
    return build_target(path, columns, candidates)
```

In `adapters/sqlite/adapter.py`: import `AdapterCapabilities` and `RowEditing` (from `..base`), `EditTarget`, `editing` (`from . import editing, introspection`); `class SqliteConnection(DatabaseConnection, RowEditing)`; before `schema_snapshot`:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        try:
            return editing.edit_target(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc
```

and in `SqliteAdapter`: `capabilities = AdapterCapabilities(row_editing=True)`.

- [ ] **Step 7: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_editing_target.py tests/test_postgres_edits.py tests/test_sqlite_edits.py tests/test_api.py tests/test_postgres_adapter.py tests/test_sqlite_adapter.py`
Expected: PASS.

- [ ] **Step 8: Regenerate the API types, run the whole suite, commit**

Run: `npm run gen:api && npm run typecheck && npm run build && cd backend && .venv/bin/pytest -q`
Expected: `EngineInfo` in `schema.d.ts` gains `row_editing: boolean`; typecheck, build and the suite pass.

```bash
git add backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/domain/profiles.py \
  backend/dbaguy_backend/adapters/editing.py backend/dbaguy_backend/adapters/postgres/editing.py \
  backend/dbaguy_backend/adapters/postgres/loaders.py backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/editing.py backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/tests/test_editing_target.py backend/tests/test_postgres_edits.py backend/tests/test_sqlite_edits.py \
  backend/tests/test_api.py src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(adapters): RowEditing and the edit target for PostgreSQL and SQLite (keys, kinds, compare rules)"
```

---
### Task 6: The MySQL and SQL Server targets; MySQL's matched rows

**Files:**
- Create: `backend/dbaguy_backend/adapters/mysql/editing.py`; modify `adapters/mysql/types.py`, `adapters/mysql/adapter.py`
- Create: `backend/dbaguy_backend/adapters/mssql/editing.py`; modify `adapters/mssql/types.py`, `adapters/mssql/adapter.py`
- Create: `backend/tests/test_mysql_edits.py`, `backend/tests/test_mssql_edits.py`
- Modify: `backend/tests/test_api.py` (`test_engines`)

**Interfaces:**
- Consumes: Task 5 (`RowEditing`, `matched_rows()`, `adapters/editing`: `KeyCandidate`, `build_target`, `read_only_relation`, `NO_KEY`, `VIEWS`); Task 1 (`MysqlScratch`, `MssqlScratch`, `scratch`, `items_table`); `adapters/mysql/catalog.query`, `first_row`; `adapters/mssql/catalog.batch`, `object_id_sql`, `result_sets`, `type_sql`.
- Produces:
  - `adapters/mysql/types.py`: `SPATIAL_TYPES: frozenset[str]`, `category_for_data_type(data_type: str) -> ColumnCategory` (built from the same kind tables as the protocol's, DRY).
  - `adapters/mysql/adapter.py`: `rows_matched(info: str | None) -> int | None`; `_VerbatimCursor.ok_info: str | None` (the last OK packet's info text); `MysqlResult.matched_rows()` (the UPDATE's `Rows matched`, else `affected_rows()`); `MysqlConnection(DatabaseConnection, RowEditing).edit_target(path)`; capability `row_editing=True`.
  - `adapters/mysql/editing.edit_target(conn, path) -> EditTarget` with `transactional` (the table's engine, `ENGINES.TRANSACTIONS = 'YES'`) and `backslash_escapes` (`NO_BACKSLASH_ESCAPES` not in `@@SESSION.sql_mode`).
  - `adapters/mssql/types.category_for_type(base: str) -> ColumnCategory` (`described_kind` uses it); `adapters/mssql/editing.edit_target(conn, path) -> EditTarget`; `MssqlConnection(DatabaseConnection, RowEditing).edit_target(path)` through `_metadata_call` (READ UNCOMMITTED, LOCK_TIMEOUT 5000, the locked-object message) after `_check_database`; capability `row_editing=True`.

Rules (spec §3.2.2, D-05-5, D-05-6, D-05-13; rulings E4, E15, E17):
- MySQL: `BASE TABLE` only (`VIEW` → `VIEWS`, `SYSTEM VIEW` → "System views are read-only here"); kind from DATA_TYPE; compare `none` for FLOAT / DOUBLE / REAL, spatial types and VECTOR; not writable: `VIRTUAL GENERATED` / `STORED GENERATED` in EXTRA ("generated column" — `DEFAULT_GENERATED` is an expression default and stays writable, E15), spatial ("geometry values are not edited here"), VECTOR ("vector values are not edited here"); `has_default`: a COLUMN_DEFAULT, AUTO_INCREMENT, generated or DEFAULT_GENERATED. Keys: `NON_UNIQUE = 0` indexes whose every part is a column (a functional key part has COLUMN_NAME NULL); `PRIMARY` is the primary key.
- MySQL `matched_rows`: `_VerbatimCursor.execute_verbatim` keeps `cmd_query`'s dict's `info_msg` when the answer is an OK packet (A1); `rows_matched` reads `^Rows matched: (\d+)`; any other statement (INSERT, DELETE: no info) falls back to `affected_rows()`. The client flags are never changed: a user's own UPDATE still reports rows changed.
- SQL Server: `type = 'U'` only (`V` → `VIEWS`); kind by the base type (an alias type through its base; a CLR type by its own name); compare `none` for float, real, text, ntext, image, xml, sql_variant, CLR types and datetime2 / datetimeoffset / time of scale > 6; not writable, in this order: computed ("computed column"), identity ("IDENTITY column"), rowversion ("rowversion column"), `generated_always_type <> 0` ("period column (system-versioned)"), sql_variant and CLR types ("<type> values are not edited here"); `unicode` False for char / varchar / text (E17); `type_name` as T-SQL writes it (`type_sql`). Keys: the primary key or a unique index without filter, not disabled, key columns only (not included ones); names from `sys.columns` joins (never `COL_NAME`).

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_mysql_edits.py` (Task 10 appends its live applies):

```python
"""MySQL grid editing (spec ⑤): the edit target from information_schema, and the rows an UPDATE matched (D-05-5, A1).
Scratch objects only (zz_05a_*)."""

import pytest

from dbaguy_backend.adapters.editing import NO_KEY, VIEWS
from dbaguy_backend.adapters.mysql.adapter import rows_matched
from dbaguy_backend.domain.errors import NotFound
from tests.edit_support import MysqlScratch, items_table, scratch
from tests.support import requires_mysql

pytestmark = requires_mysql


@pytest.fixture
def my():
    db = MysqlScratch()
    connection = db.open()
    try:
        yield db, connection
    finally:
        connection.close()
        db.close()


def test_rows_matched_is_read_from_an_updates_info():  # A1
    assert rows_matched("Rows matched: 1  Changed: 0  Warnings: 0") == 1
    assert rows_matched("Rows matched: 12  Changed: 3  Warnings: 1") == 12
    assert rows_matched("Records: 2  Duplicates: 0  Warnings: 0") is None
    assert rows_matched(None) is None


def test_an_update_to_the_same_value_still_matched_its_row(my):  # D-05-5: affected rows would say 0
    db, connection = my
    with scratch(db) as make:
        table = db.table(items_table(db, make))

        def counts(sql):
            result = connection.run(sql)
            try:
                return result.matched_rows(), result.affected_rows()
            finally:
                result.close()

        assert counts(f"UPDATE {table} SET label = 'a' WHERE id = 1") == (1, 0)
        assert counts(f"UPDATE {table} SET label = 'z' WHERE id IN (2, 3)") == (2, 2)
        assert counts(f"UPDATE {table} SET label = 'z' WHERE id = 99") == (0, 0)
        assert counts(f"INSERT INTO {table} (id, label) VALUES (4, 'd')") == (1, 1)
        assert counts(f"DELETE FROM {table} WHERE id = 99") == (0, 0)


def test_a_table_with_a_primary_key_and_every_kind_of_column(my):
    db, connection = my
    with scratch(db) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(20) NOT NULL, "
                              "amount DECIMAL(10,2), f FLOAT, d DOUBLE, flag TINYINT(1), bits BIT(8), kind ENUM('a','b'), "
                              "doc JSON, data BLOB, raw VARBINARY(16), day DATE, at DATETIME(6), "
                              "made TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, shape GEOMETRY, "
                              "twice INT GENERATED ALWAYS AS (CHAR_LENGTH(label)) VIRTUAL, "
                              "loud VARCHAR(40) GENERATED ALWAYS AS (CONCAT(label, '!')) STORED) ENGINE=InnoDB")
        target = connection.edit_target(db.path(table))
    assert (target.editable, target.key, target.transactional, target.backslash_escapes) == (True, ["id"], True, True)
    assert {c.name: (c.kind, c.compare, c.writable, c.has_default) for c in target.columns} == {
        "id": ("number", "equals", True, True), "label": ("text", "equals", True, False),
        "amount": ("number", "equals", True, False), "f": ("number", "none", True, False),
        "d": ("number", "none", True, False), "flag": ("number", "equals", True, False),
        "bits": ("number", "equals", True, False), "kind": ("text", "equals", True, False),
        "doc": ("json", "equals", True, False), "data": ("binary", "equals", True, False),
        "raw": ("binary", "equals", True, False), "day": ("datetime", "equals", True, False),
        "at": ("datetime", "equals", True, False), "made": ("datetime", "equals", True, True),
        "shape": ("binary", "none", False, False), "twice": ("number", "equals", False, True),
        "loud": ("text", "equals", False, True),
    }
    by_name = {c.name: c for c in target.columns}
    assert (by_name["twice"].read_only_reason, by_name["shape"].read_only_reason) == (
        "generated column", "geometry values are not edited here")
    assert (by_name["amount"].type_name, by_name["label"].nullable, by_name["at"].nullable) == ("decimal(10,2)", False, True)


def test_keys_engines_views_and_the_sql_mode(my):  # D-05-6, D-05-13, ruling E4
    db, connection = my
    with scratch(db) as make:
        unique = make("TABLE", "CREATE TABLE {t} (a INT NOT NULL, b INT NOT NULL, c INT, label VARCHAR(20) NOT NULL, "
                               "UNIQUE KEY u_ab (a, b), UNIQUE KEY u_c (c), UNIQUE KEY u_fn ((lower(label)))) "
                               "ENGINE=InnoDB")
        functional = make("TABLE", "CREATE TABLE {t} (label VARCHAR(20) NOT NULL, UNIQUE KEY u_fn ((lower(label)))) "
                                   "ENGINE=InnoDB")
        myisam = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, v INT) ENGINE=MyISAM")
        view = make("VIEW", f"CREATE VIEW {{t}} AS SELECT * FROM {db.table(myisam)}")
        found = {name: connection.edit_target(db.path(name)) for name in (unique, functional, myisam)}
        found[view] = connection.edit_target(db.path(view, "view"))
        no_escapes = db.open(options={"sql_mode": "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"})
        try:
            assert no_escapes.edit_target(db.path(myisam)).backslash_escapes is False
        finally:
            no_escapes.close()
    assert (found[unique].key, found[unique].key_source) == (["a", "b"], "unique")
    assert found[functional].reason == NO_KEY
    assert (found[myisam].editable, found[myisam].transactional) == (True, False)
    assert found[view].reason == VIEWS
    with pytest.raises(NotFound):
        connection.edit_target(db.path("zz_05a_never_there"))
```

`backend/tests/test_mssql_edits.py` (Task 10 appends its live applies):

```python
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
```

`backend/tests/test_api.py::test_engines` — replace Task 5's two lines with:

```python
    assert {name: info["row_editing"] for name, info in engines.items()} == {
        "postgres": True, "mysql": True, "mssql": True, "sqlite": True, "couchbase": False}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_edits.py tests/test_mssql_edits.py tests/test_api.py::test_engines`
Expected: FAIL (ImportError: `rows_matched`).

- [ ] **Step 3: MySQL.** In `adapters/mysql/types.py`, before `column_kind`:

```python
# The spatial types' names in information_schema.COLUMNS.DATA_TYPE (the protocol calls them all GEOMETRY)
SPATIAL_TYPES = frozenset({
    "geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection",
    "geomcollection",
})
_BY_DATA_TYPE: dict[str, ColumnCategory] = {
    **{kind.name: kind.category for kind in _FIXED.values()},
    **{text: ColumnCategory.TEXT for text, _ in _STRINGS.values()},
    **{binary: ColumnCategory.BINARY for _, binary in _STRINGS.values()},
    **dict.fromkeys(("tinytext", "mediumtext", "longtext", "enum", "set"), ColumnCategory.TEXT),
    **dict.fromkeys(("tinyblob", "mediumblob", "longblob", *SPATIAL_TYPES), ColumnCategory.BINARY),
}


def category_for_data_type(data_type: str) -> ColumnCategory:
    """The grid category of a column by its information_schema DATA_TYPE (grid editing reads the catalog)."""
    return _BY_DATA_TYPE.get(data_type.lower(), ColumnCategory.OTHER)
```

`backend/dbaguy_backend/adapters/mysql/editing.py`:

```python
"""What grid editing needs to know about a MySQL table (spec ⑤ §3.2.2): base tables only, each column's kind and whether
it can be written and compared, the key, whether the table's engine can roll back (D-05-6), and whether the session
reads backslashes as escapes (D-05-13). Four catalog queries on the metadata connection, which has the session's
profile options (time_zone, sql_mode)."""

from mysql.connector.connection import MySQLConnection

from ...domain.edits import Compare, EditColumn, EditTarget, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import catalog
from .types import SPATIAL_TYPES, category_for_data_type

_TABLE = """
SELECT t.TABLE_TYPE, e.TRANSACTIONS
FROM information_schema.TABLES t LEFT JOIN information_schema.ENGINES e ON e.ENGINE = t.ENGINE
WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s
"""
_COLUMNS = """
SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
# COLUMN_NAME is NULL for a functional key part (ruling E4)
_KEYS = """
SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""
_SQL_MODE = "SELECT @@SESSION.sql_mode"
_GENERATED = ("VIRTUAL GENERATED", "STORED GENERATED")  # not DEFAULT_GENERATED: an expression default (ruling E15)
_NOT_EXACT = frozenset({"float", "double", "real", "vector", *SPATIAL_TYPES})


def _column(
    name: str, column_type: str, data_type: str, is_nullable: str, default: str | None, extra: str | None
) -> EditColumn:
    data_type, extra = data_type.lower(), (extra or "").upper()
    generated = any(word in extra for word in _GENERATED)
    if generated:
        reason = "generated column"
    elif data_type in SPATIAL_TYPES:
        reason = "geometry values are not edited here"
    elif data_type == "vector":
        reason = "vector values are not edited here"
    else:
        reason = None
    compare: Compare = "none" if data_type in _NOT_EXACT else "equals"
    return EditColumn(
        name=name, type_name=column_type, kind=edit_kind(category_for_data_type(data_type)),
        nullable=is_nullable == "YES",
        has_default=default is not None or generated or "AUTO_INCREMENT" in extra or "DEFAULT_GENERATED" in extra,
        writable=reason is None, read_only_reason=reason, compare=compare,
    )


def edit_target(conn: MySQLConnection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    found = catalog.first_row(conn, _TABLE, (schema, name))
    if found is None:
        raise NotFound(f"Table {schema}.{name} not found")
    table_type, transactions = found
    if table_type != "BASE TABLE":
        return read_only_relation(path, VIEWS if table_type == "VIEW" else "System views are read-only here")
    columns = [_column(*row) for row in catalog.query(conn, _COLUMNS, (schema, name))]
    parts: dict[str, list[str | None]] = {}
    for index, column in catalog.query(conn, _KEYS, (schema, name)):
        parts.setdefault(index, []).append(column)
    candidates = [KeyCandidate(index, tuple(c for c in cols if c is not None), index == "PRIMARY")
                  for index, cols in parts.items() if None not in cols]
    mode = catalog.first_row(conn, _SQL_MODE)
    escapes = "NO_BACKSLASH_ESCAPES" not in str(mode[0] if mode else "").upper().split(",")
    return build_target(path, columns, candidates, transactional=transactions == "YES", backslash_escapes=escapes)
```

In `adapters/mysql/adapter.py` (imports: `RowEditing`, `EditTarget`, `from . import catalog, editing, introspection`), after `_PARSE_ERRNOS`:

```python
_ROWS_MATCHED = re.compile(r"Rows matched: (\d+)")


def rows_matched(info: str | None) -> int | None:
    """The rows an UPDATE matched, from its OK packet's info (`Rows matched: 1  Changed: 0  Warnings: 0`, ⑤ A1); None
    for any other statement (an INSERT or a DELETE has no such info)."""
    found = _ROWS_MATCHED.match(info or "")
    return int(found.group(1)) if found else None
```

`_VerbatimCursor`:

```python
    ok_info: str | None = None  # the OK packet's info text of the last statement (`rows_matched`)

    def execute_verbatim(self, sql: str) -> None:
        self._connection.handle_unread_result()
        self._reset_result()
        statement = sql.encode(self._connection.python_charset)
        self._executed = statement
        answer = self._connection.cmd_query(statement)
        # An UPDATE's OK packet says how many rows it matched, which affected_rows (rows changed) does not (⑤ D-05-5)
        self.ok_info = answer.get("info_msg") if isinstance(answer, dict) and "affected_rows" in answer else None
        self._handle_result(answer)
```

`MysqlResult`, before `next_result`:

```python
    def matched_rows(self) -> int | None:
        matched = rows_matched(self._cursor.ok_info)
        return matched if matched is not None else self.affected_rows()
```

`class MysqlConnection(DatabaseConnection, RowEditing)` with, before `schema_snapshot`:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        return editing.edit_target(self._conn, path)
```

and `capabilities = AdapterCapabilities(row_editing=True)` (the existing comment kept above it).

- [ ] **Step 4: SQL Server.** In `adapters/mssql/types.py`, replace `described_kind`:

```python
def category_for_type(base: str) -> ColumnCategory:
    """The grid category of a built-in type by its name (`decimal`, `nvarchar`, …)."""
    return _CATEGORY_BY_TYPE.get(base.lower(), ColumnCategory.OTHER)


def described_kind(system_type_name: str) -> ColumnKind:
    """From sys.dm_exec_describe_first_result_set's system_type_name, e.g. `decimal(38,10)`."""
    return ColumnKind(system_type_name, category_for_type(system_type_name.split("(", 1)[0].strip()))
```

`backend/dbaguy_backend/adapters/mssql/editing.py`:

```python
"""What grid editing needs to know about a SQL Server table (spec ⑤ §3.2.2): user tables only, each column's kind and
whether it can be written and compared exactly, whether its text is Unicode (N'…' only where needed, A8), and the key.
One batch on the metadata connection (READ UNCOMMITTED, LOCK_TIMEOUT 5000: a table locked by an uncommitted DDL
answers LOCKED_MESSAGE, CLAUDE.md); names come from catalog-view joins, never COL_NAME / OBJECT_NAME."""

from ...domain.edits import EditColumn, EditTarget, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import driver
from .catalog import batch, object_id_sql, result_sets, type_sql
from .types import category_for_type

_OBJECT = "SELECT type FROM sys.objects WHERE object_id = {o}"
# An alias type is read through its base type; a CLR type (hierarchyid, geometry, geography) has no base row
_COLUMNS = """
SELECT c.name, t.name, COALESCE(st.name, t.name), t.is_assembly_type, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END,
       c.generated_always_type
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id AND st.is_user_defined = 0
WHERE c.object_id = {o} ORDER BY c.column_id
"""
# Key columns only (not INCLUDE ones); no filtered or disabled index
_KEYS = """
SELECT i.name, i.is_primary_key, c.name
FROM sys.indexes i
JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id
JOIN sys.columns c ON c.object_id = k.object_id AND c.column_id = k.column_id
WHERE i.object_id = {o} AND (i.is_primary_key = 1 OR i.is_unique = 1) AND i.has_filter = 0 AND i.is_disabled = 0
  AND k.is_included_column = 0 AND k.key_ordinal > 0
ORDER BY i.name, k.key_ordinal
"""
_NOT_EXACT = frozenset({"float", "real", "text", "ntext", "image", "xml", "sql_variant"})  # 402, sql_variant: 0 rows
_FRACTIONS = frozenset({"datetime2", "datetimeoffset", "time"})  # the driver reads microseconds: scale 7 never matches
_NON_UNICODE = frozenset({"char", "varchar", "text"})


def _column(
    name: str, type_name: str, base: str, assembly: bool, max_length: int, precision: int, scale: int,
    nullable: bool, identity: bool, computed: bool, default: int, generated_always: int,
) -> EditColumn:
    if computed:
        reason: str | None = "computed column"
    elif identity:
        reason = "IDENTITY column"
    elif base == "timestamp":
        reason = "rowversion column"
    elif generated_always:
        reason = "period column (system-versioned)"
    elif base == "sql_variant" or assembly:
        reason = f"{type_name} values are not edited here"
    else:
        reason = None
    exact = not (base in _NOT_EXACT or assembly or (base in _FRACTIONS and scale > 6))
    return EditColumn(
        name=name, type_name=type_sql(type_name, max_length, precision, scale),
        kind=edit_kind(category_for_type(base)), nullable=bool(nullable),
        has_default=bool(default or identity or computed or generated_always) or base == "timestamp",
        writable=reason is None, read_only_reason=reason, compare="equals" if exact else "none",
        unicode=base not in _NON_UNICODE,
    )


def edit_target(conn: driver.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, columns, keys = result_sets(conn, batch(_OBJECT.format(o=o), _COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
    if object_type != "U":
        return read_only_relation(path, VIEWS if object_type == "V" else "This object is read-only here")
    parts: dict[str, tuple[bool, list[str]]] = {}
    for index, primary, column in keys:
        parts.setdefault(index, (bool(primary), []))[1].append(column)
    candidates = [KeyCandidate(index, tuple(cols), primary) for index, (primary, cols) in parts.items()]
    return build_target(path, [_column(*row) for row in columns], candidates)
```

In `adapters/mssql/adapter.py` (imports: `RowEditing`, `EditTarget`, `from . import catalog, driver, editing, freetds, introspection`): `class MssqlConnection(DatabaseConnection, RowEditing)` with, before `schema_snapshot`:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        self._check_database(path)
        return self._metadata_call(lambda: editing.edit_target(self._conn, path))
```

and

```python
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
    )
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_edits.py tests/test_mssql_edits.py tests/test_api.py tests/test_mysql_types.py tests/test_mssql_types.py tests/test_mysql_adapter.py tests/test_mssql_adapter.py`
Expected: PASS.

- [ ] **Step 6: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green, no `requires_*` test skipped.

```bash
git add backend/dbaguy_backend/adapters/mysql/editing.py backend/dbaguy_backend/adapters/mysql/types.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py backend/dbaguy_backend/adapters/mssql/editing.py \
  backend/dbaguy_backend/adapters/mssql/types.py backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/tests/test_mysql_edits.py backend/tests/test_mssql_edits.py backend/tests/test_api.py
git commit -m "feat(adapters): the edit target for MySQL and SQL Server; MySQL counts the rows an UPDATE matched"
```

---
### Task 7: `EditService`, the plan store, `/edits/target`, `/edits/preview`

**Files:**
- Create: `backend/dbaguy_backend/services/edit_plans.py`, `backend/dbaguy_backend/services/edit_service.py`, `backend/dbaguy_backend/api/edits.py`
- Modify: `backend/dbaguy_backend/domain/errors.py` (`NotEditable`), `backend/dbaguy_backend/services/metadata_service.py` (`_call` uses `path_database`), `backend/dbaguy_backend/container.py` (`AppContainer.edits`), `backend/dbaguy_backend/app.py` (the router)
- Create: `backend/tests/test_edit_plans.py`, `backend/tests/test_edits_api.py`
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`; modify `src/lib/api/types.ts`

**Interfaces:**
- Consumes: Task 3 (`EditChanges`, `EditPlan`, `EditPreview`, `EditTarget`, `PlanUnavailable`, the plan constants, `path_database`); Task 4 (`build_plan`, `DmlPlan`); Tasks 5–6 (`RowEditing`, `read_only_relation`, `row_editing`); `MetadataService.with_connection`; `ReadOnlyProfile` (`read_only`, 403).
- Produces:
  - `domain/errors.NotEditable(ValidationFailed)`, code `not_editable` (422); its message is the target's reason.
  - `services/edit_plans.EditPlanStore(ttl_s=PLAN_TTL_S, capacity=MAX_PLANS, clock=time.monotonic)` with `put(plan: EditPlan) -> None` and `take(plan_id: str, profile_id: UUID, database: str | None) -> EditPlan` (PlanUnavailable `plan_expired` / `plan_mismatch`; a mismatch leaves the plan).
  - `services/edit_service.EditService(metadata, profiles, registry, plans: EditPlanStore | None = None)` with `plans: EditPlanStore`, `target(profile_id: UUID, path: list[PathSegment]) -> EditTarget`, `preview(profile_id: UUID, path: list[PathSegment], changes: EditChanges) -> EditPreview`; `READ_ONLY_REASON = "This connection is read-only"`, `NO_ROW_EDITING = "Rows of this connection cannot be edited in the grid"`.
  - `AppContainer.edits: EditService` (built before the hooks factory: Task 8 hands it `edits.plans`).
  - `POST /edits/target` (`EditTargetRequest{profile_id, path (≥ 1 segment)}` → `EditTarget`), `POST /edits/preview` (`EditPreviewRequest{profile_id, path, changes: EditChanges}` → `EditPreview`); bodies `extra="forbid"`.
  - `src/lib/api/types.ts`: `EditTarget`, `EditColumn`, `EditKind` (`EditColumn["kind"]`), `CellInput`, `RowUpdate`, `RowInsert`, `RowDelete`, `EditChanges`, `EditProblem`, `PlannedStatement`, `EditWarning`, `EditCounts`, `EditPreview` (each from `S[…]` with a one-line comment, as the file's other exports).

Rules (spec §3.4, §4, §5; CR-2; rulings E3, E20):
- `target`: an engine without `row_editing` → `NotEditable(NO_ROW_EDITING)`; a read-only profile → `read_only_relation(path, READ_ONLY_REASON)` without connecting; else the metadata connection of `path_database(path)` answers `edit_target(path)` (a connection that is not `RowEditing` → `NotEditable`). Nothing is cached.
- `preview`: the same engine check; a read-only profile → `ReadOnlyProfile` (403); the target as above (`password_required` 428 passes through untouched, nothing opened); not editable → `NotEditable(target.reason)`; `build_plan` on the request thread; without problems an `EditPlan(plan_id=uuid4().hex, profile_id, database=path_database(path), table, begin, statements, commit, transactional=target.transactional, script)` goes into the store and its id into the answer. A preview writes nothing.
- Logging: INFO `Edit preview for {profile name} on {table}: {d} delete(s), {u} update(s), {i} insert(s), {p} problem(s) in {ms} ms`; DEBUG the target (table name, editable, key). Never a value.

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_edit_plans.py`:

```python
"""The plan store (spec ⑤ D-05-7, CR-2): single use, 15 minutes, the oldest out first, bound to profile and database."""

from uuid import uuid4

import pytest

from dbaguy_backend.domain.edits import MAX_PLANS, PLAN_EXPIRED, PLAN_MISMATCH, PLAN_TTL_S, EditPlan, PlanUnavailable
from dbaguy_backend.services.edit_plans import EditPlanStore

PROFILE, OTHER = uuid4(), uuid4()


def plan(plan_id: str = "p1", profile_id=PROFILE, database: str | None = None) -> EditPlan:
    return EditPlan(plan_id=plan_id, profile_id=profile_id, database=database, table="t", begin="BEGIN", statements=(),
                    commit="COMMIT", transactional=True, script="")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def code_of(store: EditPlanStore, plan_id: str, profile_id=PROFILE, database: str | None = None) -> str:
    with pytest.raises(PlanUnavailable) as raised:
        store.take(plan_id, profile_id, database)
    return raised.value.code


def test_a_plan_is_taken_once_and_only_by_its_profile_and_database():  # Review Focus 4, CR-2
    store = EditPlanStore()
    store.put(plan("p1", database="dbaguy"))
    assert code_of(store, "p1", OTHER, "dbaguy") == PLAN_MISMATCH
    assert code_of(store, "p1", PROFILE, "analytics") == PLAN_MISMATCH  # and it stays for its own session
    assert store.take("p1", PROFILE, "dbaguy").plan_id == "p1"
    assert code_of(store, "p1", PROFILE, "dbaguy") == PLAN_EXPIRED
    assert code_of(store, "never-made") == PLAN_EXPIRED


def test_a_plan_whose_path_names_no_database_goes_to_any_session_of_its_profile():  # ruling E3
    store = EditPlanStore()
    store.put(plan("p2"))
    assert code_of(store, "p2", OTHER, "dbaguy") == PLAN_MISMATCH
    assert store.take("p2", PROFILE, "dbaguy").plan_id == "p2"


def test_a_plan_lasts_fifteen_minutes():
    clock = Clock()
    store = EditPlanStore(clock=clock)
    store.put(plan("p1"))
    store.put(plan("p2"))
    clock.now += PLAN_TTL_S - 1
    assert store.take("p1", PROFILE, None).plan_id == "p1"
    clock.now += 1
    assert code_of(store, "p2") == PLAN_EXPIRED


def test_the_oldest_plans_make_room():
    store = EditPlanStore()
    for i in range(MAX_PLANS + 1):
        store.put(plan(f"p{i}"))
    assert code_of(store, "p0") == PLAN_EXPIRED
    assert store.take(f"p{MAX_PLANS}", PROFILE, None).plan_id == f"p{MAX_PLANS}"
```

`backend/tests/test_edits_api.py`:

```python
"""/edits/target and /edits/preview over HTTP (spec ⑤ §3.4, §4): the routes, their refusals, the plan they leave and
what reaches the log. SQLite files only: no dev database needed."""

import re
import sqlite3

import pytest
from loguru import logger

from tests.test_api import AUTH, client  # noqa: F401  (fixture)


def path(name: str, kind: str = "table") -> list[dict]:
    folder = {"table": "tables", "view": "views"}[kind]
    return [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": folder}, {"kind": kind, "name": name}]


def post(client, route: str, **body):
    return client.post(f"/edits/{route}", json=body, headers=AUTH)


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def value(text: str) -> dict:
    return {"kind": "value", "text": text}


ONE_DELETE = {"deletes": [{"id": "d:1", "key": {"id": 1}}]}


@pytest.fixture
def lite(client, tmp_path):
    file = tmp_path / "edits.db"
    conn = sqlite3.connect(file)
    conn.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT NOT NULL, price REAL,
                            made DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE loose (a TEXT, b TEXT);
        CREATE VIEW items_v AS SELECT * FROM items;
        INSERT INTO items (id, label, price) VALUES (1, 'a', 1.5), (2, 'b', 2.5);
    """)
    conn.close()
    body = {"profile": {"engine": "sqlite", "name": "lite", "path": str(file)}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"], file


def test_the_target_of_a_table_a_view_and_a_table_without_a_key(client, lite):
    pid, _ = lite
    target = post(client, "target", profile_id=pid, path=path("items")).json()
    assert (target["editable"], target["key"], target["key_source"]) == (True, ["id"], "primary_key")
    assert {c["name"]: (c["kind"], c["compare"], c["has_default"]) for c in target["columns"]} == {
        "id": ("number", "equals", True), "label": ("text", "equals", False), "price": ("number", "none", False),
        "made": ("datetime", "equals", True)}
    assert post(client, "target", profile_id=pid, path=path("items_v", "view")).json()["reason"] == (
        "Views are read-only here")
    assert post(client, "target", profile_id=pid, path=path("loose")).json()["editable"] is False


def test_a_preview_shows_the_statements_and_leaves_a_plan(client, lite):  # spec §3.4, CR-2
    pid, file = lite
    changes = {
        "updates": [{"id": "u:1", "key": {"id": 1}, "set": {"label": value("A"), "price": value("3")},
                     "original": {"label": "a", "price": 1.5}}],
        "inserts": [{"id": "i:1", "values": {"label": value("c")}}],
        "deletes": [{"id": "d:2", "key": {"id": 2}}],
    }
    preview = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert re.fullmatch(r"[0-9a-f]{32}", preview["plan_id"])
    assert (preview["begin"], preview["commit"], preview["problems"]) == ("BEGIN IMMEDIATE", "COMMIT", [])
    assert [(s["change_id"], s["sql"]) for s in preview["statements"]] == [
        ("d:2", "DELETE FROM main.items WHERE id = 2"),
        ("u:1", "UPDATE main.items SET label = 'A', price = 3 WHERE id = 1 AND label = 'a'"),
        ("i:1", "INSERT INTO main.items (label) VALUES ('c')"),
    ]
    assert preview["statements"][1]["unchecked"] == [{"column": "price", "reason": "REAL values cannot be compared exactly"}]
    assert [w["code"] for w in preview["warnings"]] == ["unchecked_columns"]
    assert preview["counts"] == {"updates": 1, "inserts": 1, "deletes": 1}
    assert preview["script"] == (
        "BEGIN IMMEDIATE;\n" + "".join(f"{s['sql']};\n" for s in preview["statements"]) + "COMMIT;")
    again = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert again["plan_id"] != preview["plan_id"]  # a preview writes nothing and can be asked again
    assert sqlite3.connect(file).execute("SELECT id, label FROM items ORDER BY id").fetchall() == [(1, "a"), (2, "b")]


def test_problems_come_back_per_cell_without_a_plan(client, lite):  # spec §3.3.3
    pid, _ = lite
    changes = {"updates": [{"id": "u:1", "key": {"id": 1},
                            "set": {"label": {"kind": "null"}, "price": value("cheap"), "gone": value("x")},
                            "original": {"label": "a", "price": 1.5, "gone": "x"}}]}
    preview = post(client, "preview", profile_id=pid, path=path("items"), changes=changes).json()
    assert (preview["plan_id"], preview["statements"], preview["script"]) == (None, [], "")
    assert sorted((p["change_id"], p["column"], p["code"]) for p in preview["problems"]) == [
        ("u:1", "gone", "unknown_column"), ("u:1", "label", "not_nullable"), ("u:1", "price", "invalid_number")]


def test_refusals(client, lite):  # spec §3.4, §4
    pid, file = lite
    read_only = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "ro", "path": str(file),
                                                           "read_only": True}}, headers=AUTH).json()["profile"]["id"]
    target = post(client, "target", profile_id=read_only, path=path("items")).json()
    assert (target["editable"], target["reason"]) == (False, "This connection is read-only")
    assert error_of(post(client, "preview", profile_id=read_only, path=path("items"), changes=ONE_DELETE)) == (
        403, "read_only")
    view = post(client, "preview", profile_id=pid, path=path("items_v", "view"), changes=ONE_DELETE)
    assert error_of(view) == (422, "not_editable") and view.json()["error"]["message"] == "Views are read-only here"
    couchbase = client.post("/profiles", json={"profile": {"engine": "couchbase", "name": "cb", "host": "127.0.0.1"}},
                            headers=AUTH).json()["profile"]["id"]
    assert error_of(post(client, "target", profile_id=couchbase, path=path("items"))) == (422, "not_editable")
    locked = client.post("/profiles", json={"profile": {
        "engine": "postgres", "name": "pg", "host": "127.0.0.1", "port": 1, "username": "u", "save_password": False,
    }}, headers=AUTH).json()["profile"]["id"]
    pg_path = [{"kind": "database", "name": "db"}, {"kind": "schema", "name": "public"},
               {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "t"}]
    assert error_of(post(client, "preview", profile_id=locked, path=pg_path, changes=ONE_DELETE)) == (
        428, "password_required")  # nothing was opened: the unlock dialog, then the same preview again
    assert error_of(post(client, "preview", profile_id=pid, path=path("items"), changes={})) == (
        422, "validation_failed")  # nothing to commit


def test_values_never_reach_the_info_log(client, lite):  # spec §5
    pid, _ = lite
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        changes = {"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"label": value("zz-marker-new")},
                                "original": {"label": "zz-marker-old"}}]}
        post(client, "preview", profile_id=pid, path=path("items"), changes=changes)
    finally:
        logger.remove(sink)
    assert not [text for level, text in records if level >= 20 and "zz-marker" in text]
    assert [text for level, text in records if level >= 20 and text.startswith("Edit preview for lite on main.items")]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_edit_plans.py tests/test_edits_api.py`
Expected: FAIL (ModuleNotFoundError: `dbaguy_backend.services.edit_plans`).

- [ ] **Step 3: The store** — `backend/dbaguy_backend/services/edit_plans.py`:

```python
"""The previewed plans a session may apply (spec ⑤ D-05-7, R-05-7, CR-2): in memory, at most MAX_PLANS (the oldest made
goes first), each for PLAN_TTL_S, taken once and only by a session of its profile and — when its path names one — its
database (ruling E3). A backend restart forgets them all: the client previews again."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from uuid import UUID

from ..domain.edits import (
    MAX_PLANS, PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE, PLAN_TTL_S, EditPlan,
    PlanUnavailable,
)


class EditPlanStore:
    def __init__(
        self, ttl_s: float = PLAN_TTL_S, capacity: int = MAX_PLANS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._clock = clock
        self._plans: OrderedDict[str, tuple[float, EditPlan]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, plan: EditPlan) -> None:
        with self._lock:
            self._prune()
            self._plans[plan.plan_id] = (self._clock(), plan)
            self._plans.move_to_end(plan.plan_id)
            while len(self._plans) > self._capacity:
                self._plans.popitem(last=False)

    def take(self, plan_id: str, profile_id: UUID, database: str | None) -> EditPlan:
        """The plan, removed: it runs once. PlanUnavailable(plan_expired) when there is none (expired, made room for,
        taken already, never made); PlanUnavailable(plan_mismatch) when it is another profile's or another database's,
        and it then stays for its own session."""
        with self._lock:
            self._prune()
            entry = self._plans.get(plan_id)
            if entry is None:
                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
            plan = entry[1]
            if plan.profile_id != profile_id or (plan.database is not None and plan.database != database):
                raise PlanUnavailable(PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE)
            del self._plans[plan_id]
            return plan

    def _prune(self) -> None:
        now = self._clock()
        for plan_id in [plan_id for plan_id, (made, _) in self._plans.items() if now - made >= self._ttl_s]:
            del self._plans[plan_id]
```

- [ ] **Step 4: The service** — `backend/dbaguy_backend/services/edit_service.py`:

```python
"""Grid editing's preview (spec ⑤ §3.4): the table's EditTarget read on the profile's metadata connection (a read: its
one reconnect is fine), the plan built and checked on the request thread (`sql/dml.py`, pure: checking 10,000 changes
never holds up the tree or completion), and a one-time EditPlan for the tab's own session (`EditPlanStore`). A preview
writes nothing and can be asked again; nothing is cached (each preview reads the target again, so DDL is never missed).

Logs: INFO each preview's profile, table, counts, problem count and time; DEBUG the target read. Never a value or a key."""

import time
from uuid import UUID, uuid4

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, RowEditing
from ..adapters.editing import read_only_relation
from ..adapters.registry import AdapterRegistry
from ..domain.edits import EditChanges, EditPlan, EditPreview, EditTarget
from ..domain.errors import NotEditable, ReadOnlyProfile
from ..domain.metadata import PathSegment, path_database
from ..domain.profiles import AnyProfile
from ..sql.dml import build_plan
from .edit_plans import EditPlanStore
from .metadata_service import MetadataService
from .profile_service import ProfileService

READ_ONLY_REASON = "This connection is read-only"
NO_ROW_EDITING = "Rows of this connection cannot be edited in the grid"


class EditService:
    def __init__(
        self, metadata: MetadataService, profiles: ProfileService, registry: AdapterRegistry,
        plans: EditPlanStore | None = None,
    ) -> None:
        self._metadata = metadata
        self._profiles = profiles
        self._registry = registry
        self.plans = plans or EditPlanStore()

    def target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        """How the rows of the table at `path` are identified and written. A read-only profile is answered without
        connecting: not editable, with the reason the toolbar shows (spec G8)."""
        profile, _ = self._admit(profile_id)
        if profile.read_only:
            return read_only_relation(path, READ_ONLY_REASON)
        return self._read_target(profile_id, path)

    def preview(self, profile_id: UUID, path: list[PathSegment], changes: EditChanges) -> EditPreview:
        """The exact statements a commit of `changes` sends, and a plan id when nothing is wrong with them."""
        started = time.monotonic()
        profile, adapter = self._admit(profile_id)
        if profile.read_only:
            raise ReadOnlyProfile()
        target = self._read_target(profile_id, path)
        if not target.editable:
            raise NotEditable(target.reason or NO_ROW_EDITING)
        built = build_plan(adapter.dialect, target, changes)
        plan_id = None
        if not built.problems:
            plan = EditPlan(
                plan_id=uuid4().hex, profile_id=profile_id, database=path_database(path), table=built.table,
                begin=built.begin, statements=tuple(built.statements), commit=built.commit,
                transactional=target.transactional, script=built.script,
            )
            self.plans.put(plan)
            plan_id = plan.plan_id
        counts = built.counts
        logger.info(
            "Edit preview for {} on {}: {} delete(s), {} update(s), {} insert(s), {} problem(s) in {} ms",
            profile.name, built.table, counts.deletes, counts.updates, counts.inserts, len(built.problems),
            int((time.monotonic() - started) * 1000),
        )
        return EditPreview(
            plan_id=plan_id, begin=built.begin, statements=built.statements, commit=built.commit,
            problems=built.problems, warnings=built.warnings, counts=counts, script=built.script,
        )

    def _admit(self, profile_id: UUID) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not adapter.capabilities.row_editing:
            raise NotEditable(NO_ROW_EDITING)
        return profile, adapter

    def _read_target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        def read(connection: DatabaseConnection) -> EditTarget:
            if not isinstance(connection, RowEditing):
                raise NotEditable(NO_ROW_EDITING)
            return connection.edit_target(path)

        target = self._metadata.with_connection(profile_id, path_database(path), read)
        logger.debug("Edit target of {}: editable={}, key {}", path[-1].name if path else "-", target.editable,
                     target.key)
        return target
```

In `domain/errors.py`, after `ValidationFailed`'s subclasses:

```python
class NotEditable(ValidationFailed):
    """Grid editing refused for this table or engine (spec ⑤ §3.4): the message is the target's reason."""

    code = "not_editable"
```

In `services/metadata_service.py`, `_call` becomes `return self.with_connection(request.profile_id, path_database(request.path), fn)` (import `path_database`; `NodeKind` is no longer needed there).

- [ ] **Step 5: The routes and the wiring** — `backend/dbaguy_backend/api/edits.py`:

```python
"""Grid editing (spec ⑤ §3.4): a table's edit target and the preview of a commit. The commit itself is the session's
`apply_edits` WebSocket command."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.edits import EditChanges, EditPreview, EditTarget
from ..domain.metadata import PathSegment
from .deps import get_container, require_token

router = APIRouter(prefix="/edits", tags=["edits"], dependencies=[Depends(require_token)])


class EditTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    path: list[PathSegment] = Field(min_length=1)  # the table's explorer path, as the table-data tab opened it


class EditPreviewRequest(EditTargetRequest):
    changes: EditChanges


@router.post("/target", response_model=EditTarget)
def edit_target(body: EditTargetRequest, container: AppContainer = Depends(get_container)) -> EditTarget:
    return container.edits.target(body.profile_id, body.path)


@router.post("/preview", response_model=EditPreview)
def edit_preview(body: EditPreviewRequest, container: AppContainer = Depends(get_container)) -> EditPreview:
    return container.edits.preview(body.profile_id, body.path, body.changes)
```

`app.py` imports `edits` with the other routers and calls `app.include_router(edits.router)` after `couchbase`; `container.py` builds `self.edits = EditService(self.metadata, self.profiles, self.registry)` right after `self.documents`.

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_edit_plans.py tests/test_edits_api.py tests/test_metadata_service.py tests/test_container.py tests/test_api.py`
Expected: PASS.

- [ ] **Step 7: Regenerate the API types and add the exports**

Run: `npm run gen:api && npm run typecheck && npm run build`
Expected: `schema.d.ts` gains `/edits/target`, `/edits/preview` and the edit schemas; typecheck and build pass with the new exports in `src/lib/api/types.ts`.

- [ ] **Step 8: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/services/edit_plans.py backend/dbaguy_backend/services/edit_service.py \
  backend/dbaguy_backend/api/edits.py backend/dbaguy_backend/domain/errors.py \
  backend/dbaguy_backend/services/metadata_service.py backend/dbaguy_backend/container.py backend/dbaguy_backend/app.py \
  backend/tests/test_edit_plans.py backend/tests/test_edits_api.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts
git commit -m "feat(api): /edits/target and /edits/preview — the exact DML of a grid commit, a one-time plan"
```

---
### Task 8: The worker's apply, the WebSocket protocol, hooks, History

**Files:**
- Modify: `backend/dbaguy_backend/domain/execution.py` (`ApplyEditsCommand`, `EditOutcome`, `EditOutcomeEvent`, both unions)
- Create: `backend/dbaguy_backend/sessions/edit_apply.py`
- Modify: `backend/dbaguy_backend/sessions/worker.py`, `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/container.py`
- Create: `backend/tests/test_edit_apply.py`, `backend/tests/test_edit_worker.py`
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`; modify `src/lib/api/types.ts`

**Interfaces:**
- Consumes: Task 3 (`EditPlan`, `PlannedStatement`, `PlanUnavailable`, `PLAN_EXPIRED`, `PLAN_EXPIRED_MESSAGE`); Task 5 (`StatementResult.matched_rows()`); Task 7 (`EditPlanStore.take`, `AppContainer.edits.plans`); the worker's `_run_statement` (armed cancel window), `_start_request`, `_complete`, `_set_running`, `_check_text`, `_read_only_message`; `adapters/base.transaction_open_error`.
- Produces:
  - `domain/execution.py`: `ApplyEditsCommand{type: "apply_edits", request_id, plan_id (1–64 chars)}` in `ClientCommand`; `EditOutcome = Literal["committed", "rolled_back", "partial", "unknown"]`; `EditOutcomeEvent{type: "edit_outcome", request_id, outcome, applied: list[str], already_gone: list[str], failed_change: str | None, statement: int | None}` in `ServerEvent`.
  - `sessions/edit_apply.py`: `EditReport`, `EditProgress` (`matched`, `failed`, `cancelled_before_commit`, `aborted_before_commit`, `ended_before_commit`, `commit_failed`, `committed`, `broke`), `refused(error) -> EditReport`, `history_error(report, total) -> StatementError | None`; codes `CONNECTION_LOST`, `EDIT_CONFLICT`, `ROW_COUNT_UNKNOWN`, `OUTCOME_UNKNOWN`, `TRANSACTION_FAILED`.
  - `sessions/worker.py`: `WorkerHooks.take_edit_plan: Callable[[str], EditPlan]` (default: always `plan_expired`); `SessionWorker.submit` accepts `ApplyEditsCommand`; `_start_request(req, *, before_finished=None)`; `_apply_edits`, `_take_plan`, `_check_plan`, `_run_plan`, `_edit_statement`, `_rollback_quietly`, `_end_apply`; `_forward_messages(request_id, result)` (and `_send_messages` uses it).
  - `sessions/hooks.SessionHooksFactory(registry, profiles, policy, history, schema_cache, plans: EditPlanStore | None = None)`: its hooks' `take_edit_plan(plan_id)` is `plans.take(plan_id, profile_id, database)` (this session's profile and database, CR-2).
  - `src/lib/api/types.ts`: `ApplyEditsCommand`, `EditOutcomeEvent`, `EditOutcome` (`EditOutcomeEvent["outcome"]`).

Rules (spec §3.5, R-05-4…R-05-7, D-05-5, D-05-8, D-05-9; rulings E1, E9–E14):
- One `_apply_edits` per command, a worker request like an execute (`_submit_request`; queued → a Cancel only marks it). First `_discard_open()` (the grid's paused result ends with its own `finished`). Every apply ends with exactly one `edit_outcome` before its `session_state` and `finished`; one skipped before it started ends `started`, `message`, `edit_outcome(rolled_back)`, `session_state`, `finished(cancelled)` (E1).
- Before anything is sent: the plan (`plan_expired` / `plan_mismatch` → `rolled_back`), `_check_text` on every text, the policy on every DML statement with `confirmed=True` (BLOCK → `read_only`), `in_transaction()` (→ `transaction_open`). About 0.4 ms per statement for the policy (measured while planning: 0.35–0.5 s per 1,000).
- Then BEGIN, each statement (each through `_run_statement`, so a Cancel reaches the running statement and one landing between statements stops the next before it is sent), `matched_rows()` read first and the answer read to its end (E9), `affected(index, rows)` when the count is known, `EditProgress.matched` deciding (R-05-5). `_set_running(None)` before COMMIT (R-05-6: no Cancel reaches the driver after this); for a transactional target a Cancel that landed → rollback → rolled_back, `transaction_failed()` → rollback → rolled_back, `not in_transaction()` → unknown (E12). COMMIT: refused → rollback (SQLite keeps its transaction after a busy COMMIT, A14) → rolled_back; lost → unknown, never resent; a non-transactional target is committed whatever COMMIT says (E11). Every rollback goes through `_rollback_quietly` (running cleared first; a failure is logged with its code only).
- Events: `started` → `affected` × n → [`error` — or, for a commit that stood, a `message` warning] → `edit_outcome` → `session_state` → `finished`. History through `_complete` with `req.sql = plan.script` and `req.rows` = matched rows; only once BEGIN was sent (`req.ran`); `history_error` (E14). Log: INFO `[{session}] apply {rid}: {n} statement(s) on {table} → {outcome} in {ms} ms ({code})`, WARNING for unknown, partial and an unreadable count; a conflict's statement index, kind and count at INFO; never a key or a value.

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_edit_apply.py` (pure: one test per row of spec §3.5's outcome table):

```python
"""The outcome of an apply (spec ⑤ §3.5's table, R-05-5, R-05-6, D-05-5, D-05-6; rulings E11, E12, E14): one test per
row, pure."""

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.domain.edits import PlannedStatement
from dbaguy_backend.sessions.edit_apply import EditProgress, EditReport, history_error, refused

LOST = StatementError("Connection to the server was lost", code="connection_lost")
SERVER = StatementError("duplicate key value violates unique constraint", code="23505")


def progress(*kinds: str, transactional: bool = True) -> EditProgress:
    statements = [PlannedStatement(change_id=f"c{i}", kind=kind, sql=f"{kind} {i}", row=f"id = {i}")
                  for i, kind in enumerate(kinds)]
    started = EditProgress(statements, transactional)
    started.sent = True
    return started


def run(started: EditProgress, *rows: int | None) -> StatementError | None:
    """Statement i matched rows[i], until one stops the apply (its error) or all ran (None)."""
    for index, count in enumerate(rows):
        started.index = index
        if (error := started.matched(count)) is not None:
            return error
    return None


def test_every_statement_matching_as_planned_commits():
    started = progress("delete", "update", "insert")
    assert run(started, 0, 1, 1) is None
    report = started.committed()
    assert (report.outcome, report.applied, report.already_gone, report.error) == (
        "committed", ["c0", "c1", "c2"], ["c0"], None)


@pytest.mark.parametrize(("kind", "rows", "code", "text"), [
    ("update", 0, "edit_conflict",
     "Row id = 0 was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied"),
    ("update", 2, "edit_conflict", "Row id = 0 matched 2 rows: its key is not unique. Nothing was applied"),
    ("delete", 2, "edit_conflict", "Row id = 0 matched 2 rows: its key is not unique. Nothing was applied"),
    ("insert", 0, "edit_conflict", "The new row was not inserted (a trigger may have skipped it): nothing was applied"),
    ("insert", 2, "edit_conflict", "Inserting one row reported 2 rows: nothing was applied"),
    ("update", None, "row_count_unknown", "Could not confirm how many rows changed: nothing was applied"),
    ("delete", -1, "row_count_unknown", "Could not confirm how many rows changed: nothing was applied"),
])
def test_a_statement_that_matched_wrongly_stops_the_apply(kind, rows, code, text):  # R-05-5, D-05-5, spec §4
    started = progress(kind)
    error = run(started, rows)
    assert (error.code, error.message) == (code, text)
    report = started.failed(error)
    assert (report.outcome, report.failed_change, report.statement, report.applied) == ("rolled_back", "c0", 0, [])


@pytest.mark.parametrize("error", [SERVER, LOST, StatementCancelled("canceling statement due to user request")])
def test_any_failure_before_the_commit_rolls_everything_back(error):  # R-05-6
    started = progress("update", "update")
    run(started, 1)
    started.index = 1
    report = started.failed(error)
    assert (report.outcome, report.failed_change, report.statement, report.applied) == ("rolled_back", "c1", 1, [])
    if isinstance(error, StatementCancelled):
        assert (report.error.code, report.error.message) == ("cancelled", "Cancelled: nothing was applied")


def test_refused_before_anything_was_sent():
    report = refused(StatementError("The preview has expired: preview the changes again", code="plan_expired"))
    assert (report.outcome, report.applied, report.error.code) == ("rolled_back", [], "plan_expired")
    assert refused(StatementCancelled()).error.message == "Cancelled: nothing was applied"


def test_before_the_commit_a_cancel_or_a_failed_transaction_rolls_back_and_a_vanished_one_is_unknown():  # ruling E12
    started = progress("update")
    run(started, 1)
    cancelled, aborted, vanished = (started.cancelled_before_commit(), started.aborted_before_commit(),
                                    started.ended_before_commit())
    assert (cancelled.outcome, cancelled.error.message) == ("rolled_back", "Cancelled: nothing was applied")
    assert (aborted.outcome, aborted.error.code) == ("rolled_back", "transaction_failed")
    assert (vanished.outcome, vanished.error.code) == ("unknown", "outcome_unknown")


def test_a_commit_the_server_refuses_rolls_back_and_a_lost_one_is_unknown():  # R-05-6, A13
    started = progress("update")
    run(started, 1)
    refusal = started.commit_failed(StatementError("insert or update violates foreign key constraint", code="23503"))
    assert (refusal.outcome, refusal.statement, refusal.applied, refusal.error.code) == ("rolled_back", None, [], "23503")
    assert refusal.error.message == (
        "insert or update violates foreign key constraint\nThe transaction was rolled back: nothing was applied")
    lost = started.commit_failed(LOST)
    assert (lost.outcome, lost.applied, lost.error) == ("unknown", [], LOST)


def test_a_table_that_cannot_roll_back_says_what_took_effect():  # D-05-6, ruling E11
    started = progress("update", "update", "update", transactional=False)
    error = run(started, 1, 0)
    assert error.message.endswith(": the statements before it stay applied (this table cannot roll back)")
    partial = started.failed(error)
    assert (partial.outcome, partial.applied, partial.failed_change, partial.statement) == ("partial", ["c0"], "c1", 1)
    first = progress("update", "update", transactional=False)
    assert first.failed(run(first, 0)).outcome == "rolled_back"  # nothing took effect
    lost = progress("update", "update", transactional=False)
    run(lost, 1)
    lost.index = 1
    assert (lost.failed(LOST).outcome, lost.failed(LOST).applied) == ("unknown", ["c0"])
    done = progress("update", transactional=False)
    run(done, 1)
    stood = done.commit_failed(SERVER)
    assert (stood.outcome, stood.applied, stood.error) == ("committed", ["c0"], SERVER)


def test_a_bug_is_an_unknown_outcome_once_something_went_out():  # R-05-6
    assert EditProgress([], True).broke(RuntimeError("bug")).outcome == "rolled_back"
    report = progress("update").broke(RuntimeError("bug"))
    assert (report.outcome, report.error.message) == ("unknown", "Internal error: bug")


def test_history_keeps_ok_cancelled_or_the_error():  # D-05-9, ruling E14
    started = progress("update", "update", transactional=False)
    partial = started.failed(run(started, 1, 0))
    assert history_error(progress("update").committed(), 1) is None
    assert history_error(EditReport("rolled_back", error=StatementCancelled("Cancelled: nothing was applied")), 1) is None
    assert history_error(EditReport("unknown", error=LOST), 1).message == (
        "Outcome unknown: Connection to the server was lost")
    assert history_error(partial, 2).message.startswith("Partly applied (1 of 2): Row id = 1 was changed")
    assert history_error(EditReport("rolled_back", error=SERVER), 1) is SERVER
```

`backend/tests/test_edit_worker.py` (the worker on a fake connection):

```python
"""The worker's apply (spec ⑤ §3.5) on a fake connection, so every race is forced: the event order, each outcome, what
is sent and never sent, what a Cancel reaches, History, the log, and the hooks' binding of plans to sessions. The live
engines follow in test_edit_flows.py."""

import threading
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.edits import (
    PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, EditPlan, PlannedStatement, PlanUnavailable,
)
from dbaguy_backend.domain.execution import ApplyEditsCommand, ExecuteCommand, MessageSeverity, ServerMessage
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.edit_plans import EditPlanStore
from dbaguy_backend.services.history_service import HistoryService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.services.safety import GuardAction, GuardDecision, SafetyPolicy
from dbaguy_backend.sessions.hooks import SessionHooksFactory
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_session_wiring import FakeCache
from tests.test_sessions import Recorder, finished, paused

BLOCK = object()
PROFILE = uuid4()
LOST = StatementError("Connection to the server was lost", code="connection_lost")


class Counted(FakeResult):
    """One plan statement's answer: `rows` matched (None: no count), and the messages it brought."""

    def __init__(self, rows: int | None, messages=()) -> None:
        super().__init__(0)
        self._matched = rows
        self._messages = list(messages)

    def columns(self):
        return None

    def affected_rows(self):
        return self._matched

    def take_messages(self):
        taken, self._messages = self._messages, []
        return taken


class EditFake(FakeConnection):
    """Answers a plan's statements by their text from `answers`: an int or None (rows matched), a Counted, an exception
    (raised), BLOCK (waits for a cancel, then StatementCancelled) or a callable (called first; its result is the
    answer); any other text matches one row, and `rows N` gives N rows to page through. `BEGIN` opens the transaction;
    `COMMIT` ends it — after `commit_gate`, when set — or raises `commit_error` (any but connection_lost ends the
    transaction too, as PostgreSQL after a deferred-constraint failure)."""

    def __init__(self, answers=None, commit_error=None, commit_gate=None) -> None:
        super().__init__()
        self.answers = answers or {}
        self.commit_error = commit_error
        self.commit_gate = commit_gate
        self.committing = threading.Event()

    def statement(self, sql, options):
        self.runs.append(sql)
        if sql.startswith("rows "):
            return FakeResult(int(sql.split()[1]))
        if sql == "BEGIN":
            self.tx = True
            return Counted(None)
        if sql == "COMMIT":
            self.committing.set()
            if self.commit_gate is not None:
                assert self.commit_gate.wait(10)
            if self.commit_error is not None:
                if self.commit_error.code != "connection_lost":
                    self.tx = False
                raise self.commit_error
            self.tx = False
            self.commits += 1
            return Counted(None)
        answer = self.answers.get(sql, 1)
        if callable(answer):
            answer = answer()
        if answer is BLOCK:
            self.blocked.set()
            assert self._unblock.wait(10), "the blocked statement was never cancelled"
            raise StatementCancelled()
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, Counted) else Counted(answer)


def make_plan(*statements: tuple[str, str], transactional: bool = True, plan_id: str = "p1") -> EditPlan:
    planned = tuple(PlannedStatement(change_id=f"c{i}", kind=kind, sql=sql, row=f"id = {i}")
                    for i, (kind, sql) in enumerate(statements))
    script = "BEGIN;\n" + "".join(f"{s.sql};\n" for s in planned) + "COMMIT;"
    return EditPlan(plan_id=plan_id, profile_id=PROFILE, database=None, table="t", begin="BEGIN", statements=planned,
                    commit="COMMIT", transactional=transactional, script=script)


THREE = (("delete", "d0"), ("update", "u1"), ("insert", "i2"))


def writes(sql, confirmed):
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.WRITE))


def read_only(sql, confirmed):
    return GuardDecision(action=GuardAction.BLOCK, classification=Classification(kind=StatementKind.WRITE))


class Env:
    def __init__(self, conn: EditFake, *plans: EditPlan, check=writes) -> None:
        self.conn = conn
        self.records = []
        self.plans = {plan.plan_id: plan for plan in plans}
        lease = FakeLease(conn)
        self.worker = SessionWorker("edits", lambda: lease)
        self.worker.start()
        self.worker.configure(WorkerHooks(check=check, on_record=self.records.append, take_edit_plan=self._take))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def _take(self, plan_id: str) -> EditPlan:
        plan = self.plans.pop(plan_id, None)
        if plan is None:
            raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
        return plan

    def submit(self, plan_id: str = "p1", rid: str = "a") -> None:
        self.worker.submit(ApplyEditsCommand(request_id=rid, plan_id=plan_id))

    def events(self, rid: str = "a") -> list:
        """The request's events, from its `started` to its `finished`, with the session_state among them."""
        self.recorder.wait_for(finished(rid))
        events = list(self.recorder.events)
        start = next(i for i, e in enumerate(events) if e.type == "started" and e.request_id == rid)
        end = next(i for i, e in enumerate(events) if e.type == "finished" and e.request_id == rid)
        return events[start:end + 1]

    def apply(self, plan_id: str = "p1", rid: str = "a") -> list:
        self.submit(plan_id, rid)
        return self.events(rid)


@pytest.fixture
def env_of():
    made: list[Env] = []

    def make(conn: EditFake, *plans: EditPlan, **fields) -> Env:
        env = Env(conn, *plans, **fields)
        made.append(env)
        return env

    yield make
    for env in made:
        env.worker.close()


def outcome(events):
    return next(e for e in events if e.type == "edit_outcome")


def error(events):
    return next((e for e in events if e.type == "error"), None)


def types(events) -> list[str]:
    return [e.type for e in events]


def test_a_commit_sends_the_plan_and_only_the_plan(env_of):  # R-05-7, spec §3.5 event order
    env = env_of(EditFake({"d0": 0}), make_plan(*THREE))
    events = env.apply()
    assert types(events) == ["started", "affected", "affected", "affected", "edit_outcome", "session_state", "finished"]
    assert [(e.index, e.rows) for e in events if e.type == "affected"] == [(0, 0), (1, 1), (2, 1)]
    done = outcome(events)
    assert (done.outcome, done.applied, done.already_gone, done.failed_change, done.statement) == (
        "committed", ["c0", "c1", "c2"], ["c0"], None, None)
    assert env.conn.runs == ["BEGIN", "d0", "u1", "i2", "COMMIT"] and env.conn.commits == 1 and not env.conn.tx
    [record] = env.records
    assert (record.sql, record.status, record.rows, record.error) == (make_plan(*THREE).script, "ok", 2, None)
    assert not events[-1].in_transaction and not events[-1].cancelled


@pytest.mark.parametrize(("answer", "code"), [
    (StatementError("duplicate key value", code="23505"), "23505"), (0, "edit_conflict"), (2, "edit_conflict"),
    (None, "row_count_unknown"), (LOST, "connection_lost"),
])
def test_a_statement_that_fails_or_matches_wrongly_rolls_everything_back(env_of, answer, code):  # R-05-5, R-05-6
    env = env_of(EditFake({"u1": answer}), make_plan(*THREE))
    events = env.apply()
    counted = ["affected", "affected"] if isinstance(answer, int) else ["affected"]
    assert types(events) == ["started", *counted, "error", "edit_outcome", "session_state", "finished"]
    assert error(events).code == code
    done = outcome(events)
    assert (done.outcome, done.applied, done.failed_change, done.statement) == ("rolled_back", [], "c1", 1)
    assert env.conn.runs == ["BEGIN", "d0", "u1"] and env.conn.commits == 0 and env.conn.rollbacks == 1
    assert env.records[0].status == "error"


def test_cancel_while_a_statement_waits_rolls_back(env_of):  # spec §6, R-05-6
    env = env_of(EditFake({"u1": BLOCK}), make_plan(*THREE))
    env.submit()
    assert env.conn.blocked.wait(5)
    env.worker.cancel("a")
    events = env.events()
    assert (error(events).code, error(events).message) == ("cancelled", "Cancelled: nothing was applied")
    assert (outcome(events).outcome, outcome(events).failed_change) == ("rolled_back", "c1")
    assert events[-1].cancelled and env.conn.runs == ["BEGIN", "d0", "u1"] and env.conn.rollbacks == 1
    assert env.records[0].status == "cancelled"


def test_a_cancel_that_lands_before_the_commit_rolls_back(env_of):  # R-05-6, ruling E12
    env = env_of(EditFake(), make_plan(("update", "u0")))
    env.conn.answers["u0"] = lambda: (env.worker.cancel("a"), 1)[1]  # nothing for the driver to stop by then
    events = env.apply()
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "cancelled")
    assert env.conn.runs == ["BEGIN", "u0"] and env.conn.commits == 0 and env.conn.rollbacks == 1


def test_a_cancel_after_the_commit_went_out_never_reaches_the_driver(env_of):  # R-05-6
    gate = threading.Event()
    env = env_of(EditFake(commit_gate=gate), make_plan(("update", "u0")))
    env.submit()
    assert env.conn.committing.wait(5)
    env.worker.cancel("a")
    gate.set()
    events = env.events()
    assert outcome(events).outcome == "committed" and env.conn.cancel_calls == 0 and not events[-1].cancelled


def test_a_commit_the_server_refuses_is_rolled_back(env_of):  # R-05-6 (a deferred constraint, A13)
    refusal = StatementError("insert or update violates foreign key constraint", code="23503")
    env = env_of(EditFake(commit_error=refusal), make_plan(("update", "u0")))
    events = env.apply()
    assert (outcome(events).outcome, outcome(events).statement, error(events).code) == ("rolled_back", None, "23503")
    assert error(events).message.endswith("\nThe transaction was rolled back: nothing was applied")
    assert env.conn.runs == ["BEGIN", "u0", "COMMIT"]


def test_a_commit_whose_answer_is_lost_is_unknown_and_never_sent_again(env_of):  # R-05-6
    env = env_of(EditFake(commit_error=LOST), make_plan(("update", "u0")))
    events = env.apply()
    assert (outcome(events).outcome, outcome(events).applied, error(events).code) == ("unknown", [], "connection_lost")
    assert env.conn.runs.count("COMMIT") == 1 and env.conn.rollbacks == 0
    assert env.records[0].error == "Outcome unknown: Connection to the server was lost"


def test_a_table_that_cannot_roll_back_says_what_took_effect(env_of):  # D-05-6, ruling E11
    env = env_of(EditFake({"u1": 0}), make_plan(*THREE, transactional=False),
                 make_plan(*THREE, transactional=False, plan_id="p2"))
    done = outcome(env.apply())
    assert (done.outcome, done.applied, done.already_gone, done.failed_change) == ("partial", ["c0"], [], "c1")
    env.conn.answers["u1"] = LOST
    lost = outcome(env.apply("p2", rid="b"))
    assert (lost.outcome, lost.applied) == ("unknown", ["c0"])


def test_a_worker_bug_after_the_begin_is_an_unknown_outcome(env_of):  # R-05-6
    env = env_of(EditFake({"u1": RuntimeError("bug")}), make_plan(*THREE))
    events = env.apply()
    assert (outcome(events).outcome, error(events).message) == ("unknown", "Internal error: bug")
    assert env.conn.rollbacks == 1  # what the transaction held is released


def test_before_the_commit_a_failed_transaction_rolls_back_and_a_vanished_one_is_unknown(env_of):  # ruling E12
    tainted = env_of(EditFake(), make_plan(("update", "u0")))
    tainted.conn.answers["u0"] = lambda: (setattr(tainted.conn, "failed", True), 1)[1]
    events = tainted.apply()
    assert (outcome(events).outcome, error(events).code, tainted.conn.runs) == (
        "rolled_back", "transaction_failed", ["BEGIN", "u0"])
    vanished = env_of(EditFake(), make_plan(("update", "u0")))
    vanished.conn.answers["u0"] = lambda: (setattr(vanished.conn, "tx", False), 1)[1]
    events = vanished.apply()
    assert (outcome(events).outcome, error(events).code, vanished.conn.runs) == (
        "unknown", "outcome_unknown", ["BEGIN", "u0"])


def test_an_expired_plan_sends_nothing(env_of):  # Review Focus 4, R-05-6
    env = env_of(EditFake(), make_plan(("update", "u0")))
    first, second = env.apply(), env.apply(rid="b")
    assert outcome(first).outcome == "committed"
    assert types(second) == ["started", "error", "edit_outcome", "session_state", "finished"]
    assert (error(second).code, error(second).message) == ("plan_expired", PLAN_EXPIRED_MESSAGE)
    assert outcome(second).outcome == "rolled_back" and env.conn.runs == ["BEGIN", "u0", "COMMIT"]
    assert len(env.records) == 1  # nothing was sent: no History entry


def test_a_read_only_profile_and_an_open_transaction_send_nothing(env_of):  # R-05-7, R-05-4
    blocked = env_of(EditFake(), make_plan(("update", "u0")), check=read_only)
    events = blocked.apply()
    assert (error(events).code, outcome(events).outcome, blocked.conn.runs) == ("read_only", "rolled_back", [])
    busy = env_of(EditFake(), make_plan(("update", "u0")))
    busy.conn.tx = True
    events = busy.apply()
    assert (error(events).code, outcome(events).outcome, busy.conn.runs) == ("transaction_open", "rolled_back", [])


def test_an_apply_cancelled_before_it_started_still_says_nothing_was_applied(env_of):  # ruling E1
    env = env_of(EditFake({"hold": BLOCK}), make_plan(("update", "u0")))
    env.worker.submit(ExecuteCommand(request_id="q", sql="hold"))
    assert env.conn.blocked.wait(5)
    env.submit()
    env.worker.cancel("a")  # still queued behind q
    env.worker.cancel("q")
    events = env.events()
    assert types(events) == ["started", "message", "edit_outcome", "session_state", "finished"]
    assert outcome(events).outcome == "rolled_back" and events[-1].cancelled and "u0" not in env.conn.runs


def test_a_paused_result_is_ended_before_the_apply_starts(env_of):  # R-05-4
    env = env_of(EditFake(), make_plan(("update", "u0")))
    env.worker.submit(ExecuteCommand(request_id="browse", sql="rows 1000", page_size=500))
    env.recorder.wait_for(paused("browse", 500))
    events = env.apply()
    order = [(e.type, e.request_id) for e in env.recorder.events if e.type in ("started", "finished")]
    assert order.index(("finished", "browse")) < order.index(("started", "a"))
    assert outcome(events).outcome == "committed"


def test_a_statements_messages_reach_the_apply(env_of):  # a trigger's NOTICE or PRINT
    notice = ServerMessage(severity=MessageSeverity.INFO, text="trigger says hi")
    env = env_of(EditFake({"u0": Counted(1, [notice])}), make_plan(("update", "u0")))
    assert [e.text for e in env.apply() if e.type == "message"] == ["trigger says hi"]


def test_keys_and_values_never_reach_the_info_log(env_of):  # spec §5
    statement = PlannedStatement(change_id="c0", kind="update", sql="UPDATE t SET v = 'zz-secret' WHERE id = 7",
                                 row="id = 'zz-secret-key'")
    plan = make_plan(("update", "x")).model_copy(update={"statements": (statement,)})
    env = env_of(EditFake({statement.sql: 0}), plan)
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        events = env.apply()
    finally:
        logger.remove(sink)
    assert "zz-secret-key" in error(events).message
    assert not [text for level, text in records if level >= 20 and "zz-secret" in text]


def test_the_hooks_hand_a_session_only_plans_of_its_profile_and_database(tmp_path):  # CR-2, ruling E3
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    body = {"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}}
    pid = profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id
    history = HistoryService(tmp_path / "history.db")
    store = EditPlanStore()
    registry = AdapterRegistry([SqliteAdapter()])
    try:
        hooks = SessionHooksFactory(registry, profiles, SafetyPolicy(), history, FakeCache(), store).build(pid, "main")
        base = make_plan(("update", "u0"))
        store.put(base.model_copy(update={"plan_id": "theirs"}))
        store.put(base.model_copy(update={"plan_id": "other_db", "profile_id": pid, "database": "elsewhere"}))
        store.put(base.model_copy(update={"plan_id": "mine", "profile_id": pid}))
        for plan_id in ("theirs", "other_db"):
            with pytest.raises(PlanUnavailable) as raised:
                hooks.take_edit_plan(plan_id)
            assert raised.value.code == PLAN_MISMATCH
        assert hooks.take_edit_plan("mine").plan_id == "mine"
        planless = SessionHooksFactory(registry, profiles, SafetyPolicy(), history, FakeCache()).build(pid, "main")
        with pytest.raises(PlanUnavailable) as raised:
            planless.take_edit_plan("mine")
        assert raised.value.code == PLAN_EXPIRED
    finally:
        history.close()
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_edit_apply.py tests/test_edit_worker.py`
Expected: FAIL (ModuleNotFoundError: `dbaguy_backend.sessions.edit_apply`).

- [ ] **Step 3: The protocol.** In `domain/execution.py`, before `ClientCommand`:

```python
class ApplyEditsCommand(BaseModel):
    """Apply a previewed grid-edit plan (spec ⑤ §3.5): its statements exactly as previewed, in one transaction on this
    session. A Cancel stops it until its COMMIT has been sent."""

    type: Literal["apply_edits"] = "apply_edits"
    request_id: str
    plan_id: str = Field(min_length=1, max_length=64)
```

added to `ClientCommand`'s union; before `ServerEvent`:

```python
EditOutcome = Literal["committed", "rolled_back", "partial", "unknown"]


class EditOutcomeEvent(BaseModel):
    """How an apply ended (spec ⑤ R-05-6): sent once, right before the request's `session_state` and `finished`."""

    type: Literal["edit_outcome"] = "edit_outcome"
    request_id: str
    outcome: EditOutcome
    applied: list[str] = []  # change ids that took effect: all when committed; those before the failure when partial
    already_gone: list[str] = []  # DELETEs that matched no row: the row was gone already (R-05-5)
    failed_change: str | None = None
    statement: int | None = None  # the failing statement's index in the plan's statements (0-based)
```

added to `ServerEvent`'s union.

- [ ] **Step 4: The outcome table** — `backend/dbaguy_backend/sessions/edit_apply.py`:

```python
"""How an apply of a grid-edit plan ends (spec ⑤ §3.5, R-05-5, R-05-6, D-05-6): what each statement must have matched,
and the outcome — committed, rolled_back, partial or unknown — for every way the apply can stop. Pure: the worker does
the I/O and asks here. Messages name the row by its key: they go to the user's dialog, never to a log."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..adapters.base import StatementCancelled, StatementError
from ..domain.edits import PlannedStatement
from ..domain.execution import EditOutcome

CONNECTION_LOST = "connection_lost"
EDIT_CONFLICT = "edit_conflict"
ROW_COUNT_UNKNOWN = "row_count_unknown"
OUTCOME_UNKNOWN = "outcome_unknown"
TRANSACTION_FAILED = "transaction_failed"
NOTHING_APPLIED = "nothing was applied"
EARLIER_STAY = "the statements before it stay applied (this table cannot roll back)"


@dataclass(frozen=True)
class EditReport:
    """What the apply's last events say: its EditOutcomeEvent, and the error before it."""

    outcome: EditOutcome
    applied: list[str] = field(default_factory=list)
    already_gone: list[str] = field(default_factory=list)
    failed_change: str | None = None
    statement: int | None = None
    error: StatementError | None = None


def _as_cancel(error: StatementError, tail: str) -> StatementError:
    """A cancel reads as the spec words it (`Cancelled: nothing was applied`), whatever the adapter said."""
    return StatementCancelled(f"Cancelled: {tail}") if isinstance(error, StatementCancelled) else error


def refused(error: StatementError) -> EditReport:
    """Stopped before the BEGIN went out, or by the BEGIN itself: nothing was applied."""
    return EditReport("rolled_back", error=_as_cancel(error, NOTHING_APPLIED))


@dataclass
class EditProgress:
    """Where an apply is. The worker moves it on; each way of stopping asks it for the outcome (spec §3.5's table)."""

    statements: Sequence[PlannedStatement]
    transactional: bool
    sent: bool = False  # the BEGIN went out
    index: int | None = None  # the statement running, or the last one that ran
    committing: bool = False  # the COMMIT went out: a lost answer from here is an unknown outcome
    applied: list[str] = field(default_factory=list)  # statements that ran and matched as planned
    gone: list[str] = field(default_factory=list)  # DELETEs that matched no row: already gone (R-05-5)

    def _tail(self) -> str:
        return NOTHING_APPLIED if self.transactional else EARLIER_STAY

    def _current(self) -> PlannedStatement:
        if self.index is None:
            raise ValueError("no statement has run")
        return self.statements[self.index]

    def matched(self, rows: int | None) -> StatementError | None:
        """The current statement matched `rows` (R-05-5): None when that is what it had to match — it then counts as
        applied — else the error that stops the apply. An UPDATE and an INSERT match exactly one row, a DELETE at most
        one; a count that cannot be read fails closed (D-05-5)."""
        statement = self._current()
        tail = self._tail()
        if rows is None or rows < 0:
            return StatementError(f"Could not confirm how many rows changed: {tail}", code=ROW_COUNT_UNKNOWN)
        if rows == 1 or (statement.kind == "delete" and rows == 0):
            self.applied.append(statement.change_id)
            if rows == 0:
                self.gone.append(statement.change_id)
            return None
        if statement.kind == "insert":
            text = (f"The new row was not inserted (a trigger may have skipped it): {tail}" if rows == 0
                    else f"Inserting one row reported {rows} rows: {tail}")
        elif rows == 0:
            text = f"Row {statement.row} was changed or deleted since it was loaded (or a trigger skipped it): {tail}"
        else:
            text = f"Row {statement.row} matched {rows} rows: its key is not unique. {tail[0].upper()}{tail[1:]}"
        return StatementError(text, code=EDIT_CONFLICT)

    def failed(self, error: StatementError) -> EditReport:
        """The current statement failed, matched wrongly, was cancelled, or lost its connection (spec §3.5)."""
        statement = self._current()
        common = {"failed_change": statement.change_id, "statement": self.index,
                  "error": _as_cancel(error, self._tail())}
        if self.transactional:
            return EditReport("rolled_back", **common)
        done = {"applied": list(self.applied), "already_gone": list(self.gone)}
        if error.code == CONNECTION_LOST:  # this statement may or may not have taken effect
            return EditReport("unknown", **done, **common)
        return EditReport("partial" if self.applied else "rolled_back", **done, **common)

    def cancelled_before_commit(self) -> EditReport:
        return EditReport("rolled_back", error=StatementCancelled(f"Cancelled: {NOTHING_APPLIED}"))

    def aborted_before_commit(self) -> EditReport:
        """The transaction can only roll back now (PostgreSQL: aborted; SQL Server: uncommittable)."""
        return EditReport("rolled_back", error=StatementError(
            f"The transaction had failed: {NOTHING_APPLIED}", code=TRANSACTION_FAILED))

    def ended_before_commit(self) -> EditReport:
        """The transaction is gone without an error (spec §3.5 step 7): what happened to it is not known."""
        return EditReport("unknown", error=StatementError(
            "The transaction ended before COMMIT: the changes may or may not have been applied", code=OUTCOME_UNKNOWN))

    def commit_failed(self, error: StatementError) -> EditReport:
        """COMMIT went out and failed (R-06): refused by the server → rolled back; its answer lost → unknown. A table
        that cannot roll back had every statement take effect already (ruling E11): committed, the error a warning."""
        if not self.transactional:
            return self.committed(warning=error)
        if error.code == CONNECTION_LOST:
            return EditReport("unknown", error=error)
        return EditReport("rolled_back", error=StatementError(
            f"{error.message}\nThe transaction was rolled back: {NOTHING_APPLIED}", code=error.code))

    def committed(self, warning: StatementError | None = None) -> EditReport:
        return EditReport("committed", applied=[s.change_id for s in self.statements], already_gone=list(self.gone),
                          error=warning)

    def broke(self, exc: Exception) -> EditReport:
        """A bug (not a StatementError): before the BEGIN went out nothing was applied; after it, unknown (R-06)."""
        error = StatementError(f"Internal error: {exc}")
        if not self.sent:
            return EditReport("rolled_back", error=error)
        if self.transactional:
            return EditReport("unknown", error=error)
        return EditReport("unknown", applied=list(self.applied), already_gone=list(self.gone), error=error)


def history_error(report: EditReport, total: int) -> StatementError | None:
    """What History keeps as the apply's error (D-05-9, ruling E14): none for a commit or a cancel (status ok or
    cancelled), `Outcome unknown: …`, `Partly applied (n of m): …`, else the error itself."""
    error = report.error
    if report.outcome == "committed" or (report.outcome == "rolled_back" and isinstance(error, StatementCancelled)):
        return None
    message = error.message if error is not None else ""
    if report.outcome == "unknown":
        return StatementError(f"Outcome unknown: {message}", code=OUTCOME_UNKNOWN)
    if report.outcome == "partial":
        return StatementError(f"Partly applied ({len(report.applied)} of {total}): {message}",
                              code=error.code if error is not None else None)
    return error
```

- [ ] **Step 5: The worker.** In `sessions/worker.py`:
  - imports: `transaction_open_error` (from `..adapters.base`); `from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable`; `ApplyEditsCommand`, `EditOutcomeEvent` (from `..domain.execution`); `from .edit_apply import CONNECTION_LOST, ROW_COUNT_UNKNOWN, EditProgress, EditReport, history_error, refused`;
  - `RequestCommand = TypeVar("RequestCommand", ExecuteCommand, ExportCommand, ApplyEditsCommand)`;
  - after `_ignore`:

```python
def _no_edit_plans(plan_id: str) -> EditPlan:
    raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
```

  - `WorkerHooks` gains, after `capabilities`:

```python
    # The previewed grid-edit plan with this id, taken once (spec ⑤ D-05-7); raises PlanUnavailable
    take_edit_plan: Callable[[str], EditPlan] = _no_edit_plans
```

  - `submit` gains `case ApplyEditsCommand(): self._submit_request(command, self._apply_edits)` after the export case;
  - `_start_request`:

```python
    def _start_request(self, req: _Request, *, before_finished: BaseModel | None = None) -> bool:
        """Leave the queue and announce `started`. When the request must not run, end it here (with `before_finished`
        right before its `finished`, when given) and return False."""
        skipped = self._begin_request(req.request_id)
        self._send(StartedEvent(request_id=req.request_id))
        if skipped is None:
            return True
        self._send(MessageEvent(request_id=req.request_id, severity=MessageSeverity.WARNING, text=skipped))
        if before_finished is not None:
            self._send(before_finished)
        self._send_finished(req.request_id, req.elapsed_ms(), cancelled=True)
        return False
```

  - `_send_messages` delegates:

```python
    def _send_messages(self, ex: _OpenExecution) -> None:
        self._forward_messages(ex.request_id, ex.result)

    def _forward_messages(self, request_id: str, result: StatementResult) -> None:
        for message in result.take_messages():
            self._send_live_message(request_id, message)
```

  - before `_transaction_command`, the apply:

```python
    def _apply_edits(self, cmd: ApplyEditsCommand) -> None:
        """Apply a previewed plan (spec ⑤ §3.5): its texts and nothing else, in one explicit transaction on this
        session, every statement checked to match what it must (R-05-5), the outcome classified once (`EditProgress`)
        and never sent again. Events: started → affected × n → [error] → edit_outcome → session_state → finished;
        every apply ends with one edit_outcome, a refused or skipped one too (ruling E1)."""
        self._discard_open()  # the grid's paused result ends first (R-05-4); its own `finished` goes out as usual
        req = _Request(request_id=cmd.request_id, sql="", kind=StatementKind.WRITE)
        if not self._start_request(req, before_finished=EditOutcomeEvent(request_id=req.request_id, outcome="rolled_back")):
            return
        plan: EditPlan | None = None
        progress: EditProgress | None = None
        try:
            plan = self._take_plan(cmd.plan_id)
            req.sql = plan.script  # what History keeps (D-05-9)
            progress = EditProgress(plan.statements, plan.transactional)
            self._check_plan(plan)
            report = self._run_plan(req, plan, progress)
        except StatementError as exc:  # refused before anything was sent
            report = refused(exc)
        except Exception as exc:
            logger.exception("Unexpected error applying edits on {}", self.name)
            if progress is not None and progress.sent and progress.transactional and not progress.committing:
                self._rollback_quietly()  # release what the transaction holds; the outcome stays unknown
            report = (progress or EditProgress((), True)).broke(exc)
        finally:
            self._set_running(None)
        self._end_apply(req, plan, report)

    def _take_plan(self, plan_id: str) -> EditPlan:
        try:
            return self._hooks.take_edit_plan(plan_id)
        except PlanUnavailable as exc:
            raise StatementError(exc.message, code=exc.code) from None

    def _check_plan(self, plan: EditPlan) -> None:
        """Before anything is sent (spec §3.5 steps 3–4): every text can reach the driver whole, the policy passes every
        DML statement (a profile made read-only since the preview is refused here, and the server refuses again,
        R-05-7), and no transaction is open (R-05-4)."""
        for sql in (plan.begin, *(statement.sql for statement in plan.statements), plan.commit):
            _check_text(sql)
        for statement in plan.statements:
            decision = self._hooks.check(statement.sql, True)
            if decision.action == GuardAction.BLOCK:
                logger.info("[{}] blocked an apply of {} statement(s) (read-only)", self.name, len(plan.statements))
                raise StatementError(_read_only_message(decision.classification.kind), code="read_only")
        if self.connection.in_transaction():
            raise transaction_open_error()

    def _run_plan(self, req: _Request, plan: EditPlan, progress: EditProgress) -> EditReport:
        try:
            self._edit_statement(req, plan.begin)
        except StatementError as exc:
            self._rollback_quietly()  # a BEGIN that failed opened nothing; one whose answer was lost died with its link
            return refused(exc)
        progress.sent = True
        for index, statement in enumerate(plan.statements):
            progress.index = index
            try:
                rows = self._edit_statement(req, statement.sql)
                if rows is not None and rows >= 0:
                    req.rows += rows
                    self._send(AffectedEvent(request_id=req.request_id, index=index, rows=rows))
                if (mismatch := progress.matched(rows)) is not None:
                    logger.info("[{}] apply {}: statement {} ({}) matched {} row(s)", self.name, req.request_id,
                                index, statement.kind, rows)
                    raise mismatch
            except StatementError as exc:
                self._rollback_quietly()
                return progress.failed(exc)
        cancelled = self._set_running(None)  # from here a Cancel never reaches the driver (R-05-6)
        if plan.transactional:  # ruling E11: a table that cannot roll back has every statement applied already
            connection = self.connection
            if cancelled:
                self._rollback_quietly()
                return progress.cancelled_before_commit()
            if connection.transaction_failed():
                self._rollback_quietly()
                return progress.aborted_before_commit()
            if not connection.in_transaction():
                return progress.ended_before_commit()
        progress.committing = True
        try:
            self._edit_statement(req, plan.commit)
        except StatementError as exc:
            if exc.code != CONNECTION_LOST:
                self._rollback_quietly()  # SQLite keeps the transaction after a busy COMMIT (A14); a no-op elsewhere
            return progress.commit_failed(exc)
        return progress.committed()

    def _edit_statement(self, req: _Request, sql: str) -> int | None:
        """Run one text of the plan: the rows it matched are read first, then the rest of its answer to the end, so
        SQL Server never has to stop it with an attention (ruling E9); its messages (a trigger's NOTICE or PRINT) go
        to the request."""
        result = self._run_statement(req, "edit", sql, RunOptions())
        try:
            rows = result.matched_rows()
            self._forward_messages(req.request_id, result)
            while result.next_result():
                self._forward_messages(req.request_id, result)
            return rows
        finally:
            self._close_result(result)

    def _rollback_quietly(self) -> None:
        """End the apply's transaction once it stopped. No Cancel may reach the ROLLBACK, so the request stops running
        first. A connection that is gone cannot roll back: the server does it as the connection ends (R-05-6)."""
        self._set_running(None)
        try:
            self.connection.rollback()
        except Exception as exc:
            logger.info("[{}] the rollback after a stopped apply failed ({})", self.name,
                        getattr(exc, "code", None) or type(exc).__name__)

    def _end_apply(self, req: _Request, plan: EditPlan | None, report: EditReport) -> None:
        """The apply's last events: its error (for a commit that stood, a warning instead), the edit_outcome, then the
        History record, session_state and finished (`_complete`)."""
        rid, error = req.request_id, report.error
        if error is not None and report.outcome == "committed":
            text = f"COMMIT reported an error after every statement had taken effect: {error.message}"
            self._send(MessageEvent(request_id=rid, severity=MessageSeverity.WARNING, text=text))
        elif error is not None:
            self._send(ErrorEvent(request_id=rid, message=error.message, code=error.code, position=error.position))
        self._send(EditOutcomeEvent(
            request_id=rid, outcome=report.outcome, applied=report.applied, already_gone=report.already_gone,
            failed_change=report.failed_change, statement=report.statement,
        ))
        total = len(plan.statements) if plan is not None else 0
        warn = report.outcome in ("unknown", "partial") or (error is not None and error.code == ROW_COUNT_UNKNOWN)
        logger.log("WARNING" if warn else "INFO", "[{}] apply {}: {} statement(s) on {} → {} in {} ms{}", self.name,
                   rid, total, plan.table if plan is not None else "-", report.outcome, req.elapsed_ms(),
                   f" ({error.code})" if error is not None and error.code else "")  # codes only (spec §5)
        cancelled = report.outcome == "rolled_back" and isinstance(error, StatementCancelled)
        self._complete(req, error=history_error(report, total), cancelled=cancelled)
```

- [ ] **Step 6: The hooks and the container.** `sessions/hooks.py` (imports: `from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable`, `from ..services.edit_plans import EditPlanStore`):

```python
class SessionHooksFactory:
    """Connects a session's worker to the safety policy, the history, the schema cache and the grid-edit plans."""

    def __init__(
        self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
        schema_cache: SchemaCache, plans: EditPlanStore | None = None,
    ) -> None:
        ...  # as before, plus
        self._plans = plans
```

and at the end of `build`:

```python
        plans = self._plans

        def take_edit_plan(plan_id: str) -> EditPlan:
            # Only this profile's plans, and only for this session's database when the plan's path names one (CR-2)
            if plans is None:
                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
            return plans.take(plan_id, profile_id, database)

        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record, capabilities=capabilities,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),  # every database of the profile
            take_edit_plan=take_edit_plan,
        )
```

`container.py`: `hooks = SessionHooksFactory(self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans)`.

- [ ] **Step 7: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_edit_apply.py tests/test_edit_worker.py tests/test_session_worker.py tests/test_sessions.py tests/test_worker_protocol.py tests/test_session_wiring.py tests/test_transactions.py tests/test_read_ahead.py tests/test_api.py`
Expected: PASS (the existing worker tests unchanged).

- [ ] **Step 8: Regenerate the API types, run the whole suite, commit**

Run: `npm run gen:api && npm run typecheck && npm run build && cd backend && .venv/bin/pytest -q`
Expected: the execution schema's `command` and `event` unions gain `apply_edits` and `edit_outcome` (the frontend's `applyEvent` already ignores an event type it does not know); typecheck, build and the suite pass.

```bash
git add backend/dbaguy_backend/domain/execution.py backend/dbaguy_backend/sessions/edit_apply.py \
  backend/dbaguy_backend/sessions/worker.py backend/dbaguy_backend/sessions/hooks.py backend/dbaguy_backend/container.py \
  backend/tests/test_edit_apply.py backend/tests/test_edit_worker.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts
git commit -m "feat(sessions): apply a previewed grid-edit plan — one transaction, exactly-1 checks, four outcomes, never resent"
```

---
### Task 9: Live flows on every engine; PostgreSQL and SQLite specifics

**Files:**
- Modify: `backend/tests/edit_support.py` (the app in-process and the change builders)
- Create: `backend/tests/test_edit_flows.py`
- Modify: `backend/tests/test_postgres_edits.py`, `backend/tests/test_sqlite_edits.py` (live applies appended)

**Interfaces:**
- Consumes: Tasks 1–8 (everything); `AppContainer`, `Settings`, `InMemorySecretStore`, `SessionCreateRequest`, `tests.test_sessions.Recorder`, `finished`, `paused`; `sql/select_builder.build_browse_select` (the browse a table-data tab runs).
- Produces (`tests/edit_support.py`, used by Tasks 10 and 11):
  - change builders `val(text) -> dict`, `NULL`, `DEFAULT`, `update(change_id, key, set_, original) -> dict`, `insert(change_id, values) -> dict`, `delete(change_id, key) -> dict` (request JSON, as 05b will send it);
  - `Applied(events)` with `types`, `outcome` (the EditOutcomeEvent), `error`, `affected` (`[(index, rows)]`), `messages`, `finished`;
  - `Tab(worker, recorder, database)` with `run(sql) -> Applied` (asserts no error), `rows(sql) -> list[list]` (the cells as the grid gets them), `browse(sql, page_size=500) -> str` (left paused), `submit_apply(plan_id) -> str`, `apply(plan_id, timeout=60.0) -> Applied`, `wait(rid, timeout=60.0) -> Applied`;
  - `EditApp(directory)` with `container`, `add_profile(body) -> UUID`, `update_profile(profile_id, body)`, `target(profile_id, path)`, `preview(profile_id, path, *, updates=(), inserts=(), deletes=()) -> EditPreview`, `tab(profile_id, database) -> Tab`, `history() -> list[HistoryEntry]`, `close_tabs()`, `close()`;
  - `edit_env(engine, directory)` — a context manager yielding `(db, app, profile_id)`.

These tests prove Tasks 3–8 against the real engines (ruling E21: in-process, through `EditService`, the hooks and the session worker; HTTP is Task 7's and Task 11's). They are expected to pass as soon as they are written; a failure is a defect of the task that owns the code — fix it there (and add a unit test there when the defect is in pure logic), never by loosening the test, and say so in the report. Every test passes `app.close_tabs` to `scratch`, so no session holds a lock the DROP would wait for (a MySQL DROP waits for metadata locks indefinitely).

- [ ] **Step 1: The app in-process.** Append to `backend/tests/edit_support.py` (imports added: `itertools`; `from dataclasses import dataclass`; `UUID`; `from dbaguy_backend.config import Settings`; `from dbaguy_backend.container import AppContainer`; `from dbaguy_backend.domain.edits import EditChanges, EditPreview, EditTarget`; `from dbaguy_backend.domain.execution import ApplyEditsCommand, EditOutcomeEvent, ErrorEvent, ExecuteCommand, FinishedEvent`; `from dbaguy_backend.domain.history import HistoryEntry`; `ProfileUpsertRequest`; `from dbaguy_backend.sessions.manager import SessionCreateRequest`; `from dbaguy_backend.sessions.worker import SessionWorker`; `from dbaguy_backend.storage.secret_store import InMemorySecretStore`; `from tests.test_sessions import Recorder, finished, paused`):

```python
# --- the app in-process (Task 9) -------------------------------------------------------------------------------------


def val(text: str) -> dict:
    return {"kind": "value", "text": text}


NULL = {"kind": "null"}
DEFAULT = {"kind": "default"}


def update(change_id: str, key: dict, set_: dict, original: dict) -> dict:
    return {"id": change_id, "key": key, "set": set_, "original": original}


def insert(change_id: str, values: dict) -> dict:
    return {"id": change_id, "values": values}


def delete(change_id: str, key: dict) -> dict:
    return {"id": change_id, "key": key}


@dataclass
class Applied:
    """One request's events, from its `started` to its `finished`."""

    events: list

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]

    @property
    def outcome(self) -> EditOutcomeEvent:
        return next(event for event in self.events if event.type == "edit_outcome")

    @property
    def error(self) -> ErrorEvent | None:
        return next((event for event in self.events if event.type == "error"), None)

    @property
    def affected(self) -> list[tuple[int, int]]:
        return [(event.index, event.rows) for event in self.events if event.type == "affected"]

    @property
    def messages(self) -> list[str]:
        return [event.text for event in self.events if event.type == "message"]

    @property
    def finished(self) -> FinishedEvent:
        return self.events[-1]


class Tab:
    """A table-data tab's session: its worker, with the events it sent."""

    def __init__(self, worker: SessionWorker, recorder: Recorder, database: str | None) -> None:
        self.worker = worker
        self.recorder = recorder
        self.database = database
        self._ids = itertools.count(1)

    def _rid(self, what: str) -> str:
        return f"{what}{next(self._ids)}"

    def run(self, sql: str) -> Applied:
        """A statement of the test's own (a session setting), run to its end without an error."""
        rid = self._rid("run")
        self.worker.submit(ExecuteCommand(request_id=rid, sql=sql, history=False, page_size=10_000))
        done = self.wait(rid)
        assert done.error is None, done.error.message
        return done

    def rows(self, sql: str) -> list[list]:
        """The cells of a SELECT exactly as the grid gets them (encode_row)."""
        done = self.run(sql)
        return [row for event in done.events if event.type == "rows" for row in event.rows]

    def browse(self, sql: str, page_size: int = 500) -> str:
        """A SELECT left paused after its first page, as the table-data grid leaves it."""
        rid = self._rid("browse")
        self.worker.submit(ExecuteCommand(request_id=rid, sql=sql, history=False, page_size=page_size))
        self.recorder.wait_for(paused(rid, page_size), timeout=30)
        return rid

    def submit_apply(self, plan_id: str | None) -> str:
        rid = self._rid("apply")
        self.worker.submit(ApplyEditsCommand(request_id=rid, plan_id=plan_id or "none"))
        return rid

    def apply(self, plan_id: str | None, timeout: float = 60.0) -> Applied:
        return self.wait(self.submit_apply(plan_id), timeout)

    def wait(self, rid: str, timeout: float = 60.0) -> Applied:
        self.recorder.wait_for(finished(rid), timeout)
        events = list(self.recorder.events)
        start = next(i for i, e in enumerate(events) if e.type == "started" and e.request_id == rid)
        end = next(i for i, e in enumerate(events) if e.type == "finished" and e.request_id == rid)
        return Applied(events[start:end + 1])


class EditApp:
    """The backend in-process — an AppContainer over a temporary data dir with secrets in memory — so a test previews
    through EditService and applies through a session's worker with its real hooks and adapter (ruling E21)."""

    def __init__(self, directory: Path) -> None:
        settings = Settings(token="edits", data_dir=directory / "data", log_to_file=False)
        self.container = AppContainer(settings, secret_store=InMemorySecretStore())

    def add_profile(self, body: dict) -> UUID:
        return self.container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id

    def update_profile(self, profile_id: UUID, body: dict) -> None:
        self.container.profiles.update(profile_id, ProfileUpsertRequest.model_validate(body))

    def target(self, profile_id: UUID, path: list[PathSegment]) -> EditTarget:
        return self.container.edits.target(profile_id, path)

    def preview(self, profile_id: UUID, path: list[PathSegment], *, updates=(), inserts=(), deletes=()) -> EditPreview:
        changes = EditChanges.model_validate({"updates": list(updates), "inserts": list(inserts),
                                              "deletes": list(deletes)})
        return self.container.edits.preview(profile_id, path, changes)

    def tab(self, profile_id: UUID, database: str | None) -> Tab:
        info = self.container.sessions.create(SessionCreateRequest(profile_id=profile_id, database=database))
        recorder = Recorder()
        return Tab(self.container.sessions.attach(info.session_id, recorder), recorder, info.database)

    def history(self) -> list[HistoryEntry]:
        return self.container.history.search().items

    def close_tabs(self) -> None:
        """Every session closed: nothing holds a lock a scratch DROP would wait for."""
        self.container.sessions.close_all()

    def close(self) -> None:
        self.container.close()


@contextmanager
def edit_env(engine: str, directory: Path) -> Iterator[tuple[ScratchDb, EditApp, UUID]]:
    """A scratch database, the app, and a profile for it (password saved in memory)."""
    db = scratch_db(engine, directory)
    app = EditApp(directory)
    try:
        yield db, app, app.add_profile(db.profile())
    finally:
        app.close()
        db.close()
```

- [ ] **Step 2: The shared flows** — `backend/tests/test_edit_flows.py` (every engine; the row-lock cancel on the three server engines):

```python
"""Grid edits end to end on every relational engine (spec ⑤ §6 整合, §3.5, 成功標準): a preview through EditService,
an apply through the table-data session's worker, and each engine's own driver to check what the server holds. One
table shape for all (`items_table`); engine-specific behaviour is in test_<engine>_edits.py."""

import pytest

from dbaguy_backend.sql.select_builder import build_browse_select
from tests.edit_support import (
    ENGINES, NULL, SERVER_ENGINES, delete, edit_env, insert, items_table, scratch, update, val,
)
from tests.support import scaled, wait_until

DUPLICATE_KEY = {"postgres": "23505", "mysql": "1062", "mssql": "2627", "sqlite": "SQLITE_CONSTRAINT_PRIMARYKEY"}


@pytest.fixture(params=ENGINES)
def env(request, tmp_path):
    with edit_env(request.param, tmp_path) as made:
        yield made


@pytest.fixture(params=SERVER_ENGINES)
def server_env(request, tmp_path):  # the engines whose statements wait on a row lock (SQLite waits in its busy handler)
    with edit_env(request.param, tmp_path) as made:
        yield made


def items(db, table: str) -> list[tuple]:
    return db.rows(f"SELECT id, label, {db.q('order')}, note FROM {db.table(table)} ORDER BY id")


def test_a_commit_applies_deletes_updates_and_inserts_in_that_order(env):  # 成功標準 1, Review Focus 1, 2
    db, app, pid = env
    text = "it's 50% \\ done\nline 2 😀"
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(
            pid, db.path(table),
            updates=[update("u:1", {"id": 1}, {"label": val(text), "order": NULL, "note": val("")},
                            {"label": "a", "order": 1, "note": None})],
            inserts=[insert("i:1", {"id": val("9007199254740993"), "label": val("new")})],
            deletes=[delete("d:3", {"id": 3})],
        )
        assert preview.problems == [] and [s.kind for s in preview.statements] == ["delete", "update", "insert"]
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert done.types == ["started", "affected", "affected", "affected", "edit_outcome", "session_state",
                              "finished"]
        assert (done.outcome.outcome, done.outcome.applied, done.affected) == (
            "committed", ["d:3", "u:1", "i:1"], [(0, 1), (1, 1), (2, 1)])
        assert items(db, table) == [(1, text, None, ""), (2, "b", 2, "x"), (9007199254740993, "new", None, None)]
        [entry] = [entry for entry in app.history() if entry.sql == preview.script]
        assert (entry.status, entry.rows) == ("ok", 3)


def test_a_key_beyond_two_to_the_53_matches_exactly(env):  # Review Focus 3
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make, [(9007199254740992, "below", None, None), (9007199254740993, "big", None, None)])
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": "9007199254740993"}, {"label": val("bigger")}, {"label": "big"})])
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert [row[:2] for row in items(db, table)] == [(9007199254740992, "below"), (9007199254740993, "bigger")]


def test_a_cell_changed_since_loading_rolls_everything_back(env):  # 成功標準 2, R-05-5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("mine 1")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("mine 2")}, {"label": "b"})])
        db.execute(f"UPDATE {db.table(table)} SET label = 'theirs' WHERE id = 2")
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement) == ("rolled_back", "u:2", 1)
        assert done.error.code == "edit_conflict" and done.error.message == (
            "Row id = 2 was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied")
        assert [row[:2] for row in items(db, table)] == [(1, "a"), (2, "theirs"), (3, "c")]


def test_a_change_to_another_column_of_the_same_row_is_kept(env):  # 成功標準 3
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("mine")},
                                                                   {"label": "a"})])
        db.execute(f"UPDATE {db.table(table)} SET note = 'theirs' WHERE id = 1")
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert items(db, table)[0] == (1, "mine", 1, "theirs")


def test_a_row_deleted_since_loading_is_already_gone(env):  # R-05-5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:3", {"id": 3})])
        db.execute(f"DELETE FROM {db.table(table)} WHERE id = 3")
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.already_gone, done.affected) == ("committed", ["d:3"], [(0, 0)])


def test_an_error_half_way_applies_nothing(env):  # spec §6 中途錯誤, Review Focus 5
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table),
                              updates=[update("u:1", {"id": 1}, {"label": val("changed")}, {"label": "a"})],
                              inserts=[insert("i:2", {"id": val("2"), "label": val("duplicate")})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.outcome.statement) == ("rolled_back", "i:2", 1)
        assert done.error.code == DUPLICATE_KEY[db.engine] and not done.finished.in_transaction
        assert items(db, table)[0][1] == "a"


def test_a_plan_is_applied_once(env):  # Review Focus 4, R-05-6
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), inserts=[insert("i:4", {"id": val("4"), "label": val("once")})])
        tab = app.tab(pid, db.database)
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        again = tab.apply(preview.plan_id)
        other = app.tab(pid, db.database).apply(preview.plan_id)
        for refused in (again, other):
            assert refused.types == ["started", "error", "edit_outcome", "session_state", "finished"]
            assert (refused.error.code, refused.outcome.outcome) == ("plan_expired", "rolled_back")
        assert [row[0] for row in items(db, table)] == [1, 2, 3, 4]


def test_a_paused_browse_is_ended_before_the_apply(env):  # R-05-4: one connection, the browse goes first
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        db.fill(db.table(table), 2000, 100)
        tab = app.tab(pid, db.database)
        browse = tab.browse(build_browse_select(db.dialect, db.path(table), None, []))
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("x")},
                                                                   {"label": "a"})])
        done = tab.apply(preview.plan_id)
        order = [(e.type, e.request_id) for e in tab.recorder.events if e.type in ("started", "finished")]
        assert order.index(("finished", browse)) < order.index(("started", done.events[0].request_id))
        assert done.outcome.outcome == "committed" and items(db, table)[0][1] == "x"


def test_a_profile_made_read_only_after_the_preview_sends_nothing(env):  # R-05-7: the policy blocks first
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})])
        tab = app.tab(pid, db.database)
        app.update_profile(pid, db.profile(read_only=True))
        done = tab.apply(preview.plan_id)
        assert (done.error.code, done.outcome.outcome) == ("read_only", "rolled_back")
        assert len(items(db, table)) == 3 and not [e for e in app.history() if e.sql == preview.script]


def test_cancel_while_waiting_for_a_lock_rolls_back(server_env):  # spec §6 等鎖時 Cancel, Review Focus 5
    db, app, pid = server_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[
            update("u:2", {"id": 2}, {"label": val("mine")}, {"label": "b"}),
            update("u:1", {"id": 1}, {"label": val("mine")}, {"label": "a"})])
        tab = app.tab(pid, db.database)
        with db.holding(f"UPDATE {db.table(table)} SET label = 'held' WHERE id = 1"):
            rid = tab.submit_apply(preview.plan_id)
            assert wait_until(db.waiting, scaled(10)), "the apply never waited for the row lock"
            tab.worker.cancel(rid)
            done = tab.wait(rid)
        assert (done.error.code, done.error.message) == ("cancelled", "Cancelled: nothing was applied")
        assert (done.outcome.outcome, done.outcome.failed_change) == ("rolled_back", "u:1")
        assert done.finished.cancelled and not done.finished.in_transaction
        assert [row[:2] for row in items(db, table)] == [(1, "a"), (2, "b"), (3, "c")]


def test_a_key_column_can_be_changed(env):  # CR-4: the WHERE uses the loaded key
    db, app, pid = env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), updates=[update("u:2", {"id": 2}, {"id": val("20")}, {"id": 2})])
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert [row[0] for row in items(db, table)] == [1, 3, 20]
```

- [ ] **Step 3: PostgreSQL specifics** — append to `backend/tests/test_postgres_edits.py` (its import from `tests.edit_support` becomes `DEFAULT, PgScratch, edit_env, insert, items_table, scratch, update, val`):

```python
# --- live applies (Task 9) ------------------------------------------------------------------------------------------


@pytest.fixture
def pg_env(tmp_path):
    with edit_env("postgres", tmp_path) as made:
        yield made


def test_originals_of_every_type_match_as_the_session_loaded_them(pg_env):  # A4, in a non-UTC session
    db, app, pid = pg_env
    names = ["n", "ts", "tz", "iv", "mo", "r", "ar", "j", "jb", "x", "p", "u", "c", "b", "by", "d"]
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, n numeric(30,10), ts timestamptz, tz timetz, "
                              "iv interval, mo money, r int4range, ar text[], j json, jb jsonb, x xml, p point, u uuid, "
                              "c char(5), b boolean, by bytea, d date)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.10, '2026-10-19 12:34:56.123456+08', '12:34:56+05:30', "
                   "'1 day 02:03:04', 12.34, '[1,10)', ARRAY['a b', 'c,d', NULL], '{\"b\": 1,  \"a\": 2}', "
                   "'{\"b\": 1, \"a\": [1.10, 2]}', '<a>b</a>', '(1.5,2)', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', "
                   "'ab', true, '\\x00ff', '2026-10-19')")
        tab = app.tab(pid, db.database)
        tab.run("SET TIME ZONE 'Asia/Taipei'")  # the browse and the apply share this session's settings
        [row] = tab.rows(f"SELECT {', '.join(names)} FROM {db.table(table)} WHERE id = 1")
        new = {"n": "2.50", "ts": "2026-10-20 08:00:00+08", "tz": "01:02:03+00", "iv": "2 hours", "mo": "5",
               "r": "[2,3)", "ar": "{x}", "j": '{"c": 3}', "jb": '{"c": 3}', "x": "<c/>", "p": "(0,0)",
               "u": "00000000-0000-0000-0000-000000000001", "c": "zz", "b": "false", "by": "0x01", "d": "2026-10-20"}
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {name: val(text) for name, text in new.items()}, dict(zip(names, row)))])
        assert preview.problems == [] and preview.statements[0].unchecked == []
        done = tab.apply(preview.plan_id)
        assert done.outcome.outcome == "committed", done.error and done.error.message
        assert db.rows(f"SELECT n::text, j::text, by, c FROM {db.table(table)}") == [
            ("2.5000000000", '{"c": 3}', b"\x01", "zz   ")]


def test_a_floating_point_column_is_listed_unchecked(pg_env):  # known limitation (spec §8 Q3)
    db, app, pid = pg_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, price double precision)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.5)")
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"price": val("2.5")},
                                                                   {"price": 1.5})])
        assert [(u.column, u.reason) for u in preview.statements[0].unchecked] == [
            ("price", "double precision values cannot be compared exactly")]
        assert [w.code for w in preview.warnings] == ["unchecked_columns"]
        db.execute(f"UPDATE {db.table(table)} SET price = 9 WHERE id = 1")  # not caught: the preview said so
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT price FROM {db.table(table)}") == [(2.5,)]


def test_defaults_identities_and_generated_columns(pg_env):  # R-05-8 DEFAULT, spec §3.3.2
    db, app, pid = pg_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                              "label text NOT NULL DEFAULT 'none', made timestamptz NOT NULL DEFAULT now(), "
                              "twice int GENERATED ALWAYS AS (id * 2) STORED, fixed int GENERATED ALWAYS AS IDENTITY)")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {"id": DEFAULT, "label": val("given")}),
                                                            insert("i:2", {})])
        assert [s.sql for s in preview.statements] == [
            f"INSERT INTO public.{table} (label) VALUES ('given')", f"INSERT INTO public.{table} DEFAULT VALUES"]
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, label, twice FROM {db.table(table)} ORDER BY id") == [(1, "given", 2), (2, "none", 4)]
        refused = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"twice": val("9"), "fixed": val("1")}, {"twice": 2, "fixed": 1})])
        assert sorted((p.column, p.code) for p in refused.problems) == [
            ("fixed", "read_only_column"), ("twice", "read_only_column")]


def test_a_deferred_constraint_failing_at_commit_rolls_back(pg_env):  # R-05-6, Review Focus 5
    db, app, pid = pg_env
    with scratch(db, app.close_tabs) as make:
        parent = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY)")
        child = make("TABLE", f"CREATE TABLE {{t}} (id int PRIMARY KEY, parent int REFERENCES {db.table(parent)} "
                              "DEFERRABLE INITIALLY DEFERRED)")
        preview = app.preview(pid, db.path(child), inserts=[insert("i:1", {"id": val("1"), "parent": val("99")})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.statement, done.error.code) == ("rolled_back", None, "23503")
        assert done.error.message.endswith("\nThe transaction was rolled back: nothing was applied")
        assert not done.finished.in_transaction and db.rows(f"SELECT count(*) FROM {db.table(child)}") == [(0,)]


@pytest.mark.parametrize(("state", "code"), [("serialization_failure", "40001"), ("deadlock_detected", "40P01")])
def test_a_serialization_failure_or_a_deadlock_at_commit_rolls_back(pg_env, state, code):  # A13
    db, app, pid = pg_env
    with scratch(db, app.close_tabs) as make:
        fail = make("FUNCTION", "CREATE FUNCTION {t}() RETURNS trigger LANGUAGE plpgsql AS "
                                f"$$ BEGIN RAISE EXCEPTION 'at commit' USING ERRCODE = '{state}'; END $$")
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, v text)")
        db.execute(f"CREATE CONSTRAINT TRIGGER {table}_late AFTER INSERT ON {db.table(table)} "
                   f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {db.table(fail)}()")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {"id": val("1"), "v": val("x")})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.statement, done.error.code) == ("rolled_back", None, code)
        assert not done.finished.in_transaction and db.rows(f"SELECT count(*) FROM {db.table(table)}") == [(0,)]


def test_a_trigger_that_skips_the_row_is_a_conflict_and_its_notice_reaches_the_apply(pg_env):  # R-05-5, spec §3.5
    db, app, pid = pg_env
    with scratch(db, app.close_tabs) as make:
        skip = make("FUNCTION", "CREATE FUNCTION {t}() RETURNS trigger LANGUAGE plpgsql AS "
                                "$$ BEGIN RAISE NOTICE 'skipped by a trigger'; RETURN NULL; END $$")
        table = items_table(db, make)
        db.execute(f"CREATE TRIGGER {table}_skip BEFORE UPDATE ON {db.table(table)} FOR EACH ROW "
                   f"WHEN (NEW.label = 'skip me') EXECUTE FUNCTION {db.table(skip)}()")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("kept")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("skip me")}, {"label": "b"})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.error.code) == ("rolled_back", "u:2", "edit_conflict")
        assert "(or a trigger skipped it)" in done.error.message and "skipped by a trigger" in done.messages
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]
```

- [ ] **Step 4: SQLite specifics** — append to `backend/tests/test_sqlite_edits.py` (imports: `from tests.edit_support import SqliteScratch, edit_env, insert, items_table, scratch, update, val` and `from tests.support import scaled`):

```python
# --- live applies (Task 9) ------------------------------------------------------------------------------------------


@pytest.fixture
def lite_env(tmp_path):
    with edit_env("sqlite", tmp_path) as made:
        yield made


def test_a_commit_blocked_by_another_sessions_reader_rolls_back(lite_env):  # A14, ruling E10
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        db.fill(db.table(table), 3000, 100)
        app.tab(pid, None).browse(f"SELECT * FROM {db.table(table)}")  # paused: its read lock stays
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val("x")},
                                                                   {"label": "a"})])
        done = app.tab(pid, None).apply(preview.plan_id, timeout=scaled(30))  # the 5 s busy timeout, then the error
        assert (done.outcome.outcome, done.outcome.statement, done.error.code) == ("rolled_back", None, "SQLITE_BUSY")
        assert done.error.message == "database is locked\nThe transaction was rolled back: nothing was applied"
        assert not done.finished.in_transaction
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]


def test_a_big_integer_in_a_column_without_a_type_is_found(lite_env):  # ruling E7, Review Focus 3
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id PRIMARY KEY, label TEXT)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (9007199254740993, 'big'), ('9007199254740993x', 'text')")
        tab = app.tab(pid, None)
        [[cell]] = tab.rows(f"SELECT id FROM {db.table(table)} WHERE label = 'big'")
        assert cell == "9007199254740993"  # what the grid holds: encode_value made the integer a string
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": cell}, {"label": val("bigger")},
                                                                   {"label": "big"})])
        assert "WHERE id IN (9007199254740993, '9007199254740993')" in preview.statements[0].sql
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT typeof(id), label FROM {db.table(table)} ORDER BY label") == [
            ("integer", "bigger"), ("text", "text")]


def test_declared_types_shape_values_defaults_and_checks(lite_env):  # ruling E6
    db, app, pid = lite_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INTEGER PRIMARY KEY, made DATETIME DEFAULT CURRENT_TIMESTAMP, "
                              "ok BOOLEAN NOT NULL DEFAULT 0, price REAL, twice INT GENERATED ALWAYS AS (id * 2) VIRTUAL)")
        preview = app.preview(pid, db.path(table), inserts=[
            insert("i:1", {"made": val("2026-10-19 12:00:00"), "ok": val("yes"), "price": val("1.5")}),
            insert("i:2", {})])
        assert [s.sql for s in preview.statements] == [
            f"INSERT INTO main.{table} (made, ok, price) VALUES ('2026-10-19 12:00:00', 1, 1.5)",
            f"INSERT INTO main.{table} DEFAULT VALUES"]
        tab = app.tab(pid, None)
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, made, ok, price, twice FROM {db.table(table)} WHERE id = 1") == [
            (1, "2026-10-19 12:00:00", 1, 1.5, 2)]
        update_price = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"price": val("2")}, {"price": 1.5})])
        assert [(u.column, u.reason) for u in update_price.statements[0].unchecked] == [
            ("price", "REAL values cannot be compared exactly")]
        refused = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"twice": val("9")},
                                                                   {"twice": 2})])
        assert [(p.column, p.code) for p in refused.problems] == [("twice", "read_only_column")]
```

- [ ] **Step 5: Run them**

Run: `cd backend && .venv/bin/pytest -q tests/test_edit_flows.py tests/test_postgres_edits.py tests/test_sqlite_edits.py`
Expected: PASS, nothing skipped — 43 shared flow cases (10 tests × 4 engines, the row-lock cancel × 3 server engines) and the engine files; about 15 seconds (A14's test waits out SQLite's 5 s busy timeout). Planning ran these exact tests against a prototype of Tasks 1–8: all passed. Then check that nothing was left behind: no `zz\_05a%` relation, type or function in PostgreSQL (`pg_class`, `pg_type`, `pg_proc`), table in MySQL's `information_schema.TABLES`, object or type in SQL Server's `sys.objects` / `sys.types`.

- [ ] **Step 6: Record A4, A13, A14** in the assumptions table (owner: this task): A4 — `test_originals_of_every_type_match_as_the_session_loaded_them`; A13 — `test_a_serialization_failure_or_a_deadlock_at_commit_rolls_back` (a deferred constraint trigger raising 40001 / 40P01 at COMMIT: the failure path of a real serialization failure or deadlock found at commit, made deterministic); A14 — `test_a_commit_blocked_by_another_sessions_reader_rolls_back`. If one behaves otherwise, write the result over its row and apply its Effect.

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green, no `requires_*` test skipped.

```bash
git add backend/tests/edit_support.py backend/tests/test_edit_flows.py backend/tests/test_postgres_edits.py \
  backend/tests/test_sqlite_edits.py
git commit -m "test: grid edits end to end on every engine; PostgreSQL types, triggers and commit failures; SQLite locks"
```

(Plus any fix in Tasks 3–8's code a failure led to, with its own unit test, and this plan file if Step 6 changed it.)

---
### Task 10: MySQL and SQL Server specifics

**Files:**
- Modify: `backend/tests/test_mysql_edits.py`, `backend/tests/test_mssql_edits.py` (live applies appended)

**Interfaces:**
- Consumes: Task 9 (`edit_env`, `EditApp`, `update`, `insert`, `delete`, `val`, `Applied`, `Tab`); Task 1 (`MysqlScratch`, `items_table`, `scratch`).
- Produces: the engine-specific proof of D-05-2, D-05-5, D-05-6, D-05-13, R-05-7 (the server's own refusal) and A2, A3, A9, A15.

As in Task 9: the tests are expected to pass when written; a failure is a defect of the owning task's code, fixed there with a unit test where the logic is pure.

- [ ] **Step 1: MySQL** — append to `backend/tests/test_mysql_edits.py` (its import from `tests.edit_support` becomes `EditApp, MysqlScratch, delete, edit_env, insert, items_table, scratch, update, val`):

```python
# --- live applies (Task 10) -----------------------------------------------------------------------------------------


@pytest.fixture
def my_env(tmp_path):
    with edit_env("mysql", tmp_path) as made:
        yield made


def test_a_decimal_set_to_an_equal_value_is_matched_not_a_conflict(my_env):  # D-05-5, A1, Review Focus 3
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, amount DECIMAL(10,2)) ENGINE=InnoDB")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.10)")
        preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"amount": val("1.1")},
                                                                   {"amount": "1.10"})])
        done = app.tab(pid, None).apply(preview.plan_id)
        assert (done.outcome.outcome, done.affected) == ("committed", [(0, 1)])  # matched 1, changed 0


def test_zero_dates_and_json_originals_match(my_env):  # D-05-2
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT PRIMARY KEY, d DATE NOT NULL, doc JSON) ENGINE=InnoDB")
        db.execute("SET SESSION sql_mode = ''",
                   f"INSERT INTO {db.table(table)} VALUES (1, '0000-00-00', '{{\"b\": 1, \"a\": [1.10, 2]}}')",
                   "SET SESSION sql_mode = DEFAULT")
        tab = app.tab(pid, None)
        [[day, doc]] = tab.rows(f"SELECT d, doc FROM {db.table(table)}")
        assert day == "0000-00-00"
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"d": val("2026-10-19"), "doc": val('{"c": 3}')}, {"d": day, "doc": doc})])
        sql = preview.statements[0].sql
        assert "CAST(d AS CHAR) = '0000-00-00'" in sql and "doc = CAST(" in sql
        assert tab.apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT CAST(d AS CHAR), CAST(doc AS CHAR) FROM {db.table(table)}") == [
            ("2026-10-19", '{"c": 3}')]


@pytest.mark.parametrize("sql_mode", [None, "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"])
def test_backslashes_are_stored_as_typed_in_both_sql_modes(tmp_path, sql_mode):  # A9, D-05-13, Review Focus 1
    db = MysqlScratch()
    app = EditApp(tmp_path)
    try:
        pid = app.add_profile(db.profile(options={"sql_mode": sql_mode} if sql_mode else {}))
        typed = "C:\\tmp\\new\\"
        with scratch(db, app.close_tabs) as make:
            table = items_table(db, make)
            preview = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"label": val(typed)},
                                                                       {"label": "a"})])
            written = "'C:\\tmp\\new\\'" if sql_mode else "'C:\\\\tmp\\\\new\\\\'"
            assert f"SET label = {written}" in preview.statements[0].sql
            assert app.tab(pid, None).apply(preview.plan_id).outcome.outcome == "committed"
            assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [(typed,)]
    finally:
        app.close()
        db.close()


def test_a_partial_apply_on_myisam_says_what_took_effect(my_env):  # D-05-6, Review Focus 5
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id BIGINT PRIMARY KEY, label VARCHAR(100) NOT NULL) ENGINE=MyISAM")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 'a'), (2, 'b')")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("one")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"label": val("two")}, {"label": "b"})])
        assert preview.warnings[0].code == "non_transactional"
        db.execute(f"UPDATE {db.table(table)} SET label = 'theirs' WHERE id = 2")
        done = app.tab(pid, None).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.applied, done.outcome.failed_change) == ("partial", ["u:1"], "u:2")
        assert done.error.message.endswith(": the statements before it stay applied (this table cannot roll back)")
        assert db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id") == [(1, "one"), (2, "theirs")]
        [entry] = [entry for entry in app.history() if entry.sql == preview.script]
        assert entry.status == "error" and entry.error.startswith("Partly applied (1 of 2): ")


def test_the_server_refuses_a_write_on_a_read_only_session(my_env):  # R-05-7: MySQL answers 1792
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        preview = app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})])
        app.update_profile(pid, db.profile(read_only=True))
        tab = app.tab(pid, None)  # SET SESSION TRANSACTION READ ONLY at connect
        app.update_profile(pid, db.profile())  # the policy lets it pass now: only the server stops it
        done = tab.apply(preview.plan_id)
        assert (done.outcome.outcome, done.error.code) == ("rolled_back", "1792")
        assert db.rows(f"SELECT COUNT(*) FROM {db.table(table)}") == [(3,)]


def test_defaults_auto_increment_bits_enums_and_generated_columns(my_env):
    db, app, pid = my_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id INT AUTO_INCREMENT PRIMARY KEY, "
                              "kind ENUM('a','b') NOT NULL DEFAULT 'a', bits BIT(8) NULL, "
                              "loud VARCHAR(10) GENERATED ALWAYS AS (UPPER(kind)) VIRTUAL) ENGINE=InnoDB")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {}),
                                                            insert("i:2", {"kind": val("b"), "bits": val("5")})])
        assert preview.statements[0].sql == f"INSERT INTO dbaguy.{table} () VALUES ()"
        assert app.tab(pid, None).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, kind, bits + 0, loud FROM {db.table(table)} ORDER BY id") == [
            (1, "a", None, "A"), (2, "b", 5, "B")]
        refused = app.preview(pid, db.path(table), updates=[update("u:1", {"id": 1}, {"loud": val("X")},
                                                                   {"loud": "A"})])
        assert [(p.column, p.code) for p in refused.problems] == [("loud", "read_only_column")]
```

- [ ] **Step 2: SQL Server** — append to `backend/tests/test_mssql_edits.py` (imports: `MssqlScratch, delete, edit_env, insert, items_table, scratch, update, val`):

```python
# --- live applies (Task 10) -----------------------------------------------------------------------------------------


@pytest.fixture
def ms_env(tmp_path):
    with edit_env("mssql", tmp_path) as made:
        yield made


def test_a_varchar_key_goes_without_n_and_unicode_text_with_it(ms_env):  # D-05-2, A8, ruling E17, Review Focus 1
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (code varchar(20) NOT NULL PRIMARY KEY, label varchar(50), "
                              "name nvarchar(50))")
        db.execute(f"INSERT INTO {db.table(table)} VALUES ('k42', 'old', N'old')")
        preview = app.preview(pid, db.path(table), updates=[update(
            "u:1", {"code": "k42"}, {"label": val("café 50%"), "name": val("名字")}, {"label": "old", "name": "old"})])
        assert preview.statements[0].sql == (
            f"UPDATE dbo.{table} SET label = N'café 50%', name = N'名字' "
            "WHERE code = 'k42' AND label = 'old' AND name = N'old'")
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT label, name FROM {db.table(table)}") == [("café 50%", "名字")]


def test_types_round_trip_and_datetime2_7_is_not_checked(ms_env):  # D-05-2, spec §3.2.2
    db, app, pid = ms_env
    names = ["amount", "at", "at7", "u", "flag", "data", "note"]
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, amount decimal(10,2), at datetime, "
                              "at7 datetime2(7), u uniqueidentifier, flag bit, data varbinary(16), note nvarchar(max))")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, 1.10, '2026-10-19T12:34:56.123', "
                   "'2026-10-19T12:34:56.1234567', 'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11', 1, 0x00ff, N'50% done')")
        tab = app.tab(pid, db.database)
        [row] = tab.rows(f"SELECT {', '.join(names)} FROM {db.table(table)}")
        new = {"amount": "2.25", "at": "2026-10-20 01:02:03", "at7": "2026-10-20 01:02:03.1234567",
               "u": "00000000-0000-0000-0000-000000000001", "flag": "false", "data": "0x0102", "note": "it's 100%"}
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {name: val(text) for name, text in new.items()}, dict(zip(names, row)))])
        assert [u.column for u in preview.statements[0].unchecked] == ["at7"]
        done = tab.apply(preview.plan_id)
        assert done.outcome.outcome == "committed", done.error and done.error.message
        assert db.rows(f"SELECT CONVERT(varchar(10), amount), CONVERT(varchar(27), at7, 121), data, note "
                       f"FROM {db.table(table)}") == [("2.25", "2026-10-20 01:02:03.1234567", b"\x01\x02", "it's 100%")]


def test_triggers_count_the_statements_own_row_and_their_prints_reach_the_apply(ms_env):  # A3, spec §3.5
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        log = make("TABLE", "CREATE TABLE {t} (n int)")
        db.execute(f"CREATE TRIGGER {db.q(table + '_after')} ON {db.table(table)} AFTER UPDATE AS BEGIN "
                   f"INSERT INTO {db.table(log)} VALUES (1), (2), (3); PRINT 'logged three rows' END")
        instead = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, v nvarchar(20))")
        db.execute(f"INSERT INTO {db.table(instead)} VALUES (1, N'a')",
                   f"CREATE TRIGGER {db.q(instead + '_instead')} ON {db.table(instead)} INSTEAD OF UPDATE AS "
                   f"UPDATE t SET v = i.v FROM {db.table(instead)} t JOIN inserted i ON i.id = t.id")
        tab = app.tab(pid, db.database)
        after = tab.apply(app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("x")}, {"label": "a"})]).plan_id)
        assert (after.outcome.outcome, after.affected) == ("committed", [(0, 1)])
        assert "logged three rows" in after.messages
        replaced = tab.apply(app.preview(pid, db.path(instead), updates=[
            update("u:1", {"id": 1}, {"v": val("b")}, {"v": "a"})]).plan_id)
        assert (replaced.outcome.outcome, replaced.affected) == ("committed", [(0, 1)])
        assert db.rows(f"SELECT v FROM {db.table(instead)}") == [("b",)]


def test_a_session_with_nocount_on_still_counts_inside_the_frame(ms_env):  # A2
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = items_table(db, make)
        tab = app.tab(pid, db.database)
        tab.run("SET NOCOUNT ON")  # what a server whose `user options` turn NOCOUNT on gives every session
        done = tab.apply(app.preview(pid, db.path(table), deletes=[delete("d:1", {"id": 1})]).plan_id)
        assert (done.outcome.outcome, done.affected) == ("committed", [(0, 1)])


def test_a_transaction_ending_error_rolls_back(ms_env):  # A15, Review Focus 5
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int PRIMARY KEY, label nvarchar(20), day date)")
        db.execute(f"INSERT INTO {db.table(table)} VALUES (1, N'a', '2026-01-01'), (2, N'b', '2026-01-01')")
        preview = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"label": val("changed")}, {"label": "a"}),
            update("u:2", {"id": 2}, {"day": val("not a date")}, {"day": "2026-01-01"})])
        done = app.tab(pid, db.database).apply(preview.plan_id)
        assert (done.outcome.outcome, done.outcome.failed_change, done.error.code) == ("rolled_back", "u:2", "241")
        assert not done.finished.in_transaction
        assert db.rows(f"SELECT label FROM {db.table(table)} WHERE id = 1") == [("a",)]


def test_identity_computed_and_rowversion_columns_and_default_values(ms_env):
    db, app, pid = ms_env
    with scratch(db, app.close_tabs) as make:
        table = make("TABLE", "CREATE TABLE {t} (id int IDENTITY PRIMARY KEY, label nvarchar(20) NOT NULL "
                              "DEFAULT N'none', twice AS (id * 2), ver rowversion)")
        preview = app.preview(pid, db.path(table), inserts=[insert("i:1", {}), insert("i:2", {"label": val("given")})])
        assert preview.statements[0].sql == f"INSERT INTO dbo.{table} DEFAULT VALUES"
        assert app.tab(pid, db.database).apply(preview.plan_id).outcome.outcome == "committed"
        assert db.rows(f"SELECT id, label, twice FROM {db.table(table)} ORDER BY id") == [(1, "none", 2), (2, "given", 4)]
        refused = app.preview(pid, db.path(table), updates=[
            update("u:1", {"id": 1}, {"twice": val("9"), "ver": val("0x01")},
                   {"twice": 2, "ver": {"$type": "bytes", "size": 8, "preview": "0000000000000001"}})])
        assert sorted((p.column, p.code) for p in refused.problems) == [
            ("twice", "read_only_column"), ("ver", "read_only_column")]
```

- [ ] **Step 3: Run them**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_edits.py tests/test_mssql_edits.py`
Expected: PASS (19 tests, about 5 seconds; planning ran them against the prototype: all passed). No `zz\_05a%` object left in either server (as Task 9 Step 5).

- [ ] **Step 4: Record A9 and A15** in the assumptions table (owner: this task): A9 — `test_backslashes_are_stored_as_typed_in_both_sql_modes`; A15 — `test_a_transaction_ending_error_rolls_back` (a conversion error, 241, inside the framed transaction). If one behaves otherwise, write the result over its row and apply its Effect.

- [ ] **Step 5: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/tests/test_mysql_edits.py backend/tests/test_mssql_edits.py
git commit -m "test: grid edits on MySQL (matched rows, zero dates, sql_mode, MyISAM, READ ONLY) and SQL Server (N, triggers, NOCOUNT)"
```

---
### Task 11: Frozen-sidecar check, A6 and A7, CLAUDE.md and the spec

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-19-05-grid-editing-design.md`
- Modify (only if Step 2 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/t11_probe.py`

**Interfaces:**
- Consumes: everything above; `tests/edit_support` (`scratch_db`, `scratch`, `items_table`, the change builders); 04c Task 8's procedure (the frozen sidecar in `src-tauri/resources/backend`, `scripts/measure-startup.sh`).
- Produces: a bundle whose frozen sidecar previews and applies grid edits on all four relational engines; A6 and A7 measured; 05a's backend facts in CLAUDE.md and the spec.

- [ ] **Step 1: Build the sidecar.** Run: `npm run build:backend`. Expected: passes; `du -sk src-tauri/resources/backend` within about 100 KB of 04c's 49,148 KB (only new pure-Python modules, no dependency).

- [ ] **Step 2: Drive the frozen sidecar.** Save as `<scratchpad>/t11_probe.py` and run from the repository root: `backend/.venv/bin/python <scratchpad>/t11_probe.py src-tauri/resources/backend/dbaguy-backend` (about 40 seconds with the 1,000,000-row tables; it starts the sidecar as its own child with a throwaway `DBAGUY_DATA_DIR`, profiles saved without passwords, only `zz_05a_*` scratch tables dropped by exact name, and stops the sidecar by its own handle):

```python
"""05a packaged check: the frozen sidecar (started here as a child, with a throwaway DBAGUY_DATA_DIR, and stopped here by
its own handle) answers /edits/target and /edits/preview and applies the plan over the session WebSocket on every
relational engine; then A7 (1,000 UPDATEs previewed and applied) and A6 (a paused browse of 1,000,000 rows ended
before an apply). Only zz_05a_* scratch tables, made and dropped by exact name through tests.edit_support.

Run from the repository root: backend/.venv/bin/python <this file> <sidecar command…>"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

from websockets.sync.client import connect

sys.path.insert(0, "backend")
from dbaguy_backend.sql.select_builder import build_browse_select  # noqa: E402
from tests.edit_support import delete, insert, items_table, scratch, scratch_db, update, val  # noqa: E402

TOKEN = "t11"
command = sys.argv[1:]
data_dir = tempfile.mkdtemp(prefix="dbaguy-t11-")
env = {**os.environ, "DBAGUY_TOKEN": TOKEN, "DBAGUY_PORT": "0", "DBAGUY_LOG_TO_FILE": "false",
       "DBAGUY_DATA_DIR": data_dir, "DBAGUY_LOG_LEVEL": "WARNING"}
sidecar = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
port = json.loads(sidecar.stdout.readline())["port"]
print(f"sidecar pid {sidecar.pid} on port {port}, data dir {data_dir}")


def call(method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            text = response.read()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class Tab:
    def __init__(self, profile_id: str, database: str | None) -> None:
        self.session = call("POST", "/sessions", {"profile_id": profile_id, "database": database})[1]["session_id"]
        self._stack = ExitStack()
        self.ws = self._stack.enter_context(
            connect(f"ws://127.0.0.1:{port}/sessions/{self.session}/ws?token={TOKEN}", max_size=None))

    def send(self, command: dict) -> list[dict]:
        """The request's events until its `finished`, or until an execute pauses."""
        self.ws.send(json.dumps(command))
        events = []
        while True:
            event = json.loads(self.ws.recv())
            if event.get("request_id") != command["request_id"]:
                continue
            events.append(event)
            if event["type"] == "finished" or (event["type"] == "result_set_end" and event["has_more"]):
                return events

    def apply(self, rid: str, plan_id: str) -> tuple[dict, float]:
        started = time.perf_counter()
        events = self.send({"type": "apply_edits", "request_id": rid, "plan_id": plan_id})
        outcome = next(event for event in events if event["type"] == "edit_outcome")
        error = next((event for event in events if event["type"] == "error"), None)
        return {**outcome, "error": error and error["code"]}, time.perf_counter() - started

    def close(self) -> None:
        self._stack.close()
        call("DELETE", f"/sessions/{self.session}")


def path_of(db, table: str) -> list[dict]:
    return [segment.model_dump() for segment in db.path(table)]


try:
    for engine in ("postgres", "mysql", "mssql", "sqlite"):
        db = scratch_db(engine, Path(tempfile.mkdtemp(prefix="dbaguy-t11-lite-")))
        fields = {"name": f"t11 {engine}"} | ({} if engine == "sqlite" else {"save_password": False})
        profile_id = call("POST", "/profiles", db.profile(**fields))[1]["profile"]["id"]
        tabs: list[Tab] = []

        def close_tabs() -> None:
            while tabs:
                tabs.pop().close()

        try:
            with scratch(db, close_tabs) as make:
                table = items_table(db, make)
                path = path_of(db, table)
                target = call("POST", "/edits/target", {"profile_id": profile_id, "path": path})[1]
                changes = {"updates": [update("u:1", {"id": 1}, {"label": val("t11 é 😀 50%")}, {"label": "a"})],
                           "inserts": [insert("i:4", {"id": val("9007199254740993"), "label": val("new")})],
                           "deletes": [delete("d:3", {"id": 3})]}
                status, preview = call("POST", "/edits/preview", {"profile_id": profile_id, "path": path,
                                                                  "changes": changes})
                tabs.append(tab := Tab(profile_id, db.database))
                outcome, _ = tab.apply("a1", preview["plan_id"])
                again, _ = tab.apply("a2", preview["plan_id"])
                rows = db.rows(f"SELECT id, label FROM {db.table(table)} ORDER BY id")
                print(f"{engine}: key {target['key']}, preview {status} with {len(preview['statements'])} statements, "
                      f"{outcome['outcome']} {outcome['applied']}, again {again['error']}; rows {rows}")

                thousand = items_table(db, make, [])
                db.fill(db.table(thousand), 1000, 1)
                updates = [update(f"u:{i}", {"id": i}, {"label": val(f"x{i}")}, {"label": f"row {i}"})
                           for i in range(1, 1001)]
                started = time.perf_counter()
                big_preview = call("POST", "/edits/preview", {"profile_id": profile_id, "path": path_of(db, thousand),
                                                              "changes": {"updates": updates}})[1]
                previewed = time.perf_counter() - started
                outcome, applied = tab.apply("a3", big_preview["plan_id"])
                print(f"  A7: preview of 1,000 updates {previewed * 1000:.0f} ms, apply {applied:.2f} s → "
                      f"{outcome['outcome']}")

                million = items_table(db, make, [])
                db.fill(db.table(million), 1_000_000, 1)
                browse = build_browse_select(db.dialect, db.path(million), None, [])
                paused = tab.send({"type": "execute", "request_id": "b1", "sql": browse, "page_size": 500,
                                   "history": False})
                assert paused[-1]["type"] == "result_set_end" and paused[-1]["has_more"]
                one = call("POST", "/edits/preview", {"profile_id": profile_id, "path": path_of(db, million), "changes": {
                    "updates": [update("u:1", {"id": 1}, {"label": val("x")}, {"label": "row 1"})]}})[1]
                outcome, applied = tab.apply("a4", one["plan_id"])
                print(f"  A6: with a paused 1,000,000-row browse, the apply took {applied:.2f} s → {outcome['outcome']}")
        finally:
            db.close()
finally:
    sidecar.terminate()  # this probe's own child, by its handle
    sidecar.wait(timeout=30)
    print("sidecar stopped")
```

Expected (planning ran this script against the in-process prototype served by `python -m dbaguy_backend`; the frozen sidecar must give the same): for each of postgres, mysql, mssql, sqlite: `key ['id'], preview 200 with 3 statements, committed ['d:3', 'u:1', 'i:4'], again plan_expired; rows [(1, 't11 é 😀 50%'), (2, 'b'), (9007199254740993, 'new')]`; A7 previews of 1,000 updates in 20–100 ms and applies of about 0.8 s (PostgreSQL), 0.7 s (MySQL), 5–6 s (SQL Server) and 0.4 s (SQLite) — the budgets are 3 s and, for SQL Server, 10 s (spec §7); A6 applies behind a paused 1,000,000-row browse in at most about 0.3 s (MySQL's drain after KILL and SQL Server's attention are the slow ones); `sidecar stopped`. A module the frozen sidecar cannot import goes into `hiddenimports` in `backend/dbaguy-backend.spec`; rebuild and repeat. A measurement over its budget is recorded (CLAUDE.md and the spec) with the §12 batching carry; more than twice the budget is reported to the coordinator before going on. Afterwards no `zz\_05a%` object is left in any server (as Task 9 Step 5).

- [ ] **Step 3: Build the app, check signatures, measure startup.** Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`, then `scripts/measure-startup.sh` three times. Expected: cold start and footprint within noise of 04c (1.03–1.11 s; `footprint` 176–184 MB, backend 74–78 MB); record them. Do not look at or touch `~/Library/Application Support/DbaGuy` (the script uses a temporary data dir).

- [ ] **Step 4: Update `CLAUDE.md`** (keep its language and style; 05b writes the final "Grid 內編輯" section and the frontend parts):
  - **Repo 結構:** `domain/` 加 `edits.py`（grid 編輯的模型：EditTarget、EditChanges、EditPreview、EditPlan）; `sql/` 加 `literals.py`（cell 的正規化與 literal、比對條件：Copy、Filter by this value 與 DML 共用；從 `formatting.py` 抽出）與 `dml.py`（grid commit 的 DML：純函式）; `adapters/` 加 `editing.py`（key 的選法，與 engine 無關）與每個關聯式 engine 的 `editing.py`（edit target：catalog 讀 key、kind、能不能寫、能不能比）; `services/` 加 `edit_service.py`（target 與預覽）、`edit_plans.py`（一次性 plan）; `sessions/` 加 `edit_apply.py`（apply 的結果分類，純函式）; `api/` 加 `edits.py`（`/edits/target`、`/edits/preview`；套用是 WS 的 `apply_edits`）.
  - **架構重點:** a bullet 「Grid 內編輯（⑤：05a 為 backend；前端是 05b）」 with:
    - 只有表格資料 tab；能力 `row_editing`（PG、MySQL、SQL Server、SQLite；Couchbase 沒有，文件走 04c）。`/edits/target` 由 profile 的 metadata 連線讀 catalog（`RowEditing.edit_target`），不快取；key 是 PK，否則 NOT NULL 欄位最少的 UNIQUE（不含 partial、expression、INCLUDE、filtered、disabled、functional key part，PG composite 型別的 key 不算），要每個 key 欄都能精確比對；view、沒有 key 的表、唯讀連線回 `editable=False` 與原因。
    - `/edits/preview`：`sql/dml.py` 在 request 執行緒產生每列一句的 DELETE → UPDATE → INSERT（各組依暫存順序），夾在 dialect 的交易外框之間（PG `BEGIN`／`COMMIT`；MySQL `START TRANSACTION`；SQL Server `SET NOCOUNT OFF;` 加 `BEGIN TRANSACTION`／`COMMIT TRANSACTION`；SQLite `BEGIN IMMEDIATE`）。UPDATE 只 SET 改的欄，WHERE 是 key 加上改的欄的原始值：PG `::text = '原文'`（bpchar 用 `=`）、MySQL JSON `CAST(… AS JSON)`、零日期 `CAST(c AS CHAR)`、反斜線依 metadata 連線的 `@@sql_mode`、SQL Server 非 Unicode 欄的 ASCII 不加 `N`（SQL collation 下 `N'…'` 會讓 key 變成 scan，A8）、SQLite 依 cell 型別（沒有型別的欄裡超過 2^53 的整數寫 `IN (n, 'n')`）；原始值是 NULL 寫 `IS NULL`；比不了的欄（浮點、datetime2(7) 等）列在 `unchecked`。值的檢查與問題逐 cell（`EditProblem`），有問題就沒有 plan；lone surrogate 在 request model 就被拒（422，`loc` 指到那個 cell）。plan 存在記憶體（`EditPlanStore`：uuid4、只能用一次、15 分鐘、最多 32 份），只有同一個 profile、而且 path 有 database 時同一個 database 的 session 取得到。
    - 套用：WS `apply_edits{plan_id}`，在 tab 自己的 session（先結束暫停中的瀏覽結果），worker 只送 plan 的文字；送出前每句 DML 過 `SafetyPolicy`（唯讀 → BLOCK，什麼都不送；server 端也擋：MySQL 1792 等）、已有交易就拒絕。每句先讀 `matched_rows()`（MySQL 是 OK 封包 info 的 `Rows matched`，不改 client flag）再把回應讀完（SQL Server 不用 attention）；UPDATE／INSERT 要剛好 1 列，DELETE 0 列算「已不存在」（`already_gone`），讀不到列數 fail closed。COMMIT 前 `_set_running(None)`：之後 Cancel 不會到 driver。結果 `committed`／`rolled_back`／`partial`（非交易引擎，已生效的列在 `applied`）／`unknown`（COMMIT 途中斷線、COMMIT 前交易無聲消失或 worker 例外），每個 apply 都以一個 `edit_outcome` 結束（plan 過期、唯讀、被取消的也是），從不重送；COMMIT 被拒會 rollback（SQLite 忙碌的 COMMIT 會留著交易）。History 記 plan 的 `script`（unknown 記為 `Outcome unknown: …`）。
  - **已知限制:** add 「Grid 內編輯（05a）」: 浮點、datetime2／datetimeoffset／time(7)、text／ntext／image／xml、sql_variant 等不檢查並行修改（預覽逐句列出）；MySQL／SQL Server 依 collation 比較（只改大小寫的並行修改抓不到）；兩列互換唯一值、key 互換會在中途撞唯一鍵而 rollback；MyISAM 等非交易引擎中途失敗為 partial；SQL Server 每句多 2 次往返（1,000 句約 5–6 秒，A7）；INSTEAD OF trigger 回報的是 WHERE 符合的列數（trigger 實際做了什麼不算，A3），trigger 回傳結果集時讀不到列數（rolled back）；非 Unicode 欄寫入非 ASCII 文字時由 server 轉成欄位的 code page（之外的字元變 `?`）；server 預設開交易的連線（MySQL `autocommit=0`、SQL Server `IMPLICIT_TRANSACTIONS`）在表格資料 tab 套用時被拒（`transaction_open`）；`SET NOCOUNT OFF` 留在那個 session；SQLite 的 COMMIT 被另一條連線的讀取擋住時，busy timeout（5 秒）後 rollback.
  - **量測:** Step 1's sidecar size, Step 2's A6 / A7 per engine, Step 3's startup and footprint (under the 04c line, as "05a 時量").
  - **進度 ⑤:** `進行中：05a 完成（backend：edit target、預覽 DML、一次性 plan、在 tab 的 session 套用、結果四分類；followups：05a note）；05b（前端）待做`（the coordinator writes the 05a note and links it).

- [ ] **Step 5: Update the spec** (record next to the text each refines; do not rewrite history):
  - §10: A1, A2, A3, A5, A8 〔實測〕(05a T1) with Task 1's results; A4, A13, A14 (05a T9); A9, A15 (05a T10); A6, A7 (05a T11) with Step 2's numbers.
  - §3.2.2: MySQL's not-writable rule reads `VIRTUAL GENERATED`／`STORED GENERATED`（`DEFAULT_GENERATED` 是預設值運算式，可寫，E15）; the key rules gain the exclusions of E4 (functional key parts, INCLUDE, filtered, disabled, invalid indexes; a PG composite key) and "the first candidate whose columns are all comparable".
  - §3.3.3: kind for SQLite NUMERIC affinity (E6) and the big-integer `IN` (E7); `default_not_allowed` (E8); a lone surrogate is a 422 at the model (E19); MySQL NUL under NO_BACKSLASH_ESCAPES is `invalid_text` (E16); NULL originals always `IS NULL` (E5); with problems, `statements` is empty (E8).
  - §3.4: a read-only profile's `/edits/target` answers `editable=False` without connecting (E20).
  - §3.5 and D-05-8: every apply ends with one `edit_outcome`, refusals and skips included (E1); `already_gone` (E2); answers read to the end (E9); a refused COMMIT is followed by a rollback (E10); non-transactional targets (E11); the checks before COMMIT (E12); History statuses (E14).
  - §4: the codes `not_editable`, `plan_expired`, `plan_mismatch`, `edit_conflict`, `row_count_unknown`, `transaction_failed`, `outcome_unknown` as implemented.

- [ ] **Step 6: Report the backend items of the user's final test list** in the task handoff (05b adds the UI items), each with what the dev servers already showed:
  1. Company MySQL: `SELECT @@sql_mode`; a value with backslashes saved through a scratch table comes back as typed (dev: both modes, A9); a MyISAM table, if any, warns and reports partial; whether any server opens a transaction by default (`autocommit=0`): the apply is then refused with `transaction_open`.
  2. Company SQL Server: `user options` (NOCOUNT, IMPLICIT_TRANSACTIONS); the collation of varchar keys (dev: SQL collation, `'k42'` seeks, A8); tables with AFTER / INSTEAD OF triggers (dev: counts as A3); alias types.
  3. Company PostgreSQL: tables with domains, enums, arrays, composite columns, timestamptz in the company's TimeZone (dev: A4 in Asia/Taipei).
  4. A 1,000-change commit on each server and its time (dev: A7).

- [ ] **Step 7: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green.

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-19-05-grid-editing-design.md
git commit -m "docs: 05a in CLAUDE.md and the spec (grid-edit backend: target, preview, plan, apply); ⑤ 05a done"
```

(`backend/dbaguy-backend.spec` too, only if Step 2 changed it.)

---
## Carries (to 05b and after ⑤)

| Carry | Where |
|---|---|
| The whole frontend of spec §3.6 and the workspace `staged_edits` of §3.7. The contract it builds on: `EditTarget` (with `reason` for the toolbar, also for a read-only profile), `EditPreview` (`problems` per change id and column; `statements` empty with problems), WS `apply_edits` → exactly one `edit_outcome` per apply (`applied`, `already_gone`, `failed_change`, `statement`), error codes `plan_expired` / `plan_mismatch` (preview again), `read_only`, `transaction_open`, `edit_conflict`, `row_count_unknown`, `cancelled`, `transaction_failed`, `outcome_unknown`, `connection_lost` and the server's own. A request missing `edit_outcome` (socket lost, backend restarted) is unknown (R-05-6). | 05b |
| 05b sends an `original` for every column an UPDATE sets (the model refuses one without it) and never a `default` cell in an UPDATE (`default_not_allowed`); a lone surrogate comes back as a 422 whose `detail[].loc` names the cell (E19); `execution.ts`'s `TERMINAL_ERROR_CODES` decides how the new codes end a request. | 05b |
| SQL Server spends three round trips per statement (state read, describe, the statement): 1,000 statements take about 5–6 s (A7). Batching statements (each still needs its own count), or skipping the describe for plan statements, is the follow-up. | spec §12 |
| Stronger concurrency checks (PostgreSQL `xmin`, SQL Server rowversion), binary-collation comparisons, an "insert only" mode for keyless tables, editing values beyond 64 KiB, query-result editing. | spec §12 |

## Planning rulings

### Assumptions recorded during execution

Task 1 re-runs the probes; the Result column is what they showed while planning (2026-10-19 on the dev containers: PostgreSQL 17.11, MySQL 8.4.11, SQL Server 2022 CU27, SQLite 3.53.4; scratch tables `zz_plan05a_*` dropped by exact name, none left behind). A4, A9, A13, A14 and A15 were also pre-checked while planning, through a prototype of Tasks 1–10 (see the Self-review); their owner task re-verifies them with its tests. The owner writes a differing result over the row and applies the Effect; nothing else in the plan needs editing for the outcomes listed.

| # | Assumption | Owner | Result | Effect on later tasks |
|---|---|---|---|---|
| A1 | Keeping `cmd_query`'s dict in `_VerbatimCursor` gives the UPDATE's `Rows matched` | Task 1 | Holds: `info_msg` `Rows matched: 1  Changed: 0  Warnings: 0` for a same-value UPDATE (also `1.10` → `1.1` in DECIMAL) with `affected_rows` 0; `Rows matched: 2  Changed: 2`; `Rows matched: 0` for no row; no info for a one-row INSERT and for DELETE; `Records: 2  Duplicates: 0` for a two-row INSERT | Holds: Task 6 as written. Not: Task 6 connects every MySQL session with the `FOUND_ROWS` client flag instead (a user's own UPDATE then reports matched rows too) and CLAUDE.md says so. |
| A2 | Under NOCOUNT ON (a server's `user options`) `rows_affected` is -1; the `SET NOCOUNT OFF;` frame restores it | Task 1 | Holds: `affected_rows()` None under NOCOUNT ON; 1 inside the frame | Holds: the frame as written (Task 3). Not: Task 8 fails closed (`row_count_unknown`) on such servers; CLAUDE.md's known limitations say so. |
| A3 | SQL Server counts with triggers | Task 1 | AFTER trigger inserting 3 rows: 1. INSTEAD OF trigger: the rows the statement's WHERE qualified (1, or 0), whatever the trigger did (nothing, every row, NOCOUNT ON inside); one that returns a result set leaves no count (None) | As planned: exactly-1 is about the WHERE; a trigger with a result set fails closed (`row_count_unknown`, E9); both in CLAUDE.md's known limitations (Task 11). |
| A4 | PostgreSQL `c::text` equals the text-first loaded value for every type | Task 9 | Holds (pre-checked in UTC and Asia/Taipei): int4, int8, numeric, timestamptz, timetz, interval, money, int4range, tstzrange, text[], jsonb[], json, jsonb, xml, point, uuid, bool, bytea, date, inet, bit varying, real, a domain over int, a composite, an array of composites; not bpchar (padding: `equals`); `=` fails for json, xml, point (42883) and composites (0A000) | Holds: as written. Not for a type: Task 5 gives that type `equals` (or `none` when `=` fails) and Task 9's test names it. |
| A5 | BEGIN / COMMIT through `run()` keep every adapter's transaction state right | Task 1 | Holds: `in_transaction()` True after each frame and False after COMMIT or `rollback()` on all four; PostgreSQL's error leaves `transaction_failed()` True; MySQL's, SQLite's and SQL Server's duplicate-key errors keep the transaction open; SQLite's idle `cancel()` does not stop the next statement | Holds: Task 8 as written. Not: the adapter whose cache lags gets a `_refresh_state`-style re-read after `run()` of a frame text, in Task 8, with a fake-free test in Task 9. |
| A6 | Ending a paused browse of 1,000,000 rows before an apply | Task 11 | Pre-measured in-process: PostgreSQL ≤ 0.01 s, MySQL 0.19–0.30 s (KILL and drain), SQL Server 0.22–0.27 s (attention), SQLite ≤ 0.01 s | Recorded in CLAUDE.md. |
| A7 | 1,000 UPDATEs: PostgreSQL / MySQL / SQLite ≤ 3 s, SQL Server ≤ 10 s | Task 11 | Pre-measured (in-process and over HTTP + WS): previews 13–102 ms; applies PostgreSQL 0.78–0.92 s, MySQL 0.67–0.74 s, SQL Server 5.3–6.3 s, SQLite 0.37–0.41 s | Within budget; the SQL Server carry stands. Over budget: recorded, and reported when over twice. |
| A8 | A varchar key written without N keeps its seek under a SQL collation | Task 1 | Holds (SHOWPLAN_XML, `SQL_Latin1_General_CP1_CI_AS`, 2,000 rows): `'k42'` → Clustered Index Seek; `N'k42'` → Clustered Index Scan | Holds: E17 as written. Not: the ASCII rule is dropped (always `N'…'`) and the scan is a known limitation. |
| A9 | Under NO_BACKSLASH_ESCAPES a literal without doubled backslashes stores the value | Task 10 | Holds (pre-checked): `C:\tmp\new\` stored as typed in both modes | Holds: as written. Not: Task 6 marks such targets not editable ("this server's sql_mode … ") and CLAUDE.md says so. |
| A10–A12 | WKWebView keys, IME in the inline input, the refresh keeping position | 05b | — | — |
| A13 | A serialization failure or deadlock found at COMMIT rolls back | Task 9 | Holds (pre-checked with deferred constraint triggers raising 40001 and 40P01 at COMMIT): `rolled_back`, `statement` None, not in a transaction, nothing applied; the deferred FK (23503) the same | Holds: as written. |
| A14 | SQLite `BEGIN IMMEDIATE` beside another session's reader | Task 9 | Holds (pre-checked): with another session's paused SELECT holding its read lock, the BEGIN and the UPDATE go through and COMMIT answers `database is locked` (SQLITE_BUSY) after the 5 s busy timeout; the worker rolls back (the transaction stays open otherwise, E10) → `rolled_back` | Holds: as written. |
| A15 | After a transaction-ending SQL Server error `rollback()` is quiet and the outcome rolled back | Task 10 | Holds (probe: 245 on `CONVERT(int, 'x')`, `in_transaction()` False, `rollback()` without error; pre-checked through the apply with 241 on a bad date: `rolled_back`, nothing applied) | Holds: as written. |

### Rulings made while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| E1 | Every apply ends with exactly one `edit_outcome` before its `session_state` and `finished`, also one refused before anything was sent (`plan_expired`, `plan_mismatch`, `read_only`, `transaction_open`, `invalid_text`: `rolled_back` with the error) and one cancelled while queued (`started`, `message`, `edit_outcome`, `session_state`, `finished(cancelled)`). | R-05-6 tells the frontend to treat an apply that ends without `edit_outcome` as unknown; a refusal must not read as "may have been applied". | None. |
| E2 | `EditOutcomeEvent.already_gone` lists the DELETEs that matched no row. | §3.6.5's toast "1 row was already gone"; explicit beats re-deriving it from `affected` and the statements' kinds. | One field. |
| E3 | `EditPlan.database` is the path's database segment (`path_database`), None when the path has none (MySQL, SQLite); a session takes a plan when the profile matches and the plan's database is None or the session's. A mismatched take leaves the plan for its own session. The plan keeps its table as the statements write it, not the path: the path matters only through its database. | CR-2's binding (D-05-7's "綁定 profile＋database＋path"); MySQL and SQLite sessions report a current database their paths never name. | None. |
| E4 | Key candidates: the primary key, then unique keys on NOT NULL columns by (column count, name); the first whose every column is comparable wins (a float PK with a unique integer column stays editable). Never candidates: PostgreSQL partial, expression or invalid indexes, INCLUDE columns, a composite-type column; MySQL functional key parts; SQL Server filtered or disabled indexes, included columns. A SQLite PRIMARY KEY may hold NULL: such a row is `key_incomplete`. | R-05-2 ("PK; 否則…最少的 UNIQUE") plus what each catalog adds; `=` against a PostgreSQL composite fails (0A000, measured). | A table can be keyed by a unique key while its PK exists (only when the PK cannot be matched). |
| E5 | A NULL original is always checked as `c IS NULL`, whatever the column's compare. | Exact for every type. | None. |
| E6 | SQLite kinds follow the affinity rules in order (INT; CHAR, CLOB, TEXT; BLOB; no type → other; REAL, FLOA, DOUB → number, not compared), and inside NUMERIC affinity the declared name decides: DATE / TIME → datetime, BOOL → bool, JSON → json, else number. | NUMERIC affinity catches `DATETIME`, `BOOLEAN`, `JSON` declarations whose values are text or 0/1: as numbers, `2026-10-19` would be `invalid_number`. | A NUMERIC-affinity column with an odd name is a number. |
| E7 | In a SQLite column without a declared type, a key or original string that is an integer beyond ±(2⁵³−1) is matched with `c IN (n, 'n')`. | `encode_value` sends such integers as strings, and with no affinity `'n'` never equals the stored integer: every UPDATE would conflict, every DELETE be "gone". | A text value with the same digits matches too (then > 1 row → rolled back). |
| E8 | With any problem the preview has `plan_id` None, `statements` empty and `script` ""; `begin`, `commit`, `counts` and `warnings` stay. `default_not_allowed` for DEFAULT in an UPDATE. `CellInput.text` takes up to `VALUE_ACCEPTED_CHARS` (4 × the limit) and the builder reports `value_too_large` above `MAX_VALUE_CHARS`. A malformed request is a 422 at the model: no change, more than `MAX_CHANGES`, duplicate change ids, an UPDATE without a column or a set column without its original, text with null / default or a value without text. | The dialog shows problems or statements, never both; problems need their cell, request-shape errors are client bugs. | None. |
| E9 | The worker reads a statement's `matched_rows()` first, then reads its answer to the end (`next_result()` until false) and closes it, forwarding messages. A first result set with columns (a SQL Server trigger's SELECT) has no count → `row_count_unknown`. | SQL Server stops an unread result with an attention (a round trip; with XACT_ABORT ON it would roll back); a trigger's NOTICE or PRINT belongs to the apply. | A trigger that returns rows makes its table uneditable (fails closed). |
| E10 | A COMMIT that fails with anything but `connection_lost` is followed by `rollback()`, then `rolled_back`. | SQLite keeps the transaction after a busy COMMIT (A14); elsewhere the rollback is a no-op. | None. |
| E11 | A non-transactional target (MySQL MyISAM…): the first failing statement with nothing applied is `rolled_back`, a later one `partial` (`applied` = those before), a lost connection during a statement `unknown` (`applied` = those before); a Cancel landing after the last statement does not stop the COMMIT, and a failing COMMIT is `committed` with a warning. | Every statement of such a table takes effect as it runs; R-05-6's partial is "some took effect". | None. |
| E12 | Before COMMIT (transactional): a Cancel that landed → rollback → `rolled_back`; `transaction_failed()` → rollback → `rolled_back`; `in_transaction()` false → `unknown` (spec step 7). A worker bug before BEGIN went out → `rolled_back`; after → `unknown`, with a rollback attempted unless COMMIT went out. | R-05-6 and spec §3.5 step 7, made explicit for each check. | A bug after BEGIN shows "may have been applied" although the rollback ran. |
| E13 | The policy checks every DML statement before BEGIN goes out (spec step 3), about 0.4 ms each (0.35–0.5 s per 1,000, measured while planning). | "BLOCK → read_only，什麼都不送"; checking as they go could block half-way. | Up to 4–5 s of checks before a 10,000-change commit starts. |
| E14 | History: committed → `ok`; a cancel that rolled back → `cancelled`; rolled_back → `error` with the error; partial → `error` "Partly applied (n of m): …"; unknown → `error` "Outcome unknown: …"; `rows` = the rows matched; only once BEGIN went out (`req.ran`). | D-05-9 with History's existing statuses (`cancelled` exists). | None. |
| E15 | MySQL's generated columns are those with `VIRTUAL GENERATED` or `STORED GENERATED` in EXTRA. | `DEFAULT_GENERATED` (an expression default such as CURRENT_TIMESTAMP) contains "GENERATED" too, and is writable. | None. |
| E16 | Under NO_BACKSLASH_ESCAPES a MySQL value holding U+0000 is `invalid_text`; `quote_literal(…, backslash_escapes=False)` raises ValueError for one. | MySQL's `\0` escape needs backslash escapes. | Such a value needs SQL. |
| E17 | T-SQL: `N'…'` unless the column is non-Unicode (char, varchar, text) and the text ASCII; datetime literals keep N. Non-ASCII text for a non-Unicode column goes as `N'…'` and the server converts it to the column's code page. | A8: a varchar key written `N'…'` scans under a SQL collation; correctness first for non-ASCII. | Characters outside the code page become `?` (as in SSMS). |
| E18 | `PlannedStatement.row` shows the key as text (`id = 42`) for the user's messages; it is never logged. | §4's `<key>`. | None. |
| E19 | A lone surrogate in a typed value is refused by the request model (pydantic, 422 whose `loc` names the cell); a key or original string holding one has no match term (the key is `key_incomplete`, the original unchecked); `input_literal` still refuses one (defence in depth). | Measured while planning: pydantic refuses lone surrogates in `str` fields but `JsonValue` passes them, and no driver can send them. | None. |
| E20 | `/edits/target` of a read-only profile answers `editable=False`, reason "This connection is read-only", without connecting; an engine without `row_editing` is 422 `not_editable` on both routes. | The toolbar's reason (G8) without a round trip; Couchbase has no table-data tab. | None. |
| E21 | The live tests run in-process (`AppContainer` with in-memory secrets over a temporary data dir: EditService, the hooks, SessionManager, the real adapters); the HTTP routes are tested with SQLite (Task 7) and the whole path over the network by the frozen sidecar (Task 11). | One harness for 60-odd live tests; HTTP adds nothing per engine. | None. |
| E22 | `unchecked` reasons: `<type_name> values cannot be compared exactly` (compare none) and `only a preview of the value was loaded` (a cut original). | EditColumn needs no extra field. | None. |
| E23 | PostgreSQL reads a domain through its base type (one level); `money` is `other` (its text follows lc_monetary). | Kind and compare come from what the value is. | A domain over a domain is read through the inner domain's name (other). |
| E24 | `EditPlanStore` lives in `services/edit_plans.py`, owned by `EditService` (`edits.plans`), and the hooks factory takes the store, not the service. | The session hooks need only `take()`; the store has no metadata or registry dependency. | None. |
| E25 | `affected(index, rows)` goes out only when the count is known (rows ≥ 0). | `AffectedEvent.rows` is an int; an unknown count is the error that follows. | None. |

## Spec issues found while planning

- §3.2.2's MySQL rule "EXTRA 含 `GENERATED`" also matches `DEFAULT_GENERATED`, which is a writable column with an expression default (E15).
- §3.2.2's key rules miss MySQL functional key parts, PostgreSQL INCLUDE columns and invalid indexes, SQL Server included columns and disabled indexes; a PostgreSQL composite-type key cannot be compared with `=` (0A000, measured); and choosing the PK alone makes a table with a float PK read-only even when a unique integer key exists (E4).
- §3.3.3 / §3.2.2: SQLite kinds by affinity alone turn `DATETIME`, `BOOLEAN` and `JSON` declarations (NUMERIC affinity) into numbers (E6); integers beyond 2⁵³ in a column without a type never match their string cell (E7).
- §3.3.3 has no problem code for DEFAULT in an UPDATE (E8); its `invalid_text` for a lone surrogate is unreachable through the API (pydantic answers 422 first, E19); MySQL's NUL escape needs backslash escapes (E16).
- §3.5 / R-05-6: an apply refused or skipped before anything was sent has no `edit_outcome`, while the frontend must treat a missing one as unknown (E1); the non-transactional case says nothing about a late Cancel or a failing COMMIT (E11).
- D-05-4: a refused SQLite COMMIT (busy) leaves its transaction open; a rollback must follow (E10).
- §3.5 step 6 reads `matched_rows()` and closes the result: on SQL Server an unread answer is stopped with an attention (E9).
- D-05-9 names only ok / error; History's `cancelled` status fits a cancelled apply (E14).
- A15's example is 8114; the dev server answered 245 (and 241 for a date) with the same effect (`@@TRANCOUNT` 0) — recorded, nothing changes.
- A3 resolved: INSTEAD OF triggers report the rows the WHERE qualified, whatever they do, and a trigger returning a result set leaves no count (fails closed); both become known limitations.

## Self-review

- **Spec coverage (§11 item 1):** T1 probes A1, A2, A3, A5, A8 → Task 1 (results above). `sql/literals.py` extracted unchanged → Task 2; the literal and match rules of §3.3.3 → Task 3; `sql/dml.py` and §3.3.2's statement shapes, D-05-3's order, the warnings and problems → Task 4; the dialect's transaction frame, DEFAULT VALUES and the backslash option → Task 3. `domain/edits.py` → Task 3; `RowEditing`, `row_editing`, `EngineInfo.row_editing`, `matched_rows()` → Task 5; the four `edit_target`s (§3.2.2) → Tasks 5–6; MySQL's `Rows matched` (D-05-5) → Task 6. `EditService`, `EditPlanStore` (D-05-7, CR-2), `/edits/target`, `/edits/preview` (§3.4, §4's 403 / 422 / 428) → Task 7. The worker's `_apply_edits` (§3.5's steps and outcome table, D-05-8's events), `ApplyEditsCommand`, `EditOutcomeEvent`, `take_edit_plan` in the hooks, History (D-05-9) → Task 8; `npm run gen:api` → Tasks 5, 7, 8. §6's unit list → Tasks 2–8 (literals per dialect × kind, keys and originals, DEFAULT VALUES, order, every problem and warning, the limits, the formatting regression, the plan store, `Rows matched`, every outcome row with fakes, event order); §6's integration list → Tasks 5–6 (targets: PK, unique, no key, view, generated / identity / computed / rowversion, compare kinds, MyISAM, `@@sql_mode`) and Tasks 9–10 (big integers, decimals, dates and zero dates, datetime2(7) unchecked, PostgreSQL's `::text` types, bytea / varbinary hex, NULL and '', DEFAULT, conflicts, a failure half-way, MyISAM partial, the deferred FK at COMMIT, SQL Server's AFTER trigger and transaction-ending error, PostgreSQL's BEFORE trigger, read-only in the worker and MySQL 1792, Cancel while waiting for a lock, a paused browse ended first). A4, A9, A13, A14, A15 → Tasks 9–10; A6, A7 and the frozen sidecar's preview and apply on every engine → Task 11; §13's docs (05a's part) → Task 11. CR-1 (the refresh keeping position) and §3.6–§3.7 are 05b.
- **Placeholders:** none; the probes, every test and the risky code (literals, the plan builder, the targets, the store, the service, the outcome table, the worker's apply, the harness) are given whole; each assumption gives both outcomes.
- **Names across tasks:** `SCRATCH_PREFIX`, `ITEM_ROWS`, `ScratchDb` (`execute`, `rows`, `profile`, `open`, `holding`, `waiting`, `fill`, `q`, `table`, `path`, `drop`, `close`), `PgScratch`, `MysqlScratch`, `MssqlScratch`, `SqliteScratch`, `ENGINES`, `SERVER_ENGINES`, `scratch_db`, `scratch(db, before_drop)`, `items_table` (Task 1) → Tasks 5, 6, 9, 10, 11. `Binary`, `Truncated`, `Json`, `MissingCell`, `MISSING_CELL`, `Cell`, `MalformedCell`, `normalise_raw`, `normalise_encoded`, `NORMALISERS`, `Source`, `HEX_BYTES`, `CellLiterals(dialect)(cell, category)`, `value_condition` (Task 2) → Task 3. `EditKind`, `Compare`, `EditColumn`, `EditTarget`, `CellInput`, `RowUpdate`, `RowInsert`, `RowDelete`, `EditChanges`, `EditProblemCode`, `EditProblem`, `UncheckedColumn`, `PlannedStatement` (with `row`), `EditWarning`, `EditCounts`, `EditPreview`, `EditPlan`, `PlanUnavailable(code, message)`, `edit_kind`, `kind_category`, `MAX_CHANGES`, `MAX_VALUE_CHARS`, `VALUE_ACCEPTED_CHARS`, `PLAN_TTL_S`, `MAX_PLANS`, `PLAN_EXPIRED`, `PLAN_MISMATCH`, `PLAN_EXPIRED_MESSAGE`, `PLAN_MISMATCH_MESSAGE`, `path_database`, `quote_literal(…, national, backslash_escapes)`, `datetime_literal`, `json_literal`, `TransactionFrame`, `transaction_frame`, `insert_defaults`, `script_text`, `LiteralStyle`, `DEFAULT_STYLE`, `ValueProblem`, `input_literal`, `match_condition`, `cell_text` (Task 3) → Tasks 4–8. `DmlPlan`, `build_plan` (Task 4) → Task 7. `AdapterCapabilities.row_editing`, `RowEditing.edit_target`, `StatementResult.matched_rows`, `EngineInfo.row_editing`, `KeyCandidate`, `build_target`, `read_only_relation`, `NO_KEY`, `VIEWS`, `KEY_NOT_EXACT`, `category_for_type_name`, `affinity_kind` (Task 5); `SPATIAL_TYPES`, `category_for_data_type`, `rows_matched`, `_VerbatimCursor.ok_info`, `category_for_type` (Task 6) → Tasks 7–10. `EditPlanStore(ttl_s, capacity, clock)`, `put`, `take(plan_id, profile_id, database)`, `EditService.target`, `preview`, `plans`, `READ_ONLY_REASON`, `NO_ROW_EDITING`, `NotEditable`, `AppContainer.edits` (Task 7) → Tasks 8–11. `ApplyEditsCommand`, `EditOutcome`, `EditOutcomeEvent` (`applied`, `already_gone`, `failed_change`, `statement`), `EditReport`, `EditProgress`, `refused`, `history_error`, the outcome codes, `WorkerHooks.take_edit_plan`, `_start_request(before_finished)`, `_forward_messages`, `SessionHooksFactory(…, plans)` (Task 8) → Tasks 9–11. `val`, `NULL`, `DEFAULT`, `update`, `insert`, `delete`, `Applied`, `Tab`, `EditApp`, `edit_env` (Task 9) → Tasks 10–11. Checked task by task.
- **Review Focus:** each of the five lines names its tests and their tasks; each test is in its task's code.
- **Proportion:** the plan gives the code of every module; prose is the rules, the interfaces and the expectations.
- **Pre-checked while planning:** in a scratch copy of the backend at 8b0da80, the plan's code for Tasks 2–8 and the tests of Tasks 1–10 were applied as written and run against the dev containers: the formatting, Couchbase-literal, export and SQL-API tests unchanged after Task 2 (160 passed); Task 3's 93 new tests and Task 4's 10; Tasks 5–6's target tests (PostgreSQL, SQLite, MySQL, SQL Server); Task 7's 9; Task 8's 38 (with the existing worker, session, protocol, wiring, transaction, read-ahead and API tests: 181 passed); Task 9's 43 shared flow cases and 16 PostgreSQL / SQLite tests; Task 10's 19 — all passing; no `zz\_05a%` or `zz\_plan05a%` object left in any server afterwards, Couchbase never touched, the full suite (which would touch Couchbase scratch documents) not run. Task 11's probe ran against that copy served by `python -m dbaguy_backend` (the numbers in A6 and A7). Fixes found that way are already in the tasks' text: SQLite's `CREATE INDEX` takes an unqualified table, `dec` and `off` are reserved words, a MySQL generated column cannot use an AUTO_INCREMENT column, and pydantic refuses lone surrogates (E19).
