import { Loader2 } from "lucide-react";
import type { KeyboardEvent } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api/client";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { RecoverableError } from "../connections/RecoverableError";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { jobReport, progressView, type ErrorLine, type JobReport, type Tone } from "./jobReport";
import type { JobRecord } from "./jobsStore";

/*
 * A data-tools job's view (spec ⑥ §3.6): the connection it runs on, its progress while it runs, its outcome once it
 * ended. Every word about what happened comes from `jobReport` (never made up here). Each view reads only the record
 * it is given: the dialog that shows it subscribes to that one job (Q1).
 */

/**
 * A held Return's repeats never press the button that has the focus now: the Return that started a job (Run, Dump) is
 * still down as its view opens with Close focused, and the Return that read a file is still down as the focus lands
 * on Run (`RunFileDialog`).
 */
export function ignoreHeldReturn(e: KeyboardEvent<HTMLButtonElement>) {
  if (e.key === "Enter" && e.repeat) e.preventDefault();
}

export const TONE_CLASS: Record<Tone, string> = {
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
  muted: "text-muted",
};

/** The connection a dialog works on: its engine, its colour and name (red, a production one, in red) and the database. */
export function ConnectionLine({ profileId, database }: { profileId: string; database?: string | null }) {
  const profiles = useProfiles();
  const profile = profiles.data?.find((entry) => entry.profile.id === profileId)?.profile ?? null;
  if (!profile) return null;
  const red = profile.color === "red";
  const tint = profileColor(profile.color);
  return (
    <p className="flex min-w-0 items-center gap-1.5 text-[12px]" data-selectable>
      <EngineIcon engine={profile.engine} size={12} className="shrink-0 text-muted" />
      {tint && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tint }} />}
      <span className={cn("truncate", red ? "font-medium text-danger" : "font-medium")}>{profile.name}</span>
      {database && <span className="shrink-0 text-muted">· database {database}</span>}
    </p>
  );
}

const FILTER_TITLE_PREFIX = "WHERE ";

/** A table tab's filter as a dump's and a copy's form show it: `WHERE <condition>`, cut to the line, whole on hover. */
export function FilterCode({ filter }: { filter: string }) {
  return (
    <code className="min-w-0 truncate font-mono text-[12px] text-muted" title={`${FILTER_TITLE_PREFIX}${filter}`} data-selectable>
      {FILTER_TITLE_PREFIX}
      {filter}
    </code>
  );
}

/**
 * A read a dialog needs that failed (a dump's plan, a copy's preview, a folder's tables): a missing password asks for it
 * (then reads again); another error says why, with Retry.
 */
export function ReadProblem({ profileId, error, retry }: { profileId: string; error: Error; retry: () => void }) {
  if (isPasswordRequired(error)) return <RecoverableError profileId={profileId} error={error} onRetry={retry} />;
  return (
    <div className="flex items-start gap-2 text-[12px] text-danger">
      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
        {errorMessage(error)}
      </span>
      <Button size="sm" onClick={retry}>
        Retry
      </Button>
    </div>
  );
}

/** A copy's tables and how each ended (06c, R-06-12), in the copy's order. */
function CopiedTables({ report }: { report: JobReport }) {
  if (report.tables.length === 0) return null;
  return (
    <ul className="max-h-40 overflow-y-auto rounded-md border border-border py-0.5 [scrollbar-gutter:stable]">
      {report.tables.map((line, i) => (
        <li key={i} className="flex min-w-0 items-baseline gap-2 px-2 py-0.5">
          <span className="min-w-0 flex-1 truncate" title={`${line.source ?? ""} → ${line.target}`} data-selectable>
            {line.source ? `${line.source} → ` : ""}
            {line.target}
          </span>
          <span className={cn("shrink-0", TONE_CLASS[line.tone])}>{line.outcome}</span>
          {line.rows !== null && <span className="shrink-0 text-muted tabular-nums">{plural(line.rows, "row")}</span>}
        </li>
      ))}
    </ul>
  );
}

/** A running job: what it is doing, how far it is, and a warning it gave (SQL Server: what another Cancel does). */
export function JobProgressView({ record }: { record: JobRecord }) {
  const now = useNow(500);
  const { kind, command, state } = record;
  const progress = progressView({ kind, command, state }, now, record.tables);
  const warning = state.messages.findLast((message) => message.severity === "warning")?.text ?? null;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex min-w-0 items-center gap-2 text-[13px]">
        <Loader2 size={14} className="shrink-0 animate-spin text-muted" />
        <span className="min-w-0 [overflow-wrap:anywhere]" data-selectable>
          {progress.headline}
        </span>
      </div>
      {progress.percent !== null && (
        <div
          className="h-1 overflow-hidden rounded bg-border"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress.percent)}
        >
          <div className="h-full rounded bg-accent" style={{ width: `${progress.percent}%` }} />
        </div>
      )}
      {progress.detail && <p className="text-[12px] text-muted tabular-nums">{progress.detail}</p>}
      {progress.eta && <p className="text-[12px] text-muted">{progress.eta}</p>}
      {/* Said only while the socket is down: whether the backend comes back is the banner's to say (pre-flight L10). */}
      {state.reconnecting && <p className="text-[12px] text-warning">Reconnecting to the backend…</p>}
      {warning && (
        <p className="text-[12px] whitespace-pre-wrap text-warning" data-selectable>
          {warning}
        </p>
      )}
    </div>
  );
}

/** The outcome's title, in its tone. */
export function ReportTitle({ report }: { report: JobReport }) {
  return (
    <p className={cn("text-[13px] font-semibold [overflow-wrap:anywhere]", TONE_CLASS[report.tone])} data-selectable>
      {report.title}
    </p>
  );
}

/**
 * The error that ended the job, in red; the user's own Cancel in muted words, without the backend's "Query cancelled"
 * (the title says it already, 06b F-d), keeping the line it stopped at.
 */
function ErrorBox({ error, onShowLine }: { error: ErrorLine; onShowLine?: (line: number) => void }) {
  const line = error.line;
  const cancelled = error.code === "cancelled";
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border p-2 text-[12px]",
        cancelled ? "border-border" : "border-danger/40 bg-danger/10",
      )}
    >
      {(error.label || error.code) && (
        <p className="flex flex-wrap items-baseline gap-x-2">
          {error.label && <span className={cn("font-medium", cancelled ? "text-muted" : "text-danger")}>{error.label}</span>}
          {error.code && (
            <span className="font-mono text-[11px] text-muted" data-selectable>
              {error.code}
            </span>
          )}
        </p>
      )}
      {!cancelled && (
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
          {error.message}
        </p>
      )}
      {onShowLine && line !== null && (
        <div>
          <Button size="sm" onClick={() => onShowLine(line)}>
            Show line {line.toLocaleString()}
          </Button>
        </div>
      )}
    </div>
  );
}

/** One skipped error's row: "Line N · <label, code or "error">: <the message's first line>" (the whole message on hover). */
function skippedText(error: ErrorLine): string {
  const where = error.line !== null ? `Line ${error.line.toLocaleString()} · ` : "";
  return `${where}${error.label ?? error.code ?? "error"}: ${error.message.split("\n", 1)[0]}`;
}

/** The errors a run skipped (the first ones the backend keeps), each with Show when it has a line; how many more. */
function SkippedErrors({ report, onShowLine }: { report: JobReport; onShowLine?: (line: number) => void }) {
  if (report.skipped.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <p className="text-muted">Errors skipped</p>
      <ul className="max-h-40 overflow-y-auto rounded-md border border-border py-0.5 [scrollbar-gutter:stable]">
        {report.skipped.map((error, i) => {
          const line = error.line;
          return (
            <li key={i} className="flex min-w-0 items-center gap-2 px-2 py-0.5" title={error.message}>
              <span className="min-w-0 flex-1 truncate" data-selectable>
                {skippedText(error)}
              </span>
              {onShowLine && line !== null && (
                <Button size="sm" variant="ghost" className="shrink-0" onClick={() => onShowLine(line)}>
                  Show
                </Button>
              )}
            </li>
          );
        })}
      </ul>
      {report.moreSkipped > 0 && (
        <p className="text-muted">
          and {plural(report.moreSkipped, "more error")} (only the first {report.skipped.length.toLocaleString()} are kept)
        </p>
      )}
    </div>
  );
}

/**
 * How the job ended (`jobReport`): its title, the facts, the error with its label, what to do, the notes and the errors
 * a run skipped. `onShowLine`: an error's line can be shown (a run whose file is known).
 */
export function JobReportView({ record, onShowLine }: { record: JobRecord; onShowLine?: (line: number) => void }) {
  const report = jobReport(record);
  if (!report) return null;
  return (
    <div className="flex flex-col gap-2 text-[12px]">
      <ReportTitle report={report} />
      {report.detail && (
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
          {report.detail}
        </p>
      )}
      <CopiedTables report={report} />
      {report.error && <ErrorBox error={report.error} onShowLine={onShowLine} />}
      {report.hint && <p className="text-muted">{report.hint}</p>}
      {report.notes.length > 0 && (
        <ul className="flex list-disc flex-col gap-1 pl-4 text-warning">
          {report.notes.map((note, i) => (
            <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
              {note}
            </li>
          ))}
        </ul>
      )}
      <SkippedErrors report={report} onShowLine={onShowLine} />
    </div>
  );
}
