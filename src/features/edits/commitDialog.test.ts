import { describe, expect, it } from "vitest";
import type { EditPreview, EditWarning, PlannedStatement, ServerEvent } from "@/lib/api/types";
import { applyEvent, newExecution, type ExecutionState } from "../session/execution";
import {
  afterglowHolds,
  afterglowNow,
  afterglowOf,
  applyingStep,
  applyingView,
  browseWasPaused,
  closable,
  COMMIT_RUNNING,
  commitBlocked,
  commitStep,
  countParts,
  EXPORT_RUNNING,
  failedPlace,
  LIST_MAX,
  moreText,
  nextSqlModeRefusals,
  NOT_CONNECTED_TEXT,
  outcomeView,
  previewed,
  previewFailed,
  previewing,
  previewLists,
  refreshBy,
  REREAD_FIRST,
  resultStep,
  rowLabel,
  statementLayout,
  UNKNOWN_REFRESH_FIRST,
  type Afterglow,
  type CommitDialogState,
  type CommitFacts,
  type PreviewStep,
} from "./commitDialog";
import {
  commitState,
  NEW_ROWS_NOTE,
  NOT_SENT_REPORT,
  OUTCOME_WAIT_MS,
  UNKNOWN_BEFORE,
  type CommitReport,
  type EndedCommit,
} from "./commitFlow";
import { emptyStaged, rowKeyFromKey, stage, stagingOf, type StagedEdits } from "./staging";
import { ROWS_LOADING } from "./tableEditing";

/*
 * The commit dialog's decisions (spec ⑤ §3.6.5; pre-flight H2, H8, M1, M2, M5, L10, L15; 05a M7): which preview's plan
 * is applied, when Commit may start, what an outcome shows, how long the grid draws the staging from before a commit,
 * and the dialog's lists, built in one pass over a preview of any size.
 */

const statement = (id: string, kind: PlannedStatement["kind"], sql: string, row: string | null = null): PlannedStatement => ({
  change_id: id,
  kind,
  sql,
  unchecked: [],
  row,
});

function previewOf(parts: Partial<EditPreview> = {}): EditPreview {
  return {
    plan_id: "plan-1",
    begin: "BEGIN",
    statements: [statement("u:1", "update", "UPDATE t SET a = 1 WHERE id = 1", "id = 1")],
    commit: "COMMIT",
    problems: [],
    warnings: [],
    counts: { updates: 1, inserts: 0, deletes: 0 },
    script: "BEGIN;\nUPDATE t SET a = 1 WHERE id = 1;\nCOMMIT;",
    ...parts,
  };
}

const loaded = (id: number) => ({ kind: "row" as const, rowKey: rowKeyFromKey({ id }, ["id"]), key: { id } });
const text = (value: string) => ({ kind: "value" as const, text: value });

/** A staging with an UPDATE of row `id` (change id "u:<id>"). */
function edited(id: number, base: StagedEdits = emptyStaged(["id"]), value = "x"): StagedEdits {
  return stage(base, { type: "edit", cell: { row: loaded(id), column: "a", original: "old" }, input: text(value) }, () => `u:${id}`);
}

const shown = (preview: EditPreview, staged: StagedEdits): PreviewStep => ({
  step: "preview",
  preview,
  staging: stagingOf(staged),
  key: 1,
  unknownBefore: false,
});

const R = "apply";
const run = (events: object[]): ExecutionState =>
  events.reduce<ExecutionState>(
    (state, event) => applyEvent(state, { request_id: R, ...event } as ServerEvent),
    newExecution(R, "edits", ""),
  );
const finished = { type: "finished", elapsed_ms: 1, in_transaction: false, cancelled: false, timed_out: false, skipped: false };
const outcome = (value: string, extra: object = {}) => ({ type: "edit_outcome", outcome: value, applied: [], already_gone: [], ...extra });
const ended = (events: object[]) => commitState(run(events)) as EndedCommit;

describe("which preview's plan is applied (05a M7, H8)", () => {
  it("shows a preview only while the dialog still waits for that very preview", () => {
    const answer = { preview: previewOf(), staging: stagingOf(edited(1)), key: 1, unknownBefore: false };
    const waiting = previewing(2);
    expect(previewed(waiting, 2, answer)).toEqual({ step: "preview", ...answer });
    // The dialog closed meanwhile: its answer is dropped, so its plan can never be applied.
    expect(previewed(null, 2, answer)).toBeNull();
    // Preview again asked for a newer one: the older answer changes nothing.
    expect(previewed(previewing(3), 2, answer)).toEqual(previewing(3));
    const result = previewed(waiting, 2, answer) as CommitDialogState;
    expect(previewed(result, 2, answer)).toBe(result); // a second answer for the same ticket is not taken either
  });

  it("shows a failed preview under the same rule", () => {
    const error = new Error("nope");
    expect(previewFailed(previewing(1), 1, error, "nope")).toEqual({ step: "failed", error, message: "nope" });
    expect(previewFailed(null, 1, error, "nope")).toBeNull();
    expect(previewFailed(previewing(2), 1, error, "nope")).toEqual(previewing(2));
  });

  it("applies the plan on screen while the staging is the one it was made from", () => {
    const staged = edited(1);
    const state = shown(previewOf(), staged);
    expect(commitStep(state, stagingOf(staged))).toEqual({ kind: "apply", planId: "plan-1" });
    // The preview's problems marking the cells, and the apply being marked in flight, change no change.
    const marked = stage(stage(staged, { type: "setProblems", problems: { "u:1": [{ column: "a", message: "x" }] } }), {
      type: "applying",
    });
    expect(commitStep(state, stagingOf(marked))).toEqual({ kind: "apply", planId: "plan-1" });
  });

  it("previews again when the staging changed since the preview (an editor staged as the dialog took the focus)", () => {
    const staged = edited(1);
    const state = shown(previewOf(), staged);
    expect(commitStep(state, stagingOf(edited(2, staged)))).toEqual({ kind: "repreview" });
    expect(commitStep(state, stagingOf(edited(1, staged, "y")))).toEqual({ kind: "repreview" });
    expect(commitStep(state, null)).toEqual({ kind: "repreview" });
  });

  it("compares every part of the staging", () => {
    const parts = stagingOf(edited(1));
    const state = shown(previewOf(), edited(1));
    const made = { ...state, staging: parts };
    expect(commitStep(made, { ...parts })).toEqual({ kind: "apply", planId: "plan-1" });
    expect(commitStep(made, { ...parts, updates: { ...parts.updates } })).toEqual({ kind: "repreview" });
    expect(commitStep(made, { ...parts, inserts: [...parts.inserts] })).toEqual({ kind: "repreview" });
    expect(commitStep(made, { ...parts, deletes: { ...parts.deletes } })).toEqual({ kind: "repreview" });
  });

  it("applies after an undo back to the staging the preview was made from", () => {
    const staged = edited(1);
    const state = shown(previewOf(), staged);
    const undone = stage(edited(2, staged), { type: "undo" });
    expect(commitStep(state, stagingOf(undone))).toEqual({ kind: "apply", planId: "plan-1" });
  });

  it("has nothing to apply without a plan, or outside the preview step", () => {
    const staged = edited(1);
    expect(commitStep(shown(previewOf({ plan_id: null }), staged), stagingOf(staged))).toEqual({ kind: "none" });
    expect(commitStep(previewing(1), stagingOf(staged))).toEqual({ kind: "none" });
    expect(commitStep(null, stagingOf(staged))).toEqual({ kind: "none" });
    const applying = applyingStep(shown(previewOf(), staged), "r1", false);
    expect(commitStep(applying, stagingOf(staged))).toEqual({ kind: "none" }); // a second click never sends again
  });

  it("closes on anything but a running commit", () => {
    const state = shown(previewOf(), edited(1));
    const applying = applyingStep(state, "r1", true);
    expect(closable(null)).toBe(true);
    expect(closable(previewing(1))).toBe(true);
    expect(closable(state)).toBe(true);
    expect(closable(applying)).toBe(false);
    const report: CommitReport = { ...NOT_SENT_REPORT };
    expect(closable(resultStep(applying, report, []))).toBe(true);
  });

  it("keeps the preview, its staging and its editor through applying and the result", () => {
    const state = shown(previewOf(), edited(1));
    const applying = applyingStep(state, "r1", true);
    expect(applying).toMatchObject({ step: "applying", preview: state.preview, staging: state.staging, key: 1 });
    expect(applying).toMatchObject({ requestId: "r1", browseWasPaused: true });
    const result = resultStep(applying, NOT_SENT_REPORT, [{ severity: "info", text: "m" }]);
    expect(result).toEqual({
      step: "result",
      preview: state.preview,
      staging: state.staging,
      key: 1,
      unknownBefore: false,
      report: NOT_SENT_REPORT,
      messages: [{ severity: "info", text: "m" }],
    });
  });
});

describe("when Commit… may start", () => {
  const ready: CommitFacts = {
    applying: false,
    sessionOpen: true,
    exporting: false,
    unknownPending: false,
    rowsLoading: false,
    afterglow: false,
    readOnlyReason: null,
  };

  it("starts when nothing stands in the way", () => {
    expect(commitBlocked(ready)).toBeNull();
  });

  it("says why not, the first reason first", () => {
    expect(commitBlocked({ ...ready, applying: true, sessionOpen: false })).toBe(COMMIT_RUNNING);
    expect(commitBlocked({ ...ready, sessionOpen: false, exporting: true })).toBe(NOT_CONNECTED_TEXT);
    expect(commitBlocked({ ...ready, exporting: true, unknownPending: true })).toBe(EXPORT_RUNNING);
    expect(commitBlocked({ ...ready, unknownPending: true, rowsLoading: true })).toBe(UNKNOWN_REFRESH_FIRST);
    expect(commitBlocked({ ...ready, rowsLoading: true, afterglow: true })).toBe(ROWS_LOADING);
    expect(commitBlocked({ ...ready, afterglow: true, readOnlyReason: "Views are read-only here" })).toBe(REREAD_FIRST);
    expect(commitBlocked({ ...ready, readOnlyReason: "Views are read-only here" })).toBe("Views are read-only here");
  });
});

describe("the afterglow: the staging from before a commit, until the re-read is in (pre-flight M5)", () => {
  const before = edited(2, edited(1));
  const at = { deliveredRequest: "browse-1", position: 3 };

  it("is kept for a commit and a partial one, which take changes away", () => {
    expect(afterglowOf("committed", before, at)).toEqual({ staged: before, since: "browse-1", position: 3 });
    expect(afterglowOf("partial", before, at)).toEqual({ staged: before, since: "browse-1", position: 3 });
  });

  it("is not kept when the outcome took nothing away, or nothing was staged", () => {
    expect(afterglowOf("rolled_back", before, at)).toBeNull();
    expect(afterglowOf("unknown", before, at)).toBeNull();
    expect(afterglowOf("committed", null, at)).toBeNull();
    expect(afterglowOf("committed", emptyStaged(["id"]), at)).toBeNull();
  });

  it("holds while the re-read loads, and goes once another request delivers its rows", () => {
    const glow = afterglowOf("committed", before, at) as Afterglow;
    expect(afterglowHolds(glow, at)).toBe(true); // the commit's own render: the old rows are still the ones shown
    expect(afterglowHolds(glow, { deliveredRequest: null, position: 3 })).toBe(true); // the re-read loads
    expect(afterglowHolds(glow, { deliveredRequest: "browse-2", position: 3 })).toBe(false); // its rows are in
  });

  it("goes with a new sort or filter", () => {
    const glow = afterglowOf("committed", before, at) as Afterglow;
    expect(afterglowHolds(glow, { deliveredRequest: null, position: 4 })).toBe(false);
    expect(afterglowHolds(glow, { deliveredRequest: "browse-1", position: 4 })).toBe(false);
  });

  it("stays over held rows after a re-read cut short (K17), until a request delivers", () => {
    const glow = afterglowOf("partial", before, at) as Afterglow;
    // The re-read was cancelled: nothing delivered, the rows from before stay on screen, and so does the afterglow.
    expect(afterglowNow(glow, { deliveredRequest: null, position: 3 })).toBe(glow);
    expect(afterglowNow(glow, { deliveredRequest: "browse-9", position: 3 })).toBeNull(); // the user's Refresh
  });

  it("is never drawn again once it went (a later load delivers nothing for a while)", () => {
    let glow: Afterglow | null = afterglowOf("committed", before, at);
    const seen: boolean[] = [];
    for (const deliveredRequest of ["browse-1", null, "browse-2", null, "browse-1"]) {
      glow = afterglowNow(glow, { deliveredRequest, position: 3 });
      seen.push(glow !== null);
    }
    expect(seen).toEqual([true, true, false, false, false]);
  });

  it("starts after a browse that had delivered nothing (rows kept after a cancelled Refresh)", () => {
    const glow = afterglowOf("committed", before, { deliveredRequest: null, position: 3 }) as Afterglow;
    expect(afterglowHolds(glow, { deliveredRequest: null, position: 3 })).toBe(true);
    expect(afterglowHolds(glow, { deliveredRequest: "browse-2", position: 3 })).toBe(false);
  });
});

describe("how the result's Refresh reads the table again", () => {
  it("runs the browse again on a live session, and opens a new one when it is gone", () => {
    expect(refreshBy("open")).toBe("refresh");
    expect(refreshBy("reconnecting")).toBe("refresh"); // sent once the socket is back
    expect(refreshBy("opening")).toBe("refresh");
    expect(refreshBy("failed")).toBe("reconnect"); // e.g. the backend restarted: that session is gone
    expect(refreshBy("closed")).toBe("reconnect");
    expect(refreshBy(null)).toBe("reconnect");
  });
});

describe("whether the browse had not ended when the apply went (L15)", () => {
  it("counts a paused browse and one loading more; a finished one and none do not", () => {
    const browse = newExecution("b", "execute", "SELECT 1");
    expect(browseWasPaused(null)).toBe(false);
    expect(browseWasPaused(browse)).toBe(true); // running: a Load more is on its way
    expect(browseWasPaused({ ...browse, status: "paused" })).toBe(true);
    expect(browseWasPaused({ ...browse, status: "done" })).toBe(false);
  });
});

describe("what an outcome shows", () => {
  const counts = { updates: 2, inserts: 1, deletes: 1 };
  const context = { table: "public.items", counts, sqlModeRefusals: 0 };

  it("closes a commit with a toast", () => {
    const commit = ended([{ type: "started" }, outcome("committed", { applied: ["u:1", "u:2", "i:3", "d:4"] }), finished]);
    expect(outcomeView(commit, context)).toEqual({
      kind: "toasts",
      toasts: [{ kind: "success", text: `Committed 4 changes to public.items. ${NEW_ROWS_NOTE}` }],
    });
  });

  it("gives a COMMIT's warning its own toast, never a trigger's", () => {
    const warning = "COMMIT reported an error after every statement had taken effect: disk full";
    const commit = ended([
      { type: "started" },
      { type: "message", severity: "warning", text: warning },
      outcome("committed", { applied: ["u:1"] }),
      finished,
    ]);
    const view = outcomeView(commit, { ...context, counts: { updates: 1, inserts: 0, deletes: 0 } });
    expect(view).toEqual({
      kind: "toasts",
      toasts: [
        { kind: "success", text: "Committed 1 change to public.items" },
        { kind: "warning", text: warning },
      ],
    });
  });

  it("stays open to list a trigger's messages of a commit (L10), with where its new rows went", () => {
    const notice = { severity: "info", text: "NOTICE: audit row written" };
    const commit = ended([{ type: "started" }, { type: "message", ...notice }, outcome("committed", { applied: ["i:1"] }), finished]);
    const view = outcomeView(commit, context);
    expect(view.kind).toBe("result");
    if (view.kind !== "result") return;
    expect(view.report).toMatchObject({ tone: "success", title: "Committed 1 change", actions: [] });
    expect(view.report.notes).toEqual([NEW_ROWS_NOTE]);
    expect(view.messages).toEqual([notice]);
  });

  it("lists every message of such a commit, its COMMIT warning too, and no note without new rows", () => {
    const notice = { severity: "info", text: "PRINT: audited" };
    const warning = { severity: "warning", text: "COMMIT reported an error after every statement had taken effect: x" };
    const commit = ended([
      { type: "started" },
      { type: "message", ...notice },
      { type: "message", ...warning },
      outcome("committed", { applied: ["u:1"] }),
      finished,
    ]);
    const view = outcomeView(commit, { ...context, counts: { updates: 1, inserts: 0, deletes: 0 } });
    expect(view).toMatchObject({ kind: "result", messages: [notice, warning] });
    if (view.kind === "result") expect(view.report.notes).toEqual([]);
  });

  it("shows anything else in the dialog, with what the backend said", () => {
    const failed = ended([
      { type: "started" },
      { type: "error", message: "Row id = 7 was changed", code: "edit_conflict" },
      outcome("rolled_back", { failed_change: "u:1", statement: 0 }),
      finished,
    ]);
    const view = outcomeView(failed, context);
    expect(view).toMatchObject({ kind: "result", report: { title: "Nothing was applied", actions: ["show_in_grid"] } });
    expect(outcomeView({ kind: "not_sent" }, context)).toEqual({ kind: "result", report: NOT_SENT_REPORT, messages: [] });
    const lost = ended([{ type: "started" }, { type: "affected", index: 0, rows: 1 }, finished]);
    expect(outcomeView(lost, context)).toMatchObject({ kind: "result", report: { title: "Outcome unknown" } });
  });

  it("counts the sql_mode refusals in a row; a command that never left changes nothing", () => {
    const refused = ended([{ type: "error", message: "m", code: "sql_mode_changed" }, outcome("rolled_back"), finished]);
    const other = ended([{ type: "error", message: "m", code: "plan_expired" }, outcome("rolled_back"), finished]);
    expect(nextSqlModeRefusals(0, refused)).toBe(1);
    expect(nextSqlModeRefusals(1, refused)).toBe(2);
    expect(nextSqlModeRefusals(2, other)).toBe(0);
    expect(nextSqlModeRefusals(2, { kind: "not_sent" })).toBe(2);
  });
});

describe("the statements as the dialog shows them", () => {
  it("counts the deletes first, leaving out what is not there", () => {
    expect(countParts({ updates: 3, inserts: 1, deletes: 1 })).toEqual([
      { text: "1 delete", danger: true },
      { text: "3 updates", danger: false },
      { text: "1 insert", danger: false },
    ]);
    expect(countParts({ updates: 0, inserts: 12_000, deletes: 0 })).toEqual([{ text: "12,000 inserts", danger: false }]);
  });

  it("lays out the frame and each statement as sent, a line each, knowing where each starts", () => {
    const preview = previewOf({
      begin: "SET NOCOUNT OFF;\nBEGIN TRANSACTION",
      statements: [
        statement("d:1", "delete", "DELETE FROM t WHERE id = 4", "id = 4"),
        statement("u:2", "update", "UPDATE t SET note = 'two\nlines' WHERE id = 2", "id = 2"),
        statement("i:3", "insert", "INSERT INTO t (a) VALUES (1)"),
      ],
      commit: "COMMIT TRANSACTION",
    });
    const layout = statementLayout(preview);
    expect(layout.text).toBe(
      [preview.begin, ...preview.statements.map((s) => s.sql), preview.commit].join("\n"),
    );
    preview.statements.forEach((s, i) => expect(layout.text.slice(layout.starts[i], layout.starts[i] + s.sql.length)).toBe(s.sql));
  });

  it("marks the failed statement's lines and puts the cursor where the server put its error (L12)", () => {
    const preview = previewOf({
      statements: [
        statement("u:1", "update", "UPDATE t SET a = 1 WHERE id = 1", "id = 1"),
        statement("u:2", "update", "UPDATE t SET note = 'a\nb'\nWHERE id = 2", "id = 2"),
      ],
    });
    const layout = statementLayout(preview);
    const from = layout.starts[1];
    const report: CommitReport = {
      ...NOT_SENT_REPORT,
      failedStatement: 1,
      failedPosition: { line: 3, column: 7 },
    };
    const place = failedPlace(preview, layout, report);
    expect(place).toEqual({
      from,
      to: from + preview.statements[1].sql.length,
      lineStarts: [from, from + "UPDATE t SET note = 'a\n".length, from + "UPDATE t SET note = 'a\nb'\n".length],
      cursor: from + "UPDATE t SET note = 'a\nb'\n".length + 6,
    });
    expect(layout.text.slice(place!.cursor, place!.cursor + 5)).toBe("id = ");
    expect(failedPlace(preview, layout, { ...report, failedPosition: null })?.cursor).toBe(from);
    expect(failedPlace(preview, layout, { ...report, failedStatement: null })).toBeNull();
    expect(failedPlace(preview, layout, { ...report, failedStatement: 7 })).toBeNull();
  });
});

describe("the dialog's lists (pre-flight H2)", () => {
  const unchecked = (id: string, column: string): EditWarning => ({
    code: "unchecked_columns",
    message: `Not checked for changes made since loading: ${column}`,
    change_id: id,
    columns: [column],
  });

  it("names each warning's row from its statement, with the reasons it does not check", () => {
    const preview = previewOf({
      statements: [
        { ...statement("u:1", "update", "UPDATE …", "id = 1"), unchecked: [{ column: "price", reason: "floating point" }] },
        statement("i:2", "insert", "INSERT …"),
      ],
      warnings: [
        { code: "non_transactional", message: "This table's storage engine cannot roll back: …", columns: [] },
        unchecked("u:1", "price"),
        { code: "missing_required", message: "New row: no value for code (NOT NULL, no default)", change_id: "i:2", columns: ["code"] },
      ],
    });
    const lists = previewLists(preview, stagingOf(emptyStaged(["id"])), false);
    expect(lists.transactional).toBe(false);
    expect(lists.warnings).toEqual({
      shown: [
        { label: null, message: "This table's storage engine cannot roll back: …", reasons: null },
        { label: "Row id = 1", message: "Not checked for changes made since loading: price", reasons: "price: floating point" },
        // A new row's warning names it itself: no "New row: New row: …".
        { label: null, message: "New row: no value for code (NOT NULL, no default)", reasons: null },
      ],
      more: 0,
    });
    expect(previewLists(previewOf(), stagingOf(emptyStaged(["id"])), false).transactional).toBe(true);
  });

  it("puts the warning about the last unknown outcome first", () => {
    const preview = previewOf({ warnings: [unchecked("u:1", "price")] });
    const lists = previewLists(preview, stagingOf(emptyStaged(["id"])), true);
    expect(lists.warnings.shown.map((w) => w.message)).toEqual([UNKNOWN_BEFORE, preview.warnings[0].message]);
  });

  it("names a problem's row from the staging when the preview has no statements", () => {
    let staged = edited(7);
    staged = stage(staged, { type: "addRow", id: "i:2" });
    staged = stage(staged, { type: "deleteRows", rows: [loaded(8)] }, () => "d:8");
    const preview = previewOf({
      plan_id: null,
      statements: [],
      script: "",
      problems: [
        { change_id: "u:7", column: "a", code: "invalid_number", message: "Not a number" },
        { change_id: "i:2", column: "code", code: "not_nullable", message: "code is NOT NULL" },
        { change_id: "d:8", column: null, code: "key_incomplete", message: "Its key was not loaded completely" },
        { change_id: "x:9", column: null, code: "key_collision", message: "This change moves a row…" },
      ],
    });
    expect(previewLists(preview, stagingOf(staged), false).problems).toEqual({
      shown: [
        { changeId: "u:7", column: "a", label: "Row id = 7", message: "Not a number" },
        { changeId: "i:2", column: "code", label: "New row", message: "code is NOT NULL" },
        { changeId: "d:8", column: null, label: "Row id = 8", message: "Its key was not loaded completely" },
        { changeId: "x:9", column: null, label: "A change", message: "This change moves a row…" },
      ],
      more: 0,
    });
  });

  it("shows at most LIST_MAX items of a list and counts the rest", () => {
    const n = 10_000;
    const statements = Array.from({ length: n }, (_, i) => statement(`u:${i}`, "update", `UPDATE … ${i}`, `id = ${i}`));
    const warnings = statements.map((s) => unchecked(s.change_id, "price"));
    const lists = previewLists(previewOf({ statements, warnings }), stagingOf(emptyStaged(["id"])), true);
    expect(lists.warnings.shown).toHaveLength(LIST_MAX);
    expect(lists.warnings.shown[0].message).toBe(UNKNOWN_BEFORE);
    expect(lists.warnings.shown[1].label).toBe("Row id = 0");
    expect(lists.warnings.more).toBe(n - (LIST_MAX - 1));
    expect(moreText(lists.warnings.more)).toBe(`… and ${(n - LIST_MAX + 1).toLocaleString()} more`);
    expect(moreText(0)).toBeNull();
  });

  it("reads a preview of 10,000 changes in one pass, never a search per item", () => {
    const n = 10_000;
    const reads = { statements: 0, warnings: 0, staging: 0 };
    /** An array whose element reads are counted, and which has no `find` to search with. */
    const counted = <T,>(items: T[], name: "statements" | "warnings"): T[] =>
      new Proxy(items, {
        get(target, property, receiver) {
          if (property === "find" || property === "findIndex" || property === "filter") throw new Error(`${name}.${property}`);
          if (typeof property === "string" && /^\d+$/.test(property)) reads[name] += 1;
          return Reflect.get(target, property, receiver) as unknown;
        },
      });
    const statements = Array.from({ length: n }, (_, i) => statement(`u:${i}`, "update", `UPDATE … ${i}`, `id = ${i}`));
    const warnings = statements.map((s) => unchecked(s.change_id, "price"));
    // With problems the preview has no statements: every label comes from the staging, read once.
    let staged = emptyStaged(["id"]);
    for (let i = 0; i < 300; i += 1) staged = edited(i, staged);
    const updates = new Proxy(staged.updates, {
      get(target, property, receiver) {
        reads.staging += 1;
        return Reflect.get(target, property, receiver) as unknown;
      },
    });
    const problems = Array.from({ length: 300 }, (_, i) => ({ change_id: `u:${i}`, column: "a", code: "invalid_number" as const, message: "Not a number" }));
    previewLists(previewOf({ statements: counted(statements, "statements"), warnings: counted(warnings, "warnings") }), stagingOf(staged), false);
    expect(reads.statements).toBeLessThanOrEqual(n + 10);
    expect(reads.warnings).toBeLessThanOrEqual(n + LIST_MAX + 10);
    const lists = previewLists(previewOf({ plan_id: null, statements: [], problems }), { ...stagingOf(staged), updates }, false);
    expect(lists.problems.shown).toHaveLength(LIST_MAX);
    expect(lists.problems.more).toBe(300 - LIST_MAX);
    expect(lists.problems.shown[LIST_MAX - 1].label).toBe(`Row id = ${LIST_MAX - 1}`);
    expect(reads.staging).toBeLessThanOrEqual(300 + 10);
  });

  it("names the failed row of a result from its statement, else from the staging", () => {
    const staged = edited(5);
    const preview = previewOf({ statements: [statement("u:1", "update", "UPDATE …", "id = 1"), statement("i:2", "insert", "INSERT …")] });
    expect(rowLabel(preview, stagingOf(staged), "u:1")).toBe("Row id = 1");
    expect(rowLabel(preview, stagingOf(staged), "i:2")).toBe("New row");
    expect(rowLabel(preview, stagingOf(staged), "u:5")).toBe("Row id = 5");
    expect(rowLabel(preview, stagingOf(staged), "u:404")).toBe("A change");
  });
});

describe("the applying dialog (pre-flight M1, M2, ruling K11)", () => {
  const waiting = { phase: "open" as const, counted: 0, statements: 3, quietMs: 0, sinceCancelMs: null };

  it("counts the statements, then waits for COMMIT", () => {
    expect(applyingView(waiting).progress).toBe("Committing… statement 1 of 3");
    expect(applyingView({ ...waiting, counted: 2 }).progress).toBe("Committing… statement 3 of 3");
    expect(applyingView({ ...waiting, counted: 3 }).progress).toBe("Committing…");
    expect(applyingView({ ...waiting, counted: 1233, statements: 10_000 }).progress).toBe(
      "Committing… statement 1,234 of 10,000",
    );
  });

  it("offers Cancel until every statement's count is in (then COMMIT is on its way)", () => {
    expect(applyingView(waiting).canCancel).toBe(true);
    expect(applyingView({ ...waiting, counted: 2 }).canCancel).toBe(true);
    expect(applyingView({ ...waiting, counted: 3 }).canCancel).toBe(false);
  });

  it("offers to close with the outcome unknown only once it may never come", () => {
    expect(applyingView(waiting).closeUnknown).toBe(false);
    expect(applyingView({ ...waiting, quietMs: 10 * 60_000 }).closeUnknown).toBe(false); // a statement runs: Cancel
    expect(applyingView({ ...waiting, phase: "reconnecting" }).closeUnknown).toBe(true);
    expect(applyingView({ ...waiting, counted: 3, quietMs: OUTCOME_WAIT_MS }).closeUnknown).toBe(true);
    expect(applyingView({ ...waiting, counted: 3, quietMs: OUTCOME_WAIT_MS - 1 }).closeUnknown).toBe(false);
    expect(applyingView({ ...waiting, quietMs: OUTCOME_WAIT_MS, sinceCancelMs: OUTCOME_WAIT_MS }).closeUnknown).toBe(true);
  });
});
