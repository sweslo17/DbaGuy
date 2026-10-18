# 04b: Couchbase query workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make everyday Couchbase work comfortable: every query tab has its own query context (toolbar picker, tree clicks, browse tabs from the tree and ⌘P, History), statements take typed parameters from a panel, EXPLAIN / ADVISE / executed profiles show as a plan tree with anti-pattern flags, a collection can be sampled for field paths that feed SQL++ completion — and a paused result reads the server's rows ahead into memory, so a result that fits is never closed and a larger one is closed only once the server is really held back (the 04a carry R-04a-T11a/T11b).

**Architecture:** Backend first. A generic, default-off read-ahead capability (`StatementResult.read_ahead`, `AdapterCapabilities.read_ahead_bytes`, a pure `StallClock` in `sessions/read_ahead.py`) lets the worker, between queued commands, pull rows the server has already sent into memory without ever blocking, and time the idle close from where reading really stopped; Couchbase implements it with a non-blocking poll of its socket and a text-only row buffer. The Couchbase adapter adds the executed profile as an extra `plan` result set and number warnings for parameters; pure modules analyse plans (`adapters/couchbase/explain.py`) and derive field paths from samples (`adapters/couchbase/sampling.py`); `CouchbaseService` and `FieldSampleService` serve `/couchbase/*`; SQL++ completion is a sibling of `sql/completion.py`. The frontend then adds the context picker and per-tab context state, the parameters panel, the plan view with Explain / Run with profile / Get index advice, and the sampled-fields view, all on the generated API types.

**Tech Stack:** Python 3.13 stdlib only for Couchbase (`http.client`, `ssl`, `socket`, `selectors`, `json`, `decimal`), FastAPI, pydantic v2, loguru; Couchbase Server `enterprise-7.6.12` dev cluster (OrbStack); React 19 + TypeScript, CodeMirror 6, zustand, TanStack Query, radix-ui (existing `DropdownMenu` / `ContextMenu`). No new dependency.

**Spec:** docs/superpowers/specs/2026-10-18-04-couchbase-design.md — §11 item 2 (04b): query context (picker, tab state and workspace, tree, History, browse statement and ⌘P); parameters panel (split params, execute and export with params, workspace, number WARNING); EXPLAIN / ADVISE / profile (`/couchbase/plan`, PlanView, Explain button, A16); field sampling (`FieldSampleService`, API, collection detail) and SQL++ completion. Built on R-04-6, R-04-9, R-04-10, R-04-11, R-04-13, D-04-11, §3.2, §3.10–§3.13, §4, §5, §6.3, §8, §10 (A16), §12. The 04a plan (rulings P1–P48, carries) is docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md; its ledger .superpowers/sdd/2026-10-18-04a-couchbase-engine/progress.md holds the binding carries R-04a-T11a / R-04a-T11b (read-ahead on pause), A1, A15 and the 04a final-review fix wave R-04a-F1…F6.

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 04b serves it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 | No dependency added. The read-ahead holds at most 8 MiB of row memory per paused result, counted with `sys.getsizeof` (a million tiny rows cannot hide 60 MB of object overhead), and stores row text only (Tasks 2, 3). Sampling stops at 32 MiB (Task 7). Task 13 re-measures RSS while paging and startup with `footprint`. |
| §2 Q2 穩定、不丟工作 | The read-ahead never blocks the worker: Cancel, Discard, Load more and closing the tab are served between steps (Task 2, Review Focus 1). A result that fits in memory is never closed under the user; a larger one closes with its rows kept and a message before the server kills it (Tasks 2, 3). The tab's context and parameter drafts survive a restart through `workspace.json` (Tasks 6, 9, 10). |
| §2 Q3 可信任 | Parameters go to the server as typed JSON literals, never through float; a number the server would read as a double is flagged, naming the parameter, never its value (Task 4). Context texts shown are the backend's `QueryContext.text()` (no frontend quoting, Tasks 6, 9). Plan flags follow the survey's rules and say "info" where a Fetch is normal (Task 5). Sampling counts binary documents as binary, not as strings (Task 7). |
| §2 Q4 聚焦 80% | No automatic INFER (sampling is a manual action), no index advisor beyond ADVISE inside the plan view, no ANY/UNNEST helpers, no auto META columns (FP §4 Won't and SQL++ Shoulds left for later, spec §1). |
| §2 Q5 多引擎從第一天 | Read-ahead, the parameters protocol and panel, the plan presentation and the per-tab context all hang on generic capabilities (`read_ahead_bytes`, `parameters`, `executed_plan`, `plan_format`, `query_context`); other engines keep today's behaviour by default (Tasks 2, 9–11). |
| §2 Q6 macOS 慣例 | ⌘E Explain (free in DbaGuy and the Tauri menu); ⌘↵ / ⌘⇧↵ unchanged; the palette lists Explain with its shortcut (Task 11). |
| FP §4 Must 2 Keyspace 樹 | Clicking a bucket / scope / collection / index sets the active tab's context; double-click on a collection opens a browse tab (D-04-11); Sample fields from the collection's menu (Tasks 9, 12). |
| FP §4 Must 3 編輯器 | Typed named and positional parameters with a panel; the query context picker (Tasks 4, 9, 10). |
| FP §4 Must 4 結果 | Load more on a paused result that fits keeps working however long the user waits (Tasks 2, 3). |
| FP §4 Must 7 EXPLAIN | One-key EXPLAIN as an operator tree with PrimaryScan / `#sequentialscan` / uncovered Fetch / IntersectScan flags, ADVISE's CREATE INDEX with Copy, executed profile with per-operator timings (Tasks 4, 5, 11). |
| FP §4 Must 8 History | Each entry reopens in a new tab with the context it ran in (Tasks 6, 9). |
| FP §4 SQL++ Should 1 | Completion of keyspace paths and nested field paths, back-ticks for special names (Tasks 7, 8). |
| M3 ⌘P | Collections are found by name and open as browse tabs (Tasks 6, 9). |
| M5 執行範圍 | Explain takes the statement under the cursor or the selection, one statement, never more (Task 11). |
| M6 補全 | Sampled fields, fields seen in this tab's results, aliases, keyspaces (Task 8). |
| M10 錯誤訊息 | An error inside an explained statement is marked at its token, the `EXPLAIN ` prefix accounted for (Task 11). |
| M12 取消與 timeout | Cancel and Discard of a paused result reading ahead act within one step (≤ 1 MiB parsed, Task 2). |

## Global Constraints

- Scope is spec §11 item 2 (04b) on branch `feat/04-couchbase`. 04a ends at 960e009 plus its final-review fix wave efc5f5a..4ad8fe1 (R-04a-F1…F6: TLS hints, float cells, `_INTERNAL_DEADLINE` on internal `query()` — server `timeout` 30 s and a 40 s client read timeout through `NodeClient.send(read_timeout=…)` —, `one_line` moved to `sql/jsontext.py`, doc texts) and the follow-ups note 65d0ef0; the pre-flight checks this plan against HEAD. A user's statement still has no deadline (R-04-5), so the read-ahead's sockets are blocking without a timeout. 04c follows on the same branch: do not merge to `main` in this plan.
- **No Couchbase SDK.** Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`); no new Python or frontend dependency in 04b (`@codemirror/lang-json` is 04c).
- Binding values, each defined once, in the file named:
  - `adapters/couchbase/adapter.py`: `PAUSED_RESULT_MAX_IDLE_S = 45.0` (unchanged), `READ_AHEAD_BYTES = 8 * 1024 * 1024` → capability `read_ahead_bytes`; the housekeeping pass runs every 15 s (`container._HOUSEKEEPING_INTERVAL_S`).
  - `sessions/read_ahead.py`: `STALL_RESET_FACTOR = 2` (the server's stall clock is taken to restart once 2 × the engine's read-ahead budget of answer bytes has been read since the stall began, A23), `READ_AHEAD_POLL_S = 0.25` (how soon a read-ahead that found nothing on the socket looks again).
  - `adapters/couchbase/result.py`: `READ_AHEAD_STEP_CHUNKS = 16` (one read-ahead step reads at most 16 × `READ_CHUNK` = 1 MiB); a held row costs `sys.getsizeof(text) + 8` bytes (`row_cost`).
  - `adapters/values.py`: `PLAN_TEXT_LIMIT = 4 * 1024 * 1024` (a cell of a `plan` presentation is sent whole up to this; every other cell keeps `TEXT_LIMIT` = 64 KiB).
  - `adapters/couchbase/sampling.py`: `SAMPLE_MAX_BYTES = 32 * 1024 * 1024`, `FIELD_PATH_MAX_DEPTH = 6`, `FIELD_PATHS_MAX = 2000`. `domain/settings.py`: `CouchbaseSettings.sample_size: int = 1000` (10–10,000).
  - `CompleteRequest.seen_fields`: at most 500 names. `StatementParams` (04a, unchanged): ≤ 200 named, ≤ 200 positional, each literal ≤ 1 MiB. `QueryContext` (04a): bucket ≤ 100, scope ≤ 251 characters.
  - Explain: ⌘E (`Mod-e`; free in DbaGuy's keymaps, `lib/shortcuts.ts` users and the Tauri menu — checked while planning).
- From the spec, verbatim where it binds this plan:
  - R-04-9: 「**每個 tab**：`{bucket, scope?}` 放在 tab 狀態與 workspace，toolbar 有選擇器，`execute`／`export` 每次帶上；點樹上 bucket／scope／collection 設定目前 tab 的 context（FP §4 Must 2）。Profile 的 database 欄位＝預設 context。」
  - R-04-10: 「參數面板，值以 **JSON literal** 輸入（型別＝JSON 型別），名稱 `$x`／`@x`（同一個參數）、位置 `$1`／`?`；放在 `execute`／`export` 指令上，backend 把 literal 原文拼進請求 body（數字不經 float）。」
  - R-04-11: 「INFER 只由使用者手打＋樹上手動 Sample fields（client 端取樣）……欄位路徑在 Python 推導，不可能弄垮 server。補全的欄位路徑來自取樣結果與本 session 結果裡看過的 key。」
  - R-04-13: 「規則照調查 §7（PrimaryScan、`#sequentialscan`、未涵蓋的 Fetch、IntersectScan）。ADVISE 的 `index_statement` 可複製。」
  - D-04-11: 「新 tab 的 context 設成該 scope，內容 ``SELECT META(d).id, * FROM `c` AS d``（兩行），不自動執行（「別耍聰明」，M5）。不加 LIMIT」(back-ticks only where needed, ruling B18).
  - §3.11.2: 「有用到卻沒值或不合法 → 不送出任何一句，toast 指出是哪個並展開面板。匯出帶同樣的參數。」「History 只存 SQL（含參數 placeholder）與 context，不存參數值（v1）。」
  - §4: 「原則：server 錯誤原文優先（M10）；DbaGuy 自己的錯誤是固定文字，不回顯 SQL、文件內容、參數值或密碼。」
  - §5 (loguru): INFO 「取樣（keyspace、文件數、ms）」; DEBUG 「SQL 文字（前 2000 字，同 worker）；request id、client_context_id；參數**名稱**」; 永不 「`Authorization` header、密碼、參數值、文件內容、列內容」. 04b adds: read-ahead state changes at DEBUG (request id, bytes held), one INFO when a paused result has been read to its end (request id, rows held, bytes) — never row contents.
- Threads: only the worker thread that owns a connection sends, reads or closes it (04a). The read-ahead runs on that worker thread between queued tasks and never blocks: it reads only what the socket already holds. Another thread still only calls `cancel()` / `force_cancel()` / `abort()`.
- Literals and identifiers come only from `sql/dialects.py` (SQL++: `quote_ident`, `force_quote_ident`, `keyspace_path`, `quote_literal` = JSON). `domain/` never imports `sql/`. The frontend never quotes: a context's display text comes from the backend's `QueryContext.text()` (via `ContextOption.text`, `HistoryEntry.database`). Offsets crossing HTTP are UTF-16, converted only in `sql/offsets.py` (`Utf16Index`). `sql/` stays pure: no I/O, SQL text never logged above DEBUG.
- Dev cluster as in 04a: service `couchbase` (`enterprise-7.6.12`), `127.0.0.1` ports 8091 / 8093 / 18091 / 18093, admin `dbaguy`/`dbaguy`, limited `dbaguy_reader`/`dbaguy`; start with `docker compose -f dev/docker-compose.yml up -d --build --wait`. Probes and tests only read; a test that must create something removes it by exact name. ADVISE tests use `dbaguy.app.orders`, never `app.users` (A15: 5001 on the server).
- The repo is public: only the fake dev credentials. Never touch `~/Library/Application Support/DbaGuy`; `scripts/dev.sh --web` uses `dev/data`; profiles made in browser checks use `"save_password": false`; nothing is written to the Keychain.
- **Never kill processes by pattern** (no `pkill -f`, no `killall`): stop only processes you started, by PID.
- Tests cover only error-prone logic: the stall clock and the worker's read-ahead scheduling, the Couchbase read-ahead buffer and its poll, the profile result set, parameter number warnings, plan analysis, contexts / history / workspace models, browse SELECT, keyspace search, field-path derivation and its caps, SQL++ completion, and adapter integration against the dev cluster (`requires_couchbase`). The frontend has no test framework: `npm run typecheck && npm run build`, plus each frontend task's manual check list, which the coordinator runs in the browser (`scripts/dev.sh --web`, `http://localhost:1420`).
- `cd backend && .venv/bin/pytest -q` must be green after every task, with the dev containers up; no `requires_couchbase` test may be skipped in a task report.
- After any backend model or API change: `npm run gen:api`, commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts`, never hand-edit them; TS imports API types only through `src/lib/api/types.ts`. `npm run typecheck && npm run build` must pass after every task that touches `src/` or regenerates the types.
- Frontend: colours only through the Tailwind token classes (no hex or rgb); every new React key handler checks `isImeKeyEvent(e)` from `@/lib/ime` first (CodeMirror keymaps skip composition on their own).
- Commit after each task, staging only that task's files by path (never `git add -A`; another agent may have files staged — commit with `git commit -m … -- <paths>` if `git status` shows staged files that are not yours). The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties): wait, or `docker compose -f dev/docker-compose.yml restart bastion`.

## Review Focus

1. **A paused result reading ahead must never hold up a command.** A server that trickles rows, one that has stopped sending, or a 1 MiB burst being parsed: Cancel, Discard, Load more, a new statement and closing the tab are each served within one step (≤ 1 MiB parsed, a 50 ms fake step in the tests), and the server request is `closed` after a discard. → Task 2 `test_read_ahead_never_holds_up_a_command`; Task 3 `test_a_discard_during_read_ahead_frees_the_server_request`.
2. **Rows come out once, in order, exactly**, across the `*` peek, the read-ahead buffer and the stream, with an error that followed the rows raised only after them, and number literals (`u_numbers`) unchanged by the re-parse of held rows. → Task 3 `test_parse_row_rebuilds_what_the_parser_made`, `test_read_ahead_holds_rows_until_the_budget_then_says_full`, `test_an_error_after_the_rows_is_raised_after_the_held_rows`, live `test_rows_stay_in_order_across_the_peek_the_read_ahead_and_the_stream`.
3. **The idle close times the server's stall, not DbaGuy's pauses.** A result read to its end is never closed; a larger one closes 45–60 s after reading stopped (before the server's 77 s kill, A23), a small refill (a Load more of 500 rows) does not restart that clock, an empty socket or 16 MiB drained does, and engines without read-ahead keep the first-pause clock (R-04a-T11b). → Task 2 `test_read_ahead.py` (all), `test_a_result_read_to_its_end_is_never_closed`, `test_the_idle_limit_counts_from_where_the_read_ahead_stopped`, `test_draining_twice_the_budget_restarts_the_clock`; Task 3 live `test_a_result_that_fits_is_read_to_its_end_and_stays_loadable`.
4. **Parameters reach the server exactly and only where used.** `$x` and `@x` are one parameter; `?` and `$1` share positions; each statement gets only what it uses; a missing or invalid value sends nothing; `9007199254740993` stays exact; a number the server would read as a double is flagged by name, never by value. → Task 4 `test_parameters_reach_the_server_exactly`, `test_changes_as_double`, `test_warnings_name_the_parameter_and_never_its_value`, live `test_a_number_the_server_reads_as_a_double_is_flagged`; Task 10 manual checks 1–7.
5. **Query contexts with awkward names round-trip.** A bucket with a dot or a back-tick, `_default._default`, a context from History that the snapshot no longer lists, and an explicit "No context" persisted across a restart: what the picker shows is exactly what is sent and what History records. → Task 6 `test_the_current_context_is_listed_even_when_the_snapshot_lacks_it`, `test_history_entries_carry_their_parsed_context`, `test_workspace_keeps_the_tab_context_and_params`, `test_sqlpp_browse_select_reads_the_collection_with_its_meta_id`; Task 9 manual checks 2–6.

---

## File Structure

```
backend/dbaguy_backend/
  adapters/base.py                       ReadAheadState, ReadAhead, StatementResult.read_ahead(), capability
                                         read_ahead_bytes; FieldSampling (ABC, Task 7)
  adapters/values.py                     PLAN_TEXT_LIMIT; encode_value/encode_row(text_limit)
  adapters/couchbase/http.py             HttpChannel.readable(), QueryResponse.ready()
  adapters/couchbase/stream.py           parse_row(); FieldEvent.text
  adapters/couchbase/result.py           read-ahead buffer (row text), EventStream.next_if_ready()/received,
                                         the profile result set, extra messages
  adapters/couchbase/params.py           (new) changes_as_double(), parameter_warnings()
  adapters/couchbase/explain.py          (new) analyze(), analyze_value(), go_duration_ns() (pure)
  adapters/couchbase/sampling.py         (new) sample_statement(), FieldCollector (pure)
  adapters/couchbase/adapter.py          READ_AHEAD_BYTES; executed_plan; query(context=); sample_fields();
                                         relation_path(); warnings and profile in run()
  domain/couchbase.py                    (new) Plan*, IndexAdvice, PlanView, ContextOption, Keyspace, FieldPath,
                                         FieldSample
  domain/history.py                      HistoryEntry.query_context
  domain/workspace.py                    WorkspaceTab.query_context, params
  domain/schema.py                       RelationKind "collection"
  domain/settings.py                     CouchbaseSettings, AppSettings.couchbase
  sessions/read_ahead.py                 (new) StallClock, STALL_RESET_FACTOR, READ_AHEAD_POLL_S
  sessions/worker.py                     read-ahead scheduling between tasks; stall-timed idle close;
                                         plan cells unlimited to PLAN_TEXT_LIMIT; paused_read_ahead()
  services/couchbase_service.py          (new) contexts, advise, history contexts
  services/field_sample_service.py       (new) sample + cache per (profile, keyspace)
  services/schema_cache.py               search() also over snapshot.keyspaces
  sql/select_builder.py                  SQL++ browse statement
  sql/keyspaces.py                       (new) KeyspaceTree
  sql/completion.py                      shared helpers made public; SnapshotIndex.keyspaces; new kinds
  sql/sqlpp_completion.py                (new) keyspace / field / seen-field completion
  api/couchbase.py                       (new) /couchbase/contexts, /plan, /advise, /fields/sample, /fields
  api/sql.py                             CompleteRequest.query_context / seen_fields; SQLPP dispatch; describe→None
  api/history.py, api/profiles.py, app.py, container.py   wiring
backend/tests/fixtures/couchbase/*.json  (new) EXPLAIN / ADVISE / profile texts from the dev cluster
backend/tests/…                          see each task
scripts/measure-startup.sh               footprint of the measured processes
src/lib/api/types.ts                     new exports
src/stores/workspace.ts, src/stores/settings.ts
src/features/workspace/workspaceFile.ts  query_context, params
src/features/query/{queryContext.ts, ContextPicker.tsx, params.ts, ParamsPanel.tsx}   (new)
src/features/query/{QueryTab.tsx, QueryToolbar.tsx, useQueryRunner.ts, runPlan.ts, runMarks.ts, ResultsArea.tsx,
  ResultGrid.tsx, tabActions.ts}
src/features/explain/{PlanView.tsx, PlanResult.tsx, planApi.ts}   (new)
src/features/explorer/{Explorer.tsx, browse.ts (new), ObjectDetailView.tsx, SampledFields.tsx (new),
  SampleFieldsDialog.tsx (new), kinds.tsx}
src/features/session/{SessionController.ts, execution.ts}
src/features/editor/{statements.ts, completion.ts}
src/features/grid/ExportDialog.tsx, src/features/palette/{objectSearch.tsx, commands.ts},
src/features/history/useHistory.ts, src/features/settings/{settingsForm.ts, SettingsDialog.tsx}
CLAUDE.md, docs/superpowers/specs/2026-10-18-04-couchbase-design.md (R-04-6, §1, §3.7.4, §3.12, §10)
```

Task order follows dependencies. The probes and fixtures (1) come first: their results set two constants and feed Task 5's tests. The generic read-ahead (2) is engine-agnostic and changes the worker; Couchbase's read-ahead (3) implements it; the executed profile and parameter warnings (4) build on the same result object. Plan analysis (5) is pure; the API (6) exposes it with contexts, history and workspace fields; sampling (7) and completion (8) build on the API and the snapshot. The frontend then goes context (9), parameters (10), plan view (11), sampled fields (12); packaging, measurements and docs (13) close.

---
### Task 1: Probes, plan fixtures and test support

**Files:**
- Modify: `backend/tests/support.py`, `backend/tests/test_dev_env.py`
- Create: `backend/tests/fixtures/couchbase/` with `explain_covered.json`, `explain_fetch.json`, `explain_primary.json`, `explain_sequential.json`, `explain_intersect.json`, `explain_join.json`, `explain_subquery.json`, `advise_indexes.json`, `advise_covering.json`, `advise_none.json`, `profile_timings.json`, `profile_join.json`
- Modify: this plan's "Assumptions recorded during execution" table (Planning rulings)
- Scratchpad only, not committed: `<scratchpad>/t1_probe.py`, `<scratchpad>/t1_stall.py`, `<scratchpad>/t1_fixtures.py`

**Interfaces:**
- Consumes: 04a's `tests/support.py` (`couchbase_query`, `couchbase_query_text`, `requires_couchbase`, `COUCHBASE_*`, `_basic`).
- Produces, in `tests/support.py`:
  - `request_active(client_context_id: str) -> bool` — whether `system:active_requests` lists the request now (admin, one query; `system:completed_requests` only logs requests over 1 s, so tests of short requests use this);
  - `PLAN_FIXTURES: Path` = `Path(__file__).parent / "fixtures" / "couchbase"`; `plan_fixture(name: str) -> str` — the text of `PLAN_FIXTURES / f"{name}.json"`.
- Produces, in `tests/fixtures/couchbase/`: each file is the exact text the server sent for `results[0]` of an EXPLAIN or ADVISE (`explain_*`, `advise_*`) or for the top-level `profile` field (`profile_*`) of the statements in Step 5. Task 5's tests read them.

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/test_dev_env.py`:

```python
import http.client
import json

from tests.fake_ssh import wait_until
from tests.support import COUCHBASE_HOST, COUCHBASE_PORT, _basic, plan_fixture, request_active


@requires_couchbase
def test_request_active_follows_a_request():
    ident = "dbaguy-t1-active"
    conn = http.client.HTTPConnection(COUCHBASE_HOST, COUCHBASE_PORT, timeout=30)
    body = json.dumps({"statement": "SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 100000) AS a "
                                    "UNNEST ARRAY_RANGE(0, 1000) AS b", "client_context_id": ident})
    conn.request("POST", "/query/service", body=body,
                 headers={"Authorization": _basic("dbaguy", "dbaguy"), "Content-Type": "application/json"})
    response = conn.getresponse()
    response.read1(1024)  # the request runs: rows are streaming, the client stops reading
    try:
        assert request_active(ident)
    finally:
        conn.close()  # the server stops the request once the connection goes
    wait_until(lambda: not request_active(ident), 10)


@pytest.mark.parametrize("name, key", [
    ("explain_covered", "plan"), ("explain_fetch", "plan"), ("explain_primary", "plan"),
    ("explain_sequential", "plan"), ("explain_intersect", "plan"), ("explain_join", "plan"),
    ("explain_subquery", "~subqueries"), ("advise_indexes", "advice"), ("advise_covering", "advice"),
    ("advise_none", "advice"), ("profile_timings", "executionTimings"), ("profile_join", "executionTimings"),
])
def test_plan_fixtures_hold_the_server_shapes(name, key):  # files only: no cluster needed
    assert key in json.loads(plan_fixture(name))
```

(`pytest` is already imported in this file; add `import pytest` if the pre-flight finds it missing.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_dev_env.py -q`
Expected: collection error `ImportError: cannot import name 'plan_fixture' from 'tests.support'`.

- [ ] **Step 3: Add the helpers** to `backend/tests/support.py` (next to `completed_request_state`):

```python
PLAN_FIXTURES = Path(__file__).parent / "fixtures" / "couchbase"


def plan_fixture(name: str) -> str:
    """A plan, advice or profile text as the dev cluster sent it (captured by 04b Task 1)."""
    return (PLAN_FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def request_active(client_context_id: str) -> bool:
    """Whether the query service is running that request now (system:active_requests, admin)."""
    statement = "SELECT RAW r.state FROM system:active_requests AS r WHERE r.clientContextID = $id"
    return bool(couchbase_query(statement, {"$id": client_context_id})["results"])
```

- [ ] **Step 4: Run the probes** (scratchpad; they only read). Save as `<scratchpad>/t1_probe.py` and run `backend/.venv/bin/python <scratchpad>/t1_probe.py` from the repo root:

```python
"""04b Task 1 probes (read-only): A16 profile shape, A24 binary documents in a sample, A25 plan/advice shapes, A26
parameter binding. Prints one line per check."""
import base64, http.client, json

AUTH = "Basic " + base64.b64encode(b"dbaguy:dbaguy").decode()


def text(statement, **extra):
    c = http.client.HTTPConnection("127.0.0.1", 8093, timeout=60)
    c.request("POST", "/query/service", body=json.dumps({"statement": statement, **extra}),
              headers={"Authorization": AUTH, "Content-Type": "application/json"})
    out = c.getresponse().read().decode()
    c.close()
    return out


ORDERS = "SELECT o.amount FROM dbaguy.app.orders AS o WHERE o.amount > 400 ORDER BY o.amount DESC LIMIT 3"
raw = text(ORDERS, profile="timings")
body = json.loads(raw)
print("A16 keys in order:", list(body), "| profile after metrics:", raw.index('"profile"') > raw.index('"metrics"'))
timings = body["profile"]["executionTimings"]
print("A16 root operator:", timings["#operator"], "| stats keys:", sorted(timings["#stats"]))
print("A24 SELECT RAW d of app.blobs:", json.loads(text("SELECT RAW d FROM dbaguy.app.blobs AS d"))["results"])
print("A24 with META(d).type:", json.loads(text("SELECT META(d).type AS t, d FROM dbaguy.app.blobs AS d"))["results"])
sub = json.loads(text("EXPLAIN SELECT o.amount, (SELECT RAW COUNT(*) FROM dbaguy.app.users AS u) AS n "
                      "FROM dbaguy.app.orders AS o WHERE o.amount > 700"))["results"][0]
print("A25 ~subqueries at the top:", "~subqueries" in sub, "| inside plan:", "~subqueries" in sub["plan"])
none = json.loads(text("ADVISE SELECT o.amount FROM dbaguy.app.orders AS o WHERE o.amount > 400"))["results"][0]
print("A25 advice without a recommendation:", none["advice"]["adviseinfo"]["recommended_indexes"])
bound = json.loads(text("SELECT @n AS a, $n AS b, ? AS c, ? AS d, $1 AS e, $n + 1 AS n1, $u AS u, $p AS p",
                        **{"$n": 9007199254740993, "$u": 18446744073709551615,
                           "$p": 3.141592653589793238462643383279, "args": [7, 8]}))
print("A26:", bound["results"][0])
```

Then the stall probe (about 2 minutes; read-only): save as `<scratchpad>/t1_stall.py`, run `backend/.venv/bin/python <scratchpad>/t1_stall.py`:

```python
"""A23: after a client reads 8 MiB of a big answer and stops, when does 7.6.12 end the request, and does a later read
of 128 KiB or of 8 MiB restart the server's stall clock? Three requests at once; each prints its timings."""
import base64, http.client, json, threading, time, uuid

AUTH = "Basic " + base64.b64encode(b"dbaguy:dbaguy").decode()
BIG = ('SELECT a*10000+b AS i, REPEAT("x", 200) AS pad FROM ARRAY_RANGE(0,10000) AS a '
       'UNNEST ARRAY_RANGE(0,10000) AS b')
MIB = 1024 * 1024


def query(statement, **params):
    c = http.client.HTTPConnection("127.0.0.1", 8093, timeout=30)
    c.request("POST", "/query/service", body=json.dumps({"statement": statement, **params}),
              headers={"Authorization": AUTH, "Content-Type": "application/json"})
    out = json.loads(c.getresponse().read())
    c.close()
    return out


def read(response, n):
    got = 0
    while got < n and (data := response.read1(min(64 * 1024, n - got))):
        got += len(data)


def variant(name, refill):
    ctx = f"dbaguy-t1-{name}-{uuid.uuid4().hex[:8]}"
    c = http.client.HTTPConnection("127.0.0.1", 8093)
    c.request("POST", "/query/service", body=json.dumps({"statement": BIG, "client_context_id": ctx}),
              headers={"Authorization": AUTH, "Content-Type": "application/json"})
    response = c.getresponse()
    read(response, 8 * MIB)
    stop1 = stop2 = time.monotonic()
    if refill:
        time.sleep(40)
        read(response, refill)
        stop2 = time.monotonic()
    active = "SELECT RAW r.state FROM system:active_requests AS r WHERE r.clientContextID = $c"
    while query(active, **{"$c": ctx})["results"]:
        time.sleep(1)
    gone = time.monotonic()
    print(f"{name}: gone {gone - stop1:.1f} s after stop 1, {gone - stop2:.1f} s after stop 2", flush=True)
    c.close()


threads = [threading.Thread(target=variant, args=v) for v in (("V1", 0), ("V2", 8 * MIB), ("V3", 128 * 1024))]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

Compare each line with the Result column of rows A16 and A23–A26 (recorded while planning, on this cluster). If a result differs, write the new result into the table and apply that row's Effect; nothing else in the plan needs editing for the outcomes listed.

- [ ] **Step 5: Capture the fixtures.** Save as `<scratchpad>/t1_fixtures.py` and run `backend/.venv/bin/python <scratchpad>/t1_fixtures.py backend/tests/fixtures/couchbase` from the repo root:

```python
"""Writes the plan fixtures: the server's own text of results[0] (EXPLAIN, ADVISE) or of the profile field."""
import base64, http.client, json, pathlib, sys

AUTH = "Basic " + base64.b64encode(b"dbaguy:dbaguy").decode()
OUT = pathlib.Path(sys.argv[1])
TS = "`travel-sample`.inventory"
ROWS = {
    "explain_covered": "EXPLAIN SELECT o.amount FROM dbaguy.app.orders AS o WHERE o.amount > 400",
    "explain_fetch": "EXPLAIN SELECT * FROM dbaguy.app.orders AS o WHERE o.amount > 400",
    "explain_primary": 'EXPLAIN SELECT * FROM dbaguy.app.users AS u WHERE u.name = "Ann"',
    "explain_sequential": "EXPLAIN SELECT * FROM dbaguy.app.noindex AS n WHERE n.x = 1",
    "explain_intersect": f"EXPLAIN SELECT a.name FROM {TS}.airport AS a USE INDEX (def_inventory_airport_faa, "
                         'def_inventory_airport_city) WHERE a.faa = "SFO" AND a.city = "San Francisco"',
    "explain_join": f"EXPLAIN SELECT r.airline, a.name FROM {TS}.route AS r JOIN {TS}.airline AS a "
                    'ON r.airlineid = META(a).id WHERE r.sourceairport = "SFO" LIMIT 5',
    "explain_subquery": "EXPLAIN SELECT o.amount, (SELECT RAW COUNT(*) FROM dbaguy.app.users AS u) AS n "
                        "FROM dbaguy.app.orders AS o WHERE o.amount > 700",
    "advise_indexes": "ADVISE SELECT * FROM dbaguy.app.orders AS o WHERE o.amount > 400",
    "advise_covering": f'ADVISE SELECT a.name FROM {TS}.airport AS a WHERE a.country = "France" AND a.geo.alt > 100',
    "advise_none": "ADVISE SELECT o.amount FROM dbaguy.app.orders AS o WHERE o.amount > 400",
}
PROFILES = {
    "profile_timings": "SELECT o.amount FROM dbaguy.app.orders AS o WHERE o.amount > 400 ORDER BY o.amount DESC LIMIT 3",
    "profile_join": f"SELECT r.airline, a.name FROM {TS}.route AS r JOIN {TS}.airline AS a "
                    'ON r.airlineid = META(a).id WHERE r.sourceairport = "SFO" LIMIT 5',
}


def answer(statement, **extra):
    c = http.client.HTTPConnection("127.0.0.1", 8093, timeout=60)
    c.request("POST", "/query/service", body=json.dumps({"statement": statement, **extra}),
              headers={"Authorization": AUTH, "Content-Type": "application/json"})
    out = c.getresponse().read().decode()
    c.close()
    return out


def value_text(raw, after):
    """The exact text of the JSON value that follows `after` (`"results": [` or `"profile": `)."""
    start = raw.index(after) + len(after)
    while raw[start] in " \r\n\t":
        start += 1
    _, end = json.JSONDecoder().raw_decode(raw, start)
    return raw[start:end]


OUT.mkdir(parents=True, exist_ok=True)
for name, statement in ROWS.items():
    (OUT / f"{name}.json").write_text(value_text(answer(statement), '"results": ['), encoding="utf-8")
for name, statement in PROFILES.items():
    (OUT / f"{name}.json").write_text(value_text(answer(statement, profile="timings"), '"profile": '), encoding="utf-8")
print("wrote", sorted(p.name for p in OUT.iterdir()))
```

Expected: twelve files. Open two of them (`explain_intersect.json`, `profile_timings.json`): one JSON value each, no secret (they hold `127.0.0.1:8091` and timings, nothing else of the machine).

- [ ] **Step 6: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_dev_env.py -q`
Expected: all pass, none skipped.

- [ ] **Step 7: Commit**

```bash
git add backend/tests/support.py backend/tests/test_dev_env.py backend/tests/fixtures/couchbase \
  docs/superpowers/plans/2026-10-19-04b-couchbase-query-workflow.md
git commit -m "test(couchbase): plan fixtures from the dev cluster, request_active; 04b probes recorded"
```

---

### Task 2: Generic read-ahead of paused results in the worker

**Files:**
- Modify: `backend/dbaguy_backend/adapters/base.py`
- Create: `backend/dbaguy_backend/sessions/read_ahead.py`
- Modify: `backend/dbaguy_backend/sessions/worker.py`
- Create: `backend/tests/test_read_ahead.py`
- Modify: `backend/tests/test_session_worker.py` (new section 8; the closed-text constants)
- Modify (breaks on purpose, the closed text): `backend/tests/test_couchbase_result.py` (`test_a_paused_result_is_closed_before_the_server_ends_it`), `backend/tests/test_couchbase_sessions.py` (`test_an_idle_paused_result_is_closed_and_frees_the_server_request`: its `"after it first paused"` assertion)

**Interfaces:**
- Consumes: 04a's worker (`_OpenExecution`, `_PausedMark`, `_pause`, `check_paused_result`, `_close_idle_paused`, the injectable `clock`), `AdapterCapabilities.paused_result_max_idle_s`.
- Produces, in `adapters/base.py`:
  - `class ReadAheadState(StrEnum)`: `READING = "reading"` (took rows the server had sent; more may be on the socket), `WAITING = "waiting"` (nothing on the socket: the server is working or done, not held back by DbaGuy), `FULL = "full"` (the budget is used up: reading stopped, the server will be held back from here), `DONE = "done"` (the whole answer is in memory, or ended in an error kept for later: nothing can stall);
  - `@dataclass(frozen=True) class ReadAhead`: `state: ReadAheadState`, `received: int = 0` (bytes of the server's answer read so far, by any path: cumulative);
  - `StatementResult.read_ahead(self, budget: int) -> ReadAhead` — non-abstract; default `return ReadAhead(ReadAheadState.FULL)`. Contract (docstring): worker thread only, only while the result is paused; must never block; reads rows the server has already sent into memory until the rows held take `budget` bytes; rows read here come out of the next `fetch()` / `fetch_raw()` first, in order; a failure met while reading is kept and raised after the rows read before it (never raised here);
  - `AdapterCapabilities.read_ahead_bytes: int | None = None` (None: the engine does not read ahead; not in `EngineInfo`).
- Produces, in `sessions/read_ahead.py`: `STALL_RESET_FACTOR = 2`, `READ_AHEAD_POLL_S = 0.25`, `class StallClock` with `__init__(self, reset_bytes: int | None)`, attribute `since: float | None` (when the server may have been held back since; None: it is not), `update(self, step: ReadAhead, now: float) -> None`.
- Produces, in `sessions/worker.py`: `SessionWorker.paused_read_ahead(self) -> ReadAheadState | None` (any thread: the open paused result's read-ahead state; None when no result is paused or the engine does not read ahead); `paused_result_closed_text(limit_s: float) -> str` now says `f"Closed this result {after} after the server was left waiting for it, to free the query node — run the statement again to load more, or export it to get every row"` (`after` as before: whole minutes `N min`, else `N s`).

The worker's rules, which Steps 1 and 3 pin:
- An engine with `read_ahead_bytes` set reads ahead while a result is paused: after each pause the worker, whenever no task is queued, calls `result.read_ahead(read_ahead_bytes)` — at once again after `READING`, after `READ_AHEAD_POLL_S` of an empty queue after `WAITING`, and not again after `FULL` (until the next pause, since a Load more frees room) or `DONE` (never again).
- Every step feeds `StallClock.update(step, clock())`; the paused mark's `since` follows `StallClock.since`; `check_paused_result()` closes only a mark whose `since` is set and at least `paused_result_max_idle_s` old.
- An engine without `read_ahead_bytes` never has `read_ahead` called: at each pause the worker feeds `ReadAhead(FULL)` itself, so the clock runs from the first pause and a Load more does not restart it (R-04a-T11b, unchanged).
- `StallClock.update`: `WAITING` or `DONE` → `since = None`. `READING` or `FULL` with `since` set and `step.received - received_at_stall >= reset_bytes` (reset_bytes not None) → `since = None` (the server has been unblocked). Then `FULL` with `since` None → `since = now`, `received_at_stall = step.received`.
- A read-ahead that raises (a bug: adapters keep failures) is logged with its traceback and treated as `FULL` with the last `received`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_read_ahead.py`:

```python
"""The stall clock of a paused result (R-04a-T11a/T11b, A23): when the server may be held back because DbaGuy stopped
reading, and when that ends."""

import pytest

from dbaguy_backend.adapters.base import ReadAhead
from dbaguy_backend.adapters.base import ReadAheadState as S
from dbaguy_backend.sessions.read_ahead import StallClock

MIB = 1024 * 1024


def test_the_stall_starts_where_reading_stopped():
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.READING, 1 * MIB), now=10.0)
    assert clock.since is None  # still reading: the server is not held back
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=12.0)
    assert clock.since == 12.0
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=30.0)  # nothing read since: the same stall
    assert clock.since == 12.0


def test_a_small_refill_keeps_the_stall_and_a_large_drain_ends_it():  # A23: 128 KiB does not restart the server's clock
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(S.READING, 8 * MIB + 128 * 1024), now=20.0)  # a Load more's refill
    clock.update(ReadAhead(S.FULL, 8 * MIB + 128 * 1024), now=20.1)
    assert clock.since == 10.0
    clock.update(ReadAhead(S.READING, 24 * MIB), now=30.0)  # 16 MiB read since the stall began
    assert clock.since is None
    clock.update(ReadAhead(S.FULL, 24 * MIB), now=31.0)
    assert clock.since == 31.0


@pytest.mark.parametrize("state", [S.WAITING, S.DONE])
def test_an_empty_socket_or_the_end_means_no_stall(state):
    clock = StallClock(reset_bytes=16 * MIB)
    clock.update(ReadAhead(S.FULL, 8 * MIB), now=10.0)
    clock.update(ReadAhead(state, 8 * MIB), now=11.0)
    assert clock.since is None


def test_without_read_ahead_the_stall_starts_at_the_first_pause_and_never_restarts():  # R-04a-T11b
    clock = StallClock(reset_bytes=None)
    clock.update(ReadAhead(S.FULL), now=10.0)
    clock.update(ReadAhead(S.FULL), now=50.0)
    assert clock.since == 10.0
```

Append section 8 to `backend/tests/test_session_worker.py` (imports to add at the top: `import queue`, `from dbaguy_backend.adapters.base import ReadAhead, ReadAheadState`):

```python
# ---- 8. read-ahead of paused results (04b, R-04a-T11a) ----

MIB = 1024 * 1024
CLOSED_AFTER_45 = ("Closed this result 45 s after the server was left waiting for it, to free the query node — run the "
                   "statement again to load more, or export it to get every row")


class ScriptedResult(FakeResult):
    """Rows like FakeResult. Its read-ahead answers from `script` (the test feeds steps or exceptions); with nothing fed
    within 50 ms it says READING again, as a result still reading would."""

    def __init__(self, rows: int, script: queue.Queue) -> None:
        super().__init__(rows)
        self.script = script
        self.received = 0
        self.calls = 0

    def read_ahead(self, budget: int) -> ReadAhead:
        self.calls += 1
        try:
            step = self.script.get(timeout=0.05)
        except queue.Empty:
            return ReadAhead(ReadAheadState.READING, self.received)
        if isinstance(step, BaseException):
            raise step
        self.received = step.received
        return step


class ReadAheadConnection(FakeConnection):
    """`rows N` returns a ScriptedResult sharing the connection's `script`."""

    def __init__(self) -> None:
        super().__init__()
        self.script: queue.Queue = queue.Queue()
        self.results: list[ScriptedResult] = []

    def statement(self, sql, options):
        result = super().statement(sql, options)
        if not sql.startswith("rows "):
            return result
        scripted = ScriptedResult(int(sql.split()[1]), self.script)
        self.results.append(scripted)
        return scripted


def read_ahead_worker(clock, **capabilities):
    connection = ReadAheadConnection()
    worker, _, rec = make_worker(connection, clock=clock, **{"paused_result_max_idle_s": 45.0,
                                                             "read_ahead_bytes": 1 * MIB, **capabilities})
    return worker, connection, rec


def test_a_result_read_to_its_end_is_never_closed():
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        conn.script.put(ReadAhead(ReadAheadState.DONE, 300_000))
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.DONE)
        clock.now += 10_000
        worker.check_paused_result()
        _barrier(worker)
        assert not rec.of("p", "finished")
        worker.submit(FetchMoreCommand(request_id="p", count=1000))
        rec.wait_for(finished("p"))
        assert sum(len(e.rows) for e in rec.of("p", "rows")) == 1200 and not rec.of("p", "error")
    finally:
        worker.close()


def test_the_idle_limit_counts_from_where_the_read_ahead_stopped():  # Review Focus 3
    clock = Clock()  # 1000.0 at the pause
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "p", "rows 2000")
        rec.wait_for(paused("p", 500))
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.READING)
        clock.now = 1030.0  # still reading 30 s later: the server is not held back, nothing may close
        worker.check_paused_result()
        _barrier(worker)
        conn.script.put(ReadAhead(ReadAheadState.FULL, 1 * MIB))
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1074.0  # 44 s after the stop, 74 s after the pause
        worker.check_paused_result()
        _barrier(worker)
        assert not rec.of("p", "finished")
        clock.now = 1075.0
        worker.check_paused_result()
        rec.wait_for(finished("p"))
        assert rec.of("p", "message")[-1].text == CLOSED_AFTER_45
    finally:
        worker.close()


def test_an_empty_socket_after_a_load_more_restarts_the_clock():
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "p", "rows 2000")
        rec.wait_for(paused("p", 500))
        conn.script.put(ReadAhead(ReadAheadState.FULL, 1 * MIB))  # stopped at 1000.0
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        worker.submit(FetchMoreCommand(request_id="p", count=500))
        rec.wait_for(paused("p", 1000))
        clock.now = 1040.0
        conn.script.put(ReadAhead(ReadAheadState.WAITING, 1 * MIB))  # the refill drained the socket
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.WAITING)
        clock.now = 1050.0
        conn.script.put(ReadAhead(ReadAheadState.FULL, 1 * MIB + 4096))
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1094.0  # 94 s after the first stop, 44 s after the second
        worker.check_paused_result()
        _barrier(worker)
        assert not rec.of("p", "finished")
        clock.now = 1095.0
        worker.check_paused_result()
        rec.wait_for(finished("p"))
    finally:
        worker.close()


def test_draining_twice_the_budget_restarts_the_clock():  # STALL_RESET_FACTOR = 2, budget 1 MiB here
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "p", "rows 5000")
        rec.wait_for(paused("p", 500))
        conn.script.put(ReadAhead(ReadAheadState.FULL, 1 * MIB))  # stopped at 1000.0
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1040.0
        conn.script.put(ReadAhead(ReadAheadState.READING, 2 * MIB))  # 1 MiB since the stop: not enough
        conn.script.put(ReadAhead(ReadAheadState.FULL, 2 * MIB))
        worker.submit(FetchMoreCommand(request_id="p", count=2000))
        rec.wait_for(paused("p", 2500))
        wait_until(lambda: conn.script.empty() and worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1045.0  # 45 s after the first stop: closes
        worker.check_paused_result()
        rec.wait_for(finished("p"))
    finally:
        worker.close()
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "q", "rows 5000")
        rec.wait_for(paused("q", 500))
        conn.script.put(ReadAhead(ReadAheadState.FULL, 1 * MIB))  # stopped at 1000.0
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1040.0
        conn.script.put(ReadAhead(ReadAheadState.READING, 3 * MIB))  # 2 MiB since the stop: unblocked
        conn.script.put(ReadAhead(ReadAheadState.FULL, 3 * MIB))  # stopped again at 1040.0
        worker.submit(FetchMoreCommand(request_id="q", count=2000))
        rec.wait_for(paused("q", 2500))
        wait_until(lambda: conn.script.empty() and worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now = 1084.0
        worker.check_paused_result()
        _barrier(worker)
        assert not rec.of("q", "finished")
        clock.now = 1085.0
        worker.check_paused_result()
        rec.wait_for(finished("q"))
    finally:
        worker.close()


def test_read_ahead_never_holds_up_a_command():  # Review Focus 1: every step here takes 50 ms and finds more
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        execute(worker, "p", "rows 2000")
        rec.wait_for(paused("p", 500))
        wait_until(lambda: conn.results[0].calls >= 3)  # stepping
        started = time.monotonic()
        worker.cancel("p")  # paused: a discard
        rec.wait_for(finished("p"))
        assert time.monotonic() - started < scaled(1.0)
        execute(worker, "n", "rows 1")
        rec.wait_for(finished("n"))
        assert conn.results[0].closed
    finally:
        worker.close()


def test_a_failing_read_ahead_falls_back_to_the_first_pause_clock():
    clock = Clock()
    worker, conn, rec = read_ahead_worker(clock)
    try:
        conn.script.put(RuntimeError("bug in an adapter"))
        execute(worker, "p", "rows 2000")
        rec.wait_for(paused("p", 500))
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL)
        clock.now += 45
        worker.check_paused_result()
        rec.wait_for(finished("p"))
        assert sum(len(e.rows) for e in rec.of("p", "rows")) == 500  # the rows loaded stay
    finally:
        worker.close()


def test_engines_without_read_ahead_never_read_ahead():
    clock = Clock()
    connection = ReadAheadConnection()
    worker, _, rec = make_worker(connection, clock=clock, paused_result_max_idle_s=45.0)
    try:
        execute(worker, "p", "rows 1200")
        rec.wait_for(paused("p", 500))
        _barrier(worker)
        assert connection.results[0].calls == 0 and worker.paused_read_ahead() is None
    finally:
        worker.close()
```

Add `from tests.support import scaled` if the file does not import it yet. Change the two closed-text constants of section 7 to the new wording: `CLOSED = ("Closed this result 2 min after the server was left waiting for it, to free the query node — run the statement again to load more, or export it to get every row")` and `CLOSED_45` likewise with `45 s`.

Breaks on purpose (update in this task): `tests/test_couchbase_result.py::test_a_paused_result_is_closed_before_the_server_ends_it` — its two `startswith` lines become `startswith("Closed this result 45 s after the server was left waiting")` and `startswith("Closed this result 2 min after the server was left waiting")`; `tests/test_couchbase_sessions.py::test_an_idle_paused_result_is_closed_and_frees_the_server_request` — drop `assert "after it first paused" in …` (the equality with `paused_result_closed_text(PAUSED_RESULT_MAX_IDLE_S)` stays). Task 3 revisits that live test once Couchbase reads ahead.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_read_ahead.py tests/test_session_worker.py -q`
Expected: `ImportError: cannot import name 'ReadAhead' from 'dbaguy_backend.adapters.base'`.

- [ ] **Step 3: Add the contract** to `adapters/base.py`: `ReadAheadState`, `ReadAhead`, `StatementResult.read_ahead()` (default `FULL`), `AdapterCapabilities.read_ahead_bytes` — as in Interfaces, with the contract docstring. `info()` is unchanged (the field is not part of `EngineInfo`).

- [ ] **Step 4: Write `sessions/read_ahead.py`**: the two constants (one comment each: the factor comes from A23 — on 7.6.12 a 128 KiB read after a stall did not restart the server's 77 s clock, about 5.5 MiB read in small steps and 8 MiB at once did; twice the 8 MiB budget leaves room for a setup with bigger buffers) and `StallClock` following the `update` rule in Interfaces (keep `received_at_stall` private).

- [ ] **Step 5: Schedule the read-ahead in `sessions/worker.py`.**
  - `_OpenExecution`: drop `first_paused_at`; add `stall: StallClock` (no default: built where the execution is, `StallClock(None if budget is None else STALL_RESET_FACTOR * budget)` with `budget = self._hooks.capabilities.read_ahead_bytes`), `ahead: ReadAheadState | None = None`, `received: int = 0` (the last step's).
  - `_PausedMark.since: float | None` (the stall start; None: nothing holds the server back).
  - `_pause()`: after the `_set_running(None)` check, with `budget = self._hooks.capabilities.read_ahead_bytes`: `None` → `ex.stall.update(ReadAhead(ReadAheadState.FULL), self._clock())`; otherwise, unless `ex.ahead is ReadAheadState.DONE`, `ex.ahead = ReadAheadState.READING` (read on while it waits). The mark is `_PausedMark(next(self._pause_tokens), ex.request_id, ex.stall.since)`.
  - The loop: replace the queue read in `_run_queued` with `_next_task()`; `_READ_AHEAD = object()` marks "no task queued, read ahead now":

```python
    def _next_task(self) -> object:
        """The next queued task. While a paused result reads ahead the queue is never waited on for long: an empty
        queue gives `_READ_AHEAD` at once after READING, after READ_AHEAD_POLL_S after WAITING. None: the opener failed
        and nothing is left in the queue."""
        if self._lease is None:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                return None
        wait = self._read_ahead_wait()
        try:
            if wait is None:
                return self._queue.get()
            return self._queue.get(timeout=wait) if wait > 0 else self._queue.get_nowait()
        except queue.Empty:
            return _READ_AHEAD

    def _read_ahead_wait(self) -> float | None:
        """None: nothing reads ahead (block on the queue); else how long to wait for a task before the next step."""
        ex = self._open_exec
        paused = self._paused  # only this thread changes it
        if ex is None or paused is None or paused.request_id != ex.request_id:
            return None
        if ex.ahead is ReadAheadState.READING:
            return 0.0
        return READ_AHEAD_POLL_S if ex.ahead is ReadAheadState.WAITING else None
```

    `_run_queued` returns on `None` or `_STOP`, calls `self._read_ahead_step()` on `_READ_AHEAD`, and runs anything else as before (exceptions logged).
  - `_read_ahead_step()`: `ex = self._open_exec`; `step = ex.result.read_ahead(budget)`; on any `Exception` log `logger.exception("Read-ahead of {} failed on {}; the result waits for Load more as it is", ex.request_id, self.name)` and use `ReadAhead(ReadAheadState.FULL, ex.received)`. Then `ex.ahead, ex.received = step.state, step.received`; `ex.stall.update(step, self._clock())`; under `_state_lock`, if `self._paused` is still this request's, `self._paused = replace(self._paused, since=ex.stall.since)`. Log a change of state: DEBUG `"[{}] read-ahead of {}: {} ({} bytes read)"`; INFO once at `DONE`: `"[{}] paused result {} read to its end ({} bytes)"` — no row content.
  - `check_paused_result()`: return when `mark is None or mark.since is None or self._clock() - mark.since < limit`. Docstring: the clock is the stall clock (from where reading stopped; for engines without read-ahead, the first pause).
  - `paused_read_ahead()`: read `self._paused` under `_state_lock`; return `ex.ahead` of the open execution when a mark for it exists, else None (`ex.ahead` is None for engines without read-ahead).
  - `paused_result_closed_text`: the new wording (Interfaces). Update the class docstring's locking paragraph: the read-ahead runs on the worker thread between tasks and touches only the open result.

- [ ] **Step 6: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_read_ahead.py tests/test_session_worker.py tests/test_couchbase_result.py -q`
Expected: all pass. Then the whole suite: `cd backend && .venv/bin/pytest -q` — green (no engine sets `read_ahead_bytes` yet, so every other engine and Couchbase behave as in 04a).

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/sessions/read_ahead.py \
  backend/dbaguy_backend/sessions/worker.py backend/tests/test_read_ahead.py backend/tests/test_session_worker.py \
  backend/tests/test_couchbase_result.py backend/tests/test_couchbase_sessions.py
git commit -m "feat(sessions): paused results read ahead between commands; the idle close times the server's stall"
```

---
### Task 3: Couchbase reads ahead: socket poll, text-only buffer, live pins

**Files:**
- Modify: `backend/dbaguy_backend/adapters/couchbase/http.py`, `backend/dbaguy_backend/adapters/couchbase/stream.py`, `backend/dbaguy_backend/adapters/couchbase/result.py`, `backend/dbaguy_backend/adapters/couchbase/adapter.py`
- Modify: `backend/tests/couchbase_fakes.py` (`FakeResponse.ready()`, `arrived`)
- Create: `backend/tests/test_couchbase_read_ahead.py`
- Modify: `backend/tests/test_couchbase_sessions.py` (`test_an_idle_paused_result_is_closed_and_frees_the_server_request`: wait for `FULL` before moving the clock)

**Interfaces:**
- Consumes (Task 2): `ReadAhead`, `ReadAheadState`, the `StatementResult.read_ahead(budget)` contract, `AdapterCapabilities.read_ahead_bytes`, `SessionWorker.paused_read_ahead()`; (Task 1) `request_active`.
- Produces:
  - `adapters/couchbase/adapter.py`: `READ_AHEAD_BYTES = 8 * 1024 * 1024`; `CouchbaseAdapter.capabilities` gains `read_ahead_bytes=READ_AHEAD_BYTES`.
  - `adapters/couchbase/http.py`: `HttpChannel.readable(self) -> bool` (worker thread; true when a read would not wait: bytes on the socket, decrypted TLS bytes pending, or the socket gone); `QueryResponse.ready(self) -> bool` (`done`, failed, or `channel.readable()`).
  - `adapters/couchbase/stream.py`: `parse_row(text: str) -> RawRow` — one complete row text, rebuilt exactly as `ResponseParser` made it (`parse_row(row.text) == row`); `ValueError` for anything else.
  - `adapters/couchbase/result.py`: `READ_AHEAD_STEP_CHUNKS = 16`; `row_cost(text: str) -> int` (= `sys.getsizeof(text) + 8`); `NOT_READY` (a singleton); `Response.ready()` in the protocol; `EventStream.received: int` (bytes of the answer read so far) and `EventStream.next_if_ready(self) -> Event | None | NotReady` (as `next()`, reading at most one chunk, and only when `response.ready()`); `CouchbaseResult.read_ahead(self, budget: int) -> ReadAhead`; `CouchbaseResult.held: int` (bytes the rows read ahead take, by `row_cost`).

How the Couchbase result reads ahead (the tests pin each line):
- The held rows are kept as their text only, in a `deque[str]`, and rebuilt with `parse_row` when delivered (a parsed row with its fields costs 4–5 times its text; ruling B5). `fetch()` / `fetch_raw()` deliver the `*` peek's rows first, then the held rows, then the stream; `held` drops as rows go out.
- One step: take the events the stream already holds (no I/O), then read while `next_if_ready()` has something, until `held >= budget` (`FULL`; checked after each row taken, so the events after that row stay queued in the stream), the answer ends (`DONE`: `_end()` runs, the connection is released through `on_done`), nothing is ready (`READING` if this step took something, else `WAITING`), or the step has read `READ_AHEAD_STEP_CHUNKS` chunks (`READING`). A row that crosses the budget is still taken (a row is never split). `received` is `EventStream.received`.
- A failure met while reading ahead (cut connection, protocol error, server error after the rows, a cancel) is kept as the pending error and the step says `DONE`; the held rows go out before it (Review Focus 2). `_rows()` stops on the pending error only when neither the peek nor the held rows have anything left.
- `close()` drops the held rows (`held == 0`) and abandons an answer not read to its end, as before.
- `HttpChannel.readable()` looks at the socket `open_socket()` returned, kept on the channel (http.client drops `conn.sock` after a `Connection: close` answer while the response still reads from it): `ssl.SSLSocket.pending()` first, then a zero-timeout `selectors.DefaultSelector` poll for `EVENT_READ`; a closed or failed socket counts as readable (the read then fails at once). Bytes http.client has already pulled into its own buffer are not seen (ruling B6): a step reports `WAITING`, which never closes anything, and the next Load more reads them.

- [ ] **Step 1: Write the failing tests.** In `backend/tests/couchbase_fakes.py`, `FakeResponse` gets `self.arrived: int | None = None` (chunks "on the socket"; None: all of them), `ready()` (true when `fail_with` is set, no chunk is left, `arrived` is None or above 0) and `read_chunk()` decrements `arrived` when it is set. Create `backend/tests/test_couchbase_read_ahead.py`:

```python
"""Couchbase reads a paused result ahead (R-04a-T11a): rows the server has sent go into memory without waiting, up to
READ_AHEAD_BYTES, in order, with any error after them; and against the dev cluster, a result that fits is read to its
end while a larger one keeps the server's request open."""

import sys
import time

import pytest

from dbaguy_backend.adapters.base import ReadAheadState, StatementError
from dbaguy_backend.adapters.couchbase.adapter import PAUSED_RESULT_MAX_IDLE_S, READ_AHEAD_BYTES
from dbaguy_backend.adapters.couchbase.http import ChannelError
from dbaguy_backend.adapters.couchbase.result import READ_AHEAD_STEP_CHUNKS, row_cost
from dbaguy_backend.adapters.couchbase.stream import ResponseParser, RowEvent, parse_row
from dbaguy_backend.domain.execution import ExecuteCommand, FetchMoreCommand
from dbaguy_backend.sessions.worker import paused_result_closed_text
from tests.fake_ssh import wait_until
from tests.support import request_active, requires_couchbase, scaled
from tests.test_couchbase_adapter import STREAM
from tests.test_couchbase_result import result_of
from tests.test_couchbase_sessions import APP, Clock, page_through, start_cb_worker
from tests.test_dev_env import U_NUMBERS
from tests.test_sessions import finished, paused

BIG = 10**12  # a budget nothing reaches


def chunked(body: bytes, size: int) -> list[bytes]:
    return [body[i:i + size] for i in range(0, len(body), size)]


def numbers_body(count: int) -> bytes:
    rows = ",".join(f'{{"n":{i}}}' for i in range(count))
    return (f'{{"signature":{{"n":"number"}},"results":[{rows}],"status":"success",'
            f'"metrics":{{"resultCount":{count}}}}}').encode()


def numbers(result, n):
    return [row[0] for row in result.fetch(n)]


def test_parse_row_rebuilds_what_the_parser_made():  # Review Focus 2: exact literals survive the re-parse
    body = ('{"signature":"json","results":[' + U_NUMBERS + ',"a\\"b \\u00e9",42,1e400,-0,[1,{"x":null}],'
            '{"a":{"b":[1,2]},"c":"d","e":1.10},null,true,{}],"status":"success"}').encode()
    parser = ResponseParser()
    rows = [e.row for e in parser.feed(body) + parser.finish() if isinstance(e, RowEvent)]
    assert len(rows) == 10 and [parse_row(row.text) for row in rows] == rows


def test_row_cost_counts_what_python_holds():
    assert row_cost("abc") == sys.getsizeof("abc") + 8
    assert row_cost("中文") > row_cost("ab")  # wider characters take more


def test_read_ahead_holds_rows_until_the_budget_then_says_full():  # Review Focus 2
    result, _, released = result_of(*chunked(numbers_body(5000), 4096), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert numbers(result, 10) == list(range(10))
    budget = 50_000
    step = result.read_ahead(budget)
    while step.state is ReadAheadState.READING:
        step = result.read_ahead(budget)
    assert step.state is ReadAheadState.FULL
    assert budget <= result.held < budget + row_cost('{"n":4999}')
    assert not released  # the answer goes on: the connection is still this request's
    assert numbers(result, 5000) == list(range(10, 5000))  # held rows first, then the stream, in order
    assert result.held == 0


def test_received_counts_every_byte_and_the_end_releases_the_connection():
    body = numbers_body(3000)
    result, _, released = result_of(*chunked(body, 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    step = result.read_ahead(BIG)
    while step.state is not ReadAheadState.DONE:
        step = result.read_ahead(BIG)
    assert step.received == len(body) and released
    assert numbers(result, 5000) == list(range(3000))
    assert result.next_result() is False


def test_one_step_reads_at_most_its_chunks():
    result, _, _ = result_of(*chunked(numbers_body(5000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()  # the head and the first chunk
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.READING and step.received == (1 + READ_AHEAD_STEP_CHUNKS) * 1024


def test_nothing_on_the_socket_is_waiting_and_nothing_is_read():
    result, response, _ = result_of(*chunked(numbers_body(5000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    response.arrived = 0
    assert result.read_ahead(BIG).state is ReadAheadState.READING  # the rows already parsed, taken without a read
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.WAITING and step.received == 1024
    response.arrived = 2
    step = result.read_ahead(BIG)
    assert step.state is ReadAheadState.READING and step.received == 3 * 1024


def test_an_error_after_the_rows_is_raised_after_the_held_rows():  # Review Focus 2
    body = (b'{"signature":{"a":"json"},"results":[{"a":1},{"a":2},{"a":3}],'
            b'"errors":[{"code":1080,"msg":"Timeout 3s exceeded"}],"status":"fatal"}')
    result, _, released = result_of(*chunked(body, 16), sql="SELECT a FROM ks", bare_star=False)
    result.columns()
    assert result.fetch(1) == [[1]]
    assert result.read_ahead(BIG).state is ReadAheadState.DONE and released
    assert result.fetch(10) == [[2], [3]]
    with pytest.raises(StatementError, match="Timeout 3s exceeded"):
        result.fetch(1)


def test_a_cut_answer_while_reading_ahead_keeps_the_rows_read():
    result, response, _ = result_of(*chunked(numbers_body(3000), 1024), sql="SELECT n FROM ks", bare_star=False)
    result.columns()
    assert result.read_ahead(BIG).state is ReadAheadState.READING
    response.fail_with = ChannelError("Connection reset by peer")
    assert result.read_ahead(BIG).state is ReadAheadState.DONE
    held = numbers(result, 5000)
    assert held == list(range(len(held))) and len(held) > 0
    with pytest.raises(StatementError) as error:
        result.fetch(1)
    assert error.value.code == "connection_lost"


def test_closing_drops_the_held_rows_and_abandons_the_answer():
    result, response, released = result_of(*chunked(numbers_body(5000), 4096), sql="SELECT n FROM ks",
                                           bare_star=False)
    result.columns()
    while result.read_ahead(20_000).state is ReadAheadState.READING:
        pass
    result.close()
    assert result.held == 0 and response.abandoned and released


@requires_couchbase
def test_a_result_that_fits_is_read_to_its_end_and_stays_loadable():  # Review Focus 3
    clock = Clock()
    worker, rec = start_cb_worker(clock)
    try:
        worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id, * FROM events AS d", query_context=APP,
                                     page_size=500))
        rec.wait_for(paused("e", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.DONE, 30)
        assert not request_active(ident)  # the server's request is over: it holds no servicer
        clock.now += 10 * PAUSED_RESULT_MAX_IDLE_S
        worker.check_paused_result()
        assert page_through(worker, rec, "e", count=10_000) == 20_000
        assert paused_result_closed_text(PAUSED_RESULT_MAX_IDLE_S) not in [m.text for m in rec.of("e", "message")]
    finally:
        worker.close()


@requires_couchbase
def test_a_larger_result_holds_the_budget_and_keeps_the_server_request_open():
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        held = worker._open_exec.result.held  # the adapter's own figure: a test may look inside
        assert READ_AHEAD_BYTES <= held < READ_AHEAD_BYTES + 1024
        assert request_active(ident)  # the rest is still the server's to send
    finally:
        worker.close()


@requires_couchbase
def test_rows_stay_in_order_across_the_peek_the_read_ahead_and_the_stream():  # Review Focus 2
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)
        for page in range(1, 31):  # 300,000 more rows: past the ~120,000 held, into the stream
            worker.submit(FetchMoreCommand(request_id="s", count=10_000))
            rec.wait_for(paused("s", 500 + page * 10_000), timeout=30)
        values = [row[0] for event in rec.of("s", "rows") for row in event.rows]
        assert values == list(range(300_500))
    finally:
        worker.close()


@requires_couchbase
def test_a_discard_during_read_ahead_frees_the_server_request():  # Review Focus 1
    worker, rec = start_cb_worker()
    try:
        worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500))
        rec.wait_for(paused("s", 500), timeout=30)
        ident = worker.connection.last_client_context_id
        started = time.monotonic()
        worker.cancel("s")  # paused, reading ahead: a discard
        rec.wait_for(finished("s"), timeout=10)
        assert time.monotonic() - started < scaled(1.0)
        wait_until(lambda: not request_active(ident), 5)
    finally:
        worker.close()
```

In `tests/test_couchbase_sessions.py::test_an_idle_paused_result_is_closed_and_frees_the_server_request`, right after `rec.wait_for(paused("p", 500), timeout=30)` add `wait_until(lambda: worker.paused_read_ahead() is ReadAheadState.FULL, 30)` (imports: `ReadAheadState` from `adapters.base`, `wait_until` from `tests.fake_ssh`) and change its first comment to "reading ahead stopped here: the idle clock starts". The rest stays: a 500-row Load more refills a few KB, far from the 16 MiB that restart the clock (A23), so the close still comes 45 s after that first stop, and `completed_request_state(ident) == "closed"`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_read_ahead.py -q`
Expected: `ImportError: cannot import name 'READ_AHEAD_BYTES'` (then, once it exists, failures on `parse_row` / `read_ahead`).

- [ ] **Step 3: Implement `parse_row`** in `stream.py`: reuse the parser's own row reading (`_scan_object` for an object, `_decode(_ROW_DECODER, …)` otherwise) on the complete text with `eof=True`, checking only whitespace follows; `_decode` refuses a value at the very end of its input, so give it the text plus one delimiter (`text + ","`) and check the value ends where the text did. The test pins equality with the parser's rows.

- [ ] **Step 4: Add the poll** to `http.py`: `HttpChannel` keeps the socket `open_socket()` returns (`self._sock`, set before returning it; cleared in `close()`), and `readable()` / `QueryResponse.ready()` as in Interfaces and the rules above.

- [ ] **Step 5: Read ahead in `result.py`**: `Response.ready()`, `NOT_READY`, `EventStream.received` (add `len(chunk)` in `_read`), `EventStream.next_if_ready()` (share `next()`'s body: one private `_next(wait: bool)`), `row_cost`, `READ_AHEAD_STEP_CHUNKS`, and `CouchbaseResult.read_ahead()` / `held` / the held-row deque in `_next_row()`, `_rows()` and `close()` per the rules above. Module docstring: add one paragraph on the read-ahead (what is held, why only text, never waits).

- [ ] **Step 6: Turn it on** in `adapter.py`: `READ_AHEAD_BYTES` with its comment (8 MiB of row memory per paused result, counted by `sys.getsizeof`: a 20,000-row collection page-through (≈ 3 MB held) fits; the same cap as the `*` peek; spec §8 Q1 「預讀上限 8 MiB」) and `read_ahead_bytes=READ_AHEAD_BYTES` in the capabilities.

- [ ] **Step 7: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_read_ahead.py tests/test_couchbase_sessions.py tests/test_couchbase_result.py tests/test_couchbase_http.py tests/test_couchbase_stream.py -q`
Expected: all pass, none skipped. Then `cd backend && .venv/bin/pytest -q`: green.

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/http.py backend/dbaguy_backend/adapters/couchbase/stream.py \
  backend/dbaguy_backend/adapters/couchbase/result.py backend/dbaguy_backend/adapters/couchbase/adapter.py \
  backend/tests/couchbase_fakes.py backend/tests/test_couchbase_read_ahead.py backend/tests/test_couchbase_sessions.py
git commit -m "feat(couchbase): a paused result reads the server's rows ahead (8 MiB) without waiting; a result that fits is never closed"
```

---

### Task 4: Executed profile, parameter warnings, whole plan cells

**Files:**
- Modify: `backend/dbaguy_backend/adapters/couchbase/stream.py` (`FieldEvent.text`), `backend/dbaguy_backend/adapters/couchbase/result.py` (the profile result set, extra messages), `backend/dbaguy_backend/adapters/couchbase/adapter.py` (`run()`: profile and warnings; `executed_plan=True`)
- Create: `backend/dbaguy_backend/adapters/couchbase/params.py`
- Modify: `backend/dbaguy_backend/adapters/values.py`, `backend/dbaguy_backend/sessions/worker.py` (`_pump`)
- Create: `backend/tests/test_couchbase_params.py`, `backend/tests/test_couchbase_executed_plan.py`

**Interfaces:**
- Consumes: 04a's `QueryRequest.profile` (already in `build_body`), `RunOptions.profile_plan`, `StatementParams`, `rows.build_shape(..., plan=True)` (one `plan` column, `JsonText` of a row's text), `PLAN_COLUMN`.
- Produces:
  - `stream.FieldEvent(name: str, value: Any, text: str = "")` — `text` is the field value's exact text as sent.
  - `result.CouchbaseResult(stream, sql, *, plan, bare_star, dml, on_done, profile: bool = False, messages: Sequence[ServerMessage] = ())` and `start_result(..., profile: bool = False, messages: Sequence[ServerMessage] = ())`. With `profile`, once the rows are over (and no error is pending), `next_result()` returns True once and the result becomes a second set: `columns()` = `[ColumnMeta(name="plan", type_name="json", category=MIXED)]`, one row `[JsonText(<the profile field's text>)]` (`fetch_raw` gives that text), `presentation()` = `"plan"`, `read_ahead()` = `DONE`; the next `next_result()` is False. With `profile` and no `profile` field (Community Edition, or a server that sent none), no second set and one INFO message `"The server sent no execution profile (Community Edition does not give one)"`. `messages` are delivered by the first `take_messages()`.
  - `adapter.CouchbaseConnection.run()`: sends `profile: "timings"` when `options.profile_plan`, and passes `parameter_warnings(options.params)` as `messages`; `CouchbaseAdapter.capabilities.executed_plan = True` (ruling P6 of 04a lifted: A16 holds).
  - `params.changes_as_double(literal: str) -> bool` — the literal holds a number the query service reads as a float64 whose value then differs: an integer outside [−2⁶³, 2⁶³−1], or a decimal / exponent literal whose shortest float64 repr is a different value (`3.141592653589793238462643383279`, `1e400`, `1e-400`), not `1.10`, `0.1`, `1E+3`, `-0` (ruling B11; A26: the server returns `18446744073709552000` and `3.141592653589793` for those, `1.1` for `1.10`); numbers inside arrays and objects count; strings never.
  - `params.parameter_warnings(params: StatementParams | None) -> list[ServerMessage]` — one WARNING per parameter that `changes_as_double`, named `$name` or `$n` (positional, 1-based), text `f"Parameter {label}: the query service reads a number in it as a double, which changes its value — send it as a string to keep it exact"`; never the value (spec §4; ruling B10).
  - `values.PLAN_TEXT_LIMIT = 4 * 1024 * 1024`; `encode_value(value: Any, text_limit: int = TEXT_LIMIT) -> Any` and `encode_row(row: Any, text_limit: int = TEXT_LIMIT) -> list[Any]` — the limit applies where `TEXT_LIMIT` did (strings and `JsonText`).
  - `worker._pump`: a set whose `presentation()` is `"plan"` encodes its rows with `PLAN_TEXT_LIMIT` (read once when its `ResultSetEvent` goes out); every other set with `TEXT_LIMIT` as before.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_couchbase_params.py`:

```python
"""Parameters the query service would read as a double are flagged by name (R-04-10, spec §3.5.4), and typed literals
reach the server exactly (spec §1)."""

import pytest

from dbaguy_backend.adapters.couchbase.params import changes_as_double, parameter_warnings
from dbaguy_backend.domain.execution import ExecuteCommand, MessageSeverity, StatementParams
from tests.support import requires_couchbase
from tests.test_couchbase_sessions import cb_worker  # noqa: F401  (fixture)
from tests.test_sessions import finished


@pytest.mark.parametrize("literal, changes", [
    ("9007199254740993", False), ("9223372036854775807", False), ("-9223372036854775808", False), ("-0", False),
    ("9223372036854775808", True), ("18446744073709551615", True), ("1" + "0" * 5000, True),
    ("1.10", False), ("0.1", False), ("1E+3", False), ("-0.0", False),
    ("3.141592653589793238462643383279", True), ("1e400", True), ("1e-400", True),
    ('"18446744073709551615"', False), ('[1, {"a": 18446744073709551615}]', True), ("true", False), ("null", False),
])
def test_changes_as_double(literal, changes):
    assert changes_as_double(literal) is changes


def test_warnings_name_the_parameter_and_never_its_value():  # Review Focus 4
    params = StatementParams(named={"big": "18446744073709551615", "ok": "1"},
                             positional=["3.141592653589793238462643383279", '"x"'])
    warnings = parameter_warnings(params)
    assert [w.severity for w in warnings] == [MessageSeverity.WARNING, MessageSeverity.WARNING]
    assert warnings[0].text.startswith("Parameter $big:") and warnings[1].text.startswith("Parameter $1:")
    assert not any("18446744073709551615" in w.text or "3.14159" in w.text for w in warnings)
    assert parameter_warnings(None) == []


@requires_couchbase
def test_parameters_reach_the_server_exactly(cb_worker):  # spec §1: $n = 9007199254740993 → 9007199254740994
    worker, rec = cb_worker
    params = StatementParams(named={"n": "9007199254740993"}, positional=['"first"', "[1, 2]"])
    worker.submit(ExecuteCommand(request_id="p", sql="SELECT $n + 1 AS a1, @n AS a2, ? AS a3, $2 AS a4",
                                 params=params))
    rec.wait_for(finished("p"), timeout=30)
    assert rec.of("p", "rows")[0].rows == [[
        {"$type": "json", "text": "9007199254740994"}, {"$type": "json", "text": "9007199254740993"}, "first",
        {"$type": "json", "text": "[1,2]"},
    ]]
    assert not [m for m in rec.of("p", "message") if m.severity == MessageSeverity.WARNING]


@requires_couchbase
def test_a_number_the_server_reads_as_a_double_is_flagged(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="u", sql="SELECT $u AS u",
                                 params=StatementParams(named={"u": "18446744073709551615"})))
    rec.wait_for(finished("u"), timeout=30)
    assert rec.of("u", "rows")[0].rows == [[{"$type": "json", "text": "18446744073709552000"}]]  # A26
    warnings = [m.text for m in rec.of("u", "message") if m.severity == MessageSeverity.WARNING]
    assert len(warnings) == 1 and warnings[0].startswith("Parameter $u:") and "1844674" not in warnings[0]
```

Create `backend/tests/test_couchbase_executed_plan.py`:

```python
"""Run with profile (R-04-13, A16): the executed plan arrives after the rows as a `plan` result set; plan cells are
sent whole up to PLAN_TEXT_LIMIT."""

import json

from dbaguy_backend.adapters.couchbase.result import EventStream, start_result
from dbaguy_backend.adapters.couchbase.stream import FieldEvent, ResponseParser
from dbaguy_backend.adapters.values import PLAN_TEXT_LIMIT, JsonText, encode_row
from dbaguy_backend.domain.column_category import ColumnCategory
from dbaguy_backend.domain.execution import ColumnMeta, ExecuteCommand
from tests.couchbase_fakes import FakeResponse
from tests.fake_db import FakeConnection, FakeResult
from tests.support import requires_couchbase
from tests.test_couchbase_adapter import STREAM
from tests.test_couchbase_sessions import APP, cb_worker, page_through  # noqa: F401  (fixture)
from tests.test_session_worker import execute, make_worker
from tests.test_sessions import finished, paused

PROFILE = '{"phaseTimes":{"run":"1.10ms"},"executionTimings":{"#operator":"Sequence","~children":[]}}'
ORDERS_TOP3 = "SELECT o.amount FROM orders AS o WHERE o.amount > 400 ORDER BY o.amount DESC LIMIT 3"


def profiled(body: bytes, profile: bool = True):
    stream = EventStream(FakeResponse([body]), cancelled=lambda: False, node="n:8093")
    return start_result(stream, "SELECT a FROM ks", plan=False, bare_star=False, dml=False, on_done=lambda: None,
                        profile=profile)


def test_field_events_carry_their_exact_text():
    events = ResponseParser().feed(b'{"metrics":{"a": 1.10 },"status":"success"}')
    assert [(e.name, e.text) for e in events if isinstance(e, FieldEvent)][0] == ("metrics", '{"a": 1.10 }')


def test_the_profile_becomes_a_plan_result_set():
    body = ('{"signature":{"a":"json"},"results":[{"a":1}],"status":"success","metrics":{"resultCount":1},'
            f'"profile":{PROFILE}}}').encode()
    result = profiled(body)
    result.columns()
    assert result.fetch(10) == [[1]] and result.next_result() is True
    assert result.columns() == [ColumnMeta(name="plan", type_name="json", category=ColumnCategory.MIXED)]
    assert result.presentation() == "plan"
    assert result.fetch(10) == [[JsonText(PROFILE)]] and result.fetch(10) == []
    assert result.next_result() is False


def test_no_profile_in_the_answer_is_said_once():
    result = profiled(b'{"signature":{"a":"json"},"results":[{"a":1}],"status":"success"}')
    result.columns()
    result.fetch(10)
    assert result.next_result() is False
    assert [m.text for m in result.take_messages()].count(
        "The server sent no execution profile (Community Edition does not give one)") == 1


def test_a_plan_cell_is_sent_whole_up_to_its_limit():
    big = JsonText('{"t":"' + "x" * 100_000 + '"}')
    assert encode_row([big], PLAN_TEXT_LIMIT) == [{"$type": "json", "text": big.text}]
    assert encode_row([big])[0]["truncated"] is True  # any other set keeps the 64 KiB preview

    class BigPlan(FakeResult):
        def columns(self):
            return [ColumnMeta(name="plan", type_name="json", category=ColumnCategory.MIXED)]

        def fetch(self, n):
            rows, self._rows = ([[big]] if self._rows else []), []
            return rows

        def presentation(self):
            return "plan"

    class BigPlanConnection(FakeConnection):
        def statement(self, sql, options):
            super().statement(sql, options)
            return BigPlan(1)

    worker, _, rec = make_worker(BigPlanConnection())
    try:
        execute(worker, "x", "anything")
        rec.wait_for(finished("x"))
        assert rec.of("x", "rows")[0].rows == [[{"$type": "json", "text": big.text}]]
    finally:
        worker.close()


@requires_couchbase
def test_run_with_profile_adds_a_plan_result_set_after_the_rows(cb_worker):  # A16
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="p", sql=ORDERS_TOP3, query_context=APP, profile_plan=True))
    rec.wait_for(finished("p"), timeout=30)
    sets = rec.of("p", "result_set")
    assert [(s.index, s.presentation, [c.name for c in s.columns]) for s in sets] == [
        (0, "grid", ["amount"]), (1, "plan", ["plan"]),
    ]
    profile = json.loads(rec.of("p", "rows")[-1].rows[0][0]["text"])
    assert profile["executionTimings"]["#operator"] == "Authorize" and "phaseTimes" in profile


@requires_couchbase
def test_a_profile_follows_the_last_page(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="e", sql="SELECT META(d).id FROM events AS d", query_context=APP,
                                 page_size=5000, profile_plan=True))
    assert page_through(worker, rec, "e") == 20_001  # 20,000 rows and the profile's one
    assert [s.presentation for s in rec.of("e", "result_set")] == ["grid", "plan"]


@requires_couchbase
def test_a_result_closed_early_has_no_profile(cb_worker):
    worker, rec = cb_worker
    worker.submit(ExecuteCommand(request_id="s", sql=STREAM, page_size=500, profile_plan=True))
    rec.wait_for(paused("s", 500), timeout=30)
    worker.cancel("s")  # a discard
    rec.wait_for(finished("s"), timeout=10)
    assert [s.presentation for s in rec.of("s", "result_set")] == ["grid"]
```

(`page_through` counts every row event, so the profile's row adds one.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_params.py tests/test_couchbase_executed_plan.py -q`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.adapters.couchbase.params'`, then `ImportError` for `PLAN_TEXT_LIMIT`.

- [ ] **Step 3: Write `params.py`.** Number texts are collected with a decoder whose `parse_int` / `parse_float` / `parse_constant` record their argument; an integer text longer than 20 characters is out of range without calling `int()` (its digit limit); a decimal is `float(t)`, then compared as `Decimal(repr(f)) != Decimal(t)`, with `inf` counting as a change. Literal shape was checked by `sessions/options.check_params` before (a literal that does not decode here raises nothing: return False).

- [ ] **Step 4: Carry the profile.** `FieldEvent.text` (the parser's `s[i:end]` for a field value); in `result.py` keep the `profile` field's text and switch to the profile set in `next_result()` as in Interfaces (the set's shape is `build_shape(None, plan=True, bare_star=False, first_page=[])` over `RawRow(text, None, None)`); `messages` first in `_messages`. In `adapter.py`, `run()` passes `profile="timings"` into the request (through `_begin`) and `messages=parameter_warnings(options.params)`, `profile=options.profile_plan` into `start_result`; `executed_plan=True` with its comment updated (A16 holds: the `profile` field follows `metrics`).

- [ ] **Step 5: Plan cells whole.** `values.py` and `worker._pump` as in Interfaces.

- [ ] **Step 6: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_params.py tests/test_couchbase_executed_plan.py tests/test_couchbase_result.py tests/test_session_worker.py tests/test_run_options.py -q`
Expected: all pass. Then `cd backend && .venv/bin/pytest -q`: green. `npm run gen:api`: no change expected (capability values are not in the schema); if the schema changes, commit it.

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/adapters/couchbase/stream.py backend/dbaguy_backend/adapters/couchbase/result.py \
  backend/dbaguy_backend/adapters/couchbase/adapter.py backend/dbaguy_backend/adapters/couchbase/params.py \
  backend/dbaguy_backend/adapters/values.py backend/dbaguy_backend/sessions/worker.py \
  backend/tests/test_couchbase_params.py backend/tests/test_couchbase_executed_plan.py
git commit -m "feat(couchbase): run with profile (executed plan as a plan result set), number warnings for parameters, whole plan cells"
```

---
### Task 5: Plan analysis: operator tree, anti-pattern flags, advice, profile timings

**Files:**
- Create: `backend/dbaguy_backend/domain/couchbase.py`
- Create: `backend/dbaguy_backend/adapters/couchbase/explain.py`
- Create: `backend/tests/test_couchbase_explain.py`

**Interfaces:**
- Consumes (Task 1): the fixtures and `plan_fixture(name)`.
- Produces, in `domain/couchbase.py` (pydantic v2; output models, no `extra="forbid"` needed):

```python
PlanBadge = Literal["primary_scan", "sequential_scan", "intersect_scan", "not_covered", "slowest"]


class PlanDetail(BaseModel):
    label: str
    value: str


class PlanEstimates(BaseModel):  # EE cost-based optimizer
    cost: float | None = None
    cardinality: float | None = None


class PlanStats(BaseModel):  # a profile's `#stats`, durations as the server wrote them (Go: "34.611027ms", "10µs")
    items_in: int | None = None
    items_out: int | None = None
    exec_time: str | None = None
    serv_time: str | None = None
    kern_time: str | None = None


class PlanNode(BaseModel):
    id: int  # pre-order number over the whole view: the main tree first, then each subquery's
    operator: str
    title: str  # "IndexScan3 · idx_orders_amount", "Fetch · o", "Filter"
    details: list[PlanDetail] = []
    estimates: PlanEstimates | None = None
    stats: PlanStats | None = None
    badges: list[PlanBadge] = []
    children: list["PlanNode"] = []


class PlanFinding(BaseModel):
    badge: PlanBadge
    severity: Literal["warning", "info"]
    node_id: int
    text: str


class IndexAdvice(BaseModel):
    kind: Literal["recommended", "covering", "current"]
    statement: str  # `index_statement`, fully qualified: it can be copied and run as it is
    keyspace_alias: str | None = None
    rule: str | None = None  # `recommending_rule`; for a current index its `index_status`
    update_statistics: str | None = None


class PlanSubquery(BaseModel):
    text: str
    correlated: bool
    root: PlanNode


class PlanView(BaseModel):
    kind: Literal["plan", "profile", "advice", "unknown"]
    root: PlanNode | None = None
    subqueries: list[PlanSubquery] = []
    findings: list[PlanFinding] = []
    advice: list[IndexAdvice] = []
    advice_note: str | None = None  # the server's own words when it recommends nothing
    statement_text: str | None = None  # EXPLAIN's `text`, ADVISE's `query`
    estimates: PlanEstimates | None = None  # the whole plan's (EXPLAIN's top-level cost / cardinality)
    phase_times: list[PlanDetail] = []  # a profile's `phaseTimes`, in the server's order
```

- Produces, in `adapters/couchbase/explain.py` (pure, no I/O, no logging of plan content): `analyze(text: str) -> PlanView` (not JSON → `PlanView(kind="unknown")`), `analyze_value(value: Any) -> PlanView`, `go_duration_ns(text: str) -> int | None`.

The analysis rules (spec §3.10, survey §7, A25 shapes):
- Kind: an object with a `plan` object holding `#operator` → `plan` (`statement_text` = `text`, `estimates` from top-level `cost` / `cardinality`, `subqueries` from top-level `~subqueries[]` each `{subquery, correlated, plan}`); an object with `executionTimings` → `profile` (`root` = that tree, `phase_times` from `phaseTimes`); an object whose `#operator` is `"Advise"` → `advice` (`statement_text` = `query`); anything else → `unknown`.
- Children of an operator, in this order: `~children` (list), `~child`, `scans` (list), `first`, `second`, then any other key (not starting with `#`, not `optimizer_estimates`) whose value is an object with `#operator` or a list of such (tolerant of operators the survey did not see).
- Title: `operator` + ` · ` + `index` when present; else + ` · ` + (`as` or `alias` or `keyspace`) for `Fetch`, `Join`, `NestedLoopJoin`, `HashJoin`, `Nest`, `NestedLoopNest`, `Unnest`, `KeyScan`, `ExpressionScan`, `CountScan`; else the operator alone.
- Details: first, when present, `keyspace` as `bucket.scope.keyspace` (from `bucket`/`scope`/`keyspace`), `index`, `using`, `condition`, `covers` (joined by `, `), `spans`, `limit`, `offset`, `on_keys`, `expr`, `result_terms` (each term's `expr`, joined), `aggregates`, `group_keys`, `sort_terms`; then every other scalar key not starting with `#` / `~` and not `as`, `alias`, `namespace`, `index_id`, `bucket`, `scope`, `keyspace`, `optimizer_estimates`. A value that is not a string is written as compact JSON; any value longer than 300 characters ends with `…` at 300.
- `estimates` from `optimizer_estimates.{cost, cardinality}`; `stats` from `#stats.{#itemsIn, #itemsOut, execTime, servTime, kernTime}`.
- Findings, per node in pre-order, badges in the `PlanBadge` order:
  - `primary_scan` (warning): `PrimaryScan` / `PrimaryScan3` whose `index` is not `#sequentialscan` and `using` is not `sequentialscan` — text `f"Primary scan of {keyspace}: every document key is read; an index on the filtered fields would avoid it"`.
  - `sequential_scan` (warning): `PrimaryScan3` with `index == "#sequentialscan"` or `using == "sequentialscan"` — `f"Sequential scan of {keyspace}: no index is used, every document is read"`.
  - `intersect_scan` (warning): `IntersectScan` / `OrderedIntersectScan` — `"Several indexes are scanned and intersected; one composite index is usually faster"`.
  - `not_covered` (info): an index scan (`IndexScan`, `IndexScan2`, `IndexScan3`, `IntersectScan`, `OrderedIntersectScan`, `UnionScan`, `DistinctScan`) that has no `covers` (for the intersect / union kinds: none of its `scans` has `covers`), followed later in the same `~children` list — in a later sibling or anywhere below one — by a `Fetch` of the same alias (`as`, else `keyspace`) — `f"{title} does not cover the query: each match is fetched from the data service (expected for SELECT *)"`. Primary scans never get it.
  - `slowest` (info, profiles only): each operator whose own `execTime` + `servTime` is more than half the sum of those over every operator — `f"{title} took {share:.0f}% of the operators' time"`.
- Advice from `advice.adviseinfo` (an object, or a list of them): `current_indexes[]` → `current` (`rule` = `index_status`); `recommended_indexes` an object → `indexes[]` → `recommended`, `covering_indexes[]` → `covering`; a string → `advice_note`. Order: current, recommended, covering.
- `go_duration_ns`: Go's duration text, `[-]` then one or more `<number><unit>` with units `h`, `m`, `s`, `ms`, `us`, `µs` (U+00B5), `μs` (U+03BC), `ns`, the number with an optional fraction; the whole text must match; computed with `Decimal` and truncated toward zero to whole nanoseconds; None otherwise.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_couchbase_explain.py`:

```python
"""EXPLAIN / ADVISE / profile texts as a plan view (R-04-13, spec §3.10): operator trees from the dev cluster's own
answers (fixtures, 04b Task 1), the four anti-pattern flags and the profile's slowest operator."""

import json

import pytest

from dbaguy_backend.adapters.couchbase.explain import analyze, go_duration_ns
from dbaguy_backend.domain.couchbase import IndexAdvice, PlanNode, PlanView
from tests.support import plan_fixture


def walk(node: PlanNode):
    yield node
    for child in node.children:
        yield from walk(child)


def titles(node: PlanNode) -> list[str]:
    return [n.title for n in walk(node)]


def by_id(view: PlanView) -> dict[int, PlanNode]:
    roots = [view.root, *(s.root for s in view.subqueries)]
    return {n.id: n for root in roots if root for n in walk(root)}


def flags(view: PlanView) -> list[tuple[str, str, str]]:
    nodes = by_id(view)
    return [(f.badge, f.severity, nodes[f.node_id].title) for f in view.findings]


def test_a_covered_scan_is_clean():
    view = analyze(plan_fixture("explain_covered"))
    assert view.kind == "plan" and view.statement_text.startswith("SELECT o.amount FROM dbaguy.app.orders")
    assert titles(view.root) == ["Sequence", "IndexScan3 · idx_orders_amount", "Parallel", "Sequence", "Filter",
                                 "InitialProject"]
    scan = view.root.children[0]
    assert ("keyspace", "dbaguy.app.orders") in [(d.label, d.value) for d in scan.details]
    assert any(d.label == "covers" and "cover ((`o`.`amount`))" in d.value for d in scan.details)
    assert view.findings == []


def test_a_fetch_after_an_uncovering_scan_is_noted():
    assert flags(analyze(plan_fixture("explain_fetch"))) == [("not_covered", "info", "IndexScan3 · idx_orders_amount")]


def test_a_primary_scan_is_flagged():
    assert flags(analyze(plan_fixture("explain_primary"))) == [("primary_scan", "warning", "PrimaryScan3 · #primary")]


def test_a_sequential_scan_is_flagged():
    assert flags(analyze(plan_fixture("explain_sequential"))) == [
        ("sequential_scan", "warning", "PrimaryScan3 · #sequentialscan"),
    ]


def test_an_intersect_scan_is_flagged_with_its_scans_as_children():
    view = analyze(plan_fixture("explain_intersect"))
    assert flags(view) == [("intersect_scan", "warning", "IntersectScan"), ("not_covered", "info", "IntersectScan")]
    intersect = next(n for n in walk(view.root) if n.operator == "IntersectScan")
    assert [c.title for c in intersect.children] == ["IndexScan3 · def_inventory_airport_faa",
                                                     "IndexScan3 · def_inventory_airport_city"]


def test_a_join_carries_estimates():
    view = analyze(plan_fixture("explain_join"))
    assert view.estimates is not None and view.estimates.cost > 0
    join = next(n for n in walk(view.root) if n.operator == "Join")
    assert join.title == "Join · a" and any(d.label == "on_keys" for d in join.details)
    assert join.estimates is not None and join.estimates.cardinality is not None


def test_subqueries_get_their_own_trees_and_ids_never_repeat():
    view = analyze(plan_fixture("explain_subquery"))
    assert len(view.subqueries) == 1 and view.subqueries[0].correlated is False
    assert titles(view.subqueries[0].root)[:2] == ["Sequence", "CountScan · users"]
    ids = [n.id for root in (view.root, view.subqueries[0].root) for n in walk(root)]
    assert ids == list(range(len(ids)))


def test_advice_lists_current_then_recommended_then_covering():
    view = analyze(plan_fixture("advise_covering"))
    assert view.kind == "advice" and view.statement_text.startswith("SELECT a.name FROM `travel-sample`")
    assert [a.kind for a in view.advice] == ["current", "recommended", "covering"]
    assert view.advice[1].statement.startswith("CREATE INDEX adv_country_geo_alt ON ")
    assert view.advice[1].rule.startswith("Index keys follow order of predicate types")
    simple = analyze(plan_fixture("advise_indexes"))
    assert simple.advice[0] == IndexAdvice(
        kind="current", keyspace_alias="orders_o",
        statement="CREATE INDEX idx_orders_amount ON `default`:`dbaguy`.`app`.`orders`(`amount` DESC)")


def test_no_recommendation_keeps_the_servers_words():
    view = analyze(plan_fixture("advise_none"))
    assert view.advice_note == "No index recommendation at this time."
    assert view.advice[0].rule == "THIS IS AN OPTIMAL COVERING INDEX."


@pytest.mark.parametrize("name, slowest", [("profile_timings", "IndexScan3 · idx_orders_amount"),
                                          ("profile_join", None)])
def test_a_profile_has_stats_and_names_a_dominant_operator(name, slowest):
    view = analyze(plan_fixture(name))
    assert view.kind == "profile" and view.root.operator == "Authorize" and view.phase_times
    timed = [n for n in walk(view.root) if n.stats is not None]
    assert timed and all(go_duration_ns(n.stats.exec_time) is not None for n in timed if n.stats.exec_time)
    found = [title for badge, _, title in flags(view) if badge == "slowest"]
    if slowest is not None:  # the index scan's service time dominates this tiny query
        assert found == [slowest]
    else:  # a join spreads its time: at most one operator may hold more than half
        assert len(found) <= 1


def test_unknown_operators_are_still_walked():
    text = json.dumps({"plan": {"#operator": "Sequence", "~children": [
        {"#operator": "UnionScan", "scans": [{"#operator": "IndexScan3", "index": "a", "covers": ["c"]}]},
        {"#operator": "NestedLoopJoin", "alias": "b", "~child": {"#operator": "IndexScan3", "index": "b"}},
        {"#operator": "Future", "inner": {"#operator": "Deeper"}, "#stats": {"#operator": "not a child"}},
    ]}, "text": "SELECT 1"})
    assert titles(analyze(text).root) == ["Sequence", "UnionScan", "IndexScan3 · a", "NestedLoopJoin · b",
                                          "IndexScan3 · b", "Future", "Deeper"]


def test_a_fetch_of_another_alias_is_not_an_uncovered_scan():
    text = json.dumps({"plan": {"#operator": "Sequence", "~children": [
        {"#operator": "IndexScan3", "index": "i", "as": "a", "keyspace": "k"},
        {"#operator": "Fetch", "as": "b", "keyspace": "k2"},
    ]}, "text": "x"})
    assert analyze(text).findings == []


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"a": 1}', "null"])
def test_anything_else_is_unknown(text):
    assert analyze(text).kind == "unknown"


@pytest.mark.parametrize("text, ns", [
    ("1h2m3.5s", 3_723_500_000_000), ("34.611027ms", 34_611_027), ("10µs", 10_000), ("10μs", 10_000),
    ("4.792us", 4_792), ("0s", 0), ("1.9ns", 1), ("-2ms", -2_000_000), ("1m0.001s", 60_001_000_000),
    ("", None), ("5", None), ("abc", None), ("1x", None), ("ms", None),
])
def test_go_duration_ns(text, ns):
    assert go_duration_ns(text) == ns
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_explain.py -q`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.adapters.couchbase.explain'`.

- [ ] **Step 3: Write `domain/couchbase.py`** (the models above, with a module docstring: API models of the Couchbase features) and **`explain.py`** following the rules. The one non-obvious part is `not_covered`; write it as:

```python
_INDEX_SCANS = frozenset({"IndexScan", "IndexScan2", "IndexScan3", "IntersectScan", "OrderedIntersectScan",
                          "UnionScan", "DistinctScan"})


def _covered(op: dict[str, Any]) -> bool:
    if op.get("covers"):
        return True
    return any(_covered(scan) for scan in op.get("scans") or () if isinstance(scan, dict))


def _alias(op: dict[str, Any]) -> str | None:
    return op.get("as") or op.get("keyspace")


def _fetches(op: dict[str, Any], alias: str) -> bool:
    """`op` or any operator below it fetches `alias`."""
    if op.get("#operator") == "Fetch" and _alias(op) == alias:
        return True
    return any(_fetches(child, alias) for child in _children(op))


def _uncovered_scans(sequence: list[dict[str, Any]]) -> set[int]:
    """Python ids of the index scans in one `~children` list that a later sibling (or something below it) fetches for."""
    found: set[int] = set()
    for position, op in enumerate(sequence):
        if op.get("#operator") not in _INDEX_SCANS or _covered(op):
            continue
        # an IntersectScan / UnionScan names no alias itself: its scans do
        alias = _alias(op) or next((_alias(s) for s in op.get("scans") or () if isinstance(s, dict) and _alias(s)),
                                   None)
        if alias is not None and any(_fetches(later, alias) for later in sequence[position + 1:]):
            found.add(id(op))
    return found
```

(`_children(op)` implements the children rule; the builder collects `_uncovered_scans` of every `~children` list it meets and badges those operators.) The `slowest` rule sums `go_duration_ns(execTime) + go_duration_ns(servTime)` (missing = 0) per operator; with a zero total nothing is slowest.

- [ ] **Step 4: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_explain.py -q`
Expected: all pass. If a fixture-based expectation differs because the captured plan differs (the optimizer chose otherwise), re-read the fixture and adjust only that test's expected titles, never the rules, and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add backend/dbaguy_backend/domain/couchbase.py backend/dbaguy_backend/adapters/couchbase/explain.py \
  backend/tests/test_couchbase_explain.py
git commit -m "feat(couchbase): plan analysis — operator tree, primary/sequential/intersect/uncovered flags, advice, profile timings"
```

---

### Task 6: Couchbase API: query contexts, plan, advice, history and workspace contexts, browse statement, ⌘P

**Files:**
- Modify: `backend/dbaguy_backend/domain/couchbase.py` (`ContextOption`), `backend/dbaguy_backend/domain/history.py`, `backend/dbaguy_backend/domain/workspace.py`, `backend/dbaguy_backend/domain/schema.py`
- Create: `backend/dbaguy_backend/services/couchbase_service.py`, `backend/dbaguy_backend/api/couchbase.py`
- Modify: `backend/dbaguy_backend/adapters/couchbase/adapter.py` (`query(context=…)`, `relation_path`), `backend/dbaguy_backend/services/schema_cache.py` (`search` over keyspaces), `backend/dbaguy_backend/sql/select_builder.py`, `backend/dbaguy_backend/api/sql.py` (describe), `backend/dbaguy_backend/api/history.py`, `backend/dbaguy_backend/app.py`, `backend/dbaguy_backend/container.py`
- Modify: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts` (generated), `src/lib/api/types.ts`
- Create: `backend/tests/test_couchbase_api.py`
- Modify: `backend/tests/sql/test_select_builder.py`, `backend/tests/test_local_stores.py`

**Interfaces:**
- Consumes: Task 5's `PlanView` / `analyze` / `analyze_value`; 04a's `QueryContext` (`text()`, `parse()`), `SchemaSnapshot.keyspaces`, `MetadataService.with_connection`, `SchemaCache.get`.
- Produces:
  - `domain/couchbase.ContextOption(bucket: str, scope: str | None = None, text: str, listed: bool = True)` — `text` is `QueryContext(bucket, scope).text()`; `listed` False marks the tab's current context when the snapshot does not list it.
  - `services/couchbase_service.CouchbaseService(registry: AdapterRegistry, profiles: ProfileService, schema_cache: SchemaCache, metadata: MetadataService)` with `require(profile_id: UUID) -> None` (`ValidationFailed("This is not a Couchbase connection")` unless the engine's capabilities take a query context), `contexts(profile_id: UUID, current: QueryContext | None) -> list[ContextOption]`, `advise(profile_id: UUID, statement: str, context: QueryContext | None) -> PlanView`, `with_query_contexts(page: HistoryPage) -> HistoryPage`; on the container as `container.couchbase`.
  - `GET /couchbase/contexts?profile_id=…[&bucket=…[&scope=…]]` → `list[ContextOption]`: the current context (bucket, scope) first with `listed=False` when absent from the snapshot; then per bucket (by name): the bucket alone, its `_default` scope, its other scopes by name, system scopes last.
  - `POST /couchbase/plan {text: str (≤ PLAN_TEXT_LIMIT characters)}` → `PlanView` (no profile: pure).
  - `POST /couchbase/advise {profile_id, statement: str (1 ≤ len ≤ 1,048,576), query_context: QueryContext | None = None}` → `PlanView`: runs `"ADVISE " + statement` on the profile's metadata connection (read-only like every internal query) in that context, and analyses the first row (`kind="unknown"` without one). A server error (3230 on CE, 5001 on `app.users`, A15) comes back as the usual `QueryFailed` error with the server's text.
  - `CouchbaseConnection.query(self, statement: str, named: Mapping[str, str] | None = None, *, context: QueryContext | None = None) -> list[Any]` — `context` goes to `_begin(context=…)`; the request keeps `deadline=_INTERNAL_DEADLINE` (R-04a-F3).
  - `CouchbaseAdapter.relation_path(database, schema, name, kind)` → `[bucket:database, scope:schema, collection:name]` for `kind == "collection"` (else the default).
  - `domain/schema.RelationKind` gains `"collection"`; `SchemaCache.search()` also matches `snapshot.keyspaces`: the needle against the collection name (same ranks as relations), the part before the last dot (when given) as a prefix of the scope or of `bucket.scope` (case-insensitive); hits `ObjectSearchHit(database=bucket, schema_name=scope, name=collection, kind="collection", path=adapter.relation_path(bucket, scope, collection, "collection"))`, sorted by (rank, system, length, name) together with relation hits (ruling B20).
  - `domain/history.HistoryEntry.query_context: QueryContext | None = None` — filled by `GET /history` through `CouchbaseService.with_query_contexts` for entries of existing profiles whose engine takes a query context (`QueryContext.parse(entry.database)`; a malformed text gives None); never stored.
  - `domain/workspace.WorkspaceTab.query_context: QueryContext | Literal["none"] | None = None` (None: the profile's default context; `"none"`: the user chose No context) and `params: dict[ParamName, ParamDraft] | None = Field(default=None, max_length=400)` with `ParamName = Annotated[str, Field(pattern=r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|[1-9][0-9]{0,2})$")]`, `ParamDraft = Annotated[str, Field(max_length=1_048_576)]` (the panel's text as typed, valid JSON or not; ruling B14).
  - `sql/select_builder.build_browse_select(Dialect.SQLPP, path, None, [], False)` → `"SELECT META(d).id, *\nFROM " + quote_ident(Dialect.SQLPP, collection) + " AS d"` for a path holding a `collection` segment (an index below it too); a filter, a sort or `count` for SQL++ → `ValidationFailed("Filtering and sorting a collection is done in SQL++ in a query tab")`; no collection segment → the existing "no data to browse" error.
  - `POST /sql/describe` → `null` for an SQL++ profile (spec §3.12).
  - `src/lib/api/types.ts` exports `ContextOption`, `PlanView`, `PlanNode`, `PlanFinding`, `IndexAdvice`, `PlanDetail`, `PlanSubquery`, `StatementParams`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_couchbase_api.py`:

```python
"""The Couchbase endpoints over a real dev cluster: query contexts for the picker, plan analysis, index advice on the
metadata connection, history entries' contexts, ⌘P over keyspaces."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dbaguy_backend.app import create_app
from dbaguy_backend.config import Settings
from dbaguy_backend.container import AppContainer
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.history import HistoryEntry, HistoryPage
from dbaguy_backend.storage.secret_store import InMemorySecretStore
from tests.support import COUCHBASE_PASSWORD, plan_fixture, requires_couchbase
from tests.test_api import AUTH, BASE_URL, TOKEN

COUCHBASE = {"engine": "couchbase", "name": "cb", "host": "127.0.0.1", "database": "dbaguy.app",
             "username": "dbaguy", "ssl": {"mode": "disable"}, "save_password": False}


@pytest.fixture
def app(tmp_path):
    settings = Settings(token=TOKEN, data_dir=tmp_path / "data", log_to_file=False)
    container = AppContainer(settings, secret_store=InMemorySecretStore())
    with TestClient(create_app(settings, container), base_url=BASE_URL) as client:
        yield client, container


@pytest.fixture
def cb(app):
    client, _ = app
    body = {"profile": COUCHBASE, "secrets": {"password": COUCHBASE_PASSWORD}}
    return client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]


def entry(profile_id, database, name="cb"):
    return HistoryEntry(id=1, profile_id=profile_id, profile_name=name, database=database, sql="SELECT 1",
                        started_at=datetime.now(UTC), elapsed_ms=1, rows=1, status="ok")


@requires_couchbase
def test_contexts_list_buckets_and_scopes_with_their_texts(app, cb):
    client, _ = app
    options = client.get("/couchbase/contexts", params={"profile_id": cb}, headers=AUTH).json()
    pairs = [(o["bucket"], o["scope"]) for o in options]
    for pair in [("dbaguy", None), ("dbaguy", "_default"), ("dbaguy", "app"), ("travel-sample", "inventory")]:
        assert pair in pairs
    assert pairs.index(("dbaguy", None)) < pairs.index(("dbaguy", "_default")) < pairs.index(("dbaguy", "app"))
    texts = {(o["bucket"], o["scope"]): o["text"] for o in options}
    assert texts[("travel-sample", "inventory")] == "travel-sample.inventory" and all(o["listed"] for o in options)


@requires_couchbase
def test_the_current_context_is_listed_even_when_the_snapshot_lacks_it(app, cb):  # Review Focus 5
    client, _ = app
    options = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "my.bucket", "scope": "app"},
                         headers=AUTH).json()
    assert options[0] == {"bucket": "my.bucket", "scope": "app", "text": "my.bucket.app", "listed": False}
    alone = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "a`b"}, headers=AUTH).json()[0]
    assert alone["text"] == QueryContext(bucket="a`b").text() and QueryContext.parse(alone["text"]).bucket == "a`b"
    listed = client.get("/couchbase/contexts", params={"profile_id": cb, "bucket": "dbaguy", "scope": "app"},
                        headers=AUTH).json()
    assert [o for o in listed if (o["bucket"], o["scope"]) == ("dbaguy", "app")] == [
        {"bucket": "dbaguy", "scope": "app", "text": "dbaguy.app", "listed": True}]


def test_contexts_refuse_other_engines(app, tmp_path):
    client, _ = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    response = client.get("/couchbase/contexts", params={"profile_id": sqlite}, headers=AUTH)
    assert response.status_code == 422 and "not a Couchbase connection" in response.json()["error"]["message"]


def test_plan_analyses_a_text(app):
    client, _ = app
    view = client.post("/couchbase/plan", json={"text": plan_fixture("explain_fetch")}, headers=AUTH).json()
    assert view["kind"] == "plan" and [f["badge"] for f in view["findings"]] == ["not_covered"]


@requires_couchbase
def test_advise_runs_on_the_metadata_connection_in_the_given_context(app, cb):
    client, _ = app
    body = {"profile_id": cb, "statement": 'SELECT * FROM orders WHERE customer = "c1"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    view = client.post("/couchbase/advise", json=body, headers=AUTH).json()
    assert view["kind"] == "advice"
    assert any(a["kind"] == "recommended" and "adv_customer" in a["statement"] for a in view["advice"])


@requires_couchbase
def test_advise_shows_the_servers_error(app, cb):  # A15: ADVISE on app.users panics on 7.6.12
    client, _ = app
    body = {"profile_id": cb, "statement": 'SELECT * FROM users WHERE name = "Ann"',
            "query_context": {"bucket": "dbaguy", "scope": "app"}}
    response = client.post("/couchbase/advise", json=body, headers=AUTH)
    assert response.status_code >= 400 and "Panic" in response.json()["error"]["message"]


def test_history_entries_carry_their_parsed_context(app, cb, tmp_path):  # Review Focus 5
    client, container = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    page = HistoryPage(items=[entry(cb, "travel-sample.inventory"), entry(cb, "`my.bucket`"), entry(cb, None),
                              entry(cb, "a..b"), entry(sqlite, "main", "s"), entry(uuid4(), "dbaguy.app", "gone")],
                       next_before=None)
    contexts = [e.query_context for e in container.couchbase.with_query_contexts(page).items]
    assert contexts == [QueryContext(bucket="travel-sample", scope="inventory"), QueryContext(bucket="my.bucket"),
                        None, None, None, None]


@requires_couchbase
def test_quick_open_finds_collections_with_their_tree_path(app, cb):
    client, _ = app

    def search(q):
        return client.get("/metadata/search", params={"profile_id": cb, "q": q}, headers=AUTH).json()

    hit = search("airl")[0]
    assert (hit["name"], hit["kind"], hit["database"], hit["schema_name"]) == ("airline", "collection",
                                                                                "travel-sample", "inventory")
    assert [(s["kind"], s["name"]) for s in hit["path"]] == [("bucket", "travel-sample"), ("scope", "inventory"),
                                                             ("collection", "airline")]
    assert search("inventory.airl")[0]["name"] == "airline"
    assert search("travel-sample.inventory.airl")[0]["name"] == "airline"
    assert search("app.airl") == []
    assert [h["name"] for h in search("many.c00")][:3] == ["c001", "c002", "c003"]


@requires_couchbase
def test_describe_is_not_offered_for_sqlpp(app, cb):
    client, _ = app
    body = {"profile_id": cb, "text": "SELECT * FROM users", "cursor": 16}
    response = client.post("/sql/describe", json=body, headers=AUTH)
    assert response.status_code == 200 and response.json() is None
```

Append to `backend/tests/sql/test_select_builder.py`:

```python
def test_sqlpp_browse_select_reads_the_collection_with_its_meta_id():  # D-04-11, Review Focus 5
    def path(*parts):
        kinds = ("bucket", "scope", "collection", "index")
        return [PathSegment(kind=kind, name=name) for kind, name in zip(kinds, parts)]

    assert build_browse_select(Dialect.SQLPP, path("travel-sample", "inventory", "airline"), None, []) == (
        "SELECT META(d).id, *\nFROM airline AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "value"), None, []).endswith("FROM `value` AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "odd-name"), None, []).endswith(
        "FROM `odd-name` AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "_default", "_default"), None, []).endswith(
        "FROM _default AS d")
    assert build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "orders", "#primary"), None, []).endswith(
        "FROM orders AS d")
    for where, order, count in [("x = 1", [], False), (None, [SortKey(column="x")], False), (None, [], True)]:
        with pytest.raises(ValidationFailed, match="SQL\\+\\+ in a query tab"):
            build_browse_select(Dialect.SQLPP, path("dbaguy", "app", "orders"), where, order, count)
```

(import `PathSegment`, `SortKey`, `ValidationFailed`, `Dialect` there if missing). Append to `backend/tests/test_local_stores.py`:

```python
def test_workspace_keeps_the_tab_context_and_params(tmp_path):  # Review Focus 5
    service = WorkspaceService(tmp_path / "workspace.json")
    state = WorkspaceState(tabs=[
        WorkspaceTab(id="a", kind="query", query_context=QueryContext(bucket="my.bucket", scope="app"),
                     params={"$n": "9007199254740993", "$1": '"x"', "$x": "not json yet"}),
        WorkspaceTab(id="b", kind="query", query_context="none"),
        WorkspaceTab(id="c", kind="query"),
    ])
    service.put(state)
    assert service.get() == state
    with pytest.raises(ValidationError):
        WorkspaceTab(id="d", kind="query", params={"n": "1"})  # a draft is keyed by `$name` or `$n`
    with pytest.raises(ValidationError):
        WorkspaceTab(id="d", kind="query", query_context="all")
```

(`WorkspaceService(path).put(state)` / `.get()` are the file's existing API; import `QueryContext`, `WorkspaceTab`, `WorkspaceState`, `pydantic.ValidationError`.)

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_api.py tests/sql/test_select_builder.py tests/test_local_stores.py -q`
Expected: 404s on `/couchbase/*`, `ValidationError` on the new workspace fields, the SQL++ browse assertion failing.

- [ ] **Step 3: Models.** `ContextOption`; `HistoryEntry.query_context`; `WorkspaceTab.query_context` / `params`; `RelationKind` + `"collection"`.

- [ ] **Step 4: Service and router.** `services/couchbase_service.py` per Interfaces (contexts from `schema_cache.get(profile_id).keyspaces`; `with_query_contexts` asks the registry once per profile id in the page and treats a missing profile as "no context"); `api/couchbase.py` (router prefix `/couchbase`, tag `couchbase`, `require_token`; request models `PlanRequest`, `AdviseRequest` with `extra="forbid"`); register it in `app.py`; build `container.couchbase` in `container.py`; `api/history.py` passes its page through `container.couchbase.with_query_contexts`. Logging: DEBUG on advise (profile id, statement length — never the statement above DEBUG).

- [ ] **Step 5: Adapter, search, browse, describe.** `CouchbaseConnection.query(..., context=…)` (passed to `_begin` as the request's context); `CouchbaseAdapter.relation_path`; `SchemaCache.search` over keyspaces (system keyspaces after the others); `build_browse_select` SQL++ branch (`relation_from_path` stays for the relational kinds; the SQL++ branch reads the `collection` segment); `api/sql.describe` returns None when the profile's dialect is `Dialect.SQLPP`.

- [ ] **Step 6: Generate the types**

Run: `npm run gen:api && npm run typecheck && npm run build`
Expected: the schema gains the new models and endpoints; typecheck passes (add the exports to `src/lib/api/types.ts`; `SavedTab` gains optional `query_context` / `params`, `HistoryEntry` gains `query_context`, `ObjectSearchHit.kind` gains `"collection"` — nothing reads them yet).

- [ ] **Step 7: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_api.py tests/sql/test_select_builder.py tests/test_local_stores.py tests/test_schema_cache.py tests/test_api.py -q`, then `cd backend && .venv/bin/pytest -q`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/domain/couchbase.py backend/dbaguy_backend/domain/history.py \
  backend/dbaguy_backend/domain/workspace.py backend/dbaguy_backend/domain/schema.py \
  backend/dbaguy_backend/services/couchbase_service.py backend/dbaguy_backend/api/couchbase.py \
  backend/dbaguy_backend/adapters/couchbase/adapter.py backend/dbaguy_backend/services/schema_cache.py \
  backend/dbaguy_backend/sql/select_builder.py backend/dbaguy_backend/api/sql.py backend/dbaguy_backend/api/history.py \
  backend/dbaguy_backend/app.py backend/dbaguy_backend/container.py backend/tests/test_couchbase_api.py \
  backend/tests/sql/test_select_builder.py backend/tests/test_local_stores.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts
git commit -m "feat(couchbase): /couchbase contexts, plan and advise; history and workspace keep the tab's context; browse statement; ⌘P finds collections"
```

---
### Task 7: Field sampling: field paths from a manual sample, cached per keyspace

**Files:**
- Modify: `backend/dbaguy_backend/domain/couchbase.py` (`Keyspace`, `FieldPath`, `FieldSample`), `backend/dbaguy_backend/domain/settings.py`, `backend/dbaguy_backend/adapters/base.py` (`FieldSampling`)
- Create: `backend/dbaguy_backend/adapters/couchbase/sampling.py`, `backend/dbaguy_backend/services/field_sample_service.py`
- Modify: `backend/dbaguy_backend/adapters/couchbase/adapter.py` (`sample_fields`), `backend/dbaguy_backend/api/couchbase.py`, `backend/dbaguy_backend/api/profiles.py`, `backend/dbaguy_backend/container.py`
- Modify: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts` (generated), `src/lib/api/types.ts`, `src/stores/settings.ts` (`DEFAULT_SETTINGS.couchbase`)
- Create: `backend/tests/test_couchbase_sampling.py`

**Interfaces:**
- Consumes: Task 6's `api/couchbase.py`, `CouchbaseService.require`, the `app` / `cb` fixtures of `tests/test_couchbase_api.py`; 04a's `MetadataService.with_connection`, `SettingsService.get()`, `stream.split_object`, `dialects.keyspace_path`.
- Produces, in `domain/couchbase.py`:

```python
class Keyspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # frozen: a cache key

    bucket: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=251)
    collection: str = Field(min_length=1, max_length=251)


class FieldPath(BaseModel):
    path: list[str | None]  # keys from the document's root; None: an element of an array; []: the document itself
    types: dict[str, int]  # JSON kind ("binary" for a binary document) → documents in which the path has that kind
    docs: int  # documents in which the path occurs
    percent: float  # docs / documents sampled × 100, one decimal


class FieldSample(BaseModel):
    keyspace: Keyspace
    sampled_at: datetime
    requested: int  # the sample size asked for
    documents: int  # documents read
    cut_at_bytes: bool  # stopped at SAMPLE_MAX_BYTES before `requested` documents
    paths_capped: bool  # more than FIELD_PATHS_MAX paths: the later ones are left out
    elapsed_ms: int
    fields: list[FieldPath]
```

- Produces, in `domain/settings.py`: `class CouchbaseSettings(BaseModel)` (`extra="forbid"`) with `sample_size: int = Field(default=1000, ge=10, le=10_000)`; `AppSettings.couchbase: CouchbaseSettings = CouchbaseSettings()`.
- Produces, in `adapters/base.py`: `class FieldSampling(ABC)` with `@abstractmethod sample_fields(self, keyspace: Keyspace, size: int) -> FieldSample` — a connection whose engine can read sample documents; `CouchbaseConnection` implements it.
- Produces, in `adapters/couchbase/sampling.py` (pure): `SAMPLE_MAX_BYTES = 32 * 1024 * 1024`, `FIELD_PATH_MAX_DEPTH = 6`, `FIELD_PATHS_MAX = 2000`; `sample_statement(keyspace: Keyspace) -> str` = `f"SELECT META(d).type AS t, d FROM {keyspace_path(bucket, scope, collection)} AS d LIMIT $n"` (ruling B16: `META(d).type` tells a binary document from a string one, A24); `class FieldCollector` with `add(self, meta_type: str, document_text: str) -> None`, `documents: int`, `paths_capped: bool`, `fields(self) -> list[FieldPath]`.
- Produces: `CouchbaseConnection.sample_fields(keyspace, size)` — sends `sample_statement` with `$n` = `str(size)`, read-only and with `deadline=_INTERNAL_DEADLINE` like every internal query (R-04a-F3), reads the raw rows in batches of 200 with `fetch_raw`, splits each row with `split_object` into `t` (a string) and `d`'s text, feeds `FieldCollector`, stops once the row texts read pass `sampling.SAMPLE_MAX_BYTES` (read at call time) and closes the result (which abandons the rest: the server stops at once), and returns `FieldSample` (`sampled_at` = now in UTC).
- Produces, in `services/field_sample_service.py`: `FieldSampleService(metadata: MetadataService, settings: SettingsService)` with `sample(profile_id: UUID, keyspace: Keyspace, size: int | None) -> FieldSample` (None: `settings.get().couchbase.sample_size`; runs on the profile's metadata connection; a connection that is not `FieldSampling` → `ValidationFailed("This connection cannot sample documents")`; caches the result; INFO `"Sampled {} documents of {}.{}.{} in {} ms ({} field paths)"`), `cached(profile_id: UUID, keyspace: Keyspace) -> FieldSample | None`, `invalidate(profile_id: UUID) -> None`; on the container as `container.field_samples`; `api/profiles._forget_connections_and_schema` also calls `container.field_samples.invalidate(profile_id)`.
- Produces, in `api/couchbase.py`: `POST /couchbase/fields/sample {profile_id, keyspace: Keyspace, size: int | None (10–10,000)}` → `FieldSample`; `GET /couchbase/fields?profile_id&bucket&scope&collection` → `FieldSample | None`. Both call `container.couchbase.require(profile_id)` first.
- Produces, in `src/lib/api/types.ts`: `Keyspace`, `FieldPath`, `FieldSample`.

`FieldCollector` rules (the tests pin them): each document is decoded with numbers never turned into Python numbers (`parse_int` / `parse_float` / `parse_constant` return one sentinel: `1e400` and a 5,000-digit integer are just "number"); a `base64` document counts as kind `binary` at the root and nothing below; the walk records `(path, kind)` once per document — objects go down into their keys, arrays into `None` (their elements), never deeper than `FIELD_PATH_MAX_DEPTH` segments; per path, `docs` counts the documents that have it and `types[kind]` the documents where it has that kind; paths keep their first-seen order; a new path past `FIELD_PATHS_MAX` is dropped and sets `paths_capped`; `percent = round(docs / documents * 100, 1)`.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/test_couchbase_sampling.py`:

```python
"""Sample fields (R-04-11): field paths derived in Python from documents read with a plain SELECT, never INFER; binary
documents count as binary; the sample stops at 32 MiB; results are cached per profile and keyspace."""

from dbaguy_backend.adapters.couchbase import sampling
from dbaguy_backend.adapters.couchbase.sampling import FieldCollector, sample_statement
from dbaguy_backend.domain.couchbase import Keyspace
from tests.support import couchbase_query, requires_couchbase
from tests.test_api import AUTH
from tests.test_couchbase_adapter import open_couchbase
from tests.test_couchbase_api import COUCHBASE, app, cb  # noqa: F401  (fixtures)

USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")


def collect(*documents: tuple[str, str]) -> FieldCollector:
    collector = FieldCollector()
    for meta_type, text in documents:
        collector.add(meta_type, text)
    return collector


def by_path(collector: FieldCollector):
    return {tuple(f.path): f for f in collector.fields()}


def test_paths_types_and_shares():
    collector = collect(
        ("json", '{"name":"Ann","age":31,"tags":["a","b"],"address":{"city":"Taipei","zip":"100"}}'),
        ("json", '{"name":"Bob","age":null,"address":{"city":"Tainan"}}'),
        ("json", '"just a string document"'),
        ("base64", '"<binary (9 b)>"'),
    )
    fields = by_path(collector)
    assert collector.documents == 4
    assert fields[()].types == {"object": 2, "string": 1, "binary": 1} and fields[()].docs == 4
    age = fields[("age",)]
    assert (age.types, age.docs, age.percent) == ({"number": 1, "null": 1}, 2, 50.0)
    assert fields[("tags", None)].types == {"string": 1} and fields[("tags", None)].docs == 1  # once per document
    assert fields[("address", "zip")].percent == 25.0
    assert list(fields)[:5] == [(), ("name",), ("age",), ("tags",), ("tags", None)]  # first-seen order


def test_array_elements_and_the_depth_cap():
    deep = '{"a":{"b":{"c":{"d":{"e":{"f":{"g":1}}}}}}}'
    collector = collect(("json", '{"items":[{"sku":1},{"sku":2,"qty":3}]}'), ("json", deep))
    paths = [tuple(f.path) for f in collector.fields()]
    assert ("items", None, "sku") in paths and ("items", None, "qty") in paths
    assert max(len(p) for p in paths) == 6 and ("a", "b", "c", "d", "e", "f") in paths
    assert not collector.paths_capped


def test_the_path_cap(monkeypatch):
    monkeypatch.setattr(sampling, "FIELD_PATHS_MAX", 5)
    collector = collect(("json", '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6}'))
    assert len(collector.fields()) == 5 and collector.paths_capped


def test_numbers_are_never_parsed():
    fields = by_path(collect(("json", '{"huge":1e400,"big":' + "9" * 5000 + ',"pi":3.14}')))
    assert fields[("huge",)].types == fields[("big",)].types == {"number": 1}


def test_the_sample_statement_quotes_names():
    assert sample_statement(Keyspace(bucket="travel-sample", scope="inventory", collection="airline")) == (
        "SELECT META(d).type AS t, d FROM `travel-sample`.inventory.airline AS d LIMIT $n")
    assert sample_statement(Keyspace(bucket="dbaguy", scope="app", collection="value")).endswith(
        "dbaguy.app.`value` AS d LIMIT $n")


@requires_couchbase
def test_sampling_users_sees_every_shape():
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(USERS, 1000)
    finally:
        connection.close()
    fields = {tuple(f.path): f for f in sample.fields}
    assert sample.documents == couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.users")["results"][0]
    assert not sample.cut_at_bytes and sample.requested == 1000
    assert {"object", "string", "array", "number"} <= set(fields[()].types)
    assert fields[("u64max",)].types == {"number": 1} and ("address", "city") in fields


@requires_couchbase
def test_binary_documents_count_as_binary():  # A24: SELECT RAW d gives them as "<binary (N b)>" strings
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(Keyspace(bucket="dbaguy", scope="app", collection="blobs"), 100)
    finally:
        connection.close()
    assert {tuple(f.path): f for f in sample.fields}[()].types == {"object": 1, "binary": 2}


@requires_couchbase
def test_the_sample_stops_at_its_byte_cap(monkeypatch):
    monkeypatch.setattr(sampling, "SAMPLE_MAX_BYTES", 1024 * 1024)  # u_big alone is 5 MiB
    connection = open_couchbase()
    try:
        sample = connection.sample_fields(USERS, 1000)
        assert connection.query("SELECT RAW 1") == [1]  # the cut request left the connection usable
    finally:
        connection.close()
    total = couchbase_query("SELECT RAW COUNT(*) FROM dbaguy.app.users")["results"][0]
    assert sample.cut_at_bytes and sample.documents < total


@requires_couchbase
def test_the_api_caches_until_sampled_again_or_the_profile_changes(app, cb):
    client, _ = app
    keyspace = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
    query = {"profile_id": cb, **keyspace}
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json() is None
    sample = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace},
                         headers=AUTH).json()
    assert (sample["requested"], sample["documents"]) == (1000, 500)  # the settings' default size; o0 … o499
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json()["sampled_at"] == sample["sampled_at"]
    small = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace, "size": 10},
                        headers=AUTH).json()
    assert small["documents"] == 10
    too_small = client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace, "size": 5},
                            headers=AUTH)
    assert too_small.status_code == 422
    client.put(f"/profiles/{cb}", json={"profile": {**COUCHBASE, "name": "renamed"}}, headers=AUTH)
    assert client.get("/couchbase/fields", params=query, headers=AUTH).json() is None
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_sampling.py -q`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.adapters.couchbase.sampling'`.

- [ ] **Step 3: Write the models, `sampling.py`, `FieldSampling` and `CouchbaseConnection.sample_fields`.** `sample_fields` and `query()` share one private helper that starts an internal (read-only) request and returns its `CouchbaseResult`; `query()` keeps reading everything, `sample_fields` reads in batches and closes early. Log nothing of the documents.

- [ ] **Step 4: Write `FieldSampleService`, the endpoints and the wiring** (container, the profile invalidation, `CouchbaseSettings`). Then `npm run gen:api`; add the three types to `src/lib/api/types.ts`; add `couchbase: { sample_size: 1000 }` to `DEFAULT_SETTINGS` in `src/stores/settings.ts` (the generated `AppSettings` now requires it).

- [ ] **Step 5: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/test_couchbase_sampling.py tests/test_couchbase_api.py tests/test_local_stores.py -q`, then `cd backend && .venv/bin/pytest -q`, then `npm run typecheck && npm run build`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add backend/dbaguy_backend/domain/couchbase.py backend/dbaguy_backend/domain/settings.py \
  backend/dbaguy_backend/adapters/base.py backend/dbaguy_backend/adapters/couchbase/sampling.py \
  backend/dbaguy_backend/adapters/couchbase/adapter.py backend/dbaguy_backend/services/field_sample_service.py \
  backend/dbaguy_backend/api/couchbase.py backend/dbaguy_backend/api/profiles.py backend/dbaguy_backend/container.py \
  backend/tests/test_couchbase_sampling.py src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts \
  src/stores/settings.ts
git commit -m "feat(couchbase): Sample fields — field paths from a manual sample (binary-aware, 32 MiB cap), cached per keyspace"
```

---

### Task 8: SQL++ completion: keyspaces, sampled fields, fields seen in results

**Files:**
- Create: `backend/dbaguy_backend/sql/keyspaces.py`, `backend/dbaguy_backend/sql/sqlpp_completion.py`, `backend/tests/sql/test_sqlpp_completion.py`
- Modify: `backend/dbaguy_backend/sql/completion.py` (shared helpers public; `SnapshotIndex.keyspaces`; new kinds), `backend/tests/sql/test_completion.py` (one monkeypatch name), `backend/dbaguy_backend/api/sql.py` (`CompleteRequest`, dispatch)
- Modify: `backend/tests/test_couchbase_api.py` (one live test)
- Modify: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts` (generated), `src/features/editor/completion.ts` (`TYPES` for the new kinds), `src/features/explorer/kinds.tsx` (`field` label)

**Interfaces:**
- Consumes: Task 7's `Keyspace`, `FieldPath`, `container.field_samples.cached(profile_id, keyspace)`; 04a's `SchemaSnapshot.keyspaces`, `STATEMENT_KEYWORDS[SQLPP]` / `CLAUSE_KEYWORDS[SQLPP]`, `scope.unquote`, `scope.IDENT`, `quote_ident(Dialect.SQLPP, …)`.
- Produces:
  - `sql/keyspaces.py`: `@dataclass(frozen=True) class KeyspaceTree` with `buckets: tuple[str, ...]`, `scopes: Mapping[str, tuple[str, ...]]` (bucket → scopes), `collections: Mapping[tuple[str, str], tuple[str, ...]]` ((bucket, scope) → collections), `classmethod of(keyspaces: Iterable[SnapshotKeyspace]) -> KeyspaceTree` (names in snapshot order, each once; system keyspaces included, flagged via `system_scopes: frozenset[tuple[str, str]]`).
  - `sql/completion.py`: `SnapshotIndex.keyspaces: KeyspaceTree` (built in `__init__`; empty for relational snapshots); `CompletionKind` gains `"bucket"`, `"scope"`, `"collection"`, `"field"` (`_KIND_ORDER`: `field` with `column`, `collection` with the relation kinds, `bucket` / `scope` with `schema`); the helpers SQL++ completion shares become public names in place (DRY, spec §3.12): `Candidate` (was `_Candidate`), `by_length`, `filter_candidates` (was `_filter`), `statement_bounds`, `significant`, `token_before`, `inside_literal`, `clause_context` (was `_context`), `qualifier`, `keywords`, `guarded`. Breaks on purpose: `tests/sql/test_completion.py::test_a_bug_degrades_to_no_items_and_is_logged_without_sql` monkeypatches `"statement_bounds"`.
  - `sql/sqlpp_completion.py`: `FieldLookup = Callable[[Keyspace], Sequence[FieldPath] | None]`; `complete_sqlpp(text: str, cursor: int, index: SnapshotIndex | None, context: QueryContext | None, fields: FieldLookup, seen_fields: Sequence[str]) -> CompletionResult` — never raises (`guarded`); code-point offsets like `complete()`.
  - `api/sql.CompleteRequest` gains `query_context: QueryContext | None = None` and `seen_fields: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=500)`; `/sql/complete` sends an SQL++ profile's requests to `complete_sqlpp` with `fields = lambda ks: (sample.fields if (sample := container.field_samples.cached(profile_id, ks)) else None)`.

The SQL++ completion rules (spec §3.12; the tests pin them):
- Keyspace references are read by this module itself (ruling B21; `scope.py`'s alias rules stay as they are for the other dialects): after `FROM`, `JOIN` (any `… JOIN`), `NEST`, `UPDATE`, `INTO`, `USING`, and after a comma in a `FROM` list: a dotted path of names (`unquote` for back-ticks), then an alias — `AS name`, or a bare name that is not one of `USE ON WHERE JOIN LEFT INNER NEST UNNEST LET LETTING GROUP ORDER LIMIT OFFSET SET UNSET RETURNING VALUES SELECT FROM UNION INTERSECT EXCEPT KEYS USING`; without an alias the last path part is the name to qualify with.
- A path resolves to a keyspace: three parts → as written; one part → with a context that has a scope, `(bucket, scope, part)`; with a bucket-only context, `(bucket, "_default", part)`; without a context, `(part, "_default", "_default")`; two parts → none.
- Keyspace position (the word right after one of those keywords or that comma, possibly after `x.` / `x.y.`): no qualifier → the context's collections (scope as above; none without a context) as `collection` items, then every bucket as `bucket` items; `b.` → the scopes of `b` (`scope`); `b.s.` → the collections of `b.s` (`collection`). Inserted text: `quote_ident(Dialect.SQLPP, name)`; detail `"collection in b.s"`, `"bucket"`, `"scope in b"`.
- Qualified column position (`a.`, `a.f.`, …): `a` is an alias (or the bare last part of an unaliased reference) of the statement → its keyspace → its sampled paths → the keys one level below the typed path (array element segments are skipped: `a.items[0].` is not completed in 04b) as `field` items, detail `f"{collection} · {kinds} · {percent:g}%"` (kinds by count, most first, joined with `|`), inserted with `quote_ident`; nothing else.
- Unqualified column position (`clause_context` says `columns`): the aliases (`alias`), then — when the statement reads exactly one keyspace — its top-level sampled fields (`field`), then `seen_fields` not already offered (`field`, detail `"seen in results"`, boost −1), then the clause keywords.
- Start and clause positions: the keywords, as `complete()` gives them. Inside a string or comment: nothing.

- [ ] **Step 1: Write the failing tests.** Create `backend/tests/sql/test_sqlpp_completion.py`:

```python
"""SQL++ completion (spec §3.12, SQL++ Should 1): keyspace paths from the snapshot, field paths from samples and from
results seen in the tab, back-ticks for special names."""

from datetime import UTC, datetime

import pytest

from dbaguy_backend.domain.couchbase import FieldPath, Keyspace
from dbaguy_backend.domain.execution import QueryContext
from dbaguy_backend.domain.schema import SchemaSnapshot, SnapshotKeyspace
from dbaguy_backend.sql.completion import SnapshotIndex
from dbaguy_backend.sql.dialects import Dialect
from dbaguy_backend.sql.sqlpp_completion import complete_sqlpp

KEYSPACES = [SnapshotKeyspace(bucket=b, scope=s, collection=c) for b, s, c in [
    ("dbaguy", "_default", "_default"), ("dbaguy", "app", "events"), ("dbaguy", "app", "odd-name"),
    ("dbaguy", "app", "orders"), ("dbaguy", "app", "users"), ("dbaguy", "app", "value"),
    ("travel-sample", "_default", "_default"), ("travel-sample", "inventory", "airline"),
    ("travel-sample", "inventory", "airport"), ("travel-sample", "inventory", "route"),
]]
INDEX = SnapshotIndex(SchemaSnapshot(database=None, default_schema=None, search_path=[], schemas=[],
                                     built_at=datetime.now(UTC), keyspaces=KEYSPACES), Dialect.SQLPP)
USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")
CTX = QueryContext(bucket="dbaguy", scope="app")


def path(*segments, kind="string", docs=8):
    return FieldPath(path=list(segments), types={kind: docs}, docs=docs, percent=docs * 10.0)


FIELDS = {USERS: [
    path(kind="object", docs=10), path("name"), path("age", kind="number", docs=6),
    path("address", kind="object", docs=5), path("address", "city", docs=5), path("address", "zip", docs=3),
    path("odd key", docs=1), path("select", kind="number", docs=1), path("tags", kind="array", docs=4),
    path("tags", None, docs=4),
]}


def run(text, context=CTX, seen=()):
    cursor = text.index("|")
    return complete_sqlpp(text.replace("|", ""), cursor, INDEX, context, FIELDS.get, list(seen))


def items(result):
    return {i.label: i for i in result.items}


def test_from_offers_the_context_scopes_collections_and_every_bucket():
    found = items(run("SELECT * FROM |"))
    assert {k: found[k].kind for k in ("users", "odd-name", "value", "dbaguy", "travel-sample")} == {
        "users": "collection", "odd-name": "collection", "value": "collection", "dbaguy": "bucket",
        "travel-sample": "bucket"}
    assert (found["odd-name"].apply, found["value"].apply, found["travel-sample"].apply) == (
        "`odd-name`", "`value`", "`travel-sample`")
    assert "airline" not in found  # another scope's collection


def test_a_bucket_only_context_offers_its_default_scope_and_no_context_only_buckets():
    assert items(run("SELECT * FROM |", context=QueryContext(bucket="travel-sample")))["_default"].kind == "collection"
    assert {i.kind for i in run("SELECT * FROM |", context=None).items} == {"bucket"}


def test_after_a_bucket_the_scopes_and_after_a_scope_the_collections():
    assert [(i.label, i.kind) for i in run("SELECT * FROM `travel-sample`.|").items] == [
        ("_default", "scope"), ("inventory", "scope")]
    assert [i.label for i in run("SELECT * FROM `travel-sample`.inventory.ai|").items] == ["airline", "airport"]


@pytest.mark.parametrize("text", [
    "SELECT * FROM a JOIN |", "SELECT * FROM a AS x NEST |", "UPDATE |", "INSERT INTO |", "UPSERT INTO |",
    "DELETE FROM |", "MERGE INTO |", "MERGE INTO a USING |", "SELECT * FROM a, |",
])
def test_every_keyspace_position(text):
    assert "users" in items(run(text))


def test_alias_dot_offers_sampled_fields_with_back_ticks():
    found = items(run("SELECT u.| FROM users AS u"))
    assert set(found) == {"name", "age", "address", "odd key", "select", "tags"}
    assert (found["odd key"].apply, found["select"].apply, found["name"].apply) == ("`odd key`", "`select`", None)
    assert found["name"].kind == "field" and found["name"].detail == "users · string · 80%"


def test_nested_paths_and_implicit_aliases():
    assert set(items(run("SELECT u.address.| FROM users u"))) == {"city", "zip"}
    assert "name" in items(run("SELECT users.| FROM users"))  # an unaliased keyspace qualifies by its name
    assert "name" in items(run("SELECT d.| FROM dbaguy.app.users AS d", context=None))  # a full path needs no context
    assert items(run("SELECT d.| FROM app.users AS d")) == {}  # two parts name no keyspace


def test_one_keyspace_offers_its_fields_unqualified_then_the_fields_seen():
    result = run("SELECT na| FROM users AS u", seen=["name", "nation"])
    labels = [i.label for i in result.items]
    assert labels.index("name") < labels.index("nation") and labels.count("name") == 1
    assert items(result)["nation"].detail == "seen in results"


def test_aliases_in_column_positions_and_no_guessing_with_two_keyspaces():
    found = items(run("SELECT | FROM users AS u JOIN orders AS o ON KEYS u.oid"))
    assert found["u"].kind == "alias" and found["o"].kind == "alias" and "name" not in found


def test_unsampled_keyspaces_offer_no_fields():
    assert items(run("SELECT o.| FROM orders AS o")) == {}


def test_strings_and_comments_get_nothing():
    assert run("SELECT 'FROM |' FROM users").items == []
    assert run("SELECT * FROM users -- FROM |").items == []


def test_keywords_at_the_start():
    assert "ADVISE" in items(run("|"))
```

Append to `backend/tests/test_couchbase_api.py`:

```python
@requires_couchbase
def test_completion_uses_the_sample_and_the_tab_context(app, cb):
    client, _ = app
    keyspace = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
    client.post("/couchbase/fields/sample", json={"profile_id": cb, "keyspace": keyspace}, headers=AUTH)
    body = {"profile_id": cb, "text": "SELECT u. FROM users AS u", "cursor": 9,
            "query_context": {"bucket": "dbaguy", "scope": "app"}, "seen_fields": ["late"]}
    labels = [i["label"] for i in client.post("/sql/complete", json=body, headers=AUTH).json()["items"]]
    assert "name" in labels and "u64max" in labels
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && .venv/bin/pytest tests/sql/test_sqlpp_completion.py -q`
Expected: `ModuleNotFoundError: No module named 'dbaguy_backend.sql.sqlpp_completion'`.

- [ ] **Step 3: Share the helpers.** In `completion.py` rename the helpers (Interfaces) everywhere they are used, add the kinds to `CompletionKind` and `_KIND_ORDER`, and build `SnapshotIndex.keyspaces = KeyspaceTree.of(snapshot.keyspaces)`. Update the one monkeypatch in `tests/sql/test_completion.py`. Run `cd backend && .venv/bin/pytest tests/sql/test_completion.py -q` — green before going on (a pure rename).

- [ ] **Step 4: Write `keyspaces.py` and `sqlpp_completion.py`** per the rules, reusing `statement_bounds`, `significant`, `token_before`, `inside_literal`, `qualifier`, `clause_context` (for "columns" vs keyword positions), `Candidate`, `by_length`, `filter_candidates`, `keywords`, `guarded`.

- [ ] **Step 5: Dispatch.** `CompleteRequest` fields and the SQL++ branch in `api/sql.complete_at` (log at DEBUG as today: item count, never the text). `npm run gen:api`; in `src/features/editor/completion.ts` `TYPES` gains `bucket: "namespace"`, `scope: "namespace"`, `collection: "class"`, `field: "property"`; `kinds.tsx` `KIND_LABELS` gains `field: "Field"`.

- [ ] **Step 6: Run the tests and see them pass**

Run: `cd backend && .venv/bin/pytest tests/sql -q && .venv/bin/pytest tests/test_couchbase_api.py tests/test_sql_api.py -q`, then `cd backend && .venv/bin/pytest -q`, then `npm run typecheck && npm run build`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/sql/keyspaces.py backend/dbaguy_backend/sql/sqlpp_completion.py \
  backend/dbaguy_backend/sql/completion.py backend/dbaguy_backend/api/sql.py backend/tests/sql/test_sqlpp_completion.py \
  backend/tests/sql/test_completion.py backend/tests/test_couchbase_api.py src/lib/api/openapi.json \
  src/lib/api/schema.d.ts src/features/editor/completion.ts src/features/explorer/kinds.tsx
git commit -m "feat(sql): SQL++ completion — keyspace paths, sampled field paths, fields seen in results, back-ticks where needed"
```

---
### Task 9: Frontend: the tab's query context — picker, tree, browse tabs, ⌘P, History, completion, exports

**Files:**
- Create: `src/features/query/queryContext.ts`, `src/features/query/ContextPicker.tsx`, `src/features/explorer/browse.ts`
- Modify: `src/stores/workspace.ts`, `src/features/workspace/workspaceFile.ts`
- Modify: `src/features/query/QueryTab.tsx`, `src/features/query/QueryToolbar.tsx` (only if the picker's slot needs it), `src/features/query/ConnectionPicker.tsx`, `src/features/query/useQueryRunner.ts`, `src/features/query/ResultsArea.tsx`, `src/features/query/ResultGrid.tsx`
- Modify: `src/features/session/SessionController.ts`, `src/features/session/execution.ts`, `src/features/grid/ExportDialog.tsx`
- Modify: `src/features/explorer/Explorer.tsx`, `src/features/palette/objectSearch.tsx`, `src/features/history/useHistory.ts`, `src/features/editor/completion.ts`

**Interfaces:**
- Consumes (Task 6): `GET /couchbase/contexts`, `ContextOption`, `HistoryEntry.query_context`, `WorkspaceTab.query_context`, `POST /sql/browse-select` for SQL++, `ObjectSearchHit.kind === "collection"`; (Task 8) `CompleteRequest.query_context` / `seen_fields`; 04a's `SessionInfo.server.default_query_context`, `ExecuteOptions.queryContext`, `EngineInfo.query_context`.
- Produces (later frontend tasks use them):
  - `stores/workspace.ts`: `QueryTabState.queryContext?: QueryContext | "none"` (undefined: follow the profile's default; `"none"`: No context, ruling B14); `OpenQueryOptions.queryContext?: QueryContext | "none"`.
  - `features/query/queryContext.ts`: `type TabContext = QueryContext | "none" | undefined`; `effectiveContext(choice: TabContext, sessionDefault: QueryContext | null): QueryContext | null`; `sameContext(a: QueryContext | null | undefined, b: QueryContext | null | undefined): boolean` (bucket equal and scope equal, a missing scope equal to null); `contextOfPath(path: PathSegment[]): QueryContext | null` (the `bucket` segment's name and the `scope` segment's, if any); `useContextOptions(profileId: string | null, current: QueryContext | null, enabled: boolean)` (TanStack Query, key `["couchbase-contexts", profileId, current?.bucket ?? null, current?.scope ?? null]`, `GET /couchbase/contexts` with `bucket` / `scope` when `current` is set); `applyTreeContext(profileId: string, path: PathSegment[]): void` (the active tab is a query tab on `profileId` → `updateTab(id, "query", { queryContext: contextOfPath(path) })`; nothing otherwise, and nothing when the path has no bucket).
  - `features/query/ContextPicker.tsx`: `ContextPicker({ profileId, choice, sessionDefault, blockedReason, onChange }: { profileId: string; choice: TabContext; sessionDefault: QueryContext | null; blockedReason?: string; onChange: (next: QueryContext | "none") => void })`.
  - `features/explorer/browse.ts`: `openBrowseTab(profileId: string, path: PathSegment[]): Promise<void>` — `POST /sql/browse-select {profile_id, path}` → `openQuery(profileId, null, { text: sql, title: <collection name>, queryContext: contextOfPath(path) ?? "none" })`; a failure is a toast; never runs anything (D-04-11).
  - `features/session/SessionController.ts`: `export interface RequestOptions { queryContext?: QueryContext | null; params?: StatementParams | null }`; `ExecuteOptions` gains `params?: StatementParams | null` (sent as `params`); `exportResult(sql, format, path, options, request: RequestOptions = {})` sends `query_context` (undefined → the session default, as `execute`) and `params`.
  - `features/session/execution.ts`: `ExecutionState.request: RequestOptions` — what the request was sent with (`{ queryContext: <the context sent>, params }`), set by `track`; `newExecution(requestId, kind, sql, request: RequestOptions = {})`.
  - `ExportSource.request?: RequestOptions`; `ResultsArea` / `ResultGrid` `onExport: (sql: string, request: RequestOptions) => void` (the grid passes its execution's `request`).
  - `useQueryRunner` args gain `queryContext?: QueryContext | null` (undefined for engines without a context), read when a run starts and sent with each of its statements.
  - `CompletionTarget` gains `queryContext: QueryContext | null` and `seenFields: string[]`; the request carries `query_context` (when not null) and `seen_fields` (when not empty).

Behaviour:
- `QueryTab`: `sessionDefault = snapshot?.info?.server.default_query_context ?? null`; `context = effectiveContext(tab.queryContext, sessionDefault)` for engines with `query_context`, else undefined. Runs, the completion target, the browse tab and exports use `context`. Changing the connection resets `queryContext` to undefined (the new profile's default). The tab keeps `database = null` (ruling B15).
- The picker sits in the connection picker's row, after the profile, where `DatabasePicker` sits for PostgreSQL (`ConnectionPickerProps.context?: ContextPickerProps`, rendered when `engine.query_context`). Trigger: the effective context's `text` (from the options; `…` while they load), or `No context`; tooltip `Query context: unqualified names resolve in it`. Menu: the options (checked when `sameContext` with the effective context; the session default's row shows `default` at the right; an unlisted current one shows `not in the schema` at the right), a separator, `No context` (checked when `choice === "none"`). Disabled with the run's `blockedReason` while running.
- `seenFields` (memo over the run): the column names of every grid set of the tab's latest run, unique, in order, without `value`, `(other fields)`, `plan` and names holding a `.` (alias collision columns such as `d.id`, ruling B22), at most 500.
- Explorer: selecting a node row of a profile whose engine takes a context calls `applyTreeContext(row.profileId, row.node.path)` (spec §3.11.1; a profile row keeps selecting the profile only). Double-click on a `collection` node opens `openBrowseTab` (ruling B19); `CONTAINER_KINDS` stays. A `collection` node row gets a context menu: `Browse documents` (= double-click), `Details` (`openObject` → its detail tab). Task 12 adds `Sample fields…`; 04c adds the document items.
- ⌘P: a `collection` hit opens `openBrowseTab(profile.id, hit.path)`; the section's notes say "tables, views and collections".
- History: `openInNewTab` for a profile whose engine takes a context → `openQuery(entry.profile_id, null, { text: entry.sql, queryContext: entry.query_context ?? "none" })`; other engines as before (their `database`). Insert-at-cursor does not touch the tab's context.

- [ ] **Step 1: State and persistence.** `QueryTabState.queryContext`, `OpenQueryOptions.queryContext` (set by `openQuery`), and in `workspaceFile.ts` save `query_context: tab.queryContext ?? null` / restore `queryContext: entry.query_context ?? undefined`.

- [ ] **Step 2: Session plumbing.** `RequestOptions`, `ExecuteOptions.params`, `exportResult(…, request)`, `ExecutionState.request`, `ExportSource.request`, the `onExport(sql, request)` chain (`QueryTab` → `ExportDialog`), the runner's `queryContext`.

- [ ] **Step 3: `queryContext.ts`, `ContextPicker.tsx`, `browse.ts`** per Interfaces; wire the picker into `ConnectionPicker` and `QueryTab`.

- [ ] **Step 4: Tree, ⌘P, History, completion** per Behaviour.

- [ ] **Step 5: Check the types and the build**

Run: `npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 6: Manual check list** (the coordinator runs it with `scripts/dev.sh --web`, `http://localhost:1420`, a Couchbase profile `127.0.0.1`, database `dbaguy.app`, user `dbaguy`, `save_password` off):
  1. New query tab on the Couchbase profile: the picker shows `dbaguy.app` with `default`; `SELECT * FROM users LIMIT 1` runs.
  2. Pick `travel-sample.inventory`: `SELECT * FROM airline LIMIT 1` runs; reload the page: the tab still shows `travel-sample.inventory`.
  3. Pick `No context`: `SELECT * FROM users` fails with the server's keyspace error; reload: still `No context`.
  4. With that tab active, click `travel-sample › inventory › airport` in the tree: the picker shows `travel-sample.inventory`; click the bucket `dbaguy`: `dbaguy` (and `SELECT * FROM _default LIMIT 1` reads `dbaguy._default._default`).
  5. Double-click `dbaguy › app › events`: a new tab `events` holds `SELECT META(d).id, *` / `FROM events AS d` on two lines, context `dbaguy.app`, nothing ran; ⌘↵ gives columns `id, at, d.id, kind, amount, (other fields)`. Right-click it: Browse documents, Details.
  6. Double-click `dbaguy › _default › _default`: `FROM _default AS d`, context `dbaguy._default`; it runs.
  7. ⌘P `airl` → `airline · travel-sample · inventory` → a browse tab.
  8. Run something in context `travel-sample.inventory`; History, ⌥-open the entry: the new tab's picker shows `travel-sample.inventory`. An entry run with No context reopens with No context.
  9. Completion: `SELECT * FROM ` offers the context scope's collections and the buckets; `` SELECT * FROM `travel-sample`. `` offers its scopes. After a run of `SELECT * FROM users`, `SELECT ` offers `name` marked "seen in results".
  10. Export a grid's result as CSV from a tab in `travel-sample.inventory`: it succeeds (the same context).
  11. A PostgreSQL tab: no context picker; runs and exports as before.

- [ ] **Step 7: Commit**

```bash
git add src/features/query/queryContext.ts src/features/query/ContextPicker.tsx src/features/explorer/browse.ts \
  src/stores/workspace.ts src/features/workspace/workspaceFile.ts src/features/query/QueryTab.tsx \
  src/features/query/QueryToolbar.tsx src/features/query/ConnectionPicker.tsx src/features/query/useQueryRunner.ts \
  src/features/query/ResultsArea.tsx src/features/query/ResultGrid.tsx src/features/session/SessionController.ts \
  src/features/session/execution.ts src/features/grid/ExportDialog.tsx src/features/explorer/Explorer.tsx \
  src/features/palette/objectSearch.tsx src/features/history/useHistory.ts src/features/editor/completion.ts
git commit -m "feat(app): per-tab Couchbase query context — picker, tree clicks, browse tabs, ⌘P, History, completion, exports"
```

(Stage `QueryToolbar.tsx` only if it changed.)

---

### Task 10: Frontend: the parameters panel

**Files:**
- Create: `src/features/query/params.ts`, `src/features/query/ParamsPanel.tsx`
- Modify: `src/features/editor/statements.ts`, `src/features/query/runPlan.ts`, `src/features/query/useQueryRunner.ts`, `src/features/query/QueryTab.tsx`, `src/stores/workspace.ts`, `src/features/workspace/workspaceFile.ts`

**Interfaces:**
- Consumes: `StatementRange.params` of `/sql/split` (04a: named parameters once each as `$name` — `@x` is `$x` — in first-use order, and every `?`; `$1` … as written), `WorkspaceTab.params` (Task 6), `ExecuteOptions.params` / `RequestOptions` (Task 9), `parseJson` of `features/grid/json.ts` (lossless: numbers stay text).
- Produces:
  - `statements.ts`: `StatementRange.params: string[]` (from the split; kept through `mapRanges`).
  - `runPlan.ts`: `PlannedStatement.params: string[]` and `PlannedStatement.values?: StatementParams | null` (what is sent; set by the tab before the run).
  - `stores/workspace.ts`: `QueryTabState.params?: Record<string, string>` (drafts keyed `$name` / `$n`, the text as typed); persisted as `params` (null ↔ undefined).
  - `params.ts`: `interface ParamSlots { named: string[]; positional: number }` (`named` are `$name`, never all digits); `paramSlots(lists: readonly (readonly string[])[]): ParamSlots` (named in first-use order over all statements; `positional` = the largest, over the statements, of max(highest `$k`, number of `?`)); `slotNames(slots: ParamSlots): string[]` (`[...named, "$1", …, "$n"]`); `type ValueKind = "empty" | "invalid" | "string" | "number" | "boolean" | "null" | "object" | "array"`; `valueKind(text: string): ValueKind`; `type ParamProblem = { name: string; problem: "missing" | "invalid" }`; `paramsFor(used: readonly string[], drafts: Record<string, string>, lenient = false): { params: StatementParams | null } | ParamProblem` (a statement's named parameters → `named[name without $]`, its `k` positional → `positional[$1 … $k]`, each the trimmed draft; empty → `missing`, not JSON → `invalid`; `lenient` (Explain) skips those instead; nothing used → `params: null`); `paramsWatcher(onChange: (slots: ParamSlots) => void): Extension` (a ViewPlugin: whenever `statementsField` holds a fresh split whose slots differ from the last ones reported).
  - `ParamsPanel.tsx`: `ParamsPanel({ slots, drafts, open, focus, onToggle, onChange }: { slots: ParamSlots; drafts: Record<string, string>; open: boolean; focus: string | null; onToggle: () => void; onChange: (name: string, text: string) => void })`.

Behaviour (spec §3.11.2):
- The panel shows when the engine has `parameters` and the editor's statements use any parameter; it sits at the bottom of the editor pane, a bar `Parameters (n)` that opens and closes it. Each row: the name (`$x`, `$1`), a monospace one-line input (the JSON literal), a badge from `valueKind` (`string`, `number`, …, `invalid` in the danger token, nothing for `empty`). Under an invalid value: `A string needs double quotes: "abc"`. Each change is `updateTab(id, "query", { params: { ...drafts, [name]: text } })` (the workspace saves it). New React key handlers check `isImeKeyEvent(e)` first.
- Before a run (Run, Run all, Run with profile): for each planned statement `paramsFor(statement.params, drafts)`; the first problem stops the run before anything is sent or analysed — toast `Parameter $x has no value` or `Parameter $x is not a JSON value (a string needs double quotes: "text")`, the panel opens and that input takes focus. Otherwise every planned statement gets `values` and the runner sends `params: statement.values ?? null` with it.
- Explain (Task 11) uses `paramsFor(…, lenient = true)`.
- History keeps only the SQL with its placeholders (unchanged); exports send the execution's own `request.params` (Task 9).

- [ ] **Step 1: Split ranges carry params; `params.ts`; tab drafts and their persistence.**

- [ ] **Step 2: `ParamsPanel.tsx`; the watcher and the panel in `QueryTab`; validation before a run; `values` through the runner.**

- [ ] **Step 3: Check the types and the build**

Run: `npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 4: Manual check list** (Couchbase tab, context `dbaguy.app`):
  1. Type `SELECT $n + 1 AS n1`: `Parameters (1)` appears with `$n`; enter `9007199254740993` (badge `number`); run: `n1` is `9007199254740994`.
  2. Empty the value and run: toast `Parameter $n has no value`, the panel opens with `$n` focused, nothing new in Messages.
  3. Enter `abc`: badge `invalid` and the hint; run: the "not a JSON value" toast.
  4. `SELECT @x AS a, $x AS b` gives one row `$x`; `SELECT ? AS a, ? AS b, $1 AS c` gives `$1`, `$2` and `c` equals `a`.
  5. Two statements, `SELECT $a AS a;` and `SELECT $b AS b;`, Run all: each runs with its own value.
  6. `$u` = `18446744073709551615`: Messages shows a WARNING `Parameter $u: …` without the digits; the cell shows `18446744073709552000`.
  7. Reload the page: the drafts are back.
  8. Export from that grid: the export runs with the same values (CSV holds them).
  9. Typing Chinese with the IME (注音) in a value input never runs or moves anything until the composition ends.
  10. A PostgreSQL tab with `$1` in a `$$` body shows no panel.

- [ ] **Step 5: Commit**

```bash
git add src/features/query/params.ts src/features/query/ParamsPanel.tsx src/features/editor/statements.ts \
  src/features/query/runPlan.ts src/features/query/useQueryRunner.ts src/features/query/QueryTab.tsx \
  src/stores/workspace.ts src/features/workspace/workspaceFile.ts
git commit -m "feat(app): parameters panel — typed JSON values per name and position, checked before a run, kept in the workspace"
```

---

### Task 11: Frontend: the plan view, Explain, Run with profile, index advice

**Files:**
- Create: `src/features/explain/planApi.ts`, `src/features/explain/PlanView.tsx`, `src/features/explain/PlanResult.tsx`
- Modify: `src/features/session/execution.ts` (`ResultSetState.presentation`), `src/features/session/SessionController.ts` (`ExecuteOptions.profilePlan`)
- Modify: `src/features/query/ResultsArea.tsx`, `src/features/query/QueryToolbar.tsx`, `src/features/query/QueryTab.tsx`, `src/features/query/tabActions.ts`, `src/features/query/runPlan.ts`, `src/features/query/runMarks.ts`, `src/features/query/useQueryRunner.ts`, `src/features/palette/commands.ts`

**Interfaces:**
- Consumes (Task 6): `POST /couchbase/plan`, `POST /couchbase/advise`, `PlanView` & co.; (Task 4) the `plan` presentation of EXPLAIN / ADVISE results and of the profile set, `ExecuteCommand.profile_plan`; (Task 9) `ExecutionState.request`; (Task 10) `paramsFor(…, lenient)`; `SessionInfo.server.edition`, `EngineInfo.plan_format`, `EngineInfo.executed_plan`.
- Produces:
  - `execution.ts`: `ResultSetState.presentation: "grid" | "plan"` (from `result_set`).
  - `SessionController`: `ExecuteOptions.profilePlan?: boolean` → `profile_plan`.
  - `runPlan.ts`: `PlannedStatement.prefix?: string`; `sentText(statement: PlannedStatement): string` (= `(prefix ?? "") + sql`).
  - `useQueryRunner`: `StartOptions.history?: boolean` (default true) and `profilePlan?: boolean`; it analyses and sends `sentText(statement)`.
  - `runMarks.ts`: `beginRunMarks(view, planned)` remembers each statement's prefix length; an error position is mapped against the sent text and moved back by that length, never before the statement's start (M10).
  - `tabActions.ts`: `QueryTabActionId` gains `"explain"` and `"runWithProfile"`; `palette/commands.ts`: `SHORTCUTS.explain = "⌘E"`, commands `Explain` (⌘E) and `Run with profile`.
  - `planApi.ts`: `usePlanView(text: string | null)` (TanStack Query key `["plan", text]`, `POST /couchbase/plan`, disabled for null); `useAdvice()` (a mutation: `{ profileId, statement, queryContext } → PlanView` through `POST /couchbase/advise`).
  - `PlanView.tsx`: `PlanView({ view, advice }: { view: PlanView; advice?: AdviceControl })` with `interface AdviceControl { available: boolean; reason?: string; pending: boolean; result: PlanView | null; error: string | null; request: () => void }`.
  - `PlanResult.tsx`: `PlanResult({ set, execution, profileId, edition, grid }: { set: ResultSetState; execution: ExecutionState; profileId: string; edition: "enterprise" | "community" | null; grid: ReactNode })` — a `Plan | Grid` switch (Plan first) over the plan view and the given grid.

Behaviour (spec §3.10, §3.13):
- Results: a set whose `presentation` is `plan` on an engine with `plan_format === "couchbase"` shows `PlanResult`; its tab is labelled `Plan` when it is the statement's first set and `Profile` after the first (a profile run's second set). The cell is `rows[0][0]`; its text is `jsonCellText(cell)`; a truncated cell (a plan over 4 MiB) shows `The plan is too large to draw; switch to Grid or export it as JSON`.
- `PlanView`: a header (kind, `statement_text` in monospace, the whole plan's cost / cardinality when given), the findings (warning or info marker in the warning / muted tokens, the text; a click selects the node), the operator tree as indented rows (title, the node's badges, estimates `cost · rows` and, for a profile, `items_out` and `exec + serv` time), then the subqueries' trees under `Subquery: <text>`, and a details pane for the selected node (all `details`, `estimates`, `stats`). Advice (`kind === "advice"`, or `advice.result`): current, recommended, covering, each statement in monospace with a Copy button (`writeClipboard`), the rule muted; `advice_note` when there is nothing to recommend. `kind === "unknown"`: `Nothing to draw: switch to Grid to see the server's text`.
- `Get index advice` (in a plan or profile view): `statement` = `view.statement_text ?? execution.sql` (for a profile set, the statement that ran), `queryContext` = `execution.request.queryContext`; disabled on `edition === "community"` with `Community Edition does not give index advice`; an error shows the server's text (5001 on `app.users`, A15).
- Toolbar (engines with `plan_format === "couchbase"`): `Explain` (⌘E) beside Run, with a menu `Run with profile` (disabled on Community Edition with `Community Edition does not give an execution profile`; shown only when `executed_plan`; its tooltip: `Runs the statement and shows the plan it ran with after its last row — a result closed before its last row has none`, spec §3.10). Disabled when Run is.
- Explain: the statements `planRun(view, "default", "statement", dialect)` gives (the selection's, or the one under the cursor); more than one → toast `Explain one statement at a time`; none → nothing. The statement is sent with `prefix = "EXPLAIN "` unless its text starts (after whitespace) with `EXPLAIN` or `ADVISE` (case-insensitive), `values` from `paramsFor(…, lenient = true)`, `history: false` (ruling B23, B27).
- Run with profile: like Run (the same planning, parameters check and confirmations) with `profilePlan: true`.
- Keymap: `{ key: "Mod-e", run: () => (explainRef.current(), true) }` in the tab's `Prec.highest` keymap.

- [ ] **Step 1: Presentation and the profile option** (`execution.ts`, `SessionController`), prefix and history in the runner and the marks.

- [ ] **Step 2: `planApi.ts`, `PlanView.tsx`, `PlanResult.tsx`; results area integration.**

- [ ] **Step 3: Toolbar, keymap, tab actions, palette.**

- [ ] **Step 4: Check the types and the build**

Run: `npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 5: Manual check list** (Couchbase tab, context `dbaguy.app`):
  1. Cursor in `SELECT * FROM orders WHERE amount > 400`, ⌘E: a `Plan` tab shows `Sequence › IndexScan3 · idx_orders_amount › Fetch · orders › …` with the info finding "does not cover the query"; the tab's History has no new entry.
  2. `SELECT * FROM noindex WHERE x = 1` → the sequential-scan warning; `SELECT * FROM users WHERE name = "Ann"` → the primary-scan warning; the `USE INDEX (def_inventory_airport_faa, def_inventory_airport_city)` query in `travel-sample.inventory` → the IntersectScan warning.
  3. Get index advice on the orders plan: `CREATE INDEX adv_amount ON …` with Copy (the clipboard holds it); on the users plan: the server's 5001 text.
  4. Run with profile on `SELECT o.amount FROM orders AS o WHERE o.amount > 400 ORDER BY o.amount DESC LIMIT 3`: a grid tab and a `Profile` tab with item counts and times, the index scan marked slowest.
  5. Plan ↔ Grid switch; the grid shows the one `plan` cell.
  6. A selection of two statements, ⌘E: toast `Explain one statement at a time`.
  7. `SELECT * FROM orders WHER amount > 1`, ⌘E: the error mark sits under `WHER`, not 8 characters off.
  8. A typed `EXPLAIN SELECT 1` run with ⌘↵ shows the plan view; ⌘E on it sends it unchanged.
  9. ⌘⇧P lists Explain (⌘E) and Run with profile.
  10. A PostgreSQL tab: no Explain button; ⌘E does nothing.

- [ ] **Step 6: Commit**

```bash
git add src/features/explain src/features/session/execution.ts src/features/session/SessionController.ts \
  src/features/query/ResultsArea.tsx src/features/query/QueryToolbar.tsx src/features/query/QueryTab.tsx \
  src/features/query/tabActions.ts src/features/query/runPlan.ts src/features/query/runMarks.ts \
  src/features/query/useQueryRunner.ts src/features/palette/commands.ts
git commit -m "feat(app): plan view — Explain (⌘E), Run with profile, index advice with Copy, anti-pattern findings"
```

---

### Task 12: Frontend: sampled fields and the sample size setting

**Files:**
- Create: `src/features/explorer/fieldSamples.ts`, `src/features/explorer/SampledFields.tsx`, `src/features/explorer/SampleFieldsDialog.tsx`
- Modify: `src/features/explorer/ObjectDetailView.tsx`, `src/features/explorer/Explorer.tsx`, `src/features/settings/settingsForm.ts`, `src/features/settings/SettingsDialog.tsx`

**Interfaces:**
- Consumes (Task 7): `POST /couchbase/fields/sample`, `GET /couchbase/fields`, `Keyspace`, `FieldSample`, `FieldPath`, `AppSettings.couchbase.sample_size`; (Task 9) the collection node's context menu.
- Produces:
  - `fieldSamples.ts`: `keyspaceOfPath(path: PathSegment[]): Keyspace | null` (the `bucket`, `scope` and `collection` segments); `fieldSampleKey(profileId: string, keyspace: Keyspace)` (`["field-sample", profileId, bucket, scope, collection]`); `useFieldSample(profileId: string, keyspace: Keyspace)` (`GET /couchbase/fields`); `useSampleFields()` (a mutation `{ profileId, keyspace, size } → FieldSample`; on success it puts the result under `fieldSampleKey`).
  - `SampleFieldsDialog({ open, profileId, node, onOpenChange })` — a size field (default `settings.couchbase.sample_size`, 10–10,000, the settings form's validation), `Sample`; while it runs `Sampling…` and Cancel closes the dialog (the sample finishes in the background and is cached); success opens the collection's detail tab (`openObject(profileId, node)`); an error shows the server's text in the dialog (13014 with the missing role for a user without sequential scans).
  - `SampledFields({ profileId, keyspace })` — in the collection's detail page under its DDL: heading `Fields (sampled)`; without a sample `Not sampled yet` and `Sample fields…`; with one, a line `N documents · sampled <local time> · Sample again`, notes when `cut_at_bytes` (`stopped at 32 MB`) or `paths_capped` (`only the first 2,000 paths`), and a table Path | Types | Documents: the path written with `.` between keys, `[]` for an array element and `(document)` for the root, kinds with counts (`string 8 · null 2`), `percent%`.
  - `settingsForm.ts`: section `couchbase`, field `sample_size` (`Sample size`, unit `documents`, 10–10,000, hint `Documents read by Sample fields; INFER is never run for you`); `SettingsDialog.tsx`: a `Couchbase` group with it.

- [ ] **Step 1: `fieldSamples.ts`, the dialog, the detail section, the menu item, the setting.**

- [ ] **Step 2: Check the types and the build**

Run: `npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 3: Manual check list:**
  1. Right-click `dbaguy › app › users` → `Sample fields…` (size 1000) → the users detail tab shows `Fields (sampled)`: `(document)` with object / string / array / number, `address.city`, `tags.[]`, `u64max` number, percentages.
  2. `dbaguy › app › blobs`: the root shows `binary 2`.
  3. `Sample again` with the setting changed to 10 (Settings › Couchbase): `10 documents`.
  4. Completion after sampling: `SELECT u. FROM users AS u` offers `name`, `address`, `` `odd key` `` (back-ticked); `SELECT u.address. FROM users AS u` offers `city`, `zip`.
  5. Edit the profile (rename): the detail tab says `Not sampled yet` again (the cache followed the profile).
  6. As `dbaguy_reader` (a second profile), sample `dbaguy › app › users`: it works (primary index); the tree hides other collections.

- [ ] **Step 4: Commit**

```bash
git add src/features/explorer/fieldSamples.ts src/features/explorer/SampledFields.tsx \
  src/features/explorer/SampleFieldsDialog.tsx src/features/explorer/ObjectDetailView.tsx \
  src/features/explorer/Explorer.tsx src/features/settings/settingsForm.ts src/features/settings/SettingsDialog.tsx
git commit -m "feat(app): Sample fields from the tree and the collection detail page; sample size in Settings"
```

---
### Task 13: Packaging check, measurements, CLAUDE.md, spec notes and the user's test list

**Files:**
- Modify: `scripts/measure-startup.sh` (footprint of the measured processes)
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-10-18-04-couchbase-design.md` (R-04-6, §1, §3.2.1, §3.7.4, §3.12, §10)
- Modify (only if Step 2 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/t13_probe.py`, `<scratchpad>/rss_probe.py`

**Interfaces:**
- Consumes: everything above; 04a Task 13's procedure (frozen sidecar in `src-tauri/resources/backend`, `rss_probe.py`).
- Produces: a bundle whose frozen sidecar serves every 04b endpoint and reads ahead; measurements and the 04b state in CLAUDE.md and the spec.

- [ ] **Step 1: Build the sidecar.** Run: `npm run build:backend`. Expected: passes; `du -sk src-tauri/resources/backend` grows by ≈ 0 against 04a's 49,020 KB (stdlib only; `selectors` and `decimal` are already in the bundle).

- [ ] **Step 2: Probe the frozen sidecar.** Shell 1 (note the PID; stop it with Ctrl-C or `kill <PID>`):

```bash
cd src-tauri/resources/backend
DBAGUY_TOKEN=t DBAGUY_PORT=0 DBAGUY_LOG_TO_FILE=false DBAGUY_DATA_DIR="$(mktemp -d)" ./dbaguy-backend   # prints {"event":"ready","port":N}
```

Shell 2: save as `<scratchpad>/t13_probe.py` and run `backend/.venv/bin/python <scratchpad>/t13_probe.py N t backend/tests/fixtures/couchbase/explain_fetch.json` (about 3 minutes):

```python
"""04b packaged check against the dev cluster: contexts, plan, advice, sampling, SQL++ completion, a profiled run, and
the read-ahead (a paused result that fits stays loadable past the idle limit; a larger one is closed)."""
import json, sys, time, urllib.request

from websockets.sync.client import connect

port, token, fixture = sys.argv[1:4]
APP = {"bucket": "dbaguy", "scope": "app"}


def call(method, path, body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        text = response.read()
    return json.loads(text) if text else None


profile = call("POST", "/profiles", {"profile": {
    "engine": "couchbase", "name": "c", "host": "127.0.0.1", "database": "dbaguy.app", "username": "dbaguy",
    "save_password": False, "ssl": {"mode": "disable"}}, "secrets": {"password": "dbaguy"}})["profile"]["id"]
print("contexts:", [o["text"] for o in call("GET", f"/couchbase/contexts?profile_id={profile}")][:6])
print("plan findings:", [f["badge"] for f in call("POST", "/couchbase/plan", {"text": open(fixture).read()})["findings"]])
advice = call("POST", "/couchbase/advise", {"profile_id": profile, "query_context": APP,
                                             "statement": 'SELECT * FROM orders WHERE customer = "c1"'})
print("advice:", [a["statement"] for a in advice["advice"] if a["kind"] == "recommended"])
sample = call("POST", "/couchbase/fields/sample", {"profile_id": profile, "keyspace": {**APP, "collection": "users"}})
print("sample:", sample["documents"], "documents,", len(sample["fields"]), "paths")
items = call("POST", "/sql/complete", {"profile_id": profile, "text": "SELECT u. FROM users AS u", "cursor": 9,
                                       "query_context": APP})["items"]
print("completion:", [i["label"] for i in items][:8])

session = call("POST", "/sessions", {"profile_id": profile})["session_id"]
with connect(f"ws://127.0.0.1:{port}/sessions/{session}/ws?token={token}", max_size=None) as ws:
    def send(**command):
        ws.send(json.dumps(command))

    def events_until(request_id, stop):
        seen = []
        while True:
            event = json.loads(ws.recv())
            if event.get("request_id") == request_id:
                seen.append(event)
                if stop(event):
                    return seen

    send(type="execute", request_id="p", sql="SELECT o.amount FROM orders AS o WHERE o.amount > 400 LIMIT 3",
         query_context=APP, profile_plan=True)
    done = events_until("p", lambda e: e["type"] == "finished")
    print("profiled run sets:", [e["presentation"] for e in done if e["type"] == "result_set"])

    send(type="execute", request_id="e", sql="SELECT META(d).id, * FROM events AS d", query_context=APP, page_size=500)
    events_until("e", lambda e: e["type"] == "result_set_end" and e["has_more"])
    print("events paused; waiting 70 s (past 45 s and a housekeeping pass)")
    time.sleep(70)
    send(type="fetch_more", request_id="e", count=10000)
    rest = events_until("e", lambda e: e["type"] == "result_set_end" or e["type"] == "finished")
    print("Load more after 70 s:", sum(len(e["rows"]) for e in rest if e["type"] == "rows"), "rows;",
          [e["text"] for e in rest if e["type"] == "message"])
    send(type="discard", request_id="e")
    events_until("e", lambda e: e["type"] == "finished")

    send(type="execute", request_id="s", page_size=500,
         sql="SELECT RAW a * 1000 + b FROM ARRAY_RANGE(0, 100000) AS a UNNEST ARRAY_RANGE(0, 1000) AS b")
    events_until("s", lambda e: e["type"] == "result_set_end" and e["has_more"])
    paused_at = time.monotonic()
    closed = events_until("s", lambda e: e["type"] == "finished")
    print(f"big result finished {time.monotonic() - paused_at:.0f} s after its pause:",
          [e["text"] for e in closed if e["type"] == "message"])
```

Expected: contexts list `dbaguy`, `dbaguy._default`, `dbaguy.app`, …; plan findings `['not_covered']`; advice `CREATE INDEX adv_customer …`; sample 12 documents; completion holds `name`; profiled run sets `['grid', 'plan']`; the events Load more after 70 s returns 10,000 rows and no "Closed this result" message (read ahead to its end); the big result finishes 45–62 s after its pause with "Closed this result 45 s after the server was left waiting for it…". If the frozen sidecar fails to import a module, add it to `hiddenimports` in `backend/dbaguy-backend.spec`, rebuild and repeat.

- [ ] **Step 3: Memory while paging a million rows** (Q1, spec §7). With the frozen sidecar still running, create a session (`curl -s -H "Authorization: Bearer t" -H "Content-Type: application/json" -X POST http://127.0.0.1:N/sessions -d '{"profile_id":"<the profile id printed by Step 2's profile call>"}'`) and run 04a Task 13 Step 3's `rss_probe.py` unchanged (`backend/.venv/bin/python <scratchpad>/rss_probe.py N t S <PID>`; its 10 s pause now lets the read-ahead fill its 8 MiB). Expected: 1,000,000 rows; growth < 30 MB against the same baseline 04a used (a sidecar that has run one query). Record peak RSS and `footprint <PID>` before and after.

- [ ] **Step 4: `footprint` in `scripts/measure-startup.sh`.** The awk that lists the processes also writes each selected PID to a temporary file (`print $1 > (ENVIRON["PIDS_FILE"])`, with `PIDS_FILE="$(mktemp)"` passed in its environment); after the RSS total, add:

```bash
# phys_footprint of the same processes: what Activity Monitor calls Memory (RSS above double-counts shared pages)
if command -v footprint >/dev/null && [[ -s "$PIDS_FILE" ]]; then
  # shellcheck disable=SC2046  # one PID per word
  footprint --noCategories $(<"$PIDS_FILE") 2>/dev/null | grep -E 'Footprint:' | sed -E 's/^ +//' || true
fi
rm -f "$PIDS_FILE"
```

Expected output lines per process (`dbaguy-backend [123]: 64-bit    Footprint: 72 MB …`) and `Summary Footprint: … MB`. The script still touches only the PIDs it started or selected (no pattern kills).

- [ ] **Step 5: Build the app, check signatures, measure startup.** Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`. Note `stat -f %m ~/Library/Application\ Support/DbaGuy` before and check it unchanged after; run `scripts/measure-startup.sh` 3 times. Expected: cold start and footprint within noise of 04a (1.1–1.2 s; 175–177 MB, backend 72–75 MB); record them.

- [ ] **Step 6: Update `CLAUDE.md`** (keep its language and style):
  - **Repo 結構:** `sessions/read_ahead.py`（暫停結果的停滯時鐘，`StallClock`）; `adapters/couchbase/` 加 `params`（參數數字警告）、`explain`（plan／profile／ADVISE 分析，純函式）、`sampling`（取樣欄位路徑，純函式）; `services/couchbase_service.py`（query context 清單、ADVISE、History 的 context）、`services/field_sample_service.py`（取樣快取）; `sql/keyspaces.py`、`sql/sqlpp_completion.py`; `api/couchbase.py`; 前端 `features/explain/`、`features/query/{queryContext, ContextPicker, params, ParamsPanel}`、`features/explorer/{browse, fieldSamples, SampledFields, SampleFieldsDialog}`.
  - **架構重點 → 新增 engine:** 能在暫停時預讀的 engine 實作 `StatementResult.read_ahead()`（不可阻塞）並宣告 `read_ahead_bytes`.
  - **架構重點 → Couchbase:** replace the 暫停的結果 bullet: 暫停後 worker 在指令之間預讀 server 已送出的列（每個結果最多 8 MiB，以 `sys.getsizeof` 計、只存列的原文；只讀 socket 上已有的，不等待，所以 Cancel／Discard／Load more 永遠不會被擋）；放得下就讀到結尾（server 請求結束，結果可以一直 Load more）；放不下時 45 秒從讀滿停下那一刻起算（housekeeping 每 15 秒，45–60 秒關閉），之後讀了 16 MiB（預讀上限 2 倍）或 socket 讀空才重算（A23：7.6.12 在 client 停止讀取 77 秒後砍請求；之後讀 128 KiB 不會重置，約 5.5–8 MiB 會）。沒有預讀的引擎照舊從第一次暫停起算. Add bullets: 每個 tab 自己的 query context（`QueryTabState.queryContext`：undefined＝profile 預設、`"none"`＝不帶、或 `{bucket, scope}`；workspace 的 `query_context`；顯示文字一律是 backend 的 `QueryContext.text()`；Couchbase 的 tab 不用 `database`，History 重開帶 `HistoryEntry.query_context`）；參數面板（`/sql/split` 的參數，值是 JSON literal，跑之前全部檢查，worker 收 `StatementParams`，server 會讀成 double 而改值的數字以參數名警告，不顯示值）；plan 檢視（EXPLAIN／ADVISE／profile 的結果 `presentation="plan"`，`/couchbase/plan` 分析，Explain ⌘E 前綴 `EXPLAIN `，Get index advice 走 metadata 連線的 `/couchbase/advise`，Run with profile 的 profile 是第二個 result set，plan cell 4 MiB 內不截斷）；Sample fields（`SELECT META(d).type AS t, d … LIMIT $n`，Python 推導欄位路徑，32 MiB／深度 6／2,000 條，快取到重新取樣或 profile 變更）；SQL++ 補全（keyspace 路徑、取樣欄位、結果看過的欄位，名稱需要時加 back-tick）.
  - **已知限制 → Couchbase:** drop the 04a lines that 04b resolves ("暫停：活躍使用 Load more…要等 04b 的預讀", "EXPLAIN／ADVISE 在 04a 以一欄 JSON 顯示（plan 檢視是 04b）"); add: 預讀看不到 http.client 自己已緩衝的 bytes（回應的最後一段在那裡時顯示為「等待中」，下一次 Load more 才讀到，不會因此被關）；16 MiB 重算門檻只在 dev（OrbStack）量過；多個暫停的 Couchbase 結果每個最多佔 8 MiB；補全不處理陣列元素（`a.items[0].`）與 UNNEST 的 alias；結果看過的欄位不含帶 `.` 的欄名；plan 檢視是縮排的樹（不是圖）；ADVISE／profile 在 Community Edition 停用；Explain 只對游標下或選取的一句、缺值的參數不送；Sample fields 的路徑最多 6 層、2,000 條；MySQL／PostgreSQL／SQL Server 不預讀（driver 沒有不阻塞的讀取；MySQL 的 `net_write_timeout` 是否會砍長時間暫停的結果未驗證）.
  - **量測:** Step 3's RSS and footprint while paging, Step 5's startup and footprint, the sidecar size.
  - **進度 ④:** `進行中：04a、04b 完成（SQL++ 方言與 HTTP 引擎；query context、參數、plan 檢視、取樣與補全、暫停預讀）；04c（文件）待做`.
- [ ] **Step 7: Update the spec** (`docs/superpowers/specs/2026-10-18-04-couchbase-design.md`; record decisions, do not rewrite history): R-04-6's row ends with 「04b：暫停時預讀（每個結果 8 MiB），放得下就讀到結尾、不關閉；放不下時 45 秒從預讀停下起算，讀 16 MiB 或讀空才重算（A23）」; §1's paused-result success criterion gets the same; §3.2.1's capability table gains `read_ahead_bytes` (預設 None，Couchbase `READ_AHEAD_BYTES`，worker 的暫停預讀); §3.7.4 gets a closing bullet pointing to `sessions/read_ahead.py`; §3.12's sampling query becomes `SELECT META(d).type AS t, d FROM ks AS d LIMIT $n`（binary 文件以 `META().type` 判斷，A24）; §10's A16 row records 〔實測〕(04b T1) and A23–A26 are added with their results.
- [ ] **Step 8: Report the user's test list** for 04b in the task handoff (what the dev cluster already showed next to each):
  1. The company cluster's buckets and scopes in the context picker; clicking the tree sets the tab's context; a History entry reopens in its context (dev: travel-sample, dbaguy).
  2. Parameters with real ids and amounts: big integers stay exact, a decimal the server rounds is flagged (dev: `9007199254740993` + 1).
  3. EXPLAIN of real slow queries: the flags make sense; Get index advice (EE) gives usable CREATE INDEX; Run with profile names the slow operator (dev: orders, noindex, users, travel-sample).
  4. Sample fields on large real collections: time, the 32 MB cut, a limited user without `query_use_sequential_scans` on a collection without an index (13014 with the missing role).
  5. Completion with sampled fields on real names (back-ticks for `-` and reserved words).
  6. Paused results: one under ~8 MB stays loadable after minutes; a big one closes 45–60 s after it stops reading ahead, with the message; the DBA view shows `closed`, never `fatal` (dev: events, the 100 M-row stream).
  7. The packaged app: Explain, Sample fields and the parameters panel work; the measurements in CLAUDE.md hold on this Mac.

- [ ] **Step 9: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build && bash -n scripts/measure-startup.sh`
Expected: green.

```bash
git add scripts/measure-startup.sh CLAUDE.md docs/superpowers/specs/2026-10-18-04-couchbase-design.md
git commit -m "docs: 04b in CLAUDE.md and the spec (read-ahead, contexts, parameters, plan view, sampling, completion); measure-startup prints footprint"
```

(`backend/dbaguy-backend.spec` too, only if Step 2 changed it.)

---

## Carries to 04c

| Carry | Where |
|---|---|
| The grid's "Open document…" prefills the keyspace from the tab's effective context (`effectiveContext`, `contextOfPath`) plus the last collection | Task 9 `features/query/queryContext.ts` |
| The collection context menu gains "Open document by ID…" and "New document…" next to Browse documents / Details / Sample fields… | Tasks 9, 12 `Explorer.tsx` |
| `Keyspace` (frozen, a cache key) is the documents API's keyspace model; `CouchbaseConnection.query(…, context=…)` and the internal-result helper of `sample_fields` serve `SqlppDocumentStore` | Tasks 6, 7 |
| `documents=True` with 04c; `MetadataService.with_connection(retry=False)` for writes (D-04-9); `FieldSampling` is the pattern for `DocumentCapable` (ISP) | spec §3.9, Task 7 |
| Follow-ups (not 04c): MySQL read-ahead and whether `net_write_timeout` ends a long-paused streamed MySQL result; array element and UNNEST alias completion; a TS test runner for the pure `params.ts` / `queryContext.ts` helpers | rulings B4, B21, B30 |

## Planning rulings

### Assumptions recorded during execution

Task 1 re-runs the probes; the Result column below is what they showed while planning (2026-10-19, `enterprise-7.6.12` dev cluster, scratch scripts as in Task 1 Step 4). The owner task writes a differing result over it and applies the Effect; nothing else in the plan needs editing for the outcomes listed.

| # | Assumption | Owner | Result | Effect on later tasks |
|---|---|---|---|---|
| A16 | `profile` arrives after `metrics`, shaped `executionTimings` (spec §10) | Task 1 | Holds: top-level keys `requestID, signature, results, status, metrics, profile`; `profile` = `{phaseTimes, phaseCounts, phaseOperators, cpuTime, requestTime, servicingHost, executionTimings}`; `executionTimings` is the operator tree from `Authorize` with `#stats` (`#itemsIn`, `#itemsOut`, `#phaseSwitches`, `execTime`, `kernTime`, `servTime`; Go durations such as `34.611027ms`, `4.792µs`) | Holds: Tasks 4 and 5 as written. Not: Task 4's profile set reads the field the server does send (record its name) and Task 5's `profile` kind detection follows. |
| A23 | After a client reads a big answer and stops, 7.6.12 ends the request a fixed time after the stop; a later small read does not restart that clock, a large one does | Task 1 | Holds: read 8 MiB then stop → gone 77.5 s after the stop (`fatal`, 1203 `service.slow_client`); 40 s later a 128 KiB read → still gone 77.4 s after the first stop; an 8 MiB read → gone 77.4 s after the second stop; 40 MiB → 76.2 s after the second; 16 × 512 KiB over 32 s → gone 99.6 s after the first stop (reset after ≈ 5.5 MiB read); 128 KiB every 2 s kept it alive for 200 s, then gone 51 s after the last read. After the kill the rest of the answer was gone (a read got 0 bytes) | Holds: `STALL_RESET_FACTOR = 2` (16 MiB) as written. A larger reset seen: raise the factor so 2 × budget stays above it. A small read seen to reset: keep the factor (it is the safe side). |
| A24 | Binary documents can be told apart in a sample | Task 1 | `SELECT RAW d` gives them as the strings `"<binary (9 b)>"`, `"<binary (20 b)>"` (indistinguishable from a string document); `SELECT META(d).type AS t, d` gives `t = "base64"` for them, `"json"` otherwise | Holds: ruling B16 as written. Not (`META().type` missing): binary documents count as strings, and CLAUDE.md says so. |
| A25 | Plan and advice shapes the view relies on | Task 1 | EXPLAIN rows `{plan, text}` (+ `cost`, `cardinality`, `optimizer_hints` with CBO); `~subqueries` at the top level of the row (not inside `plan`), each `{correlated, plan, subquery}`; `IntersectScan.scans[]`; joins as a flat `Join` (`on_keys`) in the Sequence; `optimizer_estimates {cardinality, cost, fr_cost, size}` per operator; ADVISE row `{#operator: Advise, advice: {#operator: IndexAdvice, adviseinfo: {current_indexes[], recommended_indexes}}, query}` where `recommended_indexes` is `{indexes[], covering_indexes[]}` or the string `"No index recommendation at this time."`, a current index may carry `index_status`, a recommendation `recommending_rule`, `update_statistics`, and with a query context `index_statement_relative`, `query_context` | Holds: Task 5 as written (its fixtures are these answers). Not: Task 5's rules follow the recorded shape. |
| A26 | Parameter binding | Task 1 | `@n` binds the request's `$n`; `?` take `args` in order and `$1` is `args[0]` (mixing works); `$n + 1` with 9007199254740993 → 9007199254740994 exactly; `18446744073709551615` → `18446744073709552000`; `3.141592653589793238462643383279` → `3.141592653589793`; `1.10` → `1.1` | Holds: Tasks 4 and 10 as written (one slot per name, `?` and `$k` share positions; the warning rule of B11). |

### Rulings made while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| B1 | The read-ahead budget is 8 MiB of memory as `sys.getsizeof` counts each held row's text (+8 for its deque slot), not 8 MiB of characters. | A million 8-character rows are 8 MB of text but ~65 MB of Python objects; Q1 needs the real figure. | Small rows hold fewer rows than 8 MiB of text would: a result of tiny rows exceeds the budget sooner and closes after 45–60 s as in 04a. |
| B2 | The stall clock restarts once `STALL_RESET_FACTOR × budget` (16 MiB) of answer bytes have been read since the stall began, by the read-ahead or by Load more. | A23: 128 KiB does not restart the server's 77 s clock; ≈ 5.5 MiB in steps and 8 MiB at once do; 2 × leaves room for bigger buffers elsewhere. | Too high: an active Load-more user's result closes earlier than needed (rows kept, clear text). Too low on another setup: the server ends the request first and the next Load more fails with `connection_lost` (04a's behaviour before R-04a-T11b). |
| B3 | A read-ahead step that finds nothing on the socket (`WAITING`) ends a stall. | Nothing waiting on the socket means DbaGuy has drained everything the server sent: its writes are not blocked by DbaGuy. | On a slow link a momentarily empty socket restarts the clock while data is in flight: at worst the same as B2's "too low". |
| B4 | Only Couchbase reads ahead in 04b; PostgreSQL, MySQL, SQL Server and SQLite keep the default (`read_ahead` → `FULL`, never called). | Their drivers read through their own buffered sockets with no non-blocking poll; a blocking read on the worker would hold Cancel / Discard / Load more behind a slow server (Q2), and SQL Server's FreeTDS must not be entered for that (03b). MySQL could free its connection with it, but that is a change to an adapter with a delicate drain / KILL path: carried. Whether MySQL's `net_write_timeout` (60 s) ends a long-paused streamed result was not probed. | None new: those engines behave as in 04a. |
| B5 | Held rows are stored as text and re-parsed on delivery (`parse_row`). | A parsed `RawRow` (row text, field texts, decoded values) is 4–5× its text. | Rows read ahead are parsed twice (≈ 80k rows/s): 8 MiB costs ~0.1–0.4 s spread over Load mores. |
| B6 | A step never waits: it reads only when `QueryResponse.ready()` (bytes on the socket or decrypted TLS bytes pending). Bytes already inside http.client's buffer are not seen. | http.client exposes no non-blocking look at its buffer, and a read with a socket timeout corrupts its chunked state for good. | An answer whose last bytes sit in that buffer stays `WAITING` until the next Load more reads them; never closed meanwhile (WAITING is not a stall). |
| B7 | The idle-close text says "Closed this result N after the server was left waiting for it, to free the query node — …" for every engine. | True with read-ahead (from the stop) and without (from the first pause); 04a's "after it first paused" is no longer true for Couchbase. | A wording change; three tests follow it. |
| B8 | Run with profile: the profile is a second result set, tab `Profile`; with no `profile` in the answer, one INFO message and no set. | Spec §3.10; CE sends none. | None. |
| B9 | Cells of a `plan` presentation are sent whole up to `PLAN_TEXT_LIMIT` (4 MiB). | The plan view needs the whole text; a profile of a complex query can pass 64 KiB. | A plan over 4 MiB shows only as a truncated cell with a message. |
| B10 | The parameter warning names the parameter only. | Spec §4 (DbaGuy's texts never echo parameter values) and §5 over §3.5.4's example, which shows the value. | The user looks the value up in the panel. |
| B11 | A literal warns when it holds an integer outside int64, or a decimal / exponent whose float64's shortest repr is a different value; `1.10`, `0.1`, `1E+3`, `-0` do not. | A26: the server returns `1.1` for `1.10` (same value), but changes π and u64max. Warning on every inexact binary fraction (0.1) would be noise. | `1.10` silently shows as `1.1` (value equal). |
| B12 | "Get index advice" runs `ADVISE <statement>` through `POST /couchbase/advise` on the profile's metadata connection (read-only), in the tab's context; not through the session. | On the session it would end a paused result of the same run and add a step to the run; ADVISE does not execute anything. | One more request on the metadata connection (opened if idle-closed). |
| B13 | `GET /couchbase/contexts` takes the tab's current context and lists it first (`listed: false`) when the snapshot lacks it; every text comes from `QueryContext.text()`. | The picker must show a context from History or a profile default the snapshot does not list, and the frontend must not quote. | None. |
| B14 | The tab's context is `undefined` (the profile's default), `"none"` (No context) or `{bucket, scope}`; the workspace stores `query_context: QueryContext | "none" | None`. | One `null` cannot mean both "default" and "none" (04a P1's reason, now in the workspace). | None. |
| B15 | Couchbase query tabs keep `database = null`: the session opens with the profile's default context and every request carries the tab's own; History reopens through `HistoryEntry.query_context`, never through `database`. | `database` reopens the session with another default (04a behaviour), which would be a second, hidden context. | Tabs reopened from History in 04a keep their `database` (still works: it is their session default). |
| B16 | Sampling reads `SELECT META(d).type AS t, d … LIMIT $n` instead of spec §3.12's `SELECT RAW d`. | A24: binary documents come back as `"<binary (N b)>"` strings in `SELECT RAW d`, indistinguishable from a string document (Q3). | None (one more field per row). |
| B17 | A field path is a list of keys with `None` for an array's elements, at most 6 segments; kinds are counted once per document. | Arrays of objects need a segment; per-document counting keeps `percent` meaningful. | A real key spelled like a marker cannot be confused (None is not a string). |
| B18 | The browse statement back-ticks the collection only when needed (`FROM events AS d`). | Spec §3.3.2's quoting rule; D-04-11's back-ticks were illustrative. | None. |
| B19 | Double-click on a collection opens the browse tab (D-04-11), replacing 04a's detail tab (P20); Details is in the collection's menu. | Spec D-04-11; the detail page is still one click away. | None. |
| B20 | ⌘P matches the collection's name; text before the last dot must prefix the scope or `bucket.scope`; hits show `bucket · scope`. | A collection name alone is ambiguous across scopes; the qualifier narrows like `schema.table` does. | None. |
| B21 | SQL++ completion reads its own keyspace references (FROM / JOIN / NEST / UPDATE / INTO / USING and comma lists) instead of extending `sql/scope.py`. | Adding NEST / UNNEST / LET to `scope.py`'s alias stops would break PostgreSQL's `FROM unnest(…) AS u`. | Two small readers of FROM lists exist side by side. |
| B22 | Fields seen in results exclude `value`, `(other fields)`, `plan` and any name holding a dot. | A `*` expansion names a collision column `d.id`; suggesting it as a field would insert `` `d.id` ``, a different key. | A real key with a dot is not suggested from results (sampling still finds it). |
| B23 | Explain sends `EXPLAIN ` + the statement (analysed as sent: EXPLAIN of a DELETE is READ, no confirmation), moves error positions back by the prefix, sends a statement already starting with EXPLAIN / ADVISE unchanged, and sends only the parameters that have valid values. | Spec §3.10; one statement per Explain; EXPLAIN does not need values. | A statement starting with a comment before EXPLAIN gets a second EXPLAIN and the server's syntax error. |
| B24 | Explain shows for `plan_format == "couchbase"`; Run with profile and Get index advice are disabled when `server.edition == "community"` and enabled when the edition is unknown. | Spec §3.10 (CE has neither); an unknown edition should not hide a working feature. | On a CE server of unknown edition the server's 3230 text shows. |
| B25 | `completion.py`'s helpers that SQL++ completion shares become public names in place (`Candidate`, `filter_candidates`, …); one test's monkeypatch follows. | Spec §3.12 (DRY); importing underscored names across modules is the alternative. | None. |
| B26 | `scripts/measure-startup.sh` prints `footprint` for the processes it already selects. | 04a's measurements needed a scratch copy for it (ledger). | None. |
| B27 | Explain runs are not recorded in History (`history=false`); Run with profile is. | Spec §3.10. | None. |
| B28 | `HistoryEntry.query_context` is filled only for entries of existing profiles whose engine takes a context; a malformed text gives None. | A PostgreSQL `database` must not read as a bucket; a deleted profile has no engine. | Entries of deleted Couchbase profiles reopen with No context (they reopen without a connection anyway). |
| B29 | A `read_ahead` that raises (a bug) is logged and treated as `FULL`: the first-pause clock. | Adapters keep failures; an unexpected exception must not kill the worker loop. | None. |
| B30 | The params slot logic and the context helpers are pure TS checked by hand (no TS test runner). | The project has no frontend test framework (CLAUDE.md work rules); the backend owns parsing (`/sql/split` gives each statement's parameters). | A slip in `paramSlots` shows as a wrong panel row; the manual list covers named, `@`, `?`/`$k` and multi-statement cases. |

### Rulings made during execution

None yet.

## Spec issues found while planning

- §3.5.4's parameter WARNING example contains the value (`18446744073709551615`) while §4 says DbaGuy's texts never echo parameter values (B10).
- R-04-6, §1 and §3.7.4 time the idle close "from the first pause"; the binding 04a carry R-04a-T11a/T11b replaces that with the read-ahead's stop, and A23 shows a Load more's small refill must not restart it (B1–B3, Task 13 Step 7 records it).
- §3.12's `SELECT RAW d` cannot tell a binary document from a string document (A24, B16).
- §3.12's seen fields "不含 value 與溢出欄" would still offer alias collision columns such as `d.id` (B22).
- §3.11.1's `GET /couchbase/contexts?profile_id` lists only snapshot contexts, but the picker must also show (and the frontend must not quote) a current context the snapshot lacks (B13).
- §3.2.3's `WorkspaceTab.query_context: QueryContext | None` cannot tell the profile's default from No context (B14).
- D-04-11 shows ``FROM `c` `` always back-ticked while §3.3.2 quotes only when needed (B18).
- §3.10's `findings: [{badge, path, text}]` cannot address a subquery's nodes with a child path from the root; node ids replace it.
- §3.10 sends "Get index advice" through the session; on the session it would end a paused result and add a run step (B12).

## Self-review

- **Spec coverage (§11 item 2):** Query context: picker, tab state and workspace, tree, History, browse statement and ⌘P → Tasks 6 (API, history, workspace, browse SELECT, keyspace search) and 9 (UI). Parameters panel: split params, execute and export with params, workspace, number WARNING → Tasks 4 (warning, exact body), 6 (workspace field), 9 (export with the execution's params), 10 (panel). EXPLAIN / ADVISE / profile: `/couchbase/plan`, PlanView, Explain button, A16 → Tasks 1 (A16, fixtures), 4 (profile set), 5 (analysis), 6 (`/plan`, `/advise`), 11 (UI). Sampling: `FieldSampleService`, API, collection detail → Tasks 7, 12. SQL++ completion: keyspace, fields, seen fields → Task 8 (+ Task 9 sends context and seen fields). The 04a carries: T11a/T11b read-ahead → Tasks 2, 3 (live pins), 13 (frozen sidecar); A1 browse shape → Task 9 check 5; A15 → Tasks 6, 11 use `orders`, `users` only for the 5001 text; `queryContext` option and `default_query_context` → Task 9; `RelationKind "collection"` / `relation_path` / ⌘P (P20, P23) → Task 6; `executed_plan` (P6) → Task 4; parameter WARNING (P24) → Task 4; `/sql/describe` → None → Task 6; measure-startup footprint → Task 13. §6.3's 04b integration tests (ADVISE and profile on EE, parameter types, per-request context) → Tasks 4, 6. §13 docs → Task 13.
- **Placeholders:** none; each probe-dependent step gives both outcomes in the assumptions table; Task 5's fixture-based expectations say what to do if a re-captured plan differs.
- **Names across tasks:** `ReadAheadState.{READING, WAITING, FULL, DONE}`, `ReadAhead(state, received)`, `StatementResult.read_ahead(budget)`, `AdapterCapabilities.read_ahead_bytes`, `StallClock(reset_bytes).update(step, now)` / `.since`, `STALL_RESET_FACTOR`, `READ_AHEAD_POLL_S`, `SessionWorker.paused_read_ahead()`, `paused_result_closed_text`; `READ_AHEAD_BYTES`, `READ_AHEAD_STEP_CHUNKS`, `row_cost`, `parse_row`, `QueryResponse.ready()`, `HttpChannel.readable()`, `EventStream.next_if_ready()` / `.received`, `CouchbaseResult.held`; `FieldEvent.text`, `start_result(…, profile, messages)`, `parameter_warnings`, `changes_as_double`, `PLAN_TEXT_LIMIT`, `encode_row(row, text_limit)`; `PlanView` / `PlanNode` / `PlanFinding` / `IndexAdvice` / `PlanSubquery` / `PlanDetail` / `PlanEstimates` / `PlanStats`, `analyze`, `analyze_value`, `go_duration_ns`; `ContextOption`, `CouchbaseService.{require, contexts, advise, with_query_contexts}`, `HistoryEntry.query_context`, `WorkspaceTab.{query_context, params}`, `CouchbaseConnection.query(…, context=)`; `Keyspace`, `FieldPath`, `FieldSample`, `FieldSampling.sample_fields`, `FieldCollector`, `sample_statement`, `SAMPLE_MAX_BYTES`, `FieldSampleService.{sample, cached, invalidate}`, `CouchbaseSettings.sample_size`; `KeyspaceTree`, `SnapshotIndex.keyspaces`, `complete_sqlpp`, `FieldLookup`, `CompleteRequest.{query_context, seen_fields}`; TS `TabContext`, `effectiveContext`, `sameContext`, `contextOfPath`, `useContextOptions`, `applyTreeContext`, `ContextPicker`, `openBrowseTab`, `RequestOptions`, `ExecutionState.request`, `ParamSlots`, `paramSlots`, `slotNames`, `valueKind`, `paramsFor`, `paramsWatcher`, `ParamsPanel`, `PlannedStatement.{params, values, prefix}`, `sentText`, `ResultSetState.presentation`, `ExecuteOptions.{params, profilePlan}`, `StartOptions.{history, profilePlan}`, `usePlanView`, `useAdvice`, `PlanView`, `PlanResult`, `keyspaceOfPath`, `useFieldSample`, `useSampleFields`. Checked task by task.
- **Review Focus:** each of the five lines names the tests (or manual checks) that pin it and the task that owns them.
- **Proportion:** the plan carries tests and the few algorithms the tests do not determine (the worker's task/read-ahead loop, the uncovered-scan walk); bodies are left to the implementers elsewhere.
