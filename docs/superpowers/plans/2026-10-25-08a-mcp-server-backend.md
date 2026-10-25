# 08a: MCP server, backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only MCP server inside the sidecar: Claude Code (and other MCP clients) reach the connections the user shares, through DbaGuy's own connection settings, to list and describe the schema and run one read statement per call on PostgreSQL, MySQL, SQL Server, SQLite and Couchbase. Writes stop at DbaGuy's gate, and at the server wherever the server can enforce read-only. Every call stays inside limits on rows, cell size, response size, memory and time, and every query, refused ones included, goes to History marked as MCP. The SDK loads only when the user switches the server on. 08b builds the frontend on top of this plan's API.

**Architecture:** `agent/` holds the MCP endpoint. Only `agent/server.py` imports the SDK, and only `AgentHost` imports that module, on a background thread when the Settings switch turns on. The endpoint is a second uvicorn server on its own thread and socket, fixed on 127.0.0.1:17531, or 17532 outside the app's data dir. In front of it sits `BearerGate`: the bearer token (Keychain, or memory), then the Host, then any Origin.

The tools' logic has no SDK in it:
- `AgentTools`: connections and the schema tools, on a read-only metadata connection of the agents' own (`MetadataService` key `agent=True`).
- `QueryTools`: `run_query`, `explain`, `sample_fields`.

Every statement passes `services/agent_gate.agent_read` first. Its checks: one statement, a first word on the dialect's allow-list, a read, no denied function (PG `U&` names decoded), and MySQL has no INTO and no executable comment.

`AgentReader` then leases the agent session for (profile, database), a normal `SessionWorker` opened on a read-only copy with `agent=True`:
- SQLite adds `query_only` and an authorizer.
- MySQL drops the lexer-unsafe `sql_mode` flags.

It sends the dialect's `ReadFrame` with `call()`: PG `BEGIN READ ONLY` plus `statement_timeout`, MySQL `max_execution_time` plus `START TRANSACTION READ ONLY`, SQL Server `BEGIN TRANSACTION`. The statement goes as an `ExecuteCommand` whose worker check is the same gate, with no rows read ahead. The reader reads it page by page as `agent/shape.ResultBudget` sizes the pages: 1 row first, then up to 100 rows or about 8 Mi characters, stopping at the response budget, at max_rows, or after 64 Mi characters of values. Then it discards, rolls back and releases session locks (`pg_advisory_unlock_all()`, `RELEASE_ALL_LOCKS()`).

Supporting pieces:
- MySQL's shared `ReadOnlyGuard` keeps every read-only session READ ONLY, agents' and users' tabs alike.
- History gets a `source`, a `client`, `repeats` and its own 5,000-record cap.

**Tech Stack:** Python 3.13 sidecar: FastAPI, pydantic v2, loguru, pytest; psycopg 3, mysql-connector-python (pure), pymssql, sqlite3, Couchbase over `http.client`. New: `mcp` 2.3.0 (MCPServer, Streamable HTTP; tests use its `Client`), with `mcp-types`, `httpx2`, `httpcore2`, `truststore`, `jsonschema` (with `referencing`, `rpds-py`, `attrs`, `jsonschema-specifications`), `pyjwt`, `cryptography` (with `cffi`, `pycparser`), `sse-starlette` and `python-multipart`. All of it installs alongside FastAPI 0.143.2, Starlette 1.8.0, uvicorn 0.54.0 and anyio 4.15.1 without conflicts. The frontend changes in only three places, each a few lines, so its type check keeps passing; 08b does the rest.

**Spec:** `docs/superpowers/specs/2026-10-25-08-mcp-server-design.md`, all of it, with the coordinator rulings CR-08-1…CR-08-12 overriding older text they contradict. 08a is §11's backend list: R-08-1…R-08-12, D-08-1…D-08-14, §3 (§3.8 is 08b's), §4, §5, §6's backend rows, §7, §9, §10 A1–A15, §12, appendix A as regression tests. The "Spec issues found while planning" table below (P1–P18) is ruled by the coordinator before Task 1. P18 changes a binding value of D-08-9. A ruling that differs from a proposal changes the named task before it starts.

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as plan completeness, and say what was solved, what was stepped on and what regressed.

| Source row | How 08a serves it, or avoids regressing it |
|---|---|
| §8 「AI：只做 MCP server」, §3 S9, §5 principle 6 | The MCP server and nothing else: no chat, no explain/fix buttons, no resources or prompts, no writes, no OAuth, no stdio bridge (spec §1). It uses the connections the user already set up, and asks no password over MCP. |
| §5 principle 1 「預設關閉、可全域關閉」 | It is off by default, and the switch turns it off as a whole. With the switch off the SDK is never imported (Task 1 checks `sys.modules` in a fresh process). Each connection's `mcp_access` is `off` by default, old profiles included. |
| §5 principle 3 「預設只送 metadata」 | `schema` access gives names, types, keys and definitions, and reads no rows. Rows need `query` access. |
| §5 principle 5 「唯讀必須在 DB 層級強制」 | PG, MySQL, SQLite and Couchbase enforce read-only at the server. Each session's default is read-only, each call runs in a read-only transaction that is rolled back, SQLite also gets `query_only` and the authorizer, and Couchbase gets `readonly=true`. Task 6 sends writes with the gate taken out (`no_gate`) and the server refuses them. SQL Server is the documented exception: the gate checks, and the transaction rolls back. The gate also closes the paths a read-only transaction leaves open (server files and programs, ending other sessions, session locks, SQL run from text). The login's dangerous privileges are read and shown (CR-08-1). |
| §2 Q1 輕、快、低記憶體 | With the switch off nothing of the SDK is loaded. Measured while planning, with the switch on: in-process, start 0.29–0.35 s and RSS +27 MB; frozen sidecar, 0.26–0.32 s and RSS +22 MB (§7: ≤ 1 s, ≤ 30 MB). `run_query` with a session open: PG 4.4 ms, MySQL 2.8 ms, SQL Server 6.1 ms, Couchbase 1.3 ms (median; §7: ≤ 100 ms). Memory per call is bounded by the response budget plus one page. One 100 MB value reads that one row (P10). Sidecar 63,156 KB (+13.6 MB, as §7 estimates). |
| §2 Q2 穩定、不丟工作 | Agents get sessions of their own and never touch the user's tabs, transactions or staged edits. Every call rolls back and releases its locks. A frame that does not end cleanly closes its session. Agent History has its own 5,000 cap and never pushes out the user's 50,000. Starting MCP never delays the API: it starts 1 s after the app is up, on a thread of its own. |
| §2 Q3 可信任 | Access is off by default and opened connection by connection. The token lives in the Keychain, the server binds 127.0.0.1 only, and requests with a wrong Host or any Origin are refused. Every query and every refused attempt lands in History with the client's name. The login's privileges are disclosed. Read-only MySQL tabs are fixed too (D-08-5: M3 no longer lets a function make them writable). Nothing in the INFO log holds SQL, a value, a server's message or the token (Task 8 audit). |
| §2 Q4 聚焦 80% | Seven tools. No writes, no DML explain, no KV reads, no per-client permissions (spec §1, §12). |
| §2 Q5 多引擎 | One set of tools for five engines. Engine differences live in the adapters and in the tables of `sql/dialects.py` and `services/agent_gate.py`. |
| §2 Q6 macOS 慣例 | Backend only: the Settings API 08b uses (switch, port, token, Copy, Regenerate, the Claude Code command). |
| §3 M2, M11, M12, M13 | M2: layered exploration (`list_objects` → `describe`). M11: DB-level read-only, with production (red) connections capped at 30 s. M12: timeouts, a client's cancel stops the statement (A4), and every limit is in §3.6. M13: History. |
| User requests (CLAUDE.md) | Agents never touch `~/Library/Application Support/DbaGuy`, `~/.claude.json` or port 17531 in tests. The Keychain is touched only by a real run, through `KeyringTokenStore`. Tests use in-memory stores. |

## Global Constraints

- Branch `feat/08-mcp-server` (HEAD 71fea18 while planning). 08a merges with 08b, never inside this plan.
- **Dependency:** `mcp>=2.3,<3` only, added with `cd backend && poetry add "mcp>=2.3,<3"`. Commit `pyproject.toml` and `poetry.lock` together.
  - Imported only by `backend/dbaguy_backend/agent/server.py`.
  - `agent/server.py` is imported only from inside `AgentHost._serve_once`.
  - Tests import `mcp` freely.
  - No frontend or Rust dependency.
- **Binding values, each defined once, in the file named:**
  - `domain/agent.py`: `APP_MCP_PORT = 17531`, `DEV_MCP_PORT = 17532` (R-08-2).
  - `domain/settings.py`: `MCP_PORT_MIN, MCP_PORT_MAX = 1024, 49151`.
  - `agent/server.py`: `json_response=False` (P18), `MCP_PATH = "/mcp"`, `MAX_BODY_BYTES = 1024 * 1024`, `MAX_SESSIONS = 32`, `SESSION_IDLE_S = 1800`, `CLIENT_NAME_CHARS = 80` (R-08-4, D-08-7, D-08-9).
  - `agent/host.py`: `START_DELAY_S = 1.0`, `STOP_JOIN_S = 5.0`, `GRACEFUL_S = 2` (D-08-8).
  - `agent/sessions.py`: `MAX_AGENT_SESSIONS = 8` (D-08-1).
  - `agent/shape.py`: `MAX_ROWS_DEFAULT, MAX_ROWS = 100, 1_000`, `MAX_CELL_DEFAULT, MAX_CELL = 1_000, 20_000`, `RESPONSE_CHARS = 40_000`, `PAGE_RAW_CHARS = 8 * 1024 * 1024`, `PAGE_ROWS_MAX = 100`, `FIRST_PAGE = 1`, `READ_RAW_CHARS = 64 * 1024 * 1024` (P10), `NOTES_MAX, NOTE_CHARS = 20, 500` (§3.6, CR-08-7).
  - `agent/queries.py`: `TIMEOUT_DEFAULT_S, TIMEOUT_MAX_S, TIMEOUT_RED_S = 30, 60, 30` (CR-08-8), `SQL_MAX = 20_000` (CR-08-3), `SAMPLE_MAX = 1_000`.
  - `agent/reader.py`: `FORCE_AFTER_S = 1.5`, `CANCEL_GRACE_S = 10.0`, `FRAME_END_S = 15.0` (P12).
  - `sql/dialects.py`: `SERVER_TIMEOUT_MARGIN_S = 5`, `EXPLAIN_HEADS`, `ReadFrame` and `read_frame()` (D-08-3, D-08-10).
  - `services/agent_gate.py`: `ALLOWED_STARTS`, `DENIED_FUNCTIONS`, `SERVER_READ_ONLY` (D-08-2, CR-08-1, CR-08-4, CR-08-6).
  - `services/history_service.py`: `APP_RETENTION = 50_000`, `MCP_RETENTION = 5_000`, `REFUSAL_MERGE_S = 60.0` (CR-08-3).
  - `adapters/mysql/read_only.py`: `LEXER_UNSAFE_MODES = {"NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI"}` (CR-08-5).
- **From the spec, verbatim where it binds:**
  - §4's error codes and messages: `unknown_connection` 「No connection '<x>' is shared with AI tools; list_connections lists them」; `not_allowed` 「'<x>' allows schema only: run_query needs 'Read-only queries' in DbaGuy's connection settings」; `refused` 「DbaGuy runs exactly one read statement per call: <原因>」; `session_changed` 「The connection's sql_mode changed: the call was not run and the connection was reset」; `password_required` 「'<x>' needs its password: connect to it in DbaGuy first (or unlock it)」 (R-08-9 adds 「DbaGuy never asks for a password over MCP」); `timeout` 「Stopped after 30 s (timeout_s; at most 60, 30 on production connections)」; `busy` 「Another call on '<x>' was still running」.
  - The server's `instructions` (§3.5's draft) and every tool description starting 「Read-only.」.
  - D-08-14's log lines: `MCP <tool> on '<profile>' by <client>: <outcome>, <rows> rows, <ms> ms` and `MCP refused <tool> on '<profile>': <reason code>`.
- **Backend rules (CLAUDE.md):**
  - At INFO and above: tools, connections, clients, outcomes, codes, counts and milliseconds only.
  - Never at INFO and above: SQL text, values, a server's message (it echoes literals) or the token. The SDK's `mcp.*` loggers are at WARNING.
  - Identifier quoting and literals come only from `sql/dialects.py`.
  - Never read `.text`, `.message`, `.srvname` or `.procname` on a pymssql exception you constructed.
  - A tool's error is a `CallToolResult` with `isError`, never a raised `ToolError` (D-08-6).
- **Tests per task:**
  - Each task runs the files it names: `cd backend && .venv/bin/pytest -q <files>`.
  - `npm run gen:api` after every task that changes a model or a route: Tasks 1, 3 and 5. Never edit `openapi.json` or `schema.d.ts` by hand.
  - `npm run typecheck` and `npm test` after every task that touches `src/`: Tasks 1 and 5.
  - The full backend suite runs once, in Task 9: dev containers up, no scratch object, one runner at a time.
- **Dev databases:**
  - All five containers stay up; never start, stop or restart one.
  - Live scratch objects are `zz_08_*` only, made by the tests and dropped by exact name in `finally`: PG `public.zz_08_victim`, `zz_08_flip()`, `zz_08_lock()`; MySQL `zz_08_victim`, `zz_08_lock()`, `zz_08_rw()`, role `zz_08_r` and user `zz_08_u`; SQL Server `dbo.zz_08_victim`.
  - Seeds stay untouched. The Couchbase tests only read; a write the gate would stop goes to key `zz_08_x`, which the server refuses.
  - Every run ends with nothing `zz_08` left (Task 9 checks).
- **Ports:**
  - Tests take a free port between 20,000 and 40,000 (`tests/agent_support.free_port`), never 17531 or 17532 when they serve.
  - Task 1's lazy-import test only reads the default (17532) from `/mcp/status` and never binds it.
  - A sidecar a probe starts gets a temporary `DBAGUY_DATA_DIR` and `DBAGUY_MCP_TOKEN`.
- **Never touch:** `~/Library/Application Support/DbaGuy`, `~/.claude.json` and any real Claude Code config.
  - Task 9's Claude Code check uses `--mcp-config <temp file> --strict-mcp-config` only.
  - Never kill a process by pattern: stop only your own PIDs.
- **Disk:** about 14 GiB free while planning. Check `df -h /` before Task 9's build (it needs about 1 GiB).
- **Repo is public:** dev credentials only.
- **Commits:** after each task, stage only that task's files by path (never `git add -A`). The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- **How the code below was checked:** every task was applied in order in a scratch worktree at 71fea18, and each task's tests, `gen:api`, typecheck and vitest passed after that task alone (see the Dry-run record).
  - New files are given whole. Existing files get one patch per task, made against the previous task's tree, applied from the repository root with `git apply`.
  - `poetry.lock`, `pyproject.toml`, `openapi.json` and `schema.d.ts` come from their commands, never from a patch.

## Spec issues found while planning

The coordinator rules on these before Task 1. Each row gives the spec text, the issue, the proposed ruling, and the evidence. The plan's code follows the proposals. A different ruling changes the task named in the last column.

| # | Spec text | Issue | Proposed ruling | Evidence | Task |
|---|---|---|---|---|---|
| P1 | §3.3: SQL Server 「既有的 `login_can_write`」 | No such function exists anywhere. | Build it: the first six values of SQL Server's `_LOGIN_SQL` (db_owner and INSERT/UPDATE/DELETE/ALTER/EXECUTE on the database), with sysadmin and bulkadmin in the same query. The 〔08a〕 note says so. | `grep -rn "can_write\|HAS_PERMS_BY_NAME\|IS_MEMBER" backend src` finds only the dump's UNMASK check. | 3 |
| P2 | §3.3, A15: the MySQL row reads FILE from `information_schema.USER_PRIVILEGES` | That view lists direct grants only. A FILE that comes from an active role is missing. | Read `SHOW GRANTS` (no FOR): FILE or ALL on `*.*`, then `@@GLOBAL.secure_file_priv`. | Probed on 8.4.11 with `zz_08_r` (FILE) granted to `zz_08_u` as a default role: `USER_PRIVILEGES` gave []; `SHOW GRANTS` gave `GRANT FILE ON *.* TO zz_08_u`. Task 3's role test covers it. | 3 |
| P3 | D-08-1: `on_record` writes `source="mcp"`. D-08-13: the client rides an internal `ExecuteCommand` subclass. | The worker records only what reaches it (no refusal, no lease failure). It measures time to its first pause, which is the agent's 1-row first page. A deadline cancel on a later page is recorded "cancelled", not "timeout". | The MCP layer (`QueryTools`) writes exactly one record per call: the call's outcome, its whole time, the rows the agent got, the client. Agent sessions keep the default no-op `on_record`. There is no `ExecuteCommand` subclass. | `worker._timeout` covers `run()` and the first page; `_complete` uses `first_page_ms`. Task 8's History assertions. | 6, 8 |
| P4 | §3.5 step 5: `text_limit = max_cell_chars` in the command | `max_cell_chars` is at most 20,000, and the worker's `TEXT_LIMIT` is 64 KiB. | `agent/shape.cap_cell` cuts the worker's encoding to `max_cell_chars` (truncated text keeps its `size`), and JSON cells become text previews. No worker change. The memory bound is unchanged: one page. | Task 6's shape tests. | 6 |
| P5 | D-08-2: MySQL INTO and the deny-list | MySQL's executable comments are not covered. `/*! … */` runs as code where the deny-list and the INTO rule cannot see. `/*+ … */` hints can lift `max_execution_time`. | Refuse both kinds outright (`executable_comment`). | The lexer's `EXEC_COMMENT`. `SELECT 1 /*!50000 , GET_LOCK('a', 1) */` is UNKNOWN only because sqlglot fails on it. Probe: a `SET_VAR(sql_mode=…)` hint does not change how its own statement's strings are read, so hints are not an H2 bypass. | 4 |
| P6 | D-08-12: `McpSettings(enabled=False, port=17531 或 17532)` | A pydantic default cannot depend on the data dir. | `port: int \| None = None`, None meaning the data dir's default (`default_mcp_port`, the same rule as `keychain_service`). `/mcp/status` reports the port in use. | Task 1's lazy-import test reads 17532 from a test data dir's status. | 1 |
| P7 | §3.8, §6: the Claude Code command is built by the frontend's `mcpCommand.ts` (Vitest) | The 08a brief lists "the Claude Code command text" among the Settings API. | The backend builds both commands (`McpToken.command`, `replace_command`; pytest). 08b shows them and drops `mcpCommand.ts`. | `test_the_token_is_kept_and_regenerate_replaces_it`. | 1 |
| P8 | R-08-3: the token in the Keychain | Tests, scratch sidecars and the packaged probe must never touch the Keychain. A new binary's read can open an "allow" dialog. | `DBAGUY_MCP_TOKEN` keeps the token in memory, and a container built with an in-memory secret store (every test) gets an in-memory token store. Only a real run uses `KeyringTokenStore`. | Task 9's frozen probe runs with it. | 1 |
| P9 | §3.5, A13: column types | With `describe=False`, SQL Server's types are coarse, and SQLite has none at all (its types come from the rows it peeks at, which are off). | Accept. `type` is null where the engine knows types only from values. Documented as a known limitation. | Measured: SQL Server `bigint` and `bit` → `int`, `datetime2` → `unknown`; SQLite all null. PG, MySQL and Couchbase are exact. | 6, 8 |
| P10 | §3.6: the memory bound stops at the response budget | With one 100 MB value per row, the budget fills only after 36 previews of 1,000 characters each. The call reads 37 rows (3.7 GB over the wire, 6.6 s). | Add `READ_RAW_CHARS = 64 Mi` characters of values per call: past it, no more pages (`read_capped`, with a note). | Measured: 100 MB values went from 37 rows in 6.6 s to 1 row in 0.9 s. The traced peak is unchanged at 200 MB (two rows: the page and the peek, as §3.6 says). The 1 MB case is unchanged (36 rows, 0.07 s). | 6, 8 |
| P11 | D-08-5: the per-statement read-only check | The check's SELECT empties what a diagnostic statement reports: SHOW WARNINGS came back empty, and the existing `test_a_set_var_hint_cannot_lift_read_only` failed. It also costs a round trip per statement, because the pure driver does not parse session-state tracking (A14). | Skip SHOW WARNINGS/ERRORS/COUNT and GET DIAGNOSTICS: they write nothing. Accept the round trip, documented as a known limitation for read-only MySQL tabs. | mysql-connector 26.7.0 `protocol.parse_ok` stops at the info text. Task 7. | 7 |
| P12 | §3.5 steps 5–7: the worker's `timeout_s` | The worker's timer covers `run()` and the first page only. Later pages and the frame are not covered. | The reader owns the call's deadline (lock, session, frame, every page), and the command carries no `timeout_s`. At the deadline it cancels, cancels again after 1.5 s (SQL Server's forced cancel), then closes the session after 10 s more. A statement the server itself stopped (PG 57014, which the adapter reports as cancelled) reads as `timeout`. | `test_postgres_statement_timeout_reaches_the_cursor_and_the_deadline_stops_the_rest`, `test_timeouts_and_production_connections`. | 6 |
| P13 | §3.1's module list | Layout only. | `services/agent_gate.agent_read`, with its tables, sits beside `safety.py` (whose rule it uses) instead of a `SafetyPolicy` method. `agent/queries.py` (`QueryTools`) sits beside `tools.py`, and `agent/sessions.py` (`AgentSessions`) beside `reader.py`. | Smaller files, one job each. | 4, 6, 8 |
| P14 | §3.3 / R-08-9: `needs_unlock` from `known_secrets` | `ProfileService.list_entries()` and `known_secrets()` read the Keychain for every saved profile, so an agent's every `list_connections` would make Keychain reads. After an update, each read can show "allow access" in the user's face. | `ProfileService.list_profiles()` looks up no secret. `needs_unlock` is false for a profile that saves its password, without reading. | `test_an_agents_calls_never_read_a_saved_secret` counts 0 reads. | 1 |
| P15 | CR-08-3: `sql` at most 20,000 characters | The cap is the tool's argument schema, so the SDK itself refuses a longer `sql`. The refusal is plain text, and nothing reaches History. | Accept: History never stores more than 20,000 characters. | `test_schema_access_cannot_query_and_off_is_unknown`. | 8 |
| P16 | §3.3: login privileges per profile | SQL Server's `db_owner` and `HAS_PERMS_BY_NAME` are per database. | Read on the profile's own database, once per profile. Documented. | Task 3. | 3 |
| P17 | §10 A1, A2, A6: checked with the real Claude Code in Task 1 | That spends model use (a few cents) from the user's account. | Task 1 and Task 8 cover both protocol versions with the SDK's own `Client`. Task 9 Step 4 runs `claude -p … --mcp-config <temp> --strict-mcp-config` only with the coordinator's go-ahead; otherwise A1, A2 and A6 go to the user's test list. | — | 9 |
| P18 | D-08-9: `json_response=True` | At 2026-07-28 the SDK's JSON-response path runs the tool with no disconnect watcher. A 2026 client cancels by closing its request's stream, so its cancel never reaches the tool: `SELECT pg_sleep(29)` kept running after the client gave up, until the server stopped. Only the SSE path (`json_response=False`) watches for the disconnect. At 2025-11-25 the client sends `notifications/cancelled`, which works either way. | `json_response=False`. At 2026-07-28 a call that ends within the SDK's 15 s still answers `application/json`; a longer one becomes an SSE stream with pings. At 2025-11-25 every answer is an SSE stream, which every MCP client reads. | Probed with the client's session kept open, as Claude Code keeps it on Esc. With `json_response=True`: 2025-11-25 stopped in 0.27 s, 2026-07-28 was still running 1.8 s later. With `json_response=False`: both stopped in 0.27 s. The planning test first used a filter that never matched the cursor's `FETCH`, so it passed without testing anything. `test_a_client_that_cancels_stops_the_statement` now asserts that the statement was running before the client gave up, and that it then stopped while the session stayed open. | 1, 8 |

## Probes run while planning

Every probe ran against the dev containers, all five up. Scratch objects were `zz_08_*` only, removed by exact name. Afterwards the Appendix's `leftovers.py` found no `zz_` relation, function, routine, user, role or database on PG, MySQL or SQL Server, no PG advisory lock, `app.orders` at 500 with no `zz_08` key, and no advisor session.

| Probe | Result |
|---|---|
| The SDK's Streamable HTTP with `mcp.Client` | Legacy (2025-11-25) and modern (2026-07-28) both list and call tools. The client name arrives in both eras. The SDK's own Host and Origin checks answer 421 and 403. |
| MySQL FILE through a role (A15) | P2. |
| MySQL `SET_VAR(sql_mode)` hint | The statement's own strings keep the session's reading (P5). |
| PG `SET LOCAL statement_timeout` in the cursor path (A7) | 57014 after 1 s, which the adapter reports as cancelled (P12). |
| SQLite authorizer (A9) | A refusal while preparing reads "not authorized…"; one while running reads "authorization denied" (`SQLITE_AUTH`). `load_extension` arrives as `SQLITE_FUNCTION` with `arg2 = 'load_extension'`. EXPLAIN QUERY PLAN and the catalog work under it. |
| SQL Server SHOWPLAN_TEXT with the adapter's state reads (A8) | It works. The state read under SHOWPLAN keeps the cached state, the plan is the second result set, and the session is normal after OFF. |
| A client's cancel (A4) | The client gives up with `move_on_after(1.5)` and keeps its session. The statement (a cursor `FETCH` waiting in `PgSleep`) is gone 0.27 s later in both protocol versions, but only with `json_response=False` (P18). |
| The pure driver's session tracking (A14) | Not parsed (P11). |
| The switch's cost | In-process: 0.29–0.35 s, +27 MB. Frozen: 0.26–0.32 s, +22 MB (0.64–0.86 s on the first run after a build). |
| PyInstaller with the SDK (A5) | 26 s. `dist/dbaguy-backend` is 63,156 KB. 95 `.so`/`.dylib` files pass `codesign --verify --strict`. No new warning. |


## Review Focus

1. **Nothing an agent sends writes, or acts beyond reading, wherever it hides.**
   - Hiding places: stacked statements (`;`, T-SQL with no semicolon, a GO or DELIMITER line), read-classified statements that are not queries, `U&"…"` names with `UESCAPE`, quoted, qualified or commented-apart calls, SQL run from text, MySQL's `sql_mode` and INTO, executable comments.
   - The gate refuses each of these before anything connects.
   - With the gate taken out, PG, MySQL, SQLite and Couchbase refuse the write themselves, and SQL Server's rollback undoes it.
   - Pinned by:
     - Task 4: `test_agent_gate.py`, 141 cases.
     - Task 6: `test_postgres_rows_more_rows_and_the_server_refuses_writes`, `test_mysql_refuses_writes_cleans_its_sql_mode_and_releases_locks`, `test_sqlite_refuses_writes_attach_and_vacuum_into_and_leaves_no_file`, `test_sql_server_rolls_back_what_the_gate_would_have_stopped`, `test_couchbase_refuses_writes_with_readonly`.
     - Task 8: `test_refused_statements_are_recorded_merged_and_never_connect`.
2. **No call leaves anything behind for the next call or for the user's tabs.**
   - Nothing carries over: no open transaction, no session lock, no read-write session default, no changed `sql_mode`, no SHOWPLAN.
   - A frame that cannot end closes its session.
   - A user's read-only MySQL tab cannot be made writable by a function either.
   - Pinned by:
     - Task 6: `test_postgres_stays_read_only_after_a_function_turns_it_off_and_its_locks_are_released`, `test_mysql_refuses_writes_cleans_its_sql_mode_and_releases_locks`.
     - Task 7: `test_a_read_only_tab_stays_read_only_after_a_function_switches_it`, `test_an_agent_session_whose_sql_mode_changed_refuses_and_is_reset`, and the existing `test_a_set_var_hint_cannot_lift_read_only` (SHOW WARNINGS still answers).
     - Task 8: `test_sql_server_explain_is_showplan_and_showplan_ends_with_it`.
3. **A big or slow result costs a page of memory and the call's timeout, never more.**
   - Huge values, wide rows and a thousand rows of a megabyte each stop at the response budget, max_rows or 64 Mi characters read.
   - The timeout covers the lock, the session and every page.
   - A client's cancel stops the statement on the server.
   - Pinned by:
     - Task 6: `test_huge_values_are_read_a_page_at_a_time_and_stop_at_the_response_budget`, `test_postgres_statement_timeout_reaches_the_cursor_and_the_deadline_stops_the_rest`, `test_a_second_call_waits_for_the_first_within_its_deadline`, `test_mcp_shape.py`.
     - Task 8: `test_timeouts_and_production_connections`, `test_a_client_that_cancels_stops_the_statement` (both protocols, the session kept open; P18).
4. **The user's settings act at once, and the user is never asked anything by an agent.**
   - With the switch off there is no SDK and no port.
   - A switch left on starts the server once the app is up.
   - A connection set to Off is gone on the next call, and its sessions close.
   - A regenerated token refuses the old one on its next request.
   - A wrong Host gets 421, any Origin 403.
   - A locked connection says so, and no dialog or Keychain prompt appears: `needs_unlock` reads no Keychain item.
   - Pinned by:
     - Task 1: `test_the_sdk_is_never_imported_while_the_switch_is_off`, `test_a_switch_left_on_starts_the_server_once_the_app_is_up`, `test_a_regenerated_token_refuses_the_old_one_at_once`, `test_the_gate_answers_401_421_and_403_before_the_sdk`.
     - Task 8: `test_a_connection_switched_off_is_gone_at_once_with_its_sessions`, `test_a_locked_connection_says_so_and_asks_for_nothing`.
5. **History and the log tell the truth, and say no more than they may.**
   - Every refused, failed, timed-out or locked call is recorded with its client.
   - Refusals within a minute merge.
   - Agents' records have their own cap.
   - A lone surrogate still gets recorded, as U+FFFD.
   - The INFO log has no SQL, value, server text or token.
   - Pinned by:
     - Task 5: `test_each_source_keeps_its_own_newest_records`, `test_a_refusal_sent_again_within_a_minute_adds_to_its_record`, `test_a_file_from_before_08_gets_the_new_columns_in_place`.
     - Task 8: `test_text_a_driver_cannot_send_is_refused_and_still_recorded`, `test_the_log_carries_no_sql_no_server_text_and_no_token`, `test_run_query_over_both_protocols_and_its_history`.

---

## File Structure

```
backend/
  pyproject.toml, poetry.lock          mcp>=2.3,<3 (Task 1, by `poetry add`)
  dbaguy-backend.spec                  the SDK collected for PyInstaller (Task 9)
  dbaguy_backend/
    tokens.py (new)                    same_token (moved from api/deps.py), new_token
    listen.py (new)                    listen_socket (moved from __main__.py): the API's and the MCP server's
    __main__.py, api/deps.py           use the two above
    config.py                          Settings.mcp_token (DBAGUY_MCP_TOKEN: memory only, P8)
    container.py                       default_mcp_port, token store, the agent services, closing them
    app.py, api/mcp.py (new)           GET /mcp/status, GET /mcp/token, POST /mcp/token/regenerate,
                                       GET /mcp/connections/{id}/login
    api/settings.py                    PUT /settings applies AppSettings.mcp
    api/profiles.py                    a saved or deleted profile forgets its agents' state
    api/history.py                     ?source=app|mcp
    domain/agent.py (new)              McpState, McpStatus, McpToken, ToolErrorCode, LoginGrant, LoginPrivileges
    domain/profiles.py                 McpAccess, _ProfileCommon.mcp_access = "off"
    domain/settings.py                 McpSettings, AppSettings.mcp
    domain/history.py                  HistoryStatus "refused", HistorySource, HistoryRecord.source/client/repeats
    agent/ (new)
      __init__.py
      host.py                          AgentHost: start/stop/restart, state, token, Claude Code commands
      server.py                        the only SDK import: MCPServer, the 7 tools, BearerGate, client_name, run_tool
      token_store.py                   TokenStore, KeyringTokenStore (account mcp:token), InMemoryTokenStore
      tools.py                         ToolFailure, answered(), AgentTools: connections and the schema tools
      login.py                         LoginRegistry: each profile's LoginPrivileges, read once
      sessions.py                      AgentSessions: one agent session per (profile, database), 8 at most, leases
      reader.py                        AgentReader (frame, pages, discard, rollback, end), CallControl, ReadRequest/Outcome
      shape.py                         cap_cell, ResultBudget (pages and the response budget), note
      queries.py                       QueryTools: run_query, explain, sample_fields, their History and log lines
    services/agent_gate.py (new)       agent_read(): the gate; ALLOWED_STARTS, DENIED_FUNCTIONS, SERVER_READ_ONLY
    services/metadata_service.py       key (profile, database, agent); children/detail/with_connection(agent=)
    services/schema_cache.py           get/search(agent=): a snapshot built on the agents' connection when needed
    services/field_sample_service.py   sample(agent=)
    services/connection_service.py     open(agent=): a read-only copy of the profile
    services/history_service.py        the columns and their upgrade, refused() and its merging, pruning per source
    sessions/manager.py                create_agent, hold/release (also the copy's source lease), close_agents
    sessions/hooks.py                  build_agent: the gate as the backstop, describe off, no read-ahead
    sessions/worker.py                 WorkerHooks.describe
    sql/dialects.py                    EXPLAIN_HEADS, ReadFrame, read_frame, explain_head, SERVER_TIMEOUT_MARGIN_S
    adapters/base.py                   open(…, agent=False); DatabaseConnection.login_privileges()
    adapters/{postgres,mssql}/adapter.py   login_privileges(); agent= in open
    adapters/mysql/catalog.py          file_granted, login_privileges (SHOW GRANTS, P2)
    adapters/mysql/read_only.py (new)  LEXER_UNSAFE_MODES, lexer_safe, make_agent_sql_mode, ReadOnlyGuard
    adapters/mysql/adapter.py          agent sql_mode at open; ReadOnlyGuard before every statement outside a transaction
    adapters/sqlite/adapter.py         an agent's query_only and authorizer
    adapters/couchbase/adapter.py      agent= in open (readonly=true is the read-only copy's)
    logging_setup.py                   the `mcp` logger at WARNING
  tests/
    agent_support.py (new)             free_port, make_container, share, serving, call_tool, list_tools, the fixture
    test_mcp_host.py, test_mcp_catalog.py, test_mcp_login.py, test_agent_gate.py, test_mcp_shape.py,
    test_mcp_reader.py, test_mysql_read_only.py, test_mcp_queries.py (new); test_history.py;
    test_core_utils.py, test_session_worker.py, test_ssh_tunnel_lifecycle.py (fakes take agent=)
src/
  lib/api/{openapi.json, schema.d.ts}  generated
  features/connections/ConnectionDialog.tsx   a new connection's draft says mcp_access "off"
  stores/settings.ts                   DEFAULT_SETTINGS.mcp
  features/history/HistoryPanel.tsx    the `refused` status (08b adds the MCP mark, the filter, Copy only)
CLAUDE.md, the spec's 〔08a〕 notes, docs/superpowers/notes/…-08a-mcp-server-backend-followups.md   (Task 9)
```

Task order follows the dependencies:
1. The switch, the endpoint and the token.
2. The schema tools on their own connection.
3. The login's privileges.
4. The gate, pure.
5. History.
6. Agent sessions, frames and the reader (the read path).
7. MySQL's read-only guard.
8. The query tools.
9. Packaging, measurements and the documents.

Each task compiles and passes its tests on its own.

---

### Task 1: The switch, the endpoint and the token — `AgentHost`, `BearerGate`, `list_connections`, the Settings API

**Suggested implementer:** Opus. The endpoint's security (token, Host, Origin) and a server's lifecycle on its own thread are easy to get subtly wrong.

**Files:**
- Create:
  - `backend/dbaguy_backend/{tokens.py, listen.py}`
  - `backend/dbaguy_backend/domain/agent.py`
  - `backend/dbaguy_backend/agent/{__init__.py, host.py, server.py, token_store.py, tools.py}`
  - `backend/dbaguy_backend/api/mcp.py`
  - `backend/tests/{agent_support.py, test_mcp_host.py}`
- Modify:
  - `backend/pyproject.toml`, `backend/poetry.lock` (by `poetry add`)
  - `backend/dbaguy_backend/{__main__.py, app.py, config.py, container.py, logging_setup.py}`
  - `backend/dbaguy_backend/api/{deps.py, settings.py}`
  - `backend/dbaguy_backend/domain/{profiles.py, settings.py}`
  - `backend/dbaguy_backend/services/profile_service.py`
  - `src/features/connections/ConnectionDialog.tsx`, `src/stores/settings.ts`
  - `src/lib/api/{openapi.json, schema.d.ts}` (by `gen:api`)
- Test: `backend/tests/test_mcp_host.py`

**Interfaces:**
- Consumes: `ProfileService.get / resolve_secrets`, `AdapterRegistry.get`, `SettingsService.get`, `keychain_service()`.
- Produces (later tasks rely on these exact names):
  - `ProfileService.list_profiles() -> list[AnyProfile]`: no secret looked up.
  - `domain/agent.py`:
    - `APP_MCP_PORT`, `DEV_MCP_PORT`
    - `McpState`, `McpStatus{state, port, url, message, shared_schema, shared_query}`
    - `McpToken{token, url, command, replace_command}`
    - `ToolErrorCode` (§4's codes plus `invalid_argument` and `internal_error`)
  - `domain/profiles.McpAccess = Literal["off", "schema", "query"]` and `_ProfileCommon.mcp_access`.
  - `domain/settings.McpSettings{enabled, port: int | None}` and `AppSettings.mcp`.
  - `tokens.same_token(provided, expected) -> bool` and `tokens.new_token() -> str`.
  - `listen.listen_socket(host, port) -> socket.socket`.
  - `agent/token_store.py`: `TokenStore` (`get() -> str | None`, `set(value)`), `KeyringTokenStore(service)`, `InMemoryTokenStore(value=None)`, `TOKEN_ACCOUNT = "mcp:token"`.
  - `agent/tools.py`:
    - `ToolFailure(code, message, *, logged=False)`
    - `Shared(profile, adapter)`
    - `AgentTools(profiles, registry)`, with `.shared(connection, tool, need="schema") -> Shared`, `.list_connections(*, client) -> dict` and `.shared_counts() -> (schema, query)`.
    - Task 2 adds `metadata` and `schema_cache` to the constructor, Task 3 adds `login`.
  - `agent/server.py`:
    - `client_name(ctx) -> str | None`
    - `run_tool(tool, client, call) -> CallToolResult` (Task 8 adds `control`)
    - `failure_result(ToolFailure)`
    - `build_server(tools)` and `build_app(tools, *, port, token)` (Task 8 adds `queries`)
    - `BearerGate`, `INSTRUCTIONS`
  - `agent/host.py`:
    - `AgentHost(tools, tokens, default_port, *, on_stop)`, with `.apply(McpSettings)`, `.start_later(McpSettings)`, `.stop()`, `.status() -> McpStatus`, `.token() -> McpToken`, `.regenerate() -> McpToken`, `.current_token() -> str`, `.port_for(settings) -> int`.
    - Task 8 adds `queries` after `tools`.
    - `mcp_url(port)`, `claude_commands(url, token) -> (add, replace)`.
  - `container.py`:
    - `is_app_data_dir`, `default_mcp_port(data_dir)`.
    - `AppContainer(…, token_store=None)`, with `.agent_tools`, `.agent_host` and `._close_agents()`. Later tasks add lines to `_close_agents()`.
  - `tests/agent_support.py`: `TOKEN`, `MCP_TOKEN`, `AUTH`, `free_port()`, `make_container(data_dir)`, `wait_for_state(container, state)`, `serving(container) -> port`, `call_tool(port, tool, args, *, mode, token, client) -> (is_error, data)`, `list_tools(port, *, mode)`, and the `container` fixture. Task 2 adds `share(container, profile, access, **changes) -> id`.

**Decisions this task carries:**
- R-08-1, D-08-8: the SDK is imported in `AgentHost._serve_once`, never at module level outside `agent/server.py`. A new `MCPServer` is built each start. The second uvicorn runs on the starting thread, with `timeout_graceful_shutdown=2`.
  - `_Uvicorn.startup` sets `listening`.
  - `apply` stops and waits (at most 5 s), and starts in the background (`starting`).
  - `start_later` waits `START_DELAY_S` (1 s) after the app is up.
- R-08-2, P6: `McpSettings.port` None means the data dir's default: 17531 for the app's own, 17532 for any other. It is the same rule as `keychain_service`, now `is_app_data_dir()`.
- R-08-3, P8: the token is read once from its store and created when there is none. Only a real run uses the Keychain (`app.dbaguy`/`app.dbaguy.dev`, account `mcp:token`). `DBAGUY_MCP_TOKEN`, or a container built with an in-memory secret store (every test), keeps it in memory.
- P18: the SDK's app takes `json_response=False`, not D-08-9's `True`.
  - Only the SDK's SSE path notices a 2026-07-28 client closing its request's stream, and closing the stream is how such a client cancels a call (A4).
  - At 2026-07-28, a call that ends within the SDK's 15 s still answers `application/json`. At 2025-11-25 every answer is an SSE stream.
  - Task 8's `test_a_client_that_cancels_stops_the_statement` pins it in both protocol versions.
- R-08-4: `BearerGate` is outermost: 401 (`WWW-Authenticate: Bearer`), then 421 for a Host other than `127.0.0.1:<port>` / `localhost:<port>`, then 403 for any Origin. The SDK gets the same allow-lists and the 1 MiB body limit (D-08-9).
- D-08-6: every tool returns one compact JSON `TextContent`, with no `structuredContent`. A failure is `isError` with `{"error": {code, message}}`. An unexpected exception is logged by its type only.
- D-08-7: the client's name comes from `ctx.session.client_params.client_info.name` in both protocol eras: the SDK synthesises `client_params` from 2026-07-28's `_meta` (checked while planning). It is cut to 80 characters, with control characters removed and lone surrogates made U+FFFD.
- P7: the backend builds the Claude Code commands (`McpToken.command`, `replace_command`). 08b shows them, and its `mcpCommand.ts` is not needed.
- P14: an agent's calls never read a saved secret: `AgentTools` lists profiles with the new `ProfileService.list_profiles()` (`list_entries()` looks every profile's passwords up for `has_password`, a Keychain read each), and `needs_unlock` is false for a profile that saves its password without looking (`test_an_agents_calls_never_read_a_saved_secret` counts the reads: 0). A Keychain read could make the Keychain ask, in the user's face, because an agent called.
- The frontend's two constructed objects need the new required fields (`openapi-typescript` makes a defaulted field required here): a new connection's draft has `mcp_access: "off"`, and `DEFAULT_SETTINGS.mcp` is `{enabled: false, port: null}`. `toDraft` spreads the stored profile, so editing a connection keeps its access.

- [ ] **Step 1: Add the dependency**

```bash
cd backend && poetry add "mcp>=2.3,<3"
```

Expected: `pyproject.toml` gains `"mcp (>=2.3,<3)"`. In `poetry.lock`, `mcp`/`mcp-types` 2.3.0 and their dependencies are added, and `cffi`/`cryptography` lose their `sys_platform == "linux"` marker (they were the keyring's on Linux only). `backend/.venv` has `mcp` 2.3.0.

- [ ] **Step 2: Write the tests**

**New file `backend/tests/agent_support.py`:**

```python
"""Shared helpers for the MCP server's tests (spec ⑧): a free test port, an MCP client against a real port, a container
whose MCP token never touches the Keychain."""

import json
import random
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from dbaguy_backend.agent.token_store import InMemoryTokenStore
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.settings import McpSettings
from dbaguy_backend.storage.secret_store import InMemorySecretStore

TOKEN = "test-token"
MCP_TOKEN = "mcp-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def free_port() -> int:
    """A port in the settings' range (1024–49151) that nothing listens on now. Never 17531 or 17532."""
    for _ in range(200):
        port = random.randint(20_000, 40_000)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port")


def make_container(data_dir: Path) -> AppContainer:
    settings = Settings(token=TOKEN, data_dir=data_dir, log_to_file=False)
    return AppContainer(settings, secret_store=InMemorySecretStore(), token_store=InMemoryTokenStore(MCP_TOKEN))


def wait_for_state(container: AppContainer, state: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while container.agent_host.status().state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"MCP state is {container.agent_host.status().state}, not {state}")
        time.sleep(0.02)


@contextmanager
def serving(container: AppContainer) -> Iterator[int]:
    """The container's MCP server listening on a free port, stopped afterwards."""
    port = free_port()
    container.agent_host.apply(McpSettings(enabled=True, port=port))
    try:
        wait_for_state(container, "listening")
        yield port
    finally:
        container.agent_host.stop()


async def _call(port: int, token: str, mode: str, tool: str | None, arguments: dict[str, Any],
                client: str) -> Any:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import Implementation

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)
        async with Client(transport, mode=mode, client_info=Implementation(name=client, version="1")) as session:
            if tool is None:
                return await session.list_tools()
            return await session.call_tool(tool, arguments)


def call_tool(port: int, tool: str, arguments: dict[str, Any] | None = None, *, mode: str = "legacy",
              token: str = MCP_TOKEN, client: str = "pytest") -> tuple[bool, Any]:
    """(is_error, the JSON of its one text block): one tool call through a new MCP client session."""
    result = anyio.run(_call, port, token, mode, tool, arguments or {}, client)
    assert len(result.content) == 1 and result.content[0].type == "text"
    return result.is_error, json.loads(result.content[0].text)


def list_tools(port: int, *, mode: str = "legacy") -> list[Any]:
    return anyio.run(_call, port, MCP_TOKEN, mode, None, {}, "pytest").tools


@pytest.fixture
def container(tmp_path):
    built = make_container(tmp_path / "data")
    try:
        yield built
    finally:
        built.close()
```

**New file `backend/tests/test_mcp_host.py`:**

```python
"""The MCP server's lifecycle, its HTTP gate and the Settings API (spec ⑧ D-08-8, R-08-3, R-08-4, D-08-12; A2, A3)."""

import json
import socket
import subprocess
import sys
import textwrap
import threading
import time

import httpx
from fastapi.testclient import TestClient

from dbaguy_backend.agent.host import claude_commands
from dbaguy_backend.agent.token_store import InMemoryTokenStore
from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.profiles import ProfileUpsertRequest
from dbaguy_backend.domain.settings import McpSettings
from dbaguy_backend.storage.secret_store import InMemorySecretStore

from .agent_support import (
    AUTH, MCP_TOKEN, TOKEN, call_tool, container, free_port, list_tools, make_container, serving, wait_for_state,
)

__all__ = ["container"]  # the fixture


def _add_profile(client: TestClient, name: str, access: str, path: str = "/tmp/zz_08_none.db") -> str:
    body = {"profile": {"engine": "sqlite", "name": name, "path": path, "mcp_access": access}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def test_the_sdk_is_never_imported_while_the_switch_is_off(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        from fastapi.testclient import TestClient
        from dbaguy_backend.app import create_app
        from dbaguy_backend.config import Settings
        from dbaguy_backend.container import AppContainer
        from dbaguy_backend.storage.secret_store import InMemorySecretStore
        settings = Settings(token="t", data_dir={str(tmp_path / "data")!r}, log_to_file=False)
        container = AppContainer(settings, secret_store=InMemorySecretStore())
        with TestClient(create_app(settings, container), base_url="http://127.0.0.1") as client:
            status = client.get("/mcp/status", headers={{"Authorization": "Bearer t"}}).json()
        assert status["state"] == "off" and status["port"] == 17532, status
        loaded = sorted(name for name in sys.modules if name == "mcp" or name.startswith(("mcp.", "mcp_types")))
        assert not loaded, loaded
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "ok", done.stderr[-2000:]


def test_a_switch_left_on_starts_the_server_once_the_app_is_up(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    (data / "settings.json").write_text(json.dumps({"mcp": {"enabled": True, "port": port}}))
    built = make_container(data)
    with TestClient(create_app(built.settings, built), base_url="http://127.0.0.1"):
        assert built.agent_host.status().state == "off"  # not before the API answers (D-08-8)
        wait_for_state(built, "listening")
        assert call_tool(port, "list_connections")[0] is False
    assert built.agent_host.status().state == "off"  # the app's end stops it


def test_the_token_is_kept_and_regenerate_replaces_it(container):
    first = container.agent_host.token()
    assert first.token == MCP_TOKEN and first.url == "http://127.0.0.1:17532/mcp"
    assert first.command == (
        'claude mcp add --transport http --scope user dbaguy http://127.0.0.1:17532/mcp '
        f'--header "Authorization: Bearer {MCP_TOKEN}"')
    assert first.replace_command == f"claude mcp remove --scope user dbaguy && {first.command}"
    second = container.agent_host.regenerate()
    assert second.token != MCP_TOKEN and len(second.token) == 43
    assert container.agent_host.token().token == second.token


def test_claude_code_gets_the_header_after_the_url():
    add, replace = claude_commands("http://127.0.0.1:17531/mcp", "abc")
    assert add.index("http://127.0.0.1:17531/mcp") < add.index("--header")
    assert replace.startswith("claude mcp remove --scope user dbaguy && claude mcp add")


def test_starting_and_stopping_again_and_again_leaves_no_thread_and_frees_the_port(container):
    port = free_port()
    threads = threading.active_count()
    for _ in range(5):
        container.agent_host.apply(McpSettings(enabled=True, port=port))
        wait_for_state(container, "listening")
        container.agent_host.apply(McpSettings(enabled=False, port=port))
        assert container.agent_host.status().state == "off"
    time.sleep(0.2)
    assert threading.active_count() <= threads + 1  # anyio's worker threads may linger idle for a moment
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # nothing holds it any more


def test_a_port_another_program_holds_is_reported_and_taken_once_free(container):
    port = free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        container.agent_host.apply(McpSettings(enabled=True, port=port))
        wait_for_state(container, "port_in_use")
        assert f"Port {port} is in use" in container.agent_host.status().message
    finally:
        holder.close()
    container.agent_host.apply(McpSettings(enabled=True, port=port))  # applying again retries
    wait_for_state(container, "listening")
    container.agent_host.stop()


def test_the_gate_answers_401_421_and_403_before_the_sdk(container):
    with serving(container) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        good = {"Authorization": f"Bearer {MCP_TOKEN}", "Accept": "application/json, text/event-stream"}
        no_token = httpx.post(url, json=body, headers={"Accept": good["Accept"]})
        assert no_token.status_code == 401 and no_token.headers["www-authenticate"] == "Bearer"
        assert httpx.post(url, json=body, headers={**good, "Authorization": "Bearer nope"}).status_code == 401
        assert httpx.post(url, json=body, headers={**good, "Host": f"evil.example:{port}"}).status_code == 421
        assert httpx.post(url, json=body, headers={**good, "Origin": f"http://localhost:{port}"}).status_code == 403
        oversized = httpx.post(url, content=b"x" * (1024 * 1024 + 1),
                               headers={**good, "Content-Type": "application/json"})
        assert oversized.status_code == 413


def test_both_protocol_versions_list_and_call_tools_and_see_only_shared_connections(container, tmp_path):
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        shared = _add_profile(client, "zz_08 shared", "query")
        _add_profile(client, "zz_08 schema", "schema")
        _add_profile(client, "zz_08 hidden", "off")
        with serving(container) as port:
            for mode in ("legacy", "2026-07-28"):
                tools = list_tools(port, mode=mode)
                assert "list_connections" in {tool.name for tool in tools}
                assert all(tool.annotations.read_only_hint for tool in tools)
                assert all(tool.description.startswith("Read-only.") for tool in tools)
                error, data = call_tool(port, "list_connections", mode=mode)
                assert not error
                names = {c["name"]: c for c in data["connections"]}
                assert set(names) == {"zz_08 shared", "zz_08 schema"}
                assert names["zz_08 shared"]["id"] == shared and names["zz_08 shared"]["access"] == "query"
                assert names["zz_08 shared"]["read_only_by"] == "server"
                assert names["zz_08 shared"]["needs_unlock"] is False


class _CountingSecrets(InMemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get(self, profile_id, kind):
        self.reads += 1
        return super().get(profile_id, kind)


def test_an_agents_calls_never_read_a_saved_secret(tmp_path):
    secrets = _CountingSecrets()
    built = AppContainer(Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False), secret_store=secrets,
                         token_store=InMemoryTokenStore(MCP_TOKEN))
    try:
        for name in ("zz_08 a", "zz_08 b"):
            built.profiles.create(ProfileUpsertRequest.model_validate({
                "profile": {"engine": "postgres", "name": name, "host": "127.0.0.1", "mcp_access": "query"},
                "secrets": {"password": "x"}}))
        secrets.reads = 0
        listed = built.agent_tools.list_connections()["connections"]
        assert [c["needs_unlock"] for c in listed] == [False, False]
        assert built.agent_tools.shared("zz_08 a", "list_objects").profile.name == "zz_08 a"
        assert secrets.reads == 0  # P14: an agent's list never makes the Keychain ask
    finally:
        built.close()


def test_a_regenerated_token_refuses_the_old_one_at_once(container):
    with serving(container) as port:
        assert call_tool(port, "list_connections")[0] is False
        new = container.agent_host.regenerate().token
        refused = httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, headers={"Authorization": f"Bearer {MCP_TOKEN}"})
        assert refused.status_code == 401
        assert call_tool(port, "list_connections", token=new)[0] is False


def test_the_settings_switch_starts_and_stops_the_server(container):
    port = free_port()
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        assert client.get("/mcp/status", headers=AUTH).json()["state"] == "off"
        settings = client.get("/settings", headers=AUTH).json()
        assert settings["mcp"] == {"enabled": False, "port": None}
        settings["mcp"] = {"enabled": True, "port": port}
        assert client.put("/settings", json=settings, headers=AUTH).status_code == 200
        wait_for_state(container, "listening")
        status = client.get("/mcp/status", headers=AUTH).json()
        assert status["port"] == port and status["url"] == f"http://127.0.0.1:{port}/mcp"
        settings["mcp"] = {"enabled": False, "port": port}
        client.put("/settings", json=settings, headers=AUTH)
        assert client.get("/mcp/status", headers=AUTH).json()["state"] == "off"
        for path in ("/mcp/status", "/mcp/token"):
            assert client.get(path).status_code == 401  # the API token guards them too
```

- [ ] **Step 3: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_host.py`
Expected: collection error, `ModuleNotFoundError: No module named 'dbaguy_backend.agent'`.

- [ ] **Step 4: Write the code**

**New file `backend/dbaguy_backend/tokens.py`:**

```python
"""Bearer tokens: the API's (DBAGUY_TOKEN) and the MCP server's (spec ⑧ R-08-3), compared the same way."""

import secrets


def same_token(provided: str, expected: str) -> bool:
    # compare_digest raises TypeError on non-ASCII str operands; bytes never raise for any input.
    return secrets.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def new_token() -> str:
    """43 URL-safe characters (32 random bytes)."""
    return secrets.token_urlsafe(32)
```

**New file `backend/dbaguy_backend/listen.py`:**

```python
"""The listening sockets: the API's (a random port) and the MCP server's (a fixed one, spec ⑧ R-08-2)."""

import socket


def listen_socket(host: str, port: int) -> socket.socket:
    """A socket bound to (host, port) and listening; OSError (EADDRINUSE: another program holds the port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock
```

**New file `backend/dbaguy_backend/domain/agent.py`:**

```python
"""DbaGuy's MCP server (spec ⑧): what the Settings API answers and the codes a tool's error carries (§4)."""

from typing import Literal

from pydantic import BaseModel

from .profiles import McpAccess

__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "APP_MCP_PORT", "DEV_MCP_PORT"]

APP_MCP_PORT = 17531  # the app's own data dir (R-08-2)
DEV_MCP_PORT = 17532  # any other data dir: dev/data, tests, a scratch sidecar

# off: the switch is off; starting: importing the SDK and binding; listening: serving; port_in_use: another program
# holds the port; error: anything else (the message says what)
McpState = Literal["off", "starting", "listening", "port_in_use", "error"]

# §4: every tool error is a result with one of these codes, never an exception the SDK logs (D-08-6)
ToolErrorCode = Literal[
    "unknown_connection", "ambiguous", "not_allowed", "refused", "session_changed", "password_required",
    "connection_failed", "query_failed", "timeout", "cancelled", "busy", "not_found", "invalid_argument",
    "internal_error",
]


class McpStatus(BaseModel):
    state: McpState
    port: int  # the port it uses or would use
    url: str  # what an MCP client connects to: http://127.0.0.1:<port>/mcp
    message: str | None = None  # why it is not listening (port_in_use, error)
    shared_schema: int = 0  # connections whose access is `schema`
    shared_query: int = 0  # connections whose access is `query`


class McpToken(BaseModel):
    """The bearer token MCP clients send, and the Claude Code commands that register it (§3.8). Never logged."""

    token: str
    url: str
    command: str  # claude mcp add … (user scope)
    replace_command: str  # after Regenerate: remove the old registration first
```

**New file `backend/dbaguy_backend/agent/__init__.py`:**

```python
"""DbaGuy's MCP server (spec ⑧): read-only access for AI tools to the connections the user shares. Not named `mcp`, the
SDK's top-level package. Only `server.py` imports the SDK, and only when the switch is on."""
```

**New file `backend/dbaguy_backend/agent/token_store.py`:**

```python
"""Where the MCP server's bearer token is kept (spec ⑧ R-08-3): the Keychain, beside the saved passwords (same service,
account `mcp:token`), or memory (tests, and a sidecar given DBAGUY_MCP_TOKEN, which never touches the Keychain)."""

import threading
from typing import Protocol

import keyring
from keyring.errors import KeyringError

from ..domain.errors import SecretStoreError

TOKEN_ACCOUNT = "mcp:token"


class TokenStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...


class KeyringTokenStore:
    """The token as a generic password of `service` (the data dir's Keychain service, `keychain_service`). The keyring
    backend is the one KeyringSecretStore set for the process."""

    def __init__(self, service: str) -> None:
        self.service = service

    def get(self) -> str | None:
        try:
            return keyring.get_password(self.service, TOKEN_ACCOUNT)
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain read failed: {exc}") from exc

    def set(self, value: str) -> None:
        try:
            keyring.set_password(self.service, TOKEN_ACCOUNT, value)
        except KeyringError as exc:
            raise SecretStoreError(f"Keychain write failed: {exc}") from exc


class InMemoryTokenStore:
    def __init__(self, value: str | None = None) -> None:
        self._value = value
        self._lock = threading.Lock()

    def get(self) -> str | None:
        with self._lock:
            return self._value

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value
```

**New file `backend/dbaguy_backend/agent/tools.py`:**

```python
"""The MCP tools' logic (spec ⑧ §3.5), without the SDK: each method returns a JSON-able dict or raises ToolFailure.
`agent/server.py` turns both into a CallToolResult (D-08-6). Every call reads the profiles again, so a connection the
user set to Off is gone from the next call on (R-08-5)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.agent import McpAccess, ToolErrorCode
from ..domain.errors import PasswordRequired, ProfileNotFound
from ..domain.execution import QueryContext
from ..domain.profiles import AnyProfile, Engine, ServerProfile
from ..services.profile_service import ProfileService

_UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
_AMBIGUOUS = "Several connections are named '{}': pass one of their ids ({})"
_SCHEMA_ONLY = "'{}' allows schema only: {} needs 'Read-only queries' in DbaGuy's connection settings"


class ToolFailure(Exception):
    """A tool's error result (§4): its code and a message for the agent. `logged`: whoever raised it wrote its log line
    already (a refusal names its reason), so the server does not add a second."""

    def __init__(self, code: ToolErrorCode, message: str, *, logged: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.logged = logged


@dataclass(frozen=True)
class Shared:
    """A connection shared with AI tools, as this call read it."""

    profile: AnyProfile
    adapter: DatabaseAdapter


def _database(profile: AnyProfile) -> str | None:
    return profile.database if isinstance(profile, ServerProfile) else None


class AgentTools:
    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
        self._profiles = profiles
        self._registry = registry

    # ---- connections ----

    def _shared_profiles(self) -> list[AnyProfile]:
        return [profile for profile in self._profiles.list_profiles() if profile.mcp_access != "off"]

    def shared(self, connection: str, tool: str, need: McpAccess = "schema") -> Shared:
        """The connection `connection` names (a profile's id, or its name: exact, then ignoring case), when it is shared
        at `need` or more. Off, missing: unknown_connection; a name several have: ambiguous; `schema` when the tool
        needs `query`: not_allowed."""
        profile = self._find(connection.strip())
        if need == "query" and profile.mcp_access != "query":
            raise ToolFailure("not_allowed", _SCHEMA_ONLY.format(profile.name, tool))
        return Shared(profile, self._registry.get(profile.engine))

    def _find(self, connection: str) -> AnyProfile:
        try:
            profile = self._profiles.get(UUID(connection))
        except (ValueError, ProfileNotFound):
            profile = None
        if profile is not None:
            if profile.mcp_access == "off":
                raise ToolFailure("unknown_connection", _UNKNOWN.format(connection))
            return profile
        shared = self._shared_profiles()
        named = [p for p in shared if p.name == connection] or [
            p for p in shared if p.name.casefold() == connection.casefold()]
        if not named:
            raise ToolFailure("unknown_connection", _UNKNOWN.format(connection))
        if len(named) > 1:
            raise ToolFailure("ambiguous", _AMBIGUOUS.format(connection, ", ".join(str(p.id) for p in named)))
        return named[0]

    def shared_counts(self) -> tuple[int, int]:
        """How many connections are shared at `schema` and at `query` (the Settings status line)."""
        shared = self._shared_profiles()
        return sum(p.mcp_access == "schema" for p in shared), sum(p.mcp_access == "query" for p in shared)

    def list_connections(self, *, client: str | None = None) -> dict[str, Any]:
        """Every connection whose access is not Off (§3.3), without connecting."""
        connections = [self._describe(profile) for profile in self._shared_profiles()]
        logger.info("MCP list_connections by {}: {} connections", client, len(connections))
        return {"connections": connections}

    def _describe(self, profile: AnyProfile) -> dict[str, Any]:
        adapter = self._registry.get(profile.engine)
        entry: dict[str, Any] = {
            "id": str(profile.id), "name": profile.name, "engine": profile.engine, "access": profile.mcp_access,
            "folder": profile.folder or None, "color": profile.color, "database": _database(profile),
            "read_only_by": "server" if adapter.capabilities.server_read_only else "dbaguy",
            "needs_unlock": self._needs_unlock(profile),
        }
        if profile.engine == Engine.COUCHBASE:
            try:
                context = QueryContext.parse(_database(profile))
            except ValueError:
                context = None
            entry["default_query_context"] = context.text() if context is not None else None
        return entry

    def _needs_unlock(self, profile: AnyProfile) -> bool:
        """A password it would ask for: a profile that does not save its password and has none unlocked this run. One
        that saves it says False without reading the Keychain (an agent's list must never make the Keychain ask)."""
        if profile.save_password:
            return False
        try:
            self._profiles.resolve_secrets(profile)  # its secrets live in memory only: no Keychain read
        except PasswordRequired:
            return True
        return False
```

**New file `backend/dbaguy_backend/agent/server.py`:**

```python
"""The MCP endpoint (spec ⑧ §3.2): a new MCPServer with DbaGuy's tools, its Streamable HTTP app (D-08-9), and the bearer
gate in front of it. The only module that imports the MCP SDK, and only `AgentHost` imports it, when the switch is on
(D-08-8): with the switch off the SDK is never loaded (Q1)."""

import json
import re
import time
from collections.abc import Callable
from typing import Any

import anyio
from loguru import logger
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..domain.well_formed import well_formed
from ..tokens import same_token
from ..version import app_version
from .tools import AgentTools, ToolFailure

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024  # R-08-4
MAX_SESSIONS = 32  # 2025-11-25 sessions (D-08-9)
SESSION_IDLE_S = 1800
CLIENT_NAME_CHARS = 80  # D-08-7
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
INSTRUCTIONS = (
    "DbaGuy shares some of the user's database connections, read-only. Start with list_connections (it also shows "
    "what each login is allowed to do). Explore with list_objects (pass back the `path` it returns), search_objects "
    "and describe. run_query and explain need the connection's 'Read-only queries' access; each call runs exactly one "
    "SELECT/WITH/VALUES/TABLE/SHOW statement, inside a read-only transaction that is rolled back. Results are capped "
    "(max_rows, max_cell_chars, about 40,000 characters, timeout up to 60 s). SQL Server connections are read-only by "
    "DbaGuy's checks only. Couchbase takes a query_context (bucket.scope)."
)
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class BearerGate:
    """Outermost (R-08-4, §3.2): the bearer token (401 with `WWW-Authenticate: Bearer`), then the Host (421: only
    127.0.0.1:<port> and localhost:<port>), then any Origin (403: CLI clients send none, a web page always does). A
    refused request never reaches the SDK. `token` is read per request, so a regenerated token applies at once."""

    def __init__(self, app: ASGIApp, *, token: Callable[[], str], port: int) -> None:
        self._app = app
        self._token = token
        self._hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # lifespan
            return
        headers = Headers(scope=scope)
        refusal: Response | None = None
        if not same_token(headers.get("authorization", ""), f"Bearer {self._token()}"):
            refusal = Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        elif headers.get("host") not in self._hosts:
            refusal = Response("Invalid Host header", status_code=421)
        elif "origin" in headers:
            refusal = Response("Origin not allowed", status_code=403)
        if refusal is None:
            await self._app(scope, receive, send)
            return
        logger.info("MCP request refused: {}", refusal.status_code)  # never a header's value: one may be a token
        await refusal(scope, receive, send)


def client_name(ctx: Context) -> str | None:
    """The name the client gave for itself (D-08-7): `initialize`'s clientInfo (2025-11-25) or the request's `_meta`
    (2026-07-28); self-reported, never an identity. At most 80 characters, without control characters."""
    try:
        params = ctx.session.client_params
    except Exception:  # outside a request
        return None
    name = params.client_info.name if params is not None else None
    if not name:
        return None
    return well_formed(_CONTROL.sub("", name))[:CLIENT_NAME_CHARS] or None


def _text(data: dict[str, Any], *, error: bool = False) -> CallToolResult:
    """One compact JSON text, no structured content (D-08-6: the data would be sent twice)."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(content=[TextContent(type="text", text=body)], is_error=error)


def failure_result(failure: ToolFailure) -> CallToolResult:
    return _text({"error": {"code": failure.code, "message": failure.message}}, error=True)


async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, Any]]) -> CallToolResult:
    """`call` on a worker thread; its data or its failure as a result, never an exception the SDK would log with its
    message (D-08-6). An unexpected error is logged by its type only above DEBUG."""
    started = time.monotonic()
    try:
        return _text(await anyio.to_thread.run_sync(call))
    except ToolFailure as failure:
        if not failure.logged:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.info("MCP {} by {}: {} ({} ms)", tool, client, failure.code, elapsed)
        return failure_result(failure)
    except Exception as exc:
        logger.error("MCP {} by {} failed unexpectedly ({})", tool, client, type(exc).__name__)
        logger.opt(exception=exc).debug("MCP {} failed", tool)
        return failure_result(ToolFailure("internal_error", "Internal error"))


def build_server(tools: AgentTools) -> MCPServer:
    """A new server each start: the SDK's session manager runs once per instance (D-08-8)."""
    server = MCPServer("dbaguy", title="DbaGuy", instructions=INSTRUCTIONS, version=app_version())

    async def list_connections(ctx: Context) -> CallToolResult:
        client = client_name(ctx)
        return await run_tool("list_connections", client, lambda: tools.list_connections(client=client))

    server.add_tool(
        list_connections, name="list_connections", annotations=_READ_ONLY, structured_output=False,
        description=(
            "Read-only. The database connections the user shares with AI tools in DbaGuy: id, name, engine, access "
            "('schema': names and definitions only; 'query': also run_query and explain), the default database, "
            "whether the server or only DbaGuy enforces read-only, whether it needs unlocking in DbaGuy first, and "
            "what the login may do beyond reading (null until a call has used the connection)."
        ),
    )
    return server


def build_app(tools: AgentTools, *, port: int, token: Callable[[], str]) -> ASGIApp:
    """The ASGI app the MCP thread serves on 127.0.0.1:`port`."""
    security = TransportSecuritySettings(  # the SDK checks again; BearerGate has refused anything else already
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"], allowed_origins=[],
    )
    # json_response=False (P18): only the SDK's SSE path notices a 2026-07-28 client closing its request's stream,
    # which is how that client cancels a call (A4). A call that ends within the SDK's 15 s still answers JSON there;
    # 2025-11-25 answers as an SSE stream and cancels with notifications/cancelled.
    app = build_server(tools).streamable_http_app(
        streamable_http_path=MCP_PATH, json_response=False, stateless_http=False, max_sessions=MAX_SESSIONS,
        session_idle_timeout=SESSION_IDLE_S, max_request_body_size=MAX_BODY_BYTES, transport_security=security,
    )
    return BearerGate(app, token=token, port=port)
```

**New file `backend/dbaguy_backend/agent/host.py`:**

```python
"""The MCP server's lifecycle (spec ⑧ D-08-8): the Settings switch starts and stops a second uvicorn server on its own
thread, event loop and socket (127.0.0.1, a fixed port, R-08-2), beside the API's. Starting imports the SDK (lazily:
with the switch off nothing of it is loaded, Q1), reads the token, binds, builds a new MCPServer and serves; stopping
ends it within 5 s and closes every agent session and agent catalog connection."""

import errno
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import uvicorn
from loguru import logger

from ..domain.agent import McpState, McpStatus, McpToken
from ..domain.settings import McpSettings
from ..listen import listen_socket
from ..tokens import new_token
from .token_store import TokenStore
from .tools import AgentTools

MCP_HOST = "127.0.0.1"
START_DELAY_S = 1.0  # at app start with the switch on: the API answers its first requests first (D-08-8, Q1)
STOP_JOIN_S = 5.0
GRACEFUL_S = 2  # uvicorn's graceful shutdown: open requests get this long before they are cut
_PORT_IN_USE = "Port {} is in use by another program: choose another port in Settings"


def mcp_url(port: int) -> str:
    return f"http://{MCP_HOST}:{port}/mcp"


def claude_commands(url: str, token: str) -> tuple[str, str]:
    """Claude Code's commands (§3.8): register DbaGuy in the user scope, and the same after a Regenerate, which removes
    the old registration first. `--header` is variadic, so it comes after the URL."""
    add = f'claude mcp add --transport http --scope user dbaguy {url} --header "Authorization: Bearer {token}"'
    return add, f"claude mcp remove --scope user dbaguy && {add}"


class _Uvicorn(uvicorn.Server):
    """Says when it is serving: `started` is set inside startup, on the server's own loop."""

    def __init__(self, config: uvicorn.Config, on_started: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_started = on_started

    async def startup(self, sockets=None) -> None:  # noqa: ANN001 (uvicorn's signature)
        await super().startup(sockets=sockets)
        if self.started:
            self._on_started()


@dataclass(eq=False)
class _Run:
    generation: int
    port: int
    thread: threading.Thread | None = None
    server: _Uvicorn | None = None
    stopping: bool = False
    stopped: threading.Event = field(default_factory=threading.Event)


class AgentHost:
    def __init__(self, tools: AgentTools, tokens: TokenStore, default_port: int, *,
                 on_stop: Callable[[], None] = lambda: None) -> None:
        self._tools = tools
        self._tokens = tokens
        self._default_port = default_port
        self._on_stop = on_stop  # closes the agent sessions and catalog connections
        self._apply_lock = threading.Lock()  # one start, stop or restart at a time
        self._lock = threading.Lock()  # the state below
        self._generations = itertools.count(1)
        self._run: _Run | None = None
        self._state: McpState = "off"
        self._port = default_port
        self._message: str | None = None
        self._token: str | None = None
        self._timer: threading.Timer | None = None

    # ---- the token ----

    def current_token(self) -> str:
        """The token, read once from its store; created and stored the first time there is none."""
        with self._lock:
            if self._token is None:
                stored = self._tokens.get()
                if not stored:
                    stored = new_token()
                    self._tokens.set(stored)
                    logger.info("MCP token created")
                self._token = stored
            return self._token

    def token(self) -> McpToken:
        return self._token_answer(self.current_token())

    def regenerate(self) -> McpToken:
        """A new token, at once: the old one's next request is 401."""
        token = new_token()
        self._tokens.set(token)
        with self._lock:
            self._token = token
        logger.info("MCP token regenerated")
        return self._token_answer(token)

    def _token_answer(self, token: str) -> McpToken:
        url = mcp_url(self._wanted_port())
        command, replace = claude_commands(url, token)
        return McpToken(token=token, url=url, command=command, replace_command=replace)

    # ---- the lifecycle ----

    def port_for(self, settings: McpSettings) -> int:
        return settings.port or self._default_port

    def _wanted_port(self) -> int:
        with self._lock:
            return self._port

    def status(self) -> McpStatus:
        schema, query = self._tools.shared_counts()
        with self._lock:
            return McpStatus(state=self._state, port=self._port, url=mcp_url(self._port), message=self._message,
                             shared_schema=schema, shared_query=query)

    def start_later(self, settings: McpSettings) -> None:
        """At app start: when the switch is on, start a moment after the API is up (D-08-8)."""
        with self._lock:
            self._port = self.port_for(settings)
        if not settings.enabled:
            return
        timer = threading.Timer(START_DELAY_S, self.apply, args=(settings,))
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()

    def apply(self, settings: McpSettings) -> None:
        """The switch or the port changed (PUT /settings): start, stop or restart. Starting happens in the background
        (`starting` until it serves); stopping waits for the server (at most 5 s)."""
        port = self.port_for(settings)
        with self._apply_lock:
            with self._lock:
                run, state = self._run, self._state
            if run is not None and settings.enabled and run.port == port and state in ("starting", "listening"):
                return
            if run is not None:
                self._stop_run(run)
            if not settings.enabled:
                self._set("off", port)
                return
            self._start(port)

    def stop(self) -> None:
        """The app is closing: stop and wait (at most 5 s)."""
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        with self._apply_lock:
            with self._lock:
                run = self._run
            if run is not None:
                self._stop_run(run)
                self._set("off", run.port)

    def _set(self, state: McpState, port: int, message: str | None = None, run: _Run | None = None) -> None:
        """Change the state; with `run`, only while that run is still the current one."""
        with self._lock:
            if run is not None and self._run is not run:
                return
            self._state, self._port, self._message = state, port, message

    def _start(self, port: int) -> None:
        run = _Run(next(self._generations), port)
        with self._lock:
            self._run = run
        self._set("starting", port)
        run.thread = threading.Thread(target=self._serve, args=(run,), name=f"mcp-server-{run.generation}",
                                      daemon=True)
        run.thread.start()

    def _serve(self, run: _Run) -> None:
        try:
            self._serve_once(run)
        finally:
            run.stopped.set()

    def _serve_once(self, run: _Run) -> None:
        try:
            from . import server as mcp_server  # the SDK: ≈0.2–0.5 s and +23 MB, only now (D-08-8)

            logging.getLogger("mcp").setLevel(logging.WARNING)  # D-08-9: its INFO lines quote what clients send
            self.current_token()  # the Keychain read happens here, not on the first request
            sock = listen_socket(MCP_HOST, run.port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.warning("MCP server: port {} is in use", run.port)
                self._set("port_in_use", run.port, _PORT_IN_USE.format(run.port), run)
            else:
                logger.error("MCP server could not bind port {} ({})", run.port, type(exc).__name__)
                self._set("error", run.port, f"Could not listen on port {run.port}: {exc.strerror or exc}", run)
            return
        except Exception as exc:
            logger.opt(exception=exc).error("MCP server could not start")
            self._set("error", run.port, f"Could not start: {type(exc).__name__}", run)
            return
        try:
            app = mcp_server.build_app(self._tools, port=run.port, token=self.current_token)
            config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on",
                                    timeout_graceful_shutdown=GRACEFUL_S)
            server = _Uvicorn(config, on_started=lambda: self._listening(run))
            with self._lock:
                if run.stopping:
                    return
                run.server = server
            server.run(sockets=[sock])
            if not server.started and not run.stopping:
                self._set("error", run.port, "The MCP server stopped while starting", run)
        except Exception as exc:
            logger.opt(exception=exc).error("MCP server failed")
            self._set("error", run.port, f"The MCP server failed: {type(exc).__name__}", run)
        finally:
            sock.close()
        logger.info("MCP server on port {} stopped", run.port)

    def _listening(self, run: _Run) -> None:
        logger.info("MCP server listening on {}:{}", MCP_HOST, run.port)
        self._set("listening", run.port, None, run)

    def _stop_run(self, run: _Run) -> None:
        """Ask the run's server to exit and wait for its thread; then close what agents held. The caller holds
        `_apply_lock`."""
        with self._lock:
            run.stopping = True
            server = run.server
        if server is not None:
            server.should_exit = True
        if not run.stopped.wait(STOP_JOIN_S):
            logger.warning("MCP server on port {} did not stop within {:g} s", run.port, STOP_JOIN_S)
        with self._lock:
            if self._run is run:
                self._run = None
        try:
            self._on_stop()
        except Exception:
            logger.exception("Closing the agents' connections failed")
```

**New file `backend/dbaguy_backend/api/mcp.py`:**

```python
"""The MCP server's Settings API (spec ⑧ D-08-12): its state, the token and its Claude Code commands, a new token. The
switch and the port are AppSettings.mcp (`PUT /settings`)."""

from fastapi import APIRouter, Depends

from ..container import AppContainer
from ..domain.agent import McpStatus, McpToken
from .deps import get_container, require_token

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[Depends(require_token)])


@router.get("/status", response_model=McpStatus)
def mcp_status(container: AppContainer = Depends(get_container)) -> McpStatus:
    return container.agent_host.status()


@router.get("/token", response_model=McpToken)
def mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
    return container.agent_host.token()


@router.post("/token/regenerate", response_model=McpToken)
def regenerate_mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
    return container.agent_host.regenerate()
```

**Patch T1** (the files this task modifies: `backend/dbaguy_backend/__main__.py`, `backend/dbaguy_backend/api/deps.py`, `backend/dbaguy_backend/api/settings.py`, `backend/dbaguy_backend/app.py`, `backend/dbaguy_backend/config.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/domain/profiles.py`, `backend/dbaguy_backend/domain/settings.py`, `backend/dbaguy_backend/logging_setup.py`, `backend/dbaguy_backend/services/profile_service.py`, `src/features/connections/ConnectionDialog.tsx`, `src/stores/settings.ts`). Save the block in your scratchpad as `08a-t1.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t1.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/__main__.py b/backend/dbaguy_backend/__main__.py
index 4f8448e..7b4c293 100644
--- a/backend/dbaguy_backend/__main__.py
+++ b/backend/dbaguy_backend/__main__.py
@@ -1,6 +1,5 @@
 import json
 import os
-import socket
 import sys
 import threading
 from collections.abc import Callable
@@ -10,18 +9,10 @@ from loguru import logger
 
 from .app import create_app
 from .config import load_settings
+from .listen import listen_socket
 from .logging_setup import setup_logging
 
 
-def _bind(host: str, port: int) -> socket.socket:
-    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
-    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
-    sock.bind((host, port))
-    sock.listen(128)
-    sock.set_inheritable(True)
-    return sock
-
-
 # How long a graceful shutdown may take before the process exits anyway. The shell waits longer than this before it
 # kills the backend (src-tauri/src/backend.rs), so the forced exit below still gets to close the tunnels.
 _SHUTDOWN_DEADLINE_S = 5.0
@@ -59,7 +50,7 @@ def main() -> None:
     settings = load_settings()
     setup_logging(settings.log_level, settings.log_dir if settings.log_to_file else None)
     app = create_app(settings)
-    sock = _bind(settings.host, settings.port)
+    sock = listen_socket(settings.host, settings.port)
     port = sock.getsockname()[1]
     server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
     if settings.watch_stdin:
diff --git a/backend/dbaguy_backend/api/deps.py b/backend/dbaguy_backend/api/deps.py
index 456d750..2ab35c2 100644
--- a/backend/dbaguy_backend/api/deps.py
+++ b/backend/dbaguy_backend/api/deps.py
@@ -1,29 +1,20 @@
-import secrets
-
 from fastapi import Header, Request, WebSocket
 
 from ..container import AppContainer
 from ..domain.errors import Unauthorized
+from ..tokens import same_token
 
 
 def get_container(request: Request) -> AppContainer:
     return request.app.state.container
 
 
-def _same_token(provided: str, expected: str) -> bool:
-    # compare_digest raises TypeError on non-ASCII str operands; bytes never raise for any input.
-    return secrets.compare_digest(
-        provided.encode("utf-8", "surrogatepass"),
-        expected.encode("utf-8", "surrogatepass"),
-    )
-
-
 def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
     expected = f"Bearer {request.app.state.container.settings.token}"
-    if authorization is None or not _same_token(authorization, expected):
+    if authorization is None or not same_token(authorization, expected):
         raise Unauthorized("Missing or invalid token")
 
 
 def websocket_token_ok(websocket: WebSocket) -> bool:
     expected = websocket.app.state.container.settings.token
-    return _same_token(websocket.query_params.get("token", ""), expected)
+    return same_token(websocket.query_params.get("token", ""), expected)
diff --git a/backend/dbaguy_backend/api/settings.py b/backend/dbaguy_backend/api/settings.py
index f38d2aa..0d5f046 100644
--- a/backend/dbaguy_backend/api/settings.py
+++ b/backend/dbaguy_backend/api/settings.py
@@ -14,4 +14,6 @@ def get_settings(container: AppContainer = Depends(get_container)) -> AppSetting
 
 @router.put("", response_model=AppSettings)
 def update_settings(body: AppSettings, container: AppContainer = Depends(get_container)) -> AppSettings:
-    return container.app_settings.update(body)
+    saved = container.app_settings.update(body)
+    container.agent_host.apply(saved.mcp)  # the MCP switch or port changed: start, stop or restart (D-08-12)
+    return saved
diff --git a/backend/dbaguy_backend/app.py b/backend/dbaguy_backend/app.py
index 7e1f4e7..ca16080 100644
--- a/backend/dbaguy_backend/app.py
+++ b/backend/dbaguy_backend/app.py
@@ -6,7 +6,7 @@ from fastapi.middleware.cors import CORSMiddleware
 from fastapi.middleware.trustedhost import TrustedHostMiddleware
 from loguru import logger
 
-from .api import couchbase, data, edits, history, metadata, profiles, scripts, sessions, sql, system, workspace
+from .api import couchbase, data, edits, history, mcp, metadata, profiles, scripts, sessions, sql, system, workspace
 from .api import settings as settings_api  # `settings` is the parameter of create_app
 from .api.errors import register_error_handlers
 from .config import Settings, load_settings
@@ -57,6 +57,7 @@ def create_app(settings: Settings, container: AppContainer | None = None) -> Fas
     app.include_router(scripts.router)
     app.include_router(history.router)
     app.include_router(sql.router)
+    app.include_router(mcp.router)
     return app
 
 
diff --git a/backend/dbaguy_backend/config.py b/backend/dbaguy_backend/config.py
index 57b6962..2048b9c 100644
--- a/backend/dbaguy_backend/config.py
+++ b/backend/dbaguy_backend/config.py
@@ -31,6 +31,9 @@ class Settings(BaseSettings):
         "http://localhost:1420",
     ]
     watch_stdin: bool = False
+    # The MCP server's bearer token, kept in memory only (a scratch or frozen sidecar a probe runs): set, the Keychain
+    # is never read or written for it (spec ⑧ R-08-3 keeps it there otherwise)
+    mcp_token: str | None = Field(default=None, min_length=1)
 
 
 def load_settings() -> Settings:
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index 5265d81..8401478 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -11,8 +11,12 @@ from .adapters.mysql.adapter import MysqlAdapter
 from .adapters.postgres.adapter import PostgresAdapter
 from .adapters.registry import AdapterRegistry
 from .adapters.sqlite.adapter import SqliteAdapter
+from .agent.host import AgentHost
+from .agent.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
+from .agent.tools import AgentTools
 from .config import Settings, default_data_dir
 from .connectivity.ssh_tunnel import SshTunnelManager
+from .domain.agent import APP_MCP_PORT, DEV_MCP_PORT
 from .services.connection_service import ConnectionService
 from .services.couchbase_service import CouchbaseService
 from .services.data_service import DataService
@@ -48,11 +52,28 @@ def default_registry(freetds_conf: Path | None = None) -> AdapterRegistry:
     return AdapterRegistry([PostgresAdapter(), MysqlAdapter(), MssqlAdapter(), CouchbaseAdapter(), SqliteAdapter()])
 
 
+def is_app_data_dir(data_dir: Path) -> bool:
+    return data_dir.resolve() == default_data_dir().resolve()
+
+
 def keychain_service(data_dir: Path) -> str:
     """The Keychain service for passwords saved with this data dir. Only the app's own data dir uses the app's service;
     any other (scripts/dev.sh's dev/data, tests, ad-hoc sidecars) gets a separate one, so a dev run never reads,
     overwrites or leaves behind entries of the user's real connections."""
-    return APP_KEYCHAIN_SERVICE if data_dir.resolve() == default_data_dir().resolve() else DEV_KEYCHAIN_SERVICE
+    return APP_KEYCHAIN_SERVICE if is_app_data_dir(data_dir) else DEV_KEYCHAIN_SERVICE
+
+
+def default_mcp_port(data_dir: Path) -> int:
+    """The MCP server's port when the settings name none (R-08-2): the app's and any other data dir's never collide."""
+    return APP_MCP_PORT if is_app_data_dir(data_dir) else DEV_MCP_PORT
+
+
+def _token_store(settings: Settings, secret_store: SecretStore | None) -> TokenStore:
+    """In memory with DBAGUY_MCP_TOKEN, and whenever the secrets are not the Keychain's (tests pass an in-memory
+    store): only a real run keeps the token in the Keychain."""
+    if settings.mcp_token is not None or secret_store is not None:
+        return InMemoryTokenStore(settings.mcp_token)
+    return KeyringTokenStore(keychain_service(settings.data_dir))
 
 
 def _keychain_store(data_dir: Path) -> KeyringSecretStore:
@@ -65,7 +86,8 @@ class AppContainer:
     """Builds and owns every long-lived service."""
 
     def __init__(
-        self, settings: Settings, secret_store: SecretStore | None = None, registry: AdapterRegistry | None = None
+        self, settings: Settings, secret_store: SecretStore | None = None, registry: AdapterRegistry | None = None,
+        token_store: TokenStore | None = None,
     ) -> None:
         self.settings = settings
         data_dir = settings.data_dir
@@ -94,6 +116,11 @@ class AppContainer:
             self.transfers.plans,
         )
         self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
+        self.agent_tools = AgentTools(self.profiles, self.registry)
+        self.agent_host = AgentHost(
+            self.agent_tools, token_store or _token_store(settings, secret_store), default_mcp_port(data_dir),
+            on_stop=self._close_agents,
+        )
         self._stop = threading.Event()
         self._housekeeper: threading.Thread | None = None
 
@@ -105,6 +132,10 @@ class AppContainer:
             target=self._guarded, args=("Warming up the SQL classifier", warm_up), name="classifier-warm-up",
             daemon=True,
         ).start()
+        self._guarded("Starting the MCP server", lambda: self.agent_host.start_later(self.app_settings.get().mcp))
+
+    def _close_agents(self) -> None:
+        """The MCP server stopped: nothing an agent opened stays open."""
 
     def _housekeep(self) -> None:
         while not self._stop.wait(_HOUSEKEEPING_INTERVAL_S):
@@ -127,6 +158,7 @@ class AppContainer:
         """Stop everything in dependency order: sessions and metadata hold tunnels, so tunnels go last; sessions
         write to the history, so it closes after them."""
         self._stop.set()
+        self._guarded("Stopping the MCP server", self.agent_host.stop)
         self._guarded("Closing sessions", self.sessions.close_all)
         self._guarded("Closing metadata connections", self.metadata.close_all)
         self._guarded("Closing SSH tunnels", self.tunnels.close_all)
diff --git a/backend/dbaguy_backend/domain/profiles.py b/backend/dbaguy_backend/domain/profiles.py
index 85fc797..e323352 100644
--- a/backend/dbaguy_backend/domain/profiles.py
+++ b/backend/dbaguy_backend/domain/profiles.py
@@ -19,6 +19,9 @@ SslMode = Literal["disable", "prefer", "require", "verify-ca", "verify-full"]
 ProfileColor = Literal["none", "green", "yellow", "orange", "red", "purple"]
 SafetyLevel = Literal["relaxed", "standard", "strict"]
 SshAuth = Literal["agent_or_key", "password"]
+# What AI tools may do with the connection through DbaGuy's MCP server (spec ⑧ R-08-5): nothing (the default for every
+# connection, new or old), read its schema, or also run read-only queries
+McpAccess = Literal["off", "schema", "query"]
 
 
 class SslConfig(BaseModel):
@@ -50,6 +53,7 @@ class _ProfileCommon(BaseModel):
     read_only: bool = False
     safety: SafetyLevel = "standard"
     save_password: bool = True
+    mcp_access: McpAccess = "off"
 
 
 class ServerProfileDraft(_ProfileCommon):
diff --git a/backend/dbaguy_backend/domain/settings.py b/backend/dbaguy_backend/domain/settings.py
index 2d49d99..5b5fa61 100644
--- a/backend/dbaguy_backend/domain/settings.py
+++ b/backend/dbaguy_backend/domain/settings.py
@@ -45,6 +45,19 @@ class CouchbaseSettings(BaseModel):
     sample_size: int = Field(default=1000, ge=SAMPLE_SIZE_MIN, le=SAMPLE_SIZE_MAX)
 
 
+MCP_PORT_MIN, MCP_PORT_MAX = 1024, 49151  # below macOS's ephemeral ports (49152–65535), so no random port takes it
+
+
+class McpSettings(BaseModel):
+    """DbaGuy's MCP server (spec ⑧ D-08-12): off by default. `port` None is the data dir's default (R-08-2): 17531 for
+    the app's own data dir, 17532 for any other (dev, tests), so a dev backend and the app never take each other's."""
+
+    model_config = ConfigDict(extra="forbid")
+
+    enabled: bool = False
+    port: int | None = Field(default=None, ge=MCP_PORT_MIN, le=MCP_PORT_MAX)
+
+
 class AppSettings(BaseModel):
     """The app preferences (`settings.json`). Persisted, so a newer build's extra fields are tolerated on read and
     kept on write; input from the API stays strict."""
@@ -56,4 +69,5 @@ class AppSettings(BaseModel):
     safety: SafetySettings = SafetySettings()
     notifications: NotificationSettings = NotificationSettings()
     couchbase: CouchbaseSettings = CouchbaseSettings()
+    mcp: McpSettings = McpSettings()
     scripts_dir: str | None = None  # None = the default folder inside the data dir
diff --git a/backend/dbaguy_backend/logging_setup.py b/backend/dbaguy_backend/logging_setup.py
index e47dcd9..f94e773 100644
--- a/backend/dbaguy_backend/logging_setup.py
+++ b/backend/dbaguy_backend/logging_setup.py
@@ -49,3 +49,5 @@ def setup_logging(level: str, log_dir: Path | None) -> None:
         std_logger.propagate = False
     # mysql-connector logs its authentication plugin choice at INFO, three lines per connect.
     logging.getLogger("mysql.connector").setLevel(logging.WARNING)
+    # The MCP SDK (spec ⑧ D-08-9) logs what clients send (session ids, a tool's failure text) at INFO.
+    logging.getLogger("mcp").setLevel(logging.WARNING)
diff --git a/backend/dbaguy_backend/services/profile_service.py b/backend/dbaguy_backend/services/profile_service.py
index 391b419..5365371 100644
--- a/backend/dbaguy_backend/services/profile_service.py
+++ b/backend/dbaguy_backend/services/profile_service.py
@@ -31,6 +31,11 @@ class ProfileService:
     def list_entries(self) -> list[ProfileEntry]:
         return [self._entry(p) for p in self._store.list()]
 
+    def list_profiles(self) -> list[AnyProfile]:
+        """The profiles alone, no secret looked up: an MCP agent's every call reads them (spec ⑧), and a Keychain read
+        could make the Keychain ask the user."""
+        return self._store.list()
+
     def entry(self, profile_id: UUID) -> ProfileEntry:
         return self._entry(self._store.get(profile_id))
 
diff --git a/src/features/connections/ConnectionDialog.tsx b/src/features/connections/ConnectionDialog.tsx
index 7278b76..af1112e 100644
--- a/src/features/connections/ConnectionDialog.tsx
+++ b/src/features/connections/ConnectionDialog.tsx
@@ -116,7 +116,15 @@ const SAFETY_LEVELS: { value: SafetyLevel; label: string; explanation: string }[
 type Feedback = { ok: boolean; text: string } | null;
 
 function emptyDraft(engine: Engine): ProfileDraft {
-  const common = { name: "", folder: "", color: "none", read_only: false, safety: "standard", save_password: true } as const;
+  const common = {
+    name: "",
+    folder: "",
+    color: "none",
+    read_only: false,
+    safety: "standard",
+    save_password: true,
+    mcp_access: "off", // spec ⑧: a new connection is never shared with AI tools until the user says so
+  } as const;
   if (engine === "sqlite") return { ...common, engine, path: "" };
   return {
     ...common,
diff --git a/src/stores/settings.ts b/src/stores/settings.ts
index 93cc4fa..8b7f97e 100644
--- a/src/stores/settings.ts
+++ b/src/stores/settings.ts
@@ -14,6 +14,7 @@ export const DEFAULT_SETTINGS: AppSettings = {
   safety: { idle_transaction_warn_minutes: 3, idle_transaction_rollback_minutes: 0 },
   notifications: { long_query_seconds: 15 },
   couchbase: { sample_size: 1000 },
+  mcp: { enabled: false, port: null },
   scripts_dir: null,
 };
 
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_host.py tests/test_api.py tests/test_container.py tests/test_profiles.py tests/test_local_stores.py tests/test_app_smoke.py tests/test_logging.py`
Expected: all pass (planning: 413 passed in 12 s).

- [ ] **Step 6: Generate the API types; type-check; vitest**

Run: `npm run gen:api && npm run typecheck && npm test`
Expected: `openapi.json` +299 lines, `schema.d.ts` +226 lines (McpSettings, McpStatus, McpToken, the `/mcp/*` routes, `mcp_access`). `tsc` is clean, and vitest shows 906 passed (47 files), as before.

- [ ] **Step 7: Measure the switch's cost (record the numbers for Task 9)**

Run the in-process check in the Appendix, `start_cost.py`, three times from `backend/`.
Expected: start ≤ 1 s and RSS +≤ 30 MB (planning: 0.29–0.35 s, +27 MB).

- [ ] **Step 8: Review against the pain points** (Q1: nothing of the SDK while off; Q3: the gate and the token; Q2: the API's first requests are not delayed)

- [ ] **Step 9: Commit**

```bash
git add backend/pyproject.toml backend/poetry.lock backend/dbaguy_backend/tokens.py backend/dbaguy_backend/listen.py \
  backend/dbaguy_backend/__main__.py backend/dbaguy_backend/app.py backend/dbaguy_backend/config.py \
  backend/dbaguy_backend/container.py backend/dbaguy_backend/logging_setup.py backend/dbaguy_backend/api/deps.py \
  backend/dbaguy_backend/services/profile_service.py \
  backend/dbaguy_backend/api/settings.py backend/dbaguy_backend/api/mcp.py backend/dbaguy_backend/domain/agent.py \
  backend/dbaguy_backend/domain/profiles.py backend/dbaguy_backend/domain/settings.py backend/dbaguy_backend/agent \
  backend/tests/agent_support.py backend/tests/test_mcp_host.py src/features/connections/ConnectionDialog.tsx \
  src/stores/settings.ts src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(08a): the MCP switch, endpoint and token: AgentHost, BearerGate, list_connections, /mcp/status and /mcp/token

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 2: Schema tools on the agents' own read-only connection — `list_objects`, `search_objects`, `describe`

**Suggested implementer:** Sonnet. It reuses the metadata machinery, with one new key and three thin tools.

**Files:**
- Modify:
  - `backend/dbaguy_backend/adapters/base.py` and `adapters/{postgres,mysql,mssql,sqlite,couchbase}/adapter.py`: `open(…, *, agent=False)`.
  - `backend/dbaguy_backend/services/{connection_service.py, metadata_service.py, schema_cache.py}`.
  - `backend/dbaguy_backend/agent/{tools.py, server.py}`, `backend/dbaguy_backend/container.py`.
  - `backend/tests/agent_support.py`.
  - Three fakes that implement `open` take `agent=`: `backend/tests/{test_core_utils.py, test_session_worker.py, test_ssh_tunnel_lifecycle.py}`.
- Test: `backend/tests/test_mcp_catalog.py` (new)

**Interfaces:**
- Consumes: Task 1's `AgentTools`, `Shared`, `ToolFailure`, `run_tool`, `client_name`, `build_server`; `MetadataService`; `SchemaCache.search / get`; `DatabaseAdapter.relation_path`.
- Produces:
  - `DatabaseAdapter.open(profile, endpoint, password, database, *, agent=False)`: every adapter takes it. Task 6 gives SQLite and MySQL behaviour for it.
  - `ConnectionService.open(…, agent=False)`: with `agent`, the profile is a `read_only=True` copy (D-08-1, D-08-11).
  - `MetadataService`:
    - key `(profile_id, database, agent)`
    - `children(request, *, agent=False)`, `detail(request, *, agent=False)`
    - `with_connection(…, retry=True, agent=False)`; an agent's call never writes, and `agent=True` with `retry=False` is a `ValueError`
    - `close_all(*, agents_only=False)`
  - `SchemaCache.get(profile_id, database=None, *, agent=False)` and `.search(…, *, agent=False)`. The cache is shared; a missing snapshot is built on the agents' connection.
  - `agent/tools.py`:
    - `answered(profile)`, a context manager: `PasswordRequired` → `password_required` (R-08-9's text), `NotFound` → `not_found`, `ConnectionFailed` → `connection_failed`, `QueryFailed` and `StatementError` → `query_failed`, `ValidationFailed` → `invalid_argument`.
    - `LIST_LIMIT, SEARCH_LIMIT, LIMIT_MAX = 100, 50, 1000`.
    - `AgentTools(profiles, registry, metadata, schema_cache)` with `.list_objects(connection, path=None, limit, *, client)`, `.search_objects(connection, pattern, database=None, include_columns=False, limit, *, client)` and `.describe(connection, path=None, database=None, schema=None, name=None, include_ddl=False, *, client)`.
  - `tests/agent_support.share(container, profile, access="query", **changes) -> str`: saves the profile with the dev password, shared at `access`.

**Decisions this task carries:**
- D-08-11, CR-08-9:
  - Every schema tool runs on the agents' own metadata connection: a `read_only=True` copy, opened with `agent=True`, and closed after 300 s idle like the others.
  - The profile's own writable metadata connection is never used. (SQL Server's catalog changes the session's isolation, so the catalog and the agent's query session must be two connections anyway.)
  - Snapshots in the cache are shared: one built for completion serves an agent too.
- `describe` is put together as the spec says: `detail(path)`, plus the snapshot's PK/FK, plus the indexes (the `indexes` folder's nodes, or index nodes right under the object, as Couchbase lists them). The DDL comes only with `include_ddl`.
  - By name: the snapshot's relations (and Couchbase's keyspaces), exact match first, then ignoring case; one match, or `ambiguous` listing the candidates.
  - A name never reaches SQL here: the path it leads to goes through the catalog's own queries, bound or quoted. The test sends `x'; DROP TABLE public.zz_08_victim; --` four ways.
- A snapshot that cannot be built leaves `describe` without FKs, with a note. The detail stands.
- The schema cache passes `agent=True` only when it is set, so fakes in older tests that replace `with_connection` keep working.

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_mcp_catalog.py`:**

```python
"""The schema tools on the agents' own read-only metadata connection (spec ⑧ D-08-11, §3.5): list_objects,
search_objects, describe; access levels; names that reach no SQL. Seeds are only read; the one scratch table is
zz_08_victim, dropped by exact name."""

import sqlite3
from uuid import UUID

import psycopg
import pytest

from dbaguy_backend.adapters.base import RunOptions
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.metadata import PathSegment
from dbaguy_backend.domain.profiles import SqliteProfile

from .agent_support import call_tool, container, serving, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_couchbase_profile, make_mssql_profile, make_mysql_profile,
    make_pg_profile, requires_couchbase, requires_mssql, requires_mysql, requires_pg,
)

__all__ = ["container"]


def _segments(path: list[dict]) -> list[PathSegment]:
    return [PathSegment(**segment) for segment in path]


def _one_value(connection, sql: str):
    result = connection.run(sql, RunOptions(describe=False))
    try:
        return result.fetch(1)[0][0]
    finally:
        result.close()


@pytest.fixture
def sqlite_file(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id), note TEXT);
        CREATE INDEX child_parent_idx ON child (parent_id);
    """)
    conn.close()
    return path


def test_an_off_or_unknown_connection_is_not_there_and_schema_access_lists(container, sqlite_file):
    hidden = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000001", engine="sqlite", name="hidden",
                                            path=str(sqlite_file)), access="off")
    shared = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000002", engine="sqlite",
                                            name="zz_08 lite", path=str(sqlite_file)), access="schema")
    tools = container.agent_tools
    for name in (hidden, "hidden", "nope"):
        with pytest.raises(ToolFailure) as refused:
            tools.list_objects(name)
        assert refused.value.code == "unknown_connection"
    root = tools.list_objects("ZZ_08 LITE")  # a name, ignoring case
    assert [o["name"] for o in root["objects"]] == ["main"] and not root["truncated"]
    tables = tools.list_objects(shared, _segments(root["objects"][0]["path"]) + [PathSegment(kind="folder",
                                                                                             name="tables")])
    assert [o["name"] for o in tables["objects"]] == ["child", "parent"]
    assert tools.list_objects(shared, None, limit=1)["truncated"] is False


def test_sqlite_describe_has_keys_foreign_keys_and_indexes(container, sqlite_file):
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000003", engine="sqlite", name="lite",
                                         path=str(sqlite_file)), access="schema")
    described = container.agent_tools.describe(pid, name="CHILD")
    assert [c["name"] for c in described["columns"]] == ["id", "parent_id", "note"]
    assert described["primary_key"] == ["id"]
    assert described["foreign_keys"] == [{"name": described["foreign_keys"][0]["name"], "columns": ["parent_id"],
                                          "references": "main.parent", "ref_columns": ["id"]}]
    assert [i["name"] for i in described["indexes"]] == ["child_parent_idx"]
    assert "ddl" not in described
    assert "CREATE TABLE child" in container.agent_tools.describe(pid, name="child", include_ddl=True)["ddl"]
    hits = container.agent_tools.search_objects(pid, "parent", include_columns=True)["objects"]
    assert {(h["name"], tuple(h.get("columns", ()))) for h in hits} == {("parent", ()), ("child", ("parent_id",))}


@requires_pg
def test_postgres_schema_tools_run_on_a_read_only_connection_of_their_own(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    tools = container.agent_tools
    databases = {o["name"] for o in tools.list_objects(pid)["objects"]}
    assert {"dbaguy", "analytics"} <= databases
    orders = tools.describe(pid, schema="sales", name="orders")
    assert orders["primary_key"] == ["id"]
    assert orders["foreign_keys"][0]["references"] == "public.customers"
    assert "orders_customer_idx" in {i["name"] for i in orders["indexes"]}
    customers = tools.describe(pid, name="customers")
    assert customers["comment"] == "Customer master data"
    assert next(c for c in customers["columns"] if c["name"] == "email")["comment"] == "Login e-mail"
    hit = tools.search_objects(pid, "sales.ord")["objects"][0]
    assert (hit["schema"], hit["name"]) == ("sales", "orders")
    assert tools.describe(pid, _segments(hit["path"]))["name"] == "orders"
    # the agents' connection is read-only, and is not the profile's own metadata connection
    show = lambda c: _one_value(c, "SHOW transaction_read_only")  # noqa: E731
    assert container.metadata.with_connection(UUID(pid), "dbaguy", show, agent=True) == "on"
    assert container.metadata.with_connection(UUID(pid), "dbaguy", show) == "off"


@requires_pg
def test_names_the_agent_gives_reach_no_sql(container):
    owner = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                            autocommit=True)
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
        tools = container.agent_tools
        evil = "x'; DROP TABLE public.zz_08_victim; --"
        for call in (
            lambda: tools.describe(pid, name=evil),
            lambda: tools.describe(pid, schema=evil, name="customers"),
            lambda: tools.describe(pid, [PathSegment(kind="database", name="dbaguy"),
                                         PathSegment(kind="schema", name=evil),
                                         PathSegment(kind="folder", name="tables"),
                                         PathSegment(kind="table", name=evil)]),
            lambda: tools.list_objects(pid, [PathSegment(kind="database", name="dbaguy"),
                                             PathSegment(kind="schema", name=evil)]),
        ):
            with pytest.raises(ToolFailure) as failed:
                call()
            assert failed.value.code in ("not_found", "query_failed", "invalid_argument")
        assert tools.search_objects(pid, evil)["objects"] == []
        assert owner.execute("SELECT count(*) FROM public.zz_08_victim").fetchone() == (0,)
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_mysql
def test_mysql_describe_by_name(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
    orders = container.agent_tools.describe(pid, schema="dbaguy", name="orders")
    assert orders["primary_key"] == ["id"] and orders["foreign_keys"][0]["name"] == "orders_customer_fk"


@requires_mssql
def test_sql_server_describe_by_name(container):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"), access="schema")
    orders = container.agent_tools.describe(pid, schema="sales", name="orders")
    assert orders["foreign_keys"][0]["name"] == "orders_customer_fk"
    assert "orders_customer_idx" in {i["name"] for i in orders["indexes"]}


@requires_couchbase
def test_couchbase_collections_by_path_and_name_over_mcp(container):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"), access="schema")
    with serving(container) as port:
        error, buckets = call_tool(port, "list_objects", {"connection": pid})
        assert not error and "dbaguy" in {o["name"] for o in buckets["objects"]}
        error, users = call_tool(port, "describe", {"connection": "zz_08 cb", "database": "dbaguy", "schema": "app",
                                                    "name": "users"})
        assert not error and users["kind"] == "collection" and users["name"] == "users"
        error, missing = call_tool(port, "describe", {"connection": pid, "name": "zz_08_no_such"})
        assert error and missing["error"]["code"] == "not_found"
```

(`tests/agent_support.py` gets `share()`, and the three fakes get `agent=`: both are in Step 3's patch.)

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_catalog.py`
Expected: `ImportError` (`share` is not in `tests.agent_support`).

- [ ] **Step 3: Write the code**

**Patch T2** (the files this task modifies: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/couchbase/adapter.py`, `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/agent/server.py`, `backend/dbaguy_backend/agent/tools.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/services/connection_service.py`, `backend/dbaguy_backend/services/metadata_service.py`, `backend/dbaguy_backend/services/schema_cache.py`, `backend/tests/agent_support.py`, `backend/tests/test_core_utils.py`, `backend/tests/test_session_worker.py`, `backend/tests/test_ssh_tunnel_lifecycle.py`). Save the block in your scratchpad as `08a-t2.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t2.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/adapters/base.py b/backend/dbaguy_backend/adapters/base.py
index 173ef57..7ca7c37 100644
--- a/backend/dbaguy_backend/adapters/base.py
+++ b/backend/dbaguy_backend/adapters/base.py
@@ -411,8 +411,11 @@ class DatabaseAdapter(ABC):
         endpoint: Endpoint | None,
         password: str | None,
         database: str | None,
+        *,
+        agent: bool = False,
     ) -> DatabaseConnection:
-        """Open a connection. Raises ConnectionFailed."""
+        """Open a connection. Raises ConnectionFailed. `agent`: an MCP agent's connection (spec ⑧ D-08-4), always on a
+        read-only copy of the profile; an engine whose read-only session leaves a way to write locks it further."""
 
     def endpoint_target(self, profile: ServerProfile) -> tuple[str, int]:
         """Where a connection to `profile` goes (directly, or as the SSH tunnel's target): the profile's host and port,
diff --git a/backend/dbaguy_backend/adapters/couchbase/adapter.py b/backend/dbaguy_backend/adapters/couchbase/adapter.py
index 7404221..74909f4 100644
--- a/backend/dbaguy_backend/adapters/couchbase/adapter.py
+++ b/backend/dbaguy_backend/adapters/couchbase/adapter.py
@@ -502,7 +502,7 @@ class CouchbaseAdapter(DatabaseAdapter, DocumentEngine):
         return endpoint_target(profile)
 
     def open(self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None,
-             database: str | None) -> DatabaseConnection:
+             database: str | None, *, agent: bool = False) -> DatabaseConnection:
         """Spec §3.6.3: the candidate nodes in order (discovered or given), the first that answers. A refused, timed
         out or unknown node: the next one; a login or TLS failure: at once (another node would say the same). Runs on
         the worker's open path; discovery is bounded by its own deadline (R-04a-C19)."""
diff --git a/backend/dbaguy_backend/adapters/mssql/adapter.py b/backend/dbaguy_backend/adapters/mssql/adapter.py
index 179c5f1..4b2c774 100644
--- a/backend/dbaguy_backend/adapters/mssql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mssql/adapter.py
@@ -774,7 +774,8 @@ class MssqlAdapter(DatabaseAdapter):
         return parse_mssql_url(url)
 
     def open(
-        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
+        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
+        *, agent: bool = False,
     ) -> DatabaseConnection:
         if not isinstance(profile, ServerProfile) or endpoint is None:
             raise ValidationFailed("Not a SQL Server profile")
diff --git a/backend/dbaguy_backend/adapters/mysql/adapter.py b/backend/dbaguy_backend/adapters/mysql/adapter.py
index 9606560..0489242 100644
--- a/backend/dbaguy_backend/adapters/mysql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mysql/adapter.py
@@ -565,7 +565,8 @@ class MysqlAdapter(DatabaseAdapter):
         return parse_mysql_url(url)
 
     def open(
-        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
+        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
+        *, agent: bool = False,
     ) -> DatabaseConnection:
         if not isinstance(profile, ServerProfile) or endpoint is None:
             raise ValidationFailed("Not a MySQL server profile")
diff --git a/backend/dbaguy_backend/adapters/postgres/adapter.py b/backend/dbaguy_backend/adapters/postgres/adapter.py
index eb54e1c..329f6aa 100644
--- a/backend/dbaguy_backend/adapters/postgres/adapter.py
+++ b/backend/dbaguy_backend/adapters/postgres/adapter.py
@@ -667,7 +667,8 @@ class PostgresAdapter(DatabaseAdapter):
         return ParsedUrl(profile=draft, password=password)
 
     def open(
-        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
+        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
+        *, agent: bool = False,
     ) -> DatabaseConnection:
         if not isinstance(profile, ServerProfile) or endpoint is None:
             raise ValidationFailed("Not a PostgreSQL server profile")
diff --git a/backend/dbaguy_backend/adapters/sqlite/adapter.py b/backend/dbaguy_backend/adapters/sqlite/adapter.py
index e97cffc..79ee9a6 100644
--- a/backend/dbaguy_backend/adapters/sqlite/adapter.py
+++ b/backend/dbaguy_backend/adapters/sqlite/adapter.py
@@ -324,7 +324,8 @@ class SqliteAdapter(DatabaseAdapter):
         return ParsedUrl(profile=SqliteProfileDraft(engine="sqlite", name=name, path=path))
 
     def open(
-        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None
+        self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None, database: str | None,
+        *, agent: bool = False,
     ) -> DatabaseConnection:
         if not isinstance(profile, SqliteProfile):
             raise ValidationFailed("Not a SQLite profile")
diff --git a/backend/dbaguy_backend/agent/server.py b/backend/dbaguy_backend/agent/server.py
index 929b94b..7d31b35 100644
--- a/backend/dbaguy_backend/agent/server.py
+++ b/backend/dbaguy_backend/agent/server.py
@@ -6,21 +6,23 @@ import json
 import re
 import time
 from collections.abc import Callable
-from typing import Any
+from typing import Annotated, Any
 
 import anyio
 from loguru import logger
 from mcp.server.mcpserver import Context, MCPServer
 from mcp.server.transport_security import TransportSecuritySettings
 from mcp.types import CallToolResult, TextContent, ToolAnnotations
+from pydantic import Field
 from starlette.datastructures import Headers
 from starlette.responses import Response
 from starlette.types import ASGIApp, Receive, Scope, Send
 
+from ..domain.metadata import PathSegment
 from ..domain.well_formed import well_formed
 from ..tokens import same_token
 from ..version import app_version
-from .tools import AgentTools, ToolFailure
+from .tools import LIMIT_MAX, LIST_LIMIT, SEARCH_LIMIT, AgentTools, ToolFailure
 
 MCP_PATH = "/mcp"
 MAX_BODY_BYTES = 1024 * 1024  # R-08-4
@@ -37,6 +39,10 @@ INSTRUCTIONS = (
     "DbaGuy's checks only. Couchbase takes a query_context (bucket.scope)."
 )
 _READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
+Connection = Annotated[str, Field(min_length=1, max_length=200, description="A connection's id or name")]
+ObjectPath = Annotated[list[PathSegment], Field(max_length=12, description="A `path` list_objects or search_objects "
+                                                                           "returned, passed back as it is")]
+Name = Annotated[str, Field(min_length=1, max_length=1000)]
 
 
 class BearerGate:
@@ -125,6 +131,56 @@ def build_server(tools: AgentTools) -> MCPServer:
             "what the login may do beyond reading (null until a call has used the connection)."
         ),
     )
+    async def list_objects(
+        connection: Connection, ctx: Context, path: ObjectPath | None = None,
+        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = LIST_LIMIT,
+    ) -> CallToolResult:
+        client = client_name(ctx)
+        return await run_tool("list_objects", client,
+                              lambda: tools.list_objects(connection, path, limit, client=client))
+
+    server.add_tool(
+        list_objects, name="list_objects", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'schema' access or more. The objects one level under `path` (none: the top level: "
+            "databases, schemas or buckets): kind, name, `path` (pass it back to go deeper or to describe), whether it "
+            "has children, a short detail. Reads no rows."
+        ),
+    )
+
+    async def search_objects(
+        connection: Connection, pattern: Name, ctx: Context, database: Name | None = None,
+        include_columns: bool = False, limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = SEARCH_LIMIT,
+    ) -> CallToolResult:
+        client = client_name(ctx)
+        return await run_tool("search_objects", client, lambda: tools.search_objects(
+            connection, pattern, database, include_columns, limit, client=client))
+
+    server.add_tool(
+        search_objects, name="search_objects", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'schema' access or more. Tables, views and Couchbase collections whose name matches "
+            "`pattern` (exact, prefix, substring, then fuzzy; `schema.name` narrows by schema); with include_columns, "
+            "also those with a column whose name contains `pattern`. Reads no rows."
+        ),
+    )
+
+    async def describe(
+        connection: Connection, ctx: Context, path: ObjectPath | None = None, database: Name | None = None,
+        schema: Name | None = None, name: Name | None = None, include_ddl: bool = False,
+    ) -> CallToolResult:
+        client = client_name(ctx)
+        return await run_tool("describe", client, lambda: tools.describe(
+            connection, path, database, schema, name, include_ddl, client=client))
+
+    server.add_tool(
+        describe, name="describe", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'schema' access or more. One table, view or collection, by `path` or by name (`schema` "
+            "and `database` narrow it): columns with types, nullability and defaults, primary key, foreign keys, "
+            "indexes, comment; its DDL with include_ddl. Reads no rows."
+        ),
+    )
     return server
 
 
diff --git a/backend/dbaguy_backend/agent/tools.py b/backend/dbaguy_backend/agent/tools.py
index a844426..e36eb9b 100644
--- a/backend/dbaguy_backend/agent/tools.py
+++ b/backend/dbaguy_backend/agent/tools.py
@@ -2,21 +2,36 @@
 `agent/server.py` turns both into a CallToolResult (D-08-6). Every call reads the profiles again, so a connection the
 user set to Off is gone from the next call on (R-08-5)."""
 
+from collections.abc import Iterator
+from contextlib import contextmanager
 from dataclasses import dataclass
 from typing import Any
 from uuid import UUID
 
 from loguru import logger
 
-from ..adapters.base import DatabaseAdapter
+from ..adapters.base import DatabaseAdapter, StatementError
 from ..adapters.registry import AdapterRegistry
 from ..domain.agent import McpAccess, ToolErrorCode
-from ..domain.errors import PasswordRequired, ProfileNotFound
+from ..domain.errors import (
+    ConnectionFailed, NotFound, PasswordRequired, ProfileNotFound, QueryFailed, ValidationFailed,
+)
 from ..domain.execution import QueryContext
+from ..domain.metadata import Folder, MetadataRequest, NodeKind, ObjectNode, PathSegment, path_database
 from ..domain.profiles import AnyProfile, Engine, ServerProfile
+from ..domain.schema import SchemaSnapshot, SnapshotRelation
+from ..services.metadata_service import MetadataService
 from ..services.profile_service import ProfileService
+from ..services.schema_cache import SchemaCache
 
+LIST_LIMIT, SEARCH_LIMIT, LIMIT_MAX = 100, 50, 1000  # list_objects, search_objects (§3.5)
 _UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
+_LOCKED = ("'{}' needs its password: connect to it in DbaGuy first (or unlock it). DbaGuy never asks for a password "
+           "over MCP")
+_NO_OBJECT = "Pass the object's path (from list_objects or search_objects), or its name"
+_NO_SUCH_NAME = "No table, view or collection named {} on '{}'"
+_SEVERAL_NAMES = "Several objects match {}: {}"
+_NO_SNAPSHOT = "Keys and foreign keys could not be read: {}"
 _AMBIGUOUS = "Several connections are named '{}': pass one of their ids ({})"
 _SCHEMA_ONLY = "'{}' allows schema only: {} needs 'Read-only queries' in DbaGuy's connection settings"
 
@@ -44,10 +59,49 @@ def _database(profile: AnyProfile) -> str | None:
     return profile.database if isinstance(profile, ServerProfile) else None
 
 
+@contextmanager
+def answered(profile: AnyProfile) -> Iterator[None]:
+    """What the app's services raise, as a tool's error (§4). The server's own text stays in the message, which only the
+    agent sees (`server.run_tool` logs the code)."""
+    try:
+        yield
+    except PasswordRequired:
+        raise ToolFailure("password_required", _LOCKED.format(profile.name)) from None
+    except NotFound as exc:
+        raise ToolFailure("not_found", exc.message) from None
+    except ConnectionFailed as exc:
+        raise ToolFailure("connection_failed", exc.message) from None
+    except (QueryFailed, StatementError) as exc:
+        raise ToolFailure("query_failed", exc.message) from None
+    except ValidationFailed as exc:
+        raise ToolFailure("invalid_argument", exc.message) from None
+
+
+def _path_text(path: list[PathSegment]) -> str:
+    return "/".join(segment.name for segment in path) or "(root)"
+
+
+def _node(node: ObjectNode) -> dict[str, Any]:
+    return {"kind": node.kind, "name": node.name, "path": [s.model_dump() for s in node.path],
+            "has_children": node.has_children, "detail": node.detail}
+
+
+def _relation_of(snapshot: SchemaSnapshot, path: list[PathSegment]) -> SnapshotRelation | None:
+    """The snapshot's relation at a tree path (its schema segment and its last segment), if it has one."""
+    schema = next((s.name for s in path if s.kind == NodeKind.SCHEMA), None)
+    for candidate in snapshot.schemas:
+        if candidate.name == schema:
+            return next((r for r in candidate.relations if r.name == path[-1].name), None)
+    return None
+
+
 class AgentTools:
-    def __init__(self, profiles: ProfileService, registry: AdapterRegistry) -> None:
+    def __init__(self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService,
+                 schema_cache: SchemaCache) -> None:
         self._profiles = profiles
         self._registry = registry
+        self._metadata = metadata
+        self._schema_cache = schema_cache
 
     # ---- connections ----
 
@@ -108,6 +162,129 @@ class AgentTools:
             entry["default_query_context"] = context.text() if context is not None else None
         return entry
 
+    # ---- schema tools: the agents' own read-only metadata connection (D-08-11) ----
+
+    def list_objects(self, connection: str, path: list[PathSegment] | None = None, limit: int = LIST_LIMIT, *,
+                     client: str | None = None) -> dict[str, Any]:
+        shared = self.shared(connection, "list_objects")
+        path = path or []
+        with answered(shared.profile):
+            nodes = self._metadata.children(MetadataRequest(profile_id=shared.profile.id, path=path), agent=True)
+        logger.info("MCP list_objects on '{}' by {}: {} ({} objects)", shared.profile.name, client, _path_text(path),
+                    len(nodes))
+        return {"objects": [_node(node) for node in nodes[:limit]], "truncated": len(nodes) > limit}
+
+    def search_objects(self, connection: str, pattern: str, database: str | None = None,
+                       include_columns: bool = False, limit: int = SEARCH_LIMIT, *,
+                       client: str | None = None) -> dict[str, Any]:
+        """Tables, views and collections by name (⌘P's ranking); with `include_columns`, also those with a column whose
+        name holds `pattern` (and those columns)."""
+        shared = self.shared(connection, "search_objects")
+        pid = shared.profile.id
+        with answered(shared.profile):
+            hits = self._schema_cache.search(pid, pattern, database, limit, agent=True)
+            found = [{"database": h.database, "schema": h.schema_name, "name": h.name, "kind": h.kind,
+                      "path": [s.model_dump() for s in h.path]} for h in hits]
+            if include_columns and (needle := pattern.strip().lower()):
+                snapshot = self._schema_cache.get(pid, database, agent=True)
+                self._add_column_hits(shared, snapshot, needle, found, limit)
+        logger.info("MCP search_objects on '{}' by {}: {} matches", shared.profile.name, client, len(found))
+        return {"objects": found[:limit], "truncated": len(found) > limit}
+
+    @staticmethod
+    def _add_column_hits(shared: Shared, snapshot: SchemaSnapshot, needle: str, found: list[dict[str, Any]],
+                         limit: int) -> None:
+        by_name = {(hit["schema"], hit["name"]): hit for hit in found}
+        for schema in snapshot.schemas:
+            for relation in schema.relations:
+                columns = [c.name for c in relation.columns if needle in c.name.lower()]
+                if not columns:
+                    continue
+                hit = by_name.get((schema.name, relation.name))
+                if hit is None:
+                    if len(found) > limit:
+                        return
+                    path = shared.adapter.relation_path(snapshot.database, schema.name, relation.name, relation.kind)
+                    hit = {"database": snapshot.database, "schema": schema.name, "name": relation.name,
+                           "kind": relation.kind, "path": [s.model_dump() for s in path]}
+                    found.append(hit)
+                    by_name[(schema.name, relation.name)] = hit
+                hit["columns"] = columns
+
+    def describe(self, connection: str, path: list[PathSegment] | None = None, database: str | None = None,
+                 schema: str | None = None, name: str | None = None, include_ddl: bool = False, *,
+                 client: str | None = None) -> dict[str, Any]:
+        """One object: its columns, keys, foreign keys, indexes and comment; its DDL when asked. By `path`, or by name
+        (`schema` and `database` narrow it): exact first, then ignoring case."""
+        shared = self.shared(connection, "describe")
+        pid = shared.profile.id
+        with answered(shared.profile):
+            if not path:
+                if not name:
+                    raise ToolFailure("invalid_argument", _NO_OBJECT)
+                path = self._path_by_name(shared, database, schema, name)
+            request = MetadataRequest(profile_id=pid, path=path)
+            detail = self._metadata.detail(request, agent=True)
+            indexes = self._indexes(request)
+        notes: list[str] = []
+        relation: SnapshotRelation | None = None
+        if shared.profile.engine != Engine.COUCHBASE:
+            try:
+                snapshot = self._schema_cache.get(pid, path_database(path), agent=True)
+                relation = _relation_of(snapshot, path)
+            except Exception as exc:  # the detail stands without them
+                notes.append(_NO_SNAPSHOT.format(getattr(exc, "message", type(exc).__name__)))
+        columns = detail.columns or []
+        primary = relation.primary_key if relation is not None else [c.name for c in columns if c.is_primary_key]
+        result: dict[str, Any] = {
+            "kind": detail.kind, "name": detail.name, "path": [s.model_dump() for s in path],
+            "columns": [{"name": c.name, "type": c.type_name, "nullable": c.nullable, "default": c.default,
+                         "comment": c.comment} for c in columns],
+            "primary_key": primary,
+            "foreign_keys": [{"name": fk.name, "columns": fk.columns, "references": f"{fk.ref_schema}.{fk.ref_table}",
+                              "ref_columns": fk.ref_columns} for fk in (relation.foreign_keys if relation else [])],
+            "indexes": indexes, "comment": detail.comment,
+        }
+        if include_ddl:
+            result["ddl"] = detail.ddl
+            result["ddl_is_generated"] = detail.ddl_is_generated
+        if notes:
+            result["notes"] = notes
+        logger.info("MCP describe on '{}' by {}: {}", shared.profile.name, client, _path_text(path))
+        return result
+
+    def _indexes(self, request: MetadataRequest) -> list[dict[str, Any]]:
+        """The object's indexes: its `indexes` folder's nodes, or index nodes right under it (Couchbase)."""
+        children = self._metadata.children(request, agent=True)
+        found = [child for child in children if child.kind == NodeKind.INDEX]
+        if any(child.kind == NodeKind.FOLDER and child.name == Folder.INDEXES for child in children):
+            folder = request.model_copy(update={"path": [*request.path,
+                                                          PathSegment(kind=NodeKind.FOLDER, name=Folder.INDEXES)]})
+            found += [child for child in self._metadata.children(folder, agent=True) if child.kind == NodeKind.INDEX]
+        return [{"name": index.name, "detail": index.detail} for index in found]
+
+    def _path_by_name(self, shared: Shared, database: str | None, schema: str | None, name: str) -> list[PathSegment]:
+        """The object named `name` in the snapshot: exact, then ignoring case; one, or `ambiguous`. Names never reach
+        SQL here: the path they lead to goes through the catalog's own queries (bound or quoted)."""
+        snapshot = self._schema_cache.get(shared.profile.id, database, agent=True)
+        candidates = [(snapshot.database, s.name, r.name, r.kind) for s in snapshot.schemas for r in s.relations]
+        candidates += [(k.bucket, k.scope, k.collection, "collection") for k in snapshot.keyspaces]
+        if database is not None and shared.profile.engine == Engine.COUCHBASE:
+            candidates = [c for c in candidates if c[0] == database]
+
+        def matching(same: Any) -> list[tuple[Any, ...]]:
+            return [c for c in candidates if same(c[2], name) and (schema is None or same(c[1], schema))]
+
+        found = matching(str.__eq__) or matching(lambda a, b: a.casefold() == b.casefold())
+        shown = ".".join(part for part in (database, schema, name) if part)
+        if not found:
+            raise ToolFailure("not_found", _NO_SUCH_NAME.format(shown, shared.profile.name))
+        if len(found) > 1:
+            raise ToolFailure("ambiguous", _SEVERAL_NAMES.format(shown, ", ".join(
+                ".".join(part for part in (c[0], c[1], c[2]) if part) for c in found[:20])))
+        db, schema_name, relation, kind = found[0]
+        return shared.adapter.relation_path(db, schema_name, relation, kind)
+
     def _needs_unlock(self, profile: AnyProfile) -> bool:
         """A password it would ask for: a profile that does not save its password and has none unlocked this run. One
         that saves it says False without reading the Keychain (an agent's list must never make the Keychain ask)."""
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index 8401478..bac8cad 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -116,7 +116,7 @@ class AppContainer:
             self.transfers.plans,
         )
         self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
-        self.agent_tools = AgentTools(self.profiles, self.registry)
+        self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache)
         self.agent_host = AgentHost(
             self.agent_tools, token_store or _token_store(settings, secret_store), default_mcp_port(data_dir),
             on_stop=self._close_agents,
@@ -136,6 +136,7 @@ class AppContainer:
 
     def _close_agents(self) -> None:
         """The MCP server stopped: nothing an agent opened stays open."""
+        self.metadata.close_all(agents_only=True)
 
     def _housekeep(self) -> None:
         while not self._stop.wait(_HOUSEKEEPING_INTERVAL_S):
diff --git a/backend/dbaguy_backend/services/connection_service.py b/backend/dbaguy_backend/services/connection_service.py
index 5feed0f..8fa5a85 100644
--- a/backend/dbaguy_backend/services/connection_service.py
+++ b/backend/dbaguy_backend/services/connection_service.py
@@ -46,13 +46,17 @@ class ConnectionService:
         secrets: ResolvedSecrets | None = None,
         *,
         share_tunnel: bool = True,
+        agent: bool = False,
     ) -> ConnectionLease:
-        """`share_tunnel=False`: use an SSH tunnel of its own, never one that sessions are using."""
+        """`share_tunnel=False`: use an SSH tunnel of its own, never one that sessions are using. `agent`: an MCP
+        agent's connection (spec ⑧ D-08-4): opened read-only, whatever the profile says."""
+        if agent:
+            profile = profile.model_copy(update={"read_only": True})
         secrets = secrets if secrets is not None else self._profiles.resolve_secrets(profile)
         adapter = self._registry.get(profile.engine)
         endpoint, release = self._endpoint(adapter, profile, secrets, share_tunnel)
         try:
-            connection = adapter.open(profile, endpoint, secrets.password, database)
+            connection = adapter.open(profile, endpoint, secrets.password, database, agent=agent)
         except BaseException:
             release()
             raise
diff --git a/backend/dbaguy_backend/services/metadata_service.py b/backend/dbaguy_backend/services/metadata_service.py
index 016acb8..91b2ae5 100644
--- a/backend/dbaguy_backend/services/metadata_service.py
+++ b/backend/dbaguy_backend/services/metadata_service.py
@@ -17,7 +17,7 @@ from .connection_service import ConnectionService
 from .profile_service import ProfileService
 
 T = TypeVar("T")
-_Key = tuple[UUID, str | None]
+_Key = tuple[UUID, str | None, bool]  # (profile, database, an MCP agent's: spec ⑧ D-08-11)
 
 WRITE_TIMEOUT_S = 120.0  # how long a write waits for its answer, time queued behind another call included (as a read)
 _NOT_SENT = "Timed out waiting for the connection: nothing was sent"
@@ -69,7 +69,9 @@ class _MetaEntry:
 
 
 class MetadataService:
-    """Object-tree queries on dedicated metadata connections, one per (profile, database)."""
+    """Object-tree queries on dedicated metadata connections, one per (profile, database), and one more per (profile,
+    database) for MCP agents (`agent=True`, spec ⑧ D-08-11): opened read-only (`ConnectionService.open(agent=True)`), so
+    an agent's schema tools never run on the profile's writable connection. Both close after the same idle time."""
 
     def __init__(
         self, registry: AdapterRegistry, connections: ConnectionService, profiles: ProfileService,
@@ -84,11 +86,11 @@ class MetadataService:
         self._key_locks: dict[_Key, threading.Lock] = {}
         self._lock = threading.Lock()
 
-    def children(self, request: MetadataRequest) -> list[ObjectNode]:
-        return self._call(request, lambda c: c.list_children(request.path))
+    def children(self, request: MetadataRequest, *, agent: bool = False) -> list[ObjectNode]:
+        return self._call(request, lambda c: c.list_children(request.path), agent)
 
-    def detail(self, request: MetadataRequest) -> ObjectDetail:
-        return self._call(request, lambda c: c.object_detail(request.path))
+    def detail(self, request: MetadataRequest, *, agent: bool = False) -> ObjectDetail:
+        return self._call(request, lambda c: c.object_detail(request.path), agent)
 
     def invalidate(self, profile_id: UUID) -> None:
         with self._lock:
@@ -104,14 +106,16 @@ class MetadataService:
             self._drop(key, worker)
         return len(idle)
 
-    def close_all(self) -> None:
+    def close_all(self, *, agents_only: bool = False) -> None:
+        """Close every metadata connection, or (`agents_only`, the MCP server stopped) every agent's."""
         with self._lock:
-            entries = [(k, e.worker) for k, e in self._workers.items()]
+            entries = [(k, e.worker) for k, e in self._workers.items() if not agents_only or k[2]]
         for key, worker in entries:
             self._drop(key, worker)
 
     def with_connection(
-        self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True
+        self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True,
+        agent: bool = False,
     ) -> T:
         """Run `fn` on the metadata connection of (profile, database), reconnecting once if it was lost.
 
@@ -125,17 +129,23 @@ class MetadataService:
         trip that had to go before the write failed, ruling C40) is QueryFailed, nothing was sent, and the connection is
         dropped. Any other failure after it started that is not a DomainError (the timeout, an unexpected error) is
         WriteOutcomeUnknown: it may have been applied.
+
+        `agent`: on the agents' read-only connection of (profile, database) (D-08-11); never a write.
         """
         profile = self._profiles.get(profile_id)
         adapter = self._registry.get(profile.engine)
         target = database if adapter.capabilities.per_database_connection else None
-        return self._call_on(profile, target, fn) if retry else self._write_on(profile, target, fn)
+        if agent and not retry:
+            raise ValueError("an agent's metadata connection never writes")
+        return self._call_on(profile, target, fn, agent) if retry else self._write_on(profile, target, fn)
 
-    def _call(self, request: MetadataRequest, fn: Callable[[DatabaseConnection], T]) -> T:
-        return self.with_connection(request.profile_id, path_database(request.path), fn)
+    def _call(self, request: MetadataRequest, fn: Callable[[DatabaseConnection], T], agent: bool = False) -> T:
+        return self.with_connection(request.profile_id, path_database(request.path), fn, agent=agent)
 
-    def _call_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
-        key = (profile.id, database)
+    def _call_on(
+        self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T], agent: bool = False,
+    ) -> T:
+        key = (profile.id, database, agent)
         for attempt in (1, 2):
             worker = self._worker(key, profile, database)
             try:
@@ -160,7 +170,7 @@ class MetadataService:
     def _write_on(self, profile: AnyProfile, database: str | None, fn: Callable[[DatabaseConnection], T]) -> T:
         """`with_connection(retry=False)`. A DomainError is the call's own answer and passes through untouched, except
         the worker's own refusal to run it (SessionBusy: closed meanwhile), which means nothing was sent."""
-        key = (profile.id, database)
+        key = (profile.id, database, False)
         worker = self._worker(key, profile, database)
         call = _WriteCall(fn)
         try:
@@ -210,9 +220,10 @@ class MetadataService:
                 if entry is not None:
                     return entry.worker
             secrets = self._profiles.resolve_secrets(profile)
+            agent = key[2]
             worker = SessionWorker(
-                f"meta:{profile.name}:{database or '-'}",
-                lambda: self._connections.open(profile, database, secrets),
+                f"{'mcp-meta' if agent else 'meta'}:{profile.name}:{database or '-'}",
+                lambda: self._connections.open(profile, database, secrets, agent=agent),
             )
             try:
                 worker.start()
diff --git a/backend/dbaguy_backend/services/schema_cache.py b/backend/dbaguy_backend/services/schema_cache.py
index 234af89..faac944 100644
--- a/backend/dbaguy_backend/services/schema_cache.py
+++ b/backend/dbaguy_backend/services/schema_cache.py
@@ -7,7 +7,7 @@ from uuid import UUID
 
 from loguru import logger
 
-from ..adapters.base import DatabaseAdapter
+from ..adapters.base import DatabaseAdapter, DatabaseConnection
 from ..adapters.registry import AdapterRegistry
 from ..domain.schema import ObjectSearchHit, RelationKind, SchemaSnapshot
 from ..sql.completion import SnapshotIndex
@@ -40,6 +40,10 @@ class _Match(NamedTuple):
 _SortKey = tuple[int, bool, int, int, str]  # rank, system, search-path position, name length, name
 
 
+def _snapshot_of(connection: DatabaseConnection) -> SchemaSnapshot:
+    return connection.schema_snapshot()
+
+
 def _is_subsequence(needle: str, text: str) -> bool:
     """True when every character of `needle` occurs in `text`, in order (not necessarily adjacent)."""
     remaining = iter(text)
@@ -65,6 +69,9 @@ class SchemaCache:
 
     A failed build is remembered for `failure_ttl_s` and re-raised without reconnecting: completion asks for the
     snapshot on every keystroke, and an unreachable server must not cost a connect timeout each time.
+
+    An MCP agent's request (`agent=True`, spec ⑧ D-08-11) uses the same snapshot when there is one (data in memory, no
+    connection); when one must be built, it is built on the agents' read-only metadata connection.
     """
 
     def __init__(
@@ -100,8 +107,8 @@ class SchemaCache:
             entry = self._entries.get(key)
         return entry.snapshot if entry else None
 
-    def get(self, profile_id: UUID, database: str | None = None) -> SchemaSnapshot:
-        snapshot = self._snapshot(profile_id, self._scope(profile_id, database), wait=True)
+    def get(self, profile_id: UUID, database: str | None = None, *, agent: bool = False) -> SchemaSnapshot:
+        snapshot = self._snapshot(profile_id, self._scope(profile_id, database), wait=True, agent=agent)
         assert snapshot is not None  # waiting always ends in a snapshot or an exception
         return snapshot
 
@@ -143,7 +150,9 @@ class SchemaCache:
                 for key in [k for k in store if k[0] == profile_id and (database is None or k[1] == database)]:
                     del store[key]
 
-    def search(self, profile_id: UUID, q: str, database: str | None = None, limit: int = 50) -> list[ObjectSearchHit]:
+    def search(
+        self, profile_id: UUID, q: str, database: str | None = None, limit: int = 50, *, agent: bool = False,
+    ) -> list[ObjectSearchHit]:
         """Tables and views whose name matches `q` (or `schema.name`): exact, prefix, substring, then subsequence.
         Couchbase collections the same way (B20): the text before the last dot has to prefix the scope or
         `bucket.scope`, so `inventory.airl` and `travel-sample.inventory.airl` both narrow to one scope."""
@@ -151,7 +160,7 @@ class SchemaCache:
         needle, schema_needle = name_part.strip().lower(), schema_part.strip().lower()
         if not needle and not schema_needle:
             return []
-        snapshot = self.get(profile_id, database)
+        snapshot = self.get(profile_id, database, agent=agent)
         adapter = self._adapter(profile_id)
         search_path = {name.lower(): position for position, name in enumerate(snapshot.search_path)}
         scored: list[tuple[_SortKey, _Match]] = []
@@ -188,7 +197,9 @@ class SchemaCache:
     def _adapter(self, profile_id: UUID) -> DatabaseAdapter:
         return self._registry.get(self._profiles.get(profile_id).engine)
 
-    def _snapshot(self, profile_id: UUID, database: str | None, wait: bool) -> SchemaSnapshot | None:
+    def _snapshot(
+        self, profile_id: UUID, database: str | None, wait: bool, agent: bool = False,
+    ) -> SchemaSnapshot | None:
         """The cached snapshot, else a build (one per key; other keys are not blocked). Without `wait`, while another
         thread is building this key: the expired snapshot it replaces, or None when there is none."""
         key = self._key(profile_id, database)
@@ -211,7 +222,7 @@ class SchemaCache:
                 if cached is not None:
                     return cached
                 epoch = self._epoch
-            return self._build(key, profile_id, database, epoch)
+            return self._build(key, profile_id, database, epoch, agent)
         finally:
             key_lock.release()
 
@@ -227,10 +238,13 @@ class SchemaCache:
             raise failure.error.with_traceback(None)  # a fresh traceback each time, or the stored one would only grow
         return None
 
-    def _build(self, key: _Key, profile_id: UUID, database: str | None, epoch: int) -> SchemaSnapshot:
+    def _build(self, key: _Key, profile_id: UUID, database: str | None, epoch: int, agent: bool) -> SchemaSnapshot:
         started = time.monotonic()
         try:
-            snapshot = self._metadata.with_connection(profile_id, database, lambda c: c.schema_snapshot())
+            if agent:  # the agents' read-only connection (D-08-11)
+                snapshot = self._metadata.with_connection(profile_id, database, _snapshot_of, agent=True)
+            else:
+                snapshot = self._metadata.with_connection(profile_id, database, _snapshot_of)
         except Exception as exc:
             logger.warning("Could not build the schema snapshot of {} ({}); retrying in {:.0f} s", profile_id, exc,
                            self._failure_ttl)
diff --git a/backend/tests/agent_support.py b/backend/tests/agent_support.py
index 1708eb9..b8d70eb 100644
--- a/backend/tests/agent_support.py
+++ b/backend/tests/agent_support.py
@@ -16,14 +16,30 @@ import pytest
 from dbaguy_backend.agent.token_store import InMemoryTokenStore
 from dbaguy_backend.config import Settings
 from dbaguy_backend.container import AppContainer
+from dbaguy_backend.domain.profiles import AnyProfile, ProfileUpsertRequest
 from dbaguy_backend.domain.settings import McpSettings
 from dbaguy_backend.storage.secret_store import InMemorySecretStore
 
+from .support import COUCHBASE_PASSWORD, MSSQL_PASSWORD, MYSQL_PASSWORD, PG_PASSWORD
+
 TOKEN = "test-token"
 MCP_TOKEN = "mcp-test-token"
 AUTH = {"Authorization": f"Bearer {TOKEN}"}
 
 
+PASSWORDS = {"postgres": PG_PASSWORD, "mysql": MYSQL_PASSWORD, "mssql": MSSQL_PASSWORD,
+             "couchbase": COUCHBASE_PASSWORD}
+
+
+def share(container: AppContainer, profile: AnyProfile, access: str = "query", **changes: Any) -> str:
+    """Save `profile` (a support.make_*_profile, or a SqliteProfile) in the container, shared at `access`, with the dev
+    password; its id."""
+    draft = profile.model_dump(exclude={"id"}) | {"mcp_access": access, **changes}
+    request = ProfileUpsertRequest.model_validate({"profile": draft,
+                                                   "secrets": {"password": PASSWORDS.get(profile.engine)}})
+    return str(container.profiles.create(request).profile.id)
+
+
 def free_port() -> int:
     """A port in the settings' range (1024–49151) that nothing listens on now. Never 17531 or 17532."""
     for _ in range(200):
diff --git a/backend/tests/test_core_utils.py b/backend/tests/test_core_utils.py
index ef2630b..ed34abf 100644
--- a/backend/tests/test_core_utils.py
+++ b/backend/tests/test_core_utils.py
@@ -100,7 +100,7 @@ class FakeAdapter(DatabaseAdapter):
     def parse_url(self, url):
         return ParsedUrl(profile=SqliteProfileDraft(engine="sqlite", name="fake", path=url))
 
-    def open(self, profile, endpoint, password, database):
+    def open(self, profile, endpoint, password, database, *, agent=False):
         raise NotImplementedError
 
 
diff --git a/backend/tests/test_session_worker.py b/backend/tests/test_session_worker.py
index cc554d7..fe95750 100644
--- a/backend/tests/test_session_worker.py
+++ b/backend/tests/test_session_worker.py
@@ -284,7 +284,7 @@ class GatedConnections:
         self.release = threading.Event()
         self.lease = FakeLease()
 
-    def open(self, profile, database, secrets):
+    def open(self, profile, database, secrets, *, agent=False):
         assert self.release.wait(10)
         return self.lease
 
diff --git a/backend/tests/test_ssh_tunnel_lifecycle.py b/backend/tests/test_ssh_tunnel_lifecycle.py
index 20d7378..d0a57ae 100644
--- a/backend/tests/test_ssh_tunnel_lifecycle.py
+++ b/backend/tests/test_ssh_tunnel_lifecycle.py
@@ -59,7 +59,7 @@ class RecordingAdapter(DatabaseAdapter):
     def parse_url(self, url):
         raise NotImplementedError
 
-    def open(self, profile, endpoint: Endpoint | None, password, database):
+    def open(self, profile, endpoint: Endpoint | None, password, database, *, agent=False):
         self.endpoints.append(endpoint)
         self.passwords.append(password)
         return FakeConnection()
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_catalog.py tests/test_mcp_host.py tests/test_metadata_service.py tests/test_metadata_lifecycle.py tests/test_schema_cache.py tests/test_connection_service.py tests/test_ssh_tunnel_lifecycle.py tests/test_core_utils.py tests/test_sql_api.py tests/test_couchbase_api.py tests/test_session_worker.py`
Expected: all pass (planning: 219 passed in 21 s). No model changed: `gen:api` would make no diff.

- [ ] **Step 5: Review against the pain points** (principle 3: schema access reads no row; principle 5: the agents' catalog connection is read-only, and `SHOW transaction_read_only` on it is `on`)

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py backend/dbaguy_backend/adapters/mssql/adapter.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py backend/dbaguy_backend/adapters/couchbase/adapter.py \
  backend/dbaguy_backend/services/connection_service.py backend/dbaguy_backend/services/metadata_service.py \
  backend/dbaguy_backend/services/schema_cache.py backend/dbaguy_backend/agent/tools.py \
  backend/dbaguy_backend/agent/server.py backend/dbaguy_backend/container.py backend/tests/agent_support.py \
  backend/tests/test_mcp_catalog.py backend/tests/test_core_utils.py backend/tests/test_session_worker.py \
  backend/tests/test_ssh_tunnel_lifecycle.py
git commit -m "feat(08a): schema tools on the agents' own read-only metadata connection

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 3: The login's privileges — `login_privileges()`, `LoginRegistry`, `/mcp/connections/{id}/login`

**Suggested implementer:** Sonnet. Three short catalog queries, and a cache.

**Files:**
- Create:
  - `backend/dbaguy_backend/agent/login.py`
  - `backend/tests/test_mcp_login.py`
- Modify:
  - `backend/dbaguy_backend/domain/agent.py`
  - `backend/dbaguy_backend/adapters/{base.py, postgres/adapter.py, mysql/adapter.py, mysql/catalog.py, mssql/adapter.py}`
  - `backend/dbaguy_backend/agent/tools.py`
  - `backend/dbaguy_backend/api/{mcp.py, profiles.py}`
  - `backend/dbaguy_backend/container.py`
  - `src/lib/api/{openapi.json, schema.d.ts}` (by `gen:api`)
- Test: `backend/tests/test_mcp_login.py`

**Interfaces:**
- Consumes: Task 2's `MetadataService.with_connection(…, agent=True)`, `AgentTools`, `share()`.
- Produces:
  - `domain/agent.py`:
    - `LoginGrant = Literal["superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends", "file", "can_write", "sysadmin", "bulkadmin"]`
    - `LoginPrivileges{grants: list[LoginGrant], secure_file_priv: str | None}`, with `.summary() -> str`
  - `DatabaseConnection.login_privileges() -> LoginPrivileges | None`: None by default (SQLite, Couchbase); PG, MySQL and SQL Server answer with one query.
  - `adapters/mysql/catalog.py`: `file_granted(grants: Sequence[str]) -> bool` and `login_privileges(conn) -> LoginPrivileges`.
  - `agent/login.py`: `LoginRegistry(metadata)`, with:
    - `.peek(profile_id) -> LoginPrivileges | None`
    - `.remember(profile, read)`: reads once; a failing query is logged by its code and tried again later
    - `.ensure(profile)`: reads on the agents' catalog connection of the profile's own database
    - `.read(profile) -> LoginPrivileges | None`: reads now (the dialog)
    - `.forget(profile_id)`
  - `AgentTools(profiles, registry, metadata, schema_cache, login)`: `list_connections()` entries carry `login` (`{grants, secure_file_priv?, summary}` or null), and every schema tool calls `login.ensure(profile)` first.
  - `GET /mcp/connections/{profile_id}/login -> LoginPrivileges | None`.
  - `container.agent_login`. `_forget_profile_state` also calls `agent_login.forget`.

**Decisions this task carries:**
- CR-08-1, §3.3: the privileges are read once, by the agent's first connection (a catalog call here, an agent session in Task 6), and kept until the profile changes. `list_connections` never connects: `login` stays null until then.
- PG:
  - `rolsuper`, and `pg_has_role(current_user, …, 'MEMBER')` on the four `pg_*` roles.
  - A superuser holds them all; the dev login lists all five.
- MySQL (P2, A15 probed):
  - `information_schema.USER_PRIVILEGES` lists only privileges granted directly; a FILE that comes from an active role is missing from it.
  - `SHOW GRANTS` without FOR lists the active roles' privileges as the account's own. The rule reads FILE, or ALL, on `*.*` from it, and then `@@GLOBAL.secure_file_priv`.
  - Probed on 8.4.11: user `zz_08_u` with role `zz_08_r` holding FILE got `USER_PRIVILEGES` [] and `SHOW GRANTS` with `GRANT FILE ON *.* TO zz_08_u`.
- SQL Server (P1): the spec calls `login_can_write` existing, but there is none. It is `_LOGIN_SQL`'s first six values: `IS_MEMBER('db_owner')` and `HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', …)` for INSERT, UPDATE, DELETE, ALTER and EXECUTE. `IS_SRVROLEMEMBER('sysadmin')` and `IS_SRVROLEMEMBER('bulkadmin')` come in the same query.
  - The dev `dbaguy` gives `["can_write"]`.
  - `sa` gives `["can_write", "sysadmin", "bulkadmin"]`.
- P16: the privileges are kept per profile, read on the profile's own database. SQL Server's `can_write` is that database's.
- The dialog's route reads on the agents' read-only connection too, so a locked profile answers 428 `password_required` as every route does. SQLite answers null.
- The privileges' summary goes to the log at INFO. It is never a secret: `MCP: the login of 'x' can: is a superuser, …`.

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_mcp_login.py`:**

```python
"""The login's privileges beyond reading (spec ⑧ CR-08-1, §3.3; A15): read once by an agent's connection, shown by
list_connections and the connection dialog's route. The MySQL role and user are zz_08_*, dropped by exact name."""

import sqlite3
from uuid import UUID

import mysql.connector
import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.adapters.mysql.catalog import file_granted
from dbaguy_backend.app import create_app
from dbaguy_backend.domain.agent import LoginPrivileges
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile, SslConfig

from .agent_support import AUTH, container, share
from .support import (
    MSSQL_SA_PASSWORD, make_mssql_profile, make_mysql_profile, make_pg_profile, mysql_driver_kwargs,
    requires_mssql, requires_mysql, requires_pg,
)

__all__ = ["container"]


def _login(container, pid: str) -> LoginPrivileges | None:
    profile = container.profiles.get(UUID(pid))
    return container.agent_login.read(profile)


@pytest.mark.parametrize(("lines", "granted"), [
    (["GRANT FILE ON *.* TO `u`@`%`"], True),
    (["GRANT SELECT, INSERT, FILE, REFERENCES ON *.* TO `root`@`%` WITH GRANT OPTION"], True),
    (["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"], True),
    (["GRANT USAGE ON *.* TO `u`@`%`", "GRANT ALL PRIVILEGES ON `dbaguy`.* TO `u`@`%`"], False),
    (["GRANT SELECT, PROCESS ON *.* TO `u`@`%`", "GRANT `r`@`%` TO `u`@`%`"], False),
    (["GRANT SELECT ON `fileserver`.* TO `u`@`%`"], False),
])
def test_mysql_file_comes_from_a_global_grant_by_name_or_by_all(lines, granted):
    assert file_granted(lines) is granted


def test_the_summary_says_what_the_login_can_do():
    assert LoginPrivileges().summary() == "nothing beyond reading"
    assert LoginPrivileges(grants=["superuser", "server_programs"]).summary() == (
        "is a superuser, run programs on the server")


@requires_pg
def test_postgres_superuser_holds_every_role_and_list_connections_shows_it_once_read(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"), access="schema")
    assert container.agent_tools.list_connections()["connections"][0]["login"] is None  # nothing connected yet
    container.agent_tools.list_objects(pid)  # the first catalog call reads it
    login = container.agent_tools.list_connections()["connections"][0]["login"]
    assert login["grants"] == ["superuser", "server_files_read", "server_files_write", "server_programs",
                               "signal_backends"]
    assert login["summary"].startswith("is a superuser")


@requires_mysql
def test_mysql_file_through_an_active_role_is_seen(container):
    root = mysql.connector.connect(**mysql_driver_kwargs(user="root", database=None, autocommit=True))
    cursor = root.cursor()
    try:
        for sql in ("DROP USER IF EXISTS 'zz_08_u'@'%'", "DROP ROLE IF EXISTS 'zz_08_r'", "CREATE ROLE 'zz_08_r'",
                    "GRANT FILE ON *.* TO 'zz_08_r'", "CREATE USER 'zz_08_u'@'%' IDENTIFIED BY 'zz08pw'",
                    "GRANT 'zz_08_r' TO 'zz_08_u'@'%'", "SET DEFAULT ROLE ALL TO 'zz_08_u'@'%'",
                    "GRANT SELECT ON dbaguy.* TO 'zz_08_u'@'%'"):
            cursor.execute(sql)
        plain = share(container, make_mysql_profile(name="zz_08 mysql"), access="schema")
        assert _login(container, plain) == LoginPrivileges()
        as_root = share(container, make_mysql_profile(name="zz_08 root", username="root"), access="schema")
        assert _login(container, as_root) == LoginPrivileges(grants=["file"], secure_file_priv="/var/lib/mysql-files/")
        # TLS: a new caching_sha2_password account's first login needs it (R-03a-P5)
        draft = make_mysql_profile(name="zz_08 role", username="zz_08_u",
                                   ssl=SslConfig(mode="require")).model_dump(exclude={"id"})
        request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                       "secrets": {"password": "zz08pw"}})
        role = str(container.profiles.create(request).profile.id)
        assert _login(container, role).grants == ["file"]
    finally:
        cursor.execute("DROP USER IF EXISTS 'zz_08_u'@'%'")
        cursor.execute("DROP ROLE IF EXISTS 'zz_08_r'")
        root.close()


@requires_mssql
def test_sql_server_says_the_login_can_write_and_its_server_roles(container):
    owner = share(container, make_mssql_profile(name="zz_08 mssql"), access="schema")
    assert _login(container, owner).grants == ["can_write"]  # dbaguy is db_owner (T0)
    draft = make_mssql_profile(name="zz_08 sa", username="sa").model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "schema"},
                                                   "secrets": {"password": MSSQL_SA_PASSWORD}})
    sa = str(container.profiles.create(request).profile.id)
    assert _login(container, sa).grants == ["can_write", "sysadmin", "bulkadmin"]


def test_the_dialogs_route_reads_it_and_a_saved_profile_forgets_it(container, tmp_path):
    path = tmp_path / "zz_08.db"
    sqlite3.connect(path).close()
    pid = share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000009", engine="sqlite", name="lite",
                                         path=str(path)), access="off")
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        answer = client.get(f"/mcp/connections/{pid}/login", headers=AUTH)
        assert answer.status_code == 200 and answer.json() is None  # SQLite: nothing to tell
        missing = client.get("/mcp/connections/00000000-0000-0000-0000-00000000000a/login", headers=AUTH)
        assert missing.status_code == 404
    container.agent_login._known[UUID(pid)] = LoginPrivileges(grants=["file"])
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
        body = {"profile": {"engine": "sqlite", "name": "lite", "path": str(path), "mcp_access": "query"}}
        assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).status_code == 200
    assert container.agent_login.peek(UUID(pid)) is None
```

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_login.py`
Expected: `ImportError: cannot import name 'file_granted'`.

- [ ] **Step 3: Write the code**

**New file `backend/dbaguy_backend/agent/login.py`:**

```python
"""What each shared connection's login may do beyond reading (spec ⑧ CR-08-1, §3.3): read once by an agent's connection
(the first agent session or catalog call), kept in memory until the profile changes, shown by list_connections and the
connection dialog. The queries are the adapters' (`DatabaseConnection.login_privileges`)."""

import threading
from collections.abc import Callable
from uuid import UUID

from loguru import logger

from ..adapters.base import DatabaseConnection, StatementError
from ..domain.agent import LoginPrivileges
from ..domain.errors import QueryFailed
from ..domain.profiles import AnyProfile
from ..services.metadata_service import MetadataService


def _read_on(connection: DatabaseConnection) -> LoginPrivileges | None:
    return connection.login_privileges()


class LoginRegistry:
    def __init__(self, metadata: MetadataService) -> None:
        self._metadata = metadata
        self._known: dict[UUID, LoginPrivileges | None] = {}  # None: the engine has nothing to tell
        self._lock = threading.Lock()

    def peek(self, profile_id: UUID) -> LoginPrivileges | None:
        """What is known, without connecting: None until a connection has read it."""
        with self._lock:
            return self._known.get(profile_id)

    def remember(self, profile: AnyProfile, read: Callable[[], LoginPrivileges | None]) -> None:
        """Read it with `read` unless known. A query that fails (a privilege the catalog needs) is logged by code and
        tried again next time; a password or connection failure is the caller's (its call fails on it anyway)."""
        with self._lock:
            if profile.id in self._known:
                return
        try:
            privileges = read()
        except (StatementError, QueryFailed) as exc:
            logger.warning("MCP: the login of '{}' could not be read ({})", profile.name,
                           getattr(exc, "code", None) or type(exc).__name__)
            return
        self._keep(profile, privileges)

    def ensure(self, profile: AnyProfile) -> None:
        """Known after this, read on the agents' catalog connection of the profile's own database when it was not."""
        self.remember(profile, lambda: self._metadata.with_connection(profile.id, None, _read_on, agent=True))

    def read(self, profile: AnyProfile) -> LoginPrivileges | None:
        """Read now (the connection dialog), on the agents' catalog connection, and keep it."""
        privileges = self._metadata.with_connection(profile.id, None, _read_on, agent=True)
        self._keep(profile, privileges)
        return privileges

    def forget(self, profile_id: UUID) -> None:
        with self._lock:
            self._known.pop(profile_id, None)

    def _keep(self, profile: AnyProfile, privileges: LoginPrivileges | None) -> None:
        with self._lock:
            self._known[profile.id] = privileges
        if privileges is not None:
            logger.info("MCP: the login of '{}' can: {}", profile.name, privileges.summary())
```

**Patch T3** (the files this task modifies: `backend/dbaguy_backend/adapters/base.py`, `backend/dbaguy_backend/adapters/mssql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/catalog.py`, `backend/dbaguy_backend/adapters/postgres/adapter.py`, `backend/dbaguy_backend/agent/tools.py`, `backend/dbaguy_backend/api/mcp.py`, `backend/dbaguy_backend/api/profiles.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/domain/agent.py`). Save the block in your scratchpad as `08a-t3.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t3.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/adapters/base.py b/backend/dbaguy_backend/adapters/base.py
index 7ca7c37..d32b830 100644
--- a/backend/dbaguy_backend/adapters/base.py
+++ b/backend/dbaguy_backend/adapters/base.py
@@ -4,6 +4,7 @@ from dataclasses import dataclass
 from enum import StrEnum
 from typing import Any, ClassVar, Literal
 
+from ..domain.agent import LoginPrivileges
 from ..domain.couchbase import FieldSample, Keyspace
 from ..domain.data_jobs import DumpObjectKinds, DumpScope
 from ..domain.edits import EditTarget
@@ -287,6 +288,12 @@ class DatabaseConnection(ABC):
         say (every engine but MySQL) or that answer did not (rows, an error)."""
         return None
 
+    def login_privileges(self) -> LoginPrivileges | None:
+        """What the login may do beyond reading that a read-only transaction does not stop (spec ⑧ CR-08-1): one query.
+        None: the engine has nothing to tell (SQLite: the sidecar's own file access; Couchbase: its roles and
+        `readonly=true`)."""
+        return None
+
     @abstractmethod
     def autocommit(self) -> bool: ...
 
diff --git a/backend/dbaguy_backend/adapters/mssql/adapter.py b/backend/dbaguy_backend/adapters/mssql/adapter.py
index 4b2c774..7fadf8a 100644
--- a/backend/dbaguy_backend/adapters/mssql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mssql/adapter.py
@@ -26,6 +26,7 @@ from typing import Any
 
 from loguru import logger
 
+from ...domain.agent import LoginGrant, LoginPrivileges
 from ...domain.column_category import ColumnCategory
 from ...domain.data_jobs import DumpObjectKinds, DumpScope
 from ...domain.edits import EditTarget
@@ -71,6 +72,15 @@ _TYPE_PEEK = 100  # rows read ahead to guess the column types of a result set th
 _OPEN_QUERY = "SELECT @@SPID, @@TRANCOUNT, XACT_STATE(), @@OPTIONS & 2, DB_NAME()"
 _STATE_QUERY = "SELECT @@TRANCOUNT, XACT_STATE(), @@OPTIONS & 2, DB_NAME()"  # @@OPTIONS bit 2: IMPLICIT_TRANSACTIONS
 _SERVER_INFO = "SELECT @@VERSION, DB_NAME(), SUSER_SNAME()"
+# spec ⑧ CR-08-1: whether the login can write in this database (db_owner, or INSERT/UPDATE/DELETE/ALTER/EXECUTE on it:
+# SQL Server has no read-only session, so this is what an agent's slip could reach), then two server roles
+_LOGIN_CAN_WRITE = (
+    "IS_MEMBER('db_owner'), HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'), "
+    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'), HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'), "
+    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE')"
+)
+_LOGIN_SQL = f"SELECT {_LOGIN_CAN_WRITE}, IS_SRVROLEMEMBER('sysadmin'), IS_SRVROLEMEMBER('bulkadmin')"
+_WRITE_CHECKS = 6  # the first six values of _LOGIN_SQL
 _COMMIT = "WHILE @@TRANCOUNT > 0 COMMIT TRANSACTION"  # every nesting level (Ruling 18)
 _ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"
 _NO_COLUMN_NAME = "(No column name)"
@@ -680,6 +690,15 @@ class MssqlConnection(DatabaseConnection, RowEditing, DataTools, ReferenceReadin
     def current_database(self) -> str | None:
         return self._database
 
+    def login_privileges(self) -> LoginPrivileges:
+        row = self._catalog_call(lambda: catalog.first_row(self._conn, _LOGIN_SQL)) or ()
+        held = [value == 1 for value in row]
+        grants: list[LoginGrant] = []
+        if any(held[:_WRITE_CHECKS]):
+            grants.append("can_write")
+        grants += [grant for grant, on in zip(("sysadmin", "bulkadmin"), held[_WRITE_CHECKS:], strict=False) if on]
+        return LoginPrivileges(grants=grants)
+
     def _check_database(self, path: list[PathSegment]) -> None:
         if path and path[0].name != self._database:
             raise ValidationFailed(f"Connection is bound to database '{self._database}', not '{path[0].name}'")
diff --git a/backend/dbaguy_backend/adapters/mysql/adapter.py b/backend/dbaguy_backend/adapters/mysql/adapter.py
index 0489242..da7d4d5 100644
--- a/backend/dbaguy_backend/adapters/mysql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mysql/adapter.py
@@ -14,6 +14,7 @@ from mysql.connector.connection import MySQLConnection
 from mysql.connector.constants import ServerFlag
 from mysql.connector.cursor import MySQLCursorRaw
 
+from ...domain.agent import LoginPrivileges
 from ...domain.data_jobs import DumpObjectKinds, DumpScope
 from ...domain.edits import EditTarget
 from ...domain.errors import ConnectionFailed, ValidationFailed
@@ -488,6 +489,12 @@ class MysqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
     def current_database(self) -> str | None:
         return self._database
 
+    def login_privileges(self) -> LoginPrivileges:
+        try:
+            return catalog.login_privileges(self._conn)
+        except mysql.connector.Error as exc:
+            raise self.translate_error(exc) from exc
+
     def list_children(self, path: list[PathSegment]) -> list[ObjectNode]:
         return introspection.list_children(self._conn, path)
 
diff --git a/backend/dbaguy_backend/adapters/mysql/catalog.py b/backend/dbaguy_backend/adapters/mysql/catalog.py
index da39a8f..ae90a38 100644
--- a/backend/dbaguy_backend/adapters/mysql/catalog.py
+++ b/backend/dbaguy_backend/adapters/mysql/catalog.py
@@ -4,12 +4,15 @@ Internal SQL only. It never contains the word "delimiter": statement splitting (
 of a statement as a client command, and these helpers must never be mistaken for user SQL.
 """
 
+import re
 from collections.abc import Sequence
 from typing import Any
 
 from mysql.connector.connection import MySQLConnection
 from mysql.connector.cursor import MySQLCursorBuffered
 
+from ...domain.agent import LoginPrivileges
+
 SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
 # Unknown database, table, routine, trigger, event.
 MISSING_OBJECT_ERRNOS = frozenset({1049, 1146, 1305, 1360, 1539})
@@ -51,6 +54,30 @@ def first_row(conn: MySQLConnection, sql: str, params: Sequence[Any] = ()) -> tu
     return rows[0] if rows else None
 
 
+# One line of SHOW GRANTS that grants global (`*.*`) static privileges: `GRANT SELECT, FILE, … ON *.* TO …`
+_GLOBAL_GRANT = re.compile(r"GRANT (.+?) ON \*\.\* TO ", re.DOTALL)
+_FILE_PRIVILEGES = frozenset({"FILE", "ALL", "ALL PRIVILEGES"})
+
+
+def file_granted(grants: Sequence[str]) -> bool:
+    """Whether SHOW GRANTS' lines give FILE on `*.*`, by name or by ALL. Without FOR, SHOW GRANTS lists the session's
+    active roles' privileges as the account's own (A15, probed on 8.4: information_schema.USER_PRIVILEGES does not)."""
+    for line in grants:
+        if (found := _GLOBAL_GRANT.match(line)) is not None:
+            if {p.strip().upper() for p in found.group(1).split(",")} & _FILE_PRIVILEGES:
+                return True
+    return False
+
+
+def login_privileges(conn: MySQLConnection) -> LoginPrivileges:
+    """FILE (spec ⑧ CR-08-1: INTO OUTFILE and LOAD_FILE are not stopped by a read-only transaction) and where the server
+    lets it read and write files."""
+    if not file_granted([str(row[0]) for row in query(conn, "SHOW GRANTS")]):
+        return LoginPrivileges()
+    row = first_row(conn, "SELECT @@GLOBAL.secure_file_priv")
+    return LoginPrivileges(grants=["file"], secure_file_priv=row[0] if row else None)
+
+
 def list_schemas(conn: MySQLConnection) -> list[tuple[str, bool]]:
     """(name, is_system) of every schema the account may access: user schemas first, each group by name."""
     rows = query(conn, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
diff --git a/backend/dbaguy_backend/adapters/postgres/adapter.py b/backend/dbaguy_backend/adapters/postgres/adapter.py
index 329f6aa..b3c602a 100644
--- a/backend/dbaguy_backend/adapters/postgres/adapter.py
+++ b/backend/dbaguy_backend/adapters/postgres/adapter.py
@@ -10,6 +10,7 @@ import psycopg
 from loguru import logger
 from psycopg import postgres, pq
 
+from ...domain.agent import LoginPrivileges
 from ...domain.data_jobs import DumpObjectKinds, DumpScope
 from ...domain.edits import EditTarget
 from ...domain.errors import ConnectionFailed, ValidationFailed
@@ -90,6 +91,14 @@ _CONNECTION_LOST_STATES = frozenset({"57P01", "57P02", "57P03"})
 # Read-only profiles set the session default at connect and re-assert it before every statement: a SELECT can call
 # set_config() to switch the default off, whether written out, hidden in a string or inside a user function.
 _READ_ONLY_GUC = "default_transaction_read_only"
+# CR-08-1: the login's superuser bit and the four roles a read-only transaction does not stop (a superuser has all)
+_LOGIN_SQL = (
+    "SELECT r.rolsuper, pg_has_role(current_user, 'pg_read_server_files', 'MEMBER'), "
+    "pg_has_role(current_user, 'pg_write_server_files', 'MEMBER'), "
+    "pg_has_role(current_user, 'pg_execute_server_program', 'MEMBER'), "
+    "pg_has_role(current_user, 'pg_signal_backend', 'MEMBER') FROM pg_roles r WHERE r.rolname = current_user"
+)
+_LOGIN_GRANTS = ("superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends")
 _READ_ONLY_SESSION = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
 
 # libpq's chunked streaming uses the extended protocol, which accepts one statement per query.
@@ -430,6 +439,13 @@ class PostgresConnection(DatabaseConnection, RowEditing, DataTools, ReferenceRea
     def current_database(self) -> str | None:
         return self._conn.info.dbname
 
+    def login_privileges(self) -> LoginPrivileges:
+        try:
+            row = self._conn.execute(_LOGIN_SQL).fetchone()
+        except psycopg.Error as exc:
+            raise self.translate_error(exc, _LOGIN_SQL) from exc
+        return LoginPrivileges(grants=[g for g, held in zip(_LOGIN_GRANTS, row or (), strict=False) if held])
+
     def _check_database(self, path: list[PathSegment]) -> None:
         if path and path[0].name != self.current_database():
             raise ValidationFailed(f"Connection is bound to database '{self.current_database()}', not '{path[0].name}'")
diff --git a/backend/dbaguy_backend/agent/tools.py b/backend/dbaguy_backend/agent/tools.py
index e36eb9b..15e8a7a 100644
--- a/backend/dbaguy_backend/agent/tools.py
+++ b/backend/dbaguy_backend/agent/tools.py
@@ -23,6 +23,7 @@ from ..domain.schema import SchemaSnapshot, SnapshotRelation
 from ..services.metadata_service import MetadataService
 from ..services.profile_service import ProfileService
 from ..services.schema_cache import SchemaCache
+from .login import LoginRegistry
 
 LIST_LIMIT, SEARCH_LIMIT, LIMIT_MAX = 100, 50, 1000  # list_objects, search_objects (§3.5)
 _UNKNOWN = "No connection '{}' is shared with AI tools; list_connections lists them"
@@ -97,11 +98,12 @@ def _relation_of(snapshot: SchemaSnapshot, path: list[PathSegment]) -> SnapshotR
 
 class AgentTools:
     def __init__(self, profiles: ProfileService, registry: AdapterRegistry, metadata: MetadataService,
-                 schema_cache: SchemaCache) -> None:
+                 schema_cache: SchemaCache, login: LoginRegistry) -> None:
         self._profiles = profiles
         self._registry = registry
         self._metadata = metadata
         self._schema_cache = schema_cache
+        self._login = login
 
     # ---- connections ----
 
@@ -153,6 +155,7 @@ class AgentTools:
             "folder": profile.folder or None, "color": profile.color, "database": _database(profile),
             "read_only_by": "server" if adapter.capabilities.server_read_only else "dbaguy",
             "needs_unlock": self._needs_unlock(profile),
+            "login": self._login_of(profile),
         }
         if profile.engine == Engine.COUCHBASE:
             try:
@@ -169,6 +172,7 @@ class AgentTools:
         shared = self.shared(connection, "list_objects")
         path = path or []
         with answered(shared.profile):
+            self._login.ensure(shared.profile)
             nodes = self._metadata.children(MetadataRequest(profile_id=shared.profile.id, path=path), agent=True)
         logger.info("MCP list_objects on '{}' by {}: {} ({} objects)", shared.profile.name, client, _path_text(path),
                     len(nodes))
@@ -182,6 +186,7 @@ class AgentTools:
         shared = self.shared(connection, "search_objects")
         pid = shared.profile.id
         with answered(shared.profile):
+            self._login.ensure(shared.profile)
             hits = self._schema_cache.search(pid, pattern, database, limit, agent=True)
             found = [{"database": h.database, "schema": h.schema_name, "name": h.name, "kind": h.kind,
                       "path": [s.model_dump() for s in h.path]} for h in hits]
@@ -219,6 +224,7 @@ class AgentTools:
         shared = self.shared(connection, "describe")
         pid = shared.profile.id
         with answered(shared.profile):
+            self._login.ensure(shared.profile)
             if not path:
                 if not name:
                     raise ToolFailure("invalid_argument", _NO_OBJECT)
@@ -285,6 +291,13 @@ class AgentTools:
         db, schema_name, relation, kind = found[0]
         return shared.adapter.relation_path(db, schema_name, relation, kind)
 
+    def _login_of(self, profile: AnyProfile) -> dict[str, Any] | None:
+        """CR-08-1: what the login may do beyond reading, once an agent's connection has read it (null before)."""
+        privileges = self._login.peek(profile.id)
+        if privileges is None:
+            return None
+        return {**privileges.model_dump(exclude_none=True), "summary": privileges.summary()}
+
     def _needs_unlock(self, profile: AnyProfile) -> bool:
         """A password it would ask for: a profile that does not save its password and has none unlocked this run. One
         that saves it says False without reading the Keychain (an agent's list must never make the Keychain ask)."""
diff --git a/backend/dbaguy_backend/api/mcp.py b/backend/dbaguy_backend/api/mcp.py
index 7514f59..1f3c27e 100644
--- a/backend/dbaguy_backend/api/mcp.py
+++ b/backend/dbaguy_backend/api/mcp.py
@@ -1,10 +1,13 @@
-"""The MCP server's Settings API (spec ⑧ D-08-12): its state, the token and its Claude Code commands, a new token. The
-switch and the port are AppSettings.mcp (`PUT /settings`)."""
+"""The MCP server's Settings API (spec ⑧ D-08-12): its state, the token and its Claude Code commands, a new token, and
+a connection's login privileges for the connection dialog (CR-08-1). The switch and the port are AppSettings.mcp
+(`PUT /settings`)."""
+
+from uuid import UUID
 
 from fastapi import APIRouter, Depends
 
 from ..container import AppContainer
-from ..domain.agent import McpStatus, McpToken
+from ..domain.agent import LoginPrivileges, McpStatus, McpToken
 from .deps import get_container, require_token
 
 router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[Depends(require_token)])
@@ -23,3 +26,10 @@ def mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
 @router.post("/token/regenerate", response_model=McpToken)
 def regenerate_mcp_token(container: AppContainer = Depends(get_container)) -> McpToken:
     return container.agent_host.regenerate()
+
+
+@router.get("/connections/{profile_id}/login", response_model=LoginPrivileges | None)
+def connection_login(profile_id: UUID, container: AppContainer = Depends(get_container)) -> LoginPrivileges | None:
+    """Read now on the agents' read-only connection (a locked profile: 428 password_required, as every route); null for
+    an engine with nothing to tell (SQLite, Couchbase)."""
+    return container.agent_login.read(container.profiles.get(profile_id))
diff --git a/backend/dbaguy_backend/api/profiles.py b/backend/dbaguy_backend/api/profiles.py
index 412a7aa..0121327 100644
--- a/backend/dbaguy_backend/api/profiles.py
+++ b/backend/dbaguy_backend/api/profiles.py
@@ -18,11 +18,12 @@ router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(
 
 
 def _forget_profile_state(container: AppContainer, profile_id: UUID) -> None:
-    """After a profile changed or went away: drop what was learned from it: its metadata connections, cached schema
-    snapshots and field samples."""
+    """After a profile changed or went away: drop what was learned from it: its metadata connections (the agents' too),
+    cached schema snapshots, field samples and its login's privileges."""
     container.metadata.invalidate(profile_id)
     container.schema_cache.invalidate(profile_id)
     container.field_samples.invalidate(profile_id)
+    container.agent_login.forget(profile_id)
 
 
 @router.get("", response_model=list[ProfileEntry])
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index bac8cad..b6e9ddb 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -12,6 +12,7 @@ from .adapters.postgres.adapter import PostgresAdapter
 from .adapters.registry import AdapterRegistry
 from .adapters.sqlite.adapter import SqliteAdapter
 from .agent.host import AgentHost
+from .agent.login import LoginRegistry
 from .agent.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
 from .agent.tools import AgentTools
 from .config import Settings, default_data_dir
@@ -116,7 +117,8 @@ class AppContainer:
             self.transfers.plans,
         )
         self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
-        self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache)
+        self.agent_login = LoginRegistry(self.metadata)
+        self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache, self.agent_login)
         self.agent_host = AgentHost(
             self.agent_tools, token_store or _token_store(settings, secret_store), default_mcp_port(data_dir),
             on_stop=self._close_agents,
diff --git a/backend/dbaguy_backend/domain/agent.py b/backend/dbaguy_backend/domain/agent.py
index 8fafcc7..188e5c5 100644
--- a/backend/dbaguy_backend/domain/agent.py
+++ b/backend/dbaguy_backend/domain/agent.py
@@ -6,7 +6,8 @@ from pydantic import BaseModel
 
 from .profiles import McpAccess
 
-__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "APP_MCP_PORT", "DEV_MCP_PORT"]
+__all__ = ["McpAccess", "McpState", "McpStatus", "McpToken", "ToolErrorCode", "APP_MCP_PORT", "DEV_MCP_PORT",
+           "LoginGrant", "LoginPrivileges"]
 
 APP_MCP_PORT = 17531  # the app's own data dir (R-08-2)
 DEV_MCP_PORT = 17532  # any other data dir: dev/data, tests, a scratch sidecar
@@ -23,6 +24,33 @@ ToolErrorCode = Literal[
 ]
 
 
+# What a login may do beyond reading that a read-only transaction does not stop (CR-08-1, §3.3), in this order:
+# PostgreSQL superuser and the four pg_* roles; MySQL FILE; SQL Server writes in the database, sysadmin, bulkadmin
+LoginGrant = Literal[
+    "superuser", "server_files_read", "server_files_write", "server_programs", "signal_backends", "file", "can_write",
+    "sysadmin", "bulkadmin",
+]
+_GRANT_TEXT: dict[str, str] = {
+    "superuser": "is a superuser", "server_files_read": "read the server's files",
+    "server_files_write": "write the server's files", "server_programs": "run programs on the server",
+    "signal_backends": "end other sessions", "file": "read and write files on the server (FILE)",
+    "can_write": "write in this database", "sysadmin": "is a sysadmin", "bulkadmin": "bulk-load files (bulkadmin)",
+}
+
+
+class LoginPrivileges(BaseModel):
+    """The connection's login, as an agent's connection found it (CR-08-1): read once, kept until the profile changes.
+    Empty `grants`: it can do nothing a read-only transaction does not stop."""
+
+    grants: list[LoginGrant] = []
+    # MySQL with `file`: secure_file_priv: "" any directory, a path only that one, None (NULL) no file at all
+    secure_file_priv: str | None = None
+
+    def summary(self) -> str:
+        """`superuser, run programs on the server`, for the log and the agent: never a secret."""
+        return ", ".join(_GRANT_TEXT[grant] for grant in self.grants) or "nothing beyond reading"
+
+
 class McpStatus(BaseModel):
     state: McpState
     port: int  # the port it uses or would use
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_login.py tests/test_mcp_catalog.py tests/test_postgres_introspection.py tests/test_mysql_introspection.py tests/test_mssql_introspection.py tests/test_api.py`
Expected: all pass (planning: 195 passed in 12 s). The MySQL test leaves no `zz_08_u` or `zz_08_r` behind.

- [ ] **Step 5: Generate the API types; type-check**

Run: `npm run gen:api && npm run typecheck`
Expected: `openapi.json` +105 lines, `schema.d.ts` +68 lines (`LoginPrivileges`, the route); `tsc` is clean.

- [ ] **Step 6: Review against the pain points** (principle 5 / CR-08-1: what the read-only transaction does not stop is shown; Q3)

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/agent/login.py backend/dbaguy_backend/domain/agent.py \
  backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/adapters/postgres/adapter.py \
  backend/dbaguy_backend/adapters/mysql/adapter.py backend/dbaguy_backend/adapters/mysql/catalog.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py backend/dbaguy_backend/agent/tools.py \
  backend/dbaguy_backend/api/mcp.py backend/dbaguy_backend/api/profiles.py backend/dbaguy_backend/container.py \
  backend/tests/test_mcp_login.py src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(08a): the login's privileges beyond reading, read once and shown (CR-08-1)

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 4: The agent gate — `services/agent_gate.agent_read` (pure)

**Suggested implementer:** Opus. It is the one layer that stands alone on SQL Server, and every bypass in appendix A becomes a test here.

**Files:**
- Create:
  - `backend/dbaguy_backend/services/agent_gate.py`
  - `backend/tests/test_agent_gate.py`
- Modify:
  - `backend/dbaguy_backend/sql/dialects.py` (`EXPLAIN_HEADS`)
- Test: `backend/tests/test_agent_gate.py`

**Interfaces:**
- Consumes: `split`, `tokenize`, `INSIGNIFICANT`, `TokenKind`, `classify`, `tsql.statements`, `SafetyPolicy._allowed_when_read_only` (`is_plain_read`'s rule), `LONE_SURROGATE`.
- Produces:
  - `services/agent_gate.py`:
    - `AgentPurpose = Literal["query", "explain"]`
    - `AgentRefusal`: an enum of `invalid_text`, `empty`, `several_statements`, `explain_added`, `not_allowed_start`, `executable_comment`, `not_a_read`, `denied_function`, `unreadable_name`, `into`.
    - `AgentVerdict{refusal, detail, classification, starts}`, with `.allowed` and `.message()`.
    - `agent_read(sql, dialect, purpose="query") -> AgentVerdict`.
    - The tables `ALLOWED_STARTS` and `DENIED_FUNCTIONS`. Task 8 adds `SERVER_READ_ONLY` here.
  - `sql/dialects.EXPLAIN_HEADS: dict[Dialect, tuple[str, ...]]`: PG `EXPLAIN`; MySQL `EXPLAIN FORMAT = TREE`, then `EXPLAIN`; SQLite `EXPLAIN QUERY PLAN`; SQL++ `EXPLAIN`; T-SQL none.

**Decisions this task carries:**
- D-08-2, in order:
  1. No U+0000 and no lone surrogate.
  2. Exactly one statement: one span, no `;` inside it, nothing significant outside it but `;` (a GO or DELIMITER line is outside), and one T-SQL statement by `tsql.statements`.
  3. The first word is on `ALLOWED_STARTS` (CR-08-6). EXPLAIN, DESCRIBE and DESC get `explain_added`.
  4. A read by the classifier and the read-only rule.
  5. No call of a denied function.
  6. MySQL: no `INTO` word.
- MySQL's executable comments (P5): `/*! … */` and `/*+ … */` are refused outright. The deny-list and the INTO rule cannot look inside a version comment, which the server runs as code; the classifier calls `SELECT 1 /*!50000 , GET_LOCK('a', 1) */` UNKNOWN only because sqlglot fails on it.
  - An optimizer hint is harmless to lexing: probed, `SET_VAR(sql_mode='NO_BACKSLASH_ESCAPES')` does not change how its own statement's strings are read. But it can lift `max_execution_time`, and agents need no hints.
- How names are compared:
  - A call is a name (WORD, QUOTED, or PG `U&"…"` with an optional `UESCAPE '<c>'`) right before `(`, insignificant tokens allowed in between. The schema-qualified form is judged by its last part.
  - An unquoted name is compared without regard to case. A quoted PG name is compared exactly: `"PG_TERMINATE_BACKEND"` is another function, which does not exist. Other dialects' quoted names are compared without regard to case: stricter.
  - A `U&` name is decoded first (`\XXXX`, `\+XXXXXX`, a doubled escape character). A `U&` name that cannot be decoded is refused (`unreadable_name`).
- `explain`: the text must start with one of the dialect's `EXPLAIN_HEADS`, and the rest must pass as a query. `EXPLAIN ANALYZE` and DML fail at the first word (OP-6).
- The tables live in `services/agent_gate.py`, beside `services/safety.py`, whose rule they build on (P13: the spec's `SafetyPolicy.agent_read`). `EXPLAIN_HEADS` is in `sql/dialects.py` because Task 6's reader builds the explain text from it.
- The messages are §4's: `DbaGuy runs exactly one read statement per call: <why>`.

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_agent_gate.py`:**

```python
"""The agent gate (spec ⑧ D-08-2, CR-08-1, CR-08-4, CR-08-6): appendix A's G1–G10, H1–H3 and the Datadog variants, per
dialect, as regression tests. Pure."""

import pytest

from dbaguy_backend.services.agent_gate import AgentRefusal, agent_read
from dbaguy_backend.sql.dialects import Dialect

PG, MY, LITE, TS, PP = Dialect.POSTGRES, Dialect.MYSQL, Dialect.SQLITE, Dialect.TSQL, Dialect.SQLPP
R = AgentRefusal

ALLOWED = [
    (PG, "SELECT 1"), (PG, "select * from public.customers where id = 1;"),
    (PG, "WITH x AS (SELECT 1) SELECT * FROM x"),
    (PG, "VALUES (1), (2)"), (PG, "TABLE public.customers"), (PG, "SHOW search_path"),
    (PG, 'SELECT "PG_TERMINATE_BACKEND"(1)'),  # a quoted name keeps its case: another function, which does not exist
    (PG, "SELECT 'pg_terminate_backend(1)'"), (PG, "SELECT $$pg_read_file('x')$$"),  # text, never a call
    (PG, "SELECT pg_terminate_backend_note FROM t"),  # a column, not a call
    (PG, "SELECT /* nested /* comment */ ; */ 1"), (PG, "SELECT E'a\\'; DROP TABLE t; --'"),
    (MY, "SELECT * FROM customers"), (MY, "SHOW TABLES"), (MY, "TABLE customers"), (MY, "VALUES ROW(1)"),
    (MY, "SELECT `into` FROM t"), (MY, "SELECT 'a\\' INTO @x -- '"),  # INTO inside the string, as the server reads it
    (LITE, "SELECT * FROM sqlite_master"), (LITE, "VALUES (1)"),
    (LITE, "WITH RECURSIVE c(n) AS (SELECT 1) SELECT n FROM c"),
    (TS, "SELECT TOP 5 * FROM dbo.customers"), (TS, "WITH c AS (SELECT 1 AS a) SELECT * FROM c"),
    (PP, "SELECT * FROM `travel-sample`.inventory.airline LIMIT 5"), (PP, "WITH a AS (1) SELECT a"),
    (PP, 'SELECT META(d).id FROM app.users AS d USE KEYS "u1"'),
]


@pytest.mark.parametrize(("dialect", "sql"), ALLOWED)
def test_one_read_on_the_allow_list_passes(dialect, sql):
    verdict = agent_read(sql, dialect)
    assert verdict.allowed, verdict.message()


REFUSED = [
    # G1, G2, G6: stacked statements (Datadog: SELECT 1; COMMIT; DROP …), however they hide
    (PG, "SELECT 1; COMMIT; DROP TABLE zz_08_t", R.SEVERAL_STATEMENTS),
    (PG, "SELECT $$;$$; COMMIT", R.SEVERAL_STATEMENTS),
    (PG, "SELECT 1;\nCOMMIT", R.SEVERAL_STATEMENTS),
    (MY, "SELECT 1; COMMIT; DROP TABLE zz_08_t", R.SEVERAL_STATEMENTS),
    (MY, "DELIMITER $$\nSELECT 1$$", R.SEVERAL_STATEMENTS),
    (LITE, "SELECT 1; DELETE FROM t", R.SEVERAL_STATEMENTS),
    (PP, "SELECT 1; DELETE FROM app.users", R.SEVERAL_STATEMENTS),
    # G4, G5, T1: T-SQL needs no semicolon
    (TS, "SELECT 1 SELECT 2", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1 COMMIT", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1; COMMIT; DROP TABLE dbo.zz_08_victim", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1\nGO", R.SEVERAL_STATEMENTS),
    (TS, "SELECT 1\nGO 5", R.SEVERAL_STATEMENTS),
    # CR-08-6, H3: read-classified statements that are not queries
    (TS, "DECLARE zz_08_c CURSOR GLOBAL FOR SELECT 1", R.NOT_ALLOWED_START),
    (TS, "WAITFOR DELAY '00:00:05'", R.NOT_ALLOWED_START),
    (TS, "PRINT 'x'", R.NOT_ALLOWED_START),
    (TS, "THROW 50000, 'x', 1", R.NOT_ALLOWED_START),
    (TS, "OPEN zz_08_c", R.NOT_ALLOWED_START),
    (TS, "FETCH NEXT FROM zz_08_c", R.NOT_ALLOWED_START),
    (TS, "RAISERROR('x', 10, 1) WITH LOG", R.NOT_ALLOWED_START),
    (TS, "EXEC sp_who", R.NOT_ALLOWED_START),
    (LITE, "PRAGMA wal_checkpoint(TRUNCATE)", R.NOT_ALLOWED_START),
    (LITE, "PRAGMA query_only = 0", R.NOT_ALLOWED_START),
    (LITE, "ATTACH 'x.db' AS x", R.NOT_ALLOWED_START),
    (LITE, "VACUUM INTO '/tmp/zz_08.db'", R.NOT_ALLOWED_START),
    (PP, "INFER app.users", R.NOT_ALLOWED_START),
    (PP, "PREPARE zz_08_p FROM SELECT 1", R.NOT_ALLOWED_START),
    (PP, "ADVISE SELECT * FROM app.users", R.NOT_ALLOWED_START),
    (PG, "COPY (SELECT 1) TO '/dev/null'", R.NOT_ALLOWED_START),
    (PG, "COPY (SELECT 1) TO PROGRAM 'true'", R.NOT_ALLOWED_START),
    (PG, "SET default_transaction_read_only = off", R.NOT_ALLOWED_START),
    (PG, "DO $$BEGIN PERFORM 1; END$$", R.NOT_ALLOWED_START),
    (PG, "CALL zz_08_p()", R.NOT_ALLOWED_START),
    (MY, "CALL zz_08_p()", R.NOT_ALLOWED_START),
    (PG, "(SELECT 1)", R.NOT_ALLOWED_START),
    (PG, "EXPLAIN SELECT 1", R.EXPLAIN_ADDED), (MY, "DESCRIBE customers", R.EXPLAIN_ADDED),
    (MY, "DESC customers", R.EXPLAIN_ADDED), (PP, "EXPLAIN SELECT 1", R.EXPLAIN_ADDED),
    # H3-a: data-modifying CTEs are not reads
    (PG, "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", R.NOT_A_READ),
    (PG, "WITH d AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM d", R.NOT_A_READ),
    (MY, "WITH c AS (SELECT 1) DELETE FROM t", R.NOT_A_READ),
    (LITE, "WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c", R.NOT_A_READ),
    (TS, "WITH c AS (SELECT 1 AS a) DELETE FROM t", R.NOT_A_READ),
    (TS, "SELECT * INTO dbo.zz_08_new FROM dbo.customers", R.NOT_A_READ),
    (PP, "WITH x AS (1) DELETE FROM app.users", R.NOT_A_READ),
    (PG, "SELECT * FROM t FOR UPDATE", R.NOT_A_READ), (MY, "SELECT * FROM t LOCK IN SHARE MODE", R.NOT_A_READ),
    (TS, "SELECT NEXT VALUE FOR dbo.zz_08_seq", R.NOT_A_READ),  # T3: a sequence is not rolled back
    (TS, "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", R.NOT_A_READ),
    (PG, "SELECT set_config('default_transaction_read_only', 'off', false)", R.NOT_A_READ),
    # G3, Q1-P, CR-08-1: what a read-only transaction does not stop
    (PG, "SELECT pg_terminate_backend(1)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_cancel_backend(pid) FROM pg_stat_activity", R.DENIED_FUNCTION),
    (PG, "SELECT pg_advisory_lock(808)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_try_advisory_xact_lock(808)", R.DENIED_FUNCTION),
    (PG, "SELECT lo_export(0, '/tmp/zz_08')", R.DENIED_FUNCTION),
    (PG, "SELECT lo_import('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_read_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_read_binary_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT * FROM pg_ls_dir('.')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_ls_logdir()", R.DENIED_FUNCTION),
    (PG, "SELECT pg_stat_file('/etc/hostname')", R.DENIED_FUNCTION),
    (PG, "SELECT dblink_exec('x', 'DROP TABLE t')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_reload_conf()", R.DENIED_FUNCTION),
    (PG, "SELECT pg_create_restore_point('x')", R.DENIED_FUNCTION),
    (PG, "SELECT pg_catalog.pg_terminate_backend(1)", R.DENIED_FUNCTION),
    (PG, 'SELECT "pg_catalog"."pg_terminate_backend"(1)', R.DENIED_FUNCTION),
    (PG, "SELECT PG_TERMINATE_BACKEND (1)", R.DENIED_FUNCTION),
    (PG, "SELECT pg_terminate_backend/* x */(1)", R.DENIED_FUNCTION),
    (PG, "SELECT 1 WHERE EXISTS (SELECT pg_terminate_backend(pid) FROM pg_stat_activity)", R.DENIED_FUNCTION),
    # H1, CR-08-4: U& names (with UESCAPE) and SQL run from text
    (PG, 'SELECT U&"pg\\005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, 'SELECT u&"pg\\005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, "SELECT U&\"pg!005fterminate!005fbackend\" UESCAPE '!' (1)", R.DENIED_FUNCTION),
    (PG, 'SELECT U&"pg\\+00005fterminate\\005fbackend"(1)', R.DENIED_FUNCTION),
    (PG, 'SELECT U&"set\\005fconfig"(\'a\', \'b\', false)', R.DENIED_FUNCTION),
    (PG, 'SELECT U&"pg\\zzzzterminate"(1)', R.UNREADABLE_NAME),
    (PG, "SELECT query_to_xml('SELECT pg_terminate_backend(1)', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT query_to_xmlschema('SELECT 1', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT query_to_xml_and_xmlschema('SELECT 1', true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT cursor_to_xml('c', 1, true, true, '')", R.DENIED_FUNCTION),
    (PG, "SELECT * FROM ts_stat('SELECT to_tsvector(''simple'', 1::text)')", R.DENIED_FUNCTION),
    (PG, "SELECT ts_rewrite('a'::tsquery, 'SELECT 1')", R.DENIED_FUNCTION),
    # G8, M5, M6, H2: MySQL locks, files, INTO
    (MY, "SELECT GET_LOCK('zz_08', 1)", R.DENIED_FUNCTION),
    (MY, "SELECT release_all_locks()", R.DENIED_FUNCTION),
    (MY, "SELECT LOAD_FILE('/etc/hostname')", R.DENIED_FUNCTION),
    (MY, "SELECT `get_lock`('zz_08', 1)", R.DENIED_FUNCTION),
    (MY, "SELECT 1 INTO @zz_08", R.INTO),
    (MY, "SELECT 1 INTO OUTFILE '/tmp/zz_08'", R.NOT_A_READ),
    (MY, "SELECT 1 FROM dual INTO DUMPFILE '/tmp/zz_08'", R.NOT_A_READ),
    (MY, "SELECT * FROM customers LIMIT 1 INTO @a, @b, @c, @d, @e, @f", R.INTO),
    (MY, "SELECT 1 /*!50000 , GET_LOCK('a', 1) */", R.EXECUTABLE_COMMENT),
    (MY, "SELECT /*+ SET_VAR(max_execution_time=0) */ 1", R.EXECUTABLE_COMMENT),
    (MY, "/*!50000 DROP TABLE t */", R.NOT_ALLOWED_START),
    # G9, S6: SQLite
    (LITE, "SELECT load_extension('x')", R.DENIED_FUNCTION),
    # G10, C3, C4, H3-d: SQL++
    (PP, "SELECT CURL('http://127.0.0.1')", R.DENIED_FUNCTION),
    (PP, 'SELECT ADVISOR({"action": "start", "duration": "2s"})', R.DENIED_FUNCTION),
    (PP, "UPDATE STATISTICS FOR app.users(name)", R.NOT_ALLOWED_START),
    (PP, "EXECUTE FUNCTION zz_08_f()", R.NOT_ALLOWED_START),
    (PP, "SELECT NEXT VALUE FOR zz_08_seq", R.NOT_A_READ),
    # D-08-2 ①: text a driver would cut or cannot send
    (PG, "SELECT 1\x00; DROP TABLE t", R.INVALID_TEXT), (MY, "SELECT '\ud800'", R.INVALID_TEXT),
    (PG, "", R.EMPTY), (PG, " -- only a comment\n", R.EMPTY), (TS, ";", R.EMPTY),
]


@pytest.mark.parametrize(("dialect", "sql", "refusal"), REFUSED)
def test_what_a_read_only_transaction_does_not_stop_is_refused(dialect, sql, refusal):
    verdict = agent_read(sql, dialect)
    assert verdict.refusal == refusal, verdict.message()
    assert verdict.message().startswith("DbaGuy runs exactly one read statement per call: ")


def test_the_messages_say_why():
    assert agent_read("DECLARE c CURSOR FOR SELECT 1", TS).message() == (
        "DbaGuy runs exactly one read statement per call: starts with DECLARE: only SELECT, WITH are run")
    assert agent_read("SELECT pg_ls_dir('.')", PG).message().endswith("pg_ls_dir (server files) is not allowed")
    assert agent_read("SELECT pg_advisory_lock(1)", PG).message().endswith("pg_advisory_lock (locks) is not allowed")
    assert agent_read("SELECT query_to_xml('x', true, true, '')", PG).message().endswith(
        "query_to_xml (runs SQL from text) is not allowed")
    assert agent_read("WITH d AS (DELETE FROM t RETURNING *) SELECT 1", PG).message().endswith("not a read (write)")
    assert agent_read("SELECT 1 INTO @x", MY).message().endswith("INTO is not allowed")
    assert agent_read("SHOW TABLES", LITE).message().endswith("starts with SHOW: only SELECT, WITH, VALUES are run")


@pytest.mark.parametrize(("dialect", "sql"), [
    (PG, "EXPLAIN SELECT * FROM t"), (MY, "EXPLAIN FORMAT=TREE SELECT * FROM t"), (MY, "EXPLAIN SELECT 1"),
    (LITE, "EXPLAIN QUERY PLAN SELECT * FROM t"), (PP, "EXPLAIN SELECT * FROM app.users"),
])
def test_explain_takes_the_tools_own_head_over_one_allowed_read(dialect, sql):
    assert agent_read(sql, dialect, "explain").allowed


@pytest.mark.parametrize(("dialect", "sql", "refusal"), [
    (PG, "EXPLAIN ANALYZE SELECT 1", R.NOT_ALLOWED_START),  # OP-6: no ANALYZE, no DML
    (PG, "EXPLAIN INSERT INTO t VALUES (1)", R.NOT_ALLOWED_START),
    (PG, "EXPLAIN SELECT 1; DROP TABLE t", R.SEVERAL_STATEMENTS),
    (PG, "SELECT 1", R.NOT_ALLOWED_START),
    (MY, "EXPLAIN SELECT GET_LOCK('a', 1)", R.DENIED_FUNCTION),
    (TS, "SELECT 1", R.NOT_ALLOWED_START),  # T-SQL's explain sends the statement itself, as a query
])
def test_explain_refuses_anything_else(dialect, sql, refusal):
    assert agent_read(sql, dialect, "explain").refusal == refusal
```

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_gate.py`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.services.agent_gate'`.

- [ ] **Step 3: Write the code**

**New file `backend/dbaguy_backend/services/agent_gate.py`:**

```python
"""The agent gate (spec ⑧ D-08-2, CR-08-1, CR-08-4, CR-08-6): what an MCP agent's statement must be before anything is
sent. Pure. In order: text a driver can take whole; exactly one statement; a first word on the dialect's allow-list; a
read by the classifier and the read-only allow-list (`is_plain_read`); no function on the deny-list (a PostgreSQL
`U&"…"` name decoded first, with its UESCAPE); MySQL: no INTO and no executable comment. A server's read-only
transaction stops writes to the database; these rules close what it does not stop: the server's files and programs,
other sessions, session locks, SQL run from text, settings.

Function names are compared as the server reads them: an unquoted name without regard to case; a quoted PostgreSQL name
exactly (`"PG_TERMINATE_BACKEND"` names another function, which does not exist); every other dialect's quoted names
without regard to case too. A schema-qualified call is judged by its last part."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ..domain.classification import Classification, StatementKind
from ..domain.well_formed import LONE_SURROGATE
from ..sql.classifier import classify
from ..sql.dialects import EXPLAIN_HEADS, Dialect
from ..sql.lexer import INSIGNIFICANT, Token, TokenKind, tokenize
from ..sql.splitter import split
from ..sql.tsql import statements
from .safety import SafetyPolicy

AgentPurpose = Literal["query", "explain"]

# CR-08-6: what an agent's statement may start with. EXPLAIN / DESCRIBE come from the explain tool only.
ALLOWED_STARTS: dict[Dialect, tuple[str, ...]] = {
    Dialect.POSTGRES: ("SELECT", "WITH", "VALUES", "TABLE", "SHOW"),
    Dialect.MYSQL: ("SELECT", "WITH", "VALUES", "TABLE", "SHOW"),
    Dialect.SQLITE: ("SELECT", "WITH", "VALUES"),
    Dialect.TSQL: ("SELECT", "WITH"),
    Dialect.SQLPP: ("SELECT", "WITH"),
}
_EXPLAIN_WORDS = frozenset({"EXPLAIN", "DESCRIBE", "DESC"})

# The deny-list (D-08-2 ⑤): lowercase names, a trailing `*` matching any name with that prefix, each with what it does.
_SERVER_FILES, _KILLS, _LOCKS, _SQL_TEXT = "server files", "kills sessions", "locks", "runs SQL from text"
_SERVER, _SETTINGS, _CONNECTIONS = "controls the server", "changes settings", "opens other connections"
DENIED_FUNCTIONS: dict[Dialect, tuple[tuple[str, str], ...]] = {
    Dialect.POSTGRES: (
        ("pg_terminate_backend", _KILLS), ("pg_cancel_backend", _KILLS),
        ("pg_advisory_*", _LOCKS), ("pg_try_advisory_*", _LOCKS),
        ("pg_reload_conf", _SERVER), ("pg_rotate_logfile", _SERVER), ("pg_switch_wal", _SERVER),
        ("pg_promote", _SERVER), ("pg_create_*", _SERVER), ("pg_drop_*", _SERVER), ("pg_replication_*", _SERVER),
        ("pg_log_*", _SERVER), ("set_config", _SETTINGS), ("dblink*", _CONNECTIONS), ("pg_file_*", _SERVER_FILES),
        ("pg_read_file", _SERVER_FILES), ("pg_read_binary_file", _SERVER_FILES), ("pg_ls_*", _SERVER_FILES),
        ("pg_stat_file", _SERVER_FILES), ("lo_*", _SERVER_FILES),
        ("query_to_xml", _SQL_TEXT), ("query_to_xmlschema", _SQL_TEXT), ("query_to_xml_and_xmlschema", _SQL_TEXT),
        ("cursor_to_xml", _SQL_TEXT), ("ts_stat", _SQL_TEXT), ("ts_rewrite", _SQL_TEXT),
    ),
    Dialect.MYSQL: (
        ("get_lock", _LOCKS), ("release_lock", _LOCKS), ("release_all_locks", _LOCKS), ("load_file", _SERVER_FILES),
    ),
    Dialect.SQLITE: (("load_extension", "loads code"),),
    Dialect.TSQL: (),
    Dialect.SQLPP: (("curl", "reaches the network"), ("advisor", "starts index advisor sessions")),
}
_QUOTED_NAMES_KEEP_CASE = frozenset({Dialect.POSTGRES})
_HEX = re.compile(r"[0-9A-Fa-f]+")


class AgentRefusal(StrEnum):
    INVALID_TEXT = "invalid_text"
    EMPTY = "empty"
    SEVERAL_STATEMENTS = "several_statements"
    EXPLAIN_ADDED = "explain_added"
    NOT_ALLOWED_START = "not_allowed_start"
    EXECUTABLE_COMMENT = "executable_comment"
    NOT_A_READ = "not_a_read"
    DENIED_FUNCTION = "denied_function"
    UNREADABLE_NAME = "unreadable_name"
    INTO = "into"


@dataclass(frozen=True)
class AgentVerdict:
    refusal: AgentRefusal | None = None
    detail: str = ""  # the word, the function and what it does, the kind: for the message
    classification: Classification | None = None
    starts: str = ""  # NOT_ALLOWED_START: what may start a statement

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    def message(self) -> str:
        """§4: `DbaGuy runs exactly one read statement per call: <why>`."""
        return f"DbaGuy runs exactly one read statement per call: {self._why()}"

    def _why(self) -> str:
        match self.refusal:
            case AgentRefusal.INVALID_TEXT:
                return "the text holds a character a driver cannot send (U+0000 or half of a surrogate pair)"
            case AgentRefusal.EMPTY:
                return "there is no statement"
            case AgentRefusal.SEVERAL_STATEMENTS:
                return "several statements"
            case AgentRefusal.EXPLAIN_ADDED:
                return f"{self.detail} is added by the explain tool"
            case AgentRefusal.NOT_ALLOWED_START:
                return f"starts with {self.detail}: only {self.starts} are run"
            case AgentRefusal.EXECUTABLE_COMMENT:
                return "executable comments (/*! … */, /*+ … */) are not allowed"
            case AgentRefusal.NOT_A_READ:
                return f"not a read ({self.detail})"
            case AgentRefusal.DENIED_FUNCTION | AgentRefusal.UNREADABLE_NAME:
                return f"{self.detail} is not allowed"
            case AgentRefusal.INTO:
                return "INTO is not allowed"
        return "allowed"


def _refuse(refusal: AgentRefusal, detail: str = "", classification: Classification | None = None,
            starts: str = "") -> AgentVerdict:
    return AgentVerdict(refusal, detail, classification, starts)


def agent_read(sql: str, dialect: Dialect, purpose: AgentPurpose = "query") -> AgentVerdict:
    """D-08-2: allowed, or why not. `explain`: `sql` is one of EXPLAIN_HEADS followed by a statement this gate allows
    as a query."""
    if "\x00" in sql or LONE_SURROGATE.search(sql):
        return _refuse(AgentRefusal.INVALID_TEXT)
    tokens = _one_statement(sql, dialect)
    if isinstance(tokens, AgentVerdict):
        return tokens
    if purpose == "explain":
        rest = _after_explain_head(tokens, dialect)
        if rest is None:
            return _refuse(AgentRefusal.NOT_ALLOWED_START, tokens[0].text[:40],
                           starts="the explain tool's own EXPLAIN")
        return agent_read(sql[rest:], dialect, "query")
    return _query(sql, tokens, dialect)


def _one_statement(sql: str, dialect: Dialect) -> list[Token] | AgentVerdict:
    """The statement's significant tokens (a trailing `;` dropped) when `sql` is exactly one statement: one span, no `;`
    inside it, nothing significant outside it but a `;` (a GO or DELIMITER line is), one T-SQL statement."""
    spans = split(sql, dialect)
    if not spans:
        return _refuse(AgentRefusal.EMPTY)
    if len(spans) > 1:
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    span = spans[0]
    significant = [t for t in tokenize(sql, dialect) if t.kind not in INSIGNIFICANT]
    inside = [t for t in significant if span.start <= t.start < span.end]
    if any(t.kind != TokenKind.SEMI for t in significant if not span.start <= t.start < span.end):
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    while inside and inside[-1].kind == TokenKind.SEMI:
        inside.pop()
    if not inside:
        return _refuse(AgentRefusal.EMPTY)
    if any(t.kind == TokenKind.SEMI for t in inside):
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    if dialect == Dialect.TSQL and len(statements(inside)) != 1:
        return _refuse(AgentRefusal.SEVERAL_STATEMENTS)
    return inside


def _after_explain_head(tokens: list[Token], dialect: Dialect) -> int | None:
    """Where the statement after the explain tool's head starts, or None when `tokens` does not start with one."""
    for head in EXPLAIN_HEADS[dialect]:
        words = head.split()
        if len(tokens) > len(words) and all(t.upper == word for t, word in zip(tokens, words, strict=False)):
            return tokens[len(words)].start
    return None


def _query(sql: str, tokens: list[Token], dialect: Dialect) -> AgentVerdict:
    first = tokens[0]
    word = first.upper if first.kind == TokenKind.WORD else ""
    if word in _EXPLAIN_WORDS:
        return _refuse(AgentRefusal.EXPLAIN_ADDED, word)
    allowed = ALLOWED_STARTS[dialect]
    if word not in allowed:
        return _refuse(AgentRefusal.NOT_ALLOWED_START, word or first.text[:40], starts=", ".join(allowed))
    if any(t.kind == TokenKind.EXEC_COMMENT for t in tokens):
        return _refuse(AgentRefusal.EXECUTABLE_COMMENT)
    classification = classify(sql, dialect)
    if classification.kind != StatementKind.READ or not SafetyPolicy._allowed_when_read_only(
            sql, classification, dialect):
        return _refuse(AgentRefusal.NOT_A_READ, classification.kind.value, classification)
    if (denied := _denied_call(tokens, dialect)) is not None:
        return denied
    if dialect == Dialect.MYSQL and any(t.kind == TokenKind.WORD and t.upper == "INTO" for t in tokens):
        return _refuse(AgentRefusal.INTO, "", classification)
    return AgentVerdict(classification=classification)


def _denied_call(tokens: list[Token], dialect: Dialect) -> AgentVerdict | None:
    """The first call of a denied function: a name (or a PostgreSQL `U&"…"` name and its UESCAPE) right before `(`."""
    denied = DENIED_FUNCTIONS[dialect]
    if not denied:
        return None
    i = 0
    while i < len(tokens):
        name, after = _name_at(tokens, i, dialect)
        if after > i and after < len(tokens) and tokens[after].kind == TokenKind.LPAREN:
            if name is None:
                return _refuse(AgentRefusal.UNREADABLE_NAME, "a U& name that cannot be read")
            if (what := _denied_as(name, denied)) is not None:
                return _refuse(AgentRefusal.DENIED_FUNCTION, f"{name} ({what})")
        i = max(after, i + 1)
    return None


def _denied_as(name: str, denied: tuple[tuple[str, str], ...]) -> str | None:
    for pattern, what in denied:
        if (name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern):
            return what
    return None


def _name_at(tokens: list[Token], i: int, dialect: Dialect) -> tuple[str | None, int]:
    """The function name a call at `tokens[i]` would use, as the deny-list is written (lowercase unless a quoted
    PostgreSQL name), and the index after it; (None, i) when no name starts there; (None, j) for a U& name that cannot
    be decoded."""
    token = tokens[i]
    if (dialect == Dialect.POSTGRES and token.kind == TokenKind.WORD and token.upper == "U" and i + 2 < len(tokens)
            and tokens[i + 1].text == "&" and tokens[i + 1].start == token.end
            and tokens[i + 2].kind == TokenKind.QUOTED and tokens[i + 2].start == tokens[i + 1].end):
        body, after, escape = tokens[i + 2], i + 3, "\\"
        if after + 1 < len(tokens) and tokens[after].upper == "UESCAPE" and tokens[after + 1].kind == TokenKind.STRING:
            escape = tokens[after + 1].text[1:-1].replace("''", "'")
            after += 2
        return _unicode_name(_unquoted(body), escape), after
    if token.kind == TokenKind.WORD:
        return token.text.lower(), i + 1
    if token.kind == TokenKind.QUOTED:
        name = _unquoted(token)
        return (name if dialect in _QUOTED_NAMES_KEEP_CASE else name.lower()), i + 1
    return None, i


def _unquoted(token: Token) -> str:
    """A quoted identifier's name: its quotes off, a doubled closing quote as one."""
    close = token.text[-1] if token.terminated else ""
    body = token.text[1:-1] if token.terminated else token.text[1:]
    return body.replace(close * 2, close) if close else body


def _unicode_name(body: str, escape: str) -> str | None:
    """A PostgreSQL `U&"…"` name decoded: `<e>XXXX`, `<e>+XXXXXX` and `<e><e>` (the escape character, `\\` unless
    UESCAPE names another). None when PostgreSQL would refuse it, or the escape is not one character."""
    if len(escape) != 1 or escape in "+'\"" or escape.isspace() or _HEX.fullmatch(escape):
        return None
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != escape:
            out.append(char)
            i += 1
            continue
        if body[i + 1:i + 2] == escape:
            out.append(escape)
            i += 2
            continue
        digits = 6 if body[i + 1:i + 2] == "+" else 4
        start = i + (2 if digits == 6 else 1)
        code = body[start:start + digits]
        if len(code) != digits or not _HEX.fullmatch(code) or int(code, 16) > 0x10FFFF:
            return None
        out.append(chr(int(code, 16)))
        i = start + digits
    return "".join(out)
```

**Patch T4** (the files this task modifies: `backend/dbaguy_backend/sql/dialects.py`). Save the block in your scratchpad as `08a-t4.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t4.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/sql/dialects.py b/backend/dbaguy_backend/sql/dialects.py
index 7e23bf3..37b1ae2 100644
--- a/backend/dbaguy_backend/sql/dialects.py
+++ b/backend/dbaguy_backend/sql/dialects.py
@@ -472,6 +472,18 @@ _UPDATE_VERBS: dict[Dialect, str] = {Dialect.SQLITE: "UPDATE OR ABORT"}
 _INSERT_VERBS: dict[Dialect, str] = {Dialect.SQLITE: "INSERT OR ABORT INTO"}
 
 
+# The heads the explain tool puts in front of an agent's statement (spec ⑧ D-08-10), each as words the lexer reads one
+# by one (`=` is a token of its own), the first the one used: MySQL's tree format is 8.0.16's, so an older server or
+# MariaDB gets the plain head. T-SQL has none: its plan comes from SET SHOWPLAN_TEXT around the statement.
+EXPLAIN_HEADS: dict[Dialect, tuple[str, ...]] = {
+    Dialect.POSTGRES: ("EXPLAIN",),
+    Dialect.MYSQL: ("EXPLAIN FORMAT = TREE", "EXPLAIN"),
+    Dialect.SQLITE: ("EXPLAIN QUERY PLAN",),
+    Dialect.TSQL: (),
+    Dialect.SQLPP: ("EXPLAIN",),
+}
+
+
 def transaction_frame(dialect: Dialect) -> TransactionFrame:
     frame = _TRANSACTION_FRAMES[dialect]
     if frame is None:
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_agent_gate.py tests/test_safety.py`
Expected: all pass (planning: 551 passed in 0.5 s; the gate's own file has 141).

- [ ] **Step 5: Review against the pain points** (principle 5 and CR-08-1/4/6: every row of appendix A's G and H is a case here)

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/services/agent_gate.py backend/dbaguy_backend/sql/dialects.py \
  backend/tests/test_agent_gate.py
git commit -m "feat(08a): the agent gate: one statement, allowed first words, a read, no denied function (U& decoded), no MySQL INTO

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 5: History — `source`, `client`, `repeats`, `refused`, a cap per source

**Suggested implementer:** Sonnet. A schema upgrade, a merge rule and per-source pruning, with pure tests.

**Files:**
- Modify:
  - `backend/dbaguy_backend/domain/history.py`
  - `backend/dbaguy_backend/services/history_service.py`
  - `backend/dbaguy_backend/api/history.py`
  - `backend/tests/test_history.py`
  - `src/features/history/HistoryPanel.tsx`
  - `src/lib/api/{openapi.json, schema.d.ts}` (by `gen:api`)
- Test: `backend/tests/test_history.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `domain/history.py`:
    - `HistoryStatus` gains `"refused"`.
    - `HistorySource = Literal["app", "mcp"]`.
    - `HistoryRecord.source: HistorySource = "app"`, `.client: str | None = None`, `.repeats: int = 1` (and `HistoryEntry` through it).
  - `services/history_service.py`:
    - `HistoryService(path, retention=APP_RETENTION, prune_every=500, mcp_retention=MCP_RETENTION, clock=time.monotonic)`.
    - `.refused(rec) -> int`: writes with status `refused`, or adds to the last record's `repeats`.
    - `.search(…, source=None)`.
    - `APP_RETENTION = 50_000`, `MCP_RETENTION = 5_000`, `REFUSAL_MERGE_S = 60.0`.
  - `GET /history?source=app|mcp`.

**Decisions this task carries:**
- D-08-13, CR-08-3:
  - Three columns, `source TEXT NOT NULL DEFAULT 'app'`, `client TEXT` and `repeats INTEGER NOT NULL DEFAULT 1`, plus the index `history_by_source (source, id)`.
  - A file from before ⑧ is upgraded in place when it opens (`PRAGMA table_info`, `ALTER TABLE … ADD COLUMN`). Its rows are the user's.
- Pruning is per source: each keeps its newest `retention` / `mcp_retention` rows (`… ORDER BY id DESC LIMIT 1 OFFSET keep - 1`). An agent in a loop never pushes out the user's records.
- Refusals: the same (profile, client, text) refused less than 60 s after the last one adds to that record's `repeats`.
  - The window counts from the last refusal, not the first, so a loop that keeps going stays one record.
  - The memory of the last few is in-process (64 kept).
  - A record pruned or cleared meanwhile gets a new one.
- The frontend's `STATUS` map needs a `refused` entry: the map is a `Record` over `HistoryStatus`, so `tsc` fails without it. It gets `ShieldBan`, "Refused", `text-danger`. 08b adds the MCP mark, the Source filter, ×N and Copy only.

- [ ] **Step 1: Write the tests** (appended to `tests/test_history.py`; in the patch below)

The three new tests:
- `test_a_file_from_before_08_gets_the_new_columns_in_place`
- `test_each_source_keeps_its_own_newest_records`
- `test_a_refusal_sent_again_within_a_minute_adds_to_its_record`

- [ ] **Step 2: Write the code** (the tests ride in the same patch)

**Patch T5** (the files this task modifies: `backend/dbaguy_backend/api/history.py`, `backend/dbaguy_backend/domain/history.py`, `backend/dbaguy_backend/services/history_service.py`, `backend/tests/test_history.py`, `src/features/history/HistoryPanel.tsx`). Save the block in your scratchpad as `08a-t5.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t5.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/api/history.py b/backend/dbaguy_backend/api/history.py
index 7188588..b0b609b 100644
--- a/backend/dbaguy_backend/api/history.py
+++ b/backend/dbaguy_backend/api/history.py
@@ -3,7 +3,7 @@ from uuid import UUID
 from fastapi import APIRouter, Depends, Query, Response
 
 from ..container import AppContainer
-from ..domain.history import HistoryPage
+from ..domain.history import HistoryPage, HistorySource
 from .deps import get_container, require_token
 
 router = APIRouter(prefix="/history", tags=["history"], dependencies=[Depends(require_token)])
@@ -15,9 +15,10 @@ def search_history(
     profile_id: UUID | None = None,
     limit: int = Query(default=100, ge=1, le=1000),
     before: int | None = None,
+    source: HistorySource | None = None,  # app: the user's; mcp: AI tools' (spec ⑧ §3.7)
     container: AppContainer = Depends(get_container),
 ) -> HistoryPage:
-    return container.couchbase.with_query_contexts(container.history.search(q, profile_id, limit, before))
+    return container.couchbase.with_query_contexts(container.history.search(q, profile_id, limit, before, source))
 
 
 @router.delete("", status_code=204)
diff --git a/backend/dbaguy_backend/domain/history.py b/backend/dbaguy_backend/domain/history.py
index d5d9300..5aa7386 100644
--- a/backend/dbaguy_backend/domain/history.py
+++ b/backend/dbaguy_backend/domain/history.py
@@ -7,8 +7,10 @@ from pydantic import BaseModel
 from .execution import QueryContext
 
 # running: a Run SQL file or a copy that started and has not ended (D-06-9); interrupted: one that was still running
-# when the backend ended (set at the next start)
-HistoryStatus = Literal["ok", "error", "cancelled", "timeout", "running", "interrupted"]
+# when the backend ended (set at the next start); refused: an MCP agent's statement the gate turned away (spec ⑧ OP-7)
+HistoryStatus = Literal["ok", "error", "cancelled", "timeout", "running", "interrupted", "refused"]
+# Who ran it: the user in DbaGuy, or an AI tool through the MCP server (spec ⑧ D-08-13)
+HistorySource = Literal["app", "mcp"]
 
 
 class HistoryRecord(BaseModel):
@@ -23,6 +25,9 @@ class HistoryRecord(BaseModel):
     rows: int
     status: HistoryStatus
     error: str | None = None
+    source: HistorySource = "app"
+    client: str | None = None  # mcp: the name the client gave for itself (self-reported, D-08-7)
+    repeats: int = 1  # a refused statement the same client sent again within a minute (CR-08-3)
 
 
 class HistoryEntry(HistoryRecord):
diff --git a/backend/dbaguy_backend/services/history_service.py b/backend/dbaguy_backend/services/history_service.py
index 6a6a182..f675c53 100644
--- a/backend/dbaguy_backend/services/history_service.py
+++ b/backend/dbaguy_backend/services/history_service.py
@@ -1,12 +1,15 @@
 import sqlite3
 import threading
+import time
+from collections import OrderedDict
+from collections.abc import Callable
 from pathlib import Path
 from uuid import UUID
 
 from loguru import logger
 
 from ..domain.errors import StorageError
-from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord, HistoryStatus
+from ..domain.history import HistoryEntry, HistoryPage, HistoryRecord, HistorySource, HistoryStatus
 from ..storage.json_files import move_aside
 
 _SCHEMA = """
@@ -20,7 +23,10 @@ CREATE TABLE IF NOT EXISTS history (
     elapsed_ms INTEGER NOT NULL,
     rows INTEGER NOT NULL,
     status TEXT NOT NULL,
-    error TEXT
+    error TEXT,
+    source TEXT NOT NULL DEFAULT 'app',
+    client TEXT,
+    repeats INTEGER NOT NULL DEFAULT 1
 );
 CREATE INDEX IF NOT EXISTS history_by_profile ON history (profile_id, id);
 CREATE VIRTUAL TABLE IF NOT EXISTS history_fts
@@ -32,7 +38,18 @@ CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
     INSERT INTO history_fts (history_fts, rowid, sql) VALUES ('delete', old.id, old.sql);
 END;
 """
-_DATA_COLUMNS = "profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status, error"
+_DATA_COLUMNS = (
+    "profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status, error, source, client, repeats"
+)
+# spec ⑧ D-08-13: columns an older file lacks, added in place when it is opened
+_ADDED_COLUMNS = (
+    ("source", "TEXT NOT NULL DEFAULT 'app'"), ("client", "TEXT"), ("repeats", "INTEGER NOT NULL DEFAULT 1"),
+)
+_SOURCE_INDEX = "CREATE INDEX IF NOT EXISTS history_by_source ON history (source, id)"
+APP_RETENTION = 50_000
+MCP_RETENTION = 5_000  # CR-08-3: an agent in a loop never pushes out the user's own records
+REFUSAL_MERGE_S = 60.0  # CR-08-3: the same refused text from the same client within this long adds to one record
+_REFUSALS_KEPT = 64
 _MAX_ERROR = 500
 _TRIGRAM = 3  # the full-text index matches from this many characters; shorter searches scan with LIKE
 _SQLITE_COMPANIONS = ("-wal", "-shm")  # a database's write-ahead log and its index: they go wherever it goes
@@ -43,10 +60,11 @@ def _like_pattern(q: str) -> str:
 
 
 def _entry(row: tuple) -> HistoryEntry:
-    id_, profile_id, profile_name, database, sql, started_at, elapsed_ms, rows, status, error = row
+    (id_, profile_id, profile_name, database, sql, started_at, elapsed_ms, rows, status, error, source, client,
+     repeats) = row
     return HistoryEntry(
         id=id_, profile_id=profile_id, profile_name=profile_name, database=database, sql=sql, started_at=started_at,
-        elapsed_ms=elapsed_ms, rows=rows, status=status, error=error,
+        elapsed_ms=elapsed_ms, rows=rows, status=status, error=error, source=source, client=client, repeats=repeats,
     )
 
 
@@ -56,12 +74,20 @@ class HistoryService:
     One connection shared by every thread, serialised by a lock, opened by the first use: building the app touches no
     file, and a history that cannot be opened fails the calls that need it, not the start of the app. A file that is
     not a database is moved aside and replaced by an empty one; other open failures (locked, read-only, no FTS5
-    trigram) leave the file alone.
+    trigram) leave the file alone. A file from before ⑧ gets its new columns in place.
+
+    The user's records and MCP agents' records are kept apart (spec ⑧ D-08-13): each source keeps its own newest
+    `retention` / `mcp_retention`.
     """
 
-    def __init__(self, path: Path, retention: int = 50_000, prune_every: int = 500) -> None:
+    def __init__(self, path: Path, retention: int = APP_RETENTION, prune_every: int = 500,
+                 mcp_retention: int = MCP_RETENTION, clock: Callable[[], float] = time.monotonic) -> None:
         self._path = path
         self._retention = retention
+        self._mcp_retention = mcp_retention
+        self._clock = clock
+        # The last refusal of each (profile, client, text): its record and when (CR-08-3); the newest few only
+        self._refusals: OrderedDict[tuple[str, str | None, str], tuple[int, float]] = OrderedDict()
         self._prune_every = prune_every
         self._inserts = 0
         self._lock = threading.Lock()
@@ -107,11 +133,22 @@ class HistoryService:
         try:
             conn.execute("PRAGMA journal_mode=WAL")
             conn.executescript(_SCHEMA)
+            self._upgrade(conn)
         except sqlite3.DatabaseError:
             conn.close()
             raise
         return conn
 
+    @staticmethod
+    def _upgrade(conn: sqlite3.Connection) -> None:
+        """Add the columns a file from before ⑧ lacks (its rows are the user's: `source` 'app'), then their index."""
+        have = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
+        for name, declaration in _ADDED_COLUMNS:
+            if name not in have:
+                conn.execute(f"ALTER TABLE history ADD COLUMN {name} {declaration}")
+                logger.info("History: added the column {}", name)
+        conn.execute(_SOURCE_INDEX)
+
     def record(self, rec: HistoryRecord) -> None:
         self._insert(rec)
 
@@ -127,36 +164,68 @@ class HistoryService:
                 (status, elapsed_ms, rows, error[:_MAX_ERROR] if error else None, record_id),
             )
 
-    def _insert(self, rec: HistoryRecord) -> int:
-        error = rec.error[:_MAX_ERROR] if rec.error else None
+    def refused(self, rec: HistoryRecord) -> int:
+        """An MCP agent's refused statement (status `refused`, CR-08-3): when the same client sent the same text to the
+        same connection less than REFUSAL_MERGE_S ago, that record's `repeats` grows instead of a new one being written.
+        Its id."""
+        key = (str(rec.profile_id), rec.client, rec.sql)
+        now = self._clock()
         with self._lock:
             conn = self._connection()
-            cursor = conn.execute(
-                f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
-                (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
-                 rec.elapsed_ms, rec.rows, rec.status, error),
-            )
-            self._inserts += 1
-            if self._inserts % self._prune_every == 0:
-                self._prune(conn)
-            return int(cursor.lastrowid or 0)
+            last = self._refusals.get(key)
+            if last is not None and now - last[1] < REFUSAL_MERGE_S and conn.execute(
+                    "UPDATE history SET repeats = repeats + 1 WHERE id = ?", (last[0],)).rowcount:
+                record_id = last[0]
+            else:  # the first, or its record was pruned or cleared meanwhile
+                record_id = self._insert_locked(conn, rec.model_copy(update={"status": "refused", "repeats": 1}))
+            self._refusals[key] = (record_id, now)
+            self._refusals.move_to_end(key)
+            while len(self._refusals) > _REFUSALS_KEPT:
+                self._refusals.popitem(last=False)
+            return record_id
+
+    def _insert(self, rec: HistoryRecord) -> int:
+        with self._lock:
+            return self._insert_locked(self._connection(), rec)
+
+    def _insert_locked(self, conn: sqlite3.Connection, rec: HistoryRecord) -> int:
+        error = rec.error[:_MAX_ERROR] if rec.error else None
+        cursor = conn.execute(
+            f"INSERT INTO history ({_DATA_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
+            (str(rec.profile_id), rec.profile_name, rec.database, rec.sql, rec.started_at.isoformat(),
+             rec.elapsed_ms, rec.rows, rec.status, error, rec.source, rec.client, rec.repeats),
+        )
+        self._inserts += 1
+        if self._inserts % self._prune_every == 0:
+            self._prune(conn)
+        return int(cursor.lastrowid or 0)
 
     def _prune(self, conn: sqlite3.Connection) -> None:
-        """Drop the entries beyond the retention. The caller holds the lock."""
-        removed = conn.execute(
-            "DELETE FROM history WHERE id <= (SELECT max(id) FROM history) - ?", (self._retention,)
-        ).rowcount
-        if removed > 0:
-            logger.debug("Pruned {} old history entries", removed)
+        """Drop each source's entries beyond its retention (D-08-13). The caller holds the lock."""
+        for source, keep in (("app", self._retention), ("mcp", self._mcp_retention)):
+            oldest_kept = conn.execute(
+                "SELECT id FROM history WHERE source = ? ORDER BY id DESC LIMIT 1 OFFSET ?", (source, keep - 1),
+            ).fetchone()
+            if oldest_kept is None:
+                continue
+            removed = conn.execute(
+                "DELETE FROM history WHERE source = ? AND id < ?", (source, oldest_kept[0]),
+            ).rowcount
+            if removed > 0:
+                logger.debug("Pruned {} old {} history entries", removed, source)
 
     def search(
-        self, q: str = "", profile_id: UUID | None = None, limit: int = 100, before: int | None = None
+        self, q: str = "", profile_id: UUID | None = None, limit: int = 100, before: int | None = None,
+        source: HistorySource | None = None,
     ) -> HistoryPage:
         where: list[str] = []
         params: list[object] = []
         if profile_id is not None:
             where.append("profile_id = ?")
             params.append(str(profile_id))
+        if source is not None:
+            where.append("source = ?")
+            params.append(source)
         if before is not None:
             where.append("id < ?")
             params.append(before)
@@ -178,6 +247,7 @@ class HistoryService:
     def clear(self) -> None:
         with self._lock:
             self._connection().execute("DELETE FROM history")
+            self._refusals.clear()
         logger.info("History cleared")
 
     def close(self) -> None:
diff --git a/backend/tests/test_history.py b/backend/tests/test_history.py
index e3987b4..936e1ff 100644
--- a/backend/tests/test_history.py
+++ b/backend/tests/test_history.py
@@ -158,3 +158,63 @@ def test_records_still_running_at_the_next_start_are_interrupted(tmp_path):  # D
     assert second.search().items[0].status == "running"
     second.finish(record_id, "ok", 1, 1, None)
     second.close()
+
+
+# ---- spec ⑧ D-08-13, CR-08-3: the MCP source, its own cap, refusals ----
+
+_BEFORE_08 = """
+CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, profile_name TEXT NOT NULL,
+    database_name TEXT, sql TEXT NOT NULL, started_at TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, rows INTEGER NOT NULL,
+    status TEXT NOT NULL, error TEXT);
+INSERT INTO history (profile_id, profile_name, database_name, sql, started_at, elapsed_ms, rows, status)
+    VALUES ('00000000-0000-0000-0000-000000000001', 'p', NULL, 'SELECT old', '2026-10-01T00:00:00+00:00', 1, 1, 'ok');
+"""
+
+
+def mcp(sql, status="ok", client="claude-code"):
+    return rec(sql, status).model_copy(update={"source": "mcp", "client": client})
+
+
+def test_a_file_from_before_08_gets_the_new_columns_in_place(tmp_path):
+    path = tmp_path / "h.db"
+    old = sqlite3.connect(path)
+    old.executescript(_BEFORE_08)
+    old.close()
+    history = HistoryService(path)
+    [entry] = history.search().items
+    assert (entry.sql, entry.source, entry.client, entry.repeats) == ("SELECT old", "app", None, 1)
+    history.record(mcp("SELECT new"))
+    assert sqls(history.search(source="mcp")) == ["SELECT new"]
+    assert sqls(history.search(source="app")) == ["SELECT old"]
+    history.close()
+    indexes = {row[1] for row in sqlite3.connect(path).execute("PRAGMA index_list(history)")}
+    assert "history_by_source" in indexes
+
+
+def test_each_source_keeps_its_own_newest_records(tmp_path):
+    history = HistoryService(tmp_path / "h.db", retention=3, mcp_retention=2, prune_every=1)
+    for n in range(6):
+        history.record(rec(f"app {n}"))
+        history.record(mcp(f"mcp {n}"))
+    assert sqls(history.search(source="app")) == ["app 5", "app 4", "app 3"]
+    assert sqls(history.search(source="mcp")) == ["mcp 5", "mcp 4"]  # an agent never pushes out the user's
+
+
+def test_a_refusal_sent_again_within_a_minute_adds_to_its_record(tmp_path):
+    now = [100.0]
+    history = HistoryService(tmp_path / "h.db", clock=lambda: now[0])
+    first = history.refused(mcp("DROP TABLE t"))
+    now[0] += 30
+    assert history.refused(mcp("DROP TABLE t")) == first
+    now[0] += 59  # a minute after the last, not the first
+    assert history.refused(mcp("DROP TABLE t")) == first
+    other_client = history.refused(mcp("DROP TABLE t", client="other"))
+    other_text = history.refused(mcp("DROP TABLE u"))
+    assert len({first, other_client, other_text}) == 3
+    now[0] += 61
+    later = history.refused(mcp("DROP TABLE t"))
+    entries = {e.id: e for e in history.search(source="mcp").items}
+    assert (entries[first].status, entries[first].repeats) == ("refused", 3)
+    assert entries[later].repeats == 1 and later != first
+    history.clear()
+    assert history.refused(mcp("DROP TABLE t")) not in (first, later)
diff --git a/src/features/history/HistoryPanel.tsx b/src/features/history/HistoryPanel.tsx
index 0e01660..aad7d80 100644
--- a/src/features/history/HistoryPanel.tsx
+++ b/src/features/history/HistoryPanel.tsx
@@ -1,6 +1,18 @@
 import { useQueryClient } from "@tanstack/react-query";
 import { useVirtualizer } from "@tanstack/react-virtual";
-import { Ban, CircleAlert, CircleCheck, CircleX, LoaderCircle, Search, TimerOff, Trash2, X, type LucideIcon } from "lucide-react";
+import {
+  Ban,
+  CircleAlert,
+  CircleCheck,
+  CircleX,
+  LoaderCircle,
+  Search,
+  ShieldBan,
+  TimerOff,
+  Trash2,
+  X,
+  type LucideIcon,
+} from "lucide-react";
 import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
 import { EngineIcon } from "@/components/EngineIcon";
 import { Button } from "@/components/ui/Button";
@@ -32,6 +44,8 @@ const STATUS: Record<HistoryStatus, { icon: LucideIcon; label: string; className
   // spec ⑥ D-06-9: a Run SQL file or copy that has not ended, or that was running when the backend ended
   running: { icon: LoaderCircle, label: "Running", className: "text-muted" },
   interrupted: { icon: CircleAlert, label: "Interrupted", className: "text-warning" },
+  // spec ⑧ OP-7: an AI tool's statement DbaGuy's gate refused (08b adds its MCP mark, the filter and Copy-only)
+  refused: { icon: ShieldBan, label: "Refused", className: "text-danger" },
 };
 
 function StatusIcon({ status }: { status: HistoryStatus }) {
```

- [ ] **Step 3: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_history.py tests/test_api.py tests/test_session_wiring.py`
Expected: all pass (planning: 74 passed in 3 s; `test_history.py` 14, the 11 old ones unchanged).

- [ ] **Step 4: Generate the API types; type-check; vitest**

Run: `npm run gen:api && npm run typecheck && npm test`
Expected:
- `openapi.json` +48 lines, `schema.d.ts` +16 lines: `source`, `client`, `repeats`, `refused` and the query parameter.
- `tsc` is clean.
- vitest: 906 passed.

- [ ] **Step 5: Review against the pain points** (M13 and Q2: agents' records never push out the user's; Q3: refusals are kept and merged)

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/domain/history.py backend/dbaguy_backend/services/history_service.py \
  backend/dbaguy_backend/api/history.py backend/tests/test_history.py src/features/history/HistoryPanel.tsx \
  src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(08a): History's source, client and repeats; refused records merged; a cap per source

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 6: The read path — agent sessions, read frames, the read-only layers per engine, the reader and its pages

**Suggested implementer:** Opus. It is the core: worker events across threads, a deadline over several steps, closing a session that may hold a transaction, and every engine's read-only behaviour, tested live.

**Files:**
- Create:
  - `backend/dbaguy_backend/agent/{sessions.py, reader.py, shape.py}`
  - `backend/dbaguy_backend/adapters/mysql/read_only.py`
  - `backend/tests/{test_mcp_reader.py, test_mcp_shape.py}`
- Modify:
  - `backend/dbaguy_backend/sql/dialects.py`
  - `backend/dbaguy_backend/sessions/{manager.py, hooks.py, worker.py}`
  - `backend/dbaguy_backend/adapters/{sqlite/adapter.py, mysql/adapter.py}`
  - `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/api/profiles.py`
- Test: `backend/tests/test_mcp_reader.py`, `backend/tests/test_mcp_shape.py`

**Interfaces:**
- Consumes:
  - Task 2's `ConnectionService.open(…, agent=True)` and `open(…, agent=)`.
  - Task 3's `LoginRegistry.remember`.
  - Task 4's `agent_read` and `EXPLAIN_HEADS`.
  - The worker's `call`, `submit`, `cancel`, `set_emitter`/`clear_emitter` and its events.
- Produces:
  - `sql/dialects.py`:
    - `ReadFrame{begin, end, rolls_back, explain_around}`
    - `read_frame(dialect) -> ReadFrame`
    - `explain_head(dialect, server_version) -> str | None`
    - `SERVER_TIMEOUT_MARGIN_S = 5`
  - `sessions/worker.WorkerHooks.describe: bool = True`: `_execute` passes it as `RunOptions.describe`.
  - `sessions/hooks.SessionHooksFactory.build_agent(profile_id) -> WorkerHooks`.
  - `sessions/manager.SessionManager`:
    - `.create_agent(profile_id, database) -> SessionInfo`
    - `.hold(session_id) -> SessionWorker` and `.release(session_id)`: a copy's source session uses these now too
    - `.close_agents(profile_id=None)`
  - `adapters/mysql/read_only.py`: `LEXER_UNSAFE_MODES`, `lexer_safe(mode)`, `unsafe_modes(mode)`, `make_agent_sql_mode(conn)`. Task 7 adds the guard.
  - `agent/shape.py`:
    - The constants in Global Constraints.
    - `compact(value) -> str`, `cap_cell(cell, max_chars) -> (cell, raw_size)`, `note(text) -> str`.
    - `ResultBudget(columns, max_rows, max_cell_chars, budget_chars=RESPONSE_CHARS)`, with `.take(rows)`, `.full`, `.read_capped`, `.next_page()`, `.spend(chars)`, `.rows`, `.dropped`, `.remaining`, `.largest_raw`, `.largest_encoded`.
  - `agent/sessions.py`:
    - `MAX_AGENT_SESSIONS = 8`
    - `Lease{worker, server_version, default_context, drop}`
    - `AgentSessions(sessions, login, clock)`, with `.key(profile, database)`, `.lease(profile, database, deadline)` (a context manager yielding a `Lease`) and `.close(profile_id=None)`. Each slot carries a `gone` flag (below).
  - `agent/reader.py`:
    - `ReadRequest{profile, dialect, sql, database, query_context, max_rows, max_cell_chars, timeout_s, explain}`
    - `ReadOutcome{status, columns, rows, more_rows, rows_dropped, notes, error_code, error_message, rows_read, read_capped, elapsed_ms, reset}`, with `status` one of `ok`, `error`, `timeout`, `cancelled`
    - `CallControl`, with `.attach(cancel)`, `.detach()`, `.cancel()` and `.cancelled`
    - `AgentReader(sessions, clock)`, with `.read(request, control=None) -> ReadOutcome`
    - `RESET_NOTE`
  - `container.agent_sessions`, `container.agent_reader`. `_close_agents()` closes the agent sessions. `_forget_profile_state` closes a profile's agent sessions.

**Decisions this task carries:**
- D-08-1, agent sessions:
  - They are created by `SessionManager.create_agent`: the same `SessionWorker` and reaper, `open(…, agent=True)`, and `build_agent` hooks.
  - The hooks' check is the gate, query or explain: the backstop. They set `describe=False`, so no rows are read ahead of a page (CR-08-7), and `read_ahead_bytes=None`, so nothing is read ahead while paused.
  - There is no History here: Task 8's tools write one record per call (P3).
  - `AgentSessions` keys them by (profile, database), with no database for SQLite and Couchbase. There are 8 at most; the least recently used idle one gives way, and `busy` when all are in use.
  - A call waits for the session's lock within its deadline (`busy` after), and holds the session in the manager for the call, so the reaper never closes it between the frame's statements.
  - A slot taken out of the registry while a call waited for its lock (evicted, or its profile changed) is marked `gone`. The waiting call lets it go and takes the key's current slot, so it never opens a session the registry no longer knows, which nothing would ever close. The eviction's session is closed outside the registry's lock.
  - The first time a profile's session opens, its login's privileges are read on it.
- D-08-3, the frame:
  - The reader sends `ReadFrame.begin` with `worker.call()`: DbaGuy's own text, never through the gate, as catalog queries go.
  - Then the statement, as an `ExecuteCommand` (`page_size=1`, `history=False`, the call's context).
  - Then, with `call()`: T-SQL's SHOWPLAN OFF for an explain, the adapter's `rollback()`, and `ReadFrame.end` (`pg_advisory_unlock_all()`, `RELEASE_ALL_LOCKS()`).
  - Any failure while opening or ending closes the session (`Lease.drop`), and the call says so (`RESET_NOTE`).
- D-08-4: SQLite's agent connection adds `PRAGMA query_only = ON` and `set_authorizer`. The authorizer allows SELECT, READ, RECURSIVE, every function but `load_extension`, and the eight read-only pragmas; everything else is denied. The catalog still works under it (H6).
- CR-08-5, MySQL's agent connection: after the profile's own settings and before READ ONLY, its `sql_mode` drops `NO_BACKSLASH_ESCAPES`, `ANSI_QUOTES` and `ANSI`, which turns `ANSI_QUOTES` back on.
- D-08-10: the reader builds an explain's text from `explain_head(dialect, server_version)`. MySQL's tree format needs MySQL 8.0.16 or newer; MariaDB and older servers get the plain `EXPLAIN`. T-SQL sends `SET SHOWPLAN_TEXT ON` / `OFF` around the statement instead.
- CR-08-7 and P10, the pages:
  - The first page is 1 row. Every later page is `PAGE_RAW_CHARS` divided by the largest raw row so far, between 1 and 100, and no more than the rows still wanted or the room left.
  - It stops (Discard) at `max_rows`, at a row that does not fit `RESPONSE_CHARS`, or after `READ_RAW_CHARS` (64 Mi) of values. P10: measured while planning, one 100 MB value per row read 37 rows (3.7 GB) in 6.6 s to fill the response with previews; with the cap it reads that one row in 0.9 s. The RSS peak is the two rows the spec names (+290 MB), a known limitation.
  - Cells are cut at `max_cell_chars` on the worker's encoding (P4): the worker's own 64 KiB `TEXT_LIMIT` stays, since `max_cell_chars` is at most 20,000.
- P12, the deadline:
  - The reader owns the call's deadline: the lock, the session, the frame, every page. The command carries no `timeout_s`.
  - At the deadline it cancels, cancels again after 1.5 s (SQL Server's second Cancel ends a statement stuck in a transaction), and closes the session after 10 s more.
  - A statement that ends cancelled without a cancel of ours (PG's own `statement_timeout`, which the adapter reports as cancelled) is a `timeout`.
  - The worker's own words, "Query cancelled" and the like, never become notes.
- Columns: `{"name", "type"}`, with type null where the engine knows types only from values. SQLite without its peek is all null (P9).

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_mcp_shape.py`:**

```python
"""What an MCP result may hold and how big its next page is (spec ⑧ §3.6, CR-08-7). Pure."""

from dbaguy_backend.agent.shape import (
    PAGE_RAW_CHARS, PAGE_ROWS_MAX, RESPONSE_CHARS, ResultBudget, cap_cell, compact, note,
)

COLUMNS = [{"name": "v", "type": "text"}]


def test_cells_past_the_limit_become_previews_that_keep_their_size():
    assert cap_cell("abc", 5) == ("abc", 3)
    assert cap_cell("abcdefgh", 5) == ({"$type": "text", "preview": "abcde", "size": 8, "truncated": True}, 8)
    grid_cut = {"$type": "text", "preview": "x" * 65536, "size": 100_000_000, "truncated": True}
    assert cap_cell(grid_cut, 10) == ({"$type": "text", "preview": "x" * 10, "size": 100_000_000,
                                       "truncated": True}, 100_000_000)
    assert cap_cell({"$type": "json", "text": '{"a": 12345}'}, 4) == (
        {"$type": "text", "preview": '{"a"', "size": 12, "truncated": True}, 12)
    blob = {"$type": "bytes", "size": 5_000_000, "preview": "00ff"}
    assert cap_cell(blob, 10) == (blob, 5_000_000)
    assert cap_cell(9007199254740993, 10) == (9007199254740993, 16)
    assert cap_cell(None, 10) == (None, 4)


def test_rows_are_kept_while_they_fit_and_one_that_does_not_ends_the_result():
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    row = ["y" * 1000]
    budget.take([row] * 100)
    kept = len(budget.rows)
    assert 30 < kept < 40 and budget.dropped == 100 - kept and budget.full
    assert len(compact({"columns": COLUMNS, "rows": budget.rows})) < RESPONSE_CHARS


def test_max_rows_ends_it_too():
    budget = ResultBudget(COLUMNS, max_rows=3, max_cell_chars=1000)
    budget.take([["a"]] * 2)
    assert not budget.full
    budget.take([["b"], ["c"]])
    assert budget.full and len(budget.rows) == 3 and budget.dropped == 1


def test_the_next_page_follows_the_largest_row_raw_and_the_room_left():
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=10)
    budget.take([["z"]])
    assert budget.next_page() == PAGE_ROWS_MAX  # small rows: the page cap
    budget.take([["m" * 1_000_000]])  # 1 Mi characters raw: 8 rows a page
    assert budget.next_page() == PAGE_RAW_CHARS // 1_000_000
    budget.take([[{"$type": "text", "preview": "h", "size": 100_000_000, "truncated": True}]])
    assert budget.next_page() == 1  # a huge value: one row a page, never fewer
    nearly = ResultBudget(COLUMNS, max_rows=5, max_cell_chars=10)
    nearly.take([["a"]] * 3)
    assert nearly.next_page() == 2  # no more than the rows still wanted
    spent = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    spent.take([["q" * 1000]] * 30)
    assert spent.next_page() == spent.remaining // spent.largest_encoded


def test_a_note_is_cut_with_an_ellipsis():
    assert note("short") == "short"
    assert note("x" * 600) == "x" * 499 + "…"


def test_a_few_huge_values_end_the_reading():
    budget = ResultBudget(COLUMNS, max_rows=1000, max_cell_chars=1000)
    huge = {"$type": "text", "preview": "x" * 65536, "size": 100_000_000, "truncated": True}
    budget.take([[huge]])
    assert budget.read_capped and budget.full and len(budget.rows) == 1  # one 100 MB value: that row alone
```

**New file `backend/tests/test_mcp_reader.py`:**

```python
"""An MCP agent's read on the five engines (spec ⑧ D-08-1, D-08-3, D-08-4, §3.4, §3.6): the frame, paging and its
memory bound, and what each server refuses with the gate taken away (`no_gate`: the worker's backstop allows anything,
as a gate bug would). Scratch objects are zz_08_*, dropped by exact name; Couchbase is only read."""

import sqlite3
import threading
import time
import tracemalloc
from uuid import UUID

import mysql.connector
import psycopg
import pytest

from dbaguy_backend.agent.reader import CallControl, ReadRequest
from dbaguy_backend.agent.sessions import MAX_AGENT_SESSIONS
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.profiles import SqliteProfile
from dbaguy_backend.services.agent_gate import AgentVerdict
from dbaguy_backend.sql.dialects import Dialect

from .agent_support import container, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, couchbase_query, make_couchbase_profile, make_mssql_profile,
    make_mysql_profile, make_pg_profile, mssql_connect, mysql_driver_kwargs, requires_couchbase, requires_mssql,
    requires_mysql, requires_pg, scaled,
)

__all__ = ["container"]


@pytest.fixture
def no_gate(monkeypatch):
    """The worker's backstop allows anything: what reaches the server is what the server alone stops."""
    allowed = AgentVerdict(classification=Classification(kind=StatementKind.READ))
    monkeypatch.setattr("dbaguy_backend.sessions.hooks.agent_read", lambda sql, dialect, purpose="query": allowed)


def _read(container, pid: str, sql: str, dialect: Dialect, **options):
    profile = container.profiles.get(UUID(pid))
    return container.agent_reader.read(ReadRequest(profile=profile, dialect=dialect, sql=sql, **options))


def _pg():
    return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                           autocommit=True)


@pytest.fixture
def lite(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT); "
                       "INSERT INTO t (note) VALUES ('a'), ('b'), ('c');")
    conn.close()
    return path


def _lite_profile(path, name="zz_08 lite", number=1) -> SqliteProfile:
    return SqliteProfile(id=f"00000000-0000-0000-0000-{number:012d}", engine="sqlite", name=name, path=str(path))


@requires_pg
def test_postgres_rows_more_rows_and_the_server_refuses_writes(container, no_gate):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    first = _read(container, pid, "SELECT n FROM generate_series(1, 250) AS n", Dialect.POSTGRES)
    assert (first.status, len(first.rows), first.more_rows) == ("ok", 100, True)
    assert first.columns == [{"name": "n", "type": "int4"}] and first.rows[:2] == [[1], [2]]
    whole = _read(container, pid, "SELECT n FROM generate_series(1, 250) AS n", Dialect.POSTGRES, max_rows=1000)
    assert (len(whole.rows), whole.more_rows) == (250, False)
    owner = _pg()
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        written = _read(container, pid, "INSERT INTO public.zz_08_victim VALUES (1)", Dialect.POSTGRES)
        assert (written.status, written.error_code) == ("error", "25006")
        stacked = _read(container, pid, "SELECT 1; COMMIT; DROP TABLE public.zz_08_victim", Dialect.POSTGRES)
        assert stacked.error_code == "read_only_single_statement"  # G6: the adapter's own refusal
        assert owner.execute("SELECT count(*) FROM public.zz_08_victim").fetchone() == (0,)
    finally:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_pg
def test_postgres_stays_read_only_after_a_function_turns_it_off_and_its_locks_are_released(container, no_gate):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    owner = _pg()
    try:
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.execute("CREATE TABLE public.zz_08_victim (id int)")
        owner.execute("CREATE OR REPLACE FUNCTION public.zz_08_flip() RETURNS int LANGUAGE plpgsql AS $$ BEGIN "
                      "PERFORM set_config('default_transaction_read_only', 'off', false); RETURN 1; END $$")
        owner.execute("CREATE OR REPLACE FUNCTION public.zz_08_lock() RETURNS int LANGUAGE plpgsql AS $$ BEGIN "
                      "PERFORM pg_advisory_lock(80808); RETURN 1; END $$")
        assert _read(container, pid, "SELECT public.zz_08_flip()", Dialect.POSTGRES).status == "ok"  # P3
        after = _read(container, pid, "INSERT INTO public.zz_08_victim VALUES (1)", Dialect.POSTGRES)
        assert after.error_code == "25006"  # the ROLLBACK put the session default back
        assert _read(container, pid, "SHOW default_transaction_read_only", Dialect.POSTGRES).rows == [["on"]]
        assert _read(container, pid, "SELECT public.zz_08_lock()", Dialect.POSTGRES).status == "ok"
        held = owner.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 80808").fetchone()
        assert held == (0,)  # H1-e: pg_advisory_unlock_all() after the ROLLBACK
    finally:
        owner.execute("DROP FUNCTION IF EXISTS public.zz_08_flip()")
        owner.execute("DROP FUNCTION IF EXISTS public.zz_08_lock()")
        owner.execute("DROP TABLE IF EXISTS public.zz_08_victim")
        owner.close()


@requires_pg
def test_postgres_statement_timeout_reaches_the_cursor_and_the_deadline_stops_the_rest(container, monkeypatch):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    monkeypatch.setattr("dbaguy_backend.agent.reader.SERVER_TIMEOUT_MARGIN_S", -4)  # the server's own: 1 s (A7)
    started = time.monotonic()
    server = _read(container, pid, "SELECT pg_sleep(3)", Dialect.POSTGRES, timeout_s=5)
    assert server.status == "timeout" and time.monotonic() - started < 2.5
    assert server.notes == ["canceling statement due to statement timeout"]  # the server's words
    monkeypatch.undo()
    started = time.monotonic()
    ours = _read(container, pid, "SELECT pg_sleep(30)", Dialect.POSTGRES, timeout_s=1)
    assert ours.status == "timeout" and time.monotonic() - started < scaled(5)
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES).rows == [[1]]  # the session goes on


@requires_pg
def test_a_cancel_stops_the_statement(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    control = CallControl()
    threading.Timer(0.5, control.cancel).start()
    started = time.monotonic()
    outcome = container.agent_reader.read(ReadRequest(
        profile=container.profiles.get(UUID(pid)), dialect=Dialect.POSTGRES, sql="SELECT pg_sleep(30)"), control)
    assert outcome.status == "cancelled" and time.monotonic() - started < scaled(5)


@requires_pg
def test_huge_values_are_read_a_page_at_a_time_and_stop_at_the_response_budget(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    sql = "SELECT repeat('x', 1000000) AS big FROM generate_series(1, 1000)"
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        outcome = _read(container, pid, sql, Dialect.POSTGRES, max_rows=1000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert outcome.status == "ok" and outcome.more_rows
    assert outcome.rows[0][0] == {"$type": "text", "preview": "x" * 1000, "size": 1_000_000, "truncated": True}
    assert len(outcome.rows) < 40 and outcome.rows_read < 60  # CR-08-7: never the 1,000 rows
    assert peak < 40 * 1024 * 1024, f"{peak / 1e6:.1f} MB"  # a page (8 Mi characters) and a peeked row, not 1 GB


@requires_pg
def test_a_second_call_waits_for_the_first_within_its_deadline(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    first = threading.Thread(target=_read, args=(container, pid, "SELECT pg_sleep(2)", Dialect.POSTGRES))
    first.start()
    time.sleep(0.5)
    with pytest.raises(ToolFailure) as busy:
        _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=0.5)
    assert busy.value.code == "busy"
    first.join()
    assert _read(container, pid, "SELECT 1", Dialect.POSTGRES, timeout_s=10).rows == [[1]]


@requires_mysql
def test_mysql_refuses_writes_cleans_its_sql_mode_and_releases_locks(container, no_gate):
    pid = share(container, make_mysql_profile(name="zz_08 mysql", options={"sql_mode": "ANSI,NO_BACKSLASH_ESCAPES"}))
    mode = _read(container, pid, "SELECT @@SESSION.sql_mode", Dialect.MYSQL).rows[0][0]
    assert not {"ANSI", "ANSI_QUOTES", "NO_BACKSLASH_ESCAPES"} & set(mode.split(","))  # CR-08-5
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    cursor = owner.cursor()
    try:
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        cursor.execute("CREATE TABLE zz_08_victim (id INT)")
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_lock")
        cursor.execute("CREATE FUNCTION zz_08_lock() RETURNS INT NO SQL RETURN GET_LOCK('zz_08_l', 1)")
        written = _read(container, pid, "INSERT INTO zz_08_victim VALUES (1)", Dialect.MYSQL)
        assert (written.status, written.error_code) == ("error", "1792")
        assert _read(container, pid, "SELECT zz_08_lock()", Dialect.MYSQL).rows == [[1]]
        cursor.execute("SELECT IS_FREE_LOCK('zz_08_l')")
        assert cursor.fetchall() == [(1,)]  # M5: RELEASE_ALL_LOCKS() after the ROLLBACK
        cursor.execute("SELECT COUNT(*) FROM zz_08_victim")
        assert cursor.fetchall() == [(0,)]
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_lock")
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        owner.close()


def test_sqlite_refuses_writes_attach_and_vacuum_into_and_leaves_no_file(container, no_gate, lite, tmp_path):
    pid = share(container, _lite_profile(lite))
    assert _read(container, pid, "SELECT note FROM t ORDER BY id", Dialect.SQLITE).rows == [["a"], ["b"], ["c"]]
    attached, copy = tmp_path / "zz_08_attached.db", tmp_path / "zz_08_copy.db"
    for sql in ("INSERT INTO t (note) VALUES ('x')", f"ATTACH '{attached}' AS x", f"VACUUM INTO '{copy}'",
                "PRAGMA query_only = 0", "BEGIN", "SELECT load_extension('x')"):
        outcome = _read(container, pid, sql, Dialect.SQLITE)
        assert outcome.status == "error", sql
        # the authorizer (D-08-4): "authorization denied" while it runs, "not authorized" while it is prepared
        assert outcome.error_code == "SQLITE_AUTH" or "not authorized" in outcome.error_message, sql
    assert not attached.exists() and not copy.exists()
    plan = _read(container, pid, "SELECT * FROM t WHERE id = 1", Dialect.SQLITE, explain=True)  # + EXPLAIN QUERY PLAN
    assert plan.status == "ok" and plan.rows  # S9: EXPLAIN QUERY PLAN under the authorizer (A9)
    catalog = container.agent_tools.describe(pid, name="t")  # H6: the catalog's pragmas under the authorizer
    assert [c["name"] for c in catalog["columns"]] == ["id", "note"]
    assert sqlite3.connect(lite).execute("SELECT count(*) FROM t").fetchone() == (3,)


def test_at_most_eight_sessions_the_least_recently_used_gives_way(container, tmp_path):
    ids = []
    for number in range(MAX_AGENT_SESSIONS + 1):
        path = tmp_path / f"zz_08_{number}.db"
        sqlite3.connect(path).close()
        ids.append(share(container, _lite_profile(path, f"zz_08 {number}", number + 1)))
        assert _read(container, ids[-1], "SELECT 1", Dialect.SQLITE).rows == [[1]]
    assert len(container.agent_sessions._slots) == MAX_AGENT_SESSIONS
    assert UUID(ids[0]) not in {key[0] for key in container.agent_sessions._slots}


@requires_mssql
def test_sql_server_rolls_back_what_the_gate_would_have_stopped(container, no_gate):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    owner = mssql_connect()
    try:
        owner.execute_non_query("IF OBJECT_ID('dbo.zz_08_victim') IS NOT NULL DROP TABLE dbo.zz_08_victim; "
                                "CREATE TABLE dbo.zz_08_victim (id int)")
        assert _read(container, pid, "INSERT INTO dbo.zz_08_victim VALUES (1)", Dialect.TSQL).status == "ok"
        stacked = _read(container, pid, "SELECT 1; DROP TABLE dbo.zz_08_victim", Dialect.TSQL)  # T1
        assert stacked.status == "ok"
        owner.execute_query("SELECT COUNT(*) FROM dbo.zz_08_victim")  # T6: both rolled back
        assert [row[0] for row in owner] == [0]
    finally:
        owner.execute_non_query("IF OBJECT_ID('dbo.zz_08_victim') IS NOT NULL DROP TABLE dbo.zz_08_victim")
        owner.close()


@requires_couchbase
def test_couchbase_refuses_writes_with_readonly(container, no_gate):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"))
    users = _read(container, pid, "SELECT RAW META(u).id FROM users AS u ORDER BY META(u).id LIMIT 2",
                  Dialect.SQLPP)
    assert users.status == "ok" and len(users.rows) == 2
    written = _read(container, pid, 'UPSERT INTO orders (KEY, VALUE) VALUES ("zz_08_x", {"a": 1})', Dialect.SQLPP)
    assert (written.status, written.error_code) == ("error", "1000")
    found = couchbase_query('SELECT RAW COUNT(*) FROM dbaguy.app.orders USE KEYS "zz_08_x"')["results"]
    assert found == [0]
```

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_shape.py tests/test_mcp_reader.py`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.agent.shape'` (and `.reader`).

- [ ] **Step 3: Write the code**

**New file `backend/dbaguy_backend/agent/shape.py`:**

```python
"""What one MCP query result may hold (spec ⑧ §3.6, CR-08-7), pure: cells cut at `max_cell_chars`, rows kept while the
response stays under RESPONSE_CHARS and `max_rows`, and the size of the next page, so that memory never holds more than
the kept rows plus one page of the driver's rows (plus the row the worker peeks at)."""

import json
from typing import Any

MAX_ROWS_DEFAULT, MAX_ROWS = 100, 1_000
MAX_CELL_DEFAULT, MAX_CELL = 1_000, 20_000
RESPONSE_CHARS = 40_000  # Claude Code warns past 10,000 tokens; about 4 characters a token
RESPONSE_OVERHEAD = 1_000  # the result's keys, counts and the truncation note
PAGE_RAW_CHARS = 8 * 1024 * 1024  # one page of the driver's rows: about this many characters, raw
# Every value one call reads, raw: past it, no more rows are asked for. A few huge values end the call (one 100 MB
# value: that row alone), instead of dozens of them being read for their 1,000-character previews (P10)
READ_RAW_CHARS = 64 * 1024 * 1024
PAGE_ROWS_MAX = 100
FIRST_PAGE = 1  # the first page is one row: its size sizes the next (as ⑥'s dump's FIRST_FETCH)
NOTES_MAX, NOTE_CHARS = 20, 500  # server messages (PG NOTICE, SQL Server PRINT)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def cap_cell(cell: Any, max_chars: int) -> tuple[Any, int]:
    """The cell as the agent gets it (the grid's encoding, text past `max_chars` as `{"$type": "text", preview, size,
    truncated}`), and its raw size in characters (`size` where the encoding has one, else its encoded length)."""
    if isinstance(cell, str):
        if len(cell) > max_chars:
            return {"$type": "text", "preview": cell[:max_chars], "size": len(cell), "truncated": True}, len(cell)
        return cell, len(cell)
    if isinstance(cell, dict):
        kind, size = cell.get("$type"), cell.get("size")
        if kind == "text" and len(cell.get("preview", "")) > max_chars:  # cut at the grid's 64 KiB already
            return {**cell, "preview": cell["preview"][:max_chars]}, int(size or 0)
        if kind == "json" and len(cell.get("text", "")) > max_chars:
            text = cell["text"]
            return {"$type": "text", "preview": text[:max_chars], "size": len(text), "truncated": True}, len(text)
        if isinstance(size, int):  # bytes, a truncated text
            return cell, size
    return cell, len(compact(cell))


class ResultBudget:
    """The rows of one result as they arrive, page by page."""

    def __init__(self, columns: list[dict[str, str | None]], max_rows: int, max_cell_chars: int,
                 budget_chars: int = RESPONSE_CHARS) -> None:
        self.max_rows = max_rows
        self.max_cell_chars = max_cell_chars
        self.rows: list[list[Any]] = []
        self.dropped = 0  # rows read but left out: the response had no room for them
        self.largest_raw = 1  # the largest row so far, raw (sizes the next page's driver read)
        self.largest_encoded = 1  # the largest row so far, as sent (sizes it against the room left)
        self.remaining = budget_chars - RESPONSE_OVERHEAD - len(compact(columns))
        self.raw_read = 0  # the kept rows' values, raw

    @property
    def full(self) -> bool:
        """No more rows are wanted: `max_rows` kept, one did not fit, or READ_RAW_CHARS of values were read."""
        return len(self.rows) >= self.max_rows or self.dropped > 0 or self.remaining <= 0 or self.read_capped

    @property
    def read_capped(self) -> bool:
        return self.raw_read >= READ_RAW_CHARS

    def take(self, rows: list[list[Any]]) -> None:
        for row in rows:
            capped = [cap_cell(cell, self.max_cell_chars) for cell in row]
            cells = [cell for cell, _ in capped]
            raw = sum(size for _, size in capped) or 1
            encoded = len(compact(cells)) + 1  # its comma
            self.largest_raw = max(self.largest_raw, raw)
            self.largest_encoded = max(self.largest_encoded, encoded)
            if self.full or encoded > self.remaining:
                self.dropped += 1
                continue
            self.rows.append(cells)
            self.remaining -= encoded
            self.raw_read += raw

    def spend(self, chars: int) -> None:
        """Room taken by something else in the response (a note)."""
        self.remaining -= chars

    def next_page(self) -> int:
        """§3.6: PAGE_RAW_CHARS over the largest row so far, between 1 and PAGE_ROWS_MAX, and no more than the rows
        still wanted or than the room left holds rows of the largest size (at least 1)."""
        by_raw = PAGE_RAW_CHARS // self.largest_raw
        by_room = self.remaining // self.largest_encoded
        return max(1, min(PAGE_ROWS_MAX, by_raw, self.max_rows - len(self.rows), by_room))


def note(text: str) -> str:
    return text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS - 1] + "…"
```

**New file `backend/dbaguy_backend/agent/sessions.py`:**

```python
"""MCP agents' sessions (spec ⑧ D-08-1): one per (profile, database), at most MAX_AGENT_SESSIONS, one call at a time
each. A call leases its session: it waits for the session's lock within the call's deadline (`busy` past it), and holds
the session in the SessionManager so the reaper never closes it between the frame's statements. The first time a
profile's session opens, its login's privileges are read on it (CR-08-1)."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from loguru import logger

from ..domain.errors import SessionNotFound
from ..domain.execution import QueryContext
from ..domain.profiles import AnyProfile, Engine
from ..sessions.manager import SessionManager
from ..sessions.worker import SessionWorker
from .login import LoginRegistry
from .tools import ToolFailure

MAX_AGENT_SESSIONS = 8
_BUSY = "Another call on '{}' was still running"
_ALL_BUSY = "All {} of DbaGuy's agent connections are busy: try again"
_Key = tuple[UUID, str | None]


@dataclass
class _Slot:
    key: _Key
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str | None = None
    server_version: str = ""
    default_context: QueryContext | None = None
    last_used: float = 0.0
    gone: bool = False  # taken out of the registry (evicted, or its profile changed): a waiting call gets another


@dataclass(frozen=True)
class Lease:
    """A session for one call: its worker, what the server said when it opened, and how to drop it (a frame that did
    not end cleanly: the next call gets a new session, never one that may hold a transaction or a lock)."""

    worker: SessionWorker
    server_version: str
    default_context: QueryContext | None
    drop: Callable[[], None]


class AgentSessions:
    def __init__(self, sessions: SessionManager, login: LoginRegistry,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions = sessions
        self._login = login
        self._clock = clock
        self._slots: dict[_Key, _Slot] = {}
        self._lock = threading.Lock()

    def key(self, profile: AnyProfile, database: str | None) -> _Key:
        """SQLite has one database; Couchbase's requests carry their context instead: one session each."""
        return profile.id, None if profile.engine in (Engine.SQLITE, Engine.COUCHBASE) else database

    @contextmanager
    def lease(self, profile: AnyProfile, database: str | None, deadline: float) -> Iterator[Lease]:
        """The session of (profile, database), opened when there is none, its lock taken before `deadline`."""
        while True:
            slot = self._slot(self.key(profile, database))
            if not slot.lock.acquire(timeout=max(0.0, deadline - self._clock())):
                raise ToolFailure("busy", _BUSY.format(profile.name))
            if not slot.gone:
                break
            slot.lock.release()  # taken out while this call waited for it: the key's slot now
        held: str | None = None
        try:
            worker = self._worker(slot, profile, database)
            held = slot.session_id
            yield Lease(worker, slot.server_version, slot.default_context, lambda: self._drop(slot))
        finally:
            if held is not None:
                try:
                    self._sessions.release(held)
                except SessionNotFound:
                    pass
            slot.last_used = self._clock()
            slot.lock.release()

    def close(self, profile_id: UUID | None = None) -> None:
        """A profile changed (its sessions close), or the MCP server stopped (all close)."""
        with self._lock:
            slots = [s for k, s in self._slots.items() if profile_id is None or k[0] == profile_id]
            for slot in slots:
                slot.gone = True
                del self._slots[slot.key]
        self._sessions.close_agents(profile_id)

    def _slot(self, key: _Key) -> _Slot:
        evicted: str | None = None
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None:
                return slot
            if len(self._slots) >= MAX_AGENT_SESSIONS:
                evicted = self._evict_idle_locked()
            slot = self._slots[key] = _Slot(key, last_used=self._clock())
        if evicted is not None:
            self._close_session(evicted)
        return slot

    def _evict_idle_locked(self) -> str | None:
        """The least recently used slot whose call has ended gives way; its session id, to close (outside `_lock`)."""
        for slot in sorted(self._slots.values(), key=lambda s: s.last_used):
            if slot.lock.acquire(blocking=False):
                try:
                    slot.gone = True
                    del self._slots[slot.key]
                    return slot.session_id
                finally:
                    slot.lock.release()
        raise ToolFailure("busy", _ALL_BUSY.format(MAX_AGENT_SESSIONS))

    def _worker(self, slot: _Slot, profile: AnyProfile, database: str | None) -> SessionWorker:
        """The slot's session, held for this call; a new one when it has none or the reaper closed it."""
        if slot.session_id is not None:
            try:
                return self._sessions.hold(slot.session_id)
            except SessionNotFound:
                slot.session_id = None  # idle too long: reaped
        info = self._sessions.create_agent(profile.id, database)
        slot.session_id, slot.server_version = info.session_id, info.server.version
        slot.default_context = info.server.default_query_context
        worker = self._sessions.hold(info.session_id)
        self._login.remember(profile, lambda: worker.call(lambda c: c.login_privileges()))
        return worker

    def _drop(self, slot: _Slot) -> None:
        session_id, slot.session_id = slot.session_id, None
        if session_id is not None:
            logger.info("MCP: closing an agent session after a call that did not end cleanly")
            self._close_session(session_id)

    def _close_session(self, session_id: str) -> None:
        try:
            self._sessions.close(session_id)
        except SessionNotFound:
            pass
```

**New file `backend/dbaguy_backend/agent/reader.py`:**

```python
"""One MCP read (spec ⑧ §3.5's run_query steps 3–7, D-08-3, CR-08-7): lease the agent session, send the dialect's
ReadFrame opening with `call()` (never through the agent gate: DbaGuy's own text), the statement as an ExecuteCommand
(the gate again as the worker's check, describe off, no read-ahead), page by page as `ResultBudget` sizes them, discard
once nothing more is wanted, then ROLLBACK and the frame's end with `call()`. The call's deadline covers it all: the
lock, the session opening, every page. A frame that does not end cleanly closes the session: the next call never meets
a transaction or a lock this one left."""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel

from ..adapters.base import DatabaseConnection, RunOptions, StatementCancelled, StatementError
from ..domain.errors import DomainError
from ..domain.execution import (
    DiscardCommand, ErrorEvent, ExecuteCommand, FetchMoreCommand, FinishedEvent, MessageEvent, QueryContext,
    ResultSetEndEvent, ResultSetEvent, RowsEvent,
)
from ..domain.profiles import AnyProfile
from ..sessions.worker import CLOSED_BEFORE_START
from ..sql.dialects import SERVER_TIMEOUT_MARGIN_S, Dialect, explain_head, read_frame
from .sessions import AgentSessions, Lease
from .shape import FIRST_PAGE, MAX_CELL_DEFAULT, MAX_ROWS_DEFAULT, NOTES_MAX, ResultBudget, compact, note

FORCE_AFTER_S = 1.5  # after a timeout's cancel, a second (SQL Server's that ends a statement stuck in a transaction)
CANCEL_GRACE_S = 10.0  # then the statement has this long to stop before its session is closed
FRAME_END_S = 15.0  # ROLLBACK and the frame's end
_INTERNAL = RunOptions(describe=False)
RESET_NOTE = ("DbaGuy closed this connection after the call (it could not end the call cleanly); the next call opens a "
              "new one")
_STUCK = "The statement did not stop after its timeout: DbaGuy closed the connection"
# What the worker itself says when a statement ends cancelled: never a server message, so never a note
_CANCEL_TEXTS = frozenset({StatementCancelled().message, CLOSED_BEFORE_START, "Query cancelled before it started"})
ReadStatus = Literal["ok", "error", "timeout", "cancelled"]


@dataclass(frozen=True)
class ReadRequest:
    profile: AnyProfile
    dialect: Dialect
    sql: str  # what the server gets: the agent's statement, or the explain tool's text
    database: str | None = None
    query_context: QueryContext | None = None  # Couchbase; None: the profile's default context
    max_rows: int = MAX_ROWS_DEFAULT
    max_cell_chars: int = MAX_CELL_DEFAULT
    timeout_s: float = 30.0
    explain: bool = False  # every result set's rows (T-SQL's SHOWPLAN plan is its second), SHOWPLAN around it


@dataclass
class ReadOutcome:
    status: ReadStatus = "ok"
    columns: list[dict[str, str | None]] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    more_rows: bool = False  # the server had more than the result holds
    rows_dropped: int = 0  # rows read that the response had no room for
    notes: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    rows_read: int = 0
    read_capped: bool = False  # it stopped at READ_RAW_CHARS of values
    elapsed_ms: int = 0
    reset: bool = False  # the session was closed after the call


class CallControl:
    """A tool call's cancel from the MCP layer's thread (a client's cancel, A4): cancels its statement, once there is
    one; a cancel before that is kept."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel: Callable[[], None] | None = None
        self.cancelled = False

    def attach(self, cancel: Callable[[], None]) -> None:
        with self._lock:
            self._cancel = cancel
            fire = self.cancelled
        if fire:
            cancel()

    def detach(self) -> None:
        with self._lock:
            self._cancel = None

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            cancel = self._cancel
        if cancel is not None:
            cancel()


class _Events:
    """The session's emitter during a call: the events of its request, in order."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._queue: queue.Queue[BaseModel] = queue.Queue()

    def __call__(self, event: BaseModel) -> None:
        if getattr(event, "request_id", None) == self.request_id:
            self._queue.put(event)

    def next(self, timeout: float) -> BaseModel | None:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


def _send(statements: list[str], connection: DatabaseConnection) -> None:
    """DbaGuy's own statements, on the worker thread; any rows read and dropped."""
    for sql in statements:
        result = connection.run(sql, _INTERNAL)
        try:
            while result.fetch(100):
                pass
        finally:
            result.close()


def _end(before: list[str], rolls_back: bool, after: list[str], connection: DatabaseConnection) -> None:
    _send(before, connection)
    if rolls_back:
        connection.rollback()
    _send(after, connection)


class AgentReader:
    def __init__(self, sessions: AgentSessions, clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions = sessions
        self._clock = clock

    def read(self, request: ReadRequest, control: CallControl | None = None) -> ReadOutcome:
        """The statement's first result (every result for an explain), within its limits. A password the profile
        lacks, a session that cannot open, a busy session: DomainError / ToolFailure from the lease."""
        started = self._clock()
        deadline = started + request.timeout_s
        outcome = ReadOutcome()
        with self._sessions.lease(request.profile, request.database, deadline) as lease:
            self._framed(lease, request, deadline, control or CallControl(), outcome)
        outcome.elapsed_ms = int((self._clock() - started) * 1000)
        return outcome

    def _framed(self, lease: Lease, request: ReadRequest, deadline: float, control: CallControl,
                outcome: ReadOutcome) -> None:
        frame = read_frame(request.dialect)
        around = frame.explain_around if request.explain else None
        timeout_ms = int((request.timeout_s + SERVER_TIMEOUT_MARGIN_S) * 1000)
        opening = [sql.format(timeout_ms=timeout_ms) for sql in frame.begin] + ([around[0]] if around else [])
        events = _Events(uuid4().hex)
        worker = lease.worker
        worker.set_emitter(events)
        try:
            try:
                worker.call(partial(_send, opening), timeout=max(0.1, deadline - self._clock()))
            except (StatementError, DomainError) as exc:
                self._reset(lease, outcome)
                outcome.status, outcome.error_code, outcome.error_message = "error", exc_code(exc), exc.message
                return
            except TimeoutError:
                self._reset(lease, outcome)
                outcome.status = "timeout"
                return
            if not self._statement(lease, events, request, deadline, control, outcome):
                return  # the session was closed under it
        finally:
            worker.clear_emitter(events)
        try:
            worker.call(partial(_end, [around[1]] if around else [], frame.rolls_back, list(frame.end)),
                        timeout=FRAME_END_S)
        except Exception as exc:  # ROLLBACK or the lock release failed: never reuse this session (D-08-3)
            logger.warning("MCP: the frame's end failed on '{}' ({})", request.profile.name,
                           getattr(exc, "code", None) or type(exc).__name__)
            self._reset(lease, outcome)

    def _statement(self, lease: Lease, events: _Events, request: ReadRequest, deadline: float, control: CallControl,
                   outcome: ReadOutcome) -> bool:
        """Run it and read its pages; False when the session had to be closed."""
        rid, worker = events.request_id, lease.worker
        context = request.query_context or lease.default_context
        sql = request.sql
        if request.explain and (head := explain_head(request.dialect, lease.server_version)) is not None:
            sql = f"{head} {sql}"  # D-08-10; the worker's check gates it again, as an explain's text
        try:
            worker.submit(ExecuteCommand(request_id=rid, sql=sql, page_size=FIRST_PAGE, history=False,
                                         query_context=context))
        except DomainError as exc:  # the session closed meanwhile (the profile changed)
            outcome.status, outcome.error_code, outcome.error_message = "error", exc.code, exc.message
            return False
        control.attach(lambda: worker.cancel(rid))
        budget: ResultBudget | None = None
        cancelled_at: float | None = None
        forced = timed_out = False
        try:
            while True:
                if cancelled_at is None:
                    wait = deadline - self._clock()
                else:
                    wait = cancelled_at + (CANCEL_GRACE_S if forced else FORCE_AFTER_S) - self._clock()
                event = events.next(wait)
                if event is None:
                    if cancelled_at is None:
                        timed_out, cancelled_at = True, self._clock()
                        worker.cancel(rid)
                    elif not forced:
                        forced = True
                        worker.cancel(rid)
                    else:
                        self._reset(lease, outcome)
                        outcome.status, outcome.error_message = "timeout", _STUCK
                        return False
                    continue
                if control.cancelled and cancelled_at is None:
                    cancelled_at = self._clock()
                match event:
                    case ResultSetEvent() if budget is None:
                        # null where the engine only knows a type from the values (SQLite without its peek)
                        outcome.columns = [{"name": c.name, "type": c.type_name or None} for c in event.columns]
                        budget = ResultBudget(outcome.columns, request.max_rows, request.max_cell_chars)
                    case RowsEvent() if budget is not None and (event.index == 0 or request.explain):
                        outcome.rows_read += len(event.rows)
                        budget.take(event.rows)
                    case ResultSetEndEvent(has_more=True):
                        if budget is None or budget.full or cancelled_at is not None:
                            outcome.more_rows = True
                            worker.submit(DiscardCommand(request_id=rid))
                        else:
                            worker.submit(FetchMoreCommand(request_id=rid, count=budget.next_page()))
                    case MessageEvent() if cancelled_at is None and len(outcome.notes) < NOTES_MAX:
                        text = note(event.text)
                        outcome.notes.append(text)
                        if budget is not None:
                            budget.spend(len(compact(text)) + 1)
                    case ErrorEvent():
                        outcome.status, outcome.error_code, outcome.error_message = "error", event.code, event.message
                    case FinishedEvent():
                        # cancelled with no cancel of ours or the client's: the server's own limit (statement_timeout)
                        timed_out = timed_out or event.timed_out or (event.cancelled and not control.cancelled)
                        if event.cancelled:
                            outcome.notes = [text for text in outcome.notes if text not in _CANCEL_TEXTS]
                        break
        finally:
            control.detach()
        if budget is not None:
            outcome.rows, outcome.rows_dropped, outcome.read_capped = budget.rows, budget.dropped, budget.read_capped
            outcome.more_rows = outcome.more_rows or budget.dropped > 0
        if control.cancelled:
            outcome.status = "cancelled"
        elif timed_out:
            outcome.status = "timeout"
        return True

    @staticmethod
    def _reset(lease: Lease, outcome: ReadOutcome) -> None:
        lease.drop()
        outcome.reset = True
        outcome.notes.append(RESET_NOTE)


def exc_code(exc: Exception) -> str | None:
    return getattr(exc, "code", None)
```

**New file `backend/dbaguy_backend/adapters/mysql/read_only.py`:**

```python
"""What keeps a MySQL session read-only and readable as DbaGuy's lexer reads it (spec ⑧ D-08-1, CR-08-5): an agent's
session leaves out the sql_mode flags that make the server read strings and quotes unlike the lexer (H2)."""

from mysql.connector.connection import MySQLConnection

from ...sql.dialects import Dialect, quote_literal
from . import catalog

# NO_BACKSLASH_ESCAPES and ANSI_QUOTES (and ANSI, which turns ANSI_QUOTES back on): with them, `'a\' INTO @x -- '` and
# `"a\" INTO @x -- "` hide an INTO from the lexer that the server runs (H2)
LEXER_UNSAFE_MODES = frozenset({"NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI"})


def lexer_safe(sql_mode: str) -> str:
    """`sql_mode` without LEXER_UNSAFE_MODES, the rest in its order."""
    return ",".join(mode for mode in sql_mode.split(",") if mode and mode.upper() not in LEXER_UNSAFE_MODES)


def unsafe_modes(sql_mode: str) -> list[str]:
    return [mode for mode in sql_mode.split(",") if mode.upper() in LEXER_UNSAFE_MODES]


def make_agent_sql_mode(conn: MySQLConnection) -> None:
    """At an agent connection's open, after the profile's own settings: its sql_mode without the unsafe flags."""
    row = catalog.first_row(conn, "SELECT @@SESSION.sql_mode")
    current = str(row[0]) if row and row[0] is not None else ""
    if unsafe_modes(current):
        conn.cmd_query(f"SET SESSION sql_mode = {quote_literal(Dialect.MYSQL, lexer_safe(current))}")
```

**Patch T6** (the files this task modifies: `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/sqlite/adapter.py`, `backend/dbaguy_backend/api/profiles.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/sessions/hooks.py`, `backend/dbaguy_backend/sessions/manager.py`, `backend/dbaguy_backend/sessions/worker.py`, `backend/dbaguy_backend/sql/dialects.py`). Save the block in your scratchpad as `08a-t6.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t6.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/adapters/mysql/adapter.py b/backend/dbaguy_backend/adapters/mysql/adapter.py
index da7d4d5..7983466 100644
--- a/backend/dbaguy_backend/adapters/mysql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mysql/adapter.py
@@ -44,7 +44,7 @@ from ..cancel_window import CancelWindow
 from ..columns import TableColumn
 from ..dump import DumpCatalog, DumpSession
 from ..sockets import enable_keepalive
-from . import catalog, columns, dump, editing, introspection
+from . import catalog, columns, dump, editing, introspection, read_only
 from . import schema as mysql_schema
 from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
 from .types import column_meta, row_converter
@@ -596,6 +596,11 @@ class MysqlAdapter(DatabaseAdapter):
                 except mysql.connector.Error as exc:
                     message = f"Could not apply the time_zone / sql_mode connection options: {exc}"
                     raise ConnectionFailed(message) from exc
+            if agent:  # spec ⑧ CR-08-5: never a mode in which the server reads a string the lexer reads otherwise
+                try:
+                    read_only.make_agent_sql_mode(conn)
+                except mysql.connector.Error as exc:
+                    raise ConnectionFailed(f"Could not set the agent connection's sql_mode: {exc}") from exc
             if profile.read_only:
                 # Last: nothing sent at connect may undo it, and the server enforces READ ONLY for writes hidden in
                 # functions or procedures too.
diff --git a/backend/dbaguy_backend/adapters/sqlite/adapter.py b/backend/dbaguy_backend/adapters/sqlite/adapter.py
index 79ee9a6..7472ab5 100644
--- a/backend/dbaguy_backend/adapters/sqlite/adapter.py
+++ b/backend/dbaguy_backend/adapters/sqlite/adapter.py
@@ -46,6 +46,28 @@ _NOT_UTF8 = re.compile(r"Could not decode to UTF-8 column '(.*?)' with text", re
 # never repeat, and 128 compiled 1,000-row INSERTs held 300 MB (⑥ A10): statements are compiled each time instead
 _STATEMENT_CACHE = 0
 _PROGRESS_OPS = 10_000  # VM instructions between two looks at the cancel flag: ~0.3% slower, stops within ~0.1 ms
+# An MCP agent's connection (spec ⑧ D-08-4): mode=ro lets ATTACH write the attached file (S4) and VACUUM INTO write a
+# new one (S3), and query_only still leaves VACUUM INTO's empty file (S5). The authorizer, which SQLite asks while it
+# prepares every statement, allows only reading: SELECT, reading a column, functions but load_extension, recursive
+# CTEs, and these pragmas, which have no form that writes (the catalog reads through some of them, H6).
+_AGENT_PRAGMAS = frozenset({
+    "table_list", "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
+    "database_list",
+})
+_AGENT_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE})
+
+
+def _agent_authorizer(action: int, arg1: str | None, arg2: str | None, database: str | None,
+                      source: str | None) -> int:
+    if action in _AGENT_ACTIONS:
+        return sqlite3.SQLITE_OK
+    if action == sqlite3.SQLITE_FUNCTION:  # arg2 is the function's name
+        return sqlite3.SQLITE_DENY if (arg2 or "").lower() == "load_extension" else sqlite3.SQLITE_OK
+    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _AGENT_PRAGMAS:
+        return sqlite3.SQLITE_OK
+    return sqlite3.SQLITE_DENY
+
+
 # Statements that control the transaction themselves or cannot run inside one: manual mode does not BEGIN for them.
 _NO_AUTO_BEGIN = frozenset(
     {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM", "PRAGMA", "ATTACH", "DETACH"}
@@ -340,9 +362,12 @@ class SqliteAdapter(DatabaseAdapter):
                                    cached_statements=_STATEMENT_CACHE)
             conn.execute("PRAGMA foreign_keys = ON")
             conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails for non-database files
+            if agent:
+                conn.execute("PRAGMA query_only = ON")
+                conn.set_authorizer(_agent_authorizer)  # last: nothing after it may change a setting
         except sqlite3.Error as exc:
             if conn is not None:
                 conn.close()
             raise ConnectionFailed(f"Cannot open {path}: {exc}") from exc
-        logger.info("Opened SQLite database {} (mode={})", path, mode)
+        logger.info("Opened SQLite database {} (mode={}{})", path, mode, ", an agent's" if agent else "")
         return SqliteConnection(conn, path)
diff --git a/backend/dbaguy_backend/api/profiles.py b/backend/dbaguy_backend/api/profiles.py
index 0121327..398c2a8 100644
--- a/backend/dbaguy_backend/api/profiles.py
+++ b/backend/dbaguy_backend/api/profiles.py
@@ -19,11 +19,12 @@ router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(
 
 def _forget_profile_state(container: AppContainer, profile_id: UUID) -> None:
     """After a profile changed or went away: drop what was learned from it: its metadata connections (the agents' too),
-    cached schema snapshots, field samples and its login's privileges."""
+    cached schema snapshots, field samples, its login's privileges and its agents' sessions (spec ⑧ R-08-5)."""
     container.metadata.invalidate(profile_id)
     container.schema_cache.invalidate(profile_id)
     container.field_samples.invalidate(profile_id)
     container.agent_login.forget(profile_id)
+    container.agent_sessions.close(profile_id)
 
 
 @router.get("", response_model=list[ProfileEntry])
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index b6e9ddb..eb2073e 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -13,6 +13,8 @@ from .adapters.registry import AdapterRegistry
 from .adapters.sqlite.adapter import SqliteAdapter
 from .agent.host import AgentHost
 from .agent.login import LoginRegistry
+from .agent.reader import AgentReader
+from .agent.sessions import AgentSessions
 from .agent.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
 from .agent.tools import AgentTools
 from .config import Settings, default_data_dir
@@ -118,6 +120,8 @@ class AppContainer:
         )
         self.sessions = SessionManager(self.connections, self.profiles, hooks, jobs=self.jobs)
         self.agent_login = LoginRegistry(self.metadata)
+        self.agent_sessions = AgentSessions(self.sessions, self.agent_login)
+        self.agent_reader = AgentReader(self.agent_sessions)
         self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache, self.agent_login)
         self.agent_host = AgentHost(
             self.agent_tools, token_store or _token_store(settings, secret_store), default_mcp_port(data_dir),
@@ -138,6 +142,7 @@ class AppContainer:
 
     def _close_agents(self) -> None:
         """The MCP server stopped: nothing an agent opened stays open."""
+        self.agent_sessions.close()
         self.metadata.close_all(agents_only=True)
 
     def _housekeep(self) -> None:
diff --git a/backend/dbaguy_backend/sessions/hooks.py b/backend/dbaguy_backend/sessions/hooks.py
index 10941dc..adc6911 100644
--- a/backend/dbaguy_backend/sessions/hooks.py
+++ b/backend/dbaguy_backend/sessions/hooks.py
@@ -1,12 +1,14 @@
 import threading
 import time
 from collections.abc import Callable
+from dataclasses import replace
 from datetime import datetime
 from pathlib import Path
 from uuid import UUID
 
 from ..adapters.base import StatementError
 from ..adapters.registry import AdapterRegistry
+from ..domain.classification import Classification, GuardReason, StatementKind
 from ..domain.data_jobs import BE_DUMPED, RUN_SQL_FILES, JobCommand, cannot
 from ..domain.edits import PLAN_EXPIRED, PLAN_EXPIRED_MESSAGE, EditPlan, PlanUnavailable
 from ..domain.errors import DomainError
@@ -15,9 +17,10 @@ from ..domain.history import HistoryRecord, HistoryStatus
 from ..domain.profiles import AnyProfile, Engine, SqliteProfile
 from ..domain.transfer import COPY_DATA
 from ..services.edit_plans import EditPlanStore
+from ..services.agent_gate import agent_read
 from ..services.history_service import HistoryService
 from ..services.profile_service import ProfileService
-from ..services.safety import GuardDecision, SafetyPolicy
+from ..services.safety import GuardAction, GuardDecision, SafetyPolicy
 from ..services.schema_cache import SchemaCache
 from ..services.transfer_plans import TransferPlanStore
 from .jobs.context import Job, JobHooks, PreparedJob
@@ -102,6 +105,25 @@ class SessionHooksFactory:
             take_edit_plan=take_edit_plan, jobs=self._job_hooks(profile, database),
         )
 
+    def build_agent(self, profile_id: UUID) -> WorkerHooks:
+        """An MCP agent's session (spec ⑧ D-08-1): the agent gate as its check (the MCP layer ran it already: this is
+        the backstop), no read-ahead while paused and no rows read ahead of a page (CR-08-7), and no History, schema
+        invalidation, edit plans or jobs: the MCP layer writes its own History (one record per tool call)."""
+        adapter = self._registry.get(self._profiles.get(profile_id).engine)
+        dialect = adapter.dialect
+
+        def check(sql: str, confirmed: bool) -> GuardDecision:
+            verdict = agent_read(sql, dialect)
+            if not verdict.allowed:  # an explain's text: the head and one statement the gate allows
+                verdict = agent_read(sql, dialect, "explain")
+            if verdict.allowed:
+                return GuardDecision(action=GuardAction.RUN, classification=verdict.classification)
+            kind = verdict.classification or Classification(kind=StatementKind.UNKNOWN)
+            return GuardDecision(action=GuardAction.BLOCK, reasons=[GuardReason.READ_ONLY], classification=kind)
+
+        return WorkerHooks(dialect=dialect, check=check, describe=False,
+                           capabilities=replace(adapter.capabilities, read_ahead_bytes=None))
+
     def _job_hooks(self, profile: AnyProfile, database: str | None) -> JobHooks:
         """A job's statement gate (gate_confirmed, the profile re-read at most once a second) and its History record."""
         profile_id = profile.id
diff --git a/backend/dbaguy_backend/sessions/manager.py b/backend/dbaguy_backend/sessions/manager.py
index 3263975..099cb93 100644
--- a/backend/dbaguy_backend/sessions/manager.py
+++ b/backend/dbaguy_backend/sessions/manager.py
@@ -38,11 +38,13 @@ class _Entry:
     info: SessionInfo
     attached: int = 0
     detached_at: float = 0.0
+    agent: bool = False  # an MCP agent's session (spec ⑧ D-08-1)
 
 
 class SessionManager:
-    """User sessions (one per editor tab, one per data-tools job). Closed explicitly, or once idle without a WebSocket:
-    nothing running or queued (D-06-10), and no attach and no request ending for `idle_timeout`."""
+    """User sessions (one per editor tab, one per data-tools job) and MCP agents' sessions (spec ⑧ D-08-1). Closed
+    explicitly, or once idle without a WebSocket: nothing running or queued (D-06-10), and no attach and no request
+    ending for `idle_timeout`."""
 
     def __init__(
         self, connections: ConnectionService, profiles: ProfileService, hooks: SessionHooksFactory | None = None,
@@ -58,18 +60,29 @@ class SessionManager:
         self._lock = threading.Lock()
 
     def create(self, request: SessionCreateRequest) -> SessionInfo:
-        profile = self._profiles.get(request.profile_id)
+        return self._open(request.profile_id, request.database, agent=False)
+
+    def create_agent(self, profile_id: UUID, database: str | None) -> SessionInfo:
+        """An MCP agent's session (D-08-1): on a read-only copy of the profile (`ConnectionService.open(agent=True)`),
+        with the agent's hooks; the reaper closes it like any other. A password it does not have is PasswordRequired:
+        it never asks (R-08-9)."""
+        return self._open(profile_id, database, agent=True)
+
+    def _open(self, profile_id: UUID, database: str | None, *, agent: bool) -> SessionInfo:
+        profile = self._profiles.get(profile_id)
         secrets = self._profiles.resolve_secrets(profile)
         session_id = uuid4().hex
         worker = SessionWorker(
-            f"{profile.name}:{session_id[:8]}",
-            lambda: self._connections.open(profile, request.database, secrets),
+            f"{'mcp:' if agent else ''}{profile.name}:{session_id[:8]}",
+            lambda: self._connections.open(profile, database, secrets, agent=agent),
             clock=self._clock,
         )
         try:
             worker.start()
             server = worker.call(lambda c: c.server_info())
-            if self._hooks is not None:
+            if self._hooks is not None and agent:
+                worker.configure(self._hooks.build_agent(profile.id))  # before it is registered
+            elif self._hooks is not None:
                 hooks = self._hooks.build(profile.id, server.current_database)
                 jobs = replace(hooks.jobs, announce=partial(self.jobs.begin, session_id, profile.id),
                                report=partial(self.jobs.report, session_id),
@@ -80,10 +93,21 @@ class SessionManager:
             raise
         info = SessionInfo(session_id=session_id, profile_id=profile.id, database=server.current_database, server=server)
         with self._lock:
-            self._sessions[session_id] = _Entry(worker=worker, info=info, detached_at=self._clock())
-        logger.info("Opened session {} for '{}'", session_id, profile.name)
+            self._sessions[session_id] = _Entry(worker=worker, info=info, detached_at=self._clock(), agent=agent)
+        logger.info("Opened {}session {} for '{}'", "an agent's " if agent else "", session_id, profile.name)
         return info
 
+    def hold(self, session_id: str) -> SessionWorker:
+        """Count `session_id` as attached until `release`: the reaper never closes it in between (a copy's source
+        session between two tables, an agent's session between the statements of one call)."""
+        entry = self._entry(session_id)
+        with self._lock:
+            entry.attached += 1
+        return entry.worker
+
+    def release(self, session_id: str) -> None:
+        self._detached(session_id)
+
     def _source(self, profile_id: UUID, database: str | None, session_id: str | None, *,
                 from_session: str) -> SourceLease:
         """A copy's source session (06c), for the copy running on `from_session`: the query tab's session the copy
@@ -97,23 +121,15 @@ class SessionManager:
             entry = self._entry(session_id)
             if entry.info.profile_id != profile_id or session_id == from_session:
                 raise PlanUnavailable(PLAN_MISMATCH, COPY_PLAN_EXPIRED)
-            with self._lock:
-                entry.attached += 1
-            return SourceLease(entry.worker, partial(self._unlease, session_id))
+            # a query tab's session the copy read from is the tab's again on release: one attach fewer
+            return SourceLease(self.hold(session_id), partial(self.release, session_id))
         info = self.create(SessionCreateRequest(profile_id=profile_id, database=database))
-        with self._lock:
-            if (own := self._sessions.get(info.session_id)) is not None:
-                own.attached += 1
-        return SourceLease(self.worker(info.session_id), partial(self._release, info.session_id))
+        return SourceLease(self.hold(info.session_id), partial(self._end_copy_source, info.session_id))
 
-    def _release(self, session_id: str) -> None:
+    def _end_copy_source(self, session_id: str) -> None:
         """A copy's own source session ends with it (never reused, as a job's own session)."""
         self._pop_and_close(session_id)
 
-    def _unlease(self, session_id: str) -> None:
-        """A query tab's session the copy read from is the tab's again: one attach fewer, as a socket that detaches."""
-        self._detached(session_id)
-
     def _detached(self, session_id: str) -> _Entry | None:
         """One attach fewer (a socket, or a copy's lease); idle time counts from the last one. None: it is gone."""
         with self._lock:
@@ -161,6 +177,14 @@ class SessionManager:
         for sid in ids:
             self._pop_and_close(sid)
 
+    def close_agents(self, profile_id: UUID | None = None) -> None:
+        """Close MCP agents' sessions: a profile's (it changed), or all (the MCP server stopped)."""
+        with self._lock:
+            ids = [sid for sid, e in self._sessions.items()
+                   if e.agent and (profile_id is None or e.info.profile_id == profile_id)]
+        for sid in ids:
+            self._pop_and_close(sid)
+
     def reap_idle(self) -> int:
         closed = 0
         for sid in self._idle_ids():
diff --git a/backend/dbaguy_backend/sessions/worker.py b/backend/dbaguy_backend/sessions/worker.py
index ae5b28b..9750b1c 100644
--- a/backend/dbaguy_backend/sessions/worker.py
+++ b/backend/dbaguy_backend/sessions/worker.py
@@ -182,6 +182,9 @@ class WorkerHooks:
     # The previewed grid-edit plan with this id, taken once (spec ⑤ D-05-7); raises PlanUnavailable
     take_edit_plan: Callable[[str], EditPlan] = _no_edit_plans
     jobs: JobHooks = JobHooks()  # data tools' jobs (spec ⑥ D-06-2)
+    # False: an execute's run() reads no rows ahead of fetch() and skips describing the columns first (an MCP agent's
+    # session, spec ⑧ CR-08-7: a page is all it holds); RunOptions.describe
+    describe: bool = True
 
 
 @dataclass
@@ -740,7 +743,8 @@ class SessionWorker:
             if not self._passes_policy(req, cmd.confirmed):
                 return
             req.query_context = options.query_context
-            options = replace(options, retry_safe=req.kind == StatementKind.READ)  # D-04-14: only a read is resent
+            # D-04-14: only a read is resent
+            options = replace(options, retry_safe=req.kind == StatementKind.READ, describe=self._hooks.describe)
             with self._timeout(req):
                 result = self._run_statement(req, "execute", cmd.sql, options)
                 budget = self._hooks.capabilities.read_ahead_bytes
diff --git a/backend/dbaguy_backend/sql/dialects.py b/backend/dbaguy_backend/sql/dialects.py
index 37b1ae2..fcace16 100644
--- a/backend/dbaguy_backend/sql/dialects.py
+++ b/backend/dbaguy_backend/sql/dialects.py
@@ -484,6 +484,55 @@ EXPLAIN_HEADS: dict[Dialect, tuple[str, ...]] = {
 }
 
 
+@dataclass(frozen=True)
+class ReadFrame:
+    """DbaGuy's own statements around an MCP agent's (spec ⑧ D-08-3), sent on the agent's session: `begin` before it
+    (`{timeout_ms}`: the call's timeout plus SERVER_TIMEOUT_MARGIN_S, the server's own limit as a backstop), the
+    adapter's rollback() after it when `rolls_back`, then `end`, which releases what a function may have locked for the
+    session (a ROLLBACK keeps session locks, H1-e, M5). `explain_around`: what an explain sends before and after the
+    statement instead of a head (T-SQL's SHOWPLAN: the statement is not run, T7)."""
+
+    begin: tuple[str, ...] = ()
+    end: tuple[str, ...] = ()
+    rolls_back: bool = False
+    explain_around: tuple[str, str] | None = None
+
+
+SERVER_TIMEOUT_MARGIN_S = 5
+_READ_FRAMES: dict[Dialect, ReadFrame] = {
+    # BEGIN READ ONLY on a session that is read-only by default too (P1, P2); statement_timeout: P10
+    Dialect.POSTGRES: ReadFrame(("BEGIN READ ONLY", "SET LOCAL statement_timeout = {timeout_ms}"),
+                                ("SELECT pg_advisory_unlock_all()",), rolls_back=True),
+    # an explicit READ ONLY transaction each time: a function can turn the session's default off (M3, M3e)
+    Dialect.MYSQL: ReadFrame(("SET SESSION max_execution_time = {timeout_ms}", "START TRANSACTION READ ONLY"),
+                             ("SELECT RELEASE_ALL_LOCKS()",), rolls_back=True),
+    # no read-only session at all: what slips through is rolled back (T6)
+    Dialect.TSQL: ReadFrame(("BEGIN TRANSACTION",), rolls_back=True,
+                            explain_around=("SET SHOWPLAN_TEXT ON", "SET SHOWPLAN_TEXT OFF")),
+    Dialect.SQLITE: ReadFrame(),  # mode=ro, query_only and the authorizer (D-08-4); no transaction to hold locks
+    Dialect.SQLPP: ReadFrame(),  # readonly=true on every request (C1); no transactions
+}
+_MYSQL_VERSION = re.compile(r"MySQL (\d+)\.(\d+)\.(\d+)")
+_MYSQL_TREE = (8, 0, 16)
+
+
+def read_frame(dialect: Dialect) -> ReadFrame:
+    return _READ_FRAMES[dialect]
+
+
+def explain_head(dialect: Dialect, server_version: str) -> str | None:
+    """What the explain tool puts before an agent's statement (D-08-10); None for T-SQL (SHOWPLAN). MySQL's tree format
+    needs 8.0.16 (MariaDB and older servers get the plain EXPLAIN)."""
+    heads = EXPLAIN_HEADS[dialect]
+    if not heads:
+        return None
+    if dialect == Dialect.MYSQL:
+        found = _MYSQL_VERSION.match(server_version)
+        if found is None or tuple(int(part) for part in found.groups()) < _MYSQL_TREE:
+            return heads[-1]
+    return heads[0]
+
+
 def transaction_frame(dialect: Dialect) -> TransactionFrame:
     frame = _TRANSACTION_FRAMES[dialect]
     if frame is None:
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_shape.py tests/test_mcp_reader.py tests/test_mcp_catalog.py tests/test_sessions.py tests/test_session_wiring.py tests/test_worker_protocol.py tests/test_session_worker.py tests/test_sqlite_adapter.py tests/test_mysql_sessions.py tests/test_postgres_sessions.py tests/test_transactions.py tests/test_job_framework.py tests/test_copy_framework.py tests/test_transfer_job.py`
Expected: all pass (planning: 263 passed in 24 s). Afterwards, no `zz_08` object is left on PG, MySQL or SQL Server.

- [ ] **Step 5: Measure (record for Task 9)**

Run the Appendix's `mem.py` from `backend/`.
Expected (planning, one run each):
- `repeat('x', 1000000) FROM generate_series(1, 1000)`: 36 rows, 37 read, 0.07 s, traced peak 9.6 MB.
- `repeat('x', 100000000) …`: 1 row, 0.9 s, traced peak 200 MB.
- 100 rows × 10 columns: 14–18 ms.

- [ ] **Step 6: Review against the pain points** (principle 5: what each server refuses with the gate taken out; Q2: no transaction, lock or setting carries over, and a stuck frame closes its session; Q1: one page in memory)

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/agent/sessions.py backend/dbaguy_backend/agent/reader.py \
  backend/dbaguy_backend/agent/shape.py backend/dbaguy_backend/adapters/mysql/read_only.py \
  backend/dbaguy_backend/sql/dialects.py backend/dbaguy_backend/sessions/manager.py \
  backend/dbaguy_backend/sessions/hooks.py backend/dbaguy_backend/sessions/worker.py \
  backend/dbaguy_backend/adapters/sqlite/adapter.py backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/container.py backend/dbaguy_backend/api/profiles.py backend/tests/test_mcp_reader.py \
  backend/tests/test_mcp_shape.py
git commit -m "feat(08a): agent sessions, read frames and the reader: one read per call, rolled back, paged within budget

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 7: MySQL's shared read-only guard — users' read-only tabs and agents (D-08-5, CR-08-11, CR-08-5)

**Suggested implementer:** Sonnet. It is one adapter hook with live regression tests, but read P11 before starting.

**Files:**
- Modify: `backend/dbaguy_backend/adapters/mysql/{read_only.py, adapter.py}`
- Test: `backend/tests/test_mysql_read_only.py` (new)

**Interfaces:**
- Consumes: Task 6's `read_only.py` (`unsafe_modes`), `AgentSessions.lease`, `AgentReader`.
- Produces:
  - `adapters/mysql/read_only.py`:
    - `SESSION_CHANGED = "session_changed"`, `SESSION_CHANGED_MESSAGE`
    - `ReadOnlyGuard(conn, thread_id, *, agent)`, with `.before_statement(sql, in_transaction)`
  - `MysqlConnection(…, *, agent=False)`: a read-only connection's `run()` calls the guard before `take_over()`. `MysqlAdapter.open` passes `agent`.

**Decisions this task carries:**
- D-08-5 (M3):
  - A stored function can switch its session to READ WRITE from inside a SELECT. Neither the statement's end nor a ROLLBACK switches it back.
  - So before every statement of a read-only session outside a transaction, the guard reads `@@SESSION.transaction_read_only, @@SESSION.sql_mode`. If the session is no longer read-only, it sends `SET SESSION TRANSACTION READ ONLY` and logs a WARNING ("…a read-only session was switched to READ WRITE; restored").
  - Inside a transaction there is no check: the transaction's access mode was fixed when it began, and the agent's own `START TRANSACTION READ ONLY` holds (M3e).
  - This also fixes the user's read-only tabs.
- CR-08-5: on an agent's session, a lexer-unsafe flag in `sql_mode` raises `StatementNotSent` with code `session_changed`, and nothing is sent. The reader (Task 6) closes that session; the next call opens a clean one.
  - A function cannot do this: MySQL restores `sql_mode` when a routine returns (H2). The check is the backstop.
- P11:
  - No check before a diagnostic statement (SHOW WARNINGS, SHOW ERRORS, SHOW COUNT…, GET DIAGNOSTICS). It writes nothing, and the check's own SELECT would empty its answer. The existing `test_a_set_var_hint_cannot_lift_read_only` caught this while planning: SHOW WARNINGS came back empty.
  - Cost: one round trip per statement outside a transaction on read-only MySQL sessions. A14: the pure driver does not read session-state tracking (`parse_ok` stops at the info text, mysql-connector 26.7.0), so it cannot be avoided.
  - Measured while planning: the agent's `SELECT 1` takes 2.8 ms median in-process (dev). On a remote server each statement of a read-only tab gets one RTT more.

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_mysql_read_only.py`:**

```python
"""MySQL's shared read-only guard (spec ⑧ D-08-5, CR-08-11, CR-08-5): M3's function that switches a read-only session to
READ WRITE no longer opens a way to write, in a user's read-only tab or an agent's session; an agent's session refuses
to go on with a lexer-unsafe sql_mode. The function and table are zz_08_*, dropped by exact name."""

import time
from uuid import UUID

import mysql.connector
import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.mysql.adapter import MysqlAdapter
from dbaguy_backend.adapters.mysql.read_only import lexer_safe, unsafe_modes
from dbaguy_backend.agent.reader import ReadRequest
from dbaguy_backend.domain.classification import Classification, StatementKind
from dbaguy_backend.domain.execution import ExecuteCommand
from dbaguy_backend.services.agent_gate import AgentVerdict
from dbaguy_backend.sessions.worker import SessionWorker
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.test_sessions import Recorder, finished

from .agent_support import container, share
from .support import (
    MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, make_mysql_profile, mysql_driver_kwargs, requires_mysql, scaled,
)

__all__ = ["container"]


def test_the_unsafe_modes_and_ansi_which_brings_one_back_are_left_out():
    mode = "REAL_AS_FLOAT,PIPES_AS_CONCAT,ANSI_QUOTES,IGNORE_SPACE,ONLY_FULL_GROUP_BY,ANSI,NO_BACKSLASH_ESCAPES"
    assert lexer_safe(mode) == "REAL_AS_FLOAT,PIPES_AS_CONCAT,IGNORE_SPACE,ONLY_FULL_GROUP_BY"
    assert unsafe_modes(mode) == ["ANSI_QUOTES", "ANSI", "NO_BACKSLASH_ESCAPES"]
    assert lexer_safe("STRICT_TRANS_TABLES") == "STRICT_TRANS_TABLES" and lexer_safe("") == ""


@pytest.fixture
def flipper():
    """zz_08_rw(): a stored function that switches its session to READ WRITE (M3), and the table it would let in."""
    owner = mysql.connector.connect(**mysql_driver_kwargs(autocommit=True))
    cursor = owner.cursor()
    try:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_rw")
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        cursor.execute("CREATE TABLE zz_08_victim (id INT)")
        cursor.execute("CREATE FUNCTION zz_08_rw() RETURNS INT NO SQL BEGIN "
                       "SET SESSION TRANSACTION READ WRITE; RETURN 1; END")
        yield cursor
    finally:
        cursor.execute("DROP FUNCTION IF EXISTS zz_08_rw")
        cursor.execute("DROP TABLE IF EXISTS zz_08_victim")
        owner.close()


def _run(worker, recorder, rid, sql):
    worker.submit(ExecuteCommand(request_id=rid, sql=sql))
    recorder.wait_for(finished(rid), timeout=scaled(30))
    errors = recorder.of(rid, "error")
    return errors[0].code if errors else None


@requires_mysql
def test_a_read_only_tab_stays_read_only_after_a_function_switches_it(flipper):
    profile = make_mysql_profile(read_only=True)
    lease = FakeLease(MysqlAdapter().open(profile, Endpoint(MYSQL_HOST, MYSQL_PORT), MYSQL_PASSWORD, None))
    worker = SessionWorker("zz_08 tab", lambda: lease)
    worker.start()
    recorder = Recorder()
    worker.set_emitter(recorder)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        assert _run(worker, recorder, "flip", "SELECT zz_08_rw()") is None
        assert _run(worker, recorder, "write", "INSERT INTO zz_08_victim VALUES (1)") == "1792"  # M3d, closed
    finally:
        logger.remove(sink)
        worker.close()
    assert any("switched to READ WRITE; restored" in text for text in warnings)
    flipper.execute("SELECT COUNT(*) FROM zz_08_victim")
    assert flipper.fetchall() == [(0,)]


@requires_mysql
def test_an_agent_session_stays_read_only_too(container, flipper, monkeypatch):
    allowed = AgentVerdict(classification=Classification(kind=StatementKind.READ))
    monkeypatch.setattr("dbaguy_backend.sessions.hooks.agent_read", lambda sql, dialect, purpose="query": allowed)
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    read = container.agent_reader.read
    assert read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT zz_08_rw()")).status == "ok"
    written = read(ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="INSERT INTO zz_08_victim VALUES (1)"))
    assert written.error_code == "1792"
    flipper.execute("SELECT COUNT(*) FROM zz_08_victim")
    assert flipper.fetchall() == [(0,)]


@requires_mysql
def test_an_agent_session_whose_sql_mode_changed_refuses_and_is_reset(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    profile = container.profiles.get(UUID(pid))
    request = ReadRequest(profile=profile, dialect=Dialect.MYSQL, sql="SELECT 1")
    assert container.agent_reader.read(request).rows == [[1]]
    with container.agent_sessions.lease(profile, None, deadline=time.monotonic() + 10) as lease:  # as H2's change
        lease.worker.call(lambda c: c._conn.cmd_query("SET SESSION sql_mode = 'ANSI_QUOTES'"))
    refused = container.agent_reader.read(request)
    assert (refused.status, refused.error_code, refused.reset) == ("error", "session_changed", True)
    assert refused.error_message.startswith("The connection's sql_mode changed")
    assert container.agent_reader.read(request).rows == [[1]]  # a new session, with a clean sql_mode
```

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_read_only.py`
Expected:
- `test_a_read_only_tab_stays_read_only_after_a_function_switches_it` fails: the INSERT succeeds (M3d).
- `test_an_agent_session_whose_sql_mode_changed_refuses_and_is_reset` fails: the call runs.
- The pure test passes.
- `test_an_agent_session_stays_read_only_too` passes already: the frame's own `START TRANSACTION READ ONLY` holds (M3e). The guard keeps it passing when the frame changes.

- [ ] **Step 3: Write the code**

**Patch T7** (the files this task modifies: `backend/dbaguy_backend/adapters/mysql/adapter.py`, `backend/dbaguy_backend/adapters/mysql/read_only.py`). Save the block in your scratchpad as `08a-t7.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t7.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/adapters/mysql/adapter.py b/backend/dbaguy_backend/adapters/mysql/adapter.py
index 7983466..cf33cf5 100644
--- a/backend/dbaguy_backend/adapters/mysql/adapter.py
+++ b/backend/dbaguy_backend/adapters/mysql/adapter.py
@@ -44,8 +44,9 @@ from ..cancel_window import CancelWindow
 from ..columns import TableColumn
 from ..dump import DumpCatalog, DumpSession
 from ..sockets import enable_keepalive
-from . import catalog, columns, dump, editing, introspection, read_only
+from . import catalog, columns, dump, editing, introspection
 from . import schema as mysql_schema
+from .read_only import ReadOnlyGuard, make_agent_sql_mode
 from .settings import URL_SCHEMES, connect_params, kill_params, parse_mysql_url, requires_tls, session_settings
 from .types import column_meta, row_converter
 
@@ -72,7 +73,7 @@ _AT_LINE = re.compile(r" at line (\d+)\s*$")
 # (`/*!50000 CALL p() */` gives ""), so a CALL written that way is caught only when its text says "autocommit".
 _MENTIONS_AUTOCOMMIT = re.compile(r"autocommit", re.IGNORECASE)
 _MAY_SET_AUTOCOMMIT = frozenset({"CALL", "EXECUTE"})  # a procedure or prepared statement may switch it too
-_READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"
+_READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"  # at connect; ReadOnlyGuard puts it back when switched off
 # ERR packets carry no status flags, but a server error can end the transaction: a deadlock rolls it back, and DDL
 # commits it before it runs, even DDL that then fails. This statement reads the flags again (R-03a-T9b) and, being a
 # diagnostic statement, keeps what SHOW WARNINGS, SHOW ERRORS and GET DIAGNOSTICS report (R-03a-T9e; `DO 0` cleared it).
@@ -190,10 +191,13 @@ class MysqlResult(StatementResult):
 
 
 class MysqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
-    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None) -> None:
+    def __init__(self, conn: MySQLConnection, kill_params: dict[str, Any], read_only: bool, database: str | None,
+                 *, agent: bool = False) -> None:
         self._conn = conn
         self._kill_params = kill_params
         self._read_only = read_only  # READ ONLY was set on the session at connect; the server enforces it
+        # D-08-5: a function can switch the session to READ WRITE: looked at before each statement outside a transaction
+        self._guard = ReadOnlyGuard(conn, conn.connection_id, agent=agent) if read_only else None
         self._database = database
         self._thread_id = conn.connection_id
         self._autocommit = True  # connections open in auto-commit mode (settings fix it)
@@ -400,6 +404,11 @@ class MysqlConnection(DatabaseConnection, RowEditing, DataTools, Dumping):
         keyword = first_keyword(sql, Dialect.MYSQL)
         if _MENTIONS_AUTOCOMMIT.search(sql) or keyword in _MAY_SET_AUTOCOMMIT:
             self._autocommit_stale = True
+        if self._guard is not None:  # before take_over: a cancel meanwhile is kept, and the statement never goes
+            try:
+                self._guard.before_statement(sql, self.in_transaction())
+            except mysql.connector.Error as exc:
+                raise self.translate_error(exc) from exc
         # Right before the first send, after the clear: a cancel kept while armed means nothing is sent; from here
         # one sets the KILL flag and KILLs.
         self._window.take_over()
@@ -598,7 +607,7 @@ class MysqlAdapter(DatabaseAdapter):
                     raise ConnectionFailed(message) from exc
             if agent:  # spec ⑧ CR-08-5: never a mode in which the server reads a string the lexer reads otherwise
                 try:
-                    read_only.make_agent_sql_mode(conn)
+                    make_agent_sql_mode(conn)
                 except mysql.connector.Error as exc:
                     raise ConnectionFailed(f"Could not set the agent connection's sql_mode: {exc}") from exc
             if profile.read_only:
@@ -617,4 +626,4 @@ class MysqlAdapter(DatabaseAdapter):
             endpoint.tls_server_name or endpoint.host, endpoint.port, params.get("database"), profile.ssl.mode,
             " (active)" if conn.is_secure else "", conn.connection_id, " read_only=True" if profile.read_only else "",
         )
-        return MysqlConnection(conn, kill_params(params), profile.read_only, params.get("database"))
+        return MysqlConnection(conn, kill_params(params), profile.read_only, params.get("database"), agent=agent)
diff --git a/backend/dbaguy_backend/adapters/mysql/read_only.py b/backend/dbaguy_backend/adapters/mysql/read_only.py
index c679907..9abe329 100644
--- a/backend/dbaguy_backend/adapters/mysql/read_only.py
+++ b/backend/dbaguy_backend/adapters/mysql/read_only.py
@@ -1,11 +1,42 @@
-"""What keeps a MySQL session read-only and readable as DbaGuy's lexer reads it (spec ⑧ D-08-1, CR-08-5): an agent's
-session leaves out the sql_mode flags that make the server read strings and quotes unlike the lexer (H2)."""
+"""What keeps a MySQL session read-only and readable as DbaGuy's lexer reads it (spec ⑧ D-08-1, D-08-5, CR-08-5,
+CR-08-11). A stored function can switch the session to READ WRITE from inside a SELECT, and neither a ROLLBACK nor the
+end of the statement switches it back (M3), so every statement a read-only session runs outside a transaction is
+preceded by a look at the session (`ReadOnlyGuard`): a user's read-only tab and an agent's session alike. An agent's
+session also leaves out the sql_mode flags that make the server read strings and quotes unlike the lexer (H2), and
+refuses to go on if they are back.
 
+The look is one round trip per statement outside a transaction: the pure driver does not read the session-state
+tracking an OK packet can carry (A14, checked on mysql-connector 26.7.0: `parse_ok` stops at the info text)."""
+
+from loguru import logger
 from mysql.connector.connection import MySQLConnection
 
 from ...sql.dialects import Dialect, quote_literal
+from ...sql.lexer import INSIGNIFICANT, TokenKind, scan
+from ..base import StatementNotSent
 from . import catalog
 
+SESSION_CHANGED = "session_changed"
+SESSION_CHANGED_MESSAGE = "The connection's sql_mode changed: the call was not run and the connection was reset"
+_READ_ONLY_SESSION = "SET SESSION TRANSACTION READ ONLY"
+_STATE = "SELECT @@SESSION.transaction_read_only, @@SESSION.sql_mode"
+# Diagnostic statements: they write nothing, and the look before them would clear what they report (a SELECT empties
+# the warnings SHOW WARNINGS shows)
+_DIAGNOSTIC_HEADS = frozenset({("SHOW", "WARNINGS"), ("SHOW", "ERRORS"), ("SHOW", "COUNT"), ("GET", "DIAGNOSTICS"),
+                               ("GET", "CURRENT"), ("GET", "STACKED")})
+
+
+def _head(sql: str) -> tuple[str, ...]:
+    """The statement's first two words, upper-cased ("" for a token that is not a word)."""
+    words: list[str] = []
+    for token in scan(sql, Dialect.MYSQL):
+        if token.kind in INSIGNIFICANT:
+            continue
+        words.append(token.upper if token.kind == TokenKind.WORD else "")
+        if len(words) == 2:
+            break
+    return tuple(words)
+
 # NO_BACKSLASH_ESCAPES and ANSI_QUOTES (and ANSI, which turns ANSI_QUOTES back on): with them, `'a\' INTO @x -- '` and
 # `"a\" INTO @x -- "` hide an INTO from the lexer that the server runs (H2)
 LEXER_UNSAFE_MODES = frozenset({"NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "ANSI"})
@@ -20,6 +51,31 @@ def unsafe_modes(sql_mode: str) -> list[str]:
     return [mode for mode in sql_mode.split(",") if mode.upper() in LEXER_UNSAFE_MODES]
 
 
+class ReadOnlyGuard:
+    """Before a statement of a read-only session (D-08-5): outside a transaction (inside one, its access mode was fixed
+    when it began), the session is READ ONLY again if a function switched it, with a WARNING; an agent's session
+    (`agent`) with a lexer-unsafe sql_mode raises StatementNotSent `session_changed` (CR-08-5), nothing sent. Not before
+    a diagnostic statement (SHOW WARNINGS, GET DIAGNOSTICS…): it writes nothing, and the look would clear its answer."""
+
+    def __init__(self, conn: MySQLConnection, thread_id: int, *, agent: bool) -> None:
+        self._conn = conn
+        self._thread_id = thread_id
+        self._agent = agent
+
+    def before_statement(self, sql: str, in_transaction: bool) -> None:
+        if in_transaction or _head(sql) in _DIAGNOSTIC_HEADS:
+            return
+        row = catalog.first_row(self._conn, _STATE) or (1, "")
+        read_only, sql_mode = int(row[0] or 0), str(row[1] or "")
+        if self._agent and (unsafe := unsafe_modes(sql_mode)):
+            logger.warning("MySQL thread {}: an agent's session has {} in its sql_mode: refused", self._thread_id,
+                           ", ".join(unsafe))
+            raise StatementNotSent(SESSION_CHANGED_MESSAGE, code=SESSION_CHANGED)
+        if not read_only:
+            self._conn.cmd_query(_READ_ONLY_SESSION)
+            logger.warning("MySQL thread {}: a read-only session was switched to READ WRITE; restored", self._thread_id)
+
+
 def make_agent_sql_mode(conn: MySQLConnection) -> None:
     """At an agent connection's open, after the profile's own settings: its sql_mode without the unsafe flags."""
     row = catalog.first_row(conn, "SELECT @@SESSION.sql_mode")
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mysql_read_only.py tests/test_mcp_reader.py tests/test_mysql_adapter.py tests/test_mysql_sessions.py tests/test_run_file_job.py`
Expected: all pass (planning: 219 passed in 21 s). `test_a_set_var_hint_cannot_lift_read_only` is among them, and SHOW WARNINGS still answers 3637.

- [ ] **Step 5: Review against the pain points** (Q3: the read-only tab's hole is closed; Q1: one round trip per statement on read-only MySQL sessions, documented)

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/adapters/mysql/read_only.py backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/tests/test_mysql_read_only.py
git commit -m "fix(08a): MySQL read-only sessions stay READ ONLY after a function switches them (D-08-5), tabs and agents

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 8: The query tools — `run_query`, `explain`, `sample_fields`, their History, timeouts and cancel, the log audit

**Suggested implementer:** Opus. It joins every earlier task, adds the client's cancel across the SDK's task and a worker thread, and owns the honesty of History and the log.

**Files:**
- Create:
  - `backend/dbaguy_backend/agent/queries.py`
  - `backend/tests/test_mcp_queries.py`
- Modify:
  - `backend/dbaguy_backend/agent/{server.py, host.py}`
  - `backend/dbaguy_backend/services/{agent_gate.py, field_sample_service.py}`
  - `backend/dbaguy_backend/container.py`
  - `backend/tests/agent_support.py`
- Test: `backend/tests/test_mcp_queries.py`

**Interfaces:**
- Consumes:
  - Task 1's `run_tool`, `client_name`, `build_server`, `AgentHost`.
  - Task 2's `AgentTools.shared`, `answered`.
  - Task 4's `agent_read`.
  - Task 5's `HistoryService.record / refused`, `HistoryRecord(source, client)`.
  - Task 6's `AgentReader`, `ReadRequest`, `CallControl`, `ReadOutcome`.
  - `FieldSampleService`, `SettingsService`, `adapters/couchbase/explain.analyze`.
- Produces:
  - `agent/queries.py`:
    - `QueryTools(tools, reader, history, samples, settings)`, with `.run_query(connection, sql, database=None, query_context=None, max_rows, max_cell_chars, timeout_s=None, *, client, control)`, `.explain(connection, sql, database=None, query_context=None, timeout_s=None, *, client, control)` and `.sample_fields(connection, bucket, scope, collection, size=None, *, client)`.
    - The constants `TIMEOUT_DEFAULT_S`, `TIMEOUT_MAX_S`, `TIMEOUT_RED_S`, `SQL_MAX`, `SAMPLE_MAX`.
  - `services/agent_gate.SERVER_READ_ONLY`: the codes each server answers a write with on an agent's session.
  - `FieldSampleService.sample(…, *, agent=False)`.
  - `run_tool(tool, client, call, control=None)`: the client's cancel cancels `control`, on a thread of its own.
  - `build_server(tools, queries)`, `build_app(tools, queries, *, port, token)`.
  - `AgentHost(tools, queries, tokens, default_port, *, on_stop)`.
  - `container.agent_queries`.
  - `tests/agent_support.call_tool` returns the SDK's own plain-text refusal of an argument as it is.
  - `tests/agent_support.mcp_session(port, *, token, mode, client)`: an async context manager holding one MCP client session open (`_call` uses it).

**Decisions this task carries:**
- The `run_query` result is §3.5's: `{columns, rows, row_count, more_rows, truncated: null | {rows_dropped_for_size}, elapsed_ms, notes}`. One note at most says why it stopped: the read cap, then the response size, then "More rows exist: raise max_rows (at most 1000) or narrow the query".
- The `explain` result:
  - `{plan, elapsed_ms, notes}`, and Couchbase adds `findings` (④'s `analyze`).
  - `plan` is the first column of every row, one line each: PG's QUERY PLAN, MySQL's tree, SQL Server's StmtText (the statement, then the operators), Couchbase's JSON. SQLite's rows become an indented tree.
  - One plan cell holds at most 30,000 characters.
  - History keeps `EXPLAIN <statement>`. SQL Server's record is marked `-- SHOWPLAN_TEXT (the statement was not run)`.
- `sample_fields`:
  - Couchbase only; other engines get `not_allowed`.
  - It returns the cached sample when there is one and no `size` is asked. Otherwise it takes a new one on the agents' catalog connection, `size` capped at 1,000.
  - History keeps `-- MCP sample_fields <bucket.scope.collection> (<n> documents)`.
  - The fields' list stops at the response budget, with a note.
- CR-08-8: `timeout_s` defaults to 30 and is at most 60; on a red connection it is at most 30. A larger value is lowered, with a note.
- Couchbase: `query_context` is `bucket` or `bucket.scope` (`QueryContext.parse`); without it, the profile's default. Another engine with `query_context` gets `invalid_argument`. Couchbase ignores `database`.
- History (P3): every call is one record, `source="mcp"` with the client's name:
  - `ok`, `error` (the server's message, up to 500 characters), `timeout` or `cancelled`;
  - `refused` through `HistoryService.refused` (merged), before anything connects;
  - an `error` for a call that never reached its statement (a password, a session that cannot open, `busy`).
  - The text and the client's name are made well-formed (U+FFFD for a lone surrogate): JSON can carry one, and the history file cannot store it.
- Errors (§4):
  - The worker's backstop `read_only` → `refused`.
  - `session_changed` keeps its message.
  - A server's read-only refusal (`SERVER_READ_ONLY`: PG 25006, MySQL 1792, SQLite `SQLITE_READONLY`/`SQLITE_AUTH`, Couchbase 1000) gets " (DbaGuy's MCP access is read-only)".
  - Every server error's message ends with its code in parentheses.
- A4: an async tool awaits `to_thread.run_sync(…, abandon_on_cancel=True)`. On cancellation, `CallControl.cancel` runs on a new thread, because a PostgreSQL cancel can wait up to 3 s, and the reader's statement is cancelled.
  - The client cancels in two ways. At 2025-11-25 it sends `notifications/cancelled`, and the SDK cancels the tool's scope. At 2026-07-28 it closes the request's stream, which the SDK notices only on its SSE path (P18, Task 1's `json_response=False`).
  - The test keeps the client's session open, as Claude Code does on Esc, and cancels only the call. It asserts that the statement was running when the client gave up: a cursor `FETCH` waiting in `PgSleep`, since the frame's read-only transaction reads through a cursor. Then it asserts that the statement stopped.
  - Measured while planning: the statement stops 0.27 s after the client gives up, in both protocol versions.
- D-08-14: the tools log their own INFO line, and mark their failures `logged`:
  - `MCP run_query on '<profile>' by <client>: <code or outcome>, <n> rows, <ms> ms`
  - `MCP refused <tool> on '<profile>' by <client>: <reason>`
  - The audit test checks that no SQL, value, server text or token is in the INFO log.
- P15: a `sql` over 20,000 characters is refused by the SDK's own argument check. Its plain-text error is not in History, which never stores more than 20,000 characters.
- P9: SQL Server's column types without the describe are coarse (A13: `bigint` and `bit` show as `int`, `datetime2` as `unknown`). Measured, documented, and enough for an agent.

- [ ] **Step 1: Write the tests**

**New file `backend/tests/test_mcp_queries.py`:**

```python
"""run_query, explain and sample_fields over MCP (spec ⑧ §3.5, §3.6, §4, D-08-13, D-08-14; A4, A8, A11, A13): results,
refusals and their History, access levels, timeouts, a client's cancel, the plan per engine, and what the log may say.
Seeds are only read; nothing is created on a server."""

import sqlite3
import time
from types import SimpleNamespace

import anyio
import psycopg
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from dbaguy_backend.agent.server import client_name
from dbaguy_backend.agent.tools import ToolFailure
from dbaguy_backend.app import create_app
from dbaguy_backend.domain.profiles import ProfileUpsertRequest, SqliteProfile

from .agent_support import AUTH, MCP_TOKEN, call_tool, container, mcp_session, serving, share
from .support import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, make_couchbase_profile, make_mssql_profile, make_mysql_profile,
    make_pg_profile, requires_couchbase, requires_mssql, requires_mysql, requires_pg, scaled, wait_until,
)

__all__ = ["container"]


@pytest.fixture
def lite(tmp_path):
    path = tmp_path / "zz_08.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT); "
                       "INSERT INTO t (note) VALUES ('it''s 中文'), (NULL), ('" + "x" * 3000 + "');")
    conn.close()
    return path


def _lite(container, path, access="query", **changes) -> str:
    return share(container, SqliteProfile(id="00000000-0000-0000-0000-000000000001", engine="sqlite",
                                          name="zz_08 lite", path=str(path)), access=access, **changes)


def test_run_query_over_both_protocols_and_its_history(container, lite):
    pid = _lite(container, lite)
    with serving(container) as port:
        for mode in ("legacy", "2026-07-28"):
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT id, note FROM t ORDER BY id",
                                                         "max_cell_chars": 10}, mode=mode, client=f"pytest {mode}")
            assert not error
            assert data["columns"] == [{"name": "id", "type": None}, {"name": "note", "type": None}]  # no peek
            assert data["rows"][:2] == [[1, "it's 中文"], [2, None]]
            assert data["rows"][2][1] == {"$type": "text", "preview": "x" * 10, "size": 3000, "truncated": True}
            assert (data["row_count"], data["more_rows"], data["truncated"]) == (3, False, None)
        error, more = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT id FROM t", "max_rows": 2})
        assert more["more_rows"] and more["notes"] == [
            "More rows exist: raise max_rows (at most 1000) or narrow the query"]
    page = container.history.search(source="mcp")
    assert [(e.client, e.status, e.rows) for e in page.items] == [
        ("pytest", "ok", 2), ("pytest 2026-07-28", "ok", 3), ("pytest legacy", "ok", 3)]
    assert container.history.search(source="app").items == []


def test_refused_statements_are_recorded_merged_and_never_connect(container, lite, monkeypatch):
    pid = _lite(container, lite)
    opened = []
    monkeypatch.setattr(container.agent_sessions, "lease", lambda *a, **k: opened.append(a))
    with serving(container) as port:
        for _ in range(3):
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "DELETE FROM t"})
            assert error and data["error"]["code"] == "refused"
            assert data["error"]["message"] == ("DbaGuy runs exactly one read statement per call: starts with DELETE: "
                                                "only SELECT, WITH, VALUES are run")
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "EXPLAIN SELECT 1"})
        assert data["error"]["code"] == "refused"
    assert opened == []
    [explained, deleted] = container.history.search(source="mcp").items
    assert (deleted.sql, deleted.status, deleted.repeats, deleted.client) == ("DELETE FROM t", "refused", 3, "pytest")
    assert deleted.error.startswith("DbaGuy runs exactly one read statement per call")


def test_text_a_driver_cannot_send_is_refused_and_still_recorded(container, lite):
    pid = _lite(container, lite)  # JSON can carry "\ud800"; the history file cannot store it
    with pytest.raises(ToolFailure) as refused:
        container.agent_queries.run_query(pid, "SELECT '\ud800'", client="pytest")
    assert refused.value.code == "refused" and "half of a surrogate pair" in refused.value.message
    [record] = container.history.search(source="mcp").items
    assert (record.sql, record.status) == ("SELECT '\ufffd'", "refused")
    named = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        client_info=SimpleNamespace(name="claude\x07 \ud800" + "x" * 100))))
    assert client_name(named) == "claude \ufffd" + "x" * 72  # D-08-7: no control characters, 80 at most


def test_a_connection_switched_off_is_gone_at_once_with_its_sessions(container, lite):
    pid = _lite(container, lite)
    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client, \
            serving(container) as port:
        assert call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})[0] is False
        assert any(e.agent for e in container.sessions._sessions.values())
        body = {"profile": {"engine": "sqlite", "name": "zz_08 lite", "path": str(lite), "mcp_access": "off"}}
        assert client.put(f"/profiles/{pid}", json=body, headers=AUTH).status_code == 200
        assert not any(e.agent for e in container.sessions._sessions.values())
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        assert data["error"]["code"] == "unknown_connection"
        assert call_tool(port, "list_connections")[1]["connections"] == []


def test_schema_access_cannot_query_and_off_is_unknown(container, lite):
    pid = _lite(container, lite, access="schema")
    with serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        assert data["error"] == {"code": "not_allowed", "message": (
            "'zz_08 lite' allows schema only: run_query needs 'Read-only queries' in DbaGuy's connection settings")}
        error, data = call_tool(port, "sample_fields", {"connection": pid, "bucket": "b", "scope": "s",
                                                        "collection": "c"})
        assert data["error"]["code"] == "not_allowed"
        error, data = call_tool(port, "run_query", {"connection": "nope", "sql": "SELECT 1"})
        assert data["error"]["code"] == "unknown_connection"
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "x" * 20_001})
        assert error  # the SDK's argument check: at most 20,000 characters (CR-08-3)


def test_a_locked_connection_says_so_and_asks_for_nothing(container):
    draft = make_pg_profile(name="zz_08 locked", save_password=False).model_dump(exclude={"id"})
    request = ProfileUpsertRequest.model_validate({"profile": draft | {"mcp_access": "query"}})
    pid = str(container.profiles.create(request).profile.id)  # no password saved, none unlocked
    with serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1"})
        listed = call_tool(port, "list_connections")[1]["connections"]
    assert data["error"]["code"] == "password_required"
    assert data["error"]["message"] == ("'zz_08 locked' needs its password: connect to it in DbaGuy first (or unlock "
                                        "it). DbaGuy never asks for a password over MCP")
    assert listed[0]["needs_unlock"] is True
    assert container.history.search(source="mcp").items[0].status == "error"


@requires_pg
def test_timeouts_and_production_connections(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    red = share(container, make_pg_profile(name="zz_08 red", color="red"))
    with serving(container) as port:
        started = time.monotonic()
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT pg_sleep(10)", "timeout_s": 1})
        assert data["error"] == {"code": "timeout", "message": (
            "Stopped after 1 s (timeout_s; at most 60, 30 on production connections)")}
        assert time.monotonic() - started < scaled(5)
        error, data = call_tool(port, "run_query", {"connection": red, "sql": "SELECT 1", "timeout_s": 60})
        assert not error and data["notes"] == ["timeout_s was lowered to 30 s: a production (red) connection"]
    assert [e.status for e in container.history.search(source="mcp").items] == ["ok", "timeout"]


@requires_pg
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_a_client_that_cancels_stops_the_statement(container, mode):
    """A4: a client gives up on a call and keeps its session, as Claude Code does on Esc. 2025-11-25 sends
    notifications/cancelled; 2026-07-28 closes the request's stream, which the SDK notices only with
    json_response=False (P18). Either way the statement stops on the server."""
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    watcher = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                              autocommit=True)

    def running() -> bool:
        # the frame's read-only transaction reads through a cursor: the backend shows its FETCH, waiting in PgSleep
        sleeping = watcher.execute("SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                                   "AND datname = 'dbaguy' AND state = 'active' AND wait_event = 'PgSleep'")
        return sleeping.fetchone()[0] > 0

    seen: list[bool] = []

    async def give_up(port: int) -> None:
        async with mcp_session(port, mode=mode) as session:
            async def call() -> None:
                with anyio.move_on_after(1.5):
                    await session.call_tool("run_query", {"connection": pid, "sql": "SELECT pg_sleep(29)"})

            async with anyio.create_task_group() as group:
                group.start_soon(call)
                seen.append(await anyio.to_thread.run_sync(lambda: wait_until(running, 1.4, interval=0.05)))
            seen.append(await anyio.to_thread.run_sync(lambda: wait_until(lambda: not running(), scaled(10))))

    try:
        with serving(container) as port:
            anyio.run(give_up, port)
        assert seen == [True, True]  # it was running when the client gave up, and it stopped while the session stayed
    finally:
        watcher.close()


@requires_pg
def test_postgres_explain_never_runs_the_statement(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM public.customers"})
        assert not error and "Seq Scan on customers" in data["plan"]
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT pg_sleep(30)"})
        assert not error and data["elapsed_ms"] < 5000
    assert container.history.search(source="mcp").items[0].sql == "EXPLAIN SELECT pg_sleep(30)"


@requires_mysql
def test_mysql_explain_is_a_tree(container):
    pid = share(container, make_mysql_profile(name="zz_08 mysql"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM customers WHERE id = 1"})
    assert not error and data["plan"].startswith("->")


def test_sqlite_explain_is_an_indented_tree(container, lite):
    pid = _lite(container, lite)
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM t WHERE id = 1"})
    assert not error and data["plan"].startswith("SEARCH t USING INTEGER PRIMARY KEY")


@requires_mssql
def test_sql_server_explain_is_showplan_and_showplan_ends_with_it(container):
    pid = share(container, make_mssql_profile(name="zz_08 mssql"))
    with serving(container) as port:
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM dbo.customers"})
        assert not error and "|--" in data["plan"]  # A8
        error, data = call_tool(port, "run_query", {"connection": pid, "sql": "SELECT 1 AS one, N'x' AS two"})
        assert not error and data["rows"] == [[1, "x"]]  # SHOWPLAN is off again: the statement ran
        assert all(column["type"] for column in data["columns"]), data["columns"]  # A13
    assert container.history.search(source="mcp").items[1].sql.startswith("-- SHOWPLAN_TEXT")


@requires_couchbase
def test_couchbase_query_explain_and_sample_fields(container):
    pid = share(container, make_couchbase_profile(name="zz_08 cb"))
    with serving(container) as port:
        error, data = call_tool(port, "run_query", {"connection": pid, "query_context": "dbaguy.app",
                                                    "sql": "SELECT RAW META(u).id FROM users AS u ORDER BY META(u).id"
                                                           " LIMIT 3"})
        assert not error and len(data["rows"]) == 3
        error, data = call_tool(port, "explain", {"connection": pid, "sql": "SELECT * FROM users"})  # A11
        assert not error and '"#operator"' in data["plan"] and isinstance(data["findings"], list)
        error, data = call_tool(port, "sample_fields", {"connection": pid, "bucket": "dbaguy", "scope": "app",
                                                        "collection": "users", "size": 20})
        assert not error and data["keyspace"] == "dbaguy.app.users" and data["fields"]
    sample = container.history.search(source="mcp").items[0]
    assert sample.sql.startswith("-- MCP sample_fields dbaguy.app.users (") and sample.sql.endswith(" documents)")


@requires_pg
def test_the_log_carries_no_sql_no_server_text_and_no_token(container):
    pid = share(container, make_pg_profile(name="zz_08 pg"))
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        with serving(container) as port:
            error, data = call_tool(port, "run_query", {"connection": pid,
                                                        "sql": "SELECT 'zz_08_secret' FROM zz_08_missing"})
            assert data["error"]["code"] == "query_failed" and "zz_08_missing" in data["error"]["message"]
            error, data = call_tool(port, "run_query", {"connection": pid, "sql": "DROP TABLE zz_08_refused"})
            assert data["error"]["code"] == "refused"
    finally:
        logger.remove(sink)
    text = "\n".join(lines)
    for secret in ("zz_08_secret", "zz_08_missing", "zz_08_refused", "does not exist", MCP_TOKEN):
        assert secret not in text, secret
    assert "MCP run_query on 'zz_08 pg' by pytest: 42P01" in text
    assert "MCP refused run_query on 'zz_08 pg' by pytest: not_allowed_start" in text
```

- [ ] **Step 2: Run them; they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_queries.py`
Expected: collection error, `ImportError: cannot import name 'mcp_session' from 'tests.agent_support'`. Step 3's patch adds the helper.

Without it, every test would fail. The tools are not registered yet, and the SDK's plain-text "Unknown tool" is not JSON.

- [ ] **Step 3: Write the code**

**New file `backend/dbaguy_backend/agent/queries.py`:**

```python
"""The tools that read rows (spec ⑧ §3.5, 'Read-only queries' access): run_query, explain and Couchbase's sample_fields.
Each call goes through the agent gate first (refused: History `refused`, nothing connects), then the reader (D-08-1,
D-08-3), and leaves one History record (`source=mcp`, the client's name, CR-08-3). INFO says which tool, connection,
client, outcome, rows and time; never the SQL, a value or the server's text (D-08-14)."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..adapters.couchbase.explain import analyze
from ..domain.couchbase import FieldSample, Keyspace
from ..domain.execution import QueryContext
from ..domain.history import HistoryRecord, HistoryStatus
from ..domain.profiles import AnyProfile, Engine, ServerProfile
from ..domain.settings import SAMPLE_SIZE_MAX
from ..domain.well_formed import well_formed
from ..services.agent_gate import SERVER_READ_ONLY, agent_read
from ..services.field_sample_service import FieldSampleService
from ..services.history_service import HistoryService
from ..services.settings_service import SettingsService
from ..sql.dialects import Dialect
from .reader import AgentReader, CallControl, ReadOutcome, ReadRequest
from .shape import MAX_CELL_DEFAULT, MAX_ROWS, MAX_ROWS_DEFAULT, RESPONSE_CHARS, compact
from .tools import AgentTools, Shared, ToolFailure, answered

TIMEOUT_DEFAULT_S, TIMEOUT_MAX_S, TIMEOUT_RED_S = 30, 60, 30  # CR-08-8: red connections: 30 at most
SQL_MAX = 20_000  # CR-08-3: also a History record's longest text
SAMPLE_MAX = 1_000
EXPLAIN_CELL_CHARS = 30_000  # one plan cell; the rest of the response is its keys and notes
MORE_ROWS = "More rows exist: raise max_rows (at most 1000) or narrow the query"
TOO_BIG = ("The response is limited to about 40,000 characters: select fewer columns, lower max_cell_chars or narrow "
           "the query")
READ_CAPPED = ("Stopped after reading about 64 Mi characters of values: select fewer or smaller columns, or narrow the "
               "query")
_LOWERED = "timeout_s was lowered to {} s: {}"
_TIMEOUT = "Stopped after {:g} s (timeout_s; at most 60, 30 on production connections)"
_READ_ONLY_NOTE = " (DbaGuy's MCP access is read-only)"
_NOT_COUCHBASE = "sample_fields reads Couchbase collections: '{}' is a {} connection"
_CONTEXT_ONLY_COUCHBASE = "query_context is for Couchbase connections; use `database` for '{}'"
_BAD_CONTEXT = "query_context is `bucket` or `bucket.scope`: {}"
_SHOWPLAN = "-- SHOWPLAN_TEXT (the statement was not run)\n"


def _database(profile: AnyProfile) -> str | None:
    return profile.database if isinstance(profile, ServerProfile) else None


class QueryTools:
    def __init__(self, tools: AgentTools, reader: AgentReader, history: HistoryService, samples: FieldSampleService,
                 settings: SettingsService) -> None:
        self._tools = tools
        self._reader = reader
        self._history = history
        self._samples = samples
        self._settings = settings

    # ---- run_query, explain ----

    def run_query(self, connection: str, sql: str, database: str | None = None, query_context: str | None = None,
                  max_rows: int = MAX_ROWS_DEFAULT, max_cell_chars: int = MAX_CELL_DEFAULT,
                  timeout_s: float | None = None, *, client: str | None = None,
                  control: CallControl | None = None) -> dict[str, Any]:
        shared = self._tools.shared(connection, "run_query", "query")
        request, notes = self._request(shared, "run_query", sql, database, query_context, timeout_s, client)
        request = replace(request, max_rows=max_rows, max_cell_chars=max_cell_chars)
        outcome = self._read("run_query", shared, request, sql, client, control)
        if outcome.read_capped:
            notes.append(READ_CAPPED)
        elif outcome.rows_dropped:
            notes.append(TOO_BIG)
        elif outcome.more_rows:
            notes.append(MORE_ROWS)
        return {
            "columns": outcome.columns, "rows": outcome.rows, "row_count": len(outcome.rows),
            "more_rows": outcome.more_rows,
            "truncated": {"rows_dropped_for_size": outcome.rows_dropped} if outcome.rows_dropped else None,
            "elapsed_ms": outcome.elapsed_ms, "notes": notes + outcome.notes,
        }

    def explain(self, connection: str, sql: str, database: str | None = None, query_context: str | None = None,
                timeout_s: float | None = None, *, client: str | None = None,
                control: CallControl | None = None) -> dict[str, Any]:
        """D-08-10: the plan of one read, without running it (no ANALYZE); Couchbase adds its findings."""
        shared = self._tools.shared(connection, "explain", "query")
        request, notes = self._request(shared, "explain", sql, database, query_context, timeout_s, client)
        request = replace(request, explain=True, max_rows=MAX_ROWS, max_cell_chars=EXPLAIN_CELL_CHARS)
        recorded = (_SHOWPLAN + sql) if shared.adapter.dialect == Dialect.TSQL else f"EXPLAIN {sql}"
        outcome = self._read("explain", shared, request, recorded, client, control)
        result: dict[str, Any] = {"plan": _plan_text(shared.adapter.dialect, outcome),
                                  "elapsed_ms": outcome.elapsed_ms, "notes": notes + outcome.notes}
        if shared.adapter.capabilities.plan_format == "couchbase":
            result["findings"] = _findings(outcome)
        if outcome.more_rows:
            result["notes"].append("The plan is longer than the response holds: it is cut")
        return result

    def _request(self, shared: Shared, tool: str, sql: str, database: str | None, query_context: str | None,
                 timeout_s: float | None, client: str | None) -> tuple[ReadRequest, list[str]]:
        """The gate (refused: History, INFO, `refused`), the timeout for this connection, the context."""
        profile, dialect = shared.profile, shared.adapter.dialect
        verdict = agent_read(sql, dialect)
        if not verdict.allowed:
            self._history.refused(
                self._record(profile, sql, database, None, 0, 0, "refused", verdict.message(), client))
            logger.info("MCP refused {} on '{}' by {}: {}", tool, profile.name, client, verdict.refusal)
            raise ToolFailure("refused", verdict.message(), logged=True)
        notes: list[str] = []
        limit = TIMEOUT_RED_S if profile.color == "red" else TIMEOUT_MAX_S
        timeout = float(timeout_s if timeout_s is not None else TIMEOUT_DEFAULT_S)
        if timeout > limit:
            why = "a production (red) connection" if profile.color == "red" else "the most a call may take"
            notes.append(_LOWERED.format(limit, why))
            timeout = limit
        context = self._context(shared, query_context)
        return ReadRequest(profile=profile, dialect=dialect, sql=sql, database=database, query_context=context,
                           timeout_s=timeout), notes

    @staticmethod
    def _context(shared: Shared, text: str | None) -> QueryContext | None:
        if text is None:
            return None
        if not shared.adapter.capabilities.query_context:
            raise ToolFailure("invalid_argument", _CONTEXT_ONLY_COUCHBASE.format(shared.profile.name))
        try:
            return QueryContext.parse(text)
        except ValueError as exc:
            raise ToolFailure("invalid_argument", _BAD_CONTEXT.format(exc)) from None

    def _read(self, tool: str, shared: Shared, request: ReadRequest, recorded_sql: str, client: str | None,
              control: CallControl | None) -> ReadOutcome:
        """The reader's outcome, its History record and log line; an outcome that is not `ok` raises its §4 error."""
        profile = shared.profile
        shown = request.query_context.text() if request.query_context else request.database or _database(profile)
        started = datetime.now(UTC)
        with self._recorded(profile, recorded_sql, shown, started, client):
            with answered(profile):
                outcome = self._reader.read(request, control)
        status: HistoryStatus = "ok" if outcome.status == "ok" else outcome.status
        error = outcome.error_message if outcome.status == "error" else None
        self._history.record(self._record(profile, recorded_sql, shown, started, outcome.elapsed_ms, len(outcome.rows),
                                          status, error, client))
        logger.info("MCP {} on '{}' by {}: {}, {} rows, {} ms", tool, profile.name, client,
                    outcome.error_code or outcome.status, len(outcome.rows), outcome.elapsed_ms)
        if outcome.status == "ok":
            return outcome
        raise self._failure(shared, request, outcome)

    @contextmanager
    def _recorded(self, profile: AnyProfile, sql: str, shown: str | None, started: datetime,
                  client: str | None) -> Iterator[None]:
        """A call that never reached the statement (a password, a session that cannot open, busy) is recorded too."""
        try:
            yield
        except ToolFailure as failure:
            self._history.record(self._record(profile, sql, shown, started, 0, 0, "error", failure.message, client))
            raise

    @staticmethod
    def _failure(shared: Shared, request: ReadRequest, outcome: ReadOutcome) -> ToolFailure:
        if outcome.status == "timeout":
            return ToolFailure("timeout", _TIMEOUT.format(request.timeout_s), logged=True)
        if outcome.status == "cancelled":
            return ToolFailure("cancelled", "Cancelled", logged=True)
        code, message = outcome.error_code, outcome.error_message or "The query failed"
        if code == "read_only":  # the worker's backstop: the gate's verdict, again
            return ToolFailure("refused", message, logged=True)
        if code == "session_changed":
            return ToolFailure("session_changed", message, logged=True)
        if code in SERVER_READ_ONLY.get(shared.adapter.dialect, ()):
            message += _READ_ONLY_NOTE
        if code:
            message += f" ({code})"
        return ToolFailure("query_failed", message, logged=True)

    @staticmethod
    def _record(profile: AnyProfile, sql: str, database: str | None, started: datetime | None, elapsed_ms: int,
                rows: int, status: HistoryStatus, error: str | None, client: str | None) -> HistoryRecord:
        # well_formed: an agent's text can hold a lone surrogate (JSON's "\ud800"), which the history file cannot store
        return HistoryRecord(
            profile_id=profile.id, profile_name=profile.name, database=database, sql=well_formed(sql),
            started_at=started or datetime.now(UTC), elapsed_ms=elapsed_ms, rows=rows, status=status, error=error,
            source="mcp", client=client,
        )

    # ---- sample_fields ----

    def sample_fields(self, connection: str, bucket: str, scope: str, collection: str, size: int | None = None, *,
                      client: str | None = None) -> dict[str, Any]:
        """A Couchbase collection's field paths from a sample of its documents (④ R-04-11): the one already taken
        when there is one and no size is asked for, else a new sample on the agents' catalog connection."""
        shared = self._tools.shared(connection, "sample_fields", "query")
        profile = shared.profile
        if profile.engine != Engine.COUCHBASE:
            raise ToolFailure("not_allowed", _NOT_COUCHBASE.format(profile.name, profile.engine))
        keyspace = Keyspace(bucket=bucket, scope=scope, collection=collection)
        shown = ".".join((bucket, scope, collection))
        started = datetime.now(UTC)
        wanted = min(size or self._settings.get().couchbase.sample_size, SAMPLE_MAX, SAMPLE_SIZE_MAX)
        summary = f"-- MCP sample_fields {shown}"
        with self._recorded(profile, summary, None, started, client):
            with answered(profile):
                sample = (self._samples.cached(profile.id, keyspace) if size is None else None) or \
                    self._samples.sample(profile.id, keyspace, wanted, agent=True)
        self._history.record(self._record(profile, f"{summary} ({sample.documents} documents)", None, started,
                                          sample.elapsed_ms, sample.documents, "ok", None, client))
        logger.info("MCP sample_fields on '{}' by {}: {} documents, {} paths", profile.name, client, sample.documents,
                    len(sample.fields))
        return _sample_result(sample)


def _sample_result(sample: FieldSample) -> dict[str, Any]:
    result: dict[str, Any] = {
        "keyspace": ".".join((sample.keyspace.bucket, sample.keyspace.scope, sample.keyspace.collection)),
        "documents": sample.documents, "fields": [], "paths_capped": sample.paths_capped,
        "depth_capped": sample.depth_capped, "long_keys_skipped": sample.long_keys_skipped,
        "cut_at_bytes": sample.cut_at_bytes,
    }
    room = RESPONSE_CHARS - len(compact(result)) - 200
    for path in sample.fields:
        entry = {"path": path.display, "types": path.types, "docs": path.docs}
        size = len(compact(entry)) + 1
        if size > room:
            result["notes"] = [f"{len(sample.fields) - len(result['fields'])} more paths left out (response size)"]
            break
        result["fields"].append(entry)
        room -= size
    return result


def _plan_text(dialect: Dialect, outcome: ReadOutcome) -> str:
    """The plan's rows as text: their first column, a line each (PostgreSQL's QUERY PLAN, MySQL's tree, SQL Server's
    StmtText, Couchbase's JSON); SQLite's EXPLAIN QUERY PLAN rows (id, parent, notused, detail) as an indented tree."""
    if dialect == Dialect.SQLITE:
        depth: dict[Any, int] = {}
        lines = []
        for row in outcome.rows:
            node, parent, detail = row[0], row[1], row[-1]
            depth[node] = depth.get(parent, -1) + 1
            lines.append("  " * depth[node] + str(detail))
        return "\n".join(lines)
    return "\n".join(_cell_text(row[0]) for row in outcome.rows if row)


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") if cell.get("$type") == "json" else cell.get("preview", ""))
    return "" if cell is None else str(cell)


def _findings(outcome: ReadOutcome) -> list[str]:
    """Couchbase's plan findings (④ explain.py: primary scans, uncovered index scans…); none for a cut plan."""
    if not outcome.rows or not isinstance(outcome.rows[0][0], dict) or outcome.rows[0][0].get("$type") != "json":
        return []
    return [finding.text for finding in analyze(outcome.rows[0][0]["text"]).findings]

```

**Patch T8** (the files this task modifies: `backend/dbaguy_backend/agent/host.py`, `backend/dbaguy_backend/agent/server.py`, `backend/dbaguy_backend/container.py`, `backend/dbaguy_backend/services/agent_gate.py`, `backend/dbaguy_backend/services/field_sample_service.py`, `backend/tests/agent_support.py`). Save the block in your scratchpad as `08a-t8.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t8.diff` from the repository root:

```diff
diff --git a/backend/dbaguy_backend/agent/host.py b/backend/dbaguy_backend/agent/host.py
index 214ae19..54e9f25 100644
--- a/backend/dbaguy_backend/agent/host.py
+++ b/backend/dbaguy_backend/agent/host.py
@@ -17,6 +17,7 @@ from ..domain.agent import McpState, McpStatus, McpToken
 from ..domain.settings import McpSettings
 from ..listen import listen_socket
 from ..tokens import new_token
+from .queries import QueryTools
 from .token_store import TokenStore
 from .tools import AgentTools
 
@@ -62,9 +63,10 @@ class _Run:
 
 
 class AgentHost:
-    def __init__(self, tools: AgentTools, tokens: TokenStore, default_port: int, *,
+    def __init__(self, tools: AgentTools, queries: QueryTools, tokens: TokenStore, default_port: int, *,
                  on_stop: Callable[[], None] = lambda: None) -> None:
         self._tools = tools
+        self._queries = queries
         self._tokens = tokens
         self._default_port = default_port
         self._on_stop = on_stop  # closes the agent sessions and catalog connections
@@ -207,7 +209,7 @@ class AgentHost:
             self._set("error", run.port, f"Could not start: {type(exc).__name__}", run)
             return
         try:
-            app = mcp_server.build_app(self._tools, port=run.port, token=self.current_token)
+            app = mcp_server.build_app(self._tools, self._queries, port=run.port, token=self.current_token)
             config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on",
                                     timeout_graceful_shutdown=GRACEFUL_S)
             server = _Uvicorn(config, on_started=lambda: self._listening(run))
diff --git a/backend/dbaguy_backend/agent/server.py b/backend/dbaguy_backend/agent/server.py
index 7d31b35..0641cb7 100644
--- a/backend/dbaguy_backend/agent/server.py
+++ b/backend/dbaguy_backend/agent/server.py
@@ -4,6 +4,7 @@ gate in front of it. The only module that imports the MCP SDK, and only `AgentHo
 
 import json
 import re
+import threading
 import time
 from collections.abc import Callable
 from typing import Annotated, Any
@@ -22,6 +23,9 @@ from ..domain.metadata import PathSegment
 from ..domain.well_formed import well_formed
 from ..tokens import same_token
 from ..version import app_version
+from .queries import SAMPLE_MAX, SQL_MAX, TIMEOUT_DEFAULT_S, TIMEOUT_MAX_S, QueryTools
+from .reader import CallControl
+from .shape import MAX_CELL, MAX_CELL_DEFAULT, MAX_ROWS, MAX_ROWS_DEFAULT
 from .tools import LIMIT_MAX, LIST_LIMIT, SEARCH_LIMIT, AgentTools, ToolFailure
 
 MCP_PATH = "/mcp"
@@ -43,6 +47,10 @@ Connection = Annotated[str, Field(min_length=1, max_length=200, description="A c
 ObjectPath = Annotated[list[PathSegment], Field(max_length=12, description="A `path` list_objects or search_objects "
                                                                            "returned, passed back as it is")]
 Name = Annotated[str, Field(min_length=1, max_length=1000)]
+Sql = Annotated[str, Field(min_length=1, max_length=SQL_MAX, description="One statement")]
+QueryContextText = Annotated[str, Field(min_length=1, max_length=400, description="Couchbase: bucket or bucket.scope")]
+Timeout = Annotated[float, Field(gt=0, le=TIMEOUT_MAX_S, description=f"Seconds, default {TIMEOUT_DEFAULT_S}; "
+                                                                        "at most 30 on production (red) connections")]
 
 
 class BearerGate:
@@ -97,12 +105,19 @@ def failure_result(failure: ToolFailure) -> CallToolResult:
     return _text({"error": {"code": failure.code, "message": failure.message}}, error=True)
 
 
-async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, Any]]) -> CallToolResult:
+async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, Any]],
+                   control: CallControl | None = None) -> CallToolResult:
     """`call` on a worker thread; its data or its failure as a result, never an exception the SDK would log with its
-    message (D-08-6). An unexpected error is logged by its type only above DEBUG."""
+    message (D-08-6). An unexpected error is logged by its type only above DEBUG. The client cancelling the request
+    (A4) cancels the call's statement through `control`, on a thread of its own: a cancel can wait on the server."""
     started = time.monotonic()
     try:
-        return _text(await anyio.to_thread.run_sync(call))
+        return _text(await anyio.to_thread.run_sync(call, abandon_on_cancel=True))
+    except anyio.get_cancelled_exc_class():
+        if control is not None:
+            threading.Thread(target=control.cancel, name=f"mcp-cancel-{tool}", daemon=True).start()
+        logger.info("MCP {} by {}: cancelled by the client", tool, client)
+        raise
     except ToolFailure as failure:
         if not failure.logged:
             elapsed = int((time.monotonic() - started) * 1000)
@@ -114,7 +129,7 @@ async def run_tool(tool: str, client: str | None, call: Callable[[], dict[str, A
         return failure_result(ToolFailure("internal_error", "Internal error"))
 
 
-def build_server(tools: AgentTools) -> MCPServer:
+def build_server(tools: AgentTools, queries: QueryTools) -> MCPServer:
     """A new server each start: the SDK's session manager runs once per instance (D-08-8)."""
     server = MCPServer("dbaguy", title="DbaGuy", instructions=INSTRUCTIONS, version=app_version())
 
@@ -181,10 +196,65 @@ def build_server(tools: AgentTools) -> MCPServer:
             "indexes, comment; its DDL with include_ddl. Reads no rows."
         ),
     )
+    async def run_query(
+        connection: Connection, sql: Sql, ctx: Context, database: Name | None = None,
+        query_context: QueryContextText | None = None,
+        max_rows: Annotated[int, Field(ge=1, le=MAX_ROWS)] = MAX_ROWS_DEFAULT,
+        max_cell_chars: Annotated[int, Field(ge=1, le=MAX_CELL)] = MAX_CELL_DEFAULT, timeout_s: Timeout | None = None,
+    ) -> CallToolResult:
+        client, control = client_name(ctx), CallControl()
+        return await run_tool("run_query", client, lambda: queries.run_query(
+            connection, sql, database, query_context, max_rows, max_cell_chars, timeout_s, client=client,
+            control=control), control)
+
+    server.add_tool(
+        run_query, name="run_query", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'Read-only queries' access. Runs exactly one SELECT, WITH, VALUES, TABLE or SHOW "
+            "statement (SQL Server and Couchbase: SELECT or WITH) in a read-only transaction that is rolled back. "
+            "Returns columns, rows (big integers as strings; long text cut at max_cell_chars, with its size), "
+            "row_count, more_rows, and notes. At most max_rows rows (100 by default, 1000 at most) and about 40,000 "
+            "characters; timeout_s 30 s by default, 60 at most (30 on production connections)."
+        ),
+    )
+
+    async def explain(
+        connection: Connection, sql: Sql, ctx: Context, database: Name | None = None,
+        query_context: QueryContextText | None = None, timeout_s: Timeout | None = None,
+    ) -> CallToolResult:
+        client, control = client_name(ctx), CallControl()
+        return await run_tool("explain", client, lambda: queries.explain(
+            connection, sql, database, query_context, timeout_s, client=client, control=control), control)
+
+    server.add_tool(
+        explain, name="explain", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'Read-only queries' access. The plan of one read statement (the statement run_query "
+            "would take), without running it: no ANALYZE. Pass the statement without EXPLAIN; DbaGuy adds it "
+            "(SQL Server: SHOWPLAN_TEXT). Couchbase also lists findings such as primary or uncovered scans."
+        ),
+    )
+
+    async def sample_fields(
+        connection: Connection, bucket: Name, scope: Name, collection: Name, ctx: Context,
+        size: Annotated[int, Field(ge=1, le=SAMPLE_MAX)] | None = None,
+    ) -> CallToolResult:
+        client = client_name(ctx)
+        return await run_tool("sample_fields", client, lambda: queries.sample_fields(
+            connection, bucket, scope, collection, size, client=client))
+
+    server.add_tool(
+        sample_fields, name="sample_fields", annotations=_READ_ONLY, structured_output=False,
+        description=(
+            "Read-only. Needs 'Read-only queries' access; Couchbase only. The field paths of a collection's "
+            "documents, from a sample (the last one DbaGuy took, or a new one of `size` documents, at most 1000): "
+            "each path with its JSON types and how many documents have it."
+        ),
+    )
     return server
 
 
-def build_app(tools: AgentTools, *, port: int, token: Callable[[], str]) -> ASGIApp:
+def build_app(tools: AgentTools, queries: QueryTools, *, port: int, token: Callable[[], str]) -> ASGIApp:
     """The ASGI app the MCP thread serves on 127.0.0.1:`port`."""
     security = TransportSecuritySettings(  # the SDK checks again; BearerGate has refused anything else already
         allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"], allowed_origins=[],
@@ -192,7 +262,7 @@ def build_app(tools: AgentTools, *, port: int, token: Callable[[], str]) -> ASGI
     # json_response=False (P18): only the SDK's SSE path notices a 2026-07-28 client closing its request's stream,
     # which is how that client cancels a call (A4). A call that ends within the SDK's 15 s still answers JSON there;
     # 2025-11-25 answers as an SSE stream and cancels with notifications/cancelled.
-    app = build_server(tools).streamable_http_app(
+    app = build_server(tools, queries).streamable_http_app(
         streamable_http_path=MCP_PATH, json_response=False, stateless_http=False, max_sessions=MAX_SESSIONS,
         session_idle_timeout=SESSION_IDLE_S, max_request_body_size=MAX_BODY_BYTES, transport_security=security,
     )
diff --git a/backend/dbaguy_backend/container.py b/backend/dbaguy_backend/container.py
index eb2073e..5e94c41 100644
--- a/backend/dbaguy_backend/container.py
+++ b/backend/dbaguy_backend/container.py
@@ -13,6 +13,7 @@ from .adapters.registry import AdapterRegistry
 from .adapters.sqlite.adapter import SqliteAdapter
 from .agent.host import AgentHost
 from .agent.login import LoginRegistry
+from .agent.queries import QueryTools
 from .agent.reader import AgentReader
 from .agent.sessions import AgentSessions
 from .agent.token_store import InMemoryTokenStore, KeyringTokenStore, TokenStore
@@ -123,9 +124,11 @@ class AppContainer:
         self.agent_sessions = AgentSessions(self.sessions, self.agent_login)
         self.agent_reader = AgentReader(self.agent_sessions)
         self.agent_tools = AgentTools(self.profiles, self.registry, self.metadata, self.schema_cache, self.agent_login)
+        self.agent_queries = QueryTools(self.agent_tools, self.agent_reader, self.history, self.field_samples,
+                                        self.app_settings)
         self.agent_host = AgentHost(
-            self.agent_tools, token_store or _token_store(settings, secret_store), default_mcp_port(data_dir),
-            on_stop=self._close_agents,
+            self.agent_tools, self.agent_queries, token_store or _token_store(settings, secret_store),
+            default_mcp_port(data_dir), on_stop=self._close_agents,
         )
         self._stop = threading.Event()
         self._housekeeper: threading.Thread | None = None
diff --git a/backend/dbaguy_backend/services/agent_gate.py b/backend/dbaguy_backend/services/agent_gate.py
index 75c96ca..46540c4 100644
--- a/backend/dbaguy_backend/services/agent_gate.py
+++ b/backend/dbaguy_backend/services/agent_gate.py
@@ -58,6 +58,13 @@ DENIED_FUNCTIONS: dict[Dialect, tuple[tuple[str, str], ...]] = {
     Dialect.SQLPP: (("curl", "reaches the network"), ("advisor", "starts index advisor sessions")),
 }
 _QUOTED_NAMES_KEEP_CASE = frozenset({Dialect.POSTGRES})
+# The codes a server answers a write with on an agent's read-only session (§3.4: P4, M2, S1/S9, C1): the tool adds that
+# DbaGuy's MCP access is read-only. SQL Server has none: it is DbaGuy's checks and a rollback there.
+SERVER_READ_ONLY: dict[Dialect, frozenset[str]] = {
+    Dialect.POSTGRES: frozenset({"25006"}), Dialect.MYSQL: frozenset({"1792"}),
+    Dialect.SQLITE: frozenset({"SQLITE_READONLY", "SQLITE_AUTH"}), Dialect.SQLPP: frozenset({"1000"}),
+    Dialect.TSQL: frozenset(),
+}
 _HEX = re.compile(r"[0-9A-Fa-f]+")
 
 
diff --git a/backend/dbaguy_backend/services/field_sample_service.py b/backend/dbaguy_backend/services/field_sample_service.py
index 9ed3f0d..156d72e 100644
--- a/backend/dbaguy_backend/services/field_sample_service.py
+++ b/backend/dbaguy_backend/services/field_sample_service.py
@@ -29,10 +29,11 @@ class FieldSampleService:
         self._samples: dict[UUID, OrderedDict[Keyspace, FieldSample]] = {}
         self._lock = threading.Lock()
 
-    def sample(self, profile_id: UUID, keyspace: Keyspace, size: int | None) -> FieldSample:
+    def sample(self, profile_id: UUID, keyspace: Keyspace, size: int | None, *, agent: bool = False) -> FieldSample:
         """Read a sample of `keyspace` (`size` documents; None: the setting's `couchbase.sample_size`) and keep it as the
         keyspace's current one. A failed sample keeps the previous one. The server's error answer (a missing keyspace, a
-        privilege) is raised as it is, and not sent a second time on a fresh connection (B39)."""
+        privilege) is raised as it is, and not sent a second time on a fresh connection (B39). `agent`: on the MCP
+        agents' read-only metadata connection (spec ⑧ D-08-11); the kept sample is the same one the app shows."""
         requested = size if size is not None else self._settings.get().couchbase.sample_size
 
         def run(connection: DatabaseConnection) -> FieldSample:
@@ -41,7 +42,10 @@ class FieldSampleService:
             with server_errors_are_final():
                 return connection.sample_fields(keyspace, requested)
 
-        sample = self._metadata.with_connection(profile_id, None, run)
+        if agent:
+            sample = self._metadata.with_connection(profile_id, None, run, agent=True)
+        else:
+            sample = self._metadata.with_connection(profile_id, None, run)
         with self._lock:
             kept = self._samples.setdefault(profile_id, OrderedDict())
             kept[keyspace] = sample
diff --git a/backend/tests/agent_support.py b/backend/tests/agent_support.py
index b8d70eb..03d5017 100644
--- a/backend/tests/agent_support.py
+++ b/backend/tests/agent_support.py
@@ -5,8 +5,8 @@ import json
 import random
 import socket
 import time
-from collections.abc import Iterator
-from contextlib import contextmanager
+from collections.abc import AsyncIterator, Iterator
+from contextlib import asynccontextmanager, contextmanager
 from pathlib import Path
 from typing import Any
 
@@ -78,8 +78,10 @@ def serving(container: AppContainer) -> Iterator[int]:
         container.agent_host.stop()
 
 
-async def _call(port: int, token: str, mode: str, tool: str | None, arguments: dict[str, Any],
-                client: str) -> Any:
+@asynccontextmanager
+async def mcp_session(port: int, *, token: str = MCP_TOKEN, mode: str = "legacy",
+                      client: str = "pytest") -> AsyncIterator[Any]:
+    """An MCP client session against 127.0.0.1:`port`, open for the block."""
     from mcp import Client
     from mcp.client.streamable_http import streamable_http_client
     from mcp.shared._httpx_utils import create_mcp_http_client
@@ -88,17 +90,25 @@ async def _call(port: int, token: str, mode: str, tool: str | None, arguments: d
     async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
         transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http)
         async with Client(transport, mode=mode, client_info=Implementation(name=client, version="1")) as session:
-            if tool is None:
-                return await session.list_tools()
-            return await session.call_tool(tool, arguments)
+            yield session
+
+
+async def _call(port: int, token: str, mode: str, tool: str | None, arguments: dict[str, Any],
+                client: str) -> Any:
+    async with mcp_session(port, token=token, mode=mode, client=client) as session:
+        if tool is None:
+            return await session.list_tools()
+        return await session.call_tool(tool, arguments)
 
 
 def call_tool(port: int, tool: str, arguments: dict[str, Any] | None = None, *, mode: str = "legacy",
               token: str = MCP_TOKEN, client: str = "pytest") -> tuple[bool, Any]:
-    """(is_error, the JSON of its one text block): one tool call through a new MCP client session."""
+    """(is_error, the JSON of its one text block): one tool call through a new MCP client session. The SDK's own
+    refusal of an argument is plain text, returned as it is."""
     result = anyio.run(_call, port, token, mode, tool, arguments or {}, client)
     assert len(result.content) == 1 and result.content[0].type == "text"
-    return result.is_error, json.loads(result.content[0].text)
+    text = result.content[0].text
+    return result.is_error, json.loads(text) if text.startswith("{") else text
 
 
 def list_tools(port: int, *, mode: str = "legacy") -> list[Any]:
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_mcp_queries.py tests/test_mcp_host.py tests/test_mcp_reader.py tests/test_mcp_shape.py tests/test_couchbase_sessions.py tests/test_couchbase_api.py tests/test_history.py`
Expected: all pass (planning: 91 passed in 24 s). `gen:api` makes no diff, and `tsc` is clean.

- [ ] **Step 5: Measure (record for Task 9)**

Run the Appendix's `types.py` from `backend/`.
Expected (planning, a median of 20 calls each, session open): PG 4.4 ms, MySQL 2.8 ms, SQL Server 6.1 ms, Couchbase 1.3 ms (§7: ≤ 100 ms). The types line shows A13's coarse SQL Server types.

- [ ] **Step 6: Review against the pain points** (§3 M11/M12/M13; Q3: every attempt in History, nothing in the log; Q1: the budgets hold through MCP)

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/agent/queries.py backend/dbaguy_backend/agent/server.py \
  backend/dbaguy_backend/agent/host.py backend/dbaguy_backend/services/agent_gate.py \
  backend/dbaguy_backend/services/field_sample_service.py backend/dbaguy_backend/container.py \
  backend/tests/agent_support.py backend/tests/test_mcp_queries.py
git commit -m "feat(08a): run_query, explain and sample_fields over MCP, with their History, timeouts and the client's cancel

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

### Task 9: Closing 08a — packaging, the frozen sidecar over MCP, Claude Code, measurements, the documents

**Suggested implementer:** Sonnet, with Opus reviewing the documents. It runs the probes, the packaging and the documents, and adds no new logic.

**Files:**
- Modify:
  - `backend/dbaguy-backend.spec`
  - `CLAUDE.md`
  - `docs/superpowers/specs/2026-10-25-08-mcp-server-design.md` (its 〔08a〕 notes)
- Create: `docs/superpowers/notes/2026-10-2x-08a-mcp-server-backend-followups.md`, dated the day it is written.

**Interfaces:** none new.

- [ ] **Step 1: The spec file**

**Patch T9** (the files this task modifies: `backend/dbaguy-backend.spec`). Save the block in your scratchpad as `08a-t9.diff`, never inside the repository, then run `git apply <scratchpad>/08a-t9.diff` from the repository root:

```diff
diff --git a/backend/dbaguy-backend.spec b/backend/dbaguy-backend.spec
index 0a0f2b1..47463e8 100644
--- a/backend/dbaguy-backend.spec
+++ b/backend/dbaguy-backend.spec
@@ -12,6 +12,11 @@ _MYSQL_LEFT_OUT = (
     "mysql.connector.plugins.authentication_webauthn_client",
 )
 
+# The MCP SDK (spec ⑧): agent/server.py imports it only once the MCP switch is on, which the analysis still follows;
+# what it loads by name (mcp_types' protocol versions, the server's transports) is collected whole. Not its CLI or the
+# v1 FastMCP stub, which raises ModuleNotFoundError on import by design.
+_MCP_LEFT_OUT = ("mcp.cli", "mcp.server.fastmcp")
+
 hiddenimports = [
     *collect_submodules("uvicorn"),
     *collect_submodules("dbaguy_backend"),
@@ -42,13 +47,15 @@ hiddenimports = [
     "pymssql._pymssql",
     # Keeps the locales (loaded with importlib) and the native/caching_sha2/sha256/clear-password auth plugins.
     *collect_submodules("mysql.connector", filter=lambda name: not name.startswith(_MYSQL_LEFT_OUT)),
+    *collect_submodules("mcp", filter=lambda name: not name.startswith(_MCP_LEFT_OUT)),
+    *collect_submodules("mcp_types"),
 ]
 
 a = Analysis(
     ["entry.py"],
     pathex=["."],
     hiddenimports=hiddenimports,
-    excludes=["tkinter", "pytest", "PyInstaller", "_mysql_connector", *_MYSQL_LEFT_OUT],
+    excludes=["tkinter", "pytest", "PyInstaller", "_mysql_connector", *_MYSQL_LEFT_OUT, *_MCP_LEFT_OUT],
     noarchive=False,
 )
 pyz = PYZ(a.pure)
```

(`collect_submodules("mcp")` leaves out `mcp.cli`, which needs `typer`, and `mcp.server.fastmcp`, which raises `ModuleNotFoundError` on import by design. hooks-contrib's `cryptography` and `jsonschema_specifications` hooks collect the rest. Planning's build had no new warning; the only one was the old `psycopg_binary` collect warning.)

- [ ] **Step 2: Build the sidecar and the app; check the bundle**

```bash
df -h /   # at least 2 GiB free
npm run build:app
du -sk src-tauri/resources/backend
find src-tauri/resources/backend -name '_rust.abi3.so' -path '*cryptography*'
codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app
```

Expected:
- The sidecar is about 63,150 KB. Planning measured 63,156 KB for PyInstaller's `dist/dbaguy-backend`, against 06b's 49,552 KB: +13.6 MB, as §7 estimates. 06c changed nothing in the sidecar's dependencies; take its own number from `docs/superpowers/notes/2026-10-25-06c-*` and record the difference from it.
- `cryptography`'s `_rust.abi3.so`, `rpds`'s `.so` and `_cffi_backend` are in `_internal`.
- Every `.so` and `.dylib` passes `codesign --verify --strict` (planning: 95 of 95).
- The app verifies.

- [ ] **Step 3: The frozen sidecar over MCP**

Copy the Appendix's `frozen_mcp_probe.py` into your scratchpad and run it three times with the dev venv's Python:

```bash
backend/.venv/bin/python <scratch>/frozen_mcp_probe.py src-tauri/resources/backend/dbaguy-backend
```

Expected (planning, against the same build in PyInstaller's `dist/`):
- Ready 0.50–0.67 s. The run right after a build took 14.5–15 s, which is macOS's first look at a new binary.
- From the switch turning on to listening: 0.26–0.32 s (§7: ≤ 1 s); 0.64–0.86 s in the first run after a build.
- RSS +22 MB when it turns on (§7: ≤ 30 MB). From 103 MB with the switch off to 125–127 MB listening, then 162–168 MB after calls on five engines.
- `401/421/403: (401, 421, 403)`.
- Every tool call `ok`: on each engine `list_objects`, `search_objects`, `describe`, `run_query` and `explain`, then a refused `DROP TABLE`, then Couchbase's `sample_fields`.
- No `ModuleNotFoundError` and no `Traceback` in stderr.

The probe uses port 27531, a temporary data dir and `DBAGUY_MCP_TOKEN`, so the Keychain is never touched. It stops only its own process.

- [ ] **Step 4: Claude Code itself (A1, A2, A6), only with the coordinator's go-ahead (it spends a few cents of model use)**

With the probe's sidecar running (or `scripts/dev.sh --web` with the switch on, in `dev/data`, port 17532), write a temporary config, **never** `~/.claude.json`:

```bash
cat > <scratch>/mcp.json <<'JSON'
{"mcpServers": {"dbaguy": {"type": "http", "url": "http://127.0.0.1:<port>/mcp",
  "headers": {"Authorization": "Bearer <the probe's or dev's MCP token>"}}}}
JSON
claude -p "Call the dbaguy list_connections tool, then run_query 'SELECT 1 AS one' on the first connection, and print both raw results." \
  --mcp-config <scratch>/mcp.json --strict-mcp-config \
  --allowedTools mcp__dbaguy__list_connections mcp__dbaguy__run_query --output-format json --max-budget-usd 0.25
```

Record:
- A1: whether it connected, which protocol version it used (the backend's DEBUG log), and the client name now in History (`GET /history?source=mcp`).
- A2: with a wrong token, what Claude Code does after the 401. Does it try OAuth discovery (requests for `/.well-known/…`, each refused with 401 by `BearerGate`)?
- A6: whether a `run_query` answer near 40,000 characters drew Claude Code's 10,000-token warning.

If the go-ahead is not given, write "A1/A2/A6: not checked with Claude Code; the SDK client covers both protocols". The test list carries them to the user.

- [ ] **Step 5: `scripts/measure-startup.sh` with the switch off**

Run it 4 times. The first run is the first launch after the build. Record:
- cold start
- the processes' RSS and `footprint`
- the backend's RSS and `footprint`

Expected: no change from 06c, since with the switch off nothing of the SDK loads (Task 1's `sys.modules` test), and every difference is within run-to-run noise.

- [ ] **Step 6: The full backend suite, once**

Dev containers up. First check with the Appendix's `leftovers.py` that no `zz_` scratch object exists. No other runner may be running.

```bash
cd backend && .venv/bin/pytest -q
npm run typecheck && npm test && npm run build
```

Expected: everything passes. Planning ran the suite at Task 9's tree with `-x`; the Dry-run record has its count and time. Then run `leftovers.py` again: nothing `zz_08`, `app.orders` 500, no advisor session.

- [ ] **Step 7: CLAUDE.md**

Update CLAUDE.md as spec §13 lists. Keep the file's style: Chinese prose and exact names. Cover:
- **Repo structure**:
  - `agent/` and each module's role, as in this plan's File Structure;
  - `domain/agent.py`, `api/mcp.py`, `services/agent_gate.py`, `adapters/mysql/read_only.py`;
  - `tokens.py` and `listen.py`;
  - the History fields.
- **技術選型**: the `mcp` SDK (2.3, lazily imported), and why (R-08-1).
- **架構重點**: a new "MCP server（⑧）" block covering:
  - the port: 17531, or 17532 outside the app's data dir;
  - the token: Keychain account `mcp:token`, or `DBAGUY_MCP_TOKEN`;
  - `BearerGate`: 401/421/403;
  - the SDK's transport: `json_response=False`, because only its SSE path notices a 2026-07-28 client's cancel (P18);
  - access levels: off/schema/query, read every call;
  - the agents' catalog connection;
  - login privileges (PG roles, MySQL `SHOW GRANTS`, SQL Server's `_LOGIN_SQL`);
  - the read-only layers per engine (§3.4's table in prose), the frame and its end, and closing a session that cannot end cleanly;
  - the gate's order, its allow-list and its deny-list, `U&` decoding, MySQL INTO and executable comments;
  - pages and the memory bound (first page 1 row, 8 Mi, 64 Mi read cap, 40,000 characters);
  - the deadline (cancel, force after 1.5 s, close after 10 s), and the client's cancel;
  - History: source, client, repeats, the 5,000 cap, merging refusals;
  - MySQL's `ReadOnlyGuard` for tabs and agents, which skips diagnostic statements;
  - logging.
- **開發指令**:
  - dev's MCP port 17532;
  - the `claude -p … --mcp-config <temp> --strict-mcp-config` one-off check;
  - "never `~/.claude.json`".
- **已知限制**:
  - SQL Server is read-only only by the gate and the rollback, and sequences advance (T3);
  - the token sits in clear in `~/.claude.json`;
  - while DbaGuy is closed, another program can listen on the fixed port;
  - the read locks of a call: up to 60 s, 30 on red connections;
  - one huge value is read whole (two rows held: RSS +290 MB at 100 MB values);
  - other sessions of the same login (`pg_terminate_backend` inside a user function);
  - one more round trip per statement on read-only MySQL sessions;
  - SQL Server's coarse column types, and SQLite's null ones;
  - Claude Code's checks (A1/A2/A6), if not done.
- **打包版量測**: an 08a row with Steps 2, 3 and 5.
- **進度**: ⑧ is in progress, 08a done and 08b not started, linking this plan and the follow-ups note.

- [ ] **Step 8: The spec's 〔08a〕 notes**

In `docs/superpowers/specs/2026-10-25-08-mcp-server-design.md`, add 〔08a〕 notes where the plan settled or changed something:
- every P ruling, at the decision it changes (P1 at §3.3's SQL Server row, P2 at the MySQL row, P3 at D-08-1/D-08-13, P5 at D-08-2, P18 at D-08-9, P7 at §3.8, P10 at §3.6, P12 at §3.5's flow);
- §10's A1–A15 outcomes: A1/A2/A6 from Step 4 (or "not checked"), A3 by `test_starting_and_stopping_again_and_again_leaves_no_thread_and_frees_the_port`, A4 by `test_a_client_that_cancels_stops_the_statement` (both protocols), A5 from Step 2, A7 by `test_postgres_statement_timeout_reaches_the_cursor_and_the_deadline_stops_the_rest`, A8 and A11 by the explain tests, A9 by the SQLite reader test, A13 and A14 from Tasks 8 and 7, A15 by Task 3's role test; A10 and A12 stay on the company checklist;
- §7's numbers measured here.

- [ ] **Step 9: The follow-ups note**

Write `docs/superpowers/notes/2026-10-2x-08a-mcp-server-backend-followups.md`, with these sections:
1. **Rulings.** P1–P18 with the coordinator's rulings.
2. **Carries for 08b:**
   - `mcp_access` must ride every profile draft (`toDraft` spreads it; a dialog that builds its draft field by field would turn sharing off on save).
   - Show `McpToken.command` and `replace_command` as they are (P7).
   - `/mcp/connections/{id}/login` answers 428 for a locked profile.
   - `HistoryEntry.source`, `client` and `repeats`, and `refused`'s Copy only.
   - The `refused` entry Task 5 put in `HistoryPanel`'s `STATUS` map.
   - `QueryTabState.from_agent` (the banner) is 08b's.
3. **The company checklist:**
   - A10: MySQL 5.7's `START TRANSACTION READ ONLY`, `max_execution_time`, `RELEASE_ALL_LOCKS` and the plain EXPLAIN.
   - A12: MySQL FILE with `secure_file_priv`.
   - PG 12/13's roles.
   - SQL Server without SHOWPLAN permission (A8's second half).
   - A login without `db_owner`.
4. **The packaged-app checks** for the user.
5. **The user's test list:** real Claude Code over a day of use, Regenerate, the switch, a red connection's 30 s, History's MCP records.

- [ ] **Step 10: Review against the pain points**

Write the whole 08a review against §2 Q1–Q6, §3 S9/M2/M11–M13, §5's principles and §8 into the follow-ups note.

- [ ] **Step 11: Commit**

```bash
git add backend/dbaguy-backend.spec CLAUDE.md docs/superpowers/specs/2026-10-25-08-mcp-server-design.md \
  docs/superpowers/notes/2026-10-2x-08a-mcp-server-backend-followups.md
git commit -m "docs(08a): packaging, the frozen sidecar over MCP, measurements; CLAUDE.md, the spec's 〔08a〕 notes, follow-ups

Co-Authored-By: <your model name> <noreply@anthropic.com>"
```

---

## Dry-run record

**Setup.**
- A detached worktree of 71fea18 in the planner's scratchpad, with `node_modules` linked from the main checkout.
- `backend/.venv` was an APFS clone of the main venv with `mcp==2.3.0` added. The main `backend/.venv` was never changed.
- `poetry.lock` came from `poetry add "mcp>=2.3,<3"` run with a scratch cache and no install. In it, `mcp`/`mcp-types` 2.3.0 and their dependencies are added, and `cffi`/`cryptography` lose their Linux-only marker.
- All five dev containers were up. None was started, stopped or restarted.
- The SDK's Streamable HTTP server always ran on a free port between 20,000 and 40,000. Every container used a temporary data dir and in-memory secret and token stores.

**How the plan's code was made and checked.**
- Each task's code was written and tested as one commit on the worktree, in task order.
- Review fixes found later were folded back into the task that owns the code, and the commit series was rebuilt, so each task stands alone.
- The blocks in this plan were generated from those commits: new files whole, and one `git diff` per task against the previous task's tree.
- Then, in a second fresh worktree of 71fea18, the blocks were applied in order exactly as written here: new files written, patches through `git apply`, generated files taken from their commands' output. Every task's tree came out identical to its commit. Every "Run them; they fail" step, run before that task's code, failed as its Expected line says.

| Task | Commit (scratch) | Files | Focused backend tests | `gen:api` | `tsc` | vitest |
|---|---|---|---|---|---|---|
| 1 | 3252422 | 27, +1,988 −32 | 413 passed (12 s) | +299 / +226 lines, then no diff | clean | 906 passed |
| 2 | e95440d | 17, +489 −43 | 219 passed (21 s) | — | — | — |
| 3 | 38921e4 | 14, +487 −8 | 195 passed (12 s) | +105 / +68, then no diff | clean | 906 passed |
| 4 | 0309603 | 3, +462 | 551 passed (0.5 s) | — | — | — |
| 5 | 3ca4e6d | 7, +242 −32 | 74 passed (3 s) | +48 / +16, then no diff | clean | 906 passed |
| 6 | d38b29b | 14, +1,012 −25 | 263 passed (24 s) | — | — | — |
| 7 | f0127dd | 3, +182 −7 | 219 passed (21 s) | — | — | — |
| 8 | f1deebb | 8, +639 −22 | 91 passed (24 s) | no diff | clean | 906 passed |
| 9 | 84f4190 | 1 (`dbaguy-backend.spec`), +8 −1 | the whole suite: 6,842 passed, 1 skipped (492 s) | — | — | — |

In all: 66 files, +5,472 −133 (the documents of Task 9 Steps 7–9 not included).

**Found late and fixed in place (P18).** The first cancel test passed without testing anything: it looked for `query = 'SELECT pg_sleep(29)'`, but inside the frame the backend shows the cursor's `FETCH`. With a test that sees the statement running first, the 2026-07-28 cancel turned out not to reach the tool under `json_response=True`.
- The fix is Task 1's `json_response=False` and Task 8's stronger test with its `mcp_session` helper. The commit series was replayed with them, and nothing else changed.
- Tasks 1, 2, 3, 6, 7 and 8 were run again on the new commits, with the counts above. Tasks 4 and 5 do not use the endpoint.
- The whole suite (the table's Task 9 row) and the frozen probe ran on the new Task 9 tree.

Each task's focused run is the command in its own "Run the tests" step, run on that task's commit alone. The "Run them; they fail" steps were checked the same way, on the previous task's commit plus the new tests. "Then no diff" means `npm run gen:api` run again on the commit changed nothing.

**After the dry run** (the Appendix's `leftovers.py`):
- no `zz_` relation, function, routine, user, role or database on PG, MySQL or SQL Server;
- no PG advisory lock;
- `app.orders` at 500, with no `zz_08` key;
- no Couchbase advisor session.

**Measurements** (in the steps that take them):
- Task 1: start 0.29–0.35 s, RSS +27 MB.
- Task 6: 1 MB × 1,000 rows gave 36 rows, 37 read, in 0.07–0.08 s, with a traced peak of 9.6 MB. 100 MB values gave 1 row in 0.89 s, with a traced peak of 200 MB and RSS +290 MB. 100 rows × 10 columns took 14–18 ms.
- Task 8: median call PG 4.4 ms, MySQL 2.8, SQL Server 6.1, Couchbase 1.3.
- Task 9, PyInstaller only, built three times (the last from the replayed Task 9 tree): 63,152–63,156 KB, 95/95 `codesign --verify --strict`, 26 s, and the only warning is the old `psycopg_binary` one. The frozen probe ran 8 times over those builds: ready 0.50–0.67 s (14.5–15 s on the first run after each build); switch to listening 0.26–0.32 s (0.64–0.86 s on that first run); RSS 103–104 → 125–127 MB (+22) listening, 162–168 MB after the calls; 401/421/403; all 32 calls ok on every run; no `ModuleNotFoundError`, no `Traceback`.

**Not done while planning:**
- `npm run build:app` and `scripts/measure-startup.sh` (Task 9 Steps 2 and 5): only PyInstaller's `dist/` was built.
- Claude Code itself (Task 9 Step 4, P17).

## Appendix: the probes

These scripts are not committed. Copy each one into your scratchpad when its step asks for it.

How to run them:
- The first four run from `backend/` with `PYTHONPATH=. .venv/bin/python <file>`. They import `tests.agent_support` and `tests.support`, use temporary data dirs and in-memory secret and token stores, and never bind 17531 or 17532.
- `frozen_mcp_probe.py` takes the frozen sidecar's path. It runs with the dev venv's Python because it needs the `mcp` client.
- All of them only read the dev databases. Their own scratch is a temporary directory, including the probe's SQLite file.

### `start_cost.py`

```python
"""Task 1: the switch's cost in-process (start time, RSS). From backend/: PYTHONPATH=. .venv/bin/python start_cost.py"""
import os, pathlib, subprocess, tempfile, time
from tests.agent_support import make_container, free_port, wait_for_state
from dbaguy_backend.domain.settings import McpSettings
def cur(): return int(subprocess.check_output(["ps","-o","rss=","-p",str(os.getpid())]))/1024
with tempfile.TemporaryDirectory() as d:
    c = make_container(pathlib.Path(d))
    before = cur(); t = time.monotonic()
    c.agent_host.apply(McpSettings(enabled=True, port=free_port()))
    wait_for_state(c, "listening")
    print(f"start {time.monotonic()-t:.2f}s rss {before:.1f} -> {cur():.1f} MB (+{cur()-before:.1f})")
    c.close()
```

### `mem.py`

```python
"""Task 6: memory and time of big results through the reader (PG). From backend/: PYTHONPATH=. .venv/bin/python mem.py"""
import os, subprocess, tempfile, pathlib, time, tracemalloc
from uuid import UUID
from tests.agent_support import make_container, share
from tests.support import make_pg_profile
from dbaguy_backend.agent.reader import ReadRequest
from dbaguy_backend.sql.dialects import Dialect
def rss(): return int(subprocess.check_output(["ps","-o","rss=","-p",str(os.getpid())]))/1024
with tempfile.TemporaryDirectory() as d:
    c = make_container(pathlib.Path(d))
    pid = share(c, make_pg_profile(name="m"))
    prof = c.profiles.get(UUID(pid))
    c.agent_reader.read(ReadRequest(profile=prof, dialect=Dialect.POSTGRES, sql="SELECT 1"))
    for size in (1_000_000, 100_000_000):
        before = rss(); t=time.monotonic()
        tracemalloc.start(); tracemalloc.reset_peak()
        o = c.agent_reader.read(ReadRequest(profile=prof, dialect=Dialect.POSTGRES, sql=f"SELECT repeat('x', {size}) FROM generate_series(1, 1000)", max_rows=1000, timeout_s=60))
        peak = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
        print(f"repeat {size}: status {o.status} rows {len(o.rows)} read {o.rows_read} dropped {o.rows_dropped} {time.monotonic()-t:.2f}s peak(traced) {peak/1e6:.1f} MB rss {before:.0f}->{rss():.0f} MB")
    for n in (100, 1000):
        t=time.monotonic(); o = c.agent_reader.read(ReadRequest(profile=prof, dialect=Dialect.POSTGRES, sql=f"SELECT g, 'it''s 中文 ' || g, g*1.5, now(), md5(g::text), g % 7, true, null, 'x', 42 FROM generate_series(1,{n}) g", max_rows=n))
        import json
        print(n, "rows", len(o.rows), "ms", int((time.monotonic()-t)*1000), "json chars", len(json.dumps(o.rows, ensure_ascii=False)))
    c.close()
```

### `types.py`

```python
"""Task 8: column types without describe, and a call's median time per engine. From backend/: PYTHONPATH=. .venv/bin/python types.py"""
import tempfile, pathlib, time
from uuid import UUID
from tests.agent_support import make_container, share
from tests.support import make_pg_profile, make_mysql_profile, make_mssql_profile, make_couchbase_profile
from dbaguy_backend.agent.reader import ReadRequest
from dbaguy_backend.sql.dialects import Dialect as D
with tempfile.TemporaryDirectory() as d:
    c = make_container(pathlib.Path(d))
    cases = [(make_pg_profile(name="pg"), D.POSTGRES, "SELECT 1::bigint AS a, 'x'::text AS b, now() AS c, 1.5::numeric AS d"),
             (make_mysql_profile(name="my"), D.MYSQL, "SELECT 1 AS a, 'x' AS b, NOW() AS c, 1.5 AS d"),
             (make_mssql_profile(name="ms"), D.TSQL, "SELECT CAST(1 AS bigint) AS a, N'x' AS b, SYSDATETIME() AS c, CAST(1.5 AS decimal(5,2)) AS d, CAST(1 AS bit) AS e"),
             (make_couchbase_profile(name="cb"), D.SQLPP, "SELECT 1 AS a, 'x' AS b")]
    for prof, dialect, sql in cases:
        pid = share(c, prof)
        p = c.profiles.get(UUID(pid))
        c.agent_reader.read(ReadRequest(profile=p, dialect=dialect, sql="SELECT 1"))
        ts=[]
        for i in range(20):
            t=time.monotonic(); o = c.agent_reader.read(ReadRequest(profile=p, dialect=dialect, sql=sql)); ts.append((time.monotonic()-t)*1000)
        ts.sort()
        print(dialect.value, o.status, o.columns, o.rows, f"median {ts[10]:.1f} ms")
    c.close()
```

### `frozen_mcp_probe.py`

```python
"""Task 9's probe: the frozen sidecar with the MCP switch on, every tool on the five dev engines, the gate's 401/421/403,
the switch's cost. Run with the dev venv's Python (it has the mcp client): python frozen_mcp_probe.py <sidecar>."""
import json, os, signal, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

import anyio
import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import Implementation

SIDECAR, PORT, API, MCP = sys.argv[1], 27_531, "probe-api-token", "probe-mcp-token"
assert PORT not in (17531, 17532)


def rss(pid: int) -> float:
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)])) / 1024


async def tools(calls):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {MCP}"}) as http:
        async with Client(streamable_http_client(f"http://127.0.0.1:{PORT}/mcp", http_client=http), mode="legacy",
                          client_info=Implementation(name="frozen-probe", version="1")) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            out = []
            for name, args in calls:
                started = time.monotonic()
                result = await client.call_tool(name, args)
                out.append((name, args.get("connection"), result.is_error, int((time.monotonic() - started) * 1000),
                            result.content[0].text[:120]))
            return names, out


with tempfile.TemporaryDirectory(prefix="zz_08_probe_") as tmp:
    data = Path(tmp) / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"mcp": {"enabled": False, "port": PORT}}))
    lite = Path(tmp) / "zz_08.db"
    sqlite3.connect(lite).executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t (v) VALUES ('a');")
    env = {**os.environ, "DBAGUY_TOKEN": API, "DBAGUY_DATA_DIR": str(data), "DBAGUY_MCP_TOKEN": MCP,
           "DBAGUY_LOG_TO_FILE": "false"}
    started = time.monotonic()
    proc = subprocess.Popen([SIDECAR], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        api_port = json.loads(proc.stdout.readline())["port"]
        ready_s, rss_ready = time.monotonic() - started, rss(proc.pid)
        api = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", headers={"Authorization": f"Bearer {API}"})
        assert api.get("/mcp/status").json()["state"] == "off"
        settings = api.get("/settings").json()
        time.sleep(2)
        rss_off = rss(proc.pid)  # the switch off: no SDK loaded
        switched = time.monotonic()
        api.put("/settings", json={**settings, "mcp": {"enabled": True, "port": PORT}})
        while api.get("/mcp/status").json()["state"] != "listening":
            time.sleep(0.02)
        listening_s, rss_listening = time.monotonic() - switched, rss(proc.pid)
        profiles = {
            "pg": {"engine": "postgres", "host": "127.0.0.1", "port": 54317, "database": "dbaguy", "username": "dbaguy", "ssl": {"mode": "disable"}},
            "mysql": {"engine": "mysql", "host": "127.0.0.1", "port": 33067, "database": "dbaguy", "username": "dbaguy", "ssl": {"mode": "require"}},
            "mssql": {"engine": "mssql", "host": "127.0.0.1", "port": 14337, "database": "dbaguy", "username": "dbaguy"},
            "cb": {"engine": "couchbase", "host": "127.0.0.1", "database": "dbaguy.app", "username": "dbaguy", "ssl": {"mode": "disable"}},
            "lite": {"engine": "sqlite", "path": str(lite)},
        }
        passwords = {"pg": "dbaguy", "mysql": "dbaguy", "mssql": "Dbaguy_dev1", "cb": "dbaguy", "lite": None}
        ids = {}
        for name, body in profiles.items():
            body = {**body, "name": f"zz_08 {name}", "mcp_access": "query", "save_password": name == "lite"}
            ids[name] = api.post("/profiles", json={"profile": body, "secrets": {"password": passwords[name]}}).json()["profile"]["id"]
        url = f"http://127.0.0.1:{PORT}/mcp"
        gate = (httpx.post(url, json={}).status_code,
                httpx.post(url, json={}, headers={"Authorization": f"Bearer {MCP}", "Host": f"evil:{PORT}"}).status_code,
                httpx.post(url, json={}, headers={"Authorization": f"Bearer {MCP}", "Origin": "http://localhost"}).status_code)
        reads = {"pg": ("SELECT * FROM public.customers", "customers", None), "mysql": ("SELECT * FROM customers", "customers", None),
                 "mssql": ("SELECT * FROM dbo.customers", "customers", None), "cb": ("SELECT * FROM users LIMIT 3", "users", "app"),
                 "lite": ("SELECT * FROM t", "t", None)}
        calls = [("list_connections", {})]
        for name, (sql, table, schema) in reads.items():
            calls += [("list_objects", {"connection": ids[name]}), ("search_objects", {"connection": ids[name], "pattern": table}),
                      ("describe", {"connection": ids[name], "name": table, **({"schema": schema} if schema else {})}),
                      ("run_query", {"connection": ids[name], "sql": sql}), ("explain", {"connection": ids[name], "sql": sql}),
                      ("run_query", {"connection": ids[name], "sql": "DROP TABLE zz_08_none"})]
        calls.append(("sample_fields", {"connection": ids["cb"], "bucket": "dbaguy", "scope": "app", "collection": "users", "size": 20}))
        names, results = anyio.run(tools, calls)
        rss_after = rss(proc.pid)
        print(f"ready {ready_s:.2f} s; switch on to listening {listening_s:.2f} s; RSS ready {rss_ready:.0f} MB, "
              f"off {rss_off:.0f} MB, listening {rss_listening:.0f} MB (+{rss_listening - rss_off:.0f}), "
              f"after the calls {rss_after:.0f} MB")
        print("tools:", names)
        print("401/421/403:", gate)
        for (_, args), (name, conn, error, ms, text) in zip(calls, results):
            expect_error = args.get("sql", "").startswith("DROP")  # the gate's refusal is the one expected error
            mark = "ok" if error == expect_error else "UNEXPECTED"
            print(f"  {mark} {name} {conn and conn[:8]} error={error} {ms} ms {text[:70]!r}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            _, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        print("ModuleNotFoundError in stderr:", "ModuleNotFoundError" in err, "| Traceback:", "Traceback" in err)
```

### `leftovers.py`

```python
"""Task 9 and every live run: nothing zz_ left on the dev databases, Couchbase untouched. From backend/: PYTHONPATH=. .venv/bin/python leftovers.py"""
import psycopg, mysql.connector, json
from tests.support import mssql_connect, couchbase_query, mysql_driver_kwargs
pg = psycopg.connect(host="127.0.0.1", port=54317, user="dbaguy", password="dbaguy", dbname="dbaguy", autocommit=True)
print("pg relations:", pg.execute("SELECT relname FROM pg_class WHERE relname LIKE 'zz\\_0%'").fetchall())
print("pg functions:", pg.execute("SELECT proname FROM pg_proc WHERE proname LIKE 'zz\\_0%'").fetchall())
print("pg advisory:", pg.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory'").fetchone())
print("pg dbs:", pg.execute("SELECT datname FROM pg_database WHERE datname LIKE 'zz\\_%'").fetchall())
pg.close()
my = mysql.connector.connect(**mysql_driver_kwargs(user="root", database=None))
cur = my.cursor()
cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_name LIKE 'zz\\_0%'"); print("mysql tables:", cur.fetchall())
cur.execute("SELECT routine_schema, routine_name FROM information_schema.routines WHERE routine_name LIKE 'zz\\_0%'"); print("mysql routines:", cur.fetchall())
cur.execute("SELECT user FROM mysql.user WHERE user LIKE 'zz\\_0%'"); print("mysql users:", cur.fetchall())
cur.execute("SHOW DATABASES LIKE 'zz\\_%'"); print("mysql dbs:", cur.fetchall())
my.close()
ms = mssql_connect(user="sa", password="Dbaguy_dev1", database="master")
ms.execute_query("SELECT name FROM sys.databases WHERE name LIKE 'zz[_]%'"); print("mssql dbs:", [r[0] for r in ms])
ms.execute_query("SELECT name FROM dbaguy.sys.objects WHERE name LIKE 'zz[_]0%'"); print("mssql objects:", [r[0] for r in ms])
ms.close()
print("couchbase orders:", couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.orders")["results"],
      "zz_08 keys:", couchbase_query("SELECT RAW META(o).id FROM dbaguy.app.orders AS o WHERE META(o).id LIKE 'zz_08%'")["results"])
print("advisor sessions:", couchbase_query('SELECT ADVISOR({"action": "list"})')["results"])
```

## Self-review

**Spec §11 → this plan's tasks.** The spec's own numbering is in brackets. §10's "08a Task n" means the spec's numbering.

| Spec §11 (08a) | Plan |
|---|---|
| [1] Dependency and spike, `AgentHost`, `token_store`, auth, `TransportSecuritySettings`, `McpSettings`, `/mcp/status`, `/mcp/token`, regenerate, HTTP tests; A1–A3, A6 | Task 1. A3 is `test_starting_and_stopping_again_and_again_leaves_no_thread_and_frees_the_port`. A1, A2 and A6 move to Task 9 Step 4 (P17); both protocol versions are tested with the SDK's own client in Tasks 1 and 8. |
| [2] `mcp_access`, the agents' catalog connection, `list_connections`, `list_objects`, `search_objects`, `describe`, hostile names | `mcp_access` and `list_connections` are in Task 1: the model change runs `gen:api` with the rest of the API. The rest is Task 2. |
| [3] Login privileges, the route, `list_connections`' `login`; A15 | Task 3 (P1, P2, P16). |
| [4] The gate: allowed starts, deny list, `U&` decoding, MySQL `INTO`; appendix A's G and H1–H3 as tests | Task 4 (141 cases; P5 adds executable comments). |
| [5] Agent sessions, `open(…, agent=True)`, SQLite's authorizer and `query_only`, MySQL's `sql_mode`, `ReadFrame` and its end, `AgentReader`; the write tests with the gate taken out, memory; A7, A9 | Task 6. The `sql_mode` check on every statement is Task 7's guard. |
| [6] MySQL's shared read-only helper, tabs and agents; M3's regression; A14 | Task 7 (P11: A14 is answered no). |
| [7] `run_query`, `explain`, `sample_fields`; `shape.py`; timeouts (red included), cancel; A4, A8, A11, A13 | `shape.py` is in Task 6, with the pages it sizes. The tools, timeouts and cancel are Task 8 (P9, P12). |
| [8] History: columns, `refused`, `repeats`, merging, the 5,000 cap, the `source` filter, the agents' records; errors and log (`CallToolResult`, the `mcp.*` logger, the caplog audit); `instructions` and tool descriptions | The store and the API are Task 5. Writing the agents' records is Task 8 (P3). The `CallToolResult` errors, the logger, `instructions` and the descriptions are Task 1; the log audit is Task 8. |
| [9] Packaging and measurements (§7, A5); CLAUDE.md | Task 9. |

**R- and D- decisions.**

| Decision | Task |
|---|---|
| R-08-1 | 1 |
| R-08-2 | 1 (P6) |
| R-08-3 | 1 (P8) |
| R-08-4 | 1 |
| R-08-5 | 1 (the field and the per-call reread), 2 (`schema`), 8 (`query`) |
| R-08-6 | 4 (the gate), 6 (the session default, the frame and rollback, SQLite's layers, Couchbase), 7 (MySQL) |
| R-08-7 | 2, 6, 8 |
| R-08-8 | 5, 8 |
| R-08-9 | 2 (`answered`), 8 (`test_a_locked_connection_says_so_and_asks_for_nothing`), with P14 |
| R-08-12 | 3 (`can_write`), 6 (the rollback test), 1 (`read_only_by`) |
| D-08-1 | 6 |
| D-08-2 | 4 |
| D-08-3 | 6 |
| D-08-4 | 6 |
| D-08-5 | 7 |
| D-08-6 | 1 |
| D-08-7 | 1 |
| D-08-8 | 1 |
| D-08-9 | 1 (P18: `json_response=False`) |
| D-08-10 | 6, 8 |
| D-08-11 | 2 |
| D-08-12 | 1, 3 |
| D-08-13 | 5, 8 (P3) |
| D-08-14 | 8 |

R-08-10 (no stdio bridge) and R-08-11 (no resources or prompts) need nothing built: Task 1's server registers tools only.

The CR rulings:
- CR-08-1: Task 3, and the gate's PG list in Task 4.
- CR-08-3: Tasks 5 and 8.
- CR-08-4: Task 4 for the names, Task 6 for the end of the frame.
- CR-08-5: Task 6 at open, Task 7 for the check.
- CR-08-6: Task 4.
- CR-08-7: Task 6 (P10).
- CR-08-8: Task 8.
- CR-08-9: Task 2.
- CR-08-11: Task 7.
- CR-08-2 and CR-08-10 are 08b's text and §9's known limitations; Task 9 writes them into CLAUDE.md.

§6's tests: the gate's unit tests are Task 4, the shape tests Task 6, History's Task 5. Every live row is in Task 6 or Task 8, and the HTTP rows in Tasks 1 and 8. The frontend's `mcpCommand.ts` is gone (P7). §7's measurements are Tasks 1, 6, 8 and 9.

**Left to 08b, on purpose:**
- the Settings section;
- the dialog's AI access and login list, Duplicate connection;
- History's MCP mark, filter, ×N and Copy only;
- the reopened-statement banner.

08a adds only the three frontend lines its type check needs: the draft's `mcp_access`, `DEFAULT_SETTINGS.mcp`, and the `refused` status.

**Placeholders.**
- None remain unfilled. Every code step carries its code.
- These names in angle brackets or with an x are meant to be filled when the step runs:
  - `<your model name>` in the commit trailers;
  - the follow-ups file's `2026-10-2x`, dated the day it is written;
  - `<scratchpad>` in each patch's instruction, and `<scratch>` and `<port>` in Task 9's commands.

**Consistency of names.**
- Every name in a task's Interfaces exists in the code of that task or an earlier one. This was checked by script against the final tree.
- Every task's commit lists exactly the files its code creates or changes. This was checked by script against the dry run's commits; Task 9 adds the three documents.
- Every new file in a task's file list has its block in that task.
