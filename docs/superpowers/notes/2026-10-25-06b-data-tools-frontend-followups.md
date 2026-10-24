# Plan 06b (data tools, frontend) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-23-06b-data-tools-frontend.md` (planning rulings V1–V20, spec issues B1–B14; its Appendix A is the user's test list). Spec: `docs/superpowers/specs/2026-10-20-06-data-tools-design.md` (06b decisions are written next to the text they refine, marked 〔06b〕, and 〔06b 修正波〕 for the final fix wave). Executed with subagent-driven development: a pre-flight review (2 High, 6 Medium, 14 Low, all accepted, ruling J1), 9 tasks (1 ⌘Q and window-close guard, 2 backend `resume_at` / excerpt / `from_line`, 3–5 the pure modules and the job runner and store, 6 the Dump dialog, 7 the Run SQL file dialog, 8 Jobs and endings, 9 measurements and docs), every task reviewed with fix rounds and scoped re-reviews (Task 2 took two rounds), the coordinator's manual pass in web mode on PostgreSQL and MySQL, a docs review of Task 9, a final whole-branch review (0 Critical, 2 Important: I1, I2) and one fix wave with a scoped re-review (every item fixed except one fixed in part; the re-review's new Lows are carried below, ruling Z7). Commits c6277c5..dc8ff77 on `feat/06-data-tools` (31 commits, 107 files, +10,022/−392). Full backend suite 6,277 passed at Task 9 (06a closed at 6,193) and 6,292 passed after the wave, with 2 Couchbase connect tests failing for a Darwin 27 reason that 46ecdd6 then fixed (`test_couchbase_http.py` 71 passed; the full suite was not re-run after it); frontend `npm test` 859 passed (46 files), typecheck and build clean. The ledger (`.superpowers/sdd/2026-10-23-06b-data-tools-frontend/progress.md`, git-ignored) has every ruling with its reason and cost.

06c (Copy between connections) is next; ⑥ merges to `main` after it.

## Outcome against the pain points (feature-priorities §2, §3, §8)

- §8 SQL dump / restore (G1, G2): the 06a backend is now reachable. Dump and Run SQL file open from the tree (connection, database, schema, Tables folder, table, view; Couchbase bucket and scope for run), the table tab's ⋯, Scripts' Run on connection… and ⌘⇧P (Dump…, Run SQL file…, Show jobs). A Jobs list at the bottom of the sidebar replaces a blocking modal: a job outlives its dialog, its socket and a page reload. Copy between connections (G3) is 06c.
- Q1 light and fast: no new dependency; JS main chunk 1,447.95 → 1,525.81 kB at Task 9 (gzip 469.12 → 491.93 kB; 1,526.60 / 492.29 after the wave), sidecar +16 KB (49,536 → 49,552 KB). Packaged, four runs: cold start 1.25–1.47 s (06a 1.22–1.28; the first launch after the build 2.54 s at load 14.5), `footprint` sum 169–172 MB (06a 183–189), backend 80–81 MB (78–81): no regression, and the job code only runs when a job does. The Jobs button reads two counts, the list reads ids, each row its own job. The wave's blocked-DROP read is one PostgreSQL query (plan 25 → 26 queries; a 600-table schema 167–194 → 186–251 ms, within noise) and no extra SQL Server round trip; MySQL's fresh statistics cost 7–11 ms either way. Not re-measured: PostgreSQL's Run SQL file memory in the frozen sidecar (06a F1).
- Q2 never lose work: a job is its own session's WS command, sent once (never on a reconnect), closed after its outcome; closing the dialog, a dropped socket and ⌘R keep it (`syncJobs` brings it back after a reload); ⌘Q, the red button and ⌘⇧W ask while a job runs (window brought to the front first, 2 s fallback if the page does not answer); a connection with a running job is not deleted (B13); `from_line` refuses a file edited between runs; "Run again" never repeats a statement known to be applied (B1, M-a, I1; the one exception, a lost connection, says to check the target first, L13). Limits, stated in CLAUDE.md: Dock Quit, logout and shutdown, a page busy for 2 s, a job list slower than 1 s. None of the native behaviour has run (below).
- Q3 trustworthy (the project's reading: honest about what happened; the literal Q3, no login or telemetry, is untouched): every outcome sentence comes from `job_outcome`; `resume_at` is null exactly where earlier statements' effect is unknown, and a stopped run with a null `resume_at` now offers no run-again at all (I1); CR-3's danger button plus checkbox for a red connection, re-asked on every retarget; read-only targets greyed. The wave's blocked-DROP warning was checked against the real server errors (2BP01, 3726, 3729): across 7 PostgreSQL scopes no object of the file's own was named, no warning for a DROP that succeeds, one known gap (row types, below). Plan, header and outcome agree on the number of tables (partitions counted apart), MySQL's row estimate is no longer a day stale, and the user's own Cancel no longer looks like a failure.
- Q4 focus: two dialogs and a list; no schedules or compression; per-object picking only in the Tables folder (V14); Copy is 06c.
- Q5 multi-engine: entries by capability; Couchbase runs files statement by statement with its query context; warnings are per engine's truth (PostgreSQL and SQL Server warn about blocked DROPs because their restore fails there; MySQL and SQLite do not because it works, checked live).
- Q6 macOS: our own Quit item (the predefined one sends `terminate:`, so ⌘Q never reached `ExitRequested`, A12), the shell holds every window close (no `allow-destroy`), system open and save panels, Return and Esc, ⌘W untouched, focus back on the Jobs button after a modal opened over its popover, room under the classic horizontal scrollbar. All untested in the packaged app.
- §3 Must for ⑥: M3 (⌘⇧P: Dump…, Run SQL file…, Show jobs), M9 (a whole database, schema or table as one file, from the tree, the table tab or the palette), M10 (errors as the server wrote them, a label only on DbaGuy's own codes, a cancel shown muted), M11 (CR-3; the DROP-first warning before a run against a production database). Should S8: a notification when a long job ends in the background, a toast otherwise.
- What 06b changed for the worse or for others: N1/N2 moved 140–180 of 13,200 fake MySQL CALL runs from stopped/completed to `unknown` (more honest, a 06a outcome changed); the export dialog now uses the shared path row and its elapsed time reads "2 min 05 s" (V19), and `formatSize` says bytes below 1 KiB (the old "0 KB"/"1 KB" test rows changed on purpose); a job cancelled from another window gets no toast.

## Rulings made during execution

| Ruling | What |
|---|---|
| L1 | The planner's V1–V20 and B1–B14 accepted. The ones later work leans on: V1 jobs live in `jobsStore` outside React (no `useJobSession`); V2 every run-again action opens a prefilled form and nothing reruns from an outcome button; B1 `JobOutcomeEvent.resume_at`; B2 `POST /data/run-file/excerpt`; B3 our own Quit item and a shell-held window close; B13 a connection with a running job is not deleted. |
| J1 | All 22 pre-flight ids accepted as the reviewer wrote them (H1 `resume_at`, H2 the `quitGuard.ts`/`QuitGuard.tsx` case clash → `quitQuestion.ts`, M1–M6, L1–L14), including M4 (the shell holds every close, no `allow-destroy`) and the sixth Review Focus line. Tally: every id is cited in a task report or fix file. |
| K1 | A question in a minimized window is a Q6 defect: a Rust `reveal_window` (unminimize, show, focus; no permission) runs before the question, only when there is a job to ask about. |
| D1, D2 | Task 3: a run with no `resume_at` says "What was applied is not known: check the target." (the count is unreliable exactly then); a real `job_outcome` revives a job lost as `not_listed` (a `session_gone` after `not_listed` stays `not_listed`). Follow-up: "counted as applied; see the notes" only for a stopped run with notes, a completed run always says "N statements applied". |
| Task 2 | N1/N2 extended to the file's rollback path (`_procedure_of`) and the chain branch (M-a); MySQL's non-transactional check runs once at the first failed write outside the file's transaction (M2); `StoppedAt.exact` stays optional in the schema and only `=== false` is inexact (no `from_line`); `from_line` compares only the line where statement N starts (L-b); SQL++ writes sent and failed leave no resume point (M-c). |
| Task 5 | The command goes on the first `session_state`, never on socket open; a Cancel before it ends the job `not_sent`. |
| Task 7 | "Stop at the error" stays enabled in all-or-nothing (only Skip greys); the CR-3 box also resets on a file change; a held preset start is valid only at the run's own place (`placedStart`: connection, database, search path, file). |
| Task 8 | B13 awaits `syncJobs()` before counting; `not_listed` does not block a delete. One rule `endingIsNews`: not news when the job ended cancelled (whoever pressed) or was never sent, except a cancelled run that ended `partial` or `unknown` (the user must look at the target). |
| M1 | One combined manual pass in web mode after Task 8 instead of three; it found F-a…F-f. |
| Z1 | Task 9's docs review ran beside the final review; one fix wave after both. |
| Z2 | I2: the blockers of a `drop_first` file are whatever it drops that something it does not drop depends on (outside the scope, or an in-scope kind left out). PostgreSQL and SQL Server only. |
| Z3 | I1: an outcome `stopped` with a null `resume_at` gets no run-again action and the hint "Check the target, then choose where to start in a new run (Run SQL file… from the tree)." |
| Z4 | F-e: `information_schema_stats_expiry = 0` for MySQL's catalog read (a version comment, so 5.7 skips it), restored with `= DEFAULT` on the shared metadata connection. |
| Z5 | On PostgreSQL the blocked-DROP query names every `pg_depend` deptype n dependent, wider than the ruling's list (views, materialized views, rules, foreign keys): every normal dependency blocks a DROP without CASCADE, so the wider set is the accurate one. The re-review confirmed it against the file's own DROPs. |
| Z6 | SQL++ `EXECUTE <prepared>` counts as a write (its text is not in the file): a failed or cancelled one leaves no resume point (dc8ff77). |
| Z7 | The re-review's residual Lows and the `CODE_LABELS` gap go to this note and CLAUDE.md's known limits, and the wave closes (breaker: one fix wave). None makes a run-again repeat work or hides a failure: the restore still fails and rolls back honestly. |

## Carries (to 06c and later)

| Carry | Where |
|---|---|
| Copy backend: the type map (spec §3.5.2–3.5.3), `OneTimePlanStore`, `POST /data/transfer/plan`, `TransferSource`/`TransferSink`/`RowPipe`, the `copy_data` command; spec §10's A9 (the 4×4 value matrix), A11 (a MySQL source when the pipe is full longer than `net_write_timeout`), A16 (matched rows of a multi-row INSERT). Build on `TableColumn`/`TypeFamily`/`read_as`, the job framework, `InsertBatcher` and `build_copy_select`. | 06c |
| Copy frontend: `CopyDataDialog`, `MappingTable`, `mappingRows.ts` (pure, Vitest); entries: the tree's "Copy data to…" (database, schema, Tables folder, table, view), the table tab's ⋯ ("Copy rows to…", with the filter, V4), a query result's "Copy result to a table…", ⌘⇧P "Copy data to…". No Copy button while the preview has a problem; the replace-rows DELETE shows in the danger colour. | 06c |
| A copy is a third job kind. Touch points in 06b's code: `JobOutcomeEvent["job"]`/`JobCommand` (`jobState.ts`), `jobReport.ts` (kind-specific words, the again-action rules, `CODE_LABELS` for the copy codes of spec §4, the lost-connection hint), `jobsStore`/`jobRunner` (command once, session closed after), `dataDialogs`/`DataToolsHost` (one dialog at a time), `JobViews`, `jobEndings`. A copy has per-table outcomes and nothing like `resume_at`: decide the again-actions deliberately. | 06c |
| B13 counts a connection's running jobs by the job's own profile; a copy also runs a source session on another connection. Check the delete guard covers the source connection too. | 06c |
| Couchbase query result → relational copy is not in ⑥ (CR-1: the JSON and JSONL exports already get the data out). The plan's Appendix A has no "tell me if you need it" sentence for it; it is under "The user's test list" below. | the user's call |
| The ⌘Q guard covers ⑥'s jobs only (CR-5); extending it to every tab close guard (an open transaction, an unsaved script) stays 02c's carry. | later |
| PostgreSQL's Run SQL file memory in the frozen sidecar (06a F1) and the `MallocLargeCache=0` A/B (06a W1): both still need the packaged app and a 1,000,000-row restore (~233 MB); disk allowing. | next packaging |
| The spec's §3.3.1 still says the PostgreSQL blocked-DROP read is "`pg_depend` deptype n 的每個依賴者" without the row-type exception (CLAUDE.md is corrected; the spec and the plan were out of this note's scope). | 06c's docs task |
| Header counts in the singular on PG, SQL Server and SQLite ("1 views (not chosen)"): convert to `adapters/dump.counted` like MySQL (06a F5; changes asserted header texts). | later (small) |
| CLAUDE.md's ⑥ block is very long (the frontend lines alone are dense paragraphs): a later docs pass could move per-engine dump detail into the spec and leave rules and pointers. | later |

## Packaged-app checks

- Built at 4971dd4 (Task 9): `npm run build:app` exit 0 in 68 s, bundle 79.27 MiB, `codesign --verify --deep --strict` passes, sidecar 49,552 KB in both copies. `scripts/measure-startup.sh` ×4 with its own temporary data dir: numbers under Q1 above; every run ended with "backend exited with the app".
- That build predates the fix wave and everything after it (0a407a3, 6de9bfd, d991a57, 46ecdd6, dc8ff77): package and measure again when 06c lands.
- Nobody drove the packaged app's UI. The user's items 1–5 below (⌘Q, window close, the minimized window and `reveal_window`, the file panels and the focus after them, Show in Finder, notifications, Zhuyin) are all untested; `reveal_window` has never run on a live window (whether `set_focus` works during the unminimize animation is unknown). `scripts/dev.sh` (Tauri dev with an external backend) checks the question, the fallbacks and the menu, but not "Quit anyway" stopping a job (the backend is not the shell's child there).
- No browser or app has shown the wave's F-c (room under the excerpt's scrollbar), F-d (muted own Cancel) or the focus return to the Jobs button: they are component code, on the user's list.
- Not covered by the manual pass (web mode, PostgreSQL and MySQL only): the SQL Server per-table warning, read-only and red (CR-3) profiles (none in `dev/data`), Couchbase run contexts, B13 with a running job, a reload in the middle of a job, ⌘Q.

## Company database checklist additions (06a's list stays; its UI items are reachable now)

### PostgreSQL
- Drop objects first on a real schema, as a table or Tables-folder dump with views, foreign keys or a shared trigger function outside it: the red warning names them, names with quotes read right, and turning the option off clears it. The blocker query's version guard (`conparentid` is PG 11+, `tgparentid` 13+) only ran on 17.
- A database with partitioned tables: the plan says "N tables" and "1 partition", the file's header and `-- Dump complete: n rows in m tables` agree with it.
- A table whose row type is used by a function or by another table's column: the restore stops at its DROP with 2BP01 and no warning beforehand (known gap, below).

### MySQL
- The plan's "≈ rows" against the real count on a big table (it sends `SET SESSION information_schema_stats_expiry = 0` through a version comment; only 8.4 was verified, 5.7's comment-only statement was not). A restricted login or a MariaDB-flavoured server may refuse it.

### SQL Server
- The blocked-DROP warning with a schema-bound view (an indexed view) and a foreign key from a table outside the dump; and whether a login without VIEW DEFINITION on those objects sees them in the dependency views at all (if not, the warning stays silent and the restore still fails honestly at 3726 or 3729).
- The per-table snapshot warning and a login whose `user options` turn IMPLICIT_TRANSACTIONS on (both reachable in the dialogs now; neither was exercised).

### Couchbase
- Run SQL file against the company cluster from a bucket and a scope node (the query context in the dialog); a file with an `EXECUTE` of a prepared statement or a JavaScript function that fails: no run-again, a "may be partly applied" note.

### 每台 server
- Dump and run through an SSH tunnel and a slow link: a job whose connection drops ends `connection_lost` with the right words; a PgBouncer in front of PostgreSQL (06a's open question).
- A real production-coloured connection: Run needs the danger button and the checkbox, again after every retarget.

## Incidents

- Usage limit mid-Task 8 (2026-10-23): the implementer of Task 8's fix round 1 was still running. On resume the coordinator checked `git log` and `git status` for its commit or leftover edits; the round landed as e89d729 and a short re-review followed.
- Weekly limit (2026-10-23): both final reviewers (the whole-branch review and Task 9's docs review) stopped early; resumed on 2026-10-25 with the same brief (the user: "請再試一次").
- Mac reboot (2026-10-25): the dev containers were down. `up --wait` exited 0 and all became healthy; `mssql-init` re-seeded (re-runnable); `couchbase-init` stopped on a 503 in the middle of its seed, and the seeds were verified intact afterwards (`app.orders` 500, `app.users` 12, `u_big` 5,242,897 bytes, `u_ttl` expiry, the three `blobs` documents).
- The final reviewer stopped and restarted the PostgreSQL and MySQL containers for about 1.5 minutes (01:15). Later reviewers and the fix wave were told never to start, stop or restart a container.
- Darwin 27 changed the kernel: a connect to a full listen queue is reset at once (ECONNRESET), so `FullListenQueue` could no longer fake an unanswered host and two Couchbase connect tests failed. Fixed in 46ecdd6: `SilentHost` uses 127.0.0.2 (macOS routes 127/8 to the loopback but holds only 127.0.0.1, so a SYN is dropped); it checks that itself first. It would fail on Linux (the project is macOS only).
- The pre-flight reviewer cleaned its scratch with a glob on the scratchpad root: checked afterwards, the other folders were intact. The manual pass emptied the dev workspace once (the old one is backed up in the scratchpad) and `dev/data/workspace.json` was left empty. The manual pass's scratch (`zz_06b_*`) was dropped by exact name; every later check found no `zz_*` object on any engine and Couchbase at `app.orders` 500, `app.users` 12.
- `savedEdits.test.ts`'s 10,000-entry case timed out at 5 s in 2 of 5 and 3 of 9 full runs while the load average was 14–18 (passes alone); it passed in Task 9 and in the wave. Watch it.

## Deferred minor findings and measured candidates

Open after the final re-review (also in CLAUDE.md's known limits):
- PostgreSQL dependents of a table's row type are not named: a function that takes or returns it, or another table's column of that type, depends on the row type (a `pg_type` with `typrelid`), and the blocker query only looks at relations, routines, written types and collations. `DROP TABLE` fails with 2BP01 and the plan says nothing; the restore rolls back honestly.
- "Dump it too" cannot be followed for some named kinds (an operator, an event trigger, "table column", "column default": you would dump the table, never the column). One outside object that reads several dropped objects is named under each of them (a view over five tables appears in five warnings), and the number of warnings is uncapped (each names at most 10).
- Job error codes without a label: `not_found` and `validation_failed` from a dump's `DomainError` (a schema or table gone, a scope the engine does not dump), and Couchbase's `http_<status>` (a proxy page). The job view shows the code and the backend's message.

Smaller:
- The blocked-DROP texts name the dependent as `pg_identify_object` writes it (quoted where needed) and the dropped object as the dump labels it (unquoted).
- The file's own DROP order (06a H7) is not modelled: a dependent the file drops "together" counts as dropped even if that DROP comes later. SQL Server blockers other than foreign keys and schema-bound references (an alias type the file drops, 3732) are not named.
- `/*!80003 SET SESSION … */` would run on MariaDB and fail (unknown variable); MariaDB is not an engine DbaGuy lists. If the TABLES query fails because the connection died, the `finally`'s `= DEFAULT` raises too and masks the first error with an equivalent one.
- An `EXECUTE FUNCTION` or an `EXECUTE <prepared>` at a stop gets two true "may" notes (partly applied, runs several statements).
- `tls_failed`'s label was only unit-tested; the MySQL 5.7 comment-only `SET` was not tried live.
- From Task 8's re-review: no test pins `data-non-modal`; `releaseTabs` awaits `closeSessions`; `TOAST_KINDS.muted` is unreachable.
- Task 9: the backend's test count grew by 84 while Task 2's reports add up to 91 in the files it touched (likely replaced rows; not traced).
- A job's `lostAt` can misdate the rollback after an earlier skipped failure (very narrow, Task 3's review); Skip it and Run the rest carry no `from_line`; `from_line` compares one line (L-b).
- `settledMode` sets state during render (idempotent); a held Return, IME and focus behaviours in WKWebView rest on code review and the user's list (no component tests, the project rule).
- Carried from 06a and unchanged: Run SQL file speed (2.1–5.0 MB/s), T-SQL batch tokenising memory, PG's two FETCHes per table, a dump filter may call side-effect functions, `0x1DROP` reaching SQL Server.

## The user's test list

It is the plan's Appendix A: `docs/superpowers/plans/2026-10-23-06b-data-tools-frontend.md`, "## Appendix A" (14 items, then a list of known limits not to report as bugs). Not copied here. Items 1–5 are packaged-app only and nobody has run them.

This fix wave's items: 8 (the muted own-Cancel box, no run-again for a stop it cannot place, the file-changed note), 9 (focus back on the Jobs button after ⌘⇧P opened over Jobs), 12 (the red Drop-objects-first warning), 13 (partitions and MySQL's row estimate), 14 (bytes below 1 KiB, no "0-tables"). The excerpt's scrollbar (F-c) has no item of its own: with a file that has long lines, look at the last line of the preview and under Show line N (item 8).

One question for the user, from spec CR-1: Couchbase query result → relational table copy is not in ⑥ (the JSON and JSONL exports already get the data out); say if you need it.
