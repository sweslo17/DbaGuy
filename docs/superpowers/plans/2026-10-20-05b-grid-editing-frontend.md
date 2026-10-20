# 05b: Grid editing, frontend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edit rows in a table-data tab's grid — change cells, add and delete rows — with every change staged and visible, then ⌘S shows the exact statements the backend will send and Commit applies them in one transaction on the tab's own session, with an honest outcome (committed, nothing applied, partly applied, unknown) and nothing ever sent twice; staged changes survive a crash or a restart and closing the tab asks first. Also: the engine icons become monochrome (SQL Server keeps a neutral glyph, see H2), and Refresh keeps the scroll position and the loaded rows.

**Architecture:** Pure modules first, each with Vitest: `features/edits/cells.ts` (staged values vs loaded cells), `staging.ts` (one immutable reducer with undo/redo, keyed by the row's key as loaded, CR-4), `overlay.ts` (the staged changes laid over the loaded rows by key, reusing every untouched row object so the memoized grid rows skip them, Q1), `savedEdits.ts` (what `workspace.json` keeps, within the backend's limits), `commitFlow.ts` (an apply's outcome read from its execution; an apply without `edit_outcome` is unknown), `editing.ts` / `editKeys.ts` (how an edit starts and ends, the keys, IME-guarded). The staged changes live outside React in `stores/stagedEdits.ts` (like `editorBuffers`), read by the one workspace writer and the close guard. The grid gets an optional `edits` prop (marks, keys, menus, an inline editor slot and the drawer editor); `useTableEdits` wires staging into the Data view, `useCommit` previews (`POST /edits/preview`, a plain call) and applies (`apply_edits` on the session) and feeds the outcome back. The backend adds only `WorkspaceTab.staged_edits` (`StagedEditsFile`, `extra="forbid"`, D-05-11 limits) and its workspace repair.

**Tech Stack:** React 19 + TypeScript 6, zustand 5, TanStack Query 5 / Virtual 3, CodeMirror 6 (`@codemirror/view` gutters and decorations, `@codemirror/lang-json` already in), radix-ui, lucide-react, Vitest 5.0.3 (node, pure modules only); Python 3.13 sidecar, pydantic v2, pytest. No new dependency on either side.

**Spec:** docs/superpowers/specs/2026-10-19-05-grid-editing-design.md — §11 item 2 (05b): §3.6 (frontend, all of it), §3.7 (workspace: the backend `WorkspaceTab.staged_edits` and the frontend `savedEdits`), §3.6.6 (changes to existing behaviour), D-05-10, D-05-11, D-05-12, D-05-14, R-05-6, R-05-7, R-05-8, R-05-9, R-05-11, CR-1…CR-4, §10 A10–A12, §13 (docs). The backend contract is 05a's: docs/superpowers/notes/2026-10-20-05a-grid-editing-backend-followups.md, "Appendix: the 05b contract" (every code, status, event and field) and its Carries rows marked 05b; 05a's plan: docs/superpowers/plans/2026-10-19-05a-grid-editing-backend.md. The closest frontend precedent is 04c's document tab (docs/superpowers/plans/2026-10-19-04c-couchbase-documents.md, Tasks 6–7, and its follow-ups note: the pure reducer C35, the close guard C20/C28, writes as plain calls C32/C45).

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as against plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 05b serves it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 | The overlay never copies the result: one new row array per changed row, every other row the loaded object, so a stage re-renders that row alone (Task 3 tests; Task 9 check 28 measures it on 100,000 × 30 with a dev-only render counter). Binding keys rows only while something is staged on loaded rows, and Load more keys only the new page (Task 3). No new dependency; Task 12 records the bundle and the startup / `footprint` against 05a. |
| §2 Q2 穩定、不丟工作 | Staged edits are in `workspace.json` (Task 4) and come back bound to their rows by key; one oversized set is left out instead of failing every save (B40), and the backend drops a bad one rather than the tab. Closing a tab with staged edits asks (Task 11, also a restored tab never shown); a failed or unknown commit keeps the staging; an editor's text is staged when its row leaves the rows shown (H12). |
| §2 Q3 可信任 | The dialog shows the backend's statements and applies that preview's plan only (05a M7, H8); an apply that ends without its outcome is unknown, Commit stays off until fresh rows, and nothing is resent (Task 5, Task 10). NULL ≠ empty string, digits never through a JS number, cut values never edited from their preview (Tasks 2, 8). |
| §2 Q4 聚焦 80% | Table-data tabs only; no query-result editing, no paste, no type-to-edit, no enum menus (spec §1). |
| §2 Q5 多引擎 | One UI for PostgreSQL, MySQL, SQL Server and SQLite; engine differences come only from the edit target (kinds, writable, reasons) and the backend's statements; Couchbase has no table tab (R-05-12). |
| §2 Q6 macOS 慣例 | Return / F2 / double click edit, Esc cancels, ⌘S previews, ⌘⌫ deletes rows, ⌥⌘N sets NULL, ⌘Z / ⇧⌘Z undo staging, ⌘W asks (Tasks 8, 9, 11); the packaged WKWebView check is on the user's list (A10). |
| §3 S1 (Must, §8) | The whole flow: stage → preview → one commit (Tasks 2–11). |
| §3 M3 每個指令都能用鍵盤 | ⌘⇧P gets Add row, Delete selected rows, Set NULL, Commit changes…, Discard all changes, Undo / Redo staged change (Task 11). |
| §3 M7 值的呈現正確 | Staged values drawn in the loaded cells' encoding (D-05-10): big numbers as text, bytes as bytes, NULL vs empty vs DEFAULT told apart (Tasks 2, 3, 7). |
| §3 M11 安全 | One transaction; the red connection's Commit is a danger button and the connection is named in the dialog; deletes counted first in danger; read-only connections, views and keyless tables have no edit entry and say why (Tasks 9, 10). |
| §8 取代 Navicat／Beekeeper／DBeaver 的日常 | "Open the table → filter → change → save" without leaving DbaGuy; Refresh keeps the place (D-05-12, Task 6). |
| User requests (this plan's brief) | Engine icons monochrome, SQL Server mark checked (Task 1); classic scrollbars: the edit bar never scrolls, the preview's statements and the drawer editor reserve theirs (Tasks 8, 9, 10); Zhuyin: every Enter / Tab / Esc / ⌘S handler checks `isImeKeyEvent` first, no type-to-edit (Task 8). |

## Global Constraints

- Scope: spec §11 item 2 (05b) on branch `feat/05-grid-editing` (05a ends at 6d88e47). ⑤ merges to `main` after this plan's final review, not inside any task.
- No new dependency (frontend or Python). No Testing Library / jsdom (ruling H1): Vitest tests pure modules only (`vite.config.ts` includes `src/**/*.test.ts`), React wiring is checked by hand.
- Binding values, each defined once, in the file named:
  - `features/edits/staging.ts`: `MAX_CHANGES = 10_000` (D-05-11), `UNDO_LIMIT = 100` (D-05-14).
  - `features/edits/savedEdits.ts`: `MAX_SAVED_VALUE_CHARS = 1_048_576`, `MAX_SAVED_CHARS = 8 * 1_048_576` (D-05-11; the backend's `MAX_VALUE_CHARS` and the new `MAX_STAGED_CHARS` in `domain/edits.py`).
  - `features/edits/editing.ts`: `INLINE_MAX_CHARS = 1_000` (R-05-9). `features/edits/CellEditor.tsx`: `INPUT_MAX_WIDTH = 480` (§3.6.3).
  - `features/session/SessionController.ts`: `PAGE_SIZE = 500` (existing), `MAX_PAGE_SIZE = 10_000` (`ExecuteCommand.page_size` le=10,000; D-05-12).
  - `features/workspace/closeGuards.ts`: `CLOSE_GUARD_ORDER.edits = 5` (§3.6.6), close-only.
- From the spec, verbatim where it binds this plan:
  - R-05-8: 「一律以文字暫存與傳遞（不經 JS Number）… **NULL 與空字串**：清空輸入就是空字串；NULL 只能明確設定（⌥⌘N、選單、編輯器的 NULL 按鈕）。**DEFAULT**：只給新列… 驗證只在 backend 預覽時做（DRY，前端不重寫規則）」 — the frontend never validates numbers, JSON, hex or bools; it only draws them (D-05-10).
  - R-05-9: 「不做打字即編輯：grid 不是文字欄位，注音的第一個組字鍵會遺失；先按 Return。每個 Enter／Tab／Esc／⌘S 處理先檢查 `isImeKeyEvent`」.
  - R-05-6: 「從不自動重送… 前端：apply 請求沒有收到 `edit_outcome` 就結束（socket 斷了、backend 重啟）一律當 unknown；指令根本沒送出（`not_connected`）則什麼都沒發生」.
  - R-05-11: 關 tab：「Discard N staged changes to <table>?」（Discard／Cancel，沒有 Commit）；沒顯示過的還原 tab 也問；commit 進行中不能關；結束 app 不問.
  - D-05-12 + CR-1: Refresh and the re-read after a commit keep the vertical position and the selection and reload the loaded row count (≤ 10,000), only for engines with `row_editing`; sort and filter start at the top.
  - 05a M7: 「05b 只套用畫面上那個對話框的 plan」.
- Frontend rules (CLAUDE.md): colours only through the token classes or `var(--color-…)` (no hex, no rgb); every new React key handler checks `isImeKeyEvent(e)` from `@/lib/ime` first (CodeMirror keymaps skip composition themselves); editor text never in React state except the inline `<input>`'s own; staged edits never in React state (`stores/stagedEdits.ts`, read by the one workspace writer); the user's Mac shows classic scrollbars, so a fixed-height scrolling area reserves its scrollbar (`[scrollbar-gutter:stable]`, `STABLE_SCROLLBAR` for CodeMirror) or never scrolls; API types only through `src/lib/api/types.ts`; `src/lib/api/openapi.json` and `schema.d.ts` only from `npm run gen:api`.
- Writes: a preview is a plain call (`previewEdits`, never a `useQuery`: the app's QueryClient retries once); the apply is the session's `apply_edits` command, sent once; nothing logs a value, a key or SQL text (console lines carry counts and outcomes only).
- Tests per task: `npm test` (Vitest) for the pure modules a task adds; `npm run typecheck && npm run build` after every task that touches `src/`; Task 4 also `cd backend && .venv/bin/pytest -q tests/test_local_stores.py tests/test_edit_models.py tests/test_edits_api.py tests/test_edit_plans.py` and `.venv/bin/pytest -q tests/test_api.py -k workspace`, then the full backend suite once (`cd backend && .venv/bin/pytest -q`, dev containers up). Task 12 runs both suites in full.
- Manual checks: the coordinator runs each UI task's numbered list in the in-app browser with `scripts/dev.sh --web` and `http://localhost:1420` (`dev/data`, never the app's data). Agents never use the in-app browser. Scratch tables only through `scripts/scratch_tables.py` (Task 6): created and dropped by exact name, `zz_05b_*` only; every list from Task 6 on starts with `create` and its last item is `check` → "No zz_05b_ object left". Profiles made for checks use `save_password` off. Never touch `~/Library/Application Support/DbaGuy`. Never kill a process by pattern.
- The dev profiles the lists use (create the missing ones in the connection dialog, password not saved): "dev pg" (127.0.0.1:54317, dbaguy/dbaguy, database dbaguy), "dev mysql" (127.0.0.1:33067, dbaguy/dbaguy, database dbaguy, SSL Prefer), "dev mssql" (127.0.0.1:14337, dbaguy / Dbaguy_dev1, database dbaguy), "zz_05b sqlite" (file `<repo>/dev/data/scratch/zz_05b.sqlite`, made by the helper), "dev pg read-only" (dev pg with Read-only on).
- Repo is public: fake dev credentials only. Never read `.text`, `.message`, `.srvname` or `.procname` on a pymssql exception built in Python (the helper never builds one).
- Commit after each task, staging only that task's files by path (never `git add -A`); the message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Patches: a modified file's change is given as a unified diff against the state after the previous task (`git apply` takes it; or edit by hand to the same result); a new file is given whole. Every task's code was compiled (`tsc --noEmit`), tested (`vitest run`) and built (`vite build`) in a scratch worktree while planning, in order, so each diff applies on the one before.

## Review Focus

1. **NULL, the empty string and DEFAULT are never confused by the editor, and an input method's keys never stage.** Return on an untouched NULL or DEFAULT cell stages nothing; typing then erasing stages ''; ⌥⌘N stages NULL; the Enter that picks a Zhuyin character (also WebKit's late keyCode 229 Enter) neither stages nor opens the preview. → Task 8 `editing.test.ts` "stages nothing unless something was typed…", "never stages on the Enter that picks a character", "ignores an input method's keys…"; Task 2 `staging.test.ts` "tells NULL from the empty string"; Task 9 manual check 4.
2. **A row with both a DELETE and an UPDATE sends only the DELETE, and committing it removes both.** Restoring the row brings the UPDATE back; undo never resurrects a change that took effect. → Task 2 `staging.test.ts` "sends only the DELETE…", "removes the hidden UPDATE…", "a partial outcome removes what took effect and clears the history…".
3. **An apply whose outcome never arrives is unknown and is never resent; a refusal is not unknown.** A socket that drops or a backend that restarts mid-apply gives "Outcome unknown" (Commit off until fresh rows); a refusal at the WebSocket layer (`invalid_command` then `edit_outcome`) is rolled back; a command that never left (`not_connected`) is "not sent". → Task 5 `commitFlow.test.ts` "calls an apply that ended without its outcome unknown", "reads a refusal at the WebSocket layer as rolled back…", "calls a command that never left not sent"; `execution.test.ts` "is not over at a terminal error…"; Task 10 manual check 8.
4. **A staged change stays on its row whatever the rows do.** Load more, a sort, a filter and a Refresh (new row objects, equal values) find it by key; a row not shown counts in "in rows not shown"; an untouched row keeps its object (Q1); a server change to a staged cell shows as a conflict. → Task 3 `overlay.test.ts` "finds a change after Load more, a sort and a refresh…", "keys only the new page after Load more…", "reuses a changed row's object…", "marks a conflict…"; Task 9 manual checks 16–19, 21, 22, 28.
5. **One large or damaged staged set never costs the workspace.** Beyond 1 MiB a value, 8 MiB in all or 10,000 entries, nothing is written for the tab (the edit bar says so) and every other tab still saves; the backend drops a staged set its model refuses and keeps the tab and its other fields. → Task 4 `savedEdits.test.ts` "keeps nothing, and says so, beyond…"; `test_local_stores.py` `test_a_tab_with_staged_edits_the_model_refuses_keeps_everything_else`, `test_staged_edits_beyond_their_limits_are_refused`.

---

## File Structure

```
backend/dbaguy_backend/
  domain/edits.py                 StagedCell, StagedRowUpdate, StagedRowInsert, StagedEditsFile, MAX_STAGED_CHARS;
                                  _text_chars / _counted_ids shared with EditChanges (DRY)
  domain/workspace.py             WorkspaceTab.staged_edits (table tabs only)
  services/workspace_service.py   the repair drops refused staged edits (B40)
backend/tests/test_local_stores.py
scripts/scratch_tables.py         (new) zz_05b_* scratch tables for the manual checks: create / drop / check / sql / hold
src/
  components/EngineIcon.tsx       monochrome (currentColor); SQL Server keeps lucide's Server (H2)
  styles.css                      the --engine-* tokens go
  lib/ime.ts                      isImeKeyEvent takes any object with the three signals (pure key maps' tests)
  lib/tabActions.ts               (new) createTabActionRegistry: the palette's per-tab actions (DRY with query tabs)
  lib/api/{openapi.json, schema.d.ts, types.ts}   StagedEditsFile (generated; types.ts exports it)
  stores/stagedEdits.ts           (new) staged edits by tab id, outside React; dispatch caps; writer notification
  features/edits/                 (new)
    cells.ts                      CellInput; editText, sameCell, inputMatches, displayCell
    staging.ts                    the reducer: StagedEdits, stage(), toChanges(), changeCount(), findChange()
    overlay.ts                    rowKeyOf, resultColumns, bindRows, overlay(), rowRefAt, displayIndexOf, wasText
    savedEdits.ts                 savedEdits() / restoredEdits(): workspace.json ↔ the store
    editsApi.ts                   fetchEditTarget, previewEdits, editsErrorMessage, editTargetKey
    commitFlow.ts                 commitState, stagedOutcome, rerunAfter, commitReport, commitToast, problemsOf, changeLabel
    editKeys.ts                   gridEditKey, cellEditorKey (IME-guarded)
    editing.ts                    editStart, editorResult, nextWritable, INLINE_MAX_CHARS
    CellEditor.tsx                the inline <input>
    ValueEditor.tsx               the drawer's CodeMirror editor
    useTableEdits.tsx             target, binding, overlay, editing, keys, menus, selection actions
    EditBar.tsx                   + Row, N changes, Commit… ⌘S, Discard…, Read-only · reason
    PreviewDialog.tsx             the commit dialog (statements, warnings, problems, progress, outcome)
    useCommit.tsx                 preview, apply, outcome, re-run, refreshed
    editActions.ts                the table tab's palette actions
  features/grid/
    marks.ts                      (new) CellMark, RowMark, HeaderMark; the marks' classes
    renderCount.ts                (new) dev-only row render counter (Q1 check)
    EditorSlot.tsx                (new) GridEditing, GridEditor, InlineEditorSlot
    DataGrid.tsx                  edits prop, resetScrollOn, selectCell handle, drawer editor, row labels
    GridRow.tsx / GridHeader.tsx / GridMenu.tsx / GridDrawer.tsx   marks, key icon, edit menu items, DrawerPanel
    ExportDialog.tsx              ExportSource.note
  features/editor/extensions.ts   EditorLanguage "text" (no auto-closed brackets), STABLE_SCROLLBAR
  features/session/execution.ts   ExecutionKind "edits", editOutcome, firstPageDelivered; terminal codes skip edits
  features/session/SessionController.ts   applyEdits(planId), MAX_PAGE_SIZE
  features/table-data/useTableData.ts     pageSize / position per browse job, deliveredRequest; Refresh keeps position
  features/table-data/TableDataTab.tsx    the Data view's wiring: edits, commit, ⌘S, close guard, tab dot, palette
  features/workspace/{workspaceFile.ts, workspaceSync.ts}   staged_edits saved and restored (into the store)
  features/workspace/{closeGuards.ts, Workspace.tsx}        CLOSE_GUARD_ORDER.edits; the dot on table tabs
  features/palette/{commands.ts, CommandPalette.tsx}        the edit commands
  features/query/tabActions.ts    reimplemented on lib/tabActions.ts (same exports)
  features/palette/parts.tsx, features/query/ConnectionPicker.tsx   icon comments and the red chip's class
CLAUDE.md, docs/superpowers/specs/2026-10-19-05-grid-editing-design.md   (Task 12)
```

Task order follows dependencies. The engine icons (1) stand alone. The pure core comes next: staging (2), the overlay (3), persistence with its backend model (4), and the session contract with the commit logic (5). Then the UI, bottom-up: Refresh keeping its place, with the scratch helper every later check uses (6); the grid's surface for marks and editors (7); the editors and keys (8); staging from the grid (9); preview and commit (10); the close guard, the tab's dot, the export note and the palette (11). Docs, measurements and the packaged build close (12).

---

### Task 1: Engine icons — monochrome, SQL Server's mark checked

**Suggested implementer:** Sonnet.

**Files:**
- Modify: `src/components/EngineIcon.tsx`, `src/styles.css`, `src/features/palette/parts.tsx` (a comment), `src/features/query/ConnectionPicker.tsx` (the red chip's class)

**Interfaces:**
- Consumes: nothing new.
- Produces: `EngineIcon({ engine, size?, className?, decorative? })` unchanged; it now draws in `currentColor` and adds no colour class of its own. The `--engine-*` CSS variables and the `text-engine-*` utilities no longer exist (nothing else used them: before this task `grep -rn "engine-" src` finds only `EngineIcon.tsx` and `styles.css`).

Behaviour (user request 2026-10-19; ruling H2):
- Every engine mark is drawn in the text colour of where it sits: the tree row's, the tab's (active text, inactive muted), a menu or palette row's (the highlighted row's accent-fg), the red connection chip's. Engines are told apart by shape only. A profile's own colour (its dot in the tree, the tab's underline) is unchanged.
- SQL Server keeps lucide's neutral `Server` glyph (ISC). There is no freely licensed Microsoft SQL Server mark: simple-icons removed every Microsoft icon in 13.0.0 (PR #10019) because Microsoft's trademark terms require explicit permission (the icon is in 12.x, gone from 13.0.0 on); Microsoft's Azure architecture icons are licensed only "in architectural diagrams, training materials, or documentation"; devicon's code is MIT but the logo is still Microsoft's trademark. The comment in `EngineIcon.tsx` says so, so nobody adds one later.

- [ ] **Step 1: The icon.** Apply:

```diff
diff --git a/src/components/EngineIcon.tsx b/src/components/EngineIcon.tsx
index 51d91f5..074c5c1 100644
--- a/src/components/EngineIcon.tsx
+++ b/src/components/EngineIcon.tsx
@@ -15,15 +15,6 @@ const MARKS: Record<Exclude<Engine, "mssql">, string> = {
     "M20.111 14.104a1.467 1.458 0 0 1-1.235 1.503c-1.422.244-4.385.398-6.875.398s-5.454-.15-6.877-.398c-.814-.14-1.235-.787-1.235-1.503V9.417a1.57 1.56 0 0 1 1.235-1.505 15.72 15.619 0 0 1 2.156-.14.537.533 0 0 1 .523.543v3.303c1.463 0 2.727-.086 4.201-.086 1.474 0 2.727.086 4.196.086V8.342a.535.532 0 0 1 .494-.569h.027a15.995 15.891 0 0 1 2.156.14 1.57 1.56 0 0 1 1.234 1.504zM12.001 0C5.373 0 0 5.374 0 12c0 6.628 5.373 12 12 12 6.628 0 12-5.372 12-12 0-6.626-5.373-12-12-12z",
 };
 
-/** Each engine's colour: a token (light and dark values in `styles.css`), written out in full so Tailwind emits it. */
-const COLORS: Record<Engine, string> = {
-  postgres: "text-engine-postgres",
-  mysql: "text-engine-mysql",
-  mssql: "text-engine-mssql",
-  couchbase: "text-engine-couchbase",
-  sqlite: "text-engine-sqlite",
-};
-
 export interface EngineIconProps {
   engine: Engine;
   size?: number;
@@ -36,15 +27,20 @@ export interface EngineIconProps {
 }
 
 /**
- * The small mark of a database engine, in the engine's colour. On a coloured surface (a highlighted menu row, a chip)
- * the caller sets the text colour of the surface's `svg`s, which wins over the engine's colour.
+ * The small mark of a database engine, drawn in the current text colour (`currentColor`): monochrome, so it follows the
+ * text and muted tokens of both themes, and a highlighted menu row's or a coloured chip's text colour. Engines are told
+ * apart by shape; a profile's own colour (its dot, the tab's underline) is separate (user request, 2026-10-19).
+ *
+ * SQL Server has no mark of its own (ruling H2): Microsoft's trademark terms allow its logos only with explicit
+ * permission — simple-icons removed every Microsoft icon in 13.0.0 for that reason (PR #10019), and Microsoft's Azure
+ * architecture icons are licensed for diagrams, training and documentation only — so it gets lucide's neutral server
+ * glyph (ISC).
  */
 export function EngineIcon({ engine, size = 14, className, decorative = false }: EngineIconProps) {
   const a11y = decorative
     ? ({ "aria-hidden": true } as const)
     : ({ role: "img", "aria-label": ENGINE_LABELS[engine] } as const);
-  const classes = cn("shrink-0", COLORS[engine], className);
-  // Microsoft's brands are not in simple-icons, and its logo is not ours to use: SQL Server gets a plain server glyph.
+  const classes = cn("shrink-0", className);
   if (engine === "mssql") return <Server size={size} className={classes} {...a11y} />;
   return (
     <svg viewBox="0 0 24 24" width={size} height={size} fill="currentColor" className={classes} {...a11y}>
```

- [ ] **Step 2: The tokens go, and the two places that worked around the engine colours.**

```diff
diff --git a/src/styles.css b/src/styles.css
index c774882..3aae379 100644
--- a/src/styles.css
+++ b/src/styles.css
@@ -16,11 +16,6 @@
   --color-warning: var(--warning);
   --color-hover: var(--hover);
   --color-selected: var(--selected);
-  --color-engine-postgres: var(--engine-postgres);
-  --color-engine-mysql: var(--engine-mysql);
-  --color-engine-mssql: var(--engine-mssql);
-  --color-engine-couchbase: var(--engine-couchbase);
-  --color-engine-sqlite: var(--engine-sqlite);
   /* Fades in after a short delay: a quick request never flashes its "Loading…" note. */
   --animate-delayed-show: delayed-show 150ms ease-out 150ms both;
 
@@ -49,12 +44,6 @@
   --warning: #b45309;
   --hover: rgba(0, 0, 0, 0.05);
   --selected: rgba(10, 102, 217, 0.14);
-  /* The engines' brand colours (simple-icons): 3.3:1 or more on every surface, 4.0:1 or more on bg and panel. */
-  --engine-postgres: #4169e1;
-  --engine-mysql: #4479a1;
-  --engine-mssql: #cc2927;
-  --engine-couchbase: #ea2328;
-  --engine-sqlite: #003b57;
   color-scheme: light dark;
 }
 
@@ -74,13 +63,6 @@
     --warning: #fbbf24;
     --hover: rgba(255, 255, 255, 0.06);
     --selected: rgba(59, 140, 255, 0.24);
-    /* The brand hues, lightened: 4.3:1 or more on bg, panel and elevated, 3.3:1 or more on a selected row (SQLite's own
-       #003b57 is 1.3:1 on panel). */
-    --engine-postgres: #6e8ff2;
-    --engine-mysql: #5f9bc8;
-    --engine-mssql: #ee6360;
-    --engine-couchbase: #ff5559;
-    --engine-sqlite: #4ba3c7;
   }
 }
```

```diff
diff --git a/src/features/palette/parts.tsx b/src/features/palette/parts.tsx
index b406603..cc6d47a 100644
--- a/src/features/palette/parts.tsx
+++ b/src/features/palette/parts.tsx
@@ -90,8 +90,8 @@ export interface PaletteItemProps {
   pending?: boolean;
   onSelect: () => void;
   /**
-   * A lucide icon (muted, in the text colour), or a ready-made element such as an `EngineIcon`, which keeps its own
-   * colour except on the highlighted row, where its `svg`s take the row's text colour.
+   * A lucide icon (muted, in the text colour), or a ready-made element such as an `EngineIcon`, drawn in the row's text
+   * colour (the highlighted row's too).
    */
   icon?: LucideIcon | ReactElement;
   label: string;
```

```diff
diff --git a/src/features/query/ConnectionPicker.tsx b/src/features/query/ConnectionPicker.tsx
index 8082b2b..4b0dfb0 100644
--- a/src/features/query/ConnectionPicker.tsx
+++ b/src/features/query/ConnectionPicker.tsx
@@ -136,10 +136,7 @@ export function ConnectionPicker(props: ConnectionPickerProps) {
           className={cn(
             "max-w-[180px]",
             // A red profile keeps its red chip, also while disabled.
-            red
-              ? // The icons take the chip's text colour: the engine's own red would vanish on it.
-                "bg-danger font-medium text-accent-fg enabled:hover:brightness-110 [&_svg]:text-accent-fg"
-              : "enabled:hover:bg-hover",
+            red ? "bg-danger font-medium text-accent-fg enabled:hover:brightness-110" : "enabled:hover:bg-hover",
           )}
         >
           {profile && <EngineIcon engine={profile.engine} size={12} />}
```

- [ ] **Step 3: Check the types and the build**

Run: `grep -rn "engine-" src; npm run typecheck && npm run build`
Expected: no `engine-` match; no errors.

- [ ] **Step 4: Manual check list** (the coordinator, `scripts/dev.sh --web`, `http://localhost:1420`; switch the pane's colour scheme with `resize_window` `colorScheme`; this list writes nothing to any database):
  1. Light scheme. The sidebar tree: every connection row's engine mark is in the row's text colour (no blue, red or teal mark); SQL Server's is the two-box server glyph; each profile's colour dot is as before.
  2. Open one query tab per engine (PostgreSQL, MySQL, SQL Server, SQLite, Couchbase): the active tab's mark is in the text colour, the inactive tabs' in the muted colour; each tab's coloured underline is as before.
  3. A query tab's connection picker: the menu's marks are monochrome and turn white on the highlighted row; a red profile's chip shows a white mark on red.
  4. Run `SELECT 1` on two engines, open History: each entry's mark is monochrome.
  5. ⌘P: the Connections group's marks and the "Database objects · <name>" heading's mark are monochrome; the highlighted row's turns white.
  6. Dark scheme: steps 1–5 again; no mark is invisible or brand-coloured.
  7. This list wrote nothing to any database (no scratch table is involved).

- [ ] **Step 5: Commit**

```bash
git add src/components/EngineIcon.tsx src/styles.css src/features/palette/parts.tsx src/features/query/ConnectionPicker.tsx
git commit -m "feat(app): monochrome engine icons in the text colour; SQL Server keeps a neutral glyph (no free Microsoft mark)"
```

---

### Task 2: Staged values and the staging reducer, and the store

**Suggested implementer:** Opus.

**Files:**
- Create: `src/features/edits/cells.ts`, `src/features/edits/staging.ts`, `src/stores/stagedEdits.ts`
- Test: `src/features/edits/cells.test.ts`, `src/features/edits/staging.test.ts`, `src/stores/stagedEdits.test.ts`

**Interfaces:**
- Consumes: `plainValue`, `BYTES_PREVIEW`, `EncodedCell` (`@/lib/cells`); `EditKind`, `EditChanges` (`@/lib/api/types`).
- Produces (every later task relies on these names):
  - `cells.ts`: `type CellInput = { kind: "value"; text: string } | { kind: "null" } | { kind: "default" }`; `NULL_INPUT`; `interface EditText { text: string; complete: boolean }`; `editText(cell: unknown): EditText`; `sameCell(a: unknown, b: unknown): boolean`; `sameInput(a: CellInput, b: CellInput): boolean`; `inputMatches(input: CellInput, original: unknown): boolean`; `displayCell(input: CellInput, kind: EditKind | undefined): unknown`.
  - `staging.ts`: `MAX_CHANGES = 10_000`, `UNDO_LIMIT = 100`; `type RowKey = string`; `StagedUpdate { id; key; set; original; seq }`, `StagedInsert { id; values; seq }`, `StagedDelete { id; key; seq }`, `StagedProblem { column: string | null; message }`, `Staging { updates: Record<RowKey, StagedUpdate>; inserts: StagedInsert[]; deletes: Record<RowKey, StagedDelete> }`, `type UnknownOutcome = "pending" | "refreshed" | null`, `StagedEdits extends Staging { keyColumns; problems; unknownOutcome; nextSeq; past: Staging[]; future: Staging[] }`; `LoadedRowRef = { kind: "row"; rowKey; key }`, `NewRowRef = { kind: "insert"; id }`, `RowRef`, `CellTarget { row: RowRef; column: string; original?: unknown }`, `AppliedOutcome { outcome; applied: string[]; failedChange: string | null; message: string | null }`; `StageAction` (the union below); `type NewId = (prefix: "u" | "i" | "d") => string`, `newChangeId`; `rowKeyFromKey(key, keyColumns): RowKey`; `emptyStaged(keyColumns): StagedEdits`; `changeCount(s: Staging): number`; `hasChanges(s: Staging): boolean`; `toChanges(s: Staging): EditChanges`; `findChange(s: Staging, id)`; `stage(state: StagedEdits, action: StageAction, newId?: NewId): StagedEdits` (returns `state` itself when nothing changes).
  - `StageAction`: `edit {cell, input}`, `setNull {cells}`, `useDefault {cells}`, `revertCells {cells}`, `revertRows {rows}`, `deleteRows {rows}` (all already marked → restore), `restoreRows {rows}`, `addRow {id}`, `discardAll`, `keepMine {row, current}`, `setProblems {problems}`, `applyOutcome {outcome}`, `refreshed`, `undo`, `redo`.
  - `stores/stagedEdits.ts`: `type DispatchResult = "staged" | "unchanged" | "too_many" | "key_changed"`; `useStagedEdits(tabId): StagedEdits | null`; `readStagedEdits(tabId): StagedEdits | null`; `putStagedEdits(tabId, staged | null): void`; `dispatchStaged(tabId, action, keyColumns?): DispatchResult`; `forgetStagedEditsExcept(openTabIds: ReadonlySet<string>): void`; `onStagedEditsChanged(listener): () => void`.

Behaviour (spec §3.6.1, D-05-3, D-05-11, D-05-14, CR-4; rulings H3, H7, H13, H15, H18, H20):
- A loaded row's change is keyed by `RowKey` = the JSON of its key cells in key order, as loaded (`rowKeyFromKey`; `overlay.rowKeyOf` in Task 3 gives the same text from a row). A key column's own value can be changed (CR-4): the RowKey stays the loaded key.
- `edit` over a loaded row stores the cell's value as loaded the first time the column is staged (`original`), and removes the column when the input is that value again (`inputMatches`: NULL over NULL, or exactly the editor's starting text over a complete value) — there is no no-op UPDATE. A cut text or a bytes preview never matches (it is not the value). DEFAULT is never staged on a loaded row. A row marked for deletion is not edited.
- `deleteRows`: new rows go at once; loaded rows are marked; when every selected row is a loaded row already marked, they are restored. The UPDATE of a marked row stays, hidden (`changeCount` and `toChanges` leave it out: only the DELETE is sent, 05a carry); `restoreRows` brings it back.
- `revertRows` on a new row removes it (H20); `revertCells` on a new row's cell puts it back to DEFAULT.
- `keepMine` re-bases the row's set columns on the server's cells now; a column whose server value already equals the staged one goes.
- `applyOutcome` removes the changes in `applied`, and with a DELETE the UPDATE hidden under it; records the failed change's message as its problem (column null); `unknownOutcome` becomes "pending" for an unknown outcome, else null (H6); clears undo and redo whenever a change was removed or the outcome is "committed" (H7: a change that took effect must never come back by undo).
- Every other action that changes the staging is one undo step (`past`, at most UNDO_LIMIT; a new step clears `future`); `setProblems`, `applyOutcome` and `refreshed` are not steps. A change edited again loses its problems (H18). Untouched changes keep their objects (Task 3's cache relies on it).
- `toChanges` lists each kind in staging order (`seq`), an `original` per set column only.
- The store creates a tab's staged edits on the first dispatch with the table's key; with changes staged on another key, an action that adds is refused (`key_changed`, H13) while reverts, discard and undo still work; an action that would leave more than MAX_CHANGES changes is refused (`too_many`); every stored change notifies the listeners (the workspace writer, Task 4).

- [ ] **Step 1: Write the failing tests**

`src/features/edits/cells.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { displayCell, editText, inputMatches, sameCell } from "./cells";

/* Staged values against loaded cells (spec ⑤ R-05-8, D-05-10): text as typed, NULL ≠ "", bytes as hex. */

const bytes = (size: number, preview: string) => ({ $type: "bytes", size, preview });
const cut = { $type: "text", preview: "abc", size: 70_000, truncated: true };

describe("the text an editor starts with", () => {
  it.each([
    [null, "", true],
    ["", "", true],
    [42, "42", true],
    ["9007199254740993", "9007199254740993", true],
    [true, "true", true],
    [bytes(2, "dead"), "0xdead", true],
    [bytes(100, "ab".repeat(64)), `0x${"ab".repeat(64)}`, false],
    [cut, "abc", false],
  ])("%j → %j (complete: %s)", (cell, text, complete) => {
    expect(editText(cell)).toEqual({ text, complete });
  });
});

describe("whether a staged value changes the loaded one", () => {
  it("NULL matches NULL only; the empty string matches the empty string only", () => {
    expect(inputMatches({ kind: "null" }, null)).toBe(true);
    expect(inputMatches({ kind: "null" }, "")).toBe(false);
    expect(inputMatches({ kind: "value", text: "" }, "")).toBe(true);
    expect(inputMatches({ kind: "value", text: "" }, null)).toBe(false);
  });

  it("compares the text the editor started with, never a preview", () => {
    expect(inputMatches({ kind: "value", text: "42" }, 42)).toBe(true);
    expect(inputMatches({ kind: "value", text: "42.0" }, 42)).toBe(false);
    expect(inputMatches({ kind: "value", text: "abc" }, cut)).toBe(false);
    expect(inputMatches({ kind: "value", text: "0xdead" }, bytes(2, "dead"))).toBe(true);
    expect(inputMatches({ kind: "default" }, null)).toBe(false);
  });
});

describe("cells read again", () => {
  it("are the same when they carry the same value", () => {
    expect(sameCell(bytes(2, "dead"), bytes(2, "dead"))).toBe(true);
    expect(sameCell(bytes(2, "dead"), bytes(2, "beef"))).toBe(false);
    expect(sameCell(null, undefined)).toBe(true);
    expect(sameCell("1", 1)).toBe(false);
  });
});

describe("a staged value as the grid draws it", () => {
  it("keeps text, numbers' digits included, as a string", () => {
    expect(displayCell({ kind: "value", text: "9007199254740993" }, "number")).toBe("9007199254740993");
    expect(displayCell({ kind: "value", text: '{"a": 1.10}' }, "json")).toBe('{"a": 1.10}');
  });

  it("reads bools and hex as the backend will", () => {
    expect(displayCell({ kind: "value", text: " Yes " }, "bool")).toBe(true);
    expect(displayCell({ kind: "value", text: "f" }, "bool")).toBe(false);
    expect(displayCell({ kind: "value", text: "maybe" }, "bool")).toBe("maybe");
    expect(displayCell({ kind: "value", text: "0xDEAD" }, "binary")).toEqual(bytes(2, "dead"));
    expect(displayCell({ kind: "value", text: "abc" }, "binary")).toBe("abc");
    expect(displayCell({ kind: "value", text: "ab".repeat(100) }, "binary")).toEqual(bytes(100, "ab".repeat(64)));
  });

  it("draws NULL and DEFAULT as null", () => {
    expect(displayCell({ kind: "null" }, "text")).toBeNull();
    expect(displayCell({ kind: "default" }, "number")).toBeNull();
  });
});
```

`src/features/edits/staging.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  changeCount,
  emptyStaged,
  findChange,
  rowKeyFromKey,
  stage,
  toChanges,
  UNDO_LIMIT,
  type LoadedRowRef,
  type NewId,
  type StageAction,
  type StagedEdits,
} from "./staging";

/*
 * The staging reducer (spec ⑤ §3.6.1): changes by key, never by position; one undo step per action; no no-op UPDATE;
 * NULL is not the empty string; the DELETE alone is sent for a key that also has an UPDATE (05a carry).
 */

/** Change ids in order: u:1, i:2, d:3, … */
function counter(): NewId {
  let n = 0;
  return (prefix) => `${prefix}:${++n}`;
}

const row = (id: number): LoadedRowRef => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } });
const value = (text: string) => ({ kind: "value", text }) as const;

/** Runs the actions from an empty staging of the `id` key. */
function run(...actions: StageAction[]): StagedEdits {
  const newId = counter();
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

describe("editing a loaded row", () => {
  it("stages the cell with the value it was loaded with, by the row's key", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") });
    expect(state.updates[row(1).rowKey]).toEqual({
      id: "u:1",
      key: { id: 1 },
      set: { label: value("b") },
      original: { label: "a" },
      seq: 0,
    });
    expect(changeCount(state)).toBe(1);
  });

  it("keeps the first original when the cell is edited again, and one change id per row", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "edit", cell: { row: row(1), column: "label", original: "b?" }, input: value("c") },
      { type: "edit", cell: { row: row(1), column: "note", original: null }, input: value("n") },
    );
    expect(state.updates[row(1).rowKey]).toMatchObject({
      id: "u:1",
      set: { label: value("c"), note: value("n") },
      original: { label: "a", note: null },
    });
  });

  it("removes the cell when the value typed is the one it was loaded with: there is no no-op UPDATE", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "edit", cell: { row: row(1), column: "label", original: "b" }, input: value("a") },
    );
    expect(state.updates).toEqual({});
    expect(changeCount(state)).toBe(0);
    expect(run({ type: "edit", cell: { row: row(2), column: "n", original: 42 }, input: value("42") }).updates).toEqual(
      {},
    );
  });

  it("tells NULL from the empty string", () => {
    const empty = run({ type: "edit", cell: { row: row(1), column: "note", original: null }, input: value("") });
    expect(empty.updates[row(1).rowKey].set).toEqual({ note: value("") });
    const nulled = run({ type: "setNull", cells: [{ row: row(1), column: "note", original: "" }] });
    expect(nulled.updates[row(1).rowKey].set).toEqual({ note: { kind: "null" } });
    expect(run({ type: "setNull", cells: [{ row: row(1), column: "note", original: null }] }).updates).toEqual({});
  });

  it("keeps a big integer's digits as typed text", () => {
    const state = run({
      type: "edit",
      cell: { row: row(1), column: "big", original: "1" },
      input: value("9007199254740993"),
    });
    expect(toChanges(state).updates?.[0].set).toEqual({ big: value("9007199254740993") });
  });

  it("never edits a cut value from its preview: staging over it always counts as a change", () => {
    const cut = { $type: "text", preview: "abc", size: 70_000, truncated: true };
    const state = run({ type: "edit", cell: { row: row(1), column: "note", original: cut }, input: value("abc") });
    expect(state.updates[row(1).rowKey].original).toEqual({ note: cut });
  });

  it("does not stage DEFAULT on a loaded row, nor edit a row marked for deletion", () => {
    expect(
      run({ type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: { kind: "default" } }).updates,
    ).toEqual({});
    const state = run(
      { type: "deleteRows", rows: [row(1)] },
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
    );
    expect(state.updates).toEqual({});
  });

  it("returns the same state for an action that changes nothing", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") });
    expect(
      stage(state, { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") }),
    ).toBe(state);
    expect(stage(state, { type: "revertCells", cells: [{ row: row(2), column: "label" }] })).toBe(state);
  });
});

describe("new rows", () => {
  it("starts every cell at DEFAULT and stages values, NULL and DEFAULT again", () => {
    const id = "i:new";
    const at = (column: string) => ({ row: { kind: "insert", id } as const, column });
    const state = run(
      { type: "addRow", id },
      { type: "edit", cell: at("label"), input: value("x") },
      { type: "setNull", cells: [at("note")] },
      { type: "edit", cell: at("code"), input: value("c") },
      { type: "useDefault", cells: [at("code")] },
    );
    expect(state.inserts).toEqual([{ id, values: { label: value("x"), note: { kind: "null" } }, seq: 0 }]);
    expect(toChanges(state).inserts).toEqual([{ id, values: { label: value("x"), note: { kind: "null" } } }]);
  });

  it("goes at once when deleted or reverted", () => {
    const fresh = { kind: "insert", id: "i:a" } as const;
    expect(run({ type: "addRow", id: "i:a" }, { type: "deleteRows", rows: [fresh] }).inserts).toEqual([]);
    expect(run({ type: "addRow", id: "i:a" }, { type: "revertRows", rows: [fresh] }).inserts).toEqual([]);
  });
});

describe("deleting rows", () => {
  it("marks loaded rows, and restores them when every selected row is marked already", () => {
    const marked = run({ type: "deleteRows", rows: [row(1), row(2)] });
    expect(Object.values(marked.deletes).map((d) => d.key)).toEqual([{ id: 1 }, { id: 2 }]);
    expect(stage(marked, { type: "deleteRows", rows: [row(1), row(2)] }).deletes).toEqual({});
    // Not all marked: the unmarked one is marked too, nothing is restored.
    expect(Object.keys(stage(marked, { type: "deleteRows", rows: [row(2), row(3)] }).deletes)).toHaveLength(3);
  });

  it("sends only the DELETE for a key that also has an UPDATE, and restoring brings the UPDATE back", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "deleteRows", rows: [row(1)] },
    );
    expect(changeCount(state)).toBe(1);
    expect(toChanges(state)).toEqual({ updates: [], inserts: [], deletes: [{ id: "d:2", key: { id: 1 } }] });
    const restored = stage(state, { type: "restoreRows", rows: [row(1)] });
    expect(toChanges(restored).updates).toHaveLength(1);
  });

  it("removes the hidden UPDATE with its DELETE once the DELETE is committed", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "label", original: "a" }, input: value("b") },
      { type: "deleteRows", rows: [row(1)] },
    );
    const done = stage(state, {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: ["d:2"], failedChange: null, message: null },
    });
    expect(changeCount(done)).toBe(0);
    expect(done.updates).toEqual({});
  });
});

describe("reverting", () => {
  it("reverts one cell, a row, or everything", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "edit", cell: { row: row(1), column: "b", original: 1 }, input: value("2") },
      { type: "deleteRows", rows: [row(2)] },
    );
    expect(
      stage(state, { type: "revertCells", cells: [{ row: row(1), column: "a" }] }).updates[row(1).rowKey].set,
    ).toEqual({
      b: value("2"),
    });
    expect(changeCount(stage(state, { type: "revertRows", rows: [row(1), row(2)] }))).toBe(0);
    const discarded = stage(state, { type: "discardAll" });
    expect(changeCount(discarded)).toBe(0);
    expect(changeCount(stage(discarded, { type: "undo" }))).toBe(2);
  });
});

describe("keep mine", () => {
  const conflicted = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: "x" }, input: value("mine") },
      { type: "edit", cell: { row: row(1), column: "b", original: "y" }, input: value("theirs") },
    );

  it("checks against the server's values now", () => {
    const state = stage(conflicted(), { type: "keepMine", row: row(1), current: { a: "changed", b: "y" } });
    expect(state.updates[row(1).rowKey].original).toEqual({ a: "changed", b: "y" });
  });

  it("drops a cell the server already holds the staged value of", () => {
    const state = stage(conflicted(), { type: "keepMine", row: row(1), current: { a: "mine", b: "y" } });
    expect(state.updates[row(1).rowKey].set).toEqual({ b: value("theirs") });
  });

  it("changes nothing when nothing changed on the server", () => {
    const state = conflicted();
    expect(stage(state, { type: "keepMine", row: row(1), current: { a: "x", b: "y" } })).toBe(state);
  });
});

describe("undo and redo", () => {
  it("steps back and forward one action at a time, a multi-cell action being one step", () => {
    const one = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      {
        type: "setNull",
        cells: [
          { row: row(2), column: "a", original: 1 },
          { row: row(3), column: "a", original: 1 },
        ],
      },
    );
    const back = stage(one, { type: "undo" });
    expect(changeCount(back)).toBe(1);
    expect(changeCount(stage(back, { type: "redo" }))).toBe(3);
    expect(stage(stage(back, { type: "undo" }), { type: "undo" })).toMatchObject({ updates: {}, past: [] });
  });

  it("a new action after an undo clears the redo", () => {
    const state = stage(run({ type: "deleteRows", rows: [row(1)] }), { type: "undo" });
    const next = stage(state, { type: "deleteRows", rows: [row(2)] });
    expect(next.future).toEqual([]);
  });

  it(`keeps at most ${UNDO_LIMIT} steps`, () => {
    const actions: StageAction[] = Array.from({ length: UNDO_LIMIT + 20 }, (_, i) => ({
      type: "deleteRows",
      rows: [row(i)],
    }));
    expect(run(...actions).past).toHaveLength(UNDO_LIMIT);
  });

  it("keeps untouched changes as the same objects", () => {
    const state = run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "edit", cell: { row: row(2), column: "a", original: 1 }, input: value("2") },
    );
    const next = stage(state, { type: "edit", cell: { row: row(2), column: "a", original: 1 }, input: value("3") });
    expect(next.updates[row(1).rowKey]).toBe(state.updates[row(1).rowKey]);
  });
});

describe("apply outcomes", () => {
  const staged = () =>
    run(
      { type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") },
      { type: "addRow", id: "i:x" },
      { type: "deleteRows", rows: [row(2)] },
    );

  it("a commit empties the staging and its history", () => {
    const done = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "committed", applied: ["u:1", "i:x", "d:2"], failedChange: null, message: null },
    });
    expect(done).toMatchObject({ updates: {}, inserts: [], deletes: {}, past: [], future: [], unknownOutcome: null });
  });

  it("a rollback keeps everything and marks the failed change", () => {
    const state = staged();
    const failed = stage(state, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: "u:1", message: "Row id = 1 was changed" },
    });
    expect(failed.updates).toBe(state.updates);
    expect(failed.past).toBe(state.past);
    expect(failed.problems).toEqual({ "u:1": [{ column: null, message: "Row id = 1 was changed" }] });
  });

  it("a partial outcome removes what took effect and clears the history, so undo cannot bring it back", () => {
    const part = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "partial", applied: ["d:2"], failedChange: "u:1", message: "conflict" },
    });
    expect(changeCount(part)).toBe(2);
    expect(part.past).toEqual([]);
  });

  it("an unknown outcome is pending until fresh rows are read, then refreshed until the next apply", () => {
    const unknown = stage(staged(), {
      type: "applyOutcome",
      outcome: { outcome: "unknown", applied: [], failedChange: null, message: null },
    });
    expect(unknown.unknownOutcome).toBe("pending");
    expect(changeCount(unknown)).toBe(3);
    const refreshed = stage(unknown, { type: "refreshed" });
    expect(refreshed.unknownOutcome).toBe("refreshed");
    expect(stage(refreshed, { type: "refreshed" })).toBe(refreshed);
    const next = stage(refreshed, {
      type: "applyOutcome",
      outcome: { outcome: "rolled_back", applied: [], failedChange: null, message: null },
    });
    expect(next.unknownOutcome).toBeNull();
  });
});

describe("problems", () => {
  it("are replaced by the next preview's, and a change edited again loses its own", () => {
    const state = stage(run({ type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("x") }), {
      type: "setProblems",
      problems: { "u:1": [{ column: "n", message: "Not a number" }] },
    });
    expect(state.problems["u:1"]).toHaveLength(1);
    const edited = stage(state, { type: "edit", cell: { row: row(1), column: "n", original: 1 }, input: value("5") });
    expect(edited.problems).toEqual({});
  });
});

describe("the changes a preview gets", () => {
  it("lists each kind in the order it was staged, with an original per set column", () => {
    const state = run(
      { type: "edit", cell: { row: row(2), column: "a", original: "x" }, input: value("1") },
      { type: "edit", cell: { row: row(1), column: "a", original: "y" }, input: value("2") },
      { type: "edit", cell: { row: row(2), column: "b", original: "z" }, input: { kind: "null" } },
      { type: "addRow", id: "i:b" },
      { type: "addRow", id: "i:a" },
    );
    const changes = toChanges(state);
    expect(changes.updates?.map((u) => u.key)).toEqual([{ id: 2 }, { id: 1 }]);
    expect(changes.updates?.[0]).toEqual({
      id: "u:1",
      key: { id: 2 },
      set: { a: value("1"), b: { kind: "null" } },
      original: { a: "x", b: "z" },
    });
    expect(changes.inserts?.map((i) => i.id)).toEqual(["i:b", "i:a"]);
  });

  it("finds a change by its id", () => {
    const state = run({ type: "edit", cell: { row: row(1), column: "a", original: 1 }, input: value("2") });
    expect(findChange(state, "u:1")).toMatchObject({ kind: "update", rowKey: row(1).rowKey });
    expect(findChange(state, "u:9")).toBeNull();
  });
});
```

`src/stores/stagedEdits.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { changeCount, MAX_CHANGES, rowKeyFromKey } from "@/features/edits/staging";
import {
  dispatchStaged,
  forgetStagedEditsExcept,
  onStagedEditsChanged,
  putStagedEdits,
  readStagedEdits,
} from "./stagedEdits";

/* The staged edits by tab (spec ⑤ §3.6.1): the D-05-11 cap, the key they were staged on, the writer's notification. */

const rowRef = (id: number) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;
const rows = (from: number, count: number) => Array.from({ length: count }, (_, i) => rowRef(from + i));

afterEach(() => forgetStagedEditsExcept(new Set()));

describe("dispatching", () => {
  it("stages on the table's key and notifies the workspace writer", () => {
    const listener = vi.fn();
    const stop = onStagedEditsChanged(listener);
    expect(dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["id"])).toBe("staged");
    expect(readStagedEdits("t1")?.keyColumns).toEqual(["id"]);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(dispatchStaged("t1", { type: "restoreRows", rows: [rowRef(9)] }, ["id"])).toBe("unchanged");
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
  });

  it(`refuses an action that would stage more than ${MAX_CHANGES.toLocaleString()} changes, and keeps what was staged`, () => {
    expect(dispatchStaged("t1", { type: "deleteRows", rows: rows(0, MAX_CHANGES) }, ["id"])).toBe("staged");
    expect(dispatchStaged("t1", { type: "deleteRows", rows: rows(MAX_CHANGES, 1) }, ["id"])).toBe("too_many");
    expect(changeCount(readStagedEdits("t1")!)).toBe(MAX_CHANGES);
    // Taking changes away is always possible.
    expect(dispatchStaged("t1", { type: "restoreRows", rows: rows(0, 1) }, ["id"])).toBe("staged");
  });

  it("stages nothing new on changes made on another key, but still reverts and undoes them", () => {
    dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(1)] }, ["id"]);
    expect(dispatchStaged("t1", { type: "deleteRows", rows: [rowRef(2)] }, ["code"])).toBe("key_changed");
    expect(dispatchStaged("t1", { type: "undo" }, ["code"])).toBe("staged");
    // Nothing staged any more: the table's key is taken.
    expect(dispatchStaged("t1", { type: "addRow", id: "i:1" }, ["code"])).toBe("staged");
    expect(readStagedEdits("t1")?.keyColumns).toEqual(["code"]);
  });

  it("leaves a tab with nothing staged alone without a key (an outcome, a refresh)", () => {
    expect(dispatchStaged("t2", { type: "refreshed" })).toBe("unchanged");
    expect(readStagedEdits("t2")).toBeNull();
  });
});

describe("keeping", () => {
  it("forgets the tabs that are closed", () => {
    dispatchStaged("open", { type: "addRow", id: "i:1" }, ["id"]);
    dispatchStaged("closed", { type: "addRow", id: "i:2" }, ["id"]);
    forgetStagedEditsExcept(new Set(["open"]));
    expect(readStagedEdits("closed")).toBeNull();
    expect(readStagedEdits("open")).not.toBeNull();
    putStagedEdits("open", null);
    expect(readStagedEdits("open")).toBeNull();
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run src/features/edits src/stores`
Expected: FAIL (cannot resolve `./cells`, `./staging`, `./stagedEdits`).

- [ ] **Step 3: Write `src/features/edits/cells.ts`**

```ts
import type { EditKind } from "@/lib/api/types";
import { BYTES_PREVIEW, plainValue, type EncodedCell } from "@/lib/cells";

/*
 * Staged cell values and the loaded cells they replace (spec ⑤ R-05-8, D-05-10). A staged value is always text as typed:
 * a number's digits never pass through a JS number, so `9007199254740993` and `1.10` reach the backend as they were
 * typed. NULL and the empty string are different values; DEFAULT exists only for a new row's cell.
 */

/** A staged cell value: typed text, NULL, or DEFAULT (a new row's cell left to the column's default). */
export type CellInput = { kind: "value"; text: string } | { kind: "null" } | { kind: "default" };

export const NULL_INPUT: CellInput = { kind: "null" };

/** A loaded cell as the text its editor starts with, and whether that text is the whole value. */
export interface EditText {
  text: string;
  /** False for a cut text (over 64 KiB, a preview only) and for bytes the grid holds only the first 64 of. */
  complete: boolean;
}

/**
 * The text a cell's editor starts with (`plainValue`, ruling R-02b-7, so it is what a single-cell copy gives): NULL is
 * "", bytes are `0x` + hex, a cut text is its preview (not complete: it is never staged from), a bool is "true" /
 * "false".
 */
export function editText(cell: unknown): EditText {
  if (cell === null || cell === undefined) return { text: "", complete: true };
  if (typeof cell === "object") {
    const encoded = cell as EncodedCell;
    if (encoded.$type === "text") return { text: encoded.preview ?? "", complete: false };
    if (encoded.$type === "missing") return { text: "", complete: false };
    if (encoded.$type === "bytes") return { text: plainValue(cell), complete: (encoded.size ?? 0) <= BYTES_PREVIEW };
  }
  return { text: plainValue(cell), complete: true };
}

const asNull = (cell: unknown) => (cell === undefined ? null : cell);

/**
 * Whether two encoded cells hold the same value. A re-read row's cells are new objects, so bytes and cut texts are
 * compared by what they carry; everything else by value.
 */
export function sameCell(a: unknown, b: unknown): boolean {
  const x = asNull(a);
  const y = asNull(b);
  if (x === y) return true;
  if (x === null || y === null || typeof x !== "object" || typeof y !== "object") return false;
  const p = x as EncodedCell;
  const q = y as EncodedCell;
  return p.$type === q.$type && p.preview === q.preview && p.size === q.size && p.text === q.text;
}

/** Whether two staged values are the same. */
export function sameInput(a: CellInput, b: CellInput): boolean {
  if (a.kind !== b.kind) return false;
  return a.kind !== "value" || (b.kind === "value" && a.text === b.text);
}

/**
 * Whether staging `input` over the loaded cell `original` would change nothing: NULL over NULL, or exactly the text the
 * cell's editor starts with over a complete value (spec §3.6.1: there is no no-op UPDATE). DEFAULT never matches.
 */
export function inputMatches(input: CellInput, original: unknown): boolean {
  const cell = asNull(original);
  if (input.kind === "null") return cell === null;
  if (input.kind === "default" || cell === null) return false;
  const { text, complete } = editText(cell);
  return complete && text === input.text;
}

const TRUE_WORDS: ReadonlySet<string> = new Set(["true", "t", "1", "yes"]);
const FALSE_WORDS: ReadonlySet<string> = new Set(["false", "f", "0", "no"]);
/** Hex as the backend reads it (`literals._hex`): an optional 0x, then two digits a byte. */
const HEX = /^(?:0[xX])?((?:[0-9a-fA-F]{2})*)$/;

/**
 * A staged value as the grid draws it (D-05-10), in the encoding of a loaded cell, so copy, the aggregates, the value
 * viewer and the record view need no new cell kind: text stays a string (a number column still aligns it right), a bool
 * spelled as the backend accepts it is a boolean, hex is bytes (`{$type: "bytes", size, preview}`), NULL and DEFAULT are
 * null (the overlay marks DEFAULT). Text the preview will refuse is drawn as typed.
 */
export function displayCell(input: CellInput, kind: EditKind | undefined): unknown {
  if (input.kind !== "value") return null;
  const { text } = input;
  if (kind === "bool") {
    const word = text.trim().toLowerCase();
    if (TRUE_WORDS.has(word)) return true;
    if (FALSE_WORDS.has(word)) return false;
    return text;
  }
  if (kind === "binary") {
    const match = HEX.exec(text.trim());
    if (!match) return text;
    const hex = match[1].toLowerCase();
    return { $type: "bytes", size: hex.length / 2, preview: hex.slice(0, BYTES_PREVIEW * 2) };
  }
  return text;
}
```

- [ ] **Step 4: Write `src/features/edits/staging.ts`**

```ts
import type { EditChanges } from "@/lib/api/types";
import { inputMatches, sameCell, sameInput, type CellInput } from "./cells";

/*
 * The staged changes of one table-data tab (spec ⑤ §3.6.1, R-05-3) as one pure reducer: `stage(state, action)` returns
 * the next state and never mutates. A loaded row is known by its key as it was loaded (`RowKey`, CR-4), never by its
 * position, so sorting, filtering, Load more, Refresh and a failed commit's re-read never lose or misplace a change; a
 * key the result no longer shows keeps its change. New rows are a list of their own (they have no key yet).
 *
 * Every user action is one undo step (D-05-14): `past` keeps the stagings before it (at most UNDO_LIMIT), sharing every
 * change it did not touch, so an untouched change keeps its object (the overlay's cache, and so Q1, rely on it).
 */

/** D-05-11: changes (rows) in one commit, and so staged at once. */
export const MAX_CHANGES = 10_000;
/** D-05-14: undo steps kept (memory only). */
export const UNDO_LIMIT = 100;

/** A loaded row's key cells, as loaded, as JSON in key order. */
export type RowKey = string;
type Cells = Readonly<Record<string, unknown>>;

/** An UPDATE: the key and each set column's cell as loaded (`original`), and the values to set. `set` is never empty. */
export interface StagedUpdate {
  readonly id: string;
  readonly key: Cells;
  readonly set: Readonly<Record<string, CellInput>>;
  readonly original: Cells;
  readonly seq: number;
}
/** An INSERT: a column it does not name takes its DEFAULT. */
export interface StagedInsert {
  readonly id: string;
  readonly values: Readonly<Record<string, CellInput>>;
  readonly seq: number;
}
export interface StagedDelete {
  readonly id: string;
  readonly key: Cells;
  readonly seq: number;
}
/** Why the last preview or commit refused a change; `column` null: the change as a whole. */
export interface StagedProblem {
  readonly column: string | null;
  readonly message: string;
}

/** What an undo step restores. */
export interface Staging {
  readonly updates: Readonly<Record<RowKey, StagedUpdate>>;
  /** In the order they were added (the grid shows the newest first). */
  readonly inserts: readonly StagedInsert[];
  readonly deletes: Readonly<Record<RowKey, StagedDelete>>;
}

/**
 * The last apply's outcome was unknown (R-05-6): "pending" until fresh rows have been read (Commit is disabled until
 * then), then "refreshed" until the next apply (its preview says new rows would be inserted again). Ruling H6.
 */
export type UnknownOutcome = "pending" | "refreshed" | null;

export interface StagedEdits extends Staging {
  /** The key the changes were staged on (the edit target's when the first one was). */
  readonly keyColumns: readonly string[];
  /** By change id. */
  readonly problems: Readonly<Record<string, readonly StagedProblem[]>>;
  readonly unknownOutcome: UnknownOutcome;
  /** The next change's `seq`: within each kind, changes are sent in the order they were staged (D-05-3). */
  readonly nextSeq: number;
  readonly past: readonly Staging[];
  readonly future: readonly Staging[];
}

export type LoadedRowRef = { readonly kind: "row"; readonly rowKey: RowKey; readonly key: Cells };
export type NewRowRef = { readonly kind: "insert"; readonly id: string };
/** A row of the grid as staging knows it. */
export type RowRef = LoadedRowRef | NewRowRef;

/** One cell; `original` is a loaded row's cell as loaded (what an UPDATE checks), unused for a new row. */
export interface CellTarget {
  readonly row: RowRef;
  readonly column: string;
  readonly original?: unknown;
}

/** An apply's outcome as staging takes it (from `edit_outcome` and the request's error). */
export interface AppliedOutcome {
  readonly outcome: "committed" | "rolled_back" | "partial" | "unknown";
  /** The change ids that took effect. */
  readonly applied: readonly string[];
  readonly failedChange: string | null;
  /** The failed change's message (the server's text), shown on its row. */
  readonly message: string | null;
}

export type StageAction =
  | { type: "edit"; cell: CellTarget; input: CellInput }
  | { type: "setNull"; cells: readonly CellTarget[] }
  | { type: "useDefault"; cells: readonly CellTarget[] }
  | { type: "revertCells"; cells: readonly CellTarget[] }
  | { type: "revertRows"; rows: readonly RowRef[] }
  /** Marks the rows for deletion (a new row goes at once); rows that are all marked already are restored. */
  | { type: "deleteRows"; rows: readonly RowRef[] }
  | { type: "restoreRows"; rows: readonly RowRef[] }
  | { type: "addRow"; id: string }
  | { type: "discardAll" }
  /** The row's set columns are checked against `current` (the server's cells now) instead of what was loaded. */
  | { type: "keepMine"; row: LoadedRowRef; current: Cells }
  | { type: "setProblems"; problems: Readonly<Record<string, readonly StagedProblem[]>> }
  | { type: "applyOutcome"; outcome: AppliedOutcome }
  /** Fresh rows were read: an unknown outcome is "refreshed" now. */
  | { type: "refreshed" }
  | { type: "undo" }
  | { type: "redo" };

/** Makes a change id: "u:", "i:" or "d:" and a random part (the backend echoes it, never reads it). */
export type NewId = (prefix: "u" | "i" | "d") => string;
export const newChangeId: NewId = (prefix) => `${prefix}:${crypto.randomUUID()}`;

/** The RowKey of a key given by column (a staged change's, or one read back from `workspace.json`). */
export function rowKeyFromKey(key: Cells, keyColumns: readonly string[]): RowKey {
  return JSON.stringify(keyColumns.map((column) => key[column] ?? null));
}

export function emptyStaged(keyColumns: readonly string[]): StagedEdits {
  return {
    keyColumns,
    updates: {},
    inserts: [],
    deletes: {},
    problems: {},
    unknownOutcome: null,
    nextSeq: 0,
    past: [],
    future: [],
  };
}

/** The changes a commit sends: an UPDATE hidden under its row's DELETE is not one (05a carry: only the DELETE is sent). */
export function changeCount(staged: Staging): number {
  let updates = 0;
  for (const rowKey in staged.updates) if (!(rowKey in staged.deletes)) updates += 1;
  return updates + staged.inserts.length + Object.keys(staged.deletes).length;
}

export function hasChanges(staged: Staging): boolean {
  return staged.inserts.length > 0 || Object.keys(staged.updates).length > 0 || Object.keys(staged.deletes).length > 0;
}

const bySeq = <T extends { seq: number }>(items: T[]): T[] => items.sort((a, b) => a.seq - b.seq);

function pick(cells: Cells, columns: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(columns.map((column) => [column, cells[column] ?? null]));
}

/**
 * What `POST /edits/preview` takes (spec §3.3.1), each kind in staging order. An UPDATE hidden under its row's DELETE
 * is left out: the DELETE alone is sent (05a carry). Every UPDATE carries an original per set column and never a
 * DEFAULT (staging never sets one on a loaded row).
 */
export function toChanges(staged: Staging): EditChanges {
  const updates = bySeq(
    Object.entries(staged.updates)
      .filter(([rowKey]) => !(rowKey in staged.deletes))
      .map(([, u]) => u),
  );
  return {
    updates: updates.map((u) => ({
      id: u.id,
      key: { ...u.key },
      set: { ...u.set },
      original: pick(u.original, Object.keys(u.set)),
    })),
    inserts: bySeq([...staged.inserts]).map((i) => ({ id: i.id, values: { ...i.values } })),
    deletes: bySeq(Object.values(staged.deletes)).map((d) => ({ id: d.id, key: { ...d.key } })),
  };
}

/** The staged change with this id and the row it is on (null for a new row), or null. */
export function findChange(
  staged: Staging,
  id: string,
):
  | { kind: "update"; rowKey: RowKey; change: StagedUpdate }
  | { kind: "delete"; rowKey: RowKey; change: StagedDelete }
  | { kind: "insert"; rowKey: null; change: StagedInsert }
  | null {
  for (const [rowKey, change] of Object.entries(staged.updates))
    if (change.id === id) return { kind: "update", rowKey, change };
  for (const [rowKey, change] of Object.entries(staged.deletes))
    if (change.id === id) return { kind: "delete", rowKey, change };
  const insert = staged.inserts.find((change) => change.id === id);
  return insert ? { kind: "insert", rowKey: null, change: insert } : null;
}

const without = <T>(record: Readonly<Record<string, T>>, key: string): Record<string, T> => {
  const copy = { ...record };
  delete copy[key];
  return copy;
};

/** One action's work on copies of the maps (entries are shared until replaced); `edited`: changes whose problems go. */
interface Work {
  updates: Record<RowKey, StagedUpdate>;
  inserts: StagedInsert[];
  deletes: Record<RowKey, StagedDelete>;
  seq: number;
  changed: boolean;
  edited: Set<string>;
  newId: NewId;
}

function begin(state: StagedEdits, newId: NewId): Work {
  return {
    updates: { ...state.updates },
    inserts: [...state.inserts],
    deletes: { ...state.deletes },
    seq: state.nextSeq,
    changed: false,
    edited: new Set(),
    newId,
  };
}

const stagingOf = (state: Staging): Staging => ({
  updates: state.updates,
  inserts: state.inserts,
  deletes: state.deletes,
});

/** The state after `work` as one undo step; the state itself when nothing changed. */
function finish(state: StagedEdits, work: Work): StagedEdits {
  if (!work.changed) return state;
  let problems = state.problems;
  for (const id of work.edited) if (id in problems) problems = without(problems, id);
  return {
    ...state,
    updates: work.updates,
    inserts: work.inserts,
    deletes: work.deletes,
    nextSeq: work.seq,
    problems,
    past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
    future: [],
  };
}

function setUpdate(work: Work, rowKey: RowKey, update: StagedUpdate | null, id: string): void {
  if (update && Object.keys(update.set).length > 0) work.updates[rowKey] = update;
  else delete work.updates[rowKey];
  work.changed = true;
  work.edited.add(id);
}

function editLoaded(work: Work, row: LoadedRowRef, column: string, input: CellInput, loaded: unknown): void {
  if (row.rowKey in work.deletes) return; // a row marked for deletion is not edited (restore it first)
  const existing = work.updates[row.rowKey];
  const staged = existing && column in existing.set;
  const original = staged ? existing.original[column] : loaded;
  if (inputMatches(input, original)) {
    // Back to the value it was loaded with: no change to the cell (there is no no-op UPDATE).
    if (!staged) return;
    const rest = { ...existing, set: without(existing.set, column), original: without(existing.original, column) };
    setUpdate(work, row.rowKey, rest, existing.id);
    return;
  }
  if (staged && sameInput(existing.set[column], input)) return;
  const id = existing?.id ?? work.newId("u");
  const update: StagedUpdate = {
    id,
    key: existing?.key ?? row.key,
    set: { ...existing?.set, [column]: input },
    original: { ...existing?.original, [column]: original === undefined ? null : original },
    seq: existing?.seq ?? work.seq++,
  };
  setUpdate(work, row.rowKey, update, id);
}

function editInsert(work: Work, id: string, column: string, input: CellInput): void {
  const index = work.inserts.findIndex((insert) => insert.id === id);
  if (index < 0) return;
  const insert = work.inserts[index];
  const current = insert.values[column];
  if (input.kind === "default" ? current === undefined : current !== undefined && sameInput(current, input)) return;
  const values = input.kind === "default" ? without(insert.values, column) : { ...insert.values, [column]: input };
  work.inserts[index] = { ...insert, values };
  work.changed = true;
  work.edited.add(id);
}

function editCell(work: Work, cell: CellTarget, input: CellInput): void {
  if (cell.row.kind === "insert") editInsert(work, cell.row.id, cell.column, input);
  else if (input.kind !== "default") editLoaded(work, cell.row, cell.column, input, cell.original);
}

function revertCell(work: Work, cell: CellTarget): void {
  if (cell.row.kind === "insert") {
    editInsert(work, cell.row.id, cell.column, { kind: "default" });
    return;
  }
  const existing = work.updates[cell.row.rowKey];
  if (!existing || !(cell.column in existing.set)) return;
  const rest = {
    ...existing,
    set: without(existing.set, cell.column),
    original: without(existing.original, cell.column),
  };
  setUpdate(work, cell.row.rowKey, rest, existing.id);
}

function removeInsert(work: Work, id: string): void {
  const index = work.inserts.findIndex((insert) => insert.id === id);
  if (index < 0) return;
  work.inserts.splice(index, 1);
  work.changed = true;
}

function revertRow(work: Work, row: RowRef): void {
  if (row.kind === "insert") {
    removeInsert(work, row.id); // a new row reverts to not existing (ruling H20)
    return;
  }
  const update = work.updates[row.rowKey];
  const remove = work.deletes[row.rowKey];
  if (update) setUpdate(work, row.rowKey, null, update.id);
  if (remove) {
    delete work.deletes[row.rowKey];
    work.changed = true;
    work.edited.add(remove.id);
  }
}

function deleteRows(work: Work, rows: readonly RowRef[]): void {
  const loaded = rows.filter((row): row is LoadedRowRef => row.kind === "row");
  const restore =
    loaded.length > 0 && loaded.length === rows.length && loaded.every((row) => row.rowKey in work.deletes);
  if (restore) {
    restoreRows(work, loaded);
    return;
  }
  for (const row of rows) {
    if (row.kind === "insert") removeInsert(work, row.id);
    else if (!(row.rowKey in work.deletes)) {
      work.deletes[row.rowKey] = { id: work.newId("d"), key: row.key, seq: work.seq++ };
      work.changed = true;
    }
  }
}

function restoreRows(work: Work, rows: readonly RowRef[]): void {
  for (const row of rows) {
    if (row.kind !== "row" || !(row.rowKey in work.deletes)) continue;
    work.edited.add(work.deletes[row.rowKey].id);
    delete work.deletes[row.rowKey]; // its hidden UPDATE, if any, shows again
    work.changed = true;
  }
}

function keepMine(work: Work, row: LoadedRowRef, current: Cells): void {
  const existing = work.updates[row.rowKey];
  if (!existing) return;
  const set = { ...existing.set };
  const original = { ...existing.original };
  let moved = false;
  for (const column of Object.keys(existing.set)) {
    if (!(column in current) || sameCell(current[column], existing.original[column])) continue;
    moved = true;
    if (inputMatches(existing.set[column], current[column])) {
      delete set[column]; // the server holds the staged value now: nothing left to change
      delete original[column];
    } else original[column] = current[column] ?? null;
  }
  if (moved) setUpdate(work, row.rowKey, { ...existing, set, original }, existing.id);
}

function applyOutcome(state: StagedEdits, outcome: AppliedOutcome): StagedEdits {
  const applied = new Set(outcome.applied);
  const updates = { ...state.updates };
  const deletes = { ...state.deletes };
  let removed = false;
  for (const [rowKey, update] of Object.entries(state.updates)) {
    if (applied.has(update.id)) {
      delete updates[rowKey];
      removed = true;
    }
  }
  for (const [rowKey, remove] of Object.entries(state.deletes)) {
    if (applied.has(remove.id)) {
      delete deletes[rowKey];
      delete updates[rowKey]; // the row is gone: the UPDATE hidden under its DELETE (never sent) goes with it
      removed = true;
    }
  }
  const inserts = state.inserts.filter((insert) => !applied.has(insert.id));
  removed ||= inserts.length !== state.inserts.length;
  const { failedChange, message } = outcome;
  // Removed changes must never come back through undo (they took effect), and a commit empties the history (D-05-14).
  const clearHistory = removed || outcome.outcome === "committed";
  return {
    ...state,
    ...(removed ? { updates, inserts, deletes } : {}),
    problems: failedChange && message ? { [failedChange]: [{ column: null, message }] } : {},
    unknownOutcome: outcome.outcome === "unknown" ? "pending" : null,
    past: clearHistory ? [] : state.past,
    future: clearHistory ? [] : state.future,
  };
}

/** The next state; `state` itself when the action changes nothing (a dispatcher then stores nothing). */
export function stage(state: StagedEdits, action: StageAction, newId: NewId = newChangeId): StagedEdits {
  switch (action.type) {
    case "undo": {
      const previous = state.past.at(-1);
      if (!previous) return state;
      return { ...state, ...previous, past: state.past.slice(0, -1), future: [stagingOf(state), ...state.future] };
    }
    case "redo": {
      const next = state.future[0];
      if (!next) return state;
      return {
        ...state,
        ...next,
        past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
        future: state.future.slice(1),
      };
    }
    case "setProblems":
      return { ...state, problems: action.problems };
    case "applyOutcome":
      return applyOutcome(state, action.outcome);
    case "refreshed":
      return state.unknownOutcome === "pending" ? { ...state, unknownOutcome: "refreshed" } : state;
    case "discardAll": {
      if (!hasChanges(state)) return state;
      return {
        ...state,
        updates: {},
        inserts: [],
        deletes: {},
        problems: {},
        past: [...state.past, stagingOf(state)].slice(-UNDO_LIMIT),
        future: [],
      };
    }
    default:
      break;
  }
  const work = begin(state, newId);
  switch (action.type) {
    case "edit":
      editCell(work, action.cell, action.input);
      break;
    case "setNull":
      for (const cell of action.cells) editCell(work, cell, { kind: "null" });
      break;
    case "useDefault":
      for (const cell of action.cells)
        if (cell.row.kind === "insert") editInsert(work, cell.row.id, cell.column, { kind: "default" });
      break;
    case "revertCells":
      for (const cell of action.cells) revertCell(work, cell);
      break;
    case "revertRows":
      for (const row of action.rows) revertRow(work, row);
      break;
    case "deleteRows":
      deleteRows(work, action.rows);
      break;
    case "restoreRows":
      restoreRows(work, action.rows);
      break;
    case "addRow":
      work.inserts.push({ id: action.id, values: {}, seq: work.seq++ });
      work.changed = true;
      break;
    case "keepMine":
      keepMine(work, action.row, action.current);
      break;
  }
  return finish(state, work);
}
```

- [ ] **Step 5: Write `src/stores/stagedEdits.ts`**

```ts
import { create } from "zustand";
import {
  changeCount,
  emptyStaged,
  MAX_CHANGES,
  stage,
  type StageAction,
  type StagedEdits,
} from "@/features/edits/staging";

/*
 * The table-data tabs' staged grid edits, by tab id (spec ⑤ §3.6.1). Outside React, like `editorBuffers`: the one
 * workspace writer and the close guard read them without waiting for a tab to mount, and a tab that is not shown still
 * has its restored changes. Every change notifies the writer (`onStagedEditsChanged`).
 */

const useStore = create<Readonly<Record<string, StagedEdits>>>(() => ({}));
const listeners = new Set<() => void>();

/** What a dispatch did: "too_many" and "key_changed" changed nothing and the caller says why. */
export type DispatchResult = "staged" | "unchanged" | "too_many" | "key_changed";

/** The actions that stage something new: refused while the changes are on another key than the table's (ruling H13). */
const ADDING: ReadonlySet<StageAction["type"]> = new Set([
  "edit",
  "setNull",
  "useDefault",
  "deleteRows",
  "addRow",
  "keepMine",
]);

const sameColumns = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((column, i) => column === b[i]);

export function useStagedEdits(tabId: string): StagedEdits | null {
  return useStore((all) => all[tabId] ?? null);
}

export function readStagedEdits(tabId: string): StagedEdits | null {
  return useStore.getState()[tabId] ?? null;
}

/** Puts a tab's staged edits (a restored file's), or drops them (null). */
export function putStagedEdits(tabId: string, staged: StagedEdits | null): void {
  const current = useStore.getState();
  if ((current[tabId] ?? null) === staged) return;
  const next = { ...current };
  if (staged) next[tabId] = staged;
  else delete next[tabId];
  useStore.setState(next, true);
  for (const listener of listeners) listener();
}

/**
 * Applies `action` to the tab's staged edits. `keyColumns` is the table's key now (its edit target's): a tab with
 * nothing staged takes it; changes staged on another key are kept (they can be committed or discarded) but nothing new
 * is staged on them (`key_changed`). An action that would leave more than MAX_CHANGES changes is refused (`too_many`,
 * D-05-11). Without `keyColumns` (an outcome, a refresh), a tab with nothing staged is left alone.
 */
export function dispatchStaged(tabId: string, action: StageAction, keyColumns?: readonly string[]): DispatchResult {
  const current = readStagedEdits(tabId);
  if (!current && !keyColumns) return "unchanged";
  let base = current ?? emptyStaged(keyColumns ?? []);
  if (keyColumns && !sameColumns(base.keyColumns, keyColumns)) {
    if (changeCount(base) > 0) {
      if (ADDING.has(action.type)) return "key_changed";
    } else base = { ...base, keyColumns };
  }
  const next = stage(base, action);
  if (next === base) return "unchanged";
  const count = changeCount(next);
  if (count > MAX_CHANGES && count > changeCount(base)) return "too_many";
  putStagedEdits(tabId, next);
  return "staged";
}

/** Drops what is kept for tabs that are no longer open (the workspace writer calls it as it saves). */
export function forgetStagedEditsExcept(openTabIds: ReadonlySet<string>): void {
  const current = useStore.getState();
  const gone = Object.keys(current).filter((tabId) => !openTabIds.has(tabId));
  for (const tabId of gone) putStagedEdits(tabId, null);
}

export function onStagedEditsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Dev only: like `editorBuffers`, a hot update would hand the writer an empty copy, so the page reloads instead.
if (import.meta.hot) {
  import.meta.hot.accept();
  import.meta.hot.dispose(() => window.location.reload());
}
```

- [ ] **Step 6: Run the tests, the types and the build**

Run: `npx vitest run src/features/edits src/stores && npm run typecheck && npm run build`
Expected: PASS (47 tests); no errors.

- [ ] **Step 7: Commit**

```bash
git add src/features/edits/cells.ts src/features/edits/cells.test.ts src/features/edits/staging.ts \
  src/features/edits/staging.test.ts src/stores/stagedEdits.ts src/stores/stagedEdits.test.ts
git commit -m "feat(app): grid-edit staging — one reducer keyed by the loaded key, undo/redo, the DELETE alone for a deleted row; the staged-edits store"
```

---

### Task 3: The overlay — staged changes over the loaded rows, by key

**Suggested implementer:** Opus.

**Files:**
- Create: `src/features/grid/marks.ts` (the mark types; Task 7 adds their classes), `src/features/edits/overlay.ts`
- Test: `src/features/edits/overlay.test.ts`

**Interfaces:**
- Consumes: Task 2 (`hasChanges`, `RowKey`, `RowRef`, `StagedEdits`, `StagedInsert`, `StagedUpdate`, `StagedDelete`, `StagedProblem`, `rowKeyFromKey`; `displayCell`, `sameCell`); `describeCell` (`@/lib/cells`); `ColumnMeta`, `ColumnCategory`, `EditKind`.
- Produces:
  - `grid/marks.ts`: `CellMark { changed: boolean; was?; isDefault?; problem?; conflict? }`, `RowMark { kind: "new" | "changed" | "deleted"; problem?; cells?: Record<number, CellMark> }`, `HeaderMark { key: boolean; readOnly: string | null }`.
  - `overlay.ts`: `rowKeyOf(row, keyIndexes): RowKey`; `ResultColumns { byName; keyColumns; keyIndexes: number[] | null }`, `resultColumns(columns, keyColumns)`; `Binding { rows; keyIndexes; byKey: Map<RowKey, number>; keyed }`, `bindRows(rows, keyIndexes, previous: Binding | null): Binding`; `Overlay { rows: unknown[][]; marks: (RowMark | undefined)[]; leading: number; notShown: number }`, `OverlayInput { rows; columns; kinds; staged; binding; result }`, `OverlayCache`, `overlay(input, cache): { overlay; cache }`; `rowRefAt(display, staged, rows, index, result): RowRef | null`; `displayIndexOf(display, staged, binding, ref): number`; `wasText(cell, category): string` ("Was: …").

Behaviour (spec §3.6.2, R-05-3, D-05-10, Q1; ruling H3):
- Nothing staged: the overlay's `rows` is the result's array itself (the grid behaves exactly as before).
- New rows come first, the newest at the top (H3); their unset cells are null with `isDefault`; a staged value is drawn with `displayCell`.
- A loaded row with an UPDATE becomes a new array (`row.slice()`, cells shared) with the staged values; its cells are marked `changed`, `was` ("Was: <value>", "(empty string)", "NULL"), a `problem` of that column, and a `conflict` ("Changed on the server: now <value>") when the loaded cell is no longer the one the change was staged on (`sameCell`, after a re-read). A row marked for deletion shows its loaded values (the UPDATE under it hidden) with kind "deleted". Problems on the change as a whole (column null) or on a column not in the result go on the row.
- Rows with no change keep their objects; a changed row whose source row, change, problems and columns are the same objects as last time reuses its display row and mark (`OverlayCache`): staging one cell gives one new row object.
- `bindRows` keys rows by `rowKeyOf`; given the previous binding of the same result grown by a page (pages are only appended, so the first and the old last row are the same objects), it keys only the new rows and extends the old map in place. A staged key that the binding does not have counts in `notShown`.

- [ ] **Step 1: Write the failing test** `src/features/edits/overlay.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { ColumnMeta, EditKind } from "@/lib/api/types";
import {
  bindRows,
  displayIndexOf,
  overlay,
  resultColumns,
  rowKeyOf,
  rowRefAt,
  type OverlayCache,
  type OverlayInput,
} from "./overlay";
import { emptyStaged, rowKeyFromKey, stage, type NewId, type StageAction, type StagedEdits } from "./staging";

/*
 * The overlay (spec ⑤ §3.6.2): changes found by key whatever the rows' order or page, untouched rows kept as the same
 * objects (Q1), new rows on top, marks for changed, new, deleted, DEFAULT, problems and conflicts.
 */

const COLUMNS: ColumnMeta[] = [
  { name: "id", type_name: "bigint", category: "number" },
  { name: "label", type_name: "text", category: "text" },
  { name: "flag", type_name: "boolean", category: "bool" },
];
const KINDS = new Map<string, EditKind>([
  ["id", "number"],
  ["label", "text"],
  ["flag", "bool"],
]);
const RESULT = resultColumns(COLUMNS, ["id"]);

const makeRows = (from: number, count: number): unknown[][] =>
  Array.from({ length: count }, (_, i) => [from + i, `row ${from + i}`, false]);
const ref = (id: number) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;

function staged(...actions: StageAction[]): StagedEdits {
  let n = 0;
  const newId: NewId = (prefix) => `${prefix}:${++n}`;
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

const editLabel = (id: number, text: string, original = `row ${id}`): StageAction => ({
  type: "edit",
  cell: { row: ref(id), column: "label", original },
  input: { kind: "value", text },
});

function draw(rows: unknown[][], state: StagedEdits | null, cache: OverlayCache | null = null) {
  const binding = bindRows(rows, RESULT.keyIndexes!, null);
  const input: OverlayInput = { rows, columns: COLUMNS, kinds: KINDS, staged: state, binding, result: RESULT };
  return overlay(input, cache);
}

describe("binding rows to keys", () => {
  it("keys a row as its staged change names it", () => {
    expect(rowKeyOf([7, "x", true], [0])).toBe(rowKeyFromKey({ id: 7 }, ["id"]));
    expect(resultColumns(COLUMNS, ["missing"]).keyIndexes).toBeNull();
  });

  it("keys only the new page after Load more, and starts over for another result", () => {
    const first = makeRows(1, 500);
    const binding = bindRows(first, [0], null);
    expect(binding.keyed).toBe(500);
    const grown = first.concat(makeRows(501, 500)); // what the session store does with a page: the same row objects
    const next = bindRows(grown, [0], binding);
    expect(next.keyed).toBe(500);
    expect(next.byKey).toBe(binding.byKey);
    expect(next.byKey.get(rowKeyFromKey({ id: 900 }, ["id"]))).toBe(899);
    expect(bindRows(grown, [0], next)).toBe(next);
    const rerun = makeRows(1, 1000); // a refresh: equal values, new objects
    expect(bindRows(rerun, [0], next).keyed).toBe(1000);
  });
});

describe("the overlay", () => {
  it("is the rows themselves when nothing is staged", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, null);
    expect(shown.rows).toBe(rows);
    expect(shown).toMatchObject({ leading: 0, notShown: 0 });
  });

  it("replaces only the changed row, keeping every other row object", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, staged(editLabel(2, "two")));
    expect(shown.rows[0]).toBe(rows[0]);
    expect(shown.rows[2]).toBe(rows[2]);
    expect(shown.rows[1]).toEqual([2, "two", false]);
    expect(rows[1]).toEqual([2, "row 2", false]); // the result is never written
    expect(shown.marks[0]).toBeUndefined();
    expect(shown.marks[1]).toEqual({
      kind: "changed",
      problem: undefined,
      cells: { 1: { changed: true, was: "Was: row 2", problem: undefined, conflict: undefined } },
    });
  });

  it("reuses a changed row's object while neither it nor its change changes", () => {
    const rows = makeRows(1, 3);
    const one = staged(editLabel(2, "two"));
    const first = draw(rows, one);
    const two = stage(one, editLabel(3, "three"));
    const second = draw(rows, two, first.cache);
    expect(second.overlay.rows[1]).toBe(first.overlay.rows[1]);
    expect(second.overlay.marks[1]).toBe(first.overlay.marks[1]);
    expect(second.overlay.rows[2]).toEqual([3, "three", false]);
  });

  it("finds a change after Load more, a sort and a refresh, and counts it while it is not shown", () => {
    const state = staged(editLabel(700, "seven hundred"));
    const firstPage = makeRows(1, 500);
    expect(draw(firstPage, state).overlay.notShown).toBe(1);
    const both = firstPage.concat(makeRows(501, 500));
    expect(draw(both, state).overlay.rows[699]).toEqual([700, "seven hundred", false]);
    const sorted = makeRows(1, 1000).reverse();
    const { overlay: resorted } = draw(sorted, state);
    expect(resorted.rows[300]).toEqual([700, "seven hundred", false]);
    expect(resorted.notShown).toBe(0);
  });

  it("marks a conflict once the server's value is not the one the change was staged on", () => {
    const state = staged(editLabel(2, "mine"));
    const reread = makeRows(1, 3);
    reread[1] = [2, "theirs", false];
    const cell = draw(reread, state).overlay.marks[1]?.cells?.[1];
    expect(cell).toMatchObject({ changed: true, was: "Was: row 2", conflict: "Changed on the server: now theirs" });
  });

  it("draws a deleted row's loaded values, its UPDATE hidden", () => {
    const rows = makeRows(1, 3);
    const { overlay: shown } = draw(rows, staged(editLabel(2, "two"), { type: "deleteRows", rows: [ref(2)] }));
    expect(shown.rows[1]).toBe(rows[1]);
    expect(shown.marks[1]).toEqual({ kind: "deleted", problem: undefined });
  });

  it("puts new rows on top, newest first, their unset cells marked DEFAULT", () => {
    const rows = makeRows(1, 2);
    const state = staged(
      { type: "addRow", id: "i:old" },
      { type: "addRow", id: "i:new" },
      {
        type: "edit",
        cell: { row: { kind: "insert", id: "i:new" }, column: "flag" },
        input: { kind: "value", text: "yes" },
      },
    );
    const { overlay: shown } = draw(rows, state);
    expect(shown.leading).toBe(2);
    expect(shown.rows.slice(0, 2)).toEqual([
      [null, null, true],
      [null, null, null],
    ]);
    expect(shown.rows[2]).toBe(rows[0]);
    expect(shown.marks[0]).toEqual({
      kind: "new",
      problem: undefined,
      cells: {
        0: { changed: false, isDefault: true, problem: undefined },
        1: { changed: false, isDefault: true, problem: undefined },
      },
    });
    expect(rowRefAt(shown, state, rows, 0, RESULT)).toEqual({ kind: "insert", id: "i:new" });
    expect(rowRefAt(shown, state, rows, 2, RESULT)).toEqual(ref(1));
    expect(displayIndexOf(shown, state, bindRows(rows, [0], null), ref(2))).toBe(3);
    expect(displayIndexOf(shown, state, null, { kind: "insert", id: "i:old" })).toBe(1);
  });

  it("puts a problem on its cell, or on the row when it is about the change as a whole", () => {
    const state = stage(staged(editLabel(1, "x")), {
      type: "setProblems",
      problems: {
        "u:1": [
          { column: "label", message: "Too long" },
          { column: null, message: "Row id = 1 was changed or deleted" },
        ],
      },
    });
    const mark = draw(makeRows(1, 1), state).overlay.marks[0];
    expect(mark?.problem).toBe("Row id = 1 was changed or deleted");
    expect(mark?.cells?.[1].problem).toBe("Too long");
  });

  it("keeps a staged number's digits as text", () => {
    const state = staged({
      type: "edit",
      cell: { row: ref(1), column: "label", original: "row 1" },
      input: { kind: "value", text: "9007199254740993" },
    });
    expect(draw(makeRows(1, 1), state).overlay.rows[0][1]).toBe("9007199254740993");
  });

  it("names an empty original and a NULL one in its tooltip", () => {
    const empty = draw([[1, "", false]], staged(editLabel(1, "x", ""))).overlay.marks[0]?.cells?.[1];
    expect(empty?.was).toBe("Was: (empty string)");
    const none = draw([[1, null, false]], staged(editLabel(1, "x", null as unknown as string))).overlay.marks[0]
      ?.cells?.[1];
    expect(none?.was).toBe("Was: NULL");
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `npx vitest run src/features/edits/overlay.test.ts`
Expected: FAIL (cannot resolve `./overlay`).

- [ ] **Step 3: Write `src/features/grid/marks.ts`** (types only; Task 7 adds the classes)

```ts
/*
 * How the grid marks staged changes (spec ⑤ §3.6.2). The overlay (`features/edits/overlay.ts`) makes the marks; the
 * grid only draws them. A mark object is kept as long as what it marks does not change, so the rows it is passed to
 * stay memoized (Q1).
 */

/** One cell of a row with staged changes. */
export interface CellMark {
  /** The cell holds a staged value (a loaded row's UPDATE). */
  readonly changed: boolean;
  /** "Was: …", the value it was loaded with (its tooltip). */
  readonly was?: string;
  /** A new row's cell left to its default: drawn as DEFAULT, not NULL. */
  readonly isDefault?: boolean;
  /** Why the last preview or commit refused it. */
  readonly problem?: string;
  /** "Changed on the server: now …": the server's value differs from the one the change was staged on. */
  readonly conflict?: string;
}

/** A new row, a loaded row with staged values, or a loaded row marked for deletion. */
export interface RowMark {
  readonly kind: "new" | "changed" | "deleted";
  /** Why the last preview or commit refused the row's change as a whole. */
  readonly problem?: string;
  /** By column index. */
  readonly cells?: Readonly<Record<number, CellMark>>;
}

/** A column header of an editable table: a key column, or one that cannot be written (and why). */
export interface HeaderMark {
  readonly key: boolean;
  readonly readOnly: string | null;
}
```

- [ ] **Step 4: Write `src/features/edits/overlay.ts`**

```ts
import type { ColumnCategory, ColumnMeta, EditKind } from "@/lib/api/types";
import { describeCell } from "@/lib/cells";
import type { CellMark, RowMark } from "../grid/marks";
import { displayCell, sameCell } from "./cells";
import {
  hasChanges,
  type RowKey,
  type RowRef,
  type StagedDelete,
  type StagedEdits,
  type StagedInsert,
  type StagedProblem,
  type StagedUpdate,
} from "./staging";

/*
 * The staged changes laid over the loaded rows for the grid (spec ⑤ §3.6.2, R-05-3), as pure functions. The result is
 * never copied: the display rows are a new array of row references in which only a changed row is a new array (its
 * cells otherwise shared), every other row is the loaded row object itself, so the grid's memoized rows skip it (Q1).
 * An overlay also reuses, from the last one (`OverlayCache`), each changed row whose source row, change and problems are
 * the same objects: staging one cell re-renders that row alone.
 *
 * Binding (`bindRows`) maps each loaded row's key to its index, only while something is staged on loaded rows; Load more
 * appends a page, and the binding then keys only the new page.
 */

/** A loaded row's key cells as JSON in key order: the same text as `rowKeyFromKey` gives for its staged change. */
export function rowKeyOf(row: readonly unknown[], keyIndexes: readonly number[]): RowKey {
  return JSON.stringify(keyIndexes.map((index) => row[index] ?? null));
}

/** Where the result's columns are, by name, and its key columns' indexes (null when one is not in the result). */
export interface ResultColumns {
  readonly byName: ReadonlyMap<string, number>;
  readonly keyColumns: readonly string[];
  readonly keyIndexes: readonly number[] | null;
}

export function resultColumns(columns: readonly ColumnMeta[], keyColumns: readonly string[]): ResultColumns {
  const byName = new Map(columns.map((column, index) => [column.name, index]));
  const indexes = keyColumns.map((column) => byName.get(column));
  const keyIndexes =
    keyColumns.length > 0 && indexes.every((index) => index !== undefined) ? (indexes as number[]) : null;
  return { byName, keyColumns, keyIndexes };
}

export interface Binding {
  readonly rows: readonly (readonly unknown[])[];
  readonly keyIndexes: readonly number[];
  /** Each key's row index (the first, if a key repeats). Extended in place when the same result grows. */
  readonly byKey: Map<RowKey, number>;
  /** How many rows this binding keyed: all of a new result, only the new page after Load more. */
  readonly keyed: number;
}

const sameIndexes = (a: readonly number[], b: readonly number[]) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

/**
 * The binding of `rows`. When they are `previous`'s rows with a page appended (the same row objects first: pages are
 * only appended), only the new rows are keyed and `previous`'s map is extended in place (it must not be used again).
 */
export function bindRows(
  rows: readonly (readonly unknown[])[],
  keyIndexes: readonly number[],
  previous: Binding | null,
): Binding {
  const old = previous?.rows;
  const grows =
    !!previous &&
    !!old &&
    old.length > 0 &&
    old.length <= rows.length &&
    sameIndexes(previous.keyIndexes, keyIndexes) &&
    rows[0] === old[0] &&
    rows[old.length - 1] === old[old.length - 1];
  if (grows && old.length === rows.length) return previous;
  const byKey = grows ? previous.byKey : new Map<RowKey, number>();
  const start = grows ? old.length : 0;
  for (let index = start; index < rows.length; index += 1) {
    const key = rowKeyOf(rows[index], keyIndexes);
    if (!byKey.has(key)) byKey.set(key, index);
  }
  return { rows, keyIndexes, byKey, keyed: rows.length - start };
}

/** What the grid draws: new rows first (the newest at the top), then the loaded rows with their changes. */
export interface Overlay {
  readonly rows: unknown[][];
  /** By display index; sparse (undefined: an untouched row). */
  readonly marks: readonly (RowMark | undefined)[];
  /** New rows at the top: the loaded rows' numbers count from after them. */
  readonly leading: number;
  /** Staged changes of loaded rows the rows shown do not have (filtered out, not loaded yet, or deleted meanwhile). */
  readonly notShown: number;
}

const NO_MARKS: readonly (RowMark | undefined)[] = [];

export interface OverlayInput {
  readonly rows: unknown[][];
  readonly columns: readonly ColumnMeta[];
  /** The edit target's kinds by column name (how a staged text is drawn: bool, hex); empty until it is known. */
  readonly kinds: ReadonlyMap<string, EditKind>;
  readonly staged: StagedEdits | null;
  /** The binding of `rows`, or null (nothing staged on loaded rows, or the key is not in the result). */
  readonly binding: Binding | null;
  readonly result: ResultColumns;
}

interface LoadedEntry {
  readonly source: readonly unknown[];
  readonly update: StagedUpdate | undefined;
  readonly remove: StagedDelete | undefined;
  readonly problems: readonly StagedProblem[] | undefined;
  readonly columns: readonly ColumnMeta[];
  readonly row: unknown[];
  readonly mark: RowMark;
}

interface InsertEntry {
  readonly insert: StagedInsert;
  readonly problems: readonly StagedProblem[] | undefined;
  readonly columns: readonly ColumnMeta[];
  readonly row: unknown[];
  readonly mark: RowMark;
}

export interface OverlayCache {
  readonly loaded: ReadonlyMap<RowKey, LoadedEntry>;
  readonly inserts: ReadonlyMap<string, InsertEntry>;
}

const EMPTY_CACHE: OverlayCache = { loaded: new Map(), inserts: new Map() };
/** A tooltip's value is cut here. */
const SHOWN_CHARS = 200;

/** A cell's value as a tooltip names it: NULL, "(empty string)", or its text. */
function shown(cell: unknown, category: ColumnCategory): string {
  const display = describeCell(cell, category, SHOWN_CHARS);
  if (display.kind === "empty") return "(empty string)";
  return `${display.text}${display.clipped ? "…" : ""}${display.detail ?? ""}`;
}

/** "Was: …", a changed cell's tooltip and the value editor's line. */
export const wasText = (cell: unknown, category: ColumnCategory) => `Was: ${shown(cell, category)}`;

const messages = (problems: readonly StagedProblem[]) => problems.map((problem) => problem.message).join("\n");

function problemFor(problems: readonly StagedProblem[] | undefined, column: string): string | undefined {
  const own = problems?.filter((problem) => problem.column === column) ?? [];
  return own.length ? messages(own) : undefined;
}

/** The problems that are not on a cell the grid shows: the change's own, and those of columns not in the result. */
function rowProblem(problems: readonly StagedProblem[] | undefined, result: ResultColumns): string | undefined {
  const rest = problems?.filter((problem) => problem.column === null || !result.byName.has(problem.column)) ?? [];
  return rest.length ? messages(rest) : undefined;
}

function insertEntry(
  insert: StagedInsert,
  problems: readonly StagedProblem[] | undefined,
  input: OverlayInput,
): InsertEntry {
  const cells: Record<number, CellMark> = {};
  const row = input.columns.map((column, index) => {
    const value = insert.values[column.name];
    const problem = problemFor(problems, column.name);
    if (!value) cells[index] = { changed: false, isDefault: true, problem };
    else if (problem) cells[index] = { changed: false, problem };
    return value ? displayCell(value, input.kinds.get(column.name)) : null;
  });
  const mark: RowMark = { kind: "new", problem: rowProblem(problems, input.result), cells };
  return { insert, problems, columns: input.columns, row, mark };
}

function loadedEntry(
  source: readonly unknown[],
  update: StagedUpdate | undefined,
  remove: StagedDelete | undefined,
  problems: readonly StagedProblem[] | undefined,
  input: OverlayInput,
): LoadedEntry {
  const base = { source, update, remove, problems, columns: input.columns };
  if (remove || !update) {
    // Marked for deletion: the loaded values, struck through (an UPDATE under it stays hidden until it is restored).
    return {
      ...base,
      row: source as unknown[],
      mark: { kind: "deleted", problem: rowProblem(problems, input.result) },
    };
  }
  const row = source.slice();
  const cells: Record<number, CellMark> = {};
  for (const [column, value] of Object.entries(update.set)) {
    const index = input.result.byName.get(column);
    if (index === undefined) continue; // not in the result any more: the next preview says `unknown_column`
    const category = input.columns[index].category;
    const original = update.original[column];
    row[index] = displayCell(value, input.kinds.get(column));
    cells[index] = {
      changed: true,
      was: wasText(original, category),
      problem: problemFor(problems, column),
      conflict: sameCell(source[index], original)
        ? undefined
        : `Changed on the server: now ${shown(source[index], category)}`,
    };
  }
  return { ...base, row, mark: { kind: "changed", problem: rowProblem(problems, input.result), cells } };
}

/** The display rows and marks; `cache` is the previous call's (null the first time). */
export function overlay(input: OverlayInput, cache: OverlayCache | null): { overlay: Overlay; cache: OverlayCache } {
  const { staged, rows } = input;
  if (!staged || !hasChanges(staged)) {
    return { overlay: { rows, marks: NO_MARKS, leading: 0, notShown: 0 }, cache: EMPTY_CACHE };
  }
  const loaded = new Map<RowKey, LoadedEntry>();
  const inserts = new Map<string, InsertEntry>();

  const top: unknown[][] = [];
  const topMarks: RowMark[] = [];
  for (let i = staged.inserts.length - 1; i >= 0; i -= 1) {
    const insert = staged.inserts[i];
    const problems = staged.problems[insert.id];
    const old = cache?.inserts.get(insert.id);
    const entry =
      old && old.insert === insert && old.problems === problems && old.columns === input.columns
        ? old
        : insertEntry(insert, problems, input);
    inserts.set(insert.id, entry);
    top.push(entry.row);
    topMarks.push(entry.mark);
  }

  let body = rows;
  let bodyMarks: (RowMark | undefined)[] | null = null;
  let notShown = 0;
  const touched = new Set([...Object.keys(staged.updates), ...Object.keys(staged.deletes)]);
  for (const rowKey of touched) {
    const index = input.result.keyIndexes ? input.binding?.byKey.get(rowKey) : undefined;
    if (index === undefined || index >= rows.length) {
      notShown += 1;
      continue;
    }
    const source = rows[index];
    const update = staged.updates[rowKey];
    const remove = staged.deletes[rowKey];
    const problems = staged.problems[(remove ?? update)!.id];
    const old = cache?.loaded.get(rowKey);
    const entry =
      old &&
      old.source === source &&
      old.update === update &&
      old.remove === remove &&
      old.problems === problems &&
      old.columns === input.columns
        ? old
        : loadedEntry(source, update, remove, problems, input);
    loaded.set(rowKey, entry);
    if (body === rows) {
      body = rows.slice();
      bodyMarks = new Array<RowMark | undefined>(rows.length);
    }
    body[index] = entry.row;
    bodyMarks![index] = entry.mark;
  }

  const leading = top.length;
  const display = leading ? [...top, ...body] : body;
  const marks = leading
    ? [...topMarks, ...(bodyMarks ?? new Array<RowMark | undefined>(rows.length))]
    : (bodyMarks ?? NO_MARKS);
  return { overlay: { rows: display, marks, leading, notShown }, cache: { loaded, inserts } };
}

/** The row at a display index as staging knows it; null past the end or when the key is not in the result. */
export function rowRefAt(
  display: Overlay,
  staged: StagedEdits | null,
  rows: readonly (readonly unknown[])[],
  index: number,
  result: ResultColumns,
): RowRef | null {
  if (index < display.leading) {
    const inserts = staged?.inserts ?? [];
    const insert = inserts[inserts.length - 1 - index];
    return insert ? { kind: "insert", id: insert.id } : null;
  }
  const row = rows[index - display.leading];
  const { keyIndexes, keyColumns } = result;
  if (!row || !keyIndexes) return null;
  const key = Object.fromEntries(keyColumns.map((column, i) => [column, row[keyIndexes[i]] ?? null]));
  return { kind: "row", rowKey: rowKeyOf(row, keyIndexes), key };
}

/** Where a row is shown now (its display index), or -1. */
export function displayIndexOf(
  display: Overlay,
  staged: StagedEdits | null,
  binding: Binding | null,
  ref: RowRef,
): number {
  if (ref.kind === "insert") {
    const inserts = staged?.inserts ?? [];
    const index = inserts.findIndex((insert) => insert.id === ref.id);
    return index < 0 ? -1 : inserts.length - 1 - index;
  }
  const index = binding?.byKey.get(ref.rowKey);
  return index === undefined ? -1 : display.leading + index;
}
```

- [ ] **Step 5: Run the tests, the types and the build**

Run: `npx vitest run src/features/edits && npm run typecheck && npm run build`
Expected: PASS; no errors.

- [ ] **Step 6: Commit**

```bash
git add src/features/grid/marks.ts src/features/edits/overlay.ts src/features/edits/overlay.test.ts
git commit -m "feat(app): grid-edit overlay — staged changes bound by key over the loaded rows, untouched rows kept, marks"
```

---

### Task 4: Staged edits in `workspace.json` — the backend model, `savedEdits`, save and restore

**Suggested implementer:** Sonnet.

**Files:**
- Modify (backend): `backend/dbaguy_backend/domain/edits.py`, `backend/dbaguy_backend/domain/workspace.py`, `backend/dbaguy_backend/services/workspace_service.py`
- Test (backend): `backend/tests/test_local_stores.py`
- Modify (generated): `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts` (`npm run gen:api`); `src/lib/api/types.ts`
- Create: `src/features/edits/savedEdits.ts`; Test: `src/features/edits/savedEdits.test.ts`
- Modify: `src/features/workspace/workspaceFile.ts`, `src/features/workspace/workspaceSync.ts`; Test: `src/features/workspace/workspaceFile.test.ts`

**Interfaces:**
- Consumes: Task 2 (`StagedEdits`, `StagedUpdate`, `StagedDelete`, `emptyStaged`, `MAX_CHANGES`, `rowKeyFromKey`, the store's `readStagedEdits`, `putStagedEdits`, `forgetStagedEditsExcept`, `onStagedEditsChanged`); 05a's `CellInput`, `RowUpdate`, `RowInsert`, `RowDelete`, `ColumnName`, `MAX_VALUE_CHARS`, `MAX_CHANGES`, `_json_chars`.
- Produces:
  - Backend: `MAX_STAGED_CHARS = 8 * 1_048_576`; `StagedCell(CellInput)` (text ≤ MAX_VALUE_CHARS), `StagedRowUpdate(RowUpdate)`, `StagedRowInsert(RowInsert)`, `StagedEditsFile {version: Literal[1], key_columns (≥ 1), updates, inserts, deletes, outcome_unknown: bool}` (`extra="forbid"`, ids unique, ≤ MAX_CHANGES entries, ≤ MAX_STAGED_CHARS of text: values, keys and originals counted as `EditChanges` counts them); `WorkspaceTab.staged_edits: StagedEditsFile | None` (only `kind == "table"`). `_text_chars` and `_counted_ids` are the one place the size and the ids are checked, for `EditChanges` too (DRY, its behaviour unchanged).
  - `types.ts`: `StagedEditsFile`.
  - `savedEdits.ts`: `MAX_SAVED_VALUE_CHARS`, `MAX_SAVED_CHARS`; `type SavedEdits = { file: StagedEditsFile; tooLarge: false } | { file: null; tooLarge: boolean }`; `savedEdits(staged: StagedEdits | null): SavedEdits` (memoized per state); `restoredEdits(file: StagedEditsFile): StagedEdits`.
  - `workspaceFile.ts`: `type ReadStaged = (tabId: string) => StagedEditsFile | null`; `toSavedWorkspace(snapshot, readBuffer, readStaged = () => null)`; `RestoredWorkspace.staged: ReadonlyMap<string, StagedEditsFile>`.

Behaviour (spec §3.7, R-05-11, D-05-11, B40; rulings H6, H14, H22):
- The file keeps every staged change as staged, the UPDATE under a DELETE included (so a restore keeps it hidden and Restore brings it back), the key it was staged on and `outcome_unknown` (H6: the preview after a restart still warns about new rows). Not kept: undo history, problems (H22).
- `savedEdits` writes nothing for nothing staged; nothing at all, with `tooLarge: true`, when one value is over 1 MiB, the text is over 8 MiB in all (values, keys and originals, counted as the backend counts) or there are more than MAX_CHANGES entries. The edit bar shows the hint (Task 9).
- The backend's repair (`_repair_tab`) drops a `staged_edits` its model refuses, or one on a tab that is not a table tab, with one WARNING "Repaired tab N of the …: dropped the staged edits" (no value, no name), and keeps the tab (H14, B40) — on read and on `PUT /workspace` alike.
- Restore: the staged edits of the tabs restored go into the store before the tabs (a table tab finds them when it mounts); a table tab dropped because its profile is gone is listed as "<title> (N staged changes discarded)" (R-05-11). The pending-vs-file comparison and the writer's baseline include the staged edits.
- The writer saves on every staged change (`onStagedEditsChanged`) and forgets the staged edits of closed tabs as it serializes.

- [ ] **Step 1: Write the failing backend tests** — append to `backend/tests/test_local_stores.py`:

```diff
diff --git a/backend/tests/test_local_stores.py b/backend/tests/test_local_stores.py
index e1e8ec8..e63b850 100644
--- a/backend/tests/test_local_stores.py
+++ b/backend/tests/test_local_stores.py
@@ -670,3 +670,78 @@ def test_workspace_keeps_the_tab_context_and_params(tmp_path):  # Review Focus 5
         WorkspaceTab(id="d", kind="query", params={"n": "1"})  # a draft is keyed by `$name` or `$n`
     with pytest.raises(ValidationError):
         WorkspaceTab(id="d", kind="query", query_context="all")
+
+
+STAGED_PATH = [{"kind": "database", "name": "dbaguy"}, {"kind": "schema", "name": "public"},
+               {"kind": "folder", "name": "tables"}, {"kind": "table", "name": "orders"}]
+
+
+def staged_edits(**overrides):
+    """A table tab's staged edits as the frontend writes them (`savedEdits`, spec ⑤ §3.7)."""
+    return {
+        "version": 1,
+        "key_columns": ["id"],
+        "updates": [{"id": "u:1", "key": {"id": "9007199254740993"},
+                     "set": {"label": {"kind": "value", "text": "b"}, "note": {"kind": "null"}},
+                     "original": {"label": "a", "note": ""}},
+                    {"id": "u:2", "key": {"id": 2}, "set": {"data": {"kind": "value", "text": "0xdead"}},
+                     "original": {"data": {"$type": "bytes", "size": 100, "preview": "ab" * 64}}}],
+        "inserts": [{"id": "i:3", "values": {"label": {"kind": "value", "text": ""}}}, {"id": "i:4", "values": {}}],
+        "deletes": [{"id": "d:5", "key": {"id": 2}}],
+        "outcome_unknown": True,
+        **overrides,
+    }
+
+
+def test_workspace_keeps_a_table_tabs_staged_edits(tmp_path):  # spec ⑤ §3.7, Review Focus 5
+    state = WorkspaceState.model_validate({"tabs": [
+        {"id": "t1", "kind": "table", "title": "orders", "object_path": STAGED_PATH, "staged_edits": staged_edits()},
+        {"id": "t2", "kind": "table", "title": "orders", "object_path": STAGED_PATH},
+    ]})
+    service = WorkspaceService(tmp_path / "workspace.json")
+    service.put(state)
+    assert service.get() == state
+    assert service.get().tabs[0].staged_edits.updates[0].key == {"id": "9007199254740993"}  # digits stay text
+
+
+@pytest.mark.parametrize("bad", [
+    {"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"a": {"kind": "value", "text": "x" * (1_048_576 + 1)}},
+                  "original": {"a": None}}]},  # one value over 1 MiB (D-05-11)
+    {"inserts": [{"id": f"i:{n}", "values": {}} for n in range(10_001)], "updates": [], "deletes": []},
+    {"inserts": [{"id": "i:1", "values": {}}, {"id": "i:1", "values": {}}]},  # a repeated id
+    {"inserts": [{"id": f"i:{n}", "values": {"a": {"kind": "value", "text": "x" * 1_048_576}}} for n in range(9)]},
+    {"updates": [{"id": "u:1", "key": {"id": 1}, "set": {"a": {"kind": "null"}}, "original": {}}]},  # no original
+    {"key_columns": []},
+    {"version": 2},
+    {"extra": True},
+])
+def test_staged_edits_beyond_their_limits_are_refused(bad):
+    with pytest.raises(ValidationError):
+        WorkspaceTab(id="t", kind="table", staged_edits=staged_edits(**bad))
+
+
+def test_only_a_table_tab_keeps_staged_edits():
+    with pytest.raises(ValidationError):
+        WorkspaceTab(id="q", kind="query", staged_edits=staged_edits())
+
+
+def test_a_tab_with_staged_edits_the_model_refuses_keeps_everything_else(tmp_path, logged_warnings):  # B40
+    path = tmp_path / "workspace.json"
+    secret = "secret-value"
+    path.write_text(json.dumps({"tabs": [
+        {"id": "t1", "kind": "table", "title": "orders", "object_path": STAGED_PATH,
+         "staged_edits": staged_edits(inserts=[{"id": "i:1", "values": {"a": {"kind": "value", "text": secret}}},
+                                               {"id": "i:1", "values": {}}])},
+        {"id": "q1", "kind": "query", "buffer": "kept", "staged_edits": staged_edits()},
+        {"id": "t2", "kind": "table", "title": "orders", "object_path": STAGED_PATH, "staged_edits": staged_edits()},
+    ]}))
+    t1, q1, t2 = WorkspaceService(path).get().tabs
+    assert (t1.id, t1.object_path is not None, t1.staged_edits) == ("t1", True, None)
+    assert (q1.buffer, q1.staged_edits) == ("kept", None)
+    assert t2.staged_edits is not None  # a good one is untouched
+    assert [line for line in logged_warnings if line.startswith("Repaired")] == [
+        "Repaired tab 0 of the saved workspace: dropped the staged edits",
+        "Repaired tab 1 of the saved workspace: dropped the staged edits"]
+    assert secret not in "".join(logged_warnings)
+    raw = {"tabs": [{"id": "t1", "kind": "table", "staged_edits": {"version": 1, "key_columns": []}}]}
+    assert WorkspaceState.model_validate(repaired_workspace(raw)).tabs[0].staged_edits is None  # `PUT /workspace`
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_local_stores.py -k "staged"`
Expected: FAIL (`staged_edits` is an extra field).

- [ ] **Step 3: The backend model and the repair**

```diff
diff --git a/backend/dbaguy_backend/domain/edits.py b/backend/dbaguy_backend/domain/edits.py
index 1b85602..5f9076f 100644
--- a/backend/dbaguy_backend/domain/edits.py
+++ b/backend/dbaguy_backend/domain/edits.py
@@ -1,6 +1,7 @@
 """Grid editing (spec ⑤ §3): how a table's rows are identified and written (EditTarget), the changes a tab stages
 (EditChanges), what a preview shows (EditPreview) and the one-time plan a session applies (EditPlan)."""
 
+from collections.abc import Sequence
 from typing import Annotated, Literal, Self
 from uuid import UUID
 
@@ -19,6 +20,7 @@ VALUE_ACCEPTED_CHARS = 4 * MAX_VALUE_CHARS  # beyond this the request itself is
 MAX_COMMIT_CHARS = 64 * 1_048_576  # L15: every value, key and original cell of one commit together (422 beyond it)
 MAX_PLAN_CHARS = 128 * 1_048_576  # L15: the statements and scripts of all stored plans together (EditPlanStore)
 COMMIT_TOO_LARGE = f"The changes are too large: at most {MAX_COMMIT_CHARS // 1_048_576} MiB of values per commit"
+MAX_STAGED_CHARS = 8 * 1_048_576  # D-05-11: the staged text one tab keeps in workspace.json (spec ⑤ §3.7)
 PLAN_TTL_S = 15 * 60  # D-05-7
 MAX_PLANS = 32  # D-05-7
 PLAN_EXPIRED = "plan_expired"
@@ -132,6 +134,25 @@ def _json_chars(cell: JsonValue) -> int:
     return total
 
 
+def _text_chars(updates: Sequence[RowUpdate], inserts: Sequence[RowInsert], deletes: Sequence[RowDelete]) -> int:
+    """The text a set of changes carries: every value's, key's and original's (`_json_chars`)."""
+    total = sum(len(cell.text or "") for change in updates for cell in change.set.values())
+    total += sum(len(cell.text or "") for change in inserts for cell in change.values.values())
+    total += sum(_json_chars(change.key) for change in (*updates, *deletes))
+    total += sum(_json_chars(change.original) for change in updates)
+    return total
+
+
+def _counted_ids(changes: Sequence[RowUpdate | RowInsert | RowDelete]) -> list[str]:
+    """The changes' ids, refused beyond MAX_CHANGES or when one repeats."""
+    ids = [change.id for change in changes]
+    if len(ids) > MAX_CHANGES:
+        raise ValueError(f"at most {MAX_CHANGES:,} changes are committed at once")
+    if len(set(ids)) != len(ids):
+        raise ValueError("change ids must be unique")
+    return ids
+
+
 class EditChanges(_Input):
     updates: list[RowUpdate] = Field(default_factory=list)
     inserts: list[RowInsert] = Field(default_factory=list)
@@ -139,29 +160,55 @@ class EditChanges(_Input):
 
     @model_validator(mode="after")
     def _counted_and_unique(self) -> Self:
-        ids = [change.id for change in (*self.deletes, *self.updates, *self.inserts)]
-        if not ids:
+        if not _counted_ids((*self.deletes, *self.updates, *self.inserts)):
             raise ValueError("nothing to commit")
-        if len(ids) > MAX_CHANGES:
-            raise ValueError(f"at most {MAX_CHANGES:,} changes are committed at once")
-        if len(set(ids)) != len(ids):
-            raise ValueError("change ids must be unique")
         return self
 
     @model_validator(mode="after")
     def _bounded_in_size(self) -> Self:
         """L15: one value may be 4 MiB of text before it is invalid and 10,000 changes may carry one each, so the commit
         as a whole is bounded too: what is previewed is held in memory twice (statements and script) until applied."""
-        total = sum(len(cell.text or "") for change in self.updates for cell in change.set.values())
-        total += sum(len(cell.text or "") for change in self.inserts for cell in change.values.values())
-        total += sum(_json_chars(change.key) for change in (*self.updates, *self.deletes))
-        total += sum(_json_chars(change.original) for change in self.updates)
-        if total > MAX_COMMIT_CHARS:
+        if _text_chars(self.updates, self.inserts, self.deletes) > MAX_COMMIT_CHARS:
             # a custom error, so `detail[0].msg` is the text itself (a ValueError's gets "Value error, " in front)
             raise PydanticCustomError("commit_too_large", COMMIT_TOO_LARGE)
         return self
 
 
+class StagedCell(CellInput):
+    """A staged cell as `workspace.json` keeps it: one value of at most MAX_VALUE_CHARS (D-05-11), not a request's 4x."""
+
+    text: str | None = Field(default=None, max_length=MAX_VALUE_CHARS)
+
+
+class StagedRowUpdate(RowUpdate):
+    set: dict[ColumnName, StagedCell] = Field(min_length=1)
+
+
+class StagedRowInsert(RowInsert):
+    values: dict[ColumnName, StagedCell] = Field(default_factory=dict)
+
+
+class StagedEditsFile(_Input):
+    """A table-data tab's staged grid edits in `workspace.json` (spec ⑤ §3.7, ruling H6): the changes as staged (an
+    UPDATE under its row's DELETE included), the key they were staged on, and whether the last commit's outcome is
+    unknown. Bounded like a commit (D-05-11) and by MAX_STAGED_CHARS in all: the frontend writes none that is larger
+    (`savedEdits`), and the workspace repair drops one this model refuses rather than the tab (B40)."""
+
+    version: Literal[1] = 1
+    key_columns: list[ColumnName] = Field(min_length=1)
+    updates: list[StagedRowUpdate] = Field(default_factory=list)
+    inserts: list[StagedRowInsert] = Field(default_factory=list)
+    deletes: list[RowDelete] = Field(default_factory=list)
+    outcome_unknown: bool = False
+
+    @model_validator(mode="after")
+    def _counted_and_bounded(self) -> Self:
+        _counted_ids((*self.deletes, *self.updates, *self.inserts))
+        if _text_chars(self.updates, self.inserts, self.deletes) > MAX_STAGED_CHARS:
+            raise ValueError(f"at most {MAX_STAGED_CHARS // 1_048_576} MiB of staged text is kept")
+        return self
+
+
 EditProblemCode = Literal[
     "invalid_number", "invalid_json", "invalid_hex", "invalid_bool", "invalid_text", "value_too_large",
     "read_only_column", "unknown_column", "not_nullable", "key_incomplete", "default_not_allowed",
```

```diff
diff --git a/backend/dbaguy_backend/domain/workspace.py b/backend/dbaguy_backend/domain/workspace.py
index 27bf67f..99584aa 100644
--- a/backend/dbaguy_backend/domain/workspace.py
+++ b/backend/dbaguy_backend/domain/workspace.py
@@ -1,9 +1,10 @@
-from typing import Annotated, Literal
+from typing import Annotated, Literal, Self
 from uuid import UUID
 
-from pydantic import BaseModel, ConfigDict, Field
+from pydantic import BaseModel, ConfigDict, Field, model_validator
 
 from .couchbase import CAS_PATTERN
+from .edits import StagedEditsFile
 from .execution import PARAM_NAME, QueryContext
 from .metadata import PathSegment
 
@@ -43,6 +44,14 @@ class WorkspaceTab(BaseModel):
     params: dict[ParamName, ParamDraft] | None = Field(default=None, max_length=MAX_PARAM_DRAFTS)
     # Couchbase document tab: the CAS its unsaved `buffer` is based on (decimal text); None for a new document
     document_cas: DocumentCas | None = None
+    # A table tab's staged grid edits (spec ⑤ §3.7); None: nothing staged, or too large to keep
+    staged_edits: StagedEditsFile | None = None
+
+    @model_validator(mode="after")
+    def _staged_edits_on_a_table_tab(self) -> Self:
+        if self.staged_edits is not None and self.kind != "table":
+            raise ValueError("only a table tab keeps staged edits")
+        return self
 
 
 class WorkspaceState(BaseModel):
```

```diff
diff --git a/backend/dbaguy_backend/services/workspace_service.py b/backend/dbaguy_backend/services/workspace_service.py
index 4c69cce..2526efc 100644
--- a/backend/dbaguy_backend/services/workspace_service.py
+++ b/backend/dbaguy_backend/services/workspace_service.py
@@ -5,6 +5,7 @@ from typing import Any, Literal, NamedTuple
 from loguru import logger
 from pydantic import TypeAdapter, ValidationError
 
+from ..domain.edits import StagedEditsFile
 from ..domain.workspace import (
     MAX_PARAM_DRAFTS,
     DocumentCas,
@@ -21,6 +22,7 @@ _NAME = TypeAdapter(ParamName)
 _DRAFT = TypeAdapter(ParamDraft)
 _CONTEXT = TypeAdapter(TabQueryContext | None)
 _CAS = TypeAdapter(DocumentCas | None)
+_STAGED = TypeAdapter(StagedEditsFile | None)
 _Extra = Literal["ignore"] | None  # as for `model_validate`: the file may come from a newer build, a request may not
 
 
@@ -29,6 +31,7 @@ class _Repair(NamedTuple):
     drafts_removed: int = 0
     context_reset: bool = False
     cas_reset: bool = False
+    staged_reset: bool = False
 
 
 def _valid(adapter: TypeAdapter[Any], value: Any, extra: _Extra) -> bool:
@@ -40,10 +43,11 @@ def _valid(adapter: TypeAdapter[Any], value: Any, extra: _Extra) -> bool:
 
 
 def _repair_tab(tab: Any, extra: _Extra) -> _Repair:
-    """The tab without what is wrong in its saved parameter drafts, query context and document CAS, which are
-    conveniences: a draft with a name or a size the model refuses is removed (and the drafts beyond the limit), a
-    context it refuses goes back to the profile's default, and a CAS it refuses is forgotten (the document tab then
-    reads as a new document). Everything else, the buffer first, stays as it was."""
+    """The tab without what is wrong in its saved parameter drafts, query context, document CAS and staged grid edits:
+    a draft with a name or a size the model refuses is removed (and the drafts beyond the limit), a context it refuses
+    goes back to the profile's default, a CAS it refuses is forgotten (the document tab then reads as a new document),
+    and staged edits it refuses (or on a tab that is not a table tab) are dropped, the tab kept (B40: one bad entry must
+    not cost every tab its saved state). Everything else, the buffer first, stays as it was."""
     if not isinstance(tab, dict):
         return _Repair(tab)
     repaired, removed, reset = tab, 0, False
@@ -61,7 +65,11 @@ def _repair_tab(tab: Any, extra: _Extra) -> _Repair:
     cas_reset = tab.get("document_cas") is not None and not _valid(_CAS, tab["document_cas"], extra)
     if cas_reset:
         repaired = {**repaired, "document_cas": None}
-    return _Repair(repaired, removed, reset, cas_reset)
+    staged = tab.get("staged_edits")
+    staged_reset = staged is not None and (tab.get("kind") != "table" or not _valid(_STAGED, staged, extra))
+    if staged_reset:
+        repaired = {**repaired, "staged_edits": None}
+    return _Repair(repaired, removed, reset, cas_reset, staged_reset)
 
 
 def _repaired_tabs(raw: Any, where: str, extra: _Extra) -> Any:
@@ -73,11 +81,12 @@ def _repaired_tabs(raw: Any, where: str, extra: _Extra) -> Any:
     repaired = []
     for index, tab in enumerate(tabs):
         result = _repair_tab(tab, extra)
-        if result.drafts_removed or result.context_reset or result.cas_reset:
-            logger.warning("Repaired tab {} of the {}: {}", index, where, " and ".join(
-                part for part in (f"removed {result.drafts_removed} parameter draft(s)" if result.drafts_removed else "",
-                                  "reset the query context" if result.context_reset else "",
-                                  "reset the document CAS" if result.cas_reset else "") if part))
+        parts = (f"removed {result.drafts_removed} parameter draft(s)" if result.drafts_removed else "",
+                 "reset the query context" if result.context_reset else "",
+                 "reset the document CAS" if result.cas_reset else "",
+                 "dropped the staged edits" if result.staged_reset else "")
+        if any(parts):
+            logger.warning("Repaired tab {} of the {}: {}", index, where, " and ".join(part for part in parts if part))
         repaired.append(result.tab)
     return {**raw, "tabs": repaired}
```

- [ ] **Step 4: Run the backend tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_local_stores.py tests/test_edit_models.py tests/test_edits_api.py tests/test_edit_plans.py && .venv/bin/pytest -q tests/test_api.py -k workspace`
Expected: PASS.

- [ ] **Step 5: Regenerate the API types and export the new one**

Run: `npm run gen:api` (only `StagedEditsFile`, `StagedRowUpdate`, `StagedRowInsert`, `StagedCell` and `WorkspaceTab.staged_edits` change).

```diff
diff --git a/src/lib/api/types.ts b/src/lib/api/types.ts
index c07cc18..f6b7009 100644
--- a/src/lib/api/types.ts
+++ b/src/lib/api/types.ts
@@ -126,6 +126,8 @@ export type ApplyEditsCommand = S["ApplyEditsCommand"];
 export type EditOutcomeEvent = S["EditOutcomeEvent"];
 /** "committed", "rolled_back" (nothing was applied), "partial" (a table that cannot roll back) or "unknown". */
 export type EditOutcome = EditOutcomeEvent["outcome"];
+/** Grid editing (spec ⑤ §3.7): a table tab's staged edits as `workspace.json` keeps them. */
+export type StagedEditsFile = S["StagedEditsFile"];
 
 export function pathKey(path: PathSegment[]): string {
   return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

- [ ] **Step 6: Write the failing frontend tests**

`src/features/edits/savedEdits.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { MAX_SAVED_CHARS, MAX_SAVED_VALUE_CHARS, restoredEdits, savedEdits } from "./savedEdits";
import { rowKeyOf } from "./overlay";
import {
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  stage,
  type NewId,
  type StageAction,
  type StagedEdits,
} from "./staging";

/*
 * What `workspace.json` keeps of staged edits (spec ⑤ §3.7, B40): only what the backend's model takes, nothing at all
 * beyond its limits (so one large set never fails every save), and the same changes bound to the same rows on restore.
 */

const ref = (id: unknown) => ({ kind: "row", rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } }) as const;

function staged(...actions: StageAction[]): StagedEdits {
  let n = 0;
  const newId: NewId = (prefix) => `${prefix}:${++n}`;
  return actions.reduce((state, action) => stage(state, action, newId), emptyStaged(["id"]));
}

const edit = (id: unknown, text: string, original: unknown = "a"): StageAction => ({
  type: "edit",
  cell: { row: ref(id), column: "label", original },
  input: { kind: "value", text },
});

describe("saving staged edits", () => {
  it("keeps nothing for nothing staged", () => {
    expect(savedEdits(null)).toEqual({ file: null, tooLarge: false });
    expect(savedEdits(emptyStaged(["id"]))).toEqual({ file: null, tooLarge: false });
  });

  it("keeps every change, the UPDATE under a DELETE included, with the key and an unknown outcome", () => {
    const state = stage(
      staged(
        edit("9007199254740993", "b"),
        { type: "deleteRows", rows: [ref("9007199254740993")] },
        { type: "addRow", id: "i:x" },
      ),
      { type: "applyOutcome", outcome: { outcome: "unknown", applied: [], failedChange: null, message: null } },
    );
    expect(savedEdits(state)).toEqual({
      tooLarge: false,
      file: {
        version: 1,
        key_columns: ["id"],
        updates: [
          {
            id: "u:1",
            key: { id: "9007199254740993" },
            set: { label: { kind: "value", text: "b" } },
            original: { label: "a" },
          },
        ],
        inserts: [{ id: "i:x", values: {} }],
        deletes: [{ id: "d:2", key: { id: "9007199254740993" } }],
        outcome_unknown: true,
      },
    });
  });

  it("is computed once per state", () => {
    const state = staged(edit(1, "b"));
    expect(savedEdits(state)).toBe(savedEdits(state));
  });

  it("keeps nothing, and says so, beyond one value's limit, the total, or the number of changes", () => {
    expect(savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS + 1)))).tooLarge).toBe(true);
    expect(savedEdits(staged(edit(1, "x".repeat(MAX_SAVED_VALUE_CHARS)))).tooLarge).toBe(false);
    const big = Array.from({ length: Math.ceil(MAX_SAVED_CHARS / MAX_SAVED_VALUE_CHARS) + 1 }, (_, i) =>
      edit(i, "x".repeat(MAX_SAVED_VALUE_CHARS)),
    );
    expect(savedEdits(staged(...big))).toEqual({ file: null, tooLarge: true });
    // Entries are counted as the file holds them: a DELETE and the UPDATE under it are two.
    const many = staged(...Array.from({ length: MAX_CHANGES / 2 + 1 }, (_, i) => edit(i, "b")), {
      type: "deleteRows",
      rows: Array.from({ length: MAX_CHANGES / 2 }, (_, i) => ref(i)),
    });
    expect(savedEdits(many).tooLarge).toBe(true);
  });
});

describe("restoring staged edits", () => {
  it("gives back the same changes, bound by key to the rows they were staged on", () => {
    const state = staged(
      edit(2, "b"),
      {
        type: "edit",
        cell: { row: ref(2), column: "data", original: { $type: "bytes", size: 2, preview: "dead" } },
        input: { kind: "null" },
      },
      { type: "addRow", id: "i:x" },
      {
        type: "edit",
        cell: { row: { kind: "insert", id: "i:x" }, column: "label" },
        input: { kind: "value", text: "" },
      },
      { type: "deleteRows", rows: [ref(3)] },
    );
    const file = savedEdits(state).file!;
    const back = restoredEdits(JSON.parse(JSON.stringify(file)));
    expect(savedEdits(back).file).toEqual(file);
    expect(Object.keys(back.updates)).toEqual([rowKeyOf([2, "row"], [0])]);
    expect(back.inserts[0].values).toEqual({ label: { kind: "value", text: "" } });
    expect(back.unknownOutcome).toBeNull();
    expect(back.past).toEqual([]);
  });

  it("brings an unknown outcome back as waiting for fresh rows", () => {
    const file = {
      version: 1 as const,
      key_columns: ["id"],
      updates: [],
      inserts: [],
      deletes: [],
      outcome_unknown: true,
    };
    expect(restoredEdits(file).unknownOutcome).toBe("pending");
  });
});
```

Append to `src/features/workspace/workspaceFile.test.ts`:

```diff
diff --git a/src/features/workspace/workspaceFile.test.ts b/src/features/workspace/workspaceFile.test.ts
index 6b04b6d..cd5c176 100644
--- a/src/features/workspace/workspaceFile.test.ts
+++ b/src/features/workspace/workspaceFile.test.ts
@@ -161,3 +161,47 @@ describe("restoring a document tab whose connection is gone (M6)", () => {
     expect(restore({ profile_id: null, buffer: "{}" }).detached).toEqual([DETACHED]);
   });
 });
+
+/* A table tab's staged grid edits (spec ⑤ §3.7): kept beside the tab, restored into the staged-edits store. */
+
+describe("a table tab's staged edits", () => {
+  const TABLE_PATH = [
+    { kind: "schema", name: "public" },
+    { kind: "folder", name: "tables" },
+    { kind: "table", name: "orders" },
+  ];
+  const tableTab: Tab = { id: "table:1", kind: "table", profileId: PROFILE, path: TABLE_PATH, title: "orders" };
+  const file = {
+    version: 1 as const,
+    key_columns: ["id"],
+    updates: [],
+    inserts: [{ id: "i:1", values: {} }],
+    deletes: [{ id: "d:2", key: { id: 7 } }],
+    outcome_unknown: false,
+  };
+  const workspace = (entry: Partial<SavedTab>): SavedWorkspace => ({
+    version: 1,
+    tabs: [{ id: "table:1", kind: "table", title: "orders", profile_id: PROFILE, object_path: TABLE_PATH, ...entry }],
+    active_tab_id: null,
+    sidebar_tab: null,
+  });
+
+  it("are saved from the staged-edits store, and only when there are some", () => {
+    const save = (staged: typeof file | null) =>
+      toSavedWorkspace({ tabs: [tableTab], activeTabId: null, sidebarTab: null }, () => null, () => staged).tabs[0];
+    expect(save(file)).toMatchObject({ kind: "table", staged_edits: file });
+    expect(save(null)).not.toHaveProperty("staged_edits");
+  });
+
+  it("are restored beside their tab", () => {
+    const restored = fromSavedWorkspace(workspace({ staged_edits: file }), () => true);
+    expect(restored.snapshot.tabs).toEqual([tableTab]);
+    expect(restored.staged.get("table:1")).toEqual(file);
+  });
+
+  it("go with a tab whose connection is gone, and the closed list says how many", () => {
+    const restored = fromSavedWorkspace(workspace({ staged_edits: file }), () => false);
+    expect(restored.closed).toEqual(["orders (2 staged changes discarded)"]);
+    expect(restored.staged.size).toBe(0);
+  });
+});
```

Run: `npx vitest run src/features/edits/savedEdits.test.ts src/features/workspace`
Expected: FAIL (cannot resolve `./savedEdits`; `restored.staged` undefined).

- [ ] **Step 7: Write `src/features/edits/savedEdits.ts`**

```ts
import type { StagedEditsFile } from "@/lib/api/types";
import type { CellInput } from "./cells";
import {
  emptyStaged,
  MAX_CHANGES,
  rowKeyFromKey,
  type StagedDelete,
  type StagedEdits,
  type StagedUpdate,
} from "./staging";

/*
 * A table tab's staged edits in `workspace.json` (spec ⑤ §3.7, R-05-11). `PUT /workspace` validates the whole state, so
 * one staged set the backend refuses would fail every save and a crash would lose every tab's unsaved work (Q2, B40):
 * `savedEdits` writes only what the backend's `StagedEditsFile` takes, and a set beyond its limits not at all (the edit
 * bar then says it will not survive a restart). The undo history and the last preview's problems are never kept.
 */

/** D-05-11, as `MAX_VALUE_CHARS` in `domain/edits.py`: one staged value. */
export const MAX_SAVED_VALUE_CHARS = 1_048_576;
/** D-05-11, as `MAX_STAGED_CHARS`: all of one tab's staged text (values, keys and originals). */
export const MAX_SAVED_CHARS = 8 * 1_048_576;

export type SavedEdits = { file: StagedEditsFile; tooLarge: false } | { file: null; tooLarge: boolean };

const NOTHING: SavedEdits = { file: null, tooLarge: false };
const TOO_LARGE: SavedEdits = { file: null, tooLarge: true };

/** The text a cell holds as the backend counts it (`_json_chars`): each string's length, an object's keys too, 1 a scalar. */
function jsonChars(cell: unknown): number {
  let total = 0;
  const pending = [cell];
  while (pending.length) {
    const item = pending.pop();
    if (typeof item === "string") total += item.length;
    else if (Array.isArray(item)) pending.push(...item);
    else if (item !== null && typeof item === "object") {
      for (const [name, value] of Object.entries(item)) {
        total += name.length;
        pending.push(value);
      }
    } else total += 1;
  }
  return total;
}

const cellsChars = (cells: Readonly<Record<string, unknown>>) =>
  Object.values(cells).reduce<number>((sum, cell) => sum + jsonChars(cell), 0);

/** A staged value's text and whether it fits one value's limit. */
function valuesChars(values: Readonly<Record<string, CellInput>>): number | null {
  let total = 0;
  for (const input of Object.values(values)) {
    if (input.kind !== "value") continue;
    if (input.text.length > MAX_SAVED_VALUE_CHARS) return null;
    total += input.text.length;
  }
  return total;
}

const bySeq = <T extends { seq: number }>(items: T[]): T[] => items.sort((a, b) => a.seq - b.seq);

const cache = new WeakMap<StagedEdits, SavedEdits>();

/** What `workspace.json` keeps of a tab's staged edits; memoized per state (the writer asks on every save). */
export function savedEdits(staged: StagedEdits | null): SavedEdits {
  if (!staged) return NOTHING;
  const known = cache.get(staged);
  if (known) return known;
  const result = build(staged);
  cache.set(staged, result);
  return result;
}

function build(staged: StagedEdits): SavedEdits {
  const updates = bySeq(Object.values(staged.updates));
  const inserts = bySeq([...staged.inserts]);
  const deletes = bySeq(Object.values(staged.deletes));
  const entries = updates.length + inserts.length + deletes.length;
  if (entries === 0 && staged.unknownOutcome === null) return NOTHING;
  if (entries > MAX_CHANGES || staged.keyColumns.length === 0) return TOO_LARGE;
  let total = 0;
  for (const change of [...updates, ...inserts]) {
    const values = valuesChars("set" in change ? change.set : change.values);
    if (values === null) return TOO_LARGE;
    total += values;
  }
  total += updates.reduce((sum, u) => sum + cellsChars(u.key) + cellsChars(u.original), 0);
  total += deletes.reduce((sum, d) => sum + cellsChars(d.key), 0);
  if (total > MAX_SAVED_CHARS) return TOO_LARGE;
  return {
    tooLarge: false,
    file: {
      version: 1,
      key_columns: [...staged.keyColumns],
      updates: updates.map((u) => ({ id: u.id, key: { ...u.key }, set: { ...u.set }, original: { ...u.original } })),
      inserts: inserts.map((i) => ({ id: i.id, values: { ...i.values } })),
      deletes: deletes.map((d) => ({ id: d.id, key: { ...d.key } })),
      outcome_unknown: staged.unknownOutcome !== null,
    },
  };
}

/** The backend's cell as a staged value. */
function inputOf(cell: { kind: "value" | "null" | "default"; text?: string | null }): CellInput {
  return cell.kind === "value" ? { kind: "value", text: cell.text ?? "" } : { kind: cell.kind };
}

const inputs = (cells: Readonly<Record<string, { kind: "value" | "null" | "default"; text?: string | null }>> = {}) =>
  Object.fromEntries(Object.entries(cells).map(([column, cell]) => [column, inputOf(cell)]));

/**
 * Staged edits read back from `workspace.json`: bound again to their rows by key once the Data view has rows. An
 * unknown outcome comes back "pending": the first rows read count as the refresh it waits for.
 */
export function restoredEdits(file: StagedEditsFile): StagedEdits {
  const keyColumns = file.key_columns;
  let seq = 0;
  const updates: Record<string, StagedUpdate> = {};
  for (const u of file.updates ?? []) {
    const rowKey = rowKeyFromKey(u.key, keyColumns);
    if (!(rowKey in updates))
      updates[rowKey] = { id: u.id, key: u.key, set: inputs(u.set), original: u.original, seq: seq++ };
  }
  const inserts = (file.inserts ?? []).map((i) => ({ id: i.id, values: inputs(i.values), seq: seq++ }));
  const deletes: Record<string, StagedDelete> = {};
  for (const d of file.deletes ?? []) {
    const rowKey = rowKeyFromKey(d.key, keyColumns);
    if (!(rowKey in deletes)) deletes[rowKey] = { id: d.id, key: d.key, seq: seq++ };
  }
  return {
    ...emptyStaged(keyColumns),
    updates,
    inserts,
    deletes,
    unknownOutcome: file.outcome_unknown ? "pending" : null,
    nextSeq: seq,
  };
}
```

- [ ] **Step 8: Save and restore them with the workspace**

```diff
diff --git a/src/features/workspace/workspaceFile.ts b/src/features/workspace/workspaceFile.ts
index b6855b8..9c083ad 100644
--- a/src/features/workspace/workspaceFile.ts
+++ b/src/features/workspace/workspaceFile.ts
@@ -1,4 +1,5 @@
-import type { PathSegment, SavedTab, SavedWorkspace, TextSelection } from "@/lib/api/types";
+import type { PathSegment, SavedTab, SavedWorkspace, StagedEditsFile, TextSelection } from "@/lib/api/types";
+import { plural } from "@/lib/format";
 import { PARAM_NAME } from "@/lib/paramNames";
 import { matchesFile } from "@/lib/scriptText";
 import type { EditorBuffer } from "@/stores/editorBuffers";
@@ -20,7 +21,8 @@ import { keyspaceOfPath } from "../explorer/fieldSamples";
  * text as typed, so a crash or a restart loses none of them (`savedParams`). A Couchbase document tab keeps its
  * connection, its path (the collection, then the document), the CAS its text is based on, and its text only while it
  * is unsaved (a new document, or edited): a clean document is read again from the server (ruling C15). Unsaved document
- * text whose connection is gone comes back in a query tab without a connection (M6).
+ * text whose connection is gone comes back in a query tab without a connection (M6). A table tab keeps its staged grid
+ * edits (spec ⑤ §3.7) as `savedEdits` gives them: they are not tab state, so they are read and restored beside it.
  */
 
 const WORKSPACE_VERSION = 1;
@@ -108,8 +110,16 @@ function savedDocument(tab: DocumentTabState, readBuffer: (tabId: string) => Edi
   };
 }
 
-function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null): SavedTab {
+/** A table tab's staged edits as `workspace.json` keeps them (`savedEdits`), or null. */
+export type ReadStaged = (tabId: string) => StagedEditsFile | null;
+
+function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null, readStaged: ReadStaged): SavedTab {
   if (tab.kind === "document") return savedDocument(tab, readBuffer);
+  if (tab.kind === "table") {
+    const saved = { id: tab.id, kind: tab.kind, title: tab.title, profile_id: tab.profileId, object_path: tab.path };
+    const staged = readStaged(tab.id);
+    return staged ? { ...saved, staged_edits: staged } : saved;
+  }
   if (tab.kind !== "query") {
     return { id: tab.id, kind: tab.kind, title: tab.title, profile_id: tab.profileId, object_path: tab.path };
   }
@@ -132,14 +142,18 @@ function savedTab(tab: Tab, readBuffer: (tabId: string) => EditorBuffer | null):
   };
 }
 
-/** The workspace as `PUT /workspace` takes it; `readBuffer` gives a query tab's current editor content. */
+/**
+ * The workspace as `PUT /workspace` takes it; `readBuffer` gives a query tab's current editor content, `readStaged` a
+ * table tab's staged edits.
+ */
 export function toSavedWorkspace(
   snapshot: WorkspaceSnapshot,
   readBuffer: (tabId: string) => EditorBuffer | null,
+  readStaged: ReadStaged = () => null,
 ): SavedWorkspace {
   return {
     version: WORKSPACE_VERSION,
-    tabs: snapshot.tabs.map((tab) => savedTab(tab, readBuffer)),
+    tabs: snapshot.tabs.map((tab) => savedTab(tab, readBuffer, readStaged)),
     active_tab_id: snapshot.activeTabId,
     sidebar_tab: snapshot.sidebarTab,
   };
@@ -157,8 +171,14 @@ export interface RestoredWorkspace {
    * whose unsaved text was reopened that way (said as such).
    */
   detached: string[];
+  /** The staged edits of the table tabs restored, by tab id (they go into `stores/stagedEdits.ts`). */
+  staged: ReadonlyMap<string, StagedEditsFile>;
 }
 
+/** The changes a saved staged set holds, as `closed` names them for a tab dropped with it. */
+const stagedCount = (file: StagedEditsFile) =>
+  (file.updates?.length ?? 0) + (file.inserts?.length ?? 0) + (file.deletes?.length ?? 0);
+
 /** A query tab as saved; with `profileId` null it reopens without a connection (nor its database or query context). */
 function restoredQuery(entry: SavedTab, profileId: string | null): QueryTabState {
   const connected = profileId !== null;
@@ -226,6 +246,7 @@ export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: st
   const tabs: Tab[] = [];
   const closed: string[] = [];
   const detached: string[] = [];
+  const staged = new Map<string, StagedEditsFile>();
   const seen = new Set<string>();
   for (const entry of saved.tabs) {
     if (seen.has(entry.id)) continue; // React keys and close guards need unique ids
@@ -248,8 +269,11 @@ export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: st
       }
     } else if (connected && entry.object_path?.length) {
       tabs.push({ id: entry.id, kind: entry.kind, profileId, path: entry.object_path, title: entry.title });
+      if (entry.kind === "table" && entry.staged_edits) staged.set(entry.id, entry.staged_edits);
     } else {
-      closed.push(entry.title);
+      // R-05-11: a table tab's staged edits go with it, and the list of closed tabs says so.
+      const lost = entry.staged_edits ? stagedCount(entry.staged_edits) : 0;
+      closed.push(lost ? `${entry.title} (${plural(lost, "staged change")} discarded)` : entry.title);
     }
   }
   const active = saved.active_tab_id ?? null;
@@ -261,5 +285,6 @@ export function fromSavedWorkspace(saved: SavedWorkspace, profileExists: (id: st
     },
     closed,
     detached,
+    staged,
   };
 }
```

```diff
diff --git a/src/features/workspace/workspaceSync.ts b/src/features/workspace/workspaceSync.ts
index 8513f3b..3dad89f 100644
--- a/src/features/workspace/workspaceSync.ts
+++ b/src/features/workspace/workspaceSync.ts
@@ -5,7 +5,9 @@ import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
 import type { SavedWorkspace } from "@/lib/api/types";
 import { getBackendInfo, isTauriRuntime, type BackendInfo } from "@/lib/backend";
 import { forgetEditorBuffersExcept, onEdited, readEditorBuffer } from "@/stores/editorBuffers";
-import { useWorkspace, type WorkspaceSnapshot } from "@/stores/workspace";
+import { forgetStagedEditsExcept, onStagedEditsChanged, putStagedEdits, readStagedEdits } from "@/stores/stagedEdits";
+import { useWorkspace } from "@/stores/workspace";
+import { restoredEdits, savedEdits } from "../edits/savedEdits";
 import { fetchProfiles, profilesKey } from "../connections/hooks";
 import { clearPendingWorkspace, readPendingWorkspace, writePendingWorkspace } from "./pendingWorkspace";
 import { fromSavedWorkspace, toSavedWorkspace, workspaceJson, type RestoredWorkspace } from "./workspaceFile";
@@ -37,16 +39,28 @@ interface Serialized {
   json: string;
 }
 
-/** The whole workspace as `workspace.json` holds it; the editors' text is read now. */
+/** A tab's staged edits as the file keeps them (`savedEdits`: null beyond the limits, B40). */
+const stagedFile = (tabId: string) => savedEdits(readStagedEdits(tabId)).file;
+
+/** The whole workspace as `workspace.json` holds it; the editors' text and the staged edits are read now. */
 function serialize(): Serialized {
   const { tabs, activeTabId, sidebarTab } = useWorkspace.getState();
-  forgetEditorBuffersExcept(new Set(tabs.map((tab) => tab.id)));
-  const file = toSavedWorkspace({ tabs, activeTabId, sidebarTab }, readEditorBuffer);
+  const open = new Set(tabs.map((tab) => tab.id));
+  forgetEditorBuffersExcept(open);
+  forgetStagedEditsExcept(open);
+  const file = toSavedWorkspace({ tabs, activeTabId, sidebarTab }, readEditorBuffer, stagedFile);
   return { file, json: workspaceJson(file) };
 }
 
 /** A restored state's JSON, before any editor is mounted (comparable whatever it was restored from). */
-const snapshotJson = (snapshot: WorkspaceSnapshot) => workspaceJson(toSavedWorkspace(snapshot, () => null));
+const snapshotJson = ({ snapshot, staged }: RestoredWorkspace) =>
+  workspaceJson(
+    toSavedWorkspace(
+      snapshot,
+      () => null,
+      (tabId) => staged.get(tabId) ?? null,
+    ),
+  );
 
 /**
  * The one workspace writer (spec §4.8, rulings R-02c-4 and R-02c-7). Every store mutation and editor change
@@ -87,6 +101,7 @@ class WorkspaceWriter {
       if (changed) this.schedule();
     });
     onEdited(this.schedule);
+    onStagedEditsChanged(this.schedule);
     window.addEventListener("pagehide", this.flush);
     window.addEventListener("beforeunload", this.flush);
     this.schedule(); // saves only if the state differs from `lastSaved`
@@ -301,12 +316,14 @@ async function restoreSaved(queryClient: QueryClient): Promise<WriterStart> {
   const profileExists = (id: string) => !profileIds || profileIds.has(id);
   const fromFile = saved && fromSavedWorkspace(saved, profileExists);
   let fromPending = pending && fromSavedWorkspace(pending.file, profileExists);
-  if (fromPending && fromFile && snapshotJson(fromPending.snapshot) === snapshotJson(fromFile.snapshot)) {
+  if (fromPending && fromFile && snapshotJson(fromPending) === snapshotJson(fromFile)) {
     clearPendingWorkspace(); // the backend has it after all
     fromPending = null;
   }
   const restored = fromPending ?? fromFile;
   if (!restored) throw new Error("Nothing to restore"); // unreachable: no file means there is a local copy
+  // Before the tabs: a table tab finds its staged edits when it mounts (spec ⑤ §3.7).
+  for (const [tabId, file] of restored.staged) putStagedEdits(tabId, restoredEdits(file));
   useWorkspace.getState().restore(restored.snapshot);
   const source = fromPending ? "the unsaved local copy" : "workspace.json";
   const { tabs } = restored.snapshot;
```

- [ ] **Step 9: Run everything**

Run: `npx vitest run && npm run typecheck && npm run build && cd backend && .venv/bin/pytest -q`
Expected: PASS (the full backend suite with the dev containers up); no errors.

- [ ] **Step 10: Commit**

```bash
git add backend/dbaguy_backend/domain/edits.py backend/dbaguy_backend/domain/workspace.py \
  backend/dbaguy_backend/services/workspace_service.py backend/tests/test_local_stores.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts \
  src/features/edits/savedEdits.ts src/features/edits/savedEdits.test.ts \
  src/features/workspace/workspaceFile.ts src/features/workspace/workspaceFile.test.ts src/features/workspace/workspaceSync.ts
git commit -m "feat(workspace): a table tab's staged grid edits in workspace.json — bounded, repaired not refused, restored by key"
```

---

### Task 5: The session contract and the commit's logic — `apply_edits`, `edit_outcome`, the outcome read as R-05-6 says

**Suggested implementer:** Opus.

**Files:**
- Modify: `src/features/session/execution.ts`, `src/features/session/SessionController.ts`
- Create: `src/features/edits/editsApi.ts`, `src/features/edits/commitFlow.ts`
- Test: `src/features/session/execution.test.ts`, `src/features/edits/commitFlow.test.ts`

**Interfaces:**
- Consumes: Task 2 (`findChange`, `AppliedOutcome`, `StagedProblem`, `Staging`); `ApiError`, `api`, `unwrap`, `errorMessage` (`@/lib/api/client`); `EditOutcome`, `EditOutcomeEvent`, `EditPreview`, `EditTarget`, `EditChanges`, `PathSegment`, `pathKey`; `plainValue`; `plural`.
- Produces:
  - `execution.ts`: `ExecutionKind` adds `"edits"`; `ExecutionState.editOutcome: EditOutcomeEvent | null`; `applyEvent` folds `edit_outcome`, and a terminal error code (`invalid_command`, `no_open_result`) no longer ends an `edits` request (its `edit_outcome` follows, 05a M6; ruling H21); `firstPageDelivered(execution: ExecutionState | null): boolean` (the first result set's first page is in: it ended, paused, or the request is over — Task 6's no-flash re-run).
  - `SessionController`: `MAX_PAGE_SIZE = 10_000`; `applyEdits(planId: string): string` (`track("edits", "", {type: "apply_edits", request_id, plan_id})`; `busy()` covers it).
  - `editsApi.ts`: `editTargetKey(profileId, path)`; `fetchEditTarget(profileId, path, signal?)`; `previewEdits(profileId, path, changes)`; `editsErrorMessage(error): string` (a 422's `detail[0].msg` when present, else the message: 05a M5).
  - `commitFlow.ts`: `EndedCommit { kind: "ended"; outcome; event: EditOutcomeEvent | null; error; messages; cancelled }`; `type CommitState = { kind: "running"; done } | { kind: "not_sent" } | EndedCommit`; `commitState(execution)`; `stagedOutcome(commit): AppliedOutcome`; `rerunAfter(outcome, browseWasPaused): boolean`; `type CommitAction = "show_in_grid" | "preview_again" | "rollback" | "reconnect" | "refresh"`; `CommitReport { tone; title; detail; notes; actions; failedChange; failedStatement }`; `commitReport(commit, { total, sqlModeRefusals })`; `NOT_SENT_REPORT`; `commitToast(event, table, inserts)`; `problemsOf(preview)`; `changeLabel(staged, changeId)`; `isSqliteBusy(code)`; texts `UNKNOWN_NOTE`, `LOST_TEXT`, `TRANSACTION_OPEN_NOTE`, `SQL_MODE_NOTE`, `SQLITE_BUSY_NOTE`, `PARTIAL_NOTE`, `UNKNOWN_BEFORE`.

Behaviour (spec R-05-6, R-05-7, §3.6.5, the 05a appendix and carries; rulings H21, H24–H27):
- `commitState`: running until `finished`; then the `edit_outcome` decides; ended without one is **unknown** (`event: null`), unless the command never left (`not_connected`: "not sent", nothing happened). An outcome that arrived before the socket dropped stands.
- Re-run after it (§3.6.5): committed and partial always; rolled back only when the apply ended a paused browse; unknown never (the user's Refresh).
- The report: committed → "Committed N changes" (+ "N rows were already gone"); partial → "Partly applied: a of N changes took effect" + PARTIAL_NOTE; unknown → "Outcome unknown", the backend's text or LOST_TEXT, UNKNOWN_NOTE, Refresh; rolled back → "Nothing was applied" (or "Cancelled: nothing was applied") with the server's text, Show in grid when a change failed, Preview again for `plan_expired` / `plan_mismatch`, Roll back + Reconnect with TRANSACTION_OPEN_NOTE for `transaction_open` (05a L12 carry), Preview again for a first `sql_mode_changed` and Reconnect with SQL_MODE_NOTE once it repeats (carry), SQLITE_BUSY_NOTE for `SQLITE_BUSY*` / `SQLITE_LOCKED*` (05a M4 carry).
- `editsErrorMessage` reads `detail?.[0]?.msg ?? message` for a 422 on `/edits/*` (05a carry).

- [ ] **Step 1: Write the failing tests**

`src/features/session/execution.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { ServerEvent } from "@/lib/api/types";
import { applyEvent, firstPageDelivered, interruptExecution, newExecution, type ExecutionState } from "./execution";

/* Folding server events (spec ⑤ §3.5, 05a M6): an apply's outcome, a refused apply, a re-run's first page (A12). */

const fold = (state: ExecutionState, events: ServerEvent[]) => events.reduce(applyEvent, state);
const R = "r1";

describe("an apply request", () => {
  it("keeps its edit_outcome", () => {
    const state = fold(newExecution(R, "edits", ""), [
      { type: "started", request_id: R },
      { type: "edit_outcome", request_id: R, outcome: "committed", applied: ["u:1"], already_gone: [] },
      { type: "finished", request_id: R, elapsed_ms: 3, cancelled: false, timed_out: false, skipped: false },
    ] as ServerEvent[]);
    expect(state.editOutcome?.outcome).toBe("committed");
    expect(state.status).toBe("done");
  });

  it("is not over at a terminal error: a refusal at the WebSocket layer still sends its outcome", () => {
    const refused = fold(newExecution(R, "edits", ""), [
      { type: "error", request_id: R, message: "Invalid command", code: "invalid_command" },
    ] as ServerEvent[]);
    expect(refused.status).toBe("running");
    const query = fold(newExecution(R, "execute", "SELECT 1"), [
      { type: "error", request_id: R, message: "Invalid command", code: "invalid_command" },
    ] as ServerEvent[]);
    expect(query.status).toBe("done"); // other requests as before
  });
});

describe("a re-run's first page", () => {
  const started = fold(newExecution(R, "execute", "SELECT"), [
    { type: "started", request_id: R },
    { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" },
    { type: "rows", request_id: R, index: 0, rows: [[1]] },
  ] as ServerEvent[]);

  it("is not delivered while its rows still arrive", () => {
    expect(firstPageDelivered(null)).toBe(false);
    expect(firstPageDelivered(newExecution(R, "execute", "SELECT"))).toBe(false);
    expect(firstPageDelivered(started)).toBe(false);
  });

  it("is delivered once the page ends, pauses, or the request is over", () => {
    const paused = applyEvent(started, {
      type: "result_set_end",
      request_id: R,
      index: 0,
      row_count: 1,
      has_more: true,
    } as ServerEvent);
    expect(firstPageDelivered(paused)).toBe(true);
    const empty = fold(newExecution(R, "execute", "SELECT"), [
      { type: "result_set", request_id: R, index: 0, columns: [], presentation: "grid" },
      { type: "result_set_end", request_id: R, index: 0, row_count: 0, has_more: false },
    ] as ServerEvent[]);
    expect(firstPageDelivered(empty)).toBe(false);
    expect(firstPageDelivered(interruptExecution(empty))).toBe(true);
  });
});
```

`src/features/edits/commitFlow.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { EditOutcomeEvent, EditPreview, ServerEvent } from "@/lib/api/types";
import { applyEvent, interruptExecution, newExecution, type ExecutionState } from "../session/execution";
import {
  changeLabel,
  commitReport,
  commitState,
  commitToast,
  LOST_TEXT,
  problemsOf,
  rerunAfter,
  SQL_MODE_NOTE,
  SQLITE_BUSY_NOTE,
  stagedOutcome,
  TRANSACTION_OPEN_NOTE,
  type EndedCommit,
} from "./commitFlow";
import { editsErrorMessage } from "./editsApi";
import { emptyStaged, rowKeyFromKey, stage } from "./staging";

/*
 * An apply's outcome as the frontend reads it (spec ⑤ R-05-6, the 05a appendix's event shapes): exactly one
 * edit_outcome; an apply that ends without one is unknown and never resent; a command that never left is not sent.
 */

const R = "apply";
const apply = (events: object[]): ExecutionState =>
  events.reduce<ExecutionState>(
    (state, event) => applyEvent(state, { request_id: R, ...event } as ServerEvent),
    newExecution(R, "edits", ""),
  );
const finished = (cancelled = false) => ({
  type: "finished",
  elapsed_ms: 5,
  cancelled,
  timed_out: false,
  skipped: false,
});
const outcome = (value: string, extra: object = {}) => ({
  type: "edit_outcome",
  outcome: value,
  applied: [],
  already_gone: [],
  ...extra,
});
const endedOf = (execution: ExecutionState) => commitState(execution) as EndedCommit;

describe("reading an apply's events", () => {
  it("counts the statements while it runs", () => {
    const running = apply([
      { type: "started" },
      { type: "affected", index: 0, rows: 1 },
      { type: "affected", index: 1, rows: 1 },
    ]);
    expect(commitState(running)).toEqual({ kind: "running", done: 2 });
  });

  it("reads a commit", () => {
    const done = apply([
      { type: "started" },
      { type: "affected", index: 0, rows: 1 },
      outcome("committed", { applied: ["u:1"], already_gone: ["d:2"] }),
      { type: "session_state", autocommit: true, in_transaction: false, transaction_failed: false },
      finished(),
    ]);
    expect(commitState(done)).toMatchObject({ kind: "ended", outcome: "committed" });
    expect(stagedOutcome(endedOf(done))).toEqual({
      outcome: "committed",
      applied: ["u:1"],
      failedChange: null,
      message: null,
    });
  });

  it("reads a rollback with the failing statement and the server's text", () => {
    const failed = apply([
      { type: "started" },
      { type: "error", message: "Row id = 7 was changed or deleted since it was loaded", code: "edit_conflict" },
      outcome("rolled_back", { failed_change: "u:1", statement: 0 }),
      finished(),
    ]);
    const commit = endedOf(failed);
    expect(stagedOutcome(commit)).toEqual({
      outcome: "rolled_back",
      applied: [],
      failedChange: "u:1",
      message: "Row id = 7 was changed or deleted since it was loaded",
    });
    expect(commitReport(commit, { total: 3, sqlModeRefusals: 0 })).toMatchObject({
      tone: "danger",
      title: "Nothing was applied",
      failedStatement: 0,
      actions: ["show_in_grid"],
    });
  });

  it("reads a refusal at the WebSocket layer as rolled back, not unknown", () => {
    const refused = apply([
      { type: "error", message: "Busy", code: "invalid_command" },
      outcome("rolled_back"),
      finished(),
    ]);
    expect(commitState(refused)).toMatchObject({ kind: "ended", outcome: "rolled_back" });
  });

  it("reads an apply cancelled while queued as rolled back and cancelled", () => {
    const queued = apply([
      { type: "started" },
      { type: "message", severity: "warning", text: "Query cancelled before it started" },
      outcome("rolled_back"),
      finished(true),
    ]);
    expect(commitReport(endedOf(queued), { total: 1, sqlModeRefusals: 0 }).title).toBe(
      "Cancelled: nothing was applied",
    );
  });

  it("calls an apply that ended without its outcome unknown", () => {
    const lost = interruptExecution(apply([{ type: "started" }, { type: "affected", index: 0, rows: 1 }]));
    const commit = endedOf(lost);
    expect(commit).toMatchObject({ kind: "ended", outcome: "unknown", event: null });
    expect(commitReport(commit, { total: 2, sqlModeRefusals: 0 })).toMatchObject({
      title: "Outcome unknown",
      detail: LOST_TEXT,
      actions: ["refresh"],
    });
  });

  it("keeps an outcome that arrived before the socket went", () => {
    const late = interruptExecution(apply([{ type: "started" }, outcome("committed", { applied: ["u:1"] })]));
    expect(commitState(late)).toMatchObject({ outcome: "committed" });
  });

  it("calls a command that never left not sent", () => {
    const never = {
      ...newExecution(R, "edits", ""),
      status: "done" as const,
      error: { message: "Not connected", code: "not_connected" },
    };
    expect(commitState(never)).toEqual({ kind: "not_sent" });
  });
});

describe("what the result says", () => {
  const rolledBack = (code: string) =>
    endedOf(apply([{ type: "error", message: "refused", code }, outcome("rolled_back"), finished()]));

  it("offers Roll back and Reconnect for an open transaction", () => {
    expect(commitReport(rolledBack("transaction_open"), { total: 1, sqlModeRefusals: 0 })).toMatchObject({
      notes: [TRANSACTION_OPEN_NOTE],
      actions: ["rollback", "reconnect"],
    });
  });

  it("offers a new preview for an expired plan, and for a first sql_mode refusal; Reconnect once it repeats", () => {
    expect(commitReport(rolledBack("plan_expired"), { total: 1, sqlModeRefusals: 0 }).actions).toEqual([
      "preview_again",
    ]);
    expect(commitReport(rolledBack("sql_mode_changed"), { total: 1, sqlModeRefusals: 1 }).actions).toEqual([
      "preview_again",
    ]);
    expect(commitReport(rolledBack("sql_mode_changed"), { total: 1, sqlModeRefusals: 2 })).toMatchObject({
      notes: [SQL_MODE_NOTE],
      actions: ["reconnect"],
    });
  });

  it("explains a busy SQLite file", () => {
    expect(commitReport(rolledBack("SQLITE_BUSY"), { total: 1, sqlModeRefusals: 0 }).notes).toEqual([SQLITE_BUSY_NOTE]);
  });

  it("counts a partial outcome", () => {
    const partial = endedOf(
      apply([outcome("partial", { applied: ["d:1"], failed_change: "u:2", statement: 1 }), finished()]),
    );
    expect(commitReport(partial, { total: 3, sqlModeRefusals: 0 }).title).toBe(
      "Partly applied: 1 of 3 changes took effect",
    );
  });

  it("toasts a commit with the rows already gone and where new rows went", () => {
    const event: EditOutcomeEvent = {
      type: "edit_outcome",
      request_id: R,
      outcome: "committed",
      applied: ["u:1", "d:2", "i:3"],
      already_gone: ["d:2"],
    };
    expect(commitToast(event, "public.orders", 0)).toBe(
      "Committed 3 changes to public.orders · 1 row was already gone",
    );
    expect(commitToast(event, "orders", 1)).toMatch(/New rows appear where the table's order puts them/);
  });

  it("runs the browse again after a commit or a partial one, after a rollback only when it was paused", () => {
    expect([rerunAfter("committed", false), rerunAfter("partial", false), rerunAfter("unknown", true)]).toEqual([
      true,
      true,
      false,
    ]);
    expect([rerunAfter("rolled_back", true), rerunAfter("rolled_back", false)]).toEqual([true, false]);
  });
});

describe("a preview's problems and the changes they name", () => {
  it("groups the problems by change, a key collision on the change as a whole", () => {
    const preview = {
      problems: [
        { change_id: "u:1", column: "n", code: "invalid_number", message: "Not a number" },
        { change_id: "u:1", column: null, code: "key_collision", message: "This change moves a row onto the key…" },
      ],
    } as EditPreview;
    expect(problemsOf(preview)).toEqual({
      "u:1": [
        { column: "n", message: "Not a number" },
        { column: null, message: "This change moves a row onto the key…" },
      ],
    });
  });

  it("names a change by its key, or as a new row", () => {
    const staged = stage(
      stage(
        emptyStaged(["id"]),
        {
          type: "deleteRows",
          rows: [
            { kind: "row", rowKey: rowKeyFromKey({ id: "9007199254740993" }, ["id"]), key: { id: "9007199254740993" } },
          ],
        },
        () => "d:1",
      ),
      { type: "addRow", id: "i:2" },
    );
    expect(changeLabel(staged, "d:1")).toBe("Row id = 9007199254740993");
    expect(changeLabel(staged, "i:2")).toBe("New row");
  });
});

describe("the text of a failed /edits request", () => {
  it("is a 422's first detail when there is one, else its message", () => {
    const detail = [
      {
        type: "commit_too_large",
        loc: ["body", "changes"],
        msg: "The changes are too large: at most 64 MiB of values per commit",
      },
    ];
    expect(editsErrorMessage(new ApiError("Invalid request", "validation_failed", 422, detail))).toBe(detail[0].msg);
    expect(editsErrorMessage(new ApiError("Not a grid cell: …", "validation_failed", 422))).toBe("Not a grid cell: …");
    expect(editsErrorMessage(new ApiError("This connection is read-only", "read_only", 403))).toBe(
      "This connection is read-only",
    );
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npx vitest run src/features/session src/features/edits/commitFlow.test.ts`
Expected: FAIL (`firstPageDelivered` is not exported; cannot resolve `./commitFlow`, `./editsApi`).

- [ ] **Step 3: The execution state and the controller**

```diff
diff --git a/src/features/session/execution.ts b/src/features/session/execution.ts
index b091b2e..b3390f5 100644
--- a/src/features/session/execution.ts
+++ b/src/features/session/execution.ts
@@ -1,13 +1,15 @@
 import type {
   Classification,
   ColumnMeta,
+  EditOutcomeEvent,
   GuardReason,
   QueryContext,
   ServerEvent,
   StatementParams,
 } from "@/lib/api/types";
 
-export type ExecutionKind = "execute" | "export" | "transaction";
+/** `edits`: a grid-edit plan applied (`apply_edits`, spec ⑤ §3.5); it ends with its `editOutcome`. */
+export type ExecutionKind = "execute" | "export" | "transaction" | "edits";
 
 /**
  * What a request is sent with besides its statement (Couchbase): the query context and the typed parameters.
@@ -58,12 +60,18 @@ export interface ExecutionState {
   skipped: boolean;
   /** The connection dropped mid-request: later events are ignored because some were lost. */
   interrupted: boolean;
+  /** An `edits` request's outcome (exactly one per apply, 05a E1); null until it arrives, and for other kinds. */
+  editOutcome: EditOutcomeEvent | null;
 }
 
 export const INTERRUPTED_MESSAGE = "Connection interrupted — results may be incomplete";
 /** Error code of a statement whose database connection dropped: the session must be reopened (retried). */
 export const CONNECTION_LOST = "connection_lost";
-/** Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. */
+/**
+ * Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. Not for
+ * an `edits` request: a refused apply still sends its `edit_outcome` after the error (05a M6), and an apply that ended
+ * without one would read as an unknown outcome (ruling H21).
+ */
 const TERMINAL_ERROR_CODES = new Set(["no_open_result", "invalid_command"]);
 
 export function newExecution(
@@ -92,6 +100,7 @@ export function newExecution(
     timedOut: false,
     skipped: false,
     interrupted: false,
+    editOutcome: null,
   };
 }
 
@@ -117,6 +126,15 @@ function updateSet(state: ExecutionState, index: number, fn: (set: ResultSetStat
   return { ...state, resultSets: state.resultSets.map((s) => (s.index === index ? fn(s) : s)) };
 }
 
+/**
+ * The request's first result set has all of its first page (the page ended, it paused, or the request is over): a
+ * re-run replaces the rows shown only then, so the grid never flashes empty and keeps its scroll position (A12).
+ */
+export function firstPageDelivered(execution: ExecutionState | null): boolean {
+  const set = execution?.resultSets[0];
+  return !!execution && !!set && (set.rowCount > 0 || set.hasMore || execution.status !== "running");
+}
+
 /** Folds one server event of this execution into its state. `session_state` is session-wide and never applies. */
 export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState {
   if (state.interrupted || event.type === "session_state") return state;
@@ -151,13 +169,15 @@ export function applyEvent(state: ExecutionState, event: ServerEvent): Execution
       return { ...state, messages: [...state.messages, { severity: event.severity, text: event.text }] };
     case "error": {
       const error = { message: event.message, code: event.code, position: event.position };
-      const terminal = event.code != null && TERMINAL_ERROR_CODES.has(event.code);
+      const terminal = event.code != null && TERMINAL_ERROR_CODES.has(event.code) && state.kind !== "edits";
       return terminal ? { ...state, error, status: "done" } : { ...state, error };
     }
     case "confirmation_required":
       return { ...state, confirmation: { reasons: event.reasons, classification: event.classification } };
     case "progress":
       return { ...state, progressRows: event.rows };
+    case "edit_outcome":
+      return { ...state, editOutcome: event };
     case "finished":
       return {
         ...state,
```

```diff
diff --git a/src/features/session/SessionController.ts b/src/features/session/SessionController.ts
index afc528f..0a13bd4 100644
--- a/src/features/session/SessionController.ts
+++ b/src/features/session/SessionController.ts
@@ -74,6 +74,8 @@ export interface SessionControllerOptions {
 }
 
 export const PAGE_SIZE = 500;
+/** The largest page a request may ask for (`ExecuteCommand.page_size`): a re-run reloads at most this many rows. */
+export const MAX_PAGE_SIZE = 10_000;
 const MAX_SESSION_MESSAGES = 50;
 const SESSION_GONE = "The session is no longer available.";
 const NOT_CONNECTED: ExecutionState["error"] = { message: "Not connected", code: "not_connected" };
@@ -216,6 +218,15 @@ export class SessionController {
     return this.track("transaction", "", { type: "set_autocommit", request_id: crypto.randomUUID(), enabled });
   }
 
+  /**
+   * Applies a previewed grid-edit plan (spec ⑤ §3.5): its statements as previewed, in one transaction. The execution
+   * ends with its `editOutcome`; one that ends without (the socket or the backend went away) is an unknown outcome, and
+   * is never sent again. Returns the request id.
+   */
+  applyEdits(planId: string): string {
+    return this.track("edits", "", { type: "apply_edits", request_id: crypto.randomUUID(), plan_id: planId });
+  }
+
   /**
    * Runs `sql` again and writes its whole result to `path` on the backend; `progressRows` reports the rows written.
    * `request` is what the statement was run with (an execution's `request`): the same context and parameters.
```

- [ ] **Step 4: Write `src/features/edits/editsApi.ts`**

```ts
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import { pathKey, type EditChanges, type EditPreview, type EditTarget, type PathSegment } from "@/lib/api/types";

/*
 * The grid-edit API (spec ⑤ §3.4, the 05a contract): a table's edit target, and the preview of a commit. A preview
 * writes nothing (it keeps a one-time plan in the backend's memory), but it is a plain call, never a `useQuery`: each
 * one makes a new plan, and only the one shown in the dialog is ever applied (05a M7). Nothing here logs a value or a key.
 */

/** The query key of a table's edit target: read once per table tab, again on Refresh. */
export const editTargetKey = (profileId: string, path: PathSegment[]) =>
  ["edits", "target", profileId, pathKey(path)] as const;

/** How the table's rows are identified and written; `editable` false with the reason the edit bar shows. */
export async function fetchEditTarget(
  profileId: string,
  path: PathSegment[],
  signal?: AbortSignal,
): Promise<EditTarget> {
  return unwrap(await (await api()).POST("/edits/target", { body: { profile_id: profileId, path }, signal }));
}

/** The exact statements a commit of `changes` sends, and a plan id when nothing is wrong with them. */
export async function previewEdits(profileId: string, path: PathSegment[], changes: EditChanges): Promise<EditPreview> {
  return unwrap(await (await api()).POST("/edits/preview", { body: { profile_id: profileId, path, changes } }));
}

/**
 * What to show for a failed `/edits/*` request (05a M5): a 422 with `detail` has its reason in `detail[0].msg` (the
 * 64 MiB limit, a refused request shape) while its message is the generic "Invalid request"; any other failure's
 * message is its own.
 */
export function editsErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 422 && Array.isArray(error.detail)) {
    const first: unknown = error.detail[0];
    if (first && typeof first === "object" && typeof (first as { msg?: unknown }).msg === "string") {
      const msg = (first as { msg: string }).msg;
      if (msg) return msg;
    }
  }
  return errorMessage(error);
}
```

- [ ] **Step 5: Write `src/features/edits/commitFlow.ts`**

```ts
import type { EditOutcome, EditOutcomeEvent, EditPreview } from "@/lib/api/types";
import { plainValue } from "@/lib/cells";
import { plural } from "@/lib/format";
import type { ExecutionState } from "../session/execution";
import { findChange, type AppliedOutcome, type StagedProblem, type Staging } from "./staging";

/*
 * How a commit stands and what the dialog and the toast say about it (spec ⑤ R-05-6, §3.6.5, the 05a appendix), as pure
 * functions of the apply request's execution. Nothing is ever sent again: an apply whose outcome did not arrive is
 * unknown, and only the user's Refresh (fresh rows) lets a commit start again.
 */

/** An apply that is over, with what came back. */
export interface EndedCommit {
  readonly kind: "ended";
  readonly outcome: EditOutcome;
  /** The backend's `edit_outcome`; null when the request ended without one (then `outcome` is "unknown"). */
  readonly event: EditOutcomeEvent | null;
  readonly error: ExecutionState["error"];
  /** A trigger's NOTICE / PRINT, warnings (05a: "COMMIT reported an error after every statement…"). */
  readonly messages: ExecutionState["messages"];
  readonly cancelled: boolean;
}

export type CommitState =
  /** Statements whose rows have been counted so far (`affected`). */
  | { readonly kind: "running"; readonly done: number }
  /** The command never left: the session was not connected (`not_connected`), so nothing happened. */
  | { readonly kind: "not_sent" }
  | EndedCommit;

const ended = (execution: ExecutionState, outcome: EditOutcome, event: EditOutcomeEvent | null): EndedCommit => ({
  kind: "ended",
  outcome,
  event,
  error: execution.error,
  messages: execution.messages,
  cancelled: execution.cancelled,
});

/** Where an apply request stands (R-05-6: an apply that ended without `edit_outcome` is unknown, never resent). */
export function commitState(execution: ExecutionState): CommitState {
  if (execution.status !== "done") return { kind: "running", done: execution.affected.length };
  if (execution.editOutcome) return ended(execution, execution.editOutcome.outcome, execution.editOutcome);
  if (execution.error?.code === "not_connected") return { kind: "not_sent" };
  return ended(execution, "unknown", null);
}

/** What the staging takes from an ended apply. */
export function stagedOutcome(commit: EndedCommit): AppliedOutcome {
  return {
    outcome: commit.outcome,
    applied: commit.event?.applied ?? [],
    failedChange: commit.event?.failed_change ?? null,
    message: commit.error?.message ?? null,
  };
}

/**
 * Whether the browse is run again after it (§3.6.5): always after a commit or a partial one; after a rollback only when
 * the apply ended a paused result (its rows stopped loading); never after an unknown one (the user's Refresh does it).
 */
export function rerunAfter(outcome: EditOutcome, browseWasPaused: boolean): boolean {
  if (outcome === "committed" || outcome === "partial") return true;
  return outcome === "rolled_back" && browseWasPaused;
}

/** What the result dialog offers besides Close. */
export type CommitAction = "show_in_grid" | "preview_again" | "rollback" | "reconnect" | "refresh";

export interface CommitReport {
  readonly tone: "success" | "warning" | "danger";
  readonly title: string;
  /** The server's text (or DbaGuy's fixed one) about what failed. */
  readonly detail: string | null;
  /** Why it happened and what to do. */
  readonly notes: readonly string[];
  readonly actions: readonly CommitAction[];
  readonly failedChange: string | null;
  /** The failed statement's index in the preview's `statements`. */
  readonly failedStatement: number | null;
}

export const UNKNOWN_NOTE =
  "The changes may or may not have been applied. Refresh to see the table as it is now: Commit is off until then, and nothing is sent again by itself.";
export const LOST_TEXT = "The connection was lost while committing: the changes may or may not have been applied.";
export const TRANSACTION_OPEN_NOTE =
  "This tab's session has a transaction open (for example from SQL Server's IMPLICIT_TRANSACTIONS login option): roll it back or reconnect, then preview again.";
export const SQL_MODE_NOTE =
  "This tab's connection and the connection DbaGuy reads the table with treat backslashes differently: the server's sql_mode changed while they were open. Reconnect this tab and preview again; setting sql_mode in the connection's options keeps it from happening.";
export const SQLITE_BUSY_NOTE =
  "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then commit again.";
export const PARTIAL_NOTE = "This table cannot roll back: what took effect is no longer staged, the rest still is.";

/** The command never left (the session was not connected): nothing happened, so nothing changes in the staging. */
export const NOT_SENT_REPORT: CommitReport = {
  tone: "danger",
  title: "Not sent",
  detail: "This tab is not connected: nothing was sent. Reconnect, then commit again.",
  notes: [],
  actions: ["reconnect"],
  failedChange: null,
  failedStatement: null,
};

/** The preview's line after an unknown outcome (R-05-6): what committing again does. */
export const UNKNOWN_BEFORE =
  "The last commit's outcome was unknown: an UPDATE or DELETE checks the loaded values again, but a new row would be inserted a second time.";

/** SQLite's busy and locked errors (`sqlite_errorname`): another connection holds the file. */
export const isSqliteBusy = (code: string | null | undefined): boolean =>
  !!code && (code.startsWith("SQLITE_BUSY") || code.startsWith("SQLITE_LOCKED"));

/** The dialog's account of an ended apply; `sqlModeRefusals` counts this tab's `sql_mode_changed` refusals in a row. */
export function commitReport(commit: EndedCommit, context: { total: number; sqlModeRefusals: number }): CommitReport {
  const failedChange = commit.event?.failed_change ?? null;
  const failedStatement = commit.event?.statement ?? null;
  const code = commit.error?.code ?? null;
  const applied = commit.event?.applied.length ?? 0;
  const base = { failedChange, failedStatement, detail: commit.error?.message ?? null };
  const showable: CommitAction[] = failedChange ? ["show_in_grid"] : [];
  switch (commit.outcome) {
    case "committed": {
      const gone = commit.event?.already_gone.length ?? 0;
      const notes = gone ? [`${plural(gone, "row")} ${gone === 1 ? "was" : "were"} already gone`] : [];
      return { ...base, tone: "success", title: `Committed ${plural(applied, "change")}`, notes, actions: [] };
    }
    case "partial":
      return {
        ...base,
        tone: "warning",
        title: `Partly applied: ${applied} of ${plural(context.total, "change")} took effect`,
        notes: [PARTIAL_NOTE],
        actions: showable,
      };
    case "unknown":
      return {
        ...base,
        tone: "warning",
        title: "Outcome unknown",
        detail: commit.event ? base.detail : LOST_TEXT,
        notes: [UNKNOWN_NOTE],
        actions: ["refresh"],
      };
    case "rolled_back": {
      const notes: string[] = [];
      const actions = [...showable];
      if (code === "plan_expired" || code === "plan_mismatch") actions.push("preview_again");
      if (code === "transaction_open") {
        notes.push(TRANSACTION_OPEN_NOTE);
        actions.push("rollback", "reconnect");
      }
      if (code === "sql_mode_changed") {
        if (context.sqlModeRefusals >= 2) {
          notes.push(SQL_MODE_NOTE);
          actions.push("reconnect");
        } else actions.push("preview_again");
      }
      if (isSqliteBusy(code)) notes.push(SQLITE_BUSY_NOTE);
      const title = commit.cancelled ? "Cancelled: nothing was applied" : "Nothing was applied";
      return { ...base, tone: "danger", title, notes, actions };
    }
  }
}

/** The toast of a commit (§3.6.5): how many, rows already gone, and where new rows went. */
export function commitToast(event: EditOutcomeEvent, table: string, inserts: number): string {
  const gone = event.already_gone.length;
  const parts = [`Committed ${plural(event.applied.length, "change")} to ${table}`];
  if (gone) parts.push(`${plural(gone, "row")} ${gone === 1 ? "was" : "were"} already gone`);
  const text = parts.join(" · ");
  return inserts
    ? `${text}. New rows appear where the table's order puts them, which may be past the rows loaded`
    : text;
}

/** A preview's problems by change, as the staging keeps them (they mark the cells). */
export function problemsOf(preview: EditPreview): Record<string, StagedProblem[]> {
  const problems: Record<string, StagedProblem[]> = {};
  for (const problem of preview.problems) {
    (problems[problem.change_id] ??= []).push({ column: problem.column ?? null, message: problem.message });
  }
  return problems;
}

/** A change as the dialog names it: "New row", or its key ("id = 7"); the key is the user's data, never logged. */
export function changeLabel(staged: Staging, changeId: string): string {
  const found = findChange(staged, changeId);
  if (!found) return "A change";
  if (found.kind === "insert") return "New row";
  const key = Object.entries(found.change.key).map(([column, cell]) => `${column} = ${plainValue(cell)}`);
  return `Row ${key.join(", ")}`;
}
```

- [ ] **Step 6: Run the tests, the types and the build**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: PASS; no errors.

- [ ] **Step 7: Commit**

```bash
git add src/features/session/execution.ts src/features/session/execution.test.ts src/features/session/SessionController.ts \
  src/features/edits/editsApi.ts src/features/edits/commitFlow.ts src/features/edits/commitFlow.test.ts
git commit -m "feat(app): apply_edits on the session and its outcome read honestly — no outcome is unknown, never resent"
```

---

### Task 6: Refresh keeps its place, and the scratch tables every later check uses

**Suggested implementer:** Sonnet.

**Files:**
- Create: `scripts/scratch_tables.py`
- Modify: `src/features/table-data/useTableData.ts`, `src/features/grid/DataGrid.tsx`, `src/features/table-data/TableDataTab.tsx`

**Interfaces:**
- Consumes: Task 5 (`firstPageDelivered`, `MAX_PAGE_SIZE`); `useEngineInfo`.
- Produces:
  - `useTableData(tab, active)` also returns `position: number` (changes only when the grid should go back to the top) and `deliveredRequest: string | null` (the browse request whose first page is in; Task 10 waits for another one after an unknown outcome); `refresh()` keeps the position on a `row_editing` engine; `keptPageSize(loaded: number): number` (exported).
  - `DataGrid` prop `resetScrollOn?: unknown` (default: the column list, as before).
  - `scripts/scratch_tables.py`: `create [ENGINE…]`, `drop [ENGINE…]`, `check`, `sql ENGINE "STATEMENT"`, `hold ENGINE SECONDS "STATEMENT"` (ENGINE: `pg`, `mysql`, `mssql`, `sqlite`).

Behaviour (D-05-12, CR-1, A12; rulings H9, H16):
- Refresh on a table tab of an engine with `row_editing` re-runs the browse with a first page as large as the rows loaded now (at least 500, at most 10,000) and keeps the vertical scroll and the selection (the same column signature); a sort, a filter or the first browse start at the top, as does any result with other columns. Other engines and every query tab behave as before.
- The grid keeps showing the previous rows (dimmed after 150 ms, "Loading…") until the new request's first page has fully arrived — not at its first `rows` event — so a re-run never shows an empty grid and the browser never clamps the scroll to 0 (A12).
- The helper's catalogue (exact names; views dropped first; `create` drops before it creates): PostgreSQL `public.zz_05b_items` (1,200 rows: identity id, bigint, varchar, text, numeric(12,2), timestamptz NOT NULL DEFAULT now(), boolean, jsonb, bytea, varchar NOT NULL DEFAULT, a STORED generated column), `public.zz_05b_view`, `public.zz_05b_nokey`, `public.zz_05b_wide` (100,000 rows × 30 columns); MySQL `zz_05b_items` (InnoDB, the same shape, MEDIUMTEXT note, TINYINT(1) flag, JSON, VARBINARY), `zz_05b_view`, `zz_05b_nokey`, `zz_05b_myisam` (MyISAM, a UNIQUE label: rows (1,a) (2,b) (3,c)); SQL Server `dbo.zz_05b_items` (IDENTITY id, nvarchar, varchar NOT NULL DEFAULT, a computed column), `dbo.zz_05b_view`, `dbo.zz_05b_nokey`; SQLite (file `dev/data/scratch/zz_05b.sqlite`) `zz_05b_items`, `zz_05b_view`, `zz_05b_nokey`. In every `zz_05b_items`: id 3's note is 1,500 characters, id 5's has a line break, id 7's is 70,000 characters (over 64 KiB: only a preview), id 9's data is 100 bytes, id 11's big is 9007199254740993, even ids have a NULL note. While planning, the same catalogue under `zz_plan05b_` was created on every engine, read with each adapter's `edit_target`, and dropped (results in the rulings, H16).

- [ ] **Step 1: Write `scripts/scratch_tables.py`**

```python
"""Scratch tables for the 05b manual checks (grid editing) on the dev databases, made and dropped by exact name.

Run from the repository root with the backend's interpreter:

    backend/.venv/bin/python scripts/scratch_tables.py create [ENGINE ...]   # pg, mysql, mssql, sqlite; default: all
    backend/.venv/bin/python scripts/scratch_tables.py drop [ENGINE ...]
    backend/.venv/bin/python scripts/scratch_tables.py check                 # lists any zz_05b_ object; exit 1 if one is left
    backend/.venv/bin/python scripts/scratch_tables.py sql ENGINE "STATEMENT"            # one statement, auto-commit
    backend/.venv/bin/python scripts/scratch_tables.py hold ENGINE SECONDS "STATEMENT"   # in a transaction kept open, then rolled back

Only the objects of CATALOGUE are ever created or dropped, each by its exact name (`DROP … IF EXISTS <name>`, views
first); `sql` and `hold` refuse a statement that names no zz_05b_ object. Seeded tables are never written. SQLite works on
dev/data/scratch/zz_05b.sqlite (git-ignored, like all of dev/data), which stays, empty, after `drop`. Uses each engine's
own driver and the dev credentials of backend/tests/support.py; never touches the app's data directory or the Keychain.
"""

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import mysql.connector  # noqa: E402
import psycopg  # noqa: E402

from tests.support import (  # noqa: E402
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, mssql_connect, mysql_driver_kwargs,
)

PREFIX = "zz_05b_"
SQLITE_FILE = ROOT / "dev" / "data" / "scratch" / "zz_05b.sqlite"
ITEMS = 1200  # more than two pages of 500: Load more is needed to see them all
WIDE_ROWS = 100_000  # spec ⑤ §1: 100,000 rows x 30 columns (the Q1 render check)
WIDE_COLUMNS = 29  # plus the id


def n(name: str) -> str:
    return f"{PREFIX}{name}"


def _wide_pg() -> list[str]:
    columns = ", ".join(f"c{i:02d} text" for i in range(1, WIDE_COLUMNS + 1))
    values = ", ".join(f"'v{i:02d} ' || n" for i in range(1, WIDE_COLUMNS + 1))
    return [f"CREATE TABLE public.{n('wide')} (id integer PRIMARY KEY, {columns})",
            f"INSERT INTO public.{n('wide')} SELECT n, {values} FROM generate_series(1, {WIDE_ROWS}) AS n"]


def _special_rows(table: str, *, long_text: str, multiline: str, huge: str, blob: str) -> list[str]:
    """Rows the checks name by id: 3 a 1,500-character note (the drawer), 5 a two-line note (the drawer), 7 a note over
    64 KiB (only NULL), 9 a 100-byte blob (hex replaces all of it), 11 a bigint beyond 2^53."""
    return [f"UPDATE {table} SET note = {long_text} WHERE id = 3",
            f"UPDATE {table} SET note = {multiline} WHERE id = 5",
            f"UPDATE {table} SET note = {huge} WHERE id = 7",
            f"UPDATE {table} SET data = {blob} WHERE id = 9",
            f"UPDATE {table} SET big = 9007199254740993 WHERE id = 11"]


PG = [
    ("VIEW", f"public.{n('view')}"),
    ("TABLE", f"public.{n('items')}"),
    ("TABLE", f"public.{n('nokey')}"),
    ("TABLE", f"public.{n('wide')}"),
]
PG_CREATE = [
    f"""CREATE TABLE public.{n('items')} (
        id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, big bigint, label varchar(40), note text,
        amount numeric(12,2), created timestamptz NOT NULL DEFAULT now(), flag boolean, payload jsonb, data bytea,
        code varchar(20) NOT NULL DEFAULT 'x', total numeric GENERATED ALWAYS AS (amount * 2) STORED)""",
    f"""INSERT INTO public.{n('items')} (big, label, note, amount, flag, payload, data, code)
        SELECT n * 1000, 'row ' || n, CASE WHEN n % 2 = 0 THEN NULL ELSE 'note ' || n END, n / 100.0, n % 2 = 0,
               jsonb_build_object('n', n, 'tags', jsonb_build_array('a', 'b')), decode(lpad(to_hex(n), 8, '0'), 'hex'),
               'c' || n
        FROM generate_series(1, {ITEMS}) AS n""",
    *_special_rows(f"public.{n('items')}", long_text="repeat('long ', 300)", multiline="E'line one\\nline two'",
                   huge="repeat('x', 70000)", blob="decode(repeat('ab', 100), 'hex')"),
    f"CREATE VIEW public.{n('view')} AS SELECT id, label FROM public.{n('items')}",
    f"CREATE TABLE public.{n('nokey')} (a integer, b text)",
    f"INSERT INTO public.{n('nokey')} VALUES (1, 'one'), (2, 'two'), (3, 'three')",
    *_wide_pg(),
]

MYSQL = [
    ("VIEW", n("view")),
    ("TABLE", n("items")),
    ("TABLE", n("nokey")),
    ("TABLE", n("myisam")),
]
MYSQL_CREATE = [
    f"""CREATE TABLE {n('items')} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, big BIGINT NULL, label VARCHAR(40) NULL, note MEDIUMTEXT NULL,
        amount DECIMAL(12,2) NULL, created DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, flag TINYINT(1) NULL,
        payload JSON NULL, data VARBINARY(200) NULL, code VARCHAR(20) NOT NULL DEFAULT 'x',
        total DECIMAL(13,2) AS (amount * 2) STORED) ENGINE=InnoDB""",
    "SET SESSION cte_max_recursion_depth = 2000",
    f"""INSERT INTO {n('items')} (big, label, note, amount, flag, payload, data, code)
        WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < {ITEMS})
        SELECT n * 1000, CONCAT('row ', n), IF(n % 2 = 0, NULL, CONCAT('note ', n)), n / 100, n % 2 = 0,
               JSON_OBJECT('n', n, 'tags', JSON_ARRAY('a', 'b')), UNHEX(LPAD(HEX(n), 8, '0')), CONCAT('c', n)
        FROM seq""",
    *_special_rows(n("items"), long_text="REPEAT('long ', 300)", multiline="'line one\\nline two'",
                   huge="REPEAT('x', 70000)", blob="UNHEX(REPEAT('ab', 100))"),
    f"CREATE VIEW {n('view')} AS SELECT id, label FROM {n('items')}",
    f"CREATE TABLE {n('nokey')} (a INT NULL, b TEXT NULL)",
    f"INSERT INTO {n('nokey')} VALUES (1, 'one'), (2, 'two'), (3, 'three')",
    f"""CREATE TABLE {n('myisam')} (id INT PRIMARY KEY, label VARCHAR(40) NOT NULL, UNIQUE KEY {n('myisam_label')} (label))
        ENGINE=MyISAM""",
    f"INSERT INTO {n('myisam')} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
]

MSSQL = [
    ("VIEW", f"dbo.{n('view')}"),
    ("TABLE", f"dbo.{n('items')}"),
    ("TABLE", f"dbo.{n('nokey')}"),
]
MSSQL_CREATE = [
    f"""CREATE TABLE dbo.{n('items')} (
        id bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, big bigint NULL, label nvarchar(40) NULL, note nvarchar(max) NULL,
        amount decimal(12,2) NULL, created datetime2(3) NOT NULL CONSTRAINT {n('items_created')} DEFAULT SYSDATETIME(),
        flag bit NULL, payload nvarchar(max) NULL, data varbinary(200) NULL,
        code varchar(20) NOT NULL CONSTRAINT {n('items_code')} DEFAULT 'x', total AS (amount * 2))""",
    f"""INSERT INTO dbo.{n('items')} (big, label, note, amount, flag, payload, data, code)
        SELECT n * 1000, CONCAT(N'row ', n), CASE WHEN n % 2 = 0 THEN NULL ELSE CONCAT(N'note ', n) END, n / 100.0,
               CASE WHEN n % 2 = 0 THEN 1 ELSE 0 END, CONCAT(N'{{"n":', n, N'}}'), CAST(CAST(n AS int) AS varbinary(4)),
               CONCAT('c', n)
        FROM (SELECT TOP ({ITEMS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
              FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b) AS seq""",
    *_special_rows(f"dbo.{n('items')}", long_text="REPLICATE(N'long ', 300)",
                   multiline="CONCAT(N'line one', NCHAR(10), N'line two')",
                   huge="REPLICATE(CAST(N'x' AS nvarchar(max)), 70000)",
                   blob="CAST(REPLICATE(CAST(0xAB AS varbinary(max)), 100) AS varbinary(200))"),
    f"CREATE VIEW dbo.{n('view')} AS SELECT id, label FROM dbo.{n('items')}",
    f"CREATE TABLE dbo.{n('nokey')} (a int NULL, b nvarchar(20) NULL)",
    f"INSERT INTO dbo.{n('nokey')} VALUES (1, N'one'), (2, N'two'), (3, N'three')",
]

SQLITE = [
    ("VIEW", n("view")),
    ("TABLE", n("items")),
    ("TABLE", n("nokey")),
]
SQLITE_CREATE = [
    f"""CREATE TABLE {n('items')} (
        id INTEGER PRIMARY KEY, big INTEGER, label TEXT, note TEXT, amount NUMERIC, flag BOOLEAN, payload JSON,
        data BLOB, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, code TEXT NOT NULL DEFAULT 'x',
        total NUMERIC GENERATED ALWAYS AS (amount * 2) VIRTUAL)""",
    f"""INSERT INTO {n('items')} (big, label, note, amount, flag, payload, data, code)
        WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < {ITEMS})
        SELECT n * 1000, 'row ' || n, CASE WHEN n % 2 = 0 THEN NULL ELSE 'note ' || n END, n / 100.0, n % 2 = 0,
               json_object('n', n, 'tags', json_array('a', 'b')), unhex(printf('%08x', n)), 'c' || n
        FROM seq""",
    *_special_rows(n("items"), long_text="replace(hex(zeroblob(300)), '00', 'long ')",
                   multiline="'line one' || char(10) || 'line two'",
                   huge="replace(hex(zeroblob(35000)), '0', 'x')", blob="unhex(replace(hex(zeroblob(100)), '00', 'ab'))"),
    f"CREATE VIEW {n('view')} AS SELECT id, label FROM {n('items')}",
    f"CREATE TABLE {n('nokey')} (a INTEGER, b TEXT)",
    f"INSERT INTO {n('nokey')} VALUES (1, 'one'), (2, 'two'), (3, 'three')",
]


class Engine:
    """One dev database: its catalogue, the statements that make it, and how to run SQL on it."""

    def __init__(self, name: str, objects: list[tuple[str, str]], create: list[str]) -> None:
        self.name, self.objects, self.create_statements = name, objects, create

    def connect(self):
        raise NotImplementedError

    def run(self, conn, sql: str) -> int | None:
        raise NotImplementedError

    def leftovers(self, conn) -> list[str]:
        raise NotImplementedError

    def begin(self, conn) -> None:
        raise NotImplementedError

    def rollback(self, conn) -> None:
        raise NotImplementedError

    def drop(self, conn) -> None:
        for kind, name in self.objects:  # views first: they depend on the tables
            assert name.rsplit(".", 1)[-1].startswith(PREFIX), name  # never anything but a catalogue object
            self.run(conn, f"DROP {kind} IF EXISTS {name}")

    def create(self, conn) -> None:
        self.drop(conn)
        for statement in self.create_statements:
            self.run(conn, statement)


class Postgres(Engine):
    def connect(self):
        return psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="dbaguy",
                               autocommit=True, connect_timeout=5)

    def run(self, conn, sql):
        return conn.execute(sql).rowcount

    def leftovers(self, conn):
        rows = conn.execute("SELECT table_schema || '.' || table_name FROM information_schema.tables "
                            "WHERE table_name LIKE %s ORDER BY 1", (PREFIX.replace("_", r"\_") + "%",)).fetchall()
        return [row[0] for row in rows]

    def begin(self, conn):
        conn.autocommit = False

    def rollback(self, conn):
        conn.rollback()
        conn.autocommit = True


class Mysql(Engine):
    def connect(self):
        conn = mysql.connector.connect(**mysql_driver_kwargs())
        conn.autocommit = True
        return conn

    def run(self, conn, sql):
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            if cursor.with_rows:
                cursor.fetchall()
            return cursor.rowcount
        finally:
            cursor.close()

    def leftovers(self, conn):
        cursor = conn.cursor()
        cursor.execute("SELECT CONCAT(TABLE_SCHEMA, '.', TABLE_NAME) FROM information_schema.TABLES "
                       "WHERE TABLE_NAME LIKE %s ORDER BY 1", (PREFIX.replace("_", r"\_") + "%",))
        names = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return names

    def begin(self, conn):
        conn.start_transaction()

    def rollback(self, conn):
        conn.rollback()


class Mssql(Engine):
    def connect(self):
        return mssql_connect(database="dbaguy")

    def run(self, conn, sql):
        conn.execute_non_query(sql)
        return conn.rows_affected

    def leftovers(self, conn):
        pattern = PREFIX.replace("_", "[_]") + "%"
        conn.execute_query(f"SELECT SCHEMA_NAME(schema_id) + N'.' + name FROM dbaguy.sys.objects "
                           f"WHERE name LIKE N'{pattern}' AND type IN ('U', 'V') ORDER BY 1")
        return [row[0] for row in conn]

    def begin(self, conn):
        conn.execute_non_query("BEGIN TRANSACTION")

    def rollback(self, conn):
        conn.execute_non_query("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")


class Sqlite(Engine):
    def connect(self):
        SQLITE_FILE.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(SQLITE_FILE, isolation_level=None, timeout=1)

    def run(self, conn, sql):
        cursor = conn.execute(sql)
        cursor.fetchone()  # a SELECT under `hold` keeps its read lock while the transaction is open
        return cursor.rowcount

    def leftovers(self, conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE name LIKE ? ESCAPE '\\' AND type IN ('table', 'view') "
                            "ORDER BY 1", (PREFIX.replace("_", "\\_") + "%",)).fetchall()
        return [f"{SQLITE_FILE.name}: {row[0]}" for row in rows]

    def begin(self, conn):
        conn.execute("BEGIN")

    def rollback(self, conn):
        conn.execute("ROLLBACK")


ENGINES: dict[str, Engine] = {
    "pg": Postgres("pg", PG, PG_CREATE),
    "mysql": Mysql("mysql", MYSQL, MYSQL_CREATE),
    "mssql": Mssql("mssql", MSSQL, MSSQL_CREATE),
    "sqlite": Sqlite("sqlite", SQLITE, SQLITE_CREATE),
}


def _scratch_statement(statement: str) -> str:
    if PREFIX not in statement.lower():
        sys.exit(f"Refused: the statement names no {PREFIX} object")
    return statement


def _engines(names: list[str]) -> list[Engine]:
    unknown = [name for name in names if name not in ENGINES]
    if unknown:
        sys.exit(f"Unknown engine(s): {', '.join(unknown)} (pg, mysql, mssql, sqlite)")
    return [ENGINES[name] for name in names] or list(ENGINES.values())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command in ("create", "drop"):
        for engine in _engines(rest):
            conn = engine.connect()
            try:
                started = time.monotonic()
                getattr(engine, command)(conn)
                print(f"{engine.name}: {command} done in {time.monotonic() - started:.1f} s")
            finally:
                conn.close()
        return 0
    if command == "check":
        left = []
        for engine in ENGINES.values():
            conn = engine.connect()
            try:
                left += [f"{engine.name} {name}" for name in engine.leftovers(conn)]
            finally:
                conn.close()
        print("\n".join(left) if left else f"No {PREFIX} object left")
        return 1 if left else 0
    if command == "sql" and len(rest) == 2:
        engine, statement = _engines([rest[0]])[0], _scratch_statement(rest[1])
        conn = engine.connect()
        try:
            print(f"{engine.name}: {engine.run(conn, statement)} row(s)")
        finally:
            conn.close()
        return 0
    if command == "hold" and len(rest) == 3:
        engine, seconds, statement = _engines([rest[0]])[0], float(rest[1]), _scratch_statement(rest[2])
        conn = engine.connect()
        try:
            engine.begin(conn)
            engine.run(conn, statement)
            print(f"{engine.name}: holding for {seconds:g} s", flush=True)
            time.sleep(seconds)
            engine.rollback(conn)
            print(f"{engine.name}: rolled back")
        finally:
            conn.close()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Run: `backend/.venv/bin/python scripts/scratch_tables.py create && backend/.venv/bin/python scripts/scratch_tables.py check; echo "exit $?"; backend/.venv/bin/python scripts/scratch_tables.py drop && backend/.venv/bin/python scripts/scratch_tables.py check`
Expected: four "create done" lines; `check` lists 14 objects (pg 4, mysql 4, mssql 3, sqlite 3) and exits 1; after `drop`, "No zz_05b_ object left".

- [ ] **Step 2: Each browse job carries its page size and its position**

```diff
diff --git a/src/features/table-data/useTableData.ts b/src/features/table-data/useTableData.ts
index 6e144b5..0132c3f 100644
--- a/src/features/table-data/useTableData.ts
+++ b/src/features/table-data/useTableData.ts
@@ -1,9 +1,16 @@
-import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
+import { useCallback, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
 import { api, errorMessage, unwrap } from "@/lib/api/client";
 import type { PathSegment } from "@/lib/api/types";
 import type { TableTabState } from "@/stores/workspace";
-import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
-import { isExporting, PAGE_SIZE, type SessionController, type SessionSnapshot } from "../session/SessionController";
+import { useEngineInfo } from "../connections/hooks";
+import { firstPageDelivered, INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
+import {
+  isExporting,
+  MAX_PAGE_SIZE,
+  PAGE_SIZE,
+  type SessionController,
+  type SessionSnapshot,
+} from "../session/SessionController";
 import { useSession } from "../session/useSession";
 import { clearFilterHistory, readFilterHistory, rememberFilter } from "./filterHistory";
 import { relationOf, type Relation } from "./filterValue";
@@ -34,14 +41,24 @@ export interface TableDataState {
 interface Job {
   run: number;
   sql: string;
+  /** Rows of its first page. */
+  pageSize: number;
 }
 
 /** The applied browse: what the grid shows (or is loading). */
 interface BrowseJob extends Job {
   filter: string;
   sorts: SortKey[];
+  /**
+   * Changes when the grid should start again at the top (a new filter or sort); a re-run that keeps the position
+   * (Refresh, the re-read after a commit: D-05-12) keeps it.
+   */
+  position: number;
 }
 
+/** The first page of a re-run that keeps the position: the rows loaded now (at least a page, at most MAX_PAGE_SIZE). */
+export const keptPageSize = (loaded: number) => Math.min(MAX_PAGE_SIZE, Math.max(PAGE_SIZE, loaded));
+
 /** A count asked for `filter`; `sql` is null while it is being built. */
 interface CountJob {
   run: number;
@@ -109,7 +126,7 @@ function useSessionJob<J extends Job>(
     const previous = sent.current?.controller === controller ? sent.current : null;
     if (previous?.run === job.run) return;
     if (previous) endRequest(controller, previous.requestId);
-    const requestId = controller.execute(job.sql, { history: false, pageSize: PAGE_SIZE });
+    const requestId = controller.execute(job.sql, { history: false, pageSize: job.pageSize });
     sent.current = { controller, run: job.run, requestId };
     setCurrent({ requestId, job });
   }, [controller, phase, exporting, job]);
@@ -160,7 +177,10 @@ export function useTableData(tab: TableTabState, active: boolean) {
   const countRuns = useRef(0);
 
   const sentBrowse = useSessionJob(data.controller, data.snapshot, browseJob);
-  const countReady = useMemo(() => (countJob?.sql ? { run: countJob.run, sql: countJob.sql } : null), [countJob]);
+  const countReady = useMemo(
+    () => (countJob?.sql ? { run: countJob.run, sql: countJob.sql, pageSize: PAGE_SIZE } : null),
+    [countJob],
+  );
   const sentCount = useSessionJob(countController, countSnapshot, countReady);
   const execution: ExecutionState | null = sentBrowse
     ? (data.snapshot?.executions[sentBrowse.requestId] ?? null)
@@ -187,7 +207,14 @@ export function useTableData(tab: TableTabState, active: boolean) {
           if (ticket !== builds.current) return;
           setBuildError(null);
           if (newFilter) setFilterError(null);
-          setBrowseJob((job) => ({ run: (job?.run ?? 0) + 1, sql, filter: nextFilter, sorts }));
+          setBrowseJob((job) => ({
+            run: (job?.run ?? 0) + 1,
+            sql,
+            filter: nextFilter,
+            sorts,
+            pageSize: PAGE_SIZE,
+            position: (job?.position ?? 0) + 1,
+          }));
         },
         (error: unknown) => {
           if (ticket !== builds.current) return;
@@ -247,13 +274,30 @@ export function useTableData(tab: TableTabState, active: boolean) {
     (column: string, descending: boolean) => browse(filter, [{ column, descending }]),
     [browse, filter],
   );
-  /** Runs the applied browse again (or builds the first one if that failed); a finished count is out of date. */
+  // D-05-12 (CR-1: only an engine whose table data can be edited): a re-run reloads as many rows as are loaded and the
+  // grid keeps its position. Read when the re-run is asked for, never as a dependency (rows arrive page by page).
+  const keepsPosition = useEngineInfo(profileId)?.row_editing === true;
+  const loaded = useRef(0);
+  useLayoutEffect(() => {
+    loaded.current = execution?.resultSets[0]?.rows.length ?? 0;
+  });
+  /**
+   * Runs the applied browse again (or builds the first one if that failed); a finished count is out of date. Where the
+   * engine can edit rows, it loads as many rows as are loaded now (up to MAX_PAGE_SIZE) and keeps the grid's position.
+   */
   const refresh = useCallback(() => {
     setBuildError(null);
     if (!countRunning) setCountJob(null);
-    if (browseJob) setBrowseJob((job) => job && { ...job, run: job.run + 1 });
-    else browse("", []);
-  }, [browse, browseJob, countRunning]);
+    if (!browseJob) {
+      browse("", []);
+      return;
+    }
+    setBrowseJob((job) =>
+      job && keepsPosition
+        ? { ...job, run: job.run + 1, pageSize: keptPageSize(loaded.current) }
+        : job && { ...job, run: job.run + 1, pageSize: PAGE_SIZE, position: job.position + 1 },
+    );
+  }, [browse, browseJob, countRunning, keepsPosition]);
   /** Stops a browse that has not delivered its first page yet. */
   const cancel = useCallback(() => {
     if (sentBrowse) data.controller?.cancel(sentBrowse.requestId);
@@ -285,6 +329,10 @@ export function useTableData(tab: TableTabState, active: boolean) {
   /** Escape (the input shows the applied filter again) or an edit: the rejected text is no longer what is shown. */
   const clearFilterError = useCallback(() => setFilterError(null), []);
 
+  // The request whose first page is shown: a commit whose outcome was unknown waits for another one (fresh rows, R-05-6),
+  // a Refresh or the same browse sent again on a new session alike.
+  const deliveredRequest = sentBrowse && firstPageDelivered(execution) ? sentBrowse.requestId : null;
+
   const state: TableDataState = {
     filter,
     sorts: browseJob?.sorts ?? [],
@@ -302,6 +350,10 @@ export function useTableData(tab: TableTabState, active: boolean) {
     state,
     execution,
     pending,
+    /** Changes only when the grid should scroll back to the top (D-05-12). */
+    position: browseJob?.position ?? 0,
+    /** The browse request whose first page is in (null while one loads). */
+    deliveredRequest,
     controller: data.controller,
     snapshot: data.snapshot,
     relation,
```

- [ ] **Step 3: The grid resets its scroll only when told to**

```diff
diff --git a/src/features/grid/DataGrid.tsx b/src/features/grid/DataGrid.tsx
index 6e5340b..9104735 100644
--- a/src/features/grid/DataGrid.tsx
+++ b/src/features/grid/DataGrid.tsx
@@ -60,6 +60,11 @@ export interface DataGridProps {
    * delivers its first page (`useHeldResult`); nothing more is loaded for them.
    */
   stale?: boolean;
+  /**
+   * A value whose change scrolls the grid back to the top; by default the column list (every new result). A table data
+   * tab passes its browse's position, so a re-run that keeps it (Refresh, a commit) keeps the scroll (D-05-12).
+   */
+  resetScrollOn?: unknown;
   /** Dialect of the copy formats that need one (INSERT, IN list), and of the formats offered; null while unknown. */
   dialect: Dialect | null;
   /** INSERT copies target this table (table data); else a `table_name` placeholder. */
@@ -164,13 +169,17 @@ export function DataGrid(props: DataGridProps) {
   useLayoutEffect(() => {
     columnVirtualizer.measure();
   }, [columnVirtualizer, layout.widths]);
-  // Incremental loading asks once per row count (see below); a new result starts over, at the top. A result with
-  // other columns also starts at the left; the same columns (a sort, a refresh) keep the horizontal position.
+  // Incremental loading asks once per row count (see below), for each new result. A new result starts at the top unless
+  // the caller keeps the position (`resetScrollOn`), but never with other columns. A result with other columns also
+  // starts at the left; the same columns (a sort, a refresh) keep the horizontal position.
   const requestedAt = useRef(-1);
+  const scrollReset = props.resetScrollOn === undefined ? columns : props.resetScrollOn;
   useLayoutEffect(() => {
     requestedAt.current = -1;
-    scrollRef.current?.scrollTo({ top: 0 });
   }, [columns]);
+  useLayoutEffect(() => {
+    scrollRef.current?.scrollTo({ top: 0 });
+  }, [scrollReset, signature]);
   useLayoutEffect(() => {
     scrollRef.current?.scrollTo({ left: 0 });
   }, [signature]);
```

- [ ] **Step 4: The Data view holds the rows until the first page is in, and keeps the position**

```diff
diff --git a/src/features/table-data/TableDataTab.tsx b/src/features/table-data/TableDataTab.tsx
index bb6a801..1d29b6d 100644
--- a/src/features/table-data/TableDataTab.tsx
+++ b/src/features/table-data/TableDataTab.tsx
@@ -17,7 +17,7 @@ import { DataGrid } from "../grid/DataGrid";
 import { ExportDialog } from "../grid/ExportDialog";
 import type { GridSort } from "../grid/GridHeader";
 import { useHeldResult } from "../grid/useHeldResult";
-import { CONNECTION_LOST } from "../session/execution";
+import { CONNECTION_LOST, firstPageDelivered } from "../session/execution";
 import { isExporting } from "../session/SessionController";
 import { useSessionGuards } from "../session/SessionGuards";
 import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
@@ -161,8 +161,9 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   const [exportOpen, setExportOpen] = useState(false);
   const openExport = useCallback(() => setExportOpen(true), []);
   const phase = snapshot?.phase ?? "opening";
-  const current = execution?.resultSets[0] ?? null;
-  // Until a sort, filter or refresh delivers its first page, the grid keeps the previous rows (and its layout), dimmed.
+  // Until a sort, filter or refresh has its whole first page, the grid keeps the previous rows (and its layout),
+  // dimmed: it never flashes empty, and a re-run that keeps the position finds it (A12).
+  const current = firstPageDelivered(execution) ? (execution?.resultSets[0] ?? null) : null;
   const shown = useHeldResult(current, table.pending);
   const set = shown.value;
   const columns = set?.columns;
@@ -278,6 +279,7 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
             loadingMore={execution?.status === "running"}
             onLoadMore={phase === "open" ? table.loadMore : undefined}
             stale={shown.stale}
+            resetScrollOn={table.position}
             dialect={dialect}
             tableName={tableName}
             sort={gridSort}
```

- [ ] **Step 5: Check the types and the build**

Run: `npm run typecheck && npm run build && npx vitest run`
Expected: no errors; PASS.

- [ ] **Step 6: Manual check list** (the coordinator, `scripts/dev.sh --web`, `http://localhost:1420`; the grid's scroll area is `document.querySelector('[data-tab-panel]:not(.hidden) [role="grid"] .overflow-auto')`, called `el` below):
  1. `backend/.venv/bin/python scripts/scratch_tables.py create` → four "create done" lines.
  2. dev pg › dbaguy › public › Tables (refresh the tree) › zz_05b_items: the status bar says "500 rows loaded"; scroll to the bottom twice → "1,200 rows".
  3. `el.scrollTop = 900 * 22`; click a cell of row 900; note `el.scrollTop`. Refresh: the rows never disappear (at most dimmed, with "Loading…"), and afterwards `el.scrollTop` is the same, row 900's cell is still the active one and the status bar says "1,200 rows" (A12, D-05-12).
  4. Sort by `label` (the header's sort control): the grid goes to the top. Sort again: top again.
  5. Filter `id > 100`: top. Clear the filter.
  6. zz_05b_wide: run in the console `for (let i = 0; i < 30; i++) { el.scrollTop = el.scrollHeight; await new Promise((r) => setTimeout(r, 400)); }` until the status bar shows at least "12,000 rows loaded"; `el.scrollTop = 9000 * 22`; Refresh → the status bar says "10,000 rows loaded" and row 9,000 is still in view; scrolling down loads on from 10,000.
  7. A query tab, `SELECT * FROM public.zz_05b_items`, run twice with ⌘↵ after scrolling down: the second run starts at the top (unchanged).
  8. dev mysql › zz_05b_items: load all 1,200, scroll to row 900, Refresh → the same position.
  9. A Couchbase collection's browse tab (double-click a collection in the tree), scrolled down, run again (⌘↵): it starts at the top (CR-1: not a table-data tab).
  10. `backend/.venv/bin/python scripts/scratch_tables.py drop && backend/.venv/bin/python scripts/scratch_tables.py check` → "No zz_05b_ object left".

- [ ] **Step 7: Commit**

```bash
git add scripts/scratch_tables.py src/features/table-data/useTableData.ts src/features/grid/DataGrid.tsx \
  src/features/table-data/TableDataTab.tsx
git commit -m "feat(app): Refresh keeps the table's position and loaded rows, and never flashes empty (D-05-12, A12); zz_05b scratch tables"
```

---

### Task 7: The grid's surface for editing — marks, row labels, the key icon, the edit menu, the editor's slots

**Suggested implementer:** Sonnet.

**Files:**
- Modify: `src/features/grid/marks.ts` (the classes), `src/features/grid/GridRow.tsx`, `src/features/grid/GridHeader.tsx`, `src/features/grid/GridMenu.tsx`, `src/features/grid/GridDrawer.tsx`, `src/features/grid/DataGrid.tsx`
- Create: `src/features/grid/EditorSlot.tsx`, `src/features/grid/renderCount.ts`

**Interfaces:**
- Consumes: Task 3 (`CellMark`, `RowMark`, `HeaderMark`); `MenuItem`; the grid's own `ColumnLayout`, `GridGeometry`, `CellPos`, `GridSelection`, `Rect`, `HEADER_HEIGHT`, `ROW_HEIGHT`.
- Produces:
  - `marks.ts`: `CHANGED_CELL`, `NEW_ROW`, `DELETED_ROW`, `PROBLEM_CELL` (token colours mixed into `--color-bg`, so a pinned cell stays opaque).
  - `EditorSlot.tsx`: `GridEditor { pos: CellPos; mode: "inline" | "drawer"; render: (width: number) => ReactNode }`; `GridEditing { editable: boolean; marks; leading: number; headerMarks; onEdit(pos); onKey(e, selection, rect): boolean; menu(pos, cell, rect): MenuItem[]; editor: GridEditor | null }`; `InlineEditorSlot({ editor, layout, geometry, totalWidth })`.
  - `DataGrid`: prop `edits?: GridEditing`; `DataGridHandle.selectCell(row, col)` (selects and scrolls); the status bar counts the loaded rows only (`rows.length - leading`).
  - `GridRow` props `label: string`, `mark?: RowMark`; `GridHeader` prop `marks?`; `GridMenuActions.editItems?(pos, cell, rect)`; `GridDrawer.tsx` exports `DrawerPanel({ width, onWidthChange, children })` (the value viewer, the record view and the value editor share it, DRY).
  - `renderCount.ts`: `countRowRender(index)` — dev only: appends to `window.__gridRowRenders` when the array exists.

Behaviour (spec §3.6.2–§3.6.4; rulings H17, H23):
- Without `edits` the grid is exactly as before (every query tab, every read-only table with nothing staged). With `edits.editable` false it draws the marks only (staged changes on a table that cannot be edited now): Return still opens the value viewer and the menus are the read-only ones.
- With `edits.editable`: a key on the focused grid goes to `edits.onKey` first (true: handled, the grid does nothing more), a double click on a cell is `onEdit`, a cell's menu and the row numbers' menu lead with `edits.menu(…)`'s items, and View value loses its ↵.
- Marks: a changed cell has the warning tint and its tooltip adds "Was: …" (and the conflict and the problem); a new row's cells the success tint, DEFAULT cells "DEFAULT" in grey italics, row number `+`; a deleted row's cells the danger tint struck through, row number `−`; a problem is a danger outline (the active cell's accent outline wins), a row problem turns the row number red with the problem as its tooltip; a conflict is a danger corner. One background class per cell (`cn` has no tailwind-merge).
- Headers: a key column has a key icon; a column that cannot be written has a muted name and "Read-only: <reason>" in its tooltip.
- The inline editor is laid over its cell inside the scroll area (so it scrolls with the rows), sticky for a pinned column, above the rows and under the sticky header; the drawer editor takes the drawer while it edits (the viewer or the record view shows again after).

- [ ] **Step 1: The marks' classes and the render counter**

```diff
diff --git a/src/features/grid/marks.ts b/src/features/grid/marks.ts
index 671e117..45eb9ec 100644
--- a/src/features/grid/marks.ts
+++ b/src/features/grid/marks.ts
@@ -32,3 +32,16 @@ export interface HeaderMark {
   readonly key: boolean;
   readonly readOnly: string | null;
 }
+
+/*
+ * The marks' look: colour tokens only, mixed into the page background so that a pinned (sticky) cell stays opaque, and
+ * written out in full so that Tailwind emits them.
+ */
+/** A changed cell: warning, light. */
+export const CHANGED_CELL = "bg-[color-mix(in_oklab,var(--color-warning)_16%,var(--color-bg))]";
+/** Every cell of a new row: success, light. */
+export const NEW_ROW = "bg-[color-mix(in_oklab,var(--color-success)_13%,var(--color-bg))]";
+/** Every cell of a row marked for deletion: danger, light, struck through. */
+export const DELETED_ROW = "bg-[color-mix(in_oklab,var(--color-danger)_11%,var(--color-bg))] line-through";
+/** A cell the last preview or commit refused (the active cell's accent outline wins). */
+export const PROBLEM_CELL = "outline-1 -outline-offset-1 outline-danger";
```

`src/features/grid/renderCount.ts`:

```ts
/*
 * Dev only (spec ⑤ §7, Q1): the indexes of the body rows rendered, so that "staging one cell re-renders only its row"
 * can be measured in the browser: set `window.__gridRowRenders = []`, stage a cell, read it back. Production builds
 * drop it (`import.meta.env.DEV` is false there). StrictMode renders twice in dev: count distinct indexes.
 */
declare global {
  interface Window {
    __gridRowRenders?: number[];
  }
}

export function countRowRender(index: number): void {
  if (!import.meta.env.DEV) return;
  window.__gridRowRenders?.push(index);
}
```

- [ ] **Step 2: Rows, cells and headers draw the marks**

```diff
diff --git a/src/features/grid/GridRow.tsx b/src/features/grid/GridRow.tsx
index e0999d5..2f80bf5 100644
--- a/src/features/grid/GridRow.tsx
+++ b/src/features/grid/GridRow.tsx
@@ -4,6 +4,8 @@ import { describeCell } from "@/lib/cells";
 import { cn } from "@/lib/cn";
 import { ROW_HEIGHT } from "./columns";
 import { columnClass, columnStyle, SELECTED_CLASS, type ColumnStrip, type DrawnColumn } from "./layout";
+import { CHANGED_CELL, DELETED_ROW, NEW_ROW, PROBLEM_CELL, type CellMark, type RowMark } from "./marks";
+import { countRowRender } from "./renderCount";
 
 /** A cell shows one line, so characters beyond this never fit and are not laid out. */
 const CELL_CHARS = 1000;
@@ -19,14 +21,22 @@ interface GridCellProps extends DrawnColumn {
   selected: boolean;
   /** Set on the active cell only (the grid's `aria-activedescendant`). */
   id?: string;
+  /** The cell's staged change, if any (an object kept while it does not change). */
+  mark?: CellMark;
+  /** The row's tint (a new row, a row marked for deletion), if any. */
+  tint?: string;
 }
 
 /**
- * Memoized on primitives, so a row re-rendered for a horizontal scroll or a selection change only renders the cells
- * that came into view or changed state.
+ * Memoized on primitives (and on its mark, an object kept while unchanged), so a row re-rendered for a horizontal
+ * scroll or a selection change only renders the cells that came into view or changed state.
  */
-const GridCell = memo(function GridCell({ value, category, selected, id, ...column }: GridCellProps) {
+const GridCell = memo(function GridCell({ value, category, selected, id, mark, tint, ...column }: GridCellProps) {
   const cell = describeCell(value, category, CELL_CHARS);
+  const isDefault = mark?.isDefault === true;
+  // One background: a pinned cell must stay opaque, and two background classes would fight (no tailwind-merge).
+  const background = mark?.changed ? CHANGED_CELL : (tint ?? (column.left !== undefined ? "bg-bg" : undefined));
+  const title = [cell.title, mark?.was, mark?.conflict, mark?.problem].filter(Boolean).join("\n") || undefined;
   return (
     <div
       id={id}
@@ -36,18 +46,32 @@ const GridCell = memo(function GridCell({ value, category, selected, id, ...colu
       className={cn(
         CELL,
         columnClass(column),
-        column.left !== undefined && "bg-bg",
+        background,
         selected && SELECTED_CLASS,
+        mark?.problem && !id && PROBLEM_CELL,
         id && FOCUSED,
-        cell.alignRight && "justify-end",
+        mark?.conflict && column.left === undefined && "relative",
+        cell.alignRight && !isDefault && "justify-end",
       )}
       style={columnStyle(column)}
-      title={cell.title}
+      title={title}
     >
-      <span className={cn("truncate", (cell.kind === "null" || cell.kind === "missing") && "italic text-muted")}>
-        {cell.text}
-      </span>
-      {cell.detail && <span className="shrink-0 whitespace-pre text-muted">{cell.detail}</span>}
+      {isDefault ? (
+        <span className="truncate text-muted italic">DEFAULT</span>
+      ) : (
+        <>
+          <span className={cn("truncate", (cell.kind === "null" || cell.kind === "missing") && "italic text-muted")}>
+            {cell.text}
+          </span>
+          {cell.detail && <span className="shrink-0 whitespace-pre text-muted">{cell.detail}</span>}
+        </>
+      )}
+      {mark?.conflict && (
+        <span
+          aria-hidden
+          className="absolute top-0 right-0 border-t-[6px] border-l-[6px] border-t-danger border-l-transparent"
+        />
+      )}
     </div>
   );
 });
@@ -67,11 +91,19 @@ interface GridRowProps {
   focusCol: number;
   /** Element id of the active cell. */
   focusId: string;
+  /** What the row-number column shows: the row's number, `+` (a new row) or `−` (marked for deletion). */
+  label: string;
+  /** The row's staged change, if any (an object kept while it does not change). */
+  mark?: RowMark;
 }
 
+const TINTS: Record<RowMark["kind"], string | undefined> = { new: NEW_ROW, deleted: DELETED_ROW, changed: undefined };
+
 /** One body row: the fixed row number, then the strip's columns. */
 export const GridRow = memo(function GridRow(props: GridRowProps) {
-  const { index, row, top, strip, selLeft, selRight, focusCol, focusId } = props;
+  const { index, row, top, strip, selLeft, selRight, focusCol, focusId, label, mark } = props;
+  countRowRender(index);
+  const tint = mark ? TINTS[mark.kind] : undefined;
   return (
     <div
       role="row"
@@ -84,12 +116,14 @@ export const GridRow = memo(function GridRow(props: GridRowProps) {
         aria-colindex={1}
         className={cn(
           CELL,
-          "sticky left-0 z-20 justify-end border-r bg-panel text-muted",
+          "sticky left-0 z-20 justify-end border-r bg-panel",
+          mark?.problem ? "font-semibold text-danger" : "text-muted",
           selLeft >= 0 && SELECTED_CLASS,
         )}
         style={{ width: strip.rowNumberWidth }}
+        title={mark?.problem}
       >
-        {index + 1}
+        {label}
       </div>
       {strip.drawn.map((column) => (
         <GridCell
@@ -99,6 +133,8 @@ export const GridRow = memo(function GridRow(props: GridRowProps) {
           category={strip.columns[column.col].category}
           selected={column.col >= selLeft && column.col <= selRight}
           id={column.col === focusCol ? focusId : undefined}
+          mark={mark?.cells?.[column.col]}
+          tint={tint}
         />
       ))}
     </div>
```

```diff
diff --git a/src/features/grid/GridHeader.tsx b/src/features/grid/GridHeader.tsx
index ec14c3f..873ea72 100644
--- a/src/features/grid/GridHeader.tsx
+++ b/src/features/grid/GridHeader.tsx
@@ -1,7 +1,9 @@
+import { KeyRound } from "lucide-react";
 import { memo, type PointerEvent } from "react";
 import { cn } from "@/lib/cn";
 import { HEADER_HEIGHT } from "./columns";
 import { columnClass, columnStyle, SELECTED_CLASS, type ColumnStrip } from "./layout";
+import type { HeaderMark } from "./marks";
 
 export interface GridSort {
   column: number;
@@ -18,6 +20,8 @@ interface GridHeaderProps {
   /** Selected column range (-1 when nothing is selected). Selecting is done by the grid's pointer handler. */
   selLeft: number;
   selRight: number;
+  /** An editable table's columns (spec ⑤ §3.6.2): a key icon, and why a column cannot be written (its tooltip). */
+  marks?: readonly (HeaderMark | undefined)[];
 }
 
 interface ResizeHandleProps {
@@ -94,7 +98,7 @@ function SortControl({ mark, onSort }: SortControlProps) {
  * 12 px semibold, type font-sans 11 px).
  */
 export const GridHeader = memo(function GridHeader(props: GridHeaderProps) {
-  const { strip, sort, onHeaderClick, onResize, onAutofit, selLeft, selRight } = props;
+  const { strip, sort, onHeaderClick, onResize, onAutofit, selLeft, selRight, marks } = props;
   return (
     <div
       role="row"
@@ -111,6 +115,8 @@ export const GridHeader = memo(function GridHeader(props: GridHeaderProps) {
       />
       {strip.drawn.map((column) => {
         const meta = strip.columns[column.col];
+        const mark = marks?.[column.col];
+        const notes = [mark?.key && "Key column", mark?.readOnly && `Read-only: ${mark.readOnly}`].filter(Boolean);
         return (
           <div
             key={column.col}
@@ -125,10 +131,11 @@ export const GridHeader = memo(function GridHeader(props: GridHeaderProps) {
               column.col >= selLeft && column.col <= selRight && SELECTED_CLASS,
             )}
             style={columnStyle(column)}
-            title={`${meta.name}\n${meta.type_name}`}
+            title={[meta.name, meta.type_name, ...notes].join("\n")}
           >
             <div className="flex min-w-0 items-center gap-1">
-              <span className="truncate font-semibold">{meta.name}</span>
+              {mark?.key && <KeyRound size={11} className="shrink-0 text-muted" aria-label="Key column" />}
+              <span className={cn("truncate font-semibold", mark?.readOnly && "text-muted")}>{meta.name}</span>
               {onHeaderClick && (
                 <SortControl
                   mark={sortMark(sort, column.col)}
```

- [ ] **Step 3: The menu's edit items and the shared drawer panel**

```diff
diff --git a/src/features/grid/GridMenu.tsx b/src/features/grid/GridMenu.tsx
index 11711eb..8253d32 100644
--- a/src/features/grid/GridMenu.tsx
+++ b/src/features/grid/GridMenu.tsx
@@ -27,6 +27,11 @@ export interface GridMenuActions {
   extraCellMenu?: (row: number, column: number) => MenuItem[];
   /** "Export…": the whole result to a file (shown only when set). */
   openExport?: () => void;
+  /**
+   * An editable grid's items (spec ⑤ §3.6.4), leading a cell's menu (`cell`) or the row numbers' (not `cell`):
+   * Return edits there, so View value loses its ↵.
+   */
+  editItems?: (pos: CellPos | null, cell: boolean, rect: Rect | null) => MenuItem[];
 }
 
 /** The group starts with a separator. */
@@ -66,9 +71,11 @@ function headerItems(col: number, a: GridMenuActions): MenuItem[] {
 
 /** Copy, Copy as, Export, the drawers; for a cell (`cell` set) also pinning and the caller's extra items. */
 function selectionItems(pos: CellPos | null, cell: boolean, a: GridMenuActions): MenuItem[] {
-  const { openValue, openRecord, openExport } = a;
+  const { openValue, openRecord, openExport, editItems } = a;
+  const edits = editItems?.(pos, cell, a.rect) ?? [];
   const items: MenuItem[] = [
-    { label: "Copy", shortcut: "⌘C", onSelect: a.copyDefault },
+    ...edits,
+    { label: "Copy", shortcut: "⌘C", onSelect: a.copyDefault, separatorBefore: edits.length > 0 },
     {
       label: "Copy as",
       children: a.copyFormats.map((format) => ({ label: COPY_FORMAT_LABELS[format], onSelect: () => a.copy(format) })),
@@ -76,7 +83,7 @@ function selectionItems(pos: CellPos | null, cell: boolean, a: GridMenuActions):
     ...(openExport ? [{ label: "Export…", onSelect: openExport }] : []),
     {
       label: "View value",
-      shortcut: "↵",
+      shortcut: editItems ? undefined : "↵",
       separatorBefore: true,
       disabled: !openValue || !pos,
       onSelect: () => pos && openValue?.(pos),
```

```diff
diff --git a/src/features/grid/GridDrawer.tsx b/src/features/grid/GridDrawer.tsx
index 54be7b9..832ca70 100644
--- a/src/features/grid/GridDrawer.tsx
+++ b/src/features/grid/GridDrawer.tsx
@@ -1,3 +1,4 @@
+import type { ReactNode } from "react";
 import { Panel } from "react-resizable-panels";
 import { PanelHandle } from "@/components/ui/PanelHandle";
 import type { ColumnMeta } from "@/lib/api/types";
@@ -63,21 +64,38 @@ function DrawerContent({ drawer, columns, rows, hasMore, focus, onMove, onClose
 }
 
 /**
- * The drawer's divider and panel, rendered inside the grid's horizontal Group. It follows the grid's active cell
- * (value viewer) or row (record view). Keeps its pixel width when the window resizes.
+ * The drawer's divider and panel, rendered inside the grid's horizontal Group. Keeps its pixel width when the window
+ * resizes.
  */
-export function GridDrawerPanel(props: GridDrawerPanelProps) {
+export function DrawerPanel({
+  width,
+  onWidthChange,
+  children,
+}: {
+  width: number;
+  onWidthChange: (width: number) => void;
+  children: ReactNode;
+}) {
   return (
     <>
       <PanelHandle orientation="horizontal" />
       <Panel
-        defaultSize={props.width}
+        defaultSize={width}
         minSize={MIN_WIDTH}
         groupResizeBehavior="preserve-pixel-size"
-        onResize={(size) => props.onWidthChange(size.inPixels)}
+        onResize={(size) => onWidthChange(size.inPixels)}
       >
-        <DrawerContent {...props} />
+        {children}
       </Panel>
     </>
   );
 }
+
+/** The value viewer or the record view in the drawer: it follows the grid's active cell (or row). */
+export function GridDrawerPanel(props: GridDrawerPanelProps) {
+  return (
+    <DrawerPanel width={props.width} onWidthChange={props.onWidthChange}>
+      <DrawerContent {...props} />
+    </DrawerPanel>
+  );
+}
```

- [ ] **Step 4: `src/features/grid/EditorSlot.tsx`**

```tsx
import type { KeyboardEvent, ReactNode } from "react";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { cn } from "@/lib/cn";
import { HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
import type { GridGeometry } from "./hitTest";
import type { ColumnLayout } from "./layout";
import type { HeaderMark, RowMark } from "./marks";
import type { CellPos, GridSelection, Rect } from "./selection";

/** The cell being edited and its editor (spec ⑤ §3.6.3): over the cell (inline) or in the drawer. */
export interface GridEditor {
  pos: CellPos;
  mode: "inline" | "drawer";
  /** The editor; `width` is the cell's width (inline) or the drawer's. Keyed by the caller per edited cell. */
  render: (width: number) => ReactNode;
}

/** What an editable table's grid gets (spec ⑤ §3.6): marks to draw, and the editing it hands back to its owner. */
export interface GridEditing {
  /**
   * Whether cells can be edited now: Return, F2 and a double click edit, and the menus lead with the edit items. When
   * false the grid only draws the marks (staged changes of a table that cannot be edited now) and Return views.
   */
  editable: boolean;
  /** By display index (sparse): new, changed and deleted rows. */
  marks: readonly (RowMark | undefined)[];
  /** New rows drawn first: the loaded rows' numbers count from after them. */
  leading: number;
  /** By column index. */
  headerMarks: readonly (HeaderMark | undefined)[];
  /** Return, F2 or a double click on a cell. */
  onEdit: (pos: CellPos) => void;
  /**
   * A key pressed while the grid itself has the focus, before the grid handles it: true when it was an editing key
   * that was handled (the grid then does nothing more with it).
   */
  onKey: (e: KeyboardEvent<HTMLDivElement>, selection: GridSelection | null, rect: Rect | null) => boolean;
  /** Items leading a cell's menu (`cell`) or the row numbers' menu (not `cell`), for the selection `rect`. */
  menu: (pos: CellPos | null, cell: boolean, rect: Rect | null) => MenuItem[];
  editor: GridEditor | null;
}

/**
 * An inline editor laid over its cell inside the grid's scroll area, so it scrolls with the rows: a strip as wide as
 * the rows at the cell's row, holding the editor at the cell's left, sticky for a pinned column as its cells are. Above
 * the rows (and their sticky cells), under the sticky header. Only the editor itself takes the pointer.
 */
export function InlineEditorSlot({
  editor,
  layout,
  geometry,
  totalWidth,
}: {
  editor: GridEditor;
  layout: ColumnLayout;
  geometry: GridGeometry;
  totalWidth: number;
}) {
  const { row, col } = editor.pos;
  const width = layout.widths[col];
  const pinned = col < layout.pinnedCount;
  const left = pinned ? layout.pinnedLeft[col] : geometry.unpinnedEnds[col - layout.pinnedCount] - width;
  return (
    <div
      className="pointer-events-none absolute left-0 z-[25] flex"
      style={{ top: HEADER_HEIGHT + row * ROW_HEIGHT, height: ROW_HEIGHT, width: totalWidth }}
    >
      <div className={cn("pointer-events-auto", pinned && "sticky")} style={pinned ? { left } : { marginLeft: left }}>
        {editor.render(width)}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: The grid takes `edits`**

```diff
diff --git a/src/features/grid/DataGrid.tsx b/src/features/grid/DataGrid.tsx
index 9104735..de8b09e 100644
--- a/src/features/grid/DataGrid.tsx
+++ b/src/features/grid/DataGrid.tsx
@@ -21,7 +21,8 @@ import { cn } from "@/lib/cn";
 import { aggregate } from "./aggregate";
 import { columnSignature, HEADER_HEIGHT, ROW_HEIGHT } from "./columns";
 import { copyDefault, copyFormatsFor, copySelection, type CopyFormat, type CopyInput } from "./copy";
-import { GridDrawerPanel, type GridDrawer } from "./GridDrawer";
+import { DrawerPanel, GridDrawerPanel, type GridDrawer } from "./GridDrawer";
+import { InlineEditorSlot, type GridEditing } from "./EditorSlot";
 import { GridHeader, type GridSort } from "./GridHeader";
 import { GridMenu } from "./GridMenu";
 import { GridRow } from "./GridRow";
@@ -37,6 +38,8 @@ export interface DataGridHandle {
    * that axis alone.
    */
   scrollToCell(row: number, col: number): void;
+  /** Makes the cell the active (and only selected) cell and shows it. */
+  selectCell(row: number, col: number): void;
   focus(): void;
 }
 
@@ -81,6 +84,8 @@ export interface DataGridProps {
    * export outlives a remounting grid.
    */
   onExport?: () => void;
+  /** An editable table's marks and editing (spec ⑤ §3.6); absent: a read-only grid, as before. */
+  edits?: GridEditing;
   ref?: Ref<DataGridHandle>;
 }
 
@@ -132,7 +137,9 @@ function LoadMoreFooter({ rowCount, hasMore, loading, onLoadMore }: LoadMoreFoot
  */
 export function DataGrid(props: DataGridProps) {
   const { columns, rows, hasMore, sort, onHeaderClick, stale = false, ref } = props;
-  const { dialect, tableName, onSortBy, extraCellMenu, onSelectionChange, onExport, partial = false } = props;
+  const { dialect, tableName, onSortBy, extraCellMenu, onSelectionChange, onExport, partial = false, edits } = props;
+  const editing = edits?.editable ? edits : undefined;
+  const leading = edits?.leading ?? 0;
   // Stale rows are only shown: nothing is loaded for them.
   const loadingMore = props.loadingMore && !stale;
   const onLoadMore = stale ? undefined : props.onLoadMore;
@@ -221,7 +228,18 @@ export function DataGrid(props: DataGridProps) {
     [rowVirtualizer, columnVirtualizer, layout.pinnedCount, layout.fixedWidth],
   );
   const focusGrid = useCallback(() => rootRef.current?.focus({ preventScroll: true }), []);
-  useImperativeHandle(ref, () => ({ scrollToCell, focus: focusGrid }), [scrollToCell, focusGrid]);
+  const selectAndShow = useCallback(
+    (row: number, col: number) => {
+      select(selectCell({ row, col }));
+      scrollToCell(row, col);
+    },
+    [select, scrollToCell],
+  );
+  useImperativeHandle(ref, () => ({ scrollToCell, selectCell: selectAndShow, focus: focusGrid }), [
+    scrollToCell,
+    selectAndShow,
+    focusGrid,
+  ]);
 
   const pointer = usePointerSelection({ scrollRef, columns, geometry, selection, rect, select, focusGrid });
   /** What a copy covers: the selection over the rows loaded now, whatever its mode. */
@@ -265,6 +283,8 @@ export function DataGrid(props: DataGridProps) {
     return el && e.target !== el ? hitTest(geometry, viewportPoint(el, e.clientX, e.clientY)) : null;
   };
   const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
+    // An editable grid's keys first (Return edits there); keys typed into its editor are the editor's.
+    if (editing && e.target === e.currentTarget && editing.onKey(e, selection, rect)) return;
     gridKeyDown(e, {
       scrollRef,
       geometry,
@@ -331,6 +351,7 @@ export function DataGrid(props: DataGridProps) {
                 copyDefault={copyDefaultSelection}
                 copy={copy}
                 copyFormats={copyFormatsFor(dialect)}
+                editItems={editing?.menu}
                 pin={layout.pin}
                 openValue={openAt(openValue)}
                 openRecord={openAt(openRecord)}
@@ -349,6 +370,10 @@ export function DataGrid(props: DataGridProps) {
                   onPointerUp={pointer.onPointerUp}
                   onPointerCancel={pointer.onPointerCancel}
                   onLostPointerCapture={pointer.onLostPointerCapture}
+                  onDoubleClick={(e) => {
+                    const hit = editing ? menuHit(e) : null;
+                    if (hit?.kind === "cell") editing?.onEdit({ row: hit.row, col: hit.col });
+                  }}
                 >
                   <div
                     className="relative isolate"
@@ -362,9 +387,13 @@ export function DataGrid(props: DataGridProps) {
                       onAutofit={layout.autofit}
                       selLeft={left}
                       selRight={right}
+                      marks={edits?.headerMarks}
                     />
                     {virtualRows.map((item) => {
                       const touched = item.index >= top && item.index <= bottom;
+                      const mark = edits?.marks[item.index];
+                      const label =
+                        mark?.kind === "new" ? "+" : mark?.kind === "deleted" ? "−" : String(item.index - leading + 1);
                       return (
                         <GridRow
                           key={item.index}
@@ -376,9 +405,19 @@ export function DataGrid(props: DataGridProps) {
                           selRight={touched ? right : -1}
                           focusCol={focus?.row === item.index ? focus.col : -1}
                           focusId={focusId}
+                          label={label}
+                          mark={mark}
                         />
                       );
                     })}
+                    {editing?.editor?.mode === "inline" && (
+                      <InlineEditorSlot
+                        editor={editing.editor}
+                        layout={layout}
+                        geometry={geometry}
+                        totalWidth={strip.totalWidth}
+                      />
+                    )}
                     {footer && (
                       <div
                         className="absolute left-0 flex"
@@ -413,25 +452,37 @@ export function DataGrid(props: DataGridProps) {
               <div className="pointer-events-none absolute inset-0 z-10 hidden border border-accent group-focus-visible/grid:block" />
             </div>
           </Panel>
-          {drawer && (
-            <GridDrawerPanel
-              drawer={drawer}
-              columns={columns}
-              rows={rows}
-              hasMore={hasMore}
-              focus={focus ?? null}
+          {editing?.editor?.mode === "drawer" ? (
+            // The value editor takes the drawer while it edits; the viewer or record view shows again after.
+            <DrawerPanel
               width={drawerWidth}
               onWidthChange={(width) => {
                 liveDrawerWidth.current = width;
               }}
-              onMove={moveRow}
-              onClose={closeDrawer}
-            />
+            >
+              {editing.editor.render(drawerWidth)}
+            </DrawerPanel>
+          ) : (
+            drawer && (
+              <GridDrawerPanel
+                drawer={drawer}
+                columns={columns}
+                rows={rows}
+                hasMore={hasMore}
+                focus={focus ?? null}
+                width={drawerWidth}
+                onWidthChange={(width) => {
+                  liveDrawerWidth.current = width;
+                }}
+                onMove={moveRow}
+                onClose={closeDrawer}
+              />
+            )
           )}
         </Group>
       </div>
       <StatusBar
-        rowCount={rows.length}
+        rowCount={rows.length - leading}
         columnCount={columns.length}
         hasMore={hasMore}
         partial={partial}
```

- [ ] **Step 6: Check the types and the build**

Run: `npm run typecheck && npm run build && npx vitest run`
Expected: no errors; PASS. (Nothing passes `edits` yet: Task 9 does.)

- [ ] **Step 7: Manual check list — no regression** (the coordinator, `scripts/dev.sh --web`):
  1. `backend/.venv/bin/python scripts/scratch_tables.py create pg`.
  2. A query tab, `SELECT * FROM public.zz_05b_items`: row numbers 1, 2, …; Return opens the value viewer, Space the record view, Escape closes the drawer; dragging the drawer's divider resizes it; a cell's menu has "View value ↵"; ⌘C copies; the status bar says "500 rows loaded".
  3. The table tab of zz_05b_items: the same; no key icon and no tint anywhere yet.
  4. In the console: `window.__gridRowRenders = []`, scroll down by a few rows, then `new Set(window.__gridRowRenders)` holds the indexes of the rows that came into view (the counter works; production builds have none).
  5. Pin two columns (header menu): pinned cells stay opaque while scrolling sideways.
  6. `backend/.venv/bin/python scripts/scratch_tables.py drop pg && backend/.venv/bin/python scripts/scratch_tables.py check` → "No zz_05b_ object left".

- [ ] **Step 8: Commit**

```bash
git add src/features/grid/marks.ts src/features/grid/renderCount.ts src/features/grid/GridRow.tsx \
  src/features/grid/GridHeader.tsx src/features/grid/GridMenu.tsx src/features/grid/GridDrawer.tsx \
  src/features/grid/EditorSlot.tsx src/features/grid/DataGrid.tsx
git commit -m "feat(grid): marks for staged changes, row labels, key and read-only headers, edit menu items, editor slots"
```

---

### Task 8: The editors and the keys — inline input, the drawer's value editor, IME-guarded key maps

**Suggested implementer:** Sonnet.

**Files:**
- Modify: `src/lib/ime.ts`, `src/features/editor/extensions.ts`
- Create: `src/features/edits/editKeys.ts`, `src/features/edits/editing.ts`, `src/features/edits/CellEditor.tsx`, `src/features/edits/ValueEditor.tsx`
- Test: `src/features/edits/editing.test.ts`

**Interfaces:**
- Consumes: Task 2 (`editText`, `CellInput`); `isImeKeyEvent`, `isCommandKey`; `canvasMeasure` (`grid/columns.ts`); `CodeEditor`, `CodeEditorHandle`; `DrawerFrame`; `EditColumn`, `EditKind`, `ColumnMeta`.
- Produces:
  - `ime.ts`: `isImeKeyEvent(event: Pick<KeyboardEvent, "isComposing" | "key" | "keyCode"> | ReactKeyboardEvent)` (same rule, a structural parameter so the key maps' tests pass plain objects).
  - `extensions.ts`: `EditorLanguage = "sql" | "json" | "text"` ("text": no language, and `closeBrackets` adds nothing, H10); `STABLE_SCROLLBAR: Extension` (`.cm-scroller { scrollbar-gutter: stable }`).
  - `editKeys.ts`: `type GridEditKey = "edit" | "setNull" | "deleteRows" | "undo" | "redo"`, `gridEditKey(e)`; `type CellEditorKey = "commit" | "next" | "previous" | "cancel" | "setNull" | "save"`, `cellEditorKey(e)`.
  - `editing.ts`: `INLINE_MAX_CHARS = 1_000`; `type RowState = "new" | "loaded" | "deleted" | "key_incomplete"`; `EditStart { column; kind; nullable; mode: "inline" | "drawer"; initialText; placeholder; original }`; `editStart(input: EditCellInput): EditStart | { refuse: string }`; `editorResult(text, touched): CellInput | null`; `nextWritable(names, writable, from, step): number`.
  - `CellEditor.tsx`: `INPUT_MAX_WIDTH = 480`; `type EditEnd = "commit" | "next" | "previous" | "blur" | "save"`; `CellEditor({ initialText, placeholder, width, numeric, onDone(text | null, how), onCancel, onSetNull })`.
  - `ValueEditor.tsx`: `ValueEditor({ column, kind, initialText, placeholder, was, nullable, onDone(text | null, "commit" | "blur" | "save"), onCancel, onSetNull })`.

Behaviour (spec §3.6.3, §3.6.4, R-05-8, R-05-9; rulings H4, H5, H10, H12, H19, H23):
- Keys on the focused grid: Return / F2 edit (no modifier), ⌥⌘N set NULL (by the physical key: ⌥N is a dead key on a Mac layout), ⌘⌫ delete rows, ⌘Z / ⇧⌘Z undo / redo; in the inline editor: Return stages, Tab / ⇧Tab stage and move along the row, Esc cancels, ⌥⌘N sets NULL, ⌘S stages and asks for the preview (H19). Every map returns nothing for an input method's key (`isImeKeyEvent` first: composing, `key` "Process", or WebKit's late keyCode 229 Enter). There is no type-to-edit: a letter on the grid does nothing.
- An edit starts: the staged value's text, or "" with the placeholder NULL / DEFAULT, or the loaded value's editor text; bytes the grid holds only 64 of start empty, "Hex to replace all N bytes" (H5); a cut text is refused ("This value is too large to edit here (N characters); Set NULL or use SQL"); a read-only column, a column not in the table, a row marked for deletion and a row whose key was not loaded completely say why. JSON columns, text with a line break and text over INLINE_MAX_CHARS open in the drawer.
- An edit ends: nothing is staged unless something was typed (H4) — an untouched Return keeps NULL or DEFAULT, a typed-then-erased value is the empty string. Leaving the editor stages what was typed (a click elsewhere), but leaving the window does not end it; an editor unmounted with typed text (its row left the rows shown) stages it (H12). The inline input never takes the grid's pointer, double click or context menu.
- The drawer's editor: CodeMirror (JSON for a json column, plain text otherwise, no auto-closed brackets), word wrap, its scrollbar reserved; ⌘↵ / Apply stage, Return is a line break, Esc / Cancel drop, Set NULL (disabled for NOT NULL), ⌘S stages and asks for the preview; focus leaving the drawer for the grid stages and closes it.

- [ ] **Step 1: Write the failing test** `src/features/edits/editing.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { EditColumn } from "@/lib/api/types";
import { editorResult, editStart, INLINE_MAX_CHARS, nextWritable, type EditCellInput } from "./editing";
import { cellEditorKey, gridEditKey } from "./editKeys";

/*
 * How a cell's edit starts and ends (spec ⑤ §3.6.3, R-05-8) and the editing keys (§3.6.4): NULL is never turned into
 * an empty string by an untouched Return, a cut value is never edited from its preview, and an input method's Enter
 * never stages or opens the preview.
 */

const column = (overrides: Partial<EditColumn> = {}): EditColumn => ({
  name: "note",
  type_name: "text",
  kind: "text",
  nullable: true,
  has_default: false,
  writable: true,
  compare: "equals",
  unicode: true,
  ...overrides,
});
const start = (overrides: Partial<EditCellInput>) =>
  editStart({ column: column(), name: "note", row: "loaded", staged: undefined, original: "hello", ...overrides });

describe("starting an edit", () => {
  it("starts with the value's text, inline", () => {
    expect(start({})).toMatchObject({ mode: "inline", initialText: "hello", placeholder: null });
    expect(start({ original: 42, column: column({ kind: "number" }) })).toMatchObject({ initialText: "42" });
  });

  it("starts empty over NULL and DEFAULT, saying which it is", () => {
    expect(start({ original: null })).toMatchObject({ initialText: "", placeholder: "NULL" });
    expect(start({ row: "new", original: undefined })).toMatchObject({ initialText: "", placeholder: "DEFAULT" });
    expect(start({ staged: { kind: "null" } })).toMatchObject({ initialText: "", placeholder: "NULL" });
    expect(start({ staged: { kind: "value", text: "staged" } })).toMatchObject({ initialText: "staged" });
  });

  it("opens JSON, text with a line break and long text in the drawer", () => {
    expect(start({ column: column({ kind: "json" }), original: '{"a":1}' })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "one\ntwo" })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "x".repeat(INLINE_MAX_CHARS + 1) })).toMatchObject({ mode: "drawer" });
    expect(start({ original: "x".repeat(INLINE_MAX_CHARS) })).toMatchObject({ mode: "inline" });
  });

  it("never starts from a preview: bytes are typed whole, a cut text only takes NULL", () => {
    expect(
      start({ column: column({ kind: "binary" }), original: { $type: "bytes", size: 100, preview: "ab".repeat(64) } }),
    ).toMatchObject({
      initialText: "",
      placeholder: "Hex to replace all 100 bytes",
    });
    expect(
      start({ column: column({ kind: "binary" }), original: { $type: "bytes", size: 2, preview: "dead" } }),
    ).toMatchObject({
      initialText: "0xdead",
    });
    expect(start({ original: { $type: "text", preview: "abc", size: 70_000, truncated: true } })).toEqual({
      refuse: "This value is too large to edit here (70,000 characters); Set NULL or use SQL",
    });
  });

  it("says why a cell cannot be edited", () => {
    expect(start({ column: column({ writable: false, read_only_reason: "generated column" }) })).toEqual({
      refuse: "note is read-only: generated column",
    });
    expect(start({ column: undefined })).toEqual({ refuse: "note is not a column of the table any more: Refresh" });
    expect(start({ row: "deleted" })).toEqual({ refuse: "This row is marked for deletion: restore it first (⌘⌫)" });
    expect(start({ row: "key_incomplete" })).toEqual({
      refuse: "This row cannot be edited: its key was not loaded completely",
    });
  });
});

describe("ending an edit", () => {
  it("stages nothing unless something was typed, and an erased value as the empty string", () => {
    expect(editorResult("", false)).toBeNull();
    expect(editorResult("hello", false)).toBeNull();
    expect(editorResult("", true)).toEqual({ kind: "value", text: "" });
    expect(editorResult("9007199254740993", true)).toEqual({ kind: "value", text: "9007199254740993" });
  });

  it("moves to the next or previous writable column, and stops at the row's ends", () => {
    const names = ["id", "total", "label", "note"];
    const writable = (name: string) => name !== "total";
    expect(nextWritable(names, writable, 0, 1)).toBe(2);
    expect(nextWritable(names, writable, 2, -1)).toBe(0);
    expect(nextWritable(names, writable, 3, 1)).toBe(-1);
  });
});

const key = (overrides: Partial<KeyboardEvent>) =>
  ({
    key: "",
    code: "",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    isComposing: false,
    keyCode: 0,
    ...overrides,
  }) as KeyboardEvent;

describe("the grid's editing keys", () => {
  it.each([
    [{ key: "Enter", code: "Enter" }, "edit"],
    [{ key: "F2", code: "F2" }, "edit"],
    [{ key: "Dead", code: "KeyN", metaKey: true, altKey: true }, "setNull"],
    [{ key: "Backspace", code: "Backspace", metaKey: true }, "deleteRows"],
    [{ key: "z", code: "KeyZ", metaKey: true }, "undo"],
    [{ key: "z", code: "KeyZ", metaKey: true, shiftKey: true }, "redo"],
    [{ key: "Backspace", code: "Backspace" }, null],
    [{ key: "a", code: "KeyA" }, null], // no typing to edit
  ])("%j → %s", (event, action) => {
    expect(gridEditKey(key(event))).toBe(action);
  });

  it("ignores an input method's keys, the late Enter included", () => {
    expect(gridEditKey(key({ key: "Enter", isComposing: true }))).toBeNull();
    expect(gridEditKey(key({ key: "Enter", keyCode: 229 }))).toBeNull();
    expect(gridEditKey(key({ key: "Process" }))).toBeNull();
  });
});

describe("the cell editor's keys", () => {
  it.each([
    [{ key: "Enter" }, "commit"],
    [{ key: "Tab" }, "next"],
    [{ key: "Tab", shiftKey: true }, "previous"],
    [{ key: "Escape" }, "cancel"],
    [{ key: "Dead", code: "KeyN", metaKey: true, altKey: true }, "setNull"],
    [{ key: "s", code: "KeyS", metaKey: true }, "save"],
    [{ key: "Enter", shiftKey: true }, null],
  ])("%j → %s", (event, action) => {
    expect(cellEditorKey(key(event))).toBe(action);
  });

  it("never stages on the Enter that picks a character", () => {
    expect(cellEditorKey(key({ key: "Enter", keyCode: 229 }))).toBeNull();
    expect(cellEditorKey(key({ key: "s", code: "KeyS", metaKey: true, isComposing: true }))).toBeNull();
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `npx vitest run src/features/edits/editing.test.ts`
Expected: FAIL (cannot resolve `./editing`, `./editKeys`).

- [ ] **Step 3: The IME check takes plain objects; the editor's "text" language and the reserved scrollbar**

```diff
diff --git a/src/lib/ime.ts b/src/lib/ime.ts
index 86d05e6..a69003d 100644
--- a/src/lib/ime.ts
+++ b/src/lib/ime.ts
@@ -1,13 +1,16 @@
 import type { KeyboardEvent as ReactKeyboardEvent } from "react";
 
+/** What decides it: a DOM key event has them all (a plain object with them does too, for the pure key maps' tests). */
+type ImeSignals = Pick<KeyboardEvent, "isComposing" | "key" | "keyCode">;
+
 /**
  * Whether a key event belongs to an input method (e.g. Zhuyin) rather than to the app: during composition, and the
  * Enter that confirms it. WebKit can deliver that Enter after `compositionend`, with `isComposing` false but
  * `keyCode` 229 (ruling R-02c-9), so all three signals count. Every keyboard shortcut and Enter/Escape handler outside
  * CodeMirror checks this first; CodeMirror's keymaps handle composition themselves.
  */
-export function isImeKeyEvent(event: KeyboardEvent | ReactKeyboardEvent): boolean {
-  const native = "nativeEvent" in event ? event.nativeEvent : event;
+export function isImeKeyEvent(event: ImeSignals | ReactKeyboardEvent): boolean {
+  const native: ImeSignals = "nativeEvent" in event ? event.nativeEvent : event;
   // `keyCode` is deprecated, but it is the only signal WebKit gives for the late Enter.
   return native.isComposing || native.key === "Process" || native.keyCode === 229;
 }
```

```diff
diff --git a/src/features/editor/extensions.ts b/src/features/editor/extensions.ts
index 72066e2..e13e820 100644
--- a/src/features/editor/extensions.ts
+++ b/src/features/editor/extensions.ts
@@ -20,8 +20,8 @@ import type { Dialect, EditorSettings } from "@/lib/api/types";
 import { undoHistory } from "./startingPoint";
 import { editorTheme, fontSizeTheme } from "./theme";
 
-/** What an editor holds: SQL (a query tab's text) or JSON (a Couchbase document's). */
-export type EditorLanguage = "sql" | "json";
+/** What an editor holds: SQL (a query tab's text), JSON (a Couchbase document's), or plain text (a cell's value). */
+export type EditorLanguage = "sql" | "json" | "text";
 
 /** What the live settings and the tab's connection decide about an editor. */
 export interface EditorOptions {
@@ -120,16 +120,28 @@ const LIVE_SLOTS: readonly LiveSlot[] = [
   // The language and the label that names it for assistive technology.
   slot(
     (o) => `${o.language}:${o.dialect}:${o.upperCaseKeywords}`,
-    (o) =>
-      o.language === "json"
-        ? [json(), EditorView.contentAttributes.of({ "aria-label": "JSON editor" })]
-        : [
-            sql({ dialect: o.dialect ? SQL_DIALECTS[o.dialect] : StandardSQL, upperCaseKeywords: o.upperCaseKeywords }),
-            EditorView.contentAttributes.of({ "aria-label": "SQL editor" }),
-          ],
+    (o) => {
+      if (o.language === "json") return [json(), EditorView.contentAttributes.of({ "aria-label": "JSON editor" })];
+      // A value is typed as it is: no bracket or quote is ever added (H10).
+      if (o.language === "text")
+        return [NO_AUTO_CLOSE, EditorView.contentAttributes.of({ "aria-label": "Value editor" })];
+      return [
+        sql({ dialect: o.dialect ? SQL_DIALECTS[o.dialect] : StandardSQL, upperCaseKeywords: o.upperCaseKeywords }),
+        EditorView.contentAttributes.of({ "aria-label": "SQL editor" }),
+      ];
+    },
   ),
 ];
 
+/** No language data of its own: `closeBrackets` then closes nothing (its brackets come from the language data). */
+const NO_AUTO_CLOSE: Extension = EditorState.languageData.of(() => [{ closeBrackets: { brackets: [] } }]);
+
+/**
+ * The editor's own scroller keeps room for its scrollbar (the user's Mac shows classic scrollbars, which take room):
+ * for an editor in a fixed-height box (the preview's statements, the value editor).
+ */
+export const STABLE_SCROLLBAR: Extension = EditorView.theme({ ".cm-scroller": { scrollbarGutter: "stable" } });
+
 /** The parts that never change: editing, selection, search, comments and the look. */
 const STATIC: Extension = [
   lineNumbers(),
```

- [ ] **Step 4: Write `src/features/edits/editKeys.ts` and `src/features/edits/editing.ts`**

```ts
import { isImeKeyEvent } from "@/lib/ime";
import { isCommandKey } from "@/lib/shortcuts";

/*
 * The editing keys (spec ⑤ §3.6.4, R-05-9), as pure maps from a key event. An input method's key — composing, or the
 * Enter that confirms it, which WebKit may send late with keyCode 229 — is never one (R-02c-9): a Zhuyin user's Enter
 * that picks a character never stages a value or opens the preview.
 */

type KeyLike = Pick<
  KeyboardEvent,
  "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "isComposing" | "keyCode"
>;

/** What a key asks of the focused, editable grid (not of an editor). */
export type GridEditKey = "edit" | "setNull" | "deleteRows" | "undo" | "redo";

const plain = (e: KeyLike) => !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey;
/** ⌥⌘N (DataGrip's key): ⌥N types a dead key on a Mac layout, so the physical key decides. */
const isSetNull = (e: KeyLike) => e.metaKey && e.altKey && !e.ctrlKey && !e.shiftKey && e.code === "KeyN";

export function gridEditKey(e: KeyLike): GridEditKey | null {
  if (isImeKeyEvent(e)) return null;
  if (plain(e) && (e.key === "Enter" || e.key === "F2")) return "edit";
  if (isSetNull(e)) return "setNull";
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && e.key === "Backspace") return "deleteRows";
  if (isCommandKey(e, "z")) return "undo";
  if (isCommandKey(e, "z", { shift: true })) return "redo";
  return null;
}

/** What a key asks of the inline cell editor. */
export type CellEditorKey = "commit" | "next" | "previous" | "cancel" | "setNull" | "save";

export function cellEditorKey(e: KeyLike): CellEditorKey | null {
  if (isImeKeyEvent(e)) return null;
  if (plain(e) && e.key === "Enter") return "commit";
  if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "Tab") return e.shiftKey ? "previous" : "next";
  if (plain(e) && e.key === "Escape") return "cancel";
  if (isSetNull(e)) return "setNull";
  if (isCommandKey(e, "s")) return "save";
  return null;
}
```

```ts
import type { EditColumn, EditKind } from "@/lib/api/types";
import { editText, type CellInput } from "./cells";

/*
 * Starting and ending a cell's edit (spec ⑤ §3.6.3, R-05-8), as pure functions: which editor, the text it starts with,
 * and what it stages. There is no typing to edit (R-05-9): an edit starts with Return, F2 or a double click.
 */

/** Text longer than this opens in the drawer's editor, as does JSON and any text with a line break. */
export const INLINE_MAX_CHARS = 1_000;

/** Where a cell's row stands for editing. */
export type RowState = "new" | "loaded" | "deleted" | "key_incomplete";

export interface EditStart {
  readonly column: string;
  readonly kind: EditKind;
  readonly nullable: boolean;
  readonly mode: "inline" | "drawer";
  readonly initialText: string;
  /** Shown while the editor is empty: NULL, DEFAULT, or what typing replaces. */
  readonly placeholder: string | null;
  /** The loaded cell (a loaded row's), for the drawer's "Was:". */
  readonly original: unknown;
}

export interface EditCellInput {
  /** The edit target's column; undefined when the table has no such column (any more). */
  readonly column: EditColumn | undefined;
  readonly name: string;
  readonly row: RowState;
  /** The cell's staged value, if it has one. */
  readonly staged: CellInput | undefined;
  /** The loaded cell; undefined for a new row. */
  readonly original: unknown;
}

const isBytes = (cell: unknown) => !!cell && typeof cell === "object" && (cell as { $type?: string }).$type === "bytes";
const sizeOf = (cell: unknown) => ((cell as { size?: number }).size ?? 0).toLocaleString();

/** How the cell's edit starts, or why it cannot (said in a toast). */
export function editStart(input: EditCellInput): EditStart | { refuse: string } {
  const { column, name, row, staged, original } = input;
  if (!column) return { refuse: `${name} is not a column of the table any more: Refresh` };
  if (!column.writable) return { refuse: `${name} is read-only: ${column.read_only_reason ?? "it cannot be written"}` };
  if (row === "deleted") return { refuse: "This row is marked for deletion: restore it first (⌘⌫)" };
  if (row === "key_incomplete") return { refuse: "This row cannot be edited: its key was not loaded completely" };
  let initialText = "";
  let placeholder: string | null = null;
  if (staged) {
    if (staged.kind === "value") initialText = staged.text;
    else placeholder = staged.kind === "null" ? "NULL" : "DEFAULT";
  } else if (row === "new") {
    placeholder = "DEFAULT";
  } else if (original === null || original === undefined) {
    placeholder = "NULL";
  } else {
    const loaded = editText(original);
    if (loaded.complete) initialText = loaded.text;
    else if (isBytes(original))
      placeholder = `Hex to replace all ${sizeOf(original)} bytes`; // H5: never the preview
    else
      return { refuse: `This value is too large to edit here (${sizeOf(original)} characters); Set NULL or use SQL` };
  }
  const long = initialText.length > INLINE_MAX_CHARS || /[\r\n]/.test(initialText);
  return {
    column: name,
    kind: column.kind,
    nullable: column.nullable,
    mode: column.kind === "json" || long ? "drawer" : "inline",
    initialText,
    placeholder,
    original,
  };
}

/**
 * What an editor that ends stages (H4): nothing unless something was typed — Return on an untouched NULL or DEFAULT
 * keeps it — and otherwise the text as it is, an empty one included (clearing a value gives the empty string; NULL is
 * only ever set explicitly).
 */
export function editorResult(text: string, touched: boolean): CellInput | null {
  return touched ? { kind: "value", text } : null;
}

/** The next (`step` 1) or previous (-1) column after `from` that can be written, or -1 (Tab, ⇧Tab). */
export function nextWritable(
  names: readonly string[],
  writable: (name: string) => boolean,
  from: number,
  step: 1 | -1,
): number {
  for (let col = from + step; col >= 0 && col < names.length; col += step) if (writable(names[col])) return col;
  return -1;
}
```

- [ ] **Step 5: Run the test**

Run: `npx vitest run src/features/edits/editing.test.ts`
Expected: PASS (24 tests).

- [ ] **Step 6: Write `src/features/edits/CellEditor.tsx` and `src/features/edits/ValueEditor.tsx`**

```tsx
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { canvasMeasure } from "../grid/columns";
import { cellEditorKey } from "./editKeys";

/** The inline editor never grows wider than this (spec ⑤ §3.6.3). */
export const INPUT_MAX_WIDTH = 480;
const INPUT_PADDING = 18;

/** How an edit ended: the editor's text is staged first unless it was untouched (`text` null). */
export type EditEnd = "commit" | "next" | "previous" | "blur" | "save";

export interface CellEditorProps {
  initialText: string;
  /** Shown while it is empty (NULL, DEFAULT, or what typing replaces). */
  placeholder: string | null;
  /** The cell's width: the input is never narrower, and grows with its text up to INPUT_MAX_WIDTH. */
  width: number;
  /** A number column: the text is aligned right, as the grid aligns it. */
  numeric: boolean;
  /** `text` is null when nothing was typed (H4: then nothing is staged). */
  onDone: (text: string | null, how: EditEnd) => void;
  onCancel: () => void;
  onSetNull: () => void;
}

/**
 * The inline cell editor (spec ⑤ §3.6.3): a real `<input>` over the cell, so an input method composes in it. Return
 * stages and closes, Tab / ⇧Tab stage and move along the row, Escape is the only way to drop what was typed, and
 * leaving it (a click elsewhere) stages too, as Numbers does. Every key goes through `cellEditorKey`, which leaves an
 * input method's keys alone. The pointer, a double click and the context menu stay with the input, not the grid.
 */
export function CellEditor({ initialText, placeholder, width, numeric, onDone, onCancel, onSetNull }: CellEditorProps) {
  const [text, setText] = useState(initialText);
  const input = useRef<HTMLInputElement>(null);
  const touched = useRef(false);
  const done = useRef(false);
  const latest = useRef({ text, onDone });
  useLayoutEffect(() => {
    latest.current = { text, onDone };
  });
  useLayoutEffect(() => {
    input.current?.focus({ preventScroll: true });
    input.current?.select();
  }, []);
  // Unmounted while something typed is still in it (its row left the rows shown, H12): it is staged, never lost.
  useEffect(
    () => () => {
      if (!done.current && touched.current) latest.current.onDone(latest.current.text, "blur");
    },
    [],
  );

  const finish = (how: EditEnd) => {
    if (done.current) return;
    done.current = true;
    onDone(touched.current ? text : null, how);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const action = cellEditorKey(e.nativeEvent);
    if (!action) return;
    e.preventDefault();
    e.stopPropagation();
    if (action === "cancel") {
      done.current = true;
      onCancel();
    } else if (action === "setNull") {
      done.current = true;
      onSetNull();
    } else finish(action);
  };
  const measured = canvasMeasure()(text || placeholder || "", "cell") + INPUT_PADDING;
  return (
    <input
      ref={input}
      value={text}
      placeholder={placeholder ?? undefined}
      spellCheck={false}
      autoComplete="off"
      aria-label="Cell value"
      onChange={(e) => {
        touched.current = true;
        setText(e.target.value);
      }}
      onKeyDown={onKeyDown}
      // Leaving the window is not leaving the cell: the editor is still there when the window comes back.
      onBlur={() => document.hasFocus() && finish("blur")}
      onPointerDown={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      className={`h-[22px] rounded-none border border-accent bg-elevated px-2 font-mono text-[12px] text-fg outline-none placeholder:text-muted placeholder:italic ${numeric ? "text-right" : ""}`}
      style={{ width: Math.min(INPUT_MAX_WIDTH, Math.max(width, measured)) }}
    />
  );
}
```

```tsx
import { Prec, type Extension } from "@codemirror/state";
import { EditorView, keymap, placeholder as placeholderText } from "@codemirror/view";
import { useEffect, useLayoutEffect, useMemo, useRef, type FocusEvent } from "react";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta, EditKind } from "@/lib/api/types";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { STABLE_SCROLLBAR } from "../editor/extensions";
import { DrawerFrame } from "../grid/DrawerFrame";
import type { EditEnd } from "./CellEditor";

export interface ValueEditorProps {
  column: ColumnMeta;
  kind: EditKind;
  initialText: string;
  placeholder: string | null;
  /** "Was: …", a loaded row's value before the edit; null for a new row. */
  was: string | null;
  nullable: boolean;
  onDone: (text: string | null, how: Extract<EditEnd, "commit" | "blur" | "save">) => void;
  onCancel: () => void;
  onSetNull: () => void;
}

/**
 * The drawer's value editor (spec ⑤ §3.6.3): CodeMirror, JSON for a json column and plain text otherwise (no bracket is
 * ever added, H10), for JSON, text with line breaks or over 1,000 characters. ⌘↵ or Apply stages, Return is a line
 * break, Escape or Cancel drop the edit, Set NULL stages NULL, ⌘S stages and opens the preview. Leaving the drawer for
 * the grid stages what was typed and closes it, as the inline editor does. CodeMirror skips an input method's keys.
 */
export function ValueEditor({
  column,
  kind,
  initialText,
  placeholder,
  was,
  nullable,
  onDone,
  onCancel,
  onSetNull,
}: ValueEditorProps) {
  const editor = useRef<CodeEditorHandle>(null);
  const touched = useRef(false);
  const done = useRef(false);
  const actions = useRef({ onDone, onCancel, onSetNull });
  useLayoutEffect(() => {
    actions.current = { onDone, onCancel, onSetNull };
  });
  const finish = (how: "commit" | "blur" | "save") => {
    if (done.current) return;
    done.current = true;
    actions.current.onDone(touched.current ? (editor.current?.getText() ?? null) : null, how);
  };
  const latestFinish = useRef(finish);
  useLayoutEffect(() => {
    latestFinish.current = finish;
  });
  const cancel = () => {
    done.current = true;
    actions.current.onCancel();
  };
  const latestCancel = useRef(cancel);
  useLayoutEffect(() => {
    latestCancel.current = cancel;
  });
  const extensions = useMemo<Extension[]>(
    () => [
      Prec.highest(
        keymap.of([
          { key: "Mod-Enter", run: () => (latestFinish.current("commit"), true) },
          { key: "Mod-s", run: () => (latestFinish.current("save"), true) },
          { key: "Escape", run: () => (latestCancel.current(), true) },
        ]),
      ),
      EditorView.lineWrapping,
      STABLE_SCROLLBAR,
      ...(placeholder ? [placeholderText(placeholder)] : []),
    ],
    [placeholder],
  );
  useEffect(() => {
    editor.current?.focus();
  }, []);
  // Unmounted while something typed is still in it (its row left the rows shown, H12): staged, never lost. The editor's
  // own unmount hands its text over first (CodeEditor's `onUnmount` runs in a layout cleanup, before this one).
  const lastText = useRef<string | null>(null);
  useEffect(
    () => () => {
      if (!done.current && touched.current) actions.current.onDone(lastText.current, "blur");
    },
    [],
  );
  // Focus going to the grid (or anywhere outside the drawer, while the window has it) ends the edit, staging it.
  const onBlur = (e: FocusEvent<HTMLElement>) => {
    if (!document.hasFocus() || e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    finish("blur");
  };
  return (
    <div className="h-full" onBlur={onBlur}>
      <DrawerFrame
        label="Value editor"
        title={
          <>
            <span className="truncate font-mono font-semibold" data-selectable>
              {column.name}
            </span>
            <span className="shrink-0 text-[11px] text-muted">{column.type_name}</span>
          </>
        }
        toolbar={
          <>
            {was && (
              <span className="min-w-0 flex-1 truncate text-muted" title={was} data-selectable>
                {was}
              </span>
            )}
            <span className="ml-auto flex gap-1">
              <Button
                size="sm"
                onClick={() => {
                  done.current = true;
                  actions.current.onSetNull();
                }}
                disabled={!nullable}
                title={nullable ? "Set NULL (⌥⌘N in the grid)" : "This column is NOT NULL"}
              >
                Set NULL
              </Button>
              <Button size="sm" variant="ghost" onClick={cancel} title="Cancel (Esc)">
                Cancel
              </Button>
              <Button size="sm" variant="primary" onClick={() => finish("commit")} title="Apply (⌘↵)">
                Apply
              </Button>
            </span>
          </>
        }
        onClose={cancel}
      >
        <div className="h-full min-h-40">
          <CodeEditor
            ref={editor}
            initialText={initialText}
            dialect={null}
            language={kind === "json" ? "json" : "text"}
            onChange={() => {
              touched.current = true;
            }}
            onUnmount={(content) => {
              lastText.current = content.text;
            }}
            extensions={extensions}
          />
        </div>
      </DrawerFrame>
    </div>
  );
}
```

- [ ] **Step 7: Check the types and the build**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: PASS; no errors. (The editors are mounted by Task 9; their manual checks are Task 9's.)

- [ ] **Step 8: Commit**

```bash
git add src/lib/ime.ts src/features/editor/extensions.ts src/features/edits/editKeys.ts src/features/edits/editing.ts \
  src/features/edits/editing.test.ts src/features/edits/CellEditor.tsx src/features/edits/ValueEditor.tsx
git commit -m "feat(app): cell editors and editing keys — inline input and the drawer's value editor, NULL never from an untouched Return, IME-guarded"
```

---

### Task 9: Staging from the grid — `useTableEdits`, the edit bar, the Data view's wiring

**Suggested implementer:** Sonnet.

**Files:**
- Create: `src/features/edits/useTableEdits.tsx`, `src/features/edits/EditBar.tsx`
- Modify: `src/features/table-data/TableDataTab.tsx`

**Interfaces:**
- Consumes: Tasks 2–8 (the store's `useStagedEdits`, `dispatchStaged`; `changeCount`, `findChange`, `newChangeId`, `MAX_CHANGES`; `editText`, `sameCell`; the overlay's `bindRows`, `overlay`, `resultColumns`, `rowRefAt`, `displayIndexOf`, `wasText`; `savedEdits`; `fetchEditTarget`, `editTargetKey`, `editsErrorMessage`; `editStart`, `editorResult`, `nextWritable`; `gridEditKey`; `CellEditor`, `ValueEditor`; `GridEditing`, `HeaderMark`, `DataGridHandle.selectCell`); `useQuery`; `toast`.
- Produces:
  - `useTableEdits(input: TableEditsInput): TableEdits` with `TableEditsInput { tab; columns; rows; rowEditing; sessionOpen; locked: string | null; gridRef; onRequestCommit? }` and `TableEdits { target; targetError; checking; refetchTarget; editable; readOnlyReason; keyChanged; staged; changes; tooLarge; display; grid; onSelectionChange; resultRowAt(index); locate(changeId, column): CellPos | null; addRow; deleteSelected; setNullSelected; discardAll; undo; redo }`.
  - `EditBar(props: EditBarProps)` with `{ editable; checking; readOnlyReason; targetError; onRetryTarget; changes; notShown; tooLarge; keyChanged; unknownOutcome; onAddRow; onDiscard; onCommit?; commitDisabled }` (no Commit button until Task 10 passes `onCommit`).

Behaviour (spec §3.6.2–§3.6.6, R-05-2, D-05-11; rulings H3, H12, H13, H15, H20):
- The edit target is read once per table tab once its session is open (a password it needed has been given), with `retry: false`, cached until Refresh (which also refetches it: DDL since). Read-only reasons come from the target (view, no key, read-only connection, …); the edit bar shows "Read-only · <reason>" (the full reason in its tooltip), or "Editing unavailable: <why>" with Retry.
- The grid gets `edits` when the table is editable or something is staged; `editable` is false while the changes are on another key than the table's (H13: the bar says "Staged on another key") and while a commit runs (`locked`, Task 10).
- Return / F2 / double click edit the active cell (or say why not); ⌥⌘N sets NULL on the selection's writable, nullable cells (NOT NULL ones are skipped and counted in a toast); ⌘⌫ marks the selection's rows (all marked → restore); ⌘Z / ⇧⌘Z undo and redo; a selection over more than 10,000 rows is refused before anything is built (H15). The menus (§3.6.4): Edit value ↵, Set NULL ⌥⌘N, Use default (new rows), Revert cell, Keep mine (a conflicting row), Revert row, Delete rows / Restore rows ⌘⌫, Add row.
- + Row adds a new row at the top and opens its first writable column's editor (§3.6.3). Tab / ⇧Tab move along writable columns; the edited cell is always the active one, in view.
- Filter by this value uses the loaded (server) value of a changed cell and has no item on a new row (§3.6.6). Discard… asks when more than one change is staged ("Discard N staged changes to <table>? ⌘Z brings them back.").
- Closing a tab with staged changes does not ask yet (Task 11).

- [ ] **Step 1: Write `src/features/edits/useTableEdits.tsx`**

```tsx
import { useQuery } from "@tanstack/react-query";
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { toast } from "@/components/ui/Toast";
import type { ColumnMeta, EditColumn, EditKind, EditTarget } from "@/lib/api/types";
import { plural } from "@/lib/format";
import { dispatchStaged, useStagedEdits } from "@/stores/stagedEdits";
import type { TableTabState } from "@/stores/workspace";
import type { DataGridHandle, GridSelectionState } from "../grid/DataGrid";
import type { GridEditing } from "../grid/EditorSlot";
import type { HeaderMark } from "../grid/marks";
import type { CellPos, Rect } from "../grid/selection";
import { CellEditor, type EditEnd } from "./CellEditor";
import { editText, sameCell } from "./cells";
import { editorResult, editStart, nextWritable, type EditStart, type RowState } from "./editing";
import { editsErrorMessage, editTargetKey, fetchEditTarget } from "./editsApi";
import { gridEditKey } from "./editKeys";
import {
  bindRows,
  displayIndexOf,
  overlay,
  resultColumns,
  rowRefAt,
  wasText,
  type Binding,
  type Overlay,
  type OverlayCache,
} from "./overlay";
import { savedEdits } from "./savedEdits";
import {
  changeCount,
  findChange,
  MAX_CHANGES,
  newChangeId,
  type CellTarget,
  type LoadedRowRef,
  type RowRef,
  type StageAction,
  type StagedEdits,
} from "./staging";
import { ValueEditor } from "./ValueEditor";

/*
 * A table-data tab's grid editing (spec ⑤ §3.6): the edit target, the staged changes laid over the rows, the cell being
 * edited, the keys and the menus. Staged changes live in `stores/stagedEdits.ts`, never in React state; the cell being
 * edited is this hook's, and the text typed into it the editor's own (a keystroke renders the editor alone). Every
 * stage goes through `dispatchStaged`, which refuses more than MAX_CHANGES changes and new changes on another key
 * (rulings H13, H15).
 */

const TOO_MANY = `At most ${MAX_CHANGES.toLocaleString()} changes are committed at once: use SQL for this many rows`;
const KEY_CHANGED = "The table's key changed since these changes were staged: commit or discard them first";
const TOO_MANY_ROWS = `Select at most ${MAX_CHANGES.toLocaleString()} rows`;
const EMPTY_ROWS: unknown[][] = [];
const NO_COLUMNS: ColumnMeta[] = [];
const NO_KINDS: ReadonlyMap<string, EditKind> = new Map();

/** The cell being edited: where its edit started from, and its row and column. */
interface Editing extends EditStart {
  readonly ref: RowRef;
  readonly col: number;
}

export interface TableEditsInput {
  tab: TableTabState;
  /** The columns and rows the grid shows (held while a re-run loads); undefined before the first result. */
  columns: ColumnMeta[] | undefined;
  rows: unknown[][] | undefined;
  /** The engine edits rows (`row_editing`, CR-1). */
  rowEditing: boolean;
  /** The tab's session is open: the edit target is read then (a password it needed has been given). */
  sessionOpen: boolean;
  /** Why nothing may be staged now (a commit is running), or null. */
  locked: string | null;
  gridRef: RefObject<DataGridHandle | null>;
  /** ⌘S in an editor: the edit is staged, then this opens the preview. */
  onRequestCommit?: () => void;
}

export interface TableEdits {
  target: EditTarget | null;
  /** The target could not be read: the edit bar says why, with Retry. */
  targetError: string | null;
  /** The target is being read. */
  checking: boolean;
  refetchTarget: () => void;
  /** Rows can be edited now: the target is editable and the changes are on its key. */
  editable: boolean;
  /** Why rows cannot be edited (the target's reason), or null. */
  readOnlyReason: string | null;
  /** Changes were staged on another key than the table's now (H13). */
  keyChanged: boolean;
  staged: StagedEdits | null;
  changes: number;
  /** Too large to keep across a restart (`savedEdits`, B40). */
  tooLarge: boolean;
  display: Overlay;
  /** The grid's `edits`; undefined when nothing can be edited and nothing is staged. */
  grid: GridEditing | undefined;
  onSelectionChange: (state: GridSelectionState | null) => void;
  /** The loaded row at a display index (null for a new row): Filter by this value uses the server's value. */
  resultRowAt: (index: number) => unknown[] | null;
  /** Where a change (one of its columns, or its first) is shown now, or null (Show in grid). */
  locate: (changeId: string, column: string | null) => CellPos | null;
  addRow: () => void;
  deleteSelected: () => void;
  setNullSelected: () => void;
  discardAll: () => void;
  undo: () => void;
  redo: () => void;
}

/** The rows a rectangle covers, from its top; null beyond MAX_CHANGES rows (H15). */
function rowsOf(rect: Rect): number[] | null {
  const count = rect.bottom - rect.top + 1;
  return count > MAX_CHANGES ? null : Array.from({ length: count }, (_, i) => rect.top + i);
}

const sameKey = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((c, i) => c === b[i]);

export function useTableEdits(input: TableEditsInput): TableEdits {
  const { tab, rowEditing, sessionOpen, locked, gridRef } = input;
  const columns = input.columns ?? NO_COLUMNS;
  const rows = input.rows ?? EMPTY_ROWS;
  const staged = useStagedEdits(tab.id);

  // The target is read once per tab, on the metadata connection, and again on Refresh (spec §3.2.1).
  const targetQuery = useQuery({
    queryKey: editTargetKey(tab.profileId, tab.path),
    queryFn: ({ signal }) => fetchEditTarget(tab.profileId, tab.path, signal),
    enabled: rowEditing && sessionOpen,
    staleTime: Infinity,
    retry: false,
  });
  const target = targetQuery.data ?? null;
  const targetColumns = useMemo(
    () => new Map<string, EditColumn>((target?.columns ?? []).map((column) => [column.name, column])),
    [target],
  );
  const kinds = useMemo(
    () => (target ? new Map((target.columns ?? []).map((column) => [column.name, column.kind])) : NO_KINDS),
    [target],
  );
  const tableKey = target?.editable ? (target.key ?? null) : null;
  const keyChanged = !!staged && !!tableKey && changeCount(staged) > 0 && !sameKey(staged.keyColumns, tableKey);
  const editable = !!target?.editable && !keyChanged;
  const keyColumns = staged?.keyColumns ?? tableKey;
  const result = useMemo(() => resultColumns(columns, keyColumns ?? []), [columns, keyColumns]);

  const [editing, setEditing] = useState<Editing | null>(null);
  const selection = useRef<GridSelectionState | null>(null);
  const onSelectionChange = useCallback((state: GridSelectionState | null) => {
    selection.current = state;
  }, []);

  // Rows are keyed only while something is staged on loaded rows, or a loaded row's cell is edited (spec §3.6.2).
  const bindingRef = useRef<Binding | null>(null);
  const needsBinding =
    (!!staged && (Object.keys(staged.updates).length > 0 || Object.keys(staged.deletes).length > 0)) ||
    editing?.ref.kind === "row";
  const binding = useMemo(() => {
    if (!needsBinding || !result.keyIndexes) return null;
    bindingRef.current = bindRows(rows, result.keyIndexes, bindingRef.current);
    return bindingRef.current;
  }, [needsBinding, rows, result]);
  const cacheRef = useRef<OverlayCache | null>(null);
  const display = useMemo(() => {
    const drawn = overlay({ rows, columns, kinds, staged, binding, result }, cacheRef.current);
    cacheRef.current = drawn.cache;
    return drawn.overlay;
  }, [rows, columns, kinds, staged, binding, result]);

  const stageAction = (action: StageAction): boolean => {
    if (locked) {
      toast("info", locked);
      return false;
    }
    const outcome = dispatchStaged(tab.id, action, tableKey ?? undefined);
    if (outcome === "too_many") toast("warning", TOO_MANY);
    if (outcome === "key_changed") toast("warning", KEY_CHANGED);
    return outcome === "staged";
  };

  const resultRowAt = useCallback(
    (index: number): unknown[] | null => (index < display.leading ? null : (rows[index - display.leading] ?? null)),
    [display, rows],
  );
  const rowStateOf = (ref: RowRef): RowState => {
    if (ref.kind === "insert") return "new";
    if (staged && ref.rowKey in staged.deletes) return "deleted";
    const complete = Object.values(ref.key).every(
      (cell) => cell !== null && cell !== undefined && editText(cell).complete,
    );
    return complete ? "loaded" : "key_incomplete";
  };
  const stagedInput = (ref: RowRef, column: string) =>
    ref.kind === "insert"
      ? staged?.inserts.find((insert) => insert.id === ref.id)?.values[column]
      : staged?.updates[ref.rowKey]?.set[column];
  const cellAt = (pos: CellPos): CellTarget | null => {
    const ref = rowRefAt(display, staged, rows, pos.row, result);
    const column = columns[pos.col];
    return ref && column ? { row: ref, column: column.name, original: resultRowAt(pos.row)?.[pos.col] } : null;
  };

  /** Opens the editor on a cell, or says why it cannot be edited. */
  const beginEdit = (pos: CellPos) => {
    if (!editable) return;
    if (locked) {
      toast("info", locked);
      return;
    }
    const cell = cellAt(pos);
    if (!cell) return;
    const start = editStart({
      column: targetColumns.get(cell.column),
      name: cell.column,
      row: rowStateOf(cell.row),
      staged: stagedInput(cell.row, cell.column),
      original: cell.original,
    });
    if ("refuse" in start) toast("info", start.refuse);
    else setEditing({ ...start, ref: cell.row, col: pos.col });
  };

  const editingPos = editing ? displayIndexOf(display, staged, binding, editing.ref) : -1;
  const latest = useRef({ editing, editingPos, onRequestCommit: input.onRequestCommit });
  useLayoutEffect(() => {
    latest.current = { editing, editingPos, onRequestCommit: input.onRequestCommit };
    // The edited row left the rows shown (a re-run, a sort): the editor closes, and stages what was typed (H12).
    if (editing && editingPos < 0) setEditing(null);
  });
  // The edited cell is the active one, and in view (Return, Tab, Add row).
  useLayoutEffect(() => {
    if (editing && editingPos >= 0) gridRef.current?.selectCell(editingPos, editing.col);
  }, [editing, editingPos, gridRef]);

  const names = useMemo(() => columns.map((column) => column.name), [columns]);
  const writable = (name: string) => !!targetColumns.get(name)?.writable;

  /** The editor ended: stage what was typed (H4), then close, move along the row, or open the preview. */
  const endEdit = (text: string | null, how: EditEnd) => {
    const { editing: current, editingPos: pos, onRequestCommit } = latest.current;
    if (!current) return;
    const value = editorResult(text ?? "", text !== null);
    if (value) {
      stageAction({
        type: "edit",
        cell: { row: current.ref, column: current.column, original: current.original },
        input: value,
      });
    }
    if (how === "next" || how === "previous") {
      const col = nextWritable(names, writable, current.col, how === "next" ? 1 : -1);
      if (col >= 0 && pos >= 0) {
        setEditing(null); // batched with the next cell's edit; it stays closed if that one is refused
        beginEdit({ row: pos, col });
        return;
      }
    }
    setEditing(null);
    if (how !== "blur") gridRef.current?.focus();
    if (how === "save") onRequestCommit?.(); // it reads the staged edits from the store: the value is in them already
  };
  const cancelEdit = () => {
    setEditing(null);
    gridRef.current?.focus();
  };
  const setNullInEditor = () => {
    const current = latest.current.editing;
    if (!current) return;
    if (!current.nullable) toast("info", `${current.column} is NOT NULL`);
    else
      stageAction({
        type: "setNull",
        cells: [{ row: current.ref, column: current.column, original: current.original }],
      });
    cancelEdit();
  };

  /** The writable cells of a rectangle; with `nullable`, NOT NULL ones are counted apart. Null: too many rows. */
  const cellsOf = (rect: Rect | null, nullable: boolean): { cells: CellTarget[]; notNull: number } | null => {
    if (!rect) return { cells: [], notNull: 0 };
    const indexes = rowsOf(rect);
    if (!indexes) return null;
    const cells: CellTarget[] = [];
    let notNull = 0;
    for (const row of indexes) {
      for (let col = rect.left; col <= rect.right; col += 1) {
        const cell = cellAt({ row, col });
        const column = cell && targetColumns.get(cell.column);
        if (!cell || !column?.writable) continue;
        const state = rowStateOf(cell.row);
        if (state === "deleted" || state === "key_incomplete") continue;
        if (nullable && !column.nullable) notNull += 1;
        else cells.push(cell);
      }
    }
    return { cells, notNull };
  };
  const rowRefsOf = (rect: Rect | null): RowRef[] | null => {
    if (!rect) return [];
    const indexes = rowsOf(rect);
    if (!indexes) return null;
    return indexes.flatMap((index) => {
      const ref = rowRefAt(display, staged, rows, index, result);
      return ref && rowStateOf(ref) !== "key_incomplete" ? [ref] : [];
    });
  };

  const setNullIn = (rect: Rect | null) => {
    const picked = cellsOf(rect, true);
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.cells.length) stageAction({ type: "setNull", cells: picked.cells });
    if (picked.notNull) toast("info", `Skipped ${plural(picked.notNull, "cell")} of NOT NULL columns`);
  };
  const deleteIn = (rect: Rect | null) => {
    const picked = rowRefsOf(rect);
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.length) stageAction({ type: "deleteRows", rows: picked });
  };
  const addRow = () => {
    if (!editable) return;
    const id = newChangeId("i");
    if (!stageAction({ type: "addRow", id })) return;
    // The new row is at the top: its first writable column opens for typing (spec §3.6.3).
    const col = nextWritable(names, writable, -1, 1);
    const column = col >= 0 ? targetColumns.get(names[col]) : undefined;
    if (!column) return;
    const start = editStart({ column, name: column.name, row: "new", staged: undefined, original: undefined });
    if (!("refuse" in start)) setEditing({ ...start, ref: { kind: "insert", id }, col });
  };

  /** The server's cells of a loaded row now, by name (Keep mine). */
  const currentCells = (ref: LoadedRowRef): Record<string, unknown> | null => {
    const index = binding?.byKey.get(ref.rowKey);
    const row = index === undefined ? undefined : rows[index];
    return row ? Object.fromEntries(names.map((name, i) => [name, row[i]])) : null;
  };
  const hasConflict = (ref: LoadedRowRef): boolean => {
    const update = staged?.updates[ref.rowKey];
    const now = update ? currentCells(ref) : null;
    return !!update && !!now && Object.keys(update.set).some((c) => c in now && !sameCell(now[c], update.original[c]));
  };

  const menu: GridEditing["menu"] = (pos, cell, rect) => {
    const refs = rowRefsOf(rect) ?? [];
    const at = pos ? cellAt(pos) : null;
    const allDeleted = refs.length > 0 && refs.every((ref) => rowStateOf(ref) === "deleted");
    const rowsChanged = refs.some(
      (ref) => ref.kind === "insert" || (!!staged && (ref.rowKey in staged.updates || ref.rowKey in staged.deletes)),
    );
    const items: MenuItem[] = [];
    if (cell && pos && at) {
      items.push(
        { label: "Edit value", shortcut: "↵", onSelect: () => beginEdit(pos) },
        { label: "Set NULL", shortcut: "⌥⌘N", onSelect: () => setNullIn(rect) },
      );
      if (at.row.kind === "insert") {
        items.push({
          label: "Use default",
          onSelect: () => stageAction({ type: "useDefault", cells: cellsOf(rect, false)?.cells ?? [] }),
        });
      }
      items.push({
        label: "Revert cell",
        disabled: !stagedInput(at.row, at.column),
        onSelect: () => {
          const cells = cellsOf(rect, false)?.cells;
          stageAction({ type: "revertCells", cells: cells?.length ? cells : [at] });
        },
      });
      const row = at.row;
      if (row.kind === "row" && hasConflict(row)) {
        items.push({
          label: "Keep mine",
          title: "Check the staged values against the server's values now",
          onSelect: () => {
            const now = currentCells(row);
            if (now) stageAction({ type: "keepMine", row, current: now });
          },
        });
      }
    }
    items.push(
      { label: "Revert row", disabled: !rowsChanged, onSelect: () => stageAction({ type: "revertRows", rows: refs }) },
      {
        label: allDeleted ? "Restore rows" : "Delete rows",
        shortcut: "⌘⌫",
        danger: !allDeleted,
        onSelect: () => deleteIn(rect),
      },
      { label: "Add row", onSelect: addRow, separatorBefore: true },
    );
    return items;
  };

  const onKey: GridEditing["onKey"] = (e, current, rect) => {
    const action = gridEditKey(e.nativeEvent);
    if (!action) return false;
    e.preventDefault();
    if (action === "edit") {
      if (current) beginEdit(current.focus);
    } else if (action === "setNull") setNullIn(rect);
    else if (action === "deleteRows") deleteIn(rect);
    else stageAction({ type: action });
    return true;
  };

  const headerMarks = useMemo<readonly (HeaderMark | undefined)[]>(() => {
    if (!target?.editable) return [];
    const key = new Set(target.key ?? []);
    return names.map((name) => {
      const meta = targetColumns.get(name);
      const readOnly = meta && !meta.writable ? (meta.read_only_reason ?? "it cannot be written") : null;
      return key.has(name) || readOnly ? { key: key.has(name), readOnly } : undefined;
    });
  }, [target, names, targetColumns]);

  const editor: GridEditing["editor"] =
    editing && editingPos >= 0
      ? {
          pos: { row: editingPos, col: editing.col },
          mode: editing.mode,
          render: (width) => {
            const key = `${editing.ref.kind === "insert" ? editing.ref.id : editing.ref.rowKey}:${editing.column}`;
            const meta = columns[editing.col];
            return editing.mode === "inline" ? (
              <CellEditor
                key={key}
                initialText={editing.initialText}
                placeholder={editing.placeholder}
                width={width}
                numeric={meta.category === "number"}
                onDone={endEdit}
                onCancel={cancelEdit}
                onSetNull={setNullInEditor}
              />
            ) : (
              <ValueEditor
                key={key}
                column={meta}
                kind={editing.kind}
                initialText={editing.initialText}
                placeholder={editing.placeholder}
                was={editing.ref.kind === "row" ? wasText(editing.original, meta.category) : null}
                nullable={editing.nullable}
                onDone={endEdit}
                onCancel={cancelEdit}
                onSetNull={setNullInEditor}
              />
            );
          },
        }
      : null;

  const changes = staged ? changeCount(staged) : 0;
  const grid: GridEditing | undefined =
    editable || changes > 0
      ? {
          editable: editable && !locked,
          marks: display.marks,
          leading: display.leading,
          headerMarks,
          onEdit: beginEdit,
          onKey,
          menu,
          editor,
        }
      : undefined;

  const locate = (changeId: string, column: string | null): CellPos | null => {
    const found = staged ? findChange(staged, changeId) : null;
    if (!found) return null;
    const ref: RowRef =
      found.kind === "insert"
        ? { kind: "insert", id: found.change.id }
        : { kind: "row", rowKey: found.rowKey, key: found.change.key };
    const row = displayIndexOf(display, staged, binding, ref);
    if (row < 0) return null;
    const name = column ?? (found.kind === "update" ? Object.keys(found.change.set)[0] : null);
    return { row, col: name ? (result.byName.get(name) ?? 0) : 0 };
  };

  return {
    target,
    targetError: targetQuery.error ? editsErrorMessage(targetQuery.error) : null,
    checking: rowEditing && sessionOpen && targetQuery.isPending,
    refetchTarget: () => void targetQuery.refetch(),
    editable,
    readOnlyReason: target && !target.editable ? (target.reason ?? "Rows of this table cannot be edited") : null,
    keyChanged,
    staged,
    changes,
    tooLarge: savedEdits(staged).tooLarge,
    display,
    grid,
    onSelectionChange,
    resultRowAt,
    locate,
    addRow,
    deleteSelected: () => deleteIn(selection.current?.rect ?? null),
    setNullSelected: () => setNullIn(selection.current?.rect ?? null),
    discardAll: () => void stageAction({ type: "discardAll" }),
    undo: () => void stageAction({ type: "undo" }),
    redo: () => void stageAction({ type: "redo" }),
  };
}
```

- [ ] **Step 2: Write `src/features/edits/EditBar.tsx`**

```tsx
import { AlertTriangle, Lock, Plus } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { plural } from "@/lib/format";

export interface EditBarProps {
  editable: boolean;
  /** The target is being read: nothing is said yet. */
  checking: boolean;
  /** Why rows cannot be edited (the table's), or null. */
  readOnlyReason: string | null;
  /** The target could not be read, or null. */
  targetError: string | null;
  onRetryTarget: () => void;
  changes: number;
  /** Changes on rows the grid does not show (filtered out, not loaded). */
  notShown: number;
  /** The staged changes are too large to keep across a restart. */
  tooLarge: boolean;
  /** The changes were staged on another key than the table's. */
  keyChanged: boolean;
  /** The last commit's outcome is unknown and fresh rows have not been read yet. */
  unknownOutcome: boolean;
  onAddRow: () => void;
  /** Discard…: the tab asks first when there is more than one change. */
  onDiscard: () => void;
  /** Commit… (⌘S); absent: no Commit button. */
  onCommit?: () => void;
  /** Why Commit… is disabled now, or null. */
  commitDisabled: string | null;
}

const NOTE = "flex min-w-0 items-center gap-1 truncate";

/**
 * The Data view's edit controls, at the right of its toolbar (spec ⑤ §3.6.5): + Row, the change count, Commit… and
 * Discard…, or "Read-only · <reason>". It never scrolls (the user's Mac shows classic scrollbars, which would cover a
 * strip this high): what does not fit is truncated, the whole text in its tooltip.
 */
export function EditBar(props: EditBarProps) {
  const { editable, checking, readOnlyReason, targetError, changes, notShown, tooLarge, keyChanged } = props;
  if (checking) return null;
  return (
    <div className="flex min-w-0 shrink items-center gap-1.5 overflow-hidden text-[12px]">
      {targetError && (
        <span className={`${NOTE} text-danger`} title={targetError} data-selectable>
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">Editing unavailable: {targetError}</span>
          <Button size="sm" className="shrink-0" onClick={props.onRetryTarget}>
            Retry
          </Button>
        </span>
      )}
      {readOnlyReason && (
        <span className={`${NOTE} text-muted`} title={readOnlyReason}>
          <Lock size={12} className="shrink-0" />
          <span className="truncate">Read-only · {readOnlyReason}</span>
        </span>
      )}
      {keyChanged && (
        <span
          className={`${NOTE} text-warning`}
          title="The table's key changed since these changes were staged: commit or discard them"
        >
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">Staged on another key</span>
        </span>
      )}
      {editable && (
        <Button size="sm" className="shrink-0" onClick={props.onAddRow} title="Add a row (⌘⇧P: Add row)">
          <Plus size={12} /> Row
        </Button>
      )}
      {changes > 0 && (
        <>
          <span
            className={`${NOTE} font-medium text-warning`}
            title={
              notShown ? `${plural(notShown, "change")} on rows not shown (filtered out or not loaded)` : undefined
            }
          >
            {plural(changes, "change")}
            {notShown > 0 && <span className="font-normal text-muted"> · {notShown} in rows not shown</span>}
          </span>
          {tooLarge && (
            <span
              className="shrink-0 text-warning"
              title="Too large to keep across a restart: commit them, or keep DbaGuy open"
            >
              <AlertTriangle size={12} aria-label="Too large to keep across a restart" />
            </span>
          )}
          {props.unknownOutcome && (
            <span
              className={`${NOTE} text-warning`}
              title="The last commit's outcome is unknown: Refresh to see the table as it is now"
            >
              Outcome unknown: Refresh
            </span>
          )}
          {props.onCommit && (
            <Button
              size="sm"
              variant="primary"
              className="shrink-0"
              onClick={props.onCommit}
              disabled={props.commitDisabled !== null}
              title={props.commitDisabled ?? "Preview the statements, then commit (⌘S)"}
            >
              Commit… ⌘S
            </Button>
          )}
          <Button size="sm" variant="ghost" className="shrink-0" onClick={props.onDiscard}>
            Discard…
          </Button>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Wire it into the Data view**

```diff
diff --git a/src/features/table-data/TableDataTab.tsx b/src/features/table-data/TableDataTab.tsx
index 1d29b6d..55eda8e 100644
--- a/src/features/table-data/TableDataTab.tsx
+++ b/src/features/table-data/TableDataTab.tsx
@@ -1,6 +1,7 @@
 import { ChevronDown, ChevronRight, Loader2, RefreshCw, SquareTerminal } from "lucide-react";
 import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
 import { Button } from "@/components/ui/Button";
+import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
 import type { MenuItem } from "@/components/ui/ContextMenu";
 import { Tabs } from "@/components/ui/Tabs";
 import { toast } from "@/components/ui/Toast";
@@ -9,11 +10,13 @@ import type { ColumnMeta, Dialect } from "@/lib/api/types";
 import { cn } from "@/lib/cn";
 import { plural } from "@/lib/format";
 import { useWorkspace, type TableTabState } from "@/stores/workspace";
-import { useDialect, useProfiles } from "../connections/hooks";
+import { useEngineInfo, useProfiles } from "../connections/hooks";
 import { RecoverableError } from "../connections/RecoverableError";
 import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
 import { kindLabel } from "../explorer/kinds";
-import { DataGrid } from "../grid/DataGrid";
+import { EditBar } from "../edits/EditBar";
+import { useTableEdits } from "../edits/useTableEdits";
+import { DataGrid, type DataGridHandle } from "../grid/DataGrid";
 import { ExportDialog } from "../grid/ExportDialog";
 import type { GridSort } from "../grid/GridHeader";
 import { useHeldResult } from "../grid/useHeldResult";
@@ -152,7 +155,8 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   const table = useTableData(tab, active);
   const { state, execution, snapshot, relation } = table;
   const { applyFilter, toggleSort, sortBy } = table;
-  const dialect = useDialect(tab.profileId);
+  const engine = useEngineInfo(tab.profileId);
+  const dialect = engine?.dialect ?? null;
   const openQuery = useWorkspace((s) => s.openQuery);
   const profiles = useProfiles();
   const profileName = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.name;
@@ -172,6 +176,23 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   const filterByValue = useFilterByValue(state.filter, applyFilter);
   // An export runs on this session: filter, sort and refresh would only wait behind it.
   const exporting = isExporting(snapshot);
+  const gridRef = useRef<DataGridHandle>(null);
+  const edits = useTableEdits({
+    tab,
+    columns,
+    rows,
+    rowEditing: engine?.row_editing === true,
+    sessionOpen: phase === "open",
+    locked: null,
+    gridRef,
+  });
+  const [askingDiscard, setAskingDiscard] = useState(false);
+  /** Discard…: more than one change asks first (⌘Z brings them back either way). */
+  const requestDiscard = () => (edits.changes > 1 ? setAskingDiscard(true) : edits.discardAll());
+  const refresh = () => {
+    table.refresh();
+    edits.refetchTarget(); // DDL since: the next edit uses the table as it is now (spec §3.2.1)
+  };
 
   const gridSort = useMemo<GridSort[]>(
     () =>
@@ -192,16 +213,19 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
     (col: number, descending: boolean) => columns && sortBy(columns[col].name, descending),
     [columns, sortBy],
   );
+  const { resultRowAt } = edits;
+  const { leading } = edits.display;
   const extraCellMenu = useCallback(
     (row: number, col: number): MenuItem[] => {
+      if (row < leading) return []; // a new row has no value on the server to filter by (§3.6.6)
       const column = columns?.[col];
-      if (!column || !rows || row >= rows.length || !dialect || exporting) {
-        return [{ label: FILTER_BY_VALUE, disabled: true }];
-      }
-      const value = rows[row][col];
+      // The loaded row under the display row: a changed cell filters by the server's value, not the staged one.
+      const source = resultRowAt(row);
+      if (!column || !source || !dialect || exporting) return [{ label: FILTER_BY_VALUE, disabled: true }];
+      const value = source[col];
       return [{ label: FILTER_BY_VALUE, onSelect: () => void filterByValue(dialect, column, value) }];
     },
-    [columns, rows, dialect, exporting, filterByValue],
+    [columns, resultRowAt, leading, dialect, exporting, filterByValue],
   );
   // Export writes the current browse SELECT (filter, sort and refresh wait while it runs).
   const exportSource = useMemo(
@@ -242,7 +266,7 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
         ) : (
           <Button
             size="sm"
-            onClick={table.refresh}
+            onClick={refresh}
             disabled={phase !== "open" || exporting}
             title={exporting ? "An export is running on this session" : "Run the query again"}
           >
@@ -255,6 +279,23 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
           onCount={table.countRows}
           onCancel={table.cancelCount}
         />
+        <div className="ml-auto flex min-w-0 justify-end">
+          <EditBar
+            editable={edits.editable}
+            checking={edits.checking}
+            readOnlyReason={edits.readOnlyReason}
+            targetError={edits.targetError}
+            onRetryTarget={edits.refetchTarget}
+            changes={edits.changes}
+            notShown={edits.display.notShown}
+            tooLarge={edits.tooLarge}
+            keyChanged={edits.keyChanged}
+            unknownOutcome={edits.staged?.unknownOutcome === "pending"}
+            onAddRow={edits.addRow}
+            onDiscard={requestDiscard}
+            commitDisabled={null}
+          />
+        </div>
       </div>
       <RecoverableError profileId={tab.profileId} error={snapshot?.error ?? null} onRetry={table.retry} />
       {state.error && (
@@ -273,8 +314,9 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
       <div className="min-h-0 flex-1">
         {set ? (
           <DataGrid
+            ref={gridRef}
             columns={set.columns}
-            rows={set.rows}
+            rows={edits.display.rows}
             hasMore={set.hasMore}
             loadingMore={execution?.status === "running"}
             onLoadMore={phase === "open" ? table.loadMore : undefined}
@@ -287,12 +329,25 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
             onSortBy={exporting ? undefined : onSortBy}
             extraCellMenu={extraCellMenu}
             onExport={exportSource ? openExport : undefined}
+            edits={edits.grid}
+            onSelectionChange={edits.onSelectionChange}
           />
         ) : (
           placeholder && <p className="p-4 text-[12px] text-muted">{placeholder}</p>
         )}
       </div>
       {exportSource && <ExportDialog source={exportSource} open={exportOpen} onOpenChange={setExportOpen} />}
+      <ConfirmDialog
+        open={askingDiscard}
+        onOpenChange={setAskingDiscard}
+        title="Discard staged changes"
+        message={`Discard ${plural(edits.changes, "staged change")} to ${relation?.name ?? tab.title}? ⌘Z brings them back.`}
+        confirmLabel="Discard"
+        onConfirm={() => {
+          setAskingDiscard(false);
+          edits.discardAll();
+        }}
+      />
       {guards.dialog}
     </div>
   );
```

- [ ] **Step 4: Check the types and the build**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: PASS; no errors.

- [ ] **Step 5: Manual check list** (the coordinator, `scripts/dev.sh --web`; `el` is the grid's scroll area as in Task 6; nothing is committed in this list):
  1. `backend/.venv/bin/python scripts/scratch_tables.py create`.
  2. dev pg › zz_05b_items: the edit bar shows "+ Row"; the `id` header has a key icon; `total`'s name is muted and its tooltip ends "Read-only: generated column".
  3. Row 1's `label`: Return → an input over the cell holding "row 1", selected; type "first", Return → the cell shows "first" on the warning tint, its tooltip says "Was: row 1"; the bar says "1 change"; the tab shows a dot.
  4. Row 2's `note` (NULL): Return → an empty input with the placeholder NULL; Return without typing → still NULL, still "1 change". Return, type "x", erase it, Return → the cell is empty (not NULL), tooltip "Was: NULL", "2 changes".
  5. Row 3's `note` (1,500 characters): Return → the drawer's value editor with the text and "Was: …"; type " more" at the end, ⌘↵ → staged, the drawer closes, "3 changes". Row 5's `note` (two lines): Return → the drawer; Esc → nothing staged.
  6. Row 7's `note` (70,000 characters): Return → toast "This value is too large to edit here (70,000 characters); Set NULL or use SQL"; ⌥⌘N → NULL staged.
  7. Row 9's `data` (100 bytes): Return → an empty input, placeholder "Hex to replace all 100 bytes"; type `0xdeadbeef`, Return → the cell shows `0xdeadbeef (4 B)`.
  8. Row 11's `big`: Return → the input holds `9007199254740993`; type `9007199254740995`, Return → the cell shows exactly that, aligned right. Row 12's `flag`: type `yes` → the cell shows `true`.
  9. Row 13's `total`: Return → toast "total is read-only: generated column".
  10. Row 14's `label`: Return, type "a", Tab → `note`'s editor opens (label staged); ⇧Tab → back to `label`. Click another cell while editing → what was typed is staged.
  11. + Row: a row at the top with `+`, the success tint, every cell "DEFAULT" in grey italics, the first column's editor open (placeholder DEFAULT); Esc. Its `label`: Return, type "new one", Return. Right-click one of its cells: "Use default" is there; use it on `label` → DEFAULT again.
  12. Select rows 20–22 by their row numbers, ⌘⌫ → struck through, danger tint, row numbers `−`; ⌘⌫ again → restored.
  13. Row 1 (staged "first"): ⌘⌫ → struck through showing "row 1"; ⌘⌫ again → "first" again.
  14. ⌘Z several times → the last actions undone one at a time; ⇧⌘Z → redone.
  15. Right-click a changed cell: the menu starts Edit value ↵, Set NULL ⌥⌘N, Revert cell, Revert row, Delete rows ⌘⌫, Add row, then Copy…, and "View value" has no ↵. Revert cell → the loaded value back.
  16. Right-click a changed cell → Filter by this value: the filter uses the loaded value (`label = 'row 1'`-style) and the row keeps its staged mark. Clear the filter.
  17. With some changes on rows below 100, filter `id > 100`: the bar says "N changes · M in rows not shown". Clear it.
  18. Sort by `label` descending: the staged rows keep their marks where they now are. Sort back.
  19. Load all 1,200 rows, stage a change on row 1,100, Refresh → the 1,200 rows are read again, the position kept, and row 1,100 is still marked.
  20. Discard… → "Discard N staged changes to zz_05b_items? ⌘Z brings them back." → Discard → no change left; ⌘Z → they are back.
  21. Conflict: stage row 30's `label` → "mine"; `backend/.venv/bin/python scripts/scratch_tables.py sql pg "UPDATE public.zz_05b_items SET label = 'theirs' WHERE id = 30"`; Refresh → the cell still shows "mine" with a danger corner, its tooltip includes "Changed on the server: now theirs"; right-click → Keep mine → the corner goes.
  22. Reload the page (the pane's reload) with 3 changes staged: the tab comes back with its dot and the same 3 marks on the same rows; `dev/data/workspace.json` holds the tab's `staged_edits` (`"version": 1`, `"key_columns": ["id"]`).
  23. zz_05b_view: "Read-only · Views are read-only here"; Return opens the value viewer. zz_05b_nokey: "Read-only · No primary key or unique key on NOT NULL columns: a row cannot be identified". dev pg read-only › zz_05b_items: "Read-only · This connection is read-only".
  24. dev mysql › zz_05b_items: `flag` (TINYINT(1)) edits as a number (type `1`); `payload` (JSON) opens the drawer with JSON highlighting; `total` is read-only. zz_05b_myisam: editable (the warning comes with the preview, Task 10).
  25. dev mssql › zz_05b_items: `id` Return → "id is read-only: IDENTITY column"; `code` (varchar) edits.
  26. zz_05b sqlite › zz_05b_items: edits; `total` read-only.
  27. In the inline editor, type a lone `(` and a `"`: nothing is added after them; in the drawer editor of a text column too (H10).
  28. Q1 (spec §1, §7): zz_05b_wide, load all 100,000 rows (the console loop of Task 6 check 6, repeated until the status bar says "100,000 rows"); `window.__gridRowRenders = []`; on a visible row: Return, type "x", Return; `new Set(window.__gridRowRenders)` holds only that row's index. Scrolling stays smooth with the change staged. Record the numbers for Task 12.
  29. Discard everything staged on every tab, close the tabs, then `backend/.venv/bin/python scripts/scratch_tables.py drop && backend/.venv/bin/python scripts/scratch_tables.py check` → "No zz_05b_ object left".

- [ ] **Step 6: Commit**

```bash
git add src/features/edits/useTableEdits.tsx src/features/edits/EditBar.tsx src/features/table-data/TableDataTab.tsx
git commit -m "feat(app): stage grid edits in table-data tabs — edit, NULL, DEFAULT, add, delete, revert, keep mine, undo; the edit bar"
```

---

### Task 10: Preview and commit — the dialog, the apply, the outcome, the re-read

**Suggested implementer:** Opus.

**Files:**
- Create: `src/features/edits/PreviewDialog.tsx`, `src/features/edits/useCommit.tsx`
- Modify: `src/features/table-data/TableDataTab.tsx`

**Interfaces:**
- Consumes: Task 5 (`SessionController.applyEdits`; `commitState`, `stagedOutcome`, `rerunAfter`, `commitReport`, `commitToast`, `NOT_SENT_REPORT`, `problemsOf`, `changeLabel`, `UNKNOWN_BEFORE`, `CommitAction`, `EndedCommit`; `previewEdits`, `editsErrorMessage`); Task 6 (`useTableData`'s `refresh`, `deliveredRequest`); Task 8 (`STABLE_SCROLLBAR`); Task 9 (`TableEdits`: `staged`, `target`, `readOnlyReason`, `locate`; `onRequestCommit`, `locked`); the store's `readStagedEdits`, `dispatchStaged`; `refreshHistory`; `RecoverableError`, `isPasswordRequired`; `profileColor`; `CopyButton`, `Dialog`.
- Produces:
  - `PreviewDialog.tsx`: `type CommitDialogState = { step: "previewing" } | { step: "failed"; error; message } | { step: "preview"; preview; staging; key } | { step: "applying"; preview; key; requestId; browseWasPaused } | { step: "result"; preview; key; report; messages }`; `PreviewDialog(props: PreviewDialogProps)`.
  - `useCommit.tsx`: `useCommit(input: CommitInput): Commit` with `CommitInput { tab; table; profile; dialect; controller; snapshot; browse: { execution; pending; deliveredRequest; refresh }; edits; exporting; gridRef; retrySession }` and `Commit { open(); disabledReason: string | null; applying: boolean; dialog: ReactNode }`.

Behaviour (spec §3.6.5, R-05-6, R-05-7, 05a M7 and carries; rulings H6, H8, H24):
- ⌘S (anywhere in the shown Data view, never in a dialog, never during composition), Commit… and an editor's ⌘S (which stages first) open the dialog when something is staged ("No changes to commit" otherwise) and Commit is allowed: not while a commit runs, the session is not open, an export runs, the rows are loading (stale), or the last outcome is unknown and fresh rows were not read ("…Refresh first"); not for a table that cannot be edited.
- The dialog first shows "Building the statements…", then the preview: the connection's name and colour (a red connection's Commit is a danger button), the counts with the deletes first in danger, the warnings (a table that cannot roll back, the columns each UPDATE does not check with their reasons, new rows missing required values, and after an unknown outcome UNKNOWN_BEFORE), the statements in a read-only SQL editor exactly as `begin`, each statement, `commit` (a U / I / D gutter mark on each statement's first line; its scrollbar reserved), Copy SQL (the `script`), and "Runs in one transaction: …" (or the non-transactional note). With problems there is no Commit: each problem with its row and Show in grid. A preview's problems mark the cells (`setProblems`). A failed preview shows its text (`editsErrorMessage`); a missing password asks in the profile's unlock dialog and previews again once given.
- Commit applies that preview's plan only (M7): the plan id lives in the dialog's state alone, dropped when it closes; a preview answer that arrives after the dialog closed is ignored; if the staging changed since the preview (identity of its parts), Commit previews again instead (H8). While applying: "Committing… statement k of n" from `affected`, Cancel until every statement's count is in (then COMMIT is on its way and Cancel no longer reaches the driver), no Close, Escape ignored; staging is locked (`locked`, a render later, H8).
- The outcome goes into the staging (`applyOutcome`), History is refreshed, the apply's execution is forgotten. Committed: the dialog closes, the toast (`commitToast`; a warning message, e.g. a non-transactional COMMIT's, as a second toast), the browse re-read keeping its position (D-05-12). Otherwise the result view: title, the failed row and the server's text, the failed statement marked red and scrolled to, the notes, trigger messages, and the actions — Show in grid (closes and selects the cell; "not among the rows shown" when it is not), Preview again, Roll back (the session's ROLLBACK; "preview the changes again"), Reconnect (a new session), Refresh. Partial re-reads at once; a rollback re-reads only when it ended a paused browse; unknown waits for the user's Refresh, and Commit stays off until a browse request other than the one shown at the outcome has delivered rows (the user's Refresh, or the browse sent again on a new session).

- [ ] **Step 1: Write `src/features/edits/PreviewDialog.tsx`**

```tsx
import { RangeSetBuilder, type Extension } from "@codemirror/state";
import { Decoration, EditorView, gutter, GutterMarker } from "@codemirror/view";
import { Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { Dialog } from "@/components/ui/Dialog";
import type { Dialect, EditPreview, Profile } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { profileColor } from "../connections/profileColors";
import { RecoverableError } from "../connections/RecoverableError";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { STABLE_SCROLLBAR } from "../editor/extensions";
import { changeLabel, UNKNOWN_BEFORE, type CommitAction, type CommitReport } from "./commitFlow";
import type { StagedEdits, Staging } from "./staging";

/** The commit dialog's steps (spec ⑤ §3.6.5). The plan id lives only in `preview`: no other preview's is ever applied. */
export type CommitDialogState =
  | { step: "previewing" }
  | { step: "failed"; error: unknown; message: string }
  | { step: "preview"; preview: EditPreview; staging: Staging; key: number }
  | { step: "applying"; preview: EditPreview; key: number; requestId: string; browseWasPaused: boolean }
  | {
      step: "result";
      preview: EditPreview | null;
      key: number;
      report: CommitReport;
      messages: readonly { severity: string; text: string }[];
    };

const TRANSACTIONAL_NOTE =
  "Runs in one transaction: if any statement fails or does not match exactly one row, nothing is applied.";
const NON_TRANSACTIONAL_NOTE =
  "This table cannot roll back: if a statement fails, the statements before it stay applied.";
const LIST = "max-h-28 overflow-y-auto rounded-md border border-border [scrollbar-gutter:stable]";
const TONES = { success: "text-success", warning: "text-warning", danger: "text-danger" } as const;

/** U, I or D before each statement's first line. */
class KindMarker extends GutterMarker {
  constructor(readonly letter: "U" | "I" | "D") {
    super();
  }
  override eq(other: GutterMarker): boolean {
    return other instanceof KindMarker && other.letter === this.letter;
  }
  override toDOM(): Node {
    const mark = document.createElement("span");
    mark.textContent = this.letter;
    mark.className = `cm-edit-kind-${this.letter}`;
    return mark;
  }
}
const LETTERS = { update: "U", insert: "I", delete: "D" } as const;
const MARKERS = { U: new KindMarker("U"), I: new KindMarker("I"), D: new KindMarker("D") };
const KIND_THEME = EditorView.theme({
  ".cm-edit-kind-U": { color: "var(--color-warning)", fontWeight: "600" },
  ".cm-edit-kind-I": { color: "var(--color-success)", fontWeight: "600" },
  ".cm-edit-kind-D": { color: "var(--color-danger)", fontWeight: "600" },
  ".cm-failed-statement": { backgroundColor: "color-mix(in oklab, var(--color-danger) 16%, transparent)" },
});

/** The text shown: the frame and each statement as sent, one after the other, and where each statement starts. */
function layout(preview: EditPreview): { text: string; starts: number[] } {
  const parts = [preview.begin];
  const starts: number[] = [];
  let at = preview.begin.length + 1;
  for (const statement of preview.statements) {
    starts.push(at);
    parts.push(statement.sql);
    at += statement.sql.length + 1;
  }
  parts.push(preview.commit);
  return { text: parts.join("\n"), starts };
}

function statementExtensions(preview: EditPreview, starts: number[], failed: number | null): Extension[] {
  const kinds = new Map(starts.map((start, i) => [start, MARKERS[LETTERS[preview.statements[i].kind]]]));
  const extensions: Extension[] = [
    gutter({
      class: "cm-edit-kinds",
      lineMarker: (_view, line) => kinds.get(line.from) ?? null,
      initialSpacer: () => MARKERS.U,
    }),
    KIND_THEME,
    STABLE_SCROLLBAR,
  ];
  if (failed !== null && starts[failed] !== undefined) {
    const from = starts[failed];
    const to = from + preview.statements[failed].sql.length;
    const line = Decoration.line({ class: "cm-failed-statement" });
    extensions.push(
      EditorView.decorations.of((view) => {
        const builder = new RangeSetBuilder<Decoration>();
        for (let pos = from; pos <= to;) {
          const current = view.state.doc.lineAt(pos);
          builder.add(current.from, current.from, line);
          pos = current.to + 1;
        }
        return builder.finish();
      }),
    );
  }
  return extensions;
}

function Counts({ preview }: { preview: EditPreview }) {
  const { deletes, updates, inserts } = preview.counts;
  return (
    <span className="text-[12px]">
      {deletes > 0 && <span className="font-medium text-danger">{plural(deletes, "delete")}</span>}
      {[updates && plural(updates, "update"), inserts && plural(inserts, "insert")].filter(Boolean).map((text, i) => (
        <span key={i}>
          {(deletes > 0 || i > 0) && " · "}
          {text}
        </span>
      ))}
    </span>
  );
}

export interface PreviewDialogProps {
  state: CommitDialogState;
  table: string;
  profile: Profile | undefined;
  dialect: Dialect | null;
  staged: StagedEdits | null;
  /** Statements counted so far while applying. */
  done: number;
  onClose: () => void;
  onCommit: () => void;
  onCancelApply: () => void;
  onAction: (action: CommitAction) => void;
  onShowInGrid: (changeId: string, column: string | null) => void;
  onPreviewAgain: () => void;
}

/**
 * The commit dialog (spec ⑤ §3.6.5): the exact statements the backend will send (the transaction's frame included),
 * with the connection, the counts (deletes first) and what is not checked; Commit applies this preview's plan and no
 * other (05a M7). While applying it shows the progress and Cancel until COMMIT goes out; then the outcome, the server's
 * text, the failed statement marked, and what can be done next. Escape closes it, never while a commit runs.
 */
export function PreviewDialog(props: PreviewDialogProps) {
  const { state, table, profile, dialect, staged, done } = props;
  const editor = useRef<CodeEditorHandle>(null);
  const preview =
    state.step === "preview" || state.step === "applying" || state.step === "result" ? state.preview : null;
  const failed = state.step === "result" ? state.report.failedStatement : null;
  const shown = useMemo(() => (preview ? layout(preview) : null), [preview]);
  const extensions = useMemo(
    () => (preview && shown ? statementExtensions(preview, shown.starts, failed) : []),
    [preview, shown, failed],
  );
  useEffect(() => {
    if (failed !== null && shown?.starts[failed] !== undefined)
      editor.current?.selectRange(shown.starts[failed], shown.starts[failed]);
  }, [failed, shown]);
  const applying = state.step === "applying";
  const red = profile?.color === "red";
  const tint = profileColor(profile?.color);
  const transactional = !preview?.warnings.some((warning) => warning.code === "non_transactional");
  const rowOf = (changeId: string | null | undefined) =>
    changeId
      ? (preview?.statements.find((s) => s.change_id === changeId)?.row ??
        (staged ? changeLabel(staged, changeId) : null))
      : null;

  const footer = (() => {
    switch (state.step) {
      case "previewing":
        return (
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
        );
      case "failed":
        return (
          <>
            <Button variant="ghost" onClick={props.onClose}>
              Close
            </Button>
            <Button onClick={props.onPreviewAgain}>Try again</Button>
          </>
        );
      case "preview":
        return state.preview.plan_id ? (
          <>
            <span className="mr-auto">
              <CopyButton text={state.preview.script} label="Copy SQL" />
            </span>
            <Button variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button variant={red ? "danger" : "primary"} onClick={props.onCommit} autoFocus>
              Commit
            </Button>
          </>
        ) : (
          <Button variant="ghost" onClick={props.onClose} autoFocus>
            Close
          </Button>
        );
      case "applying":
        return done < state.preview.statements.length ? (
          <Button variant="ghost" onClick={props.onCancelApply}>
            Cancel
          </Button>
        ) : null;
      case "result":
        return (
          <>
            {state.report.actions.map((action) => (
              <Button key={action} onClick={() => props.onAction(action)}>
                {ACTION_LABELS[action]}
              </Button>
            ))}
            <Button variant="ghost" onClick={props.onClose} autoFocus>
              Close
            </Button>
          </>
        );
    }
  })();

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !applying && props.onClose()}
      title={`Commit changes to ${table}`}
      width={760}
      footer={footer}
    >
      <div className="flex flex-col gap-2.5 text-[12px]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {profile && (
            <span className={cn("flex items-center gap-1.5 font-medium", red && "text-danger")} data-selectable>
              {tint && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tint }} />}
              {profile.name}
            </span>
          )}
          {preview && <Counts preview={preview} />}
        </div>
        {state.step === "previewing" && (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 size={12} className="animate-spin" /> Building the statements…
          </p>
        )}
        {state.step === "failed" &&
          (isPasswordRequired(state.error) && profile ? (
            <RecoverableError profileId={profile.id} error={state.error} onRetry={props.onPreviewAgain} />
          ) : (
            <p className="whitespace-pre-wrap text-danger" data-selectable>
              {state.message}
            </p>
          ))}
        {preview && preview.problems.length > 0 && (
          <>
            <p className="text-danger">Nothing can be committed until these are fixed in the grid:</p>
            <ul className={LIST}>
              {preview.problems.map((problem, i) => (
                <li key={i} className="flex items-center gap-2 border-b border-border px-2 py-1 last:border-b-0">
                  <span className="min-w-0 flex-1" data-selectable>
                    <span className="font-medium">{rowOf(problem.change_id) ?? "A change"}</span>
                    {problem.column && <span className="font-mono"> · {problem.column}</span>}: {problem.message}
                  </span>
                  <Button size="sm" onClick={() => props.onShowInGrid(problem.change_id, problem.column ?? null)}>
                    Show in grid
                  </Button>
                </li>
              ))}
            </ul>
          </>
        )}
        {preview &&
          (staged?.unknownOutcome === "refreshed" || preview.warnings.length > 0) &&
          state.step !== "result" && (
            <ul className={cn(LIST, "text-warning")}>
              {staged?.unknownOutcome === "refreshed" && <li className="px-2 py-1">{UNKNOWN_BEFORE}</li>}
              {preview.warnings.map((warning, i) => {
                const statement = preview.statements.find((s) => s.change_id === warning.change_id);
                const reasons = statement?.unchecked?.map((u) => `${u.column}: ${u.reason}`).join("; ");
                return (
                  <li key={i} className="px-2 py-1" data-selectable>
                    {rowOf(warning.change_id) && <span className="font-medium">{rowOf(warning.change_id)}: </span>}
                    {warning.message}
                    {reasons && <span className="text-muted"> ({reasons})</span>}
                  </li>
                );
              })}
            </ul>
          )}
        {preview && shown && preview.statements.length > 0 && (
          <div className="h-64 overflow-hidden rounded-md border border-border">
            <CodeEditor
              key={state.step === "preview" || state.step === "applying" || state.step === "result" ? state.key : 0}
              ref={editor}
              initialText={shown.text}
              dialect={dialect}
              readOnly
              extensions={extensions}
            />
          </div>
        )}
        {state.step === "preview" && state.preview.plan_id && (
          <p className="text-muted">{transactional ? TRANSACTIONAL_NOTE : NON_TRANSACTIONAL_NOTE}</p>
        )}
        {applying && (
          <p className="flex items-center gap-2" aria-live="polite">
            <Loader2 size={12} className="animate-spin" />
            {done < state.preview.statements.length
              ? `Committing… statement ${Math.min(done + 1, state.preview.statements.length)} of ${state.preview.statements.length}`
              : "Committing…"}
          </p>
        )}
        {state.step === "result" && (
          <div className="flex flex-col gap-1" data-selectable>
            <p className={cn("text-[13px] font-semibold", TONES[state.report.tone])}>{state.report.title}</p>
            {state.report.failedChange && rowOf(state.report.failedChange) && (
              <p className="font-medium">{rowOf(state.report.failedChange)}</p>
            )}
            {state.report.detail && <p className="whitespace-pre-wrap">{state.report.detail}</p>}
            {state.report.notes.map((note) => (
              <p key={note} className="text-muted">
                {note}
              </p>
            ))}
            {state.messages.length > 0 && (
              <ul className={cn(LIST, "font-mono")}>
                {state.messages.map((message, i) => (
                  <li key={i} className={cn("px-2 py-0.5", message.severity === "warning" && "text-warning")}>
                    {message.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}

const ACTION_LABELS: Record<CommitAction, string> = {
  show_in_grid: "Show in grid",
  preview_again: "Preview again",
  rollback: "Roll back",
  reconnect: "Reconnect",
  refresh: "Refresh",
};
```

- [ ] **Step 2: Write `src/features/edits/useCommit.tsx`**

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { toast } from "@/components/ui/Toast";
import type { Dialect, Profile } from "@/lib/api/types";
import { dispatchStaged, readStagedEdits } from "@/stores/stagedEdits";
import type { TableTabState } from "@/stores/workspace";
import type { DataGridHandle } from "../grid/DataGrid";
import { refreshHistory } from "../history/useHistory";
import type { ExecutionState } from "../session/execution";
import type { SessionController, SessionSnapshot } from "../session/SessionController";
import {
  commitReport,
  commitState,
  commitToast,
  NOT_SENT_REPORT,
  problemsOf,
  rerunAfter,
  stagedOutcome,
  type CommitAction,
  type EndedCommit,
} from "./commitFlow";
import { editsErrorMessage, previewEdits } from "./editsApi";
import { PreviewDialog, type CommitDialogState } from "./PreviewDialog";
import { changeCount, toChanges, type StagedEdits, type Staging } from "./staging";
import type { TableEdits } from "./useTableEdits";

/*
 * Committing a table tab's staged changes (spec ⑤ §3.5, §3.6.5, R-05-6, R-05-7): ⌘S or Commit… previews them (the
 * backend writes the statements), Commit applies that preview's plan on the tab's own session, and the outcome goes
 * back into the staging. Nothing is ever sent twice: an apply whose outcome did not arrive is unknown, and Commit stays
 * off until fresh rows were read. Only the plan of the preview on screen is applied (05a M7): its id lives in the
 * dialog's state alone, and a preview whose staging changed since is made again.
 */

const NOT_SHOWN = "That row is not among the rows shown (filtered out or not loaded)";
const ROLLED_BACK = "Rolled back this tab's open transaction: preview the changes again";

const sameStaging = (a: Staging, b: Staging) =>
  a.updates === b.updates && a.inserts === b.inserts && a.deletes === b.deletes;
const stagingOf = (staged: StagedEdits): Staging => ({
  updates: staged.updates,
  inserts: staged.inserts,
  deletes: staged.deletes,
});

export interface CommitInput {
  tab: TableTabState;
  /** The table as the dialog and the toast name it (schema.name). */
  table: string;
  profile: Profile | undefined;
  dialect: Dialect | null;
  controller: SessionController | null;
  snapshot: SessionSnapshot | null;
  browse: {
    execution: ExecutionState | null;
    /** The browse has not delivered its rows yet (stale rows are shown). */
    pending: boolean;
    /** The browse request whose first page is shown, or null while one loads. */
    deliveredRequest: string | null;
    /** Runs the browse again, keeping the position (D-05-12). */
    refresh: () => void;
  };
  edits: Pick<TableEdits, "staged" | "target" | "editable" | "keyChanged" | "readOnlyReason" | "locate">;
  exporting: boolean;
  gridRef: RefObject<DataGridHandle | null>;
  /** Reconnect: a new session for the tab. */
  retrySession: () => void;
}

export interface Commit {
  /** ⌘S, Commit…, the palette's Commit changes…. */
  open: () => void;
  /** Why Commit… cannot start now, or null. */
  disabledReason: string | null;
  /** A commit is running: nothing is staged meanwhile and the tab does not close. */
  applying: boolean;
  dialog: ReactNode;
}

export function useCommit(input: CommitInput): Commit {
  const { tab, table, controller, snapshot, browse, edits, exporting, gridRef } = input;
  const queryClient = useQueryClient();
  const [state, setState] = useState<CommitDialogState | null>(null);
  /** The preview request that may still answer: an older one's answer (the dialog closed meanwhile) is dropped. */
  const tickets = useRef(0);
  const keys = useRef(0);
  /** This tab's `sql_mode_changed` refusals in a row (a hint to reconnect once it repeats). */
  const sqlModeRefusals = useRef(0);
  /** The browse request shown when an outcome was unknown: rows of any other request are the refresh it waits for. */
  const unknownSince = useRef<string | null>(null);

  const staged = edits.staged;
  const applying = state?.step === "applying";
  const disabledReason = (() => {
    if (applying) return "A commit is running";
    if (!controller || snapshot?.phase !== "open") return "Not connected";
    if (exporting) return "An export is running on this session";
    if (staged?.unknownOutcome === "pending") return "The last commit's outcome is unknown: Refresh first";
    if (browse.pending) return "Wait for the rows to load";
    if (edits.target && !edits.target.editable) return edits.readOnlyReason;
    return null;
  })();

  const startPreview = (current: StagedEdits | null) => {
    if (!current || changeCount(current) === 0) {
      setState(null);
      toast("info", "No changes to commit");
      return;
    }
    const ticket = ++tickets.current;
    setState({ step: "previewing" });
    previewEdits(tab.profileId, tab.path, toChanges(current)).then(
      (preview) => {
        if (ticket !== tickets.current) return;
        dispatchStaged(tab.id, { type: "setProblems", problems: problemsOf(preview) }); // they mark the cells
        setState({ step: "preview", preview, staging: stagingOf(current), key: ++keys.current });
      },
      (error: unknown) => {
        if (ticket !== tickets.current) return;
        console.warn("Grid edit preview failed", error instanceof Error ? error.name : error);
        setState({ step: "failed", error, message: editsErrorMessage(error) });
      },
    );
  };

  const open = () => {
    if (state) return;
    const current = readStagedEdits(tab.id);
    if (!current || changeCount(current) === 0) {
      toast("info", "No changes to commit");
      return;
    }
    if (disabledReason) {
      toast("info", disabledReason);
      return;
    }
    startPreview(current);
  };

  const close = () => {
    if (applying) return;
    tickets.current += 1; // a preview still on its way is dropped: its plan is never applied
    setState(null);
  };

  const commit = () => {
    if (state?.step !== "preview" || !state.preview.plan_id || !controller) return;
    const current = readStagedEdits(tab.id);
    if (!current || !sameStaging(stagingOf(current), state.staging)) {
      startPreview(current); // the staging changed since this preview: preview what is staged now
      return;
    }
    const browseWasPaused = browse.execution?.status === "paused";
    const requestId = controller.applyEdits(state.preview.plan_id);
    console.info(`Grid edits: applying ${state.preview.statements.length} statement(s)`);
    setState({ step: "applying", preview: state.preview, key: state.key, requestId, browseWasPaused });
  };

  const finish = (held: Extract<CommitDialogState, { step: "applying" }>, ended: EndedCommit | "not_sent") => {
    controller?.forget(held.requestId);
    refreshHistory(queryClient);
    if (ended === "not_sent") {
      setState({ step: "result", preview: held.preview, key: held.key, report: NOT_SENT_REPORT, messages: [] });
      return;
    }
    dispatchStaged(tab.id, { type: "applyOutcome", outcome: stagedOutcome(ended) });
    sqlModeRefusals.current = ended.error?.code === "sql_mode_changed" ? sqlModeRefusals.current + 1 : 0;
    if (ended.outcome === "unknown") unknownSince.current = browse.deliveredRequest;
    if (rerunAfter(ended.outcome, held.browseWasPaused)) browse.refresh();
    console.info(`Grid edits: ${ended.outcome}`);
    const { counts } = held.preview;
    const total = counts.updates + counts.inserts + counts.deletes;
    if (ended.outcome === "committed" && ended.event) {
      toast("success", commitToast(ended.event, table, counts.inserts));
      const warning = ended.messages.find((message) => message.severity === "warning");
      if (warning) toast("warning", warning.text);
      setState(null);
      gridRef.current?.focus();
      return;
    }
    const report = commitReport(ended, { total, sqlModeRefusals: sqlModeRefusals.current });
    setState({ step: "result", preview: held.preview, key: held.key, report, messages: ended.messages });
  };

  // The apply's events: once it is over, its outcome goes into the staging and the dialog says it.
  const held = state?.step === "applying" ? state : null;
  const execution = held ? (snapshot?.executions[held.requestId] ?? null) : null;
  const latestFinish = useRef(finish);
  useLayoutEffect(() => {
    latestFinish.current = finish;
  });
  useEffect(() => {
    if (!held) return;
    // Gone from the session (a closed or replaced session): its outcome cannot arrive any more.
    const current = execution ?? controller?.store.getState().executions[held.requestId] ?? null;
    if (!current) {
      latestFinish.current(held, {
        kind: "ended",
        outcome: "unknown",
        event: null,
        error: null,
        messages: [],
        cancelled: false,
      });
      return;
    }
    const progress = commitState(current);
    if (progress.kind === "running") return;
    latestFinish.current(held, progress.kind === "not_sent" ? "not_sent" : progress);
  }, [held, execution, controller]);

  // R-05-6: after an unknown outcome, the first rows read since let a commit start again (after a restore: the first
  // rows read at all).
  const pendingUnknown = staged?.unknownOutcome === "pending";
  useEffect(() => {
    if (!pendingUnknown || browse.deliveredRequest === null || browse.deliveredRequest === unknownSince.current) return;
    unknownSince.current = null;
    dispatchStaged(tab.id, { type: "refreshed" });
  }, [pendingUnknown, browse.deliveredRequest, tab.id]);

  const showInGrid = (changeId: string, column: string | null) => {
    const pos = edits.locate(changeId, column);
    tickets.current += 1;
    setState(null);
    if (!pos) {
      toast("info", NOT_SHOWN);
      return;
    }
    gridRef.current?.selectCell(pos.row, pos.col);
    gridRef.current?.focus();
  };
  const onAction = (action: CommitAction) => {
    if (state?.step !== "result") return;
    switch (action) {
      case "show_in_grid":
        if (state.report.failedChange) showInGrid(state.report.failedChange, null);
        return;
      case "preview_again":
        startPreview(readStagedEdits(tab.id));
        return;
      case "rollback":
        controller?.rollback();
        toast("info", ROLLED_BACK);
        setState(null);
        return;
      case "reconnect":
        input.retrySession();
        setState(null);
        return;
      case "refresh":
        browse.refresh();
        setState(null);
        return;
    }
  };

  const done = execution?.affected.length ?? 0;
  const dialog = state ? (
    <PreviewDialog
      state={state}
      table={table}
      profile={input.profile}
      dialect={input.dialect}
      staged={staged}
      done={done}
      onClose={close}
      onCommit={commit}
      onCancelApply={() => held && controller?.cancel(held.requestId)}
      onAction={onAction}
      onShowInGrid={showInGrid}
      onPreviewAgain={() => startPreview(readStagedEdits(tab.id))}
    />
  ) : null;

  return { open, disabledReason, applying, dialog };
}
```

- [ ] **Step 3: Wire it into the Data view**

```diff
diff --git a/src/features/table-data/TableDataTab.tsx b/src/features/table-data/TableDataTab.tsx
index 55eda8e..372ddf6 100644
--- a/src/features/table-data/TableDataTab.tsx
+++ b/src/features/table-data/TableDataTab.tsx
@@ -1,5 +1,5 @@
 import { ChevronDown, ChevronRight, Loader2, RefreshCw, SquareTerminal } from "lucide-react";
-import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
+import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
 import { Button } from "@/components/ui/Button";
 import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
 import type { MenuItem } from "@/components/ui/ContextMenu";
@@ -8,13 +8,17 @@ import { toast } from "@/components/ui/Toast";
 import { errorMessage } from "@/lib/api/client";
 import type { ColumnMeta, Dialect } from "@/lib/api/types";
 import { cn } from "@/lib/cn";
+import { isDialogOpen } from "@/lib/dialogs";
 import { plural } from "@/lib/format";
+import { isImeKeyEvent } from "@/lib/ime";
+import { isCommandKey } from "@/lib/shortcuts";
 import { useWorkspace, type TableTabState } from "@/stores/workspace";
 import { useEngineInfo, useProfiles } from "../connections/hooks";
 import { RecoverableError } from "../connections/RecoverableError";
 import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
 import { kindLabel } from "../explorer/kinds";
 import { EditBar } from "../edits/EditBar";
+import { useCommit } from "../edits/useCommit";
 import { useTableEdits } from "../edits/useTableEdits";
 import { DataGrid, type DataGridHandle } from "../grid/DataGrid";
 import { ExportDialog } from "../grid/ExportDialog";
@@ -30,6 +34,7 @@ import { useTableData, type TableDataState } from "./useTableData";
 
 type View = "data" | "columns" | "ddl";
 const FILTER_BY_VALUE = "Filter by this value";
+const COMMIT_RUNNING = "A commit is running: wait for it to end";
 const CUT_VALUE_MESSAGE = "This value cannot be matched exactly: the grid holds only its beginning";
 const VIEWS: { value: View; label: string }[] = [
   { value: "data", label: "Data" },
@@ -159,7 +164,8 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   const dialect = engine?.dialect ?? null;
   const openQuery = useWorkspace((s) => s.openQuery);
   const profiles = useProfiles();
-  const profileName = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile.name;
+  const profile = profiles.data?.find((entry) => entry.profile.id === tab.profileId)?.profile;
+  const profileName = profile?.name;
   const guards = useSessionGuards(table.controller, profileName ? `${tab.title} · ${profileName}` : tab.title);
   useCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, (intent) => guards.exportStep(intent));
   const [exportOpen, setExportOpen] = useState(false);
@@ -177,14 +183,18 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
   // An export runs on this session: filter, sort and refresh would only wait behind it.
   const exporting = isExporting(snapshot);
   const gridRef = useRef<DataGridHandle>(null);
+  // Nothing is staged while a commit runs; the flag follows the commit's own state a render later (H8).
+  const [applying, setApplying] = useState(false);
+  const openCommit = useRef<() => void>(() => undefined);
   const edits = useTableEdits({
     tab,
     columns,
     rows,
     rowEditing: engine?.row_editing === true,
     sessionOpen: phase === "open",
-    locked: null,
+    locked: applying ? COMMIT_RUNNING : null,
     gridRef,
+    onRequestCommit: () => openCommit.current(),
   });
   const [askingDiscard, setAskingDiscard] = useState(false);
   /** Discard…: more than one change asks first (⌘Z brings them back either way). */
@@ -193,6 +203,40 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
     table.refresh();
     edits.refetchTarget(); // DDL since: the next edit uses the table as it is now (spec §3.2.1)
   };
+  const commit = useCommit({
+    tab,
+    table: relation ? qualifiedName(relation) : tab.title,
+    profile,
+    dialect,
+    controller: table.controller,
+    snapshot,
+    browse: {
+      execution,
+      pending: shown.stale,
+      deliveredRequest: table.deliveredRequest,
+      refresh: table.refresh,
+    },
+    edits,
+    exporting,
+    gridRef,
+    retrySession: table.retry,
+  });
+  useEffect(() => setApplying(commit.applying), [commit.applying]);
+  useLayoutEffect(() => {
+    openCommit.current = commit.open;
+  });
+  // ⌘S while this tab's Data view is shown, wherever the focus is in it (an editor stages first and opens it itself);
+  // never the browser's Save page, nothing while a dialog has the keyboard or an input method composes.
+  useEffect(() => {
+    if (!active) return;
+    const onKey = (e: KeyboardEvent) => {
+      if (isImeKeyEvent(e) || e.defaultPrevented || !isCommandKey(e, "s")) return;
+      e.preventDefault();
+      if (!isDialogOpen()) openCommit.current();
+    };
+    window.addEventListener("keydown", onKey);
+    return () => window.removeEventListener("keydown", onKey);
+  }, [active]);
 
   const gridSort = useMemo<GridSort[]>(
     () =>
@@ -293,7 +337,8 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
             unknownOutcome={edits.staged?.unknownOutcome === "pending"}
             onAddRow={edits.addRow}
             onDiscard={requestDiscard}
-            commitDisabled={null}
+            onCommit={commit.open}
+            commitDisabled={commit.disabledReason}
           />
         </div>
       </div>
@@ -348,6 +393,7 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
           edits.discardAll();
         }}
       />
+      {commit.dialog}
       {guards.dialog}
     </div>
   );
```

- [ ] **Step 4: Check the types and the build**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: PASS; no errors.

- [ ] **Step 5: Manual check list** (the coordinator, `scripts/dev.sh --web`; a `hold` runs in a terminal of its own, in the background, e.g. `backend/.venv/bin/python scripts/scratch_tables.py hold pg 30 "…" &`):
  1. `backend/.venv/bin/python scripts/scratch_tables.py create`.
  2. dev pg › zz_05b_items, all 1,200 rows loaded, scrolled to row 500. Stage: row 1 `big` → `9007199254740993`; row 2 `label` → NULL (⌥⌘N); row 3 `label` → "" (type and erase); + Row with `label` "new"; row 4 deleted. ⌘S → "Commit changes to public.zz_05b_items": "dev pg", "1 delete · 3 updates · 1 insert" (the delete in red, first); the statements BEGIN, DELETE…, three UPDATE…, INSERT…, COMMIT, with D, U, U, U, I in the gutter; the note "Runs in one transaction: …"; Copy SQL puts the script on the clipboard.
  3. Commit → the dialog closes; toast "Committed 5 changes to public.zz_05b_items. New rows appear where the table's order puts them, which may be past the rows loaded"; the grid re-reads all 1,200 rows and stays at row 500; filter `id <= 4 OR id = 1201` (the browse has no ORDER BY: PostgreSQL may list updated rows last): row 1's `big` is `9007199254740993`, row 2's `label` NULL, row 3's empty, row 4 gone, the new row is id 1201 with label "new"; no marks, no dot; History has one ok entry with the script. Clear the filter.
  4. Conflict: stage row 10's `label` → "mine"; `… sql pg "UPDATE public.zz_05b_items SET label = 'theirs' WHERE id = 10"`; Commit → "Nothing was applied", "Row id = 10", the server's text "…was changed or deleted since it was loaded…", the UPDATE marked red; Show in grid → the dialog closes, row 10's `label` is the active cell, its row number red with the message. Refresh → a conflict corner, "now theirs"; Keep mine → ⌘S → Commit → committed; the cell shows "mine".
  5. Someone else's other column: stage row 11's `label` → "x"; `… sql pg "UPDATE public.zz_05b_items SET note = 'other' WHERE id = 11"`; commit → committed; after the re-read `note` is "other".
  6. Problems: stage row 12's `amount` → "abc"; ⌘S → no Commit button; "Row id = 12 · amount: Not a number" with Show in grid → the cell has a red outline. Change it to 12.50 → ⌘S → Commit → committed.
  7. Cancel while waiting on a lock: `… hold pg 30 "UPDATE public.zz_05b_items SET label = 'held' WHERE id = 40" &`; stage row 40's `label` → "y"; Commit → "Committing… statement 1 of 1" with Cancel; Cancel → "Cancelled: nothing was applied"; the change is still staged. Wait for the hold to end.
  8. Unknown outcome: `… hold pg 60 "UPDATE public.zz_05b_items SET label = 'held' WHERE id = 41" &`; stage row 41's `label` → "z"; Commit (it waits); `touch backend/dbaguy_backend/app.py` (the dev backend reloads) → "Outcome unknown", "The connection was lost while committing: …", Refresh; the bar says "Outcome unknown: Refresh"; Commit… is disabled ("…Refresh first"); History has no entry for it (05a M9). Close; the tab's error has Retry (give the password if asked); once the rows are back, Commit… is enabled; ⌘S → the preview's first warning is "The last commit's outcome was unknown: …"; after the hold ended, Commit → committed.
  9. One plan only: stage a change, ⌘S, Cancel, ⌘S again, Commit → History has exactly one new entry, and the value is changed once.
  10. dev mysql › zz_05b_items: the same mix as step 2 (the new row's `id` DEFAULT) → committed, values exact; `payload` → `{"a": 1.10}` in the drawer → committed; the re-read shows MySQL's form of it.
  11. MyISAM, partial: zz_05b_myisam: delete row 3, stage row 2's `label` → "z"; `… sql mysql "DELETE FROM zz_05b_myisam WHERE id = 2"`; ⌘S → the warning "This table's storage engine cannot roll back: …" and the non-transactional note; Commit → "Partly applied: 1 of 2 changes took effect", the PARTIAL note; after the re-read row 3 is gone and the bar says "1 change · 1 in rows not shown". Discard.
  12. MyISAM, unknown: + Row with `id` 4 and `label` "a" (row 1 has "a": the UNIQUE key refuses it) → Commit → "Outcome unknown" with the server's 1062 text (a statement sent on a table that cannot roll back, 05a G10); Refresh → no row 4; Discard.
  13. dev mssql › zz_05b_items: the step-2 mix (the new row leaves `id` to its IDENTITY) → the preview starts with `SET NOCOUNT OFF;` and `BEGIN TRANSACTION` and ends `COMMIT TRANSACTION`; committed, values exact.
  14. zz_05b sqlite › zz_05b_items: the step-2 mix → committed. Busy file: `… hold sqlite 20 "SELECT id FROM zz_05b_items" &`; stage a change; Commit → about 5 s later "Nothing was applied", "database is locked…", and "Another tab still has an open result of this database file, or another program is using it: …".
  15. Set dev pg's colour to red (Edit…): the dialog's Commit button is red, the connection's name in red. Set it back.
  16. While the dialog is open, ⌘S, ⌘W and Escape during "Committing…" do nothing; Escape on a preview closes it.
  17. Discard what is left, close the tabs; `backend/.venv/bin/python scripts/scratch_tables.py drop && backend/.venv/bin/python scripts/scratch_tables.py check` → "No zz_05b_ object left".

- [ ] **Step 6: Commit**

```bash
git add src/features/edits/PreviewDialog.tsx src/features/edits/useCommit.tsx src/features/table-data/TableDataTab.tsx
git commit -m "feat(app): preview and commit grid edits — the exact statements, one plan, progress and Cancel, honest outcomes, the re-read"
```

---

### Task 11: Closing, the tab's dot, the export note and the palette

**Suggested implementer:** Sonnet.

**Files:**
- Create: `src/lib/tabActions.ts`, `src/features/edits/editActions.ts`
- Modify: `src/features/query/tabActions.ts`, `src/features/palette/commands.ts`, `src/features/palette/CommandPalette.tsx`, `src/features/workspace/closeGuards.ts`, `src/features/workspace/Workspace.tsx`, `src/features/grid/ExportDialog.tsx`, `src/features/table-data/TableDataTab.tsx`

**Interfaces:**
- Consumes: Tasks 9 and 10 (`edits.*`, `commit.open`, `commit.disabledReason`, `commit.applying`); `useCloseGuard`; `useReportTabStatus`; `readStagedEdits`, `changeCount`.
- Produces:
  - `lib/tabActions.ts`: `TabAction { enabled; checked?; run }`, `TabActionRegistry<Id> { read(tabId); use(tabId, actions) }`, `createTabActionRegistry<Id>()`. `features/query/tabActions.ts` keeps its exports (`QueryTabActionId`, `QueryTabAction`, `QueryTabActions`, `readQueryTabActions`, `useQueryTabActions`) on top of it (DRY).
  - `edits/editActions.ts`: `TableEditActionId = "addRow" | "deleteRows" | "setNull" | "commit" | "discardAll" | "undo" | "redo"`, `TableEditActions`, `readTableEditActions`, `useTableEditActions`.
  - `CommandContext.editActions: TableEditActions | null`; `SHORTCUTS.commitEdits`, `deleteRows`, `setNull`, `undoEdit`, `redoEdit`.
  - `CLOSE_GUARD_ORDER.edits = 5` (close-only); `ExportSource.note?: string | null`.

Behaviour (spec R-05-11, §3.6.4, §3.6.6, M3; rulings H11, H25):
- Closing a table tab with staged changes asks "Discard N staged changes to <table>?" (Discard / Cancel — no Commit: a commit needs its preview); Cancel keeps the tab and its changes. The guard lives in the Data view, which is mounted for every table tab, so a restored tab that was never shown asks too (C28). During a commit closing is refused with "A commit is running: wait for it to end, or cancel it in its dialog" (the dialog is modal, so this is only reachable by a profile deletion or a programmatic close). The guard never discards by itself: once the tab is closed, the workspace writer forgets its staged edits (a later guard may still refuse the close). Losing the connection (`disconnect`) does not ask (close-only), and quitting does not ask (the changes are saved).
- A table tab with staged changes shows the unsaved dot in the tab strip.
- The export dialog says "Staged changes are not committed and are not in the export" while changes are staged.
- ⌘⇧P lists, on a table tab only: Add row, Delete selected rows (⌘⌫), Set NULL (⌥⌘N), Commit changes… (⌘S), Discard all changes, Undo staged change (⌘Z), Redo staged change (⇧⌘Z) — each enabled as its own control is, run through the tab's own handlers (H11: the palette is the reliable path if WKWebView keeps ⌘Z for its Edit menu, A10).

- [ ] **Step 1: The generic registry, and the query tab's on top of it**

`src/lib/tabActions.ts`:

```ts
import { useLayoutEffect, useRef } from "react";

/*
 * What the command palette can do with a mounted tab, by tab id (ruling R-02c-15). The palette never runs or ends
 * anything itself: it calls the tab's own handlers, the ones its toolbar and keys call, so confirmations and state apply
 * alike. One registry per kind of tab (query tabs, table tabs).
 */

export interface TabAction {
  /** Whether the tab's own control would allow it now (its button is enabled). */
  enabled: boolean;
  /** For a toggle: whether it is on now. */
  checked?: boolean;
  /** The tab's own handler. */
  run: () => void;
}

export interface TabActionRegistry<Id extends string> {
  /** The tab's actions as they are now; null when no tab with that id is mounted. */
  read: (tabId: string) => Record<Id, TabAction> | null;
  /** Registers the tab's actions while the component is mounted; the latest `actions` passed are the ones read. */
  use: (tabId: string, actions: Record<Id, TabAction>) => void;
}

export function createTabActionRegistry<Id extends string>(): TabActionRegistry<Id> {
  const registry = new Map<string, () => Record<Id, TabAction>>();
  return {
    read: (tabId) => registry.get(tabId)?.() ?? null,
    use: function useTabActions(tabId, actions) {
      const latest = useRef(actions);
      useLayoutEffect(() => {
        latest.current = actions;
      });
      useLayoutEffect(() => {
        const read = () => latest.current;
        registry.set(tabId, read);
        return () => {
          if (registry.get(tabId) === read) registry.delete(tabId);
        };
      }, [tabId]);
    },
  };
}
```

```diff
diff --git a/src/features/query/tabActions.ts b/src/features/query/tabActions.ts
index b819c85..075ec76 100644
--- a/src/features/query/tabActions.ts
+++ b/src/features/query/tabActions.ts
@@ -1,4 +1,4 @@
-import { useLayoutEffect, useRef } from "react";
+import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";
 
 /** What the command palette can do with a query tab (ruling R-02c-15). */
 export type QueryTabActionId =
@@ -11,43 +11,12 @@ export type QueryTabActionId =
   | "commit"
   | "rollback";
 
-export interface QueryTabAction {
-  /** Whether the tab's own control would allow it now (the toolbar button is enabled). */
-  enabled: boolean;
-  /** For a toggle: whether it is on now. */
-  checked?: boolean;
-  /** The tab's own handler: the one its toolbar button or keymap calls. */
-  run: () => void;
-}
-
+export type QueryTabAction = TabAction;
 export type QueryTabActions = Record<QueryTabActionId, QueryTabAction>;
 
-/** Reads a tab's actions as they are at the moment it is called. */
-type ReadActions = () => QueryTabActions;
-
-/*
- * The mounted query tabs' actions, by tab id. The palette never runs or ends anything itself: it calls the same
- * functions as the tab's toolbar and keymap, so confirmations, `canRun` and the transaction state apply alike.
- */
-const registry = new Map<string, ReadActions>();
-
-function registerQueryTabActions(tabId: string, read: ReadActions): () => void {
-  registry.set(tabId, read);
-  return () => {
-    if (registry.get(tabId) === read) registry.delete(tabId);
-  };
-}
+const registry = createTabActionRegistry<QueryTabActionId>();
 
 /** The tab's actions as they are now; null when no query tab with that id is mounted. */
-export function readQueryTabActions(tabId: string): QueryTabActions | null {
-  return registry.get(tabId)?.() ?? null;
-}
-
+export const readQueryTabActions = registry.read;
 /** Registers the tab's actions while the component is mounted; the latest `actions` passed are the ones read. */
-export function useQueryTabActions(tabId: string, actions: QueryTabActions): void {
-  const latest = useRef(actions);
-  useLayoutEffect(() => {
-    latest.current = actions;
-  });
-  useLayoutEffect(() => registerQueryTabActions(tabId, () => latest.current), [tabId]);
-}
+export const useQueryTabActions = registry.use;
```

- [ ] **Step 2: The table tab's edit actions and the palette's commands**

`src/features/edits/editActions.ts`:

```ts
import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";

/** What the command palette can do with a table tab's staged edits (spec ⑤ §3.6.4, ruling H11 for undo / redo). */
export type TableEditActionId = "addRow" | "deleteRows" | "setNull" | "commit" | "discardAll" | "undo" | "redo";
export type TableEditActions = Record<TableEditActionId, TabAction>;

const registry = createTabActionRegistry<TableEditActionId>();

/** The table tab's edit actions as they are now; null when no table tab with that id is mounted. */
export const readTableEditActions = registry.read;
export const useTableEditActions = registry.use;
```

```diff
diff --git a/src/features/palette/commands.ts b/src/features/palette/commands.ts
index 2d72d21..646f83d 100644
--- a/src/features/palette/commands.ts
+++ b/src/features/palette/commands.ts
@@ -4,6 +4,7 @@ import { errorMessage } from "@/lib/api/client";
 import type { AppSettings, EngineInfo, ProfileEntry } from "@/lib/api/types";
 import { openNewQuery, useWorkspace, type Tab } from "@/stores/workspace";
 import { enginesKey, findEngineInfo, profilesKey } from "../connections/hooks";
+import { readTableEditActions, type TableEditActionId, type TableEditActions } from "../edits/editActions";
 import { refreshSchema } from "../explorer/refreshSchema";
 import { HISTORY_VIEW } from "../history/useHistory";
 import { readQueryTabActions, type QueryTabActionId, type QueryTabActions } from "../query/tabActions";
@@ -12,10 +13,16 @@ import { requestCloseTab } from "../workspace/closeGuards";
 
 /**
  * The shortcut labels the palette shows, in one place. The bindings: ⌘↵, ⌘⇧↵, ⌘E and ⌘⇧F are the query tab's editor
- * keymap, ⌘W the workspace, ⌘, the settings dialog, ⌘P and ⌘⇧P the app. A command without a binding has none.
+ * keymap; ⌘S, ⌘⌫, ⌥⌘N, ⌘Z and ⇧⌘Z a table tab's grid (spec ⑤ §3.6.4); ⌘W the workspace; ⌘, the settings dialog;
+ * ⌘P and ⌘⇧P the app. A command without a binding has none.
  */
 export const SHORTCUTS = {
   run: "⌘↵",
+  commitEdits: "⌘S",
+  deleteRows: "⌘⌫",
+  setNull: "⌥⌘N",
+  undoEdit: "⌘Z",
+  redoEdit: "⇧⌘Z",
   runAll: "⌘⇧↵",
   explain: "⌘E",
   format: "⌘⇧F",
@@ -31,6 +38,8 @@ export interface CommandContext {
   activeTab: Tab | null;
   /** The active query tab's own actions and whether each is possible; null when no query tab is active. */
   tabActions: QueryTabActions | null;
+  /** The active table tab's edit actions; null when no table tab is active. */
+  editActions: TableEditActions | null;
   settings: {
     /** The stored settings are known (until then nothing may be saved over them). */
     loaded: boolean;
@@ -80,6 +89,22 @@ function tabCommand(
   };
 }
 
+/** A table tab's edit command (spec ⑤ §3.6.4), listed only while a table tab is active; asked again when it runs. */
+function editCommand(id: TableEditActionId, label: string, shortcut?: string): PaletteCommand {
+  return {
+    id: `edits.${id}`,
+    label,
+    shortcut,
+    visible: (ctx) => ctx.editActions !== null,
+    enabled: (ctx) => ctx.editActions?.[id].enabled ?? false,
+    run: (ctx) => {
+      const action = ctx.activeTab ? readTableEditActions(ctx.activeTab.id)?.[id] : undefined;
+      if (action?.enabled) action.run();
+      else console.info(`Palette: ${label} is no longer possible in the active tab`);
+    },
+  };
+}
+
 /** What the active tab's connection is and can do, from the profiles and engines already loaded; null if unknown. */
 function activeEngine(ctx: CommandContext): EngineInfo | null {
   return findEngineInfo(
@@ -135,6 +160,13 @@ export const COMMANDS: readonly PaletteCommand[] = [
     checked: (ctx) => ctx.settings.current.editor.vim,
     run: (ctx) => void toggleVim(ctx.settings),
   },
+  editCommand("addRow", "Add row"),
+  editCommand("deleteRows", "Delete selected rows", SHORTCUTS.deleteRows),
+  editCommand("setNull", "Set NULL", SHORTCUTS.setNull),
+  editCommand("commit", "Commit changes…", SHORTCUTS.commitEdits),
+  editCommand("discardAll", "Discard all changes"),
+  editCommand("undo", "Undo staged change", SHORTCUTS.undoEdit),
+  editCommand("redo", "Redo staged change", SHORTCUTS.redoEdit),
   tabCommand("toggleAutocommit", "Toggle auto-commit"),
   tabCommand("commit", "Commit"),
   tabCommand("rollback", "Rollback"),
```

```diff
diff --git a/src/features/palette/CommandPalette.tsx b/src/features/palette/CommandPalette.tsx
index dc14085..38dc88d 100644
--- a/src/features/palette/CommandPalette.tsx
+++ b/src/features/palette/CommandPalette.tsx
@@ -7,6 +7,7 @@ import { isImeKeyEvent } from "@/lib/ime";
 import { isSettingsKey } from "@/lib/shortcuts";
 import { useAppSettings } from "@/stores/settings";
 import { useWorkspace } from "@/stores/workspace";
+import { readTableEditActions } from "../edits/editActions";
 import { readQueryTabActions } from "../query/tabActions";
 import { openSettings } from "../settings/SettingsDialog";
 import { COMMANDS, SHORTCUTS, type CommandContext } from "./commands";
@@ -24,6 +25,7 @@ function CommandList({ choose }: { choose: Choose }) {
   const context: CommandContext = {
     activeTab,
     tabActions: activeTab?.kind === "query" ? readQueryTabActions(activeTab.id) : null,
+    editActions: activeTab?.kind === "table" ? readTableEditActions(activeTab.id) : null,
     settings: { loaded: settings.loaded, current: settings.settings, save: settings.save },
     queryClient,
   };
```

- [ ] **Step 3: The close guard's order, the dot, the export note**

```diff
diff --git a/src/features/workspace/closeGuards.ts b/src/features/workspace/closeGuards.ts
index f4386b4..809246d 100644
--- a/src/features/workspace/closeGuards.ts
+++ b/src/features/workspace/closeGuards.ts
@@ -13,13 +13,17 @@ export type CloseGuard = (intent: CloseIntent) => Promise<boolean> | boolean;
  * The order a tab's guards are asked in, lowest first (ruling R-02c-8): the unsaved script first, while nothing has
  * been done yet (its Cancel must never follow a side effect), then a running export, then the run (no further
  * statement is sent), then an open transaction. A disconnect asks only the session's guards (the tab, and so its
- * script, stays). A document tab's unsaved text (`document`, ruling C20) is asked first too: a tab has a script or a
- * document, never both, and a document tab has no session.
+ * script, stays). A document tab's unsaved text (`document`, ruling C20) and a table tab's staged grid edits (`edits`,
+ * spec ⑤ R-05-11) are asked first too: a tab has at most one of a script, a document and staged edits.
  */
-export const CLOSE_GUARD_ORDER = { script: 5, document: 5, export: 10, run: 15, transaction: 20 } as const;
+export const CLOSE_GUARD_ORDER = { script: 5, document: 5, edits: 5, export: 10, run: 15, transaction: 20 } as const;
 
 /** The guards that only closing asks: losing the connection keeps the tab and its text. */
-const CLOSE_ONLY: ReadonlySet<number> = new Set([CLOSE_GUARD_ORDER.script, CLOSE_GUARD_ORDER.document]);
+const CLOSE_ONLY: ReadonlySet<number> = new Set([
+  CLOSE_GUARD_ORDER.script,
+  CLOSE_GUARD_ORDER.document,
+  CLOSE_GUARD_ORDER.edits,
+]);
 
 interface Entry {
   order: number;
```

```diff
diff --git a/src/features/workspace/Workspace.tsx b/src/features/workspace/Workspace.tsx
index 3a0e3bd..ad1c365 100644
--- a/src/features/workspace/Workspace.tsx
+++ b/src/features/workspace/Workspace.tsx
@@ -73,7 +73,8 @@ function TabLabel({ tab, active, profile }: { tab: Tab; active: boolean; profile
   const status = useTabStatus(tab.id);
   const tint = profileColor(profile?.color);
   const query = tab.kind === "query";
-  const savable = query || tab.kind === "document";
+  // A dot for unsaved work: a query's or a document's text, a table's staged grid edits (spec ⑤).
+  const savable = query || tab.kind === "document" || tab.kind === "table";
   // However it became active (opened, ⌘1–9, ⌃Tab, the palette, the restored workspace), the strip shows it.
   const ref = useRef<HTMLDivElement>(null);
   useEffect(() => {
```

```diff
diff --git a/src/features/grid/ExportDialog.tsx b/src/features/grid/ExportDialog.tsx
index a7440e7..9a3b042 100644
--- a/src/features/grid/ExportDialog.tsx
+++ b/src/features/grid/ExportDialog.tsx
@@ -34,6 +34,8 @@ export interface ExportSource {
   title: string;
   /** Prefills the INSERT target (table data). */
   tableName?: TableName | null;
+  /** A line the dialog shows above its settings (table data: staged changes are not in the export). */
+  note?: string | null;
 }
 
 type ExportFormat = Exclude<FormatKind, "in_list">;
@@ -327,6 +329,7 @@ export function ExportDialog({
         </div>
       ) : (
         <div className="flex flex-col gap-2">
+          {source.note && <p className="text-[12px] text-warning">{source.note}</p>}
           <FieldRow label="Format">
             <Select value={format} onChange={(e) => changeFormat(e.target.value as ExportFormat)}>
               {formats.map((kind) => (
```

- [ ] **Step 4: The Data view registers them**

```diff
diff --git a/src/features/table-data/TableDataTab.tsx b/src/features/table-data/TableDataTab.tsx
index 372ddf6..a877b57 100644
--- a/src/features/table-data/TableDataTab.tsx
+++ b/src/features/table-data/TableDataTab.tsx
@@ -6,18 +6,22 @@ import type { MenuItem } from "@/components/ui/ContextMenu";
 import { Tabs } from "@/components/ui/Tabs";
 import { toast } from "@/components/ui/Toast";
 import { errorMessage } from "@/lib/api/client";
-import type { ColumnMeta, Dialect } from "@/lib/api/types";
-import { cn } from "@/lib/cn";
 import { isDialogOpen } from "@/lib/dialogs";
-import { plural } from "@/lib/format";
 import { isImeKeyEvent } from "@/lib/ime";
 import { isCommandKey } from "@/lib/shortcuts";
+import type { ColumnMeta, Dialect } from "@/lib/api/types";
+import { cn } from "@/lib/cn";
+import { plural } from "@/lib/format";
+import { readStagedEdits } from "@/stores/stagedEdits";
+import { useReportTabStatus } from "@/stores/tabStatus";
 import { useWorkspace, type TableTabState } from "@/stores/workspace";
 import { useEngineInfo, useProfiles } from "../connections/hooks";
 import { RecoverableError } from "../connections/RecoverableError";
 import { ColumnsView, DdlView } from "../explorer/ObjectDetailView";
 import { kindLabel } from "../explorer/kinds";
+import { useTableEditActions } from "../edits/editActions";
 import { EditBar } from "../edits/EditBar";
+import { changeCount } from "../edits/staging";
 import { useCommit } from "../edits/useCommit";
 import { useTableEdits } from "../edits/useTableEdits";
 import { DataGrid, type DataGridHandle } from "../grid/DataGrid";
@@ -35,6 +39,8 @@ import { useTableData, type TableDataState } from "./useTableData";
 type View = "data" | "columns" | "ddl";
 const FILTER_BY_VALUE = "Filter by this value";
 const COMMIT_RUNNING = "A commit is running: wait for it to end";
+const CLOSE_WHILE_COMMITTING = "A commit is running: wait for it to end, or cancel it in its dialog";
+const NOT_IN_EXPORT = "Staged changes are not committed and are not in the export";
 const CUT_VALUE_MESSAGE = "This value cannot be matched exactly: the grid holds only its beginning";
 const VIEWS: { value: View; label: string }[] = [
   { value: "data", label: "Data" },
@@ -237,6 +243,52 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
     window.addEventListener("keydown", onKey);
     return () => window.removeEventListener("keydown", onKey);
   }, [active]);
+  // Closing asks while changes are staged (R-05-11: Discard / Cancel, no Commit: a commit needs its preview), never
+  // during a commit. Registered for every table tab, shown or not (a restored tab's staged edits are in the store).
+  /** The close question's answer, while it is asked. */
+  const closeAnswer = useRef<((discard: boolean) => void) | null>(null);
+  const [askingClose, setAskingClose] = useState(false);
+  const committing = useRef(false);
+  useLayoutEffect(() => {
+    committing.current = commit.applying;
+  });
+  useCloseGuard(tab.id, CLOSE_GUARD_ORDER.edits, () => {
+    if (committing.current) {
+      toast("info", CLOSE_WHILE_COMMITTING);
+      return false;
+    }
+    const staged = readStagedEdits(tab.id);
+    if (!staged || changeCount(staged) === 0) return true;
+    closeAnswer.current?.(false);
+    // The changes go with the tab: the workspace writer forgets a closed tab's (never here, a later guard may refuse).
+    return new Promise<boolean>((resolve) => {
+      closeAnswer.current = resolve;
+      setAskingClose(true);
+    });
+  });
+  useEffect(
+    () => () => {
+      closeAnswer.current?.(false); // a tab that goes while asking keeps its changes: the answer is no
+      closeAnswer.current = null;
+    },
+    [],
+  );
+  const answerClose = (discard: boolean) => {
+    const answer = closeAnswer.current;
+    closeAnswer.current = null;
+    setAskingClose(false);
+    answer?.(discard);
+  };
+  useReportTabStatus(tab.id, { dirty: edits.changes > 0 });
+  useTableEditActions(tab.id, {
+    addRow: { enabled: edits.editable && !applying, run: edits.addRow },
+    deleteRows: { enabled: edits.editable && !applying, run: edits.deleteSelected },
+    setNull: { enabled: edits.editable && !applying, run: edits.setNullSelected },
+    commit: { enabled: edits.changes > 0 && commit.disabledReason === null, run: commit.open },
+    discardAll: { enabled: edits.changes > 0 && !applying, run: requestDiscard },
+    undo: { enabled: (edits.staged?.past.length ?? 0) > 0 && !applying, run: edits.undo },
+    redo: { enabled: (edits.staged?.future.length ?? 0) > 0 && !applying, run: edits.redo },
+  });
 
   const gridSort = useMemo<GridSort[]>(
     () =>
@@ -272,12 +324,13 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
     [columns, resultRowAt, leading, dialect, exporting, filterByValue],
   );
   // Export writes the current browse SELECT (filter, sort and refresh wait while it runs).
+  const exportNote = edits.changes > 0 ? NOT_IN_EXPORT : null;
   const exportSource = useMemo(
     () =>
       table.controller && execution && relation
-        ? { controller: table.controller, sql: execution.sql, title: relation.name, tableName }
+        ? { controller: table.controller, sql: execution.sql, title: relation.name, tableName, note: exportNote }
         : null,
-    [table.controller, execution, relation, tableName],
+    [table.controller, execution, relation, tableName, exportNote],
   );
 
   const openInQueryTab = () => {
@@ -393,6 +446,14 @@ function DataView({ tab, active }: { tab: TableTabState; active: boolean }) {
           edits.discardAll();
         }}
       />
+      <ConfirmDialog
+        open={askingClose}
+        onOpenChange={(open) => !open && answerClose(false)}
+        title="Close tab"
+        message={`Discard ${plural(edits.changes, "staged change")} to ${relation?.name ?? tab.title}?`}
+        confirmLabel="Discard"
+        onConfirm={() => answerClose(true)}
+      />
       {commit.dialog}
       {guards.dialog}
     </div>
```

- [ ] **Step 5: Check the types and the build**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: PASS; no errors.

- [ ] **Step 6: Manual check list** (the coordinator, `scripts/dev.sh --web`):
  1. `backend/.venv/bin/python scripts/scratch_tables.py create pg`.
  2. dev pg › zz_05b_items: stage 3 changes → the tab shows a dot. ⌘W → "Discard 3 staged changes to zz_05b_items?" → Cancel: the tab and its changes stay. ⌘W → Discard: the tab closes; reopen the table: nothing staged; `dev/data/workspace.json` has no `staged_edits` for it after the next save.
  3. Stage 2 changes, open a query tab, reload the page: the table tab (restored, not shown) has its dot; its × in the strip asks "Discard 2 staged changes…" → Cancel. Show it: the 2 marks are there.
  4. A table tab with nothing staged closes without asking.
  5. Export… (status bar) with changes staged: the dialog's first line is "Staged changes are not committed and are not in the export"; Cancel.
  6. ⌘⇧P on the table tab: the seven edit commands are listed (with ⌘⌫, ⌥⌘N, ⌘S, ⌘Z, ⇧⌘Z); Add row adds a row and opens its editor; Delete selected rows marks the selected rows; Set NULL nulls the selected cell; Undo / Redo staged change step back and forth; Commit changes… opens the preview (Cancel); Discard all changes asks (more than one change) and discards. On a query tab none of them is listed.
  7. Deleting a profile: make "zz copy" (a copy of dev pg), open its zz_05b_items, stage a change, delete the profile from the tree → the tab asks "Discard 1 staged change…" first.
  8. Discard everything, close the tabs, delete "zz copy" if it is still there; `backend/.venv/bin/python scripts/scratch_tables.py drop pg && backend/.venv/bin/python scripts/scratch_tables.py check` → "No zz_05b_ object left".

- [ ] **Step 7: Commit**

```bash
git add src/lib/tabActions.ts src/features/query/tabActions.ts src/features/edits/editActions.ts \
  src/features/palette/commands.ts src/features/palette/CommandPalette.tsx src/features/workspace/closeGuards.ts \
  src/features/workspace/Workspace.tsx src/features/grid/ExportDialog.tsx src/features/table-data/TableDataTab.tsx
git commit -m "feat(app): closing a tab with staged edits asks; the tab's dot, the export note, the edit commands in ⌘⇧P"
```

---

### Task 12: Measurements, the packaged build, CLAUDE.md and the spec

**Suggested implementer:** Sonnet.

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-19-05-grid-editing-design.md`
- Scratchpad only, not committed: the measurement output.

**Interfaces:**
- Consumes: everything above; Task 9 check 28's Q1 numbers (from the coordinator).
- Produces: the docs only. (The follow-ups note `docs/superpowers/notes/2026-10-20-05b-grid-editing-frontend-followups.md` is the coordinator's, after the final review; Appendix A below is its user test list.)

- [ ] **Step 1: Every suite**

Run: `npx vitest run && npm run typecheck && npm run build && cd backend && .venv/bin/pytest -q`
Expected: PASS (Vitest 230 tests, +113 over 05a's 117); the backend suite green with the dev containers up; no `zz_05b_` object exists while it runs (`backend/.venv/bin/python scripts/scratch_tables.py check`).

- [ ] **Step 2: The bundle.** From `npm run build`'s output, note the main chunk (`dist/assets/index-*.js`, kB and gzip). 05a: 1,366.34 kB (gzip 441.38 kB). Planning measured about 1,416.8 kB (gzip 458.2 kB) for this plan's code.

- [ ] **Step 3: The packaged app.** `npm run build:app`, then `scripts/measure-startup.sh` 4 times (the first after the build is the build's first start). Note the sidecar size (`du -sk src-tauri/resources/backend`; 05a 49,224 KB: only `domain/edits.py`, `domain/workspace.py` and the repair changed, so about the same), the cold start, the RSS and `footprint` sums and the backend's `footprint`, against 05a's (1.07–1.19 s; 163–172 MB; backend 76–79 MB). Do not drive the packaged UI (the user's list, Appendix A).

- [ ] **Step 4: CLAUDE.md** — these edits, nothing else (each block is the exact text; indentation as shown):

  In 「Repo 結構」 › `src/` › the `features/` line, add `edits` after `documents`; after the 「Couchbase（04c）」 bullet add:

```markdown
    - Grid 內編輯（05b）：`edits/{cells, staging, overlay, savedEdits, editsApi, commitFlow, editKeys, editing, CellEditor, ValueEditor, useTableEdits, EditBar, PreviewDialog, useCommit, editActions}`（暫存的值與 reducer、依 key 疊在結果上的 overlay、workspace 的暫存檔、預覽與套用結果的判斷、按鍵與編輯器、表格資料 tab 的接線與 commit 對話框；純函式的模組都有 Vitest）；`grid/{marks, EditorSlot, renderCount}`（標記的型別與樣式、`DataGrid` 的 `edits` 介面與 inline 編輯器的位置、dev 專用的列 render 計數 `window.__gridRowRenders`）
```

  Append to the `stores/` line: `；表格資料 tab 的 grid 暫存在 stagedEdits.ts（依 tab id、在 React 外；workspace writer 與 close guard 直接讀）` (with `stagedEdits.ts` in backticks). Add to the `lib/` line, after `paramNames.ts`'s entry: `、tabActions.ts（⌘⇧P 對各 tab 動作的 registry，query tab 與 table tab 共用）` (file name in backticks). Add to the `scripts/` line: `、05b 手動檢查用的 scratch_tables.py（zz_05b_* scratch 表：以 exact name 建立與 DROP，check 列出剩下的，sql／hold 只接受提到 zz_05b_ 的語句）` (names in backticks).

  In 「架構重點」, the Grid bullet's first line becomes:

```markdown
- Grid 內編輯（⑤：05a backend、05b frontend；[spec](docs/superpowers/specs/2026-10-19-05-grid-editing-design.md)）：
```

  and after its last sub-bullet (Log) add:

```markdown
  - 前端的暫存（05b）：`features/edits/staging.ts` 是唯一的 reducer（immutable），loaded row 的變更以載入時的 key 為身分（`RowKey`＝key cell 依 key 順序的 JSON；key 欄本身可改，CR-4），新列是自己的清單，畫在最上面（最新的在最上，H3）。UPDATE 只記改的欄與它們載入時的值，輸入等於原始值就移除（沒有 no-op UPDATE）；被截斷的文字與只有預覽的 bytes 永遠不從預覽改（bytes 以空白的 hex 整個重打，H5）。標刪的列保留它的 UPDATE（隱藏），預覽只送 DELETE，DELETE commit 後 UPDATE 一起移除。每個使用者動作一步 undo（最多 100、只在記憶體；有變更被套用或 commit 後清空，H7）。暫存在 `stores/stagedEdits.ts`（React 外；`dispatchStaged` 擋超過 10,000 個變更與表的 key 已改時的新變更，H13、H15）。
  - Overlay：`overlay.ts` 只在有暫存時依 key 綁定列（Load more 只綁新的一頁），不複製結果：改過的列是新的陣列、其他列沿用原物件，上一次的 entry 在來源列、變更與問題都沒變時重用，所以暫存一格只重畫那一列（Q1；dev 的 `window.__gridRowRenders` 量，05b 實測見打包版量測）。暫存值用載入 cell 的編碼畫（D-05-10）；重讀後 server 的值不是暫存的原始值時是衝突（danger 角標、「Changed on the server: now …」、右鍵 Keep mine）。
  - 編輯器：Return／F2／雙擊開始，沒有打字即編輯；inline 的 `<input>`（`CellEditor`）或 drawer 的 CodeMirror（`ValueEditor`：json kind、含換行或超過 1,000 字元；純文字用 "text" language，不自動補括號，H10）。沒打字就什麼都不暫存：未動的 NULL／DEFAULT 照舊，打了再刪是空字串（H4）；失焦暫存，離開視窗不算；所有按鍵先過 `isImeKeyEvent`（`editKeys.ts`；⌥⌘N 看實體鍵 `KeyN`：⌥N 是 dead key）。
  - 預覽與套用：⌘S／Commit… 先 `POST /edits/preview`（直接呼叫，不是 `useQuery`），對話框顯示 backend 的句子（`begin`、各句、`commit` 原文，gutter 標 U／I／D）、警告與問題，紅色連線的 Commit 是 danger；Commit 只送畫面上那個預覽的 plan（plan id 只在對話框的 state 裡；預覽後暫存變了就重新預覽，05a M7、H8）。`useCommit` 從 execution 讀結果（`commitFlow.ts`：沒有 `edit_outcome` 就結束是 unknown，`not_connected` 是沒送出；`invalid_command` 之類的終止錯誤對 `edits` 請求不算結束，H21），寫回 staging；committed 與 partial 重讀，rolled_back 只在原本暫停時重讀，unknown 等另一個瀏覽請求的列到了之後 Commit 才恢復（Refresh，或重連後同一個瀏覽在新 session 上讀到；之後的預覽提醒新列會再新增一次，H6、H26）。`transaction_open` 提供 Roll back／Reconnect；`sql_mode_changed` 第二次起提示 Reconnect；SQLite 忙碌有自己的說明。
  - 重讀保留位置（D-05-12，CR-1：`row_editing` 的 engine）：Refresh 與 commit 後的重讀載入原本已載入的列數（500–10,000），垂直位置與選取不變；sort、filter 回到頂端；新結果的第一頁完整到達之前 grid 保留舊的列（不閃白，A12，H9）。
  - Workspace：table tab 的 `staged_edits`（`StagedEditsFile`，`extra="forbid"`：單值 1 MiB、合計 8 MiB、10,000 個，`outcome_unknown`）；前端 `savedEdits` 超過上限就整個不寫（編輯列提示「Too large to keep across a restart」），backend 的修復把模型不收的暫存丟掉、留下 tab（B40，H14）。還原時先放進 store 再還原 tab，資料載入後依 key 綁回；profile 不在了，「已關閉」清單註明捨棄了幾個暫存。
  - 關閉與其他：`CLOSE_GUARD_ORDER.edits = 5`（只在關閉時問「Discard N staged changes to <table>?」，沒有 Commit；沒顯示過的還原 tab 也問；commit 進行中不能關；guard 自己不丟暫存，tab 關掉後由 workspace writer 忘掉，H25）；有暫存的 table tab 顯示圓點；匯出對話框說明暫存不在匯出裡；⌘⇧P 有 Add row、Delete selected rows、Set NULL、Commit changes…、Discard all changes、Undo／Redo staged change（H11）。Filter by this value 用改過的 cell 載入的（server 的）值，新列沒有這個項目。
  - 引擎圖示：單色（`currentColor`，跟著文字與 muted token）；SQL Server 沒有可自由使用的商標圖（Microsoft 的商標條款要明確授權，simple-icons 13.0.0 因此移除了所有 Microsoft 圖示），維持 lucide 的 Server（H2）。
```

  In 「已知限制」, after the 「Grid 內編輯（05a…）」 group add:

```markdown
- Grid 內編輯（05b，frontend）：
  - 注音（IME）與打包版 WKWebView 是否把 ⌘Z／⇧⌘Z／⌘⌫／⌥⌘N／F2 交給頁面（A10、A11）只在使用者的測試清單；⌘Z 被 Edit 選單吃掉時用右鍵的 Revert 或 ⌘⇧P 的 Undo staged change。
  - React 接線（`useTableEdits`、`useCommit`、對話框、編輯器）沒有元件測試（H1），只有手動清單；純函式的模組有 Vitest。
  - Add row 沒有快捷鍵；沒有多格貼上；複製 DEFAULT 的 cell 得到空值（D-05-10）。
  - 瀏覽沒有 ORDER BY：commit 後新列出現在 server 給的位置（PG 的 UPDATE 也可能讓列換位置）；新列畫在最上面，commit 後它們離開頂端，選取與捲動依顯示的列號，可能差一列以上。
  - 暫存超過 8 MiB（或單值超過 1 MiB、超過 10,000 個）不跨重開（編輯列有提示）；undo 歷史與預覽的問題不跨重開；含孤立 surrogate 的值存進 workspace 時變成 U+FFFD（預覽本來就會拒絕它）。
  - 衝突標記與 Keep mine 只在重讀之後看得到（rolled_back 而瀏覽原本沒暫停時不自動重讀：按 Refresh）。
  - commit 進行中的關閉保護只在對話框之外碰得到（對話框是 modal：刪除 profile 時）。
  - backend 在 apply 途中重啟：結果不明、History 沒有紀錄（05a M9）。
```

  In 打包版量測, append one sub-bullet `  - 05b 時量：…` in the form of the 05a bullet, with Steps 2–3's numbers and Task 9 check 28's (the distinct row indexes rendered when one cell of 100,000 × 30 rows is staged).

  In 進度, row ⑤'s status becomes:

```markdown
完成：05a backend（edit target、預覽 DML、一次性 plan、在 tab 的 session 套用、結果四分類）、05b frontend（暫存、overlay、編輯器、預覽與 commit、重讀保留位置、workspace 暫存、關閉保護；引擎圖示單色）；followups：[05a](docs/superpowers/notes/2026-10-20-05a-grid-editing-backend-followups.md)、[05b](docs/superpowers/notes/2026-10-20-05b-grid-editing-frontend-followups.md)
```

- [ ] **Step 5: The spec** — these 〔05b〕 notes next to the text they refine (the text itself unchanged):

  The header's 〔05a〕 paragraph, appended:

```markdown
〔05b〕前端完成（2026-10-20）：[05b plan](../plans/2026-10-20-05b-grid-editing-frontend.md) 的規劃決定 H1–H26 記在被它們細化的文字旁，以「〔05b〕」開頭。
```

  §3.6.1, after the operations paragraph:

```markdown
〔05b〕`outcomeUnknown` 是兩段（H6）："pending"（Commit 停用，直到另一個瀏覽請求的列到了：Refresh，或重連後同一個瀏覽在新 session 上讀到，H26）→ "refreshed"（之後的預覽頂端提醒新列會再新增一次）→ 下一次 apply 後清掉；§3.7 的檔案因此多了 `outcome_unknown`。有變更被套用（partial、unknown 的 `applied`）或 commit 後 undo／redo 清空（H7）；DELETE commit 後它下面隱藏的 UPDATE 一起移除。新列的 Revert row 是把它移除（H20）。
```

  §3.6.3, after its bullets:

```markdown
〔05b〕編輯器沒打字就什麼都不暫存（H4：在 NULL 或 DEFAULT 上按 Return 再按 Return 不會變成空字串；打了再刪才是空字串）；grid 只有預覽的 bytes（超過 64 bytes）以空白開始，提示「Hex to replace all N bytes」（H5）；drawer 的純文字編輯器不自動補括號（H10）；編輯中的列離開目前的結果（重跑、sort）時，打過的字照樣暫存（H12）；編輯器裡的 ⌘S 先暫存再開預覽（H19）。
```

  §3.6.4, after the paragraph under the table:

```markdown
〔05b〕⌥⌘N 看實體鍵（`code` KeyN）：macOS 的 ⌥N 是 dead key。⌘⇧P 另有 Undo／Redo staged change（H11，A10 的備援）。選取超過 10,000 列的 ⌥⌘N／⌘⌫ 直接提示，不建任何東西（H15）。
```

  §3.6.5, after the 重跑時機 bullet:

```markdown
〔05b〕前端沒有「COMMIT 已送出」的事件：每句的 `affected` 都到了就隱藏 Cancel（之後的 Cancel 只到得了 COMMIT 前的檢查）。commit 進行中暫存鎖住；預覽後暫存變了，Commit 改成重新預覽（H8）。
```

  §3.7, after its bullets:

```markdown
〔05b〕backend 的修復把模型不收的 `staged_edits`（或不是 table tab 的）丟掉、留下 tab，WARNING 只記「dropped the staged edits」（H14）；檔案保留標刪列下面隱藏的 UPDATE；undo 歷史與問題不保存（H22）。
```

  §10, the result column of A10 and A11: `05b：使用者的最終測試清單（打包版、注音）；05b 沒有自動化驗證`. Of A12:

```markdown
〔實測〕（05b Task 6、Task 9，內建 Chromium）：重跑以「已載入列數」為第一頁，第一頁完整之前保留舊的列，捲動位置與選取不變、不閃白（原本 `useHeldResult` 在第一個 `result_set` 事件就換成 0 列的新結果，捲動被瀏覽器夾到 0，H9）；WKWebView 在使用者的清單。
```

  §11 item 2, appended:

```markdown
〔05b 完成，2026-10-20〕以上全部做完（12 個 task，沒有 05c），注音與打包版的按鍵在使用者的清單。
```

- [ ] **Step 6: The scratch tables are gone**

Run: `backend/.venv/bin/python scripts/scratch_tables.py check`
Expected: "No zz_05b_ object left".

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-19-05-grid-editing-design.md
git commit -m "docs: 05b in CLAUDE.md and the spec (grid editing frontend: staging, overlay, editors, preview and commit, workspace, close guard); ⑤ done"
```

---

## Planning rulings

Each open decision the spec left, with why and what it costs. Labels H (05a used E, F, G, L, M; 04c C).

| # | Ruling | Why | Cost |
|---|---|---|---|
| H1 | No Testing Library or jsdom: every rule that can be pure is a pure module with Vitest (`cells`, `staging`, `overlay`, `savedEdits`, `commitFlow`, `editing`, `editKeys`, `execution`, the store); hooks, components and wiring are checked by hand. | The repo has no DOM test setup (`vite.config.ts` runs `src/**/*.test.ts` in node); 04c C35 set this pattern; the decisions that lose data (NULL vs '', what an outcome means, what is sent, what is kept) are all in the pure modules. | `useTableEdits`, `useCommit`, the dialogs and the editors have no automated test (the 05a carry "component tests if Testing Library is adopted" stays open). |
| H2 | Engine icons are monochrome (`currentColor`, no colour class; the `--engine-*` tokens go). SQL Server keeps lucide's neutral `Server` glyph. | User request. No freely licensed SQL Server mark exists: simple-icons removed all Microsoft icons in 13.0.0 (PR #10019) because Microsoft's trademark terms require explicit permission (`microsoftsqlserver.svg` is in simple-icons 12.0.0, gone from 13.0.0 on); Microsoft's Azure architecture icons may be used only "in architectural diagrams, training materials, or documentation"; devicon's code is MIT but the logo stays Microsoft's trademark. The repo is public. | SQL Server is the one engine without its own mark (told apart by a different shape). |
| H3 | New rows are drawn on top, the newest first; Add row opens the editor on row 0's first writable column. | Spec R-05-3 「新列沒有 key，固定顯示在 grid 最上方」; a fixed place for the editor. | Older new rows move down one row each time. |
| H4 | An editor stages only after an input event: an untouched Return keeps NULL or DEFAULT; typed-then-erased is the empty string. | R-05-8: NULL only by an explicit action, an emptied input is ''; a Return just to look must change nothing. | Retyping the same text over a value stages nothing (the reducer drops it anyway). |
| H5 | A bytes cell the grid holds only the first 64 bytes of starts its editor empty, "Hex to replace all N bytes"; a cut text is refused with the spec's text. | Starting from the preview would stage a truncated value with a Return. | The whole value must be typed or pasted. |
| H6 | `unknownOutcome` has two phases ("pending": Commit off; "refreshed": the next preview warns new rows would be inserted again), cleared by the next apply; persisted as `StagedEditsFile.outcome_unknown` (restored as "pending"). | R-05-6 needs both phases, and the warning must survive a restart; a single boolean cannot hold them. | One field beyond §3.7's list (spec issue S1). |
| H7 | An outcome that removes changes (partial, unknown with `applied`) or a commit clears undo and redo; a committed DELETE takes the UPDATE hidden under it. | Undo must never bring back a change that took effect (it would be applied twice); the hidden UPDATE was never sent and its row is gone. | After a partial or unknown outcome the earlier steps cannot be undone. |
| H8 | While a commit runs nothing is staged (`locked`) and the grid's editing is off; Commit applies only the plan of the preview on screen — its id lives in the dialog's state alone, a late preview answer is dropped, and a staging changed since the preview (identity of its parts) is previewed again. | 05a M7 carry; R-05-7 "送出的就是預覽的". | The lock follows the commit's state a render later (the modal dialog covers that render). |
| H9 | D-05-12 through a per-job `position` (bumped by sort and filter, kept by Refresh and the post-commit re-read) and a first page as large as the rows loaded (500–10,000); the Data view holds the old rows until the new request's first page is complete (`firstPageDelivered`). | Without the hold the grid switched to a 0-row result at its first event and the browser clamped the scroll to 0 (A12, spec issue S6). CR-1: only for `row_editing` engines. | A sort or filter also waits for its whole first page before replacing the rows (500 rows: not noticeable). |
| H10 | The drawer's editor for non-JSON text is CodeMirror with a "text" language whose `closeBrackets` adds nothing; ⌘↵ / Apply stage, Esc / Cancel drop; focus leaving the drawer for the grid stages and closes it. | Spec: CodeMirror in the drawer; an auto-inserted `)` or `"` would change a value; the inline rule "leaving stages" applies to the drawer too. | None. |
| H11 | ⌘⇧P also has Undo staged change and Redo staged change. | A10: WKWebView may keep ⌘Z for its Edit menu; the palette (and the right-click Revert) are the reliable paths. | Two more palette entries on table tabs. |
| H12 | An editor unmounted while holding typed text (its row left the rows shown by a re-run or a sort) stages that text. | Q2: typed text is never lost silently. | A staged change the user did not confirm with Return (visible, undoable). |
| H13 | Changes staged on another key than the table's now (DDL since): nothing new is staged on them (`key_changed`), commit, discard, revert and undo still work; the bar says "Staged on another key". | RowKeys of the old key cannot bind new edits consistently. | After such DDL the user commits or discards first. |
| H14 | The workspace repair drops a `staged_edits` the model refuses (or one on a tab that is not a table tab) and keeps the tab, on read and on `PUT /workspace`. | B40: one bad entry must not cost every tab's saved state. | Those staged changes are lost (the WARNING counts, never shows them). |
| H15 | Set NULL, Delete rows and the menus over a selection of more than 10,000 rows are refused before anything is built; `dispatchStaged` refuses an action that would leave more than 10,000 changes. | D-05-11; keeping the UI responsive on a 100,000-row select-all. | A toast; such edits go through SQL. |
| H16 | The manual checks use `scripts/scratch_tables.py`: a fixed catalogue of `zz_05b_*` tables and views per engine, dropped by exact name, `sql` / `hold` refusing any statement that names no `zz_05b_` object, SQLite in `dev/data/scratch/zz_05b.sqlite`. Validated while planning on all four engines under `zz_plan05b_` (created, read with each adapter's `edit_target`, dropped, `check` clean): PostgreSQL's and SQLite's identity `id` are writable with a default, SQL Server's is read-only ("IDENTITY column"); MySQL's TINYINT(1) is kind number; the generated / computed columns are read-only; the views ("Views are read-only here") and the keyless tables ("No primary key or unique key on NOT NULL columns: a row cannot be identified") are not editable; the MyISAM table is `transactional: false`. | The brief: scratch tables by exact name, none left. | One more dev script. |
| H17 | A dev-only render counter (`window.__gridRowRenders`, inert in production) makes Q1's "only that row re-renders" measurable in the browser. | Spec §1 / §7 ask for a check by Profiler or counter; the in-app browser has no React Profiler. | One function call per row render in dev builds. |
| H18 | A preview's problems replace all problems; editing a change clears that change's problems. | Problems describe the last preview; a cell the user is fixing should not keep its old outline. | A value still wrong shows no outline until the next ⌘S. |
| H19 | ⌘S in an editor stages what was typed, then opens the preview. | The preview must hold what is on screen (as Numbers commits a cell on save). | None. |
| H20 | Revert row on a new row removes it. | "Revert" is back to the server's state: no row. | None. |
| H21 | A terminal error code (`invalid_command`, `no_open_result`) does not end an `edits` execution. | 05a M6: a refusal at the WebSocket layer sends `error` then `edit_outcome`; ending at the error would read as unknown. | None (other kinds unchanged). |
| H22 | Not kept in `workspace.json`: undo history, problems. | D-05-14 (memory only); problems describe a preview. | They do not survive a restart. |
| H23 | The edit bar never scrolls (it truncates, with tooltips); the dialog's lists reserve their scrollbar (`[scrollbar-gutter:stable]`), CodeMirror scrollers via `STABLE_SCROLLBAR` (the preview's statements, the drawer editor). | The user's classic scrollbars take room. | None. |
| H24 | A red connection's Commit is a danger button and the connection's name and colour lead the dialog. | M11 production colours; spec §3.6.5. | None. |
| H25 | The close guard never discards by itself: once the tab is closed the workspace writer forgets its staged edits. | A later guard (the export's) may still refuse the close; discarding first would lose the changes of a tab that stays. | A closed tab's staged edits linger in memory until the next save (≤ 500 ms). |
| H26 | After an unknown outcome, the rows of any browse request other than the one shown at the outcome end "pending" (the user's Refresh, or the same browse sent again on a new session after a reconnect); after a restore, the first rows read do. | Both are fresh reads of the table; counting runs would leave Commit off after a reconnect until a second Refresh. | None. |

## Spec issues found while planning

- **S1** (§3.6.1, §3.7): `outcomeUnknown: boolean` cannot express R-06's two phases (Commit off until a Refresh, then the next preview warns), and the §3.7 file has no field for it, so the warning is lost on a restart. H6 makes it "pending" / "refreshed" and adds `outcome_unknown` to `StagedEditsFile`.
- **S2** (§3.6.5): "Cancel（COMMIT 送出後隱藏）" — the frontend gets no event when COMMIT is sent. The plan hides Cancel once every statement's `affected` has arrived; a Cancel after that would only reach the checks before COMMIT (ms).
- **S3** (§3.6.3): the inline editor's starting text for bytes beyond the grid's 64-byte preview is not specified (the preview is not the value; H5), nor that Return on an untouched NULL must not stage '' (R-05-8 implies it; H4).
- **S4** (§3.6.6): "Filter by this value 用在改過的 cell 上時，用原始（server）的值" — after a re-read the staged original and the server's value can differ; the plan uses the loaded cell (the server's value now).
- **S5** (D-05-12 vs R-05-3): new rows are drawn on top, so after a commit they leave the top and the kept selection and scroll (by display index) land as many rows off; the browse has no ORDER BY, so PostgreSQL may also list updated rows elsewhere.
- **S6** (A12): keeping the scroll is not enough: `useHeldResult` replaced the rows at the new request's first `result_set` event (0 rows), the browser clamped the scroll to 0, and the position was lost before any row arrived. H9's hold until the first page is complete is part of D-05-12.
- **S7** (05a note): its appendix's `/edits/preview` table says the 64 MiB refusal's `detail[0].msg` is "prefixed 'Value error, '", while its fix-wave paragraph and spec §3.4 say plain text (`commit_too_large`). The plan follows the spec; `editsErrorMessage` shows `detail[0].msg` either way.
- **S8** (§3.6.4): ⌥⌘N must match the physical key (`code` KeyN): on a Mac layout ⌥N is a dead key (`key` "Dead" or "˜"). The same holds for WKWebView (A10).
- **S9** (R-05-11): "commit 進行中不能關" — the commit dialog is modal, so ⌘W and the tab strip cannot be reached during a commit; the guard matters only for a profile deletion or a programmatic close.
- **S10** (§3.1): the module list has no place for the pure logic the tests need; the plan adds `cells.ts`, `savedEdits.ts`, `commitFlow.ts`, `editing.ts`, `grid/marks.ts`, `grid/EditorSlot.tsx`, `lib/tabActions.ts` (H1).

## Appendix A: the user's final test list (05b), for the coordinator's follow-ups note

The 05a note's company database checklist stays; its UI items are now reachable from the grid (item 10).

### 05b 測試清單（Grid 內編輯）

括號裡是 dev 資料庫上已經看到的結果（05b 的手動檢查在內建 Chromium 做，打包版的 WKWebView 與注音只能你自己測）。

1. **改、增、刪、commit（四個 engine 各一次）。** 在一張可以亂改的表裡：把一個 bigint 改成 `9007199254740993`、一格設成 NULL（⌥⌘N）、另一格清成空字串，新增一列（identity／auto-increment 留 DEFAULT），刪一列 → ⌘S：預覽依序列出交易開頭、DELETE、UPDATE、INSERT、COMMIT（DELETE 在最前、紅色），文字就是送出的文字 → Commit：grid 重讀，捲動位置不變，顯示 server 存下的值，空字串和 NULL 分得出來。
2. **衝突。** 在 DbaGuy 改一格但還沒 commit，用別的工具改同一格 → Commit：「Nothing was applied」並指出那一列；Refresh 後那格有紅色角標、tooltip 寫 server 現在的值；右鍵 Keep mine 後再 commit 成功。別人改同一列的其他欄：照常 commit，別人的值留著。
3. **Refresh 保留位置。** 捲到大表中間、選一格，按 Refresh：畫面不閃白，位置與選取不變；sort／filter 回到頂端。
4. **唯讀。** view、沒有 PK／唯一鍵的表、唯讀連線：工具列寫「Read-only · 原因」，Return 照舊開值檢視器。
5. **重開 app。** 有暫存的 tab 結束 app（也試試強制結束）再開：暫存還在、綁回原本的列，tab 有圓點；⌘W 問「Discard N staged changes to …?」，Cancel 留下。
6. **注音（只能手動）。** 在 inline 編輯器與 drawer 編輯器用注音輸入中文：組字中按 Enter 選字不能暫存、不能關掉編輯器；組字中的 ⌘S 不開預覽；在 grid 上直接打注音什麼都不會發生（沒有打字即編輯，先按 Return）。
7. **打包版的按鍵（A10）。** 在 DbaGuy.app 裡、grid 有焦點時：⌘Z／⇧⌘Z 復原／重做暫存、⌘⌫ 標刪列、⌥⌘N 設 NULL、F2 開編輯器都要有作用（沒被 Edit 選單吃掉）。⌘Z 沒作用時，右鍵的 Revert 與 ⌘⇧P 的「Undo staged change」可用——請回報是哪幾個鍵。
8. **引擎圖示。** 亮色與暗色主題下，樹、tab、連線選擇器、History、⌘P 裡的引擎圖示都是單色、清楚可辨；SQL Server 是中性的 server 圖示（沒有可自由使用的官方 logo：Microsoft 的商標要授權）。
9. **（可選）結果不明。** commit 途中結束 backend 或斷網：對話框寫「may or may not have been applied」，Commit 停用到 Refresh 之後，不會自動重送；之後的預覽提醒新列會再新增一次（dev：讓 backend 重載重現過）。
10. **公司資料庫（05a 清單的 UI 項目）。** MySQL：`SELECT @@sql_mode`、含反斜線的值存回原樣、非嚴格模式下太長的字串被截斷（commit 後重讀才看得到）、MyISAM 表的預覽警告與「Partly applied」／「Outcome unknown」；SQL Server：有 `IMPLICIT_TRANSACTIONS` 的 login 應出現 Roll back／Reconnect、varchar key 的表改一格的速度、trigger、temporal／graph／ledger 表的唯讀原因、dmy 語系的日期；PostgreSQL：domain、enum、array、timestamptz 改一格不該出現假衝突。每台 server：一次 1,000 個變更的 commit 時間、進度與 Cancel。
11. **大量。** 選超過 10,000 列按 ⌘⌫：提示改用 SQL；一次刪 1,000 列的 commit 有進度。

已知、不要當成 bug 回報的限制：沒有貼上、沒有打字即編輯、Add row 沒有快捷鍵；超過 64 KiB 的值只能設 NULL；浮點、datetime2(7)、text／xml 等欄不檢查並行修改（預覽逐句列出）；MySQL／SQL Server 依 collation 比較；commit 後新列出現在 server 給的位置；超過 8 MiB 的暫存不跨重開（編輯列有提示）；undo 不跨重開。

## Self-review

- **Spec coverage.** §3.6.1 → Task 2 (every operation, undo / redo, keep mine, apply outcome, refreshed) and the store; §3.6.2 → Task 3 (binding, overlay, marks) and Task 7 (drawing them, headers) and Task 9 (the bar's counts); §3.6.3 → Task 8 (inline, drawer, refusals, blur, Add row's editor in Task 9); §3.6.4 → Task 8 (keys), Task 9 (menus, actions), Task 11 (⌘⇧P); §3.6.5 → Task 9 (the bar), Task 10 (the dialog, progress, outcomes, re-read timing); §3.6.6 → Task 9 (Return edits, Filter by value), Task 6 (Refresh), Task 5 (`applyEdits`, `edits` kind, `edit_outcome`), Task 11 (close guard order, export note); §3.7 → Task 4 (backend model, `savedEdits`, restore by key, closed-list note); D-05-10 → Tasks 2–3; D-05-11 → Tasks 2, 4, 9; D-05-12 + CR-1 → Task 6; D-05-14 → Task 2; R-05-6 → Tasks 5, 10; R-05-7 → Task 10; R-05-8, R-05-9 → Task 8; R-05-11 → Tasks 4, 11; CR-3 needs nothing new (each tab's staging is its own); CR-4 → Task 2; A10, A11 → Appendix A; A12 → Tasks 6, 9; §13 → Task 12. The 05a carries: DELETE alone (Task 2), the dialog's plan only (Task 10, H8), `detail?.[0]?.msg ?? message` (Task 5), Rollback / Reconnect on `transaction_open` (Tasks 5, 10), the reconnect hint for a repeated `sql_mode_changed` (Tasks 5, 10), the SQLite busy message (Tasks 5, 10), engine icons (Task 1), component tests (H1: not adopted, the carry stays). The brief's requests: classic scrollbars (H23), Zhuyin (Task 8, Appendix A 6), Q1 measurable (H17, Task 9 check 28), packaged-app items (Appendix A 7), scratch tables (H16, every list).
- **Placeholder scan.** Every code step has the whole file or a patch; the only figures left to fill are Task 12's measurements (a recorded number, not a design gap).
- **Type consistency.** The patches were applied in order in a scratch worktree and each task compiled (`tsc --noEmit`) on its own; the final tree passed `vitest run` (230 tests), `vite build`, and the backend tests of Task 4. Names used across tasks (`stage`, `dispatchStaged`, `overlay`, `bindRows`, `GridEditing`, `commitState`, `deliveredRequest`, `CLOSE_GUARD_ORDER.edits`, …) come from those files.
- **Review Focus.** Each of the five lines names the tests or checks that pin it and their task.
