import { Check, ListEnd, Play, Square, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import type { SessionStateView } from "../session/SessionController";
import { ConnectionPicker, type ConnectionPickerProps } from "./ConnectionPicker";
import { TransactionBadge } from "./TransactionBadge";

/** Toolbar buttons keep their size: the connection's details shrink first. */
const FIXED = "shrink-0 whitespace-nowrap";

/**
 * A control's text that gives way to its icon alone when the toolbar is narrow (it stays for screen readers); `tight`
 * text goes only when the toolbar is narrower still.
 */
function Label({ children, tight = false }: { children: string; tight?: boolean }) {
  return <span className={tight ? "@max-[40rem]:sr-only" : "@max-[50rem]:sr-only"}>{children}</span>;
}

export interface QueryToolbarProps {
  connection: ConnectionPickerProps;
  state: SessionStateView | null;
  /** The tab is shown: the badge's clock ticks. */
  visible: boolean;
  /** A session message just arrived: the badge pulses. */
  pulse: boolean;
  /** What ⌘↵ runs without a selection (the `run.default` setting). */
  runDefault: "statement" | "all";
  canRun: boolean;
  /** Why Run is disabled, as a tooltip (e.g. an export is running). */
  runBlockedReason?: string;
  canCancel: boolean;
  onRun: () => void;
  onRunAll: () => void;
  onCancel: () => void;
  /** The tab's desired auto-commit mode. */
  autocommit: boolean;
  /** Why the mode cannot change now (an open transaction, a running statement); the toggle is disabled with it. */
  autocommitBlockedReason?: string;
  onAutocommitChange: (enabled: boolean) => void;
  /** Commit and Rollback are possible now; otherwise `endBlockedReason` says why not. */
  canEnd: boolean;
  endBlockedReason?: string;
  onCommit: () => void;
  onRollback: () => void;
}

/** "Auto-commit" with a checkbox; its tooltip says why it is disabled (on the label: a disabled input shows none). */
function AutocommitToggle({
  checked,
  disabledReason,
  onChange,
}: {
  checked: boolean;
  disabledReason?: string;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <label
      className={cn(
        "flex shrink-0 items-center gap-1.5 px-1 text-[12px] whitespace-nowrap",
        disabledReason && "opacity-50",
      )}
      title={
        disabledReason ??
        "Auto-commit: each statement commits on its own; off: statements run in a transaction until Commit"
      }
    >
      <input
        type="checkbox"
        className="accent-[var(--accent)]"
        checked={checked}
        disabled={!!disabledReason}
        onChange={(e) => onChange(e.target.checked)}
      />
      <Label tight>Auto-commit</Label>
    </label>
  );
}

export function QueryToolbar(props: QueryToolbarProps) {
  const { runDefault, canRun, runBlockedReason, canCancel, onRun, onRunAll, onCancel, state } = props;
  const runTitle = `Run the selection, or ${runDefault === "all" ? "every statement" : "the statement under the cursor"} (⌘↵)`;
  const endReason = props.canEnd ? undefined : props.endBlockedReason;
  return (
    <div className="@container flex h-9 min-w-0 shrink-0 items-center gap-2 overflow-hidden border-b border-border px-2">
      <Button
        variant="primary"
        size="sm"
        className={FIXED}
        onClick={onRun}
        disabled={!canRun}
        title={runBlockedReason ?? runTitle}
      >
        <Play size={12} /> Run
      </Button>
      <Button
        size="sm"
        className={FIXED}
        onClick={onRunAll}
        disabled={!canRun}
        title={runBlockedReason ?? "Run every statement (of the selection, if any), one after another (⌘⇧↵)"}
      >
        <ListEnd size={12} />
        <Label>Run all</Label>
      </Button>
      <Button
        size="sm"
        className={FIXED}
        onClick={onCancel}
        disabled={!canCancel}
        title="Cancel the running statement and stop the run"
      >
        <Square size={12} />
        <Label>Cancel</Label>
      </Button>
      <div className="mx-1 h-4 w-px shrink-0 bg-border" />
      <AutocommitToggle
        checked={props.autocommit}
        disabledReason={props.autocommitBlockedReason}
        onChange={props.onAutocommitChange}
      />
      <Button
        size="sm"
        className={FIXED}
        onClick={props.onCommit}
        disabled={!props.canEnd}
        title={endReason ?? "Commit the transaction"}
      >
        <Check size={12} />
        <Label>Commit</Label>
      </Button>
      <Button
        size="sm"
        className={FIXED}
        onClick={props.onRollback}
        disabled={!props.canEnd}
        title={endReason ?? "Roll back the transaction"}
      >
        <Undo2 size={12} />
        <Label>Rollback</Label>
      </Button>
      <div className="flex-1" />
      <TransactionBadge state={state} visible={props.visible} pulse={props.pulse} />
      <ConnectionPicker {...props.connection} />
    </div>
  );
}
