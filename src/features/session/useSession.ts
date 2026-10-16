import { useCallback, useEffect, useState } from "react";
import { useStore } from "zustand";
import { createStore } from "zustand/vanilla";
import { emptySnapshot, SessionController, type SessionSnapshot } from "./SessionController";

/** Read while there is no controller: hooks cannot be called conditionally. */
const NO_SESSION = createStore<SessionSnapshot>()(() => emptySnapshot("closed"));

/**
 * Owns a `SessionController` for the component's lifetime (only while `enabled` is not false). The controller is
 * created in an effect and closed in its cleanup, so StrictMode's double mount opens and disposes a session cleanly.
 * `retry` closes the current controller and opens a fresh session.
 */
export function useSession(
  profileId: string,
  database: string | null,
  options: { enabled?: boolean } = {},
): { controller: SessionController | null; snapshot: SessionSnapshot | null; retry: () => void } {
  const enabled = options.enabled !== false;
  const [controller, setController] = useState<SessionController | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const created = new SessionController(profileId, database);
    setController(created);
    void created.open();
    return () => {
      created.close();
      setController((current) => (current === created ? null : current));
    };
  }, [profileId, database, enabled, attempt]);

  const snapshot = useStore(controller?.store ?? NO_SESSION);
  const retry = useCallback(() => setAttempt((a) => a + 1), []);
  return { controller, snapshot: controller ? snapshot : null, retry };
}
