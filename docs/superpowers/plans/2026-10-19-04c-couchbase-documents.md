# 04c: Couchbase documents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open a Couchbase document by its ID (from the tree, a result grid or a dialog), see its stored text with its META (CAS, expiration, type, size), edit it in a JSON editor and save it with a CAS check that keeps its expiry — showing every change the server would make before it makes it and refusing what it would corrupt — create and delete documents, keep unsaved edits across a restart, and read documents of up to 20 MiB without decoding them into Python trees.

**Architecture:** Backend first. The 04a response parser stops building nested values: objects and arrays inside a row are only located, by the C JSON decoder with hooks that keep nothing (`sql/jsontext.value_end`), so a document costs its text, not a tree. `sql/jsontext.py` gains the pure checks a save needs (`json_problem`, `number_paths`, and `diff`, which walks the edited text and the server's form of it level by level and reports key reordering, respelled numbers, dropped duplicate keys and anything else, never building a tree). An engine-neutral `DocumentStore` / `DocumentCapable` contract (`adapters/documents.py`) is implemented for Couchbase by `SqlppDocumentStore` (SQL++ only, R-04-2) on the profile's metadata connection; `DocumentService` runs the spec's save flow (guards, server-form preview, confirmation, write with `retry=False`) behind `/couchbase/documents/*`. The frontend adds a `document` tab (CodeMirror with `@codemirror/lang-json`), an open dialog, the tree's and the grid's entries, the save preview and conflict dialogs, and workspace persistence of unsaved edits.

**Tech Stack:** Python 3.13 stdlib only for Couchbase (`http.client`, `json`, `re`, `decimal`, `math`), FastAPI, pydantic v2, loguru; Couchbase Server `enterprise-7.6.12` dev cluster (OrbStack); React 19 + TypeScript, CodeMirror 6 (`@codemirror/lang-json` 6.0.2, new, exact pin like the other `@codemirror/*` packages; it brings `@lezer/json` 1.0.3), zustand, TanStack Query, radix-ui.

**Spec:** docs/superpowers/specs/2026-10-18-04-couchbase-design.md — §11 item 3 (04c): the T1 probes A2 and A3; `DocumentStore` / `SqlppDocumentStore`, `DocumentService`, `/couchbase/documents/*`, `MetadataService.with_connection(retry=False)`; the document tab, the preview / conflict dialogs, the open dialog, the grid's "Open document…", workspace restore, `@codemirror/lang-json`, opening a document in the packaged app. Built on R-04-2 (documents through SQL++ only, option B), D-04-8, D-04-9, §3.2.3, §3.9, §3.13, §4, §5, §6.3, §8, §10 (A2, A3), §12. Earlier plans: docs/superpowers/plans/2026-10-18-04a-couchbase-engine.md (carries to 04c: `documents=True`, `jsontext` token comparison and save diff, `QueryRequest.preserve_expiry`, `with_connection(retry=False)`, lang-json) and docs/superpowers/plans/2026-10-19-04b-couchbase-query-workflow.md ("Carries to 04c"); follow-ups notes docs/superpowers/notes/2026-10-19-04a-couchbase-engine-followups.md and docs/superpowers/notes/2026-10-19-04b-couchbase-query-workflow-followups.md (binding here: the lean row decoder; `server_errors_are_final()`; whole-document reads; the collection menu; tests never add documents to `app.users`).

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 04c serves it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 | Nested values in rows are located, never built (Task 2): a 4 MiB document parses in 3.0× its text instead of 9–11×, the seeded 5 MiB `u_big` reads in 3.0× instead of 4.0×, 100,000 small rows parse as fast as before. The save checks walk text level by level with the C decoder (Task 3: ≤ 8× the text, never a tree). A clean document never enters `workspace.json`, and restored document tabs load only when first shown (Task 6). One new frontend package of about 10 KB (lang-json); no Python dependency. Task 8 re-measures the sidecar, startup and footprint. |
| §2 Q2 穩定、不丟工作 | Unsaved document text and the CAS it was based on survive a crash or restart in `workspace.json` (Task 6); closing a tab with unsaved edits asks (Task 7); a write whose answer was lost is never resent and says the outcome is unknown (Task 5); a password-less profile asks for its password and the tab recovers (Task 6). |
| §2 Q3 可信任 | The tab shows the stored text exactly (`1.10`, `1e400`, `18446744073709551615`, key order, whitespace) and the real META (Tasks 4, 6). A save is checked against the CAS (exact beyond 2⁵³), keeps the expiry, never writes a binary document, refuses numbers the server would change (beyond int64 or a double's range) naming their paths, and shows every other token change (sorted keys, respelled numbers, dropped duplicate keys) before it happens, value changes first (Tasks 3–5, 7). ⌘S on an unedited document sends nothing (Task 7). Document text, field values and key paths never reach a log; IDs only at DEBUG (Tasks 4, 5). |
| §2 Q4 聚焦 80% | SQL++ only (no SDK, no KV, no sub-document API); binary documents view-only; no rename; no grid editing (⑤); no field-level editor beyond the JSON text (spec §1 out of scope). |
| §2 Q5 多引擎從第一天 | The document contract is engine-neutral (`adapters/documents.py`, capability `documents`); other engines show nothing new; the JSON editor mode and the `document` tab kind are generic (Tasks 4, 6). |
| §2 Q6 macOS 慣例 | ⌘S saves the document, ⌘W closes its tab (asking when there are unsaved edits), ⌘Z undoes Format (Tasks 6, 7). |
| FP §4 Must 6 用 ID 開文件 | Open by ID from the tree, the grid and a dialog; JSON editor; META (CAS, expiration, type, size); CAS optimistic check with a conflict dialog (Reload / Copy my version / Cancel); expiry preserved (Tasks 4–7). |
| FP §4 Must 2 / 4 | The collection menu gains Open document by ID… and New document…; a result grid's cell menu gains Open document… (Tasks 6, 7). |
| §8 自架 7.x | Probed on Enterprise 7.6.12 (Task 1); a limited user reads documents (Task 4). |
| M4 編輯器 / 當機保護 | Document tabs are restored with their unsaved text (Task 6). |
| M11 安全 | A read-only profile opens documents read-only and the backend refuses its writes; Delete asks first (Tasks 5–7). |

## Global Constraints

- Scope is spec §11 item 3 (04c) on branch `feat/04-couchbase` (04b ends at ed013d1, its follow-ups note at 7c9e25f). It is the last part of ④: the merge to `main` happens after this plan's final review, not inside any task.
- **No Couchbase SDK.** Python `>=3.13,<3.14`, poetry in-project venv (`backend/.venv`), no new Python dependency. The only new frontend dependency is `@codemirror/lang-json` at exactly `6.0.2` (`npm install --save-exact @codemirror/lang-json@6.0.2`; it needs `@codemirror/language` ^6 and `@lezer/json` ^1, both satisfied).
- Binding values, each defined once, in the file named:
  - `sql/jsontext.py`: `CHANGES_PER_KIND = 100` (changes kept per kind), `CHANGES_LISTED = 200` (changes listed in a preview), `CHANGE_TEXT_LIMIT = 200` (characters of a change's before / after text, then `…`), `KEYS_SHOWN = 10` (keys named in a key-order change).
  - `adapters/couchbase/documents.py`: `BLOCKED_PATHS_MAX = 50`; `INT64_MIN = -(2**63)`, `INT64_MAX = 2**63 - 1`.
  - `domain/couchbase.py`: `DOCUMENT_ID_MAX_BYTES = 250` (Couchbase's key limit, UTF-8 bytes), `DOCUMENT_TEXT_LIMIT = 20 * 1024 * 1024` (characters of a saved text; the server enforces its own 20 MiB), `CAS_PATTERN = r"^[1-9][0-9]{0,18}$"`, `CAS_MAX = 2**63 - 1` (a CAS beyond int64 would be compared as a double by SQL++, ruling C11).
  - Frontend: a new document starts as the text `{}`; the dirty check runs `DIRTY_CHECK_MS = 200` after typing stops; the preview dialog shows the server's text when it is at most `SERVER_TEXT_SHOWN_MAX = 1_048_576` characters (else only Copy); `CLOSE_GUARD_ORDER.document = 5`.
- From the spec, verbatim where it binds this plan:
  - R-04-2: 「開檔 `SELECT META(d).cas, META(d).expiration, META(d).type, d FROM ks d USE KEYS $id` 拿到存放原文（key 順序、空白、`1e400`），不需 index（調查 §6.2）。存檔 `UPDATE … USE KEYS $id SET d = $doc WHERE META(d).cas = $cas RETURNING META(d).cas` ＋ `preserve_expiry=true`，〔實測〕過期時間保留、CAS 不符回 0 列。副作用（key 排序、空白壓縮、小數正規化、> int64 被改值）由守衛處理：**> int64 擋下**；與編輯內容 token 不同時**先預覽再確認**（細化見 §12）；binary／base64 文件唯讀。」
  - D-04-9: 「文件 tab 不綁 query tab 的 session；metadata 連線本來就是每個 profile 一條、閒置關閉。`MetadataService.with_connection()` 斷線會重試一次，寫入要關掉（`retry=False`）；有 CAS 的 replace／remove 就算重送也只會變成衝突，但 insert 會誤報「已存在」。」
  - §3.9.3: 「1. 唯讀 profile → 直接拒絕（`read_only`）；binary 文件 → `binary_document`。2. 本地檢查：不是合法 JSON → `invalid_json`（含位置）；`blocked_numbers` 有 → `number_out_of_range`（列出路徑……），不送出。3. `server_form(text)`；用 `jsontext.diff(edited, server_form)` 比較：兩邊 token 相同（只差空白）→ 直接存；不同 → 回 `SavePreview{server_text, changes}`……前端顯示後由使用者確認（`confirmed=true` 再送一次）。4. `replace`／`insert`，經 metadata 連線、`retry=False`（D-04-9）。成功回新 CAS 與 expiration。5. 文件 API 不經 SafetyPolicy（不是使用者打的 SQL；它本身就是明確的寫入動作，有自己的確認）。」
  - §12: 「「與編輯內容不同就預覽並確認」改成「**token** 不同才確認」。只差空白（server 一定會壓縮）也確認的話……確認會變成反射動作而失去意義。」
  - §4: 「原則：server 錯誤原文優先（M10）；DbaGuy 自己的錯誤是固定文字，不回顯 SQL、文件內容、參數值或密碼。」
  - §5 (loguru): INFO 「文件存檔／新增／刪除（keyspace，不含 ID）」; DEBUG 「文件 ID」; 永不 「`Authorization` header、密碼、參數值、文件內容、列內容」. 04c adds: a save that needs confirmation logs its change counts at INFO (never paths, keys or values); blocked paths travel in the API answer, never in a log.
- Documents are written only by an explicit Save (⌘S / the button), New document's first save, or Delete… of that document. ⌘S on a document whose text equals what was loaded or last saved sends nothing. A binary document is never written: every UPDATE and DELETE the store sends carries `AND META(d).type = "json"`.
- Threads: every `DocumentStore` call runs on the profile's metadata worker thread inside `MetadataService.with_connection` (only that thread touches the connection, 04a). Reads (`get`) keep the default one reconnect-and-resend; save, insert and remove pass `retry=False`.
- Literals and identifiers only from `sql/dialects.py`: the keyspace path is `keyspace_path(bucket, scope, collection)`, `$id` is `quote_literal(Dialect.SQLPP, id)`, `$cas` is the decimal text of a checked `int`, `$doc` is the user's text after `json_problem` accepted it (it is embedded in the request body as a JSON value, as parameter literals are). UTF-16 offsets only through `sql/offsets.py` (`utf16_offset`, new). `sql/` stays pure: no I/O, no logging.
- Dev cluster as in 04a/04b: service `couchbase` (`enterprise-7.6.12`), `127.0.0.1` ports 8091 / 8093 / 18091 / 18093, admin `dbaguy`/`dbaguy`, limited `dbaguy_reader`/`dbaguy`; start with `docker compose -f dev/docker-compose.yml up -d --build --wait`.
  - Seeded documents are never changed (`app.users`: `u_numbers`, `u_keyorder`, `u_ttl`, `u_big`, `u1`…`u5`, `s1`, `a1`, `n1`; `app.blobs`: `bin1`, `txt1`, `j1`; …). Tests write only keys starting `zz_04c_` (manual checks `zz_check_`) in `dbaguy.app.orders`, never in `app.users` (04b: sampling's byte-cap test needs `u_big` to stay the 9th of 12 users), and remove them by exact key in `finally` (`tests.support.scratch_documents()`).
  - `app.orders` holds exactly its 500 seeded documents (`o0`…`o499`) whenever a backend suite runs: 04b's `test_the_api_caches_until_sampled_again_or_the_profile_changes` samples all of them and asserts 500 (ruling C24). A scratch document exists only inside its own test, probe or manual check list; never run the suite (or the Couchbase document tests) while another run, probe or check list has scratch documents in `app.orders`; a leftover is removed by exact key through the docs endpoint (`tests.support.delete_documents`) before the next run.
  - No written document is larger than 64 KiB: the `dbaguy` bucket's quota is 100 MB, and while planning two ≈ 18 MB documents made the data service answer TMPFAIL (12008) to reads and deletes until one was removed through the KV docs endpoint (A32). Large-document checks read the seeded `u_big` (5 MiB) or parse synthetic responses.
- The repo is public: only the fake dev credentials. Never touch `~/Library/Application Support/DbaGuy` (not even `ls`); `scripts/dev.sh --web` uses `dev/data`; profiles made in browser checks or sidecar probes use `"save_password": false`; nothing is written to the Keychain. Agents never use the in-app browser: the coordinator runs every manual check list.
- **Never kill processes by pattern** (no `pkill -f`, no `killall`): stop only processes you started, by PID.
- Tests cover only error-prone logic: the lean decoder (every split position, memory), `value_end` / `json_problem` / `number_paths` / `diff`, the store's statements against the dev cluster (exact text, CAS, expiry, binary guard, duplicates, non-object documents, lean read), the service's flow (preview, confirmation, refusals, no resend, logging), the workspace model. The frontend has no test framework: `npm run typecheck && npm run build` plus each frontend task's manual check list.
- `cd backend && .venv/bin/pytest -q` must be green after every task, with the dev containers up; no `requires_couchbase` test may be skipped in a task report.
- After any backend model or API change: `npm run gen:api`, commit `src/lib/api/openapi.json` and `src/lib/api/schema.d.ts`, never hand-edit them; TS imports API types only through `src/lib/api/types.ts`. `npm run typecheck && npm run build` must pass after every task that touches `src/` or regenerates the types.
- Frontend: colours only through the Tailwind token classes (no hex or rgb); every new React key handler checks `isImeKeyEvent(e)` from `@/lib/ime` first (CodeMirror keymaps skip composition on their own); editor text never goes into React state (`stores/editorBuffers.ts`, read by the one workspace writer); the user's Mac shows classic scrollbars, so a fixed-height scrolling area reserves its scrollbar (`[scrollbar-gutter:stable]`) or hides it.
- Commit after each task, staging only that task's files by path (never `git add -A`; if `git status` shows files staged by someone else, commit with `git commit -m … -- <paths>`). The message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Bastion tests can fail when re-run within about a minute (OpenSSH PerSourcePenalties): wait, or `docker compose -f dev/docker-compose.yml restart bastion`.

## Review Focus

1. **A document the user did not edit is never re-encoded.** ⌘S on an unchanged tab sends nothing; a whitespace-only edit saves without asking (spec §12); any other token difference — a key the server will sort, `1.10` that becomes `1.1`, a duplicate key it drops, an escaped `\ud800` that becomes U+FFFD — comes back as a preview and nothing is written until the user confirms. → Task 3 `test_diff_reports_each_kind_of_change`; Task 5 `test_a_save_that_changes_tokens_asks_first_then_saves_when_confirmed`, `test_a_save_without_token_changes_is_written_at_once`; Task 7 manual checks 1–3.
2. **A precision loss is never hidden behind cosmetic changes.** A document written by an SDK (keys in insertion order) gives one key-order change per object; a number whose value changes (π to float64, `1e-400` to `0`) or a dropped duplicate must be listed first and counted, whatever the number of key-order changes. → Task 3 `test_value_changes_are_listed_before_key_order`.
3. **Nothing overwrites what it should not.** A stale CAS (also one off by one beyond 2⁵³) is a conflict carrying the current CAS; a binary document is never turned into JSON; a deleted document is `document_missing`; a duplicate insert is `document_exists`; the expiry survives a save. → Task 4 `test_replace_checks_the_cas_and_keeps_the_expiry`, `test_replace_never_turns_a_binary_document_into_json`, `test_insert_twice_is_exists_and_remove_checks_the_cas`.
4. **A lost answer to a write is not sent again.** The connection drops after the UPDATE or INSERT went out: the user gets `write_outcome_unknown` ("may or may not have changed; Reload"), the statement was sent once, and the next save of a really-saved document reports a conflict instead of writing twice. → Task 5 `test_a_write_whose_answer_is_lost_is_not_sent_again`, `test_without_retry_a_failure_is_raised_once_and_never_sent_again` (C26); Task 4 `test_a_failed_read_after_a_write_is_an_unknown_outcome` (C25).
5. **Big documents stay within memory bounds and edits survive a restart.** A 4 MiB document in a response parses in ≤ 4× its text (≤ 8× for an array of short strings), the 5 MiB `u_big` reads in ≤ 3.5×; a dirty document tab is restored with its text and base CAS, a clean one is not written into `workspace.json` and loads from the server when shown. → Task 2 `test_a_big_document_costs_its_text_not_a_decoded_tree`; Task 4 `test_a_big_document_is_read_lean`; Task 6 `test_workspace_keeps_document_tabs` and manual checks 9–11.

---

## File Structure

```
backend/dbaguy_backend/
  sql/jsontext.py                         value_end() (the lean scan), json_problem(), without_strings(), json_path(),
                                          number_paths(), diff(); one_line() via a template replacement
  sql/offsets.py                          utf16_offset()
  adapters/couchbase/stream.py            nested row values located, never built (RowField.value / RawRow.value None)
  adapters/documents.py                   (new) DocumentStore, DocumentCapable: the engine-neutral contract
  adapters/couchbase/documents.py         (new) SqlppDocumentStore: statements, guards, conflict diagnosis
  adapters/couchbase/messages.py          is_server_error() (moved here from adapter.py)
  adapters/couchbase/result.py            CouchbaseResult.fetch_rows()
  adapters/couchbase/adapter.py           statement_rows(); write options of _internal_result / _begin; documents();
                                          capability documents=True
  domain/couchbase.py                     DocumentChange, ChangeCounts, ChangeKind; DocumentView, DocumentSaved,
                                          SavePreview, SaveAnswer; DOCUMENT_ID_MAX_BYTES, DOCUMENT_TEXT_LIMIT,
                                          CAS_PATTERN, CAS_MAX
  domain/errors.py                        DocumentMissing, DocumentConflict, DocumentExists, BinaryDocument,
                                          WriteOutcomeUnknown, InvalidJson, NumberOutOfRange, ReadOnlyProfile
  domain/workspace.py                     WorkspaceTab.kind "document", document_cas
  services/metadata_service.py            with_connection(…, retry=False)
  services/metadata_errors.py             imports is_server_error from messages
  services/document_service.py            (new) DocumentService: the save flow of spec §3.9.3
  api/couchbase.py                        POST /couchbase/documents/get, /save, /insert, /remove
  container.py                            AppContainer.documents
backend/tests/support.py                  scratch_key, put_raw_document, delete_documents, stored_document,
                                          scratch_documents
backend/tests/…                           see each task
package.json, package-lock.json           @codemirror/lang-json 6.0.2
src/lib/api/types.ts                      DocumentView, DocumentSaved, SavePreview, DocumentChange, ChangeCounts, SaveAnswer
src/lib/format.ts                         formatTimeUntil()
src/features/editor/{extensions.ts, CodeEditor.tsx}   language "json"; CodeEditorHandle.replaceAll()
src/stores/workspace.ts                   DocumentTabState, openDocument()
src/features/workspace/{workspaceFile.ts, pendingWorkspace.ts, Workspace.tsx, closeGuards.ts}
src/features/documents/                   (new) documentApi.ts, openDocument.ts, OpenDocumentDialog.tsx, useDocument.ts,
                                          DocumentTab.tsx, DocumentBar.tsx, SavePreviewDialog.tsx, ConflictDialog.tsx
src/features/explorer/{Explorer.tsx, kinds.tsx}
src/features/query/{QueryTab.tsx, ResultsArea.tsx, ResultGrid.tsx}
src/app/AppShell.tsx                      OpenDocumentDialogHost
CLAUDE.md, docs/superpowers/specs/2026-10-18-04-couchbase-design.md
```

Task order follows dependencies. The probes and test helpers (1) come first: their results decide the store's statements. The lean scan and decoder (2) and the pure save checks (3) need no cluster code; the store (4) builds on both; the service and API (5) on the store. The frontend then opens and shows documents (6, which also adds the workspace's document fields) and saves, creates and deletes them (7). Packaging, measurements and docs (8) close.

---
### Task 1: Probes A2, A3 and A27–A31, and scratch-document test helpers

**Files:**
- Modify: `backend/tests/support.py`, `backend/tests/test_dev_env.py`
- Modify (only when a probe result differs from the table): this plan's "Assumptions recorded during execution" table
- Scratchpad only, not committed: `<scratchpad>/t1_probe.py`

**Interfaces:**
- Consumes: `tests.support.couchbase_query_text`, `_basic`, `COUCHBASE_HOST`, `COUCHBASE_MANAGER_PORT`, `COUCHBASE_USER`, `COUCHBASE_PASSWORD`; `dbaguy_backend.sql.dialects.keyspace_path`; `dbaguy_backend.adapters.couchbase.stream.ResponseParser`, `RowEvent`, `FieldEvent`.
- Produces (`tests/support.py`, used by Tasks 4, 5, 8):
  - `SCRATCH_SCOPE = "app"`, `SCRATCH_COLLECTION = "orders"`, `SCRATCH_PREFIX = "zz_04c_"`.
  - `scratch_key(tag: str = "") -> str` — `SCRATCH_PREFIX + tag + uuid4().hex[:10]`.
  - `put_raw_document(key: str, text: str, *, expiry: int | None = None, scope: str = SCRATCH_SCOPE, collection: str = SCRATCH_COLLECTION) -> None` — `POST /pools/default/buckets/dbaguy/scopes/{scope}/collections/{collection}/docs/{urllib.parse.quote(key, safe="")}` on the cluster manager (`COUCHBASE_MANAGER_PORT`, Basic auth of the admin), form-encoded `value=<text>` and `expiry=<n>` when given; asserts HTTP 200. Stores the text byte for byte (the seed's `doc`); a text that is not JSON is stored as a binary document (`META().type` `"base64"`); `expiry` is an absolute Unix time ≤ 2147483647 (A31).
  - `delete_documents(*keys: str, scope: str = SCRATCH_SCOPE, collection: str = SCRATCH_COLLECTION) -> None` — `DELETE` on the same endpoint for every key, then one assertion over all of them (one failure must not leave the later keys behind). Fine: 200, or **400 whose body holds `Key doesn't exist`** — that is how the endpoint answers a key that is already gone (pre-flight, 7.6.12: `{"error":"bad_request","reason":"Key doesn't exist"}`); a 404 means the collection is missing and fails, as does anything else (ruling C23: with "200 or 404" every test that removes its own document errors in teardown, and the probe's `finally` stops at the deleted `_ins` key and leaves the binary `_bin` document in `app.orders`). (The KV endpoint deletes even when a SQL++ `DELETE` gets TMPFAIL, A32.)
  - `class StoredDocument(NamedTuple): text: str; cas: str; expiration: int; type: str` and `stored_document(key: str, *, scope: str = SCRATCH_SCOPE, collection: str = SCRATCH_COLLECTION) -> StoredDocument | None` — sends `SELECT META(d).cas AS cas, META(d).expiration AS e, META(d).type AS t, d FROM <keyspace_path("dbaguy", scope, collection)> AS d USE KEYS $k` with `{"$k": key}` through `couchbase_query_text`, parses the answer with `ResponseParser` (`feed(text.encode()) + finish()`) and builds the tuple from the first row's fields: `d`'s and `cas`'s exact texts, `int(e)`, `t`'s value; None without a row.
  - `scratch_documents() -> Iterator[list[str]]` (a `@contextmanager`): yields a list the test appends its keys to, and calls `delete_documents(*keys)` in `finally`.

- [ ] **Step 1: Write the helpers' test** in `backend/tests/test_dev_env.py` (import the new names from `tests.support`):

```python
@requires_couchbase
def test_scratch_documents_are_stored_as_sent_and_removed():
    with scratch_documents() as keys:
        text, binary, expiring = scratch_key("t"), scratch_key("b"), scratch_key("x")
        keys += [text, binary, expiring]
        put_raw_document(text, '{"b": 1,\n "a": 1.10}')
        put_raw_document(binary, "not json")
        put_raw_document(expiring, '{"a":1}', expiry=2_000_000_000)
        assert stored_document(text).text == '{"b": 1,\n "a": 1.10}'
        assert stored_document(text).cas.isdigit() and stored_document(text).type == "json"
        assert stored_document(binary).type == "base64"
        assert stored_document(expiring).expiration == 2_000_000_000
    assert [stored_document(key) for key in (text, binary, expiring)] == [None, None, None]
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_dev_env.py -k scratch`
Expected: FAIL (ImportError: cannot import name `scratch_documents`).

- [ ] **Step 3: Write the helpers** in `backend/tests/support.py` per Interfaces (add `import urllib.parse`; import `keyspace_path` and the stream names inside `stored_document` or at module top).

- [ ] **Step 4: Run it to see it pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_dev_env.py`
Expected: PASS.

- [ ] **Step 5: Run the probes.** Save as `<scratchpad>/t1_probe.py` and run from the repository root with `backend/.venv/bin/python <scratchpad>/t1_probe.py` (about 10 seconds; it writes only `zz_04c_t1_*` keys in `dbaguy.app.orders` and removes them by exact key):

```python
"""04c Task 1 probes against the dev cluster: A2 (SELECT RAW $doc is what SET d = $doc stores), A3 (non-object
documents, the INSERT and DELETE forms), A27 (CAS compared exactly), A28 (RETURNING expiration after preserve_expiry),
A29 (duplicate INSERT), A30 (the binary guard), A31 (the console's docs endpoint)."""
import base64
import http.client
import json
import sys
import uuid

sys.path.insert(0, "backend")
from dbaguy_backend.adapters.couchbase.stream import FieldEvent, ResponseParser, RowEvent  # noqa: E402
from tests.support import delete_documents, put_raw_document, stored_document  # noqa: E402

AUTH = "Basic " + base64.b64encode(b"dbaguy:dbaguy").decode()
KS = "dbaguy.app.orders"
KEY = f"zz_04c_t1_{uuid.uuid4().hex[:8]}"
KEYS = [KEY, KEY + "_ins", KEY + "_bin"]
UPDATE = (f'UPDATE {KS} AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas AND META(d).type = "json" '
          "RETURNING META(d).cas AS cas, META(d).expiration AS expiration")
UNGUARDED = f"UPDATE {KS} AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas RETURNING META(d).cas AS cas"
INSERT = f"INSERT INTO {KS} AS d (KEY, VALUE) VALUES ($id, $doc) RETURNING META(d).cas AS cas"
DELETE = (f'DELETE FROM {KS} AS d USE KEYS $id WHERE META(d).cas = $cas AND META(d).type = "json" '
          "RETURNING RAW META(d).id")
TEXTS = [
    '{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}',
    '{"big":9007199254740993,"i64max":9223372036854775807,"beyond":12345678901234567890,"dec":1.10,'
    '"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400}',
    '{"a":1,"b":3,"a":2}',
    '{"s":"<>& \\u2028 é \\u00e9 \\/ \\" \\u0000 \\ud83d\\ude00 \\t","e":{},"ar":[],"k<":"v>"}',
    '{"x":-0,"y":0.0,"z":1.0,"w":1e2,"t":1.5e300}',
    '"<a string document>"', '[3,1,{"b":1,"a":2}]', "42.50", "null", "true",
]


def send(statement, extra=None, **literals):
    """One request; `literals` are JSON texts embedded unchanged, as DbaGuy's body builder does. (row texts, errors)."""
    parts = [f'"statement":{json.dumps(statement)}', *(f'"${name}":{text}' for name, text in literals.items()),
             *(f'"{name}":{text}' for name, text in (extra or {}).items())]
    conn = http.client.HTTPConnection("127.0.0.1", 8093, timeout=60)
    conn.request("POST", "/query/service", body=("{" + ",".join(parts) + "}").encode(),
                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    data = conn.getresponse().read()
    conn.close()
    parser = ResponseParser()
    events = parser.feed(data) + parser.finish()
    errors = next((e.value for e in events if isinstance(e, FieldEvent) and e.name == "errors"), None)
    return [e.row.text for e in events if isinstance(e, RowEvent)], errors


key = json.dumps(KEY)
try:
    put_raw_document(KEY, '{"init": 1}', expiry=2_000_000_000)
    for text in TEXTS:
        server = send("SELECT RAW $doc", doc=text)[0][0]
        returned, errors = send(UPDATE, {"preserve_expiry": "true"}, id=key, doc=text, cas=stored_document(KEY).cas)
        stored = stored_document(KEY)
        print("A2", "SAME" if server == stored.text else "DIFFERENT", "| server:", server, "| stored:", stored.text)
        print("   A28 returned", returned, errors, "| stored expiration", stored.expiration)
    for text in ('{"h":1e400}', '{"h":2e308}', '{"i":9223372036854775808}'):
        print("A2 limits", text, send("SELECT RAW $doc", doc=text))
    cas = int(stored_document(KEY).cas)
    for delta in (1, -1, 255):
        print("A27 cas", f"{delta:+d}", send(UPDATE, id=key, doc='{"w":1}', cas=str(cas + delta))[0])
    inserted = json.dumps(KEY + "_ins")
    print("A3 insert a string", send(INSERT, id=inserted, doc='"a string"'))
    print("A29 insert again", send(INSERT, id=inserted, doc="{}")[1])
    ins_cas = int(stored_document(KEY + "_ins").cas)
    print("A3 delete, wrong cas", send(DELETE, id=inserted, cas=str(ins_cas + 1))[0],
          "| right cas", send(DELETE, id=inserted, cas=str(ins_cas))[0],
          "| again", send(DELETE, id=inserted, cas=str(ins_cas))[0])
    put_raw_document(KEY + "_bin", "not json")
    binary = stored_document(KEY + "_bin")
    print("A31 binary", binary.type, binary.text)
    print("A30 guarded", send(UPDATE, id=json.dumps(KEY + "_bin"), doc='{"x":1}', cas=binary.cas)[0],
          stored_document(KEY + "_bin").type)
    print("A30 unguarded", send(UNGUARDED, id=json.dumps(KEY + "_bin"), doc='{"x":1}', cas=binary.cas)[0],
          stored_document(KEY + "_bin").type)
finally:
    delete_documents(*KEYS)
    print("left behind:", [k for k in KEYS if stored_document(k) is not None])
```

Expected (what planning saw on 2026-10-19; the table below has the details): every A2 line `SAME` except the escapes line (`DIFFERENT`: the stored text has `<>&` and the key `k<` / value `v>` escaped, the server form `<>&` raw in values); A28 `returned … "expiration":0` with stored expiration `2000000000`; `1e400` and `2e308` refused with 1040, `9223372036854775808` answered `9223372036854775807`; A27 `[]` three times; A3 insert one row, A29 code 12009 with `reason.code` 17012; A3 delete `[]`, then the id, then `[]`; A31 `base64 "<binary (8 b)>"`; A30 guarded `[]` and still `base64`, unguarded one row and `json`; `left behind: []` (the `finally` deletes the `_ins` key the probe already removed: `delete_documents` must take the endpoint's 400 `Key doesn't exist` for it and go on to `_bin`, ruling C23). The pre-flight ran this probe (keys `zz_preflight04c_t1_*`) on 2026-10-19: every line as described.

- [ ] **Step 6: Record the results.** If a line differs from the table, write the result over that row's Result column and apply its Effect (each row says what changes for both outcomes); otherwise leave the plan as it is.

- [ ] **Step 7: Run the whole backend suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green, no `requires_couchbase` test skipped.

```bash
git add backend/tests/support.py backend/tests/test_dev_env.py
git commit -m "test(couchbase): scratch-document helpers for 04c; A2, A3, A27–A31 probed"
```

(Stage the plan file too if Step 6 changed it.)

---

### Task 2: The lean scan: nested values in rows are located, never built

**Files:**
- Modify: `backend/dbaguy_backend/sql/jsontext.py` (`NotJson`, `value_end`)
- Modify: `backend/dbaguy_backend/adapters/couchbase/stream.py`
- Create: `backend/tests/sql/test_jsontext.py`
- Modify: `backend/tests/test_couchbase_stream.py`

Breaks on purpose: `test_scalar_rows_keep_their_text` expects `RowField("a", "{}", {})`; the nested value is now None (`RowField("a", "{}", None)`), updated in Step 1.

**Interfaces:**
- Produces (`sql/jsontext.py`, used by Tasks 3 and 4):
  - `class NotJson(ValueError)` — NaN or Infinity (Python's decoder reads them; JSON has no such values).
  - `value_end(text: str, start: int) -> int` — the index right after the JSON value that begins at `text[start]` (no whitespace before it), found and checked by the C decoder with hooks that keep nothing; raises `json.JSONDecodeError` (no complete value there), `NotJson`, or `RecursionError`.
- Produces (`adapters/couchbase/stream.py`): `RowField.value` and `RawRow.value` are None for an object or an array (strings, `true`, `false`, `null` and numbers-as-text as before); every text is unchanged. `parse_row(row.text) == row` still holds.

Rules (ruling C9):
- In a row (and in a member of an object row, and in `split_object` / `parse_row`), a value starting with `{` or `[` is located with `value_end`, its text kept, its value None. Every other value goes through `_ROW_DECODER` as before. Top-level fields (signature, errors, metrics, profile…) keep `_META_DECODER` and their decoded values.
- Failure mapping is unchanged: a `JSONDecodeError` goes through `_failure` (wait for more, cut, or malformed); `NotJson` and `RecursionError` are malformed (`_take` already catches `ValueError` and `RecursionError`).

- [ ] **Step 1: Write the failing tests.** New `backend/tests/sql/test_jsontext.py`:

```python
import json

import pytest

from dbaguy_backend.sql.jsontext import NotJson, value_end


def test_value_end_finds_where_a_value_ends_without_building_it():
    text = '{"a":[1,{"b":"]} \\" ["}],"c":-0.0e+1} tail'
    assert value_end(text, 0) == text.index(" tail")
    assert value_end("  [1, 2]", 2) == 8
    assert (value_end('"x",', 0), value_end("12,", 0), value_end("null]", 0)) == (3, 2, 4)


@pytest.mark.parametrize("text, error", [
    ('{"a":', json.JSONDecodeError), ("[1,]", json.JSONDecodeError), ('{"a":tru}', json.JSONDecodeError),
    ("[NaN]", NotJson), ('{"a":-Infinity}', NotJson),
])
def test_value_end_refuses_what_is_not_json(text, error):
    with pytest.raises(error):
        value_end(text, 0)
```

In `backend/tests/test_couchbase_stream.py`: change `test_scalar_rows_keep_their_text`'s last line to `assert rows[7].fields == (RowField("a", "{}", None),)` and add (with `import tracemalloc`, `from dbaguy_backend.adapters.couchbase.stream import RawRow`, `parse_row`):

```python
def test_nested_values_are_located_not_built():  # ruling C9: a cell needs the text only
    rows = [e.row for e in parse(FULL, []) if isinstance(e, RowEvent)]
    assert rows[0].fields[1].value is None and rows[0].fields[1].text.startswith('{"name":"中文😀"')
    assert rows[2].fields[1].value == "just a string"
    raw = [e.row for e in parse(RAW, []) if isinstance(e, RowEvent)]
    assert (raw[6].text, raw[6].value) == ("[1, 2]", None)
    assert [f.value for f in split_object('{"a":{"x":1e400},"b":[],"c":"s","n":-1.10}')] == [None, None, "s", "-1.10"]
    assert all(parse_row(row.text) == row for row in rows + raw)


@pytest.mark.parametrize("data", [
    b'{"results":[{"a":[1,}]}', b'{"results":[[1,{"x":tru}]]}', b'{"results":[{"a":{"b":NaN}}]}',
    b'{"results":[{"a":{"b":1 "c":2}}]}',
])
def test_malformed_nested_values_are_still_reported(data):
    for cut in range(len(data) + 1):
        events, error = parse_until_failure(data, [cut])
        assert not error.truncated, cut


def _big_document(shape: str, size: int) -> str:
    """A JSON object of about `size` characters: many small objects, many numbers, or many short strings."""
    rng = random.Random(7)
    parts, length, i = [], 0, 0
    while length < size:
        if shape == "objects":
            part = json.dumps({"id": i, "name": f"name {i}", "price": round(rng.random() * 1000, 2),
                               "tags": ["a", "b", str(i)], "nested": {"x": i, "y": [1, 2, {"z": 'q"<>'}], "f": True}})
        elif shape == "numbers":
            part = str(rng.randrange(10**6))
        else:
            part = json.dumps(f"s{i}")
        parts.append(part)
        length += len(part) + 1
        i += 1
    return '{"items":[' + ",".join(parts) + "]}"


def _first_row(data: bytes) -> RawRow:
    parser, events = ResponseParser(), []
    for i in range(0, len(data), 64 * 1024):  # as the wire delivers it
        events += parser.feed(data[i:i + 64 * 1024])
    events += parser.finish()
    return next(e.row for e in events if isinstance(e, RowEvent))


@pytest.mark.parametrize("shape, peak_limit", [("objects", 4.0), ("numbers", 4.0), ("strings", 8.0)])
def test_a_big_document_costs_its_text_not_a_decoded_tree(shape, peak_limit):  # Review Focus 5, ruling C9
    doc = _big_document(shape, 4 * 1024 * 1024)
    data = ('{"signature":{"cas":"json","expiration":"json","type":"json","d":"json"},"results":['
            '{"cas":1792325990605258752,"expiration":0,"type":"json","d":' + doc + '}],"status":"success"}').encode()
    tracemalloc.start()
    try:
        row = _first_row(data)
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert row.fields[3].text == doc and row.fields[3].value is None
    assert peak < peak_limit * len(doc), peak / len(doc)  # decoding the tree took 9.3×, 11.1× and 8.7×
    assert held < 2.5 * len(doc), held / len(doc)  # the row's text and the member's text
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_jsontext.py tests/test_couchbase_stream.py`
Expected: FAIL — ImportError for `value_end`; `test_nested_values_are_located_not_built` sees a dict; the memory test's peak is about 9× (objects).

- [ ] **Step 3: Add `NotJson` and `value_end` to `sql/jsontext.py`**, and update its module docstring (it now also locates values without building them):

```python
class NotJson(ValueError):
    """NaN or Infinity: Python's decoder reads them, JSON has no such values."""


def _forget(_value: object) -> None:
    return None


def _not_json(name: str) -> NoReturn:
    raise NotJson(f"{name} is not JSON")


# The C decoder with hooks that keep nothing: each object and number becomes None as soon as it has been read, so
# finding where a value ends costs its text, not a tree (an array still holds its elements until it closes: an array of
# many short strings peaks at about 7× its text). Strings are still decoded, which also checks their escapes.
_LEAN = json.JSONDecoder(object_pairs_hook=_forget, parse_int=_forget, parse_float=_forget, parse_constant=_not_json)


def value_end(text: str, start: int) -> int:
    """The index right after the JSON value that begins at text[start] (no whitespace before it), checked as
    `json.loads` would check it; nothing of it is kept. json.JSONDecodeError when there is no complete value there,
    NotJson for NaN or Infinity, RecursionError for nesting deeper than Python allows."""
    return _LEAN.raw_decode(text, start)[1]
```

- [ ] **Step 4: Use it in `adapters/couchbase/stream.py`.** `_decode` takes a scan function instead of a decoder (its body is unchanged except `scan(s, i)` in place of `decoder.raw_decode(s, i)`), and one helper chooses the scan for a row value:

```python
from collections.abc import Callable

from ...sql.jsontext import value_end

_Scan = Callable[[str, int], tuple[Any, int]]
_META = _META_DECODER.raw_decode
_SCALAR = _ROW_DECODER.raw_decode  # strings, numbers (as text), true, false, null


def _located(s: str, i: int) -> tuple[None, int]:
    """An object or array inside a row: where it ends, checked but never built (its text is all a cell needs)."""
    return None, value_end(s, i)


def _row_value(s: str, i: int, eof: bool) -> tuple[Any, int]:
    """A row, or a member of an object row: an object or array is located only (value None), anything else decoded."""
    return _decode(_located if s[i:i + 1] in ("{", "[") else _SCALAR, s, i, eof)
```

Callers: `_scan_object`'s member value → `_row_value(s, i, eof)`; `_step`'s `VALUE` → `_decode(_META, s, i, eof)`; `_row`'s non-object row → `_row_value(s, i, eof)`; `parse_row`'s non-object row → `_row_value(text + ",", 0, eof=True)`. Update the module docstring ("Rows keep the server's exact text…": a nested value is located with `jsontext.value_end` and never decoded — a 20 MiB document costs about three times its text while it is parsed, not a tree) and the `RowField` / `RawRow` docstrings (`value` is None for an object or array).

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_jsontext.py tests/test_couchbase_stream.py tests/test_couchbase_rows.py tests/test_couchbase_result.py tests/test_couchbase_read_ahead.py tests/test_couchbase_sampling.py`
Expected: PASS (the memory test: about 3.0× / 3.0× / 6.8× peak, 2.0× held).

- [ ] **Step 6: Check throughput did not regress.** Run the Couchbase integration files, which hold 04a's absolute budget (`100k rows, 500-row pages, encode_row included ≤ 4.0 s`): `cd backend && .venv/bin/pytest -q tests/test_couchbase_adapter.py tests/test_couchbase_sessions.py`. Expected: PASS. (Planning measured 100,000 browse-shaped rows parsed in 0.59 s against 0.57 s before.)

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/sql/jsontext.py backend/dbaguy_backend/adapters/couchbase/stream.py \
  backend/tests/sql/test_jsontext.py backend/tests/test_couchbase_stream.py
git commit -m "perf(couchbase): nested values in rows are located, never built — a document costs its text, not a tree"
```

---
### Task 3: Pure save checks: JSON problems, number paths, the save diff

**Files:**
- Modify: `backend/dbaguy_backend/sql/jsontext.py` (`one_line` reimplemented; `JsonProblem`, `json_problem`, `without_strings`, `json_path`, `number_paths`, `DocumentDiff`, `diff`; the constants `CHANGES_PER_KIND`, `CHANGES_LISTED`, `CHANGE_TEXT_LIMIT`, `KEYS_SHOWN`)
- Modify: `backend/dbaguy_backend/sql/offsets.py` (`utf16_offset`)
- Modify: `backend/dbaguy_backend/domain/couchbase.py` (`ChangeKind`, `DocumentChange`, `ChangeCounts`)
- Test: `backend/tests/sql/test_jsontext.py`, `backend/tests/sql/test_dialects_offsets.py`

**Interfaces:**
- Consumes: `value_end`, `NotJson` (Task 2).
- Produces (used by Tasks 4 and 5):
  - `domain/couchbase.py`:
    ```python
    ChangeKind = Literal["key_order", "number", "duplicate_key", "other"]

    class DocumentChange(BaseModel):
        """One thing the server would store differently from the edited text (spec §3.9.3, ruling C2)."""

        path: str  # JSONPath as the value viewer writes it: $, $.address, $.items[0], $["odd key"]
        kind: ChangeKind
        before: str  # the edited side: a literal, a key list or a dropped value (≤ 200 characters, then "…")
        after: str  # the server's side
        value_changed: bool  # something is lost or changed; false for reordered keys and a respelled equal number

    class ChangeCounts(BaseModel):
        key_order: int = 0
        number: int = 0
        duplicate_key: int = 0
        other: int = 0
    ```
  - `sql/jsontext.py`: `one_line(text: str) -> str` (same contract, cheaper); `@dataclass(frozen=True) class JsonProblem: offset: int` (code points) `; reason: str` (a fixed text: Python's decoder's own words or ours, never a quote of the text); `json_problem(text: str) -> JsonProblem | None`; `without_strings(text: str) -> str` (every JSON string replaced by `""`); `json_path(steps: Sequence[str | int]) -> str`; `number_paths(text: str, wanted: Callable[[str], bool], limit: int) -> tuple[list[str], int]` (paths of at most `limit` numbers whose literal `wanted` accepts, in document order, and how many there are); `@dataclass(frozen=True) class DocumentDiff: changes: list[DocumentChange]; counts: ChangeCounts`; `diff(edited: str, server: str) -> DocumentDiff`. `number_paths` and `diff` take valid JSON only (`json_problem` is None) and may raise `RecursionError` for nesting deeper than Python allows.
  - `sql/offsets.py`: `utf16_offset(text: str, cp: int) -> int` — `len(text[:cp].encode("utf-16-le", "surrogatepass")) // 2` with `cp` clamped to `[0, len(text)]` (C speed: no Python loop over a 20 MiB text, as `Utf16Index` would make).

Rules (rulings C1–C3):
- `one_line` becomes `"".join(_RUN.findall(text))` with `_RUN = re.compile(r'(?:"(?:[^"\\]++|\\.)*+"|[^ \t\n\r"]++)++')`: runs of tokens with no whitespace between them. Same output as today for valid JSON (its existing test, `test_one_line_squeezes_only_the_space_between_tokens`, stays green), but a compact text is one run (no copy), where the substitution built one object per string token (11.6× a token-dense text, measured; a pretty-printed one 4.4× instead of 5.8×).
- A change's `before` / `after` are `CHANGE_TEXT_LIMIT` characters at most, then `…`. A key list names `KEYS_SHOWN` keys, each written as a JSON string, then `, … (N keys)`. A lone surrogate (a decoded `\ud800` key) is written back as its escape in paths and key lists, so an answer always encodes as UTF-8.
- `json_path` follows `features/grid/json.ts`'s `jsonPath`: `.key` when the key matches `[A-Za-z_$][A-Za-z0-9_$]*`, else `[` + the key as a JSON string (non-ASCII kept) + `]`; `[n]` for an index.
- `diff` compares strings by their decoded value (A2: the server's form spells `é`, `\/` and surrogate escapes as characters, and the stored text writes `<`, `>`, `&` as `<`, `>`, `&`: none of that is a change), numbers by their literal (`value_changed` when `Decimal(a) != Decimal(b)`, or when either does not parse), objects by their distinct keys in first-occurrence order against the server's order (a key set that differs is `other`), arrays element by element (a length that differs is `other`, `N element(s)` on each side). Kept: `CHANGES_PER_KIND` per kind; listed: the value changes first, then the rest, each group in kind order `other`, `duplicate_key`, `number`, `key_order`, at most `CHANGES_LISTED`; `counts` counts everything.

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/sql/test_jsontext.py`; import the new names, `ChangeCounts` and `DocumentChange` from `dbaguy_backend.domain.couchbase`, `tracemalloc`):

```python
@pytest.mark.parametrize("text, offset, reason", [
    ("", 0, "Expecting value"), ("{", 1, "Expecting property name"), ('{"a":1,}', 6, "Illegal trailing comma"),
    ('{"😀": 1,}', 7, "Illegal trailing comma"), ('{"a":"x\ty"}', 7, "Invalid control character"),
    ('{"a":NaN}', 5, "NaN and Infinity are not JSON"), ('{"a":"NaN","b":-Infinity}', 15, "NaN and Infinity"),
    ("[1] x", 4, "Extra data"), ("﻿{}", 0, "Expecting value"),
])
def test_json_problem_points_at_the_first_bad_spot(text, offset, reason):
    problem = json_problem(text)
    assert problem.offset == offset and problem.reason.startswith(reason)


def test_json_problem_accepts_one_value_with_space_around_it():
    assert json_problem('  {"a": [1, 2]}  \n') is None and json_problem('"s"') is None


def test_without_strings_keeps_numbers_and_structure():
    assert without_strings('{"a]":"x\\"y",  "b":[1,"2",1e400]}') == '{"":"",  "":[1,"",1e400]}'


def test_json_path_is_written_like_the_value_viewer():
    assert json_path(()) == "$"
    assert json_path(("a", 0, "b c", "$x", "中", "\ud800")) == '$.a[0]["b c"].$x["中"]["\\ud800"]'


def test_number_paths_finds_numbers_by_their_literal():
    text = '{"a":1,"b":{"c":[1, 123456789012345678901,{"d":2}]},"odd key":1e400,"s":"1e400","n":[1e400]}'
    wanted = lambda literal: literal.endswith("e400") or len(literal) > 18  # noqa: E731
    assert number_paths(text, wanted, 50) == (["$.b.c[1]", '$["odd key"]', "$.n[0]"], 3)
    assert number_paths(text, wanted, 1) == (["$.b.c[1]"], 3)
    assert number_paths('"just a string"', wanted, 50) == ([], 0)


def change(path, kind, before, after, value_changed):
    return DocumentChange(path=path, kind=kind, before=before, after=after, value_changed=value_changed)


@pytest.mark.parametrize("edited, server, expected", [
    ('{"zeta": 1,\n "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}',
     '{"alpha":{"a":2,"z":1},"mid":"a b","ws":[1,2],"zeta":1}',
     [change("$", "key_order", '"zeta", "alpha", "ws", "mid"', '"alpha", "mid", "ws", "zeta"', False),
      change("$.alpha", "key_order", '"z", "a"', '"a", "z"', False)]),
    ('{"a":1,"b":3,"a":2}', '{"a":2,"b":3}', [change("$.a", "duplicate_key", "1", "2", True)]),
    ('{"x":-0,"y":0.0,"pi":3.141592653589793238462643383279,"t":1e-400}',
     '{"pi":3.141592653589793,"t":0,"x":0,"y":0}',
     [change("$.pi", "number", "3.141592653589793238462643383279", "3.141592653589793", True),
      change("$.t", "number", "1e-400", "0", True), change("$.x", "number", "-0", "0", False),
      change("$.y", "number", "0.0", "0", False),
      change("$", "key_order", '"x", "y", "pi", "t"', '"pi", "t", "x", "y"', False)]),
    ('[3,1,{"b":1,"a":2}]', '[3,1,{"a":2,"b":1}]', [change("$[2]", "key_order", '"b", "a"', '"a", "b"', False)]),
    ("42.50", "42.5", [change("$", "number", "42.50", "42.5", False)]),
    ('{"a":"\\ud800"}', '{"a":"\\ufffd"}', [change("$.a", "other", '"\\ud800"', '"\\ufffd"', True)]),
    ('{"a":1}', '{"a":"1"}', [change("$.a", "other", "1", '"1"', True)]),
    ('{"a":1}', '{"b":1}', [change("$", "other", '"a"', '"b"', True)]),
    ("[1,2]", "[1]", [change("$", "other", "2 elements", "1 element", True)]),
])
def test_diff_reports_each_kind_of_change(edited, server, expected):  # Review Focus 1
    assert diff(edited, server).changes == expected


@pytest.mark.parametrize("edited, server", [
    ('{\n  "a": [1, 2],\n  "b": {"c": null}\n}', '{"a":[1,2],"b":{"c":null}}'),  # whitespace only (spec §12)
    ('{"s":"\\u00e9 \\u003c \\/ \\ud83d\\ude00","t":"<&>"}', '{"s":"é < / 😀","t":"\\u003c\\u0026\\u003e"}'),  # A2
    ('"a string"', '"a string"'), ("[]", "[]"),
])
def test_whitespace_and_escape_spelling_are_not_changes(edited, server):
    assert diff(edited, server) == DocumentDiff(changes=[], counts=ChangeCounts())


def test_value_changes_are_listed_before_key_order():  # Review Focus 2: an SDK-written document, one precision loss
    edited = '{"items":[' + ",".join(f'{{"b":{i},"a":{i}}}' for i in range(300)) + '],"pi":3.141592653589793238462}'
    server = '{"items":[' + ",".join(f'{{"a":{i},"b":{i}}}' for i in range(300)) + '],"pi":3.141592653589793}'
    found = diff(edited, server)
    assert found.counts == ChangeCounts(key_order=300, number=1)
    assert (found.changes[0].path, found.changes[0].kind, found.changes[0].value_changed) == ("$.pi", "number", True)
    assert len(found.changes) == 1 + CHANGES_PER_KIND


def test_long_texts_and_key_lists_are_cut():
    keys = [f"k{i:02}" for i in range(12)]
    edited = "{" + ",".join(f'"{k}":1' for k in reversed(keys)) + ',"z":"' + "x" * 300 + '"}'
    server = '{"z":"' + "y" * 300 + '",' + ",".join(f'"{k}":1' for k in keys) + "}"
    other, order = diff(edited, server).changes
    assert other.before == '"' + "x" * 199 + "…" and len(other.after) == CHANGE_TEXT_LIMIT + 1
    assert order.before.endswith(", … (13 keys)") and order.before.count(", ") == KEYS_SHOWN


def test_diff_of_a_big_document_never_builds_a_tree():  # ruling C3; a tree of this text takes about 12×
    edited = '{"items":[' + ",".join(f'{{"name":"n{i}","b":{i},"a":[{i},{{"y":1,"x":2}}]}}' for i in range(5000)) + "]}"
    server = '{"items":[' + ",".join(f'{{"a":[{i},{{"x":2,"y":1}}],"b":{i},"name":"n{i}"}}' for i in range(5000)) + "]}"
    pretty = json.dumps(json.loads(edited), indent=2)
    for text in (edited, pretty):
        tracemalloc.start()
        try:
            found = diff(text, server)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert found.counts.key_order == 10000
        assert peak < 6 * len(text), peak / len(text)  # measured 2.0× (compact) and 4.4× (pretty)
```

In `backend/tests/sql/test_dialects_offsets.py` (import `utf16_offset`):

```python
def test_utf16_offset_counts_astral_characters():
    text = 'a😀b中\U0001F600'
    assert [utf16_offset(text, cp) for cp in range(-1, 7)] == [0, 0, 1, 3, 4, 5, 7, 7]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_jsontext.py tests/sql/test_dialects_offsets.py`
Expected: FAIL (ImportError: `json_problem`, `utf16_offset`).

- [ ] **Step 3: Add the models to `domain/couchbase.py`** and `utf16_offset` to `sql/offsets.py` per Interfaces.

- [ ] **Step 4: Write `json_problem`, `without_strings`, `json_path`, `one_line`** per Interfaces and Rules. `json_problem`:

```python
_SPACE = re.compile(r"[ \t\n\r]*")
_CONSTANT = re.compile(r'"(?:[^"\\]++|\\.)*+"|(NaN|-?Infinity)')


def json_problem(text: str) -> JsonProblem | None:
    """Where `text` stops being one JSON value with optional whitespace around it, or None. Checked by the C decoder
    with nothing kept (a 20 MiB text costs its text)."""
    start = _SPACE.match(text).end()
    try:
        end = value_end(text, start)
    except json.JSONDecodeError as exc:
        return JsonProblem(exc.pos, exc.msg)
    except NotJson:
        found = next((m for m in _CONSTANT.finditer(text, start) if m.group(1)), None)
        return JsonProblem(found.start(1) if found else start, "NaN and Infinity are not JSON")
    except RecursionError:
        return JsonProblem(start, "Nested too deeply")
    end = _SPACE.match(text, end).end()
    return None if end == len(text) else JsonProblem(end, "Extra data after the value")
```

- [ ] **Step 5: Write `number_paths` and `diff`.** Both walk level by level over compact text (`one_line` once), finding each member's or element's end with `value_end` and never building a value; `diff` skips any subtree whose compact texts are equal with one comparison and descends only where they differ:

```python
_PLAIN_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_NUMBER_START = frozenset("-0123456789")
_KIND_ORDER: tuple[ChangeKind, ...] = ("other", "duplicate_key", "number", "key_order")
Steps = tuple[str | int, ...]


def _members(text: str, start: int) -> list[tuple[str, int, int]]:
    """The members of the (valid) object at text[start]: (key, value start, value end), in order, duplicates kept."""
    members: list[tuple[str, int, int]] = []
    i = _SPACE.match(text, start + 1).end()
    if text[i] == "}":
        return members
    while True:
        key, i = scanstring(text, i + 1)
        i = _SPACE.match(text, _SPACE.match(text, i).end() + 1).end()  # past the colon
        end = value_end(text, i)
        members.append((key, i, end))
        i = _SPACE.match(text, end).end()
        if text[i] == "}":
            return members
        i = _SPACE.match(text, i + 1).end()  # past the comma


def _elements(text: str, start: int) -> Iterator[tuple[int, int]]:
    """The elements of the (valid) array at text[start], one at a time (an array can hold a million)."""
    i = _SPACE.match(text, start + 1).end()
    if text[i] == "]":
        return
    while True:
        end = value_end(text, i)
        yield i, end
        i = _SPACE.match(text, end).end()
        if text[i] == "]":
            return
        i = _SPACE.match(text, i + 1).end()


class _Found:
    """Changes as the walk meets them: counted all, kept CHANGES_PER_KIND per kind (paths built only when kept)."""

    def __init__(self) -> None:
        self.kept: dict[ChangeKind, list[DocumentChange]] = {kind: [] for kind in _KIND_ORDER}
        self.counts: dict[ChangeKind, int] = dict.fromkeys(_KIND_ORDER, 0)

    def add(self, steps: Steps, kind: ChangeKind, before: str, after: str, value_changed: bool) -> None:
        self.counts[kind] += 1
        if len(self.kept[kind]) < CHANGES_PER_KIND:
            self.kept[kind].append(DocumentChange(path=json_path(steps), kind=kind, before=before, after=after,
                                                  value_changed=value_changed))

    def result(self) -> DocumentDiff:
        ordered = [change for kind in _KIND_ORDER for change in self.kept[kind]]
        listed = [c for c in ordered if c.value_changed] + [c for c in ordered if not c.value_changed]
        return DocumentDiff(changes=listed[:CHANGES_LISTED], counts=ChangeCounts(**self.counts))


def _compare(e: str, es: int, ee: int, s: str, ss: int, se: int, steps: Steps, found: _Found) -> None:
    """The edited value e[es:ee] against the server's s[ss:se], both compact: equal tokens are equal text."""
    if ee - es == se - ss and e[es:ee] == s[ss:se]:
        return
    first, server_first = e[es], s[ss]
    if first == "{" and server_first == "{":
        last: dict[str, tuple[int, int]] = {}
        for key, start, end in _members(e, es):
            if key in last:  # the server keeps the last value of a repeated key
                dropped = last[key]
                found.add((*steps, key), "duplicate_key", _short(e[dropped[0]:dropped[1]]), _short(e[start:end]), True)
            last[key] = (start, end)
        server = _members(s, ss)
        order, server_order = list(last), [key for key, _, _ in server]
        if set(order) != set(server_order):
            found.add(steps, "other", _keys(order), _keys(server_order), True)
            return
        if order != server_order:
            found.add(steps, "key_order", _keys(order), _keys(server_order), False)
        for key, start, end in server:
            _compare(e, *last[key], s, start, end, (*steps, key), found)
    elif first == "[" and server_first == "[":
        edited_items, server_items = _elements(e, es), _elements(s, ss)
        for index, (a, b) in enumerate(zip_longest(edited_items, server_items)):
            if a is None or b is None:  # different lengths: counted to the end, said once
                left = index + (a is not None) + sum(1 for _ in edited_items)
                right = index + (b is not None) + sum(1 for _ in server_items)
                found.add(steps, "other", _count(left), _count(right), True)
                return
            _compare(e, a[0], a[1], s, b[0], b[1], (*steps, index), found)
    elif first == '"' and server_first == '"':
        if scanstring(e, es + 1)[0] != scanstring(s, ss + 1)[0]:
            found.add(steps, "other", _short(e[es:ee]), _short(s[ss:se]), True)
    elif first in _NUMBER_START and server_first in _NUMBER_START:
        a, b = e[es:ee], s[ss:se]
        found.add(steps, "number", _short(a), _short(b), not _same_value(a, b))
    else:
        found.add(steps, "other", _short(e[es:ee]), _short(s[ss:se]), True)


def diff(edited: str, server: str) -> DocumentDiff:
    """What storing `edited` would change, given `server`, the server's form of it (`SELECT RAW $doc`, A2): key order,
    number spelling, dropped duplicate keys, anything else as `other`. Whitespace and string escape spelling are not
    changes. Never builds a tree (ruling C3)."""
    found = _Found()
    edited, server = one_line(edited), one_line(server)
    _compare(edited, 0, len(edited), server, 0, len(server), (), found)
    return found.result()
```

`number_paths` walks the same way (`one_line` once; `_members` / `_elements` recursively; a value starting with one of `_NUMBER_START` whose literal `wanted` accepts is counted, and its `json_path` kept while fewer than `limit` are). The small helpers: `_short(text)` (≤ `CHANGE_TEXT_LIMIT`, then `…`), `_keys(keys)` (Rules), `_count(n)` (`"1 element"`, `"N elements"`), `_same_value(a, b)` (`Decimal(a) == Decimal(b)`, False on `InvalidOperation`), `_shown(text)` (`_LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)`, used by `json_path` and `_keys`). Imports: `json`, `re`, `Callable`, `Iterator`, `Sequence`, `dataclass`, `Decimal`, `InvalidOperation`, `zip_longest`, `scanstring`, `NoReturn`, and the domain models.

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/sql/test_jsontext.py tests/sql/test_dialects_offsets.py tests/sql/test_formatting.py tests/test_couchbase_sessions.py`
Expected: PASS (`test_formatting.py` holds `one_line`'s old test and the JSONL writer; `test_couchbase_sessions.py` its live JSONL export).

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/sql/jsontext.py backend/dbaguy_backend/sql/offsets.py \
  backend/dbaguy_backend/domain/couchbase.py backend/tests/sql/test_jsontext.py backend/tests/sql/test_dialects_offsets.py
git commit -m "feat(couchbase): save checks over JSON text — problems with offsets, number paths, a diff that never builds a tree"
```

---
### Task 4: The document store: `DocumentStore`, `SqlppDocumentStore`, the connection's statement rows

**Files:**
- Create: `backend/dbaguy_backend/adapters/documents.py`, `backend/dbaguy_backend/adapters/couchbase/documents.py`
- Modify: `backend/dbaguy_backend/adapters/couchbase/messages.py` (`is_server_error` moves here), `backend/dbaguy_backend/adapters/couchbase/adapter.py`, `backend/dbaguy_backend/adapters/couchbase/result.py`, `backend/dbaguy_backend/services/metadata_errors.py` (import from `messages`)
- Modify: `backend/dbaguy_backend/domain/couchbase.py`, `backend/dbaguy_backend/domain/errors.py`
- Create: `backend/tests/test_couchbase_documents.py`
- Modify: `backend/tests/test_api.py` (`test_engines`: Couchbase now has `documents`)

**Interfaces:**
- Consumes: Task 2 (`RowField.value` None for nested values: a member's `.text` is what is read); Task 3 (`one_line`, `without_strings`, `number_paths`); Task 1 (`put_raw_document`, `stored_document`, `StoredDocument`, `scratch_key`, `scratch_documents`); 04a/04b: `CouchbaseConnection._begin`, `_internal_result`, `_INTERNAL_DEADLINE`, `QueryRequest.preserve_expiry`, `keyspace_path`, `quote_literal`, `Keyspace`.
- Produces:
  - `domain/couchbase.py`: `DOCUMENT_ID_MAX_BYTES = 250`, `DOCUMENT_TEXT_LIMIT = 20 * 1024 * 1024`, `CAS_PATTERN = r"^[1-9][0-9]{0,18}$"`, `CAS_MAX = 2**63 - 1`;
    ```python
    class DocumentView(BaseModel):
        id: str
        cas: str  # decimal text: a CAS is beyond 2^53
        expiration: int  # Unix seconds; 0 = none
        type: Literal["json", "base64"]  # base64: a binary document, shown but never changed
        text: str | None  # the stored text exactly as the server holds it; None for a binary document
        size: int  # bytes of the text; for a binary document what the server reports

    class DocumentSaved(BaseModel):
        status: Literal["saved"]
        cas: str
        expiration: int  # as read right after the write (A28: RETURNING says 0)
        size: int  # bytes of the saved text without the whitespace between its tokens (ruling C21)
    ```
  - `domain/errors.py` (fixed messages, never an ID or a text): `DocumentMissing(NotFound)` code `document_missing` ("The document does not exist (it may have been deleted)"); `DocumentConflict(DomainError)` code `document_conflict`, status 409, `__init__(self, current_cas: str)` → message "The document was changed after it was opened", `detail={"cas": current_cas}`; `DocumentExists(DomainError)` code `document_exists`, 409 ("A document with this ID already exists"); `BinaryDocument(ValidationFailed)` code `binary_document` ("This is a binary document: DbaGuy shows it but never changes it"); `WriteOutcomeUnknown(DomainError)` code `write_outcome_unknown`, status 502 ("The connection to the query node was lost during the write: the document may or may not have changed. Reload it to see what the server holds"). Each takes no argument except `DocumentConflict`.
  - `adapters/documents.py` (engine-neutral, ruling C8):
    ```python
    class DocumentStore(ABC):
        """JSON documents by key (spec §3.9.1, R-04-2). Runs on the profile's metadata worker thread. Raises the domain
        errors above, QueryFailed for the server's own error answer (final, B39), WriteOutcomeUnknown for a write whose
        answer never came; a read's lost connection is a StatementError (MetadataService reconnects once)."""

        @abstractmethod
        def get(self, keyspace: Keyspace, doc_id: str) -> DocumentView | None: ...
        @abstractmethod
        def server_form(self, text: str) -> str: ...  # what this store would store for `text` (an SDK store: `text`)
        @abstractmethod
        def blocked_numbers(self, text: str) -> list[str]: ...  # paths of numbers it would change in value; refuse
        @abstractmethod
        def replace(self, keyspace: Keyspace, doc_id: str, text: str, cas: int) -> DocumentSaved: ...
        @abstractmethod
        def insert(self, keyspace: Keyspace, doc_id: str, text: str) -> DocumentSaved: ...
        @abstractmethod
        def remove(self, keyspace: Keyspace, doc_id: str, cas: int) -> None: ...

    class DocumentCapable(ABC):
        """A connection whose engine stores JSON documents by key (capability `documents`)."""

        @abstractmethod
        def documents(self) -> DocumentStore: ...
    ```
  - `adapters/couchbase/messages.py`: `is_server_error(exc: StatementError) -> bool` (moved unchanged; `adapter.py` imports it from here).
  - `adapters/couchbase/result.py`: `CouchbaseResult.fetch_rows(self, n: int) -> list[RawRow]` — rows as parsed (each value's exact text), through the same `_rows` path as `fetch` / `fetch_raw`.
  - `adapters/couchbase/adapter.py`: `CouchbaseConnection(DatabaseConnection, FieldSampling, DocumentCapable)`; `statement_rows(self, statement: str, named: Mapping[str, str] | None = None, *, write: bool = False, preserve_expiry: bool = False) -> list[RawRow]` (an internal statement read to its end; `named`: names without `$` → JSON literal texts); `documents(self) -> DocumentStore` (a `SqlppDocumentStore(self)`); `capabilities` gains `documents=True` (04a ruling P6 lifted).
  - `adapters/couchbase/documents.py`: `SqlppDocumentStore(statements: Statements)` with `class Statements(Protocol)` declaring `statement_rows` as above; `BLOCKED_PATHS_MAX = 50`, `INT64_MIN`, `INT64_MAX`; `changed_by_server(literal: str) -> bool`.

Rules (pre-flight C25 added):
- `_internal_result(statement, named, context, *, write=False, preserve_expiry=False)` and `_begin(…, preserve_expiry: bool = False)` (passed to `QueryRequest(preserve_expiry=…)`). A read keeps `readonly=True, retry_safe=True`; a write sends `readonly=self._read_only` (a read-only profile's write is refused by the server too) and `retry_safe=False` (never resent, D-04-9). Both keep `_INTERNAL_DEADLINE`. `query()` and `statement_rows()` share one reader (`_read_all(result) -> list[RawRow]`: `fetch_rows(1000)` until empty, then `next_result()` for an error after the rows, `close()` in `finally`); `query()` keeps decoding each row with `json.loads(row.text)`.
- Statements (`ks` is `keyspace_path(bucket, scope, collection)`; `$id` is `quote_literal(Dialect.SQLPP, doc_id)`; `$cas` is `str(cas)` after `0 < cas <= INT64_MAX`, else `ValidationFailed("Not a CAS value of this server")`; `$doc` is the text):

  ```python
  _GET = ("SELECT META(d).cas AS cas, META(d).expiration AS expiration, META(d).type AS type, d "
          "FROM {ks} AS d USE KEYS $id")
  _STATE = ("SELECT META(d).cas AS cas, META(d).expiration AS expiration, META(d).type AS type "
            "FROM {ks} AS d USE KEYS $id")
  _SERVER_FORM = "SELECT RAW $doc"
  # The type guard (A30): without it an UPDATE turns a binary document into JSON.
  _REPLACE = ('UPDATE {ks} AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas AND META(d).type = "json" '
              "RETURNING META(d).cas AS cas")
  _INSERT = "INSERT INTO {ks} AS d (KEY, VALUE) VALUES ($id, $doc) RETURNING META(d).cas AS cas"
  _REMOVE = ('DELETE FROM {ks} AS d USE KEYS $id WHERE META(d).cas = $cas AND META(d).type = "json" '
             "RETURNING RAW META(d).id")
  _CONCURRENT = "12009"  # a concurrent change, and also (INSERT) a duplicate key (A29: reason 17012)
  _BINARY_SIZE = re.compile(r'"<binary \((\d+) b\)>"')
  ```

- `get`: no row → None. Fields by name from the row (`RowField.text`; a field missing is `StatementError(PROTOCOL_ERROR, code="protocol_error")`): `type` is the member's decoded value; `"json"` → `DocumentView(text=<d's text>, size=_utf8_size(text))`; anything else → `type="base64"`, `text=None`, `size` from `_BINARY_SIZE` on `d`'s text (0 when it does not match). `cas` is the `cas` member's text, `expiration` `int` of its text.
- `server_form(text)`: the one row's text of `_SERVER_FORM` with `{"doc": text}`.
- `blocked_numbers(text)`: `[]` when `without_strings(text)` holds no `[0-9]{19,}` run and no exponent `[eE]\+?[0-9]{3,}` (the fast path: almost every save); else `number_paths(text, changed_by_server, BLOCKED_PATHS_MAX)[0]`. `changed_by_server(literal)`: an integer literal (`-?(?:0|[1-9][0-9]*)`) with more than 19 digits, or outside `INT64_MIN..INT64_MAX`; any other literal whose `float()` is infinite (ruling C4).
- Writes go through one helper: `statement_rows(…, write=True, preserve_expiry=…)`; a `StatementError` whose code is `_CONCURRENT` becomes a private `_Concurrent` exception (handled below), any other server error (`is_server_error`) → `QueryFailed(exc.message)`, anything else (lost connection, protocol error, cancel) → `WriteOutcomeUnknown()`.
- `_state(ks: str, key: str) -> _State | None` reads `_STATE` (`class _State(NamedTuple): cas: str; expiration: int; type: str`, from the members' texts and `type`'s value; a read, so retried after a lost connection like any read).
- `replace` / `remove`: no row (or `_Concurrent`) → `_state`: no row → `DocumentMissing()`; type not `"json"` → `BinaryDocument()`; else `DocumentConflict(state.cas)`. `insert`: `_Concurrent` → `_state` has a row → `DocumentExists()`, else `QueryFailed(<the server's message>)`.
- `replace` sends `preserve_expiry=True`. After `replace` or `insert` succeeds, `_state` is read for the expiration (A28): `DocumentSaved(status="saved", cas=<RETURNING's cas text>, expiration=<state's, 0 without a row>, size=_utf8_size(one_line(text)))`. `_utf8_size(text)` is `len(text)` when `text.isascii()`, else `len(text.encode("utf-8"))` (no 20 MiB copy for an ASCII document).
- A `StatementError` from that read after a successful write (a lost connection, or a server error such as TMPFAIL) is raised as `WriteOutcomeUnknown()`, never as it is (ruling C25): the write's answer came, so the document was written, but its META cannot be reported; raised as it is, it would reach the user as a failed save (`QueryFailed`), and the next ⌘S would meet a conflict. Reload shows what the server holds.
- The store logs nothing (the service does, Task 5); it never logs a text.

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_couchbase_documents.py`:

```python
"""SqlppDocumentStore against the dev cluster (spec §3.9.2): exact stored text and META, the server's form, blocked
numbers, CAS checks that keep the expiry, the binary guard, duplicates, documents that are not objects, a lean read.
Writes only zz_04c_ keys in dbaguy.app.orders (removed by key)."""

import tracemalloc

import pytest

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from dbaguy_backend.adapters.couchbase.documents import SqlppDocumentStore
from dbaguy_backend.adapters.couchbase.stream import parse_row
from dbaguy_backend.domain.couchbase import Keyspace
from dbaguy_backend.domain.errors import (
    BinaryDocument, DocumentConflict, DocumentExists, DocumentMissing, WriteOutcomeUnknown,
)
from tests.support import (
    COUCHBASE_READER, StoredDocument, put_raw_document, requires_couchbase, scratch_documents, scratch_key,
    stored_document,
)
from tests.test_couchbase_adapter import U_KEYORDER, open_couchbase
from tests.test_dev_env import U_NUMBERS

pytestmark = requires_couchbase
USERS = Keyspace(bucket="dbaguy", scope="app", collection="users")
BLOBS = Keyspace(bucket="dbaguy", scope="app", collection="blobs")
ORDERS = Keyspace(bucket="dbaguy", scope="app", collection="orders")
NEVER = "zz_04c_never_there"


@pytest.fixture
def connection():
    conn = open_couchbase()
    yield conn
    conn.close()


@pytest.fixture
def store(connection):
    return connection.documents()


@pytest.fixture
def scratch():
    with scratch_documents() as keys:
        yield keys


def test_get_keeps_the_stored_text_and_meta(store):  # Review Focus 5's exactness, R-04-2
    numbers = store.get(USERS, "u_numbers")
    assert (numbers.text, numbers.type, numbers.expiration) == (U_NUMBERS, "json", 0)
    assert numbers.size == len(U_NUMBERS.encode()) and numbers.cas == stored_document("u_numbers", collection="users").cas
    assert store.get(USERS, "u_keyorder").text == U_KEYORDER
    assert store.get(USERS, "u_ttl").expiration == 4102444800
    assert [store.get(USERS, key).text for key in ("s1", "a1", "n1")] == ['"just a string document"', "[1,2,3]", "42"]
    assert store.get(USERS, NEVER) is None


def test_binary_documents_show_their_size_only(store):
    assert store.get(BLOBS, "bin1").model_dump(exclude={"id", "cas"}) == {
        "expiration": 0, "type": "base64", "text": None, "size": 9}


def test_a_big_document_is_read_lean(store):  # Review Focus 5, ruling C9
    store.get(USERS, "u1")  # the connection is open and warm
    tracemalloc.start()
    try:
        big = store.get(USERS, "u_big")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(big.text) > 5_000_000 and big.size == len(big.text)
    assert peak < 3.5 * len(big.text), peak / len(big.text)  # planning: 3.0× lean, 4.0× with the row decoded


def test_server_form_is_what_the_server_would_store(store):  # A2
    assert store.server_form(U_KEYORDER) == '{"alpha":{"a":2,"z":1},"mid":"a b","ws":[1,2],"zeta":1}'


def test_blocked_numbers_name_their_paths(store):  # ruling C4
    assert store.blocked_numbers(U_NUMBERS) == ["$.u64max", "$.beyond", "$.huge"]
    fine = '{"i":9223372036854775807,"j":-9223372036854775808,"k":1.7976931348623157e308,"t":1e-400,"s":"1e400"}'
    assert store.blocked_numbers(fine) == []
    assert store.blocked_numbers("[9223372036854775808, -9223372036854775809, 2e308, 1234567890123456789012.5]") == [
        "$[0]", "$[1]", "$[2]"]


def test_replace_checks_the_cas_and_keeps_the_expiry(store, scratch):  # Review Focus 3, A27, A28
    key = scratch_key()
    scratch.append(key)
    put_raw_document(key, '{"b": 1, "a": 1.10}', expiry=2_000_000_000)
    old = store.get(ORDERS, key)
    saved = store.replace(ORDERS, key, '{"b": 2, "a": 1.10}', int(old.cas))
    assert saved.status == "saved" and saved.cas != old.cas and saved.expiration == 2_000_000_000
    assert stored_document(key) == StoredDocument('{"a":1.1,"b":2}', saved.cas, 2_000_000_000, "json")
    for stale in (int(old.cas), int(saved.cas) + 1, int(saved.cas) - 1):
        with pytest.raises(DocumentConflict) as conflict:
            store.replace(ORDERS, key, '{"c": 3}', stale)
        assert conflict.value.detail == {"cas": saved.cas}
    assert stored_document(key).text == '{"a":1.1,"b":2}'


def test_replace_never_turns_a_binary_document_into_json(store, scratch):  # Review Focus 3, A30
    key = scratch_key("bin")
    scratch.append(key)
    put_raw_document(key, "not json")
    binary = store.get(ORDERS, key)
    assert (binary.type, binary.text, binary.size) == ("base64", None, 8)
    with pytest.raises(BinaryDocument):
        store.replace(ORDERS, key, '{"x":1}', int(binary.cas))
    with pytest.raises(BinaryDocument):
        store.remove(ORDERS, key, int(binary.cas))
    assert stored_document(key).type == "base64"


def test_replace_and_remove_of_a_missing_document_say_so(store):
    with pytest.raises(DocumentMissing):
        store.replace(ORDERS, NEVER, "{}", 1)
    with pytest.raises(DocumentMissing):
        store.remove(ORDERS, NEVER, 1)


def test_insert_twice_is_exists_and_remove_checks_the_cas(store, scratch):  # Review Focus 3, A29
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '{"b": 1, "a": 2}')
    assert saved.expiration == 0 and stored_document(key).text == '{"a":2,"b":1}'
    with pytest.raises(DocumentExists):
        store.insert(ORDERS, key, "{}")
    with pytest.raises(DocumentConflict):
        store.remove(ORDERS, key, int(saved.cas) + 1)
    store.remove(ORDERS, key, int(saved.cas))
    assert stored_document(key) is None and store.get(ORDERS, key) is None


def test_documents_that_are_not_objects(store, scratch):  # A3
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '"just text"')
    assert stored_document(key).text == '"just text"'
    for text in ("[1, 2]", "42.50", '{"back": "to an object"}', "null"):
        saved = store.replace(ORDERS, key, text, int(saved.cas))
        assert stored_document(key).text == store.server_form(text)


def test_a_limited_user_reads_documents():  # FP §8: the company's users are limited
    connection = open_couchbase(username=COUCHBASE_READER)
    try:
        assert connection.documents().get(USERS, "u1").text.startswith('{"name":"Ann"')
    finally:
        connection.close()


def test_writes_are_never_resent_and_reads_are_read_only(store, scratch, monkeypatch):  # D-04-9, ruling C7
    sent = []
    begin = CouchbaseConnection._begin

    def recording(self, sql, **kwargs):
        sent.append((sql.split()[0], kwargs["readonly"], kwargs["retry_safe"], kwargs.get("preserve_expiry", False)))
        return begin(self, sql, **kwargs)

    monkeypatch.setattr(CouchbaseConnection, "_begin", recording)
    key = scratch_key()
    scratch.append(key)
    saved = store.insert(ORDERS, key, '{"a":1}')
    store.replace(ORDERS, key, '{"a":2}', int(saved.cas))
    assert ("INSERT", False, False, False) in sent and ("UPDATE", False, False, True) in sent
    assert {(readonly, retry) for verb, readonly, retry, _ in sent if verb == "SELECT"} == {(True, True)}


def test_a_failed_read_after_a_write_is_an_unknown_outcome():  # ruling C25: written, but no META to report
    class Statements:
        def statement_rows(self, statement, named=None, *, write=False, preserve_expiry=False):
            if write:
                return [parse_row('{"cas":1792325990605258752}')]
            raise StatementError("Connection to the query node was lost: the test dropped it", code="connection_lost")

    with pytest.raises(WriteOutcomeUnknown):
        SqlppDocumentStore(Statements()).replace(ORDERS, NEVER, "{}", 1)
    with pytest.raises(WriteOutcomeUnknown):
        SqlppDocumentStore(Statements()).insert(ORDERS, NEVER, "{}")
```

(The last test is pure — a fake `Statements` — and passes against the pre-flight's prototype of this task with C25, fails without it.)

And in `backend/tests/test_api.py::test_engines`: `assert couchbase["documents"] and couchbase["executed_plan"] and couchbase["plan_format"] == "couchbase"` (the loop over the other engines is unchanged: none has `documents`).

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_couchbase_documents.py tests/test_api.py::test_engines`
Expected: FAIL (`AttributeError: 'CouchbaseConnection' object has no attribute 'documents'`; `test_engines` on `documents`).

- [ ] **Step 3: Domain models and errors** per Interfaces (`domain/couchbase.py`, `domain/errors.py`).

- [ ] **Step 4: The contract and the connection.** `adapters/documents.py`; move `is_server_error` to `messages.py` (`adapter.py` and `services/metadata_errors.py` import it from there); `CouchbaseResult.fetch_rows`; in `adapter.py` the write options of `_begin` / `_internal_result`, `_read_all`, `statement_rows`, `documents()`, `documents=True` (and drop "documents: 04c." from the capability comment).

- [ ] **Step 5: `adapters/couchbase/documents.py`** per Rules. The failure mapping is the part the tests above pin; its shape:

```python
class _Concurrent(Exception):
    """The server answered 12009 to a write: a concurrent change, or (INSERT) a duplicate key (A29)."""

    def __init__(self, error: StatementError) -> None:
        super().__init__(error.message)
        self.message = error.message


def _write(self, statement: str, named: Mapping[str, str], *, preserve_expiry: bool = False) -> list[RawRow]:
    try:
        return self._statements.statement_rows(statement, named, write=True, preserve_expiry=preserve_expiry)
    except StatementError as exc:
        if exc.code == _CONCURRENT:
            raise _Concurrent(exc) from exc
        if is_server_error(exc):
            raise QueryFailed(exc.message) from exc  # the server's answer is final (B39)
        raise WriteOutcomeUnknown() from exc  # it may have been written: never sent again (D-04-9)


def _refusal(self, ks: str, key: str) -> DomainError:
    """Why a CAS-checked UPDATE or DELETE changed nothing (spec §3.9.2), from the document's state now."""
    state = self._state(ks, key)
    if state is None:
        return DocumentMissing()
    if state.type != "json":
        return BinaryDocument()
    return DocumentConflict(state.cas)
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_couchbase_documents.py tests/test_api.py tests/test_couchbase_adapter.py tests/test_couchbase_api.py tests/test_couchbase_sampling.py`
Expected: PASS.

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run gen:api && git status --short src/lib/api`
Expected: green; `gen:api` shows no change (`EngineInfo.documents` already existed; nothing new is exposed yet).

```bash
git add backend/dbaguy_backend/adapters/documents.py backend/dbaguy_backend/adapters/couchbase/documents.py \
  backend/dbaguy_backend/adapters/couchbase/messages.py backend/dbaguy_backend/adapters/couchbase/adapter.py \
  backend/dbaguy_backend/adapters/couchbase/result.py backend/dbaguy_backend/services/metadata_errors.py \
  backend/dbaguy_backend/domain/couchbase.py backend/dbaguy_backend/domain/errors.py \
  backend/tests/test_couchbase_documents.py backend/tests/test_api.py
git commit -m "feat(couchbase): documents through SQL++ — exact reads, CAS-checked writes that keep the expiry, never a binary document"
```

---

### Task 5: The save flow: `DocumentService`, `MetadataService.with_connection(retry=False)`, `/couchbase/documents/*`

**Files:**
- Create: `backend/dbaguy_backend/services/document_service.py`
- Modify: `backend/dbaguy_backend/services/metadata_service.py`, `backend/dbaguy_backend/api/couchbase.py`, `backend/dbaguy_backend/container.py`
- Modify: `backend/dbaguy_backend/domain/couchbase.py` (`SavePreview`, `SaveAnswer`), `backend/dbaguy_backend/domain/errors.py` (`InvalidJson`, `NumberOutOfRange`, `ReadOnlyProfile`)
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`; `src/lib/api/types.ts`
- Create: `backend/tests/test_couchbase_documents_api.py`
- Modify: `backend/tests/test_metadata_service.py` (`retry=False` pinned, ruling C26)

**Interfaces:**
- Consumes: Task 4 (`DocumentCapable`, `DocumentStore`, the domain models and errors, `CAS_MAX` checked by the store); Task 3 (`json_problem`, `diff`, `DocumentDiff`); `sql/offsets.utf16_offset`; `services/metadata_errors.server_errors_are_final`.
- Produces:
  - `MetadataService.with_connection(self, profile_id: UUID, database: str | None, fn: Callable[[DatabaseConnection], T], *, retry: bool = True) -> T` — `retry=False`: one attempt; a failure that is not a `DomainError` still drops the connection and is raised as `QueryFailed`, never sent a second time (D-04-9).
  - `domain/couchbase.py`:
    ```python
    class SavePreview(BaseModel):
        """What the server would store instead of the edited text: nothing is written until the user confirms."""

        status: Literal["needs_confirmation"]
        server_text: str  # the server's form of the text (SELECT RAW $doc, A2)
        changes: list[DocumentChange]  # value changes first, at most CHANGES_LISTED (ruling C2)
        counts: ChangeCounts  # every change, by kind

    SaveAnswer = Annotated[DocumentSaved | SavePreview, Field(discriminator="status")]
    ```
  - `domain/errors.py`: `InvalidJson(ValidationFailed)` code `invalid_json`, `__init__(self, offset: int, reason: str)` → message `f"Not valid JSON: {reason}"`, `detail={"offset": offset, "reason": reason}` (UTF-16 offset); `NumberOutOfRange(ValidationFailed)` code `number_out_of_range`, `__init__(self, paths: list[str])` → message "Couchbase would change these numbers (integers beyond 64 bits or numbers beyond a double's range): write them as strings to save", `detail={"paths": paths}`; `ReadOnlyProfile(DomainError)` code `read_only`, status 403 ("This connection is read-only").
  - `services/document_service.py`: `DocumentService(metadata: MetadataService, profiles: ProfileService, registry: AdapterRegistry)` with
    `get(profile_id: UUID, keyspace: Keyspace, doc_id: str) -> DocumentView`,
    `save(profile_id: UUID, keyspace: Keyspace, doc_id: str, cas: str, text: str, confirmed: bool) -> DocumentSaved | SavePreview`,
    `insert(profile_id: UUID, keyspace: Keyspace, doc_id: str, text: str, confirmed: bool) -> DocumentSaved | SavePreview`,
    `remove(profile_id: UUID, keyspace: Keyspace, doc_id: str, cas: str) -> None`.
  - `AppContainer.documents: DocumentService`.
  - API (`api/couchbase.py`, all `POST`, bodies with `extra="forbid"` like `_Input`):
    `DocumentRequest{profile_id: UUID, keyspace: Keyspace, id: str (min_length=1, max_length=DOCUMENT_ID_MAX_BYTES)}`;
    `SaveDocumentRequest(DocumentRequest){cas: str (pattern=CAS_PATTERN), text: str (max_length=DOCUMENT_TEXT_LIMIT), confirmed: bool = False}`;
    `InsertDocumentRequest(DocumentRequest){text, confirmed}`; `RemoveDocumentRequest(DocumentRequest){cas}`;
    `/couchbase/documents/get` → `DocumentView`; `/save`, `/insert` → `SaveAnswer` (`response_model=SaveAnswer`); `/remove` → 204.
  - `src/lib/api/types.ts`: `DocumentView`, `DocumentSaved`, `SavePreview`, `DocumentChange`, `ChangeCounts`, and `export type SaveAnswer = DocumentSaved | SavePreview;` (each from `S[…]`, with a one-line comment as the file's other exports have).

Rules (spec §3.9.3, rulings C7, C11; pre-flight C26, C27):
- Every method first requires the profile's engine to have `documents` (else `ValidationFailed("This connection has no documents")`); `save`, `insert`, `remove` also refuse a read-only profile (`ReadOnlyProfile`). The ID must be 1–`DOCUMENT_ID_MAX_BYTES` UTF-8 bytes and valid text (a lone surrogate: `ValidationFailed("The document ID holds a character that is not valid text")`); `int(cas)` is passed on (the store checks its range).
- A text to write is checked before any connection is used: valid text (`text.encode("utf-8")`, else `ValidationFailed("The document holds a character that is not valid text")`), then `json_problem(text)` → `InvalidJson(utf16_offset(text, problem.offset), problem.reason)`.
- Inside one `with_connection(profile_id, None, run, retry=False)` call, under `server_errors_are_final()`: `store = connection.documents()` (a connection that is not `DocumentCapable` → `ValidationFailed`); `blocked = store.blocked_numbers(text)` → `NumberOutOfRange(blocked)` (checked also when `confirmed`); unless `confirmed`: `server = store.server_form(text)`, `found = diff(text, server)`. A `RecursionError` from `blocked_numbers` **or** `diff` → `InvalidJson(0, "Nested too deeply to check before saving")` (ruling C27: `json_problem`'s C decoder accepts nesting far deeper than Python's recursion limit — 1,100 levels pass, measured — while `number_paths` and `diff` recurse one Python frame per level; unmapped, the error leaves the metadata call as a non-`DomainError`, which drops the profile's metadata connection and answers `query_failed` "maximum recursion depth exceeded"); `found.changes` not empty → `SavePreview(status="needs_confirmation", server_text=server, changes=found.changes, counts=found.counts)` and nothing is written; else `store.replace(keyspace, doc_id, text, int(cas))` / `store.insert(keyspace, doc_id, text)`. `get` uses `with_connection` with its default retry, also under `server_errors_are_final()`; no row → `DocumentMissing()`.
- Logging: INFO `Saved a document in {bucket}.{scope}.{collection}` / `Inserted…` / `Deleted…`, and `A save in {…} needs confirmation: {n} change(s) ({kind} {count}, …)` with the non-zero counts only; DEBUG `Document {id} in {…}: {get|save|insert|remove}`. Never a text, a path or a value.

- [ ] **Step 1: Write the failing tests** — `backend/tests/test_couchbase_documents_api.py`:

```python
"""The documents API over the dev cluster (spec §3.9.3–§3.9.4): the save flow with its preview and confirmation, the
refusals, no resend of a write whose answer was lost, nothing of a document in the log. Writes only zz_04c_ keys in
dbaguy.app.orders (removed by key)."""

import pytest
from loguru import logger

from dbaguy_backend.adapters.base import StatementError
from dbaguy_backend.adapters.couchbase.adapter import CouchbaseConnection
from tests.support import (
    COUCHBASE_PASSWORD, put_raw_document, requires_couchbase, scratch_documents, scratch_key, stored_document,
)
from tests.test_api import AUTH
from tests.test_couchbase_api import COUCHBASE, app, cb  # noqa: F401  (fixtures)
from tests.test_dev_env import U_NUMBERS

pytestmark = requires_couchbase
ORDERS = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}
USERS = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
NEVER = "zz_04c_never_there"


def post(client, path, **body):
    return client.post(f"/couchbase/documents/{path}", json=body, headers=AUTH)


def error_of(response):
    return response.status_code, response.json()["error"]["code"]


@pytest.fixture
def scratch():
    with scratch_documents() as keys:
        yield keys


def new_document(scratch, text, **options):
    key = scratch_key()
    scratch.append(key)
    put_raw_document(key, text, **options)
    return key


def test_get_answers_the_document_and_404_when_missing(app, cb):
    client, _ = app
    answer = post(client, "get", profile_id=cb, keyspace=USERS, id="u_numbers").json()
    assert (answer["text"], answer["type"], answer["expiration"]) == (U_NUMBERS, "json", 0)
    assert error_of(post(client, "get", profile_id=cb, keyspace=ORDERS, id=NEVER)) == (404, "document_missing")


def test_a_save_without_token_changes_is_written_at_once(app, cb, scratch):  # Review Focus 1, spec §12
    client, _ = app
    key = new_document(scratch, '{"a":1,"b":[1,2]}', expiry=2_000_000_000)
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas,
                 text='{\n  "a": 2,\n  "b": [1, 2]\n}').json()
    assert saved["status"] == "saved" and saved["cas"] != cas and saved["expiration"] == 2_000_000_000
    assert stored_document(key).text == '{"a":2,"b":[1,2]}'


def test_a_save_that_changes_tokens_asks_first_then_saves_when_confirmed(app, cb, scratch):  # Review Focus 1
    client, _ = app
    key = new_document(scratch, '{"zeta": 1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    text = '{"zeta": 1, "alpha": {"z": 1, "a": 2}, "dec": 1.10}'
    preview = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text).json()
    assert preview["status"] == "needs_confirmation"
    assert preview["server_text"] == '{"alpha":{"a":2,"z":1},"dec":1.1,"zeta":1}'
    assert [(c["path"], c["kind"], c["value_changed"]) for c in preview["changes"]] == [
        ("$.dec", "number", False), ("$", "key_order", False), ("$.alpha", "key_order", False)]
    assert preview["counts"] == {"key_order": 2, "number": 1, "duplicate_key": 0, "other": 0}
    assert stored_document(key).text == '{"zeta": 1}'  # nothing written before the confirmation
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text, confirmed=True).json()
    assert saved["status"] == "saved" and stored_document(key).text == preview["server_text"]


def test_a_stale_cas_is_a_conflict_carrying_the_current_cas(app, cb, scratch):  # Review Focus 3
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    first = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    saved = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=first, text='{"a":2}').json()
    stale = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=first, text='{"a":3}')
    assert error_of(stale) == (409, "document_conflict") and stale.json()["error"]["detail"] == {"cas": saved["cas"]}


def test_numbers_the_server_would_change_are_refused_with_their_paths(app, cb, scratch, monkeypatch):  # ruling C4
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    sent = []
    begin = CouchbaseConnection._begin
    monkeypatch.setattr(CouchbaseConnection, "_begin",
                        lambda self, sql, **kwargs: (sent.append(sql), begin(self, sql, **kwargs))[1])
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas,
                  text='{"ok":1,"u":18446744073709551615,"h":1e400}')
    assert error_of(answer) == (422, "number_out_of_range")
    assert answer.json()["error"]["detail"] == {"paths": ["$.u", "$.h"]}
    assert not [sql for sql in sent if sql.startswith(("UPDATE", "SELECT RAW"))]  # refused before anything is sent


def test_invalid_json_points_at_the_spot_in_utf16(app, cb):
    client, _ = app
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=NEVER, cas="1", text='{"😀": 1,}')
    assert error_of(answer) == (422, "invalid_json")
    assert answer.json()["error"]["detail"] == {"offset": 8, "reason": "Illegal trailing comma before end of object"}


def test_read_only_profiles_cannot_write(app):  # M11
    client, _ = app
    body = {"profile": {**COUCHBASE, "read_only": True}, "secrets": {"password": COUCHBASE_PASSWORD}}
    read_only = client.post("/profiles", json=body, headers=AUTH).json()["profile"]["id"]
    assert post(client, "get", profile_id=read_only, keyspace=USERS, id="u1").status_code == 200
    for path, extra in (("save", {"cas": "1", "text": "{}"}), ("insert", {"text": "{}"}), ("remove", {"cas": "1"})):
        assert error_of(post(client, path, profile_id=read_only, keyspace=ORDERS, id=NEVER, **extra)) == (403, "read_only")


def test_a_binary_document_is_refused(app, cb, scratch):
    client, _ = app
    key = new_document(scratch, "not json")
    view = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()
    assert (view["type"], view["text"], view["size"]) == ("base64", None, 8)
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=view["cas"], text='{"x":1}')
    assert error_of(answer) == (422, "binary_document") and stored_document(key).type == "base64"


def test_insert_previews_then_creates_and_remove_deletes(app, cb, scratch):
    client, _ = app
    key = scratch_key()
    scratch.append(key)
    text = '{"b": 1, "a": 2}'
    preview = post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text=text).json()
    assert preview["status"] == "needs_confirmation" and preview["counts"]["key_order"] == 1
    assert stored_document(key) is None
    saved = post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text=text, confirmed=True).json()
    assert saved["status"] == "saved" and saved["expiration"] == 0
    assert error_of(post(client, "insert", profile_id=cb, keyspace=ORDERS, id=key, text="{}")) == (409, "document_exists")
    assert post(client, "remove", profile_id=cb, keyspace=ORDERS, id=key, cas=saved["cas"]).status_code == 204
    assert error_of(post(client, "get", profile_id=cb, keyspace=ORDERS, id=key)) == (404, "document_missing")


def test_a_write_whose_answer_is_lost_is_not_sent_again(app, cb, scratch, monkeypatch):  # Review Focus 4, D-04-9
    client, _ = app
    key = new_document(scratch, '{"a":1}')
    cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
    sent = []
    begin = CouchbaseConnection._begin

    def losing(self, sql, **kwargs):
        stream = begin(self, sql, **kwargs)
        if not sql.startswith("UPDATE"):
            return stream
        sent.append(sql)
        while stream.next() is not None:  # the server has answered: the write is done
            pass
        self._end()
        raise StatementError("Connection to the query node was lost: the test dropped it", code="connection_lost")

    monkeypatch.setattr(CouchbaseConnection, "_begin", losing)
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text='{"a":2}')
    assert error_of(answer) == (502, "write_outcome_unknown")
    assert len(sent) == 1 and stored_document(key).text == '{"a":2}'
    monkeypatch.undo()
    again = post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text='{"a":2}')
    assert error_of(again) == (409, "document_conflict")  # a second write never lands on top of the first


def test_document_text_and_ids_never_reach_the_info_log(app, cb, scratch):  # spec §5
    client, _ = app
    key = new_document(scratch, '{"a":"zz-marker-before"}')
    records = []
    sink = logger.add(lambda message: records.append((message.record["level"].no, message.record["message"])),
                      level=0)
    try:
        cas = post(client, "get", profile_id=cb, keyspace=ORDERS, id=key).json()["cas"]
        text = '{"b":"zz-marker-after","a":"zz-marker-before"}'
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text)
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas=cas, text=text, confirmed=True)
        post(client, "save", profile_id=cb, keyspace=ORDERS, id=key, cas="1", text='{"u":18446744073709551615}')
    finally:
        logger.remove(sink)
    assert not [text for _, text in records if "zz-marker" in text]
    assert not [text for level, text in records if level >= 20 and (key in text or "$.u" in text)]
    assert [text for level, text in records if level < 20 and key in text]  # the ID only at DEBUG


def test_other_engines_have_no_documents(app, tmp_path):
    client, _ = app
    sqlite = client.post("/profiles", json={"profile": {"engine": "sqlite", "name": "s", "path": str(tmp_path / "x.db")}},
                         headers=AUTH).json()["profile"]["id"]
    assert error_of(post(client, "get", profile_id=sqlite, keyspace=USERS, id="u1")) == (422, "validation_failed")


def test_nesting_too_deep_to_check_is_refused_before_anything_is_sent(app, cb):  # ruling C27
    client, _ = app
    text = '{"a":' * 1100 + "12345678901234567890" + "}" * 1100  # valid JSON; blocked_numbers walks it
    answer = post(client, "save", profile_id=cb, keyspace=ORDERS, id=NEVER, cas="1", text=text)
    assert error_of(answer) == (422, "invalid_json")
```

In `backend/tests/test_metadata_service.py` (ruling C26: no other test fails if `retry=False` is ignored — the store turns every write failure into a `DomainError`, which `MetadataService` never resends — so the flag itself is pinned here; add `from dbaguy_backend.domain.errors import QueryFailed`):

```python
def test_without_retry_a_failure_is_raised_once_and_never_sent_again(service_and_profile):  # D-04-9, ruling C26
    service, pid = service_and_profile
    calls = []

    def failing(connection):
        calls.append(connection)
        raise RuntimeError("the answer was lost")

    with pytest.raises(QueryFailed):
        service.with_connection(pid, "dbaguy", failing, retry=False)
    assert len(calls) == 1
    with pytest.raises(QueryFailed):
        service.with_connection(pid, "dbaguy", failing)
    assert len(calls) == 3  # the default reconnects once and runs it again
```

(The pre-flight ran all of this task's tests against a prototype of Tasks 1–5: they pass, and fail without the code.)

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_couchbase_documents_api.py tests/test_metadata_service.py`
Expected: FAIL (404: the routes do not exist; `with_connection` takes no `retry`).

- [ ] **Step 3: `with_connection(…, retry=False)`** in `services/metadata_service.py`: `_call_on(profile, database, fn, retry)` loops over `(1, 2) if retry else (1,)`; the rest unchanged (a `DomainError` passes through untouched; anything else drops the worker and, on the last attempt, becomes `QueryFailed`).

- [ ] **Step 4: Models, errors, `DocumentService`, the four routes and `AppContainer.documents`** per Interfaces and Rules.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && .venv/bin/pytest -q tests/test_couchbase_documents_api.py tests/test_couchbase_documents.py tests/test_couchbase_api.py tests/test_metadata_service.py`
Expected: PASS.

- [ ] **Step 6: Regenerate the API types and add the exports**

Run: `npm run gen:api && npm run typecheck && npm run build`
Expected: `schema.d.ts` gains the four paths and the six schemas (`DocumentSaved | SavePreview` as a union); typecheck and build pass.

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && .venv/bin/pytest -q`
Expected: green.

```bash
git add backend/dbaguy_backend/services/document_service.py backend/dbaguy_backend/services/metadata_service.py \
  backend/dbaguy_backend/api/couchbase.py backend/dbaguy_backend/container.py backend/dbaguy_backend/domain/couchbase.py \
  backend/dbaguy_backend/domain/errors.py backend/tests/test_couchbase_documents_api.py \
  backend/tests/test_metadata_service.py src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts
git commit -m "feat(couchbase): /couchbase/documents — guarded saves that preview server changes, never resend a write"
```

---
### Task 6: Frontend: open and view documents — the document tab, the open dialog, the tree and grid entries, workspace restore

**Files:**
- Modify (backend): `backend/dbaguy_backend/domain/workspace.py`, `backend/tests/test_local_stores.py`
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`
- Modify: `package.json`, `package-lock.json`
- Modify: `src/lib/format.ts`, `src/features/editor/extensions.ts`, `src/features/editor/CodeEditor.tsx`
- Modify: `src/stores/workspace.ts`, `src/features/workspace/workspaceFile.ts`, `src/features/workspace/pendingWorkspace.ts`, `src/features/workspace/Workspace.tsx`
- Create: `src/features/documents/documentApi.ts`, `src/features/documents/openDocument.ts`, `src/features/documents/OpenDocumentDialog.tsx`, `src/features/documents/useDocument.ts`, `src/features/documents/DocumentBar.tsx`, `src/features/documents/DocumentTab.tsx`
- Modify: `src/features/explorer/Explorer.tsx`, `src/features/explorer/kinds.tsx`, `src/features/query/QueryTab.tsx`, `src/features/query/ResultsArea.tsx`, `src/features/query/ResultGrid.tsx`, `src/app/AppShell.tsx`

**Interfaces:**
- Consumes: Task 5 (`/couchbase/documents/get`, `DocumentView`, the error codes; `EngineInfo.documents`); `keyspaceOfPath`, `keyspaceLabel` (`features/explorer/fieldSamples.ts`); `childrenQuery` (`features/explorer/useTreeRows.ts`); `parseJson`, `formatJson` (`features/grid/json.ts`); `RecoverableError`; `registerEditor`, `rememberEditorBuffer`, `readEditorBuffer`, `markEdited` (`stores/editorBuffers.ts`); `useReportTabStatus`; `writeClipboard`; `formatBytes`; `ConfirmDialog`, `Dialog`, `FieldRow`, `Input`.
- Produces (Task 7 builds on all of it):
  - Backend: `WorkspaceTab.kind: Literal["query", "table", "object", "document"]`; `WorkspaceTab.document_cas: str | None = Field(default=None, pattern=CAS_PATTERN)` (the CAS a document tab's unsaved text is based on; null for a new document). `PathSegment.kind` is a plain string, so the `document` segment needs no new `NodeKind` (spec §3.2.3's `{kind:"document", name:id}` as it is).
  - `package.json`: `"@codemirror/lang-json": "6.0.2"`.
  - `lib/format.ts`: `formatTimeUntil(at: number, now: number): string` — `"in 13 yr."`, `"in 5 days"` with the same units and `Intl.RelativeTimeFormat("en", { numeric: "auto", style: "short" })` as `formatRelativeTime`; `"expired"` when `at <= now`.
  - `editor/extensions.ts`: `EditorOptions.language: "sql" | "json"`; `editorOptions(editor, dialect, readOnly = false, language: "sql" | "json" = "sql")`; the language slot's key is `${language}:${dialect}:${upperCaseKeywords}` and builds `[json(), EditorView.contentAttributes.of({ "aria-label": "JSON editor" })]` or the SQL mode with `"aria-label": "SQL editor"` (the label leaves `STATIC`).
  - `editor/CodeEditor.tsx`: prop `language?: "sql" | "json"` (default `"sql"`); `CodeEditorHandle.replaceAll(text: string): void` — the whole text replaced as one undoable change of its own (`userEvent: "format"`, `isolateHistory.of("full")`, cursor at 0, scrolled into view).
  - `stores/workspace.ts`:
    ```ts
    /** A Couchbase document (spec §3.13): its stored text in a JSON editor, with its META. */
    export interface DocumentTabState {
      id: string;
      kind: "document";
      profileId: string;
      /** bucket › scope › collection › document: the keyspace (`keyspaceOfPath`) and, last, the ID. */
      path: PathSegment[];
      /** The document's ID. */
      title: string;
      /** Not in the database yet: the first ⌘S inserts it. */
      isNew: boolean;
      /** The CAS the editor's text was loaded with or last saved as; null for a new document or until loaded. */
      cas: string | null;
      /** The editor holds changes not saved yet: `workspace.json` then keeps the text and `cas`. */
      dirty: boolean;
      /** What the editor starts with (restored unsaved text, or a new document's); null loads it from the server. */
      initialText: string | null;
    }
    export type Tab = QueryTabState | TableTabState | ObjectTabState | DocumentTabState;
    // WorkspaceState:
    openDocument: (profileId: string, keyspace: Keyspace, id: string, options?: { isNew?: boolean }) => string;
    ```
    `openDocument` activates an open document tab of the same profile and path (and returns its id); otherwise it adds `{ id: "document:" + crypto.randomUUID(), kind: "document", profileId, path: documentPath(keyspace, id), title: id, isNew, cas: null, dirty: false, initialText: isNew ? "{}" : null }` and activates it.
  - `documents/documentApi.ts`: `interface DocumentRef { profileId: string; keyspace: Keyspace; id: string }`; `documentPath(keyspace: Keyspace, id: string): PathSegment[]` (`bucket`, `scope`, `collection`, `document` segments); `documentRef(tab: DocumentTabState): DocumentRef`; `getDocument(ref: DocumentRef, signal?: AbortSignal): Promise<DocumentView>`. (Task 7 adds `saveDocument`, `insertDocument`, `removeDocument`, `isPreview`.)
  - `documents/openDocument.ts` (a zustand store, like `unlockPrompts.ts`):
    ```ts
    export interface OpenDocumentRequest {
      profileId: string;
      /** "new": the dialog asks for the ID of a document to create (Task 7). */
      mode: "open" | "new";
      keyspace: Partial<Keyspace>;
      id?: string;
    }
    export function askOpenDocument(request: OpenDocumentRequest): void;
    export function useOpenDocumentRequest(): OpenDocumentRequest | null;
    export function closeOpenDocument(): void;
    /** The collection last opened for this profile's bucket and scope (this run of the app only). */
    export function rememberCollection(profileId: string, keyspace: Keyspace): void;
    export function lastCollection(profileId: string, bucket: string, scope: string): string | undefined;
    /** The ID a grid cell suggests: the row's `id` column when it is a string, else the clicked cell's string, else "". */
    export function idPrefill(columns: readonly ColumnMeta[], row: readonly unknown[], col: number): string;
    ```
  - `documents/OpenDocumentDialog.tsx`: `OpenDocumentDialogHost()` (mounted once in `AppShell`, next to `SettingsDialogHost`).
  - `documents/useDocument.ts`:
    ```ts
    export interface DocumentMeta { cas: string; expiration: number; type: "json" | "base64"; size: number }
    export type DocumentLoad =
      | { status: "new" } // not in the database yet: nothing to load
      | { status: "loading" }
      | { status: "ready"; meta: DocumentMeta }
      | { status: "missing" }
      | { status: "error"; error: Error };
    export interface DocumentState {
      load: DocumentLoad;
      /** Reads the document again and puts its text in the editor (ask first when there are unsaved changes). */
      reload: () => void;
      /**
       * After a failed load (RecoverableError's Retry, the unlock dialog): the first load again, which keeps the
       * editor's unsaved text (ruling C29). Never discards anything.
       */
      retry: () => void;
      /** After a save or an insert (Task 7): the new META, and `baseline` (the editor's document) as the clean state. */
      saved: (meta: DocumentMeta, baseline: Text) => void;
      /** Restored unsaved text whose CAS is not the server's any more: a save will report a conflict. */
      changedOnServer: boolean;
      /** The editor's text changed (the tab calls it from CodeEditor's onChange). */
      edited: () => void;
    }
    export function useDocument(tab: DocumentTabState, shown: boolean, editor: RefObject<CodeEditorHandle | null>): DocumentState;
    ```
  - `documents/DocumentBar.tsx`: `DocumentBar({ id, keyspace, load, isNew, children }: { id: string; keyspace: Keyspace; load: DocumentLoad; isNew: boolean; children: ReactNode })`.
  - `documents/DocumentTab.tsx`: `DocumentTab({ tab, active }: { tab: DocumentTabState; active: boolean })`.
  - `ResultGrid` prop `documentsProfileId: string | null` (`ResultsArea` passes it through; `QueryTab` sets it to `tab.profileId` when `engine.documents`, else null).

Behaviour (spec §3.13, rulings C15–C17; pre-flight C28–C30, C33, C34):
- `useDocument`: loads with `getDocument` the first time the tab is `shown`, unless `isNew`; aborts on unmount. `load` is `{ status: "new" }` whenever `tab.isNew` (it follows the tab: Task 7 turns a missing tab into a new one, and a new one into an existing one, with `updateTab` and `reload()`). A clean tab — **`!tab.dirty && !tab.isNew`**, not `initialText === null` (ruling C29: a loaded tab that was edited and is mounted again by its error boundary's Retry has `initialText` null and unsaved text) — puts the text in the editor with `setText(view.text ?? "")` (outside the undo history), keeps `view.state.doc` as the baseline, and sets `updateTab(id, "document", { cas: view.cas, dirty: false })`. A tab with unsaved text (restored, or edited) keeps its editor text and stays dirty until it is saved or reloaded (no baseline); `changedOnServer = tab.cas !== view.cas`. 404 `document_missing` → `missing`; any other failure → `error`. `retry()` is the first load again (same rules: unsaved text is kept); `reload()` re-reads and behaves as for a clean tab (it discards unsaved text: only the Reload button after its confirmation, and Task 7's conflict Reload and "Open the existing document", call it). The error state's Retry and the unlock dialog call `retry()`, never `reload()` (ruling C29: otherwise a restored tab with unsaved edits whose first load needs the password — a backend restart, check 12 — gets its edits replaced by the server's text the moment the password is given). `edited()`: `markEdited()`; when the tab is not dirty, `updateTab(dirty: true)` at once; `DIRTY_CHECK_MS` (200) after the last edit, when there is a baseline and `view.state.doc.eq(baseline)`, `updateTab(dirty: false)` (CodeMirror's `Text.eq` skips shared structure: cheap even for 20 MiB).
- `DocumentTab`: mounted for every document tab, as `QueryTab` is (`QueryTab` is not "rendered once shown": it mounts with the tab and only its session waits for the first show) — here only the first load waits for the first show (ruling C28). Its `CodeEditor` stays mounted in every load state; `loading`, `error`, `missing` and binary show their panel in its place by hiding the editor (`hidden`), never by unmounting it (ruling C28: `useDocument` puts the loaded text in through the handle, which needs a mounted view — an editor unmounted while `loading` has none when the answer comes — and a remount would drop the undo history; Task 7's close guard and the tab's dirty dot must also exist for a restored tab with unsaved text that was never shown, or closing it from the strip, or deleting its profile, discards the text without asking). It registers its editor (`registerEditor(tab.id, () => editor.current)`, `rememberEditorBuffer` from `onUnmount`) so the workspace writer reads its text; `useReportTabStatus(tab.id, { busy: load.status === "loading", dirty: tab.dirty })`. Layout: `DocumentBar` (action buttons as its children), a notice line, then the editor (`CodeEditor language="json" dialect={null} initialText={readEditorBuffer(tab.id)?.text ?? tab.initialText ?? ""}`, as `QueryTab` takes its remembered text, read-only unless the load is `ready` or `new` and the profile is not `read_only`), with these shown in its place: `loading` → "Loading…"; `error` → `RecoverableError` (its retry is `retry()`, C29; a missing password asks in the profile's one unlock dialog and loads once given); `missing` → "No document `<id>` in <keyspace label>" with Reload — but a tab with unsaved text keeps its editor shown under the notice "The document no longer exists: Create it saves these edits as a new document" (ruling C30: otherwise a restored tab whose document was deleted meanwhile hides the user's edits, and Task 7's "Create it" would replace them with `{}`); `ready` with `type === "base64"` → "Binary document (N bytes) — view only" (no editor shown). Notices: a read-only profile → "Read-only connection: documents open for reading"; `changedOnServer` → "The document changed on the server after these edits were made: saving will report a conflict". Actions: Format (⇧⌘F; `parseJson` of the text → `replaceAll(formatJson(root))`, or the toast "Not valid JSON: nothing to format"), Reload (a dirty tab asks first: `ConfirmDialog` "Discard your changes and read the document again?", confirm label "Reload"), Copy (`writeClipboard(text)`, toast "Copied the document"). New React key handlers check `isImeKeyEvent(e)` first.
- `DocumentBar`: the ID (monospace, selectable, truncated with a tooltip), `keyspaceLabel(keyspace)`; for `ready`: `CAS <cas>` (selectable), `Expires <toLocaleString of expiration × 1000> (<formatTimeUntil>)` or `No expiry`, `json` / `binary`, `formatBytes(size)`; for a new document: "New document — not saved yet". Colours from tokens only.
- `OpenDocumentDialogHost`: title "Open document" (Task 7 adds "New document"); fields Bucket, Scope, Collection, Document ID (`FieldRow` + `Input`), prefilled from the request (`keyspace.collection` falling back to `lastCollection(profileId, bucket, scope)`); the Collection input has a `<datalist>` of the scope's collections from `useQuery(childrenQuery(profileId, [bucket segment, scope segment]))` (only `collection` nodes; enabled when bucket and scope are set); the ID field has the focus. Open is enabled when all four are non-empty and the ID is at most 250 UTF-8 bytes (`new TextEncoder().encode(id).length`; else the hint "At most 250 bytes"). Enter submits (IME-guarded). Submit: `rememberCollection`, `openDocument(profileId, keyspace, id, { isNew: mode === "new" })`, close.
- Workspace persistence (`workspaceFile.ts`): a document tab saves `{ id, kind: "document", title, profile_id, object_path: path, document_cas: cas, buffer }`, where `buffer` is the editor's text (`readBuffer(tab.id)?.text ?? tab.initialText ?? ""`) only when `isNew || dirty`, else null — a clean document never enters `workspace.json` (ruling C15). `document_cas` is written only when it matches the backend's `CAS_PATTERN` (`/^[1-9][0-9]{0,18}$/`), else null, as `savedParams` filters drafts (ruling C33, B40: `PUT /workspace` validates the whole state, so one CAS the pattern refuses would fail every save and a crash would lose every tab's unsaved text). Restore: a `document` entry of an existing profile whose `object_path` names a keyspace (`keyspaceOfPath`) and ends with a `document` segment becomes `{ kind: "document", profileId, path, title, isNew: document_cas == null && buffer != null, cas: document_cas ?? null, dirty: buffer != null, initialText: buffer ?? null }`; otherwise it is listed in `closed`, as table and object tabs are. `pendingWorkspace.ts`'s `TAB_KINDS` gains `"document"`.
- `Workspace.tsx`: `TabContent` renders `DocumentTab` for `"document"`; `TabLabel` shows the unsaved dot for document tabs too; a document tab's tooltip is `<id> · <profile name>`, then the keyspace label (and "New document — not saved yet").
- `kinds.tsx`: `document` → lucide `FileBraces` icon, label "Document" (quick open lists the tab with it).
- Tree: a collection's menu gains "Open document by ID…" after Browse documents, only when the profile's engine has `documents` (`askOpenDocument({ profileId, mode: "open", keyspace: keyspaceOfPath(node.path) ?? {} })`: `keyspaceOfPath` returns `Keyspace | null`, which `Partial<Keyspace>` does not take, ruling C34).
- Grid: with `documentsProfileId` set, a cell's menu (`DataGrid.extraCellMenu`) gains "Open document…" → `askOpenDocument({ profileId, mode: "open", keyspace: { bucket, scope } of the execution's `request.queryContext` (empty without one), id: idPrefill(set.columns, set.rows[row], col) })`; the collection comes from `lastCollection`.

- [ ] **Step 1: Backend workspace fields.** Test first, in `backend/tests/test_local_stores.py`:

```python
def test_workspace_keeps_document_tabs(tmp_path):  # Review Focus 5, ruling C15
    path = [{"kind": "bucket", "name": "dbaguy"}, {"kind": "scope", "name": "app"},
            {"kind": "collection", "name": "users"}, {"kind": "document", "name": "u 1/😀"}]
    state = WorkspaceState.model_validate({"tabs": [
        {"id": "d1", "kind": "document", "title": "u 1/😀", "object_path": path, "buffer": '{"a": 1}',
         "document_cas": "1792325990605258752"},
        {"id": "d2", "kind": "document", "title": "u2", "object_path": path[:3] + [{"kind": "document", "name": "u2"}]},
    ]})
    service = WorkspaceService(tmp_path / "workspace.json")
    service.put(state)
    assert service.get() == state
    with pytest.raises(ValidationError):
        WorkspaceTab(id="d3", kind="document", document_cas="12ab")
```

Run: `cd backend && .venv/bin/pytest -q tests/test_local_stores.py -k document` → FAIL; add the two fields; run again → PASS; then `npm run gen:api`.

- [ ] **Step 2: The JSON editor.** `npm install --save-exact @codemirror/lang-json@6.0.2`; `language` in `EditorOptions` / `editorOptions` / the language slot; `CodeEditor`'s `language` prop and `replaceAll`. Note the JS bundle size from `npm run build` before and after (for Task 8).

- [ ] **Step 3: State and persistence.** `DocumentTabState`, `Tab`, `openDocument` in `stores/workspace.ts`; `workspaceFile.ts`, `pendingWorkspace.ts`; `formatTimeUntil`.

- [ ] **Step 4: The tab.** `documentApi.ts` (Task 6 part), `useDocument.ts`, `DocumentBar.tsx`, `DocumentTab.tsx`; `Workspace.tsx` (`TabContent`, `TabLabel`, tooltip); `kinds.tsx`.

- [ ] **Step 5: Opening.** `openDocument.ts`, `OpenDocumentDialog.tsx` and its host in `AppShell.tsx`; the tree's "Open document by ID…"; the grid's "Open document…" (`ResultGrid` → `DataGrid.extraCellMenu`, `ResultsArea`, `QueryTab`).

- [ ] **Step 6: Check the types and the build**

Run: `npm run typecheck && npm run build && cd backend && .venv/bin/pytest -q`
Expected: no errors; green.

- [ ] **Step 7: Manual check list** (the coordinator runs it with `scripts/dev.sh --web`, `http://localhost:1420`, a Couchbase profile `127.0.0.1`, database `dbaguy.app`, user `dbaguy`, `save_password` off; nothing in this list writes to the cluster):
  1. Tree › dbaguy › app › users, right-click → Open document by ID…: the dialog shows dbaguy / app / users, the Collection field offers the scope's collections; type `u_numbers`, Enter → a tab `u_numbers` with the document icon; CAS (digits), No expiry, json, 241 B (ruling C34: `u_numbers` is 241 bytes); the text exactly as stored (`9007199254740993`, `18446744073709551615`, `1.10`, `1E+3`, `1e-400`, `1e400`).
  2. `u_keyorder`: the line break and the spaces as stored. `u_ttl`: Expires on 1 Jan 2100 (local time), "in 73 yr." (whole 365-day years, as `formatRelativeTime` counts them, from October 2026; C34). `s1`, `a1`, `n1` open (a string, an array, a number).
  3. blobs › `bin1`: "Binary document (9 bytes) — view only", no editor.
  4. An ID that does not exist: the tab says so, with Reload.
  5. `u1`: Format → pretty-printed, numbers and key order unchanged, the unsaved dot appears; ⌘Z → the stored text again and the dot goes (within a moment). Copy → the clipboard holds the text.
  6. Open `u_numbers` again from the dialog: the existing tab is activated, no second one.
  7. Run `SELECT META(d).id, * FROM users AS d` (context dbaguy.app); right-click a cell of row `u2` → Open document… → dbaguy / app / users / `u2` (collection from step 1); Open → its tab. In `SELECT d.name FROM users AS d`, the clicked name prefills the ID.
  8. Type in `u1` (dot), reload the page: the tab returns with the typed text and the dot; Reload → confirm → the stored text, no dot.
  9. `dev/data/workspace.json`: the clean `u_numbers` tab has `"buffer": null` and its `document_cas`; after a page reload it shows the server's text again.
  10. `u_big` (5 MiB, one long line): it opens; scroll to the end, type a character, undo. Note how long the tab took to show and whether typing lags (Task 8 records it).
  11. A read-only copy of the profile: documents open read-only with the notice; a PostgreSQL profile's collection-less tree and its grids show no document items.
  12. With a document tab open **and edited** (the dot), make the dev backend reload (`touch backend/dbaguy_backend/app.py`: uvicorn `--reload` keeps the port and token, the unsaved password is gone) and reload the page: the tab comes back with its edits and the dot; shown, it asks for the password (one dialog for the profile) and, once it is given, shows the META while keeping the edits (ruling C29: Retry is the first load, never a Reload).
  13. Quick open (⌘P) lists the document tab with its icon.

- [ ] **Step 8: Commit**

```bash
git add backend/dbaguy_backend/domain/workspace.py backend/tests/test_local_stores.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts package.json package-lock.json \
  src/lib/format.ts src/features/editor/extensions.ts src/features/editor/CodeEditor.tsx src/stores/workspace.ts \
  src/features/workspace/workspaceFile.ts src/features/workspace/pendingWorkspace.ts src/features/workspace/Workspace.tsx \
  src/features/documents/documentApi.ts src/features/documents/openDocument.ts \
  src/features/documents/OpenDocumentDialog.tsx src/features/documents/useDocument.ts \
  src/features/documents/DocumentBar.tsx src/features/documents/DocumentTab.tsx src/features/explorer/Explorer.tsx \
  src/features/explorer/kinds.tsx src/features/query/QueryTab.tsx src/features/query/ResultsArea.tsx \
  src/features/query/ResultGrid.tsx src/app/AppShell.tsx
git commit -m "feat(app): Couchbase document tabs — open by ID from the tree, a grid or a dialog; stored text and META; restored with unsaved edits"
```

---

### Task 7: Frontend: save, create and delete documents — preview, conflicts, close guard

**Files:**
- Create: `src/features/documents/useDocumentSave.ts`, `src/features/documents/SavePreviewDialog.tsx`, `src/features/documents/ConflictDialog.tsx`
- Modify: `src/features/documents/documentApi.ts`, `src/features/documents/DocumentTab.tsx`, `src/features/documents/OpenDocumentDialog.tsx`, `src/features/explorer/Explorer.tsx`, `src/features/workspace/closeGuards.ts`

**Interfaces:**
- Consumes: Task 6 (everything it produces); Task 5 (`/couchbase/documents/save`, `/insert`, `/remove`, `SavePreview`, `DocumentSaved`, `DocumentChange`, the error codes and their `detail`).
- Produces:
  - `documentApi.ts`: `saveDocument(ref: DocumentRef, cas: string, text: string, confirmed: boolean): Promise<SaveAnswer>`; `insertDocument(ref: DocumentRef, text: string, confirmed: boolean): Promise<SaveAnswer>`; `removeDocument(ref: DocumentRef, cas: string): Promise<void>`; `isPreview(answer: SaveAnswer): answer is SavePreview` (`answer.status === "needs_confirmation"`).
  - `useDocumentSave.ts`:
    ```ts
    export type SaveProblem =
      | { kind: "invalid_json"; offset: number; reason: string }
      | { kind: "numbers"; paths: string[] }
      | { kind: "exists" }
      | { kind: "outcome_unknown"; message: string }
      | { kind: "error"; message: string };
    export interface DocumentSaving {
      busy: boolean;
      preview: SavePreview | null;
      conflict: "changed" | "deleted" | null;
      problem: SaveProblem | null;
      save: () => void; // ⌘S and the Save button
      confirm: () => void; // the preview's "Save as shown"
      cancelPreview: () => void;
      dismissConflict: () => void;
      /** A deleted document's edits saved as a new document with the same ID (the conflict dialog). */
      saveAsNew: () => void;
      /** Delete…, after its confirmation: true when the document is gone (the tab then closes). */
      remove: () => Promise<boolean>;
      /**
       * A save, insert or delete answered `password_required` (ruling C31): the tab shows `RecoverableError` with
       * `error`, and `resend` (its onRetry, also run once the unlock dialog has the password) sends the same request.
       */
      passwordNeeded: { error: Error; resend: () => void } | null;
    }
    export function useDocumentSave(tab: DocumentTabState, editor: RefObject<CodeEditorHandle | null>,
                                    loaded: DocumentState): DocumentSaving;
    ```
  - `SavePreviewDialog({ preview, documentId, pending, onConfirm, onCancel }: { preview: SavePreview; documentId: string; pending: boolean; onConfirm: () => void; onCancel: () => void })`.
  - `ConflictDialog({ kind, documentId, onReload, onCopy, onSaveAsNew, onCancel }: { kind: "changed" | "deleted"; documentId: string; onReload: () => void; onCopy: () => void; onSaveAsNew: () => void; onCancel: () => void })`.
  - `closeGuards.ts`: `CLOSE_GUARD_ORDER.document = 5` (close-only, like `script`; a tab has one or the other).

Behaviour (spec §3.9.3, §3.13; rulings C12–C14, C19, C20; pre-flight C28, C30–C32, C34):
- `save()`: nothing while `busy`, for a read-only profile, a binary document, or a load that is neither `ready` nor `new`. A tab that is neither new nor dirty sends nothing: toast "No changes to save" (ruling C12); "dirty" is decided at that moment — when the tab has a baseline, `view.state.doc.eq(baseline)` means nothing to save — not by the `dirty` flag, which settles `DIRTY_CHECK_MS` after typing (ruling C32: ⌘S right after an undo must send nothing, Review Focus 1). Otherwise keep `sent = view.state.doc` and its text, set `busy` (the editor is read-only while busy, C14), and call `insertDocument(ref, text, false)` for a new tab or `saveDocument(ref, tab.cas, text, false)`; a `SavePreview` opens the preview dialog; a `DocumentSaved` applies directly.
- `confirm()`: the same call with `confirmed: true` and the text that was previewed; on `DocumentSaved` the editor shows the preview's `server_text`, pretty-printed with `formatJson(parseJson(server_text).root)` when the saved text held a line break (else, and when `parseJson` gives null — nesting deeper than its 500 levels, ruling C34 — as the server wrote it), set with `setText` (outside the undo history); that document is the new baseline (ruling C13).
- Applying a `DocumentSaved`: `loaded.saved({ cas, expiration, type: "json", size }, baseline)` (baseline: `sent` after a direct save, the replaced text after a confirmed one), `updateTab(id, "document", { cas, isNew: false, dirty: !view.state.doc.eq(baseline) })`.
- Errors (by `ApiError.code`): `document_conflict` → conflict `"changed"`; `document_missing` → `"deleted"`; `document_exists` → problem `exists` (a line "A document with this ID already exists" with "Open the existing document", which asks `ConfirmDialog` "Discard this draft and open the existing document?" and then turns the tab into an existing one: `updateTab(isNew: false, initialText: null)` + `loaded.reload()`); `invalid_json` → problem with `detail.offset` / `detail.reason`, the cursor moved there (`selectRange(offset, offset)`, focus) and the line "Not valid JSON at line L, column C: <reason>" (L, C from `view.state.doc.lineAt(offset)`); `number_out_of_range` → the line "Couchbase would change these numbers: <paths> — write them as strings to save"; `write_outcome_unknown` → its message and a Reload button; `password_required` → `passwordNeeded` (ruling C31: the backend restarted since the tab loaded, so a password that was not saved is gone; nothing was sent — the metadata connection could not open — so sending the same request again once the unlock dialog has the password is safe; without it the tab has no way to unlock, and Reload would discard the edits); `binary_document`, `read_only` and anything else → the server's message. A problem line goes away at the next save attempt or edit.
- Saves, inserts and deletes are direct calls (or a `useMutation`, which does not retry), never a `useQuery`: the app's `QueryClient` defaults to `retry: 1`, which would send a write a second time (ruling C32, Review Focus 4).
- `SavePreviewDialog`: title "Save document — the server will change it"; a summary from `counts` ("2 keys reordered · 1 number written differently · …", only non-zero kinds); the listed changes, value changes first, each `path` (monospace) · label (`key_order` "Keys sorted", `number` "Number written differently" or, when `value_changed`, "Number changes value", `duplicate_key` "Duplicate key: the last value is kept", `other` "Changed") · `before → after`; a value change is marked in the danger token; when the counts exceed the listed changes, "… and N more"; the server's text (read-only `CodeEditor language="json"`, pretty-printed) when it is at most `SERVER_TEXT_SHOWN_MAX` characters, else a Copy button for it. Buttons Cancel and "Save as shown" (primary, disabled while `pending`). The list scrolls inside a fixed-height area with `[scrollbar-gutter:stable]`.
- `ConflictDialog`: `"changed"` — "`<id>` was changed after you opened it." with "Reload (discard my changes)", "Copy my version", Cancel; `"deleted"` — "`<id>` was deleted after you opened it." with "Save as new document", "Copy my version", Cancel. Copy uses `writeClipboard` and a toast; the dialog stays open after Copy.
- `remove()` runs after `ConfirmDialog` "Delete document `<id>`? This cannot be undone." (confirm label "Delete"); `removeDocument(ref, tab.cas)`; success → the tab closes without asking (`closeTab`) and the toast "Deleted <id>"; a conflict opens the conflict dialog.
- `DocumentTab` adds: Save (⌘S: CodeMirror keymap `Mod-s` at `Prec.highest`, and a window `keydown` while the tab is active and the focus is elsewhere in it — IME-guarded, nothing while a dialog is open) and Delete…, both hidden for a read-only profile and a binary document (Delete also for a new tab); `busy` in the tab status; the missing state's "Create it" (turns the tab into a new document: `updateTab(isNew: true, cas: null)`; a tab without unsaved text also gets the text `{}` — `updateTab(initialText: "{}")`, `setText("{}")` — while a tab with unsaved text keeps its editor's text, ruling C30); `passwordNeeded` as `RecoverableError` under the bar (C31); the problem line under the bar; the dialogs.
- Close guard (`useCloseGuard(tab.id, CLOSE_GUARD_ORDER.document, …)`, in `DocumentTab`, which is mounted for every document tab, shown or not — ruling C28): a dirty tab asks `ConfirmDialog` "Discard unsaved changes to `<id>`?" (confirm label "Discard"); Cancel keeps it open (ruling C20).
- Tree: a collection's menu gains "New document…" after "Open document by ID…" when the engine has `documents` and the profile is not `read_only` (`askOpenDocument({ profileId, mode: "new", keyspace })`); the dialog in mode `"new"` is titled "New document" and its button is "Create".

- [ ] **Step 1: The API calls and `useDocumentSave`** per Interfaces and Behaviour.

- [ ] **Step 2: `SavePreviewDialog.tsx`, `ConflictDialog.tsx`.**

- [ ] **Step 3: Wire them into `DocumentTab`, the open dialog's "new" mode, the tree's "New document…" and the close guard.**

- [ ] **Step 4: Check the types and the build**

Run: `npm run typecheck && npm run build`
Expected: no errors.

- [ ] **Step 5: Manual check list** (as Task 6; every document written here is a `zz_check_` key in `dbaguy.app.orders`; the last item removes them):
  1. orders › right-click → New document… → ID `zz_check_1` → a tab with `{}` and "New document — not saved yet". Type `{"b": 1, "a": 1.10}`, ⌘S → the preview: "Number written differently" `$.a` `1.10 → 1.1`, "Keys sorted" `$`; "Save as shown" → the tab shows `{"a":1.1,"b":1}`, a CAS, No expiry, no dot.
  2. ⌘S again: "No changes to save", and the network panel shows no `/save` request.
  3. Format, ⌘S → saved at once (whitespace only), the text stays formatted, the CAS changes.
  4. Add `"pi": 3.141592653589793238462643383279`, ⌘S → the preview lists `$.pi` first, marked as changing a value; Cancel → nothing saved.
  5. Add `"u": 18446744073709551615`, ⌘S → the line names `$.u` and asks for a string; nothing saved. Remove it.
  6. Delete a closing brace, ⌘S → "Not valid JSON at line …" and the cursor is at the spot. Undo.
  7. In a query tab (context dbaguy.app): `UPDATE orders USE KEYS "zz_check_1" SET touched = true`. Back in the document, type, ⌘S → "was changed after you opened it": Copy my version (clipboard), then Reload → the server's text with `touched`.
  8. Query tab: `UPSERT INTO orders (KEY, VALUE, OPTIONS) VALUES ("zz_check_ttl", {"a": 1}, {"expiration": 2000000000})`; open `zz_check_ttl`: Expires 18 May 2033; change `1` to `2`, ⌘S → saved, still Expires 18 May 2033.
  9. New document… `zz_check_1` again, ⌘S → "already exists" with "Open the existing document" → confirm → the stored document loads in that tab.
  10. With `zz_check_ttl` open and edited, run `DELETE FROM orders USE KEYS "zz_check_ttl"` in a query tab; ⌘S → "was deleted": Save as new document → saved (no expiry now).
  11. Edit `zz_check_ttl`, ⌘W → "Discard unsaved changes?": Cancel keeps the tab; ⌘W, Discard closes it. Edit it again, open another tab, reload the page (the edited tab is restored but not shown): its close button in the strip asks the same (C28); Cancel.
  12. `bin1` and a read-only copy of the profile: no Save, no Delete, no New document…; ⌘S does nothing.
  13. Typing with the 注音 IME in the document editor: the Enter that ends a composition never saves.
  13a. With `zz_check_ttl` edited and loaded, make the dev backend reload under the open page (`touch backend/dbaguy_backend/app.py`: `scripts/dev.sh` runs uvicorn with `--reload`, same port and token; the unsaved password is gone): ⌘S → the profile's unlock dialog; give the password → the save goes through (or previews) with the edits intact (C31).
  14. Delete… on `zz_check_1` → confirm → the tab closes with "Deleted zz_check_1"; delete `zz_check_ttl` the same way. Open document by ID `zz_check_1` → it does not exist.
  15. Leftovers (ruling C24, before any backend suite runs again): in a query tab, `SELECT RAW META(d).id FROM dbaguy.app.orders AS d WHERE META(d).id LIKE "zz%"` → `[]` and `SELECT RAW COUNT(*) FROM dbaguy.app.orders` → `[500]`; remove any leftover by exact key (`cd backend && .venv/bin/python -c 'from tests.support import delete_documents; delete_documents("<key>")'`).

- [ ] **Step 6: Commit**

```bash
git add src/features/documents/useDocumentSave.ts src/features/documents/SavePreviewDialog.tsx \
  src/features/documents/ConflictDialog.tsx src/features/documents/documentApi.ts src/features/documents/DocumentTab.tsx \
  src/features/documents/OpenDocumentDialog.tsx src/features/explorer/Explorer.tsx src/features/workspace/closeGuards.ts
git commit -m "feat(app): save, create and delete Couchbase documents — server changes previewed, conflicts explained, edits guarded"
```

---
### Task 8: Packaging check, measurements, CLAUDE.md, the spec, and the user's test list

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-18-04-couchbase-design.md`
- Modify (only if Step 2 finds a missing module): `backend/dbaguy-backend.spec`
- Scratchpad only, not committed: `<scratchpad>/t8_probe.py`

**Interfaces:**
- Consumes: everything above; 04a/04b Task 13's procedure (the frozen sidecar in `src-tauri/resources/backend`, `scripts/measure-startup.sh`, which prints `footprint`); Task 6 Step 2's bundle sizes and Step 7 item 10's `u_big` note.
- Produces: a bundle whose frozen sidecar reads, saves, creates and deletes documents; 04c's measurements and state in CLAUDE.md and the spec.

- [ ] **Step 1: Build the sidecar.** Run: `npm run build:backend`. Expected: passes; `du -sk src-tauri/resources/backend` within a few KB of 04b's 49,100 KB (stdlib only).

- [ ] **Step 2: Probe the frozen sidecar.** Shell 1 (note the PID; stop it with Ctrl-C or `kill <PID>`):

```bash
cd src-tauri/resources/backend
DBAGUY_TOKEN=t DBAGUY_PORT=0 DBAGUY_LOG_TO_FILE=false DBAGUY_DATA_DIR="$(mktemp -d)" ./dbaguy-backend   # prints {"event":"ready","port":N}
```

Shell 2, from the repository root: save as `<scratchpad>/t8_probe.py` and run `backend/.venv/bin/python <scratchpad>/t8_probe.py N t <PID>` (it writes one `zz_04c_t8_` key in `dbaguy.app.orders` and deletes it):

```python
"""04c packaged check against the dev cluster: exact reads, a lean read of u_big, the save flow (preview, confirm,
conflict, direct save), insert, remove, and a read over TLS (verify-ca)."""
import json, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid
from pathlib import Path

sys.path.insert(0, "backend")
from tests.support import couchbase_ca_file  # noqa: E402
from tests.test_dev_env import U_NUMBERS  # noqa: E402

port, token, pid = sys.argv[1:4]
USERS = {"bucket": "dbaguy", "scope": "app", "collection": "users"}
ORDERS = {"bucket": "dbaguy", "scope": "app", "collection": "orders"}


def call(path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read()
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def rss():
    return int(subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True).stdout) // 1024


def profile(**ssl):
    body = {"engine": "couchbase", "name": "c", "host": "127.0.0.1", "database": "dbaguy.app", "username": "dbaguy",
            "save_password": False, "ssl": {"mode": "disable", **ssl}}
    return call("/profiles", {"profile": body, "secrets": {"password": "dbaguy"}})[1]["profile"]["id"]


plain = profile()
_, numbers = call("/couchbase/documents/get", {"profile_id": plain, "keyspace": USERS, "id": "u_numbers"})
print("u_numbers exact:", numbers["text"] == U_NUMBERS, numbers["cas"], numbers["size"])
before, started = rss(), time.monotonic()
_, big = call("/couchbase/documents/get", {"profile_id": plain, "keyspace": USERS, "id": "u_big"})
print(f"u_big: {big['size']:,} bytes in {time.monotonic() - started:.2f} s; sidecar RSS {before} → {rss()} MB")
key = f"zz_04c_t8_{uuid.uuid4().hex[:8]}"
doc = {"profile_id": plain, "keyspace": ORDERS, "id": key}
try:
    print("insert:", call("/couchbase/documents/insert", {**doc, "text": '{"b": 1, "a": 1.10}'})[1]["status"])
    _, saved = call("/couchbase/documents/insert", {**doc, "text": '{"b": 1, "a": 1.10}', "confirmed": True})
    print("confirmed:", saved["status"], saved["cas"])
    print("stale cas:", call("/couchbase/documents/save", {**doc, "cas": "1", "text": '{"a": 2}'}))
    status, direct = call("/couchbase/documents/save", {**doc, "cas": saved["cas"], "text": '{"a": 2, "b": 1}'})
    print("direct save:", status, direct["status"])
    print("remove:", call("/couchbase/documents/remove", {**doc, "cas": direct["cas"]})[0])
    print("gone:", call("/couchbase/documents/get", doc)[0])
finally:
    subprocess.run(["curl", "-s", "-o", "/dev/null", "-u", "dbaguy:dbaguy", "-X", "DELETE",
                    f"http://127.0.0.1:8091/pools/default/buckets/dbaguy/scopes/app/collections/orders/docs/{key}"])
ca = couchbase_ca_file(Path(tempfile.mkdtemp()))
tls = profile(mode="verify-ca", ca_file=str(ca))
print("TLS u1:", call("/couchbase/documents/get", {"profile_id": tls, "keyspace": USERS, "id": "u1"})[1]["text"][:20])
```

Expected: `u_numbers exact: True`; `u_big: 5,242,… bytes` in well under a second and an RSS rise of about 15–25 MB (3× the text while it is parsed, the answer's JSON on top: the pre-flight measured 3.4–3.6× through the whole API in-process; ruling C34); `insert: needs_confirmation`, `confirmed: saved`; `stale cas: (409, … document_conflict …)`; `direct save: 200 saved`; `remove: 204`; `gone: 404`; `TLS u1: {"name":"Ann",…`. If the frozen sidecar fails to import a module, add it to `hiddenimports` in `backend/dbaguy-backend.spec`, rebuild and repeat.

- [ ] **Step 3: Build the app, check signatures, measure startup.** Run: `npm run build:app && codesign --verify --deep --strict src-tauri/target/release/bundle/macos/DbaGuy.app && echo "signature ok"`, then `scripts/measure-startup.sh` 3 times. Expected: cold start and footprint within noise of 04b (0.98–1.06 s; 163–167 MB, backend 74–77 MB); record them. Do not look at or touch `~/Library/Application Support/DbaGuy` (the script uses a temporary data dir).

- [ ] **Step 4: Update `CLAUDE.md`** (keep its language and style):
  - **Repo 結構:** `adapters/documents.py`（`DocumentStore`／`DocumentCapable`：依 key 讀寫 JSON 文件的介面，與 engine 無關）; `adapters/couchbase/` 加 `documents`（`SqlppDocumentStore`：只用 SQL++ 的文件讀寫，CAS 檢查、保留 expiry、binary 守衛）; `sql/jsontext.py` 的說明加「`value_end`（不建樹找值的結尾：回應解析的巢狀值與文件檢查共用）、存檔前的 `json_problem`／`number_paths`／`diff`」; `services/document_service.py`（文件存檔流程：守衛、server 形式的預覽、確認；經 metadata 連線，寫入不重送）; `api/couchbase.py` 加 `/documents/*`; 前端 `features/documents/`（document tab、開啟對話框、存檔預覽、衝突對話框、`useDocument`／`useDocumentSave`）.
  - **架構重點 → Couchbase:** a bullet 文件（04c）：只用 SQL++（R-04-2）。開檔 `SELECT META(d).cas, META(d).expiration, META(d).type, d … USE KEYS $id`，`d` 是存放原文（key 順序、空白、`1e400` 都照存）；回應解析對 row 裡的物件／陣列只找結尾、不建樹（`jsontext.value_end`，一份文件約佔原文 3 倍，短字串很多的陣列約 7 倍）。存檔：先檢查 JSON（位置換成 UTF-16）、擋下 server 會改值的數字（超出 int64 的整數、超出 double 範圍的數字，列出路徑）、再用 `SELECT RAW $doc` 取得 server 會存的形式並逐層比較（`jsontext.diff`：key 排序、數字寫法、重複 key、其他；空白與字串跳脫寫法不算），有差別就回 `SavePreview` 由使用者確認，改值的項目排在前面；寫入是 `UPDATE … SET d = $doc WHERE META(d).cas = $cas AND META(d).type = "json"`＋`preserve_expiry`（沒有 type 條件時 UPDATE 會把 binary 文件改成 JSON），expiration 在寫入後另讀（RETURNING 給 0）；0 列時讀文件現況分辨衝突／已刪除／binary；INSERT 的 12009 讀現況分辨已存在。寫入經 metadata 連線、`with_connection(retry=False)`，回應遺失時回 `write_outcome_unknown`、絕不重送。沒改過的文件按 ⌘S 不送任何東西；確認過的存檔之後編輯器顯示 server 的形式（原文有換行就排版）。Workspace 只保存新文件或有未存修改的文件（`buffer`＋`document_cas`），乾淨的文件重開時從 server 讀；tab 第一次顯示才讀取.
  - **已知限制 → Couchbase:** replace 「大文件：……精簡的 row decoder（巢狀值保留文字）留給 04c」 with the measured result (Task 2, Step 2 above); add: 存檔會被 server 正規化（key 排序、空白壓縮、`1.10`→`1.1`、`1e2`→`100`、`-0`→`0`、超出 double 精度的位數、`1e-400`→`0`、重複 key 留最後一個），有預覽與確認，但存不出 byte-exact 的文件（需要時是 SDK store，R-04-2 選項 C）；預覽顯示的 server 文字與實際存放只差字串跳脫的寫法（存放時物件／陣列裡字串的 `<`、`>`、`&` 是 `<`、`>`、`&`，值相同，A2）；超出 int64 的整數與超出 double 範圍的數字不能存，要改成字串；binary 文件只能看 META；沒有改名、沒有欄位層級的編輯器；每個物件都要重排 key 的大文件，存檔預覽約每 MiB 0.4 秒；未存修改的大文件每次存 workspace 都寫整份；Sample fields 仍把取樣的每份文件整份 decode（20 MiB 文件約 10 倍暫時記憶體）；SQL++ 存檔後文件的 flags 與 xattr 是否保留沒有實測；`u_big`（5 MiB 單行）在編輯器裡的表現（Task 6 check 10 的結果）.
  - **量測:** the sidecar size, Step 2's `u_big` time and RSS, Step 3's startup and footprint, the JS bundle's growth from lang-json (Task 6 Step 2).
  - **進度 ④:** `完成（SQL++ 方言與 HTTP 引擎；query context、參數、plan 檢視、取樣與補全、暫停預讀；文件；followups：[04a](…)、[04b](…)、[04c](docs/superpowers/notes/2026-10-19-04c-couchbase-documents-followups.md)）` (the coordinator writes the 04c note before the merge).

- [ ] **Step 5: Update the spec** (record decisions next to the text they refine; do not rewrite history):
  - R-04-2's row ends with 「04c：存檔語句另加 `AND META(d).type = "json"`（A30）；expiration 寫入後另讀（A28）；也擋超出 double 範圍的數字（C4）；`SELECT RAW $doc` 與存放只差字串跳脫寫法（A2）。」
  - D-04-8's row: 「04c T1：成立（值層級）；差別只在存放時字串裡的 `<>&` 寫成 `<` 等，diff 以字串的值比較。」
  - §3.9.1: the ABC lives in `adapters/documents.py` (engine-neutral; ruling C8).
  - §3.9.2's table: replace and remove carry `AND META(d).type = "json"`; replace returns only the CAS and the expiration is read after the write; insert's duplicate is 12009 (reason 17012) told apart by reading the key; blocked numbers include numbers beyond a double's range.
  - §3.9.3: step 3's change kinds are `key_order`, `number`, `duplicate_key`, `other`, each with `value_changed`, value changes listed first, `counts` complete (C2); step 4 adds `write_outcome_unknown` (C7); ⌘S on an unedited document sends nothing (C12); after a confirmed save the editor shows the server's form (C13).
  - §3.9.4: save and insert answer `DocumentSaved{status:"saved", cas, expiration, size}` or `SavePreview{status:"needs_confirmation", server_text, changes, counts}`; CAS is a decimal string ≤ int64 (C11).
  - §3.13 (Document tab): binary documents show META and "view only"; a missing document offers "Create it"; a conflict with a deleted document offers "Save as new document" (C22); the workspace keeps only new or edited documents (C15).
  - §4's table gains `write_outcome_unknown`; §10's A2 and A3 rows record 〔實測〕(04c T1) and A27–A32 are added with their results.

- [ ] **Step 6: Report the user's test list** for 04c in the task handoff (what the dev cluster already showed next to each):
  1. Open a real document by ID from the tree and from a grid row; its CAS and expiration match the Web Console. (Dev: `u_numbers` exact, `u_ttl` 2100-01-01.)
  2. A document your applications wrote (keys in insertion order): change one value on a copy you may rewrite, ⌘S → the preview names the key reordering per object and lists value changes first; confirm; reopen shows the stored form. (Dev: `zz_check_` documents.)
  3. A document with a TTL keeps its expiration after a save. (Dev: 2000000000 kept.)
  4. Change the document in the Web Console after opening it in DbaGuy → Save → the conflict dialog; Reload shows the console's change.
  5. A document holding integers beyond int64 (if the company has any): refused with their paths.
  6. A limited user: documents open; a save without write roles shows the server's error with the missing role. (Dev: `dbaguy_reader` reads `u1`.)
  7. A large real document (MBs): opening, scrolling, typing and the save preview's time. (Dev: `u_big`, see CLAUDE.md.)
  8. Binary documents, if any: META only, no Save or Delete. (Dev: `bin1`.)
  9. The packaged app: open, edit, save and delete a scratch document; unsaved edits come back after quitting and reopening the app.

- [ ] **Step 7: Run the full suites again and commit**

Run: `cd backend && .venv/bin/pytest -q && cd .. && npm run typecheck && npm run build`
Expected: green (with `app.orders` back at its 500 documents: Task 7's check 15, this task's probe removed its key; Global Constraints, C24).

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-18-04-couchbase-design.md
git commit -m "docs: 04c in CLAUDE.md and the spec (documents through SQL++, the save preview, the lean decoder); ④ done"
```

(`backend/dbaguy-backend.spec` too, only if Step 2 changed it.)

---

## Carries (after ④)

| Carry | Where |
|---|---|
| Sampling still decodes each sampled document whole (`sampling.parse_sample_row`): a 20 MiB document costs about 10× its text transiently. A collector that summarises each object in the decoder's hooks (paths and kinds, no values) would bring it near the row decoder's 3×. | ruling C10; 04b note's lean-decoder carry |
| Byte-exact saves need a KV store (`DocumentStore` with the SDK or the KV protocol, R-04-2 option C); SQL++ always sorts keys, compacts whitespace and respells numbers. | ruling C12 |
| Flags and user xattrs after a SQL++ save were not probed (an SDK-written document with a custom transcoder or xattrs may change). | company checklist |
| No rename (insert + remove), no field-level editor, no Save from the close prompt. | spec §1, ruling C20 |
| A TS test runner for the pure frontend helpers (`idPrefill`, the save hook's error mapping, `workspaceFile`'s document entries) — still none (04b carry). | B30 |
| The save preview of a 20 MiB document whose every object the server re-sorts takes several seconds (about 0.4 s per MiB), shown as a busy tab. | ruling C3 |

## Planning rulings

### Assumptions recorded during execution

Task 1 re-runs the probes; the Result column is what they showed while planning (2026-10-19, `enterprise-7.6.12` dev cluster, scratch keys `zz_plan04c_*` in `dbaguy.app.blobs`, all removed by exact key; one ≈ 18 MB document needed the KV docs endpoint to delete, A32). The owner task writes a differing result over it and applies the Effect; nothing else in the plan needs editing for the outcomes listed.

| # | Assumption | Owner | Result | Effect on later tasks |
|---|---|---|---|---|
| A2 | `SELECT RAW $doc` gives what `UPDATE … SET d = $doc` stores (spec §10, D-04-8) | Task 1 | Holds at the value level. Identical for key order (sorted), whitespace (compact), duplicate keys (the last kept), numbers (`1.10`→`1.1`, `-0`/`0.0`/`1.0`→`0`/`0`/`1`, `1e2`→`100`, `1E+3`→`1000`, `1.5e300` → 301 digits, `12345678901234567890.0`→`12345678901234567000`, `1e-400`→`0`, π → 16 digits) and for non-object documents. Differs only in string escapes: inside objects and arrays the stored text writes `<`, `>`, `&` as `<`, `>`, `&` (SELECT RAW escapes `<` only in keys); both turn `é`, `\/` and surrogate-pair escapes into characters; both refuse `1e400` and `2e308` with 1040; integers outside int64 come back changed in both (`12345678901234567890` → `12345678901234567000`, `18446744073709551615` → `18446744073709552000`, `9223372036854775808` → `9223372036854775807`, `-9223372036854775809` → `-9223372036854775808`). The limited user can run it. | Holds: rulings C1–C3 as written. Not (a number, key order or duplicate result differs between the two): Task 5 adds `SavePreview.approximate: bool` (True), Task 7's dialog says "The server may store this slightly differently", CLAUDE.md records it. |
| A3 | Non-object documents through `SET d = $doc`; the INSERT and DELETE forms of §3.9.2 | Task 1 | Holds: a string, an array, a number, `null` and `true` replace an object and each other; `INSERT INTO ks AS d (KEY, VALUE) VALUES ($id, $doc) RETURNING META(d).cas AS cas` inserts them; `DELETE … WHERE META(d).cas = $cas … RETURNING RAW META(d).id` returns the id with the right CAS and no row with a wrong or missing one | Holds: Task 4 as written. Not (a non-object document refused): the store refuses a text that is not an object with `ValidationFailed("Couchbase does not store this kind of document through SQL++")` before sending, and `test_documents_that_are_not_objects` asserts that refusal. |
| A27 | `META(d).cas = $cas` compares exactly when the CAS is beyond 2⁵³ | Task 1 | Holds: the CAS ±1 and +255 match no row | Holds: as written. Not: stop and report to the coordinator — SQL++ offers no other CAS check, and R-04-2 rests on it. |
| A28 | `RETURNING META(d).expiration` after a `preserve_expiry` UPDATE gives the kept expiration | Task 1 | Does not hold: it answers 0 while the stored expiration is kept (4102444800, then 2000000000); without `preserve_expiry` the stored expiration becomes 0 | As planned (ruling C5: the expiration is read after the write). If it held, nothing changes. |
| A29 | A duplicate INSERT answers 12009 with `reason.code` 17012 (`dml.statement.duplicatekey`) | Task 1 | Holds (HTTP 200, `errors` in the body) | Holds: ruling C6. Another code: Task 4 treats that code as it treats 12009 (read the key, then `DocumentExists` or the server's error). |
| A30 | `AND META(d).type = "json"` leaves a binary document alone; without it an UPDATE turns it into JSON | Task 1 | Holds, both halves: guarded → no row, still `base64`; unguarded → one row, type `json` | Holds: ruling C5. Not (the guard does not stop it): Task 4's `replace` and `remove` read `_STATE` first and refuse a binary document before sending the write; the guard stays. |
| A31 | The cluster manager's docs endpoint stores a text byte for byte, a non-JSON value as a binary document, and an absolute `expiry` ≤ 2147483647 | Task 1 | Holds (`expiry=4102444800` is refused: "must be a valid integer between 0 and 2147483647"; 2000000000 works; `flags` is ignored). Pre-flight: a `DELETE` of a key that is already gone answers **400** `{"error":"bad_request","reason":"Key doesn't exist"}`; 404 is a missing collection (C23) | Holds: Task 1's helpers. Not: tests needing a binary or expiring scratch document use `UPSERT … OPTIONS {"expiration": n}` for expiry, and the binary cases read the seeded `bin1` only (no write test on a binary document), recorded in CLAUDE.md. |
| A32 | The dev bucket takes multi-MiB test documents | planning | Does not hold: two ≈ 18 MB documents in the 100 MB `dbaguy` bucket made reads and SQL++ deletes answer TMPFAIL (12008) for over a minute; one could only be removed through the KV docs endpoint | Global Constraints: written test documents stay ≤ 64 KiB; big-document checks read `u_big` or synthetic responses. |

### Rulings made while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| C1 | The server's form is `SELECT RAW $doc` (D-04-8), and the diff compares string tokens by value: escape spelling is never a change. | A2 holds at the value level; only `<>&` escape spelling and the decoding of `é`-style escapes differ, and those are the same strings. | The preview's text can show `<` where the stored text has `<` (same value); a reload shows the stored spelling. |
| C2 | Change kinds `key_order`, `number`, `duplicate_key`, `other`; each with `value_changed` (numbers compared as `Decimal`; key order and an equal number respelled are not value changes); `CHANGES_PER_KIND` kept, value changes listed first, `CHANGES_LISTED` at most, `counts` complete. | Spec §3.9.3 has three kinds and no way to tell "π loses digits" from "1.10 becomes 1.1"; an SDK-written document gives one key-order change per object, which must not push a value change out of the list (Review Focus 2). `other` keeps anything unexpected visible instead of silent. | None found: a kind the server might produce that the walk does not know is reported as `other`. |
| C3 | The diff compacts both texts once (`one_line`, reimplemented as `"".join(runs)` — 0× extra for a compact text where the substitution took 11.6×) and walks them level by level, finding value ends with `value_end`, skipping equal subtrees with one comparison, iterating arrays lazily; it never builds a tree. Measured: 2.0× (compact) and 4.4× (pretty) peak for a token-dense megabyte; 8 MiB in 0.9 s when the keys are already sorted and 2.9 s when every object is re-sorted. | Q1: the save path of a 20 MiB document must stay as lean as its read (the carry's "a document editor of up to 20 MiB"). | A 20 MiB document whose every object the server re-sorts takes about 8 s to preview (a busy tab). |
| C4 | `blocked_numbers` refuses integers outside int64 (spec) and numbers beyond a double's range; underflow (`1e-400` → `0`) and precision loss (π) are previewed as value changes, not blocked. A prefilter (`without_strings` + a regex for 19+ digit runs and three-digit positive exponents) keeps the walk off almost every save. | The server stores out-of-int64 integers as doubles or clamps them (`9223372036854775808` → `…807`) and refuses `1e400` with Go's 1040 text; a named path is clearer. Underflow and precision loss are visible in the preview, marked as value changes. | A user can confirm a precision loss that was marked as such. |
| C5 | Replace is `UPDATE … WHERE META(d).cas = $cas AND META(d).type = "json" RETURNING META(d).cas AS cas` with `preserve_expiry`; remove carries the same type guard; the expiration is read after the write. | A30: without the guard an UPDATE turns a binary document into JSON (spec: binary documents are read-only); A28: RETURNING's expiration is 0. | One more read after each save (≈ 1 ms). |
| C6 | No row (or 12009) after a replace or remove reads the document's state: missing → `document_missing`, not JSON → `binary_document`, else `document_conflict` with the current CAS; an INSERT's 12009 with the key present → `document_exists`. | Spec §3.9.2; A29: a duplicate key and a concurrent change share 12009. | None. |
| C7 | A write's server error is `QueryFailed` (final, B39); any other failure (lost connection, protocol error) is `write_outcome_unknown` (502, "may or may not have changed; Reload"), never resent; save, insert and remove also run under `with_connection(retry=False)`. | D-04-9: a resent INSERT reports "exists" for a document it created, and §4 has no code for an unknown outcome. | A save whose answer was lost needs a Reload to see what happened. |
| C8 | `DocumentStore` / `DocumentCapable` live in `adapters/documents.py` (engine-neutral); `SqlppDocumentStore` in `adapters/couchbase/documents.py`; `is_server_error` moves to `adapters/couchbase/messages.py`. | DIP: `DocumentService` depends on the contract, not on the Couchbase package (spec §3.9.1 put the ABC in the Couchbase file); `documents.py` needs `is_server_error`, and importing it from `adapter.py`, which imports `documents.py`, would be circular. | None. |
| C9 | Row values that are objects or arrays are located by the C decoder with hooks that keep nothing (`jsontext.value_end`); their `value` is None. Measured on 4–8 MiB documents: 3.0× (objects, numbers) and 6.8× (an array of short strings) peak instead of 9.3×, 11.1× and 8.7×, 2.0× held instead of 8×; live `u_big` 3.0× instead of 4.0×; 100,000 browse-shaped rows 0.59 s against 0.57 s. A regex-based skip (no validation) was 3.0× everywhere but up to 4× slower on object-heavy rows. | The 04b carry; nothing reads a nested value's decoded form (`value_cell` reads the text, alias expansion and the overflow cell use texts, sampling and `query()` decode rows themselves). The C decoder keeps today's validation and speed. | An array of many short strings still peaks at about 7× while it is parsed. |
| C10 | Sampling's whole-document decode stays as it is (carried). | 04c's binding need is the document editor; sampling reads at most 32 MiB. | Sampling a collection of 20 MiB documents costs about 10× one of them transiently. |
| C11 | `/couchbase/documents/{get, save, insert, remove}` (POST, `{profile_id, keyspace, id}`); save and insert answer a discriminated union `DocumentSaved` / `SavePreview` on `status`; CAS travels as a decimal string matching `CAS_PATTERN` and the store refuses one beyond int64; IDs 1–250 UTF-8 bytes; texts ≤ `DOCUMENT_TEXT_LIMIT` characters. Errors: `document_missing` 404, `document_conflict` 409 (`detail.cas`), `document_exists` 409, `binary_document` 422, `invalid_json` 422 (`detail.offset` in UTF-16, `detail.reason`), `number_out_of_range` 422 (`detail.paths`), `read_only` 403, `write_outcome_unknown` 502. | Spec §3.9.4 and §4; a CAS beyond int64 would be compared as a double by SQL++ (A26's rule), so it could match a wrong CAS. Paths travel in `detail`, never in a message, so they never reach the INFO log. | None today: CAS values stay below 2⁶³ until about 2262 (spec §3.9.2). |
| C12 | ⌘S on a document whose text equals what was loaded or last saved sends nothing; only Save, a new document's first save, and Delete… write; whitespace-only edits save without asking (spec §12); any other token change is previewed. This is the answer to the user's 04a question ("whether byte-exact document saves matter"): SQL++ cannot save byte-exact, so DbaGuy never re-encodes a document the user did not edit and shows every token change before it happens. | R-04-2 and §12 decide the rest; the gap was a Save with nothing edited, which would sort keys and bump the CAS. | A document that must stay byte-exact (for example signed JSON) cannot be edited in DbaGuy; the SDK store behind `DocumentStore` is the way out. |
| C13 | After a save that needed confirmation the editor shows the server's form (pretty-printed when the saved text held a line break) and that is the new baseline; after a direct save the user's text stays. | Otherwise the next save of the same document asks about the same key order again, and the confirmation becomes a reflex (spec §12). | The user's own layout of that document is replaced once. |
| C14 | The editor is read-only while a save, insert or delete is in flight. | The answer replaces the baseline (and after a confirmed save the text). | None (sub-second normally). |
| C15 | A document tab persists `object_path` (bucket, scope, collection, a `document` segment named by the ID), `document_cas`, and `buffer` only while it is new or dirty; restored clean tabs load from the server; dirty tracking uses CodeMirror's `Text.eq` against the loaded document. | Spec §3.2.3 / §3.13 (unsaved text and its CAS survive a crash); a clean 20 MiB document must not be written into `workspace.json` every few seconds (Q1). | A dirty 20 MiB document makes each workspace save 20 MiB, as a 20 MiB query buffer does today. |
| C16 | A document tab loads the first time it is shown; load errors use `RecoverableError` (the profile's one unlock dialog). | Q1 (a restored workspace does not fetch every document at start) and 04b's Task 12b behaviour. | None. |
| C17 | The open dialog has Bucket / Scope / Collection / ID fields (the collection offers the scope's collections from the tree's children query); the grid prefills bucket and scope from the execution's context, the collection last used for them in this run of the app, and the ID from the row's `id` column or the clicked string. | Spec §3.13; a result does not say which collection a row came from. | The first grid open in a scope needs the collection picked. |
| C18 | Tests and manual checks write only small `zz_04c_` / `zz_check_` documents in `dbaguy.app.orders` and remove them by exact key. | 04b: `app.users` must keep 12 documents with `u_big` 9th; A32. | None. |
| C19 | A new document starts as `{}` with its ID chosen in the dialog; its first save previews like any save; `document_exists` offers "Open the existing document", which discards the draft after a confirmation. | Spec §3.13 ("New document…"); a tab's identity is its path, so the ID cannot change afterwards. | None. |
| C20 | Closing a dirty document tab asks "Discard unsaved changes?" (Discard / Cancel): no Save in that prompt. | A save can need a preview or a conflict decision, which a close prompt cannot host. | The user saves with ⌘S before closing. |
| C21 | `DocumentSaved.size` is the UTF-8 length of the saved text without the whitespace between its tokens. | What the server stores, up to number respelling and escape spelling, without reading the document back. | The size shown after a save can be off by a few bytes until the next load. |
| C22 | A conflict with a deleted document offers "Save as new document" (with Copy my version and Cancel) instead of Reload. | Spec §3.13 lists Reload / Copy / Cancel; for a deleted document Reload only shows "does not exist" and drops the edits. | None. |

### Pre-flight rulings (C23–C34)

Made by the pre-flight review before Task 1: prototypes of Tasks 1–5 (the plan's code where it gives code, the rest per Interfaces and Rules) and of Task 6's types, store, workspace file and editor options against the repository at `501508c`, in a scratch worktree; the full backend suite ran there twice (4,270 passed with Tasks 1–5, 4,274 with the rulings' code and tests; baseline 4,201), `npm run typecheck` and `npm run build` passed with the Task 6 types, and Task 1's probe ran against the dev cluster with `zz_preflight04c_*` keys in `app.orders` (all removed; `app.orders` 500, `app.users` 12 with `u_big` 9th afterwards). Verified without a ruling: the lean scan changes no cell, `JsonText`, row text, member text, `row_cost` or order (a differential run of HEAD's parser against the lean one over 2,677 responses at random cuts, malformed and 3,000-deep ones included: identical), its memory (synthetic 4 MiB documents 3.0× / 3.0× / 6.8× peak and 2.0× held, against 10.0× / 11.1× / 8.8×; live `u_big` 3.01× against 4.01×) and throughput (100,000 browse rows within noise); every new test of Tasks 1–5 fails without its code; a save on the metadata connection while a session of the same profile holds a paused result answers in 25 ms and the paused result then pages on in order; nothing of a document reaches a log at any level (Task 5's log test, sink at level 0). Details in `.superpowers/sdd/2026-10-19-04c-couchbase-documents/preflight.md`. Each ruling is written into the tasks' own text: an implementer reads only its task and the Global Constraints.

| # | Ruling (what; why; cost if wrong) | Changed in |
|---|---|---|
| C23 | `delete_documents` tries every key, then asserts once; fine is 200, or 400 whose body holds `Key doesn't exist`. Why: the docs endpoint answers a key that is already gone with 400 (404 is a missing collection), so with "200 or 404" every test that removes its own document errors in teardown (`test_insert_twice_is_exists_and_remove_checks_the_cas`, Task 5's insert/remove test), and Task 1's probe `finally` stops at the `_ins` key it already deleted and leaves the binary `_bin` document in `app.orders` (both reproduced). Cost if wrong: none. | Task 1 Interfaces, Step 5 expected; A31 row |
| C24 | `app.orders` holds exactly its 500 seeded documents whenever a backend suite runs; a scratch document exists only inside its test, probe or check list; never run a suite while another run, probe or manual list has scratch documents there; Task 7's list ends by checking for `zz%` leftovers and the count. Why: 04b's `test_the_api_caches_until_sampled_again_or_the_profile_changes` samples every order and asserts 500 — the `app.users` lesson (u_big 9th) applies to `app.orders` too, and the manual list keeps `zz_check_` documents for minutes. Cost if wrong: a red suite until the leftover is removed. | Global Constraints (dev cluster); Task 7 Step 5 (check 15); Task 8 Step 7 |
| C25 | A `StatementError` from the `_state` read after a successful `replace` / `insert` is raised as `WriteOutcomeUnknown`, with a pure test (fake `Statements`). Why: the write's answer came, so the document was written; raised as it is (lost connection, or a server error such as TMPFAIL right after the write) it reaches the user as a failed save, and the next ⌘S meets a conflict. Cost if wrong: in that rare case a saved document reads "may or may not have changed"; Reload shows it. | Task 4 Rules, Step 1 |
| C26 | Task 5 pins `with_connection(retry=False)` with a unit test in `tests/test_metadata_service.py` (one attempt, `QueryFailed`; the default runs twice). Why: no planned test fails if the flag is ignored — the store turns every write failure into a `DomainError`, which `MetadataService` never resends — yet it is what keeps any other failure inside the save's metadata call (`worker.call`'s timeout, an unexpected exception after the write went out) from re-running the whole save, write included. Cost if wrong: none. | Task 5 Files, Step 1, Step 2, commit |
| C27 | A `RecursionError` from `blocked_numbers` is mapped like the diff's: `InvalidJson(0, "Nested too deeply to check before saving")`, with an API test. Why: `json_problem`'s C decoder accepts far deeper nesting than Python's recursion limit (1,100 levels pass), while `number_paths` recurses per level; unmapped, a deep document with a 19-digit number or a 3-digit exponent leaves the metadata call as a non-`DomainError`, drops the profile's metadata connection and answers `query_failed` "maximum recursion depth exceeded" (reproduced). Cost if wrong: none. | Task 5 Rules, Step 1 |
| C28 | `DocumentTab` is mounted for every document tab (as `QueryTab` is: it mounts with the tab; only its session waits for the first show) and only the first load waits for the first show; its `CodeEditor` stays mounted in every load state, the state panels hiding it (`initialText` = the remembered buffer ?? `initialText` ?? ""). Why: `useDocument` puts the loaded text in through the handle, which has no view if `loading` unmounted the editor (and a remount drops the undo history); and the close guard and dirty dot must exist for a restored tab with unsaved text that was never shown, or closing it from the strip or deleting its profile discards the text without asking (Q2). Cost if wrong: none (a hidden editor per document tab, as query tabs have). | Task 6 Interfaces (Consumes), Behaviour (`DocumentTab`); Task 7 Behaviour (close guard), check 11 |
| C29 | `useDocument` gains `retry()` — the first load again, which keeps unsaved text — used by the error state's Retry and the unlock dialog; `reload()` (discard and re-read) only for the Reload button after its confirmation, the conflict dialog's Reload and "Open the existing document". "Clean" is `!tab.dirty && !tab.isNew`, not `initialText === null`. Why: as planned, a restored tab with unsaved edits whose first load needs the password (a backend restart: check 12) had them replaced by the server's text the moment the password was given; and an edited tab remounted by its error boundary has `initialText` null. Cost if wrong: none. | Task 6 Interfaces (`DocumentState.retry`), Behaviour (`useDocument`, `DocumentTab`), check 12 |
| C30 | A missing document's tab that holds unsaved text keeps its editor shown under "The document no longer exists: Create it saves these edits as a new document"; "Create it" keeps that text (only a tab without unsaved text starts from `{}`). Why: as planned, a restored tab whose document was deleted meanwhile hid the edits and "Create it" overwrote them with `{}` (Q2). Cost if wrong: none. | Task 6 Behaviour (`DocumentTab`); Task 7 Behaviour (`DocumentTab`) |
| C31 | A save, insert or delete answered `password_required` asks in the profile's unlock dialog (`RecoverableError` with that error; `DocumentSaving.passwordNeeded`) and sends the same request again once the password is given. Why: after a backend restart an unsaved password is gone; nothing was sent (the metadata connection could not open), so resending is safe; as planned the tab only showed the message, with no way to unlock from it, and Reload would discard the edits. Cost if wrong: none. | Task 7 Interfaces, Behaviour (errors, `DocumentTab`), check 13a |
| C32 | ⌘S decides "nothing to save" at that moment (`view.state.doc.eq(baseline)` when there is a baseline), not by the `dirty` flag that settles 200 ms after typing; saves, inserts and deletes are direct calls or `useMutation`, never `useQuery`. Why: Review Focus 1 (⌘S right after an undo must send nothing) and Review Focus 4 (the app's `QueryClient` defaults to `retry: 1`, which would send a write twice). Cost if wrong: none. | Task 7 Behaviour (`save()`, errors) |
| C33 | `workspaceFile.ts` writes `document_cas` only when it matches `/^[1-9][0-9]{0,18}$/`, else null. Why: B40 — `PUT /workspace` validates the whole state, so one CAS the backend's `CAS_PATTERN` refuses would fail every save and a crash would lose every tab's unsaved text. Cost if wrong: none before 2262 (CAS values stay below 2⁶³). | Task 6 Behaviour (workspace persistence) |
| C34 | Text corrections: the tree's `keyspaceOfPath(node.path) ?? {}` (it returns `Keyspace \| null`, which `Partial<Keyspace>` does not take — typecheck); Task 6 check 1 `u_numbers` is 241 B (not 302), check 2 `u_ttl` is "in 73 yr." from October 2026 (whole 365-day years); Task 8's `u_big` RSS rise about 15–25 MB (3× while parsed plus the answer's JSON; 3.4–3.6× through the whole API in-process); C13's pretty-print after a confirmed save falls back to the server's text when `parseJson` gives null (deeper than its 500 levels), where `.root` of null would throw after the write. Cost if wrong: none. | Task 6 Behaviour (tree), checks 1, 2; Task 7 Behaviour (`confirm()`); Task 8 Step 2 |

## Spec issues found while planning

- §3.9.2's replace takes the new expiration from `RETURNING META(d).expiration`, which answers 0 even when `preserve_expiry` kept it (A28, C5).
- §3.9.2's UPDATE and DELETE have no type condition, and an UPDATE turns a binary document into JSON (A30); "binary／base64 文件唯讀" needs the guard in the statement, not only in the UI (C5).
- D-04-8 expects `SELECT RAW $doc` to equal the stored text; it differs in string escape spelling (`<>&`), so the diff must compare strings by value (A2, C1).
- §3.9.2's duplicate-key detection: the server answers 12009 (reason 17012), the same code as a concurrent change (A29, C6).
- §3.9.2's `blocked_numbers` covers int64 only; numbers beyond a double's range are refused by the server with Go's text (C4).
- §4 has no error for a write whose answer was lost, the case D-04-9 describes (C7).
- §3.9.3's three change kinds cannot mark a lost value apart from a respelled one, nor show something unexpected (C2).
- §3.9.1 puts the `DocumentStore` ABC in the Couchbase package, which `DocumentService` would then depend on (C8).
- §3.9.4's `/save` output `{cas, expiration}` or `SavePreview` needs a discriminator to be typed (C11).
- §3.13's conflict dialog offers Reload for a deleted document, which drops the edits (C22).
- §3.9.2 says CAS travels as a JSON integer literal and is < 2⁶³; a CAS beyond int64 would be compared as a double, so the API refuses it (C11).

## Self-review

- **Spec coverage (§11 item 3):** T1 probes A2, A3 → Task 1 (plus A27–A31 found while planning). `DocumentStore` / `SqlppDocumentStore` → Task 4; `DocumentService`, `/couchbase/documents/*`, `MetadataService.with_connection(retry=False)` → Task 5. Document tab, open dialog, the grid's "Open document…", workspace restore, `@codemirror/lang-json` → Task 6; the preview and conflict dialogs, New document…, Delete…, the close guard → Task 7; opening a document in the packaged app → Task 8. The 04a carries (`documents=True`, `jsontext` token comparison and save diff, `preserve_expiry` in the body, `retry=False`, lang-json) → Tasks 3–6; the 04b carries (lean row decoder → Task 2; `server_errors_are_final()` → Task 5; the collection menu → Tasks 6, 7; whole-document reads → Task 4's `_GET`; no documents added to `app.users` → Global Constraints, C18). §3.9.3's five steps → Task 5's Rules; §4's document codes → Tasks 4, 5; §5's log rules → Task 5 (`test_document_text_and_ids_never_reach_the_info_log`); §6.3's 04c integration list (get / save / conflict / expiry / int64 / binary / insert duplicate / remove) → Tasks 4, 5; §7's packaged check → Task 8; §13's docs → Task 8.
- **Placeholders:** none; Task 1's probe and Task 8's probe are given whole; each assumption gives both outcomes.
- **Names across tasks:** `value_end`, `NotJson` (Task 2) → Tasks 3, 4; `one_line`, `json_problem`, `JsonProblem`, `without_strings`, `json_path`, `number_paths`, `diff`, `DocumentDiff`, `CHANGES_PER_KIND`, `CHANGES_LISTED`, `CHANGE_TEXT_LIMIT`, `KEYS_SHOWN`, `utf16_offset`, `DocumentChange`, `ChangeCounts`, `ChangeKind` (Task 3) → Tasks 4, 5; `DocumentView`, `DocumentSaved`, `DocumentStore`, `DocumentCapable`, `SqlppDocumentStore`, `Statements.statement_rows`, `CouchbaseResult.fetch_rows`, `is_server_error` (messages), `BLOCKED_PATHS_MAX`, `changed_by_server`, `DocumentMissing`, `DocumentConflict(current_cas)`, `DocumentExists`, `BinaryDocument`, `WriteOutcomeUnknown`, `DOCUMENT_ID_MAX_BYTES`, `DOCUMENT_TEXT_LIMIT`, `CAS_PATTERN`, `CAS_MAX` (Task 4) → Task 5; `SavePreview`, `SaveAnswer`, `InvalidJson(offset, reason)`, `NumberOutOfRange(paths)`, `ReadOnlyProfile`, `DocumentService.{get, save, insert, remove}`, `with_connection(…, retry=)` (Task 5) → Tasks 6, 7; `WorkspaceTab.document_cas`, `DocumentTabState`, `openDocument`, `documentPath`, `documentRef`, `getDocument`, `askOpenDocument`, `OpenDocumentRequest`, `rememberCollection`, `lastCollection`, `idPrefill`, `useDocument` / `DocumentState` / `DocumentLoad` / `DocumentMeta`, `DocumentBar`, `CodeEditorHandle.replaceAll`, `formatTimeUntil`, `documentsProfileId` (Task 6) → Task 7; `saveDocument`, `insertDocument`, `removeDocument`, `isPreview`, `useDocumentSave` / `DocumentSaving` / `SaveProblem`, `SavePreviewDialog`, `ConflictDialog`, `CLOSE_GUARD_ORDER.document` (Task 7). Test helpers `scratch_key`, `put_raw_document`, `delete_documents`, `stored_document`, `StoredDocument`, `scratch_documents` (Task 1) → Tasks 4, 5. Checked task by task.
- **Review Focus:** each of the five lines names the tests (or manual checks) that pin it and the task that owns them.
- **Proportion:** the code given is the probes, the tests, and the algorithms the tests do not determine (the lean scan, the diff walk, the store's failure mapping); the service, the routes and the frontend are given as signatures, rules and check lists.
- **Pre-checked while planning:** Task 3's tests pass against the plan's own code (30 of 30, 2.6 s); Task 2's stream tests (the whole existing file plus the new ones, 63) pass with an equivalent lean patch and its new tests fail without it.
