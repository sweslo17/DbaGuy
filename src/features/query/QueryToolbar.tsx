import { ListEnd, Play, Square } from "lucide-react";
import { Button } from "@/components/ui/Button";
import type { SessionInfo } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import type { SessionPhase, SessionStateView } from "../session/SessionController";
import { TransactionBadge } from "./TransactionBadge";

const PHASE_DOT: Record<SessionPhase, string> = {
  opening: "bg-warning",
  open: "bg-success",
  reconnecting: "bg-warning",
  failed: "bg-danger",
  closed: "bg-danger",
};

export interface QueryToolbarProps {
  /** null: the tab has no connection yet. */
  profileName: string | null;
  phase: SessionPhase;
  session: SessionInfo | null;
  sessionFailed: boolean;
  state: SessionStateView | null;
  canRun: boolean;
  /** Why Run is disabled, as a tooltip (e.g. an export is running). */
  runBlockedReason?: string;
  canCancel: boolean;
  onRun: () => void;
  onRunAll: () => void;
  onCancel: () => void;
}

/** The connection this tab runs on: profile, database and server, with the session's phase as a dot. */
function ConnectionLabel({
  profileName,
  phase,
  session,
  sessionFailed,
}: Pick<QueryToolbarProps, "profileName" | "phase" | "session" | "sessionFailed">) {
  if (profileName === null) return <span className="text-[12px] text-muted">Choose a connection</span>;
  const detail = session
    ? [session.database, session.server.version].filter(Boolean).join(" · ")
    : sessionFailed
      ? "Not connected"
      : "Connecting…";
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-[12px]">
      <span className={cn("h-2 w-2 shrink-0 rounded-full", PHASE_DOT[phase])} title={phase} />
      <span className="shrink-0 font-medium">{profileName}</span>
      <span className="truncate text-[11px] text-muted">{detail}</span>
    </span>
  );
}

export function QueryToolbar(props: QueryToolbarProps) {
  const { canRun, runBlockedReason, canCancel, onRun, onRunAll, onCancel } = props;
  return (
    <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-2">
      <Button
        variant="primary"
        size="sm"
        onClick={onRun}
        disabled={!canRun}
        title={runBlockedReason ?? "Run the selection, or the whole text (⌘↵)"}
      >
        <Play size={12} /> Run
      </Button>
      <Button size="sm" onClick={onRunAll} disabled={!canRun} title={runBlockedReason ?? "Run the whole text (⌘⇧↵)"}>
        <ListEnd size={12} /> Run all
      </Button>
      <Button size="sm" onClick={onCancel} disabled={!canCancel} title="Cancel the running statement">
        <Square size={12} /> Cancel
      </Button>
      <div className="flex-1" />
      <TransactionBadge state={props.state} />
      <ConnectionLabel
        profileName={props.profileName}
        phase={props.phase}
        session={props.session}
        sessionFailed={props.sessionFailed}
      />
    </div>
  );
}
