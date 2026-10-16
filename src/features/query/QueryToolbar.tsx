import { Check, ListEnd, Loader2, Play, Square, Timer, Undo2 } from "lucide-react";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import type { SessionStateView } from "../session/SessionController";
import { NUMBER_FIELDS, numberProblem } from "../settings/settingsForm";
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

/** The tab's statement timeout (ruling R-02c-20). */
export interface TimeoutControl {
  /** Seconds; 0 is none. */
  seconds: number;
  /** The `run.statement_timeout_s` setting; the tab follows it until a timeout is typed here. */
  setting: number;
  followsSetting: boolean;
  onChange: (seconds: number) => void;
  /** Enter or Escape in the field: the keyboard goes back to the editor. */
  onDone: () => void;
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
  /** The statements are being split or checked before a run: a long script takes a moment. */
  checking: boolean;
  timeout: TimeoutControl;
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

const TIMEOUT_FIELD = NUMBER_FIELDS.statement_timeout_s;
const seconds = (value: number) => (value > 0 ? `${value.toLocaleString()} s` : "none");

/**
 * "Timeout [ ] s": the tab's own statement timeout, in memory only (ruling R-02c-1). Empty or 0 is none; the range is
 * the backend's. A typed value applies once the field is left (or on Enter); one out of range is not taken.
 */
function TimeoutField({ seconds: value, setting, followsSetting, onChange, onDone }: TimeoutControl) {
  /** What is being typed; null while the field shows the value. Mirrored in a ref: Enter and the blur it causes both commit. */
  const [draft, setDraft] = useState<string | null>(null);
  const typed = useRef<string | null>(null);
  const edit = (text: string | null) => {
    typed.current = text;
    setDraft(text);
  };
  const problemOf = (text: string) => (text.trim() === "" ? null : numberProblem(TIMEOUT_FIELD, text));
  const problem = draft === null ? null : problemOf(draft);
  const commit = () => {
    const text = typed.current;
    if (text === null) return;
    edit(null);
    const why = problemOf(text);
    if (why) {
      toast("warning", `Timeout not changed: enter whole seconds from 0 to ${TIMEOUT_FIELD.max.toLocaleString()}`);
      return;
    }
    const next = text.trim() === "" ? 0 : Number(text.trim());
    if (next !== value) onChange(next);
  };
  const source = followsSetting ? "from Settings" : `Settings: ${seconds(setting)}`;
  return (
    <label
      className="flex shrink-0 items-center gap-1 text-[12px] whitespace-nowrap"
      title={problem ?? `Statement timeout for this tab (${source}); empty or 0: none`}
    >
      <Timer size={12} className="shrink-0 text-muted" aria-hidden />
      <Label>Timeout</Label>
      <input
        type="text"
        inputMode="numeric"
        aria-label="Statement timeout in seconds"
        aria-invalid={problem !== null}
        spellCheck={false}
        autoComplete="off"
        placeholder="none"
        value={draft ?? (value > 0 ? String(value) : "")}
        onChange={(e) => edit(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (isImeKeyEvent(e)) return;
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
            onDone();
          } else if (e.key === "Escape") {
            e.preventDefault();
            edit(null);
            onDone();
          }
        }}
        className={cn(
          "h-5 w-12 rounded border border-border bg-elevated px-1 text-right tabular-nums outline-none",
          "placeholder:text-muted/70 focus:border-accent aria-invalid:border-danger",
          followsSetting && draft === null && "text-muted",
        )}
      />
      <span className="text-muted">s</span>
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
      <TimeoutField {...props.timeout} />
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
      {props.checking && (
        // Shown only if it takes a moment (a long script), so a short run does not flicker.
        <span role="status" className="flex shrink-0 animate-delayed-show items-center gap-1 text-[12px] text-muted">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          <Label>Checking statements…</Label>
        </span>
      )}
      <TransactionBadge state={state} visible={props.visible} pulse={props.pulse} />
      <ConnectionPicker {...props.connection} />
    </div>
  );
}
