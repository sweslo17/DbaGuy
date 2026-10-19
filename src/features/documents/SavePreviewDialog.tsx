import { useMemo } from "react";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { Dialog } from "@/components/ui/Dialog";
import type { ChangeCounts, DocumentChange, SavePreview } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { CodeEditor } from "../editor/CodeEditor";
import { prettyJson } from "./documentApi";

/** The server's text is shown in the dialog up to this many characters; a longer one is only offered for copying. */
export const SERVER_TEXT_SHOWN_MAX = 1_048_576;

/** One kind's share of the summary line ("2 keys reordered"); the kinds in the order the API lists them. */
const SUMMARY: Record<keyof ChangeCounts, (count: number) => string> = {
  key_order: (count) => `${plural(count, "key")} reordered`,
  number: (count) => `${plural(count, "number")} written differently`,
  duplicate_key: (count) => plural(count, "duplicate key"),
  other: (count) => plural(count, "other change"),
};

/** "2 keys reordered · 1 number written differently": the kinds with a change only. */
function summarize(counts: ChangeCounts): string {
  return (Object.keys(SUMMARY) as (keyof ChangeCounts)[])
    .filter((kind) => counts[kind] > 0)
    .map((kind) => SUMMARY[kind](counts[kind]))
    .join(" · ");
}

const total = (counts: ChangeCounts): number => Object.values(counts).reduce((sum, count) => sum + count, 0);

function labelOf(change: DocumentChange): string {
  switch (change.kind) {
    case "key_order":
      return "Keys sorted";
    case "number":
      return change.value_changed ? "Number changes value" : "Number written differently";
    case "duplicate_key":
      return "Duplicate key: the last value is kept";
    case "other":
      return "Changed";
  }
}

function ChangeRow({ change }: { change: DocumentChange }) {
  return (
    <li className="border-b border-border px-3 py-1.5 text-[12px] last:border-b-0" data-selectable>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-mono">{change.path}</span>
        <span className={change.value_changed ? "font-medium text-danger" : "text-muted"}>{labelOf(change)}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5 font-mono">
        <span className="break-all">{change.before}</span>
        <span className="text-muted" aria-hidden>
          →
        </span>
        <span className={cn("break-all", change.value_changed && "text-danger")}>{change.after}</span>
      </div>
    </li>
  );
}

/**
 * What the server would store instead of what was typed (spec §3.9.3, rulings C1–C2, C13): a summary, the changes (value
 * changes first, marked in the danger colour), and the server's text. Nothing is written until "Save as shown", which
 * sends the same text again, confirmed. `pending` is that request being in flight.
 */
export function SavePreviewDialog({
  preview,
  documentId,
  pending,
  onConfirm,
  onCancel,
}: {
  preview: SavePreview;
  documentId: string;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { server_text: serverText, changes, counts } = preview;
  // The server lists value changes first; sorted again (stable), so the order never depends on it.
  const ordered = useMemo(() => [...changes].sort((a, b) => Number(b.value_changed) - Number(a.value_changed)), [changes]);
  const more = total(counts) - changes.length;
  const showsText = serverText.length <= SERVER_TEXT_SHOWN_MAX;
  const shown = useMemo(() => (showsText ? prettyJson(serverText) : ""), [showsText, serverText]);

  return (
    <Dialog
      open
      // Escape and a click outside count as Cancel, but not while the confirmed write is on its way.
      onOpenChange={(open) => !open && !pending && onCancel()}
      title="Save document — the server will change it"
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={pending} autoFocus>
            Cancel
          </Button>
          <Button variant="primary" onClick={onConfirm} disabled={pending}>
            Save as shown
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-[13px]" data-selectable>
          Couchbase would store <span className="font-mono">{documentId}</span> differently from your text:{" "}
          {summarize(counts)}. Nothing is saved until you confirm.
        </p>
        <ul className="max-h-52 overflow-y-auto rounded-md border border-border [scrollbar-gutter:stable]">
          {ordered.map((change, index) => (
            <ChangeRow key={index} change={change} />
          ))}
          {more > 0 && <li className="px-3 py-1.5 text-[12px] text-muted">… and {more.toLocaleString()} more</li>}
        </ul>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] font-medium text-muted">What will be stored</span>
          {showsText ? (
            <div className="h-44 overflow-hidden rounded-md border border-border">
              <CodeEditor initialText={shown} dialect={null} language="json" readOnly />
            </div>
          ) : (
            <div className="flex items-center gap-2 text-[12px] text-muted">
              <span>The server&apos;s text is too long to show here.</span>
              <CopyButton text={serverText} label="Copy it" />
            </div>
          )}
        </div>
      </div>
    </Dialog>
  );
}
