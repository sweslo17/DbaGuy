import { useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/Toast";
import { useWorkspace } from "@/stores/workspace";
import type { SessionController, SessionSnapshot } from "../session/SessionController";
import { endTransaction, toastKind, type TransactionEnd } from "./transactions";

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
 *   state, and again whenever the toggle changes it (sent at most once per session and value, so a failure never
 *   loops; a failure reverts the toggle);
 * - Commit / Rollback, whose warnings and errors become toasts;
 * - session messages (idle warnings, idle rollbacks) become toasts, each once, and pulse the badge for 3 s.
 */
export function useTransactionControls({ tabId, controller, snapshot, autocommit, subject }: TransactionControlsArgs) {
  const updateTab = useWorkspace((s) => s.updateTab);
  const phase = snapshot?.phase ?? null;
  const state = snapshot?.state ?? null;
  const subjectRef = useRef(subject);
  useEffect(() => {
    subjectRef.current = subject;
  });

  const asked = useRef<{ controller: SessionController; value: boolean } | null>(null);
  useEffect(() => {
    if (!controller || phase !== "open" || !state || state.inTransaction || state.autocommit === autocommit) return;
    if (asked.current?.controller === controller && asked.current.value === autocommit) return;
    asked.current = { controller, value: autocommit };
    const requestId = controller.setAutocommit(autocommit);
    controller.whenDone(requestId).then(
      (done) => {
        controller.forget(requestId);
        if (!done.error) return;
        toast(
          "error",
          `${subjectRef.current}: could not turn auto-commit ${autocommit ? "on" : "off"}: ${done.error.message}`,
        );
        updateTab(tabId, { autocommit: !autocommit });
      },
      () => undefined, // forgotten by a run that started meanwhile: its state arrives with `session_state` anyway
    );
  }, [controller, phase, state, autocommit, tabId, updateTab]);

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
    setAutocommit: (enabled: boolean) => updateTab(tabId, { autocommit: enabled }),
  };
}
