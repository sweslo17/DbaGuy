# Sub-project ②b Grid and Data — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give query results and table data a real data grid: virtualized rows and columns, selection, keyboard, copy in every format, value viewer and record view, a table-data tab with filter/sort/count, and export to file — on top of a reusable session controller that understands the extended execution protocol from ②a.

**Architecture:** A framework-free `SessionController` (zustand vanilla store) owns one backend session and its WebSocket and folds every server event into per-request execution state plus session-level state. React components subscribe through `useSession`. The grid is a custom DOM grid on TanStack Virtual (both axes), with selection kept as an anchor/focus rectangle (never per-cell). Copy and export go through the backend formatter (`/sql/format-rows`, WS `export`). The ① console keeps working on the new controller and the new grid until ②c replaces it with the query tab.

**Tech Stack:** React 19, TypeScript 6, Vite 8, Tailwind 4 (tokens in `src/styles.css`), zustand 5, TanStack Query 5 / Virtual 3, radix-ui, lucide-react, Tauri 2 (+ `@tauri-apps/plugin-dialog` 2.8.3, `@tauri-apps/plugin-notification` 2.7.0).

**Spec:** `docs/superpowers/specs/2026-10-16-02-query-workflow-design.md` (§3.5, §4.3–§4.5). Backend contracts: `docs/superpowers/notes/2026-10-16-02a-query-backend-followups.md` ("Frontend contracts"). Project rules: `CLAUDE.md`.

## Global Constraints

- No frontend test framework (spec §6). Every task ends with `npm run typecheck && npm run build` passing and the task's **manual check** done in browser mode: `scripts/dev.sh --web`, open `http://localhost:1420`, use a profile for the dev PG (`127.0.0.1:54317`, dbaguy/dbaguy; `public.big_table` has 100,000 rows) and a SQLite file. If you cannot drive a browser, say so in the report and list the checks for the controller to run.
- API types come only from `src/lib/api/types.ts` (aliases over the generated `schema.d.ts`; add aliases there, never edit `schema.d.ts`). Backend changes are out of scope for this plan.
- Frontend contracts from ②a (binding): transaction state only from `session_state`; schema refresh by `profile_id` only; one statement per execute; `ErrorPosition.column` is UTF-16; reason codes are typed enums; tolerate a `session_state` before every `finished` and at every pause.
- Colours, fonts and radii only through the existing tokens (`bg`, `panel`, `elevated`, `border`, `fg`, `muted`, `accent`, `accent-fg`, `danger`, `success`, `hover`, `selected`; `font-mono`). Light and dark mode must both work.
- Text the user may want to select is marked `data-selectable` (the app disables selection elsewhere).
- No `localStorage` for anything that must persist reliably (spec §2: workspace/settings are backend files). Filter history is a convenience and may use `localStorage` inside try/catch (ruling R-02b-1).
- DRY: one session controller for every session user (console, table data, ②c query tab); one grid; one copy/export path.
- Commit after every task; message ends with a blank line and `Co-Authored-By: <your model> <noreply@anthropic.com>`. Branch `feat/02-query-workflow`.

## Review Focus

1. **100,000 rows × 50 columns** stay smooth to scroll and select: both axes virtualized, selection is a rectangle, aggregates capped → manual check in Tasks 3–4.
2. **A copy larger than the backend accepts** (> 100,000 rows) gives a clear message and never freezes the UI → Task 5.
3. **The socket drops mid-request** → unfinished executions become "interrupted", load-more is disabled, nothing stays "Running…" → Task 2.
4. **A broken table filter** (`WHERE nonsense`) shows the database error under the filter bar and keeps the text for editing; an empty filter clears the WHERE → Task 7.
5. **Export while a page is paused** ends the paused result cleanly (no stuck "Load more"), shows progress and can be cancelled → Task 8.

## File Structure

```
src/
  lib/native.ts                     save dialog + notifications (Tauri or browser fallback)
  lib/cells.ts                      (extended) cell kinds, display text, tooltip, alignment
  lib/api/types.ts                  (extended) aliases for new schema types
  components/ui/ContextMenu.tsx     (extended) submenus, disabled items
  components/ui/Toast.tsx           toast store + <Toaster/>
  features/session/
    execution.ts                    ExecutionState + applyEvent (moved from console, extended)
    SessionController.ts            one backend session: socket, executions, session state
    useSession.ts                   React hook over a controller
  features/grid/
    DataGrid.tsx                    virtualized grid component
    columns.ts                      width estimation / autofit
    selection.ts                    selection model (pure)
    aggregate.ts                    status-bar aggregates (pure)
    copy.ts                         copy via /sql/format-rows
    GridMenu.tsx                    context menu items
    StatusBar.tsx                   selection count + aggregates
    ValueViewer.tsx, JsonTree.tsx, RecordView.tsx
    ExportDialog.tsx
  features/table-data/
    TableDataTab.tsx                Data | Columns | DDL
    useTableData.ts                 browse state (filter, sorts, SQL, requests)
    FilterBar.tsx
    filterValue.ts                  "filter by this value" SQL fragment (pure)
  features/explorer/ObjectDetailView.tsx   (split) ColumnsView / DdlView reused by the table tab
  features/console/                 (kept until ②c) uses useSession + DataGrid
  stores/workspace.ts               (extended) openQuery(..., initialSql)
```

---

### Task 1: Native bridge — save dialog and notifications

**Files:**
- Modify: `package.json` (`@tauri-apps/plugin-dialog@^2.8.3`, `@tauri-apps/plugin-notification@^2.7.0`), `src-tauri/Cargo.toml` (`tauri-plugin-dialog = "2.8"`, `tauri-plugin-notification = "2.7"`), `src-tauri/src/lib.rs` (register both plugins), `src-tauri/capabilities/default.json` (`dialog:allow-save`, `notification:default`)
- Create: `src/lib/native.ts`

**Interfaces:**
- Produces:
  - `chooseSavePath(defaultName: string, filters: { name: string; extensions: string[] }[]): Promise<string | null>` — Tauri: the native save dialog (null when cancelled); browser mode: always `null` (callers show a path input instead, see `canChooseFiles`).
  - `canChooseFiles(): boolean` — true only in Tauri.
  - `notify(title: string, body: string): Promise<void>` — Tauri: request permission once if needed, then send; browser: `Notification` API when already granted, otherwise nothing. Never throws (log a warning).
  - `appIsFocused(): boolean` — `document.hasFocus()`.

```ts
import { isTauriRuntime } from "./backend";

export interface FileFilter {
  name: string;
  extensions: string[];
}

export function canChooseFiles(): boolean {
  return isTauriRuntime();
}

export async function chooseSavePath(defaultName: string, filters: FileFilter[]): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { save } = await import("@tauri-apps/plugin-dialog");
  return (await save({ defaultPath: defaultName, filters })) ?? null;
}

export async function notify(title: string, body: string): Promise<void> {
  try {
    if (isTauriRuntime()) {
      const n = await import("@tauri-apps/plugin-notification");
      let granted = await n.isPermissionGranted();
      if (!granted) granted = (await n.requestPermission()) === "granted";
      if (granted) n.sendNotification({ title, body });
    } else if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(title, { body });
    }
  } catch (error) {
    console.warn("Notification failed", error);
  }
}

export function appIsFocused(): boolean {
  return document.hasFocus();
}
```

- [ ] **Step 1:** add the npm packages (`npm install @tauri-apps/plugin-dialog@^2.8.3 @tauri-apps/plugin-notification@^2.7.0`) and the Rust crates; register `.plugin(tauri_plugin_dialog::init())` and `.plugin(tauri_plugin_notification::init())` next to the log plugin in `lib.rs`; add the two permissions.
- [ ] **Step 2:** create `src/lib/native.ts` as above.
- [ ] **Step 3:** verify: `npm run typecheck && npm run build`; `cd src-tauri && cargo check` (must compile; fix versions to what resolves if a patch version differs, keeping the major.minor).
- [ ] **Step 4:** commit `feat(app): native save dialog and notifications (Tauri plugins)`.

---

### Task 2: Session controller and execution state v2

**Files:**
- Create: `src/features/session/execution.ts`, `src/features/session/SessionController.ts`, `src/features/session/useSession.ts`
- Modify: `src/lib/api/types.ts` (aliases: `SessionStateEvent`, `GuardReason`, `Classification`, `FormatKind`, `RowFormatOptions`, `Dialect`), `src/features/console/QueryConsole.tsx` (use the controller)
- Delete: `src/features/console/execution.ts`, `src/features/console/useConsoleSession.ts`

**Interfaces (produced, used by every later task and by ②c):**
```ts
// execution.ts
export type ExecutionKind = "execute" | "export" | "transaction";
export interface ResultSetState { index: number; columns: ColumnMeta[]; rows: unknown[][]; rowCount: number; hasMore: boolean }
export interface ExecutionState {
  requestId: string;
  kind: ExecutionKind;
  sql: string;                     // "" for transaction commands
  status: "running" | "paused" | "done";
  resultSets: ResultSetState[];
  affected: { index: number; rows: number }[];
  messages: { severity: string; text: string }[];
  error: { message: string; code?: string | null; position?: { line: number; column: number } | null } | null;
  confirmation: { reasons: GuardReason[]; classification: Classification } | null;
  progressRows: number | null;     // export only
  elapsedMs: number | null;
  startedAt: number;               // Date.now() when created
  cancelled: boolean;
  timedOut: boolean;
  skipped: boolean;                // confirmation required: nothing ran
  interrupted: boolean;
}
export function newExecution(requestId: string, kind: ExecutionKind, sql: string): ExecutionState;
export function interruptExecution(state: ExecutionState): ExecutionState;
export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState;

// SessionController.ts
export type SessionPhase = "opening" | "open" | "reconnecting" | "failed" | "closed";
export interface SessionStateView {
  autocommit: boolean; inTransaction: boolean; transactionFailed: boolean;
  transactionStartedAt: string | null; idleSeconds: number | null;
}
export interface SessionMessage { id: number; severity: string; text: string; at: number }
export interface SessionSnapshot {
  phase: SessionPhase;
  info: SessionInfo | null;
  error: Error | null;               // opening failed (ApiError, e.g. password_required) or the session is gone
  state: SessionStateView | null;    // last session_state
  executions: Record<string, ExecutionState>;
  sessionMessages: SessionMessage[]; // request_id "" (idle warnings, idle rollback)
}
export interface ExecuteOptions { pageSize?: number; timeoutS?: number | null; confirmed?: boolean; history?: boolean }
export const PAGE_SIZE = 500;
export class SessionController {
  readonly store: StoreApi<SessionSnapshot>;
  constructor(profileId: string, database: string | null);
  open(): Promise<void>;                   // idempotent
  close(): void;                           // socket + DELETE /sessions/{id}; idempotent
  execute(sql: string, options?: ExecuteOptions): string;          // returns request id
  fetchMore(requestId: string, count?: number): void;
  discard(requestId: string): void;
  cancel(requestId: string): void;
  commit(): string; rollback(): string; setAutocommit(enabled: boolean): string;
  exportResult(sql: string, format: FormatKind, path: string, options: RowFormatOptions): string;
  forget(requestId: string): void;          // drop finished state from the store
  whenDone(requestId: string): Promise<ExecutionState>;
}
// useSession.ts
export function useSession(profileId: string, database: string | null, options?: { enabled?: boolean }):
  { controller: SessionController | null; snapshot: SessionSnapshot | null; retry: () => void };
```

Rules:
- **Routing:** `session_state` → `snapshot.state`; `message` with `request_id === ""` → `sessionMessages` (keep the last 50); every other event → `applyEvent` on `executions[event.request_id]` if present (unknown ids ignored).
- **applyEvent additions** (keep every ① rule — terminal error codes, never wiping state on unknown events, `hasMore` false at `finished`): `confirmation_required` → `confirmation`; `progress` → `progressRows`; `finished` → `timedOut`, `skipped`, `cancelled`, `elapsedMs`, `status: "done"`; `session_state` is never applied to an execution.
- **Lifecycle:** `open()` = `POST /sessions` then `SessionSocket.connect()`. Phase: `opening` → `open` on the first socket open; on socket close → `reconnecting` (or `failed` with `Error("The session is no longer available.")` when the socket is rejected); when the socket opens again after a close, mark every unfinished execution `interruptExecution` and send `cancel` for it **unless** the last known state says `inTransaction` (cancelling inside a transaction would abort it — same reasoning as the ① console). An open failure sets `phase: "failed"` and `error`.
- **Closing:** `close()` sets `closed`, closes the socket and calls `DELETE /sessions/{id}` (fire and forget, warn on failure). Calls after `close()` are no-ops.
- **Commands** send through the socket only while `phase === "open"`; otherwise the execution is created already `done` with `error: {message: "Not connected", code: "not_connected"}` (never queue user statements for replay).
- `whenDone` resolves immediately for a done execution, else on the store update that makes it done (or interrupted).
- **useSession:** creates the controller in an effect (React StrictMode-safe: cleanup closes it), only when `enabled !== false`; `retry()` closes the current controller and creates a new one; `snapshot` via `useStore(controller.store)`.

`execution.ts` (complete code):
```ts
import type { Classification, ColumnMeta, GuardReason, ServerEvent } from "@/lib/api/types";

export type ExecutionKind = "execute" | "export" | "transaction";

export interface ResultSetState {
  index: number;
  columns: ColumnMeta[];
  rows: unknown[][];
  rowCount: number;
  hasMore: boolean;
}

export interface ExecutionState {
  requestId: string;
  kind: ExecutionKind;
  sql: string;
  status: "running" | "paused" | "done";
  resultSets: ResultSetState[];
  affected: { index: number; rows: number }[];
  messages: { severity: string; text: string }[];
  error: { message: string; code?: string | null; position?: { line: number; column: number } | null } | null;
  confirmation: { reasons: GuardReason[]; classification: Classification } | null;
  progressRows: number | null;
  elapsedMs: number | null;
  startedAt: number;
  cancelled: boolean;
  timedOut: boolean;
  skipped: boolean;
  interrupted: boolean;
}

export const INTERRUPTED_MESSAGE = "Connection interrupted — results may be incomplete";
/** Error codes the backend sends for requests it never ran; the request is over even if `finished` goes missing. */
const TERMINAL_ERROR_CODES = new Set(["no_open_result", "invalid_command"]);

export function newExecution(requestId: string, kind: ExecutionKind, sql: string): ExecutionState {
  return {
    requestId, kind, sql, status: "running", resultSets: [], affected: [], messages: [], error: null,
    confirmation: null, progressRows: null, elapsedMs: null, startedAt: Date.now(),
    cancelled: false, timedOut: false, skipped: false, interrupted: false,
  };
}

export function interruptExecution(state: ExecutionState): ExecutionState {
  if (state.status === "done") return state;
  return {
    ...state,
    status: "done",
    interrupted: true,
    messages: [...state.messages, { severity: "warning", text: INTERRUPTED_MESSAGE }],
    resultSets: state.resultSets.map((s) => ({ ...s, hasMore: false })),
  };
}

function updateSet(state: ExecutionState, index: number, fn: (set: ResultSetState) => ResultSetState): ExecutionState {
  return { ...state, resultSets: state.resultSets.map((s) => (s.index === index ? fn(s) : s)) };
}

export function applyEvent(state: ExecutionState, event: ServerEvent): ExecutionState {
  if (state.interrupted || event.type === "session_state") return state;
  switch (event.type) {
    case "started":
      return { ...state, status: "running" };
    case "result_set":
      return {
        ...state,
        resultSets: [...state.resultSets, { index: event.index, columns: event.columns, rows: [], rowCount: 0, hasMore: false }],
      };
    case "rows":
      return updateSet(state, event.index, (s) => ({ ...s, rows: s.rows.concat(event.rows) }));
    case "result_set_end": {
      const next = updateSet(state, event.index, (s) => ({ ...s, rowCount: event.row_count, hasMore: event.has_more }));
      return event.has_more ? { ...next, status: "paused" } : next;
    }
    case "affected":
      return { ...state, affected: [...state.affected, { index: event.index, rows: event.rows }] };
    case "message":
      return { ...state, messages: [...state.messages, { severity: event.severity, text: event.text }] };
    case "error": {
      const error = { message: event.message, code: event.code, position: event.position };
      const terminal = event.code != null && TERMINAL_ERROR_CODES.has(event.code);
      return terminal ? { ...state, error, status: "done" } : { ...state, error };
    }
    case "confirmation_required":
      return { ...state, confirmation: { reasons: event.reasons, classification: event.classification } };
    case "progress":
      return { ...state, progressRows: event.rows };
    case "finished":
      return {
        ...state,
        status: "done",
        elapsedMs: event.elapsed_ms,
        cancelled: event.cancelled,
        timedOut: event.timed_out,
        skipped: event.skipped,
        resultSets: state.resultSets.map((s) => ({ ...s, hasMore: false })),
      };
    default:
      return state; // an event type this client does not know must never wipe the state
  }
}
```
(`rows` concatenation must stay linear in total rows: `concat` on each page is fine at 500-row pages; do not copy every set on every event — `updateSet` only rebuilds the touched set.)

`SessionController.ts` — implement to the rules above with `createStore` from `zustand/vanilla`, the existing `SessionSocket`, `api()`/`unwrap()`; one private `send(command)` that refuses when not open; one private `track(kind, sql, command)` that adds `newExecution` and sends. Keep each method short; no React imports.

- [ ] **Step 1:** add the type aliases to `src/lib/api/types.ts` (`SessionStateEvent = Extract<ServerEvent, {type: "session_state"}>`, `GuardReason = S["GuardReason"]`, `Classification = S["Classification"]`, `FormatKind = S["FormatKind"]`, `RowFormatOptions = S["RowFormatOptions"]`, `Dialect = S["Dialect"]` — use the names the generated schema actually has).
- [ ] **Step 2:** create `execution.ts`, `SessionController.ts`, `useSession.ts`.
- [ ] **Step 3:** port `QueryConsole.tsx` to `useSession` + controller (one current request id in component state; `controller.execute(text, {pageSize: PAGE_SIZE})`, `fetchMore`, `cancel`); keep its UI and the "unlock" flow (open errors come from `snapshot.error`); delete the two old console files. The console now shows a small transaction badge from `snapshot.state` ("Transaction" orange / "Rollback required" red) — this is the only new console UI in this task.
- [ ] **Step 4:** verify `npm run typecheck && npm run build`. **Manual check:** run a query in the console; `SELECT * FROM big_table` pauses at 500 and Load more works; `SELECT pg_sleep(30)` → Cancel works; `BEGIN` shows the transaction badge, `ROLLBACK` clears it; stop the backend (`kill` the uvicorn process started by `dev.sh`, then restart `dev.sh`) during `SELECT pg_sleep(30)` → the request shows "Connection interrupted" and the console can run again after reconnect (if the session is gone, the error and Retry appear).
- [ ] **Step 5:** commit `refactor(frontend): session controller and execution state for the extended protocol`.

---

### Task 3: Data grid foundation (rendering, columns, load more)

**Files:**
- Modify: `src/lib/cells.ts`
- Create: `src/features/grid/columns.ts`, `src/features/grid/DataGrid.tsx`
- Modify: `src/features/console/QueryConsole.tsx` (render `DataGrid` for each result set); Delete: `src/features/console/ResultTable.tsx`

**Interfaces:**
```ts
// cells.ts
export type CellKind = "null" | "empty" | "number" | "bool" | "json" | "bytes" | "truncated" | "text";
export interface CellDisplay { text: string; kind: CellKind; title?: string; alignRight: boolean }
export function describeCell(value: unknown, category: ColumnCategory): CellDisplay;
export function formatBytes(size: number): string;   // "1.2 KB" style, base 1024, one decimal
// columns.ts
export const ROW_HEIGHT = 22, HEADER_HEIGHT = 40, ROW_NUMBER_MIN = 44, MIN_WIDTH = 60, MAX_WIDTH = 400, AUTOFIT_MAX = 600;
export function estimateWidths(columns: ColumnMeta[], rows: unknown[][], measure: TextMeasure): number[];
export function autofitWidth(column: ColumnMeta, rows: unknown[][], col: number, measure: TextMeasure): number;
export type TextMeasure = (text: string, font: "cell" | "header" | "type") => number;
export function canvasMeasure(): TextMeasure;   // one shared canvas, fonts read from CSS (font-mono 12px cells)
// DataGrid.tsx
export interface DataGridProps {
  columns: ColumnMeta[];
  rows: unknown[][];
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore?: () => void;
  dialect: Dialect;                                   // copy/INSERT formats (Task 5)
  tableName?: { schema: string | null; name: string } | null;
  sort?: { column: number; descending: boolean }[];   // table data view (Task 7)
  onHeaderClick?: (column: number, additive: boolean) => void;
  extraCellMenu?: (row: number, column: number) => MenuItem[];
  // Task 8 adds `exportSource?: ExportSource | null`.
}
```

Rendering rules (spec §4.3):
- Header shows name (semibold) and type (muted, smaller) on two lines; `HEADER_HEIGHT` 40; row height 22; both axes virtualized with TanStack Virtual (`useVirtualizer` for rows, `useVirtualizer({horizontal: true})` for columns); overscan 10 rows / 3 columns.
- The row-number column is always fixed (sticky left), width from the digit count of `rows.length` (min `ROW_NUMBER_MIN`). "Pin up to here" (Task 5 menu) freezes columns `0..k` next to it (sticky left, rendered outside the column virtualizer); state `pinnedCount`.
- Widths: initial `estimateWidths` on the first rows (header text vs. first 100 rows' display text, clamp 60–400, +16 px padding); recomputed only when the column list changes (not on every page). Drag the header's right edge to resize (min 60); double-click the edge → `autofitWidth` over the loaded rows (sample at most 2,000 evenly spaced rows, clamp 60–600).
- Cells: `describeCell`. NULL → italic muted "NULL"; empty string → blank with `title="empty string"`; number category or numeric value → right aligned; bool → `true`/`false`; JSON (category json, or object/array text) → compact one-line preview; bytes → `0x<preview>…` + muted ` (1.2 KB)`; truncated text → preview + `…` + muted size in characters. Text never wraps (single line, ellipsis).
- Load more: when the last rendered row index ≥ `rows.length - 50`, `hasMore` and not `loadingMore` → `onLoadMore()`; a footer row shows "Loading more…" or a "Load more" button when `hasMore`.
- The grid root is a focusable element (`tabIndex={0}`) — keyboard handling arrives in Task 4.

```ts
// columns.ts (core)
const PADDING = 16;
const SAMPLE_ROWS = 100;
const AUTOFIT_SAMPLE = 2000;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

function headerWidth(column: ColumnMeta, measure: TextMeasure): number {
  return Math.max(measure(column.name, "header"), measure(column.type_name, "type"));
}

function cellWidth(value: unknown, column: ColumnMeta, measure: TextMeasure): number {
  const display = describeCell(value, column.category);
  return measure(display.text.slice(0, 200), "cell");
}

export function estimateWidths(columns: ColumnMeta[], rows: unknown[][], measure: TextMeasure): number[] {
  const sample = rows.slice(0, SAMPLE_ROWS);
  return columns.map((column, c) => {
    let width = headerWidth(column, measure);
    for (const row of sample) width = Math.max(width, cellWidth(row[c], column, measure));
    return clamp(Math.ceil(width) + PADDING, MIN_WIDTH, MAX_WIDTH);
  });
}

export function autofitWidth(column: ColumnMeta, rows: unknown[][], c: number, measure: TextMeasure): number {
  const step = Math.max(1, Math.floor(rows.length / AUTOFIT_SAMPLE));
  let width = headerWidth(column, measure);
  for (let r = 0; r < rows.length; r += step) width = Math.max(width, cellWidth(rows[r][c], column, measure));
  return clamp(Math.ceil(width) + PADDING, MIN_WIDTH, AUTOFIT_MAX);
}
```

- [ ] **Step 1:** extend `cells.ts` (keep `BYTES_PREVIEW` in sync with the backend; `formatCell` callers switch to `describeCell`).
- [ ] **Step 2:** create `columns.ts` and `DataGrid.tsx`.
- [ ] **Step 3:** console renders each result set with `DataGrid` (height: fill the results panel), passing `onLoadMore` → `controller.fetchMore`; delete `ResultTable.tsx`.
- [ ] **Step 4:** verify typecheck/build. **Manual check:** `SELECT * FROM big_table` then keep scrolling to load several pages — smooth scrolling, header stays, row numbers stay; `SELECT * FROM public.blobs` shows bytes with size, big numbers right aligned; `SELECT NULL, '', true, '{"a":1}'::json` renders per the rules; a 60-column query (`SELECT g AS c1, g AS c2, …` or `SELECT * FROM information_schema.columns`) scrolls horizontally smoothly; resize and double-click autofit work; dark mode looks right.
- [ ] **Step 5:** commit `feat(grid): virtualized data grid with column sizing and incremental loading`.

---

### Task 4: Selection, keyboard and status-bar aggregates

**Files:**
- Create: `src/features/grid/selection.ts`, `src/features/grid/aggregate.ts`, `src/features/grid/StatusBar.tsx`
- Modify: `src/features/grid/DataGrid.tsx`

**Interfaces:**
```ts
// selection.ts (pure, complete code below)
export interface CellPos { row: number; col: number }
export type SelectionMode = "cells" | "rows" | "columns";
export interface GridSelection { anchor: CellPos; focus: CellPos; mode: SelectionMode }
export interface Rect { top: number; left: number; bottom: number; right: number }   // inclusive
export interface Dims { rows: number; cols: number }
export function rectOf(sel: GridSelection, dims: Dims): Rect;
export function cellCount(rect: Rect): number;
export function selectCell(pos: CellPos): GridSelection;
export function extendTo(sel: GridSelection, pos: CellPos): GridSelection;
export function selectColumns(anchorCol: number, focusCol: number, dims: Dims): GridSelection;
export function selectRows(anchorRow: number, focusRow: number, dims: Dims): GridSelection;
export function selectAll(dims: Dims): GridSelection;
export type MoveKey = "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight" | "Home" | "End" | "PageUp" | "PageDown";
export function move(sel: GridSelection, key: MoveKey, opts: { extend: boolean; toEdge: boolean; page: number }, dims: Dims): GridSelection;
export function clampSelection(sel: GridSelection, dims: Dims): GridSelection | null;
// aggregate.ts
export interface Aggregates { cells: number; nonNull: number; numeric: number; sum?: number; avg?: number; min?: number; max?: number; approximate: boolean; capped: boolean }
export function aggregate(rect: Rect, rows: unknown[][], columns: ColumnMeta[], cap?: number): Aggregates;
```

```ts
// selection.ts
export interface CellPos { row: number; col: number }
export type SelectionMode = "cells" | "rows" | "columns";
export interface GridSelection { anchor: CellPos; focus: CellPos; mode: SelectionMode }
export interface Rect { top: number; left: number; bottom: number; right: number }
export interface Dims { rows: number; cols: number }
export type MoveKey = "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight" | "Home" | "End" | "PageUp" | "PageDown";

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export function rectOf(sel: GridSelection, dims: Dims): Rect {
  const top = Math.min(sel.anchor.row, sel.focus.row);
  const bottom = Math.max(sel.anchor.row, sel.focus.row);
  const left = Math.min(sel.anchor.col, sel.focus.col);
  const right = Math.max(sel.anchor.col, sel.focus.col);
  if (sel.mode === "rows") return { top, bottom, left: 0, right: dims.cols - 1 };
  if (sel.mode === "columns") return { top: 0, bottom: dims.rows - 1, left, right };
  return { top, bottom, left, right };
}

export const cellCount = (r: Rect) => Math.max(0, r.bottom - r.top + 1) * Math.max(0, r.right - r.left + 1);
export const selectCell = (pos: CellPos): GridSelection => ({ anchor: pos, focus: pos, mode: "cells" });
export const extendTo = (sel: GridSelection, pos: CellPos): GridSelection => ({ ...sel, focus: pos });

export function selectColumns(anchorCol: number, focusCol: number, dims: Dims): GridSelection {
  return { anchor: { row: 0, col: anchorCol }, focus: { row: Math.max(0, dims.rows - 1), col: focusCol }, mode: "columns" };
}

export function selectRows(anchorRow: number, focusRow: number, dims: Dims): GridSelection {
  return { anchor: { row: anchorRow, col: 0 }, focus: { row: focusRow, col: Math.max(0, dims.cols - 1) }, mode: "rows" };
}

export function selectAll(dims: Dims): GridSelection {
  return { anchor: { row: 0, col: 0 }, focus: { row: dims.rows - 1, col: dims.cols - 1 }, mode: "cells" };
}

export function move(
  sel: GridSelection, key: MoveKey, opts: { extend: boolean; toEdge: boolean; page: number }, dims: Dims,
): GridSelection {
  const { row, col } = sel.focus;
  const lastRow = dims.rows - 1;
  const lastCol = dims.cols - 1;
  let next: CellPos;
  switch (key) {
    case "ArrowUp": next = { row: opts.toEdge ? 0 : row - 1, col }; break;
    case "ArrowDown": next = { row: opts.toEdge ? lastRow : row + 1, col }; break;
    case "ArrowLeft": next = { row, col: opts.toEdge ? 0 : col - 1 }; break;
    case "ArrowRight": next = { row, col: opts.toEdge ? lastCol : col + 1 }; break;
    case "Home": next = opts.toEdge ? { row: 0, col: 0 } : { row, col: 0 }; break;
    case "End": next = opts.toEdge ? { row: lastRow, col: lastCol } : { row, col: lastCol }; break;
    case "PageUp": next = { row: row - opts.page, col }; break;
    case "PageDown": next = { row: row + opts.page, col }; break;
  }
  next = { row: clamp(next.row, 0, lastRow), col: clamp(next.col, 0, lastCol) };
  return opts.extend ? { ...sel, focus: next } : selectCell(next);
}

export function clampSelection(sel: GridSelection, dims: Dims): GridSelection | null {
  if (dims.rows === 0 || dims.cols === 0) return null;
  const fix = (p: CellPos): CellPos => ({ row: clamp(p.row, 0, dims.rows - 1), col: clamp(p.col, 0, dims.cols - 1) });
  return { ...sel, anchor: fix(sel.anchor), focus: fix(sel.focus) };
}
```

```ts
// aggregate.ts
import type { ColumnMeta } from "@/lib/api/types";
import type { Rect } from "./selection";

const NUMERIC = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
export const AGGREGATE_CAP = 200_000;

export interface Aggregates {
  cells: number; nonNull: number; numeric: number;
  sum?: number; avg?: number; min?: number; max?: number;
  approximate: boolean;   // a value was beyond exact double precision
  capped: boolean;        // the selection is larger than the cap: only `cells` is meaningful
}

function numberOf(value: unknown): { value: number; exact: boolean } | null {
  if (typeof value === "number") return { value, exact: Number.isSafeInteger(value) || !Number.isInteger(value) };
  if (typeof value === "string" && NUMERIC.test(value.trim())) {
    const digits = value.replace(/[^0-9]/g, "").replace(/^0+/, "").length;
    return { value: Number(value), exact: digits <= 15 };
  }
  return null;
}

export function aggregate(rect: Rect, rows: unknown[][], columns: ColumnMeta[], cap = AGGREGATE_CAP): Aggregates {
  const cells = Math.max(0, rect.bottom - rect.top + 1) * Math.max(0, rect.right - rect.left + 1);
  const result: Aggregates = { cells, nonNull: 0, numeric: 0, approximate: false, capped: cells > cap };
  if (result.capped) return result;
  let sum = 0;
  for (let r = rect.top; r <= rect.bottom; r += 1) {
    const row = rows[r];
    if (!row) continue;
    for (let c = rect.left; c <= rect.right; c += 1) {
      const value = row[c];
      if (value === null || value === undefined) continue;
      result.nonNull += 1;
      if (columns[c]?.category !== "number") continue;
      const n = numberOf(value);
      if (!n || !Number.isFinite(n.value)) continue;
      result.numeric += 1;
      result.approximate ||= !n.exact;
      sum += n.value;
      result.min = result.min === undefined ? n.value : Math.min(result.min, n.value);
      result.max = result.max === undefined ? n.value : Math.max(result.max, n.value);
    }
  }
  if (result.numeric > 0) {
    result.sum = sum;
    result.avg = sum / result.numeric;
  }
  return result;
}
```

Interaction rules (spec §4.3):
- Mouse: mousedown on a cell selects it (shift: extend from the anchor); dragging extends (auto-scroll when the pointer leaves the viewport edge); header click selects the column (shift: column range); row-number click selects the row (shift: row range); the top-left corner selects all. A right-click inside the selection keeps it; outside moves the selection to that cell.
- Keyboard (grid focused): arrows / shift+arrows; ⌘+arrow → edge; Home/End (⌘ → first/last cell); PageUp/PageDown by the visible row count; ⌘A select all; Escape collapses to the focus cell; ⌘C copy TSV and Enter / Space open the value viewer / record view — wire those keys to callbacks that Tasks 5 and 6 fill in (until then no-ops).
- Selection survives new pages (clamp when the row count changes; reset when columns change). The focus cell scrolls into view.
- Selected cells use the `selected` token; the focus cell gets a 1 px `accent` outline.
- Status bar under the grid: `N rows · M columns` of the result plus, when more than one cell is selected, `K cells` and (numeric) `Sum · Avg · Min · Max` (prefix `≈` when `approximate`; when `capped`, show only the count). Compute aggregates in a `useMemo` keyed by the rect and the row count.

- [ ] **Step 1:** create `selection.ts`, `aggregate.ts`, `StatusBar.tsx`.
- [ ] **Step 2:** wire mouse, keyboard and rendering into `DataGrid`.
- [ ] **Step 3:** verify typecheck/build. **Manual check (big_table, 100k rows loaded by scrolling a few pages):** click, shift-click, drag across columns, header and row-number selection, ⌘A — all instant; status bar sums `value` correctly for a small selection (compare with `SELECT sum(value) FROM big_table WHERE id <= 10`); arrows/PageDown/⌘↓ move and scroll; selecting all 100k × 3 shows counts without freezing.
- [ ] **Step 4:** commit `feat(grid): rectangular selection, keyboard navigation and status-bar aggregates`.

---

### Task 5: Copy, context menu and toasts

**Files:**
- Create: `src/features/grid/copy.ts`, `src/features/grid/GridMenu.tsx`, `src/components/ui/Toast.tsx`
- Modify: `src/components/ui/ContextMenu.tsx` (submenus + disabled items), `src/features/grid/DataGrid.tsx`, `src/app/AppShell.tsx` (mount `<Toaster/>`), `src/features/connections/hooks.ts` (`useDialect(profileId)`)

**Interfaces:**
```ts
// Toast.tsx
export type ToastKind = "info" | "success" | "warning" | "error";
export function toast(kind: ToastKind, text: string): void;      // module-level zustand store; auto-dismiss 4 s (errors 8 s)
export function Toaster(): JSX.Element;                           // bottom-right stack, max 4 visible
// ContextMenu.tsx
export interface MenuItem { label: string; onSelect?: () => void; danger?: boolean; disabled?: boolean; separatorBefore?: boolean; children?: MenuItem[] }
// copy.ts
export type CopyFormat = "tsv" | "csv" | "json" | "markdown" | "insert" | "in_list" | "column_names";
export const COPY_ROW_LIMIT = 100_000;
export async function copySelection(format: CopyFormat, input: {
  rect: Rect; rows: unknown[][]; columns: ColumnMeta[]; dialect: Dialect;
  tableName?: { schema: string | null; name: string } | null;
}): Promise<void>;   // writes the clipboard; toasts success, backend warnings (truncated values), errors
// hooks.ts
export function useDialect(profileId: string): Dialect | null;   // profile.engine → EngineInfo.dialect
```

Rules:
- `column_names` is client-side (selected column names joined by tab). Every other format posts `/sql/format-rows {dialect, kind, columns: [{name, category}], rows, options: {header, null_text: "", table_schema, table_name}}` with the selected rectangle's cells (encoded, as the grid holds them).
- `header`: true for CSV/TSV/Markdown when the selection spans whole columns or more than one row; single-cell TSV copies just the value (no header).
- More than `COPY_ROW_LIMIT` rows → no request; toast warning "Selection too large to copy (N rows). Use Export instead.". `in_list` with more than one column → toast error from the backend's 422 message.
- Backend `warnings` → toast warning; success → toast "Copied N rows as CSV" (info, short).
- Clipboard failure (webview refused) → toast error.
- Context menu on a cell: **Copy** (⌘C, TSV), **Copy as ▸** (TSV, CSV, JSON, Markdown, SQL INSERT, IN list, Column names), separator, **View value** (Enter), **Record view** (Space), **Pin columns up to here** / **Unpin columns**, plus `extraCellMenu(row, col)` items (table data adds "Filter by this value"), plus **Export…** when `exportSource` is set (Task 8). Header context menu: Copy column name, Pin columns up to here, Sort ascending/descending (only when `onHeaderClick` exists).

- [ ] **Step 1:** extend `ContextMenu` (Radix `Sub`, `SubTrigger`, `SubContent`; `disabled` maps to Radix `disabled`), create `Toast.tsx` and mount it.
- [ ] **Step 2:** create `copy.ts`, `GridMenu.tsx`, `useDialect`; wire ⌘C and the menus into `DataGrid` (console passes its profile's dialect).
- [ ] **Step 3:** verify typecheck/build. **Manual check:** copy a 3×3 block as TSV, CSV (with header), JSON, Markdown, INSERT (PG and SQLite profiles — SQLite booleans 1/0, PG `'\x…'::bytea` for full bytes from `blobs`), IN list from one column; copy a 64 KB+ text cell as INSERT → warning toast about truncation; select all 100,000 rows and copy → "too large" toast immediately; paste results into a text editor to confirm.
- [ ] **Step 4:** commit `feat(grid): copy in every format, context menus and toasts`.

---

### Task 6: Value viewer and record view

**Files:**
- Create: `src/features/grid/ValueViewer.tsx`, `src/features/grid/JsonTree.tsx`, `src/features/grid/RecordView.tsx`
- Modify: `src/features/grid/DataGrid.tsx` (drawer host + Enter/Space + menu items)

**Interfaces:**
```ts
export type GridDrawer = { kind: "value" } | { kind: "record" } | null;
export function ValueViewer(props: { value: unknown; column: ColumnMeta; onClose: () => void }): JSX.Element;
export function JsonTree(props: { value: unknown; onCopyPath: (path: string) => void }): JSX.Element;
export function RecordView(props: { columns: ColumnMeta[]; row: unknown[] | undefined; rowIndex: number; rowCount: number;
  onMove: (delta: -1 | 1) => void; onClose: () => void }): JSX.Element;
export function jsonPath(segments: (string | number)[]): string;   // $.a[0]["b c"]
```

Rules (spec §4.3):
- The drawer opens on the grid's right (resizable split with `react-resizable-panels`, default 360 px, min 240 px) and **follows the focus cell** (value viewer) or the focus row (record view). Escape or the close button closes it and returns focus to the grid.
- Value viewer by kind:
  - text: monospace, wrapping, `data-selectable`, size shown in characters (`Array.from(text).length`); truncated text shows the preview plus "Showing the first 65,536 of N characters — export the result for the full value."
  - JSON (category `json`, or text that starts with `{`/`[` and parses): a collapsible tree (objects/arrays collapsed beyond depth 2 by default), click a key to copy its path (`jsonPath`), "Copy JSON" button (pretty-printed).
  - bytes: hex dump, 16 bytes per line with offsets (`00000010  de ad be ef …`), total size, and a note when the preview is shorter than the value.
  - NULL / bool / number: the value and its type.
  - Copy value button for every kind (full preview text).
- Record view: two columns (name + type | value via `describeCell`, wrapping), prev/next buttons and ↑/↓ while the drawer has focus, "Row N of M".
- `jsonPath` code:
```ts
const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
export function jsonPath(segments: (string | number)[]): string {
  return "$" + segments.map((s) => (typeof s === "number" ? `[${s}]` : IDENT.test(s) ? `.${s}` : `[${JSON.stringify(s)}]`)).join("");
}
```

- [ ] **Step 1:** create the three components and wire Enter/Space/menu items.
- [ ] **Step 2:** verify typecheck/build. **Manual check:** `SELECT profile FROM customers` (jsonb) → tree, copy a nested path; `SELECT data FROM blobs` → hex dump; `SELECT repeat('x', 70000)` → truncated note; record view on `big_table` moves with ↑/↓ and buttons; the drawer follows arrow-key moves in the grid.
- [ ] **Step 3:** commit `feat(grid): value viewer with JSON tree and hex dump; record view`.

---

### Task 7: Table data tab (Data | Columns | DDL)

**Files:**
- Create: `src/features/table-data/TableDataTab.tsx`, `src/features/table-data/useTableData.ts`, `src/features/table-data/FilterBar.tsx`, `src/features/table-data/filterValue.ts`
- Modify: `src/features/explorer/ObjectDetailView.tsx` (extract `ColumnsView` and `DdlView`, keep `ObjectDetailView` for non-relations), `src/features/workspace/Workspace.tsx` (route relation object tabs to `TableDataTab`), `src/stores/workspace.ts` (`openQuery(profileId, database, profileName, initialSql?)`), `src/features/console/QueryConsole.tsx` (accept `initialSql`)

**Interfaces:**
```ts
// useTableData.ts
export interface SortKey { column: string; descending: boolean }
export interface TableDataState {
  filter: string;                // applied WHERE text ("" = none)
  sorts: SortKey[];
  sql: string | null;            // the generated browse SELECT
  requestId: string | null;
  error: string | null;          // browse-select (422) or execution error
  count: { status: "idle" | "running" | "done" | "error"; value?: number; error?: string };
}
export function useTableData(tab: ObjectTab, active: boolean): {
  state: TableDataState; execution: ExecutionState | null; controller: SessionController | null; snapshot: SessionSnapshot | null;
  applyFilter(text: string): void; toggleSort(column: string, additive: boolean): void;
  refresh(): void; loadMore(): void; countRows(): void; retry(): void;
};
// filterValue.ts
export function filterForValue(column: ColumnMeta, value: unknown, dialect: Dialect): string | null;   // null = not filterable
export function addCondition(current: string, condition: string): string;
export const RELATION_KINDS: ReadonlySet<string>;   // table, view, materialized_view, partition
export function relationOf(path: PathSegment[]): { database: string | null; schema: string | null; name: string; kind: string } | null;
```

Rules (spec §4.5):
- Tabs inside: **Data** (default) | **Columns** | **DDL**. Columns/DDL reuse the extracted views over the existing `/metadata/detail` query.
- **Session:** the Data tab opens its own session the first time it is shown (`useSession(profileId, database, {enabled: dataShown})`; `database` = the path's database segment for PG, null for SQLite) and keeps it while the tab is open.
- **Browse flow:** `POST /sql/browse-select {profile_id, path, where, order_by}` → `sql` → `controller.discard(previous)` → `controller.execute(sql, {history: false, pageSize: 500})`. A 422 (bad filter) shows its message under the filter bar; an execution error (e.g. unknown column) shows the DB message there too; the filter text stays for editing.
- **Generated SQL** row (collapsible, monospace, `data-selectable`) with **Open in query tab** → `openQuery(profileId, database, profileName, sql)` (opens a console tab prefilled — ②c's query tab will take this over).
- **Filter bar:** single-line input; Enter applies (empty clears); Escape restores the applied filter; a history dropdown (the last 20 distinct applied filters per profile + relation, newest first) — stored in `localStorage` under `dbaguy.filters.<profileId>.<pathKey>`, read/write inside try/catch (ruling R-02b-1).
- **Sort:** header click on column X cycles X: ascending → descending → removed; without shift the sort list becomes just X; with shift X is added/cycled in place (multi-sort). Header shows ▲/▼ with the sort order number when more than one. Each change re-runs the browse.
- **Load more** through the grid (`fetchMore`); **Refresh** re-runs the current browse; **Count rows** runs `browse-select … count: true` on a **separate count session** created on first use (ruling R-02b-2: one connection cannot hold the paused page and run another statement), shows "N rows" (or the error) next to the button, with a spinner and Cancel while running.
- **Filter by this value** (grid `extraCellMenu`): `filterForValue` → `addCondition` → apply. Identifiers are always double-quoted (`"` doubled — valid in PG and SQLite); NULL → `"c" IS NULL`; numbers (number category and numeric text) bare; bool → `TRUE`/`FALSE` (PG) or `1`/`0` (SQLite); strings `'…'` with `'` doubled; bytes and truncated cells → item disabled.
- The grid gets `tableName` (`{schema, name}` from the path) so INSERT copies target the real table.

```ts
// filterValue.ts (core)
const NUMERIC = /^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
export const quoteIdent = (name: string) => `"${name.replace(/"/g, '""')}"`;
export function filterForValue(column: ColumnMeta, value: unknown, dialect: Dialect): string | null {
  const col = quoteIdent(column.name);
  if (value === null || value === undefined) return `${col} IS NULL`;
  if (typeof value === "object") return null; // bytes / truncated text cells cannot be matched exactly
  if (typeof value === "boolean") return `${col} = ${dialect === "sqlite" ? (value ? "1" : "0") : value ? "TRUE" : "FALSE"}`;
  if (typeof value === "number" || (column.category === "number" && NUMERIC.test(String(value)))) return `${col} = ${value}`;
  return `${col} = '${String(value).replace(/'/g, "''")}'`;
}
export function addCondition(current: string, condition: string): string {
  const trimmed = current.trim();
  return trimmed ? `(${trimmed}) AND ${condition}` : condition;
}
```

- [ ] **Step 1:** split `ObjectDetailView`; add `openQuery` + console `initialSql`.
- [ ] **Step 2:** create `filterValue.ts`, `useTableData.ts`, `FilterBar.tsx`, `TableDataTab.tsx`; route relation tabs in `Workspace.tsx`.
- [ ] **Step 3:** verify typecheck/build. **Manual check:** open `public.big_table` → 500 rows, load more works; filter `id > 99990` → 10 rows; `nonsense` → DB error shown, text kept; empty + Enter clears; sort by `value` desc, shift-click `label` → two-level sort with order numbers; Count rows → 100,000 while the paused page still loads more afterwards; Filter by this value on a `label` cell and on a NULL; Open in query tab runs the same SQL; views/materialized views/partitions open the same way; `"Order Items"` (quoted names) works; a SQLite table works.
- [ ] **Step 4:** commit `feat(table-data): table data tab with filter, multi-sort, count and filter-by-value`.

---

### Task 8: Export dialog

**Files:**
- Create: `src/features/grid/ExportDialog.tsx`
- Modify: `src/features/grid/DataGrid.tsx` (menu item + toolbar button when `exportSource` is set), `src/features/console/QueryConsole.tsx` and `src/features/table-data/TableDataTab.tsx` (pass `exportSource`)

**Interfaces:**
```ts
export interface ExportSource {
  controller: SessionController;
  sql: string;                                   // the statement that produced the result (browse SQL for table data)
  title: string;                                 // default file name stem
  tableName?: { schema: string | null; name: string } | null;
}
export function ExportDialog(props: { source: ExportSource; open: boolean; onOpenChange: (open: boolean) => void }): JSX.Element;
```

Rules (spec §3.3, §4.4):
- Formats: CSV, TSV, JSON, JSONL, Markdown, SQL INSERT (no IN list). Options: header (CSV/TSV/Markdown, default on), NULL as (text formats: empty or `NULL`), table name for INSERT (prefilled from `tableName`, else `table_name`).
- Destination: in Tauri a "Choose…" button (`chooseSavePath(\`${title}.${ext}\`, [{name, extensions:[ext]}])`) showing the chosen path; in browser mode an absolute-path text input. Start is disabled until a path is set.
- Start → `controller.exportResult(sql, format, path, options)`; the dialog switches to progress: rows written (`progressRows`, formatted), elapsed time, **Cancel** (`controller.cancel(id)`). On done: success → toast "Exported N rows to <path>" (the backend's message) and close; error → show the message in the dialog (e.g. `export_requires_select`, `invalid_path`, I/O errors) and allow changing the path and retrying; cancelled → "Export cancelled", the file is not created.
- Exporting ends the grid's paused result on the same session (backend ruling P8): the grid shows the rows it has, `hasMore` false; no stuck "Load more".

- [ ] **Step 1:** create `ExportDialog.tsx`; wire `exportSource` from the console (the executed statement) and the table data tab (the current browse SQL).
- [ ] **Step 2:** verify typecheck/build. **Manual check (browser mode, path input; then `scripts/dev.sh` in Tauri with the native dialog if available):** export `big_table` (paused at 500) as CSV to `/tmp/big.csv` → progress counts to 100,000, file has 100,001 lines, the grid stops offering Load more; export as JSON and INSERT (PG) and check the files; cancel an export of a slow `generate_series(1, 5000000)` → "Export cancelled", no file; export `DELETE …` → `export_requires_select` shown; export to `/nonexistent/x.csv` → path error shown.
- [ ] **Step 3:** commit `feat(grid): export the full result to a file with progress and cancel`.

---

## Rulings recorded while planning

| # | Ruling | Why | Cost if wrong |
|---|---|---|---|
| R-02b-1 | Table filter history lives in `localStorage` (best effort). | A convenience, not durable state; avoids a backend change in this plan. | History may be lost in some webview conditions. |
| R-02b-2 | Count rows uses a separate, lazily created session. | One connection cannot keep the paused page and run `count(*)`; discarding the page would break Load more. | One extra connection per table tab that counts. |
| R-02b-3 | Aggregates use JS doubles with an `≈` marker when a value exceeds exact precision; capped at 200,000 cells. | Exact decimal arithmetic is not worth its complexity for a status bar. | Sums of huge decimals are approximate (marked). |
| R-02b-4 | "Pin column" freezes columns 0..k (freeze panes), not arbitrary columns. | Matches "固定到此欄" and keeps sticky layout simple. | No pinning of a single middle column. |
| R-02b-5 | The ① console stays (on the new controller and grid) until ②c replaces it. | Lets the grid be exercised now; ②c owns the editor/query tab. | Small throwaway console work. |
