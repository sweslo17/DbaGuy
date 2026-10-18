import { useEffect } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { BLOCKED_TEXT, reasonText } from "./reasons";
import type { ConfirmPrompt, FlaggedStatement } from "./useQueryRunner";

const PREVIEW_LINES = 3;

/** The statement's first lines (blank ones dropped), "…" when there is more. */
function preview(sql: string): string {
  const lines = sql.split("\n").filter((line) => line.trim() !== "");
  const shown = lines.slice(0, PREVIEW_LINES).join("\n");
  return lines.length > PREVIEW_LINES ? `${shown}\n…` : shown;
}

function FlaggedItem({ item }: { item: FlaggedStatement }) {
  const blocked = item.decision.action === "block";
  return (
    <li className="rounded-md border border-border bg-panel px-2.5 py-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="text-muted">Statement {item.index + 1}</span>
        {item.statement.repeat > 1 && (
          <span className="rounded bg-border px-1.5 py-px text-muted">
            runs {item.statement.repeat.toLocaleString("en-US")} times (GO {item.statement.repeat})
          </span>
        )}
        {blocked && <span className="rounded bg-danger/15 px-1.5 py-px text-danger">{BLOCKED_TEXT}</span>}
        {item.decision.reasons
          .filter((reason) => !(blocked && reason === "read_only")) // the chip before says it
          .map((reason) => (
            <span key={reason} className="rounded bg-warning/15 px-1.5 py-px text-warning">
              {reasonText(reason)}
            </span>
          ))}
      </div>
      <pre className="overflow-hidden font-mono text-[12px] text-ellipsis whitespace-pre" data-selectable>
        {preview(item.statement.sql)}
      </pre>
    </li>
  );
}

/**
 * Confirmation of the statements the safety policy flagged, all in one list. A blocked statement (read-only
 * connection) cannot run: the dialog only offers Close and nothing is sent. Otherwise Run sends the whole run with
 * these statements confirmed, and Cancel (or Escape) sends nothing. Focus starts on the harmless button and goes to
 * `onClosed` afterwards (the editor). The title names the tab: a confirmation asked mid-run may appear while another
 * tab is in front.
 */
export function ConfirmRunDialog({
  prompt,
  context,
  onClosed,
}: {
  prompt: ConfirmPrompt | null;
  /** The tab and its connection, e.g. "Query 1 · dev pg". */
  context: string;
  onClosed?: () => void;
}) {
  const items = prompt?.items ?? [];
  const blocked = items.some((item) => item.decision.action === "block");
  const answer = (run: boolean) => prompt?.answer(run);
  // A prompt that goes away unanswered (replaced, or the dialog unmounts with its tab) means "do not run".
  useEffect(() => () => prompt?.answer(false), [prompt]);
  const note = blocked
    ? "Nothing was sent."
    : prompt?.when === "during"
      ? "The connection's safety level now asks before this statement. Cancel stops the run here."
      : `${items.length === 1 ? "This statement needs" : "These statements need"} confirmation. Nothing has been sent yet; Cancel runs none of the statements.`;

  return (
    <Dialog
      open={prompt !== null}
      onOpenChange={(open) => {
        if (!open) answer(false);
      }}
      onCloseAutoFocus={(event) => {
        if (!onClosed) return;
        event.preventDefault();
        onClosed();
      }}
      title={`${blocked ? BLOCKED_TEXT : "Confirm before running"} — ${context}`}
      width={600}
      footer={
        blocked ? (
          <Button autoFocus onClick={() => answer(false)}>
            Close
          </Button>
        ) : (
          <>
            <Button variant="ghost" autoFocus onClick={() => answer(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => answer(true)}>
              Run
            </Button>
          </>
        )
      }
    >
      <p className="mb-3 text-[12px] text-muted">{note}</p>
      <ul className="flex flex-col gap-2">
        {items.map((item) => (
          <FlaggedItem key={item.index} item={item} />
        ))}
      </ul>
    </Dialog>
  );
}
