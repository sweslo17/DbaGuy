import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toast";
import { ApiError, errorMessage } from "@/lib/api/client";
import type { FileExcerpt } from "@/lib/api/types";
import { fileNameOf } from "@/lib/filePaths";
import { useCancelAgain } from "@/lib/useCancelAgain";
import { RecoverableError } from "../connections/RecoverableError";
import { closeDataDialog, openDumpDialog, openRunDialog } from "./dataDialogs";
import { dumpFormFromCommand } from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";
import { cancelNote, jobReport, type JobAction } from "./jobReport";
import { FileLines } from "./FileLines";
import { fileExcerpt, revealFile } from "./jobsApi";
import { cancelJob, isRunning, retryJob, type JobRecord } from "./jobsStore";
import { ConnectionLine, ignoreHeldReturn, JobProgressView, JobReportView, ReportTitle } from "./JobViews";
import { againEntry } from "./runForm";

const KEEPS_RUNNING = {
  dump: "The dump keeps running: open it again from Jobs, at the bottom of the sidebar",
  run_file: "The SQL file keeps running: open it again from Jobs, at the bottom of the sidebar",
  copy_data: "The copy keeps running: open it again from Jobs, at the bottom of the sidebar",
} as const;
/** The jobs whose dialog was closed while they ran and that were told so (once each). */
const toldKeepsRunning = new Set<string>();

const isDumpEntry = (entry: DumpEntry | RunEntry | null): entry is DumpEntry => entry !== null && "target" in entry;

async function reveal(path: string): Promise<void> {
  try {
    await revealFile(path);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      toast("info", "The file is no longer there, or its job was cleared from Jobs");
    } else {
      toast("error", errorMessage(error));
    }
  }
}

/**
 * One outcome action as a button. Every "again" goes back to the form, prefilled, never straight to a run (V2): the
 * form reads the file again and asks a red connection's box again; only a job this page started has its command (V6).
 */
function actionButton(record: JobRecord, action: JobAction, key: number) {
  if (action.kind === "show_in_finder") {
    return (
      <Button key={key} onClick={() => void reveal(action.path)}>
        {action.label}
      </Button>
    );
  }
  const { entry, command } = record;
  if (record.kind === "run_file" && command?.type === "run_file") {
    // From where the outcome says (its resume point, with its line when exact), or as it was
    const again = againEntry({ profileId: record.profileId, database: record.database, command }, action.patch, action.note);
    return (
      <Button key={key} onClick={() => openRunDialog(again)}>
        {action.label}
      </Button>
    );
  }
  // Dump again…: back to the form with the job's own choices
  if (record.kind !== "dump" || !isDumpEntry(entry) || command?.type !== "dump") return null;
  return (
    <Button key={key} onClick={() => openDumpDialog(entry, dumpFormFromCommand(entry, command))}>
      {action.label}
    </Button>
  );
}

/** An error's place in the run's file: being read, shown, or why it cannot be. */
type Excerpt =
  | { status: "reading"; line: number }
  | { status: "shown"; line: number; excerpt: FileExcerpt }
  | { status: "failed"; line: number; text: string };

/** The lines around an error of a run (`POST /data/run-file/excerpt`, at most 60 lines / 64 KiB): one at a time. */
function useExcerpt(path: string | null) {
  const [excerpt, setExcerpt] = useState<Excerpt | null>(null);
  const reads = useRef(0);
  const show = (line: number) => {
    if (!path) return;
    const seq = ++reads.current; // a click on another line drops the answer for this one
    setExcerpt({ status: "reading", line });
    fileExcerpt(path, line).then(
      (got) => {
        if (seq !== reads.current) return;
        setExcerpt(
          got.found
            ? { status: "shown", line, excerpt: got }
            : { status: "failed", line, text: "The file has fewer lines now" },
        );
      },
      (error: unknown) => {
        if (seq !== reads.current) return;
        const gone = error instanceof ApiError && error.status === 404;
        setExcerpt({ status: "failed", line, text: gone ? "The file is no longer there" : errorMessage(error) });
      },
    );
  };
  const hide = () => {
    reads.current++;
    setExcerpt(null);
  };
  return { excerpt, show: path ? show : undefined, hide };
}

function ExcerptView({ excerpt, path, hide }: { excerpt: Excerpt; path: string; hide: () => void }) {
  return (
    <div className="flex flex-col gap-1 text-[12px]">
      <div className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-muted" title={path}>
          Line {excerpt.line.toLocaleString()} of {fileNameOf(path)}
        </span>
        <Button size="sm" variant="ghost" className="shrink-0" onClick={hide}>
          Hide
        </Button>
      </div>
      {excerpt.status === "reading" ? (
        <p className="animate-delayed-show text-muted">Reading the file…</p>
      ) : excerpt.status === "failed" ? (
        <p className="text-warning" data-selectable>
          {excerpt.text}
        </p>
      ) : (
        <FileLines
          text={excerpt.excerpt.text}
          firstLine={excerpt.excerpt.first_line}
          mark={excerpt.line}
          label={`The file's lines around line ${excerpt.line.toLocaleString()}`}
        />
      )}
    </div>
  );
}

/**
 * A data-tools job's dialog (spec ⑥ §3.6): its progress while it runs, then its outcome and what can be done next.
 * Closing it (Esc, Close) never stops the job: the first time for each job the toast says where to find it again.
 * Cancel is said beside the button for what it does to this job.
 */
export function JobDialog({ record }: { record: JobRecord }) {
  const { requestId, state } = record;
  const running = isRunning(record);
  const report = running ? null : jobReport(record);
  const warnings = state.messages.filter((message) => message.severity === "warning").length;
  // "Cancelling…" for a second after a click or until a new warning, as the export's; a view opened again right after
  // its job's Cancel keeps it for the rest of that second (the job's own cancel time)
  const cancelAgain = useCancelAgain(warnings, state.cancelRequestedAt);
  const cancelling = cancelAgain.cancelling;

  const close = () => {
    if (running && !toldKeepsRunning.has(requestId)) {
      toldKeepsRunning.add(requestId);
      toast("info", KEEPS_RUNNING[record.kind]);
    }
    closeDataDialog();
  };
  const cancel = () => {
    cancelJob(requestId);
    cancelAgain.sent();
  };

  // A run's error is shown in its file: the file the backend read (the outcome's), else the one sent
  const runPath =
    record.kind === "run_file"
      ? (state.outcome?.path ?? (record.command?.type === "run_file" ? record.command.path : null))
      : null;
  const lines = useExcerpt(runPath);

  const note = cancelNote(record);
  // Each Close has its own key: the one shown once the job ended mounts anew and takes the focus (Return closes)
  const footer = running ? (
    <>
      {note && <span className="mr-auto self-center text-[12px] text-muted">{note}</span>}
      {state.progress?.stage === "commit" ? (
        <Button disabled>Committing…</Button>
      ) : (
        <Button onClick={cancel} disabled={cancelling}>
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      )}
      <Button key="close-running" variant="primary" onClick={close} onKeyDown={ignoreHeldReturn} autoFocus>
        Close
      </Button>
    </>
  ) : (
    <>
      {report?.actions.map((action, i) => actionButton(record, action, i))}
      <Button key="close-ended" variant="primary" onClick={close} onKeyDown={ignoreHeldReturn} autoFocus>
        Close
      </Button>
    </>
  );

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && close()}
      title={record.title}
      width={record.kind === "dump" ? 560 : 640}
      footer={footer}
    >
      <div className="flex flex-col gap-3">
        <ConnectionLine profileId={record.profileId} database={record.database} />
        {running ? (
          <JobProgressView record={record} />
        ) : state.loss === "open_failed" ? (
          <div className="flex flex-col gap-2">
            {report && <ReportTitle report={report} />}
            {/* The session could not open: a missing password asks for it, then the job starts again (nothing was sent) */}
            <RecoverableError profileId={record.profileId} error={state.openError} onRetry={() => retryJob(requestId)} />
          </div>
        ) : (
          <>
            <JobReportView record={record} onShowLine={lines.show} />
            {lines.excerpt && runPath && <ExcerptView excerpt={lines.excerpt} path={runPath} hide={lines.hide} />}
          </>
        )}
      </div>
    </Dialog>
  );
}
