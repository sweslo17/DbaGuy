# Plan 03b (SQL Server) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-18-03b-sql-server.md` (planning rulings at its end). Spec: `docs/superpowers/specs/2026-10-17-03-mysql-mssql-design.md` (SQL Server sections, errata, §8). Executed with subagent-driven development: a pre-flight scan, 10 tasks each reviewed (Task 8 through three fix rounds), every review also checked against the researched pain points; a final Opus whole-branch review, one fix wave, a packaged-binary verification that found one more critical issue (F6), its fix and a second packaged verification. Commits 6471422..6925cd2 on `feat/03-mysql-mssql`.

## Outcome against the pain points (feature-priorities §2, §3)

- Q1 light and fast: 100k rows stream in 0.27–0.32 s, a page ≤ 3 ms (budget 2.0 s / 25 ms). Sidecar 41.9 → 48.9 MB on disk (pymssql + its OpenSSL); sidecar cold start (0.47 s) and footprint unchanged against 03a in a same-machine A/B. Two extra round trips per statement remain (carry).
- Q2 never lose work / never hang: no cancel ever puts two threads inside FreeTDS (F6); a Cancel never silently rolls back a transaction (warn first, a second Cancel ≥ 1 s or closing the tab forces); a double-click never forces. Packaged verification: 1,200-iteration storms with 6 sessions + metadata + a lock holder, 0 hangs, the sidecar alive throughout, 0 new crash reports; ≈ 15,000 more hammer iterations in source runs. If the sidecar ever exits, the app restarts it automatically (≤ 3 times in 5 min), editor text kept.
- Q3 trustworthy: server text with error number and line; exact decimals/money/bigint; FOR JSON/XML joined; `real` with float32 digits; honest drop/warning texts; read-only limits stated in the dialog.
- Q5 / §8 one tool: SQL Server runs every flow the other engines have (tree incl. synonyms, DDL, table data, filters, query with GO n, live messages, manual commit, completion, copy/export, read-only, SSH, TLS).
- M11 safety: the T-SQL statement cutter classifies every statement in a batch (with or without `;`), fail-closed floor words, locking hints count as writes; ≈ 660k generated batches across reviews with 0 read-only escapes accepted by the server.

## Rulings made during execution (details in the plan workspace ledger, summarised here)

- R-03b-P1…P11: pre-flight fixes (seed service stays up, fail-closed classification, describe via the DMF, phase-locked cancel …).
- R-03b-T3a–d: ANSI_DEFAULTS off the read-only list; UPDLOCK/XLOCK/HOLDLOCK/SERIALIZABLE/REPEATABLEREAD are WRITE (SERIALIZABLE exempt only after ISOLATION LEVEL); Service Broker words count only in their statement forms; columns named serializable/repeatableread are blocked on read-only (accepted).
- R-03b-T5, T6a–h, T7a–d: seed additions; URL import never echoes values and refuses mixed separators/ambiguous forms; limited accounts get no DDL rather than wrong DDL; hidden FK targets shown as `?`.
- R-03b-T8a–i: the cancel design — process-wide wait callback (never cancels inside it) for a same-thread cancel while waiting for the first response; cancel from the message handler; flag between rows; otherwise the 1 s escalation (auto-commit: drop with an honest text; transaction possible: warn, then a second Cancel ≥ 1 s or tab close drops); @@ROWCOUNT/@@ERROR across GO batches and the 7th fractional digit accepted as limits; NUL refused in the worker for every engine.
- R-03b-F1–F6 (final wave): warn before any forced drop; repeated Cancel during a huge value follows the same rule and the export dialog can reach it; metadata reads past uncommitted DDL (READ UNCOMMITTED, LOCK_TIMEOUT 5000, XACT_ABORT ON on metadata connections only); measure-startup touches only its own instance; `driver.GuardedConnection` counts every pymssql entry and never frees a connection under a live call (leak instead); **F6: no thread but the worker ever enters FreeTDS** — the packaged verification showed a cross-thread `dbcancel` during a row read corrupting the worker's buffers.

## Carries

- Performance: fold the state read into the describe batch (2 extra round trips per statement; ~+60 ms over a 30 ms tunnel).
- Cancel cost of F6: a read blocked mid-result on a row lock, a wait between result sets and a NOCOUNT OFF loop end by escalation (auto-commit: connection dropped after ~1 s; transaction: warning first) instead of a clean cancel. A future driver with a send-only attention would remove it.
- A locked object's detail holds that database's metadata worker for up to 5 s (other tree calls queue behind it).
- Not supported (out of scope unless the company server needs it): strict (TDS 8.0) encryption, named instances, Windows/AD logins.
- A SQL Server tab does not follow `USE` (the picker and completion stay on the picked database).
- OpenSSL: Python's `ssl` now loads pymssql's copy (same 3.6.3); the build fails if the versions ever differ.

## Packaged-app checks (for the user's final test list)

Company SQL Server (Appendix A, about 10 minutes):
1. Connect with defaults (SSL Prefer); read `@@VERSION`; with VIEW SERVER STATE, `encrypt_option`/`protocol_version` from `sys.dm_exec_connections`.
2. verify-ca / verify-full with the company CA file and host name.
3. Does the server require strict (TDS 8.0) encryption? (Not supported — decides that carry.)
4. `WAITFOR DELAY '00:01:00'` + Stop: stops within about a round trip; `SELECT 1` works after.
5. Stop while a large SELECT streams: cancelled at once. A SELECT blocked on another session's row lock: auto-commit → dropped after ~1 s with the explanation; in a transaction → a warning, a second Stop drops.
6. `SELECT 1; WAITFOR DELAY '00:00:30'; SELECT 2` + Stop during the wait: cancelled, or (when it escalates) the explained drop; the next statement / Reconnect works.
7. `BEGIN TRAN; UPDATE #t …; WAITFOR …` + Stop: the transaction stays open (badge); Rollback undoes the UPDATE.
8. `PRINT` / `RAISERROR(…,0,1) WITH NOWAIT` / delay / `RAISERROR(…,16,1)`: live and in order; the error shows number and line.
9. Types: `datetimeoffset(7)` (7th digit lost), `decimal(38,10)`, `NEWID()`, `varbinary(max)`, `sql_variant` (bytes), a 100k-character `nvarchar(max)`, `xml`, a `Chinese_Taiwan` varchar column.
10. Two tabs streaming large SELECTs over TLS at once (compare with `HASHBYTES`).
11. `HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')` (the app must work without it).
12. A `SET NOCOUNT OFF` DML loop in auto-commit + Stop → explained drop after ~1 s; inside `BEGIN TRAN` → warning only, a second Stop ≥ 1 s later drops; a double-click on Stop inside a transaction keeps it.

Packaged app:
- A SQL Server profile with a saved password lands in Keychain service `app.dbaguy`; the first launch after a rebuild is slow (~4 s) and may ask for Keychain access again.
- A read-only SQL Server profile shows the "cannot enforce read-only" note and blocks `EXEC`.
- `GO 3` runs the batch three times (one Messages line each); `GO 0` runs nothing; a flagged `GO 100` batch asks once and says "runs 100 times".
- SSL mode Require and the dev SSH tunnel (`mssql:1433`) both run `SELECT 1`.
- Not checked in the browser during 03b (do these once): export of `SELECT a.id FROM dbo.big_table a CROSS JOIN (VALUES (1),(2),(3),(4),(5)) v(n)` to CSV (500,001 lines) and its Cancel; copy rows as INSERT (`[Order Items]`-style names, `N'…'`, `0x…`); rowversion / sql_variant cells show hex; "Filter by this value" on `seen`, `flag`, `guid`, `data` of `types_demo`; a read-only copy of the profile allows `DECLARE @x int = 1; SELECT @x` and `SET NOCOUNT ON`; Cancel of an export in manual mode shows no "aborts the transaction" warning.
- If the backend ever stops unexpectedly, the banner says it was restarted, editor text is kept, tabs offer Reconnect.

## Incidents

- Native crash reports during development (Homebrew Python, not the app): 11:22/11:28 from Task 8's first cancel design (rewritten); 12:31 ×5 from the ④ survey's Couchbase SDK probes (the SDK is not used); 15:02–15:03 ×3 from a test fixture that closed a connection while a reader thread was inside FreeTDS (test fixed; the guard now makes it impossible in code). After the fixes: 0 new reports across all storms, hammers and the packaged verification. The DiagnosticReports folder was emptied at 16:53 by something outside the agents (presumably the user).
- The Task 10 agent's scratch script matched its own tool shell by command line and terminated it (no DbaGuy or user process involved); rule restated: exact executable path / PID only.
- `scripts/measure-startup.sh` used to quit "DbaGuy" by name with a pattern-kill fallback; now it touches only the instance it launched.

## Deferred minor findings

- Always-N literals can force an index scan on a SQL-collation varchar column (performance only).
- `up --wait` waits out 100 health retries (~5 min) to report a failed seed (README says so).
- URL import: a digits-first password holding a literal `;user=…@…` can still split (contrived); a JDBC property value with `@` is refused as malformed; tunnel ports add freetds.conf sections for the process lifetime.
- Read-only filter guard accepts `id = 1 RECEIVE * FROM q` (the server rejects the built SELECT); RECEIVE narrowing probed on 2022 only; over-blocks of an alias `conversation` after `END` or `send` before `ON conversation`.
- A mid-run policy-change confirmation asks once per GO copy; `GO 1000` of a SELECT yields 1,000 result tabs.
- `COMMIT; SELECT <big>` while paused reports a transaction (Ruling 17); a shutdown exactly at completion reports "rolled back" for a statement that finished.
- A second thread calling `run()` while the worker is inside is refused by the guard but disturbs the cancel bookkeeping (the app never does it).
