# Sub-project ②c Editor and Query — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ① console with the real query tab — a CodeMirror 6 editor with schema-aware completion, statement-aware run modes, confirmations, transactions, per-statement results — and add crash-safe workspace restore, the Scripts and History panels, the Settings dialog, the formatter, long-query notifications, the command palette and quick open.

**Architecture:** Each query tab owns one `SessionController` (②b) and a CodeMirror `EditorView` that lives as long as the tab. Run orchestration is a small pure planner (which statements, in which order, with which confirmations) plus a runner that drives the controller statement by statement. Editor intelligence calls the ②a `/sql/*` endpoints (UTF-16 offsets are native to CodeMirror's JS strings). The workspace store mirrors `workspace.json` (debounced `PUT /workspace`) so buffers survive crashes; sessions open lazily when a tab is first shown.

**Tech Stack:** React 19, TypeScript 6, Tailwind 4 tokens, zustand 5, TanStack Query 5, CodeMirror 6 (`@codemirror/state` 6.7.6, `view` 6.43.14, `commands` 6.11.1, `language` 6.13.1, `autocomplete` 6.20.3, `search` 6.7.2, `lint` 6.9.7, `lang-sql` 6.10.0 pinned, `@lezer/highlight` 1.2.5), `@replit/codemirror-vim` 6.4.0, `sql-formatter` 15.9.0, `cmdk` 1.1.1, Tauri plugins from ②b (dialog, notification, clipboard-manager).

**Spec:** `docs/superpowers/specs/2026-10-16-02-query-workflow-design.md` (§3.5, §4.1, §4.2, §4.6–§4.11). Backend contracts: `docs/superpowers/notes/2026-10-16-02a-query-backend-followups.md`; grid/session carries: `docs/superpowers/notes/2026-10-16-02b-grid-and-data-followups.md`. Project rules: `CLAUDE.md`.

## Global Constraints

- Everything from ②b's global constraints still holds (tokens incl. `warning`, `data-selectable`, no frontend test framework, verification = `npm run typecheck && npm run build` + manual browser check with `scripts/dev.sh --web` against the dev PG at 127.0.0.1:54317 dbaguy/dbaguy and a SQLite file; `dev.sh` uses `dev/data`).
- Backend changes are allowed only where a task says so (small, with pytest coverage); `cd backend && .venv/bin/pytest -q` must stay green.
- API types only via `src/lib/api/types.ts`; never edit generated files; run `npm run gen:api` after any backend model change.
- ②a frontend contracts (binding): transaction state only from `session_state`; refresh schema with `profile_id` only; completion gets only the statement window around the cursor; one statement per execute; `ErrorPosition.column` is UTF-16 (JS `.length`); reason codes are enums mapped to text in one place.
- ②b carries (binding): add `whenSettled`, a `busy` selector and `forgetAllExcept` to the controller; the query tab forgets previous runs; `ExportDialog` is hosted by the tab (grid exposes `onExport`); warn before cancelling an export inside a manual-commit transaction; ⌘W/close guards cover running exports and open transactions.
- IME safety: every keyboard shortcut handler ignores `e.isComposing` / `key === "Process"`; CodeMirror handles composition itself — do not intercept keys inside the editor except through CodeMirror keymaps.
- Commit after every task; message ends with a blank line and `Co-Authored-By: <your model> <noreply@anthropic.com>`. Branch `feat/02-query-workflow`.

## Review Focus

1. **A pasted script with `$$` bodies, CJK and emoji** runs statement by statement with correct ranges, highlights and error jumps → Tasks 2–3.
2. **A dangerous statement in the middle of "Run all"** stops for confirmation listing exactly the flagged statements; declining runs nothing; read-only blocks show the reason → Task 2.
3. **Crash safety:** kill the backend and the app mid-edit → on restart every tab, buffer and cursor is back → Task 5.
4. **Closing a tab with an open transaction or running export** never silently loses work → Task 3.
5. **Completion under latency** (slow or unreachable server) never blocks typing and never shows stale items for an old cursor position → Task 4.

## File Structure

```
src/
  stores/settings.ts              useAppSettings (GET/PUT /settings) + derived editor/run options
  stores/workspace.ts             (reworked) tabs: query | table | object; persistence (Task 5)
  features/editor/
    CodeEditor.tsx                CM6 wrapper (one EditorView per tab, kept alive)
    extensions.ts                 base extensions: language per dialect, theme, keymaps, compartments
    theme.ts                      light/dark editor theme from CSS tokens
    statements.ts                 split cache (/sql/split) + statement-under-cursor field + highlight
    completion.ts                 completion source over /sql/complete (window, debounce, abort)
    describe.ts                   ⌥click / F1 describe popover over /sql/describe
    diagnostics.ts                error marks (lint) from execution errors
    format.ts                     sql-formatter wrapper (⌘⇧F)
  features/query/
    QueryTab.tsx                  toolbar + editor + results area
    QueryToolbar.tsx              run / cancel / connection / auto-commit / commit / rollback / timeout / badge
    runPlan.ts                    pure: which statements to run (cursor / selection / all)
    useQueryRunner.ts             analyze → confirm → sequential execute → results
    ConfirmRunDialog.tsx          confirmation for flagged statements
    ResultsArea.tsx               per-statement result tabs + Messages
    TransactionBadge.tsx, CloseTabGuard.tsx, reasons.ts (reason code → text)
  features/scripts/ScriptsPanel.tsx, useScripts.ts, SaveAsDialog.tsx
  features/history/HistoryPanel.tsx, useHistory.ts
  features/settings/SettingsDialog.tsx
  features/palette/CommandPalette.tsx, QuickOpen.tsx, commands.ts
  features/notifications/useLongQueryNotifications.ts
  app/Sidebar.tsx                 Connections | Scripts | History tabs
  (deleted) features/console/
```

---

### Task 1: Settings store, CodeMirror editor and the query tab shell (replaces the console)

**Files:**
- Modify: `package.json` (CodeMirror packages at the versions above, `@replit/codemirror-vim@6.4.0`), `src/stores/workspace.ts` (tab model), `src/features/workspace/Workspace.tsx`, `src/features/explorer/Explorer.tsx` ("New Query"), `src/features/table-data/TableDataTab.tsx` ("Open in query tab")
- Create: `src/stores/settings.ts`, `src/features/editor/CodeEditor.tsx`, `src/features/editor/extensions.ts`, `src/features/editor/theme.ts`, `src/features/query/QueryTab.tsx`, `src/features/query/QueryToolbar.tsx`
- Delete: `src/features/console/` (after the query tab runs statements)

**Interfaces:**
```ts
// stores/settings.ts
export type AppSettings = components["schemas"]["AppSettings"];   // via types.ts alias
export function useAppSettings(): { settings: AppSettings; isLoading: boolean; save: (next: AppSettings) => Promise<void> };
//   GET /settings (react-query key ["settings"], staleTime Infinity); save = PUT /settings then setQueryData.
//   Until loaded, `settings` is the documented defaults (editor.font_size 13, vim false, word_wrap false, tab_size 2,
//   keyword_case "upper", run.default "statement", continue_on_error false, page_size 500, statement_timeout_s 0,
//   safety.idle_transaction_warn_minutes 3, rollback 0, notifications.long_query_seconds 15, scripts_dir null).
// stores/workspace.ts — tab model (Task 5 adds persistence)
export interface QueryTabState {
  id: string; kind: "query"; title: string;
  profileId: string | null; database: string | null;
  scriptPath: string | null; savedText: string | null;   // Task 6
  initialText: string;                                     // editor content when the tab was created/restored
  initialCursor?: number; initialSelection?: { anchor: number; head: number };
  autocommit: boolean;                                     // desired mode, applied when the session opens
}
export interface TableTabState { id: string; kind: "table"; profileId: string; path: PathSegment[]; title: string }
export interface ObjectTabState { id: string; kind: "object"; profileId: string; path: PathSegment[]; title: string }
export type Tab = QueryTabState | TableTabState | ObjectTabState;
//   openObject(profileId, node, profileName) → table tab for table/view/materialized_view/partition, else object tab
//   openQuery(profileId | null, database, options?: { text?: string; scriptPath?: string; title?: string })
//   closeTab, activate, updateTab(id, patch)
// features/editor/CodeEditor.tsx
export interface CodeEditorHandle {
  view: EditorView;
  getText(): string; setText(text: string): void;
  focus(): void; selectRange(from: number, to: number): void;
}
export function CodeEditor(props: {
  initialText: string; initialSelection?: { anchor: number; head: number };
  dialect: Dialect | null;                // language mode; null → generic SQL
  onChange?: (text: string) => void;      // debounced by the caller
  onSelectionChange?: (sel: { anchor: number; head: number }) => void;
  extensions?: Extension[];               // tab-specific (completion, statements, diagnostics…) added by later tasks
  ref?: Ref<CodeEditorHandle>;
}): JSX.Element;
```

Rules:
- **One EditorView per tab, created once** (in a `useLayoutEffect`), destroyed on unmount; hidden tabs stay mounted (Workspace already hides instead of unmounting) so undo history and scroll survive tab switches. Settings changes reconfigure through `Compartment`s (font size, tab size, word wrap, vim, language/dialect) — never recreate the view.
- Base extensions: line numbers, history, drawSelection, multiple selections (`EditorState.allowMultipleSelections`), bracket matching, close brackets, indent on input, highlight active line, search (`⌘F`, `⌘G`, select next occurrence `⌘D`), comment toggle (`⌘/`), `indentWithTab`, the SQL language from `@codemirror/lang-sql` (`PostgreSQL` or `SQLite` dialect, `upperCaseKeywords` from `keyword_case`), syntax highlighting with a theme built from the CSS tokens (light and dark follow `prefers-color-scheme`), vim (`@replit/codemirror-vim`) when `editor.vim`.
- **Query tab layout:** toolbar (Task 1: connection label, Run ⌘↵, Run all ⌘⇧↵, Cancel; Task 3 adds the rest) / vertical split editor | results (`react-resizable-panels`, default 55/45, `PanelHandle`). In this task Run executes the selection or the whole text as **one** statement through the tab's `useSession` + `DataGrid` (Task 2 replaces it with real orchestration); keep `forget` of the previous request.
- A query tab with `profileId === null` shows "Choose a connection" in the toolbar (picker in Task 3); the editor works without a connection.
- `Workspace.tsx` routes `query` → `QueryTab`, `table` → `TableDataTab`, `object` → `ObjectDetailView`. The explorer's "New Query" and the table tab's "Open in query tab" call the new `openQuery`. Delete `features/console/` once the query tab executes and shows results; move anything still shared (e.g. `SessionError`, transaction badge) to the right feature folder.

- [ ] **Step 1:** install the CodeMirror packages (exact versions) and the vim package; add `AppSettings` alias; create `stores/settings.ts`.
- [ ] **Step 2:** create `theme.ts`, `extensions.ts`, `CodeEditor.tsx`.
- [ ] **Step 3:** rework the workspace tab model and routing; create `QueryTab.tsx`, `QueryToolbar.tsx`; wire New Query / Open in query tab; delete the console.
- [ ] **Step 4:** verify typecheck/build. **Manual check:** open a query tab, type with Zhuyin (IME composition works, ⌘↵ during composition does not run), multi-cursor with ⌥-click and ⌘D, ⌘F search, ⌘/ comment, undo survives switching tabs, dark mode highlighting readable, run `SELECT * FROM big_table` → grid with load more, a table tab's "Open in query tab" prefills the editor.
- [ ] **Step 5:** commit `feat(query): CodeMirror query tab replaces the console`.

---

### Task 2: Run orchestration — statements, confirmations, results, error marks

**Files:**
- Modify: `src/features/session/SessionController.ts` (`whenSettled`, `busy`, `forgetAllExcept`)
- Create: `src/features/editor/statements.ts`, `src/features/editor/diagnostics.ts`, `src/features/query/runPlan.ts`, `src/features/query/useQueryRunner.ts`, `src/features/query/ConfirmRunDialog.tsx`, `src/features/query/ResultsArea.tsx`, `src/features/query/reasons.ts`
- Modify: `src/features/query/QueryTab.tsx`, `QueryToolbar.tsx`

**Interfaces:**
```ts
// SessionController additions
whenSettled(requestId: string): Promise<ExecutionState>;   // resolves when status is "paused" or "done"
export function busy(snapshot: SessionSnapshot | null): boolean;   // any execution running, or an export unfinished
forgetAllExcept(keep: Iterable<string>): void;
// statements.ts
export interface StatementRange { from: number; to: number; terminatorEnd: number }   // UTF-16 offsets into the doc
export const statementsField: StateField<{ version: number; ranges: StatementRange[] | null }>;
export function statementsExtension(opts: { dialect: () => Dialect | null }): Extension;
//   Debounce 150 ms after doc changes → POST /sql/split {text, dialect} → dispatch ranges (dropped if the doc changed
//   since the request); until fresh, `statementAt` falls back to the last ranges mapped through changes.
export function statementAt(state: EditorState, pos: number): StatementRange | null;   // the range containing pos, else the previous one
export function currentStatementHighlight(): Extension;   // faint `selected`-token background on the cursor's statement
export function flashRange(view: EditorView, from: number, to: number): void;           // 300 ms flash on run
// runPlan.ts
export type RunMode = "default" | "all";
export interface PlannedStatement { sql: string; from: number; to: number }
export async function planRun(view: EditorView, mode: RunMode, runDefault: "statement" | "all", dialect: Dialect): Promise<PlannedStatement[]>;
//   selection non-empty → split the selected text (POST /sql/split) and offset by selection.from;
//   else mode "all" or runDefault "all" → all ranges; else the statement under the cursor. Empty/comment-only → [].
// useQueryRunner.ts
export interface RunStep { statement: PlannedStatement; requestId: string | null; status: "pending" | "running" | "done" | "error" | "cancelled" | "skipped" }
export interface RunState { id: string; steps: RunStep[]; active: boolean; startedAt: number; finishedAt: number | null }
export function useQueryRunner(args: { controller: SessionController | null; profileId: string | null; settings: AppSettings;
  timeoutS: number | null }): { run: RunState | null; start(planned: PlannedStatement[]): Promise<void>; cancel(): void };
// reasons.ts
export function reasonText(reason: GuardReason): string;   // e.g. delete_without_where → "DELETE without WHERE"
```

Rules (spec §4.1):
- **⌘↵** runs `planRun(view, "default", settings.run.default)`; **⌘⇧↵** runs all. Before sending anything: `POST /sql/analyze {profile_id, statements}`; statements with action `block` → the dialog shows them with "Blocked on a read-only connection" and only offers Close; statements with `confirm` are listed (statement preview, 3 lines max, and `reasonText` for each reason) with **Run** / **Cancel**; confirming sends those statements with `confirmed: true`. Nothing is sent when the user cancels.
- **Sequential execution:** for each statement: `controller.execute(sql, {pageSize: settings.run.page_size, timeoutS, confirmed, history: true})` → `await whenSettled(id)` → error and `!continue_on_error` → stop (remaining steps `skipped`). A result that is paused stays paused only for the **last** statement (the backend discards a paused result when the next execute arrives; earlier result tabs show "N rows (partial)" and no Load more). `confirmation_required` returned by the backend despite analyze (e.g. the profile changed) shows the same dialog for that single statement.
- **Cancel** cancels the running statement and stops the run.
- **Results area** (spec §4.1): one tab per result set titled by the first 30 characters of its statement (`Result 2` when a statement returns several), plus **Messages**: per statement — elapsed, affected rows, notices, errors, "Timed out after N s", "Cancelled", "Skipped". A new run clears previous results (`forgetAllExcept` the new run's ids). The `DataGrid` gets `onExport` → the tab's `ExportDialog` (Task 3 hosts it; until then keep the grid's own).
- **Error marks:** an error with `position` maps to a doc offset: statement `from` + offset of (line, column) inside the statement text, where column counts UTF-16 units (JS strings, so `line start + column - 1`). Add a lint diagnostic (severity error, message) at that offset to the end of the token; clicking an error in Messages selects that position and focuses the editor. Without a position, mark the whole statement range.
- **Flash** the executed range of each statement when it starts.
- The toolbar's Run buttons are disabled while `busy(snapshot)`; the statement under the cursor is always highlighted faintly.

```ts
// runPlan.ts — offset mapping helper used by diagnostics (complete code)
export function positionToOffset(statementText: string, statementFrom: number, line: number, column: number): number {
  let offset = 0;
  for (let l = 1; l < line; l += 1) {
    const next = statementText.indexOf("\n", offset);
    if (next < 0) return statementFrom + statementText.length;
    offset = next + 1;
  }
  return statementFrom + Math.min(offset + Math.max(0, column - 1), statementText.length);
}
```

- [ ] **Step 1:** controller additions; `statements.ts`, `diagnostics.ts`, `runPlan.ts`, `reasons.ts`.
- [ ] **Step 2:** `useQueryRunner.ts`, `ConfirmRunDialog.tsx`, `ResultsArea.tsx`; wire into the query tab.
- [ ] **Step 3:** verify typecheck/build. **Manual check (dev PG):** a script `SELECT 1; SELECT '😀' AS e; DO $$ BEGIN PERFORM 1; END $$; SELECT * FROM big_table;` — ⌘↵ with the cursor in each statement runs only that one (highlight correct, emoji does not shift ranges); ⌘⇧↵ runs all with one result tab per result and Messages; `SELECT 1; DELETE FROM sales.orders; SELECT 2` (standard profile) asks to confirm only the DELETE (Cancel → nothing ran; confirm inside a `BEGIN`… then `ROLLBACK`); an error `SELECT nope FROM big_table` marks the exact token, clicking the message jumps there; a read-only profile shows the block reason; `SELECT pg_sleep(10)` with timeout 2 s → "Timed out after 2 s"; continue-on-error off stops at the first error.
- [ ] **Step 4:** commit `feat(query): statement-aware runs with confirmations, per-statement results and error marks`.

---

### Task 3: Transactions, connection picker, tab chrome and close guards

**Files:**
- Create: `src/features/query/TransactionBadge.tsx` (moved/extended from the console), `src/features/query/CloseTabGuard.tsx`, `src/features/query/ConnectionPicker.tsx`
- Modify: `QueryToolbar.tsx`, `QueryTab.tsx`, `Workspace.tsx` (tab tint, close guard, ⌘W), `src/features/grid/DataGrid.tsx` (`onExport` prop; dialog hosted by the tab), `src/features/grid/ExportDialog.tsx` (transaction warning)

Rules (spec §4.2, ②b carries):
- **Connection picker** in the toolbar: profile (from `useProfiles`, grouped by folder) and, for PG, database (from the explorer's children of the profile — `/metadata/children` at the root); changing it closes the old session and opens a new one (confirm first if a transaction is open). New tabs default to the explorer's selected profile.
- **Auto-commit** toggle (default on); applying it sends `set_autocommit`; while a transaction is open the toggle is disabled with a tooltip ("Commit or roll back first"). The desired mode is stored on the tab (`autocommit`) and applied when the session opens.
- **Commit / Rollback** buttons enabled only `inTransaction`; Commit on a failed transaction shows the backend warning ("The transaction had failed, so COMMIT rolled it back") as a toast.
- **Badge** (from `session_state`): `warning` token "Transaction · mm:ss" counting from `transaction_started_at`; `danger` "Transaction aborted — Rollback required". Session messages (`request_id ""`: idle warnings, idle rollback) → toast (warning) and the badge pulses for 3 s.
- **Tab chrome:** profile colour tints the tab label underline and the editor's top border (2 px); red profiles also show the connection name on a red chip in the toolbar. A query tab title shows `•` when its script is dirty (Task 6) and a spinner while a run is active.
- **Close guard** (tab close button, middle click, ⌘W, closing via palette): transaction open → dialog **Commit / Rollback / Cancel** (commit/rollback then close); export running → "Stop the export and close?" (cancel export then close); otherwise close immediately. Never lose work silently.
- **Export hosting:** `DataGrid` exposes `onExport()`; the query tab and the table tab host `ExportDialog` (so a remounting grid never kills a running export). When the session is in a transaction in manual mode, the dialog warns "Cancelling the export aborts the open transaction" next to Cancel and asks for confirmation before cancelling.

- [ ] **Step 1:** picker, badge, toolbar controls; session message toasts.
- [ ] **Step 2:** tab tint, dirty/spinner markers, close guard (all close paths), export hosting + warning.
- [ ] **Step 3:** verify typecheck/build. **Manual check:** auto-commit off → `INSERT` into a TEMP table → badge counts; Rollback clears; `SELECT 1/0` inside a transaction → red badge; Commit → toast that it rolled back; set idle warn to 0.1 min in settings (Task 8 not ready → temporarily via `PUT /settings` in devtools) and wait → toast + pulse; close the tab with an open transaction → dialog, each choice works; switch connection mid-transaction → confirm; a red profile shows the chip; export during manual transaction → warning on cancel.
- [ ] **Step 4:** commit `feat(query): transaction controls, connection picker, tab tint and close guards`.

---

### Task 4: Editor intelligence — completion, describe, keyword case

**Files:**
- Create: `src/features/editor/completion.ts`, `src/features/editor/describe.ts`
- Modify: `src/features/query/QueryTab.tsx` (add the extensions with the tab's profile/database)

**Interfaces:**
```ts
export function sqlCompletion(ctx: () => { profileId: string | null; database: string | null; dialect: Dialect | null }): Extension;
export function describeOnDemand(ctx: () => { profileId: string | null; database: string | null }): Extension;   // ⌥click and F1
```

Rules (spec §3.4, §4.11, ②a contracts):
- **Window:** send only the statement around the cursor: use `statementAt(state, pos)` (`from`..`terminatorEnd`, or the gap between statements) widened to at most 20,000 characters around the cursor; `text = doc.sliceString(from, to)`, `cursor = pos - from`; map the returned `start/end` back by adding `from`.
- **Debounce 30 ms**, one `AbortController` per request, abort the previous one; drop responses whose doc version or cursor changed. Explicit completion (`Ctrl+Space`) bypasses the debounce. Never await longer than 1.5 s (abort → no items) so typing never waits.
- Items map to CodeMirror completions: `label`, `type` (kind → keyword/type/property/function/…), `detail`, `apply` (backend's quoted text when present), `boost`. Keywords insert in upper case when `keyword_case === "upper"`, otherwise preserve the typed prefix's case.
- No profile → `dialect` from settings-free fallback (`postgres`) with keywords only (the backend accepts `dialect` without a profile).
- **Describe:** ⌥click on an identifier or F1 → `POST /sql/describe` (same window rule) → a tooltip/popover listing columns (name, type, PK badge, FK target); Escape closes; nothing found → a short "No table here" tooltip for 1.5 s.

- [ ] **Step 1:** create both extensions; add to the query tab.
- [ ] **Step 2:** verify typecheck/build. **Manual check:** `SELECT * FROM sal` → `sales.orders`…; `SELECT o.| FROM sales.orders o` → columns; `JOIN customers c ON |` → FK join condition; `"Order Items"` quoted on apply; CTE names complete; typing fast never lags (watch for stale popups); with the backend stopped completion silently shows nothing and typing is unaffected; F1 on `sales.orders` shows columns with the FK to customers.
- [ ] **Step 3:** commit `feat(editor): schema-aware completion and describe popover`.

---

### Task 5: Workspace persistence and crash-safe restore

**Files:**
- Modify: `src/stores/workspace.ts` (load/save), `src/App.tsx` (restore before render), `src/features/query/QueryTab.tsx` (report text/selection changes), `src/app/Sidebar.tsx` (sidebar tab persisted — created in Task 6; if Task 6 is not done yet, persist `sidebar_tab` as `null`)
- Backend (small): none required — `WorkspaceState`/`WorkspaceTab` already carry everything (kind, title, profile_id, database, script_path, object_path, buffer, cursor, selection, autocommit).

Rules (spec §4.8):
- **Mapping:** query tab ↔ `{kind:"query", id, title, profile_id, database, script_path, buffer, cursor, selection, autocommit}`; table/object tab ↔ `{kind, id, title, profile_id, object_path}`. `active_tab_id`, `sidebar_tab`.
- **Load** on startup (`GET /workspace`) before the workspace renders (a small "Restoring…" state); tabs whose profile no longer exists open with `profileId: null` (query) or are dropped with a toast (table/object). Sessions are **not** opened on load: a query tab opens its session the first time it is shown.
- **Save:** every workspace mutation and every editor change (text, cursor/selection) marks dirty; a single debounced writer (500 ms) sends the whole state with `PUT /workspace`; write only when the serialized state changed; on `beforeunload`/`pagehide` flush synchronously with `navigator.sendBeacon`-style `fetch(..., {keepalive: true})` (include the auth header); a failed save retries with backoff and shows one warning toast after 3 consecutive failures.
- Editor changes reach the store without re-rendering the editor: the query tab keeps the latest text in a ref and calls `workspace.markEdited(tabId, text, selection)` which updates a non-reactive buffer map used by the serializer (do not put every keystroke into React state).

- [ ] **Step 1:** load/save in the store; restore flow in `App.tsx`; query tab reports edits.
- [ ] **Step 2:** verify typecheck/build. **Manual check:** open 3 query tabs (one bound to nothing, one with unsaved text, one long script with the cursor in the middle), a table tab and an object tab; reload the page → everything back including cursor; kill the backend process and the browser tab mid-typing, restart `dev.sh` → the last ≤0.5 s of typing may be lost, nothing else; delete a profile → its query tab reopens without a connection.
- [ ] **Step 3:** commit `feat(workspace): crash-safe workspace persistence and restore`.

---

### Task 6: Sidebar tabs and the Scripts panel

**Files:**
- Create: `src/app/Sidebar.tsx`, `src/features/scripts/ScriptsPanel.tsx`, `src/features/scripts/useScripts.ts`, `src/features/scripts/SaveAsDialog.tsx`
- Modify: `src/App.tsx`, `src/features/query/QueryTab.tsx` (⌘S/⌘⇧S, dirty), `src/stores/workspace.ts` (script binding)

Rules (spec §4.7):
- Sidebar: **Connections | Scripts | History** tabs (History content in Task 7); the selected tab persists via the workspace (`sidebar_tab`).
- Scripts tree from `GET /scripts` (folders first); refresh button; root path shown muted at the top (from `ScriptTree.root`); context menu: New file (prompt name → `PUT /scripts/file` with empty content, open it), New folder, Rename (inline prompt → `POST /scripts/rename`), Delete (confirm → `DELETE /scripts/file`). Backend 4xx messages show as toasts (dot paths, conflicts, non-`.sql`).
- Double-click a file → open in a query tab bound to it (`scriptPath`, `savedText` = content, title = file name); already open → activate. The connection: the tab's last used profile (stored in the workspace) or, the first time, the explorer's selected profile.
- **⌘S** saves a bound tab (`PUT /scripts/file`), updates `savedText`; an unbound tab behaves like **⌘⇧S**: Save As dialog — folder picker from the tree + file name (`.sql` appended when missing) → `PUT` → the tab becomes bound. Dirty = text ≠ savedText → `•` on the tab title; closing a dirty bound tab asks Save / Discard / Cancel (combined with the Task 3 close guard).

- [ ] **Step 1:** sidebar + scripts panel + hooks; script binding and save flows.
- [ ] **Step 2:** verify typecheck/build. **Manual check:** create folder/file, open, edit (dot appears), ⌘S (dot clears, file content updated on disk under `dev/data/scripts`), ⌘⇧S from an unbound tab, rename and delete with confirmation, `../x.sql` in the name → error toast, restore after reload keeps the binding and dirty state.
- [ ] **Step 3:** commit `feat(scripts): scripts panel with file-bound query tabs`.

---

### Task 7: History panel

**Files:**
- Create: `src/features/history/HistoryPanel.tsx`, `src/features/history/useHistory.ts`
- Modify: `src/app/Sidebar.tsx`, `src/features/query/QueryTab.tsx` (insert at cursor)

Rules (spec §4.6):
- Search box (debounced 200 ms) → `GET /history?q=&profile_id=&limit=100`; a filter toggle "This connection / All"; infinite scroll with `before = next_before`.
- Rows: time (relative, absolute in tooltip), connection name, first line of SQL (single line, ellipsis), status icon (ok / error / cancelled / timeout), duration.
- Single click → preview below the list (full SQL, read-only, `data-selectable`, error text when failed). Double click → insert the SQL at the cursor of the active query tab (new tab when none); ⌥double-click → new query tab with the SQL on that row's profile. "Clear history" (confirm) → `DELETE /history`.
- After each run the list refreshes (invalidate the query when a run finishes).

- [ ] **Step 1:** create the panel and hook; insertion into the active editor via the tab's `CodeEditorHandle` (register handles in a small map keyed by tab id).
- [ ] **Step 2:** verify typecheck/build. **Manual check:** run a few statements (one failing), search `big` (trigram) and `1` (short), filter by connection, scroll past 100, double-click inserts, ⌥double-click opens a tab, clear history empties.
- [ ] **Step 3:** commit `feat(history): searchable query history panel`.

---

### Task 8: Settings dialog, formatter, notifications, connection safety level

**Files:**
- Create: `src/features/settings/SettingsDialog.tsx`, `src/features/editor/format.ts`, `src/features/notifications/useLongQueryNotifications.ts`
- Modify: `package.json` (`sql-formatter@15.9.0`), `src/features/connections/ConnectionDialog.tsx` (safety level select), `src/features/query/QueryTab.tsx` (⌘⇧F, notifications hook), `src-tauri/capabilities/default.json` (narrow notifications to `notification:allow-notify`, `notification:allow-request-permission`, `notification:allow-is-permission-granted`), `src-tauri/capabilities` + `lib.rs` only if the folder picker needs `dialog:allow-open`

Rules (spec §4.9, §4.11):
- **Settings dialog (⌘,):** sections Editor (font size 8–32, vim, word wrap, tab size 1–8, keyword case), Run (default statement/all, continue on error, page size 1–10,000, default statement timeout s), Safety (idle warn minutes, idle rollback minutes, 0 = off), Scripts folder (path input; in Tauri a "Choose…" button using the dialog plugin's `open({directory: true})` — add `dialog:allow-open` for this only), Notifications (long query seconds). Save → `PUT /settings`; 422 messages shown inline; changes apply live to every open editor (Task 1 compartments) and to new runs.
- **Formatter (⌘⇧F):** formats the selection, else the statement under the cursor, with `sql-formatter` (`language: "postgresql" | "sqlite"`, `keywordCase: settings.editor.keyword_case === "upper" ? "upper" : "preserve"`, `tabWidth: settings.editor.tab_size`) as one undoable change; a formatter error → toast, text unchanged.
- **Notifications:** when a run finishes after ≥ `long_query_seconds` and `!appIsFocused()`, `notify("Query finished in 42 s", "1,234 rows · <connection>")`; failures notify "Query failed after 42 s" with the first error line.
- **Connection dialog:** a "Safety" select (Relaxed / Standard / Strict) with one-line explanations, saved in the profile (`safety`).

- [ ] **Step 1:** dialog, formatter, notifications, safety select; capability changes.
- [ ] **Step 2:** verify typecheck/build (+ `cd src-tauri && cargo check` if Rust/capabilities changed). **Manual check:** change font size / vim / wrap → open editors update live; page size 50 → next run pages at 50; ⌘⇧F on a messy statement and on a selection, undo restores; a profile set to Strict asks to confirm an INSERT; notifications in browser mode (grant permission, unfocus the window during `pg_sleep(16)`).
- [ ] **Step 3:** commit `feat(app): settings dialog, SQL formatter, long-query notifications, profile safety level`.

---

### Task 9: Command palette and quick open

**Files:**
- Create: `src/features/palette/CommandPalette.tsx`, `src/features/palette/QuickOpen.tsx`, `src/features/palette/commands.ts`
- Modify: `package.json` (`cmdk@1.1.1`), `src/App.tsx` (global shortcuts)

Rules (spec §4.10):
- **⌘⇧P command palette** (cmdk): New query, Run, Run all, Format, Toggle vim, Toggle auto-commit, Commit, Rollback, Refresh schema (`POST /metadata/schema/refresh {profile_id}` for the active tab's profile, then invalidate explorer queries), Close tab (goes through the close guard), Settings, Open history, Open scripts. Commands that need an active query tab are disabled (shown greyed) otherwise. Each command shows its shortcut.
- **⌘P quick open:** one list with sections — Open tabs, Connections (opens a new query tab), Scripts (file names from the cached tree), Database objects (`GET /metadata/search?profile_id=<active tab's profile>&q=` debounced 150 ms, top 50; selecting opens the object via `openObject` with the hit's path). Typing filters locally for tabs/connections/scripts (cmdk's fuzzy filter) and remotely for objects; no profile → the objects section shows "Open a connection to search objects".
- Global shortcuts ignore IME composition and do not fire while a dialog other than the palette is open.

- [ ] **Step 1:** commands registry + palette + quick open + shortcuts.
- [ ] **Step 2:** verify typecheck/build. **Manual check:** ⌘⇧P → every command works (Commit/Rollback in a transaction; Close tab hits the guard); ⌘P → `big` finds `public.big_table` and opens its table tab; a script opens; a connection opens a new query tab; fuzzy `usrrol`-style query works for objects.
- [ ] **Step 3:** commit `feat(app): command palette and quick open`.

---

## Rulings recorded while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| R-02c-1 | Per-tab statement timeout is kept in memory only (defaults to `run.statement_timeout_s`); not persisted in `workspace.json`. | The backend model has no field for it; not worth a model change now. | The timeout resets to the default after a restart. |
| R-02c-2 | Earlier result tabs of a multi-statement run show their loaded rows without Load more. | The backend discards a paused result when the next statement runs (one connection). | Users re-run a single statement to page further. |
| R-02c-3 | Completion is capped at a 20,000-character window and a 1.5 s budget. | Keeps per-keystroke cost bounded (②a measured 66 ms on 100 KB). | Very long single statements complete only near the cursor. |
| R-02c-4 | Workspace edits are buffered outside React state and saved by one debounced writer. | Keystrokes must not re-render the tab tree. | Slightly more plumbing in the store. |
