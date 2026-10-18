# Plan 03a (dialect groundwork + MySQL) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-17-03a-dialects-and-mysql.md` (planning rulings 1–32 in its last section). Spec: `docs/superpowers/specs/2026-10-17-03-mysql-mssql-design.md` §5 item 1. Executed with subagent-driven development: an Opus pre-flight scan (it applied Tasks 1–5 to a scratch copy), 11 tasks each reviewed with fix rounds re-reviewed, every review also checked against the researched pain points; final Opus whole-branch review with one fix wave and a follow-up. Commits 88a50ae..264f338 on `feat/03-mysql-mssql` (03b continues on the same branch).

## Outcome against the pain points (feature-priorities §2, §3)

- Q1 light and fast: 100k MySQL rows stream in 0.59 s, a 500-row page in ~3–4 ms (budget 2.0 s / 25 ms, pure-Python driver). Packaged sidecar 41.9 MB (+0.3 MB; the C extension would add 13 MB). Startup 0.88–1.12 s (2.3 s first launch after a rebuild), footprint 156 MB.
- Q2 never lose work: cancelling or closing a CALL never desyncs the protocol (found and fixed in review); a cancel keeps the transaction; a session is dropped only when the KILL itself cannot be sent; closing a tab never cancels a statement before the user answers.
- Q5 / §8 one tool for every DB: MySQL runs every flow PG and SQLite have (tree, DDL, table data, query, completion, copy/export, history, transactions, read-only, SSH); the UI reads engine capabilities, no `dialect === "postgres"` left.
- M11 safety: read-only enforced at the server (`SET SESSION TRANSACTION READ ONLY`, 1792 on any write incl. temp tables) and in the classifier (~400 bypass probes over the reviews, incl. `/*!`, `/*M!`, INTO OUTFILE, LOAD DATA, START REPLICA, DESCRIBE ANALYZE, SET locking reads); the transaction badge follows the server after errors that end a transaction.

## Rulings made during execution

- R-03a-P1: speed budget instead of "≤ 3× PG" — pure decode is ~16× psycopg; interactive paging is what matters.
- R-03a-P2…P7: pre-flight fixes (tests assert only visible schemas; unconditional hooks; per-statement KILL flag; TLS hint for `disable` logins; `--log-bin-trust-function-creators=1`; small plan fixes).
- R-03a-T2: the MySQL body tracker keeps a block stack; a bare `end` closes a BEGIN only after `;`/BEGIN — a routine is never split so that its tail runs alone.
- R-03a-T4: on MySQL only BEGIN / START TRANSACTION / COMMIT / ROLLBACK / SAVEPOINT / RELEASE are TRANSACTION; other START … is UNKNOWN.
- R-03a-T6: seed additions only in new tables (edge_values, 客戶備註, `group`).
- R-03a-T7: JDBC `verifyServerCertificate`/`requireSSL` never import weaker TLS than asked.
- R-03a-T8: SHOW CREATE refused (1142/1356/1227) → no DDL but columns kept; views fall back to information_schema.
- R-03a-T9a…f: KILL a CALL closed at a result-set boundary; refresh status flags after server errors with `SHOW WARNINGS LIMIT 0` (keeps diagnostics); zero-part dates filter via `CAST(col AS CHAR)`; KILL on close even if already killed; the drain time cap only when the KILL could not be sent.
- R-03a-K1: dev runs (any non-default `DBAGUY_DATA_DIR`) use Keychain service `app.dbaguy.dev`.
- R-03a-F1: ALTER … DROP/TRUNCATE PARTITION and DROP PRIMARY KEY need confirmation at standard.
- R-03a-F2: `time_zone`/`sql_mode` options are applied by the adapter with quoted literals (the driver pasted them unquoted).
- R-03a-F3: `character_set_results` is not allowed on read-only and not a driver option (results are always decoded UTF-8).
- R-03a-F4: closing a tab never cancels a running statement before the guards are answered.
- R-03a-F5: after a KILL the result-set wait cap starts only once the CALL proves it swallowed the KILL.

## Carries

- 03b: `_EXPLAIN_KEYWORDS`, `_TRANSACTION_STARTS` and every Dialect-keyed table need a TSQL entry (the tripwire test names missing tables); `server_read_only=False` drives the SQL Server note in the connection dialog.
- Later (performance): closing a paused huge MySQL result drains what the server already sent (≈1.2–1.9M rows, 3.5–6.6 s on OrbStack); bulk socket reads or the C extension would cut it.
- Optional: refuse or warn on `ANSI_QUOTES` / `NO_BACKSLASH_ESCAPES` in the profile's `sql_mode` option (the lexer assumes the default mode — Ruling 25).

## Packaged-app checks (for the user's final test list)

1. A MySQL connection with SSL mode Prefer / Require / verify-ca against a real server; the "needs TLS for the first login" hint with SSL disabled after a server restart.
2. MySQL query tab: ⌘↵ on a DELIMITER script, Cancel on a long statement, manual commit badge, read-only profile blocking an INSERT.
3. The Keychain: a profile saved in the packaged app is stored under `app.dbaguy` (dev runs use `app.dbaguy.dev`).

## Incidents

- During the Task 10 browser check a dev MySQL profile save wrote the fake dev password ("dbaguy") into the real Keychain (the form-fill missed the save-password checkbox). It was removed through the app; R-03a-K1 now isolates dev runs.
- During the final fix wave an agent stopped its Vite server with `pkill -f vite`, which matches every Vite process; a training-app Vite dev server may have been killed (one was running again afterwards). Agents now stop only processes they started, by PID.

## Deferred minor findings

- MySQL tabs do not follow a typed `USE` (completion keeps the profile database); no database picker for MySQL by design.
- Completion scope: `FROM partition p` / `UPDATE IGNORE t` lose the relation; a comma list after `USE INDEX (…)` ends; hint words are global.
- `SET … TRANSACTION ISOLATION LEVEL` is blocked on read-only (intentional); SET PASSWORD / DEFAULT ROLE keep a SESSION label (behaviour correct).
- A CASE statement used directly as a HANDLER action with a bare `end` column can still cut a body without DELIMITER.
- URL import: `_ADDRESS_FORM` scans the whole netloc (fixed text, contrived false positives); contradictory `useSSL=false&requireSSL=true` imports disable.
- The empty-delimiter lexer test would hang rather than fail if its guard were removed.
- MariaDB: `STATISTICS.EXPRESSION` (index detail) needs MySQL 8.0.13+; MariaDB is not a target.
