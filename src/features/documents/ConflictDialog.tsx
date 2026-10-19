import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";

/**
 * A save or a delete met a document that is not the one that was opened (spec §3.13, ruling C22): changed (Reload shows
 * what is stored and drops the edits here), or deleted (Reload could only show "does not exist", so the edits are saved
 * as a new document instead). Copy keeps the text safe first and leaves the dialog open.
 */
export function ConflictDialog({
  kind,
  documentId,
  onReload,
  onCopy,
  onSaveAsNew,
  onCancel,
}: {
  kind: "changed" | "deleted";
  documentId: string;
  onReload: () => void;
  onCopy: () => void;
  onSaveAsNew: () => void;
  onCancel: () => void;
}) {
  const changed = kind === "changed";
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onCancel()}
      title={changed ? "Document changed" : "Document deleted"}
      width={480}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} autoFocus>
            Cancel
          </Button>
          <Button onClick={onCopy}>Copy my version</Button>
          {changed ? (
            <Button variant="danger" onClick={onReload}>
              Reload (discard my changes)
            </Button>
          ) : (
            <Button variant="primary" onClick={onSaveAsNew}>
              Save as new document
            </Button>
          )}
        </>
      }
    >
      <p className="text-[13px]" data-selectable>
        <span className="font-mono">{documentId}</span> was {changed ? "changed" : "deleted"} after you opened it.
      </p>
      <p className="mt-2 text-[12px] text-muted">
        {changed
          ? "Reload reads what is stored now and drops the edits made here. Copy my version puts them on the clipboard first."
          : "Save as new document stores your text under the same ID, without an expiry."}
      </p>
    </Dialog>
  );
}
