import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toast";
import { ApiError, errorMessage } from "@/lib/api/client";
import { RecoverableError } from "../connections/RecoverableError";
import { closeDataDialog, openDumpDialog } from "./dataDialogs";
import { dumpFormFromCommand } from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";
import { cancelNote, jobReport, type JobAction } from "./jobReport";
import { revealFile } from "./jobsApi";
import { cancelJob, isRunning, retryJob, type JobRecord } from "./jobsStore";
import { ConnectionLine, JobProgressView, JobReportView, ReportTitle } from "./JobViews";

/**
 * Cancel comes back this long after a click, or as soon as the job gives a new warning (SQL Server: that the cancel
 * cannot stop it safely, and what another Cancel does), as the export's does: a second Cancel within it would be read
 * as a double click.
 */
const CANCEL_AGAIN_MS = 1_000;

const KEEPS_RUNNING = {
  dump: "The dump keeps running: open it again from Jobs, at the bottom of the sidebar",
  run_file: "The SQL file keeps running: open it again from Jobs, at the bottom of the sidebar",
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

/** One outcome action as a button; null for those this dialog does not offer yet (a run's again: Task 7). */
function actionButton(record: JobRecord, action: JobAction, key: number) {
  if (action.kind === "show_in_finder") {
    return (
      <Button key={key} onClick={() => void reveal(action.path)}>
        {action.label}
      </Button>
    );
  }
  // Dump again…: back to the form with the job's own choices; only a job this page started has them (V2, V6)
  const { entry, command } = record;
  if (record.kind !== "dump" || !isDumpEntry(entry) || command?.type !== "dump") return null;
  return (
    <Button key={key} onClick={() => openDumpDialog(entry, dumpFormFromCommand(entry, command))}>
      {action.label}
    </Button>
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
  // The last Cancel click and how many warnings the job had then: "Cancelling…" until CANCEL_AGAIN_MS or a new warning
  const [cancelSent, setCancelSent] = useState<{ warnings: number } | null>(null);
  useEffect(() => {
    if (!cancelSent) return;
    const timer = window.setTimeout(() => setCancelSent(null), CANCEL_AGAIN_MS);
    return () => window.clearTimeout(timer);
  }, [cancelSent]);
  const cancelling = cancelSent !== null && cancelSent.warnings === warnings;

  const close = () => {
    if (running && !toldKeepsRunning.has(requestId)) {
      toldKeepsRunning.add(requestId);
      toast("info", KEEPS_RUNNING[record.kind]);
    }
    closeDataDialog();
  };
  const cancel = () => {
    cancelJob(requestId);
    setCancelSent({ warnings });
  };

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
      <Button key="close-running" variant="primary" onClick={close} autoFocus>
        Close
      </Button>
    </>
  ) : (
    <>
      {report?.actions.map((action, i) => actionButton(record, action, i))}
      <Button key="close-ended" variant="primary" onClick={close} autoFocus>
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
          <JobReportView record={record} />
        )}
      </div>
    </Dialog>
  );
}
