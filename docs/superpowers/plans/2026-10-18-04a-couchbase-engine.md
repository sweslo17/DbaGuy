# 04a: Couchbase SQL++ dialect, HTTP query engine and dev cluster — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make self-hosted Couchbase Server 7.x a DbaGuy engine that connects (URL import, seed list with node discovery, TLS, SSH, Keychain), browses (bucket › scope › collection › index with generated DDL) and runs SQL++ (client-side splitting, classification and read-only, streamed pages, cancel by socket shutdown, tab timeout, exact numbers and key order, MISSING ≠ NULL, `*` expansion with an overflow column, metrics and warnings, raw JSON/JSONL export) against a seeded Enterprise 7.6.12 dev cluster, with the minimum frontend that makes queries work.

**Architecture:** `sql/` gains `Dialect.SQLPP` only through its rule tables plus a keyword-only classifier (sqlglot has no SQL++), and a formatter token guard serves every dialect. The adapter contract grows generic, default-off pieces — `run(sql, options)` with `RunOptions`, capability fields, `presentation()`, `RawJsonRows`, typed cells (`JsonText`, `MISSING`, `ColumnCategory.MIXED`) — that the worker checks and uses (option checks, idle close of paused results, raw JSON export, query context in the history). A new `adapters/couchbase/` package talks to the Query Service REST API with stdlib `http.client` and an incremental response parser (no SDK): pure modules for the parser, row shaping, messages, settings and catalog, and an adapter that wires them with a cancel that shuts the socket down from any thread.

**Tech Stack:** Python 3.13 stdlib `http.client` / `ssl` / `socket` / `json` / `codecs` / `decimal` (no Couchbase SDK, no new Python dependency), FastAPI, pydantic v2, loguru; Couchbase Server `enterprise-7.6.12` in Docker (OrbStack, arm64); React 19 + TypeScript, `@codemirror/lang-sql` 6.10.0 (`SQLDialect.define`), `sql-formatter` 15.9.0 (`"n1ql"`).

**Spec:** docs/superpowers/specs/2026-10-18-04-couchbase-design.md — §11 item 1 (04a), built on §2–§8 and the §10 assumptions assigned to 04a. Technical survey: docs/research/2026-10-18-04-couchbase-survey.md (cited "survey §x").

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 04a serves it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 (§8 又重又慢) | No Python dependency added; the sidecar grows by ≈ 0 (Task 13 measures it). Responses stream through an incremental parser with TCP backpressure; a paused result holds one parser buffer (Tasks 7, 11). Peeking for `*` is capped at 500 rows / 8 MiB (Task 8). Large rows parse in at most twice their size (Task 7 doubling rule). Absolute speed budget (Task 11) and RSS during 1M rows < 30 MB growth (Task 13). |
| §2 Q2 穩定、不丟工作 | No C++ SDK abort path; a malformed response, a dropped node or a cancel becomes one statement's error, never a crash (Tasks 7, 11). Only the worker thread reads the HTTP connection; other threads only shut down a dup of its socket (Task 7). The paused-result idle close keeps the loaded rows and says why (Task 6). Seed and dev cluster restart cleanly (Task 1). |
| §2 Q3 可信任 | Numbers keep their literal (`9007199254740993`, uint64, `1.10`, `1e400`), key order is the server's, MISSING is not NULL, JSON/JSONL export is the server's text (Tasks 4, 5, 8, 11). Read-only is enforced by the server (`readonly=true`) as well as the classifier (Tasks 3, 11). Server errors are shown as sent, with UTF-16 positions (Task 8). Basic auth over plain HTTP to a remote host is flagged in the dialog (Task 12). The `Authorization` header, passwords, parameter values and row contents are never logged (all tasks). |
| §2 Q4 聚焦 80% | No INFER, no cluster management, no Capella, no SQL++ transactions (refused), no KV (FP §4 Won't, spec §1 out of scope). 04a builds only what makes queries work; context picker, parameters panel, EXPLAIN view, sampling and documents are 04b/04c. |
| §2 Q5 多引擎從第一天 | Couchbase plugs into the same adapter/worker/WS protocol; every new piece is a generic capability (`RunOptions`, `presentation`, `transactions`, MIXED cells, raw export, endpoint hook) that other engines can use later (Tasks 4–6). |
| §2 Q6 macOS 慣例 | Nothing new beyond the existing ⌘↵ / ⌘⇧F / ⌘W; the formatter guard keeps ⌘⇧F from silently changing a statement in any dialect (Task 2). |
| FP §4 Must 1 連線 | `couchbase://`, `couchbases://`, `http(s)://` import; seed lists and `nodeServices` discovery; SSH to one node; TLS modes incl. verify-full through a tunnel; Keychain unchanged (Tasks 9, 11, 12). Capella items are out (FP §8). |
| FP §4 Must 2 Keyspace 樹 | bucket › scope › collection › index from system catalogs (300 collections, nothing dropped), index state and generated DDL, `_default` / `_system` handling (Task 10). Clicking sets the context: 04b. |
| FP §4 Must 3 編輯器 | Client-side splitting (one statement per request), ⌘↵ / selection / all, cancel, tab timeout (Tasks 2, 11). Typed parameters reach the protocol and the request body (Tasks 4, 6, 7); the panel and the context picker are 04b. |
| FP §4 Must 4 結果 | Streaming pages, signature column order, union of keys on the first page + overflow column, int64/CAS exact, metrics and warnings messages, Load more (Tasks 8, 11). |
| FP §4 Must 5 匯出 | CSV/TSV/Markdown by grid columns (nested values as JSON text), JSON/JSONL as the server's row text (Tasks 5, 6). |
| FP §4 Must 6, 7 | Not 04a (04c documents; 04b EXPLAIN). 04a only marks EXPLAIN/ADVISE results `presentation="plan"` (Task 11). |
| FP §4 Must 8 History | Each record keeps the request's query context in the `database` field (Task 6). |
| FP §8 自架 7.x | Enterprise 7.6.12 dev cluster; edition detection (Task 11); 7.0–7.2 behaviour is passed through as server errors. |
| M5 執行範圍 | SQL++ is always split on the client; `;` inside strings, back-ticks, comments, `{…}` / `[…]` constructors never splits (Task 2). |
| M7 / M8 grid and JSON | MIXED cells keep their own type; JSON cells carry the server text; the value viewer shows a lossless tree; MISSING is grey (Tasks 4, 12). |
| M9 複製與匯出 | IN list as an SQL++ array, no INSERT for SQL++, MISSING empty in text formats and omitted in JSON (Task 5). |
| M10 錯誤訊息 | Server message and code, `reason` and `missing_role`, line + code-point column turned into UTF-16 (Task 8). |
| M11 安全 | SQL++ classifier with drop / unfiltered update-delete / `system:` / MERGE…DELETE reasons, fail-closed EXECUTE, server-enforced read-only (Tasks 3, 11). No transactions, so no idle transaction (Task 11). |
| M12 取消與 timeout | Socket shutdown from any thread, effective immediately on the server (`closed`), the worker's tab timeout reused, no server `timeout` sent (Tasks 7, 11). |

## Global Constraints

- Scope is spec §11 item 1 (04a) on branch `feat/04-couchbase` (base 9d48329). 04b and 04c follow on the same branch: do not merge to `main` in this plan.
- **No Couchbase SDK.** Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`); no new Python dependency: only stdlib `http.client`, `ssl`, `socket`, `json`, `codecs`, `decimal` (spec §7). No new frontend dependency in 04a (`@codemirror/lang-json` is 04c).
- Binding values from the spec, each defined once:
  - `adapters/couchbase/adapter.py`: `PAUSED_RESULT_MAX_IDLE_S = 45.0` (was 120.0: A13 does not hold, 7.6.12 ends a stalled request 77–86 s after the pause), passed to the worker as the capability `paused_result_max_idle_s`; the housekeeping pass runs every 15 s (`container._HOUSEKEEPING_INTERVAL_S`), so a paused result closes 45–60 s after its first pause (ruling R-04a-T11b: the clock runs from the result's first pause and a Load more does not restart it).
  - `adapters/couchbase/rows.py`: `STAR_PEEK_ROWS = 500`, `STAR_PEEK_BYTES = 8 * 1024 * 1024` (at least 1 row); `OVERFLOW_NAME = "(other fields)"`.
  - `adapters/couchbase/http.py`: `CONNECT_TIMEOUT_S = 10.0` (connect and TLS handshake; reads afterwards are blocking with no timeout), `KEEPALIVE_REUSE_S = 30.0`, `READ_CHUNK = 64 * 1024`, path `/query/service`.
  - `adapters/couchbase/nodes.py`: `DISCOVERY_TIMEOUT_S = 3.0`.
  - `adapters/couchbase/settings.py`: query ports 8093 / 18093 (TLS), manager ports 8091 / 18091 (TLS), `default_port = 8093`.
  - `client_context_id` = `"dbaguy-" + uuid4().hex` (`run()` receives no request id, ruling P30) with every character outside `[A-Za-z0-9_-]` removed, cut to 64 characters; `client_context_id()` takes any id string (P47).
  - TCP keepalive 30 s idle, 10 s interval, 3 probes, through the shared `adapters/sockets.enable_keepalive()`.
- Request body (spec §3.4.1): assembled as text, parameter literals embedded unchanged; `readonly: true` for every request of a read-only profile and for every internal query (catalog, probes); `query_context` as `default:` + quoted parts; **never** send `timeout` or `scan_consistency`; header `User-Agent: DbaGuy/<app_version()>`.
- Only the worker thread that owns a connection sends, reads or closes its HTTP connection. Another thread may only call `cancel()` / `force_cancel()` / `abort()`, which shut down a dup of the socket taken at connect time; never `SSLSocket.shutdown()` (one exception, ruling P41, spec §9: only if Task 1's A10 probe shows that shutting down the duplicated TCP descriptor does not end a blocked TLS read, `HttpChannel.shutdown()` also shuts down the TLS socket, and then any exception the reader gets while the cancel flag is set counts as cancelled), never closing the shared socket object from another thread (the ③ lesson: no driver state is touched by two threads).
- Logging is loguru, per spec §5. INFO: connection (host:port, chosen node, TLS mode, discovery outcome, edition, version), session open/close, cancel (how, ms), paused-result auto-close. WARNING: connection lost mid-request, malformed response (offset and field name only), ignored option keys, dropped URL parameter keys, keys outside the columns after the first page. DEBUG: SQL text (first 2000 characters, as the worker), request id and `client_context_id`, parameter **names**, HTTP status, metrics. **Never**: the `Authorization` header, passwords, parameter values, document or row contents.
- DbaGuy's own errors are fixed texts that never echo SQL, documents, parameter values, URLs or passwords; server errors are shown as sent (M10).
- Literals and identifiers come only from `sql/dialects.py` (SQL++: `quote_ident` / `force_quote_ident` / `keyspace_path` / `quote_literal` = JSON). Catalog queries use named parameters, never spliced names (spec §3.8). `sql/` stays pure: no I/O, every dialect table is a dict keyed by `Dialect`, SQL text never logged above DEBUG. Offsets crossing HTTP are UTF-16, converted only through `sql/offsets.py` (`Utf16Index`, `utf16_column`).
- Dev cluster: service `couchbase`, image `couchbase/server:enterprise-7.6.12` (arm64), ports bound same-number on `127.0.0.1`: `8091`, `8093`, `18091`, `18093` (no KV port on the host); cluster admin `dbaguy` / `dbaguy`; limited user `dbaguy_reader` / `dbaguy` (`query_select[dbaguy:app:users]`, `data_reader[dbaguy:app:users]`, no `query_use_sequential_scans`); about 1.2 GiB. One-shot `couchbase-init` (same image) seeds once and then stays up idle (R-03b-P1). The bastion reaches it as `couchbase:8093`. Start everything with `docker compose -f dev/docker-compose.yml up -d --build --wait`; from Task 1 on, run every backend test with the containers up, and no `requires_couchbase` test may be skipped in a task report.
- The repo is public: only the fake credentials above. TLS tests fetch the cluster CA at run time (`GET /pools/default/trustedCAs`) into a temporary file; no certificate is committed.
- Never touch `~/Library/Application Support/DbaGuy`. `scripts/dev.sh --web` uses `dev/data`; ad-hoc sidecar runs use `DBAGUY_DATA_DIR=$(mktemp -d)`. Profiles created in browser checks or ad-hoc sidecar runs use `"save_password": false` (check `dev/data/profiles.json` afterwards). Nothing is written to the Keychain.
- **Never kill processes by pattern** (no `pkill -f`, no `killall`). Stop only processes you started, by the PID you noted. Remove only containers you created, by name.
- Tests cover only error-prone logic: lexer, splitter, classifier and safety, literals, the format guard, cell encoding and writers, the worker's option checks and idle close, the response parser (every byte split), row shaping, messages, settings and URL import, discovery parsing, catalog DDL, and adapter integration against the dev cluster (`requires_couchbase`). The frontend has no test framework: `npm run typecheck && npm run build` plus the manual browser check in Task 12.
- `cd backend && .venv/bin/pytest -q` must be green after every task.
- After any backend model or API change, run `npm run gen:api` and commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts`; never hand-edit them. TS imports API types only through `src/lib/api/types.ts`. `npm run typecheck && npm run build` must pass after every task that touches `src/` or regenerates the types.
- Frontend: colours only through the Tailwind token classes (no hex or rgb). Any new key handler checks `isImeKeyEvent(e)` from `@/lib/ime`.
- Commit after each task; stage only that task's files. The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties): wait, or `docker compose -f dev/docker-compose.yml restart bastion`.

## Review Focus

1. **Exact numbers and key order end to end.** `u_numbers` (2⁵³+1, int64 max/min, uint64 max, 20 digits, `1.10`, long π, `1E+3`, `1e-400`, `1e400`) must reach the grid cells, the value viewer, CSV and JSON/JSONL exports with the stored literal; `u_keyorder` keeps its key order in alias expansion and in the JSONL line (whitespace outside strings squeezed, tokens identical). Any `float()` or `json.loads` round trip in the path breaks this. → Task 5 `test_json_cells_in_every_writer`, `test_raw_json_writer_keeps_row_text`; Task 8 `test_number_cells_follow_the_literal`; Task 11 `test_stored_numbers_reach_the_cells_exactly`, Task 11 sessions `test_jsonl_export_is_the_server_text`.
2. **A server error that arrives after rows.** The rows already streamed stay; the error is reported with its code even when it follows the last row (it must not be swallowed because the worker stops calling `fetch()` after a short page), and `status:"stopped"` without errors is a cancel. → Task 7 `test_errors_after_rows_come_after_them`; Task 11 `test_errors_after_the_last_row_raise_from_next_result`, `test_stopped_without_errors_is_a_cancel`, integration `test_an_error_after_rows_keeps_the_rows`.
3. **Cancel and close at every moment.** A cancel while waiting for a blocking aggregate, while the `*` peek reads, while a page is streamed, between `run()` starting and the request going out (kept, nothing sent), with nothing running (no effect on the next statement), and closing a paused result: each ends at once, the server shows the request `closed`, and the next statement on the tab works. → Task 7 `test_shutdown_from_another_thread_ends_a_blocked_read`, `test_attach_refusal_sends_nothing`; Task 11 `test_a_cancel_during_the_star_peek_keeps_the_rows_read`, `test_cancel_a_long_aggregate_within_a_second`, `test_a_cancel_during_the_start_is_kept`, `test_an_idle_cancel_never_reaches_the_next_statement`, `test_closing_a_partly_read_result_frees_the_server_request`; Task 6 `test_idle_close_and_fetch_more_race_either_way`.
4. **`*` with heterogeneous documents.** Different key sets on the first page, keys that appear only after it, non-object documents, an alias whose value is not an object in a later row, a document key equal to a projected name, and a document key equal to the overflow name: columns never change mid-stream and no key or value is lost. → Task 8 `test_star_keys_after_the_first_page_go_to_the_overflow`, `test_alias_expansion_needs_one_object_alias_on_every_first_page_row`, `test_alias_expansion_falls_back_per_row`, `test_names_that_collide`; Task 11 `test_star_over_mixed_documents`.
5. **Read-only cannot be bypassed.** `EXECUTE` of a prepared write, `EXECUTE FUNCTION`, `NEXT VALUE FOR` inside a SELECT, `DELETE FROM system:active_requests`, `BEGIN WORK` are blocked or refused by DbaGuy, and the same statements sent straight to the adapter of a read-only profile are rejected by the server (1000). → Task 3 `test_sqlpp_read_only_bypasses_fail_closed`; Task 11 `test_read_only_profile_is_enforced_by_the_server`.

---

## File Structure

```
dev/docker-compose.yml                    couchbase + couchbase-init; bastion depends on couchbase
dev/couchbase/seed.sh                     (new) one-shot seed: cluster init, travel-sample, bucket dbaguy, docs, user
dev/couchbase/init/01-keyspaces.n1ql      (new) scopes app, many; app collections
dev/couchbase/init/02-indexes.n1ql        (new) primary + secondary + deferred indexes
dev/couchbase/init/03-data.n1ql           (new) events (20k), orders, u_big, u_ttl, noindex
dev/README.md                             Couchbase section
backend/dbaguy_backend/
  domain/dialect.py                       Dialect.SQLPP
  domain/classification.py                SYSTEM_KEYSPACE, MERGE_DELETE
  domain/execution.py                     QueryContext (text/parse), StatementParams, Presentation; command fields;
                                          ResultSetEvent.presentation
  domain/column_category.py               MIXED
  domain/metadata.py                      ServerInfo.edition / default_query_context; NodeKind BUCKET/SCOPE/COLLECTION
  domain/schema.py                        SnapshotKeyspace, SchemaSnapshot.keyspaces
  domain/profiles.py                      EngineInfo: transactions, query_context, parameters, documents,
                                          executed_plan, plan_format
  domain/errors.py                        UnsupportedFormat
  sql/dialects.py                         SQLPP rules, reserved words, JSON string literals, keyspace_path, in_list,
                                          KEYWORD_ONLY_DIALECTS, CASE_SENSITIVE_IDENTIFIERS, is_reserved
  sql/lexer.py                            back-tick backslash escapes, $name/@name/$1/? parameters
  sql/splitter.py                         BodyRule.NONE; StatementSpan.params
  sql/classifier.py                       _classify_sqlpp, is_transaction_statement, _unfiltered_dml(filters)
  sql/completion.py                       SQLPP keywords
  sql/format_guard.py                     (new) first_difference(): formatter token guard, every dialect
  sql/formatting.py                       JSON / MISSING cells, MIXED, SQL++ IN list, unsupported_reason,
                                          open_raw_json_writer
  sql/offsets.py                          utf16_column()
  adapters/base.py                        RunOptions, RawJsonRows, presentation(), capability fields,
                                          run(sql, options), endpoint_target()
  adapters/values.py                      JsonText, MISSING
  adapters/sockets.py                     (new) enable_keepalive() (moved out of mysql/adapter.py)
  adapters/{postgres,mysql,mssql,sqlite}/adapter.py   run(sql, options=None)
  adapters/couchbase/__init__.py          (new, empty)
  adapters/couchbase/jsontext.py          (new) one_line()
  adapters/couchbase/stream.py            (new) incremental response parser, split_object, json_kind (pure)
  adapters/couchbase/http.py              (new) body assembly, HttpChannel, QueryResponse, NodeClient
  adapters/couchbase/rows.py              (new) signature → columns, * and alias expansion, overflow, cells (pure)
  adapters/couchbase/messages.py          (new) errors with positions, warnings, metrics (pure)
  adapters/couchbase/settings.py          (new) seeds, ports, TLS context, options, URL import, endpoint target
  adapters/couchbase/nodes.py             (new) nodeServices discovery and candidate order
  adapters/couchbase/catalog.py           (new) tree, generated DDL, snapshot over a Query callable
  adapters/couchbase/result.py            (new) EventStream, start_result, CouchbaseResult
  adapters/couchbase/adapter.py           (new) CouchbaseAdapter, CouchbaseConnection
  services/connection_service.py          adapter.endpoint_target()
  sessions/options.py                     (new) run_options(): capability and parameter checks
  sessions/worker.py                      RunOptions, presentation, idle close of paused results, raw export,
                                          query context in ExecutionRecord, injectable clock
  sessions/hooks.py                       capabilities in WorkerHooks; history database = query context text
  sessions/manager.py                     check_paused_results()
  container.py                            CouchbaseAdapter registered; housekeeping closes idle paused results
  api/sql.py                              StatementRange.params; POST /sql/format-guard
backend/tests/support.py                  Couchbase constants, couchbase_query helpers, requires_couchbase, profile
backend/tests/couchbase_fakes.py          (new) FakeQueryServer, FakeResponse
backend/tests/…                           see each task
src/features/editor/extensions.ts         SQLPP CodeMirror dialect
src/features/editor/format.ts             sqlpp → n1ql; /sql/format-guard before replacing
src/lib/cells.ts, src/features/grid/{aggregate.ts,ValueViewer.tsx,RecordView.tsx,GridRow.tsx,copy.ts,GridMenu.tsx,
  DataGrid.tsx,ExportDialog.tsx}           missing / json cells, MIXED, copy formats per dialect
src/features/query/{QueryTab.tsx,QueryToolbar.tsx,useTransactionControls.ts,reasons.ts}   transactions hidden
src/features/session/SessionController.ts the session's default query context on execute and export
src/features/connections/ConnectionDialog.tsx   Couchbase labels, hints, default SSL
src/features/explorer/{kinds.tsx,Explorer.tsx}   bucket / scope / collection
CLAUDE.md, docs/research/2026-10-15-feature-priorities.md (a note in §7)
```

Task order follows dependencies. The dev cluster (1) comes first: its probes decide two values in later tasks (A7 → Task 2, A10/A11 → Tasks 7/11), and integration tests from Task 2 on need it. `Dialect.SQLPP` (2) must exist in every dialect table at once (the tripwires) and in the frontend's `Record<Dialect, …>`. The classifier (3) builds on its lexer. The shared contract (4) is engine-agnostic and touches every adapter; formatting (5) and the worker (6) build on its cell types and options. The Couchbase pieces then go from the wire up: HTTP and parser (7), row shaping and messages (8), settings and discovery (9), catalog (10), and the adapter (11) that wires them and registers the engine. The frontend (12) needs the regenerated types and a registered engine; packaging (13) verifies the bundle.

---
### Task 1: Dev Couchbase cluster, seed, test support and the Task 1 probes

**Files:**
- Modify: `dev/docker-compose.yml`, `dev/README.md`
- Create: `dev/couchbase/seed.sh` (mode 755), `dev/couchbase/init/01-keyspaces.n1ql`, `dev/couchbase/init/02-indexes.n1ql`, `dev/couchbase/init/03-data.n1ql`
- Modify: `backend/tests/support.py`, `backend/tests/test_dev_env.py`
- Modify: this plan's "Assumptions recorded during execution" table (Planning rulings)
- Scratchpad only, not committed: `<scratchpad>/t1_probe.py`

**Interfaces:**
- Consumes: the R-03b-P1 one-shot pattern (`dev/mssql/seed.sh`: idle ending, TERM trap, `run`).
- Produces, in `tests/support.py`:
  - `COUCHBASE_HOST = "127.0.0.1"`, `COUCHBASE_PORT = int(os.environ.get("DBAGUY_TEST_COUCHBASE_PORT", "8093"))`, `COUCHBASE_TLS_PORT = 18093`, `COUCHBASE_MANAGER_PORT = 8091`, `COUCHBASE_USER = "dbaguy"`, `COUCHBASE_PASSWORD = "dbaguy"`, `COUCHBASE_READER = "dbaguy_reader"`;
  - `couchbase_query_text(statement: str, params: dict[str, Any] | None = None, *, user: str = COUCHBASE_USER, password: str = COUCHBASE_PASSWORD, timeout: float = 60.0) -> str` — POSTs `{"statement": …, **params}` to `/query/service` with stdlib `http.client` (HTTP timeout `timeout` seconds; the import-time `_couchbase_ready()` uses 5, P48) and returns the raw response text;
  - `couchbase_query(statement, params=None, *, user=…, password=…, timeout=…) -> dict[str, Any]` — the same, parsed with `json.loads(…, parse_float=Decimal)`;
  - `couchbase_ca_file(directory: Path) -> Path` — writes the cluster CA(s) from `GET /pools/default/trustedCAs` (admin) to `directory / "couchbase-ca.pem"`;
  - `completed_request_state(client_context_id: str) -> str | None` — `state` of that request in `system:completed_requests` (admin), polled for up to 3 s;
  - `LONG_AGGREGATE = "SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 100000) AS b"` (minutes of server work, no rows until the end);
  - `COUCHBASE_AVAILABLE`, `requires_couchbase`, `requires_couchbase_bastion`;
  - `make_couchbase_profile(**overrides) -> ServerProfile` — engine `couchbase`, host `127.0.0.1`, `port=None` (so discovery runs, D-04-6), database `dbaguy.app`, username `dbaguy`, `SslConfig(mode="disable")`.
- Produces, in the seeded cluster (later tasks rely on every name):
  - buckets `travel-sample` (sample, with its indexes) and `dbaguy` (100 MB);
  - `dbaguy.app`: collections `users`, `orders`, `events`, `` `odd-name` ``, `` `value` ``, `blobs`, `noindex`; `dbaguy.many`: `c001` … `c300`;
  - indexes: `#primary` on `app.users`, `app.orders`, `app.events`; `idx_orders_amount ON app.orders(amount DESC)`; `idx_users_deferred ON app.users(name) WITH {"defer_build": true}` (state `deferred`); none on `app.noindex`;
  - `app.events`: 20,000 documents with keys `e100000` … `e119999` (key order = number order); every 7th (`i % 7 = 0`) has no `amount`; `i ≥ 1000 AND i % 1000 = 0` and not a multiple of 7 has an extra key `late` (first at `e101000`, row 1,001 in key order);
  - `app.users` (stored byte for byte through the console docs endpoint): `u1` … `u5` (mixed shapes), `u_numbers`, `u_keyorder` (a newline and odd spacing in its text), non-object `s1` (string), `a1` (array), `n1` (number); plus `u_big` (5 MiB string field) and `u_ttl` (expiration 4102444800 = 2100-01-01, ruling P8) through SQL++;
  - `app.orders`: `o0` … `o499`; `app.blobs`: `bin1` (binary), `txt1` (text, not JSON), `j1` (JSON) — `bin1` / `txt1` only if Step 5's image check shows `cbc` can write them (P40); `app.noindex`: `n_1`; `` app.`value` ``: `v1`; `` app.`odd-name` ``: `x1`;
  - `dbaguy._default._default::seed_done` (written last).

The exact document texts (the tests compare them byte for byte):

| id | text |
|---|---|
| `u_numbers` | `{"big":9007199254740993,"i64max":9223372036854775807,"i64min":-9223372036854775808,"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400,"huge":1e400,"half":0.5}` |
| `u_keyorder` | `{"zeta": 1,`⏎` "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}` (⏎ = one newline) |
| `u1` | `{"name":"Ann","age":31,"tags":["a","b"],"address":{"city":"Taipei","zip":"100"}}` |
| `u2` | `{"name":"Bob","age":null,"address":{"city":"Tainan"}}` |
| `u3` | `{"title":"no name field","nested":{"deep":{"deeper":[1,{"x":null}]}}}` |
| `u4` | `{"name":"Dee","value":true,"select":1,"odd key":"needs quoting"}` |
| `u5` | `{"name":"Eve","age":28,"tags":[]}` |
| `s1` / `a1` / `n1` | `"just a string document"` / `[1,2,3]` / `42` |

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_dev_env.py`:

```python
from tests.support import COUCHBASE_READER, couchbase_query, couchbase_query_text, requires_couchbase

U_NUMBERS = (
    '{"big":9007199254740993,"i64max":9223372036854775807,"i64min":-9223372036854775808,'
    '"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,'
    '"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400,"huge":1e400,"half":0.5}'
)


@requires_couchbase
def test_dev_couchbase_is_seeded():
    def one(statement, **kw):
        return couchbase_query(statement, **kw)["results"][0]

    assert one("SELECT RAW COUNT(*) FROM dbaguy.app.events") == 20_000
    assert one('SELECT RAW COUNT(*) FROM system:keyspaces AS k WHERE k.`bucket` = "dbaguy" AND k.`scope` = "many"') == 300
    assert one('SELECT RAW i.state FROM system:indexes AS i WHERE i.name = "idx_users_deferred"') == "deferred"
    assert one('SELECT RAW d FROM dbaguy.app.events AS d USE KEYS "e101000"')["late"]
    # Stored byte for byte, and passed through unchanged by SELECT RAW (survey §4)
    assert U_NUMBERS in couchbase_query_text('SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    types = couchbase_query('SELECT RAW META(d).type FROM dbaguy.app.blobs AS d USE KEYS ["bin1", "txt1", "j1"]')
    assert sorted(types["results"]) == ["base64", "base64", "json"]  # A20
    assert one('SELECT RAW META(d).expiration FROM dbaguy.app.users AS d USE KEYS "u_ttl"') == 4102444800  # A21
    assert one('SELECT RAW LENGTH(d.blob) FROM dbaguy.app.users AS d USE KEYS "u_big"') == 5 * 1024 * 1024
    # No `namespace` predicate (P35): every row is in the one data namespace `default`, so there is nothing to filter
    # (the 7.x rows carry both `namespace` and `namespace_id`, A22).
    reader = couchbase_query("SELECT RAW k.name FROM system:keyspaces AS k", user=COUCHBASE_READER)
    assert reader["results"] == ["users"]  # the limited user sees only its own keyspace (survey §5.1)
```

(If Step 5's image check drops `bin1` / `txt1`, or Step 8 drops `u_ttl`, delete the matching A20 / A21 assertion from this test; P40.)

- [ ] **Step 2: Run it and see it fail**

Run: `cd backend && .venv/bin/pytest tests/test_dev_env.py -q`
Expected: collection error `ImportError: cannot import name 'COUCHBASE_READER' from 'tests.support'`.

- [ ] **Step 3: Add the support helpers** to `backend/tests/support.py` (imports: `base64`, `http.client`, `json`, `time`, `from decimal import Decimal`, `from pathlib import Path`, `from typing import Any`):

```python
COUCHBASE_HOST = "127.0.0.1"
COUCHBASE_PORT = int(os.environ.get("DBAGUY_TEST_COUCHBASE_PORT", "8093"))
COUCHBASE_TLS_PORT = 18093
COUCHBASE_MANAGER_PORT = 8091
COUCHBASE_USER = "dbaguy"
COUCHBASE_PASSWORD = "dbaguy"
COUCHBASE_READER = "dbaguy_reader"  # query_select + data_reader on dbaguy.app.users only
# Minutes of server work and no row until the end: what a cancel must stop (spec §1 success criteria).
LONG_AGGREGATE = "SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 100000) AS b"


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def couchbase_query_text(
    statement: str, params: dict[str, Any] | None = None, *, user: str = COUCHBASE_USER,
    password: str = COUCHBASE_PASSWORD, timeout: float = 60.0,
) -> str:
    """One SQL++ request to the dev cluster's query service, answered as the raw response text. `timeout` (seconds) is
    the HTTP timeout: the import-time readiness check uses a short one (P48)."""
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_PORT, timeout=timeout)
    try:
        body = json.dumps({"statement": statement, **(params or {})})
        conn.request("POST", "/query/service", body=body,
                     headers={"Authorization": _basic(user, password), "Content-Type": "application/json"})
        return conn.getresponse().read().decode("utf-8")
    finally:
        conn.close()


def couchbase_query(statement: str, params: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    """`options`: `user`, `password`, `timeout` of couchbase_query_text."""
    return json.loads(couchbase_query_text(statement, params, **options), parse_float=Decimal)


def couchbase_ca_file(directory: Path) -> Path:
    """The cluster's CA certificates, fetched now (none is committed), as a PEM file in `directory`."""
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_MANAGER_PORT, timeout=10)
    try:
        conn.request("GET", "/pools/default/trustedCAs",
                     headers={"Authorization": _basic(COUCHBASE_USER, COUCHBASE_PASSWORD)})
        cas = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    path = directory / "couchbase-ca.pem"
    path.write_text("".join(ca["pem"] for ca in cas))
    return path


def completed_request_state(client_context_id: str) -> str | None:
    """How the server ended a request (`closed`, `stopped`, `completed`, …), from system:completed_requests. The log
    holds requests over 1 s (and failed ones), and an entry can take a moment to appear: polled for up to 3 s."""
    statement = "SELECT RAW r.state FROM system:completed_requests AS r WHERE r.clientContextID = $id"
    for _ in range(30):
        found = couchbase_query(statement, {"$id": client_context_id})["results"]
        if found:
            return found[0]
        time.sleep(0.1)
    return None


def _couchbase_ready() -> bool:
    if not _port_open(COUCHBASE_PORT):
        return False
    try:
        # A short timeout (P48): this runs at import, and a slow query service must not stall every pytest start for 60 s
        done = couchbase_query('SELECT RAW COUNT(*) FROM dbaguy._default._default USE KEYS "seed_done"', timeout=5)
        return done.get("results") == [1]
    except (OSError, ValueError, http.client.HTTPException):
        return False
```

Then, next to the other flags: `COUCHBASE_AVAILABLE = _couchbase_ready()`, `requires_couchbase = pytest.mark.skipif(not COUCHBASE_AVAILABLE, reason=SKIP_REASON)`, `requires_couchbase_bastion = pytest.mark.skipif(not (COUCHBASE_AVAILABLE and BASTION_AVAILABLE), reason=SKIP_REASON)`, and:

```python
def make_couchbase_profile(**overrides) -> ServerProfile:
    data = {
        "id": uuid4(),
        "name": "dev couchbase",
        "engine": "couchbase",
        "host": COUCHBASE_HOST,
        "port": None,  # the default query port: node discovery runs (D-04-6)
        "database": "dbaguy.app",
        "username": COUCHBASE_USER,
        "ssl": SslConfig(mode="disable"),
    }
    data.update(overrides)
    return ServerProfile(**data)
```

- [ ] **Step 4: Add the compose services.** Add to `dev/docker-compose.yml` (and `- couchbase` to the bastion's `depends_on`):

```yaml
  couchbase:
    image: couchbase/server:enterprise-7.6.12  # free for development and testing (spec R-04-3); arm64
    ports:  # same numbers inside and out: nodeServices reports the server's own ports (spec §6.1); no KV port
      - "127.0.0.1:8091:8091"
      - "127.0.0.1:8093:8093"
      - "127.0.0.1:18091:18091"
      - "127.0.0.1:18093:18093"
    healthcheck:
      # Healthy means "seeded": `up --wait` does not wait for the one-shot couchbase-init, so this looks for the seed's
      # last document (resultCount is 1 only when it exists). USE KEYS needs no index.
      test: ["CMD-SHELL", "curl -s -u dbaguy:dbaguy http://127.0.0.1:8093/query/service --data-urlencode 'statement=SELECT RAW 1 FROM dbaguy._default._default USE KEYS \"seed_done\"' | grep -Eq '\"resultCount\": *1[^0-9]'"]
      interval: 5s
      timeout: 5s
      retries: 120
      start_period: 60s

  couchbase-init:
    image: couchbase/server:enterprise-7.6.12
    depends_on:
      - couchbase
    volumes:
      - ./couchbase:/couchbase:ro
    entrypoint: ["/bin/bash", "/couchbase/seed.sh"]  # seeds once, then stays up idle (R-03b-P1)
    restart: "no"
```

- [ ] **Step 5: Check the image, then write `dev/couchbase/seed.sh`** exactly as below (mode 755). The script relies on `curl`, `cbq`, `cbc` and `python3` in the image, which the pre-flight review could not confirm (P40), so check first: `docker run --rm --entrypoint sh couchbase/server:enterprise-7.6.12 -c 'command -v curl cbq cbc python3; cbc create --help'`.
  - `curl` or `cbq` missing: stop and report; the seed cannot be written as planned.
  - `python3` missing: add `-d pretty=false` to the `curl` in `q()` (one-line JSON) and define `value()` as below, which takes the first scalar of `"results"` (every `value` call of the seed is a `SELECT RAW COUNT(*)`):

    ```bash
    value() { q "$1" | sed -n 's/.*"results":\[\([^],]*\)[],].*/\1/p'; }
    ```

  - `cbc` missing, or `cbc create --help` shows no `--scope` / `--collection`: drop `bin1` and `txt1` from 04a. Remove the `blob` function, the two `printf` lines and the two `blob` calls from the seed, and the A20 assertion from Step 1's test (nothing else uses them); record A20 as "not seeded" in Step 10 and add a row to "Carries to 04b and 04c" for 04c (binary and text documents).
  - `u_ttl` stays on the SQL++ `OPTIONS` path whatever the check shows: there is no `cbc` fallback for it (Step 8).

```bash
#!/bin/bash
# Seeds the dev Couchbase cluster (the couchbase-init container, same image as `couchbase`): initialises the one-node
# cluster, installs travel-sample, creates the dbaguy bucket with its scopes, collections, indexes and documents, and
# the limited user. Fake credentials only. Every step can run again (IF NOT EXISTS, UPSERT, "exists" checks), so a seed
# that stopped half-way is finished by the next `up`. The last step writes dbaguy._default._default::seed_done, which
# the `couchbase` healthcheck waits for.
# Both successful paths end idle instead of exiting: `docker compose up --wait` fails once a one-shot service has
# exited (R-03b-P1). TERM ends the container at once in every phase.
set -euo pipefail
trap 'exit 0' TERM
# bash runs a pending trap only after the foreground command has ended, so slow commands run in the background and are
# waited for. Not for commands that read stdin: a background command reads /dev/null.
run() { "$@" & wait $!; }
idle() { sleep infinity & wait; }
MGR=http://couchbase:8091
QRY=http://couchbase:8093/query/service
CURL=(curl -sS -u dbaguy:dbaguy)
CBQ=(cbq -e http://couchbase:8093 -u dbaguy -p dbaguy -q -exit-on-error)

# q STATEMENT: one SQL++ statement; anything but status "success" stops the seed (printed to stderr).
q() {
  local out
  out="$(run "${CURL[@]}" "$QRY" --data-urlencode "statement=$1")" || { echo "$out" >&2; return 1; }
  if ! grep -q '"status": *"success"' <<<"$out"; then echo "$1" >&2; echo "$out" >&2; return 1; fi
  printf '%s' "$out"
}
# value STATEMENT: the first result of a SQL++ statement.
value() { q "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["results"][0])'; }
# wait_for SECONDS WHAT COMMAND…: retries COMMAND every 2 s.
wait_for() {
  local limit=$1 what=$2
  shift 2
  for _ in $(seq 1 $((limit / 2))); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    run sleep 2
  done
  echo "timed out waiting for $what" >&2
  return 1
}
# doc SCOPE COLLECTION ID JSON: stored byte for byte through the console's docs endpoint (SQL++ would sort the keys and
# normalise the numbers, survey §9.3).
doc() {
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/pools/default/buckets/dbaguy/scopes/$1/collections/$2/docs/$3" \
    --data-urlencode "value=$4"
}
# blob ID FLAGS: a non-JSON document in dbaguy.app.blobs, value read from /tmp/ID (cbc reads stdin: not through `run`).
blob() { cbc create -U couchbase://couchbase/dbaguy -u dbaguy -P dbaguy --scope app --collection blobs -f "$2" "$1" <"/tmp/$1"; }
seeded() { [[ "$(value 'SELECT RAW COUNT(*) FROM dbaguy._default._default USE KEYS "seed_done"')" == 1 ]]; }
sample_ready() {
  ! "${CURL[@]}" "$MGR/pools/default/tasks" | grep -q loadingSampleBucket &&
    [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE i.bucket_id = "travel-sample" OR i.keyspace_id = "travel-sample"')" -gt 0 ]] &&
    [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE (i.bucket_id = "travel-sample" OR i.keyspace_id = "travel-sample") AND i.state != "online"')" == 0 ]]
}
# Only the scopes this seed creates are counted (P36): a `_system` scope (7.6) or a `_default._default` row that carries
# bucket and scope fields would make an unrestricted count 308–310, the wait would time out and `up --wait` would fail.
collections_ready() {
  [[ "$(value 'SELECT RAW COUNT(*) FROM system:keyspaces AS k WHERE k.`bucket` = "dbaguy" AND k.`scope` IN ["app", "many"]')" == 307 ]]
}
indexes_ready() {
  [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE i.bucket_id = "dbaguy" AND i.scope_id = "app" AND i.state = "online"')" == 4 ]]
}

wait_for 300 "the cluster manager" curl -sf -o /dev/null "$MGR/ui/index.html"
if seeded 2>/dev/null; then
  echo "already seeded"
  idle
fi
if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default" 2>/dev/null; then
  run curl -sSf -o /dev/null -X POST "$MGR/clusterInit" -d hostname=127.0.0.1 -d services=kv,n1ql,index \
    -d memoryQuota=512 -d indexMemoryQuota=256 -d indexerStorageMode=plasma -d username=dbaguy -d password=dbaguy \
    -d port=SAME
fi
wait_for 120 "the query service" q 'SELECT 1'

if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default/buckets/travel-sample" 2>/dev/null; then
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/sampleBuckets/install" -d '["travel-sample"]'
fi
wait_for 300 "travel-sample and its indexes" sample_ready

if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default/buckets/dbaguy" 2>/dev/null; then
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/pools/default/buckets" -d name=dbaguy -d ramQuota=100 \
    -d bucketType=couchbase
fi
wait_for 120 "the dbaguy bucket" q 'CREATE SCOPE dbaguy.app IF NOT EXISTS'
run "${CBQ[@]}" -f /couchbase/init/01-keyspaces.n1ql
for i in $(seq -w 1 300); do echo "CREATE COLLECTION dbaguy.many.c$i IF NOT EXISTS;"; done >/tmp/many.n1ql
run "${CBQ[@]}" -f /tmp/many.n1ql
wait_for 120 "the collections" collections_ready  # 300 in many + 7 in app
run "${CBQ[@]}" -f /couchbase/init/02-indexes.n1ql
wait_for 180 "the indexes" indexes_ready  # three primary indexes and idx_orders_amount; idx_users_deferred stays deferred

doc app users u_numbers '{"big":9007199254740993,"i64max":9223372036854775807,"i64min":-9223372036854775808,"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400,"huge":1e400,"half":0.5}'
doc app users u_keyorder '{"zeta": 1,
 "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'
doc app users u1 '{"name":"Ann","age":31,"tags":["a","b"],"address":{"city":"Taipei","zip":"100"}}'
doc app users u2 '{"name":"Bob","age":null,"address":{"city":"Tainan"}}'
doc app users u3 '{"title":"no name field","nested":{"deep":{"deeper":[1,{"x":null}]}}}'
doc app users u4 '{"name":"Dee","value":true,"select":1,"odd key":"needs quoting"}'
doc app users u5 '{"name":"Eve","age":28,"tags":[]}'
doc app users s1 '"just a string document"'
doc app users a1 '[1,2,3]'
doc app users n1 '42'
doc app blobs j1 '{"ok":true}'
doc app value v1 '{"value":1}'
doc app odd-name x1 '{"k":"v"}'
printf '\x00\x01\xffbinary' >/tmp/bin1
printf 'plain text, not JSON' >/tmp/txt1
blob bin1 0x03000002  # A20: binary and text documents (META().type "base64"); they trip INFER and ADVISE (survey §5.3)
blob txt1 0x04000004
run "${CBQ[@]}" -f /couchbase/init/03-data.n1ql

run "${CURL[@]}" -f -o /dev/null -X PUT "$MGR/settings/rbac/users/local/dbaguy_reader" -d password=dbaguy \
  --data-urlencode 'roles=query_select[dbaguy:app:users],data_reader[dbaguy:app:users]'
q 'UPSERT INTO dbaguy._default._default (KEY, VALUE) VALUES ("seed_done", {"at": NOW_STR()})' >/dev/null
echo "seeded"
idle
```

- [ ] **Step 6: Write the SQL++ files** exactly as below.

`IF NOT EXISTS` goes after the name in SQL++ 7.6 (`CREATE SCOPE b.s IF NOT EXISTS`, `CREATE COLLECTION b.s.c IF NOT EXISTS`, `CREATE INDEX i IF NOT EXISTS ON …`, `CREATE PRIMARY INDEX IF NOT EXISTS ON …`; checked in the 7.6 grammar while planning).

`dev/couchbase/init/01-keyspaces.n1ql`:

```sql
-- DbaGuy dev Couchbase (7.6.12 EE). Run by couchbase-init with cbq. Fake credentials only.
CREATE SCOPE dbaguy.app IF NOT EXISTS;
CREATE SCOPE dbaguy.many IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.users IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.orders IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.events IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.`odd-name` IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.`value` IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.blobs IF NOT EXISTS;
CREATE COLLECTION dbaguy.app.noindex IF NOT EXISTS;
```

`dev/couchbase/init/02-indexes.n1ql`:

```sql
CREATE PRIMARY INDEX IF NOT EXISTS ON dbaguy.app.users;
CREATE PRIMARY INDEX IF NOT EXISTS ON dbaguy.app.orders;
CREATE PRIMARY INDEX IF NOT EXISTS ON dbaguy.app.events;
CREATE INDEX idx_orders_amount IF NOT EXISTS ON dbaguy.app.orders(amount DESC);
-- Stays deferred: the tree shows the state and the generated DDL keeps the WITH clause.
CREATE INDEX idx_users_deferred IF NOT EXISTS ON dbaguy.app.users(name) WITH {"defer_build": true};
-- dbaguy.app.noindex has no index on purpose: read by a sequential scan (EE: needs query_use_sequential_scans).
```

`dev/couchbase/init/03-data.n1ql` (SQL++ writes sort the keys: these documents are about shapes and counts, not order):

```sql
-- 20,000 events, keys in number order (e100000…e119999). Every 7th has no amount (MISSING); from e101000 on, every
-- 1000th that is not a multiple of 7 has an extra key `late`, which the first page (500 rows) never shows.
UPSERT INTO dbaguy.app.events (KEY k, VALUE v)
SELECT "e" || TO_STRING(100000 + i) AS k,
       CASE WHEN i % 7 = 0 THEN {"id": i, "kind": "view", "at": MILLIS_TO_STR(1760000000000 + i * 1000)}
            WHEN i >= 1000 AND i % 1000 = 0
              THEN {"id": i, "kind": "buy", "amount": i * 1.5, "at": MILLIS_TO_STR(1760000000000 + i * 1000),
                    "late": {"note": "a key the first page does not show"}}
            ELSE {"id": i, "kind": "click", "amount": i * 1.5, "at": MILLIS_TO_STR(1760000000000 + i * 1000)}
       END AS v
FROM ARRAY_RANGE(0, 20000) AS i;
UPSERT INTO dbaguy.app.orders (KEY k, VALUE v)
SELECT "o" || TO_STRING(i) AS k,
       {"id": i, "user": "u" || TO_STRING(i % 5 + 1), "amount": i * 1.5,
        "status": CASE WHEN i % 3 = 0 THEN "closed" ELSE "open" END} AS v
FROM ARRAY_RANGE(0, 500) AS i;
UPSERT INTO dbaguy.app.users (KEY, VALUE) VALUES ("u_big", {"n": 1, "blob": REPEAT("x", 5242880)});
-- A21: an absolute expiry (2100-01-01): a relative TTL above 30 days is read as a Unix time, and a long-lived dev
-- cluster would lose a short one (ruling P8).
UPSERT INTO dbaguy.app.users (KEY, VALUE, OPTIONS) VALUES ("u_ttl", {"name": "Expires in 2100"}, {"expiration": 4102444800});
UPSERT INTO dbaguy.app.noindex (KEY, VALUE) VALUES ("n_1", {"note": "no index: read by a sequential scan"});
```

- [ ] **Step 7: Document it** in `dev/README.md`: a "Couchbase" section — Enterprise 7.6.12 (free for development and testing), ports 8091/8093/18091/18093 on 127.0.0.1 (same numbers inside, because `nodeServices` reports the server's own ports), admin `dbaguy`/`dbaguy`, limited user `dbaguy_reader`/`dbaguy`, about 1.2 GiB; the seed runs in `couchbase-init` (`couchbase/seed.sh` + `couchbase/init/*.n1ql`), takes 1–2 minutes the first time, skips when `seed_done` exists and then stays up idle; every step is re-runnable, so a seed that stopped half-way is finished by the next `up`; the TLS CA is not committed (tests fetch it from `/pools/default/trustedCAs`); to start from an empty cluster: `docker compose -f dev/docker-compose.yml rm -sfv couchbase couchbase-init` then `up -d --build --wait`. Mention the console at http://127.0.0.1:8091.

- [ ] **Step 8: Start the cluster and run the tests**

Run: `docker compose -f dev/docker-compose.yml up -d --build --wait && cd backend && .venv/bin/pytest -q`
Expected: all green, `test_dev_couchbase_is_seeded` passes (not skipped). Then run the `up … --wait` command a second time and check that it exits 0 (R-03b-P1). A first pull of the image (0.9 GB) can exceed `--wait`'s patience: run the command again. Note the seed's duration (`docker logs dbaguy-dev-couchbase-init-1` timestamps) and `docker stats --no-stream dbaguy-dev-couchbase-1` memory for CLAUDE.md (Task 13).
If `cbq -exit-on-error` turns out not to stop on an error (check with `docker exec dbaguy-dev-couchbase-init-1 cbq -e http://couchbase:8093 -u dbaguy -p dbaguy -q -exit-on-error -s 'SELEC 1'; echo $?`), replace each `run "${CBQ[@]}" -f FILE` with a loop over the file's `;`-terminated statements through `q` (the files hold no `;` inside strings).
**A20** fails (a type is not `base64`) → write `bin1` / `txt1` with `cbc create … -f 0` and record the flags that work; a `blob` call that cannot connect or write at all (the node is named `127.0.0.1`, so `cbc` has to reach the data service at `couchbase:11210` through the cluster map) → drop `bin1` / `txt1` as in Step 5 (P40). **A21** fails (an error on the `OPTIONS` form, or another expiration) → `u_ttl` stays on the SQL++ path and `cbc` is no fallback (P40): remove that statement from `03-data.n1ql` and the A21 assertion from Step 1's test, record A21 as "not seeded" and add a row to "Carries to 04b and 04c" for 04c. Record either outcome in the assumptions table.

- [ ] **Step 9: Run the Task 1 probes** with the cluster up. Save as `<scratchpad>/t1_probe.py` (stdlib only) and run `backend/.venv/bin/python -I <scratchpad>/t1_probe.py`:

```python
"""Task 1 probes of plan 04a (A1, A6, A7, A10, A11, A15, A22) against the dev cluster. Fake dev credentials only."""
import base64, http.client, json, socket, ssl, tempfile, threading, time, uuid

HOST, AUTH = "127.0.0.1", "Basic " + base64.b64encode(b"dbaguy:dbaguy").decode()
HEADERS = {"Authorization": AUTH, "Content-Type": "application/json"}
LONG = "SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 100000) AS b"


def post(statement, **params):
    conn = http.client.HTTPConnection(HOST, 8093, timeout=60)
    conn.request("POST", "/query/service", body=json.dumps({"statement": statement, **params}), headers=HEADERS)
    return conn.getresponse().read().decode()


def ca_file():
    conn = http.client.HTTPConnection(HOST, 8091, timeout=10)
    conn.request("GET", "/pools/default/trustedCAs", headers={"Authorization": AUTH})
    path = tempfile.mktemp(suffix=".pem")
    open(path, "w").write("".join(ca["pem"] for ca in json.loads(conn.getresponse().read())))
    return path


print("A1", post('SELECT META(d).id, * FROM dbaguy.app.events AS d LIMIT 2')[:600])
print("A6", post("SELECT 1 AS a -- c\r, 2 AS b")[:400])
for name, ch in [("NBSP", "\u00a0"), ("EM SPACE", "\u2003"), ("IDEOGRAPHIC", "\u3000"), ("ZWSP", "\u200b"),
                 ("BOM", "\ufeff")]:  # written as escapes: these characters are invisible in a source file (P43)
    print("A7", name, json.loads(post(f"SELECT{ch}1 AS a")).get("status"))
for statement in ("EXPLAIN SELECT * FROM dbaguy.app.users", "ADVISE SELECT * FROM dbaguy.app.users WHERE name = 'x'"):
    print("A15", statement.split()[0], json.dumps(json.loads(post(statement)).get("signature")))
print("A22 keyspaces", post("SELECT * FROM system:keyspaces LIMIT 3")[:1500])  # the field names (P35)
print("A22 indexes", post("SELECT * FROM system:indexes LIMIT 1")[:1500])

ca = ca_file()
full = ssl.create_default_context(cafile=ca)
try:
    with socket.create_connection((HOST, 18093), timeout=5) as raw, full.wrap_socket(raw, server_hostname=HOST) as tls:
        print("A11 verify-full against 127.0.0.1: ok")
except ssl.SSLError as exc:
    print("A11 verify-full against 127.0.0.1: FAILED", exc)
loose = ssl.create_default_context(cafile=ca)
loose.check_hostname = False
with socket.create_connection((HOST, 18093), timeout=5) as raw, loose.wrap_socket(raw, server_hostname=HOST) as tls:
    print("A11 subjectAltName", tls.getpeercert().get("subjectAltName"))


class Tls(http.client.HTTPConnection):  # the design of Task 7: a dup of the TCP socket kept for shutdown()
    def connect(self):
        raw = socket.create_connection((HOST, 18093), timeout=10)
        self.handle = raw.dup()
        self.sock = loose.wrap_socket(raw, server_hostname=HOST)
        self.sock.settimeout(20)  # not None (P42): a read that survives the shutdown ends in TimeoutError, not a hang


ccid = "t1-a10-" + uuid.uuid4().hex[:8]
conn = Tls(HOST, 18093)
conn.request("POST", "/query/service", body=json.dumps({"statement": LONG, "client_context_id": ccid}), headers=HEADERS)
response = conn.getresponse()
shut = []
threading.Timer(2.0, lambda: (shut.append(time.monotonic()), conn.handle.shutdown(socket.SHUT_RDWR))).start()
try:
    response.read()
    outcome = "read returned"
except Exception as exc:  # noqa: BLE001 - recording what the reader sees
    outcome = type(exc).__name__
print("A10 reader:", outcome, f"{(time.monotonic() - shut[0]) * 1000:.0f} ms after the shutdown")
time.sleep(1)
print("A10 server state:", post("SELECT RAW r.state FROM system:completed_requests AS r WHERE r.clientContextID = $id",
                                **{"$id": ccid})[:300])
```

Expected (recorded, not asserted): A1 rows `{"id":"e100000","d":{…}}`; A6 shows whether `b` is in the result; A7 one status per character; A10 the reader ends within about 100 ms and the state is `closed` (the read ending only with `TimeoutError`, about 20 s after the shutdown, means shutting down the TCP duplicate did not end the blocked TLS read: A10 failed, and the spec §9 fallback of Tasks 7 and 11 applies, P41); A11 `ok` or the verification error plus the SAN list; A15 the two signatures; A22 the field names of `system:keyspaces` and `system:indexes` rows (P35).

- [ ] **Step 10: Record the results** in this plan's "Assumptions recorded during execution" table (Planning rulings): one row each for A1, A6, A7, A10, A11, A15, A20, A21, A22 — the observed result in a few words and the effect on later tasks, exactly as that table's "Effect" column describes for each outcome.

- [ ] **Step 11: Commit**

```bash
git add dev/docker-compose.yml dev/README.md dev/couchbase backend/tests/support.py backend/tests/test_dev_env.py \
  docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md
git commit -m "chore(dev): Couchbase 7.6.12 EE dev cluster seeded by an init container, test support, Task 1 probes"
```

---

### Task 2: `Dialect.SQLPP`: lexer, splitter parameters, dialect tables, the formatter and its token guard

**Files:**
- Modify: `backend/dbaguy_backend/domain/dialect.py`
- Modify: `backend/dbaguy_backend/sql/dialects.py`, `backend/dbaguy_backend/sql/lexer.py`, `backend/dbaguy_backend/sql/splitter.py`, `backend/dbaguy_backend/sql/completion.py`
- Modify: `backend/dbaguy_backend/sql/classifier.py` (only `_EXPLAIN_KEYWORDS`, `_from_sqlglot`, `warm_up`)
- Create: `backend/dbaguy_backend/sql/format_guard.py`
- Modify: `backend/dbaguy_backend/api/sql.py`
- Modify: `src/features/editor/extensions.ts`, `src/features/editor/format.ts`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Test: `backend/tests/sql/test_lexer.py`, `test_splitter.py`, `test_dialects_offsets.py`, `test_classifier.py`, `test_completion.py`; create `backend/tests/sql/test_format_guard.py`, `backend/tests/test_couchbase_literals.py`; modify `backend/tests/test_sql_api.py`

**Interfaces:**
- Consumes: 03b's `LexRules`, `BodyRule`, `RULES`, every `_…` dialect table, the tripwire `test_every_dialect_has_an_entry_in_every_dialect_table`; Task 1's `couchbase_query`, `requires_couchbase`; the A7 result recorded by Task 1.
- Produces:
  - `Dialect.SQLPP = "sqlpp"`; `BodyRule.NONE = "none"` (no statement holds a body: `;` always ends one).
  - `LexRules` gains three last fields with defaults: `identifier_backslash_escapes: bool = False` (SQL++: `` \` `` inside back-ticks), `named_param_prefixes: frozenset[str] = frozenset()` (SQL++ `$` and `@`: `$name`, `@name`, `$1` are one PARAM token), `question_params: bool = False` (SQL++ `?` is a PARAM).
  - `RULES[Dialect.SQLPP]`, `SQLPP_RESERVED` (the 225 words of the 7.6 list, lower case), SQLPP entries in every table; `_BYTES_LITERALS` and `_JSON_LITERALS` become `dict[Dialect, str | None]` (None: SQL++ has no binary literal, and its JSON literal is the JSON text itself).
  - New tables: `_JSON_STRINGS: dict[Dialect, bool]` (SQL++: a string literal is a JSON string), `_IN_LIST_BRACKETS: dict[Dialect, tuple[str, str]]` (SQL++: `[`, `]`).
  - New functions in `sql/dialects.py`: `is_reserved(dialect: Dialect, word: str) -> bool`; `keyspace_path(bucket: str, scope: str | None = None, collection: str | None = None) -> str` (each part through `quote_ident(Dialect.SQLPP, …)`); `in_list(dialect: Dialect, literals: Iterable[str]) -> str`.
  - New constants: `KEYWORD_ONLY_DIALECTS: frozenset[Dialect] = frozenset({Dialect.SQLPP})` (no sqlglot dialect exists: keyword rules only); `CASE_SENSITIVE_IDENTIFIERS: frozenset[Dialect] = frozenset({Dialect.SQLPP})`.
  - `quote_literal(SQLPP, v) == json.dumps(v, ensure_ascii=False)`; `force_quote_ident(SQLPP, n)` doubles `\` and `` ` ``; `bool_literal` → `TRUE`/`FALSE`; `json_literal(SQLPP, t) == t`; `bytes_literal(SQLPP, …)` raises `ValueError`.
  - `StatementSpan.params: tuple[str, ...] = ()` (last field): the statement's parameters in order of first use — `$name` for `$name` and `@name`, `$1`…, and every `?` (each `?` is the next positional argument); filled only for dialects with `named_param_prefixes` or `question_params`.
  - `POST /sql/split` → `StatementRange.params: list[str]`.
  - `sql/format_guard.py`: `first_difference(dialect: Dialect, original: str, formatted: str) -> int | None` — code-point offset in `original` of the first significant token the formatter changed; None when they differ only in whitespace, word case (SQL++: reserved words and function names only) and comment layout.
  - `POST /sql/format-guard` with `FormatGuardRequest{dialect, original, formatted}` → `FormatGuardResponse{same: bool, offset: int | None}` (UTF-16, in `original`).
  - `STATEMENT_KEYWORDS[SQLPP]`, `CLAUSE_KEYWORDS[SQLPP]`; `classifier._EXPLAIN_KEYWORDS[SQLPP] = ()`; `_from_sqlglot()` returns None for `KEYWORD_ONLY_DIALECTS` without logging; `warm_up()` skips them.
  - Frontend: `SQL_DIALECTS.sqlpp` (a `SQLDialect.define`), `LANGUAGES.sqlpp = "n1ql"`, and `formatSql` replaces the text only when `/sql/format-guard` says `same`.
- Breaks on purpose (updated here): `tests/test_sql_api.py` split ranges gain `"params": []`; `test_every_dialect_parses_through_sqlglot_resolved_by_import` and `test_dialects_survive_a_bundle_without_sqlglots_by_name_lookups` iterate only dialects outside `KEYWORD_ONLY_DIALECTS`; `test_warm_up_parses_once_per_dialect_and_logs_its_duration` asserts `len(calls) == len(Dialect) - len(KEYWORD_ONLY_DIALECTS)` (`warm_up()` skips SQL++; P34); the tripwire checks `SQLGLOT_DIALECT` only for them. Between this task and Task 3, SQL++ is classified by the generic keyword rules (`_lexical`); Task 3 replaces that before anything ships (ruling P22).

- [ ] **Step 1: Write the failing tests.**

`tests/sql/test_lexer.py`: the constants line becomes `PG, LITE, MY, TS, SQ = Dialect.POSTGRES, Dialect.SQLITE, Dialect.MYSQL, Dialect.TSQL, Dialect.SQLPP`. Append:

```python
def test_sqlpp_strings_names_and_parameters():
    text = r"""'it''s;' "a\"b;" 'c\'d' `a``b;` `c\`d;` $x @y $1 ? $ x$y"""
    assert kinds(text, SQ) == [
        (K.STRING, "'it''s;'"), (K.STRING, r'"a\"b;"'), (K.STRING, r"'c\'d'"), (K.QUOTED, "`a``b;`"),
        (K.QUOTED, r"`c\`d;`"), (K.PARAM, "$x"), (K.PARAM, "@y"), (K.PARAM, "$1"), (K.PARAM, "?"),
        (K.OTHER, "$"), (K.WORD, "x$y"),
    ]
    assert kinds("? $x", PG) == [(K.OTHER, "?"), (K.OTHER, "$"), (K.WORD, "x")]  # other dialects unchanged


def test_sqlpp_comments():
    assert kinds("SELECT 1--x", SQ)[-1] == (K.COMMENT, "--x")  # no space needed after --
    assert kinds("-- c\r; x", SQ) == [(K.COMMENT, "-- c"), (K.SEMI, ";"), (K.WORD, "x")]  # CR ends it (A6: safe side)
    assert kinds("/* a /* b */ c */", SQ)[0] == (K.COMMENT, "/* a /* b */")  # not nested
    assert kinds("# not a comment", SQ)[0] == (K.OTHER, "#")
    assert kinds("/*+ ordered */ SELECT", SQ)[0] == (K.COMMENT, "/*+ ordered */")  # a hint is a plain comment
```

and, depending on A7 as Task 1 recorded it, **one** of these two (U+00A0 is written as the escape `\u00a0`: a literal no-break space is invisible in a source file, P43):

```python
def test_sqlpp_unicode_spaces_follow_the_server():  # A7: the server reads U+00A0 as a space
    assert kinds("DELETE\u00a0FROM t", SQ) == [(K.WORD, "DELETE"), (K.WORD, "FROM"), (K.WORD, "t")]
```

```python
def test_sqlpp_unicode_spaces_follow_the_server():  # A7: the server rejects U+00A0 (syntax error, nothing runs)
    assert kinds("DELETE\u00a0FROM t", SQ) == [(K.WORD, "DELETE\u00a0FROM"), (K.WORD, "t")]
```

Add to `test_unterminated_tokens_are_flagged`: `("`abc", SQ)`, `(r"`a\`", SQ)`, `(r"'a\'", SQ)`, `('"abc', SQ)`. Add to `test_closed_tokens_are_terminated`: `(r"`a\`b`", SQ)`, `("`a``b`", SQ)`, `(r'"a\"b"', SQ)`, `("-- to the end", SQ)`.

`tests/sql/test_splitter.py` (`SQ = Dialect.SQLPP`):

```python
def test_sqlpp_splits_on_every_semicolon_outside_tokens():
    sql = (
        "SELECT {\"a\": [1, 2]} AS o, ARRAY v FOR v IN [1] END AS x FROM `a;b` WHERE s = 'x;y' AND t = \"u;v\";"
        " /* ; */ UPDATE ks SET a = 1 -- ;\n; INSERT INTO ks (KEY, VALUE) VALUES ('k', {\"v\": \";\"})"
    )
    assert [s.text for s in split(sql, SQ)] == [
        "SELECT {\"a\": [1, 2]} AS o, ARRAY v FOR v IN [1] END AS x FROM `a;b` WHERE s = 'x;y' AND t = \"u;v\"",
        "UPDATE ks SET a = 1",  # a span ends at its last significant token
        "INSERT INTO ks (KEY, VALUE) VALUES ('k', {\"v\": \";\"})",
    ]
    assert not any(s.has_body for s in split("CREATE FUNCTION f() { 1 }; SELECT f()", SQ))  # BodyRule.NONE


def test_sqlpp_spans_carry_their_parameters():
    first, second = split("SELECT $a, @a, $b, $1, ?, ?, '$c' FROM ks WHERE x = @b; SELECT `$d`, $e", SQ)
    assert first.params == ("$a", "$b", "$1", "?", "?")  # @a is $a; strings and names are not parameters
    assert second.params == ("$e",)
    assert split("SELECT $1", Dialect.POSTGRES)[0].params == ()  # only dialects with named or ? parameters
```

`tests/sql/test_dialects_offsets.py`: `SQ = Dialect.SQLPP`; rows for `test_quote_ident`: `(SQ, "users", "`users`")` (USERS is reserved), `(SQ, "orders", "orders")`, `(SQ, "travel-sample", "`travel-sample`")`, `(SQ, "value", "`value`")`, `(SQ, "Name", "Name")`, `(SQ, "a$b", "`a$b`")`, `(SQ, "中文", "`中文`")`, `(SQ, "_index_key", "`_index_key`")`, `(SQ, "a`b", "`a``b`")`, `(SQ, "a\\b", "`a\\\\b`")`. In the tripwire's `tables` dict add `"dialects._JSON_STRINGS": dialects._JSON_STRINGS`, `"dialects._IN_LIST_BRACKETS": dialects._IN_LIST_BRACKETS`; change the `missing` line to
`missing = [name for name, table in tables.items() if dialect not in table and not (name == "sqlglot_rules.SQLGLOT_DIALECT" and dialect in dialects.KEYWORD_ONLY_DIALECTS)]`. Append:

```python
def test_sqlpp_literals_and_paths():
    assert quote_literal(SQ, "it's \"中文\"\n\\") == '"it\'s \\"中文\\"\\n\\\\"'  # a JSON string (D-04-15)
    assert quote_literal(SQ, "\x00") == '"\\u0000"'
    assert [bool_literal(SQ, True), bool_literal(SQ, False)] == ["TRUE", "FALSE"]
    assert json_literal(SQ, '{"a": [1, 1e400]}') == '{"a": [1, 1e400]}'  # JSON is SQL++ syntax
    with pytest.raises(ValueError):
        bytes_literal(SQ, "dead")
    assert force_quote_ident(SQ, "a\\b`c") == "`a\\\\b``c`"
    assert keyspace_path("travel-sample", "inventory", "airline") == "`travel-sample`.inventory.airline"
    assert keyspace_path("dbaguy", "app") == "dbaguy.app"
    assert keyspace_path("my.bucket") == "`my.bucket`"
    assert in_list(SQ, ["1", '"a"']) == '[1, "a"]'
    assert in_list(Dialect.POSTGRES, ["1", "'a'"]) == "(1, 'a')"
    assert is_reserved(SQ, "Users") and not is_reserved(SQ, "name")
```

(Import `in_list`, `is_reserved`, `keyspace_path` with the others.)

`tests/sql/test_classifier.py`: import `KEYWORD_ONLY_DIALECTS` from `dbaguy_backend.sql.dialects`, and change the classifier import line to `from dbaguy_backend.sql.classifier import _from_sqlglot, classify, warm_up` (the new test below calls `_from_sqlglot`, which the file does not import yet; P34); `test_every_dialect_parses_through_sqlglot_resolved_by_import` gets `@pytest.mark.parametrize("dialect", [d for d in Dialect if d not in KEYWORD_ONLY_DIALECTS])`; in `test_dialects_survive_a_bundle_without_sqlglots_by_name_lookups` the subprocess code iterates `[d for d in Dialect if d not in KEYWORD_ONLY_DIALECTS]` (import it there too) and the assertion compares with `* (len(Dialect) - len(KEYWORD_ONLY_DIALECTS))`; in `test_warm_up_parses_once_per_dialect_and_logs_its_duration` the assertion `assert len(calls) == len(Dialect)` becomes `assert len(calls) == len(Dialect) - len(KEYWORD_ONLY_DIALECTS)` (P34). Append:

```python
def test_keyword_only_dialects_never_reach_sqlglot(monkeypatch):
    monkeypatch.setattr(sqlglot, "parse", lambda *a, **k: pytest.fail("sqlglot was asked about SQL++"))
    result, warnings = _warnings_while(lambda: _from_sqlglot("SELECT 1", Dialect.SQLPP))
    assert result is None and warnings == []
```

`tests/sql/test_completion.py`: add `"$x", "@y", "?", "`a\\`b`", "{", "}", "[", "]", "USE KEYS", "MISSING"` to `_FUZZ_PIECES`, and append:

```python
def test_sqlpp_keywords():
    labels = [i.label for i in complete("SELECT * FROM ks US", 19, Dialect.SQLPP, None).items]
    assert "USE KEYS" in labels
    assert {"UPSERT", "INFER", "ADVISE"} <= set(STATEMENT_KEYWORDS[Dialect.SQLPP])
    assert {"UNNEST", "NEST", "SATISFIES", "IS MISSING", "RETURNING"} <= set(CLAUSE_KEYWORDS[Dialect.SQLPP])
```

Create `tests/sql/test_format_guard.py`:

```python
import pytest

from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.format_guard import first_difference

SQ, PG = Dialect.SQLPP, Dialect.POSTGRES


@pytest.mark.parametrize("dialect, original, formatted", [
    (PG, "select a from t where b = 'x'", "SELECT\n  a\nFROM\n  t\nWHERE\n  b = 'x'"),
    (PG, "select Users.a from users", "SELECT USERS.a FROM users"),  # word case is free outside SQL++
    (SQ, "select meta(d).id, d from ks as d use keys $k", "SELECT\n  META(d).id,\n  d\nFROM\n  ks AS d USE KEYS $k"),
    (SQ, "/* a   note */ select 1", "/*\n  a note\n*/\nSELECT\n  1"),  # comments: layout only
])
def test_whitespace_keyword_case_and_comment_layout_are_the_same(dialect, original, formatted):
    assert first_difference(dialect, original, formatted) is None


def test_a_split_string_is_refused_with_its_offset():
    original = "SELECT 'it''s' AS a"
    assert first_difference(SQ, original, "SELECT\n  'it' 's' AS a") == original.index("'it")


def test_sqlpp_identifier_case_matters():
    assert first_difference(SQ, "SELECT d.type FROM ks d", "SELECT d.TYPE FROM ks d") == len("SELECT d.")


def test_extra_or_missing_tokens():
    assert first_difference(PG, "SELECT 1", "SELECT 1;") == len("SELECT 1")
    assert first_difference(PG, "SELECT 1, 2", "SELECT 1") == len("SELECT 1")
```

Create `tests/test_couchbase_literals.py` (A4, A5: what DbaGuy writes is what the server reads):

```python
import pytest

from dbaguy_backend.sql.dialects import Dialect, force_quote_ident, quote_literal
from tests.support import couchbase_query, requires_couchbase

pytestmark = requires_couchbase
SQ = Dialect.SQLPP


@pytest.mark.parametrize("value", [
    "it's", 'say "hi"', "back\\slash", "tab\tnew\nline\rcr", "\b\f", "\x00\x1f", "/", "中文😀", "",
])
def test_string_literals_round_trip(value):  # A4
    assert couchbase_query(f"SELECT RAW {quote_literal(SQ, value)}")["results"] == [value]


@pytest.mark.parametrize("name", ["a\\b", "a`b", "x.y", "value", "中文"])
def test_quoted_names_round_trip(name):  # A5: `\\` is one backslash, `` is one back-tick
    statement = f"SELECT RAW o.{force_quote_ident(SQ, name)} FROM [{{{quote_literal(SQ, name)}: 1}}] AS o"
    assert couchbase_query(statement)["results"] == [1]
```

`tests/test_sql_api.py`: the expected ranges at lines 40 and 48–49 gain `"params": []`; append:

```python
def test_split_returns_sqlpp_parameters(client):
    body = {"text": "SELECT $a, ? FROM ks; SELECT @a", "dialect": "sqlpp"}
    statements = client.post("/sql/split", json=body, headers=AUTH).json()["statements"]
    assert [s["params"] for s in statements] == [["$a", "?"], ["$a"]]


def test_format_guard_offsets_are_utf16(client):
    original = "SELECT '😀', 'it''s'"
    body = {"dialect": "sqlpp", "original": original, "formatted": "SELECT '😀', 'it' 's'"}
    assert client.post("/sql/format-guard", json=body, headers=AUTH).json() == {"same": False, "offset": 13}
    body["formatted"] = "SELECT\n  '😀',\n  'it''s'"
    assert client.post("/sql/format-guard", json=body, headers=AUTH).json() == {"same": True, "offset": None}
```

(The second string starts at code point 12, UTF-16 unit 13: 😀 is two units.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql tests/test_sql_api.py tests/test_couchbase_literals.py -q`
Expected: collection errors (`AttributeError: SQLPP`, `ImportError: format_guard`).

- [ ] **Step 3: Implement the dialect tables** in `sql/dialects.py`:
  - `BodyRule.NONE = "none"  # SQL++: no statement holds a body (JS UDF bodies are strings, inline ones {expr}); ";" always ends one`.
  - The three new `LexRules` fields, last, with the comments from Interfaces.
  - The rules:

```python
    Dialect.SQLPP: LexRules(  # Couchbase SQL++ 7.6 (survey §8.1; spec §3.3.1)
        nested_block_comments=False, line_comment_end=re.compile(r"[\n\r]"), dash_comment_needs_space=False,
        hash_comments=False, executable_comments=False, dollar_quotes=False, dollar_params=False,
        escape_string_prefixes=frozenset(), backslash_escapes=True, string_quotes=frozenset("'\""),  # "x" is a string
        identifier_quotes=(("`", "`"),), variable_prefixes=frozenset(), body_rule=BodyRule.NONE,
        unicode_spaces=False,  # A7 (Task 1): True when the server reads U+00A0 as a space
        identifier_backslash_escapes=True, named_param_prefixes=frozenset("$@"), question_params=True,
    ),
```

  Set `unicode_spaces=True` if Task 1 recorded that the server ran `SELECT 1` with U+00A0 between the two words (and keep the matching lexer test from Step 1).
  - `SQLPP_RESERVED` with exactly these 225 words (the 7.6 list, fetched while planning; quoting an extra word is harmless, as in the other dialects):

```python
SQLPP_RESERVED = frozenset(  # Couchbase SQL++ 7.6 reserved words (225), case-insensitive
    """_index_condition _index_key advise all alter analyze and any array as asc at begin between binary boolean break
    bucket build by cache call case cast cluster collate collection commit committed connect continue correlated cover
    create current cycle database dataset datastore declare decrement default delete derived desc describe distinct do
    drop each element else end escape every except exclude execute exists explain false fetch filter first flatten
    flatten_keys flush following for force from fts function golang grant group groups gsi hash having if ignore ilike
    in include increment index infer inline inner insert intersect into is isolation javascript join key keys keyspace
    known language last lateral left let letting level like limit lsm map mapping matched materialized maxvalue merge
    minvalue missing namespace nest next nextval nl no not nth_value null nulls number object offset on option options
    or order others outer over parse partition password path pool preceding prepare prev previous prevval primary
    private privilege probe procedure public range raw read realm recursive reduce rename replace respect restart
    restrict return returning revoke right role rollback row rows satisfies savepoint schema scope select self sequence
    set show some start statistics string system then ties to tran transaction trigger true truncate unbounded under
    union unique unknown unnest unset update upsert use user users using validate value valued values vector via view
    when where while window with within work xor""".split()
)
```

  - Entries: `_SIMPLE_IDENT[SQLPP] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")` (ASCII only, survey §8.1), `_RESERVED[SQLPP] = SQLPP_RESERVED`, `_IDENT_QUOTES[SQLPP] = ("`", "`")`, `_BOOL_LITERALS[SQLPP] = ("TRUE", "FALSE")`, `_BYTES_LITERALS[SQLPP] = None`, `_JSON_LITERALS[SQLPP] = None`, `_ZERO_DATE_AS_TEXT[SQLPP] = None`, `_STRING_PREFIXES[SQLPP] = ""`, `_ISO_DATETIME[SQLPP] = False`, `_NUL_IN_LITERAL[SQLPP] = None` (JSON writes `\u0000`).
  - New tables and functions:

```python
# SQL++ string literals are JSON strings (double quotes, JSON escapes; D-04-15); the other dialects quote with ''.
_JSON_STRINGS: dict[Dialect, bool] = {
    Dialect.POSTGRES: False, Dialect.SQLITE: False, Dialect.MYSQL: False, Dialect.TSQL: False, Dialect.SQLPP: True,
}
_IN_LIST_BRACKETS: dict[Dialect, tuple[str, str]] = {  # SQL++: `x IN [1, 2]` (an array; A19)
    Dialect.POSTGRES: ("(", ")"), Dialect.SQLITE: ("(", ")"), Dialect.MYSQL: ("(", ")"), Dialect.TSQL: ("(", ")"),
    Dialect.SQLPP: ("[", "]"),
}
# No sqlglot dialect exists for these: they are classified by keyword rules only (sqlglot_rules has no entry).
KEYWORD_ONLY_DIALECTS: frozenset[Dialect] = frozenset({Dialect.SQLPP})
# Unquoted identifiers keep their case: a formatter may not change it (format_guard).
CASE_SENSITIVE_IDENTIFIERS: frozenset[Dialect] = frozenset({Dialect.SQLPP})


def is_reserved(dialect: Dialect, word: str) -> bool:
    return word.lower() in _RESERVED[dialect]


def needs_quote(dialect: Dialect, name: str) -> bool:
    return not _SIMPLE_IDENT[dialect].fullmatch(name) or is_reserved(dialect, name)


def force_quote_ident(dialect: Dialect, name: str) -> str:
    """Always quoted, the closing quote doubled inside (SQL++ also doubles backslashes, which escape in back-ticks):
    for names interpolated into introspection SQL."""
    opening, closing = _IDENT_QUOTES[dialect]
    if RULES[dialect].identifier_backslash_escapes:
        name = name.replace("\\", "\\\\")
    return opening + name.replace(closing, closing * 2) + closing


def keyspace_path(bucket: str, scope: str | None = None, collection: str | None = None) -> str:
    """A SQL++ keyspace path, `bucket[.scope[.collection]]`, each part quoted when needed."""
    return ".".join(quote_ident(Dialect.SQLPP, part) for part in (bucket, scope, collection) if part is not None)


def in_list(dialect: Dialect, literals: Iterable[str]) -> str:
    opening, closing = _IN_LIST_BRACKETS[dialect]
    return opening + ", ".join(literals) + closing
```

  - `quote_literal` starts with `if _JSON_STRINGS[dialect]: return json.dumps(value, ensure_ascii=False)`; `bytes_literal` raises `ValueError(f"{dialect} has no binary literal")` when the template is None; `json_literal` returns `text` itself when `_JSON_LITERALS[dialect]` is None. Update the docstrings.
  - If Task 2's A4 test shows the server refusing `\b` or `\f` (json.dumps writes them), make `quote_literal` replace them with `\u0008` / `\u000c` after `json.dumps` and record it as a ruling.

- [ ] **Step 4: Implement the lexer** (`sql/lexer.py`):
  - Inside the quoted-identifier loop, before the closing-quote check: `if rules.identifier_backslash_escapes and text[i] == "\\": i += 2; continue  # SQL++: \` and \\ inside back-ticks`.
  - Two branches right after the quoted-identifier branch (before PostgreSQL's `$` branches):

```python
        elif ch in rules.named_param_prefixes and i + 1 < n and _param_char(text[i + 1]):
            i += 1  # SQL++ $name, @name, $1
            while i < n and _param_char(text[i]):
                i += 1
            kind = TokenKind.PARAM
        elif ch == "?" and rules.question_params:
            i += 1  # SQL++ positional parameter
            kind = TokenKind.PARAM
```

  with `def _param_char(ch: str) -> bool: return ch == "_" or (ch.isascii() and ch.isalnum())`.

- [ ] **Step 5: Implement the splitter** (`sql/splitter.py`): `StatementSpan` gains `params: tuple[str, ...] = ()  # the parameters it uses (SQL++): $name, $1, and each ?`; `_emit(…, repeat: int = 1, params: bool = False)` stores `_params(significant) if params else ()`; `split()` passes `params=bool(rules.named_param_prefixes or rules.question_params)` on both `_emit` calls of the general path. `_BodyTracker` already opens nothing for an unknown rule, so `BodyRule.NONE` needs no code.

```python
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
```

- [ ] **Step 6: Wire the rest.**
  - `domain/dialect.py`: `SQLPP = "sqlpp"`.
  - `sql/classifier.py`: `_EXPLAIN_KEYWORDS[Dialect.SQLPP] = ()  # SQL++ EXPLAIN never executes; the executed plan is profile=timings (04b)`; first line of `_from_sqlglot`: `if dialect in KEYWORD_ONLY_DIALECTS: return None  # no sqlglot dialect exists: the keyword rules decide, and that is not worth a log line`; `warm_up` loops over `[d for d in Dialect if d not in KEYWORD_ONLY_DIALECTS]`.
  - `sql/completion.py`:

```python
    Dialect.SQLPP: (
        "SELECT", "INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE", "WITH", "CREATE", "DROP", "ALTER", "BUILD",
        "EXPLAIN", "ADVISE", "INFER", "PREPARE", "EXECUTE", "GRANT", "REVOKE",
    ),
```

```python
    Dialect.SQLPP: (  # spec §3.3.6; no FULL / RIGHT JOIN
        "SELECT", "RAW", "DISTINCT", "FROM", "USE KEYS", "USE INDEX", "ON KEYS", "JOIN", "LEFT JOIN", "INNER JOIN",
        "NEST", "UNNEST", "LET", "LETTING", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET", "RETURNING",
        "SET", "UNSET", "VALUES", "ANY", "EVERY", "SATISFIES", "IN", "WITHIN", "END", "IS MISSING", "IS NOT MISSING",
        "IS VALUED", "IS NULL", "IS NOT NULL", "AND", "OR", "NOT", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE",
        "ARRAY", "FOR", "EXISTS", "UNION", "INTERSECT", "EXCEPT", "ALL", "AS", "ASC", "DESC", "MISSING", "NULL",
    ),
```

  - Create `sql/format_guard.py`:

```python
"""Did the formatter change what a statement means? (spec §3.3.7; R-04-12) sql-formatter splits `'it''s'` into two
strings for n1ql, and nothing stops a future formatter from doing worse in any dialect, so its output replaces the
text only when the significant tokens are the same. Pure; offsets are code points."""

from .dialects import CASE_SENSITIVE_IDENTIFIERS, Dialect, is_reserved
from .lexer import Token, TokenKind, tokenize

_LAYOUT_ONLY = frozenset({TokenKind.COMMENT, TokenKind.EXEC_COMMENT})


def _significant(text: str, dialect: Dialect) -> list[Token]:
    return [t for t in tokenize(text, dialect) if t.kind != TokenKind.SPACE]


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _case_free(tokens: list[Token], i: int, dialect: Dialect) -> bool:
    """A word whose case means nothing: everywhere but in dialects with case-sensitive names, where only reserved
    words and function names (a word right before `(`) are free."""
    if dialect not in CASE_SENSITIVE_IDENTIFIERS or is_reserved(dialect, tokens[i].text):
        return True
    following = next((t for t in tokens[i + 1:] if t.kind not in _LAYOUT_ONLY), None)
    return following is not None and following.kind == TokenKind.LPAREN


def _same(before: list[Token], after: list[Token], i: int, dialect: Dialect) -> bool:
    a, b = before[i], after[i]
    if a.kind != b.kind:
        return False
    if a.kind in _LAYOUT_ONLY:
        return _squeezed(a.text) == _squeezed(b.text)
    if a.kind == TokenKind.WORD and _case_free(before, i, dialect):
        return a.text.upper() == b.text.upper()
    return a.text == b.text


def first_difference(dialect: Dialect, original: str, formatted: str) -> int | None:
    """The code-point offset in `original` of the first significant token the formatter changed (or `len(original)`
    when it only added tokens); None when the texts differ only in whitespace, word case and comment layout."""
    before, after = _significant(original, dialect), _significant(formatted, dialect)
    for i in range(min(len(before), len(after))):
        if not _same(before, after, i, dialect):
            return before[i].start
    if len(before) > len(after):
        return before[len(after)].start
    return len(original) if len(after) > len(before) else None
```

  - `api/sql.py`: `StatementRange.params: list[str]  # the statement's parameters (SQL++): $name, $1, and each ?` filled with `list(s.params)`; and

```python
class FormatGuardRequest(_Input):
    dialect: Dialect
    original: str
    formatted: str


class FormatGuardResponse(BaseModel):
    same: bool
    offset: int | None  # UTF-16 in `original`: the first token the formatter changed


@router.post("/format-guard", response_model=FormatGuardResponse)
def format_guard(body: FormatGuardRequest) -> FormatGuardResponse:
    """Whether a formatter's output may replace the original: only when its significant tokens are the same."""
    offset = first_difference(body.dialect, body.original, body.formatted)
    if offset is None:
        return FormatGuardResponse(same=True, offset=None)
    return FormatGuardResponse(same=False, offset=Utf16Index(body.original).to_utf16(offset))
```

- [ ] **Step 7: Frontend.** Run `npm run gen:api`. `src/features/editor/extensions.ts` line 3 imports `type SQLDialect` (type-only, for the `SQL_DIALECTS` annotation), but `SQLDialect.define` needs the value: make it a value import, `import { MSSQL, MySQL, PostgreSQL, sql, SQLite, StandardSQL, SQLDialect } from "@codemirror/lang-sql";` (P44), and add:

```ts
/** SQL++ (Couchbase 7.6): its reserved words (as `SQLPP_RESERVED` in the backend), common functions, "…" strings,
 * back-tick names and $name / @name / ? parameters. */
const SQLPP = SQLDialect.define({
  keywords:
    "_index_condition _index_key advise all alter analyze and any array as asc at begin between binary boolean break bucket build by cache call case cast cluster collate collection commit committed connect continue correlated cover create current cycle database dataset datastore declare decrement default delete derived desc describe distinct do drop each element else end escape every except exclude execute exists explain false fetch filter first flatten flatten_keys flush following for force from fts function golang grant group groups gsi hash having if ignore ilike in include increment index infer inline inner insert intersect into is isolation javascript join key keys keyspace known language last lateral left let letting level like limit lsm map mapping matched materialized maxvalue merge minvalue missing namespace nest next nextval nl no not nth_value null nulls number object offset on option options or order others outer over parse partition password path pool preceding prepare prev previous prevval primary private privilege probe procedure public range raw read realm recursive reduce rename replace respect restart restrict return returning revoke right role rollback row rows satisfies savepoint schema scope select self sequence set show some start statistics string system then ties to tran transaction trigger true truncate unbounded under union unique unknown unnest unset update upsert use user users using validate value valued values vector via view when where while window with within work xor",
  builtin:
    "meta uuid now_str now_millis millis_to_str str_to_millis date_add_str date_diff_str date_part_str date_trunc_str array_agg array_append array_concat array_contains array_count array_distinct array_flatten array_length array_max array_min array_position array_range array_remove array_sort array_sum object_names object_pairs object_values object_length object_put object_remove object_concat to_number to_string to_boolean to_array to_object is_array is_object is_string is_number is_boolean typename lower upper trim ltrim rtrim length substr replace split contains position concat regexp_contains regexp_like regexp_replace abs ceil floor round trunc sum avg count min max ifmissing ifnull ifmissingornull nvl nvl2 decode greatest least base64_encode base64_decode encode_json decode_json ds_version current_users",
  doubleQuotedStrings: true,
  backslashEscapes: true,
  identifierQuotes: "`",
  hashComments: false,
  slashComments: false,
  spaceAfterDashes: false,
  specialVar: "?$@",
});
```

and `sqlpp: SQLPP` in `SQL_DIALECTS`. In `src/features/editor/format.ts`: `sqlpp: "n1ql"` in `LANGUAGES`; in `formatSql`, after `formatted` is computed and before the `view.state.doc !== state.doc` check:

```ts
    if (formatted === original) return;
    // The formatter must not change what the statement means (spec §3.3.7): n1ql splits 'it''s' into two strings.
    let same: boolean;
    try {
      const body = { dialect: settings.dialect ?? "postgres", original, formatted };
      same = unwrap(await (await api()).POST("/sql/format-guard", { body })).same;
    } catch (error) {
      toast("error", `Could not check the formatted text: ${errorMessage(error)}`);
      return;
    }
    if (!same) {
      toast("warning", "The formatter would change what the statement means (for example a string): the text is kept as it was");
      return;
    }
```

(and drop the later duplicate `if (formatted === original) return;`; import `api`, `unwrap` from `@/lib/api/client`). The guard runs for every dialect.

- [ ] **Step 8: Run everything**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green; the existing PG/SQLite/MySQL/T-SQL lexer, splitter, quoting, completion and classifier tests pass unchanged; the A4/A5 tests pass against the dev cluster (if A4 needed the `\b`/`\f` change, it is in and recorded).

- [ ] **Step 9: Commit**

```bash
git add backend/dbaguy_backend/domain/dialect.py backend/dbaguy_backend/sql backend/dbaguy_backend/api/sql.py \
  backend/tests/sql backend/tests/test_sql_api.py backend/tests/test_couchbase_literals.py \
  src/features/editor/extensions.ts src/features/editor/format.ts src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(sql): SQL++ dialect — lexer, parameters in spans, quoting and JSON literals, keywords; formatter token guard for every dialect"
```

---

### Task 3: SQL++ classification, danger reasons, read-only, `is_transaction_statement`

**Files:**
- Modify: `backend/dbaguy_backend/domain/classification.py`
- Modify: `backend/dbaguy_backend/sql/classifier.py`
- Modify: `src/features/query/reasons.ts`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Test: `backend/tests/sql/test_classifier.py`, `backend/tests/test_safety.py`

**Interfaces:**
- Consumes: `Dialect.SQLPP`, `tokenize`, `_TRANSACTION_STARTS`, `_combine`, `_words`, `_begins_a_transaction_statement` (Task 2, 03a/03b).
- Produces:
  - `DangerReason.SYSTEM_KEYSPACE = "system_keyspace"` (a write to a `system:` keyspace: killing requests, dropping prepared statements) and `DangerReason.MERGE_DELETE = "merge_delete"` (`MERGE … WHEN MATCHED THEN DELETE`); `GuardReason` gains both with the same values.
  - `_TRANSACTION_STARTS[Dialect.SQLPP]`.
  - `_unfiltered_dml(tokens: list[Token], filters: tuple[tuple[str, ...], ...]) -> list[DangerReason]` replaces `_tsql_unfiltered_dml(tokens)`; T-SQL passes `_TSQL_FILTERS = (("WHERE",),)`, SQL++ `_SQLPP_FILTERS = (("WHERE",), ("USE", "KEYS"), ("ON", "KEYS"))`.
  - `_classify_sqlpp(tokens: list[Token]) -> Classification`, called by `_classify_single` for SQL++ before anything else.
  - Public `is_transaction_statement(sql: str, dialect: Dialect) -> bool` (Task 11's adapter refuses these; D-04-13); `__all__` lists it.
  - `reasons.ts`: `system_keyspace`, `merge_delete` texts.

Rules (spec §3.3.4, survey §8.2): READ — SELECT, WITH, EXPLAIN (incl. EXPLAIN FUNCTION), ADVISE, INFER, PREPARE; leading `(` are skipped (`(SELECT …) UNION (SELECT …)`). WRITE — INSERT, UPSERT, UPDATE, DELETE, MERGE; `UPDATE STATISTICS` and `ANALYZE` (no danger reason); a SELECT/WITH holding `NEXT VALUE FOR` / `NEXTVAL FOR` or a DML word (a safety net: SQL++ rejects DML inside SELECT). DDL — CREATE, DROP, ALTER, BUILD, GRANT, REVOKE. TRANSACTION — the `_TRANSACTION_STARTS` table. UNKNOWN — EXECUTE (a prepared statement or a function, either may write), any other SET, anything else. EXPLAIN / ADVISE / PREPARE / INFER never execute the statement they hold, so no word inside them raises the kind. Reasons: DROP → `drop`; UPDATE/DELETE with none of WHERE, USE KEYS, ON KEYS at parenthesis depth 0 → `update_without_where` / `delete_without_where`; a MERGE holding DELETE → `merge_delete`; a write naming `system:` → `system_keyspace`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/sql/test_classifier.py`:

```python
SQ = Dialect.SQLPP
D = DangerReason


@pytest.mark.parametrize("sql, kind, reasons", [
    ("SELECT * FROM users", K.READ, []),
    ("select raw d.name from ks as d use keys ['a']", K.READ, []),
    ("WITH a AS (SELECT 1 AS x) SELECT a", K.READ, []),
    ("(SELECT 1) UNION (SELECT 2)", K.READ, []),
    ("EXPLAIN DELETE FROM users", K.READ, []),
    ("EXPLAIN FUNCTION f", K.READ, []),
    ("ADVISE SELECT * FROM users WHERE name = 'x'", K.READ, []),
    ('INFER users WITH {"sample_size": 10}', K.READ, []),
    ("PREPARE p FROM DELETE FROM users", K.READ, []),
    ("SELECT NEXT VALUE FOR s1", K.WRITE, []),
    ("SELECT nextval for s1", K.WRITE, []),
    ("SELECT PREV VALUE FOR s1", K.READ, []),
    ("SELECT 1 DELETE FROM users", K.WRITE, []),  # invalid for the server; still never a read
    ("INSERT INTO users (KEY, VALUE) VALUES ('k', {})", K.WRITE, []),
    ("UPSERT INTO users (KEY, VALUE) VALUES ('k', {})", K.WRITE, []),
    ("UPDATE users SET a = 1", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("UPDATE users USE KEYS 'k' SET a = 1", K.WRITE, []),
    ("UPDATE users SET a = 1 WHERE b = 2", K.WRITE, []),
    ("UPDATE users SET a = (SELECT RAW 1 FROM t WHERE x = 1)", K.WRITE, [D.UPDATE_WITHOUT_WHERE]),
    ("DELETE FROM users", K.WRITE, [D.DELETE_WITHOUT_WHERE]),
    ("DELETE FROM users d USE KEYS ['a', 'b']", K.WRITE, []),
    ("DELETE FROM users d WHERE d.k IN (SELECT RAW o.k FROM orders o WHERE o.x = 1)", K.WRITE, []),
    ("MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN DELETE", K.WRITE, [D.MERGE_DELETE]),
    ("MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN UPDATE SET u.n = 1", K.WRITE, []),
    ("DELETE FROM system:active_requests WHERE clientContextID = 'x'", K.WRITE, [D.SYSTEM_KEYSPACE]),
    ("DELETE FROM system:prepareds", K.WRITE, [D.DELETE_WITHOUT_WHERE, D.SYSTEM_KEYSPACE]),
    ("SELECT * FROM system:active_requests", K.READ, []),
    ("UPDATE STATISTICS FOR users(name)", K.WRITE, []),
    ("ANALYZE KEYSPACE users", K.WRITE, []),
    ("CREATE INDEX i ON users(name)", K.DDL, []),
    ("CREATE PRIMARY INDEX ON users", K.DDL, []),
    ("BUILD INDEX ON users(i)", K.DDL, []),
    ('ALTER INDEX i ON users WITH {"action": "move"}', K.DDL, []),
    ("DROP INDEX i ON users", K.DDL, [D.DROP]),
    ("DROP COLLECTION dbaguy.app.users", K.DDL, [D.DROP]),
    ("DROP SCOPE dbaguy.app", K.DDL, [D.DROP]),
    ("CREATE FUNCTION f() { 1 }", K.DDL, []),
    ("CREATE SEQUENCE s1", K.DDL, []),
    ("GRANT query_select ON users TO bob", K.DDL, []),
    ("BEGIN WORK", K.TRANSACTION, []),
    ("begin transaction", K.TRANSACTION, []),
    ("START TRANSACTION", K.TRANSACTION, []),
    ("COMMIT", K.TRANSACTION, []),
    ("ROLLBACK TO SAVEPOINT s", K.TRANSACTION, []),
    ("SAVEPOINT s", K.TRANSACTION, []),
    ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", K.TRANSACTION, []),
    ("BEGIN", K.UNKNOWN, []),
    ("EXECUTE p", K.UNKNOWN, []),
    ("EXECUTE FUNCTION f()", K.UNKNOWN, []),
    ("SET x = 1", K.UNKNOWN, []),
    ("`users`", K.UNKNOWN, []),
    ("FOO BAR", K.UNKNOWN, []),
])
def test_sqlpp(sql, kind, reasons):
    result = classify(sql, SQ)
    assert (result.kind, result.dangerous_reasons) == (kind, reasons)


def test_sqlpp_comments_strings_and_names_are_not_words():
    assert classify("/* DELETE */ SELECT 'DELETE', `delete` FROM ks", SQ).kind == K.READ
    assert classify("SELECT 1 -- ;DELETE\n", SQ).kind == K.READ
    assert classify("SELECT 1; DELETE FROM users", SQ).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]  # a script


def test_is_transaction_statement():
    assert is_transaction_statement("  begin work", SQ)
    assert is_transaction_statement("/* x */ COMMIT", SQ)
    assert not is_transaction_statement("SELECT 1", SQ)
    assert not is_transaction_statement("BEGIN", SQ)
    assert is_transaction_statement("BEGIN", Dialect.POSTGRES)  # a dialect without a table: the classifier decides


def test_tsql_unfiltered_dml_is_unchanged():  # the helper was generalised for SQL++
    assert classify("DELETE FROM t", Dialect.TSQL).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]
    assert classify("DELETE FROM t WHERE id IN (SELECT id FROM u)", Dialect.TSQL).dangerous_reasons == []
    assert classify("WITH c AS (SELECT 1 AS id) DELETE FROM t", Dialect.TSQL).dangerous_reasons == [D.DELETE_WITHOUT_WHERE]
```

(Import `is_transaction_statement` from `dbaguy_backend.sql.classifier` and `DangerReason` from the domain if the file does not yet.)

Append to `tests/test_safety.py` (the file imports `GuardAction as A`, so the tests below use the alias `A.RUN` / `A.BLOCK` / `A.CONFIRM`, never `GuardAction`; P38):

```python
SQ = Dialect.SQLPP


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users", "EXPLAIN DELETE FROM users", "ADVISE SELECT * FROM users WHERE a = 1",
    'INFER users WITH {"sample_size": 5}', "PREPARE p FROM UPSERT INTO users (KEY, VALUE) VALUES ('k', {})",
    "WITH a AS (SELECT 1 AS x) SELECT a",
])
def test_sqlpp_reads_run_read_only(sql):
    assert SafetyPolicy().check(sql, SQ, "standard", read_only=True).action == A.RUN


@pytest.mark.parametrize("sql", [
    "UPSERT INTO users (KEY, VALUE) VALUES ('k', {})", "EXECUTE p", "EXECUTE FUNCTION f()",
    "SELECT NEXT VALUE FOR s1", "DELETE FROM system:active_requests", "CREATE INDEX i ON users(a)",
    "MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN DELETE", "SET x = 1",
])
def test_sqlpp_read_only_bypasses_fail_closed(sql):  # Review Focus 5
    assert SafetyPolicy().check(sql, SQ, "standard", read_only=True).action == A.BLOCK


def test_sqlpp_transactions_are_left_to_the_adapter():
    # Not a safety decision (D-04-13): the policy lets BEGIN WORK through, the Couchbase adapter refuses it.
    assert SafetyPolicy().check("BEGIN WORK", SQ, "standard", read_only=True).action == A.RUN


@pytest.mark.parametrize("sql, reason", [
    ("DELETE FROM users", GuardReason.DELETE_WITHOUT_WHERE),
    ("DELETE FROM system:active_requests WHERE clientContextID = 'x'", GuardReason.SYSTEM_KEYSPACE),
    ("MERGE INTO users u USING orders o ON KEY o.u WHEN MATCHED THEN DELETE", GuardReason.MERGE_DELETE),
    ("DROP COLLECTION dbaguy.app.users", GuardReason.DROP),
])
def test_sqlpp_dangers_are_confirmed_at_standard(sql, reason):
    decision = SafetyPolicy().check(sql, SQ, "standard", read_only=False)
    assert (decision.action, decision.reasons) == (A.CONFIRM, [reason])


def test_targeted_sqlpp_writes_run_at_standard():
    assert SafetyPolicy().check("DELETE FROM users USE KEYS 'k'", SQ, "standard", read_only=False).action == A.RUN
```

(Import `GuardReason` from `dbaguy_backend.domain.classification` if the file does not yet.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_classifier.py tests/test_safety.py -q`
Expected: `AttributeError: SYSTEM_KEYSPACE` at collection.

- [ ] **Step 3: Implement.** `domain/classification.py`: add to `DangerReason` `SYSTEM_KEYSPACE = "system_keyspace"  # SQL++: a write to system: (kills requests, drops prepared statements)` and `MERGE_DELETE = "merge_delete"  # SQL++: MERGE … WHEN MATCHED THEN DELETE`, and to `GuardReason` the same two with `DangerReason.….value`.

`sql/classifier.py`:

```python
_TRANSACTION_STARTS[Dialect.SQLPP] = (  # written in the dict literal next to the T-SQL entry
    ("BEGIN", "WORK"), ("BEGIN", "TRANSACTION"), ("BEGIN", "TRAN"), ("START", "TRANSACTION"), ("START", "WORK"),
    ("COMMIT",), ("ROLLBACK",), ("SAVEPOINT",), ("SET", "TRANSACTION"),
)
# SQL++ (spec §3.3.4): keyword rules only, no sqlglot dialect exists.
_SQLPP_KIND: dict[str, StatementKind] = {
    **dict.fromkeys(("SELECT", "WITH", "EXPLAIN", "ADVISE", "INFER", "PREPARE"), StatementKind.READ),
    **dict.fromkeys(("INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE"), StatementKind.WRITE),
    **dict.fromkeys(("CREATE", "DROP", "ALTER", "BUILD", "GRANT", "REVOKE"), StatementKind.DDL),
}  # EXECUTE (a prepared statement or a JS function may write), other SETs, anything else: UNKNOWN
_SQLPP_DML = frozenset({"INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE"})
_SQLPP_SEQUENCE_ADVANCE = (("NEXT", "VALUE", "FOR"), ("NEXTVAL", "FOR"))  # PREV VALUE FOR only reads
_SQLPP_FILTERS = (("WHERE",), ("USE", "KEYS"), ("ON", "KEYS"))  # a statement given its documents is targeted
_TSQL_FILTERS = (("WHERE",),)


def _has_phrase(words: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(tuple(words[k:k + len(p)]) == p for p in phrases for k in range(len(words)))


def _writes_system_keyspace(tokens: list[Token]) -> bool:
    """`system:` named in the statement (`DELETE FROM system:active_requests …`): the lexer reads `system`, `:`."""
    return any(
        a.kind == TokenKind.WORD and a.upper == "SYSTEM" and b.kind == TokenKind.OTHER and b.text == ":"
        for a, b in zip(tokens, tokens[1:])
    )


def _classify_sqlpp(tokens: list[Token]) -> Classification:
    significant = [t for t in tokens if t.kind not in INSIGNIFICANT]
    while significant and significant[0].kind == TokenKind.LPAREN:  # (SELECT …) UNION (SELECT …)
        significant = significant[1:]
    if not significant or significant[0].kind != TokenKind.WORD:
        return Classification(kind=StatementKind.UNKNOWN)
    words = _words(significant)
    if _begins_a_transaction_statement(words, Dialect.SQLPP):
        return Classification(kind=StatementKind.TRANSACTION)
    first = words[0]
    if words[:2] == ["UPDATE", "STATISTICS"] or first == "ANALYZE":
        return Classification(kind=StatementKind.WRITE)  # maintenance (EE); no WHERE to miss
    kind = _SQLPP_KIND.get(first, StatementKind.UNKNOWN)
    if first in ("SELECT", "WITH") and (
        any(w in _SQLPP_DML for w in words[1:]) or _has_phrase(words, _SQLPP_SEQUENCE_ADVANCE)
    ):
        kind = StatementKind.WRITE  # NEXT VALUE FOR advances a sequence; DML words: a safety net
    reasons: list[DangerReason] = []
    if first == "DROP":
        reasons.append(DangerReason.DROP)
    if first in ("UPDATE", "DELETE"):
        reasons.extend(_unfiltered_dml(significant, _SQLPP_FILTERS))
    if first == "MERGE" and "DELETE" in words[1:]:
        reasons.append(DangerReason.MERGE_DELETE)
    if kind == StatementKind.WRITE and _writes_system_keyspace(significant):
        reasons.append(DangerReason.SYSTEM_KEYSPACE)
    return Classification(kind=kind, dangerous_reasons=reasons)
```

Replace `_tsql_unfiltered_dml` with the general helper (T-SQL behaviour unchanged; `_classify_single` calls `_unfiltered_dml(tokens, _TSQL_FILTERS)` where it called `_tsql_unfiltered_dml(tokens)`):

```python
def _unfiltered_dml(tokens: list[Token], filters: tuple[tuple[str, ...], ...]) -> list[DangerReason]:
    """The danger reason of an UPDATE or DELETE, also one after a T-SQL CTE (WITH … DELETE), that has none of `filters`
    at parenthesis depth 0 after it: a filter inside a subquery or a CTE filters nothing. T-SQL filters with WHERE;
    SQL++ also with USE KEYS / ON KEYS. Decided on the words, whatever a parser makes of the rest."""
    depth, top = 0, []  # the words at depth 0, in order
    for k, token in enumerate(t for t in tokens if t.kind not in INSIGNIFICANT):
        word = token.upper if token.kind == TokenKind.WORD else ""
        depth = max(depth + (token.kind == TokenKind.LPAREN) - (token.kind == TokenKind.RPAREN), 0)
        if depth or not word:
            continue
        if k == 0 and word not in ("UPDATE", "DELETE", "WITH"):
            return []
        top.append(word)
    dml = next((i for i, w in enumerate(top) if w in ("SELECT", "INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE")), None)
    if dml is None or top[dml] not in ("UPDATE", "DELETE") or _has_phrase(top[dml + 1:], filters):
        return []
    return [DangerReason.UPDATE_WITHOUT_WHERE if top[dml] == "UPDATE" else DangerReason.DELETE_WITHOUT_WHERE]
```

`_classify_single` begins with `if dialect is Dialect.SQLPP: return _classify_sqlpp(tokenize(sql, dialect))`. And:

```python
def is_transaction_statement(sql: str, dialect: Dialect) -> bool:
    """Whether `sql` (one statement) is transaction control by the same table the classifier uses (DRY): the Couchbase
    adapter refuses these itself, since v1 has no SQL++ transactions (D-04-13). Dialects without a table ask the
    classifier."""
    starts = _TRANSACTION_STARTS.get(dialect)
    if starts is None:
        return classify(sql, dialect).kind == StatementKind.TRANSACTION
    return _begins_a_transaction_statement(_words(tokenize(sql, dialect)), dialect)
```

`services/safety.py` needs no change: READ runs read-only, a TRANSACTION without WRITE passes to the adapter, SQL++ has no SESSION entry (blocked), the rest is blocked.

`src/features/query/reasons.ts`: `system_keyspace: "Writes to a system keyspace (system:…)"`, `merge_delete: "MERGE deletes matched documents"` (after `npm run gen:api`, `Record<GuardReason, string>` requires both).

- [ ] **Step 4: Run everything**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run gen:api && npm run typecheck && npm run build`
Expected: green; the T-SQL classifier tests are unchanged.

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/domain/classification.py backend/dbaguy_backend/sql/classifier.py \
  backend/tests/sql/test_classifier.py backend/tests/test_safety.py src/features/query/reasons.ts \
  src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(sql): SQL++ classification — keyword rules, unfiltered/system:/MERGE…DELETE reasons, fail-closed EXECUTE, transaction statements"
```

---

### Task 4: The shared contract: `RunOptions`, capabilities, typed cells, query context, endpoint hook

**Files:**
- Modify: `backend/dbaguy_backend/domain/execution.py`, `domain/column_category.py`, `domain/metadata.py`, `domain/profiles.py`, `domain/errors.py`
- Modify: `backend/dbaguy_backend/adapters/base.py`, `adapters/values.py`
- Create: `backend/dbaguy_backend/adapters/sockets.py`
- Modify: `backend/dbaguy_backend/adapters/mysql/adapter.py` (keepalive through `sockets`; `run` signature), `adapters/postgres/adapter.py`, `adapters/sqlite/adapter.py`, `adapters/mssql/adapter.py` (`run` signature only)
- Modify: `backend/dbaguy_backend/services/connection_service.py`
- Modify: `src/features/session/SessionController.ts` (`execute()` sends `profile_plan: false`; P33)
- Modify: `backend/tests/fake_db.py`, `backend/tests/test_session_worker.py` (`MessagingConnection.run`), `backend/tests/test_api.py` (`test_engines`)
- Create: `backend/tests/test_query_context.py`; modify `backend/tests/test_core_utils.py`, `backend/tests/test_connection_service.py`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces (`domain/execution.py`):

```python
Presentation = Literal["grid", "plan"]  # "plan": an EXPLAIN / ADVISE / profile result the client may show as a plan


class QueryContext(BaseModel):
    """Couchbase: the bucket (and scope) that unqualified keyspace names resolve against, sent with every request
    (D-04-5: a structure, because a bucket name may hold a dot)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    bucket: str = Field(min_length=1, max_length=100)
    scope: str | None = Field(default=None, min_length=1, max_length=251)

    def text(self) -> str: ...                    # display and history text; parse(text()) == self
    @classmethod
    def parse(cls, text: str | None) -> "QueryContext | None": ...   # ValueError when malformed


class StatementParams(BaseModel):
    """Typed parameters of one statement (R-10): JSON literal texts, sent unchanged. Content is checked by the worker
    (`invalid_params`), only the shape here."""
    model_config = ConfigDict(extra="forbid")
    named: dict[str, str] = Field(default_factory=dict, max_length=200)  # name without $ / @ → JSON literal text
    positional: list[str] = Field(default_factory=list, max_length=200)  # $1, $2 … / ? in order
```

  - `ExecuteCommand` gains `query_context: QueryContext | None = None`, `params: StatementParams | None = None`, `profile_plan: bool = False`; `ExportCommand` gains `query_context`, `params`; `ResultSetEvent` gains `presentation: Presentation = "grid"`. `query_context` None means **no** context in the request (ruling P1).
  - `ColumnCategory.MIXED = "mixed"` — every cell carries its own type: a string is a string, never guessed as JSON.
  - `ServerInfo.edition: Literal["enterprise", "community"] | None = None`; `ServerInfo.default_query_context: QueryContext | None = None` (Couchbase: the profile's default context; the client sends it unless a tab chose another, P1).
  - `EngineInfo` gains `transactions: bool`, `query_context: bool`, `parameters: bool`, `documents: bool`, `executed_plan: bool`, `plan_format: Literal["couchbase"] | None` (copied by `info()`).
  - `domain/errors.py`: `class UnsupportedFormat(ValidationFailed): code = "unsupported_format"`.
- Produces (`adapters/base.py`):

```python
@dataclass(frozen=True)
class RunOptions:
    """Per-request options of `run()` (D-04-3). The worker checks them against the engine's capabilities, so an adapter
    never receives one it did not declare."""
    query_context: QueryContext | None = None  # needs capabilities.query_context
    params: StatementParams | None = None  # needs capabilities.parameters
    profile_plan: bool = False  # needs capabilities.executed_plan (04b)
    retry_safe: bool = False  # the statement was classified READ: an adapter may send it again after a stale connection


class RawJsonRows(ABC):
    """A result whose rows are JSON documents as the server sent them (Couchbase): JSON / JSONL exports write them."""
    @abstractmethod
    def fetch_raw(self, n: int) -> list[str]: ...
```

  - `StatementResult.presentation(self) -> Presentation` (default `"grid"`).
  - `AdapterCapabilities` gains `transactions: bool = True`, `query_context: bool = False`, `parameters: bool = False`, `documents: bool = False`, `executed_plan: bool = False`, `plan_format: Literal["couchbase"] | None = None`, `paused_result_max_idle_s: float | None = None`.
  - `DatabaseConnection.run(self, sql: str, options: RunOptions | None = None) -> StatementResult` (abstract). PostgreSQL, MySQL, SQL Server, SQLite and `FakeConnection` take and ignore `options`.
  - `DatabaseAdapter.endpoint_target(self, profile: ServerProfile) -> tuple[str, int]` — default `(profile.host, profile.port or self.default_port)`, `ValidationFailed("A port is required")` without one; `ConnectionService._endpoint` uses it for the direct endpoint and the tunnel target.
- Produces (`adapters/values.py`): `@dataclass(frozen=True, slots=True) class JsonText: text: str` (a JSON value kept as the server's text: objects, arrays, numbers that no Python number holds exactly); `MISSING` (the single `MissingType()` instance: the row has no such key); `encode_value(JsonText(t))` → `{"$type": "json", "text": t}` (over `TEXT_LIMIT` characters: the existing truncated text cell of `t`); `encode_value(MISSING)` → `{"$type": "missing"}`.
- Produces (`adapters/sockets.py`): `enable_keepalive(sock: socket.socket) -> None` — SO_KEEPALIVE with 30 s idle, 10 s interval, 3 probes, best effort (an `OSError` is logged at DEBUG).

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_query_context.py`:

```python
import pytest

from dbaguy_backend.domain.execution import QueryContext as Q


@pytest.mark.parametrize("text, expected", [
    ("travel-sample", Q(bucket="travel-sample")),
    ("travel-sample.inventory", Q(bucket="travel-sample", scope="inventory")),
    ("my.bucket.app", Q(bucket="my.bucket", scope="app")),  # the last dot: scope names hold no dot
    ("`my.bucket`", Q(bucket="my.bucket")),
    ("`my.bucket`.app", Q(bucket="my.bucket", scope="app")),
    ("`b`.`s`", Q(bucket="b", scope="s")),
    ("default:dbaguy.app", Q(bucket="dbaguy", scope="app")),
    ("DEFAULT:`a``b`", Q(bucket="a`b")),
    ("  dbaguy  ", Q(bucket="dbaguy")),
    ("", None), (None, None), ("   ", None),
])
def test_parse(text, expected):
    assert Q.parse(text) == expected


@pytest.mark.parametrize("text", ["`a", "a.", ".a", "`a`b", "a.b.`c", "`a`.`b`.`c`", "a..b"])
def test_malformed(text):
    with pytest.raises(ValueError):
        Q.parse(text)


@pytest.mark.parametrize("context, text", [
    (Q(bucket="travel-sample", scope="inventory"), "travel-sample.inventory"),
    (Q(bucket="dbaguy"), "dbaguy"),
    (Q(bucket="my.bucket"), "`my.bucket`"),
    (Q(bucket="my.bucket", scope="app"), "my.bucket.app"),
    (Q(bucket="a`b", scope="s"), "`a``b`.s"),
])
def test_text_round_trips(context, text):
    assert context.text() == text and Q.parse(text) == context
```

Append to `tests/test_core_utils.py` (import `JsonText`, `MISSING`, `TEXT_LIMIT` from `dbaguy_backend.adapters.values`, `enable_keepalive` from `dbaguy_backend.adapters.sockets`, `socket`):

```python
def test_encode_json_text_and_missing():
    assert encode_value(JsonText('{"a": 1e400}')) == {"$type": "json", "text": '{"a": 1e400}'}
    assert encode_value(MISSING) == {"$type": "missing"}
    long = JsonText("[" + "1," * TEXT_LIMIT + "1]")
    cell = encode_value(long)
    assert cell["$type"] == "text" and cell["truncated"] and cell["size"] == len(long.text)


def test_enable_keepalive_sets_the_timers():
    server = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(server.getsockname())
    try:
        enable_keepalive(client)
        assert client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            assert client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE) == 30
    finally:
        client.close()
        server.close()
```

Append to `tests/test_connection_service.py`:

```python
def test_the_adapter_decides_the_endpoint(tmp_path):
    class Pinned(SqliteAdapter):  # any adapter: only endpoint_target matters here
        def endpoint_target(self, profile):
            return "first-seed", 4242

    profile = ServerProfile(id=uuid4(), name="x", engine="postgres", host="a,b", port=None)
    service = ConnectionService(AdapterRegistry([Pinned()]), None, None)
    endpoint, release = service._endpoint(Pinned(), profile, ResolvedSecrets(), share_tunnel=True)
    assert (endpoint.host, endpoint.port) == ("first-seed", 4242)
    release()


def test_the_default_endpoint_needs_a_port():
    with pytest.raises(ValidationFailed, match="port"):
        SqliteAdapter().endpoint_target(ServerProfile(id=uuid4(), name="x", engine="postgres", host="h"))
```

(Add the imports the file lacks: `uuid4`, `ServerProfile`, `ValidationFailed`, `SqliteAdapter`, `AdapterRegistry`, `ResolvedSecrets`.)

`tests/test_api.py::test_engines`: the engine set gains `"couchbase"` only in Task 11; here assert the new fields for the four existing engines — every one has `transactions` True and `query_context`, `parameters`, `documents`, `executed_plan` False and `plan_format` None:

```python
    for info in engines.values():
        assert info["transactions"] and info["plan_format"] is None
        assert not (info["query_context"] or info["parameters"] or info["documents"] or info["executed_plan"])
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_query_context.py tests/test_core_utils.py tests/test_connection_service.py tests/test_api.py -q`
Expected: import errors (`QueryContext`, `JsonText`, `sockets`).

- [ ] **Step 3: Implement the domain models** as listed under Interfaces. `QueryContext.text()` and `parse()`:

```python
_DEFAULT_NAMESPACE = "default:"


def _context_part(name: str, *, quote_dots: bool) -> str:
    """A name as context text: back-ticked when it holds a back-tick, or a dot where a dot would be read as the split."""
    if "`" in name or (quote_dots and "." in name):
        return "`" + name.replace("`", "``") + "`"
    return name


def _path_parts(text: str) -> list[str]:
    """`a.b`, `` `a.b` ``, `` `a`.`b` ``: SQL++ path parts, back-ticks with `` and \\ escapes."""
    parts: list[str] = []
    i, n = 0, len(text)
    while True:
        if i < n and text[i] == "`":
            name: list[str] = []
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unclosed back-tick")
                if text[i] == "\\" and i + 1 < n:
                    name.append(text[i + 1])
                    i += 2
                elif text[i] == "`" and text[i + 1:i + 2] == "`":
                    name.append("`")
                    i += 2
                elif text[i] == "`":
                    i += 1
                    break
                else:
                    name.append(text[i])
                    i += 1
            parts.append("".join(name))
        else:
            end = text.find(".", i)
            end = n if end < 0 else end
            if "`" in text[i:end]:
                raise ValueError("a back-tick inside a name")
            parts.append(text[i:end])
            i = end
        if i == n:
            return parts
        if text[i] != ".":
            raise ValueError("expected a dot")
        i += 1
```

```python
    def text(self) -> str:
        """`travel-sample.inventory`, `dbaguy`, `` `my.bucket` `` (a bucket alone whose name holds a dot), `my.bucket.app`
        (with a scope the last dot splits). What the history keeps; `parse` reads it back."""
        if self.scope is None:
            return _context_part(self.bucket, quote_dots=True)
        return f"{_context_part(self.bucket, quote_dots=False)}.{_context_part(self.scope, quote_dots=True)}"

    @classmethod
    def parse(cls, text: str | None) -> "QueryContext | None":
        """A profile's database field or a history entry: `bucket`, `bucket.scope` (split at the last dot), with an
        optional `default:` in front, or SQL++ back-ticked parts. None for no text; ValueError when malformed."""
        text = (text or "").strip()
        if text[:len(_DEFAULT_NAMESPACE)].lower() == _DEFAULT_NAMESPACE:
            text = text[len(_DEFAULT_NAMESPACE):]
        if not text:
            return None
        if "`" in text:
            parts = _path_parts(text)
        else:
            parts = text.rsplit(".", 1) if "." in text else [text]
        # an empty part, or a bucket ending in a dot (`a..b` splits into `a.` and `b`), is malformed
        if not 1 <= len(parts) <= 2 or not all(parts) or ("`" not in text and parts[0].endswith(".")):
            raise ValueError("not a bucket or bucket.scope")
        return cls(bucket=parts[0], scope=parts[1] if len(parts) == 2 else None)  # pydantic's ValidationError is a ValueError
```

- [ ] **Step 4: Implement `adapters/values.py` and `adapters/sockets.py`.**

```python
@dataclass(frozen=True, slots=True)
class JsonText:
    """A JSON value kept as the server's text (Couchbase): an object or array, or a number no Python number holds
    exactly (`1.10`, `1e400`, an integer past ±(2^53−1)). The text is never re-encoded (spec §3.2.4)."""

    text: str


@final
class MissingType:
    """The row has no such key (SQL++ MISSING), which is not NULL (R-04-8). Use the `MISSING` instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = MissingType()
```

and in `encode_value`, before the `str` case: `if isinstance(value, JsonText): return {"$type": "json", "text": value.text} if len(value.text) <= TEXT_LIMIT else _truncated(value.text)` and `if value is MISSING: return {"$type": "missing"}`, where `_truncated(text)` is the existing long-text dict, factored out of the `str` case (DRY).

`adapters/sockets.py`:

```python
"""Socket options shared by adapters that own their TCP socket (MySQL's driver socket, Couchbase's HTTP connection)."""

import socket

from loguru import logger

KEEPALIVE_IDLE_S, KEEPALIVE_INTERVAL_S, KEEPALIVE_PROBES = 30, 10, 3


def enable_keepalive(sock: socket.socket) -> None:
    """TCP keepalive (30 s idle, 10 s interval, 3 probes), as PostgreSQL's connection uses: an idle tunnel or NAT does
    not drop the connection unnoticed. Best effort."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPALIVE", KEEPALIVE_IDLE_S), ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_S),
                            ("TCP_KEEPCNT", KEEPALIVE_PROBES)):
            if (option := getattr(socket, name, None)) is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as exc:
        logger.debug("TCP keepalive not set: {}", exc)
```

`adapters/mysql/adapter.py`: `_enable_keepalive(conn)` keeps finding the driver's socket (`getattr(getattr(conn, "_socket", None), "sock", None)`, DEBUG when absent) and then calls `enable_keepalive(sock)`.

- [ ] **Step 5: Implement `adapters/base.py`** as listed under Interfaces; `info()` copies the six new `EngineInfo` fields from `capabilities`. Every adapter's `run` becomes `def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:` (docstring of the abstract one: "Execute one statement/batch. `options` holds only what the adapter's capabilities declare (the worker checks); adapters without such capabilities ignore it. Raises StatementError."). `FakeConnection.run(self, sql, options=None)` records `options` in `self.options: list[RunOptions | None]`; `MessagingConnection.run(self, sql, options=None)` passes it on. `ConnectionService._endpoint`:

```python
        host, port = adapter.endpoint_target(profile)
        if not profile.ssh.enabled:
            return Endpoint(host, port), _noop
        tunnel = self._tunnels.lease(profile.id, profile.ssh, host, port, secrets.ssh_password, shared=share_tunnel)
```

- [ ] **Step 6: Run everything**

First one frontend change (P33): openapi-typescript 7 makes a field with a non-null default required, so after `npm run gen:api` `ExecuteCommand.profile_plan` (default `false`) is a required `profile_plan: boolean`, and `SessionController.execute()` (`src/features/session/SessionController.ts`) passes its object literal to `track(…, command: ClientCommand)` without it, which fails the typecheck. Add `profile_plan: false,` to that literal, after `history: options.history ?? true,`. (`query_context` and `params` default to null, so they stay optional and need nothing; Task 12 adds `query_context` to the same literal and keeps `profile_plan: false`.)

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run gen:api && npm run typecheck && npm run build`
Expected: green; nothing else changes behaviour (no adapter declares a new capability yet).

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/domain backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/adapters/values.py \
  backend/dbaguy_backend/adapters/sockets.py backend/dbaguy_backend/adapters/mysql/adapter.py \
  backend/dbaguy_backend/adapters/postgres/adapter.py backend/dbaguy_backend/adapters/sqlite/adapter.py \
  backend/dbaguy_backend/adapters/mssql/adapter.py backend/dbaguy_backend/services/connection_service.py \
  src/features/session/SessionController.ts \
  backend/tests/fake_db.py backend/tests/test_session_worker.py backend/tests/test_api.py \
  backend/tests/test_query_context.py backend/tests/test_core_utils.py backend/tests/test_connection_service.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts
git commit -m "feat(core): run(sql, options), engine capabilities, presentation, raw JSON rows, JSON/MISSING cells, query context, endpoint hook"
```

---

### Task 5: Copy and export of typed cells: JSON and MISSING cells, MIXED columns, SQL++ IN lists, raw JSON writer

**Files:**
- Create: `backend/dbaguy_backend/adapters/couchbase/__init__.py` (empty), `backend/dbaguy_backend/adapters/couchbase/jsontext.py`
- Modify: `backend/dbaguy_backend/sql/formatting.py`
- Test: `backend/tests/sql/test_formatting.py`, `backend/tests/test_couchbase_literals.py`

**Interfaces:**
- Consumes: `JsonText`, `MISSING`, `ColumnCategory.MIXED`, `UnsupportedFormat` (Task 4); `in_list`, `json_literal`, `quote_literal` for SQL++ (Task 2).
- Produces:
  - `adapters/couchbase/jsontext.py`: `one_line(text: str) -> str` — JSON text without the whitespace between tokens; strings, numbers, key order and escapes unchanged.
  - `sql/formatting.py`:
    - normalised cells `_Json(text)` and `_MISSING_CELL` (a `_MissingCell` instance) from raw `JsonText` / `MISSING` and from encoded `{"$type":"json","text":…}` / `{"$type":"missing"}`;
    - text writers (TSV, CSV, Markdown): `_Json` → its text, MISSING → `""` (never `null_text`; R-04-8);
    - JSON / JSONL object writers: `_Json` embedded as its text, MISSING → the key is left out, a MIXED column's string is always a JSON string (never re-read as JSON);
    - SQL literals: `_Json` → `json_literal(dialect, text)`, MISSING → `MISSING`; the IN list goes through `in_list(dialect, …)` (SQL++: `[…]`);
    - `value_condition`: MISSING → `<name> IS MISSING`, `_Json` → `<name> = <text>`;
    - `unsupported_reason(kind: FormatKind, dialect: Dialect) -> str | None` — `"INSERT statements are not offered for Couchbase"` for (INSERT, SQLPP); `format_rows` raises `UnsupportedFormat(reason)` for it;
    - `RAW_JSON_FORMATS: frozenset[FormatKind] = frozenset({FormatKind.JSON, FormatKind.JSONL})`;
    - `class RawJsonWriter` with `write(rows: Iterable[str]) -> None` and `close() -> None`, and `open_raw_json_writer(kind: FormatKind, fp: TextIO) -> RawJsonWriter` (ValueError for other kinds).

- [ ] **Step 1: Write the failing tests.** Append to `tests/sql/test_formatting.py` (imports: `JsonText as J`, `MISSING` from `dbaguy_backend.adapters.values`; `UnsupportedFormat` from `dbaguy_backend.domain.errors`; `open_raw_json_writer`, `unsupported_reason` from `dbaguy_backend.sql.formatting`; `StringIO`):

```python
SQ = Dialect.SQLPP
MIX = C.MIXED
DOC_COLUMNS = [FormatColumn(name="doc", category=MIX), FormatColumn(name="n", category=C.NUMBER),
               FormatColumn(name="s", category=MIX)]
RAW_ROWS = [[J('{"z":1,"a":[1e400, 1.10]}'), J("9007199254740993"), "[1] stays a string"], [MISSING, 5, None]]
ENCODED_ROWS = [[{"$type": "json", "text": '{"z":1,"a":[1e400, 1.10]}'}, {"$type": "json", "text": "9007199254740993"},
                 "[1] stays a string"], [{"$type": "missing"}, 5, None]]


@pytest.mark.parametrize("source, rows", [("raw", RAW_ROWS), ("encoded", ENCODED_ROWS)])
def test_json_cells_in_every_writer(source, rows):  # Review Focus 1
    def run(kind, **options):
        return format_rows(kind, DOC_COLUMNS, rows, RowFormatOptions(**options), SQ, source).text

    assert run(F.CSV, null_text="NULL") == (
        'doc,n,s\n"{""z"":1,""a"":[1e400, 1.10]}",9007199254740993,[1] stays a string\n,5,NULL\n'
    )  # MISSING is empty even when NULL has a text
    assert run(F.JSONL).splitlines() == [
        '{"doc": {"z":1,"a":[1e400, 1.10]}, "n": 9007199254740993, "s": "[1] stays a string"}',
        '{"n": 5, "s": null}',  # the missing key is left out
    ]
    assert run(F.TSV).splitlines()[2] == "\t5\t"
    assert run(F.MARKDOWN).splitlines()[3] == "|  | 5 |  |"


def test_sqlpp_in_list_and_no_insert():
    column = [FormatColumn(name="v", category=MIX)]
    rows = [[1], ["a"], [True], [None], [MISSING], [J('{"k":1}')], [1]]
    assert format_rows(F.IN_LIST, column, rows, RowFormatOptions(), SQ, "raw").text == '[1, "a", TRUE, NULL, MISSING, {"k":1}]'
    assert unsupported_reason(F.INSERT, SQ) == "INSERT statements are not offered for Couchbase"
    assert unsupported_reason(F.INSERT, PG) is None and unsupported_reason(F.CSV, SQ) is None
    with pytest.raises(UnsupportedFormat):
        format_rows(F.INSERT, column, rows, RowFormatOptions(), SQ, "raw")


@pytest.mark.parametrize("value, category, expected", [
    ({"$type": "missing"}, MIX, "amount IS MISSING"),
    ({"$type": "json", "text": '{"a": 1}'}, MIX, 'amount = {"a": 1}'),
    ("x", MIX, 'amount = "x"'),
    (5, C.NUMBER, "amount = 5"),
    (None, MIX, "amount IS NULL"),
])
def test_sqlpp_value_condition(value, category, expected):
    assert value_condition(SQ, "amount", value, category) == expected


def test_a_malformed_json_cell_is_refused():
    with pytest.raises(ValidationFailed, match="json"):
        format_rows(F.CSV, [FormatColumn(name="d", category=MIX)], [[{"$type": "json"}]], RowFormatOptions(), SQ, "encoded")


def test_raw_json_writer_keeps_row_text():  # Review Focus 1
    stored = '{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'
    rows = [stored, "1e400", '"x\\ny"', '{"a": 1, "b": "c d"}']
    out = StringIO()
    writer = open_raw_json_writer(F.JSONL, out)
    writer.write(rows)
    writer.close()
    assert out.getvalue().splitlines() == [
        '{"zeta":1,"alpha":{"z":1,"a":2},"ws":[1,2],"mid":"a b"}',  # had a line break: squeezed outside strings
        "1e400", '"x\\ny"', '{"a": 1, "b": "c d"}',  # one line already: exactly as sent
    ]
    out = StringIO()
    writer = open_raw_json_writer(F.JSON, out)
    writer.write(rows[:2])
    writer.close()
    assert out.getvalue() == "[\n  " + stored + ",\n  1e400\n]\n"  # a JSON array keeps every byte
    out = StringIO()
    open_raw_json_writer(F.JSON, out).close()
    assert out.getvalue() == "[]\n"
    with pytest.raises(ValueError):
        open_raw_json_writer(F.CSV, StringIO())
```

Append to `tests/test_couchbase_literals.py` (A19: the IN list DbaGuy writes is one the server reads):

```python
def test_in_list_is_an_array_the_server_reads():  # A19
    column = [FormatColumn(name="v", category=ColumnCategory.MIXED)]
    values = [[1], ["a"], [JsonText('{"k": 1}')], [True]]
    in_list = format_rows(FormatKind.IN_LIST, column, values, RowFormatOptions(), SQ, "raw").text
    result = couchbase_query(f'SELECT RAW x FROM [1, "a", {{"k": 1}}, true, 2, "b"] AS x WHERE x IN {in_list}')
    assert result["results"] == [1, "a", {"k": 1}, True]
```

(Imports: `FormatColumn`, `FormatKind`, `RowFormatOptions` from `dbaguy_backend.domain.formats`, `ColumnCategory`, `JsonText`, `format_rows`.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_formatting.py tests/test_couchbase_literals.py -q`
Expected: `ImportError: cannot import name 'open_raw_json_writer'`.

- [ ] **Step 3: Implement `adapters/couchbase/jsontext.py`.**

```python
"""Lossless helpers for JSON text as Couchbase sends and stores it: never parsed and re-encoded, so numbers, key order
and escapes stay exactly as they are (04c adds the token comparison and the save diff here)."""

import re

# A JSON string (kept whole), or a run of whitespace between tokens (dropped). JSON strings hold no raw line breaks.
_STRING_OR_SPACE = re.compile(r'("(?:[^"\\]|\\.)*")|[ \t\n\r]+')


def one_line(text: str) -> str:
    """`text` without the whitespace between its tokens: one line for JSON Lines, tokens unchanged (R-04-15)."""
    return _STRING_OR_SPACE.sub(lambda m: m.group(1) or "", text)
```

- [ ] **Step 4: Implement `sql/formatting.py`.**

```python
@dataclass(frozen=True, slots=True)
class _Json:
    """A JSON value as its text (Couchbase objects, arrays, exact numbers): written as it is, never re-encoded."""

    text: str


class _MissingCell:
    """SQL++ MISSING: the row has no such key. Empty in text formats, a left-out key in JSON (R-04-8)."""

    __slots__ = ()


_MISSING_CELL = _MissingCell()
Cell = None | bool | int | float | str | _Binary | _Truncated | _Json | _MissingCell
_UNSUPPORTED: dict[tuple[FormatKind, Dialect], str] = {
    # SQL++ INSERT takes a key and a document, not columns (spec §3.2.4)
    (FormatKind.INSERT, Dialect.SQLPP): "INSERT statements are not offered for Couchbase",
}
RAW_JSON_FORMATS: frozenset[FormatKind] = frozenset({FormatKind.JSON, FormatKind.JSONL})


def unsupported_reason(kind: FormatKind, dialect: Dialect) -> str | None:
    """Why `kind` cannot be produced for `dialect` (copy and export alike); None when it can."""
    return _UNSUPPORTED.get((kind, dialect))
```

  - `_normalise_raw`: first `if isinstance(value, JsonText): return _Json(value.text)` and `if value is MISSING: return _MISSING_CELL`.
  - `_normalise_encoded`: `case "json":` → `text = value.get("text")`, `_MalformedCell("a json cell needs its text")` unless `str`, else `_Json(text)`; `case "missing": return _MISSING_CELL`; the fallback message becomes "an object cell must be a bytes, text, json or missing cell".
  - `_display`: `case _Json(): return cell.text`, `case _MissingCell(): return ""`.
  - `_json_text`: first `if isinstance(cell, _Json): return cell.text`.
  - `_ObjectWriter._object`: skip the fields whose cell is `_MISSING_CELL`.
  - `_SqlLiterals.__call__`: `case _MissingCell(): return "MISSING"` and `case _Json(): return json_literal(self._dialect, cell.text)` before the text cases.
  - `value_condition`: after normalising, `if cell is _MISSING_CELL: return f"{name} IS MISSING"`, `if isinstance(cell, _Json): return f"{name} = {json_literal(dialect, cell.text)}"`.
  - `_in_list`: `text=in_list(dialect, distinct)`.
  - `format_rows`: first `if (reason := unsupported_reason(kind, dialect)) is not None: raise UnsupportedFormat(reason)`.
  - The raw writer:

```python
class RawJsonWriter:
    """JSON / JSONL export of rows that are already JSON text (`RawJsonRows`): each row exactly as the server sent it
    (R-04-15). A JSON Lines row must be one line, so a row holding a line break (whitespace a document was stored with)
    is squeezed outside its strings, tokens unchanged; a JSON array keeps every row's bytes."""

    def __init__(self, kind: FormatKind, fp: TextIO) -> None:
        if kind not in RAW_JSON_FORMATS:
            raise ValueError(f"{kind.value} is not a raw JSON format")
        self._lines = kind == FormatKind.JSONL
        self._fp = fp
        self._first = True
        if not self._lines:
            fp.write("[")

    def write(self, rows: Iterable[str]) -> None:
        for text in rows:
            if self._lines:
                self._fp.write((one_line(text) if "\n" in text or "\r" in text else text) + "\n")
            else:
                self._fp.write(("\n  " if self._first else ",\n  ") + text)
            self._first = False

    def close(self) -> None:
        if not self._lines:
            self._fp.write("]\n" if self._first else "\n]\n")


def open_raw_json_writer(kind: FormatKind, fp: TextIO) -> RawJsonWriter:
    return RawJsonWriter(kind, fp)
```

  (`from ..adapters.couchbase.jsontext import one_line`; `__all__` gains the new public names.)

- [ ] **Step 5: Run everything**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green; the existing writer tests pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase backend/dbaguy_backend/sql/formatting.py \
  backend/tests/sql/test_formatting.py backend/tests/test_couchbase_literals.py
git commit -m "feat(sql): JSON and MISSING cells in copy and export, SQL++ IN lists, no SQL++ INSERT, raw JSON/JSONL writer"
```

---

### Task 6: Worker: option checks, idle close of paused results, raw JSON export, query context in the history

**Files:**
- Create: `backend/dbaguy_backend/sessions/options.py`
- Modify: `backend/dbaguy_backend/sessions/worker.py`, `sessions/hooks.py`, `sessions/manager.py`, `backend/dbaguy_backend/container.py`
- Test: create `backend/tests/test_run_options.py`; modify `backend/tests/test_session_worker.py`, `backend/tests/test_session_wiring.py`

**Interfaces:**
- Consumes: `RunOptions`, `RawJsonRows`, `AdapterCapabilities` (new fields), `presentation()`, `QueryContext`, `StatementParams`, `ExecuteCommand`/`ExportCommand` fields (Task 4); `RAW_JSON_FORMATS`, `open_raw_json_writer`, `unsupported_reason` (Task 5).
- Produces:
  - `sessions/options.py`: `MAX_PARAM_BYTES = 1024 * 1024`; `check_params(params: StatementParams) -> None`; `run_options(command: ExecuteCommand | ExportCommand, capabilities: AdapterCapabilities) -> RunOptions` — raises `StatementError(code="unsupported_option")` (`"This engine does not take a query context"` / `"… parameters"` / `"… an executed plan"`) or `StatementError(code="invalid_params")` naming `$name` / `$n`, never the value; an empty `StatementParams` counts as none; `retry_safe` is left False (the worker sets it after the policy).
  - `WorkerHooks.capabilities: AdapterCapabilities = AdapterCapabilities()`; `SessionHooksFactory.build` fills it from the registry.
  - `ExecutionRecord.query_context: QueryContext | None = None`; for engines with `capabilities.query_context`, the history's `database` is `record.query_context.text()` (None: no context), for the others the session's database as before.
  - `SessionWorker(name, opener, clock: Callable[[], float] = time.monotonic)`; `SessionWorker.check_paused_result() -> None` (any thread); `paused_result_closed_text(limit_s: float) -> str` (module function).
  - `SessionManager.check_paused_results() -> None`; the container's housekeeping calls it every 15 s.
  - The worker sends `ResultSetEvent.presentation = result.presentation()`; passes `RunOptions` to `run()` with `retry_safe = (kind == READ)` for execute and `True` for export; refuses an export format the dialect cannot produce (`unsupported_reason` → `StatementError(code="unsupported_format")`); writes JSON/JSONL exports of a `RawJsonRows` result with `open_raw_json_writer` and `fetch_raw()`.

Idle close (R-04-6, D-04-12, spec §3.7.4): `_pause()` records `_PausedMark(token, request_id, since)` under `_state_lock` right after it leaves the running state (before any event); `_set_running(<a request>)`, `_finish()` and `_fail()` clear it (`_set_running(None)` does not: `_execute`'s `finally` calls it after a pause). `check_paused_result()` reads the mark under the lock and, past the limit, queues `_close_idle_paused(mark, limit)`; on the worker thread that closes only if the mark is still the same and the open execution is that request — so a Load more or a new statement that came first wins, and one that comes after gets `no_open_result`.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_run_options.py`:

```python
import pytest

from dbaguy_backend.adapters.base import AdapterCapabilities, RunOptions, StatementError
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand, QueryContext, StatementParams
from dbaguy_backend.sessions.options import run_options

CB = AdapterCapabilities(query_context=True, parameters=True)
CONTEXT = QueryContext(bucket="dbaguy", scope="app")


def execute(**fields):
    return ExecuteCommand(request_id="r", sql="SELECT 1", **fields)


def test_plain_requests_take_no_options():
    assert run_options(execute(), AdapterCapabilities()) == RunOptions()
    assert run_options(execute(params=StatementParams()), AdapterCapabilities()) == RunOptions()  # empty = none


@pytest.mark.parametrize("fields, words", [
    ({"query_context": CONTEXT}, "query context"),
    ({"params": StatementParams(named={"a": "1"})}, "parameters"),
    ({"profile_plan": True}, "executed plan"),
])
def test_options_the_engine_does_not_declare_are_refused(fields, words):
    with pytest.raises(StatementError, match=words) as error:
        run_options(execute(**fields), AdapterCapabilities())
    assert error.value.code == "unsupported_option"


@pytest.mark.parametrize("params, label, value", [
    (StatementParams(named={"1x": "1"}), "$1x", None),
    (StatementParams(named={"n": "abc"}), "$n", "abc"),
    (StatementParams(named={"n": "NaN"}), "$n", "NaN"),
    (StatementParams(named={"n": '"\ud800"'}), "$n", None),
    (StatementParams(positional=["1", "[secret"]), "$2", "secret"),
    (StatementParams(named={"n": '"' + "x" * (1024 * 1024) + '"'}), "$n", "xxxx"),
])
def test_invalid_params_name_the_parameter_never_the_value(params, label, value):
    with pytest.raises(StatementError) as error:
        run_options(execute(params=params), CB)
    assert error.value.code == "invalid_params" and label in error.value.message
    assert value is None or value not in error.value.message


def test_valid_params_pass_unchanged():
    params = StatementParams(
        named={"n": "9007199254740993", "big": "1" * 5000, "s": '"中文"'}, positional=['[1, {"a": null}]', " true "],
    )
    assert run_options(execute(params=params, query_context=CONTEXT), CB) == RunOptions(query_context=CONTEXT, params=params)


def test_an_export_takes_the_same_context_and_params():
    command = ExportCommand(request_id="e", sql="SELECT 1", format="csv", path="/tmp/x.csv", query_context=CONTEXT)
    assert run_options(command, CB).query_context == CONTEXT
```

Append to `tests/test_session_worker.py` (imports: `dataclasses.replace` not needed; `AdapterCapabilities`, `RawJsonRows`, `RunOptions` from `dbaguy_backend.adapters.base`; `QueryContext`, `StatementParams` from the execution domain; `FakeResult` from `tests.fake_db`; `Dialect`):

```python
CONTEXT = QueryContext(bucket="dbaguy", scope="app")


def _policy(sql, confirmed):  # `rows …` reads; anything else writes
    kind = StatementKind.READ if sql.startswith("rows") else StatementKind.WRITE
    return GuardDecision(action=GuardAction.RUN, classification=Classification(kind=kind))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_worker(connection=None, clock=None, recorder=None, **capabilities):
    lease = FakeLease(connection)
    worker = SessionWorker("fake", lambda: lease, clock=clock or Clock())
    worker.start()
    worker.configure(WorkerHooks(check=_policy, capabilities=AdapterCapabilities(**capabilities)))
    recorder = recorder or Recorder()
    worker.set_emitter(recorder)
    return worker, lease.connection, recorder


def test_unsupported_options_never_reach_the_connection():
    worker, conn, rec = make_worker()
    try:
        worker.submit(ExecuteCommand(request_id="c", sql="rows 1", query_context=CONTEXT))
        rec.wait_for(finished("c"))
        assert rec.of("c", "error")[0].code == "unsupported_option" and conn.runs == []
    finally:
        worker.close()


def test_options_and_retry_safety_reach_run():
    worker, conn, rec = make_worker(query_context=True, parameters=True)
    try:
        params = StatementParams(named={"n": "1"})
        worker.submit(ExecuteCommand(request_id="r", sql="rows 1", query_context=CONTEXT, params=params))
        rec.wait_for(finished("r"))
        worker.submit(ExecuteCommand(request_id="w", sql="begin"))
        rec.wait_for(finished("w"))
        assert conn.options == [RunOptions(query_context=CONTEXT, params=params, retry_safe=True), RunOptions()]
        worker.submit(ExecuteCommand(request_id="bad", sql="rows 1", params=StatementParams(positional=["x"])))
        rec.wait_for(finished("bad"))
        assert rec.of("bad", "error")[0].code == "invalid_params" and len(conn.runs) == 2
    finally:
        worker.close()


class PlanConnection(FakeConnection):
    def run(self, sql, options=None):
        result = super().run(sql, options)
        result.presentation = lambda: "plan"
        return result


def test_presentation_reaches_the_result_set_event():
    worker, _, rec = make_worker(PlanConnection())
    try:
        execute(worker, "x", "rows 1")
        rec.wait_for(finished("x"))
        assert rec.of("x", "result_set")[0].presentation == "plan"
    finally:
        worker.close()


def test_the_history_record_carries_the_requests_query_context():  # P37: `_complete` fills ExecutionRecord.query_context
    worker, _, rec = make_worker(query_context=True)
    records = []
    worker.configure(WorkerHooks(check=_policy, capabilities=AdapterCapabilities(query_context=True),
                                 on_record=records.append))
    try:
        worker.submit(ExecuteCommand(request_id="h", sql="rows 1", query_context=CONTEXT))
        rec.wait_for(finished("h"))
        worker.submit(ExecuteCommand(request_id="n", sql="rows 1"))  # no context: the record says so
        rec.wait_for(finished("n"))
        assert [r.query_context for r in records] == [CONTEXT, None]
    finally:
        worker.close()


CLOSED = ("Closed this result after 2 min without loading more rows, to free the query node — run the statement again "
          "to load more")


def test_an_idle_paused_result_is_closed_after_the_limit():
    clock = Clock()
    worker, _, rec = make_worker(clock=clock, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        clock.now += 119
        worker.check_paused_result()  # below the limit it queues nothing, so this is already settled
        assert not rec.of("p", "finished")
        clock.now += 2
        worker.check_paused_result()
        rec.wait_for(finished("p"))
        assert rec.of("p", "message")[-1].text == CLOSED  # loaded rows stay; the client marks the set partial
        worker.submit(FetchMoreCommand(request_id="p"))
        rec.wait_for(lambda e: e.type == "error" and e.code == "no_open_result")
    finally:
        worker.close()


def test_engines_without_a_limit_keep_paused_results():
    clock = Clock()
    worker, _, rec = make_worker(clock=clock)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        clock.now += 10_000
        worker.check_paused_result()
        worker.submit(FetchMoreCommand(request_id="p"))
        rec.wait_for(paused("p", 1000))
    finally:
        worker.close()


@pytest.mark.parametrize("fetch_first", [True, False])
def test_idle_close_and_fetch_more_race_either_way(fetch_first):  # Review Focus 3
    clock = Clock()
    rec = HoldingRecorder(lambda e: e.type == "result_set_end" and e.has_more)
    worker, _, _ = make_worker(clock=clock, recorder=rec, paused_result_max_idle_s=120.0)
    try:
        execute(worker, "p", "rows 900")
        assert rec.holding.wait(5)  # the worker thread is inside the pause; its mark is set
        clock.now += 121
        if fetch_first:
            worker.submit(FetchMoreCommand(request_id="p"))
            worker.check_paused_result()
        else:
            worker.check_paused_result()
            worker.submit(FetchMoreCommand(request_id="p"))
        rec.proceed.set()
        worker.call(lambda c: None)  # a barrier: both queued tasks have run
        texts = [e.text for e in rec.of("p", "message")]
        if fetch_first:  # the Load more won: every row arrived and the close found nothing to do
            assert sum(len(e.rows) for e in rec.of("p", "rows")) == 900 and CLOSED not in texts
            assert len(rec.of("p", "finished")) == 1
        else:  # the close won: the Load more finds no open result
            assert CLOSED in texts and rec.of("p", "error")[0].code == "no_open_result"
    finally:
        worker.close()


class RawResult(FakeResult, RawJsonRows):
    def fetch_raw(self, n):
        taken, self._rows = self._rows[:n], self._rows[n:]
        return [f'{{"n": {value},\n "s": "a b"}}' for value in taken]


class RawConnection(FakeConnection):
    def run(self, sql, options=None):
        self.runs.append(sql)
        self.options.append(options)
        return RawResult(int(sql.split()[1]))


def test_a_raw_json_export_writes_the_server_text(tmp_path):
    worker, conn, rec = make_worker(RawConnection())
    try:
        target = tmp_path / "out.jsonl"
        worker.submit(ExportCommand(request_id="j", sql="rows 3", format="jsonl", path=str(target)))
        rec.wait_for(finished("j"))
        assert target.read_text().splitlines() == ['{"n":0,"s":"a b"}', '{"n":1,"s":"a b"}', '{"n":2,"s":"a b"}']
        assert conn.options[-1].retry_safe  # an export is a read
        csv = tmp_path / "out.csv"
        worker.submit(ExportCommand(request_id="c", sql="rows 2", format="csv", path=str(csv)))
        rec.wait_for(finished("c"))
        assert csv.read_text() == "n\n0\n1\n"  # other formats go by the columns
    finally:
        worker.close()


def test_an_export_format_the_dialect_cannot_write_is_refused(tmp_path):
    worker, conn, rec = make_worker()
    worker.configure(WorkerHooks(dialect=Dialect.SQLPP, check=_policy))
    try:
        worker.submit(ExportCommand(request_id="i", sql="rows 1", format="insert", path=str(tmp_path / "x.sql")))
        rec.wait_for(finished("i"))
        assert rec.of("i", "error")[0].code == "unsupported_format" and conn.runs == []
    finally:
        worker.close()
```

Append to `tests/test_session_wiring.py`:

```python
class ContextSqlite(SqliteAdapter):  # an engine with a per-request query context, as Couchbase
    capabilities = AdapterCapabilities(query_context=True)


@pytest.mark.parametrize("context, shown", [(QueryContext(bucket="travel-sample", scope="inventory"), "travel-sample.inventory"),
                                            (None, None)])
def test_the_history_keeps_the_request_query_context(env, context, shown):
    _, _, pid, history, cache, manager = env
    hooks = SessionHooksFactory(AdapterRegistry([ContextSqlite()]), manager._profiles, SafetyPolicy(), history, cache)
    record = ExecutionRecord(request_id="r", sql="SELECT 1", started_at=datetime.now(UTC), elapsed_ms=1, rows=1,
                             status="ok", query_context=context)
    hooks.build(pid, "the session's database").on_record(record)
    assert history.search().items[0].database == shown


def test_other_engines_keep_the_session_database(env):
    _, _, pid, history, cache, manager = env
    hooks = SessionHooksFactory(AdapterRegistry([SqliteAdapter()]), manager._profiles, SafetyPolicy(), history, cache)
    record = ExecutionRecord(request_id="r", sql="SELECT 1", started_at=datetime.now(UTC), elapsed_ms=1, rows=1,
                             status="ok", query_context=QueryContext(bucket="ignored"))
    hooks.build(pid, "main").on_record(record)
    assert history.search().items[0].database == "main"
```

(Imports: `datetime`, `UTC`, `AdapterCapabilities`, `QueryContext`, `ExecutionRecord` from `dbaguy_backend.sessions.worker`.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_run_options.py tests/test_session_worker.py tests/test_session_wiring.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.sessions.options`, then `TypeError: … unexpected keyword argument 'clock'`.

- [ ] **Step 3: Implement `sessions/options.py`.**

```python
"""Per-request options of execute and export, checked before anything runs (spec §3.2.1–§3.2.2): an engine only ever
receives options its capabilities declare, and parameters that are valid JSON literals under valid names. Messages
name the parameter, never its value (spec §4)."""

import json
import re
from decimal import Decimal
from typing import NoReturn

from ..adapters.base import AdapterCapabilities, RunOptions, StatementError
from ..domain.execution import ExecuteCommand, ExportCommand, StatementParams

MAX_PARAM_BYTES = 1024 * 1024
UNSUPPORTED_OPTION = "unsupported_option"
INVALID_PARAMS = "invalid_params"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(name)


def _invalid(text: str) -> StatementError:
    return StatementError(text, code=INVALID_PARAMS)


def _check_literal(label: str, literal: str) -> None:
    try:
        size = len(literal.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid(f"Parameter {label} holds a character that is not valid text") from None
    if size > MAX_PARAM_BYTES:
        raise _invalid(f"Parameter {label} is larger than 1 MiB")
    try:  # parse_int=str: an integer beyond Python's digit limit is still a literal the server reads
        json.loads(literal, parse_int=str, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _invalid(f'Parameter {label} is not a JSON value (a string needs double quotes: "text")') from None


def check_params(params: StatementParams) -> None:
    for name, literal in params.named.items():
        if not _NAME.fullmatch(name):
            raise _invalid(f"${name[:40]} is not a parameter name: use letters, digits and _")
        _check_literal(f"${name}", literal)
    for index, literal in enumerate(params.positional, start=1):
        _check_literal(f"${index}", literal)


def run_options(command: ExecuteCommand | ExportCommand, capabilities: AdapterCapabilities) -> RunOptions:
    """The options `run()` gets for `command`; raises StatementError (unsupported_option / invalid_params)."""
    context = command.query_context
    params = command.params if command.params is not None and (command.params.named or command.params.positional) else None
    profile_plan = isinstance(command, ExecuteCommand) and command.profile_plan
    if context is not None and not capabilities.query_context:
        raise StatementError("This engine does not take a query context", code=UNSUPPORTED_OPTION)
    if params is not None and not capabilities.parameters:
        raise StatementError("This engine does not take parameters", code=UNSUPPORTED_OPTION)
    if profile_plan and not capabilities.executed_plan:
        raise StatementError("This engine does not take an executed plan", code=UNSUPPORTED_OPTION)
    if params is not None:
        check_params(params)
    return RunOptions(query_context=context, params=params, profile_plan=profile_plan)
```

- [ ] **Step 4: Implement the worker changes** in `sessions/worker.py`:
  - `ExecutionRecord.query_context: QueryContext | None = None`; `_Request.query_context: QueryContext | None = None`; `WorkerHooks.capabilities: AdapterCapabilities = AdapterCapabilities()`.
  - `_complete` (the `ExecutionRecord(...)` it hands to `on_record`): add `query_context=req.query_context` (P37). Without it the history never receives the context: the hook tests call `on_record()` directly and would still pass.
  - `_PausedMark` (frozen dataclass: `token: int`, `request_id: str`, `since: float`); in `__init__`: `self._clock = clock`, `self._paused: _PausedMark | None = None` (guarded by `_state_lock`, listed in the class docstring's locking paragraph), `self._pause_tokens = itertools.count(1)`.
  - `_execute`, inside the `try`, after `_check_text`:

```python
            options = run_options(cmd, self._hooks.capabilities)  # before the policy: a refused request never asks
            if not self._passes_policy(req, cmd.confirmed):
                return
            self._raise_if_cancelled()
            logger.debug("[{}] execute {}: {}", self.name, cmd.request_id, cmd.sql[:2000])
            if options.params is not None:  # names only, never values (spec §5)
                logger.debug("[{}] {} parameters: {}", self.name, cmd.request_id, _param_names(options.params))
            req.ran = True
            req.query_context = options.query_context
            options = replace(options, retry_safe=req.kind == StatementKind.READ)  # D-04-14
            with self._timeout(req):
                self._listen(req.request_id)
                result = self.connection.run(cmd.sql, options)
```

    with `def _param_names(params: StatementParams) -> str: return ", ".join([*(f"${n}" for n in params.named), *(f"${i}" for i in range(1, len(params.positional) + 1))])`.
  - `_export`: inside the `try` after `_check_text`: `options = replace(run_options(cmd, self._hooks.capabilities), retry_safe=True)  # an export is always a read`; `_write_export(cmd, target, req, options)`.
  - `_check_export`, first: `if (reason := unsupported_reason(cmd.format, self._hooks.dialect)) is not None: raise StatementError(reason, code="unsupported_format")`.
  - `_write_export` runs `self.connection.run(cmd.sql, options)` and chooses the writer:

```python
            raw = isinstance(result, RawJsonRows) and cmd.format in RAW_JSON_FORMATS
            with atomic_text_file(target) as fp:
                if raw:  # Couchbase JSON / JSONL: the server's row text (R-04-15)
                    writer: RowWriter | RawJsonWriter = open_raw_json_writer(cmd.format, fp)
                    fetch: Callable[[int], list[Any]] = result.fetch_raw  # type: ignore[attr-defined]
                else:
                    writer = open_writer(cmd.format, fp, [FormatColumn(name=c.name, category=c.category) for c in columns],
                                         cmd.options, self._hooks.dialect)
                    fetch = result.fetch
                while True:  # the existing loop, now over `fetch`
                    rows = fetch(_EXPORT_CHUNK)
                    self._raise_if_cancelled()
                    if not rows:
                        break
                    writer.write(rows)
                    before, written = written, written + len(rows)
                    req.rows = written
                    if written // _PROGRESS_EVERY > before // _PROGRESS_EVERY:
                        self._send(ProgressEvent(request_id=cmd.request_id, rows=written))
                writer.close()
```

  - `_pump`: `ResultSetEvent(request_id=ex.request_id, index=ex.index, columns=columns, presentation=result.presentation())`.
  - `_pause`, right after `if self._set_running(None): … return`:

```python
        with self._state_lock:  # before any event: the idle check may start counting now
            self._paused = _PausedMark(next(self._pause_tokens), ex.request_id, self._clock())
```

  - `_set_running`: inside the lock, `if request_id is not None: self._paused = None` (a Load more or a new request ends the idle stretch; `_set_running(None)` after a pause must not). `_finish` and `_fail` start with `with self._state_lock: self._paused = None`.
  - New methods:

```python
    def check_paused_result(self) -> None:
        """Close the paused result once it has waited for Load more longer than the engine allows (capability
        `paused_result_max_idle_s`; Couchbase: a paused request holds a query servicer, R-04-6). Any thread; the close
        itself runs on the worker thread."""
        limit = self._hooks.capabilities.paused_result_max_idle_s
        if limit is None:
            return
        with self._state_lock:
            mark = self._paused
        if mark is None or self._clock() - mark.since < limit:
            return
        try:
            self._put(lambda: self._close_idle_paused(mark, limit))
        except SessionBusy:
            pass  # closing: the worker finishes the open result itself

    def _close_idle_paused(self, mark: _PausedMark, limit: float) -> None:
        with self._state_lock:
            still = self._paused == mark
        execution = self._open_exec
        if not still or execution is None or execution.request_id != mark.request_id:
            return  # a Load more, a new statement or a discard came first: that result is not idle any more
        logger.info("[{}] closed paused result {} after {:.0f} s without Load more", self.name, mark.request_id, limit)
        self._send(MessageEvent(
            request_id=mark.request_id, severity=MessageSeverity.WARNING, text=paused_result_closed_text(limit),
        ))
        self._finish(execution)
```

```python
def paused_result_closed_text(limit_s: float) -> str:
    return (f"Closed this result after {limit_s / 60:g} min without loading more rows, to free the query node — run the "
            "statement again to load more")
```

- [ ] **Step 5: Hooks, manager, container.**
  - `sessions/hooks.py` `build`: `adapter = self._registry.get(profile.engine)`; `dialect, capabilities = adapter.dialect, adapter.capabilities`; in `on_record`: `shown = (record.query_context.text() if record.query_context else None) if capabilities.query_context else database` (comment: "engines with a per-request query context keep that request's context, spec §3.2.3; the others the session's database"), and `capabilities=capabilities` in the returned `WorkerHooks`.
  - `sessions/manager.py`:

```python
    def check_paused_results(self) -> None:
        """Close paused results that waited too long (engines that set a limit); one failing session does not stop the
        rest."""
        with self._lock:
            entries = list(self._sessions.items())
        for session_id, entry in entries:
            try:
                entry.worker.check_paused_result()
            except Exception:
                logger.exception("Paused-result check failed for session {}", session_id)
```

  - `container.py` `_housekeep_once`: add `self._guarded("Paused-result check", self.sessions.check_paused_results)`; the interval comment becomes "also the resolution of the idle-transaction warning and rollback, and of closing idle paused results".

- [ ] **Step 6: Run everything**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green; the existing worker, export and history tests pass unchanged.

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/sessions backend/dbaguy_backend/container.py backend/tests/test_run_options.py \
  backend/tests/test_session_worker.py backend/tests/test_session_wiring.py
git commit -m "feat(sessions): per-request options checked against capabilities, idle close of paused results, raw JSON export, query context in the history"
```

---

### Task 7: The query service over HTTP and the incremental response parser

**Files:**
- Create: `backend/dbaguy_backend/adapters/couchbase/stream.py`, `backend/dbaguy_backend/adapters/couchbase/http.py`
- Create: `backend/tests/couchbase_fakes.py`, `backend/tests/test_couchbase_stream.py`, `backend/tests/test_couchbase_http.py`

**Interfaces:**
- Consumes: `enable_keepalive` (Task 4); `COUCHBASE_HOST`, `COUCHBASE_PORT`, `requires_couchbase` (Task 1).
- Produces (`adapters/couchbase/stream.py`, pure):
  - `JsonKind = Literal["string", "number", "boolean", "null", "object", "array"]`; `json_kind(text: str) -> JsonKind` (from the first character).
  - `@dataclass(frozen=True, slots=True) RowField(key: str, text: str, value: Any)` — `text` is the value's exact text; `value` is decoded with numbers left as text, so it is only meaningful for strings, true/false/null.
  - `@dataclass(frozen=True, slots=True) RawRow(text: str, value: Any, fields: tuple[RowField, ...] | None)` — `fields`: the members of an object row in order (then `value` is None); None for any other row (then `value` as in `RowField`).
  - Events (frozen dataclasses): `FieldEvent(name: str, value: Any)` (a top-level field other than `results`, decoded normally), `ResultsStart()`, `RowEvent(row: RawRow)`, `ResultsEnd()`, `EndEvent()`; `Event` is their union.
  - `class ProtocolError(Exception)` with `offset: int` (characters read), `field: str | None`, `truncated: bool` (the response ended before its object closed).
  - `class ResponseParser` with `feed(data: bytes) -> list[Event]`, `finish() -> list[Event]` (the body ended; `ProtocolError(truncated=True)` when the object never closed), `offset: int` and `attempts: int` (value parses started; tests read it). A failure is raised by the call that meets it when nothing parsed before it in that call; otherwise those events are returned and every later call raises it (rows before a bad spot are not lost).
  - `split_object(text: str) -> tuple[RowField, ...]` — the members of one complete JSON object text; `ValueError` for anything else.
- Produces (`adapters/couchbase/http.py`):
  - `READ_CHUNK = 64 * 1024`, `CONNECT_TIMEOUT_S = 10.0`, `KEEPALIVE_REUSE_S = 30.0`, `QUERY_PATH = "/query/service"`.
  - `client_context_id(ident: str) -> str` (`"dbaguy-" + ident`, every character outside `[A-Za-z0-9_-]` removed, cut to 64; takes any id string: Task 11 passes `uuid4().hex`, P30, P47); `basic_auth(user: str, password: str) -> str`.
  - `@dataclass(frozen=True) QueryRequest(statement: str, client_context_id: str, query_context: str | None = None, readonly: bool = False, named: Mapping[str, str] = field(default_factory=dict), positional: Sequence[str] = (), profile: str | None = None, preserve_expiry: bool = False)` (a `{}` default raises `ValueError: mutable default` when the class is created, P39); `build_body(request: QueryRequest) -> bytes` (raises `UnicodeEncodeError` for a lone surrogate).
  - `class ChannelError(Exception)` with `before_response: bool`, `tls: bool`; `failure_reason(exc: BaseException) -> str` (`refused`, `timed out`, `unknown host`, or the error's own text; Task 9's discovery uses it too).
  - `SocketFactory = Callable[[str, int, float], socket.socket]`.
  - `class HttpChannel` — `shutdown()` (any thread), `close()`, `reusable(now: float) -> bool`, attributes `host`, `port`, `used`, `idle_since`, `dead`.
  - `class QueryResponse` — `status: int`, `reason: str`, `content_type: str`, `done: bool`, `read_chunk() -> bytes` (`b""` at the end; raises `ChannelError`), `abandon()`.
  - `class NodeClient(host: str, port: int, *, auth: str, user_agent: str, tls: ssl.SSLContext | None, server_hostname: str | None = None, socket_factory: SocketFactory | None = None, clock: Callable[[], float] = time.monotonic)` — `label: str` (`host:port`, IPv6 in brackets), `send(body: bytes, *, retry_safe: bool, attach: Callable[[HttpChannel], None]) -> QueryResponse` (raises `ChannelError`, or whatever `attach` raises, before anything is sent), `close()`.
- Produces (`tests/couchbase_fakes.py`): `Reply(body: bytes, status: int = 200, content_type: str = "application/json", close: bool = False, drop_after: bool = False, hold: threading.Event | None = None)`; `FakeQueryServer(replies: list[Reply])` with `port`, `bodies: list[bytes]`, `connections: int`, `close()`; `FakeResponse(chunks: list[bytes], status: int = 200, content_type: str = "application/json")` with `read_chunk()`, `abandon()`, `done`, `abandoned`, `fail_with: Exception | None` (Task 11's unit tests use it).

Parser (spec §3.4.2; the survey's `httpq.py` prototype): a strict incremental UTF-8 decoder; a state machine over the top-level object (`start → key → colon → value → …`, with `results` read as `ResultsStart`, rows, `ResultsEnd`); a value is accepted only when something follows it (a number may go on in the next chunk); an object row is scanned member by member on the buffer (`json.decoder.scanstring` for keys, `JSONDecoder.raw_decode(s, idx)` for values), keeping each member's text; a value that is not complete yet is retried only once the buffered text from it has doubled, and new text is kept in a list of pieces joined only for a retry, so a 5 MB row costs about twice its size, never once per chunk. A decode error whose bad token runs to the end of what has arrived (an open string, `tru`, `1e`) means "wait for more" — at the end of the body, a response cut short (`truncated`); a bad token followed by more JSON is malformed at once.

- [ ] **Step 1: Write the fakes** — `backend/tests/couchbase_fakes.py`:

```python
"""Stand-ins for the Couchbase query service: a local HTTP/1.1 server with scripted replies, and a canned response."""

import socket
import threading
from dataclasses import dataclass


@dataclass
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "application/json"
    close: bool = False  # say `Connection: close` and close afterwards
    drop_after: bool = False  # close the connection after this reply without saying so (an idle keep-alive timeout)
    hold: threading.Event | None = None  # send the first half, then wait until set (a long query)


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n" if data else b""


class FakeQueryServer:
    """Serves each request with the next Reply, chunked and kept alive; counts connections and records request bodies."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.bodies: list[bytes] = []
        self.connections = 0
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:  # the whole loop (P45): a client that abandons a response (reset, broken pipe) leaves no thread warning
                reader = conn.makefile("rb")
                while reader.readline():  # the request line; empty when the client left
                    headers = {}
                    while (line := reader.readline()) not in (b"\r\n", b"\n", b""):
                        name, _, value = line.decode().partition(":")
                        headers[name.strip().lower()] = value.strip()
                    self.bodies.append(reader.read(int(headers.get("content-length", "0"))))
                    reply = self.replies.pop(0)
                    head = (f"HTTP/1.1 {reply.status} Reason\r\nContent-Type: {reply.content_type}\r\n"
                            f"Transfer-Encoding: chunked\r\n{'Connection: close' + chr(13) + chr(10) if reply.close else ''}\r\n")
                    half = len(reply.body) // 2
                    conn.sendall(head.encode() + _chunk(reply.body[:half]))
                    if reply.hold is not None:
                        reply.hold.wait(10)
                    conn.sendall(_chunk(reply.body[half:]) + b"0\r\n\r\n")
                    if reply.close or reply.drop_after:
                        return
            except OSError:
                return

    def close(self) -> None:
        self._server.close()


class FakeResponse:
    """A QueryResponse made of canned chunks (result tests without a server)."""

    def __init__(self, chunks: list[bytes], status: int = 200, content_type: str = "application/json") -> None:
        self._chunks = list(chunks)
        self.status, self.reason, self.content_type = status, "Reason", content_type
        self.done = False
        self.abandoned = False
        self.fail_with: Exception | None = None

    def read_chunk(self) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        if not self._chunks:
            self.done = True
            return b""
        return self._chunks.pop(0)

    def abandon(self) -> None:
        self.abandoned = not self.done
```

- [ ] **Step 2: Write the failing parser tests** — `backend/tests/test_couchbase_stream.py`:

```python
import pytest

from dbaguy_backend.adapters.couchbase.stream import (
    EndEvent, FieldEvent, ProtocolError, ResponseParser, ResultsEnd, ResultsStart, RowEvent, RowField, json_kind,
    split_object,
)

FULL = (
    '{"requestID":"r1","clientContextID":"dbaguy-x","signature":{"id":"json","*":"*"},"results":[\n'
    '{"id":"e1","d":{"name":"中文😀","n":9007199254740993,"pi":3.141592653589793238462643383279,"tags":["a"]}},\n'
    '{"id":"e2","d":{"name":"b","x":[1,{"y":1e400}]}},\n'
    '{"id":"e3","d":"just a string"}\n'
    '],"errors":[{"code":1080,"msg":"Timeout 3s exceeded","retry":true}],"status":"fatal",'
    '"metrics":{"elapsedTime":"3.1s","resultCount":3},"unknownField":{"a":[1,2]}}\n'
).encode()
RAW = (b'{"signature":"json","results":[1e400, -0.0, 12345678901234567890, "x", null, true, [1, 2], {"a": {}}],'
       b'"status":"success"}')
EMPTY = b'{"requestID":"r2","signature":{"$1":"number"},"results":[],"status":"success","metrics":{"resultCount":0}}'
NO_RESULTS = (b'{"requestID":"r3","errors":[{"code":3000,"msg":"syntax error - line 1, column 8","line":1,"column":8}],'
              b'"status":"fatal"}')


def parse(data: bytes, cuts: list[int]) -> list:
    parser, events, last = ResponseParser(), [], 0
    for cut in cuts:
        events += parser.feed(data[last:cut])
        last = cut
    events += parser.feed(data[last:])
    return events + parser.finish()


@pytest.mark.parametrize("data", [FULL, RAW, EMPTY, NO_RESULTS])
def test_every_split_position_gives_the_same_events(data):
    whole = parse(data, [])
    for cut in range(len(data) + 1):  # inside multi-byte UTF-8, inside numbers, between a value and its comma …
        assert parse(data, [cut]) == whole, cut


def test_one_byte_at_a_time():
    assert parse(FULL, list(range(1, len(FULL)))) == parse(FULL, [])


def test_events_of_a_full_response():
    events = parse(FULL, [])
    assert events[:4] == [
        FieldEvent("requestID", "r1"), FieldEvent("clientContextID", "dbaguy-x"),
        FieldEvent("signature", {"id": "json", "*": "*"}), ResultsStart(),
    ]
    rows = [e.row for e in events if isinstance(e, RowEvent)]
    assert [r.fields[0].text for r in rows] == ['"e1"', '"e2"', '"e3"']
    assert rows[0].fields[1].text == (
        '{"name":"中文😀","n":9007199254740993,"pi":3.141592653589793238462643383279,"tags":["a"]}'
    )
    assert rows[0].text.startswith('{"id":"e1","d":{') and rows[0].value is None
    assert rows[2].fields[1].value == "just a string"
    assert events[-6:] == [
        ResultsEnd(), FieldEvent("errors", [{"code": 1080, "msg": "Timeout 3s exceeded", "retry": True}]),
        FieldEvent("status", "fatal"), FieldEvent("metrics", {"elapsedTime": "3.1s", "resultCount": 3}),
        FieldEvent("unknownField", {"a": [1, 2]}), EndEvent(),
    ]


def test_errors_after_rows_come_after_them():  # Review Focus 2
    events = parse(FULL, [])
    last_row = max(i for i, e in enumerate(events) if isinstance(e, RowEvent))
    errors = next(i for i, e in enumerate(events) if isinstance(e, FieldEvent) and e.name == "errors")
    assert errors > last_row


def test_scalar_rows_keep_their_text():
    rows = [e.row for e in parse(RAW, []) if isinstance(e, RowEvent)]
    assert [r.text for r in rows] == ["1e400", "-0.0", "12345678901234567890", '"x"', "null", "true", "[1, 2]", '{"a": {}}']
    assert (rows[3].value, rows[4].value, rows[5].value, rows[0].fields) == ("x", None, True, None)
    assert rows[7].fields == (RowField("a", "{}", {}),)


@pytest.mark.parametrize("data, truncated", [
    (b"\xff", False), (b"<html>bad gateway</html>", False), (b'{"a":1} x', False), (b'{"results":[NaN]}', False),
    (FULL[:-10], True), (b"", True),
])
def test_malformed_or_cut_responses(data, truncated):
    with pytest.raises(ProtocolError) as error:
        parse(data, [])
    assert error.value.truncated == truncated


def test_results_that_are_not_an_array_are_a_field():
    assert FieldEvent("results", None) in parse(b'{"results":null,"status":"success"}', [])


def test_a_big_row_is_parsed_a_few_times_not_once_per_chunk():
    data = ('{"results":[{"blob":"' + "x" * 2_000_000 + '"}],"status":"success"}').encode()
    parser, events = ResponseParser(), []
    for i in range(0, len(data), 1024):  # about 1,950 chunks
        events += parser.feed(data[i:i + 1024])
    events += parser.finish()
    assert len([e for e in events if isinstance(e, RowEvent)]) == 1
    assert parser.attempts < 40


def test_split_object_keeps_member_texts():
    fields = split_object('{ "b" : [ 1 , 2 ] , "a":{"x":1e400},"s":"q\\"","n":-1.10 }')
    assert [(f.key, f.text) for f in fields] == [("b", "[ 1 , 2 ]"), ("a", '{"x":1e400}'), ("s", '"q\\""'), ("n", "-1.10")]
    assert fields[2].value == 'q"'
    for bad in ("[1]", '{"a":1} x', '{"a":', "1"):
        with pytest.raises(ValueError):
            split_object(bad)


def test_json_kind():
    assert [json_kind(t) for t in ('"a"', "{}", "[]", "true", "false", "null", "-1", "1e400")] == [
        "string", "object", "array", "boolean", "boolean", "null", "number", "number",
    ]
```

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_stream.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.stream`.

- [ ] **Step 4: Implement `stream.py`.** The data classes and events as listed under Interfaces, then:

```python
_WHITESPACE = " \t\n\r"
_KIND_BY_FIRST: dict[str, JsonKind] = {
    '"': "string", "{": "object", "[": "array", "t": "boolean", "f": "boolean", "n": "null",
}


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


# Rows: numbers stay text (no float, no int() digit limit); values are decoded only to find their end and read strings.
_ROW_DECODER = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=_reject_constant)
# Top-level metadata (signature, errors, warnings, status, metrics, profile): small, decoded as usual.
_META_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


class _Incomplete(Exception):
    """The buffer ends inside the value being read: wait for more."""


class _EndedEarly(ValueError):
    """The body ended inside a value: a cut response (a dropped connection), not a malformed one."""


def json_kind(text: str) -> JsonKind:
    return _KIND_BY_FIRST.get(text[:1], "number")


def _skip(s: str, i: int) -> int:
    while i < len(s) and s[i] in _WHITESPACE:
        i += 1
    return i


_DELIMITER = re.compile(r"[ \t\n\r,\]}]")  # what may follow a value


def _short(eof: bool, problem: str) -> Exception:
    """Running out of text: more may come, or (at the end of the body) the response was cut there."""
    return _EndedEarly(problem) if eof else _Incomplete()


def _failure(exc: json.JSONDecodeError, s: str, eof: bool) -> Exception:
    """A decode error means the text stops inside a value (an open string; `tru`, `-`, `{"a"` at the end of what has
    arrived) or is malformed (the bad token is followed by more JSON)."""
    if exc.msg.startswith("Unterminated") or not _DELIMITER.search(s, exc.pos):
        return _short(eof, "the text ends inside a value")
    return ValueError(f"malformed JSON ({exc.msg})")


def _decode(decoder: json.JSONDecoder, s: str, i: int, eof: bool) -> tuple[Any, int]:
    try:
        value, end = decoder.raw_decode(s, i)
    except json.JSONDecodeError as exc:
        raise _failure(exc, s, eof) from None
    if end < len(s) and s[end] not in " \t\n\r,]}":
        if _DELIMITER.search(s, end):
            raise ValueError("malformed JSON after a value")
        raise _short(eof, "the text ends inside a number")  # `1e` of `1e400`, cut between two chunks
    if end >= len(s) and not eof:
        raise _Incomplete()  # a number may go on in the next chunk
    return value, end


def _scan_object(s: str, start: int, eof: bool) -> tuple[tuple[RowField, ...], int]:
    """The members of the object at s[start] ("{") with their exact texts, and the index after its "}"."""
    fields: list[RowField] = []
    i = _skip(s, start + 1)
    if i < len(s) and s[i] == "}":
        return (), i + 1
    while True:
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] != '"':
            raise ValueError("expected a key")
        try:
            key, i = scanstring(s, i + 1)
        except json.JSONDecodeError as exc:
            raise _failure(exc, s, eof) from None
        i = _skip(s, i)
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] != ":":
            raise ValueError("expected a colon")
        i = _skip(s, i + 1)
        value, end = _decode(_ROW_DECODER, s, i, eof)
        fields.append(RowField(key, s[i:end], value))
        i = _skip(s, end)
        if i >= len(s):
            raise _short(eof, "an object ends early")
        if s[i] == "}":
            return tuple(fields), i + 1
        if s[i] != ",":
            raise ValueError("expected a comma")
        i = _skip(s, i + 1)


def split_object(text: str) -> tuple[RowField, ...]:
    """The members of one complete JSON object text, with their exact texts (alias expansion reads a document this
    way, Task 8). ValueError for anything else."""
    start = _skip(text, 0)
    if start >= len(text) or text[start] != "{":
        raise ValueError("not a JSON object")
    fields, end = _scan_object(text, start, eof=True)
    if _skip(text, end) != len(text):
        raise ValueError("text after the object")
    return fields


class ResponseParser:
    """See the module docstring. Not thread-safe: one per response, fed by the reading thread."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buf, self._pos = "", 0
        self._pieces: list[str] = []  # text received since the last attempt, joined only for the next one
        self._pieces_len = 0
        self._need = 0  # characters from `_pos` that must be buffered before the next attempt (the doubling rule)
        self._dropped = 0  # characters already cut from the front of the buffer
        self._state = "start"
        self._key: str | None = None
        self._failed: ProtocolError | None = None  # raised by every later call
        self.attempts = 0

    @property
    def offset(self) -> int:
        return self._dropped + self._pos

    def feed(self, data: bytes) -> list[Event]:
        self._raise_failure()
        return self._take(self._text(data, final=False), eof=False)

    def finish(self) -> list[Event]:
        self._raise_failure()
        events = self._take(self._text(b"", final=True), eof=True)
        if self._failed is None and self._state != "done":
            self._failed = ProtocolError("the response ended early", self.offset, self._key, truncated=True)
        if not events:
            self._raise_failure()
        return events  # a failure after these events is raised by the next call

    def _raise_failure(self) -> None:
        if self._failed is not None:
            raise self._failed

    def _text(self, data: bytes, final: bool) -> str:
        try:
            return self._decoder.decode(data, final=final)
        except UnicodeDecodeError:
            raise ProtocolError("the response is not UTF-8", self.offset, self._key) from None

    def _take(self, text: str, eof: bool) -> list[Event]:
        if text:
            self._pieces.append(text)
            self._pieces_len += len(text)
        if not eof and len(self._buf) - self._pos + self._pieces_len < self._need:
            return []  # still short of what the incomplete value needs: do not parse it again yet
        self._dropped += self._pos
        self._buf = self._buf[self._pos:] + "".join(self._pieces)
        self._pos, self._pieces, self._pieces_len, self._need = 0, [], 0, 0
        events: list[Event] = []
        try:
            while (i := _skip(self._buf, self._pos)) < len(self._buf):
                self._pos = i
                self._step(events, eof)
        except _Incomplete:
            self._need = 2 * (len(self._buf) - self._pos)
        except (ValueError, RecursionError) as exc:  # cut short (_EndedEarly), malformed JSON, NaN, absurd nesting
            self._failed = ProtocolError(str(exc), self.offset, self._key, truncated=isinstance(exc, _EndedEarly))
            if not events:
                raise self._failed from None
        return events  # what parsed before a failure goes out first; the next call raises it
```

`_step(events, eof)` handles one unit and moves `_pos` only when the unit is complete (a key, a colon, a field value, `[`, `]`, `,`, one row, `}`):
  - `start`: `{` → `key`; anything else → `ValueError("not a JSON object")`.
  - `key`: `}` → `done`, `EndEvent`; `,` → skip; `"` → `scanstring` (a `JSONDecodeError` → `_failure(exc, …)`), store `_key`, → `colon`; else `ValueError`.
  - `colon`: `:` → `value`; else `ValueError`.
  - `value`: when `_key == "results"` and the character is `[` → `ResultsStart`, → `row`; otherwise `self.attempts += 1`, `_decode(_META_DECODER, …)` → `FieldEvent(_key, value)`, → `key`.
  - `row`: `]` → `ResultsEnd`, → `key`; `,` → skip; else `self.attempts += 1`; a `{` → `_scan_object(…)` → `RawRow(text, None, fields)`, any other value → `_decode(_ROW_DECODER, …)` → `RawRow(text, value, None)`; `RowEvent`.
  - `done`: anything but whitespace → `ValueError("data after the end of the response")`.

- [ ] **Step 5: Run the parser tests**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_stream.py -q`
Expected: PASS (the every-split test runs about 2,000 parses; it takes a few seconds).

- [ ] **Step 6: Write the failing HTTP tests** — `backend/tests/test_couchbase_http.py`:

```python
import json
import threading
import time

import pytest

from dbaguy_backend.adapters.couchbase.http import (
    ChannelError, NodeClient, QueryRequest, basic_auth, build_body, client_context_id,
)
from tests.couchbase_fakes import FakeQueryServer, Reply
from tests.support import COUCHBASE_HOST, COUCHBASE_PORT, requires_couchbase

OK = b'{"results":[1],"status":"success"}'


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def node(server, clock=time.monotonic, auth=basic_auth("u", "p")):
    return NodeClient("127.0.0.1", server.port, auth=auth, user_agent="DbaGuy/test", tls=None, clock=clock)


def read_all(response) -> bytes:
    data = b""
    while chunk := response.read_chunk():
        data += chunk
    return data


def send(client, retry_safe=True, attach=lambda channel: None):
    return client.send(b'{"statement":"SELECT 1"}', retry_safe=retry_safe, attach=attach)


def test_body_embeds_literals_unchanged():
    body = build_body(QueryRequest(
        statement='SELECT $n, ? -- "中文"\n', client_context_id="dbaguy-r1", query_context="default:dbaguy.app",
        readonly=True, named={"n": "9007199254740993", "doc": '{"a": 1.10}'}, positional=["1e400", '"x"'],
    ))
    assert body.decode() == (
        '{"statement":"SELECT $n, ? -- \\"中文\\"\\n","client_context_id":"dbaguy-r1",'
        '"query_context":"default:dbaguy.app","readonly":true,"$n":9007199254740993,"$doc":{"a": 1.10},'
        '"args":[1e400,"x"]}'
    )
    assert json.loads(body)["$n"] == 9007199254740993  # still valid JSON
    assert b"timeout" not in body and b"scan_consistency" not in body  # never sent (R-04-5)


def test_a_lone_surrogate_cannot_be_sent():
    with pytest.raises(UnicodeEncodeError):
        build_body(QueryRequest(statement="SELECT '\ud800'", client_context_id="x"))


def test_client_context_ids():
    assert client_context_id('a/b"c 1-2_3') == "dbaguy-abc1-2_3"
    assert len(client_context_id("x" * 100)) == 64


def test_a_connection_is_reused_while_fresh():
    server = FakeQueryServer([Reply(OK), Reply(OK)])
    client = node(server)
    try:
        assert read_all(send(client)) == OK and read_all(send(client)) == OK
        assert server.connections == 1 and len(server.bodies) == 2
    finally:
        client.close()
        server.close()


def test_an_old_or_unfinished_connection_is_replaced():
    clock = Clock()
    server = FakeQueryServer([Reply(OK), Reply(OK), Reply(b'{"results":[' + b"1," * 50_000 + b'1]}'), Reply(OK)])
    client = node(server, clock)
    try:
        read_all(send(client))
        clock.now += 31  # idle for longer than KEEPALIVE_REUSE_S
        read_all(send(client))
        assert server.connections == 2
        unfinished = send(client)
        unfinished.read_chunk()
        unfinished.abandon()  # not read to the end: the connection is dropped
        read_all(send(client))
        assert server.connections == 3
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("retry_safe", [True, False])
def test_a_read_is_resent_once_when_the_kept_connection_was_closed(retry_safe):
    server = FakeQueryServer([Reply(OK, drop_after=True), Reply(OK)])
    client = node(server)
    try:
        read_all(send(client))
        time.sleep(0.1)  # the server has closed its end
        if retry_safe:
            assert read_all(send(client)) == OK and server.connections == 2
        else:
            with pytest.raises(ChannelError) as error:
                send(client, retry_safe=False)
            assert error.value.before_response
    finally:
        client.close()
        server.close()


def test_shutdown_from_another_thread_ends_a_blocked_read():  # Review Focus 3
    hold = threading.Event()
    server = FakeQueryServer([Reply(b'{"results":[' + b"1," * 1000 + b'1],"status":"success"}', hold=hold)])
    client = node(server)
    channels = []
    try:
        response = send(client, attach=channels.append)
        response.read_chunk()  # the first half; the server now waits
        threading.Timer(0.2, channels[0].shutdown).start()
        started = time.monotonic()
        with pytest.raises(ChannelError):
            read_all(response)
        assert time.monotonic() - started < 1.0
    finally:
        hold.set()
        client.close()
        server.close()


def test_attach_refusal_sends_nothing():  # Review Focus 3: a cancel before the request goes out
    server = FakeQueryServer([Reply(OK)])
    client = node(server)

    def refuse(channel):
        raise RuntimeError("cancelled")

    try:
        with pytest.raises(RuntimeError):
            send(client, attach=refuse)
        time.sleep(0.1)
        assert server.connections == 0 and server.bodies == []
    finally:
        client.close()
        server.close()


def test_a_non_json_error_page_is_reported_as_it_came():
    server = FakeQueryServer([Reply(b"<html>bad gateway</html>", status=502, content_type="text/html")])
    client = node(server)
    try:
        response = send(client)
        assert (response.status, response.content_type) == (502, "text/html")
    finally:
        client.close()
        server.close()


@requires_couchbase
@pytest.mark.parametrize("password", ["wrong", "dbaguy"])
def test_the_query_service_answers_errors_as_json(password):  # A14
    client = NodeClient(COUCHBASE_HOST, COUCHBASE_PORT, auth=basic_auth("dbaguy", password), user_agent="DbaGuy/test",
                        tls=None)
    try:
        response = client.send(build_body(QueryRequest("SELEC 1", "dbaguy-a14")), retry_safe=True,
                               attach=lambda channel: None)
        body = read_all(response)
        assert "application/json" in response.content_type and json.loads(body)["errors"]
        assert response.status == 401 if password == "wrong" else response.status != 401
    finally:
        client.close()
```

- [ ] **Step 7: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_http.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.http`.

- [ ] **Step 8: Implement `http.py`.** Module docstring:

```python
"""The query service over HTTP/1.1 with the standard library (spec §3.4.1): one keep-alive connection at a time to one
query node, request bodies assembled as text (parameter literals embedded unchanged), and a shutdown any thread may call
to stop the request at once (the server marks it `closed`, survey §3.7).

Threads: the session's worker thread sends, reads and closes. Another thread only calls `HttpChannel.shutdown()`, which
acts on a duplicate of the TCP socket taken right after connecting and kept until close(): http.client may close its own
socket at any time, and a descriptor number closed and reused meanwhile would let a late cancel shut down an unrelated
connection (ruling P3). SSLSocket.shutdown() is not called from another thread (spec §3.4.1), unless shutting down the TCP
duplicate cannot end a blocked TLS read (probe A10; spec §9, ruling P41)."""
```

`client_context_id`, `basic_auth`, `QueryRequest`, `build_body`:

```python
_NOT_CONTEXT_ID = re.compile(r"[^A-Za-z0-9_-]")
_CONTEXT_ID_LIMIT = 64  # longer ids are cut by the server; `/` and `"` are refused (error 1110)


def client_context_id(ident: str) -> str:
    """`dbaguy-` + any id string (Task 11 passes `uuid4().hex`, P30), reduced to what the server accepts."""
    return ("dbaguy-" + _NOT_CONTEXT_ID.sub("", ident))[:_CONTEXT_ID_LIMIT]


@dataclass(frozen=True)  # `from dataclasses import dataclass, field`
class QueryRequest:
    statement: str
    client_context_id: str
    query_context: str | None = None
    readonly: bool = False
    named: Mapping[str, str] = field(default_factory=dict)  # not `= {}`: a dataclass refuses a mutable default (P39)
    positional: Sequence[str] = ()
    profile: str | None = None
    preserve_expiry: bool = False


def build_body(request: QueryRequest) -> bytes:
    """The body as text, so parameter literals go out exactly as given (a number never passes through float; R-04-10).
    Never `timeout` (it keeps counting while a result is paused, R-04-5) nor `scan_consistency`."""
    parts = [f'"statement":{json.dumps(request.statement, ensure_ascii=False)}',
             f'"client_context_id":{json.dumps(request.client_context_id)}']
    if request.query_context is not None:
        parts.append(f'"query_context":{json.dumps(request.query_context, ensure_ascii=False)}')
    if request.readonly:
        parts.append('"readonly":true')
    parts.extend(f"{json.dumps('$' + name, ensure_ascii=False)}:{literal}" for name, literal in request.named.items())
    if request.positional:
        parts.append('"args":[' + ",".join(request.positional) + "]")
    if request.profile is not None:
        parts.append(f'"profile":{json.dumps(request.profile)}')
    if request.preserve_expiry:
        parts.append('"preserve_expiry":true')
    return ("{" + ",".join(parts) + "}").encode("utf-8")
```

The connection classes:

```python
# What a kept-alive connection the server has closed meanwhile raises before any byte of the answer.
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def failure_reason(exc: BaseException) -> str:
    """A short reason a connection failed, for messages and logs (never the request or credentials)."""
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "unknown host"
    return str(exc) or type(exc).__name__


def _tcp(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


class _Connection(http.client.HTTPConnection):
    def __init__(self, channel: "HttpChannel") -> None:
        super().__init__(channel.host, channel.port)
        self._channel = channel

    def connect(self) -> None:
        self.sock = self._channel.open_socket()


class HttpChannel:
    """One TCP (TLS) connection to a query node. Dead once shut down, failed or closed: NodeClient opens another."""

    def __init__(self, host: str, port: int, *, tls: ssl.SSLContext | None, server_hostname: str | None,
                 socket_factory: SocketFactory) -> None:
        self.host, self.port = host, port
        self._tls, self._server_hostname, self._socket_factory = tls, server_hostname or host, socket_factory
        self._lock = threading.Lock()
        self._handle: socket.socket | None = None
        self._shut = False
        self.conn = _Connection(self)
        self.used = False
        self.idle_since: float | None = None
        self.dead = False

    def open_socket(self) -> socket.socket:
        sock = self._socket_factory(self.host, self.port, CONNECT_TIMEOUT_S)
        try:
            enable_keepalive(sock)
            handle = sock.dup()
            with self._lock:
                if self._shut:  # a cancel came while connecting
                    handle.close()
                    raise ChannelError("cancelled while connecting", before_response=True)
                self._handle = handle
            if self._tls is not None:
                sock = self._tls.wrap_socket(sock, server_hostname=self._server_hostname)  # handshake within the timeout
            sock.settimeout(None)  # reads wait as long as a paused result needs; a cancel shuts the socket down
            return sock
        except BaseException:
            sock.close()
            raise

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True
            if self._handle is not None:
                with suppress(OSError):
                    self._handle.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.dead = True
        with self._lock:
            self._shut = True
            handle, self._handle = self._handle, None
        with suppress(Exception):
            self.conn.close()
        if handle is not None:
            handle.close()

    def reusable(self, now: float) -> bool:
        with self._lock:
            if self._shut or self.dead:
                return False
        return not self.used or (self.idle_since is not None and now - self.idle_since < KEEPALIVE_REUSE_S)
```

`QueryResponse` (`read_chunk` reads `response.read1(READ_CHUNK)`; an `http.client.HTTPException`, `OSError` or `ValueError` closes the channel and raises `ChannelError(failure_reason(exc))`; `b""` sets `done` and either closes the channel (`response.will_close`) or sets `idle_since = clock()`; `abandon()` shuts down and closes the channel unless `done`) and `NodeClient`:

```python
class NodeClient:
    """Requests to one query node over one HttpChannel at a time (D-04-14): reused while its last response was read to
    the end less than KEEPALIVE_REUSE_S ago, replaced otherwise. A read (`retry_safe`) that meets a kept-alive connection
    the server has closed, before any byte of the answer, is sent once more on a new one; nothing else is resent."""

    def __init__(self, host: str, port: int, *, auth: str, user_agent: str, tls: ssl.SSLContext | None,
                 server_hostname: str | None = None, socket_factory: SocketFactory | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.host, self.port = host, port
        self.label = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self._headers = {"Authorization": auth, "Content-Type": "application/json", "User-Agent": user_agent}
        self._tls, self._server_hostname = tls, server_hostname
        self._socket_factory = socket_factory or _tcp
        self._clock = clock
        self._channel: HttpChannel | None = None

    def send(self, body: bytes, *, retry_safe: bool, attach: Callable[[HttpChannel], None]) -> QueryResponse:
        channel = self._channel_for_request()
        reused = channel.used
        try:
            return self._post(channel, body, attach)
        except ChannelError as exc:
            if not (reused and retry_safe and exc.before_response):
                raise
            logger.info("The server closed the kept-alive connection to {}; sending the read again", self.label)
            return self._post(self._channel_for_request(), body, attach)

    def _channel_for_request(self) -> HttpChannel:
        if self._channel is None or not self._channel.reusable(self._clock()):
            if self._channel is not None:
                self._channel.close()
            self._channel = HttpChannel(self.host, self.port, tls=self._tls, server_hostname=self._server_hostname,
                                        socket_factory=self._socket_factory)
        return self._channel

    def _post(self, channel: HttpChannel, body: bytes, attach: Callable[[HttpChannel], None]) -> QueryResponse:
        attach(channel)  # raises when a cancel came first: nothing is sent
        channel.used, channel.idle_since = True, None
        try:
            channel.conn.request("POST", QUERY_PATH, body=body, headers=self._headers)
            response = channel.conn.getresponse()
        except (http.client.HTTPException, OSError, ValueError) as exc:  # ssl.SSLError is an OSError
            channel.close()
            raise ChannelError(failure_reason(exc), before_response=isinstance(exc, _STALE),
                               tls=isinstance(exc, ssl.SSLError)) from exc
        return QueryResponse(channel, response, self._clock)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
```

**Only if** Task 1 recorded that A10 failed (shutting down the TCP duplicate does not end a blocked TLS read), apply the one exception to the Global Constraint "never `SSLSocket.shutdown()`" (ruling P41, spec §9's fallback): when `self._tls` is set and `self.conn.sock` exists, `HttpChannel.shutdown()` also calls `self.conn.sock.shutdown(socket.SHUT_RDWR)` on the TLS socket (inside `suppress(OSError)`, after the duplicate's shutdown). The reader can then get an exception of any type from that socket, so Task 11's `EventStream` counts every exception the reader gets while the cancel flag is set as a cancelled statement. If A10 holds, none of this is added and `SSLSocket.shutdown()` is never called. Record the outcome in the A10 row of the assumptions table.

- [ ] **Step 9: Run them**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_stream.py tests/test_couchbase_http.py -q`
Expected: PASS; record A14 (the statuses and that both bodies are JSON) in the assumptions table. If the 401 body turns out not to be JSON, keep only the status assertion for `wrong`, record it, and rely on Task 11's `http_<status>` path for such bodies.

- [ ] **Step 10: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/stream.py backend/dbaguy_backend/adapters/couchbase/http.py \
  backend/tests/couchbase_fakes.py backend/tests/test_couchbase_stream.py backend/tests/test_couchbase_http.py \
  docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md
git commit -m "feat(couchbase): query service over stdlib HTTP — keep-alive reuse and safe resend, shutdown from any thread, incremental response parser"
```

---

### Task 8: Row shaping and server messages (pure)

**Files:**
- Create: `backend/dbaguy_backend/adapters/couchbase/rows.py`, `backend/dbaguy_backend/adapters/couchbase/messages.py`
- Modify: `backend/dbaguy_backend/sql/offsets.py`
- Create: `backend/tests/test_couchbase_rows.py`, `backend/tests/test_couchbase_messages.py`

**Interfaces:**
- Consumes: `RawRow`, `RowField`, `json_kind`, `split_object`, `ResponseParser`, `RowEvent` (Task 7); `JsonText`, `MISSING`, `MAX_SAFE_INT` (values), `ColumnCategory.MIXED` (Task 4); `tokenize`, `INSIGNIFICANT`, `Dialect.SQLPP` (Task 2); `StatementError`, `ErrorPosition`, `ServerMessage`, `MessageSeverity`.
- Produces (`adapters/couchbase/rows.py`):
  - `STAR_PEEK_ROWS = 500`, `STAR_PEEK_BYTES = 8 * 1024 * 1024`, `OVERFLOW_NAME = "(other fields)"`, `VALUE_COLUMN = "value"`, `PLAN_COLUMN = "plan"`.
  - `value_cell(text: str, value: Any) -> Any` — spec §3.2.4: strings, true/false/null as decoded; an integer literal → `int` when within ±(2⁵³−1) and `str(int)` gives the literal back, else `JsonText`; any other number → `float` when `repr(float(text)) == text`, else `JsonText`; objects and arrays → `JsonText(text)`.
  - `has_bare_star(sql: str) -> bool` — D-04-1 condition 1: the outermost projection holds a `*` of its own (`SELECT *`, `SELECT a, *`), not `d.*`, `COUNT(*)` or a product.
  - `needs_first_page(signature: Any, plan: bool) -> bool` — only a `*` signature is peeked (spec §3.5.1).
  - `class RowShape(ABC)` — `columns: list[ColumnMeta]`, `cells(row: RawRow) -> list[Any]`, `take_warning() -> str | None` (once).
  - `build_shape(signature: Any, *, plan: bool, bare_star: bool, first_page: Sequence[RawRow]) -> RowShape | None` — None without a signature (DML without RETURNING, DDL).
- Produces (`adapters/couchbase/messages.py`): `PROTOCOL_ERROR = "The query service sent a malformed response"`; `statement_error(errors: Sequence[Any], sql: str) -> StatementError`; `warning_messages(warnings: Sequence[Any]) -> list[ServerMessage]`; `metrics_message(metrics: Mapping[str, Any]) -> ServerMessage | None`.
- Produces (`sql/offsets.py`): `utf16_column(text: str, line: int, column: int) -> int` — a 1-based code-point column on a 1-based line of `text`, in UTF-16 units.

Columns (spec §3.5.1): no signature → None; a string signature (RAW, EXPLAIN) → one column `value` (type name = the signature; category NUMBER / TEXT / BOOL for `number` / `string` / `boolean`, else MIXED); an object signature without `*` → its keys in order with their types (no peek, no overflow column: keys outside it or a non-object row raise one warning, the JSON export keeps them; ruling P5); with `*` → the signature's names in order, `*` replaced by the keys the first page brought (first-appearance order; type name = the JSON kinds seen, joined by `|`; MIXED), then the overflow column (`(other fields)`, ` 2`… when taken; type `object`; MIXED). Alias expansion (D-04-1): when the statement has a bare `*` and every first-page row brought exactly one key K whose value is an object, the columns are K's members instead (a member named like a signature column shows as `K.name`, made unique). A presentation of `plan` is always one column `plan` (MIXED) whose cell is the whole row's `JsonText` (A15: whatever the signature). Cells never drop data: a row that is not an object goes whole into the overflow column; keys without a column go there as `{"key":<text>,…}` in their order; a later row whose `*` part does not fit the alias puts what `*` brought there as it is.

- [ ] **Step 1: Write the failing tests.** `backend/tests/test_couchbase_rows.py`:

```python
import pytest

from dbaguy_backend.adapters.couchbase.rows import (
    OVERFLOW_NAME, build_shape, has_bare_star, needs_first_page, value_cell,
)
from dbaguy_backend.adapters.couchbase.stream import ResponseParser, RowEvent
from dbaguy_backend.adapters.values import MISSING
from dbaguy_backend.adapters.values import JsonText as J
from dbaguy_backend.domain.column_category import ColumnCategory as C


def rows(*texts):
    """RawRows exactly as the parser makes them."""
    parser = ResponseParser()
    events = parser.feed(('{"results":[' + ",".join(texts) + "]}").encode()) + parser.finish()
    return [e.row for e in events if isinstance(e, RowEvent)]


def names(shape):
    return [c.name for c in shape.columns]


def shape_of(signature, page=(), bare_star=False, plan=False):
    return build_shape(signature, plan=plan, bare_star=bare_star, first_page=list(page))


@pytest.mark.parametrize("text, cell", [
    ("42", 42), ("-7", -7), ("9007199254740991", 9007199254740991), ("9007199254740993", J("9007199254740993")),
    ("-9223372036854775808", J("-9223372036854775808")), ("18446744073709551615", J("18446744073709551615")),
    ("12345678901234567890", J("12345678901234567890")), ("1" * 5000, J("1" * 5000)), ("-0", J("-0")),
    ("0.5", 0.5), ("-0.0", -0.0), ("3.141592653589793", 3.141592653589793), ("1.10", J("1.10")),
    ("3.141592653589793238462643383279", J("3.141592653589793238462643383279")), ("1E+3", J("1E+3")),
    ("1e400", J("1e400")), ("1e-400", J("1e-400")),
    ('"x"', "x"), ('"[1]"', "[1]"), ("true", True), ("null", None), ("[1, 2]", J("[1, 2]")), ('{"a":1}', J('{"a":1}')),
])
def test_number_cells_follow_the_literal(text, cell):  # Review Focus 1
    [row] = rows(text)
    got = value_cell(row.text, row.value)
    assert (type(got), got) == (type(cell), cell)


def test_no_signature_has_no_columns():
    assert shape_of(None) is None


def test_raw_results_are_one_value_column():
    shape = shape_of("number")
    assert [(c.name, c.type_name, c.category) for c in shape.columns] == [("value", "number", C.NUMBER)]
    assert [shape.cells(r) for r in rows("1", "9007199254740993", '"x"')] == [[1], [J("9007199254740993")], ["x"]]


def test_an_object_signature_keeps_its_order_and_types():
    shape = shape_of({"id": "json", "n": "number", "s": "string", "b": "boolean"})
    assert [(c.name, c.type_name, c.category) for c in shape.columns] == [
        ("id", "json", C.MIXED), ("n", "number", C.NUMBER), ("s", "string", C.TEXT), ("b", "boolean", C.BOOL),
    ]
    assert shape.cells(rows('{"n":1,"id":"k","b":true}')[0]) == ["k", 1, MISSING, True]


def test_union_rows_have_only_their_own_keys():
    shape = shape_of({"a": "number", "b": "number"})
    assert [shape.cells(r) for r in rows('{"a":1}', '{"b":2}')] == [[1, MISSING], [MISSING, 2]]
    assert shape.take_warning() is None


def test_keys_outside_a_plain_signature_warn_once():
    shape = shape_of({"a": "json"})
    assert [shape.cells(r) for r in rows('{"a":1}', '{"a":2,"z":3}', '{"a":4,"y":5}', "7")] == [[1], [2], [4], [MISSING]]
    assert shape.take_warning() == "Some rows have keys outside the columns (from row 2); export as JSON to see every key"
    assert shape.take_warning() is None


@pytest.mark.parametrize("signature", [{"plan": "json", "text": "json"}, "json"])
def test_a_plan_is_one_cell_of_the_whole_row(signature):  # A15: either signature shape
    [row] = rows('{"plan":{"#operator":"Sequence"},"text":"SELECT 1"}')
    shape = shape_of(signature, plan=True)
    assert [(c.name, c.category) for c in shape.columns] == [("plan", C.MIXED)] and shape.cells(row) == [J(row.text)]


def test_star_columns_follow_the_first_page():
    page = rows('{"name":"a","age":1}', '{"age":null,"city":"x"}', '{"name":"b","tags":[1]}')
    shape = shape_of({"*": "*"}, page)
    assert [(c.name, c.type_name) for c in shape.columns] == [
        ("name", "string"), ("age", "number|null"), ("city", "string"), ("tags", "array"), (OVERFLOW_NAME, "object"),
    ]
    assert {c.category for c in shape.columns} == {C.MIXED}
    assert shape.cells(page[0]) == ["a", 1, MISSING, MISSING, MISSING]


def test_star_keys_after_the_first_page_go_to_the_overflow():  # Review Focus 4
    shape = shape_of({"id": "json", "*": "*", "amount": "json"}, rows('{"id":"k","name":"a","amount":1}'))
    assert names(shape) == ["id", "name", "amount", OVERFLOW_NAME]
    later = rows('{"id":"k","name":"b","late":{"x":1e400},"amount":1.10}', '"not an object"')
    assert shape.cells(later[0]) == ["k", "b", J("1.10"), J('{"late":{"x":1e400}}')]
    assert shape.cells(later[1]) == [MISSING, MISSING, MISSING, J('"not an object"')]


def test_alias_expansion_needs_one_object_alias_on_every_first_page_row():  # Review Focus 4
    sig = {"id": "json", "*": "*"}
    page = rows('{"id":"e1","d":{"zeta":1,"alpha":{"z":1},"id":7}}', '{"id":"e2","d":{"alpha":2,"kind":"x"}}')
    shape = shape_of(sig, page, bare_star=True)
    assert names(shape) == ["id", "zeta", "alpha", "d.id", "kind", OVERFLOW_NAME]  # stored order; d.id: a projected name
    assert shape.cells(page[0]) == ["e1", 1, J('{"z":1}'), 7, MISSING, MISSING]
    assert names(shape_of(sig, page))[:2] == ["id", "d"]  # `d.*`, not a bare star
    mixed = rows('{"id":"e1","d":{"a":1}}', '{"id":"s1","d":"just a string"}')
    assert names(shape_of(sig, mixed, bare_star=True)) == ["id", "d", OVERFLOW_NAME]
    two = rows('{"id":"e1","d":{"a":1},"o":{"b":2}}')
    assert names(shape_of(sig, two, bare_star=True)) == ["id", "d", "o", OVERFLOW_NAME]
    assert names(shape_of(sig, [], bare_star=True)) == ["id", OVERFLOW_NAME]  # an empty result


def test_alias_expansion_falls_back_per_row():  # Review Focus 4
    shape = shape_of({"id": "json", "*": "*"}, rows('{"id":"e1","d":{"a":1}}'), bare_star=True)
    later = rows('{"id":"e2","d":{"a":2,"b":3}}', '{"id":"e3","d":[1,2]}', '{"id":"e4","x":{"a":5}}')
    assert [shape.cells(r) for r in later] == [
        ["e2", 2, J('{"b":3}')],  # a member the first page did not show
        ["e3", MISSING, J('{"d":[1,2]}')],  # the alias holds no object: kept whole
        ["e4", MISSING, J('{"x":{"a":5}}')],  # another alias
    ]


def test_names_that_collide():  # Review Focus 4
    page = rows('{"' + OVERFLOW_NAME + '":1,"a":2}')
    assert names(shape_of({"*": "*"}, page)) == [OVERFLOW_NAME, "a", OVERFLOW_NAME + " 2"]
    alias_page = rows('{"id":"e1","d":{"id":1,"d.id":2}}')
    assert names(shape_of({"id": "json", "*": "*"}, alias_page, bare_star=True)) == ["id", "d.id", "d.id 2", OVERFLOW_NAME]


@pytest.mark.parametrize("sql, bare", [
    ("SELECT * FROM ks", True), ("select meta(d).id, * from ks d", True), ("SELECT DISTINCT * FROM ks", True),
    ("SELECT * FROM a UNION SELECT b.* FROM b", True), ("SELECT d.* FROM ks d", False),
    ("SELECT COUNT(*) FROM ks", False), ("SELECT a * 2 FROM ks", False), ("SELECT RAW d FROM ks d", False),
    ("WITH c AS (SELECT * FROM ks) SELECT c.* FROM c", False), ("SELECT (SELECT * FROM x) AS y FROM ks", False),
    ("SELECT `*` FROM ks", False), ("SELECT 1 /* , * */ FROM ks", False),
])
def test_bare_star(sql, bare):
    assert has_bare_star(sql) == bare


def test_only_star_signatures_are_peeked():
    assert needs_first_page({"*": "*"}, plan=False) and needs_first_page({"id": "json", "*": "*"}, plan=False)
    assert not needs_first_page({"*": "*"}, plan=True) and not needs_first_page("json", plan=False)
    assert not needs_first_page({"a": "json"}, plan=False) and not needs_first_page(None, plan=False)
```

`backend/tests/test_couchbase_messages.py`:

```python
from dbaguy_backend.adapters.couchbase.messages import metrics_message, statement_error, warning_messages
from dbaguy_backend.domain.execution import ErrorPosition, MessageSeverity, ServerMessage
from dbaguy_backend.sql.offsets import utf16_column


def test_error_text_code_and_utf16_position():
    sql = "SELECT '中文😀' AS x WHER"  # WHER is code point 19, UTF-16 unit 20 (survey §3.3)
    error = statement_error([{"code": 3000, "msg": "syntax error - at: WHER", "line": 1, "column": 19}], sql)
    assert (error.code, error.message, error.position) == ("3000", "syntax error - at: WHER", ErrorPosition(line=1, column=20))


def test_reason_missing_role_and_further_errors():
    errors = [
        {"code": 13014, "msg": "User does not have credentials to run SELECT queries",
         "reason": {"message": "No index available", "missing_role": "query_use_sequential_scans"}},
        {"code": 5000, "msg": "second"},
    ]
    error = statement_error(errors, "SELECT 1")
    assert error.message == (
        "User does not have credentials to run SELECT queries\nReason: No index available\n"
        "Missing role: query_use_sequential_scans\n5000: second"
    )
    assert (error.code, error.position) == ("13014", None)


def test_positions_on_later_lines_and_past_the_end():
    sql = "SELECT 1\n  FROM 😀x WHER"
    assert statement_error([{"code": 3000, "msg": "m", "line": 2, "column": 11}], sql).position == ErrorPosition(line=2, column=12)
    assert utf16_column("ab😀", 1, 6) == 7  # beyond the line: the extra columns count as they are
    assert utf16_column("a", 3, 4) == 4  # beyond the text: unchanged
    assert statement_error([{"code": 1, "msg": "m", "line": True, "column": 1}], "x").position is None


def test_warnings_and_metrics():
    assert warning_messages([{"code": 4038, "msg": "a warning"}]) == [
        ServerMessage(severity=MessageSeverity.WARNING, text="4038: a warning"),
    ]
    message = metrics_message({"elapsedTime": "12.96s", "executionTime": "12.95s", "resultCount": 1000,
                               "resultSize": 2202010, "mutationCount": 3, "warningCount": 1})
    assert message == ServerMessage(
        severity=MessageSeverity.INFO, text="Elapsed 12.96s · execution 12.95s · 1,000 results · 2.1 MB · 3 mutations · 1 warning",
    )
    assert metrics_message({"resultCount": 1}).text == "1 result"
    assert metrics_message({}) is None
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_rows.py tests/test_couchbase_messages.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.rows`.

- [ ] **Step 3: Implement `sql/offsets.py` `utf16_column`.**

```python
def utf16_column(text: str, line: int, column: int) -> int:
    """A 1-based column counted in code points on 1-based `line` of `text` (Couchbase's error positions, survey §3.3),
    counted in UTF-16 units as the editor does. Columns past the line's end, and lines past the text, count as given."""
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return column
    content = lines[line - 1]
    within = min(column - 1, len(content))
    return Utf16Index(content).to_utf16(within) + (column - 1 - within) + 1
```

- [ ] **Step 4: Implement `messages.py`.**

```python
"""What the query service says besides rows, as DbaGuy shows it (spec §3.5.4, §4): errors with the server's own text
(M10), its code and a UTF-16 position; warnings and the metrics line as messages. Pure."""

PROTOCOL_ERROR = "The query service sent a malformed response"
_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _line(entry: Any) -> str:
    if isinstance(entry, dict):
        code, text = entry.get("code"), str(entry.get("msg") or "")
        return f"{code}: {text}" if code is not None else text
    return str(entry)


def _first_text(error: dict[str, Any]) -> str:
    text = str(error.get("msg") or "The query failed")
    reason = error.get("reason")
    if isinstance(reason, dict):
        if detail := reason.get("message") or reason.get("msg") or reason.get("key"):
            text += f"\nReason: {detail}"
        if role := reason.get("missing_role"):
            text += f"\nMissing role: {role}"
    elif reason:
        text += f"\nReason: {reason}"
    return text


def _position(error: dict[str, Any], sql: str) -> ErrorPosition | None:
    line, column = error.get("line"), error.get("column")
    if type(line) is int and type(column) is int and line >= 1 and column >= 1:  # not bool: True is an int
        return ErrorPosition(line=line, column=utf16_column(sql, line, column))
    return None


def statement_error(errors: Sequence[Any], sql: str) -> StatementError:
    """The statement's error: the first one's message (with `reason` and `missing_role`), code and position; any
    further errors one line each."""
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    message = "\n".join([_first_text(first), *(_line(e) for e in errors[1:])])
    code = first.get("code")
    return StatementError(message, code=str(code) if code is not None else None, position=_position(first, sql))


def warning_messages(warnings: Sequence[Any]) -> list[ServerMessage]:
    return [ServerMessage(severity=MessageSeverity.WARNING, text=_line(w)) for w in warnings]


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _size(n: int) -> str:
    value, unit = float(n), 0
    while value >= 1024 and unit < len(_BYTE_UNITS) - 1:
        value, unit = value / 1024, unit + 1
    return f"{n} B" if unit == 0 else f"{value:.1f} {_BYTE_UNITS[unit]}"


def metrics_message(metrics: Mapping[str, Any]) -> ServerMessage | None:
    """`Elapsed 12.96s · execution 12.95s · 1,000 results · 2.1 MB · 3 mutations · 1 warning`: the fields present only;
    durations as the server writes them (Go format), never parsed."""
    parts: list[str] = []
    if elapsed := metrics.get("elapsedTime"):
        parts.append(f"Elapsed {elapsed}")
    if execution := metrics.get("executionTime"):
        parts.append(f"execution {execution}")
    for key, noun in (("resultCount", "result"), ("resultSize", None), ("mutationCount", "mutation"),
                      ("warningCount", "warning")):
        value = metrics.get(key)
        if type(value) is int:
            parts.append(_size(value) if noun is None else _count(value, noun))
    return ServerMessage(severity=MessageSeverity.INFO, text=" · ".join(parts)) if parts else None
```

- [ ] **Step 5: Implement `rows.py`.** Module docstring as in Interfaces. The key parts:

```python
_SIGNATURE_CATEGORY = {"number": ColumnCategory.NUMBER, "string": ColumnCategory.TEXT, "boolean": ColumnCategory.BOOL}
_INTEGER = re.compile(r"-?\d+")
_SAFE_DIGITS = 17  # ±(2^53 − 1) has 16 digits: longer literals are never converted (and int() has a digit limit)
_PROJECTION_ENDS = frozenset({"FROM", "LET", "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT",
                              "EXCEPT", "RAW", "ELEMENT", "VALUE"})
_LATE_KEYS = "Some rows have keys outside the columns (from row {:,}); export as JSON to see every key"


def value_cell(text: str, value: Any) -> Any:
    kind = json_kind(text)
    if kind in ("object", "array"):
        return JsonText(text)
    if kind != "number":
        return value  # str, True / False, None
    if _INTEGER.fullmatch(text):
        if len(text) <= _SAFE_DIGITS and str(number := int(text)) == text and abs(number) <= MAX_SAFE_INT:
            return number
        return JsonText(text)
    as_float = float(text)  # JSON numbers always parse; 1e400 gives inf
    return as_float if repr(as_float) == text else JsonText(text)


def _cell(field: RowField | None) -> Any:
    return MISSING if field is None else value_cell(field.text, field.value)


def _overflow(fields: Sequence[RowField]) -> Any:
    """Keys without a column, in their order, values as sent; MISSING when there are none."""
    if not fields:
        return MISSING
    return JsonText("{" + ",".join(f"{json.dumps(f.key, ensure_ascii=False)}:{f.text}" for f in fields) + "}")


def _signature_column(name: str, type_name: Any) -> ColumnMeta:
    text = type_name if isinstance(type_name, str) else json.dumps(type_name)
    return ColumnMeta(name=name, type_name=text, category=_SIGNATURE_CATEGORY.get(text, ColumnCategory.MIXED))


def _unique(names: Iterable[str], taken: set[str]) -> list[str]:
    """Each name, with " 2", " 3"… when an earlier column (or `taken`) has it already."""
    result = []
    for name in names:
        unique, n = name, 2
        while unique in taken:
            unique, n = f"{name} {n}", n + 1
        taken.add(unique)
        result.append(unique)
    return result


def has_bare_star(sql: str) -> bool:
    depth, projecting, item_start = 0, False, False
    for token in tokenize(sql, Dialect.SQLPP):
        if token.kind in INSIGNIFICANT:
            continue
        if token.kind == TokenKind.RPAREN:
            depth = max(depth - 1, 0)
            continue
        if depth == 0:
            word = token.upper if token.kind == TokenKind.WORD else ""
            if word == "SELECT":
                projecting = item_start = True
                continue
            if projecting:
                if word in ("DISTINCT", "ALL") and item_start:
                    continue
                if word in _PROJECTION_ENDS:
                    projecting = False
                elif token.kind == TokenKind.COMMA:
                    item_start = True
                    continue
                elif token.kind == TokenKind.OTHER and token.text == "*" and item_start:
                    return True
            item_start = False
        if token.kind == TokenKind.LPAREN:
            depth += 1
    return False
```

The shapes: `RowShape` holds `columns` and `_warning: str | None` with `take_warning()` returning it once. `_PlanShape` (column `plan`, type `json`, MIXED; cell `JsonText(row.text)`); `_ValueShape(signature)` (one `_signature_column(VALUE_COLUMN, signature)`; cell `value_cell(row.text, row.value)`); `_ObjectShape(signature)` (columns by `_signature_column`; counts rows; a non-object row or a key outside the signature sets `_LATE_KEYS.format(row_number)` once, and the non-object row's cells are all MISSING); and:

```python
class _StarShape(RowShape):
    def __init__(self, signature: dict[str, Any], *, bare_star: bool, first_page: Sequence[RawRow]) -> None:
        keys = list(signature)
        at = keys.index("*")
        self._before, self._after = keys[:at], keys[at + 1:]
        self._named = set(self._before) | set(self._after)
        self._alias = _alias_of(first_page, self._named) if bare_star else None
        seen: dict[str, list[str]] = {}  # expanded key → JSON kinds seen on the first page, in order
        for row in first_page:
            for field in self._first_page_fields(row):
                kinds = seen.setdefault(field.key, [])
                if (kind := json_kind(field.text)) not in kinds:
                    kinds.append(kind)
        self._expanded = list(seen)
        self._expanded_set = set(seen)
        shown = [f"{self._alias}.{k}" if self._alias is not None and k in self._named else k for k in self._expanded]
        columns = [_signature_column(k, signature[k]) for k in self._before]
        columns += [ColumnMeta(name=name, type_name="|".join(seen[k]), category=ColumnCategory.MIXED)
                    for name, k in zip(_unique(shown, set(self._named)), self._expanded)]
        columns += [_signature_column(k, signature[k]) for k in self._after]
        [overflow] = _unique([OVERFLOW_NAME], {c.name for c in columns})
        super().__init__([*columns, ColumnMeta(name=overflow, type_name="object", category=ColumnCategory.MIXED)])

    def _first_page_fields(self, row: RawRow) -> Sequence[RowField]:
        if row.fields is None:
            return ()
        if self._alias is None:
            return [f for f in row.fields if f.key not in self._named]
        return split_object(next(f for f in row.fields if f.key == self._alias).text)

    def _star_part(self, brought: list[RowField]) -> tuple[dict[str, RowField], list[RowField]]:
        """What `*` brought, as {expanded key: field}, and what has no column."""
        if self._alias is not None:
            if len(brought) == 1 and brought[0].key == self._alias and json_kind(brought[0].text) == "object":
                brought = list(split_object(brought[0].text))
            else:
                return {}, brought  # another shape than the first page's: kept whole in the overflow column
        found: dict[str, RowField] = {}
        extra: list[RowField] = []
        for field in brought:
            if field.key in self._expanded_set and field.key not in found:
                found[field.key] = field
            else:
                extra.append(field)  # a key the first page did not show, or a repeated key
        return found, extra

    def cells(self, row: RawRow) -> list[Any]:
        if row.fields is None:  # not an object: the whole row goes to the overflow column
            return [MISSING] * (len(self.columns) - 1) + [JsonText(row.text)]
        named = {f.key: f for f in row.fields if f.key in self._named}
        found, extra = self._star_part([f for f in row.fields if f.key not in self._named])
        return [*(_cell(named.get(k)) for k in self._before), *(_cell(found.get(k)) for k in self._expanded),
                *(_cell(named.get(k)) for k in self._after), _overflow(extra)]
```

`_alias_of(first_page, named) -> str | None`: None for an empty page or as soon as a row is not an object, brought other than exactly one key, a value that is not an object, or a key other than the first row's. `build_shape`: `plan` → `_PlanShape()`; no signature → None; a `str` → `_ValueShape`; a `dict` with `*` → `_StarShape`; any other `dict` → `_ObjectShape`; anything else → `_ValueShape(json.dumps(signature))` (one column, nothing lost). `needs_first_page(signature, plan) = not plan and isinstance(signature, dict) and "*" in signature`.

- [ ] **Step 6: Run them**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_rows.py tests/test_couchbase_messages.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/rows.py backend/dbaguy_backend/adapters/couchbase/messages.py \
  backend/dbaguy_backend/sql/offsets.py backend/tests/test_couchbase_rows.py backend/tests/test_couchbase_messages.py
git commit -m "feat(couchbase): columns from the signature, * and alias expansion with an overflow column, exact cells; errors, warnings and metrics"
```

---

### Task 9: Couchbase connection settings, URL import and node discovery

**Files:**
- Create: `backend/dbaguy_backend/adapters/couchbase/settings.py`, `backend/dbaguy_backend/adapters/couchbase/nodes.py`
- Create: `backend/tests/test_couchbase_settings.py`

**Interfaces:**
- Consumes: `QueryContext.parse` (Task 4); `keyspace_path` (Task 2); `failure_reason` (Task 7); `Endpoint`, `ParsedUrl`, `MALFORMED_URL_MESSAGE` (base); `couchbase_ca_file`, `make_couchbase_profile`, `requires_couchbase` (Task 1).
- Produces (`adapters/couchbase/settings.py`):
  - `URL_SCHEMES = ("couchbase", "couchbases", "http", "https")`; `QUERY_PORT = 8093`, `QUERY_TLS_PORT = 18093`, `MANAGER_PORT = 8091`, `MANAGER_TLS_PORT = 18091`.
  - `seed_hosts(host: str) -> list[str]` — `cb1, cb2`, `[::1]` → hosts without brackets; `ValidationFailed` for an empty item.
  - `uses_tls(config: SslConfig) -> bool` — `require`, `verify-ca`, `verify-full` (`prefer` = `disable`: Couchbase's TLS is another port, D-04-7).
  - `query_port(profile: ServerProfile) -> int`; `endpoint_target(profile: ServerProfile) -> tuple[str, int]` (first seed, query port; `ValidationFailed("An SSH tunnel reaches one node: give a single host")` for SSH with several seeds).
  - `tls_context(config: SslConfig) -> ssl.SSLContext | None` — `ConnectionFailed` for unreadable files or a certificate without its key.
  - `request_context(context: QueryContext) -> str` — `default:` + `keyspace_path(bucket, scope)`.
  - `@dataclass(frozen=True) CouchbaseSettings(seeds: tuple[str, ...], port: int, tls: ssl.SSLContext | None, tls_mode: SslMode, server_hostname: str | None, discover: bool, manager_port: int, default_context: QueryContext | None, read_only: bool, username: str, tunnel: bool)`.
  - `connection_settings(profile: ServerProfile, endpoint: Endpoint, database: str | None) -> CouchbaseSettings`.
  - `parse_couchbase_url(url: str) -> ParsedUrl`.
- Produces (`adapters/couchbase/nodes.py`):
  - `DISCOVERY_TIMEOUT_S = 3.0`; `NODE_SERVICES_PATH = "/pools/default/nodeServices"`.
  - `@dataclass(frozen=True) QueryNode(host: str, port: int)` with `label: str` (`host:port`, IPv6 in brackets).
  - `class DiscoveryFailed(Exception)` (an answer that is not a usable 200).
  - `Fetch = Callable[[str, int], bytes]` (host, manager port → the nodeServices body).
  - `parse_node_services(body: Any, asked_host: str, tls: bool) -> list[QueryNode]` (ValueError when it is not a nodeServices map).
  - `discover(seeds: Sequence[str], manager_port: int, *, tls: ssl.SSLContext | None, auth: str, fetch: Fetch | None = None) -> tuple[list[QueryNode], str | None]` — the nodes from the first seed that answers, or `[]` and the reasons.
  - `candidate_nodes(settings: CouchbaseSettings, auth: str, fetch: Fetch | None = None) -> tuple[list[QueryNode], str]` — the nodes to try in order and how they were found (for the INFO log).

Rules: profile fields per spec §3.6.1 — `host` one host or a comma list (only one with SSH), `port` the query port (None → 8093 / 18093 by TLS), `database` the default query context, username required, `options` `manager_port` (default 8091 / 18091) and `discover_nodes` (`true` / `false`, default true), other keys ignored with a WARNING naming them. Discovery (D-04-6, §3.6.3) runs only without SSH, with the default port for the TLS mode, and `discover_nodes` true; it asks each seed's manager port in turn (3 s), uses the first answer (`n1ql` / `n1qlSSL` ports; an entry without `hostname` is the asked node, A8; external alternate addresses when the seed is one of them), and falls back to the seeds with an INFO line on any failure. URL import per spec §3.6.2 (`couchbase://` ports are dropped: KV or old manager ports, never the query port), never echoing the URL.

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_couchbase_settings.py`:

```python
import json
import ssl
from uuid import uuid4

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.couchbase.nodes import (
    DiscoveryFailed, QueryNode, candidate_nodes, discover, parse_node_services,
)
from dbaguy_backend.adapters.couchbase.settings import (
    connection_settings, endpoint_target, parse_couchbase_url, request_context, seed_hosts, tls_context,
)
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from tests.support import COUCHBASE_PASSWORD, couchbase_ca_file, make_couchbase_profile, requires_couchbase

AUTH = "Basic eA=="


def settings_of(**overrides):
    profile = make_couchbase_profile(**overrides)
    host, port = endpoint_target(profile)
    return connection_settings(profile, Endpoint(host, port), None)


@pytest.mark.parametrize("url, host, port, database, user, password, ssl_fields", [
    ("couchbase://cb1,cb2/travel-sample", "cb1,cb2", None, "travel-sample", None, None, ("disable", None)),
    ("couchbase://u:p%40ss@cb1:11210,cb2:11210/dbaguy.app", "cb1,cb2", None, "dbaguy.app", "u", "p@ss", ("disable", None)),
    ("couchbases://cb1/b?certpath=/tmp/ca.pem", "cb1", None, "b", None, None, ("verify-full", "/tmp/ca.pem")),
    ("couchbases://cb1?ssl=no_verify", "cb1", None, None, None, None, ("require", None)),
    ("couchbase://[::1]:8091/b", "[::1]", None, "b", None, None, ("disable", None)),
    ("http://h:8093", "h", 8093, None, None, None, ("disable", None)),
    ("HTTPS://user@h:18093/b.s", "h", 18093, "b.s", "user", None, ("verify-full", None)),
    ("couchbases://cb1/b?truststorepath=/tmp/t.pem&network=external", "cb1", None, "b", None, None, ("verify-full", "/tmp/t.pem")),
])
def test_url_import(url, host, port, database, user, password, ssl_fields):
    parsed = parse_couchbase_url(url)
    p = parsed.profile
    assert (p.engine, p.host, p.port, p.database, p.username, parsed.password, (p.ssl.mode, p.ssl.ca_file)) == (
        "couchbase", host, port, database, user, password, ssl_fields,
    )


def test_dropped_url_parameters_are_named_never_their_values():
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        parse_couchbase_url("couchbase://u:secret@cb1/b?network=external&kv_timeout=secret2")
    finally:
        logger.remove(sink)
    assert any("kv_timeout" in m and "network" in m for m in seen) and not any("secret" in m for m in seen)


@pytest.mark.parametrize("url", ["couchbase://", "couchbase:///b", "http://a,b:8093", "couchbase://h:x/b"])
def test_malformed_urls(url):
    with pytest.raises((ValidationFailed, ValueError)):
        parse_couchbase_url(url)


def test_seed_hosts():
    assert seed_hosts(" cb1, cb2 ") == ["cb1", "cb2"]
    assert seed_hosts("[::1],cb2") == ["::1", "cb2"]
    with pytest.raises(ValidationFailed):
        seed_hosts("a,,b")


def test_endpoint_target():
    assert endpoint_target(make_couchbase_profile(host="cb1,cb2")) == ("cb1", 8093)
    assert endpoint_target(make_couchbase_profile(host="cb1", ssl=SslConfig(mode="verify-ca"))) == ("cb1", 18093)
    assert endpoint_target(make_couchbase_profile(host="cb1", port=9000)) == ("cb1", 9000)
    ssh = SshConfig(enabled=True, host="bastion")
    assert endpoint_target(make_couchbase_profile(host="cb1", ssh=ssh)) == ("cb1", 8093)
    with pytest.raises(ValidationFailed, match="one node"):
        endpoint_target(make_couchbase_profile(host="cb1,cb2", ssh=ssh))


@pytest.mark.parametrize("mode, verify, hostname", [
    ("require", ssl.CERT_NONE, False), ("verify-ca", ssl.CERT_REQUIRED, False), ("verify-full", ssl.CERT_REQUIRED, True),
])
def test_tls_contexts(mode, verify, hostname):
    context = tls_context(SslConfig(mode=mode))
    assert (context.verify_mode, context.check_hostname) == (verify, hostname)


def test_tls_settings_that_cannot_work():
    assert tls_context(SslConfig(mode="disable")) is None and tls_context(SslConfig(mode="prefer")) is None
    with pytest.raises(ConnectionFailed):
        tls_context(SslConfig(mode="verify-ca", ca_file="/nonexistent/ca.pem"))
    with pytest.raises(ConnectionFailed, match="key"):
        tls_context(SslConfig(mode="verify-full", cert_file="/tmp/c.pem"))


def test_discovery_rules():
    assert settings_of().discover and settings_of(port=8093).discover
    assert not settings_of(port=9000).discover
    assert not settings_of(options={"discover_nodes": "false"}).discover
    assert settings_of(options={"manager_port": "9091"}).manager_port == 9091
    assert settings_of(ssl=SslConfig(mode="require")).manager_port == 18091
    tunnel = connection_settings(
        make_couchbase_profile(host="cb1", ssh=SshConfig(enabled=True, host="bastion"), ssl=SslConfig(mode="verify-full")),
        Endpoint("127.0.0.1", 54321, tls_server_name="cb1"), None,
    )
    assert (tunnel.seeds, tunnel.port, tunnel.server_hostname, tunnel.discover, tunnel.tunnel) == (
        ("127.0.0.1",), 54321, "cb1", False, True,
    )


def test_options_and_context():
    assert settings_of().default_context == QueryContext(bucket="dbaguy", scope="app")
    assert connection_settings(make_couchbase_profile(), Endpoint("127.0.0.1", 8093), "travel-sample").default_context == (
        QueryContext(bucket="travel-sample")
    )
    for bad in ({"manager_port": "x"}, {"discover_nodes": "maybe"}):
        with pytest.raises(ConnectionFailed):
            settings_of(options=bad)
    with pytest.raises(ConnectionFailed, match="query context"):
        settings_of(database="`unclosed")
    with pytest.raises(ConnectionFailed, match="user name"):
        settings_of(username=None)
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        settings_of(options={"kv_timeout": "5"})
    finally:
        logger.remove(sink)
    assert any("kv_timeout" in m for m in seen)
    assert request_context(QueryContext(bucket="travel-sample", scope="inventory")) == "default:`travel-sample`.inventory"


SERVICES = {"nodesExt": [
    {"services": {"mgmt": 8091, "kv": 11210}, "hostname": "kv-only"},
    {"services": {"n1ql": 8093, "n1qlSSL": 18093}, "hostname": "q2"},
    {"services": {"n1ql": 8093, "n1qlSSL": 18093, "mgmt": 8091}, "thisNode": True},
]}


def test_query_nodes_with_the_asked_node_first():
    assert parse_node_services(SERVICES, "seed", tls=False) == [QueryNode("seed", 8093), QueryNode("q2", 8093)]
    assert parse_node_services(SERVICES, "seed", tls=True) == [QueryNode("seed", 18093), QueryNode("q2", 18093)]


def test_external_addresses_when_the_seed_is_one():
    body = {"nodesExt": [
        {"services": {"n1ql": 8093}, "hostname": "10.0.0.1", "thisNode": True,
         "alternateAddresses": {"external": {"hostname": "ext1", "ports": {"n1ql": 31093}}}},
        {"services": {"n1ql": 8093}, "hostname": "10.0.0.2",
         "alternateAddresses": {"external": {"hostname": "ext2", "ports": {"n1ql": 32093}}}},
    ]}
    assert parse_node_services(body, "ext1", tls=False) == [QueryNode("ext1", 31093), QueryNode("ext2", 32093)]
    assert parse_node_services(body, "10.0.0.1", tls=False) == [QueryNode("10.0.0.1", 8093), QueryNode("10.0.0.2", 8093)]
    assert QueryNode("fd00::1", 8093).label == "[fd00::1]:8093"


def test_discovery_tries_each_seed_then_reports():
    def fetch(host, port):
        if host == "a":
            raise ConnectionRefusedError()
        if host == "b":
            raise DiscoveryFailed("HTTP 401")
        return json.dumps(SERVICES).encode()

    assert discover(["a", "c"], 8091, tls=None, auth=AUTH, fetch=fetch) == ([QueryNode("c", 8093), QueryNode("q2", 8093)], None)
    assert discover(["a", "b"], 8091, tls=None, auth=AUTH, fetch=fetch) == ([], "a: refused; b: HTTP 401")
    nodes, how = candidate_nodes(settings_of(host="a,b"), AUTH, fetch=fetch)
    assert nodes == [QueryNode("a", 8093), QueryNode("b", 8093)] and "discovery failed" in how
    assert candidate_nodes(settings_of(host="x", port=9000), AUTH, fetch=fetch)[0] == [QueryNode("x", 9000)]


@requires_couchbase
def test_discovery_against_the_dev_cluster(tmp_path):  # A8 (one node)
    from dbaguy_backend.adapters.couchbase.http import basic_auth

    auth = basic_auth("dbaguy", COUCHBASE_PASSWORD)
    assert candidate_nodes(settings_of(), auth)[0] == [QueryNode("127.0.0.1", 8093)]
    tls = settings_of(ssl=SslConfig(mode="verify-ca", ca_file=str(couchbase_ca_file(tmp_path))))
    assert candidate_nodes(tls, auth)[0] == [QueryNode("127.0.0.1", 18093)]
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_settings.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.nodes`.

- [ ] **Step 3: Implement `settings.py`.**

```python
_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
_KNOWN_OPTIONS = ("manager_port", "discover_nodes")
_URL = re.compile(r"([^/?#]*)([^?#]*)(?:\?([^#]*))?")
_CA_KEYS = frozenset({"certpath", "truststorepath"})


def seed_hosts(host: str) -> list[str]:
    hosts = [item.strip() for item in host.split(",")]
    if not all(hosts):
        raise ValidationFailed("A host list has an empty item: write hosts as cb1,cb2")
    return [h[1:-1] if h.startswith("[") and h.endswith("]") else h for h in hosts]


def uses_tls(config: SslConfig) -> bool:
    return config.mode in _TLS_MODES


def query_port(profile: ServerProfile) -> int:
    return profile.port or (QUERY_TLS_PORT if uses_tls(profile.ssl) else QUERY_PORT)


def endpoint_target(profile: ServerProfile) -> tuple[str, int]:
    """Where ConnectionService connects or tunnels to: the first seed's query port. A tunnel reaches one node."""
    seeds = seed_hosts(profile.host)
    if profile.ssh.enabled and len(seeds) > 1:
        raise ValidationFailed("An SSH tunnel reaches one node: give a single host")
    return seeds[0], query_port(profile)


def tls_context(config: SslConfig) -> ssl.SSLContext | None:
    """disable / prefer: none (plain HTTP); require: TLS without checks; verify-ca: the CA file (or OpenSSL's default
    bundle — not the macOS keychain, so it fails closed without one); verify-full: also the host name (D-04-7)."""
    if config.mode not in _TLS_MODES:
        return None
    if bool(config.cert_file) != bool(config.key_file):
        raise ConnectionFailed("Give both a client certificate and its key, or neither")
    try:
        cafile = os.path.expanduser(config.ca_file) if config.ca_file and config.mode != "require" else None
        context = ssl.create_default_context(cafile=cafile)
        if config.mode != "verify-full":
            context.check_hostname = False
        if config.mode == "require":
            context.verify_mode = ssl.CERT_NONE
        if config.cert_file and config.key_file:
            context.load_cert_chain(os.path.expanduser(config.cert_file), os.path.expanduser(config.key_file))
    except (OSError, ssl.SSLError) as exc:
        raise ConnectionFailed(f"Could not read the TLS files: {exc.strerror or exc}") from exc
    return context


def request_context(context: QueryContext) -> str:
    return "default:" + keyspace_path(context.bucket, context.scope)
```

`connection_settings`: username required (`ConnectionFailed("Couchbase needs a user name: the query service does not take anonymous requests")`); `_options(profile.options)` (WARNING `"Ignoring Couchbase connection options: {}"` with the sorted unknown keys; `manager_port` must be digits within 1–65535 and `discover_nodes` `true`/`false`, case-insensitive, else `ConnectionFailed` naming the option, not its value); `tls = tls_context(profile.ssl)`; `default_context = QueryContext.parse(database if database is not None else profile.database)` (a `ValueError` → `ConnectionFailed("The default query context is not a bucket or bucket.scope (write a bucket name that holds a dot in back-ticks)")`); with SSH: `seeds=(endpoint.host,)`, `port=endpoint.port`, `server_hostname=endpoint.tls_server_name`, `discover=False`, `tunnel=True`; without: `seeds=tuple(seed_hosts(profile.host))`, `port=query_port(profile)`, `server_hostname=None`, `discover = options.discover and port == (QUERY_TLS_PORT if tls else QUERY_PORT)`, `tunnel=False`; `manager_port = options.manager_port or (MANAGER_TLS_PORT if tls else MANAGER_PORT)`; `tls_mode=profile.ssl.mode`, `read_only=profile.read_only`.

`parse_couchbase_url`:

```python
def _host_and_port(item: str) -> tuple[str, int | None]:
    if item.startswith("["):
        close = item.index("]")  # ValueError: malformed
        host, rest = item[1:close], item[close + 1:]
    else:
        host, _, port_text = item.partition(":")
        rest = f":{port_text}" if port_text else ""
    if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
        raise ValueError("malformed port")
    return host, int(rest[1:]) if rest else None


def parse_couchbase_url(url: str) -> ParsedUrl:
    """`couchbase://[user[:password]@]h1[:port],h2…[/bucket[.scope]][?…]`, `couchbases://…`, `http(s)://host:port/…`
    (spec §3.6.2). Messages and logs never repeat the URL: its authority may hold a password."""
    scheme, sep, rest = url.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in URL_SCHEMES:
        raise ValidationFailed(MALFORMED_URL_MESSAGE)
    authority, path, query = _URL.match(rest).groups()
    userinfo, at, hosts = authority.rpartition("@")
    if not at:
        userinfo, hosts = "", authority
    username, colon, password = userinfo.partition(":")
    items = [_host_and_port(item.strip()) for item in hosts.split(",")]
    if not all(host for host, _ in items):
        raise ValidationFailed("A Couchbase URL needs a host, e.g. couchbase://cb1,cb2/bucket")
    http_url = scheme in ("http", "https")
    if http_url and len(items) > 1:
        raise ValidationFailed("An http(s) URL names one query node, e.g. http://host:8093")
    ssl_fields: dict[str, str] = {"mode": "verify-full" if scheme in ("couchbases", "https") else "disable"}
    dropped: list[str] = []
    for key, value in parse_qsl(query or "", keep_blank_values=True):
        if key.lower() in _CA_KEYS:
            ssl_fields["ca_file"] = value
        elif key.lower() == "ssl" and value.lower() == "no_verify":
            ssl_fields["mode"] = "require"
        else:
            dropped.append(key)
    if dropped:
        logger.warning("Dropping Couchbase URL parameters DbaGuy does not take: {}", ", ".join(sorted(set(dropped))))
    database = unquote(path.lstrip("/")) or None
    first = items[0][0]
    draft = ServerProfileDraft(
        engine="couchbase", name=f"{first}/{database}" if database else first,
        host=",".join(f"[{h}]" if ":" in h else h for h, _ in items),
        port=items[0][1] if http_url else None,  # a couchbase:// port is KV (11210) or the old 8091: never the query port
        database=database, username=unquote(username) or None, ssl=SslConfig(**ssl_fields),
    )
    return ParsedUrl(profile=draft, password=unquote(password) if colon else None)
```

- [ ] **Step 4: Implement `nodes.py`.**

```python
"""Which query nodes a Couchbase profile can use (spec §3.6.3): the cluster's nodeServices map, asked through a seed's
manager port (works for limited users; anonymous gets 401), or else the seeds themselves."""

DISCOVERY_TIMEOUT_S = 3.0
NODE_SERVICES_PATH = "/pools/default/nodeServices"
Fetch = Callable[[str, int], bytes]


class DiscoveryFailed(Exception):
    """The manager answered, but not with a usable nodeServices map."""


@dataclass(frozen=True)
class QueryNode:
    host: str
    port: int

    @property
    def label(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def _external(entry: Any) -> dict[str, Any]:
    alternate = entry.get("alternateAddresses") if isinstance(entry, dict) else None
    external = alternate.get("external") if isinstance(alternate, dict) else None
    return external if isinstance(external, dict) else {}


def parse_node_services(body: Any, asked_host: str, tls: bool) -> list[QueryNode]:
    entries = body.get("nodesExt") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise ValueError("not a nodeServices map")
    service = "n1qlSSL" if tls else "n1ql"
    external = any(_external(e).get("hostname") == asked_host for e in entries)
    asked: list[QueryNode] = []
    others: list[QueryNode] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        alternate = _external(entry)
        if external and alternate.get("hostname"):
            host, ports = alternate["hostname"], alternate.get("ports") or {}
        else:  # no hostname: the node that was asked (A8)
            host, ports = entry.get("hostname") or asked_host, entry.get("services") or {}
        port = ports.get(service) if isinstance(ports, dict) else None
        if type(port) is not int:
            continue  # no query service on this node
        node = QueryNode(host[1:-1] if host.startswith("[") else host, port)
        (asked if entry.get("thisNode") is True or node.host == asked_host else others).append(node)
    return asked + others


def _fetcher(tls: ssl.SSLContext | None, auth: str) -> Fetch:
    def fetch(host: str, port: int) -> bytes:
        conn = (http.client.HTTPSConnection(host, port, timeout=DISCOVERY_TIMEOUT_S, context=tls) if tls is not None
                else http.client.HTTPConnection(host, port, timeout=DISCOVERY_TIMEOUT_S))
        try:
            conn.request("GET", NODE_SERVICES_PATH, headers={"Authorization": auth})
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        if response.status != 200:
            raise DiscoveryFailed(f"HTTP {response.status}")
        return body
    return fetch


def discover(seeds, manager_port, *, tls, auth, fetch=None):
    fetch = fetch or _fetcher(tls, auth)
    reasons: list[str] = []
    for seed in seeds:
        try:
            nodes = parse_node_services(json.loads(fetch(seed, manager_port)), seed, tls is not None)
        except Exception as exc:  # unreachable, 401, a timeout, not JSON …: the next seed, then the seeds themselves
            reasons.append(f"{seed}: {failure_reason(exc)}")
            continue
        if nodes:
            return nodes, None
        reasons.append(f"{seed}: no query node in the cluster map")
    return [], "; ".join(reasons)


def candidate_nodes(settings: CouchbaseSettings, auth: str, fetch: Fetch | None = None) -> tuple[list[QueryNode], str]:
    seeds = [QueryNode(host, settings.port) for host in settings.seeds]
    if not settings.discover:
        return seeds, "the SSH tunnel" if settings.tunnel else "the given hosts (no discovery)"
    nodes, failure = discover(settings.seeds, settings.manager_port, tls=settings.tls, auth=auth, fetch=fetch)
    if nodes:
        return nodes, f"{len(nodes)} query node(s) from the cluster map"
    logger.info("Couchbase node discovery failed ({}); trying the given hosts", failure)
    return seeds, f"the given hosts (discovery failed: {failure})"
```

(Annotate `discover` with the types listed under Interfaces.)

- [ ] **Step 5: Run them**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_settings.py -q`
Expected: PASS, including the dev-cluster discovery (record A8: the node entry had no `hostname`, or what it had).

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/settings.py backend/dbaguy_backend/adapters/couchbase/nodes.py \
  backend/tests/test_couchbase_settings.py docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md
git commit -m "feat(couchbase): connection settings, URL import, TLS contexts and nodeServices discovery"
```

---

### Task 10: Keyspace tree, generated DDL and schema snapshot

**Files:**
- Modify: `backend/dbaguy_backend/domain/metadata.py` (`NodeKind.BUCKET`, `SCOPE`, `COLLECTION`), `backend/dbaguy_backend/domain/schema.py` (`SnapshotKeyspace`, `SchemaSnapshot.keyspaces`)
- Create: `backend/dbaguy_backend/adapters/couchbase/catalog.py`
- Create: `backend/tests/test_couchbase_catalog.py`

**Interfaces:**
- Consumes: `force_quote_ident(Dialect.SQLPP, …)` (Task 2); `couchbase_query`, `COUCHBASE_READER`, `requires_couchbase` (Task 1); `NotFound` (domain errors); `make_node`, `ObjectDetail`, `PathSegment`.
- Produces:
  - `NodeKind.BUCKET = "bucket"`, `NodeKind.SCOPE = "scope"`, `NodeKind.COLLECTION = "collection"` (`INDEX` is reused). Paths: `[bucket:b]`, `[bucket:b, scope:s]`, `[bucket:b, scope:s, collection:c]`, `[…, index:i]`.
  - `class SnapshotKeyspace(BaseModel): bucket: str; scope: str; collection: str; system: bool = False`; `SchemaSnapshot.keyspaces: list[SnapshotKeyspace] = []` (relational engines leave it empty).
  - `catalog.Query = Callable[[str, Mapping[str, str]], list[Any]]` — runs one read-only statement with named parameters (name without `$` → JSON literal text) and returns the decoded result rows. Task 11 passes `CouchbaseConnection.query`; the tests pass the dev cluster through `couchbase_query`.
  - `list_children(query: Query, path: list[PathSegment]) -> list[ObjectNode]`, `object_detail(query: Query, path: list[PathSegment]) -> ObjectDetail`, `build_snapshot(query: Query) -> SchemaSnapshot`.
  - `index_ddl(index: Mapping[str, Any], bucket: str, scope: str, collection: str) -> str` and `index_detail(index: Mapping[str, Any]) -> str` (pure).

Rules (spec §3.8; survey §5): system catalogs only (filtered by the user's privileges, one query port is enough; `/indexStatus` and the scopes manifest are 403 for limited users), every name passed as a parameter, `ORDER BY` everywhere. Buckets: `has_children`. Scopes of a bucket: `system:scopes` plus `_default` (never listed there) first, `_system` last and `system=True`. Collections of a scope: `system:keyspaces` (no `namespace` predicate, P35: it lists only datastore keyspaces, all in the one namespace `default`, so there is nothing to filter, and its 7.x rows carry both `namespace` and `namespace_id`; this supersedes the query text of spec §3.8; the row field names are Task 1's A22; `namespace` is a reserved word, so a query that ever names it back-ticks it, R-04a-C6); for `_default`, the bucket-level keyspace row (no `bucket` field, `name = $b`) is the `_default` collection, listed first. Collections `has_children` (their indexes hang right under them). Indexes of a collection: `system:indexes` by `bucket_id`/`scope_id`/`keyspace_id`; `_default._default` indexes have `keyspace_id = $b` and no `bucket_id`. Index `detail` = state, `primary · ` in front for a primary index. Details, all `ddl_is_generated=True`: bucket — no DDL, a comment that cluster management is not part of DbaGuy; scope — ``CREATE SCOPE `b`.`s`;`` (`_default`: a comment line, it exists with the bucket; ruling P9); collection — ``CREATE COLLECTION `b`.`s`.`c`;`` (or that comment for `_default._default`) followed by every index's DDL; index — the generated `CREATE [PRIMARY] INDEX … ON `b`.`s`.`c`(keys) [PARTITION BY …] [WHERE …] [WITH {…}];` with `index_key` / `condition` / `partition` pasted as the server wrote them, `defer_build` only for a deferred index, `num_replica` only when above 0; its comment = state, last scan, `using`. A path that names nothing → `NotFound` (the metadata connection stays). Snapshot: one `system:keyspaces` query (buckets follow from it; ruling P7).

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_couchbase_catalog.py`:

```python
import json
import time

import pytest

from dbaguy_backend.adapters.couchbase.catalog import (
    build_snapshot, index_ddl, index_detail, list_children, object_detail,
)
from dbaguy_backend.domain.errors import NotFound
from dbaguy_backend.domain.metadata import PathSegment
from tests.support import COUCHBASE_PASSWORD, COUCHBASE_READER, couchbase_query, requires_couchbase


def path(*pairs):
    return [PathSegment(kind=kind, name=name) for kind, name in pairs]


@pytest.mark.parametrize("index, ddl", [
    ({"name": "#primary", "is_primary": True, "index_key": [], "state": "online", "metadata": {"num_replica": 0}},
     "CREATE PRIMARY INDEX `#primary` ON `b`.`s`.`c`;"),
    ({"name": "idx_amount", "index_key": ["`amount` DESC"], "state": "online"},
     "CREATE INDEX `idx_amount` ON `b`.`s`.`c`(`amount` DESC);"),
    ({"name": "adv", "index_key": ["(distinct (array `t` for `t` in `tags` end))", "`name`"], "condition": "(`type` = \"x\")",
      "partition": "HASH(`id`)", "state": "deferred", "metadata": {"num_replica": 1}},
     "CREATE INDEX `adv` ON `b`.`s`.`c`((distinct (array `t` for `t` in `tags` end)), `name`) PARTITION BY HASH(`id`) "
     "WHERE (`type` = \"x\") WITH {\"defer_build\": true, \"num_replica\": 1};"),
    ({"name": "odd`name", "index_key": ["`a`"], "state": "online"}, "CREATE INDEX `odd``name` ON `b`.`s`.`c`(`a`);"),
])
def test_index_ddl(index, ddl):
    assert index_ddl(index, "b", "s", "c") == ddl


def test_index_detail():
    assert index_detail({"is_primary": True, "state": "online"}) == "primary · online"
    assert index_detail({"state": "deferred"}) == "deferred"


class Recording:
    """A Query that answers from a table and records what it was asked."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, statement, named):
        self.asked.append((statement, dict(named)))
        return next(rows for words, rows in self.answers if words in statement)


def test_names_are_parameters_never_spliced():
    query = Recording([("system:scopes", ["app"]), ("system:keyspaces", [])])
    list_children(query, path(("bucket", "x\"; DROP")))
    statement, named = query.asked[0]
    assert "DROP" not in statement and named == {"b": json.dumps('x"; DROP')}


def test_a_path_that_names_nothing_is_not_found():
    with pytest.raises(NotFound):
        object_detail(Recording([("system:indexes", [])]), path(("bucket", "b"), ("scope", "s"), ("collection", "c"), ("index", "nope")))


def dev_query(user="dbaguy"):
    def query(statement, named):
        response = couchbase_query(statement, {f"${k}": json.loads(v) for k, v in named.items()},
                                   user=user, password=COUCHBASE_PASSWORD)
        assert not response.get("errors"), response.get("errors")
        return response["results"]
    return query


@requires_couchbase
def test_the_dev_cluster_tree():
    query = dev_query()
    assert [n.name for n in list_children(query, [])] == ["dbaguy", "travel-sample"]
    scopes = list_children(query, path(("bucket", "dbaguy")))
    names = [s.name for s in scopes]
    assert names[0] == "_default" and {"app", "many"} <= set(names)
    assert "_system" not in names[:-1] and all(s.system == (s.name == "_system") for s in scopes)  # last, when listed
    started = time.monotonic()
    many = list_children(query, path(("bucket", "dbaguy"), ("scope", "many")))
    assert len(many) == 300 and many[0].name == "c001" and time.monotonic() - started < 1.0  # spec: < 0.5 s locally
    assert [c.name for c in list_children(query, path(("bucket", "dbaguy"), ("scope", "_default")))] == ["_default"]
    users = path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "users"))
    indexes = {n.name: n.detail for n in list_children(query, users)}
    assert indexes == {"#primary": "primary · online", "idx_users_deferred": "deferred"}
    detail = object_detail(query, [*users, PathSegment(kind="index", name="idx_users_deferred")])
    assert detail.ddl == "CREATE INDEX `idx_users_deferred` ON `dbaguy`.`app`.`users`(`name`) WITH {\"defer_build\": true};"
    assert detail.ddl_is_generated and "deferred" in detail.comment
    collection = object_detail(query, users)
    assert collection.ddl.startswith("CREATE COLLECTION `dbaguy`.`app`.`users`;\n\nCREATE PRIMARY INDEX `#primary`")


@requires_couchbase
def test_a_limited_user_sees_only_its_keyspace():  # A18 for indexes
    query = dev_query(COUCHBASE_READER)
    assert [n.name for n in list_children(query, [])] == ["dbaguy"]
    assert [n.name for n in list_children(query, path(("bucket", "dbaguy"), ("scope", "app")))] == ["users"]
    users = path(("bucket", "dbaguy"), ("scope", "app"), ("collection", "users"))
    assert {n.name for n in list_children(query, users)} == set()  # A18: no index listing for query_select + data_reader
    snapshot = build_snapshot(query)
    assert [(k.bucket, k.scope, k.collection) for k in snapshot.keyspaces] == [("dbaguy", "app", "users")]


@requires_couchbase
def test_the_snapshot_holds_every_keyspace():
    snapshot = build_snapshot(dev_query())
    keyspaces = {(k.bucket, k.scope, k.collection) for k in snapshot.keyspaces}
    assert ("dbaguy", "_default", "_default") in keyspaces and ("travel-sample", "inventory", "airline") in keyspaces
    assert len([k for k in keyspaces if k[:2] == ("dbaguy", "many")]) == 300
    assert snapshot.schemas == [] and snapshot.database is None
    assert all(k.system == (k.scope == "_system") for k in snapshot.keyspaces)
```

(A18 is recorded: the limited user sees no index at all, so the test expects the empty set and the tree simply shows no index for such users.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_catalog.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.catalog`.

- [ ] **Step 3: Implement the domain additions** (`NodeKind` members with the comment `# Couchbase: bucket › scope › collection › index`; `SnapshotKeyspace`; `SchemaSnapshot.keyspaces: list[SnapshotKeyspace] = []  # Couchbase keyspaces; relational engines leave it empty`).

- [ ] **Step 4: Implement `catalog.py`.**

```python
"""The Couchbase object tree from system catalogs (spec §3.8): buckets › scopes › collections › indexes, generated DDL
and the schema snapshot. Every name goes in as a named parameter; read-only queries only (the caller sends
readonly=true). Pure apart from the Query callable."""

Query = Callable[[str, Mapping[str, str]], list[Any]]
DEFAULT = "_default"
SYSTEM_SCOPE = "_system"
_BUCKETS = "SELECT RAW b.name FROM system:buckets AS b ORDER BY b.name"
_SCOPES = "SELECT RAW s.name FROM system:scopes AS s WHERE s.`bucket` = $b ORDER BY s.name"
# No `namespace` predicate in the keyspace queries (P35): system:keyspaces lists only the datastore's keyspaces, all in
# the one data namespace `default` (the 7.x rows carry it as both `namespace` and `namespace_id`, A22), so there is
# nothing to filter. `namespace` is a reserved word: a query that ever names it back-ticks it (R-04a-C6).
_COLLECTIONS = ("SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` = $b AND k.`scope` = $s "
                "ORDER BY k.name")
_BUCKET_KEYSPACE = ("SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` IS MISSING AND k.name = $b")
_INDEXES = ("SELECT RAW i FROM system:indexes AS i WHERE i.bucket_id = $b AND i.scope_id = $s AND i.keyspace_id = $c "
            "ORDER BY i.name")
_DEFAULT_INDEXES = ("SELECT RAW i FROM system:indexes AS i WHERE i.keyspace_id = $b AND i.bucket_id IS MISSING "
                    "ORDER BY i.name")
_KEYSPACES = "SELECT k.`bucket` AS b, k.`scope` AS s, k.name AS n FROM system:keyspaces AS k"
_BUILT_IN = "-- exists with the bucket: it cannot be created or dropped"


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ident(name: str) -> str:
    return force_quote_ident(Dialect.SQLPP, name)


def _keyspace(bucket: str, scope: str, collection: str) -> str:
    return f"{_ident(bucket)}.{_ident(scope)}.{_ident(collection)}"


def index_detail(index: Mapping[str, Any]) -> str:
    state = str(index.get("state") or "unknown")
    return f"primary · {state}" if index.get("is_primary") else state


def index_ddl(index: Mapping[str, Any], bucket: str, scope: str, collection: str) -> str:
    """CREATE [PRIMARY] INDEX from a system:indexes row (/indexStatus is 403 for limited users, survey §5.2): key and
    condition expressions as the server wrote them; WITH only for what differs from the default."""
    on = _keyspace(bucket, scope, collection)
    if index.get("is_primary"):
        text = f"CREATE PRIMARY INDEX {_ident(index['name'])} ON {on}"
    else:
        text = f"CREATE INDEX {_ident(index['name'])} ON {on}({', '.join(index.get('index_key') or [])})"
    if index.get("partition"):
        text += f" PARTITION BY {index['partition']}"
    if index.get("condition"):
        text += f" WHERE {index['condition']}"
    with_: dict[str, Any] = {}
    if index.get("state") == "deferred":
        with_["defer_build"] = True
    replicas = (index.get("metadata") or {}).get("num_replica")
    if type(replicas) is int and replicas > 0:
        with_["num_replica"] = replicas
    if with_:
        text += f" WITH {json.dumps(with_)}"
    return text + ";"
```

`list_children(query, path)` dispatches on `[segment.kind for segment in path]`: `[]` → buckets (`make_node([], NodeKind.BUCKET, name, has_children=True)`); `[bucket]` → scopes (`_default` first, the listed ones except `_default` / `_system`, then `_system` with `system=True` when listed); `[bucket, scope]` → collections (`_default` scope: `_BUCKET_KEYSPACE` first as `_default`, then `_COLLECTIONS` without a second `_default`); `[bucket, scope, collection]` → indexes (`_DEFAULT_INDEXES` with `{"b": …}` for `_default._default`, else `_INDEXES`), each `make_node(path, NodeKind.INDEX, i["name"], has_children=False, detail=index_detail(i))`; anything else → `NotFound("No such object")`. Parameters are `{"b": _literal(bucket), "s": _literal(scope), "c": _literal(collection)}` as needed.

`object_detail(query, path)`: bucket → `ObjectDetail(path, kind="bucket", name, ddl=None, ddl_is_generated=True, comment="Buckets are created and managed in the Couchbase Web Console: cluster management is not part of DbaGuy")`; scope → `CREATE SCOPE {_ident(b)}.{_ident(s)};` or `_BUILT_IN` for `_default`; collection → `CREATE COLLECTION {_keyspace(b, s, c)};` (or `_BUILT_IN` for `_default._default`), a blank line, and every index DDL separated by blank lines; index → its row found by name in the collection's index list (none → `NotFound`), `ddl=index_ddl(…)`, `comment=" · ".join(part for part in (state, f"last scanned {last_scan_time}" if present, f"using {using}" if present))`.

`build_snapshot(query)`:

```python
def build_snapshot(query: Query) -> SchemaSnapshot:
    """Every keyspace the user can see, from one query (ruling P7); the bucket-level row is `_default._default`."""
    keyspaces = []
    for row in query(_KEYSPACES, {}):
        bucket, scope, name = row.get("b"), row.get("s"), row["n"]
        if bucket is None:
            bucket, scope, name = name, DEFAULT, DEFAULT
        keyspaces.append(SnapshotKeyspace(bucket=bucket, scope=scope, collection=name, system=scope == SYSTEM_SCOPE))
    keyspaces.sort(key=lambda k: (k.bucket, k.scope, k.collection))
    return SchemaSnapshot(database=None, default_schema=None, search_path=[], schemas=[], keyspaces=keyspaces,
                          built_at=datetime.now(UTC))
```

- [ ] **Step 5: Run them**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_catalog.py -q`
Expected: PASS; record A18 (does `system:indexes` filter by privileges for `dbaguy_reader`?) in the assumptions table, and note the time the 300-collection listing took for Task 13.

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/domain/metadata.py backend/dbaguy_backend/domain/schema.py \
  backend/dbaguy_backend/adapters/couchbase/catalog.py backend/tests/test_couchbase_catalog.py \
  docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md
git commit -m "feat(couchbase): bucket › scope › collection › index tree from system catalogs, generated DDL, keyspace snapshot"
```

---

### Task 11: The Couchbase adapter: connect, run, results, cancel, read-only, no transactions; registration

**Files:**
- Create: `backend/dbaguy_backend/adapters/couchbase/result.py`, `backend/dbaguy_backend/adapters/couchbase/adapter.py`
- Modify: `backend/dbaguy_backend/container.py` (register `CouchbaseAdapter`)
- Modify: `backend/tests/test_api.py` (`test_engines`)
- Create: `backend/tests/test_couchbase_result.py`, `backend/tests/test_couchbase_adapter.py`, `backend/tests/test_couchbase_sessions.py`
- Scratchpad only, not committed: `<scratchpad>/a9_a13_probe.py`

**Interfaces:**
- Consumes: everything from Tasks 2–10: `is_transaction_statement` (3); `RunOptions`, `RawJsonRows`, capability fields, `invalid_text_error`, `QueryContext` (4); `one_line` (5); `run_options`-checked options and `WorkerHooks.capabilities` (6); `EventStream` inputs `QueryResponse` / `ChannelError` / `NodeClient` / `QueryRequest` / `build_body` / `client_context_id` / `basic_auth` and `ResponseParser` events (7); `build_shape` / `needs_first_page` / `has_bare_star` / `STAR_PEEK_*` and `statement_error` / `warning_messages` / `metrics_message` / `PROTOCOL_ERROR` (8); `connection_settings` / `endpoint_target` / `request_context` / `parse_couchbase_url` / `candidate_nodes` / `QUERY_PORT` / `URL_SCHEMES` (9); `catalog.list_children` / `object_detail` / `build_snapshot` (10); `app_version()`.
- Produces (`adapters/couchbase/result.py`):
  - `class EventStream(response: QueryResponse | FakeResponse-like, *, cancelled: Callable[[], bool], node: str)` — `next() -> Event | None` (None after `EndEvent`; once the object has closed, the rest of the body is read so the connection can be kept), `done: bool`, `close()` (abandons an unfinished response). Read failures become `StatementCancelled` when a cancel was asked for, else `StatementError(code="connection_lost")` (also for a response cut short) or `StatementError(PROTOCOL_ERROR, code="protocol_error")` (WARNING log with offset and field only).
  - `start_result(stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool, on_done: Callable[[], None]) -> CouchbaseResult` — reads the head up to `ResultsStart` or the end; errors there raise (`statement_error`), `status:"stopped"` without errors raises `StatementCancelled("Query was stopped on the server")`.
  - `class CouchbaseResult(StatementResult, RawJsonRows)` — `columns()` peeks the first page for a `*` signature (inside the worker's timeout); `fetch(n)` / `fetch_raw(n)`; an error arriving after rows is kept and raised by the next `fetch()` that has no rows, `next_result()` or `affected_rows()` (Review Focus 2); `affected_rows()` reads to the end (`mutationCount`, 0 for DML without it, else None); `take_messages()` (warnings as they come, the metrics line at the end, one WARNING for keys outside the columns); `presentation()`; `close()`.
- Produces (`adapters/couchbase/adapter.py`):
  - `PAUSED_RESULT_MAX_IDLE_S = 120.0`; `TRANSACTIONS_UNSUPPORTED = "transactions_unsupported"`.
  - `class CouchbaseAdapter(DatabaseAdapter)` — `engine = Engine.COUCHBASE`, `url_schemes = URL_SCHEMES`, `default_port = QUERY_PORT`, `dialect = Dialect.SQLPP`, `capabilities = AdapterCapabilities(server_read_only=True, transactions=False, query_context=True, parameters=True, documents=False, executed_plan=False, plan_format="couchbase", paused_result_max_idle_s=PAUSED_RESULT_MAX_IDLE_S)` (documents: 04c; executed plan: 04b; ruling P6); `parse_url`, `endpoint_target`, `open`.
  - `class CouchbaseConnection(DatabaseConnection)` — `run(sql, options)`, `query(statement: str, named: Mapping[str, str] | None = None) -> list[Any]` (internal, read-only, read to the end, decoded rows; the catalog's `Query`), `cancel()` / `force_cancel()` / `abort()` (any thread), `cancelled() -> bool`, `last_client_context_id: str | None` (tests and DEBUG logs), `identify(username: str) -> ServerInfo`, no-transaction methods, `server_info()`, `current_database()`, catalog delegation, `close()`.
  - Registered in `container.default_registry()` between SQL Server and SQLite.

Request flow (spec §3.7): `run()` refuses transaction statements first (`is_transaction_statement` → `StatementError(…, code="transactions_unsupported")`, nothing sent); refuses text that is not valid UTF-8 (`invalid_text_error`); closes a result left open; `_begin()` marks the connection busy and the cancel flag clear under a small lock, builds the body (`client_context_id(uuid4().hex)`, which adds the `dbaguy-` prefix — `run()` gets no request id, rulings P30, P47; `query_context` from the options; `readonly` for a read-only profile; parameters as given), and sends it through the `NodeClient` with an `attach` callback that, under the same lock, refuses when a cancel already came (nothing sent) or records the channel a cancel must shut down. An answer that is not JSON (a proxy's page) → `StatementError(f"HTTP {status} {reason}", code=f"http_{status}")`. Then `start_result`. The busy state ends when the response has been read to its end or the result is closed (`on_done`). `cancel()` with nothing busy does nothing and sets no flag — a deviation from spec §3.7.2, which says an idle cancel only sets the flag (ruling P46: `_begin()` would clear it anyway, and the worker's `_cancel_requested` covers the window before `run()`); otherwise it sets the flag and shuts the channel down (if one is attached yet); every read error under the flag is a cancel (and, only if Task 1's A10 probe failed, so does any other exception the reader gets under the flag: ruling P41, Step 3). Internal queries (`query`) use the same path with `readonly=true` and `retry_safe=True`.

Connect (spec §3.6.3): `connection_settings` → `basic_auth` → `candidate_nodes` (INFO line on how) → for each node a `NodeClient`, a `CouchbaseConnection`, and `identify()`: `SELECT ds_version() AS v, CURRENT_USERS() AS u` (internal, so `readonly=true`); if that fails with a server error other than authentication, `SELECT ds_version() AS v` and the profile's user name (A17). A refused / timed-out / unknown node → the next one; authentication (`10000`, `13014`, `http_401`) → `ConnectionFailed(<server text> + "\nCheck the user name and password")` at once; TLS (`tls_failed`) → `ConnectionFailed(<ssl text> + "\nSet the cluster's CA file in the SSL tab")` at once; none left → `ConnectionFailed("Could not reach a query node: cb1:8093 (refused), cb2:8093 (timed out)")`. `ServerInfo(engine=COUCHBASE, version=f"Couchbase Server {v}", edition=enterprise|community from the suffix, current_user=first of CURRENT_USERS() or the profile's, current_database=default_context.text() or None, default_query_context=default_context)`; INFO `Connected to Couchbase Server <v> at <node> via <how> (tls=<mode>[, read-only])` — no user, no password.

- [ ] **Step 1: Write the failing unit tests** — `backend/tests/test_couchbase_result.py`:

```python
import pytest

from dbaguy_backend.adapters.base import Endpoint, StatementCancelled, StatementError
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from dbaguy_backend.adapters.couchbase.http import NodeClient, basic_auth
from dbaguy_backend.adapters.couchbase.result import EventStream, start_result
from dbaguy_backend.adapters.couchbase.rows import OVERFLOW_NAME
from dbaguy_backend.adapters.couchbase.settings import connection_settings
from dbaguy_backend.adapters.values import MISSING, JsonText
from dbaguy_backend.domain.execution import ErrorPosition, MessageSeverity
from dbaguy_backend.domain.metadata import ServerInfo
from dbaguy_backend.domain.profiles import Engine
from tests.couchbase_fakes import FakeQueryServer, FakeResponse, Reply
from tests.support import make_couchbase_profile


def result_of(*chunks, sql="SELECT * FROM ks", plan=False, bare_star=True, dml=False, cancelled=lambda: False):
    released = []
    response = FakeResponse(list(chunks))
    stream = EventStream(response, cancelled=cancelled, node="n:8093")
    result = start_result(stream, sql, plan=plan, bare_star=bare_star, dml=dml, on_done=lambda: released.append(1))
    return result, response, released


def test_errors_after_the_last_row_raise_from_next_result():  # Review Focus 2
    body = (b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2}],'
            b'"errors":[{"code":1080,"msg":"Timeout 3s exceeded"}],"status":"fatal","metrics":{"resultCount":2}}')
    result, _, released = result_of(body[:40], body[40:])
    assert [c.name for c in result.columns()] == ["a"]
    assert result.fetch(200) == [[1], [2]]  # the rows come first, the request has ended
    assert released and [m.text for m in result.take_messages()] == ["2 results"]
    with pytest.raises(StatementError) as error:
        result.next_result()
    assert (error.value.code, error.value.message) == ("1080", "Timeout 3s exceeded")


def test_an_error_after_a_full_page_comes_on_the_next_fetch():
    body = b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2}],"errors":[{"code":5010,"msg":"boom"}],"status":"fatal"}'
    result, _, _ = result_of(body)
    result.columns()
    assert result.fetch(2) == [[1], [2]]
    with pytest.raises(StatementError, match="boom"):
        result.fetch(1)


def test_errors_before_results_raise_from_the_start():
    body = b'{"requestID":"r","errors":[{"code":3000,"msg":"syntax error","line":1,"column":10}],"status":"fatal"}'
    with pytest.raises(StatementError) as error:
        result_of(body, sql="SELECT 😀 WHER")
    assert error.value.position == ErrorPosition(line=1, column=11)


def test_stopped_without_errors_is_a_cancel():  # Review Focus 2
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1}],"status":"stopped"}')
    result.columns()
    assert result.fetch(10) == [[1]]
    with pytest.raises(StatementCancelled, match="stopped on the server"):
        result.next_result()
    with pytest.raises(StatementCancelled):
        result_of(b'{"requestID":"r","status":"stopped"}')


def test_dml_counts_its_mutations():
    result, _, _ = result_of(b'{"results":[],"status":"success","metrics":{"mutationCount":3}}', sql="UPDATE ks SET a = 1",
                             dml=True)
    assert result.columns() is None and result.affected_rows() == 3
    assert result_of(b'{"results":[],"status":"success","metrics":{}}', dml=True)[0].affected_rows() == 0
    assert result_of(b'{"status":"success"}', sql="CREATE INDEX i ON ks(a)")[0].affected_rows() is None


@pytest.mark.parametrize("was_cancelled, code", [(False, "connection_lost"), (True, "cancelled")])
def test_a_cut_response_keeps_the_rows_read(was_cancelled, code):
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1},', cancelled=lambda: was_cancelled)
    result.columns()
    assert result.fetch(10) == [[1]]
    with pytest.raises(StatementError) as error:
        result.fetch(10)
    assert error.value.code == code


def test_a_malformed_response_is_a_protocol_error():
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1} x {"a":2}]}')
    result.columns()
    with pytest.raises(StatementError) as error:
        result.fetch(10)
        result.fetch(10)
    assert (error.value.code, error.value.message) == ("protocol_error", "The query service sent a malformed response")


def test_the_star_peek_reads_at_most_about_8_mib():
    row = '{"k":"' + "x" * (1024 * 1024) + '"}'
    chunks = [b'{"signature":{"*":"*"},"results":['] + [(row + ",").encode() for _ in range(19)] + [(row + ']}').encode()]
    result, response, _ = result_of(*chunks, bare_star=False)
    assert [c.name for c in result.columns()] == ["k", OVERFLOW_NAME]
    assert len(response._chunks) >= 10  # not everything was read to build the columns
    assert len(result.fetch(100)) == 20  # the peeked rows come first, then the rest


def test_a_cancel_during_the_star_peek_keeps_the_rows_read():  # Review Focus 3
    result, _, _ = result_of(b'{"signature":{"*":"*"},"results":[{"a":1},{"a":2},', bare_star=False,
                             cancelled=lambda: True)
    assert [c.name for c in result.columns()] == ["a", OVERFLOW_NAME]  # built from the rows that arrived
    assert result.fetch(10) == [[1, MISSING], [2, MISSING]]
    with pytest.raises(StatementCancelled):
        result.fetch(10)


def test_rows_for_a_raw_export_are_the_server_text():
    result, _, _ = result_of(b'{"signature":{"*":"*"},"results":[{"d":{"b":1.10, "a":[1e400]}}],"status":"success"}')
    result.columns()
    assert result.fetch_raw(10) == ['{"d":{"b":1.10, "a":[1e400]}}']


def test_closing_early_abandons_and_releases_once():
    result, response, released = result_of(b'{"signature":{"a":"json"},"results":[{"a":1},')
    result.columns()
    result.close()
    result.close()
    assert response.abandoned and released == [1]
    finished, response, _ = result_of(b'{"signature":"json","results":[1],"status":"success"}')
    finished.fetch(10)
    finished.close()
    assert not response.abandoned  # read to the end: the connection is kept


def test_a_plan_is_one_cell():
    text = '{"plan":{"#operator":"Sequence"},"text":"SELECT 1"}'
    result, _, _ = result_of(('{"signature":"json","results":[' + text + '],"status":"success"}').encode(),
                             sql="EXPLAIN SELECT 1", plan=True)
    assert result.presentation() == "plan" and [c.name for c in result.columns()] == ["plan"]
    assert result.fetch(10) == [[JsonText(text)]]


def test_keys_outside_the_columns_warn_once():
    result, _, _ = result_of(b'{"signature":{"a":"json"},"results":[{"a":1,"z":2},{"a":3,"y":4}],"status":"success"}',
                             bare_star=False)
    result.columns()
    result.fetch(10)
    warnings = [m for m in result.take_messages() if m.severity == MessageSeverity.WARNING]
    assert [m.text for m in warnings] == ["Some rows have keys outside the columns (from row 1); export as JSON to see every key"]
    assert result.fetch(10) == [] and not [m for m in result.take_messages() if m.severity == MessageSeverity.WARNING]


def test_a_non_json_answer_is_reported_by_its_status():
    server = FakeQueryServer([Reply(b"<html>bad gateway</html>", status=502, content_type="text/html")])
    profile = make_couchbase_profile(port=server.port)
    settings = connection_settings(profile, Endpoint("127.0.0.1", server.port), None)
    client = NodeClient("127.0.0.1", server.port, auth=basic_auth("u", "p"), user_agent="t", tls=None)
    connection = CouchbaseConnection(client, settings=settings,
                                     info=ServerInfo(engine=Engine.COUCHBASE, version="Couchbase Server test"))
    try:
        with pytest.raises(StatementError) as error:
            connection.run("SELECT 1")
        assert (error.value.code, error.value.message) == ("http_502", "HTTP 502 Reason")
    finally:
        connection.close()
        server.close()
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_result.py -q`
Expected: `ModuleNotFoundError: dbaguy_backend.adapters.couchbase.result`.

- [ ] **Step 3: Implement `result.py`.**

```python
"""One Couchbase response as a StatementResult (spec §3.5, §3.7): rows read on demand from the wire, the `*` peek
inside columns(), messages from warnings and metrics, an error that follows rows raised where the worker will see it,
and each row's server text for raw JSON exports."""


class EventStream:
    def __init__(self, response: Any, *, cancelled: Callable[[], bool], node: str) -> None:
        self._response, self._cancelled, self._node = response, cancelled, node
        self._parser = ResponseParser()
        self._events: deque[Event] = deque()
        self._ended = False  # the response object has closed (EndEvent seen)
        self.done = False  # …and the body has been read to its end: the connection may carry the next request

    def next(self) -> Event | None:
        while not self._events:
            if self._ended:
                return None
            self._read()
        event = self._events.popleft()
        if isinstance(event, EndEvent):
            self._ended = True
            while not self.done:  # the last bytes (a newline, the end of the chunked body): usually already here
                self._read()
        return event

    def _read(self) -> None:
        try:
            chunk = self._response.read_chunk()
            events = self._parser.feed(chunk) if chunk else self._parser.finish()
        except ChannelError as exc:
            raise self._lost(str(exc)) from exc
        except ProtocolError as exc:
            if self._cancelled():
                raise StatementCancelled() from exc
            if exc.truncated:
                raise self._lost("the query node closed the connection mid-response") from exc
            logger.warning("Malformed response from the query node {} at offset {} (field {})", self._node, exc.offset,
                           exc.field)
            raise StatementError(PROTOCOL_ERROR, code="protocol_error") from exc
        self.done = not chunk
        self._events.extend(events)

    def _lost(self, reason: str) -> StatementError:
        if self._cancelled():
            return StatementCancelled()
        logger.warning("Connection to the query node {} was lost: {}", self._node, reason)
        return StatementError(f"Connection to the query node was lost: {reason}", code="connection_lost")

    def close(self) -> None:
        if not self.done:
            self._response.abandon()  # the server stops the request at once (`closed`)
            self.done = True
```

**Only if** Task 1 recorded that A10 failed (ruling P41: `HttpChannel.shutdown()` then also shuts down the TLS socket from the cancelling thread, Task 7 Step 8), the reader can get an exception of any type from that socket, not only `ChannelError` / `ProtocolError`. Then `_read` gets a last clause after the two above, `except Exception as exc:` with `if self._cancelled(): raise StatementCancelled() from exc` and a bare `raise` otherwise: any exception the reader gets while the cancel flag is set counts as cancelled (spec §9). If A10 holds, `_read` stays as written.

`CouchbaseResult` keeps: `_stream`, `_sql`, `_plan`, `_bare_star`, `_dml`, `_on_done` (called once, from `_end()` or `close()`), `_signature`, `_errors`, `_status`, `_metrics`, `_messages: list[ServerMessage]`, `_peeked: deque[RawRow]`, `_rows_done`, `_ended`, `_pending: StatementError | None`, `_shape`, `_shaped`. The key methods:

```python
    def _field(self, event: FieldEvent) -> None:
        match event.name:
            case "signature":
                self._signature = event.value
            case "errors":
                self._errors = event.value if isinstance(event.value, list) else [event.value]
            case "warnings":
                self._messages.extend(warning_messages(event.value if isinstance(event.value, list) else [event.value]))
            case "status":
                self._status = event.value
            case "metrics":
                self._metrics = event.value if isinstance(event.value, dict) else {}
                logger.debug("Couchbase metrics: {}", self._metrics)
            case "requestID" | "clientContextID":
                logger.debug("Couchbase {}: {}", event.name, event.value)

    def _end(self) -> None:
        """The response object closed: the metrics line, and what the statement ends with. Once."""
        if self._ended:
            return
        self._ended = self._rows_done = True
        if (line := metrics_message(self._metrics)) is not None:
            self._messages.append(line)
        if self._errors:
            self._pending = statement_error(self._errors, self._sql)
        elif self._status == "stopped":  # someone else stopped it (DELETE FROM system:active_requests)
            self._pending = StatementCancelled("Query was stopped on the server")
        self._release()

    def _next_row(self) -> RawRow | None:
        if self._peeked:
            return self._peeked.popleft()
        while not self._rows_done:
            event = self._stream.next()
            match event:
                case RowEvent(row=row):
                    return row
                case FieldEvent():
                    self._field(event)
                case ResultsEnd():
                    self._rows_done = True
                case EndEvent() | None:
                    self._end()
        return None

    def _read_to_end(self) -> None:
        while not self._ended:
            event = self._stream.next()
            if isinstance(event, FieldEvent):
                self._field(event)
            elif event is None or isinstance(event, EndEvent):
                self._end()
            # rows nobody asked for any more (only after a full read, or for statements without rows): dropped

    def _rows(self, n: int, convert: Callable[[RawRow], Any]) -> list[Any]:
        self.columns()
        out: list[Any] = []
        while len(out) < n:
            if self._pending is not None and not self._peeked:
                break  # a failure kept while earlier rows went out: those rows first, then it
            try:
                row = self._next_row()
            except StatementError as exc:
                if not out:
                    raise
                self._pending = exc  # the rows read so far go out first (Review Focus 2)
                break
            if row is None:
                break
            out.append(convert(row))
        if len(out) < n and self._pending is None:
            self._read_to_end()  # the rows are exhausted: errors, status and metrics follow them
        if self._shape is not None and (warning := self._shape.take_warning()):
            logger.warning("A Couchbase result has keys outside its columns after the first page")
            self._messages.append(ServerMessage(severity=MessageSeverity.WARNING, text=warning))
        if not out and self._pending is not None:
            raise self._take_pending()
        return out

    def columns(self) -> list[ColumnMeta] | None:
        if not self._shaped:
            first_page: list[RawRow] = []
            if needs_first_page(self._signature, self._plan):  # inside the worker's timeout (spec §3.5.1)
                size = 0
                while len(first_page) < STAR_PEEK_ROWS and (not first_page or size < STAR_PEEK_BYTES):
                    try:
                        row = self._next_row()
                    except StatementError as exc:
                        if not first_page:
                            raise
                        self._pending = exc  # the rows peeked so far still go out first
                        break
                    if row is None:
                        break
                    first_page.append(row)
                    size += len(row.text)  # characters, close enough to bytes for a limit (ruling P12)
                self._peeked.extend(first_page)
            self._shape = build_shape(self._signature, plan=self._plan, bare_star=self._bare_star, first_page=first_page)
            self._shaped = True
        return None if self._shape is None else self._shape.columns

    def fetch(self, n: int) -> list[list[Any]]:
        if n <= 0 or self.columns() is None:
            return []
        shape = self._shape
        return self._rows(n, shape.cells)

    def fetch_raw(self, n: int) -> list[str]:
        return [] if n <= 0 or self.columns() is None else self._rows(n, lambda row: row.text)

    def affected_rows(self) -> int | None:
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        count = self._metrics.get("mutationCount")
        return count if type(count) is int else (0 if self._dml else None)

    def next_result(self) -> bool:  # 04b adds the profile result set here
        self._read_to_end()
        if self._pending is not None:
            raise self._take_pending()
        return False
```

plus `take_messages()` (return and clear `_messages`), `presentation()` (`"plan" if self._plan else "grid"`), `_take_pending()` (return it and clear it), `_release()` (call `_on_done` once), and `close()` (`self._stream.close()`, `self._release()`). `start_result()`:

```python
def start_result(stream: EventStream, sql: str, *, plan: bool, bare_star: bool, dml: bool,
                 on_done: Callable[[], None]) -> "CouchbaseResult":
    """Reads the head of the response (spec §3.7.1 step 3): signature, early errors and warnings, up to the start of
    `results` or the end. `run()` returns quickly because the server sends the signature first."""
    result = CouchbaseResult(stream, sql, plan=plan, bare_star=bare_star, dml=dml, on_done=on_done)
    try:
        result.read_head()
    except BaseException:
        result.close()
        raise
    return result
```

with `read_head()`: read events into `_field` until `ResultsStart` (then return) or the end (`_end()`, then raise the pending error if any — a statement without `results` that failed or was stopped).

- [ ] **Step 4: Implement `adapter.py`.**

```python
"""Couchbase through the Query Service REST API (no SDK, R-04-1): SQL++ sent with the session's options, the response
streamed and shaped by result.py, cancel by shutting the socket down from any thread (the server marks the request
`closed`, R-04-5), read-only enforced by the server (`readonly=true`) as well as the classifier, and no transactions
(R-04-4)."""

PAUSED_RESULT_MAX_IDLE_S = 120.0  # R-04-6: a paused request holds a query servicer; the worker closes it after this
TRANSACTIONS_UNSUPPORTED = "transactions_unsupported"
_NO_TRANSACTIONS = ("SQL++ transactions (BEGIN WORK, COMMIT, ROLLBACK, SAVEPOINT) are not supported in DbaGuy yet; "
                    "every statement commits on its own")
_IDENTIFY = "SELECT ds_version() AS v, CURRENT_USERS() AS u"
_VERSION = "SELECT ds_version() AS v"
_DML = frozenset({"INSERT", "UPSERT", "UPDATE", "DELETE", "MERGE"})
_PLANS = frozenset({"EXPLAIN", "ADVISE"})
_AUTH_FAILURES = frozenset({"10000", "13014", "http_401"})
_TLS_FAILED = "tls_failed"
_NODE_DOWN = "node_down"


class CouchbaseConnection(DatabaseConnection):
    """All methods except cancel(), force_cancel() and abort() run on the owning worker thread."""

    def __init__(self, client: NodeClient, *, settings: CouchbaseSettings, info: ServerInfo) -> None:
        self._client = client
        self._read_only = settings.read_only
        self._default_context = settings.default_context
        self._info = info
        self._lock = threading.Lock()  # a leaf: guards the three fields below, never held across I/O
        self._busy = False  # from the start of a request until its response is read or abandoned
        self._channel: HttpChannel | None = None  # what a cancel shuts down
        self._cancelled = False  # a cancel reached the current request
        self._result: CouchbaseResult | None = None
        self._identified = False  # the node has answered once: from then on a failure is a lost connection
        self.last_client_context_id: str | None = None

    def _begin(self, sql: str, *, context: QueryContext | None, params: StatementParams | None, readonly: bool,
               retry_safe: bool) -> EventStream:
        with self._lock:
            self._busy, self._cancelled, self._channel = True, False, None
        try:
            request = QueryRequest(
                statement=sql, client_context_id=client_context_id(uuid4().hex),
                query_context=request_context(context) if context is not None else None, readonly=readonly,
                named=params.named if params else {}, positional=params.positional if params else (),
            )
            self.last_client_context_id = request.client_context_id
            logger.debug("Couchbase request {} to {}", request.client_context_id, self._client.label)
            response = self._client.send(build_body(request), retry_safe=retry_safe, attach=self._attach)
        except ChannelError as exc:
            self._end()
            raise self._channel_failure(exc) from exc
        except BaseException:
            self._end()
            raise
        self._identified = True
        logger.debug("Couchbase response {} {}", response.status, response.reason)
        if "json" not in response.content_type.lower():  # a proxy's page, not the query service
            response.abandon()
            self._end()
            raise StatementError(f"HTTP {response.status} {response.reason}", code=f"http_{response.status}")
        return EventStream(response, cancelled=self.cancelled, node=self._client.label)

    def _attach(self, channel: HttpChannel) -> None:
        with self._lock:
            if self._cancelled:
                raise StatementCancelled()  # a cancel came while the request was being prepared: nothing is sent
            self._channel = channel

    def _end(self) -> None:
        with self._lock:
            self._busy, self._channel = False, None

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _channel_failure(self, exc: ChannelError) -> StatementError:
        if self.cancelled():
            return StatementCancelled()
        if exc.tls:
            return StatementError(f"TLS failed: {exc}", code=_TLS_FAILED)
        if not self._identified:  # opening: this node is down, try the next one
            return StatementError(str(exc), code=_NODE_DOWN)
        logger.warning("Connection to the query node {} was lost: {}", self._client.label, exc)
        return StatementError(f"Connection to the query node was lost: {exc}", code="connection_lost")
```

(`_begin()` sets `_identified` once the node has answered; before that, a refused or timed-out node is `node_down`, which `open()` turns into "try the next one".)

```python
    def run(self, sql: str, options: RunOptions | None = None) -> StatementResult:
        options = options or RunOptions()
        if is_transaction_statement(sql, Dialect.SQLPP):  # D-04-13: an engine capability, not a safety decision
            raise StatementError(_NO_TRANSACTIONS, code=TRANSACTIONS_UNSUPPORTED)
        try:
            sql.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise invalid_text_error(sql, exc.start) from None
        self._close_result()
        stream = self._begin(sql, context=options.query_context, params=options.params, readonly=self._read_only,
                             retry_safe=options.retry_safe)
        first = first_keyword(sql, Dialect.SQLPP)
        self._result = start_result(stream, sql, plan=first in _PLANS, bare_star=has_bare_star(sql), dml=first in _DML,
                                    on_done=self._end)
        return self._result

    def query(self, statement: str, named: Mapping[str, str] | None = None) -> list[Any]:
        """An internal statement (catalog, identification): always read-only, read to the end, rows decoded."""
        self._close_result()
        params = StatementParams(named=dict(named)) if named else None
        stream = self._begin(statement, context=None, params=params, readonly=True, retry_safe=True)
        result = start_result(stream, statement, plan=False, bare_star=False, dml=False, on_done=self._end)
        try:
            rows: list[Any] = []
            while batch := result.fetch_raw(1000):
                rows.extend(json.loads(text) for text in batch)
            result.next_result()  # an error that came after the rows
            return rows
        finally:
            result.close()

    def cancel(self) -> None:
        started = time.monotonic()
        with self._lock:
            if not self._busy:
                return  # nothing running: no flag either (P46); a cancel must never reach the next statement
            self._cancelled = True
            channel = self._channel
        if channel is not None:
            channel.shutdown()
        logger.info("Couchbase cancel on {}: {} in {:.0f} ms", self._client.label,
                    "connection shut down" if channel is not None else "before the request went out",
                    (time.monotonic() - started) * 1000)

    def force_cancel(self) -> None:
        self.cancel()  # a shutdown already ends the request at once

    def abort(self) -> None:
        self.cancel()

    def identify(self, username: str) -> ServerInfo:
        """What answered (spec §3.6.3 step 4): version, edition and the current user, read-only like every internal
        query."""
        try:
            row = self.query(_IDENTIFY)[0]
        except StatementError as exc:
            if exc.code in _AUTH_FAILURES or exc.code in (_NODE_DOWN, _TLS_FAILED, "cancelled", "connection_lost"):
                raise
            row = {**self.query(_VERSION)[0], "u": None}  # A17: a limited user may not call CURRENT_USERS()
        version = str(row.get("v") or "")
        users = row.get("u")
        user = users[0] if isinstance(users, list) and users and isinstance(users[0], str) else username
        edition = "enterprise" if version.endswith("-enterprise") else "community" if version.endswith("-community") else None
        context = self._default_context
        self._info = ServerInfo(
            engine=Engine.COUCHBASE, version=f"Couchbase Server {version}", edition=edition, current_user=user,
            current_database=context.text() if context is not None else None, default_query_context=context,
        )
        return self._info

    def in_transaction(self) -> bool: return False
    def autocommit(self) -> bool: return True
    def set_autocommit(self, enabled: bool) -> None:
        if not enabled:
            raise StatementError(_NO_TRANSACTIONS, code=TRANSACTIONS_UNSUPPORTED)
    def commit(self) -> None: return None
    def rollback(self) -> None: return None
    def server_info(self) -> ServerInfo: return self._info
    def current_database(self) -> str | None: return self._info.current_database
    def list_children(self, path): return catalog.list_children(self.query, path)
    def object_detail(self, path): return catalog.object_detail(self.query, path)
    def schema_snapshot(self): return catalog.build_snapshot(self.query)
    def close(self) -> None:
        self._close_result()
        self._client.close()
```

`CouchbaseAdapter.open()`:

```python
    def open(self, profile: AnyProfile, endpoint: Endpoint | None, password: str | None,
             database: str | None) -> DatabaseConnection:
        if not isinstance(profile, ServerProfile) or endpoint is None:
            raise ValidationFailed("Not a Couchbase server profile")
        settings = connection_settings(profile, endpoint, database)
        auth = basic_auth(settings.username, password or "")
        nodes, how = candidate_nodes(settings, auth)
        failures: list[str] = []
        for node in nodes:
            client = NodeClient(node.host, node.port, auth=auth, user_agent=f"DbaGuy/{app_version()}", tls=settings.tls,
                                server_hostname=settings.server_hostname)
            connection = CouchbaseConnection(client, settings=settings,
                                             info=ServerInfo(engine=Engine.COUCHBASE, version="Couchbase Server"))
            try:
                info = connection.identify(settings.username)
            except StatementError as exc:
                connection.close()
                if exc.code == _NODE_DOWN:
                    failures.append(f"{node.label} ({exc.message})")
                    continue
                if exc.code in _AUTH_FAILURES:
                    raise ConnectionFailed(f"{exc.message}\nCheck the user name and password") from exc
                if exc.code == _TLS_FAILED:
                    raise ConnectionFailed(f"{exc.message}\nSet the cluster's CA file in the SSL tab") from exc
                raise ConnectionFailed(exc.message) from exc
            except BaseException:
                connection.close()
                raise
            logger.info("Connected to {} at {} via {} (tls={}{})", info.version, node.label, how, settings.tls_mode,
                        ", read-only" if settings.read_only else "")
            return connection
        raise ConnectionFailed("Could not reach a query node: " + ", ".join(failures))
```

The registry: `AdapterRegistry([PostgresAdapter(), MysqlAdapter(), MssqlAdapter(), CouchbaseAdapter(), SqliteAdapter()])`.

`tests/test_api.py::test_engines`: the set gains `"couchbase"`, and

```python
    couchbase = engines["couchbase"]
    assert couchbase["dialect"] == "sqlpp" and couchbase["default_port"] == 8093 and couchbase["server_read_only"]
    assert not couchbase["transactions"] and couchbase["query_context"] and couchbase["parameters"]
    assert not couchbase["documents"] and not couchbase["executed_plan"] and couchbase["plan_format"] == "couchbase"
```

(the loop over the other engines from Task 4 skips `couchbase`).

- [ ] **Step 5: Run the unit tests**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_result.py tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 6: Write the integration tests** — `backend/tests/test_couchbase_adapter.py` (`pytestmark = requires_couchbase`):

```python
import threading
import time
import uuid

import pytest

from dbaguy_backend.adapters.base import Endpoint, RunOptions, StatementCancelled, StatementError
from dbaguy_backend.adapters.couchbase import adapter as couchbase_adapter
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseAdapter
from dbaguy_backend.adapters.couchbase.rows import OVERFLOW_NAME
from dbaguy_backend.adapters.registry import AdapterRegistry
from dbaguy_backend.adapters.values import MISSING, JsonText as J, encode_row
from dbaguy_backend.connectivity.ssh_tunnel import SshTunnelManager, write_askpass_script
from dbaguy_backend.container import default_registry
from dbaguy_backend.domain.errors import ConnectionFailed, ValidationFailed
from dbaguy_backend.domain.execution import QueryContext, StatementParams
from dbaguy_backend.domain.profiles import SshConfig, SslConfig
from dbaguy_backend.services.connection_service import ConnectionService
from dbaguy_backend.services.profile_service import ProfileService, ResolvedSecrets
from dbaguy_backend.storage.paths import profiles_file
from dbaguy_backend.storage.profile_store import ProfileStore
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import (
    BASTION_PORT, COUCHBASE_PASSWORD, COUCHBASE_READER, LONG_AGGREGATE, completed_request_state, couchbase_ca_file,
    couchbase_query, make_couchbase_profile, requires_couchbase, requires_couchbase_bastion,
)
from tests.test_dev_env import U_NUMBERS
from tests.test_ssh_tunnel import TEST_SSH_OPTIONS

pytestmark = requires_couchbase
APP = QueryContext(bucket="dbaguy", scope="app")
U_KEYORDER = '{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'


def open_couchbase(password=COUCHBASE_PASSWORD, **overrides):
    profile = make_couchbase_profile(**overrides)
    adapter = CouchbaseAdapter()
    host, port = adapter.endpoint_target(profile)
    return adapter.open(profile, Endpoint(host, port), password, None)


@pytest.fixture
def connection():
    conn = open_couchbase()
    yield conn
    conn.close()


def read(connection, sql, options=None):
    """(column names, rows, message texts) of one statement, read to the end."""
    result = connection.run(sql, options)
    try:
        columns = result.columns()
        rows = []
        while columns is not None and (batch := result.fetch(500)):
            rows += batch
        result.next_result()
        return [c.name for c in columns or []], rows, [m.text for m in result.take_messages()]
    finally:
        result.close()


def test_connects_and_identifies(connection):
    info = connection.server_info()
    assert info.version.startswith("Couchbase Server 7.6.12-") and info.edition == "enterprise"
    assert "dbaguy" in (info.current_user or "")
    assert (info.current_database, info.default_query_context) == ("dbaguy.app", APP)


def test_a_limited_user_connects():  # A17
    conn = open_couchbase(username=COUCHBASE_READER)
    try:
        assert COUCHBASE_READER in (conn.server_info().current_user or "")
    finally:
        conn.close()


def test_wrong_password_and_unreachable_nodes():
    with pytest.raises(ConnectionFailed, match="Check the user name and password"):
        open_couchbase(password="wrong")
    with pytest.raises(ConnectionFailed, match=r"Could not reach a query node: 127\.0\.0\.1:1 \(refused\)"):
        open_couchbase(port=1)
    conn = open_couchbase(host="cb-nowhere.invalid,127.0.0.1")  # discovery skips the dead seed
    conn.close()


def test_stored_numbers_reach_the_cells_exactly(connection):  # Review Focus 1
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    assert names == ["big", "i64max", "i64min", "u64max", "beyond", "dec", "pi", "sci", "tiny", "huge", "half", OVERFLOW_NAME]
    assert rows == [[J("9007199254740993"), J("9223372036854775807"), J("-9223372036854775808"),
                     J("18446744073709551615"), J("12345678901234567890"), J("1.10"),
                     J("3.141592653589793238462643383279"), J("1E+3"), J("1e-400"), J("1e400"), 0.5, MISSING]]
    assert encode_row(rows[0])[5] == {"$type": "json", "text": "1.10"}
    _, [[raw]], _ = read(connection, 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_numbers"')
    assert raw == J(U_NUMBERS)
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d USE KEYS "u_keyorder"')
    assert names == ["zeta", "alpha", "ws", "mid", OVERFLOW_NAME] and rows[0][1] == J('{"z": 1, "a": 2}')


def test_star_over_mixed_documents(connection):  # Review Focus 4
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d WHERE META(d).id IN ["u1","u2","u3","u4","u5"] '
                                      "ORDER BY META(d).id")
    assert names == ["name", "age", "tags", "address", "title", "nested", "value", "select", "odd key", OVERFLOW_NAME]
    assert rows[1][:4] == ["Bob", None, MISSING, J('{"city":"Tainan"}')]  # null and MISSING apart
    names, rows, _ = read(connection, 'SELECT * FROM dbaguy.app.users AS d WHERE META(d).id IN ["s1","u1"] ORDER BY META(d).id')
    assert names == ["d", OVERFLOW_NAME] and rows[0][0] == "just a string document"  # no expansion: nothing lost


def test_browse_shape_expands_the_alias(connection):  # A1, D-04-11's statement
    sql = "SELECT META(d).id, * FROM events AS d WHERE META(d).id BETWEEN 'e100400' AND 'e101100' ORDER BY META(d).id"
    names, rows, _ = read(connection, sql, RunOptions(query_context=APP))
    assert names == ["id", "amount", "at", "d.id", "kind", OVERFLOW_NAME]  # first page from e100400 (i=400): amount first
    assert len(rows) == 701 and rows[600][0] == "e101000"
    assert rows[600][-1] == J('{"late":{"note":"a key the first page does not show"}}')  # after the first page
    assert rows[6][1] is MISSING  # e100406 (i % 7 == 0) has no amount
```

(If Task 1 recorded A1 as not holding, replace the first `names` assertion with the recorded row shape's columns and add the 04b carry, as the assumptions table says.)

```python
def test_errors_carry_code_and_utf16_position(connection):
    with pytest.raises(StatementError) as error:
        connection.run("SELECT '中文😀' AS x WHER")
    assert (error.value.code, error.value.position.line, error.value.position.column) == ("3000", 1, 20)


def test_an_error_after_rows_keeps_the_rows(connection):  # Review Focus 2
    result = connection.run('SELECT RAW CASE WHEN i < 3000 THEN i ELSE ABORT("stop at 3000") END '
                            "FROM ARRAY_RANGE(0, 10000) AS i")
    rows = []
    with pytest.raises(StatementError, match="stop at 3000"):
        result.columns()
        while batch := result.fetch(500):
            rows += batch
        result.next_result()
    result.close()
    assert rows and rows[0] == [0]
```

(If the server has no `ABORT()` and answers with an unknown-function error before any row, drop this test, record it, and keep the unit tests of Step 1 as the coverage.)

```python
def test_dml_ddl_and_returning(connection):
    try:
        result = connection.run('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_t1", {"a": 1}), ("zz_t2", {"a": 2})')
        assert result.columns() is None and result.affected_rows() == 2
        result.close()
        names, rows, messages = read(connection, 'DELETE FROM dbaguy.app.orders AS o USE KEYS ["zz_t1", "zz_t2"] '
                                                 "RETURNING RAW META(o).id")
        assert sorted(rows) == [["zz_t1"], ["zz_t2"]] and any("2 mutations" in m for m in messages)
        result = connection.run("CREATE INDEX zz_idx IF NOT EXISTS ON dbaguy.app.orders(status)")
        assert result.columns() is None and result.affected_rows() is None
        result.close()
    finally:
        couchbase_query('DELETE FROM dbaguy.app.orders USE KEYS ["zz_t1", "zz_t2"]')
        couchbase_query("DROP INDEX zz_idx IF EXISTS ON dbaguy.app.orders")


def test_parameters_are_typed_and_exact(connection):
    params = StatementParams(named={"n": "9007199254740993", "s": '"中文"'})
    _, rows, _ = read(connection, "SELECT $n + 1 AS n1, @s AS s", RunOptions(params=params))
    assert rows == [[J("9007199254740994"), "中文"]]  # int64 arithmetic on the server: exact (spec §1)
    _, rows, _ = read(connection, "SELECT $1 AS a, $2 AS b", RunOptions(params=StatementParams(positional=["[1,2]", "true"])))
    assert rows == [[J("[1,2]"), True]]


def test_the_query_context_goes_with_each_request(connection):
    assert read(connection, "SELECT RAW COUNT(*) FROM events", RunOptions(query_context=APP))[1] == [[20_000]]
    with pytest.raises(StatementError) as error:
        connection.run("SELECT RAW COUNT(*) FROM events")
    assert error.value.code == "12003"  # keyspace not found: no context, no resolution


def test_transactions_are_refused(connection):
    with pytest.raises(StatementError) as error:
        connection.run("BEGIN WORK")
    assert error.value.code == "transactions_unsupported"
    with pytest.raises(StatementError):
        connection.set_autocommit(False)
    connection.set_autocommit(True)
    assert (connection.in_transaction(), connection.autocommit()) == (False, True)


def test_read_only_profile_is_enforced_by_the_server():  # Review Focus 5
    conn = open_couchbase(read_only=True)
    name = f"zz_p_{uuid.uuid4().hex[:8]}"
    try:
        for sql in ('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_ro", {})',
                    'DELETE FROM system:active_requests WHERE clientContextID = "none"'):
            with pytest.raises(StatementError) as error:
                conn.run(sql)
            assert error.value.code == "1000", sql
        read(conn, f'PREPARE {name} FROM UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_ro", {{}})')
        with pytest.raises(StatementError) as error:
            conn.run(f"EXECUTE {name}")
        assert error.value.code == "1000"
        assert read(conn, "WITH a AS (SELECT 1 AS x) SELECT a")[1]  # A12: a CTE reads
    finally:
        conn.close()
    assert couchbase_query('SELECT RAW 1 FROM dbaguy.app.orders USE KEYS "zz_ro"')["results"] == []
```

Add one more read-only probe for A12 to the same test and record the outcome: `ADVISE SELECT * FROM dbaguy.app.users WHERE name = "x"` (works, or which error).

```python
def test_cancel_a_long_aggregate_within_a_second(connection):  # Review Focus 3, spec §1
    result = connection.run(LONG_AGGREGATE)
    threading.Timer(2.0, connection.cancel).start()
    with pytest.raises(StatementCancelled):
        result.columns()
        result.fetch(1)
    cancelled_at = time.monotonic()
    result.close()
    assert time.monotonic() - cancelled_at < 1.0
    assert completed_request_state(connection.last_client_context_id) == "closed"
    assert read(connection, "SELECT RAW 1")[1] == [[1]]  # the same connection object works on


def test_a_cancel_during_the_start_is_kept(connection, monkeypatch):  # Review Focus 3
    real = couchbase_adapter.build_body

    def cancel_while_building(request):
        connection.cancel()  # arrives after run() began, before the request goes out
        return real(request)

    monkeypatch.setattr(couchbase_adapter, "build_body", cancel_while_building)
    with pytest.raises(StatementCancelled):
        connection.run('UPSERT INTO dbaguy.app.orders (KEY, VALUE) VALUES ("zz_never", {})')
    monkeypatch.undo()
    assert couchbase_query('SELECT RAW 1 FROM dbaguy.app.orders USE KEYS "zz_never"')["results"] == []


def test_an_idle_cancel_never_reaches_the_next_statement(connection):  # Review Focus 3
    connection.cancel()
    assert read(connection, "SELECT RAW 1")[1] == [[1]]


def test_closing_a_partly_read_result_frees_the_server_request(connection):  # Review Focus 3
    result = connection.run("SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 3000) AS a UNNEST ARRAY_RANGE(0, 1000) AS b")
    result.columns()
    assert len(result.fetch(500)) == 500
    time.sleep(1.5)  # long enough for system:completed_requests to keep it
    started = time.monotonic()
    result.close()
    assert time.monotonic() - started < 0.5
    assert completed_request_state(connection.last_client_context_id) == "closed"
    assert read(connection, "SELECT RAW 2")[1] == [[2]]


def test_explain_is_one_plan_cell(connection):  # A15
    result = connection.run("EXPLAIN SELECT * FROM dbaguy.app.users")
    try:
        assert result.presentation() == "plan" and [c.name for c in result.columns()] == ["plan"]
        [[cell]] = result.fetch(10)
        assert isinstance(cell, J) and '"#operator"' in cell.text
    finally:
        result.close()


def test_a_big_document_is_one_truncated_cell(connection):
    _, [[cell]], _ = read(connection, 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS "u_big"')
    assert len(cell.text) > 5 * 1024 * 1024 and encode_row([cell])[0]["truncated"]


def test_streams_100k_rows_within_budget(connection):  # Q1, an absolute budget as R-03a-P1 (ruling P21)
    started = time.monotonic()
    result = connection.run('SELECT i AS n, "x" AS s FROM ARRAY_RANGE(0, 100000) AS i')
    result.columns()
    total = 0
    while batch := result.fetch(500):
        encode_row(batch[0])
        total += len([encode_row(row) for row in batch])
    result.close()
    assert total == 100_000 and time.monotonic() - started < 4.0


def test_tls_with_the_cluster_ca(tmp_path):  # A10, A11
    ca = str(couchbase_ca_file(tmp_path))
    for mode in ("verify-ca", "verify-full", "require"):
        if mode == "verify-full" and not A11_SAN_HAS_LOOPBACK:
            with pytest.raises(ConnectionFailed, match="CA file"):
                open_couchbase(ssl=SslConfig(mode=mode, ca_file=ca))
            continue
        conn = open_couchbase(ssl=SslConfig(mode=mode, ca_file=ca if mode != "require" else None))
        try:
            assert read(conn, "SELECT RAW 1")[1] == [[1]]
            if mode == "verify-ca":  # cancel over TLS (A10)
                result = conn.run(LONG_AGGREGATE)
                threading.Timer(1.0, conn.cancel).start()
                with pytest.raises(StatementCancelled):
                    result.columns()
                    result.fetch(1)
                result.close()
        finally:
            conn.close()
    with pytest.raises(ConnectionFailed, match="CA file"):
        open_couchbase(ssl=SslConfig(mode="verify-ca"))  # the default bundle does not know the cluster CA
```

(Define `A11_SAN_HAS_LOOPBACK = <True or False as Task 1 recorded>` at the top of the file with a comment pointing at the assumptions table.)

```python
@requires_couchbase_bastion
def test_couchbase_through_the_ssh_bastion_including_cancel(tmp_path):
    profiles = ProfileService(ProfileStore(profiles_file(tmp_path)), InMemorySecretStore())
    tunnels = SshTunnelManager(write_askpass_script(tmp_path), extra_options=TEST_SSH_OPTIONS)
    service = ConnectionService(AdapterRegistry([CouchbaseAdapter()]), profiles, tunnels)
    profile = make_couchbase_profile(
        host="couchbase", port=8093, ssh=SshConfig(enabled=True, host="127.0.0.1", port=BASTION_PORT, user="dev", auth="password"),
    )
    lease = service.open(profile, None, ResolvedSecrets(password=COUCHBASE_PASSWORD, ssh_password="dev"))
    try:
        connection = lease.connection
        assert connection.server_info().version.startswith("Couchbase Server 7.6.12")
        result = connection.run(LONG_AGGREGATE)
        threading.Timer(1.0, connection.cancel).start()
        with pytest.raises(StatementCancelled):
            result.columns()
            result.fetch(1)
        result.close()
        assert read(connection, "SELECT RAW 1")[1] == [[1]]
    finally:
        lease.close()
    assert tunnels.active_count() == 0
    with pytest.raises(ValidationFailed, match="one node"):
        service.open(make_couchbase_profile(host="a,b", ssh=profile.ssh), None, ResolvedSecrets(password="x"))


def test_urls_through_the_registry():
    registry = default_registry()
    assert registry.parse_url("couchbases://cb1,cb2/travel-sample").profile.engine == "couchbase"
    with pytest.raises(ValidationFailed) as error:
        registry.parse_url("couchbase://u:secret@h:x/b")
    assert "secret" not in error.value.message
```

`backend/tests/test_couchbase_sessions.py` (worker + real connection; `pytestmark = requires_couchbase`):

```python
import time

import pytest

from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseAdapter
from dbaguy_backend.adapters.couchbase.jsontext import one_line
from dbaguy_backend.domain.execution import ExecuteCommand, ExportCommand, FetchMoreCommand, QueryContext
from dbaguy_backend.services.safety import SafetyPolicy
from dbaguy_backend.sessions.worker import SessionWorker, WorkerHooks
from dbaguy_backend.sql.dialects import Dialect
from tests.fake_db import FakeLease
from tests.support import COUCHBASE_PASSWORD, LONG_AGGREGATE, make_couchbase_profile, requires_couchbase
from tests.test_couchbase_adapter import U_KEYORDER
from tests.test_dev_env import U_NUMBERS
from tests.test_sessions import Recorder, finished

pytestmark = requires_couchbase
APP = QueryContext(bucket="dbaguy", scope="app")


@pytest.fixture
def cb_worker():
    adapter = CouchbaseAdapter()
    lease = FakeLease(adapter.open(make_couchbase_profile(), Endpoint("127.0.0.1", 8093), COUCHBASE_PASSWORD, None))
    worker = SessionWorker("couchbase", lambda: lease)
    worker.start()
    worker.configure(WorkerHooks(
        dialect=Dialect.SQLPP, capabilities=adapter.capabilities,
        check=lambda sql, confirmed: SafetyPolicy().check(sql, Dialect.SQLPP, "standard", False, confirmed),
    ))
    recorder = Recorder()
    worker.set_emitter(recorder)
    yield worker, recorder
    worker.close()


def page_through(worker, rec, rid, count=5000, timeout=60.0):
    """Load more after every pause until `rid` finishes; the number of rows that arrived."""
    deadline, asked = time.monotonic() + timeout, 0
    while not rec.of(rid, "finished"):
        assert time.monotonic() < deadline, rec.types()
        pauses = [e for e in rec.of(rid, "result_set_end") if e.has_more]
        if len(pauses) > asked:
            asked = len(pauses)
            worker.submit(FetchMoreCommand(request_id=rid, count=count))
        time.sleep(0.01)
    return sum(len(e.rows) for e in rec.of(rid, "rows"))


def test_paging_20k_events(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id, * FROM events AS d", query_context=APP,
                                 page_size=5000))
    assert page_through(worker, rec, "e") == 20_000 and not rec.of("e", "error")
    names = [c.name for c in rec.of("e", "result_set")[0].columns]
    assert names[0] == "id" and names[-1] == "(other fields)"


def test_statement_timeout(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="t", sql=LONG_AGGREGATE, timeout_s=0.5))
    done = rec.wait_for(finished("t"), timeout=30)
    assert done.timed_out and done.elapsed_ms < 3000
    worker.submit(ExecuteCommand(request_id="u", sql="SELECT RAW 1"))
    assert not rec.wait_for(finished("u")).cancelled


def test_jsonl_export_is_the_server_text(cb_worker, tmp_path):  # Review Focus 1
    worker, rec = cb_worker
    target = tmp_path / "users.jsonl"
    sql = 'SELECT RAW d FROM dbaguy.app.users AS d USE KEYS ["u_numbers", "u_keyorder"]'
    worker.submit(ExportCommand(request_id="x", sql=sql, format="jsonl", path=str(target)))
    rec.wait_for(finished("x"), timeout=30)
    assert not rec.of("x", "error")
    assert sorted(target.read_text().splitlines()) == sorted([U_NUMBERS, one_line(U_KEYORDER)])


def test_csv_export_writes_nested_values_as_json(cb_worker, tmp_path):
    worker, rec = cb_worker
    target = tmp_path / "u1.csv"
    worker.submit(ExportCommand(request_id="c", sql='SELECT * FROM dbaguy.app.users AS d USE KEYS "u1"', format="csv",
                                path=str(target)))
    rec.wait_for(finished("c"), timeout=30)
    assert target.read_text().splitlines() == [
        "name,age,tags,address,(other fields)", 'Ann,31,"[""a"",""b""]","{""city"":""Taipei"",""zip"":""100""}",',
    ]


def test_begin_work_and_insert_exports_are_refused(cb_worker, tmp_path):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="b", sql="BEGIN WORK"))
    rec.wait_for(finished("b"))
    assert rec.of("b", "error")[0].code == "transactions_unsupported"
    worker.submit(ExportCommand(request_id="i", sql="SELECT RAW 1", format="insert", path=str(tmp_path / "x.sql")))
    rec.wait_for(finished("i"))
    assert rec.of("i", "error")[0].code == "unsupported_format"
```

- [ ] **Step 7: Run everything with the cluster up**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green, no `requires_couchbase` test skipped. Record A12 (ADVISE read-only), A17, A10 (cancel over TLS works) and the 100k-row time in the assumptions table.

- [ ] **Step 8: One-off probes A9 and A13** (record, not committed). `<scratchpad>/a9_a13_probe.py`:

```python
"""A9: does the query service keep an idle keep-alive connection for 35 s? A13: is a request paused for 125 s still
running (not closed by the server)? Run with the dev cluster up; fake credentials only."""
import sys, time
sys.path.insert(0, "backend")
from dbaguy_backend.adapters.base import Endpoint
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseAdapter
from tests.support import COUCHBASE_PASSWORD, couchbase_query, make_couchbase_profile

conn = CouchbaseAdapter().open(make_couchbase_profile(port=8093), Endpoint("127.0.0.1", 8093), COUCHBASE_PASSWORD, None)
result = conn.run("SELECT RAW 1"); result.columns(); result.fetch(10); result.close()
time.sleep(35)
started = time.monotonic()
result = conn.run("SELECT RAW 2"); result.columns(); print("A9 after 35 s:", result.fetch(10), f"{(time.monotonic() - started) * 1000:.0f} ms")
result.close()  # the INFO log says whether the read had to be resent on a new connection
result = conn.run("SELECT RAW i FROM ARRAY_RANGE(0, 200000) AS i"); result.columns(); result.fetch(500)
for _ in range(25):
    time.sleep(5)
state = couchbase_query("SELECT RAW r.state FROM system:active_requests AS r WHERE r.clientContextID = $id",
                        {"$id": conn.last_client_context_id})["results"]
print("A13 state after 125 s paused:", state, "rows after resuming:", len(result.fetch(300000)))
result.close(); conn.close()
```

Run: `cd <repo root> && backend/.venv/bin/python <scratchpad>/a9_a13_probe.py` (it takes about 2.7 minutes). Record both. If A9 shows the server closing idle connections sooner than 30 s, lower `KEEPALIVE_REUSE_S` below that time and record it; reads are resent anyway. If A13 shows the server closing paused requests before 120 s, lower `PAUSED_RESULT_MAX_IDLE_S` below that time and record it.

- [ ] **Step 9: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/result.py backend/dbaguy_backend/adapters/couchbase/adapter.py \
  backend/dbaguy_backend/container.py backend/tests/test_api.py backend/tests/test_couchbase_result.py \
  backend/tests/test_couchbase_adapter.py backend/tests/test_couchbase_sessions.py \
  docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md
git commit -m "feat(couchbase): adapter — discovery and TLS, streamed results with * expansion, socket-shutdown cancel, server read-only, no transactions"
```

---

### Task 12: Frontend minimum: typed cells, copy formats, no transaction controls, the default context, the connection dialog, the tree

**Files:**
- Modify: `src/lib/api/types.ts` (export `QueryContext`)
- Modify: `src/lib/cells.ts`, `src/features/grid/aggregate.ts`, `src/features/grid/ValueViewer.tsx`, `src/features/grid/RecordView.tsx`, `src/features/grid/GridRow.tsx`
- Modify: `src/features/grid/copy.ts`, `src/features/grid/GridMenu.tsx`, `src/features/grid/DataGrid.tsx`, `src/features/grid/ExportDialog.tsx`
- Modify: `src/features/query/QueryTab.tsx`, `src/features/query/QueryToolbar.tsx`, `src/features/query/useTransactionControls.ts`
- Modify: `src/features/session/SessionController.ts`
- Modify: `src/features/connections/ConnectionDialog.tsx`
- Modify: `src/features/explorer/kinds.tsx`, `src/features/explorer/Explorer.tsx`
- Regenerate: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`

**Interfaces:**
- Consumes: the generated types with `Dialect` `sqlpp` (Task 2), `GuardReason` additions (Task 3), `ColumnCategory` `mixed`, `EngineInfo.transactions`, `ServerInfo.default_query_context`, `QueryContext`, `ExecuteCommand.query_context` (Task 4); the registered `couchbase` engine (Task 11); node kinds `bucket`, `scope`, `collection` (Task 10).
- Produces:
  - `cells.ts`: `CellKind` gains `"missing"`; `EncodedCell` gains `text?: string`; `{ $type: "json", text }` is `"number"` when `text` is a JSON number literal, else `"json"` (displayed compact, never re-parsed); `{ $type: "missing" }` is `"missing"` (`MISSING`, muted italic, title "This row has no such field (MISSING, not NULL)"); a string in a `mixed` column is `"text"` / `"empty"`, never guessed as JSON (D-04-2); `plainValue` gives `""` for missing and `text` for json cells.
  - `aggregate.ts`: `missing` is not counted as non-null; `number` and `mixed` columns sum JS numbers and numeric json cells; a `mixed` column never counts a string.
  - `copy.ts`: `copyFormatsFor(dialect: Dialect | null): readonly CopyFormat[]` (no `insert` for `sqlpp`); `GridMenuActions.copyFormats: readonly CopyFormat[]`.
  - `SessionController.execute(sql, options)` with `ExecuteOptions.queryContext?: QueryContext | null` (undefined: the session's default context, `info.server.default_query_context`; null: none) and `exportResult(…)` sending the session's default; 04b's context picker passes the tab's choice through the same option.
  - Query tabs on an engine with `transactions: false` hide auto-commit, Commit, Rollback and the transaction badge, never send `set_autocommit`, and do not wait for an auto-commit mode before Run (spec §3.7.5).

- [ ] **Step 1: Regenerate and export the types.** Run `npm run gen:api`; add `export type QueryContext = S["QueryContext"];` to `src/lib/api/types.ts`.

- [ ] **Step 2: Cells.** In `src/lib/cells.ts`:

```ts
export type CellKind = "null" | "missing" | "empty" | "number" | "bool" | "json" | "bytes" | "truncated" | "text";

/** Shape of the objects the backend sends for cells that do not fit a JSON scalar (see adapters/values.py). */
export interface EncodedCell {
  $type?: string;
  preview?: string;
  size?: number;
  /** `$type: "json"`: the value's JSON text as the server sent it (Couchbase); numbers beyond double stay exact. */
  text?: string;
}

const JSON_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

/** The text of a `$type: "json"` cell, or null for any other value. */
export function jsonCellText(value: unknown): string | null {
  if (value === null || typeof value !== "object") return null;
  const cell = value as EncodedCell;
  return cell.$type === "json" && typeof cell.text === "string" ? cell.text : null;
}

export function isMissing(value: unknown): boolean {
  return value !== null && typeof value === "object" && (value as EncodedCell).$type === "missing";
}
```

`cellKind`: in the object branch, `missing` → `"missing"`, `json` → `JSON_NUMBER.test(text) ? "number" : "json"`, `bytes` / `text` as before, anything else `"json"`; for strings, after the `number` category check, `if (category === "mixed") return "text";` before the JSON guess. `describeCell`: `case "missing": return { text: "MISSING", kind, title: "This row has no such field (MISSING, not NULL)", alignRight: false };`; the `number` case shows `clip(plainValue(value), maxLength)`; the `json` case uses `compactJson(jsonCellText(value) ?? …)` for json cells (the server text; compacted lexically, never re-parsed) and keeps the existing paths for the other values. `plainValue`: `missing` → `""`, `json` → its `text`.

- [ ] **Step 3: Aggregates, viewer, rows.**
  - `aggregate.ts`: skip `isMissing(value)` before counting `nonNull`; columns count when `category === "number" || category === "mixed"`; `numberOf(value, typedOnly)` reads a json cell's `text` like a numeric string, and returns null for plain strings when `typedOnly` (a `mixed` column: a string is text there, D-04-2).
  - `ValueViewer.tsx`: `ValueView` gains `{ kind: "missing" }`; `viewOf` maps `"missing"` to it; json cells reach `parseJson(plainValue(value))` (the lossless tree); `ValueBody` shows `<p className="p-3 font-mono text-[12px] text-muted italic">MISSING — this row has no such field</p>`; `describeSize` gives `` `MISSING · ${column.type_name}` ``.
  - `GridRow.tsx` and `RecordView.tsx`: the muted italic style applies to `cell.kind === "null" || cell.kind === "missing"`.

- [ ] **Step 4: Copy and export formats.** `copy.ts`:

```ts
/** The "Copy as" formats a dialect can produce: SQL++ has no INSERT by columns (it inserts a key and a document). */
export function copyFormatsFor(dialect: Dialect | null): readonly CopyFormat[] {
  return dialect === "sqlpp" ? COPY_FORMATS.filter((format) => format !== "insert") : COPY_FORMATS;
}
```

`GridMenuActions` gains `copyFormats: readonly CopyFormat[]` and the menu maps `a.copyFormats` instead of `COPY_FORMATS`; `DataGrid` passes `copyFormats={copyFormatsFor(dialect)}` to `<GridMenu>`. `ExportDialog`: `const formats = FORMAT_ORDER.filter((kind) => kind !== "insert" || engine?.dialect !== "sqlpp");` renders `formats`, and an effect sets the format back to `"csv"` when the chosen one is not in `formats`. JSON and JSON Lines exports of Couchbase results are the server's row text (Task 6); the dialog needs nothing else for that.

- [ ] **Step 5: No transaction controls where the engine has none.**
  - `QueryTab.tsx`: `const engine = useEngineInfo(tab.profileId); const transactions = engine?.transactions;` (undefined while engines load). `modeApplied = !!snapshot?.state && (transactions === false || snapshot.state.autocommit === tab.autocommit)`. Pass `transactions` to `useTransactionControls` and `showTransactions={transactions !== false}` to `QueryToolbar`. In `useQueryTabActions`: `toggleAutocommit.enabled`, `commit.enabled` and `rollback.enabled` are false when `transactions === false`.
  - `useTransactionControls.ts`: `TransactionControlsArgs.transactions: boolean | undefined`; the auto-commit effect starts with `if (transactions !== true) return;` (never send `set_autocommit` to an engine without transactions, nor before the engine is known), and `transactions` joins its dependency list.
  - `QueryToolbar.tsx`: prop `showTransactions: boolean`; when false, `AutocommitToggle`, Commit, Rollback and `TransactionBadge` are not rendered.

- [ ] **Step 6: The session's default context.** `SessionController.ts`:

```ts
  /** The context a request runs in unless the tab chose one: the profile's default (Couchbase), else none. */
  private defaultQueryContext(): QueryContext | null {
    return this.store.getState().info?.server.default_query_context ?? null;
  }
```

`execute` adds `query_context: options.queryContext !== undefined ? options.queryContext : this.defaultQueryContext()` to the literal it sends and keeps the `profile_plan: false` Task 4 put there (P33: the regenerated type requires the field); `ExecuteOptions` gains `queryContext?: QueryContext | null` with the doc comment from Interfaces; `exportResult` adds `query_context: this.defaultQueryContext()` to its command. Engines other than Couchbase have no default, so their commands carry `null` (the worker refuses a non-null context only where the engine has none).

- [ ] **Step 7: The connection dialog** (`ConnectionDialog.tsx`), as tables keyed by engine like the existing ones:

```ts
URL_EXAMPLES.couchbase = "couchbase://cb1,cb2/bucket · couchbases://host/bucket · http://host:8093";
SSL_MODE_LABELS.couchbase = {
  disable: "disable (HTTP)",
  prefer: "prefer (= disable: HTTP)",
  require: "require (HTTPS, no certificate check)",
  "verify-ca": "verify-ca (HTTPS, check the CA)",
  "verify-full": "verify-full (HTTPS, check the CA and host name)",
};
SSL_MODE_HINTS.couchbase =
  "TLS uses ports 18093 and 18091; verify-ca and verify-full need the cluster's CA file unless its certificate is publicly trusted";
const HOST_HINTS: Partial<Record<Engine, string>> = {
  couchbase: "One host, or several separated by commas (cb1,cb2); only one through an SSH tunnel",
};
const PORT_PLACEHOLDERS: Partial<Record<Engine, string>> = { couchbase: "8093 (18093 with TLS)" };
const DATABASE_LABELS: Partial<Record<Engine, string>> = { couchbase: "Default query context" };
const DATABASE_PLACEHOLDERS: Partial<Record<Engine, string>> = { couchbase: "bucket or bucket.scope" };
const DATABASE_HINTS: Partial<Record<Engine, string>> = {
  couchbase: "A bucket alone whose name holds a dot goes in back-ticks: `my.bucket`",
};
const OPTIONS_HINTS: Partial<Record<Engine, string>> = { couchbase: "manager_port=8091, discover_nodes=false" };
const PLAIN_TEXT_NOTE =
  "The password goes to the cluster in clear text (HTTP Basic auth): use TLS (require or verify-*) for a remote cluster";
```

  - `emptyDraft("couchbase")` uses `{ ...DEFAULT_SSL, mode: "disable" }` (spec §3.6.4: a new Couchbase profile starts with HTTP; `switchEngine` goes through `emptyDraft`).
  - The Host row's `hint` is `HOST_HINTS[draft.engine]`; the port placeholder `PORT_PLACEHOLDERS[draft.engine] ?? String(engineInfo?.default_port ?? "")`; the Database row's label, placeholder and hint come from the three tables (defaults: "Database", none, none); the Options row's hint is `OPTIONS_HINTS[draft.engine] ?? "One key=value per line, passed to the driver"`.
  - The SSL Mode row's hint becomes `PLAIN_TEXT_NOTE` when `draft.engine === "couchbase"`, the mode is `disable` or `prefer`, SSH is off and the host is not loopback (`127.0.0.1`, `localhost`, `::1`, `[::1]`); otherwise `SSL_MODE_HINTS[draft.engine]`.

- [ ] **Step 8: The tree.** `kinds.tsx`: `KIND_ICONS` gains `bucket: Cylinder`, `scope: Boxes`, `collection: Files` (lucide-react names checked while planning); `KIND_LABELS` gains `bucket: "Bucket"`, `scope: "Scope"`, `collection: "Collection"`. `Explorer.tsx`: `CONTAINER_KINDS = new Set(["folder", "database", "schema", "bucket", "scope"])` (double-click expands them). A collection or index opens the existing object tab with its generated DDL (the browse tab of D-04-11 is 04b; ruling P20).

- [ ] **Step 9: Verify the build.**

Run: `npm run typecheck && npm run build`
Expected: both pass.

- [ ] **Step 10: Manual check in the browser.** With the dev containers up, run `scripts/dev.sh --web` (note the PID; stop only it afterwards) and open http://localhost:1420.
  1. **New Connection → Couchbase.** The URL placeholder shows the Couchbase examples; SSL mode starts at `disable (HTTP)`; type host `cb.example.com`: the clear-text note appears; set it back to `127.0.0.1`. Import `couchbase://dbaguy@127.0.0.1/dbaguy.app`: host, database `dbaguy.app`, user, SSL disable. Password `dbaguy`, **untick "Save password in Keychain"**, Test → "Connected to Couchbase Server 7.6.12-…-enterprise", Save; check `dev/data/profiles.json` has `"save_password": false`. Import `couchbases://127.0.0.1/dbaguy.app?certpath=/tmp/x.pem`: SSL `verify-full`, CA file `/tmp/x.pem` (do not save it).
  2. **Tree.** `dbaguy` and `travel-sample`; `dbaguy` → `_default`, `app`, `many` (and `_system` greyed and last, when listed); `many` lists `c001` … `c300`; `app` → users, orders, events, odd-name, value, blobs, noindex; `users` → `#primary` (primary · online) and `idx_users_deferred` (deferred); double-click on a bucket or scope expands it; double-click on `users` opens its detail with `CREATE COLLECTION` and both index DDLs, "generated".
  3. **Query tab.** The toolbar has no auto-commit toggle, Commit, Rollback or badge; the palette's auto-commit, Commit and Rollback entries are disabled. Run `SELECT META(d).id, * FROM events AS d` (the default context `dbaguy.app` goes with it): columns `id, at, d.id, kind, amount, (other fields)` (first-appearance order: `e100000` has no `amount`) — `amount` shows a grey `MISSING` on `e100000`; Load more pages through to 20,000; `e101000`'s `(other fields)` holds `{"late":…}`. Run `SELECT * FROM dbaguy.app.users AS d USE KEYS "u_numbers"`: every number exactly as stored (`9007199254740993`, `18446744073709551615`, `1.10`, `1E+3`, `1e400`); the value viewer of `pi` shows the long literal; selecting the number cells shows a sum flagged approximate; Copy as JSON keeps `1e400`; Copy as IN list of a column gives `[…]`; the Copy menu and the Export dialog offer no SQL INSERT; export the u_numbers row as JSON Lines and compare it with the stored text.
  4. **Errors and cancel.** `SELECT '中文😀' AS x WHER` marks the error under `WHER`. Run the long aggregate `SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 100000) AS b`, press Cancel: it stops within a second and `SELECT RAW 1` runs right after. Set the tab timeout to 2 s and run it again: "Timed out after 2 s". `BEGIN WORK` → the transactions-unsupported error.
  5. **Formatter.** ⌘⇧F on `select meta(d).id from users d where d.name = 'it''s'` (SQL++) leaves the text and shows the warning toast; on `select meta(d).id, d.type from users d` it formats, keywords upper case, `d.type` unchanged. ⌘⇧F on a PostgreSQL tab still formats as before.
  6. **Read-only copy of the profile.** `UPSERT INTO users (KEY, VALUE) VALUES ("k", {})` and `EXECUTE p` are blocked with the read-only reason; a `SELECT` runs.
  7. **Paused result.** Run `SELECT META(d).id, * FROM events AS d`, leave it paused for 2¼ minutes: the warning "Closed this result after 2 min…" appears in Messages, the loaded rows stay, the status shows partial, Load more is gone.

- [ ] **Step 11: Commit**

```bash
git add src
git commit -m "feat(app): Couchbase in the UI — MISSING and JSON cells, mixed columns, no SQL++ INSERT copy, no transaction controls, default query context, connection dialog, keyspace tree"
```

---

### Task 13: Packaging check, measurements, CLAUDE.md and the user's test list

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/research/2026-10-15-feature-priorities.md` (one note in §7 "Couchbase", spec §13)
- Modify (only if Step 2 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/ws_probe.py`, `<scratchpad>/rss_probe.py`

**Interfaces:**
- Consumes: everything above.
- Produces: an app bundle whose frozen sidecar connects to Couchbase over HTTP and TLS, lists the tree, classifies SQL++ without sqlglot, and streams a query; measurements recorded in CLAUDE.md.

- [ ] **Step 1: Build the sidecar.** Run: `npm run build:backend`. Expected: the build passes. No hidden import is needed: the adapter uses only stdlib modules that `collect_submodules("dbaguy_backend")` pulls in through their imports, and SQL++ has no sqlglot dialect. Note the size of `src-tauri/resources/backend` (`du -sh`) against the previous build: the growth should be ≈ 0 (spec §7).

- [ ] **Step 2: Probe the frozen sidecar against the dev cluster.** Shell 1 (note the PID; stop it with Ctrl-C or `kill <PID>`):

```bash
cd src-tauri/resources/backend
DBAGUY_TOKEN=t DBAGUY_PORT=0 DBAGUY_LOG_TO_FILE=false DBAGUY_DATA_DIR="$(mktemp -d)" ./dbaguy-backend   # prints {"event":"ready","port":N}
```

Shell 2 (`C` below stands for `curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json"`; write the CA first with `backend/.venv/bin/python -c 'from pathlib import Path; from tests.support import couchbase_ca_file; print(couchbase_ca_file(Path("/tmp")))'` run from `backend/`):
1. `C -X POST http://127.0.0.1:N/profiles/test -d '{"profile":{"engine":"couchbase","name":"c","host":"127.0.0.1","database":"dbaguy.app","username":"dbaguy","ssl":{"mode":"disable"}},"secrets":{"password":"dbaguy"}}'` → `"ok":true`, "Connected to Couchbase Server 7.6.12-…-enterprise".
2. The same with `"ssl":{"mode":"verify-ca","ca_file":"/tmp/couchbase-ca.pem"}` → ok (TLS through the bundled `ssl`).
3. `C -X POST http://127.0.0.1:N/profiles -d '{"profile":{"engine":"couchbase","name":"c","host":"127.0.0.1","database":"dbaguy.app","username":"dbaguy","save_password":false,"ssl":{"mode":"disable"}},"secrets":{"password":"dbaguy"}}'` → note `profile.id` as P.
4. `C -X POST http://127.0.0.1:N/sql/analyze -d '{"profile_id":"P","statements":["DELETE FROM users","EXECUTE p","SELECT NEXT VALUE FOR s"]}'` → `delete_without_where`; `unknown`; `write`.
5. `C -X POST http://127.0.0.1:N/metadata/children -d '{"profile_id":"P","path":[{"kind":"bucket","name":"dbaguy"},{"kind":"scope","name":"many"}]}'` → 300 collections.
6. `C -X POST http://127.0.0.1:N/sessions -d '{"profile_id":"P"}'` → note `session_id` as S and check `server.default_query_context` is `{"bucket":"dbaguy","scope":"app"}`. Save as `<scratchpad>/ws_probe.py` and run `backend/.venv/bin/python <scratchpad>/ws_probe.py N t S`:

```python
import json
import sys
import time

from websockets.sync.client import connect

port, token, session = sys.argv[1:4]
with connect(f"ws://127.0.0.1:{port}/sessions/{session}/ws?token={token}") as ws:
    started = time.monotonic()
    ws.send(json.dumps({"type": "execute", "request_id": "q", "sql": "SELECT META(d).id, * FROM events AS d",
                        "query_context": {"bucket": "dbaguy", "scope": "app"}, "page_size": 500}))
    while True:
        event = json.loads(ws.recv())
        if event["type"] == "result_set":
            print("columns", [c["name"] for c in event["columns"]])
        if event["type"] == "rows":
            print(f"first rows after {(time.monotonic() - started) * 1000:.0f} ms:", event["rows"][0])
        if event["type"] == "result_set_end":
            ws.send(json.dumps({"type": "discard", "request_id": "q"}))
        if event["type"] == "finished":
            break
```

Expected: `columns ['id', 'at', 'd.id', 'kind', 'amount', '(other fields)']`, the first rows within 100 ms on this Mac (spec §1), then `finished`. If the frozen sidecar fails to import a module, add it to `hiddenimports` in `backend/dbaguy-backend.spec`, rebuild and repeat.

- [ ] **Step 3: Memory while paging a million rows** (spec §7, Q1). With the frozen sidecar of Step 2 still running (PID from Step 2), save `<scratchpad>/rss_probe.py` and run `backend/.venv/bin/python <scratchpad>/rss_probe.py N t S <PID>`:

```python
import json
import subprocess
import sys
import time

from websockets.sync.client import connect

port, token, session, pid = sys.argv[1:5]


def rss_mb() -> float:
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", pid]).decode()) / 1024


sql = 'SELECT i, REPEAT("x", 200) AS pad FROM ARRAY_RANGE(0, 1000000) AS i'
before, peak, rows, paused = rss_mb(), 0.0, 0, False
with connect(f"ws://127.0.0.1:{port}/sessions/{session}/ws?token={token}", max_size=None) as ws:
    ws.send(json.dumps({"type": "execute", "request_id": "m", "sql": sql, "page_size": 10000}))
    while True:
        event = json.loads(ws.recv())
        if event["type"] == "rows":
            rows += len(event["rows"])
        if event["type"] == "result_set_end" and event["has_more"]:
            peak = max(peak, rss_mb())
            if rows >= 500_000 and not paused:
                paused = True
                time.sleep(10)
                peak = max(peak, rss_mb())
            ws.send(json.dumps({"type": "fetch_more", "request_id": "m", "count": 10000}))
        if event["type"] == "finished":
            break
print(f"rows {rows:,}; RSS before {before:.0f} MB, peak {peak:.0f} MB, growth {peak - before:.0f} MB")
```

Expected: 1,000,000 rows; growth < 30 MB. Record the numbers.

- [ ] **Step 4: Build the app, check signatures, re-measure startup.** Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`. Then `scripts/measure-startup.sh` (it uses a temporary data dir; note `stat -f %m ~/Library/Application\ Support/DbaGuy` before and check it is unchanged after). Cold start and memory should match the numbers in CLAUDE.md within noise; record them.

- [ ] **Step 5: Update `CLAUDE.md`** (keep its language and style; spec §13, 04a's part):
  - **技術選型 → Drivers:** `couchbase 4.x（Couchbase）` becomes `Couchbase：Query Service REST（stdlib http.client，無 SDK）`.
  - **Repo 結構:** `adapters/couchbase/`：`settings`（URL、seed、TLS、options）／`nodes`（nodeServices 探索）／`http`（keep-alive 連線、任一執行緒 shutdown）／`stream`（增量解析）／`rows`（signature、`*` 與 alias 展開、溢出欄）／`messages`／`jsontext`／`result`／`adapter`／`catalog`；`sql/format_guard.py`（formatter 的 token 守衛，所有方言）；`sessions/options.py`（每次請求的選項與能力檢查）.
  - **開發指令 → 測試 DB:** Couchbase Enterprise 7.6.12：`127.0.0.1` 的 8091／8093／18091／18093（同號，nodeServices 回報自己的 port），admin `dbaguy`/`dbaguy`、受限使用者 `dbaguy_reader`/`dbaguy`；seed 在 `couchbase-init`（第一次約 1–2 分鐘，記 Task 1 量到的時間），約 1.2 GiB；bastion 可連 `couchbase:8093`；TLS 的 CA 不入庫，測試從 `/pools/default/trustedCAs` 取.
  - **架構重點:** a Couchbase bullet — REST＋增量解析（不經 SDK：不失真、不 abort process、signature 先到）；每個 session 一條 keep-alive HTTP 連線，閒置 < 30 秒才重用，READ 遇到被關掉的連線重送一次；取消＝從任一執行緒 shutdown 連線時保留的 socket 副本，server 標成 `closed`；不送 server `timeout`，tab timeout 由 worker 計時；暫停的結果 120 秒沒有 Load more 由 housekeeping 關閉（120–135 秒）；欄位：signature 順序、`*` 依首頁（500 列／8 MiB）展開、alias 展開、之後才出現的 key 進 `(other fields)`；MISSING 與 JSON cell（`JsonText`、`MISSING`、`ColumnCategory.MIXED`）；JSON／JSONL 匯出是 server 原文；每次請求帶 query context（protocol 的 None＝沒有 context，前端預設送 profile 的）；參數 literal 原文進 body；唯讀＝每個請求 `readonly=true`＋分類器；沒有交易（`transactions=false`）；SQL++ 只用關鍵字分類（`KEYWORD_ONLY_DIALECTS`）.
  - **已知限制:** 沒有 `*` 的結果在首頁之後出現的 key 只有警告（JSON 匯出看得到）；`*` 結果多一欄溢出欄、第一批列要等首頁讀完；CSV 裡 NULL 與 MISSING 都是空字串；只信任 OpenSSL 預設 CA bundle（不讀 macOS keychain），verify-* 通常要給 cluster CA；prefer＝disable（明文 HTTP）；多節點探索、`alternateAddresses`、Reconnect 換 node 只在單節點 dev 驗過；server 自己的 timeout（1080）原文顯示；EXPLAIN／ADVISE 在 04a 以一欄 JSON 顯示（plan 檢視是 04b）；the A-results that matter (A6 CR, A7 spaces, A11 verify-full on 127.0.0.1, A13, A9) as recorded.
  - **Measurements:** Step 3's RSS growth, Step 2's first-page time, Task 10's tree time, Step 4's startup and memory, the sidecar size.
  - **進度 ④:** `進行中：04a 完成（SQL++ 方言、HTTP 引擎、dev cluster）；04b、04c 待做`. The controller adds the follow-up note link after the final review.
- [ ] **Step 6: Note the outdated advice** in `docs/research/2026-10-15-feature-priorities.md` §7 "Couchbase": one line under the heading — `> 2026-10-18：已由 ④ 的設計取代（不用 Python SDK，改用 Query Service REST；見 docs/superpowers/specs/2026-10-18-04-couchbase-design.md）。` — without rewriting the research text (spec §13).

- [ ] **Step 7: Report the user's test list** for 04a in the task handoff (spec §13; what the dev cluster already showed next to each):
  1. The company cluster's version and edition in Test (dev: 7.6.12 EE).
  2. URL import of the real connection string; discovery of every query node (dev: one node) and a Reconnect after a node goes away (dev: not possible).
  3. TLS with the company CA, verify-full against the real host names (dev: A11 result).
  4. A limited user: the tree shows only its keyspaces, a sequential-scan query shows 13014 with the missing role (dev: `dbaguy_reader`).
  5. Cancel a long query and close a paused result: the DBA's view (`system:active_requests` / `completed_requests`) shows `closed`.
  6. A paused result closes after 2 minutes with the message.
  7. A real document collection with odd shapes: `SELECT *` columns, MISSING, the overflow column, exact numbers.
  8. Packaged-app checks: a Couchbase profile saved in the packaged app lands in Keychain service `app.dbaguy`; Cancel in the packaged app.

- [ ] **Step 8: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green.

```bash
git add CLAUDE.md docs/research/2026-10-15-feature-priorities.md
git commit -m "docs: Couchbase engine in CLAUDE.md (REST engine, dev cluster, limits, measurements); 04a done"
```

(`backend/dbaguy-backend.spec` too, only if Step 2 changed it.)

---

## Carries to 04b and 04c

| Carry | Where |
|---|---|
| 04b: a new tab's context = `SessionInfo.server.default_query_context`; the context picker passes `ExecuteOptions.queryContext` (null = "No context"); a History entry's `database` is read back with `QueryContext.parse` | P1, P2; `SessionController.execute` already takes the option (Task 12) |
| 04b: `RelationKind` gains `"collection"`, `CouchbaseAdapter.relation_path()`, ⌘P over `snapshot.keyspaces`, the browse tab of D-04-11 (its statement depends on A1) | P20, P23 |
| 04b: the parameters panel reads `StatementRange.params` (Task 2) and sends `StatementParams` (Tasks 4, 6, 7 already carry it to the body); the "> int64 / inexact decimal" WARNING | P24 |
| 04b: `executed_plan=True` once `profile=timings` is parsed into an extra result set through `next_result()` (A16); the frontend keeps `ResultSetEvent.presentation` and shows `plan` results in a plan view | P6 |
| 04b: SQL++ completion dispatch (`sql/sqlpp_completion.py`), `/sql/describe` → None for SQLPP, sampling | spec §3.12 |
| 04c: `documents=True`; `jsontext` gains token comparison and the save diff; `QueryRequest.preserve_expiry` is already in the body builder; `MetadataService.with_connection(retry=False)`; `@codemirror/lang-json` | P6; spec §3.9 |

## Planning rulings

### Assumptions recorded during execution

Owner tasks fill in the Result column (a few words) and apply the Effect; nothing else in the plan needs editing for the outcomes listed. Results so far: Task 1 (A1, A6, A7, A10, A11, A15, A20, A21, A22; 7.6.12 EE dev cluster, probes in Task 1 Step 9); Task 2 (A4, A5; `tests/test_couchbase_literals.py` against the same cluster); Task 5 (A19, same file); Task 11 (A9, A10 through the adapter, A12, A13, A17; `tests/test_couchbase_adapter.py`, `tests/test_couchbase_sessions.py` and the scratch probes of Task 11 Step 8).

| # | Assumption (spec §10) | Owner | Result | Effect on later tasks |
|---|---|---|---|---|
| A1 | `SELECT META(d).id, * FROM ks AS d` rows are `{"id":…,"d":{doc}}` | Task 1 | Holds: rows `{"id":"e100000","d":{…}}`, signature `{"id":"json","*":"*"}` | Holds: Task 11 `test_browse_shape_expands_the_alias` as written. Not: its expected columns follow the recorded shape (still lossless, D-04-1 falls back) and 04b's browse statement becomes `SELECT META(d).id, d FROM …` (add to Carries). |
| A4 | SQL++ string escapes equal JSON's (`\b`, `\f`, `\u0000`, `/`, emoji) | Task 2 | Holds: `json.dumps(…, ensure_ascii=False)` round-trips every value of the test (`it's`, `say "hi"`, `back\slash`, tab/LF/CR, `\b\f`, `\x00\x1f`, `/`, `中文😀`, empty) and, probed beside it, DEL, U+2028/2029, BOM/NBSP/ZWSP inside a string, `$x`, `?`, `--`, `/* */` | Holds: nothing. `\b` or `\f` refused: `quote_literal` writes `\u0008` / `\u000c` (Task 2 Step 3). |
| A5 | `\\` in back-ticks is one backslash | Task 2 | Holds: `force_quote_ident(SQLPP, n)` reads back as the field `n` for `a\b`, ``a`b``, `x.y`, `value`, `中文` (probed beside them: `\``, ` `` `, `\\`, `a b`, `1x`, `a-b`, `é`) | Holds: nothing. Not: `force_quote_ident(SQLPP)` stops doubling backslashes (the lexer flag stays). |
| A6 | `--` ends at CR | Task 1 | Holds: `SELECT 1 AS a -- c` CR `, 2 AS b` → `{"a":1,"b":2}` | No code change either way (`[\n\r]` is the safe side); CLAUDE.md notes it. |
| A7 | The server reads U+00A0, U+2003, U+3000, U+200B, U+FEFF as spaces | Task 1 | Refused, all five: 3000 syntax error (`at: \xa0` …), also as a leading BOM → `unicode_spaces=False`, the second test variant. Also seen: VT (U+000B) refused; TAB and FF accepted | NBSP accepted as a space: `unicode_spaces=True` for SQLPP and the first lexer test variant; refused: False and the second. |
| A8 | `nodeServices` entry without `hostname` = the asked node | Task 9 | Holds for one node (7.6.12 EE): the single `nodesExt` entry has no `hostname` and carries `thisNode: true` (same answer for `dbaguy` and the limited `dbaguy_reader`, anonymous 401); `candidate_nodes` returns `[127.0.0.1:8093]` (`18093` with TLS). Multi-node maps and `alternateAddresses.external` are parsed from fixtures only (also: `external.ports` lists only the ports that differ, so the internal ones fill in) | Multi-node and external addresses go to the user's test list (Task 13 Step 7). |
| A9 | Idle keep-alive connections survive ≥ 30 s | Task 11 Step 8 | Holds: after 29 s idle the kept-alive connection is reused (no new connection, no resend, 12 ms); with reuse forced past the window (`KEEPALIVE_REUSE_S` patched) the server still answered on the same connection after 35 s and 65 s idle, no resend (Task 7: ≥ 200 s). `KEEPALIVE_REUSE_S` stays 30 | Closed sooner: lower `KEEPALIVE_REUSE_S` below the observed time (reads are resent anyway). |
| A10 | Shutting down the TCP duplicate ends a blocked TLS read at once | Task 1, Task 11 | Holds: over TLS the blocked reader ends 0–1 ms after the duplicate's shutdown with `BrokenPipeError`, waiting for the status line or mid-body, socket timeout 20 s or None; server state `closed` (plain HTTP: `RemoteDisconnected` / `IncompleteRead`). The server sends no status line until its first output is ready, so cancelling a long aggregate interrupts `getresponse()`. Task 7: confirmed through `NodeClient` (plain and TLS: `ChannelError` at once, no resend, server state `closed`); no P41 fallback added. Task 11: through the adapter, `run(LONG_AGGREGATE)` blocked on the status line ends in `StatementCancelled` 0.2–3.9 ms after a cancel over TLS verify-ca (plain 0.3–10 ms), server state `closed`, the same connection works on; also through the SSH bastion | Holds: as planned (`SSLSocket.shutdown()` is never called). Not: `HttpChannel.shutdown()` also shuts down the TLS socket and Task 11 counts any exception under the cancel flag as a cancel (spec §9 fallback, ruling P41; Task 7 Step 8, Task 11 Step 3). |
| A11 | The dev 18093 certificate holds 127.0.0.1 (verify-full) | Task 1 | Holds: verify-full against 127.0.0.1 ok; SAN = `IP Address: 127.0.0.1` only → `A11_SAN_HAS_LOOPBACK = True` | Sets `A11_SAN_HAS_LOOPBACK` in Task 11's TLS test; CLAUDE.md notes a False. |
| A12 | `readonly=true` with CTE, EXPLAIN FUNCTION, ADVISE | Task 11 | Holds for what was probed: a CTE (`WITH a AS (SELECT 1 AS x) SELECT a`) reads; `ADVISE SELECT * FROM dbaguy.app.orders WHERE status = "x"` answers (signature `"json"`, `IndexAdvice`); writes are refused with 1000 "The server or request is read-only…" (HTTP 403): UPSERT, `DELETE FROM system:active_requests`, `EXECUTE` of a prepared UPSERT (the PREPARE itself is accepted). EXPLAIN FUNCTION not probed. **Exception (M1): `UPDATE STATISTICS` (and the `ANALYZE` spellings) runs under `readonly=true` on 7.6.12** and writes optimizer statistics into `_system._query`. Everything else probed is refused with 1000: DML, index / scope / collection / function / sequence DDL, `EXECUTE FUNCTION` (also a JS function that writes), `NEXT VALUE FOR`, EXECUTE of a prepared write, DELETE on `system:` keyspaces, GRANT / REVOKE, `FLUSH COLLECTION` | Record only (CTE is asserted; ADVISE recorded; EXPLAIN FUNCTION needs a function: not probed in 04a). The classifier is the only wall for `UPDATE STATISTICS` on a read-only profile and it blocks it (`test_safety.py`); CLAUDE.md 已知限制 and the user's test list (item 9) say so. |
| A13 | A paused request is not closed by the server within 120 s | Task 11 Step 8 | **Does not hold.** A request whose client stops reading leaves `system:active_requests` 77–86 s after the pause (4 runs: 77.3, 77.7, 85.0, 86 s; paused after 500 or 300,000 rows): `completed_requests` state `fatal`, error 1203 `service.slow_client` "Slow/stalled client write timed out"; a later Load more gets what the buffers hold (4.7 M rows of a 100 M-row stream) and then a cut body (`connection_lost`, IncompleteRead). A result that fits the buffers (200,000 RAW ints, 1 MB) completes on the server at once and is not affected. `PAUSED_RESULT_MAX_IDLE_S` lowered to **45** (closed after 45–60 s with the housekeeping pass, ≥ 17 s before the earliest kill seen); `paused_result_closed_text` now says "45 s" (whole minutes stay "N min"). Fix round (R-04a-T11b): a 500-row Load more is served from 17–37 MB of buffers and does not unstall the server, so the limit counts from the result's first pause and a Load more does not restart it; the text says "Closed this result 45 s after it first paused…" | Closed sooner: lower `PAUSED_RESULT_MAX_IDLE_S` below the observed time. |
| A14 | Query service error answers (4xx/5xx) are JSON | Task 7 | Holds (7.6.12 EE): a wrong password answers `401 Unauthorized` with a JSON body (`errors[0].code` 2120 "Failure to authenticate user"), a syntax error `400 Bad Request` with JSON (3000, `line`/`column`); Content-Type `application/json; version=7.6.12-N1QL`. Small answers come with `Content-Length`, larger ones chunked: `read1()` handles both | A non-JSON 401: the test keeps the status assertion only; Task 11's `http_<status>` path covers such bodies. |
| A15 | EXPLAIN / ADVISE signature shape | Task 1 | EXPLAIN `"json"`; ADVISE `"json"` (`app.orders`, `app.events`, `app.blobs`, travel-sample). ADVISE on `app.users` fails, any predicate: 5001 "Panic: interface conversion: interface {} is []uint8, not map[string]interface {}" (no signature; trigger not isolated, copies of its documents elsewhere do not reproduce it) | Record only: the plan shape ignores the signature (Task 8 tests both). |
| A17 | A limited user can call `CURRENT_USERS()` | Task 11 | Holds: `dbaguy_reader` gets `["local:dbaguy_reader"]` (the admin `["builtin:dbaguy"]`), shown as is in `ServerInfo.current_user`. The version-only fallback stays (unit-tested with a fake service) | Not: the version-only probe and the profile's user name (already coded). |
| A18 | `system:indexes` is filtered by privileges | Task 10 | Holds (7.6.12 EE): `dbaguy_reader` (`query_select` + `data_reader` on `dbaguy.app.users`) gets zero rows from `system:indexes`, for its own collection too (`indexes` of `users`, the `_default` ones and the whole catalog all empty), while the same user sees its bucket, scope and collection in `system:buckets`, `system:scopes` and `system:keyspaces` (the admin sees 28 indexes). Listing indexes is a privilege `query_select` and `data_reader` do not give | Holds: the reader's test expects the empty set (done); a user without the privilege sees a collection with no index and its collection DDL has no index part. Not: the `<=` set check. |
| A19 | `x IN [ … ]` is how SQL++ reads an IN list | Task 5 | Holds: `WHERE x IN [1, "a", {"k": 1}, true]` over `[1, "a", {"k": 1}, true, 2, "b"]` returns exactly those four (`test_in_list_is_an_array_the_server_reads`); also seen on 7.6.12: `[1, "a", TRUE, NULL, MISSING, {"k":1}]` parses (uppercase `TRUE`/`NULL`/`MISSING`; inside an array MISSING reads as null); an object element matches by value, `{"k":1}` also matches `{"k":1.0}`; `IN []` returns nothing; NULL/MISSING elements never match a null row (three-valued logic) | Holds: nothing. Not: `_IN_LIST_BRACKETS[SQLPP]` back to `(`, `)` and the test follows. |
| A20 | `cbc create` writes binary/text documents into `app.blobs` | Task 1 | Holds with the flags in decimal: `cbc create -f` refuses `0x…` ("Trailing garbage"), so `blob()` passes `$((FLAGS))` (50331650, 67108868); `bin1`, `txt1` → `base64`; `cbc` reaches KV through the cluster map | Not: flags 0 (Task 1 Step 8); `cbc` cannot write there at all: `bin1` / `txt1` are not seeded and the case is carried to 04c (Task 1 Steps 5 and 8, P40). |
| A21 | `UPSERT … (KEY, VALUE, OPTIONS)` with an absolute expiration | Task 1 | Holds: `META().expiration` = 4102444800 | Not: `u_ttl` is not seeded, its statement and assertion are removed and the case is carried to 04c; no `cbc` fallback (Task 1 Step 8, P40). |
| A22 | `system:keyspaces` rows carry `bucket`, `scope` and `name` (the namespace is in both `namespace` and `namespace_id`; every row is in `default`, so no query filters on it); `system:indexes` rows carry `bucket_id`, `scope_id`, `keyspace_id`, `name`, `state`, `is_primary`, `index_key`, `condition`, `partition`, `metadata` | Task 1 | Holds (Tasks 1, 10 as written). Keyspace rows: `bucket`, `scope`, `name`, `id`, `path`, `datastore_id`, and both `namespace` and `namespace_id` (`default`); bucket rows lack `bucket` / `scope`. Index rows: `bucket_id`, `scope_id`, `keyspace_id`, `name`, `state`, `index_key`, `metadata`, `using`, `id`; `is_primary` only on primary indexes, `condition` / `partition` only when set; `_default._default` indexes: `keyspace_id` = bucket, no `bucket_id`. `_system` (manifest: `_query`, `_mobile`) is in neither catalog nor `system:scopes` | Holds: Tasks 1 and 10 as written. A name differs: the seed's two wait queries (Task 1 Step 5) and Task 10's `_COLLECTIONS`, `_BUCKET_KEYSPACE`, `_KEYSPACES`, `_INDEXES`, `_DEFAULT_INDEXES` and `index_ddl` follow the recorded names (P35). |

A2, A3 (04c) and A16 (04b) are not 04a's.

### Rulings made while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| P1 | `query_context: None` in the protocol and in `RunOptions` means **no** context. The profile's default reaches the client as `ServerInfo.default_query_context`, and the client sends it unless the tab chose otherwise (04a: always the default). | The spec wants both "the profile's database is the default context" and a "No context" choice (§3.11.1); one `None` cannot mean both. Keeps the adapter dumb and 04b's picker a frontend change. | None: 04b only changes where the client takes the value from. |
| P2 | Context text for display, history and the profile field is `QueryContext.text()` / `parse()`: back-ticks only where a dot or back-tick would be misread (`travel-sample.inventory`, `` `my.bucket` ``). The request uses `default:` + `keyspace_path` (`` `travel-sample`.inventory ``). | §1 shows `travel-sample.inventory` while §3.2.3 says `qualified()`; the history text must parse back (§3.11.1's last-dot rule). | None. |
| P3 | Cancel shuts down a duplicate of the TCP socket taken at connect time and kept until close, not a `socket.fromfd()` copy made at cancel time. | http.client may close its socket on its own; a descriptor number closed and reused meanwhile would let a late cancel shut down an unrelated connection. | One extra descriptor per connection. |
| P4 | An error that follows the rows is kept and raised by the next `fetch()` without rows, `next_result()` or `affected_rows()`; rows read before a failure always go out first. | The worker stops calling `fetch()` after a short page and calls `next_result()`; raising only from `fetch()` (spec §3.5.4) would lose such an error. | None. |
| P5 | A result without `*` is never peeked and has no overflow column; keys outside its signature (or non-object rows) raise one WARNING naming the first row, and JSON / JSONL exports keep everything. | §3.5.1 says both "no peek without `*`" and "first-page extra keys → overflow column"; the observed shapes never have such keys. | Such keys are visible only in JSON exports. |
| P6 | 04a's Couchbase capabilities: `documents=False` (04c turns it on), `executed_plan=False` (04b, with A16), `plan_format="couchbase"` now (the adapter marks EXPLAIN/ADVISE results `plan`). | A capability must not promise what is not built yet; the worker would accept `profile_plan` and nothing would come back. | None. |
| P7 | The snapshot is built from one `system:keyspaces` query; buckets follow from its rows. | A bucket without a visible keyspace cannot be queried; §3.8's "buckets ＋ keyspaces" needs no second query for the snapshot's one field. | None. |
| P8 | `u_ttl` expires at 2100-01-01 (4102444800), not "in one year". | A relative TTL above 30 days is read as a Unix time (1971: gone at once), and the long-lived dev cluster would lose a short one. | None. |
| P9 | The `_default` scope and the `_default._default` collection show a comment instead of a CREATE statement. | Neither can be created or dropped. | None. |
| P10 | Pydantic checks only the parameters' shape (≤ 200 each); names, JSON validity (`parse_int=str`: long integers are valid), ≤ 1 MiB and valid text are checked by the worker (`invalid_params`, naming `$name` / `$n`, never the value). | §3.2.2 wants the worker's own code and message; model constraints would answer `invalid_command` with pydantic's text. | None. |
| P11 | `StatementSpan.params` lists named parameters once each (`@x` is `$x`) in first-use order, and every `?`. | The panel shows one field per name; each `?` takes the next positional argument. | None. |
| P12 | The `*` peek's 8 MiB counts characters of row text. | The parser holds text; bytes would need re-encoding. | A CJK-heavy first page may hold up to ~3× 8 MiB of UTF-8. |
| P13 | The format guard compares WORDs case-insensitively everywhere except SQL++, where only reserved words and words right before `(` (function calls) are case-free; comments compare without whitespace; an extra or missing token is a difference. | SQL++ names are case-sensitive, function names are not (`meta` → `META`). | A SQL++ identifier written right before `(` (`INSERT INTO ks (KEY, VALUE)`) could change case unnoticed — `sql-formatter` does not touch identifiers. |
| P14 | SQL++ classification: leading `(` skipped; a MERGE holding DELETE → `merge_delete`; `system_keyspace` for a write naming `system:`; `BEGIN` alone, `EXECUTE`, other `SET` → UNKNOWN; DML words or `NEXT VALUE FOR` in SELECT/WITH → WRITE. | Spec §3.3.4 / survey §8.2, with the fail-closed choices made explicit. | `BEGIN` alone is blocked read-only (the server rejects it anyway). |
| P15 | Discovery runs when the port is unset or equals the TLS mode's default query port, without SSH, and unless `discover_nodes=false`. | D-04-6: a port of one's own (LB, proxy) or SSH means "this endpoint". | None. |
| P16 | A9 and A13 are one-off probes (35 s and 125 s waits) recorded in this plan, not suite tests. | They are facts about the server, not about DbaGuy's code; the suite stays fast. | A server upgrade could change them unnoticed (CLAUDE.md records the values). |
| P17 | Opening: refused, timed out or unknown nodes → the next candidate; authentication (`10000`, `13014`, `http_401`) and TLS failures stop at once with a hint. | A wrong password or CA is not a dead node; trying every node would only repeat it. | None. |
| P18 | One `HttpChannel` per TCP connection; `NodeClient` replaces a dead or old one; only a READ (`retry_safe`) is resent, once, and only when no byte of the answer had arrived. | D-04-14. | None. |
| P19 | The frontend hides INSERT copy/export for `sqlpp`, hides transaction controls when `transactions` is false, and does not wait for an auto-commit mode there. | Spec §3.13; `modeApplied` would otherwise block Run if a tab's stored auto-commit were off. | None. |
| P20 | In 04a a collection's double-click opens its detail tab (DDL with indexes); the browse tab (D-04-11) is 04b. | Context setting and browse statements are 04b's (§11). | None. |
| P21 | Throughput budget for Couchbase: 100k rows in 500-row pages, `encode_row` included, ≤ 4.0 s (absolute, as R-03a-P1). | The survey measured 80k rows/s with per-field decoding over real documents; these rows are small. | A slower machine may need a looser bound. |
| P22 | Between Tasks 2 and 3 SQL++ goes through the generic keyword rules; Task 3 replaces them before anything ships. | Task 2 must add every table at once (tripwires); the classifier is its own reviewable unit. | None. |
| P23 | `DatabaseAdapter.relation_path()` for Couchbase and `RelationKind "collection"` move to 04b with ⌘P. | Their only consumer is ⌘P (§3.12). | None. |
| P24 | The parameter WARNING (> int64, inexact decimals) is 04b's. | §11 lists it with the panel; 04a has no parameter UI. | None. |
| P25 | `_BYTES_LITERALS[SQLPP]` and `_JSON_LITERALS[SQLPP]` are None: `bytes_literal(SQLPP)` raises `ValueError`, `json_literal(SQLPP, t)` is `t`. | SQL++ has no binary literal; JSON is SQL++ syntax (D-04-15). | None. |
| P26 | The CodeMirror SQL++ keyword list repeats `SQLPP_RESERVED`. | The editor needs it at runtime; lang-sql has no N1QL dialect. | A future list change must touch both. |
| P27 | Couchbase `ServerInfo.current_database` is the default context text; the history's `database` is each request's context text. | §3.2.3; `SessionInfo.database` has a meaningful value for display. | None. |
| P28 | A read failure mid-request, or a node unreachable for a later request, is `connection_lost` (the existing Reconnect); a later TLS failure is `tls_failed`. | Spec §4; Reconnect re-runs discovery. | None. |
| P29 | Dev health = the `seed_done` document (via `USE KEYS`, no index); the seed is re-runnable (`IF NOT EXISTS` after the name, as the 7.6 grammar wants; `UPSERT`; existence checks) and ends idle. | R-03b-P1; a seed stopped half-way finishes on the next `up`. | An idle helper container stays listed (dev only). |
| P30 | `client_context_id` is `"dbaguy-" + uuid4().hex`; `CouchbaseConnection.last_client_context_id` exposes it to tests and DEBUG logs. | `run()` receives no request id, and `RunOptions` keeps the spec's four fields. | A DBA cannot match the id to DbaGuy's request id; the DEBUG log pairs them. |
| P31 | The response parser returns what it parsed before a failure and raises it on the next call. | Rows before a bad spot reach the grid (Review Focus 2). | None. |
| P32 | A decode error whose bad token runs to the end of what has arrived means "wait for more" (at the body's end: a cut response → `connection_lost`); one followed by more JSON is malformed (`protocol_error`). | `raw_decode` reports a cut `tru`, `-` or `1e` like a malformed value; the delimiter after it tells them apart. | A malformed token at the very end of the body reads as a cut response. |

### Pre-flight rulings (P33–P48)

Made by the pre-flight review before Task 1 (prototypes of the plan's code and tests against the repository at `3379ac7`), adopted by the coordinator, and written into the tasks' own text: an implementer reads only its task and the Global Constraints.

| # | Ruling | Changed in |
|---|---|---|
| P33 | `SessionController.execute()` sends `profile_plan: false`: openapi-typescript 7 makes a field with a non-null default required, so Task 4's `npm run typecheck` failed without it. Task 12 keeps the field when it adds `query_context`. | Task 4 (Files, Step 6, Step 7 commit); Task 12 Step 6 |
| P34 | `test_warm_up_parses_once_per_dialect_and_logs_its_duration` now expects `len(Dialect) - len(KEYWORD_ONLY_DIALECTS)` calls (listed in "Breaks on purpose"), and `test_classifier.py` imports `_from_sqlglot` for the new keyword-only test. | Task 2 ("Breaks on purpose", Step 1) |
| P35 | Catalog queries do not filter on `k.namespace` (`system:keyspaces` lists only datastore keyspaces, every row in the one namespace `default`, so there is nothing to filter; the 7.x rows carry both `namespace` and `namespace_id`, so `k.namespace` does not match nothing: the first wording of this ruling was wrong, R-04a-C4): dropped from Task 1's reader assertion and Task 10's `_COLLECTIONS`, `_BUCKET_KEYSPACE`, `_KEYSPACES`. The Task 1 probe prints one row of each catalog and the field names are recorded as A22. Spec §3.8's query text is superseded. | Task 1 (Step 1, Step 9, Step 10, A22 row); Task 10 (Rules, `catalog.py`) |
| P36 | The seed's waits count only what it creates: `collections_ready` adds ``k.`scope` IN ["app", "many"]`` (307), `indexes_ready` adds `i.scope_id = "app"` (4), so a `_system` scope or a `_default._default` row cannot stall `up --wait`. | Task 1 Step 5 (`seed.sh`) |
| P37 | `SessionWorker._complete` passes `query_context=req.query_context` into `ExecutionRecord`; a worker test checks that `on_record` receives the request's context (the hook tests called `on_record()` directly and hid the gap). | Task 6 (Step 1 test, Step 4) |
| P38 | The `test_safety.py` additions use the file's alias `A.RUN` / `A.BLOCK` / `A.CONFIRM`, not `GuardAction`. | Task 3 Step 1 |
| P39 | `QueryRequest.named: Mapping[str, str] = field(default_factory=dict)`: a `{}` default raises `ValueError: mutable default` at import. | Task 7 (Interfaces, Step 8) |
| P40 | Before writing `seed.sh`, check the image (`command -v curl cbq cbc python3; cbc create --help`). No `python3`: `value()` reads the first scalar with `sed` (`pretty=false`). No usable `cbc`: drop `bin1` / `txt1` and the A20 assertion, record A20 as "not seeded", carry it to 04c. `u_ttl` stays on the SQL++ `OPTIONS` path (no `cbc` fallback). | Task 1 (Interfaces, Steps 1, 5, 8; A20, A21 rows) |
| P41 | Conditional: `SSLSocket.shutdown()` from the cancelling thread is used only if the A10 probe shows that shutting down the duplicated TCP descriptor does not end a blocked TLS read; then any exception the reader gets while the cancel flag is set counts as cancelled (spec §9). If A10 holds, nothing changes. | Global Constraints (cancel bullet); Task 7 (Step 8 docstring and last paragraph); Task 11 (request flow, Step 3 `EventStream._read`); A10 row |
| P42 | The Task 1 A10 probe sets `settimeout(20)` on the TLS socket, so a blocked read that survives the shutdown reports `TimeoutError` instead of hanging the probe. | Task 1 Step 9 |
| P43 | Tests and probes write U+00A0, U+2003, U+3000, U+200B and U+FEFF as `\u…` escapes: literal invisible characters are lost in a source file. | Task 1 Step 9 (A7 probe line); Task 2 (Step 1 both test variants, Step 3 prose) |
| P44 | `extensions.ts` imports `SQLDialect` as a value (it is a type-only import today); `SQLDialect.define` needs it. | Task 2 Step 7 |
| P45 | `FakeQueryServer._handle` wraps its whole request loop in `try … except OSError: return`, so a client that abandons a response leaves no unhandled-thread warning. | Task 7 Step 1 |
| P46 | `CouchbaseConnection.cancel()` with no request in flight does nothing and sets no flag (the worker's `_cancel_requested` covers the window before `run()`): recorded as a deviation from spec §3.7.2's "only set the flag". | Task 11 (request flow, `cancel()` comment) |
| P47 | `client_context_id` is `"dbaguy-" + uuid4().hex`, filtered to `[A-Za-z0-9_-]` and cut to 64 characters (P30); `client_context_id()` takes any id string (parameter renamed `ident`). | Global Constraints; Task 7 (Interfaces, `http.py`); Task 11 (request flow) |
| P48 | `_couchbase_ready()` uses a 5 s HTTP timeout (a `timeout` keyword on `couchbase_query_text` / `couchbase_query`): a slow query service must not stall every pytest start for 60 s. | Task 1 (Interfaces, Step 3) |

### Rulings made during execution

None yet.

## Spec issues found while planning

- §3.5.1 says a result without `*` is not peeked, and also that such a result's first-page extra keys get an overflow column; both cannot hold (P5).
- §1's success criteria show the context as `travel-sample.inventory`, while §3.2.3 says the display text comes from `qualified()`, which back-ticks `travel-sample`; the history text must also parse back with §3.11.1's rule (P2).
- One `query_context: None` cannot be both "the profile's default" and the picker's "No context" (§3.11.1) (P1).
- §3.2.1's capability table gives Couchbase `documents` and `executed_plan` True, but those features land in 04c and 04b (P6).
- §6.2's `u_ttl` with `{"expiration": 31536000}` (one year) would be read as a Unix time in 1971 — expired at once — since a TTL above 30 days is absolute (P8).
- §3.4.1's cancel copies the descriptor with `socket.fromfd()` at cancel time; a descriptor closed and reused meanwhile would shut down another connection (P3).
- §3.5.4 raises an error that follows the rows from "the next `fetch()`", but the worker calls `next_result()`, not `fetch()`, after a short page (P4).
- §3.4.1's `client_context_id` uses the request id, which `run(sql, options)` does not receive (P30).
- §3.8 gives `CREATE SCOPE` / `CREATE COLLECTION` DDL for every scope and collection; `_default` ones cannot be created (P9).
- §6.2 writes `IF NOT EXISTS` before the name in places; the 7.6 grammar puts it after the name for scopes, collections and named indexes (P29).
- §3.2.2's parameter checks belong to the worker (`invalid_params`), so the models may constrain only their shape (P10).

## Self-review

- **Spec coverage (§11 item 1):** T1 probes and dev environment → Task 1. `Dialect.SQLPP` (LexRules fields, `BodyRule.NONE`, span `params`, quoting / reserved words / literals, tripwires, `/sql/format-guard`) → Task 2; classifier, danger reasons, safety, `is_transaction_statement` → Task 3. Shared contract (`RunOptions` and every adapter's `run()`, capabilities, `endpoint_target`, `presentation`, `RawJsonRows`, `JsonText` / `MISSING` / MIXED, `sockets.enable_keepalive`) → Task 4; formatting cells and the raw JSON writer → Task 5; worker option checks, idle close, raw export, history context → Task 6. Couchbase adapter: http + stream → Task 7; rows, metrics, warnings, errors with code-point → UTF-16 positions → Task 8; settings, URL, TLS, discovery → Task 9; catalog (tree, DDL, snapshot) → Task 10; result, cancel, read-only, transactions refused, registration → Task 11. Frontend minimum (gen:api, CodeMirror dialect and formatter + guard in Task 2, cells, aggregate, viewer, copy formats, transaction controls, connection dialog, tree icons) → Tasks 2, 3, 12. Packaging, measurements, CLAUDE.md → Task 13. §10's 04a assumptions each have an owner in the table above. Out of 04a on purpose: context picker, parameters panel, EXPLAIN view, sampling, completion, documents (04b, 04c; see Carries).
- **Placeholders:** none; the steps that depend on a probe give both outcomes' exact code or command.
- **Names across tasks:** `RunOptions(query_context, params, profile_plan, retry_safe)`; `QueryContext.text()` / `parse()`; `StatementParams(named, positional)`; `AdapterCapabilities.paused_result_max_idle_s`; `SessionWorker(name, opener, clock)`, `check_paused_result()`, `SessionManager.check_paused_results()`; `open_raw_json_writer(kind, fp)`, `RAW_JSON_FORMATS`, `unsupported_reason(kind, dialect)`; `ResponseParser.feed/finish`, `RawRow(text, value, fields)`, `RowField(key, text, value)`, `split_object`, `json_kind`; `NodeClient.send(body, *, retry_safe, attach)`, `failure_reason`; `build_shape(signature, *, plan, bare_star, first_page)`, `has_bare_star`, `needs_first_page`, `value_cell(text, value)`; `statement_error(errors, sql)`, `utf16_column(text, line, column)`; `connection_settings(profile, endpoint, database)`, `candidate_nodes(settings, auth, fetch=None)`; `catalog.Query`, `list_children(query, path)`; `EventStream(response, *, cancelled, node)`, `start_result(stream, sql, *, plan, bare_star, dml, on_done)`, `CouchbaseConnection(client, *, settings, info)`, `query(statement, named)`. Checked task by task.
- **Review Focus:** each of the five lines names the tests that pin it and the task that owns them.
