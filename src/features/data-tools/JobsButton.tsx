import { DatabaseBackup, FileCode, ListChecks, Loader2 } from "lucide-react";
import { memo, useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Popover } from "@/components/ui/Popover";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { cn } from "@/lib/cn";
import { formatElapsed } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { openJobDialog, setJobsPopoverOpen, useJobsPopoverOpen } from "./dataDialogs";
import { hasEndedJob, runningCount, shownElapsed } from "./jobEndings";
import { jobSummary } from "./jobReport";
import { clearFinishedJobs, isRunning, syncJobs, useJobs } from "./jobsStore";
import { TONE_CLASS } from "./JobViews";

/*
 * The Jobs button at the bottom of the sidebar and its list (spec ⑥ R-06-15, V3): this window's data-tools jobs, the
 * ended ones too until Clear finished; each opens its view again. Q1: the button reads two numbers, the list the ids,
 * and each row its own job, so a job's progress renders its row only, never the sidebar or the app.
 */

const ROW = "[data-job-row]";

/** Up and Down (Home, End) move between the rows; Return opens one (a button). */
function moveBetweenRows(e: KeyboardEvent<HTMLUListElement>) {
  const rows = [...e.currentTarget.querySelectorAll<HTMLButtonElement>(ROW)];
  const at = rows.indexOf(document.activeElement as HTMLButtonElement);
  const to =
    e.key === "ArrowDown" ? at + 1 : e.key === "ArrowUp" ? at - 1 : e.key === "Home" ? 0 : e.key === "End" ? rows.length - 1 : null;
  if (to === null) return;
  e.preventDefault();
  rows[Math.max(0, Math.min(rows.length - 1, to))]?.focus();
}

/** One job: its kind, title, progress or outcome (`jobSummary`, in its tone) and how long it ran, when known. */
const JobRow = memo(function JobRow({ requestId, onOpen }: { requestId: string; onOpen: (requestId: string) => void }) {
  const record = useJobs((s) => s.jobs[requestId]);
  const running = record !== undefined && isRunning(record);
  const now = useNow(1000, running);
  if (!record) return null;
  const summary = jobSummary(record, now);
  const elapsed = shownElapsed(record.state, now);
  const Icon = record.kind === "dump" ? DatabaseBackup : FileCode;
  return (
    <li>
      <button
        type="button"
        data-job-row
        className="flex w-full min-w-0 items-start gap-2 px-3 py-1.5 text-left outline-none hover:bg-hover focus-visible:bg-hover"
        onClick={() => onOpen(requestId)}
      >
        <Icon size={14} className="mt-px shrink-0 text-muted" />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-[12px]" title={record.title}>
            {record.title}
          </span>
          <span
            className={cn("truncate text-[11px]", summary.tone ? TONE_CLASS[summary.tone] : "text-muted")}
            title={summary.text}
          >
            {summary.text}
          </span>
        </span>
        {elapsed !== null && <span className="shrink-0 text-[11px] text-muted tabular-nums">{formatElapsed(elapsed)}</span>}
      </button>
    </li>
  );
});

/** The list in the popover: newest first, the first row focused as it opens (Return opens it). */
function JobsList({ onOpen }: { onOpen: (requestId: string) => void }) {
  const order = useJobs((s) => s.order);
  const anyEnded = useJobs(hasEndedJob);
  const [clearing, setClearing] = useState(false);
  const list = useRef<HTMLUListElement>(null);
  const focusFirst = () => list.current?.querySelector<HTMLButtonElement>(ROW)?.focus({ preventScroll: true });
  // Before the panel's own first-control focus (it runs a commit later), which would pick Clear finished
  useEffect(focusFirst, []);

  const clear = async () => {
    setClearing(true);
    try {
      await clearFinishedJobs();
      // The row that had the focus may be gone, and Clear finished is greyed now: the newest job left takes it
      requestAnimationFrame(focusFirst);
    } catch (error) {
      toast("error", `Could not clear the finished jobs: ${errorMessage(error)}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-border py-1 pr-1 pl-3">
        <span className="text-[12px] font-semibold">Jobs</span>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          disabled={!anyEnded || clearing}
          title="Forget the jobs that ended (the backend's list too)"
          onClick={() => void clear()}
        >
          Clear finished
        </Button>
      </div>
      {/* A fixed-height list: the gutter keeps room for a classic scrollbar, so the times never sit under it */}
      <ul ref={list} className="max-h-80 overflow-y-auto py-1 [scrollbar-gutter:stable]" onKeyDown={moveBetweenRows}>
        {order.map((requestId) => (
          <JobRow key={requestId} requestId={requestId} onOpen={onOpen} />
        ))}
      </ul>
    </div>
  );
}

/**
 * The sidebar's Jobs button (V3: only while this window has jobs): a spinner and "N running" while jobs run, else
 * "Jobs". It opens the list (⌘⇧P "Show jobs" too), which reads the backend's list each time it opens.
 */
export function JobsButton() {
  const count = useJobs((s) => s.order.length);
  const running = useJobs(runningCount);
  const open = useJobsPopoverOpen();
  const shown = open && count > 0;
  const trigger = useRef<HTMLButtonElement>(null);

  // Each opening asks the backend (a job another page load started, an outcome a lost socket missed)
  useEffect(() => {
    if (shown) void syncJobs();
  }, [shown]);
  // Nothing left to list (Clear finished took the last): it is closed, so the next job does not open it
  useEffect(() => {
    if (open && count === 0) setJobsPopoverOpen(false);
  }, [open, count]);

  const openJob = useCallback((requestId: string) => {
    // The focus goes to this button first: the job's dialog gives it back here when it closes
    trigger.current?.focus();
    setJobsPopoverOpen(false);
    openJobDialog(requestId);
  }, []);

  if (count === 0) return null;
  return (
    <div className="shrink-0 border-t border-border p-1">
      <Popover
        open={shown}
        onOpenChange={setJobsPopoverOpen}
        label="Jobs"
        className="w-[380px] max-w-[calc(100vw-16px)]"
        trigger={
          <button
            ref={trigger}
            type="button"
            title="Data-tools jobs of this run"
            className="flex h-6 w-full min-w-0 items-center gap-1.5 rounded px-2 text-[12px] text-muted outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-accent/50 data-[state=open]:bg-hover data-[state=open]:text-fg"
          >
            {running > 0 ? (
              <Loader2 size={13} className="shrink-0 animate-spin" />
            ) : (
              <ListChecks size={13} className="shrink-0" />
            )}
            <span className="truncate">{running > 0 ? `${running.toLocaleString()} running` : "Jobs"}</span>
          </button>
        }
      >
        <JobsList onOpen={openJob} />
      </Popover>
    </div>
  );
}
