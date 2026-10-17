import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useStore } from "zustand";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toast";
import { useEngineInfo } from "../connections/hooks";
import { busy, isExporting, type SessionController } from "./SessionController";
import { endTransaction, EXPORT_CANCEL_ABORTS_TRANSACTION, type TransactionEnd } from "./transactions";

/**
 * Why the tab's session is about to go away: the tab closes, the tab switches to another connection, or the tab stays
 * but loses its connection (the connection is being deleted).
 */
export type GuardIntent = "close" | "switch" | "disconnect";

/** `inTransaction`: in an open transaction that stopping the export would abort (only where the engine does that). */
type Prompt =
  { kind: "export"; intent: GuardIntent; inTransaction: boolean } | { kind: "transaction"; intent: GuardIntent };

type Choice = "stop" | TransactionEnd | "cancel";

interface Asking {
  prompt: Prompt;
  /** The session asked about: the dialog follows its state live. */
  controller: SessionController;
  /** The chosen action is in progress (the dialog waits for it; Cancel still leaves). */
  pending: Choice | null;
  /** The chosen action failed: the dialog stays, so another choice can be made. */
  error: string | null;
}

/** How long a guard waits for a cancelled export to end before it goes on anyway. */
const STOP_WAIT_MS = 5000;

const PENDING_LABELS: Record<Exclude<Choice, "cancel">, string> = {
  stop: "Stopping…",
  commit: "Committing…",
  rollback: "Rolling back…",
};

const STATEMENT_RUNNING = "A statement is running — wait for it or roll back";

const VERBS: Record<GuardIntent, string> = { close: "close", switch: "switch", disconnect: "disconnect" };
const AFTER: Record<GuardIntent, string> = {
  close: "the tab closes",
  switch: "the connection switches",
  disconnect: "the tab loses its connection",
};

/** Cancels the session's running exports and waits (briefly) until they have ended. */
async function stopExports(controller: SessionController): Promise<void> {
  const exports = Object.values(controller.store.getState().executions).filter(
    (execution) => execution.kind === "export" && execution.status !== "done",
  );
  exports.forEach((execution) => controller.cancel(execution.requestId));
  let timer = 0;
  await Promise.race([
    Promise.allSettled(exports.map((execution) => controller.whenDone(execution.requestId))),
    new Promise((resolve) => {
      timer = window.setTimeout(resolve, STOP_WAIT_MS);
    }),
  ]);
  window.clearTimeout(timer);
}

/**
 * The checks a tab with a session makes before its session goes away (spec §4.2; ②b carries): a running export asks
 * "Stop the export and close?" (and cancels it), an open transaction asks Commit / Rollback / Cancel (and ends it).
 * Each step resolves true to go on, false to keep things as they are; it reads the session's latest state. The dialog
 * follows the session live: while a statement runs, Commit waits (it would queue behind the statement) and Rollback
 * goes on at once, because ending the session interrupts the statement and rolls the transaction back. `dialog` must
 * be rendered by the tab; an unanswered question is answered "no" when the tab goes away.
 */
export function useSessionGuards(controller: SessionController | null, subject: string) {
  const engine = useEngineInfo(controller?.profileId ?? null);
  // Unknown engine (still loading): assume the cancel aborts the transaction, so the warning errs on the safe side.
  const cancelAborts = engine?.cancel_aborts_transaction ?? true;
  const [asking, setAsking] = useState<Asking | null>(null);
  const answer = useRef<((go: boolean) => void) | null>(null);
  const latest = useRef({ controller, subject, cancelAborts });
  useLayoutEffect(() => {
    latest.current = { controller, subject, cancelAborts };
  });
  useEffect(() => () => answer.current?.(false), []);

  const ask = useCallback(
    (prompt: Prompt, session: SessionController) =>
      new Promise<boolean>((resolve) => {
        answer.current?.(false); // never two at a time: the closes are serialized, so this is only a safety net
        const settle = (go: boolean) => {
          if (answer.current !== settle) return;
          answer.current = null;
          setAsking(null);
          resolve(go);
        };
        answer.current = settle;
        setAsking({ prompt, controller: session, pending: null, error: null });
      }),
    [],
  );

  const exportStep = useCallback(
    async (intent: GuardIntent): Promise<boolean> => {
      const { controller: session, cancelAborts } = latest.current;
      const snapshot = session?.store.getState() ?? null;
      if (!session || !isExporting(snapshot)) return true;
      const inTransaction = !!snapshot?.state?.inTransaction && !snapshot.state.transactionFailed;
      return ask({ kind: "export", intent, inTransaction: inTransaction && cancelAborts }, session);
    },
    [ask],
  );

  const transactionStep = useCallback(
    async (intent: GuardIntent): Promise<boolean> => {
      const session = latest.current.controller;
      if (!session?.store.getState().state?.inTransaction) return true;
      return ask({ kind: "transaction", intent }, session);
    },
    [ask],
  );

  const choose = async (choice: Choice) => {
    const settle = answer.current;
    if (!settle || !asking) return;
    if (choice === "cancel") return settle(false);
    if (asking.pending) return;
    const session = asking.controller;
    const snapshot = session.store.getState();
    // Nothing can be sent, or it would wait behind the running statement: ending the session stops the export,
    // interrupts the statement and rolls the transaction back.
    if (snapshot.phase !== "open" || (choice === "rollback" && busy(snapshot))) {
      if (choice !== "commit") return settle(true);
      return; // Commit is disabled then (the dialog says why)
    }
    if (choice === "commit" && busy(snapshot)) return;
    setAsking({ ...asking, pending: choice, error: null });
    if (choice === "stop") {
      await stopExports(session);
      return settle(true);
    }
    const name = latest.current.subject;
    const error = await endTransaction(session, choice, name);
    if (error === null) return settle(true);
    if (answer.current !== settle) {
      toast("error", `${name}: ${choice === "commit" ? "Commit" : "Rollback"} failed: ${error}`); // dismissed meanwhile
      return;
    }
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
  const { prompt, pending, error, controller } = asking;
  const connected = useStore(controller.store, (s) => s.phase === "open");
  const failed = useStore(controller.store, (s) => !!s.state?.transactionFailed);
  // The guard's own COMMIT / ROLLBACK keeps the session busy too: only a statement before it counts.
  const statementRunning = useStore(controller.store, busy) && pending === null;
  const label = (choice: Exclude<Choice, "cancel">, idle: string) =>
    pending === choice ? PENDING_LABELS[choice] : idle;
  const working = pending !== null;
  const verb = VERBS[prompt.intent];
  const after = AFTER[prompt.intent];

  let title: string;
  let message: string;
  let note: string | null = null;
  let footer;
  if (prompt.kind === "export") {
    title = `Stop the export and ${verb}?`;
    message = `An export is still running in ${subject}.${prompt.inTransaction ? ` ${EXPORT_CANCEL_ABORTS_TRANSACTION}.` : ""}`;
    footer = (
      <>
        <Button variant="ghost" autoFocus onClick={() => onChoose("cancel")}>
          Cancel
        </Button>
        <Button variant="danger" onClick={() => onChoose("stop")} disabled={working}>
          {label("stop", `Stop and ${verb}`)}
        </Button>
      </>
    );
  } else {
    title = `${verb[0].toUpperCase()}${verb.slice(1)} with an open transaction?`;
    if (!connected) {
      message = `${subject} has an open transaction, but its session is not connected: Commit is not possible, and Rollback lets ${after}, which ends the session and rolls the transaction back.`;
    } else if (failed) {
      message = `The transaction in ${subject} failed: it can only be rolled back, then ${after}.`;
    } else {
      message = `${subject} has an open transaction. Commit or roll it back, then ${after}.`;
    }
    const commitBlocked = !connected ? "The session is not connected" : statementRunning ? STATEMENT_RUNNING : null;
    if (connected && statementRunning) note = STATEMENT_RUNNING;
    footer = (
      <>
        <Button variant="ghost" autoFocus onClick={() => onChoose("cancel")}>
          Cancel
        </Button>
        <Button
          onClick={() => onChoose("rollback")}
          disabled={working}
          title={statementRunning ? "Ends the session, which interrupts the statement and rolls back" : undefined}
        >
          {label("rollback", "Rollback")}
        </Button>
        {!failed && (
          <Button
            variant="primary"
            onClick={() => onChoose("commit")}
            disabled={working || commitBlocked !== null}
            title={commitBlocked ?? undefined}
          >
            {label("commit", "Commit")}
          </Button>
        )}
      </>
    );
  }
  return (
    <Dialog open onOpenChange={(open) => !open && onChoose("cancel")} title={title} width={460} footer={footer}>
      <p className="text-[13px]" data-selectable>
        {message}
      </p>
      {note && <p className="mt-2 text-[12px] text-warning">{note}</p>}
      {error && (
        <p className="mt-2 text-[12px] whitespace-pre-wrap text-danger" data-selectable>
          {error}
        </p>
      )}
    </Dialog>
  );
}
