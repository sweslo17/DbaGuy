import { Button } from "./Button";
import { Dialog } from "./Dialog";

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  message,
  confirmLabel = "Delete",
  onConfirm,
  pending = false,
  error = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  pending?: boolean;
  error?: string | null;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      width={420}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} disabled={pending}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="text-[13px]">{message}</p>
      {error && (
        <p className="mt-2 whitespace-pre-wrap text-[12px] text-danger" data-selectable>
          {error}
        </p>
      )}
    </Dialog>
  );
}
