# Plan 02b (grid and data) — rulings and follow-ups

Plan: `docs/superpowers/plans/2026-10-16-02b-grid-and-data.md` (planning rulings R-02b-1…5 are in its last section). Executed with subagent-driven development: 8 tasks, each reviewed (fix rounds re-reviewed), most manually verified in the built-in browser against the dev PG; final Opus whole-branch review with one fix wave (all items verified). Commits 7d144fa..3516939.

## Rulings made during execution

- Task 2: Ruling: scripts/dev.sh defaults DBAGUY_DATA_DIR to dev/data (unless set) — the plan's constraint assumed it already did; dev runs must never touch the real app data — costs nothing; folded into Task 2's fix round.
- Ruling R-02b-6: header click selects the column (spec §4.3); when sorting is available (table data, spec §4.5) each header shows a sort control at its right edge — clicking it cycles asc → desc → none, shift adds/cycles in a multi-sort — both spec rules hold without ambiguity — costs a slightly smaller click target for sorting. Carry to Tasks 4 and 7.
- Ruling R-02b-7: single-cell ⌘C copies the cell's plain value client-side (no TSV quoting), shared with the value viewer's Copy value — a JSON/multi-line cell must paste as itself — carried into Task 6.
- Ruling R-02b-8: in Tauri, copy writes through @tauri-apps/plugin-clipboard-manager (native), browser mode keeps navigator.clipboard — copy is a Must and WKWebView clipboard cannot be verified here — costs one plugin + capability.
- Ruling R-02b-9: add a `warning` colour token now (badge, toast stripe) before ②c adds more warning UI — costs nothing.

## Carries for plan 02c (query tab, editor)

- Task 8 carry: wrap chooseSavePath in try/catch and show the error (do not treat it as cancel).
- Task 7 carry: wait for phase === "open" before the first browse; after discard(previous) also forget(previous) so old pages are freed.
- 02c carry: an orphan left running inside a transaction after a reconnect cannot be cancelled from the UI (interrupted = done); console ignores confirmation_required (②c query tab must show the confirmation dialog).
- Task 5 carry: confirm ⌘C/⌘A reach keydown in the Tauri WKWebView (default macOS Edit menu binds them).
- Tasks 5–7 carry: read the selection's rect (not mode); "all" spans whatever is loaded — copies must bound themselves.
- Task 5: implemented 177d793 (base b8c13aa); implementer ran the manual check in browser mode (clipboard stubbed — real clipboard + Tauri WKWebView ⌘C/⌘A need a packaged-app check); new onSortBy prop for header menu sort items (Task 7 carry); copy limit: exactly 100,000 allowed (brief rule).
- Task 7 carry: pass onSortBy (header menu Sort ascending/descending) besides onHeaderClick.
- 02c carries (from final review recommendations): whenSettled(requestId) (done or paused) for sequential runs; one busy/canSend selector; forget previous runs and transaction executions (forgetAllExcept); hoist ExportDialog out of DataGrid (onExport) — natural home for export-in-transaction warning and close-tab guard; cancelling an export inside a manual-commit transaction aborts it — warn or use a savepoint (mandatory in 02c); count session could close when idle.

## Carries for later sub-projects

- ③ carry: filterForValue is PG/SQLite only (double quotes, TRUE/FALSE) — make dialect-aware for MySQL/MSSQL.

## Packaged-app checks (for the user's final test list)

- Packaged-app manual checks (for the user's final test list): real clipboard copy in Tauri; ⌘C/⌘A in the grid vs the default macOS Edit menu.
- Packaged-app checks for the user's final test list: clipboard (native plugin), ⌘A/⌘C in grid vs Edit menu, context menus, save panel, Zhuyin filter entry.

## Deferred minor findings

- Task 1: minor (deferred): notification:default broader than needed (allow-notify / allow-request-permission / allow-is-permission-granted suffice); sendNotification failures surface as unhandled rejections.
- Task 2: minor (deferred): orange badge uses raw palette classes (no warning token).
- Task 2: minor (deferred): dev.sh does not set DBAGUY_LOG_DIR (dev logs go to ~/Library/Logs/DbaGuy); relative DBAGUY_DATA_DIR override resolves differently in the backend subshell; SessionSocket stops retrying if getBackendInfo throws during a reconnect.
- Task 3: minor (deferred): formatBytes can print 1024.0 KB; no title tooltip for long plain text until drawers; header band stops at last column; row-number width jumps at digit boundaries; grid height shifts ~28 px when the Running strip toggles.
- Task 4: minor (deferred): aria-activedescendant may reference unrendered cell; Load-more footer outside a row inside role=grid; sort control mouse-only (tabIndex=-1); column-mode shift+ArrowDown moves an invisible focus row; gridKeyDown plain function in a use* file.
- Task 4: minor (deferred): e.code ties ⌘A/⌘C to QWERTY positions (Dvorak/AZERTY: add e.key fallback); two inline clamps remain (layout.ts:75, Explorer.tsx:130); Escape in column/row mode jumps to row/col 0.
- Task 5: minor (deferred): isShortcut ORs code and key (AZERTY ⌘Q swallowed as select-all; prefer key when ASCII letter); no rows×cols cap (100k×50 = 5M cells in one POST); clipboard fallback if WKWebView refuses (execCommand or Tauri clipboard plugin); warning stripe raw yellow; small unhandled rejection when clipboard missing + format fails; toast hovered state after dismissing last toast; COPY_FORMATS/LABELS duplication.
- Task 6: minor (deferred): looksLikeJson false positives for bracketed log text ("[1] and [2]" compacted); disabled prev/next buttons drop focus; every JSON key is a tab stop; drawer re-renders whole tree on grid renders (memo + stable callbacks); stale onCopy comment, optional openValue/openRecord; drawer width ref+state; jsonPath `.$x`; string values shown without re-escaping.
- Task 7: minor (deferred; candidates for the final fix wave): grid unmounts on every re-run so dragged widths/scroll reset on each sort/filter/refresh; sorting after a 422 clears the filter error while the input still shows rejected text; refresh keeps a stale count; sort race reads applied job (ref fix); red filter border for non-filter errors (connection); addCondition with trailing comment/semicolon; PG float4 exact-match with bare literal; relationOf computed twice; DropdownMenu lives in ContextMenu.tsx; history labels lack title; icon button lacks aria-label; menus untested in WKWebView.
- Task 8: minor (deferred; candidates for the fix wave): table-tab browse build in flight can send an execute mid-export and orphan the dialog (useSessionJob should hold sends while exporting); ⌘W closes the tab mid-export without asking; double-click Export can land on Cancel; Cancel no-ops when socket not open and the dialog stays locked; console multi-result grids all export the whole script; dialog settings reset on remount; no autofocus; cancel inside a PG transaction aborts it (no warning); qualified duplicate.
- Final fix wave re-review: all 8 items ADDRESSED, no new Critical/Important. Residual minors: record view collapses multi-line text (value viewer keeps it); untouched widths re-estimated per result; red filter border for non-filter errors; sticky filterError masks later errors; stale grid undimmed during export-hold race; export from a stale grid targets the new SQL; badge text contrast ~4.1:1.
