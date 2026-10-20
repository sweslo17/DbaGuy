# Plan 06a (data tools, backend) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-20-06a-data-tools-backend.md` (planning rulings S1–S21, assumptions A1–A16; S22 added at Task 10). Spec: `docs/superpowers/specs/2026-10-20-06-data-tools-design.md` (coordinator review CR-1…CR-6 in §12; Task 10 and the fix wave wrote the 06a decisions next to the text they refine, marked 〔06a〕). Executed with subagent-driven development: a pre-flight review (7 High, 15 Medium, 15 Low, all accepted and carried per task, ruling J1), 10 tasks, every code task reviewed live and adversarially on the dev databases with fix rounds and scoped re-reviews (Task 9 took three rounds), a docs review of Task 10, a final whole-branch review ("ready to close after fixes": 0 Critical, 3 Important, 3 Minor) and one fix wave with a scoped re-review (all six fixed, no new problems, "ready to close"). The pre-flight tally (ruling J1): all 37 finding ids are applied and cited in the task reports. Commits 8748e1e..HEAD on `feat/06-data-tools` (spec 8089a93, plan 9a8775d); full backend suite 6,193 passed. 06a has no UI: 06b builds the dialogs and the Jobs list on this contract, 06c adds Copy between connections, and ⑥ merges to `main` after both.

**Paused here on the user's instruction (2026-10-20): 06b and 06c start only when the user says to continue.**

## Outcome against the pain points (feature-priorities §2, §3, §8)

- §8 SQL dump / restore (G1, G2): native dumps of PostgreSQL, MySQL, SQL Server and SQLite and Run SQL file on all five engines, with no external program. The packaged sidecar dumped and restored 1,000,000 rows on every relational engine, read back equal (aggregates over every column); every seed and each task's hostile database restored twice with `drop_first` and compared through independent inventories plus row hashes, never through the dump's own reader. Copy (G3) is 06c.
- Q1 light and fast: no new dependency (sidecar +300 KB of modules); dumps of 1 M rows in 9–19 s with +3–8 MB RSS in-process; reads sized from the largest row seen (a 24 MiB-row table no longer costs ~1 GB); PostgreSQL's run memory was psycopg's prepared-statement cache (F1: in-process peak +40–45 → +17–19 MB; the packaged numbers in CLAUDE.md predate the fix). Startup and idle memory unchanged within noise.
- Q2 never lose work: a dump is whole or absent (`.part`, fsync, rename; ENOSPC, any OS error, cancel and a bug leave the old file), it stops at a 256 MiB disk floor, it never replaces the SQLite file it reads; every job ends with exactly one `job_outcome`, kept for a reloaded window with its error (F3); History records `running` → `interrupted` across a crash; a job outlives its dialog and its WebSocket.
- Q3 trustworthy: outcomes are true to what the server holds (completed / rolled_back / partial / unknown / stopped / not_started / failed; MySQL implicit commits, SQL Server trigger commits, MyISAM, CHAIN, commit-and-reopen and errors mid-rows all checked live); nothing is ever resent; the header and the plan list what a dump leaves out; a filtered dump says so (F2).
- Q4 focus: dump, plan, Show in Finder and Run SQL file only; no compression, schedules or external binaries.
- Q5 multi-engine: one job framework for five engines; engine knowledge is data in the per-engine modules.
- Q6 macOS: `/data/reveal` (Show in Finder); the dialogs and the ⌘Q guard are 06b.
- §3 M9 whole results as one file; M11 read-only refused before sending and per statement, DROPs only when asked, a dump filter is one read-only condition. Stepped on and fixed in this plan: a SQL Server read-only bypass in query tabs that predates ⑥ (`SELECT 0x1TRUNCATE TABLE t`; the T-SQL number lexing now matches the server).

## Rulings made during execution

The ledger (`.superpowers/sdd/2026-10-20-06a-data-tools-backend/progress.md`, git-ignored) has every ruling with its reason and cost; the ones that change behaviour a later session relies on:

| Ruling | What |
|---|---|
| J1 | Every pre-flight finding accepted as recommended; H6's minimum (everything not written is counted in `not_included`) and its preferred additions. |
| L1 | The planner's spec rulings S1–S21 accepted (Copy moved to 06c, S1). |
| R1–R10, N1–N6 | Run SQL file honesty: implicit commits, trigger commits (3609), MyISAM (1196), session settings re-sent when running from statement N, CHAIN, DO blocks, commit-and-reopen detected by `CURRENT_TRANSACTION_ID`, CALL/EXECUTE inside the file's transaction → unknown. |
| P1–P6, T1–T10, S1–S7 (Tasks 5–7) | What a dump writes or leaves out per engine: objects needing left-out or hidden objects are left out transitively and named; range types, collations, aggregates written (PG); NO_BACKSLASH_ESCAPES programs left out (MySQL); EXECUTE AS 'user' modules left out (SQL Server); batched catalog reads (no N+1). |
| U1 | Only `[]` ends a result; SQL Server's deferred error surfaces on the next fetch (the dump and the CSV export used to lose an error that came after some rows). |
| U2, W2 | `FIRST_FETCH = 1`, later reads sized from the largest row seen; two FETCHes per non-empty PG table accepted. |
| U3, X1, Y3 | A dump filter is one condition: classified as a plain read, through the session's gate, no clause word at depth 0 (per-dialect lists), no unopened `)`. The table-data tab's filter shares the check. |
| Y1 | T-SQL number lexing matches SQL Server (`0x` + hex run, an exponent marker with or without digits, `$` money and pseudocolumns). |
| W1 | `MallocLargeCache` not changed (process-wide; needs an A/B on the packaged app). |
| Z1 | Task 10's docs review ran beside the final review; its fixes joined the one fix wave. |
| F1–F6 | Fix wave: `prepare_threshold=None` on every PG connection; filtered dumps labelled (header, inspect, plan, outcome); `JobOutcomeEvent.error`; SQLite FK hint at COMMIT; MySQL unreadable routines named; docs. |

## Carries

| Carry | Where |
|---|---|
| The frontend of dump and Run SQL file (spec §3.6): DumpDialog, RunFileDialog (CR-3: a red connection's Run needs a danger button plus a checkbox), the Jobs sidebar, entry points. Map every code CLAUDE.md's ⑥ block lists (`implicit_commit`, `procedure_call`, `file_truncated`, `file_not_utf8`, `statement_too_large`, `transaction_in_file`, `transaction_ended`, `psql_meta_command`, `unsupported_copy`, `file_io`, `dump_io`, `invalid_path`, `invalid_filter`, `invalid_text`, `not_supported`, `read_only`, `transaction_open`, `connection_lost`, `commit_unconfirmed`, …). | 06b |
| A job's session is never reused: open one per job, close it after (`DELETE /sessions/{id}`, D-06-10) — a dump leaves PG `search_path = ''`, `TimeZone`, `row_security` and MySQL `USE`/`sql_mode`/`time_zone` on it. | 06b |
| A client that attaches after the job ended gets the last `job_progress`, the `job_outcome` (now with `error`) and a `session_state`, but no `finished`: treat the outcome as the end. | 06b |
| Show before a dump starts: the plan's warnings (free space, SQLite journal mode, SQL Server per-table reads, MySQL non-InnoDB tables, FK cycles and self-references in data-only dumps, a filter combined with drop_first). | 06b |
| Show a filtered file's inspect warning prominently before Run (F2: restoring it replaces the table with the matched rows). | 06b |
| SQLite busy at COMMIT: reuse ⑤'s `SQLITE_BUSY_NOTE` from `job_outcome.error.code` (the backend can't import the frontend constant). | 06b |
| The table-data tab's filter now refuses `… ORDER BY x` / `… LIMIT 5` (one condition only): say so in the filter UI (the tab has its own sort). | 06b |
| The ⌘Q / close-window guard for running jobs (CR-5, Tauri `RunEvent::ExitRequested`; 02c's carry: quit skips every close guard). | 06b |
| `DumpPlanRequest` has `drop_first` now (F2): send the dialog's fields to both the plan and the dump. | 06b |
| Copy between connections: type map, `OneTimePlanStore`, `/data/transfer/plan`, the source → sink pipe (A9/A11/A16), CopyDataDialog; build on `TableColumn`/`TypeFamily`/`read_as`, the job framework, `InsertBatcher`/`build_copy_select`. Couchbase → relational stays deferred (CR-1). | 06c |
| Header counts in the singular on PG, SQL Server and SQLite ("1 views (not chosen)"): convert them to `adapters/dump.counted` like MySQL (F5; changes asserted header texts). | later (small) |

## Packaged-app checks

- The frozen sidecar (in `src-tauri/resources/backend`) dumped and ran 1,000,000 rows on PostgreSQL, MySQL, SQL Server and SQLite and a two-statement file on Couchbase, with asserted outcomes and equal read-backs; the `.app` built and `codesign --verify --deep --strict` passed; cold start 1.22–1.28 s (first launch after the build 3.52 s), footprint 183–189 MB, backend 78–81 MB (load average 4–13).
- Not re-measured frozen after the fix wave: PostgreSQL's Run SQL file memory (F1; in-process A/B only). Re-measure at the next packaging.
- For the user (no UI yet, so after 06b): Show in Finder from the packaged app; a large dump and run with Activity Monitor open; the first launch after an update.

## Company database checklist for 06a (06b adds the UI items)

### PostgreSQL
- Dump and restore one real schema with extensions (their objects are listed as left out), `money[]` or a money domain, inheritance (children come back as tables, S19), and the server's own `pg_dump` output with `\restrict` (skipped with a message).
- A login without BYPASSRLS on an RLS table (the plan warns; the dump fails rather than leaving rows out); PG 12/13 guards (`tgparentid`, multirange) never ran on dev (PG 17).
- Behind PgBouncer in transaction mode: F1 means DbaGuy never creates `_pg3_*` prepared statements (not verified); whether `describe()`'s unnamed statement (two separate requests) survives transaction pooling was not checked (predates ⑥).
- Profile options named like psycopg's own `connect()` arguments (`autocommit`, `prepare_threshold`, …) are reserved and dropped (F1): check that no company profile relied on one.

### MySQL
- `SELECT @@sql_mode`; routines made under another mode (ANSI_QUOTES restored; NO_BACKSLASH_ESCAPES programs left out and named); DEFINER accounts missing on the target; `AUTO_INCREMENT` after a restore; mysqldump files with and without `--hex-blob`.
- MySQL 5.7's 4 MiB `max_allowed_packet` (dev has 64 MiB): the size warning; DBA-owned routines the login can't read (named in warnings now, F5); VECTOR on 9.x. DbaGuy's MySQL dumps restore statement by statement only (S22).

### SQL Server
- `snapshot_isolation_state` (dev: off → each table read in its turn, said in the header); VIEW DEFINITION on modules; temporal or ledger tables (left out, S9); an SSMS "Generate Scripts" file statement by statement (UTF-16 refused with "save it as UTF-8").
- SQL Server 2016/2017 (`CURRENT_TRANSACTION_ID`, graph tables; dev is 2022); a login whose `user options` turn IMPLICIT_TRANSACTIONS on (L8: fake connection only); RLS/UNMASK logins (dump only what they see, warned); a CLR-type column (no assemblies on dev).

### 每台 server
- A 1,000,000-row table: dump and run times against §7 (dev, frozen: dump 9.8–19.1 s, run 46.5–108.5 s).

## Incidents

- Live tests on another engine's DB: three times an agent's filtered run let a few tests of another engine through (Tasks 5, 6, and a mutation run); no residue (scratch dropped by exact name). Rule added: read a multi-engine file's selection before running it.
- A reviewer's probe hung on `pg_advisory_lock` held by its own earlier job session (Task 9 round 2); killing it left one `zz_06_m2_*` scratch database, dropped by exact name.
- ~321 stale `zz_06_*` MySQL grants from `_Admin.drop` never revoking (Task 6): listed and revoked by exact name; `drop` now revokes.
- Two orphaned `tail -f` monitors from earlier work stopped by exact PID. OrbStack reported an I/O error once (the disk was nearly full): every engine and seed count was verified afterwards; scratch is now capped at 1 GB.
- The dev MySQL binary log grew to 2.6 GB and Docker volumes to 7.2 GB from the 1 M-row probes (free disk ~20 GiB); not purged (dev server state: the user's call).

## Deferred minor findings and measured candidates

- `MallocLargeCache=0` for the sidecar (W1): freed large blocks stay in the process (a 24 MiB-row dump: +460 MB RSS on SQLite vs +100 MB with it off); needs an A/B on the packaged app's footprint and speed.
- Batches of 1 MiB statements on PG still peak +42–67 MB briefly after F1 (not investigated).
- Run SQL file speed 2.1–5.0 MB/s (the split's limit): shared tokens between the splitter and the classifier, a fast path for ⑥'s own INSERTs, SQL Server batches of several INSERTs (a 1 M-row run is 10,003 round trips).
- T-SQL batch tokenising: a 10 MB INSERT in a query tab ≈ 460 MB; a lazy `statements()`.
- PG's two FETCHes per non-empty table (W2) at high RTT; the PG grid shows rows only up to the last chunk boundary before a mid-stream error (pre-existing).
- A dump filter may call side-effect functions a read-only transaction does not stop (`pg_terminate_backend`, advisory locks, `GET_LOCK`) — as in a read-only query tab; session locks end with the job's session.
- A hex-letter-initial keyword glued to a number (`0x1DROP`) reaches SQL Server and fails with its syntax error 102 (nothing runs) instead of `invalid_filter`.
- A table whose name contains the header phrase " that match a filter: " shows cut off in the inspect warning (F2).
- ⑤ M9: the grid apply could use History's `begin`/`finish` (written before COMMIT).

## Appendix: the 06b contract

The contract is the code and CLAUDE.md's ⑥ block (kept accurate by Task 10's docs review and the fix wave), not a copy here:
- Routes (`api/data.py`): `POST /data/run-file/inspect`, `POST /data/dump/plan`, `POST /data/reveal` (204 / 404), `GET /data/jobs`, `POST /data/jobs/cancel`, `POST /data/jobs/clear`.
- WS commands (`domain/execution.py`): `RunFileCommand`, `DumpCommand`; events `job_progress`, `job_outcome` (`domain/data_jobs.py`: `JobProgressEvent`, `JobOutcomeEvent` with `error`, `errors`, `stopped_at`, `warnings`, `objects`), then `session_state` and `finished` on a live run.
- Capabilities (`AdapterCapabilities`, reported in `EngineInfo`): `run_file` (all five engines) and `dump` (PostgreSQL, MySQL, SQL Server, SQLite).
- Generated types: `npm run gen:api` (`src/lib/api/schema.d.ts`); `src/lib/api/types.ts` exports `DumpCommand`, `DumpScope`, `DumpObjectKinds`, `DumpPlan`, `DumpPlanObject` and the run-file types.
