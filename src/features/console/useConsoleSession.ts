import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { SessionSocket, type SocketStatus } from "@/lib/api/session-socket";
import type { SessionInfo } from "@/lib/api/types";

interface ConsoleSession {
  session: SessionInfo | null;
  socket: SessionSocket | null;
  status: SocketStatus;
  error: Error | null;
}

const IDLE: ConsoleSession = { session: null, socket: null, status: "connecting", error: null };

function disposeSession(sessionId: string): void {
  api()
    .then((client) => client.DELETE("/sessions/{session_id}", { params: { path: { session_id: sessionId } } }))
    .catch((error: unknown) => console.warn(`Could not close session ${sessionId}: ${errorMessage(error)}`));
}

/** Opens a backend session (and its WebSocket) for the lifetime of the component; `retry` opens a fresh one. */
export function useConsoleSession(profileId: string, database: string | null) {
  const [state, setState] = useState<ConsoleSession>(IDLE);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let socket: SessionSocket | null = null;
    let sessionId: string | null = null;
    setState(IDLE);
    (async () => {
      try {
        const session = unwrap(await (await api()).POST("/sessions", { body: { profile_id: profileId, database } }));
        sessionId = session.session_id;
        if (cancelled) {
          disposeSession(sessionId); // StrictMode / fast unmount: do not leak the session
          return;
        }
        const opened = new SessionSocket(sessionId);
        socket = opened;
        opened.onStatus((status) => {
          if (cancelled) return;
          setState((s) => ({
            ...s,
            status,
            error: status === "closed" && opened.isRejected ? new Error("The session is no longer available.") : s.error,
          }));
        });
        setState({ session, socket: opened, status: "connecting", error: null });
        await opened.connect();
      } catch (error) {
        if (!cancelled) setState((s) => ({ ...s, error: error instanceof Error ? error : new Error(String(error)) }));
      }
    })();
    return () => {
      cancelled = true;
      socket?.close();
      if (sessionId) disposeSession(sessionId);
    };
  }, [profileId, database, attempt]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);
  return { ...state, retry };
}
