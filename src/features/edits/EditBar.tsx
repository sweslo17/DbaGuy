import { AlertTriangle, Lock, Plus } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { plural } from "@/lib/format";
import { KEY_CHANGED } from "./tableEditing";

export interface EditBarProps {
  /** The table's rows can be edited: + Row shows (off while `paused`). */
  editable: boolean;
  /** Why + Row is off now (the rows are loading, a commit runs), or null. */
  paused: string | null;
  /** The target is being read: nothing is said of it yet (staged changes still show). */
  checking: boolean;
  /** Why rows cannot be edited (the table's), or null. */
  readOnlyReason: string | null;
  /** The target could not be read, or null. */
  targetError: string | null;
  onRetryTarget: () => void;
  changes: number;
  /** Changes on rows the grid does not show (filtered out, not loaded). */
  notShown: number;
  /** Why the staged changes are not kept across a restart (too large, or a text that cannot be saved), or null. */
  notKept: string | null;
  /** The changes were staged on another key than the table's. */
  keyChanged: boolean;
  /** The last commit's outcome is unknown and fresh rows have not been read yet. */
  unknownOutcome: boolean;
  onAddRow: () => void;
  /** Discard…: the tab asks first when there is more than one change. */
  onDiscard: () => void;
  /** Commit… (⌘S); absent: no Commit button (Task 10 adds it). */
  onCommit?: () => void;
  /** Why Commit… is disabled now, or null. */
  commitDisabled: string | null;
}

const NOTE = "flex min-w-0 items-center gap-1";

/**
 * The Data view's edit controls, at the right of its toolbar (spec ⑤ §3.6.5): + Row, the change count, Commit… and
 * Discard…, or "Read-only · <reason>". It never scrolls (the user's Mac shows classic scrollbars, which would cover a
 * strip this high): what does not fit is truncated, the whole text in its tooltip.
 */
export function EditBar(props: EditBarProps) {
  const { editable, paused, checking, readOnlyReason, targetError, changes, notShown, notKept, keyChanged } = props;
  const showTarget = !checking;
  return (
    <div className="flex min-w-0 items-center gap-1.5 overflow-hidden text-[12px]">
      {showTarget && targetError && (
        <span className={`${NOTE} text-danger`} title={targetError} data-selectable>
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">Editing unavailable: {targetError}</span>
          <Button size="sm" className="shrink-0" onClick={props.onRetryTarget}>
            Retry
          </Button>
        </span>
      )}
      {showTarget && readOnlyReason && (
        <span className={`${NOTE} text-muted`} title={readOnlyReason}>
          <Lock size={12} className="shrink-0" />
          <span className="truncate">Read-only · {readOnlyReason}</span>
        </span>
      )}
      {keyChanged && (
        <span className={`${NOTE} text-warning`} title={KEY_CHANGED}>
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">Staged on another key</span>
        </span>
      )}
      {props.unknownOutcome && (
        <span
          className={`${NOTE} text-warning`}
          title="The last commit's outcome is unknown: Refresh to see the table as it is now"
        >
          <AlertTriangle size={12} className="shrink-0" />
          <span className="truncate">Outcome unknown: Refresh</span>
        </span>
      )}
      {showTarget && editable && (
        <Button
          size="sm"
          className="shrink-0"
          onClick={props.onAddRow}
          disabled={paused !== null}
          title={paused ?? "Add a row (⌘⇧P: Add row)"}
        >
          <Plus size={12} /> Row
        </Button>
      )}
      {changes > 0 && (
        <>
          <span
            className={`${NOTE} font-medium text-warning`}
            title={
              notShown > 0
                ? `${plural(changes, "change")}; ${plural(notShown, "change")} on rows not shown (filtered out or not loaded)`
                : plural(changes, "change")
            }
          >
            <span className="truncate">
              {plural(changes, "change")}
              {notShown > 0 && (
                <span className="font-normal text-muted"> · {notShown.toLocaleString()} in rows not shown</span>
              )}
            </span>
          </span>
          {notKept && (
            <span className="shrink-0 text-warning" title={`${notKept}. Commit them, or keep DbaGuy open.`}>
              <AlertTriangle size={12} aria-label={notKept} />
            </span>
          )}
          {props.onCommit && (
            <Button
              size="sm"
              variant="primary"
              className="shrink-0"
              onClick={props.onCommit}
              disabled={props.commitDisabled !== null}
              title={props.commitDisabled ?? "Preview the statements, then commit (⌘S)"}
            >
              Commit… ⌘S
            </Button>
          )}
          <Button size="sm" variant="ghost" className="shrink-0" onClick={props.onDiscard}>
            Discard…
          </Button>
        </>
      )}
    </div>
  );
}
