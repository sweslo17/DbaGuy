import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { INTERRUPTED_MESSAGE, type ExecutionState } from "./execution";
import type { SessionController } from "./SessionController";

export type TransactionEnd = "commit" | "rollback";

/** Cancelling an export cancels its statement, which aborts the transaction it runs in (PostgreSQL). */
export const EXPORT_CANCEL_ABORTS_TRANSACTION = "Cancelling the export aborts the open transaction";

/** What a message's severity shows as when it becomes a toast. */
export function toastKind(severity: string): "info" | "warning" | "error" {
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  return "info";
}

/**
 * Sends COMMIT or ROLLBACK and waits until it is done. Its warnings become toasts prefixed with `subject` (the tab and
 * its connection) — e.g. "The transaction had failed, so COMMIT rolled it back". Returns why it failed, or null.
 */
export async function endTransaction(
  controller: SessionController,
  kind: TransactionEnd,
  subject: string,
): Promise<string | null> {
  const requestId = kind === "commit" ? controller.commit() : controller.rollback();
  let done: ExecutionState;
  try {
    done = await controller.whenDone(requestId);
  } catch (error) {
    return errorMessage(error);
  } finally {
    controller.forget(requestId);
  }
  for (const message of done.messages) {
    // An interruption is reported as the failure itself.
    if (message.severity === "warning" && message.text !== INTERRUPTED_MESSAGE)
      toast("warning", `${subject}: ${message.text}`);
  }
  if (done.error) return done.error.message;
  if (done.interrupted) return INTERRUPTED_MESSAGE;
  return null;
}
