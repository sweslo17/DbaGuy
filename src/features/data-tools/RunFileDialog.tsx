import { useEffect, useEffectEvent, useId, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Radio } from "@/components/ui/Field";
import { FilePathField } from "@/components/ui/FilePathField";
import { toast } from "@/components/ui/Toast";
import { ApiError, errorMessage } from "@/lib/api/client";
import type { FileInspection } from "@/lib/api/types";
import { BROWSER_OPEN_HINT, pathProblem } from "@/lib/filePaths";
import { formatSize } from "@/lib/format";
import { chooseOpenPath } from "@/lib/native";
import { useEngineInfo, useProfiles } from "../connections/hooks";
import { closeDataDialog, openJobDialog } from "./dataDialogs";
import type { RunEntry } from "./entryPoints";
import { FileLines } from "./FileLines";
import { inspectRunFile } from "./jobsApi";
import { ignoreHeldReturn } from "./JobViews";
import { startJob } from "./jobsStore";
import {
  EVERY_STATEMENT_RUNS,
  fromStatementProblem,
  initialRunForm,
  inspectionNotes,
  pickMode,
  pickOnError,
  previewNote,
  queryContextLabel,
  queryContextOf,
  READ_ONLY_RUN,
  RED_CONFIRM,
  retargeted,
  runChoices,
  runCommand,
  runGate,
  runTitle,
  settledMode,
  withDatabase,
  withPath,
  withSearchPath,
  type OnError,
  type RunForm,
} from "./runForm";
import { TargetPicker } from "./TargetPicker";

/** What the dialog knows of the file: read for one connection and one path (`inspectRunFile`, no connection made). */
type Inspection =
  | { status: "idle" }
  | { status: "reading"; profileId: string; path: string }
  | { status: "read"; profileId: string; path: string; file: FileInspection }
  /** `retry`: reading again may help (not for a read-only connection, which refuses every read). */
  | { status: "failed"; profileId: string; path: string; message: string; retry: boolean };

const IDLE: Inspection = { status: "idle" };
const FROM_STATEMENT_HINT = "Statements before it are read, not sent; their SET, USE and PRAGMA settings still are";
const ON_ERROR: { value: OnError; label: string }[] = [
  { value: "stop", label: "Stop at the error" },
  { value: "continue", label: "Skip it and continue" },
];

/**
 * Why the file could not be read: a read-only connection is said as the dialog says it (and reading again cannot
 * help); else the backend's words (404: "<path> is not a file").
 */
function readFailure(error: unknown): { message: string; retry: boolean } {
  return error instanceof ApiError && error.status === 403
    ? { message: READ_ONLY_RUN, retry: false }
    : { message: errorMessage(error), retry: true };
}

/** The inspection is of this form's file on this form's connection. */
const isOf = (inspection: Inspection, form: RunForm) =>
  inspection.status !== "idle" && inspection.profileId === form.profileId && inspection.path === form.path.trim();

/**
 * Run SQL file… (spec ⑥ §3.6 RunFileDialog): where (the connection and its database; PostgreSQL's search path,
 * Couchbase's query context), which file (read first: its size, its DbaGuy header, its warnings, its first lines), how
 * (all or nothing, or statement by statement and what an error does; where to start), and on a red connection the box
 * (CR-3). Return in the path reads the file, never runs it (V5): once read, the focus is on Run, and Return runs. Run
 * starts the job on a session of its own and turns this dialog into the job's view.
 */
export function RunFileDialog({ entry }: { entry: RunEntry }) {
  const profiles = useProfiles();
  const [form, setForm] = useState<RunForm>(() => initialRunForm(entry, null));
  const engine = useEngineInfo(form.profileId);
  const profile = profiles.data?.find((item) => item.profile.id === form.profileId)?.profile ?? null;

  // The file as read for this form: one read at a time, a late answer (another path, another connection) dropped
  const [inspection, setInspection] = useState<Inspection>(IDLE);
  const reads = useRef(0);
  const inspected = inspection.status === "read" && isOf(inspection, form) ? inspection.file : null;

  // Until the user picks a mode it follows the engine as it loads or the target changes; a mode greyed here (all or
  // nothing without transactions, or for a dump cut short) moves to statement by statement (06b T4 re-review)
  const settled = settledMode(form, engine, inspected);
  if (settled !== form) setForm(settled);

  const choices = runChoices(engine, inspected, form);
  const notes = inspectionNotes(inspected);
  const gate = runGate(profile, engine, form, inspected !== null);
  const queryContext = queryContextOf(entry, form);
  const modeGroup = useId();
  const onErrorGroup = useId();
  const fromId = useId();
  const searchPathId = useId();
  const confirmId = useId();

  // Where the focus goes after a read that Return, a chosen file or the dialog's opening asked for: Run when it can be
  // pressed; on a red connection that waits only for its box, the box (Space checks it, and the focus moves to Run)
  const runRef = useRef<HTMLButtonElement>(null);
  const [focusAsked, setFocusAsked] = useState(0);
  /** The path was typed since it was last read: leaving the field reads it. */
  const pathChanged = useRef(false);

  /** Reads the file for a connection; `focusRun`: once read, Run takes the focus when it can be pressed (V5, M6). */
  const read = (profileId: string | null, path: string, focusRun: boolean) => {
    const target = path.trim();
    const seq = ++reads.current;
    pathChanged.current = false;
    if (!profileId || !target || pathProblem(target)) {
      setInspection(IDLE);
      return;
    }
    setInspection({ status: "reading", profileId, path: target });
    inspectRunFile(profileId, target).then(
      (file) => {
        if (seq !== reads.current) return;
        setInspection({ status: "read", profileId, path: target, file });
        if (focusRun) setFocusAsked((n) => n + 1);
      },
      (error: unknown) => {
        if (seq !== reads.current) return;
        setInspection({ status: "failed", profileId, path: target, ...readFailure(error) });
      },
    );
  };

  // A typed path is read on Return or as the field is left; a change drops what was read of the path before
  const changePath = (path: string) => {
    pathChanged.current = true;
    reads.current++;
    setInspection(IDLE);
    setForm((current) => withPath(current, path)); // another file: a red connection's box is asked again (CR-3)
  };

  const focusNext = useEffectEvent(() => {
    if (gate.blocked === null) {
      runRef.current?.focus();
    } else if (gate.red && !form.confirmed) {
      const onlyTheBox = runGate(profile, engine, { ...form, confirmed: true }, inspected !== null).blocked === null;
      if (onlyTheBox) document.getElementById(confirmId)?.focus();
    }
  });
  useEffect(() => {
    if (focusAsked > 0) focusNext();
  }, [focusAsked]);

  // A prefilled file (a script, a run again) is read as the dialog opens
  const readAtOpen = useEffectEvent(() => {
    if (entry.path.trim()) read(form.profileId, form.path, true);
  });
  useEffect(() => {
    readAtOpen();
  }, []);

  const changeTarget = (profileId: string, database: string | null) => {
    if (profileId !== form.profileId) {
      // CR-3: the box was for the connection before; what belonged to it goes (its database, its search path)
      const next = retargeted(form, profileId, entry);
      setForm(next);
      if (next.path.trim()) read(profileId, next.path, false); // the inspection says what this target makes of the file
    } else {
      setForm(withDatabase(form, database)); // another database of the same connection: the box is asked again too
    }
  };

  const choose = async (): Promise<string | null> => {
    try {
      return await chooseOpenPath(); // null: the panel was cancelled
    } catch (error) {
      toast("error", `Could not open the file panel: ${errorMessage(error)}`);
      return null;
    }
  };

  const run = () => {
    if (gate.blocked !== null || !profile || !engine) return;
    const requestId = crypto.randomUUID();
    const command = runCommand(form, engine, queryContext, requestId);
    startJob({
      kind: "run_file",
      profileId: profile.id,
      database: form.database,
      title: runTitle(command.path, profile.name),
      command,
      entry: {
        ...entry,
        profileId: profile.id,
        database: form.database,
        searchPath: form.searchPath.trim() || null,
        queryContext,
        path: command.path,
      },
    });
    openJobDialog(requestId);
  };

  const summary = fileSummary(isOf(inspection, form) ? inspection : IDLE, form, notes, () =>
    read(form.profileId, form.path, false),
  );

  // An all-or-nothing run stops at the first error, whatever the form keeps for statement by statement (runCommand)
  const onError: OnError = form.mode === "all_or_nothing" ? "stop" : form.onError;

  const footer = (
    <>
      <Button variant="ghost" onClick={closeDataDialog}>
        Close
      </Button>
      <Button
        ref={runRef}
        variant={gate.red ? "danger" : "primary"}
        onClick={run}
        // the Return that read the file may still be held as the focus lands here: its repeats never run the file
        onKeyDown={ignoreHeldReturn}
        disabled={gate.blocked !== null}
        title={gate.blocked ?? undefined}
      >
        Run
      </Button>
    </>
  );

  return (
    <Dialog open onOpenChange={(open) => !open && closeDataDialog()} title="Run a SQL file" width={680} footer={footer}>
      <div className="flex flex-col gap-3">
        {entry.note && (
          <p className="text-[12px] text-warning" data-selectable>
            {entry.note}
          </p>
        )}
        <FieldRow label="Connection">
          <TargetPicker profileId={form.profileId} database={form.database} onChange={changeTarget} />
        </FieldRow>
        {choices.searchPath && (
          <FieldRow label="Search path" htmlFor={searchPathId} hint="One schema">
            <Input
              id={searchPathId}
              value={form.searchPath}
              // another search path: a red connection's box is asked again (CR-3: the file's names land there)
              onChange={(e) => setForm((current) => withSearchPath(current, e.target.value))}
              placeholder="the connection's default"
              className="font-mono"
            />
          </FieldRow>
        )}
        {engine?.query_context && (
          <FieldRow label="Query context">
            <p className="text-[12px]" data-selectable>
              {queryContextLabel(queryContext)}
            </p>
          </FieldRow>
        )}
        <FilePathField
          label="File"
          value={form.path}
          onChange={changePath}
          onEnter={() => read(form.profileId, form.path, true)}
          onBlur={() => {
            if (pathChanged.current) read(form.profileId, form.path, false);
          }}
          choose={choose}
          onChosen={(path) => read(form.profileId, path, true)}
          placeholder="/path/to/file.sql"
          browserHint={BROWSER_OPEN_HINT}
          error={pathProblem(form.path)}
          autoFocus
        />
        {summary && <FieldRow label="">{summary}</FieldRow>}
        <FieldRow label="Mode">
          <div className="flex flex-col gap-1">
            {choices.modes.map((choice) => (
              <div key={choice.value} className="flex flex-col">
                <Radio
                  name={modeGroup}
                  label={choice.label}
                  checked={form.mode === choice.value}
                  disabled={choice.disabled !== null}
                  title={choice.disabled ?? undefined}
                  onChange={() => setForm((current) => pickMode(current, choice.value))}
                />
                {(choice.disabled ?? choice.note) && (
                  <span className="pl-5 text-[11px] text-muted">{choice.disabled ?? choice.note}</span>
                )}
              </div>
            ))}
          </div>
        </FieldRow>
        <FieldRow label="On error">
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {ON_ERROR.map((choice) => {
                // "Skip it and continue" is greyed (unchecked) where it cannot apply: all or nothing stops at the first error
                const greyed = choice.value === "continue" ? choices.skip : null;
                return (
                  <Radio
                    key={choice.value}
                    name={onErrorGroup}
                    label={choice.label}
                    checked={greyed === null && onError === choice.value}
                    disabled={greyed !== null}
                    title={greyed ?? undefined}
                    onChange={() => setForm((current) => pickOnError(current, choice.value))}
                  />
                );
              })}
            </div>
            {choices.skip && <span className="text-[11px] text-muted">{choices.skip}</span>}
          </div>
        </FieldRow>
        <FieldRow
          label="Start at statement"
          htmlFor={fromId}
          hint={FROM_STATEMENT_HINT}
          error={fromStatementProblem(form.fromStatement)}
        >
          <Input
            id={fromId}
            value={form.fromStatement}
            onChange={(e) => setForm((current) => ({ ...current, fromStatement: e.target.value }))}
            inputMode="numeric"
            aria-invalid={fromStatementProblem(form.fromStatement) !== null}
            className="w-24 tabular-nums"
          />
        </FieldRow>
        <FieldRow label="">
          <div className="flex flex-col gap-2">
            <p className="text-[12px] text-muted">{EVERY_STATEMENT_RUNS}</p>
            {gate.red && (
              <Checkbox
                id={confirmId}
                label={<span className="font-medium text-danger">{RED_CONFIRM}</span>}
                checked={form.confirmed}
                onChange={(e) => {
                  const confirmed = e.target.checked;
                  setForm((current) => ({ ...current, confirmed }));
                  if (confirmed) setFocusAsked((n) => n + 1); // checked: Run takes the focus when it can be pressed
                }}
              />
            )}
          </div>
        </FieldRow>
      </div>
    </Dialog>
  );
}

/**
 * What was read of the file, or why not (yet): its size, its DbaGuy header, its warnings (F2's first, in red), its
 * first lines. Null when there is nothing to say (no path yet).
 */
function fileSummary(
  inspection: Inspection,
  form: RunForm,
  notes: { prominent: string[]; other: string[] },
  retry: () => void,
): ReactNode {
  switch (inspection.status) {
    case "idle":
      if (!form.path.trim() || pathProblem(form.path)) return null;
      return (
        <p className="text-[12px] text-muted">
          {form.profileId ? "Press Return to read the file" : "Choose a connection: the file is read for it"}
        </p>
      );
    case "reading":
      return <p className="animate-delayed-show text-[12px] text-muted">Reading the file…</p>;
    case "failed":
      return (
        <div className="flex items-start gap-2 text-[12px] text-danger">
          <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
            {inspection.message}
          </span>
          {inspection.retry && (
            <Button size="sm" onClick={retry}>
              Read again
            </Button>
          )}
        </div>
      );
    case "read": {
      const { file } = inspection;
      const shown = previewNote(file);
      return (
        <div className="flex flex-col gap-1.5 text-[12px]">
          <p className="text-muted tabular-nums">{formatSize(file.size)}</p>
          {file.header && file.header.lines.length > 0 && (
            <pre className="font-mono text-[11px] whitespace-pre-wrap text-muted [overflow-wrap:anywhere]" data-selectable>
              {file.header.lines.join("\n")}
            </pre>
          )}
          {notes.prominent.map((warning, i) => (
            <p
              key={i}
              className="rounded-md border border-danger/40 bg-danger/10 p-2 font-medium whitespace-pre-wrap text-danger [overflow-wrap:anywhere]"
              data-selectable
            >
              {warning}
            </p>
          ))}
          {notes.other.length > 0 && (
            <ul className="flex list-disc flex-col gap-1 pl-4 text-warning">
              {notes.other.map((warning, i) => (
                <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
                  {warning}
                </li>
              ))}
            </ul>
          )}
          {file.preview && <FileLines text={file.preview} firstLine={1} label="The file's first lines" />}
          {shown && <p className="text-[11px] text-muted">{shown}</p>}
        </div>
      );
    }
  }
}
