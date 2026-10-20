# Plan 05a (grid editing, backend) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-19-05a-grid-editing-backend.md` (planning rulings E1–E25, assumptions A1–A15). Spec: `docs/superpowers/specs/2026-10-19-05-grid-editing-design.md` (coordinator review CR-1…CR-4; Task 11 and the fix wave wrote the 05a decisions next to the text they refine, marked 〔05a〕). Executed with subagent-driven development: a pre-flight review (0 High, 6 Medium, 18 Low, all accepted and carried per task), 11 tasks, every code task reviewed with fix rounds and a scoped re-review (Tasks 9 and 10 are tests and Task 11 docs: reviewed by the final review), a final Opus review of the whole range ("With fixes": 0 Critical, 1 Important, 10 Minor) and one fix wave with a scoped re-review. Commits 03ff157..HEAD on `feat/05-grid-editing`; full backend suite 5,062 passed. 05a has no UI: 05b builds the grid on this contract, and ⑤ merges to `main` after 05b.

## Outcome against the pain points (feature-priorities §2, §3, §8)

- Q1 light and fast: no new dependency (sidecar 49,148 → 49,224 KB); previews of 10,000 changes take 129–294 ms; an apply of 1,000 UPDATEs takes PostgreSQL 0.8–1.1 s, MySQL 0.7–1.0 s, SQLite 0.4 s and SQL Server 1.5–2.6 s after the fix wave (it was 7.2–9.6 s frozen under load before skipping the per-statement describe); ending a paused 1,000,000-row browse before an apply costs at most about 0.6 s. Startup and memory unchanged from 04c (backend `footprint` 76–79 MB).
- Q2 never lose work: an apply that cannot be shown to be complete is `unknown`, never `committed` or `rolled_back`, and nothing is ever resent (E1, E10–E12, G10, G11, G13); every apply ends with exactly one `edit_outcome`, also one refused at the WebSocket layer (preflight M1) or cancelled while queued; History records each apply that sent a statement.
- Q3 trustworthy: what runs is the previewed text (a single-use, unguessable plan bound to its profile and database); an UPDATE checks the original values of the columns it edits and must match exactly one row; key choice refuses rows that cannot be identified exactly; literals never change a value silently (dates under a dmy language, integral exponents, SQLite typeless values, backslashes, numbers); the outcome classes were checked live on every engine, including MyISAM, triggers, tx-dooming errors and concurrent sessions.
- Q4 focus: table-data tabs of the four relational engines only (no query results, views, keyless tables, Couchbase; spec R-05-1, R-05-2, R-05-12).
- Q5 multi-engine: one plan builder, one worker flow, per-engine knowledge only in `edit_target` and the literal tables; Couchbase is untouched (`row_editing` False); Filter by value and Copy as INSERT share the literal module (their only change is the safer T-SQL date form).
- Q6 macOS: no UI yet (05b).
- §3 S1 (Must by §8) has its backend; M7 values exact; M10 server text and the failing statement named; M11 one transaction, read-only refused twice (policy and server where the server can).

## Rulings made during execution

Labels: E = the plan's rulings; F = coordinator rulings on the pre-flight review; G = coordinator rulings on task reviews; L/M = pre-flight and review finding numbers.

- F1: every pre-flight finding accepted as recommended, carried into its task's dispatch. F2: at most one agent runs live dev-DB tests at a time unless the engines are disjoint, and a full suite never overlaps another live run (scratch views and types in the seeded schemas break the exact-list introspection tests).
- G1: in a SQLite column without a declared type, a value is written bare only when SQLite stores exactly that text (canonical int64, or `repr(float(t)) == t`); otherwise quoted (`007`, `0912…` keep their zeros).
- G2: `literals.unmatched_reason` says why a match term is missing (preview-only value, or a character that cannot be sent), shared with the sendable rule; the plan's unchecked and key texts come from it.
- G3: a PostgreSQL server with `standard_conforming_strings` off makes tables read-only (literals assume it on).
- G4: a SQLite STRICT table's ANY column is `other`; a virtual table's shadow tables are read-only (`pragma_table_list` type `shadow`). A table-data tab never runs user SQL, so its session settings equal the metadata connection's.
- G5/F1(T-SQL): a T-SQL date alone is written `N'yyyymmdd'` and a space-separated datetime is joined with `T`; verified live under British and us_english (with the old form British stored 2026-05-10 for 2026-10-05).
- G6/F5: on MySQL, SQL Server and SQLite an integral number written with an exponent goes as its digits (≤ 38); fractions and numbers Decimal cannot hold keep the typed text.
- G7: a plan whose UPDATE moves a row onto another UPDATE's original key is refused (`key_collision`): after a concurrent delete it committed a wrong result (measured on PostgreSQL). Placed on the change that moves onto another's key (a swap: once, on the later).
- G8: MySQL's matched rows are the first of exactly three integers in the UPDATE's info text (18 of 26 `lc_messages` translate it); anything else fails closed (`row_count_unknown`).
- G9: SQL Server temporal history, graph node/edge and ledger tables are read-only (flags read through `COL_LENGTH`-guarded dynamic SQL, safe on 2016–2019); `json` and `vector` (2025) are not compared. Hypothetical indexes never count as keys.
- G10, G11, G13: on a table that cannot roll back (MyISAM), any failure once a statement was sent is `unknown` (the statements before it applied); a 0-row conflict and a cancel caught before sending keep partial / rolled_back. A COMMIT that returns with a transaction still open is `unknown`. SQL Server 3609 (a trigger's own COMMIT) is `unknown`.
- G12: SQL Server logs only an error number at WARNING; a server's text (which may echo literals) only at DEBUG.
- G14: `server_text` reads a pymssql error only from `exc.args`. pymssql's `.text`, `.message`, `.srvname`, `.procname` are C `char*`: NULL on an exception built in Python (reading one segfaults the process), and on a raised one they point into the connection's message buffer. The `procname` read stays behind `number != 0`.
- G15: test-only tasks verbatim from the brief (Tasks 9, 10) and docs-only Task 11 are reviewed by the final review.
- G16: the final fix wave: SQLite `UPDATE OR ABORT` / `INSERT OR ABORT INTO` (an `ON CONFLICT REPLACE` table silently deleted untouched rows and reported committed); SQL Server plan statements skip the describe; MySQL refuses before any DML when BEGIN's status flag (512) shows a backslash mode other than the plan's; PostgreSQL bytea originals compare with `=`; the 64 MiB refusal's `detail[0].msg` is plain text.
- L15: a request holds at most 64 MiB of value text (422); stored plans at most 128 MiB in all, oldest first; a single larger plan is refused (422 in `error.message`).
- Others recorded in the plan and spec: E1–E25; the stored plan's TTL runs on a clock that counts while the Mac sleeps; MySQL invisible columns are never keys; PostgreSQL domains are read to their last base type; CR-2 plan ids are uuid4.

## Carries

| Carry | Where |
|---|---|
| The whole frontend (spec §3.6) and the workspace `staged_edits` (§3.7), on the contract in the appendix below (every code, status, event and field). | 05b |
| Send only the DELETE when one key has both a DELETE and an UPDATE (both planned now; the apply fails closed with a misleading conflict). | 05b reducer |
| Apply only the plan of the preview dialog on screen: a superseded preview's plan stays applicable for 15 minutes (final review M7). | 05b |
| Show `detail?.[0]?.msg ?? message` for a 422 from `/edits/*`. | 05b client |
| A table-data tab that gets `transaction_open` (SQL Server `IMPLICIT_TRANSACTIONS` login default) has no way out but closing: offer Rollback or Reconnect. | 05b |
| On a repeated "the session's sql_mode changed since the preview" refusal (the server's global sql_mode changed mid-session), suggest reconnecting the connection. | 05b |
| SQLite: another tab with a paused result on the same file makes a commit wait 5 s and roll back: a clearer message. | 05b |
| Engine icons: monochrome (`currentColor`) and a real SQL Server icon (user request, 2026-10-19; check the icon's licence). | 05b |
| SQL Server statement batching for large commits (each statement still needs its own count); company servers with network latency multiply the per-statement round trips. | spec §12 |
| Stronger concurrency checks (PostgreSQL `xmin`, SQL Server rowversion), binary-collation comparisons, an insert-only mode for keyless tables, editing values beyond 64 KiB, query-result editing. | spec §12 |
| A cancel caught in the last instant before a statement is sent counts as sent (`unknown` on MyISAM): the adapters would need to report "not sent". | later |
| `tests/test_edit_apply_live.py` and the flow tests use the shared helpers; the jsdom-style probes of 05b should become real component tests if Testing Library is adopted. | 05b |

## Packaged-app checks

- The frozen sidecar (in `src-tauri/resources/backend` and inside the built `.app`) previewed and applied on PostgreSQL, MySQL, SQL Server and SQLite with asserted outcomes (committed; a replay refused `plan_expired`; the expected rows), 3 runs. `codesign --verify --deep --strict` passed.
- Not re-measured frozen after the fix wave: SQL Server's apply time (in-process 1.5–2.6 s for 1,000 UPDATEs).

## Company database checklist for 05a (05b adds the UI items)

05a 沒有 UI 入口，這些項目在 05b 的 grid 編輯完成後從 grid 做。請在公司資料庫上用自己建的 scratch 表試，不要拿正式資料表；括號裡是 dev 已經看到的結果。

### MySQL
1. `SELECT @@sql_mode`：有沒有 `STRICT_TRANS_TABLES`、`NO_BACKSLASH_ESCAPES`。
2. 改一格成含反斜線的值（`C:\tmp\new\`）→ commit → 重讀：與輸入相同（dev：兩種模式都存回原樣）。
3. 沒有 `STRICT_TRANS_TABLES` 的 server：`VARCHAR(5)` 改成 10 個字會被截斷、只回警告（1265），apply 照樣 commit；嚴格模式回 1406、整批 rollback（dev 是嚴格模式）。
4. MyISAM 之類不能 rollback 的表：預覽頂端警告；送出後才失敗的那一句是「結果不明」（Commit 停用到 Refresh），「那列已被改過或刪掉」的衝突是部分套用（dev 都驗過）。
5. `lc_messages` 不是英文：把一格改成它原本的值不該被當成衝突（G8；dev 只有英文，翻譯以單元測試驗）。

### SQL Server
1. `user options`（或 `DBCC USEROPTIONS`）：有 `NOCOUNT` 時照常 commit（dev 驗過）；有 `IMPLICIT_TRANSACTIONS` 時 apply 被拒（`transaction_open`），dev 沒有這種 login，未實測。
2. varchar key 的 collation：SQL collation 下 DbaGuy 不加 `N`，保持 index seek（dev 驗過）；大表上改一格的速度要看。
3. 有 AFTER／INSTEAD OF trigger 的表：AFTER trigger 多插的列不影響列數；trigger 回傳結果集 → 整批 rollback（「Could not confirm how many rows changed」）；trigger 自己 COMMIT（3609）→ 結果不明。
4. temporal history、graph、ledger 表應該唯讀並說明原因（ledger 只有單元測試）；`json`／`vector`（2025）沒測過。
5. `SELECT @@LANGUAGE` 是 dmy 的語系時，改一格日期，存下來的日期要對（dev：British 與 us_english 都對）。

### PostgreSQL
1. `SHOW standard_conforming_strings` 應為 `on`；off 時表會唯讀（G3）。
2. 有 domain、enum、array、composite、timestamptz（公司的 `TimeZone`）的表：改一格不該出現假衝突（dev 在 Asia/Taipei 驗過多種型別；enum、citext 未實測）。`bytea_output = escape` 的 server 也不該假衝突（fix wave）。

### 每台 server
- 一次 1,000 個變更的 commit 時間與進度（dev：PG 0.8–1.1 秒、MySQL 0.7–1.0 秒、SQLite 0.4 秒、SQL Server 1.5–2.6 秒）；跨網路的 SQL Server 每句兩個往返，可能更慢，確認有進度與 Cancel。

## Incidents

- Two Python segfaults the user reported (2026-10-19 22:36 and 22:38): both `python -c` processes under the Claude desktop coalition, not the app. The first was an implementer's probe that built a pymssql `MSSQLDatabaseException` and read `.text`; the second, the coordinator's isolated confirmation probes (each crash showed the user a macOS dialog). The app never builds that exception, but G14 made its error reading safe regardless, with a subprocess-isolated regression test. Rule kept in project memory: never read those attributes on a constructed exception, and run crash-prone checks once, isolated.
- An API weekly-limit stop halted three reviewers mid-run; all were resumed with their saved work after the user's "continue".
- The scratch-object rule held throughout: only `zz_05a_*` objects, dropped by exact name (`drop()` refuses any other name); every run ended with none left, and the Couchbase seed stayed exact.

## Deferred minor findings

- A cancel that lands in the last instant before a statement is sent counts as sent (documented).
- sqlglot cannot parse `UPDATE OR ABORT`; the policy's keyword fallback still classifies it as WRITE (one DEBUG line per SQLite UPDATE).
- Case-insensitive collations (MySQL, SQL Server) let a concurrent change that only alters letter case through the original-value check; swapping unique values between two rows rolls back (use SQL).
- MySQL `waiting()` in the test helpers counts an UPDATE running more than about a second as a lock wait (test-only).

## Appendix: the 05b contract (from the final review, after the fix wave)

**`POST /edits/target {profile_id, path}`**
- Path has at least one segment. PostgreSQL and SQL Server paths must start with their database segment, or the answer is 422 `validation_failed`.
- **200 `EditTarget`:**
  - `editable`, `reason` (the toolbar text), `key`, `key_source`, `transactional`, `backslash_escapes`, `path`.
  - `columns[]`: `name`, `type_name`, `kind`, `nullable`, `has_default`, `writable`, `read_only_reason`, `compare` (`equals` / `as_text` / `none`), `unicode`.
  - `editable=false` with a reason is not an error. A read-only profile gets "This connection is read-only" without connecting.
- **Errors:**

| Status | Code | When |
|---|---|---|
| 401 | `unauthorized` | |
| 404 | `profile_not_found` | |
| 404 | `not_found` | Table missing |
| 422 | `not_editable` | Couchbase: "Rows of this connection cannot be edited in the grid" |
| 422 | `validation_failed` | Request shape (`detail[]` without input), or a database segment mismatch |
| 428 | `password_required` | Open unlock, then retry |
| 502 | `connection_failed` / `tunnel_failed` | |
| 400 | `query_failed` | A catalog read failed after its one reconnect, including SQL Server's "locked by another session" |
| 409 | `session_busy` | Rare |

**`POST /edits/preview {profile_id, path, changes:{updates[{id,key,set,original}], inserts[{id,values}], deletes[{id,key}]}}`**
- **200 `EditPreview`:**

| Field | Contents |
|---|---|
| `plan_id` | string, or null when there are problems |
| `begin`, `commit` | The transaction frame's texts |
| `statements[]` | `change_id`, `kind`, `sql`, `unchecked[{column, reason}]`, `row`; order: deletes, updates, inserts; empty when there are problems |
| `problems[]` | `change_id`, `column` (null for `key_collision`), `code`, `message` |
| `warnings[]` | `code`, `message`, `change_id`, `columns` |
| `counts` | `{updates, inserts, deletes}` |
| `script` | The whole text; "" when there are problems |

- **Problem codes:** `invalid_number`, `invalid_json`, `invalid_hex`, `invalid_bool`, `invalid_text`, `value_too_large`, `read_only_column`, `unknown_column`, `not_nullable`, `key_incomplete`, `default_not_allowed`, `key_collision`.
- **Warning codes:**
  - `unchecked_columns`: one per statement, with `change_id`.
  - `non_transactional`.
  - `missing_required`: per insert.
- **Errors:**

| Status | Code | When / where the text is |
|---|---|---|
| 403 | `read_only` | |
| 422 | `not_editable` | The target's reason |
| 422 | `validation_failed` | Shape errors: no change, more than 10,000 changes, duplicate ids, a set column without `original`, text vs kind, a lone surrogate (`detail[].loc` names the cell), more than 64 MiB (`detail[0].msg`, prefixed "Value error, ") |
| 422 | `validation_failed` | A malformed key or original cell; `error.message` "Not a grid cell: …", no detail |
| 422 | `validation_failed` | A single plan over 128 MiB; `error.message` |

  Plus the same 404 / 428 / 502 / 400 as `/edits/target`.

**WebSocket command:** `{"type":"apply_edits", "request_id", "plan_id" (1–64 characters)}`.

**WebSocket events.** `message` events (a trigger's NOTICE / PRINT, warnings) may appear anywhere between `started` and `edit_outcome`.
- **Normal:**
  - A paused browse's own events and `finished` come first.
  - Then `started` → `affected{index, rows}` × n (only when the count is known; `index` indexes `statements`) → [`error{code, message, position}`] → `edit_outcome` → `session_state` → `finished`.
  - `position` is relative to `statements[statement]`.
- **Committed with a warning (non-transactional COMMIT error):** a `message` warning "COMMIT reported an error after every statement had taken effect: …", no `error`.
- **Rollback failed on a live connection:** a `message` warning "The transaction could not be rolled back: close and reopen this tab", and `finished.in_transaction` true.
- **Queued and cancelled, or the session closed:** `started` → `message` → `edit_outcome(rolled_back)` → `session_state` → `finished(cancelled=true)`. No `error`.
- **Refused at the WebSocket layer:** `error(invalid_command | session_busy)` → `edit_outcome(rolled_back)` → `finished`. No `started`, no `session_state`.
- **No `edit_outcome` before the socket ends** (socket lost, backend restarted): unknown (R-06). Never resend.

**`error.code` on an apply.** The rules below come from `edit_apply.py` and the worker.

| Code | Notes |
|---|---|
| `plan_expired`, `plan_mismatch` | Same text; preview again |
| `read_only` | |
| `transaction_open` | Offer Rollback or Reconnect, the L12 carry |
| `invalid_text` | |
| `cancelled` | "Cancelled: …" |
| `edit_conflict` | 0 or more than 1 rows; the text names `row` |
| `row_count_unknown` | |
| `transaction_failed` | |
| `outcome_unknown` | |
| `connection_lost` | |
| The server's own | PostgreSQL SQLSTATE, MySQL errno, SQL Server number as a string, SQLite `SQLITE_*` |
| `null` | "Internal error: …", or SQL Server error 0 |

**`edit_outcome` fields:** `outcome` (`committed` / `rolled_back` / `partial` / `unknown`), `applied[]`, `already_gone[]`, `failed_change`, `statement`.
- `failed_change` and `statement` are null:
  - for a cancel between statements;
  - for checks before COMMIT;
  - for a COMMIT error;
  - for refusals.
- `partial` happens only on non-transactional tables, after a 0-row conflict or a cancel before sending.
- **`unknown` (Commit disabled until Refresh):**
  - COMMIT's answer lost;
  - COMMIT returned with a transaction left open;
  - the transaction vanished before COMMIT;
  - SQL Server 3609;
  - a worker bug after BEGIN;
  - any failure after sending on a non-transactional table.
- `finished.cancelled` is true for a cancelled `rolled_back`.
- **History:**

| Outcome | History status |
|---|---|
| committed | `ok` |
| cancel | `cancelled` |
| rolled_back | `error` |
| partial | "Partly applied (n of m): …" |
| unknown | "Outcome unknown: …" |

  `sql` is `script` and `rows` is the rows matched. A row is written only once BEGIN reached the connection.

**Carries.**
- **From the ledger and the Task 11 report:**
  - For a key with both a DELETE and an UPDATE, send only the DELETE.
  - Show `detail[0].msg ?? message` for 422s on `/edits/*`.
  - Offer Rollback or Reconnect on `transaction_open` (SQL Server's `IMPLICIT_TRANSACTIONS` user option).
  - Engine icons: monochrome, plus a real SQL Server icon (user request).
  - The 05a follow-ups note, the coordinator's. It must define the F/G/L labels.
- **Added here:**
  - M4's SQLite lock message.
  - M5's "Value error, " prefix.
  - M6's two event shapes.
  - M7's plan-id hygiene.
  - The preview header after an unknown outcome (R-05-6: new rows would be inserted again).
  - Every UPDATE sends an `original` per set column and never a `default` cell.

After the fix wave: SQLite statements read `UPDATE OR ABORT` / `INSERT OR ABORT INTO`; MySQL adds the refusal `sql_mode_changed` (rolled_back, nothing sent); the 64 MiB refusal is `detail[0]` with type `commit_too_large` and plain text; other shape errors keep pydantic's "Value error, " prefix (05b never sends them).
