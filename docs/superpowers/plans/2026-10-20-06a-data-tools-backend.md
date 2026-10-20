# 06a: Data tools — backend (dump and Run SQL file) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The backend of ⑥'s dump and Run SQL file. Jobs run on sessions of their own through one framework: exactly one `job_outcome` per job, progress, cancel, a job list that a reloaded window finds again, and History records that start as `running` and end as `interrupted` after a crash. Run SQL file streams a file of any size, a statement at a time, on all five engines: all or nothing in the dialect's transaction frame, or statement by statement (stop or skip), from statement N, including the files `pg_dump --inserts`, `mysqldump`, `sqlite3 .dump` and SSMS write. A native dump of PostgreSQL, MySQL, SQL Server and SQLite writes the DDL in dependency order and the data as multi-row INSERTs read inside one snapshot, through a temporary file, and Run SQL file restores it. The whole is measured with 1,000,000 rows and checked again in the frozen sidecar.

**Scope (Copy → 06c, spec D-06-14 / CR-6):** this plan is 06a's dump and run half and the foundations both use. Copy data is spec §11's 06a T10–T11: `type_map`, `OneTimePlanStore`, `/data/transfer/plan`, `TransferSource`/`TransferSink`/`RowPipe`, the 4×4 matrix with A9, A11 and A16, and the `copy_data` capability. It moves to **06c**, with CopyDataDialog from 06b. The dump and run half comes to 10 tasks here. The spec's T1 probes are folded into the tasks that need them; the job framework and Run SQL file are a task each; the dump needs a catalog task per engine group and a text task. Copy at this depth comes to at least four more tasks (types and plans; sources and sinks on four engines; the pipe and its job; the matrix), so the whole would be 14 or more, past the limit of 13. What 06a hands 06c: `TableColumn`/`TypeFamily` from every relational engine (Task 4), the job framework (Task 2), `InsertBatcher` and `build_copy_select` (Task 8). The 06b frontend for dump and run can start after Task 9. This split is spec issue S1.

**Architecture:** Pure layers come first. Task 1: `sql/splitter.py` splits in one pass without keeping tokens (`iter_spans`, resumable through `SplitState`); `sql/statement_stream.StatementStream` reads a file in 512 KiB chunks and yields its statements with line, byte offset and `GO n` count; the classifier judges an INSERT over 64 KiB by its first word. Task 2: the job framework. A `JobCommand` subclass goes through `SessionWorker._run_job`, and the job object (`sessions/jobs/context.Job`) does only its own work, through a `JobContext`: the worker's own statement path, progress at most every 250 ms, server messages up to 200, and the History record. `JobRegistry` lists the jobs (`GET /data/jobs`, cancel, clear); a busy session is never reaped; a client that attaches gets the job's last events again. History gains `running` and `interrupted`, and `SafetyPolicy.gate_confirmed` is the read-only gate. Task 3: `RunFileJob`, with the outcome table of R-06-12, `/data/run-file/inspect`, and the `run_file` capability on all five engines. Task 4: `DataTools.table_columns`, an engine-neutral `TableColumn` with its `TypeFamily` and how a dump reads it (`read_as`); ⑤'s edit targets now read it. Tasks 5–7: each relational engine's `Dumping` (`dump_session`, `dump_catalog`), in engine-neutral `DumpCatalog`/`DumpObject`/`DumpTable`: PostgreSQL (Task 5), MySQL and SQLite (Task 6), SQL Server (Task 7). Task 8: the file's text — `InsertBatcher`, `DumpScript`, `build_copy_select`, the dialect's non-finite floats and identity writes. Task 9: `DumpJob` (atomic file, CR-4's disk check), `/data/dump/plan`, `/data/reveal`, the `dump` capability, and the dump → run round trip of every engine's seed (A7). Task 10: A10's measurements, the frozen sidecar, CLAUDE.md and the spec.

**Tech Stack:** Python 3.13, FastAPI, pydantic v2, loguru; psycopg 3.3.6, mysql-connector-python 26.7.0 (pure), pymssql 2.4.4 (`_mssql`), sqlite3; the existing Couchbase REST adapter. pytest runs against the dev containers: PostgreSQL 17.11, MySQL 8.4.11, SQL Server 2022 CU27, SQLite 3.53.4 and Couchbase EE 7.6.12. No new dependency. The frontend gets only the regenerated API types, the type exports in `src/lib/api/types.ts`, and the History panel's two new statuses.

**Spec:** docs/superpowers/specs/2026-10-20-06-data-tools-design.md, §11 item 1 (06a): T1 probes A1–A5 (folded into Tasks 4–7); T2 `iter_spans`, `StatementStream`, the classifier's limit (A6); T3 the job framework; T4 `RunFileJob` and inspect (A8); T5 `table_columns` and `TypeFamily` (A13, A15 here); T6–T8 the engines' `dump_catalog` (A2, A3, A4, A5, A14); T9 `dump_script`, `InsertBatcher`, `build_copy_select`, `DumpJob`, `/data/dump/plan` and the round trips (A7); T12 the measurements (A10), the frozen sidecar and the docs. Spec T10–T11 (Copy) go to 06c (S1). Binding: R-06-1…R-06-15 and D-06-1…D-06-14, except their Copy parts, which belong to 06c; §3.1–§3.4, §4, §5, §6, §7, §10, appendix A; the coordinator review CR-1…CR-6 (CR-3's confirm text and CR-5's quit guard are 06b; CR-4 is Tasks 9 and 10).

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 06a serves it, or avoids regressing it |
|---|---|
| FP §8 SQL dump, 跨 DB 傳輸 (G1, G2) | A native dump of the four relational engines (Tasks 5–9) that Run SQL file restores (Task 3), checked by a round trip of every seed (Task 9, A7); no external program (D-06-1). Copy (G3) is 06c. |
| §2 Q1 輕、快、低記憶體 | Every path streams with fixed limits: 512 KiB file chunks, no token lists (Task 1), 1,000-row reads and 1 MiB INSERTs (Tasks 8–9), sqlite3's statement cache off for 1 MiB statements (Task 3). The sqlglot cost on large INSERTs is gone (D-06-4, Task 1). Measured with 1,000,000 rows in Task 10; no new dependency. |
| §2 Q2 穩定、不丟工作 | Exactly one `job_outcome` per job, whatever happens, and never a resend (Tasks 2–3). A dump writes through `.part` and leaves no file on failure, cancel or a full disk (Task 9, CR-4). History says `running` from the start and `interrupted` after a crash (Task 2). A job outlives its dialog: the reaper skips busy sessions and a reload finds the job (Task 2). |
| §2 Q3 可信任 | The literals are ⑤'s (`sql/literals.py`), so values read back as themselves: bigints, 38-digit decimals, SQL Server's 7th fractional digit, binary, JSON, NULL vs '' (Tasks 4, 8, 9). Every outcome says what took effect (`partial`, `stopped` with the line, `unknown`). What a dump leaves out is in its header (Tasks 5–9). Read-only is refused twice: before connecting (403), and by every statement's gate (Tasks 2–3). |
| §2 Q4 聚焦 80% | Only dump and run. No sync, compare, upsert, schedules, compression or psql/sqlcmd meta-commands; Couchbase gets only Run SQL file (R-06-14). |
| §2 Q5 多引擎從第一天 | One job framework, one run with one outcome table, and one dump script for every engine. Engine knowledge sits in `columns.py`/`dump.py` per adapter and in the dialect tables (Tasks 4–8). Couchbase runs files statement by statement (Task 3). |
| §2 Q6 macOS 慣例 | `/data/reveal` (Show in Finder) for files a dump wrote (Task 9); the dialogs, notifications and the quit guard are 06b. |
| §3 M9 完整結果匯出 | A whole schema or database, structure and data, as one SQL file (Tasks 5–9). |
| §3 M11 安全 | The read-only gate on every statement of a run (`gate_confirmed`, re-read at most once a second, Tasks 2–3). A dump never writes DROPs unless asked (`drop_first`, Task 8), and a run is refused on a read-only connection (Task 3). |
| §3 M7 值的呈現 | The dump writes what the adapters' lossless loaders read; the 7th digit is read as text (Task 4); non-finite floats are written as each dialect reads them (Task 8). |

## Global Constraints

- Scope is spec §11 item 1 (06a) without Copy (S1), on branch `feat/06-data-tools`; the spec and its coordinator review end at 8089a93. No frontend code beyond the generated API types, the type exports in `src/lib/api/types.ts`, and `HistoryPanel.tsx`'s two new statuses (the status union grew, and typecheck needs them). No merge to `main`: ⑥ merges after 06b and 06c.
- Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`); **no new Python dependency** and no new frontend dependency.
- Binding values, each defined once, in the file named:
  - `sql/statement_stream.py`: `STREAM_CHUNK = 512 * 1024` (S5), `MAX_STATEMENT_CHARS = 256 * 1048576`, `FILE_NOT_UTF8`, `STATEMENT_TOO_LARGE`.
  - `sql/classifier.py`: `LARGE_INSERT_CHARS = 64 * 1024` (D-06-4).
  - `domain/data_jobs.py`: `MAX_JOB_ERRORS = 100`, `MAX_JOB_MESSAGES = 200`, `PROGRESS_INTERVAL_S = 0.25`, `MAX_JOBS_LISTED = 200` (D-06-10, D-06-11), `MAX_DUMP_NAMES = 500`.
  - `sessions/hooks.py`: `READ_ONLY_RECHECK_S = 1.0`. `sessions/worker.py`: `_JOB_LOG_CHARS = 200` (D-06-12).
  - `sql/bulk_insert.py`: `MAX_INSERT_ROWS = 1000`, `TSQL_INSERT_ROWS = 100`, `MAX_INSERT_CHARS = 1048576` (D-06-5).
  - `services/data_service.py`: `PREVIEW_LINES = 200`, `PREVIEW_BYTES = 64 * 1024`, `LOW_DISK_BYTES = 1 << 30` (CR-4).
  - `sessions/jobs/run_file.py`: the codes `transaction_in_file`, `transaction_ended`, `psql_meta_command`, `unsupported_copy`, `file_io` and the hints `COPY_HINT`, `HEX_BLOB_HINT`; `sessions/jobs/dump.py`: `dump_io`.
- From the spec, verbatim where it binds this plan:
  - R-06-12: 「每個 job 恰好一個 `job_outcome`（⑤ E1），從不重送；排隊時就被取消、或 session 在開始前關閉的 job 是 `not_started`。」 Run, all or nothing: `completed`／`rolled_back`／`partial`／`unknown`; statement by statement: `completed`／`stopped`／`unknown`. §3.2: 「`cancelled` 是一個旗標，不是另一種結果」. The dump's outcomes are `completed` and `failed` (S2).
  - D-06-2's event order: `started` → `job_progress`×n → [`message`] → [`error`] → `job_outcome` → `session_state` → `finished`.
  - R-06-6: 「整份確認一次，逐句只做唯讀閘門」 (`gate_confirmed`).
  - R-06-7's texts and §4's messages: `The file controls its own transactions (line N: COMMIT): run it statement by statement`; `The transaction ended at line N (MySQL commits around DDL): the statements before it are applied; run the rest statement by statement` (other dialects: `(a statement in the file ended it)`, S16); `Line N is not UTF-8 text`; `The statement starting at line N is larger than 256 MiB`; `Line N is a psql command (\connect): run this file with psql`; the COPY hint `pg_dump: use --inserts, or run this file with psql`; `Cannot write <path>: <reason>`; `This connection is read-only`; `<Engine> connections cannot be dumped` and `… cannot run SQL files` (§4's `cannot <dump>`, as a sentence); `<path> is not a file`.
  - D-06-12 / §5: INFO gives the job's kind, profile, scope or path, counts, bytes, ms and outcome; WARNING gives partial, unknown, interrupted, skipped objects and a per-table dump; DEBUG gives each statement's first 200 characters and the server's error text. Never at INFO or above: values, literals, or the server's error text (it echoes literals).
- Literals and identifiers come only from `sql/dialects.py` (through `sql/literals.py`). `sql/` stays pure: no I/O, no logging.
- Threads: a job runs only on its session's worker thread. Each statement goes through `_run_statement` (the armed cancel window), and a dump's catalog reads go through the worker's own connection. **SQL Server:** every driver call happens on the worker thread, and the jobs add no cross-thread driver call: a Cancel reaches the adapter only through `SessionWorker.cancel` → `connection.cancel()`, as before.
- **SQL Server: never read `.text`, `.message`, `.srvname` or `.procname` on a pymssql `MSSQLDatabaseException` you constructed. It segfaults Python and pops a crash dialog for the user.** Tests, probes and fakes may read `str()`, `repr()`, `args`, `number`, `severity`, `state` and `line` (CLAUDE.md G14); the adapter's `server_text()` reads only `exc.args`.
- Dev databases (connection details in CLAUDE.md 測試資料庫):
  - Tests and probes create only `zz_06_*` objects, through `tests/data_support.scratch_name()` and `dropping()`, dropped by exact name when the block ends. Restores go into scratch databases `zz_06_restore_*` (and `zz_06_dumpcat_*`) that `scratch_database()` creates and drops as PostgreSQL's and MySQL's root and SQL Server's sa. SQLite uses files in `tmp_path`.
  - Seeded tables are only read. Couchbase is written only under `zz_06_` keys in `dbaguy.app.orders`, each under 64 KiB, removed by exact key, so `app.orders` stays at exactly 500.
  - Never touch `~/Library/Application Support/DbaGuy` (not even `ls`). Probes against the frozen sidecar use a temp `DBAGUY_DATA_DIR`, and their profiles are saved with `"save_password": false` and unlocked.
  - **Never kill processes by pattern**: stop only what you started, by PID. The repo is public: only the fake dev credentials.
- Disk: a 1,000,000-row dump is about 230 MB. Measurements write into the scratchpad and delete what they wrote; check `df -h /` first (stop below 10 GiB free).
- `cd backend && .venv/bin/pytest -q` is green after every task with the dev containers up, and a task report never shows a skipped `requires_*` test. Never run it while scratch objects exist (04b's sampling tests count `app.orders`, introspection's exact lists see scratch objects), and only one person runs live tests at a time. Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties): wait, or `docker compose -f dev/docker-compose.yml restart bastion`.
- After any backend model or API change, run `npm run gen:api` and commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts` (never hand-edited); add the type exports to `src/lib/api/types.ts`; `npm run typecheck && npm run build` pass.
- Commit after each task, staging only that task's files by path (never `git add -A`; if `git status` shows files staged by someone else, commit with `git commit -m … -- <paths>`). The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.

## Review Focus

1. **An all-or-nothing run never leaves half a file applied, a statement-by-statement run says exactly where it stopped, and nothing is ever sent twice.** Covers an error at statement k, a Cancel while a statement waits on a lock, the file's own COMMIT, MySQL's DDL commit, an aborted or vanished transaction, a refused COMMIT, a lost link during COMMIT, and a transaction the file leaves open. → Task 3 `test_run_file_job.py` (`test_all_or_nothing_stops_roll_everything_back`, `test_what_commit_answers_decides_the_outcome`, `test_a_transaction_the_file_ends_stops_an_all_or_nothing_run_as_partial`, `test_statement_by_statement_a_lost_link_is_unknown_and_a_cancel_stops`, `test_a_transaction_the_file_leaves_open_is_rolled_back_and_said`); `test_run_file.py` on every engine (`test_all_or_nothing_an_error_applies_nothing`, `test_a_cancel_rolls_back_or_stops_where_it_reached`, `test_mysql_ddl_commits_an_all_or_nothing_run_as_partial`).
2. **Statement boundaries are the same when the file is streamed as when it is read whole, at any chunk size.** Covers strings, comments, `$$` bodies, `DELIMITER`, `GO n`, CRLF, a BOM, psql lines, a multi-byte character cut at a chunk boundary, and a line that is not UTF-8. → Task 1 `test_statement_stream.py` (`test_split_is_unchanged_by_the_single_pass`, `test_any_chunk_size_gives_the_same_statements`, `test_every_cut_gives_what_reading_the_file_whole_gives`, `test_text_that_is_not_utf8_stops_at_its_line`); Task 3 `test_run_file.py`'s other tools' files (A8).
3. **What a dump writes reads back as the same values.** Covers bigints beyond 2⁵³, 38-digit decimals, SQL Server's 7th fractional digit, datetimeoffset, binary, JSON, NULL vs '', non-finite floats, `it's`, backslashes in both MySQL modes, and names that need quoting. → Task 8 `test_dump_text.py` (`test_numbers_dates_and_decimals_keep_every_digit`, `test_text_is_escaped_for_the_dialect_and_mode`, `test_non_finite_floats_are_written_as_each_dialect_reads_them`, `test_datetimeoffset_text_with_a_space_before_its_offset`); Task 9's four round trips in `test_dump.py`.
4. **A dump's objects come in an order that runs, and nothing is lost silently.** Covers views on views, a function a default or computed column calls, partitions, triggers and their functions, sequences at their next value, identity and AUTO_INCREMENT counters, `sqlite_sequence`, and what is left out (named in the header). → Task 5 `test_a_schema_dump_orders_every_object_by_its_dependencies`; Task 6 `test_a_mysql_dump_orders_its_objects_and_drops_their_definers`, `test_a_sqlite_dump_keeps_sqlite_masters_order_and_its_counters`; Task 7 `test_a_sql_server_dump_orders_its_objects`; Task 9's round trips.
5. **A job's life holds up.** Exactly one `job_outcome` (queued and cancelled, session closed first, refused, worker bug); the reaper never closes a running job's session; a reloaded window finds the job and its last events; History goes from `running` to finished, or to `interrupted` after a crash; a dump leaves no file and keeps the old one on failure, cancel or a full disk. → Task 2 `test_job_framework.py` (`test_a_job_cancelled_while_queued_or_closed_before_it_started_says_not_started`, `test_a_session_running_a_request_is_never_reaped`, `test_a_client_that_attaches_later_gets_the_last_progress_and_the_outcome`, `test_an_unclassified_failure_is_unknown_once_something_was_sent`), `test_history.py`; Task 9 `test_dump_job.py` (`test_a_cancelled_dump_leaves_no_file_and_the_old_one_as_it_was`, `test_a_full_disk_says_so_and_leaves_nothing`).

---

## File Structure

```
backend/dbaguy_backend/
  sql/splitter.py                 iter_spans() single pass, SplitState, StatementSpan.meta; split() = list(iter_spans())
  sql/statement_stream.py         (new) StatementStream, FileStatement, StreamError, STREAM_CHUNK, MAX_STATEMENT_CHARS
  sql/classifier.py               LARGE_INSERT_CHARS: an INSERT over 64 KiB by its first word
  domain/data_jobs.py             (new) JobKind, JobOutcomeKind, JobStage, JobCommand, JobError, StoppedAt, JobObject,
                                  JobProgressEvent, JobOutcomeEvent, JobInfo; ENGINE_NAMES, DumpHeader,
                                  FileInspection (Task 3); DumpScope, DumpObjectKinds (Task 5); DumpContent,
                                  DumpPlanObject, DumpPlan (Task 9)
  domain/execution.py             ServerEvent + the job events; RunFileCommand (Task 3), DumpCommand (Task 9)
  domain/history.py               HistoryStatus + running, interrupted
  domain/errors.py                NotSupported, FileNotFound
  domain/profiles.py              EngineInfo.run_file, EngineInfo.dump
  services/history_service.py     begin(), finish(); running → interrupted at open
  services/safety.py              gate_confirmed()
  services/data_service.py        (new) DataService: inspect() (Task 3), dump_plan(), reveal() (Task 9)
  sessions/jobs/context.py        (new) Job, JobContext, JobHooks, JobProgress, JobReport, history_status()
  sessions/jobs/registry.py       (new) JobRegistry
  sessions/jobs/run_file.py       (new) RunFileJob, summary(); the run's outcome table
  sessions/jobs/dump.py           (new) DumpJob
  sessions/worker.py              _run_job and its helpers; replay_job(), busy(), finished_at; _start_request(before_finished)
  sessions/hooks.py               _job_hooks(), build_job(), _Recent
  sessions/manager.py             jobs registry wiring; reaper skips busy sessions; attach replays the job
  sql/dump_header.py              (new) header_lines(), parse_header(), comment_safe()
  sql/bulk_insert.py              (new) InsertBatcher
  sql/dump_script.py              (new) DumpScript, RESTORE_SETTINGS, statement_text(), object_text(), table_sql()
  sql/dialects.py, literals.py    non-finite floats, OVERRIDING SYSTEM VALUE, IDENTITY_INSERT; datetimeoffset text
  sql/select_builder.py           build_copy_select()
  adapters/base.py                capabilities run_file, dump; DataTools, Dumping
  adapters/columns.py             (new) TableColumn, TypeFamily, not_copied()
  adapters/dump.py                (new) DumpObject, DumpTable, DumpCatalog, DumpSession, Ordering
  adapters/{postgres,mysql,mssql,sqlite}/columns.py   (new) each engine's table_columns()
  adapters/{postgres,mysql,mssql,sqlite}/dump.py      (new) each engine's dump session and catalog
  adapters/{postgres,mysql,mssql,sqlite}/editing.py   edit targets read table_columns
  adapters/postgres/ddl.py        table_parts(); sequence_ddl, type_ddl public
  adapters/mssql/introspection.py table_statements(); sequence_ddl(at_next_value=); type / synonym DDL public
  adapters/*/adapter.py           capabilities; table_columns; dump_session / dump_catalog; SQLite's statement cache off
  api/data.py                     (new) /data/jobs, /data/jobs/cancel, /data/jobs/clear, /data/run-file/inspect,
                                  /data/dump/plan, /data/reveal
  app.py, container.py            the router; AppContainer.jobs, AppContainer.data
backend/tests/
  data_support.py                 (new) scratch_name, dropping, scratch_database, Ran, JobTab, DataApp
  fixtures/run_file/*.sql         (new) pg_dump (INSERT and COPY forms), mysqldump (with and without --hex-blob),
                                  sqlite3 .dump, an SSMS-style script (CRLF)
  sql/test_statement_stream.py, sql/test_dump_text.py, test_job_framework.py, test_run_file_job.py, test_run_file.py,
  test_run_file_inspect.py, test_table_columns.py, test_postgres_dump_catalog.py, test_mysql_sqlite_dump_catalog.py,
  test_mssql_dump_catalog.py, test_dump_job.py, test_dump.py   (new)
  sql/test_classifier.py, test_history.py, test_safety.py   (extended)
src/lib/api/{openapi.json, schema.d.ts}   regenerated;  src/lib/api/types.ts   the new exports
src/features/history/HistoryPanel.tsx     the running and interrupted statuses
CLAUDE.md, docs/superpowers/specs/2026-10-20-06-data-tools-design.md
```

Task order follows dependencies. The pure split and stream (1) come first, since the run needs them. The job framework (2) comes before either job. Run SQL file (3) needs both and is complete on its own: 06b can build its dialog after it. Table columns (4) come before any dump, since the catalogs read them, and so do ⑤'s targets. The engines' catalogs (5–7) come next, then the file's text (8), then the dump job, its routes and the round trips (9). Measurements, the packaged check and the docs (10) close.

| # | Task | Suggested implementer |
|---|---|---|
| 1 | Streaming split and the classifier's large INSERT (A6) | Opus (lexer state, chunk boundaries) |
| 2 | The job framework: worker, context, registry, History, gate, `/data/jobs` | Opus (concurrency, protocol) |
| 3 | Run SQL file: `RunFileJob`, inspect, `run_file`; other tools' files (A8) | Opus (transactions, every engine) |
| 4 | `DataTools.table_columns` and `TypeFamily`; edit targets read them (A1, A13, A15) | Opus (catalogs) |
| 5 | `Dumping` and the PostgreSQL catalog (A2) | Opus (catalog, dependencies) |
| 6 | The MySQL and SQLite catalogs (A3, A5, A14) | Opus (catalogs) |
| 7 | The SQL Server catalog (A4) | Opus (catalog, driver) |
| 8 | The dump's text: `InsertBatcher`, `DumpScript`, `build_copy_select` | Sonnet |
| 9 | `DumpJob`, `/data/dump/plan`, `/data/reveal`, `dump`; the round trips (A7) | Opus (files, every engine) |
| 10 | Measurements (A10), the frozen sidecar, CLAUDE.md and the spec | Sonnet |

---

### Task 1: Streaming split and the classifier's large INSERT (A6)

**Files:**
- Create: `backend/dbaguy_backend/sql/statement_stream.py`, `backend/tests/sql/test_statement_stream.py`
- Modify: `backend/dbaguy_backend/sql/classifier.py`, `backend/dbaguy_backend/sql/splitter.py`, `backend/tests/sql/test_classifier.py`

**Interfaces:**
- Consumes: `sql/lexer.scan` (a lazy token iterator) and the existing splitter's rules (`DELIMITER` and `GO` lines, PostgreSQL bodies, `StatementSpan`); `sql/tsql.py` (T-SQL batches); `sql/offsets.py`.
- Produces:
  - `sql/splitter.iter_spans(text, dialect, state: SplitState | None = None, *, final: bool = True, meta_commands: bool = False) -> Iterator[StatementSpan]`: one pass over `scan()`, no token lists. `final=False` holds back the last, possibly unfinished statement and leaves in `state` the delimiter and where to resume. `meta_commands=True` (PostgreSQL files) gives a psql backslash line as a span of its own with `meta=True`. `split()` is `list(iter_spans(...))` and gives exactly what it gave before (A6). `after_account` is still private here; Task 6 makes it public.
  - `SplitState(delimiter: str = ";", resume: int = 0)`; `StatementSpan.meta: bool = False`.
  - `sql/statement_stream.StatementStream(read: Callable[[int], bytes], dialect, *, chunk=STREAM_CHUNK, max_statement_chars=MAX_STATEMENT_CHARS)`: iterating it yields `FileStatement(index, line, text, end_byte, repeat, meta)`. `index` is 1-based and counts statements only (meta lines are not numbered). `line` is 1-based where the statement starts. `end_byte` is how many bytes of the file lie before its end (BOM included). `repeat` is the `GO n` count (1 elsewhere; 0 for `GO 0`). `.bytes_read` is the bytes read so far. Iterating raises `StreamError(code, message, line)` with `FILE_NOT_UTF8` (`Line N is not UTF-8 text`) or `STATEMENT_TOO_LARGE` (`The statement starting at line N is larger than 256 MiB`).
  - `sql/classifier.LARGE_INSERT_CHARS`: an INSERT (MySQL: also REPLACE) longer than this, alone and with no executable comment, is `WRITE` without sqlglot (D-06-4).

- [ ] **Step 1: A6 is this task's tests.** The assumption "`iter_spans` gives what `split()` gave on the whole corpus, and `StatementStream` gives the same at every cut" is checked by `test_split_is_unchanged_by_the_single_pass` (every case of `tests/sql/test_splitter.py` and `test_tsql.py`, run through both), `test_any_chunk_size_gives_the_same_statements` (30 chunk sizes) and `test_every_cut_gives_what_reading_the_file_whole_gives` (every byte position of five dialect corpora). If one differs, the splitter change is wrong. `split()`'s behaviour is binding (02a), so fix `iter_spans`, never the old expectations.

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/sql/test_statement_stream.py`:

```python
"""The streaming splitter of Run SQL file (spec ⑥ D-06-3, A6): read in chunks cut anywhere, a file gives exactly the
statements, lines and byte offsets of reading it whole. Pure."""

import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.splitter import iter_spans, split
from dbaguy_backend.sql.statement_stream import (
    FILE_NOT_UTF8, STATEMENT_TOO_LARGE, FileStatement, StatementStream, StreamError,
)

PG, MY, TS, LITE, SQ = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE, Dialect.SQLPP

CORPUS = {
    PG: (
        "\\restrict k3y\n"
        "SET statement_timeout = 0;\nSELECT pg_catalog.set_config('search_path', '', false);\n"
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$\nBEGIN\n  RETURN 1; -- ;\nEND;\n$fn$;\n"
        "INSERT INTO t VALUES (1, 'a;\nb'), (2, E'c\\';d'), (3, '中文😀');\r\n"
        "/* a /* nested ; */ comment */ SELECT 2;\n"
        "CREATE FUNCTION g(x int) RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT CASE WHEN x > 0 THEN 1 END; END;\n"
        "  \\connect other\n"
        "SELECT '\\not a command';\n"
        "\\unrestrict k3y\n"
    ),
    MY: (
        "/*!40101 SET NAMES utf8mb4 */;\nLOCK TABLES `t` WRITE;\n"
        "INSERT INTO `t` VALUES (1,'it''s \\\\ back'),(2,\"x;y\"),(3,'中文😀');\r\n"
        "DELIMITER ;;\n"
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1; SELECT 2;\nEND ;;\n"
        "DELIMITER ;\n"
        "# a hash comment ;\nSELECT 3; -- trailing\n"
        "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END;\n"
        "UNLOCK TABLES;\n"
    ),
    TS: (
        "SET NOCOUNT ON;\nGO\n"
        "CREATE PROCEDURE dbo.p AS\nSELECT 1;\nSELECT 'GO\nnot a line';\nGO\n"
        "INSERT INTO t VALUES (N'中文😀'), (N'x]y');\r\nGO 3\r\n"
        "/* GO\n */ PRINT 'done'\n  go\n"
        "SELECT 2"
    ),
    LITE: (
        "PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n"
        "CREATE TABLE t (a, \"b;\" TEXT);\n"
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET a = 1; SELECT CASE WHEN 1 THEN 2 END; END;\n"
        "INSERT INTO t VALUES(1,'x\ny;'),(2,X'00ff'),(3,'中文😀');\nCOMMIT;\n"
    ),
    SQ: (
        "UPSERT INTO ks (KEY, VALUE) VALUES ('k1', {\"a\": \";\"});\n"
        "SELECT `a;b` FROM ks WHERE x = $v; /* ; */\nDELETE FROM ks USE KEYS 'k1';\n"
    ),
}


class Pieces:
    """A file whose read() hands out the given pieces, whatever size is asked for, then b''."""

    def __init__(self, *pieces: bytes) -> None:
        self._pieces = [p for p in pieces if p]
        self.asked: list[int] = []

    def read(self, size: int) -> bytes:
        self.asked.append(size)
        return self._pieces.pop(0) if self._pieces else b""


def whole(data: bytes, dialect: Dialect) -> list[FileStatement]:
    """The statements of the file read in one piece, from the one-shot splitter (the reference)."""
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data[3:].decode() if bom else data.decode()
    found, index = [], 0
    for span in iter_spans(text, dialect, meta_commands=dialect is PG):
        index += not span.meta
        found.append(FileStatement(
            index=0 if span.meta else index, line=text.count("\n", 0, span.start) + 1, text=span.text,
            end_byte=len(text[:span.terminator_end].encode()) + 3 * bom, repeat=span.repeat, meta=span.meta,
        ))
    return found


def streamed(read, dialect: Dialect, **options) -> list[FileStatement]:
    return list(StatementStream(read, dialect, **options))


@pytest.mark.parametrize("dialect", list(CORPUS), ids=lambda d: d.value)
def test_every_cut_gives_what_reading_the_file_whole_gives(dialect):  # A6
    data = CORPUS[dialect].encode()
    expected = whole(data, dialect)
    assert len(expected) >= 3
    for cut in range(len(data) + 1):
        assert streamed(Pieces(data[:cut], data[cut:]).read, dialect) == expected, f"cut at byte {cut}"


@pytest.mark.parametrize("dialect", list(CORPUS), ids=lambda d: d.value)
@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 1 << 20])
def test_any_chunk_size_gives_the_same_statements(dialect, chunk):
    data = CORPUS[dialect].encode()

    class File:
        position = 0

        def read(self, size: int) -> bytes:
            piece = data[self.position:self.position + size]
            self.position += len(piece)
            return piece

    assert streamed(File().read, dialect, chunk=chunk) == whole(data, dialect)


def test_split_is_unchanged_by_the_single_pass():
    for dialect, text in CORPUS.items():  # meta lines are only spans of their own in a file
        assert [s.text for s in split(text, dialect)] == [
            s.text for s in iter_spans(text, dialect) if not s.meta
        ]
    assert not any(s.meta for s in split(CORPUS[PG], PG))


def test_psql_meta_lines_are_spans_of_their_own_in_a_postgresql_file():
    statements = streamed(Pieces(CORPUS[PG].encode()).read, PG)
    meta = [(s.line, s.text) for s in statements if s.meta]
    assert meta == [(1, "\\restrict k3y"), (13, "\\connect other"), (15, "\\unrestrict k3y")]
    assert all(s.index == 0 for s in statements if s.meta)
    assert [s.index for s in statements if not s.meta] == list(range(1, 8))
    assert "SELECT '\\not a command'" in [s.text for s in statements]  # a backslash inside a statement is not one


def test_line_numbers_and_byte_offsets():
    data = "SELECT 1;\n\n-- note\nSELECT 'a\nb';\r\nSELECT '中';".encode()
    statements = streamed(Pieces(data).read, PG)
    assert [(s.index, s.line, s.text) for s in statements] == [
        (1, 1, "SELECT 1"), (2, 4, "SELECT 'a\nb'"), (3, 6, "SELECT '中'"),
    ]
    assert [s.end_byte for s in statements] == [9, 9 + len("\n\n-- note\nSELECT 'a\nb';"), len(data)]


def test_a_bom_is_dropped_and_counted_in_the_bytes():
    data = b"\xef\xbb\xbfSELECT 1;\nSELECT 2;\n"
    for cut in range(len(data) + 1):
        statements = streamed(Pieces(data[:cut], data[cut:]).read, MY)
        assert [(s.text, s.end_byte) for s in statements] == [("SELECT 1", 12), ("SELECT 2", 22)], cut


def test_go_counts_and_crlf_line_ends():
    data = b"SELECT 1\r\nGO 3\r\nSELECT 2\r\ngo\r\n"
    assert [(s.text, s.repeat, s.line) for s in streamed(Pieces(data).read, TS)] == [
        ("SELECT 1", 3, 1), ("SELECT 2", 1, 3),
    ]


def test_text_that_is_not_utf8_stops_at_its_line():
    data = b"SELECT 1;\nSELECT 2;\nSELECT '\xff';\n"
    for cut in range(len(data) + 1):
        with pytest.raises(StreamError) as raised:
            streamed(Pieces(data[:cut], data[cut:]).read, PG)
        assert (raised.value.code, raised.value.line, raised.value.message) == (
            FILE_NOT_UTF8, 3, "Line 3 is not UTF-8 text"), cut


def test_a_statement_past_the_limit_stops_at_its_first_line():
    data = b"SELECT 1;\n\n  INSERT INTO t VALUES\n" + b"(1),\n" * 50 + b"(2);\n"
    with pytest.raises(StreamError) as raised:
        streamed(Pieces(data).read, PG, max_statement_chars=100)
    assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 3)
    one_line = b"SELECT '" + b"x" * 500 + b"'"  # no line break at all: still stopped, never held whole
    with pytest.raises(StreamError) as raised:
        streamed(Pieces(*[one_line[i:i + 10] for i in range(0, len(one_line), 10)]).read, PG,
                 max_statement_chars=100)
    assert (raised.value.code, raised.value.line) == (STATEMENT_TOO_LARGE, 1)
    assert len(streamed(Pieces(data).read, PG, max_statement_chars=1000)) == 2  # under the limit: fine


def test_a_statement_that_goes_on_doubles_the_next_read():
    data = b"INSERT INTO t VALUES\n" + b"(1),\n" * 1000 + b"(2);\nSELECT 1;\n"

    class File:
        position = 0
        asked: list[int] = []

        def read(self, size: int) -> bytes:
            self.asked.append(size)
            piece = data[self.position:self.position + size]
            self.position += len(piece)
            return piece

    file = File()
    assert [s.text[:6] for s in streamed(file.read, PG, chunk=16)] == ["INSERT", "SELECT"]
    assert file.asked[:10] == [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 16]  # log2(size / chunk) rescans


def test_bytes_read_counts_the_whole_file():
    data = CORPUS[MY].encode()
    stream = StatementStream(Pieces(data[:100], data[100:]).read, MY)
    list(stream)
    assert stream.bytes_read == len(data)
```

Append to `backend/tests/sql/test_classifier.py`:

```python


# --- D-06-4: a large INSERT is judged by its first word ---------------------------------------------------------------

_ROWS = ", ".join(f"({i}, 'name {i} it''s', {i}.5, NULL)" for i in range(2_000))  # about 64 KiB and more
LARGE_INSERTS = [
    (PG, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS}"),
    (PG, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS} ON CONFLICT (a) DO UPDATE SET b = excluded.b"),
    (MY, f"INSERT INTO t (a, b, c, d) VALUES {_ROWS} ON DUPLICATE KEY UPDATE b = VALUES(b)"),
    (MY, f"REPLACE INTO t (a, b, c, d) VALUES {_ROWS}"),
    (LITE, f"INSERT OR ABORT INTO t (a, b, c, d) VALUES {_ROWS}"),
    (Dialect.TSQL, f"INSERT INTO dbo.t (a, b, c, d) VALUES {_ROWS}"),
    (PG, f"INSERT INTO t SELECT * FROM u WHERE b IN ({_ROWS.replace('NULL', '0')})"),
]


@pytest.mark.parametrize(("dialect", "sql"), LARGE_INSERTS)
def test_a_large_insert_gets_the_answer_sqlglot_gives(monkeypatch, dialect, sql):
    assert len(sql) > classifier.LARGE_INSERT_CHARS
    capped = classify(sql, dialect)
    monkeypatch.setattr(classifier, "LARGE_INSERT_CHARS", 10**12)
    assert classify(sql, dialect) == capped == Classification(kind=K.WRITE)


def test_a_large_insert_never_hides_a_second_statement():
    drop = f"DELIMITER $$\nINSERT INTO t VALUES {_ROWS}; DROP TABLE t$$"  # one span under the custom delimiter
    result = classify(drop, MY)
    assert (result.kind, result.dangerous_reasons) == (K.DDL, [DangerReason.DROP])
    hinted = f"INSERT /*+ SET_VAR(sort_buffer_size = 16M) */ INTO t VALUES {_ROWS}"  # an executable comment: full rules
    assert classify(hinted, MY).kind == K.WRITE
    versioned = f"/*!40000 INSERT INTO t VALUES {_ROWS} */"
    assert classify(versioned, MY).kind == K.UNKNOWN


def test_a_large_insert_is_classified_without_its_tokens_in_memory():
    import tracemalloc

    sql = f"INSERT INTO t (a, b, c, d) VALUES {', '.join([_ROWS] * 8)}"  # about 0.6 MB
    tracemalloc.start()
    try:
        assert classify(sql, PG) == Classification(kind=K.WRITE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * len(sql), f"peak {peak:,} bytes for a {len(sql):,}-character INSERT"  # sqlglot: about 113 times
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_statement_stream.py tests/sql/test_classifier.py`
Expected: collection error, `ModuleNotFoundError: No module named 'dbaguy_backend.sql.statement_stream'` (and `LARGE_INSERT_CHARS` cannot be imported).

- [ ] **Step 4: The single-pass splitter**

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
import re
from dataclasses import dataclass

from .dialects import RULES, BodyRule, ClientCommand, Dialect
```

with:

```python
import re
from collections.abc import Iterator
from dataclasses import dataclass

from .dialects import RULES, BodyRule, ClientCommand, Dialect
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
_GO_REST = re.compile(r"(?:[ \t]+(\d+))?[ \t]*(?:--[^\r\n]*)?\r?(?=\n|\Z)")  # after GO: [count] [-- comment], line end
_MODULES = frozenset({"PROC", "PROCEDURE", "FUNCTION", "TRIGGER", "VIEW"})
_MAX_COUNT_DIGITS = 9  # `GO n` counts longer than this are read as 10**9


@dataclass(frozen=True, slots=True)
```

with:

```python
_GO_REST = re.compile(r"(?:[ \t]+(\d+))?[ \t]*(?:--[^\r\n]*)?\r?(?=\n|\Z)")  # after GO: [count] [-- comment], line end
_MODULES = frozenset({"PROC", "PROCEDURE", "FUNCTION", "TRIGGER", "VIEW"})
_MAX_COUNT_DIGITS = 9  # `GO n` counts longer than this are read as 10**9
_MODULE_HEAD = 4  # CREATE OR ALTER PROCEDURE: the significant tokens that tell whether a T-SQL batch defines a module


@dataclass(frozen=True, slots=True)
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
    has_body: bool = False
    repeat: int = 1  # T-SQL "GO n": times the batch runs (0 for GO 0)
    params: tuple[str, ...] = ()  # the parameters it uses (SQL++): $name, $1, and each ?


class _BodyTracker:
```

with:

```python
    has_body: bool = False
    repeat: int = 1  # T-SQL "GO n": times the batch runs (0 for GO 0)
    params: tuple[str, ...] = ()  # the parameters it uses (SQL++): $name, $1, and each ?
    meta: bool = False  # a psql meta-command line (`iter_spans(meta_commands=True)` only): never sent to the server


class _BodyTracker:
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
    return _CompoundTracker() if rule is BodyRule.COMPOUND else _BodyTracker(rule)


def _params(tokens: list[Token]) -> tuple[str, ...]:
    """The parameters of one statement in order of first use: `$x` for both `$x` and `@x` (one parameter), `$1`…, and
    every `?` (each one takes the next positional argument)."""
    named: dict[str, None] = {}
    found: list[str] = []
    for token in tokens:
        if token.kind != TokenKind.PARAM:
            continue
        if token.text == "?":
            found.append("?")
        elif (name := "$" + token.text[1:]) not in named:
            named[name] = None
            found.append(name)
    return tuple(found)


def _emit(
    spans: list[StatementSpan], text: str, tokens: list[Token], terminator_end: int | None, has_body: bool,
    repeat: int = 1, params: bool = False,
) -> None:
    significant = [t for t in tokens if t.kind not in INSIGNIFICANT]
    if not significant:
        return
    start, end = significant[0].start, significant[-1].end
    terminator = terminator_end if terminator_end is not None else end
    spans.append(
        StatementSpan(start, end, terminator, text[start:end], has_body, repeat, _params(significant) if params else ())
    )


def _first_on_line(text: str, token: Token) -> bool:
```

with:

```python
    return _CompoundTracker() if rule is BodyRule.COMPOUND else _BodyTracker(rule)


class _Accumulator:
    """What one statement's span needs from its tokens, kept as they go by instead of the tokens themselves (D-06-3:
    a 50 MB INSERT is millions of tokens): where its first and last significant tokens are, its first few significant
    tokens (T-SQL reads whether a batch defines a module from them) and, when the dialect has them, its parameters."""

    __slots__ = ("start", "end", "head", "_named", "params", "_with_params")

    def __init__(self, with_params: bool) -> None:
        self.start: int | None = None
        self.end = 0
        self.head: list[Token] = []
        self._named: set[str] = set()
        self.params: list[str] = []
        self._with_params = with_params

    @property
    def started(self) -> bool:
        return self.start is not None

    def add(self, token: Token) -> None:
        """A significant token of the statement."""
        if self.start is None:
            self.start = token.start
        self.end = token.end
        if len(self.head) < _MODULE_HEAD:
            self.head.append(token)
        if self._with_params and token.kind == TokenKind.PARAM:
            if token.text == "?":  # each `?` takes the next positional argument
                self.params.append("?")
            elif (name := "$" + token.text[1:]) not in self._named:  # `$x` for both `$x` and `@x`: one parameter
                self._named.add(name)
                self.params.append(name)

    def span(self, text: str, terminator_end: int | None, has_body: bool, repeat: int = 1) -> StatementSpan | None:
        """The statement's span, None when it holds no significant token (an empty statement, a comment)."""
        if self.start is None:
            return None
        terminator = terminator_end if terminator_end is not None else self.end
        return StatementSpan(self.start, self.end, terminator, text[self.start:self.end], has_body, repeat,
                             tuple(self.params))


def _first_on_line(text: str, token: Token) -> bool:
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
    if started or token.kind != TokenKind.WORD or token.upper != "DELIMITER" or not _first_on_line(text, token):
        return None
    return _DELIMITER_ARGUMENT.match(text, token.end)


def _go_line(text: str, token: Token) -> re.Match[str] | None:
```

with:

```python
    if started or token.kind != TokenKind.WORD or token.upper != "DELIMITER" or not _first_on_line(text, token):
        return None
    return _DELIMITER_ARGUMENT.match(text, token.end)


def _meta_line(text: str, token: Token, started: bool) -> int | None:
    """A psql meta-command (`\\restrict …`, `\\connect db`): a backslash first on its line where no statement has
    started (D-06-3). Where its line ends (the newline excluded), or None when the token starts none."""
    if started or token.kind != TokenKind.OTHER or token.text != "\\" or not _first_on_line(text, token):
        return None
    end = text.find("\n", token.start)
    return len(text) if end < 0 else end


def _go_line(text: str, token: Token) -> re.Match[str] | None:
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
def _defines_module(significant: list[Token]) -> bool:
    """CREATE [OR ALTER] | ALTER  PROC|PROCEDURE|FUNCTION|TRIGGER|VIEW. T-SQL requires such a statement to be the only
    one in its batch, so everything after it, up to the GO line, is the module's body and runs later, not now."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in significant[:4]]
    if words[:3] == ["CREATE", "OR", "ALTER"]:
        return words[3:4] != [] and words[3] in _MODULES
    return words[:1] in (["CREATE"], ["ALTER"]) and len(words) > 1 and words[1] in _MODULES


def _split_batches(text: str, dialect: Dialect) -> list[StatementSpan]:
    """T-SQL: batches end at GO lines (spec §3.3); `;` never splits a batch, because variables and temporary tables
    live as long as the batch."""
    spans: list[StatementSpan] = []
    start = 0
    while True:
        current: list[Token] = []
        line: re.Match[str] | None = None
        for token in scan(text, dialect, start):
            if line := _go_line(text, token):
                break
            current.append(token)
        significant = [t for t in current if t.kind not in INSIGNIFICANT]
        if line is None:
            _emit(spans, text, current, None, _defines_module(significant))
            return spans
        _emit(spans, text, current, line.end(), _defines_module(significant), _go_count(line.group(1)))
        start = line.end()


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    rules = RULES[dialect]
    if rules.client_command is ClientCommand.GO:
        return _split_batches(text, dialect)
    commands = rules.client_command is ClientCommand.DELIMITER
    with_params = bool(rules.named_param_prefixes or rules.question_params)
    spans: list[StatementSpan] = []
    delimiter: str | None = None  # set by a DELIMITER line; None: `;` ends statements (outside bodies)
    start = 0
    while True:
        tracker = _tracker(rules.body_rule)
        current: list[Token] = []
        started = False  # `current` holds a significant token: a statement has begun
        restart: int | None = None
        for token in scan(text, dialect, start, delimiter):
            if commands and (line := _delimiter_line(text, token, started)):
                delimiter = None if line.group(1) == ";" else line.group(1)
                restart = line.end()  # the line is consumed: it belongs to no statement and never reaches the server
                break
            if token.kind == TokenKind.DELIMITER or (
                delimiter is None and token.kind == TokenKind.SEMI and tracker.depth == 0
            ):
                _emit(spans, text, current, token.end, tracker.has_body, params=with_params)
                current, tracker, started = [], _tracker(rules.body_rule), False
                continue
            current.append(token)
            if token.kind not in INSIGNIFICANT:
                started = True
                tracker.feed(token)  # also under a custom delimiter: has_body tells the classifier about bodies
        if restart is None:
            _emit(spans, text, current, None, tracker.has_body, params=with_params)
            return spans
        start = restart
```

with:

```python
def _defines_module(significant: list[Token]) -> bool:
    """CREATE [OR ALTER] | ALTER  PROC|PROCEDURE|FUNCTION|TRIGGER|VIEW. T-SQL requires such a statement to be the only
    one in its batch, so everything after it, up to the GO line, is the module's body and runs later, not now."""
    words = [t.upper if t.kind == TokenKind.WORD else "" for t in significant[:_MODULE_HEAD]]
    if words[:3] == ["CREATE", "OR", "ALTER"]:
        return words[3:4] != [] and words[3] in _MODULES
    return words[:1] in (["CREATE"], ["ALTER"]) and len(words) > 1 and words[1] in _MODULES


@dataclass
class SplitState:
    """Where splitting stands between two calls of `iter_spans` over a growing text (`StatementStream`): the MySQL
    terminator a DELIMITER line set (None: `;`), and where the statement that has not ended yet begins — the end of
    the last terminator (or DELIMITER or GO line) read, so its leading comments come along."""

    delimiter: str | None = None
    resume: int = 0


def _iter_batches(text: str, dialect: Dialect, state: SplitState, final: bool) -> Iterator[StatementSpan]:
    """T-SQL: batches end at GO lines (spec §3.3); `;` never splits a batch, because variables and temporary tables
    live as long as the batch."""
    start = state.resume
    while True:
        acc = _Accumulator(with_params=False)
        line: re.Match[str] | None = None
        for token in scan(text, dialect, start):
            if line := _go_line(text, token):
                break
            if token.kind not in INSIGNIFICANT:
                acc.add(token)
        if line is None:
            if final and (span := acc.span(text, None, _defines_module(acc.head))) is not None:
                yield span
            return
        if (span := acc.span(text, line.end(), _defines_module(acc.head), _go_count(line.group(1)))) is not None:
            yield span
        start = state.resume = line.end()


def _iter_statements(
    text: str, dialect: Dialect, state: SplitState, final: bool, meta_commands: bool,
) -> Iterator[StatementSpan]:
    rules = RULES[dialect]
    commands = rules.client_command is ClientCommand.DELIMITER
    with_params = bool(rules.named_param_prefixes or rules.question_params)
    start = state.resume
    while True:
        tracker = _tracker(rules.body_rule)
        acc = _Accumulator(with_params)
        restart: int | None = None
        for token in scan(text, dialect, start, state.delimiter):
            if commands and (line := _delimiter_line(text, token, acc.started)):
                state.delimiter = None if line.group(1) == ";" else line.group(1)
                restart = line.end()  # the line is consumed: it belongs to no statement and never reaches the server
                break
            if meta_commands and (line_end := _meta_line(text, token, acc.started)) is not None:
                if line_end == len(text) and not final:
                    return  # its line may go on in the next chunk: it is the statement that has not ended yet
                yield StatementSpan(token.start, line_end, line_end, text[token.start:line_end], meta=True)
                restart = line_end
                break
            if token.kind == TokenKind.DELIMITER or (
                state.delimiter is None and token.kind == TokenKind.SEMI and tracker.depth == 0
            ):
                if (span := acc.span(text, token.end, tracker.has_body)) is not None:
                    yield span
                state.resume = token.end
                tracker, acc = _tracker(rules.body_rule), _Accumulator(with_params)
                continue
            if token.kind not in INSIGNIFICANT:
                acc.add(token)
                tracker.feed(token)  # also under a custom delimiter: has_body tells the classifier about bodies
        if restart is None:
            if final and (span := acc.span(text, None, tracker.has_body)) is not None:
                yield span
            return
        start = state.resume = restart


def iter_spans(
    text: str, dialect: Dialect, state: SplitState | None = None, *, final: bool = True, meta_commands: bool = False,
) -> Iterator[StatementSpan]:
    """The statements of `text[state.resume:]` in one pass, keeping no token list (D-06-3).

    `final` False: `text` is a prefix of a longer text that ends at a line break (`StatementStream`); the statement that
    has not ended at its end is not given, and `state` says where it begins and which MySQL terminator is in force, so
    a later call over the longer text, from the start of that line, goes on from there. `meta_commands`: a psql
    meta-command line (PostgreSQL files) is a span of its own with `meta` set."""
    state = state if state is not None else SplitState()
    if RULES[dialect].client_command is ClientCommand.GO:
        return _iter_batches(text, dialect, state, final)
    return _iter_statements(text, dialect, state, final, meta_commands)


def split(text: str, dialect: Dialect) -> list[StatementSpan]:
    return list(iter_spans(text, dialect))
```

- [ ] **Step 5: The statement stream**

Create `backend/dbaguy_backend/sql/statement_stream.py`:

```python
"""A SQL file read as statements, a chunk at a time (spec ⑥ D-06-3): what Run SQL file sends.

The file is read in chunks of `STREAM_CHUNK` bytes; only the text up to the chunk's last line break is split (a `GO`
or `DELIMITER` line can only be told whole), statements that ended are given out and the one that has not waits for
the next chunk, which then rescans it from the start of its line. A statement that stays unfinished doubles the next
read, so a large one is rescanned only a few times; one that grows past `MAX_STATEMENT_CHARS` stops the stream. Each
statement carries the line it starts on, its number (psql meta-command lines are not numbered) and how many bytes of
the file lie before its end. Pure: the bytes come from the `read` callable it is given."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .dialects import Dialect
from .splitter import SplitState, iter_spans

# Bytes read at a time. Not the spec's 1 MiB (D-06-3): reading 1 MiB at a time left the process 70–90 MB larger after a
# few hundred 1,000-row INSERTs (macOS malloc keeps the freed multi-megabyte strings of text with an emoji in it, which
# Python holds 4 bytes a character), 512 KiB 15 MB, at 8% of the splitting speed (⑥ A10, measured while planning 06a)
STREAM_CHUNK = 512 * 1024
MAX_STATEMENT_CHARS = 256 * 1_048_576  # one statement, as text
FILE_NOT_UTF8 = "file_not_utf8"
STATEMENT_TOO_LARGE = "statement_too_large"
_BOM = b"\xef\xbb\xbf"
_SPACE = re.compile(r"\s*")
_MEBIBYTE = 1_048_576


class StreamError(Exception):
    """The file cannot be read on as statements: `code` is FILE_NOT_UTF8 or STATEMENT_TOO_LARGE, `line` 1-based."""

    def __init__(self, code: str, message: str, line: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


@dataclass(frozen=True, slots=True)
class FileStatement:
    index: int  # 1-based among the file's statements; 0 for a psql meta-command line
    line: int  # 1-based line of its first character
    text: str
    end_byte: int  # bytes of the file up to the end of its terminator (the BOM included)
    repeat: int = 1  # T-SQL `GO n`
    meta: bool = False  # a psql meta-command line (PostgreSQL): never sent


class _Cursor:
    """A position in the current text with what lies before it: its line number and its byte offset in the file."""

    __slots__ = ("char", "line", "byte")

    def __init__(self, byte: int) -> None:
        self.char, self.line, self.byte = 0, 1, byte

    def advance(self, text: str, to: int) -> None:
        if to > self.char:
            piece = text[self.char:to]
            self.line += piece.count("\n")
            self.byte += len(piece.encode("utf-8"))
            self.char = to

    def rebase(self, text: str, start: int) -> None:
        """The text now starts at `start`: positions before it are gone."""
        self.advance(text, start)
        self.char -= start


class StatementStream:
    """`for statement in StatementStream(file.read, dialect)`. Raises StreamError from the iteration."""

    def __init__(
        self, read: Callable[[int], bytes], dialect: Dialect, *, chunk: int = STREAM_CHUNK,
        max_statement_chars: int = MAX_STATEMENT_CHARS,
    ) -> None:
        self._read = read
        self._dialect = dialect
        self._chunk = chunk
        self._max = max_statement_chars
        self._meta = dialect is Dialect.POSTGRES  # psql meta-commands: `\restrict` in pg_dump's output (P8)
        self.bytes_read = 0  # of the file, so far

    def __iter__(self) -> Iterator[FileStatement]:
        pending = b""  # read, after the last line break
        text = ""  # decoded: from the start of the line where the statement that has not ended begins
        state = SplitState()
        spans_at = _Cursor(byte=0)  # where the last statement given out ended
        lines_at = _Cursor(byte=0)  # where the last statement given out began (its line)
        index = 0
        size = self._chunk
        bom_checked = False
        while True:
            data = self._read(size)
            self.bytes_read += len(data)
            eof = not data
            pending += data
            if not bom_checked:
                if len(pending) < len(_BOM) and not eof:
                    continue
                bom_checked = True
                if pending.startswith(_BOM):
                    pending = pending[len(_BOM):]
                    spans_at.byte = lines_at.byte = len(_BOM)
            cut = len(pending) if eof else pending.rfind(b"\n") + 1
            complete, pending = pending[:cut], pending[cut:]
            start_line = lines_at.line + text.count("\n", lines_at.char)
            try:
                text += complete.decode("utf-8")
            except UnicodeDecodeError as exc:
                line = start_line + complete.count(b"\n", 0, exc.start)
                raise StreamError(FILE_NOT_UTF8, f"Line {line:,} is not UTF-8 text", line) from None
            ended = False
            for span in iter_spans(text, self._dialect, state, final=eof, meta_commands=self._meta):
                lines_at.advance(text, span.start)
                if len(span.text) > self._max:  # it ended inside one large read
                    raise self._too_large(lines_at.line)
                spans_at.advance(text, span.terminator_end)
                index += not span.meta
                yield FileStatement(
                    index=0 if span.meta else index, line=lines_at.line, text=span.text, end_byte=spans_at.byte,
                    repeat=span.repeat, meta=span.meta,
                )
                ended = True
            if eof:
                return
            line_start = text.rfind("\n", 0, state.resume) + 1
            spans_at.rebase(text, line_start)
            lines_at.rebase(text, line_start)
            text, state.resume = text[line_start:], state.resume - line_start
            if len(text) - state.resume + len(pending) > self._max:
                first = _SPACE.match(text, state.resume).end()
                raise self._too_large(lines_at.line + text.count("\n", lines_at.char, first))
            # a statement that goes on: read more at once, so it is rescanned fewer times (never more than the limit)
            size = self._chunk if ended else min(size * 2, self._max)

    def _too_large(self, line: int) -> StreamError:
        return StreamError(
            STATEMENT_TOO_LARGE, f"The statement starting at line {line:,} is larger than {self._max // _MEBIBYTE:,} MiB",
            line,
        )
```

`STREAM_CHUNK` is 512 KiB, not the 1 MiB the spec suggests (S5). Splitting a 231 MB dump of 1,000 statements in 1 MiB chunks peaks 91 MB above the start; in 512 KiB chunks, 28 MB (38.7 vs 42.7 s, measured while planning).

- [ ] **Step 6: The classifier's limit**

In `backend/dbaguy_backend/sql/classifier.py`, replace:

```python
from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import KEYWORD_ONLY_DIALECTS, Dialect
from .lexer import (
    HINT_COMMENT_START, INSIGNIFICANT, VERSION_COMMENT_STARTS, Token, TokenKind, tokenize, with_ascii_spaces,
)
from .splitter import split
from .tsql import statements
```

with:

```python
from ..domain.classification import Classification, DangerReason, StatementKind
from .dialects import KEYWORD_ONLY_DIALECTS, Dialect
from .lexer import (
    HINT_COMMENT_START, INSIGNIFICANT, VERSION_COMMENT_STARTS, Token, TokenKind, scan, tokenize, with_ascii_spaces,
)
from .splitter import split
from .tsql import statements
```

In `backend/dbaguy_backend/sql/classifier.py`, replace:

```python
_SELECT_FAMILY = ("SELECT", "WITH")
_SQLPP_SELECT_FIRST = (*_SELECT_FAMILY, "FROM")  # the words a SQL++ select starts with (T-SQL has no FROM-first)
_DML_WORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})  # the statements that write rows, in every dialect
_SQLPP_DML = _DML_WORDS | {"UPSERT"}
_SQLPP_SEQUENCE_ADVANCE = (("NEXT", "VALUE", "FOR"), ("NEXTVAL", "FOR"))  # PREV VALUE FOR only reads
_SQLPP_FILTERS = (("WHERE",), ("USE", "KEYS"), ("ON", "KEYS"))  # a statement given its documents is targeted
```

with:

```python
_SELECT_FAMILY = ("SELECT", "WITH")
_SQLPP_SELECT_FIRST = (*_SELECT_FAMILY, "FROM")  # the words a SQL++ select starts with (T-SQL has no FROM-first)
_DML_WORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})  # the statements that write rows, in every dialect
# D-06-4: an INSERT longer than this is judged by its first word alone. sqlglot answers WRITE without a reason for every
# INSERT (ON CONFLICT, ON DUPLICATE KEY, INSERT … SELECT, REPLACE INTO included; A X1) — the keyword rules' answer — but
# parses a 1,000-row INSERT in 150–210 ms with a memory peak of about 113 times its text (10 MB: about 1.1 GB).
LARGE_INSERT_CHARS = 64 * 1024
_BULK_INSERT_WORDS: dict[Dialect, frozenset[str]] = {Dialect.MYSQL: frozenset({"INSERT", "REPLACE"})}
_INSERT_WORDS = frozenset({"INSERT"})
_SQLPP_DML = _DML_WORDS | {"UPSERT"}
_SQLPP_SEQUENCE_ADVANCE = (("NEXT", "VALUE", "FOR"), ("NEXTVAL", "FOR"))  # PREV VALUE FOR only reads
_SQLPP_FILTERS = (("WHERE",), ("USE", "KEYS"), ("ON", "KEYS"))  # a statement given its documents is targeted
```

In `backend/dbaguy_backend/sql/classifier.py`, replace:

```python
    return _classify_single(sql, dialect, has_body)


def _classify_single(sql: str, dialect: Dialect, has_body: bool = False) -> Classification:
    """One statement; `has_body`: it holds a routine or trigger body."""
    tokens = tokenize(sql, dialect)
    if dialect is Dialect.SQLPP:
        return _classify_sqlpp(tokens)  # keyword rules only: no version comments, no sqlglot, no EXPLAIN ANALYZE
```

with:

```python
    return _classify_single(sql, dialect, has_body)


def _bulk_insert(sql: str, dialect: Dialect) -> bool:
    """`sql` is one INSERT (MySQL: or REPLACE), read lazily (never a token list: D-06-3's concern too). An executable
    comment anywhere, or a second statement after a `;`, leaves it to the full rules."""
    words = _BULK_INSERT_WORDS.get(dialect, _INSERT_WORDS)
    first, ended = True, False
    for token in scan(sql, dialect):
        if token.kind in INSIGNIFICANT:
            continue
        if token.kind == TokenKind.EXEC_COMMENT or ended:
            return False
        if first:
            if token.kind != TokenKind.WORD or token.upper not in words:
                return False
            first = False
        elif token.kind == TokenKind.SEMI:
            ended = True
    return not first


def _classify_single(sql: str, dialect: Dialect, has_body: bool = False) -> Classification:
    """One statement; `has_body`: it holds a routine or trigger body."""
    if dialect is not Dialect.SQLPP and len(sql) > LARGE_INSERT_CHARS and _bulk_insert(sql, dialect):
        return Classification(kind=StatementKind.WRITE)  # what the keyword rules and sqlglot both say (D-06-4)
    tokens = tokenize(sql, dialect)
    if dialect is Dialect.SQLPP:
        return _classify_sqlpp(tokens)  # keyword rules only: no version comments, no sqlglot, no EXPLAIN ANALYZE
```

- [ ] **Step 7: Run the tests and the splitter's own**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_statement_stream.py tests/sql/test_classifier.py tests/sql/test_splitter.py tests/sql/test_tsql.py`
Expected: `790 passed` (44 of them new in `test_statement_stream.py`, 3 tests (9 cases) new in `test_classifier.py`).

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/sql/classifier.py \
  backend/dbaguy_backend/sql/splitter.py \
  backend/dbaguy_backend/sql/statement_stream.py \
  backend/tests/sql/test_classifier.py \
  backend/tests/sql/test_statement_stream.py
git commit -m "feat(06a): single-pass splitter, streaming statement reader, classifier limit for large INSERTs"
```

---

### Task 2: The job framework: worker, context, registry, History, gate, `/data/jobs`

**Files:**
- Create: `backend/dbaguy_backend/api/data.py`, `backend/dbaguy_backend/domain/data_jobs.py`, `backend/dbaguy_backend/sessions/jobs/__init__.py`, `backend/dbaguy_backend/sessions/jobs/context.py`, `backend/dbaguy_backend/sessions/jobs/registry.py`, `backend/tests/test_job_framework.py`
- Modify: `backend/dbaguy_backend/app.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/domain/execution.py`, `backend/dbaguy_backend/domain/history.py`, `backend/dbaguy_backend/services/history_service.py`, `backend/dbaguy_backend/services/safety.py`, `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/sessions/manager.py`, `backend/dbaguy_backend/sessions/worker.py`, `backend/tests/test_history.py`, `backend/tests/test_safety.py`, `src/features/history/HistoryPanel.tsx`, `src/lib/api/types.ts`
- Regenerate (never by hand): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: `SessionWorker` (`_submit_request`, `_start_request`, `_run_statement`, `_raise_if_cancelled`, `_set_running`, `_close_result`, `_discard_open`, `_send`, `_notify`, `_emit_lock`), `WorkerHooks`, `SessionManager` (reaper, attach), `HistoryService`, `SafetyPolicy.check`, `domain/execution.ServerEvent`, `transaction_open_error`.
- Produces:
  - `domain/data_jobs.py`: `JobKind = Literal["dump", "run_file"]`; `JobOutcomeKind = Literal["completed", "failed", "rolled_back", "partial", "stopped", "unknown", "not_started"]`; `JobStage`; `JobCommand(BaseModel)` with the class variable `job_kind` and `request_id`; `JobError(line, statement, code, message)`; `StoppedAt(line, statement)`; `JobObject(kind, name, rows)`; `JobProgressEvent{request_id, stage, object, rows, bytes, total_bytes, statements, errors, line}`; `JobOutcomeEvent{request_id, job, outcome, cancelled, rows, bytes, statements, path, objects, errors, error_count, stopped_at, warnings}`; `JobInfo{session_id, request_id, job, title, profile_id, started_at, progress, outcome}` (the list's row). Constants as in Global Constraints.
  - `sessions/jobs/context.py`: `Job` (`title`, `run(ctx) -> JobReport`, `broke(ctx, exc) -> JobReport`: `unknown` once something was sent, else `not_started`); `JobContext` (`connection`, `dialect`, `capabilities`, `hooks`, `progress: JobProgress`, `sent`, `run(sql, options) -> StatementResult`, `drain(result) -> int | None` (rows affected), `close(result)`, `raise_if_cancelled()`, `stop_cancels() -> bool` (True when a Cancel had landed), `tick(force=False)`, `message(severity, text)`, `server_message(msg)`, `begin_record(sql)`, `outcome(kind, **fields) -> JobOutcomeEvent`); `JobHooks(build, gate, read_only, begin_record, finish_record, announce, report)`; `JobReport(outcome, error, schema_changed)`; `history_status(outcome, error) -> (status, text)`; `NO_DESCRIBE`.
  - `sessions/jobs/registry.JobRegistry`: `begin(session_id, profile_id, request_id, kind, title)`, `report(session_id, event)`, `list() -> list[JobInfo]` (newest first), `get(session_id, request_id)`, `clear_finished()`; Task 9 adds `dumped(path)`.
  - `SessionWorker`: `case JobCommand()` → `_run_job`; `replay_job()`; `busy()`; `finished_at`. `_start_request(req, *, before_finished=…)` sends a not-started job's `job_outcome` before its `finished`. `_run_statement(…, listener=, log_chars=)`: the listener gets a job's live messages; a job's statements log 200 characters.
  - `WorkerHooks.jobs: JobHooks`; `SessionHooksFactory._job_hooks(profile, database)` (the gate is `SafetyPolicy.gate_confirmed(sql, dialect, read_only())`, with the profile re-read at most once a second through `_Recent`); `build_job(command, profile, database)`. It refuses every command here; Tasks 3 and 9 add theirs.
  - `SessionManager(…, jobs=JobRegistry())`: each session's hooks announce and report into the registry; the reaper skips a session that is `busy()` and counts idle time from `max(detached_at, finished_at)`; `attach` calls `replay_job()`.
  - `HistoryService.begin(rec) -> int` (status `running`), `finish(record_id, status, elapsed_ms, rows, error)`; at open, `running` → `interrupted`. `HistoryStatus` gains `running`, `interrupted`.
  - `SafetyPolicy.gate_confirmed(sql, dialect, read_only) -> GuardDecision`: writable → RUN unclassified; read-only → `check(…, confirmed=True)`, and only RUN passes (else BLOCK, `read_only`).
  - `api/data.py`: `GET /data/jobs` → `list[JobInfo]`; `POST /data/jobs/cancel {session_id, request_id}` → 204 (404 for an unknown job; a job that has ended is left alone); `POST /data/jobs/clear` → 204 (S12). `AppContainer.jobs`.

- [ ] **Step 1: Write the failing tests**

`test_job_framework.py` drives a real `SessionWorker` with a scripted job (`Scripted`/`ScriptedJob`) on `tests/fake_db.py`'s fake connection, and the routes and the reaper through an `AppContainer` on a SQLite file (no server). It checks: the event order and one outcome; not-started outcomes (queued and cancelled, session closed first, refused by `build`); an open transaction refused; a Cancel reaching the running statement and one from another thread; progress throttling; the message limit; `broke()` after something was sent; the replay to a later client; the reaper and `busy()`; the registry and the routes; `history_status` for every outcome; and `_Recent`.

Create `backend/tests/test_job_framework.py`:

```python
"""The job framework (spec ⑥ D-06-2, D-06-10, D-06-11) on a fake connection: the event order, exactly one
job_outcome however a job ends, cancels, progress and message limits, History, the replay on attach, the job list and
a busy session that is never reaped. The jobs here are scripted; Run SQL file and dump follow in their own tests."""

import sqlite3
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.adapters.base import StatementCancelled, StatementError
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.sqlite.adapter import SqliteAdapter
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import JobCommand, JobOutcomeEvent, JobProgressEvent, StoppedAt
from dbaguy_backend.domain.execution import ExecuteCommand, MessageSeverity
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService
from dbaguy_backend.sessions.hooks import _Recent
from dbaguy_backend.sessions.jobs.context import Job, JobContext, JobHooks, JobReport, history_status
from dbaguy_backend.sessions.jobs.registry import JobRegistry
from dbaguy_backend.sessions.manager import SessionCreateRequest, SessionManager
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.fake_db import FakeConnection, FakeLease
from tests.test_sessions import Recorder, request_events


class Scripted(JobCommand):
    """A job command of the tests: the job does what `steps` says (see ScriptedJob)."""

    job_kind = "run_file"
    type: str = "scripted"
    steps: list[str]


class ScriptedJob(Job):
    """Steps: `sql <text>` runs a statement and drains it (StatementCancelled → stopped, cancelled), `tick` and
    `tick!` progress, `msg <text>`, `msgs <n>`, `crash`, `fail <code>` (an error report), `record`."""

    kind = "run_file"

    def __init__(self, command: Scripted) -> None:
        self._steps = command.steps

    @property
    def title(self) -> str:
        return "Scripted job"

    def run(self, ctx: JobContext) -> JobReport:
        done = 0
        for step in self._steps:
            word, _, arg = step.partition(" ")
            if word == "sql":
                try:
                    ctx.drain(ctx.run(arg))
                except StatementCancelled as exc:
                    return JobReport(ctx.outcome("stopped", cancelled=True, statements=done,
                                                 stopped_at=StoppedAt(line=1, statement=done + 1)), exc)
                done += 1
                ctx.progress.statements = done
            elif word in ("tick", "tick!"):
                ctx.tick(force=word == "tick!")
            elif word == "msg":
                ctx.message(MessageSeverity.INFO, arg)
            elif word == "msgs":
                for i in range(int(arg)):
                    ctx.message(MessageSeverity.INFO, f"m{i}")
            elif word == "crash":
                raise RuntimeError("bug")
            elif word == "fail":
                return JobReport(ctx.outcome("stopped", statements=done), StatementError("it failed", code=arg))
            elif word == "record":
                ctx.begin_record("-- scripted")
        return JobReport(ctx.outcome("completed", statements=done), schema_changed=done > 0)


class Env:
    def __init__(self, conn: FakeConnection | None = None, clock=None) -> None:
        self.conn = conn or FakeConnection()
        self.records: list[tuple] = []
        self.listed: list = []
        self.schema_changes = 0
        lease = FakeLease(self.conn)
        self.worker = SessionWorker("jobs", lambda: lease)
        self.worker.start()
        jobs = JobHooks(
            build=self._build, begin_record=lambda sql, at: self.records.append(("begin", sql)) or 7,
            finish_record=lambda *args: self.records.append(("finish", *args)),
            announce=lambda *args: self.listed.append(("announce", *args)),
            report=lambda event: self.listed.append(("report", event.type)),
        )
        self.worker.configure(WorkerHooks(jobs=jobs, on_schema_change=self._changed))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def _changed(self) -> None:
        self.schema_changes += 1

    @staticmethod
    def _build(command: JobCommand) -> Job:
        if not isinstance(command, Scripted):
            raise StatementError("not a test job", code="not_supported")
        return ScriptedJob(command)

    def submit(self, *steps: str, rid: str = "j") -> None:
        self.worker.submit(Scripted(request_id=rid, steps=list(steps)))

    def run(self, *steps: str, rid: str = "j") -> list:
        self.submit(*steps, rid=rid)
        return request_events(self.recorder, rid)


@pytest.fixture
def env():
    made: list[Env] = []

    def make(**fields) -> Env:
        made.append(Env(**fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def types(events) -> list[str]:
    return [e.type for e in events]


def outcome(events) -> JobOutcomeEvent:
    [found] = [e for e in events if e.type == "job_outcome"]  # exactly one per job (⑤ E1)
    return found


def test_a_job_sends_its_events_in_order_and_one_outcome(env):  # D-06-2
    e = env()
    events = e.run("tick!", "sql rows 3", "msg hello", "tick!", "record")
    assert types(events) == ["started", "job_progress", "message", "job_progress", "job_outcome", "session_state",
                             "finished"]
    done = outcome(events)
    assert (done.job, done.outcome, done.statements, done.cancelled) == ("run_file", "completed", 1, False)
    assert events[3].statements == 1 and not events[-1].cancelled
    assert e.conn.runs == ["rows 3"]
    assert e.records == [("begin", "-- scripted"), ("finish", 7, "ok", e.records[1][3], 0, None)]
    assert e.schema_changes == 1
    assert e.listed[0] == ("announce", "j", "run_file", "Scripted job")
    assert [item for item in e.listed[1:]] == [("report", "job_progress"), ("report", "job_progress"),
                                               ("report", "job_outcome")]


def test_an_error_goes_out_right_before_the_outcome(env):
    events = env().run("sql x", "fail 42P01")
    assert types(events)[-4:] == ["error", "job_outcome", "session_state", "finished"]
    error = next(ev for ev in events if ev.type == "error")
    assert (error.code, error.message, outcome(events).outcome) == ("42P01", "it failed", "stopped")


def test_a_job_cancelled_while_queued_or_closed_before_it_started_says_not_started(env):
    e = env()
    e.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert e.conn.blocked.wait(5)
    e.submit("sql x", rid="queued")
    e.worker.cancel("queued")
    e.worker.cancel("busy")
    queued = request_events(e.recorder, "queued")
    assert types(queued) == ["started", "message", "job_outcome", "session_state", "finished"]
    assert (outcome(queued).outcome, outcome(queued).cancelled, queued[-1].cancelled) == ("not_started", True, True)
    assert "x" not in e.conn.runs

    closing = env()
    closing.worker.submit(ExecuteCommand(request_id="busy", sql="block"))
    assert closing.conn.blocked.wait(5)
    closing.submit("sql x", rid="late")
    closing.worker.close()
    late = request_events(closing.recorder, "late")
    assert (outcome(late).outcome, outcome(late).cancelled) == ("not_started", False)


def test_a_cancel_reaches_the_running_statement(env):
    e = env()
    e.submit("sql block", "sql after")
    assert e.conn.blocked.wait(5)
    e.worker.cancel("j")
    events = request_events(e.recorder, "j")
    done = outcome(events)
    assert (done.outcome, done.cancelled, done.stopped_at.statement) == ("stopped", True, 1)
    assert events[-1].cancelled and e.conn.runs == ["block"]


def test_an_unclassified_failure_is_unknown_once_something_was_sent(env):
    events = env().run("sql x", "crash")
    assert (outcome(events).outcome, next(ev for ev in events if ev.type == "error").message) == (
        "unknown", "Internal error: bug")
    events = env().run("crash")
    assert outcome(events).outcome == "not_started"


def test_a_job_never_runs_inside_an_open_transaction(env):
    e = env()
    e.conn.tx = True
    events = e.run("sql x")
    error = next(ev for ev in events if ev.type == "error")
    assert (error.code, outcome(events).outcome, e.conn.runs) == ("transaction_open", "not_started", [])


def test_a_command_the_session_cannot_run_is_refused():
    worker = SessionWorker("jobs", lambda: FakeLease())
    worker.start()
    try:
        recorder = Recorder()
        worker.set_emitter(recorder)
        worker.submit(Scripted(request_id="j", steps=["sql x"]))  # the default hooks build no job
        events = request_events(recorder, "j")
        error = next(ev for ev in events if ev.type == "error")
        assert (error.code, outcome(events).outcome) == ("not_supported", "not_started")
    finally:
        worker.close()


def test_progress_is_sent_at_most_every_250_ms_and_always_when_forced():
    now = [0.0]
    sent: list = []
    ctx = JobContext(
        request_id="j", kind="dump", connection=FakeConnection(), dialect=None, capabilities=None, hooks=JobHooks(),
        _run=None, _close=None, _raise_if_cancelled=None, _send=None, _publish=sent.append, _stop_cancels=None,
        _sent=lambda: False,
        _clock=lambda: now[0],
    )
    for step in range(10):  # 10 ticks over 0.45 s
        ctx.progress.rows = step
        ctx.tick()
        now[0] += 0.05
    assert [event.rows for event in sent] == [0, 5]
    ctx.tick(force=True)
    assert sent[-1].rows == 9 and isinstance(sent[-1], JobProgressEvent)


def test_server_messages_past_the_limit_are_counted_not_sent(env):  # D-06-11
    events = env().run("msgs 250")
    assert sum(ev.type == "message" for ev in events) == 200
    assert outcome(events).warnings == ["50 more server messages were not shown"]


def test_a_client_that_attaches_later_gets_the_last_progress_and_the_outcome(env):  # D-06-10
    e = env()
    e.run("tick!", "sql x", "tick!")
    later = Recorder()
    e.worker.set_emitter(later)
    e.worker.replay_job()
    assert types(later.events) == ["job_progress", "job_outcome"]
    assert later.events[0].statements == 1


@pytest.mark.parametrize(("event", "error", "expected"), [
    (dict(outcome="completed"), None, ("ok", None)),
    (dict(outcome="completed", error_count=3), None, ("ok", "3 error(s) skipped")),
    (dict(outcome="rolled_back", cancelled=True), StatementCancelled(), ("cancelled", "Query cancelled")),
    (dict(outcome="rolled_back"), StatementError("boom"), ("error", "Rolled back: boom")),
    (dict(outcome="stopped", stopped_at=StoppedAt(line=12, statement=3)), StatementError("boom"),
     ("error", "Stopped at line 12: boom")),
    (dict(outcome="partial", stopped_at=StoppedAt(line=1_200, statement=40)), None, ("error", "Partly applied at line 1,200")),
    (dict(outcome="unknown"), StatementError("lost"), ("error", "Outcome unknown: lost")),
])
def test_history_status_of_each_outcome(event, error, expected):  # D-06-9
    assert history_status(JobOutcomeEvent(request_id="j", job="run_file", **event), error) == expected


def test_the_read_only_flag_is_read_again_at_most_once_a_second():
    now, reads = [0.0], []
    flag = _Recent(lambda: reads.append(now[0]) or len(reads) > 1, clock=lambda: now[0])
    assert flag() is False and flag() is False
    now[0] = 0.9
    assert flag() is False
    now[0] = 1.0
    assert flag() is True and reads == [0.0, 1.0]


def test_the_job_list_keeps_running_jobs_and_forgets_finished_ones():
    registry = JobRegistry(limit=2, clock=lambda: datetime(2026, 10, 20, tzinfo=UTC))
    profile = uuid4()
    for rid in ("a", "b", "c"):
        registry.begin("s", profile, rid, "dump", f"Dump {rid}")
    assert [info.request_id for info in registry.list()] == ["c", "b", "a"]  # running jobs are never trimmed
    registry.report("s", JobProgressEvent(request_id="b", rows=5))
    registry.report("s", JobOutcomeEvent(request_id="a", job="dump", outcome="completed"))
    registry.report("other", JobOutcomeEvent(request_id="c", job="dump", outcome="completed"))  # another session's
    assert registry.get("s", "b").progress.rows == 5 and registry.get("s", "c").outcome is None
    registry.begin("s", profile, "d", "run_file", "Run d")
    assert [info.request_id for info in registry.list()] == ["d", "c", "b"]  # the oldest finished one went
    registry.report("s", JobOutcomeEvent(request_id="b", job="dump", outcome="failed"))
    assert registry.clear_finished() == 1 and [info.request_id for info in registry.list()] == ["d", "c"]


@pytest.fixture
def manager(tmp_path):
    db = tmp_path / "s.db"
    sqlite3.connect(db).close()
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    entry = profiles.create(ProfileUpsertRequest.model_validate({"profile": {"engine": "sqlite", "name": "s",
                                                                             "path": str(db)}}))
    connections = ConnectionService(AdapterRegistry([SqliteAdapter()]), profiles,
                                    SshTunnelManager(write_askpass_script(tmp_path)))
    made = SessionManager(connections, profiles, idle_timeout=0)
    yield made, entry.profile.id
    made.close_all()


def test_a_session_running_a_request_is_never_reaped(manager):  # D-06-10
    sessions, profile_id = manager
    info = sessions.create(SessionCreateRequest(profile_id=profile_id))
    recorder = Recorder()
    worker = sessions.attach(info.session_id, recorder)
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT count(*) FROM n"
    worker.submit(ExecuteCommand(request_id="slow", sql=slow))
    recorder.wait_for(lambda event: event.type == "started")
    sessions.detach(info.session_id, recorder)  # no WebSocket any more: the events still reach the test
    worker.set_emitter(recorder)
    assert worker.busy()
    assert sessions.reap_idle() == 0  # detached and past the idle time, but running
    worker.cancel("slow")
    request_events(recorder, "slow")
    assert sessions.reap_idle() == 1  # idle once its request ended (idle_timeout 0 here)


def test_the_job_routes(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
        assert client.get("/data/jobs", headers=auth).json() == []
        registry = container.sessions.jobs
        registry.begin("s1", uuid4(), "r1", "dump", "Dump public")
        registry.report("s1", JobOutcomeEvent(request_id="r1", job="dump", outcome="completed", path="/x.sql"))
        [listed] = client.get("/data/jobs", headers=auth).json()
        assert (listed["title"], listed["outcome"]["outcome"], listed["outcome"]["path"]) == (
            "Dump public", "completed", "/x.sql")
        missing = client.post("/data/jobs/cancel", json={"session_id": "s1", "request_id": "nope"}, headers=auth)
        assert missing.status_code == 404
        gone = client.post("/data/jobs/cancel", json={"session_id": "s1", "request_id": "r1"}, headers=auth)
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "session_not_found")
        assert client.post("/data/jobs/clear", headers=auth).status_code == 204
        assert client.get("/data/jobs", headers=auth).json() == []
        assert client.get("/data/jobs").status_code == 401


def test_a_cancel_from_another_thread_while_the_job_runs_is_safe(env):
    e = env()
    e.submit("sql block")
    assert e.conn.blocked.wait(5)
    threading.Thread(target=e.worker.cancel, args=("j",)).start()
    assert outcome(request_events(e.recorder, "j")).cancelled
```

Append to `backend/tests/test_history.py`:

```python


def test_a_job_record_is_written_running_and_finished_later(tmp_path):  # spec ⑥ D-06-9
    history = HistoryService(tmp_path / "h.db")
    record_id = history.begin(rec("-- Run SQL file /x.sql (1.0 MB), all or nothing"))
    [entry] = history.search().items
    assert (entry.id, entry.status) == (record_id, "running")
    history.finish(record_id, "error", 1234, 56, "Stopped at line 9: boom")
    [entry] = history.search().items
    assert (entry.status, entry.elapsed_ms, entry.rows, entry.error) == ("error", 1234, 56, "Stopped at line 9: boom")
    history.finish(record_id + 1, "ok", 1, 1, None)  # a record that is gone stays gone
    assert len(history.search().items) == 1
    history.close()


def test_records_still_running_at_the_next_start_are_interrupted(tmp_path):  # D-06-9
    first = HistoryService(tmp_path / "h.db")
    first.record(rec("SELECT 1"))
    first.begin(rec("-- Copy data"))
    first.close()  # the backend ended with the job running
    second = HistoryService(tmp_path / "h.db")
    assert [(e.sql, e.status) for e in second.search().items] == [("-- Copy data", "interrupted"), ("SELECT 1", "ok")]
    record_id = second.begin(rec("-- Run SQL file"))  # this run's own: never marked
    assert second.search().items[0].status == "running"
    second.finish(record_id, "ok", 1, 1, None)
    second.close()
```

Append to `backend/tests/test_safety.py`:

```python


@pytest.mark.parametrize("sql", ["DROP TABLE t", "DELETE FROM t", "TRUNCATE t", "frobnicate", "SELECT 1"])
def test_a_confirmed_file_runs_unclassified_on_a_writable_connection(sql):  # spec ⑥ R-06-6
    decision = policy.gate_confirmed(sql, PG, read_only=False)
    assert (decision.action, decision.classification.kind) == (A.RUN, StatementKind.UNKNOWN)


@pytest.mark.parametrize(("sql", "dialect", "action"), [
    ("SELECT 1", PG, A.RUN),
    ("SET search_path TO public", PG, A.RUN),
    ("INSERT INTO t VALUES (1)", PG, A.BLOCK),
    ("DROP TABLE t", PG, A.BLOCK),  # never CONFIRM: the file was confirmed as a whole
    ("SELECT set_config('default_transaction_read_only', 'off', false)", PG, A.BLOCK),
    ("USE dbaguy", Dialect.MYSQL, A.RUN),
    ("UPSERT INTO ks (KEY, VALUE) VALUES ('k', 1)", Dialect.SQLPP, A.BLOCK),
])
def test_a_confirmed_file_on_a_read_only_connection_runs_only_what_check_runs(sql, dialect, action):
    decision = policy.gate_confirmed(sql, dialect, read_only=True)
    assert decision.action == action
    if action == A.BLOCK:
        assert decision.reasons == [GuardReason.READ_ONLY]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_job_framework.py tests/test_history.py tests/test_safety.py`
Expected: collection error, `ModuleNotFoundError: No module named 'dbaguy_backend.domain.data_jobs'`.

- [ ] **Step 3: The job models and the protocol**

Create `backend/dbaguy_backend/domain/data_jobs.py`:

```python
"""Data tools (spec ⑥): a job — a dump, a run of a SQL file — runs on a session of its own and ends with exactly one
outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel

JobKind = Literal["dump", "run_file"]
# R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
# or a DDL ended the transaction), unknown (the link died during COMMIT). Run, statement by statement: completed,
# stopped, unknown (the link died while a statement ran). Any job: not_started (cancelled while queued, its session
# closed first, or refused before anything was sent). A cancel is the `cancelled` flag on one of these, never an outcome.
JobOutcomeKind = Literal["completed", "failed", "rolled_back", "partial", "stopped", "unknown", "not_started"]
JobStage = Literal["starting", "schema", "data", "post_data", "statements", "commit"]

MAX_JOB_ERRORS = 100  # D-06-11: errors kept with their text ("skip and continue"); the rest are only counted
MAX_JOB_MESSAGES = 200  # D-06-11: server messages (NOTICE, PRINT) forwarded per job; the rest are only counted
PROGRESS_INTERVAL_S = 0.25  # D-06-11: at most one job_progress this often (and at each object's start and end)
MAX_JOBS_LISTED = 200  # the job list keeps this many; the oldest finished ones go first


class JobCommand(BaseModel):
    """A WebSocket command that starts a job (D-06-2): the worker runs every subclass through one path."""

    job_kind: ClassVar[JobKind]
    request_id: str


class JobError(BaseModel):
    """One statement that failed: where, the server's code and its own text (shown, never logged above DEBUG)."""

    line: int | None = None
    statement: int | None = None  # 1-based among the file's statements
    code: str | None = None
    message: str


class StoppedAt(BaseModel):
    """Where a run stopped: the statement that failed, was cancelled or was not sent."""

    line: int
    statement: int


class JobObject(BaseModel):
    """One object of a dump, with the rows written for it."""

    kind: str  # table, view, sequence, function, …
    name: str  # as the file names it: schema.name
    rows: int = 0


class JobProgressEvent(BaseModel):
    """How far a job is (D-06-11): at most every 250 ms, and as each object starts and ends. Cumulative counts."""

    type: Literal["job_progress"] = "job_progress"
    request_id: str
    stage: JobStage = "starting"
    object: str | None = None  # the object being written or read (dump), schema.name
    rows: int = 0
    bytes: int = 0  # written (dump) or read (run) so far
    total_bytes: int | None = None  # the file's size (run)
    statements: int = 0  # sent (run)
    errors: int = 0  # skipped (run, statement by statement, continue)
    line: int | None = None  # where the run is


class JobOutcomeEvent(BaseModel):
    """How a job ended (R-06-12): sent once, right before the request's `session_state` and `finished`."""

    type: Literal["job_outcome"] = "job_outcome"
    request_id: str
    job: JobKind
    outcome: JobOutcomeKind
    cancelled: bool = False
    rows: int = 0
    bytes: int = 0
    statements: int = 0  # sent and done (run)
    path: str | None = None  # the file written (dump) or read (run)
    objects: list[JobObject] = []
    errors: list[JobError] = []  # the first MAX_JOB_ERRORS
    error_count: int = 0
    stopped_at: StoppedAt | None = None
    warnings: list[str] = []


class JobInfo(BaseModel):
    """A job of this backend run, as the Jobs list shows it (GET /data/jobs)."""

    session_id: str
    request_id: str
    job: JobKind
    title: str
    profile_id: UUID
    started_at: datetime
    progress: JobProgressEvent | None = None  # the last one sent
    outcome: JobOutcomeEvent | None = None  # set once the job ended
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

with:

```python

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python

ServerEvent = Annotated[
    StartedEvent | ResultSetEvent | RowsEvent | ResultSetEndEvent | AffectedEvent | MessageEvent | ErrorEvent
    | ProgressEvent | ConfirmationRequiredEvent | SessionStateEvent | FinishedEvent | EditOutcomeEvent,
    Field(discriminator="type"),
]

```

with:

```python

ServerEvent = Annotated[
    StartedEvent | ResultSetEvent | RowsEvent | ResultSetEndEvent | AffectedEvent | MessageEvent | ErrorEvent
    | ProgressEvent | ConfirmationRequiredEvent | SessionStateEvent | FinishedEvent | EditOutcomeEvent
    | JobProgressEvent | JobOutcomeEvent,
    Field(discriminator="type"),
]

```

In `backend/dbaguy_backend/domain/history.py`, replace:

```python

from .execution import QueryContext

HistoryStatus = Literal["ok", "error", "cancelled", "timeout"]


class HistoryRecord(BaseModel):
```

with:

```python

from .execution import QueryContext

# running: a Run SQL file or a copy that started and has not ended (D-06-9); interrupted: one that was still running
# when the backend ended (set at the next start)
HistoryStatus = Literal["ok", "error", "cancelled", "timeout", "running", "interrupted"]


class HistoryRecord(BaseModel):
```

- [ ] **Step 4: History's running records and the confirmed gate**

In `backend/dbaguy_backend/services/history_service.py`, replace:

```python
from loguru import logger

from ..domain.errors import StorageError
from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord
from ..storage.json_files import move_aside

_SCHEMA = """
```

with:

```python
from loguru import logger

from ..domain.errors import StorageError
from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord, HistoryStatus
from ..storage.json_files import move_aside

_SCHEMA = """
```

In `backend/dbaguy_backend/services/history_service.py`, replace:

```python
            self._conn = self._open()
            logger.debug("History database {} is open", self._path)
            self._prune(self._conn)  # a short-lived run may never reach `prune_every` inserts
        return self._conn

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
```

with:

```python
            self._conn = self._open()
            logger.debug("History database {} is open", self._path)
            self._prune(self._conn)  # a short-lived run may never reach `prune_every` inserts
            self._interrupt(self._conn)
        return self._conn

    @staticmethod
    def _interrupt(conn: sqlite3.Connection) -> None:
        """Records still `running` belong to a backend that ended mid-job (D-06-9): one process uses the history, and
        it opens the database before it writes anything, so none of them can be this run's."""
        marked = conn.execute("UPDATE history SET status = 'interrupted' WHERE status = 'running'").rowcount
        if marked > 0:
            logger.warning("{} history record(s) of an earlier run were still running: marked interrupted", marked)

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
```

In `backend/dbaguy_backend/services/history_service.py`, replace:

```python
        return conn

    def record(self, rec: HistoryRecord) -> None:
        error = rec.error[:_MAX_ERROR] if rec.error else None
        with self._lock:
            conn = self._connection()
            conn.execute(
                f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
                 rec.elapsed_ms, rec.rows, rec.status, error),
```

with:

```python
        return conn

    def record(self, rec: HistoryRecord) -> None:
        self._insert(rec)

    def begin(self, rec: HistoryRecord) -> int:
        """A job's record, written as it starts (status `running`, D-06-9): its id, for `finish`."""
        return self._insert(rec.model_copy(update={"status": "running"}))

    def finish(self, record_id: int, status: HistoryStatus, elapsed_ms: int, rows: int, error: str | None) -> None:
        """How the job of `begin` ended. A record that is gone (cleared or pruned meanwhile) is left gone."""
        with self._lock:
            self._connection().execute(
                "UPDATE history SET status = ?, elapsed_ms = ?, rows = ?, error = ? WHERE id = ?",
                (status, elapsed_ms, rows, error[:_MAX_ERROR] if error else None, record_id),
            )

    def _insert(self, rec: HistoryRecord) -> int:
        error = rec.error[:_MAX_ERROR] if rec.error else None
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
                 rec.elapsed_ms, rec.rows, rec.status, error),
```

In `backend/dbaguy_backend/services/history_service.py`, replace:

```python
            self._inserts += 1
            if self._inserts % self._prune_every == 0:
                self._prune(conn)

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop the entries beyond the retention. The caller holds the lock."""
```

with:

```python
            self._inserts += 1
            if self._inserts % self._prune_every == 0:
                self._prune(conn)
            return int(cursor.lastrowid or 0)

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop the entries beyond the retention. The caller holds the lock."""
```

In `backend/dbaguy_backend/services/safety.py`, replace:

```python
        self, sql: str, dialect: Dialect, level: SafetyLevel, read_only: bool, confirmed: bool = False
    ) -> GuardDecision:
        return self.decide(sql, classify(sql, dialect), dialect, level, read_only, confirmed)

    def decide(
        self, sql: str, classification: Classification, dialect: Dialect, level: SafetyLevel, read_only: bool,
```

with:

```python
        self, sql: str, dialect: Dialect, level: SafetyLevel, read_only: bool, confirmed: bool = False
    ) -> GuardDecision:
        return self.decide(sql, classify(sql, dialect), dialect, level, read_only, confirmed)

    def gate_confirmed(self, sql: str, dialect: Dialect, read_only: bool) -> GuardDecision:
        """A statement of a whole file the user confirmed (Run SQL file, R-06-6): on a writable connection it runs,
        unclassified — confirmed, `decide()` runs every kind, so classifying would only cost time (3–10 times the
        split, A X2). On a read-only one, `check()` decides, and only RUN passes (it cannot ask: BLOCK otherwise)."""
        if not read_only:
            return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))
        decision = self.check(sql, dialect, "relaxed", True, confirmed=True)
        if decision.action == GuardAction.RUN:
            return decision
        return GuardDecision(action=GuardAction.BLOCK, reasons=[GuardReason.READ_ONLY],
                             classification=decision.classification)

    def decide(
        self, sql: str, classification: Classification, dialect: Dialect, level: SafetyLevel, read_only: bool,
```

- [ ] **Step 5: The job context and the registry**

Create `backend/dbaguy_backend/sessions/jobs/__init__.py`:

```python
"""Data tools' jobs (spec ⑥): the framework (`context`), the job list (`registry`) and one module per job."""
```

Create `backend/dbaguy_backend/sessions/jobs/context.py`:

```python
"""The job framework (spec ⑥ D-06-2): a job is an object with one `run(ctx)`; the worker gives it a JobContext — the
worker's own statement path (arm/disarm, cancel checks, `describe=False`), progress, messages, the History record —
and sends whatever the job reports as exactly one `job_outcome`. Jobs import this module, never the worker."""

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from loguru import logger
from pydantic import BaseModel

from ...adapters.base import AdapterCapabilities, DatabaseConnection, RunOptions, StatementError, StatementResult
from ...domain.classification import Classification, StatementKind
from ...domain.data_jobs import (
    MAX_JOB_MESSAGES, PROGRESS_INTERVAL_S, JobCommand, JobKind, JobOutcomeEvent, JobOutcomeKind, JobProgressEvent,
    JobStage,
)
from ...domain.execution import MessageEvent, MessageSeverity, ServerMessage
from ...domain.history import HistoryStatus
from ...services.safety import GuardAction, GuardDecision
from ...sql.dialects import Dialect

NO_DESCRIBE = RunOptions(describe=False)  # a job shows no result set: SQL Server skips the describe (⑤ M1)
_DRAIN_CHUNK = 1000  # rows read and dropped at a time


def _no_job(command: JobCommand) -> "Job":
    raise StatementError("This connection cannot run this job", code="not_supported")


def _run_everything(sql: str) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


def _writable() -> bool:
    return False


def _no_record(sql: str, started_at: datetime) -> int | None:
    return None


def _ignore(*_: object) -> None:
    return None


@dataclass(frozen=True)
class JobHooks:
    """How a job reaches the rest of the app (a session's WorkerHooks.jobs). The defaults run nothing and record
    nothing."""

    build: Callable[[JobCommand], "Job"] = _no_job  # raises StatementError for a command it cannot run
    gate: Callable[[str], GuardDecision] = _run_everything  # SafetyPolicy.gate_confirmed for this session (R-06-6)
    read_only: Callable[[], bool] = _writable  # the profile is read-only now
    begin_record: Callable[[str, datetime], int | None] = _no_record  # History (D-06-9): the record's id
    finish_record: Callable[[int, HistoryStatus, int, int, str | None], None] = _ignore  # id, status, ms, rows, error
    announce: Callable[[str, JobKind, str], None] = _ignore  # the job list (D-06-10): request id, kind, title
    report: Callable[[BaseModel], None] = _ignore  # the job list: each job_progress and the job_outcome


@dataclass
class JobProgress:
    """What the next job_progress says: the job updates it, `JobContext.tick` sends it."""

    stage: JobStage = "starting"
    object: str | None = None
    rows: int = 0
    bytes: int = 0
    total_bytes: int | None = None
    statements: int = 0
    errors: int = 0
    line: int | None = None


@dataclass(frozen=True)
class JobReport:
    """How a job ended: its outcome, the error sent right before it (if any), and whether the profile's schema cache
    must be refreshed (DDL may have run)."""

    outcome: JobOutcomeEvent
    error: StatementError | None = None
    schema_changed: bool = False


Runner = Callable[[str, RunOptions, Callable[[ServerMessage], None]], StatementResult]


@dataclass
class JobContext:
    """A job's view of its session (any method: the worker thread only)."""

    request_id: str
    kind: JobKind
    connection: DatabaseConnection  # catalog and dump methods are called on it directly, on this thread
    dialect: Dialect
    capabilities: AdapterCapabilities
    hooks: JobHooks
    _run: Runner
    _close: Callable[[StatementResult], None]
    _raise_if_cancelled: Callable[[], None]
    _send: Callable[[BaseModel], None]  # an event of the request (a message)
    _publish: Callable[[BaseModel], None]  # a job event: sent, kept for a replay, and given to the job list
    _stop_cancels: Callable[[], bool]
    _sent: Callable[[], bool]
    _clock: Callable[[], float] = time.monotonic
    progress: JobProgress = field(default_factory=JobProgress)
    record_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _last_tick: float = -math.inf
    _messages: int = 0
    messages_dropped: int = 0

    @property
    def sent(self) -> bool:
        """Whether a statement of this job reached the connection (`run()` was called on it)."""
        return self._sent()

    def run(self, sql: str, options: RunOptions = NO_DESCRIBE) -> StatementResult:
        """One statement through the worker's path: refused text, the last cancel check and the armed window, then
        `run()`. Its live messages (SQL Server PRINT) go to `message`."""
        return self._run(sql, options, self.server_message)

    def drain(self, result: StatementResult) -> int | None:
        """Read `result` to its end without keeping a row, forward its messages and close it: the rows its sets
        affected (None when none said), for a statement whose answer is not shown (Run SQL file)."""
        affected: int | None = None
        try:
            while True:
                if result.columns() is None:
                    if (rows := result.affected_rows()) is not None:
                        affected = (affected or 0) + rows
                else:
                    while len(result.fetch(_DRAIN_CHUNK)) == _DRAIN_CHUNK:
                        self.raise_if_cancelled()
                self._forward(result)
                if not result.next_result():
                    return affected
        finally:
            self._close(result)

    def close(self, result: StatementResult) -> None:
        self._close(result)

    def raise_if_cancelled(self) -> None:
        """A Cancel that came while no driver call was there to stop: StatementCancelled."""
        self._raise_if_cancelled()

    def stop_cancels(self) -> bool:
        """From now on a Cancel never reaches the driver (before a COMMIT, ⑤ R-05-6). Whether one had been asked."""
        return self._stop_cancels()

    def tick(self, *, force: bool = False) -> None:
        """Send `progress` when PROGRESS_INTERVAL_S has passed since the last one, or now with `force` (an object
        starts or ends)."""
        now = self._clock()
        if force or now - self._last_tick >= PROGRESS_INTERVAL_S:
            self._last_tick = now
            self._publish(JobProgressEvent(request_id=self.request_id, **asdict(self.progress)))

    def message(self, severity: MessageSeverity, text: str) -> None:
        """A message in the job's Messages: the first MAX_JOB_MESSAGES are sent, the rest counted (D-06-11)."""
        if self._messages >= MAX_JOB_MESSAGES:
            self.messages_dropped += 1
            return
        self._messages += 1
        self._send(MessageEvent(request_id=self.request_id, severity=severity, text=text))

    def server_message(self, message: ServerMessage) -> None:
        self.message(message.severity, message.text)

    def begin_record(self, sql: str) -> None:
        """Write the job's History record as running (D-06-9); a History that fails never stops the job."""
        try:
            self.record_id = self.hooks.begin_record(sql, self.started_at)
        except Exception:
            logger.warning("The History record of job {} could not be written", self.request_id, exc_info=True)

    def outcome(self, outcome: JobOutcomeKind, **fields: Any) -> JobOutcomeEvent:
        """The job's outcome event, with a note on the server messages that were not shown."""
        warnings = list(fields.pop("warnings", []))
        if self.messages_dropped:
            warnings.append(f"{self.messages_dropped:,} more server messages were not shown")
        return JobOutcomeEvent(request_id=self.request_id, job=self.kind, outcome=outcome, warnings=warnings, **fields)

    def _forward(self, result: StatementResult) -> None:
        for message in result.take_messages():
            self.server_message(message)


class Job(ABC):
    """One job of one command. `run` classifies every way it ends (R-06-12) and never resends anything."""

    kind: ClassVar[JobKind]

    @property
    @abstractmethod
    def title(self) -> str:
        """What the job list calls it: `Run orders.sql on Local PG`."""

    @abstractmethod
    def run(self, ctx: JobContext) -> JobReport: ...

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """An exception `run` did not classify (a bug, or an error it let through): nothing is known to have been
        applied when nothing was sent; otherwise the outcome is unknown."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        return JobReport(ctx.outcome("unknown" if ctx.sent else "not_started"), error, schema_changed=ctx.sent)


def history_status(outcome: JobOutcomeEvent, error: StatementError | None) -> tuple[HistoryStatus, str | None]:
    """How a job's History record ends (D-06-9): ok, cancelled, or error with what happened in front of the error."""
    text = error.message if error is not None else None
    if outcome.cancelled:
        return "cancelled", text
    if outcome.outcome == "completed":
        skipped = f"{outcome.error_count:,} error(s) skipped" if outcome.error_count else None
        return "ok", skipped
    where = f" at line {outcome.stopped_at.line:,}" if outcome.stopped_at is not None else ""
    prefix = {
        "rolled_back": "Rolled back", "stopped": f"Stopped{where}", "partial": f"Partly applied{where}",
        "unknown": f"Outcome unknown{where}", "failed": "Failed", "not_started": "Not started",
    }[outcome.outcome]
    return "error", f"{prefix}: {text}" if text else prefix
```

Create `backend/dbaguy_backend/sessions/jobs/registry.py`:

```python
"""The jobs of this backend run (spec ⑥ D-06-10): which session runs each, its kind and title, its last progress and
its outcome — what GET /data/jobs lists, so a reloaded window finds its jobs again. Kept until cleared or the backend
ends; at most MAX_JOBS_LISTED, the oldest finished first to go. Any thread."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel

from ...domain.data_jobs import MAX_JOBS_LISTED, JobInfo, JobKind, JobOutcomeEvent, JobProgressEvent


class JobRegistry:
    def __init__(self, limit: int = MAX_JOBS_LISTED, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._limit = limit
        self._clock = clock
        self._jobs: dict[tuple[str, str], JobInfo] = {}  # in the order they started
        self._lock = threading.Lock()

    def begin(self, session_id: str, profile_id: UUID, request_id: str, kind: JobKind, title: str) -> None:
        info = JobInfo(session_id=session_id, request_id=request_id, job=kind, title=title, profile_id=profile_id,
                       started_at=self._clock())
        with self._lock:
            self._jobs[(session_id, request_id)] = info
            self._trim()

    def report(self, session_id: str, event: BaseModel) -> None:
        """A job_progress or the job_outcome of a job `begin` announced; any other event is ignored."""
        if not isinstance(event, JobProgressEvent | JobOutcomeEvent):
            return
        with self._lock:
            info = self._jobs.get((session_id, event.request_id))
            if info is None:
                return
            if isinstance(event, JobProgressEvent):
                info.progress = event
            else:
                info.outcome = event

    def list(self) -> list[JobInfo]:
        """Newest first."""
        with self._lock:
            return [info.model_copy() for info in reversed(self._jobs.values())]

    def get(self, session_id: str, request_id: str) -> JobInfo | None:
        with self._lock:
            info = self._jobs.get((session_id, request_id))
            return info.model_copy() if info is not None else None

    def clear_finished(self) -> int:
        """Forget every job that has ended; the number forgotten."""
        with self._lock:
            done = [key for key, info in self._jobs.items() if info.outcome is not None]
            for key in done:
                del self._jobs[key]
        return len(done)

    def _trim(self) -> None:
        """The caller holds the lock. Running jobs are never dropped."""
        finished = [key for key, info in self._jobs.items() if info.outcome is not None]
        for key in finished[:max(0, len(self._jobs) - self._limit)]:
            del self._jobs[key]
```

- [ ] **Step 6: The worker's job path**

`_run_job` does everything common: it ends a paused result, announces `started`, builds the job, refuses an open transaction, runs it, classifies an exception it did not, and sends the error, the `job_outcome`, the History record, the schema cache, `session_state` and `finished`, in that order. A job never imports the worker.

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
)
from ..adapters.values import PLAN_TEXT_LIMIT, TEXT_LIMIT, encode_row
from ..domain.classification import Classification, StatementKind
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.errors import ConnectionFailed, SessionBusy
from ..domain.execution import (
```

with:

```python
)
from ..adapters.values import PLAN_TEXT_LIMIT, TEXT_LIMIT, encode_row
from ..domain.classification import Classification, StatementKind
from ..domain.data_jobs import JobCommand, JobOutcomeEvent
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.errors import ConnectionFailed, SessionBusy
from ..domain.execution import (
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
    unsupported_reason,
)
from ..storage.atomic_file import atomic_text_file
from .edit_apply import (
    COMMIT_WARNING,
    CONNECTION_LOST,
```

with:

```python
    unsupported_reason,
)
from ..storage.atomic_file import atomic_text_file
from .jobs.context import Job, JobContext, JobHooks, JobReport, history_status
from .edit_apply import (
    COMMIT_WARNING,
    CONNECTION_LOST,
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
from .session_state import SessionStateTracker

T = TypeVar("T")
RequestCommand = TypeVar("RequestCommand", ExecuteCommand, ExportCommand, ApplyEditsCommand)
Emitter = Callable[[BaseModel], None]
TransactionCommand = CommitCommand | RollbackCommand | SetAutocommitCommand
_CHUNK = 200
```

with:

```python
from .session_state import SessionStateTracker

T = TypeVar("T")
RequestCommand = TypeVar("RequestCommand", ExecuteCommand, ExportCommand, ApplyEditsCommand, JobCommand)
Emitter = Callable[[BaseModel], None]
TransactionCommand = CommitCommand | RollbackCommand | SetAutocommitCommand
_CHUNK = 200
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
# A grid commit's texts (BEGIN, its DML, COMMIT) return no result set to show: nothing to describe (05a review M1). A
# trigger's SELECT still comes back as a result set, which has no count (row_count_unknown, ruling E9).
_EDIT_OPTIONS = RunOptions(describe=False)


def paused_result_closed_text(limit_s: float) -> str:
```

with:

```python
# A grid commit's texts (BEGIN, its DML, COMMIT) return no result set to show: nothing to describe (05a review M1). A
# trigger's SELECT still comes back as a result set, which has no count (row_count_unknown, ruling E9).
_EDIT_OPTIONS = RunOptions(describe=False)
_JOB_LOG_CHARS = 200  # a job's statement at DEBUG: its first characters only (D-06-12: a million-statement run)


def paused_result_closed_text(limit_s: float) -> str:
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
    capabilities: AdapterCapabilities = AdapterCapabilities()  # which run() options the engine takes; idle limits
    # The previewed grid-edit plan with this id, taken once (spec ⑤ D-05-7); raises PlanUnavailable
    take_edit_plan: Callable[[str], EditPlan] = _no_edit_plans


@dataclass
```

with:

```python
    capabilities: AdapterCapabilities = AdapterCapabilities()  # which run() options the engine takes; idle limits
    # The previewed grid-edit plan with this id, taken once (spec ⑤ D-05-7); raises PlanUnavailable
    take_edit_plan: Callable[[str], EditPlan] = _no_edit_plans
    jobs: JobHooks = JobHooks()  # data tools' jobs (spec ⑥ D-06-2)


@dataclass
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
        self._tracker = SessionStateTracker()
        self._ddl_pending = False  # worker thread only: DDL ran inside a transaction that has not ended yet
        self.last_activity = time.monotonic()

    # ---- lifecycle (any thread) ----

```

with:

```python
        self._tracker = SessionStateTracker()
        self._ddl_pending = False  # worker thread only: DDL ran inside a transaction that has not ended yet
        self.last_activity = time.monotonic()
        self.finished_at = 0.0  # when the last request sent its `finished` (the reaper counts idle time from it)
        # The last job_progress and job_outcome of this session's job, by type: sent again to a client that attaches
        # (D-06-10). Written by the worker thread, read by any (under _emit_lock)
        self._job_events: dict[str, BaseModel] = {}

    # ---- lifecycle (any thread) ----

```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
            if state is not None:
                self._send(state)

    def call(self, fn: Callable[[DatabaseConnection], T], timeout: float | None = 120.0) -> T:
        """Run fn(connection) on the worker thread and wait for its result."""
        future: Future[T] = Future()
```

with:

```python
            if state is not None:
                self._send(state)

    def replay_job(self) -> None:
        """Send the last job_progress and job_outcome of this session's job, if any, to the current emitter."""
        with self._emit_lock:
            events = list(self._job_events.values())
        for event in events:
            self._send(event)

    def busy(self) -> bool:
        """A request runs or waits in the queue: the session must not be reaped (D-06-10)."""
        with self._state_lock:
            return self._running_request is not None or bool(self._queued)

    def call(self, fn: Callable[[DatabaseConnection], T], timeout: float | None = 120.0) -> T:
        """Run fn(connection) on the worker thread and wait for its result."""
        future: Future[T] = Future()
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
                self._submit_request(command, self._export)
            case ApplyEditsCommand():
                self._submit_request(command, self._apply_edits)
            case FetchMoreCommand():
                self._put(lambda: self._fetch_more(command))
            case DiscardCommand():
```

with:

```python
                self._submit_request(command, self._export)
            case ApplyEditsCommand():
                self._submit_request(command, self._apply_edits)
            case JobCommand():
                self._submit_request(command, self._run_job)
            case FetchMoreCommand():
                self._put(lambda: self._fetch_more(command))
            case DiscardCommand():
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
            self._tracker.activity(busy=True)
        return None

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

with:

```python
            self._tracker.activity(busy=True)
        return None

    def _start_request(
        self, req: _Request, *, before_finished: Callable[[str], BaseModel] | None = None,
    ) -> bool:
        """Leave the queue and announce `started`. When the request must not run, end it here — with the event
        `before_finished(why)` gives right before its `finished`, when given — and return False."""
        skipped = self._begin_request(req.request_id)
        self._send(StartedEvent(request_id=req.request_id))
        if skipped is None:
            return True
        self._send(MessageEvent(request_id=req.request_id, severity=MessageSeverity.WARNING, text=skipped))
        if before_finished is not None:
            self._send(before_finished(skipped))
        self._send_finished(req.request_id, req.elapsed_ms(), cancelled=True)
        return False

```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
        finally:
            self._set_running(None)

    def _run_statement(self, req: _Request, what: str, sql: str, options: RunOptions) -> StatementResult:
        """The request's last cancel check, then run(). A cancel that lands between the two finds the driver idle (an
        adapter must ignore an idle cancel, R-03a-P4), so the statement would run to its end: the connection is armed
        before the check and keeps such a cancel, and run() then does not start (Task 6b). Disarmed on every way out,
        once run() has returned or raised; the result's own cancel handling takes over from there."""
        connection = self.connection
        try:
            connection.arm_cancel()
            self._raise_if_cancelled()  # cancelled during the checks: the driver had nothing to interrupt
            self._log_statement(what, req.request_id, sql, options)
            req.ran = True
            self._listen(req.request_id)
            return connection.run(sql, options)
        finally:
            try:
```

with:

```python
        finally:
            self._set_running(None)

    def _run_statement(
        self, req: _Request, what: str, sql: str, options: RunOptions, *,
        listener: Callable[[ServerMessage], None] | None = None, log_chars: int = 2000,
    ) -> StatementResult:
        """The request's last cancel check, then run(). A cancel that lands between the two finds the driver idle (an
        adapter must ignore an idle cancel, R-03a-P4), so the statement would run to its end: the connection is armed
        before the check and keeps such a cancel, and run() then does not start (Task 6b). Disarmed on every way out,
        once run() has returned or raised; the result's own cancel handling takes over from there. `listener`: where
        live messages go instead of the request's Messages (a job counts them, D-06-11)."""
        connection = self.connection
        try:
            connection.arm_cancel()
            self._raise_if_cancelled()  # cancelled during the checks: the driver had nothing to interrupt
            self._log_statement(what, req.request_id, sql, options, log_chars)
            req.ran = True
            self._listen(req.request_id, listener)
            return connection.run(sql, options)
        finally:
            try:
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
            except Exception:  # never lose the result (or the error) over it
                logger.warning("Could not disarm the cancel on {}", self.name, exc_info=True)

    def _log_statement(self, what: str, request_id: str, sql: str, options: RunOptions) -> None:
        """The SQL text and the parameter names, at DEBUG; never parameter values (spec §5)."""
        logger.debug("[{}] {} {}: {}", self.name, what, request_id, sql[:2000])
        if options.params is not None:
            logger.debug("[{}] {} parameters: {}", self.name, request_id, _param_names(options.params))

```

with:

```python
            except Exception:  # never lose the result (or the error) over it
                logger.warning("Could not disarm the cancel on {}", self.name, exc_info=True)

    def _log_statement(self, what: str, request_id: str, sql: str, options: RunOptions, chars: int = 2000) -> None:
        """The SQL text and the parameter names, at DEBUG; never parameter values (spec §5)."""
        logger.debug("[{}] {} {}: {}", self.name, what, request_id, sql[:chars])
        if options.params is not None:
            logger.debug("[{}] {} parameters: {}", self.name, request_id, _param_names(options.params))

```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
        if self._open_exec is not None:
            self._finish(self._open_exec)

    def _listen(self, request_id: str | None) -> None:
        """Route messages the connection delivers as they arrive to `request_id`'s Messages (None: stop). Connections
        without live messages ignore it and keep using take_messages()."""
        listener = None if request_id is None else partial(self._send_live_message, request_id)
        try:
            self.connection.set_message_listener(listener)
        except Exception:  # a session whose connection is gone still ends its request
```

with:

```python
        if self._open_exec is not None:
            self._finish(self._open_exec)

    def _listen(self, request_id: str | None, listener: Callable[[ServerMessage], None] | None = None) -> None:
        """Route messages the connection delivers as they arrive to `request_id`'s Messages, or to `listener` when
        given (None: stop). Connections without live messages ignore it and keep using take_messages()."""
        if request_id is None:
            listener = None
        elif listener is None:
            listener = partial(self._send_live_message, request_id)
        try:
            self.connection.set_message_listener(listener)
        except Exception:  # a session whose connection is gone still ends its request
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
        self._discard_open()  # the grid's paused result ends first (R-05-4); its own `finished` goes out as usual
        req = _Request(request_id=cmd.request_id, sql="", kind=StatementKind.WRITE)
        skipped = EditOutcomeEvent(request_id=req.request_id, outcome="rolled_back")
        if not self._start_request(req, before_finished=skipped):
            return
        plan: EditPlan | None = None
        progress: EditProgress | None = None
```

with:

```python
        self._discard_open()  # the grid's paused result ends first (R-05-4); its own `finished` goes out as usual
        req = _Request(request_id=cmd.request_id, sql="", kind=StatementKind.WRITE)
        skipped = EditOutcomeEvent(request_id=req.request_id, outcome="rolled_back")
        if not self._start_request(req, before_finished=lambda _: skipped):
            return
        plan: EditPlan | None = None
        progress: EditProgress | None = None
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
        cancelled = report.outcome == "rolled_back" and isinstance(error, StatementCancelled)
        self._complete(req, error=history_error(report, len(statements)), cancelled=cancelled)

    def _transaction_command(self, cmd: TransactionCommand) -> None:
        self._discard_open()
        started = time.monotonic()
```

with:

```python
        cancelled = report.outcome == "rolled_back" and isinstance(error, StatementCancelled)
        self._complete(req, error=history_error(report, len(statements)), cancelled=cancelled)

    def _run_job(self, cmd: JobCommand) -> None:
        """Every data-tools job (spec ⑥ D-06-2): the job object does its own work through a JobContext, this does
        the rest. Events: started → job_progress × n → [message] → [error] → job_outcome → session_state → finished;
        a job cancelled while queued, or whose session closed first, still gets its job_outcome (not_started)."""
        self._discard_open()  # one connection: a paused result ends first
        req = _Request(request_id=cmd.request_id, sql="", history=False)
        kind = cmd.job_kind
        with self._emit_lock:
            self._job_events = {}

        def not_started(why: str) -> BaseModel:  # _start_request sends it
            return self._keep_job_event(JobOutcomeEvent(
                request_id=req.request_id, job=kind, outcome="not_started", cancelled=why != CLOSED_BEFORE_START,
            ))

        if not self._start_request(req, before_finished=not_started):
            return
        job: Job | None = None
        ctx: JobContext | None = None
        try:
            job = self._hooks.jobs.build(cmd)
            ctx = JobContext(
                request_id=req.request_id, kind=kind, connection=self.connection, dialect=self._hooks.dialect,
                capabilities=self._hooks.capabilities, hooks=self._hooks.jobs, _run=partial(self._job_statement, req),
                _close=self._close_result, _raise_if_cancelled=self._raise_if_cancelled, _send=self._send,
                _publish=self._job_event,
                _stop_cancels=lambda: self._set_running(None), _sent=lambda: req.ran,
            )
            self._notify("job list", self._hooks.jobs.announce, req.request_id, kind, job.title)
            if self.connection.in_transaction():  # a job runs on a session of its own: never inside a user's
                raise transaction_open_error()
            report = job.run(ctx)
        except Exception as exc:
            if job is None or ctx is None or not req.ran:
                if not isinstance(exc, StatementError):
                    logger.exception("Unexpected error starting a job on {}", self.name)
                error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
                report = JobReport(JobOutcomeEvent(request_id=req.request_id, job=kind, outcome="not_started"), error)
            else:
                logger.exception("Unexpected error in job {} on {}", req.request_id, self.name)
                report = job.broke(ctx, exc)
        finally:
            self._set_running(None)
        self._end_job(req, ctx, report)

    def _job_statement(
        self, req: _Request, sql: str, options: RunOptions, listener: Callable[[ServerMessage], None],
    ) -> StatementResult:
        """A job's statement (JobContext.run): refused text first (U+0000), then the worker's path."""
        _check_text(sql)
        return self._run_statement(req, "job", sql, options, listener=listener, log_chars=_JOB_LOG_CHARS)

    def _job_event(self, event: BaseModel) -> None:
        """A job's progress or outcome: kept for a client that attaches later, given to the job list, and sent."""
        self._send(self._keep_job_event(event))

    def _keep_job_event(self, event: BaseModel) -> BaseModel:
        with self._emit_lock:
            self._job_events[getattr(event, "type", "")] = event
        self._notify("job list", self._hooks.jobs.report, event)
        return event

    def _end_job(self, req: _Request, ctx: JobContext | None, report: JobReport) -> None:
        """The job's last events: its error, the job_outcome, then its History record, the schema cache, the
        session_state and `finished`."""
        rid, outcome, error = req.request_id, report.outcome, report.error
        if error is not None:
            self._send(ErrorEvent(request_id=rid, message=error.message, code=error.code, position=error.position))
        self._job_event(outcome)
        if ctx is not None and ctx.record_id is not None:
            status, text = history_status(outcome, error)
            self._notify("history", self._hooks.jobs.finish_record, ctx.record_id, status, req.elapsed_ms(),
                         outcome.rows, text)
        if report.schema_changed:
            self._notify("schema change", self._hooks.on_schema_change)
        warn = outcome.outcome in ("partial", "unknown")
        logger.log("WARNING" if warn else "INFO",
                   "[{}] job {} ({}) → {}{} in {} ms: {:,} statement(s), {:,} row(s), {:,} bytes{}", self.name, rid,
                   outcome.job, outcome.outcome, " (cancelled)" if outcome.cancelled else "", req.elapsed_ms(),
                   outcome.statements, outcome.rows, outcome.bytes,
                   f" ({error.code})" if error is not None and error.code else "")
        self._listen(None)
        self._send_finished(rid, req.elapsed_ms(), cancelled=outcome.cancelled)

    def _transaction_command(self, cmd: TransactionCommand) -> None:
        self._discard_open()
        started = time.monotonic()
```

In `backend/dbaguy_backend/sessions/worker.py`, replace:

```python
    ) -> None:
        """The one `finished` every request ends with, right after the `session_state` it reports."""
        state = self._refresh_state(began_at, ddl_done)
        self._send(FinishedEvent(
            request_id=request_id,
            elapsed_ms=elapsed_ms,
```

with:

```python
    ) -> None:
        """The one `finished` every request ends with, right after the `session_state` it reports."""
        state = self._refresh_state(began_at, ddl_done)
        self.finished_at = time.monotonic()
        self._send(FinishedEvent(
            request_id=request_id,
            elapsed_ms=elapsed_ms,
```

- [ ] **Step 7: Hooks, manager, routes and container**

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.history import HistoryRecord
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .worker import ExecutionRecord, WorkerHooks


class SessionHooksFactory:
```

with:

```python
import threading
import time
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import JobCommand
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
from ..adapters.base import StatementError
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .worker import ExecutionRecord, WorkerHooks

READ_ONLY_RECHECK_S = 1.0  # a job's statement gate re-reads the profile at most this often (R-06-6)


class _Recent:
    """`read()`, re-read at most every `every_s`: ProfileStore.get reads profiles.json each time, and a run of a
    million statements gates each one (R-06-6). Any thread."""

    def __init__(self, read: Callable[[], bool], every_s: float = READ_ONLY_RECHECK_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._read = read
        self._every_s = every_s
        self._clock = clock
        self._lock = threading.Lock()
        self._value = False
        self._read_at: float | None = None

    def __call__(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._read_at is None or now - self._read_at >= self._every_s:
                self._value, self._read_at = self._read(), now
            return self._value


class SessionHooksFactory:
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record, capabilities=capabilities,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),  # every database of the profile
            take_edit_plan=take_edit_plan,
        )
```

with:

```python
        return WorkerHooks(
            dialect=dialect, check=check, on_record=on_record, capabilities=capabilities,
            on_schema_change=lambda: self._schema_cache.invalidate(profile_id),  # every database of the profile
            take_edit_plan=take_edit_plan, jobs=self._job_hooks(profile, database),
        )

    def _job_hooks(self, profile: AnyProfile, database: str | None) -> JobHooks:
        """A job's statement gate (gate_confirmed, the profile re-read at most once a second) and its History record."""
        profile_id = profile.id
        dialect = self._registry.get(profile.engine).dialect
        read_only = _Recent(lambda: self._profiles.get(profile_id).read_only)

        def build(command: JobCommand) -> Job:
            return self.build_job(command, profile, database)

        def begin_record(sql: str, started_at: datetime) -> int | None:
            return self._history.begin(HistoryRecord(
                profile_id=profile_id, profile_name=profile.name, database=database, sql=sql, started_at=started_at,
                elapsed_ms=0, rows=0, status="running",
            ))

        def finish_record(record_id: int, status: HistoryStatus, elapsed_ms: int, rows: int, error: str | None) -> None:
            self._history.finish(record_id, status, elapsed_ms, rows, error)

        return JobHooks(
            build=build, gate=lambda sql: self._policy.gate_confirmed(sql, dialect, read_only()), read_only=read_only,
            begin_record=begin_record, finish_record=finish_record,
        )

    def build_job(self, command: JobCommand, profile: AnyProfile, database: str | None) -> Job:
        """The job object for a job command on a session of `profile` (the composition root of data tools' jobs)."""
        raise StatementError(f"{type(command).__name__} is not a job this backend runs", code="not_supported")
```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from loguru import logger
```

with:

```python
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from uuid import UUID, uuid4

from loguru import logger
```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python
from ..services.connection_service import ConnectionService
from ..services.profile_service import ProfileService
from .hooks import SessionHooksFactory
from .worker import Emitter, SessionWorker


```

with:

```python
from ..services.connection_service import ConnectionService
from ..services.profile_service import ProfileService
from .hooks import SessionHooksFactory
from .jobs.registry import JobRegistry
from .worker import Emitter, SessionWorker


```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python


class SessionManager:
    """User sessions (one per editor tab). Closed explicitly or after idling without a WebSocket."""

    def __init__(
        self, connections: ConnectionService, profiles: ProfileService, hooks: SessionHooksFactory | None = None,
        idle_timeout: float = 600.0,
    ) -> None:
        self._connections = connections
        self._profiles = profiles
        self._hooks = hooks  # None: workers keep their defaults (everything runs, nothing is recorded)
        self._idle_timeout = idle_timeout
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()

```

with:

```python


class SessionManager:
    """User sessions (one per editor tab, one per data-tools job). Closed explicitly, or once idle without a WebSocket:
    nothing running or queued (D-06-10), and no attach and no request ending for `idle_timeout`."""

    def __init__(
        self, connections: ConnectionService, profiles: ProfileService, hooks: SessionHooksFactory | None = None,
        idle_timeout: float = 600.0, jobs: JobRegistry | None = None,
    ) -> None:
        self._connections = connections
        self._profiles = profiles
        self._hooks = hooks  # None: workers keep their defaults (everything runs, nothing is recorded)
        self._idle_timeout = idle_timeout
        self.jobs = jobs if jobs is not None else JobRegistry()
        self._sessions: dict[str, _Entry] = {}
        self._lock = threading.Lock()

```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python
            worker.start()
            server = worker.call(lambda c: c.server_info())
            if self._hooks is not None:
                worker.configure(self._hooks.build(profile.id, server.current_database))  # before it is registered
        except BaseException:
            worker.close()  # whatever failed, never leave the thread, connection or tunnel behind
            raise
```

with:

```python
            worker.start()
            server = worker.call(lambda c: c.server_info())
            if self._hooks is not None:
                hooks = self._hooks.build(profile.id, server.current_database)
                jobs = replace(hooks.jobs, announce=partial(self.jobs.begin, session_id, profile.id),
                               report=partial(self.jobs.report, session_id))
                worker.configure(replace(hooks, jobs=jobs))  # before it is registered
        except BaseException:
            worker.close()  # whatever failed, never leave the thread, connection or tunnel behind
            raise
```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python
            entry.attached += 1
        entry.worker.set_emitter(emit)
        entry.worker.replay_state()  # a client attaching mid-transaction learns the state before any `finished`
        return entry.worker

    def detach(self, session_id: str, emit: Emitter) -> None:
```

with:

```python
            entry.attached += 1
        entry.worker.set_emitter(emit)
        entry.worker.replay_state()  # a client attaching mid-transaction learns the state before any `finished`
        entry.worker.replay_job()  # and a reloaded window the progress or outcome of the session's job (D-06-10)
        return entry.worker

    def detach(self, session_id: str, emit: Emitter) -> None:
```

In `backend/dbaguy_backend/sessions/manager.py`, replace:

```python
                logger.exception("{} failed for session {}", what, session_id)

    def _is_idle(self, entry: _Entry) -> bool:
        return entry.attached == 0 and time.monotonic() - entry.detached_at >= self._idle_timeout

    def _idle_ids(self) -> list[str]:
        with self._lock:
```

with:

```python
                logger.exception("{} failed for session {}", what, session_id)

    def _is_idle(self, entry: _Entry) -> bool:
        if entry.attached or entry.worker.busy():  # a job outlives its dialog and the window (D-06-10)
            return False
        return time.monotonic() - max(entry.detached_at, entry.worker.finished_at) >= self._idle_timeout

    def _idle_ids(self) -> list[str]:
        with self._lock:
```

Create `backend/dbaguy_backend/api/data.py`:

```python
"""Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
own (dump, run_file); these routes find it again after the window reloads, and cancel it."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ..container import AppContainer
from ..domain.data_jobs import JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])


class JobCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    request_id: str


@router.get("/jobs", response_model=list[JobInfo])
def list_jobs(container: AppContainer = Depends(get_container)) -> list[JobInfo]:
    """Newest first: running and finished, until cleared (D-06-10)."""
    return container.sessions.jobs.list()


@router.post("/jobs/cancel", status_code=204)
def cancel_job(body: JobCancelRequest, container: AppContainer = Depends(get_container)) -> Response:
    """The WebSocket's `cancel`, for a job whose dialog is gone: a job that has ended is left as it is."""
    if container.sessions.jobs.get(body.session_id, body.request_id) is None:
        raise NotFound("No such job")
    container.sessions.worker(body.session_id).cancel(body.request_id)  # SessionNotFound once its session closed
    return Response(status_code=204)


@router.post("/jobs/clear", status_code=204)
def clear_jobs(container: AppContainer = Depends(get_container)) -> Response:
    """Forget the jobs that have ended (the Jobs list's Clear finished)."""
    container.sessions.jobs.clear_finished()
    return Response(status_code=204)
```

In `backend/dbaguy_backend/app.py`, replace:

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger

from .api import couchbase, edits, history, metadata, profiles, scripts, sessions, sql, system, workspace
from .api import settings as settings_api  # `settings` is the parameter of create_app
from .api.errors import register_error_handlers
from .config import Settings, load_settings
```

with:

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger

from .api import couchbase, data, edits, history, metadata, profiles, scripts, sessions, sql, system, workspace
from .api import settings as settings_api  # `settings` is the parameter of create_app
from .api.errors import register_error_handlers
from .config import Settings, load_settings
```

In `backend/dbaguy_backend/app.py`, replace:

```python
    app.include_router(sessions.router)
    app.include_router(couchbase.router)
    app.include_router(edits.router)
    app.include_router(settings_api.router)
    app.include_router(workspace.router)
    app.include_router(scripts.router)
```

with:

```python
    app.include_router(sessions.router)
    app.include_router(couchbase.router)
    app.include_router(edits.router)
    app.include_router(data.router)
    app.include_router(settings_api.router)
    app.include_router(workspace.router)
    app.include_router(scripts.router)
```

In `backend/dbaguy_backend/container.py`, replace:

```python
from .services.settings_service import SettingsService
from .services.workspace_service import WorkspaceService
from .sessions.hooks import SessionHooksFactory
from .sessions.manager import SessionManager
from .sql.classifier import warm_up
from .storage.paths import default_scripts_dir, history_file, profiles_file, settings_file, workspace_file
```

with:

```python
from .services.settings_service import SettingsService
from .services.workspace_service import WorkspaceService
from .sessions.hooks import SessionHooksFactory
from .sessions.jobs.registry import JobRegistry
from .sessions.manager import SessionManager
from .sql.classifier import warm_up
from .storage.paths import default_scripts_dir, history_file, profiles_file, settings_file, workspace_file
```

In `backend/dbaguy_backend/container.py`, replace:

```python
        hooks = SessionHooksFactory(
            self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
        )
        self.sessions = SessionManager(self.connections, self.profiles, hooks)
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

```

with:

```python
        hooks = SessionHooksFactory(
            self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
        )
        self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=JobRegistry())
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

```

- [ ] **Step 8: API types and the History panel**

Regenerate the API types (never edit them by hand), from the repository root:

```bash
npm run gen:api
```

`src/lib/api/openapi.json` and `src/lib/api/schema.d.ts` change; nothing else.

In `src/lib/api/types.ts`, replace:

```ts
export type EditOutcome = EditOutcomeEvent["outcome"];
/** Grid editing (spec ⑤ §3.7): a table tab's staged edits as `workspace.json` keeps them. */
export type StagedEditsFile = S["StagedEditsFile"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

with:

```ts
export type EditOutcome = EditOutcomeEvent["outcome"];
/** Grid editing (spec ⑤ §3.7): a table tab's staged edits as `workspace.json` keeps them. */
export type StagedEditsFile = S["StagedEditsFile"];
/** Data tools (spec ⑥ D-06-11): how far a job is, at most every 250 ms (cumulative counts). */
export type JobProgressEvent = S["JobProgressEvent"];
/** Data tools (spec ⑥ R-06-12): how a job ended, sent once right before its `session_state` and `finished`. */
export type JobOutcomeEvent = S["JobOutcomeEvent"];
/** "completed", "failed", "rolled_back", "partial", "stopped", "unknown" or "not_started"; a cancel is `cancelled`. */
export type JobOutcomeKind = JobOutcomeEvent["outcome"];
/** Data tools: a statement that failed (line, statement number, the server's code and text). */
export type JobError = S["JobError"];
/** Data tools (spec ⑥ D-06-10): a job of this backend run, as `GET /data/jobs` lists it. */
export type JobInfo = S["JobInfo"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

The History panel maps every status to an icon, so the two new ones need one each:

In `src/features/history/HistoryPanel.tsx`, replace:

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Ban, CircleCheck, CircleX, Search, TimerOff, Trash2, X, type LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
```

with:

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Ban, CircleAlert, CircleCheck, CircleX, LoaderCircle, Search, TimerOff, Trash2, X, type LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
```

In `src/features/history/HistoryPanel.tsx`, replace:

```tsx
  error: { icon: CircleX, label: "Failed", className: "text-danger" },
  timeout: { icon: TimerOff, label: "Timed out", className: "text-warning" },
  cancelled: { icon: Ban, label: "Cancelled", className: "text-muted" },
};

function StatusIcon({ status }: { status: HistoryStatus }) {
```

with:

```tsx
  error: { icon: CircleX, label: "Failed", className: "text-danger" },
  timeout: { icon: TimerOff, label: "Timed out", className: "text-warning" },
  cancelled: { icon: Ban, label: "Cancelled", className: "text-muted" },
  // spec ⑥ D-06-9: a Run SQL file or copy that has not ended, or that was running when the backend ended
  running: { icon: LoaderCircle, label: "Running", className: "text-muted" },
  interrupted: { icon: CircleAlert, label: "Interrupted", className: "text-warning" },
};

function StatusIcon({ status }: { status: HistoryStatus }) {
```

- [ ] **Step 9: Run the tests, the worker's and the protocol's**

Run: `cd backend && .venv/bin/pytest -q tests/test_job_framework.py tests/test_history.py tests/test_safety.py tests/test_session_worker.py tests/test_sessions.py tests/test_worker_protocol.py tests/test_session_wiring.py tests/test_edit_worker.py && cd .. && npm run typecheck`
Expected: `582 passed` (22 new in `test_job_framework.py`, 2 in `test_history.py`, 2 tests (12 cases) in `test_safety.py`); typecheck clean.

- [ ] **Step 10: Commit**

```bash
git add backend/dbaguy_backend/api/data.py \
  backend/dbaguy_backend/app.py \
  backend/dbaguy_backend/container.py \
  backend/dbaguy_backend/domain/data_jobs.py \
  backend/dbaguy_backend/domain/execution.py \
  backend/dbaguy_backend/domain/history.py \
  backend/dbaguy_backend/services/history_service.py \
  backend/dbaguy_backend/services/safety.py \
  backend/dbaguy_backend/sessions/hooks.py \
  backend/dbaguy_backend/sessions/jobs/__init__.py \
  backend/dbaguy_backend/sessions/jobs/context.py \
  backend/dbaguy_backend/sessions/jobs/registry.py \
  backend/dbaguy_backend/sessions/manager.py \
  backend/dbaguy_backend/sessions/worker.py \
  backend/tests/test_history.py \
  backend/tests/test_job_framework.py \
  backend/tests/test_safety.py \
  src/features/history/HistoryPanel.tsx \
  src/lib/api/openapi.json \
  src/lib/api/schema.d.ts \
  src/lib/api/types.ts
git commit -m "feat(06a): job framework — one outcome per job, progress, job list, History running/interrupted, confirmed gate"
```

---

### Task 3: Run SQL file: `RunFileJob`, inspect, `run_file`; other tools' files (A8)

**Files:**
- Create: `backend/dbaguy_backend/services/data_service.py`, `backend/dbaguy_backend/sessions/jobs/run_file.py`, `backend/dbaguy_backend/sql/dump_header.py`, `backend/tests/data_support.py`, `backend/tests/fixtures/run_file/mysqldump.sql`, `backend/tests/fixtures/run_file/mysqldump_raw_binary.sql`, `backend/tests/fixtures/run_file/pg_dump_copy.sql`, `backend/tests/fixtures/run_file/pg_dump_inserts.sql`, `backend/tests/fixtures/run_file/sqlite3_dump.sql`, `backend/tests/fixtures/run_file/ssms_script.sql`, `backend/tests/test_run_file.py`, `backend/tests/test_run_file_inspect.py`, `backend/tests/test_run_file_job.py`
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/couchbase/adapter.py`, `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/api/data.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/domain/data_jobs.py`, `backend/dbaguy_backend/domain/errors.py`, `backend/dbaguy_backend/domain/execution.py`, `backend/dbaguy_backend/domain/profiles.py`, `backend/dbaguy_backend/sessions/hooks.py`, `src/lib/api/types.ts`
- Regenerate (never by hand): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: Task 1 (`StatementStream`, `FileStatement`, `StreamError`); Task 2 (`Job`, `JobContext`, `JobReport`, `build_job`, `JobRegistry`, History `begin`/`finish`); ⑤'s `transaction_frame(dialect)`; `is_transaction_statement`, `first_keyword`; `DatabaseConnection.in_transaction()`, `transaction_failed()`, `rollback()`; the adapters' `unsupported_copy` (PostgreSQL); `QueryContext` (Couchbase).
- Produces:
  - `AdapterCapabilities.run_file` and `EngineInfo.run_file`: True on all five engines.
  - `domain/execution.RunFileCommand{type: "run_file", path, mode: "all_or_nothing" | "statements", on_error: "stop" | "continue", from_statement, search_path, query_context}`: `continue` only with `statements`.
  - `domain/data_jobs`: `ENGINE_NAMES`, `DumpHeader` (what a ⑥ header says), `FileInspection{path, size, header, preview, preview_truncated, warnings}`.
  - `sql/dump_header.py`: `FORMAT = 1`, `header_lines(...)`, `parse_header(text) -> DumpHeader | None`; Task 8 adds `comment_safe`.
  - `sessions/jobs/run_file.RunFileJob(command, profile_name)`: the run and every outcome of R-06-12 (see the module's `_Run`). `summary(command, size)` is the History text, never the statements. The codes and hints are in Global Constraints.
  - `services/data_service.DataService(profiles, registry, metadata, jobs)`: `inspect(profile_id, path) -> FileInspection` opens no connection; a read-only profile → `ReadOnlyProfile` (403); a missing file → `FileNotFound` (404 `file_not_found`).
  - `POST /data/run-file/inspect {profile_id, path}`. `AppContainer.data`. `domain/errors`: `NotSupported` (422 `not_supported`), `FileNotFound`.
  - The SQLite adapter's connections use `cached_statements=0` (S5).
  - `tests/data_support.py`: `SCRATCH_PREFIX = "zz_06_"`, `scratch_name(kind)`, `drop_statement`, `dropping(db, before_drop)` (scratch objects in a ScratchDb's schema), `Ran` (a job's events: `outcome`, `error`, `messages`, `progress`, `finished`), `JobTab` (a job session's worker with a recorder: `run(make_command)`, `submit`, `wait`, `wait_for_progress`), `DataApp(tmp_path)` (an `AppContainer` over a temp data dir with in-memory secrets: `add_profile`, `inspect`, `job_session`, `history`, `close`).

- [ ] **Step 1: Probe A8 — other tools' files (the fixtures).** The files in `backend/tests/fixtures/run_file/` are what the dev containers' own tools wrote while planning, for one scratch table `zz_06_tool_tbl`, made and dropped by exact name: `pg_dump --inserts` and plain `pg_dump` (PostgreSQL 17.11's container; `\restrict` lines included); `mysqldump --single-transaction --triggers --set-gtid-purged=OFF`, with and without `--hex-blob` (MySQL 8.4.11's); `sqlite3 .dump` (3.53.4); and an SSMS-style script with CRLF line ends and `GO`. Step 2 gives their text, so the tests do not depend on the tools; the raw-binary one is derived from the `--hex-blob` one, as mysqldump writes it. What the tools showed: `pg_dump` writes `\restrict <key>` and `\unrestrict <key>` lines (psql 17.6+) around the dump, and a `SELECT pg_catalog.set_config('search_path', '', false)`. mysqldump without `--hex-blob` writes VARBINARY as raw bytes, which is not UTF-8 (S11). `sqlite3 .dump` wraps everything in `BEGIN TRANSACTION`/`COMMIT`. The Step 7 tests run each file: if one stops differently, the file's text decides and the job is fixed.

- [ ] **Step 2: Write the failing tests and the fixtures**

Create `backend/tests/data_support.py`:

```python
"""Data-tools tests (⑥): scratch objects on the dev databases, made and dropped by exact name, and the backend in-process
running jobs on sessions of their own.

Every object a test makes is named `zz_06_<kind>_<random>` (several runs may share a server) and dropped by that name
when its `dropping()` block ends, whatever the test did. Seeded tables are never written; Couchbase is written only
under `zz_06_` keys in `dbaguy.app.orders`, removed by exact key. Set-up and checks go through each engine's own driver
(`tests.edit_support`'s ScratchDb), never through the code under test."""

import itertools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import FileInspection, JobCommand, JobOutcomeEvent, JobProgressEvent
from dbaguy_backend.domain.execution import ErrorEvent, FinishedEvent
from dbaguy_backend.domain.history import HistoryEntry
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.sessions.manager import SessionCreateRequest
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.edit_support import ScratchDb
from tests.support import unique_name
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"


def scratch_name(kind: str) -> str:
    return unique_name(SCRATCH_PREFIX + kind)


@contextmanager
def dropping(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[list[tuple[str, str]]]:
    """A list the test appends `(kind, bare name)` to for every object it (or a job) creates in `db`'s schema. On
    exit `before_drop` runs (close the sessions that could hold a lock the DROP waits for), then each is dropped by
    exact name, newest first; every drop is tried before the failures are reported, and a failure of the body wins."""
    made: list[tuple[str, str]] = []

    def clean_up() -> str:
        failures = []
        if before_drop is not None:
            try:
                before_drop()
            except Exception as exc:
                failures.append(f"before_drop: {exc}")
        for kind, name in reversed(made):
            assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped here"
            try:
                db.execute(f"DROP {kind} IF EXISTS {db.table(name)}")
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        return "; ".join(failures)

    try:
        yield made
    except BaseException as exc:
        if failed := clean_up():
            exc.add_note("dropping scratch objects failed: " + failed)
        raise
    else:
        failed = clean_up()
        assert not failed, "dropping scratch objects failed: " + failed


@dataclass
class Ran:
    """One job's events, from its `started` to its `finished`."""

    events: list

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]

    @property
    def outcome(self) -> JobOutcomeEvent:
        [event] = [event for event in self.events if event.type == "job_outcome"]  # exactly one per job
        return event

    @property
    def error(self) -> ErrorEvent | None:
        return next((event for event in self.events if event.type == "error"), None)

    @property
    def messages(self) -> list[str]:
        return [event.text for event in self.events if event.type == "message"]

    @property
    def progress(self) -> list[JobProgressEvent]:
        return [event for event in self.events if event.type == "job_progress"]

    @property
    def finished(self) -> FinishedEvent:
        return self.events[-1]


class JobTab:
    """A job's session: its worker, with the events it sent."""

    def __init__(self, worker: SessionWorker, recorder: Recorder, session_id: str) -> None:
        self.worker = worker
        self.recorder = recorder
        self.session_id = session_id
        self._ids = itertools.count(1)

    def submit(self, make: Callable[[str], JobCommand]) -> str:
        """`make(request_id)` builds the command."""
        rid = f"job{next(self._ids)}"
        self.worker.submit(make(rid))
        return rid

    def run(self, make: Callable[[str], JobCommand], timeout: float = 120.0) -> Ran:
        return self.wait(self.submit(make), timeout)

    def wait(self, rid: str, timeout: float = 120.0) -> Ran:
        return Ran(request_events(self.recorder, rid, timeout))

    def wait_for_progress(self, rid: str, test: Callable[[JobProgressEvent], bool], timeout: float = 60.0) -> None:
        self.recorder.wait_for(lambda e: e.type == "job_progress" and e.request_id == rid and test(e), timeout)


class DataApp:
    """The backend in-process — an AppContainer over a temporary data dir with secrets in memory — so a test runs jobs
    on sessions with their real hooks and adapters."""

    def __init__(self, directory: Path) -> None:
        settings = Settings(token="data", data_dir=directory / "data", log_to_file=False)
        self.container = AppContainer(settings, secret_store=InMemorySecretStore())

    def add_profile(self, body: dict) -> UUID:
        return self.container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id

    def inspect(self, profile_id: UUID, path: Path) -> FileInspection:
        return self.container.data.inspect(profile_id, str(path))

    def job_session(self, profile_id: UUID, database: str | None = None) -> JobTab:
        info = self.container.sessions.create(SessionCreateRequest(profile_id=profile_id, database=database))
        recorder = Recorder()
        return JobTab(self.container.sessions.attach(info.session_id, recorder), recorder, info.session_id)

    def history(self) -> list[HistoryEntry]:
        return self.container.history.search().items

    def close_sessions(self) -> None:
        self.container.sessions.close_all()

    def close(self) -> None:
        self.container.close()
```

Create `backend/tests/fixtures/run_file/pg_dump_inserts.sql`:

```sql
--
-- PostgreSQL database dump
--

\restrict 6jDw07Dbi2XyxZkS6fzEQsssvW5fdW9nuxrZjuA2PGdGvHTyc1vQ9WYbnn9IzxS

-- Dumped from database version 17.11 (Debian 17.11-1.pgdg13+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: zz_06_tool_tbl; Type: TABLE; Schema: public; Owner: dbaguy
--

CREATE TABLE public.zz_06_tool_tbl (
    id integer NOT NULL,
    label text NOT NULL,
    data bytea,
    amount numeric(30,10)
);


ALTER TABLE public.zz_06_tool_tbl OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE; Schema: public; Owner: dbaguy
--

CREATE SEQUENCE public.zz_06_tool_tbl_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: dbaguy
--

ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNED BY public.zz_06_tool_tbl.id;


--
-- Name: zz_06_tool_tbl id; Type: DEFAULT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl ALTER COLUMN id SET DEFAULT nextval('public.zz_06_tool_tbl_id_seq'::regclass);


--
-- Data for Name: zz_06_tool_tbl; Type: TABLE DATA; Schema: public; Owner: dbaguy
--

INSERT INTO public.zz_06_tool_tbl VALUES (1, 'it''s', '\x00ff', 12345678901234567890.1234567890);
INSERT INTO public.zz_06_tool_tbl VALUES (2, 'two
lines', NULL, NULL);
INSERT INTO public.zz_06_tool_tbl VALUES (3, '中文😀', '\xdeadbeef', -1.5000000000);


--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE SET; Schema: public; Owner: dbaguy
--

SELECT pg_catalog.setval('public.zz_06_tool_tbl_id_seq', 3, true);


--
-- Name: zz_06_tool_tbl zz_06_tool_tbl_pkey; Type: CONSTRAINT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl
    ADD CONSTRAINT zz_06_tool_tbl_pkey PRIMARY KEY (id);


--
-- Name: zz_06_tool_tbl_label; Type: INDEX; Schema: public; Owner: dbaguy
--

CREATE INDEX zz_06_tool_tbl_label ON public.zz_06_tool_tbl USING btree (label);


--
-- PostgreSQL database dump complete
--

\unrestrict 6jDw07Dbi2XyxZkS6fzEQsssvW5fdW9nuxrZjuA2PGdGvHTyc1vQ9WYbnn9IzxS

```

Create `backend/tests/fixtures/run_file/pg_dump_copy.sql`:

```sql
--
-- PostgreSQL database dump
--

\restrict 9n9GvQZd9BHKBD2NncLpVHY4lAcahnwO726kllWLShcGgRldVUSyQeKRvnGUbpX

-- Dumped from database version 17.11 (Debian 17.11-1.pgdg13+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: zz_06_tool_tbl; Type: TABLE; Schema: public; Owner: dbaguy
--

CREATE TABLE public.zz_06_tool_tbl (
    id integer NOT NULL,
    label text NOT NULL,
    data bytea,
    amount numeric(30,10)
);


ALTER TABLE public.zz_06_tool_tbl OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE; Schema: public; Owner: dbaguy
--

CREATE SEQUENCE public.zz_06_tool_tbl_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNER TO dbaguy;

--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: dbaguy
--

ALTER SEQUENCE public.zz_06_tool_tbl_id_seq OWNED BY public.zz_06_tool_tbl.id;


--
-- Name: zz_06_tool_tbl id; Type: DEFAULT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl ALTER COLUMN id SET DEFAULT nextval('public.zz_06_tool_tbl_id_seq'::regclass);


--
-- Data for Name: zz_06_tool_tbl; Type: TABLE DATA; Schema: public; Owner: dbaguy
--

COPY public.zz_06_tool_tbl (id, label, data, amount) FROM stdin;
1	it's	\\x00ff	12345678901234567890.1234567890
2	two\nlines	\N	\N
3	中文😀	\\xdeadbeef	-1.5000000000
\.


--
-- Name: zz_06_tool_tbl_id_seq; Type: SEQUENCE SET; Schema: public; Owner: dbaguy
--

SELECT pg_catalog.setval('public.zz_06_tool_tbl_id_seq', 3, true);


--
-- Name: zz_06_tool_tbl zz_06_tool_tbl_pkey; Type: CONSTRAINT; Schema: public; Owner: dbaguy
--

ALTER TABLE ONLY public.zz_06_tool_tbl
    ADD CONSTRAINT zz_06_tool_tbl_pkey PRIMARY KEY (id);


--
-- Name: zz_06_tool_tbl_label; Type: INDEX; Schema: public; Owner: dbaguy
--

CREATE INDEX zz_06_tool_tbl_label ON public.zz_06_tool_tbl USING btree (label);


--
-- PostgreSQL database dump complete
--

\unrestrict 9n9GvQZd9BHKBD2NncLpVHY4lAcahnwO726kllWLShcGgRldVUSyQeKRvnGUbpX

```

Create `backend/tests/fixtures/run_file/mysqldump.sql`:

```sql
-- MySQL dump 10.13  Distrib 8.4.11, for Linux (aarch64)
--
-- Host: localhost    Database: dbaguy
-- ------------------------------------------------------
-- Server version	8.4.11

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `zz_06_tool_tbl`
--

DROP TABLE IF EXISTS `zz_06_tool_tbl`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `zz_06_tool_tbl` (
  `id` int NOT NULL AUTO_INCREMENT,
  `label` varchar(50) NOT NULL,
  `data` varbinary(8) DEFAULT NULL,
  `born` date DEFAULT NULL,
  `note` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `zz_06_tool_tbl`
--

LOCK TABLES `zz_06_tool_tbl` WRITE;
/*!40000 ALTER TABLE `zz_06_tool_tbl` DISABLE KEYS */;
INSERT INTO `zz_06_tool_tbl` VALUES (1,'it\'s \\ back',0x00FF,'2026-10-20',NULL),(2,'中文😀',NULL,NULL,'x;y');
/*!40000 ALTER TABLE `zz_06_tool_tbl` ENABLE KEYS */;
UNLOCK TABLES;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`dbaguy`@`%`*/ /*!50003 TRIGGER `zz_06_tool_tbl_bi` BEFORE INSERT ON `zz_06_tool_tbl` FOR EACH ROW BEGIN IF NEW.note IS NULL THEN SET NEW.note = ''; END IF; END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-10-20  3:54:57
```

Make `backend/tests/fixtures/run_file/mysqldump_raw_binary.sql` from `mysqldump.sql`: what mysqldump writes without `--hex-blob` — the same file with the VARBINARY value as raw bytes (`_binary '\0` and the byte 0xFF), which is not UTF-8 (A8). Run from the repository root:

```bash
backend/.venv/bin/python - <<'EOF'
from pathlib import Path
fixtures = Path("backend/tests/fixtures/run_file")
hexed = (fixtures / "mysqldump.sql").read_bytes()
assert hexed.count(b"0x00FF,") == 1
(fixtures / "mysqldump_raw_binary.sql").write_bytes(hexed.replace(b"0x00FF,", b"_binary '\\0\xff',"))
EOF
```

Create `backend/tests/fixtures/run_file/sqlite3_dump.sql`:

```sql
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE zz_06_tool_tbl (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, data BLOB, v);
INSERT INTO zz_06_tool_tbl VALUES(1,'it''s',X'00ff','007');
INSERT INTO zz_06_tool_tbl VALUES(2,unistr('two\u000alines'),NULL,1.5);
INSERT INTO zz_06_tool_tbl VALUES(3,'中文😀',NULL,0);
INSERT INTO sqlite_sequence VALUES('zz_06_tool_tbl',3);
CREATE TRIGGER zz_06_tool_tbl_ai AFTER INSERT ON zz_06_tool_tbl BEGIN UPDATE zz_06_tool_tbl SET v = coalesce(v, 0) WHERE id = new.id; END;
COMMIT;
```

Create `backend/tests/fixtures/run_file/ssms_script.sql` (CRLF line endings):

```sql
/****** Object:  Table [dbo].[zz_06_tool_tbl]    Script Date: 10/20/2026 12:00:00 PM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
SET ANSI_PADDING ON
GO
CREATE TABLE [dbo].[zz_06_tool_tbl](
	[id] [int] IDENTITY(1,1) NOT NULL,
	[label] [nvarchar](50) NOT NULL,
	[data] [varbinary](8) NULL,
 CONSTRAINT [PK_zz_06_tool_tbl] PRIMARY KEY CLUSTERED 
(
	[id] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
SET IDENTITY_INSERT [dbo].[zz_06_tool_tbl] ON 
GO
INSERT [dbo].[zz_06_tool_tbl] ([id], [label], [data]) VALUES (1, N'it''s', 0x00FF)
GO
INSERT [dbo].[zz_06_tool_tbl] ([id], [label], [data]) VALUES (2, N'中文😀', NULL)
GO
SET IDENTITY_INSERT [dbo].[zz_06_tool_tbl] OFF
GO
/****** Object:  Trigger [dbo].[zz_06_tool_tbl_ai] ******/
CREATE TRIGGER [dbo].[zz_06_tool_tbl_ai] ON [dbo].[zz_06_tool_tbl] AFTER INSERT AS
BEGIN
	SET NOCOUNT ON;
END
GO
ALTER TABLE [dbo].[zz_06_tool_tbl] ENABLE TRIGGER [zz_06_tool_tbl_ai]
GO
```

`test_run_file_job.py` runs `RunFileJob` against `tests/fake_db.py`'s fake connection: every outcome row, every refusal, `GO n`, psql lines, `from_statement`, the search path, a line that is not UTF-8, and the History summary. `test_run_file.py` runs real files on every engine's scratch tables, plus the fixtures. `test_run_file_inspect.py` covers the inspection and its route.

Create `backend/tests/test_run_file_job.py`:

```python
"""Run SQL file (spec ⑥ §3.4, R-06-7, R-06-12) on a fake connection, so every way a run ends is forced: what is sent
and never sent, each outcome, the stops that skip-and-continue does not skip, psql lines, GO n, progress and History.
The live engines follow in test_run_file.py."""

from pathlib import Path

import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, StatementCancelled, StatementError
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.execution import RunFileCommand
from dbaguy_backend.services.safety import GuardAction, GuardDecision
from dbaguy_backend.sessions.jobs.context import JobHooks
from dbaguy_backend.sessions.jobs.run_file import COPY_HINT, HEX_BLOB_HINT, RunFileJob, summary
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_sessions import Recorder, request_events

LOST = StatementError("Connection to the server was lost", code="connection_lost")
FRAME = {Dialect.SQLITE: ("BEGIN IMMEDIATE", "COMMIT"), Dialect.POSTGRES: ("BEGIN", "COMMIT"),
         Dialect.MYSQL: ("START TRANSACTION", "COMMIT"), Dialect.TSQL: ("SET NOCOUNT OFF;\nBEGIN TRANSACTION",
                                                                        "COMMIT TRANSACTION")}


class RunFake(FakeConnection):
    """The frame's BEGIN opens a transaction and its COMMIT ends it (or raises `commit_error`; `commit_keeps` leaves it
    open). Statement words: `rows N`, `block` (waits for a cancel), `fail MSG`, `lost` (the link dies), `endtx` (ends
    the transaction, as MySQL DDL does), `copy` (the adapter's COPY refusal), `tx` (opens one), anything else: done."""

    def __init__(self, dialect: Dialect = Dialect.SQLITE, commit_error: StatementError | None = None,
                 commit_keeps: bool = False) -> None:
        super().__init__()
        self.begin, self.end = FRAME[dialect]
        self.commit_error = commit_error
        self.commit_keeps = commit_keeps

    def statement(self, sql, options):
        if sql == self.begin:
            self.runs.append(sql)
            self.tx = True
            return FakeResult(0)
        if sql == self.end:
            self.runs.append(sql)
            if self.commit_error is not None:
                if self.commit_error.code != "connection_lost":
                    self.tx = self.commit_keeps
                raise self.commit_error
            if not self.commit_keeps:
                self.tx = False
                self.commits += 1
            return FakeResult(0)
        word = sql.split()[0] if sql.split() else ""
        if word == "lost":
            self.runs.append(sql)
            raise LOST
        if word == "endtx":
            self.runs.append(sql)
            self.tx = False
            return FakeResult(0)
        if word == "copy":
            raise StatementError("COPY FROM STDIN / TO STDOUT is not supported in the console yet",
                                 code="unsupported_copy")
        if word == "tx":
            self.runs.append(sql)
            self.tx = True
            return FakeResult(0)
        return super().statement(sql, options)


def runs_everything(sql: str) -> GuardDecision:
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=StatementKind.UNKNOWN))


class Env:
    def __init__(self, conn: RunFake, *, dialect: Dialect = Dialect.SQLITE, gate=runs_everything, read_only=False,
                 transactions: bool = True) -> None:
        self.conn = conn
        self.records: list[tuple] = []
        lease = FakeLease(conn)
        self.worker = SessionWorker("run", lambda: lease)
        self.worker.start()
        jobs = JobHooks(build=lambda command: RunFileJob(command, "Fake DB"), gate=gate, read_only=lambda: read_only,
                        begin_record=lambda sql, at: self.records.append(("begin", sql)) or 1,
                        finish_record=lambda *args: self.records.append(("finish", *args)))
        capabilities = AdapterCapabilities(run_file=True, transactions=transactions)
        self.worker.configure(WorkerHooks(dialect=dialect, capabilities=capabilities, jobs=jobs))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def submit(self, path: Path, rid: str = "r", **fields) -> None:
        fields.setdefault("mode", "all_or_nothing")
        self.worker.submit(RunFileCommand(request_id=rid, path=str(path), **fields))

    def run(self, path: Path, **fields) -> list:
        self.submit(path, **fields)
        return request_events(self.recorder, "r")


@pytest.fixture
def env():
    made: list[Env] = []

    def make(conn: RunFake | None = None, **fields) -> Env:
        made.append(Env(conn or RunFake(fields.get("dialect", Dialect.SQLITE)), **fields))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


@pytest.fixture
def sql_file(tmp_path):
    def write(text: str | bytes, name: str = "run.sql") -> Path:
        path = tmp_path / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path

    return write


def outcome(events):
    [found] = [e for e in events if e.type == "job_outcome"]
    return found


def error(events):
    return next((e for e in events if e.type == "error"), None)


def test_all_or_nothing_sends_the_file_between_begin_and_commit(env, sql_file):
    e = env()
    path = sql_file("rows 1;\nrows 2;\n-- done\n")
    events = e.run(path)
    done = outcome(events)
    assert (done.outcome, done.statements, done.cancelled, done.path, done.bytes) == (
        "completed", 2, False, str(path), len("rows 1;\nrows 2;"))
    assert e.conn.runs == ["BEGIN IMMEDIATE", "rows 1", "rows 2", "COMMIT"] and not e.conn.tx
    assert [ev.type for ev in events][-3:] == ["job_outcome", "session_state", "finished"]
    progress = [ev for ev in events if ev.type == "job_progress"]
    assert progress[0].total_bytes == path.stat().st_size and progress[-1].stage == "commit"
    assert e.records[0] == ("begin", f"-- Run SQL file {path} (0.0 MB), all or nothing")
    assert e.records[1][2] == "ok"


@pytest.mark.parametrize(("statement", "code", "rolled_back_by_us"), [
    ("fail boom", "42000", True), ("block", "cancelled", True), ("lost", "connection_lost", False),
])
def test_all_or_nothing_stops_roll_everything_back(env, sql_file, statement, code, rolled_back_by_us):
    e = env()
    e.submit(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    if statement == "block":
        assert e.conn.blocked.wait(5)
        e.worker.cancel("r")
    events = request_events(e.recorder, "r")
    done = outcome(events)
    assert (done.outcome, done.cancelled, done.stopped_at.line, done.stopped_at.statement, done.statements) == (
        "rolled_back", statement == "block", 2, 2, 1)
    assert error(events).code == code
    assert "rows 3" not in e.conn.runs and "COMMIT" not in e.conn.runs
    assert e.conn.rollbacks == (1 if rolled_back_by_us else 0)
    assert events[-1].cancelled == (statement == "block")


def test_a_transaction_the_file_ends_stops_an_all_or_nothing_run_as_partial(env, sql_file):  # MySQL DDL, M8
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file("rows 1;\nendtx;\nrows 3;"))
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.statements) == ("partial", 2, 2)
    assert error(events).code == "transaction_ended" and "MySQL commits around DDL" in error(events).message
    assert e.conn.runs == ["START TRANSACTION", "rows 1", "endtx"]


@pytest.mark.parametrize("statement", ["COMMIT", "BEGIN TRANSACTION", "END", "ROLLBACK", "SAVEPOINT a"])
def test_the_files_own_transaction_statements_are_never_sent_all_or_nothing(env, sql_file, statement):
    e = env()
    events = e.run(sql_file(f"rows 1;\n{statement};\nrows 3;"))
    assert (outcome(events).outcome, error(events).code) == ("rolled_back", "transaction_in_file")
    assert error(events).message.startswith("The file controls its own transactions (line 2: ")
    assert e.conn.runs == ["BEGIN IMMEDIATE", "rows 1"]


def test_words_that_only_look_like_transactions_run(env, sql_file):
    e = env()
    events = e.run(sql_file("rows 1;\nbegin_x;\n"))  # not a transaction statement: sent
    assert outcome(events).outcome == "completed" and "begin_x" in e.conn.runs


@pytest.mark.parametrize(("commit_error", "keeps", "expected"), [
    (StatementError("deferred constraint", code="23503"), False, "rolled_back"),
    (LOST, False, "unknown"),
    (None, True, "unknown"),  # COMMIT returned with a transaction still open (⑤ G11)
])
def test_what_commit_answers_decides_the_outcome(env, sql_file, commit_error, keeps, expected):
    e = env(RunFake(commit_error=commit_error, commit_keeps=keeps))
    events = e.run(sql_file("rows 1;"))
    assert outcome(events).outcome == expected
    if expected == "rolled_back":
        assert error(events).message == "deferred constraint\nThe transaction was rolled back: nothing was applied"
    if keeps:
        assert error(events).code == "commit_unconfirmed" and e.conn.rollbacks == 1


def test_statement_by_statement_stops_at_an_error_or_skips_it(env, sql_file):
    path = sql_file("rows 1;\nfail boom;\nrows 3;\nfail again;\nrows 5;")
    stop = env()
    events = stop.run(path, mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, done.statements, stop.conn.runs) == (
        "stopped", 2, 1, ["rows 1", "fail boom"])
    go_on = env()
    events = go_on.run(path, mode="statements", on_error="continue")
    done = outcome(events)
    assert (done.outcome, done.statements, done.error_count) == ("completed", 3, 2)
    assert [(err.line, err.statement, err.code, err.message) for err in done.errors] == [
        (2, 2, "42000", "boom"), (4, 4, "42000", "again")]
    assert go_on.conn.runs == ["rows 1", "fail boom", "rows 3", "fail again", "rows 5"]
    finish = go_on.records[1]  # ("finish", record id, status, ms, rows, error)
    assert (finish[2], finish[5]) == ("ok", "2 error(s) skipped")


def test_statement_by_statement_a_lost_link_is_unknown_and_a_cancel_stops(env, sql_file):
    lost = env()
    done = outcome(lost.run(sql_file("rows 1;\nlost;\nrows 3;"), mode="statements", on_error="continue"))
    assert (done.outcome, done.stopped_at.line) == ("unknown", 2)
    cancelled = env()
    cancelled.submit(sql_file("rows 1;\nblock;\nrows 3;"), mode="statements")
    assert cancelled.conn.blocked.wait(5)
    cancelled.worker.cancel("r")
    done = outcome(request_events(cancelled.recorder, "r"))
    assert (done.outcome, done.cancelled, done.stopped_at.statement, done.statements) == ("stopped", True, 2, 1)


def test_from_statement_reads_the_earlier_statements_without_sending_them(env, sql_file):
    e = env()
    events = e.run(sql_file("rows 1;\nrows 2;\nrows 3;"), mode="statements", from_statement=2)
    assert (outcome(events).statements, e.conn.runs) == (2, ["rows 2", "rows 3"])
    beyond = env()
    done = outcome(beyond.run(sql_file("rows 1;", name="short.sql"), mode="statements", from_statement=5))
    assert done.outcome == "completed" and done.warnings == ["The file has 1 statements: none from statement 5 on was run"]


def test_psql_lines_are_skipped_or_stop_the_run(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    events = e.run(sql_file("\\restrict abc\nrows 1;\n\\unrestrict abc\n"), mode="statements")
    assert outcome(events).outcome == "completed"
    assert [ev.text for ev in events if ev.type == "message"] == [
        "Skipped line 1: \\restrict is a psql command", "Skipped line 3: \\unrestrict is a psql command"]
    stop = env(dialect=Dialect.POSTGRES)
    events = stop.run(sql_file("rows 1;\n\\connect other\nrows 3;"), mode="statements", on_error="continue")
    assert (outcome(events).outcome, error(events).code, stop.conn.runs) == ("stopped", "psql_meta_command", ["rows 1"])
    assert error(events).message == "Line 2 is a psql command (\\connect): run this file with psql"


def test_copy_from_stdin_stops_even_a_run_that_skips_errors(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    events = e.run(sql_file("rows 1;\ncopy t FROM stdin;\n1\tx\n\\.\nrows 2;"), mode="statements",
                   on_error="continue")
    assert (outcome(events).outcome, error(events).code) == ("stopped", "unsupported_copy")
    assert error(events).message.endswith(COPY_HINT)


def test_a_line_that_is_not_utf8_stops_the_run_there(env, sql_file):
    e = env(dialect=Dialect.MYSQL)
    events = e.run(sql_file(b"rows 1;\nrows '\xff';\n"), mode="statements")
    done = outcome(events)
    assert (done.outcome, done.stopped_at.line, error(events).code) == ("stopped", 2, "file_not_utf8")
    assert error(events).message == f"Line 2 is not UTF-8 text ({HEX_BLOB_HINT})"


def test_go_n_sends_a_batch_n_times_and_go_0_never(env, sql_file):
    e = env(dialect=Dialect.TSQL)
    events = e.run(sql_file("rows 1\nGO 3\nrows 2\nGO 0\nrows 3\nGO\n"), mode="statements")
    assert (outcome(events).statements, e.conn.runs) == (3, ["rows 1"] * 3 + ["rows 3"])


def test_a_read_only_connection_mid_run_stops_it(env, sql_file):
    def gate(sql: str) -> GuardDecision:
        action = GuardAction.BLOCK if sql == "rows 2" else GuardAction.RUN
        return GuardDecision(action=action, classification=Classification(kind=StatementKind.WRITE))

    e = env(gate=gate)
    events = e.run(sql_file("rows 1;\nrows 2;"), mode="statements", on_error="continue")
    assert (outcome(events).outcome, error(events).code) == ("stopped", "read_only")
    assert e.conn.runs == ["rows 1"]


@pytest.mark.parametrize(("setup", "fields", "code"), [
    ({}, {"path": "/nowhere/x.sql"}, "file_not_found"),
    ({"read_only": True}, {}, "read_only"),
    ({"transactions": False}, {}, "not_supported"),
    ({}, {"search_path": "public"}, "unsupported_option"),  # SQLite: no search path
])
def test_what_refuses_a_run_before_anything_is_sent(env, sql_file, setup, fields, code):
    e = env(**setup)
    path = fields.pop("path", sql_file("rows 1;"))
    events = e.run(path, **fields)
    assert (outcome(events).outcome, error(events).code, e.conn.runs) == ("not_started", code, [])
    assert e.records == []  # never started: no History record


def test_a_transaction_the_file_leaves_open_is_rolled_back_and_said(env, sql_file):
    e = env()
    done = outcome(e.run(sql_file("rows 1;\ntx;\nrows 3;"), mode="statements"))
    assert done.outcome == "completed" and e.conn.rollbacks == 1
    assert done.warnings == ["The file left a transaction open: it was rolled back, so the statements in it are not "
                             "applied"]


def test_the_search_path_goes_first(env, sql_file):
    e = env(dialect=Dialect.POSTGRES)
    e.run(sql_file("rows 1;"), search_path="Sales Data")
    assert e.conn.runs == ['SET search_path TO "Sales Data"', "BEGIN", "rows 1", "COMMIT"]


def test_the_history_summary():
    command = RunFileCommand(request_id="r", path="/x/a.sql", mode="statements", on_error="continue",
                             from_statement=12)
    assert summary(command, 155_400_000) == (
        "-- Run SQL file /x/a.sql (148.2 MB), statement by statement, skipping errors, from statement 12")
```

Create `backend/tests/test_run_file.py`:

```python
"""Run SQL file on the dev databases (spec ⑥ §3.4, R-06-7, §6): both modes, the outcomes a server can force (an
error, a cancel, MySQL's DDL commit, the file's own COMMIT), a read-only connection, the inspection, and files other
tools wrote (A8: pg_dump --inserts and its COPY form, mysqldump with and without --hex-blob, sqlite3 .dump, an SSMS
script). Scratch objects are `zz_06_*`, dropped by exact name; Couchbase writes only `zz_06_run_*` keys."""

import time
from pathlib import Path

import pytest

from dbaguy_backend.domain.errors import ReadOnlyProfile
from dbaguy_backend.domain.execution import QueryContext, RunFileCommand
from dbaguy_backend.sql.dialects import Dialect, script_text
from tests.data_support import DataApp, dropping, scratch_name
from tests.edit_support import ENGINES, ScratchDb, scratch_db
from tests.support import COUCHBASE_PASSWORD, couchbase_query, make_couchbase_profile, requires_couchbase, \
    requires_mssql, requires_mysql, requires_pg, unique_name

FIXTURES = Path(__file__).parent / "fixtures" / "run_file"
TOOL_TABLE = "zz_06_tool_tbl"  # the table name the tool fixtures were written with
SLEEP = {  # a statement that runs until it is cancelled
    "postgres": "SELECT pg_sleep(30)", "mysql": "SELECT SLEEP(30)", "mssql": "WAITFOR DELAY '00:00:30'",
    "sqlite": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1000000000) "
              "SELECT count(*) FROM n",
}


@pytest.fixture(params=ENGINES)
def env(request, tmp_path):
    db = scratch_db(request.param, tmp_path)
    app = DataApp(tmp_path)
    try:
        yield db, app, app.add_profile(db.profile())
    finally:
        app.close()
        db.close()


def write(tmp_path: Path, db: ScratchDb, statements: list[str], name: str = "run.sql") -> Path:
    """`statements` as one file of the engine: `;` after each, or a GO line after each T-SQL batch."""
    path = tmp_path / name
    path.write_text(script_text(db.dialect, statements) + "\n", encoding="utf-8")
    return path


def make_table(db: ScratchDb, made: list) -> str:
    name = scratch_name("run")
    made.append(("TABLE", name))
    db.execute(f"CREATE TABLE {db.table(name)} (id INT PRIMARY KEY, label VARCHAR(50)){db.table_options}")
    return name


def inserts(db: ScratchDb, table: str, *ids: int) -> list[str]:
    return [f"INSERT INTO {db.table(table)} VALUES ({i}, 'row {i}')" for i in ids]


def ids(db: ScratchDb, table: str) -> list[int]:
    return [row[0] for row in db.rows(f"SELECT id FROM {db.table(table)} ORDER BY id")]


def command(path: Path, mode: str, **fields):
    return lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode, **fields)


def line_of(path: Path, text: str) -> int:
    return next(i for i, line in enumerate(path.read_text().splitlines(), start=1) if text in line)


def test_all_or_nothing_applies_the_whole_file_and_records_it(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 3))
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.outcome.rows, ran.error) == ("completed", 3, 3, None)
        assert ids(db, table) == [1, 2, 3]
        [record] = app.history()
        assert (record.status, record.rows) == ("ok", 3)
        assert record.sql.startswith(f"-- Run SQL file {path} (0.0 MB), all or nothing")


def test_all_or_nothing_an_error_applies_nothing(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 1, 4))  # the second 1 is a duplicate key
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.outcome.stopped_at.statement) == ("rolled_back", 3)
        assert ran.outcome.stopped_at.line == (5 if db.dialect is Dialect.TSQL else 3)  # T-SQL: a GO line after each
        assert ran.error is not None and ids(db, table) == []
        assert app.history()[0].status == "error"


def test_statement_by_statement_stops_or_skips(env, tmp_path):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, inserts(db, table, 1, 2, 1, 4))
        tab = app.job_session(profile)
        stopped = tab.run(command(path, "statements"))
        assert (stopped.outcome.outcome, stopped.outcome.statements, stopped.outcome.stopped_at.statement) == (
            "stopped", 2, 3)
        assert ids(db, table) == [1, 2]
        again = tab.run(command(path, "statements", from_statement=4))  # "run again from statement 4"
        assert (again.outcome.outcome, again.outcome.statements) == ("completed", 1)
        assert ids(db, table) == [1, 2, 4]
        db.execute(f"DELETE FROM {db.table(table)}")
        skipped = tab.run(command(path, "statements", on_error="continue"))
        assert (skipped.outcome.outcome, skipped.outcome.statements, skipped.outcome.error_count) == ("completed", 3, 1)
        assert skipped.outcome.errors[0].statement == 3 and ids(db, table) == [1, 2, 4]


@pytest.mark.parametrize("mode", ["all_or_nothing", "statements"])
def test_a_cancel_rolls_back_or_stops_where_it_reached(env, tmp_path, mode):
    db, app, profile = env
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [*inserts(db, table, 1), SLEEP[db.engine], *inserts(db, table, 3)])
        tab = app.job_session(profile)
        rid = tab.submit(command(path, mode))
        tab.wait_for_progress(rid, lambda p: p.statements == 1)
        time.sleep(0.5)  # the long statement has started
        tab.worker.cancel(rid)
        ran = tab.wait(rid)
        expected = "rolled_back" if mode == "all_or_nothing" else "stopped"
        assert (ran.outcome.outcome, ran.outcome.cancelled, ran.outcome.stopped_at.statement) == (expected, True, 2)
        assert ids(db, table) == ([] if mode == "all_or_nothing" else [1])


def test_the_files_own_commit_stops_an_all_or_nothing_run(env, tmp_path):
    db, app, profile = env
    commit = "COMMIT TRANSACTION" if db.dialect is Dialect.TSQL else "COMMIT"
    with dropping(db, app.close_sessions) as made:
        table = make_table(db, made)
        path = write(tmp_path, db, [*inserts(db, table, 1), commit, *inserts(db, table, 2)])
        ran = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (ran.outcome.outcome, ran.error.code) == ("rolled_back", "transaction_in_file")
        assert ids(db, table) == []


def test_a_read_only_connection_is_refused_before_anything(env, tmp_path):
    db, app, _ = env
    profile = app.add_profile(db.profile(read_only=True))
    path = write(tmp_path, db, ["SELECT 1"])
    with pytest.raises(ReadOnlyProfile):
        app.inspect(profile, path)
    ran = app.job_session(profile).run(command(path, "statements"))
    assert (ran.outcome.outcome, ran.error.code) == ("not_started", "read_only")


@requires_mysql
def test_mysql_ddl_commits_an_all_or_nothing_run_as_partial(tmp_path):
    db = scratch_db("mysql", tmp_path)
    app = DataApp(tmp_path)
    profile = app.add_profile(db.profile())
    try:
        with dropping(db, app.close_sessions) as made:
            table = make_table(db, made)
            other = scratch_name("ddl")
            made.append(("TABLE", other))
            path = write(tmp_path, db, [*inserts(db, table, 1), f"CREATE TABLE {db.table(other)} (a INT)",
                                        *inserts(db, table, 2)])
            ran = app.job_session(profile).run(command(path, "all_or_nothing"))
            assert (ran.outcome.outcome, ran.outcome.stopped_at.statement, ran.error.code) == (
                "partial", 2, "transaction_ended")
            assert ids(db, table) == [1] and db.exists(other)  # M8: the INSERT before the DDL is committed
    finally:
        app.close()
        db.close()


# --- files other tools wrote (A8) -------------------------------------------------------------------------------


def tool_file(tmp_path: Path, fixture: str, table: str) -> Path:
    path = tmp_path / fixture
    path.write_bytes((FIXTURES / fixture).read_bytes().replace(TOOL_TABLE.encode(), table.encode()))
    return path


@pytest.fixture
def tool_env(tmp_path):
    made_envs = []

    def make(engine: str):
        db = scratch_db(engine, tmp_path)
        app = DataApp(tmp_path)
        made_envs.append((db, app))
        return db, app, app.add_profile(db.profile())

    yield make
    for db, app in made_envs:
        app.close()
        db.close()


@requires_pg
def test_a_pg_dump_inserts_file_runs_and_its_copy_form_stops_at_copy(tool_env, tmp_path):
    db, app, profile = tool_env("postgres")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "pg_dump_inserts.sql", table)
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert [m for m in ran.messages if m.startswith("Skipped line")] == [
            f"Skipped line {line_of(path, '\\restrict')}: \\restrict is a psql command",
            f"Skipped line {line_of(path, '\\unrestrict')}: \\unrestrict is a psql command"]
        assert db.rows(f"SELECT id, label, data, amount::text FROM {db.table(table)} ORDER BY id") == [
            (1, "it's", b"\x00\xff", "12345678901234567890.1234567890"), (2, "two\nlines", None, None),
            (3, "中文😀", b"\xde\xad\xbe\xef", "-1.5000000000")]
        assert db.rows(f"SELECT nextval('public.{table}_id_seq')") == [(4,)]
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "pg_dump_copy.sql", table)
        assert any("copies data from stdin" in w for w in app.inspect(profile, path).warnings)
        ran = app.job_session(profile).run(command(path, "statements", on_error="continue"))
        assert (ran.outcome.outcome, ran.error.code) == ("stopped", "unsupported_copy")
        assert ran.outcome.stopped_at.line == line_of(path, "COPY public.")
        assert "pg_dump: use --inserts, or run this file with psql" in ran.error.message


@requires_mysql
def test_a_mysqldump_file_runs_statement_by_statement(tool_env, tmp_path):
    db, app, profile = tool_env("mysql")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))  # its trigger goes with it
        ran = app.job_session(profile).run(command(tool_file(tmp_path, "mysqldump.sql", table), "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert db.rows(f"SELECT id, label, HEX(data), CAST(born AS CHAR), note FROM {db.table(table)} ORDER BY id") == [
            (1, "it's \\ back", "00FF", "2026-10-20", None), (2, "中文😀", None, None, "x;y")]
        assert db.rows("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'dbaguy' "
                       f"AND EVENT_OBJECT_TABLE = '{table}'") == [(1,)]
        raw = tool_file(tmp_path, "mysqldump_raw_binary.sql", unique_name("zz_06_raw"))
        assert any("not UTF-8" in w and "--hex-blob" in w for w in app.inspect(profile, raw).warnings)
        made.append(("TABLE", raw.read_bytes().split(b"`")[1].decode()))
        stopped = app.job_session(profile).run(command(raw, "statements"))
        assert (stopped.outcome.outcome, stopped.error.code) == ("stopped", "file_not_utf8")
        assert "--hex-blob" in stopped.error.message


def test_a_sqlite3_dump_runs_statement_by_statement_and_stops_all_or_nothing(tool_env, tmp_path):
    db, app, profile = tool_env("sqlite")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        path = tool_file(tmp_path, "sqlite3_dump.sql", table)
        refused = app.job_session(profile).run(command(path, "all_or_nothing"))
        assert (refused.outcome.outcome, refused.error.code) == ("rolled_back", "transaction_in_file")
        assert not db.exists(table)
        ran = app.job_session(profile).run(command(path, "statements"))
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert db.rows(f'SELECT id, label, data, v FROM "{table}" ORDER BY id') == [
            (1, "it's", b"\x00\xff", "007"), (2, "two\nlines", None, 1.5), (3, "中文😀", None, 0)]


@requires_mssql
def test_an_ssms_style_script_runs(tool_env, tmp_path):
    db, app, profile = tool_env("mssql")
    with dropping(db, app.close_sessions) as made:
        table = unique_name("zz_06_tool")
        made.append(("TABLE", table))
        ran = app.job_session(profile).run(command(tool_file(tmp_path, "ssms_script.sql", table), "statements"))
        assert (ran.outcome.outcome, ran.error, ran.outcome.statements) == ("completed", None, 10)
        assert db.rows(f"SELECT id, label, data FROM {db.table(table)} ORDER BY id") == [
            (1, "it's", b"\x00\xff"), (2, "中文😀", None)]
        assert db.rows(f"SELECT IDENT_CURRENT('dbo.{table}')")[0][0] == 2


@requires_couchbase
def test_couchbase_runs_a_file_statement_by_statement_only(tmp_path):
    app = DataApp(tmp_path)
    profile = make_couchbase_profile()
    body = {"profile": profile.model_dump(mode="json", exclude={"id"}), "secrets": {"password": COUCHBASE_PASSWORD}}
    profile_id = app.add_profile(body)
    key = unique_name("zz_06_run")
    path = tmp_path / "docs.sqlpp"
    path.write_text(
        f'UPSERT INTO orders (KEY, VALUE) VALUES ("{key}", {{"n": 1}});\n'
        f'UPDATE orders USE KEYS "{key}" SET n = 2;\n'
        f'SELECT RAW n FROM orders USE KEYS "{key}";\n'
        f'DELETE FROM orders USE KEYS "{key}";\n', encoding="utf-8")
    context = QueryContext(bucket="dbaguy", scope="app")
    try:
        tab = app.job_session(profile_id)
        refused = tab.run(command(path, "all_or_nothing", query_context=context))
        assert (refused.outcome.outcome, refused.error.code) == ("not_started", "not_supported")
        ran = tab.run(command(path, "statements", query_context=context))
        assert (ran.outcome.outcome, ran.outcome.statements, ran.error) == ("completed", 4, None)
    finally:
        couchbase_query(f'DELETE FROM dbaguy.app.orders USE KEYS "{key}"')
        app.close()
    assert couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.orders")["results"] == [500]
```

Create `backend/tests/test_run_file_inspect.py`:

```python
"""Run SQL file's inspection (spec ⑥ §3.2): what the dialog learns from a file before it runs, without connecting:
size, first lines, a DbaGuy dump's header (a hint, never a permission) and the warnings. Pure (a SQLite profile; no
database is opened)."""

import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.errors import FileNotFound, ReadOnlyProfile
from dbaguy_backend.domain.profiles import Engine, ProfileUpsertRequest
from dbaguy_backend.services.data_service import PREVIEW_LINES
from dbaguy_backend.sql.dump_header import header_lines, parse_header
from dbaguy_backend.storage.secret_store import InMemorySecretStore


@pytest.fixture
def container(tmp_path):
    settings = Settings(token="t", data_dir=tmp_path / "data", log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore())


def profile(container, tmp_path, **fields):
    body = {"profile": {"engine": "sqlite", "name": "local", "path": str(tmp_path / "x.db"), **fields}}
    return container.profiles.create(ProfileUpsertRequest.model_validate(body)).profile.id


def header(engine=Engine.POSTGRES, drop_first=False) -> str:
    return "\n".join(header_lines(
        engine=engine, server="PostgreSQL 17.11", scope="database dbaguy · schema public",
        created="2026-10-20T12:34:56+08:00", content="schema and data · 2 tables", snapshot="one consistent snapshot",
        not_included=["owners and privileges"], drop_first=drop_first,
    )) + "\n"


def test_a_header_is_read_back_as_written():
    found = parse_header("﻿" + header(drop_first=True) + "SET x = 1;\n-- Options: not a header line\n")
    assert (found.format, found.engine, found.server, found.scope, found.drop_first) == (
        1, Engine.POSTGRES, "PostgreSQL 17.11", "database dbaguy · schema public", True)
    assert len(found.lines) == 7 and found.lines[0] == "-- DbaGuy SQL dump · format 1"
    assert parse_header("-- MySQL dump 10.13\n") is None
    assert parse_header("-- DbaGuy SQL dump · format 1\n-- Engine: oracle · x · y\n") is None


def test_the_inspection_of_a_dbaguy_dump_written_for_another_engine(container, tmp_path):
    path = tmp_path / "d.sql"
    path.write_text(header(drop_first=True) + "CREATE TABLE t (a int);\n")
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert (found.size, found.header.engine, found.preview_truncated) == (path.stat().st_size, Engine.POSTGRES, False)
    assert found.preview.endswith("CREATE TABLE t (a int);\n")
    assert found.warnings == ["Written for PostgreSQL, the target is SQLite", "This dump drops and recreates its objects"]


def test_the_preview_is_the_first_lines_and_says_when_there_is_more(container, tmp_path):
    path = tmp_path / "long.sql"
    path.write_text("".join(f"INSERT INTO t VALUES ({i});\n" for i in range(PREVIEW_LINES + 50)))
    found = container.data.inspect(profile(container, tmp_path), str(path))
    assert found.preview.count("\n") == PREVIEW_LINES and found.preview_truncated and found.header is None


def test_what_the_inspection_warns_about(container, tmp_path):
    pid = profile(container, tmp_path)
    empty = tmp_path / "empty.sql"
    empty.write_bytes(b"")
    assert container.data.inspect(pid, str(empty)).warnings == ["The file is empty"]
    latin = tmp_path / "latin.sql"
    latin.write_bytes(b"SELECT 1;\nSELECT '\xe9';\n")
    found = container.data.inspect(pid, str(latin))
    assert found.warnings == ["Line 2 is not UTF-8 text: a run stops there"] and "�" in found.preview
    cut = tmp_path / "cut.sql"  # a character cut by the preview's byte limit is not an error
    cut.write_bytes(b"-- " + b"x" * (64 * 1024 - 4) + "中".encode() + b"\n")
    assert container.data.inspect(pid, str(cut)).warnings == []


def test_a_missing_file_or_a_read_only_connection_is_refused(container, tmp_path):
    with pytest.raises(FileNotFound):
        container.data.inspect(profile(container, tmp_path), str(tmp_path / "missing.sql"))
    with pytest.raises(FileNotFound):
        container.data.inspect(profile(container, tmp_path), "relative.sql")
    path = tmp_path / "x.sql"
    path.write_text("SELECT 1;\n")
    with pytest.raises(ReadOnlyProfile):
        container.data.inspect(profile(container, tmp_path, read_only=True), str(path))


def test_the_inspect_route(container, tmp_path):
    path = tmp_path / "x.sql"
    path.write_text("SELECT 1;\n")
    pid = str(profile(container, tmp_path))
    settings = container.settings
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
        answer = client.post("/data/run-file/inspect", json={"profile_id": pid, "path": str(path)}, headers=auth)
        assert (answer.status_code, answer.json()["preview"]) == (200, "SELECT 1;\n")
        missing = client.post("/data/run-file/inspect", json={"profile_id": pid, "path": "/nope.sql"}, headers=auth)
        assert (missing.status_code, missing.json()["error"]["code"], missing.json()["error"]["message"]) == (
            404, "file_not_found", "/nope.sql is not a file")
        engines = {e["engine"]: e["run_file"] for e in client.get("/system/engines", headers=auth).json()}
        assert engines == {"postgres": True, "mysql": True, "mssql": True, "couchbase": True, "sqlite": True}
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_run_file_job.py tests/test_run_file.py tests/test_run_file_inspect.py`
Expected: collection errors (`RunFileCommand`, `dbaguy_backend.sessions.jobs.run_file` and `dbaguy_backend.sql.dump_header` do not exist yet).

- [ ] **Step 4: The capability, the command and the errors**

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
    row_editing: bool = False  # a table-data grid can be edited (spec ⑤): the connection is RowEditing
    # A table-data tab's first browse waits for the edit target and sorts by its `stable_order` (PostgreSQL, 05b K13)
    orders_browse_by_key: bool = False


@dataclass(frozen=True)
```

with:

```python
    row_editing: bool = False  # a table-data grid can be edited (spec ⑤): the connection is RowEditing
    # A table-data tab's first browse waits for the edit target and sorts by its `stable_order` (PostgreSQL, 05b K13)
    orders_browse_by_key: bool = False
    run_file: bool = False  # Run SQL file (spec ⑥ R-06-1, D-06-13)


@dataclass(frozen=True)
```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
            cancel_aborts_transaction=caps.cancel_aborts_transaction, server_read_only=caps.server_read_only,
            transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
            documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
            row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
```

with:

```python
            cancel_aborts_transaction=caps.cancel_aborts_transaction, server_read_only=caps.server_read_only,
            transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
            documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
            row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key, run_file=caps.run_file,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
```

Append to `backend/dbaguy_backend/domain/profiles.py`:

```python
    run_file: bool = False  # Run SQL file (spec ⑥ D-06-13)
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    dialect = Dialect.POSTGRES
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

with:

```python
    dialect = Dialect.POSTGRES
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
        run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
    default_port = 3306
    dialect = Dialect.MYSQL
    # one connection sees every database; KILL QUERY keeps the transaction
    capabilities = AdapterCapabilities(row_editing=True)

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)
```

with:

```python
    default_port = 3306
    dialect = Dialect.MYSQL
    # one connection sees every database; KILL QUERY keeps the transaction
    capabilities = AdapterCapabilities(row_editing=True, run_file=True)

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

with:

```python
    # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
        run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

In `backend/dbaguy_backend/adapters/couchbase/adapter.py`, replace:

```python
    capabilities = AdapterCapabilities(
        server_read_only=True, transactions=False, query_context=True, parameters=True, documents=True,
        executed_plan=True, plan_format="couchbase", paused_result_max_idle_s=PAUSED_RESULT_MAX_IDLE_S,
        read_ahead_bytes=READ_AHEAD_BYTES,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

with:

```python
    capabilities = AdapterCapabilities(
        server_read_only=True, transactions=False, query_context=True, parameters=True, documents=True,
        executed_plan=True, plan_format="couchbase", paused_result_max_idle_s=PAUSED_RESULT_MAX_IDLE_S,
        read_ahead_bytes=READ_AHEAD_BYTES, run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

SQLite's connections stop caching statements (S5). A run of a dump sends 1,000 different INSERTs of up to 1 MiB, and sqlite3's default cache of 128 prepared statements held 111 MB after 200 of them (4 MB without it, measured while planning). A query tab loses nothing that matters: its statements are rarely repeated texts.

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
from . import schema as sqlite_schema

_PEEK_ROWS = 200
_PROGRESS_OPS = 10_000  # VM instructions between two looks at the cancel flag: ~0.3% slower, stops within ~0.1 ms
# Statements that control the transaction themselves or cannot run inside one: manual mode does not BEGIN for them.
_NO_AUTO_BEGIN = frozenset(
```

with:

```python
from . import schema as sqlite_schema

_PEEK_ROWS = 200
# sqlite3 keeps this many compiled statements per connection (128 by default). Run SQL file's multi-row INSERTs
# never repeat, and 128 compiled 1,000-row INSERTs held 300 MB (⑥ A10): statements are compiled each time instead
_STATEMENT_CACHE = 0
_PROGRESS_OPS = 10_000  # VM instructions between two looks at the cancel flag: ~0.3% slower, stops within ~0.1 ms
# Statements that control the transaction themselves or cannot run inside one: manual mode does not BEGIN for them.
_NO_AUTO_BEGIN = frozenset(
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE
    capabilities = AdapterCapabilities(row_editing=True)

    def parse_url(self, url: str) -> ParsedUrl:
        """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
```

with:

```python
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE
    capabilities = AdapterCapabilities(row_editing=True, run_file=True)

    def parse_url(self, url: str) -> ParsedUrl:
        """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
        uri = f"file:{quote(str(path))}?mode={mode}"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails for non-database files
        except sqlite3.Error as exc:
```

with:

```python
        uri = f"file:{quote(str(path))}?mode={mode}"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True,
                                   cached_statements=_STATEMENT_CACHE)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails for non-database files
        except sqlite3.Error as exc:
```

In `backend/dbaguy_backend/domain/errors.py`, replace:

```python
    """The export or copy format cannot be produced for this engine's dialect."""

    code = "unsupported_format"


class NotEditable(ValidationFailed):
```

with:

```python
    """The export or copy format cannot be produced for this engine's dialect."""

    code = "unsupported_format"


class NotSupported(ValidationFailed):
    """The engine has no such data tool (spec ⑥ D-06-13): `<Engine> connections cannot …`."""

    code = "not_supported"


class FileNotFound(NotFound):
    """A path that names no regular file (spec ⑥ §4): `<path> is not a file`."""

    code = "file_not_found"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path} is not a file")


class NotEditable(ValidationFailed):
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

with:

```python
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import JobCommand, JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python
    plan_id: str = Field(min_length=1, max_length=64)


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand,
    Field(discriminator="type"),
]

```

with:

```python
    plan_id: str = Field(min_length=1, max_length=64)


class RunFileCommand(JobCommand):
    """Run a SQL file on this session, a statement at a time (spec ⑥ §3.4): all of it in one transaction, or each
    statement on its own (stopping at an error, or skipping it). Statements before `from_statement` are read, never
    sent."""

    job_kind = "run_file"
    type: Literal["run_file"] = "run_file"
    path: str = Field(min_length=1, max_length=4096)  # absolute
    mode: Literal["all_or_nothing", "statements"]
    on_error: Literal["stop", "continue"] = "stop"  # continue: statement by statement only (R-06-7)
    from_statement: int = Field(default=1, ge=1)
    search_path: str | None = Field(default=None, min_length=1, max_length=1024)  # PostgreSQL: one schema
    query_context: QueryContext | None = None  # Couchbase

    @model_validator(mode="after")
    def _continue_only_statement_by_statement(self) -> Self:
        if self.mode == "all_or_nothing" and self.on_error == "continue":
            raise ValueError("an all-or-nothing run stops at the first error: skip errors statement by statement")
        return self


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand,
    Field(discriminator="type"),
]

```

In `backend/dbaguy_backend/domain/data_jobs.py`, replace:

```python
from uuid import UUID

from pydantic import BaseModel

JobKind = Literal["dump", "run_file"]
# R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
```

with:

```python
from uuid import UUID

from pydantic import BaseModel

from .profiles import Engine

JobKind = Literal["dump", "run_file"]
# R-06-12. Dump: completed or failed (nothing written). Run, all or nothing: completed, rolled_back, partial (the file
```

In `backend/dbaguy_backend/domain/data_jobs.py`, replace:

```python
MAX_JOB_MESSAGES = 200  # D-06-11: server messages (NOTICE, PRINT) forwarded per job; the rest are only counted
PROGRESS_INTERVAL_S = 0.25  # D-06-11: at most one job_progress this often (and at each object's start and end)
MAX_JOBS_LISTED = 200  # the job list keeps this many; the oldest finished ones go first


class JobCommand(BaseModel):
```

with:

```python
MAX_JOB_MESSAGES = 200  # D-06-11: server messages (NOTICE, PRINT) forwarded per job; the rest are only counted
PROGRESS_INTERVAL_S = 0.25  # D-06-11: at most one job_progress this often (and at each object's start and end)
MAX_JOBS_LISTED = 200  # the job list keeps this many; the oldest finished ones go first
ENGINE_NAMES: dict[Engine, str] = {  # what messages call an engine (the UI's ENGINE_LABELS)
    Engine.POSTGRES: "PostgreSQL", Engine.MYSQL: "MySQL", Engine.MSSQL: "SQL Server", Engine.SQLITE: "SQLite",
    Engine.COUCHBASE: "Couchbase",
}


class JobCommand(BaseModel):
```

Append to `backend/dbaguy_backend/domain/data_jobs.py`:

```python


class DumpHeader(BaseModel):
    """The first lines of a dump DbaGuy wrote (spec ⑥ §3.3.2), as Run SQL file's inspection reads them back: a hint
    for the dialog, never a permission."""

    format: int
    engine: Engine
    server: str | None = None  # `PostgreSQL 17.11`
    scope: str | None = None  # `database dbaguy · schema public`
    created: str | None = None
    drop_first: bool = False
    lines: list[str]  # the header's comment lines, as written


class FileInspection(BaseModel):
    """What Run SQL file's dialog shows of a file before it runs (POST /data/run-file/inspect)."""

    path: str
    size: int  # bytes
    header: DumpHeader | None = None  # a DbaGuy dump's header
    preview: str  # its first PREVIEW_LINES lines, at most PREVIEW_BYTES
    preview_truncated: bool
    warnings: list[str] = []
```

- [ ] **Step 5: The header format**

Create `backend/dbaguy_backend/sql/dump_header.py`:

```python
"""The header of a DbaGuy dump (spec ⑥ §3.3.2): written by the dump, read back by Run SQL file's inspection (one
format, defined here once). Pure.

    -- DbaGuy SQL dump · format 1
    -- Engine: postgres · PostgreSQL 17.11 · database dbaguy · schema public
    -- Created: 2026-10-20T12:34:56+08:00
    -- Content: schema and data · 12 tables, 2 views
    -- Snapshot: one consistent snapshot (REPEATABLE READ)
    -- Not included: owners and privileges · 1 extension's objects
    -- Options: drop objects first: no
"""

import re

from ..domain.data_jobs import DumpHeader
from ..domain.profiles import Engine

FORMAT = 1
_FIRST = re.compile(r"-- DbaGuy SQL dump · format (\d+)")
_FIELD = re.compile(r"-- ([A-Z][A-Za-z ]*): (.*)")
_SEPARATOR = " · "
_DROP_FIRST = "drop objects first: "


def header_lines(
    *, engine: Engine, server: str, scope: str, created: str, content: str, snapshot: str, not_included: list[str],
    drop_first: bool,
) -> list[str]:
    """The header's comment lines, without line breaks."""
    return [
        f"-- DbaGuy SQL dump · format {FORMAT}",
        f"-- Engine: {engine.value}{_SEPARATOR}{server}{_SEPARATOR}{scope}",
        f"-- Created: {created}",
        f"-- Content: {content}",
        f"-- Snapshot: {snapshot}",
        f"-- Not included: {_SEPARATOR.join(not_included)}",
        f"-- Options: {_DROP_FIRST}{'yes' if drop_first else 'no'}",
    ]


def parse_header(text: str) -> DumpHeader | None:
    """The header at the start of `text` (a file's first lines), or None when the file does not start with one."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or (first := _FIRST.fullmatch(lines[0].rstrip())) is None:
        return None
    fields: dict[str, str] = {}
    kept = [lines[0].rstrip()]
    for line in lines[1:]:
        found = _FIELD.fullmatch(line.rstrip())
        if found is None:
            break
        kept.append(line.rstrip())
        fields[found.group(1)] = found.group(2)
    engine, _, rest = fields.get("Engine", "").partition(_SEPARATOR)
    if engine not in {member.value for member in Engine}:
        return None
    server, _, scope = rest.partition(_SEPARATOR)
    return DumpHeader(
        format=int(first.group(1)), engine=Engine(engine), server=server or None, scope=scope or None,
        created=fields.get("Created"), drop_first=fields.get("Options", "").endswith(f"{_DROP_FIRST}yes"), lines=kept,
    )
```

- [ ] **Step 6: The run, the inspection and the route**

The outcome table (R-06-12), as `_Run` implements it:

| Mode | How it ends | Outcome |
|---|---|---|
| any | the search path or BEGIN fails; read-only before anything; `transaction_open` | `not_started` (rolled back) |
| all or nothing | every statement ran, COMMIT answered, no transaction left | `completed` |
| all or nothing | a statement fails, a Cancel, the file's own transaction statement, `copy`, a psql command, an unreadable line, `transaction_failed()` before COMMIT, a refused COMMIT | `rolled_back` (`cancelled` for a Cancel) |
| all or nothing | `in_transaction()` false after a statement, or before COMMIT | `partial` (`transaction_ended`) |
| all or nothing | the link died during COMMIT; COMMIT returned with a transaction still open (rolled back) | `unknown` |
| statements | read to the end (errors skipped are counted; a transaction the file left open is rolled back and said, S14) | `completed` |
| statements | a statement fails (stop), a Cancel, `copy`, a psql command, an unreadable line | `stopped` at that line and statement |
| statements | the link died while a statement ran | `unknown` |

Create `backend/dbaguy_backend/sessions/jobs/run_file.py`:

```python
"""Run SQL file (spec ⑥ §3.4): a file read as statements (`StatementStream`) and sent one at a time on the job's own
session. All or nothing: the dialect's transaction frame around everything, and any stop before the COMMIT rolls it
back. Statement by statement: each takes effect on its own; an error stops the run or is skipped. Every way a run ends
is classified here (R-06-12) and nothing is ever sent twice.

Not classified (R-06-6): the user confirmed the whole file, so each statement passes only the read-only gate
(`gate_confirmed`). The file's own transaction statements are refused in an all-or-nothing run (a cheap first-keyword
look, then the classifier's table for those few), and a transaction that ends anyway (MySQL commits around DDL, a
COMMIT inside a T-SQL batch) stops the run as `partial`."""

from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from ...adapters.base import RunOptions, StatementCancelled, StatementError
from ...domain.data_jobs import MAX_JOB_ERRORS, JobError, StoppedAt
from ...domain.errors import READ_ONLY_MESSAGE
from ...domain.execution import MessageSeverity, RunFileCommand
from ...services.safety import GuardAction
from ...sql.classifier import is_transaction_statement
from ...sql.dialects import Dialect, quote_ident, transaction_frame
from ...sql.lexer import first_keyword
from ...sql.statement_stream import FILE_NOT_UTF8, FileStatement, StatementStream, StreamError
from .context import Job, JobContext, JobReport

CONNECTION_LOST = "connection_lost"
UNSUPPORTED_COPY = "unsupported_copy"
TRANSACTION_IN_FILE = "transaction_in_file"
TRANSACTION_ENDED = "transaction_ended"
PSQL_META_COMMAND = "psql_meta_command"
FILE_IO = "file_io"
COPY_HINT = "pg_dump: use --inserts, or run this file with psql"
HEX_BLOB_HINT = "mysqldump writes binary columns as raw bytes unless it is given --hex-blob"
ROLLED_BACK = "The transaction was rolled back: nothing was applied"
# psql meta-commands pg_dump 17 writes around its plain output (A P8): skipped, with a message
_SKIPPED_META = frozenset({"\\restrict", "\\unrestrict"})
# The first words of a transaction statement in any dialect: only a statement that starts with one is looked at closer
_TRANSACTION_WORDS = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "SAVE"})
_FATAL = frozenset({UNSUPPORTED_COPY, PSQL_META_COMMAND, "read_only"})  # a skip-and-continue run stops at these too


class _Stop(Exception):
    """The run stops at `statement` (it was not sent, failed, or was cancelled): why, and whether it may have run."""

    def __init__(self, error: StatementError, statement: FileStatement | None, *, ran: bool = False) -> None:
        super().__init__(error.message)
        self.error = error
        self.statement = statement
        self.ran = ran


def summary(command: RunFileCommand, size: int) -> str:
    """The History record's text (D-06-9): what ran, never the file's statements."""
    mode = "all or nothing" if command.mode == "all_or_nothing" else (
        "statement by statement, " + ("skipping errors" if command.on_error == "continue" else "stopping at an error"))
    start = f", from statement {command.from_statement:,}" if command.from_statement > 1 else ""
    return f"-- Run SQL file {command.path} ({size / 1_048_576:.1f} MB), {mode}{start}"


class RunFileJob(Job):
    kind = "run_file"

    def __init__(self, command: RunFileCommand, connection_name: str) -> None:
        self._command = command
        self._name = connection_name

    @property
    def title(self) -> str:
        return f"Run {Path(self._command.path).name} on {self._name}"

    def run(self, ctx: JobContext) -> JobReport:
        command = self._command
        path = self._check(ctx)
        size = path.stat().st_size
        with path.open("rb") as file:
            ctx.progress.stage, ctx.progress.total_bytes = "statements", size
            ctx.begin_record(summary(command, size))
            logger.info("[{}] run of {} ({:,} bytes) {}, from statement {}", self._name, path, size, command.mode,
                        command.from_statement)
            return _Run(ctx, command).go(StatementStream(file.read, ctx.dialect))

    def _check(self, ctx: JobContext) -> Path:
        """What refuses the run before anything is sent: the job then never started."""
        command = self._command
        path = Path(command.path).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise StatementError(f"{command.path} is not a file", code="file_not_found")
        if command.mode == "all_or_nothing" and not ctx.capabilities.transactions:
            raise StatementError("This connection has no transactions: run the file statement by statement",
                                 code="not_supported")
        if command.query_context is not None and not ctx.capabilities.query_context:
            raise StatementError("This engine does not take a query context", code="unsupported_option")
        if command.search_path is not None and ctx.dialect is not Dialect.POSTGRES:
            raise StatementError("Only PostgreSQL takes a search path", code="unsupported_option")
        if ctx.hooks.read_only():
            raise StatementError(READ_ONLY_MESSAGE, code="read_only")
        return path


class _Run:
    """One run's state and the outcome table of R-06-12."""

    def __init__(self, ctx: JobContext, command: RunFileCommand) -> None:
        self.ctx = ctx
        self.command = command
        self.all_or_nothing = command.mode == "all_or_nothing"
        self.options = RunOptions(describe=False, query_context=command.query_context)
        self.done = 0  # statements sent and finished
        self.rows = 0
        self.bytes = 0
        self.errors: list[JobError] = []
        self.error_count = 0
        self.read = 0  # the number of the last statement read
        self.last_line = 0  # the line of the last statement read

    def go(self, stream: StatementStream) -> JobReport:
        try:
            self._begin()
            for statement in self._statements(stream):
                self._one(statement)
        except _Stop as stop:
            return self._stopped(stop)
        if self.all_or_nothing:
            return self._commit()
        return self._statements_done()

    # ---- the statements ----

    def _begin(self) -> None:
        """The search path, then (all or nothing) the transaction: a failure here applied nothing of the file."""
        setup = []
        if self.command.search_path is not None:
            setup.append(f"SET search_path TO {quote_ident(Dialect.POSTGRES, self.command.search_path)}")
        if self.all_or_nothing:
            setup.append(transaction_frame(self.ctx.dialect).begin)
        for sql in setup:
            try:
                self.ctx.drain(self.ctx.run(sql, self.options))
            except StatementError as exc:
                raise _Stop(exc, None) from None

    def _statements(self, stream: StatementStream) -> Iterator[FileStatement]:
        """The file's statements, from `from_statement` on; a file that cannot be read on stops the run there."""
        statements = iter(stream)
        while True:
            try:
                statement = next(statements)
            except StopIteration:
                return
            except StreamError as exc:
                message = exc.message
                if exc.code == FILE_NOT_UTF8 and self.ctx.dialect is Dialect.MYSQL:
                    message += f" ({HEX_BLOB_HINT})"
                raise _Stop(StatementError(message, code=exc.code), self._at(exc.line)) from None
            except OSError as exc:
                error = StatementError(f"Cannot read {self.command.path}: {exc.strerror or exc}", code=FILE_IO)
                raise _Stop(error, self._at(self.last_line + 1)) from None
            self.last_line = statement.line
            if statement.meta:
                if self.read + 1 >= self.command.from_statement:  # a psql line before the start was dealt with then
                    self._meta(statement)
                continue
            self.read = statement.index
            if statement.index >= self.command.from_statement:
                yield statement
            else:  # read, never sent: only so that what follows is split as the whole file splits it
                self.bytes = self.ctx.progress.bytes = statement.end_byte
                self.ctx.progress.line = statement.line
                self.ctx.tick()

    def _at(self, line: int) -> FileStatement:
        """Where a run stops that has no statement of its own (a line that is not text)."""
        return FileStatement(index=self.read + 1, line=line, text="", end_byte=self.bytes)

    def _meta(self, statement: FileStatement) -> None:
        word = statement.text.split(None, 1)[0]
        if word in _SKIPPED_META:
            self.ctx.message(MessageSeverity.INFO, f"Skipped line {statement.line:,}: {word} is a psql command")
            return
        error = StatementError(f"Line {statement.line:,} is a psql command ({word}): run this file with psql",
                               code=PSQL_META_COMMAND)
        raise _Stop(error, statement)

    def _one(self, statement: FileStatement) -> None:
        ctx = self.ctx
        try:
            ctx.raise_if_cancelled()  # a Cancel between statements: this one is never sent
            if (decision := ctx.hooks.gate(statement.text)).action != GuardAction.RUN:
                kind = decision.classification.kind
                raise StatementError(f"This connection is read-only: {kind} statements are not allowed",
                                     code="read_only")
            if self.all_or_nothing and self._controls_transaction(statement.text):
                raise StatementError(
                    f"The file controls its own transactions (line {statement.line:,}: "
                    f"{first_keyword(statement.text, ctx.dialect)}): run it statement by statement",
                    code=TRANSACTION_IN_FILE)
        except StatementError as exc:
            raise _Stop(exc, statement) from None
        for _ in range(statement.repeat):  # T-SQL `GO n` (GO 0: never sent)
            try:
                affected = ctx.drain(ctx.run(statement.text, self.options))
            except StatementError as exc:
                self._failed(exc, statement)
                return
            self.rows += max(affected or 0, 0)
        self.done += 1
        self.bytes = statement.end_byte
        if self.all_or_nothing and not ctx.connection.in_transaction():
            why = "MySQL commits around DDL" if ctx.dialect is Dialect.MYSQL else "a statement in the file ended it"
            raise _Stop(StatementError(
                f"The transaction ended at line {statement.line:,} ({why}): the statements before it are applied; "
                "run the rest statement by statement", code=TRANSACTION_ENDED), statement, ran=True)
        self._progress(statement)

    def _controls_transaction(self, text: str) -> bool:
        return (first_keyword(text, self.ctx.dialect) in _TRANSACTION_WORDS
                and is_transaction_statement(text, self.ctx.dialect))

    def _failed(self, exc: StatementError, statement: FileStatement) -> None:
        """A statement that failed: the run stops, or (skip and continue) the error is kept and the run goes on."""
        if exc.code == UNSUPPORTED_COPY:
            exc = StatementError(f"{exc.message}\n{COPY_HINT}", code=UNSUPPORTED_COPY)
        lost = exc.code == CONNECTION_LOST
        if self.all_or_nothing or self.command.on_error == "stop" or lost or exc.code in _FATAL \
                or isinstance(exc, StatementCancelled):
            raise _Stop(exc, statement, ran=lost)
        logger.debug("[run] statement {} at line {} failed and was skipped: {}", statement.index, statement.line,
                     exc.message)
        self.error_count += 1
        if len(self.errors) < MAX_JOB_ERRORS:
            self.errors.append(JobError(line=statement.line, statement=statement.index, code=exc.code,
                                        message=exc.message))
        self.bytes = statement.end_byte
        self._progress(statement)

    def _progress(self, statement: FileStatement) -> None:
        progress = self.ctx.progress
        progress.statements, progress.line, progress.bytes, progress.rows = (
            self.done, statement.line, statement.end_byte, self.rows)
        progress.errors = self.error_count
        self.ctx.tick()

    # ---- the outcome ----

    def _report(self, outcome: str, error: StatementError | None = None, *, cancelled: bool = False,
                stopped: FileStatement | None = None, warnings: list[str] | None = None) -> JobReport:
        at = StoppedAt(line=stopped.line, statement=stopped.index) if stopped is not None else None
        return JobReport(
            self.ctx.outcome(outcome, cancelled=cancelled, rows=self.rows, bytes=self.bytes, statements=self.done,
                             path=self.command.path, errors=self.errors, error_count=self.error_count,
                             stopped_at=at, warnings=warnings or []),
            error, schema_changed=self.ctx.sent,
        )

    def _stopped(self, stop: _Stop) -> JobReport:
        error, statement = stop.error, stop.statement
        cancelled = isinstance(error, StatementCancelled)
        if statement is None:  # the search path or BEGIN failed: nothing of the file ran
            self._rollback()
            return self._report("not_started", error, cancelled=cancelled)
        if self.all_or_nothing:
            if error.code == TRANSACTION_ENDED:
                self._rollback()  # nothing after the statement that ended it is open, but never leave one behind
                return self._report("partial", error, stopped=statement)
            if error.code != CONNECTION_LOST:
                self._rollback()  # a lost link: the server rolled it back when the connection ended
            return self._report("rolled_back", error, cancelled=cancelled, stopped=statement)
        if stop.ran:  # the link died while this statement ran: it may have taken effect
            return self._report("unknown", error, stopped=statement)
        return self._report("stopped", error, cancelled=cancelled, stopped=statement)

    def _commit(self) -> JobReport:
        """COMMIT, once nothing stopped the run (⑤ §3.5 step 7): no Cancel reaches it, and an aborted, vanished or
        refused transaction is rolled back (or unknown when the answer to COMMIT was lost)."""
        ctx, frame = self.ctx, transaction_frame(self.ctx.dialect)
        ctx.progress.stage = "commit"
        ctx.tick(force=True)
        cancelled = ctx.stop_cancels()
        connection = ctx.connection
        try:
            if cancelled:
                self._rollback()
                return self._report("rolled_back", StatementCancelled(), cancelled=True)
            if connection.transaction_failed():
                self._rollback()
                return self._report("rolled_back", StatementError(ROLLED_BACK, code="transaction_failed"))
            if not connection.in_transaction():
                return self._report("partial", StatementError(
                    "The transaction ended before the COMMIT: what ran before that is applied", code=TRANSACTION_ENDED))
        except StatementError as exc:
            self._rollback()
            return self._report("rolled_back", exc)
        try:
            ctx.drain(ctx.run(frame.commit, self.options))
        except StatementError as exc:
            if exc.code == CONNECTION_LOST:
                return self._report("unknown", exc)
            self._rollback()  # SQLite keeps the transaction after a busy COMMIT (⑤ A14)
            return self._report("rolled_back", StatementError(f"{exc.message}\n{ROLLED_BACK}", code=exc.code))
        try:
            left_open = connection.in_transaction()
        except StatementError as exc:
            return self._report("unknown", exc)
        if left_open:  # nested @@TRANCOUNT, or MySQL completion_type=CHAIN: what COMMIT did cannot be told (⑤ G11)
            self._rollback()
            return self._report("unknown", StatementError(
                "COMMIT returned but a transaction is still open: it was rolled back; what was applied is unknown",
                code="commit_unconfirmed"))
        return self._report("completed")

    def _statements_done(self) -> JobReport:
        """Statement by statement, every statement read: a transaction the file left open is rolled back, as other
        clients do at the end of a file (said in the outcome, never committed on the file's behalf)."""
        warnings = []
        if self.read < self.command.from_statement:
            warnings.append(f"The file has {self.read:,} statements: none from statement "
                            f"{self.command.from_statement:,} on was run")
        try:
            open_transaction = self.ctx.connection.in_transaction()
        except StatementError:
            open_transaction = False
        if open_transaction:
            self._rollback()
            warnings.append("The file left a transaction open: it was rolled back, so the statements in it are not "
                            "applied")
        return self._report("completed", warnings=warnings)

    def _rollback(self) -> None:
        """End the run's transaction once it stopped; no Cancel may reach the ROLLBACK. A connection that is gone
        has none to end (the server rolled it back)."""
        self.ctx.stop_cancels()
        try:
            self.ctx.connection.rollback()
        except StatementError as exc:
            logger.info("[run] the rollback after a stopped run failed ({})", exc.code)
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import JobCommand
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile
from ..services.edit_plans import EditPlanStore
```

with:

```python
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import JobCommand
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.execution import RunFileCommand
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile
from ..services.edit_plans import EditPlanStore
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .worker import ExecutionRecord, WorkerHooks

READ_ONLY_RECHECK_S = 1.0  # a job's statement gate re-reads the profile at most this often (R-06-6)
```

with:

```python
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .jobs.run_file import RunFileJob
from .worker import ExecutionRecord, WorkerHooks

READ_ONLY_RECHECK_S = 1.0  # a job's statement gate re-reads the profile at most this often (R-06-6)
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python

    def build_job(self, command: JobCommand, profile: AnyProfile, database: str | None) -> Job:
        """The job object for a job command on a session of `profile` (the composition root of data tools' jobs)."""
        raise StatementError(f"{type(command).__name__} is not a job this backend runs", code="not_supported")
```

with:

```python

    def build_job(self, command: JobCommand, profile: AnyProfile, database: str | None) -> Job:
        """The job object for a job command on a session of `profile` (the composition root of data tools' jobs)."""
        capabilities = self._registry.get(profile.engine).capabilities
        match command:
            case RunFileCommand() if capabilities.run_file:
                return RunFileJob(command, profile.name)
        raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")
```

Create `backend/dbaguy_backend/services/data_service.py`:

```python
"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts. No job runs here, and Run SQL file's
inspection opens no connection at all (a read-only profile is refused first, R-06-6).

Logs: INFO the profile and the path of each inspection; never the file's contents."""

import re
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import ENGINE_NAMES, FileInspection
from ..domain.errors import FileNotFound, NotSupported, ReadOnlyProfile
from ..domain.profiles import AnyProfile, Engine
from ..sql.dump_header import parse_header
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
_COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
_HEX_BLOB = "mysqldump: use --hex-blob"


def _decoded(head: bytes) -> tuple[str, int | None]:
    """The head as text, and the 1-based line of its first byte that is not UTF-8 (None: it all is). A character cut
    at the end of the head is not an error."""
    try:
        return head.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 3 and exc.reason == "unexpected end of data":
            return head[:exc.start].decode("utf-8", "replace"), None
        return head.decode("utf-8", "replace"), head.count(b"\n", 0, exc.start) + 1


class DataService:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._profiles = profiles
        self._registry = registry

    def inspect(self, profile_id: UUID, path: str) -> FileInspection:
        """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
        the run will stop at or should be told (another engine's dump, objects dropped first, not UTF-8, COPY)."""
        profile, _ = self._admit(profile_id, "run SQL files", "run_file")
        if profile.read_only:
            raise ReadOnlyProfile()
        target = Path(path).expanduser()
        if not target.is_absolute() or not target.is_file():
            raise FileNotFound(path)
        size = target.stat().st_size
        with target.open("rb") as file:
            head = file.read(PREVIEW_BYTES)
        text, bad_line = _decoded(head)
        lines = text.splitlines(keepends=True)
        preview = "".join(lines[:PREVIEW_LINES])
        truncated = len(lines) > PREVIEW_LINES or size > len(head)
        header = parse_header(text)
        warnings = self._warnings(profile.engine, size, header, preview, bad_line)
        logger.info("Inspected {} ({:,} bytes) for a run on {}", target, size, profile.name)
        return FileInspection(path=str(target), size=size, header=header, preview=preview.lstrip("﻿"),
                              preview_truncated=truncated, warnings=warnings)

    @staticmethod
    def _warnings(engine: Engine, size: int, header, preview: str, bad_line: int | None) -> list[str]:
        warnings = []
        if size == 0:
            warnings.append("The file is empty")
        if header is not None and header.engine != engine:
            warnings.append(f"Written for {ENGINE_NAMES[header.engine]}, the target is {ENGINE_NAMES[engine]}")
        if header is not None and header.drop_first:
            warnings.append("This dump drops and recreates its objects")
        if bad_line is not None:
            hint = f" ({_HEX_BLOB})" if engine == Engine.MYSQL else ""
            warnings.append(f"Line {bad_line:,} is not UTF-8 text: a run stops there{hint}")
        if engine == Engine.POSTGRES:
            for number, line in enumerate(preview.splitlines(), start=1):
                if _COPY_FROM_STDIN.match(line):
                    warnings.append(f"Line {number:,} copies data from stdin, which only psql can do: a run stops "
                                    "there (pg_dump: use --inserts)")
                    break
        return warnings

    def _admit(self, profile_id: UUID, action: str, capability: str) -> tuple[AnyProfile, DatabaseAdapter]:
        profile = self._profiles.get(profile_id)
        adapter = self._registry.get(profile.engine)
        if not getattr(adapter.capabilities, capability):
            raise NotSupported(f"{ENGINE_NAMES[profile.engine]} connections cannot {action}")
        return profile, adapter
```

In `backend/dbaguy_backend/api/data.py`, replace:

```python
"""Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
own (dump, run_file); these routes find it again after the window reloads, and cancel it."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ..container import AppContainer
from ..domain.data_jobs import JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

```

with:

```python
"""Data tools (spec ⑥ §3.2): the jobs of this backend run. A job itself is a WebSocket command on a session of its
own (dump, run_file); these routes find it again after the window reloads, and cancel it."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.data_jobs import FileInspection, JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

```

In `backend/dbaguy_backend/api/data.py`, replace:

```python

    session_id: str
    request_id: str


@router.get("/jobs", response_model=list[JobInfo])
```

with:

```python

    session_id: str
    request_id: str


class RunFileInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID  # the connection the file would run on
    path: str = Field(min_length=1, max_length=4096)


@router.post("/run-file/inspect", response_model=FileInspection)
def inspect_run_file(body: RunFileInspectRequest, container: AppContainer = Depends(get_container)) -> FileInspection:
    """Run SQL file's dialog: the file's size, first lines and warnings. A read-only connection is refused (403)."""
    return container.data.inspect(body.profile_id, body.path)


@router.get("/jobs", response_model=list[JobInfo])
```

In `backend/dbaguy_backend/container.py`, replace:

```python
from .connectivity.ssh_tunnel import SshTunnelManager
from .services.connection_service import ConnectionService
from .services.couchbase_service import CouchbaseService
from .services.document_service import DocumentService
from .services.edit_service import EditService
from .services.field_sample_service import FieldSampleService
```

with:

```python
from .connectivity.ssh_tunnel import SshTunnelManager
from .services.connection_service import ConnectionService
from .services.couchbase_service import CouchbaseService
from .services.data_service import DataService
from .services.document_service import DocumentService
from .services.edit_service import EditService
from .services.field_sample_service import FieldSampleService
```

In `backend/dbaguy_backend/container.py`, replace:

```python
        self.documents = DocumentService(self.metadata, self.profiles, self.registry)
        self.edits = EditService(self.metadata, self.profiles, self.registry)
        self.field_samples = FieldSampleService(self.metadata, self.app_settings)
        self.history = HistoryService(history_file(data_dir))
        self.policy = SafetyPolicy()
        hooks = SessionHooksFactory(
```

with:

```python
        self.documents = DocumentService(self.metadata, self.profiles, self.registry)
        self.edits = EditService(self.metadata, self.profiles, self.registry)
        self.field_samples = FieldSampleService(self.metadata, self.app_settings)
        self.data = DataService(self.profiles, self.registry)
        self.history = HistoryService(history_file(data_dir))
        self.policy = SafetyPolicy()
        hooks = SessionHooksFactory(
```

- [ ] **Step 7: API types**

Regenerate the API types (never edit them by hand), from the repository root:

```bash
npm run gen:api
```

`src/lib/api/openapi.json` and `src/lib/api/schema.d.ts` change; nothing else.

In `src/lib/api/types.ts`, replace:

```ts
export type JobError = S["JobError"];
/** Data tools (spec ⑥ D-06-10): a job of this backend run, as `GET /data/jobs` lists it. */
export type JobInfo = S["JobInfo"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

with:

```ts
export type JobError = S["JobError"];
/** Data tools (spec ⑥ D-06-10): a job of this backend run, as `GET /data/jobs` lists it. */
export type JobInfo = S["JobInfo"];
/** Run SQL file (spec ⑥ §3.4): run a file on a job session, all or nothing or statement by statement. */
export type RunFileCommand = S["RunFileCommand"];
/** Run SQL file (spec ⑥ §3.2): the size, first lines, DbaGuy header and warnings of a file, before it runs. */
export type FileInspection = S["FileInspection"];
/** The header of a dump DbaGuy wrote, read back by the inspection: a hint, never a permission. */
export type DumpHeader = S["DumpHeader"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

- [ ] **Step 8: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_run_file_job.py tests/test_run_file.py tests/test_run_file_inspect.py tests/test_sqlite_adapter.py tests/test_container.py && cd .. && npm run typecheck`
Expected: `100 passed` (29 cases in `test_run_file_job.py`, 34 in `test_run_file.py` across the five engines, 6 in `test_run_file_inspect.py`); typecheck clean. The Couchbase test writes one `zz_06_` key in `dbaguy.app.orders` and removes it by exact key: afterwards `SELECT RAW COUNT(*) FROM dbaguy.app.orders` is `[500]`.

- [ ] **Step 9: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py \
  backend/dbaguy_backend/adapters/couchbase/adapter.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/api/data.py \
  backend/dbaguy_backend/container.py \
  backend/dbaguy_backend/domain/data_jobs.py \
  backend/dbaguy_backend/domain/errors.py \
  backend/dbaguy_backend/domain/execution.py \
  backend/dbaguy_backend/domain/profiles.py \
  backend/dbaguy_backend/services/data_service.py \
  backend/dbaguy_backend/sessions/hooks.py \
  backend/dbaguy_backend/sessions/jobs/run_file.py \
  backend/dbaguy_backend/sql/dump_header.py \
  backend/tests/data_support.py \
  backend/tests/fixtures/run_file/mysqldump.sql \
  backend/tests/fixtures/run_file/mysqldump_raw_binary.sql \
  backend/tests/fixtures/run_file/pg_dump_copy.sql \
  backend/tests/fixtures/run_file/pg_dump_inserts.sql \
  backend/tests/fixtures/run_file/sqlite3_dump.sql \
  backend/tests/fixtures/run_file/ssms_script.sql \
  backend/tests/test_run_file.py \
  backend/tests/test_run_file_inspect.py \
  backend/tests/test_run_file_job.py \
  src/lib/api/openapi.json \
  src/lib/api/schema.d.ts \
  src/lib/api/types.ts
git commit -m "feat(06a): Run SQL file — streamed, all or nothing or statement by statement, on every engine; inspect"
```

---

### Task 4: `DataTools.table_columns` and `TypeFamily`; edit targets read them (A1, A13, A15)

**Files:**
- Create: `backend/dbaguy_backend/adapters/columns.py`, `backend/dbaguy_backend/adapters/mssql/columns.py`, `backend/dbaguy_backend/adapters/mysql/columns.py`, `backend/dbaguy_backend/adapters/postgres/columns.py`, `backend/dbaguy_backend/adapters/sqlite/columns.py`, `backend/tests/test_table_columns.py`
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mssql/editing.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/editing.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/adapters/postgres/editing.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/editing.py`

**Interfaces:**
- Consumes: ⑤'s edit targets: each engine's `editing.py` (`edit_target`), `EditColumn`, `EditKind`, `Compare`, `edit_kind()`, `category_for_type_name`. The engines' catalog helpers: PostgreSQL native loaders and `PathSegment` resolution; MySQL `catalog.query`; SQL Server `catalog.rows`, `object_id_sql` and the metadata `_catalog_call`; SQLite `pragma_table_xinfo`.
- Produces:
  - `adapters/columns.py`: `TypeFamily` (spec §3.5.2's families, the type map's key for 06c: `bool`, `int16`, `int32`, `int64`, `uint64`, `decimal`, `money`, `float32`, `float64`, `char`, `varchar`, `text`, `binary`, `date`, `time`, `duration`, `year`, `timestamp`, `timestamp_tz`, `time_tz`, `interval`, `uuid`, `json`, `xml`, `enum`, `set`, `composite`, `spatial`, `variant`, `rowversion`, `other`); `TableColumn` (frozen: `name`, `type_name`, `family`, `nullable`, `has_default`, `kind`, `compare`, `writable`, `read_only_reason`, `unicode`, `precision`, `scale`, `length`, `identity`, `generated`, `copied`, `not_copied_reason`, `hidden`, `read_as`, `literal`; `value_kind`; `edit_column() -> EditColumn`); `not_copied(reason)`; the reasons `GENERATED`, `ROWVERSION`, `SQL_VARIANT`, `PERIOD`.
  - `adapters/base.DataTools.table_columns(path) -> list[TableColumn]`, on every relational connection.
  - `adapters/{postgres,mysql,mssql,sqlite}/columns.py`: `read_columns(…)` (by oid, by schema and name, by an object-id expression, by a `SqliteRelation`), `table_columns(conn, path)`, `column(…)` (one catalog row → `TableColumn`, the engine's rules in one place). SQLite also has `read_relation`, `affinity_kind`, `columns_of`, `rowid_alias`.
  - Each `editing.py` builds `EditColumn`s from `read_columns(…)`; the targets stay exactly what ⑤ gave (the MySQL target leaves out INVISIBLE columns as before).
  - `read_as` values: PostgreSQL `money` → `CAST({} AS numeric)` (literal `number`), `money[]` → `CAST({} AS numeric[])` (A13). SQL Server `datetime2`/`datetimeoffset`/`time` with scale 7 → `CONVERT(varchar(27|34|16), {}, 126)` (literal `datetime`, A15, S4). CLR types (hierarchyid, geography, geometry) → `CAST({} AS varbinary(max))` (literal `binary`, A1). MySQL keeps its own reads (GEOMETRY bytes as `X'…'`, A1).

- [ ] **Step 1: Probe A1, A13 and A15.** Save as `<scratchpad>/probe_a1_a13_a15.py` and run from `backend/`: `.venv/bin/python <scratchpad>/probe_a1_a13_a15.py <scratchpad>` (about 5 seconds; only `zz_06_*` scratch tables and a domain, dropped by exact name):

```python
"""06a Task 4 probe (spec ⑥ §10): A1 — MySQL GEOMETRY and SQL Server hierarchyid read as bytes and written back as a
hex literal stay the same value; A13 — PostgreSQL money, money[] and a domain over money read through numeric and
written back stay equal; A15 — SQL Server datetime2(7), datetimeoffset(7) and time(7) read as text and written back
as `N'…'` stay equal under us_english and British, for CONVERT style 126 and for CAST. Only zz_06_* scratch objects,
dropped by exact name.

Run from backend/: .venv/bin/python <this file> <a scratch folder>"""

import sys
from pathlib import Path

sys.path.insert(0, ".")
from tests.data_support import dropping, scratch_name  # noqa: E402
from tests.edit_support import scratch_db  # noqa: E402

work = Path(sys.argv[1])


def a1_mysql() -> None:
    db = scratch_db("mysql", work)
    try:
        version = db.rows("SELECT VERSION()")[0][0]
        with dropping(db) as made:
            name = scratch_name("a1")
            made.append(("TABLE", name))
            db.execute(f"CREATE TABLE {db.table(name)} (id INT PRIMARY KEY, geo GEOMETRY)",
                       f"INSERT INTO {db.table(name)} VALUES (1, ST_GeomFromText('POLYGON((0 0, 3 0, 3 3, 0 0))', 4326))")
            raw = db.rows(f"SELECT geo FROM {db.table(name)} WHERE id = 1")[0][0]
            db.execute(f"INSERT INTO {db.table(name)} VALUES (2, X'{bytes(raw).hex()}')")
            same = db.rows(f"SELECT COUNT(DISTINCT HEX(geo)), MIN(ST_SRID(geo)), MIN(ST_AsText(geo)) "
                           f"FROM {db.table(name)}")[0]
            print(f"A1 MySQL {version}: GEOMETRY as X'…' {'same' if same[0] == 1 else 'DIFFERENT'} "
                  f"(SRID {same[1]}, {same[2]})")
            print("A1 MySQL: VECTOR " + ("exists" if version.startswith("9") else "is not in this version (8.4)"))
    finally:
        db.close()


def a1_sql_server() -> None:
    db = scratch_db("mssql", work)
    try:
        with dropping(db) as made:
            name = scratch_name("a1")
            made.append(("TABLE", name))
            db.execute(f"CREATE TABLE {db.table(name)} (id int PRIMARY KEY, h hierarchyid)",
                       f"INSERT INTO {db.table(name)} VALUES (1, '/1/2/3/')")
            raw = db.rows(f"SELECT CAST(h AS varbinary(max)) FROM {db.table(name)} WHERE id = 1")[0][0]
            db.execute(f"INSERT INTO {db.table(name)} VALUES (2, 0x{bytes(raw).hex()})")
            texts = db.rows(f"SELECT h.ToString() FROM {db.table(name)} ORDER BY id")
            print(f"A1 SQL Server: hierarchyid as 0x… {'same' if texts[0] == texts[1] else 'DIFFERENT'} ({texts[0][0]})")
    finally:
        db.close()


def a13() -> None:
    db = scratch_db("postgres", work)
    try:
        with dropping(db) as made:
            cash, name = scratch_name("a13_cash"), scratch_name("a13")
            made += [("DOMAIN", cash), ("TABLE", name)]
            db.execute(f"CREATE DOMAIN public.{cash} AS money",
                       f"CREATE TABLE {db.table(name)} (id int PRIMARY KEY, m money, ma money[], d public.{cash})",
                       f"INSERT INTO {db.table(name)} VALUES (1, '-1234567.89', '{{1.50,-2,NULL}}', '7.25')")
            m, ma, d = db.rows(f"SELECT CAST(m AS numeric)::text, CAST(ma AS numeric[])::text, CAST(d AS numeric)::text "
                               f"FROM {db.table(name)} WHERE id = 1")[0]
            db.execute(f"INSERT INTO {db.table(name)} VALUES (2, {m}, '{ma}', {d})")
            same = db.rows(f"SELECT a.m = b.m, a.ma = b.ma, a.d = b.d FROM {db.table(name)} a, {db.table(name)} b "
                           "WHERE a.id = 1 AND b.id = 2")[0]
            print(f"A13: money {m} {same[0]}, money[] {ma} {same[1]}, domain over money {d} {same[2]}")
    finally:
        db.close()


def a15() -> None:
    db = scratch_db("mssql", work)
    try:
        with dropping(db) as made:
            name = scratch_name("a15")
            made.append(("TABLE", name))
            db.execute(f"CREATE TABLE {db.table(name)} (id int PRIMARY KEY, d2 datetime2(7), dto datetimeoffset(7), "
                       "t7 time(7))",
                       f"INSERT INTO {db.table(name)} VALUES (1, '2026-10-05T12:34:56.1234567', "
                       "'2026-10-05T12:34:56.1234567+08:00', '12:34:56.1234567')")
            forms = {"CONVERT 126": "CONVERT(varchar(27), d2, 126), CONVERT(varchar(34), dto, 126), "
                                    "CONVERT(varchar(16), t7, 126)",
                     "CAST": "CAST(d2 AS varchar(27)), CAST(dto AS varchar(34)), CAST(t7 AS varchar(16))"}
            next_id = 2
            for language in ("us_english", "British"):
                for form, read in forms.items():
                    texts = db.rows(f"SET LANGUAGE {language}; SELECT {read} FROM {db.table(name)} WHERE id = 1")[0]
                    # what the dump writes: a date and a time joined by T (F1), the text otherwise as read
                    literals = [f"N'{t.replace(' ', 'T', 1) if t[4:5] == '-' else t}'" for t in texts]
                    try:
                        db.execute(f"SET LANGUAGE {language}; INSERT INTO {db.table(name)} "
                                   f"VALUES ({next_id}, {', '.join(literals)})")
                        same = db.rows(f"SELECT CASE WHEN a.d2 = b.d2 THEN 1 ELSE 0 END, CASE WHEN a.dto = b.dto "
                                       "THEN 1 ELSE 0 END, CASE WHEN a.t7 = b.t7 THEN 1 ELSE 0 END "
                                       f"FROM {db.table(name)} a, {db.table(name)} b "
                                       f"WHERE a.id = 1 AND b.id = {next_id}")[0]
                        result = "same" if all(same) else f"DIFFERENT {same}"
                    except Exception as exc:  # pymssql's own exception: only str() is read (never .text/.message)
                        result = f"refused: {str(exc).splitlines()[0][:120]}"
                    print(f"A15 {language}, {form}: {texts} -> {result}")
                    next_id += 1
    finally:
        db.close()


a1_mysql()
a1_sql_server()
a13()
a15()
```

Expected (what it showed while planning, on the dev containers):

```
A1 MySQL 8.4.11: GEOMETRY as X'…' same (SRID 4326, POLYGON((0 0,3 0,3 3,0 0)))
A1 MySQL: VECTOR is not in this version (8.4)
A1 SQL Server: hierarchyid as 0x… same (/1/2/3/)
A13: money -1234567.89 True, money[] {1.50,-2.00,NULL} True, domain over money 7.25 True
A15 us_english, CONVERT 126: ('2026-10-05T12:34:56.1234567', '2026-10-05T12:34:56.1234567+08:00', '12:34:56.1234567') -> same
A15 us_english, CAST: ('2026-10-05 12:34:56.1234567', '2026-10-05 12:34:56.1234567 +08:00', '12:34:56.1234567') -> refused: (241, …
A15 British, CONVERT 126: (…) -> same
A15 British, CAST: (…) -> refused: (241, …
```

If A1 differs for a type, that type is not copied (`not_copied(…)` with its reason) and the header lists it. If A13 differs, money stays `other` and is written as its text (lc_monetary: a known limitation). A15's CAST refusal is why `read_as` uses style 126 (S4). If style 126 ever differs, the 7th digit is read as `CAST(… AS varchar)` with the space before the offset removed by `datetime_literal` (Task 8), and the probe's CAST line must then say `same`.

- [ ] **Step 2: Write the failing tests**

`test_table_columns.py` makes one scratch table per engine with every family the engine has, and checks each column's family, sizes, identity, `copied`, `read_as` and `literal`.

Create `backend/tests/test_table_columns.py`:

```python
"""A relation's columns as data tools read them (spec ⑥ D-06-1, §3.5.2): each engine's catalog in one shape — the
type family, sizes, identity, what a dump or copy leaves out and how its SELECT reads a column. Grid editing reads the
same columns (its own tests check its targets). Scratch tables are `zz_06_cols_*`, dropped by exact name."""

import pytest

from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.domain.errors import NotFound
from tests.data_support import dropping, scratch_name
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg


def shape(column: TableColumn) -> tuple:
    return (column.family, column.precision, column.scale, column.length, column.identity, column.copied,
            column.read_as, column.value_kind)


def read(db, name: str) -> dict[str, TableColumn]:
    conn = db.open()
    try:
        return {column.name: column for column in conn.table_columns(db.path(name))}
    finally:
        conn.close()


@requires_pg
def test_postgresql_columns(tmp_path):
    db = scratch_db("postgres", tmp_path)
    with dropping(db) as made:
        name = scratch_name("cols")
        cash, mood = f"{name}_cash", f"{name}_mood"
        made += [("DOMAIN", cash), ("TYPE", mood), ("TABLE", name)]
        db.execute(f"CREATE DOMAIN public.{cash} AS numeric(10,2)", f"CREATE TYPE public.{mood} AS ENUM ('a', 'b')",
                   f"CREATE TABLE public.{name} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                   "s serial, by_default int GENERATED BY DEFAULT AS IDENTITY, n numeric(30,10), free numeric, "
                   f"d public.{cash}, f real, c char(4), v varchar(20), t text, b bytea, ts timestamptz(3), "
                   f"tz timetz, j jsonb, m money, ma money[], e public.{mood}, a int[], u uuid, "
                   "g int GENERATED ALWAYS AS (s * 2) STORED)")
        found = read(db, name)
        assert {column: shape(found[column]) for column in found} == {
            "id": ("int64", None, None, None, "always", True, None, "number"),
            "s": ("int32", None, None, None, None, True, None, "number"),  # serial: a default, its sequence the dump's
            "by_default": ("int32", None, None, None, "by_default", True, None, "number"),
            "n": ("decimal", 30, 10, None, None, True, None, "number"),
            "free": ("decimal", None, None, None, None, True, None, "number"),
            "d": ("decimal", 10, 2, None, None, True, None, "number"),  # a domain: its base type and modifier
            "f": ("float32", None, None, None, None, True, None, "number"),
            "c": ("char", None, None, 4, None, True, None, "text"),
            "v": ("varchar", None, None, 20, None, True, None, "text"),
            "t": ("text", None, None, None, None, True, None, "text"),
            "b": ("binary", None, None, None, None, True, None, "binary"),
            "ts": ("timestamp_tz", None, 3, None, None, True, None, "datetime"),
            "tz": ("time_tz", None, None, None, None, True, None, "datetime"),
            "j": ("json", None, None, None, None, True, None, "json"),
            "m": ("money", None, None, None, None, True, "CAST({} AS numeric)", "number"),  # P10: lc_monetary's text is not data
            "ma": ("other", None, None, None, None, True, "CAST({} AS numeric[])", "other"),  # A13
            "e": ("enum", None, None, None, None, True, None, "other"),
            "a": ("other", None, None, None, None, True, None, "other"),
            "u": ("uuid", None, None, None, None, True, None, "text"),
            "g": ("int32", None, None, None, None, False, None, "number"),
        }
        assert found["g"].not_copied_reason == "generated column" and found["g"].generated
        assert found["id"].edit_column().read_only_reason == "identity column (GENERATED ALWAYS)"
        view = scratch_name("cols_view")
        made.append(("VIEW", view))
        db.execute(f"CREATE VIEW public.{view} AS SELECT id, n FROM public.{name}")
        assert [column.name for column in read(db, view).values()] == ["id", "n"]  # views have columns too
        with pytest.raises(NotFound):
            read(db, scratch_name("cols_missing"))


@requires_mysql
def test_mysql_columns(tmp_path):
    db = scratch_db("mysql", tmp_path)
    with dropping(db) as made:
        name = scratch_name("cols")
        made.append(("TABLE", name))
        db.execute(f"CREATE TABLE {name} (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, flag TINYINT(1), "
                   "tiny TINYINT UNSIGNED, n DECIMAL(30,10), f FLOAT, c CHAR(4), v VARCHAR(20), t LONGTEXT, "
                   "b VARBINARY(8), dt DATETIME(6), tm TIME, y YEAR, j JSON, e ENUM('a','b'), st SET('x','y'), "
                   "bt BIT(4), geo GEOMETRY, g INT AS (tiny * 2) STORED, inv INT INVISIBLE)")
        found = read(db, name)
        assert {column: shape(found[column]) for column in found} == {
            "id": ("uint64", None, None, None, "by_default", True, None, "number"),
            "flag": ("bool", None, None, None, None, True, None, "number"),
            "tiny": ("int16", None, None, None, None, True, None, "number"),
            "n": ("decimal", 30, 10, None, None, True, None, "number"),
            "f": ("float32", None, None, None, None, True, None, "number"),
            "c": ("char", None, None, 4, None, True, None, "text"),
            "v": ("varchar", None, None, 20, None, True, None, "text"),
            "t": ("text", None, None, None, None, True, None, "text"),
            "b": ("binary", None, None, 8, None, True, None, "binary"),
            "dt": ("timestamp", None, 6, None, None, True, None, "datetime"),
            "tm": ("duration", None, 0, None, None, True, None, "datetime"),  # TIME: beyond 24 h, or negative
            "y": ("year", None, None, None, None, True, None, "datetime"),
            "j": ("json", None, None, None, None, True, None, "json"),
            "e": ("enum", None, None, None, None, True, None, "text"),
            "st": ("set", None, None, None, None, True, None, "text"),
            "bt": ("int64", None, None, None, None, True, None, "number"),  # A M7: read as an integer
            "geo": ("spatial", None, None, None, None, True, None, "binary"),
            "g": ("int32", None, None, None, None, False, None, "number"),
            "inv": ("int32", None, None, None, None, True, None, "number"),
        }
        assert found["inv"].hidden and not found["t"].hidden  # INVISIBLE: still dumped


@requires_mssql
def test_sql_server_columns(tmp_path):
    db = scratch_db("mssql", tmp_path)
    with dropping(db) as made:
        name = scratch_name("cols")
        made.append(("TABLE", name))
        db.execute(f"CREATE TABLE dbo.{name} (id int IDENTITY(1,1) PRIMARY KEY, n decimal(38,10), f float, "
                   "r real, c nchar(4), v nvarchar(20), vm varchar(max), b varbinary(8), d2 datetime2(7), "
                   "d3 datetime2(3), dto datetimeoffset(7), t7 time(7), dt datetime, m money, g uniqueidentifier, "
                   "x xml, sv sql_variant, h hierarchyid, geo geography, rv rowversion, comp AS (id * 2))")
        found = read(db, name)
        assert {column: shape(found[column]) for column in found} == {
            "id": ("int32", None, None, None, "always", True, None, "number"),
            "n": ("decimal", 38, 10, None, None, True, None, "number"),
            "f": ("float64", None, None, None, None, True, None, "number"),
            "r": ("float32", None, None, None, None, True, None, "number"),
            "c": ("char", None, None, 4, None, True, None, "text"),
            "v": ("varchar", None, None, 20, None, True, None, "text"),
            "vm": ("text", None, None, None, None, True, None, "text"),
            "b": ("binary", None, None, 8, None, True, None, "binary"),
            "d2": ("timestamp", None, 7, None, None, True, "CONVERT(varchar(27), {}, 126)", "datetime"),  # A S7: the 7th digit
            "d3": ("timestamp", None, 3, None, None, True, None, "datetime"),
            "dto": ("timestamp_tz", None, 7, None, None, True, "CONVERT(varchar(34), {}, 126)", "datetime"),
            "t7": ("time", None, 7, None, None, True, "CONVERT(varchar(16), {}, 126)", "datetime"),
            "dt": ("timestamp", None, None, None, None, True, None, "datetime"),
            "m": ("money", None, None, None, None, True, None, "number"),
            "g": ("uuid", None, None, None, None, True, None, "text"),
            "x": ("xml", None, None, None, None, True, None, "text"),
            "sv": ("variant", None, None, None, None, False, None, "binary"),
            "h": ("variant", None, None, None, None, True, "CAST({} AS varbinary(max))", "binary"),  # A S6, A1
            "geo": ("spatial", None, None, None, None, True, "CAST({} AS varbinary(max))", "binary"),
            "rv": ("rowversion", None, None, None, None, False, None, "binary"),
            "comp": ("int32", None, None, None, None, False, None, "number"),
        }
        assert found["vm"].unicode is False and found["v"].unicode
        assert (found["sv"].not_copied_reason, found["rv"].not_copied_reason) == (
            "sql_variant column: only its raw bytes can be read", "rowversion column")


def test_sqlite_columns(tmp_path):
    db = scratch_db("sqlite", tmp_path)
    with dropping(db) as made:
        name = scratch_name("cols")
        made.append(("TABLE", name))
        db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, n DECIMAL(12,3), f REAL, c CHAR(4), "
                   "v VARCHAR(20), t TEXT, b BLOB, d DATE, ts DATETIME, flag BOOLEAN, j JSON, x, "
                   "g INT GENERATED ALWAYS AS (id * 2))")
        found = read(db, name)
        assert {column: shape(found[column]) for column in found} == {
            "id": ("int64", None, None, None, "by_default", True, None, "number"),  # the rowid
            "n": ("decimal", 12, 3, None, None, True, None, "number"),
            "f": ("float64", None, None, None, None, True, None, "number"),
            "c": ("char", None, None, 4, None, True, None, "text"),
            "v": ("varchar", None, None, 20, None, True, None, "text"),
            "t": ("text", None, None, None, None, True, None, "text"),
            "b": ("binary", None, None, None, None, True, None, "binary"),
            "d": ("date", None, None, None, None, True, None, "datetime"),
            "ts": ("timestamp", None, None, None, None, True, None, "datetime"),
            "flag": ("bool", None, None, None, None, True, None, "bool"),
            "j": ("json", None, None, None, None, True, None, "json"),
            "x": ("other", None, None, None, None, True, None, "other"),
            "g": ("int64", None, None, None, None, False, None, "number"),
        }
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_table_columns.py`
Expected: collection error, `ModuleNotFoundError: No module named 'dbaguy_backend.adapters.columns'`.

- [ ] **Step 4: The engine-neutral column and the interface**

Create `backend/dbaguy_backend/adapters/columns.py`:

```python
"""A relation's columns as data tools and grid editing read them (spec ⑥ D-06-1): what each engine's catalog says,
in one engine-neutral shape. Grid editing projects a column to its EditColumn (⑤'s rules, unchanged); a dump or a copy
reads which columns it writes, how the SELECT reads each one, and its type family (`TypeFamily`, the key of the
cross-engine type map). Pure."""

from dataclasses import dataclass
from typing import Literal

from ..domain.edits import Compare, EditColumn, EditKind

# The families of spec ⑥ §3.5.2: what a column holds, whatever its engine calls it. `composite` is a PostgreSQL row
# type; `variant` SQL Server's sql_variant and hierarchyid; `other` anything a copy writes as the engine's text.
TypeFamily = Literal[
    "bool", "int16", "int32", "int64", "uint64", "decimal", "money", "float32", "float64", "char", "varchar", "text",
    "binary", "date", "time", "duration", "year", "timestamp", "timestamp_tz", "time_tz", "interval", "uuid", "json",
    "xml", "enum", "set", "composite", "spatial", "variant", "rowversion", "other",
]
Identity = Literal["always", "by_default"]

GENERATED = "generated column"
ROWVERSION = "rowversion column"
SQL_VARIANT = "sql_variant column: only its raw bytes can be read"
PERIOD = "period column (system-versioned)"


@dataclass(frozen=True, slots=True)
class TableColumn:
    name: str
    type_name: str  # as declared: numeric(10,2), varchar(20), datetime2(7)
    family: TypeFamily
    nullable: bool
    has_default: bool  # a default, identity, auto_increment or generated value: an INSERT may leave it out
    kind: EditKind  # how a typed value is read (grid editing), and by default how a read value is written
    compare: Compare  # how grid editing checks an original value
    writable: bool  # grid editing may set it
    read_only_reason: str | None = None
    unicode: bool = True  # SQL Server: False for char / varchar / text (ASCII text then goes without N)
    precision: int | None = None  # decimal(p, s)
    scale: int | None = None  # decimal(p, s); the fractional digits of a time type
    length: int | None = None  # char / varchar / binary (n) in the engine's unit; None: no limit (text, max)
    identity: Identity | None = None  # always: written only with OVERRIDING SYSTEM VALUE / IDENTITY_INSERT
    generated: bool = False  # the server computes it (never written)
    copied: bool = True  # a dump or a copy writes it
    not_copied_reason: str | None = None  # why not: generated, rowversion, sql_variant, period column
    hidden: bool = False  # MySQL INVISIBLE: not in SELECT *, but a dump keeps its values
    # The expression a dump or copy SELECT reads it with, `{}` standing for the quoted column (PG money: CAST({} AS
    # numeric); SQL Server time(7): CONVERT(varchar(16), {}, 126)); None: the column itself
    read_as: str | None = None
    literal: EditKind | None = None  # how a value read that way is written; None: as `kind`

    @property
    def value_kind(self) -> EditKind:
        """How a value the dump or copy SELECT read is written as a literal."""
        return self.literal or self.kind

    def edit_column(self) -> EditColumn:
        """The column as grid editing knows it (spec ⑤ §3.2.2)."""
        return EditColumn(
            name=self.name, type_name=self.type_name, kind=self.kind, nullable=self.nullable,
            has_default=self.has_default, writable=self.writable, read_only_reason=self.read_only_reason,
            compare=self.compare, unicode=self.unicode,
        )


def not_copied(reason: str) -> dict:
    """The fields of a column a dump or a copy leaves out (the server fills it, or it cannot be read back)."""
    return {"copied": False, "not_copied_reason": reason}
```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo, ServerProfile
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .positions import offset_to_line_col


```

with:

```python
from ..domain.profiles import AnyDraft, AnyProfile, Engine, EngineInfo, ServerProfile
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .columns import TableColumn
from .positions import offset_to_line_col


```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
        error. NotFound when the table does not exist."""


class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
```

with:

```python
        error. NotFound when the table does not exist."""


class DataTools(ABC):
    """A connection whose relations data tools read and write (spec ⑥ D-06-1: dump, copy). Every method runs on the
    owning worker thread, like the others."""

    @abstractmethod
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        """The columns of the table, view or materialized view at `path`, in order; NotFound when it is gone."""


class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
```

- [ ] **Step 5: PostgreSQL**

Create `backend/dbaguy_backend/adapters/postgres/columns.py`:

```python
"""A PostgreSQL relation's columns (spec ⑥ D-06-1): one catalog query on the connection's native loaders. A domain is
read through its base type, however many domains deep (⑤ ruling E23), and so is its type modifier (precision, length).
Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

import psycopg

from ...domain.edits import Compare, EditKind, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
from .introspection import relation_oid
from .loaders import category_for_type_name

# `base` follows typbasetype from each column's type until a type that is not a domain, one row per column, carrying
# the innermost type modifier a domain sets (a domain column's own atttypmod is -1). typtype 'c' is a composite type,
# 'e' an enum; typcategory 'A' an array.
_COLUMNS = """
WITH RECURSIVE base (attnum, typname, typtype, typbasetype, typtypmod, typcategory, typmod) AS (
    SELECT a.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory, a.atttypmod
    FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
    SELECT d.attnum, t.typname, t.typtype, t.typbasetype, t.typtypmod, t.typcategory,
           CASE WHEN d.typmod <> -1 THEN d.typmod ELSE d.typtypmod END
    FROM base d JOIN pg_type t ON t.oid = d.typbasetype
    WHERE d.typtype = 'd'
)
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef, a.attidentity::text,
       a.attgenerated::text, b.typname::text, b.typtype::text, b.typcategory::text, b.typmod
FROM pg_attribute a
JOIN base b ON b.attnum = a.attnum AND b.typtype <> 'd'
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
# Compared with `=`, not ::text: bpchar, whose ::text drops the padding the loaded value has (`=` ignores it, A4); bytea,
# whose ::text follows bytea_output (escape: every original would differ) while `= '\x…'::bytea` is exact (05a review M3)
_EQUALS = frozenset({"bpchar", "bytea"})
_FAMILIES: dict[str, TypeFamily] = {
    "bool": "bool", "int2": "int16", "int4": "int32", "int8": "int64", "numeric": "decimal", "money": "money",
    "float4": "float32", "float8": "float64", "bpchar": "char", "varchar": "varchar", "text": "text", "citext": "text",
    "name": "text", "bytea": "binary", "date": "date", "time": "time", "timetz": "time_tz", "timestamp": "timestamp",
    "timestamptz": "timestamp_tz", "interval": "interval", "uuid": "uuid", "json": "json", "jsonb": "json",
    "xml": "xml", "geometry": "spatial", "geography": "spatial",
}
_LENGTHS = frozenset({"bpchar", "varchar"})
_FRACTIONS = frozenset({"time", "timetz", "timestamp", "timestamptz"})
# money's text follows lc_monetary (`$1,234.56`): a dump or copy reads it as numeric, exact, and writes the number (P10;
# A13: an array of money and a domain over money too)
_MONEY_READ = {"money": ("CAST({} AS numeric)", "number"), "_money": ("CAST({} AS numeric[])", "other")}


def _family(base: str, typtype: str, category: str) -> TypeFamily:
    if typtype == "e":
        return "enum"
    if typtype == "c":
        return "composite"
    if category == "A":
        return "other"  # an array: PostgreSQL's text
    return _FAMILIES.get(base, "other")


def _sizes(base: str, typmod: int) -> dict:
    """precision / scale / length from the type modifier (-1: none)."""
    if typmod < 0:
        return {}
    if base == "numeric" and typmod >= 4:
        scale = (typmod - 4) & 0x7FF
        return {"precision": ((typmod - 4) >> 16) & 0xFFFF, "scale": scale - 2048 if scale > 1023 else scale}
    if base in _LENGTHS and typmod >= 4:
        return {"length": typmod - 4}
    if base in _FRACTIONS:
        return {"scale": typmod}
    return {}


def column(
    name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
    typtype: str, category: str, typmod: int,
) -> TableColumn:
    """One row of _COLUMNS."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _EQUALS else "as_text"
    reason = GENERATED if generated else "identity column (GENERATED ALWAYS)" if identity == "a" else None
    read_as, literal = _MONEY_READ.get(base, (None, None))
    return TableColumn(
        name=name, type_name=type_name, family=_family(base, typtype, category), nullable=nullable,
        has_default=has_default or bool(identity) or bool(generated), kind=kind, compare=compare,
        writable=reason is None, read_only_reason=reason, **_sizes(base, typmod),
        identity="always" if identity == "a" else "by_default" if identity == "d" else None,
        generated=bool(generated), **(not_copied(GENERATED) if generated else {}), read_as=read_as, literal=literal,
    )


def read_columns(conn: psycopg.Connection, oid: int) -> list[TableColumn]:
    return [column(*row) for row in conn.execute(_COLUMNS, {"oid": oid})]


def table_columns(conn: psycopg.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the relation at `path` (a table, a view, a materialized view); NotFound when it is gone."""
    schema, name = relation_from_path(path)
    return read_columns(conn, relation_oid(conn, qualified(Dialect.POSTGRES, schema, name)))
```

In `backend/dbaguy_backend/adapters/postgres/editing.py`, replace:

```python
from loguru import logger
from psycopg import pq

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget, edit_kind
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import SortKey, build_browse_select, relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .introspection import relation_oid
from .loaders import category_for_type_name

# standard_conforming_strings rides along (no round trip of its own): off, a backslash in '…' escapes (ruling G3)
_RELKIND = "SELECT relkind::text, current_setting('standard_conforming_strings') FROM pg_class WHERE oid = %(oid)s"
# A domain is read through its base type, however many domains deep (ruling E23): `base` follows typbasetype from each
# column's type until a type that is not a domain, one row per column. typtype 'c' marks a composite type (ruling E4).
_COLUMNS = """
WITH RECURSIVE base (attnum, typname, typtype, typbasetype) AS (
    SELECT a.attnum, t.typname, t.typtype, t.typbasetype
    FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
    SELECT d.attnum, t.typname, t.typtype, t.typbasetype
    FROM base d JOIN pg_type t ON t.oid = d.typbasetype
    WHERE d.typtype = 'd'
)
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.atthasdef, a.attidentity::text,
       a.attgenerated::text, b.typname::text, b.typtype::text
FROM pg_attribute a
JOIN base b ON b.attnum = a.attnum AND b.typtype <> 'd'
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
# Key columns only (INCLUDE columns come after indnkeyatts); no partial, expression or invalid index.
_KEYS = """
SELECT ic.relname::text, i.indisprimary, array_agg(a.attname::text ORDER BY k.ord)
```

with:

```python
from loguru import logger
from psycopg import pq

from ...domain.edits import EditTarget
from ...domain.metadata import PathSegment
from ...sql.dialects import Dialect, qualified
from ...sql.select_builder import SortKey, build_browse_select, relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .columns import read_columns
from .introspection import relation_oid

# standard_conforming_strings rides along (no round trip of its own): off, a backslash in '…' escapes (ruling G3)
_RELKIND = "SELECT relkind::text, current_setting('standard_conforming_strings') FROM pg_class WHERE oid = %(oid)s"
# Key columns only (INCLUDE columns come after indnkeyatts); no partial, expression or invalid index.
_KEYS = """
SELECT ic.relname::text, i.indisprimary, array_agg(a.attname::text ORDER BY k.ord)
```

In `backend/dbaguy_backend/adapters/postgres/editing.py`, replace:

```python
"""
_STRINGS_NOT_STANDARD = "standard_conforming_strings is off: values with backslashes would be changed"
_READ_ONLY = {"v": VIEWS, "m": "Materialized views are read-only here", "f": "Foreign tables are read-only here"}
_NOT_EXACT = frozenset({"float4", "float8"})  # loaded as Python floats: their text is not the server's
# Compared with `=`, not ::text: bpchar, whose ::text drops the padding the loaded value has (`=` ignores it, A4); bytea,
# whose ::text follows bytea_output (escape: every original would differ) while `= '\x…'::bytea` is exact (05a review M3)
_EQUALS = frozenset({"bpchar", "bytea"})
# A plan that sorts at most this many rows (the planner's estimate) still starts the first page at once: a small table
_SMALL_SORT_ROWS = 10_000
_SORTS = frozenset({"Sort", "Incremental Sort"})
```

with:

```python
"""
_STRINGS_NOT_STANDARD = "standard_conforming_strings is off: values with backslashes would be changed"
_READ_ONLY = {"v": VIEWS, "m": "Materialized views are read-only here", "f": "Foreign tables are read-only here"}
# A plan that sorts at most this many rows (the planner's estimate) still starts the first page at once: a small table
_SMALL_SORT_ROWS = 10_000
_SORTS = frozenset({"Sort", "Incremental Sort"})
```

In `backend/dbaguy_backend/adapters/postgres/editing.py`, replace:

```python
_PLAN_LIMITS = f"SET LOCAL statement_timeout = {_PLAN_TIMEOUT_MS}; SET LOCAL lock_timeout = {_PLAN_TIMEOUT_MS}"


def _column(
    name: str, type_name: str, nullable: bool, has_default: bool, identity: str, generated: str, base: str,
    typtype: str,
) -> tuple[EditColumn, bool]:
    """The column, and whether its type is composite (a key on it cannot be written as `c = '…'`, 0A000)."""
    kind: EditKind = "other" if base == "money" else edit_kind(category_for_type_name(base))  # money: lc_monetary
    compare: Compare = "none" if base in _NOT_EXACT else "equals" if base in _EQUALS else "as_text"
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
```

with:

```python
_PLAN_LIMITS = f"SET LOCAL statement_timeout = {_PLAN_TIMEOUT_MS}; SET LOCAL lock_timeout = {_PLAN_TIMEOUT_MS}"


def edit_target(conn: psycopg.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    oid = relation_oid(conn, qualified(Dialect.POSTGRES, schema, name))
```

In `backend/dbaguy_backend/adapters/postgres/editing.py`, replace:

```python
        return read_only_relation(path, _READ_ONLY.get(relkind, "This object is read-only here"))
    if standard_strings != "on":  # fail closed: only the setting the literals are written for
        return read_only_relation(path, _STRINGS_NOT_STANDARD)
    read = [_column(*row) for row in conn.execute(_COLUMNS, {"oid": oid})]
    composite = {column.name for column, is_composite in read if is_composite}
    candidates = [
        KeyCandidate(index, tuple(columns), primary)
        for index, primary, columns in conn.execute(_KEYS, {"oid": oid})
        if not composite.intersection(columns)
    ]
    target = build_target(path, [column for column, _ in read], candidates)
    if not target.editable:
        return target
    return target.model_copy(update={"stable_order": _stable_order(conn, path, target.key)})
```

with:

```python
        return read_only_relation(path, _READ_ONLY.get(relkind, "This object is read-only here"))
    if standard_strings != "on":  # fail closed: only the setting the literals are written for
        return read_only_relation(path, _STRINGS_NOT_STANDARD)
    read = read_columns(conn, oid)
    composite = {column.name for column in read if column.family == "composite"}  # `=` on it is 0A000 (ruling E4)
    candidates = [
        KeyCandidate(index, tuple(columns), primary)
        for index, primary, columns in conn.execute(_KEYS, {"oid": oid})
        if not composite.intersection(columns)
    ]
    target = build_target(path, [column.edit_column() for column in read], candidates)
    if not target.editable:
        return target
    return target.model_copy(update={"stable_order": _stable_order(conn, path, target.key)})
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
from ..cancel_window import CancelWindow
from ..positions import offset_to_line_col
from ..sql_text import first_keyword
from . import ddl, editing, introspection
from . import schema as pg_schema
from .loaders import category_for_oid, configure_user_loaders

```

with:

```python
from ..cancel_window import CancelWindow
from ..positions import offset_to_line_col
from ..sql_text import first_keyword
from ..columns import TableColumn
from . import columns, ddl, editing, introspection
from . import schema as pg_schema
from .loaders import category_for_oid, configure_user_loaders

```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
            logger.warning("Error while closing PostgreSQL cursor: {}", exc)


class PostgresConnection(DatabaseConnection, RowEditing):
    def __init__(self, conn: psycopg.Connection, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
```

with:

```python
            logger.warning("Error while closing PostgreSQL cursor: {}", exc)


class PostgresConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: psycopg.Connection, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        self._check_database(path)
        return editing.edit_target(self._conn, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        # The connection's own (native-loader) cursors: arrays come back as lists, unlike the text-first user cursors.
```

with:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        self._check_database(path)
        return editing.edit_target(self._conn, path)

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        self._check_database(path)
        return columns.table_columns(self._conn, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        # The connection's own (native-loader) cursors: arrays come back as lists, unlike the text-first user cursors.
```

- [ ] **Step 6: MySQL**

Create `backend/dbaguy_backend/adapters/mysql/columns.py`:

```python
"""A MySQL relation's columns (spec ⑥ D-06-1): one information_schema query. Grid editing's kind and compare rules
(spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

from mysql.connector.connection import MySQLConnection

from ...domain.edits import Compare, edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, TypeFamily, not_copied
from . import catalog
from .types import SPATIAL_TYPES, category_for_data_type

_COLUMNS = """
SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, NUMERIC_PRECISION, NUMERIC_SCALE,
       CHARACTER_MAXIMUM_LENGTH, DATETIME_PRECISION
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION
"""
_GENERATED = ("VIRTUAL GENERATED", "STORED GENERATED")  # not DEFAULT_GENERATED: an expression default (ruling E15)
_NOT_EXACT = frozenset({"float", "double", "real", "vector", *SPATIAL_TYPES})
_FAMILIES: dict[str, TypeFamily] = {
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
_UNSIGNED: dict[str, TypeFamily] = {"tinyint": "int16", "smallint": "int32", "mediumint": "int32", "int": "int64",
                                    "integer": "int64", "bigint": "uint64"}


def _family(data_type: str, column_type: str) -> TypeFamily:
    if column_type.startswith("tinyint(1)") and "unsigned" not in column_type:
        return "bool"  # MySQL's BOOLEAN
    if "unsigned" in column_type and data_type in _UNSIGNED:
        return _UNSIGNED[data_type]
    return _FAMILIES.get(data_type, "other")


def column(
    name: str, column_type: str, data_type: str, is_nullable: str, default: str | None, extra: str | None,
    precision: int | None, scale: int | None, length: int | None, fraction: int | None,
) -> TableColumn:
    """One row of _COLUMNS."""
    data_type, extra, lowered = data_type.lower(), (extra or "").upper(), column_type.lower()
    generated = any(word in extra for word in _GENERATED)
    if generated:
        reason: str | None = GENERATED
    elif data_type in SPATIAL_TYPES:
        reason = "geometry values are not edited here"
    elif data_type == "vector":
        reason = "vector values are not edited here"
    else:
        reason = None
    compare: Compare = "none" if data_type in _NOT_EXACT else "equals"
    sizes: dict = {}
    if data_type in ("decimal", "numeric"):
        sizes = {"precision": precision, "scale": scale}
    elif data_type in ("char", "varchar", "binary", "varbinary"):
        sizes = {"length": length}
    elif data_type in ("time", "datetime", "timestamp"):
        sizes = {"scale": fraction}
    return TableColumn(
        name=name, type_name=column_type, family=_family(data_type, lowered), nullable=is_nullable == "YES",
        has_default=default is not None or generated or "AUTO_INCREMENT" in extra or "DEFAULT_GENERATED" in extra,
        kind=edit_kind(category_for_data_type(data_type)), compare=compare, writable=reason is None,
        read_only_reason=reason, **sizes, identity="by_default" if "AUTO_INCREMENT" in extra else None,
        generated=generated, **(not_copied(GENERATED) if generated else {}), hidden="INVISIBLE" in extra,
    )


def read_columns(conn: MySQLConnection, schema: str, name: str) -> list[TableColumn]:
    return [column(*row) for row in catalog.query(conn, _COLUMNS, (schema, name))]


def table_columns(conn: MySQLConnection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`, INVISIBLE ones included (`hidden`); NotFound when it is gone."""
    schema, name = relation_from_path(path)
    found = read_columns(conn, schema or "", name)
    if not found:
        raise NotFound(f"Table {schema}.{name} not found")
    return found
```

In `backend/dbaguy_backend/adapters/mysql/editing.py`, replace:

```python

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
```

with:

```python

from mysql.connector.connection import MySQLConnection

from ...domain.edits import EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import catalog
from .columns import read_columns

_TABLE = """
SELECT t.TABLE_TYPE, e.TRANSACTIONS
FROM information_schema.TABLES t LEFT JOIN information_schema.ENGINES e ON e.ENGINE = t.ENGINE
WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s
"""
# COLUMN_NAME is NULL for a functional key part (ruling E4)
_KEYS = """
```

In `backend/dbaguy_backend/adapters/mysql/editing.py`, replace:

```python
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
```

with:

```python
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_INDEX
"""
_SQL_MODE = "SELECT @@SESSION.sql_mode"


def edit_target(conn: MySQLConnection, path: list[PathSegment]) -> EditTarget:
```

In `backend/dbaguy_backend/adapters/mysql/editing.py`, replace:

```python
        return read_only_relation(path, VIEWS if table_type == "VIEW" else "System views are read-only here")
    # An INVISIBLE column is not in the grid's SELECT *: it is left out of the target, so it is never the key (a generated
    # invisible primary key, my_row_id: the key is then a visible unique key, or there is none)
    columns = [_column(*row) for row in catalog.query(conn, _COLUMNS, (schema, name))
               if "INVISIBLE" not in (row[5] or "").upper()]
    parts: dict[str, list[str | None]] = {}
    for index, column in catalog.query(conn, _KEYS, (schema, name)):
        parts.setdefault(index, []).append(column)
```

with:

```python
        return read_only_relation(path, VIEWS if table_type == "VIEW" else "System views are read-only here")
    # An INVISIBLE column is not in the grid's SELECT *: it is left out of the target, so it is never the key (a generated
    # invisible primary key, my_row_id: the key is then a visible unique key, or there is none)
    columns = [column.edit_column() for column in read_columns(conn, schema, name) if not column.hidden]
    parts: dict[str, list[str | None]] = {}
    for index, column in catalog.query(conn, _KEYS, (schema, name)):
        parts.setdefault(index, []).append(column)
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
)
from ..cancel_window import CancelWindow
from ..sockets import enable_keepalive
from . import catalog, editing, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
from .types import column_meta, row_converter
```

with:

```python
)
from ..cancel_window import CancelWindow
from ..sockets import enable_keepalive
from ..columns import TableColumn
from . import catalog, columns, editing, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
from .types import column_meta, row_converter
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
        self._owner.finish(self._cursor, more_results=not self._failed)


class MysqlConnection(DatabaseConnection, RowEditing):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
```

with:

```python
        self._owner.finish(self._cursor, more_results=not self._failed)


class MysqlConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        return editing.edit_target(self._conn, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

```

with:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        return editing.edit_target(self._conn, path)

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

```

- [ ] **Step 7: SQL Server** (the catalog call goes through `_catalog_call`, on the worker thread, like the other catalog reads)

Create `backend/dbaguy_backend/adapters/mssql/columns.py`:

```python
"""A SQL Server relation's columns (spec ⑥ D-06-1), from catalog-view joins (never COL_NAME / OBJECT_NAME). An alias
type is read through its base type; a CLR type (hierarchyid, geometry, geography) has no base row. Grid editing's kind
and compare rules (spec ⑤ §3.2.2) are made here, once, for editing and data tools alike."""

from ...domain.edits import edit_kind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, PERIOD, ROWVERSION, SQL_VARIANT, TableColumn, TypeFamily, not_copied
from . import driver
from .catalog import object_id_sql, rows, type_sql
from .types import category_for_type

COLUMNS = """
SELECT c.name, t.name, COALESCE(st.name, t.name), t.is_assembly_type, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END,
       c.generated_always_type
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id AND st.is_user_defined = 0
WHERE c.object_id = {o} ORDER BY c.column_id
"""
# 402, sql_variant: 0 rows; json and vector (2025) have no = (G9)
_NOT_EXACT = frozenset({"float", "real", "text", "ntext", "image", "xml", "sql_variant", "json", "vector"})
_FRACTIONS = frozenset({"datetime2", "datetimeoffset", "time"})  # the driver reads microseconds: scale 7 never matches
_NON_UNICODE = frozenset({"char", "varchar", "text"})
_NCHARS = frozenset({"nchar", "nvarchar"})  # max_length counts bytes: two per character
# Scale 7 as text (A S7: the driver only reads microseconds; the text keeps the 7th digit and writes back equal). Style
# 126 (ISO 8601): `2026-10-05T12:34:56.1234567+08:00`. CAST's own `… 12:34:56.1234567 +08:00` cannot be written back
# with a `T` (241, A15), and without it a datetime would read by the login's DATEFORMAT (F1)
_SEVENTH_DIGIT = {"datetime2": "CONVERT(varchar(27), {}, 126)", "datetimeoffset": "CONVERT(varchar(34), {}, 126)",
                  "time": "CONVERT(varchar(16), {}, 126)"}
_FAMILIES: dict[str, TypeFamily] = {
    "bit": "bool", "tinyint": "int16", "smallint": "int16", "int": "int32", "bigint": "int64", "decimal": "decimal",
    "numeric": "decimal", "money": "money", "smallmoney": "money", "real": "float32", "char": "char", "nchar": "char",
    "varchar": "varchar", "nvarchar": "varchar", "text": "text", "ntext": "text", "binary": "binary",
    "varbinary": "binary", "image": "binary", "date": "date", "time": "time", "datetime": "timestamp",
    "smalldatetime": "timestamp", "datetime2": "timestamp", "datetimeoffset": "timestamp_tz",
    "uniqueidentifier": "uuid", "xml": "xml", "json": "json", "timestamp": "rowversion", "sql_variant": "variant",
    "hierarchyid": "variant", "geometry": "spatial", "geography": "spatial",
}


def _family(base: str, max_length: int, precision: int) -> TypeFamily:
    if base == "float":
        return "float64" if precision > 24 else "float32"
    if base in ("varchar", "nvarchar") and max_length == -1:
        return "text"  # (max)
    return _FAMILIES.get(base, "other")


def _sizes(base: str, max_length: int, precision: int, scale: int) -> dict:
    if base in ("decimal", "numeric"):
        return {"precision": precision, "scale": scale}
    if base in ("char", "varchar", "binary", "varbinary", "nchar", "nvarchar"):
        if max_length == -1:
            return {}
        return {"length": max_length // 2 if base in _NCHARS else max_length}
    if base in _FRACTIONS:
        return {"scale": scale}
    return {}


def column(
    name: str, type_name: str, base: str, assembly: bool, max_length: int, precision: int, scale: int,
    nullable: bool, identity: bool, computed: bool, default: int, generated_always: int,
) -> TableColumn:
    """One row of COLUMNS."""
    if computed:
        reason: str | None = "computed column"
    elif identity:
        reason = "IDENTITY column"
    elif base == "timestamp":
        reason = ROWVERSION
    elif generated_always:
        reason = PERIOD
    elif base == "sql_variant" or assembly:
        reason = f"{type_name} values are not edited here"
    else:
        reason = None
    exact = not (base in _NOT_EXACT or assembly or (base in _FRACTIONS and scale > 6))
    skipped = (GENERATED if computed else ROWVERSION if base == "timestamp" else PERIOD if generated_always
               else SQL_VARIANT if base == "sql_variant" else None)
    if base in _SEVENTH_DIGIT and scale > 6:
        read_as, literal = _SEVENTH_DIGIT[base], "datetime"
    elif assembly:
        read_as, literal = "CAST({} AS varbinary(max))", "binary"  # A S6: written back as 0x… (A1: hierarchyid too)
    else:
        read_as, literal = None, None
    family = _family(base, max_length, precision)
    return TableColumn(
        name=name, type_name=type_sql(type_name, max_length, precision, scale), family=family, nullable=bool(nullable),
        has_default=bool(default or identity or computed or generated_always) or base == "timestamp",
        kind=edit_kind(category_for_type(base)), compare="equals" if exact else "none", writable=reason is None,
        read_only_reason=reason, unicode=base not in _NON_UNICODE, **_sizes(base, max_length, precision, scale),
        identity="always" if identity else None, generated=bool(computed or generated_always),
        **(not_copied(skipped) if skipped else {}), read_as=read_as, literal=literal,
    )


def read_columns(conn: driver.Connection, object_sql: str) -> list[TableColumn]:
    """`object_sql`: an expression for the object's id (`object_id_sql`)."""
    return [column(*row) for row in rows(conn, COLUMNS.format(o=object_sql))]


def table_columns(conn: driver.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`; NotFound when it is gone."""
    schema, name = relation_from_path(path)
    found = read_columns(conn, object_id_sql(schema or "dbo", name))
    if not found:
        raise NotFound(f"Table {schema}.{name} not found")
    return found
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
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
# sys.tables' temporal_type (2016), is_node / is_edge (2017) and ledger_type (2022), read through dynamic SQL so that an
```

with:

```python
One batch on the metadata connection (READ UNCOMMITTED, LOCK_TIMEOUT 5000: a table locked by an uncommitted DDL
answers LOCKED_MESSAGE, CLAUDE.md); names come from catalog-view joins, never COL_NAME / OBJECT_NAME."""

from ...domain.edits import EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from . import driver
from .catalog import batch, object_id_sql, result_sets
from .columns import COLUMNS, column as table_column

_OBJECT = "SELECT type FROM sys.objects WHERE object_id = {o}"
# sys.tables' temporal_type (2016), is_node / is_edge (2017) and ledger_type (2022), read through dynamic SQL so that an
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
HISTORY_TABLE = "History table of a system-versioned table: read-only here"
GRAPH_TABLE = "Graph node and edge tables are read-only here"
LEDGER_TABLE = "Ledger tables are read-only here"
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
# Key columns only (not INCLUDE ones); no filtered, disabled or hypothetical index (a Tuning Advisor leftover: unique
# in the catalog, but it enforces nothing)
_KEYS = """
```

with:

```python
HISTORY_TABLE = "History table of a system-versioned table: read-only here"
GRAPH_TABLE = "Graph node and edge tables are read-only here"
LEDGER_TABLE = "Ledger tables are read-only here"
# Key columns only (not INCLUDE ones); no filtered, disabled or hypothetical index (a Tuning Advisor leftover: unique
# in the catalog, but it enforces nothing)
_KEYS = """
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
  AND i.is_hypothetical = 0 AND k.is_included_column = 0 AND k.key_ordinal > 0
ORDER BY i.name, k.key_ordinal
"""
# 402, sql_variant: 0 rows; json and vector (2025) have no = (G9)
_NOT_EXACT = frozenset({"float", "real", "text", "ntext", "image", "xml", "sql_variant", "json", "vector"})
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


def table_flags_reason(temporal_type: int, is_node: bool, is_edge: bool, ledger_type: int) -> str | None:
```

with:

```python
  AND i.is_hypothetical = 0 AND k.is_included_column = 0 AND k.key_ordinal > 0
ORDER BY i.name, k.key_ordinal
"""


def table_flags_reason(temporal_type: int, is_node: bool, is_edge: bool, ledger_type: int) -> str | None:
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, flags, columns, keys = result_sets(
        conn, batch(_OBJECT.format(o=o), _FLAGS.format(o=o), _COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
```

with:

```python
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, flags, columns, keys = result_sets(
        conn, batch(_OBJECT.format(o=o), _FLAGS.format(o=o), COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
    for index, primary, column in keys:
        parts.setdefault(index, (bool(primary), []))[1].append(column)
    candidates = [KeyCandidate(index, tuple(cols), primary) for index, (primary, cols) in parts.items()]
    return build_target(path, [_column(*row) for row in columns], candidates)
```

with:

```python
    for index, primary, column in keys:
        parts.setdefault(index, (bool(primary), []))[1].append(column)
    candidates = [KeyCandidate(index, tuple(cols), primary) for index, (primary, cols) in parts.items()]
    return build_target(path, [table_column(*row).edit_column() for row in columns], candidates)
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    MessageListener,
    ParsedUrl,
```

with:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    MessageListener,
    ParsedUrl,
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    invalid_text_error,
    transaction_open_error,
)
from . import catalog, driver, editing, freetds, introspection
from . import schema as mssql_schema
from .cancel import Call, CallKind, CancelControl, CancelledWhileWaiting, CancelPending
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
```

with:

```python
    invalid_text_error,
    transaction_open_error,
)
from ..columns import TableColumn
from . import catalog, columns, driver, editing, freetds, introspection
from . import schema as mssql_schema
from .cancel import Call, CallKind, CancelControl, CancelledWhileWaiting, CancelPending
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
        self._owner.finish_result(self, complete=self._exhausted)


class MssqlConnection(DatabaseConnection, RowEditing):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
```

with:

```python
        self._owner.finish_result(self, complete=self._exhausted)


class MssqlConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
        self._check_database(path)
        return self._metadata_call(lambda: editing.edit_target(self._conn, path))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._metadata_call(lambda: mssql_schema.build_snapshot(self._conn))

```

with:

```python
        self._check_database(path)
        return self._metadata_call(lambda: editing.edit_target(self._conn, path))

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        """Data tools run it on a job's own session: the catalog call, never the metadata session's settings (READ
        UNCOMMITTED would make a dump's reads dirty)."""
        self._check_database(path)
        return self._catalog_call(lambda: columns.table_columns(self._conn, path))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._metadata_call(lambda: mssql_schema.build_snapshot(self._conn))

```

- [ ] **Step 8: SQLite**

Create `backend/dbaguy_backend/adapters/sqlite/columns.py`:

```python
"""A SQLite relation's columns (spec ⑥ D-06-1), read by the declared type's affinity in SQLite's own order (⑤ ruling
E6; ANY in a STRICT table has none). Grid editing's kind and compare rules (spec ⑤ §3.2.2) are made here, once, for
editing and data tools alike. The rowid is never a column: `SELECT *` does not return it."""

import re
import sqlite3
from dataclasses import dataclass

from ...domain.edits import Compare, EditKind
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..columns import GENERATED, TableColumn, TypeFamily, not_copied

# The pragma takes the name only (a second argument raises); it matches the name without case, as SQLite does
_TABLE = "SELECT type, strict FROM pragma_table_list(?) WHERE schema = ?"
_COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?) ORDER BY cid'
_INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
_GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
_HIDDEN = 1  # pragma_table_xinfo.hidden: a virtual table's hidden column
_SIZES = re.compile(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)")


@dataclass(frozen=True)
class SqliteRelation:
    """pragma_table_list's row and the relation's pragma_table_xinfo and pragma_index_list rows."""

    kind: str  # table, view, virtual, shadow
    strict: bool
    rows: list[tuple]
    indexes: list[tuple]


def read_relation(conn: sqlite3.Connection, schema: str, name: str) -> SqliteRelation:
    found = conn.execute(_TABLE, (name, schema)).fetchone()
    if found is None:
        raise NotFound(f"Table {name} not found")
    kind, strict = found
    return SqliteRelation(kind, bool(strict), conn.execute(_COLUMNS, (name, schema)).fetchall(),
                          conn.execute(_INDEXES, (name, schema)).fetchall())


def affinity_kind(declared: str, strict: bool = False) -> tuple[EditKind, Compare]:
    """SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT; BLOB or no type; REAL, FLOA, DOUB; else NUMERIC
    — and inside NUMERIC what the declared name says: DATE / TIME, BOOL, JSON (ruling E6). REAL affinity holds floating
    point, which is not compared. In a STRICT table ANY keeps every value as given, like no type ('007' stays text);
    elsewhere it is NUMERIC ('007' is stored as 7)."""
    upper = declared.upper()
    if strict and upper == "ANY":
        return "other", "equals"
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


def _family(declared: str, strict: bool) -> tuple[TypeFamily, dict]:
    """The family a declared type names, by the same order as `affinity_kind`, with its sizes."""
    upper = declared.upper()
    sizes = _SIZES.search(upper)
    first = int(sizes.group(1)) if sizes else None
    second = int(sizes.group(2)) if sizes and sizes.group(2) else None
    if strict and upper == "ANY" or not upper.strip():
        return "other", {}
    if "INT" in upper:
        return "int64", {}
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        if "CHAR" in upper and first is not None:
            return ("varchar" if "VAR" in upper else "char"), {"length": first}
        return "text", {}
    if "BLOB" in upper:
        return "binary", {}
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "float64", {}
    if "DATE" in upper and "TIME" in upper or "TIMESTAMP" in upper:
        return "timestamp", {}
    if "DATE" in upper:
        return "date", {}
    if "TIME" in upper:
        return "time", {}
    if "BOOL" in upper:
        return "bool", {}
    if "JSON" in upper:
        return "json", {}
    return "decimal", ({"precision": first, "scale": second if second is not None else 0} if first else {})


def rowid_alias(relation: SqliteRelation) -> str | None:
    """INTEGER PRIMARY KEY is the rowid: never NULL, assigned when left out. Not when the key has an index of its own
    (origin pk): WITHOUT ROWID (NOT NULL, never assigned) or INTEGER PRIMARY KEY DESC (an ordinary nullable column)."""
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in relation.rows if pk > 0)]
    if len(key) != 1 or any(origin == "pk" for _, _, origin, _ in relation.indexes):
        return None
    declared = next(t for c, t, *_ in relation.rows if c == key[0])
    return key[0] if (declared or "").upper() == "INTEGER" else None


def columns_of(relation: SqliteRelation) -> list[TableColumn]:
    rowid = rowid_alias(relation)
    found = []
    for name, declared, notnull, default, _, hidden in relation.rows:
        if hidden == _HIDDEN:
            continue
        kind, compare = affinity_kind(declared or "", strict=relation.strict)
        family, sizes = _family(declared or "", relation.strict)
        generated = hidden in _GENERATED
        found.append(TableColumn(
            name=name, type_name=declared or "", family=family, nullable=not notnull and name != rowid,
            has_default=default is not None or generated or name == rowid, kind=kind, compare=compare,
            writable=not generated, read_only_reason=GENERATED if generated else None, **sizes,
            identity="by_default" if name == rowid else None, generated=generated,
            **(not_copied(GENERATED) if generated else {}),
        ))
    return found


def table_columns(conn: sqlite3.Connection, path: list[PathSegment]) -> list[TableColumn]:
    """The columns of the table or view at `path`; NotFound when it is gone."""
    schema, name = relation_from_path(path)
    return columns_of(read_relation(conn, schema or "main", name))
```

In `backend/dbaguy_backend/adapters/sqlite/editing.py`, replace:

```python

import sqlite3

from ...domain.edits import Compare, EditColumn, EditKind, EditTarget
from ...domain.errors import NotFound
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation

# The pragma takes the name only (a second argument raises); it matches the name without case, as SQLite does
_TABLE = "SELECT type, strict FROM pragma_table_list(?) WHERE schema = ?"
_READ_ONLY = {
    "view": VIEWS, "virtual": "Virtual tables are read-only here",
    "shadow": "Internal table of a virtual table: read-only here",  # e.g. fts5's _data, _idx, _content, _config
}
_GENERATED = frozenset({2, 3})  # pragma_table_xinfo.hidden: a VIRTUAL or STORED generated column
_COLUMNS = 'SELECT name, type, "notnull", dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?) ORDER BY cid'
_INDEXES = 'SELECT name, "unique", origin, partial FROM pragma_index_list(?, ?)'
_INDEX_COLUMNS = "SELECT name FROM pragma_index_info(?, ?) ORDER BY seqno"


def affinity_kind(declared: str, strict: bool = False) -> tuple[EditKind, Compare]:
    """SQLite's affinity rules in their order — INT; CHAR, CLOB, TEXT; BLOB or no type; REAL, FLOA, DOUB; else NUMERIC
    — and inside NUMERIC what the declared name says: DATE / TIME, BOOL, JSON (ruling E6). REAL affinity holds floating
    point, which is not compared. In a STRICT table ANY keeps every value as given, like no type ('007' stays text);
    elsewhere it is NUMERIC ('007' is stored as 7)."""
    upper = declared.upper()
    if strict and upper == "ANY":
        return "other", "equals"
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
    found = conn.execute(_TABLE, (name, schema)).fetchone()
    if found is None:
        raise NotFound(f"Table {name} not found")
    kind, strict = found
    if kind != "table":
        return read_only_relation(path, _READ_ONLY.get(kind, "This object is read-only here"))
    rows = conn.execute(_COLUMNS, (name, schema)).fetchall()
    indexes = conn.execute(_INDEXES, (name, schema)).fetchall()
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in rows if pk > 0)]
    # INTEGER PRIMARY KEY is the rowid: never NULL, assigned when left out. Not when the key has an index of its own
    # (origin pk): WITHOUT ROWID (NOT NULL, never assigned) or INTEGER PRIMARY KEY DESC (an ordinary nullable column).
    rowid = key[0] if (
        len(key) == 1 and next(t for c, t, *_ in rows if c == key[0]).upper() == "INTEGER"
        and not any(origin == "pk" for _, _, origin, _ in indexes)
    ) else None
    columns = []
    for column, declared, notnull, default, _, hidden in rows:
        kind_of, compare = affinity_kind(declared or "", strict=bool(strict))
        generated = hidden in _GENERATED
        columns.append(EditColumn(
            name=column, type_name=declared or "", kind=kind_of, nullable=not notnull and column != rowid,
            has_default=default is not None or generated or column == rowid, writable=not generated,
            read_only_reason="generated column" if generated else None, compare=compare,
        ))
    candidates = [KeyCandidate("PRIMARY KEY", tuple(key), True)] if key else []
    for index, unique, origin, partial in indexes:
        if not unique or partial or origin == "pk":
            continue
        parts = [part for (part,) in conn.execute(_INDEX_COLUMNS, (index, schema)).fetchall()]
```

with:

```python

import sqlite3

from ...domain.edits import EditTarget
from ...domain.metadata import PathSegment
from ...sql.select_builder import relation_from_path
from ..editing import VIEWS, KeyCandidate, build_target, read_only_relation
from .columns import columns_of, read_relation

_READ_ONLY = {
    "view": VIEWS, "virtual": "Virtual tables are read-only here",
    "shadow": "Internal table of a virtual table: read-only here",  # e.g. fts5's _data, _idx, _content, _config
}
_INDEX_COLUMNS = "SELECT name FROM pragma_index_info(?, ?) ORDER BY seqno"


def edit_target(conn: sqlite3.Connection, path: list[PathSegment]) -> EditTarget:
    schema, name = relation_from_path(path)
    schema = schema or "main"
    relation = read_relation(conn, schema, name)
    if relation.kind != "table":
        return read_only_relation(path, _READ_ONLY.get(relation.kind, "This object is read-only here"))
    columns = [column.edit_column() for column in columns_of(relation)]
    key = [column for _, column in sorted((pk, column) for column, _, _, _, pk, _ in relation.rows if pk > 0)]
    candidates = [KeyCandidate("PRIMARY KEY", tuple(key), True)] if key else []
    for index, unique, origin, partial in relation.indexes:
        if not unique or partial or origin == "pk":
            continue
        parts = [part for (part,) in conn.execute(_INDEX_COLUMNS, (index, schema)).fetchall()]
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    AdapterCapabilities,
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from . import editing, introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200
```

with:

```python
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from ..columns import TableColumn
from . import columns, editing, introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
            self._cursor.close()


class SqliteConnection(DatabaseConnection, RowEditing):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn  # isolation_level=None: the driver never begins a transaction on its own
        self._path = path
```

with:

```python
            self._cursor.close()


class SqliteConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn  # isolation_level=None: the driver never begins a transaction on its own
        self._path = path
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        try:
            return editing.edit_target(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

```

with:

```python
    def edit_target(self, path: list[PathSegment]) -> EditTarget:
        try:
            return editing.edit_target(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

```

- [ ] **Step 9: Run the tests and ⑤'s target tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_table_columns.py tests/test_editing_target.py`
Expected: `8 passed`.

Then ⑤'s live target and flow tests, which must be unchanged: `cd backend && .venv/bin/pytest -q tests/test_postgres_edits.py tests/test_mysql_edits.py tests/test_mssql_edits.py tests/test_sqlite_edits.py tests/test_edit_flows.py tests/test_edit_apply_live.py`. Expected: all pass, with nothing changed in them. They make and drop their own `zz_05a_*` objects; run them alone.

- [ ] **Step 10: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py \
  backend/dbaguy_backend/adapters/columns.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/dbaguy_backend/adapters/mssql/columns.py \
  backend/dbaguy_backend/adapters/mssql/editing.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/mysql/columns.py \
  backend/dbaguy_backend/adapters/mysql/editing.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/postgres/columns.py \
  backend/dbaguy_backend/adapters/postgres/editing.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/columns.py \
  backend/dbaguy_backend/adapters/sqlite/editing.py \
  backend/tests/test_table_columns.py
git commit -m "feat(06a): table columns with type family and read expression on every relational engine; edit targets read them"
```

---

### Task 5: `Dumping` and the PostgreSQL catalog (A2)

**Files:**
- Create: `backend/dbaguy_backend/adapters/dump.py`, `backend/dbaguy_backend/adapters/postgres/dump.py`, `backend/tests/test_postgres_dump_catalog.py`
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/adapters/postgres/ddl.py`, `backend/dbaguy_backend/domain/data_jobs.py`, `backend/tests/data_support.py`

**Interfaces:**
- Consumes: Task 4 (`read_columns`, `TableColumn`); `adapters/postgres/ddl.py` (`table_ddl`, sequence and type DDL, `pg_get_*`), the native loaders; `ServerInfo`'s version text.
- Produces:
  - `adapters/dump.py`: `DumpObject(kind, name, statements, drop, section, compound)`; `DumpTable(schema, table, name, columns, rows_estimate, bytes_estimate, only)`; `DumpCatalog(server, scope, consistency, pre, tables, post, not_included, warnings, snapshot_text)`; `DumpSession(setup, teardown, consistency)`; `Ordering(depends).order(keys)`: dependencies first, otherwise creation order; a cycle never stops it. `OWNERS_AND_PRIVILEGES`.
  - `adapters/base.Dumping`: `dump_session(scope) -> DumpSession` and `dump_catalog(scope, kinds, *, ddl=True) -> DumpCatalog`. `ddl=False` (the plan) lists the objects without reading their definitions. NotFound when the schema or a named relation is gone.
  - `domain/data_jobs`: `DumpScope(kind: "database" | "schema" | "tables", schema_name, names)` (validated: a schema for schema and tables, names only for tables, at most `MAX_DUMP_NAMES`); `DumpObjectKinds(views, sequences, types, routines, triggers)` (S17).
  - `adapters/postgres/ddl.py`: `TableParts` (`qname`, `columns`, `constraints`, `partition_of`, `relkind`, `partkey`, `unlogged`; `create(foreign_keys=True)`, `comments()`), `table_parts(conn, schema, name)`; `table_ddl` gives exactly what it gave (it is built from `table_parts`); `sequence_ddl`, `type_ddl` public.
  - `adapters/postgres/dump.py`: `SESSION` (`DateStyle = ISO, MDY`, `IntervalStyle = postgres`, `extra_float_digits = 3`, `search_path = ''`, `TimeZone = 'UTC'`, then `BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY`; teardown `ROLLBACK`), `SNAPSHOT_TEXT`, `dump_catalog(conn, scope, kinds, server, database, *, ddl=True)`; the order is in the module's docstring. `PostgresConnection.dump_session` / `dump_catalog`.
  - `tests/data_support.drop_statement` (a scratch schema goes with `CASCADE` on PostgreSQL).

- [ ] **Step 1: Probe A2.** Save as `<scratchpad>/probe_a2.py` and run from `backend/`: `.venv/bin/python <scratchpad>/probe_a2.py` (about 10 seconds; it creates nothing):

```python
"""06a Task 5 probe (spec ⑥ §10 A2): inside one REPEATABLE READ READ ONLY transaction, the catalog's pg_get_*
functions run between the FETCHes of a server-side cursor over 1,000,000 rows of about 230 bytes, and the client's
memory stays flat while it streams. Nothing is created: the rows come from generate_series.

Run from backend/: .venv/bin/python <this file>"""

import resource
import sys

sys.path.insert(0, ".")
import psycopg  # noqa: E402

from tests.support import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER  # noqa: E402


def peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576


conn = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                       autocommit=True)
try:
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    indexes = [oid for (oid,) in conn.execute("SELECT indexrelid FROM pg_index LIMIT 5").fetchall()]
    conn.execute("DECLARE zz_06_a2 NO SCROLL CURSOR FOR SELECT g, repeat('x', 200) || g, g * 1.25, "
                 "timestamp '2026-01-01' + g * interval '1 second' FROM generate_series(1, 1000000) g")
    rows, catalog_calls, before, at_100k = 0, 0, peak_mb(), None
    while batch := conn.execute("FETCH FORWARD 1000 FROM zz_06_a2").fetchall():
        rows += len(batch)
        if rows % 50_000 == 0:  # the catalog between two FETCHes
            conn.execute("SELECT pg_get_viewdef('pg_catalog.pg_tables'::regclass), "
                         "pg_get_functiondef('pg_catalog.now'::regproc), pg_get_indexdef(%s::oid)",
                         (indexes[catalog_calls % len(indexes)],)).fetchall()
            catalog_calls += 1
        if rows == 100_000:
            at_100k = peak_mb()
    conn.execute("CLOSE zz_06_a2")
    still = conn.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")
    isolation, read_only = still.fetchone()
    conn.execute("ROLLBACK")
    print(f"A2: {rows:,} rows, {catalog_calls} catalog reads between FETCHes, still {isolation} read_only={read_only}; "
          f"peak RSS {before:.0f} MB before, {at_100k:.0f} MB at 100,000 rows, {peak_mb():.0f} MB at the end")
finally:
    conn.close()
```

Expected (while planning): `A2: 1,000,000 rows, 20 catalog reads between FETCHes, still repeatable read read_only=on; peak RSS 69 MB before, 70 MB at 100,000 rows, 70 MB at the end`. If the catalog calls fail inside the cursor's transaction, `dump_catalog` must run before the first table's SELECT and never between FETCHes (it already does: the dump reads the whole catalog first, Task 9). If memory grows with the rows, the dump reads each table through the adapter's buffered path in 1,000-row pages, and Task 10 measures again.

- [ ] **Step 2: Write the failing tests**

`test_postgres_dump_catalog.py` builds a scratch schema `zz_06_dumpcat_*`, dropped with CASCADE, holding: an enum, a domain and a composite type; a standalone sequence, a serial and an identity column; a partitioned table and its partition; indexes and foreign keys; a function, one returning a table's row type, a trigger function and its trigger; a view created before what it uses (replaced later), a materialized view, and an aggregate (left out). It checks the order of `pre`, `tables` and `post`, what a `tables` scope brings, what is left out, and a plan without definitions.

In `backend/tests/data_support.py`, replace:

```python
    return unique_name(SCRATCH_PREFIX + kind)


@contextmanager
def dropping(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[list[tuple[str, str]]]:
    """A list the test appends `(kind, bare name)` to for every object it (or a job) creates in `db`'s schema. On
```

with:

```python
    return unique_name(SCRATCH_PREFIX + kind)


def drop_statement(db: ScratchDb, kind: str, name: str) -> str:
    """A scratch schema goes with everything in it (its objects are all the test's); anything else by its name in
    `db`'s schema."""
    if kind == "SCHEMA":
        return f"DROP SCHEMA IF EXISTS {db.q(name)}" + (" CASCADE" if db.engine == "postgres" else "")
    return f"DROP {kind} IF EXISTS {db.table(name)}"


@contextmanager
def dropping(db: ScratchDb, before_drop: Callable[[], None] | None = None) -> Iterator[list[tuple[str, str]]]:
    """A list the test appends `(kind, bare name)` to for every object it (or a job) creates in `db`'s schema. On
```

In `backend/tests/data_support.py`, replace:

```python
        for kind, name in reversed(made):
            assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped here"
            try:
                db.execute(f"DROP {kind} IF EXISTS {db.table(name)}")
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        return "; ".join(failures)
```

with:

```python
        for kind, name in reversed(made):
            assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch object: never dropped here"
            try:
                db.execute(drop_statement(db, kind, name))
            except Exception as exc:
                failures.append(f"{kind} {name}: {exc}")
        return "; ".join(failures)
```

Create `backend/tests/test_postgres_dump_catalog.py`:

```python
"""What a PostgreSQL dump writes and in which order (spec ⑥ §3.3.3, R-06-2): a scratch schema with every kind of
object, created partly out of order so that only the catalog's dependencies can order it. The dump → run round trip
is test_dump.py's. Scratch: one `zz_06_dumpcat_*` schema, dropped with what is in it."""

import pytest

from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound
from tests.data_support import dropping, scratch_name
from tests.edit_support import scratch_db
from tests.support import requires_pg

pytestmark = requires_pg


@pytest.fixture
def schema(tmp_path):
    db = scratch_db("postgres", tmp_path)
    try:
        with dropping(db) as made:
            name = scratch_name("dumpcat")
            made.append(("SCHEMA", name))
            s = f'"{name}"'
            db.execute(
                f"CREATE SCHEMA {s}",
                f"CREATE TYPE {s}.mood AS ENUM ('a', 'b')",
                f"CREATE DOMAIN {s}.pos AS int CHECK (VALUE > 0)",
                f"CREATE TYPE {s}.pair AS (x int, y text)",
                f"CREATE SEQUENCE {s}.counter START 100",
                f"CREATE TABLE {s}.parent (id serial PRIMARY KEY, m {s}.mood, p {s}.pos)",
                f'CREATE TABLE {s}."Child Rows" (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, '
                f"parent_id int REFERENCES {s}.parent (id), pr {s}.pair, note text)",
                f'CREATE INDEX child_note ON {s}."Child Rows" (note)',
                f"CREATE TABLE {s}.events (id int, at date, v text, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)",
                f"CREATE TABLE {s}.events_2026 PARTITION OF {s}.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
                f"CREATE INDEX events_v ON {s}.events (v)",
                f"CREATE FUNCTION {s}.add1(i int) RETURNS int LANGUAGE sql AS 'SELECT i + 1'",
                f"CREATE FUNCTION {s}.parents() RETURNS SETOF {s}.parent LANGUAGE sql AS 'SELECT * FROM {s}.parent'",
                f"CREATE FUNCTION {s}.touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
                f"CREATE TRIGGER touch BEFORE INSERT ON {s}.parent FOR EACH ROW EXECUTE FUNCTION {s}.touch()",
                f"CREATE VIEW {s}.v_top AS SELECT 1 AS x",  # older than what it will use
                f"CREATE VIEW {s}.v_base AS SELECT id AS y FROM {s}.parent",
                f"CREATE OR REPLACE VIEW {s}.v_top AS SELECT y AS x FROM {s}.v_base",
                f"CREATE MATERIALIZED VIEW {s}.mv AS SELECT count(*) AS n FROM {s}.parent",
                f"CREATE AGGREGATE {s}.total(int) (sfunc = int4pl, stype = int)",
                f"INSERT INTO {s}.parent (m, p) VALUES ('a', 1), ('b', 2)",
                f"SELECT setval('{s}.counter', 123)",
            )
            yield db, name
    finally:
        db.close()


def catalog(db, scope: DumpScope, kinds: DumpObjectKinds | None = None, ddl: bool = True):
    conn = db.open()
    try:
        conn.run("SET search_path = ''").close()  # the dump session's setting (P5)
        return conn.dump_catalog(scope, kinds or DumpObjectKinds(), ddl=ddl)
    finally:
        conn.close()


def names(objects, schema: str) -> list[tuple[str, str]]:
    return [(item.kind, item.name.replace(f"{schema}.", "")) for item in objects]


def test_a_schema_dump_orders_every_object_by_its_dependencies(schema):
    db, name = schema
    found = catalog(db, DumpScope(kind="schema", schema_name=name))
    assert names(found.pre, name) == [
        ("schema", name), ("type", "mood"), ("domain", "pos"), ("type", "pair"), ("sequence", "counter"),
        ("sequence", "parent_id_seq"), ("function", "add1(i integer)"), ("function", "touch()"),
        ("table", "parent"), ("table", "Child Rows"), ("table", "events"), ("table", "events_2026"),
    ]
    assert names(found.post, name) == [
        ("sequence value", "counter"), ("sequence value", "parent_id_seq"), ("sequence owner", "parent_id_seq"),
        ("sequence value", "Child Rows_id_seq"), ("index", "child_note"), ("index", "events_v"), ("foreign key", "Child Rows.Child Rows_parent_id_fkey"),
        ("view", "v_base"), ("view", "v_top"), ("materialized view", "mv"), ("function", "parents()"),
        ("trigger", "parent.touch"),
    ]
    assert [(t.name.replace(f"{name}.", ""), t.only) for t in found.tables] == [
        ("parent", True), ("Child Rows", True), ("events", False)]  # a partition's rows come with its parent's
    assert found.not_included == ("owners and privileges", "1 aggregates")
    statements = {(item.kind, item.name.replace(f"{name}.", "")): item.statements for item in (*found.pre, *found.post)}
    assert statements[("sequence", "counter")] == (
        f"CREATE SEQUENCE {name}.counter\n    AS bigint\n    INCREMENT BY 1\n    MINVALUE 1\n    "
        "MAXVALUE 9223372036854775807\n    START WITH 100\n    CACHE 1\n    NO CYCLE",)
    assert statements[("sequence value", "counter")] == (f"SELECT pg_catalog.setval('{name}.counter', 123, true)",)
    assert statements[("sequence value", "Child Rows_id_seq")] == (
        f"""SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('{name}."Child Rows"', 'id'), 1, false)""",)
    assert "REFERENCES" not in statements[("table", "Child Rows")][0]  # foreign keys come after the data
    assert statements[("index", "events_v")] == (f"CREATE INDEX events_v ON {name}.events USING btree (v)",)  # not ONLY
    assert statements[("table", "events_2026")][0].startswith(f"CREATE TABLE {name}.events_2026 PARTITION OF {name}.events")
    assert all(item.section == "data" for item in found.post if item.kind == "sequence value")


def test_a_tables_dump_brings_what_its_tables_need_and_leaves_out_foreign_keys_to_others(schema):
    db, name = schema
    found = catalog(db, DumpScope(kind="tables", schema_name=name, names=["Child Rows", "events"]))
    assert names(found.pre, name) == [("schema", name), ("type", "pair"), ("table", "Child Rows"), ("table", "events"),
                                      ("table", "events_2026")]
    assert names(found.post, name) == [("sequence value", "Child Rows_id_seq"), ("index", "child_note"),
                                       ("index", "events_v")]
    assert found.warnings == (f"Foreign key Child Rows_parent_id_fkey of {name}.Child Rows is left out: it references "
                              f"{name}.parent, which is not in this dump",)
    assert found.not_included == ("owners and privileges", "1 foreign keys to tables not in the dump")
    one = catalog(db, DumpScope(kind="tables", schema_name=name, names=["parent"]))
    assert ("function", "touch()") in names(one.pre, name) and ("trigger", "parent.touch") in names(one.post, name)
    assert ("sequence", "parent_id_seq") in names(one.pre, name) and ("type", "mood") in names(one.pre, name)
    with pytest.raises(NotFound):
        catalog(db, DumpScope(kind="tables", schema_name=name, names=["nope"]))


def test_left_out_kinds_and_a_plan_without_definitions(schema):
    db, name = schema
    kinds = DumpObjectKinds(views=False, sequences=False, types=False, routines=False, triggers=False)
    found = catalog(db, DumpScope(kind="schema", schema_name=name), kinds)
    pre = names(found.pre, name)
    assert ("sequence", "counter") not in pre and ("function", "add1(i integer)") not in pre
    assert ("sequence", "parent_id_seq") in pre and ("type", "mood") in pre  # what its tables need stays
    assert not [item for item in found.post if item.kind in ("view", "materialized view", "trigger")]
    plan = catalog(db, DumpScope(kind="schema", schema_name=name), ddl=False)
    assert names(plan.pre, name) == names(catalog(db, DumpScope(kind="schema", schema_name=name)).pre, name)
    assert all(not item.statements for item in (*plan.pre, *plan.post))
    assert [t.name for t in plan.tables] == [t.name for t in catalog(db, DumpScope(kind="schema", schema_name=name)).tables]
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_postgres_dump_catalog.py`
Expected: collection error (`DumpScope` cannot be imported).

- [ ] **Step 4: The engine-neutral catalog and the interface**

In `backend/dbaguy_backend/domain/data_jobs.py`, replace:

```python
outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel

from .profiles import Engine

```

with:

```python
outcome (R-06-12, ⑤ E1). Its WebSocket events, and what the job list shows of it (D-06-10)."""

from datetime import datetime
from typing import ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .profiles import Engine

```

Append to `backend/dbaguy_backend/domain/data_jobs.py`:

```python


MAX_DUMP_NAMES = 500  # relations one `tables` scope may name


class DumpScope(BaseModel):
    """What a dump covers (spec ⑥ §3.3.1): a whole database (PostgreSQL, SQL Server), one schema (MySQL: a database;
    SQLite: main), or named tables and views of one schema."""

    kind: Literal["database", "schema", "tables"]
    schema_name: str | None = Field(default=None, min_length=1, max_length=1024)  # schema and tables
    names: list[str] = Field(default_factory=list, max_length=MAX_DUMP_NAMES)  # tables: the relations, in that schema

    @model_validator(mode="after")
    def _a_schema_for_schema_and_tables(self) -> Self:
        if self.kind != "database" and self.schema_name is None:
            raise ValueError("a schema or tables dump names its schema")
        if (self.kind == "tables") != bool(self.names):
            raise ValueError("a tables dump names its tables, and only a tables dump does")
        return self


class DumpObjectKinds(BaseModel):
    """Which objects besides tables a dump writes (R-06-2); a table's own sequences and column types always come
    with it. Routines include MySQL events; views include SQL Server synonyms."""

    views: bool = True
    sequences: bool = True
    types: bool = True
    routines: bool = True
    triggers: bool = True
```

Create `backend/dbaguy_backend/adapters/dump.py`:

```python
"""What a dump reads from an engine's catalog (spec ⑥ §3.3, D-06-1), in one engine-neutral shape: the objects in the
order they must be created — before the data and after it — and the tables whose rows are written. Each engine's
`dump.py` knows its own catalog and ordering rules; `sql/dump_script.py` writes the file. Pure."""

from dataclasses import dataclass, field
from typing import Literal

from .columns import TableColumn

Consistency = Literal["snapshot", "per_table"]
Section = Literal["schema", "data"]
OWNERS_AND_PRIVILEGES = "owners and privileges"  # never dumped (R-06-2)


@dataclass(frozen=True)
class DumpObject:
    """One object's DDL: its statements in order (no terminators), the DROP … IF EXISTS a `drop_first` dump writes
    for it, and which part of the dump it belongs to — `schema` objects need the structure, `data` ones (PostgreSQL's
    setval) the rows."""

    kind: str  # schema, extension, type, sequence, function, procedure, table, view, materialized_view, index, …
    name: str  # as the file names it: schema.name (MySQL, SQLite: name)
    statements: tuple[str, ...]
    drop: str | None = None
    section: Section = "schema"
    compound: bool = False  # MySQL: a stored program, written between DELIMITER lines


@dataclass(frozen=True)
class DumpTable:
    """A table whose rows the dump writes."""

    schema: str | None
    table: str
    name: str  # as the file names it
    columns: tuple[TableColumn, ...]
    rows_estimate: int | None = None  # the catalog's estimate (≈): progress only
    bytes_estimate: int | None = None  # its size on disk: the free-space check (CR-4)
    only: bool = False  # PostgreSQL: FROM ONLY (an inheritance parent's own rows)


@dataclass(frozen=True)
class DumpCatalog:
    server: str  # `PostgreSQL 17.11`, as ServerInfo writes it
    scope: str  # `database dbaguy · schema public`: the header's text
    consistency: Consistency
    pre: tuple[DumpObject, ...] = ()  # before the data, in creation order
    tables: tuple[DumpTable, ...] = ()  # whose rows are written, in this order
    post: tuple[DumpObject, ...] = ()  # after the data, in creation order
    not_included: tuple[str, ...] = (OWNERS_AND_PRIVILEGES,)  # what the header lists as left out
    warnings: tuple[str, ...] = ()
    snapshot_text: str = "one consistent snapshot"  # the header's Snapshot line


@dataclass(frozen=True)
class DumpSession:
    """The statements a dump's own session sends before it reads (output settings, the snapshot) and after."""

    setup: tuple[str, ...]
    teardown: tuple[str, ...]
    consistency: Consistency


@dataclass
class Ordering:
    """Objects in dependency order: those whose dependencies are placed first, the rest in the catalog's own creation
    order (`key`) after them — a cycle, or a dependency the catalog does not record (dynamic SQL), never stops a dump
    (§3.3.3: "逐句＋跳過並繼續" completes the rest)."""

    depends: dict[str, set[str]] = field(default_factory=dict)

    def order(self, keys: list[str]) -> list[str]:
        """`keys` in creation order, each moved only as far down as one of its dependencies forces: the first key
        whose dependencies are all placed goes next; when none is ready (a cycle), the first one does."""
        wanted = set(keys)
        placed: list[str] = []
        done: set[str] = set()
        pending = list(keys)
        while pending:
            ready = next((key for key in pending if not (self.depends.get(key, set()) & wanted) - done), pending[0])
            placed.append(ready)
            done.add(ready)
            pending.remove(ready)
        return placed
```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
from typing import Any, ClassVar, Literal

from ..domain.couchbase import FieldSample, Keyspace
from ..domain.edits import EditTarget
from ..domain.errors import ValidationFailed
from ..domain.execution import ColumnMeta, ErrorPosition, Presentation, QueryContext, ServerMessage, StatementParams
```

with:

```python
from typing import Any, ClassVar, Literal

from ..domain.couchbase import FieldSample, Keyspace
from ..domain.data_jobs import DumpObjectKinds, DumpScope
from ..domain.edits import EditTarget
from ..domain.errors import ValidationFailed
from ..domain.execution import ColumnMeta, ErrorPosition, Presentation, QueryContext, ServerMessage, StatementParams
```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .columns import TableColumn
from .positions import offset_to_line_col


```

with:

```python
from ..domain.schema import RelationKind, SchemaSnapshot
from ..sql.dialects import Dialect
from .columns import TableColumn
from .dump import DumpCatalog, DumpSession
from .positions import offset_to_line_col


```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
        """The columns of the table, view or materialized view at `path`, in order; NotFound when it is gone."""


class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
```

with:

```python
        """The columns of the table, view or materialized view at `path`, in order; NotFound when it is gone."""


class Dumping(ABC):
    """A connection whose engine DbaGuy dumps (spec ⑥ R-06-2, capability `dump`): what its dump session sends
    around the reads, and what the dump writes, in order. On the owning worker thread, like every method."""

    @abstractmethod
    def dump_session(self, scope: DumpScope) -> DumpSession:
        """The output settings and the snapshot (R-06-4) the dump's own session sends before it reads, and what ends
        them."""

    @abstractmethod
    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        """The objects and tables of `scope`, in the order the dump writes them; `ddl` False (a dump plan): which
        there are, without their definitions. NotFound when the scope's schema or a named relation is gone."""


class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
```

- [ ] **Step 5: PostgreSQL's DDL parts** (a pure split of `table_ddl`: the object tree's DDL text does not change)

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
import psycopg

from ...domain.errors import NotFound, ValidationFailed
```

with:

```python
from dataclasses import dataclass

import psycopg

from ...domain.errors import NotFound, ValidationFailed
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
from .introspection import PgColumn, relation_oid, table_columns

_CONSTRAINTS_SQL = """
SELECT conname, pg_get_constraintdef(oid, true) FROM pg_constraint
WHERE conrelid = %(oid)s AND contype IN ('p', 'u', 'c', 'f', 'x')
ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 WHEN 'f' THEN 3 ELSE 4 END, conname
"""
```

with:

```python
from .introspection import PgColumn, relation_oid, table_columns

_CONSTRAINTS_SQL = """
SELECT conname, pg_get_constraintdef(oid, true), contype::text FROM pg_constraint
WHERE conrelid = %(oid)s AND contype IN ('p', 'u', 'c', 'f', 'x')
ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 WHEN 'f' THEN 3 ELSE 4 END, conname
"""
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
    return " ".join(parts)


def table_ddl(conn: psycopg.Connection, schema: str, name: str) -> tuple[str, list[PgColumn], str | None]:
    qname = qualified_pg(schema, name)
    oid = relation_oid(conn, qname)
    relkind, is_partition, partkey, comment, parent, bound, persistence = conn.execute(
```

with:

```python
    return " ".join(parts)


@dataclass(frozen=True)
class TableParts:
    """A table's DDL as parts: the tree shows them joined (`table_ddl`), a dump writes its foreign keys after the data
    (spec ⑥ §3.3.3)."""

    qname: str
    columns: list[PgColumn]
    constraints: list[tuple[str, str, str]]  # (name, definition, contype), primary key first
    partition_of: str | None  # a partition's CREATE TABLE … PARTITION OF …
    relkind: str
    partkey: str | None
    unlogged: bool
    indexes: list[str]  # the indexes no constraint owns, as CREATE INDEX statements
    comment: str | None

    def create(self, foreign_keys: bool = True) -> str:
        """CREATE TABLE, without its `;`. `foreign_keys` False leaves them out (a dump adds them after the data)."""
        if self.partition_of is not None:
            return self.partition_of
        body = [_column_def(c) for c in self.columns]
        body += [f"CONSTRAINT {quote_ident_pg(n)} {d}" for n, d, kind in self.constraints
                 if foreign_keys or kind != "f"]
        statement = f"CREATE {'UNLOGGED ' if self.unlogged else ''}TABLE {self.qname} (\n    " + ",\n    ".join(body) + "\n)"
        return statement + (f" PARTITION BY {self.partkey}" if self.relkind == "p" else "")

    def comments(self) -> list[str]:
        """COMMENT ON the table and its columns, without their `;`."""
        found = [f"COMMENT ON TABLE {self.qname} IS {quote_literal_pg(self.comment)}"] if self.comment else []
        return found + [f"COMMENT ON COLUMN {self.qname}.{quote_ident_pg(c.name)} IS {quote_literal_pg(c.comment)}"
                        for c in self.columns if c.comment]


def table_parts(conn: psycopg.Connection, schema: str, name: str) -> TableParts:
    qname = qualified_pg(schema, name)
    oid = relation_oid(conn, qname)
    relkind, is_partition, partkey, comment, parent, bound, persistence = conn.execute(
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
        "pg_get_expr(c.relpartbound, c.oid), c.relpersistence::text FROM pg_class c WHERE c.oid = %(oid)s",
        {"oid": oid},
    ).fetchone()
    columns = table_columns(conn, oid)
    blocks: list[str] = []
    if is_partition:
        sub_partitioning = f" PARTITION BY {partkey}" if relkind == "p" else ""  # a partition can itself be partitioned
        blocks.append(f"CREATE TABLE {qname} PARTITION OF {parent}\n    {bound}{sub_partitioning};")
    else:
        body = [_column_def(c) for c in columns]
        body += [f"CONSTRAINT {quote_ident_pg(n)} {d}" for n, d in conn.execute(_CONSTRAINTS_SQL, {"oid": oid})]
        unlogged = "UNLOGGED " if persistence == "u" else ""
        statement = f"CREATE {unlogged}TABLE {qname} (\n    " + ",\n    ".join(body) + "\n)"
        if relkind == "p":
            statement += f" PARTITION BY {partkey}"
        blocks.append(statement + ";")
    indexes = [f"{d};" for (d,) in conn.execute(_EXTRA_INDEXES_SQL, {"oid": oid})]
    if indexes:
        blocks.append("\n".join(indexes))
    comments = []
    if comment:
        comments.append(f"COMMENT ON TABLE {qname} IS {quote_literal_pg(comment)};")
    comments += [
        f"COMMENT ON COLUMN {qname}.{quote_ident_pg(c.name)} IS {quote_literal_pg(c.comment)};"
        for c in columns if c.comment
    ]
    if comments:
        blocks.append("\n".join(comments))
    return "\n\n".join(blocks), columns, comment


def _view_ddl(conn: psycopg.Connection, qname: str, materialized: bool) -> str:
```

with:

```python
        "pg_get_expr(c.relpartbound, c.oid), c.relpersistence::text FROM pg_class c WHERE c.oid = %(oid)s",
        {"oid": oid},
    ).fetchone()
    partition_of = None
    if is_partition:
        sub_partitioning = f" PARTITION BY {partkey}" if relkind == "p" else ""  # a partition can itself be partitioned
        partition_of = f"CREATE TABLE {qname} PARTITION OF {parent}\n    {bound}{sub_partitioning}"
    constraints = [] if is_partition else [(n, d, k) for n, d, k in conn.execute(_CONSTRAINTS_SQL, {"oid": oid})]
    return TableParts(
        qname=qname, columns=table_columns(conn, oid), constraints=constraints, partition_of=partition_of,
        relkind=relkind, partkey=partkey, unlogged=persistence == "u",
        indexes=[d for (d,) in conn.execute(_EXTRA_INDEXES_SQL, {"oid": oid})], comment=comment,
    )


def table_ddl(conn: psycopg.Connection, schema: str, name: str) -> tuple[str, list[PgColumn], str | None]:
    parts = table_parts(conn, schema, name)
    blocks = [parts.create() + ";"]
    if parts.indexes:
        blocks.append("\n".join(f"{d};" for d in parts.indexes))
    if comments := parts.comments():
        blocks.append("\n".join(f"{c};" for c in comments))
    return "\n\n".join(blocks), parts.columns, parts.comment


def _view_ddl(conn: psycopg.Connection, qname: str, materialized: bool) -> str:
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
    return definition.rstrip() + ";"


def _sequence_ddl(conn: psycopg.Connection, qname: str) -> str:
    type_, start, inc, minv, maxv, cache, cycle = _one(
        conn,
        "SELECT format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcache, s.seqcycle "
```

with:

```python
    return definition.rstrip() + ";"


def sequence_ddl(conn: psycopg.Connection, qname: str) -> str:
    type_, start, inc, minv, maxv, cache, cycle = _one(
        conn,
        "SELECT format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmin, s.seqmax, s.seqcache, s.seqcycle "
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
    )


def _type_ddl(conn: psycopg.Connection, qname: str) -> str:
    oid, typtype, base, not_null, default, typrelid = _one(
        conn,
        "SELECT t.oid, t.typtype::text, format_type(t.typbasetype, t.typtypmod), t.typnotnull, t.typdefault, t.typrelid "
```

with:

```python
    )


def type_ddl(conn: psycopg.Connection, qname: str) -> str:
    oid, typtype, base, not_null, default, typrelid = _one(
        conn,
        "SELECT t.oid, t.typtype::text, format_type(t.typbasetype, t.typtypmod), t.typnotnull, t.typdefault, t.typrelid "
```

In `backend/dbaguy_backend/adapters/postgres/ddl.py`, replace:

```python
        case NodeKind.FUNCTION | NodeKind.PROCEDURE:
            return ObjectDetail(**base, ddl=_function_ddl(conn, schema, last.name))
        case NodeKind.SEQUENCE:
            return ObjectDetail(**base, ddl=_sequence_ddl(conn, qname))
        case NodeKind.TYPE:
            return ObjectDetail(**base, ddl=_type_ddl(conn, qname))
        case NodeKind.INDEX:
            (definition,) = _one(conn, "SELECT pg_get_indexdef(to_regclass(%(q)s))", {"q": qname}, qname)
            return ObjectDetail(**base, ddl=f"{definition};")
```

with:

```python
        case NodeKind.FUNCTION | NodeKind.PROCEDURE:
            return ObjectDetail(**base, ddl=_function_ddl(conn, schema, last.name))
        case NodeKind.SEQUENCE:
            return ObjectDetail(**base, ddl=sequence_ddl(conn, qname))
        case NodeKind.TYPE:
            return ObjectDetail(**base, ddl=type_ddl(conn, qname))
        case NodeKind.INDEX:
            (definition,) = _one(conn, "SELECT pg_get_indexdef(to_regclass(%(q)s))", {"q": qname}, qname)
            return ObjectDetail(**base, ddl=f"{definition};")
```

- [ ] **Step 6: The PostgreSQL catalog**

Create `backend/dbaguy_backend/adapters/postgres/dump.py`:

```python
"""What a PostgreSQL dump writes, and in which order (spec ⑥ §3.3.3), read inside the dump's snapshot on the native
loaders. The dump session's `search_path = ''` (P5) makes format_type, defaults and every pg_get_* function qualify
names.

Order. Before the data: schemas, extensions, types (enums first), sequences, the routines whose signatures use no
relation's row type (their bodies are not checked: the header's check_function_bodies = false, P9), tables (a
partition after its parent) without their foreign keys, with their comments. After the data: setval and OWNED BY,
indexes, foreign keys, views and materialized views by their dependencies (pg_depend through pg_rewrite), the routines
that use a row type (P9: 42704 before it exists), triggers. What the catalog cannot order (a cycle) follows in creation
order (oid). A `tables` scope brings each table's partitions, the sequences its columns own or use, the types its
columns use and its triggers with their functions."""

import psycopg

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound
from ...sql.dialects import Dialect, quote_ident, quote_literal
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering
from . import columns as pg_columns
from .ddl import sequence_ddl, table_parts, type_ddl

SESSION = DumpSession(
    setup=("SET DateStyle = ISO, MDY", "SET IntervalStyle = postgres", "SET extra_float_digits = 3",
           "SET search_path = ''", "SET TimeZone = 'UTC'", "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY"),
    teardown=("ROLLBACK",), consistency="snapshot",
)
SNAPSHOT_TEXT = "one consistent snapshot (REPEATABLE READ)"
_NOT_EXTENSION = ("NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = '{catalog}'::regclass AND e.objid = {oid} "
                  "AND e.deptype = 'e')")
_SCHEMAS = ("SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%%' "
            "AND n.nspname <> 'information_schema' ORDER BY n.nspname")
_SCHEMA = "SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname = %(name)s"
_RELATIONS = f"""
SELECT c.oid, n.nspname::text, c.relname::text, c.relkind::text, c.relispartition,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END,
       CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) END
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relnamespace = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND {_NOT_EXTENSION.format(catalog="pg_class", oid="c.oid")}
ORDER BY c.oid
"""
_PARTITIONS = """
WITH RECURSIVE part (oid, parent) AS (
    SELECT inhrelid, inhparent FROM pg_inherits WHERE inhparent = ANY(%(tables)s)
  UNION
    SELECT h.inhrelid, h.inhparent FROM pg_inherits h JOIN part p ON h.inhparent = p.oid
)
SELECT oid, parent FROM part
"""
# A sequence a column owns: a serial's (deptype a) or an identity's (i, which the table's DDL creates itself)
_OWNED_SEQUENCES = """
SELECT d.objid, d.deptype::text, d.refobjid, a.attname::text
FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
  AND d.objid IN (SELECT oid FROM pg_class WHERE relkind = 'S') AND d.refobjid = ANY(%(tables)s)
"""
_ANY_OWNED = ("SELECT objid FROM pg_depend WHERE classid = 'pg_class'::regclass AND deptype IN ('a', 'i') "
              "AND objid = ANY(%(oids)s)")
# Sequences a table's defaults use without owning them (nextval('s'))
_DEFAULT_SEQUENCES = """
SELECT DISTINCT d.refobjid FROM pg_attrdef ad
JOIN pg_depend d ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid AND d.refclassid = 'pg_class'::regclass
WHERE ad.adrelid = ANY(%(tables)s) AND d.refobjid IN (SELECT oid FROM pg_class WHERE relkind = 'S')
"""
# Enums, domains and standalone composites, and (to be counted as left out) range, multirange and base types; never an
# array type, nor a table's own row type
_TYPES = f"""
SELECT t.oid, n.nspname::text, t.typname::text, t.typtype::text
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typnamespace = ANY(%(schemas)s) AND {_NOT_EXTENSION.format(catalog="pg_type", oid="t.oid")}
  AND (t.typtype IN ('e', 'd', 'r', 'm') OR (t.typtype = 'b' AND t.typelem = 0)
       OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))
ORDER BY t.typtype <> 'e', t.oid
"""
# The types a table's columns use: through domains, arrays and composites, however deep
_COLUMN_TYPES = """
WITH RECURSIVE used (oid) AS (
    SELECT a.atttypid FROM pg_attribute a WHERE a.attrelid = ANY(%(tables)s) AND a.attnum > 0 AND NOT a.attisdropped
  UNION
    SELECT inner_type.oid FROM used u JOIN pg_type t ON t.oid = u.oid
    CROSS JOIN LATERAL (
        SELECT t.typbasetype WHERE t.typtype = 'd'
        UNION ALL SELECT t.typelem WHERE t.typelem <> 0
        UNION ALL SELECT a.atttypid FROM pg_attribute a WHERE t.typtype = 'c' AND a.attrelid = t.typrelid
                                                          AND a.attnum > 0 AND NOT a.attisdropped
    ) inner_type (oid)
)
SELECT oid FROM used
"""
_ROUTINES = f"""
SELECT p.oid, n.nspname::text, p.proname::text, p.prokind::text, pg_get_function_identity_arguments(p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.pronamespace = ANY(%(schemas)s) AND {_NOT_EXTENSION.format(catalog="pg_proc", oid="p.oid")}
ORDER BY p.oid
"""
# Routines whose signature uses a relation's row type: they wait for that relation (P9)
_ROW_TYPE_ROUTINES = """
SELECT DISTINCT d.objid FROM pg_depend d JOIN pg_type t ON d.refclassid = 'pg_type'::regclass AND d.refobjid = t.oid
JOIN pg_class c ON c.oid = t.typrelid
WHERE d.classid = 'pg_proc'::regclass AND d.objid = ANY(%(routines)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
"""
_EXTENSIONS = """
SELECT e.extname::text, n.nspname::text FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extnamespace = ANY(%(schemas)s) AND e.extname <> 'plpgsql' ORDER BY e.oid
"""
_EXTENSION_OBJECTS = """
SELECT count(*) FROM pg_depend d WHERE d.deptype = 'e' AND (
    (d.classid = 'pg_class'::regclass AND d.objid IN (SELECT oid FROM pg_class WHERE relnamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_proc'::regclass AND d.objid IN (SELECT oid FROM pg_proc WHERE pronamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_type'::regclass
     AND d.objid IN (SELECT oid FROM pg_type WHERE typnamespace = ANY(%(schemas)s) AND typelem = 0)))
"""
# Indexes no constraint owns; a partition's index attached to its parent's is made again by the parent's, written ON
# (not ON ONLY) so that it reaches every partition
_INDEXES = """
SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid) FROM pg_index i
WHERE i.indrelid = ANY(%(tables)s)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid)
  AND NOT EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = i.indexrelid)
ORDER BY i.indexrelid
"""
_FOREIGN_KEYS = """
SELECT k.conrelid, k.confrelid, k.conname::text, pg_get_constraintdef(k.oid), k.confrelid::regclass::text
FROM pg_constraint k WHERE k.contype = 'f' AND k.conrelid = ANY(%(tables)s) AND k.conparentid = 0 ORDER BY k.oid
"""
# A partition's copy of its parent's trigger has a parent (tgparentid, 13+): the parent's makes it again
_TRIGGERS = """
SELECT t.tgrelid, t.tgname::text, t.tgfoid, pg_get_triggerdef(t.oid, false) FROM pg_trigger t
WHERE NOT t.tgisinternal AND t.tgrelid = ANY(%(tables)s) AND t.tgparentid = 0 ORDER BY t.oid
"""
_VIEW_USES = """
SELECT DISTINCT r.ev_class, d.refobjid FROM pg_rewrite r
JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid AND d.refclassid = 'pg_class'::regclass
WHERE r.ev_class = ANY(%(views)s) AND d.refobjid <> r.ev_class
"""
_LEFT_OUT = {"r": "range types", "m": "multirange types", "b": "base types", "a": "aggregates",
             "f": "foreign tables"}
_ROUTINE_KIND = {"f": "function", "w": "function", "p": "procedure"}


def _q(*parts: str) -> str:
    return ".".join(quote_ident(Dialect.POSTGRES, part) for part in parts)


class _Reader:
    """One catalog read; `ddl` False (a dump plan) reads which objects there are, not their definitions."""

    def __init__(self, conn: psycopg.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.scope, self.kinds, self.ddl = conn, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}
        self.warnings: list[str] = []
        self.relations: dict[int, tuple] = {}  # oid → _RELATIONS row
        self.owners: dict[int, tuple[int, str]] = {}  # sequence → (table, column) it belongs to
        self.identity: set[int] = set()  # sequences of identity columns

    def _ddl(self, *statements: str) -> tuple[str, ...]:
        return statements if self.ddl else ()

    def rows(self, sql: str, **params: object) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(sql, params)]

    def leave_out(self, what: str, count: int = 1) -> None:
        self.left_out[what] = self.left_out.get(what, 0) + count

    def name(self, oid: int) -> str:
        _, schema, name, *_ = self.relations[oid]
        return f"{schema}.{name}"

    def qualified(self, oid: int) -> str:
        _, schema, name, *_ = self.relations[oid]
        return _q(schema, name)

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        schemas = self._schemas()
        schema_oids = [oid for oid, _ in schemas]
        rows = self.rows(_RELATIONS, schemas=schema_oids)
        self.relations = {row[0]: row for row in rows}
        picked = self._picked(rows)
        tables = self._table_order([row for row in picked if row[3] in ("r", "p")])
        views = [row for row in picked if row[3] in ("v", "m") and (self.kinds.views or self.named)]
        self.leave_out(_LEFT_OUT["f"], sum(row[3] == "f" for row in picked))
        table_oids = [row[0] for row in tables]
        sequences = self._sequences(rows, table_oids)
        triggers = self.rows(_TRIGGERS, tables=table_oids) if self.kinds.triggers and table_oids else []
        routines, late = self._routines(schema_oids, {row[2] for row in triggers})
        pre = [DumpObject("schema", name, self._ddl(f"CREATE SCHEMA IF NOT EXISTS {_q(name)}")) for _, name in schemas]
        if not self.named:
            pre += [DumpObject("extension", name,
                               self._ddl(f"CREATE EXTENSION IF NOT EXISTS {_q(name)} WITH SCHEMA {_q(schema)}"))
                    for name, schema in self.rows(_EXTENSIONS, schemas=schema_oids)]
            self.leave_out("extension objects", self.rows(_EXTENSION_OBJECTS, schemas=schema_oids)[0][0])
        pre += self._types(schema_oids, table_oids)
        pre += [self._sequence(oid) for oid in sequences]
        pre += [self._routine(row) for row in routines if row[0] not in late]
        pre += [self._table(row) for row in tables]
        post = [*self._sequence_values(sequences, table_oids), *self._indexes(table_oids),
                *self._foreign_keys(table_oids), *self._views(views),
                *(self._routine(row) for row in routines if row[0] in late), *(self._trigger(row) for row in triggers)]
        data = [self._data(row) for row in tables if not row[4]]  # a partition's rows come with its parent's
        return pre, data, post

    def _schemas(self) -> list[tuple[int, str]]:
        if self.scope.kind == "database":
            return self.rows(_SCHEMAS)
        found = self.rows(_SCHEMA, name=self.scope.schema_name)
        if not found:
            raise NotFound(f"Schema {self.scope.schema_name} not found")
        return found

    def _picked(self, rows: list[tuple]) -> list[tuple]:
        """The relations of the scope; a `tables` scope's partitioned tables bring their partitions."""
        if not self.named:
            return [row for row in rows if row[3] != "S"]
        wanted = set(self.scope.names)
        picked = [row for row in rows if row[2] in wanted and row[3] != "S"]
        if missing := wanted - {row[2] for row in picked}:
            raise NotFound(f"Not found in {self.scope.schema_name}: {', '.join(sorted(missing))}")
        parents = [row[0] for row in picked if row[3] == "p"]
        have = {row[0] for row in picked}
        partitions = {oid for oid, _ in self.rows(_PARTITIONS, tables=parents)} if parents else set()
        return picked + [self.relations[oid] for oid in sorted(partitions - have) if oid in self.relations]

    def _table_order(self, tables: list[tuple]) -> list[tuple]:
        """Creation order, a partition after its parent (it may have been attached later than it was created)."""
        oids = [row[0] for row in tables]
        ordering = Ordering()
        partitioned = [row[0] for row in tables if row[3] == "p"]
        for oid, parent in (self.rows(_PARTITIONS, tables=partitioned) if partitioned else []):
            ordering.depends.setdefault(str(oid), set()).add(str(parent))
        return [self.relations[int(key)] for key in ordering.order([str(oid) for oid in oids])]

    def _types(self, schema_oids: list[int], table_oids: list[int]) -> list[DumpObject]:
        used = {oid for (oid,) in self.rows(_COLUMN_TYPES, tables=table_oids)} if table_oids else set()
        found = []
        for oid, schema, name, typtype in self.rows(_TYPES, schemas=schema_oids):
            if typtype in _LEFT_OUT:
                if not self.named or oid in used:
                    self.leave_out(_LEFT_OUT[typtype])
                continue
            if oid not in used and (self.named or not self.kinds.types):
                continue
            qname = _q(schema, name)
            statements = (type_ddl(self.conn, qname).rstrip(";"),) if self.ddl else ()
            word = "DOMAIN" if typtype == "d" else "TYPE"
            found.append(DumpObject(word.lower(), f"{schema}.{name}", statements, f"DROP {word} IF EXISTS {qname}"))
        return found

    def _sequences(self, rows: list[tuple], table_oids: list[int]) -> list[int]:
        """Standalone sequences (unless left out), and those a dumped table's serial owns or its defaults use; never an
        identity's, which the table's DDL makes (its value is set after the data all the same)."""
        owned = self.rows(_OWNED_SEQUENCES, tables=table_oids) if table_oids else []
        self.owners = {sequence: (table, column) for sequence, _, table, column in owned}
        self.identity = {sequence for sequence, deptype, _, _ in owned if deptype == "i"}
        used = {sequence for sequence, deptype, _, _ in owned if deptype == "a"}
        if table_oids:
            used |= {oid for (oid,) in self.rows(_DEFAULT_SEQUENCES, tables=table_oids)}
        if self.named or not self.kinds.sequences:
            return sorted(used - self.identity)
        sequences = [row[0] for row in rows if row[3] == "S"]
        anyone_owns = {oid for (oid,) in self.rows(_ANY_OWNED, oids=sequences)} if sequences else set()
        return sorted((used | (set(sequences) - anyone_owns)) - self.identity)

    def _sequence(self, oid: int) -> DumpObject:
        qname = self.qualified(oid)
        statements = (sequence_ddl(self.conn, qname).rstrip(";"),) if self.ddl else ()
        return DumpObject("sequence", self.name(oid), statements, f"DROP SEQUENCE IF EXISTS {qname}")

    def _sequence_values(self, sequences: list[int], table_oids: list[int]) -> list[DumpObject]:
        """setval for every sequence of the dump, an identity's too (P2: rows written with their ids leave it where it
        was), and OWNED BY for a serial's."""
        found = []
        for oid in [*sequences, *sorted(self.identity)]:
            statements: list[str] = []
            if self.ddl:
                last_value, called = self.rows(f"SELECT last_value, is_called FROM {self.qualified(oid)}")[0]
                if oid in self.identity:  # its name is the server's to choose: found again through the column
                    table, column = self.owners[oid]
                    target = (f"pg_catalog.pg_get_serial_sequence("
                              f"{quote_literal(Dialect.POSTGRES, self.qualified(table))}, "
                              f"{quote_literal(Dialect.POSTGRES, column)})")
                else:
                    target = quote_literal(Dialect.POSTGRES, self.qualified(oid))
                statements.append(f"SELECT pg_catalog.setval({target}, {last_value}, {'true' if called else 'false'})")
            found.append(DumpObject("sequence value", self.name(oid), tuple(statements), section="data"))
            if oid in self.owners and oid not in self.identity and self.owners[oid][0] in table_oids and self.ddl:
                table, column = self.owners[oid]
                found.append(DumpObject("sequence owner", self.name(oid), (
                    f"ALTER SEQUENCE {self.qualified(oid)} OWNED BY {self.qualified(table)}.{_q(column)}",)))
        return found

    def _routines(self, schema_oids: list[int], trigger_functions: set[int]) -> tuple[list[tuple], set[int]]:
        """The routines to write (a `tables` scope: only its triggers' functions), and those that wait for a row
        type."""
        found = []
        for row in self.rows(_ROUTINES, schemas=schema_oids):
            oid, kind = row[0], row[3]
            if kind == "a":
                if not self.named:
                    self.leave_out(_LEFT_OUT["a"])
            elif oid in trigger_functions or (not self.named and self.kinds.routines):
                found.append(row)
        late = {oid for (oid,) in self.rows(_ROW_TYPE_ROUTINES, routines=[row[0] for row in found])} if found else set()
        return found, late

    def _routine(self, row: tuple) -> DumpObject:
        oid, schema, name, kind, arguments = row
        statements: tuple[str, ...] = ()
        if self.ddl:
            (definition,) = self.rows("SELECT pg_get_functiondef(%(oid)s)", oid=oid)[0]
            statements = (definition.rstrip().rstrip(";"),)
        word = "PROCEDURE" if kind == "p" else "FUNCTION"
        return DumpObject(_ROUTINE_KIND[kind], f"{schema}.{name}({arguments})", statements,
                          f"DROP {word} IF EXISTS {_q(schema, name)}({arguments})")

    def _table(self, row: tuple) -> DumpObject:
        _, schema, name, _, is_partition, *_ = row
        statements: tuple[str, ...] = ()
        if self.ddl:
            parts = table_parts(self.conn, schema, name)
            statements = (parts.create(foreign_keys=False), *parts.comments())
        drop = None if is_partition else f"DROP TABLE IF EXISTS {_q(schema, name)}"  # a partition goes with it
        return DumpObject("table", f"{schema}.{name}", statements, drop)

    def _data(self, row: tuple) -> DumpTable:
        oid, schema, name, relkind, _, estimate, size = row
        return DumpTable(schema=schema, table=name, name=f"{schema}.{name}",
                         columns=tuple(pg_columns.read_columns(self.conn, oid)), rows_estimate=estimate,
                         bytes_estimate=size, only=relkind == "r")

    def _indexes(self, table_oids: list[int]) -> list[DumpObject]:
        if not table_oids or not self.ddl:
            return []
        return [DumpObject("index", name, (definition.replace(" ON ONLY ", " ON ", 1),))
                for name, definition in self.rows(_INDEXES, tables=table_oids)]

    def _foreign_keys(self, table_oids: list[int]) -> list[DumpObject]:
        """Only those whose referenced table is in the dump (§3.3.1): one to a table left out fails on an empty
        database, and rolls an all-or-nothing run back. The header lists the others."""
        if not table_oids:
            return []
        dumped, found = set(table_oids), []
        for table, referenced, name, definition, referenced_name in self.rows(_FOREIGN_KEYS, tables=table_oids):
            if referenced not in dumped:
                self.warnings.append(f"Foreign key {name} of {self.name(table)} is left out: it references "
                                     f"{referenced_name}, which is not in this dump")
                self.leave_out("foreign keys to tables not in the dump")
                continue
            statements = (f"ALTER TABLE ONLY {self.qualified(table)} ADD CONSTRAINT {_q(name)} {definition}",)
            found.append(DumpObject("foreign key", f"{self.name(table)}.{name}", statements if self.ddl else ()))
        return found

    def _views(self, views: list[tuple]) -> list[DumpObject]:
        oids = [row[0] for row in views]
        ordering = Ordering()
        for view, uses in (self.rows(_VIEW_USES, views=oids) if oids else []):
            ordering.depends.setdefault(str(view), set()).add(str(uses))
        found = []
        for key in ordering.order([str(oid) for oid in oids]):
            oid, schema, name, relkind, *_ = self.relations[int(key)]
            head = "MATERIALIZED VIEW" if relkind == "m" else "VIEW"
            statements: tuple[str, ...] = ()
            if self.ddl:
                (definition,) = self.rows("SELECT pg_get_viewdef(%(oid)s::oid, true)", oid=oid)[0]
                statements = (f"CREATE {head} {_q(schema, name)} AS\n{definition.rstrip().rstrip(';')}",)
            found.append(DumpObject(head.lower(), f"{schema}.{name}", statements,
                                    f"DROP {head} IF EXISTS {_q(schema, name)}"))
        return found

    def _trigger(self, row: tuple) -> DumpObject:
        table, name, _, definition = row
        return DumpObject("trigger", f"{self.name(table)}.{name}", (definition,) if self.ddl else ())


def dump_catalog(
    conn: psycopg.Connection, scope: DumpScope, kinds: DumpObjectKinds, server: str, database: str, *, ddl: bool = True,
) -> DumpCatalog:
    reader = _Reader(conn, scope, kinds, ddl)
    pre, tables, post = reader.read()
    where = f"database {database}" + (f" · schema {scope.schema_name}" if scope.schema_name else "")
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items() if count)]
    return DumpCatalog(server=server, scope=where, consistency="snapshot", pre=tuple(pre), tables=tuple(tables),
                       post=tuple(post), not_included=tuple(left_out), warnings=tuple(reader.warnings),
                       snapshot_text=SNAPSHOT_TEXT)
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
from ..cancel_window import CancelWindow
from ..positions import offset_to_line_col
from ..sql_text import first_keyword
from ..columns import TableColumn
from . import columns, ddl, editing, introspection
from . import schema as pg_schema
from .loaders import category_for_oid, configure_user_loaders

```

with:

```python
from ..cancel_window import CancelWindow
from ..positions import offset_to_line_col
from ..sql_text import first_keyword
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from . import columns, ddl, dump, editing, introspection
from . import schema as pg_schema
from .loaders import category_for_oid, configure_user_loaders

```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
            logger.warning("Error while closing PostgreSQL cursor: {}", exc)


class PostgresConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: psycopg.Connection, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
```

with:

```python
            logger.warning("Error while closing PostgreSQL cursor: {}", exc)


class PostgresConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(self, conn: psycopg.Connection, read_only: bool = False) -> None:
        self._conn = conn
        self._read_only = read_only
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        self._check_database(path)
        return columns.table_columns(self._conn, path)

    def schema_snapshot(self) -> SchemaSnapshot:
        # The connection's own (native-loader) cursors: arrays come back as lists, unlike the text-first user cursors.
```

with:

```python
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        self._check_database(path)
        return columns.table_columns(self._conn, path)

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.SESSION

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        try:
            return dump.dump_catalog(self._conn, scope, kinds, self.server_info().version,
                                     self.current_database() or "", ddl=ddl)
        except psycopg.Error as exc:
            raise self.translate_error(exc, "dump catalog") from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        # The connection's own (native-loader) cursors: arrays come back as lists, unlike the text-first user cursors.
```

- [ ] **Step 7: Run the tests and the introspection tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_postgres_dump_catalog.py`
Expected: `3 passed`.

Then the object tree's DDL, which `table_parts` must leave unchanged: `cd backend && .venv/bin/pytest -q tests/test_postgres_introspection.py`. Expected: all pass, nothing changed in them (they make and drop their own `pytest_subpart` schema; run them alone).

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py \
  backend/dbaguy_backend/adapters/dump.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/postgres/ddl.py \
  backend/dbaguy_backend/adapters/postgres/dump.py \
  backend/dbaguy_backend/domain/data_jobs.py \
  backend/tests/data_support.py \
  backend/tests/test_postgres_dump_catalog.py
git commit -m "feat(06a): dump catalog interface; the PostgreSQL catalog in dependency order"
```

---

### Task 6: The MySQL and SQLite catalogs (A3, A5, A14)

**Files:**
- Create: `backend/dbaguy_backend/adapters/mysql/dump.py`, `backend/dbaguy_backend/adapters/sqlite/dump.py`, `backend/tests/test_mysql_sqlite_dump_catalog.py`
- Modify: `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/sql/splitter.py`, `backend/tests/data_support.py`

**Interfaces:**
- Consumes: Task 5 (`Dumping`, `DumpCatalog`, `Ordering`, `DumpScope`); Task 4 (`read_columns`, `read_relation`, `columns_of`); `sql/splitter`'s account scanner (made public as `after_account`); MySQL `catalog.query`, `SHOW CREATE …`; SQLite `sqlite_master`, `pragma_table_list`.
- Produces:
  - `sql/splitter.after_account(tokens, i)` (public; the MySQL dump uses it to find a `DEFINER=` account).
  - `adapters/mysql/dump.py`: `session(database)` (`USE <db>`, `SET NAMES utf8mb4`, `SET time_zone = '+00:00'`, `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`; teardown `ROLLBACK`); `strip_definer(sql)` (A14); `dump_catalog(conn, database, scope, kinds, server, *, ddl=True)`. A `database` scope → `ValidationFailed` ("A MySQL dump covers one database: dump its schema node", S18). Stored programs are `compound` objects: `SET SESSION sql_mode = <its own>` first, then the program (S20). `RESTORE_SQL_MODE`.
  - `adapters/sqlite/dump.py`: `SESSION` (`BEGIN` / `ROLLBACK`), `dump_catalog(conn, scope, kinds, *, ddl=True)`; counters as `UPDATE`/`INSERT` of `sqlite_sequence` after the data; virtual tables and their shadow tables, and SQLite's own tables, left out (listed).
  - `MysqlConnection` / `SqliteConnection`: `dump_session`, `dump_catalog`.
  - `tests/data_support`: `_Admin` (root, sa) with `create`, `drop`, `run_in`; `scratch_database(engine, kind)` → `(name, admin)`, a `zz_06_<kind>_*` database dropped by exact name.

- [ ] **Step 1: Probes A3, A5 and A14 are this task's tests.** A3: `test_a_mysql_dump_orders_its_objects_and_drops_their_definers` reads the whole catalog (`SHOW CREATE` of each table and program) inside `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`. Another connection then inserts a row, and the snapshot's `COUNT(*)` must not see it. Planning showed it holds, and that `AUTO_INCREMENT=n` in `SHOW CREATE TABLE` is read live, outside the snapshot: that is S10, asserted as `AUTO_INCREMENT=4`. A5: `test_a_sqlite_dump_holds_other_writers_back_only_without_wal` runs with `journal_mode` `delete` (a writer gets `database is locked`) and `wal` (the writer goes through). A14: `test_definer_is_left_out_in_every_form` covers seven account forms. If A3 fails, the dump reads each table in its turn (`per_table`, said in the header, as SQL Server does). If A5 differs, the plan's journal warning (Task 9) changes to match. If a definer form survives, `strip_definer` gains that form, with a test.

- [ ] **Step 2: Write the failing tests**

In `backend/tests/data_support.py`, replace:

```python
from pathlib import Path
from uuid import UUID

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import FileInspection, JobCommand, JobOutcomeEvent, JobProgressEvent
```

with:

```python
from pathlib import Path
from uuid import UUID

import mysql.connector
import psycopg

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.data_jobs import FileInspection, JobCommand, JobOutcomeEvent, JobProgressEvent
```

In `backend/tests/data_support.py`, replace:

```python
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.edit_support import ScratchDb
from tests.support import unique_name
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"
```

with:

```python
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.edit_support import ScratchDb
from tests.support import (
    MSSQL_SA_PASSWORD, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, mssql_connect, mysql_driver_kwargs, unique_name,
)
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"
```

In `backend/tests/data_support.py`, replace:

```python
    else:
        failed = clean_up()
        assert not failed, "dropping scratch objects failed: " + failed


@dataclass
```

with:

```python
    else:
        failed = clean_up()
        assert not failed, "dropping scratch objects failed: " + failed


class _Admin:
    """Creates and drops a scratch database as the engine's administrator (PostgreSQL: the dev login is one; MySQL:
    root; SQL Server: sa), giving the dev login every right in it."""

    def __init__(self, engine: str) -> None:
        self.engine = engine

    def create(self, name: str) -> None:
        self._run(*{
            "postgres": [f'CREATE DATABASE "{name}"'],
            "mysql": [f"CREATE DATABASE `{name}`", f"GRANT ALL ON `{name}`.* TO 'dbaguy'@'%'"],
            "mssql": [f"CREATE DATABASE [{name}]",
                      f"USE [{name}]; CREATE USER dbaguy FOR LOGIN dbaguy; ALTER ROLE db_owner ADD MEMBER dbaguy"],
        }[self.engine])

    def drop(self, name: str) -> None:
        assert name.startswith(SCRATCH_PREFIX), f"{name!r} is not a scratch database: never dropped here"
        self._run(*{
            "postgres": [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'],
            "mysql": [f"DROP DATABASE IF EXISTS `{name}`"],
            "mssql": [f"IF DB_ID(N'{name}') IS NOT NULL ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
                      f"DROP DATABASE IF EXISTS [{name}]"],
        }[self.engine])

    def run_in(self, name: str, *statements: str) -> None:
        """Set-up statements as the administrator, in database `name`."""
        self._run(*statements, database=name)

    def _run(self, *statements: str, database: str | None = None) -> None:
        if self.engine == "postgres":
            with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                 dbname=database or "postgres", autocommit=True) as conn:
                for statement in statements:
                    conn.execute(statement)
        elif self.engine == "mysql":
            conn = mysql.connector.connect(**mysql_driver_kwargs(user="root", password="dbaguy", autocommit=True,
                                                                 database=database or "mysql"))
            try:
                for statement in statements:
                    cursor = conn.cursor()
                    cursor.execute(statement)
                    if cursor.with_rows:
                        cursor.fetchall()
                    cursor.close()
            finally:
                conn.close()
        else:
            conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database or "master")
            try:
                for statement in statements:
                    conn.execute_non_query(statement)
            finally:
                conn.close()


@contextmanager
def scratch_database(engine: str, kind: str = "db") -> Iterator[tuple[str, "_Admin"]]:
    """A database `zz_06_<kind>_<random>` the dev login may use (PostgreSQL, MySQL, SQL Server), dropped by its exact
    name when the block ends, whatever happened."""
    admin = _Admin(engine)
    name = scratch_name(kind)
    admin.create(name)
    try:
        yield name, admin
    finally:
        admin.drop(name)


@dataclass
```

Create `backend/tests/test_mysql_sqlite_dump_catalog.py`:

```python
"""What a MySQL and a SQLite dump write and in which order (spec ⑥ §3.3.3): DEFINER left out in every account form
(A14), stored programs under their own sql_mode between DELIMITER lines, views by their dependencies, SQLite's
counters and left-out virtual tables; the snapshots (A3: MySQL's SHOW CREATE inside one; A5: SQLite's read
transaction and other connections' writes). MySQL works in a scratch database `zz_06_dumpcat_*`."""

import sqlite3

import pytest

from dbaguy_backend.adapters.mysql.dump import RESTORE_SQL_MODE, strip_definer
from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.errors import NotFound
from tests.data_support import scratch_database
from tests.edit_support import scratch_db
from tests.support import requires_mysql


@pytest.mark.parametrize("account", [
    "dbaguy@localhost", "'dbaguy'@'%'", "`dbaguy`@`%`", "`db``x`@`h`", "CURRENT_USER", "CURRENT_USER()",
    "`u` @ `h`",
])
def test_definer_is_left_out_in_every_form(account):  # A14
    assert strip_definer(f"CREATE DEFINER={account} PROCEDURE p() SELECT 1") == "CREATE PROCEDURE p() SELECT 1"
    view = f"CREATE ALGORITHM=UNDEFINED DEFINER = {account} SQL SECURITY DEFINER VIEW `v` AS select 1 AS `definer`"
    assert strip_definer(view) == "CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `v` AS select 1 AS `definer`"
    assert strip_definer("CREATE PROCEDURE p() SELECT 'DEFINER=x@y'") == "CREATE PROCEDURE p() SELECT 'DEFINER=x@y'"


def program(found, name):
    return next(item for item in found.post if item.name == name)


@requires_mysql
def test_a_mysql_dump_orders_its_objects_and_drops_their_definers(tmp_path):
    db = scratch_db("mysql", tmp_path)
    with scratch_database("mysql", "dumpcat") as (name, admin):
        admin.run_in(
            name,
            "CREATE TABLE parent (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(20))",
            "CREATE TABLE child (id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES parent (id))",
            "INSERT INTO parent (label) VALUES ('a'), ('b')",
            "CREATE DEFINER=`dbaguy`@`%` VIEW b_base AS SELECT id FROM parent",
            "CREATE DEFINER=`dbaguy`@`%` VIEW a_top AS SELECT id FROM b_base",  # its name sorts before what it uses
            "SET SESSION sql_mode = 'ANSI_QUOTES'",
            'CREATE DEFINER=`dbaguy`@`%` PROCEDURE two() BEGIN SELECT 1; SELECT "parent".id FROM parent; END',
            "SET SESSION sql_mode = DEFAULT",
            "CREATE DEFINER=`dbaguy`@`%` FUNCTION add1(i INT) RETURNS INT DETERMINISTIC RETURN i + 1",
            "CREATE DEFINER=`dbaguy`@`%` TRIGGER child_bi BEFORE INSERT ON child FOR EACH ROW "
            "BEGIN IF NEW.parent_id IS NULL THEN SET NEW.parent_id = 1; END IF; END",
            "CREATE DEFINER=`dbaguy`@`%` EVENT tidy ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00' "
            "DO DELETE FROM child WHERE id < 0",
        )
        conn = db.open(database=name)
        try:
            for statement in ("SET time_zone = '+00:00'", "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"):
                conn.run(statement).close()
            admin.run_in(name, "INSERT INTO parent (label) VALUES ('after the snapshot')")
            found = conn.dump_catalog(DumpScope(kind="schema", schema_name=name), DumpObjectKinds())  # A3
            result = conn.run("SELECT COUNT(*) FROM parent")
            assert result.fetch(10) == [[2]]  # the snapshot does not see the later row
            result.close()
            conn.rollback()
        finally:
            conn.close()
        assert [(item.kind, item.name) for item in found.pre] == [("table", "child"), ("table", "parent")]
        assert [(item.kind, item.name) for item in found.post] == [
            ("function", "add1"), ("procedure", "two"), ("view", "b_base"), ("view", "a_top"), ("trigger", "child_bi"),
            ("event", "tidy")]
        assert [table.name for table in found.tables] == ["child", "parent"]
        assert "REFERENCES `parent`" in found.pre[0].statements[0]
        assert "AUTO_INCREMENT=4" in found.pre[1].statements[0]  # the counter is no part of the snapshot: it moved on
        two = program(found, "two")
        assert two.compound and two.statements[0] == "SET SESSION sql_mode = 'ANSI_QUOTES'"
        assert two.statements[1].startswith('CREATE PROCEDURE "two"()')  # its own sql_mode quotes with "
        assert "DEFINER" not in two.statements[1]
        assert two.statements[2] == f"SET SESSION sql_mode = '{RESTORE_SQL_MODE}'"
        view = program(found, "a_top").statements[0]
        assert view.startswith("CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `a_top` AS select")
        assert f"`{name}`" not in view  # USE: no database in the names (M6)
        assert program(found, "tidy").statements[1].startswith("CREATE EVENT `tidy`")
        assert program(found, "child_bi").drop == "DROP TRIGGER IF EXISTS child_bi"
        conn = db.open(database=name)
        try:
            one = conn.dump_catalog(DumpScope(kind="tables", schema_name=name, names=["child"]), DumpObjectKinds())
            with pytest.raises(NotFound):
                conn.dump_catalog(DumpScope(kind="tables", schema_name=name, names=["nope"]), DumpObjectKinds())
        finally:
            conn.close()
        assert [(item.kind, item.name) for item in (*one.pre, *one.post)] == [("table", "child"), ("trigger", "child_bi")]
    db.close()


def test_a_sqlite_dump_keeps_sqlite_masters_order_and_its_counters(tmp_path):
    db = scratch_db("sqlite", tmp_path)
    db.execute(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT)",
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id), v)",
        "CREATE INDEX child_parent ON child (parent_id)",
        "CREATE VIEW labels AS SELECT label FROM parent",
        "CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN UPDATE child SET v = 1 WHERE id = new.id; END",
        "CREATE VIRTUAL TABLE notes USING fts5(body)",
        "INSERT INTO parent (label) VALUES ('a'), ('b'), ('c')",
        "DELETE FROM parent WHERE id = 3",  # the counter stays at 3: the next id is 4, not 3
    )
    conn = db.open()
    try:
        found = conn.dump_catalog(DumpScope(kind="schema", schema_name="main"), DumpObjectKinds())
    finally:
        conn.close()
    assert [(item.kind, item.name) for item in found.pre] == [("table", "parent"), ("table", "child")]
    assert [(item.kind, item.name) for item in found.post] == [
        ("sequence value", "parent"), ("index", "child_parent"), ("view", "labels"), ("trigger", "child_ai")]
    assert found.not_included == ("owners and privileges", "6 virtual tables and their internal tables")
    assert program(found, "parent").statements[0] == "UPDATE sqlite_sequence SET seq = max(seq, 3) WHERE name = 'parent'"
    assert [column.name for column in found.tables[1].columns] == ["id", "parent_id", "v"]
    db.close()


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_a_sqlite_dump_holds_other_writers_back_only_without_wal(tmp_path, journal):  # A5
    db = scratch_db("sqlite", tmp_path)
    db.execute(f"PRAGMA journal_mode = {journal}", "CREATE TABLE t (a)", "INSERT INTO t VALUES (1)")
    conn = db.open()
    other = sqlite3.connect(db.file, isolation_level=None, timeout=0.1)
    try:
        conn.run("BEGIN").close()
        conn.dump_catalog(DumpScope(kind="schema", schema_name="main"), DumpObjectKinds())  # the read starts here
        if journal == "delete":
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other.execute("INSERT INTO t VALUES (2)")
        else:
            other.execute("INSERT INTO t VALUES (2)")
        conn.rollback()
    finally:
        other.close()
        conn.close()
        db.close()
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_sqlite_dump_catalog.py`
Expected: collection error (`dbaguy_backend.adapters.mysql.dump` does not exist).

- [ ] **Step 4: The account scanner, public**

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
        return self._head_word(0) == "CREATE" and created == "TRIGGER"


def _after_account(head: list[Token], i: int) -> int:
    """`head[i]` follows the word DEFINER: skips `= account`, the account being user, 'user'@'host', `user`@`host`,
    user@host or CURRENT_USER[()], and returns the index after it. The lexer reads `@host` as one word, and a bare
    `@` as a word of its own that a quoted host follows."""
```

with:

```python
        return self._head_word(0) == "CREATE" and created == "TRIGGER"


def after_account(head: list[Token], i: int) -> int:
    """`head[i]` follows the word DEFINER: skips `= account`, the account being user, 'user'@'host', `user`@`host`,
    user@host or CURRENT_USER[()], and returns the index after it. The lexer reads `@host` as one word, and a bare
    `@` as a word of its own that a quoted host follows."""
```

In `backend/dbaguy_backend/sql/splitter.py`, replace:

```python
    if words[i:i + 2] == ["OR", "REPLACE"]:
        i += 2
    if words[i:i + 1] == ["DEFINER"]:
        i = _after_account(head, i + 1)
    if words[i:i + 2] == ["SQL", "SECURITY"]:
        i += 3
    return i < len(words) and words[i] in _COMPOUND_OBJECTS
```

with:

```python
    if words[i:i + 2] == ["OR", "REPLACE"]:
        i += 2
    if words[i:i + 1] == ["DEFINER"]:
        i = after_account(head, i + 1)
    if words[i:i + 2] == ["SQL", "SECURITY"]:
        i += 3
    return i < len(words) and words[i] in _COMPOUND_OBJECTS
```

- [ ] **Step 5: The MySQL catalog**

Create `backend/dbaguy_backend/adapters/mysql/dump.py`:

```python
"""What a MySQL dump writes, and in which order (spec ⑥ §3.3.3), read inside the dump's snapshot. The dump session
`USE`s the database, so SHOW CREATE VIEW leaves the database out of names (M6) and the file restores into whichever
database its run uses.

Order. Before the data: tables, as SHOW CREATE TABLE writes them (foreign keys inside: the header turns
FOREIGN_KEY_CHECKS off; AUTO_INCREMENT kept). After the data: routines (a body may name a table that is not there yet,
M10), views by VIEW_TABLE_USAGE (a view calling a missing function is 1305, M10), triggers, events. Every stored
program is written between DELIMITER lines, under the sql_mode it was created with, and without its DEFINER (the
account that runs the file becomes it; keeping it needs SUPER or SET_USER_ID, 1227)."""

import mysql.connector
from loguru import logger
from mysql.connector.connection import MySQLConnection

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound, ValidationFailed
from ...sql.dialects import Dialect, quote_ident, quote_literal
from ...sql.lexer import INSIGNIFICANT, TokenKind, scan
from ...sql.splitter import after_account
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering
from . import catalog
from .columns import read_columns

RESTORE_SQL_MODE = "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO"  # the header's (M1): zero dates, an id of 0, strict
SNAPSHOT_TEXT = "one consistent snapshot (InnoDB)"
_RELATIONS = """
SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH, ENGINE
FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME
"""
_VIEW_USES = """
SELECT VIEW_NAME, TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE
WHERE VIEW_SCHEMA = %s AND TABLE_SCHEMA = %s
"""
_ROUTINES = "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME"
_TRIGGERS = """
SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s
ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER
"""
_EVENTS = "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME"
# SHOW CREATE <word>: the index of its sql_mode and of its DDL in the row
_PROGRAMS = {"procedure": ("PROCEDURE", 1, 2), "function": ("FUNCTION", 1, 2), "trigger": ("TRIGGER", 1, 2),
             "event": ("EVENT", 1, 3)}
_HEAD_TOKENS = 24  # CREATE ALGORITHM = UNDEFINED DEFINER = `u` @ `h` SQL SECURITY DEFINER VIEW …: where DEFINER is


def session(database: str) -> DumpSession:
    return DumpSession(
        setup=(f"USE {quote_ident(Dialect.MYSQL, database)}", "SET NAMES utf8mb4", "SET time_zone = '+00:00'",
               "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"),
        teardown=("ROLLBACK",), consistency="snapshot",
    )


def strip_definer(sql: str) -> str:
    """`sql` without its `DEFINER = account` clause (A14: user@host, 'u'@'h', `u`@`h`, CURRENT_USER[()]); SQL SECURITY
    DEFINER stays. Text inside the account is never touched beyond the clause."""
    head = []
    for token in scan(sql, Dialect.MYSQL):
        if token.kind in INSIGNIFICANT or token.kind == TokenKind.EXEC_COMMENT:
            continue
        head.append(token)
        if len(head) >= _HEAD_TOKENS:
            break
    for i, token in enumerate(head):
        if token.kind == TokenKind.WORD and token.upper == "DEFINER" and i + 1 < len(head) and head[i + 1].text == "=":
            end = after_account(head, i + 1)
            stop = head[end].start if end < len(head) else len(sql)
            return sql[:token.start] + sql[stop:]
    return sql


class _Reader:
    def __init__(self, conn: MySQLConnection, database: str, scope: DumpScope, kinds: DumpObjectKinds,
                 ddl: bool) -> None:
        self.conn, self.database, self.scope, self.kinds, self.ddl = conn, database, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}

    def q(self, name: str) -> str:
        return quote_ident(Dialect.MYSQL, name)

    def leave_out(self, what: str) -> None:
        self.left_out[what] = self.left_out.get(what, 0) + 1

    def show(self, word: str, name: str, index: int) -> tuple | None:
        """SHOW CREATE <word> <name>'s row, or None when the account may not read the definition (it is left out)."""
        try:
            row = catalog.first_row(self.conn, f"SHOW CREATE {word} {self.q(name)}")
        except mysql.connector.Error as exc:
            if exc.errno not in catalog.UNREADABLE_DEFINITION_ERRNOS:
                raise
            logger.debug("MySQL error {} on SHOW CREATE {} {}: left out of the dump", exc.errno, word, name)
            row = None
        if row is None or not row[index]:
            self.leave_out(f"{word.lower()}s whose definition cannot be read")
            return None
        return row

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        relations = catalog.query(self.conn, _RELATIONS, (self.database,))
        if self.named:
            wanted = set(self.scope.names)
            if missing := wanted - {row[0] for row in relations}:
                raise NotFound(f"Not found in {self.database}: {', '.join(sorted(missing))}")
            relations = [row for row in relations if row[0] in wanted]
        tables = [row for row in relations if row[1] == "BASE TABLE"]
        views = [row for row in relations if row[1] == "VIEW" and (self.kinds.views or self.named)]
        pre = [obj for row in tables if (obj := self._table(row[0])) is not None]
        data = [self._data(row) for row in tables]
        post: list[DumpObject] = []
        if self.kinds.routines and not self.named:
            post += [obj for name, kind in catalog.query(self.conn, _ROUTINES, (self.database,))
                     if (obj := self._program(kind.lower(), name)) is not None]
        post += self._views([row[0] for row in views])
        if self.kinds.triggers:
            names = {row[0] for row in tables}
            post += [obj for name, table in catalog.query(self.conn, _TRIGGERS, (self.database,))
                     if table in names and (obj := self._program("trigger", name)) is not None]
        if self.kinds.routines and not self.named:
            post += [obj for (name,) in catalog.query(self.conn, _EVENTS, (self.database,))
                     if (obj := self._program("event", name)) is not None]
        return pre, data, post

    def _table(self, name: str) -> DumpObject | None:
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show("TABLE", name, 1)
            if row is None:
                return None
            statements = (row[1],)
        return DumpObject("table", name, statements, f"DROP TABLE IF EXISTS {self.q(name)}")

    def _data(self, row: tuple) -> DumpTable:
        name, _, estimate, size, _ = row
        return DumpTable(schema=self.database, table=name, name=name,
                         columns=tuple(read_columns(self.conn, self.database, name)),
                         rows_estimate=int(estimate) if estimate is not None else None,
                         bytes_estimate=int(size) if size is not None else None)

    def _program(self, kind: str, name: str) -> DumpObject | None:
        word, mode_index, ddl_index = _PROGRAMS[kind]
        statements: tuple[str, ...] = ()
        if self.ddl:
            row = self.show(word, name, ddl_index)
            if row is None:
                return None
            mode = quote_literal(Dialect.MYSQL, str(row[mode_index] or ""))
            statements = (f"SET SESSION sql_mode = {mode}", strip_definer(row[ddl_index]),
                          f"SET SESSION sql_mode = '{RESTORE_SQL_MODE}'")
        return DumpObject(kind, name, statements, f"DROP {word} IF EXISTS {self.q(name)}", compound=True)

    def _views(self, names: list[str]) -> list[DumpObject]:
        ordering = Ordering()
        for view, uses in catalog.query(self.conn, _VIEW_USES, (self.database, self.database)) if names else []:
            ordering.depends.setdefault(view, set()).add(uses)
        found = []
        for name in ordering.order(names):
            statements: tuple[str, ...] = ()
            if self.ddl:
                row = self.show("VIEW", name, 1)
                if row is None:
                    continue
                statements = (strip_definer(row[1]),)
            found.append(DumpObject("view", name, statements, f"DROP VIEW IF EXISTS {self.q(name)}"))
        return found


def dump_catalog(
    conn: MySQLConnection, database: str | None, scope: DumpScope, kinds: DumpObjectKinds, server: str, *,
    ddl: bool = True,
) -> DumpCatalog:
    if scope.kind == "database":
        raise ValidationFailed("A MySQL dump covers one database: dump its schema node")
    name = scope.schema_name or database or ""
    if catalog.first_row(conn, "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (name,)) is None:
        raise NotFound(f"Database {name} not found")
    reader = _Reader(conn, name, scope, kinds, ddl)
    pre, tables, post = reader.read()
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items())]
    return DumpCatalog(server=server, scope=f"database {name}", consistency="snapshot", pre=tuple(pre),
                       tables=tuple(tables), post=tuple(post), not_included=tuple(left_out),
                       snapshot_text=SNAPSHOT_TEXT)
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
)
from ..cancel_window import CancelWindow
from ..sockets import enable_keepalive
from ..columns import TableColumn
from . import catalog, columns, editing, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
from .types import column_meta, row_converter
```

with:

```python
)
from ..cancel_window import CancelWindow
from ..sockets import enable_keepalive
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from . import catalog, columns, dump, editing, introspection
from . import schema as mysql_schema
from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
from .types import column_meta, row_converter
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
        self._owner.finish(self._cursor, more_results=not self._failed)


class MysqlConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
```

with:

```python
        self._owner.finish(self._cursor, more_results=not self._failed)


class MysqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
        self._conn = conn
        self._kill_params = kill_params
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

```

with:

```python
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.session(scope.schema_name or self._database or "")

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        try:
            return dump.dump_catalog(self._conn, self._database, scope, kinds, self.server_info().version, ddl=ddl)
        except mysql.connector.Error as exc:
            raise self.translate_error(exc) from exc

    def schema_snapshot(self) -> SchemaSnapshot:
        return mysql_schema.build_snapshot(self._conn)

```

- [ ] **Step 6: The SQLite catalog**

Create `backend/dbaguy_backend/adapters/sqlite/dump.py`:

```python
"""What a SQLite dump writes, and in which order (spec ⑥ §3.3.3): sqlite_master's own texts in its order (creation
order), read in one read transaction (WAL: other connections keep writing; a rollback journal: their writes wait until
the dump ends, A5). Before the data: tables (foreign keys inside: the header turns the checks off, and defers them
inside a transaction). After the data: AUTOINCREMENT counters (sqlite_sequence follows the ids written, but not a
counter past them), indexes, views (SQLite does not check a view's tables when it is created, L1), triggers. A virtual
table, its internal tables and SQLite's own tables are left out (⑤ G4: writing them breaks their index)."""

import sqlite3

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound, ValidationFailed
from ...sql.dialects import Dialect, quote_ident, quote_literal
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable
from .columns import columns_of, read_relation

SESSION = DumpSession(setup=("BEGIN",), teardown=("ROLLBACK",), consistency="snapshot")
SNAPSHOT_TEXT = "one consistent snapshot (a read transaction)"
_MASTER = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
_KINDS = "SELECT name, type FROM pragma_table_list WHERE schema = 'main'"  # table, view, virtual, shadow
_SEQUENCES = "SELECT name, seq FROM sqlite_sequence"
_HAS_SEQUENCES = "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"


def _q(name: str) -> str:
    return quote_ident(Dialect.SQLITE, name)


def dump_catalog(conn: sqlite3.Connection, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
    if scope.kind == "database" or (scope.schema_name or "main") != "main":
        raise ValidationFailed("A SQLite dump covers the main database")
    kind_of = dict(conn.execute(_KINDS).fetchall())
    master = conn.execute(_MASTER).fetchall()
    named = scope.kind == "tables"
    wanted = set(scope.names)
    if named and (missing := wanted - {name for kind, name, _, _ in master if kind in ("table", "view")}):
        raise NotFound(f"Not found: {', '.join(sorted(missing))}")
    left_out: dict[str, int] = {}
    pre: list[DumpObject] = []
    tables: list[str] = []
    post_indexes, post_views, post_triggers = [], [], []
    for kind, name, table, sql in master:
        if name.startswith("sqlite_"):
            continue
        if kind == "table":
            if kind_of.get(name) in ("virtual", "shadow"):
                if not named or name in wanted:
                    left_out["virtual tables and their internal tables"] = left_out.get(
                        "virtual tables and their internal tables", 0) + 1
                continue
            if named and name not in wanted:
                continue
            tables.append(name)
            pre.append(DumpObject("table", name, (sql,) if ddl else (), f"DROP TABLE IF EXISTS {_q(name)}"))
        elif kind == "index" and table in tables:
            post_indexes.append(DumpObject("index", name, (sql,) if ddl else ()))
        elif kind == "view" and (name in wanted if named else kinds.views):
            post_views.append(DumpObject("view", name, (sql,) if ddl else (), f"DROP VIEW IF EXISTS {_q(name)}"))
        elif kind == "trigger" and table in tables and kinds.triggers:
            post_triggers.append(DumpObject("trigger", name, (sql,) if ddl else ()))
    counters = []
    if conn.execute(_HAS_SEQUENCES).fetchone() is not None:
        for name, seq in conn.execute(_SEQUENCES).fetchall():
            if name in tables:
                literal = quote_literal(Dialect.SQLITE, name)
                counters.append(DumpObject("sequence value", name, (
                    f"UPDATE sqlite_sequence SET seq = max(seq, {int(seq)}) WHERE name = {literal}",
                    f"INSERT INTO sqlite_sequence (name, seq) SELECT {literal}, {int(seq)} "
                    f"WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = {literal})",
                ) if ddl else (), section="data"))
    data = [DumpTable(schema="main", table=name, name=name, columns=tuple(columns_of(read_relation(conn, "main", name))))
            for name in tables]
    not_included = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in left_out.items())]
    return DumpCatalog(server=f"SQLite {sqlite3.sqlite_version}", scope="schema main", consistency="snapshot",
                       pre=tuple(pre), tables=tuple(data), post=(*counters, *post_indexes, *post_views, *post_triggers),
                       not_included=tuple(not_included), snapshot_text=SNAPSHOT_TEXT)
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

with:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    ParsedUrl,
    RowEditing,
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from ..columns import TableColumn
from . import columns, editing, introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200
```

with:

```python
    transaction_open_error,
)
from ..cancel_window import CancelWindow
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from . import columns, dump, editing, introspection
from . import schema as sqlite_schema

_PEEK_ROWS = 200
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
            self._cursor.close()


class SqliteConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn  # isolation_level=None: the driver never begins a transaction on its own
        self._path = path
```

with:

```python
            self._cursor.close()


class SqliteConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn  # isolation_level=None: the driver never begins a transaction on its own
        self._path = path
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

```

with:

```python
    def table_columns(self, path: list[PathSegment]) -> list[TableColumn]:
        try:
            return columns.table_columns(self._conn, path)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return dump.SESSION

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        try:
            return dump.dump_catalog(self._conn, scope, kinds, ddl=ddl)
        except sqlite3.Error as exc:
            raise _translate(exc) from exc

```

- [ ] **Step 7: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_sqlite_dump_catalog.py tests/sql/test_splitter.py`
Expected: `64 passed` (11 new: 7 definer forms, 2 journal modes, 2 catalogs).

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/mysql/dump.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/dump.py \
  backend/dbaguy_backend/sql/splitter.py \
  backend/tests/data_support.py \
  backend/tests/test_mysql_sqlite_dump_catalog.py
git commit -m "feat(06a): the MySQL and SQLite dump catalogs (snapshot, DEFINER, own sql_mode, counters)"
```

---

### Task 7: The SQL Server catalog (A4)

**Files:**
- Create: `backend/dbaguy_backend/adapters/mssql/dump.py`, `backend/tests/test_mssql_dump_catalog.py`
- Modify: `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mssql/editing.py`, `backend/dbaguy_backend/adapters/mssql/introspection.py`

**Interfaces:**
- Consumes: Task 5 (`Dumping`, `DumpCatalog`, `Ordering`); Task 4 (`read_columns`); `adapters/mssql/introspection.py` (table, type, sequence and synonym DDL, `OBJECT_DEFINITION`), `catalog.rows`/`first_row`, `editing.table_flags_reason` (temporal, graph and ledger tables, ⑤ G9).
- Produces:
  - `adapters/mssql/introspection.py`: `TableStatements(create, indexes, foreign_keys)` and `table_statements(conn, schema, name)` (the table's DDL in parts; the object tree's DDL unchanged); `sequence_ddl(…, at_next_value=False)` (a dump starts it at its next value, appendix A S11); `type_ddl`, `synonym_ddl` public.
  - `adapters/mssql/editing.TABLE_FLAGS` (the catalog columns `table_flags_reason` reads, shared with the dump).
  - `adapters/mssql/dump.py`: `SNAPSHOT` (`SET TRANSACTION ISOLATION LEVEL SNAPSHOT`, `BEGIN TRANSACTION`; teardown `ROLLBACK TRANSACTION`) when `sys.databases.snapshot_isolation_state` is 1, else `PER_TABLE` (no setup; the header and the plan warn); `session(conn)`; `dump_catalog(conn, scope, kinds, server, database, *, ddl=True)`. Modules (views, functions, synonyms) are ordered by `sys.sql_expression_dependencies`, with procedures after them; each module comes in its own batch under the QUOTED_IDENTIFIER and ANSI_NULLS it was created with. The functions a table's default, check or computed column calls come before the tables (S7). Identity counters are reseeded after the data (`DBCC CHECKIDENT (…, RESEED, n) WITH NO_INFOMSGS`, S8). System-versioned, graph and ledger tables, CLR types, database triggers, columnstore/XML/spatial indexes and hidden definitions are left out and listed (S9).
  - `MssqlConnection.dump_session` / `dump_catalog`, through `_catalog_call` (the worker thread; the metadata session's READ UNCOMMITTED is never used for a dump).

- [ ] **Step 1: Probe A4 is this task's test.** `test_a_snapshot_dump_does_not_see_later_rows` runs in a scratch database with `ALLOW_SNAPSHOT_ISOLATION ON` (made by sa, owned by the dev login). The dump session's SNAPSHOT setup goes first, then the catalog read, then another connection inserts a row: the snapshot's `COUNT(*)` must not see it. Planning showed it holds, and that a SNAPSHOT transaction's view starts at its first read, not at `BEGIN TRANSACTION` (S13): the catalog read is that first read. If DDL reads fail under SNAPSHOT (3961 on a table altered since), the dump falls back to `PER_TABLE`, with the header's note.

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_mssql_dump_catalog.py`:

```python
"""What a SQL Server dump writes and in which order (spec ⑥ §3.3.3): modules by their dependencies with their own
QUOTED_IDENTIFIER, a function a table needs before it, sequences at their next value (A S11), identity counters, left-out
tables; SNAPSHOT where the database allows it (A4) and the honest per-table read where it does not (A S9). Works in a
scratch database `zz_06_dumpcat_*` (sa makes it; the dev login owns it)."""

import pytest

from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from tests.data_support import scratch_database
from tests.edit_support import scratch_db
from tests.support import requires_mssql

pytestmark = requires_mssql


@pytest.fixture
def database(tmp_path):
    db = scratch_db("mssql", tmp_path)
    with scratch_database("mssql", "dumpcat") as (name, admin):
        admin.run_in(name, f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        for batch in (
            "CREATE SCHEMA app",
            "CREATE TYPE app.code FROM varchar(10) NOT NULL",
            "CREATE TYPE app.ids AS TABLE (id int PRIMARY KEY)",
            "CREATE SEQUENCE app.tickets AS int START WITH 10 INCREMENT BY 5",
            "CREATE FUNCTION app.double_it (@v int) RETURNS int AS BEGIN RETURN @v * 2 END",
            "CREATE TABLE app.parent (id int IDENTITY(1, 1) PRIMARY KEY, code app.code, "
            "ticket int DEFAULT NEXT VALUE FOR app.tickets, twice AS app.double_it(id))",
            "CREATE TABLE app.child (id int PRIMARY KEY, parent_id int REFERENCES app.parent (id), note nvarchar(50))",
            "CREATE INDEX child_note ON app.child (note)",
            "INSERT INTO app.parent (code) VALUES ('a'), ('b'), ('c')",
            "DELETE FROM app.parent WHERE id = 3",
            "SELECT NEXT VALUE FOR app.tickets",
            "CREATE FUNCTION app.parents_of (@id int) RETURNS TABLE AS RETURN SELECT id FROM app.parent WHERE id = @id",
            "CREATE VIEW app.v_base AS SELECT id FROM app.parent",
            "CREATE VIEW app.a_top AS SELECT id FROM app.v_base",
            "CREATE PROCEDURE app.later AS SELECT * FROM app.a_top",
            "CREATE SYNONYM app.p FOR app.parent",
            "CREATE TRIGGER app.child_ai ON app.child AFTER INSERT AS SET NOCOUNT ON",
            "DISABLE TRIGGER app.child_ai ON app.child",
            "CREATE TABLE app.versioned (id int PRIMARY KEY, a datetime2 GENERATED ALWAYS AS ROW START, "
            "b datetime2 GENERATED ALWAYS AS ROW END, PERIOD FOR SYSTEM_TIME (a, b)) "
            "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = app.versioned_history))",
        ):
            admin.run_in(name, batch)
        admin.run_in(name, "SET QUOTED_IDENTIFIER OFF")
        admin.run_in(name, 'SET QUOTED_IDENTIFIER OFF; EXEC(\'CREATE VIEW app.quoted AS SELECT "text" AS t\')')
        yield db, name, admin
    db.close()


def kinds(objects) -> list[tuple[str, str]]:
    return [(item.kind, item.name) for item in objects]


def test_a_sql_server_dump_orders_its_objects(database):
    db, name, _ = database
    conn = db.open(database=name)
    try:
        session = conn.dump_session(DumpScope(kind="database"))
        found = conn.dump_catalog(DumpScope(kind="database"), DumpObjectKinds())
    finally:
        conn.close()
    assert (session.setup, session.consistency) == (("SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "BEGIN TRANSACTION"),
                                                     "snapshot")
    assert kinds(found.pre) == [("schema", "app"), ("type", "app.code"), ("type", "app.ids"), ("sequence", "app.tickets"),
                                ("function", "app.double_it"), ("table", "app.parent"), ("table", "app.child")]
    assert kinds(found.post) == [
        ("identity value", "app.parent"), ("index", "app.child"), ("foreign key", "app.child"),
        ("function", "app.parents_of"), ("view", "app.v_base"), ("view", "app.a_top"), ("synonym", "app.p"),
        ("view", "app.quoted"), ("procedure", "app.later"), ("trigger", "app.child_ai")]
    statements = {(item.kind, item.name): item.statements for item in (*found.pre, *found.post)}
    assert "START WITH 30 " in statements[("sequence", "app.tickets")][0]  # A S11: 10, 15, 20 (the rows), 25 used
    assert statements[("identity value", "app.parent")] == (
        "DBCC CHECKIDENT (N'[app].[parent]', RESEED, 3) WITH NO_INFOMSGS",)  # past the deleted row 3
    assert "REFERENCES" not in statements[("table", "app.child")][0]
    assert statements[("foreign key", "app.child")][0].startswith("ALTER TABLE [app].[child] ADD FOREIGN KEY")
    assert statements[("view", "app.quoted")] == (
        "SET QUOTED_IDENTIFIER OFF", 'CREATE VIEW app.quoted AS SELECT "text" AS t', "SET QUOTED_IDENTIFIER ON")
    assert statements[("trigger", "app.child_ai")][-1] == "DISABLE TRIGGER [app].[child_ai] ON [app].[child]"
    assert [table.name for table in found.tables] == ["app.parent", "app.child"]
    assert "2 system-versioned, graph or ledger tables" in found.not_included  # and its history table
    assert found.warnings == tuple(f"app.{table} is left out: system-versioned, graph and ledger tables are not dumped"
                                   for table in ("versioned", "versioned_history"))
    assert [column.name for column in found.tables[0].columns if column.copied] == ["id", "code", "ticket"]


def test_a_snapshot_dump_does_not_see_later_rows(database):  # A4
    db, name, admin = database
    conn = db.open(database=name)
    try:
        for statement in conn.dump_session(DumpScope(kind="schema", schema_name="app")).setup:
            conn.run(statement).close()
        found = conn.dump_catalog(DumpScope(kind="schema", schema_name="app"), DumpObjectKinds())  # the first read
        admin.run_in(name, "INSERT INTO app.parent (code) VALUES ('later')")  # SNAPSHOT starts at the first read
        result = conn.run("SELECT COUNT(*) FROM app.parent")
        assert result.fetch(10) == [[2]]
        result.close()
        conn.rollback()
    finally:
        conn.close()
    assert [table.name for table in found.tables] == ["app.parent", "app.child"]


def test_without_snapshot_isolation_each_table_is_read_in_its_turn(database):
    db, name, admin = database
    admin.run_in("master", f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION OFF")
    conn = db.open(database=name)
    try:
        session = conn.dump_session(DumpScope(kind="schema", schema_name="app"))
        found = conn.dump_catalog(DumpScope(kind="tables", schema_name="app", names=["child"]), DumpObjectKinds())
    finally:
        conn.close()
    assert (session.setup, session.consistency, found.consistency) == ((), "per_table", "per_table")
    assert found.warnings[0].startswith("Not a consistent snapshot")
    assert "A foreign key of app.child is left out: it references app.parent, which is not in this dump" in found.warnings
    assert kinds(found.post) == [("index", "app.child"), ("trigger", "app.child_ai")]
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mssql_dump_catalog.py`
Expected: collection error (`dbaguy_backend.adapters.mssql.dump` does not exist).

- [ ] **Step 4: Introspection in parts, editing's flags**

In `backend/dbaguy_backend/adapters/mssql/introspection.py`, replace:

```python
_SEQUENCE = """
SELECT TYPE_NAME(user_type_id), CAST(start_value AS nvarchar(40)), CAST(increment AS nvarchar(40)),
       CAST(minimum_value AS nvarchar(40)), CAST(maximum_value AS nvarchar(40)), is_cycling, is_cached, cache_size,
       precision, scale
FROM sys.sequences WHERE object_id = {o}
"""
_SYNONYM = "SELECT base_object_name FROM sys.synonyms WHERE object_id = {o}"
```

with:

```python
_SEQUENCE = """
SELECT TYPE_NAME(user_type_id), CAST(start_value AS nvarchar(40)), CAST(increment AS nvarchar(40)),
       CAST(minimum_value AS nvarchar(40)), CAST(maximum_value AS nvarchar(40)), is_cycling, is_cached, cache_size,
       precision, scale,
       CAST(CASE WHEN last_used_value IS NULL THEN CAST(start_value AS decimal(38, 0))
                 ELSE CAST(current_value AS decimal(38, 0)) + CAST(increment AS decimal(38, 0)) END AS nvarchar(40))
FROM sys.sequences WHERE object_id = {o}
"""
_SYNONYM = "SELECT base_object_name FROM sys.synonyms WHERE object_id = {o}"
```

In `backend/dbaguy_backend/adapters/mssql/introspection.py`, replace:

```python
    return "\n".join(statements)


def _sequence_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SEQUENCE.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SEQUENCE, schema, name)
    type_name, start, increment, minimum, maximum, is_cycling, is_cached, cache_size, precision, scale = row
    cache = f"CACHE {cache_size}" if is_cached and cache_size is not None else "CACHE" if is_cached else "NO CACHE"
    return (f"CREATE SEQUENCE {q(schema)}.{q(name)} AS {type_sql(type_name, 0, precision, scale)} START WITH {start} "
            f"INCREMENT BY {increment} MINVALUE {minimum} MAXVALUE {maximum} {'CYCLE' if is_cycling else 'NO CYCLE'} {cache};")


def _synonym_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SYNONYM.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SYNONYM, schema, name)
    return f"CREATE SYNONYM {q(schema)}.{q(name)} FOR {row[0]};"


def _type_ddl(conn: driver.Connection, schema: str, name: str) -> str | None:
    """One batch: the type, and what a table type's body needs (empty for an alias type). None for a CLR type: its
    assembly is not something a catalog query can write back."""
    ref = name_literal(schema, name)
```

with:

```python
    return "\n".join(statements)


@dataclass(frozen=True, slots=True)
class TableStatements:
    """A table's DDL as a dump writes it (spec ⑥ §3.3.3): CREATE TABLE without its foreign keys, then — after the
    data — its other indexes and its foreign keys. No `;` at the ends."""

    create: str | None  # None: a definition it needs is hidden from the account (VIEW DEFINITION)
    indexes: list[str]
    foreign_keys: list[tuple[str, str, str]]  # (referenced schema, referenced table, ALTER TABLE … ADD …)


def table_statements(conn: driver.Connection, schema: str, name: str) -> TableStatements:
    """One batch, as the table's detail reads it."""
    o = object_id_sql(schema, name)
    column_rows, keys, checks, foreign_keys, indexes = result_sets(conn, batch(
        _COLUMNS.format(o=o), _KEYS.format(o=o), _CHECKS.format(o=o), _FOREIGN_KEYS.format(o=o), _INDEXES.format(o=o)))
    if not column_rows:
        raise _not_found(NodeKind.TABLE, schema, name)
    columns = [_Column(*row) for row in column_rows]

    def create() -> str:
        lines = _body_lines(columns, keys, checks, named=True)
        return "CREATE TABLE {} (\n    {}\n)".format(f"{q(schema)}.{q(name)}", ",\n    ".join(lines))

    def written(build: Callable[[], str]) -> str | None:
        return _written(NodeKind.TABLE, schema, name, build)

    index_statements = [statement for index, parts in _grouped(indexes).items()
                        if not (parts[0][7] or parts[0][8])  # constraint indexes are in the body
                        and (statement := written(lambda p=parts, i=index: _index_statement(schema, name, i, p)))]
    keys_out = []
    for key, parts in _grouped(foreign_keys).items():
        clause = written(lambda p=parts, k=key: _foreign_key_clause(k, p))
        if clause is not None:
            keys_out.append((parts[0][2], parts[0][3], f"ALTER TABLE {q(schema)}.{q(name)} ADD {clause}"))
    return TableStatements(written(create), [i.rstrip(";") for i in index_statements], keys_out)


def sequence_ddl(conn: driver.Connection, schema: str, name: str, *, at_next_value: bool = False) -> str:
    """`at_next_value`: START WITH the value the sequence gives next (a dump, A S11: its start value until one was used,
    then the current value plus the increment), not the one it was created with."""
    row = first_row(conn, _SEQUENCE.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SEQUENCE, schema, name)
    type_name, start, increment, minimum, maximum, is_cycling, is_cached, cache_size, precision, scale, next_value = row
    if at_next_value:
        start = next_value
    cache = f"CACHE {cache_size}" if is_cached and cache_size is not None else "CACHE" if is_cached else "NO CACHE"
    return (f"CREATE SEQUENCE {q(schema)}.{q(name)} AS {type_sql(type_name, 0, precision, scale)} START WITH {start} "
            f"INCREMENT BY {increment} MINVALUE {minimum} MAXVALUE {maximum} {'CYCLE' if is_cycling else 'NO CYCLE'} {cache};")


def synonym_ddl(conn: driver.Connection, schema: str, name: str) -> str:
    row = first_row(conn, _SYNONYM.format(o=object_id_sql(schema, name)))
    if row is None:
        raise _not_found(NodeKind.SYNONYM, schema, name)
    return f"CREATE SYNONYM {q(schema)}.{q(name)} FOR {row[0]};"


def type_ddl(conn: driver.Connection, schema: str, name: str) -> str | None:
    """One batch: the type, and what a table type's body needs (empty for an alias type). None for a CLR type: its
    assembly is not something a catalog query can write back."""
    ref = name_literal(schema, name)
```

In `backend/dbaguy_backend/adapters/mssql/introspection.py`, replace:

```python
    NodeKind.PROCEDURE: _module_detail,
    NodeKind.FUNCTION: _module_detail,
    NodeKind.TRIGGER: _module_detail,  # a trigger's schema is its table's: path[1]
    NodeKind.SEQUENCE: _generated_detail(_sequence_ddl),
    NodeKind.SYNONYM: _generated_detail(_synonym_ddl),
    NodeKind.TYPE: _generated_detail(_type_ddl),
    NodeKind.INDEX: _index_detail,
    NodeKind.FOREIGN_KEY: _foreign_key_detail,
    NodeKind.COLUMN: _column_leaf_detail,
```

with:

```python
    NodeKind.PROCEDURE: _module_detail,
    NodeKind.FUNCTION: _module_detail,
    NodeKind.TRIGGER: _module_detail,  # a trigger's schema is its table's: path[1]
    NodeKind.SEQUENCE: _generated_detail(sequence_ddl),
    NodeKind.SYNONYM: _generated_detail(synonym_ddl),
    NodeKind.TYPE: _generated_detail(type_ddl),
    NodeKind.INDEX: _index_detail,
    NodeKind.FOREIGN_KEY: _foreign_key_detail,
    NodeKind.COLUMN: _column_leaf_detail,
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
_OBJECT = "SELECT type FROM sys.objects WHERE object_id = {o}"
# sys.tables' temporal_type (2016), is_node / is_edge (2017) and ledger_type (2022), read through dynamic SQL so that an
# older server answers 0 instead of failing the whole batch with 207 (invalid column name)
_FLAGS = """
DECLARE @o int = {o}, @q nvarchar(max) = N'SELECT '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'temporal_type') IS NULL THEN N'0' ELSE N'temporal_type' END + N', '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'is_node') IS NULL THEN N'0, 0' ELSE N'is_node, is_edge' END + N', '
```

with:

```python
_OBJECT = "SELECT type FROM sys.objects WHERE object_id = {o}"
# sys.tables' temporal_type (2016), is_node / is_edge (2017) and ledger_type (2022), read through dynamic SQL so that an
# older server answers 0 instead of failing the whole batch with 207 (invalid column name)
TABLE_FLAGS = """
DECLARE @o int = {o}, @q nvarchar(max) = N'SELECT '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'temporal_type') IS NULL THEN N'0' ELSE N'temporal_type' END + N', '
  + CASE WHEN COL_LENGTH(N'sys.tables', N'is_node') IS NULL THEN N'0, 0' ELSE N'is_node, is_edge' END + N', '
```

In `backend/dbaguy_backend/adapters/mssql/editing.py`, replace:

```python
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, flags, columns, keys = result_sets(
        conn, batch(_OBJECT.format(o=o), _FLAGS.format(o=o), COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
```

with:

```python
    schema, name = relation_from_path(path)
    o = object_id_sql(schema or "dbo", name)
    kinds, flags, columns, keys = result_sets(
        conn, batch(_OBJECT.format(o=o), TABLE_FLAGS.format(o=o), COLUMNS.format(o=o), _KEYS.format(o=o)))
    if not kinds:
        raise NotFound(f"Table {schema}.{name} not found")
    object_type = str(kinds[0][0]).strip()
```

- [ ] **Step 5: The SQL Server catalog**

Create `backend/dbaguy_backend/adapters/mssql/dump.py`:

```python
"""What a SQL Server dump writes, and in which order (spec ⑥ §3.3.3). Read in a SNAPSHOT transaction only when the
database allows it (A4); otherwise each table is read in its turn at READ COMMITTED (A S9: the dev database, and most
others, do not), and the header says so. Catalog calls never take the metadata session's READ UNCOMMITTED.

Order. Before the data: schemas, user types (alias and table types), sequences (each starting at its next value,
A S11), the functions a table's default, check or computed column calls, tables without their foreign keys and
non-constraint indexes. After the data: identity counters (RESEED to where the source was), indexes, foreign keys,
views, functions and synonyms by sys.sql_expression_dependencies, procedures (deferred name resolution: any order),
triggers (each in its own batch, a disabled one disabled again). A module keeps the QUOTED_IDENTIFIER and ANSI_NULLS it
was created with. Left out: system-versioned, graph and ledger tables (a plain copy would not be one), CLR types,
database-level triggers, indexes DbaGuy does not write (columnstore, XML, spatial), modules whose definition is hidden
(encrypted, or no VIEW DEFINITION); partitioned tables come back unpartitioned."""

from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ...domain.errors import NotFound
from ..dump import OWNERS_AND_PRIVILEGES, DumpCatalog, DumpObject, DumpSession, DumpTable, Ordering
from . import driver
from .catalog import first_row, is_system_schema, literal, q, rows
from .columns import read_columns
from .editing import TABLE_FLAGS, table_flags_reason
from .introspection import sequence_ddl, synonym_ddl, table_statements, type_ddl

SNAPSHOT = DumpSession(setup=("SET TRANSACTION ISOLATION LEVEL SNAPSHOT", "BEGIN TRANSACTION"),
                       teardown=("ROLLBACK TRANSACTION",), consistency="snapshot")
PER_TABLE = DumpSession(setup=(), teardown=(), consistency="per_table")
SNAPSHOT_TEXT = "one consistent snapshot (SNAPSHOT isolation)"
PER_TABLE_TEXT = "not one consistent snapshot: each table was read in its turn (READ COMMITTED)"
_SNAPSHOT_ALLOWED = "SELECT snapshot_isolation_state FROM sys.databases WHERE name = DB_NAME()"
_SCHEMAS = "SELECT name, schema_id FROM sys.schemas ORDER BY schema_id"
_OBJECTS = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type), o.parent_object_id FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.schema_id IN ({schemas}) AND o.is_ms_shipped = 0 AND o.type IN ('U', 'V', 'FN', 'IF', 'TF', 'P', 'SN', 'SO', 'TR')
ORDER BY o.object_id
"""
_TYPES = """
SELECT s.name, t.name, t.is_table_type, t.is_assembly_type, t.user_type_id FROM sys.types t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_user_defined = 1 AND t.schema_id IN ({schemas}) ORDER BY t.user_type_id
"""
_COLUMN_TYPES = "SELECT DISTINCT user_type_id FROM sys.columns WHERE object_id IN ({tables})"
_SIZES = """
SELECT object_id, SUM(row_count), SUM(used_page_count) * 8192 FROM sys.dm_db_partition_stats
WHERE index_id IN (0, 1) AND object_id IN ({tables}) GROUP BY object_id
"""
_MODULES = """
SELECT object_id, definition, uses_ansi_nulls, uses_quoted_identifier FROM sys.sql_modules
WHERE object_id IN ({modules})
"""
_DEPENDS = """
SELECT referencing_id, referenced_id FROM sys.sql_expression_dependencies
WHERE referenced_id IS NOT NULL AND referencing_id IN ({modules})
"""
# The functions a table's computed column, default or check calls, and the sequences its defaults use
_TABLE_USES = """
SELECT DISTINCT d.referenced_id FROM sys.sql_expression_dependencies d JOIN sys.objects o ON o.object_id = d.referencing_id
WHERE d.referenced_id IS NOT NULL AND (o.object_id IN ({tables}) OR (o.type IN ('C', 'D') AND o.parent_object_id IN ({tables})))
"""
_TRIGGERS = "SELECT object_id, is_disabled FROM sys.triggers WHERE parent_class = 1 AND parent_id IN ({tables})"
_IDENTITY = "SELECT object_id, CAST(last_value AS nvarchar(40)) FROM sys.identity_columns WHERE object_id IN ({tables})"
_DATABASE_TRIGGERS = "SELECT COUNT(*) FROM sys.triggers WHERE parent_class = 0"
_PARTITIONED = """
SELECT COUNT(DISTINCT i.object_id) FROM sys.indexes i JOIN sys.data_spaces d ON d.data_space_id = i.data_space_id
WHERE d.type = 'PS' AND i.object_id IN ({tables})
"""
_NOT_WRITTEN_INDEXES = "SELECT COUNT(*) FROM sys.indexes WHERE object_id IN ({tables}) AND type NOT IN (0, 1, 2)"
_MODULE_KINDS = {"V": ("view", "V", "VIEW"), "FN": ("function", "FN", "FUNCTION"), "IF": ("function", "IF", "FUNCTION"),
                 "TF": ("function", "TF", "FUNCTION"), "P": ("procedure", "P", "PROCEDURE"),
                 "SN": ("synonym", "SN", "SYNONYM")}


def session(conn: driver.Connection) -> DumpSession:
    row = first_row(conn, _SNAPSHOT_ALLOWED)
    return SNAPSHOT if row is not None and row[0] == 1 else PER_TABLE


def _ids(values) -> str:
    """Object ids inlined into a catalog query (integers only); `NULL` for none, which matches nothing."""
    return ", ".join(str(int(value)) for value in values) or "NULL"


class _Reader:
    def __init__(self, conn: driver.Connection, scope: DumpScope, kinds: DumpObjectKinds, ddl: bool) -> None:
        self.conn, self.scope, self.kinds, self.ddl = conn, scope, kinds, ddl
        self.named = scope.kind == "tables"
        self.left_out: dict[str, int] = {}
        self.warnings: list[str] = []
        self.objects: dict[int, tuple] = {}  # object_id → (id, schema, name, type, parent)

    def leave_out(self, what: str, count: int = 1) -> None:
        if count:
            self.left_out[what] = self.left_out.get(what, 0) + count

    def name(self, object_id: int) -> str:
        _, schema, name, *_ = self.objects[object_id]
        return f"{schema}.{name}"

    def qualified(self, object_id: int) -> str:
        _, schema, name, *_ = self.objects[object_id]
        return f"{q(schema)}.{q(name)}"

    def read(self) -> tuple[list[DumpObject], list[DumpTable], list[DumpObject]]:
        schemas = self._schemas()
        objects = rows(self.conn, _OBJECTS.format(schemas=_ids(schemas.values())))
        self.objects = {row[0]: row for row in objects}
        picked = self._picked(objects)
        tables = [row for row in picked if row[3] == "U" and self._dumped_table(row)]
        table_ids = [row[0] for row in tables]
        uses = {object_id for (object_id,) in rows(self.conn, _TABLE_USES.format(tables=_ids(table_ids)))} \
            if table_ids else set()
        modules = self._modules(picked, uses)
        module_texts = {row[0]: row[1:] for row in rows(self.conn, _MODULES.format(modules=_ids(modules)))} \
            if modules and self.ddl else {}
        pre = [self._schema(name) for name in schemas if name != "dbo"]
        pre += self._types(table_ids, schemas)
        pre += [self._sequence(row) for row in objects if row[3] == "SO"
                and (row[0] in uses or (not self.named and self.kinds.sequences))]
        early = [object_id for object_id in modules if object_id in uses]
        pre += [item for object_id in early if (item := self._module(object_id, module_texts)) is not None]
        statements = {row[0]: table_statements(self.conn, row[1], row[2]) if self.ddl else None for row in tables}
        pre += [self._table(row, statements[row[0]]) for row in tables]
        post = [*self._identities(table_ids), *self._indexes(tables, statements), *self._foreign_keys(tables, statements)]
        late = [object_id for object_id in modules if object_id not in uses]
        post += [item for object_id in self._module_order(late)
                 if (item := self._module(object_id, module_texts)) is not None]
        post += self._triggers(table_ids)
        self._count_left_out(table_ids)
        sizes = {row[0]: row[1:] for row in rows(self.conn, _SIZES.format(tables=_ids(table_ids)))} if table_ids else {}
        data = [self._data(row, sizes.get(row[0])) for row in tables]
        return pre, data, post

    def _schemas(self) -> dict[str, int]:
        found = {name: schema_id for name, schema_id in rows(self.conn, _SCHEMAS) if not is_system_schema(name, schema_id)}
        if self.scope.kind == "database":
            return found
        if self.scope.schema_name not in found:
            raise NotFound(f"Schema {self.scope.schema_name} not found")
        return {self.scope.schema_name: found[self.scope.schema_name]}

    def _picked(self, objects: list[tuple]) -> list[tuple]:
        if not self.named:
            return objects
        wanted = set(self.scope.names)
        picked = [row for row in objects if row[2] in wanted and row[3] in ("U", "V")]
        if missing := wanted - {row[2] for row in picked}:
            raise NotFound(f"Not found in {self.scope.schema_name}: {', '.join(sorted(missing))}")
        tables = {row[0] for row in picked}
        return picked + [row for row in objects if row[3] == "TR" and row[4] in tables]

    def _dumped_table(self, row: tuple) -> bool:
        """System-versioned, graph and ledger tables are left out: a plain table would not be one (G9)."""
        flags = rows(self.conn, TABLE_FLAGS.format(o=row[0]))
        temporal = flags[0][0] if flags else 0
        reason = table_flags_reason(*(flags[0] if flags else (0, False, False, 0)))
        if reason is None and temporal != 2:
            return True
        self.leave_out("system-versioned, graph or ledger tables")
        self.warnings.append(f"{row[1]}.{row[2]} is left out: system-versioned, graph and ledger tables are not dumped")
        return False

    def _modules(self, picked: list[tuple], uses: set[int]) -> list[int]:
        found = []
        for object_id, _, _, kind, _ in picked:
            if kind == "V":
                wanted = self.kinds.views or self.named
            elif kind == "SN":
                wanted = self.kinds.views and not self.named
            elif kind in ("FN", "IF", "TF", "P"):
                wanted = (self.kinds.routines and not self.named) or object_id in uses
            else:
                wanted = False
            if wanted:
                found.append(object_id)
        return found

    def _module_order(self, modules: list[int]) -> list[int]:
        """Views, functions and synonyms by their dependencies, procedures after them."""
        ordering = Ordering()
        for referencing, referenced in rows(self.conn, _DEPENDS.format(modules=_ids(modules))) if modules else []:
            ordering.depends.setdefault(str(referencing), set()).add(str(referenced))
        first = [str(m) for m in modules if self.objects[m][3] != "P"]
        last = [str(m) for m in modules if self.objects[m][3] == "P"]
        return [int(key) for key in (*ordering.order(first), *ordering.order(last))]

    # ---- objects ----

    def _schema(self, name: str) -> DumpObject:
        create = f"CREATE SCHEMA {q(name)}"
        return DumpObject("schema", name, (f"IF SCHEMA_ID({literal(name)}) IS NULL EXEC({literal(create)})",)
                          if self.ddl else ())

    def _types(self, table_ids: list[int], schemas: dict[str, int]) -> list[DumpObject]:
        used = {type_id for (type_id,) in rows(self.conn, _COLUMN_TYPES.format(tables=_ids(table_ids)))} \
            if table_ids else set()
        found = []
        for schema, name, is_table_type, assembly, type_id in rows(self.conn, _TYPES.format(schemas=_ids(schemas.values()))):
            if assembly:
                self.leave_out("CLR types")
                continue
            if type_id not in used and (self.named or not self.kinds.types):
                continue
            statements: tuple[str, ...] = ()
            if self.ddl:
                text = type_ddl(self.conn, schema, name)
                if text is None:
                    self.leave_out("types whose definition cannot be read")
                    continue
                statements = (text.rstrip(";"),)
            qname = f"{q(schema)}.{q(name)}"
            found.append(DumpObject("type", f"{schema}.{name}", statements,
                                    f"IF TYPE_ID({literal(qname)}) IS NOT NULL DROP TYPE {qname}"))
        return found

    def _sequence(self, row: tuple) -> DumpObject:
        object_id, schema, name, *_ = row
        statements: tuple[str, ...] = ()
        if self.ddl:
            statements = (sequence_ddl(self.conn, schema, name, at_next_value=True).rstrip(";"),)
        return DumpObject("sequence", self.name(object_id), statements,
                          f"IF OBJECT_ID({literal(self.qualified(object_id))}, N'SO') IS NOT NULL "
                          f"DROP SEQUENCE {self.qualified(object_id)}")

    def _module(self, object_id: int, texts: dict[int, tuple]) -> DumpObject | None:
        _, schema, name, kind, _ = self.objects[object_id]
        label, code, word = _MODULE_KINDS[kind]
        drop = (f"IF OBJECT_ID({literal(self.qualified(object_id))}, N'{code}') IS NOT NULL "
                f"DROP {word} {self.qualified(object_id)}")
        if kind == "SN":
            statements = (synonym_ddl(self.conn, schema, name).rstrip(";"),) if self.ddl else ()
            return DumpObject(label, self.name(object_id), statements, drop)
        if not self.ddl:
            return DumpObject(label, self.name(object_id), (), drop)
        definition, ansi_nulls, quoted_identifier = texts.get(object_id, (None, True, True))
        if definition is None:
            self.leave_out("modules whose definition cannot be read (encrypted, or no VIEW DEFINITION)")
            return None
        return DumpObject(label, self.name(object_id), _module_statements(definition, ansi_nulls, quoted_identifier),
                          drop)

    def _table(self, row: tuple, statements) -> DumpObject:
        object_id = row[0]
        qname = self.qualified(object_id)
        drop = f"IF OBJECT_ID({literal(qname)}, N'U') IS NOT NULL DROP TABLE {qname}"
        if statements is not None and statements.create is None:
            self.leave_out("tables whose definition cannot be read")
            self.warnings.append(f"The definition of {self.name(object_id)} is hidden from this login: its CREATE "
                                 "TABLE is not written")
        create = (statements.create,) if statements is not None and statements.create else ()
        return DumpObject("table", self.name(object_id), create, drop)

    def _data(self, row: tuple, sizes: tuple | None) -> DumpTable:
        object_id, schema, name, *_ = row
        estimate, size = sizes if sizes is not None else (None, None)
        return DumpTable(schema=schema, table=name, name=f"{schema}.{name}",
                         columns=tuple(read_columns(self.conn, str(object_id))),
                         rows_estimate=int(estimate) if estimate is not None else None,
                         bytes_estimate=int(size) if size is not None else None)

    def _identities(self, table_ids: list[int]) -> list[DumpObject]:
        """IDENTITY_INSERT leaves the counter after the largest id written (A S2); a source whose counter had gone past
        it (deleted rows) gets it back, so that no id is ever given twice."""
        found = []
        for object_id, last_value in rows(self.conn, _IDENTITY.format(tables=_ids(table_ids))) if table_ids else []:
            if last_value is None:
                continue
            statement = f"DBCC CHECKIDENT ({literal(self.qualified(object_id))}, RESEED, {last_value}) WITH NO_INFOMSGS"
            found.append(DumpObject("identity value", self.name(object_id), (statement,) if self.ddl else (),
                                    section="data"))
        return found

    def _indexes(self, tables: list[tuple], statements: dict) -> list[DumpObject]:
        return [DumpObject("index", self.name(row[0]), (index,))
                for row in tables if (found := statements.get(row[0])) is not None for index in found.indexes]

    def _foreign_keys(self, tables: list[tuple], statements: dict) -> list[DumpObject]:
        dumped = {(row[1], row[2]) for row in tables}
        found = []
        for row in tables:
            for schema, table, statement in (statements[row[0]].foreign_keys if statements.get(row[0]) else []):
                if (schema, table) not in dumped:
                    self.warnings.append(f"A foreign key of {self.name(row[0])} is left out: it references "
                                         f"{schema}.{table}, which is not in this dump")
                    self.leave_out("foreign keys to tables not in the dump")
                    continue
                found.append(DumpObject("foreign key", self.name(row[0]), (statement,)))
        return found

    def _triggers(self, table_ids: list[int]) -> list[DumpObject]:
        if not self.kinds.triggers or not table_ids:
            return []
        triggers = rows(self.conn, _TRIGGERS.format(tables=_ids(table_ids)))
        texts = {row[0]: row[1:] for row in rows(self.conn, _MODULES.format(
            modules=_ids(object_id for object_id, _ in triggers)))} if triggers and self.ddl else {}
        found = []
        for object_id, disabled in triggers:
            _, schema, name, _, parent = self.objects[object_id]
            if not self.ddl:
                found.append(DumpObject("trigger", self.name(object_id), ()))
                continue
            definition, ansi_nulls, quoted_identifier = texts.get(object_id, (None, True, True))
            if definition is None:
                self.leave_out("modules whose definition cannot be read (encrypted, or no VIEW DEFINITION)")
                continue
            statements = _module_statements(definition, ansi_nulls, quoted_identifier)
            if disabled:
                statements += (f"DISABLE TRIGGER {q(schema)}.{q(name)} ON {self.qualified(parent)}",)
            found.append(DumpObject("trigger", self.name(object_id), statements))
        return found

    def _count_left_out(self, table_ids: list[int]) -> None:
        if self.scope.kind == "database":
            self.leave_out("database triggers", rows(self.conn, _DATABASE_TRIGGERS)[0][0])
        if table_ids:
            self.leave_out("indexes DbaGuy does not write (columnstore, XML, spatial)",
                           rows(self.conn, _NOT_WRITTEN_INDEXES.format(tables=_ids(table_ids)))[0][0])
            partitioned = rows(self.conn, _PARTITIONED.format(tables=_ids(table_ids)))[0][0]
            if partitioned:
                self.leave_out("partition schemes and filegroups (the tables come back unpartitioned)", partitioned)


def _module_statements(definition: str, ansi_nulls: bool, quoted_identifier: bool) -> tuple[str, ...]:
    """A module's definition, under the QUOTED_IDENTIFIER and ANSI_NULLS it was created with (the header sets both
    ON): each SET in a batch of its own, before, and back to ON after."""
    before = [f"SET {name} OFF" for name, on in (("QUOTED_IDENTIFIER", quoted_identifier), ("ANSI_NULLS", ansi_nulls))
              if not on]
    after = [statement.replace(" OFF", " ON") for statement in before]
    return (*before, definition.strip(), *after)


def dump_catalog(
    conn: driver.Connection, scope: DumpScope, kinds: DumpObjectKinds, server: str, database: str, *, ddl: bool = True,
) -> DumpCatalog:
    reader = _Reader(conn, scope, kinds, ddl)
    pre, tables, post = reader.read()
    consistency = session(conn).consistency
    where = f"database {database}" + (f" · schema {scope.schema_name}" if scope.schema_name else "")
    left_out = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in reader.left_out.items())]
    warnings = list(reader.warnings)
    if consistency == "per_table":
        warnings.insert(0, "Not a consistent snapshot: the database does not allow SNAPSHOT isolation, so each table "
                           "is read in its turn")
    return DumpCatalog(server=server, scope=where, consistency=consistency, pre=tuple(pre), tables=tuple(tables),
                       post=tuple(post), not_included=tuple(left_out), warnings=tuple(warnings),
                       snapshot_text=SNAPSHOT_TEXT if consistency == "snapshot" else PER_TABLE_TEXT)
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Endpoint,
    MessageListener,
    ParsedUrl,
```

with:

```python
    DatabaseAdapter,
    DatabaseConnection,
    DataTools,
    Dumping,
    Endpoint,
    MessageListener,
    ParsedUrl,
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    invalid_text_error,
    transaction_open_error,
)
from ..columns import TableColumn
from . import catalog, columns, driver, editing, freetds, introspection
from . import schema as mssql_schema
from .cancel import Call, CallKind, CancelControl, CancelledWhileWaiting, CancelPending
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
```

with:

```python
    invalid_text_error,
    transaction_open_error,
)
from ...domain.data_jobs import DumpObjectKinds, DumpScope
from ..columns import TableColumn
from ..dump import DumpCatalog, DumpSession
from . import catalog, columns, driver, dump, editing, freetds, introspection
from . import schema as mssql_schema
from .cancel import Call, CallKind, CancelControl, CancelledWhileWaiting, CancelPending
from .settings import CONNECT_TIMEOUT_S, URL_SCHEMES, connect_params, parse_mssql_url
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
        self._owner.finish_result(self, complete=self._exhausted)


class MssqlConnection(DatabaseConnection, RowEditing, DataTools):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
```

with:

```python
        self._owner.finish_result(self, complete=self._exhausted)


class MssqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
    def __init__(
        self, conn: driver.Connection, *, spid: int, database: str | None, read_only: bool,
        state: tuple[int, int, bool],  # (@@TRANCOUNT, XACT_STATE(), IMPLICIT_TRANSACTIONS) read at open
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
        self._check_database(path)
        return self._catalog_call(lambda: columns.table_columns(self._conn, path))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._metadata_call(lambda: mssql_schema.build_snapshot(self._conn))

```

with:

```python
        self._check_database(path)
        return self._catalog_call(lambda: columns.table_columns(self._conn, path))

    def dump_session(self, scope: DumpScope) -> DumpSession:
        return self._catalog_call(lambda: dump.session(self._conn))

    def dump_catalog(self, scope: DumpScope, kinds: DumpObjectKinds, *, ddl: bool = True) -> DumpCatalog:
        """The catalog call, never the metadata session's: READ UNCOMMITTED would make the dump's reads dirty."""
        server = self.server_info().version
        return self._catalog_call(lambda: dump.dump_catalog(self._conn, scope, kinds, server, self._database or "",
                                                            ddl=ddl))

    def schema_snapshot(self) -> SchemaSnapshot:
        return self._metadata_call(lambda: mssql_schema.build_snapshot(self._conn))

```

- [ ] **Step 6: Run the tests and the introspection tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mssql_dump_catalog.py tests/test_mssql_introspection.py`
Expected: `61 passed` (3 new; the object tree's DDL unchanged).

Then ⑤'s SQL Server targets, which `TABLE_FLAGS` must leave unchanged: `cd backend && .venv/bin/pytest -q tests/test_mssql_edits.py`. Expected: all pass (they make and drop their own `zz_05a_*` objects; run them alone).

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/dbaguy_backend/adapters/mssql/dump.py \
  backend/dbaguy_backend/adapters/mssql/editing.py \
  backend/dbaguy_backend/adapters/mssql/introspection.py \
  backend/tests/test_mssql_dump_catalog.py
git commit -m "feat(06a): the SQL Server dump catalog (snapshot when allowed, module order, identity reseed, left-out tables)"
```

---

### Task 8: The dump's text: `InsertBatcher`, `DumpScript`, `build_copy_select`

**Files:**
- Create: `backend/dbaguy_backend/sql/bulk_insert.py`, `backend/dbaguy_backend/sql/dump_script.py`, `backend/tests/sql/test_dump_text.py`
- Modify: `backend/dbaguy_backend/sql/dialects.py`, `backend/dbaguy_backend/sql/dump_header.py`, `backend/dbaguy_backend/sql/literals.py`, `backend/dbaguy_backend/sql/select_builder.py`

**Interfaces:**
- Consumes: ⑤'s `sql/literals.CellLiterals`, `LiteralStyle`, `ColumnCategory`; `sql/dialects` (`quote_ident`, `qualified`, `RULES`, `ClientCommand`, `datetime_literal`); Task 3 (`header_lines`); Tasks 4–5 (`TableColumn`, `DumpCatalog`, `DumpObject`, `DumpTable`).
- Produces:
  - `sql/dialects.py`: `non_finite_literal(dialect, text) -> str | None` (SQLite `9e999`, `-9e999`, `NULL`; elsewhere None: PostgreSQL's quoted `'inf'` already reads back, MySQL and SQL Server floats have none, S3); `overriding_identity(dialect)` (PostgreSQL `OVERRIDING SYSTEM VALUE`); `identity_insert(dialect, table_sql) -> (on, off) | None` (SQL Server `SET IDENTITY_INSERT … ON/OFF`). `datetime_literal` joins a datetimeoffset's offset without the space before it (`_SPACE_BEFORE_OFFSET`, S4).
  - `sql/literals.CellLiterals`: a non-finite float through `non_finite_literal`.
  - `sql/bulk_insert.InsertBatcher(dialect, table_sql, columns, *, backslash_escapes=None, max_rows=None, max_chars=MAX_INSERT_CHARS)`: `row_text(row)`, `add(row) -> str | None`, `flush() -> str | None`. A statement has no terminator; a row longer than the limit is a statement of its own.
  - `sql/select_builder.build_copy_select(dialect, schema, name, columns, where=None, *, only=False)`: the columns a dump writes, each `read_as`; the filter is checked as one expression (as browsing does); PostgreSQL `FROM ONLY`.
  - `sql/dump_header.comment_safe(text)` (S15).
  - `sql/dump_script.py`: `RESTORE_SETTINGS` and `RESET_SETTINGS` per dialect (SQL Server without `SET NOCOUNT ON`, S21); `statement_text`, `object_text` (MySQL compound objects between `DELIMITER ;;` lines); `table_sql`; `lost_columns`; `DumpScript(dialect, engine, catalog, *, content, drop_first, created)` with `head()` (header, settings, DROPs in reverse creation order), `pre()`, `table_head(table)` / `table_tail(table, rows)` (`-- Data:` comments; IDENTITY_INSERT on and off), `post()` (data-section objects such as `setval` even for data only), `tail(rows)`, `summary()`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/sql/test_dump_text.py`:

```python
"""The text a dump writes (spec ⑥ §3.3.2, D-06-5, D-06-6, D-06-7): multi-row INSERTs and their limits, each cell's
literal, the SELECT that reads the rows, and the file's sections, settings and comments. Pure."""

import math
from datetime import date, datetime
from decimal import Decimal

import pytest

from dbaguy_backend.adapters.columns import SQL_VARIANT, TableColumn
from dbaguy_backend.adapters.dump import DumpCatalog, DumpObject, DumpTable
from dbaguy_backend.domain.errors import ValidationFailed
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sql.bulk_insert import InsertBatcher
from dbaguy_backend.sql.dialects import Dialect, datetime_literal, identity_insert, non_finite_literal
from dbaguy_backend.sql.dump_header import parse_header
from dbaguy_backend.sql.dump_script import DumpScript, lost_columns, object_text, statement_text, table_sql
from dbaguy_backend.sql.select_builder import build_copy_select
from dbaguy_backend.sql.splitter import split

PG, MY, TS, LITE = Dialect.POSTGRES, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLITE


def col(name: str, kind: str = "number", **fields) -> TableColumn:
    fields.setdefault("family", "int64")
    return TableColumn(name=name, type_name="t", nullable=True, has_default=False, kind=kind, compare="equals",
                       writable=True, **fields)


def test_an_insert_writes_each_cell_as_its_columns_literal():
    columns = [col("id"), col("label", "text"), col("ok", "bool", family="bool"), col("data", "binary"),
               col("at", "datetime"), col("doc", "json"), col("code", "text", unicode=False)]
    batcher = InsertBatcher(TS, "[dbo].[t]", columns)
    batcher.add([1, "it's 中文", True, b"\x00\xff", "2026-10-05 12:34:56.123", '{"a": 1}', "abc"])
    batcher.add([2, None, False, None, None, None, "中"])
    assert batcher.flush() == (
        "INSERT INTO [dbo].[t] (id, label, ok, data, at, doc, code) VALUES\n"
        "(1, N'it''s 中文', 1, 0x00ff, N'2026-10-05T12:34:56.123', N'{\"a\": 1}', 'abc'),\n"
        "(2, NULL, 0, NULL, NULL, NULL, N'中')")  # a non-Unicode column: ASCII without N, other text with it
    assert batcher.flush() is None


@pytest.mark.parametrize(("dialect", "escapes", "expected"), [
    (MY, True, "('a\\\\b', 'x\\0y')"),  # the header's sql_mode: backslash escapes
    (PG, None, "('a\\b', NULL)"),  # PostgreSQL cannot hold U+0000: the cell is never one
    (LITE, None, "('a\\b', 'x' || char(0) || 'y')"),
])
def test_text_is_escaped_for_the_dialect_and_mode(dialect, escapes, expected):
    batcher = InsertBatcher(dialect, "t", [col("a", "text"), col("b", "text")], backslash_escapes=escapes)
    assert batcher.row_text(["a\\b", None if dialect is PG else "x\x00y"]) == expected


def test_numbers_dates_and_decimals_keep_every_digit():
    batcher = InsertBatcher(PG, "t", [col("a"), col("b"), col("c"), col("d", "datetime"), col("e", "datetime")])
    row = batcher.row_text([2**63 - 1, "12345678901234567890.1234567890", Decimal("1.10"), date(2026, 10, 5),
                            datetime(2026, 10, 5, 1, 2, 3, 456789)])
    assert row == "(9223372036854775807, 12345678901234567890.1234567890, 1.10, '2026-10-05', '2026-10-05T01:02:03.456789')"


def test_non_finite_floats_are_written_as_each_dialect_reads_them():  # R-06-11, L3
    sqlite = InsertBatcher(LITE, "t", [col("a"), col("b"), col("c"), col("d")])
    assert sqlite.row_text([math.inf, -math.inf, math.nan, 1.5]) == "(9e999, -9e999, NULL, 1.5)"
    pg = InsertBatcher(PG, "t", [col("a"), col("b")])
    assert pg.row_text([math.inf, math.nan]) == "('inf', 'nan')"
    assert non_finite_literal(MY, "inf") is None


def test_a_statement_holds_at_most_1000_rows_or_100_on_sql_server():
    for dialect, limit in ((PG, 1000), (TS, 100), (MY, 1000), (LITE, 1000)):
        batcher = InsertBatcher(dialect, "t", [col("id")])
        statements = [s for i in range(2 * limit + 1) if (s := batcher.add([i])) is not None]
        statements.append(batcher.flush())
        assert [s.count("\n") for s in statements] == [limit, limit, 1], dialect


def test_a_statement_holds_at_most_a_mebibyte_and_a_longer_row_is_alone():
    batcher = InsertBatcher(PG, "t", [col("id"), col("v", "text")], max_chars=100)
    out = [batcher.add([1, "a" * 40]), batcher.add([2, "b" * 40]), batcher.add([3, "c" * 300]), batcher.add([4, "d"])]
    out.append(batcher.flush())
    assert [s is None for s in out] == [True, True, False, False, False]
    assert [s.count("\n") for s in out if s] == [2, 1, 1]  # 1 and 2 together; 3 alone; 4 alone at the end
    assert "c" * 300 in out[3]


def test_an_identity_written_with_its_values():
    columns = [col("id", identity="always"), col("v", "text")]
    assert InsertBatcher(PG, "t", columns).add([1, "a"]) is None
    batcher = InsertBatcher(PG, "s.t", columns)
    batcher.add([1, "a"])
    assert batcher.flush().startswith("INSERT INTO s.t (id, v) OVERRIDING SYSTEM VALUE VALUES\n")
    assert InsertBatcher(PG, "t", [col("id", identity="by_default")])._head == "INSERT INTO t (id) VALUES\n"
    assert identity_insert(TS, "[s].[t]") == ("SET IDENTITY_INSERT [s].[t] ON", "SET IDENTITY_INSERT [s].[t] OFF")
    assert identity_insert(MY, "t") is None


def test_the_rows_are_read_by_the_columns_a_dump_writes():  # D-06-6
    columns = [col("id"), col("Total"), col("m", read_as="CAST({} AS numeric)"),
               col("gen", copied=False, not_copied_reason="generated column"), col("order", "text")]
    assert build_copy_select(PG, "public", "orders", columns, only=True) == (
        'SELECT id, "Total", CAST(m AS numeric) AS m, "order" FROM ONLY public.orders')
    assert build_copy_select(TS, "dbo", "t", [col("t7", read_as="CONVERT(varchar(16), {}, 126)")],
                             where="t7 > '12:00' -- noon") == (
        "SELECT CONVERT(varchar(16), t7, 126) AS t7 FROM dbo.t\nWHERE t7 > '12:00'")
    with pytest.raises(ValidationFailed):
        build_copy_select(PG, "public", "t", [col("id")], where="1 = 1; DROP TABLE t")
    with pytest.raises(ValidationFailed):
        build_copy_select(PG, "public", "t", [col("g", copied=False)])


def test_datetimeoffset_text_with_a_space_before_its_offset():  # A15: 241 with the T and the space
    assert datetime_literal(TS, "2026-10-05 12:34:56.1234567 +08:00") == "N'2026-10-05T12:34:56.1234567+08:00'"
    assert datetime_literal(TS, "2026-10-05 12:34:56 -05:30") == "N'2026-10-05T12:34:56-05:30'"
    assert datetime_literal(PG, "2026-10-05 12:34:56 +08:00") == "'2026-10-05 12:34:56 +08:00'"


def catalog(**fields) -> DumpCatalog:
    table = DumpTable(schema="public", table="t", name="public.t",
                      columns=(col("id", identity="always"), col("v", copied=False, not_copied_reason=SQL_VARIANT)))
    defaults = dict(
        server="PostgreSQL 17.11", scope="database d · schema public", consistency="snapshot",
        pre=(DumpObject("table", "public.t", ("CREATE TABLE public.t (id int)",), "DROP TABLE IF EXISTS public.t"),),
        tables=(table,),
        post=(DumpObject("sequence value", "public.t_id_seq", ("SELECT setval('s', 3, true)",), section="data"),
              DumpObject("view", "public.v", ("CREATE VIEW public.v AS SELECT 1",), "DROP VIEW IF EXISTS public.v"),
              DumpObject("index", "public.t_i", ("CREATE INDEX t_i ON public.t (id)",))),
    )
    return DumpCatalog(**{**defaults, **fields})


def whole(script: DumpScript, rows: int = 2) -> str:
    table = script.catalog.tables[0]
    data = script.table_head(table) + "INSERT INTO public.t (id) VALUES\n(1),\n(2);\n" + script.table_tail(table, rows)
    return script.head() + script.pre() + (data if script.data else "") + script.post() + script.tail(rows)


def test_the_file_reads_back_as_the_statements_it_was_written_as():
    script = DumpScript(PG, Engine.POSTGRES, catalog(), content="both", drop_first=True, created="2026-10-20T12:00:00Z")
    text = whole(script)
    assert [s.text for s in split(text, PG)] == [
        "SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false",
        "DROP VIEW IF EXISTS public.v", "DROP TABLE IF EXISTS public.t",  # every drop first, in reverse order
        "CREATE TABLE public.t (id int)", "INSERT INTO public.t (id) VALUES\n(1),\n(2)",
        "SELECT setval('s', 3, true)", "CREATE VIEW public.v AS SELECT 1", "CREATE INDEX t_i ON public.t (id)"]
    header = parse_header(text)
    assert (header.engine, header.drop_first) == (Engine.POSTGRES, True)
    assert "-- Content: schema and data · 1 table, 1 view" in text
    assert "-- Not included: owners and privileges · the values of sql_variant column public.t.v" in text
    assert text.endswith("-- Dump complete: 2 rows in 1 tables\n") and "-- public.t: 2 rows\n" in text


def test_schema_only_and_data_only():
    schema = whole(DumpScript(PG, Engine.POSTGRES, catalog(), content="schema", drop_first=False, created="x"))
    assert "INSERT" not in schema and "setval" not in schema and "CREATE VIEW" in schema
    data = whole(DumpScript(PG, Engine.POSTGRES, catalog(), content="data", drop_first=True, created="x"))
    assert "CREATE" not in data and "DROP" not in data and "setval" in data and "-- Content: data only · 1 table" in data


def test_sql_server_batches_and_its_identity_switch():
    script = DumpScript(TS, Engine.MSSQL, catalog(), content="both", drop_first=False, created="x")
    table = script.catalog.tables[0]
    assert script.table_head(table).endswith("SET IDENTITY_INSERT [public].t ON\nGO\n")  # public: reserved in T-SQL
    assert script.table_tail(table, 3) == "SET IDENTITY_INSERT [public].t OFF\nGO\n-- public.t: 3 rows\n"
    assert statement_text(TS, "SELECT 1") == "SELECT 1\nGO\n"
    assert table_sql(TS, table) == "[public].t" and table_sql(MY, table) == "t"


def test_mysql_stored_programs_go_between_delimiter_lines():
    program = DumpObject("procedure", "p", ("SET SESSION sql_mode = ''", "CREATE PROCEDURE p() BEGIN SELECT 1; END"),
                         compound=True)
    text = object_text(MY, program)
    assert text == "DELIMITER ;;\nSET SESSION sql_mode = '';;\nCREATE PROCEDURE p() BEGIN SELECT 1; END;;\nDELIMITER ;\n"
    assert [s.text for s in split(text, MY)] == ["SET SESSION sql_mode = ''", "CREATE PROCEDURE p() BEGIN SELECT 1; END"]
    script = DumpScript(MY, Engine.MYSQL, catalog(pre=(), post=()), content="schema", drop_first=False, created="x")
    assert script.tail(0).startswith("\nSET FOREIGN_KEY_CHECKS = 1;\n")


def test_a_name_with_a_line_break_never_leaves_its_comment():  # Review Focus: names that break comments
    evil = 'x\nDROP TABLE victim;--\r'
    table = DumpTable(schema="public", table=evil, name=f"public.{evil}", columns=(col("id"),))
    script = DumpScript(PG, Engine.POSTGRES, catalog(tables=(table,), scope=f"schema {evil}", pre=(), post=()),
                        content="data", drop_first=False, created="x")
    text = script.head() + script.table_head(table) + script.table_tail(table, 0) + script.tail(0)
    assert [s.text for s in split(text, PG)] == [
        "SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on", "SET check_function_bodies = false"]
    assert lost_columns(catalog()) == ["public.t.v"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_dump_text.py`
Expected: collection error (`dbaguy_backend.sql.bulk_insert` does not exist).

- [ ] **Step 3: The dialect's and the literals' additions**

In `backend/dbaguy_backend/sql/dialects.py`, replace:

```python
CASE_SENSITIVE_IDENTIFIERS: frozenset[Dialect] = frozenset({Dialect.SQLPP})
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DATE_SPACE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", re.ASCII)
NUMERIC_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)  # text that can be written as a bare number


```

with:

```python
CASE_SENSITIVE_IDENTIFIERS: frozenset[Dialect] = frozenset({Dialect.SQLPP})
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DATE_SPACE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", re.ASCII)
# A space before a time's offset (`12:34:56 +08:00`, as SSMS and CAST write a datetimeoffset): with the `T`, SQL Server
# refuses it (241, ⑥ A15), so the two are joined
_SPACE_BEFORE_OFFSET = re.compile(r"(?<=\d)\s+(?=[+-]\d{2}:\d{2}$)", re.ASCII)
# What SQLite's REAL writes for the values a number literal cannot (⑥ R-06-11, L3): 9e999 is read as infinity, while
# 'inf' would be stored as text; NaN is stored as NULL anyway. The text is Python's, as encode_value gives it.
_NON_FINITE: dict[Dialect, dict[str, str]] = {Dialect.SQLITE: {"inf": "9e999", "-inf": "-9e999", "nan": "NULL"}}
# Writing an identity column's own values (⑥ D-06-5): PostgreSQL's GENERATED ALWAYS needs OVERRIDING SYSTEM VALUE in
# the INSERT (P1); SQL Server's IDENTITY needs IDENTITY_INSERT on, one table at a time (A S2)
_OVERRIDING: dict[Dialect, str] = {Dialect.POSTGRES: "OVERRIDING SYSTEM VALUE"}
NUMERIC_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)  # text that can be written as a bare number


```

In `backend/dbaguy_backend/sql/dialects.py`, replace:

```python
        if _DATE_ONLY.fullmatch(text):
            text = text.replace("-", "")
        elif found := _DATE_SPACE_TIME.match(text):
            text = f"{found.group(1)}T{found.group(2)}{text[found.end():]}"
    return quote_literal(dialect, text, backslash_escapes=backslash_escapes)


def bool_literal(dialect: Dialect, value: bool) -> str:
```

with:

```python
        if _DATE_ONLY.fullmatch(text):
            text = text.replace("-", "")
        elif found := _DATE_SPACE_TIME.match(text):
            text = _SPACE_BEFORE_OFFSET.sub("", f"{found.group(1)}T{found.group(2)}{text[found.end():]}")
    return quote_literal(dialect, text, backslash_escapes=backslash_escapes)


def non_finite_literal(dialect: Dialect, text: str) -> str | None:
    """How the dialect writes Python's `inf`, `-inf` or `nan` as a number; None: as any other text (PostgreSQL reads
    'inf' and 'NaN' itself; MySQL and SQL Server cannot hold them)."""
    return _NON_FINITE.get(dialect, {}).get(text)


def overriding_identity(dialect: Dialect) -> str | None:
    """What an INSERT that writes a GENERATED ALWAYS identity's values says after its column list."""
    return _OVERRIDING.get(dialect)


def identity_insert(dialect: Dialect, table_sql: str) -> tuple[str, str] | None:
    """The statements around writing an IDENTITY column's values (SQL Server); None where no switch exists."""
    if dialect is not Dialect.TSQL:
        return None
    return f"SET IDENTITY_INSERT {table_sql} ON", f"SET IDENTITY_INSERT {table_sql} OFF"


def bool_literal(dialect: Dialect, value: bool) -> str:
```

In `backend/dbaguy_backend/sql/literals.py`, replace:

```python
from ..domain.errors import ValidationFailed
from ..domain.well_formed import LONE_SURROGATE
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, in_list, json_literal, quote_ident,
    quote_literal, zero_date_as_text,
)
from .jsontext import json_problem

```

with:

```python
from ..domain.errors import ValidationFailed
from ..domain.well_formed import LONE_SURROGATE
from .dialects import (
    NUMERIC_LITERAL, Dialect, bool_literal, bytes_literal, datetime_literal, in_list, json_literal, non_finite_literal,
    quote_ident, quote_literal, zero_date_as_text,
)
from .jsontext import json_problem

```

In `backend/dbaguy_backend/sql/literals.py`, replace:

```python
                return str(cell)
            case _ if category == ColumnCategory.DATETIME:
                return self._style.datetime(self._dialect, cell)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else self._style.quote(self._dialect, cell)
```

with:

```python
                return str(cell)
            case _ if category == ColumnCategory.DATETIME:
                return self._style.datetime(self._dialect, cell)
            case _ if category == ColumnCategory.NUMBER and (special := non_finite_literal(self._dialect, cell)):
                return special  # SQLite's infinity: 9e999 (⑥ R-06-11)
            case _:
                numeric = category == ColumnCategory.NUMBER and NUMERIC_LITERAL.fullmatch(cell)
                return cell if numeric else self._style.quote(self._dialect, cell)
```

In `backend/dbaguy_backend/sql/dump_header.py`, replace:

```python
_DROP_FIRST = "drop objects first: "


def header_lines(
    *, engine: Engine, server: str, scope: str, created: str, content: str, snapshot: str, not_included: list[str],
    drop_first: bool,
) -> list[str]:
    """The header's comment lines, without line breaks."""
    return [
        f"-- DbaGuy SQL dump · format {FORMAT}",
        f"-- Engine: {engine.value}{_SEPARATOR}{server}{_SEPARATOR}{scope}",
        f"-- Created: {created}",
```

with:

```python
_DROP_FIRST = "drop objects first: "


def comment_safe(text: str) -> str:
    """`text` as one line of a `--` comment: a name holding a line break would otherwise end the comment, and the rest
    of the name would run as SQL."""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def header_lines(
    *, engine: Engine, server: str, scope: str, created: str, content: str, snapshot: str, not_included: list[str],
    drop_first: bool,
) -> list[str]:
    """The header's comment lines, without line breaks."""
    lines = [
        f"-- DbaGuy SQL dump · format {FORMAT}",
        f"-- Engine: {engine.value}{_SEPARATOR}{server}{_SEPARATOR}{scope}",
        f"-- Created: {created}",
```

In `backend/dbaguy_backend/sql/dump_header.py`, replace:

```python
        f"-- Not included: {_SEPARATOR.join(not_included)}",
        f"-- Options: {_DROP_FIRST}{'yes' if drop_first else 'no'}",
    ]


def parse_header(text: str) -> DumpHeader | None:
```

with:

```python
        f"-- Not included: {_SEPARATOR.join(not_included)}",
        f"-- Options: {_DROP_FIRST}{'yes' if drop_first else 'no'}",
    ]
    return [comment_safe(line) for line in lines]


def parse_header(text: str) -> DumpHeader | None:
```

- [ ] **Step 4: The batcher, the SELECT and the script**

Create `backend/dbaguy_backend/sql/bulk_insert.py`:

```python
"""Multi-row INSERTs (spec ⑥ D-06-5): a dump's data, and later a copy's. Rows go in as the driver read them (raw
values); each cell is written as its column's literal (`sql/literals.py`: the column's kind and its literal style), so a
value reads back as itself. A statement holds at most MAX_INSERT_ROWS rows (SQL Server: TSQL_INSERT_ROWS — one VALUES
list takes at most 1,000 rows, 10738, and 100 rows load three times faster, X4) and at most MAX_INSERT_CHARS of text; a
row longer than that is a statement of its own. Pure."""

from collections.abc import Sequence
from typing import Any

from ..adapters.columns import TableColumn
from ..domain.column_category import ColumnCategory
from ..domain.edits import kind_category
from .dialects import Dialect, overriding_identity, quote_ident
from .literals import CellLiterals, LiteralStyle, normalise_raw

MAX_INSERT_ROWS = 1000
TSQL_INSERT_ROWS = 100
MAX_INSERT_CHARS = 1_048_576


class InsertBatcher:
    """`add()` a row at a time, then `flush()`; each returns a finished statement (no terminator) or None."""

    def __init__(
        self, dialect: Dialect, table_sql: str, columns: Sequence[TableColumn], *, backslash_escapes: bool | None = None,
        max_rows: int | None = None, max_chars: int = MAX_INSERT_CHARS,
    ) -> None:
        names = ", ".join(quote_ident(dialect, column.name) for column in columns)
        overriding = overriding_identity(dialect) if any(c.identity == "always" for c in columns) else None
        self._head = f"INSERT INTO {table_sql} ({names}){f' {overriding}' if overriding else ''} VALUES\n"
        writers = {unicode: CellLiterals(dialect, LiteralStyle(national=unicode, backslash_escapes=backslash_escapes))
                   for unicode in (True, False)}
        self._cells: list[tuple[CellLiterals, ColumnCategory]] = [
            (writers[column.unicode], kind_category(column.value_kind)) for column in columns]
        self._max_rows = max_rows or (TSQL_INSERT_ROWS if dialect is Dialect.TSQL else MAX_INSERT_ROWS)
        self._max_chars = max_chars
        self._rows: list[str] = []
        self._chars = 0

    def row_text(self, row: Sequence[Any]) -> str:
        return "(" + ", ".join(write(normalise_raw(value), category)
                               for value, (write, category) in zip(row, self._cells, strict=True)) + ")"

    def add(self, row: Sequence[Any]) -> str | None:
        text = self.row_text(row)
        done = None
        if self._rows and (len(self._rows) >= self._max_rows or self._chars + len(text) > self._max_chars):
            done = self.flush()
        self._rows.append(text)
        self._chars += len(text) + 2
        return done

    def flush(self) -> str | None:
        if not self._rows:
            return None
        statement = self._head + ",\n".join(self._rows)
        self._rows, self._chars = [], 0
        return statement
```

In `backend/dbaguy_backend/sql/select_builder.py`, replace:

```python

from pydantic import BaseModel

from ..domain.errors import ValidationFailed
from ..domain.metadata import NodeKind, PathSegment
from .dialects import Dialect, qualified, quote_ident
```

with:

```python

from pydantic import BaseModel

from ..adapters.columns import TableColumn
from ..domain.errors import ValidationFailed
from ..domain.metadata import NodeKind, PathSegment
from .dialects import Dialect, qualified, quote_ident
```

Append to `backend/dbaguy_backend/sql/select_builder.py`:

```python


def build_copy_select(
    dialect: Dialect, schema: str | None, name: str, columns: Sequence[TableColumn], where: str | None = None, *,
    only: bool = False,
) -> str:
    """The SELECT a dump or copy reads a relation's rows with (spec ⑥ D-06-6): the columns it writes, each through its
    read expression (PostgreSQL money as numeric, SQL Server's 7th digit as text), the table-data tab's filter when
    given (one expression, as browsing checks it), and PostgreSQL's FROM ONLY for an inheritance parent's own rows."""
    picked = []
    for column in columns:
        if not column.copied:
            continue
        quoted = quote_ident(dialect, column.name)
        picked.append(f"{column.read_as.format(quoted)} AS {quoted}" if column.read_as else quoted)
    if not picked:
        raise ValidationFailed(f"{name} has no column a dump or copy can write")
    sql = f"SELECT {', '.join(picked)} FROM {'ONLY ' if only else ''}{qualified(dialect, schema, name)}"
    if condition := _single_expression(where, dialect):
        sql += f"\nWHERE {condition}"
    return sql
```

Create `backend/dbaguy_backend/sql/dump_script.py`:

```python
"""The text of a dump file (spec ⑥ §3.3.2), piece by piece as the dump job writes it: the header, the settings a
restore needs (the literals are written for them: MySQL's backslash escapes, PostgreSQL's standard strings), the DROPs
of a `drop_first` dump, the objects before the data, each table's rows, the objects after the data, and the end. UTF-8,
LF, a `;` after each statement (T-SQL: a GO line after each batch; MySQL stored programs between DELIMITER lines).
Pure."""

from collections import Counter
from collections.abc import Iterable
from typing import Literal

from ..adapters.columns import SQL_VARIANT
from ..adapters.dump import DumpCatalog, DumpObject, DumpTable
from ..domain.profiles import Engine
from .dialects import ClientCommand, Dialect, RULES, identity_insert, qualified, quote_ident
from .dump_header import comment_safe, header_lines

Content = Literal["schema", "data", "both"]
_CONTENT = {"schema": "schema only", "data": "data only", "both": "schema and data"}
# What a restore must run under (D-06-7, §3.3.3)
RESTORE_SETTINGS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("SET client_encoding = 'UTF8'", "SET standard_conforming_strings = on",
                       "SET check_function_bodies = false"),
    Dialect.MYSQL: ("SET NAMES utf8mb4", "SET FOREIGN_KEY_CHECKS = 0",
                    "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'", "SET time_zone = '+00:00'"),
    # no SET NOCOUNT ON (unlike SSMS's scripts): under it no INSERT reports its rows, and the run of the dump would
    # say 0 rows restored
    Dialect.TSQL: ("SET QUOTED_IDENTIFIER ON", "SET ANSI_NULLS ON"),
    # foreign_keys = OFF does nothing inside a transaction (an all-or-nothing run): defer_foreign_keys then holds the
    # checks until its COMMIT
    Dialect.SQLITE: ("PRAGMA foreign_keys = OFF", "PRAGMA defer_foreign_keys = ON"),
}
RESET_SETTINGS: dict[Dialect, tuple[str, ...]] = {
    Dialect.MYSQL: ("SET FOREIGN_KEY_CHECKS = 1",), Dialect.SQLITE: ("PRAGMA foreign_keys = ON",),
}
_PLURALS = {"materialized view": "materialized views", "sequence value": "sequence values", "index": "indexes",
            "foreign key": "foreign keys", "identity value": "identity values", "sequence owner": "sequence owners"}
_COUNTED = ("table", "view", "materialized view", "sequence", "type", "domain", "function", "procedure", "trigger",
            "event", "synonym")


def statement_text(dialect: Dialect, sql: str) -> str:
    """One statement as the file writes it."""
    if RULES[dialect].client_command is ClientCommand.GO:
        return f"{sql}\nGO\n"
    return f"{sql};\n"


def object_text(dialect: Dialect, item: DumpObject) -> str:
    if item.compound and dialect is Dialect.MYSQL:
        return "DELIMITER ;;\n" + "".join(f"{statement};;\n" for statement in item.statements) + "DELIMITER ;\n"
    return "".join(statement_text(dialect, statement) for statement in item.statements)


def table_sql(dialect: Dialect, table: DumpTable) -> str:
    """The name a dump's INSERTs use (§R-06-3): PostgreSQL and SQL Server with the schema, MySQL and SQLite without
    (the file restores into the database its run uses)."""
    if dialect in (Dialect.POSTGRES, Dialect.TSQL):
        return qualified(dialect, table.schema, table.table)
    return quote_ident(dialect, table.table)


def lost_columns(catalog: DumpCatalog) -> list[str]:
    """The columns whose values a dump cannot carry (sql_variant): the header names them."""
    return [f"{table.name}.{column.name}" for table in catalog.tables for column in table.columns
            if column.not_copied_reason == SQL_VARIANT]


def _plural(kind: str, count: int) -> str:
    word = kind if count == 1 else _PLURALS.get(kind, kind + "s")
    return f"{count:,} {word}"


class DumpScript:
    """The pieces of one dump's text."""

    def __init__(
        self, dialect: Dialect, engine: Engine, catalog: DumpCatalog, *, content: Content, drop_first: bool,
        created: str,
    ) -> None:
        self.dialect, self.engine, self.catalog = dialect, engine, catalog
        self.content, self.drop_first, self.created = content, drop_first, created
        self.schema = content in ("schema", "both")
        self.data = content in ("data", "both")

    def _written(self, objects: Iterable[DumpObject]) -> list[DumpObject]:
        """The objects this content writes: the structure's, the data's (setval, counters), or both."""
        return [item for item in objects if (self.schema if item.section == "schema" else self.data)]

    def summary(self) -> str:
        counts = Counter(item.kind for item in (*self._written(self.catalog.pre), *self._written(self.catalog.post)))
        if not self.schema:
            counts["table"] = len(self.catalog.tables)
        parts = [_plural(kind, counts[kind]) for kind in _COUNTED if counts[kind]]
        return _CONTENT[self.content] + (" · " + ", ".join(parts) if parts else "")

    def head(self) -> str:
        not_included = [*self.catalog.not_included,
                        *(f"the values of sql_variant column {name}" for name in lost_columns(self.catalog))]
        lines = header_lines(
            engine=self.engine, server=self.catalog.server, scope=self.catalog.scope, created=self.created,
            content=self.summary(), snapshot=self.catalog.snapshot_text, not_included=not_included,
            drop_first=self.drop_first,
        )
        text = "\n".join(lines) + "\n\n" + "".join(statement_text(self.dialect, s) for s in RESTORE_SETTINGS[self.dialect])
        if self.drop_first and self.schema:
            drops = [item.drop for item in (*reversed(self.catalog.post), *reversed(self.catalog.pre)) if item.drop]
            if drops:
                text += "\n-- Drop objects first\n" + "".join(statement_text(self.dialect, drop) for drop in drops)
        return text

    def pre(self) -> str:
        return self._section(self._written(self.catalog.pre))

    def table_head(self, table: DumpTable) -> str:
        text = f"\n-- Data: {comment_safe(table.name)}\n"
        if (switch := self._identity(table)) is not None:
            text += statement_text(self.dialect, switch[0])
        return text

    def table_tail(self, table: DumpTable, rows: int) -> str:
        text = ""
        if (switch := self._identity(table)) is not None:
            text += statement_text(self.dialect, switch[1])
        return text + f"-- {comment_safe(table.name)}: {rows:,} rows\n"

    def post(self) -> str:
        return self._section(self._written(self.catalog.post))

    def tail(self, rows: int) -> str:
        reset = "".join(statement_text(self.dialect, s) for s in RESET_SETTINGS.get(self.dialect, ()))
        tables = len(self.catalog.tables) if self.data else 0
        return ("\n" + reset if reset else "\n") + f"-- Dump complete: {rows:,} rows in {tables:,} tables\n"

    def _identity(self, table: DumpTable) -> tuple[str, str] | None:
        if not any(column.identity == "always" and column.copied for column in table.columns):
            return None
        return identity_insert(self.dialect, table_sql(self.dialect, table))

    def _section(self, objects: list[DumpObject]) -> str:
        return "".join(f"\n-- {item.kind}: {comment_safe(item.name)}\n" + object_text(self.dialect, item)
                       for item in objects if item.statements)
```

- [ ] **Step 5: Run the tests and the literal, dialect and DML tests they touch**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_dump_text.py tests/sql/test_literals.py tests/sql/test_select_builder.py tests/sql/test_dialects_offsets.py tests/sql/test_dml.py`
Expected: `334 passed` (16 new).

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/sql/bulk_insert.py \
  backend/dbaguy_backend/sql/dialects.py \
  backend/dbaguy_backend/sql/dump_header.py \
  backend/dbaguy_backend/sql/dump_script.py \
  backend/dbaguy_backend/sql/literals.py \
  backend/dbaguy_backend/sql/select_builder.py \
  backend/tests/sql/test_dump_text.py
git commit -m "feat(06a): the dump's text — multi-row INSERTs, restore settings, DROP first, the copy SELECT"
```

---

### Task 9: `DumpJob`, `/data/dump/plan`, `/data/reveal`, `dump`; the round trips (A7)

**Files:**
- Create: `backend/dbaguy_backend/sessions/jobs/dump.py`, `backend/tests/test_dump.py`, `backend/tests/test_dump_job.py`
- Modify: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/dump.py`, `backend/dbaguy_backend/api/data.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/domain/data_jobs.py`, `backend/dbaguy_backend/domain/execution.py`, `backend/dbaguy_backend/domain/profiles.py`, `backend/dbaguy_backend/services/data_service.py`, `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/sessions/jobs/registry.py`, `backend/tests/data_support.py`, `src/lib/api/types.ts`
- Regenerate (never by hand): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: Tasks 2–8: the job framework, `RunFileJob` (to restore), `Dumping`, `DumpScript`, `InsertBatcher`, `build_copy_select`; `atomic_text_file` (the export's); `encode_row`'s raw values (the batcher takes the driver's values); `MetadataService.with_connection`.
- Produces:
  - `AdapterCapabilities.dump` and `EngineInfo.dump`: True on PostgreSQL, MySQL, SQL Server and SQLite.
  - `domain/execution.DumpCommand{type: "dump", path, scope, content: "schema" | "data" | "both", objects, drop_first, filter}`. The filter only applies to a dump of one table.
  - `domain/data_jobs`: `DumpContent`, `DumpPlanObject(kind, name, rows_estimate, notes)`, `DumpPlan(objects, not_included, consistency, warnings)`.
  - `sessions/jobs/dump.DumpJob(command, profile_name, engine)`. It sends the dump session's setup, reads the catalog, writes through `atomic_text_file`, reads each table 1,000 rows at a time with a cancel check and progress, then the teardown. The outcome is `completed` with `objects` (each table's rows), `path`, `bytes` and `rows`, or `failed` (S2); a cancel is `failed` with `cancelled=true`. A failed write gives `dump_io`: `Cannot write <path>: Disk full. Nothing was written` for ENOSPC (CR-4), the OS's text in place of `Disk full` otherwise. A wrong path gives `invalid_path`. No History record.
  - `JobRegistry.dumped(path)`; `DataService.dump_plan(profile_id, database, scope, content, kinds, path) -> DumpPlan` (metadata connection, `ddl=False`, CR-4's free-space warning under `LOW_DISK_BYTES` or the estimate); `DataService.reveal(path)` (only a file a completed dump of this run wrote; `open -R`).
  - `POST /data/dump/plan`, `POST /data/reveal` (204; 404 for any other path).
  - `adapters/sqlite/dump.JOURNAL_WARNING` (the plan's warning for a rollback journal).
  - `tests/data_support`: `_Admin.rows(database, sql)`; `DataApp.job_session(profile, database) -> JobTab`.

- [ ] **Step 1: A7 is this task's round-trip tests.** Each engine's seed is dumped through the job and run into a scratch database (`zz_06_restore_*`; SQLite a new file). Every table's rows are compared: PostgreSQL by count and `md5(string_agg(t::text …))`, the others row by row. So are sequences' and identities' next values, AUTO_INCREMENT, `sqlite_sequence`, and the lists of views, routines, triggers, events and foreign keys. The seeds are only read. MySQL runs statement by statement (S6), and all-or-nothing is also asserted to stop as `partial` at statement 5. SQL Server's `sql_variant` column comes back NULL, as its header says. If an engine's object fails to restore, that engine's catalog (Tasks 5–7) is fixed, never the test's expectations.

- [ ] **Step 2: Write the failing tests**

`test_dump_job.py` runs `DumpJob` on a fake `Dumping` connection: the file and its snapshot's end, a filter, a cancel between chunks (no file, the old one kept), a failing read and a bad path, a full disk (`ENOSPC` from the writer). `test_dump.py` holds the four round trips, a filtered one-table dump, and the routes.

In `backend/tests/data_support.py`, replace:

```python

import mysql.connector
import psycopg

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
```

with:

```python

import mysql.connector
import psycopg
from pymssql import _mssql

from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
```

In `backend/tests/data_support.py`, replace:

```python
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"


def scratch_name(kind: str) -> str:
```

with:

```python
from tests.test_sessions import Recorder, request_events

SCRATCH_PREFIX = "zz_06_"
_ROW_TUPLE = _mssql.ROW_FORMAT_TUPLE


def scratch_name(kind: str) -> str:
```

In `backend/tests/data_support.py`, replace:

```python
        """Set-up statements as the administrator, in database `name`."""
        self._run(*statements, database=name)

    def _run(self, *statements: str, database: str | None = None) -> None:
        if self.engine == "postgres":
            with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                 dbname=database or "postgres", autocommit=True) as conn:
                for statement in statements:
                    conn.execute(statement)
        elif self.engine == "mysql":
            conn = mysql.connector.connect(**mysql_driver_kwargs(user="root", password="dbaguy", autocommit=True,
                                                                 database=database or "mysql"))
```

with:

```python
        """Set-up statements as the administrator, in database `name`."""
        self._run(*statements, database=name)

    def rows(self, database: str, sql: str) -> list[tuple]:
        """A read as the administrator, in database `name` (checks compare the source and the restore with it)."""
        return self._run(sql, database=database)

    def _run(self, *statements: str, database: str | None = None) -> list[tuple]:
        """The statements in order; the rows of the last one."""
        found: list[tuple] = []
        if self.engine == "postgres":
            with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
                                 dbname=database or "postgres", autocommit=True) as conn:
                for statement in statements:
                    cursor = conn.execute(statement)
                    found = [tuple(row) for row in cursor.fetchall()] if cursor.description else []
        elif self.engine == "mysql":
            conn = mysql.connector.connect(**mysql_driver_kwargs(user="root", password="dbaguy", autocommit=True,
                                                                 database=database or "mysql"))
```

In `backend/tests/data_support.py`, replace:

```python
                for statement in statements:
                    cursor = conn.cursor()
                    cursor.execute(statement)
                    if cursor.with_rows:
                        cursor.fetchall()
                    cursor.close()
            finally:
                conn.close()
```

with:

```python
                for statement in statements:
                    cursor = conn.cursor()
                    cursor.execute(statement)
                    found = [tuple(row) for row in cursor.fetchall()] if cursor.with_rows else []
                    cursor.close()
            finally:
                conn.close()
```

In `backend/tests/data_support.py`, replace:

```python
            conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database or "master")
            try:
                for statement in statements:
                    conn.execute_non_query(statement)
            finally:
                conn.close()


@contextmanager
```

with:

```python
            conn = mssql_connect(user="sa", password=MSSQL_SA_PASSWORD, database=database or "master")
            try:
                for statement in statements:
                    conn.execute_query(statement)
                    found = [tuple(row) for row in conn.get_iterator(_ROW_TUPLE)]
                    while conn.nextresult():
                        for _ in conn.get_iterator(_ROW_TUPLE):
                            pass
            finally:
                conn.close()
        return found


@contextmanager
```

Create `backend/tests/test_dump_job.py`:

```python
"""The dump job (spec ⑥ §3.3, R-06-12, CR-4) on a fake connection: the file it writes, the snapshot it opens and ends,
and every way it stops — cancelled mid-table, a full disk, a failing read — leaving no file and any old one as it was.
The live engines follow in test_dump.py."""

import errno
import threading
from pathlib import Path

import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, Dumping, StatementCancelled, StatementError
from dbaguy_backend.adapters.columns import TableColumn
from dbaguy_backend.adapters.dump import DumpCatalog, DumpObject, DumpSession, DumpTable
from dbaguy_backend.domain.data_jobs import DumpScope
from dbaguy_backend.domain.dialect import Dialect
from dbaguy_backend.domain.execution import DumpCommand
from dbaguy_backend.domain.profiles import Engine
from dbaguy_backend.sessions.jobs import dump as dump_module
from dbaguy_backend.sessions.jobs.context import JobHooks
from dbaguy_backend.sessions.jobs.dump import DumpJob
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dump_header import parse_header
from dbaguy_backend.sql.splitter import split
from tests.fake_db import FakeConnection, FakeLease, FakeResult
from tests.test_sessions import Recorder, request_events

COLUMNS = (TableColumn(name="id", type_name="INTEGER", family="int64", nullable=False, has_default=False,
                       kind="number", compare="equals", writable=True),
           TableColumn(name="v", type_name="TEXT", family="text", nullable=True, has_default=False, kind="text",
                       compare="equals", writable=True))


class Rows(FakeResult):
    """`count` rows (id, 'v<id>'); with `gate`, waits after `stop_at` rows until a cancel, then StatementCancelled."""

    def __init__(self, count: int, conn: "DumpFake") -> None:
        super().__init__(0)
        self._next, self._count, self._conn = 0, count, conn

    def columns(self):
        return []

    def fetch(self, n):
        if self._conn.stop_at is not None and self._next >= self._conn.stop_at:
            self._conn.blocked.set()
            assert self._conn._unblock.wait(10), "never cancelled"
            raise StatementCancelled()
        end = min(self._next + n, self._count)
        rows = [[i, f"v{i}"] for i in range(self._next, end)]
        self._next = end
        return rows


class DumpFake(FakeConnection, Dumping):
    def __init__(self, rows: int = 2500, stop_at: int | None = None, fail_select: bool = False) -> None:
        super().__init__()
        self.rows, self.stop_at, self.fail_select = rows, stop_at, fail_select

    def dump_session(self, scope):
        return DumpSession(setup=("snapshot on",), teardown=("snapshot off",), consistency="snapshot")

    def dump_catalog(self, scope, kinds, *, ddl=True):
        return DumpCatalog(
            server="SQLite 3", scope="schema main", consistency="snapshot",
            pre=(DumpObject("table", "t", ('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)',), "DROP TABLE IF EXISTS t"),),
            tables=(DumpTable(schema="main", table="t", name="t", columns=COLUMNS),),
            post=(DumpObject("index", "t_v", ("CREATE INDEX t_v ON t (v)",)),), warnings=("a warning",),
        )

    def _interrupt(self, kind: str) -> None:
        super()._interrupt(kind)
        if self.blocked.is_set():  # a cancel reaches a fetch, as it reaches the driver's read in the adapters
            self._unblock.set()

    def statement(self, sql, options):
        if sql.startswith("SELECT"):
            self.runs.append(sql)
            if self.fail_select:
                raise StatementError("no such table: t", code="SQLITE_ERROR")
            return Rows(self.rows, self)
        return super().statement(sql, options)


class Env:
    def __init__(self, conn: DumpFake) -> None:
        self.conn = conn
        lease = FakeLease(conn)
        self.worker = SessionWorker("dump", lambda: lease)
        self.worker.start()
        jobs = JobHooks(build=lambda command: DumpJob(command, "Fake", Engine.SQLITE))
        self.worker.configure(WorkerHooks(dialect=Dialect.SQLITE, capabilities=AdapterCapabilities(dump=True), jobs=jobs))
        self.recorder = Recorder()
        self.worker.set_emitter(self.recorder)

    def submit(self, path: Path, **fields) -> None:
        fields.setdefault("scope", DumpScope(kind="schema", schema_name="main"))
        self.worker.submit(DumpCommand(request_id="d", path=str(path), **fields))

    def run(self, path: Path, **fields) -> list:
        self.submit(path, **fields)
        return request_events(self.recorder, "d")


@pytest.fixture
def env():
    made: list[Env] = []

    def make(conn: DumpFake | None = None) -> Env:
        made.append(Env(conn or DumpFake()))
        return made[-1]

    yield make
    for e in made:
        e.worker.close()


def outcome(events):
    [found] = [e for e in events if e.type == "job_outcome"]
    return found


def error(events):
    return next((e for e in events if e.type == "error"), None)


def test_a_dump_writes_the_file_and_ends_its_snapshot(env, tmp_path):
    e = env()
    target = tmp_path / "out.sql"
    events = e.run(target)
    done = outcome(events)
    assert (done.outcome, done.rows, done.path, done.bytes, done.warnings) == (
        "completed", 2500, str(target), target.stat().st_size, ["a warning"])
    assert [(o.kind, o.name, o.rows) for o in done.objects] == [("table", "t", 2500)]
    assert e.conn.runs == ["snapshot on", "SELECT id, v FROM main.t", "snapshot off"]
    text = target.read_text()
    assert parse_header(text).engine == Engine.SQLITE
    statements = [s.text for s in split(text, Dialect.SQLITE)]
    assert statements[:3] == ["PRAGMA foreign_keys = OFF", "PRAGMA defer_foreign_keys = ON",
                              "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"]
    inserts = [s for s in statements if s.startswith("INSERT")]
    assert [s.count("\n") for s in inserts] == [1000, 1000, 500]  # 1,000 rows a statement
    assert statements[-2:] == ["CREATE INDEX t_v ON t (v)", "PRAGMA foreign_keys = ON"]
    progress = [ev for ev in events if ev.type == "job_progress"]
    assert progress[0].stage == "data" and progress[0].object == "t" and progress[-1].rows == 2500


def test_a_cancelled_dump_leaves_no_file_and_the_old_one_as_it_was(env, tmp_path):
    e = env(DumpFake(rows=5000, stop_at=2000))
    target = tmp_path / "out.sql"
    target.write_text("the old dump")
    e.submit(target)
    assert e.conn.blocked.wait(5)
    e.worker.cancel("d")
    events = request_events(e.recorder, "d")
    done = outcome(events)
    assert (done.outcome, done.cancelled, error(events).code) == ("failed", True, "cancelled")
    assert target.read_text() == "the old dump" and sorted(p.name for p in tmp_path.iterdir()) == ["out.sql"]
    assert e.conn.runs[-1] == "snapshot off"  # the snapshot is ended all the same


def test_a_full_disk_says_so_and_leaves_nothing(env, tmp_path, monkeypatch):  # CR-4
    real = dump_module.atomic_text_file

    class Full:
        def __init__(self, file):
            self._file, self._written = file, 0

        def write(self, text):
            self._written += len(text)
            if self._written > 4096:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._file.write(text)

    def full_disk(target):
        context = real(target)

        class Wrapped:
            def __enter__(self):
                return Full(context.__enter__())

            def __exit__(self, *exc):
                return context.__exit__(*exc)

        return Wrapped()

    monkeypatch.setattr(dump_module, "atomic_text_file", full_disk)
    target = tmp_path / "out.sql"
    events = env().run(target)
    assert (outcome(events).outcome, error(events).code) == ("failed", "dump_io")
    assert error(events).message == f"Cannot write {target}: Disk full. Nothing was written"
    assert list(tmp_path.iterdir()) == []


def test_a_failing_read_and_a_bad_path(env, tmp_path):
    e = env(DumpFake(fail_select=True))
    events = e.run(tmp_path / "out.sql")
    assert (outcome(events).outcome, error(events).message) == ("failed", "no such table: t")
    assert list(tmp_path.iterdir()) == [] and e.conn.runs[-1] == "snapshot off"
    for path in (tmp_path, tmp_path / "missing" / "out.sql", Path("relative.sql")):
        events = env().run(path)
        assert (outcome(events).outcome, error(events).code) == ("not_started", "invalid_path")


def test_a_filter_reads_only_the_matching_rows(env, tmp_path):
    e = env()
    e.run(tmp_path / "out.sql", scope=DumpScope(kind="tables", schema_name="main", names=["t"]), filter="id < 10")
    assert e.conn.runs[1] == "SELECT id, v FROM main.t\nWHERE id < 10"


def test_a_cancel_between_chunks_stops_the_dump(env, tmp_path):
    e = env(DumpFake(rows=200_000))
    target = tmp_path / "out.sql"
    e.submit(target)
    e.recorder.wait_for(lambda ev: ev.type == "job_progress" and ev.rows > 0)
    threading.Thread(target=e.worker.cancel, args=("d",)).start()
    done = outcome(request_events(e.recorder, "d"))
    assert (done.outcome, done.cancelled) == ("failed", True) and not target.exists()
```

Create `backend/tests/test_dump.py`:

```python
"""Dump → Run SQL file, on the dev databases (spec ⑥ §1 success criteria, A7): each engine's seed dumped through the
job and run into a scratch database `zz_06_restore_*`; every table's rows, every sequence's and identity's next value,
every view, routine and trigger come back. The seed is only read. SQLite works on files in tmp_path."""

import sqlite3
from pathlib import Path

import pytest

from dbaguy_backend.domain.data_jobs import DumpObjectKinds, DumpScope
from dbaguy_backend.domain.execution import DumpCommand, RunFileCommand
from dbaguy_backend.sql.dump_header import parse_header
from tests.data_support import DataApp, Ran, scratch_database
from tests.edit_support import scratch_db
from tests.support import requires_mssql, requires_mysql, requires_pg


def dump(app: DataApp, profile, database: str | None, path: Path, scope: DumpScope, **fields) -> Ran:
    tab = app.job_session(profile, database)
    return tab.run(lambda rid: DumpCommand(request_id=rid, path=str(path), scope=scope, **fields), timeout=300)


def restore(app: DataApp, profile, database: str | None, path: Path, mode: str) -> Ran:
    tab = app.job_session(profile, database)
    return tab.run(lambda rid: RunFileCommand(request_id=rid, path=str(path), mode=mode), timeout=300)


def same(admin, source: str, target: str, sql: str) -> list[tuple]:
    """`sql`'s rows in the seed and in the restore, asserted equal; the seed's."""
    seed, back = admin.rows(source, sql), admin.rows(target, sql)
    assert back == seed, sql
    return seed


@pytest.fixture
def env(tmp_path, request):
    engine = request.param
    db = scratch_db(engine, tmp_path)
    app = DataApp(tmp_path)
    try:
        yield db, app, app.add_profile(db.profile())
    finally:
        app.close()
        db.close()


@requires_pg
@pytest.mark.parametrize("env", ["postgres"], indirect=True)
def test_postgresql_dumps_and_restores_its_seed_all_or_nothing(env, tmp_path):
    _, app, profile = env
    path = tmp_path / "pg.sql"
    dumped = dump(app, profile, "dbaguy", path, DumpScope(kind="database"))
    assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
    assert parse_header(path.read_text()).scope == "database dbaguy"
    with scratch_database("postgres", "restore") as (restore_db, admin):
        ran = restore(app, profile, restore_db, path, "all_or_nothing")
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        for table in ("public.customers", "sales.orders", "sales.events", "public.blobs", "public.big_table",
                      'public."Order Items"', "sales.daily_totals"):
            same(admin, "dbaguy", restore_db,
                 f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t")
        for sequence in ("public.customers_id_seq", "public.invoice_seq", "sales.events_id_seq"):
            same(admin, "dbaguy", restore_db, f"SELECT last_value, is_called FROM {sequence}")
        same(admin, "dbaguy", restore_db, "SELECT pg_sequence_last_value(pg_get_serial_sequence('sales.orders', 'id'))")
        catalog = ("SELECT n.nspname, p.proname, p.prokind FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                   "WHERE n.nspname IN ('public', 'sales') ORDER BY 1, 2")
        assert len(same(admin, "dbaguy", restore_db, catalog)) == 4
        same(admin, "dbaguy", restore_db, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1")
        same(admin, "dbaguy", restore_db, "SELECT schemaname, viewname FROM pg_views WHERE schemaname = 'sales'")
        same(admin, "dbaguy", restore_db, "SELECT conname FROM pg_constraint WHERE contype = 'f' ORDER BY 1")
        same(admin, "dbaguy", restore_db, "SELECT indexname FROM pg_indexes WHERE schemaname IN ('public', 'sales') "
                                          "ORDER BY 1")


@requires_mysql
@pytest.mark.parametrize("env", ["mysql"], indirect=True)
def test_mysql_dumps_and_restores_its_seed_statement_by_statement(env, tmp_path):
    _, app, profile = env
    path = tmp_path / "my.sql"
    dumped = dump(app, profile, None, path, DumpScope(kind="schema", schema_name="dbaguy"))
    assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
    with scratch_database("mysql", "restore") as (restore_db, admin):
        ran = restore(app, profile, restore_db, path, "statements")  # MySQL commits around DDL (R-06-7)
        assert (ran.outcome.outcome, ran.error, ran.outcome.error_count) == ("completed", None, 0)
        for table in ("customers", "orders", "types_demo", "big_table", "`Order Items`", "`tick``table`", "job_log",
                      "edge_values", "`客戶備註`", "`group`"):
            same(admin, "dbaguy", restore_db, f"SELECT * FROM {table} ORDER BY 1")
        same(admin, "dbaguy", restore_db, "SELECT TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES "
                                          "WHERE TABLE_SCHEMA = DATABASE() ORDER BY 1")
        for listing in ("SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = "
                        "DATABASE() ORDER BY 1", "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE "
                        "TRIGGER_SCHEMA = DATABASE()", "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE "
                        "EVENT_SCHEMA = DATABASE()", "SELECT TABLE_NAME FROM information_schema.VIEWS WHERE "
                        "TABLE_SCHEMA = DATABASE()"):
            assert same(admin, "dbaguy", restore_db, listing)
        same(admin, "dbaguy", restore_db, "SELECT * FROM order_summary ORDER BY 1")
    with scratch_database("mysql", "restore") as (again, admin):  # all or nothing: the first CREATE TABLE commits
        ran = restore(app, profile, again, path, "all_or_nothing")
        assert (ran.outcome.outcome, ran.error.code) == ("partial", "transaction_ended")
        assert ran.outcome.stopped_at.statement == 5  # after the four SETs of the header


@requires_mssql
@pytest.mark.parametrize("env", ["mssql"], indirect=True)
def test_sql_server_dumps_and_restores_its_seed_all_or_nothing(env, tmp_path):
    _, app, profile = env
    path = tmp_path / "ms.sql"
    dumped = dump(app, profile, "dbaguy", path, DumpScope(kind="database"))
    assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
    text = path.read_text()
    assert "-- Snapshot: not one consistent snapshot" in text  # A S9: the dev database does not allow SNAPSHOT
    assert "the values of sql_variant column dbo.types_demo.anything" in text
    with scratch_database("mssql", "restore") as (restore_db, admin):
        ran = restore(app, profile, restore_db, path, "all_or_nothing")
        assert (ran.outcome.outcome, ran.error) == ("completed", None)
        assert ran.outcome.rows == dumped.outcome.rows  # every INSERT reports its rows: no SET NOCOUNT ON (S21)
        for table, columns in (("dbo.customers", "*"), ("sales.orders", "*"), ("dbo.big_table", "*"),
                               ("dbo.[Order Items]", "*"), ("dbo.[brack]]et]", "*"), ("dbo.[客戶備註]", "*"),
                               ("dbo.[group]", "*"), ("dbo.job_log", "*"), ("dbo.variant_cases", "id"),
                               ("dbo.types_demo", "id, huge, amount, price, ratio, flag, guid, born, seen, "
                                                  "CONVERT(varchar(27), precise, 126), CONVERT(varchar(34), stamped, 126), "
                                                  "CONVERT(varchar(16), lasted, 126), data, body, CAST(doc AS nvarchar(max)), legacy")):
            same(admin, "dbaguy", restore_db, f"SELECT {columns} FROM {table} ORDER BY 1")
        assert admin.rows(restore_db, "SELECT COUNT(*) FROM dbo.types_demo WHERE anything IS NOT NULL") == [(0,)]
        for identity in ("dbo.customers", "sales.orders", "dbo.[Order Items]", "dbo.job_log"):
            same(admin, "dbaguy", restore_db, f"SELECT IDENT_CURRENT(N'{identity}')")
        same(admin, "dbaguy", restore_db, "SELECT CASE WHEN last_used_value IS NULL THEN start_value ELSE "
                                          "CAST(current_value AS bigint) + 10 END FROM sys.sequences")
        same(admin, "dbaguy", restore_db, "SELECT SCHEMA_NAME(schema_id), name, type FROM sys.objects "
                                          "WHERE is_ms_shipped = 0 AND type IN ('V', 'FN', 'IF', 'P', 'TR', 'SN', "
                                          "'SO', 'F') ORDER BY 1, 2")
        same(admin, "dbaguy", restore_db, "SELECT name FROM sys.types WHERE is_user_defined = 1 ORDER BY 1")
        same(admin, "dbaguy", restore_db, "SELECT name, filter_definition FROM sys.indexes "
                                          "WHERE object_id = OBJECT_ID(N'sales.orders') AND type = 2")


def test_sqlite_dumps_and_restores_types_counters_and_objects(tmp_path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source, isolation_level=None)
    conn.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, loose, ratio REAL, data BLOB);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id),
                            twice INTEGER GENERATED ALWAYS AS (parent_id * 2) VIRTUAL);
        CREATE INDEX child_parent ON child (parent_id);
        CREATE VIEW labels AS SELECT label FROM parent;
        CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN UPDATE parent SET label = label WHERE id = new.parent_id; END;
        INSERT INTO parent (label, loose, ratio, data) VALUES ('it''s', '007', 9e999, X'00ff'), ('中文😀', 7, -0.0, NULL),
                                                             ('gone', 1.10, 1.5, NULL);
        DELETE FROM parent WHERE label = 'gone';
        INSERT INTO child (id, parent_id) VALUES (1, 1), (2, 2);
    """)
    conn.close()
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "lite.sql"
        dumped = dump(app, profile, None, path, DumpScope(kind="schema", schema_name="main"), drop_first=True)
        assert (dumped.outcome.outcome, dumped.error) == ("completed", None)
        target = tmp_path / "target.db"
        sqlite3.connect(target).close()
        restored = app.add_profile({"profile": {"engine": "sqlite", "name": "target", "path": str(target)}})
        for _ in range(2):  # drop first: a second run replaces what the first made
            ran = restore(app, restored, None, path, "all_or_nothing")
            assert (ran.outcome.outcome, ran.error) == ("completed", None)
        checks = ("SELECT id, label, loose, typeof(loose), ratio, typeof(ratio), data FROM parent ORDER BY id",
                  "SELECT * FROM child ORDER BY id", "SELECT * FROM sqlite_sequence", "SELECT * FROM labels",
                  "SELECT type, name FROM sqlite_master ORDER BY name")
        a, b = sqlite3.connect(source), sqlite3.connect(target)
        try:
            for check in checks:
                assert b.execute(check).fetchall() == a.execute(check).fetchall(), check
        finally:
            a.close()
            b.close()
    finally:
        app.close()


def test_a_dump_of_one_table_with_a_filter(tmp_path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source, isolation_level=None)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); "
                       "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50) "
                       "INSERT INTO t SELECT i, 'v' || i FROM n;")
    conn.close()
    app = DataApp(tmp_path)
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "t.sql"
        ran = dump(app, profile, None, path, DumpScope(kind="tables", schema_name="main", names=["t"]),
                   content="data", filter="id <= 10")
        assert (ran.outcome.outcome, ran.outcome.rows) == ("completed", 10)
        assert "CREATE TABLE" not in path.read_text()
        plan = app.container.data.dump_plan(profile, None, DumpScope(kind="tables", schema_name="main", names=["t"]),
                                            "both", DumpObjectKinds(), str(path))
        assert [(o.kind, o.name, o.rows_estimate) for o in plan.objects] == [("table", "t", None)]
        assert plan.warnings == ["While the dump reads, other connections cannot write to this file (it uses a "
                                 "rollback journal, not WAL)"]
    finally:
        app.close()


def test_the_dump_routes_plan_and_show_only_what_a_dump_wrote(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from dbaguy_backend.app import create_app

    source = tmp_path / "source.db"
    sqlite3.connect(source).executescript("CREATE TABLE t (id INTEGER PRIMARY KEY); CREATE VIEW v AS SELECT id FROM t;")
    app = DataApp(tmp_path)
    shown: list[Path] = []
    monkeypatch.setattr(app.container.data, "_reveal", shown.append)  # never Finder in a test
    auth = {"Authorization": "Bearer data"}
    try:
        profile = app.add_profile({"profile": {"engine": "sqlite", "name": "source", "path": str(source)}})
        path = tmp_path / "out.sql"
        with TestClient(create_app(app.container.settings, app.container), base_url="http://127.0.0.1") as client:
            plan = client.post("/data/dump/plan", headers=auth, json={
                "profile_id": str(profile), "scope": {"kind": "schema", "schema_name": "main"}, "path": str(path)})
            assert plan.status_code == 200, plan.text
            assert [(o["kind"], o["name"]) for o in plan.json()["objects"]] == [("table", "t"), ("view", "v")]
            assert plan.json()["consistency"] == "snapshot"
            assert client.post("/data/reveal", headers=auth, json={"path": str(path)}).status_code == 404
            ran = dump(app, profile, None, path, DumpScope(kind="schema", schema_name="main"))
            assert ran.outcome.outcome == "completed"
            assert client.post("/data/reveal", headers=auth, json={"path": str(path)}).status_code == 204
            assert shown == [path]
            other = tmp_path / "other.sql"
            other.write_text("x")
            assert client.post("/data/reveal", headers=auth, json={"path": str(other)}).status_code == 404
            engines = {e["engine"]: e["dump"] for e in client.get("/system/engines", headers=auth).json()}
            assert engines == {"postgres": True, "mysql": True, "mssql": True, "couchbase": False, "sqlite": True}
    finally:
        app.close()
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_dump_job.py tests/test_dump.py`
Expected: collection error (`DumpCommand` cannot be imported).

- [ ] **Step 4: The capability, the command and the plan model**

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
    # A table-data tab's first browse waits for the edit target and sorts by its `stable_order` (PostgreSQL, 05b K13)
    orders_browse_by_key: bool = False
    run_file: bool = False  # Run SQL file (spec ⑥ R-06-1, D-06-13)


@dataclass(frozen=True)
```

with:

```python
    # A table-data tab's first browse waits for the edit target and sorts by its `stable_order` (PostgreSQL, 05b K13)
    orders_browse_by_key: bool = False
    run_file: bool = False  # Run SQL file (spec ⑥ R-06-1, D-06-13)
    dump: bool = False  # a native SQL dump (spec ⑥ R-06-1): the connection is Dumping


@dataclass(frozen=True)
```

In `backend/dbaguy_backend/adapters/base.py`, replace:

```python
            transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
            documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
            row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key, run_file=caps.run_file,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
```

with:

```python
            transactions=caps.transactions, query_context=caps.query_context, parameters=caps.parameters,
            documents=caps.documents, executed_plan=caps.executed_plan, plan_format=caps.plan_format,
            row_editing=caps.row_editing, orders_browse_by_key=caps.orders_browse_by_key, run_file=caps.run_file,
            dump=caps.dump,
        )

    def relation_path(self, database: str | None, schema: str, name: str, kind: RelationKind) -> list[PathSegment]:
```

Append to `backend/dbaguy_backend/domain/profiles.py`:

```python
    dump: bool = False  # a native SQL dump (spec ⑥ D-06-13)
```

In `backend/dbaguy_backend/adapters/postgres/adapter.py`, replace:

```python
    dialect = Dialect.POSTGRES
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
        run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

with:

```python
    dialect = Dialect.POSTGRES
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=True, row_editing=True, orders_browse_by_key=True,
        run_file=True, dump=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

In `backend/dbaguy_backend/adapters/mysql/adapter.py`, replace:

```python
    default_port = 3306
    dialect = Dialect.MYSQL
    # one connection sees every database; KILL QUERY keeps the transaction
    capabilities = AdapterCapabilities(row_editing=True, run_file=True)

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)
```

with:

```python
    default_port = 3306
    dialect = Dialect.MYSQL
    # one connection sees every database; KILL QUERY keeps the transaction
    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True)

    def parse_url(self, url: str) -> ParsedUrl:
        return parse_mysql_url(url)
```

In `backend/dbaguy_backend/adapters/mssql/adapter.py`, replace:

```python
    # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
        run_file=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

with:

```python
    # One connection works in one database; an attention keeps the transaction; read-only is DbaGuy's alone (spec §3.4).
    capabilities = AdapterCapabilities(
        per_database_connection=True, cancel_aborts_transaction=False, server_read_only=False, row_editing=True,
        run_file=True, dump=True,
    )

    def parse_url(self, url: str) -> ParsedUrl:
```

In `backend/dbaguy_backend/adapters/sqlite/adapter.py`, replace:

```python
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE
    capabilities = AdapterCapabilities(row_editing=True, run_file=True)

    def parse_url(self, url: str) -> ParsedUrl:
        """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
```

with:

```python
    url_schemes = ("sqlite",)
    default_port = None
    dialect = Dialect.SQLITE
    capabilities = AdapterCapabilities(row_editing=True, run_file=True, dump=True)

    def parse_url(self, url: str) -> ParsedUrl:
        """`sqlite:///abs/path` and `sqlite:////abs/path` (SQLAlchemy's absolute form) are absolute paths, `sqlite://rel`
```

In `backend/dbaguy_backend/adapters/sqlite/dump.py`, replace:

```python

SESSION = DumpSession(setup=("BEGIN",), teardown=("ROLLBACK",), consistency="snapshot")
SNAPSHOT_TEXT = "one consistent snapshot (a read transaction)"
_MASTER = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
_KINDS = "SELECT name, type FROM pragma_table_list WHERE schema = 'main'"  # table, view, virtual, shadow
_SEQUENCES = "SELECT name, seq FROM sqlite_sequence"
```

with:

```python

SESSION = DumpSession(setup=("BEGIN",), teardown=("ROLLBACK",), consistency="snapshot")
SNAPSHOT_TEXT = "one consistent snapshot (a read transaction)"
JOURNAL_WARNING = ("While the dump reads, other connections cannot write to this file (it uses a rollback journal, "
                   "not WAL)")  # A5
_MASTER = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
_KINDS = "SELECT name, type FROM pragma_table_list WHERE schema = 'main'"  # table, view, virtual, shadow
_SEQUENCES = "SELECT name, seq FROM sqlite_sequence"
```

In `backend/dbaguy_backend/adapters/sqlite/dump.py`, replace:

```python
    data = [DumpTable(schema="main", table=name, name=name, columns=tuple(columns_of(read_relation(conn, "main", name))))
            for name in tables]
    not_included = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in left_out.items())]
    return DumpCatalog(server=f"SQLite {sqlite3.sqlite_version}", scope="schema main", consistency="snapshot",
                       pre=tuple(pre), tables=tuple(data), post=(*counters, *post_indexes, *post_views, *post_triggers),
                       not_included=tuple(not_included), snapshot_text=SNAPSHOT_TEXT)
```

with:

```python
    data = [DumpTable(schema="main", table=name, name=name, columns=tuple(columns_of(read_relation(conn, "main", name))))
            for name in tables]
    not_included = [OWNERS_AND_PRIVILEGES, *(f"{count:,} {what}" for what, count in left_out.items())]
    (journal,) = conn.execute("PRAGMA journal_mode").fetchone()
    warnings = () if str(journal).lower() == "wal" else (JOURNAL_WARNING,)
    return DumpCatalog(server=f"SQLite {sqlite3.sqlite_version}", scope="schema main", consistency="snapshot",
                       pre=tuple(pre), tables=tuple(data), post=(*counters, *post_indexes, *post_views, *post_triggers),
                       not_included=tuple(not_included), warnings=warnings, snapshot_text=SNAPSHOT_TEXT)
```

Append to `backend/dbaguy_backend/domain/data_jobs.py`:

```python


DumpContent = Literal["schema", "data", "both"]


class DumpPlanObject(BaseModel):
    kind: str  # table, view, function, …
    name: str  # schema.name
    rows_estimate: int | None = None  # ≈, from the catalog's statistics (tables)
    notes: list[str] = []


class DumpPlan(BaseModel):
    """What a dump of a scope would write (POST /data/dump/plan): the dialog's object list and warnings. Read on the
    metadata connection, outside any snapshot; the dump itself reads the catalog again inside its own."""

    objects: list[DumpPlanObject]
    not_included: list[str]
    consistency: Literal["snapshot", "per_table"]
    warnings: list[str] = []
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import JobCommand, JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

with:

```python

from .classification import Classification, GuardReason
from .column_category import ColumnCategory
from .data_jobs import DumpContent, DumpObjectKinds, DumpScope, JobCommand, JobOutcomeEvent, JobProgressEvent
from .formats import FormatKind, RowFormatOptions

Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan
```

In `backend/dbaguy_backend/domain/execution.py`, replace:

```python
        return self


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand,
    Field(discriminator="type"),
]

```

with:

```python
        return self


class DumpCommand(JobCommand):
    """Dump a scope of this session's database to a SQL file (spec ⑥ §3.3): written through a temporary file, so a
    failed or cancelled dump leaves no file and keeps any old one."""

    job_kind = "dump"
    type: Literal["dump"] = "dump"
    path: str = Field(min_length=1, max_length=4096)  # absolute, in an existing folder
    scope: DumpScope
    content: DumpContent = "both"
    objects: DumpObjectKinds = DumpObjectKinds()
    drop_first: bool = False  # a DROP … IF EXISTS before each object (the header says so)
    filter: str | None = Field(default=None, max_length=65_536)  # one table only: the table-data tab's filter

    @model_validator(mode="after")
    def _a_filter_for_one_table_only(self) -> Self:
        if self.filter is not None and (self.scope.kind != "tables" or len(self.scope.names) != 1):
            raise ValueError("a filter applies to a dump of one table")
        return self


ClientCommand = Annotated[
    ExecuteCommand | ExportCommand | FetchMoreCommand | CancelCommand | DiscardCommand | CommitCommand
    | RollbackCommand | SetAutocommitCommand | ApplyEditsCommand | RunFileCommand | DumpCommand,
    Field(discriminator="type"),
]

```

- [ ] **Step 5: The dump job**

Create `backend/dbaguy_backend/sessions/jobs/dump.py`:

```python
"""A dump (spec ⑥ §3.3) on the job's own session: the engine's output settings and snapshot (`Dumping.dump_session`),
the catalog read inside it (`dump_catalog`), then the file — header, settings, the objects before the data, each
table's rows as multi-row INSERTs read a chunk at a time, the objects after the data — written through a temporary
file (`atomic_text_file`): a dump that fails or is cancelled leaves no file and keeps any old one (R-06-12, CR-4).

A dump is a read: no History record (like an export), and a read-only connection may dump. Logs: INFO the scope, the
file and the totals; never a value."""

import errno
from datetime import datetime
from pathlib import Path
from typing import TextIO

from loguru import logger

from ...adapters.base import Dumping, RunOptions, StatementCancelled, StatementError
from ...adapters.dump import DumpTable
from ...domain.data_jobs import JobObject
from ...domain.errors import DomainError
from ...domain.execution import DumpCommand
from ...domain.profiles import Engine
from ...sql.bulk_insert import InsertBatcher
from ...sql.dialects import Dialect
from ...sql.dump_script import DumpScript, statement_text, table_sql
from ...sql.select_builder import build_copy_select
from ...storage.atomic_file import atomic_text_file
from .context import Job, JobContext, JobReport

DUMP_IO = "dump_io"
_READ = RunOptions(describe=False, retry_safe=True)  # a dump's SELECTs return rows nobody is shown
_FETCH = 1000  # rows read at a time (the export's chunk)


def scope_text(command: DumpCommand) -> str:
    scope = command.scope
    if scope.kind == "database":
        return "the database"
    if scope.kind == "schema":
        return str(scope.schema_name)
    return f"{scope.schema_name}.{scope.names[0]}" if len(scope.names) == 1 else f"{len(scope.names)} tables"


class DumpJob(Job):
    kind = "dump"

    def __init__(self, command: DumpCommand, connection_name: str, engine: Engine) -> None:
        self._command = command
        self._name = connection_name
        self._engine = engine

    @property
    def title(self) -> str:
        return f"Dump {scope_text(self._command)} of {self._name} to {Path(self._command.path).name}"

    def run(self, ctx: JobContext) -> JobReport:
        target = self._target()
        connection = ctx.connection
        if not isinstance(connection, Dumping):
            raise StatementError("This connection cannot be dumped", code="not_supported")
        session = connection.dump_session(self._command.scope)
        try:
            for sql in session.setup:
                ctx.drain(ctx.run(sql, _READ))
        except StatementError as exc:
            self._end_session(ctx, session.teardown)
            return self._failed(ctx, [], 0, exc, cancelled=isinstance(exc, StatementCancelled))
        try:
            return self._dump(ctx, connection, target)
        finally:
            self._end_session(ctx, session.teardown)

    def broke(self, ctx: JobContext, exc: Exception) -> JobReport:
        """A dump only reads: whatever broke, nothing was written (the temporary file is gone)."""
        error = exc if isinstance(exc, StatementError) else StatementError(f"Internal error: {exc}")
        return JobReport(ctx.outcome("failed" if ctx.sent else "not_started", path=self._command.path), error)

    def _target(self) -> Path:
        target = Path(self._command.path).expanduser()
        if not target.is_absolute() or target.is_dir() or not target.parent.is_dir():
            raise StatementError(f"Choose a file in an existing folder to dump into: {self._command.path}",
                                 code="invalid_path")
        return target

    def _dump(self, ctx: JobContext, connection: Dumping, target: Path) -> JobReport:
        command = self._command
        objects: list[JobObject] = []
        rows = 0
        try:
            catalog = connection.dump_catalog(command.scope, command.objects)
            script = DumpScript(ctx.dialect, self._engine, catalog, content=command.content,
                                drop_first=command.drop_first, created=datetime.now().astimezone().isoformat("T", "seconds"))
            logger.info("[{}] dump of {} to {}: {} object(s) before the data, {} table(s), {} after", self._name,
                        scope_text(command), target, len(catalog.pre), len(catalog.tables), len(catalog.post))
            if catalog.consistency == "per_table":  # D-06-12
                logger.warning("[{}] dump of {}: not one consistent snapshot, each table is read in its turn",
                               self._name, scope_text(command))
            with atomic_text_file(target) as file:
                writer = _Writer(file, ctx)
                ctx.progress.stage = "schema"
                writer.write(script.head())
                writer.write(script.pre())
                if script.data:
                    ctx.progress.stage = "data"
                    for table in catalog.tables:
                        count = self._table(ctx, writer, script, table)
                        rows += count
                        objects.append(JobObject(kind="table", name=table.name, rows=count))
                ctx.progress.stage, ctx.progress.object = "post_data", None
                writer.write(script.post())
                writer.write(script.tail(rows))
        except StatementCancelled as exc:
            return self._failed(ctx, objects, rows, exc, cancelled=True)
        except StatementError as exc:
            return self._failed(ctx, objects, rows, exc)
        except DomainError as exc:  # the schema or a table is gone, or a scope the engine does not dump
            return self._failed(ctx, objects, rows, StatementError(exc.message, code=exc.code))
        except UnicodeEncodeError:
            return self._failed(ctx, objects, rows, StatementError(
                "A value holds text that is not valid Unicode (half of a surrogate pair): it cannot be written to a "
                "UTF-8 file. Nothing was written", code=DUMP_IO))
        except OSError as exc:
            why = "Disk full" if exc.errno == errno.ENOSPC else (exc.strerror or str(exc))
            return self._failed(ctx, objects, rows, StatementError(
                f"Cannot write {command.path}: {why}. Nothing was written", code=DUMP_IO))
        size = target.stat().st_size
        warnings = list(catalog.warnings)
        return JobReport(ctx.outcome("completed", rows=rows, bytes=size, path=str(target), objects=objects,
                                     warnings=warnings))

    def _table(self, ctx: JobContext, writer: "_Writer", script: DumpScript, table: DumpTable) -> int:
        """One table's rows; returns how many."""
        columns = [column for column in table.columns if column.copied]
        ctx.progress.object = table.name
        ctx.tick(force=True)
        writer.write(script.table_head(table))
        count = 0
        if columns:
            sql = build_copy_select(ctx.dialect, table.schema, table.table, table.columns, self._command.filter,
                                    only=table.only)
            escapes = True if ctx.dialect is Dialect.MYSQL else None  # the header's sql_mode (D-06-7)
            batcher = InsertBatcher(ctx.dialect, table_sql(ctx.dialect, table), columns, backslash_escapes=escapes)
            result = ctx.run(sql, _READ)
            try:
                while chunk := result.fetch(_FETCH):
                    for row in chunk:
                        if (statement := batcher.add(row)) is not None:
                            writer.write(statement_text(ctx.dialect, statement))
                    count += len(chunk)
                    ctx.progress.rows += len(chunk)
                    ctx.raise_if_cancelled()
                    ctx.tick()
                    if len(chunk) < _FETCH:
                        break
            finally:
                ctx.close(result)
            if (statement := batcher.flush()) is not None:
                writer.write(statement_text(ctx.dialect, statement))
        writer.write(script.table_tail(table, count))
        ctx.tick(force=True)
        return count

    def _failed(self, ctx: JobContext, objects: list[JobObject], rows: int, error: StatementError, *,
                cancelled: bool = False) -> JobReport:
        """Nothing was written (the temporary file is gone) and an old file of that name is as it was."""
        return JobReport(ctx.outcome("failed", cancelled=cancelled, rows=rows, path=self._command.path,
                                     objects=objects), error)

    @staticmethod
    def _end_session(ctx: JobContext, teardown: tuple[str, ...]) -> None:
        """End the snapshot. A Cancel never reaches it; a connection that is gone has nothing to end."""
        ctx.stop_cancels()
        for sql in teardown:
            try:
                ctx.drain(ctx.run(sql, _READ))
            except StatementError as exc:
                logger.info("[dump] ending the dump's snapshot failed ({})", exc.code)
                return


class _Writer:
    """Writes text to the dump's file and counts it for the progress (characters: about the bytes)."""

    def __init__(self, file: TextIO, ctx: JobContext) -> None:
        self._file = file
        self._ctx = ctx

    def write(self, text: str) -> None:
        self._file.write(text)
        self._ctx.progress.bytes += len(text)
```

In `backend/dbaguy_backend/sessions/jobs/registry.py`, replace:

```python
            info = self._jobs.get((session_id, request_id))
            return info.model_copy() if info is not None else None

    def clear_finished(self) -> int:
        """Forget every job that has ended; the number forgotten."""
        with self._lock:
```

with:

```python
            info = self._jobs.get((session_id, request_id))
            return info.model_copy() if info is not None else None

    def dumped(self, path: str) -> bool:
        """Whether a dump of this backend run wrote the file at `path` (Show in Finder reveals only those)."""
        with self._lock:
            return any(info.job == "dump" and info.outcome is not None and info.outcome.outcome == "completed"
                       and info.outcome.path == path for info in self._jobs.values())

    def clear_finished(self) -> int:
        """Forget every job that has ended; the number forgotten."""
        with self._lock:
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import JobCommand
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.execution import RunFileCommand
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
```

with:

```python
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import JobCommand
from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
from ..domain.execution import DumpCommand, RunFileCommand
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile, Engine
from ..services.edit_plans import EditPlanStore
from ..services.history_service import HistoryService
from ..services.profile_service import ProfileService
```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .jobs.run_file import RunFileJob
from .worker import ExecutionRecord, WorkerHooks

```

with:

```python
from ..services.safety import GuardDecision, SafetyPolicy
from ..services.schema_cache import SchemaCache
from .jobs.context import Job, JobHooks
from .jobs.dump import DumpJob
from .jobs.run_file import RunFileJob
from .worker import ExecutionRecord, WorkerHooks

```

In `backend/dbaguy_backend/sessions/hooks.py`, replace:

```python
        match command:
            case RunFileCommand() if capabilities.run_file:
                return RunFileJob(command, profile.name)
        raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")
```

with:

```python
        match command:
            case RunFileCommand() if capabilities.run_file:
                return RunFileJob(command, profile.name)
            case DumpCommand() if capabilities.dump:
                return DumpJob(command, profile.name, Engine(profile.engine))
        raise StatementError(f"{type(command).__name__} is not a job this connection runs", code="not_supported")
```

- [ ] **Step 6: The plan, Show in Finder, the routes**

In `backend/dbaguy_backend/services/data_service.py`, replace:

```python
"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts. No job runs here, and Run SQL file's
inspection opens no connection at all (a read-only profile is refused first, R-06-6).

Logs: INFO the profile and the path of each inspection; never the file's contents."""

import re
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import ENGINE_NAMES, FileInspection
from ..domain.errors import FileNotFound, NotSupported, ReadOnlyProfile
from ..domain.profiles import AnyProfile, Engine
from ..sql.dump_header import parse_header
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
_COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
_HEX_BLOB = "mysqldump: use --hex-blob"


def _decoded(head: bytes) -> tuple[str, int | None]:
```

with:

```python
"""Data tools' previews (spec ⑥ §3.2): what a dialog shows before a job starts, and Show in Finder for what a dump
wrote. No job runs here. Run SQL file's inspection opens no connection at all (a read-only profile is refused first,
R-06-6); a dump's plan reads the catalog on the metadata connection, without definitions.

Logs: INFO the profile and the path of each inspection, plan and reveal; never the file's contents."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter, DatabaseConnection, Dumping
from ..adapters.dump import DumpCatalog
from ..adapters.registry import AdapterRegistry
from ..domain.data_jobs import (
    ENGINE_NAMES, DumpContent, DumpObjectKinds, DumpPlan, DumpPlanObject, DumpScope, FileInspection,
)
from ..domain.errors import FileNotFound, NotFound, NotSupported, ReadOnlyProfile
from ..domain.profiles import AnyProfile, Engine, SqliteProfile
from ..sessions.jobs.registry import JobRegistry
from ..sql.dump_header import parse_header
from ..sql.dump_script import lost_columns
from .metadata_service import MetadataService
from .profile_service import ProfileService

PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
PREVIEW_BYTES = 64 * 1024
_COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
_HEX_BLOB = "mysqldump: use --hex-blob"
LOW_DISK_BYTES = 1 << 30  # CR-4: below this much free space a dump's plan warns, whatever its estimate
_GIB = 1 << 30


def _open_in_finder(path: Path) -> None:
    subprocess.run(["/usr/bin/open", "-R", str(path)], check=False, timeout=10)


def _decoded(head: bytes) -> tuple[str, int | None]:
```

In `backend/dbaguy_backend/services/data_service.py`, replace:

```python


class DataService:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._profiles = profiles
        self._registry = registry

    def inspect(self, profile_id: UUID, path: str) -> FileInspection:
        """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
```

with:

```python


class DataService:
    def __init__(
        self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService, jobs: JobRegistry,
        reveal: Callable[[Path], None] = _open_in_finder,
    ) -> None:
        self._profiles = profiles
        self._registry = registry
        self._metadata = metadata
        self._jobs = jobs
        self._reveal = reveal

    def dump_plan(
        self, profile_id: UUID, database: str | None, scope: DumpScope, content: DumpContent, kinds: DumpObjectKinds,
        path: str | None,
    ) -> DumpPlan:
        """What a dump of `scope` would write, its consistency and its warnings (a hidden definition, a foreign key to
        a table left out, an sql_variant column, little free disk at `path`, CR-4). A read-only profile may dump."""
        profile, _ = self._admit(profile_id, "be dumped", "dump")

        def read(connection: DatabaseConnection) -> DumpCatalog:
            if not isinstance(connection, Dumping):
                raise NotSupported(f"{ENGINE_NAMES[profile.engine]} connections cannot be dumped")
            return connection.dump_catalog(scope, kinds, ddl=False)

        catalog = self._metadata.with_connection(profile_id, database, read)
        objects = [DumpPlanObject(kind=item.kind, name=item.name) for item in catalog.pre if item.section == "schema"]
        sizes = {table.name: table for table in catalog.tables}
        objects = [item.model_copy(update={"rows_estimate": sizes[item.name].rows_estimate})
                   if item.kind == "table" and item.name in sizes else item for item in objects]
        objects += [DumpPlanObject(kind=item.kind, name=item.name) for item in catalog.post
                    if item.section == "schema" and item.kind not in ("index", "foreign key", "sequence owner")]
        warnings = list(catalog.warnings)
        warnings += [f"The values of sql_variant column {name} are not written" for name in lost_columns(catalog)]
        if path is not None and content != "schema":
            warnings += self._disk_warnings(profile, catalog, Path(path).expanduser())
        logger.info("Dump plan for {}: {} object(s), {} table(s), {}", profile.name, len(objects), len(catalog.tables),
                    catalog.consistency)
        return DumpPlan(objects=objects, not_included=list(catalog.not_included), consistency=catalog.consistency,
                        warnings=warnings)

    @staticmethod
    def _disk_warnings(profile: AnyProfile, catalog: DumpCatalog, target: Path) -> list[str]:
        """CR-4: a warning (never a refusal) when the destination's disk has less free space than about twice the
        tables' size on disk (an INSERT's text is larger than the rows it holds), or less than 1 GiB."""
        folder = target.parent
        if not folder.is_dir():
            return []
        free = shutil.disk_usage(folder).free
        estimate = 2 * sum(table.bytes_estimate or 0 for table in catalog.tables)
        if not estimate and isinstance(profile, SqliteProfile) and Path(profile.path).is_file():
            estimate = 2 * Path(profile.path).stat().st_size
        if free >= max(estimate, LOW_DISK_BYTES):
            return []
        need = f" (the dump may need about {estimate / _GIB:.1f} GB)" if estimate > free else ""
        return [f"Only {free / _GIB:.1f} GB free where the dump is written{need}"]

    def reveal(self, path: str) -> None:
        """Show in Finder: only a file a dump of this backend run wrote, and only while it is there."""
        target = Path(path)
        if not self._jobs.dumped(path) or not target.is_file():
            raise NotFound(f"{path} is not a file a dump of this run wrote")
        logger.info("Showing {} in Finder", target)
        self._reveal(target)

    def inspect(self, profile_id: UUID, path: str) -> FileInspection:
        """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
```

In `backend/dbaguy_backend/api/data.py`, replace:

```python
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.data_jobs import FileInspection, JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

```

with:

```python
from pydantic import BaseModel, ConfigDict, Field

from ..container import AppContainer
from ..domain.data_jobs import DumpContent, DumpObjectKinds, DumpPlan, DumpScope, FileInspection, JobInfo
from ..domain.errors import NotFound
from .deps import get_container, require_token

```

In `backend/dbaguy_backend/api/data.py`, replace:

```python

    profile_id: UUID  # the connection the file would run on
    path: str = Field(min_length=1, max_length=4096)


@router.post("/run-file/inspect", response_model=FileInspection)
```

with:

```python

    profile_id: UUID  # the connection the file would run on
    path: str = Field(min_length=1, max_length=4096)


class DumpPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    database: str | None = None  # the database node's (PostgreSQL, SQL Server); else the profile's
    scope: DumpScope
    content: DumpContent = "both"
    objects: DumpObjectKinds = DumpObjectKinds()
    path: str | None = Field(default=None, max_length=4096)  # where it would be written: the free-space check


class RevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


@router.post("/dump/plan", response_model=DumpPlan)
def dump_plan(body: DumpPlanRequest, container: AppContainer = Depends(get_container)) -> DumpPlan:
    """The dump dialog's object list, consistency and warnings (the metadata connection; nothing is written)."""
    return container.data.dump_plan(body.profile_id, body.database, body.scope, body.content, body.objects, body.path)


@router.post("/reveal", status_code=204)
def reveal(body: RevealRequest, container: AppContainer = Depends(get_container)) -> Response:
    """Show in Finder, for a file a dump of this backend run wrote (404 for any other path)."""
    container.data.reveal(body.path)
    return Response(status_code=204)


@router.post("/run-file/inspect", response_model=FileInspection)
```

In `backend/dbaguy_backend/container.py`, replace:

```python
        self.documents = DocumentService(self.metadata, self.profiles, self.registry)
        self.edits = EditService(self.metadata, self.profiles, self.registry)
        self.field_samples = FieldSampleService(self.metadata, self.app_settings)
        self.data = DataService(self.profiles, self.registry)
        self.history = HistoryService(history_file(data_dir))
        self.policy = SafetyPolicy()
        hooks = SessionHooksFactory(
            self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
        )
        self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=JobRegistry())
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

```

with:

```python
        self.documents = DocumentService(self.metadata, self.profiles, self.registry)
        self.edits = EditService(self.metadata, self.profiles, self.registry)
        self.field_samples = FieldSampleService(self.metadata, self.app_settings)
        self.jobs = JobRegistry()
        self.data = DataService(self.profiles, self.registry, self.metadata, self.jobs)
        self.history = HistoryService(history_file(data_dir))
        self.policy = SafetyPolicy()
        hooks = SessionHooksFactory(
            self.registry, self.profiles, self.policy, self.history, self.schema_cache, self.edits.plans,
        )
        self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None

```

- [ ] **Step 7: API types**

Regenerate the API types (never edit them by hand), from the repository root:

```bash
npm run gen:api
```

`src/lib/api/openapi.json` and `src/lib/api/schema.d.ts` change; nothing else.

In `src/lib/api/types.ts`, replace:

```ts
export type FileInspection = S["FileInspection"];
/** The header of a dump DbaGuy wrote, read back by the inspection: a hint, never a permission. */
export type DumpHeader = S["DumpHeader"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

with:

```ts
export type FileInspection = S["FileInspection"];
/** The header of a dump DbaGuy wrote, read back by the inspection: a hint, never a permission. */
export type DumpHeader = S["DumpHeader"];
/** Dump (spec ⑥ §3.3): dump a database, a schema or named tables of this session to a SQL file. */
export type DumpCommand = S["DumpCommand"];
/** Dump: what it covers ("database", "schema", "tables" with their names). */
export type DumpScope = S["DumpScope"];
/** Dump: which objects besides tables it writes (views, sequences, types, routines, triggers). */
export type DumpObjectKinds = S["DumpObjectKinds"];
/** Dump (POST /data/dump/plan): the objects, what is left out, the consistency and the warnings, before it runs. */
export type DumpPlan = S["DumpPlan"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

- [ ] **Step 8: Run the tests, the framework's and the run's again**

Run: `cd backend && .venv/bin/pytest -q tests/test_dump_job.py tests/test_dump.py tests/test_job_framework.py tests/test_run_file_inspect.py && cd .. && npm run typecheck && npm run build`
Expected: `40 passed` (12 new; about 25 seconds, mostly the four round trips); typecheck and build clean. Afterwards no `zz_06_restore_*` database is left on any server.

- [ ] **Step 9: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/dump.py \
  backend/dbaguy_backend/api/data.py \
  backend/dbaguy_backend/container.py \
  backend/dbaguy_backend/domain/data_jobs.py \
  backend/dbaguy_backend/domain/execution.py \
  backend/dbaguy_backend/domain/profiles.py \
  backend/dbaguy_backend/services/data_service.py \
  backend/dbaguy_backend/sessions/hooks.py \
  backend/dbaguy_backend/sessions/jobs/dump.py \
  backend/dbaguy_backend/sessions/jobs/registry.py \
  backend/tests/data_support.py \
  backend/tests/test_dump.py \
  backend/tests/test_dump_job.py \
  src/lib/api/openapi.json \
  src/lib/api/schema.d.ts \
  src/lib/api/types.ts
git commit -m "feat(06a): the dump job (atomic file, disk check), dump plan and Show in Finder; round trips on every engine"
```

---

### Task 10: Measurements (A10), the frozen sidecar, CLAUDE.md and the spec

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`
- Modify (only if Step 2 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/t10_probe.py`

**Interfaces:**
- Consumes: everything above; `tests/data_support` (`scratch_database`, `scratch_name`), `tests/edit_support.scratch_db`, `tests/support` (Couchbase helpers); 05a Task 11's procedure (the frozen sidecar in `src-tauri/resources/backend`, `scripts/measure-startup.sh`).
- Produces: a bundle whose frozen sidecar dumps and restores 1,000,000 rows on every relational engine and runs a file on Couchbase; A10 measured against §7's budgets; 06a's backend facts in CLAUDE.md and the spec.

- [ ] **Step 1: Build the sidecar.** Check `df -h /` first: Step 2 writes four dumps of about 230 MB in turn, each deleted after its run, and SQLite's restore file is about 430 MB. Run: `npm run build:backend`. Expected: passes; `du -sk src-tauri/resources/backend` is about 144 KB over 05b's 49,236 KB (planning: 49,380 KB; only new pure-Python modules, no dependency).

- [ ] **Step 2: Drive the frozen sidecar (A10).** Save as `<scratchpad>/t10_probe.py` and run from the repository root: `backend/.venv/bin/python <scratchpad>/t10_probe.py <scratchpad> src-tauri/resources/backend/dbaguy-backend`. It takes about 8 minutes, mostly filling, dumping and restoring 1,000,000 rows four times. It starts the sidecar as its own child with a throwaway `DBAGUY_DATA_DIR`; profiles are saved without passwords and unlocked; only `zz_06_*` scratch tables and `zz_06_restore_*` databases are made, dropped by exact name; one `zz_06_run_*` key goes into Couchbase and is removed by exact key; it stops the sidecar by its own handle.

```python
"""06a packaged check and A10: the frozen sidecar (started here as a child, with a throwaway DBAGUY_DATA_DIR, and stopped
here by its own handle) dumps a 1,000,000-row table and runs that dump into a scratch database on each relational
engine, over job sessions and their WebSocket, sampling the sidecar's RSS; then runs a small file on Couchbase. Only
zz_06_* scratch objects (tables, restore databases, one Couchbase key), dropped by exact name.

Run from the repository root: backend/.venv/bin/python <this file> <workdir> <sidecar command…>"""

import json
import os
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
from tests.data_support import scratch_database, scratch_name  # noqa: E402
from tests.edit_support import scratch_db  # noqa: E402
from tests.support import COUCHBASE_PASSWORD, couchbase_query, make_couchbase_profile, unique_name  # noqa: E402

TOKEN = "t10"
ROWS = 1_000_000
LABEL = "row label it''s 中文 😀 " + "x" * 120
FILL = {
    "postgres": "INSERT INTO {t} SELECT g, '{l}' || g, g * 1.25, timestamp '2026-01-01' + g * interval '1 second', "
                "g / 3.0, CASE WHEN g % 10 = 0 THEN NULL ELSE 'note ' || g END FROM generate_series(1, {n}) g",
    "mysql": "INSERT INTO {t} WITH RECURSIVE s (g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM s WHERE g < {n}) "
             "SELECT g, CONCAT('{l}', g), g * 1.25, TIMESTAMP '2026-01-01 00:00:00' + INTERVAL g SECOND, g / 3.0, "
             "IF(g % 10 = 0, NULL, CONCAT('note ', g)) FROM s",
    "mssql": "WITH s AS (SELECT TOP ({n}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g FROM sys.all_objects a "
             "CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c) INSERT INTO {t} SELECT g, CONCAT(N'{l}', g), "
             "g * 1.25, DATEADD(second, g, CAST('2026-01-01' AS datetime2)), g / 3.0, "
             "CASE WHEN g % 10 = 0 THEN NULL ELSE CONCAT(N'note ', g) END FROM s",
    "sqlite": "WITH RECURSIVE s (g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM s WHERE g < {n}) INSERT INTO {t} "
              "SELECT g, '{l}' || g, g * 1.25, datetime('2026-01-01', '+' || g || ' seconds'), g / 3.0, "
              "CASE WHEN g % 10 = 0 THEN NULL ELSE 'note ' || g END FROM s",
}
TYPES = {"postgres": "bigint, text, numeric(20,2), timestamp, double precision, text",
         "mysql": "BIGINT, TEXT, DECIMAL(20,2), DATETIME, DOUBLE, TEXT",
         "mssql": "bigint, nvarchar(400), decimal(20,2), datetime2(0), float, nvarchar(100)",
         "sqlite": "INTEGER, TEXT, DECIMAL(20,2), DATETIME, REAL, TEXT"}
BUDGET_S = {"postgres": (30, 120), "mysql": (30, 120), "mssql": (60, 240), "sqlite": (30, 120)}  # spec §7: dump, run

work = Path(sys.argv[1]).resolve()
command = sys.argv[2:]
data_dir = tempfile.mkdtemp(prefix="dbaguy-t10-")
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


def job(profile_id: str, database: str | None, command_body: dict) -> tuple[dict, float, float, float]:
    """One job on a session of its own: (job_outcome, seconds, RSS before, peak RSS while it ran)."""
    status, info = call("POST", "/sessions", {"profile_id": profile_id, "database": database})
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
        ws.send(json.dumps({**command_body, "request_id": "j"}))
        while True:
            event = json.loads(ws.recv(timeout=1800))
            if event["type"] == "job_outcome":
                outcome = event
            if event["type"] == "finished" and event["request_id"] == "j":
                break
    took = time.monotonic() - started
    done.set()
    sampler.join()
    call("DELETE", f"/sessions/{session}")
    return outcome, took, before, max(peak[0], rss_mb())


def check(engine: str) -> None:
    db = scratch_db(engine, work)
    body = db.profile()
    secrets = body.pop("secrets", None)  # never saved: given to this run only (unlock)
    if secrets is not None:
        body["profile"]["save_password"] = False
    status, entry = call("POST", "/profiles", body)
    assert status == 201, entry
    profile_id = entry["profile"]["id"]
    if secrets is not None:
        assert call("POST", f"/profiles/{profile_id}/unlock", secrets)[0] == 204
    name = scratch_name("big")
    columns = ", ".join(f"{c} {t}" for c, t in zip(["id", "label", "amount", "at", "ratio", "note"],
                                                   TYPES[engine].split(", ")))
    db.execute(f"CREATE TABLE {db.table(name)} ({columns}, PRIMARY KEY (id))")
    dump_path = work / f"t10_{engine}.sql"
    try:
        if engine == "mysql":
            db.execute(f"SET SESSION cte_max_recursion_depth = {ROWS + 1}")
        db.execute(FILL[engine].format(t=db.table(name), l=LABEL, n=ROWS))
        database = {"postgres": "dbaguy", "mssql": "dbaguy"}.get(engine)
        scope = {"kind": "tables", "schema_name": db.schema, "names": [name]}
        for attempt in (1, 2):
            outcome, took, before, peak = job(profile_id, database, {"type": "dump", "path": str(dump_path),
                                                                     "scope": scope})
            assert outcome["outcome"] == "completed" and outcome["rows"] == ROWS, outcome
            print(f"{engine}: dump {attempt}: {took:.1f} s (budget {BUDGET_S[engine][0]} s), "
                  f"{dump_path.stat().st_size / 1e6:.0f} MB; sidecar RSS {before:.0f} -> peak {peak:.0f} MB "
                  f"(+{peak - before:.0f})", flush=True)
        mode = "statements" if engine == "mysql" else "all_or_nothing"  # MySQL commits around DDL (R-06-7)
        run = {"type": "run_file", "path": str(dump_path), "mode": mode}
        if engine == "sqlite":
            target = work / "t10_restore.db"
            target.unlink(missing_ok=True)
            import sqlite3

            sqlite3.connect(target).close()
            status, restored = call("POST", "/profiles", {"profile": {"engine": "sqlite", "name": "t10 restore",
                                                                      "path": str(target)}})
            outcome, took, before, peak = job(restored["profile"]["id"], None, run)
            target.unlink()
        else:
            with scratch_database(engine, "restore") as (restore_db, _):
                outcome, took, before, peak = job(profile_id, restore_db, run)
        assert outcome["outcome"] == "completed" and outcome["rows"] == ROWS, outcome
        print(f"{engine}: run ({mode}): {took:.1f} s (budget {BUDGET_S[engine][1]} s), "
              f"{dump_path.stat().st_size / 1e6 / took:.1f} MB/s; sidecar RSS {before:.0f} -> peak {peak:.0f} MB "
              f"(+{peak - before:.0f})", flush=True)
    finally:
        dump_path.unlink(missing_ok=True)
        db.execute(f"DROP TABLE IF EXISTS {db.table(name)}")
        db.close()


def couchbase() -> None:
    profile = make_couchbase_profile()
    body = {"profile": {**profile.model_dump(mode="json", exclude={"id"}), "save_password": False}}
    status, entry = call("POST", "/profiles", body)
    profile_id = entry["profile"]["id"]
    call("POST", f"/profiles/{profile_id}/unlock", {"password": COUCHBASE_PASSWORD})
    key = unique_name("zz_06_run")
    path = work / "t10.sqlpp"
    path.write_text(f'UPSERT INTO orders (KEY, VALUE) VALUES ("{key}", {{"n": 1}});\n'
                    f'DELETE FROM orders USE KEYS "{key}";\n', encoding="utf-8")
    try:
        outcome, took, _, _ = job(profile_id, None, {"type": "run_file", "path": str(path), "mode": "statements",
                                                     "query_context": {"bucket": "dbaguy", "scope": "app"}})
        assert outcome["outcome"] == "completed" and outcome["statements"] == 2, outcome
        print(f"couchbase: run: {took:.1f} s", flush=True)
    finally:
        couchbase_query(f'DELETE FROM dbaguy.app.orders USE KEYS "{key}"')
        path.unlink(missing_ok=True)
    assert couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.orders")["results"] == [500]


try:
    for engine in ("sqlite", "postgres", "mysql", "mssql"):
        check(engine)
    couchbase()
finally:
    sidecar.terminate()
    sidecar.wait(timeout=30)
    print("sidecar stopped", flush=True)
```

Expected (planning ran it against the sidecar built from this plan's code with PyInstaller into the scratchpad; load average about 3):

```
sqlite: dump 1: 8.4 s (budget 30 s), 231 MB; sidecar RSS 88 -> peak 96 MB (+8)
sqlite: dump 2: 8.3 s (budget 30 s), 231 MB; sidecar RSS 91 -> peak 91 MB (+0)
sqlite: run (all_or_nothing): 45.2 s (budget 120 s), 5.1 MB/s; sidecar RSS 74 -> peak 94 MB (+21)
postgres: dump 1: 9.2 s (budget 30 s), 232 MB; sidecar RSS 52 -> peak 72 MB (+20)
postgres: dump 2: 8.8 s (budget 30 s), 232 MB; sidecar RSS 61 -> peak 65 MB (+4)
postgres: run (all_or_nothing): 54.2 s (budget 120 s), 4.3 MB/s; sidecar RSS 62 -> peak 104 MB (+41)
mysql: dump 1: 14.4 s (budget 30 s), 231 MB; sidecar RSS 74 -> peak 83 MB (+9)
mysql: dump 2: 14.2 s (budget 30 s), 231 MB; sidecar RSS 52 -> peak 92 MB (+40)
mysql: run (statements): 67.3 s (budget 120 s), 3.4 MB/s; sidecar RSS 89 -> peak 107 MB (+17)
mssql: dump 1: 18.0 s (budget 60 s), 233 MB; sidecar RSS 49 -> peak 55 MB (+6)
mssql: dump 2: 17.3 s (budget 60 s), 233 MB; sidecar RSS 49 -> peak 53 MB (+4)
mssql: run (all_or_nothing): 89.9 s (budget 240 s), 2.6 MB/s; sidecar RSS 49 -> peak 68 MB (+20)
couchbase: run: 0.0 s
sidecar stopped
```

Read the RSS as CLAUDE.md's measurements do: the "before" is wherever macOS left the idle sidecar (49–91 MB here), so the increase depends on it. The budget is about the working set: the peaks (53–107 MB) do not grow from the first dump to the second, and an in-process run of the same jobs, from a warm 88 MB, added 0–4 MB for a dump and 0–21 MB for a run. Every time is within budget, the slowest (SQL Server's run) at 37% of it. Over budget: record it, report it when over twice, and leave the speed-ups to spec §12. If a module is missing (`ModuleNotFoundError` in the sidecar's stderr), add it to `hiddenimports` in `backend/dbaguy-backend.spec`, rebuild, and run again.

- [ ] **Step 3: Build the app, check signatures, measure startup.** Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`, then `scripts/measure-startup.sh` three times. Expected: cold start and footprint within noise of 05b (0.97–1.25 s; `footprint` 164–196 MB, backend 76–77 MB), since 06a adds no frontend and about 144 KB of backend; record them. The sidecar alone, started five times while planning, printed its port in 0.46–0.49 s (the first 0.61 s), as in 05a.

- [ ] **Step 4: Update `CLAUDE.md`.** The text below is what planning measured: write Steps 1–3's numbers over the 06a measurement line and the packaging line, and add Step 3's startup to the 06a line.

In `CLAUDE.md`, replace:

```markdown
## Repo 結構

- `backend/dbaguy_backend/`：Python sidecar
  - `domain/`：pydantic 模型（profile、metadata、WS 執行協定；`edits.py`：grid 編輯的模型 EditTarget、EditChanges、EditPreview、EditPlan，與 workspace 的暫存檔 `StagedEditsFile`；`well_formed.py`：孤立 surrogate 的唯一規則 `LONE_SURROGATE`，`sql/`、`services/`、adapter 都從這裡 import）
  - `adapters/`：`base.py` 是 5 種 DB 共用的介面；每個 engine 一個子套件，在 `container.default_registry()` 註冊
    - `adapters/mysql/`：`settings`（連線參數、SSL、options）、`catalog`／`introspection`／`schema`（物件樹、DDL、補全用 snapshot）、`types`（型別表）、`adapter`（連線、執行、取消、交易）
    - `adapters/mssql/`：`settings`＋`freetds`（連線參數、URL 匯入、app 自己的 freetds.conf）、`catalog`／`introspection`／`schema`、`types`、`adapter`（連線、執行、交易）、`cancel`（取消的階段與決策）、`driver`（唯一 import pymssql 的地方；每次進 driver 都經 `GuardedConnection` 計數）
    - `adapters/couchbase/`：`settings`（URL 匯入、seed、TLS、options）、`nodes`（`nodeServices` 探索）、`http`（查詢的 keep-alive 連線；任一執行緒可 shutdown；`nodes` 的探索另外連 cluster manager）、`stream`（回應的增量解析）、`rows`（signature、`*` 與 alias 展開、溢出欄）、`messages`（server 錯誤、warnings、metrics 的文字與位置）、`result`（一個回應＝一個 `StatementResult`）、`adapter`（連線、執行、取消、唯讀）、`documents`（`SqlppDocumentStore`：只用 SQL++ 的文件讀寫、CAS 檢查、保留 expiry、binary 守衛；`changed_by_server`／`blocked_numbers` 是不用連線的純規則）、`catalog`（物件樹、DDL、補全用 snapshot）、`params`（參數數字的警告）、`explain`（plan／profile／ADVISE 的分析，純函式）、`sampling`（取樣欄位路徑的推導，純函式）
    - `adapters/cancel_window.py`：PG／MySQL／SQLite／Couchbase 共用的 arm 視窗（見「新增 engine」；SQL Server 用自己的 `cancel.py` 階段）；`adapters/sockets.py`：共用的 TCP keepalive；`adapters/documents.py`：`DocumentStore`／`DocumentCapable`（依 key 讀寫 JSON 文件的介面，與 engine 無關，目前只有 Couchbase 實作）、`DocumentRules`／`DocumentEngine`（只看文字就知道的規則，不用連線，所以在 request 執行緒檢查，C43）；`adapters/editing.py`：grid 編輯的 key 怎麼選（`build_target`，與 engine 無關）與 `RowEditing` 介面，四個關聯式 engine 各有 `adapters/{postgres,mysql,mssql,sqlite}/editing.py`（`edit_target`：從 catalog 讀 key、kind、能不能寫、能不能比對；PG 的另外以 EXPLAIN 判斷 `stable_order`，05b K13）
  - `sessions/worker.py`：每個 session 一條連線、一個執行緒；分頁、暫停、取消都在這裡通用處理；`sessions/options.py`：每次請求的選項與參數檢查（引擎只會收到它的能力欄位宣告過的選項）；`sessions/read_ahead.py`：暫停結果的停滯時鐘（`StallClock`），worker 的預讀與閒置關閉都看它；`sessions/edit_apply.py`：grid 編輯的 apply 怎麼結束（每句要符合幾列、四種結果與訊息，純函式，worker 做 I/O 再來問它）
  - `sql/`：純函式的 SQL 文字處理（lexer、切分、分類、補全、copy/匯出格式、表格瀏覽 SELECT；`tsql.py` 是 T-SQL 批次內的語句切分；`format_guard.py` 是 formatter 的 token 守衛，所有方言：排版後的有意義 token 和原文不同就不採用；`jsontext.py` 是不經 parse（不建樹）的 JSON 原文處理：`one_line`（Couchbase 的 JSON Lines 匯出用）、`value_end`（不建樹找值的結尾：回應解析的巢狀值與文件檢查共用）、存檔前的 `json_problem`／`number_paths`／`diff`（文件存檔流程；逐層走文字、只記 offset，巢狀上限 `MAX_DOCUMENT_DEPTH` = 100）；`keyspaces.py` 是 snapshot 的 bucket／scope／collection 三層查詢、`sqlpp_completion.py` 是 SQL++ 補全，兩者只給 Couchbase）；API 位移一律 UTF-16，只在 `sql/offsets.py` 轉換（`utf16_offset`）。各方言的識別字引號與 literal 只從 `sql/dialects.py` 來，其他地方（含前端）不自己拼；`literals.py` 是 cell 的正規化與 literal、比對條件（Copy、Filter by this value 與 grid 編輯的 DML 共用，從 `formatting.py` 抽出），`dml.py` 是 grid commit 的 DML（`build_plan`：純函式，每列一句，驗證與警告）
  - `services/`：profile、連線（含 SSH tunnel）、metadata、schema 快取、安全政策、history、設定／工作區／scripts；Couchbase 的 `couchbase_service.py`（query context 清單、ADVISE、History 的 context）、`field_sample_service.py`（取樣快取）、`document_service.py`（文件存檔流程：守衛、server 形式的預覽、確認；經 metadata 連線，寫入不重送）；`metadata_errors.py`（`server_errors_are_final()`：server 的錯誤回應就是答案，metadata 連線不重連、不重送）；`edit_service.py`（grid 編輯的 edit target 與預覽）、`edit_plans.py`（預覽出來的一次性 plan：`EditPlanStore`）
  - `api/`：FastAPI routers；WS 在 `api/sessions.py`；Couchbase 的 contexts／plan／advise／fields／statement-keyspace／documents 在 `api/couchbase.py`；grid 編輯的 `/edits/target`、`/edits/preview` 在 `api/edits.py`（套用是 WS 的 `apply_edits`）
- `src/`：React 前端；API 型別由 `npm run gen:api` 從 OpenAPI 產生（`src/lib/api/schema.d.ts`，不要手改）
  - `features/`：editor（CodeMirror 6）、query（執行流程）、session（WS 控制器、關閉保護）、workspace（自動存檔與還原）、grid、table-data（表格資料 tab）、scripts、history、palette、settings、explorer、explain、documents、edits
    - Couchbase（04b）：`query/{queryContext, ContextPicker, params, ParamsPanel}`（tab 的 context 與參數面板）、`explorer/{browse, fieldSamples, SampledFields, SampleFieldsDialog}`（瀏覽語句與取樣欄位）、`explain/{PlanView, PlanResult, planApi, planModel, explainText}`（plan 檢視；Explain 的前綴規則只在 `explainText.ts`）
```

with:

```markdown
## Repo 結構

- `backend/dbaguy_backend/`：Python sidecar
  - `domain/`：pydantic 模型（profile、metadata、WS 執行協定；`edits.py`：grid 編輯的模型 EditTarget、EditChanges、EditPreview、EditPlan，與 workspace 的暫存檔 `StagedEditsFile`；`well_formed.py`：孤立 surrogate 的唯一規則 `LONE_SURROGATE`，`sql/`、`services/`、adapter 都從這裡 import；`data_jobs.py`：資料工具的 job 與它們的 WS 事件（`JobCommand`、`JobProgressEvent`、`JobOutcomeEvent`、`JobInfo`）、dump 的範圍與預覽（`DumpScope`、`DumpObjectKinds`、`DumpPlan`）、Run SQL file 的檢查（`FileInspection`、`DumpHeader`））
  - `adapters/`：`base.py` 是 5 種 DB 共用的介面；每個 engine 一個子套件，在 `container.default_registry()` 註冊
    - `adapters/mysql/`：`settings`（連線參數、SSL、options）、`catalog`／`introspection`／`schema`（物件樹、DDL、補全用 snapshot）、`types`（型別表）、`adapter`（連線、執行、取消、交易）
    - `adapters/mssql/`：`settings`＋`freetds`（連線參數、URL 匯入、app 自己的 freetds.conf）、`catalog`／`introspection`／`schema`、`types`、`adapter`（連線、執行、交易）、`cancel`（取消的階段與決策）、`driver`（唯一 import pymssql 的地方；每次進 driver 都經 `GuardedConnection` 計數）
    - `adapters/couchbase/`：`settings`（URL 匯入、seed、TLS、options）、`nodes`（`nodeServices` 探索）、`http`（查詢的 keep-alive 連線；任一執行緒可 shutdown；`nodes` 的探索另外連 cluster manager）、`stream`（回應的增量解析）、`rows`（signature、`*` 與 alias 展開、溢出欄）、`messages`（server 錯誤、warnings、metrics 的文字與位置）、`result`（一個回應＝一個 `StatementResult`）、`adapter`（連線、執行、取消、唯讀）、`documents`（`SqlppDocumentStore`：只用 SQL++ 的文件讀寫、CAS 檢查、保留 expiry、binary 守衛；`changed_by_server`／`blocked_numbers` 是不用連線的純規則）、`catalog`（物件樹、DDL、補全用 snapshot）、`params`（參數數字的警告）、`explain`（plan／profile／ADVISE 的分析，純函式）、`sampling`（取樣欄位路徑的推導，純函式）
    - `adapters/cancel_window.py`：PG／MySQL／SQLite／Couchbase 共用的 arm 視窗（見「新增 engine」；SQL Server 用自己的 `cancel.py` 階段）；`adapters/sockets.py`：共用的 TCP keepalive；`adapters/documents.py`：`DocumentStore`／`DocumentCapable`（依 key 讀寫 JSON 文件的介面，與 engine 無關，目前只有 Couchbase 實作）、`DocumentRules`／`DocumentEngine`（只看文字就知道的規則，不用連線，所以在 request 執行緒檢查，C43）；`adapters/editing.py`：grid 編輯的 key 怎麼選（`build_target`，與 engine 無關）與 `RowEditing` 介面，四個關聯式 engine 各有 `adapters/{postgres,mysql,mssql,sqlite}/editing.py`（`edit_target`：從 catalog 讀 key、kind、能不能寫、能不能比對；PG 的另外以 EXPLAIN 判斷 `stable_order`，05b K13）
    - `adapters/columns.py`：表格欄位的 engine 中立形狀（`TableColumn`、`TypeFamily`、dump 讀那一欄的 `read_as`；`DataTools.table_columns`，⑤ 的 edit target 也從它讀）；`adapters/dump.py`：dump 讀出的 catalog（`DumpCatalog`、`DumpObject`、`DumpTable`、`DumpSession`、依相依排序的 `Ordering`）；四個關聯式 engine 各有 `adapters/{postgres,mysql,mssql,sqlite}/columns.py`（`table_columns`）與 `dump.py`（`Dumping.dump_session`／`dump_catalog`：順序、snapshot、略過的物件）
  - `sessions/worker.py`：每個 session 一條連線、一個執行緒；分頁、暫停、取消都在這裡通用處理；`sessions/options.py`：每次請求的選項與參數檢查（引擎只會收到它的能力欄位宣告過的選項）；`sessions/read_ahead.py`：暫停結果的停滯時鐘（`StallClock`），worker 的預讀與閒置關閉都看它；`sessions/edit_apply.py`：grid 編輯的 apply 怎麼結束（每句要符合幾列、四種結果與訊息，純函式，worker 做 I/O 再來問它）；`sessions/jobs/`：資料工具的 job 框架（`context.py`：`Job`、`JobContext`、`JobHooks`、`JobReport`；`registry.py`：`JobRegistry`，`GET /data/jobs` 的清單）與兩種 job（`run_file.py`：Run SQL file 與它的結果表；`dump.py`：`DumpJob`）
  - `sql/`：純函式的 SQL 文字處理（lexer、切分、分類、補全、copy/匯出格式、表格瀏覽 SELECT；`tsql.py` 是 T-SQL 批次內的語句切分；`format_guard.py` 是 formatter 的 token 守衛，所有方言：排版後的有意義 token 和原文不同就不採用；`jsontext.py` 是不經 parse（不建樹）的 JSON 原文處理：`one_line`（Couchbase 的 JSON Lines 匯出用）、`value_end`（不建樹找值的結尾：回應解析的巢狀值與文件檢查共用）、存檔前的 `json_problem`／`number_paths`／`diff`（文件存檔流程；逐層走文字、只記 offset，巢狀上限 `MAX_DOCUMENT_DEPTH` = 100）；`keyspaces.py` 是 snapshot 的 bucket／scope／collection 三層查詢、`sqlpp_completion.py` 是 SQL++ 補全，兩者只給 Couchbase）；API 位移一律 UTF-16，只在 `sql/offsets.py` 轉換（`utf16_offset`）。各方言的識別字引號與 literal 只從 `sql/dialects.py` 來，其他地方（含前端）不自己拼；`literals.py` 是 cell 的正規化與 literal、比對條件（Copy、Filter by this value 與 grid 編輯的 DML 共用，從 `formatting.py` 抽出），`dml.py` 是 grid commit 的 DML（`build_plan`：純函式，每列一句，驗證與警告）；`statement_stream.py` 是檔案的串流切句（Run SQL file：512 KiB 一塊、`iter_spans` 接著切，行號、byte 位置、`GO n`）；`dump_header.py` 是 ⑥ dump 的檔頭（寫與讀同一份格式）；`bulk_insert.py` 是多列 INSERT（`InsertBatcher`）；`dump_script.py` 是 dump 檔的文字（檔頭、還原設定、DROP、物件、資料）
  - `services/`：profile、連線（含 SSH tunnel）、metadata、schema 快取、安全政策、history、設定／工作區／scripts；Couchbase 的 `couchbase_service.py`（query context 清單、ADVISE、History 的 context）、`field_sample_service.py`（取樣快取）、`document_service.py`（文件存檔流程：守衛、server 形式的預覽、確認；經 metadata 連線，寫入不重送）；`metadata_errors.py`（`server_errors_are_final()`：server 的錯誤回應就是答案，metadata 連線不重連、不重送）；`edit_service.py`（grid 編輯的 edit target 與預覽）、`edit_plans.py`（預覽出來的一次性 plan：`EditPlanStore`）；`data_service.py`（Run SQL file 的檢查、dump 的預覽、Show in Finder）
  - `api/`：FastAPI routers；WS 在 `api/sessions.py`；Couchbase 的 contexts／plan／advise／fields／statement-keyspace／documents 在 `api/couchbase.py`；grid 編輯的 `/edits/target`、`/edits/preview` 在 `api/edits.py`（套用是 WS 的 `apply_edits`）；資料工具的 `/data/jobs`（清單、cancel、clear）、`/data/run-file/inspect`、`/data/dump/plan`、`/data/reveal` 在 `api/data.py`（job 本身是 job session 的 WS 指令 `dump`／`run_file`）
- `src/`：React 前端；API 型別由 `npm run gen:api` 從 OpenAPI 產生（`src/lib/api/schema.d.ts`，不要手改）
  - `features/`：editor（CodeMirror 6）、query（執行流程）、session（WS 控制器、關閉保護）、workspace（自動存檔與還原）、grid、table-data（表格資料 tab）、scripts、history、palette、settings、explorer、explain、documents、edits
    - Couchbase（04b）：`query/{queryContext, ContextPicker, params, ParamsPanel}`（tab 的 context 與參數面板）、`explorer/{browse, fieldSamples, SampledFields, SampleFieldsDialog}`（瀏覽語句與取樣欄位）、`explain/{PlanView, PlanResult, planApi, planModel, explainText}`（plan 檢視；Explain 的前綴規則只在 `explainText.ts`）
```

In `CLAUDE.md`, replace:

```markdown
  - 編輯列與選單：`EditBar` 在 Data 檢視的工具列：「+ Row」（列還在載入時停用並說明）、「Read-only · <原因>」或「Editing unavailable: … Retry」、「Staged on another key」、「Outcome unknown: Refresh」、「N changes · M in rows not shown」、不跨重開時的 ⚠（tooltip 寫原因）、Discard…（超過一個變更先確認，⌘Z 可復原）、Commit… ⌘S。右鍵選單依 spec 的順序，做不到的項目停用並以 tooltip 說原因；列還在重讀（stale）或 afterglow 時不能編輯，只剩 Revert 與 ⌘Z，commit 進行中選單是空的（L5、K19）。Filter by this value 用改過的 cell 載入的（server 的）值，新列沒有這個項目。
  - Workspace：table tab 的 `staged_edits`（`StagedEditsFile`，`extra="forbid"`：單值 1 MiB、合計 8 MiB（以 Python `len` 的字元數，key 與原始值連欄名一起算，兩邊同一套，M3）、10,000 個、key 與原始值的巢狀最多 64 層（`MAX_CELL_LEVELS`：254 層的檔案寫得進去卻讀不回來）、`outcome_unknown`）。前端 `savedEdits` 只寫 backend 會收的：超過上限時再試一次不帶標刪列底下隱藏的 UPDATE（K3b：commit 從不送它們，送的 DELETE 照樣保存），放得下就寫，編輯列的 ⚠ 說「Kept across a restart without the edits of rows marked for deletion」（`withoutHidden`，`KEPT_WITHOUT_HIDDEN`；重開後 Restore 帶回的列沒有那些編輯），還是放不下才是「Too large to keep across a restart」；含孤立 surrogate、欄名或 id 不合法是 `unsaveable` 與原因（K5），沒寫的暫存仍在記憶體、照樣可以 commit；兩邊同一份 fixture（`staged_edits_cases.json`；`{"$repeat": n, "item": …}` 是 n 份、`{n}` 換成 1…n，兩邊都這樣讀，超過 10,000 個或 8 MiB 的檔案才寫得小）。`PUT /workspace` 的 body 用 wrap validator（`validated_workspace`）只驗證一次：先只換掉文字裡的孤立 surrogate（模型收它、檔案卻寫不出，K7），模型拒絕時才整個修復（`repaired_workspace`）再驗證一次；修復只改模型拒絕的東西，所以保留的內容與每一行 WARNING 都和以前每次先修復一樣（測試逐案比對）。backend 的修復把模型不收的暫存丟掉、留下 tab（B40，H14，WARNING 只記「dropped the staged edits」）。tab 與 workspace 本身的文字欄位裡的孤立 surrogate 存成 U+FFFD（前端 `workspaceJson`，backend 的修復兜底，K7）；local copy 裡讀不出來的暫存只讓那個 tab 失去暫存（`readableEdits`），還原的 toast（和「已關閉」清單同一則）列出「<tab>: its staged changes could not be read and were dropped」（`restoreLosses`，不帶值）。還原時先放進 store 再還原 tab，資料載入後依 key 綁回；profile 不在了，「已關閉」清單註明捨棄了幾個暫存。undo 歷史與預覽的問題不保存（H22）。
  - 關閉與其他：`CLOSE_GUARD_ORDER.edits = 5`（只在關閉時問「Discard N staged changes to <table>?」，Discard／Cancel，沒有 Commit；先 `flushEditor()` 把打到一半的文字算進去（L8），那時有編輯器的話 Cancel 後焦點回到 grid；沒顯示過的還原 tab 也問；commit 進行中拒絕關閉（toast）；guard 自己不丟暫存，tab 關掉後由 workspace writer 忘掉，H25）。問法是 `closeGuards.ts` 共用的 `useDiscardQuestion`（04c 的 DocumentTab 也改用它，M6）。有暫存的 table tab 顯示圓點；匯出對話框說明暫存不在匯出裡；⌘⇧P（`lib/tabActions.ts` 的 registry）有 Add row、Delete selected rows、Set NULL、Commit changes…、Discard all changes、Undo／Redo staged change（H11；沒有 `row_editing` 的 engine 不列出；前三個只在 grid 顯示時可用）。
- 密碼只在 Keychain；`save_password=false` 的連線，在連線設定或 unlock 對話框輸入的密碼只暫存在記憶體到 app 結束。缺少的密碼（含 SSH 密碼驗證的 SSH 密碼）回 `password_required`，`detail` 指出缺哪一個。
  - Keychain service 由 data dir 決定（`container.keychain_service`）：預設的 `~/Library/Application Support/DbaGuy` 用 `app.dbaguy`；`DBAGUY_DATA_DIR` 指到別處（`scripts/dev.sh` 的 `dev/data`、測試、臨時 sidecar、`measure-startup.sh`）一律用 `app.dbaguy.dev`，開發時存的密碼不會寫進或讀到正式 app 的項目。這個改動之前 dev 存在 `app.dbaguy` 的密碼要重新輸入。
- SSH tunnel：ssh 跑在一個 `/bin/sh` watchdog 底下，watchdog 的 stdin 是只有 backend 持有的 pipe，backend 一結束（含 SIGKILL）ssh 就跟著結束。Tunnel 以完整 SSH 設定與 SSH 密碼指紋為 key；連線測試一律用自己的 tunnel。
```

with:

```markdown
  - 編輯列與選單：`EditBar` 在 Data 檢視的工具列：「+ Row」（列還在載入時停用並說明）、「Read-only · <原因>」或「Editing unavailable: … Retry」、「Staged on another key」、「Outcome unknown: Refresh」、「N changes · M in rows not shown」、不跨重開時的 ⚠（tooltip 寫原因）、Discard…（超過一個變更先確認，⌘Z 可復原）、Commit… ⌘S。右鍵選單依 spec 的順序，做不到的項目停用並以 tooltip 說原因；列還在重讀（stale）或 afterglow 時不能編輯，只剩 Revert 與 ⌘Z，commit 進行中選單是空的（L5、K19）。Filter by this value 用改過的 cell 載入的（server 的）值，新列沒有這個項目。
  - Workspace：table tab 的 `staged_edits`（`StagedEditsFile`，`extra="forbid"`：單值 1 MiB、合計 8 MiB（以 Python `len` 的字元數，key 與原始值連欄名一起算，兩邊同一套，M3）、10,000 個、key 與原始值的巢狀最多 64 層（`MAX_CELL_LEVELS`：254 層的檔案寫得進去卻讀不回來）、`outcome_unknown`）。前端 `savedEdits` 只寫 backend 會收的：超過上限時再試一次不帶標刪列底下隱藏的 UPDATE（K3b：commit 從不送它們，送的 DELETE 照樣保存），放得下就寫，編輯列的 ⚠ 說「Kept across a restart without the edits of rows marked for deletion」（`withoutHidden`，`KEPT_WITHOUT_HIDDEN`；重開後 Restore 帶回的列沒有那些編輯），還是放不下才是「Too large to keep across a restart」；含孤立 surrogate、欄名或 id 不合法是 `unsaveable` 與原因（K5），沒寫的暫存仍在記憶體、照樣可以 commit；兩邊同一份 fixture（`staged_edits_cases.json`；`{"$repeat": n, "item": …}` 是 n 份、`{n}` 換成 1…n，兩邊都這樣讀，超過 10,000 個或 8 MiB 的檔案才寫得小）。`PUT /workspace` 的 body 用 wrap validator（`validated_workspace`）只驗證一次：先只換掉文字裡的孤立 surrogate（模型收它、檔案卻寫不出，K7），模型拒絕時才整個修復（`repaired_workspace`）再驗證一次；修復只改模型拒絕的東西，所以保留的內容與每一行 WARNING 都和以前每次先修復一樣（測試逐案比對）。backend 的修復把模型不收的暫存丟掉、留下 tab（B40，H14，WARNING 只記「dropped the staged edits」）。tab 與 workspace 本身的文字欄位裡的孤立 surrogate 存成 U+FFFD（前端 `workspaceJson`，backend 的修復兜底，K7）；local copy 裡讀不出來的暫存只讓那個 tab 失去暫存（`readableEdits`），還原的 toast（和「已關閉」清單同一則）列出「<tab>: its staged changes could not be read and were dropped」（`restoreLosses`，不帶值）。還原時先放進 store 再還原 tab，資料載入後依 key 綁回；profile 不在了，「已關閉」清單註明捨棄了幾個暫存。undo 歷史與預覽的問題不保存（H22）。
  - 關閉與其他：`CLOSE_GUARD_ORDER.edits = 5`（只在關閉時問「Discard N staged changes to <table>?」，Discard／Cancel，沒有 Commit；先 `flushEditor()` 把打到一半的文字算進去（L8），那時有編輯器的話 Cancel 後焦點回到 grid；沒顯示過的還原 tab 也問；commit 進行中拒絕關閉（toast）；guard 自己不丟暫存，tab 關掉後由 workspace writer 忘掉，H25）。問法是 `closeGuards.ts` 共用的 `useDiscardQuestion`（04c 的 DocumentTab 也改用它，M6）。有暫存的 table tab 顯示圓點；匯出對話框說明暫存不在匯出裡；⌘⇧P（`lib/tabActions.ts` 的 registry）有 Add row、Delete selected rows、Set NULL、Commit changes…、Discard all changes、Undo／Redo staged change（H11；沒有 `row_editing` 的 engine 不列出；前三個只在 grid 顯示時可用）。
- 資料工具（⑥：06a 是 backend 的 dump 與 Run SQL file；Copy 的 backend 與對話框是 06c，dump 與 run 的前端是 06b；[spec](docs/superpowers/specs/2026-10-20-06-data-tools-design.md)，06a 的實作決定在 spec 的〔06a〕註記）：
  - Job 框架（D-06-2）：job 是 job 自己的 session 上的一個 WS 指令（`JobCommand` 的子類：`dump`、`run_file`），worker 一律走 `_run_job`：先結束暫停中的結果、`started`，job 物件（`sessions/jobs/`）只做自己的事，經過 `JobContext`（worker 自己的 `_run_statement`：arm／disarm、取消檢查、`describe=False`；`job_progress` 每 250 ms 最多一次、物件開始與結束時強制送；server 訊息最多 200 則，其餘只計數；History 的紀錄）。事件順序 `started` → `job_progress`×n → [`message`] → [`error`] → `job_outcome` → `session_state` → `finished`，每個 job 恰好一個 `job_outcome`：排隊時被取消、session 先關、開始前就被拒的都是 `not_started`；job 沒分類到的例外：還沒送出任何語句是 `not_started`，送過了由 job 的 `broke()` 決定（run 是 `unknown`，dump 是 `failed`）。`cancelled` 是旗標，不是另一種結果。已在交易中的 session 不跑 job（`transaction_open`）。
  - Job 的 session 與清單（D-06-10）：`JobRegistry`（`GET /data/jobs`、`POST /data/jobs/cancel`、`POST /data/jobs/clear`，最多 200 筆，先丟最舊的已結束的）；reaper 不關有請求在跑或排隊的 session（`busy()`），閒置從最後一個 `finished` 起算；WS attach 時 worker 重送那個 job 最後的 `job_progress` 與 `job_outcome`（`replay_job`），重新載入的視窗接得回去。
  - History（D-06-9）：run 開始時寫一筆 `running`（`begin`），結束時補上（`finish`）；backend 第一次打開 history 時把還是 `running` 的改成 `interrupted`。History 只記 run 的摘要（檔案、模式、大小），不記檔案的語句。Dump 是讀取，不寫 History（同匯出）。
  - Run SQL file（`RunFileJob`，R-06-5～R-06-7）：`StatementStream` 一塊一塊讀（512 KiB，S5；BOM 去掉、行號、byte 位置；不是 UTF-8 的那一行停下，MySQL 附 `--hex-blob` 的提示；單句超過 256 MiB 停），逐句送。不分類（R-06-6）：可寫的連線每句只過 `gate_confirmed`（不分類），唯讀的照 `check()`、只有 RUN 才送；唯讀旗標最多每秒重讀一次（`_Recent`）。全有或全無：dialect 的交易外框（⑤ 的 `transaction_frame`），檔案自己的交易語句在送出前就停（`transaction_in_file`），每句之後 `in_transaction()` 不再為真就停（`transaction_ended`，partial：MySQL 的 DDL、T-SQL 批次裡的 COMMIT），COMMIT 前的任何停止都 rollback（rolled_back），COMMIT 途中斷線是 unknown，COMMIT 回來了交易卻還開著（巢狀 `@@TRANCOUNT`、`completion_type=CHAIN`）rollback 後 unknown。逐句：出錯就停（stopped，帶行號與第幾句）或跳過（錯誤最多記 100 筆，其餘計數）；執行中斷線是 unknown；檔案結束時還開著的交易 rollback 並在結果說明（S14）。從第 N 句開始：前面的照樣切句（DELIMITER 等狀態要對）但不送。`GO n` 送 n 次（`GO 0` 不送）；psql 的 `\restrict`／`\unrestrict` 跳過並說明，其他 psql 指令停（`psql_meta_command`）；`COPY … FROM stdin` 停並提示 `--inserts`。PG 可指定 `search_path`；Couchbase 帶 tab 的 query context，只有逐句。`/data/run-file/inspect` 不連線：大小、前 200 行（最多 64 KiB）、⑥ 的檔頭（engine 不同、DROP first 的警告）、COPY 與不是 UTF-8 的警告；唯讀 profile 回 403。
  - Dump（`DumpJob`，R-06-2～R-06-4、D-06-5～D-06-7）：dump 的 session 先送 engine 的設定與 snapshot（`Dumping.dump_session`：PG `REPEATABLE READ READ ONLY` 與輸出格式；MySQL `CONSISTENT SNAPSHOT`；SQL Server 只在 database 允許時用 `SNAPSHOT`，dev 不允許，所以逐表在 READ COMMITTED 讀，檔頭說明；SQLite 一個讀取交易，rollback journal 時會擋住別人的寫入，預覽警告），catalog 在同一個 snapshot 裡讀（`dump_catalog`；`Ordering`：相依先放，其餘照 catalog 的建立順序），資料是多列 INSERT（`InsertBatcher`：1,000 列、SQL Server 100 列、一句最多 1 MiB；literal 是 ⑤ 的 `CellLiterals`），讀取 1,000 列一塊。檔案經 `atomic_text_file` 寫在 `.part`：失敗、取消、磁碟滿（`Cannot write <path>: Disk full. Nothing was written`）都不留檔，原本的同名檔不變（CR-4）；預覽（`/data/dump/plan`，metadata 連線、不讀定義）在可用空間少於估計大小或 1 GiB 時警告。檔頭（`sql/dump_header.py`）寫 engine、範圍、內容、snapshot、沒有包含的東西與 DROP first。結果是 `completed` 或 `failed`（S2）。`/data/reveal` 只開這個 backend 寫過的檔案。
  - 各 engine 的 dump：PG 依序是 schema、extension（只寫 `CREATE EXTENSION`，不寫它的物件）、型別、序列、不用 row type 的 routine、表（partition 在 parent 之後，不含 FK），資料之後是 `setval` 與 OWNED BY、index、FK、依 pg_depend 排序的 view 與 materialized view、用到 row type 的 routine、trigger；繼承的子表不寫 `INHERITS`（S19）。MySQL：`SHOW CREATE TABLE` 原樣（FK 在裡面，檔頭關掉 FK 檢查），資料之後是 routine、依 VIEW_TABLE_USAGE 排序的 view、trigger、event；stored program 在 DELIMITER 之間，照它自己的 sql_mode（S20），拿掉 DEFINER。SQL Server：schema、型別、從下一個值開始的序列、表的預設值／check／computed 欄用到的 function 在表之前，其他 module 在資料之後依 `sql_expression_dependencies` 排序、各自帶 QUOTED_IDENTIFIER／ANSI_NULLS；id 以 IDENTITY_INSERT 寫，之後 `DBCC CHECKIDENT … RESEED` 回到來源的值（S8）；system-versioned、graph、ledger 表不寫（S9）。SQLite：`sqlite_master` 的原文依建立順序，`sqlite_sequence` 在資料之後，virtual table 與它的內部表不寫。檔頭之後的還原設定：PG `client_encoding`、`standard_conforming_strings = on`、`check_function_bodies = false`；MySQL `SET NAMES utf8mb4`、關掉 FK 檢查、`sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'`、`time_zone = '+00:00'`；SQL Server `QUOTED_IDENTIFIER ON`、`ANSI_NULLS ON`（不寫 `NOCOUNT ON`，還原時每句才回報列數，S21）；SQLite `foreign_keys = OFF`、`defer_foreign_keys = ON`。MySQL 的 dump 要逐句還原（DDL 會隱含 commit，S6）。
  - 欄位（`DataTools.table_columns`，D-06-1）：每個 engine 一個 catalog 查詢，⑤ 的 edit target 也從它讀（行為不變）。`read_as` 是 dump 讀那一欄的 SELECT 運算式：PG 的 money（含 `money[]` 與 domain）讀成 `numeric`（A13）；SQL Server 第 7 位小數的 datetime2／datetimeoffset／time 讀 `CONVERT(…, 126)`（A15：CAST 的文字在位移前有空格，接上 `T` 之後 server 拒絕，241；dialect 的 datetime literal 也把那個空格拿掉，⑤ 一起受惠，S4）；hierarchyid、geography 等 CLR 型別讀成 `varbinary`、以 `0x…` 寫回（A1）。generated、rowversion、sql_variant、period 欄不寫。
  - Classifier（D-06-4）：超過 64 KiB 的 INSERT（MySQL 含 REPLACE）只看第一個字（WRITE），不交給 sqlglot（一句 10 MB 的 INSERT 原本要約 1.1 GB）；有 executable comment 或 `;` 之後還有語句時照舊。SQLite 的連線不快取 prepared statement（`cached_statements=0`：dump 的大 INSERT 每句都不同，預設快取 128 句會讓 RSS 多約 110 MB，S5）。
- 密碼只在 Keychain；`save_password=false` 的連線，在連線設定或 unlock 對話框輸入的密碼只暫存在記憶體到 app 結束。缺少的密碼（含 SSH 密碼驗證的 SSH 密碼）回 `password_required`，`detail` 指出缺哪一個。
  - Keychain service 由 data dir 決定（`container.keychain_service`）：預設的 `~/Library/Application Support/DbaGuy` 用 `app.dbaguy`；`DBAGUY_DATA_DIR` 指到別處（`scripts/dev.sh` 的 `dev/data`、測試、臨時 sidecar、`measure-startup.sh`）一律用 `app.dbaguy.dev`，開發時存的密碼不會寫進或讀到正式 app 的項目。這個改動之前 dev 存在 `app.dbaguy` 的密碼要重新輸入。
- SSH tunnel：ssh 跑在一個 `/bin/sh` watchdog 底下，watchdog 的 stdin 是只有 backend 持有的 pipe，backend 一結束（含 SIGKILL）ssh 就跟著結束。Tunnel 以完整 SSH 設定與 SSH 密碼指紋為 key；連線測試一律用自己的 tunnel。
```

In `CLAUDE.md`, replace:

```markdown
  - SQL Server：pymssql 的 `_mssql`／`_pymssql`（FreeTDS 連在裡面）與它自帶的 OpenSSL（`pymssql/.dylibs/libssl.3.dylib`、`libcrypto.3.dylib`）隨 sidecar 打包；spec 把兩個 extension 與 sqlglot 的 T-SQL 方言按名稱列在 hidden imports（`driver.py` 的 import 與 PyInstaller 的 hook 本來就會收，列名稱是保險）。PyInstaller 把 `@loader_path/.dylibs/…` 改寫成 `@rpath/…`（rpath `@loader_path/..`），`_internal/libssl.3.dylib`、`libcrypto.3.dylib` 是指向 pymssql 副本的 symlink，Python 的 `ssl`（MySQL TLS）也用這一份（和 Homebrew 的同為 OpenSSL 3.6.3、同一組設定）；psycopg_binary 自帶另一份。`build-backend.sh` 在 bundle 裡找不到 `_mssql*.so` 就失敗。sidecar 裡每個 `.so`／`.dylib` 都過 `codesign --verify --strict`，`finalize-app.sh` 不必另外重簽；bundle 的 libcrypto 版本和 venv Python 的 `ssl.OPENSSL_VERSION` 不同時 `build-backend.sh` 失敗。打包後的檢查：凍結的 sidecar 以 TLS 登入 SQL Server、分類 T-SQL、列物件樹、串流含即時訊息的批次，也測過取消。
  - Couchbase：只用標準函式庫（`http.client`、`ssl`、`socket`、`json`、`decimal`），`collect_submodules("dbaguy_backend")` 本來就收得進來，spec 不需要 hidden import，sidecar 沒有新相依（48,896 → 49,020 KB；04b 後 49,100 KB）。打包後的檢查：凍結的 sidecar 以明文、verify-ca、verify-full 連 dev cluster（TLS 走 bundle 的 `ssl`）、分類 SQL++、列 300 個 collection、串流第一頁、取消（server 標 `closed`）、暫停 45 秒後關閉；04b 加上：query context 清單、plan 分析、ADVISE、取樣、SQL++ 補全、Run with profile（兩個 result set）、暫停預讀（明文與 TLS；放得下的結果暫停 70 秒後仍可 Load more，放不下的在暫停後約 53 秒關閉），都不需要 hidden import。
  - Grid 內編輯（05a）：只有新的純 Python 模組（`domain/edits.py`、`sql/literals.py`、`sql/dml.py`、`adapters/editing.py` 與四個 engine 的 `editing.py`、`services/edit_service.py`、`services/edit_plans.py`、`sessions/edit_apply.py`、`api/edits.py`），沒有新相依，spec 不需要 hidden import（49,148 → 49,224 KB，+76 KB）。打包後的檢查：凍結的 sidecar（probe 自己啟動、暫存的 data dir、profile 不存密碼）對 PG、MySQL、SQL Server、SQLite 各做一次 `/edits/target`、`/edits/preview` 與 WS 的 `apply_edits`（改一格含 emoji 與 `%`、新增一列 id 超過 2^53、刪一列；再套用同一個 plan 是 `plan_expired`；`probe` 逐項 assert，不只印出），再各做 A7 與 A6（量測在下面）。
- Backend 意外結束（Tauri）：前端（`src/app/BackendBanner.tsx`）立刻自動重啟，5 分鐘內最多 3 次，再多就顯示手動 Restart 的 banner；editor 內容在前端，重啟後照樣存到新 backend，開著的結果、交易與 session 都沒了（tab 提供 Reconnect）。
- 選單（`src-tauri/src/menu.rs`）：照 Tauri 預設選單，但 Close Window 改成 ⌘⇧W，⌘W 留給前端關 tab（預設的 Close Window 綁 ⌘W，打包版會直接關掉整個視窗）。
- Log：stderr 與 `~/Library/Logs/DbaGuy/backend.log` 都跟著 `DBAGUY_LOG_LEVEL`（預設 INFO）。SQL 文字是 DEBUG，只有明確開 DEBUG 才會寫進檔案（`scripts/dev.sh` 會開）。server 的錯誤訊息會回顯 SQL 常值，所以 `QueryFailed` 在 INFO 只記 method、path、code，訊息在 DEBUG；422 驗證錯誤不回顯請求的 `input`；workspace 裡壞掉的 tab 會被修復（保留它的 buffer）而不是被丟掉。
```

with:

```markdown
  - SQL Server：pymssql 的 `_mssql`／`_pymssql`（FreeTDS 連在裡面）與它自帶的 OpenSSL（`pymssql/.dylibs/libssl.3.dylib`、`libcrypto.3.dylib`）隨 sidecar 打包；spec 把兩個 extension 與 sqlglot 的 T-SQL 方言按名稱列在 hidden imports（`driver.py` 的 import 與 PyInstaller 的 hook 本來就會收，列名稱是保險）。PyInstaller 把 `@loader_path/.dylibs/…` 改寫成 `@rpath/…`（rpath `@loader_path/..`），`_internal/libssl.3.dylib`、`libcrypto.3.dylib` 是指向 pymssql 副本的 symlink，Python 的 `ssl`（MySQL TLS）也用這一份（和 Homebrew 的同為 OpenSSL 3.6.3、同一組設定）；psycopg_binary 自帶另一份。`build-backend.sh` 在 bundle 裡找不到 `_mssql*.so` 就失敗。sidecar 裡每個 `.so`／`.dylib` 都過 `codesign --verify --strict`，`finalize-app.sh` 不必另外重簽；bundle 的 libcrypto 版本和 venv Python 的 `ssl.OPENSSL_VERSION` 不同時 `build-backend.sh` 失敗。打包後的檢查：凍結的 sidecar 以 TLS 登入 SQL Server、分類 T-SQL、列物件樹、串流含即時訊息的批次，也測過取消。
  - Couchbase：只用標準函式庫（`http.client`、`ssl`、`socket`、`json`、`decimal`），`collect_submodules("dbaguy_backend")` 本來就收得進來，spec 不需要 hidden import，sidecar 沒有新相依（48,896 → 49,020 KB；04b 後 49,100 KB）。打包後的檢查：凍結的 sidecar 以明文、verify-ca、verify-full 連 dev cluster（TLS 走 bundle 的 `ssl`）、分類 SQL++、列 300 個 collection、串流第一頁、取消（server 標 `closed`）、暫停 45 秒後關閉；04b 加上：query context 清單、plan 分析、ADVISE、取樣、SQL++ 補全、Run with profile（兩個 result set）、暫停預讀（明文與 TLS；放得下的結果暫停 70 秒後仍可 Load more，放不下的在暫停後約 53 秒關閉），都不需要 hidden import。
  - Grid 內編輯（05a）：只有新的純 Python 模組（`domain/edits.py`、`sql/literals.py`、`sql/dml.py`、`adapters/editing.py` 與四個 engine 的 `editing.py`、`services/edit_service.py`、`services/edit_plans.py`、`sessions/edit_apply.py`、`api/edits.py`），沒有新相依，spec 不需要 hidden import（49,148 → 49,224 KB，+76 KB）。打包後的檢查：凍結的 sidecar（probe 自己啟動、暫存的 data dir、profile 不存密碼）對 PG、MySQL、SQL Server、SQLite 各做一次 `/edits/target`、`/edits/preview` 與 WS 的 `apply_edits`（改一格含 emoji 與 `%`、新增一列 id 超過 2^53、刪一列；再套用同一個 plan 是 `plan_expired`；`probe` 逐項 assert，不只印出），再各做 A7 與 A6（量測在下面）。
  - 資料工具（06a）：只有新的純 Python 模組（`sessions/jobs/`、`sql/{statement_stream,dump_header,bulk_insert,dump_script}.py`、`adapters/{columns,dump}.py` 與四個 engine 的 `columns.py`／`dump.py`、`services/data_service.py`、`api/data.py`），沒有新相依，spec 不需要 hidden import（49,236 → 49,380 KB，+144 KB）。打包後的檢查：凍結的 sidecar（probe 自己啟動、暫存的 data dir、profile 不存密碼）在四個 engine 各把一張 100 萬列的 `zz_06_*` 表 dump 兩次、再以 Run SQL file 還原進 `zz_06_restore_*`（列數相符），Couchbase 逐句跑一個 UPSERT／DELETE 的檔案（`zz_06_` key，`app.orders` 仍是 500）。
- Backend 意外結束（Tauri）：前端（`src/app/BackendBanner.tsx`）立刻自動重啟，5 分鐘內最多 3 次，再多就顯示手動 Restart 的 banner；editor 內容在前端，重啟後照樣存到新 backend，開著的結果、交易與 session 都沒了（tab 提供 Reconnect）。
- 選單（`src-tauri/src/menu.rs`）：照 Tauri 預設選單，但 Close Window 改成 ⌘⇧W，⌘W 留給前端關 tab（預設的 Close Window 綁 ⌘W，打包版會直接關掉整個視窗）。
- Log：stderr 與 `~/Library/Logs/DbaGuy/backend.log` 都跟著 `DBAGUY_LOG_LEVEL`（預設 INFO）。SQL 文字是 DEBUG，只有明確開 DEBUG 才會寫進檔案（`scripts/dev.sh` 會開）。server 的錯誤訊息會回顯 SQL 常值，所以 `QueryFailed` 在 INFO 只記 method、path、code，訊息在 DEBUG；422 驗證錯誤不回顯請求的 `input`；workspace 裡壞掉的 tab 會被修復（保留它的 buffer）而不是被丟掉。
```

In `CLAUDE.md`, replace:

```markdown
  - SQL Server login 開了 `IMPLICIT_TRANSACTIONS`：結果的 Roll back 只幫得了一次，下一次瀏覽的 SELECT 又開了交易（05a 的限制；Reconnect 也一樣）。
  - 衝突標記與 Keep mine 只在重讀之後看得到（rolled_back 而瀏覽原本沒暫停時不自動重讀：按 Refresh）。commit 進行中的關閉保護只在對話框之外碰得到（對話框是 modal：刪除 profile 時）。backend 在 apply 途中重啟：結果不明、History 沒有紀錄（05a M9）。
  - DRY：視窗層的 ⌘S handler 有三份（query、document、table tab，L4），之後合併。
- 打包版量測（Apple Silicon、macOS 26；04a 時量，已加入 Couchbase；`scripts/measure-startup.sh` 跑 6 次，其中 3 次用同一份腳本加上 `footprint`）：冷啟動到第一個 API 請求約 1.1–1.2 秒（6 次裡有 4 次；一次 2.0 秒；每次重新 build 後的第一次啟動約 4 秒，這次 3.9 秒）；各程序 RSS 加總 263–299 MB（上限值，共用頁面重複計算），`footprint` 加總 175–177 MB（backend 72–75 MB）。和 03b（1.2–1.4 秒、269–297 MB、約 173 MB、backend 約 72 MB）相比沒有變化。Sidecar 48,896 → 49,020 KB（Couchbase 只用標準函式庫，多約 0.1 MB；之前的 41 → 49 MB 是 SQL Server 約 7 MB、MySQL 約 0.3 MB；03a 的 0.9–1.1 秒、156 MB 的差距仍沒有在 app 層級驗證，sidecar 同機 A/B 都約 0.47 秒、70–72 MB，不在 backend）。
  - Couchbase（凍結的 sidecar 連 dev cluster）：第一頁（500 列，`SELECT META(d).id, * FROM events AS d`）31–64 ms，剛啟動的 sidecar 的第一次查詢曾量到 103 ms、另一次 42 ms（目標 < 100 ms）；300 個 collection 的樹第一次 39 ms、之後 8–9 ms（根 3 ms）；分頁讀 100 萬列（每列約 230 位元組、每頁 10,000 列、暫停 10 秒再繼續）：RSS 尖峰 99–104 MB，重複跑也不再上升，`footprint` 91 MB 持平（沒有累積）。RSS 的「增加」看基準線：從剛啟動並跑過一次查詢的 96 MB 起算增加 8 MB（目標 < 30 MB）；sidecar 閒置一陣子、macOS 把 RSS 收到 41 MB 之後再跑，增加 22–58 MB（隨基準線），尖峰一樣約 99–100 MB，所以那是一頁 10,000 列的工作集重新載入，不是累積。取消長聚合查詢 3 ms，server 標 `closed`。
  - 04b 時量（凍結的 sidecar 連 dev cluster；`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動）：Sidecar 49,020 → 49,100 KB（+80 KB，沒有新相依）；冷啟動 0.98–1.06 秒（第一次 2.40 秒）；各程序 RSS 加總 256–264 MB，`footprint` 加總 163–167 MB（backend 74–77 MB），和 04a（1.1–1.2 秒、175–177 MB、backend 72–75 MB）沒有退步（WebKit 子程序每次跑都有差）。預讀：凍結的 sidecar（明文與 TLS verify-ca 各一）暫停 3 秒後，server 上已經沒有這個 active request（2 萬列的 `events` 讀到結尾），Load more 照常取得更多列；暫停 70 秒後 Load more 仍給 10,000 列、沒有關閉訊息；放不下的 1 億列串流在暫停後 53 秒關閉並說明原因。分頁讀 100 萬列（每列約 230 位元組、每頁 10,000 列、暫停 10 秒再繼續）：剛啟動並跑過一次查詢的 sidecar（RSS 87–98 MB、`footprint` 77–78 MB）尖峰 112 MB，增加 14–26 MB（目標 < 30 MB），跑完 `footprint` 94–98 MB；04a 同一個量法是尖峰 99–104 MB、增加 8 MB、`footprint` 91 MB；多出的約 8–13 MB 與暫停 10 秒時預讀存的 8 MiB 相符（預期內）。另一個已經跑過 profile、取樣、補全與大結果的 sidecar：第一輪從被 macOS 收到的 44 MB 起算增加 60 MB、尖峰 104 MB，第二輪（基準線 103 MB）增加 1 MB、尖峰 104 MB，`footprint` 99–100 MB；增加量看基準線，尖峰沒有累積。
  - 04c 時量（`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動）：Sidecar 49,100 → 49,148 KB（+48 KB，沒有新相依；`.app` 裡同為 49,148 KB）；JS 主 chunk 1,331.87 → 1,365.09 kB（gzip 429.31 → 441.13 kB，+33.2 kB／+11.8 kB：`@codemirror/lang-json` 與 `@lezer/json`、04c 的前端；Vitest 檔案不在 bundle 裡）；冷啟動 1.03–1.11 秒（第一次 2.77 秒；04b 是 0.98–1.06 秒、第一次 2.40 秒）；各程序 RSS 加總 265–280 MB（04b 256–264），`footprint` 加總 176–184 MB（04b 163–167），backend 74–78 MB（04b 74–77）：backend 與 04b 沒有差別，總和多出的約 10–15 MB 在 WebKit／shell 程序（這些每次跑都有差，04b 也記過），沒有做 A/B。文件（凍結的 sidecar 連 dev cluster，`src-tauri/resources/backend` 與 `.app` 裡的兩份各跑一次）：`u_numbers` 與 seed 的原文相同；`u_big`（5,242,897 bytes）0.06 秒，RSS 66 → 93–94 MB（之後重複讀 +10–12 MB 平穩，從被 macOS 收回的 48 MB 起算 +35 MB）；新增（預覽 → 確認）、CAS 過期的 save（409 `document_conflict`）、直接存、移除（204）、讀回 404、TLS（verify-ca）讀 `u1`，都符合預期，不需要 hidden import。
  - 05a 時量（凍結的 sidecar `src-tauri/resources/backend`：probe 自己啟動、暫存的 data dir、連 dev 的四個 engine，每個 engine 一張 1,000 列與一張 1,000,000 列的 `zz_05a_*` scratch 表；probe 跑 3 次，另一次用 `python -m dbaguy_backend`（非凍結）當 A/B；機器的 load average 約 7–9，使用者桌面的 app 自己占約 350% CPU，所有數字都帶這個雜訊）：每個 engine 的預覽與套用都和 in-process 一樣，key `['id']`、3 句、`committed ['d:3', 'u:1', 'i:4']`、同一個 plan 再套用是 `plan_expired`、存回 `(1, 't11 é 😀 50%')`、`(2, 'b')`、`(9007199254740993, 'new')`。**A7**（1,000 個 UPDATE；預算 PG／MySQL／SQLite ≤ 3 秒、SQL Server ≤ 10 秒）：預覽 PG 25–28 ms、MySQL 26–31 ms、SQL Server 78–96 ms、SQLite 24–26 ms；套用 PG 0.79–1.14 秒、MySQL 0.73–0.99 秒、SQL Server 7.22–9.56 秒、SQLite 0.41–0.43 秒；非凍結的 server 是 1.64／0.76／8.75／0.48 秒，所以 SQL Server 比規劃時量的 5.3–6.3 秒慢，不是打包造成的。都在預算內，SQL Server 的餘裕只有 4–28%（每句三個往返加 Rosetta），合併批次留給之後（spec §12）。最終審查的修正波（M1）讓 plan 的語句不做 describe：in-process 1,000 個 UPDATE 1.54–2.62 秒（同一個 session、同一時間的 A/B 還做 describe 時 3.20–5.99 秒，load average 6–8；沒有重新量打包版）。**A6**（暫停中的 100 萬列瀏覽結果被結束後再套用）：PG 0.02 秒、MySQL 0.38–0.56 秒（KILL 與 drain；非凍結 0.27 秒）、SQL Server 0.22–0.24 秒（attention）、SQLite 0.00–0.02 秒。sidecar 單獨啟動到印出 port 0.48–0.51 秒（5 次，和 03a 的約 0.47 秒一樣；另一次剛 build 完的第一次啟動花了 22 秒，之後沒有再現，原因不明）。打包版（`scripts/measure-startup.sh` 跑 7 次，第一次是 build 後的第一次啟動）：Sidecar 49,148 → 49,224 KB（+76 KB，`.app` 裡同為 49,224 KB）；JS 主 chunk 1,365.09 → 1,366.34 kB（gzip 441.13 → 441.38 kB：05a 沒有新的前端程式，`src/` 只多了產生的 API 型別與 `types.ts` 的型別 export，那 +1.25 kB 是 04c 最後一輪修正之後的差，沒有 A/B）；冷啟動 1.07–1.19 秒（4 次）與 1.55、1.72 秒（第一次 2.72 秒；04c 是 1.03–1.11 秒、第一次 2.77 秒）；各程序 RSS 加總 263–270 MB 與一次 236 MB（04c 265–280），`footprint` 加總 163–172 MB（04c 176–184），backend 76–79 MB（04c 74–78）：backend 比 04c 多 0–2 MB，在 run 之間的差距內；兩次較慢的冷啟動發生在 load average 7–8 時，沒有做 A/B。
  - 05b 時量（`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動；load average 6.4–9.2）：Sidecar 49,224 → 49,236 KB（+12 KB，`.app` 裡同為 49,236 KB：backend 只多了 `StagedEditsFile`、workspace 的修復、`well_formed.py` 與 PG 的 `stable_order`，沒有新相依）；JS 主 chunk 1,366.34 → 1,444.43 kB（gzip 441.38 → 467.80 kB，+78.1 kB／+26.4 kB：05b 的前端，沒有新相依；規劃時估 1,416.8 kB，多的是審查的修正與 `commitDialog.ts`、`tableEditing.ts` 等規劃以外的純函式模組）；冷啟動 0.97–1.25 秒（3 次；第一次 2.92 秒；05a 1.07–1.19 秒、第一次 2.72 秒）；各程序 RSS 加總 261–292 MB（05a 236–270），`footprint` 加總 164–196 MB（05a 163–172；196 那次 WebContent 是 74 MB，另外三次 44–57 MB），backend 76–77 MB（05a 76–79）：backend 沒有變化，總和的差在 WebKit 程序（每次跑都有差，沒有做 A/B）。沒有操作打包版的 UI（使用者的清單）。**Q1**（暫存一格只重畫那一列）：coordinator 在 dev 瀏覽器（Chromium、StrictMode，`zz_05b_items`）以 `window.__gridRowRenders` 量：暫存一格只重畫那一列（2 次，StrictMode 的雙 render），開編輯器與打字 0 列；一欄第一次暫存（`amount`）因為欄寬重新估算，畫面內的 42 列各重畫一次，之後每次 1 列；Task 9 審查以 jsdom 量 100,000 × 30：打字不重畫 owner、暫存一格只產生 1 個新的列物件、閒置的重畫 0.2 ms。**套用**（in-process：`EditService` 預覽、tab 的 `SessionWorker` 套用、先結束暫停中的瀏覽，各 3 次，load average 2.5–4.4；recorder 只在 `finished`／`result_set_end` 時喚醒：tests 的 `Recorder` 每個事件都重掃全部事件，10,000 句時讓量到的 worker 從 6.7 慢到 8.7 秒）：10,000 個 UPDATE（PG `zz_05b_wide`）預覽 0.14–0.17 秒（含讀 target；service 自己記 101–102 ms）、套用 6.37–6.54 秒，committed；1,000 個 UPDATE（`zz_05b_items`，A7 的預算 PG／MySQL／SQLite ≤ 3 秒、SQL Server ≤ 10 秒）：PG 0.67–0.70 秒、MySQL 0.51–0.59 秒、SQL Server 1.52–1.60 秒、SQLite 0.33 秒，預覽 13–40 ms（05a 是 0.79–1.14、0.73–0.99、M1 之後 1.54–2.62、0.41–0.43 秒）；前端：10,000 句的 apply 在 Data view 的主執行緒 0.38 秒（Task 10 審查，jsdom、prod build，snapshot 修正之前 3.77 秒）。**L18**（8 MiB 的暫存每次存檔；兩種形狀：8 個 1,040,000 字元的值／10,000 個 800 字元的變更，ASCII 與中文各一次，中文的 body 約 24 MB）：前端 `savedEdits`（新的 state，不吃快取）ASCII 0.1／6–11 ms、中文 6／14 ms，`workspaceJson` ASCII 7–8／14–15 ms、中文 16／27 ms（Node 24，同一套模組以 rolldown 打包）；backend `PUT /workspace`（TestClient、含寫檔）中位數 ASCII 42／321 ms、中文 115／380 ms（最多 62–455 ms），其中 `json.loads` 8–9／20–24 ms、兩次驗證各 0.4–1.1／72–78 ms，檔案 7.9–23.8／11.0–26.3 MB（最終審查的修正波改成只驗證一次，見 05b 的已知限制）。**L2**：見 05b 的已知限制。

## 進度

```

with:

```markdown
  - SQL Server login 開了 `IMPLICIT_TRANSACTIONS`：結果的 Roll back 只幫得了一次，下一次瀏覽的 SELECT 又開了交易（05a 的限制；Reconnect 也一樣）。
  - 衝突標記與 Keep mine 只在重讀之後看得到（rolled_back 而瀏覽原本沒暫停時不自動重讀：按 Refresh）。commit 進行中的關閉保護只在對話框之外碰得到（對話框是 modal：刪除 profile 時）。backend 在 apply 途中重啟：結果不明、History 沒有紀錄（05a M9）。
  - DRY：視窗層的 ⌘S handler 有三份（query、document、table tab，L4），之後合併。
- 資料工具（06a，backend；前端的限制由 06b 補，Copy 是 06c）：
  - Run SQL file 的速度受 Python 切句限制，本機 2.6–5.2 MB/s（100 萬列、約 231 MB 的 dump：SQLite 45 秒、PG 54 秒、MySQL 67 秒、SQL Server 90 秒）；之後讓切句與 classifier 共用 token，或給 ⑥ 自己寫的 INSERT 一條快速路徑（spec §12）。
  - MySQL：DbaGuy 的 dump 要逐句還原（DDL 隱含 commit，全有或全無會在第一個 CREATE TABLE 之後停下，partial，S6）；`SHOW CREATE TABLE` 的 AUTO_INCREMENT 不在 snapshot 裡，是讀到那一刻的值，可能比 dump 寫的列大，不會重複給號（S10）；dump 只涵蓋一個 database（範圍是 schema，S18）；檔頭固定 `STRICT_TRANS_TABLES`，非嚴格的 `sql_mode` 不適用。
  - SQL Server：database 沒開 `ALLOW_SNAPSHOT_ISOLATION`（dev 與多數 server）時每張表各自在 READ COMMITTED 讀，表之間不一致，檔頭與預覽都說明；SNAPSHOT 從第一次讀取才開始（S13）；system-versioned、graph、ledger 表不寫（S9）；`sql_variant` 欄不寫（只讀得到 bytes，檔頭列出）；SSMS 的腳本帶 `SET NOCOUNT ON` 時 run 回報的列數是 0。
  - PG：繼承的子表 dump 成獨立的表（沒有 `INHERITS`，S19）；extension 的物件不寫（只有 `CREATE EXTENSION`）；range／multirange／base 型別、aggregate、foreign table 不寫，檔頭列出。
  - 逐句的 run：檔案結束時還開著的交易會被 rollback（不替檔案 commit），結果說明（S14）。
  - 當機：dump 只會留下一個隱藏的 `.<名>.<亂數>.part`；History 那一筆是 interrupted；全有或全無的 run 什麼都沒套用（server 在斷線時 rollback）。
- 打包版量測（Apple Silicon、macOS 26；04a 時量，已加入 Couchbase；`scripts/measure-startup.sh` 跑 6 次，其中 3 次用同一份腳本加上 `footprint`）：冷啟動到第一個 API 請求約 1.1–1.2 秒（6 次裡有 4 次；一次 2.0 秒；每次重新 build 後的第一次啟動約 4 秒，這次 3.9 秒）；各程序 RSS 加總 263–299 MB（上限值，共用頁面重複計算），`footprint` 加總 175–177 MB（backend 72–75 MB）。和 03b（1.2–1.4 秒、269–297 MB、約 173 MB、backend 約 72 MB）相比沒有變化。Sidecar 48,896 → 49,020 KB（Couchbase 只用標準函式庫，多約 0.1 MB；之前的 41 → 49 MB 是 SQL Server 約 7 MB、MySQL 約 0.3 MB；03a 的 0.9–1.1 秒、156 MB 的差距仍沒有在 app 層級驗證，sidecar 同機 A/B 都約 0.47 秒、70–72 MB，不在 backend）。
  - Couchbase（凍結的 sidecar 連 dev cluster）：第一頁（500 列，`SELECT META(d).id, * FROM events AS d`）31–64 ms，剛啟動的 sidecar 的第一次查詢曾量到 103 ms、另一次 42 ms（目標 < 100 ms）；300 個 collection 的樹第一次 39 ms、之後 8–9 ms（根 3 ms）；分頁讀 100 萬列（每列約 230 位元組、每頁 10,000 列、暫停 10 秒再繼續）：RSS 尖峰 99–104 MB，重複跑也不再上升，`footprint` 91 MB 持平（沒有累積）。RSS 的「增加」看基準線：從剛啟動並跑過一次查詢的 96 MB 起算增加 8 MB（目標 < 30 MB）；sidecar 閒置一陣子、macOS 把 RSS 收到 41 MB 之後再跑，增加 22–58 MB（隨基準線），尖峰一樣約 99–100 MB，所以那是一頁 10,000 列的工作集重新載入，不是累積。取消長聚合查詢 3 ms，server 標 `closed`。
  - 04b 時量（凍結的 sidecar 連 dev cluster；`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動）：Sidecar 49,020 → 49,100 KB（+80 KB，沒有新相依）；冷啟動 0.98–1.06 秒（第一次 2.40 秒）；各程序 RSS 加總 256–264 MB，`footprint` 加總 163–167 MB（backend 74–77 MB），和 04a（1.1–1.2 秒、175–177 MB、backend 72–75 MB）沒有退步（WebKit 子程序每次跑都有差）。預讀：凍結的 sidecar（明文與 TLS verify-ca 各一）暫停 3 秒後，server 上已經沒有這個 active request（2 萬列的 `events` 讀到結尾），Load more 照常取得更多列；暫停 70 秒後 Load more 仍給 10,000 列、沒有關閉訊息；放不下的 1 億列串流在暫停後 53 秒關閉並說明原因。分頁讀 100 萬列（每列約 230 位元組、每頁 10,000 列、暫停 10 秒再繼續）：剛啟動並跑過一次查詢的 sidecar（RSS 87–98 MB、`footprint` 77–78 MB）尖峰 112 MB，增加 14–26 MB（目標 < 30 MB），跑完 `footprint` 94–98 MB；04a 同一個量法是尖峰 99–104 MB、增加 8 MB、`footprint` 91 MB；多出的約 8–13 MB 與暫停 10 秒時預讀存的 8 MiB 相符（預期內）。另一個已經跑過 profile、取樣、補全與大結果的 sidecar：第一輪從被 macOS 收到的 44 MB 起算增加 60 MB、尖峰 104 MB，第二輪（基準線 103 MB）增加 1 MB、尖峰 104 MB，`footprint` 99–100 MB；增加量看基準線，尖峰沒有累積。
  - 04c 時量（`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動）：Sidecar 49,100 → 49,148 KB（+48 KB，沒有新相依；`.app` 裡同為 49,148 KB）；JS 主 chunk 1,331.87 → 1,365.09 kB（gzip 429.31 → 441.13 kB，+33.2 kB／+11.8 kB：`@codemirror/lang-json` 與 `@lezer/json`、04c 的前端；Vitest 檔案不在 bundle 裡）；冷啟動 1.03–1.11 秒（第一次 2.77 秒；04b 是 0.98–1.06 秒、第一次 2.40 秒）；各程序 RSS 加總 265–280 MB（04b 256–264），`footprint` 加總 176–184 MB（04b 163–167），backend 74–78 MB（04b 74–77）：backend 與 04b 沒有差別，總和多出的約 10–15 MB 在 WebKit／shell 程序（這些每次跑都有差，04b 也記過），沒有做 A/B。文件（凍結的 sidecar 連 dev cluster，`src-tauri/resources/backend` 與 `.app` 裡的兩份各跑一次）：`u_numbers` 與 seed 的原文相同；`u_big`（5,242,897 bytes）0.06 秒，RSS 66 → 93–94 MB（之後重複讀 +10–12 MB 平穩，從被 macOS 收回的 48 MB 起算 +35 MB）；新增（預覽 → 確認）、CAS 過期的 save（409 `document_conflict`）、直接存、移除（204）、讀回 404、TLS（verify-ca）讀 `u1`，都符合預期，不需要 hidden import。
  - 05a 時量（凍結的 sidecar `src-tauri/resources/backend`：probe 自己啟動、暫存的 data dir、連 dev 的四個 engine，每個 engine 一張 1,000 列與一張 1,000,000 列的 `zz_05a_*` scratch 表；probe 跑 3 次，另一次用 `python -m dbaguy_backend`（非凍結）當 A/B；機器的 load average 約 7–9，使用者桌面的 app 自己占約 350% CPU，所有數字都帶這個雜訊）：每個 engine 的預覽與套用都和 in-process 一樣，key `['id']`、3 句、`committed ['d:3', 'u:1', 'i:4']`、同一個 plan 再套用是 `plan_expired`、存回 `(1, 't11 é 😀 50%')`、`(2, 'b')`、`(9007199254740993, 'new')`。**A7**（1,000 個 UPDATE；預算 PG／MySQL／SQLite ≤ 3 秒、SQL Server ≤ 10 秒）：預覽 PG 25–28 ms、MySQL 26–31 ms、SQL Server 78–96 ms、SQLite 24–26 ms；套用 PG 0.79–1.14 秒、MySQL 0.73–0.99 秒、SQL Server 7.22–9.56 秒、SQLite 0.41–0.43 秒；非凍結的 server 是 1.64／0.76／8.75／0.48 秒，所以 SQL Server 比規劃時量的 5.3–6.3 秒慢，不是打包造成的。都在預算內，SQL Server 的餘裕只有 4–28%（每句三個往返加 Rosetta），合併批次留給之後（spec §12）。最終審查的修正波（M1）讓 plan 的語句不做 describe：in-process 1,000 個 UPDATE 1.54–2.62 秒（同一個 session、同一時間的 A/B 還做 describe 時 3.20–5.99 秒，load average 6–8；沒有重新量打包版）。**A6**（暫停中的 100 萬列瀏覽結果被結束後再套用）：PG 0.02 秒、MySQL 0.38–0.56 秒（KILL 與 drain；非凍結 0.27 秒）、SQL Server 0.22–0.24 秒（attention）、SQLite 0.00–0.02 秒。sidecar 單獨啟動到印出 port 0.48–0.51 秒（5 次，和 03a 的約 0.47 秒一樣；另一次剛 build 完的第一次啟動花了 22 秒，之後沒有再現，原因不明）。打包版（`scripts/measure-startup.sh` 跑 7 次，第一次是 build 後的第一次啟動）：Sidecar 49,148 → 49,224 KB（+76 KB，`.app` 裡同為 49,224 KB）；JS 主 chunk 1,365.09 → 1,366.34 kB（gzip 441.13 → 441.38 kB：05a 沒有新的前端程式，`src/` 只多了產生的 API 型別與 `types.ts` 的型別 export，那 +1.25 kB 是 04c 最後一輪修正之後的差，沒有 A/B）；冷啟動 1.07–1.19 秒（4 次）與 1.55、1.72 秒（第一次 2.72 秒；04c 是 1.03–1.11 秒、第一次 2.77 秒）；各程序 RSS 加總 263–270 MB 與一次 236 MB（04c 265–280），`footprint` 加總 163–172 MB（04c 176–184），backend 76–79 MB（04c 74–78）：backend 比 04c 多 0–2 MB，在 run 之間的差距內；兩次較慢的冷啟動發生在 load average 7–8 時，沒有做 A/B。
  - 05b 時量（`scripts/measure-startup.sh` 跑 4 次，第一次是 build 後的第一次啟動；load average 6.4–9.2）：Sidecar 49,224 → 49,236 KB（+12 KB，`.app` 裡同為 49,236 KB：backend 只多了 `StagedEditsFile`、workspace 的修復、`well_formed.py` 與 PG 的 `stable_order`，沒有新相依）；JS 主 chunk 1,366.34 → 1,444.43 kB（gzip 441.38 → 467.80 kB，+78.1 kB／+26.4 kB：05b 的前端，沒有新相依；規劃時估 1,416.8 kB，多的是審查的修正與 `commitDialog.ts`、`tableEditing.ts` 等規劃以外的純函式模組）；冷啟動 0.97–1.25 秒（3 次；第一次 2.92 秒；05a 1.07–1.19 秒、第一次 2.72 秒）；各程序 RSS 加總 261–292 MB（05a 236–270），`footprint` 加總 164–196 MB（05a 163–172；196 那次 WebContent 是 74 MB，另外三次 44–57 MB），backend 76–77 MB（05a 76–79）：backend 沒有變化，總和的差在 WebKit 程序（每次跑都有差，沒有做 A/B）。沒有操作打包版的 UI（使用者的清單）。**Q1**（暫存一格只重畫那一列）：coordinator 在 dev 瀏覽器（Chromium、StrictMode，`zz_05b_items`）以 `window.__gridRowRenders` 量：暫存一格只重畫那一列（2 次，StrictMode 的雙 render），開編輯器與打字 0 列；一欄第一次暫存（`amount`）因為欄寬重新估算，畫面內的 42 列各重畫一次，之後每次 1 列；Task 9 審查以 jsdom 量 100,000 × 30：打字不重畫 owner、暫存一格只產生 1 個新的列物件、閒置的重畫 0.2 ms。**套用**（in-process：`EditService` 預覽、tab 的 `SessionWorker` 套用、先結束暫停中的瀏覽，各 3 次，load average 2.5–4.4；recorder 只在 `finished`／`result_set_end` 時喚醒：tests 的 `Recorder` 每個事件都重掃全部事件，10,000 句時讓量到的 worker 從 6.7 慢到 8.7 秒）：10,000 個 UPDATE（PG `zz_05b_wide`）預覽 0.14–0.17 秒（含讀 target；service 自己記 101–102 ms）、套用 6.37–6.54 秒，committed；1,000 個 UPDATE（`zz_05b_items`，A7 的預算 PG／MySQL／SQLite ≤ 3 秒、SQL Server ≤ 10 秒）：PG 0.67–0.70 秒、MySQL 0.51–0.59 秒、SQL Server 1.52–1.60 秒、SQLite 0.33 秒，預覽 13–40 ms（05a 是 0.79–1.14、0.73–0.99、M1 之後 1.54–2.62、0.41–0.43 秒）；前端：10,000 句的 apply 在 Data view 的主執行緒 0.38 秒（Task 10 審查，jsdom、prod build，snapshot 修正之前 3.77 秒）。**L18**（8 MiB 的暫存每次存檔；兩種形狀：8 個 1,040,000 字元的值／10,000 個 800 字元的變更，ASCII 與中文各一次，中文的 body 約 24 MB）：前端 `savedEdits`（新的 state，不吃快取）ASCII 0.1／6–11 ms、中文 6／14 ms，`workspaceJson` ASCII 7–8／14–15 ms、中文 16／27 ms（Node 24，同一套模組以 rolldown 打包）；backend `PUT /workspace`（TestClient、含寫檔）中位數 ASCII 42／321 ms、中文 115／380 ms（最多 62–455 ms），其中 `json.loads` 8–9／20–24 ms、兩次驗證各 0.4–1.1／72–78 ms，檔案 7.9–23.8／11.0–26.3 MB（最終審查的修正波改成只驗證一次，見 05b 的已知限制）。**L2**：見 05b 的已知限制。
  - 06a 時量（100 萬列、每列約 230 bytes、6 欄的 `zz_06_*` 表；dump 是結構＋資料，約 231–233 MB；run 把那份 dump 還原進 `zz_06_restore_*`（SQLite 是新檔），PG／SQL Server／SQLite 全有或全無、MySQL 逐句；load average 約 3；預算是 spec §7：dump ≤ 30 秒（SQL Server ≤ 60）、run ≤ 2 分鐘（SQL Server ≤ 4）、RSS 增加 ≤ 30 MB）：凍結的 sidecar（probe 自己啟動、暫存的 data dir、經 HTTP 與 WS）dump PG 8.8–9.2 秒、MySQL 14.2–14.4 秒、SQL Server 17.3–18.0 秒、SQLite 8.3–8.4 秒；run PG 54.2 秒（4.3 MB/s）、MySQL 67.3 秒（3.4 MB/s）、SQL Server 89.9 秒（2.6 MB/s）、SQLite 45.2 秒（5.1 MB/s）。RSS 尖峰 dump 53–96 MB、run 68–107 MB，同一份 dump 跑第二次的尖峰不高於第一次（沒有累積）；「增加」看基準線：in-process 同一套（基準線約 88 MB）dump +0–4 MB、run +0–21 MB，凍結的 sidecar 從被 macOS 收回的 49–62 MB 起算最多 +41 MB（PG 的 run，尖峰 104 MB）。Sidecar 49,236 → 49,380 KB（+144 KB），單獨啟動到印出 port 0.46–0.49 秒（5 次，第一次 0.61 秒；05a 0.48–0.51 秒）。

## 進度

```

In `CLAUDE.md`, replace:

```markdown
| ③ | MySQL + MSSQL adapter | 完成（followups：[03a](docs/superpowers/notes/2026-10-18-03a-dialects-and-mysql-followups.md)、[03b](docs/superpowers/notes/2026-10-18-03b-sql-server-followups.md)，含公司 SQL Server 檢查清單與打包版待驗清單） |
| ④ | Couchbase 一級支援 | 完成：04a、04b、04c（SQL++ 方言與 HTTP 引擎；query context、參數、plan 檢視、取樣與補全、暫停預讀；文件：開啟、存檔預覽、新增、刪除；followups：[04a](docs/superpowers/notes/2026-10-19-04a-couchbase-engine-followups.md)、[04b](docs/superpowers/notes/2026-10-19-04b-couchbase-query-workflow-followups.md)、[04c](docs/superpowers/notes/2026-10-19-04c-couchbase-documents-followups.md)，含公司 Couchbase 檢查清單與打包版待驗清單） |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 完成：[spec](docs/superpowers/specs/2026-10-19-05-grid-editing-design.md)、05a backend（edit target、預覽 DML、一次性 plan、在 tab 的 session 套用、結果四分類）、05b frontend（暫存、overlay、編輯器、預覽與 commit、重讀依 key 保留位置、PG 依 key 排序瀏覽、workspace 暫存、關閉保護；引擎圖示單色）；followups：[05a](docs/superpowers/notes/2026-10-20-05a-grid-editing-backend-followups.md)（含公司資料庫檢查清單）、[05b](docs/superpowers/notes/2026-10-20-05b-grid-editing-frontend-followups.md)（含使用者的測試清單：注音與打包版的按鍵） |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 未開始 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
```

with:

```markdown
| ③ | MySQL + MSSQL adapter | 完成（followups：[03a](docs/superpowers/notes/2026-10-18-03a-dialects-and-mysql-followups.md)、[03b](docs/superpowers/notes/2026-10-18-03b-sql-server-followups.md)，含公司 SQL Server 檢查清單與打包版待驗清單） |
| ④ | Couchbase 一級支援 | 完成：04a、04b、04c（SQL++ 方言與 HTTP 引擎；query context、參數、plan 檢視、取樣與補全、暫停預讀；文件：開啟、存檔預覽、新增、刪除；followups：[04a](docs/superpowers/notes/2026-10-19-04a-couchbase-engine-followups.md)、[04b](docs/superpowers/notes/2026-10-19-04b-couchbase-query-workflow-followups.md)、[04c](docs/superpowers/notes/2026-10-19-04c-couchbase-documents-followups.md)，含公司 Couchbase 檢查清單與打包版待驗清單） |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 完成：[spec](docs/superpowers/specs/2026-10-19-05-grid-editing-design.md)、05a backend（edit target、預覽 DML、一次性 plan、在 tab 的 session 套用、結果四分類）、05b frontend（暫存、overlay、編輯器、預覽與 commit、重讀依 key 保留位置、PG 依 key 排序瀏覽、workspace 暫存、關閉保護；引擎圖示單色）；followups：[05a](docs/superpowers/notes/2026-10-20-05a-grid-editing-backend-followups.md)（含公司資料庫檢查清單）、[05b](docs/superpowers/notes/2026-10-20-05b-grid-editing-frontend-followups.md)（含使用者的測試清單：注音與打包版的按鍵） |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 進行中：[spec](docs/superpowers/specs/2026-10-20-06-data-tools-design.md) 完成；06a 完成（backend：job 框架與清單、Run SQL file（五個 engine、兩種模式、別的工具的檔案）、PG／MySQL／SQL Server／SQLite 的原生 dump 與還原往返）；06b（dump 與 run 的前端）、06c（Copy：backend 與對話框）待做 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
```

- [ ] **Step 5: Update the spec.** Record each result next to the text it refines, and do not rewrite history: the §10 rows, §7's numbers, §11's split and the rulings S1–S21 after CR-6. Again, write Step 2's numbers over planning's.

In `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`, replace:

```markdown

- Run 受切句限制（約 2–3 MB/s，R-06-6）：100 萬列的 dump 約 150 MB，切句約 60 秒，再加 server 的時間。
- SQL Server 的載入是瓶頸（每句 100 列 21,847 列／秒；同一批次放多句可到 26,931，§12）。
- 打包：沒有新的 Python 或前端相依（stdlib 與既有 driver），不需要 hidden import；sidecar 預估增加 < 200 KB（新的純 Python 模組）。打包後的檢查：凍結的 sidecar 對四個 engine 各 dump 一次、run 一次、copy 一次（probe 逐項 assert），Couchbase run 一次。

## 8. 痛點對照
```

with:

```markdown

- Run 受切句限制（約 2–3 MB/s，R-06-6）：100 萬列的 dump 約 150 MB，切句約 60 秒，再加 server 的時間。
- SQL Server 的載入是瓶頸（每句 100 列 21,847 列／秒；同一批次放多句可到 26,931，§12）。
- 〔06a 實測〕凍結的 sidecar、100 萬列（約 231–233 MB 的 dump）：dump PG 8.8–9.2 秒、MySQL 14.2–14.4 秒、SQL Server 17.3–18.0 秒、SQLite 8.3–8.4 秒；run PG 54.2 秒、MySQL 67.3 秒（逐句）、SQL Server 89.9 秒、SQLite 45.2 秒（2.6–5.1 MB/s，比估計的 2–3 MB/s 快，dump 也比估計的 150 MB 大）；RSS 尖峰 dump 53–96 MB、run 68–107 MB，重複跑不累積；in-process（基準線約 88 MB）dump 增加 0–4 MB、run 0–21 MB，都在預算內。sidecar +144 KB。Copy 的一列 → 06c。
- 打包：沒有新的 Python 或前端相依（stdlib 與既有 driver），不需要 hidden import；sidecar 預估增加 < 200 KB（新的純 Python 模組）。打包後的檢查：凍結的 sidecar 對四個 engine 各 dump 一次、run 一次、copy 一次（probe 逐項 assert），Couchbase run 一次。

## 8. 痛點對照
```

In `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`, replace:

```markdown

| # | 假設 | 驗證 |
|---|---|---|
| A1 | MySQL GEOMETRY 與 VECTOR 的 bytes 以 `X'…'` 寫回不變（mysqldump `--hex-blob` 的做法）；SQL Server hierarchyid 同 geography（〔實測〕geography 的 `0x…` 可以） | 06a T1 |
| A2 | PG 在 REPEATABLE READ READ ONLY 交易裡：DDL 輔助函式（`pg_get_*`）與 server-side cursor 交錯使用沒有問題，100 萬列的串流記憶體平穩 | 06a T1 |
| A3 | MySQL CONSISTENT SNAPSHOT 交易裡：unbuffered 串流讀完再 `SHOW CREATE` 下一張表（〔實測〕交易裡 `SHOW CREATE TABLE` 可以） | 06a T1 |
| A4 | SQL Server 開了 `ALLOW_SNAPSHOT_ISOLATION` 的 database 用 SNAPSHOT 讀，DDL 讀取不受影響 | 06a T1（scratch database 裡打開） |
| A5 | SQLite rollback journal 模式下，dump 的讀取交易擋住其他連線的寫入（`database is locked`），WAL 不擋 | 06a T1 |
| A6 | `iter_spans` 與既有 `split()` 在全部語料上相同；`StatementStream` 在每個切塊位置相同 | 06a T2 |
| A7 | 四個 engine 的 seed schema 能 dump 並在 `zz_06_restore` 裡重建，逐表 checksum 相同 | 06a T9 |
| A8 | dev 容器裡的 `mysqldump`、`pg_dump --inserts`、`sqlite3 .dump` 的輸出與 SSMS 風格的 GO 腳本能跑（逐句） | 06a T4 |
| A9 | Copy 的 4×4 矩陣上，`zz_06_types` 的每個值依 §3.5.3 相同 | 06a T11 |
| A10 | §7 的預算 | 06a T12 |
| A11 | MySQL 來源在管線滿超過 `net_write_timeout` 時的行為與訊息 | 06a T11 |
| A12 | Tauri 2 的 `onCloseRequested` 在 WKWebView 可用；predefined Quit 換成自訂項目後 ⌘Q 仍是系統的鍵、確認後能正常結束 | 06b 第一個 task |
| A13 | PG 的 `money` 以 `::numeric` 讀（〔實測〕P10）對 `money[]` 與 domain over money 也適用；做不到的寫進已知限制 | 06a T6 |
| A14 | MySQL 的 `DEFINER=` 移除對各種帳號寫法都正確（`user@host`、`'u'@'h'`、`` `u`@`h` ``、`CURRENT_USER`） | 06a T7 |
| A15 | SQL Server 的 `CAST(… AS varchar)` 對 datetime2(7)、datetimeoffset(7)、time(7) 在 British 與 us_english 都寫回相同（〔實測〕us_english 可以） | 06a T8 |
| A16 | `INSERT` 符合列數的讀法（⑤ 的 `matched_rows()`）對多列 INSERT 正確：MySQL 多列 INSERT 的 OK 封包是 `Records: n`（〔實測〕⑤ A1），`affected_rows` 即列數 | 06a T11 |

## 11. 執行計畫拆分

一個 branch `feat/06-data-tools`，兩份 plan 依序執行、各自可合併驗收：

1. **06a：backend（約 12 個 task）**
   - T1 probe：A1–A5。
   - T2 `iter_spans`、`StatementStream`、classifier 的 64 KiB 上限（純函式，A6）。
   - T3 job 框架：指令與事件、`JobContext`、`_run_job`、`JobRegistry`、忙碌的 session 不 reap、History 的 `begin`／`finish`、`gate_confirmed`、能力欄位；`npm run gen:api`。
```

with:

```markdown

| # | 假設 | 驗證 |
|---|---|---|
| A1 | MySQL GEOMETRY 與 VECTOR 的 bytes 以 `X'…'` 寫回不變（mysqldump `--hex-blob` 的做法）；SQL Server hierarchyid 同 geography（〔實測〕geography 的 `0x…` 可以） | 06a T1 → 〔06a Task 4，實測〕MySQL 8.4.11 的 GEOMETRY 以 `X'…'` 寫回相同（SRID 4326 保留）；SQL Server hierarchyid 以 `CAST(… AS varbinary(max))` 讀、`0x…` 寫回相同（`/1/2/3/`）；VECTOR 不在 MySQL 8.4，沒有測（公司是 9.x 時再驗） |
| A2 | PG 在 REPEATABLE READ READ ONLY 交易裡：DDL 輔助函式（`pg_get_*`）與 server-side cursor 交錯使用沒有問題，100 萬列的串流記憶體平穩 | 06a T1 → 〔06a Task 5，實測〕REPEATABLE READ READ ONLY 交易裡，server-side cursor 讀 100 萬列、FETCH 之間穿插 20 次 `pg_get_viewdef`／`pg_get_functiondef`／`pg_get_indexdef`：都正常，交易仍是 repeatable read、read only，client RSS 69 → 70 MB；dump 100 萬列時 sidecar 的 RSS 平穩（Task 10） |
| A3 | MySQL CONSISTENT SNAPSHOT 交易裡：unbuffered 串流讀完再 `SHOW CREATE` 下一張表（〔實測〕交易裡 `SHOW CREATE TABLE` 可以） | 06a T1 → 〔06a Task 6，實測〕CONSISTENT SNAPSHOT 交易裡讀整份 catalog（`SHOW CREATE` 每張表與每個 stored program）之後，別的連線新增的列看不到；`SHOW CREATE TABLE` 的 AUTO_INCREMENT 是讀到那一刻的值，不在 snapshot 裡（S10） |
| A4 | SQL Server 開了 `ALLOW_SNAPSHOT_ISOLATION` 的 database 用 SNAPSHOT 讀，DDL 讀取不受影響 | 06a T1（scratch database 裡打開） → 〔06a Task 7，實測〕scratch database 開了 `ALLOW_SNAPSHOT_ISOLATION`：SNAPSHOT 交易裡 catalog 與 DDL 的讀取正常，之後別的連線新增的列看不到；SNAPSHOT 從第一次讀取開始，不是從 BEGIN（S13）。沒開時 dump 逐表讀（per_table），檔頭說明 |
| A5 | SQLite rollback journal 模式下，dump 的讀取交易擋住其他連線的寫入（`database is locked`），WAL 不擋 | 06a T1 → 〔06a Task 6，實測〕rollback journal（`delete`）時 dump 的讀取交易讓別的連線寫入 `database is locked`；WAL 不擋 |
| A6 | `iter_spans` 與既有 `split()` 在全部語料上相同；`StatementStream` 在每個切塊位置相同 | 06a T2 → 〔06a Task 1，實測〕`split()` 改用 `iter_spans` 後既有的切句測試全過；`StatementStream` 在每一個切塊位置、30 種塊大小都與一次讀完相同 |
| A7 | 四個 engine 的 seed schema 能 dump 並在 `zz_06_restore` 裡重建，逐表 checksum 相同 | 06a T9 → 〔06a Task 9，實測〕四個 engine 的 seed dump → run 進 `zz_06_restore_*`：逐表列數與 checksum（PG `md5(string_agg)`、其他逐列比對）相同，序列、identity、AUTO_INCREMENT、`sqlite_sequence` 接得上，view、routine、trigger、event 都在；MySQL 要逐句（S6）；SQL Server 的 `sql_variant` 欄是 NULL（不寫，檔頭列出） |
| A8 | dev 容器裡的 `mysqldump`、`pg_dump --inserts`、`sqlite3 .dump` 的輸出與 SSMS 風格的 GO 腳本能跑（逐句） | 06a T4 → 〔06a Task 3，實測〕dev 容器的 `pg_dump --inserts`（含 `\restrict`）跑完、COPY 形式在第一個 COPY 停；`mysqldump --hex-blob` 逐句跑完，沒有 `--hex-blob` 時二進位欄是原始 bytes，在那一行以 `file_not_utf8` 停並提示 `--hex-blob`（S11）；`sqlite3 .dump` 逐句跑完、全有或全無在它的 `BEGIN TRANSACTION` 停；SSMS 風格的 CRLF、`GO` 腳本跑完 |
| A9 | Copy 的 4×4 矩陣上，`zz_06_types` 的每個值依 §3.5.3 相同 | 06a T11 → → 06c（S1） |
| A10 | §7 的預算 | 06a T12 → 〔06a Task 10，實測〕見 §7 的〔06a〕 |
| A11 | MySQL 來源在管線滿超過 `net_write_timeout` 時的行為與訊息 | 06a T11 → → 06c（S1） |
| A12 | Tauri 2 的 `onCloseRequested` 在 WKWebView 可用；predefined Quit 換成自訂項目後 ⌘Q 仍是系統的鍵、確認後能正常結束 | 06b 第一個 task |
| A13 | PG 的 `money` 以 `::numeric` 讀（〔實測〕P10）對 `money[]` 與 domain over money 也適用；做不到的寫進已知限制 | 06a T6 → 〔06a Task 4，實測〕`money`、`money[]` 與 domain over money 經 `numeric`／`numeric[]` 讀出、寫回都相同（`-1234567.89`、`{1.50,-2.00,NULL}`、`7.25`） |
| A14 | MySQL 的 `DEFINER=` 移除對各種帳號寫法都正確（`user@host`、`'u'@'h'`、`` `u`@`h` ``、`CURRENT_USER`） | 06a T7 → 〔06a Task 6，實測〕`user@host`、`'u'@'h'`、`` `u`@`h` ``、`CURRENT_USER`、`CURRENT_USER()`、含空白與跳脫的帳號，七種寫法都拿掉 DEFINER、其餘原文不變 |
| A15 | SQL Server 的 `CAST(… AS varchar)` 對 datetime2(7)、datetimeoffset(7)、time(7) 在 British 與 us_english 都寫回相同（〔實測〕us_english 可以） | 06a T8 → 〔06a Task 4，實測〕CAST 的文字在 datetimeoffset 的位移前有空格（`… 12:34:56.1234567 +08:00`），接上 `T` 之後 us_english 與 British 都拒絕（241）；改用 `CONVERT(…, 126)`（`2026-10-05T12:34:56.1234567+08:00`），兩種語系寫回都相同（S4） |
| A16 | `INSERT` 符合列數的讀法（⑤ 的 `matched_rows()`）對多列 INSERT 正確：MySQL 多列 INSERT 的 OK 封包是 `Records: n`（〔實測〕⑤ A1），`affected_rows` 即列數 | 06a T11 → → 06c（S1） |

## 11. 執行計畫拆分

一個 branch `feat/06-data-tools`，兩份 plan 依序執行、各自可合併驗收：

1. **06a：backend（約 12 個 task）**〔06a：規劃成 10 個 task，Copy（下面的 T10–T11）連同 CopyDataDialog 拆成 06c（D-06-14、CR-6；S1）。T1 的 probe 分到用到它們的 task：A1、A13、A15 → Task 4，A2 → Task 5，A3、A5、A14 → Task 6，A4 → Task 7；plan 見 `docs/superpowers/plans/2026-10-20-06a-data-tools-backend.md`〕
   - T1 probe：A1–A5。
   - T2 `iter_spans`、`StatementStream`、classifier 的 64 KiB 上限（純函式，A6）。
   - T3 job 框架：指令與事件、`JobContext`、`_run_job`、`JobRegistry`、忙碌的 session 不 reap、History 的 `begin`／`finish`、`gate_confirmed`、能力欄位；`npm run gen:api`。
```

In `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`, replace:

```markdown
- **CR-4**：Dump 寫檔遇到磁碟滿（ENOSPC）或其他寫入錯誤：`.part` 刪除、原本的同名檔不變，結果寫「Disk full: nothing was written」（或 OS 的原文）並指出寫到哪裡；開始前以 `shutil.disk_usage` 檢查目的地的可用空間，少於估計大小（列數 × 平均列長的上限）或 1 GiB 時在預覽警告（不擋）。使用者的 Mac 磁碟曾經被寫滿、讓 OrbStack 出 I/O error（Q2）。
- **CR-5**：job 進行中的 ⌘Q／關視窗守衛（R-06-15）只涵蓋 ⑥ 的 job；把 quit 守衛擴大到所有 close guard 仍是 02c 的 carry（下面的「之後」）。Tauri 端以 `RunEvent::ExitRequested` 的 `prevent_exit()` 實作，Rust 只負責轉給前端問，決定在前端（CLAUDE.md：Rust 只負責視窗、選單與 sidecar）。
- **CR-6**：06a 依 §11 的 12 個 task 規劃；planner 超過 13 個就照 D-06-14 拆出 06c（Copy），不要擠。

之後（不在 ⑥）：
- Couchbase query 結果 → 關聯式表的 copy（R-06-14；值與欄位走匯出的路徑，`MISSING` → NULL、物件與陣列 → JSON 文字）。
```

with:

```markdown
- **CR-4**：Dump 寫檔遇到磁碟滿（ENOSPC）或其他寫入錯誤：`.part` 刪除、原本的同名檔不變，結果寫「Disk full: nothing was written」（或 OS 的原文）並指出寫到哪裡；開始前以 `shutil.disk_usage` 檢查目的地的可用空間，少於估計大小（列數 × 平均列長的上限）或 1 GiB 時在預覽警告（不擋）。使用者的 Mac 磁碟曾經被寫滿、讓 OrbStack 出 I/O error（Q2）。
- **CR-5**：job 進行中的 ⌘Q／關視窗守衛（R-06-15）只涵蓋 ⑥ 的 job；把 quit 守衛擴大到所有 close guard 仍是 02c 的 carry（下面的「之後」）。Tauri 端以 `RunEvent::ExitRequested` 的 `prevent_exit()` 實作，Rust 只負責轉給前端問，決定在前端（CLAUDE.md：Rust 只負責視窗、選單與 sidecar）。
- **CR-6**：06a 依 §11 的 12 個 task 規劃；planner 超過 13 個就照 D-06-14 拆出 06c（Copy），不要擠。

〔06a〕規劃時的裁決（S1–S21；理由與出處在 06a plan 的「Spec issues found while planning」）：
- S1：Copy 的 backend（§11 的 06a T10–T11、A9、A11、A16、`copy_data`、`OneTimePlanStore`）連同 CopyDataDialog 拆成 06c；06a 交給 06c 的是 `TableColumn`／`TypeFamily`、job 框架、`InsertBatcher` 與 `build_copy_select`。
- S2：dump 的結果是 `completed` 或 `failed`（什麼都沒寫），加上 `not_started`；取消是 `failed` 帶 `cancelled=true`。
- S3：PG 的非有限 float 照既有的 literal 寫成 `'inf'`／`'nan'`（P4：PG 讀得回）；MySQL、SQL Server 的 float 沒有非有限值；只有 SQLite 需要 `9e999`（L3）。
- S4：A15 的 CAST 文字寫不回 datetimeoffset：改用 `CONVERT(…, 126)`；`datetime_literal` 也把位移前的空格拿掉（⑤ 的 grid 編輯輸入 `… +08:00` 時一樣受惠）。
- S5：`STREAM_CHUNK` 是 512 KiB（不是 1 MiB）：切 231 MB、1,000 句的 dump，1 MiB 一塊時切句本身的 RSS 尖峰 +91 MB，512 KiB 時 +28 MB（38.7 對 42.7 秒）；sqlite3 的連線 `cached_statements=0`（dump 的大 INSERT 每句不同，預設 128 句的快取在 200 句 1,000 列的 INSERT 後讓 RSS 多 111 MB，關掉後 4 MB）。
- S6：§1 成功標準的「全有或全無」對 MySQL 做不到（DDL 隱含 commit）：MySQL 的往返以逐句還原，全有或全無在第一個 CREATE TABLE 之後停（partial）也有測試。
- S7：SQL Server 的 function 與 view、synonym 一起在資料之後依 `sql_expression_dependencies` 排序（inline／table-valued 與 schema-bound 的 function 建立時就要表在）；只有表的預設值、check、computed 欄用到的 function 在表之前。
- S8：SQL Server 的 IDENTITY_INSERT 讓 identity 接在寫入的最大 id 之後（附錄 A S2），來源的值更大（刪過列）時資料之後補 `DBCC CHECKIDENT (…, RESEED, n)`，不會重複給號。
- S9：SQL Server 的 system-versioned、graph、ledger 表不寫（照抄成一般表就不是同一種表），檔頭列出。
- S10：MySQL 的 `AUTO_INCREMENT=n` 照 `SHOW CREATE TABLE` 寫：它不在 snapshot 裡，可能比 dump 的列大，不會重複給號。
- S11：沒有 `--hex-blob` 的 mysqldump 檔（二進位欄是原始 bytes）不是 UTF-8：照 §1 的範圍不支援，在那一行停並提示 `--hex-blob`，檢查時也警告。
- S12：加 `POST /data/jobs/clear`（Jobs 的 Clear finished，§3.6）。
- S13：SQL Server 的 SNAPSHOT 從第一次讀取開始（不是 BEGIN）：dump 的第一次讀取就是 catalog。
- S14：逐句的 run 讀完檔案時若交易還開著，rollback 並在結果說明（不替檔案 commit）。
- S15：檔頭與 `-- Data:` 等註解裡的名稱把換行寫成 `\n`（`comment_safe`）：否則名稱的其餘部分會被當成 SQL 執行。
- S16：`transaction_ended` 的括號依 dialect：MySQL 是「MySQL commits around DDL」，其他是「a statement in the file ended it」。
- S17：`DumpObjectKinds` 沒有另外的勾選：MySQL 的 event 跟著 routines，SQL Server 的 synonym 跟著 views。
- S18：MySQL 的 database 就是 schema 節點：範圍 `database` 回 422（「dump its schema node」）。
- S19：PG 繼承（`INHERITS`）的子表 dump 成獨立的表（`table_ddl` 不寫 INHERITS），列各自讀（`FROM ONLY`）。
- S20：MySQL 的 stored program 照它建立時的 sql_mode 寫（`SET SESSION sql_mode = …` 夾在 DELIMITER 裡），之後恢復檔頭的模式。
- S21：SQL Server 的檔頭不寫 `SET NOCOUNT ON`：在它之下 INSERT 不回報列數，還原的結果會說 0 列（其他兩句照 §3.3.3）。

之後（不在 ⑥）：
- Couchbase query 結果 → 關聯式表的 copy（R-06-14；值與欄位走匯出的路徑，`MISSING` → NULL、物件與陣列 → JSON 文字）。
```

- [ ] **Step 6: Report the backend items of the user's final test list** in the task handoff (06b adds the UI items), each with what the dev servers already showed:
  1. Company PostgreSQL: a dump and restore of one real schema with extensions (the header lists their objects as left out), `money[]` or money domains (A13), inheritance (S19), and `\restrict` in the server's own `pg_dump`.
  2. Company MySQL: `SELECT @@sql_mode` and whether routines were made under another mode (S20). DEFINER accounts that do not exist on the target (dropped, A14). `AUTO_INCREMENT` after the restore (S10). A mysqldump file with and without `--hex-blob` (S11).
  3. Company SQL Server: `snapshot_isolation_state` of the database (a dump without it reads each table in its turn, A4). VIEW DEFINITION on the modules (hidden ones are listed as left out). Temporal or ledger tables (left out, S9). An SSMS "Generate Scripts" file run statement by statement; with `SET NOCOUNT ON` it reports 0 rows.
  4. A 1,000,000-row table on each server: dump and run times against §7 (dev: Step 2).

- [ ] **Step 7: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green (no scratch object exists first: `SELECT … LIKE 'zz\_06%'` finds nothing on any server, and `app.orders` is 500).

```bash
git add CLAUDE.md \
  docs/superpowers/specs/2026-10-20-06-data-tools-design.md
git commit -m "docs: 06a in CLAUDE.md and the spec (job framework, Run SQL file, native dump; Copy is 06c)"
```

(`backend/dbaguy-backend.spec` too, only if Step 2 changed it.)

---

## Carries (to 06b, 06c and after ⑥)

| Carry | Where |
|---|---|
| The frontend of dump and Run SQL file (§3.6): DumpDialog, RunFileDialog, the entry points, Jobs, notifications, the close and quit guard (CR-5, A12), CR-3's confirmation (the target's name and color, "I've checked the file and the connection" on a production-colored connection). The contract it builds on: the WS commands `dump` and `run_file` on a session of the job's own (`POST /sessions`, then `DELETE` when the job ends); events `job_progress` and exactly one `job_outcome` per job (outcomes per job as in Global Constraints; `cancelled` a flag; `stopped_at`, `errors`, `error_count`, `objects`, `warnings`); `GET /data/jobs`, `POST /data/jobs/cancel`, `POST /data/jobs/clear`; `POST /data/run-file/inspect` (403 read-only, 404 `file_not_found`), `POST /data/dump/plan`, `POST /data/reveal`; the codes `transaction_in_file`, `transaction_ended`, `psql_meta_command`, `unsupported_copy`, `file_not_utf8`, `statement_too_large`, `file_io`, `dump_io`, `invalid_path`, `not_supported`, `read_only`, `transaction_open`, `connection_lost`, `commit_unconfirmed`. A job whose `job_outcome` never arrived (socket lost, backend restarted) is found again through `GET /data/jobs`, or is gone with the backend (History: interrupted). | 06b |
| Copy data (S1): `type_map`, `OneTimePlanStore` (⑤'s `EditPlanStore` generalized, D-06-8), `/data/transfer/plan`, `TransferSource` / `TransferSink` / `RowPipe`, the `copy_data` capability, A9, A11, A16, the 4×4 matrix, and CopyDataDialog. What 06a gives it: `DataTools.table_columns` with `TypeFamily` and `read_as` on every relational engine (Task 4), the job framework and its registry (Task 2), `InsertBatcher` (with `backslash_escapes` and `max_rows` for a sink) and `build_copy_select` (Task 8). | 06c |
| Run SQL file's speed is the Python split (2.6–5.2 MB/s): shared tokens between the split and the classifier, or a fast path for ⑥'s own INSERTs; SQL Server batches of several INSERTs (26,931 rows/s measured in appendix A). | spec §12 |
| ⑤'s apply could use History's `begin` / `finish` now (05a M9: no record when the backend dies mid-apply). | spec §12 |

## Planning rulings

### Assumptions recorded during execution

The owner task re-verifies each one. The Result column is what it showed while planning (2026-10-20, on the dev containers: PostgreSQL 17.11, MySQL 8.4.11, SQL Server 2022 CU27, SQLite 3.53.4, Couchbase EE 7.6.12). The scratch objects were `zz_06_*`, dropped by exact name, and none was left behind. If a result differs, the owner writes it over the row and applies the Effect; nothing else in the plan needs editing for the outcomes listed.

| # | Assumption | Owner | Result | Effect on later tasks |
|---|---|---|---|---|
| A1 | MySQL GEOMETRY / VECTOR and SQL Server hierarchyid bytes written back as hex stay the same value | Task 4 | Holds: GEOMETRY `X'…'` same (SRID 4326 kept); hierarchyid `0x…` same (`/1/2/3/`); VECTOR does not exist in 8.4 (unchecked) | Holds: as written. Not for a type: that column is `not_copied` with its reason and the header lists it. |
| A2 | PostgreSQL REPEATABLE READ READ ONLY: `pg_get_*` between a cursor's FETCHes, flat memory over 1,000,000 rows | Task 5 | Holds: 20 catalog reads between FETCHes, still repeatable read and read only; client RSS 69 → 70 MB; the dump of 1,000,000 rows flat (Task 10) | Holds: as written. |
| A3 | MySQL CONSISTENT SNAPSHOT: the catalog's `SHOW CREATE` inside it, and the rows read after | Task 6 | Holds: a row inserted after the catalog read is not seen. `AUTO_INCREMENT=n` is read live (S10) | Holds: as written. |
| A4 | SQL Server SNAPSHOT where the database allows it: DDL reads unaffected | Task 7 | Holds in a scratch database with `ALLOW_SNAPSHOT_ISOLATION ON`; the snapshot starts at the first read (S13). The dev databases have it OFF: per table, said | Holds: as written. |
| A5 | SQLite: a dump's read transaction blocks writers in rollback-journal mode, not in WAL | Task 6 | Holds: `delete` → `database is locked`; `wal` → the write goes through | The plan's journal warning (Task 9) as written. |
| A6 | `iter_spans` equals `split()` on the corpus; `StatementStream` equals a whole read at every cut | Task 1 | Holds: every splitter and T-SQL case; 30 chunk sizes; every byte position of five corpora | — |
| A7 | Every engine's seed dumps and restores into `zz_06_restore_*` with equal per-table checksums | Task 9 | Holds on all four: rows, counters, views, routines, triggers, events. MySQL statement by statement (S6); SQL Server's `sql_variant` column NULL (not written, listed) | — |
| A8 | The dev containers' `mysqldump`, `pg_dump --inserts`, `sqlite3 .dump` and an SSMS-style GO script run (statement by statement) | Task 3 | Holds: `pg_dump --inserts` runs (`\restrict` skipped); its COPY form stops at the first COPY; `mysqldump --hex-blob` runs; without `--hex-blob` it stops at the binary line (`file_not_utf8` with the hint, S11); `sqlite3 .dump` runs statement by statement, and all or nothing stops at its `BEGIN TRANSACTION`; the CRLF GO script runs | Holds: as written. |
| A9 | Copy's 4×4 matrix | 06c | — | — |
| A10 | §7's budgets | Task 10 | Within budget, frozen sidecar: dump PG 8.8–9.2 s, MySQL 14.2–14.4 s, SQL Server 17.3–18.0 s, SQLite 8.3–8.4 s (≤ 30 / 60 s); run 54.2 s, 67.3 s, 89.9 s, 45.2 s (≤ 120 / 240 s); RSS peaks 53–107 MB, not growing on a repeat; in-process from a warm 88 MB: dump +0–4 MB, run +0–21 MB (≤ 30 MB) | Recorded in CLAUDE.md and §7. |
| A11 | MySQL as a copy source past `net_write_timeout` | 06c | — | — |
| A12 | Tauri `onCloseRequested` and the custom Quit | 06b | — | — |
| A13 | PostgreSQL `money[]` and a domain over money read through numeric | Task 4 | Holds: `-1234567.89`, `{1.50,-2.00,NULL}`, `7.25` written back equal | Holds: as written. |
| A14 | MySQL `DEFINER=` removed in every account form | Task 6 | Holds: `user@host`, `'u'@'%'`, `` `u`@`%` ``, a doubled back-tick in a name, `CURRENT_USER`, `CURRENT_USER()`, spaces around `@` | Holds: as written. |
| A15 | SQL Server's text of datetime2(7), datetimeoffset(7), time(7) writes back equal under British and us_english | Task 4 | `CAST(… AS varchar)` does not: datetimeoffset's text has a space before the offset, refused with the `T` (241) in both. `CONVERT(…, 126)` writes back equal in both | `read_as` uses style 126; `datetime_literal` drops the space (S4). |
| A16 | Multi-row INSERT counts for a copy | 06c | — | — |

### Rulings made while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| F1 | The `JobRegistry` belongs to `SessionManager` (`AppContainer.jobs` is the same object). Each session's `JobHooks.announce` / `report` are bound to its session id when the manager configures the worker. | The registry needs every job session, and the manager is the only place that makes them. | None. |
| F2 | A worker keeps its job's last `job_progress` and `job_outcome` and sends them again to a client that attaches (`replay_job`). | D-06-10: a reloaded window reattaches to the job session and must see where the job is without waiting for the next tick. | Two events per attach. |
| F3 | A job never runs inside an open transaction (`transaction_open`, `not_started`). | A job session is the job's own; a user's transaction must not be committed or rolled back by a run. | None. |
| F4 | `from_statement` counts statements only: psql lines are not numbered, and the statements before it are read and split but never sent. | §3.4's "前面的語句仍會切句，但不送出": the numbers the outcome shows are the ones to restart from. | None. |
| F5 | psql's `\restrict` / `\unrestrict` are skipped with an info message; any other backslash line stops the run (`psql_meta_command`), even when skipping errors. So does `COPY … FROM stdin`. | §1: pg_dump 17.6+ writes them around every dump; the others change what the file means. | None. |
| F6 | The dump writes the drivers' raw values through `CellLiterals` (as ⑤'s literals take them), not `encode_row`'s JSON cells. | No round trip through JSON-safe text: bigints and bytes stay exact, and the cost per row stays low (X3). | None. |
| F7 | The live tests run in-process (`DataApp`: an `AppContainer` over a temp data dir with in-memory secrets, real adapters and sessions); the routes go through `TestClient`; the whole path over HTTP and WS is the frozen sidecar's (Task 10). | One harness for every engine's flows (⑤ E21). | None. |
| F8 | The read-only flag for a job's gate is re-read at most once a second (`_Recent`). | `ProfileStore.get` reads `profiles.json` each call, and a million-statement run gates each statement; a profile turned read-only mid-run stops it within a second (`test_a_read_only_connection_mid_run_stops_it`). | Up to a second of statements after the switch. |

## Spec issues found while planning

Each is recorded in the spec (Task 10, after CR-6) with its ruling; "Where" is where the plan carries it out.

| # | Issue | Ruling | Where |
|---|---|---|---|
| S1 | §11 / D-06-14 / CR-6: the whole of 06a would be more than 13 tasks at this depth. Dump and run come to 10; Copy (T10–T11) needs at least four more. | Copy's backend and CopyDataDialog move to 06c; 06a is dump, run and the shared foundations. | Header; Carries; Task 10 Step 5 (§11) |
| S2 | R-06-12 names no outcomes for a dump. | `completed` or `failed` (nothing written), plus `not_started`; a cancel is `failed` with `cancelled=true`. | Task 2 (`JobOutcomeKind`), Task 9 |
| S3 | §3.1 lists non-finite floats only for SQLite. | PostgreSQL's existing quoted `'inf'` / `'nan'` reads back (P4); MySQL and SQL Server floats have none; only SQLite maps them (`9e999`, L3). | Task 8 (`non_finite_literal`) |
| S4 | A15 / R-06-11: `CAST(… AS varchar)` of a scale-7 datetimeoffset gives a space before the offset that SQL Server refuses once the date and time are joined by `T` (241), in both languages. ⑤'s grid literal has the same gap for typed `… +08:00` text. | Read with `CONVERT(…, 126)`; `datetime_literal` drops a space before the offset (⑤ benefits). | Task 4 (`read_as`), Task 8 (`_SPACE_BEFORE_OFFSET`) |
| S5 | D-06-3's 1 MiB chunk, and the memory of a run of large INSERTs. | `STREAM_CHUNK` is 512 KiB (splitting a 231 MB dump: +28 MB vs +91 MB); SQLite connections use `cached_statements=0` (+4 MB vs +111 MB after 200 INSERTs of 1,000 rows). | Task 1, Task 3 |
| S6 | §1's success criterion runs every engine's dump all or nothing; MySQL commits around DDL. | MySQL's round trip runs statement by statement; all or nothing is tested to stop as `partial` after the first CREATE TABLE. | Task 9 (`test_mysql_dumps_and_restores_its_seed_statement_by_statement`) |
| S7 | §3.3.3 puts SQL Server's non-schema-bound functions before the tables; an inline or table-valued function needs its tables when it is created. | Functions go with views and synonyms after the data, by `sql_expression_dependencies`; only those a table's default, check or computed column calls go before the tables. | Task 7 |
| S8 | §3.3.3: IDENTITY_INSERT leaves SQL Server's counter after the largest id written, below the source's when rows were deleted. | `DBCC CHECKIDENT (…, RESEED, n)` after the data, where the source's counter is past the largest id. | Task 7 (`_identities`), Task 8 |
| S9 | §3.3.1 does not say what becomes of SQL Server's system-versioned, graph and ledger tables. | Left out and listed (a plain copy would not be one; ⑤ G9's flags). | Task 7 |
| S10 | §3.3.3 keeps MySQL's `AUTO_INCREMENT=n`; it is read live, outside the snapshot. | Keep it (it may be past the dump's rows; it never gives an id twice); a known limitation. | Task 6 (asserted) |
| S11 | §1: "mysqldump 的檔案以逐句模式跑完". Without `--hex-blob`, binary columns are raw bytes, and a non-UTF-8 file is out of scope (§1). | The run stops at that line (`file_not_utf8`) with the `--hex-blob` hint; the inspection warns first. | Task 3 |
| S12 | §3.6's Jobs "Clear finished" has no route in §3.2. | `POST /data/jobs/clear`. | Task 2 |
| S13 | R-06-4 / A4: SQL Server's SNAPSHOT starts at the transaction's first read, not at BEGIN. | The dump's first read is its catalog read, so the snapshot holds from there; recorded. | Task 7 |
| S14 | R-06-7 does not say what happens when a statement-by-statement run ends with the file's transaction still open. | It is rolled back, and the outcome says so (never committed on the file's behalf). | Task 3 (`test_a_transaction_the_file_leaves_open_is_rolled_back_and_said`) |
| S15 | §3.3.2's comment lines hold object names; a name with a line break would end the comment and run the rest as SQL. | `comment_safe` writes `\r` and `\n` as text in every comment. | Task 8 |
| S16 | §4's `transaction_ended` text says "(MySQL commits around DDL)" for every dialect. | MySQL's says that; the others say "(a statement in the file ended it)". | Task 3 |
| S17 | §3.2's `DumpObjectKinds` lists views, sequences, types, routines and triggers; MySQL events and SQL Server synonyms have no switch. | Events follow `routines`; synonyms follow `views`. | Task 5 |
| S18 | §3.3.1: a MySQL database is a schema node; a `database` scope has no meaning there. | 422 `validation_failed` ("A MySQL dump covers one database: dump its schema node"). | Task 6 |
| S19 | §3.3.3: `table_ddl` writes no `INHERITS`. | An inheritance child is dumped as a table of its own, its rows read `FROM ONLY`; a known limitation. | Task 5 |
| S20 | §3.3.3 writes MySQL routines under the header's `sql_mode`; a routine made under another mode (`ANSI_QUOTES`) would not parse. | Each stored program is written under its own `sql_mode`, then the header's comes back. | Task 6 |
| S21 | §3.3.3's SQL Server header has `SET NOCOUNT ON`; under it no INSERT reports its rows, and a run of the dump says 0 rows restored. | Leave it out (the other two settings stay). An SSMS script with it still reports 0 rows: a known limitation. | Task 8 (`RESTORE_SETTINGS`), Task 9 (`ran.outcome.rows == dumped.outcome.rows`) |

## Self-review

- **Spec coverage (§11 item 1 without Copy, S1):**
  - The spec's T1 probes: A1, A13 and A15 → Task 4's probe; A2 → Task 5's; A3, A5 and A14 → Task 6's tests; A4 → Task 7's.
  - T2 (`iter_spans`, `StatementStream`, the classifier's 64 KiB limit, A6) → Task 1.
  - T3 (commands and events, `JobContext`, `_run_job`, `JobRegistry`, the reaper, History `begin`/`finish`, `gate_confirmed`, gen:api) → Task 2. The capability fields are split by job: `run_file` in Task 3, `dump` in Task 9.
  - T4 (`RunFileJob`, `/data/run-file/inspect`, every engine, other tools' files A8) → Task 3.
  - T5 (`table_columns`, `TypeFamily`, ⑤'s targets reading them) → Task 4.
  - T6–T8 (each engine's `dump_catalog`) → Tasks 5–7.
  - T9 (`dump_script`, `InsertBatcher`, `build_copy_select`, `DumpJob`, `/data/dump/plan`, the round trips A7) → Tasks 8–9.
  - T12 (A10, the frozen sidecar, CLAUDE.md and the spec's 〔06a〕 notes) → Task 10.
  - T10–T11 (Copy) → 06c.
  - R-06-12's outcome table → Task 3's table and tests; D-06-11's limits → Task 2; CR-4 → Task 9 (atomic file, ENOSPC, the plan's disk warning).
- **Placeholders:** none. Every changed file is given whole when it is new, or as exact replacements of the previous task's text. The probes and the frozen-sidecar check are given whole. Each assumption gives both outcomes. The measured numbers in Task 10's CLAUDE.md and spec text are planning's, and Task 10 says to write the executor's own over them.
- **Names across tasks:**
  - Task 1 → Task 3: `iter_spans`, `SplitState`, `StatementSpan.meta`, `StatementStream`, `FileStatement`, `StreamError`, `FILE_NOT_UTF8`, `STATEMENT_TOO_LARGE`, `STREAM_CHUNK`, `MAX_STATEMENT_CHARS`, `LARGE_INSERT_CHARS`.
  - Task 2 → Tasks 3 and 9: `JobCommand.job_kind`, `JobProgressEvent`, `JobOutcomeEvent`, `JobInfo`, `JobOutcomeKind`, `Job.run`/`broke`, `JobContext` (`run`, `drain`, `close`, `tick`, `message`, `stop_cancels`, `begin_record`, `outcome`, `progress`, `sent`), `JobHooks`, `JobReport`, `history_status`, `JobRegistry`, `build_job`, `gate_confirmed`, `HistoryService.begin`/`finish`.
  - Task 3 → Tasks 5–10: `RunFileCommand`, `RunFileJob`, `DataService`, `header_lines`/`parse_header`, and `tests/data_support` (`scratch_name`, `dropping`, `Ran`, `JobTab`, `DataApp`).
  - Task 4 → Tasks 5–9: `TableColumn` (`read_as`, `literal`, `value_kind`, `copied`, `identity`), `TypeFamily`, `read_columns`, `columns_of`.
  - Tasks 5–7 → Tasks 8–9: `DumpScope`, `DumpObjectKinds`, `DumpObject`, `DumpTable`, `DumpCatalog`, `DumpSession`, `Ordering`, `Dumping`, `scratch_database`, `_Admin`.
  - Task 8 → Task 9: `InsertBatcher`, `DumpScript`, `build_copy_select`, `comment_safe`, `RESTORE_SETTINGS`.
- **Review Focus:** each of the five lines names its tests and their tasks, and each test is in its task's code.
- **Proportion:** the plan gives the code of every module; the prose is the rules, the interfaces and the expectations.
- **Pre-checked while planning:**
  - Method. The code was written task by task in a scratch worktree at 8089a93, and each task's tree was snapshotted. Later fixes to an earlier task's code went back into that task's tree, so each task carries its final code. The plan was then generated from those trees and replayed as written on a fresh worktree, task by task: every step's text was applied in order, `npm run gen:api` was run where the plan says, and the mysqldump fixture was derived by its command. All 10 resulting trees are byte-for-byte the snapshots.
  - Each task's checks were then run on the replayed tree at that task, alone, against the dev containers:
    - Task 1: `790 passed` (10 s).
    - Task 2: `582 passed` (8 s) and typecheck clean.
    - Task 3: `100 passed` (7 s, five engines, the fixtures, Couchbase back at 500) and typecheck clean.
    - Task 4: `8 passed`. ⑤'s edit targets compared before and after, on one `zz_06_eq_*` table per engine with every column kind: same on all four (key `['id']`, 22 / 20 / 18 / 14 columns). ⑤'s own `zz_05a_*` tests were not run while planning (the rules allowed only `zz_06_*` objects).
    - Task 5: `3 passed`. `table_ddl`'s text compared before and after on six seed tables (partition included): same. The introspection tests, which make `pytest_subpart`, were not run.
    - Task 6: `64 passed`.
    - Task 7: `61 passed` (introspection included); the edit targets compared again after `TABLE_FLAGS`: same.
    - Task 8: `334 passed`.
    - Task 9: `40 passed` (21 s, the four round trips), typecheck and build clean.
  - Afterwards no `zz\_06%` object was left in any server, and `app.orders` held 500. The full backend suite was not run while planning (the planning rules said not to: it writes Couchbase scratch documents and other suites' scratch objects).
  - Probes: Task 4's (A1, A13, A15) and Task 5's (A2) gave the results above.
  - Measurements:
    - In-process, the same jobs on 1,000,000 rows (warm baseline about 88 MB): dump PG 8.9–9.3 s, MySQL 14.0–14.2 s, SQL Server 17.2–17.7 s, SQLite 8.3–8.4 s, RSS +0–4 MB; run PG 53.7 s, MySQL 67.1 s, SQL Server 89.3 s, SQLite 44.7 s, RSS +0–21 MB.
    - The frozen sidecar (PyInstaller from the replayed backend into the scratchpad, 49,380 KB) gave Task 10's expected output: every engine's dump completed with 1,000,000 rows twice and its run restored all 1,000,000; Couchbase ran its two statements; it starts in 0.46–0.49 s.
    - The split's chunk and SQLite's statement cache were measured as S5 says.
    - Not done while planning: `npm run build:app` and `scripts/measure-startup.sh` (Task 10 Step 3).
