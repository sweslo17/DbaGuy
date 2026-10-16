import { useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/Toast";
import { useWorkspace } from "@/stores/workspace";
import { busy, type SessionController, type SessionSnapshot } from "../session/SessionController";
import { endTransaction, toastKind, type TransactionEnd } from "../session/transactions";

/** How long the transaction badge pulses after a session message (idle warning, idle rollback). */
const PULSE_MS = 3000;

export interface TransactionControlsArgs {
  tabId: string;
  controller: SessionController | null;
  snapshot: SessionSnapshot | null;
  /** The tab's desired auto-commit mode. */
  autocommit: boolean;
  /** The tab and its connection, e.g. "Query 1 · dev pg": prefixes toasts, which can show while another tab is open. */
  subject: string;
}

/**
 * The query tab's transaction controls (spec §4.2):
 * - the tab's desired auto-commit mode is applied to its session: once the session is open and has reported its
 *   state, and again whenever the toggle changes it (sent once per session and value until it fails; a failure
 *   reverts the toggle to the session's real mode, so it never loops and choosing the mode again retries it);
 * - Commit / Rollback, whose warnings and errors become toasts;
 * - session messages (idle warnings, idle rollbacks) become toasts, each once, and pulse the badge for 3 s.
 */
export function useTransactionControls({ tabId, controller, snapshot, autocommit, subject }: TransactionControlsArgs) {
  const updateTab = useWorkspace((s) => s.updateTab);
  const phase = snapshot?.phase ?? null;
  const state = snapshot?.state ?? null;
  // Never queued behind a running statement: one that opens a transaction would make it fail (`transaction_open`).
  const idle = !busy(snapshot);
  const subjectRef = useRef(subject);
  useEffect(() => {
    subjectRef.current = subject;
  });

  const asked = useRef<{ controller: SessionController; value: boolean } | null>(null);
  // Bumped when a request ended without an answer, so the effect asks again even if nothing else changed.
  const [askAgain, setAskAgain] = useState(0);
  useEffect(() => {
    if (!controller || phase !== "open" || !idle || !state || state.inTransaction) return;
    if (state.autocommit === autocommit) return;
    if (asked.current?.controller === controller && asked.current.value === autocommit) return;
    asked.current = { controller, value: autocommit };
    const requestId = controller.setAutocommit(autocommit);
    const retryable = () => {
      if (asked.current?.controller === controller && asked.current.value === autocommit) asked.current = null;
    };
    controller.whenDone(requestId).then(
      (done) => {
        controller.forget(requestId);
        if (done.interrupted) {
          // The connection dropped before the answer (a reconnect): ask again once the session is back, or Run
          // would wait for this mode forever.
          retryable();
          setAskAgain((n) => n + 1);
          return;
        }
        if (!done.error) return;
        // The toggle goes back to what the session really does; asking for the same mode again sends it again.
        retryable();
        const actual = controller.store.getState().state?.autocommit ?? !autocommit;
        updateTab(tabId, "query", { autocommit: actual });
        toast(
          "error",
          `${subjectRef.current}: could not turn auto-commit ${autocommit ? "on" : "off"}: ${done.error.message}`,
        );
      },
      retryable, // forgotten meanwhile: the next state change asks again if the modes still differ
    );
  }, [controller, phase, idle, state, autocommit, tabId, updateTab, askAgain]);

  const [pulseUntil, setPulseUntil] = useState(0);
  const seen = useRef<{ controller: SessionController | null; id: number }>({ controller: null, id: 0 });
  const messages = snapshot?.sessionMessages;
  useEffect(() => {
    if (!controller || !messages) return;
    if (seen.current.controller !== controller) seen.current = { controller, id: 0 };
    const fresh = messages.filter((message) => message.id > seen.current.id);
    if (fresh.length === 0) return;
    seen.current.id = fresh[fresh.length - 1].id;
    for (const message of fresh) toast(toastKind(message.severity), `${subjectRef.current}: ${message.text}`);
    setPulseUntil(Date.now() + PULSE_MS);
  }, [controller, messages]);
  useEffect(() => {
    if (pulseUntil === 0) return;
    const timer = window.setTimeout(() => setPulseUntil(0), Math.max(0, pulseUntil - Date.now()));
    return () => window.clearTimeout(timer);
  }, [pulseUntil]);

  const end = (kind: TransactionEnd) => {
    if (!controller) return;
    void endTransaction(controller, kind, subject).then((error) => {
      if (error) toast("error", `${subject}: ${kind === "commit" ? "Commit" : "Rollback"} failed: ${error}`);
    });
  };

  return {
    /** A session message just arrived. */
    pulse: pulseUntil > 0,
    commit: () => end("commit"),
    rollback: () => end("rollback"),
    setAutocommit: (enabled: boolean) => updateTab(tabId, "query", { autocommit: enabled }),
  };
}
