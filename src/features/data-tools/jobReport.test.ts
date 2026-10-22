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

  it("keeps the run's own on-error choice in the again patch", () => {
    const stopped = outcome({
      outcome: "stopped",
      stopped_at: { line: 8, statement: 4 },
      resume_at: { line: 8, statement: 4 },
      error: { line: 8, statement: 4, code: "42000", message: "boom" },
    });
    expect(report(stopped, run({ on_error: "continue" })).actions.map((a) => (a.kind === "again" ? a.patch?.on_error : null))).toEqual([
      "continue",
      "continue",
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

  it("does not count statements as applied when the run cannot say what ran before the stop", () => {
    const unclear = outcome({ outcome: "unknown", statements: 12, stopped_at: { line: 3, statement: 3 }, resume_at: null });
    expect(report(unclear).detail).toBe("What was applied is not known: check the target.");
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
    expect(said.hint).toBe(
      "Another tab still has an open result of this database file, or another program is using it: finish or close that result, then run the file again.",
    );
  });

  it("gives the lost-connection hint only where it is true: an unknown run must be checked, a rolled-back one need not", () => {
    const lost = { code: "connection_lost", message: "Connection to the server was lost" };
    const rolledBack = report(outcome({ outcome: "rolled_back", error: lost }), run({ mode: "all_or_nothing" }));
    expect([rolledBack.title, rolledBack.hint]).toEqual(["Nothing was applied: the transaction was rolled back", null]);
    const unknown = report(outcome({ outcome: "unknown", error: lost }), run({ mode: "all_or_nothing" }));
    expect(unknown.hint).toContain("Check the target");
  });

  it("looks codes up as own properties only: constructor and __proto__ are no codes", () => {
    for (const code of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
      expect(codeLabel(code), code).toBeNull();
      const said = report(outcome({ outcome: "rolled_back", error: { code, message: "x" } }), run({ mode: "all_or_nothing" }));
      expect([said.error?.label, said.hint], code).toEqual([null, null]);
    }
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

  it("offers no skip after a Cancel: there is no failure to skip, running it again from there stays (fix 6)", () => {
    const cancelled = outcome({
      outcome: "stopped",
      cancelled: true,
      statements: 40,
      stopped_at: { line: 90, statement: 41 },
      resume_at: { line: 90, statement: 41 },
      error: { line: 90, statement: 41, code: "cancelled", message: "Cancelled" },
    });
    const said = report(cancelled);
    expect([said.tone, said.title, said.detail]).toEqual(["warning", "Cancelled at line 90", "40 statements applied"]);
    expect(said.actions.map((a) => a.label)).toEqual(["Run again from line 90"]);
  });

  it("does not promise a count it cannot stand behind: counted as applied, see the notes (fix 2)", () => {
    const notes = ["Line 4 may have committed or rolled back the file's transaction from line 2: the statements from there on may or may not be applied"];
    const stopped = outcome({ outcome: "stopped", statements: 12, stopped_at: { line: 9, statement: 6 }, resume_at: null, warnings: notes });
    expect(report(stopped).detail).toBe("12 statements counted as applied; see the notes");
    expect(report(stopped).notes).toEqual(notes);
    const completed = outcome({ statements: 12, rows: 5, resume_at: null, warnings: notes });
    expect(report(completed).detail).toBe("12 statements counted as applied; see the notes · 5 rows affected");
    // without notes, in one transaction, or with a place to run again from, the plain count stands
    expect(report(outcome({ outcome: "stopped", statements: 12, resume_at: null, stopped_at: { line: 9, statement: 6 } })).detail).toBe("12 statements applied");
    expect(report(completed, run({ mode: "all_or_nothing" })).detail).toBe("12 statements applied · 5 rows affected");
    expect(report(outcome({ statements: 12, resume_at: { line: 2, statement: 2 }, warnings: notes })).detail).toBe("12 statements applied · 0 rows affected");
  });

  it("names the line a lost connection's rolled-back file transaction began at, not the line that was running (fix 12)", () => {
    const lost = outcome({
      outcome: "unknown",
      statements: 1,
      stopped_at: { line: 40, statement: 30 },
      resume_at: { line: 2, statement: 2 },
      error: { line: 40, statement: 30, code: "connection_lost", message: "Connection to the server was lost" },
    });
    const said = report(lost);
    expect(said.detail).toBe("1 statement applied before line 2; the file's transaction from there was rolled back by the server when the connection was lost at line 40.");
    expect(said.actions.map((a) => a.label)).toEqual(["Run again from line 2"]);
  });

  it("tells a run that did not start: cancelled, or refused", () => {
    expect(report(outcome({ outcome: "not_started", cancelled: true })).title).toBe("Cancelled before anything ran");
    const refused = report(outcome({ outcome: "not_started", error: { code: "read_only", message: "This connection is read-only" } }));
    expect([refused.tone, refused.title, refused.error?.label, refused.actions.map((a) => a.label)]).toEqual([
      "danger",
      "Not started: nothing was run",
      "Read-only connection",
      ["Run again…"],
    ]);
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

  it("does not say 0 tables of a dump that wrote only the schema (pre-flight L3)", () => {
    expect(report(dumped({ bytes: 2048 }), dump).detail).toBe("2 KB · /tmp/d.sql");
  });

  it("says nothing was written when it failed or was cancelled, and offers to dump again", () => {
    const failed = report(dumped({ outcome: "failed", error: { code: "dump_io", message: "Cannot write /tmp/d.sql: Disk full. Nothing was written" } }), dump);
    expect([failed.title, failed.error?.label, failed.actions.map((a) => a.label)]).toEqual(["Dump failed: nothing was written", "The file cannot be written", ["Dump again…"]]);
    expect(report(dumped({ outcome: "failed", cancelled: true }), dump).title).toBe("Dump cancelled: nothing was written");
    const filter = report(dumped({ outcome: "not_started", error: { code: "invalid_filter", message: "The filter must be a condition on the table's rows (what follows WHERE), and nothing else" } }), dump);
    expect([filter.title, filter.hint]).toEqual(["The dump did not start", FILTER_RULE]);
  });

  it("says a dump whose outcome is unknown must be checked, and offers to dump again", () => {
    const unknown = report(dumped({ outcome: "unknown" }), dump);
    expect([unknown.tone, unknown.title, unknown.detail, unknown.actions.map((a) => a.label)]).toEqual([
      "danger",
      "The dump's outcome is unknown",
      "Check whether /tmp/d.sql was written.",
      ["Dump again…"],
    ]);
  });

  it("says a lost connection wrote nothing, for a dump; not the run's check-the-target hint", () => {
    const lost = { code: "connection_lost", message: "Connection to the server was lost" };
    expect(report(dumped({ outcome: "failed", error: lost }), dump).hint).toBe("The connection was lost: nothing was written. Dump again when it is back.");
    expect(report(dumped({ outcome: "not_started", error: lost }), dump).hint).toContain("nothing was written");
    expect(report(dumped({ outcome: "unknown", error: lost }), dump).hint).toBeNull();
  });

  it("offers no dump again for a job listed after a reload (its command is not here; pre-flight L4)", () => {
    // a listed job has no command: its kind comes from the backend's list, not from what was sent
    const listed = (fields: Partial<JobOutcomeEvent>) => jobReport({ kind: "dump", command: null, state: ended(dumped(fields)) })!;
    expect(listed({ outcome: "failed" }).actions).toEqual([]);
    expect(listed({ outcome: "not_started" }).actions).toEqual([]);
    expect(listed({ outcome: "unknown" }).actions).toEqual([]);
    expect(listed({ outcome: "failed" }).title).toBe("Dump failed: nothing was written");
    // what was written is still where it was: Show in Finder needs no command
    expect(listed({ rows: 1, objects: [{ kind: "table", name: "a", rows: 1 }] }).actions.map((a) => a.kind)).toEqual(["show_in_finder"]);
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

  it("says statement by statement how far it had come, without promising what a file's open transaction kept (pre-flight M5)", () => {
    expect(lost(sentState({ statements: 40 }), run()).detail).toBe(
      "About 40 statements had run by then (the last progress); any in a transaction the file had open are rolled back by the server, and the statement running may or may not have taken effect.",
    );
  });

  it("says a request that finished without an outcome did exactly that", () => {
    const finished = jobReducer(jobReducer(openingJob(0), { type: "sent", at: 1 }), {
      type: "server",
      at: 2,
      event: { type: "finished", request_id: "r1", elapsed_ms: 5, in_transaction: false, transaction_failed: false, cancelled: false, timed_out: false, skipped: false },
    });
    const said = lost(finished, run());
    expect([said.tone, said.title]).toEqual(["danger", "The job finished without reporting an outcome"]);
    expect(said.title).not.toBe(lost(sentState(), run()).title);
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

  it("offers to run a command that never left again, and nothing for one this page does not hold", () => {
    const notSent = jobReducer(openingJob(0), { type: "session_gone", at: 3 });
    expect(lost(notSent, run()).actions.map((a) => a.label)).toEqual(["Run again…"]);
    expect(lost(notSent, null).actions).toEqual([]);
  });

  it("says a job not listed may never have started, and a run's work then is not known (pre-flight M2)", () => {
    const state = jobReducer(jobReducer(openingJob(0), { type: "sent", at: 1 }), { type: "not_listed", at: 20_000 });
    const said = lost(state, run({ mode: "all_or_nothing" }));
    expect(said.title).toBe("The backend has no record of this job: it restarted, or never got the command");
    expect(said.tone).toBe("danger");
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

  it("calls a busy session what the backend raises it for: a closed one", () => {
    expect(codeLabel("session_busy")).toBe("The job's session had closed");
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

  it("stays quiet for a job that was cancelled elsewhere: the outcome says so (fix 7)", () => {
    expect(jobNotification(titled(long(outcome({ outcome: "rolled_back", cancelled: true }))), 15, false)).toBeNull();
    expect(jobNotification(titled(long(outcome({ job: "dump", outcome: "failed", cancelled: true })), dump), 15, false)).toBeNull();
  });
});
