import { useEffect } from "react";
import { closeDataDialog, shownJob, useDataDialogAsk } from "./dataDialogs";
import { DumpDialog } from "./DumpDialog";
import { JobDialog } from "./JobDialog";
import { useJobs } from "./jobsStore";

/** A job's dialog, subscribed to that one job (Q1: another job's progress never renders it). */
function JobAsk({ requestId }: { requestId: string }) {
  const record = useJobs((s) => s.jobs[requestId]);
  // Cleared from Jobs (Clear finished) while its dialog was asked for: the ask goes, unless another took its place
  useEffect(() => {
    if (!record && shownJob() === requestId) closeDataDialog();
  }, [record, requestId]);
  return record ? <JobDialog record={record} /> : null;
}

/**
 * The data tools' one dialog (`dataDialogs.ts`), in the app shell: a dump's form, a run's form, or a job's view. A tab
 * that closes never takes a job's dialog with it. Each ask mounts its dialog anew (`serial`).
 */
export function DataToolsHost() {
  const ask = useDataDialogAsk();
  switch (ask?.kind) {
    case "dump":
      return <DumpDialog key={ask.serial} entry={ask.entry} form={ask.form} />;
    case "job":
      return <JobAsk key={ask.serial} requestId={ask.requestId} />;
    default:
      return null; // none, or a run's form (Task 7 hosts RunFileDialog)
  }
}
