# Plan 02c (editor and query) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-16-02c-editor-and-query.md` (planning rulings R-02c-1…4 are in its last section). Executed with subagent-driven development: 9 tasks, each reviewed (fix rounds re-reviewed), all manually verified in the built-in browser against the dev PG and a SQLite file; final Opus whole-branch review with one fix wave (re-reviewed). Commits 3e48757..5a2387a.

## Rulings made during execution

- R-02c-5: multi-cursor add is ⌘-click (CodeMirror's macOS default); ⌥-click stays reserved for describe (spec §4.11) — the plan's manual-check wording was wrong — costs nothing.
- R-02c-6: completion auto-pops only after a word character, "." or "ON "; empty-prefix contexts (`FROM ␣`, `JOIN ␣`, after ",") need Ctrl+Space or a first letter (DataGrip-like; Enter after a space never accepts a list) — costs one keystroke in those contexts.
- R-02c-7: a write-ahead localStorage mirror holds only workspace state the backend has not acknowledged (removed on a successful save; on restore a pending entry wins and is pushed at once) — the spec keeps backend files as the source of truth — costs ~25 lines.
- R-02c-8: the dirty-script close guard runs first (before run and transaction guards) — Cancel on "Save changes?" must never follow a side effect (stopped run, committed/rolled-back transaction) — costs nothing.
- R-02c-9: one shared `isImeKeyEvent` (isComposing / key "Process" / keyCode 229) guards every non-CodeMirror key handler — WebKit sends the composition-confirming Enter after compositionend — costs nothing.
- R-02c-10: History "This connection" = the active tab's connection (any tab kind), else the explorer's selected one, else the toggle is disabled; double-click with no active query tab opens a new tab on the row's connection; a deleted connection opens the tab with none — spec silent — cosmetic if wrong.
- R-02c-11: relative times are English ("16 min. ago") to match the English UI; the absolute tooltip uses the system locale — one line to change.
- R-02c-12: changing the scripts folder unbinds every script-bound tab (text and edits kept, toast names them) — a relative path would otherwise point into the new folder and ⌘S would overwrite a different file — costs re-binding with Save As.
- R-02c-13: long-query notifications: threshold 0 = off; cancelled runs never notify; multi-statement runs say "N statements" — spec silent — cosmetic.
- R-02c-14: crash safety — the editor hands its final text to the workspace store on unmount (independent of React's cleanup order); a root error boundary (Reload) and a per-tab boundary (Try again) keep other tabs alive — found when a render crash made the next save write a tab's original text (spec §4.8 "a crash never loses work") — costs ~60 lines.
- R-02c-15: palette commands call the active tab's own handlers through a tab-action registry (same functions as the toolbar and keymaps), so guards and confirmations behave identically.
- R-02c-16: quick-open object search uses the active tab's connection, else the selected one; server order kept; a failed search is an inline row.
- R-02c-17: the app menu is Tauri's default menu except Close Window, which is a custom item on ⌘⇧W — Tauri 2.12.4's predefined Close Window carries ⌘W, which would close the whole window instead of the tab in the packaged app — ~90 lines of Rust in `src-tauri/src/menu.rs`.
- R-02c-18: dialogs take the focus when they open and give it back when they close (recorded per dialog when it opens) — focus used to stay in the editor behind a modal.
- R-02c-19: ⌘W is ignored while any dialog (the palette included) is open — it used to close the tab behind the modal.
- R-02c-20: the per-tab statement timeout (R-02c-1) is a toolbar control; a tab left on the default follows the setting.
- R-02c-21: a script-bound tab whose text equals its file stores no buffer in the workspace; on restore it shows the file as it is now — a file changed outside DbaGuy (git pull) used to come back as stale "unsaved changes" that ⌘S would write over the newer file. Until its file is read the tab is read-only ("Loading…").
- R-02c-22: deleting a script first loads its file into any restored tab not read yet (a failed read cancels the delete); changing the scripts folder closes tabs still showing just their file (unchanged, still in the old folder) and unbinds the others — the R-02c-21 change had made those tabs empty on unbind (the re-review of the final fix wave caught it).

## Fixed along the way (pre-existing)

- `sqlite:////abs/path` URL import produced `//abs/path` (backend adapter; test added).
- Run all on long scripts re-rendered every statement row per event: 2,000 INSERTs took 13 min 43 s; now ~33 s with no long tasks (memoised, virtualised Messages).

## Carries for later sub-projects

- ③: `filterForValue` and quoting are PG/SQLite only (from 02b); sql-formatter dialect mapping and statement splitting need MySQL ("mysql") and MSSQL ("transactsql").
- ⑥: classify must cap sqlglot input for huge INSERTs (from 02a); the splitter is now one-in-flight with a cost-scaled pause, but multi-MB scripts still split in ~0.5 s/MB and the formatter has no size guard.
- Any later sub-project touching scripts: a `modified_at` conflict prompt (Reload / Keep mine) when a dirty tab's file also changed on disk; no external-change check before ⌘S while the app runs.
- Quit (⌘Q), Close Window (⌘⇧W) and the traffic light skip the close guards: an open manual-commit transaction is rolled back and a running export is killed (text survives in the workspace).
- A failed restore starts from an empty baseline: the first tab change overwrites `workspace.json` (a toast warns).
- Structure/DRY: a shared query-keys module (palette ↔ explorer imports), one `useAsk` for the three promise-backed dialogs, one `firstLine`, a `useProfile(id)` for repeated profile-name lookups.
- Possible: reopen closed tab (⌘⇧T) — closing an unbound query tab discards its text without asking (plan Task 3: close immediately).

## Packaged-app checks (for the user's final test list)

1. Zhuyin typing in the editor, palette, Save As, filter bar, scripts rename and history search (including the late Enter).
2. ⌘W closes a tab (not the window); ⌘⇧W, the traffic light and ⌘Q close/quit; ⌘, ⌘P ⌘⇧P ⌘⇧F ⌘↵ reach the page; Edit menu ⌘Z ⌘⇧Z ⌘A ⌘C ⌘V do not double-fire with CodeMirror.
3. Quit and Close Window save the workspace (pagehide/beforeunload before the backend stops); the localStorage mirror survives an abrupt quit.
4. Native folder chooser (Settings → Scripts folder); notifications (first-run permission prompt, background only, 15 s threshold).
5. The lazy formatter chunk loads under the production CSP; focus after the palette opens a table tab; focus after cancelling a close-guard dialog.
6. First launch after a rebuild: Keychain "Allow" prompt must not make the restore time out into an empty workspace.

## Deferred minor findings

- Task 1: editor theme's dark flag read at module load; run shortcuts only in tooltips; bundle ~1.2 MB (lazy vim possible).
- Task 2: "SELECT 1;SELECT 2" with the cursor right after ";" picks statement 2; a "text changed" toast when the tab closes during analysis; any edit anywhere aborts a pending run (conservative).
- Task 3: the run guard stops a run before later guards answer (remaining statements skipped silently); Rollback while busy relies on closing the session; export dialog settings reset on a new session.
- Task 4: the ON auto-popup preselects the first FK condition (also pops for ON CONFLICT / CREATE INDEX … ON / GRANT … ON); 300-item cap hard-coded; statementWindow has no test (no frontend framework).
- Task 5: backend://exited missed while restoring; the mirror is rewritten on every retry even if unchanged (multi-MB scripts during an outage); shallow mirror validation.
- Task 6: mixed line endings show dirty on open; no per-script connection memory after the tab closes; tree name field commits on blur; Explorer tree Enter / drawer Escape lack the IME guard (not text inputs).
- Task 7: HistoryPanel.tsx ~370 lines; 30-day months in relative time; a long load error can squeeze the list; deeply scrolled history shrinks to its first page after a run.
- Task 8: no formatter size guard; field errors lack aria-describedby; a respelled scripts folder (~/x vs /Users/me/x) also unbinds tabs; a crashed tab's close guards unmount with it.
- Task 9: the scripts section may show twice when a refresh fails with a cached tree; a dialog opened from inside another dialog restores nothing on close (the parent's focus trap recovers on Tab).
- Final fix wave: a tab's timeout, once set, cannot go back to following Settings; drag-selecting Messages text past the rendered rows is clipped (virtualised); a memo'd Messages row's "Line N, column M" can lag later edits (the jump target is current); Messages row heights reused briefly across runs; `loadWaitingTabs` finds the tab again by path, and its read has no timeout; "Nothing to save" shows as an error toast.
