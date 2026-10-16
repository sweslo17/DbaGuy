import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { EXPORT_CANCEL_ABORTS_TRANSACTION } from "../grid/ExportDialog";
import { isExporting, type SessionController } from "../session/SessionController";
import { endTransaction, type TransactionEnd } from "./transactions";

/** What the user is about to do to the tab's session: close the tab, or switch the tab to another connection. */
export type GuardIntent = "close" | "switch";

type Prompt =
  | { kind: "export"; intent: GuardIntent; inTransaction: boolean }
  | { kind: "transaction"; intent: GuardIntent; failed: boolean; connected: boolean };

type Choice = "stop" | TransactionEnd | "cancel";

interface Asking {
  prompt: Prompt;
  /** The chosen action is in progress (the dialog waits for it). */
  pending: Choice | null;
  /** The chosen action failed: the dialog stays, so another choice can be made. */
  error: string | null;
}

/** How long closing waits for a cancelled export to end before it goes on anyway. */
const STOP_WAIT_MS = 5000;

const PENDING_LABELS: Record<Exclude<Choice, "cancel">, string> = {
  stop: "Stopping…",
  commit: "Committing…",
  rollback: "Rolling back…",
};

/** Cancels the session's running exports and waits (briefly) until they have ended. */
async function stopExports(controller: SessionController): Promise<void> {
  const exports = Object.values(controller.store.getState().executions).filter(
    (execution) => execution.kind === "export" && execution.status !== "done",
  );
  exports.forEach((execution) => controller.cancel(execution.requestId));
  if (controller.store.getState().phase !== "open") return; // the cancel cannot be sent: closing ends them
  let timer = 0;
  await Promise.race([
    Promise.allSettled(exports.map((execution) => controller.whenDone(execution.requestId))),
    new Promise((resolve) => {
      timer = window.setTimeout(resolve, STOP_WAIT_MS);
    }),
  ]);
  window.clearTimeout(timer);
}

function promptText(prompt: Prompt, subject: string): { title: string; message: string } {
  const verb = prompt.intent === "close" ? "close" : "switch";
  if (prompt.kind === "export") {
    return {
      title: `Stop the export and ${verb}?`,
      message:
        `An export is still running in ${subject}.` +
        (prompt.inTransaction ? ` ${EXPORT_CANCEL_ABORTS_TRANSACTION}.` : ""),
    };
  }
  const title = prompt.intent === "close" ? "Close with an open transaction?" : "Switch with an open transaction?";
  const after = prompt.intent === "close" ? "the tab closes" : "the connection switches";
  if (!prompt.connected) {
    return {
      title,
      message: `${subject} has an open transaction, but its session is not connected: Commit is not possible, and Rollback lets ${after}, which ends the session and rolls the transaction back.`,
    };
  }
  if (prompt.failed) {
    return { title, message: `The transaction in ${subject} failed: it can only be rolled back, then ${after}.` };
  }
  return { title, message: `${subject} has an open transaction. Commit or roll it back, then ${after}.` };
}

/**
 * The checks a tab with a session makes before its session goes away (spec §4.2; ②b carries): a running export asks
 * "Stop the export and close?" (and cancels it), an open transaction asks Commit / Rollback / Cancel (and ends it).
 * Each step resolves true to go on, false to keep the tab; they always read the session's latest state. `dialog` must
 * be rendered by the tab. An unanswered question is answered "no" when the tab goes away.
 */
export function useSessionGuards(controller: SessionController | null, subject: string) {
  const [asking, setAsking] = useState<Asking | null>(null);
  const answer = useRef<((go: boolean) => void) | null>(null);
  const latest = useRef({ controller, subject });
  useLayoutEffect(() => {
    latest.current = { controller, subject };
  });
  useEffect(() => () => answer.current?.(false), []);

  const ask = useCallback(
    (prompt: Prompt) =>
      new Promise<boolean>((resolve) => {
        answer.current?.(false); // never two at a time: the closes are serialized, so this is only a safety net
        const settle = (go: boolean) => {
          if (answer.current !== settle) return;
          answer.current = null;
          setAsking(null);
          resolve(go);
        };
        answer.current = settle;
        setAsking({ prompt, pending: null, error: null });
      }),
    [],
  );

  const exportStep = useCallback(
    async (intent: GuardIntent): Promise<boolean> => {
      const session = latest.current.controller;
      const snapshot = session?.store.getState() ?? null;
      if (!isExporting(snapshot)) return true;
      const inTransaction = !!snapshot?.state?.inTransaction && !snapshot.state.transactionFailed;
      return ask({ kind: "export", intent, inTransaction });
    },
    [ask],
  );

  const transactionStep = useCallback(
    async (intent: GuardIntent): Promise<boolean> => {
      const snapshot = latest.current.controller?.store.getState();
      const state = snapshot?.state;
      if (!snapshot || !state?.inTransaction) return true;
      return ask({
        kind: "transaction",
        intent,
        failed: state.transactionFailed,
        connected: snapshot.phase === "open",
      });
    },
    [ask],
  );

  const choose = async (choice: Choice) => {
    const settle = answer.current;
    const { controller: session, subject: name } = latest.current;
    if (!settle || !asking || asking.pending) return;
    if (choice === "cancel") return settle(false);
    if (!session || session.store.getState().phase !== "open") {
      // Nothing can be sent; ending the session stops the export and rolls the transaction back.
      if (choice !== "commit") return settle(true);
      setAsking({ ...asking, error: "The session is not connected: the transaction cannot be committed now." });
      return;
    }
    setAsking({ ...asking, pending: choice, error: null });
    if (choice === "stop") {
      await stopExports(session);
      return settle(true);
    }
    const error = await endTransaction(session, choice, name);
    if (error === null) return settle(true);
    setAsking((current) => current && { ...current, pending: null, error });
  };

  const dialog = asking && <GuardDialog asking={asking} subject={subject} onChoose={(choice) => void choose(choice)} />;
  return { exportStep, transactionStep, dialog };
}

function GuardDialog({
  asking,
  subject,
  onChoose,
}: {
  asking: Asking;
  subject: string;
  onChoose: (choice: Choice) => void;
}) {
  const { prompt, pending, error } = asking;
  const { title, message } = promptText(prompt, subject);
  const busy = pending !== null;
  const label = (choice: Exclude<Choice, "cancel">, idle: string) =>
    pending === choice ? PENDING_LABELS[choice] : idle;
  const footer =
    prompt.kind === "export" ? (
      <>
        <Button variant="ghost" onClick={() => onChoose("cancel")} disabled={busy}>
          Cancel
        </Button>
        <Button variant="danger" onClick={() => onChoose("stop")} disabled={busy}>
          {label("stop", prompt.intent === "close" ? "Stop and close" : "Stop and switch")}
        </Button>
      </>
    ) : (
      <>
        <Button variant="ghost" onClick={() => onChoose("cancel")} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => onChoose("rollback")} disabled={busy}>
          {label("rollback", "Rollback")}
        </Button>
        {!prompt.failed && (
          <Button variant="primary" onClick={() => onChoose("commit")} disabled={busy || !prompt.connected}>
            {label("commit", "Commit")}
          </Button>
        )}
      </>
    );
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !busy && onChoose("cancel")}
      title={title}
      width={460}
      footer={footer}
    >
      <p className="text-[13px]" data-selectable>
        {message}
      </p>
      {error && (
        <p className="mt-2 text-[12px] whitespace-pre-wrap text-danger" data-selectable>
          {error}
        </p>
      )}
    </Dialog>
  );
}
