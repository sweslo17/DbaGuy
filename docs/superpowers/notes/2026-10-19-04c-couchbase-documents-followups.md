# Plan 04c (Couchbase documents) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-19-04c-couchbase-documents.md` (planning rulings C1–C22, pre-flight rulings C23–C34, assumptions A2, A3 and A27–A32; the rulings made during execution are below). Spec: `docs/superpowers/specs/2026-10-18-04-couchbase-design.md` (Task 8 recorded A2/A3 as measured, added A33/A34 and a §12 list of C35–C48; the coordinator added the final fix wave's decisions next to the text they refine). Executed with subagent-driven development: a pre-flight review that prototyped Tasks 1–5, 8 tasks, every code task reviewed with fix rounds and a scoped re-review, the frontend tasks checked by the coordinator in the browser against the dev cluster, a final Opus review of the whole range ("Ready to merge: With fixes", no Critical, one Important: these close-out docs) and one fix wave with a scoped re-review. Commits 501508c..HEAD on `feat/04-couchbase` (22 commits before this note; full backend suite 4,641 passed, `npm test` 117 passed). With 04c, ④ is done and the branch merges to `main`.

## Outcome against the pain points (feature-priorities §2, §3, §4, §8)

- Q1 light and fast: no new Python dependency (sidecar 49,100 → 49,148 KB); the frontend adds `@codemirror/lang-json` and 04c's code (main chunk +33.2 kB, gzip +11.8 kB). Cold start 1.03–1.11 s (04b 0.98–1.06 s), backend `footprint` 74–78 MB (04b 74–77 MB). The lean decoder (`jsontext.value_end`, C9) brings a 5 MiB document's peak from 4.0× to 3.0× its text and removes 04b's "20 MiB costs 320–540 MB" for the common shapes (worst shapes 17–23×, still never above 04b). A confirmed save no longer pretty-prints a server text over 1 MiB in the frontend (M4).
  - Open: the app's total `footprint` is 10–15 MB above 04b's recorded range (176–184 MB against 163–167 MB); the backend is flat and the extra sits in the WebKit and shell processes, which vary per run. No 04b build was available for an A/B, so it is unverified.
- Q2 never lose work: new and dirty document tabs persist in the workspace with the CAS they were edited from; every document tab has a close guard, including restored tabs never shown (C28); a first read never replaces unsaved text (C29); cancelling the unlock no longer traps the editor (C48); edits stay copyable when the document can no longer be read (M1); a tab whose profile vanished reopens its text in a query tab (M6); a Reload's undo history cannot put old text back into the server's text (7f6ba70).
- Q3 trustworthy: the server's own form (`SELECT RAW $doc`) is previewed before any write that changes a token, value changes listed first and in the danger colour, summarised first (B2); numbers the server would change are refused even when confirmed (C4); every confirmed request carries exactly the previewed text (probed in jsdom over 77 cases); a write is never sent twice and an unknown outcome says so (C42, C45); an unedited document is never re-encoded (⌘S sends nothing, C12, C32); a CAS conflict and a deleted document each have their own dialog. Honest limits: saves are not byte-exact (C12), the DELETE race (C41), and an unknown outcome after 1080.
- Q4 focus: open, save, create and delete only — no rename, no field editor, no Save in the close prompt (spec §1, C20).
- Q5 multi-engine: capability-gated (`documents`) with an engine-neutral contract (`adapters/documents.py`); the other four engines never see the new menu items, `gen:api` changed only the new routes, and the shared `CodeEditor` change (a text replacement starts a fresh undo history) was checked on every editor.
- Q6 macOS conventions: ⌘S saves, ⌘W goes through the close guard, dialogs focus their first empty field and labels focus their inputs, the preview's lists reserve their scrollbar (the user's Mac shows classic scrollbars).
- §4 Must 6 (documents) is done; §8: the 04a question about byte-exact saves is answered by C12 (preview and confirm; byte-exact needs a KV/SDK store).

## Rulings made during execution

- C35 (coordinator, on the pre-flight's riskiest concern): every document-tab state transition lives in one pure reducer, `src/features/documents/documentState.ts`, tested with Vitest (the project's first frontend test runner, pinned at 5.0.3; `npm test`). Hooks only run its effects.
- C36: one agent runs the full backend suite at a time while 04c runs (scratch documents in `app.orders` against the exact-500 sampling test).
- C37/C38 (Task 3): the diff's memory note names the shapes that cost more; documents nested deeper than 100 levels are refused before `diff`/`number_paths` ("Nested deeper than 100 levels").
- C39 (security, Task 4): every raw literal entering a request body (`$doc`, parameters) is exactly one JSON value, checked where literals are serialised; otherwise `invalid_params` and nothing is sent (a crafted literal could add top-level request fields such as `readonly`).
- C40: before a document write, a channel idle for more than 2 s gets one retry-safe `SELECT RAW 1`, so a server-closed keep-alive is replaced before a write that must not be resent. A failed ping means nothing was sent (Task 5 fix round 2).
- C41: the DELETE / concurrent-save race (21 of 80 probes both succeed) is a recorded limitation; the claim-first workaround would silently re-encode the stored text.
- C42: write calls carry an "abandoned" flag checked atomically at start: a timeout while queued never runs ("nothing was sent"); a timeout or failure after start is `WriteOutcomeUnknown`, never resent.
- C43: the pure save checks run outside the metadata call, so an 8-second diff never blocks the profile's tree, completion or sampling.
- C44: the grid's Open document prefill uses the statement's own keyspace when it names exactly one (`POST /couchbase/statement-keyspace`, completion's FROM reader); `system:` and other namespace paths name nothing.
- C45: a write whose request fails without an API answer (backend restarted or died) is `outcome_unknown` and never resent; writes carry no timeout of their own.
- C46: Delete answered `document_missing` closes the tab ("already deleted"). New tab transitions go into the reducer with tests; `useDocument` exposes the baseline as a getter for ⌘S's check.
- C47: a missing document's unsaved text stays read-only in view until Create it or Save as new document.
- C48: cancelling the unlock dialog after a write answered `password_required` ends that attempt (nothing was sent); a later resend sends the same request only while the text is unchanged, otherwise a fresh save (and for a delete, the confirm again — final review P9).
- C49: Task 8 (docs and measurements only) was reviewed by the final review instead of a separate task review.
- C50: the final fix wave's scope (below); M3 deferred.
- C51 (coordinator): the API's document-text cap is 4× the 20 MiB limit — it only picks the message (the body is read before it is checked), and Format on a large compact document adds more than 1 MiB.

### The final fix wave (fbe014b, ce5fd26, e48fe7f)

- P1: a metadata connection is dropped only by the worker that failed on it; a read queued on a connection closed meanwhile reconnects once (writes never retry). P2 pins "no drop" on the queued-then-closed write path; P3 fixes the `with_connection` docstring.
- M5/C51: a document over 20 MiB answers "Documents are limited to 20 MiB".
- M7: `reject_constant`, the lone-surrogate and JSON-whitespace patterns each come from `sql/jsontext.py`.
- P6: the missing-with-edits notice says "Create it, then ⌘S" (a read-only connection points to Copy). P7: a ⌘S that finds no changes clears the "Not saved…" line. P8: the open dialog refocuses only when focus is not already on one of its fields.
- P9 + M2: Delete on a dirty tab says the edits go too, and a delete resend after the text changed asks again.
- M1: Copy works behind the error and binary panels of a dirty tab ("Copy my edits"). M4: the confirmed-save pretty-print is capped at `SERVER_TEXT_SHOWN_MAX`. M6: a dirty or new document tab whose profile vanished reopens as a query tab titled after the ID.

## Carries

| Carry | Where |
|---|---|
| Sampling still decodes each sampled document whole (`sampling.parse_sample_row`): a 20 MiB document costs about 10× its text transiently; a collector in the decoder's hooks would bring it near 3×. | C10 |
| Byte-exact saves need a KV store (`DocumentStore` with the SDK or the KV protocol, R-04-2 option C); SQL++ always sorts keys, compacts whitespace and respells numbers. | C12 |
| A write whose `RETURNING` row arrived before a later server error is reported as failed although it was applied (the next ⌘S meets a conflict). Theoretical for single-key DML; needs `statement_rows` to surface "rows came, then an error". | final review M3 |
| A dirty document tab whose profile still exists but whose saved path is damaged is still closed with its text on restore (M6 covers a vanished profile only). | fix wave concern 4 |
| Two more duplicates: `sessions/options.py::_reject_constant`, and `adapters/couchbase/rows.py::_LONE_SURROGATE` with its escape function. | fix wave concern 2 |
| The React wiring (`useDocumentSave`, `DocumentTab`, the dialogs) has no component tests; the reviewers' jsdom/StrictMode probes stayed in scratch. Adding jsdom + Testing Library would let them become tests. | C35 |
| The app-level `footprint` +10–15 MB over 04b (WebKit/shell) is unverified: A/B against a 04b build. | Task 8 |
| The save preview of a 20 MiB document whose every object the server re-sorts takes several seconds (about 0.26 s per MiB measured), shown as a busy tab. | C3 |
| No rename (insert + remove), no field-level editor, no Save from the close prompt. | spec §1, C20 |

## Packaged-app checks (for the user's final test list)

- The frozen sidecar, both in `src-tauri/resources/backend` and inside the built `DbaGuy.app`, read `u_numbers` byte for byte, read `u_big` (5,242,897 bytes) in 0.06 s, ran insert (preview → confirm), a stale-CAS save (409), a direct save, remove (204), the read-back 404 and a TLS (verify-ca) read, with no hidden import.
- Not driven: the built `.app`'s UI. Item 9 of the test list below.

## Company Couchbase checklist for 04c (04a's and 04b's lists stay open)

- Flags and user xattrs after a SQL++ save (an SDK-written document with a custom transcoder or xattrs may change) — not probed on dev.
- Writes on a multi-node cluster (dev is single-node): CAS conflicts, 1080 timeouts, the C40 ping.
- A restricted user's refused write shows the server's message and the missing role (dev showed the read side only).
- Real documents written by the company's applications (key order, numbers beyond int64 if any, TTLs, multi-MB documents).

## 04c 測試清單（Couchbase 文件編輯）

括號裡是 dev cluster（7.6.12 EE）上已經看到的結果。

1. **開啟真實文件。** 從樹的「Open document by ID…」與從 grid 某一列的「Open document…」各開一份真實文件；CAS 與 expiration 要和 Couchbase Web Console 一致。grid 的對話框會預填這句 SQL 自己的 bucket／scope／collection 與該列的 `id`，游標直接在 ID 欄位（dev：`u_numbers` 的文字與 seed 完全相同、`u_ttl` 顯示 2100-01-01；grid 預填 `dbaguy/app/users/u2`，語句用 `system:` 時退回 tab 的 context）。
2. **你的應用程式寫的文件（key 照寫入順序）。** 在一份可以被改寫的副本上改一個值，⌘S：預覽逐個物件指出 key 被重排，會改值的項目排在最前面、摘要第一句就是「N values change」；確認後編輯器顯示 server 存的形式，重開同一份文件看到的就是存放的樣子（dev：`$.a 1.10 → 1.1` 與 `$` 的 keys sorted）。再按一次 ⌘S 要顯示 "No changes to save"。
3. **有 TTL 的文件存檔後 expiration 不變。**（dev：2000000000，即 2033/5/18 保留；「Save as new document」建立的文件沒有 expiry。）
4. **衝突。** 在 DbaGuy 開著一份文件，到 Web Console 改它，再回 DbaGuy 存檔：出現 "Document changed"；「Copy my version」複製並保留對話框，Reload 顯示 Console 的修改。再試一次：在 Console 刪掉它，回來存檔，出現 "Document deleted"，選「Save as new document」（dev 都 OK；Reload 之後 ⌘Z 不會把舊文字塞回來）。
5. **超出 int64 的整數**（如果公司有）：存檔被拒絕並列出路徑，說明要改成字串（dev：介面上 `18446744073709551615` 列出 `$.u`；自動測試涵蓋 `1e400`）。
6. **受限使用者與唯讀。** 文件可以開；沒有寫入角色的帳號存檔時，顯示 server 的錯誤原文與缺少的角色（dev 只驗了讀）。唯讀的連線設定：沒有 Save、Delete、New document…，⌘S 什麼都不做（dev 已驗證）。
7. **很大的真實文件（數 MB）。** 開啟、捲動、打字、⌘S 看預覽花多久（dev：`u_big` 5 MiB 單行，開啟約 1 秒、打字約 0.2 秒出現未存的圓點、⌘Z 還原後圓點消失；預覽最壞約每 MiB 0.26 秒）。
8. **binary 文件**（如果有）：只顯示 META 與 "view only"，沒有 Save、Delete，⌘S 不送東西（dev：`bin1` 已驗證）。
9. **打包版。** 用 `DbaGuy.app` 開一份、改、存、刪一份暫時的文件；有未存修改的 tab（含新文件）結束 app 再開要回來，存檔仍以原本的 CAS 檢查（dev：打包版的 sidecar 跑過完整的讀、存、衝突、新增、刪除與 TLS 讀取；`.app` 的介面本身沒有開過）。
10. **新增與刪除。** 樹的「New document…」從 `{}` 開始，第一次存照樣預覽；ID 已存在時出現 "already exists" 與「Open the existing document」（確認後捨棄草稿）。Delete… 要確認，有未存修改時訊息會說一起捨棄，成功後 tab 關閉；文件早已被別人刪掉時 tab 直接關閉並提示 "was already deleted"（最後這項沒有在介面上點過）。
11. **後端重啟與密碼。** 讓 backend 重啟後（或重開 app 而連線沒存密碼）按 ⌘S：跳出密碼對話框，給了就存；按 Cancel 則顯示 "Not saved: the connection needs its password…"，編輯器仍可編輯、Save 可按；之後改了文字再存，會照新的文字存（刪除則會再問一次）（dev 瀏覽器已驗證）。
12. **關閉保護。** 有未存修改的 document tab（包含重開 app 後還沒點開過的）關閉時要問 "Discard unsaved changes?"（Discard／Cancel，沒有 Save；先 ⌘S）；沒改過的直接關閉。
13. **注音輸入（只能手動測）。** 在 document 編輯器用注音輸入中文（含組字中按 Enter、⌘S）；在「Open document」與「New document」對話框的欄位用注音：組字中按 Enter 選字不能送出對話框、也不能觸發存檔。
14. **（可選）結果不明。** 存檔或刪除途中失去連線時，畫面要說「可能已寫入」並提供 Reload，而不是自動再送一次或顯示成功（dev 無法穩定重現，只列出預期）。

已知、不要當成 bug 回報的限制：存檔一定會被 server 排序 key、壓縮空白、改寫數字（所以才有預覽；需要 byte-exact 時要換成 KV／SDK 的 store）；巢狀超過 100 層的文件不能存；超過 20 MiB 的文件不能存；沒有改名、沒有欄位層級的編輯器；另一個 client 在同一瞬間存同一份文件、你正好刪除它時，文件可能被刪掉（C41）；存檔後顯示的大小到下次載入前是估計（C21）。

## Incidents

- A32 (planning): the planner wrote two ≈ 18 MB documents into the 100 MB `dbaguy` bucket; reads and SQL++ deletes answered TMPFAIL (12008) for over a minute and one document could only be deleted through the KV docs endpoint. All `zz_plan04c_*` keys were removed and the seeds verified; ruling C18 limits tests and checks to small `zz_` documents in `dbaguy.app.orders`, removed by exact key, and C24 requires `app.orders` to hold exactly 500 documents whenever a suite runs.
- Task 8: one `rm -rf` of the agent's own `mktemp -d` data directory was refused by the safety check and was not retried by anyone; that empty temporary directory stays in the system temp folder.
- Coordinator (browser check of Task 7): text typed through the browser tool landed in the query editor behind an open modal dialog (the tool inserts at the old selection, not the focused element); the query tab's text was restored with undo. The same check found the dialog's focus problem (B4), fixed in 15cfc00.
- After the context was compacted, Task 6's implementer could not be resumed; its second fix round went to a fresh implementer, as the skill allows.

## Deferred minor findings

- `default:<bucket>` (the legacy form for a bucket's `_default._default`) resolves to no keyspace (safe: no prefill, no fields).
- `useReadOnlyProfile` treats a profile as writable until `/profiles` answers; the backend's 403 `read_only` still blocks the write and nothing is sent.
- Three near-identical "is the editor still mounted" helpers in the document hooks.
- A non-envelope 5xx (`http_error`) on a write stays an ordinary error (the backend always uses the envelope).
- The dialog's refocus (timeout plus a frame) was proven in jsdom with Radix's real menu and in the in-app Chromium browser, not in WebKit.
- A ⌘S on a clean tab also clears a pending delete's "Not deleted: … password" line, so its Retry goes (Delete… is still there).
- A text more than 4× the limit (80 MiB) still answers "Invalid request" (only reachable by Format on a very token-dense document near 20 MiB).
- A metadata worker opened after an `invalidate` snapshot survives that invalidate (the same window as before P1).
