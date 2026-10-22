# 06b: Data tools, frontend (dump and Run SQL file) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give 06a's dump and Run SQL file their UI: a Dump dialog and a Run SQL file dialog with every warning shown before a job starts, a job that runs on a session of its own and outlives its dialog, a Jobs list at the bottom of the sidebar that survives a window reload, honest outcomes in words with "Run again from line N" / "Skip it and continue" that never skip a statement, the entry points (tree, table tab, Scripts, ⌘⇧P), the long-job notification, and a ⌘Q / window-close guard while jobs run.

**Architecture:** Jobs live outside React. `jobState.ts` folds a job's WebSocket events and the backend's job list into one state (a `job_outcome` is the end, with or without `finished`); `jobRunner.ts` opens the job's own session, sends its command exactly once, closes the session after the outcome and never reuses it; `jobsStore.ts` (zustand) holds this window's jobs and reconciles them with `GET /data/jobs` after a reload. `jobReport.ts` turns a job into words and safe actions; `entryPoints.ts`, `dumpForm.ts` and `runForm.ts` decide what each entry and engine offers. The dialogs, the Jobs button and the quit guard render those pure modules. The backend gains two small things the UI needs: `JobOutcomeEvent.resume_at` (where a run again starts without skipping anything, B1) and `POST /data/run-file/excerpt` (an error's lines, B2). The shell replaces the predefined Quit with its own item that asks the page (B3).

**Tech Stack:** React 19 + TypeScript 6, zustand 5, TanStack Query 5, radix-ui 1.8 (its Popover, already installed), lucide-react, Vitest 5.0.3 (node, pure modules only); Tauri 2.12.4 (tao 0.37.1, muda 0.20.0) for the Quit item; Python 3.13 sidecar (FastAPI, pydantic v2, pytest) for B1 and B2. No new dependency on any side.

**Spec:** docs/superpowers/specs/2026-10-20-06-data-tools-design.md — §3.6 (frontend, without CopyDataDialog / MappingTable / Copy entries: 06c), R-06-15, D-06-10, D-06-13, §4 (the error table and "結果的說明由前端依 job_outcome 組成"), §12 CR-3 and CR-5, §10 A12, §11 item 2, and the 〔06a〕 notes that touch the frontend. The backend contract is 06a's: docs/superpowers/notes/2026-10-21-06a-data-tools-backend-followups.md (its Carries rows marked 06b are requirements; its appendix and CLAUDE.md's ⑥ block "資料工具" are the contract). The frontend precedents: 05b's plan (docs/superpowers/plans/2026-10-20-05b-grid-editing-frontend.md: pure modules with Vitest, manual lists) and its follow-ups note (rulings K1–K22), the export dialog (`src/features/grid/ExportDialog.tsx`), the document dialog host (`features/documents/openDocument.ts`).

## Pain points this plan must serve

From `docs/research/2026-10-15-feature-priorities.md`. Every task review and the final review check the task against these rows as well as plan completeness, and say what was solved, stepped on or regressed.

| Source row | How 06b serves it, or avoids regressing it |
|---|---|
| §2 Q1 輕、快、低記憶體 | Jobs are plain objects outside React; a dialog or the Jobs button re-renders on its own job's events only (250 ms progress at most, D-06-11). No new dependency; the bundle grows by the new modules only (dry run: 1,447.95 → 1,450.42 kB before the dialogs). Task 9 measures startup and `footprint` against 06a. |
| §2 Q2 穩定、不丟工作 | A job outlives its dialog, its socket and a window reload (`GET /data/jobs`, replayed outcome); a backend restart ends a job with what is known, never "completed"; ⌘Q and closing the window ask while jobs run; deleting a connection whose job runs is refused (B13); a command is never sent twice. |
| §2 Q3 可信任 | Outcomes in words straight from `job_outcome` (R-06-12); "Run again from line N" starts where nothing is skipped (B1); every run-again goes back to the form (V2); a filtered file that replaces its table is said first and in red (06a F2); CR-3's danger button and box on a red connection; "every statement runs" is always said (R-06-6). |
| §2 Q4 聚焦 80% | Two dialogs and a list; no scheduling, no Copy (06c), no per-object dump picking beyond the Tables folder (V14). |
| §2 Q5 多引擎 | One dialog per tool for every engine; what differs comes from the capabilities (`dump`, `run_file`, `transactions`, `query_context`) and the backend's texts. Couchbase gets Run SQL file statement by statement. |
| §2 Q6 macOS 慣例 | System save and open panels, Show in Finder, Return is the default button, Esc closes, ⌘Q asks while jobs run, ⌘W still closes a tab, background notifications (S8's setting). |
| §3 M3 每個指令都能用鍵盤 | ⌘⇧P: Dump…, Run SQL file…, Show jobs. |
| §3 M9 匯出 | A whole database or schema to one file, from the tree, the table tab (with its filter) or ⌘⇧P. |
| §3 M11 安全 | A red connection's Run is a danger button behind a box (CR-3); a read-only connection cannot be a run's target (greyed, said); DROP first and filtered files are said in red before the run. |
| §8 SQL dump / restore (Navicat's Dump SQL File / Execute SQL File) | Both from the tree's menu, with Jobs instead of a blocking modal. |
| User requests | Classic scrollbars: every fixed-height list reserves its scrollbar (`[scrollbar-gutter:stable]`); Zhuyin: every Enter handler checks `isImeKeyEvent` first; monochrome engine icons wherever a connection is shown. |

## Global Constraints

- Branch `feat/06-data-tools` (06a ends at 7d756ed). ⑥ merges to `main` after 06c, never inside this plan.
- No new dependency (frontend, Python, Rust). One new Tauri permission, `core:window:allow-destroy` (the JS `onCloseRequested` destroys the window itself once it allows the close; `core:default` does not include it — verified in tauri 2.12.4's `permissions/window/autogenerated/reference.md`, and `cargo check` rejects an unknown permission).
- No Testing Library or jsdom (05b H1): Vitest tests pure modules only (`vite.config.ts` includes `src/**/*.test.ts`); React wiring is checked by hand.
- Binding values, each defined once, in the file named:
  - `features/data-tools/jobState.ts`: `MAX_JOB_MESSAGES = 200` (the backend's own cap, `domain/data_jobs.py`).
  - `features/data-tools/jobsStore.ts`: `NOT_LISTED_AFTER_MS = 10_000`.
  - `features/data-tools/jobReport.ts`: an ETA after 3 s (`ETA_AFTER_MS`) and 1 % of the file (`ETA_MIN_SHARE`).
  - `features/data-tools/dumpForm.ts`: `MAX_DUMP_TABLES = 500` (`MAX_DUMP_NAMES`).
  - `backend/dbaguy_backend/services/data_service.py`: `EXCERPT_BEFORE = 20`, `EXCERPT_LINES = 60`, `EXCERPT_BYTES = 64 * 1024`.
  - `src-tauri/src/quit.rs`: `ACK_TIMEOUT = 2 s`. `src/app/QuitGuard.tsx`: `LIST_TIMEOUT_MS = 1_000`.
  - `features/data-tools/JobDialog.tsx`: `CANCEL_AGAIN_MS = 1_000` (as the export's: SQL Server's second Cancel). `features/data-tools/DumpDialog.tsx`: `PLAN_PATH_DEBOUNCE_MS = 400`.
- From the spec, verbatim where it binds:
  - R-06-15: 「對話框關掉 job 照跑，側欄底部的 **Jobs** 顯示進行中的數量，點開列出這次執行的 job（跑完的也在，直到清除），每一個都能重開它的對話框（進度、結果、取消）… 重新載入視窗後仍看得到、能取消… 跑超過 `notifications.long_query_seconds` 且 app 不在前景時發通知… job 進行中關閉視窗或 ⌘Q 先問（Keep running／Quit anyway）」.
  - D-06-10: 「前端為 job 開 session（`POST /sessions`，unlock 流程照舊），job 結束就關（`DELETE /sessions/{id}`）」; 06a carry: a job's session is never reused.
  - D-06-13: 「前端只依它們顯示入口」 (`dump`, `run_file`).
  - CR-3: 「目標連線的名稱與顏色、database、模式；紅色（production 色）連線的執行鈕是 danger 鈕，另外要勾一個「I've checked the file and the connection」才能按」, and 「會執行檔案裡的每一句，包括 DROP、TRUNCATE」.
  - CR-5: the guard covers ⑥'s jobs only; the other close guards on quit stay 02c's carry.
  - §3.6: 「Return 是主要按鈕、Esc 關閉（執行中也可以，job 照跑）」.
- Frontend rules (CLAUDE.md): colours only through token classes or `var(--color-…)`; every new React key handler checks `isImeKeyEvent(e)` first; classic scrollbars: a fixed-height scrolling area reserves its scrollbar (`[scrollbar-gutter:stable]`) or never scrolls; API types only through `src/lib/api/types.ts`; `openapi.json` and `schema.d.ts` only from `npm run gen:api`; a zustand selector never returns a new array or object (select `order` and `jobs`, derive with `useMemo`).
- Writes and jobs: a job's command is sent at most once (`JobRunner`); a job's session is closed after its outcome and never reused; every "run again" goes back to the form (V2); REST calls are plain calls (no `useQuery` retries) except the dump plan, a read (`retry: false`). No console line or log carries a value, a filter or SQL text.
- Tests per task: `npm test` for the pure modules a task adds; `npm run typecheck && npm run build` after every task touching `src/`; Task 1 also `cargo check` and `cargo clippy` in `src-tauri`; Task 2 the backend files named there; the full backend suite once, in Task 9 (dev containers up, no scratch object, one runner at a time).
- Manual checks: the coordinator runs each UI task's numbered list in the in-app browser with `scripts/dev.sh --web` and `http://localhost:1420` (`dev/data`, never the app's data). Agents never use a browser. Scratch database objects are `zz_06b_*` only, created and dropped by exact name from a DbaGuy query tab; scratch files are `dev/data/scratch/zz_06b_*` (git-ignored), removed by exact path; Couchbase writes only `zz_check_06b*` keys in `dbaguy.app.orders`, removed by exact key. Every list ends with a check that nothing `zz_06b` is left. Profiles for checks: "dev pg", "dev mysql", "dev mssql", "zz_06b sqlite" (`<repo>/dev/data/scratch/zz_06b.sqlite`), "dev couchbase", "dev pg read-only", and "dev pg red" (dev pg with colour red), all with save password off except where a check says. Never touch `~/Library/Application Support/DbaGuy`. Never kill a process by pattern.
- Disk: about 19 GiB free. Scratch under 1 GB (the 2,000,000-row scratch table's dump is about 250 MB: delete it at the end of each list). Never `cargo clean` or prune Docker. `npm run build:app` only in Task 9, after `df -h /` shows at least 5 GiB free.
- Repo is public: dev credentials only.
- Commit after each task, staging only that task's files by path (never `git add -A`); the message ends with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Code given whole in a task was compiled (`tsc --noEmit`), tested (`vitest run`, `pytest`), built (`vite build`) and, for Rust, `cargo check` / `cargo clippy` clean in a scratch worktree at 7d756ed while planning, all tasks' code together (see the Dry-run record); imports only run forward in task order, so each task compiles on its own. React components are specified, not given whole: their wiring is checked by hand (H1).

## Review Focus

1. **A run again never skips or repeats a statement.** After a statement-by-statement run stops inside the file's own transaction (sqlite3 `.dump`'s `BEGIN TRANSACTION … COMMIT`, a hand-written `BEGIN; …`), the server rolled the whole transaction back: "Run again from line N" must start at that transaction's first statement, not at the failure, and "Skip it and continue" is not offered; after a stream error (not UTF-8, a 256 MiB statement, a psql command, COPY from stdin) no skip either; a job listed after a reload offers no run-again at all (its command is not known here). → Task 2 `test_resume_at_is_where_a_run_again_starts_without_skipping_anything`; Task 3 `jobReport.test.ts` "runs again from the file's rolled-back transaction…", "offers no skip past a line…", "offers nothing to run again for a job listed after a reload"; Task 7 manual checks 6–7.
2. **A job's command is sent once and its session is used once.** A reconnect gets the backend's replay, never a second command; a Cancel while the session opens keeps the command from ever leaving and closes the session; the session is closed after the outcome (live or replayed) and never reused. → Task 5 `jobRunner.test.ts` (all seven tests); Task 8 manual check 2.
3. **No way of losing the window loses the job or invents its outcome.** Closing the dialog, a socket drop, a reload: the job is still listed and its outcome still arrives (a replayed outcome without `finished` ends it); a backend restart ends it with a loss in words ("The backend stopped before the job reported its outcome") whose detail depends on the mode and the last stage, never "completed"; the backend's list outranks a loss. → Task 3 `jobState.test.ts`, `jobReport.test.ts` "a job the backend could not report on"; Task 5 `jobsStore.test.ts` "reconciling…"; Task 8 manual checks 2, 3, 7.
4. **⌘Q and closing the window ask only while jobs run, and can never trap the user.** No job: the app quits at once, as before; jobs: "Keep running" (the default, Return and Esc) / "Quit anyway"; a page that does not answer within 2 s does not keep the app open; ⌘W still closes a tab and ⌘⇧W closes the window through the same question. → Task 1 `quitGuard.test.ts`, `cargo check`; Appendix A item 1 (packaged).
5. **A red connection's run cannot start by accident.** Its Run is a danger button that stays disabled until "I've checked the file and the connection" is checked, the box is asked again after every run-again; Return in the path field reads the file and never runs it; a read-only connection is greyed in the target picker and cannot start. → Task 4 `forms.test.ts` "asks a red connection for the box…", "asks for the box again after a run-again action"; Task 7 manual checks 8–10.

---

## File Structure

```
src-tauri/
  src/quit.rs (new)                ⌘Q asks the page: QuitGate, request(), quit_ack, quit_app (B3)
  src/menu.rs                      our Quit item (⌘Q) instead of the predefined one; on_menu_event dispatches
  src/lib.rs                       mod quit; .manage(QuitGate); the two commands
  capabilities/default.json        core:window:allow-destroy
backend/dbaguy_backend/
  domain/data_jobs.py              JobOutcomeEvent.resume_at (B1); FileExcerpt (B2)
  sessions/jobs/run_file.py        resume_at: undone_from, unclear, _resume_at()
  services/data_service.py         excerpt(): a file's lines around one line
  api/data.py                      POST /data/run-file/excerpt
backend/tests/test_run_file_job.py, backend/tests/test_run_file_inspect.py
src/
  lib/api/{openapi.json, schema.d.ts, types.ts}   generated; types.ts exports DumpPlanRequest, StoppedAt, FileExcerpt
  lib/native.ts                    chooseOpenPath, onQuitRequested, quitApp, onWindowCloseRequested
  lib/format.ts                    formatElapsed, formatSize
  lib/filePaths.ts (new)           BROWSER_PATH_HINT, BROWSER_OPEN_HINT, pathProblem, safeFileStem, fileNameOf
  lib/sqliteBusy.ts (new)          isSqliteBusy, sqliteBusyNote (the grid's commit and Run SQL file)
  components/ui/FilePathField.tsx (new)   the path row of the export, the dump and the run
  components/ui/Popover.tsx (new)  the Jobs list's panel (radix-ui Popover)
  lib/useDebouncedValue.ts (new)   a value as it was some ms after its last change (the dump plan's path)
  app/quitGuard.ts (+ .test.ts) (new)     what quitting asks while jobs run (pure)
  app/QuitGuard.tsx (new)          the question, mounted in App
  App.tsx                          <QuitGuard />
  app/AppShell.tsx                 <DataToolsHost />, useJobEndings(), syncJobs() on mount
  app/Sidebar.tsx                  <JobsButton /> at the bottom
  stores/tabStatus.ts              isTabDirty
  features/data-tools/ (new)
    jobState.ts (+ test)           a job's state: events and the backend's list → JobState
    jobReport.ts (+ test)          a job in words: progress, outcome, error labels, safe actions, notification
    jobRunner.ts (+ test)          a job's session and socket: send once, close after the outcome
    jobsStore.ts (+ test)          this window's jobs; start, retry, cancel, sync with GET /data/jobs, clear
    jobsApi.ts                     the REST calls (sessions, jobs, reveal, inspect, excerpt, plan)
    dataDialogs.ts                 which dialog is open (a form or a job's view); the Jobs popover
    entryPoints.ts (+ test)        tree node / table tab / script / palette → DumpEntry, RunEntry
    fileNames.ts                   a dump's default file name
    dumpForm.ts, runForm.ts (+ forms.test.ts)   what each form offers, greys and sends
    DataToolsHost.tsx              hosts the open dialog
    DumpDialog.tsx                 the dump's form
    RunFileDialog.tsx              the run's form
    JobDialog.tsx                  a job's view: progress, outcome, errors and their lines
    JobViews.tsx                   JobProgressView, JobReportView (used by JobDialog)
    TargetPicker.tsx               a run's connection and database
    FileLines.tsx                  numbered read-only lines (the preview, an error's excerpt)
    JobsButton.tsx                 the sidebar button and its popover
    useJobEndings.ts               the notification and the toast when a job ends
  features/edits/commitFlow.ts     SQLITE_BUSY_NOTE from lib/sqliteBusy
  features/table-data/filterValue.ts       FILTER_RULE
  features/table-data/FilterBar.tsx        the one-condition rule in its placeholder and tooltip
  features/table-data/tableTabActions.ts (new)   the table tab's palette action registry (Dump table…)
  features/table-data/TableDataTab.tsx     ⋯ menu "Dump table…" (with the filter), its palette action
  features/grid/ExportDialog.tsx   FilePathField and the shared path helpers (DRY)
  features/notifications/useLongQueryNotifications.ts   formatElapsed
  features/explorer/Explorer.tsx   Dump… / Run SQL file… menus; a connection with a running job is not deleted
  features/scripts/ScriptsPanel.tsx        Run on connection…
  features/query/ConnectionPicker.tsx      exports profileItems (with a disabled reason) and DatabasePicker (nodeKind)
  features/palette/{commands.ts, CommandPalette.tsx, commands.test.ts, dataCommands.test.ts}   Dump…, Run SQL file…, Show jobs
CLAUDE.md, docs/superpowers/specs/2026-10-20-06-data-tools-design.md   (Task 9)
```

Task order follows dependencies. The quit guard stands alone (1). The backend additions the UI needs come next (2), then the pure core: a job's state and words (3), the entries and forms (4), the jobs outside React (5). Then the UI: the dump dialog with the shared job view and the dump entries (6), the run dialog with the run entries (7), the Jobs list, reload, notifications and the delete guard (8). Measurements, the packaged app and the docs close (9).

---

### Task 1: The quit and window-close guard (A12, CR-5) and the native helpers

**Suggested implementer:** Opus (the Rust shell and a guard that must never trap the user).

**Files:**
- Create: `src-tauri/src/quit.rs`, `src/app/quitGuard.ts`, `src/app/quitGuard.test.ts`, `src/app/QuitGuard.tsx`, `src/features/data-tools/jobsApi.ts` (only `listJobs` in this task)
- Modify: `src-tauri/src/menu.rs`, `src-tauri/src/lib.rs`, `src-tauri/capabilities/default.json`, `src/lib/native.ts`, `src/App.tsx`

**Interfaces:**
- Consumes: `GET /data/jobs` → `JobInfo[]` (06a; `outcome` null while it runs).
- Produces:
  - `chooseOpenPath(filters?: FileFilter[]): Promise<string | null>` (app only; no filter: any file).
  - `onQuitRequested(handler: () => void): Promise<() => void>`, `quitApp(): Promise<void>`, `onWindowCloseRequested(allow: () => Promise<boolean>): Promise<() => void>` (app only; no-ops in browser mode).
  - `listJobs(timeoutMs?: number): Promise<JobInfo[]>` in `features/data-tools/jobsApi.ts`.
  - `quitQuestion(running: readonly RunningJob[], how: QuitHow): QuitQuestion | null`; `RunningJob = { job: "dump" | "run_file"; title: string }`; `QuitHow = "quit" | "close"`.
  - `<QuitGuard />`, mounted once in `App` (outside the "Restoring workspace…" gate).

What A12 found (verified against the crate sources in `~/.cargo/registry`, spec issue B3):
- The predefined Quit (`PredefinedMenuItem::quit`) sends `terminate:` to NSApp (muda 0.20.0 `platform_impl/macos/mod.rs:944`). tao 0.37.1's app delegate implements `applicationWillTerminate` only (no `applicationShouldTerminate`, `app_delegate.rs:62`), which goes straight to `LoopDestroyed` → `RunEvent::Exit`. `RunEvent::ExitRequested` (tauri-runtime-wry 2.12.2 `lib.rs:4262`, `4302`) fires only when the last window is destroyed or on `AppHandle::exit` / `request_exit`. So CR-5's `ExitRequested` + `prevent_exit()` never sees ⌘Q: the Quit item must be our own.
- Closing a window (the red button, `window.close()` from our Close Window item) emits `CloseRequested`; when the page listens to `tauri://close-requested`, Rust prevents the close by itself (tauri 2.12.4 `manager/window.rs:177`) and the JS `onCloseRequested` wrapper calls `window.destroy()` unless the handler called `preventDefault()` (`@tauri-apps/api/window.js:2028`). `destroy` needs `core:window:allow-destroy`. Closing the last window then fires `ExitRequested` and the app exits as today.
- The Dock's Quit, logging out and shutting down also send `terminate:`: they still bypass the guard (known limitation, Appendix A).

- [ ] **Step 1: Write the failing test** — `src/app/quitGuard.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { quitQuestion } from "./quitGuard";

/* What ⌘Q and closing the window ask while jobs run (spec ⑥ CR-5). */

describe("the quit question", () => {
  it("asks nothing when no job runs: the app quits as before", () => {
    expect(quitQuestion([], "quit")).toBeNull();
    expect(quitQuestion([], "close")).toBeNull();
  });

  it("names the running jobs and what quitting does to each kind", () => {
    const asked = quitQuestion([{ job: "dump", title: "Dump public of Local PG to a.sql" }], "quit")!;
    expect([asked.title, asked.keep, asked.confirm]).toEqual(["Quit DbaGuy?", "Keep running", "Quit anyway"]);
    expect(asked.message).toBe(
      "A job is still running: Dump public of Local PG to a.sql. Quitting stops it: a dump writes no file, a run in one " +
        "transaction applies nothing, and a run statement by statement keeps the statements before the one running " +
        "(History shows a stopped run as Interrupted).",
    );
  });

  it("says that closing the window quits, and lists at most three", () => {
    const jobs = ["a", "b", "c", "d", "e"].map((title) => ({ job: "run_file" as const, title }));
    const asked = quitQuestion(jobs, "close")!;
    expect([asked.title, asked.confirm]).toEqual(["Close the window?", "Close anyway"]);
    expect(asked.message.startsWith("5 jobs are still running: a; b; c, and 2 more. Closing the window quits DbaGuy, which stops them")).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npx vitest run src/app/quitGuard.test.ts`
Expected: FAIL (cannot resolve `./quitGuard`).

- [ ] **Step 3: Implement `src/app/quitGuard.ts`** (the copy is fixed here):

```ts
/*
 * What quitting asks while data-tools jobs run (spec ⑥ R-06-15, CR-5, A12): ⌘Q, the menu's Quit and closing the window
 * (which quits the app) stop every job, because the backend goes with the app. Only jobs ask (CR-5): the other close
 * guards stay a 02c carry. Pure.
 */

export type QuitHow = "quit" | "close";

export interface RunningJob {
  job: "dump" | "run_file";
  title: string;
}

export interface QuitQuestion {
  title: string;
  message: string;
  keep: string;
  confirm: string;
}

const LISTED = 3;

/** The question to ask, or null when no job runs (then the app quits at once, as before). */
export function quitQuestion(running: readonly RunningJob[], how: QuitHow): QuitQuestion | null {
  if (!running.length) return null;
  const n = running.length;
  const names = running.slice(0, LISTED).map((job) => job.title);
  const more = n > LISTED ? `, and ${n - LISTED} more` : "";
  const subject = n === 1 ? "A job is" : `${n} jobs are`;
  const act = how === "quit" ? "Quitting" : "Closing the window quits DbaGuy, which";
  const them = n === 1 ? "it" : "them";
  return {
    title: how === "quit" ? "Quit DbaGuy?" : "Close the window?",
    message:
      `${subject} still running: ${names.join("; ")}${more}. ${act} stops ${them}: a dump writes no file, a run in ` +
      "one transaction applies nothing, and a run statement by statement keeps the statements before the one running " +
      "(History shows a stopped run as Interrupted).",
    keep: "Keep running",
    confirm: how === "quit" ? "Quit anyway" : "Close anyway",
  };
}
```

- [ ] **Step 4: Run it to make sure it passes**

Run: `npx vitest run src/app/quitGuard.test.ts`
Expected: 3 passed.

- [ ] **Step 5: The shell's Quit.** Create `src-tauri/src/quit.rs`:

```rust
//! ⌘Q asks the page first (spec ⑥ CR-5, A12). The predefined Quit item sends `terminate:` to NSApp, which tao answers
//! with `applicationWillTerminate` only: no `RunEvent::ExitRequested`, so nothing can keep the app open. The menu's
//! Quit is ours instead: it emits `QUIT_REQUESTED` to the page, which acks at once (`quit_ack`), asks while data-tools
//! jobs run, and quits through `quit_app`. A page that does not ack within `ACK_TIMEOUT` (it is loading, or hung) does
//! not keep the app open: it quits as the predefined item would. Rust only relays: the decision is the page's.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, Runtime};

/// The event the page listens to; its payload is the request's number, which the page acks.
pub const QUIT_REQUESTED: &str = "app://quit-requested";
const ACK_TIMEOUT: Duration = Duration::from_secs(2);

/// The last Quit asked and the last one the page acked (numbers grow; an ack covers every request up to it).
#[derive(Default)]
pub struct QuitGate {
    asked: AtomicU64,
    acked: AtomicU64,
}

/// The menu's Quit (⌘Q): ask the page; quit anyway when it does not answer in time.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn request<R: Runtime>(app: &AppHandle<R>) {
    let id = app.state::<QuitGate>().asked.fetch_add(1, Ordering::SeqCst) + 1;
    if let Err(error) = app.emit_to("main", QUIT_REQUESTED, id) {
        log::warn!("Quit: could not ask the page ({error}); quitting");
        app.exit(0);
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(ACK_TIMEOUT);
        if app.state::<QuitGate>().acked.load(Ordering::SeqCst) < id {
            log::warn!("Quit: the page did not answer within {ACK_TIMEOUT:?}; quitting");
            app.exit(0);
        }
    });
}

/// The page got Quit request `id` and decides now (it may ask the user for as long as it takes).
#[tauri::command]
pub fn quit_ack(gate: tauri::State<'_, QuitGate>, id: u64) {
    gate.acked.fetch_max(id, Ordering::SeqCst);
}

/// The page's decision to quit: the app exits as the predefined Quit would (the sidecar is stopped on `RunEvent::Exit`).
#[tauri::command]
pub fn quit_app(app: AppHandle) {
    log::info!("Quit: confirmed by the page");
    app.exit(0);
}
```

and apply to the menu, the builder and the capability:

```diff
diff --git a/src-tauri/src/menu.rs b/src-tauri/src/menu.rs
index dcfa1d6..e5216ce 100644
--- a/src-tauri/src/menu.rs
+++ b/src-tauri/src/menu.rs
@@ -1,7 +1,8 @@
-//! The macOS app menu (ruling R-02c-17): Tauri's default menu, except Close Window. The default binds Close Window to
-//! ⌘W, and the native menu takes the shortcut before the page sees it; ⌘W belongs to the page (it closes a tab), so
-//! Close Window is ⌘⇧W here. The Edit items stay the predefined ones: WKWebView needs them for the clipboard and text
-//! editing.
+//! The macOS app menu (ruling R-02c-17): Tauri's default menu, except Close Window and Quit. The default binds Close
+//! Window to ⌘W, and the native menu takes the shortcut before the page sees it; ⌘W belongs to the page (it closes a
+//! tab), so Close Window is ⌘⇧W here. Quit (⌘Q) is our own item (spec ⑥ CR-5): the predefined one terminates the app
+//! without any event that could keep it open, and the page must first ask while data-tools jobs run (`quit.rs`). The
+//! Edit items stay the predefined ones: WKWebView needs them for the clipboard and text editing.
 
 use tauri::menu::{
     AboutMetadata, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu, HELP_SUBMENU_ID, WINDOW_SUBMENU_ID,
@@ -9,12 +10,19 @@ use tauri::menu::{
 use tauri::{AppHandle, Manager, Runtime};
 
 const CLOSE_WINDOW_ID: &str = "close-window";
+const QUIT_ID: &str = "quit";
 
 /// Close Window, in the File and the Window menu.
 fn close_window<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<MenuItem<R>> {
     MenuItem::with_id(app, CLOSE_WINDOW_ID, "Close Window", true, Some("CmdOrCtrl+Shift+W"))
 }
 
+/// Quit, in the app menu: ⌘Q, as the predefined item, but the page is asked first (`quit::request`).
+fn quit<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<MenuItem<R>> {
+    let label = format!("Quit {}", app.package_info().name);
+    MenuItem::with_id(app, QUIT_ID, label, true, Some("CmdOrCtrl+Q"))
+}
+
 /// The menu, item for item as `tauri::menu::Menu::default` builds it on macOS (tauri 2.12).
 pub fn app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
     let package = app.package_info();
@@ -41,7 +49,7 @@ pub fn app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
                     &PredefinedMenuItem::hide(app, None)?,
                     &PredefinedMenuItem::hide_others(app, None)?,
                     &PredefinedMenuItem::separator(app)?,
-                    &PredefinedMenuItem::quit(app, None)?,
+                    &quit(app)?,
                 ],
             )?,
             &Submenu::with_items(app, "File", true, &[&close_window(app)?])?,
@@ -77,11 +85,17 @@ pub fn app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
     )
 }
 
-/// Close Window closes the focused window, as the predefined item does.
+/// Close Window closes the focused window, as the predefined item does (the page is asked first: `onCloseRequested`);
+/// Quit asks the page.
 pub fn on_menu_event<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
-    if event.id() != CLOSE_WINDOW_ID {
-        return;
+    match event.id().as_ref() {
+        CLOSE_WINDOW_ID => close_focused_window(app),
+        QUIT_ID => crate::quit::request(app),
+        _ => {}
     }
+}
+
+fn close_focused_window<R: Runtime>(app: &AppHandle<R>) {
     let focused = app.webview_windows().into_values().find(|window| window.is_focused().unwrap_or(false));
     match focused {
         Some(window) => {
```

```diff
diff --git a/src-tauri/src/lib.rs b/src-tauri/src/lib.rs
index 5c7ec5a..43f056c 100644
--- a/src-tauri/src/lib.rs
+++ b/src-tauri/src/lib.rs
@@ -1,6 +1,7 @@
 mod backend;
 #[cfg(target_os = "macos")]
 mod menu;
+mod quit;
 
 use backend::{BackendInfo, BackendManager};
 use std::path::PathBuf;
@@ -51,11 +52,12 @@ pub fn run() {
         .plugin(tauri_plugin_notification::init())
         .plugin(tauri_plugin_clipboard_manager::init())
         .manage(BackendManager::default())
+        .manage(quit::QuitGate::default())
         .setup(|app| {
             let _ = app.state::<BackendManager>().start(app.handle());
             Ok(())
         })
-        .invoke_handler(tauri::generate_handler![backend_info, restart_backend])
+        .invoke_handler(tauri::generate_handler![backend_info, restart_backend, quit::quit_ack, quit::quit_app])
         .build(tauri::generate_context!())
         .expect("error while building tauri application")
         .run(|app, event| {
```

```diff
diff --git a/src-tauri/capabilities/default.json b/src-tauri/capabilities/default.json
index c7015dd..ffb1e9b 100644
--- a/src-tauri/capabilities/default.json
+++ b/src-tauri/capabilities/default.json
@@ -6,6 +6,7 @@
   "permissions": [
     "core:default",
     "core:window:allow-start-dragging",
+    "core:window:allow-destroy",
     "log:default",
     "dialog:allow-save",
     "dialog:allow-open",
```

- [ ] **Step 6: Check the shell**

Run: `cd src-tauri && cargo check && cargo clippy`
Expected: `Finished` with no warning (`tauri-build` also validates `core:window:allow-destroy`: an unknown permission fails the build script).

- [ ] **Step 7: The native helpers.** In `src/lib/native.ts` (static imports of `@tauri-apps/api/core` and `event`: both are already in the main chunk, a dynamic import of them makes Vite warn `INEFFECTIVE_DYNAMIC_IMPORT`):

```diff
diff --git a/src/lib/native.ts b/src/lib/native.ts
index bad1d6a..f5b008d 100644
--- a/src/lib/native.ts
+++ b/src/lib/native.ts
@@ -1,3 +1,5 @@
+import { invoke } from "@tauri-apps/api/core";
+import { listen } from "@tauri-apps/api/event";
 import { isTauriRuntime } from "./backend";
 
 export interface FileFilter {
@@ -15,6 +17,47 @@ export async function chooseSavePath(defaultName: string, filters: FileFilter[])
   return (await save({ defaultPath: defaultName, filters })) ?? null;
 }
 
+/** A file picked in the native open panel (app only: null in browser mode or when cancelled); no filter: any file. */
+export async function chooseOpenPath(filters: FileFilter[] = []): Promise<string | null> {
+  if (!isTauriRuntime()) return null;
+  const { open } = await import("@tauri-apps/plugin-dialog");
+  return (await open({ directory: false, multiple: false, filters: filters.length ? filters : undefined })) ?? null;
+}
+
+/** The event the app menu's Quit (⌘Q) sends (`QUIT_REQUESTED` in src-tauri/src/quit.rs). */
+const QUIT_REQUESTED = "app://quit-requested";
+
+/**
+ * Calls `handler` when the user chooses Quit (⌘Q, the app menu): the app quits only through `quitApp`. Each request is
+ * acknowledged at once, or the shell quits by itself after 2 s (a page that does not answer never keeps the app open).
+ * App only: a no-op in browser mode. Resolves to the function that stops listening.
+ */
+export async function onQuitRequested(handler: () => void): Promise<() => void> {
+  if (!isTauriRuntime()) return () => undefined;
+  return listen<number>(QUIT_REQUESTED, (event) => {
+    void invoke("quit_ack", { id: event.payload });
+    handler();
+  });
+}
+
+/** Quits the app (the shell stops the backend). App only. */
+export async function quitApp(): Promise<void> {
+  if (!isTauriRuntime()) return;
+  await invoke("quit_app");
+}
+
+/**
+ * Asks `allow` before the window closes (the red button, ⌘⇧W, the menu's Close Window); false keeps it open. App only.
+ * `allow` must settle: the window waits for it (it closes once it resolves true).
+ */
+export async function onWindowCloseRequested(allow: () => Promise<boolean>): Promise<() => void> {
+  if (!isTauriRuntime()) return () => undefined;
+  const { getCurrentWindow } = await import("@tauri-apps/api/window");
+  return getCurrentWindow().onCloseRequested(async (event) => {
+    if (!(await allow())) event.preventDefault();
+  });
+}
+
 /** A folder picked in the native dialog (app only: null in browser mode or when cancelled). */
 export async function chooseFolder(defaultPath?: string): Promise<string | null> {
   if (!isTauriRuntime()) return null;
```

Create `src/features/data-tools/jobsApi.ts` with `listJobs` only (Task 5 gives the whole file):

```ts
import { api, unwrap } from "@/lib/api/client";
import type { JobInfo } from "@/lib/api/types";

/** This backend run's jobs, newest first; `timeoutMs` bounds the wait (the quit guard must never hang on it). */
export async function listJobs(timeoutMs?: number): Promise<JobInfo[]> {
  const signal = timeoutMs === undefined ? undefined : AbortSignal.timeout(timeoutMs);
  return unwrap(await (await api()).GET("/data/jobs", { signal }));
}
```

- [ ] **Step 8: The question.** Create `src/app/QuitGuard.tsx` (it asks the backend's list, not this window's store: right after a reload too, V8; a backend that cannot answer within 1 s is gone, and so are its jobs):

```tsx
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { onQuitRequested, onWindowCloseRequested, quitApp } from "@/lib/native";
import { listJobs } from "@/features/data-tools/jobsApi";
import { quitQuestion, type QuitHow, type QuitQuestion, type RunningJob } from "./quitGuard";

const LIST_TIMEOUT_MS = 1_000;

/** The jobs running now, as the backend lists them; none when it cannot say in time (it is gone: so are its jobs). */
async function runningNow(): Promise<RunningJob[]> {
  try {
    return (await listJobs(LIST_TIMEOUT_MS)).filter((info) => !info.outcome).map((info) => ({ job: info.job, title: info.title }));
  } catch (error) {
    console.warn("Quit: the running jobs could not be listed", error);
    return [];
  }
}

/** ⌘Q and closing the window ask while data-tools jobs run (spec ⑥ CR-5, A12); app only. */
export function QuitGuard() {
  const [asking, setAsking] = useState<{ question: QuitQuestion; answer: (quit: boolean) => void } | null>(null);
  const pending = useRef<Promise<boolean> | null>(null);
  useEffect(() => {
    const decide = (how: QuitHow): Promise<boolean> => {
      pending.current ??= (async () => {
        const question = quitQuestion(await runningNow(), how);
        if (!question) return true;
        return new Promise<boolean>((resolve) => setAsking({ question, answer: resolve }));
      })().finally(() => {
        pending.current = null;
        setAsking(null);
      });
      return pending.current;
    };
    const stopQuit = onQuitRequested(() => void decide("quit").then((quit) => quit && quitApp()));
    const stopClose = onWindowCloseRequested(() => decide("close"));
    return () => {
      void stopQuit.then((stop) => stop());
      void stopClose.then((stop) => stop());
    };
  }, []);
  if (!asking) return null;
  const { question, answer } = asking;
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && answer(false)}
      title={question.title}
      width={460}
      footer={
        <>
          <Button variant="danger" onClick={() => answer(true)}>
            {question.confirm}
          </Button>
          <Button variant="primary" onClick={() => answer(false)} autoFocus>
            {question.keep}
          </Button>
        </>
      }
    >
      <p className="text-[13px]" data-selectable>
        {question.message}
      </p>
    </Dialog>
  );
}
```

In `src/App.tsx`, mount one `<QuitGuard />` beside the restoring / restored switch and outside the app's `ErrorBoundary` (a fragment: `<>{restored ? <ErrorBoundary …>…</ErrorBoundary> : <Restoring />}<QuitGuard /></>`), so ⌘Q asks while the workspace restores and after the app's own error screen too, and its listeners register once.

- [ ] **Step 9: Check the types and the build**

Run: `npm run typecheck && npm run build && npm test`
Expected: no errors; no `INEFFECTIVE_DYNAMIC_IMPORT` line from `src/lib/native.ts`; all tests pass.

- [ ] **Step 10: Manual check** (the coordinator, web mode; the app-only behaviour is Appendix A item 1 and Task 9's packaged check):
  1. Reload the page and close the browser tab with no job: nothing asks (the guard is app only); the console has no error from `QuitGuard`.
  2. ⌘W still closes the active workspace tab (with its own guards), as before.

- [ ] **Step 11: Commit**

```bash
git add src-tauri/src/quit.rs src-tauri/src/menu.rs src-tauri/src/lib.rs src-tauri/capabilities/default.json \
  src/lib/native.ts src/features/data-tools/jobsApi.ts src/app/quitGuard.ts src/app/quitGuard.test.ts \
  src/app/QuitGuard.tsx src/App.tsx
git commit -m "feat(app): ⌘Q and closing the window ask while data-tools jobs run (our own Quit item; A12)"
```

---

### Task 2: Backend — where a run starts again (B1) and an error's lines (B2)

**Suggested implementer:** Opus (run-file outcome rules; 06a's honesty rulings R1–R10, N1–N6 must not move).

**Files:**
- Modify: `backend/dbaguy_backend/domain/data_jobs.py`, `backend/dbaguy_backend/sessions/jobs/run_file.py`, `backend/dbaguy_backend/services/data_service.py`, `backend/dbaguy_backend/api/data.py`
- Test: `backend/tests/test_run_file_job.py`, `backend/tests/test_run_file_inspect.py`
- Generated: `src/lib/api/openapi.json`, `src/lib/api/schema.d.ts`; modify `src/lib/api/types.ts`

**Interfaces:**
- Produces:
  - `JobOutcomeEvent.resume_at: StoppedAt | None` — Run SQL file, statement by statement: where a run again starts so that nothing is skipped (every statement before it took effect, or failed and was skipped and is in `errors`). The earliest of: the first statement of a file transaction the run rolled back and counted out, and where the run stopped (`stopped` and `unknown` outcomes). None for all or nothing, for `not_started`, for a run read to its end with nothing undone, and whenever what an earlier statement did is not known (a "may have committed" warning, a batch or procedure that may have run part of itself, a trigger's COMMIT, a procedure inside the file's transaction (N1), a table that could not roll back (R3), a transaction restarted inside the file (R10), a transaction begun inside a statement (N2)).
  - `POST /data/run-file/excerpt` `{path, line ≥ 1}` → `FileExcerpt{path, first_line, text, found, truncated}`: from `EXCERPT_BEFORE` (20) lines before `line`, at most `EXCERPT_LINES` (60) lines and `EXCERPT_BYTES` (64 KiB); lines counted at LF (as a run counts them); a leading UTF-8 BOM dropped; text that is not UTF-8 replaced; 404 `file_not_found` for no file; no connection, no profile.
  - `src/lib/api/types.ts`: `DumpPlanRequest`, `StoppedAt`, `FileExcerpt`.

- [ ] **Step 1: Write the failing tests.** Append to `backend/tests/test_run_file_job.py` and `backend/tests/test_run_file_inspect.py`:

```diff
diff --git a/backend/tests/test_run_file_job.py b/backend/tests/test_run_file_job.py
index a208709..fd1c68c 100644
--- a/backend/tests/test_run_file_job.py
+++ b/backend/tests/test_run_file_job.py
@@ -787,3 +787,35 @@ def test_a_deadlock_victim_whose_table_cannot_roll_back_is_unknown(env, sql_file
         "Some tables cannot roll back (non-transactional): their changes stayed"])
     plain = env(dialect=Dialect.MYSQL)
     assert outcome(plain.run(sql_file("rows 1;\ndeadlock;"))).outcome == "rolled_back"
+
+
+@pytest.mark.parametrize(("text", "fields", "dialect", "expected"), [
+    ("rows 1;\nfail boom;\nrows 3;", {}, Dialect.SQLITE, ("stopped", (2, 2))),
+    ("rows 1;\ntx;\nrows 3;\nfail boom;\nrows 5;", {}, Dialect.SQLITE, ("stopped", (2, 2))),  # its transaction undone
+    ("rows 1;\ntx;\nrows 3;", {}, Dialect.SQLITE, ("completed", (2, 2))),  # left open: rolled back at the end
+    ("tx;\nrows 2;\nfailend;\nrows 4;", {"on_error": "continue"}, Dialect.SQLITE, ("completed", (1, 1))),
+    ("tx;\nrows 2;\nfail boom;\nrows 4;\nCOMMIT;", {"on_error": "continue"}, Dialect.SQLITE, ("stopped", (1, 1))),
+    ("rows 1;\nlost;\nrows 3;", {"on_error": "continue"}, Dialect.SQLITE, ("unknown", (2, 2))),
+    ("rows 1;\ntx;\nrows 3;\nlost;", {}, Dialect.SQLITE, ("unknown", (2, 2))),  # the server undid its transaction
+    ("rows 1;\nfail boom;\nrows 3;\nfail again;", {"on_error": "continue"}, Dialect.SQLITE, ("completed", None)),
+    ("tx;\nrows 2;\nCALL p();\nrows 4;\nfail boom;", {}, Dialect.MYSQL, ("unknown", None)),  # N1: not known
+    ("rows 1\nrows 2\nGO\n", {}, Dialect.TSQL, ("stopped", None)),  # a batch that may have applied part of it
+    ("rows 1;\nfail boom;", {"mode": "all_or_nothing"}, Dialect.SQLITE, ("rolled_back", None)),
+    ("rows 1;", {"from_statement": 5}, Dialect.SQLITE, ("completed", None)),
+])
+def test_resume_at_is_where_a_run_again_starts_without_skipping_anything(env, sql_file, text, fields, dialect,
+                                                                         expected):  # 06b B1
+    acts = {"rows 1\nrows 2": "fail"} if dialect is Dialect.TSQL else None
+    e = env(dialect=dialect, acts=acts)
+    done = outcome(e.run(sql_file(text), **{"mode": "statements", **fields}))
+    at = (done.resume_at.line, done.resume_at.statement) if done.resume_at is not None else None
+    assert (done.outcome, at) == expected
+
+
+def test_a_cancelled_run_resumes_at_the_statement_it_did_not_finish(env, sql_file):  # 06b B1
+    e = env()
+    e.submit(sql_file("rows 1;\nblock;\nrows 3;"), mode="statements")
+    assert e.conn.blocked.wait(5)
+    e.worker.cancel("r")
+    done = outcome(request_events(e.recorder, "r"))
+    assert (done.outcome, done.cancelled, done.resume_at.statement, done.resume_at.line) == ("stopped", True, 2, 2)
```

```diff
diff --git a/backend/tests/test_run_file_inspect.py b/backend/tests/test_run_file_inspect.py
index 1444506..c052d6b 100644
--- a/backend/tests/test_run_file_inspect.py
+++ b/backend/tests/test_run_file_inspect.py
@@ -159,3 +159,46 @@ def test_an_engine_without_the_tool_is_refused(tmp_path):  # pre-flight L4: §4'
     path.write_text("SELECT 1;\n")
     with pytest.raises(NotSupported, match="^SQLite connections cannot run SQL files$"):
         container.data.inspect(profile(container, tmp_path), str(path))
+
+
+def test_an_excerpt_shows_the_lines_around_one_line(container, tmp_path):  # 06b B2
+    path = tmp_path / "e.sql"
+    path.write_text("".join(f"SELECT {n};\n" for n in range(1, 101)))
+    found = container.data.excerpt(str(path), 50)
+    assert (found.first_line, found.found, found.truncated) == (30, True, True)
+    assert found.text.splitlines()[0] == "SELECT 30;" and found.text.splitlines()[20] == "SELECT 50;"
+    assert len(found.text.splitlines()) == 60
+    start = container.data.excerpt(str(path), 3)
+    assert (start.first_line, start.text.splitlines()[0]) == (1, "SELECT 1;")
+    end = container.data.excerpt(str(path), 100)
+    assert (end.first_line, end.found, end.truncated, end.text.splitlines()[-1]) == (80, True, False, "SELECT 100;")
+    beyond = container.data.excerpt(str(path), 101)
+    assert (beyond.found, beyond.text.splitlines()[-1]) == (False, "SELECT 100;")
+    far = container.data.excerpt(str(path), 500)
+    assert (far.found, far.text) == (False, "")
+
+
+def test_an_excerpt_takes_any_bytes_and_stops_at_its_size(container, tmp_path):  # 06b B2
+    path = tmp_path / "b.sql"
+    path.write_bytes(b"\xef\xbb\xbfSELECT 1;\nSELECT '\xff';\n" + b"x" * 100_000 + b"\nSELECT 3;\n")
+    found = container.data.excerpt(str(path), 2)
+    assert found.text.startswith("SELECT 1;\nSELECT '�';\nxxx") and found.truncated
+    assert len(found.text.encode()) <= 64 * 1024 + 2  # the replacement character is longer than the byte it replaces
+    with pytest.raises(FileNotFound):
+        container.data.excerpt(str(tmp_path / "missing.sql"), 1)
+    with pytest.raises(FileNotFound):
+        container.data.excerpt("relative.sql", 1)
+
+
+def test_the_excerpt_route(container, tmp_path):  # 06b B2
+    path = tmp_path / "r.sql"
+    path.write_text("SELECT 1;\n")
+    auth = {"Authorization": "Bearer t"}
+    with TestClient(create_app(container.settings, container), base_url="http://127.0.0.1") as client:
+        answer = client.post("/data/run-file/excerpt", json={"path": str(path), "line": 1}, headers=auth)
+        assert (answer.status_code, answer.json()["text"], answer.json()["found"]) == (200, "SELECT 1;", True)
+        missing = client.post("/data/run-file/excerpt", json={"path": str(tmp_path / "no.sql"), "line": 1},
+                              headers=auth)
+        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "file_not_found")
+        zero = client.post("/data/run-file/excerpt", json={"path": str(path), "line": 0}, headers=auth)
+        assert zero.status_code == 422
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && .venv/bin/pytest -q tests/test_run_file_job.py tests/test_run_file_inspect.py -k "resume or excerpt"`
Expected: FAIL (`resume_at` is not a field; `DataService` has no `excerpt`).

- [ ] **Step 3: `resume_at`.** In `domain/data_jobs.py` and `sessions/jobs/run_file.py` (`_may_have_ended` becomes an instance method so every "may" warning marks the run unclear; `_undone` records a counted-out transaction; `_resume_at` is called once, by `_report`):

```diff
diff --git a/backend/dbaguy_backend/domain/data_jobs.py b/backend/dbaguy_backend/domain/data_jobs.py
index f13d15d..7dc667e 100644
--- a/backend/dbaguy_backend/domain/data_jobs.py
+++ b/backend/dbaguy_backend/domain/data_jobs.py
@@ -102,6 +102,11 @@ class JobOutcomeEvent(BaseModel):
     errors: list[JobError] = []  # the first MAX_JOB_ERRORS
     error_count: int = 0
     stopped_at: StoppedAt | None = None
+    # Run SQL file, statement by statement: where a run again starts so that nothing is skipped — every statement before
+    # it took effect (or failed and was skipped, in `errors`). The earliest statement of a file transaction that was
+    # rolled back, else where the run stopped (06b B1). None: all or nothing, read to the end with nothing undone, or
+    # what the earlier statements did is not known (the warnings say which)
+    resume_at: StoppedAt | None = None
     # Why it stopped or failed: the `error` event sent right before it, with where the run stopped when it did, so a
     # window that attaches later or lists the jobs (GET /data/jobs) knows it too (06a fix wave F3). None: no error
     error: JobError | None = None
@@ -150,6 +155,17 @@ class FileInspection(BaseModel):
     warnings: list[str] = []
 
 
+class FileExcerpt(BaseModel):
+    """Lines of a file around one line (POST /data/run-file/excerpt): where an error of Run SQL file is, for its dialog
+    (06b B2). Lines are counted at LF, as a run counts them; text that is not UTF-8 is shown replaced."""
+
+    path: str
+    first_line: int  # the line `text` starts at
+    text: str  # at most EXCERPT_LINES lines and EXCERPT_BYTES, from first_line on
+    found: bool  # the line asked for is in the file
+    truncated: bool  # the file goes on after `text`
+
+
 MAX_DUMP_NAMES = 500  # relations one `tables` scope may name
```

```diff
diff --git a/backend/dbaguy_backend/sessions/jobs/run_file.py b/backend/dbaguy_backend/sessions/jobs/run_file.py
index cb0a340..ed7f1a7 100644
--- a/backend/dbaguy_backend/sessions/jobs/run_file.py
+++ b/backend/dbaguy_backend/sessions/jobs/run_file.py
@@ -209,6 +209,10 @@ class _Run:
         # began it ran other statements before its BEGIN (N2: those may be applied)
         self.tx_procedure: FileStatement | None = None
         self.tx_began_inside = False
+        # Where a run again starts (06b B1): the earliest file transaction rolled back and counted out, and whether what
+        # an earlier statement did is not known (a "may" warning), which leaves no such place
+        self.undone_from: FileStatement | None = None
+        self.unclear = False
 
     def go(self, stream: StatementStream) -> JobReport:
         try:
@@ -570,12 +574,13 @@ class _Run:
             return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) ended when line "
                     f"{failed.line:,} failed: the server rolled it back"]
         self.done, self.rows = self.done_before_tx, self.rows_before_tx
+        self._undone(tx)
         return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) ended when line "
                 f"{failed.line:,} failed: the server rolled it back, so the statements from there on are not applied; "
                 f"run again from statement {tx.index:,}"]
 
-    @staticmethod
-    def _may_have_ended(tx: FileStatement, failed: FileStatement) -> str:
+    def _may_have_ended(self, tx: FileStatement, failed: FileStatement) -> str:
+        self.unclear = True
         return (f"Line {failed.line:,} may have committed or rolled back the file's transaction from line {tx.line:,} "
                 f"(statement {tx.index:,}): the statements from there on may or may not be applied")
 
@@ -595,6 +600,7 @@ class _Run:
         if tx is None:
             if open_now is None:
                 return []
+            self.unclear = True  # which statements the transaction held is not known
             if kept:
                 return ["The file left a transaction open: it was rolled back"]
             return ["The file left a transaction open: it was rolled back, so the statements in it are not applied"]
@@ -604,16 +610,24 @@ class _Run:
             self.unsure = True
             return [self._may_have_ended(tx, procedure)]
         if kept or restarted:  # what stayed is not known statement by statement: the counts stay, the outcome is unknown
+            self.unclear = True
             return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) was still open: it was "
                     "rolled back"]
         self.done, self.rows = self.done_before_tx, self.rows_before_tx
         if began_inside:  # N2: the statement that began it stays counted, and what it did before its BEGIN may stand
+            self.unclear = True
             return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) was still open: it was "
                     "rolled back, so the statements after it are not applied",
                     self._began_inside(tx)]
+        self._undone(tx)
         return [f"The file's transaction from line {tx.line:,} (statement {tx.index:,}) was still open: it was rolled "
                 f"back, so the statements from there on are not applied; run again from statement {tx.index:,}"]
 
+    def _undone(self, tx: FileStatement) -> None:
+        """The file's transaction from `tx` was rolled back and its statements counted out: a run again starts there."""
+        if self.undone_from is None or tx.index < self.undone_from.index:
+            self.undone_from = tx
+
     @staticmethod
     def _began_inside(tx: FileStatement) -> str:
         return (f"Line {tx.line:,} runs several statements and began the transaction inside them: what it did before "
@@ -639,10 +653,22 @@ class _Run:
         return JobReport(
             self.ctx.outcome(outcome, cancelled=cancelled, rows=self.rows, bytes=self.bytes, statements=self.done,
                              path=self.command.path, errors=self.errors, error_count=self.error_count,
-                             stopped_at=at, warnings=said),
+                             stopped_at=at, resume_at=self._resume_at(outcome, stopped), warnings=said),
             error, schema_changed=self.ctx.sent,
         )
 
+    def _resume_at(self, outcome: JobOutcomeKind, stopped: FileStatement | None) -> StoppedAt | None:
+        """Where a run again starts so that nothing is skipped (06b B1): the earliest of the file transaction rolled
+        back and where the run stopped; None all or nothing, when nothing is left to run, or when what an earlier
+        statement did is not known."""
+        if self.all_or_nothing or outcome == "not_started" or self.unclear or self.restarted or self.unsure or self.kept:
+            return None
+        points = [point for point in (self.undone_from, stopped) if point is not None]
+        if not points:
+            return None
+        first = min(points, key=lambda point: point.index)
+        return StoppedAt(line=first.line, statement=first.index)
+
     def _stopped(self, stop: _Stop) -> JobReport:
         error, statement = stop.error, stop.statement
         cancelled = isinstance(error, StatementCancelled)
@@ -656,12 +682,14 @@ class _Run:
             return self._report("unknown", error, stopped=statement, warnings=self._lost_in_file_transaction(statement))
         warnings = []
         if stop.sent and self.tx_from is None and runs_several(statement.text, self.dialect):
+            self.unclear = True
             warnings.append(f"Line {statement.line:,} runs several statements (a batch or a procedure), each taking "
                             "effect on its own: those that ran before it stopped may be applied")
         warnings += self._settle_file_transaction(statement if stop.sent else None, error)
         outcome: JobOutcomeKind = "stopped"
         if stop.sent and self._ended_inside(error):  # a trigger committed inside it: it may have taken effect (R2)
             outcome = "unknown"
+            self.unclear = True
             error = _reworded(error, f"{error.message}\nA trigger ended the transaction inside line {statement.line:,}: "
                                      "what it did may be applied")
         self._unlock_tables()
@@ -708,9 +736,11 @@ class _Run:
         if may_commit(statement.text, self.dialect) or procedure is not None:
             return [self._may_have_ended(tx, procedure or statement)]
         self.done, self.rows = self.done_before_tx, self.rows_before_tx
+        self._undone(tx)
         lost = (f"The connection was lost inside the file's transaction from line {tx.line:,} (statement {tx.index:,}): "
                 "the server rolls it back, so the statements from there on are not applied")
         if began_inside:
+            self.unclear = True
             return [lost, self._began_inside(tx)]
         return [f"{lost}; run again from statement {tx.index:,}"]
```

- [ ] **Step 4: The excerpt.** In `services/data_service.py` (lines are skipped a 1 MiB chunk at a time with `bytes.count`, so line 5,000,000 of a 1 GB file costs about a second and never holds more than a chunk) and `api/data.py`:

```diff
diff --git a/backend/dbaguy_backend/services/data_service.py b/backend/dbaguy_backend/services/data_service.py
index 0e71dc2..461b11b 100644
--- a/backend/dbaguy_backend/services/data_service.py
+++ b/backend/dbaguy_backend/services/data_service.py
@@ -9,6 +9,7 @@ import shutil
 import subprocess
 from collections.abc import Callable
 from pathlib import Path
+from typing import BinaryIO
 from uuid import UUID
 
 from loguru import logger
@@ -20,7 +21,7 @@ from ..adapters.dump import DumpCatalog
 from ..adapters.registry import AdapterRegistry
 from ..domain.data_jobs import (
     BE_DUMPED, ENGINE_NAMES, RUN_SQL_FILES, DumpContent, DumpHeader, DumpObjectKinds, DumpPlan, DumpPlanObject,
-    DumpScope, FileInspection, cannot, cannot_read,
+    DumpScope, FileExcerpt, FileInspection, cannot, cannot_read,
 )
 from ..domain.errors import FileNotFound, NotFound, NotSupported, QueryFailed, ReadOnlyProfile, StorageError
 from ..domain.profiles import AnyProfile, Engine, SqliteProfile
@@ -34,6 +35,11 @@ from .profile_service import ProfileService
 
 PREVIEW_LINES = 200  # spec §3.2: the first lines of a file Run SQL file shows
 PREVIEW_BYTES = 64 * 1024
+EXCERPT_BEFORE = 20  # lines shown before the one asked for (06b B2)
+EXCERPT_LINES = 60
+EXCERPT_BYTES = 64 * 1024
+_READ_CHUNK = 1 << 20
+_UTF8_BOM = b"\xef\xbb\xbf"
 LOW_DISK_BYTES = 1 << 30  # CR-4: below this much free space a dump's plan warns, whatever its estimate
 _GIB = 1 << 30
 _COPY_FROM_STDIN = re.compile(r"COPY\s.*\sFROM\s+stdin", re.IGNORECASE)
@@ -160,6 +166,54 @@ class DataService:
         logger.info("Showing {} in Finder", target)
         self._reveal(target)
 
+    def excerpt(self, path: str, line: int) -> FileExcerpt:
+        """The lines of the file at `path` around `line` (06b B2): from EXCERPT_BEFORE lines before it, at most
+        EXCERPT_LINES lines and EXCERPT_BYTES. No connection: what Run SQL file's dialog shows of an error's place."""
+        target = Path(path).expanduser()
+        if not target.is_absolute() or not target.is_file():
+            raise FileNotFound(path)
+        first = max(1, line - EXCERPT_BEFORE)
+        try:
+            with target.open("rb") as file:
+                head, skipped = self._skip_lines(file, first - 1)
+                body = head[:EXCERPT_BYTES + 1]
+                if len(body) <= EXCERPT_BYTES:
+                    body += file.read(EXCERPT_BYTES + 1 - len(body))
+        except OSError as exc:
+            raise StorageError(cannot_read(path, exc)) from None
+        if skipped < first - 1:  # the file has fewer lines
+            return FileExcerpt(path=str(target), first_line=first, text="", found=False, truncated=False)
+        more = len(body) > EXCERPT_BYTES
+        body = body[:EXCERPT_BYTES]
+        if first == 1 and body.startswith(_UTF8_BOM):
+            body = body[len(_UTF8_BOM):]
+        lines = body.split(b"\n")
+        if not more and lines[-1] == b"":
+            lines.pop()  # after the file's last line end: no line
+        shown, truncated = (lines[:EXCERPT_LINES], True) if len(lines) > EXCERPT_LINES else (lines, more)
+        logger.info("Excerpt of {} at line {:,}", target, line)
+        return FileExcerpt(path=str(target), first_line=first, text=b"\n".join(shown).decode("utf-8", "replace"),
+                           found=line - first < len(lines), truncated=truncated)
+
+    @staticmethod
+    def _skip_lines(file: BinaryIO, count: int) -> tuple[bytes, int]:
+        """Read past `count` line ends: what was read after the last of them, and how many there were (fewer: the file
+        ended first)."""
+        seen = 0
+        while seen < count:
+            chunk = file.read(_READ_CHUNK)
+            if not chunk:
+                return b"", seen
+            ends = chunk.count(b"\n")
+            if seen + ends < count:
+                seen += ends
+                continue
+            at = -1
+            for _ in range(count - seen):
+                at = chunk.index(b"\n", at + 1)
+            return chunk[at + 1:], count
+        return b"", seen
+
     def inspect(self, profile_id: UUID, path: str) -> FileInspection:
         """Run SQL file's look at a file before it runs: its size, its first lines, a DbaGuy dump's header, and what
         the run will stop at or should be told (another engine's dump, objects dropped first, only a filter's rows of a
```

```diff
diff --git a/backend/dbaguy_backend/api/data.py b/backend/dbaguy_backend/api/data.py
index 9700cef..7f71551 100644
--- a/backend/dbaguy_backend/api/data.py
+++ b/backend/dbaguy_backend/api/data.py
@@ -9,8 +9,8 @@ from pydantic import BaseModel, ConfigDict, Field, model_validator
 
 from ..container import AppContainer
 from ..domain.data_jobs import (
-    DUMP_FILTER_LENGTH, FILTER_ONE_TABLE, DumpContent, DumpObjectKinds, DumpPlan, DumpScope, FileInspection, JobInfo,
-    filter_fits,
+    DUMP_FILTER_LENGTH, FILTER_ONE_TABLE, DumpContent, DumpObjectKinds, DumpPlan, DumpScope, FileExcerpt,
+    FileInspection, JobInfo, filter_fits,
 )
 from ..domain.errors import NotFound
 from .deps import get_container, require_token
@@ -32,6 +32,13 @@ class RunFileInspectRequest(BaseModel):
     path: str = Field(min_length=1, max_length=4096)
 
 
+class RunFileExcerptRequest(BaseModel):
+    model_config = ConfigDict(extra="forbid")
+
+    path: str = Field(min_length=1, max_length=4096)
+    line: int = Field(ge=1)
+
+
 class DumpPlanRequest(BaseModel):
     model_config = ConfigDict(extra="forbid")
 
@@ -79,6 +86,12 @@ def inspect_run_file(body: RunFileInspectRequest, container: AppContainer = Depe
     return container.data.inspect(body.profile_id, body.path)
 
 
+@router.post("/run-file/excerpt", response_model=FileExcerpt)
+def run_file_excerpt(body: RunFileExcerptRequest, container: AppContainer = Depends(get_container)) -> FileExcerpt:
+    """Run SQL file's error list: the file's lines around one line (06b B2). 404 `file_not_found` for no file."""
+    return container.data.excerpt(body.path, body.line)
+
+
 @router.get("/jobs", response_model=list[JobInfo])
 def list_jobs(container: AppContainer = Depends(get_container)) -> list[JobInfo]:
     """Newest first: running and finished, until cleared (D-06-10)."""
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/bin/pytest -q tests/test_run_file_job.py tests/test_run_file_inspect.py tests/test_job_framework.py`
Expected: all pass (planning dry run: 109 in `test_run_file_job.py`, 16 in `test_run_file_inspect.py`, `test_job_framework.py` unchanged).

Then, with the dev containers up and no scratch object anywhere: `cd backend && .venv/bin/pytest -q tests/test_run_file.py` — expected: all pass (the live runs' outcomes only gain a defaulted field).

- [ ] **Step 6: The types.** Run `npm run gen:api`, then add to `src/lib/api/types.ts` after `DumpPlanObject`:

```diff
diff --git a/src/lib/api/types.ts b/src/lib/api/types.ts
index bae533a..60c21fa 100644
--- a/src/lib/api/types.ts
+++ b/src/lib/api/types.ts
@@ -154,6 +154,12 @@ export type DumpObjectKinds = S["DumpObjectKinds"];
 export type DumpPlan = S["DumpPlan"];
 /** Dump plan: one object the dump writes under its own name (a table with its rows estimate). */
 export type DumpPlanObject = S["DumpPlanObject"];
+/** Dump plan request (POST /data/dump/plan): the dump command's fields, and the profile and database to read. */
+export type DumpPlanRequest = S["DumpPlanRequest"];
+/** Data tools: where a run stopped, or where a run again starts (`resume_at`, 06b B1): a line and a statement number. */
+export type StoppedAt = S["StoppedAt"];
+/** Run SQL file (06b B2): a file's lines around one line, for the error list. */
+export type FileExcerpt = S["FileExcerpt"];
 
 export function pathKey(path: PathSegment[]): string {
   return path.map((s) => `${s.kind}:${s.name}`).join("/");
```

Run: `npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add backend/dbaguy_backend/domain/data_jobs.py backend/dbaguy_backend/sessions/jobs/run_file.py \
  backend/dbaguy_backend/services/data_service.py backend/dbaguy_backend/api/data.py \
  backend/tests/test_run_file_job.py backend/tests/test_run_file_inspect.py \
  src/lib/api/openapi.json src/lib/api/schema.d.ts src/lib/api/types.ts
git commit -m "feat(data): resume_at says where a run again starts without skipping; POST /data/run-file/excerpt"
```

---

### Task 3: A job's state and its words

**Suggested implementer:** Opus (what an outcome means and which actions are safe).

**Files:**
- Create: `src/features/data-tools/jobState.ts`, `jobState.test.ts`, `jobReport.ts`, `jobReport.test.ts`, `src/lib/sqliteBusy.ts`
- Modify: `src/lib/format.ts`, `src/features/edits/commitFlow.ts`, `src/features/table-data/filterValue.ts`, `src/features/notifications/useLongQueryNotifications.ts`

**Interfaces:**
- Consumes: `JobOutcomeEvent` (with `resume_at`, `error`, `stopped_at`, `warnings`, `errors`, `error_count`), `JobProgressEvent`, `JobInfo`, `ServerEvent`, `DumpCommand`, `RunFileCommand` from `types.ts` (Task 2).
- Produces:
  - `jobState.ts`: `JobKind`, `JobCommand`, `JobLoss`, `JobState`, `JobEvent`, `MAX_JOB_MESSAGES`, `openingJob(at)`, `listedJob(info, at)`, `jobReducer(state, event)`, `neverReachedBackend(state)`, `lastStage(state)`.
  - `jobReport.ts`: `JobView {kind, command, state}`, `Tone`, `RunPatch`, `JobAction`, `ErrorLine`, `JobReport`, `CODE_LABELS`, `codeLabel(code)`, `jobReport(view)`, `ProgressView`, `etaText(done, total, elapsedMs)`, `progressView(view, now, tables?)`, `jobSummary(view, now)`, `jobNotification(view & {title}, thresholdS, focused)`.
  - `lib/format.ts`: `formatElapsed(ms)` ("42 s", "2 min 05 s"), `formatSize(bytes)` ("512 KB", "148.2 MB", "2.31 GB": binary units, as the backend's own texts).
  - `lib/sqliteBusy.ts`: `isSqliteBusy(code)`, `sqliteBusyNote(retry)`; `features/table-data/filterValue.ts`: `FILTER_RULE`.

The reducer's rules (each pinned by a test below): the first ending stands (an outcome, or a loss the frontend decides only when the backend can no longer say); `job_outcome` ends the job whether or not `finished` follows (a late client gets none, 06a carry); a `finished` without an outcome is the loss `no_outcome`, never completed; a session gone after the command left is `session_gone`, before it `not_sent`; only `open_failed` and `not_sent` may be retried; the backend's list replaces a loss with its outcome and gives a running job its progress only when it has none (a live one is newer).

What a job says once it ended (the copy, decided here; spec §4's examples kept): see `jobReport.ts` below. Every action that runs something again is `{kind: "again", patch}` and goes back to the form (V2); `show_in_finder` is the only one that acts at once. "Skip it and continue" needs `resume_at` equal to `stopped_at` and a code a run can read past; a run listed after a reload (no command) offers no run-again.

- [ ] **Step 1: Write the failing tests** — `src/features/data-tools/jobState.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { JobInfo, JobOutcomeEvent, JobProgressEvent, ServerEvent } from "@/lib/api/types";
import { jobReducer, listedJob, MAX_JOB_MESSAGES, neverReachedBackend, openingJob, type JobEvent, type JobState } from "./jobState";

/* A job's state (spec ⑥ R-06-15, D-06-10): the outcome ends it, with or without `finished`; a loss only when the
   backend can no longer say, and the backend's list outranks it. */

const RID = "r1";
const progress = (fields: Partial<JobProgressEvent> = {}): JobProgressEvent => ({
  type: "job_progress",
  request_id: RID,
  stage: "statements",
  rows: 0,
  bytes: 100,
  statements: 1,
  errors: 0,
  ...fields,
});
const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome",
  request_id: RID,
  job: "run_file",
  outcome: "completed",
  cancelled: false,
  rows: 0,
  bytes: 0,
  statements: 3,
  objects: [],
  errors: [],
  error_count: 0,
  warnings: [],
  ...fields,
});
const server = (event: ServerEvent, at = 10): JobEvent => ({ type: "server", event, at });
const run = (events: JobEvent[], from: JobState = openingJob(0)) => events.reduce(jobReducer, from);
const sent: JobEvent = { type: "sent", at: 1 };
const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
  session_id: "s1",
  request_id: RID,
  job: "run_file",
  title: "Run x.sql on Local PG",
  profile_id: "00000000-0000-0000-0000-000000000001",
  started_at: "2026-10-23T01:02:03Z",
  ...fields,
});

describe("a job's state", () => {
  it("runs from started and progress, and ends at its outcome with or without the finished after it", () => {
    const live = run([sent, server({ type: "started", request_id: RID }), server(progress())]);
    expect([live.phase, live.progress?.bytes]).toEqual(["running", 100]);
    const done = run([server(outcome({ statements: 7 }), 20)], live);
    expect([done.phase, done.outcome?.statements, done.endedAt, done.loss]).toEqual(["ended", 7, 20, null]);
    const after = run([server({ type: "finished", request_id: RID, elapsed_ms: 5, in_transaction: false, transaction_failed: false, cancelled: false, timed_out: false, skipped: false })], done);
    expect(after).toBe(done);
  });

  it("ends at a replayed outcome alone: a late client gets no finished (06a carry)", () => {
    const replay = run([sent, server(progress({ statements: 9 })), server(outcome({ outcome: "stopped" }))]);
    expect([replay.phase, replay.outcome?.outcome, replay.progress?.statements]).toEqual(["ended", "stopped", 9]);
  });

  it("keeps the outcome's error, and the live error before it", () => {
    const failed = outcome({ outcome: "rolled_back", error: { line: 4, statement: 2, code: "42P01", message: "no table" } });
    const state = run([sent, server({ type: "error", request_id: RID, message: "live", code: "42P01" }), server(failed)]);
    expect(state.error).toEqual({ message: "no table", code: "42P01" });
    const onlyLive = run([sent, server({ type: "error", request_id: RID, message: "live", code: null }), server(outcome())]);
    expect(onlyLive.error).toEqual({ message: "live", code: null });
  });

  it("keeps at most the backend's number of messages", () => {
    const message = server({ type: "message", request_id: RID, severity: "warning", text: "w" });
    const state = run([sent, ...Array.from({ length: MAX_JOB_MESSAGES + 5 }, () => message)]);
    expect(state.messages).toHaveLength(MAX_JOB_MESSAGES);
  });

  it("calls a request that finished without an outcome unknown, never completed", () => {
    const state = run([sent, server({ type: "finished", request_id: RID, elapsed_ms: 5, in_transaction: false, transaction_failed: false, cancelled: false, timed_out: false, skipped: false })]);
    expect([state.phase, state.loss, state.outcome]).toEqual(["ended", "no_outcome", null]);
  });

  it("tells a session gone after the command left from one gone before it", () => {
    expect(run([sent, { type: "session_gone", at: 5 }]).loss).toBe("session_gone");
    expect(run([{ type: "session_gone", at: 5 }]).loss).toBe("not_sent");
    expect(run([sent, { type: "not_sent", at: 5 }]).phase).toBe("opening"); // sent: never "not sent"
  });

  it("starts again only what never reached the backend", () => {
    const failed = run([{ type: "open_failed", error: new Error("password"), at: 3 }]);
    expect([failed.loss, neverReachedBackend(failed)]).toEqual(["open_failed", true]);
    expect(run([{ type: "retry", at: 9 }], failed)).toEqual(openingJob(9));
    const gone = run([sent, { type: "session_gone", at: 5 }]);
    expect(neverReachedBackend(gone)).toBe(false);
    expect(run([{ type: "retry", at: 9 }], gone)).toBe(gone);
  });

  it("lets the backend's listed outcome replace a loss, never an outcome", () => {
    const gone = run([sent, { type: "session_gone", at: 5 }]);
    const listed = run([{ type: "listed", info: info({ outcome: outcome({ outcome: "unknown", warnings: ["Its session closed before it reported an outcome"] }) }), at: 6 }], gone);
    expect([listed.loss, listed.outcome?.outcome]).toEqual([null, "unknown"]);
    const done = run([sent, server(outcome({ outcome: "completed" }))]);
    expect(run([{ type: "listed", info: info({ outcome: outcome({ outcome: "unknown" }) }), at: 6 }], done)).toBe(done);
  });

  it("takes the list's progress only when it has none: a live one is newer", () => {
    const live = run([sent, server(progress({ statements: 50 }))]);
    expect(run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], live)).toBe(live);
    const fresh = run([sent]);
    expect(run([{ type: "listed", info: info({ progress: progress({ statements: 10 }) }), at: 6 }], fresh).progress?.statements).toBe(10);
  });

  it("builds a listed job as running or ended, from its start time", () => {
    const running = listedJob(info({ progress: progress() }), 99);
    expect([running.phase, running.sent, running.startedAt]).toEqual(["running", true, Date.parse("2026-10-23T01:02:03Z")]);
    const done = listedJob(info({ outcome: outcome({ outcome: "failed", job: "dump" }) }), 99);
    expect([done.phase, done.outcome?.outcome]).toEqual(["ended", "failed"]);
  });

  it("calls a job not listed only once it was sent", () => {
    expect(run([{ type: "not_listed", at: 5 }]).phase).toBe("opening");
    expect(run([sent, { type: "not_listed", at: 5 }]).loss).toBe("not_listed");
  });

  it("follows the socket while running, and records a cancel once", () => {
    const state = run([sent, { type: "socket", open: false }, { type: "cancel_sent", at: 7 }]);
    expect([state.reconnecting, state.cancelRequestedAt]).toEqual([true, 7]);
    expect(run([{ type: "socket", open: true }], state).reconnecting).toBe(false);
  });
});
```

and `src/features/data-tools/jobReport.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { DumpCommand, JobOutcomeEvent, JobProgressEvent, RunFileCommand } from "@/lib/api/types";
import { sqliteBusyNote } from "@/lib/sqliteBusy";
import { FILTER_RULE } from "../table-data/filterValue";
import { CODE_LABELS, codeLabel, etaText, jobNotification, jobReport, jobSummary, progressView, type JobView } from "./jobReport";
import { jobReducer, openingJob, type JobState } from "./jobState";

/* A job in words (spec ⑥ §3.6, §4): every outcome and loss, the actions that are safe now, the progress and its ETA. */

const run = (fields: Partial<RunFileCommand> = {}): RunFileCommand => ({
  type: "run_file",
  request_id: "r1",
  path: "/tmp/x.sql",
  mode: "statements",
  on_error: "stop",
  from_statement: 1,
  ...fields,
});
const dump: DumpCommand = {
  type: "dump",
  request_id: "r1",
  path: "/tmp/d.sql",
  scope: { kind: "schema", schema_name: "public", names: [] },
  content: "both",
  objects: { views: true, sequences: true, types: true, routines: true, triggers: true },
  drop_first: false,
};
const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome",
  request_id: "r1",
  job: "run_file",
  outcome: "completed",
  cancelled: false,
  rows: 0,
  bytes: 0,
  statements: 0,
  path: "/tmp/x.sql",
  objects: [],
  errors: [],
  error_count: 0,
  warnings: [],
  ...fields,
});
const ended = (event: JobOutcomeEvent, from: JobState = openingJob(0)): JobState =>
  jobReducer(jobReducer(from, { type: "sent", at: 1 }), { type: "server", event, at: 2 });
const view = (state: JobState, command: RunFileCommand | DumpCommand | null = run()): JobView => ({
  kind: command?.type === "dump" ? "dump" : "run_file",
  command,
  state,
});
const report = (event: JobOutcomeEvent, command: RunFileCommand | DumpCommand | null = run()) =>
  jobReport(view(ended(event), command))!;
const labels = (event: JobOutcomeEvent, command: RunFileCommand | DumpCommand | null = run()) =>
  report(event, command).actions.map((action) => action.label);

describe("a run's outcome in words", () => {
  it("says stopped, what was applied, and offers to run again from there or skip it", () => {
    const stopped = outcome({
      outcome: "stopped",
      statements: 1233,
      stopped_at: { line: 5001, statement: 1234 },
      resume_at: { line: 5001, statement: 1234 },
      error: { line: 5001, statement: 1234, code: "23505", message: "duplicate key" },
    });
    const said = report(stopped);
    expect([said.tone, said.title, said.detail]).toEqual(["danger", "Stopped at line 5,001", "1,233 statements applied"]);
    expect(said.error).toEqual({ line: 5001, statement: 1234, code: "23505", label: null, message: "duplicate key" });
    expect(said.actions).toEqual([
      { kind: "again", label: "Run again from line 5,001", patch: { mode: "statements", on_error: "stop", from_statement: 1234 } },
      { kind: "again", label: "Skip it and continue", patch: { mode: "statements", on_error: "stop", from_statement: 1235 } },
    ]);
  });

  it("runs again from the file's rolled-back transaction, never from the failure after it (06b B1)", () => {
    const stopped = outcome({
      outcome: "stopped",
      stopped_at: { line: 40, statement: 30 },
      resume_at: { line: 2, statement: 2 },
      error: { line: 40, statement: 30, code: "42000", message: "boom" },
    });
    expect(report(stopped).actions).toEqual([
      { kind: "again", label: "Run again from line 2", patch: { mode: "statements", on_error: "stop", from_statement: 2 } },
    ]);
  });

  it("offers no skip past a line the file cannot be read beyond", () => {
    for (const code of ["file_not_utf8", "statement_too_large", "psql_meta_command", "unsupported_copy", "read_only", "file_io"]) {
      const stopped = outcome({
        outcome: "stopped",
        stopped_at: { line: 3, statement: 3 },
        resume_at: { line: 3, statement: 3 },
        error: { line: 3, statement: 3, code, message: "x" },
      });
      expect(labels(stopped)).toEqual(["Run again from line 3"]);
    }
  });

  it("offers nothing to run again when what ran before is not known", () => {
    const unclear = outcome({ outcome: "unknown", stopped_at: { line: 3, statement: 3 }, resume_at: null });
    expect(labels(unclear)).toEqual([]);
    expect(report(unclear).title).toBe("Outcome unknown at line 3");
    const atCommit = outcome({ outcome: "unknown", error: { code: "commit_unconfirmed", message: "COMMIT returned but…" } });
    expect([report(atCommit, run({ mode: "all_or_nothing" })).title, report(atCommit, run({ mode: "all_or_nothing" })).detail]).toEqual([
      "Outcome unknown",
      "What was applied is not known: check the target.",
    ]);
  });

  it("offers both for a lost link outside the file's transaction: the user checks first", () => {
    const lost = outcome({
      outcome: "unknown",
      stopped_at: { line: 2, statement: 2 },
      resume_at: { line: 2, statement: 2 },
      error: { line: 2, statement: 2, code: "connection_lost", message: "Connection to the server was lost" },
    });
    const said = report(lost);
    expect(said.detail).toBe("0 statements applied before line 2; that line may or may not have taken effect.");
    expect(said.actions.map((a) => a.label)).toEqual(["Run again from line 2", "Skip it and continue"]);
    expect([said.error?.label, said.hint]).toEqual([CODE_LABELS.connection_lost, expect.stringContaining("Check the target")]);
  });

  it("says nothing was applied for a rolled-back run, and the other mode for the refusals that need it", () => {
    const refused = outcome({
      outcome: "rolled_back",
      stopped_at: { line: 12, statement: 7 },
      error: { line: 12, statement: 7, code: "transaction_in_file", message: "The file controls its own transactions (line 12: COMMIT): run it statement by statement" },
    });
    const said = report(refused, run({ mode: "all_or_nothing" }));
    expect([said.tone, said.title, said.error?.label]).toEqual(["danger", "Nothing was applied: the transaction was rolled back", "The file has its own transactions"]);
    expect(said.actions).toEqual([
      { kind: "again", label: "Run statement by statement…", patch: { mode: "statements", on_error: "stop", from_statement: 1 } },
    ]);
    const plain = outcome({ outcome: "rolled_back", error: { code: "42P01", message: "no such table" } });
    expect(labels(plain, run({ mode: "all_or_nothing" }))).toEqual(["Run again…"]);
    const cancelled = outcome({ outcome: "rolled_back", cancelled: true });
    expect(report(cancelled).title).toBe("Cancelled: nothing was applied");
  });

  it("reuses the grid's SQLite busy note with the run's own verb (06a F4)", () => {
    const busy = outcome({ outcome: "rolled_back", error: { code: "SQLITE_BUSY", message: "database is locked" } });
    const said = report(busy, run({ mode: "all_or_nothing" }));
    expect([said.error?.label, said.hint]).toEqual(["SQLite file in use", sqliteBusyNote("run the file again")]);
  });

  it("calls a partial run partly applied and offers the rest statement by statement", () => {
    const partial = outcome({
      outcome: "partial",
      stopped_at: { line: 9, statement: 5 },
      error: { code: "transaction_ended", message: "The transaction ended at line 9 (MySQL commits around DDL)…" },
    });
    expect(report(partial, run({ mode: "all_or_nothing" })).title).toBe("Partly applied: the transaction ended at line 9");
    expect(report(partial, run({ mode: "all_or_nothing" })).actions).toEqual([
      { kind: "again", label: "Run the rest statement by statement…", patch: { mode: "statements", on_error: "stop", from_statement: 6 } },
    ]);
  });

  it("warns on a completed run that skipped errors or had statements rolled back", () => {
    expect(report(outcome({ statements: 3 })).tone).toBe("success");
    const skipped = report(outcome({
      statements: 3,
      error_count: 102,
      errors: [{ line: 2, statement: 2, code: "42000", message: "boom" }],
    }));
    expect([skipped.tone, skipped.title, skipped.moreSkipped, skipped.skipped[0].line]).toEqual(["warning", "Ran x.sql with 102 errors skipped", 101, 2]);
    const undone = report(outcome({ resume_at: { line: 2, statement: 2 } }));
    expect([undone.title, undone.actions.map((a) => a.label)]).toEqual(["Read to the end, but some statements were rolled back", ["Run again from line 2"]]);
  });

  it("offers nothing to run again for a job listed after a reload (its command is not here)", () => {
    const stopped = outcome({ outcome: "stopped", stopped_at: { line: 2, statement: 2 }, resume_at: { line: 2, statement: 2 } });
    expect(labels(stopped, null)).toEqual([]);
  });
});

describe("a dump's outcome in words", () => {
  const dumped = (fields: Partial<JobOutcomeEvent>) => outcome({ job: "dump", path: "/tmp/d.sql", ...fields });

  it("says where it went and offers Show in Finder", () => {
    const said = report(dumped({ rows: 12345, bytes: 3 * 1_048_576, objects: [{ kind: "table", name: "public.a", rows: 1 }] }), dump);
    expect([said.tone, said.title, said.detail]).toEqual(["success", "Dumped to d.sql", "12,345 rows in 1 table · 3.0 MB · /tmp/d.sql"]);
    expect(said.actions).toEqual([{ kind: "show_in_finder", label: "Show in Finder", path: "/tmp/d.sql" }]);
  });

  it("says nothing was written when it failed or was cancelled, and offers to dump again", () => {
    const failed = report(dumped({ outcome: "failed", error: { code: "dump_io", message: "Cannot write /tmp/d.sql: Disk full. Nothing was written" } }), dump);
    expect([failed.title, failed.error?.label, failed.actions.map((a) => a.label)]).toEqual(["Dump failed: nothing was written", "The file cannot be written", ["Dump again…"]]);
    expect(report(dumped({ outcome: "failed", cancelled: true }), dump).title).toBe("Dump cancelled: nothing was written");
    const filter = report(dumped({ outcome: "not_started", error: { code: "invalid_filter", message: "The filter must be a condition on the table's rows (what follows WHERE), and nothing else" } }), dump);
    expect([filter.title, filter.hint]).toEqual(["The dump did not start", FILTER_RULE]);
  });
});

describe("a job the backend could not report on", () => {
  const lost = (state: JobState, command: RunFileCommand | DumpCommand | null) => jobReport(view(state, command))!;
  const sentState = (progress?: Partial<JobProgressEvent>) => {
    let state = jobReducer(openingJob(0), { type: "sent", at: 1 });
    if (progress) {
      state = jobReducer(state, { type: "server", at: 2, event: { type: "job_progress", request_id: "r1", stage: "statements", rows: 0, bytes: 0, statements: 0, errors: 0, ...progress } });
    }
    return jobReducer(state, { type: "session_gone", at: 3 });
  };

  it("says an all-or-nothing run is rolled back by the server unless COMMIT had begun", () => {
    expect(lost(sentState({ stage: "statements" }), run({ mode: "all_or_nothing" })).detail).toContain("nothing should be applied");
    expect(lost(sentState({ stage: "commit" }), run({ mode: "all_or_nothing" })).detail).toContain("during COMMIT");
  });

  it("says statement by statement how far it had come, and that the statement running is not known", () => {
    expect(lost(sentState({ statements: 40 }), run()).detail).toBe(
      "About 40 statements applied by then (the last progress); the statement running may or may not have taken effect.",
    );
  });

  it("says a dump that did not finish wrote no file", () => {
    expect(lost(sentState(), dump).detail).toContain("writes no file");
  });

  it("tells a command that never left, and one whose session never opened", () => {
    const notSent = jobReducer(openingJob(0), { type: "session_gone", at: 3 });
    expect(lost(notSent, run()).title).toBe("Not sent: the connection went away first");
    const failed = jobReducer(openingJob(0), { type: "open_failed", error: new Error("A password is needed"), at: 3 });
    expect([lost(failed, run()).title, lost(failed, run()).detail]).toEqual(["Could not connect: nothing was sent", "A password is needed"]);
  });
});

describe("the error codes", () => {
  it("label every code of the 06a contract, and no server code", () => {
    for (const code of [
      "implicit_commit", "procedure_call", "file_truncated", "file_not_utf8", "statement_too_large", "transaction_in_file",
      "transaction_ended", "psql_meta_command", "unsupported_copy", "file_io", "dump_io", "invalid_path", "invalid_filter",
      "invalid_text", "not_supported", "read_only", "transaction_open", "connection_lost", "commit_unconfirmed",
      "file_not_found", "unsupported_option", "transaction_failed", "cancelled", "invalid_command", "session_busy",
    ]) {
      expect(codeLabel(code), code).toBeTruthy();
    }
    expect([codeLabel("42P01"), codeLabel("SQLITE_BUSY_SNAPSHOT"), codeLabel(null)]).toEqual([null, "SQLite file in use", null]);
  });
});

describe("a running job's progress", () => {
  const running = (progress: Partial<JobProgressEvent>) =>
    jobReducer(jobReducer(openingJob(0), { type: "sent", at: 1 }), {
      type: "server",
      at: 2,
      event: { type: "job_progress", request_id: "r1", stage: "statements", rows: 0, bytes: 0, statements: 0, errors: 0, ...progress },
    });

  it("shows a run's bytes of its file, its counts, and an ETA once it has a basis", () => {
    const state = running({ bytes: 50 * 1_048_576, total_bytes: 200 * 1_048_576, statements: 1200, errors: 2, line: 4800 });
    expect(progressView(view(state), 60_000)).toEqual({
      headline: "50.0 MB of 200.0 MB",
      detail: "1,200 statements · 2 errors skipped · line 4,800 · 1 min 00 s",
      percent: 25,
      eta: "about 3 min left",
    });
    expect(progressView(view(running({ stage: "commit" })), 5_000).headline).toBe("Committing…");
  });

  it("names the table a dump writes, and which one of the plan's it is", () => {
    const state = running({ stage: "data", object: "public.b", rows: 10, bytes: 2048 });
    expect(progressView(view(state, dump), 3_000, ["public.a", "public.b", "public.c"]).headline).toBe("Writing public.b (2 of 3)");
    expect(progressView(view(state, dump), 3_000).headline).toBe("Writing public.b");
  });

  it("gives an ETA only after 3 s and 1% of the file", () => {
    expect(etaText(9, 1000, 10_000)).toBeNull(); // under 1%
    expect(etaText(10, 100, 2_000)).toBeNull();
    expect(etaText(50, 100, 10_000)).toBe("less than a minute left");
    expect(etaText(1, 100, 600_000)).toBe("about 17 h left");
    expect(etaText(10, null, 10_000)).toBeNull();
  });

  it("summarises a row of the Jobs list", () => {
    expect(jobSummary(view(openingJob(0)), 0)).toEqual({ text: "Connecting…", tone: null });
    expect(jobSummary(view(ended(outcome({ outcome: "rolled_back", cancelled: true }))), 0)).toEqual({
      text: "Cancelled: nothing was applied",
      tone: "muted",
    });
  });
});

describe("the notification of a long job", () => {
  const long = (event: JobOutcomeEvent, endedAt = 120_000): JobState => ({ ...ended(event), startedAt: 0, endedAt });
  const titled = (state: JobState, command: RunFileCommand | DumpCommand = run()) => ({ ...view(state, command), title: "Run x.sql on Local PG" });

  it("tells of a job that ended after the threshold while the app was in the background", () => {
    expect(jobNotification(titled(long(outcome({ statements: 3 }))), 15, false)).toEqual({
      title: "SQL file run finished after 2 min 00 s",
      body: "Ran x.sql · Run x.sql on Local PG",
    });
    const failed = long(outcome({ job: "dump", outcome: "failed", path: "/tmp/d.sql" }));
    expect(jobNotification(titled(failed, dump), 15, false)?.title).toBe("Dump ended after 2 min 00 s");
  });

  it("stays quiet in front, under the threshold, at 0, and for a job the user cancelled", () => {
    const done = long(outcome());
    expect(jobNotification(titled(done), 15, true)).toBeNull();
    expect(jobNotification(titled(long(outcome(), 10_000)), 15, false)).toBeNull();
    expect(jobNotification(titled(done), 0, false)).toBeNull();
    expect(jobNotification(titled({ ...long(outcome({ outcome: "rolled_back", cancelled: true })), cancelRequestedAt: 5 }), 15, false)).toBeNull();
  });
});
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `npx vitest run src/features/data-tools/`
Expected: FAIL (cannot resolve `./jobState`, `./jobReport`).

- [ ] **Step 3: The shared helpers.** Append `formatElapsed` and `formatSize` to `src/lib/format.ts`; create `src/lib/sqliteBusy.ts`; move `commitFlow.ts` onto it (its `SQLITE_BUSY_NOTE` keeps its exact text: `sqliteBusyNote("commit again")`); add `FILTER_RULE` to `filterValue.ts`; the long-query notification uses `formatElapsed` instead of its private `seconds` (the same text):

```diff
diff --git a/src/lib/format.ts b/src/lib/format.ts
index ddf59b7..847fab7 100644
--- a/src/lib/format.ts
+++ b/src/lib/format.ts
@@ -46,3 +46,17 @@ export function formatTimeUntil(at: number, now: number): string {
   }
   return relativeTime.format(Math.max(1, Math.floor(remaining / 1000)), "second");
 }
+
+/** "42 s", or "2 min 05 s" from a minute on: whole seconds, for elapsed times and notifications. */
+export function formatElapsed(ms: number): string {
+  const whole = Math.max(0, Math.round(ms / 1000));
+  return whole < 60 ? `${whole} s` : `${Math.floor(whole / 60)} min ${String(whole % 60).padStart(2, "0")} s`;
+}
+
+const MIB = 1_048_576;
+/** A file's size as the backend's own texts write it (binary units, "MB"): "512 KB", "148.2 MB", "2.31 GB". */
+export function formatSize(bytes: number): string {
+  if (bytes < MIB) return `${Math.ceil(bytes / 1024).toLocaleString()} KB`;
+  if (bytes < 1024 * MIB) return `${(bytes / MIB).toFixed(1)} MB`;
+  return `${(bytes / (1024 * MIB)).toFixed(2)} GB`;
+}
```

```ts
/** SQLite's busy and locked errors (`sqlite_errorname`): another connection holds the file. */
export const isSqliteBusy = (code: string | null | undefined): boolean =>
  !!code && (code.startsWith("SQLITE_BUSY") || code.startsWith("SQLITE_LOCKED"));

/**
 * Why SQLite said busy, and what to do (ruling ⑤ M4; spec ⑥ 06a F4): `retry` is what to do again ("commit again",
 * "run the file again"). The grid's commit and Run SQL file share it.
 */
export const sqliteBusyNote = (retry: string): string =>
  `Another tab still has an open result of this database file, or another program is using it: finish or close that result, then ${retry}.`;
```

```diff
diff --git a/src/features/edits/commitFlow.ts b/src/features/edits/commitFlow.ts
index 2f59d40..7995453 100644
--- a/src/features/edits/commitFlow.ts
+++ b/src/features/edits/commitFlow.ts
@@ -1,6 +1,7 @@
 import type { EditOutcome, EditOutcomeEvent, EditPreview } from "@/lib/api/types";
 import { plainValue } from "@/lib/cells";
 import { plural } from "@/lib/format";
+import { isSqliteBusy, sqliteBusyNote } from "@/lib/sqliteBusy";
 import { CONNECTION_LOST, INTERRUPTED_MESSAGE, NOT_CONNECTED_CODE, type ExecutionState } from "../session/execution";
 import type { SessionPhase } from "../session/SessionController";
 import { findChange, type AppliedOutcome, type StagedProblem, type Staging } from "./staging";
@@ -196,8 +197,7 @@ export const TRANSACTION_OPEN_NOTE =
   "This tab's session has a transaction open (for example from SQL Server's IMPLICIT_TRANSACTIONS login option): roll it back or reconnect, then preview again.";
 export const SQL_MODE_NOTE =
   "This tab's connection and the connection DbaGuy reads the table with treat backslashes differently: the server's sql_mode changed while they were open. Reconnect this tab and preview again; setting sql_mode in the connection's options keeps it from happening.";
-export const SQLITE_BUSY_NOTE =
-  "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then commit again.";
+export const SQLITE_BUSY_NOTE = sqliteBusyNote("commit again");
 export const PARTIAL_NOTE = "This table cannot roll back: what took effect is no longer staged, the rest still is.";
 const CANCELLED_TITLE = "Cancelled: nothing was applied";
 const CLOSED_TITLE = "The session closed before the commit started: nothing was applied";
@@ -218,10 +218,6 @@ export const NOT_SENT_REPORT: CommitReport = {
 export const UNKNOWN_BEFORE =
   "The last commit's outcome was unknown: an UPDATE or DELETE checks the loaded values again, but a new row would be inserted a second time.";
 
-/** SQLite's busy and locked errors (`sqlite_errorname`): another connection holds the file. */
-export const isSqliteBusy = (code: string | null | undefined): boolean =>
-  !!code && (code.startsWith("SQLITE_BUSY") || code.startsWith("SQLITE_LOCKED"));
-
 /** The warning of a commit that stood though its COMMIT failed (non-transactional): `COMMIT_WARNING` in edit_apply.py. */
 const COMMIT_WARNING = "COMMIT reported an error after every statement had taken effect";
 /**
```

```diff
diff --git a/src/features/table-data/filterValue.ts b/src/features/table-data/filterValue.ts
index acd97b3..f05fbfb 100644
--- a/src/features/table-data/filterValue.ts
+++ b/src/features/table-data/filterValue.ts
@@ -1,6 +1,13 @@
 import { api, unwrap } from "@/lib/api/client";
 import type { ColumnMeta, Dialect, PathSegment } from "@/lib/api/types";
 
+/**
+ * What a filter may be (06a ruling U3: the backend refuses anything else, `invalid_filter`): the table-data tab's filter
+ * bar and a filtered dump say it.
+ */
+export const FILTER_RULE =
+  "A filter is one condition on the rows, as after WHERE: no ORDER BY, LIMIT or second statement (sort with the column headers).";
+
 /** Tree object kinds that hold rows: they open as a table data tab (keep in sync with select_builder.py). */
 export const RELATION_KINDS: ReadonlySet<string> = new Set(["table", "view", "materialized_view", "partition"]);
```

```diff
diff --git a/src/features/notifications/useLongQueryNotifications.ts b/src/features/notifications/useLongQueryNotifications.ts
index 21d878a..7b61f38 100644
--- a/src/features/notifications/useLongQueryNotifications.ts
+++ b/src/features/notifications/useLongQueryNotifications.ts
@@ -1,16 +1,10 @@
 import { useEffect, useRef } from "react";
-import { formatDuration, plural } from "@/lib/format";
+import { formatElapsed, plural } from "@/lib/format";
 import { appIsFocused, notify } from "@/lib/native";
 import { useAppSettings } from "@/stores/settings";
 import type { RunState } from "../query/useQueryRunner";
 import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
 
-/** "42 s", or "2 min 05 s" from a minute on: a notification has no use for hundredths. */
-function seconds(ms: number): string {
-  const whole = Math.round(ms / 1000);
-  return whole < 60 ? `${whole} s` : formatDuration(ms);
-}
-
 const firstLine = (text: string) => text.split("\n", 1)[0].trim();
 
 /** What a single statement returned: "1,234 rows", "500 rows loaded" (more to fetch), "3 rows affected", or nothing. */
@@ -47,7 +41,7 @@ export function runNotification(
   connection: string | null,
 ): { title: string; body: string } | null {
   if (run.finishedAt === null || run.cancelled || run.steps.some((step) => step.status === "cancelled")) return null;
-  const took = seconds(run.finishedAt - run.startedAt);
+  const took = formatElapsed(run.finishedAt - run.startedAt); // a notification has no use for hundredths
   const execution = (requestId: string | null) => (requestId ? executions[requestId] : undefined);
   const failed = run.steps.find((step) => step.status === "error");
   if (failed) return { title: `Query failed after ${took}`, body: failure(execution(failed.requestId)) };
```

- [ ] **Step 4: Implement `jobState.ts`**

```ts
import type {
  DumpCommand,
  JobInfo,
  JobOutcomeEvent,
  JobProgressEvent,
  RunFileCommand,
  ServerEvent,
} from "@/lib/api/types";

/*
 * One data-tools job as the frontend knows it (spec ⑥ R-06-15, D-06-10): folded from its WebSocket events and the
 * backend's job list. Pure: the runner (`jobRunner.ts`) and the store (`jobsStore.ts`) feed it events. The backend's
 * `job_outcome` is the end, with or without the `finished` after it (a client that attaches late gets none, 06a carry);
 * when the backend can no longer say (its session is gone), the frontend ends the job with a `loss` instead, which a
 * later outcome from the backend's list replaces. Nothing here ever sends anything.
 */

export type JobKind = JobOutcomeEvent["job"];
export type JobCommand = DumpCommand | RunFileCommand;

/** How a job ended without the backend's outcome: only what the frontend knows. */
export type JobLoss =
  /** Its session could not be opened (a password, a refused connection): nothing was sent. */
  | "open_failed"
  /** The command never left: cancelled while connecting, or the socket was refused before it opened. */
  | "not_sent"
  /** It was sent, then its session went away (the backend stopped or restarted) before an outcome. */
  | "session_gone"
  /** It was sent a while ago, nothing came back, and the backend's list does not have it. */
  | "not_listed"
  /** The request finished without an outcome (the contract says it never does: read as unknown). */
  | "no_outcome";

export interface JobState {
  phase: "opening" | "running" | "ended";
  /** The command reached the socket: from here on it is never sent again. */
  sent: boolean;
  progress: JobProgressEvent | null;
  outcome: JobOutcomeEvent | null;
  loss: JobLoss | null;
  /** Why the session could not be opened (`open_failed`): the ApiError, for the unlock flow. */
  openError: Error | null;
  /** The live `error` event (the outcome carries it too, 06a F3). */
  error: { message: string; code: string | null } | null;
  /** The job's server messages and warnings, as they came (the backend sends at most MAX_JOB_MESSAGES). */
  messages: { severity: string; text: string }[];
  cancelRequestedAt: number | null;
  /** The socket is closed and trying again: events may be late, never lost (the backend replays its last ones). */
  reconnecting: boolean;
  startedAt: number;
  endedAt: number | null;
  /** When the last event of this job arrived (null: none yet). */
  lastEventAt: number | null;
}

export type JobEvent =
  | { type: "sent"; at: number }
  | { type: "open_failed"; error: Error; at: number }
  | { type: "not_sent"; at: number }
  | { type: "server"; event: ServerEvent; at: number }
  | { type: "socket"; open: boolean }
  | { type: "session_gone"; at: number }
  | { type: "not_listed"; at: number }
  | { type: "cancel_sent"; at: number }
  | { type: "listed"; info: JobInfo; at: number }
  | { type: "retry"; at: number };

/** The backend's own cap (`MAX_JOB_MESSAGES` in domain/data_jobs.py): a job never sends more. */
export const MAX_JOB_MESSAGES = 200;

/** A job this window is starting: its session is being opened. */
export function openingJob(at: number): JobState {
  return {
    phase: "opening",
    sent: false,
    progress: null,
    outcome: null,
    loss: null,
    openError: null,
    error: null,
    messages: [],
    cancelRequestedAt: null,
    reconnecting: false,
    startedAt: at,
    endedAt: null,
    lastEventAt: null,
  };
}

/** A job the backend lists (a reloaded window): what the list says, sent long ago. */
export function listedJob(info: JobInfo, at: number): JobState {
  const startedAt = Date.parse(info.started_at);
  const base = { ...openingJob(Number.isNaN(startedAt) ? at : startedAt), sent: true, progress: info.progress ?? null };
  if (!info.outcome) return { ...base, phase: "running" };
  return { ...base, phase: "ended", outcome: info.outcome, error: errorOf(info.outcome), endedAt: at };
}

const errorOf = (outcome: JobOutcomeEvent) =>
  outcome.error ? { message: outcome.error.message, code: outcome.error.code ?? null } : null;

const ended = (state: JobState, at: number, loss: JobLoss, patch: Partial<JobState> = {}): JobState => ({
  ...state,
  phase: "ended",
  loss,
  endedAt: at,
  reconnecting: false,
  ...patch,
});

/** What nothing reached the backend for: its job may be started again (`retry`). */
export const neverReachedBackend = (state: JobState): boolean =>
  state.phase === "ended" && (state.loss === "open_failed" || state.loss === "not_sent");

function onServerEvent(state: JobState, event: ServerEvent, at: number): JobState {
  if (state.phase === "ended") return state; // the first ending stands (a `finished` after the outcome, say)
  const seen = { ...state, lastEventAt: at };
  switch (event.type) {
    case "started":
      return { ...seen, phase: "running" };
    case "job_progress":
      return { ...seen, phase: "running", progress: event };
    case "job_outcome":
      return {
        ...seen,
        phase: "ended",
        outcome: event,
        error: errorOf(event) ?? state.error,
        endedAt: at,
        reconnecting: false,
      };
    case "error":
      return { ...seen, error: { message: event.message, code: event.code ?? null } };
    case "message":
      if (state.messages.length >= MAX_JOB_MESSAGES) return seen;
      return { ...seen, messages: [...state.messages, { severity: event.severity, text: event.text }] };
    case "finished":
      return ended(seen, at, "no_outcome");
    default:
      return state; // session_state, and nothing else a job sends
  }
}

/** The backend's list: an outcome replaces a loss (it is the truth); a running job takes its progress if it has none. */
function onListed(state: JobState, info: JobInfo, at: number): JobState {
  if (state.outcome) return state;
  if (info.outcome) {
    return { ...state, phase: "ended", outcome: info.outcome, loss: null, error: errorOf(info.outcome), endedAt: at };
  }
  if (state.phase === "ended") return state;
  return state.progress || !info.progress ? state : { ...state, phase: "running", progress: info.progress };
}

export function jobReducer(state: JobState, event: JobEvent): JobState {
  switch (event.type) {
    case "sent":
      return state.phase === "opening" ? { ...state, sent: true } : state;
    case "open_failed":
      return state.phase === "ended" ? state : ended(state, event.at, "open_failed", { openError: event.error });
    case "not_sent":
      return state.phase === "ended" || state.sent ? state : ended(state, event.at, "not_sent");
    case "server":
      return onServerEvent(state, event.event, event.at);
    case "socket":
      return state.phase === "ended" ? state : { ...state, reconnecting: !event.open };
    case "session_gone":
      return state.phase === "ended" ? state : ended(state, event.at, state.sent ? "session_gone" : "not_sent");
    case "not_listed":
      return state.phase === "ended" || !state.sent ? state : ended(state, event.at, "not_listed");
    case "cancel_sent":
      return state.phase === "ended" ? state : { ...state, cancelRequestedAt: event.at };
    case "listed":
      return onListed(state, event.info, event.at);
    case "retry":
      return neverReachedBackend(state) ? openingJob(event.at) : state;
  }
}

/** The stage the last progress was at ("commit": the run's COMMIT may have been sent). */
export const lastStage = (state: JobState): JobProgressEvent["stage"] | null => state.progress?.stage ?? null;
```

- [ ] **Step 5: Implement `jobReport.ts`** (the copy and the action rules are decided here):

```ts
import type { JobOutcomeEvent, JobProgressEvent } from "@/lib/api/types";
import { formatElapsed, formatSize, plural } from "@/lib/format";
import { isSqliteBusy, sqliteBusyNote } from "@/lib/sqliteBusy";
import { FILTER_RULE } from "../table-data/filterValue";
import type { JobCommand, JobKind, JobState } from "./jobState";

/*
 * What a job's dialog and the Jobs list say about it (spec ⑥ §3.6, §4 "結果的說明由前端依 job_outcome 組成"): its
 * progress while it runs and, once it ended, its outcome in words, the error with a label and what to do, the skipped
 * errors and the actions that make sense now. Pure. Every action that would run something again goes back to the form,
 * prefilled, so a run is always started (and, on a red connection, confirmed) from the form (ruling B6).
 */

export interface JobView {
  kind: JobKind;
  /** What was sent; null for a job listed after a reload (it was started by another page load). */
  command: JobCommand | null;
  state: JobState;
}

export type Tone = "success" | "warning" | "danger" | "muted";

/** The form's fields a "run again" action changes. */
export interface RunPatch {
  mode: "all_or_nothing" | "statements";
  on_error: "stop" | "continue";
  from_statement: number;
}

export type JobAction =
  | { kind: "show_in_finder"; label: string; path: string }
  /** Back to the form with the job's own settings, changed by `patch` (null: as they were). */
  | { kind: "again"; label: string; patch: RunPatch | null };

export interface ErrorLine {
  line: number | null;
  statement: number | null;
  code: string | null;
  label: string | null;
  message: string;
}

export interface JobReport {
  tone: Tone;
  title: string;
  /** The facts: counts, where, the error's text. */
  detail: string | null;
  /** The error that ended it, with its label; null when none. */
  error: ErrorLine | null;
  /** What to do about it, when the backend's text does not say. */
  hint: string | null;
  /** The outcome's warnings (what it means, what was left out). */
  notes: string[];
  /** The errors a run skipped (the first ones), and how many more there were. */
  skipped: ErrorLine[];
  moreSkipped: number;
  actions: JobAction[];
}

/** A short name for each of DbaGuy's own error codes (spec ⑥ §4, the 06a carries); a server's own code has none. */
export const CODE_LABELS = {
  implicit_commit: "MySQL commits around this statement",
  procedure_call: "A procedure call may commit inside it",
  transaction_in_file: "The file has its own transactions",
  transaction_ended: "The transaction ended inside the file",
  transaction_failed: "The transaction was aborted",
  commit_unconfirmed: "COMMIT left a transaction open",
  file_truncated: "The dump is cut short",
  file_not_utf8: "Not UTF-8 text",
  statement_too_large: "A statement over 256 MiB",
  psql_meta_command: "A psql command",
  unsupported_copy: "COPY … FROM stdin",
  file_not_found: "Not a file",
  file_io: "The file cannot be read",
  dump_io: "The file cannot be written",
  invalid_path: "Not a file to dump into",
  invalid_filter: "Not one condition",
  invalid_text: "Text a UTF-8 file cannot hold",
  not_supported: "Not available on this connection",
  unsupported_option: "An option this connection does not take",
  read_only: "Read-only connection",
  transaction_open: "A transaction was already open",
  connection_lost: "Connection lost",
  cancelled: "Cancelled",
  invalid_command: "Refused by the backend",
  session_busy: "The job's session was busy",
} as const;
export type JobErrorCode = keyof typeof CODE_LABELS;

const SQLITE_BUSY_LABEL = "SQLite file in use";
/** What to do, where the backend's own text does not say it. */
const CODE_HINTS: Partial<Record<JobErrorCode, string>> = {
  connection_lost:
    "Check the target before running anything again: the statement that was running may or may not have taken effect.",
  transaction_open:
    "The connection had a transaction open before the job began (SQL Server: a login whose user options turn IMPLICIT_TRANSACTIONS on). Nothing was sent.",
  invalid_filter: FILTER_RULE,
};
/** Codes after which "Skip it and continue" cannot help: the file cannot be read past them, or nothing can run. */
const NOT_SKIPPABLE: ReadonlySet<string> = new Set([
  "file_not_utf8",
  "statement_too_large",
  "file_io",
  "psql_meta_command",
  "unsupported_copy",
  "read_only",
]);
/** All-or-nothing refusals whose remedy is the other mode (the backend's text says "run it statement by statement"). */
const NEEDS_STATEMENTS: ReadonlySet<string> = new Set([
  "transaction_in_file",
  "implicit_commit",
  "procedure_call",
  "file_truncated",
  "SQLITE_CONSTRAINT_FOREIGNKEY",
]);

export function codeLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  if (isSqliteBusy(code)) return SQLITE_BUSY_LABEL;
  return code in CODE_LABELS ? CODE_LABELS[code as JobErrorCode] : null;
}

function codeHint(code: string | null, kind: JobKind): string | null {
  if (isSqliteBusy(code)) return sqliteBusyNote(kind === "dump" ? "dump again" : "run the file again");
  return code && code in CODE_HINTS ? (CODE_HINTS[code as JobErrorCode] ?? null) : null;
}

const fileName = (path: string | null | undefined) => (path ? (path.split("/").pop() ?? path) : "the file");
const runCommand = (command: JobCommand | null) => (command?.type === "run_file" ? command : null);
const statementsApplied = (count: number) => `${plural(count, "statement")} applied`;

function errorLine(outcome: JobOutcomeEvent | null, state: JobState): ErrorLine | null {
  const error = outcome?.error ?? (state.error ? { ...state.error, line: null, statement: null } : null);
  if (!error) return null;
  const code = error.code ?? null;
  return { line: error.line ?? null, statement: error.statement ?? null, code, label: codeLabel(code), message: error.message };
}

const NOTHING_WRITTEN_NOTE = "A file that was already at that path is as it was.";

function dumpReport(outcome: JobOutcomeEvent, error: ErrorLine | null): Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint"> {
  const again: JobAction = { kind: "again", label: "Dump again…", patch: null };
  const notes = outcome.warnings;
  switch (outcome.outcome) {
    case "completed": {
      const tables = outcome.objects.length;
      const path = outcome.path ?? "";
      return {
        tone: notes.length ? "warning" : "success",
        title: `Dumped to ${fileName(path)}`,
        detail: `${plural(outcome.rows, "row")} in ${plural(tables, "table")} · ${formatSize(outcome.bytes)} · ${path}`,
        notes,
        actions: [{ kind: "show_in_finder", label: "Show in Finder", path }],
      };
    }
    case "failed":
      return outcome.cancelled
        ? { tone: "muted", title: "Dump cancelled: nothing was written", detail: NOTHING_WRITTEN_NOTE, notes, actions: [again] }
        : { tone: "danger", title: "Dump failed: nothing was written", detail: NOTHING_WRITTEN_NOTE, notes, actions: [again] };
    case "not_started":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled before the dump started" : "The dump did not start",
        detail: error ? null : "Nothing was written.",
        notes,
        actions: [again],
      };
    default: // unknown (the backend's list: its session closed first)
      return {
        tone: "danger",
        title: "The dump's outcome is unknown",
        detail: `Check whether ${outcome.path ?? "the file"} was written.`,
        notes,
        actions: [again],
      };
  }
}

function runReport(outcome: JobOutcomeEvent, command: JobCommand | null, error: ErrorLine | null): Omit<JobReport, "error" | "skipped" | "moreSkipped" | "hint"> {
  const run = runCommand(command);
  const notes = outcome.warnings;
  const at = outcome.stopped_at;
  const resume = outcome.resume_at ?? null;
  const code = error?.code ?? null;
  const actions: JobAction[] = [];
  const onError = run?.on_error ?? "stop";
  // Only a run this page started can be started again from its form (a listed one has no command here).
  if (run) {
    if (resume) {
      actions.push({ kind: "again", label: `Run again from line ${resume.line.toLocaleString()}`,
        patch: { mode: "statements", on_error: onError, from_statement: resume.statement } });
      if (at && resume.statement === at.statement && !(code && NOT_SKIPPABLE.has(code))) {
        actions.push({ kind: "again", label: "Skip it and continue",
          patch: { mode: "statements", on_error: onError, from_statement: at.statement + 1 } });
      }
    } else if (outcome.outcome === "partial" && at) {
      actions.push({ kind: "again", label: "Run the rest statement by statement…",
        patch: { mode: "statements", on_error: "stop", from_statement: at.statement + 1 } });
    } else if ((outcome.outcome === "rolled_back" || outcome.outcome === "not_started") && code && NEEDS_STATEMENTS.has(code)) {
      actions.push({ kind: "again", label: "Run statement by statement…",
        patch: { mode: "statements", on_error: "stop", from_statement: 1 } });
    } else if (outcome.outcome !== "completed" && outcome.outcome !== "unknown") {
      actions.push({ kind: "again", label: "Run again…", patch: null });
    }
  }
  const where = at ? ` at line ${at.line.toLocaleString()}` : "";
  switch (outcome.outcome) {
    case "completed": {
      const undone = resume !== null;
      const skipped = outcome.error_count;
      const title = undone
        ? "Read to the end, but some statements were rolled back"
        : skipped
          ? `Ran ${fileName(outcome.path)} with ${plural(skipped, "error")} skipped`
          : `Ran ${fileName(outcome.path)}`;
      return {
        tone: undone || skipped || notes.length ? "warning" : "success",
        title,
        detail: `${statementsApplied(outcome.statements)} · ${plural(outcome.rows, "row")} affected`,
        notes,
        actions,
      };
    }
    case "rolled_back":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled: nothing was applied" : "Nothing was applied: the transaction was rolled back",
        detail: null,
        notes,
        actions,
      };
    case "partial":
      return {
        tone: "danger",
        title: `Partly applied: the transaction ended${where}`,
        detail: "The statements before it are applied; the rest are not.",
        notes,
        actions,
      };
    case "stopped":
      return {
        tone: outcome.cancelled ? "warning" : "danger",
        title: `${outcome.cancelled ? "Cancelled" : "Stopped"}${where}`,
        detail: statementsApplied(outcome.statements),
        notes,
        actions,
      };
    case "not_started":
      return {
        tone: outcome.cancelled ? "muted" : "danger",
        title: outcome.cancelled ? "Cancelled before anything ran" : "Not started: nothing was run",
        detail: null,
        notes,
        actions,
      };
    default: // unknown, failed (never for a run)
      return {
        tone: "danger",
        title: `Outcome unknown${where}`,
        // Statement by statement, the statements before the stop took effect; in one transaction, nothing is known
        detail:
          run?.mode === "statements" && at
            ? `${statementsApplied(outcome.statements)} before line ${at.line.toLocaleString()}; that line may or may not have taken effect.`
            : "What was applied is not known: check the target.",
        notes,
        actions,
      };
  }
}

/** The frontend's own ending (no outcome from the backend): what is known, and nothing more. */
function lossReport(view: JobView): JobReport {
  const { state, kind } = view;
  const base = { error: null, hint: null, notes: [], skipped: [], moreSkipped: 0 };
  const again: JobAction = { kind: "again", label: kind === "dump" ? "Dump again…" : "Run again…", patch: null };
  const canAgain = view.command ? [again] : [];
  switch (state.loss) {
    case "open_failed":
      return { ...base, tone: "danger", title: "Could not connect: nothing was sent", detail: state.openError?.message ?? null, actions: [] };
    case "not_sent":
      return {
        ...base,
        tone: "muted",
        title: state.cancelRequestedAt !== null ? "Cancelled before it was sent" : "Not sent: the connection went away first",
        detail: "Nothing ran.",
        actions: canAgain,
      };
    default: {
      const notListed = state.loss === "not_listed";
      const title = notListed
        ? "The backend has no record of this job: it may never have started"
        : "The backend stopped before the job reported its outcome";
      if (kind === "dump") {
        return { ...base, tone: "danger", title,
          detail: "A dump that does not finish writes no file (at most a hidden .part file beside it, which can be deleted).",
          actions: canAgain };
      }
      const run = runCommand(view.command);
      const statements = state.progress?.statements ?? 0;
      const detail =
        run === null
          ? "What it applied is not known: check the target."
          : run.mode === "all_or_nothing"
            ? state.progress?.stage === "commit"
              ? "It stopped during COMMIT: what was applied is not known. Check the target."
              : "The run was one transaction, which the server rolls back when its connection ends: nothing should be applied. Check the target."
            : `About ${statementsApplied(statements)} by then (the last progress); the statement running may or may not have taken effect.`;
      return { ...base, tone: "danger", title, detail, actions: [] };
    }
  }
}

/** How the job ended, in words; null while it runs. */
export function jobReport(view: JobView): JobReport | null {
  const { state, kind, command } = view;
  if (state.phase !== "ended") return null;
  const outcome = state.outcome;
  if (!outcome) return lossReport(view);
  const error = errorLine(outcome, state);
  const words = kind === "dump" ? dumpReport(outcome, error) : runReport(outcome, command, error);
  const skipped = outcome.errors.map((item) => ({
    line: item.line ?? null,
    statement: item.statement ?? null,
    code: item.code ?? null,
    label: codeLabel(item.code),
    message: item.message,
  }));
  return {
    ...words,
    error,
    hint: codeHint(error?.code ?? null, kind),
    skipped,
    moreSkipped: Math.max(0, outcome.error_count - skipped.length),
  };
}

export interface ProgressView {
  headline: string;
  detail: string | null;
  /** 0–100 when the job knows its total (a run: the file's bytes). */
  percent: number | null;
  eta: string | null;
}

const ETA_AFTER_MS = 3_000;
const ETA_MIN_SHARE = 0.01;

/** "about 3 min left" once a run has read 1% of its file for 3 s; null before, or when the total is unknown. */
export function etaText(done: number, total: number | null | undefined, elapsedMs: number): string | null {
  if (!total || done <= 0 || done >= total || elapsedMs < ETA_AFTER_MS || done / total < ETA_MIN_SHARE) return null;
  const left = (elapsedMs * (total - done)) / done;
  if (left < 60_000) return "less than a minute left";
  const minutes = Math.round(left / 60_000);
  return minutes < 120 ? `about ${minutes} min left` : `about ${Math.round(minutes / 60)} h left`;
}

const DUMP_STAGES: Partial<Record<JobProgressEvent["stage"], string>> = {
  starting: "Reading the catalog",
  schema: "Writing the schema",
  post_data: "Writing what comes after the data",
};

/** What a running job's dialog and row say: `tables` are the dump's tables in their order (from its plan), if known. */
export function progressView(view: JobView, now: number, tables: readonly string[] | null = null): ProgressView {
  const { state, kind } = view;
  const elapsed = formatElapsed(now - state.startedAt);
  if (state.phase === "opening") return { headline: "Connecting…", detail: null, percent: null, eta: null };
  const progress = state.progress;
  if (!progress) return { headline: "Starting…", detail: elapsed, percent: null, eta: null };
  if (kind === "dump") {
    let headline = DUMP_STAGES[progress.stage] ?? "Writing the rows";
    if (progress.stage === "data" && progress.object) {
      const index = tables?.indexOf(progress.object) ?? -1;
      headline = `Writing ${progress.object}${index >= 0 && tables ? ` (${index + 1} of ${tables.length})` : ""}`;
    }
    return {
      headline,
      detail: `${plural(progress.rows, "row")} · ${formatSize(progress.bytes)} written · ${elapsed}`,
      percent: null,
      eta: null,
    };
  }
  if (progress.stage === "commit") return { headline: "Committing…", detail: elapsed, percent: 100, eta: null };
  const total = progress.total_bytes ?? null;
  const parts = [
    plural(progress.statements, "statement"),
    ...(progress.errors ? [`${plural(progress.errors, "error")} skipped`] : []),
    ...(progress.line ? [`line ${progress.line.toLocaleString()}`] : []),
    elapsed,
  ];
  return {
    headline: total ? `${formatSize(progress.bytes)} of ${formatSize(total)}` : formatSize(progress.bytes),
    detail: parts.join(" · "),
    percent: total ? Math.min(100, (progress.bytes / total) * 100) : null,
    eta: etaText(progress.bytes, total, now - state.startedAt),
  };
}

/** One line for the Jobs list: the progress while it runs, the outcome's title once it ended. */
export function jobSummary(view: JobView, now: number): { text: string; tone: Tone | null } {
  const report = jobReport(view);
  if (report) return { text: report.title, tone: report.tone };
  const progress = progressView(view, now);
  return { text: [progress.headline, progress.eta].filter(Boolean).join(" · "), tone: null };
}

/**
 * The notification for a job that ended after at least `thresholdS` seconds (S8's `notifications.long_query_seconds`,
 * 0: never) while DbaGuy was in the background (R-06-15); none for a job the user cancelled or one that never started.
 */
export function jobNotification(
  view: JobView & { title: string },
  thresholdS: number,
  focused: boolean,
): { title: string; body: string } | null {
  const { state } = view;
  const report = jobReport(view);
  if (!report || focused || thresholdS <= 0 || state.endedAt === null) return null;
  if (state.cancelRequestedAt !== null || state.loss === "open_failed" || state.loss === "not_sent") return null;
  const took = state.endedAt - state.startedAt;
  if (took < thresholdS * 1000) return null;
  const what = view.kind === "dump" ? "Dump" : "SQL file run";
  const verdict = report.tone === "success" ? "finished" : "ended";
  return { title: `${what} ${verdict} after ${formatElapsed(took)}`, body: `${report.title} · ${view.title}` };
}
```

- [ ] **Step 6: Run the tests**

Run: `npx vitest run src/features/data-tools/ src/features/edits/commitFlow.test.ts`
Expected: all pass (`commitFlow.test.ts`'s SQLite busy tests unchanged).

- [ ] **Step 7: Check the types and the build**

Run: `npm run typecheck && npm run build && npm test`
Expected: no errors; all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/features/data-tools/jobState.ts src/features/data-tools/jobState.test.ts src/features/data-tools/jobReport.ts \
  src/features/data-tools/jobReport.test.ts src/lib/format.ts src/lib/sqliteBusy.ts src/features/edits/commitFlow.ts \
  src/features/table-data/filterValue.ts src/features/notifications/useLongQueryNotifications.ts
git commit -m "feat(data-tools): a job's state from its events and the backend's list, and its outcome in words"
```

---

### Task 4: Entry points, file names and the two forms (pure)

**Suggested implementer:** Sonnet (rules given whole; tests pin them).

**Files:**
- Create: `src/lib/filePaths.ts`, `src/features/data-tools/{entryPoints.ts, entryPoints.test.ts, fileNames.ts, dumpForm.ts, runForm.ts, forms.test.ts}`
- Modify: `src/stores/tabStatus.ts`

**Interfaces:**
- Consumes: `relationOf`, `FILTER_RULE` (`features/table-data/filterValue.ts`), `RunPatch` (Task 3), `EngineInfo`, `ObjectNode`, `PathSegment`, `Profile`, `QueryContext`, `Tab`.
- Produces:
  - `lib/filePaths.ts`: `BROWSER_PATH_HINT`, `BROWSER_OPEN_HINT`, `pathProblem(text)`, `safeFileStem(title, fallback)`, `fileNameOf(path)`.
  - `entryPoints.ts`: `DumpTarget`, `DumpEntry`, `RunEntry`, `relationHasRows(kind)`, `dumpEntryForNode(profileId, engine, node)`, `dumpEntryForTable(profileId, engine, path, filter)`, `runEntryForNode(profileId, engine, node | null)`, `runEntryForScript(path, profileId, unsavedTab?)`, `paletteDumpEntry(tab, profile, engine, tableFilter)`, `paletteRunEntry(tab, selectedProfileId)`.
  - `fileNames.ts`: `dumpStamp(at)`, `sqliteStem(path)`, `dumpFileName(entry, engine, sqlitePath, tables, at)`.
  - `dumpForm.ts`: `DumpContent`, `DumpForm`, `MAX_DUMP_TABLES`, `CONTENT_LABELS`, `initialDumpForm(entry, path, tables?)`, `Choice<T>`, `DumpChoices`, `dumpChoices(entry, engine, form)`, `dumpScope(entry, form)`, `dumpProblem(entry, form)`, `dumpPlanRequest(entry, form)`, `dumpCommand(entry, form, requestId)`, `dumpDialogTitle(entry)`, `dumpTitle(scope, connection, path)`, `dumpFormFromCommand(entry, command)`, `PlanLine`, `planSummary(objects)`, `planTables(objects)`.
  - `runForm.ts`: `RunMode`, `OnError`, `RunForm`, `EVERY_STATEMENT_RUNS`, `RED_CONFIRM`, `defaultMode(engine)`, `initialRunForm(entry, engine)`, `patchedForm(form, patch)`, `ModeChoice`, `RunChoices`, `runChoices(engine, inspection, form)`, `inspectionNotes(inspection)`, `fromStatementProblem(text)`, `RunGate`, `runGate(profile, engine, form, inspected)`, `runCommand(form, engine, queryContext, requestId)`, `runTitle(path, connection)`, `againEntry(job, patch)`, `numberedLines(text, firstLine)`.
  - `stores/tabStatus.ts`: `isTabDirty(tabId)`.

The tree's paths these rules read (from the adapters' `list_children`, verified while planning): PostgreSQL and SQL Server `database:D / schema:S / folder:tables / table:T` (PostgreSQL partitions nest `… / folder:partitions / partition:P`; the session's database is the path's first segment); MySQL `schema:S / folder:tables / table:T` (its database is the schema node, 06a S18); SQLite `schema:main / folder:tables / table:T` (only `main` dumps); Couchbase `bucket:B / scope:S / collection:C`.

- [ ] **Step 1: Write the failing tests** — `src/features/data-tools/entryPoints.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { EngineInfo, ObjectNode, PathSegment } from "@/lib/api/types";
import type { QueryTabState, TableTabState } from "@/stores/workspace";
import {
  dumpEntryForNode,
  dumpEntryForTable,
  paletteDumpEntry,
  paletteRunEntry,
  runEntryForNode,
  runEntryForScript,
} from "./entryPoints";
import { dumpFileName, dumpStamp, sqliteStem } from "./fileNames";

/* Where Dump… and Run SQL file… start (spec ⑥ §3.6): the scope and defaults each tree node gives, by capability. */

const engine = (name: EngineInfo["engine"], fields: Partial<EngineInfo> = {}): EngineInfo => ({
  engine: name,
  default_port: null,
  url_schemes: [],
  dialect: name === "mssql" ? "tsql" : name === "couchbase" ? "sqlpp" : name,
  per_database_connection: name === "postgres",
  cancel_aborts_transaction: name === "postgres",
  server_read_only: name !== "mssql",
  transactions: name !== "couchbase",
  query_context: name === "couchbase",
  parameters: name === "couchbase",
  documents: name === "couchbase",
  executed_plan: false,
  plan_format: null,
  row_editing: name !== "couchbase",
  orders_browse_by_key: name === "postgres",
  run_file: true,
  dump: name !== "couchbase",
  ...fields,
});
const seg = (kind: string, name: string): PathSegment => ({ kind, name });
const node = (path: PathSegment[]): ObjectNode => {
  const last = path[path.length - 1];
  return { kind: last.kind, name: last.name, path, has_children: true, system: false };
};
const PG = engine("postgres");
const MYSQL = engine("mysql");
const MSSQL = engine("mssql");
const SQLITE = engine("sqlite");
const COUCHBASE = engine("couchbase");
const P = "p1";

describe("Dump… on the tree", () => {
  it("dumps a PostgreSQL database, schema, Tables folder, table and view on the database's own session", () => {
    const db = [seg("database", "dbaguy")];
    const schema = [...db, seg("schema", "public")];
    const folder = [...schema, seg("folder", "tables")];
    expect(dumpEntryForNode(P, PG, node(db))).toEqual({ profileId: P, database: "dbaguy", target: { kind: "database", database: "dbaguy" }, filter: null });
    expect(dumpEntryForNode(P, PG, node(schema))?.target).toEqual({ kind: "schema", schema: "public" });
    expect(dumpEntryForNode(P, PG, node(folder))?.target).toEqual({ kind: "tables", schema: "public", folder });
    expect(dumpEntryForNode(P, PG, node([...folder, seg("table", "orders")]))?.target).toEqual({
      kind: "relation", schema: "public", name: "orders", relationKind: "table",
    });
    const view = node([...schema, seg("folder", "views"), seg("view", "v")]);
    expect(dumpEntryForNode(P, PG, view)?.target).toEqual({ kind: "relation", schema: "public", name: "v", relationKind: "view" });
  });

  it("dumps MySQL's database as its schema node, with no database of the session's own (06a S18)", () => {
    const schema = [seg("schema", "shop")];
    expect(dumpEntryForNode(P, MYSQL, node(schema))).toEqual({ profileId: P, database: null, target: { kind: "schema", schema: "shop" }, filter: null });
    expect(dumpEntryForNode(P, MYSQL, node([...schema, seg("folder", "tables"), seg("table", "t")]))?.target).toMatchObject({ schema: "shop", name: "t" });
  });

  it("dumps only SQLite's main database", () => {
    expect(dumpEntryForNode(P, SQLITE, node([seg("schema", "main")]))?.target).toEqual({ kind: "schema", schema: "main" });
    expect(dumpEntryForNode(P, SQLITE, node([seg("schema", "aux")]))).toBeNull();
  });

  it("offers nothing on objects that are not dumped alone, nor on an engine without dumps", () => {
    const table = [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")];
    expect(dumpEntryForNode(P, PG, node([...table, seg("folder", "columns")]))).toBeNull();
    expect(dumpEntryForNode(P, PG, node([seg("database", "d"), seg("schema", "s"), seg("folder", "functions"), seg("function", "f()")]))).toBeNull();
    expect(dumpEntryForNode(P, COUCHBASE, node([seg("bucket", "b")]))).toBeNull();
    expect(dumpEntryForNode(P, null, node(table))).toBeNull();
  });

  it("takes the table-data tab's filter for a table, never for a view", () => {
    const path = [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")];
    expect(dumpEntryForTable(P, PG, path, "  status = 'open' ")?.filter).toBe("status = 'open'");
    expect(dumpEntryForTable(P, PG, path, "   ")?.filter).toBeNull();
    const view = [seg("database", "d"), seg("schema", "s"), seg("folder", "views"), seg("view", "v")];
    expect(dumpEntryForTable(P, PG, view, "x = 1")?.filter).toBeNull();
  });
});

describe("Run SQL file… on the tree", () => {
  it("runs on the node's database, PostgreSQL's schema as the search path", () => {
    expect(runEntryForNode(P, PG, node([seg("database", "dbaguy"), seg("schema", "sales")]))).toEqual({
      profileId: P, database: "dbaguy", searchPath: "sales", queryContext: null, path: "",
    });
    expect(runEntryForNode(P, MYSQL, node([seg("schema", "shop")]))?.database).toBe("shop");
    expect(runEntryForNode(P, MSSQL, node([seg("database", "app"), seg("schema", "dbo")]))).toMatchObject({ database: "app", searchPath: null });
    expect(runEntryForNode(P, PG, null)).toEqual({ profileId: P, database: null, searchPath: null, queryContext: null, path: "" });
  });

  it("runs on Couchbase in the bucket's or scope's context", () => {
    expect(runEntryForNode(P, COUCHBASE, node([seg("bucket", "b")]))?.queryContext).toEqual({ bucket: "b" });
    expect(runEntryForNode(P, COUCHBASE, node([seg("bucket", "b"), seg("scope", "s")]))?.queryContext).toEqual({ bucket: "b", scope: "s" });
  });

  it("offers nothing on other nodes or engines without the tool", () => {
    expect(runEntryForNode(P, PG, node([seg("database", "d"), seg("schema", "s"), seg("folder", "tables")]))).toBeNull();
    expect(runEntryForNode(P, engine("postgres", { run_file: false }), null)).toBeNull();
  });

  it("runs a script on the connection chosen in the tree", () => {
    expect(runEntryForScript("/u/scripts/a.sql", null)).toEqual({ profileId: null, database: null, searchPath: null, queryContext: null, path: "/u/scripts/a.sql", note: null });
    expect(runEntryForScript("/u/scripts/a.sql", "p1", "a.sql").note).toBe("a.sql has changes not saved to this file: the file as saved runs");
  });
});

describe("the palette's entries", () => {
  const profile = { id: P, name: "Local PG", engine: "postgres", database: "dbaguy" } as never;
  const query = (fields: Partial<QueryTabState> = {}): QueryTabState => ({
    id: "t1", kind: "query", title: "Q", profileId: P, database: null, scriptPath: null, savedText: null,
    textFromFile: false, initialText: "", autocommit: true, ...fields,
  });
  const table: TableTabState = { id: "t2", kind: "table", profileId: P, title: "t", path: [seg("database", "d"), seg("schema", "s"), seg("folder", "tables"), seg("table", "t")] };

  it("dumps the active table with its filter, or the query tab's database", () => {
    expect(paletteDumpEntry(table, profile, PG, "a > 1")).toMatchObject({ database: "d", filter: "a > 1", target: { kind: "relation", name: "t" } });
    expect(paletteDumpEntry(query(), profile, PG, "")).toMatchObject({ database: "dbaguy", target: { kind: "database", database: "dbaguy" } });
    expect(paletteDumpEntry(query({ database: "analytics" }), profile, PG, "")?.database).toBe("analytics");
    expect(paletteDumpEntry(null, profile, PG, "")).toBeNull();
  });

  it("runs on the active tab's connection, else the tree's", () => {
    expect(paletteRunEntry(query({ database: "analytics" }), "other")).toMatchObject({ profileId: P, database: "analytics" });
    expect(paletteRunEntry(table, null)).toMatchObject({ profileId: P, database: "d" });
    expect(paletteRunEntry(null, "other").profileId).toBe("other");
    expect(paletteRunEntry(query({ queryContext: { bucket: "b" } }), null).queryContext).toEqual({ bucket: "b" });
  });
});

describe("a dump's file name", () => {
  const at = new Date(2026, 9, 23, 14, 5);
  it("is database_schema-or-table_stamp.sql, without what macOS does not take", () => {
    expect(dumpStamp(at)).toBe("20261023-1405");
    const schema = dumpEntryForNode(P, PG, node([seg("database", "dbaguy"), seg("schema", "public")]))!;
    expect(dumpFileName(schema, "postgres", null, 0, at)).toBe("dbaguy_public_20261023-1405.sql");
    const db = dumpEntryForNode(P, PG, node([seg("database", "dbaguy")]))!;
    expect(dumpFileName(db, "postgres", null, 0, at)).toBe("dbaguy_20261023-1405.sql");
    const mysql = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop"), seg("folder", "tables"), seg("table", "a/b:c")]))!;
    expect(dumpFileName(mysql, "mysql", null, 0, at)).toBe("shop_abc_20261023-1405.sql");
    const mysqlSchema = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop")]))!;
    expect(dumpFileName(mysqlSchema, "mysql", null, 0, at)).toBe("shop_20261023-1405.sql");
    const folder = dumpEntryForNode(P, MYSQL, node([seg("schema", "shop"), seg("folder", "tables")]))!;
    expect(dumpFileName(folder, "mysql", null, 3, at)).toBe("shop_3-tables_20261023-1405.sql");
    const sqlite = dumpEntryForNode(P, SQLITE, node([seg("schema", "main")]))!;
    expect(dumpFileName(sqlite, "sqlite", "/x/app.sqlite3", 0, at)).toBe("app_20261023-1405.sql");
    const odd = dumpEntryForNode(P, MYSQL, node([seg("schema", "a\nb")]))!;
    expect(dumpFileName(odd, "mysql", null, 0, at)).toBe("a b_20261023-1405.sql");
    expect(sqliteStem("/x/.hidden")).toBe(".hidden");
  });
});
```

and `src/features/data-tools/forms.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { EngineInfo, FileInspection, Profile } from "@/lib/api/types";
import {
  dumpChoices,
  dumpCommand,
  dumpDialogTitle,
  dumpFormFromCommand,
  dumpPlanRequest,
  dumpProblem,
  dumpScope,
  dumpTitle,
  initialDumpForm,
  MAX_DUMP_TABLES,
  planSummary,
  planTables,
} from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";
import {
  againEntry,
  EVERY_STATEMENT_RUNS,
  numberedLines,
  initialRunForm,
  inspectionNotes,
  patchedForm,
  RED_CONFIRM,
  runChoices,
  runCommand,
  runGate,
  runTitle,
} from "./runForm";

/* The two dialogs' forms (spec ⑥ §3.6): what is offered, greyed and why, and what is sent. */

const pg: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "schema", schema: "public" }, filter: null };
const table: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "relation", schema: "public", name: "orders", relationKind: "table" }, filter: "status = 'open'" };
const view: DumpEntry = { profileId: "p1", database: "dbaguy", target: { kind: "relation", schema: "public", name: "v", relationKind: "view" }, filter: null };
const folder: DumpEntry = { profileId: "p1", database: null, target: { kind: "tables", schema: "shop", folder: [] }, filter: null };

describe("the dump form", () => {
  it("offers the engine's object kinds for a schema, and only triggers for a table", () => {
    const form = initialDumpForm(pg, "/tmp/a.sql");
    expect(dumpChoices(pg, "postgres", form).kinds.map((k) => k.value)).toEqual(["views", "sequences", "types", "routines", "triggers"]);
    expect(dumpChoices(pg, "mysql", form).kinds.map((k) => k.label)).toEqual(["Views", "Routines and events", "Triggers"]);
    expect(dumpChoices(pg, "sqlite", form).kinds.map((k) => k.value)).toEqual(["views", "triggers"]);
    expect(dumpChoices(table, "postgres", initialDumpForm(table, "")).kinds.map((k) => k.value)).toEqual(["triggers"]);
    expect(dumpChoices(view, "postgres", initialDumpForm(view, "")).kinds).toEqual([]);
  });

  it("dumps a view's definition only, and greys the rest with why", () => {
    const form = initialDumpForm(view, "/tmp/v.sql");
    expect(form.content).toBe("schema");
    expect(dumpChoices(view, "postgres", form).contents.map((c) => [c.value, c.disabled])).toEqual([
      ["both", "A view has no rows of its own: its definition is dumped"],
      ["schema", null],
      ["data", "A view has no rows of its own: its definition is dumped"],
    ]);
  });

  it("sends the table tab's filter only while it is checked and rows are written, to the plan and the dump alike", () => {
    const form = initialDumpForm(table, "/tmp/o.sql");
    expect([form.onlyFiltered, dumpPlanRequest(table, form)?.filter, dumpCommand(table, form, "r").filter]).toEqual([true, "status = 'open'", "status = 'open'"]);
    const unchecked = { ...form, onlyFiltered: false };
    expect(dumpCommand(table, unchecked, "r").filter).toBeNull();
    const schemaOnly = { ...form, content: "schema" as const };
    expect([dumpChoices(table, "postgres", schemaOnly).filter, dumpCommand(table, schemaOnly, "r").filter]).toEqual([
      { shown: true, disabled: "A schema-only dump writes no rows" },
      null,
    ]);
  });

  it("drops nothing in a data-only dump, and says so", () => {
    const form = { ...initialDumpForm(pg, "/tmp/a.sql"), dropFirst: true, content: "data" as const };
    expect([dumpChoices(pg, "postgres", form).dropFirst, dumpCommand(pg, form, "r").drop_first, dumpPlanRequest(pg, form)?.drop_first]).toEqual([
      "A data-only dump creates no objects, so it drops none",
      false,
      false,
    ]);
  });

  it("dumps the Tables folder's checked tables, at least one and at most 500", () => {
    const form = initialDumpForm(folder, "/tmp/s.sql", ["a", "b"]);
    expect(dumpScope(folder, form)).toEqual({ kind: "tables", schema_name: "shop", names: ["a", "b"] });
    expect(dumpProblem(folder, { ...form, checked: [] })).toBe("Check at least one table");
    expect(dumpScope(folder, { ...form, checked: [] })).toBeNull();
    const many = Array.from({ length: MAX_DUMP_TABLES + 1 }, (_, i) => `t${i}`);
    expect(dumpProblem(folder, { ...form, checked: many })).toBe("A dump of chosen tables names at most 500: dump the schema instead");
  });

  it("needs an absolute file, and sends a path to the plan only once it is one", () => {
    expect(dumpProblem(pg, initialDumpForm(pg, ""))).toBe("Choose a file to dump into");
    expect(dumpProblem(pg, initialDumpForm(pg, "a.sql"))).toBe("Enter an absolute path (starting with /)");
    expect(dumpPlanRequest(pg, initialDumpForm(pg, "a.sql"))?.path).toBeNull();
    expect(dumpPlanRequest(pg, initialDumpForm(pg, " /tmp/a.sql "))).toEqual({
      profile_id: "p1", database: "dbaguy", scope: { kind: "schema", schema_name: "public", names: [] }, content: "both",
      objects: { views: true, sequences: true, types: true, routines: true, triggers: true }, path: "/tmp/a.sql", drop_first: false, filter: null,
    });
  });

  it("titles the dialog by what it dumps", () => {
    expect(dumpDialogTitle(pg)).toBe("Dump public to a SQL file");
    expect(dumpDialogTitle(table)).toBe("Dump public.orders to a SQL file");
    expect(dumpDialogTitle(folder)).toBe("Dump tables of shop to a SQL file");
    expect(dumpDialogTitle({ ...pg, target: { kind: "database", database: "dbaguy" } })).toBe("Dump the database dbaguy to a SQL file");
    expect(dumpDialogTitle({ ...pg, target: { kind: "relation", schema: "main", name: "t", relationKind: "table" } })).toBe("Dump t to a SQL file");
  });

  it("titles the job as the backend does", () => {
    expect(dumpTitle({ kind: "schema", schema_name: "public", names: [] }, "Local PG", "/tmp/a.sql")).toBe("Dump public of Local PG to a.sql");
    expect(dumpTitle({ kind: "tables", schema_name: "s", names: ["a", "b"] }, "X", "/b.sql")).toBe("Dump 2 tables of X to b.sql");
    expect(dumpTitle({ kind: "database", names: [] }, "X", "/b.sql")).toBe("Dump the database of X to b.sql");
  });
});

const engine = (name: EngineInfo["engine"], fields: Partial<EngineInfo> = {}) =>
  ({ engine: name, transactions: name !== "couchbase", query_context: name === "couchbase", run_file: true, ...fields }) as EngineInfo;
const entry: RunEntry = { profileId: "p1", database: null, searchPath: "sales", queryContext: null, path: "/tmp/x.sql" };
const profile = (fields: Partial<Profile> = {}) => ({ id: "p1", name: "Local PG", color: "none", read_only: false, ...fields }) as Profile;
const inspection = (warnings: string[], header = false): FileInspection =>
  ({ path: "/tmp/x.sql", size: 10, preview: "", preview_truncated: false, warnings, header: header ? ({ format: 1, engine: "postgres", lines: [] } as never) : null }) as FileInspection;

describe("the run form", () => {
  it("starts all or nothing where the engine has transactions, statement by statement on MySQL and Couchbase", () => {
    expect(initialRunForm(entry, engine("postgres")).mode).toBe("all_or_nothing");
    expect(initialRunForm(entry, engine("mysql")).mode).toBe("statements");
    expect(initialRunForm(entry, engine("couchbase")).mode).toBe("statements");
    expect(initialRunForm({ ...entry, preset: { mode: "statements", from_statement: 40 } }, engine("postgres"))).toMatchObject({ mode: "statements", fromStatement: "40" });
  });

  it("greys what does not apply, and says why", () => {
    const form = initialRunForm(entry, engine("couchbase"));
    expect(runChoices(engine("couchbase"), null, form).modes[0].disabled).toBe("This connection has no transactions: each statement takes effect on its own");
    const pgForm = initialRunForm(entry, engine("postgres"));
    expect(runChoices(engine("postgres"), null, pgForm).skip).toBe("All or nothing stops at the first error");
    expect(runChoices(engine("mysql"), null, pgForm).modes[0].note).toContain("MySQL commits around DDL");
    const cut = inspection(["The file does not end with its 'Dump complete' line: it was cut short"], true);
    expect(runChoices(engine("postgres"), cut, pgForm).modes[0].disabled).toBe("The dump is cut short: all or nothing refuses it");
    expect(runChoices(engine("postgres"), null, pgForm)).toMatchObject({ searchPath: true, databases: "database" });
    expect(runChoices(engine("mysql"), null, pgForm)).toMatchObject({ searchPath: false, databases: "schema" });
  });

  it("puts the inspection's warnings that change what a run does first (06a F2)", () => {
    const notes = inspectionNotes(inspection([
      "The file is empty",
      "The file holds only the rows of public.orders that match a filter: restoring it drops the table and replaces it with those rows",
      "This dump drops and recreates its objects",
      "Written for PostgreSQL, the target is MySQL",
    ]));
    expect(notes.prominent).toHaveLength(3);
    expect(notes.other).toEqual(["The file is empty"]);
  });

  it("asks a red connection for the box and a danger button (CR-3); a read-only one cannot run", () => {
    const form = initialRunForm(entry, engine("postgres"));
    expect(runGate(profile(), engine("postgres"), form, true)).toEqual({ red: false, blocked: null });
    expect(runGate(profile({ color: "red" }), engine("postgres"), form, true)).toEqual({ red: true, blocked: `Check “${RED_CONFIRM}” first` });
    expect(runGate(profile({ color: "red" }), engine("postgres"), { ...form, confirmed: true }, true).blocked).toBeNull();
    expect(runGate(profile({ read_only: true }), engine("postgres"), form, true).blocked).toBe("This connection is read-only: it cannot run files");
    expect(runGate(profile(), engine("postgres"), form, false).blocked).toBe("The file has not been read yet");
    expect(runGate(profile(), engine("postgres"), { ...form, fromStatement: "0" }, true).blocked).toBe("A statement number, 1 or more");
    expect(EVERY_STATEMENT_RUNS).toContain("DROP and TRUNCATE");
  });

  it("asks for the box again after a run-again action", () => {
    const form = { ...initialRunForm(entry, engine("postgres")), confirmed: true };
    expect(patchedForm(form, { mode: "statements", on_error: "stop", from_statement: 7 })).toMatchObject({ mode: "statements", fromStatement: "7", confirmed: false });
  });

  it("sends the search path to PostgreSQL only, and the context to Couchbase only", () => {
    const form = { ...initialRunForm(entry, engine("postgres")), mode: "statements" as const, onError: "continue" as const, fromStatement: " 12 " };
    expect(runCommand(form, engine("postgres"), null, "r")).toEqual({
      type: "run_file", request_id: "r", path: "/tmp/x.sql", mode: "statements", on_error: "continue", from_statement: 12, search_path: "sales", query_context: null,
    });
    expect(runCommand(form, engine("mysql"), { bucket: "b" }, "r")).toMatchObject({ search_path: null, query_context: null });
    expect(runCommand(form, engine("couchbase"), { bucket: "b" }, "r").query_context).toEqual({ bucket: "b" });
    expect(runCommand({ ...form, mode: "all_or_nothing" }, engine("postgres"), null, "r").on_error).toBe("stop");
    expect(runTitle("/tmp/x.sql", "Local PG")).toBe("Run x.sql on Local PG");
  });
});

describe("the forms' helpers", () => {
  it("sums the plan by kind in the file's order, with the tables' rows", () => {
    expect(planSummary([
      { kind: "schema", name: "public", notes: [] },
      { kind: "table", name: "public.a", rows_estimate: 10, notes: [] },
      { kind: "table", name: "public.b", rows_estimate: null, notes: [] },
      { kind: "view", name: "public.v", notes: [] },
      { kind: "table", name: "public.c", rows_estimate: 5, notes: [] },
    ])).toEqual([
      { kind: "schema", count: 1, rows: null },
      { kind: "table", count: 3, rows: 15 },
      { kind: "view", count: 1, rows: null },
    ]);
    expect(planTables([{ kind: "table", name: "a", notes: [] }, { kind: "view", name: "v", notes: [] }])).toEqual(["a"]);
  });

  it("dumps again with the job's own choices", () => {
    const command = dumpCommand(table, { ...initialDumpForm(table, "/tmp/o.sql"), dropFirst: true }, "r");
    expect(dumpFormFromCommand(table, command)).toEqual({
      content: "both", objects: { views: true, sequences: true, types: true, routines: true, triggers: true },
      dropFirst: true, onlyFiltered: true, checked: null, path: "/tmp/o.sql",
    });
  });

  it("runs again on the job's target, from where the action says", () => {
    const command = runCommand({ ...initialRunForm(entry, engine("postgres")), mode: "statements" }, engine("postgres"), null, "r");
    const job = { profileId: "p1", database: "dbaguy", command };
    expect(againEntry(job, { mode: "statements", on_error: "stop", from_statement: 9 })).toEqual({
      profileId: "p1", database: "dbaguy", searchPath: "sales", queryContext: null, path: "/tmp/x.sql",
      preset: { mode: "statements", on_error: "stop", from_statement: 9 },
    });
    expect(againEntry(job, null).preset).toEqual({ mode: "statements", on_error: "stop", from_statement: 1 });
  });

  it("numbers a file's lines from where they start, without their CR", () => {
    expect(numberedLines("a\r\nb\n", 30)).toEqual([{ number: 30, text: "a" }, { number: 31, text: "b" }]);
    expect(numberedLines("", 1)).toEqual([]);
  });
});
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `npx vitest run src/features/data-tools/entryPoints.test.ts src/features/data-tools/forms.test.ts`
Expected: FAIL (the functions do not exist yet).

- [ ] **Step 3: Implement the modules** — `src/lib/filePaths.ts`:

```ts
/*
 * Paths the data dialogs and the export take: the save and open panels in the app, a typed absolute path in browser
 * mode (the backend runs on this Mac and reads or writes the path itself).
 */

/** Browser mode's note under a path field: there is no panel, the backend takes the path as typed. */
export const BROWSER_PATH_HINT = "An absolute path on this Mac, in an existing folder; an existing file is replaced";
export const BROWSER_OPEN_HINT = "An absolute path on this Mac";

/** Why a typed path cannot be used yet; null when it is absolute. */
export function pathProblem(text: string): string | null {
  const target = text.trim();
  return target && !target.startsWith("/") ? "Enter an absolute path (starting with /)" : null;
}

/** APFS takes 255 bytes for a name; the stem leaves room for a date and an extension. */
const MAX_STEM_CHARS = 120;

/**
 * A file name's stem from a title: without what macOS does not take in a name (`/`, `:`) and without control characters
 * (a name with a line break, say), trimmed, at most MAX_STEM_CHARS; `fallback` when nothing is left.
 */
export function safeFileStem(title: string, fallback: string): string {
  const stem = [...title.replace(/[/:]/g, "").replace(/\p{Cc}/gu, " ").trim()].slice(0, MAX_STEM_CHARS).join("").trim();
  return stem || fallback;
}

/** The last part of a path ("x.sql" of "/a/b/x.sql"). */
export const fileNameOf = (path: string): string => path.split("/").pop() || path;
```

`src/features/data-tools/entryPoints.ts`:

```ts
import type { EngineInfo, ObjectNode, PathSegment, Profile, QueryContext } from "@/lib/api/types";
import type { Tab } from "@/stores/workspace";
import { relationOf } from "../table-data/filterValue";

/*
 * Where Dump… and Run SQL file… start (spec ⑥ §3.6 入口, D-06-13): a tree node, the table-data tab, a script, the
 * palette. Each gives the dialog its connection, its database and what the node means (a dump's scope, a run's
 * database, search path or query context). Pure; the engine's capabilities decide whether there is an entry at all.
 */

/** What a dump covers, as the tree names it: its scope is built from it and the dialog's choices (`dumpForm.ts`). */
export type DumpTarget =
  | { kind: "database"; database: string }
  | { kind: "schema"; schema: string }
  /** The Tables folder: the dialog lists its tables (the folder's children) and dumps the ones checked. */
  | { kind: "tables"; schema: string; folder: PathSegment[] }
  /** One table, view, materialized view or partition. */
  | { kind: "relation"; schema: string; name: string; relationKind: string };

export interface DumpEntry {
  profileId: string;
  /** The session's (and the plan's) database: PostgreSQL's and SQL Server's database node; else null (the profile's). */
  database: string | null;
  target: DumpTarget;
  /** The table-data tab's filter: "Only rows matching the filter" (one table). */
  filter: string | null;
}

export interface RunEntry {
  /** null: the dialog asks (a script, with no connection chosen yet). */
  profileId: string | null;
  /** The session's database: a database node (PostgreSQL, SQL Server) or a schema node (MySQL); null: the profile's. */
  database: string | null;
  /** PostgreSQL: the schema node's name, sent as the run's search path. */
  searchPath: string | null;
  /** Couchbase: the bucket or scope node; null: the connection's default. */
  queryContext: QueryContext | null;
  /** The file; "" until chosen. */
  path: string;
  /** "Run again…": the form starts with these. */
  preset?: { mode?: "all_or_nothing" | "statements"; on_error?: "stop" | "continue"; from_statement?: number };
  /** Said above the file (a script's tab with changes not saved to it). */
  note?: string | null;
}

const RELATION_DATA: ReadonlySet<string> = new Set(["table", "partition"]);

/** Whether a relation kind has rows a dump writes (a view, a materialized view: only its definition, spec §3.3.1). */
export const relationHasRows = (relationKind: string): boolean => RELATION_DATA.has(relationKind);

const schemaOf = (path: PathSegment[]) => path.findLast((segment) => segment.kind === "schema")?.name ?? null;
const databaseOf = (path: PathSegment[]) => (path[0]?.kind === "database" ? path[0].name : null);

/** Dump… on a tree node: a database, schema, Tables folder or relation of an engine that dumps; else null. */
export function dumpEntryForNode(profileId: string, engine: EngineInfo | null, node: ObjectNode): DumpEntry | null {
  if (!engine?.dump) return null;
  const database = databaseOf(node.path);
  const entry = (target: DumpTarget): DumpEntry => ({ profileId, database, target, filter: null });
  switch (node.kind) {
    case "database":
      return entry({ kind: "database", database: node.name });
    case "schema":
      // SQLite dumps its main database only; MySQL's database is its schema node (06a S18)
      return engine.engine === "sqlite" && node.name !== "main" ? null : entry({ kind: "schema", schema: node.name });
    case "folder": {
      const schema = schemaOf(node.path);
      // the schema's own Tables folder, never a table's (its columns, indexes …)
      const parent = node.path.at(-2);
      if (node.name !== "tables" || !schema || parent?.kind !== "schema") return null;
      if (engine.engine === "sqlite" && schema !== "main") return null;
      return entry({ kind: "tables", schema, folder: node.path });
    }
    default: {
      const relation = relationOf(node.path);
      if (!relation?.schema || (engine.engine === "sqlite" && relation.schema !== "main")) return null;
      return entry({ kind: "relation", schema: relation.schema, name: relation.name, relationKind: relation.kind });
    }
  }
}

/** Dump table… in a table-data tab: its relation, with the filter it shows (empty: none). */
export function dumpEntryForTable(
  profileId: string,
  engine: EngineInfo | null,
  path: PathSegment[],
  filter: string,
): DumpEntry | null {
  const relation = relationOf(path);
  if (!engine?.dump || !relation?.schema) return null;
  if (engine.engine === "sqlite" && relation.schema !== "main") return null;
  const target: DumpTarget = { kind: "relation", schema: relation.schema, name: relation.name, relationKind: relation.kind };
  const kept = filter.trim();
  return { profileId, database: relation.database, target, filter: kept && relationHasRows(relation.kind) ? kept : null };
}

/** Run SQL file… on a tree node (null: the connection's row) of an engine that runs files. */
export function runEntryForNode(profileId: string, engine: EngineInfo | null, node: ObjectNode | null): RunEntry | null {
  if (!engine?.run_file) return null;
  const entry = (fields: Partial<RunEntry> = {}): RunEntry => ({
    profileId,
    database: null,
    searchPath: null,
    queryContext: null,
    path: "",
    ...fields,
  });
  if (!node) return entry();
  switch (node.kind) {
    case "database":
      return entry({ database: node.name });
    case "schema":
      if (engine.engine === "mysql") return entry({ database: node.name });
      if (engine.engine === "postgres") return entry({ database: databaseOf(node.path), searchPath: node.name });
      if (engine.engine === "mssql") return entry({ database: databaseOf(node.path) });
      return entry();
    case "bucket":
      return entry({ queryContext: { bucket: node.name } });
    case "scope":
      return entry({ queryContext: { bucket: node.path[0]?.name ?? "", scope: node.name } });
    default:
      return null;
  }
}

/**
 * Run on connection… on a script: the file, and the connection selected in the tree when there is one. `unsavedTab`: a
 * tab bound to the script holds changes not saved to it (the file as saved runs, and the dialog says so).
 */
export function runEntryForScript(absolutePath: string, profileId: string | null, unsavedTab: string | null = null): RunEntry {
  const note = unsavedTab ? `${unsavedTab} has changes not saved to this file: the file as saved runs` : null;
  return { profileId, database: null, searchPath: null, queryContext: null, path: absolutePath, note };
}

/** ⌘⇧P Dump…: the active table tab's table (with its filter), else the active tab's connection as a whole. */
export function paletteDumpEntry(
  tab: Tab | null,
  profile: Profile | null,
  engine: EngineInfo | null,
  tableFilter: string,
): DumpEntry | null {
  if (!tab || !profile || !engine?.dump || tab.profileId !== profile.id) return null;
  if (tab.kind === "table") return dumpEntryForTable(profile.id, engine, tab.path, tableFilter);
  if (tab.kind !== "query") return null;
  const database = tab.database ?? ("database" in profile ? (profile.database ?? null) : null);
  switch (engine.engine) {
    case "sqlite":
      return { profileId: profile.id, database: null, target: { kind: "schema", schema: "main" }, filter: null };
    case "mysql":
      return database ? { profileId: profile.id, database: null, target: { kind: "schema", schema: database }, filter: null } : null;
    default: // PostgreSQL, SQL Server: the tab's database
      return database ? { profileId: profile.id, database, target: { kind: "database", database }, filter: null } : null;
  }
}

/** ⌘⇧P Run SQL file…: on the active tab's connection and database, else the one selected in the tree. */
export function paletteRunEntry(tab: Tab | null, selectedProfileId: string | null): RunEntry {
  const fromTab = tab && tab.profileId ? tab : null;
  const database = fromTab?.kind === "query" ? fromTab.database : fromTab ? (relationOf(fromTab.path)?.database ?? null) : null;
  const context = fromTab?.kind === "query" && typeof fromTab.queryContext === "object" ? fromTab.queryContext : null;
  return { profileId: fromTab?.profileId ?? selectedProfileId, database, searchPath: null, queryContext: context, path: "" };
}
```

`src/features/data-tools/fileNames.ts`:

```ts
import type { Engine } from "@/lib/api/types";
import { fileNameOf, safeFileStem } from "@/lib/filePaths";
import type { DumpEntry } from "./entryPoints";

/** `yyyyMMdd-HHmm` in local time. */
export function dumpStamp(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}-${pad(at.getHours())}${pad(at.getMinutes())}`;
}

/** A SQLite file's stem ("app" of "/x/app.sqlite3"): its dumps are named after it. */
export function sqliteStem(path: string): string {
  const name = fileNameOf(path);
  return name.replace(/\.[^.]+$/, "") || name;
}

/**
 * A dump's default file name (spec ⑥ §3.3.4): `<database>_<schema or table>_<yyyyMMdd-HHmm>.sql`. The database is the
 * entry's (PostgreSQL, SQL Server), MySQL's schema (it is the database: not written twice), or the SQLite file's stem
 * (`sqlitePath`); SQLite's "main" says nothing. A Tables folder says how many tables are checked.
 */
export function dumpFileName(entry: DumpEntry, engine: Engine, sqlitePath: string | null, tables: number, at: Date): string {
  const target = entry.target;
  const database =
    entry.database ??
    (engine === "sqlite" && sqlitePath ? sqliteStem(sqlitePath) : engine === "mysql" && target.kind !== "database" ? target.schema : null);
  const parts =
    target.kind === "database"
      ? [database]
      : target.kind === "schema"
        ? [database, target.schema]
        : target.kind === "relation"
          ? [database, target.name]
          : [database, target.schema, `${tables}-tables`];
  const named = parts.filter((part): part is string => !!part && part !== "main");
  const unique = named.filter((part, i) => named.indexOf(part) === i);
  return `${safeFileStem(unique.join("_"), "dump")}_${dumpStamp(at)}.sql`;
}
```

`src/features/data-tools/dumpForm.ts`:

```ts
import type { DumpCommand, DumpObjectKinds, DumpPlanObject, DumpPlanRequest, DumpScope, Engine } from "@/lib/api/types";
import { pathProblem } from "@/lib/filePaths";
import { relationHasRows, type DumpEntry } from "./entryPoints";

/*
 * The dump dialog's form (spec ⑥ §3.6 DumpDialog, R-06-2, §3.3.1): what can be chosen for this entry and engine, why the
 * rest is greyed, and the plan request and the command it makes. The plan and the dump get the same fields (06a carry:
 * `DumpPlanRequest.drop_first` and `filter`). Pure.
 */

export type DumpContent = NonNullable<DumpCommand["content"]>;

export interface DumpForm {
  content: DumpContent;
  objects: DumpObjectKinds;
  dropFirst: boolean;
  /** "Only rows matching the filter": the table-data tab's filter goes with the dump. */
  onlyFiltered: boolean;
  /** The Tables folder's tables checked; null for any other entry. */
  checked: readonly string[] | null;
  path: string;
}

/** A tables scope names at most this many (`MAX_DUMP_NAMES` in domain/data_jobs.py). */
export const MAX_DUMP_TABLES = 500;

const ALL_KINDS: DumpObjectKinds = { views: true, sequences: true, types: true, routines: true, triggers: true };

/** Each engine's kinds besides tables, as its catalog names them (06a S17: MySQL's events go with its routines, SQL Server's synonyms with its views). */
const KIND_LABELS: Record<Engine, Partial<Record<keyof DumpObjectKinds, string>>> = {
  postgres: { views: "Views and materialized views", sequences: "Sequences", types: "Types", routines: "Functions and procedures", triggers: "Triggers" },
  mysql: { views: "Views", routines: "Routines and events", triggers: "Triggers" },
  mssql: { views: "Views and synonyms", sequences: "Sequences", types: "Types", routines: "Functions and procedures", triggers: "Triggers" },
  sqlite: { views: "Views", triggers: "Triggers" },
  couchbase: {},
};

export const CONTENT_LABELS: Record<DumpContent, string> = {
  both: "Schema and data",
  schema: "Schema only",
  data: "Data only",
};
const NO_ROWS = "A view has no rows of its own: its definition is dumped";
const SCHEMA_ONLY_FILTER = "A schema-only dump writes no rows";
const DATA_ONLY_DROP = "A data-only dump creates no objects, so it drops none";

const isView = (entry: DumpEntry) => entry.target.kind === "relation" && !relationHasRows(entry.target.relationKind);

export function initialDumpForm(entry: DumpEntry, path: string, tables: readonly string[] | null = null): DumpForm {
  return {
    content: isView(entry) ? "schema" : "both",
    objects: ALL_KINDS,
    dropFirst: false,
    onlyFiltered: entry.filter !== null,
    checked: entry.target.kind === "tables" ? (tables ?? []) : null,
    path,
  };
}

export interface Choice<T> {
  value: T;
  label: string;
  /** Why it cannot be chosen; null when it can. */
  disabled: string | null;
}

export interface DumpChoices {
  contents: Choice<DumpContent>[];
  /** The object kinds the entry and engine offer (a table or the Tables folder: its triggers only). */
  kinds: Choice<keyof DumpObjectKinds>[];
  dropFirst: string | null;
  /** "Only rows matching the filter": shown only with a filter; null when it can be checked. */
  filter: { shown: boolean; disabled: string | null };
}

export function dumpChoices(entry: DumpEntry, engine: Engine, form: DumpForm): DumpChoices {
  const view = isView(entry);
  const contents = (["both", "schema", "data"] as const).map((value) => ({
    value,
    label: CONTENT_LABELS[value],
    disabled: view && value !== "schema" ? NO_ROWS : null,
  }));
  const labels = KIND_LABELS[engine];
  const wanted: (keyof DumpObjectKinds)[] =
    entry.target.kind === "relation" ? (view ? [] : ["triggers"]) : entry.target.kind === "tables" ? ["triggers"] : ["views", "sequences", "types", "routines", "triggers"];
  const kinds = wanted.flatMap((key) => (labels[key] ? [{ value: key, label: labels[key], disabled: form.content === "data" ? "A data-only dump writes no objects" : null }] : []));
  return {
    contents,
    kinds,
    dropFirst: form.content === "data" ? DATA_ONLY_DROP : null,
    filter: { shown: entry.filter !== null, disabled: form.content === "schema" ? SCHEMA_ONLY_FILTER : null },
  };
}

/** What the form dumps; null while the Tables folder has none checked. */
export function dumpScope(entry: DumpEntry, form: DumpForm): DumpScope | null {
  const target = entry.target;
  switch (target.kind) {
    case "database":
      return { kind: "database", names: [] };
    case "schema":
      return { kind: "schema", schema_name: target.schema, names: [] };
    case "relation":
      return { kind: "tables", schema_name: target.schema, names: [target.name] };
    case "tables":
      return form.checked?.length ? { kind: "tables", schema_name: target.schema, names: [...form.checked] } : null;
  }
}

/** The filter the form sends: the entry's, while "Only rows matching the filter" is checked and rows are written. */
const sentFilter = (entry: DumpEntry, form: DumpForm) =>
  entry.filter !== null && form.onlyFiltered && form.content !== "schema" ? entry.filter : null;

/** Why Dump cannot start yet; null when it can. */
export function dumpProblem(entry: DumpEntry, form: DumpForm): string | null {
  if (entry.target.kind === "tables") {
    if (!form.checked?.length) return "Check at least one table";
    if (form.checked.length > MAX_DUMP_TABLES) return `A dump of chosen tables names at most ${MAX_DUMP_TABLES}: dump the schema instead`;
  }
  if (!form.path.trim()) return "Choose a file to dump into";
  return pathProblem(form.path);
}

/** The plan's request (the same fields as the command); null while there is no scope. */
export function dumpPlanRequest(entry: DumpEntry, form: DumpForm): DumpPlanRequest | null {
  const scope = dumpScope(entry, form);
  if (!scope) return null;
  const path = form.path.trim();
  return {
    profile_id: entry.profileId,
    database: entry.database,
    scope,
    content: form.content,
    objects: form.objects,
    path: path && !pathProblem(path) ? path : null,
    drop_first: form.dropFirst && form.content !== "data",
    filter: sentFilter(entry, form),
  };
}

export function dumpCommand(entry: DumpEntry, form: DumpForm, requestId: string): DumpCommand {
  const scope = dumpScope(entry, form);
  if (!scope || dumpProblem(entry, form)) throw new Error("The dump's form is not complete");
  return {
    type: "dump",
    request_id: requestId,
    path: form.path.trim(),
    scope,
    content: form.content,
    objects: form.objects,
    drop_first: form.dropFirst && form.content !== "data",
    filter: sentFilter(entry, form),
  };
}

/** The dump dialog's title (spec ⑥ §3.6: "Dump <schema> to a SQL file"). */
export function dumpDialogTitle(entry: DumpEntry): string {
  const target = entry.target;
  const what =
    target.kind === "database"
      ? `the database ${target.database}`
      : target.kind === "schema"
        ? target.schema
        : target.kind === "tables"
          ? `tables of ${target.schema}`
          : target.schema === "main"
            ? target.name
            : `${target.schema}.${target.name}`;
  return `Dump ${what} to a SQL file`;
}

/** What the job list calls a dump (as the backend's `DumpJob.title` does). */
export function dumpTitle(scope: DumpScope, connection: string, path: string): string {
  const names = scope.names ?? [];
  const what =
    scope.kind === "database"
      ? "the database"
      : scope.kind === "schema"
        ? (scope.schema_name ?? "")
        : names.length === 1
          ? `${scope.schema_name}.${names[0]}`
          : `${names.length} tables`;
  return `Dump ${what} of ${connection} to ${path.split("/").pop() || path}`;
}

/** "Dump again…": the form as the job was sent (its path too: the file is written again, as a whole). */
export function dumpFormFromCommand(entry: DumpEntry, command: DumpCommand): DumpForm {
  const names = command.scope.names ?? [];
  return {
    content: command.content ?? "both",
    objects: command.objects ?? ALL_KINDS,
    dropFirst: command.drop_first ?? false,
    onlyFiltered: command.filter != null,
    checked: entry.target.kind === "tables" ? names : null,
    path: command.path,
  };
}

export interface PlanLine {
  kind: string;
  count: number;
  /** ≈ the rows of the tables of that kind (the catalog's statistics); null when none is known. */
  rows: number | null;
}

/** The plan's objects by kind, in the order they first appear (the file's order): "12 tables · ≈ 1,234 rows". */
export function planSummary(objects: readonly DumpPlanObject[]): PlanLine[] {
  const lines = new Map<string, PlanLine>();
  for (const object of objects) {
    const line = lines.get(object.kind) ?? { kind: object.kind, count: 0, rows: null };
    line.count += 1;
    if (object.rows_estimate != null) line.rows = (line.rows ?? 0) + object.rows_estimate;
    lines.set(object.kind, line);
  }
  return [...lines.values()];
}

/** The dump's tables in the plan's order: the job's "table k of n". */
export const planTables = (objects: readonly DumpPlanObject[]): string[] =>
  objects.filter((object) => object.kind === "table").map((object) => object.name);
```

`src/features/data-tools/runForm.ts`:

```ts
import type { EngineInfo, FileInspection, Profile, QueryContext, RunFileCommand } from "@/lib/api/types";
import { fileNameOf, pathProblem } from "@/lib/filePaths";
import type { RunEntry } from "./entryPoints";
import type { RunPatch } from "./jobReport";

/*
 * Run SQL file's form (spec ⑥ §3.6 RunFileDialog, R-06-6, R-06-7, §12 CR-3): the modes the engine and the file allow
 * and why the others are greyed, what the inspection says first, the confirmation a red connection asks for, and the
 * command. A run is not classified statement by statement (R-06-6), so the dialog says that every statement runs.
 */

export type RunMode = RunFileCommand["mode"];
export type OnError = NonNullable<RunFileCommand["on_error"]>;

export interface RunForm {
  profileId: string | null;
  database: string | null;
  /** PostgreSQL: one schema; "" for none. */
  searchPath: string;
  path: string;
  mode: RunMode;
  onError: OnError;
  /** "Start at statement N", as typed. */
  fromStatement: string;
  /** CR-3: a red connection's run waits for this box. */
  confirmed: boolean;
}

/** CR-3: always said, because a run is not checked statement by statement (R-06-6). */
export const EVERY_STATEMENT_RUNS =
  "Every statement in the file runs as written, DROP and TRUNCATE included: DbaGuy does not check them one by one.";
export const RED_CONFIRM = "I've checked the file and the connection";

/** The backend's texts the dialog looks for in an inspection (sql/dump_header.py, services/data_service.py). */
const FILTERED_ROWS = "The file holds only the rows of ";
const DROPS_FIRST = "This dump drops and recreates its objects";
const OTHER_ENGINE = "Written for ";
const CUT_SHORT = "The file does not end with its 'Dump complete' line: it was cut short";

/** The engines whose files usually run statement by statement (R-06-7: MySQL commits around DDL; Couchbase has no transactions). */
const STATEMENTS_FIRST: ReadonlySet<string> = new Set(["mysql", "couchbase"]);

export function defaultMode(engine: EngineInfo | null): RunMode {
  return !engine || STATEMENTS_FIRST.has(engine.engine) || !engine.transactions ? "statements" : "all_or_nothing";
}

export function initialRunForm(entry: RunEntry, engine: EngineInfo | null): RunForm {
  const preset = entry.preset ?? {};
  return {
    profileId: entry.profileId,
    database: entry.database,
    searchPath: entry.searchPath ?? "",
    path: entry.path,
    mode: preset.mode ?? defaultMode(engine),
    onError: preset.on_error ?? "stop",
    fromStatement: String(preset.from_statement ?? 1),
    confirmed: false,
  };
}

/** A "run again" action's changes on the form (`RunPatch`); the confirmation is asked again. */
export function patchedForm(form: RunForm, patch: RunPatch | null): RunForm {
  if (!patch) return { ...form, confirmed: false };
  return { ...form, mode: patch.mode, onError: patch.on_error, fromStatement: String(patch.from_statement), confirmed: false };
}

export interface ModeChoice {
  value: RunMode;
  label: string;
  disabled: string | null;
  /** What to know about it here (MySQL's DDL). */
  note: string | null;
}

export interface RunChoices {
  modes: ModeChoice[];
  /** "Skip errors and continue": why it cannot be chosen, or null. */
  skip: string | null;
  /** PostgreSQL takes a search path. */
  searchPath: boolean;
  /** Which nodes the database picker lists: database (PostgreSQL, SQL Server), schema (MySQL), none. */
  databases: "database" | "schema" | null;
}

export function runChoices(engine: EngineInfo | null, inspection: FileInspection | null, form: RunForm): RunChoices {
  const cutShort = !!inspection?.header && inspection.warnings.includes(CUT_SHORT);
  const noTransactions = engine && !engine.transactions;
  return {
    modes: [
      {
        value: "all_or_nothing",
        label: "All or nothing",
        disabled: noTransactions
          ? "This connection has no transactions: each statement takes effect on its own"
          : cutShort
            ? "The dump is cut short: all or nothing refuses it"
            : null,
        note:
          engine?.engine === "mysql"
            ? "MySQL commits around DDL (CREATE, ALTER, DROP…): all or nothing only fits files without it, and DbaGuy's own MySQL dumps run statement by statement."
            : null,
      },
      { value: "statements", label: "Statement by statement", disabled: null, note: null },
    ],
    skip: form.mode === "all_or_nothing" ? "All or nothing stops at the first error" : null,
    searchPath: engine?.engine === "postgres",
    databases: engine?.engine === "postgres" || engine?.engine === "mssql" ? "database" : engine?.engine === "mysql" ? "schema" : null,
  };
}

/** What the inspection says, the warnings that change what a run does first (F2: a filtered file replaces its table). */
export function inspectionNotes(inspection: FileInspection | null): { prominent: string[]; other: string[] } {
  const warnings = inspection?.warnings ?? [];
  const prominent = warnings.filter((w) => w.startsWith(FILTERED_ROWS) || w === DROPS_FIRST || w.startsWith(OTHER_ENGINE));
  return { prominent, other: warnings.filter((w) => !prominent.includes(w)) };
}

export function fromStatementProblem(text: string): string | null {
  return /^\s*[1-9][0-9]{0,9}\s*$/.test(text) ? null : "A statement number, 1 or more";
}

export interface RunGate {
  /** CR-3: a red (production) connection's Run is a danger button and needs the box checked. */
  red: boolean;
  /** Why Run cannot be pressed now; null when it can. */
  blocked: string | null;
}

export function runGate(profile: Profile | null, engine: EngineInfo | null, form: RunForm, inspected: boolean): RunGate {
  const red = profile?.color === "red";
  const blocked = !profile
    ? "Choose a connection"
    : profile.read_only
      ? "This connection is read-only: it cannot run files"
      : !engine?.run_file
        ? "This connection cannot run SQL files"
        : !form.path.trim()
          ? "Choose a file"
          : (pathProblem(form.path) ??
            (!inspected
              ? "The file has not been read yet"
              : (fromStatementProblem(form.fromStatement) ?? (red && !form.confirmed ? `Check “${RED_CONFIRM}” first` : null))));
  return { red, blocked };
}

/** The command the form sends; `queryContext` is the entry's (Couchbase), else the connection's default. */
export function runCommand(form: RunForm, engine: EngineInfo, queryContext: QueryContext | null, requestId: string): RunFileCommand {
  return {
    type: "run_file",
    request_id: requestId,
    path: form.path.trim(),
    mode: form.mode,
    on_error: form.mode === "statements" ? form.onError : "stop",
    from_statement: Number(form.fromStatement.trim()),
    search_path: engine.engine === "postgres" && form.searchPath.trim() ? form.searchPath.trim() : null,
    query_context: engine.query_context ? queryContext : null,
  };
}

/** What the job list calls a run (as the backend's `RunFileJob.title` does). */
export const runTitle = (path: string, connection: string): string => `Run ${fileNameOf(path)} on ${connection}`;

/** "Run again…": the job's own target and file, its settings changed by `patch` (null: as they were). */
export function againEntry(
  job: { profileId: string; database: string | null; command: RunFileCommand },
  patch: RunPatch | null,
): RunEntry {
  const { command } = job;
  return {
    profileId: job.profileId,
    database: job.database,
    searchPath: command.search_path ?? null,
    queryContext: command.query_context ?? null,
    path: command.path,
    preset: patch ?? { mode: command.mode, on_error: command.on_error ?? "stop", from_statement: command.from_statement ?? 1 },
  };
}

/** A file's lines, numbered from `firstLine` (an excerpt, or the inspection's first lines). */
export function numberedLines(text: string, firstLine: number): { number: number; text: string }[] {
  if (!text) return [];
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.map((line, i) => ({ number: firstLine + i, text: line.endsWith("\r") ? line.slice(0, -1) : line }));
}
```

and in `src/stores/tabStatus.ts`:

```diff
diff --git a/src/stores/tabStatus.ts b/src/stores/tabStatus.ts
index ddec529..6055f62 100644
--- a/src/stores/tabStatus.ts
+++ b/src/stores/tabStatus.ts
@@ -26,6 +26,11 @@ export function isTabBusy(tabId: string): boolean {
   return (useStatuses.getState()[tabId] ?? IDLE).busy;
 }
 
+/** Whether the tab reports unsaved changes (a script's text); for code outside React (the Scripts panel's menu). */
+export function isTabDirty(tabId: string): boolean {
+  return (useStatuses.getState()[tabId] ?? IDLE).dirty;
+}
+
 /**
  * Sets the given fields of a tab's status (undefined ones keep their value). Cheap when nothing changes, so it may be
  * called on every keystroke. Only for a mounted tab: the entry goes when it unmounts (`useReportTabStatus`).
```

- [ ] **Step 4: Run the tests, check the types and the build**

Run: `npx vitest run src/features/data-tools/ && npm run typecheck && npm run build && npm test`
Expected: all pass; no errors.

- [ ] **Step 5: Commit**

```bash
git add src/lib/filePaths.ts src/features/data-tools/fileNames.ts src/features/data-tools/entryPoints.ts \
  src/features/data-tools/entryPoints.test.ts src/features/data-tools/dumpForm.ts src/features/data-tools/runForm.ts \
  src/features/data-tools/forms.test.ts src/stores/tabStatus.ts
git commit -m "feat(data-tools): where Dump and Run SQL file start, and what each form offers and sends"
```

---

### Task 5: Jobs outside React — the runner, the store, the dialogs' store

**Suggested implementer:** Opus (send-once, close-after, reconcile).

**Files:**
- Create: `src/features/data-tools/jobRunner.ts`, `jobRunner.test.ts`, `jobsStore.ts`, `jobsStore.test.ts`, `dataDialogs.ts`
- Modify: `src/features/data-tools/jobsApi.ts` (the whole file below; Task 1 made it with `listJobs`)

**Interfaces:**
- Consumes: Task 3's `jobReducer`, `openingJob`, `listedJob`, `neverReachedBackend`, `JobEvent`, `JobCommand`, `JobKind`, `JobState`; Task 4's `DumpEntry`, `RunEntry`, `DumpForm` (types only); `SessionSocket` (`lib/api/session-socket.ts`: reconnects with backoff, `isRejected` on 4401/4404, `trySend` never queues).
- Produces:
  - `jobsApi.ts`: `openJobSession(profileId, database)`, `closeJobSession(sessionId)` (404 is fine), `cancelJobByRest(sessionId, requestId)`, `listJobs(timeoutMs?)`, `clearFinishedJobsOnBackend()`, `revealFile(path)`, `inspectRunFile(profileId, path)`, `fileExcerpt(path, line)`, `fetchDumpPlan(body, signal?)`.
  - `jobRunner.ts`: `JobSocket`, `JobTransport`, `class JobRunner(requestId, transport, report, onSession?)` with `start(profileId, database, command)`, `attach(sessionId)`, `cancel()`.
  - `jobsStore.ts`: `JobRecord`, `JobsSnapshot`, `useJobs` (zustand), `backendTransport`, `setJobTransport` (tests), `NOT_LISTED_AFTER_MS`, `isRunning`, `onJobEnded(listener)`, `NewJob`, `startJob(job): string`, `retryJob(requestId): boolean`, `cancelJob(requestId)`, `Reconciled`, `reconcile(jobs, listed, now)`, `syncJobs()`, `clearFinishedJobs()`, `jobList(snapshot)`, `runningJobs(snapshot)`.
  - `dataDialogs.ts`: `DataDialogAsk`, `openDumpDialog(entry, form?)`, `openRunDialog(entry)`, `openJobDialog(requestId)`, `closeDataDialog()`, `useDataDialogAsk()`, `shownJob()`, `useJobsPopoverOpen()`, `setJobsPopoverOpen(open)`.

The runner's rules (each pinned by a test): the command goes the first time the socket opens, never on a reconnect (the backend replays the job's last progress and outcome instead, 06a `attach_emitter`); only events with the job's `request_id` count (a session-level message with `request_id ""` does not); at the job's outcome the socket and the session close (`DELETE /sessions/{id}` joins the worker, so its History record is written first); a socket the backend refuses for good (`isRejected`: the session is gone) ends the job with `session_gone`, and nothing is closed; a session that cannot open sends nothing; a Cancel before the command left keeps it from ever leaving and closes the session (`not_sent`); a Cancel goes on the open socket, else through `POST /data/jobs/cancel`; a job listed after a reload only listens, and its session closes at its outcome too.

`reconcile` (pure): a listed job this window does not know is added with what the list says (command null) — attached when it runs, its session closed when it has ended (never reused); a known job gets a `listed` event; a running job of this window that was sent `NOT_LISTED_AFTER_MS` ago or more and that the backend does not list ends `not_listed` (the backend restarted, or never got it: its list keeps every running job, only finished ones are trimmed).

- [ ] **Step 1: Write the failing tests** — `src/features/data-tools/jobRunner.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { SocketStatus } from "@/lib/api/session-socket";
import type { ClientCommand, RunFileCommand, ServerEvent, SessionInfo } from "@/lib/api/types";
import { JobRunner, type JobSocket, type JobTransport } from "./jobRunner";
import type { JobEvent } from "./jobState";

/* A job's session and socket (spec ⑥ D-06-10): the command goes once, the session closes after the outcome and is
   never reused, a refused socket ends the job, a cancel before the command left keeps it from ever leaving. */

class FakeSocket implements JobSocket {
  isRejected = false;
  open = false;
  sent: ClientCommand[] = [];
  closed = false;
  private events = new Set<(event: ServerEvent) => void>();
  private statuses = new Set<(status: SocketStatus) => void>();
  connect = async () => undefined;
  trySend(command: ClientCommand): boolean {
    if (!this.open) return false;
    this.sent.push(command);
    return true;
  }
  subscribe(listener: (event: ServerEvent) => void) {
    this.events.add(listener);
    return () => this.events.delete(listener);
  }
  onStatus(listener: (status: SocketStatus) => void) {
    this.statuses.add(listener);
    return () => this.statuses.delete(listener);
  }
  close() {
    this.closed = true;
  }
  status(status: SocketStatus, rejected = false) {
    this.open = status === "open";
    this.isRejected = rejected;
    this.statuses.forEach((listener) => listener(status));
  }
  emit(event: ServerEvent) {
    this.events.forEach((listener) => listener(event));
  }
}

class FakeTransport implements JobTransport {
  sockets: FakeSocket[] = [];
  closedSessions: string[] = [];
  cancels: [string, string][] = [];
  fail: Error | null = null;
  private resolveOpen: (() => void) | null = null;
  holdOpen = false;
  async openSession(): Promise<SessionInfo> {
    if (this.fail) throw this.fail;
    if (this.holdOpen) await new Promise<void>((resolve) => (this.resolveOpen = resolve));
    return { session_id: "s1", profile_id: "p1", database: null, server: {} as SessionInfo["server"] };
  }
  release() {
    this.resolveOpen?.();
  }
  async closeSession(sessionId: string) {
    this.closedSessions.push(sessionId);
  }
  async cancel(sessionId: string, requestId: string) {
    this.cancels.push([sessionId, requestId]);
  }
  socket(): JobSocket {
    const socket = new FakeSocket();
    this.sockets.push(socket);
    return socket;
  }
  now = () => 5;
}

const command: RunFileCommand = { type: "run_file", request_id: "r1", path: "/x.sql", mode: "statements", on_error: "stop", from_statement: 1 };
const outcome: ServerEvent = {
  type: "job_outcome", request_id: "r1", job: "run_file", outcome: "completed", cancelled: false, rows: 0, bytes: 0,
  statements: 1, objects: [], errors: [], error_count: 0, warnings: [],
};
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function started() {
  const transport = new FakeTransport();
  const events: JobEvent[] = [];
  const runner = new JobRunner("r1", transport, (event) => events.push(event));
  return { transport, events, runner, types: () => events.map((event) => event.type) };
}

describe("a job's runner", () => {
  it("sends the command once, the first time the socket opens, never on a reconnect", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.status("closed");
    socket.status("open");
    expect(socket.sent).toEqual([command]);
    expect(types()).toEqual(["socket", "sent", "socket", "socket"]);
  });

  it("closes the socket and the session once the outcome is in, and ignores what follows", async () => {
    const { transport, runner, events } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.emit({ type: "started", request_id: "r1" });
    socket.emit({ type: "message", request_id: "", severity: "warning", text: "idle transaction" });
    socket.emit({ type: "started", request_id: "other" });
    socket.emit(outcome);
    socket.emit({ type: "finished", request_id: "r1", elapsed_ms: 1, in_transaction: false, transaction_failed: false, cancelled: false, timed_out: false, skipped: false });
    expect(events.filter((event) => event.type === "server").map((event) => event.type === "server" && event.event.type)).toEqual(["started", "job_outcome"]);
    expect([socket.closed, transport.closedSessions]).toEqual([true, ["s1"]]);
  });

  it("ends a job whose socket the backend refuses: its session is gone, nothing is closed", async () => {
    const { transport, runner, types } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    socket.status("closed", true);
    expect(types()).toEqual(["socket", "sent", "session_gone"]);
    expect(transport.closedSessions).toEqual([]);
  });

  it("reports a session that could not open, and sends nothing", async () => {
    const { transport, runner, events } = started();
    transport.fail = new Error("A password is needed");
    runner.start("p1", null, command);
    await flush();
    expect(events).toEqual([{ type: "open_failed", error: transport.fail, at: 5 }]);
    expect(transport.sockets).toEqual([]);
  });

  it("never sends a command cancelled while its session opened, and closes the session", async () => {
    const { transport, runner, types } = started();
    transport.holdOpen = true;
    runner.start("p1", null, command);
    runner.cancel();
    transport.release();
    await flush();
    await flush();
    expect(types()).toEqual(["cancel_sent", "not_sent"]);
    expect([transport.sockets.length, transport.closedSessions]).toEqual([0, ["s1"]]);
  });

  it("cancels on the open socket, else through REST", async () => {
    const { transport, runner } = started();
    runner.start("p1", null, command);
    await flush();
    const socket = transport.sockets[0];
    socket.status("open");
    runner.cancel();
    expect(socket.sent).toEqual([command, { type: "cancel", request_id: "r1" }]);
    socket.status("closed");
    runner.cancel();
    expect(transport.cancels).toEqual([["s1", "r1"]]);
  });

  it("only listens to a job listed after a reload", async () => {
    const { transport, runner, types } = started();
    runner.attach("s9");
    const socket = transport.sockets[0];
    socket.status("open");
    socket.emit({ type: "job_progress", request_id: "r1", stage: "statements", rows: 0, bytes: 1, statements: 1, errors: 0 });
    socket.emit(outcome);
    expect(socket.sent).toEqual([]);
    expect(types()).toEqual(["socket", "server", "server"]);
    expect(transport.closedSessions).toEqual(["s9"]);
  });
});
```

and `src/features/data-tools/jobsStore.test.ts`:

```ts
import { beforeEach, describe, expect, it } from "vitest";
import type { JobInfo, JobOutcomeEvent, RunFileCommand, SessionInfo } from "@/lib/api/types";
import type { RunEntry } from "./entryPoints";
import type { JobSocket, JobTransport } from "./jobRunner";
import { openingJob } from "./jobState";
import {
  NOT_LISTED_AFTER_MS,
  onJobEnded,
  reconcile,
  retryJob,
  runningJobs,
  setJobTransport,
  startJob,
  useJobs,
  type JobRecord,
} from "./jobsStore";

/* This window's jobs (spec ⑥ D-06-10): the backend's list after a reload, a job it no longer has, a retry only of what
   never reached it, and one ending each. */

const outcome = (fields: Partial<JobOutcomeEvent> = {}): JobOutcomeEvent => ({
  type: "job_outcome", request_id: "r1", job: "run_file", outcome: "completed", cancelled: false, rows: 0, bytes: 0,
  statements: 1, objects: [], errors: [], error_count: 0, warnings: [], ...fields,
});
const info = (fields: Partial<JobInfo> = {}): JobInfo => ({
  session_id: "s1", request_id: "r1", job: "run_file", title: "Run x.sql on Local PG",
  profile_id: "00000000-0000-0000-0000-000000000001", started_at: "2026-10-23T01:00:00Z", ...fields,
});
const record = (fields: Partial<JobRecord> = {}): JobRecord => ({
  requestId: "r1", sessionId: "s1", kind: "run_file", profileId: "p1", database: null, title: "t", command: null,
  entry: null, tables: null, state: { ...openingJob(0), phase: "running", sent: true }, ...fields,
});

describe("reconciling with the backend's list", () => {
  it("adds what it lists that this window does not know: running ones attached, ended ones' sessions closed", () => {
    const listed = [info({ request_id: "a", session_id: "sa" }), info({ request_id: "b", session_id: "sb", outcome: outcome({ request_id: "b" }) })];
    const result = reconcile({}, listed, 100);
    expect(result.added.map((r) => [r.requestId, r.state.phase, r.command])).toEqual([["a", "running", null], ["b", "ended", null]]);
    expect([result.attach, result.close]).toEqual([[["a", "sa"]], ["sb"]]);
  });

  it("gives a known job what the list says", () => {
    const result = reconcile({ r1: record() }, [info({ outcome: outcome() })], 100);
    expect(result.events).toEqual([["r1", { type: "listed", info: info({ outcome: outcome() }), at: 100 }]]);
    expect(result.added).toEqual([]);
  });

  it("ends a running job the backend no longer has, once it was sent long enough ago", () => {
    const jobs = { r1: record({ state: { ...openingJob(0), phase: "running", sent: true } }) };
    expect(reconcile(jobs, [], NOT_LISTED_AFTER_MS - 1).events).toEqual([]);
    expect(reconcile(jobs, [], NOT_LISTED_AFTER_MS).events).toEqual([["r1", { type: "not_listed", at: NOT_LISTED_AFTER_MS }]]);
    const opening = { r1: record({ state: openingJob(0) }) };
    expect(reconcile(opening, [], NOT_LISTED_AFTER_MS * 2).events).toEqual([]);
  });
});

class QuietSocket implements JobSocket {
  isRejected = false;
  connect = async () => undefined;
  trySend = () => false;
  subscribe = () => () => undefined;
  onStatus = () => () => undefined;
  close = () => undefined;
}

describe("starting a job", () => {
  let fail: Error | null;
  const transport: JobTransport = {
    openSession: async () => {
      if (fail) throw fail;
      return { session_id: "s1", profile_id: "p1", database: null, server: {} as SessionInfo["server"] };
    },
    closeSession: async () => undefined,
    cancel: async () => undefined,
    socket: () => new QuietSocket(),
    now: () => 7,
  };
  const command: RunFileCommand = { type: "run_file", request_id: "r1", path: "/x.sql", mode: "statements", on_error: "stop", from_statement: 1 };
  const entry: RunEntry = { profileId: "p1", database: null, searchPath: null, queryContext: null, path: "/x.sql" };
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  beforeEach(() => {
    fail = null;
    setJobTransport(transport);
    useJobs.setState({ jobs: {}, order: [] });
  });

  it("lists it at once, and tells of its end once", async () => {
    const ends: string[] = [];
    const stop = onJobEnded((job) => ends.push(job.state.loss ?? "outcome"));
    fail = new Error("A password is needed");
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "Run x.sql on Local PG", command, entry });
    expect(runningJobs(useJobs.getState()).map((job) => job.requestId)).toEqual(["r1"]);
    await flush();
    expect([useJobs.getState().jobs.r1.state.loss, ends]).toEqual(["open_failed", ["open_failed"]]);
    stop();
  });

  it("starts again only a job nothing of which reached the backend", async () => {
    fail = new Error("A password is needed");
    startJob({ kind: "run_file", profileId: "p1", database: null, title: "t", command, entry });
    await flush();
    fail = null;
    expect(retryJob("r1")).toBe(true);
    expect(useJobs.getState().jobs.r1.state.phase).toBe("opening");
    await flush();
    useJobs.setState(({ jobs }) => ({ jobs: { ...jobs, r1: { ...jobs.r1, state: { ...jobs.r1.state, phase: "ended", sent: true, loss: "session_gone" } } } }));
    expect(retryJob("r1")).toBe(false);
  });
});
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `npx vitest run src/features/data-tools/jobRunner.test.ts src/features/data-tools/jobsStore.test.ts`
Expected: FAIL (cannot resolve `./jobRunner`, `./jobsStore`).

- [ ] **Step 3: The REST calls** — `src/features/data-tools/jobsApi.ts`, whole:

```ts
import { api, ApiError, unwrap } from "@/lib/api/client";
import type {
  DumpPlan,
  DumpPlanRequest,
  FileExcerpt,
  FileInspection,
  JobInfo,
  SessionInfo,
} from "@/lib/api/types";

/*
 * The data tools' REST calls (spec ⑥ §3.2, 06a appendix). A job itself is a WebSocket command on a session of its
 * own (`jobRunner.ts`); these open and close that session, list and cancel jobs, and read what a dialog shows first.
 * Plain calls: none of them is a `useQuery` that the app's QueryClient would retry.
 */

/** A session of the job's own (D-06-10): never a tab's, never reused. */
export async function openJobSession(profileId: string, database: string | null): Promise<SessionInfo> {
  return unwrap(await (await api()).POST("/sessions", { body: { profile_id: profileId, database } }));
}

/** Its job ended: the session goes (DELETE waits for the worker to finish the job's History record). Gone already: fine. */
export async function closeJobSession(sessionId: string): Promise<void> {
  try {
    unwrap(await (await api()).DELETE("/sessions/{session_id}", { params: { path: { session_id: sessionId } } }));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return;
    throw error;
  }
}

/** The WebSocket's cancel, for a job whose socket is not open: a job that has ended is left as it is. */
export async function cancelJobByRest(sessionId: string, requestId: string): Promise<void> {
  unwrap(await (await api()).POST("/data/jobs/cancel", { body: { session_id: sessionId, request_id: requestId } }));
}

/** This backend run's jobs, newest first; `timeoutMs` bounds the wait (the quit guard must never hang on it). */
export async function listJobs(timeoutMs?: number): Promise<JobInfo[]> {
  const signal = timeoutMs === undefined ? undefined : AbortSignal.timeout(timeoutMs);
  return unwrap(await (await api()).GET("/data/jobs", { signal }));
}

export async function clearFinishedJobsOnBackend(): Promise<void> {
  unwrap(await (await api()).POST("/data/jobs/clear"));
}

/** Show in Finder: only a file a completed dump of this backend run wrote (404 otherwise). */
export async function revealFile(path: string): Promise<void> {
  unwrap(await (await api()).POST("/data/reveal", { body: { path } }));
}

export async function inspectRunFile(profileId: string, path: string): Promise<FileInspection> {
  return unwrap(await (await api()).POST("/data/run-file/inspect", { body: { profile_id: profileId, path } }));
}

/** The file's lines around `line` (06b B2): where a run's error is. */
export async function fileExcerpt(path: string, line: number): Promise<FileExcerpt> {
  return unwrap(await (await api()).POST("/data/run-file/excerpt", { body: { path, line } }));
}

export async function fetchDumpPlan(body: DumpPlanRequest, signal?: AbortSignal): Promise<DumpPlan> {
  return unwrap(await (await api()).POST("/data/dump/plan", { body, signal }));
}
```

- [ ] **Step 4: The runner** — `src/features/data-tools/jobRunner.ts` (no runtime import: its tests run it on fakes):

```ts
import type { SocketStatus } from "@/lib/api/session-socket";
import type { ClientCommand, ServerEvent, SessionInfo } from "@/lib/api/types";
import type { JobCommand, JobEvent } from "./jobState";

/*
 * One job's session and socket (spec ⑥ D-06-10, R-06-15), outside React: a job outlives its dialog. A new job opens a
 * session of its own and sends its command once, the first time the socket opens, never again (a reconnect gets the
 * backend's replay of its last events instead). A job listed after a reload only listens. Once the job's outcome is
 * in, the socket and the session close (a job's session is never reused: a dump leaves its settings on it, 06a
 * carry). A socket the backend refuses (the session is gone: the backend stopped or restarted) ends the job here with
 * a loss. Everything it learns goes to `report` as a `JobEvent`.
 */

/** What the runner needs of a session's WebSocket (`SessionSocket`). */
export interface JobSocket {
  readonly isRejected: boolean;
  connect(): Promise<void>;
  trySend(command: ClientCommand): boolean;
  subscribe(listener: (event: ServerEvent) => void): () => void;
  onStatus(listener: (status: SocketStatus) => void): () => void;
  close(): void;
}

/** The backend, as the runner reaches it (`backendTransport` in jobsStore.ts; fakes in the tests). */
export interface JobTransport {
  openSession(profileId: string, database: string | null): Promise<SessionInfo>;
  closeSession(sessionId: string): Promise<void>;
  cancel(sessionId: string, requestId: string): Promise<void>;
  socket(sessionId: string): JobSocket;
  now(): number;
}

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));

const hasRequestId = (event: ServerEvent): event is Extract<ServerEvent, { request_id: string }> => "request_id" in event;

export class JobRunner {
  private socket: JobSocket | null = null;
  private sessionId: string | null = null;
  private sent = false;
  private done = false;
  /** A Cancel before the command left: it is never sent. */
  private withheld = false;

  constructor(
    readonly requestId: string,
    private readonly transport: JobTransport,
    private readonly report: (event: JobEvent) => void,
    /** The job's session, once known (the store keeps it for a cancel through REST and for the list). */
    private readonly onSession: (sessionId: string) => void = () => undefined,
  ) {}

  /** A new job: its own session, then `command` once the socket first opens. */
  start(profileId: string, database: string | null, command: JobCommand): void {
    void this.open(profileId, database, command);
  }

  /** A job the backend lists (a reloaded window): its socket only, which replays the job's last events. */
  attach(sessionId: string): void {
    this.sent = true;
    this.sessionId = sessionId;
    this.listen(sessionId, null);
  }

  /** Cancel: on the socket while it is open, else through REST; before the command left, it is never sent. */
  cancel(): void {
    if (this.done) return;
    this.report({ type: "cancel_sent", at: this.transport.now() });
    if (!this.sent) {
      this.withheld = true;
      return;
    }
    if (this.socket?.trySend({ type: "cancel", request_id: this.requestId })) return;
    if (this.sessionId) {
      this.transport
        .cancel(this.sessionId, this.requestId)
        .catch((error: unknown) => console.warn(`Job ${this.requestId}: cancel failed: ${errorMessage(error)}`));
    }
  }

  private async open(profileId: string, database: string | null, command: JobCommand): Promise<void> {
    let info: SessionInfo;
    try {
      info = await this.transport.openSession(profileId, database);
    } catch (error) {
      this.done = true;
      this.report({ type: "open_failed", error: error instanceof Error ? error : new Error(String(error)), at: this.transport.now() });
      return;
    }
    this.sessionId = info.session_id;
    this.onSession(info.session_id);
    if (this.withheld) {
      this.notSent();
      return;
    }
    this.listen(info.session_id, command);
  }

  private listen(sessionId: string, command: JobCommand | null): void {
    const socket = this.transport.socket(sessionId);
    this.socket = socket;
    socket.subscribe((event) => {
      if (this.done || !hasRequestId(event) || event.request_id !== this.requestId) return;
      this.report({ type: "server", event, at: this.transport.now() });
      if (event.type === "job_outcome") this.finish();
    });
    socket.onStatus((status) => {
      if (this.done) return;
      if (status === "open") {
        this.report({ type: "socket", open: true });
        if (command && !this.sent) this.send(socket, command);
      } else if (status === "closed") {
        if (!socket.isRejected) {
          this.report({ type: "socket", open: false });
          return;
        }
        // The backend refused the socket for good: the session is gone, and the job's outcome with it
        this.done = true;
        socket.close();
        this.report({ type: "session_gone", at: this.transport.now() });
      }
    });
    socket.connect().catch((error: unknown) => {
      console.warn(`Job ${this.requestId}: the socket could not connect: ${errorMessage(error)}`);
      if (!this.sent && !this.done) this.notSent();
    });
  }

  private send(socket: JobSocket, command: JobCommand): void {
    if (this.withheld || !socket.trySend(command)) {
      this.notSent();
      return;
    }
    this.sent = true;
    this.report({ type: "sent", at: this.transport.now() });
  }

  /** Nothing left: the session is closed (it never ran anything of this job). */
  private notSent(): void {
    this.report({ type: "not_sent", at: this.transport.now() });
    this.finish();
  }

  /** The job ended: its socket and its session go. */
  private finish(): void {
    this.done = true;
    this.socket?.close();
    const sessionId = this.sessionId;
    if (sessionId) {
      this.transport
        .closeSession(sessionId)
        .catch((error: unknown) => console.warn(`Job ${this.requestId}: its session could not be closed: ${errorMessage(error)}`));
    }
  }
}
```

- [ ] **Step 5: The store** — `src/features/data-tools/jobsStore.ts`:

```ts
import { create } from "zustand";
import { errorMessage } from "@/lib/api/client";
import { SessionSocket } from "@/lib/api/session-socket";
import type { JobInfo } from "@/lib/api/types";
import type { DumpEntry, RunEntry } from "./entryPoints";
import { JobRunner, type JobTransport } from "./jobRunner";
import {
  cancelJobByRest,
  clearFinishedJobsOnBackend,
  closeJobSession,
  listJobs,
  openJobSession,
} from "./jobsApi";
import {
  jobReducer,
  listedJob,
  neverReachedBackend,
  openingJob,
  type JobCommand,
  type JobEvent,
  type JobKind,
  type JobState,
} from "./jobState";

/*
 * This window's data-tools jobs (spec ⑥ R-06-15, D-06-10), outside React: a job outlives its dialog, and the Jobs list,
 * the dialogs, the notifications and the quit guard all read it. A job is keyed by its request id (made here, unique).
 * The backend's list (`GET /data/jobs`) is the truth after a reload or a backend restart: `syncJobs` adds what it lists
 * that this window does not know, attaches to the running ones, and ends a job it no longer has (`not_listed`).
 */

export interface JobRecord {
  requestId: string;
  sessionId: string | null;
  kind: JobKind;
  profileId: string;
  database: string | null;
  title: string;
  /** What was sent, and the dialog entry it came from: null for a job listed after a reload. */
  command: JobCommand | null;
  entry: DumpEntry | RunEntry | null;
  /** A dump's tables in the file's order (its plan), for "table k of n"; null when not known. */
  tables: readonly string[] | null;
  state: JobState;
}

export interface JobsSnapshot {
  jobs: Readonly<Record<string, JobRecord>>;
  /** Request ids, newest first. */
  order: readonly string[];
}

export const useJobs = create<JobsSnapshot>(() => ({ jobs: {}, order: [] }));

export const backendTransport: JobTransport = {
  openSession: openJobSession,
  closeSession: closeJobSession,
  cancel: cancelJobByRest,
  socket: (sessionId) => new SessionSocket(sessionId),
  now: () => Date.now(),
};

let transport: JobTransport = backendTransport;
/** Tests only: the backend the runners reach. */
export function setJobTransport(next: JobTransport): void {
  transport = next;
}

const runners = new Map<string, JobRunner>();
const endListeners = new Set<(record: JobRecord) => void>();
/** A job sent this long ago that the backend's list does not have is not there: the backend restarted (or never got it). */
export const NOT_LISTED_AFTER_MS = 10_000;

export const isRunning = (record: JobRecord): boolean => record.state.phase !== "ended";

/** Told once of each job that ends (its notification, its toast). */
export function onJobEnded(listener: (record: JobRecord) => void): () => void {
  endListeners.add(listener);
  return () => endListeners.delete(listener);
}

function put(record: JobRecord, first = false): void {
  useJobs.setState(({ jobs, order }) => ({
    jobs: { ...jobs, [record.requestId]: record },
    order: first || !(record.requestId in jobs) ? [record.requestId, ...order.filter((id) => id !== record.requestId)] : order,
  }));
}

function dispatch(requestId: string, event: JobEvent): void {
  const record = useJobs.getState().jobs[requestId];
  if (!record) return;
  const state = jobReducer(record.state, event);
  if (state === record.state) return;
  const next = { ...record, state };
  put(next);
  if (record.state.phase !== "ended" && state.phase === "ended") {
    runners.delete(requestId);
    for (const listener of endListeners) listener(next);
  }
}

function run(record: JobRecord): void {
  const runner = new JobRunner(record.requestId, transport, (event) => dispatch(record.requestId, event), (sessionId) => {
    const current = useJobs.getState().jobs[record.requestId];
    if (current) put({ ...current, sessionId });
  });
  runners.set(record.requestId, runner);
  if (record.command) runner.start(record.profileId, record.database, record.command);
}

export interface NewJob {
  kind: JobKind;
  profileId: string;
  database: string | null;
  title: string;
  command: JobCommand;
  entry: DumpEntry | RunEntry;
  tables?: readonly string[] | null;
}

/** Starts a job on a session of its own; returns its request id (the command's). */
export function startJob(job: NewJob): string {
  const record: JobRecord = { ...job, requestId: job.command.request_id, sessionId: null, tables: job.tables ?? null, state: openingJob(transport.now()) };
  put(record, true);
  run(record);
  return record.requestId;
}

/** Starts again a job nothing of which reached the backend (its session did not open, or its command never left). */
export function retryJob(requestId: string): boolean {
  const record = useJobs.getState().jobs[requestId];
  if (!record?.command || !neverReachedBackend(record.state)) return false;
  const again = { ...record, sessionId: null, state: jobReducer(record.state, { type: "retry", at: transport.now() }) };
  put(again);
  run(again);
  return true;
}

export function cancelJob(requestId: string): void {
  runners.get(requestId)?.cancel();
}

export interface Reconciled {
  /** Jobs the backend lists that this window does not know (a reload): added with what the list says. */
  added: JobRecord[];
  events: [requestId: string, event: JobEvent][];
  /** Running listed jobs to listen to. */
  attach: [requestId: string, sessionId: string][];
  /** Sessions of listed jobs that have ended: closed (never reused). */
  close: string[];
}

/** What the backend's list changes here (pure). */
export function reconcile(jobs: Readonly<Record<string, JobRecord>>, listed: readonly JobInfo[], now: number): Reconciled {
  const result: Reconciled = { added: [], events: [], attach: [], close: [] };
  const seen = new Set<string>();
  for (const info of listed) {
    seen.add(info.request_id);
    const known = jobs[info.request_id];
    if (known) {
      result.events.push([info.request_id, { type: "listed", info, at: now }]);
      continue;
    }
    result.added.push({
      requestId: info.request_id,
      sessionId: info.session_id,
      kind: info.job,
      profileId: info.profile_id,
      database: null,
      title: info.title,
      command: null,
      entry: null,
      tables: null,
      state: listedJob(info, now),
    });
    if (info.outcome) result.close.push(info.session_id);
    else result.attach.push([info.request_id, info.session_id]);
  }
  for (const record of Object.values(jobs)) {
    if (seen.has(record.requestId) || !isRunning(record) || !record.state.sent) continue;
    if (now - record.state.startedAt >= NOT_LISTED_AFTER_MS) result.events.push([record.requestId, { type: "not_listed", at: now }]);
  }
  return result;
}

/** Brings the list in line with the backend's (on start, and whenever the Jobs list opens). */
export async function syncJobs(): Promise<void> {
  let listed: JobInfo[];
  try {
    listed = await listJobs();
  } catch (error) {
    console.warn(`Could not list the jobs: ${errorMessage(error)}`);
    return;
  }
  const { added, events, attach, close } = reconcile(useJobs.getState().jobs, listed, transport.now());
  for (const record of added.toReversed()) put(record); // oldest first, so the newest ends on top
  for (const [requestId, event] of events) dispatch(requestId, event);
  for (const [requestId, sessionId] of attach) {
    const runner = new JobRunner(requestId, transport, (event) => dispatch(requestId, event));
    runners.set(requestId, runner);
    runner.attach(sessionId);
  }
  for (const sessionId of close) {
    transport.closeSession(sessionId).catch((error: unknown) => console.warn(`Could not close session ${sessionId}: ${errorMessage(error)}`));
  }
}

/** Clear finished: forgotten here and by the backend (POST /data/jobs/clear). */
export async function clearFinishedJobs(): Promise<void> {
  await clearFinishedJobsOnBackend();
  useJobs.setState(({ jobs, order }) => {
    const kept = order.filter((id) => isRunning(jobs[id]));
    return { jobs: Object.fromEntries(kept.map((id) => [id, jobs[id]])), order: kept };
  });
}

/** The jobs, newest first. */
export const jobList = ({ jobs, order }: JobsSnapshot): JobRecord[] => order.map((id) => jobs[id]);
export const runningJobs = (snapshot: JobsSnapshot): JobRecord[] => jobList(snapshot).filter(isRunning);
```

- [ ] **Step 6: The dialogs' store** — `src/features/data-tools/dataDialogs.ts`:

```ts
import { create } from "zustand";
import type { DumpForm } from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";

/*
 * Which data-tools dialog is open (one at a time, hosted by `DataToolsHost` in the app shell, so a tab that closes never
 * takes a job's dialog with it): a dump's or a run's form, or a job's view (progress, then its outcome). Starting a job
 * turns its form into the job's view; the Jobs list and a toast open a job's view again.
 */

export type DataDialogAsk =
  | { kind: "dump"; entry: DumpEntry; form: DumpForm | null; serial: number }
  | { kind: "run"; entry: RunEntry; serial: number }
  | { kind: "job"; requestId: string; serial: number };

let serial = 0;
const useAsk = create<{ ask: DataDialogAsk | null }>(() => ({ ask: null }));

/** `form`: "Dump again…" starts from the job's own choices. */
export function openDumpDialog(entry: DumpEntry, form: DumpForm | null = null): void {
  useAsk.setState({ ask: { kind: "dump", entry, form, serial: ++serial } });
}

export function openRunDialog(entry: RunEntry): void {
  useAsk.setState({ ask: { kind: "run", entry, serial: ++serial } });
}

export function openJobDialog(requestId: string): void {
  useAsk.setState({ ask: { kind: "job", requestId, serial: ++serial } });
}

export function closeDataDialog(): void {
  useAsk.setState({ ask: null });
}

export const useDataDialogAsk = (): DataDialogAsk | null => useAsk((s) => s.ask);

/** The job whose view is open now, if any (a job that ends in it needs no toast). */
export function shownJob(): string | null {
  const ask = useAsk.getState().ask;
  return ask?.kind === "job" ? ask.requestId : null;
}

/** The Jobs list's popover, opened by its button or by ⌘⇧P "Show jobs". */
const usePopover = create<{ open: boolean }>(() => ({ open: false }));
export const useJobsPopoverOpen = (): boolean => usePopover((s) => s.open);
export const setJobsPopoverOpen = (open: boolean): void => usePopover.setState({ open });
```

- [ ] **Step 7: Run the tests, check the types and the build**

Run: `npx vitest run src/features/data-tools/ && npm run typecheck && npm run build`
Expected: all pass; no errors.

- [ ] **Step 8: Commit**

```bash
git add src/features/data-tools/jobsApi.ts src/features/data-tools/jobRunner.ts src/features/data-tools/jobRunner.test.ts \
  src/features/data-tools/jobsStore.ts src/features/data-tools/jobsStore.test.ts src/features/data-tools/dataDialogs.ts
git commit -m "feat(data-tools): jobs outside React — a session of their own, the command once, the backend's list after a reload"
```

---

### Task 6: The dump dialog, a job's view, and the Dump entries

**Suggested implementer:** Opus (the first UI on the job framework; the shared job view).

**Files:**
- Create: `src/components/ui/FilePathField.tsx`, `src/features/data-tools/{DataToolsHost.tsx, DumpDialog.tsx, JobDialog.tsx, JobViews.tsx}`, `src/features/table-data/tableTabActions.ts`, `src/features/palette/dataCommands.test.ts`, `src/lib/useDebouncedValue.ts`
- Modify: `src/app/AppShell.tsx`, `src/features/grid/ExportDialog.tsx`, `src/features/explorer/Explorer.tsx`, `src/features/table-data/{TableDataTab.tsx, FilterBar.tsx}`, `src/features/palette/{commands.ts, CommandPalette.tsx, commands.test.ts}`

**Interfaces:**
- Consumes: Tasks 3–5 (`jobReport`, `progressView`, `startJob`, `retryJob`, `cancelJob`, `useJobs`, `openDumpDialog`, `openJobDialog`, `closeDataDialog`, `useDataDialogAsk`, `dumpEntryForNode`, `dumpEntryForTable`, `paletteDumpEntry`, `paletteRunEntry`, the dump form's functions, `dumpFileName`, `sqliteStem`, `fetchDumpPlan`, `revealFile`), `childrenQuery` (`features/explorer/useTreeRows`), `RecoverableError`, `isPasswordRequired`, `useProfiles`, `useEngineInfo`, `profileColor`, `EngineIcon`, `Dialog`, `Button`, `Checkbox`, `FieldRow`, `DropdownMenu`, `toast`, `useNow`, `chooseSavePath`.
- Produces:
  - `FilePathField({label?, value, onChange, onEnter?, onBlur?, choose, onChosen?, placeholder, browserHint, error?, inputRef?})` — the export, the dump and the run share it.
  - `useDebouncedValue<T>(value: T, ms: number): T` (`src/lib/useDebouncedValue.ts`).
  - `DataToolsHost()` (in `AppShell`), `DumpDialog({entry, form})`, `JobDialog({record})`, `JobProgressView({record})`, `JobReportView({record, onShowLine?})`.
  - `features/table-data/tableTabActions.ts`: `TableTabActionId = "dumpTable"`, `TableTabActions`, `readTableTabActions`, `useTableTabActions`.
  - Palette: `CommandContext.tableActions`; commands `dataTools.dump` ("Dump…"), `dataTools.runFile` ("Run SQL file…"; opens the run dialog, which renders from Task 7 on: until then `DataToolsHost` shows nothing for a run ask), `dataTools.jobs` ("Show jobs"; opens the popover of Task 8).

- [ ] **Step 1: Write the failing palette test** — `src/features/palette/dataCommands.test.ts`:

```ts
import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppSettings, EngineInfo, ProfileEntry } from "@/lib/api/types";
import type { Tab } from "@/stores/workspace";
import { enginesKey, profilesKey } from "../connections/hooks";
import { useJobs } from "../data-tools/jobsStore";
import type { TableTabActions } from "../table-data/tableTabActions";
import { COMMANDS, type CommandContext } from "./commands";

/* ⌘⇧P's data-tools commands (spec ⑥ §3.6): Dump… where the engine dumps, Run SQL file… always, Show jobs once there
   are jobs; a table tab's Dump runs its own handler (it knows its filter). */

const opened = vi.hoisted(() => ({ dump: [] as unknown[], run: [] as unknown[], jobs: [] as boolean[] }));
vi.mock("../data-tools/dataDialogs", () => ({
  openDumpDialog: (entry: unknown) => opened.dump.push(entry),
  openRunDialog: (entry: unknown) => opened.run.push(entry),
  setJobsPopoverOpen: (open: boolean) => opened.jobs.push(open),
}));
const mounted = vi.hoisted(() => ({ actions: null as TableTabActions | null }));
vi.mock("../table-data/tableTabActions", () => ({ readTableTabActions: (id: string) => (id === "table:1" ? mounted.actions : null) }));

const P = "p1";
const TABLE: Tab = { id: "table:1", kind: "table", profileId: P, path: [{ kind: "database", name: "d" }, { kind: "schema", name: "s" }, { kind: "folder", name: "tables" }, { kind: "table", name: "t" }], title: "t" };
const QUERY = { id: "query:1", kind: "query", profileId: P, database: null, title: "Q" } as unknown as Tab;

function context(activeTab: Tab | null, dump = true, tableActions: TableTabActions | null = null): CommandContext {
  const queryClient = new QueryClient();
  queryClient.setQueryData<ProfileEntry[]>(profilesKey, [{ profile: { id: P, engine: "postgres", name: "Local PG", database: "dbaguy" } } as ProfileEntry]);
  queryClient.setQueryData<EngineInfo[]>(enginesKey, [{ engine: "postgres", dump, run_file: true } as EngineInfo]);
  return { activeTab, tabActions: null, editActions: null, tableActions, settings: { loaded: true, current: {} as AppSettings, save: async (n) => n }, queryClient };
}
const command = (id: string) => COMMANDS.find((c) => c.id === id)!;

describe("the palette's data-tools commands", () => {
  beforeEach(() => {
    opened.dump = [];
    opened.run = [];
    opened.jobs = [];
    useJobs.setState({ jobs: {}, order: [] });
  });

  it("list Dump… only where the engine dumps, and dump a query tab's database", () => {
    expect(command("dataTools.dump").visible!(context(QUERY, false))).toBe(false);
    const ctx = context(QUERY);
    expect(command("dataTools.dump").enabled(ctx)).toBe(true);
    command("dataTools.dump").run(ctx);
    expect(opened.dump).toEqual([{ profileId: P, database: "dbaguy", target: { kind: "database", database: "dbaguy" }, filter: null }]);
  });

  it("run a table tab's own Dump table…, as it is when it runs", () => {
    const run = vi.fn();
    mounted.actions = { dumpTable: { enabled: true, run } };
    const ctx = context(TABLE, true, mounted.actions);
    expect(command("dataTools.dump").enabled(ctx)).toBe(true);
    command("dataTools.dump").run(ctx);
    expect([run.mock.calls.length, opened.dump]).toEqual([1, []]);
  });

  it("always offer Run SQL file… on the active tab's connection", () => {
    command("dataTools.runFile").run(context(QUERY));
    expect(opened.run).toEqual([{ profileId: P, database: null, searchPath: null, queryContext: null, path: "" }]);
  });

  it("enable Show jobs once there are jobs", () => {
    expect(command("dataTools.jobs").enabled(context(null))).toBe(false);
    useJobs.setState({ jobs: {}, order: ["r1"] });
    expect(command("dataTools.jobs").enabled(context(null))).toBe(true);
    command("dataTools.jobs").run(context(null));
    expect(opened.jobs).toEqual([true]);
  });
});
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `npx vitest run src/features/palette/dataCommands.test.ts`
Expected: FAIL (no `dataTools.*` command; `../table-data/tableTabActions` does not exist).

- [ ] **Step 3: The palette's commands and the table tab's action registry.** Create `src/features/table-data/tableTabActions.ts`:

```ts
import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";

/** A table tab's own actions besides grid editing (⌘⇧P): Dump table… with the tab's filter (spec ⑥ §3.6). */
export type TableTabActionId = "dumpTable";
export type TableTabActions = Record<TableTabActionId, TabAction>;

const registry = createTabActionRegistry<TableTabActionId>();
export const readTableTabActions = registry.read;
export const useTableTabActions = registry.use;
```

and apply:

```diff
diff --git a/src/features/palette/commands.ts b/src/features/palette/commands.ts
index b916542..7336132 100644
--- a/src/features/palette/commands.ts
+++ b/src/features/palette/commands.ts
@@ -1,15 +1,19 @@
 import type { QueryClient } from "@tanstack/react-query";
 import { toast } from "@/components/ui/Toast";
 import { errorMessage } from "@/lib/api/client";
-import type { AppSettings, EngineInfo, ProfileEntry } from "@/lib/api/types";
+import type { AppSettings, EngineInfo, Profile, ProfileEntry } from "@/lib/api/types";
 import type { TabAction } from "@/lib/tabActions";
 import { openNewQuery, useWorkspace, type Tab } from "@/stores/workspace";
 import { enginesKey, findEngineInfo, profilesKey } from "../connections/hooks";
+import { openDumpDialog, openRunDialog, setJobsPopoverOpen } from "../data-tools/dataDialogs";
+import { paletteDumpEntry, paletteRunEntry } from "../data-tools/entryPoints";
+import { useJobs } from "../data-tools/jobsStore";
 import { readTableEditActions, type TableEditActionId, type TableEditActions } from "../edits/editActions";
 import { refreshSchema } from "../explorer/refreshSchema";
 import { HISTORY_VIEW } from "../history/useHistory";
 import { readQueryTabActions, type QueryTabActionId, type QueryTabActions } from "../query/tabActions";
 import { openSettings } from "../settings/SettingsDialog";
+import { readTableTabActions, type TableTabActions } from "../table-data/tableTabActions";
 import { requestCloseTab } from "../workspace/closeGuards";
 
 /**
@@ -41,6 +45,8 @@ export interface CommandContext {
   tabActions: QueryTabActions | null;
   /** The active table tab's edit actions; null when no table tab is active. */
   editActions: TableEditActions | null;
+  /** The active table tab's other actions (Dump table…); null when no table tab is active. */
+  tableActions: TableTabActions | null;
   settings: {
     /** The stored settings are known (until then nothing may be saved over them). */
     loaded: boolean;
@@ -119,6 +125,34 @@ function editCommand(id: TableEditActionId, label: string, shortcut?: string): P
   };
 }
 
+/** The active tab's connection, from the profiles already loaded; null if unknown. */
+function activeProfile(ctx: CommandContext): Profile | null {
+  const id = ctx.activeTab?.profileId ?? null;
+  return ctx.queryClient.getQueryData<ProfileEntry[]>(profilesKey)?.find((entry) => entry.profile.id === id)?.profile ?? null;
+}
+
+/**
+ * ⌘⇧P Dump… (spec ⑥ §3.6): a table tab dumps its table through its own handler (it knows the filter it shows); a query
+ * tab dumps its database (PostgreSQL, SQL Server), its schema (MySQL) or main (SQLite). Listed for engines that dump.
+ */
+const dumpCommand: PaletteCommand = {
+  id: "dataTools.dump",
+  label: "Dump…",
+  visible: (ctx) => activeEngine(ctx)?.dump === true,
+  enabled: (ctx) =>
+    ctx.activeTab?.kind === "table"
+      ? (ctx.tableActions?.dumpTable.enabled ?? false)
+      : paletteDumpEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "") !== null,
+  run: (ctx) => {
+    if (ctx.activeTab?.kind === "table") {
+      runTabAction(readTableTabActions(ctx.activeTab.id)?.dumpTable, "Dump…");
+      return;
+    }
+    const entry = paletteDumpEntry(ctx.activeTab, activeProfile(ctx), activeEngine(ctx), "");
+    if (entry) openDumpDialog(entry);
+  },
+};
+
 /** Couchbase: its plans (Explain). */
 const takesPlans = (ctx: CommandContext) => activeEngine(ctx)?.plan_format === "couchbase";
 /** Couchbase: the plan a statement ran with (Run with profile). */
@@ -184,6 +218,20 @@ export const COMMANDS: readonly PaletteCommand[] = [
       if (profileId) void refreshActiveSchema(ctx.queryClient, profileId);
     },
   },
+  dumpCommand,
+  {
+    id: "dataTools.runFile",
+    label: "Run SQL file…",
+    enabled: always,
+    // On the active tab's connection and database, else the one selected in the tree; the dialog can change them.
+    run: (ctx) => openRunDialog(paletteRunEntry(ctx.activeTab, useWorkspace.getState().selectedProfileId)),
+  },
+  {
+    id: "dataTools.jobs",
+    label: "Show jobs",
+    enabled: () => useJobs.getState().order.length > 0,
+    run: () => setJobsPopoverOpen(true),
+  },
   {
     id: "closeTab",
     label: "Close tab",
```

```diff
diff --git a/src/features/palette/CommandPalette.tsx b/src/features/palette/CommandPalette.tsx
index 38dc88d..98b8f8e 100644
--- a/src/features/palette/CommandPalette.tsx
+++ b/src/features/palette/CommandPalette.tsx
@@ -9,6 +9,7 @@ import { useAppSettings } from "@/stores/settings";
 import { useWorkspace } from "@/stores/workspace";
 import { readTableEditActions } from "../edits/editActions";
 import { readQueryTabActions } from "../query/tabActions";
+import { readTableTabActions } from "../table-data/tableTabActions";
 import { openSettings } from "../settings/SettingsDialog";
 import { COMMANDS, SHORTCUTS, type CommandContext } from "./commands";
 import { closePalette, usePalette } from "./paletteStore";
@@ -26,6 +27,7 @@ function CommandList({ choose }: { choose: Choose }) {
     activeTab,
     tabActions: activeTab?.kind === "query" ? readQueryTabActions(activeTab.id) : null,
     editActions: activeTab?.kind === "table" ? readTableEditActions(activeTab.id) : null,
+    tableActions: activeTab?.kind === "table" ? readTableTabActions(activeTab.id) : null,
     settings: { loaded: settings.loaded, current: settings.settings, save: settings.save },
     queryClient,
   };
```

```diff
diff --git a/src/features/palette/commands.test.ts b/src/features/palette/commands.test.ts
index 92a4545..4c6df26 100644
--- a/src/features/palette/commands.test.ts
+++ b/src/features/palette/commands.test.ts
@@ -61,6 +61,7 @@ function context(activeTab: Tab | null, editActions: CommandContext["editActions
     activeTab,
     tabActions: null,
     editActions,
+    tableActions: null,
     settings: { loaded: true, current: {} as AppSettings, save: async (next) => next },
     queryClient: queryClient(rowEditing),
   };
```

Run: `npx vitest run src/features/palette/`
Expected: all pass.

- [ ] **Step 4: The shared path field, and the export on it** (DRY with the dump and the run). Create `src/components/ui/FilePathField.tsx`:

```tsx
import type { KeyboardEvent, Ref } from "react";
import { isImeKeyEvent } from "@/lib/ime";
import { canChooseFiles } from "@/lib/native";
import { Button } from "./Button";
import { FieldRow, Input } from "./Field";

/**
 * A file path: the system panel's Choose… in the app, a typed absolute path in browser mode (the backend reads or writes
 * it on this Mac). The export, the dump and Run SQL file share it. `onEnter`: Return in the field (never during an
 * input method's composition); `onChosen`: the panel gave a path (null: cancelled, nothing changes).
 */
export function FilePathField({
  label = "File",
  value,
  onChange,
  onEnter,
  onBlur,
  choose,
  onChosen,
  placeholder,
  browserHint,
  error,
  inputRef,
}: {
  label?: string;
  value: string;
  onChange: (path: string) => void;
  onEnter?: () => void;
  onBlur?: () => void;
  choose: () => Promise<string | null>;
  onChosen?: (path: string) => void;
  placeholder: string;
  browserHint: string;
  error?: string | null;
  inputRef?: Ref<HTMLInputElement>;
}) {
  const choosable = canChooseFiles();
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || isImeKeyEvent(e)) return;
    e.preventDefault();
    onEnter?.();
  };
  const pick = async () => {
    const chosen = await choose();
    if (chosen) {
      onChange(chosen);
      onChosen?.(chosen);
    }
  };
  return (
    <FieldRow label={label} hint={choosable ? undefined : browserHint} error={error}>
      <div className="flex gap-2">
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onBlur={onBlur}
          placeholder={choosable ? "Choose a file…" : placeholder}
          aria-label="File path"
          aria-invalid={!!error}
          className="font-mono"
        />
        {choosable && (
          <Button className="shrink-0" onClick={() => void pick()}>
            Choose…
          </Button>
        )}
      </div>
    </FieldRow>
  );
}
```

and move the export dialog onto it and onto `lib/filePaths.ts` / `formatElapsed` (its elapsed time now reads "2 min 05 s" instead of "2:05", V19):

```diff
diff --git a/src/features/grid/ExportDialog.tsx b/src/features/grid/ExportDialog.tsx
index 62dfbc6..1ee333d 100644
--- a/src/features/grid/ExportDialog.tsx
+++ b/src/features/grid/ExportDialog.tsx
@@ -1,16 +1,17 @@
 import { Loader2 } from "lucide-react";
-import { useEffect, useEffectEvent, useMemo, useRef, useState, type KeyboardEvent } from "react";
+import { useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
 import { useStore } from "zustand";
 import { Button } from "@/components/ui/Button";
 import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
 import { Dialog } from "@/components/ui/Dialog";
 import { Checkbox, FieldRow, Input, Select } from "@/components/ui/Field";
+import { FilePathField } from "@/components/ui/FilePathField";
 import { toast } from "@/components/ui/Toast";
 import { errorMessage } from "@/lib/api/client";
 import type { FormatKind, RowFormatOptions } from "@/lib/api/types";
-import { plural } from "@/lib/format";
-import { isImeKeyEvent } from "@/lib/ime";
-import { canChooseFiles, chooseSavePath } from "@/lib/native";
+import { BROWSER_PATH_HINT, pathProblem as typedPathProblem, safeFileStem } from "@/lib/filePaths";
+import { formatElapsed, plural } from "@/lib/format";
+import { chooseSavePath } from "@/lib/native";
 import { useNow } from "@/lib/useNow";
 import { useEngineInfo } from "../connections/hooks";
 import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
@@ -61,7 +62,6 @@ const NULL_TEXTS = [
   { value: "", label: "Empty" },
   { value: "NULL", label: "NULL" },
 ];
-const BROWSER_PATH_HINT = "An absolute path on this Mac, in an existing folder; an existing file is replaced";
 /** The backend's own placeholder when no INSERT target is known. */
 const TABLE_PLACEHOLDER = "table_name";
 /**
@@ -72,11 +72,8 @@ const CANCEL_AGAIN_MS = 1000;
 
 type Outcome = { kind: "error"; message: string; code?: string | null } | { kind: "cancelled" };
 
-/** `<title>.<ext>` without the characters macOS does not allow in a file name (`/` and `:`). */
-function defaultFileName(title: string, ext: string): string {
-  const stem = title.replace(/[/:]/g, "").trim();
-  return `${stem || "export"}.${ext}`;
-}
+/** `<title>.<ext>` without what macOS does not take in a file name (`safeFileStem`). */
+const defaultFileName = (title: string, ext: string) => `${safeFileStem(title, "export")}.${ext}`;
 
 const qualified = (table: TableName) => (table.schema ? `${table.schema}.${table.name}` : table.name);
 
@@ -98,12 +95,6 @@ function successMessage(done: ExecutionState, path: string): string {
   return reported ?? `Exported ${plural(done.progressRows ?? 0, "row")} to ${path}`;
 }
 
-function formatElapsed(ms: number): string {
-  const seconds = Math.max(0, Math.floor(ms / 1000));
-  if (seconds < 60) return `${seconds} s`;
-  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
-}
-
 function ExportProgress({ path, rows, startedAt }: { path: string; rows: number; startedAt: number }) {
   const now = useNow(500);
   return (
@@ -192,9 +183,8 @@ export function ExportDialog({
   }, [outcome]);
 
   const spec = FORMATS[format];
-  const choosable = canChooseFiles();
   const target = path.trim();
-  const pathProblem = target && !target.startsWith("/") ? "Enter an absolute path (starting with /)" : null;
+  const pathProblem = typedPathProblem(target);
   const canStart = !running && !!target && !pathProblem;
 
   /** A file name ending in the old format's extension follows the format. */
@@ -213,13 +203,13 @@ export function ExportDialog({
     dropUnsupportedFormat();
   }, [formats, format]);
 
-  const choose = async () => {
+  const choose = async (): Promise<string | null> => {
     try {
       const filters = [{ name: spec.label, extensions: [spec.ext] }];
-      const chosen = await chooseSavePath(defaultFileName(title, spec.ext), filters);
-      if (chosen) setPath(chosen); // null: the user cancelled the save panel
+      return await chooseSavePath(defaultFileName(title, spec.ext), filters); // null: the user cancelled the panel
     } catch (error) {
       setOutcome({ kind: "error", message: `Could not open the save panel: ${errorMessage(error)}` });
+      return null;
     }
   };
 
@@ -279,13 +269,6 @@ export function ExportDialog({
     onOpenChange(next);
   };
 
-  const onPathKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
-    if (e.key === "Enter" && !isImeKeyEvent(e)) {
-      e.preventDefault();
-      start();
-    }
-  };
-
   // Export keeps its slot (disabled while running), so a double click on it never lands on Cancel.
   const footer = (
     <>
@@ -373,26 +356,16 @@ export function ExportDialog({
               />
             </FieldRow>
           )}
-          <FieldRow label="File" hint={choosable || pathProblem ? undefined : BROWSER_PATH_HINT}>
-            <div className="flex gap-2">
-              <Input
-                ref={pathInput}
-                value={path}
-                onChange={(e) => setPath(e.target.value)}
-                onKeyDown={onPathKeyDown}
-                placeholder={choosable ? "Choose a file…" : `/path/to/${defaultFileName(title, spec.ext)}`}
-                aria-label="File path"
-                aria-invalid={!!pathProblem}
-                className="font-mono"
-              />
-              {choosable && (
-                <Button className="shrink-0" onClick={() => void choose()}>
-                  Choose…
-                </Button>
-              )}
-            </div>
-            {pathProblem && <p className="mt-1 text-[11px] text-danger">{pathProblem}</p>}
-          </FieldRow>
+          <FilePathField
+            inputRef={pathInput}
+            value={path}
+            onChange={setPath}
+            onEnter={start}
+            choose={choose}
+            placeholder={`/path/to/${defaultFileName(title, spec.ext)}`}
+            browserHint={BROWSER_PATH_HINT}
+            error={pathProblem}
+          />
           {outcome && <OutcomeNote outcome={outcome} />}
         </div>
       )}
```

Create `src/lib/useDebouncedValue.ts`: `export function useDebouncedValue<T>(value: T, ms: number): T` — the value as it was `ms` after it last changed (a `useState` set from a `setTimeout` in an effect on `[value, ms]`, cleared in its cleanup; the first render returns `value`).

- [ ] **Step 5: A job's view — `JobViews.tsx` and `JobDialog.tsx`.** Behaviour (exact copy in quotes):
  - `JobProgressView({record})`: `useNow(500)`; `progressView({kind, command, state}, now, record.tables)`. A spinner (`Loader2`, muted) and the headline (13px); when `percent` is not null a 4 px bar (`h-1 rounded bg-border`, the inner `bg-accent` at `percent`%); the detail (12px, muted, `tabular-nums`); the ETA (12px muted); while `state.reconnecting`: "Reconnecting to the backend… the job goes on" (warning); the last message of severity `warning` (as the export dialog shows it: `text-warning`, pre-wrap, `data-selectable`).
  - `JobReportView({record, onShowLine?})`: `jobReport(view)`. The title in its tone (`success`/`warning`/`danger` → `text-success`/`text-warning`/`text-danger`, `muted` → `text-muted`; 13px semibold); the detail (12px, pre-wrap, `data-selectable`); the error box when `report.error` (`rounded-md border border-danger/40 bg-danger/10 p-2 text-[12px]`): its label (font-medium) and code (mono, muted, after the label) on the first line, the message (pre-wrap, `data-selectable`), and — when `onShowLine` and the error has a line — a small button "Show line N"; the hint (muted); the notes (warning list). Task 7 adds the skipped errors.
  - `JobDialog({record})`: `Dialog` titled `record.title`, width 560 (dump) / 640 (run). The connection line first (the profile's engine icon, colour dot and name; red: `text-danger font-medium`). Body: while `state.phase !== "ended"` → `JobProgressView`; `loss === "open_failed"` → `RecoverableError` (profile, `state.openError`, `onRetry={() => retryJob(record.requestId)}`) under the report's title; otherwise `JobReportView`.
  - Footer while it runs: on the left (`mr-auto`, 12px muted) "Cancel writes no file" (dump), "Cancel rolls the whole run back" (all or nothing), "Cancel stops before the next statement: the ones before stay applied" (statement by statement), nothing for a listed job; "Cancel" (secondary) → `cancelJob(requestId)`, then "Cancelling…" disabled for `CANCEL_AGAIN_MS` (1 s) and clickable again after it (SQL Server's second Cancel, as the export); disabled "Committing…" while `progress.stage === "commit"`; "Close" (primary, `autoFocus`: Return closes, the job goes on).
  - Footer once it ended: one button per `report.actions` — `show_in_finder` → `revealFile(path)`, a 404 → toast info "The file is no longer there, or its job was cleared from Jobs", another error → toast error with `errorMessage`; `again` on a dump → `openDumpDialog(entry, dumpFormFromCommand(entry, command))` with the record's dump entry and command (shown only when both are there: a job listed after a reload has neither, V6) (V2); then "Close" (primary, `autoFocus`).
  - Closing (Esc, Close, the overlay) → `closeDataDialog()`; while the job runs, the first close of each job also toasts info "The dump keeps running: open it again from Jobs, at the bottom of the sidebar" / "The SQL file keeps running: open it again from Jobs, at the bottom of the sidebar" (once per request id, a module-level `Set`).

- [ ] **Step 6: The dump's form — `DumpDialog.tsx`.** `DumpDialog({entry, form})`; title `dumpDialogTitle(entry)`; width 600. In order:
  1. The connection line (as `JobDialog`), with " · database D" when `entry.database`.
  2. The Tables folder only: "Tables" with "All" and "None" (small ghost buttons) and "N of M checked"; the folder's tables from `useQuery(childrenQuery(profileId, target.folder))`, nodes of kind `table`, each a `Checkbox` with its `detail` (≈ rows) muted; the list `max-h-48 overflow-y-auto [scrollbar-gutter:stable]`; "Loading the tables…" / the error with Retry. The first time the list arrives and nothing was checked by the user, every table is checked (§3.3.1 預設全勾).
  3. "Content": three radios from `dumpChoices(...).contents`; a disabled one is greyed with its reason as `title` and, when the entry disables any, the reason once in 11px muted under the radios.
  4. "Objects": a checkbox per `kinds` (hidden when empty); disabled with its reason for a data-only dump.
  5. "Options": `Checkbox` "Drop objects first" with the hint "A DROP … IF EXISTS before each object the file creates; nothing else is dropped" (disabled with its reason); when `entry.filter`: `Checkbox` "Only rows matching the filter" and `WHERE <filter>` (mono) beside it, disabled with its reason.
  6. The plan (`useQuery`, key `["dumpPlan", body]`, `body = dumpPlanRequest(entry, {...form, path: useDebouncedValue(form.path, PLAN_PATH_DEBOUNCE_MS)})`, `queryFn: ({signal}) => fetchDumpPlan(body, signal)`, `enabled: body !== null`, `retry: false`, `gcTime: 0`): while loading "Reading the catalog…" (`animate-delayed-show`); a `password_required` error → `RecoverableError(profileId, error, refetch)`, another → its message (danger) with Retry; once read: one line per `planSummary(plan.objects)` entry, `${plural(count, kind)}` followed by ` · ≈ ${rows.toLocaleString()} rows` when `rows` is known ("12 tables · ≈ 1,234,567 rows", "2 views"), a disclosure "Show the objects" listing `kind name` and each object's `notes` (`max-h-48 overflow-y-auto [scrollbar-gutter:stable]`); "One consistent snapshot" or "Not one consistent snapshot: each table is read in its turn" (`plan.consistency`); "Not included: " + `plan.not_included` joined with " · " (muted).
  7. The plan's warnings: those that start with "The file holds only the rows of " (06a F2: a filter with Drop objects first) in a danger box above the others; the rest as a warning list. (Free space, SQLite's journal, SQL Server's per-table reads, MySQL's non-InnoDB tables, FK cycles: all the backend's own text.)
  8. `FilePathField`: `choose = () => chooseSavePath(name, [{ name: "SQL file", extensions: ["sql"] }])` with `name = dumpFileName(entry, engine, sqlitePath, form.checked?.length ?? 0, new Date())` (`sqlitePath`: the SQLite profile's `path`), placeholder `/path/to/<name>`, `browserHint={BROWSER_PATH_HINT}`, `error={pathProblem(form.path)}`, `onEnter={start}`. In browser mode the field has `autoFocus`; in the app the Choose… button does.
  - Footer: "Close" (ghost) and "Dump" (primary; disabled with `title={dumpProblem(entry, form)}` while there is one; `autoFocus` once the form has none).
  - `start()`: nothing while `dumpProblem` says something; else `requestId = crypto.randomUUID()`, `command = dumpCommand(entry, form, requestId)`, `startJob({ kind: "dump", profileId: entry.profileId, database: entry.database, title: dumpTitle(command.scope, profile.name, command.path), command, entry, tables: plan ? planTables(plan.objects) : null })`, then `openJobDialog(requestId)`.

- [ ] **Step 7: The host.** `DataToolsHost()`: `ask = useDataDialogAsk()`; `dump` → `<DumpDialog key={ask.serial} entry={ask.entry} form={ask.form} />`; `run` → nothing until Task 7; `job` → `record = useJobs((s) => s.jobs[ask.requestId])`: a missing record (cleared) closes the ask in an effect; else `<JobDialog key={ask.serial} record={record} />`. Render it in `AppShell` after `<OpenDocumentDialogHost />`.

- [ ] **Step 8: The entries.**
  - `Explorer.tsx`: a node row's menu is its data items followed by the Couchbase collection's existing items: `Dump…` when `dumpEntryForNode(row.profileId, findEngineInfo(entries, engines, row.profileId), row.node)` gives an entry (`onSelect: () => openDumpDialog(entry)`). A row with no item keeps having no `ContextMenu`, as today.
  - `TableDataTab.tsx` (`DataView`): `dumpEntry = useMemo(() => dumpEntryForTable(tab.profileId, engine, tab.path, state.filter), [...])`; `useTableTabActions(tab.id, { dumpTable: { enabled: dumpEntry !== null, run: () => dumpEntry && openDumpDialog(dumpEntry) } })`; at the toolbar's right end, after the edit bar, a `DropdownMenu` on a ghost icon button (`MoreHorizontal` 14, `aria-label="More"`, `title="More"`) with one item "Dump table…" (disabled without an entry; `title` "With the rows matching the filter, or all of them" when there is a filter). 06c adds "Copy rows to…" there.
  - `FilterBar.tsx`: placeholder `e.g. id > 100 AND name LIKE 'A%'  (one condition; Enter to apply)` and `title={FILTER_RULE}` on the input (06a carry).

- [ ] **Step 9: Check the types and the build**

Run: `npm run typecheck && npm run build && npm test`
Expected: no errors; all tests pass.

- [ ] **Step 10: Manual check list** (the coordinator, `scripts/dev.sh --web`, `http://localhost:1420`). Setup: in a "dev pg" query tab run `CREATE TABLE zz_06b_items (id int PRIMARY KEY, name text, note text); INSERT INTO zz_06b_items SELECT g, 'n' || g, CASE WHEN g % 3 = 0 THEN NULL ELSE 'x' END FROM generate_series(1, 30) g; CREATE VIEW zz_06b_v AS SELECT id FROM zz_06b_items;`. The big table (`CREATE TABLE zz_06b_big AS SELECT g AS id, repeat('x', 100) AS pad FROM generate_series(1, 2000000) g;`, about 250 MB dumped) is made right before check 9 and dropped right after check 10, so no other dump carries it and scratch stays under 1 GB.
  1. Tree → dev pg → database dbaguy → right-click → Dump…: the title "Dump the database dbaguy to a SQL file"; the connection line; the plan's lines (tables with ≈ rows, views…), "One consistent snapshot", "Not included: …"; no Choose… (browser mode) and the path hint; Dump is disabled with "Choose a file to dump into".
  2. Type `<repo>/dev/data/scratch/zz_06b_db.sql`, Return: the job's view (title "Dump the database of dev pg to zz_06b_db.sql"), progress "Writing public.… (k of n)" and rows/MB/elapsed, then "Dumped to zz_06b_db.sql" with rows, tables, size and path; Show in Finder opens Finder on the file.
  3. Schema public → Dump… → "Data only": "Drop objects first" greyed with "A data-only dump creates no objects, so it drops none"; the object kinds greyed.
  4. Tables folder → Dump…: every table checked, "N of N checked"; None → Dump disabled "Check at least one table"; check `zz_06b_items` only → Dump to `dev/data/scratch/zz_06b_items.sql`: completed, 1 table.
  5. View `zz_06b_v` → Dump…: only "Schema only" can be chosen; the others greyed with "A view has no rows of its own: its definition is dumped"; no object kinds.
  6. Open `zz_06b_items` as a table tab, filter `id < 10`, ⋯ → Dump table…: "Only rows matching the filter" checked with `WHERE id < 10`; check "Drop objects first": the red box "The file holds only the rows of public.zz_06b_items that match a filter: restoring it drops the table and replaces it with those rows". Dump to `dev/data/scratch/zz_06b_filtered.sql`: the outcome's notes repeat it.
  7. Content "Schema only" on that dialog: the filter option greyed with "A schema-only dump writes no rows".
  8. A relative path (`zz.sql`): "Enter an absolute path (starting with /)"; a path in a missing folder (`/nope/zz_06b.sql`): the plan warns "Choose a file in an existing folder to dump into: …", and Dump ends "The dump did not start" with "Not a file to dump into".
  9. Make `zz_06b_big`; dump it to `dev/data/scratch/zz_06b_big.sql` and Cancel while it writes: "Dump cancelled: nothing was written" and "A file that was already at that path is as it was."; `ls -a dev/data/scratch/` shows neither `zz_06b_big.sql` nor a `.zz_06b_big.sql.*.part`.
  10. Dump `zz_06b_big` again and press Esc while it writes: the dialog closes, the toast "The dump keeps running: open it again from Jobs, at the bottom of the sidebar"; the file appears when it is done (`ls -la`); then `rm` it and `DROP TABLE zz_06b_big;`.
  11. "dev mysql" schema dbaguy → Dump…: object kinds "Views", "Routines and events", "Triggers"; "zz_06b sqlite" main → "Views", "Triggers"; "dev mssql" database dbaguy → the warning "Not a consistent snapshot: …" (dev's SNAPSHOT is off).
  12. "dev pg read-only" → schema public → Dump…: the plan and the dump work (a dump only reads).
  13. A profile without a saved password, after a backend restart (touch a backend file: `--reload`): Dump… asks for the password once; Cancel shows the error with Retry; Retry asks again and the plan appears.
  14. ⌘⇧P "Dump…" on the `zz_06b_items` table tab opens its dialog with the filter; on a dev pg query tab, "Dump the database dbaguy to a SQL file"; on a Couchbase query tab the command is not listed.
  15. The export dialog of a query result: Choose… / the path hint as before, Return exports, the progress line reads "N s".
  16. Cleanup: `DROP VIEW zz_06b_v; DROP TABLE zz_06b_items;` and `rm` each `dev/data/scratch/zz_06b_*.sql` by exact path; `SELECT relname FROM pg_class WHERE relname LIKE 'zz\_06b\_%'` returns no row.

- [ ] **Step 11: Commit**

```bash
git add src/components/ui/FilePathField.tsx src/lib/useDebouncedValue.ts src/features/data-tools/DataToolsHost.tsx \
  src/features/data-tools/DumpDialog.tsx src/features/data-tools/JobDialog.tsx src/features/data-tools/JobViews.tsx \
  src/features/table-data/tableTabActions.ts src/features/table-data/TableDataTab.tsx src/features/table-data/FilterBar.tsx \
  src/features/grid/ExportDialog.tsx src/features/explorer/Explorer.tsx src/app/AppShell.tsx \
  src/features/palette/commands.ts src/features/palette/CommandPalette.tsx src/features/palette/commands.test.ts \
  src/features/palette/dataCommands.test.ts
git commit -m "feat(data-tools): the dump dialog, a job's view, Dump… in the tree, the table tab and ⌘⇧P"
```

---

### Task 7: The Run SQL file dialog and the Run entries

**Suggested implementer:** Opus (CR-3, the run-again flow, the error list).

**Files:**
- Create: `src/features/data-tools/{RunFileDialog.tsx, TargetPicker.tsx, FileLines.tsx}`
- Modify: `src/features/data-tools/{JobDialog.tsx, JobViews.tsx, DataToolsHost.tsx}`, `src/features/query/ConnectionPicker.tsx`, `src/features/explorer/Explorer.tsx`, `src/features/scripts/ScriptsPanel.tsx`

**Interfaces:**
- Consumes: Task 4's run form (`initialRunForm`, `runChoices`, `inspectionNotes`, `runGate`, `runCommand`, `runTitle`, `patchedForm`, `againEntry`, `numberedLines`, `EVERY_STATEMENT_RUNS`, `RED_CONFIRM`, `fromStatementProblem`, `defaultMode`), `runEntryForNode`, `runEntryForScript`, `inspectRunFile`, `fileExcerpt`, `chooseOpenPath`, `BROWSER_OPEN_HINT`, `buildFolderTree`, `isTabDirty`, `joinPath` (`lib/scriptPaths`).
- Produces:
  - `RunFileDialog({entry})`; `TargetPicker({profileId, database, onChange})`; `FileLines({text, firstLine, mark?, className?})`.
  - `ConnectionPicker.tsx` exports `profileItems(folder, current, pick, disabled?: (profile: Profile) => string | null)` (a reason disables the item and is its `title`) and `DatabasePicker({profileId, current, blockedReason?, onPick, nodeKind = "database"})` (`"schema"` lists MySQL's schema nodes; its trigger then reads "Default schema" when `current` is null). `ConnectionPicker` keeps calling both as before.
  - `JobReportView` gains the skipped errors; `JobDialog` gains "Show line N" with the excerpt and the run's `again` actions.

- [ ] **Step 1: The target picker and the lines.**
  - `ConnectionPicker.tsx`: the two exports above (no behaviour change for the query tab).
  - `TargetPicker`: the profiles whose engine has `run_file` (`useProfiles`, `useEngines`, `buildFolderTree`), a read-only one disabled with "Read-only: it cannot run files"; the trigger as the query tab's (`PickerTrigger`; a red profile on a red chip `bg-danger font-medium text-accent-fg`; the engine icon, the colour dot); "Choose a connection" with none. Beside it `DatabasePicker` with `nodeKind` from `runChoices(...).databases` (none for SQLite and Couchbase). Picking a profile calls `onChange(id, null)`; a database `onChange(profileId, name)`.
  - `FileLines`: a `role="region"` box `max-h-56 overflow-auto [scrollbar-gutter:stable] rounded-md border border-border bg-panel font-mono text-[12px]`; one row per `numberedLines(text, firstLine)`: the number (`w-14 shrink-0 select-none pr-2 text-right text-muted`) and the text (`whitespace-pre`, `data-selectable`); the `mark` row `bg-danger/15`, scrolled into view (`block: "center"`) when it mounts.

- [ ] **Step 2: The run's form — `RunFileDialog.tsx`.** `RunFileDialog({entry})`, title "Run a SQL file", width 680. State: `form` (`initialRunForm(entry, engine)`; until the user picks a mode, the mode follows `defaultMode` of the engine as it loads or changes), the inspection (`idle | reading | read | failed`, a sequence number drops a late answer), `runRef` on the Run button. In order:
  1. `entry.note` (a script's unsaved tab) in warning text.
  2. "Connection": `TargetPicker`; for PostgreSQL "Search path": an `Input` (placeholder "the connection's default", hint "One schema"); for Couchbase "Query context": "bucket B · scope S" from `entry.queryContext`, else "the connection's default".
  3. `FilePathField` (label "File", `choose = () => chooseOpenPath()`, placeholder "/path/to/file.sql", `browserHint={BROWSER_OPEN_HINT}`, `error={pathProblem(form.path)}`): Return, leaving the field after a change, and a chosen file read it (`inspectRunFile(profileId, path)`); so does a change of connection while a path is there. Return reads, never runs (V5); a read that Return started moves the focus to Run when `runGate` lets it.
  4. The inspection: "Reading the file…"; a failure: 403 → "This connection is read-only: it cannot run files", 404 → the backend's "<path> is not a file", another → its message; once read: `formatSize(size)`; a DbaGuy header's `lines` in a small muted mono block; `inspectionNotes(...).prominent` in a danger box (06a F2: "The file holds only the rows of …", "This dump drops and recreates its objects", "Written for …"); the others as a warning list; the preview `<FileLines text={preview} firstLine={1} />` and, when `preview_truncated`, "The first 200 lines" under it.
  5. "Mode": two radios from `runChoices(...).modes` (a disabled one greyed with its reason; a mode's `note` in 11px muted under it); "On error": radios "Stop at the error" / "Skip it and continue" (`skip` disables the second with its reason).
  6. "Start at statement": an `Input` (`w-24`, numeric keyboard), hint "Statements before it are read, not sent; their SET, USE and PRAGMA settings still are", error `fromStatementProblem`.
  7. `EVERY_STATEMENT_RUNS` (12px muted) always; on a red connection `Checkbox` with `RED_CONFIRM` (its label `text-danger font-medium`), bound to `form.confirmed`.
  - Footer: "Close" (ghost) and "Run" (`variant={gate.red ? "danger" : "primary"}`, disabled with `title={gate.blocked}`), `gate = runGate(profile, engine, form, inspection.status === "read")`.
  - `run()`: nothing while `gate.blocked`; `requestId = crypto.randomUUID()`, `command = runCommand(form, engine, entry.queryContext, requestId)`, `startJob({ kind: "run_file", profileId, database: form.database, title: runTitle(command.path, profile.name), command, entry: { ...entry, profileId, database: form.database, searchPath: form.searchPath || null, path: command.path } })`, `openJobDialog(requestId)`.
  - Every `Enter` handler in this dialog checks `isImeKeyEvent` first (FilePathField does; the search path and Start at statement inputs do nothing on Return).

- [ ] **Step 3: The run's outcome in the job's view.**
  - `JobReportView`: under the notes, the skipped errors (`report.skipped`): a list `max-h-40 overflow-y-auto [scrollbar-gutter:stable]`, rows "Line N · <label or code or "error">: <the message's first line>" (the full message in `title`), each with "Show" when it has a line; `moreSkipped` → "and N more errors (only the first 100 are kept)".
  - `JobDialog` (run): "Show line N" and the rows' "Show" call `fileExcerpt(outcome.path ?? command.path, line)`; the answer shows `<FileLines text={text} firstLine={first_line} mark={line} />` under the list (one at a time; "Reading the file…" meanwhile; `found: false` → "The file has fewer lines now"; a 404 → "The file is no longer there").
  - The run's `again` actions → `openRunDialog(againEntry({ profileId: record.profileId, database: record.database, command: record.command }, action.patch))` (V2): the form opens prefilled, reads the file again, and asks the red box again (`patchedForm` semantics: `confirmed` starts false).
  - `DataToolsHost`: `run` → `<RunFileDialog key={ask.serial} entry={ask.entry} />`.

- [ ] **Step 4: The entries.**
  - `Explorer.tsx`: node rows get "Run SQL file…" when `runEntryForNode(...)` gives one (database, schema, Couchbase bucket and scope nodes), after "Dump…"; the connection row gets "Run SQL file…" first (`runEntryForNode(profileId, engine, null)`). On a read-only profile the item is disabled with `title` "This connection is read-only: it cannot run files".
  - `ScriptsPanel.tsx`: a file's menu gets "Run on connection…" after Open (`separatorBefore` on New File… stays); `onSelect`: `openRunDialog(runEntryForScript(joinPath(root, node.path), selectedProfileId ?? activeTab?.profileId ?? null, dirtyTab))` with `dirtyTab` the title of a query tab bound to `node.path` that `isTabDirty`; disabled while the tree's `root` is unknown.

- [ ] **Step 5: Check the types and the build**

Run: `npm run typecheck && npm run build && npm test`
Expected: no errors; all tests pass.

- [ ] **Step 6: Manual check list** (web mode). Setup: Task 6's `zz_06b_items` statements again (not its view: the dump's DROP TABLE would fail on a view that depends on the table), then dump `zz_06b_items` with "Drop objects first" to `dev/data/scratch/zz_06b_items.sql`; write these files with the coordinator's file tool:
  - `dev/data/scratch/zz_06b_err.sql`: `CREATE TABLE zz_06b_err (id int PRIMARY KEY);\nINSERT INTO zz_06b_err VALUES (1);\nINSERT INTO zz_06b_err VALUES (1);\nINSERT INTO zz_06b_err VALUES (2);\n`
  - `dev/data/scratch/zz_06b_tx.sql`: `INSERT INTO zz_06b_err VALUES (10);\nBEGIN;\nINSERT INTO zz_06b_err VALUES (11);\nINSERT INTO zz_06b_err VALUES (10);\nINSERT INTO zz_06b_err VALUES (12);\nCOMMIT;\n`
  - `dev/data/scratch/zz_06b_sleep.sql`: twenty lines `SELECT pg_sleep(1);`
  - `dev/data/scratch/zz_06b_latin1.sql`: bytes `SELECT 1;\nSELECT '\xff';\nSELECT 3;\n`
  1. Tree → dev pg (the connection row) → Run SQL file…: the target chip, "Default database"; choose `zz_06b_items.sql` (type it, Return): the size, the DbaGuy header lines, the red box "This dump drops and recreates its objects", the numbered preview, "All or nothing" chosen, the sentence "Every statement in the file runs as written, DROP and TRUNCATE included: …"; the focus is on Run; Return runs it: "Ran zz_06b_items.sql", "N statements applied · M rows affected".
  2. The same file with the target switched to "dev mysql" (look only, do not run it): the file is read again, the red box adds "Written for PostgreSQL, the target is MySQL"; the mode is "Statement by statement" and "All or nothing" carries the MySQL note.
  3. Run `zz_06b_err.sql` on dev pg statement by statement, Stop at the error: "Stopped at line 3", "2 statements applied", the error box with the server's duplicate-key text; "Show line 3" shows lines 1–4 with line 3 marked; "Run again from line 3" and "Skip it and continue".
  4. "Skip it and continue": the form, prefilled "Statement by statement", Start at statement 4; Run: "Ran zz_06b_err.sql"; `SELECT * FROM zz_06b_err` gives 1 and 2.
  5. `DROP TABLE zz_06b_err;` then `zz_06b_err.sql` all or nothing: "Nothing was applied: the transaction was rolled back"; the table is not there.
  6. B1: recreate `zz_06b_err` (lines 1–2 of the file in a query tab) and run `zz_06b_tx.sql` statement by statement: "Stopped at line 4", the note "The file's transaction from line 2 (statement 2) was still open: it was rolled back…", and only "Run again from line 2" (no Skip); the table holds 1 and 10 only.
  7. On PG the same file with "Skip it and continue" still stops at line 4 (PG aborts the transaction: the error adds "This error aborted the file's own transaction…") with only "Run again from line 2". On "zz_06b sqlite" (create `zz_06b_err` there with lines 1–2 first), Skip it and continue: "Ran zz_06b_tx.sql with 1 error skipped", the list "Line 4 · …: UNIQUE constraint failed…" with "Show"; the table holds 1, 10, 11, 12.
  8. Edit "dev pg red" (colour red): Run is a red button, disabled with "Check “I've checked the file and the connection” first"; check the box, Run; "Run again…" from its outcome opens the form with the box unchecked.
  9. "dev pg read-only" is greyed in the target picker ("Read-only: it cannot run files"); the tree's Run SQL file… on it is disabled with its reason.
  10. Return in the path field of a fresh dialog reads the file and does not run it; a second Return runs it (non-red connection).
  11. "dev couchbase" → bucket dbaguy → scope app → Run SQL file…: "Query context: bucket dbaguy · scope app"; "All or nothing" greyed with "This connection has no transactions: …"; run a file `UPSERT INTO orders (KEY, VALUE) VALUES ("zz_check_06b", {"a": 1});\nDELETE FROM orders USE KEYS "zz_check_06b";`: "Ran …", 2 statements; `app.orders` still holds 500.
  12. Scripts panel: a script's menu → Run on connection…: the absolute path prefilled and read; with that script open and edited (unsaved) the dialog says "<tab> has changes not saved to this file: the file as saved runs".
  13. ⌘⇧P "Run SQL file…" from a dev mysql query tab: the target is dev mysql and its database.
  14. `zz_06b_latin1.sql`: the warning "Line 2 is not UTF-8 text: a run stops there"; statement by statement it stops at line 2 with "Not UTF-8 text", "1 statement applied", only "Run again from line 2" (no Skip).
  15. `zz_06b_sleep.sql` statement by statement, Cancel after a few seconds: "Cancelled at line N", the lines before applied; all or nothing: "Cancelled: nothing was applied".
  16. Cleanup: on dev pg `DROP TABLE zz_06b_err; DROP TABLE zz_06b_items;`, on "zz_06b sqlite" `DROP TABLE zz_06b_err;`, the colour of "dev pg red" back to none if it was another profile, `rm` the `dev/data/scratch/zz_06b_*` files by exact path; nothing `zz_06b` is left in PG or SQLite, and no `zz_check_06b` key in Couchbase.

- [ ] **Step 7: Commit**

```bash
git add src/features/data-tools/RunFileDialog.tsx src/features/data-tools/TargetPicker.tsx src/features/data-tools/FileLines.tsx \
  src/features/data-tools/JobDialog.tsx src/features/data-tools/JobViews.tsx src/features/data-tools/DataToolsHost.tsx \
  src/features/query/ConnectionPicker.tsx src/features/explorer/Explorer.tsx src/features/scripts/ScriptsPanel.tsx
git commit -m "feat(data-tools): the Run SQL file dialog (CR-3), its outcome with the errors' lines, Run entries"
```

---

### Task 8: Jobs in the sidebar, after a reload, and when they end

**Suggested implementer:** Sonnet.

**Files:**
- Create: `src/components/ui/Popover.tsx`, `src/features/data-tools/{JobsButton.tsx, useJobEndings.ts}`
- Modify: `src/app/Sidebar.tsx`, `src/app/AppShell.tsx`, `src/features/explorer/Explorer.tsx`

**Interfaces:**
- Consumes: `useJobs`, `jobList`, `isRunning`, `syncJobs`, `clearFinishedJobs`, `runningJobs`, `onJobEnded` (Task 5), `jobSummary`, `jobReport`, `jobNotification` (Task 3), `openJobDialog`, `shownJob`, `useJobsPopoverOpen`, `setJobsPopoverOpen` (Task 5), `notify`, `appIsFocused`, `useAppSettings`, `toast`, `useNow`, `formatElapsed`.
- Produces: `Popover({open, onOpenChange, trigger, children, className?})`, `JobsButton()`, `useJobEndings(): void`.

- [ ] **Step 1: The panel** — `src/components/ui/Popover.tsx` (radix-ui's Popover, already in `node_modules/@radix-ui/react-popover`):

```tsx
import { Popover as RadixPopover } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** A panel opened by a button (the Jobs list): the menus' look, its own content. */
export function Popover({ open, onOpenChange, trigger, children, className }: { open: boolean; onOpenChange: (open: boolean) => void; trigger: ReactNode; children: ReactNode; className?: string }) {
  return (
    <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content side="top" align="start" sideOffset={4} className={cn("z-50 rounded-lg border border-border bg-elevated shadow-xl outline-none", className)}>
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
```

- [ ] **Step 2: The Jobs button** — `JobsButton()`, at the bottom of the sidebar (`Sidebar.tsx`: after the views' container, a `shrink-0 border-t border-border` row): nothing while `useJobs((s) => s.order).length` is 0 (V3). The button (full width, `h-6 px-2 text-[12px]`, hover `bg-hover`): with running jobs a spinning `Loader2` and "N running", else `ListChecks` and "Jobs"; `title` "Data-tools jobs of this run". It opens the popover (`useJobsPopoverOpen` / `setJobsPopoverOpen`; ⌘⇧P "Show jobs" opens it too). When the popover opens: `void syncJobs()`. The popover (width 380): a header "Jobs" and "Clear finished" (ghost, disabled with no ended job; `clearFinishedJobs()`, a failure → toast error "Could not clear the finished jobs: …"); the rows (`max-h-80 overflow-y-auto [scrollbar-gutter:stable]`), newest first, each a button: an icon (`DatabaseBackup` for a dump, `FileCode` for a run, muted), the title (truncated, `title` the full one), under it `jobSummary(view, now)` in its tone's colour (muted while it runs), and at the right the elapsed time (`formatElapsed(now - startedAt)` while it runs, `formatElapsed(endedAt - startedAt)` once ended; a listed job that ended before this page load shows nothing there); a click → `openJobDialog(requestId)` and the popover closes. `useNow(1000, open)`. Selectors return `order` and `jobs` (never a new array); the rows are derived with `useMemo`.

- [ ] **Step 3: When a job ends** — `useJobEndings()`, called once in `AppShell`: it registers `onJobEnded` for the component's life (the latest settings through a ref). For each ended job: `jobNotification({kind, command, state, title}, settings.notifications.long_query_seconds, appIsFocused())` → `notify(title, body)` when it gives one; and, when `shownJob() !== requestId`, the job's own user did not cancel it (`state.cancelRequestedAt === null`) and it is not `not_sent`: `toast(kind, `${report.title} — ${record.title}`, { label: "Open", run: () => openJobDialog(requestId) })` with the kind from the tone (`success` → "success", `warning` → "warning", `danger` → "error", `muted` → "info"). In `AppShell` also `useEffect(() => { void syncJobs(); }, [])`: a reloaded window lists the backend's jobs and attaches to the running ones.

- [ ] **Step 4: A connection whose job runs is not deleted** (B13: `DELETE /profiles/{id}` closes every session of the profile, the jobs' too). In `Explorer.tsx`'s `releaseTabs`, first: the running jobs of `profileId` (`runningJobs(useJobs.getState())`); with any, `toast("warning", `Profile not deleted — ${n === 1 ? "a job is" : `${n} jobs are`} still running on it`)` and false.

- [ ] **Step 5: Check the types and the build**

Run: `npm run typecheck && npm run build && npm test`
Expected: no errors; all tests pass.

- [ ] **Step 6: Manual check list** (web mode). Setup: make `zz_06b_big` on dev pg (Task 6's statement, 2,000,000 rows). Every dump in this list writes `dev/data/scratch/zz_06b_big.sql` (each replaces the last: at most about 500 MB on disk while one is written).
  1. Dump `zz_06b_big` to `dev/data/scratch/zz_06b_big.sql` and close the dialog: the sidebar's bottom shows a spinner and "1 running"; the popover lists it with its progress line and elapsed time; a click opens its view; it ends with "Dumped to zz_06b_big.sql" and, the dialog being closed, the toast "Dumped to zz_06b_big.sql — Dump public.zz_06b_big of dev pg to zz_06b_big.sql" with Open.
  2. Start it again and reload the page (⌘R) while it writes: after the reload the Jobs button lists it (the backend's title), the progress goes on, its view opens from the popover and Cancel works ("Dump cancelled: nothing was written").
  3. After another reload the ended jobs are listed with their outcomes (no run-again action on a listed run); "Clear finished" empties the list and the button goes; `GET /data/jobs` (DevTools) returns `[]`.
  4. Settings → long_query_seconds 1; start the dump and switch to another app: a system notification "Dump finished after N s" (the browser asks for permission the first time).
  5. While a dump of `zz_06b_big` runs, delete its connection's profile (a copy of dev pg made for this check): "Profile not deleted — a job is still running on it"; after it ends the delete works.
  6. ⌘⇧P "Show jobs" opens the popover; with no job it is greyed.
  7. Restart the dev backend while the dump runs (touch `backend/dbaguy_backend/__init__.py`: `--reload`): the job ends "The backend stopped before the job reported its outcome" with "A dump that does not finish writes no file…"; no file at its path (a hidden `.part` may be left: delete it by exact name).
  8. The popover with more jobs than fit: its list scrolls with room kept for the scrollbar.
  9. Cleanup: `DROP TABLE zz_06b_big;`, the copied profile deleted, `rm` the `dev/data/scratch/zz_06b_*` files and any `.zz_06b_*.part` by exact path; nothing `zz_06b` is left.

- [ ] **Step 7: Commit**

```bash
git add src/components/ui/Popover.tsx src/features/data-tools/JobsButton.tsx src/features/data-tools/useJobEndings.ts \
  src/app/Sidebar.tsx src/app/AppShell.tsx src/features/explorer/Explorer.tsx
git commit -m "feat(data-tools): Jobs at the bottom of the sidebar, after a reload, notified when they end"
```

---

### Task 9: Measurements, the packaged app, the user's list, CLAUDE.md and the spec

**Suggested implementer:** Sonnet (with the coordinator for the packaged app's check).

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-20-06-data-tools-design.md`

- [ ] **Step 1: Both suites.** With the dev containers up and no scratch object: `npm test` (all pass) and `cd backend && .venv/bin/pytest -q` (all pass; 06a closed at 6,193 passed, plus Task 2's 16).

- [ ] **Step 2: The packaged app.** `df -h /` shows at least 5 GiB free; `npm run build:app`; `codesign --verify --deep --strict` on the `.app` passes. Record the sidecar size (06a: 49,536 KB; this plan adds two small functions: expect +5–15 KB), the JS main chunk (06a: 1,447.95 kB, gzip 469.12 kB) and its delta.

- [ ] **Step 3: Startup and memory.** `scripts/measure-startup.sh` three times (the first is the first launch after the build). Compare with 06a: cold start 1.22–1.28 s (first launch 3.52 s), `footprint` sum 183–189 MB, backend 78–81 MB. Expected: no change beyond the run-to-run spread (the jobs code runs only when a job does); a larger WebKit share is the new code, said with its size.

- [ ] **Step 4: The packaged app's own checks are the user's** (Appendix A items 1–5: ⌘Q and closing the window, the panels, Show in Finder, notifications, Zhuyin): the coordinator checks in the in-app browser only and nobody drives the native app. Note in the follow-ups that they are untested until the user's list.

- [ ] **Step 5: CLAUDE.md.** In the project's style (Chinese), using what the tasks built:
  - Repo 結構: `src-tauri/src/quit.rs`; `src/features/data-tools/` (each module's one-line role, as in the File Structure above); `src/lib/{filePaths, sqliteBusy, useDebouncedValue}.ts`; `src/components/ui/{FilePathField, Popover}.tsx`; `src/app/{quitGuard.ts, QuitGuard.tsx}`; `features/table-data/tableTabActions.ts`; backend: `POST /data/run-file/excerpt` and `JobOutcomeEvent.resume_at` in the ⑥ block's routes and events.
  - 架構重點, the ⑥ block: a new 06b paragraph — jobs outside React (`jobState` / `jobRunner` / `jobsStore`, the command once, the session closed after the outcome, never reused, the replayed outcome as the end, `reconcile` after a reload and `not_listed`), the dialogs (forms → job view, every run-again back to the form, V2), `resume_at` (B1) and the excerpt (B2), CR-3, the entries per capability, the Jobs button (V3), notifications and toasts (V11), the profile-delete guard (B13), the quit guard (B3: our own Quit item, the page's ack within 2 s, `onCloseRequested` with `core:window:allow-destroy`; jobs only, CR-5).
  - 已知限制: the Dock's Quit, logging out and shutting down bypass the guard (`terminate:`); a run listed after a reload offers no run-again (V6); the dump's "k of n" only for a job started in this window (V15); Couchbase's context comes from the node or the default, no picker (V13); per-object picking only in the Tables folder (V14); IME and the panels only on the user's list.
  - 打包版量測: the 06b line (sidecar, chunk, cold start, `footprint`, backend), and that 06a's F1 PostgreSQL run memory was still not re-measured frozen (it needs a 1,000,000-row restore: when disk allows).
  - 進度: ⑥ "進行中：…06a、06b 完成…；06c 未開始".
- [ ] **Step 6: The spec's 〔06b〕 notes**, next to the text they refine: the header's status line; §3.2's REST table (`/data/run-file/excerpt`) and `JobOutcomeEvent` (`resume_at`); §3.6 (as built: B4–B12, V1–V20 in one paragraph each where they change the text); R-06-15 and CR-5 (B3); §10 A12 (the result); §11 item 2 (06b: 9 tasks).

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-20-06-data-tools-design.md
git commit -m "docs(06b): CLAUDE.md and the spec's 06b notes; packaged measurements"
```

---

## Planning rulings

Each open decision the spec left, with why and what it costs. Labels V (05b used H and K; 06a S, J and others; B is this plan's spec issues).

| # | Ruling | Why | Cost |
|---|---|---|---|
| V1 | Jobs live in `jobsStore` (zustand, outside React), each with a `JobRunner`; there is no `useJobSession` hook. | A job outlives its dialog (R-06-15): a hook would die with its component. The send-once and close-after rules are then pure enough to test on fakes. | The dialogs read the store by request id. |
| V2 | Every action that runs something again ("Run again from line N", "Skip it and continue", "Run statement by statement…", "Run the rest statement by statement…", "Run again…", "Dump again…") opens the form prefilled; nothing reruns from the outcome's button. | Each run is confirmed where it is configured (CR-3's box is asked again); a prefilled form shows the file read again. | One more click per run-again. |
| V3 | The Jobs button shows only while this window has jobs (running or ended, until cleared). | Nothing to show before the first job; the sidebar stays as it is. | A user who never ran a job does not see it. |
| V4 | The table tab's "menu" is a ⋯ button at the end of its toolbar ("Dump table…"; 06c adds "Copy rows to…"). | The tab has no menu today; a cell's context menu is the wrong place; the toolbar holds the filter it uses. | One small button in the toolbar. |
| V5 | In Run SQL file, Return in the path field reads the file; after that read the focus moves to Run, so the next Return runs (a red connection needs its box first). | §3.6's "Return is the primary button" without a Return that runs a file nobody looked at. | Two Returns to run. |
| V6 | A job's command is kept in memory only: a job listed after a reload has `command: null` and offers no run-again. | The backend's `JobInfo` has no command; persisting commands is a feature for a rare case (a reload in the app follows a crash). | After a reload, a run-again is set up by hand ("Start at statement"). |
| V7 | A job's session is deleted at its outcome, live or replayed, and a listed ended job's session on sync. | D-06-10 and the 06a carry: never reused; `DELETE` joins the worker, so the History record is written first. | None. |
| V8 | The quit guard asks `GET /data/jobs` (1 s), not this window's store; no answer → quit. | True after a reload; a backend that cannot answer is gone and its jobs with it. | Up to 1 s before the question. |
| V9 | A running job sent 10 s ago that the backend does not list ends `not_listed` (at sync: app start, opening Jobs). | The backend lists every running job; a restart (with no rejected socket yet) or a lost command shows up honestly. | A job sent in the 10 s before a sync is left as it is until the next. |
| V10 | The inspection's preview and an error's excerpt are numbered `<div>` lines (`FileLines`), not CodeMirror. | CodeMirror's line numbers start at 1 and its base gutter cannot be offset without a second gutter; 60–200 read-only lines need no editor. | No syntax colours in the preview. |
| V11 | A job that ends while its view is not open toasts (with Open); a notification only per S8 (background, over the threshold); none for a job its user cancelled or one never sent. | R-06-15's notification, plus a toast so a closed dialog's job is not missed in front. | One toast per ended job. |
| V12 | Show in Finder is offered in browser mode too. | The backend runs `open -R` on this Mac either way (06a `/data/reveal`). | None. |
| V13 | Couchbase's query context comes from the tree node (bucket, scope) or the connection's default; no picker in the dialog. | Run SQL file on Couchbase is rare; the context picker needs its own lists. | Change the context by starting from another node. |
| V14 | Object kinds: a schema or database gets the engine's kinds; a table, a partition or the Tables folder gets Triggers only; a view none. Per-object checkboxes only in the Tables folder. | `DumpScope` names tables of one schema; a table's sequences and column types always come with it (06a); §3.3.1's 勾選的表 is the Tables folder. | No dump of "these views only" (dump each, or the schema). |
| V15 | The dump's "table k of n" comes from the plan's table order, kept on the job when it starts. | The progress event names only the current object (06a D-06-11). | A listed job shows the name without "k of n". |
| V16 | The dump plan is a `useQuery` (`retry: false`, `gcTime: 0`), its path debounced 400 ms; the inspection and the excerpt are plain calls with a sequence number. | A plan is a read the dialog keeps current; a PG plan can wait 5 s on a lock (06a M3): no automatic retry. | None. |
| V17 | ⌘⇧P: "Dump…" (listed for engines that dump; a table tab through its own handler with its filter, a query tab: its database, MySQL's schema, SQLite's main), "Run SQL file…" (always), "Show jobs". | M3; the table tab knows its filter, so the palette asks it (as for the edit commands, 05b H11). | Three more commands. |
| V18 | Cancel in a job's view: clickable again 1 s after a click (SQL Server's second Cancel drops a stuck connection, as the export); disabled at the COMMIT stage. | After COMMIT is sent a Cancel cannot reach the driver (06a). | None. |
| V19 | The export dialog moves onto `FilePathField`, `safeFileStem`, `pathProblem` and `formatElapsed` (its elapsed time reads "2 min 05 s" instead of "2:05"). | DRY: three dialogs, one path row. | A small visual change in the export. |
| V20 | The dump dialog's path is empty until chosen or typed (as the export); the save panel suggests `dumpFileName`. | The frontend does not know the user's folders; `~` is refused by the absolute-path rule. | One Choose… or a typed path. |

## Spec issues found while planning

| # | Spec text | What the code or a probe shows | Ruling |
|---|---|---|---|
| B1 | §3.6 / R-06-7: "Run again from line N" and "Skip it and continue" after a statement-by-statement stop. | 06a H2 rolls back the file's own open transaction when a run stops inside it and counts its statements out (`run_file.py` `_settle_file_transaction`, `_ended_in`, `_lost_in_file_transaction`): rerunning from the failure would skip them for good (a sqlite3 `.dump` stopped at line 5,000 would restore half its rows). `JobOutcomeEvent` has no field for where to restart; the only trace is warning text. | `JobOutcomeEvent.resume_at: StoppedAt | None` (Task 2): the earliest counted-out transaction start or the stop, None when an earlier statement's effect is not known. The frontend offers "Run again from line N" only with it, and Skip only when it equals the stop. |
| B2 | §3.6: 「錯誤清單（行號、server 原文；點了以唯讀預覽顯示那一段）」. | `FileInspection.preview` holds the first 200 lines (64 KiB); an error is usually far beyond. No route reads other lines. | `POST /data/run-file/excerpt {path, line}` → `FileExcerpt` (Task 2): 20 lines before, at most 60 lines / 64 KiB, no connection. |
| B3 | CR-5 / R-06-15 / §3.6: ⌘Q through `RunEvent::ExitRequested` and `prevent_exit()`; "predefined Quit 改成自訂項目，先發事件給前端". | The predefined Quit sends `terminate:`; tao 0.37.1 has no `applicationShouldTerminate`, so ⌘Q never raises `ExitRequested` (only the last window's destruction and `AppHandle::exit` do). The JS `onCloseRequested` destroys the window itself and needs `core:window:allow-destroy`, which `core:default` lacks. | Our own Quit item (⌘Q) emits `app://quit-requested`; the page acks (`quit_ack`) and quits through `quit_app`; no ack within 2 s → quit (a hung page never traps the user). The window close goes through `onCloseRequested`. `ExitRequested` is left alone. The Dock's Quit, logout and shutdown still bypass the guard (known limitation). |
| B4 | §3.1 / §3.6: `useJobSession.ts` ("開 session、送命令、收事件、結束後關 session"). | A hook dies with its component; R-06-15 wants the job to outlive its dialog. | V1: `jobRunner.ts` + `jobsStore.ts`. |
| B5 | §3.6 RunFileDialog: 「結果：…「Run again from line N」與「Skip it and continue」」 (as buttons that run). | A run is configured and confirmed in its form (CR-3); an outcome's button that runs again would skip that. | V2. |
| B6 | §3.6 DumpDialog: 「物件清單（…可勾選）」. | `DumpScope` takes database, schema, or named tables/views of one schema; `DumpObjectKinds` takes kinds. A per-object choice beyond tables would turn a schema dump into a tables dump and drop its routines. | V14: per-table checkboxes in the Tables folder (§3.3.1), kinds elsewhere. |
| B7 | §3.6 DumpDialog progress: 「目前的物件、第幾個」. | `JobProgressEvent` names the object only. | V15. |
| B8 | §3.6 Jobs: 「重開它的對話框（進度、結果、取消）」 after a reload. | `JobInfo` has no command: the result and Cancel work, a run-again needs the command. | V6. |
| B9 | §3.6: "SQLite busy uses ⑤'s SQLITE_BUSY_NOTE" (06a F4 carry). | ⑤'s note ends "then commit again". | `lib/sqliteBusy.ts`'s `sqliteBusyNote(retry)`: ⑤ keeps its exact text, a run says "run the file again", a dump "dump again". |
| B10 | §3.3.4: `<database>_<schema 或表>_<yyyyMMdd-HHmm>.sql`. | MySQL's schema is its database; SQLite has a file and `main`; the Tables folder is several tables. | `dumpFileName`: MySQL not twice, SQLite the file's stem, "main" left out, the folder `<schema>_<n>-tables`; `/`, `:` and control characters removed; 120 characters at most. |
| B11 | §3.6 RunFileDialog target: 「連線、database；PG 的 search path」. | Couchbase takes a query context instead (`RunFileCommand.query_context`). | V13. |
| B12 | §3.6 入口: 「表格資料 tab 的選單」. | The table tab has no menu. | V4. |
| B13 | R-06-15 (a job survives its dialog and the window). | `DELETE /profiles/{id}` calls `close_for_profile`, which closes the profile's job sessions too: deleting a connection would end its running jobs as unknown. | The tree refuses to delete a connection while its jobs run (Task 8). |
| B14 | §3.6: 「完成通知沿用 S8」 only. | A job whose dialog is closed in front ends silently. | V11: a toast too. |

## Appendix A: the user's final test list (06b), for the coordinator's follow-ups note

The 06a note's company database checklist stays; its UI items are reachable now (item 9).

### 06b 測試清單（資料工具：dump 與 Run SQL file）

括號裡是 dev 資料庫上已經看到的結果（06b 的手動檢查在內建的 Chromium 做；打包版的選單、檔案對話框、通知、Show in Finder 與注音只能你自己測）。

1. **⌘Q 與關視窗（打包版，A12）。**
   - 沒有 job 在跑時 ⌘Q：直接結束，跟以前一樣。
   - Dump 一張大表或 Run 一個大檔時 ⌘Q：問「Quit DbaGuy?」，列出那個 job；預設按鈕是 Keep running（Return、Esc 都是留下），Quit anyway 才結束；Run 的話，下次開 app 時 History 那一筆是 Interrupted（dump 不寫 History），dump 的檔案不會出現。
   - 有 job 在跑時按紅燈或 ⌘⇧W：問「Close the window?」，按法相同。
   - ⌘W 仍只關目前的 tab。
   - 從 Dock 的 Quit、登出或關機結束：不會問（已知，系統直接結束 app）。
2. **檔案對話框（打包版）。** Dump 的 Choose… 是存檔面板，預設檔名像 `dbaguy_public_20261023-1405.sql`；Run SQL file 的 Choose… 是開檔面板，選了就讀檔、顯示前 200 行。
3. **Show in Finder（打包版）。** Dump 完成後按 Show in Finder，Finder 選到那個檔。
4. **通知。** 設定裡的 long_query_seconds 設小一點，開始 dump 或 run 後切到別的 app：結束時有系統通知；app 在前景、對話框關著時，右下角有提示，可以按 Open 回到結果。
5. **注音。** 在 Dump／Run 對話框的路徑欄、search path、Start at statement 用注音輸入：組字中按 Enter 選字，不會開始 dump、不會讀檔、不會執行。
6. **四個 engine 的 dump → run（你的真實 schema，在可以亂改的 database 上）。** PostgreSQL、SQL Server、SQLite 用 All or nothing；MySQL 用 Statement by statement（MySQL 的 DDL 會自動 commit）。
7. **紅色連線（CR-3）。** production 色的連線：Run 是紅色按鈕，要先勾「I've checked the file and the connection」；從結果按 Run again… 回到表單時要再勾一次。
8. **出錯之後。** 逐句執行停在某一行：Show line 顯示那一段；Run again from line N 與 Skip it and continue 都會回到表單、預先填好；檔案自己的 BEGIN…COMMIT 裡出錯時只有 Run again from line N（從那個 BEGIN 開始，前面被 rollback 的不會漏掉）。
9. **大檔與 Jobs。** 一個 1 GB 的檔：進度、ETA、Cancel；關掉對話框後側欄底部的 Jobs 還在，重新開回來有進度；重新載入視窗（或 app 當掉後回來）Jobs 仍列出它。
10. **公司資料庫（06a 清單的 UI 項目）。** SQL Server 的 snapshot 設定（Not a consistent snapshot 的警告）、MySQL 的 DEFINER 與 sql_mode、PG 的 extension 與 `money[]`、大表的 dump 與 run 時間。

已知的限制，不要當成 bug 回報：
- Dock 的 Quit、登出、關機不會問。
- 重新載入視窗後列出的 run，結果上沒有 Run again（要的話在表單裡用 Start at statement）。
- Dump 只能勾 Tables 資料夾裡的表；其他範圍勾的是物件種類。
- Couchbase 的 Run SQL file 只能逐句，query context 來自你點的 bucket／scope。
- 刪除有 job 在跑的連線會被擋下，等 job 結束再刪。

## Self-review

- **Spec coverage.** §3.6 DumpDialog → Task 6 (title, connection and colour, objects with ≈ rows, content, options with the filter, the plan's warnings and F2, the file with the save panel or a typed path, progress with the object and "k of n", MB, elapsed, closable, Cancel, the outcome with path, size, objects, warnings, Show in Finder); RunFileDialog → Task 7 (target, database, PG search path, the open panel and `chooseOpenPath` from Task 1, the inspection with the header, preview and warnings, the modes greyed with reasons, Start at statement N, CR-3's danger button and box, progress with MB of total, statements, errors, line and ETA, the outcome, the errors with their lines (B2), Run again from line N and Skip it and continue (B1, V2)); 入口 → Tasks 6–7 (tree, table tab, Scripts, ⌘⇧P; capabilities decide, D-06-13; no Copy items: 06c); Jobs → Task 8 (button with spinner and count, popover with kind, title, progress or outcome, elapsed, reopen, Clear finished, `GET /data/jobs`); 純模組 → Tasks 3–5 (`jobState`, `entryPoints`, `fileNames`, plus `jobReport`, `jobRunner`, `jobsStore`, `dumpForm`, `runForm`; `mappingRows` is 06c); macOS → Tasks 1, 6, 7 (Return, Esc, system panels, S8 notifications, `onCloseRequested`, our Quit item: A12 / B3). R-06-15 → Tasks 5, 6, 8; D-06-10 → Task 5 (`POST /sessions` with the unlock flow through `RecoverableError`, `DELETE` after, never reused); CR-3 → Tasks 4, 7; CR-5 → Task 1; §11 item 2 → this plan; §4's codes → Task 3 (`CODE_LABELS`, hints, the outcome texts). The 06a carries marked 06b: the dialogs, the Jobs list and the entries (Tasks 6–8), every listed code (Task 3's test enumerates them), the session never reused (Task 5), the late client's outcome as the end (Task 3), the plan's warnings before a dump (Task 6), the filtered file's warning first (Tasks 4, 7), SQLite busy (Task 3, B9), the filter's one-condition rule in the filter UI (Task 6), the ⌘Q guard (Task 1), `drop_first` and `filter` to both the plan and the dump (Task 4 `dumpPlanRequest` / `dumpCommand`).
- **Placeholder scan.** Every code step of a pure module, the shell and the backend has its whole code or a patch; the UI steps specify each component's props, order, copy and handlers; the only figures left are Task 9's measurements (recorded numbers, not design gaps).
- **Type consistency.** The pure modules, the shell, the backend and the palette changes were applied in a scratch worktree and compiled, tested and built together (imports only run forward in task order) (Dry-run record); names used across tasks (`jobReducer`, `jobReport`, `JobRunner`, `startJob`, `retryJob`, `cancelJob`, `syncJobs`, `openDumpDialog`, `openRunDialog`, `openJobDialog`, `shownJob`, `dumpEntryForNode`, `runEntryForNode`, `dumpPlanRequest`, `dumpCommand`, `runGate`, `runCommand`, `againEntry`, `readTableTabActions`, `resume_at`, `FileExcerpt`) come from those files.
- **Review Focus.** Each of the five lines names the tests or checks that pin it and their task.
- **Proportion.** The plan carries the pure modules' code because their rules and copy are the decisions (verified); the React components are specifications, as their wiring is checked by hand.

## Dry-run record

A scratch worktree at 7d756ed (`node_modules` linked from the main checkout; `cargo` with the main checkout's `target/` as `CARGO_TARGET_DIR`, so only the `dbaguy` crate rebuilt; the backend's tests with the main checkout's venv against the worktree's package), every task's given code applied together:
- Task 1's shell: `cargo check` and `cargo clippy` clean (tauri 2.12.4); a deliberately unknown permission (`core:window:allow-bogus`) failed the build script, `core:window:allow-destroy` passed.
- Task 2: `pytest tests/test_run_file_job.py` 109 passed (96 before + 13 new), `tests/test_run_file_inspect.py` 16 passed (13 + 3), `tests/test_job_framework.py` unchanged (163 in the three files); the generator (`openapi_dump` + `openapi-typescript`, as `npm run gen:api` runs them) added `FileExcerpt`, `resume_at` and the route.
- Tasks 3–6's pure modules and palette changes: `vitest run` 738 passed (40 files), `tsc --noEmit` clean, `vite build` clean (no `INEFFECTIVE_DYNAMIC_IMPORT` once `native.ts` imports `core` and `event` statically); main chunk 1,450.42 kB (gzip 470.03 kB) with the data-tools modules reachable from the palette, against 06a's 1,447.95 kB.
- Not dry-run: the React components of Tasks 6–8 (`DumpDialog`, `RunFileDialog`, `JobDialog`, `JobViews`, `TargetPicker`, `FileLines`, `DataToolsHost`, `JobsButton`, `useJobEndings`), except `QuitGuard.tsx`, `FilePathField.tsx`, `Popover.tsx` and the export dialog's move, which compiled.
