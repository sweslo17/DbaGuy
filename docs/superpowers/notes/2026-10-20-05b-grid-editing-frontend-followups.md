# Plan 05b (grid editing, frontend): rulings and follow-ups

- **Plan:** `docs/superpowers/plans/2026-10-20-05b-grid-editing-frontend.md`. It has 12 tasks, planning rulings H1–H26 and spec issues S1–S10.
- **Spec:** `docs/superpowers/specs/2026-10-19-05-grid-editing-design.md`. It carries 〔05a〕 and 〔05b〕 notes next to the text they refine.
- **Backend contract:** `docs/superpowers/notes/2026-10-20-05a-grid-editing-backend-followups.md` (its appendix).
- **Execution:** subagent-driven development.
  - A pre-flight review found 2 High, 7 Medium and 24 Low. All were accepted and assigned to a task (ruling J1); the tally is complete.
  - The 12 tasks became 13 with Task 6b, which a manual check showed was needed.
  - Every task was reviewed, with fix rounds.
  - The coordinator checked in the built-in Chromium browser on dev PG and dev MySQL.
  - A final whole-branch review on the most capable model came back "with fixes", with no Critical findings and no path to a wrong-row edit, a false outcome or a resend.
  - One fix wave followed: bounded undo (K22), K3b, the "applying" save awaited for up to 2 s before the apply is sent, the afterglow for an unknown outcome with applied changes, the restore toast naming an unreadable set, and one pydantic validation per workspace save (10,000 changes: 305–314 → 229–241 ms).
  - A scoped re-review found it merge-ready (an undo/redo fuzz against an unbounded reference, and 19 identical `PUT /workspace` bodies before and after). The coordinator then applied its two small items: `CHANGE_CELLS` = 8, and a guard so that a throw before the save's own guard cannot hold the commit.

## Outcome against the pain points (feature-priorities §2, §3, §8)

- **S1, which became a Must because the user edits data daily, is delivered.**
  - In a table tab you edit cells (inline or in the drawer), add and delete rows, undo and redo, and preview the exact DML.
  - A commit runs once in a transaction on the tab's own session. The outcome is read honestly (committed / rolled back / partly applied / unknown) and never resent.
  - The grid is re-read and keeps its place.
  - Staged edits survive a restart and are bound back to their rows by key.
- **Q1, light and fast:**
  - Staging one cell re-renders exactly that row (measured in the dev browser, and at 100k × 30 in jsdom).
  - A 10,000-statement apply costs 0.38 s of main thread, down from 3.77 s before the session-snapshot selector.
  - Undo history is capped at 100 steps and a cell budget (K22).
  - PostgreSQL's key order never forces a full sort before the first page (K13 asks the planner).
  - Bundle 1,366 → 1,444 kB (gzip 441 → 468). Sidecar +12 KB. Cold start 0.97–1.25 s. Backend `footprint` 76–77 MB (05a: 76–79).
- **Q2, never lose work:**
  - Editors stage their text when they unmount.
  - A refused Set NULL keeps the editor (K16). A refused staging offers Copy text (K20).
  - ⌘W flushes an open editor before asking.
  - A restored tab that was never shown still asks before closing.
  - "Applying" is saved as unknown before the apply is sent (H1, fix wave).
  - A cancelled Refresh keeps the rows from before (K17).
  - A lone surrogate or a deep cell can no longer make every save fail (K5, K7, depth 64).
  - A staging over the caps is kept without its hidden UPDATEs (K3b).
- **Q3, trustworthy:**
  - A loaded row is identified by its key everywhere: binding, overlay, editors, anchor.
  - Rows that share a key are read-only (K6), and a vanished selected row clears the selection (K14b).
  - PostgreSQL's heap order no longer moves an edited row away (K13).
  - The preview's text is what runs, and only that dialog's plan is applied.
  - An unknown outcome keeps Commit off until fresh rows are read, and the next preview warns about inserting new rows twice.
- **Q4, focus:** table-data tabs of the four relational engines only. No typing-to-edit and no paste (spec R-05-9).
- **Q5, multi-engine:** PG, MySQL, SQL Server and SQLite go through the `row_editing` capability, and the PG key order sits behind `orders_browse_by_key`. Couchbase keeps 04c's document editor.
- **Q6, macOS:** ⌘S, ⌘Z / ⇧⌘Z, ⌘⌫, ⌥⌘N, Return / F2, ⌘W asks, and every edit action is in ⌘⇧P (the fallback if WKWebView takes ⌘Z).
  - Zhuyin is guarded in every key map (`isImeKeyEvent`). It is verified only by code review and the user's list.
  - Classic scrollbars have reserved room.
  - Engine icons are monochrome, as the user asked.

## Rulings made during execution
These are in `.superpowers/sdd/2026-10-20-05b-grid-editing-frontend/progress.md` until the workspace is deleted. They are summarised here.

### Persistence and memory
- **K3 / K3b.** UPDATEs hidden under a DELETE are kept and saved: Restore brings them back. When a staging exceeds the 10,000-change or 8 MiB cap, it is written without them; a commit only sends the DELETE. If it still doesn't fit, it is not kept, and the edit bar says so.
- **K5 / K7 and the depth cap.**
  - A staging the backend model would refuse is never written, and the edit bar says why.
  - Lone surrogates in tab text are stored as U+FFFD.
  - A key or original nested more than 64 levels is not kept.
  - The workspace's own text fields are repaired too.
  - A malformed local set costs only its tab, and the restore toast names it.
- **K22.** Undo history is capped at 100 steps and about 2,000,000 cells (each change weighs 8 cells plus its entries), dropping the oldest steps and always keeping the last one. In V8, 100 mass steps over 29 columns went from 1,194 to 34 MB, and the worst one-column case from 145 to 55 MB. `withoutAll` rebuilds records instead of using `delete`. After mass edits, only the last few mass steps can be undone.
- **Kind-aware no-op.** `yes` typed over `true`, or hex in another case, is not a change. Numbers compare by exact text, because `1.0` and `1` can differ on the server.

### Binding and identity
- **K1, K8.** Rows are bound by the staged key while it is on another key. They are bound whenever the result has the key, not only while something is staged.
- **K2.** Column names are looked up as own properties only (a column may be named `constructor` or `__proto__`).
- **K6.** Rows that share a key are read-only and draw a neutral mark.
- **K18.** Each editor is keyed by its cell and stages into its own cell.

### Order and anchoring
- **K13.**
  - A PG table tab with no sort of its own browses in its key's order when `EXPLAIN` shows no Sort node, or only one over at most 10,000 estimated rows. It is a real header sort the user can clear.
  - The EXPLAIN runs in an explicit transaction with 1 s statement and lock timeouts, and only on an idle auto-commit connection.
  - The first browse waits for the target for at most 2 s, counted from when the session opens.
- **K14 / K14b.**
  - A Refresh or a commit's re-read puts the selection and scroll back by the anchor row's key: the selected row, or else the first visible one.
  - A key that is gone or repeated clears the selection.
- **K17.** A re-run of the same browse that ends early keeps the rows from before ("Refresh cancelled: showing the rows from before"). A sort or filter cut short shows "(partial)".

### Commit and outcomes
- **K9 / K10.** An apply that applied nothing (not sent, rolled back) or applied only part restores the "refreshed" warning it replaced. Only a commit clears it.
- **K11 / K21.**
  - "Close — outcome unknown" appears after 30 s with no event, or 30 s after a Cancel brought nothing back.
  - It says "Stopped waiting for the outcome: the changes may have been applied…", not "connection lost".
- **Cancel stays enabled.** A second Cancel lets SQL Server drop a stuck connection. The apply's last warning shows under the progress line.
- **M5, the afterglow.** The pre-commit display stays until the re-read's first page is in, so committed values don't flip back and new rows stay on top. The fix wave also uses it for an unknown outcome that lists applied changes.
- **Session snapshot.** `useSession` keeps the snapshot while only a running apply's statement count grows (`session/sessionSnapshots.ts`).

### Editors and keys
- **K12.** A Cancel that takes Refresh's place ignores clicks for its first 400 ms.
- **K15.** With vim on, Escape in the drawer belongs to vim. Cancel with the button.
- **K16 / K20.** A refused Set NULL keeps the editor and its text. A refused staging toasts "… What you typed was not staged." with Copy text, which is available only while the toast is shown, about 8 s.
- **K19.** Nothing can be staged on held (stale) or locked rows. Their menus offer reverts only, and the grid also guards this itself.
- **Plain-text drawer keys.** Return types "\n", Tab types "\t", and Backspace deletes one character. JSON keeps auto-close and indentation.
- **Inline editor.** It is at least as wide as its cell. An unpinned cell's editor draws under the pinned columns.

### Close guard
- **M6.** The table tab and 04c's DocumentTab share one close question, `useDiscardQuestion` in `closeGuards.ts`. DocumentTab's behaviour was checked line by line against the old code.

### Other
- **Engine icons.** They use `currentColor`. SQL Server uses lucide's Server glyph, because no freely licensed Microsoft mark exists.

## Carries (later work, none blocking)
- **L4.** The ⌘S window handler exists three times (query, document, table). Merge it into a shared hook in a DRY pass.
- **Component tests.** The React wiring has none; H1 was not adopted. Pure modules have Vitest, and the reviewers used scratch jsdom probes. If wiring regressions appear, add jsdom + testing-library as a dev dependency.
- **The palette reads actions at render.** A command can stay greyed until a key is pressed.
- **A crashed tab (error boundary) has no close guard.** This is true for every guard kind.
- **Light-mode contrast.** Muted italic NULL/DEFAULT text on the light tints is 4.1–4.4:1, against AA's 4.5.
- **K13 and big tables.** A big uncorrelated table (larger than `effective_cache_size`) stays in heap order, so an edited row can move after a commit there. A foreign partition counts as ordered.
- **SQL Server.** 1,000 UPDATEs take 1.5–1.6 s, and statement batching is 05a/spec §12's item. Under IMPLICIT_TRANSACTIONS, Roll back helps only once.
- **Tests.** The backend tests' `Recorder` is O(n²) in events, so 05a's in-process 10,000-statement numbers are a little pessimistic.
- **Undo doesn't weigh value text.** 100 edits of one cell, each with a new 1,000,000-character text, weigh 399 cells but keep 96 MB. That needs 100 huge pastes, so it stays as is.
- **`useSession`'s selector writes a ref.** It is deterministic, but not the usual pattern. Once `use-sync-external-store` is a dependency, `useStoreWithEqualityFn` would be cleaner.

## Packaged-app checks (the user's, below)
These can only be checked in the packaged app:
- WKWebView keys (A10): whether ⌘Z, ⌘⌫, ⌥⌘N and F2 reach the grid. ⌥⌘N may arrive as keyCode 229, which `isImeKeyEvent` would swallow.
- Zhuyin in the inline and drawer editors.
- WebKit clicks that do not blur an editor.
- The engine icons in light and dark.

## Incidents
- **A shared scratch mutation script edited the main checkout.** During Task 2, a script in the shared scratchpad (overwritten by another agent) briefly changed `src/features/edits/overlay.ts` in the main checkout. It was restored to HEAD and checked. From then on, mutation tooling ran only in each agent's own scratch folder and never wrote into the repo.
- **The disk ran nearly full, and OrbStack reported an I/O error.**
  - The Mac's free space fell to about 150 MiB once. Causes: agents' scratch worktrees with `npm ci`, and a reviewer's 3.2 GB scratch file.
  - OrbStack then reported an I/O error, and the user restarted it.
  - Every engine and the Couchbase seeds checked out afterwards: PG seeds, MySQL CHECK TABLE, SQL Server CHECKDB, Couchbase 500 / 12.
  - Scratch is now capped at 1 GB, and worktrees are removed after use (memory `mac-disk-space-tight`).
- **OrbStack was found stopped.** During Task 6 the dev containers were down; the agent started OrbStack and the three relational containers.
- **A mutant left scratch objects behind.** A Task 6b mutant (the one without COMMIT) left `zz_05a_type_…` and `zz_05a_domain_…` on dev PG. The agent dropped them by exact name.
- **No pymssql crashes in 05b.** Every SQL Server dispatch carried the rule against reading `.text` and the other three properties.

## 05b 測試清單（Grid 內編輯）

括號裡是 dev 資料庫上已經看到的結果。05b 的手動檢查是在內建的 Chromium 做的；打包版的 WKWebView 與注音只能你自己測。公司資料庫的檢查清單見 05a 的 note，它的 UI 項目現在可以從 grid 操作（第 11 項）。

1. **改、增、刪、commit（四個 engine 各一次）。**
   - 在一張可以亂改的表裡：把一個 bigint 改成 `9007199254740993`、一格設成 NULL（⌥⌘N）、另一格清成空字串，新增一列（identity／auto-increment 留 DEFAULT），刪一列。
   - 按 ⌘S：預覽依序列出交易開頭、DELETE、UPDATE、INSERT、COMMIT，DELETE 排最前、紅色，文字就是送出的文字。
   - 按 Commit：grid 重讀，捲動位置不變，顯示 server 存下的值，空字串和 NULL 分得出來。
   - （dev PG 已確認：server 上的值、History 的 ok 紀錄。）
2. **PostgreSQL 依 key 排序。**
   - PG 的表格 tab 打開時標題列是 `id ▲`，SQL 列是 `ORDER BY id ASC`。
   - 改一列、Commit 後，那一列留在原位：PG 的 UPDATE 會把列存到別處，沒有排序時改過的列會跑走。
   - 按標題清掉排序，就回到 server 的順序。
   - 很大、key 不連續的表（例如 UUID 主鍵）可能照舊不排序，那是刻意的：不讓 server 先排序整張表。
3. **衝突。**
   - 在 DbaGuy 改一格但還沒 commit，用別的工具改同一格，再 Commit：結果是「Nothing was applied」，並指出那一列（dev 已確認）。
   - Show in grid 會選到那一格，列號是紅的。
   - Refresh 後那格有紅色角標，tooltip 寫 server 現在的值；右鍵 Keep mine 後再 commit 就會成功。
   - 別人改同一列的其他欄：照常 commit，別人的值會留著。
4. **Refresh 保留位置。**
   - 捲到大表中間、選一格，按 Refresh：畫面不閃白，位置與選取不變。
   - 刪掉畫面上方的幾列再 Refresh：選取還在同一列。
   - 選取的那一列被別人刪了：Refresh 後沒有選取，不會跳到別的列（dev 已確認）。
   - sort、filter 都回到頂端。
5. **唯讀。** view、沒有 PK 或唯一鍵的表、唯讀連線：工具列寫「Read-only · 原因」，Return 照舊開值檢視器（dev 已確認 view 與沒有 key 的表）。
6. **重開 app。**
   - 有暫存的 tab 結束 app（也試試強制結束）再開：暫存還在，綁回原本的列，tab 標題前有「•」。
   - ⌘W 會問「Discard N staged changes to …?」，按 Cancel 會留下。
   - inline 編輯器裡打了字還沒按 Return 就 ⌘W：也會問，而且算進數量（dev 已確認）。
7. **注音（只能手動測）。**
   - 在 inline 編輯器與 drawer 編輯器用注音輸入中文：組字中按 Enter 選字，不能暫存，也不能關掉編輯器。
   - 組字中的 Escape 只取消組字，⌘S 不開預覽。
   - 在 grid 上直接打注音什麼都不會發生。沒有「打字即編輯」，要先按 Return。
8. **打包版的按鍵（A10）。**
   - 在 DbaGuy.app 裡、grid 有焦點時，這些鍵都要有作用，沒被 Edit 選單吃掉：⌘Z／⇧⌘Z 復原／重做暫存、⌘⌫ 標刪列、⌥⌘N 設 NULL、F2 開編輯器。
   - ⌥⌘N 在 inline 輸入框與 drawer 裡也要有作用。
   - 若 ⌘Z 沒作用，右鍵的 Revert 與 ⌘⇧P 的「Undo staged change」可以用。請回報是哪幾個鍵。
   - 在編輯器打字後直接點 drawer 的 Apply／Cancel／Set NULL，要照按鈕做。
9. **引擎圖示。**
   - 亮色與暗色主題下，樹、tab、連線選擇器、History、⌘P 裡的引擎圖示都是單色、清楚可辨。
   - SQL Server 是中性的 server 圖示：Microsoft 的商標要授權，沒有可以自由使用的官方 logo。
10. **（可選）結果不明。**
    - commit 途中結束 backend 或斷網：對話框寫「may or may not have been applied」，Commit 停用到 Refresh 之後，不會自動重送。
    - 之後的預覽會提醒：新列會再新增一次（dev 已用 MyISAM 的重複 key 重現）。
    - 一直等不到結果時，30 秒後可以按「Close — outcome unknown」。
11. **公司資料庫（05a 清單的 UI 項目）。**
    - MySQL：
      - `SELECT @@sql_mode`；
      - 含反斜線的值存回原樣；
      - 非嚴格模式下，太長的字串會被截斷（commit 後重讀才看得到）；
      - MyISAM 表的預覽警告，以及「Partly applied」與「Outcome unknown」（dev 已確認兩者）。
    - SQL Server：
      - 有 `IMPLICIT_TRANSACTIONS` 的 login 應該出現 Roll back／Reconnect；
      - 改 varchar key 的表的一格要多久；
      - trigger；
      - temporal、graph、ledger 表的唯讀原因；
      - dmy 語系下的日期。
    - PostgreSQL：domain、enum、array、timestamptz 改一格，不該出現假衝突。
    - 每台 server：一次 1,000 個變更的 commit 時間、進度與 Cancel。Cancel 可以再按一次，SQL Server 卡住時會斷線。
12. **大量。**
    - 選超過 10,000 列按 ⌘⌫：提示改用 SQL。
    - 一次刪 1,000 列的 commit 有進度。
    - 對 1 萬列做好幾次全選的 Revert 或 Set NULL 之後，只能復原最後幾步（undo 有記憶體上限）。
13. **Vim 模式（如果你有開）。** drawer 裡的 Escape 交給 vim，不會取消編輯；要取消就按 Cancel 鈕。

已知的限制，不要當成 bug 回報：
- 沒有貼上、沒有「打字即編輯」，Add row 沒有快捷鍵。
- 超過 64 KiB 的值只能設 NULL。
- 浮點、datetime2(7)、text／xml 等欄不檢查並行修改（預覽會逐句列出）。MySQL 與 SQL Server 依 collation 比較。
- commit 後，新列出現在 server 給的位置。
- 超過 8 MiB 的暫存不跨重開，編輯列有提示：先去掉標刪列底下的修改再存，還是放不下才不存。
- undo 不跨重開。
- 被拒絕暫存的文字只能在提示出現的約 8 秒內按 Copy text 複製。
