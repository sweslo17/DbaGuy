# 06c: Data tools, Copy data (backend and frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Copy the rows of tables, views or a query tab's result from one connection into a table on another (or the same) connection, between PostgreSQL, MySQL, SQL Server and SQLite in every direction: a preview that maps every column to the target engine's type and says what happens to its values on the way, three modes (create a table, append, delete its rows then append), one transaction per table, an honest outcome per table, the Copy dialog with its mapping table, the entries (tree, table tab, query result, ⌘⇧P), Copy as the third kind of job in Jobs; then close ⑥ (both suites, the packaged app, a 1,000,000-row copy in the frozen sidecar, CLAUDE.md, the spec).

**Architecture:** Backend: `adapters/families.py` is the one rule from a declared type to its `TypeFamily` on each engine (the catalog reads move there; a query result's type text uses it too); `sql/type_map.py` (pure) maps a family to each engine's type (§3.5.2), checks a typed type (R-06-10) and converts values on the way (§3.5.3): a value the target cannot hold fails its row with the table, the row number and the column, never NULL. ⑤'s plan store becomes the generic `services/plans.py` `OneTimePlanStore` (D-06-8); `TransferService.plan` (`POST /data/transfer/plan`) reads both catalogs on the metadata connections and stores the one-time `StoredTransfer` (the SELECTs, the statements around the rows, the column mapping; the INSERTs are written while copying, against the target session). The copy is the third job kind: `CopyDataCommand{plan_id, source_session_id}` on the target's job session runs `TransferSink`; for each table it submits a `TransferSource` (a helper job: never listed, run inside its session's transaction) to the source session — one the copy opens and closes itself for tables, the query tab's own for a query result — and the two talk through a bounded `RowPipe` (2 chunks). Frontend: `copyForm.ts` and `mappingRows.ts` (pure, Vitest) are the form and the mapping; `CopyDataDialog` and `MappingTable` render them; the job's state, words and lifecycle are 06b's (`jobState`, `jobReport`, `jobsStore`) with copy added; the entries are the tree's "Copy data to…", the table tab's ⋯ "Copy rows to…" (with its filter), a result's "Copy result to a table…" (grid menu and status bar) and ⌘⇧P.

**Tech Stack:** Python 3.13 sidecar (FastAPI, pydantic v2, loguru, pytest; psycopg 3, mysql-connector-python pure, pymssql, sqlite3); React 19 + TypeScript 6, zustand 5, TanStack Query 5, Vitest 5.0.3 (node, pure modules only). No new dependency on any side.

**Spec:** docs/superpowers/specs/2026-10-20-06-data-tools-design.md — §3.5 (all of it), §3.6 (CopyDataDialog, MappingTable, `mappingRows`, the entries), R-06-8 to R-06-13, D-06-1, D-06-6, D-06-8, D-06-13, §4 (the copy rows), §5, §6, §7 (the Copy row), §10 A9, A11, A16, §11 item 3 / D-06-14 / S1, §12 CR-1, CR-2, CR-3, and the 〔06a〕/〔06b〕 notes, which override the text they annotate. Carries: docs/superpowers/notes/2026-10-21-06a-data-tools-backend-followups.md (the Copy row) and docs/superpowers/notes/2026-10-25-06b-data-tools-frontend-followups.md (the four 06c rows, and "package and measure again when 06c lands"). Precedents: 06a's plan (backend style, the frozen-sidecar probe), 06b's plan (pure modules with Vitest, components checked by hand, the job's lifecycle), ⑤ (one-time plans, `matched_rows()`, D-05-5's fail-closed row counts, H24's red Commit).

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 06c serves it, or avoids regressing it |
|---|---|
| §8 跨 DB 傳輸或複製資料 (the user's own request) | Tables, views, a schema's checked tables or a query's result, into any of the four relational engines, same engine included; three modes; the preview shows every column's target type and what changes on the way before anything runs. |
| §2 Q1 輕、快、低記憶體 | A pipe of 2 chunks, each ≤ 1,000 rows and ≤ 4 MiB of text (R-06-13), reads sized by the largest row (06a M12's `fetch_size`, now shared); no temporary file; no new dependency. Planning measured 1,000,000 rows (6 columns, about 230 bytes) on an unfrozen sidecar: PG → MySQL 19.2 s, PG → SQL Server 87.2 s, PG → SQLite 12.3 s, SQLite → PG 19.6 s, sidecar RSS +2–12 MB (§7: ≤ 60 s, SQL Server ≤ 2.5 min, ≤ 40 MB); Task 9 repeats it frozen. Bundle 1,526.60 → 1,549.50 kB (+23 kB; gzip 492.29 → 497.79 kB). The dialog reads the preview 400 ms after the last change. |
| §2 Q2 穩定、不丟工作 | One transaction per table (MySQL's CREATE TABLE excepted, and said); every outcome per table; the plan runs once, never resent; the job outlives its dialog and a reload (06b); a Cancel between two tables or during a COMMIT stops before the next table; one History record; deleting the source connection while a copy runs is refused (B13). |
| §2 Q3 可信任 | Each column's mapping and conversion is shown before; a value the target cannot hold fails its row by table, row and column (never NULL, never rounded, never cut: MySQL's target session is strict); offsets go to UTC in Python keeping SQL Server's 7th digit; a MySQL source reads in UTC; an INSERT's matched rows are checked (A16), so a trigger that skips rows rolls the table back; the copy runs exactly the previewed plan; the 4×4 matrix reads every value back (A9). |
| §2 Q4 聚焦 80% | Three modes; no upsert, no sync, no resumable copy, no Couchbase (R-06-14, CR-1, CR-2); a new table gets its columns, NOT NULL and its primary key only (R-06-9). |
| §2 Q5 多引擎 | One family table and one type table for four engines in both directions; the engines' differences live in `families.py`, `type_map.py` and `transfer_sql.py`; Couchbase is not offered as a target (R-06-14; pre-flight L-j: the dialog's `TargetPicker tool="copy_data"` leaves it out, and the API answers 422 `not_supported`). |
| §2 Q6 macOS 慣例 | Return is the primary button, Esc closes (the job keeps running), ⌘⇧P; the Jobs list (06b). |
| §3 M3 每個指令都能用鍵盤 | ⌘⇧P "Copy data to…" (a table tab's own "Copy rows to…", with its filter). |
| §3 M9 複製與匯出 | "Copy result to a table…" next to Export in the result's status bar and in the grid's menu. |
| §3 M11 安全 | A red target makes Copy a danger button (⑤ H24); replace's `DELETE FROM` shows in the danger colour; a read-only target is greyed and refused before anything is sent (403); a query source must only read; the source and the target being the same table or SQLite file is refused. |
| User requests | Classic scrollbars: the mapping table and the statements reserve their scrollbar (`[scrollbar-gutter:stable]`); Zhuyin: every key handler checks `isImeKeyEvent` first; monochrome engine icons wherever a connection is shown. |

## Global Constraints

- Branch `feat/06-data-tools` (06b ends at 40a15dc). ⑥ merges to `main` after this plan's final review, never inside it.
- No new dependency (Python, frontend, Rust). No Rust change.
- Binding values, each defined once, in the file named:
  - `backend/dbaguy_backend/domain/transfer.py`: `MAX_TRANSFER_TABLES = 500` (spec §3.5.1), `MAX_QUERY_CHARS = 1_048_576`, `MAX_QUERY_COLUMNS = 4_096`, `TRANSFER_FILTER_LENGTH = 65_536` (as a dump's filter), `TYPE_TEXT_LENGTH = 1_024` (what the request takes; the plan says `invalid_type` past `TYPE_CHARS`).
  - `backend/dbaguy_backend/sql/type_map.py`: `TYPE_CHARS = 200` (spec §3.5.1: a type is at most 200 characters).
  - `backend/dbaguy_backend/sessions/jobs/pipe.py`: `PIPE_CHUNKS = 2`, `POLL_S = 0.1` (R-06-8, §3.5.5 step 5).
  - `backend/dbaguy_backend/sessions/jobs/transfer_source.py`: `PIPE_CHUNK_CHARS = 4 * 1_048_576` (R-06-13); `sessions/jobs/context.py`: `FETCH_ROWS = 1000`, `FIRST_FETCH = 1`, `fetch_size()` (moved from the dump, shared).
  - `backend/dbaguy_backend/sql/transfer_sql.py`: `SOURCE_SETTINGS` (PostgreSQL `DateStyle = 'ISO, MDY'`, `IntervalStyle = 'postgres'`; MySQL `net_write_timeout = 3600`, `time_zone = '+00:00'`), `TARGET_SETUP` (06a's `RESTORE_SETTINGS`, SQL Server's with `SET IMPLICIT_TRANSACTIONS OFF` first).
  - `backend/dbaguy_backend/services/plans.py`: ⑤'s `PLAN_TTL_S` (15 min), `MAX_PLANS` (32) and `MAX_PLAN_CHARS` (128 MiB), per store (the edit store and the transfer store are two stores).
  - `src/features/data-tools/copyForm.ts`: `MAX_COPY_TABLES = 500`, `QUERY_TABLE = "query_result"`. `CopyDataDialog.tsx`: `PLAN_DEBOUNCE_MS = 400` (as the dump's).
- From the spec, verbatim where it binds:
  - R-06-8: 「每條連線只在自己的 worker thread 上被呼叫…來源 worker 跑 `TransferSource`…目標 worker 跑 `TransferSink`…中間的 `RowPipe` 最多放 2 塊」; 「query 結果來自那個 tab 自己的 session（重跑那一句，同匯出：同一個交易、temp table 看得到；只收 READ）」; 「任一邊失敗或取消，另一邊跟著停」.
  - R-06-9: 「建出來的表只有欄位、NOT NULL 與 primary key」; 「key 衝突一律失敗、整張表 rollback」; 「多張表依 FK 排序（父表先…）；環狀的在預覽警告」; identity 「預設保留來源的值…附加模式可逐欄改成『讓目標產生』…PG 寫入之後以 `setval` 把序列推到最大值之後」; 「來源與目標是同一張表、或同一個 SQLite 檔案…預覽就拒絕」.
  - R-06-10: 「同 engine 照抄宣告的型別（使用者自訂型別除外…）」; 「使用者改的型別經 lexer 檢查只是一個型別片段（沒有 `;`、註解、未關閉的引號或括號…）」.
  - R-06-12: 「逐表一個交易，逐表的結果 `copied`／`rolled_back`／`not_started`／`unknown`，整體是 `completed`／`partial`／`rolled_back`／`unknown`」; 「MySQL 建新表時 CREATE TABLE 隱含 commit，失敗後表還在（空的），結果明說…不自動 DROP」.
  - §3.5.3: 「PG `infinity`、西元前的日期、MySQL `0000-00-00` → 其他 engine：那一列失敗，訊息指出表、行與欄（不改成 NULL：Q3…）」.
  - §3.5.5 step 2: 「確認符合的列數等於這句的列數，不符就失敗：trigger 跳過或改寫，同 ⑤ D-05-5 的 fail closed」.
  - D-06-8: 「`TransferCommand` 只帶 `plan_id`：執行的就是預覽的（R-05-7）；plan 綁目標 profile＋database 與來源 profile，來源 session 必須屬於來源 profile（`plan_mismatch`）。INSERT 的文字不在 plan 裡」.
  - §3.6 CopyDataDialog: 「有問題時沒有 Copy 按鈕」 (built as: Copy is disabled and says why, as the dump's).
- Backend rules (CLAUDE.md): loguru INFO carries kinds, profiles, counts, ms and outcomes only; values, literals, SQL text and server error text only at DEBUG (D-06-12); identifier quoting and literals only from `sql/dialects.py` / `sql/literals.py`; never read `.text`, `.message`, `.srvname` or `.procname` on a pymssql exception you constructed.
- Frontend rules (CLAUDE.md): colours only through token classes or `var(--color-…)`; every new React key handler checks `isImeKeyEvent(e)` first; a fixed-height scrolling area reserves its scrollbar (`[scrollbar-gutter:stable]`); API types only through `src/lib/api/types.ts`; `openapi.json` and `schema.d.ts` only from `npm run gen:api`; a zustand selector never returns a new array or object; a name read from a record is read as an own property (05b K2).
- Writes: the copy command is sent once (06b's `JobRunner`); the plan is taken once; nothing is ever sent again by the backend; the preview is a read (`useQuery`, `retry: false`).
- Tests per task: each backend task runs the files it names (`cd backend && .venv/bin/pytest -q <files>`); `npm run gen:api` after every backend task that changes a model or a route; `npm test`, `npm run typecheck` and `npm run build` after every task touching `src/`. The full backend suite runs once, in Task 9 (dev containers up, no scratch object, one runner at a time).
- Dev databases: all five containers up and healthy (never start, stop or restart one; if one is down, stop and report). Live scratch objects are `zz_06c_*` only, made through `tests/transfer_support.table_name()` and dropped by exact name in `finally` (`tests/data_support.dropping`); no Couchbase write; seeds untouched; the full backend suite only in Task 9.
- Manual checks: the coordinator runs each UI task's numbered list in the in-app browser with `scripts/dev.sh --web` and `http://localhost:1420` (`dev/data`, never the app's data). Agents never use a browser. Scratch tables for checks are `zz_06c_*`, created and dropped by exact name from a DbaGuy query tab; a scratch SQLite file is `dev/data/scratch/zz_06c.sqlite` (git-ignored), removed by exact path. Every list ends with a check that nothing `zz_06c` is left on any engine. Profiles for checks: "dev pg", "dev mysql", "dev mssql", "zz_06c sqlite", "dev couchbase", "dev pg read-only", "dev mysql red" (dev mysql with colour red), save password off. Never touch `~/Library/Application Support/DbaGuy`. Never kill a process by pattern; stop only your own PIDs.
- Disk: about 18 GiB free while planning. Scratch under 1 GB. Task 9's 1,000,000-row probe needs `df -h /` ≥ 5 GiB before it starts; its SQLite file (about 250 MB) is deleted at its end. Never `cargo clean` or prune Docker.
- Repo is public: dev credentials only.
- Commit after each task, staging only that task's files by path (never `git add -A`); the message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Every task's code below was applied in order in a scratch worktree at 40a15dc, and each task's tests, `gen:api`, typecheck and build passed after that task alone (see the Dry-run record): new files are given whole, changed files as patches against the previous task's tree. The React components were compiled and built, not driven: their wiring is checked by hand (06b H1).

## Review Focus

1. **A value the target cannot hold fails its row, by name; nothing is changed silently.** PostgreSQL's `infinity` and BC dates, MySQL's zero dates and times past a day, NaN into MySQL or SQL Server, a NUL into PostgreSQL, text into binary: the table rolls back with "Row N of <source>, column <c>: …" (`value_not_copyable`), never NULL, never rounded; decimals into SQLite are TEXT; offsets go to UTC with every fractional digit; MySQL's target session is strict, so nothing is cut. → Task 1 `test_postgresqls_special_dates_fail_their_row_on_another_engine`, `test_mysqls_zero_dates_and_long_times_fail_their_row_elsewhere`, `test_numbers_into_text_keep_every_digit_and_non_finite_numbers_fail_where_they_cannot_be_held`, `test_a_time_zones_value_goes_to_utc_where_the_column_has_none_never_rounded`, `test_a_nul_or_binary_data_in_text_fails_its_row_where_it_cannot_be_written`; Task 4 `test_mysql_keeps_the_table_it_created_when_a_value_fails_its_row`; Task 5 (A9) `test_every_value_of_a_type_complete_table_reads_back_as_itself`.
2. **A table's outcome is what happened, never more.** One transaction per table; a key conflict, a trigger that skips rows (A16), a failing source roll the table back; a table that cannot roll back after a failed write, and a COMMIT that may or may not have happened, are `unknown`; MySQL's created table stays, empty, and is said; the tables after a failure are `not_started`. → Task 4 `test_a_key_conflict_rolls_the_table_back_and_says_so`, `test_a_trigger_that_skips_rows_rolls_the_table_back`, `test_a_table_that_cannot_roll_back_is_unknown_once_a_write_failed`, `test_several_tables_stop_at_the_first_that_fails`, `test_a_query_tabs_copy_reads_on_its_session_and_a_failing_read_is_the_sources`; Task 6 `jobReport.test.ts` "says what was copied, table by table".
3. **A Cancel stops the copy at the next safe point and leaves nothing behind.** During a table: rolled back, the source stopped at its next chunk; between two tables or during a COMMIT: held, and the next table never starts; a source session the copy opened is closed; a query tab gets its own settings back. → Task 3 `test_a_cancel_while_the_statements_cannot_be_cancelled_stops_the_next_table`, `test_each_side_waiting_on_the_other_sees_its_own_cancel`, `test_a_mysql_source_reads_in_utc_without_a_write_timeout_and_its_tab_gets_its_settings_back`; Task 4 `test_a_cancel_rolls_the_table_back`.
4. **The copy runs only the previewed plan, once, and only reads its source.** The plan is bound to the target's profile and database and the source's profile (and, for a query, its tab's session); a query that writes or whose columns changed is never read; the command carries the plan id only and is sent once. → Task 2 `test_a_plan_is_taken_once_by_the_targets_session`; Task 3 `test_a_copys_source_is_its_query_tabs_session_or_one_of_its_own`, `test_a_query_that_writes_or_changed_its_columns_is_never_read`; Task 4 `test_a_plan_runs_once`; Task 6 `copy.test.ts` "copies only the preview shown, with a plan, and a query reads on its tab's session".
5. **Nothing destructive happens without being shown, and the source cannot be pulled away.** Create refuses a table that exists; the same table or SQLite file is refused; replace's `DELETE FROM` is in the danger colour; a red target's Copy is a danger button; deleting the source connection while its copy runs is refused (B13); "Copy the rest…" checks only the tables not copied. → Task 2 `test_create_refuses_a_table_that_exists_and_append_one_that_does_not`; Task 6 `copy.test.ts` "marks the DELETE…", `jobsStore.test.ts` "its deletion waits for the copy…", `jobReport.test.ts` "offers the rest of a copy that stopped part way…"; Task 7 manual checks 5–6; Task 8 manual check 5.

---

## File Structure

```
backend/dbaguy_backend/
  adapters/families.py (new)          a declared type → TypeFamily, per engine (catalogs and a query's type text)
  adapters/columns.py                 TableColumn.custom_type
  adapters/{postgres,mysql,mssql,sqlite}/columns.py   their family rules from families.py; PG/SQL Server custom types
  adapters/mysql/types.py             SPATIAL_TYPES from families.py
  adapters/base.py                    AdapterCapabilities.copy_data; the four relational adapters declare it
  adapters/editing.py                 not_writable_reason(target): why a table cannot be written (⑤'s rules)
  sql/type_map.py (new)               §3.5.2 target types, R-06-10 type_problem, §3.5.3 conversions (pure)
  sql/transfer_sql.py (new)           the copy's statements: settings, CREATE TABLE, DELETE, IDENTITY_INSERT, setval, History
  sql/bulk_insert.py                  InsertBatcher: abort_on_conflict (SQLite INSERT OR ABORT), flushed_rows
  domain/transfer.py (new)            TransferRequest/SourceSpec/TargetSpec/QuerySource, TransferPlan/TableTransfer/ColumnMapping
  domain/data_jobs.py                 JobKind copy_data, TableOutcome, JobObject.outcome/source, JobInfo.source_profile_id
  domain/execution.py                 CopyDataCommand
  domain/profiles.py                  EngineInfo.copy_data
  services/plans.py (new)             OneTimePlanStore (⑤'s store, generic)
  services/edit_plans.py              EditPlanStore on OneTimePlanStore
  services/transfer_plans.py (new)    PlannedColumn, PlannedTable, StoredTransfer, TransferPlanStore
  services/transfer_service.py (new)  TransferService.plan: both catalogs → TransferPlan, the one-time plan
  api/data.py                         POST /data/transfer/plan
  container.py                        transfers = TransferService(…); the hooks get its plans
  sessions/jobs/context.py            fetch_size, SourceLease, JobHooks.source, resume_cancels, setting, matched, Job.helper, PreparedJob
  sessions/jobs/dump.py               fetch_size from context.py
  sessions/jobs/pipe.py (new)         RowPipe, SourceFailed
  sessions/jobs/transfer_source.py (new)   TransferSource: one table's rows into the pipe, on the source session
  sessions/jobs/transfer.py (new)     TransferSink: the copy, table by table, on the target session
  sessions/jobs/registry.py           begin(…, source_profile_id)
  sessions/worker.py                  a helper job; a Cancel held while cancels are stopped; _resume
  sessions/manager.py                 _source: the query tab's session, or one the copy opens and closes
  sessions/hooks.py                   PreparedJob; CopyDataCommand → TransferSink (plan_expired, plan_mismatch)
backend/tests/
  test_type_map.py, test_transfer_sql.py, test_transfer_plan.py, test_transfer_pipe.py, test_copy_framework.py,
  test_transfer_job.py, test_copy_matrix.py, copy_matrix.py, transfer_support.py (new); data_support.py, test_job_framework.py
src/
  lib/api/{openapi.json, schema.d.ts, types.ts}   generated; types.ts exports the copy's models
  app/quitQuestion.ts (+ test)        a running copy in the quit question
  features/data-tools/
    copyForm.ts (new)                 the form, the preview's request, the command, titles, "Copy the rest…"
    mappingRows.ts (new)              a TableTransfer → the mapping table's rows; overrides, identity, left out
    copy.test.ts (new)                both
    CopyDataDialog.tsx (new)          the dialog: from, to, mode, the preview, Copy
    MappingTable.tsx (new)            one table's columns, types, conversions, identities, statements
    entryPoints.ts (+ test)           CopyEntry: tree node, table tab, query result, palette
    jobState.ts, jobReport.ts (+ test), jobsStore.ts (+ test), jobEndings.test.ts   copy as the third job kind
    dataDialogs.ts, DataToolsHost.tsx, JobDialog.tsx, JobViews.tsx, DumpDialog.tsx, TargetPicker.tsx, jobsApi.ts
  features/edits/tableEditing.ts      NOT_IN_COPY
  features/query/{ConnectionPicker.tsx, QueryTab.tsx, ResultGrid.tsx, ResultsArea.tsx}   DatabasePicker under a database; Copy result
  features/grid/{DataGrid.tsx, GridMenu.tsx, StatusBar.tsx}   "Copy result to a table…", "Copy to table…"
  features/table-data/{TableDataTab.tsx, tableTabActions.ts}   ⋯ "Copy rows to…" (with the filter), its palette action
  features/palette/{commands.ts, dataCommands.test.ts}         "Copy data to…"
  features/explorer/Explorer.tsx      "Copy data to…"; a connection a copy reads from is not deleted (B13)
CLAUDE.md, docs/superpowers/specs/2026-10-20-06-data-tools-design.md   (Task 9)
```

Task order follows dependencies: the pure type rules (1), the preview and its plan (2), the job framework's parts with the pipe and the source (3), the copy job (4), the 4×4 matrix (5); then the frontend's pure modules (6), the dialog (7), the entries (8); the closing (9). Each task compiles and passes its tests on its own.

---
### Task 1: Type families and the type map (pure)

**Suggested implementer:** Opus (every rule of §3.5.2–§3.5.3; a value must never change silently).

**Files:**
- Create: `backend/dbaguy_backend/adapters/families.py`, `backend/dbaguy_backend/sql/type_map.py`
- Modify: `backend/dbaguy_backend/adapters/columns.py`, `backend/dbaguy_backend/adapters/{postgres,mysql,mssql,sqlite}/columns.py`, `backend/dbaguy_backend/adapters/mysql/types.py`
- Test: `backend/tests/test_type_map.py` (new)

**Interfaces:**
- Consumes: 06a's `TableColumn`, `TypeFamily`, `read_as`, `not_copied` (`adapters/columns.py`) and the four engines' `columns.py` catalog queries; `EditKind`; `Dialect`, `quote_ident` (`sql/dialects.py`); the dialect lexer (`sql/lexer.py`) for `type_problem`; `ColumnCategory` (`domain/execution.py`).
- Produces:
  - `adapters/families.py` — the one rule from a declared type to its family, per engine (06a's `columns.py` modules had their own; they import these now): `pg_family(base: str, typtype: str = "b", category: str = "") -> TypeFamily`, `mysql_family(data_type: str, column_type: str, bits: int | None = None) -> TypeFamily`, `mssql_family(base: str, max_length: int, precision: int) -> TypeFamily`, `sqlite_affinity(upper: str, strict: bool) -> str`, `sqlite_family(declared: str, strict: bool = False) -> tuple[TypeFamily, dict]` (the family and its sizes), `SPATIAL_TYPES`; and for a query result's columns (§3.5.2's last paragraph): `NamedType(family, precision, scale, length)`, `of_name(dialect: Dialect, type_name: str, category: ColumnCategory | None = None) -> NamedType` (a type's text → its family and sizes; else by category: number → decimal without precision, text → text, datetime → timestamp, binary → binary, bool → bool, json → json, anything else → text).
  - `TableColumn.custom_type: bool = False` — a type of the database's own (PostgreSQL domain, enum, composite, or any type outside `pg_catalog`; a SQL Server alias type): a copy maps it by family even to the same engine (R-06-10: the target database may not have it).
  - `sql/type_map.py` (pure):
    - `engine_name(dialect) -> str`; `Mapped(type_sql: str | None, note: str | None, problem: str | None)`; `unmappable(column, target) -> str` (§4's `<column> (<type>) has no <Engine> type: choose one, or leave the column out`).
    - `target_type(column: TableColumn, source: Dialect, target: Dialect) -> Mapped` — §3.5.2: the same engine keeps the declared type (a custom type, except a spatial one, by family); across engines the table's cell, with its ⚠ note; spatial, `sql_variant`, `hierarchyid` across engines are problems.
    - `planned_column(name, type_sql, dialect) -> TableColumn` — a new table's column as the target will read it back (its kind, so the INSERT's literals are the target's).
    - `incompatible(source: TableColumn, target: TableColumn) -> str | None` — text into binary or binary into text (§3.5.3: a problem, never converted).
    - `TYPE_CHARS = 200`; `type_problem(dialect, text) -> str | None` — R-06-10's check of a typed type with the dialect's lexer: empty, longer than 200, an unterminated string or identifier, a comment, `;`, unbalanced parentheses, a comma outside them, a word that belongs to a column, not its type (`NOT`, `NULL`, `DEFAULT`, `PRIMARY`, `KEY`, `REFERENCES`, `CHECK`, `CONSTRAINT`, `UNIQUE`, `GENERATED`, `AS`, `IDENTITY`, `AUTO_INCREMENT`, `AUTOINCREMENT`, `ON`, `COMMENT`), no type word. Each reason is the text after `Not a single type: `.
    - `RowFailure(reason: str)` (an exception: the row's value cannot be written; the job names the row and column); `to_utc(value: str) -> str` (an ISO text with an offset → UTC without it, every fractional digit kept: SQL Server's 7th too); `with_minute_offset(value: str) -> str` (`±HH` → `±HH:MM`; an offset with seconds → UTC `+00:00`); `UTC_NOTE`.
    - `Conversion(convert: Callable[[Any], Any] | None, notes: tuple[str, ...])`; `conversion(source, target, source_dialect, target_dialect, *, backslash_escapes: bool | None = None) -> Conversion` — §3.5.3 as steps applied to each value: PostgreSQL `infinity` / `-infinity` / BC dates → another engine fail the row; MySQL `0000-00-00` dates and TIMEs past a day or negative → elsewhere fail the row; `timestamp_tz` → a type without a zone: UTC; → `timestamp_tz` or text: `±HH:MM`; `time_tz` → a time (MySQL's TIME is the `duration` family): the offset dropped, → text: `±HH:MM`; MySQL `duration` → SQL Server `time`: within a day or the row fails; `bool` → bool (an integer 0 or 1 becomes false or true, any other integer fails its row; text such as `t` goes as it is, for the server to read), → integer `1`/`0`, → text `true`/`false`; a float or numeric from PostgreSQL or SQLite (the engines that hold them) into MySQL or SQL Server: NaN and ±infinity fail the row; PostgreSQL's NaN into SQLite fails the row (SQLite would store NULL; ±infinity is written `9e999`, 06a S3); numbers → text keep every digit; a NUL into PostgreSQL text (or MySQL without backslash escapes) fails the row. The same engine and family needs no conversion (`convert` None).

Two rules the spec's table did not say, both in the code below: PostgreSQL's NaN into SQLite fails its row rather than becoming NULL (06a's literal writes NaN as `NULL` for SQLite, right for a dump of SQLite, which holds none, but a silent change for a copy: Q3), and the dry run's 4×4 matrix (Task 5) found that PostgreSQL `timetz` into MySQL `TIME` failed with 1292, because MySQL's `TIME` is the `duration` family, so the offset is dropped for both `time` and `duration`; `test_a_time_zones_offset_is_written_as_hours_and_minutes` pins it.

- [ ] **Step 1: Write the failing test.** Create `backend/tests/test_type_map.py`:

```python
"""Copy's type map and value conversions (spec ⑥ §3.5.2–3.5.3, 06c Task 1): pure."""

from decimal import Decimal

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.adapters.families import NamedType, of_name
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.sql.type_map import (
    DIGITS_AS_TEXT, UTC_NOTE, RowFailure, conversion, incompatible, planned_column, target_type, to_utc,
    type_problem, with_minute_offset,
)

PG, MY, MS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE
ENGINES = (PG, MY, MS, LITE)


def col(family: str, kind: str = "text", type_name: str | None = None, **fields) -> TableColumn:
    return TableColumn(name=fields.pop("name", "c"), type_name=family if type_name is None else type_name,
                       family=family,  # type: ignore[arg-type]
                       nullable=fields.pop("nullable", True), has_default=False, kind=kind,  # type: ignore[arg-type]
                       compare="equals", writable=True, **fields)


# §3.5.2, row by row: (family, fields) → PostgreSQL, MySQL, SQL Server, SQLite
TABLE = [
    (("bool", "bool"), {}, ("boolean", "tinyint(1)", "bit", "INTEGER")),
    (("int16", "number"), {}, ("smallint", "smallint", "smallint", "INTEGER")),
    (("int32", "number"), {}, ("integer", "int", "int", "INTEGER")),
    (("int64", "number"), {}, ("bigint", "bigint", "bigint", "INTEGER")),
    (("uint64", "number"), {}, ("numeric(20)", "bigint unsigned", "decimal(20,0)", "TEXT")),
    (("decimal", "number"), {"precision": 12, "scale": 3}, ("numeric(12,3)", "decimal(12,3)", "decimal(12,3)", "TEXT")),
    (("decimal", "number"), {}, ("numeric", "decimal(65,30)", "decimal(38,10)", "TEXT")),
    (("money", "number"), {}, ("numeric(19,4)", "decimal(19,4)", "money", "TEXT")),
    (("float32", "number"), {}, ("real", "float", "real", "REAL")),
    (("float64", "number"), {}, ("double precision", "double", "float", "REAL")),
    (("char", "text"), {"length": 5}, ("char(5)", "char(5)", "nchar(5)", "TEXT")),
    (("char", "text"), {"length": 300}, ("char(300)", "varchar(300)", "nchar(300)", "TEXT")),
    (("varchar", "text"), {"length": 10}, ("varchar(10)", "varchar(10)", "nvarchar(20)", "TEXT")),
    (("varchar", "text"), {"length": 3000}, ("varchar(3000)", "varchar(3000)", "nvarchar(max)", "TEXT")),
    (("varchar", "text"), {"length": 20000}, ("varchar(20000)", "longtext", "nvarchar(max)", "TEXT")),
    (("text", "text"), {}, ("text", "longtext", "nvarchar(max)", "TEXT")),
    (("binary", "binary"), {"length": 16}, ("bytea", "varbinary(16)", "varbinary(16)", "BLOB")),
    (("binary", "binary"), {}, ("bytea", "longblob", "varbinary(max)", "BLOB")),
    (("date", "datetime"), {}, ("date", "date", "date", "TEXT")),
    (("time", "datetime"), {}, ("time", "time(6)", "time(7)", "TEXT")),
    (("duration", "datetime"), {}, ("interval", "time(6)", "time(7)", "TEXT")),
    (("year", "datetime"), {}, ("smallint", "year", "smallint", "INTEGER")),
    (("timestamp", "datetime"), {}, ("timestamp", "datetime(6)", "datetime2(7)", "TEXT")),
    (("timestamp_tz", "datetime"), {}, ("timestamptz", "datetime(6)", "datetimeoffset(7)", "TEXT")),
    (("time_tz", "datetime"), {}, ("timetz", "time(6)", "time(7)", "TEXT")),
    (("interval", "datetime"), {}, ("interval", "varchar(64)", "nvarchar(64)", "TEXT")),
    (("uuid", "text"), {}, ("uuid", "char(36)", "uniqueidentifier", "TEXT")),
    (("json", "json"), {}, ("jsonb", "json", "nvarchar(max)", "TEXT")),
    (("xml", "text"), {}, ("xml", "longtext", "xml", "TEXT")),
    (("enum", "text"), {}, ("text", "varchar(255)", "nvarchar(255)", "TEXT")),
    (("other", "other"), {}, ("text", "longtext", "nvarchar(max)", "TEXT")),
]


@pytest.mark.parametrize(("family", "fields", "expected"), TABLE)
def test_every_cell_of_the_type_table_from_another_engine(family, fields, expected) -> None:
    for target, wanted in zip(ENGINES, expected, strict=True):
        source = next(engine for engine in ENGINES if engine is not target)  # another engine than the target
        if family[0] == "json" and target is PG:
            source = MY  # json from PostgreSQL keeps its own json / jsonb
        assert target_type(col(*family, **fields), source, target).type_sql == wanted, (family, target)


def test_what_the_preview_says_of_a_lossy_mapping() -> None:
    assert target_type(col("uint64", "number"), MY, LITE).note == DIGITS_AS_TEXT
    assert target_type(col("decimal", "number"), PG, MY).note.startswith("more than 30 digits after the point")
    assert target_type(col("timestamp_tz", "datetime"), PG, MY).note == "converted to UTC: the offset is not kept"
    assert "UTF-16" in target_type(col("varchar", "text", length=10), PG, MS).note
    assert target_type(col("json", "json"), MS, PG).note == "jsonb orders the keys and keeps the last of duplicate keys"
    assert target_type(col("json", "json", type_name="json"), PG, PG).type_sql == "json"
    assert target_type(col("other", "other", type_name="integer[]"), PG, MY).note == "PostgreSQL's text of the value"
    assert target_type(col("money", "number", type_name="money"), PG, PG).type_sql == "money"


def test_a_type_with_no_counterpart_is_a_problem() -> None:
    for family in ("spatial", "variant"):
        mapped = target_type(col(family, "binary", type_name="geography", name="g"), MS, PG)
        assert mapped.type_sql is None
        assert mapped.problem == "g (geography) has no PostgreSQL type: choose one, or leave the column out"
    assert target_type(col("decimal", "number", precision=70, scale=2), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=20, scale=31), PG, MY).problem is not None
    assert target_type(col("decimal", "number", precision=39, scale=0), PG, MS).problem is not None
    assert target_type(col("decimal", "number", precision=38, scale=0), PG, MS).type_sql == "decimal(38,0)"


def test_the_same_engine_keeps_the_declared_type_unless_it_is_the_databases_own() -> None:
    assert target_type(col("varchar", "text", type_name="character varying(10)"), PG, PG).type_sql == \
        "character varying(10)"
    assert target_type(col("enum", "text", type_name="enum('a','b')"), MY, MY).type_sql == "enum('a','b')"
    assert target_type(col("other", "other", type_name=""), LITE, LITE).type_sql == ""
    domain = target_type(col("varchar", "text", type_name="email", length=40, custom_type=True), PG, PG)
    assert (domain.type_sql, domain.note) == ("varchar(40)", "email is the source database's own type: the new "
                                                             "column is varchar(40)")
    assert target_type(col("enum", "text", type_name="mood", custom_type=True), PG, PG).type_sql == "text"
    assert target_type(col("spatial", "binary", type_name="geometry", custom_type=True), PG, PG).type_sql == "geometry"


def test_text_and_binary_never_go_into_each_other() -> None:
    assert incompatible(col("binary", "binary", name="b"), col("text", "text", name="t", type_name="text"))
    assert incompatible(col("varchar", "text", name="v"), col("binary", "binary", name="b", type_name="bytea"))
    assert incompatible(col("binary", "binary"), col("binary", "binary")) is None
    assert incompatible(col("int32", "number"), col("binary", "binary")) is None  # the server decides


@pytest.mark.parametrize(("text", "problem"), [
    ("int", None), ("varchar(20)", None), ("numeric(10, 2)", None), ("double precision", None),
    ("timestamp with time zone", None), ("nvarchar(max)", None), ("enum('a','b')", None), ("int unsigned", None),
    ("", "it is empty"), ("int; DROP TABLE t", "it has a ;"), ("int -- x", "it has a comment"),
    ("int /*! x */", "it has a comment"), ("int, x int", "it has a comma outside parentheses"),
    ("int NOT NULL", "NOT is not part of a type"), ("int DEFAULT 1", "DEFAULT is not part of a type"),
    ("int REFERENCES t(id)", "REFERENCES is not part of a type"), ("varchar(20", "it leaves a parenthesis open"),
    ("int)", "it closes a parenthesis it did not open"), ("nvarchar('a", "it has an unterminated string, quoted "
                                                                         "name or comment"),
    ("x" * 201, "it is longer than 200 characters"), ("(1)", "it does not name a type"),
])
def test_a_typed_type_is_one_type_and_nothing_else(text, problem) -> None:
    assert type_problem(MY, text) == problem


@pytest.mark.parametrize(("dialect", "text", "category", "expected"), [
    (PG, "int4", ColumnCategory.NUMBER, NamedType("int32")),
    (PG, "numeric(10,2)", ColumnCategory.NUMBER, NamedType("decimal", precision=10, scale=2)),
    (PG, "varchar(5)", ColumnCategory.TEXT, NamedType("varchar", length=5)),
    (PG, "timestamp with time zone", None, NamedType("timestamp_tz")),
    (PG, "int4[]", ColumnCategory.OTHER, NamedType("other")),
    (PG, "mood", ColumnCategory.TEXT, NamedType("text")),  # an enum's name: by the grid's category
    (MY, "bigint unsigned", ColumnCategory.NUMBER, NamedType("uint64")),
    (MY, "decimal", ColumnCategory.NUMBER, NamedType("decimal")),
    (MY, "tinyint(1)", None, NamedType("bool")),
    (MY, "time", ColumnCategory.DATETIME, NamedType("duration")),
    (MS, "nvarchar(max)", ColumnCategory.TEXT, NamedType("text")),
    (MS, "datetimeoffset(7)", ColumnCategory.DATETIME, NamedType("timestamp_tz", scale=7)),
    (MS, "float", None, NamedType("float64")),
    (LITE, "VARCHAR(10)", ColumnCategory.TEXT, NamedType("varchar", length=10)),
    (LITE, "", ColumnCategory.OTHER, NamedType("other")),
])
def test_a_types_text_names_its_family(dialect, text, category, expected) -> None:
    assert of_name(dialect, text, category) == expected


def test_a_new_columns_kind_is_what_its_engine_reads() -> None:
    assert planned_column("b", "boolean", PG).kind == "bool"
    assert planned_column("b", "tinyint(1)", MY).kind == "number"
    assert planned_column("b", "INTEGER", LITE).kind == "number"
    assert planned_column("t", "datetime2(7)", MS).kind == "datetime"
    assert planned_column("d", "decimal(12,3)", MY).precision == 12


def convert(source: TableColumn, target: TableColumn, source_dialect: Dialect, target_dialect: Dialect, value):
    done = conversion(source, target, source_dialect, target_dialect).convert
    return value if done is None else done(value)


def test_a_time_zones_value_goes_to_utc_where_the_column_has_none_never_rounded() -> None:
    tz, plain = col("timestamp_tz", "datetime"), col("timestamp", "datetime")
    assert convert(tz, plain, PG, MY, "2026-10-05 12:34:56.123456+08") == "2026-10-05 04:34:56.123456"
    assert convert(tz, plain, MS, PG, "2026-10-05T12:34:56.1234567+05:30") == "2026-10-05 07:04:56.1234567"
    assert convert(tz, plain, PG, MY, "2026-01-01 00:30:00-01") == "2026-01-01 01:30:00"
    assert convert(tz, plain, PG, MY, None) is None
    assert to_utc("1900-01-01 00:00:00+08:05:43") == "1899-12-31 15:54:17"
    assert UTC_NOTE in conversion(tz, plain, PG, MY).notes


def test_a_time_zones_offset_is_written_as_hours_and_minutes() -> None:
    tz, text = col("timestamp_tz", "datetime"), col("text", "text")
    assert convert(tz, col("timestamp_tz", "datetime"), PG, MS, "2026-10-05 04:34:56+00") == "2026-10-05 04:34:56+00:00"
    assert convert(tz, text, PG, LITE, "2026-10-05 04:34:56.5+05:30") == "2026-10-05 04:34:56.5+05:30"
    assert with_minute_offset("2026-10-05T04:34:56Z") == "2026-10-05T04:34:56+00:00"
    assert with_minute_offset("1900-01-01 00:00:00+08:05:43") == "1899-12-31 15:54:17+00:00"  # no minute offset: UTC
    assert convert(col("time_tz", "datetime"), col("time", "datetime"), PG, MS, "12:34:56+05:30") == "12:34:56"
    # MySQL's TIME is a duration's family: the offset goes too (the 4×4 matrix found it)
    assert convert(col("time_tz", "datetime"), col("duration", "datetime"), PG, MY, "12:34:56+05:30") == "12:34:56"
    assert convert(col("time_tz", "datetime"), text, PG, LITE, "12:34:56+05") == "12:34:56+05:00"


@pytest.mark.parametrize(("value", "reason"), [
    ("infinity", "is 'infinity', which MySQL cannot hold"),
    ("-infinity", "is '-infinity', which MySQL cannot hold"),
    ("0044-03-15 12:00:00 BC", "is a date before the year 1 (BC), which MySQL cannot hold"),
])
def test_postgresqls_special_dates_fail_their_row_on_another_engine(value, reason) -> None:
    with pytest.raises(RowFailure) as failed:
        convert(col("timestamp", "datetime"), col("timestamp", "datetime"), PG, MY, value)
    assert failed.value.reason == reason
    assert convert(col("timestamp", "datetime"), col("timestamp", "datetime"), PG, PG, value) == value


def test_mysqls_zero_dates_and_long_times_fail_their_row_elsewhere() -> None:
    with pytest.raises(RowFailure, match="zero part"):
        convert(col("date", "datetime"), col("date", "datetime"), MY, PG, "0000-00-00")
    assert convert(col("date", "datetime"), col("date", "datetime"), MY, MY, "0000-00-00") == "0000-00-00"
    duration, time = col("duration", "datetime"), col("time", "datetime")
    assert convert(duration, time, MY, MS, "23:59:59.000000") == "23:59:59.000000"
    for value in ("24:00:00", "-01:00:00", "838:59:59.000000"):
        with pytest.raises(RowFailure, match="24 hours or more"):
            convert(duration, time, MY, MS, value)
    assert convert(duration, col("interval", "datetime"), MY, PG, "-838:59:59.000000") == "-838:59:59.000000"


def test_true_and_false_become_what_the_target_column_holds() -> None:
    assert convert(col("bool", "number"), col("bool", "bool"), MY, PG, 1) is True
    assert convert(col("bool", "number"), col("bool", "bool"), LITE, PG, 0) is False
    with pytest.raises(RowFailure, match="holds 5, which is neither 0 nor 1"):
        convert(col("bool", "number"), col("bool", "bool"), MY, PG, 5)
    assert convert(col("bool", "bool"), col("bool", "number"), PG, MY, True) == 1
    assert convert(col("bool", "bool"), col("text", "text"), MS, PG, False) == "false"
    assert convert(col("bool", "bool"), col("bool", "bool"), LITE, LITE, 5) == 5  # the same engine: as read


def test_numbers_into_text_keep_every_digit_and_non_finite_numbers_fail_where_they_cannot_be_held() -> None:
    text = col("text", "text")
    assert convert(col("uint64", "number"), text, MY, LITE, 18446744073709551615) == "18446744073709551615"
    assert convert(col("decimal", "number"), text, MS, LITE, Decimal("1E-7")) == "0.0000001"
    assert convert(col("float64", "number"), text, LITE, PG, 0.1) == "0.1"
    for value in (float("inf"), float("nan"), "NaN", "Infinity"):
        with pytest.raises(RowFailure, match="not a finite number"):
            convert(col("float64", "number"), col("float64", "number"), LITE, MY, value)
    assert convert(col("float64", "number"), col("float64", "number"), PG, LITE, float("inf")) == float("inf")
    with pytest.raises(RowFailure, match="is NaN, which SQLite cannot hold"):  # it would store NULL
        convert(col("float64", "number"), col("float64", "number"), PG, LITE, float("nan"))
    assert conversion(col("int32", "number"), col("int32", "number"), PG, MY).notes == ()  # an integer is finite
    assert conversion(col("float64", "number"), col("float64", "number"), MS, MY).notes == ()  # SQL Server holds none
    finite = conversion(col("decimal", "number"), col("decimal", "number"), PG, MS)  # PostgreSQL's numeric holds NaN
    assert finite.notes == ("NaN and infinity fail their row",)


def test_a_nul_or_binary_data_in_text_fails_its_row_where_it_cannot_be_written() -> None:
    with pytest.raises(RowFailure, match="NUL character"):
        convert(col("text", "text"), col("text", "text"), MS, PG, "a\x00b")
    assert convert(col("text", "text"), col("text", "text"), MS, MY, "a\x00b") == "a\x00b"  # MySQL writes \0
    with pytest.raises(RowFailure, match="NUL character"):
        conversion(col("text", "text"), col("text", "text"), MS, MY, backslash_escapes=False).convert("a\x00b")
    with pytest.raises(RowFailure, match="binary data"):
        convert(col("other", "other"), col("text", "text"), LITE, PG, b"\x00")
```

- [ ] **Step 2: Run it to see it fail.** `cd backend && .venv/bin/pytest -q tests/test_type_map.py` → collection error: `ModuleNotFoundError: No module named 'dbaguy_backend.adapters.families'`.

- [ ] **Step 3: The family rules in one module.** Create `backend/dbaguy_backend/adapters/families.py`:

```python
"""What a column holds whatever its engine calls it (spec ⑥ §3.5.2, `TypeFamily`): each engine's rule, from what its
catalog says (`<engine>/columns.py` reads it) or from a type's text — a query result's column (`ColumnMeta.type_name`)
or a type the user typed for a copy's new table (`of_name`). One rule per engine, used both ways (06c). Pure."""

import re
from dataclasses import dataclass

from ..domain.column_category import ColumnCategory
from ..domain.dialect import Dialect
from .columns import TypeFamily

# ---- PostgreSQL: pg_type.typname of the column's type (a domain read through to its base) ----

PG_FAMILIES: dict[str, TypeFamily] = {
    "bool": "bool", "int2": "int16", "int4": "int32", "int8": "int64", "numeric": "decimal", "money": "money",
    "float4": "float32", "float8": "float64", "bpchar": "char", "varchar": "varchar", "text": "text", "citext": "text",
    "name": "text", "bytea": "binary", "date": "date", "time": "time", "timetz": "time_tz", "timestamp": "timestamp",
    "timestamptz": "timestamp_tz", "interval": "interval", "uuid": "uuid", "json": "json", "jsonb": "json",
    "xml": "xml", "geometry": "spatial", "geography": "spatial",
}
# The SQL names PostgreSQL also takes for them (format_type writes these; a typed type may use either)
_PG_ALIASES = {
    "boolean": "bool", "smallint": "int2", "integer": "int4", "int": "int4", "bigint": "int8", "decimal": "numeric",
    "real": "float4", "float": "float8", "double precision": "float8", "character": "bpchar", "char": "bpchar",
    "character varying": "varchar", "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz", "time without time zone": "time", "time with time zone": "timetz",
    "smallserial": "int2", "serial": "int4", "bigserial": "int8",
}


def pg_family(base: str, typtype: str = "b", category: str = "") -> TypeFamily:
    """`typtype` e: an enum, c: a composite (row) type; `category` A (typcategory): an array, PostgreSQL's text."""
    if typtype == "e":
        return "enum"
    if typtype == "c":
        return "composite"
    if category == "A":
        return "other"
    return PG_FAMILIES.get(base, "other")


# ---- MySQL: information_schema.COLUMNS.DATA_TYPE, with COLUMN_TYPE for unsigned and tinyint(1) ----

# The spatial types' names in information_schema.COLUMNS.DATA_TYPE (the protocol calls them all GEOMETRY)
SPATIAL_TYPES = frozenset({
    "geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection",
    "geomcollection",
})
MYSQL_FAMILIES: dict[str, TypeFamily] = {
    "tinyint": "int16", "smallint": "int16", "mediumint": "int32", "int": "int32", "integer": "int32",
    "bigint": "int64", "decimal": "decimal", "numeric": "decimal", "float": "float32", "double": "float64",
    "real": "float64", "char": "char", "varchar": "varchar", "tinytext": "text", "text": "text", "mediumtext": "text",
    "longtext": "text", "binary": "binary", "varbinary": "binary", "tinyblob": "binary", "blob": "binary",
    "mediumblob": "binary", "longblob": "binary", "date": "date", "time": "duration", "year": "year",
    "datetime": "timestamp", "timestamp": "timestamp", "json": "json", "enum": "enum", "set": "set",
    "bit": "int64",  # the adapter reads BIT(n) as an integer (A M7)
    **dict.fromkeys(SPATIAL_TYPES, "spatial"),
}
# One step wider than the signed family, so that every unsigned value fits; BIGINT UNSIGNED has a family of its own
_MYSQL_UNSIGNED: dict[str, TypeFamily] = {"tinyint": "int16", "smallint": "int32", "mediumint": "int32",
                                          "int": "int64", "integer": "int64", "bigint": "uint64"}


def mysql_family(data_type: str, column_type: str, bits: int | None = None) -> TypeFamily:
    """`column_type` lower case (`tinyint(1)`, `int unsigned`); `bits`: a BIT column's width (NUMERIC_PRECISION)."""
    if column_type.startswith("tinyint(1)") and "unsigned" not in column_type:
        return "bool"  # MySQL's BOOLEAN
    if "unsigned" in column_type and data_type in _MYSQL_UNSIGNED:
        return _MYSQL_UNSIGNED[data_type]
    if data_type == "bit" and (bits or 0) > 63:
        return "uint64"  # BIT(64) holds up to 2^64 - 1
    return MYSQL_FAMILIES.get(data_type, "other")


# ---- SQL Server: sys.types' name of the column's base type ----

MSSQL_FAMILIES: dict[str, TypeFamily] = {
    "bit": "bool", "tinyint": "int16", "smallint": "int16", "int": "int32", "bigint": "int64", "decimal": "decimal",
    "numeric": "decimal", "money": "money", "smallmoney": "money", "real": "float32", "char": "char", "nchar": "char",
    "varchar": "varchar", "nvarchar": "varchar", "text": "text", "ntext": "text", "binary": "binary",
    "varbinary": "binary", "image": "binary", "date": "date", "time": "time", "datetime": "timestamp",
    "smalldatetime": "timestamp", "datetime2": "timestamp", "datetimeoffset": "timestamp_tz",
    "uniqueidentifier": "uuid", "xml": "xml", "json": "json", "timestamp": "rowversion", "rowversion": "rowversion",
    "sql_variant": "variant", "hierarchyid": "variant", "geometry": "spatial", "geography": "spatial",
}


def mssql_family(base: str, max_length: int, precision: int) -> TypeFamily:
    """`max_length` -1: (max); `precision`: a float's bits (53 unless float(n))."""
    if base == "float":
        return "float64" if precision > 24 else "float32"
    if base in ("varchar", "nvarchar") and max_length == -1:
        return "text"
    return MSSQL_FAMILIES.get(base, "other")


# ---- SQLite: the declared type, by affinity in SQLite's own order (⑤ ruling E6) ----

_SQLITE_SIZES = re.compile(r"\(\s*(\d{1,9})\s*(?:,\s*(\d{1,9})\s*)?\)")  # at most 9 digits: int() stays cheap
_SQLITE_FAMILIES: dict[str, TypeFamily] = {"none": "other", "integer": "int64", "blob": "binary", "real": "float64",
                                           "bool": "bool", "json": "json"}


def sqlite_affinity(upper: str, strict: bool) -> str:
    """What a declared type (upper case) is, by SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT;
    BLOB; no type; REAL, FLOA, DOUB; else NUMERIC — and inside NUMERIC by what its name says: DATE / TIME, BOOL, JSON
    (ruling E6). In a STRICT table ANY keeps every value as given, like no type."""
    if strict and upper == "ANY":
        return "none"
    if "INT" in upper:
        return "integer"
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        return "text"
    if "BLOB" in upper:
        return "blob"
    if not upper.strip():
        return "none"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "real"
    if "DATE" in upper or "TIME" in upper:
        return "datetime"
    if "BOOL" in upper:
        return "bool"
    if "JSON" in upper:
        return "json"
    return "numeric"


def sqlite_family(declared: str, strict: bool = False) -> tuple[TypeFamily, dict]:
    """The family a declared type names, with its sizes (`length`, or `precision` and `scale`)."""
    upper = declared.upper()
    affinity = sqlite_affinity(upper, strict)
    if affinity in _SQLITE_FAMILIES:
        return _SQLITE_FAMILIES[affinity], {}
    sizes = _SQLITE_SIZES.search(upper)
    first = int(sizes.group(1)) if sizes else None
    second = int(sizes.group(2)) if sizes and sizes.group(2) else None
    if affinity == "text":
        if "CHAR" in upper and first is not None:
            return ("varchar" if "VAR" in upper else "char"), {"length": first}
        return "text", {}
    if affinity == "datetime":
        if "DATE" in upper and "TIME" in upper or "TIMESTAMP" in upper:
            return "timestamp", {}
        return ("date" if "DATE" in upper else "time"), {}
    return "decimal", ({"precision": first, "scale": second if second is not None else 0} if first else {})


# ---- a type's text: a query result's column, a type typed for a new table ----

@dataclass(frozen=True, slots=True)
class NamedType:
    """A type's text read as its family and sizes (what the catalog would say of a column of that type)."""

    family: TypeFamily
    precision: int | None = None
    scale: int | None = None
    length: int | None = None  # None: no limit (text, max)


_NAME = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(?:\(\s*([^)]*)\s*\))?\s*(\[\])?\s*(unsigned)?\s*$", re.IGNORECASE)
# A query result's column with no family its name gives: by what the grid knows of it (spec §3.5.2's last sentence)
_BY_CATEGORY: dict[ColumnCategory, TypeFamily] = {
    ColumnCategory.NUMBER: "decimal", ColumnCategory.TEXT: "text", ColumnCategory.DATETIME: "timestamp",
    ColumnCategory.BINARY: "binary", ColumnCategory.BOOL: "bool", ColumnCategory.JSON: "json",
}
_SIZED = frozenset({"char", "varchar", "binary"})
_SCALED = frozenset({"time", "timestamp", "timestamp_tz", "time_tz"})


def _numbers(text: str | None) -> list[int]:
    if not text:
        return []
    found = []
    for part in text.split(","):
        part = part.strip().lower()
        if part == "max":
            return [-1]
        if not part.isdigit() or len(part) > 9:
            return []
        found.append(int(part))
    return found


def of_name(dialect: Dialect, type_name: str, category: ColumnCategory | None = None) -> NamedType:
    """The family of a type's text in `dialect` (`numeric(10,2)`, `int4[]`, `nvarchar(max)`, `int unsigned`), with the
    sizes its parentheses give; a name no rule knows is the family `category` gives (a query result's column), else
    `other`. Never raises."""
    found = _NAME.match(type_name or "")
    if found is None:
        return NamedType(_BY_CATEGORY.get(category, "other") if category else "other")
    base, args, array, unsigned = found.group(1).lower(), found.group(2), found.group(3), found.group(4)
    numbers = _numbers(args)
    family: TypeFamily
    if dialect is Dialect.POSTGRES:
        family = "other" if array else pg_family(_PG_ALIASES.get(base, base))
    elif dialect is Dialect.MYSQL:
        column_type = f"{base}({args})" if args else base
        family = mysql_family(base, f"{column_type} unsigned" if unsigned else column_type,
                              numbers[0] if base == "bit" and numbers else None)
    elif dialect is Dialect.TSQL:
        max_length = -1 if numbers == [-1] else 0
        family = mssql_family(base, max_length, numbers[0] if base == "float" and numbers else 53)
    elif dialect is Dialect.SQLITE:
        sqlite, sizes = sqlite_family(type_name)
        return NamedType(sqlite, sizes.get("precision"), sizes.get("scale"), sizes.get("length"))
    else:
        family = "other"
    if family == "other" and category is not None and not array:
        family = _BY_CATEGORY.get(category, "other")
    if family == "decimal" and numbers and numbers != [-1]:
        return NamedType(family, precision=numbers[0], scale=numbers[1] if len(numbers) > 1 else 0)
    if family in _SIZED and numbers and numbers != [-1]:
        return NamedType(family, length=numbers[0])
    if family in _SCALED and numbers and numbers != [-1]:
        return NamedType(family, scale=numbers[0])
    return NamedType(family)
```

Modify `backend/dbaguy_backend/adapters/columns.py` (`custom_type`):

```diff
diff --git a/backend/dbaguy_backend/adapters/columns.py b/backend/dbaguy_backend/adapters/columns.py
index 04dba69..6977596 100644
--- a/backend/dbaguy_backend/adapters/columns.py
+++ b/backend/dbaguy_backend/adapters/columns.py
@@ -47,6 +47,9 @@ class TableColumn:
     # numeric); SQL Server time(7): CONVERT(varchar(16), {}, 126)); None: the column itself
     read_as: str | None = None
     literal: EditKind | None = None  # how a value read that way is written; None: as `kind`
+    # The column's type is the database's own (PostgreSQL: a domain, enum, row or extension type; SQL Server: an alias
+    # type): a copy to another database cannot count on it, and maps it by its family (spec ⑥ R-06-10, 06c)
+    custom_type: bool = False
 
     @property
     def value_kind(self) -> EditKind:
```

Modify `backend/dbaguy_backend/adapters/mysql/types.py` (`SPATIAL_TYPES` comes from `families.py`):

```diff
diff --git a/backend/dbaguy_backend/adapters/mysql/types.py b/backend/dbaguy_backend/adapters/mysql/types.py
index 5cd633e..c43fadf 100644
--- a/backend/dbaguy_backend/adapters/mysql/types.py
+++ b/backend/dbaguy_backend/adapters/mysql/types.py
@@ -10,6 +10,7 @@ from mysql.connector.constants import FieldFlag, FieldType
 
 from ...domain.column_category import ColumnCategory
 from ...domain.execution import ColumnMeta
+from ..families import SPATIAL_TYPES  # the spatial types' names in information_schema.COLUMNS.DATA_TYPE
 
 BINARY_CHARSET = 63  # MySQL's `binary` character set: BLOB/BINARY/VARBINARY hold bytes, not text
 
@@ -67,11 +68,6 @@ _STRINGS: dict[int, tuple[str, str]] = {  # (text name, binary name)
     FieldType.LONG_BLOB: ("text", "blob"), FieldType.BLOB: ("text", "blob"),
 }
 
-# The spatial types' names in information_schema.COLUMNS.DATA_TYPE (the protocol calls them all GEOMETRY)
-SPATIAL_TYPES = frozenset({
-    "geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection",
-    "geomcollection",
-})
 _BY_DATA_TYPE: dict[str, ColumnCategory] = {
     **{kind.name: kind.category for kind in _FIXED.values()},
     **{text: ColumnCategory.TEXT for text, _ in _STRINGS.values()},
```

Modify the four engines' catalog reads to use those rules (PostgreSQL also reads whether the column's own type is custom; SQL Server an alias type):

```diff
diff --git a/backend/dbaguy_backend/adapters/postgres/columns.py b/backend/dbaguy_backend/adapters/postgres/columns.py
index 818d82b..449afa1 100644
--- a/backend/dbaguy_backend/adapters/postgres/columns.py
+++ b/backend/dbaguy_backend/adapters/postgres/columns.py
@@ -8,13 +8,15 @@ from ...domain.edits import Compare, EditKind, edit_kind
 from ...domain.metadata import PathSegment
 from ...sql.dialects import Dialect, qualified
 from ...sql.select_builder import relation_from_path
-from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
+from ..columns import GENERATED, TableColumn, not_copied
+from ..families import pg_family
 from .introspection import relation_oid
 from .loaders import category_for_type_name
 
 # `base` follows typbasetype from each column's type until a type that is not a domain, one row per column, carrying
 # the innermost type modifier a domain sets (a domain column's own atttypmod is -1). typtype 'c' is a composite type,
-# 'e' an enum; typcategory 'A' an array.
+# 'e' an enum; typcategory 'A' an array. The last column: the column's own type is the database's (a domain, an enum, a
+# row type, an extension's type, an array of one), which another database may not have (a copy, 06c).
 _COLUMNS = """
 WITH RECURSIVE base (attrelid, attnum, typname, typtype, typbasetype, typtypmod, typcategory, typmod) AS (
     SELECT a.attrelid, a.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory, a.atttypmod
@@ -27,8 +29,10 @@ WITH RECURSIVE base (attrelid, attnum, typname, typtype, typbasetype, typtypmod,
     WHERE d.typtype = 'd'
 )
 SELECT a.attrelid, a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef,
-       a.attidentity::text, a.attgenerated::text, b.typname::text, b.typtype::text, b.typcategory::text, b.typmod
+       a.attidentity::text, a.attgenerated::text, b.typname::text, b.typtype::text, b.typcategory::text, b.typmod,
+       own.typtype IN ('d', 'e', 'c') OR own.typnamespace <> 'pg_catalog'::regnamespace
 FROM pg_attribute a
+JOIN pg_type own ON own.oid = a.atttypid
 JOIN base b ON b.attrelid = a.attrelid AND b.attnum = a.attnum AND b.typtype <> 'd'
 WHERE a.attrelid = ANY(%(oids)s) AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attrelid, a.attnum
@@ -37,13 +41,6 @@ _NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their t
 # Compared with `=`, not ::text: bpchar, whose ::text drops the padding the loaded value has (`=` ignores it, A4); bytea,
 # whose ::text follows bytea_output (escape: every original would differ) while `= '\x…'::bytea` is exact (05a review M3)
 _EQUALS = frozenset({"bpchar", "bytea"})
-_FAMILIES: dict[str, TypeFamily] = {
-    "bool": "bool", "int2": "int16", "int4": "int32", "int8": "int64", "numeric": "decimal", "money": "money",
-    "float4": "float32", "float8": "float64", "bpchar": "char", "varchar": "varchar", "text": "text", "citext": "text",
-    "name": "text", "bytea": "binary", "date": "date", "time": "time", "timetz": "time_tz", "timestamp": "timestamp",
-    "timestamptz": "timestamp_tz", "interval": "interval", "uuid": "uuid", "json": "json", "jsonb": "json",
-    "xml": "xml", "geometry": "spatial", "geography": "spatial",
-}
 _LENGTHS = frozenset({"bpchar", "varchar"})
 _FRACTIONS = frozenset({"time", "timetz", "timestamp", "timestamptz"})
 # money's text follows lc_monetary (`$1,234.56`): a dump or copy reads it as numeric, exact, and writes the number (P10;
@@ -51,16 +48,6 @@ _FRACTIONS = frozenset({"time", "timetz", "timestamp", "timestamptz"})
 _MONEY_READ = {"money": ("CAST({} AS numeric)", "number"), "_money": ("CAST({} AS numeric[])", "other")}
 
 
-def _family(base: str, typtype: str, category: str) -> TypeFamily:
-    if typtype == "e":
-        return "enum"
-    if typtype == "c":
-        return "composite"
-    if category == "A":
-        return "other"  # an array: PostgreSQL's text
-    return _FAMILIES.get(base, "other")
-
-
 def _sizes(base: str, typmod: int) -> dict:
     """precision / scale / length from the type modifier (-1: none)."""
     if typmod < 0:
@@ -77,7 +64,7 @@ def _sizes(base: str, typmod: int) -> dict:
 
 def column(
     name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
-    typtype: str, category: str, typmod: int,
+    typtype: str, category: str, typmod: int, custom: bool = False,
 ) -> TableColumn:
     """One row of _COLUMNS."""
     kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
@@ -85,11 +72,12 @@ def column(
     reason = GENERATED if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
     read_as, literal = _MONEY_READ.get(base, (None, None))
     return TableColumn(
-        name=name, type_name=type_name, family=_family(base, typtype, category), nullable=nullable,
+        name=name, type_name=type_name, family=pg_family(base, typtype, category), nullable=nullable,
         has_default=has_default or bool(identity) or bool(generated), kind=kind, compare=compare,
         writable=reason is None, read_only_reason=reason, **_sizes(base, typmod),
         identity="always" if identity == "a" else "by_default" if identity == "d" else None,
         generated=bool(generated), **(not_copied(GENERATED) if generated else {}), read_as=read_as, literal=literal,
+        custom_type=custom,
     )
 
 
```

```diff
diff --git a/backend/dbaguy_backend/adapters/mysql/columns.py b/backend/dbaguy_backend/adapters/mysql/columns.py
index 4e16ad8..fb9463a 100644
--- a/backend/dbaguy_backend/adapters/mysql/columns.py
+++ b/backend/dbaguy_backend/adapters/mysql/columns.py
@@ -7,9 +7,10 @@ from ...domain.edits import Compare, edit_kind
 from ...domain.errors import NotFound
 from ...domain.metadata import PathSegment
 from ...sql.select_builder import relation_from_path
-from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
+from ..columns import GENERATED, TableColumn, not_copied
+from ..families import SPATIAL_TYPES, mysql_family
 from . import catalog
-from .types import SPATIAL_TYPES, category_for_data_type
+from .types import category_for_data_type
 
 _FIELDS = """COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, NUMERIC_PRECISION, NUMERIC_SCALE,
        CHARACTER_MAXIMUM_LENGTH, DATETIME_PRECISION"""  # column()'s arguments
@@ -23,35 +24,12 @@ FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORD
 """
 _GENERATED = ("VIRTUAL GENERATED", "STORED GENERATED")  # not DEFAULT_GENERATED: an expression default (ruling E15)
 _NOT_EXACT = frozenset({"float", "double", "real", "vector", *SPATIAL_TYPES})
-_FAMILIES: dict[str, TypeFamily] = {
-    "tinyint": "int16", "smallint": "int16", "mediumint": "int32", "int": "int32", "integer": "int32",
-    "bigint": "int64", "decimal": "decimal", "numeric": "decimal", "float": "float32", "double": "float64",
-    "real": "float64", "char": "char", "varchar": "varchar", "tinytext": "text", "text": "text", "mediumtext": "text",
-    "longtext": "text", "binary": "binary", "varbinary": "binary", "tinyblob": "binary", "blob": "binary",
-    "mediumblob": "binary", "longblob": "binary", "date": "date", "time": "duration", "year": "year",
-    "datetime": "timestamp", "timestamp": "timestamp", "json": "json", "enum": "enum", "set": "set",
-    "bit": "int64",  # the adapter reads BIT(n) as an integer (A M7)
-    **dict.fromkeys(SPATIAL_TYPES, "spatial"),
-}
-# One step wider than the signed family, so that every unsigned value fits; BIGINT UNSIGNED has a family of its own
-_UNSIGNED: dict[str, TypeFamily] = {"tinyint": "int16", "smallint": "int32", "mediumint": "int32", "int": "int64",
-                                    "integer": "int64", "bigint": "uint64"}
 # The text protocol sends a FLOAT with 6 significant digits (0.1234567 arrives as 0.123457, another float32: pre-flight
 # M7). Times 1E0 it is a DOUBLE, sent with every digit, and written back it is the same float32 again. Not `+ 0E0`, the
 # pre-flight's text: -0 + 0 is 0, while -0 * 1 keeps the sign (probed, 8.4.11)
 _FLOAT_READ = "({} * 1E0)"
 
 
-def _family(data_type: str, column_type: str, bits: int | None) -> TypeFamily:
-    if column_type.startswith("tinyint(1)") and "unsigned" not in column_type:
-        return "bool"  # MySQL's BOOLEAN
-    if "unsigned" in column_type and data_type in _UNSIGNED:
-        return _UNSIGNED[data_type]
-    if data_type == "bit" and (bits or 0) > 63:
-        return "uint64"  # BIT(64) holds up to 2^64 - 1 (NUMERIC_PRECISION is its width)
-    return _FAMILIES.get(data_type, "other")
-
-
 def column(
     name: str, column_type: str, data_type: str, is_nullable: str, default: str | None, extra: str | None,
     precision: int | None, scale: int | None, length: int | None, fraction: int | None,
@@ -76,7 +54,7 @@ def column(
     elif data_type in ("time", "datetime", "timestamp"):
         sizes = {"scale": fraction}
     return TableColumn(
-        name=name, type_name=column_type, family=_family(data_type, lowered, precision), nullable=is_nullable == "YES",
+        name=name, type_name=column_type, family=mysql_family(data_type, lowered, precision), nullable=is_nullable == "YES",
         has_default=default is not None or generated or "AUTO_INCREMENT" in extra or "DEFAULT_GENERATED" in extra,
         kind=edit_kind(category_for_data_type(data_type)), compare=compare, writable=reason is None,
         read_only_reason=reason, **sizes, identity="by_default" if "AUTO_INCREMENT" in extra else None,
```

```diff
diff --git a/backend/dbaguy_backend/adapters/mssql/columns.py b/backend/dbaguy_backend/adapters/mssql/columns.py
index 7df3878..e9ca942 100644
--- a/backend/dbaguy_backend/adapters/mssql/columns.py
+++ b/backend/dbaguy_backend/adapters/mssql/columns.py
@@ -6,7 +6,8 @@ from ...domain.edits import edit_kind
 from ...domain.errors import NotFound
 from ...domain.metadata import PathSegment
 from ...sql.select_builder import relation_from_path
-from ..columns import GENERATED, PERIOD, ROWVERSION, SQL_VARIANT, TableColumn, TypeFamily, not_copied
+from ..columns import GENERATED, PERIOD, ROWVERSION, SQL_VARIANT, TableColumn, not_copied
+from ..families import mssql_family
 from . import driver
 from .catalog import (
     CHARS, FRACTION, NCHARS, PerObject, by_object, char_length, id_sources, object_id_sql, rows, type_sql)
@@ -32,25 +33,6 @@ _NON_UNICODE = frozenset({"char", "varchar", "text"})
 # with a `T` (241, A15), and without it a datetime would read by the login's DATEFORMAT (F1)
 _SEVENTH_DIGIT = {"datetime2": "CONVERT(varchar(27), {}, 126)", "datetimeoffset": "CONVERT(varchar(34), {}, 126)",
                   "time": "CONVERT(varchar(16), {}, 126)"}
-_FAMILIES: dict[str, TypeFamily] = {
-    "bit": "bool", "tinyint": "int16", "smallint": "int16", "int": "int32", "bigint": "int64", "decimal": "decimal",
-    "numeric": "decimal", "money": "money", "smallmoney": "money", "real": "float32", "char": "char", "nchar": "char",
-    "varchar": "varchar", "nvarchar": "varchar", "text": "text", "ntext": "text", "binary": "binary",
-    "varbinary": "binary", "image": "binary", "date": "date", "time": "time", "datetime": "timestamp",
-    "smalldatetime": "timestamp", "datetime2": "timestamp", "datetimeoffset": "timestamp_tz",
-    "uniqueidentifier": "uuid", "xml": "xml", "json": "json", "timestamp": "rowversion", "sql_variant": "variant",
-    "hierarchyid": "variant", "geometry": "spatial", "geography": "spatial",
-}
-
-
-def _family(base: str, max_length: int, precision: int) -> TypeFamily:
-    if base == "float":
-        return "float64" if precision > 24 else "float32"
-    if base in ("varchar", "nvarchar") and max_length == -1:
-        return "text"  # (max)
-    return _FAMILIES.get(base, "other")
-
-
 def _sizes(base: str, max_length: int, precision: int, scale: int) -> dict:
     if base in ("decimal", "numeric"):
         return {"precision": precision, "scale": scale}
@@ -88,7 +70,7 @@ def column(
         read_as, literal = "CAST({} AS varbinary(max))", "binary"  # A S6: written back as 0x… (A1: hierarchyid too)
     else:
         read_as, literal = None, None
-    family = _family(base, max_length, precision)
+    family = mssql_family(base, max_length, precision)
     return TableColumn(
         name=name, type_name=type_sql(type_name, max_length, precision, scale), family=family, nullable=bool(nullable),
         has_default=bool(default or identity or computed or generated_always) or base == "timestamp",
@@ -96,6 +78,7 @@ def column(
         read_only_reason=reason, unicode=base not in _NON_UNICODE, **_sizes(base, max_length, precision, scale),
         identity="always" if identity else None, generated=bool(computed or generated_always),
         **(not_copied(skipped) if skipped else {}), read_as=read_as, literal=literal,
+        custom_type=type_name != base,  # an alias type, which another database may not have (06c)
     )
 
 
```

```diff
diff --git a/backend/dbaguy_backend/adapters/sqlite/columns.py b/backend/dbaguy_backend/adapters/sqlite/columns.py
index 82aaacb..bfe96c3 100644
--- a/backend/dbaguy_backend/adapters/sqlite/columns.py
+++ b/backend/dbaguy_backend/adapters/sqlite/columns.py
@@ -2,7 +2,6 @@
 E6; ANY in a STRICT table has none). Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for
 editing and data tools alike. The rowid is never a column: `SELECT *` does not return it."""
 
-import re
 import sqlite3
 from dataclasses import dataclass
 
@@ -10,7 +9,8 @@ from ...domain.edits import Compare, EditKind
 from ...domain.errors import NotFound
 from ...domain.metadata import PathSegment
 from ...sql.select_builder import relation_from_path
-from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
+from ..columns import GENERATED, TableColumn, not_copied
+from ..families import sqlite_affinity, sqlite_family
 
 # The pragma takes the name only (a second argument raises); it matches the name without case, as SQLite does
 _TABLE = "SELECT type, strict FROM pragma_table_list(?) WHERE schema = ?"
@@ -18,8 +18,6 @@ _COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_tab
 _INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
 _GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
 _HIDDEN = 1  # pragma_table_xinfo.hidden: a virtual table's hidden column
-# At most 9 digits: SQLite takes any declared size, and int() refuses a text of 4,300 digits or more (no size then)
-_SIZES = re.compile(r"\(\s*(\d{1,9})\s*(?:,\s*(\d{1,9})\s*)?\)")
 
 
 @dataclass(frozen=True)
@@ -51,32 +49,6 @@ def read_relation(
                           conn.execute(_INDEXES, (name, schema)).fetchall())
 
 
-def _affinity(upper: str, strict: bool) -> str:
-    """What a declared type (upper case) is, by SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT;
-    BLOB; no type; REAL, FLOA, DOUB; else NUMERIC — and inside NUMERIC by what its name says: DATE / TIME, BOOL, JSON
-    (ruling E6). In a STRICT table ANY keeps every value as given, like no type. The one ladder `affinity_kind` and
-    `_family` both read."""
-    if strict and upper == "ANY":
-        return "none"
-    if "INT" in upper:
-        return "integer"
-    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
-        return "text"
-    if "BLOB" in upper:
-        return "blob"
-    if not upper.strip():
-        return "none"
-    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
-        return "real"
-    if "DATE" in upper or "TIME" in upper:
-        return "datetime"
-    if "BOOL" in upper:
-        return "bool"
-    if "JSON" in upper:
-        return "json"
-    return "numeric"
-
-
 # REAL affinity holds floating point, which is not compared. Outside a STRICT table ANY is NUMERIC ('007' is stored as
 # 7); in one it keeps every value as given, like no type ('007' stays text)
 _EDIT_KINDS: dict[str, tuple[EditKind, Compare]] = {
@@ -84,33 +56,11 @@ _EDIT_KINDS: dict[str, tuple[EditKind, Compare]] = {
     "blob": ("binary", "equals"), "real": ("number", "none"), "datetime": ("datetime", "equals"),
     "bool": ("bool", "equals"), "json": ("json", "equals"), "numeric": ("number", "equals"),
 }
-_FAMILIES: dict[str, TypeFamily] = {"none": "other", "integer": "int64", "blob": "binary", "real": "float64",
-                                    "bool": "bool", "json": "json"}
 
 
 def affinity_kind(declared: str, strict: bool = False) -> tuple[EditKind, Compare]:
-    """Grid editing's kind and compare rule for a declared type (`_affinity`)."""
-    return _EDIT_KINDS[_affinity(declared.upper(), strict)]
-
-
-def _family(declared: str, strict: bool) -> tuple[TypeFamily, dict]:
-    """The family a declared type names (`_affinity`), with its sizes."""
-    upper = declared.upper()
-    affinity = _affinity(upper, strict)
-    if affinity in _FAMILIES:
-        return _FAMILIES[affinity], {}
-    sizes = _SIZES.search(upper)
-    first = int(sizes.group(1)) if sizes else None
-    second = int(sizes.group(2)) if sizes and sizes.group(2) else None
-    if affinity == "text":
-        if "CHAR" in upper and first is not None:
-            return ("varchar" if "VAR" in upper else "char"), {"length": first}
-        return "text", {}
-    if affinity == "datetime":
-        if "DATE" in upper and "TIME" in upper or "TIMESTAMP" in upper:
-            return "timestamp", {}
-        return ("date" if "DATE" in upper else "time"), {}
-    return "decimal", ({"precision": first, "scale": second if second is not None else 0} if first else {})
+    """Grid editing's kind and compare rule for a declared type (`families.sqlite_affinity`)."""
+    return _EDIT_KINDS[sqlite_affinity(declared.upper(), strict)]
 
 
 def rowid_alias(relation: SqliteRelation) -> str | None:
@@ -130,7 +80,7 @@ def columns_of(relation: SqliteRelation) -> list[TableColumn]:
         if hidden == _HIDDEN:
             continue
         kind, compare = affinity_kind(declared or "", strict=relation.strict)
-        family, sizes = _family(declared or "", relation.strict)
+        family, sizes = sqlite_family(declared or "", relation.strict)
         generated = hidden in _GENERATED
         found.append(TableColumn(
             name=name, type_name=declared or "", family=family, nullable=not notnull and name != rowid,
```

- [ ] **Step 4: The type map.** Create `backend/dbaguy_backend/sql/type_map.py`:

```python
"""A copy's types and values across engines (spec ⑥ R-06-10, R-06-11, §3.5.2–3.5.3). `target_type`: the type a new
table's column gets on the target engine, with what the preview says of it (⚠, `note`) or why it has none (a
problem: a type to choose, or the column left out). Same-engine copies keep the declared type, unless it is the source
database's own (`TableColumn.custom_type`), which the target database may lack. `incompatible`: text and binary data do
not go into each other's columns. `converter`: how each value goes from a source column into a target column — `None`
when it goes as the driver read it (the target column's literal writes it, `sql/literals.py`), else one function per
column pair, chosen once per table, that raises `RowFailure` for a value the target cannot hold (that row fails: the
table is rolled back and the message names the row and the column, never NULL in its place, Q3). Pure."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..adapters.columns import TableColumn, TypeFamily
from ..adapters.families import of_name
from ..domain.data_jobs import ENGINE_NAMES
from ..domain.dialect import Dialect
from ..domain.edits import EditKind
from ..domain.profiles import Engine
from .lexer import TokenKind, tokenize

ENGINES: dict[Dialect, Engine] = {
    Dialect.POSTGRES: Engine.POSTGRES, Dialect.MYSQL: Engine.MYSQL, Dialect.TSQL: Engine.MSSQL,
    Dialect.SQLITE: Engine.SQLITE,
}
_TEXTUAL: frozenset[TypeFamily] = frozenset({"char", "varchar", "text", "json", "xml", "enum", "set", "uuid",
                                            "interval"})
_DATED: frozenset[TypeFamily] = frozenset({"date", "timestamp", "timestamp_tz"})
_NUMERIC: frozenset[TypeFamily] = frozenset({"int16", "int32", "int64", "uint64", "decimal", "money", "float32",
                                            "float64", "year"})
# Where NaN or ±infinity can come from: PostgreSQL's floats and numerics hold all three, SQLite's REAL ±infinity
_NON_FINITE: frozenset[TypeFamily] = frozenset({"float32", "float64", "decimal", "other"})
_UNMAPPABLE: frozenset[TypeFamily] = frozenset({"spatial", "variant"})
_MYSQL_DECIMAL = (65, 30)  # MySQL's widest DECIMAL: digits, digits after the point
_MSSQL_DECIMAL = 38
_MYSQL_CHAR = 255  # CHAR(n) up to 255; wider is VARCHAR
_MYSQL_VARCHAR = 16_383  # VARCHAR(n) of utf8mb4 that fits MySQL's 65,535-byte row (4 bytes a character)
_NVARCHAR = 4_000  # nvarchar(n) / nchar(n) up to 4,000 UTF-16 code units; wider is (max)
_BINARY = 8_000  # varbinary(n) up to 8,000 bytes; wider is (max) / LONGBLOB
DIGITS_AS_TEXT = "stored as text, so that every digit is kept (SQLite would make it a number of 15 digits)"


def engine_name(dialect: Dialect) -> str:
    return ENGINE_NAMES[ENGINES[dialect]]


@dataclass(frozen=True, slots=True)
class Mapped:
    """A new table's column type on the target, or none (`problem`)."""

    type_sql: str | None
    note: str | None = None  # ⚠: what the preview says of this mapping
    problem: str | None = None


def unmappable(column: TableColumn, target: Dialect) -> str:
    """Spec §4 `unmappable_type`."""
    return f"{column.name} ({column.type_name}) has no {engine_name(target)} type: choose one, or leave the column out"


def _decimal(column: TableColumn, target: Dialect) -> Mapped:
    precision, scale = column.precision, column.scale or 0
    if target is Dialect.SQLITE:
        return Mapped("TEXT", DIGITS_AS_TEXT)
    if precision is None:  # PostgreSQL's numeric without a precision, MySQL's DECIMAL of a query's column
        return {
            Dialect.POSTGRES: Mapped("numeric"),
            Dialect.MYSQL: Mapped("decimal(65,30)", "more than 30 digits after the point are rounded, more than 35 "
                                                    "before it fail"),
            Dialect.TSQL: Mapped("decimal(38,10)", "more than 10 digits after the point are rounded, more than 28 "
                                                   "before it fail"),
        }[target]
    if target is Dialect.MYSQL and (precision > _MYSQL_DECIMAL[0] or scale > _MYSQL_DECIMAL[1]):
        return Mapped(None, problem=unmappable(column, target))
    if target is Dialect.TSQL and precision > _MSSQL_DECIMAL:
        return Mapped(None, problem=unmappable(column, target))
    return Mapped(f"{'numeric' if target is Dialect.POSTGRES else 'decimal'}({precision},{scale})")


def _text(target: Dialect) -> str:
    return {Dialect.POSTGRES: "text", Dialect.MYSQL: "longtext", Dialect.TSQL: "nvarchar(max)",
            Dialect.SQLITE: "TEXT"}[target]


def _char(column: TableColumn, target: Dialect, varying: bool) -> Mapped:
    n = column.length
    if n is None or target is Dialect.SQLITE:
        return Mapped(_text(target))
    if target is Dialect.POSTGRES:
        return Mapped(f"{'varchar' if varying else 'char'}({n})")
    if target is Dialect.MYSQL:
        if not varying and n <= _MYSQL_CHAR:
            return Mapped(f"char({n})")
        return Mapped(f"varchar({n})" if n <= _MYSQL_VARCHAR else "longtext")
    if not varying:
        return Mapped(f"nchar({n})" if n <= _NVARCHAR else "nvarchar(max)")
    units = 2 * n  # nvarchar counts UTF-16 code units: a character beyond the BMP (an emoji) takes two
    shown = f"nvarchar({units})" if units <= _NVARCHAR else "nvarchar(max)"
    return Mapped(shown, f"nvarchar counts UTF-16 units: {shown} holds {n:,} characters even when each takes two")


def _binary(column: TableColumn, target: Dialect) -> Mapped:
    n = column.length
    if target is Dialect.POSTGRES:
        return Mapped("bytea")
    if target is Dialect.SQLITE:
        return Mapped("BLOB")
    if target is Dialect.MYSQL:
        return Mapped(f"varbinary({n})" if n is not None and n <= _BINARY else "longblob")
    return Mapped(f"varbinary({n})" if n is not None and n <= _BINARY else "varbinary(max)")


_PLAIN: dict[TypeFamily, dict[Dialect, str]] = {
    "bool": {Dialect.POSTGRES: "boolean", Dialect.MYSQL: "tinyint(1)", Dialect.TSQL: "bit", Dialect.SQLITE: "INTEGER"},
    "int16": {Dialect.POSTGRES: "smallint", Dialect.MYSQL: "smallint", Dialect.TSQL: "smallint",
              Dialect.SQLITE: "INTEGER"},
    "int32": {Dialect.POSTGRES: "integer", Dialect.MYSQL: "int", Dialect.TSQL: "int", Dialect.SQLITE: "INTEGER"},
    "int64": {Dialect.POSTGRES: "bigint", Dialect.MYSQL: "bigint", Dialect.TSQL: "bigint", Dialect.SQLITE: "INTEGER"},
    "uint64": {Dialect.POSTGRES: "numeric(20)", Dialect.MYSQL: "bigint unsigned", Dialect.TSQL: "decimal(20,0)",
               Dialect.SQLITE: "TEXT"},
    "money": {Dialect.POSTGRES: "numeric(19,4)", Dialect.MYSQL: "decimal(19,4)", Dialect.TSQL: "money",
              Dialect.SQLITE: "TEXT"},
    "float32": {Dialect.POSTGRES: "real", Dialect.MYSQL: "float", Dialect.TSQL: "real", Dialect.SQLITE: "REAL"},
    "float64": {Dialect.POSTGRES: "double precision", Dialect.MYSQL: "double", Dialect.TSQL: "float",
                Dialect.SQLITE: "REAL"},
    "date": {Dialect.POSTGRES: "date", Dialect.MYSQL: "date", Dialect.TSQL: "date", Dialect.SQLITE: "TEXT"},
    "time": {Dialect.POSTGRES: "time", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)", Dialect.SQLITE: "TEXT"},
    "duration": {Dialect.POSTGRES: "interval", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)",
                 Dialect.SQLITE: "TEXT"},
    "year": {Dialect.POSTGRES: "smallint", Dialect.MYSQL: "year", Dialect.TSQL: "smallint", Dialect.SQLITE: "INTEGER"},
    "timestamp": {Dialect.POSTGRES: "timestamp", Dialect.MYSQL: "datetime(6)", Dialect.TSQL: "datetime2(7)",
                  Dialect.SQLITE: "TEXT"},
    "timestamp_tz": {Dialect.POSTGRES: "timestamptz", Dialect.MYSQL: "datetime(6)", Dialect.TSQL: "datetimeoffset(7)",
                     Dialect.SQLITE: "TEXT"},
    "time_tz": {Dialect.POSTGRES: "timetz", Dialect.MYSQL: "time(6)", Dialect.TSQL: "time(7)", Dialect.SQLITE: "TEXT"},
    "interval": {Dialect.POSTGRES: "interval", Dialect.MYSQL: "varchar(64)", Dialect.TSQL: "nvarchar(64)",
                 Dialect.SQLITE: "TEXT"},
    "uuid": {Dialect.POSTGRES: "uuid", Dialect.MYSQL: "char(36)", Dialect.TSQL: "uniqueidentifier",
             Dialect.SQLITE: "TEXT"},
    "xml": {Dialect.POSTGRES: "xml", Dialect.MYSQL: "longtext", Dialect.TSQL: "xml", Dialect.SQLITE: "TEXT"},
}
_NOTES: dict[tuple[TypeFamily, Dialect], str] = {
    ("uint64", Dialect.SQLITE): DIGITS_AS_TEXT,
    ("duration", Dialect.TSQL): "a value of 24 hours or more, or a negative one, fails its row",
    ("timestamp_tz", Dialect.MYSQL): "converted to UTC: the offset is not kept",
    ("time_tz", Dialect.MYSQL): "the offset is dropped",
    ("time_tz", Dialect.TSQL): "the offset is dropped",
    ("interval", Dialect.MYSQL): "PostgreSQL's text of the interval",
    ("interval", Dialect.TSQL): "PostgreSQL's text of the interval",
}
_AS_TEXT_NOTE = "PostgreSQL's text of the value"  # an array, a range, an inet … on another engine
_JSONB_NOTE = "jsonb orders the keys and keeps the last of duplicate keys"


def _json(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    if target is Dialect.POSTGRES:
        if source is Dialect.POSTGRES:
            return Mapped(column.type_name if column.type_name in ("json", "jsonb") else "jsonb")
        return Mapped("jsonb", _JSONB_NOTE)
    return Mapped({Dialect.MYSQL: "json", Dialect.TSQL: "nvarchar(max)", Dialect.SQLITE: "TEXT"}[target])


def _same_engine_custom(column: TableColumn, target: Dialect) -> Mapped:
    """A type of the source database's own, on the same engine: its family's type (a domain's base), or text."""
    mapped = _cross(column, target, target)
    why = f"{column.type_name} is the source database's own type"
    return Mapped(mapped.type_sql, f"{why}: the new column is {mapped.type_sql}" if mapped.type_sql else None,
                  mapped.problem)


def _cross(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    family = column.family
    if family in _UNMAPPABLE:
        return Mapped(None, problem=unmappable(column, target))
    if family == "decimal":
        return _decimal(column, target)
    if family in ("char", "varchar"):
        return _char(column, target, varying=family == "varchar")
    if family == "binary":
        return _binary(column, target)
    if family == "json":
        return _json(column, source, target)
    if family in ("enum", "set"):
        return Mapped({Dialect.POSTGRES: "text", Dialect.MYSQL: "varchar(255)", Dialect.TSQL: "nvarchar(255)",
                       Dialect.SQLITE: "TEXT"}[target])
    if family in _PLAIN:
        return Mapped(_PLAIN[family][target], _NOTES.get((family, target)))
    # text, a PostgreSQL row type, an array, a range, SQLite's untyped column …: the engine's text
    note = _AS_TEXT_NOTE if source is Dialect.POSTGRES and family == "other" and target is not Dialect.SQLITE else None
    return Mapped(_text(target), note)


def target_type(column: TableColumn, source: Dialect, target: Dialect) -> Mapped:
    """The type a new table's column gets for `column` (spec §3.5.2). Same engine: the declared type, unless it is the
    database's own (a domain, an enum, an extension's or an alias type: its family's type then, with a note); spatial
    types keep theirs. Another engine: by family."""
    if source is target:
        if column.custom_type and column.family != "spatial":
            return _same_engine_custom(column, target)
        return Mapped(column.type_name)  # SQLite's untyped column too: none ("")
    if column.family == "money" and target is Dialect.POSTGRES and source is Dialect.POSTGRES:
        return Mapped("money")
    return _cross(column, source, target)


_KINDS: dict[TypeFamily, EditKind] = {
    **dict.fromkeys(("int16", "int32", "int64", "uint64", "decimal", "money", "float32", "float64"), "number"),
    **dict.fromkeys(("date", "time", "timestamp", "timestamp_tz", "time_tz", "duration", "year"), "datetime"),
    "bool": "bool", "binary": "binary", "json": "json",
}


def planned_column(name: str, type_sql: str, dialect: Dialect) -> TableColumn:
    """A new table's column as its catalog will read it — its family and kind from the type's text (MySQL's
    tinyint(1) is read as a number) — what the preview computes its conversions against. The copy itself reads the
    real column after its CREATE TABLE."""
    named = of_name(dialect, type_sql)
    kind = _KINDS.get(named.family, "text")
    if named.family == "bool" and dialect is Dialect.MYSQL:
        kind = "number"
    return TableColumn(name=name, type_name=type_sql, family=named.family, nullable=True, has_default=False,
                       kind=kind, compare="none", writable=True, precision=named.precision, scale=named.scale,
                       length=named.length)


def incompatible(source: TableColumn, target: TableColumn) -> str | None:
    """Why a source column's values cannot go into an existing target column at all (spec §3.5.3: text and binary
    data are not converted into each other), or None."""
    if source.family == "binary" and target.value_kind not in ("binary", "other"):
        return f"{source.name} holds binary data, which does not go into {target.name} ({target.type_name})"
    if target.value_kind == "binary" and source.family in _TEXTUAL:
        return f"{source.name} holds text, which does not go into {target.name} ({target.type_name}), a binary column"
    return None


TYPE_CHARS = 200  # spec §3.5.1: the longest type a column of a new table may be given
# Words that make a column definition more than its type (a constraint, a default, a generated value): refused outside
# parentheses, so a typed type cannot add to the CREATE TABLE what its preview does not say it is
_NOT_A_TYPE = frozenset({"NOT", "NULL", "DEFAULT", "PRIMARY", "KEY", "REFERENCES", "CHECK", "CONSTRAINT", "UNIQUE",
                         "GENERATED", "AS", "IDENTITY", "AUTO_INCREMENT", "AUTOINCREMENT", "ON", "COMMENT"})


def type_problem(dialect: Dialect, text: str) -> str | None:
    """Why `text` is not one type for a new table's column (spec §4 `invalid_type`: `Not a single type: <reason>`),
    or None. The lexer reads it as the filter's check does (`select_builder._single_expression`): every quote and
    comment closed, no `;`, no comment at all (a MySQL executable one would run), parentheses balanced, and no comma or
    constraint word outside them."""
    stripped = text.strip()
    if not stripped:
        return "it is empty"
    if len(stripped) > TYPE_CHARS:
        return f"it is longer than {TYPE_CHARS} characters"
    depth = 0
    words = 0
    for token in tokenize(stripped, dialect):
        if not token.terminated:
            return "it has an unterminated string, quoted name or comment"
        if token.kind in (TokenKind.COMMENT, TokenKind.EXEC_COMMENT):
            return "it has a comment"
        if token.kind in (TokenKind.SEMI, TokenKind.DELIMITER):
            return "it has a ;"
        if token.kind is TokenKind.LPAREN:
            depth += 1
        elif token.kind is TokenKind.RPAREN:
            depth -= 1
            if depth < 0:
                return "it closes a parenthesis it did not open"
        elif depth == 0 and token.kind is TokenKind.COMMA:
            return "it has a comma outside parentheses"
        elif depth == 0 and token.kind is TokenKind.WORD:
            if token.upper in _NOT_A_TYPE:
                return f"{token.text} is not part of a type"
            words += 1
    if depth:
        return "it leaves a parenthesis open"
    return None if words else "it does not name a type"


# ---- values ----

class RowFailure(Exception):
    """A value its target column cannot hold: its row fails (the table is rolled back). The reason never quotes the
    value beyond a special word (`infinity`, a zero date, a number that is neither 0 nor 1)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Convert = Callable[[Any], Any]

_PG_SPECIAL = frozenset({"infinity", "-infinity"})
_ZERO_DATE = re.compile(r"(?:0000-\d\d-\d\d|\d{4}-00-\d\d|\d{4}-\d\d-00)(?:$|[ T])", re.ASCII)
_NAN = frozenset({"nan", "infinity", "-infinity", "inf", "-inf"})
# A date and time with an offset, as PostgreSQL (`+08`, `+05:30`, `+08:05:43`, ` BC`), SQL Server (`+08:00`) and an ISO
# text (`Z`) write it; any number of fractional digits (SQL Server's 7th) is kept as text, never rounded
_WITH_OFFSET = re.compile(
    r"(?P<date>\d{4,})-(?P<month>\d{2})-(?P<day>\d{2})(?P<sep>[ T])(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?(?P<fraction>\.\d+)?\s*(?P<offset>Z|[+-]\d{2}(?::?\d{2}(?::?\d{2})?)?)(?P<bc> BC)?",
    re.ASCII,
)
_TIME_WITH_OFFSET = re.compile(r"(?P<time>\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(?P<offset>Z|[+-]\d{2}(?::?\d{2})*)",
                               re.ASCII)
_DURATION = re.compile(r"(?P<sign>-)?(?P<hours>\d+):(?P<minutes>\d{2})(?::(?P<seconds>\d{2})(?:\.\d+)?)?", re.ASCII)
_DAY_HOURS = 24


def _offset(text: str) -> timedelta:
    if text == "Z":
        return timedelta(0)
    digits = text[1:].replace(":", "")
    hours, minutes, seconds = int(digits[:2]), int(digits[2:4] or 0), int(digits[4:6] or 0)
    delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return -delta if text[0] == "-" else delta


def _parts(value: str) -> re.Match[str]:
    found = _WITH_OFFSET.fullmatch(value.strip())
    if found is None:
        raise RowFailure("is not a date and time with an offset")
    if found.group("bc"):
        raise RowFailure("is a date before the year 1 (BC)")
    return found


def _utc(value: str) -> tuple[datetime, str]:
    """The UTC wall time of a date and time with an offset (no fraction), and its fraction's text."""
    found = _parts(value)
    try:
        local = datetime(int(found["date"]), int(found["month"]), int(found["day"]), int(found["hour"]),
                         int(found["minute"]), int(found["second"] or 0))
        return local - _offset(found["offset"]), found["fraction"] or ""
    except (ValueError, OverflowError):
        raise RowFailure("is outside the years 1 to 9999 in UTC") from None


def to_utc(value: str) -> str:
    """`2026-10-05 12:34:56.1234567+08:00` → `2026-10-05 04:34:56.1234567` (spec §3.5.3: a time zone's value into a
    column without one is converted to UTC; never by the server, which drops the offset, A S10)."""
    when, fraction = _utc(value)
    return when.isoformat(sep=" ") + fraction


def with_minute_offset(value: str) -> str:
    """The offset as ±HH:MM (MySQL and SQL Server refuse PostgreSQL's `+00`, A X5, S10); an offset with seconds (an old
    local mean time) has none, so the value goes in UTC, `+00:00`, the same instant."""
    found = _parts(value)
    offset = found["offset"]
    digits = offset[1:].replace(":", "") if offset != "Z" else "0000"
    if len(digits) > 4 and int(digits[4:]):
        when, fraction = _utc(value)
        return when.isoformat(sep=found["sep"]) + fraction + "+00:00"
    sign = "+" if offset == "Z" else offset[0]
    head = value.strip()[:found.start("offset")].rstrip()
    return f"{head}{sign}{digits[:2]}:{digits[2:4] or '00'}"


def _no_pg_special(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and (value.lower() in _PG_SPECIAL or value.endswith(" BC")):
            what = f"'{value.lower()}'" if value.lower() in _PG_SPECIAL else "a date before the year 1 (BC)"
            raise RowFailure(f"is {what}, which {target} cannot hold")
        return value
    return check


def _no_zero_date(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and _ZERO_DATE.match(value):
            raise RowFailure(f"is a date with a zero part (MySQL's 0000-00-00), which {target} cannot hold")
        return value
    return check


def _finite_number(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value) or (
                isinstance(value, str) and value.strip().lower() in _NAN):
            raise RowFailure(f"is not a finite number (NaN or infinity), which {target} cannot hold")
        return value
    return check


def _not_nan(value: Any) -> Any:
    """SQLite holds ±infinity (written 9e999) but stores NaN as NULL: the row fails instead (§3.5.3, Q3)."""
    if isinstance(value, float) and math.isnan(value) or isinstance(value, str) and value.strip().lower() == "nan":
        raise RowFailure("is NaN, which SQLite cannot hold (it would store NULL)")
    return value


def _within_a_day(value: Any) -> Any:
    if isinstance(value, str) and (found := _DURATION.fullmatch(value.strip())):
        if found["sign"] or int(found["hours"]) >= _DAY_HOURS:
            raise RowFailure("is a time of 24 hours or more, or negative, which SQL Server's time cannot hold")
    return value


def _drop_offset(value: Any) -> Any:
    if isinstance(value, str) and (found := _TIME_WITH_OFFSET.fullmatch(value.strip())):
        return found["time"]
    return value


def _utc_text(value: Any) -> Any:
    return to_utc(value) if isinstance(value, str) else value


def _minute_offset_text(value: Any) -> Any:
    return with_minute_offset(value) if isinstance(value, str) else value


def _time_offset_text(value: Any) -> Any:
    """A time with an offset as text: `+05` → `+05:00`."""
    if isinstance(value, str) and (found := _TIME_WITH_OFFSET.fullmatch(value.strip())):
        offset = found["offset"]
        if offset == "Z":
            return found["time"] + "+00:00"
        digits = offset[1:].replace(":", "")
        return f"{found['time']}{offset[0]}{digits[:2]}:{digits[2:4] or '00'}"
    return value


def _to_bool(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        return value  # a bool, or text the server reads itself ('t', 'true')
    if value in (0, 1):
        return bool(value)
    raise RowFailure(f"holds {value}, which is neither 0 nor 1 (true or false)")


def _bool_to_int(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


def _bool_text(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, int) else value


def _number_text(value: Any) -> Any:
    """A number into a text column as its text (an INTEGER or REAL literal would be stored by SQLite as a number of 15
    digits, R-06-10): every digit kept."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _no_nul(target: str) -> Convert:
    def check(value: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            raise RowFailure(f"holds a NUL character (U+0000), which {target} text cannot hold here")
        return value
    return check


def _no_binary(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RowFailure("holds binary data, which does not go into a text column")
    return value


UTC_NOTE = "converted to UTC: the offset is not kept"
_PG_SPECIAL_NOTE = "PostgreSQL's infinity and dates before the year 1 fail their row"
_ZERO_DATE_NOTE = "MySQL's zero dates (0000-00-00) fail their row"
_DROP_OFFSET_NOTE = "the offset is dropped"
_DAY_NOTE = "a value of 24 hours or more, or a negative one, fails its row"
_TO_BOOL_NOTE = "0 and 1 as false and true; any other number fails its row"
_TO_INT_NOTE = "true and false as 1 and 0"
_BOOL_TEXT_NOTE = "true and false as text"
_FINITE_NOTE = "NaN and infinity fail their row"
_NAN_NOTE = "NaN fails its row (SQLite would store NULL)"
_NUL_NOTE = "text holding a NUL character (U+0000) fails its row"


@dataclass(frozen=True)
class Conversion:
    """How a source column's values go into a target column: `convert` (None: as read) and what the preview says of
    it, one note per rule that changes or refuses a value."""

    convert: Convert | None
    notes: tuple[str, ...] = ()


def _chain(steps: list[Convert]) -> Convert | None:
    if not steps:
        return None
    if len(steps) == 1:
        only = steps[0]
        return lambda value: None if value is None else only(value)

    def convert(value: Any) -> Any:
        if value is None:
            return None
        for step in steps:
            value = step(value)
        return value
    return convert


def conversion(
    source: TableColumn, target: TableColumn, source_dialect: Dialect, target_dialect: Dialect, *,
    backslash_escapes: bool | None = None,
) -> Conversion:
    """How a value of `source` goes into `target` (spec §3.5.3). A value of the same engine's same kind of column goes
    as read: it came from that engine. `backslash_escapes` False: a MySQL target session with NO_BACKSLASH_ESCAPES,
    where no literal can write a NUL."""
    family, kind, into = source.family, target.value_kind, target.family
    if source_dialect is target_dialect and family == into and source.value_kind == kind:
        return Conversion(None)
    name = engine_name(target_dialect)
    steps: list[tuple[Convert, str | None]] = []
    if source_dialect is Dialect.POSTGRES and target_dialect is not Dialect.POSTGRES and family in _DATED:
        steps.append((_no_pg_special(name), _PG_SPECIAL_NOTE))
    if source_dialect is Dialect.MYSQL and target_dialect is not Dialect.MYSQL and family in ("date", "timestamp"):
        steps.append((_no_zero_date(name), _ZERO_DATE_NOTE))
    if family == "timestamp_tz":
        if into in ("timestamp", "date"):
            steps.append((_utc_text, UTC_NOTE))
        elif into == "timestamp_tz" or kind == "text":
            steps.append((_minute_offset_text, None))
    elif family == "time_tz":
        # into a time of day (MySQL's TIME is a duration's family): the offset dropped; into text, kept as ±HH:MM
        dropped = into in ("time", "duration")
        steps.append((_drop_offset, _DROP_OFFSET_NOTE) if dropped else (_time_offset_text, None))
    elif family == "duration" and target_dialect is Dialect.TSQL and into == "time":
        steps.append((_within_a_day, _DAY_NOTE))
    elif family == "bool":
        steps.append((_to_bool, _TO_BOOL_NOTE) if kind == "bool" else (_bool_to_int, _TO_INT_NOTE)
                     if kind == "number" else (_bool_text, _BOOL_TEXT_NOTE))
    numeric = family in _NUMERIC or family == "other"
    if kind == "number" and family in _NON_FINITE and source_dialect in (Dialect.POSTGRES, Dialect.SQLITE):
        if target_dialect in (Dialect.MYSQL, Dialect.TSQL):
            steps.append((_finite_number(name), _FINITE_NOTE))  # MySQL and SQL Server hold no NaN or infinity (06a S3)
        elif target_dialect is Dialect.SQLITE and source_dialect is Dialect.POSTGRES:
            steps.append((_not_nan, _NAN_NOTE))
    if kind in ("text", "json", "datetime"):
        if numeric:
            steps.append((_number_text, None))
        if family != "binary":
            steps.append((_no_binary, None))
        if target_dialect is Dialect.POSTGRES or (target_dialect is Dialect.MYSQL and backslash_escapes is False):
            said = source_dialect is not Dialect.POSTGRES and kind != "datetime"  # PostgreSQL's text holds none
            steps.append((_no_nul(name), _NUL_NOTE if said else None))
    notes = tuple(dict.fromkeys(note for _, note in steps if note))
    return Conversion(_chain([step for step, _ in steps]), notes)
```

- [ ] **Step 5: Run the tests.** `cd backend && .venv/bin/pytest -q tests/test_type_map.py tests/test_table_columns.py tests/test_mysql_types.py tests/test_postgres_dump_catalog.py tests/test_mssql_dump_catalog.py tests/test_mysql_sqlite_dump_catalog.py` → all pass (dry run: 204 passed, 81 of them new; about 2 minutes, live). Then the edit targets, which read the same columns: `.venv/bin/pytest -q tests/test_editing_target.py tests/test_postgres_edits.py tests/test_mysql_edits.py tests/test_mssql_edits.py tests/test_sqlite_edits.py` → all pass (dry run: 62). The refactor must not move a family: 06a's and ⑤'s tests are the check.

- [ ] **Step 6: Commit.**

```bash
git add backend/dbaguy_backend/adapters/families.py backend/dbaguy_backend/sql/type_map.py backend/tests/test_type_map.py \
  backend/dbaguy_backend/adapters/columns.py backend/dbaguy_backend/adapters/mysql/types.py \
  backend/dbaguy_backend/adapters/postgres/columns.py backend/dbaguy_backend/adapters/mysql/columns.py \
  backend/dbaguy_backend/adapters/mssql/columns.py backend/dbaguy_backend/adapters/sqlite/columns.py
git commit -m "feat(06c): type families in one place and the copy's type map"
```

---

### Task 2: The copy's preview — the one-time plan store, the request, `POST /data/transfer/plan`

**Suggested implementer:** Opus (two catalogs, every mode's rules, the one-time plan of D-06-8).

**Files:**
- Create: `backend/dbaguy_backend/services/plans.py`, `backend/dbaguy_backend/services/transfer_plans.py`, `backend/dbaguy_backend/services/transfer_service.py`, `backend/dbaguy_backend/domain/transfer.py`, `backend/dbaguy_backend/sql/transfer_sql.py`
- Modify: `backend/dbaguy_backend/services/edit_plans.py`, `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/{postgres,mysql,mssql,sqlite}/adapter.py`, `backend/dbaguy_backend/domain/profiles.py`, `backend/dbaguy_backend/adapters/editing.py`, `backend/dbaguy_backend/api/data.py`, `backend/dbaguy_backend/container.py`, `backend/tests/data_support.py`, `src/features/data-tools/entryPoints.test.ts`, `src/lib/api/types.ts`
- Test: `backend/tests/test_transfer_sql.py`, `backend/tests/test_transfer_plan.py`, `backend/tests/transfer_support.py` (new)
- Generated: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: Task 1 (`target_type`, `planned_column`, `conversion`, `incompatible`, `type_problem`, `engine_name`, `of_name`); 06a's `DataTools.table_columns`, `build_copy_select` / `check_filter` / `relation_from_path` (`sql/select_builder.py`), `parents_first` and `DumpTable` (`adapters/dump.py`), `RESTORE_SETTINGS` / `RESET_SETTINGS` / `RESTORE_CHECKS_FOREIGN_KEYS` (`sql/dump_script.py`), `cannot()` (`domain/data_jobs.py`); ⑤'s `RowEditing.edit_target`, `EditTarget`, `edit_kind`, `EditPlanStore`, `PLAN_EXPIRED`/`PLAN_MISMATCH`, `MAX_PLAN_CHARS`; `MetadataService.call`, `SchemaCache.get`, `SafetyPolicy`'s `is_plain_read`; `identity_insert`, `qualified`, `quote_ident`, `quote_literal`, `script_text` (`sql/dialects.py`).
- Produces:
  - `services/plans.py`: `OneTimePlanStore[P]` (⑤'s store, generic; `put(plan)`, `take(plan_id, profile_id, database) -> P` raising `PlanUnavailable` with `plan_expired` / `plan_mismatch`; uuid4 ids, taken once, `PLAN_TTL_S` on `CLOCK_MONOTONIC`, `MAX_PLANS`, `max_chars` with the oldest plan dropped first and one too large refused with its `too_large` text); `awake_or_asleep()`. `EditPlanStore` is now `OneTimePlanStore[EditPlan]` with the same behaviour (`test_edit_plans.py` unchanged).
  - `domain/transfer.py`: `QuerySource{sql, columns: list[ColumnMeta], title}`, `SourceSpec{profile_id, database, tables | query, filter}` (one of tables or query; a filter only with one table), `TargetSpec{profile_id, database, schema_name, table, mode, overrides, identity, left_out}` (`overrides`, `identity`, `left_out` keyed by the source table's key, then the source column), `TransferRequest{source, target}` (a new name only for one source), `TransferProblem{code, message, column}`, `ColumnMapping{source_name, source_type, family, target_name, target_type, type_editable, overridden, conversion, skipped, identity, problem}`, `TableTransfer{source, target, source_key, source_path, mode, columns, before, after, warnings, problems}`, `TransferPlan{plan_id, source_profile_id, tables, warnings, problems}` (`plan_id` null while any problem stands), `COPY_DATA` (§4's "copy data").
  - `services/transfer_plans.py`: `PlannedColumn(source: int, target: str)`, `PlannedTable` (the SELECT, the source columns read, the written columns, `before`, `after`, `created`, `create_first` (MySQL's CREATE TABLE, before the transaction), `transactional`, `filtered`; `statements`), `StoredTransfer` (plan id, target profile and database, source profile and database, `query`, tables, History `summary`, `title`), `TransferPlanStore`.
  - `services/transfer_service.py`: `TransferService(profiles, registry, metadata, schema_cache, plans=None)`, `.plans`, `.plan(request: TransferRequest) -> TransferPlan` — refuses a read-only target (`ReadOnlyProfile`, 403) and an engine without `copy_data` on either side (`NotSupported`: `<Engine> connections cannot copy data`, 422) before any connection; reads each source relation's `table_columns` on the source's metadata connection (a query: `query_columns(dialect, metas)` through `of_name`, refused unless a plain read, columns must have distinct names); orders the tables parents first from the source's schema snapshot (FK cycles warned only when appending into PostgreSQL or SQL Server, which check FKs row by row); reads each target's `table_columns` and `edit_target` on the target's metadata connection; then per mode: **create** — `target_exists` if the table is there, each column's type (a typed override checked by `type_problem` → `invalid_type`), its conversion notes, the source's primary key when all its columns are copied, MySQL's CREATE TABLE before the transaction with `CREATED_STAYS`; **append / replace_rows** — `target_missing`, `target_not_writable` (`not_writable_reason`), columns matched by name (exact first, else the one that differs only in case), a target column the source lacks: its default, or `missing_column` when it is NOT NULL without one, the target's computed and generated columns not written, `incompatible` → `unconvertible`, identities kept by default (PostgreSQL's `GENERATED ALWAYS` through `OVERRIDING SYSTEM VALUE`, which 06a's `InsertBatcher` adds; SQL Server's `SET IDENTITY_INSERT … ON` before the rows and `OFF` after; PostgreSQL's `setval` after the rows for every written integer or decimal column with an identity or a default, R-06-9) or left to the target (`identity: generate`: the column is not written), SQL Server non-Unicode columns warned (`NOT_UNICODE`), a non-transactional table warned (`NON_TRANSACTIONAL`), replace's `DELETE FROM` first; both: the source's not-copied columns listed (`not copied: <reason>`), the user's left-out columns (`left out`), `same_table` / `same_sqlite_file`, `source_missing`, `no_columns`; the filter through `check_filter` and the SELECT `is_plain_read`. Stores the plan when there is no problem.
  - `sql/transfer_sql.py`: `TARGET_SETUP` (06a's `RESTORE_SETTINGS`; SQL Server's with `SET IMPLICIT_TRANSACTIONS OFF` first), `TARGET_RESET`, `SOURCE_SETTINGS`, `read_settings(dialect) -> str | None`, `set_setting(dialect, name, value) -> str`, `create_table(dialect, schema, name, columns, key) -> str`, `delete_rows(dialect, schema, name) -> str`, `identity_switch(dialect, schema, name) -> (on, off) | None`, `advance_sequence(schema, name, column) -> str` (PostgreSQL: `setval(seq, GREATEST(max(col), last_value))`, nothing when the column has no sequence or the table no row), `summary(source, target, tables, dialect, statements) -> str` (History's text: `-- Copy data: <source> <name> → <target> <name> (<mode>)`, or `…, N tables`, then each table and the statements).
  - `AdapterCapabilities.copy_data` / `EngineInfo.copy_data` (PostgreSQL, MySQL, SQL Server, SQLite; not Couchbase, R-06-14); `not_writable_reason(target: EditTarget) -> str | None` (`adapters/editing.py`: the relation-level reasons of ⑤'s target — a view, a shadow, history or ledger table, PostgreSQL without standard strings — while a table without a usable key is still appended to).
  - `POST /data/transfer/plan` (`api/data.py`) → `TransferPlan`; `AppContainer.transfers`.
  - `tests/data_support.SCRATCH_06C = "zz_06c_"` (accepted by `scratch_name` and `dropping`); `tests/transfer_support.py`: `table_name(kind)`, `profile(app, db, name)`, `tables_request(…)`, `query_request(…)`.
  - `src/lib/api/types.ts`: `TransferRequest`, `SourceSpec`, `TargetSpec`, `QuerySource`, `TransferPlan`, `TableTransfer`, `ColumnMapping`, `TransferProblem`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/transfer_support.py`, `backend/tests/test_transfer_sql.py` and `backend/tests/test_transfer_plan.py`, and let `data_support` make `zz_06c_*` names:

```python
"""Copy-data tests (06c): requests between two dev databases, scratch tables `zz_06c_*` made and dropped by exact name
(`data_support.dropping`), checks through each engine's own driver (`edit_support.ScratchDb.rows`)."""

from typing import Any
from uuid import UUID

from dbaguy_backend.domain.transfer import QuerySource, SourceSpec, TargetSpec, TransferRequest
from tests.data_support import SCRATCH_06C, DataApp, scratch_name
from tests.edit_support import ScratchDb


def table_name(kind: str = "t") -> str:
    return scratch_name(kind, SCRATCH_06C)


def profile(app: DataApp, db: ScratchDb, name: str) -> UUID:
    """A profile of `db` in the app (its password in memory only)."""
    return app.add_profile(db.profile(name=name))


def tables_request(
    source: ScratchDb, source_profile: UUID, names: list[str], target: ScratchDb, target_profile: UUID,
    mode: str = "create", *, filter: str | None = None, **target_fields: Any,
) -> TransferRequest:
    """Copy `names` of `source`'s schema into `target`'s schema."""
    return TransferRequest(
        source=SourceSpec(profile_id=source_profile, database=source.database,
                          tables=[source.path(name) for name in names], filter=filter),
        target=TargetSpec(profile_id=target_profile, database=target.database, schema_name=target.schema, mode=mode,
                          **target_fields),
    )


def query_request(source: ScratchDb, source_profile: UUID, sql: str, columns: list[dict], target: ScratchDb,
                  target_profile: UUID, table: str, mode: str = "create", **target_fields: Any) -> TransferRequest:
    return TransferRequest(
        source=SourceSpec(profile_id=source_profile, database=source.database,
                          query=QuerySource(sql=sql, columns=columns, title="Query 1")),
        target=TargetSpec(profile_id=target_profile, database=target.database, schema_name=target.schema, mode=mode,
                          table=table, **target_fields),
    )
```

```python
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
```

```python
"""Copy data's preview (spec ⑥ §3.5.1, 06c Task 2): mappings, statements, problems and the one-time plan, on the dev
databases. Scratch tables are `zz_06c_*`, dropped by exact name; nothing is copied here."""

from pathlib import Path

import pytest

from dbaguy_backend.domain.edits import PLAN_EXPIRED, PLAN_MISMATCH, PlanUnavailable
from dbaguy_backend.domain.errors import NotSupported, ReadOnlyProfile
from dbaguy_backend.domain.transfer import SourceSpec, TargetSpec, TransferRequest
from dbaguy_backend.services.transfer_service import CREATED_STAYS, QUERY_RUNS_AGAIN
from tests.data_support import DataApp, dropping
from tests.edit_support import ENGINES, scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg
from tests.transfer_support import profile, query_request, table_name, tables_request

ORDERS = {
    "postgres": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed timestamptz NOT NULL, amount numeric(12,2), "
                "note text, done boolean)",
    "mysql": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed datetime(6) NOT NULL, amount decimal(12,2), "
             "note text, done tinyint(1))",
    "mssql": "CREATE TABLE {t} (id bigint PRIMARY KEY, placed datetimeoffset(7) NOT NULL, amount decimal(12,2), "
             "note nvarchar(max), done bit)",
    "sqlite": "CREATE TABLE {t} (id INTEGER PRIMARY KEY, placed DATETIME NOT NULL, amount DECIMAL(12,2), note TEXT, "
              "done BOOLEAN)",
}


@pytest.fixture
def app(tmp_path: Path):
    app = DataApp(tmp_path)
    yield app
    app.close()


@requires_pg
@requires_mysql
def test_a_table_maps_to_the_target_engines_types_with_its_key_and_notes(app: DataApp, tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as made:
            name = table_name()
            pg.execute(ORDERS["postgres"].format(t=pg.table(name)))
            made.append(("TABLE", name))
            plan = app.container.transfers.plan(tables_request(pg, profile(app, pg, "pg"), [name], my,
                                                               profile(app, my, "my")))
        assert plan.plan_id is not None and plan.problems == []
        [table] = plan.tables
        assert (table.source, table.target, table.mode) == (f"public.{name}", name, "create")
        assert [(c.source_name, c.target_type) for c in table.columns] == [
            ("id", "bigint"), ("placed", "datetime(6)"), ("amount", "decimal(12,2)"), ("note", "longtext"),
            ("done", "tinyint(1)")]
        placed = table.columns[1]
        assert placed.conversion == ("converted to UTC: the offset is not kept; PostgreSQL's infinity and dates "
                                     "before the year 1 fail their row")
        assert table.columns[4].conversion == "true and false as 1 and 0"
        assert table.before == [f"CREATE TABLE dbaguy.{name} (\n  id bigint NOT NULL,\n  placed datetime(6) NOT NULL,"
                                "\n  amount decimal(12,2),\n  note longtext,\n  done tinyint(1),\n  PRIMARY KEY (id)\n)"]
        assert CREATED_STAYS.format(name) in table.warnings
    finally:
        pg.close()
        my.close()


@pytest.mark.parametrize("engine", ENGINES)
def test_create_refuses_a_table_that_exists_and_append_one_that_does_not(app: DataApp, tmp_path: Path,
                                                                        engine: str) -> None:
    db = scratch_db(engine, tmp_path)
    try:
        with dropping(db) as made:
            source, there = table_name("s"), table_name("e")
            for name in (source, there):
                db.execute(ORDERS[engine].format(t=db.table(name)))
                made.append(("TABLE", name))
            me = profile(app, db, "db")
            exists = app.container.transfers.plan(tables_request(db, me, [source], db, me, "create", table=there))
            missing = app.container.transfers.plan(tables_request(db, me, [source], db, me, "append",
                                                                  table=table_name("m")))
            same = app.container.transfers.plan(tables_request(db, me, [source], db, me, "append"))
        assert exists.plan_id is None and [p.code for p in exists.tables[0].problems] == ["target_exists"]
        assert exists.tables[0].problems[0].message.endswith("already exists: append to it, or choose another name")
        assert [p.code for p in missing.tables[0].problems] == ["target_missing"]
        assert [p.code for p in same.tables[0].problems] == ["same_table"]
        if engine == "sqlite":  # one file: every copy within it is refused
            assert [p.code for p in exists.problems] == ["same_sqlite_file"]
    finally:
        db.close()


@requires_pg
@requires_mssql
def test_append_maps_columns_by_name_and_writes_what_identities_need(app: DataApp, tmp_path: Path) -> None:
    pg, ms = scratch_db("postgres", tmp_path), scratch_db("mssql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(ms) as ms_made:
            source, into_pg, into_ms = table_name("s"), table_name("p"), table_name("q")
            pg.execute(f'CREATE TABLE {pg.table(source)} ("ID" int, label text, extra text, n serial)')
            pg.execute(f"CREATE TABLE {pg.table(into_pg)} (id int GENERATED ALWAYS AS IDENTITY, label text, "
                       "n int DEFAULT 1)")
            ms.execute(f"CREATE TABLE {ms.table(into_ms)} (id int IDENTITY(1,1), label nvarchar(50), n int, "
                       "must int NOT NULL)")
            pg_made += [("TABLE", source), ("TABLE", into_pg)]
            ms_made.append(("TABLE", into_ms))
            me, other = profile(app, pg, "pg"), profile(app, ms, "ms")
            keep = app.container.transfers.plan(tables_request(pg, me, [source], pg, me, "append", table=into_pg))
            generate = app.container.transfers.plan(tables_request(
                pg, me, [source], ms, other, "replace_rows", table=into_ms,
                identity={f"public.{source}": {"ID": "generate"}}))
        [table] = keep.tables
        assert [(c.source_name, c.target_name, c.identity, c.skipped) for c in table.columns] == [
            ("ID", "id", "keep", None), ("label", "label", None, None), ("extra", None, None, "no such column in the "
                                                                                              "target"),
            ("n", "n", None, None)]
        assert [line.splitlines()[0] for line in table.after] == ["SELECT setval(s.seq, GREATEST(s.top, "
                                                                  "COALESCE(pg_sequence_last_value(s.seq), s.top)))"] * 2
        assert keep.plan_id is not None
        [table] = generate.tables
        assert table.before == [f"DELETE FROM dbo.{into_ms}"]  # the identity is generated: no IDENTITY_INSERT
        assert [p.code for p in table.problems] == ["missing_column"]
        assert generate.plan_id is None
    finally:
        pg.close()
        ms.close()


@requires_mssql
@requires_pg
def test_a_type_without_a_counterpart_is_chosen_or_left_out(app: DataApp, tmp_path: Path) -> None:
    ms, pg = scratch_db("mssql", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(ms) as made:
            name = table_name()
            ms.execute(f"CREATE TABLE {ms.table(name)} (id int PRIMARY KEY, g geography, v sql_variant)")
            made.append(("TABLE", name))
            source, target = profile(app, ms, "ms"), profile(app, pg, "pg")
            key = f"dbo.{name}"
            asked = app.container.transfers.plan(tables_request(ms, source, [name], pg, target))
            typed = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                                overrides={key: {"g": "bytea"}}))
            bad = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                              overrides={key: {"g": "bytea; DROP TABLE x"}}))
            left = app.container.transfers.plan(tables_request(ms, source, [name], pg, target,
                                                               left_out={key: ["g"]}))
        assert [p.code for p in asked.tables[0].problems] == ["unmappable_type"]
        assert asked.tables[0].columns[0].skipped == "not copied: sql_variant column: only its raw bytes can be read"
        assert typed.plan_id is not None and typed.tables[0].columns[2].overridden
        assert [p.message for p in bad.tables[0].problems] == ["g: Not a single type: it has a ;"]
        assert left.plan_id is not None and left.tables[0].columns[2].skipped == "left out"
    finally:
        ms.close()
        pg.close()


@requires_pg
def test_tables_go_parents_first_and_a_query_runs_again(app: DataApp, tmp_path: Path) -> None:
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        with dropping(pg) as made:
            parent, child = table_name("p"), table_name("c")
            pg.execute(f"CREATE TABLE {pg.table(parent)} (id int PRIMARY KEY)",
                       f"CREATE TABLE {pg.table(child)} (id int PRIMARY KEY, p int REFERENCES {pg.table(parent)})")
            made += [("TABLE", parent), ("TABLE", child)]
            me, target = profile(app, pg, "pg"), profile(app, lite, "lite")
            plan = app.container.transfers.plan(tables_request(pg, me, [child, parent], lite, target))
            query = app.container.transfers.plan(query_request(
                pg, me, "SELECT 1 AS a, now() AS b", [{"name": "a", "type_name": "int4", "category": "number"},
                                                     {"name": "b", "type_name": "timestamptz",
                                                      "category": "datetime"}], lite, target, "zz_06c_q"))
            write = app.container.transfers.plan(query_request(
                pg, me, f"DELETE FROM {pg.table(parent)} RETURNING id", [{"name": "id", "type_name": "int4",
                                                                          "category": "number"}],
                lite, target, "zz_06c_q"))
            twice = app.container.transfers.plan(query_request(
                pg, me, "SELECT 1 AS a, 2 AS a", [{"name": "a", "type_name": "int4", "category": "number"}] * 2,
                lite, target, "zz_06c_q"))
        assert [table.source for table in plan.tables] == [f"public.{parent}", f"public.{child}"]
        assert query.plan_id is not None and QUERY_RUNS_AGAIN in query.warnings
        assert [c.target_type for c in query.tables[0].columns] == ["INTEGER", "TEXT"]
        assert [p.code for p in write.problems] == ["query_not_select"]
        assert [p.code for p in twice.problems] == ["duplicate_column"]
    finally:
        pg.close()
        lite.close()


def test_a_read_only_target_and_an_engine_without_copies_are_refused_first(app: DataApp, tmp_path: Path) -> None:
    lite = scratch_db("sqlite", tmp_path)
    source = profile(app, lite, "source")
    read_only = app.add_profile(lite.profile(name="ro", read_only=True))
    bucket = app.add_profile({"profile": {"engine": "couchbase", "name": "cb", "host": "127.0.0.1"},
                              "secrets": {"password": "x"}})

    def request(target: object) -> TransferRequest:
        return TransferRequest(source=SourceSpec(profile_id=source, tables=[lite.path("t")]),
                               target=TargetSpec(profile_id=target, schema_name="main", mode="create"))

    with pytest.raises(ReadOnlyProfile):
        app.container.transfers.plan(request(read_only))
    with pytest.raises(NotSupported, match="Couchbase connections cannot copy data"):
        app.container.transfers.plan(request(bucket))
    lite.close()


def test_a_plan_is_taken_once_by_the_targets_session(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    lite, other = scratch_db("sqlite", tmp_path), scratch_db("sqlite", tmp_path / "other")
    try:
        with dropping(lite) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (id INTEGER PRIMARY KEY)")
            made.append(("TABLE", name))
            source, target = profile(app, lite, "a"), profile(app, other, "b")
            plan = app.container.transfers.plan(tables_request(lite, source, [name], other, target))
        store = app.container.transfers.plans
        with pytest.raises(PlanUnavailable) as wrong:
            store.take(plan.plan_id, source, None)
        assert wrong.value.code == PLAN_MISMATCH
        taken = store.take(plan.plan_id, target, None)
        assert (taken.source_profile_id, taken.query, len(taken.tables)) == (source, False, 1)
        assert taken.summary.startswith(f"-- Copy data: a main.{name} → b main.{name} (create)\nCREATE TABLE")
        with pytest.raises(PlanUnavailable) as again:
            store.take(plan.plan_id, target, None)
        assert again.value.code == PLAN_EXPIRED
    finally:
        lite.close()
        other.close()
```

```diff
diff --git a/backend/tests/data_support.py b/backend/tests/data_support.py
index aab762d..0b26652 100644
--- a/backend/tests/data_support.py
+++ b/backend/tests/data_support.py
@@ -34,7 +34,8 @@ from tests.test_sessions import Recorder, request_events
 
 SCRATCH_PREFIX = "zz_06_"
 SCRATCH_06B = "zz_06b_"  # 06b's own (its fix wave's tests)
-_SCRATCH = (SCRATCH_PREFIX, SCRATCH_06B)
+SCRATCH_06C = "zz_06c_"  # 06c's own (copy data)
+_SCRATCH = (SCRATCH_PREFIX, SCRATCH_06B, SCRATCH_06C)
 _ROW_TUPLE = _mssql.ROW_FORMAT_TUPLE
 
 
@@ -66,7 +67,7 @@ def dropping(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> It
             except Exception as exc:
                 failures.append(f"before_drop: {exc}")
         for kind, name in reversed(made):
-            assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped here"
+            assert name.startswith(_SCRATCH), f"{name!r} is not a scratch object: never dropped here"
             try:
                 db.execute(drop_statement(db, kind, name))
             except Exception as exc:
```

- [ ] **Step 2: Run them to see them fail.** `cd backend && .venv/bin/pytest -q tests/test_transfer_sql.py tests/test_transfer_plan.py` → collection errors (`No module named 'dbaguy_backend.sql.transfer_sql'`, `No module named 'dbaguy_backend.domain.transfer'`).

- [ ] **Step 3: The generic plan store.** Create `backend/dbaguy_backend/services/plans.py` and rebase `EditPlanStore` on it:

```python
"""Previewed plans a session may run once (spec ⑥ D-06-8, from ⑤ D-05-7): in memory, each for PLAN_TTL_S, taken once
and only by a session the plan names (its profile, and its database when it names one, ⑤ E3). At most MAX_PLANS and
MAX_PLAN_CHARS of their text together: the plan made longest ago goes first (a plan is used once, by being taken, so that
is also the least recently used). A backend restart forgets them all: the client previews again. Grid edits keep their
plans in one store (`edit_plans.EditPlanStore`), copies in another (`transfer_plans.TransferPlanStore`)."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from loguru import logger

from ..domain.edits import (
    MAX_PLAN_CHARS, MAX_PLANS, PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE, PLAN_TTL_S,
    PlanUnavailable,
)
from ..domain.errors import ValidationFailed


class Plan(Protocol):
    @property
    def plan_id(self) -> str: ...

    @property
    def profile_id(self) -> UUID: ...

    @property
    def database(self) -> str | None: ...


P = TypeVar("P", bound=Plan)


@dataclass(frozen=True, slots=True)
class _Entry(Generic[P]):
    made: float
    chars: int
    plan: P


def awake_or_asleep() -> float:
    """Seconds that keep counting while the Mac sleeps: `time.monotonic` is `mach_absolute_time` there, which stops, so a
    plan would outlive its 15 minutes across a closed lid."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class OneTimePlanStore(Generic[P]):
    """`chars(plan)`: what a plan holds that grows with it (its statements' text); `too_large`: the refusal of a plan
    larger than every plan's bound together."""

    def __init__(
        self, chars: Callable[[P], int], too_large: str, *, what: str = "plan", ttl_s: float = PLAN_TTL_S,
        capacity: int = MAX_PLANS, clock: Callable[[], float] = awake_or_asleep, max_chars: int = MAX_PLAN_CHARS,
    ) -> None:
        self._chars_of = chars
        self._too_large = too_large
        self._what = what
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._max_chars = max_chars
        self._clock = clock
        self._plans: OrderedDict[str, _Entry[P]] = OrderedDict()  # oldest made first
        self._chars = 0  # the sum of every entry's `chars`
        self._lock = threading.Lock()

    def put(self, plan: P) -> None:
        """Keep `plan`, making room: the expired go first, then the oldest until both bounds hold. A plan larger than the
        whole text bound is refused (ValidationFailed, 422) and nothing else is touched."""
        chars = self._chars_of(plan)
        if chars > self._max_chars:
            raise ValidationFailed(self._too_large)
        with self._lock:
            self._prune()
            self._remove(plan.plan_id)
            self._plans[plan.plan_id] = _Entry(self._clock(), chars, plan)
            self._chars += chars
            evicted = 0
            while len(self._plans) > self._capacity or self._chars > self._max_chars:
                self._remove(next(iter(self._plans)))
                evicted += 1
            if evicted:
                logger.debug("{} older {}(s) made room for a new one ({} stored)", evicted, self._what,
                             len(self._plans))

    def take(self, plan_id: str, profile_id: UUID, database: str | None) -> P:
        """The plan, removed: it runs once. PlanUnavailable(plan_expired) when there is none (expired, made room for,
        taken already, never made); PlanUnavailable(plan_mismatch) when it is another profile's or another database's,
        and it then stays for its own session."""
        with self._lock:
            self._prune()
            entry = self._plans.get(plan_id)
            if entry is None:
                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
            plan = entry.plan
            if plan.profile_id != profile_id or (plan.database is not None and plan.database != database):
                raise PlanUnavailable(PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE)
            self._remove(plan_id)
            return plan

    def _prune(self) -> None:
        now = self._clock()
        for plan_id in [plan_id for plan_id, entry in self._plans.items() if now - entry.made >= self._ttl_s]:
            self._remove(plan_id)

    def _remove(self, plan_id: str) -> None:
        entry = self._plans.pop(plan_id, None)
        if entry is not None:
            self._chars -= entry.chars
```

```diff
diff --git a/backend/dbaguy_backend/services/edit_plans.py b/backend/dbaguy_backend/services/edit_plans.py
index 66f3479..d2bf4b1 100644
--- a/backend/dbaguy_backend/services/edit_plans.py
+++ b/backend/dbaguy_backend/services/edit_plans.py
@@ -1,23 +1,11 @@
-"""The previewed plans a session may apply (spec ⑤ D-05-7, R-05-7, CR-2, pre-flight L15): in memory, each for PLAN_TTL_S,
-taken once and only by a session of its profile and — when its path names one — its database (ruling E3). At most
-MAX_PLANS and MAX_PLAN_CHARS of statement and script text together: the plan made longest ago goes first (a plan is
-used once, by being taken, so that is also the least recently used). A backend restart forgets them all: the client
-previews again."""
+"""The previewed plans a session may apply (spec ⑤ D-05-7, R-05-7, CR-2, pre-flight L15): the one-time plan store
+(`plans.OneTimePlanStore`, ⑥ D-06-8) over grid-edit plans, whose text is their statements and script. A plan larger than
+every plan's bound together is refused at the preview (`PLAN_TOO_LARGE`, 422)."""
 
-import threading
-import time
-from collections import OrderedDict
 from collections.abc import Callable
-from dataclasses import dataclass
-from uuid import UUID
 
-from loguru import logger
-
-from ..domain.edits import (
-    MAX_PLAN_CHARS, MAX_PLANS, PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE, PLAN_TOO_LARGE,
-    PLAN_TTL_S, EditPlan, PlanUnavailable,
-)
-from ..domain.errors import ValidationFailed
+from ..domain.edits import MAX_PLAN_CHARS, MAX_PLANS, PLAN_TOO_LARGE, PLAN_TTL_S, EditPlan
+from .plans import OneTimePlanStore, awake_or_asleep
 
 
 def plan_chars(plan: EditPlan) -> int:
@@ -25,71 +13,10 @@ def plan_chars(plan: EditPlan) -> int:
     return len(plan.script) + sum(len(statement.sql) for statement in plan.statements)
 
 
-@dataclass(frozen=True, slots=True)
-class _Entry:
-    made: float
-    chars: int
-    plan: EditPlan
-
-
-def _awake_or_asleep() -> float:
-    """Seconds that keep counting while the Mac sleeps: `time.monotonic` is `mach_absolute_time` there, which stops, so a
-    plan would outlive its 15 minutes across a closed lid."""
-    return time.clock_gettime(time.CLOCK_MONOTONIC)
-
-
-class EditPlanStore:
+class EditPlanStore(OneTimePlanStore[EditPlan]):
     def __init__(
-        self, ttl_s: float = PLAN_TTL_S, capacity: int = MAX_PLANS, clock: Callable[[], float] = _awake_or_asleep,
+        self, ttl_s: float = PLAN_TTL_S, capacity: int = MAX_PLANS, clock: Callable[[], float] = awake_or_asleep,
         max_chars: int = MAX_PLAN_CHARS,
     ) -> None:
-        self._ttl_s = ttl_s
-        self._capacity = capacity
-        self._max_chars = max_chars
-        self._clock = clock
-        self._plans: OrderedDict[str, _Entry] = OrderedDict()  # oldest made first
-        self._chars = 0  # the sum of every entry's `chars`
-        self._lock = threading.Lock()
-
-    def put(self, plan: EditPlan) -> None:
-        """Keep `plan`, making room: the expired go first, then the oldest until both bounds hold. A plan larger than the
-        whole text bound is refused (ValidationFailed, 422) and nothing else is touched."""
-        chars = plan_chars(plan)
-        if chars > self._max_chars:
-            raise ValidationFailed(PLAN_TOO_LARGE)
-        with self._lock:
-            self._prune()
-            self._remove(plan.plan_id)
-            self._plans[plan.plan_id] = _Entry(self._clock(), chars, plan)
-            self._chars += chars
-            evicted = 0
-            while len(self._plans) > self._capacity or self._chars > self._max_chars:
-                self._remove(next(iter(self._plans)))
-                evicted += 1
-            if evicted:
-                logger.debug("{} older edit plan(s) made room for a new one ({} stored)", evicted, len(self._plans))
-
-    def take(self, plan_id: str, profile_id: UUID, database: str | None) -> EditPlan:
-        """The plan, removed: it runs once. PlanUnavailable(plan_expired) when there is none (expired, made room for,
-        taken already, never made); PlanUnavailable(plan_mismatch) when it is another profile's or another database's,
-        and it then stays for its own session."""
-        with self._lock:
-            self._prune()
-            entry = self._plans.get(plan_id)
-            if entry is None:
-                raise PlanUnavailable(PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE)
-            plan = entry.plan
-            if plan.profile_id != profile_id or (plan.database is not None and plan.database != database):
-                raise PlanUnavailable(PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE)
-            self._remove(plan_id)
-            return plan
-
-    def _prune(self) -> None:
-        now = self._clock()
-        for plan_id in [plan_id for plan_id, entry in self._plans.items() if now - entry.made >= self._ttl_s]:
-            self._remove(plan_id)
-
-    def _remove(self, plan_id: str) -> None:
-        entry = self._plans.pop(plan_id, None)
-        if entry is not None:
-            self._chars -= entry.chars
+        super().__init__(plan_chars, PLAN_TOO_LARGE, what="edit plan", ttl_s=ttl_s, capacity=capacity, clock=clock,
+                         max_chars=max_chars)
```

- [ ] **Step 4: The request, the preview and the stored plan.** Create `backend/dbaguy_backend/domain/transfer.py` and `backend/dbaguy_backend/services/transfer_plans.py`:

```python
"""Copy data between connections (spec ⑥ G3, §3.5): what the dialog asks for (`TransferRequest`), what the preview
says (`TransferPlan`, POST /data/transfer/plan), and the one-time plan a copy job takes (`StoredTransfer`): the copy
runs exactly what was previewed (R-05-7, D-06-8), and the command carries only the plan's id
(`services/transfer_plans.py` keeps it)."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution import ColumnMeta
from .metadata import PathSegment

MAX_TRANSFER_TABLES = 500  # spec §3.5.1: one copy holds at most this many tables
MAX_QUERY_CHARS = 1_048_576  # a query result's statement (the editor's, run again)
MAX_QUERY_COLUMNS = 4_096
TRANSFER_FILTER_LENGTH = 65_536  # as a dump's (DUMP_FILTER_LENGTH)
TYPE_TEXT_LENGTH = 1_024  # a typed type as the request takes it; the plan says `invalid_type` past TYPE_CHARS (200)
NAME_LENGTH = 1_024
TransferMode = Literal["create", "append", "replace_rows"]
IdentityChoice = Literal["keep", "generate"]
COPY_DATA = "copy data"  # what a connection without `copy_data` cannot (spec §4)
ONE_TABLE_FILTER = "a filter applies to a copy of one table"


class QuerySource(BaseModel):
    """A query tab's result (spec §3.5.1): its statement, run again on the tab's own session as an export is (the same
    transaction, its temporary tables), and the columns the tab already has, which the preview maps without running it."""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    columns: list[ColumnMeta] = Field(min_length=1, max_length=MAX_QUERY_COLUMNS)
    title: str = Field(default="the query", max_length=NAME_LENGTH)  # what messages call it: the tab's name


class SourceSpec(BaseModel):
    """Tables, views and materialized views of one connection, or a query tab's result."""

    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = Field(default=None, max_length=NAME_LENGTH)  # PostgreSQL, SQL Server: the database node's
    tables: list[list[PathSegment]] | None = Field(default=None, min_length=1, max_length=MAX_TRANSFER_TABLES)
    query: QuerySource | None = None
    filter: str | None = Field(default=None, max_length=TRANSFER_FILTER_LENGTH)  # one table: the table-data tab's

    @model_validator(mode="after")
    def _tables_or_a_query(self) -> Self:
        if (self.tables is None) == (self.query is None):
            raise ValueError("a copy's source is tables or a query, one of them")
        if self.filter is not None and (self.tables is None or len(self.tables) != 1):
            raise ValueError(ONE_TABLE_FILTER)
        return self


class TargetSpec(BaseModel):
    """Where the rows go (spec §3.5.1). `overrides`, `identity` and `left_out` are by the source table's name (a
    query's: its `title`) and then the source column's."""

    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = Field(default=None, max_length=NAME_LENGTH)
    schema_name: str | None = Field(default=None, max_length=NAME_LENGTH)  # SQLite: main; None: the session's own
    table: str | None = Field(default=None, min_length=1, max_length=NAME_LENGTH)  # one source only: its new name
    mode: TransferMode
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)  # a new table's column types (create)
    identity: dict[str, dict[str, IdentityChoice]] = Field(default_factory=dict)  # append: keep or let it generate
    left_out: dict[str, list[str]] = Field(default_factory=dict)  # source columns not copied

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        for table in (self.overrides, self.identity, self.left_out):
            if len(table) > MAX_TRANSFER_TABLES:
                raise ValueError(f"at most {MAX_TRANSFER_TABLES} tables")
        if any(len(text) > TYPE_TEXT_LENGTH for columns in self.overrides.values() for text in columns.values()):
            raise ValueError(f"a type is at most {TYPE_TEXT_LENGTH} characters")
        return self


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceSpec
    target: TargetSpec

    @model_validator(mode="after")
    def _a_name_for_one_table(self) -> Self:
        if self.target.table is not None and (self.source.tables is not None and len(self.source.tables) != 1):
            raise ValueError("a new name is for a copy of one table")
        return self


ProblemCode = Literal[
    "target_exists", "target_missing", "target_not_writable", "unmappable_type", "invalid_type", "same_table",
    "same_sqlite_file", "source_missing", "missing_column", "unconvertible", "query_not_select", "no_columns",
    "duplicate_column", "invalid_filter",
]


class TransferProblem(BaseModel):
    """Why the copy cannot run as asked (spec §4): the preview has no plan id while there is one."""

    code: ProblemCode
    message: str
    column: str | None = None  # the source column it is about


class ColumnMapping(BaseModel):
    """One source column and where it goes (spec §3.5.1)."""

    source_name: str
    source_type: str
    family: str  # TypeFamily
    target_name: str | None = None  # None: not copied (`skipped` says why)
    target_type: str | None = None
    type_editable: bool = False  # create: the new column's type may be changed
    overridden: bool = False  # the type is the one asked for
    conversion: str | None = None  # how its values change on the way (⚠), or what the preview should say of it
    skipped: str | None = None  # why it is not copied
    identity: IdentityChoice | None = None  # append into an identity column: keep the values, or let it generate them
    problem: str | None = None


class TableTransfer(BaseModel):
    """One table of the copy, in the order the copy runs (spec §3.5.1)."""

    source: str  # as messages name it: schema.name, or the query's title
    target: str  # schema.name on the target
    source_key: str  # what `overrides`, `identity` and `left_out` key it by
    source_path: list[PathSegment] | None = None  # the request's tree path of it (None: a query)
    mode: TransferMode
    columns: list[ColumnMapping]
    before: list[str] = []  # in its transaction (MySQL's CREATE TABLE before it): CREATE TABLE, DELETE, IDENTITY_INSERT
    after: list[str] = []  # before its COMMIT: PostgreSQL's setval, SQL Server's IDENTITY_INSERT OFF
    warnings: list[str] = []
    problems: list[TransferProblem] = []


class TransferPlan(BaseModel):
    """The preview (POST /data/transfer/plan). `plan_id` is null while any table or the copy has a problem (⑤ E8)."""

    plan_id: str | None = None
    source_profile_id: UUID
    tables: list[TableTransfer]
    warnings: list[str] = []
    problems: list[TransferProblem] = []
```

```python
"""A previewed copy as its job runs it (spec ⑥ D-06-8): what the preview showed — each table's SELECT, the target's
statements and which source column goes where — kept once, for 15 minutes, in a one-time plan store. The copy job takes
it by id on the target's session; nothing it sends is chosen after the preview, except its INSERTs' literals, which
follow the target session's own settings (§3.5.5 step 1)."""

from dataclasses import dataclass
from uuid import UUID

from ..adapters.columns import TableColumn
from ..domain.edits import MAX_PLAN_CHARS
from ..domain.metadata import PathSegment
from .plans import OneTimePlanStore

PLAN_TOO_LARGE = (f"The copy's statements are too large to keep for it: at most {MAX_PLAN_CHARS // 1_048_576} MiB of "
                  "text. Copy fewer tables at once")


@dataclass(frozen=True)
class PlannedColumn:
    """A column the copy writes: the source column (its place in the SELECT's columns) and the target's by name."""

    source: int
    target: str


@dataclass(frozen=True)
class PlannedTable:
    source_name: str  # as messages name it: schema.name, or the query's title
    target_name: str  # schema.name on the target
    target_schema: str | None
    target_table: str
    target_path: tuple[PathSegment, ...]  # the target relation's tree path (its columns are read again after CREATE)
    select: str  # what the source runs (a query: the tab's statement, as it is)
    columns: tuple[TableColumn, ...]  # what `select` reads, in its order
    written: tuple[PlannedColumn, ...]
    before: tuple[str, ...]  # inside the table's transaction: CREATE TABLE (but MySQL's), DELETE, IDENTITY_INSERT ON
    after: tuple[str, ...]  # before its COMMIT: setval, IDENTITY_INSERT OFF
    created: bool  # the copy creates the table
    create_first: str | None = None  # MySQL: its CREATE TABLE, sent before the transaction (it commits at once)
    transactional: bool = True  # False: a MySQL table that cannot roll back (MyISAM, ⑤ G10)
    filtered: bool = False  # the SELECT has the table-data tab's filter: it is checked to be a plain read first

    @property
    def statements(self) -> tuple[str, ...]:
        """The statements the copy adds around the rows, in order (the History record's)."""
        return ((self.create_first,) if self.create_first else ()) + self.before + self.after


@dataclass(frozen=True)
class StoredTransfer:
    plan_id: str
    profile_id: UUID  # the target's: the plan store hands it to that profile's session only
    database: str | None  # the target's database where the engine connects per database (PostgreSQL)
    source_profile_id: UUID
    source_database: str | None
    query: bool  # the source is a query tab's result: the copy reads it on that tab's session
    tables: tuple[PlannedTable, ...]
    summary: str  # the History record's text
    title: str  # the job list's: `Copy public.orders from Local PG to Dev MySQL`


def stored_chars(plan: StoredTransfer) -> int:
    """What a stored copy holds that grows with it: its SELECTs and statements."""
    return len(plan.summary) + sum(len(table.select) + sum(map(len, table.statements)) for table in plan.tables)


class TransferPlanStore(OneTimePlanStore[StoredTransfer]):
    def __init__(self, **options: object) -> None:
        super().__init__(stored_chars, PLAN_TOO_LARGE, what="copy plan", **options)  # type: ignore[arg-type]
```

- [ ] **Step 5: The copy's statements.** Create `backend/dbaguy_backend/sql/transfer_sql.py`:

```python
"""The SQL a copy sends besides its rows (spec ⑥ §3.5.4–3.5.5): a new table, the DELETE of the rows it replaces, an
identity's switch and its sequence after, its sessions' settings, and the History record's text. Quoting and literals
come from `dialects` only. Pure."""

from collections.abc import Sequence

from .dialects import Dialect, identity_insert, qualified, quote_ident, quote_literal, script_text
from .dump_script import RESET_SETTINGS, RESTORE_SETTINGS

# The copy's own target session, before its first table: what a restore of a dump runs under (DRY, D-06-7) — PostgreSQL
# standard strings, MySQL's strict sql_mode that keeps zero dates and AUTO_INCREMENT 0 with time_zone +00:00 and its
# foreign key checks off, SQLite's foreign keys off (each table commits on its own, so deferring them to a COMMIT would
# not help a later table, 06c) — and SQL Server's implicit transactions off (06a L8: a login's user options)
TARGET_SETUP: dict[Dialect, tuple[str, ...]] = {
    **RESTORE_SETTINGS,
    Dialect.TSQL: ("SET IMPLICIT_TRANSACTIONS OFF", *RESTORE_SETTINGS[Dialect.TSQL]),
}
TARGET_RESET = RESET_SETTINGS  # after its last table (the session is never used again; a courtesy)
# A source's own session settings for the copy's SELECT, read before and set back after (a query tab's session is the
# user's): PostgreSQL's text of dates and intervals as every engine reads them; MySQL's TIMESTAMP in UTC, as the target
# writes it, and a reader that may wait on a slow target longer than net_write_timeout (60 s by default) without the
# server cutting the read (A11: probed, 2013 after 66,001 of 1,000,000 rows with 2 s and an 8 s wait; all of them with
# 3,600 s)
SOURCE_SETTINGS: dict[Dialect, tuple[tuple[str, str], ...]] = {
    Dialect.POSTGRES: (("DateStyle", "ISO, MDY"), ("IntervalStyle", "postgres")),
    Dialect.MYSQL: (("net_write_timeout", "3600"), ("time_zone", "+00:00")),
}


def read_settings(dialect: Dialect) -> str | None:
    """The SELECT of a source session's settings `SOURCE_SETTINGS` names, in their order; None: it has none."""
    names = [name for name, _ in SOURCE_SETTINGS.get(dialect, ())]
    if not names:
        return None
    if dialect is Dialect.POSTGRES:
        return "SELECT " + ", ".join(f"current_setting({quote_literal(dialect, name)})" for name in names)
    return "SELECT " + ", ".join(f"@@SESSION.{name}" for name in names)


def set_setting(dialect: Dialect, name: str, value: str) -> str:
    """`SET` one setting `SOURCE_SETTINGS` names to `value` (a number goes bare in MySQL)."""
    if dialect is Dialect.POSTGRES:
        return f"SET {name} = {quote_literal(dialect, value)}"
    shown = value if value.isdigit() else quote_literal(dialect, value)
    return f"SET SESSION {name} = {shown}"


def create_table(
    dialect: Dialect, schema: str | None, name: str, columns: Sequence[tuple[str, str, bool]], key: Sequence[str],
) -> str:
    """A copy's new table (spec §3.5.4): its columns (name, type, nullable) with NOT NULL, and the source's primary key
    when every column of it is copied. Nothing else (defaults, checks, indexes, foreign keys and identities are each
    engine's own: not translated, R-06-9)."""
    lines = [f"  {quote_ident(dialect, column)} {type_sql}".rstrip() + ("" if nullable else " NOT NULL")
             for column, type_sql, nullable in columns]  # SQLite's untyped column: no type at all
    if key:
        lines.append(f"  PRIMARY KEY ({', '.join(quote_ident(dialect, column) for column in key)})")
    return f"CREATE TABLE {qualified(dialect, schema, name)} (\n" + ",\n".join(lines) + "\n)"


def delete_rows(dialect: Dialect, schema: str | None, name: str) -> str:
    """Replace rows (spec §3.5.4): every row goes, in the copy's transaction (DELETE_WITHOUT_WHERE, said in danger)."""
    return f"DELETE FROM {qualified(dialect, schema, name)}"


def identity_switch(dialect: Dialect, schema: str | None, name: str) -> tuple[str, str] | None:
    """SQL Server's IDENTITY_INSERT on and off around a table whose identity values are kept (one table at a time)."""
    return identity_insert(dialect, qualified(dialect, schema, name))


def advance_sequence(schema: str | None, name: str, column: str) -> str:
    """PostgreSQL, after writing a column's own values: its sequence (an identity's or a serial's) moves past the
    largest value, never back (writing them does not move it, A P2). A column that owns no sequence changes nothing;
    neither does an empty table."""
    table = quote_literal(Dialect.POSTGRES, qualified(Dialect.POSTGRES, schema, name))
    quoted = quote_ident(Dialect.POSTGRES, column)
    return ("SELECT setval(s.seq, GREATEST(s.top, COALESCE(pg_sequence_last_value(s.seq), s.top)))\n"
            f"FROM (SELECT pg_get_serial_sequence({table}, {quote_literal(Dialect.POSTGRES, column)})::regclass AS seq,\n"
            f"             (SELECT max({quoted}) FROM {qualified(Dialect.POSTGRES, schema, name)}) AS top) s\n"
            "WHERE s.seq IS NOT NULL AND s.top IS NOT NULL")


def summary(source: str, target: str, tables: Sequence[tuple[str, str, str]], dialect: Dialect,
            statements: Sequence[str]) -> str:
    """The History record's text (D-06-9): what goes where, then the statements the copy adds (DDL, DELETE), never its
    INSERTs: `-- Copy data: Local PG public.orders → Dev MySQL orders (create)`."""
    if len(tables) == 1:
        source_name, target_name, mode = tables[0]
        head = f"-- Copy data: {source} {source_name} → {target} {target_name} ({mode})"
    else:
        head = f"-- Copy data: {source} → {target}, {len(tables):,} tables"
        head += "".join(f"\n--   {source_name} → {target_name} ({mode})" for source_name, target_name, mode in tables)
    return head + ("\n" + script_text(dialect, statements) if statements else "")
```

- [ ] **Step 6: The capability.** `copy_data` on the four relational engines:

```diff
diff --git a/backend/dbaguy_backend/adapters/base.py b/backend/dbaguy_backend/adapters/base.py
index a7750f2..ff72e7e 100644
--- a/backend/dbaguy_backend/adapters/base.py
+++ b/backend/dbaguy_backend/adapters/base.py
@@ -51,6 +51,7 @@ class AdapterCapabilities:
     orders_browse_by_key: bool = False
     run_file: bool = False  # Run SQL file (spec ⑥ R-06-1, D-06-13)
     dump: bool = False  # a native SQL dump (spec ⑥ R-06-1): the connection is Dumping
+    copy_data: bool = False  # copy rows between connections (spec ⑥ R-06-1, G3): the connection is DataTools
 
 
 @dataclass(frozen=True)
@@ -417,7 +418,7 @@ class DatabaseAdapter(ABC):
             transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
             documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
             row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key, run_file=caps.run_file,
-            dump=caps.dump,
+            dump=caps.dump, copy_data=caps.copy_data,
         )
 
     def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
```

```diff
diff --git a/backend/dbaguy_backend/domain/profiles.py b/backend/dbaguy_backend/domain/profiles.py
index 0168775..85fc797 100644
--- a/backend/dbaguy_backend/domain/profiles.py
+++ b/backend/dbaguy_backend/domain/profiles.py
@@ -149,3 +149,4 @@ class EngineInfo(BaseModel):
     orders_browse_by_key: bool
     run_file: bool = False  # Run SQL file (spec ⑥ D-06-13)
     dump: bool = False  # a native SQL dump (spec ⑥ D-06-13)
+    copy_data: bool = False  # copy rows between connections (spec ⑥ D-06-13; 06c)
```

```diff
diff --git a/backend/dbaguy_backend/adapters/postgres/adapter.py b/backend/dbaguy_backend/adapters/postgres/adapter.py
index d93b767..6f8a4c2 100644
--- a/backend/dbaguy_backend/adapters/postgres/adapter.py
+++ b/backend/dbaguy_backend/adapters/postgres/adapter.py
@@ -616,7 +616,7 @@ class PostgresAdapter(DatabaseAdapter):
     dialect = Dialect.POSTGRES
     capabilities = AdapterCapabilities(
         per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
-        run_file=True, dump=True,
+        run_file=True, dump=True, copy_data=True,
     )
 
     def parse_url(self, url: str) -> ParsedUrl:
```

```diff
diff --git a/backend/dbaguy_backend/adapters/mysql/adapter.py b/backend/dbaguy_backend/adapters/mysql/adapter.py
index 10d915a..ce6dbcc 100644
--- a/backend/dbaguy_backend/adapters/mysql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mysql/adapter.py
@@ -559,7 +559,7 @@ class MysqlAdapter(DatabaseAdapter):
     default_port = 3306
     dialect = Dialect.MYSQL
     # one connection sees every database; KILL QUERY keeps the transaction
-    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True)
+    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True, copy_data=True)
 
     def parse_url(self, url: str) -> ParsedUrl:
         return parse_mysql_url(url)
```

```diff
diff --git a/backend/dbaguy_backend/adapters/mssql/adapter.py b/backend/dbaguy_backend/adapters/mssql/adapter.py
index b13a49e..34af35d 100644
--- a/backend/dbaguy_backend/adapters/mssql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mssql/adapter.py
@@ -761,7 +761,7 @@ class MssqlAdapter(DatabaseAdapter):
     # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
     capabilities = AdapterCapabilities(
         per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
-        run_file=True, dump=True,
+        run_file=True, dump=True, copy_data=True,
     )
 
     def parse_url(self, url: str) -> ParsedUrl:
```

```diff
diff --git a/backend/dbaguy_backend/adapters/sqlite/adapter.py b/backend/dbaguy_backend/adapters/sqlite/adapter.py
index 70ff88d..3d9849a 100644
--- a/backend/dbaguy_backend/adapters/sqlite/adapter.py
+++ b/backend/dbaguy_backend/adapters/sqlite/adapter.py
@@ -309,7 +309,7 @@ class SqliteAdapter(DatabaseAdapter):
     url_schemes = ("sqlite",)
     default_port = None
     dialect = Dialect.SQLITE
-    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True)
+    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True, copy_data=True)
 
     def parse_url(self, url: str) -> ParsedUrl:
         """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
```

- [ ] **Step 7: Whether a table can be written at all.** Modify `backend/dbaguy_backend/adapters/editing.py`:

```diff
diff --git a/backend/dbaguy_backend/adapters/editing.py b/backend/dbaguy_backend/adapters/editing.py
index fa2c74e..4c531a2 100644
--- a/backend/dbaguy_backend/adapters/editing.py
+++ b/backend/dbaguy_backend/adapters/editing.py
@@ -25,6 +25,13 @@ def read_only_relation(path: Sequence[PathSegment], reason: str) -> EditTarget:
     return EditTarget(path=list(path), editable=False, reason=reason)
 
 
+def not_writable_reason(target: EditTarget) -> str | None:
+    """Why no row can be written into the relation at all (⑥ 06c: a copy's existing target), or None: what
+    `read_only_relation` said (a view, a foreign or shadow table, a history or ledger table, PostgreSQL without
+    standard strings). A table that has no usable key is editable by no grid, yet a copy appends to it."""
+    return None if target.editable or target.columns else target.reason
+
+
 def build_target(
     path: Sequence[PathSegment], columns: Sequence[EditColumn], candidates: Sequence[KeyCandidate], *,
     transactional: bool = True, backslash_escapes: bool = True,
```

- [ ] **Step 8: The service.** Create `backend/dbaguy_backend/services/transfer_service.py`:

```python
"""Copy data's preview (spec ⑥ §3.5.1, POST /data/transfer/plan): which source columns go to which target columns,
with which types and conversions, the statements the copy adds (CREATE TABLE, DELETE, IDENTITY_INSERT, setval), its
warnings and problems; when nothing is wrong, the plan the copy job takes once (D-06-8: the copy runs what was
previewed). Reads the source and the target on their metadata connections (the relations' columns, the target's
edit target, the source's keys from the schema snapshot); nothing is written and no query is run.

Logs: INFO the two profiles, the table count, the mode and the problem count; never a value, a filter or a query's
text."""

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, DataTools, RowEditing
from ..adapters.columns import TableColumn
from ..adapters.dump import DumpTable, parents_first
from ..adapters.editing import not_writable_reason
from ..adapters.families import of_name
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import cannot
from ..domain.edits import EditTarget, edit_kind
from ..domain.errors import NotFound, NotSupported, ReadOnlyProfile, ValidationFailed
from ..domain.execution import ColumnMeta
from ..domain.metadata import PathSegment
from ..domain.profiles import AnyProfile, SqliteProfile
from ..domain.schema import SchemaSnapshot, SnapshotRelation
from ..domain.transfer import (
    COPY_DATA, ColumnMapping, IdentityChoice, TableTransfer, TargetSpec, TransferPlan, TransferProblem,
    TransferRequest,
)
from ..sql.dialects import Dialect, qualified
from ..sql.dump_script import RESTORE_CHECKS_FOREIGN_KEYS
from ..sql.select_builder import FILTER_NOT_A_CONDITION, build_copy_select, check_filter, relation_from_path
from ..sql.transfer_sql import advance_sequence, create_table, delete_rows, identity_switch, summary
from ..sql.type_map import conversion, engine_name, incompatible, planned_column, target_type, type_problem
from .metadata_service import MetadataService
from .profile_service import ProfileService
from .safety import is_plain_read
from .schema_cache import SchemaCache
from .transfer_plans import PlannedColumn, PlannedTable, StoredTransfer, TransferPlanStore

DEFAULT_SCHEMAS: dict[Dialect, str] = {Dialect.POSTGRES: "public", Dialect.TSQL: "dbo", Dialect.SQLITE: "main"}
QUERY_RUNS_AGAIN = "The query runs again when the copy starts, as an export does: its rows may differ from those shown"
FK_CYCLE = ("Foreign keys between these tables form a cycle: {} checks them as rows load, so a table may fail to "
            "copy")
CREATED_STAYS = ("MySQL commits a CREATE TABLE at once: if the copy of {} fails, the new table stays, empty")
NON_TRANSACTIONAL = ("{} cannot roll back (non-transactional): if the copy fails, the rows written before stay, and "
                     "what happened to the table is not known")
NOT_UNICODE = ("{} is not Unicode ({}): characters outside its code page become ?")
LEFT_OUT = "left out"
NOT_IN_TARGET = "no such column in the target"
COMPUTED = "the target computes it"
SKIPPED_SOURCE = "not copied: {}"  # the source's own reason (a generated column, rowversion, sql_variant, …)


def _problem(code: str, message: str, column: str | None = None) -> TransferProblem:
    return TransferProblem(code=code, message=message, column=column)  # type: ignore[arg-type]


class _Source:
    """One source relation (or the query) as the plan reads it."""

    def __init__(self, key: str, name: str, schema: str | None, table: str, path: list[PathSegment] | None,
                 columns: list[TableColumn] | None, filter: str | None = None) -> None:
        self.key = key  # what the request's overrides, identity and left_out use
        self.name = name  # as messages say it
        self.schema = schema
        self.table = table
        self.path = path
        self.columns = columns  # None: it is gone
        self.filter = filter


def query_columns(dialect: Dialect, metas: Sequence[ColumnMeta]) -> list[TableColumn]:
    """A query result's columns as a copy reads them: their family from the type's name (or, failing that, from the
    grid's category, spec §3.5.2's last sentence); nullable, nothing else known."""
    found = []
    for meta in metas:
        named = of_name(dialect, meta.type_name, meta.category)
        found.append(TableColumn(
            name=meta.name, type_name=meta.type_name, family=named.family, nullable=True, has_default=False,
            kind=edit_kind(meta.category), compare="none", writable=True, precision=named.precision,
            scale=named.scale, length=named.length,
        ))
    return found


def _same_file(first: AnyProfile, second: AnyProfile) -> bool:
    if not (isinstance(first, SqliteProfile) and isinstance(second, SqliteProfile)):
        return False
    one, other = Path(first.path).expanduser(), Path(second.path).expanduser()
    try:
        return one.resolve() == other.resolve() or (one.exists() and other.exists() and one.samefile(other))
    except OSError:
        return False


def _find(snapshot: SchemaSnapshot | None, schema: str | None, name: str) -> SnapshotRelation | None:
    if snapshot is None:
        return None
    for each in snapshot.schemas:
        if schema is None or each.name == schema:
            for relation in each.relations:
                if relation.name == name:
                    return relation
    return None


def _match(name: str, target: dict[str, TableColumn]) -> TableColumn | None:
    """The target column of that name: the same name first, else the one that differs only in case."""
    if name in target:
        return target[name]
    folded = [column for key, column in target.items() if key.casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


class TransferService:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService,
                 schema_cache: SchemaCache, plans: TransferPlanStore | None = None) -> None:
        self._profiles = profiles
        self._registry = registry
        self._metadata = metadata
        self._schema_cache = schema_cache
        self.plans = plans if plans is not None else TransferPlanStore()

    def plan(self, request: TransferRequest) -> TransferPlan:
        source, target = request.source, request.target
        source_profile, source_adapter = self._admit(source.profile_id)
        target_profile, target_adapter = self._admit(target.profile_id)
        if target_profile.read_only:
            raise ReadOnlyProfile()
        source_dialect, target_dialect = source_adapter.dialect, target_adapter.dialect
        schema = target.schema_name or DEFAULT_SCHEMAS.get(target_dialect)
        if schema is None:
            raise ValidationFailed("Choose the schema (MySQL: the database) to copy into")
        problems: list[TransferProblem] = []
        warnings: list[str] = []
        sources = self._sources(request, source_dialect, problems, warnings)
        snapshot = self._snapshot(source.profile_id, source.database) if source.tables else None
        ordered, cycle = self._order(sources, snapshot)
        if cycle and target.mode != "create" and target_dialect in RESTORE_CHECKS_FOREIGN_KEYS:
            warnings.append(FK_CYCLE.format(engine_name(target_dialect)))
        if _same_file(source_profile, target_profile):
            problems.append(_problem("same_sqlite_file", "The source and the target are the same SQLite file: use "
                                                         "INSERT … SELECT in a query tab"))
        names = [target.table or item.table for item in ordered]
        existing = self._targets(target, target_adapter, schema, names)
        tables: list[TableTransfer] = []
        planned: list[PlannedTable] = []
        for item, name in zip(ordered, names, strict=True):
            view, made = self._table(request, item, name, schema, existing.get(name), source_dialect,
                                     target_dialect, target_adapter, source_profile, target_profile, snapshot)
            tables.append(view)
            if made is not None:
                planned.append(made)
        blocked = bool(problems) or any(table.problems for table in tables) or not tables
        plan_id = None if blocked else self._store(request, planned, source_profile, target_profile, target_adapter)
        logger.info("Copy plan from {} to {}: {} table(s), {}, {} problem(s)", source_profile.name, target_profile.name,
                    len(tables), target.mode, len(problems) + sum(len(table.problems) for table in tables))
        return TransferPlan(plan_id=plan_id, source_profile_id=source_profile.id, tables=tables, warnings=warnings,
                            problems=problems)

    def _admit(self, profile_id: UUID) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not adapter.capabilities.copy_data:
            raise NotSupported(cannot(profile.engine, COPY_DATA))
        return profile, adapter

    def _sources(self, request: TransferRequest, dialect: Dialect, problems: list[TransferProblem],
                 warnings: list[str]) -> list[_Source]:
        source = request.source
        if source.query is not None:
            query = source.query
            if not is_plain_read(query.sql, dialect):
                problems.append(_problem("query_not_select", "Only a statement that returns rows (SELECT, WITH, "
                                                             "VALUES…) can be copied, and one that only reads"))
            names = [meta.name for meta in query.columns]
            if len(set(names)) != len(names):
                twice = next(name for name in names if names.count(name) > 1)
                problems.append(_problem("duplicate_column", f"The result has two columns named {twice}: give them "
                                                             "names of their own (AS) and run it again", twice))
            warnings.append(QUERY_RUNS_AGAIN)
            return [_Source(query.title, query.title, None, query.title, None, query_columns(dialect, query.columns))]
        paths = source.tables or []

        def read(connection: DatabaseConnection) -> list[_Source]:
            if not isinstance(connection, DataTools):
                raise NotSupported(cannot(connection.server_info().engine, COPY_DATA))
            found = []
            for path in paths:
                schema, name = relation_from_path(path)
                shown = qualified(dialect, schema, name) if schema else name
                try:
                    columns: list[TableColumn] | None = connection.table_columns(path)
                except NotFound:
                    columns = None
                found.append(_Source(shown, shown, schema, name, path, columns, source.filter))
            return found

        return self._metadata.with_connection(source.profile_id, source.database, read)

    def _snapshot(self, profile_id: UUID, database: str | None) -> SchemaSnapshot | None:
        """The source's keys (a new table's primary key) and foreign keys (the order): from the schema snapshot,
        built or cached for completion. Without it the copy runs anyway, in the order chosen and without keys."""
        try:
            return self._schema_cache.get(profile_id, database)
        except Exception as exc:  # above DEBUG only its code: a server's text may echo what it read (D-06-12)
            logger.warning("The source's keys could not be read for a copy plan ({})", getattr(exc, "code",
                                                                                              type(exc).__name__))
            logger.opt(exception=exc).debug("The source's keys could not be read")
            return None

    @staticmethod
    def _order(sources: list[_Source], snapshot: SchemaSnapshot | None) -> tuple[list[_Source], bool]:
        """Parents first, by the source's foreign keys among these tables (R-06-9), else as chosen; and whether they
        form a cycle."""
        if len(sources) < 2 or snapshot is None:
            return sources, False
        key = "{}\x00{}".format  # schema and name: a name with a dot in it stays one name
        by_key = {key(item.schema, item.table): item for item in sources}
        tables = []
        for item in sources:
            relation = _find(snapshot, item.schema, item.table)
            refs = tuple(ref for ref in (key(fk.ref_schema if item.schema else None, fk.ref_table)
                                         for fk in (relation.foreign_keys if relation else [])) if ref in by_key)
            tables.append(DumpTable(schema=item.schema, table=item.table, name=key(item.schema, item.table),
                                    columns=(), references=refs))
        ordered, cycle = parents_first(tuple(tables))
        return [by_key[table.name] for table in ordered], cycle

    def _targets(self, target: TargetSpec, adapter: DatabaseAdapter, schema: str,
                 names: list[str]) -> dict[str, tuple[list[TableColumn], EditTarget] | None]:
        """Each target table's columns and what grid editing says of writing it (a view, a table that cannot be
        written as a whole), or None when there is no such table."""

        def read(connection: DatabaseConnection) -> dict[str, tuple[list[TableColumn], EditTarget] | None]:
            found: dict[str, tuple[list[TableColumn], EditTarget] | None] = {}
            for name in dict.fromkeys(names):
                path = adapter.relation_path(target.database, schema, name, "table")
                try:
                    columns = connection.table_columns(path) if isinstance(connection, DataTools) else []
                except NotFound:
                    found[name] = None
                    continue
                edit = connection.edit_target(path) if isinstance(connection, RowEditing) else None
                found[name] = (columns, edit) if edit is not None else (columns, EditTarget(path=path, editable=True))
            return found

        return self._metadata.with_connection(target.profile_id, target.database, read)

    def _table(
        self, request: TransferRequest, item: _Source, name: str, schema: str,
        existing: tuple[list[TableColumn], EditTarget] | None, source_dialect: Dialect, target_dialect: Dialect,
        adapter: DatabaseAdapter, source_profile: AnyProfile, target_profile: AnyProfile,
        snapshot: SchemaSnapshot | None,
    ) -> tuple[TableTransfer, PlannedTable | None]:
        target = request.target
        shown = qualified(target_dialect, schema, name) if target_dialect is not Dialect.MYSQL else name
        view = TableTransfer(source=item.name, target=shown, source_key=item.key, source_path=item.path,
                             mode=target.mode, columns=[])
        if item.columns is None:
            view.problems.append(_problem("source_missing", f"{item.name} is not there any more"))
            return view, None
        same_database = (source_profile.id == target_profile.id
                         and (not adapter.capabilities.per_database_connection
                              or request.source.database == target.database))
        if same_database and item.path is not None and (item.schema or DEFAULT_SCHEMAS.get(source_dialect)) == schema \
                and item.table == name:
            view.problems.append(_problem("same_table", "The source and the target are the same table"))
            return view, None
        if target.mode == "create" and existing is not None:
            view.problems.append(_problem("target_exists",
                                          f"{shown} already exists: append to it, or choose another name"))
            return view, None
        if target.mode != "create" and existing is None:
            view.problems.append(_problem("target_missing",
                                          f"{shown} does not exist: create it, or choose another table"))
            return view, None
        if existing is not None and (refused := not_writable_reason(existing[1])) is not None:
            view.problems.append(_problem("target_not_writable", f"{shown} cannot be written: {refused}"))
            return view, None
        read = [column for column in item.columns if column.copied]
        if item.path is not None:
            for column in item.columns:
                if not column.copied:
                    view.columns.append(ColumnMapping(
                        source_name=column.name, source_type=column.type_name, family=column.family,
                        skipped=SKIPPED_SOURCE.format(column.not_copied_reason or "the server computes it")))
        left_out = set(target.left_out.get(item.key, []))
        if target.mode == "create":
            written, before, after, create_first = self._create(view, item, read, left_out, schema, name,
                                                                source_dialect, target_dialect, request, snapshot)
            transactional = True
        else:
            assert existing is not None
            written, before, after = self._append(view, item, read, left_out, schema, name, existing[0],
                                                  source_dialect, target_dialect, request)
            create_first = None
            transactional = existing[1].transactional
            if not transactional:
                view.warnings.append(NON_TRANSACTIONAL.format(shown))
        if not written and not view.problems:
            view.problems.append(_problem("no_columns", f"No column of {item.name} would be copied"))
        view.before, view.after = ([create_first] if create_first else []) + before, after
        select = self._select(view, item, read, source_dialect, request)
        if view.problems or select is None:
            return view, None
        path = tuple(adapter.relation_path(target.database, schema, name, "table"))
        return view, PlannedTable(
            source_name=item.name, target_name=shown, target_schema=schema, target_table=name, target_path=path,
            select=select, columns=tuple(read), written=tuple(written), before=tuple(before), after=tuple(after),
            created=target.mode == "create", create_first=create_first, transactional=transactional,
            filtered=item.filter is not None,
        )

    def _select(self, view: TableTransfer, item: _Source, read: list[TableColumn], dialect: Dialect,
                request: TransferRequest) -> str | None:
        """What the source runs: the tab's statement as it is, or the table's SELECT with its filter (one condition
        that only reads, as a dump's)."""
        if request.source.query is not None:
            return request.source.query.sql
        if not read:
            return None
        try:
            check_filter(dialect, item.filter)
            sql = build_copy_select(dialect, item.schema, item.table, read, item.filter)
        except ValidationFailed as exc:
            view.problems.append(_problem("invalid_filter", exc.message))
            return None
        if item.filter is not None and not is_plain_read(sql, dialect):
            view.problems.append(_problem("invalid_filter", FILTER_NOT_A_CONDITION))
            return None
        return sql

    def _create(
        self, view: TableTransfer, item: _Source, read: list[TableColumn], left_out: set[str], schema: str,
        name: str, source_dialect: Dialect, target_dialect: Dialect, request: TransferRequest,
        snapshot: SchemaSnapshot | None,
    ) -> tuple[list[PlannedColumn], list[str], list[str], str | None]:
        overrides = request.target.overrides.get(item.key, {})
        columns: list[tuple[str, str, bool]] = []
        written: list[PlannedColumn] = []
        for index, column in enumerate(read):
            mapping = ColumnMapping(source_name=column.name, source_type=column.type_name, family=column.family,
                                    type_editable=True)
            if column.name in left_out:
                view.columns.append(mapping.model_copy(update={"skipped": LEFT_OUT}))
                continue
            typed = overrides.get(column.name)
            if typed is not None:
                if (why := type_problem(target_dialect, typed)) is not None:
                    message = f"{column.name}: Not a single type: {why}"
                    view.problems.append(_problem("invalid_type", message, column.name))
                    view.columns.append(mapping.model_copy(update={"target_name": column.name, "target_type": typed,
                                                                   "overridden": True, "problem": message}))
                    continue
                type_sql, note = typed.strip(), None
                into = planned_column(column.name, type_sql, target_dialect)
            else:
                mapped = target_type(column, source_dialect, target_dialect)
                if mapped.type_sql is None:
                    message = mapped.problem or f"{column.name} has no type"
                    view.problems.append(_problem("unmappable_type", message, column.name))
                    view.columns.append(mapping.model_copy(update={"target_name": column.name, "problem": message}))
                    continue
                type_sql, note = mapped.type_sql, mapped.note
                into = planned_column(column.name, type_sql, target_dialect)
            notes = [note] if note else []
            notes += [said for said in conversion(column, into, source_dialect, target_dialect).notes
                      if said not in notes]
            if (why := incompatible(column, into)) is not None:
                view.problems.append(_problem("unconvertible", why, column.name))
            view.columns.append(mapping.model_copy(update={
                "target_name": column.name, "target_type": type_sql, "overridden": typed is not None,
                "conversion": "; ".join(notes) or None}))
            columns.append((column.name, type_sql, column.nullable))
            written.append(PlannedColumn(index, column.name))
        relation = _find(snapshot, item.schema, item.table) if item.path else None
        copied = {column for column, _, _ in columns}
        key = list(relation.primary_key) if relation and relation.primary_key and set(relation.primary_key) <= copied \
            else []
        ddl = create_table(target_dialect, schema, name, columns, key) if columns else ""
        if target_dialect is Dialect.MYSQL:
            view.warnings.append(CREATED_STAYS.format(name))
            return written, [], [], ddl or None
        return written, [ddl] if ddl else [], [], None

    def _append(
        self, view: TableTransfer, item: _Source, read: list[TableColumn], left_out: set[str], schema: str,
        name: str, target_columns: list[TableColumn], source_dialect: Dialect, target_dialect: Dialect,
        request: TransferRequest,
    ) -> tuple[list[PlannedColumn], list[str], list[str]]:
        choices: dict[str, IdentityChoice] = request.target.identity.get(item.key, {})
        by_name = {column.name: column for column in target_columns}
        used: set[str] = set()
        written: list[PlannedColumn] = []
        kept_identity: list[TableColumn] = []
        for index, column in enumerate(read):
            mapping = ColumnMapping(source_name=column.name, source_type=column.type_name, family=column.family)
            into = _match(column.name, by_name)
            if column.name in left_out:
                view.columns.append(mapping.model_copy(update={"skipped": LEFT_OUT}))
                continue
            if into is None:
                view.columns.append(mapping.model_copy(update={"skipped": NOT_IN_TARGET}))
                view.warnings.append(f"{column.name} is not copied: {name} has no such column")
                continue
            used.add(into.name)
            update: dict = {"target_name": into.name, "target_type": into.type_name}
            if into.generated or into.family == "rowversion" or not into.copied:
                view.columns.append(mapping.model_copy(update={**update, "skipped": COMPUTED}))
                continue
            if into.identity is not None:
                choice = choices.get(column.name, "keep")
                update["identity"] = choice
                if choice == "generate":
                    view.columns.append(mapping.model_copy(update={**update, "skipped": "the target generates it"}))
                    continue
                kept_identity.append(into)
            if (why := incompatible(column, into)) is not None:
                view.problems.append(_problem("unconvertible", why, column.name))
                update["problem"] = why
            notes = list(conversion(column, into, source_dialect, target_dialect).notes)
            if target_dialect is Dialect.TSQL and not into.unicode and column.family in ("char", "varchar", "text"):
                notes.append(NOT_UNICODE.format(into.name, into.type_name))
            update["conversion"] = "; ".join(notes) or None
            view.columns.append(mapping.model_copy(update=update))
            written.append(PlannedColumn(index, into.name))
        for column in target_columns:
            if column.name not in used and not column.nullable and not column.has_default and not column.generated:
                view.problems.append(_problem("missing_column", f"{name}.{column.name} needs a value (NOT NULL, no "
                                                                f"default) and no column of {item.name} goes into it"))
        before = [delete_rows(target_dialect, schema, name)] if request.target.mode == "replace_rows" else []
        after: list[str] = []
        if kept_identity and (switch := identity_switch(target_dialect, schema, name)) is not None:
            before.append(switch[0])
            after.append(switch[1])
        if target_dialect is Dialect.POSTGRES:
            numbers = ("int16", "int32", "int64", "decimal")
            after += [advance_sequence(schema, name, column.name) for column in target_columns
                      if column.name in {planned.target for planned in written} and column.family in numbers
                      and (column.identity is not None or column.has_default)]
        return written, before, after

    def _store(self, request: TransferRequest, planned: list[PlannedTable], source_profile: AnyProfile,
               target_profile: AnyProfile, target_adapter: DatabaseAdapter) -> str:
        source, target = request.source, request.target
        plan_id = uuid4().hex
        per_database = target_adapter.capabilities.per_database_connection
        statements = [statement for table in planned for statement in table.statements]
        text = summary(source_profile.name, target_profile.name,
                       [(table.source_name, table.target_name, target.mode) for table in planned],
                       target_adapter.dialect, statements)
        what = planned[0].source_name if len(planned) == 1 else f"{len(planned):,} tables"
        self.plans.put(StoredTransfer(
            plan_id=plan_id, profile_id=target_profile.id, database=target.database if per_database else None,
            source_profile_id=source_profile.id, source_database=source.database, query=source.query is not None,
            tables=tuple(planned), summary=text, title=f"Copy {what} from {source_profile.name} to {target_profile.name}",
        ))
        return plan_id
```

- [ ] **Step 9: The route and the container.**

```diff
diff --git a/backend/dbaguy_backend/api/data.py b/backend/dbaguy_backend/api/data.py
index 0c91df3..ae29df1 100644
--- a/backend/dbaguy_backend/api/data.py
+++ b/backend/dbaguy_backend/api/data.py
@@ -1,5 +1,6 @@
 """Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
-own (dump, run_file); these routes find it again after the window reloads, and cancel it."""
+own (dump, run_file, copy_data); these routes find it again after the window reloads, and cancel it, and preview a
+dump, a file and a copy."""
 
 from typing import Self
 from uuid import UUID
@@ -13,6 +14,7 @@ from ..domain.data_jobs import (
     FileInspection, JobInfo, filter_fits,
 )
 from ..domain.errors import NotFound
+from ..domain.transfer import TransferPlan, TransferRequest
 from .deps import get_container, require_token
 
 router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])
@@ -73,6 +75,14 @@ def dump_plan(body: DumpPlanRequest, container: AppContainer = Depends(get_conta
                                     drop_first=body.drop_first, filter=body.filter)
 
 
+@router.post("/transfer/plan", response_model=TransferPlan)
+def transfer_plan(body: TransferRequest, container: AppContainer = Depends(get_container)) -> TransferPlan:
+    """Copy data's preview (spec ⑥ §3.5.1): each table's column mapping, types and conversions, the statements the copy
+    adds, its warnings and problems, and the plan id the copy command takes once (null while there is a problem). A
+    read-only target is refused (403) and an engine without copies 422 `not_supported`, before any connection."""
+    return container.transfers.plan(body)
+
+
 @router.post("/reveal", status_code=204)
 def reveal(body: RevealRequest, container: AppContainer = Depends(get_container)) -> Response:
     """Show in Finder, for a file a completed dump of this backend run wrote (404 for any other path)."""
```

```diff
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index d718790..8ee8d77 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -26,6 +26,7 @@ from .services.safety import SafetyPolicy
 from .services.schema_cache import SchemaCache
 from .services.scripts_service import ScriptsService
 from .services.settings_service import SettingsService
+from .services.transfer_service import TransferService
 from .services.workspace_service import WorkspaceService
 from .sessions.hooks import SessionHooksFactory
 from .sessions.jobs.registry import JobRegistry
@@ -85,6 +86,7 @@ class AppContainer:
         self.field_samples = FieldSampleService(self.metadata, self.app_settings)
         self.jobs = JobRegistry()
         self.data = DataService(self.profiles, self.registry, self.metadata, self.jobs)
+        self.transfers = TransferService(self.profiles, self.registry, self.metadata, self.schema_cache)
         self.history = HistoryService(history_file(data_dir))
         self.policy = SafetyPolicy()
         hooks = SessionHooksFactory(
```

- [ ] **Step 10: Run the tests.** `cd backend && .venv/bin/pytest -q tests/test_transfer_sql.py tests/test_transfer_plan.py tests/test_edit_plans.py tests/test_edits_api.py tests/test_container.py tests/test_api.py` → all pass (dry run: 94 passed; 14 new).

- [ ] **Step 11: The frontend's types.** Run `npm run gen:api` (it regenerates `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts`: the route, the transfer models and `EngineInfo.copy_data`). Export the copy's models from `src/lib/api/types.ts`, and give the test fixture's engine its new field:

```diff
diff --git a/src/lib/api/types.ts b/src/lib/api/types.ts
index 60c21fa..c48d8fe 100644
--- a/src/lib/api/types.ts
+++ b/src/lib/api/types.ts
@@ -160,6 +160,22 @@ export type DumpPlanRequest = S["DumpPlanRequest"];
 export type StoppedAt = S["StoppedAt"];
 /** Run SQL file (06b B2): a file's lines around one line, for the error list. */
 export type FileExcerpt = S["FileExcerpt"];
+/** Copy data (spec ⑥ §3.5.1, POST /data/transfer/plan): the source, the target and how. */
+export type TransferRequest = S["TransferRequest"];
+/** Copy data: tables (tree paths) of one connection, or a query tab's result; a filter for one table. */
+export type SourceSpec = S["SourceSpec"];
+/** Copy data: where the rows go, the mode, and per table and column the types, identities and columns left out. */
+export type TargetSpec = S["TargetSpec"];
+/** Copy data: a query tab's statement (run again on its session) and the result's columns the tab already has. */
+export type QuerySource = S["QuerySource"];
+/** Copy data's preview: each table's mapping, statements, warnings and problems; `plan_id` null while any problem. */
+export type TransferPlan = S["TransferPlan"];
+/** Copy data's preview: one table, in the order the copy runs. */
+export type TableTransfer = S["TableTransfer"];
+/** Copy data's preview: one source column and where it goes. */
+export type ColumnMapping = S["ColumnMapping"];
+/** Copy data's preview: why it cannot run as asked (spec §4). */
+export type TransferProblem = S["TransferProblem"];
 
 export function pathKey(path: PathSegment[]): string {
   return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

```diff
diff --git a/src/features/data-tools/entryPoints.test.ts b/src/features/data-tools/entryPoints.test.ts
index cd62db8..3abc61c 100644
--- a/src/features/data-tools/entryPoints.test.ts
+++ b/src/features/data-tools/entryPoints.test.ts
@@ -31,6 +31,7 @@ const engine = (name: EngineInfo["engine"], fields: Partial<EngineInfo> = {}): E
   orders_browse_by_key: name === "postgres",
   run_file: true,
   dump: name !== "couchbase",
+  copy_data: name !== "couchbase",
   ...fields,
 });
 const seg = (kind: string, name: string): PathSegment => ({ kind, name });
```

Run `npm run typecheck` → clean.

- [ ] **Step 12: Commit.**

```bash
git add backend/dbaguy_backend/services/plans.py backend/dbaguy_backend/services/edit_plans.py \
  backend/dbaguy_backend/services/transfer_plans.py backend/dbaguy_backend/services/transfer_service.py \
  backend/dbaguy_backend/domain/transfer.py backend/dbaguy_backend/sql/transfer_sql.py \
  backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/domain/profiles.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/adapters/editing.py backend/dbaguy_backend/api/data.py backend/dbaguy_backend/container.py \
  backend/tests/data_support.py backend/tests/transfer_support.py backend/tests/test_transfer_sql.py \
  backend/tests/test_transfer_plan.py src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts \
  src/features/data-tools/entryPoints.test.ts
git commit -m "feat(06c): the copy's preview and its one-time plan (POST /data/transfer/plan)"
```

---

### Task 3: The job framework's parts for a copy, the pipe and the source

**Suggested implementer:** Opus (the worker's cancel and job lifecycle; two sessions on two threads).

**Files:**
- Create: `backend/dbaguy_backend/sessions/jobs/pipe.py`, `backend/dbaguy_backend/sessions/jobs/transfer_source.py`
- Modify: `backend/dbaguy_backend/domain/data_jobs.py`, `backend/dbaguy_backend/domain/execution.py`, `backend/dbaguy_backend/sessions/jobs/context.py`, `backend/dbaguy_backend/sessions/jobs/dump.py`, `backend/dbaguy_backend/sessions/jobs/registry.py`, `backend/dbaguy_backend/sessions/manager.py`, `backend/dbaguy_backend/sessions/worker.py`, `backend/dbaguy_backend/sessions/hooks.py`, `src/app/quitQuestion.ts`, `src/features/data-tools/JobDialog.tsx`
- Test: `backend/tests/test_transfer_pipe.py`, `backend/tests/test_copy_framework.py` (new); `backend/tests/test_job_framework.py`, `backend/tests/transfer_support.py`
- Generated: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: Task 2 (`PlannedTable`, `SOURCE_SETTINGS`, `read_settings`, `set_setting`, `transfer_support`); 06a's job framework (`Job`, `JobContext`, `JobHooks`, `JobReport`, `SessionWorker._run_job`, `JobRegistry`), `is_plain_read`, `GuardAction`.
- Produces:
  - `domain/data_jobs.py`: `JobKind` gains `"copy_data"`; `TableOutcome = Literal["copied", "rolled_back", "not_started", "unknown"]`; `JobObject.outcome: TableOutcome | None`, `JobObject.source: str | None` (a copy's table); `JobInfo.source_profile_id: UUID | None` (a copy's source connection, for B13).
  - `domain/execution.py`: `CopyDataCommand(JobCommand){type: "copy_data", plan_id: str (1–64), source_session_id: str | None}`, in `ClientCommand`. (Until Task 4 the worker answers it `not_supported`.)
  - `sessions/jobs/context.py`: `FETCH_ROWS = 1000`, `FIRST_FETCH = 1`, `fetch_size(largest, budget) -> int` (moved from `dump.py`, which imports it); `SourceWorker` (protocol: `submit`, `cancel`), `SourceLease(worker, release)`; `JobHooks.announce(request_id, kind, title, source_profile_id)` and `JobHooks.source(profile_id, database, session_id) -> SourceLease`; `JobContext.setting(sql)` (a session setting, run with its messages dropped), `JobContext.matched(sql) -> int | None` (one DML statement's matched rows, then the rest of its answer read: ⑤'s `matched_rows()`, D-05-5), `JobContext.resume_cancels() -> bool` (a Cancel reaches the job's statements again; whether one came while it could not); `Job.helper: ClassVar[bool] = False`, `Job.source_profile_id -> UUID | None`; `PreparedJob(JobCommand){job}` (a job its maker built, submitted to another session's worker; never a client's command).
  - `sessions/worker.py`: a helper job is not announced to the job list and runs inside the session's open transaction (a query tab's, as an export does); a Cancel of the running job while its statements cannot be cancelled (`stop_cancels()`, a COMMIT) is held, and `resume_cancels()` honours it at the job's next check; `announce` carries the job's `source_profile_id`.
  - `sessions/jobs/registry.py`: `begin(…, source_profile_id=None)` → `JobInfo.source_profile_id`.
  - `sessions/manager.py`: `SessionManager._source(profile_id, database, session_id) -> SourceLease` (given to every session's job hooks): a query tab's session must be the plan's source profile's (`plan_mismatch`) and stays the tab's; otherwise a session of the copy's own, opened now (it may fail as any session does: a password, a server down) and closed by the lease's `release`.
  - `sessions/hooks.py`: a `PreparedJob` is its own job.
  - `sessions/jobs/pipe.py`: `PIPE_CHUNKS = 2`, `POLL_S = 0.1`, `SOURCE_FAILED = "source_failed"`, `PipeClosed(StatementCancelled)`, `SourceFailed(StatementError)` (`Reading <source> failed: <the source's text>`, §4), `RowPipe(source, capacity=PIPE_CHUNKS, poll_s=POLL_S)`: `put(rows, check)` (waits while full, `check` every `poll_s`; raises `PipeClosed` once the sink closed it), `finish()`, `fail(error)`, `take(check) -> rows | None` (None: the end; the source's error is raised as `SourceFailed` after the rows put before it), `close()`.
  - `sessions/jobs/transfer_source.py`: `PIPE_CHUNK_CHARS = 4 MiB`, `COLUMNS_CHANGED` / `COLUMNS_CHANGED_MESSAGE` (§4), `NOT_A_READ` (`query_not_select`), `row_chars(row) -> int`, `TransferSource(pipe, table: PlannedTable, *, query: bool, label: str)` (a helper `Job` of kind `copy_data`): reads the session's current `SOURCE_SETTINGS`, sets them (`ctx.setting`), runs the table's SELECT (a query's, or a filtered table's, only when `is_plain_read`, and through the session's gate), compares a query's columns with the plan's by name (`columns_changed`), reads `FIRST_FETCH` then `fetch_size(largest, PIPE_CHUNK_CHARS)` rows at a time into the pipe, `finish()`; any error goes to the pipe (`fail`) and ends the source's own outcome; the settings are set back in every case.
  - `tests/transfer_support.planned(select, columns, *, filtered=False) -> PlannedTable` (a source read on its own).
  - Frontend: `quitQuestion.ts` and `JobDialog.tsx` compile with a third `JobKind` (Task 6 gives copy its words).

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_transfer_pipe.py` and `backend/tests/test_copy_framework.py`; add the source's planned table to `transfer_support.py`; the job framework's scripted job gains a `resume` step (a copy's next table) and its announce carries the source profile:

```python
"""The pipe between a copy's source and its sink (spec ⑥ R-06-8, 06c Task 3): bounded, ordered, and each side stops on
its own Cancel or on the other side's end. Pure: two threads, no database."""

import threading
import time

import pytest

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.sessions.jobs.pipe import PIPE_CHUNKS, PipeClosed, RowPipe, SourceFailed


def never() -> None:
    return None


def test_the_rows_arrive_in_order_and_the_end_says_so() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.put([[2], [3]], never)
    pipe.finish()
    assert [pipe.take(never), pipe.take(never), pipe.take(never)] == [[[1]], [[2], [3]], None]


def test_a_full_pipe_holds_the_source_back_until_the_sink_takes() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    for n in range(PIPE_CHUNKS):
        pipe.put([[n]], never)
    put = threading.Event()

    def source() -> None:
        pipe.put([[99]], never)
        put.set()

    thread = threading.Thread(target=source)
    thread.start()
    assert not put.wait(0.2)  # the third chunk waits: the pipe holds PIPE_CHUNKS
    assert pipe.take(never) == [[0]]
    assert put.wait(2)
    thread.join(2)


def test_the_sources_error_comes_after_the_rows_put_before_it() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.fail(StatementError("division by zero", code="22012"))
    assert pipe.take(never) == [[1]]
    with pytest.raises(SourceFailed) as failed:
        pipe.take(never)
    assert (failed.value.code, failed.value.message) == ("source_failed", "Reading public.t failed: division by zero")
    assert failed.value.cause.code == "22012"


def test_a_sink_that_stops_stops_the_source_at_its_next_chunk() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    pipe.put([[1]], never)
    pipe.close()
    with pytest.raises(PipeClosed):
        pipe.put([[2]], never)


def test_each_side_waiting_on_the_other_sees_its_own_cancel() -> None:
    pipe = RowPipe("public.t", poll_s=0.01)
    asked = threading.Event()

    def cancel() -> None:
        if asked.is_set():
            raise StatementCancelled()

    threading.Timer(0.1, asked.set).start()
    started = time.monotonic()
    with pytest.raises(StatementCancelled):
        pipe.take(cancel)  # empty: the sink waits, and its Cancel ends the wait
    assert time.monotonic() - started < 2
    for n in range(PIPE_CHUNKS):
        pipe.put([[n]], never)
    with pytest.raises(StatementCancelled):
        pipe.put([[9]], cancel)  # full: the source waits, and its Cancel ends the wait
```

```python
"""The job framework's parts a copy adds (06c Task 3): a helper job on another session (never listed, run inside that
session's transaction), a Cancel held between two tables, the source's session lease, the job list's source profile,
and a copy's source read into the pipe on the dev databases (fetch sizing, a query's checks, its settings set back)."""

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.edits import PLAN_MISMATCH, PlanUnavailable
from dbaguy_backend.domain.execution import ExecuteCommand
from dbaguy_backend.sessions.jobs.context import Job, JobContext, JobReport, PreparedJob
from dbaguy_backend.sessions.jobs.pipe import RowPipe, SourceFailed
from dbaguy_backend.sessions.jobs.registry import JobRegistry
from dbaguy_backend.sessions.jobs.transfer_source import TransferSource
from dbaguy_backend.sessions.manager import SessionCreateRequest
from tests.data_support import DataApp, dropping
from tests.edit_support import scratch_db
from tests.support import requires_mysql
from tests.test_job_framework import HOLDS, Env, outcome
from tests.test_sessions import request_events
from tests.transfer_support import planned, profile, table_name


class Helper(Job):
    kind = "copy_data"
    helper = True

    @property
    def title(self) -> str:
        return "a helper"

    def run(self, ctx: JobContext) -> JobReport:
        ctx.drain(ctx.run("x"))
        return JobReport(ctx.outcome("completed", statements=1))


class CopyEnv(Env):
    @staticmethod
    def _build(command):
        return command.job if isinstance(command, PreparedJob) else Env._build(command)


@pytest.fixture
def env():
    made: list[CopyEnv] = []

    def make(**fields) -> CopyEnv:
        made.append(CopyEnv(**fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def test_a_helper_job_runs_inside_the_sessions_transaction_and_is_never_listed(env) -> None:
    e = env()
    e.conn.tx = True  # a query tab's open transaction: a copy's source reads in it, as an export does
    e.worker.submit(PreparedJob(request_id="h", job=Helper()))
    events = request_events(e.recorder, "h")
    assert (outcome(events).outcome, e.conn.runs) == ("completed", ["x"])
    assert not [item for item in e.listed if item[0] == "announce"]


def test_a_cancel_while_the_statements_cannot_be_cancelled_stops_the_next_table(env) -> None:
    HOLDS["between"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "hold between", "resume", "sql second")
    reached, go = HOLDS["between"]
    assert reached.wait(5)
    e.worker.cancel("j")  # its COMMIT, say: kept, never lost
    go.set()
    events = request_events(e.recorder, "j")
    assert (outcome(events).outcome, outcome(events).cancelled, e.conn.runs) == ("stopped", True, ["first"])


def test_without_a_cancel_the_next_table_goes_on(env) -> None:
    HOLDS["calm"] = (threading.Event(), threading.Event())
    e = env()
    e.submit("sql first", "hold calm", "resume", "sql second")
    assert HOLDS["calm"][0].wait(5)
    HOLDS["calm"][1].set()
    events = request_events(e.recorder, "j")
    assert (outcome(events).outcome, e.conn.runs) == ("completed", ["first", "second"])


def test_the_job_list_names_a_copys_source_connection() -> None:
    registry, source = JobRegistry(), uuid4()
    registry.begin("s", uuid4(), "r", "copy_data", "Copy t from a to b", source)
    assert registry.list()[0].source_profile_id == source


def test_a_copys_source_is_its_query_tabs_session_or_one_of_its_own(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        mine, other = profile(app, lite, "mine"), profile(app, lite, "other")
        manager = app.container.sessions
        tab = manager.create(SessionCreateRequest(profile_id=mine))
        borrowed = manager._source(mine, None, tab.session_id)
        assert borrowed.worker is manager.worker(tab.session_id)
        borrowed.release()
        assert manager.worker(tab.session_id) is borrowed.worker  # a query tab's session stays the tab's
        with pytest.raises(PlanUnavailable) as mismatch:
            manager._source(other, None, tab.session_id)  # the plan's source is another connection
        assert mismatch.value.code == PLAN_MISMATCH
        own = manager._source(other, None, None)
        opened = [sid for sid in manager._sessions if manager.worker(sid) is own.worker]
        own.release()
        assert opened and opened[0] not in manager._sessions  # the copy's own session ends with it
    finally:
        lite.close()
        app.close()


def _column(name: str) -> TableColumn:
    return TableColumn(name=name, type_name="", family="other", nullable=True, has_default=False, kind="other",
                       compare="none", writable=True)


def _drain(pipe: RowPipe) -> list[list]:
    chunks = []
    while (chunk := pipe.take(lambda: None)) is not None:
        chunks.append(chunk)
    return chunks


def test_a_source_reads_its_rows_a_chunk_at_a_time_sized_by_their_text(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        with dropping(lite, before_drop=app.close_sessions) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (n INTEGER)")
            made.append(("TABLE", name))
            lite.execute(f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 2500) "
                         f"INSERT INTO {lite.table(name)} SELECT n FROM s")
            tab = app.job_session(profile(app, lite, "lite"))
            pipe = RowPipe("t", poll_s=0.01)
            tab.worker.submit(PreparedJob(request_id="src", job=TransferSource(
                pipe, planned(f"SELECT n FROM {lite.table(name)}", [_column("n")]), query=False, label="t")))
            chunks = _drain(pipe)
        assert [len(chunk) for chunk in chunks] == [1, 1000, 1000, 499]  # the first read is one row (06a M1)
        assert chunks[0][0] == [1] and chunks[-1][-1] == [2500]
    finally:
        lite.close()
        app.close()


def test_a_query_that_writes_or_changed_its_columns_is_never_read(tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    lite = scratch_db("sqlite", tmp_path)
    try:
        with dropping(lite, before_drop=app.close_sessions) as made:
            name = table_name()
            lite.execute(f"CREATE TABLE {lite.table(name)} (a INTEGER)", f"INSERT INTO {lite.table(name)} VALUES (1)")
            made.append(("TABLE", name))
            tab = app.job_session(profile(app, lite, "lite"))
            causes = []
            for sql, columns in ((f"DELETE FROM {lite.table(name)} RETURNING a", ["a"]),
                                 (f"SELECT a AS b FROM {lite.table(name)}", ["a"])):
                pipe = RowPipe("Query 1", poll_s=0.01)
                tab.worker.submit(PreparedJob(request_id=f"q{len(causes)}", job=TransferSource(
                    pipe, planned(sql, [_column(c) for c in columns]), query=True, label="q")))
                with pytest.raises(SourceFailed) as failed:
                    _drain(pipe)
                causes.append(failed.value.cause.code)
            assert lite.rows(f"SELECT a FROM {lite.table(name)}") == [(1,)]  # the DELETE never ran
        assert causes == ["query_not_select", "columns_changed"]
    finally:
        lite.close()
        app.close()


@requires_mysql
def test_a_mysql_source_reads_in_utc_without_a_write_timeout_and_its_tab_gets_its_settings_back(
        tmp_path: Path) -> None:
    app = DataApp(tmp_path)
    my = scratch_db("mysql", tmp_path)
    try:
        tab = app.job_session(profile(app, my, "my"))
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="SET SESSION time_zone = '+02:00'"))
        pipe = RowPipe("Query 1", poll_s=0.01)
        sql = "SELECT @@SESSION.time_zone AS tz, @@SESSION.net_write_timeout AS w"
        tab.worker.submit(PreparedJob(request_id="src", job=TransferSource(
            pipe, planned(sql, [_column("tz"), _column("w")]), query=True, label="q")))
        assert _drain(pipe) == [[["+00:00", 3600]]]  # A11: a slow target cannot outwait the server's write
        after = tab.run(lambda rid: ExecuteCommand(request_id=rid, sql=sql))
        [rows] = [event.rows for event in after.events if event.type == "rows"]
        assert rows == [["+02:00", 60]]  # the tab's own session as it was
    finally:
        my.close()
        app.close()
```

```diff
diff --git a/backend/tests/transfer_support.py b/backend/tests/transfer_support.py
index 9f1658f..8563720 100644
--- a/backend/tests/transfer_support.py
+++ b/backend/tests/transfer_support.py
@@ -4,7 +4,9 @@
 from typing import Any
 from uuid import UUID
 
+from dbaguy_backend.adapters.columns import TableColumn
 from dbaguy_backend.domain.transfer import QuerySource, SourceSpec, TargetSpec, TransferRequest
+from dbaguy_backend.services.transfer_plans import PlannedColumn, PlannedTable
 from tests.data_support import SCRATCH_06C, DataApp, scratch_name
 from tests.edit_support import ScratchDb
 
@@ -39,3 +41,13 @@ def query_request(source: ScratchDb, source_profile: UUID, sql: str, columns: li
         target=TargetSpec(profile_id=target_profile, database=target.database, schema_name=target.schema, mode=mode,
                           table=table, **target_fields),
     )
+
+
+def planned(select: str, columns: list[TableColumn], *, filtered: bool = False) -> PlannedTable:
+    """A table of a plan, for a source read on its own (TransferSource): only its SELECT and columns matter."""
+    return PlannedTable(
+        source_name="the source", target_name="the target", target_schema=None, target_table="t", target_path=(),
+        select=select, columns=tuple(columns),
+        written=tuple(PlannedColumn(index, column.name) for index, column in enumerate(columns)), before=(), after=(),
+        created=False, filtered=filtered,
+    )
```

```diff
diff --git a/backend/tests/test_job_framework.py b/backend/tests/test_job_framework.py
index bda3981..9c5ac14 100644
--- a/backend/tests/test_job_framework.py
+++ b/backend/tests/test_job_framework.py
@@ -56,7 +56,8 @@ class ScriptedJob(Job):
     """Steps: `sql <text>` runs a statement and drains it (StatementCancelled → stopped, cancelled), `tick` and
     `tick!` progress, `msg <text>`, `msgs <n>`, `crash`, `fail <code>` (an error report), `record`, `hold <name>`
     (stop_cancels(), then wait as a COMMIT would: HOLDS), `wait <name>` (wait, a Cancel still lands), `check`
-    (raise_if_cancelled(), let through). Its broke() is the framework's, fails too, or a plain unknown."""
+    (raise_if_cancelled(), let through), `resume` (resume_cancels(): stopped, cancelled, when a Cancel was held). Its
+    broke() is the framework's, fails too, or a plain unknown."""
 
     kind = "run_file"
 
@@ -101,6 +102,9 @@ class ScriptedJob(Job):
                 assert go.wait(5)
             elif word == "check":
                 ctx.raise_if_cancelled()
+            elif word == "resume":  # a copy's next table (06c): a Cancel held since `hold` stops it here
+                if ctx.resume_cancels():
+                    return JobReport(ctx.outcome("stopped", cancelled=True, statements=done), StatementCancelled())
         return JobReport(ctx.outcome("completed", statements=done), schema_changed=done > 0)
 
     def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
@@ -180,7 +184,7 @@ def test_a_job_sends_its_events_in_order_and_one_outcome(env):  # D-06-2
     assert e.conn.runs == ["rows 3"]
     assert e.records == [("begin", "-- scripted"), ("finish", 7, "ok", e.records[1][3], 0, None)]
     assert e.schema_changes == 1
-    assert e.listed[0] == ("announce", "j", "run_file", "Scripted job")
+    assert e.listed[0] == ("announce", "j", "run_file", "Scripted job", None)  # no source profile: not a copy
     assert [item for item in e.listed[1:]] == [("report", "job_progress"), ("report", "job_progress"),
                                                ("report", "job_outcome")]
 
```

- [ ] **Step 2: Run them to see them fail.** `cd backend && .venv/bin/pytest -q tests/test_transfer_pipe.py tests/test_copy_framework.py tests/test_job_framework.py` → collection errors (`No module named 'dbaguy_backend.sessions.jobs.pipe'`; `cannot import name 'PreparedJob'`), and `test_job_framework.py` fails on its announce (no source profile yet) and its `resume` step.

- [ ] **Step 3: The domain.** Modify `backend/dbaguy_backend/domain/data_jobs.py` and `backend/dbaguy_backend/domain/execution.py`:

```diff
diff --git a/backend/dbaguy_backend/domain/data_jobs.py b/backend/dbaguy_backend/domain/data_jobs.py
index fa76329..14355af 100644
--- a/backend/dbaguy_backend/domain/data_jobs.py
+++ b/backend/dbaguy_backend/domain/data_jobs.py
@@ -1,5 +1,5 @@
-"""Data tools (spec ⑥): a job — a dump, a run of a SQL file — runs on a session of its own and ends with exactly one
-outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""
+"""Data tools (spec ⑥): a job — a dump, a run of a SQL file, a copy between connections — runs on a session of its own
+and ends with exactly one outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""
 
 from datetime import datetime
 from typing import ClassVar, Literal, Self
@@ -9,11 +9,13 @@ from pydantic import BaseModel, Field, model_validator
 
 from .profiles import Engine
 
-JobKind = Literal["dump", "run_file"]
+JobKind = Literal["dump", "run_file", "copy_data"]
 # R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
 # or a DDL ended the transaction), unknown (the link died during COMMIT). Run, statement by statement: completed,
 # stopped, unknown (the link died while a statement ran). Any job: not_started (cancelled while queued, its session
-# closed first, or refused before anything was sent). A cancel is the `cancelled` flag on one of these, never an outcome.
+# closed first, or refused before anything was sent). Copy (06c): completed (every table copied), partial (some were,
+# then one failed or a Cancel stopped it), rolled_back (none was), unknown (what a table's COMMIT did, or a table that
+# cannot roll back, is not known). A cancel is the `cancelled` flag on one of these, never an outcome.
 JobOutcomeKind = Literal["completed", "failed", "rolled_back", "partial", "stopped", "unknown", "not_started"]
 JobStage = Literal["starting", "schema", "data", "post_data", "statements", "commit"]
 
@@ -69,12 +71,19 @@ class StoppedAt(BaseModel):
     exact: bool = Field(default=True, json_schema_extra=lambda schema: schema.pop("default", None))
 
 
+# A table of a copy (06c, R-06-12): its rows are in, its transaction was rolled back, it never began, or what its
+# COMMIT (or a table that cannot roll back) did is not known
+TableOutcome = Literal["copied", "rolled_back", "not_started", "unknown"]
+
+
 class JobObject(BaseModel):
-    """One object of a dump, with the rows written for it."""
+    """One object of a dump, with the rows written for it; one table of a copy, with its outcome."""
 
     kind: str  # table, view, sequence, function, …
-    name: str  # as the file names it: schema.name
+    name: str  # as the file names it: schema.name (a copy: the target table)
     rows: int = 0
+    outcome: TableOutcome | None = None  # a copy's table
+    source: str | None = None  # a copy's table: what it was copied from
 
 
 class JobProgressEvent(BaseModel):
@@ -132,6 +141,8 @@ class JobInfo(BaseModel):
     job: JobKind
     title: str
     profile_id: UUID
+    # A copy's source connection (06c): its sessions close when that connection is deleted, so the delete waits
+    source_profile_id: UUID | None = None
     started_at: datetime
     progress: JobProgressEvent | None = None  # the last one sent
     outcome: JobOutcomeEvent | None = None  # set once the job ended
```

```diff
diff --git a/backend/dbaguy_backend/domain/execution.py b/backend/dbaguy_backend/domain/execution.py
index 0a16e01..0bc5f33 100644
--- a/backend/dbaguy_backend/domain/execution.py
+++ b/backend/dbaguy_backend/domain/execution.py
@@ -257,9 +257,20 @@ class DumpCommand(JobCommand):
         return self
 
 
+class CopyDataCommand(JobCommand):
+    """Copy data between connections (spec ⑥ §3.5.5): the previewed plan, run on this session (the target's), reading
+    each table on the source's: the query tab's session the plan's query came from, or one the copy opens and closes
+    itself (None). Each table is copied in a transaction of its own; nothing is ever sent again."""
+
+    job_kind = "copy_data"
+    type: Literal["copy_data"] = "copy_data"
+    plan_id: str = Field(min_length=1, max_length=64)
+    source_session_id: str | None = Field(default=None, min_length=1, max_length=64)
+
+
 ClientCommand = Annotated[
     ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
-    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand | DumpCommand,
+    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand | DumpCommand | CopyDataCommand,
     Field(discriminator="type"),
 ]
 
```

- [ ] **Step 4: The framework's parts.** Modify `backend/dbaguy_backend/sessions/jobs/context.py` (and `dump.py`, whose `fetch_size` moves there):

```diff
diff --git a/backend/dbaguy_backend/sessions/jobs/context.py b/backend/dbaguy_backend/sessions/jobs/context.py
index b1bc576..a2967af 100644
--- a/backend/dbaguy_backend/sessions/jobs/context.py
+++ b/backend/dbaguy_backend/sessions/jobs/context.py
@@ -8,10 +8,11 @@ from abc import ABC, abstractmethod
 from collections.abc import Callable
 from dataclasses import asdict, dataclass, field, replace
 from datetime import UTC, datetime
-from typing import Any, ClassVar
+from typing import Any, ClassVar, Protocol
+from uuid import UUID
 
 from loguru import logger
-from pydantic import BaseModel
+from pydantic import BaseModel, ConfigDict, Field
 
 from ...adapters.base import (
     AdapterCapabilities, DatabaseConnection, RunOptions, StatementCancelled, StatementError, StatementResult,
@@ -31,6 +32,14 @@ NO_DESCRIBE = RunOptions(describe=False)  # a job shows no result set: SQL Serve
 # rolls it back when the job's session closes (its dialog closes it, or the reaper once it is idle)
 JOB_ROLLBACK_FAILED = "The transaction could not be rolled back: it ends when the job's session closes"
 _DRAIN_CHUNK = 1000  # rows read and dropped at a time
+FETCH_ROWS = 1000  # rows a job reads at a time at most (the export's chunk)
+FIRST_FETCH = 1  # a table's first read, before the size of its rows is known (06a Task 9 fix M1)
+
+
+def fetch_size(largest: int, budget: int) -> int:
+    """How many rows the next read of a table takes: as many as fit `budget` characters at the size of the largest row
+    seen so far, at most FETCH_ROWS (06a pre-flight M12: memory follows the rows' size, never their number)."""
+    return max(1, min(FETCH_ROWS, budget // max(largest, 1)))
 
 
 def _no_job(command: JobCommand) -> "Job":
@@ -53,6 +62,27 @@ def _ignore(*_: object) -> None:
     return None
 
 
+class SourceWorker(Protocol):
+    """Another session's worker, as a copy reaches its source's (a SessionWorker): a job submitted, a request cancelled."""
+
+    def submit(self, command: BaseModel) -> None: ...
+
+    def cancel(self, request_id: str) -> None: ...
+
+
+@dataclass(frozen=True)
+class SourceLease:
+    """A copy's source session (06c): its worker, and what ends the lease — closing a session the copy opened for
+    itself; nothing for a query tab's."""
+
+    worker: SourceWorker
+    release: Callable[[], None]
+
+
+def _no_source(profile_id: UUID, database: str | None, session_id: str | None) -> SourceLease:
+    raise StatementError("This session cannot reach another connection", code="not_supported")
+
+
 @dataclass(frozen=True)
 class JobHooks:
     """How a job reaches the rest of the app (a session's WorkerHooks.jobs). The defaults run nothing and record
@@ -63,8 +93,11 @@ class JobHooks:
     read_only: Callable[[], bool] = _writable  # the profile is read-only now
     begin_record: Callable[[str, datetime], int | None] = _no_record  # History (D-06-9): the record's id
     finish_record: Callable[[int, HistoryStatus, int, int, str | None], None] = _ignore  # id, status, ms, rows, error
-    announce: Callable[[str, JobKind, str], None] = _ignore  # the job list (D-06-10): request id, kind, title
+    # The job list (D-06-10): request id, kind, title and a copy's source profile (None for the other jobs)
+    announce: Callable[[str, JobKind, str, UUID | None], None] = _ignore
     report: Callable[[BaseModel], None] = _ignore  # the job list: each job_progress and the job_outcome
+    # A copy's source session (06c): (source profile, its database, the query tab's session or None) → its lease
+    source: Callable[[UUID, str | None, str | None], SourceLease] = _no_source
 
 
 @dataclass
@@ -126,6 +159,7 @@ class JobContext:
     _publish: Callable[[BaseModel], None]  # a job event: sent, kept for a replay, and given to the job list
     _stop_cancels: Callable[[], bool]
     _sent: Callable[[], bool]
+    _resume_cancels: Callable[[], bool] = lambda: False
     _clock: Callable[[], float] = time.monotonic
     progress: JobProgress = field(default_factory=JobProgress)
     record_id: int | None = None
@@ -162,6 +196,30 @@ class JobContext:
         finally:
             self._close(result)
 
+    def setting(self, sql: str) -> None:
+        """A setting of the job's own session (a SET, a PRAGMA): run, and its answer dropped with its messages (MySQL
+        warns that a strict sql_mode without NO_ZERO_DATE is deprecated: nothing its reader can act on, 06c)."""
+        result = self.run(sql)
+        try:
+            result.take_messages()
+            while result.next_result():
+                result.take_messages()
+        finally:
+            self._close(result)
+
+    def matched(self, sql: str) -> int | None:
+        """One DML statement: the rows it matched, read first, then the rest of its answer to the end (so SQL Server
+        never stops it with an attention, ⑤ E9), its messages forwarded. None: not known (a trigger returned rows)."""
+        result = self.run(sql)
+        try:
+            rows = result.matched_rows()
+            self._forward(result)
+            while result.next_result():
+                self._forward(result)
+            return rows
+        finally:
+            self._close(result)
+
     def close(self, result: StatementResult) -> None:
         self._close(result)
 
@@ -173,6 +231,11 @@ class JobContext:
         """From now on a Cancel never reaches the driver (before a COMMIT, ⑤ R-05-6). Whether one had been asked."""
         return self._stop_cancels()
 
+    def resume_cancels(self) -> bool:
+        """A Cancel reaches the job's statements again (a copy's next table, after the COMMIT of the one before:
+        06c). Whether one came while it could not: the next check (`raise_if_cancelled`, `run`) then stops it."""
+        return self._resume_cancels()
+
     def tick(self, *, force: bool = False) -> None:
         """Send `progress` when PROGRESS_INTERVAL_S has passed since the last one, or now with `force` (an object
         starts or ends)."""
@@ -212,9 +275,18 @@ class JobContext:
 
 
 class Job(ABC):
-    """One job of one command. `run` classifies every way it ends (R-06-12) and never resends anything."""
+    """One job of one command. `run` classifies every way it ends (R-06-12) and never resends anything.
+
+    `helper`: the part of another job that runs on another session (a copy's source, 06c): never listed on its own, and
+    it runs inside whatever transaction that session has (a query tab's, as an export does)."""
 
     kind: ClassVar[JobKind]
+    helper: ClassVar[bool] = False
+
+    @property
+    def source_profile_id(self) -> UUID | None:
+        """A copy's source connection, for the job list (06c B13); None for the other jobs."""
+        return None
 
     @property
     @abstractmethod
@@ -246,3 +318,12 @@ def history_status(outcome: JobOutcomeEvent, error: StatementError | None) -> tu
         "unknown": f"Outcome unknown{where}", "failed": "Failed", "not_started": "Not started",
     }[outcome.outcome]
     return "error", f"{prefix}: {text}" if text else prefix
+
+
+class PreparedJob(JobCommand):
+    """A job its maker built, run like any job on the worker it is submitted to: a copy's source, made with the pipe to
+    its sink (06c). Never a client's command (not in ClientCommand): it comes from another job only."""
+
+    model_config = ConfigDict(arbitrary_types_allowed=True)
+    job_kind = "copy_data"
+    job: Any = Field(exclude=True)
```

```diff
diff --git a/backend/dbaguy_backend/sessions/jobs/dump.py b/backend/dbaguy_backend/sessions/jobs/dump.py
index ce57682..e01ab1f 100644
--- a/backend/dbaguy_backend/sessions/jobs/dump.py
+++ b/backend/dbaguy_backend/sessions/jobs/dump.py
@@ -46,12 +46,10 @@ from ...sql.dump_script import (
 )
 from ...sql.select_builder import FILTER_NOT_A_CONDITION, build_copy_select, build_count_select, check_filter
 from ...storage.atomic_file import atomic_text_file
-from .context import Job, JobContext, JobReport
+from .context import FETCH_ROWS, FIRST_FETCH, Job, JobContext, JobReport, fetch_size
 
 DUMP_IO = "dump_io"
-FETCH_ROWS = 1000  # rows read at a time at most (the export's chunk)
-FETCH_CHARS = 32 * 1_048_576  # … and about this much row text (pre-flight M12)
-FIRST_FETCH = 1  # a table's first read, before the size of its rows is known (Task 9 fix M1)
+FETCH_CHARS = 32 * 1_048_576  # about this much row text a read takes, at most FETCH_ROWS rows (pre-flight M12)
 # The free space is looked at every this much written (pre-flight M14): at most 64 MiB of bytes (4 a character, Task 9
 # fix L6) between two looks, under DISK_FLOOR_BYTES
 DISK_CHECK_CHARS = 16 * 1_048_576
@@ -328,7 +326,7 @@ class DumpJob(Job):
                 ctx.progress.rows += len(chunk)
                 ctx.raise_if_cancelled()
                 ctx.tick()
-                size = max(1, min(FETCH_ROWS, FETCH_CHARS // max(largest, 1)))  # fix M1: the largest row, not the mean
+                size = fetch_size(largest, FETCH_CHARS)  # fix M1: the largest row, not the mean
         finally:
             ctx.close(result)
         write(batcher.flush())
```

Modify `backend/dbaguy_backend/sessions/worker.py` (the helper job, the held Cancel, `_resume`):

```diff
diff --git a/backend/dbaguy_backend/sessions/worker.py b/backend/dbaguy_backend/sessions/worker.py
index 051c9ff..1b68c63 100644
--- a/backend/dbaguy_backend/sessions/worker.py
+++ b/backend/dbaguy_backend/sessions/worker.py
@@ -303,6 +303,10 @@ class SessionWorker:
         # A job runs, from its start until its last event went out (guarded by _state_lock). Past stop_cancels() no
         # request is "running", yet its COMMIT or rollback is: the reaper must not close it then (pre-flight L1)
         self._job_active = False
+        self._job_request: str | None = None  # that job's request id (guarded by _state_lock)
+        # A Cancel of that job while its statements could not be cancelled (a copy's COMMIT between two tables): kept
+        # for `_resume`, so the next table does not start (06c; guarded by _state_lock)
+        self._held_cancel = False
 
     # ---- lifecycle (any thread) ----
 
@@ -412,6 +416,11 @@ class SessionWorker:
                 logger.info("Request {} on {} cancelled before it started", request_id, self.name)
                 self._queued[request_id] = True
                 return
+            if self._job_active and self._job_request == request_id:  # its COMMIT, say: the job reads it later
+                logger.info("Cancel of job {} on {} kept until its statements can be cancelled again", request_id,
+                            self.name)
+                self._held_cancel = True
+                return
         try:
             self._put(lambda: self._discard(request_id, cancelled=True))  # a paused result, if it is the open one
         except SessionBusy:
@@ -1245,12 +1254,12 @@ class SessionWorker:
         a job cancelled while queued, or whose session closed first, still gets its job_outcome (not_started). The
         session is busy until the job's last event went out, through its COMMIT too (pre-flight L1)."""
         with self._state_lock:
-            self._job_active = True
+            self._job_active, self._job_request, self._held_cancel = True, cmd.request_id, False
         try:
             self._job(cmd)
         finally:
             with self._state_lock:
-                self._job_active = False
+                self._job_active, self._job_request, self._held_cancel = False, None, False
 
     def _job(self, cmd: JobCommand) -> None:
         """`_run_job`'s request, from `started` to `finished`."""
@@ -1278,10 +1287,13 @@ class SessionWorker:
                 _close=self._close_result, _raise_if_cancelled=self._raise_if_cancelled, _send=self._send,
                 _publish=self._job_event,
                 _stop_cancels=lambda: self._set_running(None), _sent=lambda: req.ran,
+                _resume_cancels=lambda: self._resume(req.request_id),
             )
-            self._notify("job list", self._hooks.jobs.announce, req.request_id, kind, job.title)
-            if self.connection.in_transaction():  # a job runs on a session of its own: never inside a user's
-                raise transaction_open_error()
+            if not job.helper:  # a copy's source is its copy's part: never listed, run in the session's transaction
+                self._notify("job list", self._hooks.jobs.announce, req.request_id, kind, job.title,
+                             job.source_profile_id)
+                if self.connection.in_transaction():  # a job runs on a session of its own: never inside a user's
+                    raise transaction_open_error()
             report = job.run(ctx)
         except Exception as exc:
             cancelled = isinstance(exc, StatementCancelled)  # a flag on whatever the outcome is (spec ⑥ §3.2)
@@ -1303,6 +1315,17 @@ class SessionWorker:
             self._set_running(None)
         self._end_job(req, ctx, report)
 
+    def _resume(self, request_id: str) -> bool:
+        """The job's statements can be cancelled again (a copy's next table, 06c): it is the running request once more,
+        and a Cancel held since `stop_cancels()` is honoured at its next check. Whether one was held."""
+        with self._state_lock:
+            held, self._held_cancel = self._held_cancel, False
+            self._running_request = request_id
+            self._cancel_requested = held
+            self._cancels = 0
+            self._tracker.activity(busy=True)
+        return held
+
     def _log_unclassified(self, request_id: str, exc: Exception) -> None:
         """An exception a job's run() let through once something was sent. A StatementError's text may echo literals:
         above DEBUG only its code (D-06-12); anything else is a bug, with its traceback."""
```

Modify `backend/dbaguy_backend/sessions/jobs/registry.py`, `backend/dbaguy_backend/sessions/manager.py` and `backend/dbaguy_backend/sessions/hooks.py`:

```diff
diff --git a/backend/dbaguy_backend/sessions/jobs/registry.py b/backend/dbaguy_backend/sessions/jobs/registry.py
index 3ee465b..061ed9b 100644
--- a/backend/dbaguy_backend/sessions/jobs/registry.py
+++ b/backend/dbaguy_backend/sessions/jobs/registry.py
@@ -21,9 +21,10 @@ class JobRegistry:
         self._jobs: dict[tuple[str, str], JobInfo] = {}  # in the order they started
         self._lock = threading.Lock()
 
-    def begin(self, session_id: str, profile_id: UUID, request_id: str, kind: JobKind, title: str) -> None:
+    def begin(self, session_id: str, profile_id: UUID, request_id: str, kind: JobKind, title: str,
+              source_profile_id: UUID | None = None) -> None:
         info = JobInfo(session_id=session_id, request_id=request_id, job=kind, title=title, profile_id=profile_id,
-                       started_at=self._clock())
+                       source_profile_id=source_profile_id, started_at=self._clock())
         with self._lock:
             self._jobs[(session_id, request_id)] = info
             self._trim()
```

```diff
diff --git a/backend/dbaguy_backend/sessions/manager.py b/backend/dbaguy_backend/sessions/manager.py
index 093383a..ce3f1de 100644
--- a/backend/dbaguy_backend/sessions/manager.py
+++ b/backend/dbaguy_backend/sessions/manager.py
@@ -8,11 +8,13 @@ from uuid import UUID, uuid4
 from loguru import logger
 from pydantic import BaseModel
 
+from ..domain.edits import PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE, PlanUnavailable
 from ..domain.errors import SessionNotFound
 from ..domain.metadata import ServerInfo
 from ..services.connection_service import ConnectionService
 from ..services.profile_service import ProfileService
 from .hooks import SessionHooksFactory
+from .jobs.context import SourceLease
 from .jobs.registry import JobRegistry
 from .worker import Emitter, SessionWorker
 
@@ -69,7 +71,7 @@ class SessionManager:
             if self._hooks is not None:
                 hooks = self._hooks.build(profile.id, server.current_database)
                 jobs = replace(hooks.jobs, announce=partial(self.jobs.begin, session_id, profile.id),
-                               report=partial(self.jobs.report, session_id))
+                               report=partial(self.jobs.report, session_id), source=self._source)
                 worker.configure(replace(hooks, jobs=jobs))  # before it is registered
         except BaseException:
             worker.close()  # whatever failed, never leave the thread, connection or tunnel behind
@@ -80,6 +82,22 @@ class SessionManager:
         logger.info("Opened session {} for '{}'", session_id, profile.name)
         return info
 
+    def _source(self, profile_id: UUID, database: str | None, session_id: str | None) -> SourceLease:
+        """A copy's source session (06c): the query tab's session the copy names, which must be one of the plan's source
+        profile (`plan_mismatch`), and stays the tab's; or a session of the copy's own, closed when the copy releases it.
+        Opening one may fail as a session does (a password, a server down): the copy then never started."""
+        if session_id is not None:
+            entry = self._entry(session_id)
+            if entry.info.profile_id != profile_id:
+                raise PlanUnavailable(PLAN_MISMATCH, PLAN_MISMATCH_MESSAGE)
+            return SourceLease(entry.worker, lambda: None)
+        info = self.create(SessionCreateRequest(profile_id=profile_id, database=database))
+        return SourceLease(self.worker(info.session_id), partial(self._release, info.session_id))
+
+    def _release(self, session_id: str) -> None:
+        """A copy's own source session ends with it (never reused, as a job's own session)."""
+        self._pop_and_close(session_id)
+
     def _entry(self, session_id: str) -> _Entry:
         with self._lock:
             entry = self._sessions.get(session_id)
```

```diff
diff --git a/backend/dbaguy_backend/sessions/hooks.py b/backend/dbaguy_backend/sessions/hooks.py
index 02d283c..a98d5fb 100644
--- a/backend/dbaguy_backend/sessions/hooks.py
+++ b/backend/dbaguy_backend/sessions/hooks.py
@@ -17,7 +17,7 @@ from ..services.history_service import HistoryService
 from ..services.profile_service import ProfileService
 from ..services.safety import GuardDecision, SafetyPolicy
 from ..services.schema_cache import SchemaCache
-from .jobs.context import Job, JobHooks
+from .jobs.context import Job, JobHooks, PreparedJob
 from .jobs.dump import DumpJob
 from .jobs.run_file import RunFileJob
 from .worker import ExecutionRecord, WorkerHooks
@@ -124,6 +124,8 @@ class SessionHooksFactory:
         command the engine has no tool for is refused with §4's sentence (pre-flight L4)."""
         capabilities = self._registry.get(profile.engine).capabilities
         match command:
+            case PreparedJob():  # made by another job (a copy's source, 06c)
+                return command.job
             case RunFileCommand() if capabilities.run_file:
                 return RunFileJob(command, profile.name)
             case RunFileCommand():
```

- [ ] **Step 5: The pipe and the source.** Create `backend/dbaguy_backend/sessions/jobs/pipe.py` and `backend/dbaguy_backend/sessions/jobs/transfer_source.py`:

```python
"""The rows between a copy's source and its sink (spec ⑥ R-06-8, R-06-13): two workers, each on its own connection's
thread, and at most PIPE_CHUNKS chunks between them (Q1). The source puts what it read and blocks while the pipe is
full; the sink takes and blocks while it is empty; both look at their own Cancel every POLL_S. The source ends it with
`finish()` or `fail(error)`; the sink with `close()` when it stops first (a target error, a Cancel), and the source's
next `put` then stops the read (`PipeClosed`). Any thread."""

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from ...adapters.base import StatementCancelled, StatementError

PIPE_CHUNKS = 2  # R-06-8: chunks the pipe holds
POLL_S = 0.1  # how often a side waiting on the other looks at its own Cancel (§3.5.5 step 5)
SOURCE_FAILED = "source_failed"
Rows = list[list[Any]]


class PipeClosed(StatementCancelled):
    """The sink stopped reading: the source ends as cancelled (nothing it read is wanted any more)."""

    def __init__(self) -> None:
        super().__init__("The copy stopped reading")


class SourceFailed(StatementError):
    """The source's read failed (spec §4 `source_failed`): its own error in `cause`, the text says where."""

    def __init__(self, source: str, cause: StatementError) -> None:
        super().__init__(f"Reading {source} failed: {cause.message}", code=SOURCE_FAILED)
        self.cause = cause


class RowPipe:
    def __init__(self, source: str, capacity: int = PIPE_CHUNKS, poll_s: float = POLL_S) -> None:
        self._source = source  # what messages call it
        self._capacity = capacity
        self._poll_s = poll_s
        self._chunks: deque[Rows] = deque()
        self._lock = threading.Condition()
        self._done = False
        self._error: StatementError | None = None
        self._closed = False

    # ---- the source's side ----

    def put(self, rows: Rows, check: Callable[[], None]) -> None:
        """Hand a chunk to the sink, waiting while the pipe is full; `check()` raises the source's own Cancel."""
        while True:
            with self._lock:
                if self._closed:
                    raise PipeClosed()
                if len(self._chunks) < self._capacity:
                    self._chunks.append(rows)
                    self._lock.notify_all()
                    return
                self._lock.wait(self._poll_s)
            check()

    def finish(self) -> None:
        """Every row was put."""
        with self._lock:
            self._done = True
            self._lock.notify_all()

    def fail(self, error: StatementError) -> None:
        """The read stopped on `error` (a cancel included): the sink raises it once the rows put before it are taken."""
        with self._lock:
            if not self._done and self._error is None:
                self._error = error
            self._lock.notify_all()

    # ---- the sink's side ----

    def take(self, check: Callable[[], None]) -> Rows | None:
        """The next chunk, waiting for one; None once every row was taken. `check()` raises the sink's own Cancel.
        Raises SourceFailed with the source's error, once the rows put before it are taken: the sink stops reading
        before it cancels its source (`close`), so a cancel here is not the copy's own (the tab's session closed)."""
        while True:
            with self._lock:
                if self._chunks:
                    rows = self._chunks.popleft()
                    self._lock.notify_all()
                    return rows
                if self._error is not None:
                    raise SourceFailed(self._source, self._error)
                if self._done:
                    return None
                self._lock.wait(self._poll_s)
            check()

    def close(self) -> None:
        """The sink stopped: what is in the pipe is dropped, and the source's next `put` stops it."""
        with self._lock:
            self._closed = True
            self._chunks.clear()
            self._lock.notify_all()
```

```python
"""A copy's source (spec ⑥ R-06-8, §3.5.5): one table's rows read on the source's own worker — the session the copy
opened for itself, or the query tab's (the same transaction and temporary tables as the tab, as an export) — and put
into the pipe to its sink, a chunk at a time. A helper job (`Job.helper`): never listed on its own; its events reach the
source session's client, which knows no such request (a query tab ignores them). Its outcome is not the copy's: the sink
reads the pipe, where every way it ends is put (`finish`, `fail`).

Its read is DbaGuy's SELECT of the table (with the table-data tab's filter, one condition that only reads), or the
tab's statement as it is, which must only read too (the classifier's plain read, as a dump's filter: Task 9 fix M2)
and give the columns the preview mapped (`columns_changed`). Around it the session's output settings are set to what
every engine reads and set back after (`transfer_sql.SOURCE_SETTINGS`): PostgreSQL's ISO dates, MySQL's UTC TIMESTAMPs
and a net_write_timeout a slow target cannot outwait (A11). Reads are sized by the rows' text (PIPE_CHUNK_CHARS a chunk,
at most FETCH_ROWS rows), so memory follows the rows' size, never their number (Q1)."""

from typing import Any

from loguru import logger

from ...adapters.base import StatementCancelled, StatementError
from ...services.safety import GuardAction, is_plain_read
from ...services.transfer_plans import PlannedTable
from ...sql.transfer_sql import SOURCE_SETTINGS, read_settings, set_setting
from .context import FIRST_FETCH, Job, JobContext, JobReport, fetch_size
from .pipe import RowPipe

PIPE_CHUNK_CHARS = 4 * 1_048_576  # R-06-13: a chunk's values hold about this much text at most
COLUMNS_CHANGED = "columns_changed"
COLUMNS_CHANGED_MESSAGE = "The query's columns changed since the preview: preview the copy again"
NOT_A_READ = "query_not_select"
NOT_A_READ_MESSAGE = "Only a statement that returns rows, and only reads, can be copied"
_OTHER_CHARS = 8  # what a value that is not text or bytes counts for in a chunk's size


def row_chars(row: list[Any]) -> int:
    return sum(len(value) if isinstance(value, str | bytes | bytearray) else _OTHER_CHARS for value in row)


class TransferSource(Job):
    kind = "copy_data"
    helper = True

    def __init__(self, pipe: RowPipe, table: PlannedTable, *, query: bool, label: str) -> None:
        self._pipe = pipe
        self._table = table
        self._query = query  # the tab's statement: it is checked to only read, and its columns are compared
        self._label = label  # the copy's name in the source session's log

    @property
    def title(self) -> str:
        return f"Read {self._table.source_name} for {self._label}"

    def run(self, ctx: JobContext) -> JobReport:
        saved: list[tuple[str, str]] = []
        rows = 0
        try:
            saved = self._read(ctx)
            for name, value in SOURCE_SETTINGS.get(ctx.dialect, ()):
                ctx.setting(set_setting(ctx.dialect, name, value))
            result = ctx.run(self._checked(ctx))
            try:
                self._compare(result.columns())
                size, largest = FIRST_FETCH, 0
                while chunk := result.fetch(size):  # until `[]`: an error may follow a short chunk (06a fix H1)
                    largest = max(largest, max(row_chars(row) for row in chunk))
                    rows += len(chunk)
                    self._pipe.put(chunk, ctx.raise_if_cancelled)
                    size = fetch_size(largest, PIPE_CHUNK_CHARS)
            finally:
                ctx.close(result)
            self._pipe.finish()
            return JobReport(ctx.outcome("completed", rows=rows))
        except StatementError as exc:
            self._pipe.fail(exc)
            return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", rows=rows,
                                         cancelled=isinstance(exc, StatementCancelled)), exc)
        except Exception as exc:  # a bug: the sink must not wait for rows that never come
            logger.exception("Reading {} for a copy failed", self._table.source_name)
            error = StatementError(f"Internal error: {exc}")
            self._pipe.fail(error)
            return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", rows=rows), error)
        finally:
            self._restore(ctx, saved)

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """Never reached (`run` classifies every error), but the sink must hear of it whatever happened."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        self._pipe.fail(error)
        return super().broke(ctx, exc)

    def _checked(self, ctx: JobContext) -> str:
        """The SELECT, when it may run: a statement the user wrote, or a filter of theirs in DbaGuy's, must only read
        (on any profile), and every statement passes the session's gate."""
        sql = self._table.select
        if (self._query or self._table.filtered) and not is_plain_read(sql, ctx.dialect):
            raise StatementError(NOT_A_READ_MESSAGE, code=NOT_A_READ)
        if ctx.hooks.gate(sql).action != GuardAction.RUN:
            raise StatementError("This connection is read-only, and the statement is not a plain read", code="read_only")
        return sql

    def _compare(self, columns: list | None) -> None:
        """A query runs again: its columns must be the ones the preview mapped (spec §3.5.5 step 2)."""
        expected = [column.name for column in self._table.columns]
        found = [column.name for column in columns or []]
        if columns is None or (self._query and found != expected):
            raise StatementError(COLUMNS_CHANGED_MESSAGE, code=COLUMNS_CHANGED)

    @staticmethod
    def _read(ctx: JobContext) -> list[tuple[str, str]]:
        """The session's own values of the settings the copy sets (`transfer_sql.SOURCE_SETTINGS`): set back after."""
        read = read_settings(ctx.dialect)
        if read is None:
            return []
        result = ctx.run(read)
        try:
            [values] = result.fetch(1)
            while result.fetch(1):
                pass
        finally:
            ctx.close(result)
        names = [name for name, _ in SOURCE_SETTINGS[ctx.dialect]]
        return [(name, str(value)) for name, value in zip(names, values, strict=True)]

    @staticmethod
    def _restore(ctx: JobContext, saved: list[tuple[str, str]]) -> None:
        """Set back what the copy changed (a query tab's session is the user's); no Cancel may stop it."""
        if not saved:
            return
        ctx.stop_cancels()
        for name, value in saved:
            try:
                ctx.setting(set_setting(ctx.dialect, name, value))
            except StatementError as exc:
                logger.warning("[copy] setting {} back on the source failed ({})", name, exc.code)
                return
```

- [ ] **Step 6: Run the tests.** `cd backend && .venv/bin/pytest -q tests/test_transfer_pipe.py tests/test_copy_framework.py tests/test_job_framework.py tests/test_session_worker.py tests/test_sessions.py tests/test_worker_protocol.py tests/test_session_wiring.py` → all pass (dry run: 151 passed; 13 new). `test_copy_framework.py`'s A11 test needs MySQL (`requires_mysql`); the source tests read SQLite and MySQL scratch tables.

- [ ] **Step 7: The frontend compiles with a third job kind.** Run `npm run gen:api` (`JobKind`, `JobObject`, `JobInfo`, `CopyDataCommand` change in the schema), then:

```diff
diff --git a/src/app/quitQuestion.ts b/src/app/quitQuestion.ts
index f8eae4f..29abab6 100644
--- a/src/app/quitQuestion.ts
+++ b/src/app/quitQuestion.ts
@@ -5,10 +5,12 @@
  * `QuitGuard.tsx` the two names are one file on the Mac's case-insensitive disk (06b pre-flight H2).
  */
 
+import type { JobInfo } from "@/lib/api/types";
+
 export type QuitHow = "quit" | "close";
 
 export interface RunningJob {
-  job: "dump" | "run_file";
+  job: JobInfo["job"];
   title: string;
 }
 
```

```diff
diff --git a/src/features/data-tools/JobDialog.tsx b/src/features/data-tools/JobDialog.tsx
index a34a027..e01adce 100644
--- a/src/features/data-tools/JobDialog.tsx
+++ b/src/features/data-tools/JobDialog.tsx
@@ -20,6 +20,7 @@ import { againEntry } from "./runForm";
 const KEEPS_RUNNING = {
   dump: "The dump keeps running: open it again from Jobs, at the bottom of the sidebar",
   run_file: "The SQL file keeps running: open it again from Jobs, at the bottom of the sidebar",
+  copy_data: "The copy keeps running: open it again from Jobs, at the bottom of the sidebar",
 } as const;
 /** The jobs whose dialog was closed while they ran and that were told so (once each). */
 const toldKeepsRunning = new Set<string>();
```

Run `npm run typecheck` → clean.

- [ ] **Step 8: Commit.**

```bash
git add backend/dbaguy_backend/domain/data_jobs.py backend/dbaguy_backend/domain/execution.py \
  backend/dbaguy_backend/sessions/jobs/context.py backend/dbaguy_backend/sessions/jobs/dump.py \
  backend/dbaguy_backend/sessions/jobs/pipe.py backend/dbaguy_backend/sessions/jobs/transfer_source.py \
  backend/dbaguy_backend/sessions/jobs/registry.py backend/dbaguy_backend/sessions/manager.py \
  backend/dbaguy_backend/sessions/worker.py backend/dbaguy_backend/sessions/hooks.py \
  backend/tests/test_transfer_pipe.py backend/tests/test_copy_framework.py backend/tests/test_job_framework.py \
  backend/tests/transfer_support.py src/lib/api/openapi.json src/lib/api/schema.d.ts src/app/quitQuestion.ts \
  src/features/data-tools/JobDialog.tsx
git commit -m "feat(06c): a copy's source on another session, the pipe between them, a Cancel held between tables"
```

---

### Task 4: The copy job — `TransferSink`, the `copy_data` command, its outcomes (A16)

**Suggested implementer:** Opus (R-06-12's outcomes; every way a table ends must be told truly).

**Files:**
- Create: `backend/dbaguy_backend/sessions/jobs/transfer.py`
- Modify: `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/sql/bulk_insert.py`
- Test: `backend/tests/test_transfer_job.py` (new)

**Interfaces:**
- Consumes: Tasks 1–3 (`conversion`, `RowFailure`, `StoredTransfer`, `PlannedTable`, `TARGET_SETUP`/`TARGET_RESET`, `RowPipe`, `TransferSource`, `PreparedJob`, `SourceLease`, `JobContext.setting`/`matched`/`resume_cancels`, `CopyDataCommand`, `TableOutcome`); 06a's `InsertBatcher`, `transaction_frame`, the gate (`ctx.gate`), `JOB_ROLLBACK_FAILED`; ⑤'s `ROW_COUNT_UNKNOWN`, `TRANSACTION_ENDED`, `insert_head` (SQLite's `INSERT OR ABORT`, ⑤ I1).
- Produces:
  - `InsertBatcher(…, abort_on_conflict=False)`: True writes the dialect's `insert_head` (SQLite `INSERT OR ABORT INTO`; the others `INSERT INTO`), so a SQLite constraint's own `ON CONFLICT REPLACE`/`IGNORE` never deletes or skips a row while the INSERT reports them all; `InsertBatcher.flushed_rows` (the rows of the statement `flush()` / `add()` just returned).
  - `sessions/jobs/transfer.py`: `TransferSink(command: CopyDataCommand, plan: StoredTransfer, source_dialect: Dialect)` (`Job`, kind `copy_data`; `title` is the plan's, `source_profile_id` the plan's source); codes `ROW_COUNT_MISMATCH = "row_count_mismatch"`, `VALUE_NOT_COPYABLE = "value_not_copyable"`; texts `CREATED_EMPTY`, `NOT_ROLLED_BACK`, `NOT_STARTED`. Its run: a read-only target now → `read_only`, nothing sent; a query plan without the tab's session, or a table plan with one → `plan_mismatch`; the source lease (`hooks.source`; a session that cannot be opened → "The source connection could not be opened: …", `not_started`); one History record (the plan's summary); the target session's `TARGET_SETUP`, quietly; then each table in the plan's order — from the second on, a Cancel held during the previous COMMIT stops here (`not_started`, `cancelled`) — MySQL's CREATE TABLE first when the plan says so, the dialect's BEGIN, MySQL's backslash mode read from that BEGIN's answer (`backslash_escapes()`, ⑤ M2), the plan's `before`, the target's columns read again (a column gone → `columns_changed`), each column's conversion, a `TransferSource` submitted to the source session as a `PreparedJob` (`<request id>:source:<k>`), each chunk taken from the pipe, converted (a `RowFailure` → `value_not_copyable`: "Row N of <source>, column <c>: <reason>"), batched and sent through the gate, each INSERT's matched rows checked (`None` → `row_count_unknown`; another number → `row_count_mismatch`: "An INSERT of N rows reported M rows (a trigger may have skipped or changed them): the table was rolled back"), the plan's `after`, COMMIT; the copy stops at the first table that is not copied; `TARGET_RESET` at the end. A table's end: copied; rolled back (any failure before COMMIT, a Cancel, the source's error as `source_failed`, the connection lost before COMMIT); unknown (a write went out to a table that cannot roll back, SQL Server's trigger COMMIT 3609, the transaction gone before COMMIT (`transaction_ended`), the connection lost during COMMIT, a COMMIT that left a transaction open (`commit_unconfirmed`, rolled back)); MySQL's created table stays, said (`CREATED_EMPTY`), never dropped. The copy's outcome: unknown if any table is; completed if all are copied; partial if some are; rolled_back if none is; `objects` lists every table of the plan with its outcome, rows and source; `schema_changed` when a table was created. On any exception while a table copies, the pipe is closed and the source's request cancelled.
  - `sessions/hooks.py`: `CopyDataCommand` → `TransferSink` when the engine copies and the plans are given, taken once from `TransferPlanStore` by this session's profile and database (`plan_expired`, `plan_mismatch` before anything is sent); otherwise `<Engine> connections cannot copy data` (`not_supported`). `SessionHooksFactory(…, transfers: TransferPlanStore | None = None)`; the container passes `self.transfers.plans`.

A16, probed while planning on the dev databases (only `zz_06c_*` tables, dropped by exact name): a multi-row INSERT's `matched_rows()` is its row count on PostgreSQL, MySQL and SQLite (MySQL's OK packet: `Records: n`) and SQL Server; a PostgreSQL `BEFORE` trigger returning NULL for two of four rows reports 2 (caught: `row_count_mismatch`); SQLite's `RAISE(IGNORE)` the same; SQL Server's `INSTEAD OF` trigger that skips rows reports 4 (the outer count: not detectable, as ⑤ A3: a known limitation); `AFTER` triggers that write elsewhere leave the count as it is (all engines); a SQL Server trigger that returns a result set makes the count unknown (`row_count_unknown`); MyISAM in a transaction: the ROLLBACK warns 1196 and the rows stay (so: `unknown`); duplicate keys are 23505 / 1062 / 2627 / `SQLITE_CONSTRAINT_PRIMARYKEY`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_transfer_job.py`:

```python
"""Copy data's job (spec ⑥ §3.5.5, R-06-12, 06c Task 4) on the dev databases: append and replace in one transaction per
table, a key conflict rolled back, identities and their sequences, a trigger that skips rows, a table that cannot roll
back, MySQL's CREATE TABLE that stays, several tables stopping at the first failure, a Cancel, a failing source, a
query tab's own session, a plan run once, and History. Scratch tables `zz_06c_*`, dropped by exact name."""

from pathlib import Path
from uuid import UUID

import pytest

from dbaguy_backend.domain.execution import CopyDataCommand, ExecuteCommand
from dbaguy_backend.sessions.jobs.transfer import CREATED_EMPTY, NOT_ROLLED_BACK
from tests.data_support import DataApp, JobTab, Ran, dropping
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg
from tests.transfer_support import profile, query_request, table_name, tables_request


@pytest.fixture
def app(tmp_path: Path):
    app = DataApp(tmp_path)
    yield app
    app.close()


def copy(app: DataApp, request, target_profile: UUID, database: str | None, *,
         source_session: str | None = None) -> Ran:
    plan = app.container.transfers.plan(request)
    assert plan.plan_id is not None, [p.message for t in plan.tables for p in t.problems] + [
        p.message for p in plan.problems]
    tab = app.job_session(target_profile, database)
    return tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id,
                                               source_session_id=source_session))


@requires_pg
def test_append_adds_rows_and_replace_takes_the_tables_place(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"INSERT INTO {lite.table(source)} VALUES (1, 'a'), (2, 'b')")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY, label text)",
                       f"INSERT INTO {pg.table(target)} VALUES (10, 'old')")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            appended = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            after_append = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
            replaced = copy(app, tables_request(lite, src, [source], pg, dst, "replace_rows", table=target), dst,
                            "dbaguy")
            after_replace = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
        assert (appended.outcome.outcome, appended.outcome.rows) == ("completed", 2)
        assert after_append == [(1, "a"), (2, "b"), (10, "old")]
        assert replaced.outcome.outcome == "completed" and after_replace == [(1, "a"), (2, "b")]
        records = app.history()
        assert [(entry.status, entry.sql.splitlines()[0]) for entry in records][:1] == [
            ("ok", f"-- Copy data: lite main.{source} → pg public.{target} (replace_rows)")]
        assert f"DELETE FROM public.{target}" in records[0].sql
    finally:
        lite.close()
        pg.close()


@requires_pg
def test_a_key_conflict_rolls_the_table_back_and_says_so(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"INSERT INTO {lite.table(source)} VALUES (1, 'a'), (10, 'clash')")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY, label text)",
                       f"INSERT INTO {pg.table(target)} VALUES (10, 'old')")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            ran = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            rows = pg.rows(f"SELECT id, label FROM {pg.table(target)} ORDER BY id")
        assert (ran.outcome.outcome, ran.outcome.objects[0].outcome, ran.error.code) == (
            "rolled_back", "rolled_back", "23505")
        assert rows == [(10, "old")]
        assert app.history()[0].status == "error" and app.history()[0].error.startswith("Rolled back")
    finally:
        lite.close()
        pg.close()


@requires_pg
@requires_mssql
def test_kept_identities_move_their_sequence_on_and_generated_ones_are_new(app: DataApp, tmp_path: Path) -> None:
    lite, pg, ms = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path), scratch_db("mssql", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as pg_made, dropping(ms) as ms_made:
            source, into_pg, into_ms = table_name("s"), table_name("p"), table_name("m")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"INSERT INTO {lite.table(source)} VALUES (5, 'a'), (40, 'b')")
            pg.execute(f"CREATE TABLE {pg.table(into_pg)} (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                       "label text)")
            ms.execute(f"CREATE TABLE {ms.table(into_ms)} (id int IDENTITY(1,1) PRIMARY KEY, label nvarchar(10))")
            pg_made.append(("TABLE", into_pg))
            ms_made.append(("TABLE", into_ms))
            src, to_pg, to_ms = profile(app, lite, "lite"), profile(app, pg, "pg"), profile(app, ms, "ms")
            key = f"main.{source}"
            kept = copy(app, tables_request(lite, src, [source], pg, to_pg, "append", table=into_pg), to_pg, "dbaguy")
            pg.execute(f"INSERT INTO {pg.table(into_pg)} (label) VALUES ('next')")
            kept_ms = copy(app, tables_request(lite, src, [source], ms, to_ms, "append", table=into_ms), to_ms,
                           "dbaguy")
            ms.execute(f"INSERT INTO {ms.table(into_ms)} (label) VALUES (N'next')")
            generated = copy(app, tables_request(lite, src, [source], ms, to_ms, "append", table=into_ms,
                                                 identity={key: {"id": "generate"}}), to_ms, "dbaguy")
            pg_rows = pg.rows(f"SELECT id, label FROM {pg.table(into_pg)} ORDER BY id")
            ms_rows = ms.rows(f"SELECT id, label FROM {ms.table(into_ms)} ORDER BY id")
        assert [ran.outcome.outcome for ran in (kept, kept_ms, generated)] == ["completed"] * 3
        assert pg_rows == [(5, "a"), (40, "b"), (41, "next")]  # setval moved the identity past 40 (A P2)
        assert ms_rows == [(5, "a"), (40, "b"), (41, "next"), (42, "a"), (43, "b")]
    finally:
        lite.close()
        pg.close()
        ms.close()


@requires_pg
def test_a_trigger_that_skips_rows_rolls_the_table_back(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            function = table_name("f")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"INSERT INTO {lite.table(source)} VALUES (1), (2), (3)")
            pg.execute(f"CREATE TABLE {pg.table(target)} (id int PRIMARY KEY)",
                       f"CREATE FUNCTION {pg.table(function)}() RETURNS trigger LANGUAGE plpgsql AS "
                       "$$ BEGIN IF NEW.id = 2 THEN RETURN NULL; END IF; RETURN NEW; END $$",
                       f"CREATE TRIGGER skip BEFORE INSERT ON {pg.table(target)} FOR EACH ROW EXECUTE FUNCTION "
                       f"{pg.table(function)}()")
            made += [("FUNCTION", function), ("TABLE", target)]
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            ran = copy(app, tables_request(lite, src, [source], pg, dst, "append", table=target), dst, "dbaguy")
            rows = pg.rows(f"SELECT count(*) FROM {pg.table(target)}")
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "row_count_mismatch")
        assert ran.error.message.startswith("An INSERT of 3 rows reported 2 rows") and rows == [(0,)]
    finally:
        lite.close()
        pg.close()


@requires_mysql
def test_a_table_that_cannot_roll_back_is_unknown_once_a_write_failed(app: DataApp, tmp_path: Path) -> None:
    lite, my = scratch_db("sqlite", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(my, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY)",
                         f"INSERT INTO {lite.table(source)} VALUES (1), (2), (3)")
            my.execute(f"CREATE TABLE {my.table(target)} (id int PRIMARY KEY) ENGINE=MyISAM",
                       f"INSERT INTO {my.table(target)} VALUES (3)")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, my, "my")
            ran = copy(app, tables_request(lite, src, [source], my, dst, "append", table=target), dst, None)
            rows = my.rows(f"SELECT id FROM {my.table(target)} ORDER BY id")
        assert (ran.outcome.outcome, ran.error.code) == ("unknown", "1062")
        assert NOT_ROLLED_BACK.format(target) in ran.outcome.warnings
        assert rows == [(1,), (2,), (3,)]  # MyISAM kept the rows before the duplicate: what stays is not known
    finally:
        lite.close()
        my.close()


@requires_pg
@requires_mysql
def test_mysql_keeps_the_table_it_created_when_a_value_fails_its_row(app: DataApp, tmp_path: Path) -> None:
    pg, my = scratch_db("postgres", tmp_path), scratch_db("mysql", tmp_path)
    try:
        with dropping(pg) as pg_made, dropping(my, before_drop=app.close_sessions) as my_made:
            source, target = table_name("s"), table_name("t")
            pg.execute(f"CREATE TABLE {pg.table(source)} (id int PRIMARY KEY, at timestamp)",
                       f"INSERT INTO {pg.table(source)} VALUES (1, '2026-10-05'), (2, 'infinity')")
            pg_made.append(("TABLE", source))
            my_made.append(("TABLE", target))
            src, dst = profile(app, pg, "pg"), profile(app, my, "my")
            ran = copy(app, tables_request(pg, src, [source], my, dst, table=target), dst, None)
            rows = my.rows(f"SELECT COUNT(*) FROM {my.table(target)}")
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "value_not_copyable")
        assert ran.error.message == (f"Row 2 of public.{source}, column at: is 'infinity', which MySQL cannot hold")
        assert CREATED_EMPTY.format(target) in ran.outcome.warnings and rows == [(0,)]
    finally:
        pg.close()
        my.close()


def test_several_tables_stop_at_the_first_that_fails(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        names = [table_name("a"), table_name("b"), table_name("c")]
        for name in names:
            source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)",
                           f"INSERT INTO {source.table(name)} VALUES (1), (2)")
            target.execute(f"CREATE TABLE {target.table(name)} (id INTEGER PRIMARY KEY)")
        target.execute(f"INSERT INTO {target.table(names[1])} VALUES (2)")  # b's second row clashes
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        ran = copy(app, tables_request(source, src, names, target, dst, "append"), dst, None)
        counts = [target.rows(f"SELECT COUNT(*) FROM {target.table(name)}")[0][0] for name in names]
        assert ran.outcome.outcome == "partial"
        assert [(item.outcome, item.rows) for item in ran.outcome.objects] == [
            ("copied", 2), ("rolled_back", 0), ("not_started", 0)]
        assert counts == [2, 1, 0] and ran.error.code == "SQLITE_CONSTRAINT_PRIMARYKEY"
    finally:
        source.close()
        target.close()


@requires_pg
def test_a_cancel_rolls_the_table_back(app: DataApp, tmp_path: Path) -> None:
    lite, pg = scratch_db("sqlite", tmp_path), scratch_db("postgres", tmp_path)
    try:
        with dropping(pg, before_drop=app.close_sessions) as made:
            source, target = table_name("s"), table_name("t")
            lite.execute(f"CREATE TABLE {lite.table(source)} (id INTEGER PRIMARY KEY, label TEXT)",
                         f"WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 300000) "
                         f"INSERT INTO {lite.table(source)} SELECT n, 'row ' || n FROM s")
            made.append(("TABLE", target))
            src, dst = profile(app, lite, "lite"), profile(app, pg, "pg")
            plan = app.container.transfers.plan(tables_request(lite, src, [source], pg, dst, table=target))
            tab = app.job_session(dst, "dbaguy")
            rid = tab.submit(lambda r: CopyDataCommand(request_id=r, plan_id=plan.plan_id))
            tab.wait_for_progress(rid, lambda event: event.rows > 0)
            tab.worker.cancel(rid)
            ran = tab.wait(rid)
            exists = pg.rows(f"SELECT to_regclass('public.{target}') IS NOT NULL")
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.objects[0].outcome) == (
            "rolled_back", True, "rolled_back")
        assert exists == [(False,)]  # its CREATE TABLE was in the rolled-back transaction
    finally:
        lite.close()
        pg.close()


@requires_pg
def test_a_query_tabs_copy_reads_on_its_session_and_a_failing_read_is_the_sources(app: DataApp,
                                                                                   tmp_path: Path) -> None:
    pg, lite = scratch_db("postgres", tmp_path), scratch_db("sqlite", tmp_path)
    try:
        src, dst = profile(app, pg, "pg"), profile(app, lite, "lite")
        info = app.job_session(src, "dbaguy")
        tab = JobTab(info.worker, info.recorder, info.session_id)
        tab.run(lambda rid: ExecuteCommand(request_id=rid, sql="CREATE TEMP TABLE zz_06c_tmp AS SELECT 7 AS n"))
        columns = [{"name": "n", "type_name": "int4", "category": "number"}]
        ran = copy(app, query_request(pg, src, "SELECT n FROM zz_06c_tmp", columns, lite, dst, "zz_06c_q"), dst, None,
                   source_session=tab.session_id)
        failing = copy(app, query_request(pg, src, "SELECT 1 / (n - 500) AS n FROM generate_series(1, 1000) n",
                                          columns, lite, dst, "zz_06c_q2"), dst, None, source_session=tab.session_id)
        assert ran.outcome.outcome == "completed" and lite.rows("SELECT n FROM zz_06c_q") == [(7,)]
        assert (failing.outcome.outcome, failing.error.code) == ("rolled_back", "source_failed")
        assert failing.error.message.startswith("Reading Query 1 failed: division by zero")
        assert lite.rows("SELECT name FROM sqlite_master WHERE name = 'zz_06c_q2'") == []
    finally:
        pg.close()
        lite.close()


def test_a_plan_runs_once(app: DataApp, tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    source, target = scratch_db("sqlite", tmp_path / "s"), scratch_db("sqlite", tmp_path / "t")
    try:
        name = table_name()
        source.execute(f"CREATE TABLE {source.table(name)} (id INTEGER PRIMARY KEY)")
        src, dst = profile(app, source, "s"), profile(app, target, "t")
        plan = app.container.transfers.plan(tables_request(source, src, [name], target, dst))
        tab = app.job_session(dst)
        first = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        again = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
        assert first.outcome.outcome == "completed"
        assert (again.outcome.outcome, again.error.code) == ("not_started", "plan_expired")
    finally:
        source.close()
        target.close()


def test_a_copy_command_whose_plan_is_gone_never_starts(app: DataApp, tmp_path: Path) -> None:
    lite = scratch_db("sqlite", tmp_path)
    try:
        tab = app.job_session(profile(app, lite, "lite"))
        ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id="nothing"))
        assert (ran.outcome.outcome, ran.error.code) == ("not_started", "plan_expired")
    finally:
        lite.close()
```

- [ ] **Step 2: Run them to see them fail.** `cd backend && .venv/bin/pytest -q tests/test_transfer_job.py` → collection error (`No module named 'dbaguy_backend.sessions.jobs.transfer'`).

- [ ] **Step 3: `INSERT OR ABORT` and the rows of a statement.** Modify `backend/dbaguy_backend/sql/bulk_insert.py`:

```diff
diff --git a/backend/dbaguy_backend/sql/bulk_insert.py b/backend/dbaguy_backend/sql/bulk_insert.py
index 31edbf2..3f14080 100644
--- a/backend/dbaguy_backend/sql/bulk_insert.py
+++ b/backend/dbaguy_backend/sql/bulk_insert.py
@@ -11,7 +11,7 @@ from typing import Any
 from ..adapters.columns import TableColumn
 from ..domain.column_category import ColumnCategory
 from ..domain.edits import kind_category
-from .dialects import Dialect, overriding_identity, quote_ident
+from .dialects import Dialect, insert_head, overriding_identity, quote_ident
 from .lexer import TokenKind, scan
 from .literals import Cell, CellLiterals, LiteralStyle, normalise_raw
 from .statement_stream import MAX_STATEMENT_CHARS
@@ -40,18 +40,21 @@ def _cell(value: Any) -> Cell:
 
 class InsertBatcher:
     """`add()` a row at a time, then `flush()`; each returns a finished statement (no terminator: the file adds it,
-    `dump_script.insert_text`) or None. `row_chars`: the characters of every row's text so far (a dump sizes its next
-    fetch by it, pre-flight M12)."""
+    `dump_script.insert_text`) or None, and `flushed_rows` is how many rows that statement holds. `row_chars`: the
+    characters of every row's text so far (a dump sizes its next fetch by it, pre-flight M12). `abort_on_conflict`: a
+    copy into an existing table (06c) writes SQLite's `INSERT OR ABORT`, so a constraint's own ON CONFLICT REPLACE or
+    IGNORE never deletes or skips a row while the INSERT reports them all (⑤ final review I1)."""
 
     def __init__(
         self, dialect: Dialect, table_sql: str, columns: Sequence[TableColumn], *, backslash_escapes: bool | None = None,
-        max_rows: int | None = None, max_chars: int = MAX_INSERT_CHARS,
+        max_rows: int | None = None, max_chars: int = MAX_INSERT_CHARS, abort_on_conflict: bool = False,
     ) -> None:
         self._dialect = dialect
         self._names = [column.name for column in columns]
         names = ", ".join(quote_ident(dialect, column.name) for column in columns)
         overriding = overriding_identity(dialect) if any(c.identity == "always" for c in columns) else None
-        self._head = f"INSERT INTO {table_sql} ({names}){f' {overriding}' if overriding else ''} VALUES\n"
+        verb = insert_head(dialect, table_sql) if abort_on_conflict else f"INSERT INTO {table_sql}"
+        self._head = f"{verb} ({names}){f' {overriding}' if overriding else ''} VALUES\n"
         writers = {unicode: CellLiterals(dialect, LiteralStyle(national=unicode, backslash_escapes=backslash_escapes))
                    for unicode in (True, False)}
         self._cells: list[tuple[CellLiterals, ColumnCategory]] = [
@@ -61,6 +64,7 @@ class InsertBatcher:
         self._rows: list[str] = []
         self._chars = 0
         self.row_chars = 0
+        self.flushed_rows = 0
 
     def row_text(self, row: Sequence[Any]) -> str:
         return "(" + ", ".join(write(_cell(value), category)
@@ -80,6 +84,7 @@ class InsertBatcher:
         if not self._rows:
             return None
         statement = self._head + ",\n".join(self._rows)
+        self.flushed_rows = len(self._rows)
         self._rows, self._chars = [], 0
         return statement
 
```

- [ ] **Step 4: The sink.** Create `backend/dbaguy_backend/sessions/jobs/transfer.py`:

```python
"""Copy data (spec ⑥ §3.5.5): a previewed plan run on the target's job session, each table in a transaction of its own,
its rows read on the source's worker (`TransferSource`) and handed over a pipe (R-06-8). What the preview showed is
what runs: its CREATE TABLE, DELETE and identity statements, its columns and SELECTs. Only the INSERTs are made here,
for the target session as it is (§3.5.5 step 1: MySQL's backslash mode read after its BEGIN; the session runs a dump's
restore settings, `transfer_sql.TARGET_SETUP`): each value converted as the type map says (`type_map.conversion`; a
value the target cannot hold fails its row, named by row and column), written as the target column's literal
(`InsertBatcher`: at most 1,000 rows, 100 on SQL Server, 1 MiB a statement), and each INSERT must report its rows (a
trigger that skipped or added rows fails the table, ⑤ D-05-5; SQL Server's INSTEAD OF triggers report the rows asked
for, A16).

Outcomes (R-06-12), per table: copied, rolled_back, not_started, unknown; for the copy: completed, partial (some tables
copied, then one failed or a Cancel stopped it), rolled_back (none copied), unknown (a COMMIT whose answer was lost, a
table that cannot roll back after a write went out, a SQL Server trigger's COMMIT). A copy stops at the first table that
does not copy. A Cancel stops the table being copied (rolled back) and every table after it; one that comes during a
COMMIT stops the next table. Nothing is ever sent again.

Logs: INFO the copy's tables, rows and outcome per table (the worker logs the job's end); never a value."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from loguru import logger

from ...adapters.base import CONNECTION_LOST, StatementCancelled, StatementError
from ...adapters.columns import TableColumn
from ...domain.data_jobs import JobObject, JobOutcomeKind, TableOutcome
from ...domain.edits import PlanUnavailable
from ...domain.errors import READ_ONLY_MESSAGE, DomainError
from ...domain.execution import CopyDataCommand
from ...services.safety import GuardAction
from ...services.transfer_plans import PlannedTable, StoredTransfer
from ...sql.bulk_insert import InsertBatcher
from ...sql.dialects import Dialect, qualified, transaction_frame
from ...sql.transfer_sql import TARGET_RESET, TARGET_SETUP
from ...sql.type_map import Convert, RowFailure, conversion
from ..edit_apply import ROW_COUNT_UNKNOWN, TRANSACTION_ENDED
from .context import JOB_ROLLBACK_FAILED, Job, JobContext, JobReport, PreparedJob, SourceLease
from .pipe import RowPipe
from .transfer_source import TransferSource

ROW_COUNT_MISMATCH = "row_count_mismatch"
VALUE_NOT_COPYABLE = "value_not_copyable"
CREATED_EMPTY = ("{} was created (MySQL commits a CREATE TABLE at once) and stays, empty: drop it, or copy again "
                 "into it")
NOT_ROLLED_BACK = "{} cannot roll back (non-transactional): the rows written before the failure stay"
NOT_STARTED = "{} were not copied"


@dataclass
class _Table:
    """One table's end."""

    outcome: TableOutcome
    rows: int = 0
    error: StatementError | None = None
    warnings: list[str] = field(default_factory=list)


class TransferSink(Job):
    kind = "copy_data"

    def __init__(self, command: CopyDataCommand, plan: StoredTransfer, source_dialect: Dialect) -> None:
        self._command = command
        self._plan = plan
        self._source_dialect = source_dialect

    @property
    def title(self) -> str:
        return self._plan.title

    @property
    def source_profile_id(self) -> UUID | None:
        return self._plan.source_profile_id

    def run(self, ctx: JobContext) -> JobReport:
        """What is refused before anything is sent is `not_started` (the worker's)."""
        plan, command = self._plan, self._command
        if ctx.hooks.read_only():
            raise StatementError(READ_ONLY_MESSAGE, code="read_only")
        if plan.query != (command.source_session_id is not None):
            raise StatementError("A query's copy reads on its tab's session, a table's on one of its own: preview the "
                                 "copy again", code="plan_mismatch")
        try:
            lease = ctx.hooks.source(plan.source_profile_id, plan.source_database, command.source_session_id)
        except PlanUnavailable as exc:
            raise StatementError(exc.message, code=exc.code) from None
        except DomainError as exc:  # a password, a server down, the tab's session gone: nothing was sent
            raise StatementError(f"The source connection could not be opened: {exc.message}", code=exc.code) from None
        try:
            ctx.begin_record(plan.summary)
            logger.info("[copy] {} table(s): {}", len(plan.tables), plan.title)
            return _Copy(ctx, plan, lease, self._source_dialect).go()
        finally:
            lease.release()


class _Copy:
    """One copy's tables, in order, and the outcome table of R-06-12."""

    def __init__(self, ctx: JobContext, plan: StoredTransfer, lease: SourceLease, source_dialect: Dialect) -> None:
        self.ctx = ctx
        self.plan = plan
        self.lease = lease
        self.source_dialect = source_dialect
        self.dialect = ctx.dialect
        self.frame = transaction_frame(ctx.dialect)
        self.statements = 0  # INSERTs sent and done

    def go(self) -> JobReport:
        ctx = self.ctx
        ended: list[_Table] = []
        try:
            for sql in TARGET_SETUP.get(self.dialect, ()):
                ctx.setting(sql)
        except StatementError as exc:
            return self._report([_Table("not_started", error=exc)], setup_failed=True)
        for index, table in enumerate(self.plan.tables):
            if index and ctx.resume_cancels():  # a Cancel during the COMMIT before: this table does not start
                ended.append(_Table("not_started", error=StatementCancelled()))
                break
            done = self._table(index, table)
            ended.append(done)
            logger.log("WARNING" if done.outcome == "unknown" else "INFO", "[copy] {} → {}: {} ({:,} rows)",
                       table.source_name, table.target_name, done.outcome, done.rows)
            if done.outcome != "copied":
                break
        self._reset()
        return self._report(ended)

    # ---- one table ----

    def _table(self, index: int, table: PlannedTable) -> _Table:
        ctx = self.ctx
        ctx.progress.stage, ctx.progress.object = "data", table.target_name
        ctx.tick(force=True)
        created = sent = False
        rows = 0
        try:
            if table.create_first:  # MySQL: it commits at once, before the table's transaction
                ctx.drain(ctx.run(table.create_first))
                created = True
            ctx.drain(ctx.run(self.frame.begin))
            escapes = ctx.connection.backslash_escapes() if self.dialect is Dialect.MYSQL else None
            for sql in table.before:
                ctx.drain(ctx.run(sql))
            batcher, converts = self._writer(table, escapes)
            pipe = RowPipe(table.source_name)
            source_id = f"{ctx.request_id}:source:{index + 1}"
            self.lease.worker.submit(PreparedJob(request_id=source_id, job=TransferSource(
                pipe, table, query=self.plan.query, label=self.plan.title)))
            try:
                while (chunk := pipe.take(ctx.raise_if_cancelled)) is not None:
                    for row in chunk:
                        rows += 1
                        if (statement := batcher.add(self._values(table, converts, row, rows))) is not None:
                            sent = True
                            self._insert(statement, batcher.flushed_rows)
                    ctx.progress.rows += len(chunk)
                    ctx.tick()
                if (statement := batcher.flush()) is not None:
                    sent = True
                    self._insert(statement, batcher.flushed_rows)
            except BaseException:
                pipe.close()  # the source's next chunk stops it; a read it waits on is cancelled
                self.lease.worker.cancel(source_id)
                raise
            for sql in table.after:
                ctx.drain(ctx.run(sql))
        except StatementError as exc:
            return self._failed(table, exc, created=created, sent=sent, counted=rows)
        return self._commit(table, rows, created)

    def _writer(self, table: PlannedTable, escapes: bool | None) -> tuple[InsertBatcher, list[Convert | None]]:
        """The INSERTs' writer for the target table as its catalog says now (after the plan's CREATE TABLE), and each
        written column's conversion."""
        found = {column.name: column for column in self.ctx.connection.table_columns(list(table.target_path))}
        targets: list[TableColumn] = []
        for planned in table.written:
            if planned.target not in found:
                raise StatementError(f"{table.target_name} has no column {planned.target} any more: preview the copy "
                                     "again", code="columns_changed")
            targets.append(found[planned.target])
        converts = [conversion(table.columns[planned.source], target, self.source_dialect, self.dialect,
                               backslash_escapes=escapes).convert for planned, target in zip(table.written, targets,
                                                                                              strict=True)]
        name = qualified(self.dialect, table.target_schema, table.target_table)
        return InsertBatcher(self.dialect, name, targets, backslash_escapes=escapes, abort_on_conflict=True), converts

    @staticmethod
    def _values(table: PlannedTable, converts: list[Convert | None], row: list[Any], number: int) -> list[Any]:
        values = []
        for planned, convert in zip(table.written, converts, strict=True):
            value = row[planned.source]
            if convert is not None:
                try:
                    value = convert(value)
                except RowFailure as failure:
                    raise StatementError(f"Row {number:,} of {table.source_name}, column {planned.target}: "
                                         f"{failure.reason}", code=VALUE_NOT_COPYABLE) from None
            values.append(value)
        return values

    def _insert(self, sql: str, rows: int) -> None:
        """One INSERT: through the session's gate, and it must report the rows it was given (⑤ D-05-5, A16)."""
        ctx = self.ctx
        if ctx.hooks.gate(sql).action != GuardAction.RUN:
            raise StatementError(READ_ONLY_MESSAGE, code="read_only")
        matched = ctx.matched(sql)
        if matched is None:
            raise StatementError("Could not confirm how many rows an INSERT wrote (a trigger returned rows?): the "
                                 "table was rolled back", code=ROW_COUNT_UNKNOWN)
        if matched != rows:
            raise StatementError(f"An INSERT of {rows:,} rows reported {matched:,} rows (a trigger may have skipped "
                                 "or changed them): the table was rolled back", code=ROW_COUNT_MISMATCH)
        self.statements += 1
        ctx.progress.statements = self.statements

    def _commit(self, table: PlannedTable, rows: int, created: bool) -> _Table:
        """COMMIT, once nothing stopped the table (⑤ §3.5 step 7): no Cancel reaches it."""
        ctx, connection = self.ctx, self.ctx.connection
        ctx.progress.stage = "commit"
        ctx.tick(force=True)
        cancelled = ctx.stop_cancels()
        try:
            if cancelled:
                return self._undone(table, StatementCancelled(), created=created)
            if connection.transaction_failed():
                return self._undone(table, StatementError("The transaction had failed", code="transaction_failed"),
                                    created=created)
            if not connection.in_transaction():
                return _Table("unknown", error=StatementError(
                    f"The transaction of {table.target_name} ended before its COMMIT: what was written may or may not "
                    "stay", code="transaction_ended"))
            ctx.drain(ctx.run(self.frame.commit))
        except StatementError as exc:
            if exc.code == CONNECTION_LOST:
                return _Table("unknown", error=exc)
            return self._undone(table, exc, created=created)
        try:
            left_open = connection.in_transaction()
        except StatementError as exc:
            return _Table("unknown", error=exc)
        if left_open:  # nested @@TRANCOUNT, or MySQL completion_type=CHAIN: what COMMIT did cannot be told (⑤ G11)
            self._rollback()
            return _Table("unknown", error=StatementError(
                "COMMIT returned but a transaction is still open: it was rolled back; what was copied is unknown",
                code="commit_unconfirmed"))
        return _Table("copied", rows=rows)

    def _failed(self, table: PlannedTable, error: StatementError, *, created: bool, sent: bool, counted: int) -> _Table:
        """A table that stopped before its COMMIT: rolled back, unless it cannot be (a table that is not
        transactional, once a write went out: ⑤ G10) or the server ended its transaction itself (a SQL Server
        trigger's COMMIT, 3609)."""
        self.ctx.stop_cancels()
        ended = error.code in TRANSACTION_ENDED.get(self.dialect, frozenset())
        if (sent and not table.transactional) or ended:
            self._rollback()
            said = [NOT_ROLLED_BACK.format(table.target_name)] if not ended else [
                f"A trigger ended the transaction of {table.target_name}: what it committed stays"]
            return _Table("unknown", error=error, warnings=said)
        if error.code == CONNECTION_LOST:  # the server rolls the open transaction back as the connection ends
            return _Table("rolled_back", error=error, warnings=self._created(table, created))
        return self._undone(table, error, created=created)

    def _undone(self, table: PlannedTable, error: StatementError, *, created: bool) -> _Table:
        warnings = self._created(table, created)
        if not self._rollback():
            warnings.append(JOB_ROLLBACK_FAILED)
        return _Table("rolled_back", error=error, warnings=warnings)

    @staticmethod
    def _created(table: PlannedTable, created: bool) -> list[str]:
        return [CREATED_EMPTY.format(table.target_name)] if created and table.create_first else []

    def _rollback(self) -> bool:
        """End the table's transaction; whether it is ended (a connection that is gone has none)."""
        self.ctx.stop_cancels()
        try:
            self.ctx.connection.rollback()
            return True
        except StatementError as exc:
            logger.warning("[copy] the rollback of a stopped table failed ({})", exc.code)
            try:
                return not self.ctx.connection.in_transaction()
            except StatementError:
                return True

    def _reset(self) -> None:
        """The session's settings back (it is closed after the job; a courtesy)."""
        self.ctx.stop_cancels()
        for sql in TARGET_RESET.get(self.dialect, ()):
            try:
                self.ctx.setting(sql)
            except StatementError as exc:
                logger.info("[copy] resetting the target session failed ({})", exc.code)
                return

    # ---- the outcome ----

    def _report(self, ended: list[_Table], *, setup_failed: bool = False) -> JobReport:
        ctx, plan = self.ctx, self.plan
        objects = []
        for table, done in zip(plan.tables, ended + [_Table("not_started")] * (len(plan.tables) - len(ended)),
                               strict=True):
            objects.append(JobObject(kind="table", name=table.target_name, source=table.source_name, rows=done.rows,
                                     outcome=done.outcome))
        outcomes = [item.outcome for item in objects]
        copied = outcomes.count("copied")
        overall: JobOutcomeKind
        if "unknown" in outcomes:
            overall = "unknown"
        elif copied == len(outcomes):
            overall = "completed"
        elif copied:
            overall = "partial"
        else:
            overall = "rolled_back"
        last = ended[-1] if ended else None
        error = last.error if last is not None else None
        cancelled = isinstance(error, StatementCancelled)
        warnings = [warning for done in ended for warning in done.warnings]
        left = [item.name for item in objects if item.outcome == "not_started"]
        if left and not setup_failed and len(left) < len(objects):
            warnings.append(NOT_STARTED.format(", ".join(left[:10]) + (" …" if len(left) > 10 else "")))
        rows = sum(item.rows for item in objects if item.outcome == "copied")
        outcome = ctx.outcome(overall, rows=rows, statements=self.statements, objects=objects, warnings=warnings,
                              cancelled=cancelled)
        created = any(table.created for table in plan.tables)
        return JobReport(outcome, error, schema_changed=created)
```

- [ ] **Step 5: The command reaches it.** Modify `backend/dbaguy_backend/sessions/hooks.py` and `backend/dbaguy_backend/container.py`:

```diff
diff --git a/backend/dbaguy_backend/sessions/hooks.py b/backend/dbaguy_backend/sessions/hooks.py
index a98d5fb..9db2f82 100644
--- a/backend/dbaguy_backend/sessions/hooks.py
+++ b/backend/dbaguy_backend/sessions/hooks.py
@@ -8,8 +8,9 @@ from uuid import UUID
 from ..adapters.base import StatementError
 from ..adapters.registry import AdapterRegistry
 from ..domain.data_jobs import BE_DUMPED, RUN_SQL_FILES, JobCommand, cannot
+from ..domain.transfer import COPY_DATA
 from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
-from ..domain.execution import DumpCommand, RunFileCommand
+from ..domain.execution import CopyDataCommand, DumpCommand, RunFileCommand
 from ..domain.history import HistoryRecord, HistoryStatus
 from ..domain.profiles import AnyProfile, Engine, SqliteProfile
 from ..services.edit_plans import EditPlanStore
@@ -17,9 +18,11 @@ from ..services.history_service import HistoryService
 from ..services.profile_service import ProfileService
 from ..services.safety import GuardDecision, SafetyPolicy
 from ..services.schema_cache import SchemaCache
+from ..services.transfer_plans import TransferPlanStore
 from .jobs.context import Job, JobHooks, PreparedJob
 from .jobs.dump import DumpJob
 from .jobs.run_file import RunFileJob
+from .jobs.transfer import TransferSink
 from .worker import ExecutionRecord, WorkerHooks
 
 READ_ONLY_RECHECK_S = 1.0  # a job's statement gate re-reads the profile at most this often (R-06-6)
@@ -51,7 +54,7 @@ class SessionHooksFactory:
 
     def __init__(
         self, registry: AdapterRegistry, profiles: ProfileService, policy: SafetyPolicy, history: HistoryService,
-        schema_cache: SchemaCache, plans: EditPlanStore | None = None,
+        schema_cache: SchemaCache, plans: EditPlanStore | None = None, transfers: TransferPlanStore | None = None,
     ) -> None:
         self._registry = registry
         self._profiles = profiles
@@ -59,6 +62,7 @@ class SessionHooksFactory:
         self._history = history
         self._schema_cache = schema_cache
         self._plans = plans
+        self._transfers = transfers
 
     def build(self, profile_id: UUID, database: str | None) -> WorkerHooks:
         profile = self._profiles.get(profile_id)
@@ -135,4 +139,19 @@ class SessionHooksFactory:
                 return DumpJob(command, profile.name, Engine(profile.engine), source_file=source)
             case DumpCommand():
                 raise StatementError(cannot(profile.engine, BE_DUMPED), code="not_supported")
+            case CopyDataCommand() if capabilities.copy_data and self._transfers is not None:
+                return self._copy(command, profile, database)
+            case CopyDataCommand():
+                raise StatementError(cannot(profile.engine, COPY_DATA), code="not_supported")
         raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")
+
+    def _copy(self, command: CopyDataCommand, profile: AnyProfile, database: str | None) -> Job:
+        """The previewed copy, taken once: by this profile's session and, where the engine connects per database, this
+        database's (D-06-8; `plan_expired`, `plan_mismatch` refuse it before anything is sent)."""
+        assert self._transfers is not None
+        try:
+            plan = self._transfers.take(command.plan_id, profile.id, database)
+        except PlanUnavailable as exc:
+            raise StatementError(exc.message, code=exc.code) from None
+        source = self._registry.get(self._profiles.get(plan.source_profile_id).engine)
+        return TransferSink(command, plan, source.dialect)
```

```diff
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index 8ee8d77..5265d81 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -91,6 +91,7 @@ class AppContainer:
         self.policy = SafetyPolicy()
         hooks = SessionHooksFactory(
             self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
+            self.transfers.plans,
         )
         self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
         self._stop = threading.Event()
```

- [ ] **Step 6: Run the tests.** `cd backend && .venv/bin/pytest -q tests/test_transfer_job.py tests/test_copy_framework.py` → all pass (dry run: 19 passed; about 2 s). Then the dump and the run, which share `InsertBatcher` and the job framework: `.venv/bin/pytest -q tests/test_dump_job.py tests/test_mysql_sqlite_dump_catalog.py tests/test_dump.py tests/test_run_file_job.py` → all pass (dry run: 227 passed, about a minute).

- [ ] **Step 7: Commit.**

```bash
git add backend/dbaguy_backend/sessions/jobs/transfer.py backend/dbaguy_backend/sessions/hooks.py \
  backend/dbaguy_backend/container.py backend/dbaguy_backend/sql/bulk_insert.py backend/tests/test_transfer_job.py
git commit -m "feat(06c): the copy job, table by table, with honest outcomes"
```

---

### Task 5: The 4×4 matrix (A9)

**Suggested implementer:** Sonnet (tests only; the expected values are each engine's own reading).

**Files:**
- Test: `backend/tests/copy_matrix.py`, `backend/tests/test_copy_matrix.py` (new)

**Interfaces:**
- Consumes: Tasks 1–4 through `DataApp`, `transfer_support`, `edit_support.scratch_db`.
- Produces: `tests/copy_matrix.py`: `TYPES` (a type-complete table per engine: PostgreSQL 24 columns, MySQL 23, SQL Server 23, SQLite 13; three rows each: one of edge values (int64 and uint64 bounds, 38-digit decimals, SQL Server's 7th fractional digit, offsets such as +05:30, emoji, quotes, backslashes and line breaks, binary with a zero byte, year-1 dates, JSON with `1.10`), one of NULLs and empty strings, one of the other bounds), `READ_AS` (how the check reads a column back where the driver's own value is not exact: PostgreSQL arrays and money as text and numeric; MySQL SET, BIT, JSON and FLOAT as text, a number and a DOUBLE), and `canon()` (a value as its meaning: the same instant in UTC, the same number, the same bytes, the same JSON); `test_copy_matrix.test_every_value_of_a_type_complete_table_reads_back_as_itself` over every source × target pair, same engine included (16): create mode, then every value read back through the target's own driver equals the source's by meaning.

A9 while planning: 16 of 16 pairs equal after one fix, found by the matrix and folded into Task 1 (PostgreSQL `timetz` into MySQL `TIME`: the offset dropped for the `duration` family too); the dry run also needed `({} * 1E0)` to read MySQL's FLOAT exactly in the check (06a M7's read).

- [ ] **Step 1: Write the matrix.** Create `backend/tests/copy_matrix.py` and `backend/tests/test_copy_matrix.py`:

```python
"""The copy matrix's tables (06c Task 5, spec ⑥ A9): one type-complete table per source engine, and how a value is
compared once copied — by what it means (its family), never by how each driver prints it: a decimal by its value, a
time zone's value by its instant (UTC where the target has no zone), a char by its text without the padding, a float32
as a float32. A value the copy changes on purpose (§3.5.3) compares as its conversion says."""

import json
import re
import struct
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

# Per source engine: the table (its {t}), its rows, and each column's family. Values the matrix copies everywhere: no
# PostgreSQL infinity, no MySQL zero date, no time past 24 hours (their own tests show their rows fail).
TYPES: dict[str, tuple[str, list[str], dict[str, str]]] = {
    "postgres": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b boolean, i2 smallint, i8 bigint, n numeric(38,10), nn numeric,
        m money, f4 real, f8 double precision, c char(5), vc varchar(10), t text, by bytea, d date, tm time(6),
        ts timestamp(6), tstz timestamptz, tmtz timetz, iv interval, u uuid, j json, jb jsonb, x xml, arr int[])""",
        ["""INSERT INTO {t} VALUES (1, true, -32768, 9223372036854775807, 1234567890123456789012345678.0123456789,
            1.10, 1234.56, 1.1, 0.1, 'ab', 'é😀''\\', 'line1
line2', '\\x00ff10'::bytea, '2026-10-05', '12:34:56.123456', '2026-10-05 12:34:56.123456',
            '2026-10-05 12:34:56.123456+08', '12:34:56+05:30', '1 day 02:03:04',
            'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '{{"b": 1.10, "a": [1, 2]}}', '{{"b": 1.10, "a": [1, 2]}}',
            '<a>x</a>', '{{1,2,NULL}}')""",
         "INSERT INTO {t} (id, vc, t) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, false, 32767, -9223372036854775808, -0.0000000001, 0, -0.01, -3.4e38,
            1e-300, '', 'x', 'é', ''::bytea, '0001-01-01', '00:00:00', '1999-12-31 23:59:59', '1970-01-01 00:00:00-05:30',
            '23:59:59-08', '-1 days', '00000000-0000-0000-0000-000000000000', '[]', '{{}}', '<a>y</a>', '{{}}')"""],
        {"id": "int32", "b": "bool", "i2": "int16", "i8": "int64", "n": "decimal", "nn": "decimal", "m": "money",
         "f4": "float32", "f8": "float64", "c": "char", "vc": "varchar", "t": "text", "by": "binary", "d": "date",
         "tm": "time", "ts": "timestamp", "tstz": "timestamp_tz", "tmtz": "time_tz", "iv": "interval", "u": "uuid",
         "j": "json", "jb": "json", "x": "text", "arr": "other"},
    ),
    "mysql": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b tinyint(1), ti tinyint, i8 bigint, u8 bigint unsigned,
        n decimal(30,10), f4 float, f8 double, c char(5), vc varchar(10), t text, bn binary(4), vb varbinary(10),
        bl blob, d date, tm time(6), y year, dt datetime(6), ts timestamp(6) NULL, j json, e enum('a','b'),
        s set('x','y'), bt bit(4))""",
        ["""INSERT INTO {t} VALUES (1, 1, -128, 9223372036854775807, 18446744073709551615,
            12345678901234567890.0123456789, 0.1234567, 0.1, 'ab', 'é😀''\\\\', 'line1\\nline2', X'00ff', X'00ff',
            X'0001', '2026-10-05', '12:34:56.123456', 2026, '2026-10-05 12:34:56.123456', '2026-10-05 12:34:56.123456',
            '{{"b": 1.10, "a": [1, 2]}}', 'b', 'x,y', b'1010')""",
         "INSERT INTO {t} (id, vc, t) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, 0, 127, -9223372036854775808, 0, -0.0000000001, -1.5, 1e-300, '', 'x', 'é',
            X'', X'', X'', '1000-01-01', '00:00:00', 1901, '1999-12-31 23:59:59', '1970-01-02 00:00:00', '[]', 'a', '',
            b'0')"""],
        {"id": "int32", "b": "bool", "ti": "int16", "i8": "int64", "u8": "uint64", "n": "decimal", "f4": "float32",
         "f8": "float64", "c": "char", "vc": "varchar", "t": "text", "bn": "binary", "vb": "binary", "bl": "binary",
         "d": "date", "tm": "duration", "y": "year", "dt": "timestamp", "ts": "timestamp", "j": "json", "e": "text",
         "s": "text", "bt": "int64"},
    ),
    "mssql": (
        """CREATE TABLE {t} (id int PRIMARY KEY, b bit, ti tinyint, i8 bigint, n decimal(38,10), m money,
        f4 real, f8 float, c char(5), nc nchar(5), vc varchar(10), nvc nvarchar(10), t nvarchar(max), bn binary(4),
        vb varbinary(10), d date, tm time(7), dt datetime, sdt smalldatetime, dt2 datetime2(7), dto datetimeoffset(7),
        u uniqueidentifier, x xml)""",
        ["""INSERT INTO {t} VALUES (1, 1, 255, 9223372036854775807, 1234567890123456789012345678.0123456789,
            1234.5678, 1.1, 0.1, 'ab', N'é😀', 'a''\\', N'é😀''\\', N'line1' + CHAR(10) + N'line2', 0x00ff, 0x00ff,
            '2026-10-05', '12:34:56.123456', '2026-10-05T12:34:56.123', '2026-10-05T12:34:00',
            '2026-10-05T12:34:56.123456', '2026-10-05T12:34:56.123456+08:00', 'A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11',
            N'<a>x</a>')""",
         "INSERT INTO {t} (id, vc, nvc, t) VALUES (2, '', N'', N'')",
         """INSERT INTO {t} VALUES (3, 0, 0, -9223372036854775808, -0.0000000001, -922337203685477.5808, -1.5,
            1e-300, '', N'', 'x', N'é', N'', 0x, 0x, '0001-01-01', '00:00:00', '1753-01-01T00:00:00',
            '1900-01-01T00:00:00', '0001-01-01T00:00:00', '1970-01-01T00:00:00-05:30',
            '00000000-0000-0000-0000-000000000000', N'<a/>')"""],
        {"id": "int32", "b": "bool", "ti": "int16", "i8": "int64", "n": "decimal", "m": "money", "f4": "float32",
         "f8": "float64", "c": "char", "nc": "char", "vc": "varchar", "nvc": "varchar", "t": "text", "bn": "binary",
         "vb": "binary", "d": "date", "tm": "time", "dt": "timestamp", "sdt": "timestamp", "dt2": "timestamp",
         "dto": "timestamp_tz", "u": "uuid", "x": "text"},
    ),
    "sqlite": (
        """CREATE TABLE {t} (id INTEGER PRIMARY KEY, i INTEGER, r REAL, tx TEXT, bl BLOB, nm NUMERIC, un,
        bo BOOLEAN, d DATE, dt DATETIME, j JSON, vc VARCHAR(10), dc DECIMAL(10,2))""",
        ["""INSERT INTO {t} VALUES (1, 9223372036854775807, 0.1, 'é😀''\\', X'00ff', 7, '1.10', 1, '2026-10-05',
            '2026-10-05 12:34:56', '{{"b": 1.10}}', 'abc', 12.30)""",
         "INSERT INTO {t} (id, tx, vc) VALUES (2, '', '')",
         """INSERT INTO {t} VALUES (3, -9223372036854775808, -1.5, 'x', X'', -2.5, 'text', 0, '1999-12-31',
            '1999-12-31 23:59:59', '[]', '', 0)"""],
        {"id": "int64", "i": "int64", "r": "float64", "tx": "text", "bl": "binary", "nm": "decimal", "un": "other",
         "bo": "bool", "d": "date", "dt": "timestamp", "j": "json", "vc": "varchar", "dc": "decimal"},
    ),
}
_TIME = re.compile(r"(-)?(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")
_INTERVAL = re.compile(r"(?:(-?\d+) days?)?\s*(?:(-)?(\d+):(\d{2}):(\d{2})(?:\.(\d+))?)?")
# How a test reads a column with its engine's own driver where the driver's own type would hide the value: an array as
# PostgreSQL's text, money as numeric (psycopg has no money loader), a set and a bit as MySQL's text and number, a MySQL
# FLOAT as a DOUBLE (its text protocol sends 6 digits: the copy reads it so too, 06a pre-flight M7)
READ_AS: dict[str, dict[str, str]] = {
    "postgres": {"arr": "{}::text", "m": "{}::numeric"},
    "mysql": {"s": "CAST({} AS CHAR)", "bt": "{} + 0", "j": "CAST({} AS CHAR)", "f4": "({} * 1E0)"},
}
_OFFSET = re.compile(r"([+-]\d{2})(?::?(\d{2}))?$")


def _micros(text: str) -> int:
    found = _TIME.fullmatch(text.strip())
    assert found is not None, text
    sign, hours, minutes, seconds, fraction = found.groups()
    micro = round(int((fraction or "0")[:7].ljust(7, "0")) / 10)
    total = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1_000_000 + micro
    return -total if sign else total


def _clock(value: Any) -> int:
    """A time of day or a duration as microseconds."""
    if isinstance(value, timedelta):
        return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    if isinstance(value, time):
        return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond
    text = str(value)
    return _micros(_OFFSET.sub("", text) if re.search(r"\d[+-]\d{2}(:?\d{2})?$", text) else text)


def _interval(value: Any) -> int:
    """PostgreSQL's interval (a timedelta, or its text `1 day 02:03:04`, `-1 days`) as microseconds."""
    if isinstance(value, timedelta):
        return _clock(value)
    found = _INTERVAL.fullmatch(str(value).strip())
    assert found is not None, value
    days, sign, hours, minutes, seconds, fraction = found.groups()
    clock = 0
    if hours is not None:
        clock = _micros(f"{hours}:{minutes}:{seconds}" + (f".{fraction}" if fraction else ""))
    return int(days or 0) * 86_400_000_000 + (-clock if sign else clock)


def _naive(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).replace("T", " ")
    head, _, fraction = text.partition(".")
    return datetime.fromisoformat(head + (f".{fraction[:6].ljust(6, '0')}" if fraction else ""))


def _instant(value: Any) -> datetime:
    """A time zone's value as its UTC instant; a naive one (MySQL's datetime, which the copy writes in UTC) is UTC."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).replace("T", " ")
    found = _OFFSET.search(text)
    if found is None:
        return _naive(text).replace(tzinfo=UTC)
    hours, minutes = int(found[1]), int(found[2] or 0) * (1 if found[1][0] == "+" else -1)
    local = _naive(text[:found.start()].strip())
    return (local - timedelta(hours=hours, minutes=minutes)).replace(tzinfo=UTC)


def canon(value: Any, family: str) -> Any:
    """What a value means, for comparing a source's value with its copy."""
    if value is None:
        return None
    match family:
        case "bool":
            return value.lower() in ("t", "true", "1") if isinstance(value, str) else bool(int(value))
        case "int16" | "int32" | "int64" | "uint64" | "year":
            return int.from_bytes(value, "big") if isinstance(value, bytes) else int(value)
        case "decimal" | "money":
            return Decimal(str(value)).normalize()
        case "float32":
            return struct.unpack("f", struct.pack("f", float(value)))[0]
        case "float64":
            return float(value)
        case "char":
            return str(value).rstrip(" ")
        case "binary":
            return bytes(value)
        case "date":
            return value.isoformat() if isinstance(value, date) else str(value)[:10]
        case "time" | "duration" | "time_tz":
            return _clock(value)
        case "interval":
            return _interval(value)
        case "timestamp":
            return _naive(value)
        case "timestamp_tz":
            return _instant(value)
        case "uuid":
            return str(value).lower() if not isinstance(value, UUID) else str(value)
        case "json":
            return value if isinstance(value, dict | list) else json.loads(value)
        case "other":
            return value.decode() if isinstance(value, bytes) else str(value)
        case _:
            return str(value)
```

```python
"""The copy matrix (06c Task 5, spec ⑥ A9): a type-complete table of each relational engine copied into a new table
on each of them (16 copies, the same engine included), and every value read back with the target's own driver equal to
the source's by what it means (`copy_matrix.canon`) — a time zone's value by its instant, UTC where the target has no
zone. Scratch tables `zz_06c_*`, dropped by exact name."""

from pathlib import Path

import pytest

from dbaguy_backend.domain.execution import CopyDataCommand
from tests.copy_matrix import READ_AS, TYPES, canon
from tests.data_support import DataApp, dropping
from tests.edit_support import ENGINES, ScratchDb, scratch_db
from tests.transfer_support import profile, table_name, tables_request


def _read(db: ScratchDb, table: str, columns: list[str]) -> list[tuple]:
    reads = READ_AS.get(db.engine, {})
    picked = ", ".join(reads[column].format(db.q(column)) if column in reads else db.q(column) for column in columns)
    return db.rows(f"SELECT {picked} FROM {db.table(table)} ORDER BY {db.q('id')}")


@pytest.mark.parametrize("target", ENGINES)
@pytest.mark.parametrize("source", ENGINES)
def test_every_value_of_a_type_complete_table_reads_back_as_itself(tmp_path: Path, source: str, target: str) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "t").mkdir()
    app = DataApp(tmp_path)
    src, dst = scratch_db(source, tmp_path / "s"), scratch_db(target, tmp_path / "t")
    try:
        with dropping(src) as src_made, dropping(dst, before_drop=app.close_sessions) as dst_made:
            ddl, rows, families = TYPES[source]
            name, copy = table_name("mx"), table_name("mxc")
            src.execute(ddl.replace("{t}", src.table(name)))
            src_made.append(("TABLE", name))
            for row in rows:
                src.execute(row.replace("{t}", src.table(name)).replace("{{", "{").replace("}}", "}"))
            into = profile(app, dst, "dst")
            plan = app.container.transfers.plan(tables_request(src, profile(app, src, "src"), [name], dst, into,
                                                               table=copy))
            assert plan.plan_id is not None, [p.message for t in plan.tables for p in t.problems]
            tab = app.job_session(into, dst.database)
            dst_made.append(("TABLE", copy))
            ran = tab.run(lambda rid: CopyDataCommand(request_id=rid, plan_id=plan.plan_id))
            assert (ran.outcome.outcome, ran.outcome.rows) == ("completed", len(rows)), ran.error
            columns = list(families)
            before, after = _read(src, name, columns), _read(dst, copy, columns)
        assert len(after) == len(before)
        for row_before, row_after in zip(before, after, strict=True):
            for column, was, now in zip(columns, row_before, row_after, strict=True):
                assert canon(now, families[column]) == canon(was, families[column]), (column, was, now)
    finally:
        src.close()
        dst.close()
        app.close()
```

- [ ] **Step 2: Run it.** `cd backend && .venv/bin/pytest -q tests/test_copy_matrix.py` → 16 passed (dry run: about 3 s). A failure names the pair, the column and both values: fix the rule in `type_map.py` (with a unit test in `test_type_map.py`), never the expected value, unless the engine itself cannot hold the source's value (then that row of `TYPES` is wrong, and the plan's preview must say so).

- [ ] **Step 3: Commit.**

```bash
git add backend/tests/copy_matrix.py backend/tests/test_copy_matrix.py
git commit -m "test(06c): A9 — every value of a type-complete table copies across the 4×4 matrix"
```

---

### Task 6: Copy in the frontend's pure modules — the form, the mapping, the third job kind

**Suggested implementer:** Opus (the again-actions and the words decide what a user does next; 06b's carry: decide them deliberately).

**Files:**
- Create: `src/features/data-tools/copyForm.ts`, `src/features/data-tools/mappingRows.ts`
- Modify: `src/features/data-tools/entryPoints.ts`, `src/features/data-tools/jobState.ts`, `src/features/data-tools/jobReport.ts`, `src/features/data-tools/jobsStore.ts`, `src/features/data-tools/dataDialogs.ts`, `src/features/data-tools/JobDialog.tsx`, `src/app/quitQuestion.ts`, `src/lib/api/types.ts`
- Test: `src/features/data-tools/copy.test.ts` (new); `src/features/data-tools/{entryPoints,jobReport,jobsStore,jobEndings}.test.ts`, `src/app/quitQuestion.test.ts`

**Interfaces:**
- Consumes: Tasks 2–4's API (`TransferRequest`, `TransferPlan`, `TableTransfer`, `ColumnMapping`, `CopyDataCommand`, `JobObject.outcome`/`source`, `JobInfo.source_profile_id`); 06b's `jobReport`, `jobsStore`, `dataDialogs`, `entryPoints` (`databaseOf`, `relationOf`, `schemaOf`, `relationHasRows`), `plural`.
- Produces:
  - `entryPoints.ts`: `CopySource` (`{kind: "relation", path, schema, name, relationKind, filter}` | `{kind: "tables", schema, folder}` | `{kind: "query", sessionId, sql, columns, title}`), `CopyEntry{profileId, database, source, tabId?}`, `copyEntryForNode(profileId, engine, node)` (a schema or its Tables folder: its tables; a table, view or materialized view: its rows; SQLite only `main`; never a database node, C10), `copyEntryForTable(profileId, engine, path, filter, tabId?)` (the tab's filter for a relation with rows), `copyEntryForResult(profileId, engine, database, sessionId, sql, columns, title)`, `paletteCopyEntry(tab, profile, engine, tableFilter)` (a table tab's table, else a query tab's default schema's tables).
  - `copyForm.ts`: `CopyMode`, `IdentityChoice`, `MAX_COPY_TABLES = 500`, `MODE_LABELS`, `MODE_HINTS`, `QUERY_TABLE = "query_result"`, `CopyForm{targetProfileId, targetDatabase, targetSchema, table, mode, onlyFiltered, checked, overrides, identity, leftOut}`, `initialCopyForm(entry, checked?)`, `defaultSchema(engine, database)`, `withTarget(form, profile, engine)`, `targetRefusal(profile)` (a read-only connection: "Read-only: nothing can be copied into it"), `requestProblem(entry, form, tables)`, `copyRequest(entry, form, tables) -> TransferRequest | null`, `copyProblem(entry, form, tables, plan, behind)` (Copy is enabled only for the preview of these very choices, read and without a problem), `copyCommand(plan, entry, requestId) -> CopyDataCommand` (a query's copy names its tab's session; a table's does not), `planTargets(plan)`, `copyDialogTitle(entry)`, `copyJobTitle(plan, from, to)`, `restForm(form, notCopied)`.
  - `mappingRows.ts`: `MappingRow`, `mappingRows(table, form) -> MappingRow[]`, `withOverride(form, table, column, text)`, `withIdentity(form, table, column, choice)`, `withLeftOut(form, table, column, out)` (each reads and writes names as own properties: a table or column named `constructor` or `__proto__` is a name, 05b K2), `statementLines(table)` (replace's `DELETE` in danger, M11), `previewProblems(plan)` (each problem once).
  - `jobState.ts`: `JobCommand` includes `CopyDataCommand`.
  - `jobReport.ts`: `JobView.copy?: {sources} | null`; `JobAction` gains `{kind: "copy_again", label, tables: readonly string[] | null}`; `TableLine{source, target, outcome, rows, tone}`, `JobReport.tables`; the copy's `CODE_LABELS` (`plan_expired`, `plan_mismatch`, `source_failed`, `row_count_mismatch`, `row_count_unknown`, `columns_changed`, `value_not_copyable`, `query_not_select`, `password_required`, `connection_failed`, `session_not_found`) and hints; the copy's words: completed ("Copied N rows to <table>" / "in N tables"), partial ("Stopped" or "Cancelled": "k of N tables copied"), rolled_back, not_started, unknown ("What was copied is not known", check the target); the again-actions (C17): nothing applied → "Copy again…" (the form as it was); stopped part way → "Copy the rest…" (only the tables not copied checked); copied or unknown → none; a lost connection's words for a copy; progress "Copying <table> (k of n)" / "Committing <table>"; Cancel's note "Cancel rolls back the table being copied; the tables before it stay" (none during a COMMIT); the notification's "Copy".
  - `jobsStore.ts`: `JobRecord.sourceProfileId`, `JobRecord.copy: {form, sources} | null`, `NewJob.sourceProfileId?`, `NewJob.copy?`; `reconcile` reads `JobInfo.source_profile_id`; `runningOn(snapshot, profileId)` (the running jobs a connection's deletion would stop: on its sessions, or reading from it: B13).
  - `dataDialogs.ts`: `DataDialogAsk` `{kind: "copy", entry, form, serial}`, `openCopyDialog(entry, form = null)`.
  - `JobDialog.tsx`: the `copy_again` action opens the copy's form (with `restForm` for "Copy the rest…"); `app/quitQuestion.ts`: the question says a copy rolls back the table it is copying.
  - `src/lib/api/types.ts`: `CopyDataCommand`, `JobObject`.

- [ ] **Step 1: Write the failing tests.** Create `src/features/data-tools/copy.test.ts`; add the copy's cases to the other tests:

```ts
import { describe, expect, it } from "vitest";
import type { ColumnMapping, EngineInfo, ObjectNode, PathSegment, Profile, TableTransfer, TransferPlan } from "@/lib/api/types";
import {
  copyCommand,
  copyDialogTitle,
  copyJobTitle,
  copyProblem,
  copyRequest,
  initialCopyForm,
  QUERY_TABLE,
  requestProblem,
  restForm,
  withTarget,
  type CopyForm,
} from "./copyForm";
import type { CopyEntry } from "./entryPoints";
import { mappingRows, previewProblems, statementLines, withIdentity, withLeftOut, withOverride } from "./mappingRows";

/* Copy data's form and its column mapping (spec ⑥ §3.6 CopyDataDialog, MappingTable; 06c Task 6). */

const seg = (kind: string, name: string): PathSegment => ({ kind, name });
const ordersPath = [seg("database", "shop"), seg("schema", "public"), seg("folder", "tables"), seg("table", "orders")];
const relation: CopyEntry = {
  profileId: "src",
  database: "shop",
  source: { kind: "relation", path: ordersPath, schema: "public", name: "orders", relationKind: "table", filter: "id > 5" },
};
const folder = [seg("database", "shop"), seg("schema", "public"), seg("folder", "tables")];
const tables: CopyEntry = { profileId: "src", database: "shop", source: { kind: "tables", schema: "public", folder } };
const query: CopyEntry = {
  profileId: "src",
  database: null,
  source: { kind: "query", sessionId: "tab-1", sql: "SELECT 1 AS a", columns: [{ name: "a", type_name: "int4", category: "number" }], title: "Query 1" },
};
const node = (name: string): ObjectNode => ({ kind: "table", name, path: [...folder, seg("table", name)], has_children: true, system: false });
const listed = [node("a"), node("b"), node("c")];
const mysql = { engine: "mysql", per_database_connection: false } as EngineInfo;
const pg = { engine: "postgres", per_database_connection: true } as EngineInfo;
const profile = (fields: Partial<Profile> = {}) => ({ id: "dst", name: "Dev", engine: "mysql", database: "app", read_only: false, ...fields }) as Profile;
const targeted = (entry: CopyEntry, fields: Partial<CopyForm> = {}): CopyForm => ({
  ...withTarget(initialCopyForm(entry), profile(), mysql),
  ...fields,
});

describe("the copy form", () => {
  it("starts with the source's own name, its filter on, and no target", () => {
    expect([initialCopyForm(relation).table, initialCopyForm(relation).onlyFiltered]).toEqual(["orders", true]);
    expect([initialCopyForm(query).table, initialCopyForm(tables).table, initialCopyForm(tables).checked]).toEqual([QUERY_TABLE, "", []]);
    expect(requestProblem(relation, initialCopyForm(relation), null)).toBe("Choose the connection to copy into");
  });

  it("takes a target's database and its default schema (MySQL: the database itself)", () => {
    expect(withTarget(initialCopyForm(relation), profile(), mysql)).toMatchObject({ targetDatabase: null, targetSchema: "app" });
    expect(withTarget(initialCopyForm(relation), profile({ engine: "postgres", database: "dw" }), pg)).toMatchObject({
      targetDatabase: "dw",
      targetSchema: "public",
    });
  });

  it("asks for a relation with its filter, a schema's checked tables in the tree's order, a query with its columns", () => {
    expect(copyRequest(relation, targeted(relation), null)).toEqual({
      source: { profile_id: "src", database: "shop", tables: [ordersPath], filter: "id > 5" },
      target: { profile_id: "dst", database: null, schema_name: "app", table: "orders", mode: "create", overrides: {}, identity: {}, left_out: {} },
    });
    expect(copyRequest(relation, targeted(relation, { onlyFiltered: false }), null)?.source.filter).toBeUndefined();
    const some = copyRequest(tables, targeted(tables, { checked: ["c", "a"] }), listed);
    expect([some?.source.tables, some?.target.table]).toEqual([[listed[0].path, listed[2].path], undefined]);
    expect(copyRequest(query, targeted(query), null)?.source.query?.columns).toEqual(query.source.kind === "query" ? query.source.columns : []);
  });

  it("says what is missing before it asks", () => {
    expect(requestProblem(tables, targeted(tables), null)).toBe("Reading the tables…");
    expect(requestProblem(tables, targeted(tables), listed)).toBe("Check the tables to copy");
    expect(requestProblem(relation, targeted(relation, { table: "  " }), null)).toBe("Name the table to copy into");
    expect(requestProblem(relation, targeted(relation, { targetSchema: null }), null)).toBe("Choose the schema to copy into");
  });

  it("copies only the preview shown, with a plan, and a query reads on its tab's session", () => {
    const plan = { plan_id: "p1", source_profile_id: "src", tables: [], warnings: [], problems: [] } as TransferPlan;
    expect(copyProblem(relation, targeted(relation), null, plan, true)).toBe("Reading the preview…");
    expect(copyProblem(relation, targeted(relation), null, { ...plan, plan_id: null }, false)).toBe("The preview has problems: see them above");
    expect(copyProblem(relation, targeted(relation), null, plan, false)).toBeNull();
    expect(copyCommand(plan, query, "r1")).toEqual({ type: "copy_data", request_id: "r1", plan_id: "p1", source_session_id: "tab-1" });
    expect(copyCommand(plan, relation, "r1").source_session_id).toBeNull();
  });

  it("names the dialog and the job by what is copied", () => {
    expect([copyDialogTitle(relation), copyDialogTitle(tables), copyDialogTitle(query)]).toEqual([
      "Copy public.orders",
      "Copy tables of public",
      "Copy the result of Query 1 to a table",
    ]);
    const plan = { tables: [{ source: "public.orders" }, { source: "public.items" }] } as TransferPlan;
    expect(copyJobTitle(plan, "Local PG", "Dev")).toBe("Copy 2 tables from Local PG to Dev");
    expect(restForm(targeted(tables, { checked: ["a", "b", "c"] }), ["c"]).checked).toEqual(["c"]);
  });
});

const col = (fields: Partial<ColumnMapping> & Pick<ColumnMapping, "source_name" | "source_type" | "family">): ColumnMapping => ({
  type_editable: false,
  overridden: false,
  ...fields,
});
const table: TableTransfer = {
  source: "public.orders",
  target: "orders",
  source_key: "public.orders",
  mode: "create",
  columns: [
    col({ source_name: "id", source_type: "bigint", family: "int64", target_name: "id", target_type: "bigint", type_editable: true }),
    col({ source_name: "placed", source_type: "timestamptz", family: "timestamp_tz", target_name: "placed", target_type: "datetime(6)", type_editable: true, conversion: "converted to UTC: the offset is not kept" }),
    col({ source_name: "g", source_type: "geography", family: "spatial", target_name: "g", type_editable: true, problem: "g (geography) has no MySQL type: choose one, or leave the column out" }),
    col({ source_name: "note", source_type: "text", family: "text", skipped: "left out" }),
    col({ source_name: "v", source_type: "sql_variant", family: "variant", skipped: "not copied: sql_variant column: only its raw bytes can be read" }),
  ],
  before: ["CREATE TABLE orders (…)", "DELETE FROM orders"],
  after: ["SELECT setval(…)"],
  warnings: [],
  problems: [{ code: "unmappable_type", message: "g (geography) has no MySQL type: choose one, or leave the column out", column: "g" }],
};

describe("the column mapping", () => {
  it("shows each column, what changes on the way, what blocks the copy and what can be left out", () => {
    const form = withLeftOut(targeted(relation), "public.orders", "note", true);
    const rows = mappingRows(table, withOverride(form, "public.orders", "id", "numeric(20)"));
    expect(rows.map((row) => [row.source, row.target, row.typed, row.leftOut, row.canLeaveOut])).toEqual([
      ["id", "id", "numeric(20)", false, true],
      ["placed", "placed", null, false, true],
      ["g", "g", null, false, true],
      ["note", null, null, true, true],
      ["v", null, null, false, false],
    ]);
    expect([rows[1].conversion, rows[2].problem]).toEqual(["converted to UTC: the offset is not kept", table.problems[0].message]);
  });

  it("keeps a typed type, an identity's choice and a column left out per table, clearing what goes back", () => {
    let form = withOverride(targeted(relation), "t", "a", "text");
    expect(form.overrides).toEqual({ t: { a: "text" } });
    expect(withOverride(form, "t", "a", " ").overrides).toEqual({});
    form = withIdentity(form, "t", "id", "generate");
    expect([form.identity, withIdentity(form, "t", "id", "keep").identity]).toEqual([{ t: { id: "generate" } }, {}]);
    form = withLeftOut(withLeftOut(form, "t", "x", true), "t", "y", true);
    expect([form.leftOut, withLeftOut(form, "t", "x", false).leftOut]).toEqual([{ t: ["x", "y"] }, { t: ["y"] }]);
  });

  it("reads a table or column named __proto__ or constructor as a name (05b K2)", () => {
    const form = withOverride(withOverride(targeted(relation), "__proto__", "constructor", "text"), "t", "__proto__", "int");
    expect(Object.hasOwn(form.overrides, "__proto__") && Object.hasOwn(form.overrides.t, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(form.overrides)).toBe(Object.prototype);
    const named = { ...table, source_key: "__proto__", columns: [{ ...table.columns[0], source_name: "constructor" }] };
    expect(mappingRows(named, form)[0].typed).toBe("text");
    expect(mappingRows({ ...table, source_key: "constructor" }, targeted(relation))[0].typed).toBeNull();
  });

  it("marks the DELETE of the rows a copy replaces as dangerous, and says each problem once", () => {
    expect(statementLines(table).map((line) => line.danger)).toEqual([false, true, false]);
    expect(previewProblems({ problems: [{ message: "x" }], tables: [table, table] })).toEqual(["x", table.problems[0].message]);
  });
});
```

```diff
diff --git a/src/features/data-tools/entryPoints.test.ts b/src/features/data-tools/entryPoints.test.ts
index 3abc61c..bc895ae 100644
--- a/src/features/data-tools/entryPoints.test.ts
+++ b/src/features/data-tools/entryPoints.test.ts
@@ -2,8 +2,12 @@ import { describe, expect, it } from "vitest";
 import type { EngineInfo, ObjectNode, PathSegment } from "@/lib/api/types";
 import type { ObjectTabState, QueryTabState, TableTabState } from "@/stores/workspace";
 import {
+  copyEntryForNode,
+  copyEntryForResult,
+  copyEntryForTable,
   dumpEntryForNode,
   dumpEntryForTable,
+  paletteCopyEntry,
   paletteDumpEntry,
   paletteRunEntry,
   runEntryForNode,
@@ -267,3 +271,54 @@ describe("a dump's file name", () => {
     expect([...name.split("_")[0]].length).toBe(66);
   });
 });
+
+describe("Copy data to… (06c)", () => {
+  const db = [seg("database", "dbaguy")];
+  const schema = [...db, seg("schema", "public")];
+  const folder = [...schema, seg("folder", "tables")];
+  const orders = [...folder, seg("table", "orders")];
+
+  it("copies a schema's tables, its Tables folder's, or one relation, on the database's own session", () => {
+    expect(copyEntryForNode(P, PG, node(schema))).toEqual({ profileId: P, database: "dbaguy", source: { kind: "tables", schema: "public", folder } });
+    expect(copyEntryForNode(P, PG, node(folder))?.source).toEqual({ kind: "tables", schema: "public", folder });
+    expect(copyEntryForNode(P, PG, node(orders))?.source).toEqual({
+      kind: "relation", path: orders, schema: "public", name: "orders", relationKind: "table", filter: null,
+    });
+    const view = [...schema, seg("folder", "views"), seg("view", "v")];
+    expect(copyEntryForNode(P, PG, node(view))?.source).toMatchObject({ kind: "relation", relationKind: "view" });
+  });
+
+  it("offers nothing on a database node, another SQLite schema, or an engine that does not copy", () => {
+    expect(copyEntryForNode(P, PG, node(db))).toBeNull();
+    expect(copyEntryForNode(P, SQLITE, node([seg("schema", "aux")]))).toBeNull();
+    expect(copyEntryForNode(P, SQLITE, node([seg("schema", "main")]))?.source).toMatchObject({ kind: "tables", schema: "main" });
+    expect(copyEntryForNode(P, COUCHBASE, node([seg("bucket", "b")]))).toBeNull();
+    expect(copyEntryForNode(P, engine("postgres", { copy_data: false }), node(orders))).toBeNull();
+  });
+
+  it("copies a table tab's rows with its filter, a view's without one, and a query's result on its session", () => {
+    expect(copyEntryForTable(P, PG, orders, " id > 5 ", "tab-1")).toMatchObject({ tabId: "tab-1", source: { filter: "id > 5" } });
+    const view = [...schema, seg("folder", "views"), seg("view", "v")];
+    expect(copyEntryForTable(P, PG, view, "x = 1")?.source).toMatchObject({ filter: null });
+    const columns = [{ name: "a", type_name: "int4", category: "number" as const }];
+    expect(copyEntryForResult(P, PG, "dbaguy", "s1", "SELECT 1 AS a", columns, "Q")).toEqual({
+      profileId: P, database: "dbaguy", source: { kind: "query", sessionId: "s1", sql: "SELECT 1 AS a", columns, title: "Q" },
+    });
+    expect(copyEntryForResult(P, PG, null, "s1", "SELECT", [], "Q")).toBeNull();
+  });
+
+  it("from the palette: the table tab's table, else the query tab's default schema's tables", () => {
+    const profile = { id: P, name: "Local PG", engine: "postgres", database: "dbaguy" } as never;
+    const tab = { id: "t2", kind: "table" as const, profileId: P, title: "t", path: orders };
+    expect(paletteCopyEntry(tab, profile, PG, "a > 1")?.source).toMatchObject({ kind: "relation", filter: "a > 1" });
+    const query = { id: "t1", kind: "query" as const, title: "Q", profileId: P, database: null, scriptPath: null,
+      savedText: null, textFromFile: false, initialText: "", autocommit: true };
+    expect(paletteCopyEntry(query, profile, PG, "")).toEqual({
+      profileId: P, database: "dbaguy", source: { kind: "tables", schema: "public", folder },
+    });
+    const mysqlProfile = { id: P, name: "My", engine: "mysql", database: "shop" } as never;
+    expect(paletteCopyEntry(query, mysqlProfile, MYSQL, "")?.source).toEqual({
+      kind: "tables", schema: "shop", folder: [seg("schema", "shop"), seg("folder", "tables")],
+    });
+  });
+});
```

```diff
diff --git a/src/features/data-tools/jobReport.test.ts b/src/features/data-tools/jobReport.test.ts
index 49de703..87f9591 100644
--- a/src/features/data-tools/jobReport.test.ts
+++ b/src/features/data-tools/jobReport.test.ts
@@ -1,5 +1,5 @@
 import { describe, expect, it } from "vitest";
-import type { DumpCommand, JobOutcomeEvent, JobProgressEvent, RunFileCommand } from "@/lib/api/types";
+import type { CopyDataCommand, DumpCommand, JobOutcomeEvent, JobProgressEvent, RunFileCommand } from "@/lib/api/types";
 import { sqliteBusyNote } from "@/lib/sqliteBusy";
 import { FILTER_RULE } from "../table-data/filterValue";
 import { cancelNote, CODE_LABELS, codeLabel, endingIsNews, etaText, jobNotification, jobReport, jobSummary, NO_RESUME_HINT, progressView, type JobView } from "./jobReport";
@@ -571,3 +571,51 @@ describe("whether a job's ending is news (its notification and its toast)", () =
     expect(endingIsNews(openingJob(0))).toBe(false);
   });
 });
+
+describe("a copy in words (06c)", () => {
+  const copy: CopyDataCommand = { type: "copy_data", request_id: "r1", plan_id: "p1", source_session_id: null };
+  const copied = (fields: Partial<JobOutcomeEvent> = {}) =>
+    outcome({ job: "copy_data", path: null, objects: [
+      { kind: "table", name: "a", source: "public.a", rows: 2, outcome: "copied" },
+      { kind: "table", name: "b", source: "public.b", rows: 0, outcome: "rolled_back" },
+      { kind: "table", name: "c", source: "public.c", rows: 0, outcome: "not_started" },
+    ], ...fields });
+  const said = (event: JobOutcomeEvent, command: CopyDataCommand | null = copy, sources: string[] | null = ["a", "b", "c"]) =>
+    jobReport({ kind: "copy_data", command, state: ended(event), copy: sources ? { sources } : null })!;
+
+  it("says what was copied, table by table", () => {
+    const one = said(outcome({ job: "copy_data", rows: 1200, objects: [{ kind: "table", name: "orders", source: "public.orders", rows: 1200, outcome: "copied" }] }));
+    expect([one.tone, one.title, one.actions]).toEqual(["success", "Copied 1,200 rows to orders", []]);
+    expect(one.tables).toEqual([{ source: "public.orders", target: "orders", outcome: "copied", rows: 1200, tone: "success" }]);
+    const partial = said(copied({ outcome: "partial", rows: 2 }));
+    expect([partial.tone, partial.title]).toEqual(["danger", "Stopped: 1 of 3 tables copied"]);
+    expect(partial.tables.map((line) => line.outcome)).toEqual(["copied", "rolled back", "not copied"]);
+  });
+
+  it("offers the rest of a copy that stopped part way, and the whole of one that applied nothing", () => {
+    expect(said(copied({ outcome: "partial" })).actions).toEqual([{ kind: "copy_again", label: "Copy the rest…", tables: ["b", "c"] }]);
+    expect(said(copied({ outcome: "partial" }), copy, null).actions).toEqual([]);  // listed after a reload: not known
+    expect(said(copied({ outcome: "rolled_back" })).actions).toEqual([{ kind: "copy_again", label: "Copy again…", tables: null }]);
+    expect(said(copied({ outcome: "unknown" })).actions).toEqual([]);  // check the target first
+    expect(said(copied({ outcome: "rolled_back" }), null).actions).toEqual([]);
+  });
+
+  it("labels a copy's own codes and says what a lost connection left", () => {
+    const failed = said(copied({ outcome: "rolled_back", error: { code: "value_not_copyable", message: "Row 2 of public.a, column at: …" } }));
+    expect([failed.error?.label, failed.hint]).toEqual([
+      CODE_LABELS.value_not_copyable,
+      "Leave that row out with a filter, or give the column a text type, then copy again.",
+    ]);
+    expect(codeLabel("source_failed")).toBe("Reading the source failed");
+    const lost = said(copied({ outcome: "partial", error: { code: "connection_lost", message: "gone" } }));
+    expect(lost.hint).toContain("tables before it stay copied");
+  });
+
+  it("shows which table is being copied, and what Cancel does", () => {
+    const progress: JobProgressEvent = { type: "job_progress", request_id: "r1", stage: "data", object: "b", rows: 1500, bytes: 0, statements: 2, errors: 0 };
+    const state = jobReducer(jobReducer(openingJob(0), { type: "sent", at: 1 }), { type: "server", event: progress, at: 2 });
+    const view = { kind: "copy_data" as const, command: copy, state };
+    expect(progressView(view, 2_000, ["a", "b", "c"]).headline).toBe("Copying b (2 of 3)");
+    expect(cancelNote(view)).toBe("Cancel rolls back the table being copied; the tables before it stay");
+  });
+});
```

```diff
diff --git a/src/features/data-tools/jobsStore.test.ts b/src/features/data-tools/jobsStore.test.ts
index 3a80a52..141d8de 100644
--- a/src/features/data-tools/jobsStore.test.ts
+++ b/src/features/data-tools/jobsStore.test.ts
@@ -16,6 +16,7 @@ import {
   retryJob,
   runningCount,
   runningJobs,
+  runningOn,
   setJobListing,
   setJobTransport,
   startJob,
@@ -36,8 +37,8 @@ const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
   profile_id: "00000000-0000-0000-0000-000000000001", started_at: "2026-10-23T01:00:00Z", ...fields,
 });
 const record = (fields: Partial<JobRecord> = {}): JobRecord => ({
-  requestId: "r1", sessionId: "s1", kind: "run_file", profileId: "p1", database: null, title: "t", command: null,
-  entry: null, tables: null, state: { ...openingJob(0), phase: "running", sent: true, sentAt: 0 }, ...fields,
+  requestId: "r1", sessionId: "s1", kind: "run_file", profileId: "p1", sourceProfileId: null, database: null, title: "t",
+  command: null, entry: null, tables: null, copy: null, state: { ...openingJob(0), phase: "running", sent: true, sentAt: 0 }, ...fields,
 });
 const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
 
@@ -292,3 +293,15 @@ describe("the Jobs button's counts", () => {
     expect([runningCount(some), hasEndedJob(some)]).toEqual([2, true]);
   });
 });
+
+describe("a copy's source connection (06c B13)", () => {
+  it("comes with a listed copy, and its deletion waits for the copy as the target's does", () => {
+    const source = "00000000-0000-0000-0000-000000000002";
+    const { added } = reconcile({}, [info({ job: "copy_data", source_profile_id: source })], 0);
+    expect(added[0].sourceProfileId).toBe(source);
+    const jobs = { r1: record({ kind: "copy_data", sourceProfileId: "src" }) };
+    expect(runningOn({ jobs, order: ["r1"] }, "src").map((job) => job.requestId)).toEqual(["r1"]);
+    expect(runningOn({ jobs, order: ["r1"] }, "p1")).toHaveLength(1);
+    expect(runningOn({ jobs, order: ["r1"] }, "other")).toHaveLength(0);
+  });
+});
```

```diff
diff --git a/src/features/data-tools/jobEndings.test.ts b/src/features/data-tools/jobEndings.test.ts
index 1434049..37a6d74 100644
--- a/src/features/data-tools/jobEndings.test.ts
+++ b/src/features/data-tools/jobEndings.test.ts
@@ -64,11 +64,13 @@ const record = (state: JobState, fields: Partial<JobRecord> = {}): JobRecord =>
   sessionId: "s1",
   kind: "dump",
   profileId: "p1",
+  sourceProfileId: null,
   database: null,
   title: "Dump public.big of dev pg to big.sql",
   command: dump,
   entry: null,
   tables: null,
+  copy: null,
   state,
   ...fields,
 });
```

```diff
diff --git a/src/app/quitQuestion.test.ts b/src/app/quitQuestion.test.ts
index 6d77d10..8cddcb1 100644
--- a/src/app/quitQuestion.test.ts
+++ b/src/app/quitQuestion.test.ts
@@ -14,8 +14,9 @@ describe("the quit question", () => {
     expect([asked.title, asked.keep, asked.confirm]).toEqual(["Quit DbaGuy?", "Keep running", "Quit anyway"]);
     expect(asked.message).toBe(
       "A job is still running: Dump public of Local PG to a.sql. Quitting stops it: a dump writes no file, a run " +
-        "in one transaction is rolled back, and a run statement by statement keeps what took effect (a transaction " +
-        "the file opened and did not commit is rolled back).",
+        "in one transaction is rolled back, a run statement by statement keeps what took effect (a transaction the " +
+        "file opened and did not commit is rolled back), and a copy rolls back the table it is copying (the tables " +
+        "before it stay).",
     );
   });
 
@@ -25,8 +26,9 @@ describe("the quit question", () => {
     expect([asked.title, asked.confirm]).toEqual(["Close the window?", "Close anyway"]);
     expect(asked.message).toBe(
       "5 jobs are still running: a; b; c, and 2 more. Closing the window quits DbaGuy, which stops them: a dump " +
-        "writes no file, a run in one transaction is rolled back, and a run statement by statement keeps what took " +
-        "effect (a transaction the file opened and did not commit is rolled back).",
+        "writes no file, a run in one transaction is rolled back, a run statement by statement keeps what took " +
+        "effect (a transaction the file opened and did not commit is rolled back), and a copy rolls back the table " +
+        "it is copying (the tables before it stay).",
     );
   });
 });
```

- [ ] **Step 2: Run them to see them fail.** `npm test -- src/features/data-tools src/app` → failures (`copyForm` and `mappingRows` do not exist; `copyEntryForNode`, `runningOn`, the copy's words are missing).

- [ ] **Step 3: The types and where a copy starts.**

```diff
diff --git a/src/lib/api/types.ts b/src/lib/api/types.ts
index c48d8fe..6af5d82 100644
--- a/src/lib/api/types.ts
+++ b/src/lib/api/types.ts
@@ -160,6 +160,10 @@ export type DumpPlanRequest = S["DumpPlanRequest"];
 export type StoppedAt = S["StoppedAt"];
 /** Run SQL file (06b B2): a file's lines around one line, for the error list. */
 export type FileExcerpt = S["FileExcerpt"];
+/** Copy data (spec ⑥ §3.5.5): run a previewed copy (its plan id, once) on the target's job session. */
+export type CopyDataCommand = S["CopyDataCommand"];
+/** Data tools: one object of a dump, or one table of a copy with its outcome (`copied`, `rolled_back`, …). */
+export type JobObject = S["JobObject"];
 /** Copy data (spec ⑥ §3.5.1, POST /data/transfer/plan): the source, the target and how. */
 export type TransferRequest = S["TransferRequest"];
 /** Copy data: tables (tree paths) of one connection, or a query tab's result; a filter for one table. */
```

```diff
diff --git a/src/features/data-tools/entryPoints.ts b/src/features/data-tools/entryPoints.ts
index 71360fe..971f777 100644
--- a/src/features/data-tools/entryPoints.ts
+++ b/src/features/data-tools/entryPoints.ts
@@ -1,12 +1,13 @@
-import type { EngineInfo, ObjectNode, PathSegment, Profile, QueryContext } from "@/lib/api/types";
+import type { ColumnMeta, EngineInfo, ObjectNode, PathSegment, Profile, QueryContext } from "@/lib/api/types";
 import type { Tab } from "@/stores/workspace";
 import { databaseOf, relationOf, schemaOf } from "../table-data/filterValue";
 import type { RunPatch } from "./jobReport";
 
 /*
- * Where Dump… and Run SQL file… start (spec ⑥ §3.6 入口, D-06-13): a tree node, the table-data tab, a script, the
- * palette. Each gives the dialog its connection, its database and what the node means (a dump's scope, a run's
- * database, search path or query context). Pure; the engine's capabilities decide whether there is an entry at all.
+ * Where Dump…, Run SQL file… and Copy data to… start (spec ⑥ §3.6 入口, D-06-13): a tree node, the table-data tab, a
+ * script, a query result, the palette. Each gives the dialog its connection, its database and what the node means (a
+ * dump's scope, a run's database, search path or query context, a copy's source). Pure; the engine's capabilities
+ * decide whether there is an entry at all.
  */
 
 /** What a dump covers, as the tree names it: its scope is built from it and the dialog's choices (`dumpForm.ts`). */
@@ -177,3 +178,118 @@ export function paletteRunEntry(tab: Tab | null, selectedProfileId: string | nul
   const context = fromTab?.kind === "query" ? (fromTab.queryContext === "none" ? null : fromTab.queryContext) : undefined;
   return { profileId: fromTab?.profileId ?? selectedProfileId, database, searchPath: null, queryContext: context, path: "" };
 }
+
+/** What a copy reads (spec ⑥ §3.5.1). */
+export type CopySource =
+  /** One table, view or materialized view (its rows), with the table-data tab's filter when it has one. */
+  | { kind: "relation"; path: PathSegment[]; schema: string; name: string; relationKind: string; filter: string | null }
+  /** A schema's tables (its Tables folder's): the dialog lists them, every one checked. */
+  | { kind: "tables"; schema: string; folder: PathSegment[] }
+  /** A query tab's result: its statement runs again on the tab's own session, as an export does (R-06-8). */
+  | { kind: "query"; sessionId: string; sql: string; columns: ColumnMeta[]; title: string };
+
+export interface CopyEntry {
+  /** The source's connection. */
+  profileId: string;
+  /** The source's database node (PostgreSQL, SQL Server); null: the profile's. */
+  database: string | null;
+  source: CopySource;
+  /** The table-data tab it was opened from: its staged changes are not copied (the server's rows are), said first. */
+  tabId?: string;
+}
+
+const tablesFolder = (path: PathSegment[]): PathSegment[] => [...path, { kind: "folder", name: "tables" }];
+
+/**
+ * Copy data to… on a tree node of an engine that copies (D-06-13): a schema or its Tables folder (its tables, chosen
+ * in the dialog), or one relation. Not a database node: its schemas' tables would meet in one target schema (06c C10).
+ */
+export function copyEntryForNode(profileId: string, engine: EngineInfo | null, node: ObjectNode): CopyEntry | null {
+  if (!engine?.copy_data) return null;
+  const database = databaseOf(node.path);
+  const entry = (source: CopySource): CopyEntry => ({ profileId, database, source });
+  switch (node.kind) {
+    case "schema":
+      return engine.engine === "sqlite" && node.name !== "main"
+        ? null
+        : entry({ kind: "tables", schema: node.name, folder: tablesFolder(node.path) });
+    case "folder": {
+      const schema = schemaOf(node.path);
+      if (node.name !== "tables" || !schema || node.path.at(-2)?.kind !== "schema") return null;
+      return engine.engine === "sqlite" && schema !== "main" ? null : entry({ kind: "tables", schema, folder: node.path });
+    }
+    default: {
+      const relation = relationOf(node.path);
+      if (!relation?.schema || (engine.engine === "sqlite" && relation.schema !== "main")) return null;
+      return entry({
+        kind: "relation", path: node.path, schema: relation.schema, name: relation.name, relationKind: relation.kind,
+        filter: null,
+      });
+    }
+  }
+}
+
+/** Copy rows to… in a table-data tab: its relation, with the filter it shows (empty: none). */
+export function copyEntryForTable(
+  profileId: string,
+  engine: EngineInfo | null,
+  path: PathSegment[],
+  filter: string,
+  tabId?: string,
+): CopyEntry | null {
+  const relation = relationOf(path);
+  if (!engine?.copy_data || !relation?.schema) return null;
+  if (engine.engine === "sqlite" && relation.schema !== "main") return null;
+  const kept = filter.trim();
+  return {
+    profileId,
+    database: relation.database,
+    source: {
+      kind: "relation", path, schema: relation.schema, name: relation.name, relationKind: relation.kind,
+      filter: kept && relationHasRows(relation.kind) ? kept : null,
+    },
+    ...(tabId ? { tabId } : {}),
+  };
+}
+
+/** Copy result to a table… under a query tab's result: its statement and the columns the tab has, on its session. */
+export function copyEntryForResult(
+  profileId: string,
+  engine: EngineInfo | null,
+  database: string | null,
+  sessionId: string,
+  sql: string,
+  columns: ColumnMeta[],
+  title: string,
+): CopyEntry | null {
+  if (!engine?.copy_data || columns.length === 0) return null;
+  return { profileId, database, source: { kind: "query", sessionId, sql, columns, title } };
+}
+
+/** The schema a query tab's palette copy lists, when its tab names none: the engine's default one. */
+const DEFAULT_SCHEMA: Partial<Record<EngineInfo["engine"], string>> = { postgres: "public", mssql: "dbo", sqlite: "main" };
+
+/**
+ * ⌘⇧P Copy data to…: the active table tab's table (with its filter), else the active query tab's schema's tables (its
+ * database's default schema; MySQL: its database), as Dump… does.
+ */
+export function paletteCopyEntry(
+  tab: Tab | null,
+  profile: Profile | null,
+  engine: EngineInfo | null,
+  tableFilter: string,
+): CopyEntry | null {
+  if (!tab || !profile || !engine?.copy_data || tab.profileId !== profile.id) return null;
+  if (tab.kind === "table") return copyEntryForTable(profile.id, engine, tab.path, tableFilter, tab.id);
+  if (tab.kind !== "query") return null;
+  const database = tab.database ?? ("database" in profile ? (profile.database ?? null) : null);
+  const schema = engine.engine === "mysql" ? database : (DEFAULT_SCHEMA[engine.engine] ?? null);
+  if (!schema) return null;
+  const head: PathSegment[] = engine.per_database_connection && database ? [{ kind: "database", name: database }] : [];
+  const folder = tablesFolder([...head, { kind: "schema", name: schema }]);
+  return {
+    profileId: profile.id,
+    database: engine.per_database_connection ? database : null,
+    source: { kind: "tables", schema, folder },
+  };
+}
```

- [ ] **Step 4: The form and the mapping.** Create `src/features/data-tools/copyForm.ts` and `src/features/data-tools/mappingRows.ts`:

```ts
import type {
  CopyDataCommand,
  EngineInfo,
  ObjectNode,
  Profile,
  TargetSpec,
  TransferPlan,
  TransferRequest,
} from "@/lib/api/types";
import { relationHasRows, type CopyEntry } from "./entryPoints";

/*
 * The copy dialog's form (spec ⑥ §3.6 CopyDataDialog, §3.5.1): where the rows go, how, and which columns change on the
 * way; the preview request it makes (POST /data/transfer/plan) and the command that runs that preview once (D-06-8: the
 * command carries the plan's id only). Pure.
 */

export type CopyMode = TargetSpec["mode"];
export type IdentityChoice = "keep" | "generate";

/** A copy holds at most this many tables (`MAX_TRANSFER_TABLES` in domain/transfer.py). */
export const MAX_COPY_TABLES = 500;

export const MODE_LABELS: Record<CopyMode, string> = {
  create: "Create a new table",
  append: "Append to an existing table",
  replace_rows: "Delete its rows, then append",
};
/** What each mode does, under it (R-06-9). */
export const MODE_HINTS: Record<CopyMode, string> = {
  create: "Its columns, NOT NULL and the primary key only: no defaults, indexes, foreign keys or identities",
  append: "Columns are matched by name; a key that is already there rolls the table back",
  replace_rows: "Every row of the target goes first (DELETE FROM), in the same transaction as the new ones",
};
/** The new table's name of a query's result, until the user names it. */
export const QUERY_TABLE = "query_result";

export interface CopyForm {
  targetProfileId: string | null;
  /** The target's database (PostgreSQL, SQL Server); null: the connection's own. */
  targetDatabase: string | null;
  /** The target's schema (MySQL: its database; SQLite: main); null until known. */
  targetSchema: string | null;
  /** One source: the target table's name. Several keep their own names. */
  table: string;
  mode: CopyMode;
  /** "Only rows matching the filter": the table-data tab's filter goes with the copy. */
  onlyFiltered: boolean;
  /** A schema's tables checked; null for one table or a query. */
  checked: readonly string[] | null;
  /** By the source table's key (`TableTransfer.source_key`), then the source column: a new column's type. */
  overrides: Readonly<Record<string, Readonly<Record<string, string>>>>;
  identity: Readonly<Record<string, Readonly<Record<string, IdentityChoice>>>>;
  leftOut: Readonly<Record<string, readonly string[]>>;
}

export function initialCopyForm(entry: CopyEntry, checked: readonly string[] | null = null): CopyForm {
  const { source } = entry;
  return {
    targetProfileId: null,
    targetDatabase: null,
    targetSchema: null,
    table: source.kind === "relation" ? source.name : source.kind === "query" ? QUERY_TABLE : "",
    mode: "create",
    onlyFiltered: source.kind === "relation" && source.filter !== null,
    checked: source.kind === "tables" ? (checked ?? []) : null,
    overrides: {},
    identity: {},
    leftOut: {},
  };
}

/** The schema a target connection starts with: PostgreSQL's public, SQL Server's dbo, SQLite's main, MySQL's database. */
export function defaultSchema(engine: EngineInfo["engine"], database: string | null): string | null {
  switch (engine) {
    case "postgres":
      return "public";
    case "mssql":
      return "dbo";
    case "sqlite":
      return "main";
    case "mysql":
      return database;
    default:
      return null;
  }
}

/** A new target connection: its own database, and that database's default schema. */
export function withTarget(form: CopyForm, profile: Profile, engine: EngineInfo): CopyForm {
  const database = "database" in profile ? (profile.database ?? null) : null;
  return {
    ...form,
    targetProfileId: profile.id,
    targetDatabase: engine.per_database_connection ? database : null,
    targetSchema: defaultSchema(engine.engine, database),
  };
}

/** Why a connection cannot be a copy's target, or null: a read-only one is refused before anything is sent (403). */
export const targetRefusal = (profile: Profile): string | null =>
  profile.read_only ? "Read-only: nothing can be copied into it" : null;

const isOne = (entry: CopyEntry) => entry.source.kind !== "tables";

/** The relations a schema's tables source copies: the checked ones, in the tree's order. */
function checkedPaths(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null) {
  if (entry.source.kind !== "tables") return null;
  const on = new Set(form.checked ?? []);
  return (tables ?? []).filter((node) => on.has(node.name)).map((node) => node.path);
}

/** Why the preview cannot be asked for yet, or null. */
export function requestProblem(entry: CopyEntry, form: CopyForm, tables: readonly ObjectNode[] | null): string | null {
  if (!form.targetProfileId) return "Choose the connection to copy into";
  if (!form.targetSchema) return "Choose the schema to copy into";
  if (isOne(entry) && !form.table.trim()) return "Name the table to copy into";
  if (entry.source.kind === "tables") {
    if (tables === null) return "Reading the tables…";
    const count = checkedPaths(entry, form, tables)?.length ?? 0;
    if (count === 0) return "Check the tables to copy";
    if (count > MAX_COPY_TABLES) return `A copy takes at most ${MAX_COPY_TABLES.toLocaleString()} tables`;
  }
  return null;
}

/** The preview's request, or null while it cannot be asked (`requestProblem`). The command's plan comes from it. */
export function copyRequest(
  entry: CopyEntry,
  form: CopyForm,
  tables: readonly ObjectNode[] | null,
): TransferRequest | null {
  if (requestProblem(entry, form, tables) !== null || !form.targetProfileId) return null;
  const { source } = entry;
  const filter = source.kind === "relation" && source.filter !== null && form.onlyFiltered ? source.filter : null;
  return {
    source: {
      profile_id: entry.profileId,
      database: entry.database,
      ...(source.kind === "query"
        ? { query: { sql: source.sql, columns: source.columns, title: source.title } }
        : { tables: source.kind === "relation" ? [source.path] : checkedPaths(entry, form, tables) }),
      ...(filter !== null ? { filter } : {}),
    },
    target: {
      profile_id: form.targetProfileId,
      database: form.targetDatabase,
      schema_name: form.targetSchema,
      ...(isOne(entry) ? { table: form.table.trim() } : {}),
      mode: form.mode,
      overrides: form.overrides as Record<string, Record<string, string>>,
      identity: form.identity as Record<string, Record<string, IdentityChoice>>,
      left_out: form.leftOut as Record<string, string[]>,
    },
  };
}

/**
 * Why Copy cannot be pressed, or null: the form is not complete, the preview shown is not this form's (being read,
 * or of the choices before), or it has problems (no plan id: ⑤ E8).
 */
export function copyProblem(
  entry: CopyEntry,
  form: CopyForm,
  tables: readonly ObjectNode[] | null,
  plan: TransferPlan | null,
  behind: boolean,
): string | null {
  const problem = requestProblem(entry, form, tables);
  if (problem) return problem;
  if (!plan || behind) return "Reading the preview…";
  if (!plan.plan_id) return "The preview has problems: see them above";
  return null;
}

/** The command that runs the preview shown (its plan, once). A query's copy reads on its tab's session. */
export function copyCommand(plan: TransferPlan, entry: CopyEntry, requestId: string): CopyDataCommand {
  return {
    type: "copy_data",
    request_id: requestId,
    plan_id: plan.plan_id ?? "",
    source_session_id: entry.source.kind === "query" ? entry.source.sessionId : null,
  };
}

/** The target tables in the order the copy runs (its progress: "table k of n"). */
export const planTargets = (plan: TransferPlan): string[] => plan.tables.map((table) => table.target);

/** The dialog's title: what is copied. */
export function copyDialogTitle(entry: CopyEntry): string {
  const { source } = entry;
  switch (source.kind) {
    case "relation":
      return `Copy ${relationHasRows(source.relationKind) ? "" : "the rows of "}${source.schema}.${source.name}`;
    case "tables":
      return `Copy tables of ${source.schema}`;
    default:
      return `Copy the result of ${source.title} to a table`;
  }
}

/** The job's title, as the Jobs list shows it: `Copy public.orders from Local PG to Dev MySQL`. */
export function copyJobTitle(plan: TransferPlan, from: string, to: string): string {
  const what = plan.tables.length === 1 ? plan.tables[0].source : `${plan.tables.length.toLocaleString()} tables`;
  return `Copy ${what} from ${from} to ${to}`;
}

/**
 * "Copy the rest…" after a copy that stopped part way: the form as it was, with only the tables not copied checked
 * (their source names: the schema's tables are listed by name), so the ones copied are not copied twice.
 */
export function restForm(form: CopyForm, notCopied: readonly string[]): CopyForm {
  return { ...form, checked: notCopied };
}
```

```ts
import type { ColumnMapping, TableTransfer } from "@/lib/api/types";
import type { CopyForm, IdentityChoice } from "./copyForm";

/*
 * A copy's column mapping as the dialog's table shows it (spec ⑥ §3.6 MappingTable, §3.5.1): one row per source column
 * — where it goes, its type (a new table's may be typed over), how its values change on the way (⚠), why it is not
 * copied, and the problem that blocks the copy — and what each control changes in the form (a type, an identity's
 * choice, a column left out). Every word comes from the preview (`TableTransfer`); nothing here decides a mapping. Pure.
 */

export interface MappingRow {
  source: string;
  sourceType: string;
  /** Where it goes; null when it is not copied (`skipped` says why). */
  target: string | null;
  targetType: string | null;
  /** A new table's column: its type can be typed over (the form's override, shown in its field). */
  typeEditable: boolean;
  /** What the form asks for it (the field's text), when it overrides the mapped type. */
  typed: string | null;
  conversion: string | null;
  skipped: string | null;
  /** Appending into an identity column: keep the source's values or let the target make them; null otherwise. */
  identity: IdentityChoice | null;
  problem: string | null;
  /** The user left it out: it can be put back. */
  leftOut: boolean;
  /** It can be left out (it is copied now, or left out by the user). */
  canLeaveOut: boolean;
}

const LEFT_OUT = "left out";

/** A key's own value: a table or column named `constructor` or `__proto__` is a name like any other (05b K2). */
const own = <T>(record: Readonly<Record<string, T>>, key: string): T | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

/** `record` with `key` set to `value` (undefined: without it), as own data properties only. */
function withKey<T>(record: Readonly<Record<string, T>>, key: string, value: T | undefined): Record<string, T> {
  const kept: [string, T][] = Object.entries(record).filter(([name]) => name !== key);
  return Object.fromEntries(value === undefined ? kept : [...kept, [key, value]]);
}

export function mappingRows(table: TableTransfer, form: CopyForm): MappingRow[] {
  const typed = own(form.overrides, table.source_key) ?? {};
  const out = new Set(own(form.leftOut, table.source_key) ?? []);
  return table.columns.map((column: ColumnMapping) => {
    const leftOut = out.has(column.source_name) && column.skipped === LEFT_OUT;
    return {
      source: column.source_name,
      sourceType: column.source_type,
      target: column.target_name ?? null,
      targetType: column.target_type ?? null,
      typeEditable: column.type_editable ?? false,
      typed: own(typed, column.source_name) ?? null,
      conversion: column.conversion ?? null,
      skipped: column.skipped ?? null,
      identity: column.identity ?? null,
      problem: column.problem ?? null,
      leftOut,
      canLeaveOut: leftOut || (!column.skipped && column.target_name != null),
    };
  });
}

/** The form with a new column's type typed over (empty text: the mapped type again). */
export function withOverride(form: CopyForm, table: string, column: string, text: string): CopyForm {
  const columns = withKey(own(form.overrides, table) ?? {}, column, text.trim() ? text : undefined);
  return { ...form, overrides: withKey(form.overrides, table, Object.keys(columns).length ? columns : undefined) };
}

/** The form with an identity column's choice (keep is the default: it is not sent). */
export function withIdentity(form: CopyForm, table: string, column: string, choice: IdentityChoice): CopyForm {
  const columns = withKey(own(form.identity, table) ?? {}, column, choice === "keep" ? undefined : choice);
  return { ...form, identity: withKey(form.identity, table, Object.keys(columns).length ? columns : undefined) };
}

/** The form with a column left out of the copy, or put back. */
export function withLeftOut(form: CopyForm, table: string, column: string, out: boolean): CopyForm {
  const kept = (own(form.leftOut, table) ?? []).filter((name) => name !== column);
  const columns = out ? [...kept, column] : kept;
  return { ...form, leftOut: withKey(form.leftOut, table, columns.length ? columns : undefined) };
}

/** The statements a table adds, each with whether it destroys rows (replace's DELETE: danger, M11). */
export function statementLines(table: TableTransfer): { sql: string; danger: boolean }[] {
  return [...table.before, ...table.after].map((sql) => ({ sql, danger: /^\s*DELETE\b/i.test(sql) }));
}

/** Every problem of the preview, the copy's first: what blocks Copy, said once each. */
export function previewProblems(plan: { problems: { message: string }[]; tables: TableTransfer[] }): string[] {
  return [...new Set([...plan.problems, ...plan.tables.flatMap((table) => table.problems)].map((p) => p.message))];
}
```

- [ ] **Step 5: The third job kind.**

```diff
diff --git a/src/features/data-tools/jobState.ts b/src/features/data-tools/jobState.ts
index e80d3d4..3acf19f 100644
--- a/src/features/data-tools/jobState.ts
+++ b/src/features/data-tools/jobState.ts
@@ -1,4 +1,5 @@
 import type {
+  CopyDataCommand,
   DumpCommand,
   JobInfo,
   JobOutcomeEvent,
@@ -16,7 +17,7 @@ import type {
  */
 
 export type JobKind = JobOutcomeEvent["job"];
-export type JobCommand = DumpCommand | RunFileCommand;
+export type JobCommand = DumpCommand | RunFileCommand | CopyDataCommand;
 
 /** How a job ended without the backend's outcome: only what the frontend knows. */
 export type JobLoss =
```

```diff
diff --git a/src/features/data-tools/jobReport.ts b/src/features/data-tools/jobReport.ts
index 2c9cfac..eec6761 100644
--- a/src/features/data-tools/jobReport.ts
+++ b/src/features/data-tools/jobReport.ts
@@ -1,5 +1,6 @@
 import type {
   JobError,
+  JobObject,
   JobOutcomeEvent,
   JobOutcomeKind,
   JobProgressEvent,
@@ -14,8 +15,8 @@ import type { JobCommand, JobKind, JobState } from "./jobState";
 /*
  * What a job's dialog and the Jobs list say about it (spec ⑥ §3.6, §4 "結果的說明由前端依 job_outcome 組成"): its
  * progress while it runs and, once it ended, its outcome in words, the error with a label and what to do, the skipped
- * errors and the actions that make sense now. Pure. Every action that would run something again goes back to the form,
- * prefilled, so a run is always started (and, on a red connection, confirmed) from the form (ruling B6).
+ * errors, a copy's tables, and the actions that make sense now. Pure. Every action that would run something again goes
+ * back to the form, prefilled, so a job is always started (and, on a red connection, confirmed) from the form (B6).
  */
 
 export interface JobView {
@@ -23,6 +24,8 @@ export interface JobView {
   /** What was sent; null for a job listed after a reload (it was started by another page load). */
   command: JobCommand | null;
   state: JobState;
+  /** A copy started here: its source tables' names in the copy's order, for "Copy the rest…" (06c; a JobRecord's). */
+  copy?: { sources: readonly string[] } | null;
 }
 
 export type Tone = "success" | "warning" | "danger" | "muted";
@@ -42,6 +45,11 @@ export interface RunPatch {
 
 export type JobAction =
   | { kind: "show_in_finder"; label: string; path: string }
+  /**
+   * A copy back to its form with its own choices (06c): `tables` null as it was; else only these source tables
+   * checked ("Copy the rest…": the ones not copied, so none is copied twice).
+   */
+  | { kind: "copy_again"; label: string; tables: readonly string[] | null }
   /**
    * Back to the form with the job's own settings, changed by `patch` (null: as they were); `note`: said above the
    * form's file (a run again after `file_changed`, whose start is the refused statement's number).
@@ -56,6 +64,15 @@ export interface ErrorLine {
   message: string;
 }
 
+/** One table of a copy and how it ended (06c, R-06-12). */
+export interface TableLine {
+  source: string | null;
+  target: string;
+  outcome: string;
+  rows: number;
+  tone: Tone;
+}
+
 export interface JobReport {
   tone: Tone;
   title: string;
@@ -70,6 +87,8 @@ export interface JobReport {
   /** The errors a run skipped (the first ones), and how many more there were. */
   skipped: ErrorLine[];
   moreSkipped: number;
+  /** A copy's tables, in its order, each with how it ended (empty for a dump or a run). */
+  tables: TableLine[];
   actions: JobAction[];
 }
 
@@ -106,6 +125,18 @@ export const CODE_LABELS = {
   transactions_unsupported: "Couchbase runs no transactions",
   protocol_error: "An answer DbaGuy cannot read",
   tls_failed: "TLS failed",
+  // Copy data (06c, spec §4)
+  plan_expired: "The preview expired",
+  plan_mismatch: "Not this connection's preview",
+  source_failed: "Reading the source failed",
+  row_count_mismatch: "A trigger skipped or added rows",
+  row_count_unknown: "The rows written were not confirmed",
+  columns_changed: "The columns changed since the preview",
+  value_not_copyable: "A value the target cannot hold",
+  query_not_select: "Not a query that only reads",
+  password_required: "The source connection needs its password",
+  connection_failed: "The source connection could not be opened",
+  session_not_found: "The query's tab was closed",
 } as const;
 export type JobErrorCode = keyof typeof CODE_LABELS;
 
@@ -117,6 +148,10 @@ const CODE_HINTS: Partial<Record<JobErrorCode, string>> = {
   invalid_filter: FILTER_RULE,
   file_changed:
     "The file was edited after the run that gave this place, so its statement numbers are not the ones they were. Look at the file, then set where to start again.",
+  plan_expired: "Copy again: the preview is read once more.",
+  value_not_copyable: "Leave that row out with a filter, or give the column a text type, then copy again.",
+  row_count_mismatch: "A trigger on the target changed what was written: copy into a table without it.",
+  session_not_found: "Copy again from the result of a tab that is open.",
 };
 /** Codes after which "Skip it and continue" cannot help: the file cannot be read past them, or nothing can run. */
 const NOT_SKIPPABLE: ReadonlySet<string> = new Set([
@@ -151,6 +186,11 @@ export function codeLabel(code: string | null | undefined): string | null {
 
 /** What a lost connection means for what to do next: a run that is unknown must be checked, a dump wrote nothing. */
 function connectionLostHint(kind: JobKind, outcome: JobOutcomeKind): string | null {
+  if (kind === "copy_data") {
+    return outcome === "unknown"
+      ? "Check the target before copying again."
+      : "The table being copied was rolled back by the server as the connection ended; tables before it stay copied.";
+  }
   if (kind === "run_file") {
     return outcome === "unknown"
       ? // the detail says what is known (a line that may or may not have run, or a transaction the server rolled back)
@@ -163,7 +203,9 @@ function connectionLostHint(kind: JobKind, outcome: JobOutcomeKind): string | nu
 }
 
 function codeHint(code: string | null, kind: JobKind, outcome: JobOutcomeKind): string | null {
-  if (isSqliteBusy(code)) return sqliteBusyNote(kind === "dump" ? "dump again" : "run the file again");
+  if (isSqliteBusy(code)) {
+    return sqliteBusyNote(kind === "dump" ? "dump again" : kind === "copy_data" ? "copy again" : "run the file again");
+  }
   if (code === "connection_lost") return connectionLostHint(kind, outcome);
   return code && has(CODE_HINTS, code) ? (CODE_HINTS[code] ?? null) : null;
 }
@@ -189,7 +231,9 @@ function endingError(outcome: JobOutcomeEvent | null, state: JobState): ErrorLin
  * a dump's, a lost run's or any other).
  */
 function againAction(kind: JobKind, command: JobCommand | null): JobAction[] {
-  return command ? [{ kind: "again", label: kind === "dump" ? "Dump again…" : "Run again…", patch: null }] : [];
+  if (!command) return [];
+  if (kind === "copy_data") return [{ kind: "copy_again", label: "Copy again…", tables: null }];
+  return [{ kind: "again", label: kind === "dump" ? "Dump again…" : "Run again…", patch: null }];
 }
 
 const NOTHING_WRITTEN_NOTE = "A file that was already at that path is as it was.";
@@ -200,7 +244,7 @@ const NOT_KNOWN_NOTE = "What was applied is not known: check the target.";
  */
 export const NO_RESUME_HINT = "Check the target, then choose where to start in a new run (Run SQL file… from the tree).";
 
-type Words = Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint">;
+type Words = Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint" | "tables">;
 
 function dumpReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Words {
   const again = againAction("dump", command);
@@ -244,6 +288,71 @@ function dumpReport(outcome: JobOutcomeEvent, command: JobCommand | null, error:
   }
 }
 
+const TABLE_WORDS: Record<NonNullable<JobObject["outcome"]>, { text: string; tone: Tone }> = {
+  copied: { text: "copied", tone: "success" },
+  rolled_back: { text: "rolled back", tone: "danger" },
+  not_started: { text: "not copied", tone: "muted" },
+  unknown: { text: "unknown: check it", tone: "danger" },
+};
+
+/** A copy's tables as the view lists them (R-06-12: each table's own outcome). */
+function tableLines(objects: readonly JobObject[]): TableLine[] {
+  return objects.map((object) => {
+    const words = TABLE_WORDS[object.outcome ?? "unknown"];
+    return { source: object.source ?? null, target: object.name, outcome: words.text, rows: object.rows ?? 0, tone: words.tone };
+  });
+}
+
+/**
+ * What a copy offers to do again (06c): nothing applied (rolled back, not started) — the form as it was; stopped part
+ * way — the tables not copied only ("Copy the rest…", so none is copied twice); copied, or not known (check the target
+ * first) — nothing.
+ */
+function copyActions(outcome: JobOutcomeEvent, command: JobCommand | null, sources: readonly string[] | null): JobAction[] {
+  if (outcome.outcome === "rolled_back" || outcome.outcome === "not_started") return againAction("copy_data", command);
+  if (outcome.outcome !== "partial" || !command || !sources || sources.length !== outcome.objects.length) return [];
+  const rest = sources.filter((_, i) => outcome.objects[i].outcome !== "copied");
+  return rest.length ? [{ kind: "copy_again", label: "Copy the rest…", tables: rest }] : [];
+}
+
+function copyReport(outcome: JobOutcomeEvent, command: JobCommand | null, sources: readonly string[] | null): Words {
+  const notes = outcome.warnings;
+  const tables = outcome.objects;
+  const copied = tables.filter((table) => table.outcome === "copied").length;
+  const actions = copyActions(outcome, command, sources);
+  const into = tables.length === 1 ? ` to ${tables[0].name}` : ` in ${plural(tables.length, "table")}`;
+  switch (outcome.outcome) {
+    case "completed":
+      return { tone: notes.length ? "warning" : "success", title: `Copied ${plural(outcome.rows, "row")}${into}`, detail: null, notes, actions };
+    case "partial":
+      return {
+        tone: outcome.cancelled ? "warning" : "danger",
+        title: `${outcome.cancelled ? "Cancelled" : "Stopped"}: ${copied.toLocaleString()} of ${plural(tables.length, "table")} copied`,
+        detail: "The tables copied stay. The one being copied was rolled back; the rest were not started.",
+        notes,
+        actions,
+      };
+    case "rolled_back":
+      return {
+        tone: outcome.cancelled ? "muted" : "danger",
+        title: outcome.cancelled ? "Cancelled: nothing was copied" : "Nothing was copied: the table was rolled back",
+        detail: null,
+        notes,
+        actions,
+      };
+    case "not_started":
+      return {
+        tone: outcome.cancelled ? "muted" : "danger",
+        title: outcome.cancelled ? "Cancelled before anything was copied" : "The copy did not start",
+        detail: "Nothing was copied.",
+        notes,
+        actions,
+      };
+    default: // unknown
+      return { tone: "danger", title: "What was copied is not known", detail: NOT_KNOWN_NOTE, notes, actions };
+  }
+}
+
 /** What a run offers to do again: from where it can start without skipping or repeating a statement that took effect. */
 function runActions(outcome: JobOutcomeEvent, command: JobCommand | null, code: string | null): JobAction[] {
   const run = runCommand(command);
@@ -409,7 +518,7 @@ const LOSS_TITLES: Partial<Record<NonNullable<JobState["loss"]>, string>> = {
 /** The frontend's own ending (no outcome from the backend): what is known, and nothing more. */
 function lossReport(view: JobView): JobReport {
   const { state, kind } = view;
-  const base = { error: null, hint: null, notes: [], skipped: [], moreSkipped: 0 };
+  const base = { error: null, hint: null, notes: [], skipped: [], moreSkipped: 0, tables: [] };
   const canAgain = againAction(kind, view.command);
   switch (state.loss) {
     case "open_failed":
@@ -433,6 +542,18 @@ function lossReport(view: JobView): JobReport {
           actions: canAgain,
         };
       }
+      if (kind === "copy_data") {
+        return {
+          ...base,
+          tone: "danger",
+          title,
+          // each table is a transaction of its own: the server rolled back the one open, except during its COMMIT
+          detail: state.progress?.stage === "commit"
+            ? "It stopped during a table's COMMIT: whether that table is copied is not known. Check the target."
+            : "The table being copied was rolled back by the server as its connection ended; the tables before it stay copied. Check the target.",
+          actions: [],
+        };
+      }
       const run = runCommand(view.command);
       const statements = state.progress?.statements ?? 0;
       const detail =
@@ -460,7 +581,12 @@ export function jobReport(view: JobView): JobReport | null {
   const outcome = state.outcome;
   if (!outcome) return lossReport(view);
   const error = endingError(outcome, state);
-  const words = kind === "dump" ? dumpReport(outcome, command, error) : runReport(outcome, command, error);
+  const words =
+    kind === "dump"
+      ? dumpReport(outcome, command, error)
+      : kind === "copy_data"
+        ? copyReport(outcome, command, view.copy?.sources ?? null)
+        : runReport(outcome, command, error);
   const skipped = outcome.errors.map(toErrorLine);
   const hints = [codeHint(error?.code ?? null, kind, outcome.outcome), stoppedUnplaced(kind, outcome) ? NO_RESUME_HINT : null];
   return {
@@ -469,6 +595,7 @@ export function jobReport(view: JobView): JobReport | null {
     hint: hints.filter((hint) => hint !== null).join(" ") || null,
     skipped,
     moreSkipped: Math.max(0, outcome.error_count - skipped.length),
+    tables: kind === "copy_data" ? tableLines(outcome.objects) : [],
   };
 }
 
@@ -505,6 +632,13 @@ export function progressView(view: JobView, now: number, tables: readonly string
   if (state.phase === "opening") return { headline: "Connecting…", detail: null, percent: null, eta: null };
   const progress = state.progress;
   if (!progress) return { headline: "Starting…", detail: elapsed, percent: null, eta: null };
+  if (kind === "copy_data") {
+    const object = progress.object ?? null;
+    const index = object && tables ? tables.indexOf(object) : -1;
+    const which = index >= 0 && tables && tables.length > 1 ? ` (${index + 1} of ${tables.length})` : "";
+    const headline = !object ? "Starting…" : progress.stage === "commit" ? `Committing ${object}${which}` : `Copying ${object}${which}`;
+    return { headline, detail: `${plural(progress.rows, "row")} copied · ${elapsed}`, percent: null, eta: null };
+  }
   if (kind === "dump") {
     let headline = DUMP_STAGES[progress.stage] ?? "Writing the rows";
     if (progress.stage === "data" && progress.object) {
@@ -544,6 +678,9 @@ export function cancelNote(view: JobView): string | null {
   const { command, state } = view;
   if (!command || state.phase === "ended") return null;
   if (command.type === "dump") return "Cancel writes no file";
+  if (command.type === "copy_data") {
+    return state.progress?.stage === "commit" ? null : "Cancel rolls back the table being copied; the tables before it stay";
+  }
   if (state.progress?.stage === "commit") return null;
   return command.mode === "all_or_nothing"
     ? "Cancel rolls the whole run back"
@@ -588,7 +725,7 @@ export function jobNotification(
   if (!endingIsNews(state) || state.loss === "open_failed") return null;
   const took = state.endedAt - state.startedAt;
   if (took < thresholdS * 1000) return null;
-  const what = view.kind === "dump" ? "Dump" : "SQL file run";
+  const what = view.kind === "dump" ? "Dump" : view.kind === "copy_data" ? "Copy" : "SQL file run";
   const verdict = report.tone === "success" ? "finished" : "ended";
   return { title: `${what} ${verdict} after ${formatElapsed(took)}`, body: `${report.title} · ${view.title}` };
 }
```

```diff
diff --git a/src/features/data-tools/jobsStore.ts b/src/features/data-tools/jobsStore.ts
index 5eb064e..4e801d5 100644
--- a/src/features/data-tools/jobsStore.ts
+++ b/src/features/data-tools/jobsStore.ts
@@ -2,7 +2,8 @@ import { create } from "zustand";
 import { errorMessage } from "@/lib/api/client";
 import { SessionSocket } from "@/lib/api/session-socket";
 import type { JobInfo } from "@/lib/api/types";
-import type { DumpEntry, RunEntry } from "./entryPoints";
+import type { CopyForm } from "./copyForm";
+import type { CopyEntry, DumpEntry, RunEntry } from "./entryPoints";
 import { JobRunner, type JobTransport } from "./jobRunner";
 import {
   cancelJobByRest,
@@ -35,14 +36,19 @@ export interface JobRecord {
   requestId: string;
   sessionId: string | null;
   kind: JobKind;
+  /** The connection the job's session is on (a copy's: its target). */
   profileId: string;
+  /** A copy's source connection (06c): its sessions close when it is deleted, so it waits for the copy too (B13). */
+  sourceProfileId: string | null;
   database: string | null;
   title: string;
   /** What was sent, and the dialog entry it came from: null for a job listed after a reload. */
   command: JobCommand | null;
-  entry: DumpEntry | RunEntry | null;
-  /** A dump's tables in the file's order (its plan), for "table k of n"; null when not known. */
+  entry: DumpEntry | RunEntry | CopyEntry | null;
+  /** A dump's tables in the file's order (its plan), a copy's target tables in its order: "table k of n". */
   tables: readonly string[] | null;
+  /** A copy started here: its form ("Copy again…") and its source tables' names in the copy's order ("Copy the rest…"). */
+  copy: { form: CopyForm; sources: readonly string[] } | null;
   state: JobState;
 }
 
@@ -197,16 +203,26 @@ function run(record: JobRecord): void {
 export interface NewJob {
   kind: JobKind;
   profileId: string;
+  sourceProfileId?: string | null;
   database: string | null;
   title: string;
   command: JobCommand;
-  entry: DumpEntry | RunEntry;
+  entry: DumpEntry | RunEntry | CopyEntry;
   tables?: readonly string[] | null;
+  copy?: { form: CopyForm; sources: readonly string[] } | null;
 }
 
 /** Starts a job on a session of its own; returns its request id (the command's). */
 export function startJob(job: NewJob): string {
-  const record: JobRecord = { ...job, requestId: job.command.request_id, sessionId: null, tables: job.tables ?? null, state: openingJob(transport.now()) };
+  const record: JobRecord = {
+    ...job,
+    requestId: job.command.request_id,
+    sessionId: null,
+    sourceProfileId: job.sourceProfileId ?? null,
+    tables: job.tables ?? null,
+    copy: job.copy ?? null,
+    state: openingJob(transport.now()),
+  };
   put(record, true);
   run(record);
   return record.requestId;
@@ -254,11 +270,13 @@ export function reconcile(jobs: Readonly<Record<string, JobRecord>>, listed: rea
       sessionId: info.session_id,
       kind: info.job,
       profileId: info.profile_id,
+      sourceProfileId: info.source_profile_id ?? null,
       database: null,
       title: info.title,
       command: null,
       entry: null,
       tables: null,
+      copy: null,
       state: listedJob(info, now),
     });
     if (info.outcome) result.close.push(info.session_id);
@@ -349,6 +367,12 @@ export function resetJobsForTests(): void {
 /** The jobs, newest first. */
 export const jobList = ({ jobs, order }: JobsSnapshot): JobRecord[] => order.map((id) => jobs[id]);
 export const runningJobs = (snapshot: JobsSnapshot): JobRecord[] => jobList(snapshot).filter(isRunning);
+/**
+ * The running jobs a connection's deletion would stop (B13): those on its sessions, and the copies reading from it (06c:
+ * `DELETE /profiles/{id}` closes the source's session too).
+ */
+export const runningOn = (snapshot: JobsSnapshot, profileId: string): JobRecord[] =>
+  runningJobs(snapshot).filter((job) => job.profileId === profileId || job.sourceProfileId === profileId);
 /** How many jobs run (the Jobs button's count): a number, so a selector of it renders only when it changes (Q1). */
 export const runningCount = (snapshot: JobsSnapshot): number => runningJobs(snapshot).length;
 /** Whether a job has ended (Clear finished has something to clear): a boolean, for the same reason. */
```

```diff
diff --git a/src/features/data-tools/dataDialogs.ts b/src/features/data-tools/dataDialogs.ts
index 16db3f8..dfc27d8 100644
--- a/src/features/data-tools/dataDialogs.ts
+++ b/src/features/data-tools/dataDialogs.ts
@@ -1,16 +1,18 @@
 import { create } from "zustand";
+import type { CopyForm } from "./copyForm";
 import type { DumpForm } from "./dumpForm";
-import type { DumpEntry, RunEntry } from "./entryPoints";
+import type { CopyEntry, DumpEntry, RunEntry } from "./entryPoints";
 
 /*
  * Which data-tools dialog is open (one at a time, hosted by `DataToolsHost` in the app shell, so a tab that closes never
- * takes a job's dialog with it): a dump's or a run's form, or a job's view (progress, then its outcome). Starting a job
- * turns its form into the job's view; the Jobs list and a toast open a job's view again.
+ * takes a job's dialog with it): a dump's, a run's or a copy's form, or a job's view (progress, then its outcome).
+ * Starting a job turns its form into the job's view; the Jobs list and a toast open a job's view again.
  */
 
 export type DataDialogAsk =
   | { kind: "dump"; entry: DumpEntry; form: DumpForm | null; serial: number }
   | { kind: "run"; entry: RunEntry; serial: number }
+  | { kind: "copy"; entry: CopyEntry; form: CopyForm | null; serial: number }
   | { kind: "job"; requestId: string; serial: number };
 
 let serial = 0;
@@ -25,6 +27,11 @@ export function openRunDialog(entry: RunEntry): void {
   useAsk.setState({ ask: { kind: "run", entry, serial: ++serial } });
 }
 
+/** `form`: "Copy again…" and "Copy the rest…" start from the job's own choices. */
+export function openCopyDialog(entry: CopyEntry, form: CopyForm | null = null): void {
+  useAsk.setState({ ask: { kind: "copy", entry, form, serial: ++serial } });
+}
+
 export function openJobDialog(requestId: string): void {
   useAsk.setState({ ask: { kind: "job", requestId, serial: ++serial } });
 }
```

```diff
diff --git a/src/features/data-tools/JobDialog.tsx b/src/features/data-tools/JobDialog.tsx
index e01adce..ce5576a 100644
--- a/src/features/data-tools/JobDialog.tsx
+++ b/src/features/data-tools/JobDialog.tsx
@@ -7,9 +7,10 @@ import type { FileExcerpt } from "@/lib/api/types";
 import { fileNameOf } from "@/lib/filePaths";
 import { useCancelAgain } from "@/lib/useCancelAgain";
 import { RecoverableError } from "../connections/RecoverableError";
-import { closeDataDialog, openDumpDialog, openRunDialog } from "./dataDialogs";
+import { restForm } from "./copyForm";
+import { closeDataDialog, openCopyDialog, openDumpDialog, openRunDialog } from "./dataDialogs";
 import { dumpFormFromCommand } from "./dumpForm";
-import type { DumpEntry, RunEntry } from "./entryPoints";
+import type { CopyEntry, DumpEntry, RunEntry } from "./entryPoints";
 import { cancelNote, jobReport, type JobAction } from "./jobReport";
 import { FileLines } from "./FileLines";
 import { fileExcerpt, revealFile } from "./jobsApi";
@@ -25,7 +26,10 @@ const KEEPS_RUNNING = {
 /** The jobs whose dialog was closed while they ran and that were told so (once each). */
 const toldKeepsRunning = new Set<string>();
 
-const isDumpEntry = (entry: DumpEntry | RunEntry | null): entry is DumpEntry => entry !== null && "target" in entry;
+const isDumpEntry = (entry: DumpEntry | RunEntry | CopyEntry | null): entry is DumpEntry =>
+  entry !== null && "target" in entry;
+const isCopyEntry = (entry: DumpEntry | RunEntry | CopyEntry | null): entry is CopyEntry =>
+  entry !== null && "source" in entry;
 
 async function reveal(path: string): Promise<void> {
   try {
@@ -52,6 +56,16 @@ function actionButton(record: JobRecord, action: JobAction, key: number) {
     );
   }
   const { entry, command } = record;
+  if (action.kind === "copy_again") {
+    // Back to the copy's form with its own choices; "Copy the rest…": only the tables not copied checked (06c)
+    if (record.kind !== "copy_data" || !isCopyEntry(entry) || !record.copy) return null;
+    const form = action.tables ? restForm(record.copy.form, action.tables) : record.copy.form;
+    return (
+      <Button key={key} onClick={() => openCopyDialog(entry, form)}>
+        {action.label}
+      </Button>
+    );
+  }
   if (record.kind === "run_file" && command?.type === "run_file") {
     // From where the outcome says (its resume point, with its line when exact), or as it was
     const again = againEntry({ profileId: record.profileId, database: record.database, command }, action.patch, action.note);
```

```diff
diff --git a/src/app/quitQuestion.ts b/src/app/quitQuestion.ts
index 29abab6..80efcfb 100644
--- a/src/app/quitQuestion.ts
+++ b/src/app/quitQuestion.ts
@@ -36,11 +36,13 @@ export function quitQuestion(running: readonly RunningJob[], how: QuitHow): Quit
     title: how === "quit" ? "Quit DbaGuy?" : "Close the window?",
     // A run in one transaction is rolled back (not "applies nothing": MySQL's non-transactional tables keep their
     // changes, and the run reports unknown; 06b review). A stopped run statement by statement keeps what took effect,
-    // but not a transaction the file opened and had not committed: the server rolls it back (06b pre-flight M5).
+    // but not a transaction the file opened and had not committed: the server rolls it back (06b pre-flight M5). A
+    // copy's tables are each a transaction of their own: the one being copied is rolled back (06c).
     message:
       `${subject} still running: ${names.join("; ")}${more}. ${act} stops ${them}: a dump writes no file, a run in ` +
-      "one transaction is rolled back, and a run statement by statement keeps what took effect (a transaction the " +
-      "file opened and did not commit is rolled back).",
+      "one transaction is rolled back, a run statement by statement keeps what took effect (a transaction the file " +
+      "opened and did not commit is rolled back), and a copy rolls back the table it is copying (the tables before " +
+      "it stay).",
     keep: "Keep running",
     confirm: how === "quit" ? "Quit anyway" : "Close anyway",
   };
```

- [ ] **Step 6: Run the checks.** `npm test` → all pass (dry run: 878 tests in 47 files; 859 before 06c); `npm run typecheck` → clean.

- [ ] **Step 7: Commit.**

```bash
git add src/features/data-tools/copyForm.ts src/features/data-tools/mappingRows.ts src/features/data-tools/copy.test.ts \
  src/features/data-tools/entryPoints.ts src/features/data-tools/entryPoints.test.ts src/features/data-tools/jobState.ts \
  src/features/data-tools/jobReport.ts src/features/data-tools/jobReport.test.ts src/features/data-tools/jobsStore.ts \
  src/features/data-tools/jobsStore.test.ts src/features/data-tools/jobEndings.test.ts \
  src/features/data-tools/dataDialogs.ts src/features/data-tools/JobDialog.tsx src/app/quitQuestion.ts \
  src/app/quitQuestion.test.ts src/lib/api/types.ts
git commit -m "feat(06c): the copy's form, its column mapping and copy as the third kind of job"
```

---

### Task 7: The Copy dialog and its mapping table

**Suggested implementer:** Opus (the dialog's states; checked by hand).

**Files:**
- Create: `src/features/data-tools/CopyDataDialog.tsx`, `src/features/data-tools/MappingTable.tsx`
- Modify: `src/features/data-tools/DataToolsHost.tsx`, `src/features/data-tools/JobViews.tsx`, `src/features/data-tools/DumpDialog.tsx`, `src/features/data-tools/JobDialog.tsx`, `src/features/data-tools/TargetPicker.tsx`, `src/features/data-tools/jobsApi.ts`, `src/features/query/ConnectionPicker.tsx`, `src/features/edits/tableEditing.ts`

**Interfaces:**
- Consumes: Task 6; 06b's `Dialog`, `Button`, `FieldRow`, `Checkbox`, `Radio`, `Input`, `ConnectionLine`, `TargetPicker`, `DatabasePicker`, `childrenQuery`, `useDebouncedValue`, `useStagedEdits`, `changeCount`, `startJob`, `openJobDialog`, `closeDataDialog`, `RecoverableError`, `isPasswordRequired`, `api`.
- Produces:
  - `CopyDataDialog({entry, form})`: title from `copyDialogTitle`; **From** (the source connection's line; a table tab with staged changes says `NOT_IN_COPY`); **Tables** (a schema's: its Tables folder's tables, all checked when they first arrive, All / None and "k of n checked", in red past 500); **Rows** ("Only rows matching the filter" with the filter shown, a relation with a filter only); **To** (`TargetPicker` with `tool="copy_data"` and `targetRefusal`; PostgreSQL and SQL Server also a schema picker under the chosen database; MySQL's database is its schema); **Table** (one source: the new or existing table's name; Return never copies from there); **How** (the three modes, with the mode's hint under them, replace's in red); **Preview** (read 400 ms after the choices settle; the one of the choices before stays, dimmed, while the next is read; each problem once in a red box, then each table's mapping, then the warnings once each). The footer: Close, and Copy — a danger button on a red target, disabled with the reason as its tooltip while `copyProblem` says one. Copy starts the job (`startJob`: kind `copy_data`, the target's profile and database, the source profile, the plan's targets for "k of n", the form and the source names for "Copy the rest…") and turns the dialog into the job's view.
  - `MappingTable({table, form, onChange})`: one row per source column — source name and type; target name; the target type (a new table's in an input that sends its text when left or on Return, IME-guarded; otherwise text), the identity choice when appending into one ("Keep the source's values" / "Let the target generate them"); "On the way": the problem in red, else why it is not copied, else the conversion's ⚠ note; a checkbox to copy it or leave it out; then the table's statements (`before`, `after`; replace's `DELETE` in the danger colour). The table scrolls inside a fixed height with its scrollbar reserved.
  - `ReadProblem({profileId, error, retry})` moves from `DumpDialog.tsx` to `JobViews.tsx` (the dump, the copy's preview and its tables share it); `JobReportView` lists a copy's tables (`CopiedTables`: source → target, outcome, rows); a copy's job view is 680 px wide.
  - `TargetPicker({…, tool = "run_file", refusal = readOnlyReason, children})`; `DatabasePicker({…, under = []})` (whose children it lists: a copy's schemas under a database node).
  - `jobsApi.fetchTransferPlan(body, signal) -> Promise<TransferPlan>`; `tableEditing.NOT_IN_COPY = "Staged changes are not copied: the copy reads what the server holds"`.

- [ ] **Step 1: The shared pieces.** Move `ReadProblem` and add the copy's tables to the job view; let the target picker serve a copy; let the database picker list a database's schemas; the preview's call; the staged-changes note:

```diff
diff --git a/src/features/data-tools/JobViews.tsx b/src/features/data-tools/JobViews.tsx
index d06e936..851b956 100644
--- a/src/features/data-tools/JobViews.tsx
+++ b/src/features/data-tools/JobViews.tsx
@@ -2,11 +2,14 @@ import { Loader2 } from "lucide-react";
 import type { KeyboardEvent } from "react";
 import { EngineIcon } from "@/components/EngineIcon";
 import { Button } from "@/components/ui/Button";
+import { errorMessage } from "@/lib/api/client";
 import { cn } from "@/lib/cn";
 import { plural } from "@/lib/format";
 import { useNow } from "@/lib/useNow";
 import { useProfiles } from "../connections/hooks";
 import { profileColor } from "../connections/profileColors";
+import { RecoverableError } from "../connections/RecoverableError";
+import { isPasswordRequired } from "../connections/UnlockDialog";
 import { jobReport, progressView, type ErrorLine, type JobReport, type Tone } from "./jobReport";
 import type { JobRecord } from "./jobsStore";
 
@@ -49,6 +52,43 @@ export function ConnectionLine({ profileId, database }: { profileId: string; dat
   );
 }
 
+/**
+ * A read a dialog needs that failed (a dump's plan, a copy's preview, a folder's tables): a missing password asks for it
+ * (then reads again); another error says why, with Retry.
+ */
+export function ReadProblem({ profileId, error, retry }: { profileId: string; error: Error; retry: () => void }) {
+  if (isPasswordRequired(error)) return <RecoverableError profileId={profileId} error={error} onRetry={retry} />;
+  return (
+    <div className="flex items-start gap-2 text-[12px] text-danger">
+      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
+        {errorMessage(error)}
+      </span>
+      <Button size="sm" onClick={retry}>
+        Retry
+      </Button>
+    </div>
+  );
+}
+
+/** A copy's tables and how each ended (06c, R-06-12), in the copy's order. */
+function CopiedTables({ report }: { report: JobReport }) {
+  if (report.tables.length === 0) return null;
+  return (
+    <ul className="max-h-40 overflow-y-auto rounded-md border border-border py-0.5 [scrollbar-gutter:stable]">
+      {report.tables.map((line, i) => (
+        <li key={i} className="flex min-w-0 items-baseline gap-2 px-2 py-0.5">
+          <span className="min-w-0 flex-1 truncate" title={`${line.source ?? ""} → ${line.target}`} data-selectable>
+            {line.source ? `${line.source} → ` : ""}
+            {line.target}
+          </span>
+          <span className={cn("shrink-0", TONE_CLASS[line.tone])}>{line.outcome}</span>
+          {line.outcome === "copied" && <span className="shrink-0 text-muted tabular-nums">{plural(line.rows, "row")}</span>}
+        </li>
+      ))}
+    </ul>
+  );
+}
+
 /** A running job: what it is doing, how far it is, and a warning it gave (SQL Server: what another Cancel does). */
 export function JobProgressView({ record }: { record: JobRecord }) {
   const now = useNow(500);
@@ -189,6 +229,7 @@ export function JobReportView({ record, onShowLine }: { record: JobRecord; onSho
           {report.detail}
         </p>
       )}
+      <CopiedTables report={report} />
       {report.error && <ErrorBox error={report.error} onShowLine={onShowLine} />}
       {report.hint && <p className="text-muted">{report.hint}</p>}
       {report.notes.length > 0 && (
```

```diff
diff --git a/src/features/data-tools/DumpDialog.tsx b/src/features/data-tools/DumpDialog.tsx
index 1e21e11..54497b2 100644
--- a/src/features/data-tools/DumpDialog.tsx
+++ b/src/features/data-tools/DumpDialog.tsx
@@ -14,8 +14,6 @@ import { chooseSavePath } from "@/lib/native";
 import { useDebouncedValue } from "@/lib/useDebouncedValue";
 import { useStagedEdits } from "@/stores/stagedEdits";
 import { useEngineInfo, useProfiles } from "../connections/hooks";
-import { RecoverableError } from "../connections/RecoverableError";
-import { isPasswordRequired } from "../connections/UnlockDialog";
 import { changeCount } from "../edits/staging";
 import { NOT_IN_DUMP } from "../edits/tableEditing";
 import { childrenQuery } from "../explorer/useTreeRows";
@@ -40,7 +38,7 @@ import type { DumpEntry } from "./entryPoints";
 import { dumpFileName } from "./fileNames";
 import { fetchDumpPlan } from "./jobsApi";
 import { startJob } from "./jobsStore";
-import { ConnectionLine } from "./JobViews";
+import { ConnectionLine, ReadProblem } from "./JobViews";
 
 /**
  * The plan is read again once the choices have stayed the same this long: a run of clicks on the tables or a path being
@@ -55,21 +53,6 @@ const FILTER_TITLE_PREFIX = "WHERE ";
 const reasonsOf = (choices: readonly Choice<unknown>[]) =>
   [...new Set(choices.flatMap((choice) => (choice.disabled ? [choice.disabled] : [])))].join(" · ");
 
-/** A read that failed: a missing password asks for it (then reads again); another error says why, with Retry. */
-function ReadProblem({ profileId, error, retry }: { profileId: string; error: Error; retry: () => void }) {
-  if (isPasswordRequired(error)) return <RecoverableError profileId={profileId} error={error} onRetry={retry} />;
-  return (
-    <div className="flex items-start gap-2 text-[12px] text-danger">
-      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
-        {errorMessage(error)}
-      </span>
-      <Button size="sm" onClick={retry}>
-        Retry
-      </Button>
-    </div>
-  );
-}
-
 /**
  * The Tables folder's tables, every one checked the first time they arrive (§3.3.1); All, None and the count, and beside
  * it what is wrong with the checks (none, or more than a dump names): said here, not only on Dump's tooltip.
```

```diff
diff --git a/src/features/data-tools/TargetPicker.tsx b/src/features/data-tools/TargetPicker.tsx
index 16253ff..bd65595 100644
--- a/src/features/data-tools/TargetPicker.tsx
+++ b/src/features/data-tools/TargetPicker.tsx
@@ -1,5 +1,6 @@
+import type { ReactNode } from "react";
 import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
-import type { Profile, ProfileEntry } from "@/lib/api/types";
+import type { EngineInfo, Profile, ProfileEntry } from "@/lib/api/types";
 import { buildFolderTree } from "../connections/folders";
 import { findEngineInfo, useEngines, useProfiles } from "../connections/hooks";
 import { DatabasePicker, profileItems } from "../query/ConnectionPicker";
@@ -11,18 +12,25 @@ const READ_ONLY = "Read-only: it cannot run files";
 const readOnlyReason = (profile: Profile): string | null => (profile.read_only ? READ_ONLY : null);
 
 /**
- * Where Run SQL file runs (spec ⑥ §3.6): the connection, among those whose engine runs files (a read-only one greyed
- * with why), and its database where the engine has them (MySQL: its schemas). The trigger reads as the query tab's: a
- * red connection on a red chip. Picking a connection gives its default database (`onChange(id, null)`).
+ * Where Run SQL file runs, or where a copy goes (spec ⑥ §3.6): the connection, among those whose engine has the tool
+ * (`tool`; one it refuses greyed with why: a read-only one), and its database where the engine has them (MySQL: its
+ * schemas). The trigger reads as the query tab's: a red connection on a red chip. Picking a connection gives its default
+ * database (`onChange(id, null)`). `children`: what follows the database (a copy's schema).
  */
 export function TargetPicker({
   profileId,
   database,
   onChange,
+  tool = "run_file",
+  refusal = readOnlyReason,
+  children,
 }: {
   profileId: string | null;
   database: string | null;
   onChange: (profileId: string, database: string | null) => void;
+  tool?: keyof Pick<EngineInfo, "run_file" | "copy_data">;
+  refusal?: (profile: Profile) => string | null;
+  children?: ReactNode;
 }) {
   const profiles = useProfiles();
   const engines = useEngines();
@@ -31,13 +39,13 @@ export function TargetPicker({
   const engine = findEngineInfo(entries, engines.data, profileId);
   // Only once both lists are here: until then, which engines run files is not known
   const runnable = engines.data
-    ? entries.filter((entry) => findEngineInfo(entries, engines.data, entry.profile.id)?.run_file)
+    ? entries.filter((entry) => findEngineInfo(entries, engines.data, entry.profile.id)?.[tool])
     : [];
   const pick = (id: string) => {
     if (id !== profileId) onChange(id, null);
   };
   const items: MenuItem[] = runnable.length
-    ? profileItems(buildFolderTree(runnable), profileId, pick, readOnlyReason)
+    ? profileItems(buildFolderTree(runnable), profileId, pick, refusal)
     : [{ label: profiles.isPending || engines.isPending ? "Loading…" : "No connections", disabled: true }];
   const nodes = databaseNodes(engine);
   return (
@@ -61,6 +69,7 @@ export function TargetPicker({
           onPick={(name) => name !== database && onChange(profileId, name)}
         />
       )}
+      {children}
     </div>
   );
 }
```

```diff
diff --git a/src/features/query/ConnectionPicker.tsx b/src/features/query/ConnectionPicker.tsx
index e5576a5..456ef4b 100644
--- a/src/features/query/ConnectionPicker.tsx
+++ b/src/features/query/ConnectionPicker.tsx
@@ -4,7 +4,7 @@ import { useState } from "react";
 import { EngineIcon } from "@/components/EngineIcon";
 import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
 import { errorMessage } from "@/lib/api/client";
-import type { Profile, ProfileEntry, SessionInfo } from "@/lib/api/types";
+import type { PathSegment, Profile, ProfileEntry, SessionInfo } from "@/lib/api/types";
 import { cn } from "@/lib/cn";
 import { ENGINE_LABELS } from "@/lib/engines";
 import { buildFolderTree, type FolderNode } from "../connections/folders";
@@ -66,6 +66,8 @@ export function profileItems(
   ];
 }
 
+const NO_PATH: PathSegment[] = [];
+
 /** What the database picker lists: the root's database nodes, or MySQL's schema nodes (its databases). */
 export type DatabaseNodeKind = "database" | "schema";
 
@@ -85,16 +87,19 @@ export function DatabasePicker({
   blockedReason,
   onPick,
   nodeKind = "database",
+  under = NO_PATH,
 }: {
   profileId: string;
   current: string | null;
   blockedReason?: string;
   onPick: (database: string) => void;
   nodeKind?: DatabaseNodeKind;
+  /** Whose children it lists: the root, or a database node (a copy's target schema in PostgreSQL, SQL Server; 06c). */
+  under?: PathSegment[];
 }) {
   const words = NODE_WORDS[nodeKind];
   const [open, setOpen] = useState(false);
-  const children = useQuery({ ...childrenQuery(profileId, []), enabled: open });
+  const children = useQuery({ ...childrenQuery(profileId, under), enabled: open });
   let items: MenuItem[];
   if (children.isPending) items = [{ label: "Loading…", disabled: true }];
   else if (children.isError) items = [{ label: errorMessage(children.error), disabled: true }];
```

```diff
diff --git a/src/features/data-tools/jobsApi.ts b/src/features/data-tools/jobsApi.ts
index e85d6f1..abb7db0 100644
--- a/src/features/data-tools/jobsApi.ts
+++ b/src/features/data-tools/jobsApi.ts
@@ -6,6 +6,8 @@ import type {
   FileInspection,
   JobInfo,
   SessionInfo,
+  TransferPlan,
+  TransferRequest,
 } from "@/lib/api/types";
 
 /*
@@ -61,3 +63,8 @@ export async function fileExcerpt(path: string, line: number): Promise<FileExcer
 export async function fetchDumpPlan(body: DumpPlanRequest, signal?: AbortSignal): Promise<DumpPlan> {
   return unwrap(await (await api()).POST("/data/dump/plan", { body, signal }));
 }
+
+/** Copy data's preview (POST /data/transfer/plan): a plan id only while nothing is wrong. */
+export async function fetchTransferPlan(body: TransferRequest, signal?: AbortSignal): Promise<TransferPlan> {
+  return unwrap(await (await api()).POST("/data/transfer/plan", { body, signal }));
+}
```

```diff
diff --git a/src/features/edits/tableEditing.ts b/src/features/edits/tableEditing.ts
index f1532dc..79de599 100644
--- a/src/features/edits/tableEditing.ts
+++ b/src/features/edits/tableEditing.ts
@@ -56,6 +56,8 @@ export const CLOSE_WHILE_COMMITTING = "A commit is running: wait for it to end,
 export const NOT_IN_EXPORT = "Staged changes are not committed and are not in the export";
 /** The dump dialog's line while the table tab it came from has staged changes (06b pre-flight L8). */
 export const NOT_IN_DUMP = "Staged changes are not in the dump: it reads what the server holds";
+/** The copy dialog's line while the table tab it came from has staged changes (06c, as the dump's). */
+export const NOT_IN_COPY = "Staged changes are not copied: the copy reads what the server holds";
 
 export const COPY_TEXT = "Copy text";
 export const COPY_FAILED = "Could not copy the text";
```

- [ ] **Step 2: The mapping table.** Create `src/features/data-tools/MappingTable.tsx`:

```tsx
import { useState, type KeyboardEvent } from "react";
import { Checkbox, Input } from "@/components/ui/Field";
import type { TableTransfer } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import type { CopyForm, IdentityChoice } from "./copyForm";
import { mappingRows, statementLines, withIdentity, withLeftOut, withOverride, type MappingRow } from "./mappingRows";

/*
 * One table of a copy's preview (spec ⑥ §3.6 MappingTable): each source column → its target column and type (a new
 * table's typed over in place), what happens to its values on the way (⚠), why it is not copied, its problem; an
 * identity's keep or generate when appending; the statements the copy adds around the rows (a DELETE in danger, M11).
 * Every change goes to the form, and the dialog reads the preview again. The words come from `mappingRows` only.
 */

/** A new column's type, typed over: sent once the field is left or Return is pressed (each keystroke would re-plan). */
function TypeField({ row, onType }: { row: MappingRow; onType: (text: string) => void }) {
  const shown = row.typed ?? row.targetType ?? "";
  const [text, setText] = useState(shown);
  const [was, setWas] = useState(shown);
  if (shown !== was) {
    // the preview answered with another type (or the form was reset): the field follows it
    setWas(shown);
    setText(shown);
  }
  const commit = () => {
    if (text !== shown) onType(text.trim() === (row.targetType ?? "") && row.typed === null ? "" : text);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (isImeKeyEvent(e)) return;
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    }
  };
  return (
    <Input
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={onKeyDown}
      aria-label={`Type of ${row.source}`}
      className={cn("h-6 w-full font-mono text-[12px]", row.problem && "border-danger")}
      spellCheck={false}
    />
  );
}

export function MappingTable({
  table,
  form,
  onChange,
}: {
  table: TableTransfer;
  form: CopyForm;
  onChange: (form: CopyForm) => void;
}) {
  const rows = mappingRows(table, form);
  const key = table.source_key;
  const statements = statementLines(table);
  return (
    <div className="flex flex-col gap-2 text-[12px]">
      <div className="max-h-64 overflow-auto rounded-md border border-border [scrollbar-gutter:stable]">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-elevated text-left text-[11px] text-muted">
            <tr>
              <th className="px-2 py-1 font-normal">Source</th>
              <th className="px-2 py-1 font-normal">Target</th>
              <th className="px-2 py-1 font-normal">Type</th>
              <th className="px-2 py-1 font-normal">On the way</th>
              <th className="px-2 py-1 font-normal">Copy</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.source} className="border-t border-border align-top">
                <td className="px-2 py-1" data-selectable>
                  <span className="font-mono [overflow-wrap:anywhere]">{row.source}</span>
                  <span className="block text-[11px] text-muted">{row.sourceType}</span>
                </td>
                <td className="px-2 py-1 font-mono [overflow-wrap:anywhere]" data-selectable>
                  {row.target ?? <span className="text-muted">—</span>}
                </td>
                <td className="min-w-[9rem] px-2 py-1">
                  {row.typeEditable && !row.skipped ? (
                    <TypeField row={row} onType={(text) => onChange(withOverride(form, key, row.source, text))} />
                  ) : (
                    <span className="font-mono text-muted" data-selectable>
                      {row.targetType ?? ""}
                    </span>
                  )}
                  {row.identity && (
                    <select
                      className="mt-1 h-6 rounded border border-border bg-elevated px-1 text-[12px]"
                      value={row.identity}
                      aria-label={`Identity of ${row.target ?? row.source}`}
                      onChange={(e) => onChange(withIdentity(form, key, row.source, e.target.value as IdentityChoice))}
                    >
                      <option value="keep">Keep the source's values</option>
                      <option value="generate">Let the target generate them</option>
                    </select>
                  )}
                </td>
                <td className="px-2 py-1 [overflow-wrap:anywhere]" data-selectable>
                  {row.problem ? (
                    <span className="text-danger">{row.problem}</span>
                  ) : row.skipped ? (
                    <span className="text-muted">{row.skipped}</span>
                  ) : row.conversion ? (
                    <span className="text-warning">⚠ {row.conversion}</span>
                  ) : null}
                </td>
                <td className="px-2 py-1">
                  {row.canLeaveOut && (
                    <Checkbox
                      label=""
                      checked={!row.leftOut}
                      aria-label={`Copy ${row.source}`}
                      onChange={(e) => onChange(withLeftOut(form, key, row.source, !e.target.checked))}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {statements.length > 0 && (
        <div className="flex flex-col gap-1">
          {statements.map((line, i) => (
            <pre
              key={i}
              className={cn(
                "max-h-40 overflow-auto rounded-md border px-2 py-1 font-mono text-[11px] whitespace-pre-wrap [scrollbar-gutter:stable] [overflow-wrap:anywhere]",
                line.danger ? "border-danger/40 bg-danger/10 font-medium text-danger" : "border-border text-muted",
              )}
              data-selectable
            >
              {line.sql}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: The dialog.** Create `src/features/data-tools/CopyDataDialog.tsx`, host it, and widen a copy's job view:

```tsx
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Radio } from "@/components/ui/Field";
import type { TransferPlan, TransferRequest } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import { useStagedEdits } from "@/stores/stagedEdits";
import { findEngineInfo, useEngines, useProfiles } from "../connections/hooks";
import { changeCount } from "../edits/staging";
import { NOT_IN_COPY } from "../edits/tableEditing";
import { childrenQuery } from "../explorer/useTreeRows";
import { DatabasePicker } from "../query/ConnectionPicker";
import {
  copyCommand,
  copyDialogTitle,
  copyJobTitle,
  copyProblem,
  copyRequest,
  defaultSchema,
  initialCopyForm,
  MAX_COPY_TABLES,
  MODE_HINTS,
  MODE_LABELS,
  planTargets,
  targetRefusal,
  withTarget,
  type CopyForm,
  type CopyMode,
} from "./copyForm";
import { closeDataDialog, openJobDialog } from "./dataDialogs";
import type { CopyEntry } from "./entryPoints";
import { fetchTransferPlan } from "./jobsApi";
import { startJob } from "./jobsStore";
import { ConnectionLine, ReadProblem } from "./JobViews";
import { MappingTable } from "./MappingTable";
import { previewProblems } from "./mappingRows";
import { TargetPicker } from "./TargetPicker";

/**
 * The preview is read again once the choices have stayed the same this long (as the dump's plan): a run of clicks or a
 * typed type is one read of both catalogs, not one per change.
 */
const PLAN_DEBOUNCE_MS = 400;
const FILTER_TITLE_PREFIX = "WHERE ";
const MODES: CopyMode[] = ["create", "append", "replace_rows"];

/** A schema's tables, every one checked the first time they arrive; All, None and the count. */
function TableChecks({ names, checked, onChange }: { names: string[]; checked: readonly string[]; onChange: (n: string[]) => void }) {
  const on = new Set(checked);
  const toggle = (name: string, value: boolean) => onChange(names.filter((n) => (n === name ? value : on.has(n))));
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1 text-[12px]">
        <Button size="sm" variant="ghost" onClick={() => onChange(names)}>
          All
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange([])}>
          None
        </Button>
        <span className={cn("ml-auto tabular-nums", checked.length > MAX_COPY_TABLES ? "text-danger" : "text-muted")}>
          {checked.length.toLocaleString()} of {names.length.toLocaleString()} checked
        </span>
      </div>
      <div className="flex max-h-40 flex-col gap-0.5 overflow-y-auto rounded-md border border-border px-2 py-1 [scrollbar-gutter:stable]">
        {names.length === 0 && <p className="text-[12px] text-muted">No tables</p>}
        {names.map((name) => (
          <Checkbox key={name} label={<span className="[overflow-wrap:anywhere]">{name}</span>} checked={on.has(name)}
            onChange={(e) => toggle(name, e.target.checked)} />
        ))}
      </div>
    </div>
  );
}

/** The preview: each table's mapping and statements, and what blocks the copy (said once each, in red). */
function Preview({ plan, stale, form, onChange }: { plan: TransferPlan; stale: boolean; form: CopyForm; onChange: (f: CopyForm) => void }) {
  const problems = previewProblems(plan);
  const warnings = [...plan.warnings, ...plan.tables.flatMap((table) => table.warnings)];
  return (
    <div className={cn("flex flex-col gap-3 transition-opacity", stale && "opacity-60")}>
      {problems.length > 0 && (
        <ul className="flex flex-col gap-1 rounded-md border border-danger/40 bg-danger/10 p-2 text-[12px] text-danger">
          {problems.map((problem, i) => (
            <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
              {problem}
            </li>
          ))}
        </ul>
      )}
      {plan.tables.map((table) => (
        <div key={table.source_key} className="flex flex-col gap-1">
          <p className="text-[12px] font-medium [overflow-wrap:anywhere]" data-selectable>
            {table.source} → {table.target}
          </p>
          <MappingTable table={table} form={form} onChange={onChange} />
        </div>
      ))}
      {warnings.length > 0 && (
        <ul className="flex list-disc flex-col gap-1 pl-4 text-[12px] text-warning">
          {[...new Set(warnings)].map((warning, i) => (
            <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
              {warning}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Copy data to… (spec ⑥ §3.6 CopyDataDialog): the source as the entry gave it, where the rows go, how, and the backend's
 * preview of every column and statement, read again as the choices change. Copy runs exactly the preview shown (its
 * plan id; D-06-8) on a session of the target's own, and turns this dialog into the job's view. A red target's Copy is a
 * danger button (⑤ H24); a preview with a problem has no Copy. `form`: "Copy again…" and "Copy the rest…".
 */
export function CopyDataDialog({ entry, form: preset }: { entry: CopyEntry; form: CopyForm | null }) {
  const profiles = useProfiles();
  const engines = useEngines();
  const entries = profiles.data ?? [];
  const sourceProfile = entries.find((item) => item.profile.id === entry.profileId)?.profile ?? null;
  const [form, setForm] = useState<CopyForm>(() => preset ?? initialCopyForm(entry));
  const patch = (changes: Partial<CopyForm>) => setForm((current) => ({ ...current, ...changes }));
  const modeGroup = useId();
  const target = entries.find((item) => item.profile.id === form.targetProfileId)?.profile ?? null;
  const targetEngine = findEngineInfo(entries, engines.data, form.targetProfileId);

  // A schema's tables: listed from its Tables folder, all checked the first time they arrive (unless "again")
  const folder = entry.source.kind === "tables" ? entry.source.folder : null;
  const children = useQuery({ ...childrenQuery(entry.profileId, folder ?? []), enabled: folder !== null });
  const tables = useMemo(() => children.data?.filter((node) => node.kind === "table") ?? null, [children.data]);
  const [seeded, setSeeded] = useState(preset !== null || folder === null);
  if (!seeded && tables) {
    setSeeded(true);
    setForm((current) => ({ ...current, checked: tables.map((table) => table.name) }));
  }

  // The preview: asked once the choices settle; the one of the choices before stays, dimmed, while the next is read
  const requestKey = useMemo(() => {
    const fresh = copyRequest(entry, form, tables);
    return fresh === null ? null : JSON.stringify(fresh);
  }, [entry, form, tables]);
  const request = useMemo(() => (requestKey === null ? null : (JSON.parse(requestKey) as TransferRequest)), [requestKey]);
  const body = useDebouncedValue(request, PLAN_DEBOUNCE_MS);
  const plan = useQuery({
    queryKey: ["copyPlan", body],
    queryFn: ({ signal }) => fetchTransferPlan(body!, signal),
    enabled: body !== null,
    retry: false,
    gcTime: 0,
    placeholderData: keepPreviousData,
  });
  const shownPlan = request !== null && !plan.isError ? (plan.data ?? null) : null;
  const behind = body !== request || plan.isPlaceholderData;
  const readPlan = behind ? null : shownPlan;
  const problem = copyProblem(entry, form, tables, readPlan, behind);

  const tabId = entry.tabId ?? "";
  const staged = useStagedEdits(tabId);
  const stagedNote = staged && changeCount(staged) > 0 ? NOT_IN_COPY : null;
  const red = target?.color === "red";
  const one = entry.source.kind !== "tables";

  const pickTarget = (profileId: string, database: string | null) => {
    const profile = entries.find((item) => item.profile.id === profileId)?.profile;
    const engine = findEngineInfo(entries, engines.data, profileId);
    if (!profile || !engine) return;
    if (profileId !== form.targetProfileId) {
      setForm((current) => withTarget(current, profile, engine));
      return;
    }
    // the same connection, another database: MySQL's "database" is the schema itself
    if (engine.engine === "mysql") patch({ targetSchema: database });
    else patch({ targetDatabase: database, targetSchema: defaultSchema(engine.engine, database) });
  };

  const start = () => {
    if (problem !== null || !readPlan || !target || !sourceProfile || !form.targetProfileId) return;
    const requestId = crypto.randomUUID();
    startJob({
      kind: "copy_data",
      profileId: form.targetProfileId,
      sourceProfileId: entry.profileId,
      database: form.targetDatabase,
      title: copyJobTitle(readPlan, sourceProfile.name, target.name),
      command: copyCommand(readPlan, entry, requestId),
      entry,
      tables: planTargets(readPlan),
      copy: { form, sources: readPlan.tables.map((table) => table.source_path?.at(-1)?.name ?? table.source) },
    });
    openJobDialog(requestId);
  };

  const footer = (
    <>
      <Button variant="ghost" onClick={closeDataDialog}>
        Close
      </Button>
      <Button variant={red ? "danger" : "primary"} onClick={start} disabled={problem !== null} title={problem ?? undefined}>
        Copy
      </Button>
    </>
  );

  const source = entry.source;
  return (
    <Dialog open onOpenChange={(open) => !open && closeDataDialog()} title={copyDialogTitle(entry)} width={760} footer={footer}>
      <div className="flex flex-col gap-3">
        <FieldRow label="From">
          <ConnectionLine profileId={entry.profileId} database={entry.database} />
        </FieldRow>
        {stagedNote && (
          <p className="text-[12px] text-warning" data-selectable>
            {stagedNote}
          </p>
        )}
        {source.kind === "tables" && (
          <FieldRow label="Tables">
            {tables ? (
              <TableChecks names={tables.map((node) => node.name)} checked={form.checked ?? []} onChange={(checked) => patch({ checked })} />
            ) : children.error ? (
              <ReadProblem profileId={entry.profileId} error={children.error} retry={() => void children.refetch()} />
            ) : (
              <p className="animate-delayed-show text-[12px] text-muted">Loading the tables…</p>
            )}
          </FieldRow>
        )}
        {source.kind === "relation" && source.filter !== null && (
          <FieldRow label="Rows">
            <div className="flex min-w-0 items-center gap-2">
              <Checkbox label="Only rows matching the filter" checked={form.onlyFiltered} onChange={(e) => patch({ onlyFiltered: e.target.checked })} />
              <code className="min-w-0 truncate font-mono text-[12px] text-muted" title={`${FILTER_TITLE_PREFIX}${source.filter}`} data-selectable>
                {FILTER_TITLE_PREFIX}
                {source.filter}
              </code>
            </div>
          </FieldRow>
        )}
        <FieldRow label="To">
          <TargetPicker
            profileId={form.targetProfileId}
            database={targetEngine?.engine === "mysql" ? form.targetSchema : form.targetDatabase}
            onChange={pickTarget}
            tool="copy_data"
            refusal={targetRefusal}
          >
            {form.targetProfileId && targetEngine && (targetEngine.engine === "postgres" || targetEngine.engine === "mssql") && (
              <DatabasePicker
                profileId={form.targetProfileId}
                current={form.targetSchema}
                nodeKind="schema"
                under={form.targetDatabase ? [{ kind: "database", name: form.targetDatabase }] : []}
                onPick={(schema) => patch({ targetSchema: schema })}
              />
            )}
          </TargetPicker>
        </FieldRow>
        {one && (
          <FieldRow label="Table">
            <Input
              value={form.table}
              onChange={(e) => patch({ table: e.target.value })}
              onKeyDown={(e) => {
                if (!isImeKeyEvent(e) && e.key === "Enter") e.preventDefault(); // Return never copies from here
              }}
              className="h-7 w-full max-w-[320px] font-mono text-[12px]"
              aria-label="The target table's name"
              spellCheck={false}
            />
          </FieldRow>
        )}
        <FieldRow label="How">
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {MODES.map((mode) => (
                <Radio key={mode} name={modeGroup} label={MODE_LABELS[mode]} checked={form.mode === mode} onChange={() => patch({ mode })} />
              ))}
            </div>
            <span className={cn("text-[11px]", form.mode === "replace_rows" ? "text-danger" : "text-muted")}>{MODE_HINTS[form.mode]}</span>
          </div>
        </FieldRow>
        {request !== null && (
          <FieldRow label="Preview">
            {plan.error ? (
              <ReadProblem profileId={entry.profileId} error={plan.error} retry={() => void plan.refetch()} />
            ) : shownPlan ? (
              <Preview plan={shownPlan} stale={behind} form={form} onChange={setForm} />
            ) : (
              <p className="animate-delayed-show text-[12px] text-muted">Reading both connections…</p>
            )}
          </FieldRow>
        )}
      </div>
    </Dialog>
  );
}
```

```diff
diff --git a/src/features/data-tools/DataToolsHost.tsx b/src/features/data-tools/DataToolsHost.tsx
index 48da581..132057d 100644
--- a/src/features/data-tools/DataToolsHost.tsx
+++ b/src/features/data-tools/DataToolsHost.tsx
@@ -1,4 +1,5 @@
 import { useEffect } from "react";
+import { CopyDataDialog } from "./CopyDataDialog";
 import { closeDataDialog, shownJob, useDataDialogAsk } from "./dataDialogs";
 import { DumpDialog } from "./DumpDialog";
 import { JobDialog } from "./JobDialog";
@@ -16,7 +17,7 @@ function JobAsk({ requestId }: { requestId: string }) {
 }
 
 /**
- * The data tools' one dialog (`dataDialogs.ts`), in the app shell: a dump's form, a run's form, or a job's view. A tab
+ * The data tools' one dialog (`dataDialogs.ts`), in the app shell: a dump's, a run's or a copy's form, or a job's view. A tab
  * that closes never takes a job's dialog with it. Each ask mounts its dialog anew (`serial`).
  */
 export function DataToolsHost() {
@@ -26,6 +27,8 @@ export function DataToolsHost() {
       return <DumpDialog key={ask.serial} entry={ask.entry} form={ask.form} />;
     case "run":
       return <RunFileDialog key={ask.serial} entry={ask.entry} />;
+    case "copy":
+      return <CopyDataDialog key={ask.serial} entry={ask.entry} form={ask.form} />;
     case "job":
       return <JobAsk key={ask.serial} requestId={ask.requestId} />;
     default:
```

```diff
diff --git a/src/features/data-tools/JobDialog.tsx b/src/features/data-tools/JobDialog.tsx
index ce5576a..b806c25 100644
--- a/src/features/data-tools/JobDialog.tsx
+++ b/src/features/data-tools/JobDialog.tsx
@@ -214,7 +214,7 @@ export function JobDialog({ record }: { record: JobRecord }) {
       open
       onOpenChange={(open) => !open && close()}
       title={record.title}
-      width={record.kind === "dump" ? 560 : 640}
+      width={record.kind === "dump" ? 560 : record.kind === "copy_data" ? 680 : 640}
       footer={footer}
     >
       <div className="flex flex-col gap-3">
```

- [ ] **Step 4: Run the checks.** `npm run typecheck` → clean; `npm test` → all pass (878); `npm run build` → clean.

- [ ] **Step 5: Commit.**

```bash
git add src/features/data-tools/CopyDataDialog.tsx src/features/data-tools/MappingTable.tsx \
  src/features/data-tools/DataToolsHost.tsx src/features/data-tools/JobViews.tsx src/features/data-tools/DumpDialog.tsx \
  src/features/data-tools/JobDialog.tsx src/features/data-tools/TargetPicker.tsx src/features/data-tools/jobsApi.ts \
  src/features/query/ConnectionPicker.tsx src/features/edits/tableEditing.ts
git commit -m "feat(06c): the Copy dialog and its mapping table"
```

- [ ] **Step 6: Manual checks (the coordinator, after Task 8: the dialog has no entry before it).** `scripts/dev.sh --web`, `http://localhost:1420`. First, in a "dev pg" query tab: `CREATE TABLE zz_06c_orders (id bigint PRIMARY KEY, placed timestamptz, amount numeric(12,2), note text, tags text[]); INSERT INTO zz_06c_orders VALUES (1, '2026-10-05 12:00:00+08', 12.50, 'first', '{a,b}'), (2, '2026-10-05 12:00:00.123456+05:30', 0.01, NULL, '{}'), (3, 'infinity', 1, 'last', NULL);` and in a "dev mysql" query tab nothing yet.
  1. **Create, across engines.** Tree: dev pg › public › Tables › zz_06c_orders › Copy data to…. The title is "Copy public.zz_06c_orders"; From shows dev pg; To: pick dev mysql (its database `dbaguy` is the schema); Table `zz_06c_orders`; How: Create a new table. Within about half a second the Preview shows the five columns: `placed` → `datetime(6)` with "⚠ converted to UTC: the offset is not kept", `tags` → `longtext` with "⚠ PostgreSQL's text of the value", `amount` → `decimal(12,2)`; the statements show the CREATE TABLE; Copy is enabled.
  2. **A value that cannot go.** Press Copy: the dialog becomes the job's view ("Copying zz_06c_orders"), then "Nothing was copied: the table was rolled back" with the error "Row 3 of public.zz_06c_orders, column placed: is 'infinity', which MySQL cannot hold" and its hint (filter the row out or give the column a text type); the notes say `zz_06c_orders was created (MySQL commits a CREATE TABLE at once) and stays, empty…`; the table line reads "rolled back". "Copy again…" returns to the form as it was. In dev mysql, `SELECT COUNT(*) FROM zz_06c_orders` is 0; drop it there (`DROP TABLE zz_06c_orders`).
  3. **A filter, a typed type, a left-out column.** From a dev pg table tab of zz_06c_orders, filter `id < 3`, then ⋯ › Copy rows to…: "Only rows matching the filter" is checked with `WHERE id < 3`; target dev mysql. In the mapping table type over `note`'s type with `varchar(5)` and press Return: the preview reads again with it; type `text; DROP TABLE x` and leave the field: the red box says `note: Not a single type: it has a ;` and Copy is disabled with that reason as its tooltip; clear the field: the mapped type comes back. Uncheck `tags`' Copy box: it reads "left out" and its target is "—". Copy: "Copied 2 rows to zz_06c_orders"; in MySQL `SELECT id, placed FROM zz_06c_orders ORDER BY id` reads `2026-10-05 04:00:00.000000` and `2026-10-05 06:30:00.123456` (UTC, every digit kept), and `note` is `varchar(5)` if you left it typed so.
  4. **Append, identities, replace.** In dev pg: `CREATE TABLE zz_06c_into (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, amount numeric(12,2))`. Copy zz_06c_orders (filter `id < 3`) into it with "Append to an existing table": the mapping shows `id` with "Keep the source's values", `placed`, `note`, `tags` as "no such column in the target"… (not copied: listed); the statements show `SELECT setval(…)` after the rows. Copy → "Copied 2 rows". Then `INSERT INTO zz_06c_into (amount) VALUES (9)` in the query tab gets id 3. Copy again with "Delete its rows, then append": the hint under How is red, the statements show `DELETE FROM public.zz_06c_into` in the danger colour.
  5. **Refusals.** Create mode into zz_06c_into: the red box says "public.zz_06c_into already exists: append to it, or choose another name", Copy disabled. Source and target the same table (dev pg, Append, Table `zz_06c_orders`): "The source and the target are the same table". In To, "dev pg read-only" is greyed with "Read-only: nothing can be copied into it"; dev couchbase is not listed.
  6. **Red target.** Target "dev mysql red": Copy is a red (danger) button; the replace-rows DELETE is red in the statements.
  7. **A schema's tables.** In dev pg: `CREATE SCHEMA zz_06c_s; CREATE TABLE zz_06c_s.zz_06c_parent (id int PRIMARY KEY); CREATE TABLE zz_06c_s.zz_06c_child (id int PRIMARY KEY, parent int REFERENCES zz_06c_s.zz_06c_parent); INSERT INTO zz_06c_s.zz_06c_parent VALUES (1); INSERT INTO zz_06c_s.zz_06c_child VALUES (1, 1);` Refresh the tree; zz_06c_s › Copy data to…: Tables lists zz_06c_child and zz_06c_parent, both checked ("2 of 2 checked"), All / None work; target "zz_06c sqlite" (`main`). The preview lists zz_06c_parent before zz_06c_child. Copy: the view shows "Copying zz_06c_parent (1 of 2)" (briefly), then "Copied 2 rows in 2 tables" with each table's line "copied · 1 row".
  8. **Cancel and Jobs.** In dev pg: `CREATE TABLE zz_06c_big AS SELECT g AS id, md5(g::text) AS label FROM generate_series(1, 2000000) g;` Copy it to dev mssql (create). While "Copying zz_06c_big" shows, the note reads "Cancel rolls back the table being copied; the tables before it stay"; close the dialog: the toast says "The copy keeps running: open it again from Jobs, at the bottom of the sidebar"; Jobs shows it running; reopen it and Cancel: "Cancelled: nothing was copied", the table line "rolled back"; in dev mssql the new table exists only if SQL Server committed its CREATE — it does not (the CREATE is in the table's transaction): `SELECT OBJECT_ID('dbo.zz_06c_big')` is NULL.
  9. **Copy the rest.** Make zz_06c_child fail: `ALTER TABLE zz_06c_s.zz_06c_child ADD COLUMN at timestamptz DEFAULT 'infinity';` Copy zz_06c_s (both tables) into dev mysql: the result is "Stopped: 1 of 2 tables copied", zz_06c_parent "copied · 1 row", zz_06c_child "rolled back", the row's error, and the note that zz_06c_child was created and stays, empty; "Copy the rest…" opens the form with only zz_06c_child checked.
  10. **Nothing left.** Drop every `zz_06c` object made above on dev pg (`DROP SCHEMA zz_06c_s CASCADE; DROP TABLE zz_06c_orders, zz_06c_into, zz_06c_big;`), dev mysql (`zz_06c_orders`, `zz_06c_parent`, `zz_06c_child`) and dev mssql (`zz_06c_big` if a copy completed); delete `dev/data/scratch/zz_06c.sqlite`. Check that `SELECT … LIKE 'zz\_06c%'` finds nothing on each engine.

---

### Task 8: The entries, and the delete guard for a copy's source (B13)

**Suggested implementer:** Sonnet (wiring; each entry follows the dump's).

**Files:**
- Modify: `src/features/explorer/Explorer.tsx`, `src/features/table-data/{TableDataTab.tsx, tableTabActions.ts}`, `src/features/grid/{DataGrid.tsx, GridMenu.tsx, StatusBar.tsx}`, `src/features/query/{QueryTab.tsx, ResultGrid.tsx, ResultsArea.tsx}`, `src/features/palette/commands.ts`
- Test: `src/features/palette/dataCommands.test.ts`

**Interfaces:**
- Consumes: Task 6's `copyEntryForNode`, `copyEntryForTable`, `copyEntryForResult`, `paletteCopyEntry`, `openCopyDialog`, `runningOn`; Task 7's dialog.
- Produces:
  - The tree's "Copy data to…" after Dump… on a schema, a Tables folder, a table, a view or a materialized view of an engine that copies (C10: not a database node).
  - The table tab's ⋯ "Copy rows to…" after "Dump table…" (with the filter it shows; greyed with why when the engine does not copy), and its palette action `copyRows` (`TableTabActionId = "dumpTable" | "copyRows"`).
  - A query result's "Copy result to a table…" in the grid's menu (after Export…) and "Copy to table…" in its status bar (after Export…), for an engine that copies, once the run has ended and the result has columns: its statement and columns, on the tab's session (`DataGrid.onCopyToTable`, `GridMenuActions.openCopyToTable`, `StatusBar.onCopyToTable`, `ResultGrid.onCopyToTable(sql, columns)`, `ResultsArea.onCopyToTable`).
  - ⌘⇧P "Copy data to…" (`dataTools.copy`, after Dump…): a table tab through its own `copyRows`, a query tab its default schema's tables; listed for engines that copy.
  - Deleting a connection waits for every running job that runs on it or reads from it (`runningOn`; B13's toast "Profile not deleted — a job is still running on it").

- [ ] **Step 1: Write the failing test.** Add the palette's case to `src/features/palette/dataCommands.test.ts`:

```diff
diff --git a/src/features/palette/dataCommands.test.ts b/src/features/palette/dataCommands.test.ts
index fd3376d..53b8ca9 100644
--- a/src/features/palette/dataCommands.test.ts
+++ b/src/features/palette/dataCommands.test.ts
@@ -10,9 +10,10 @@ import { COMMANDS, type CommandContext } from "./commands";
 /* ⌘⇧P's data-tools commands (spec ⑥ §3.6): Dump… where the engine dumps, Run SQL file… always, Show jobs once there
    are jobs; a table tab's Dump runs its own handler (it knows its filter). */
 
-const opened = vi.hoisted(() => ({ dump: [] as unknown[], run: [] as unknown[], jobs: [] as boolean[] }));
+const opened = vi.hoisted(() => ({ dump: [] as unknown[], run: [] as unknown[], copy: [] as unknown[], jobs: [] as boolean[] }));
 vi.mock("../data-tools/dataDialogs", () => ({
   openDumpDialog: (entry: unknown) => opened.dump.push(entry),
+  openCopyDialog: (entry: unknown) => opened.copy.push(entry),
   openRunDialog: (entry: unknown) => opened.run.push(entry),
   setJobsPopoverOpen: (open: boolean) => opened.jobs.push(open),
 }));
@@ -23,10 +24,12 @@ const P = "p1";
 const TABLE: Tab = { id: "table:1", kind: "table", profileId: P, path: [{ kind: "database", name: "d" }, { kind: "schema", name: "s" }, { kind: "folder", name: "tables" }, { kind: "table", name: "t" }], title: "t" };
 const QUERY = { id: "query:1", kind: "query", profileId: P, database: null, title: "Q" } as unknown as Tab;
 
-function context(activeTab: Tab | null, dump = true, tableActions: TableTabActions | null = null): CommandContext {
+function context(activeTab: Tab | null, dump = true, tableActions: TableTabActions | null = null, copy = true): CommandContext {
   const queryClient = new QueryClient();
   queryClient.setQueryData<ProfileEntry[]>(profilesKey, [{ profile: { id: P, engine: "postgres", name: "Local PG", database: "dbaguy" } } as ProfileEntry]);
-  queryClient.setQueryData<EngineInfo[]>(enginesKey, [{ engine: "postgres", dump, run_file: true } as EngineInfo]);
+  queryClient.setQueryData<EngineInfo[]>(enginesKey, [
+    { engine: "postgres", dump, run_file: true, copy_data: copy, per_database_connection: true } as EngineInfo,
+  ]);
   return { activeTab, tabActions: null, editActions: null, tableActions, settings: { loaded: true, current: {} as AppSettings, save: async (n) => n }, queryClient };
 }
 const command = (id: string) => COMMANDS.find((c) => c.id === id)!;
@@ -35,6 +38,7 @@ describe("the palette's data-tools commands", () => {
   beforeEach(() => {
     opened.dump = [];
     opened.run = [];
+    opened.copy = [];
     opened.jobs = [];
     useJobs.setState({ jobs: {}, order: [] });
   });
@@ -49,7 +53,7 @@ describe("the palette's data-tools commands", () => {
 
   it("run a table tab's own Dump table…, as it is when it runs", () => {
     const run = vi.fn();
-    mounted.actions = { dumpTable: { enabled: true, run } };
+    mounted.actions = { dumpTable: { enabled: true, run }, copyRows: { enabled: true, run: vi.fn() } };
     const ctx = context(TABLE, true, mounted.actions);
     expect(command("dataTools.dump").enabled(ctx)).toBe(true);
     command("dataTools.dump").run(ctx);
@@ -58,7 +62,7 @@ describe("the palette's data-tools commands", () => {
 
   it("grey a table tab's Dump… while its own action cannot run, and run nothing then", () => {
     const run = vi.fn();
-    mounted.actions = { dumpTable: { enabled: false, run } };
+    mounted.actions = { dumpTable: { enabled: false, run }, copyRows: { enabled: false, run: vi.fn() } };
     const ctx = context(TABLE, true, mounted.actions);
     expect(command("dataTools.dump").enabled(ctx)).toBe(false);
     command("dataTools.dump").run(ctx);
@@ -78,4 +82,18 @@ describe("the palette's data-tools commands", () => {
     command("dataTools.jobs").run(context(null));
     expect(opened.jobs).toEqual([true]);
   });
+
+  it("list Copy data to… where the engine copies: a table tab's own Copy rows to…, a query tab's schema", () => {
+    expect(command("dataTools.copy").visible!(context(QUERY, true, null, false))).toBe(false);
+    const copyRows = vi.fn();
+    mounted.actions = { dumpTable: { enabled: true, run: vi.fn() }, copyRows: { enabled: true, run: copyRows } };
+    command("dataTools.copy").run(context(TABLE, true, mounted.actions));
+    expect([copyRows.mock.calls.length, opened.copy]).toEqual([1, []]);
+    command("dataTools.copy").run(context(QUERY));
+    expect(opened.copy).toEqual([{
+      profileId: P,
+      database: "dbaguy",
+      source: { kind: "tables", schema: "public", folder: [{ kind: "database", name: "dbaguy" }, { kind: "schema", name: "public" }, { kind: "folder", name: "tables" }] },
+    }]);
+  });
 });
```

Run `npm test -- src/features/palette` → it fails (no `dataTools.copy`).

- [ ] **Step 2: The tree and the delete guard.**

```diff
diff --git a/src/features/explorer/Explorer.tsx b/src/features/explorer/Explorer.tsx
index 66e41c0..a26b73b 100644
--- a/src/features/explorer/Explorer.tsx
+++ b/src/features/explorer/Explorer.tsx
@@ -17,9 +17,9 @@ import { findEngineInfo, isReadOnlyProfile, useDeleteProfile, useEngines, usePro
 import { profileColor } from "../connections/profileColors";
 import { secretsNeeded } from "../connections/UnlockDialog";
 import { joinUnlockPrompt } from "../connections/unlockPrompts";
-import { openDumpDialog, openRunDialog } from "../data-tools/dataDialogs";
-import { dumpEntryForNode, runEntryForNode, type RunEntry } from "../data-tools/entryPoints";
-import { runningJobs, syncJobs, useJobs } from "../data-tools/jobsStore";
+import { openCopyDialog, openDumpDialog, openRunDialog } from "../data-tools/dataDialogs";
+import { copyEntryForNode, dumpEntryForNode, runEntryForNode, type RunEntry } from "../data-tools/entryPoints";
+import { runningOn, syncJobs, useJobs } from "../data-tools/jobsStore";
 import { READ_ONLY_RUN } from "../data-tools/runForm";
 import { askOpenDocument } from "../documents/openDocument";
 import { applyTreeContext } from "../query/queryContext";
@@ -219,12 +219,13 @@ export function Explorer() {
    * Before a profile is deleted, each of its tabs is asked in turn (a running export, a run, an open transaction):
    * table and object tabs close, query tabs agree to lose their session. Any "no" keeps the profile. First, a
    * data-tools job running on it keeps it, before any tab is asked: the delete closes every session of the profile,
-   * the jobs' too (B13). The backend's list is read first: a job wrongly guessed lost (`not_listed`) that it has
-   * running counts again, and one it does not have never blocks the delete.
+   * the jobs' too (B13), and a copy reading from it as well (its source's session, 06c). The backend's list is read
+   * first: a job wrongly guessed lost (`not_listed`) that it has running counts again, and one it does not have never
+   * blocks the delete.
    */
   const releaseTabs = async (profileId: string): Promise<boolean> => {
     await syncJobs(); // a list that fails is logged, and the jobs stay as this window knows them
-    const jobs = runningJobs(useJobs.getState()).filter((job) => job.profileId === profileId).length;
+    const jobs = runningOn(useJobs.getState(), profileId).length;
     if (jobs > 0) {
       toast("warning", `Profile not deleted — ${jobs === 1 ? "a job is" : `${jobs} jobs are`} still running on it`);
       return false;
@@ -393,9 +394,11 @@ export function Explorer() {
         const engine = findEngineInfo(entries, engines, row.profileId);
         // The data tools first (spec ⑥ §3.6: what the engine can do decides, D-06-13), then a collection's own items
         const dumpEntry = dumpEntryForNode(row.profileId, engine, row.node);
+        const copyEntry = copyEntryForNode(row.profileId, engine, row.node);
         const runEntry = runEntryForNode(row.profileId, engine, row.node);
         const dataItems: MenuItem[] = [
           ...(dumpEntry ? [{ label: "Dump…", onSelect: () => openDumpDialog(dumpEntry) }] : []),
+          ...(copyEntry ? [{ label: "Copy data to…", onSelect: () => openCopyDialog(copyEntry) }] : []),
           ...(runEntry ? [runItem(runEntry, isReadOnlyProfile(entries, row.profileId))] : []),
         ];
         const items = [...dataItems, ...(row.node.kind === BROWSABLE_KIND ? collectionItems(row, engine?.documents === true) : [])];
```

- [ ] **Step 3: The table tab.**

```diff
diff --git a/src/features/table-data/tableTabActions.ts b/src/features/table-data/tableTabActions.ts
index 44af469..789e42c 100644
--- a/src/features/table-data/tableTabActions.ts
+++ b/src/features/table-data/tableTabActions.ts
@@ -1,7 +1,10 @@
 import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";
 
-/** A table tab's own actions besides grid editing (⌘⇧P): Dump table… with the tab's filter (spec ⑥ §3.6). */
-export type TableTabActionId = "dumpTable";
+/**
+ * A table tab's own actions besides grid editing (⌘⇧P): Dump table… and Copy rows to… with the tab's filter (spec ⑥
+ * §3.6; 06c).
+ */
+export type TableTabActionId = "dumpTable" | "copyRows";
 export type TableTabActions = Record<TableTabActionId, TabAction>;
 
 const registry = createTabActionRegistry<TableTabActionId>();
```

```diff
diff --git a/src/features/table-data/TableDataTab.tsx b/src/features/table-data/TableDataTab.tsx
index 2951635..76eda4a 100644
--- a/src/features/table-data/TableDataTab.tsx
+++ b/src/features/table-data/TableDataTab.tsx
@@ -17,8 +17,8 @@ import { useReportTabStatus } from "@/stores/tabStatus";
 import { useWorkspace, type TableTabState } from "@/stores/workspace";
 import { useEngineInfo, useProfiles } from "../connections/hooks";
 import { RecoverableError } from "../connections/RecoverableError";
-import { openDumpDialog } from "../data-tools/dataDialogs";
-import { dumpEntryForTable } from "../data-tools/entryPoints";
+import { openCopyDialog, openDumpDialog } from "../data-tools/dataDialogs";
+import { copyEntryForTable, dumpEntryForTable } from "../data-tools/entryPoints";
 import { COMMIT_RUNNING } from "../edits/commitDialog";
 import { tableEditActions, useTableEditActions } from "../edits/editActions";
 import { EditBar } from "../edits/EditBar";
@@ -53,9 +53,11 @@ import { useTableData, type TableDataState } from "./useTableData";
 type View = "data" | "columns" | "ddl";
 const FILTER_BY_VALUE = "Filter by this value";
 const CUT_VALUE_MESSAGE = "This value cannot be matched exactly: the grid holds only its beginning";
-/** Dump table…'s tooltip while a filter is applied: the dialog offers its rows only, or all of them. */
-const DUMP_WITH_FILTER = "With the rows matching the filter, or all of them";
+/** Dump table…'s and Copy rows to…'s tooltip while a filter is applied: the dialog offers its rows only, or all of
+ * them. */
+const WITH_FILTER = "With the rows matching the filter, or all of them";
 const NO_DUMP = "This table cannot be dumped from here";
+const NO_COPY = "This table cannot be copied from here"; // an engine that does not copy (06c)
 const VIEWS: { value: View; label: string }[] = [
   { value: "data", label: "Data" },
   { value: "columns", label: "Columns" },
@@ -337,7 +339,18 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   const openDump = useCallback(() => {
     if (dumpEntry) openDumpDialog(dumpEntry);
   }, [dumpEntry]);
-  useTableTabActions(tab.id, { dumpTable: { enabled: dumpEntry !== null, run: openDump } });
+  // Copy rows to… (spec ⑥ §3.6, 06c), with the filter applied now; the dialog says when the tab has staged changes
+  const copyEntry = useMemo(
+    () => copyEntryForTable(tab.profileId, engine, tab.path, state.filter, tab.id),
+    [tab.profileId, tab.id, tab.path, engine, state.filter],
+  );
+  const openCopy = useCallback(() => {
+    if (copyEntry) openCopyDialog(copyEntry);
+  }, [copyEntry]);
+  useTableTabActions(tab.id, {
+    dumpTable: { enabled: dumpEntry !== null, run: openDump },
+    copyRows: { enabled: copyEntry !== null, run: openCopy },
+  });
   const moreItems: MenuItem[] = [
     {
       label: "Dump table…",
@@ -347,10 +360,22 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
           ? NO_DUMP
           : "Reading the connection's details…"
         : dumpEntry.filter !== null
-          ? DUMP_WITH_FILTER
+          ? WITH_FILTER
           : undefined,
       onSelect: openDump,
     },
+    {
+      label: "Copy rows to…",
+      disabled: !copyEntry,
+      title: !copyEntry
+        ? engine
+          ? NO_COPY
+          : "Reading the connection's details…"
+        : copyEntry.source.kind === "relation" && copyEntry.source.filter !== null
+          ? WITH_FILTER
+          : undefined,
+      onSelect: openCopy,
+    },
   ];
   // ⌘S while this tab's Data view is shown, wherever the focus is in it (an editor stages first and asks itself):
   // never the browser's Save page, nothing while a dialog has the keyboard or an input method composes.
```

- [ ] **Step 4: A query's result.**

```diff
diff --git a/src/features/grid/StatusBar.tsx b/src/features/grid/StatusBar.tsx
index 32b7d86..590ce10 100644
--- a/src/features/grid/StatusBar.tsx
+++ b/src/features/grid/StatusBar.tsx
@@ -1,5 +1,6 @@
-import { Download } from "lucide-react";
+import { ArrowRightLeft, Download } from "lucide-react";
 import { memo } from "react";
+import { cn } from "@/lib/cn";
 import { plural } from "@/lib/format";
 import type { Aggregates } from "./aggregate";
 
@@ -13,6 +14,8 @@ interface StatusBarProps {
   aggregates: Aggregates | null;
   /** Opens the export dialog (shown only when set). */
   onExport?: () => void;
+  /** Opens the copy dialog for the whole result (shown only when set; 06c). */
+  onCopyToTable?: () => void;
 }
 
 /** Up to 15 significant digits: a double's precision, without float noise such as 0.30000000000000004. */
@@ -47,7 +50,7 @@ function Figures({ aggregates: a }: { aggregates: Aggregates }) {
 
 /** Under the grid: the size of the result, the selected cell count with its numeric figures, and Export. */
 export const StatusBar = memo(function StatusBar(props: StatusBarProps) {
-  const { rowCount, columnCount, hasMore, partial, aggregates, onExport } = props;
+  const { rowCount, columnCount, hasMore, partial, aggregates, onExport, onCopyToTable } = props;
   return (
     <div className="flex h-6 shrink-0 items-center gap-4 border-y border-border px-3 text-[12px] whitespace-nowrap text-muted">
       <span className="shrink-0">
@@ -64,7 +67,8 @@ export const StatusBar = memo(function StatusBar(props: StatusBarProps) {
         {onExport && (
           <button
             type="button"
-            className="-mr-2 flex h-5 shrink-0 items-center gap-1 rounded px-1.5 hover:bg-hover hover:text-fg"
+            // the last button reaches the bar's edge (-mr-2); Copy to table… is last when it is there
+            className={cn("flex h-5 shrink-0 items-center gap-1 rounded px-1.5 hover:bg-hover hover:text-fg", !onCopyToTable && "-mr-2")}
             title="Export the whole result to a file"
             onClick={onExport}
           >
@@ -72,6 +76,17 @@ export const StatusBar = memo(function StatusBar(props: StatusBarProps) {
             Export…
           </button>
         )}
+        {onCopyToTable && (
+          <button
+            type="button"
+            className="-mr-2 flex h-5 shrink-0 items-center gap-1 rounded px-1.5 hover:bg-hover hover:text-fg"
+            title="Copy the whole result into a table of any connection"
+            onClick={onCopyToTable}
+          >
+            <ArrowRightLeft size={12} />
+            Copy to table…
+          </button>
+        )}
       </span>
     </div>
   );
```

```diff
diff --git a/src/features/grid/GridMenu.tsx b/src/features/grid/GridMenu.tsx
index ea54c8e..2a5520f 100644
--- a/src/features/grid/GridMenu.tsx
+++ b/src/features/grid/GridMenu.tsx
@@ -34,6 +34,8 @@ export interface GridMenuActions {
   extraCellMenu?: (row: number, column: number) => MenuItem[];
   /** "Export…": the whole result to a file (shown only when set). */
   openExport?: () => void;
+  /** "Copy result to a table…": the whole result into a table of any connection (shown only when set; 06c). */
+  openCopyToTable?: () => void;
   /** An editing grid's items (`GridEditing.menu`). */
   editItems?: EditItems;
   /** Return edits the active cell (the grid is editable now), so View value loses its ↵. */
@@ -77,7 +79,7 @@ function headerItems(col: number, a: GridMenuActions): MenuItem[] {
 
 /** Copy, Copy as, Export, the drawers; for a cell (`cell` set) also pinning and the caller's extra items. */
 function selectionItems(pos: CellPos | null, cell: boolean, a: GridMenuActions): MenuItem[] {
-  const { openValue, openRecord, openExport, editItems } = a;
+  const { openValue, openRecord, openExport, openCopyToTable, editItems } = a;
   const edits = editItems?.(pos, cell, a.rect) ?? [];
   const items: MenuItem[] = [
     ...edits,
@@ -87,6 +89,7 @@ function selectionItems(pos: CellPos | null, cell: boolean, a: GridMenuActions):
       children: a.copyFormats.map((format) => ({ label: COPY_FORMAT_LABELS[format], onSelect: () => a.copy(format) })),
     },
     ...(openExport ? [{ label: "Export…", onSelect: openExport }] : []),
+    ...(openCopyToTable ? [{ label: "Copy result to a table…", onSelect: openCopyToTable }] : []),
     {
       label: "View value",
       shortcut: a.returnEdits ? undefined : "↵",
```

```diff
diff --git a/src/features/grid/DataGrid.tsx b/src/features/grid/DataGrid.tsx
index 4e93f44..6ff43c7 100644
--- a/src/features/grid/DataGrid.tsx
+++ b/src/features/grid/DataGrid.tsx
@@ -104,6 +104,8 @@ export interface DataGridProps {
    * export outlives a remounting grid.
    */
   onExport?: () => void;
+  /** "Copy result to a table…" (context menu and status bar; spec ⑥ §3.6, 06c); none: not offered. */
+  onCopyToTable?: () => void;
   /** An editing table's marks and editing (spec ⑤ §3.6); absent: a read-only grid, as every query tab's is. */
   edits?: GridEditing;
   ref?: Ref<DataGridHandle>;
@@ -157,7 +159,8 @@ function LoadMoreFooter({ rowCount, hasMore, loading, onLoadMore }: LoadMoreFoot
  */
 export function DataGrid(props: DataGridProps) {
   const { columns, rows, hasMore, sort, onHeaderClick, stale = false, ref } = props;
-  const { dialect, tableName, onSortBy, extraCellMenu, onSelectionChange, onExport, partial = false, edits } = props;
+  const { dialect, tableName, onSortBy, extraCellMenu, onSelectionChange, onExport, onCopyToTable, partial = false, edits } =
+    props;
   const leading = edits?.leading ?? 0;
   // Stale rows are only shown: nothing is loaded for them, and nothing is edited on them.
   const loadingMore = props.loadingMore && !stale;
@@ -416,6 +419,7 @@ export function DataGrid(props: DataGridProps) {
                 sortBy={onSortBy}
                 extraCellMenu={extraCellMenu}
                 openExport={onExport}
+                openCopyToTable={onCopyToTable}
               >
                 <div
                   ref={scrollRef}
@@ -537,6 +541,7 @@ export function DataGrid(props: DataGridProps) {
         partial={partial}
         aggregates={aggregates}
         onExport={onExport}
+        onCopyToTable={onCopyToTable}
       />
     </div>
   );
```

```diff
diff --git a/src/features/query/ResultGrid.tsx b/src/features/query/ResultGrid.tsx
index 52a505d..fc03c2a 100644
--- a/src/features/query/ResultGrid.tsx
+++ b/src/features/query/ResultGrid.tsx
@@ -1,6 +1,6 @@
 import { memo, useCallback } from "react";
 import type { MenuItem } from "@/components/ui/ContextMenu";
-import type { Dialect } from "@/lib/api/types";
+import type { ColumnMeta, Dialect } from "@/lib/api/types";
 import { cn } from "@/lib/cn";
 import { askOpenDocument, idPrefill, prefillKeyspace } from "../documents/openDocument";
 import { DataGrid } from "../grid/DataGrid";
@@ -25,6 +25,11 @@ export interface ResultGridProps {
   exportable: boolean;
   /** Opens the tab's export dialog for this statement and what it was sent with; stable across renders. */
   onExport: (sql: string, request: RequestOptions) => void;
+  /**
+   * Opens the copy dialog for this statement's result, with its columns (spec ⑥ §3.6, 06c); stable across renders.
+   * Absent: the connection's engine does not copy.
+   */
+  onCopyToTable?: (sql: string, columns: ColumnMeta[]) => void;
   controller: SessionController | null;
   pageSize: number;
   dialect: Dialect | null;
@@ -38,10 +43,11 @@ export interface ResultGridProps {
  * changed (the reducer keeps the identity of every untouched execution and set), not every grid of the run.
  */
 export const ResultGrid = memo(function ResultGrid(props: ResultGridProps) {
-  const { set, requestId, sql, request, controller, pageSize, canLoadMore, exportable, onExport } = props;
+  const { set, requestId, sql, request, controller, pageSize, canLoadMore, exportable, onExport, onCopyToTable } = props;
   const { documentsProfileId } = props;
   const loadMore = useCallback(() => controller?.fetchMore(requestId, pageSize), [controller, requestId, pageSize]);
   const exportThis = useCallback(() => onExport(sql, request), [onExport, sql, request]);
+  const copyThis = useCallback(() => onCopyToTable?.(sql, set.columns), [onCopyToTable, sql, set.columns]);
   // "Open document…": the ID is the row's `id` column or the clicked text, the collection the one the statement reads
   // (when it reads exactly one), else the run's context and the collection opened last (ruling C44).
   const openDocumentItems = useCallback(
@@ -72,6 +78,7 @@ export const ResultGrid = memo(function ResultGrid(props: ResultGridProps) {
         dialect={props.dialect}
         extraCellMenu={documentsProfileId === null ? undefined : openDocumentItems}
         onExport={controller && exportable ? exportThis : undefined}
+        onCopyToTable={controller && exportable && onCopyToTable && set.columns.length ? copyThis : undefined}
       />
     </div>
   );
```

```diff
diff --git a/src/features/query/ResultsArea.tsx b/src/features/query/ResultsArea.tsx
index f0badab..c817127 100644
--- a/src/features/query/ResultsArea.tsx
+++ b/src/features/query/ResultsArea.tsx
@@ -1,5 +1,5 @@
 import { useCallback, useMemo, useState } from "react";
-import type { Dialect } from "@/lib/api/types";
+import type { ColumnMeta, Dialect } from "@/lib/api/types";
 import { sameElements } from "@/lib/arrays";
 import { cn } from "@/lib/cn";
 import { PlanResult } from "../explain/PlanResult";
@@ -86,6 +86,8 @@ export interface ResultsAreaProps extends Omit<RunMessagesProps, "run"> {
   documentsProfileId: string | null;
   /** Opens the tab's export dialog for a result's statement and what it was sent with; stable across renders. */
   onExport: (sql: string, request: RequestOptions) => void;
+  /** Opens the copy dialog for a result's statement and columns (06c); absent: the engine does not copy. */
+  onCopyToTable?: (sql: string, columns: ColumnMeta[]) => void;
 }
 
 /**
@@ -106,6 +108,7 @@ export function ResultsArea({
   plans,
   documentsProfileId,
   onExport,
+  onCopyToTable,
   ...messages
 }: ResultsAreaProps) {
   const tabs = useMemo(() => resultTabs(run, executions), [run, executions]);
@@ -164,6 +167,7 @@ export function ResultsArea({
               canLoadMore={canLoadMore}
               exportable={!run.active}
               onExport={onExport}
+              onCopyToTable={onCopyToTable}
               controller={controller}
               pageSize={pageSize}
               dialect={dialect}
```

```diff
diff --git a/src/features/query/QueryTab.tsx b/src/features/query/QueryTab.tsx
index 6ce2db7..1ec49ee 100644
--- a/src/features/query/QueryTab.tsx
+++ b/src/features/query/QueryTab.tsx
@@ -5,7 +5,7 @@ import { Group, Panel } from "react-resizable-panels";
 import { PanelHandle } from "@/components/ui/PanelHandle";
 import { toast } from "@/components/ui/Toast";
 import { errorMessage } from "@/lib/api/client";
-import type { Dialect } from "@/lib/api/types";
+import type { ColumnMeta, Dialect } from "@/lib/api/types";
 import { isImeKeyEvent } from "@/lib/ime";
 import { isCommandKey } from "@/lib/shortcuts";
 import { markEdited, readEditorBuffer, registerEditor, rememberEditorBuffer } from "@/stores/editorBuffers";
@@ -15,6 +15,8 @@ import { startSelection, useWorkspace, type QueryTabState } from "@/stores/works
 import { useEngineInfo, useProfiles } from "../connections/hooks";
 import { profileColor } from "../connections/profileColors";
 import { RecoverableError } from "../connections/RecoverableError";
+import { openCopyDialog } from "../data-tools/dataDialogs";
+import { copyEntryForResult } from "../data-tools/entryPoints";
 import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
 import { sqlCompletion, type CompletionTarget } from "../editor/completion";
 import { describeOnDemand } from "../editor/describe";
@@ -233,6 +235,16 @@ export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean
     (sql: string, request: RequestOptions) => setExportRequest({ sql, request, open: true }),
     [],
   );
+  // Copy result to a table… (spec ⑥ §3.6, 06c): the statement runs again on this tab's session, as an export does
+  const sessionId = snapshot?.info?.session_id ?? null;
+  const copyToTable = useCallback(
+    (sql: string, columns: ColumnMeta[]) => {
+      if (!tab.profileId || !sessionId) return;
+      const entry = copyEntryForResult(tab.profileId, engine, tab.database, sessionId, sql, columns, tab.title);
+      if (entry) openCopyDialog(entry);
+    },
+    [tab.profileId, tab.database, tab.title, sessionId, engine],
+  );
 
   /** A run that cannot start for want of a parameter's value: says which, and opens the panel on its field. */
   const refuseRun = (problem: ParamProblem) => {
@@ -557,6 +569,7 @@ export function QueryTab({ tab, active }: { tab: QueryTabState; active: boolean
               plans={plans}
               documentsProfileId={engine?.documents && tab.profileId !== null ? tab.profileId : null}
               onExport={openExport}
+              onCopyToTable={engine?.copy_data ? copyToTable : undefined}
               locate={locate}
               onJump={jump}
               onReconnect={retry}
```

- [ ] **Step 5: The palette.**

```diff
diff --git a/src/features/palette/commands.ts b/src/features/palette/commands.ts
index d3ec232..2d574af 100644
--- a/src/features/palette/commands.ts
+++ b/src/features/palette/commands.ts
@@ -5,8 +5,8 @@ import type { AppSettings, EngineInfo, Profile, ProfileEntry } from "@/lib/api/t
 import type { TabAction } from "@/lib/tabActions";
 import { openNewQuery, useWorkspace, type Tab } from "@/stores/workspace";
 import { enginesKey, findEngineInfo, profilesKey } from "../connections/hooks";
-import { openDumpDialog, openRunDialog, setJobsPopoverOpen } from "../data-tools/dataDialogs";
-import { paletteDumpEntry, paletteRunEntry } from "../data-tools/entryPoints";
+import { openCopyDialog, openDumpDialog, openRunDialog, setJobsPopoverOpen } from "../data-tools/dataDialogs";
+import { paletteCopyEntry, paletteDumpEntry, paletteRunEntry } from "../data-tools/entryPoints";
 import { useJobs } from "../data-tools/jobsStore";
 import { readTableEditActions, type TableEditActionId, type TableEditActions } from "../edits/editActions";
 import { refreshSchema } from "../explorer/refreshSchema";
@@ -153,6 +153,28 @@ const dumpCommand: PaletteCommand = {
   },
 };
 
+/**
+ * ⌘⇧P Copy data to… (spec ⑥ §3.6, 06c): a table tab copies its rows through its own handler (it knows its filter); a
+ * query tab its schema's tables (its database's default schema; MySQL: its database). Listed for engines that copy.
+ */
+const copyCommand: PaletteCommand = {
+  id: "dataTools.copy",
+  label: "Copy data to…",
+  visible: (ctx) => activeEngine(ctx)?.copy_data === true,
+  enabled: (ctx) =>
+    ctx.activeTab?.kind === "table"
+      ? (ctx.tableActions?.copyRows.enabled ?? false)
+      : paletteCopyEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "") !== null,
+  run: (ctx) => {
+    if (ctx.activeTab?.kind === "table") {
+      runTabAction(readTableTabActions(ctx.activeTab.id)?.copyRows, "Copy data to…");
+      return;
+    }
+    const entry = paletteCopyEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "");
+    if (entry) openCopyDialog(entry);
+  },
+};
+
 /** Couchbase: its plans (Explain). */
 const takesPlans = (ctx: CommandContext) => activeEngine(ctx)?.plan_format === "couchbase";
 /** Couchbase: the plan a statement ran with (Run with profile). */
@@ -219,6 +241,7 @@ export const COMMANDS: readonly PaletteCommand[] = [
     },
   },
   dumpCommand,
+  copyCommand,
   {
     id: "dataTools.runFile",
     label: "Run SQL file…",
```

- [ ] **Step 6: Run the checks.** `npm test` → all pass (dry run: 879 in 47 files); `npm run typecheck` → clean; `npm run build` → clean (dry run: main chunk 1,549.50 kB, gzip 497.79 kB; 06b ended at 1,526.60 kB, gzip 492.29 kB).

- [ ] **Step 7: Commit.**

```bash
git add src/features/explorer/Explorer.tsx src/features/table-data/TableDataTab.tsx \
  src/features/table-data/tableTabActions.ts src/features/grid/DataGrid.tsx src/features/grid/GridMenu.tsx \
  src/features/grid/StatusBar.tsx src/features/query/QueryTab.tsx src/features/query/ResultGrid.tsx \
  src/features/query/ResultsArea.tsx src/features/palette/commands.ts src/features/palette/dataCommands.test.ts
git commit -m "feat(06c): Copy data to… from the tree, the table tab, a query's result and ⌘⇧P"
```

- [ ] **Step 8: Manual checks (the coordinator; then Task 7's list).** `scripts/dev.sh --web`, `http://localhost:1420`, the scratch tables of Task 7's list.
  1. **The tree.** On dev pg: the database node has Dump… and Run SQL file… but no Copy data to…; `public`, its Tables folder, a table, a view and a materialized view each have "Copy data to…" after Dump…. On "zz_06c sqlite" only `main` has it. dev couchbase's nodes have none. A read-only connection's nodes have it (a copy only reads its source).
  2. **The table tab.** A dev pg table tab's ⋯ lists "Copy rows to…" after "Dump table…"; with a filter applied its tooltip is "With the rows matching the filter, or all of them", and the dialog's Rows line shows the filter, checked. Stage an edit in the tab (any cell): the dialog says "Staged changes are not copied: the copy reads what the server holds".
  3. **A query's result.** In a dev pg query tab run `CREATE TEMP TABLE zz_06c_tmp AS SELECT 1 AS id, 'a' AS label; SELECT * FROM zz_06c_tmp;` The result's status bar shows "Copy to table…" after "Export…", and the grid's right-click menu "Copy result to a table…" after "Export…". The dialog's title is "Copy the result of <tab title> to a table", Table is `query_result` (rename it `zz_06c_result`), and the preview's warning says the query runs again when the copy starts, as an export does. Copy into "zz_06c sqlite": "Copied 1 row to zz_06c_result" (the temporary table was read on the tab's own session). A statement that writes (`UPDATE zz_06c_orders SET note = note RETURNING *`): its Copy result to a table… opens, and the preview refuses it in red ("Only a statement that returns rows (SELECT, WITH, VALUES…) can be copied, and one that only reads"), Copy disabled.
  4. **⌘⇧P.** On a dev pg table tab with a filter: "Copy data to…" opens the dialog with the filter. On a dev pg query tab: it opens the copy of `public`'s tables. On a dev couchbase tab it is not listed.
  5. **B13.** Start a long copy (zz_06c_big from dev pg into dev mssql). While it runs, delete dev pg (the source) from the tree: the toast says "Profile not deleted — a job is still running on it"; the same for dev mssql (the target). Cancel the copy; both deletions are possible again (cancel them in their confirmation).
  6. **After a reload.** Start the long copy again, reload the window: Jobs still lists it running; reopen it: progress, Cancel; after it ends, its view has no "Copy again…" (a job listed after a reload has no command, as 06b's).
  7. **Nothing left.** As Task 7's step 10.

---

### Task 9: Closing ⑥ — both suites, the packaged app, a 1,000,000-row copy in the frozen sidecar, CLAUDE.md and the spec

**Suggested implementer:** Sonnet (with the coordinator for the packaged app's check).

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`
- Modify (only if Step 3 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/t9_probe.py`

**Interfaces:**
- Consumes: everything above; 06a Task 10's procedure (the frozen sidecar in `src-tauri/resources/backend`, `scripts/measure-startup.sh`); `tests/data_support` (`SCRATCH_06C`, `scratch_name`), `tests/edit_support.scratch_db`.
- Produces: §7's Copy row measured in the frozen sidecar; the packaged app built and measured after 06b's fix wave and 06c (the 06b follow-ups' "package and measure again when 06c lands"); 06c's facts in CLAUDE.md and the spec.

- [ ] **Step 1: Both suites.** With the dev containers up, no scratch object on any engine and nobody else running tests: `npm test` (all pass; dry run 879) and `cd backend && .venv/bin/pytest -q` (all pass: 06b ended at 6,292 passed with 2 Couchbase connect tests failing that 46ecdd6 then fixed, so 6,294; 06c adds 135, about 6,429: `test_type_map` 81, `test_transfer_sql` 4, `test_transfer_plan` 10, `test_transfer_pipe` 5, `test_copy_framework` 8, `test_transfer_job` 11, `test_copy_matrix` 16). The full suite's exact-list introspection tests fail if a `zz_06c_*` object is left over: drop it by exact name, never loosen the test.

- [ ] **Step 2: The packaged app.** `df -h /` shows at least 5 GiB free; `npm run build:app`; `codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`. Record the sidecar's size (06b: 49,552 KB; 06c adds about 2,300 lines of pure Python and no dependency: expect roughly +100 KB) and the JS main chunk (dry run: 1,549.50 kB, gzip 497.79 kB; 06b 1,526.60 / 492.29 kB).

- [ ] **Step 3: A 1,000,000-row copy in the frozen sidecar (spec §7's Copy row).** Save as `<scratchpad>/t9_probe.py` and run from the repository root: `backend/.venv/bin/python <scratchpad>/t9_probe.py <scratchpad> 1000000 src-tauri/resources/backend/dbaguy-backend`. It takes about 3 minutes. It starts the sidecar as its own child with a throwaway `DBAGUY_DATA_DIR`; profiles are saved without passwords and unlocked for this run; it fills a `zz_06c_big_*` PostgreSQL table, copies it (create) into MySQL, SQL Server and a new SQLite file in `<scratchpad>`, then that SQLite table back into PostgreSQL, each through `POST /data/transfer/plan` and a `copy_data` job on the target's own session; it compares the count, the sum of the ids, the non-NULL notes, and one row's text and amount on every target; it drops every `zz_06c_*` table by exact name, deletes the SQLite file and stops the sidecar by its own handle.

```python
"""06c packaged check and §7's copy budget: the frozen sidecar (started here as a child, with a throwaway
DBAGUY_DATA_DIR, and stopped here by its own handle) copies a 1,000,000-row PostgreSQL table into MySQL, SQL Server and
a new SQLite file, then that SQLite table back into PostgreSQL, each through POST /data/transfer/plan and a copy_data
job on the target's own session over its WebSocket, sampling the sidecar's RSS. Only zz_06c_* scratch tables (and the
SQLite file in <workdir>), dropped or deleted by exact name.

Run from the repository root: backend/.venv/bin/python <this file> <workdir> <rows> <sidecar command…>"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

sys.path.insert(0, "backend")
from tests.data_support import SCRATCH_06C, scratch_name  # noqa: E402
from tests.edit_support import scratch_db  # noqa: E402

TOKEN = "t9"
LABEL = "row label it''s 中文 😀 " + "x" * 120
FILL = ("INSERT INTO {t} SELECT g, '{l}' || g, g * 1.25, timestamp '2026-01-01' + g * interval '1 second', g / 3.0, "
        "CASE WHEN g % 10 = 0 THEN NULL ELSE 'note ' || g END FROM generate_series(1, {n}) g")
BUDGET_S = {"postgres": 60, "mysql": 60, "mssql": 150, "sqlite": 60}  # spec §7: copy from another engine, by target
BUDGET_MB = 40

work = Path(sys.argv[1]).resolve()
rows = int(sys.argv[2])
command = sys.argv[3:]
data_dir = tempfile.mkdtemp(prefix="dbaguy-t9-")
env = {**os.environ, "DBAGUY_TOKEN": TOKEN, "DBAGUY_PORT": "0", "DBAGUY_LOG_TO_FILE": "false",
       "DBAGUY_DATA_DIR": data_dir, "DBAGUY_LOG_LEVEL": "WARNING"}
sidecar = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
port = json.loads(sidecar.stdout.readline())["port"]
print(f"sidecar pid {sidecar.pid} on port {port}", flush=True)


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


def rss_mb() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(sidecar.pid)], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024


def add_profile(db) -> str:
    body = db.profile(name=f"t9 {db.engine}")
    secrets = body.pop("secrets", None)  # never saved: given to this run only (unlock)
    if secrets is not None:
        body["profile"]["save_password"] = False
    status, entry = call("POST", "/profiles", body)
    assert status == 201, entry
    profile_id = entry["profile"]["id"]
    if secrets is not None:
        assert call("POST", f"/profiles/{profile_id}/unlock", secrets)[0] == 204
    return profile_id


def sqlite_profile(path: Path) -> str:
    status, entry = call("POST", "/profiles", {"profile": {"engine": "sqlite", "name": "t9 sqlite", "path": str(path)}})
    assert status == 201, entry
    return entry["profile"]["id"]


def copy(source: tuple[str, str | None, list], target: tuple[str, str | None, str], table: str):
    """One copy (create): (job_outcome, seconds, RSS before, peak RSS while it ran)."""
    (source_profile, source_database, path), (target_profile, target_database, schema) = source, target
    request = {"source": {"profile_id": source_profile, "database": source_database, "tables": [path]},
               "target": {"profile_id": target_profile, "database": target_database, "schema_name": schema,
                          "table": table, "mode": "create"}}
    status, plan = call("POST", "/data/transfer/plan", request)
    assert status == 200 and plan["plan_id"], plan
    status, info = call("POST", "/sessions", {"profile_id": target_profile, "database": target_database})
    assert status == 201, info
    session = info["session_id"]
    before, peak, done = rss_mb(), [0.0], threading.Event()

    def sample() -> None:
        while not done.is_set():
            peak[0] = max(peak[0], rss_mb())
            time.sleep(0.25)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    outcome = None
    with connect(f"ws://127.0.0.1:{port}/sessions/{session}/ws?token={TOKEN}", max_size=None) as ws:
        ws.send(json.dumps({"type": "copy_data", "request_id": "j", "plan_id": plan["plan_id"],
                            "source_session_id": None}))
        while outcome is None:
            event = json.loads(ws.recv(timeout=1800))
            if event["type"] == "job_outcome":
                outcome = event
    took = time.monotonic() - started
    done.set()
    sampler.join()
    call("DELETE", f"/sessions/{session}")
    return outcome, took, before, max(peak[0], rss_mb())


def facts(db, table: str) -> tuple:
    """What every copy keeps: the count, the ids, the NULLs, one row's text and amount as text."""
    t = db.table(table)
    count, ids, notes = db.rows(f"SELECT COUNT(*), SUM(id), COUNT(note) FROM {t}")[0]
    label, amount = db.rows(f"SELECT label, amount FROM {t} WHERE id = 777")[0]
    return int(count), int(ids), int(notes), label, str(amount)


def report(name: str, target_engine: str, outcome: dict, took: float, before: float, peak: float) -> None:
    assert outcome["outcome"] == "completed" and outcome["rows"] == rows, outcome
    print(f"{name}: {took:.1f} s (budget {BUDGET_S[target_engine]} s), {rows / took:,.0f} rows/s, "
          f"{outcome['statements']:,} INSERTs; sidecar RSS {before:.0f} -> peak {peak:.0f} MB "
          f"(+{peak - before:.0f}, budget {BUDGET_MB})", flush=True)


pg, my, ms = (scratch_db(engine, work) for engine in ("postgres", "mysql", "mssql"))
lite_path = work / "t9_copy.sqlite"
lite_path.unlink(missing_ok=True)
sqlite3.connect(lite_path).close()
name, back = scratch_name("big", SCRATCH_06C), scratch_name("back", SCRATCH_06C)
assert name.startswith(SCRATCH_06C) and back.startswith(SCRATCH_06C)
made: list[tuple[object, str]] = []
try:
    pg.execute(f"CREATE TABLE {pg.table(name)} (id bigint PRIMARY KEY, label text, amount numeric(20,2), "
               "at timestamp, ratio double precision, note text)")
    made.append((pg, name))
    pg.execute(FILL.format(t=pg.table(name), l=LABEL, n=rows))
    expected = facts(pg, name)
    pg_id, my_id, ms_id = add_profile(pg), add_profile(my), add_profile(ms)
    lite_id = sqlite_profile(lite_path)
    source = (pg_id, "dbaguy", [s.model_dump(mode="json") for s in pg.path(name)])
    for db, profile_id, database, schema in ((my, my_id, None, "dbaguy"), (ms, ms_id, "dbaguy", "dbo")):
        made.append((db, name))
        outcome, took, before, peak = copy(source, (profile_id, database, schema), name)
        report(f"postgres -> {db.engine}", db.engine, outcome, took, before, peak)
        assert facts(db, name) == expected, (db.engine, facts(db, name), expected)
    outcome, took, before, peak = copy(source, (lite_id, None, "main"), name)
    report("postgres -> sqlite", "sqlite", outcome, took, before, peak)
    with sqlite3.connect(lite_path) as check:
        found = check.execute(f'SELECT COUNT(*), SUM(id), COUNT(note) FROM "{name}"').fetchone()
        label, amount = check.execute(f'SELECT label, amount FROM "{name}" WHERE id = 777').fetchone()
    assert (*found, label, str(amount)) == expected, (found, label, amount, expected)
    path = [{"kind": "schema", "name": "main"}, {"kind": "folder", "name": "tables"}, {"kind": "table", "name": name}]
    made.append((pg, back))
    outcome, took, before, peak = copy((lite_id, None, path), (pg_id, "dbaguy", "public"), back)
    report("sqlite -> postgres", "postgres", outcome, took, before, peak)
    assert facts(pg, back) == expected, (facts(pg, back), expected)
finally:
    for db, table in reversed(made):
        assert table.startswith(SCRATCH_06C)
        db.execute(f"DROP TABLE IF EXISTS {db.table(table)}")
    for db in (pg, my, ms):
        db.close()
    lite_path.unlink(missing_ok=True)
    sidecar.terminate()
    sidecar.wait(timeout=30)
    print("sidecar stopped", flush=True)
```

Expected (planning ran it with an unfrozen sidecar, `python -m dbaguy_backend` from the scratch worktree, load average about 4):

```
postgres -> mysql: 19.2 s (budget 60 s), 52,111 rows/s, 1,000 INSERTs; sidecar RSS 93 -> peak 106 MB (+12, budget 40)
postgres -> mssql: 87.2 s (budget 150 s), 11,471 rows/s, 10,000 INSERTs; sidecar RSS 77 -> peak 80 MB (+2, budget 40)
postgres -> sqlite: 12.3 s (budget 60 s), 81,074 rows/s, 1,000 INSERTs; sidecar RSS 63 -> peak 73 MB (+10, budget 40)
sqlite -> postgres: 19.6 s (budget 60 s), 51,047 rows/s, 1,000 INSERTs; sidecar RSS 69 -> peak 73 MB (+4, budget 40)
sidecar stopped
```

Read the RSS as CLAUDE.md's measurements do: the "before" is wherever macOS left the idle sidecar, so the increase depends on it; the peak is the working set. Over a budget: record it, report it when over twice, and leave the speed-ups to spec §12 (SQL Server's several statements a batch). A `ModuleNotFoundError` in the sidecar's stderr: add the module to `hiddenimports` in `backend/dbaguy-backend.spec`, rebuild, run again (planning expects none: stdlib and the existing drivers only). Disk: each target holds about 250–300 MB while its copy runs, dropped right after; MySQL's binary log keeps about as much (as 06a's restores did).

- [ ] **Step 4: Startup and memory.** `scripts/measure-startup.sh` three times (the first is the first launch after the build). Compare with 06b (cold start 1.25–1.47 s, first launch 2.54 s; `footprint` sum 169–172 MB, backend 80–81 MB). Expected: no change beyond the run-to-run spread (the copy's code runs only when a copy does).

- [ ] **Step 5 (disk allowing): the 06b follow-ups' PostgreSQL run memory.** If `df -h /` still shows at least 5 GiB, run 06a Task 10's probe (`docs/superpowers/plans/2026-10-20-06a-data-tools-backend.md`, Task 10 Step 2) with its last loop narrowed to `check("postgres")`, against the new sidecar, and record the PostgreSQL run's RSS after 06a's F1 (`prepare_threshold=None`): 06a measured 101–146 MB before F1. Otherwise say it is still open.

- [ ] **Step 6: The packaged app's own checks are the user's** (Appendix A): the coordinator checks in the in-app browser only and nobody drives the native app.

- [ ] **Step 7: CLAUDE.md.** In the project's style (Chinese), using what the tasks built:
  - Repo 結構: backend — `adapters/families.py` (each engine's declared type → `TypeFamily`, the catalogs' and a query's), `TableColumn.custom_type`, `sql/type_map.py` (§3.5.2 target types, `type_problem`, §3.5.3 conversions; a value the target cannot hold fails its row), `sql/transfer_sql.py` (the target's and the source's settings, CREATE TABLE, DELETE, IDENTITY_INSERT, setval, History's text), `domain/transfer.py`, `services/plans.py` (`OneTimePlanStore`; `EditPlanStore` on it), `services/transfer_plans.py`, `services/transfer_service.py`, `POST /data/transfer/plan`, `sessions/jobs/{pipe, transfer_source, transfer}.py`, `CopyDataCommand`, `PreparedJob`, `SourceLease`, `InsertBatcher.abort_on_conflict`; frontend — `features/data-tools/{copyForm, mappingRows}.ts` (pure, Vitest in `copy.test.ts`), `CopyDataDialog.tsx`, `MappingTable.tsx`, `entryPoints`' copy entries, `runningOn`; `tests/{transfer_support, copy_matrix}.py`, scratch prefix `zz_06c_`.
  - 架構重點, the ⑥ block: a 06c paragraph — the architecture (R-06-8 as built: the sink on the target's job session, the source a helper job on a session the copy opens and closes, or the query tab's; `RowPipe` of 2 chunks; a Cancel held between tables), the type families in one place, the type map's rules and the two rules found while planning (PostgreSQL's NaN into SQLite fails its row; `timetz` into MySQL `TIME` drops the offset), the target session's settings (06a's restore settings, C4), the source's (set and set back, A11), the outcomes (R-06-12 per table, `value_not_copyable`, `row_count_mismatch`, `row_count_unknown`, MySQL's created table that stays), the one-time plan, the preview's rules (create, append, replace; FK order; identities; problems), the frontend (the dialog, the mapping table, the again-actions C17, the entries, C10, B13's `runningOn`).
  - 已知限制: SQL Server's `INSTEAD OF` triggers that skip rows are not caught (A16; ⑤ A3); a copy's created table has columns, NOT NULL and the primary key only (R-06-9); a query source runs again on its tab, ending a paused result (as an export); FK checks on PostgreSQL and SQL Server targets are row by row, so a cycle may fail (warned); MySQL's created table stays when its copy fails; no copy from a database node (C10); a job listed after a reload has no "Copy again…" (06b V6); Couchbase has no copy (R-06-14, CR-1).
  - 打包版量測: the 06c line (sidecar, chunk, cold start, `footprint`, backend; Step 3's copy times and RSS; Step 5's PostgreSQL run memory or that it is still open).
  - 進度: ⑥ "完成：06a、06b、06c…" with the plan's link and the follow-ups' (the coordinator writes the follow-ups note).
- [ ] **Step 8: The spec's 〔06c〕 notes**, next to the text they refine: the header's status line; §3.5.1 (the request as built: C1); §3.5.2 and §3.5.3 (the two rules; C13 custom types; C16); §3.5.4 (C4: the target session is strict, so no non-strict warning; C11 FK order and cycles; C12 not-copied columns; C15 inheritance); §3.5.5 (C2, C3, C5, C6, C8); §3.6 (CopyDataDialog and MappingTable as built; C9; the entries with C10; C17); §4 (C7's codes); §10 A9, A11, A16 (the results below); R-06-8, R-06-9, R-06-12 where C2–C6 change their text; §11 item 3 (06c: 9 tasks, this plan).
- [ ] **Step 9: Nothing left.** On PostgreSQL, MySQL and SQL Server, list tables (and PostgreSQL schemas) named `zz\_06c%`: none.
- [ ] **Step 10: Commit.**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-20-06-data-tools-design.md
git commit -m "docs(06c): CLAUDE.md and the spec's 06c notes; packaged measurements and the 1,000,000-row copy"
```

---

## Spec issues found while planning

Each row: what the spec says or leaves open, the ruling this plan builds, and the evidence. The final review checks the code against the rulings; Task 9 writes them into the spec as 〔06c〕 notes.

| Id | Issue | Ruling (built) | Evidence |
|---|---|---|---|
| C1 | §3.5.1's request: `TargetSpec.schema` shadows pydantic's `BaseModel.schema`; no way to leave a column out though R-06-10 says a column without a counterpart is "排除該欄" to run; `QuerySource` carries `params` and `query_context`, which only Couchbase has; `rows_estimate` has no use (a copy's progress shows rows copied, not a share). | `TargetSpec.schema_name`; `TargetSpec.left_out` (per source table, source columns); `QuerySource{sql, columns, title}` (Couchbase does not copy, R-06-14); `TableTransfer` gains `source_key` (what `overrides`/`identity`/`left_out` key by) and `source_path`, loses `rows_estimate`; `ColumnMapping` gains `type_editable`, `overridden`, `identity`, `problem`. | Task 2 `domain/transfer.py`; `test_a_type_without_a_counterpart_is_chosen_or_left_out`. |
| C2 | R-06-8 / D-06-8: `TransferCommand{plan_id, source_session_id}` with the source session always given; a table source's "新開的 job session" would be the frontend's to open, socket and close, and to leak on a reload. | `CopyDataCommand{plan_id, source_session_id: str \| None}` (type `copy_data`, its `JobKind`). None: the backend opens the source's session itself and closes it when the copy ends (`SessionManager._source`, `SourceLease.release`); a query's copy names its tab's session, which must be the plan's source profile's (`plan_mismatch`); a table plan with a session, or a query plan without one, is `plan_mismatch`; a source session that cannot be opened (a password, a server down, the tab gone) is "The source connection could not be opened: …" with that error's code, before anything is sent. | Task 3 `test_a_copys_source_is_its_query_tabs_session_or_one_of_its_own`; Task 4 `test_a_query_tabs_copy_reads_on_its_session_and_a_failing_read_is_the_sources`. |
| C3 | §3.5.5 step 2: the sink "經 `hooks.source_worker` 在來源 worker 提交 `TransferSource`" — how a job runs on another session's worker without its own command, job list entry or transaction refusal is not said. | `TransferSource` is a `Job` with `helper = True`, submitted as a `PreparedJob` (a command that is never a client's): the source worker runs it through `_run_job` like any job (cancel, arm, events) but never announces it to the job list and runs it inside the session's open transaction (a query tab's, as an export does). Its events go to the source session's socket under `<request id>:source:<k>`; a query tab ignores them (`SessionController` drops unknown request ids) apart from `session_state`. | Task 3 `test_a_helper_job_runs_inside_the_sessions_transaction_and_is_never_listed`. |
| C4 | §3.5.5 step 3: MySQL and SQLite turn FK checks off before the first table and back after; §3.5.4: warn when MySQL's `sql_mode` lacks `STRICT_TRANS_TABLES`. With a transaction per table, SQLite's `defer_foreign_keys` alone checks at each COMMIT; and a warning does not stop a truncation. | The target's job session runs 06a's `RESTORE_SETTINGS` (`transfer_sql.TARGET_SETUP`, quietly, before the first table; `TARGET_RESET` after): MySQL `SET NAMES utf8mb4`, `FOREIGN_KEY_CHECKS = 0`, `sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'`, `time_zone = '+00:00'`; PostgreSQL `standard_conforming_strings = on` and its encoding; SQL Server `IMPLICIT_TRANSACTIONS OFF`, `QUOTED_IDENTIFIER`, `ANSI_NULLS`; SQLite `foreign_keys = OFF` (outside any transaction, so it takes effect) and `defer_foreign_keys`. The session is strict, so a too-long value fails its statement (the table rolls back) instead of being cut: no warning needed. | 06a's restore settings (every dump's round trip restores through them); Task 2 `transfer_sql.TARGET_SETUP`; every copy in Tasks 4–5 runs through it (the matrix's MySQL targets read every value back uncut). |
| C5 | §3.5.3: "PG 來源 session 設 `TimeZone = 'UTC'`；SQL Server `datetimeoffset` 由 Python 換算". Changing the source's TimeZone changes a query tab's own session; and two rules for one conversion. A11 left open what a MySQL source does when the pipe is full. | Every source's offset value goes to UTC in Python (`type_map.to_utc`, every fractional digit kept), so nothing changes TimeZone. The source session's own settings are read, set for the copy and set back after each table (`transfer_sql.SOURCE_SETTINGS`): PostgreSQL `DateStyle = 'ISO, MDY'`, `IntervalStyle = 'postgres'` (texts `to_utc` and the other engines read); MySQL `time_zone = '+00:00'` (TIMESTAMP columns in UTC) and `net_write_timeout = 3600` (A11 below). They run quietly (`ctx.setting`: MySQL warns of a deprecated sql_mode, which its reader cannot act on). | A11; Task 3 `test_a_mysql_source_reads_in_utc_without_a_write_timeout_and_its_tab_gets_its_settings_back`; Task 1 `test_a_time_zones_value_goes_to_utc_where_the_column_has_none_never_rounded`. |
| C6 | §3.5.5 step 4: a Cancel rolls back the table being copied and the rest are not started. Between two tables the job calls `stop_cancels()` for the COMMIT (⑤ R-05-6), and a Cancel then reaches nothing: the next table would start. | The worker holds a Cancel of the running job while its statements cannot be cancelled; `JobContext.resume_cancels()` (each table after the first) honours it: that table and the rest are `not_started`, the outcome `cancelled`. | Task 3 `test_a_cancel_while_the_statements_cannot_be_cancelled_stops_the_next_table`, `test_without_a_cancel_the_next_table_goes_on`. |
| C7 | §4 lists `columns_changed`, `row_count_mismatch`, `source_failed`, and the problems `target_exists`, `unmappable_type`, `same_table`/`same_sqlite_file`, `invalid_type`. A row that cannot be written (§3.5.3: "那一列失敗，訊息指出表、行與欄") has no code; nor do the other ways a copy ends or is refused. | Job codes: `value_not_copyable` ("Row N of <source>, column <c>: <reason>"), ⑤'s `row_count_unknown`, run's `transaction_ended` and `commit_unconfirmed`, `query_not_select` (a query that does not only read, at run time). Problems: `target_missing`, `target_not_writable`, `source_missing`, `missing_column`, `unconvertible`, `no_columns`, `duplicate_column`, `invalid_filter`, `query_not_select`. The frontend labels each (`CODE_LABELS`). | Task 2 `domain/transfer.ProblemCode`; Task 4 `transfer.py`; Task 6 `jobReport.test.ts` "labels a copy's own codes…". |
| C8 | R-06-9: "key 衝突一律失敗、整張表 rollback". A SQLite table whose key says `ON CONFLICT REPLACE` (or `IGNORE`) would delete (or skip) a row and the INSERT still report every row (⑤ final review I1). | A copy writes SQLite's `INSERT OR ABORT` (`InsertBatcher(abort_on_conflict=True)`, ⑤'s `insert_head`): a conflict fails the table as R-06-9 says. | Task 4 `bulk_insert.py`; ⑤ I1's probe. |
| C9 | CR-3 asks a red connection for a checkbox before Run SQL file runs, because a file is not classified. Does a copy into a red connection need the same? | No box: a copy's statements are all shown in the preview before (CREATE TABLE, `DELETE FROM` in the danger colour, setval, IDENTITY_INSERT) and its INSERTs only add rows. A red target makes Copy a danger button (⑤ H24), as a red connection's grid commit. | Task 7 `CopyDataDialog` (`variant={red ? "danger" : "primary"}`), `MappingTable` (`statementLines`); Task 7 manual check 6. |
| C10 | §3.6's entries put "Copy data to…" on database nodes too; a copy's target is one schema, and a database's tables come from several schemas, whose same-named tables would meet there. | Not on a database node; on a schema, its Tables folder, a table, a view or a materialized view (SQLite: `main` only). A database is copied schema by schema. | Task 6 `entryPoints.test.ts` "offers nothing on a database node…". |
| C11 | R-06-9: tables ordered by FK, parents first; "環狀的在預覽警告". A new table has no FK; MySQL and SQLite targets check none (C4). | Ordered parents first from the source's schema snapshot (06a's `parents_first`); the cycle is warned only when appending or replacing into PostgreSQL or SQL Server, which check FKs row by row (`FK_CYCLE`). | Task 2 `test_tables_go_parents_first_and_a_query_runs_again` (the order); `transfer_service._order` (the cycle's rule). |
| C12 | §3.5.2: rowversion "不複製"; generated and computed source columns and `sql_variant` are not said for a copy. | A source column the dump does not write (`TableColumn.copied` false: generated, computed, rowversion, `sql_variant`, a period column) is not copied and listed with its reason ("not copied: …"); an append never writes the target's computed or generated columns ("the target computes it"). Neither is a problem. | Task 2 `test_append_maps_columns_by_name_and_writes_what_identities_need`. |
| C13 | R-06-10: the same engine copies the declared type, except the database's own types (PostgreSQL enum, domain, composite). Which types are "own" is not said for SQL Server, and a spatial type's declared text is the same engine's. | `TableColumn.custom_type` (PostgreSQL: a domain, enum or composite, or any type outside `pg_catalog`; SQL Server: an alias type) maps by family even to the same engine, with a note; spatial types keep their declared text on the same engine; PostgreSQL `money` stays `money` between PostgreSQL databases. | Task 1 `test_the_same_engine_keeps_the_declared_type_unless_it_is_the_databases_own`. |
| C14 | §3.5.5 step 2 fails a table whose INSERT reports another row count. SQL Server's `INSTEAD OF` trigger reports the rows asked for (A16). | Known limitation, as ⑤ A3: such a trigger's skipped rows are not caught. PostgreSQL's `BEFORE … RETURN NULL` and SQLite's `RAISE(IGNORE)` are. | A16 below; Task 4 `test_a_trigger_that_skips_rows_rolls_the_table_back`. |
| C15 | D-06-6: the dump reads PostgreSQL tables `FROM ONLY` (a child is dumped on its own). For a copy of an inheritance parent? | A copy reads what the table tab shows: without `ONLY` (a parent's copy includes its children's rows); partitions come through their root as for the dump. | Task 2 `transfer_service._select` (`build_copy_select(…)` with `only` False). |
| C16 | §3.5.3 fails a row the target cannot hold "不改成 NULL". 06a writes PostgreSQL's NaN into SQLite as `NULL` (SQLite stores NaN as NULL), right for a dump of SQLite, which holds none; and the NaN note would show on every number column into MySQL or SQL Server. | PostgreSQL's NaN into SQLite fails its row (±infinity is `9e999`); the "NaN and infinity fail their row" step and note only for a float, decimal or untyped column from PostgreSQL or SQLite (the engines that hold them) into MySQL or SQL Server. A bool from an integer that is neither 0 nor 1 fails its row. | Task 1 `test_numbers_into_text_keep_every_digit_and_non_finite_numbers_fail_where_they_cannot_be_held`, `test_true_and_false_become_what_the_target_column_holds`. |
| C17 | 06b's carry: "A copy has per-table outcomes and nothing like `resume_at`: decide the again-actions deliberately". | Nothing applied (rolled back, not started): "Copy again…" (the form as it was). Stopped part way (partial, failed or cancelled): "Copy the rest…" (the form with only the tables not copied checked: none is copied twice). Copied, or unknown (check the target first): none. A job listed after a reload: none (no command, 06b V6). Every again-action goes back to the form (06b V2). | Task 6 `jobReport.test.ts` "offers the rest of a copy that stopped part way, and the whole of one that applied nothing". |
| C18 | R-06-8's query source "重跑那一句，同匯出": a paused result on the tab, and the statement's columns vs the preview's, are not said. | As the export: the job ends the tab's paused result first; only a plain read is run (`is_plain_read`, at plan and at run); the columns must be the preview's by name (`columns_changed`); the preview warns that the query runs again (`QUERY_RUNS_AGAIN`). | Task 3 `test_a_query_that_writes_or_changed_its_columns_is_never_read`; Task 2 `test_tables_go_parents_first_and_a_query_runs_again`. |

## Probes run while planning

On the dev databases (only `zz_06c_*` tables, each dropped by exact name; nothing left after), with the dry run's code:

- **A16 (matched rows of a multi-row INSERT).** PostgreSQL, MySQL and SQLite: `matched_rows()` of a 4-row INSERT is 4 (MySQL's OK packet `Records: 4`), SQL Server's too. A PostgreSQL `BEFORE INSERT` trigger returning NULL for two rows: 2 (caught: `row_count_mismatch`); SQLite `RAISE(IGNORE)` for two rows: 2 (caught); SQL Server `INSTEAD OF INSERT` inserting two of four: 4 (the outer count; not caught: C14); `AFTER` triggers writing another table: the count unchanged on every engine; a SQL Server trigger returning a result set: unknown (`row_count_unknown`); MyISAM inside a transaction: the ROLLBACK warns 1196 and the rows stay (so a failed write there is `unknown`); duplicate keys: 23505, 1062, 2627, `SQLITE_CONSTRAINT_PRIMARYKEY`.
- **A11 (a MySQL source while the pipe is full).** With `net_write_timeout = 2` and a reader that stalls 8 s, a 1,000,000-row read ended after 66,001 rows with `connection_lost` ("Lost connection to MySQL server during query"), never a silent truncation. `SET SESSION net_write_timeout = 3600` is allowed for the dev login, and the same 8 s stall then read all 1,000,000 rows: the copy sets it on the source session and sets the session's own value back after the table (C5).
- **A9 (the 4×4 matrix).** Type-complete tables (PostgreSQL 24 columns, MySQL 23, SQL Server 23, SQLite 13, three rows each): 16 of 16 pairs read every value back equal by meaning once `timetz` → MySQL `TIME` dropped its offset (folded into Task 1).
- **Speed (§7's Copy row), unfrozen sidecar, 1,000,000 rows of 6 columns (about 230 bytes):** PG → MySQL 19.2 s, PG → SQL Server 87.2 s (10,000 INSERTs of 100 rows), PG → SQLite 12.3 s, SQLite → PG 19.6 s; sidecar RSS +2–12 MB. In-process, 200,000 rows from SQLite: PostgreSQL and MySQL 3.7 s, SQL Server 20.3 s, max RSS +9 MB.

## Appendix A: the user's final test list (06c), for the coordinator's follow-ups note

As built (updated in Task 9 from what Tasks 1–8, Task 5b and the fix rounds made, and again in the final fix wave; the words in quotes are the app's). Nobody drove the packaged app: items 5, 8 and 11 need it (Zhuyin, ⌘Q and the frozen sidecar only work as they do there), item 9 needs the web mode, the rest can be done in either.

1. **你的真實資料（在可以亂改的 database 上）。** 從公司的 PostgreSQL、MySQL、SQL Server 各挑一張有代表性的表（含時區時間、SQL Server 的 datetimeoffset／datetime2(7)、DECIMAL、JSON、中文與 emoji、NULL、有 `\` 接換行的文字），Copy 到另一個 engine 的新表：預覽的每一欄型別與 ⚠ 說明看起來合理（例如 timestamptz → `datetime(6)` 的 UTC 說明、MySQL `json` 會正規化的說明）；結果的列數與來源相同；抽幾列比對值（時間是同一個時刻、數字每一位都在、文字一字不差）。MySQL 的 `TIMESTAMP` 欄（公司的 server 或 profile 的時區不是 UTC 時）：預覽說「MySQL TIMESTAMP is read as UTC: 20:00 at +08:00 is copied as 12:00」，copy 出去的是 UTC 的時間（grid 顯示的減去時區）；MySQL TIMESTAMP 進 MySQL TIMESTAMP 沒有這句、值不變。
2. **大表。** 一張幾百萬列的表 Copy 到另一個 engine：進度顯示「Copying <表>」與「N rows read」（兩張以上的表才有「(k of n)」，側欄的 Jobs 那一列也一樣；要看到它，就用第 3 項那種好幾張表的 copy）；關掉對話框後 Jobs 裡還在（第一次關時 toast「The copy keeps running: open it again from Jobs, at the bottom of the sidebar」）；Cancel 之後結果是「Cancelled: nothing was copied」或那張表的列 rolled back（目標裡沒有半張表；MySQL 建新表模式例外：表留著、是空的，結果的說明會說「…stays, empty: drop it, or copy again into it」）。
3. **好幾張表、停在一半。** 先在 PostgreSQL 建兩張表，第二張放一列 `'infinity'::timestamp`（表的順序是 FK 的父表先，沒有 FK 就照勾選的順序）；從那個 schema（或 Tables 資料夾勾這幾張）Copy 到 MySQL：結果裡第一張「copied」、失敗的那張「rows rolled back」並指出第幾列哪一欄，後面的「not copied」；「Copy the rest…」回到表單、只勾沒複製的表。MySQL 建新表的那張失敗時表已經在了：先 DROP 那張空表，再「Copy the rest…」（否則預覽說 already exists）。
4. **紅色連線。** 目標是 production 色的連線時，Copy 是紅色按鈕（不另外要勾選：每一句都在預覽裡）；「Delete its rows, then append」的 DELETE 在預覽裡是紅的。
5. **注音。** 在對照表的型別欄、目標表名欄用注音輸入：組字中按 Enter 選字，不會送出型別、不會開始 Copy。
6. **刪除連線。** 一個 copy 在跑時，刪除目標連線被擋下（「Profile not deleted — a job is still running on it」），刪除只被它讀取的來源連線也被擋下（「Profile not deleted — a copy is still reading from it」）；確認的對話框跟著關掉。
7. **Query 結果。** 在 PostgreSQL 的 query tab 建一個 temp table 再 SELECT 它，結果的 status bar 按「Copy to table…」（或 grid 右鍵「Copy result to a table…」）複製到另一個 engine：預覽說 query 會重跑、copy 讀得到 temp table；copy 進行中關那個 tab（⌘W）或換它的連線會被擋下（toast 說明），copy 結束後就可以。
8. **打包版的 ⌘Q。** Copy 進行中按 ⌘Q：問「Quit DbaGuy?」並列出那個 copy，說明「a copy rolls back the rows of the table it is copying, unless that table cannot roll back (the tables before it stay)」；Keep running 是預設。
9. **重新載入。** Copy 進行中重新載入頁面（`scripts/dev.sh --web` 的瀏覽器重新整理；打包版的選單沒有 Reload）：Jobs 仍列出它、可以 Cancel；結束後它的結果沒有「Copy again…」（重新載入後的 job 沒有原本的表單）。
10. **傳統捲軸。** 對照表很寬（或有很多欄）、預覽的語句很長時：水平捲軸不蓋住最後一列、不畫在固定高度的框裡面（CLAUDE.md 的傳統捲軸規則）；附加進有 identity 的表時，identity 的選單是「Keep values」／「Generate new」，字沒有被切掉，hover 看到完整的說法。
11. **打包版跑一次 copy。** 在 `.app` 裡做第 1 項的任何一個 copy 一次：凍結的 sidecar 經 WKWebView 的 WebSocket 跑 `copy_data`，只有 probe 驗過，沒有人從 UI 驗過。
12. **取代有 FK 的表。** 在 PostgreSQL（或 SQL Server）有一張父表、一張以一般的 FK（沒有 ON DELETE CASCADE）參照它而且有列的子表；用「Delete its rows, then append」把兩張一起 Copy 進去：預覽在父表下說「Replacing the rows of <父表> fails while <子表> holds rows that reference them (ON DELETE NO ACTION or RESTRICT): empty <子表> first, then copy」；照樣按 Copy 會在父表失敗（23503／547）、rolled back、什麼都沒少；在 query tab 先 `DELETE FROM <子表>` 再 Copy 就成功。
13. **很多表的預覽。** 一個超過 40 張表的 schema Copy 到別的 engine，讓其中一張有問題（例如一欄的型別對不上）：它畫在最前面；把那欄的型別改好之後，它留在原地（不會掉出畫出來的範圍），底下寫「Showing 40 of N tables: those with a problem or a change first」；快速連續改幾個選擇時，樹與補全不會被舊的預覽卡住。

已知的限制，不要當成 bug 回報：
- SQL Server 目標表上的 INSTEAD OF trigger 若略過了列，Copy 看不出來（列數照樣對）。
- 建新表只帶欄位、NOT NULL 與 primary key：預設值、index、FK、identity 都不建（要一模一樣的表：同 engine 用 Dump＋Run 建結構，再以附加模式 Copy）。
- Database 節點沒有 Copy data to…：一次複製一個 schema。
- Query 結果的 Copy 會在那個 tab 重跑那一句（同匯出），暫停中的結果會被結束；Cancel 之後那個 tab 要等它正在讀的那一次回來才會閒下來。
- 一個值目標裝不下（PostgreSQL 的 infinity 與 NaN、西元前的日期、MySQL 的 0000-00-00…）時那張表整張 rollback 並指出第幾列哪一欄：用篩選排除那列，或（建新表時）把那欄的型別改成文字。
- MySQL 建新表失敗後表留著（MySQL 的 CREATE TABLE 立刻 commit）：再 Copy 之前先 DROP 它（見第 3 項）。
- 結果是 unknown 的表（例如 MySQL 的 MyISAM）不顯示列數，說明裡寫「Up to N rows … may stand」：去目標看。
- 取代進 PostgreSQL、SQL Server 時，父表不能和以一般的 FK 參照它、而且有列的表一起取代（預覽會說，第 12 項）：先清空那些表。
- MySQL 的 TIMESTAMP 以 UTC 讀寫（預覽會說，第 1 項）；query 結果照 grid 的讀法重讀（MySQL 的 FLOAT 只有 6 位、SQL Server 的第 7 位小數、PostgreSQL 的 money 文字，預覽逐欄說）：要每一位就 Copy 那張表。
- 預覽每張表讀幾次 catalog：表多、server 遠時預覽要好幾秒到幾分鐘（較新的預覽會讓舊的停下）。
- Couchbase 沒有 Copy（CR-1：要的話再說）。

## Self-review

- **Spec coverage.** §3.5.1 → Task 2 (`TransferRequest`, `TransferPlan`, the 500-table and 200-character limits; C1); §3.5.2 → Task 1 (every row of the table is a test case; same engine; query columns by name or category) and Task 5; §3.5.3 → Task 1 (each conversion and each row failure; C16) and Task 4 (`value_not_copyable` names the row and column); §3.5.4 → Task 2 (create, append by name, replace's DELETE; ⑤'s refusals through `not_writable_reason`; C4, C11, C12, C15); §3.5.5 → Tasks 3–4 (the plan taken once, the target session's settings, per-table transactions, `before`/`after`, the source on its worker, the pipe, the gate, the matched-row check, FK settings, IDENTITY_INSERT per table, cancel both ways, `source_failed`, the full pipe; C2, C3, C5, C6, C8); §3.6 → Tasks 6–8 (CopyDataDialog, MappingTable, `mappingRows`, the entries, "no Copy while a problem"; C9, C10, C17); R-06-8 → Tasks 3–4; R-06-9 → Tasks 2, 4; R-06-10 → Tasks 1–2; R-06-11 → Task 1 (UTC, ±HH:MM, money as numeric, NUL) and 06a's reads; R-06-12 → Task 4; R-06-13 → Task 3 (2 chunks of ≤ 1,000 rows and ≤ 4 MiB); D-06-8 → Task 2 (`OneTimePlanStore`) and Task 4 (taken once; INSERTs written at run time); D-06-13 → Tasks 6, 8; §4 → Tasks 2, 4, 6 (C7); §5 → Task 4's logs (INFO counts and outcomes, never a value); §6 → the tests named in each task; §7 → Task 9 Step 3; §10 A9, A11, A16 → the probes above and Tasks 3–5; the 06a and 06b carries → Tasks 1–8 (B13 in Tasks 6 and 8; the third job kind in Task 6), and "package and measure again" → Task 9.
- **Placeholder scan.** Every changed file is given whole when new, or as a patch against the previous task's tree; the probe is given whole; the only figures left open are Task 9's measurements (recorded numbers) and the CLAUDE.md and spec text, specified by content.
- **Type consistency.** All tasks' code was applied in order in one scratch worktree and each task's tests, `gen:api`, typecheck and build passed after that task alone (Dry-run record); names across tasks (`TransferService.plan`, `StoredTransfer`, `PlannedTable`, `TransferPlanStore.take`, `RowPipe`, `TransferSource`, `TransferSink`, `PreparedJob`, `SourceLease`, `resume_cancels`, `CopyDataCommand`, `copyRequest`, `copyCommand`, `mappingRows`, `openCopyDialog`, `runningOn`) come from those files.
- **Review Focus.** Each of the five lines names the tests or checks that pin it and their task.
- **Proportion.** 9 tasks: four backend tasks carry the risk (types, the plan, two sessions, outcomes) and are tested live on four engines; the matrix is its own task; the frontend's rules are pure and tested, its components compiled and checked by hand.

## Dry-run record

A scratch git worktree at 40a15dc (`node_modules` and `backend/.venv` linked from the main checkout; the backend's tests and the OpenAPI dump run with `PYTHONPATH` at the worktree's `backend/`, since the venv's `.pth` points at the main checkout), each task's code applied in order and committed, and that task's checks run before the next:

- Task 1: `test_type_map.py test_table_columns.py test_mysql_types.py test_postgres_dump_catalog.py test_mssql_dump_catalog.py test_mysql_sqlite_dump_catalog.py` 204 passed; `test_editing_target.py` and the four engines' `*_edits.py` 62 passed (the families moved without moving one).
- Task 2: `test_transfer_sql.py test_transfer_plan.py test_edit_plans.py test_edits_api.py test_container.py test_api.py` 94 passed; `gen:api` (+569 lines of `openapi.json`, +255 of `schema.d.ts`); `tsc --noEmit` clean.
- Task 3: `test_transfer_pipe.py test_copy_framework.py test_job_framework.py test_session_worker.py test_sessions.py test_worker_protocol.py test_session_wiring.py` 151 passed; `gen:api`; `tsc --noEmit` clean.
- Task 4: `test_transfer_job.py test_copy_framework.py` 19 passed; `test_dump_job.py test_mysql_sqlite_dump_catalog.py test_dump.py test_run_file_job.py` 227 passed; `gen:api` unchanged.
- Task 5: `test_copy_matrix.py` 16 passed.
- After the NaN rule (C16) was added to Task 1: `test_type_map.py test_transfer_plan.py test_transfer_job.py test_copy_matrix.py test_copy_framework.py` 126 passed.
- After the log levels were settled (a table whose outcome is unknown, a failed rollback, a source setting not set back: WARNING; a snapshot failure's text only at DEBUG): `test_transfer_plan.py test_transfer_job.py test_copy_framework.py test_transfer_pipe.py` 34 passed.
- Task 6: `vitest run` 878 passed (47 files; 859 before); `tsc --noEmit` clean; `gen:api` unchanged.
- Task 7: `tsc --noEmit` clean; `vite build` clean.
- Task 8: `vitest run` 879 passed; `tsc --noEmit` clean; `vite build` clean, main chunk 1,549.50 kB (gzip 497.79 kB) against 06b's 1,526.60 kB (492.29 kB).
- Task 9's probe: run with an unfrozen sidecar from the worktree, 20,000 rows and then 1,000,000 rows (the numbers above); the frozen build was not made while planning (Task 9 builds it).
- No `zz_06c_*` object left on PostgreSQL, MySQL or SQL Server afterwards; no Couchbase write; the worktree removed.
- Not dry-run: the components' behaviour in a browser (Task 7 and 8's manual lists), the packaged app, Task 9's documents.
