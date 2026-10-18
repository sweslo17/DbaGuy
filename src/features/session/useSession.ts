import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useStore } from "zustand";
import { createStore } from "zustand/vanilla";
import { useEngineInfo } from "../connections/hooks";
import { emptySnapshot, SessionController, type SessionSnapshot } from "./SessionController";
import { cancelAbortsTransaction } from "./transactions";

/** Read while there is no controller: hooks cannot be called conditionally. */
const NO_SESSION = createStore<SessionSnapshot>()(() => emptySnapshot("closed"));

/**
 * Owns a `SessionController` for the component's lifetime (only while `enabled` is not false and there is a
 * profile). The controller is created in an effect and closed in its cleanup, so StrictMode's double mount opens and
 * disposes a session cleanly. `retry` closes the current controller and opens a fresh session. The controller reads the
 * engine's `cancel_aborts_transaction` as it is when it needs it (engines load after the controller is created).
 */
export function useSession(
  profileId: string | null,
  database: string | null,
  options: { enabled?: boolean } = {},
): { controller: SessionController | null; snapshot: SessionSnapshot | null; retry: () => void } {
  const enabled = options.enabled !== false;
  const [controller, setController] = useState<SessionController | null>(null);
  const [attempt, setAttempt] = useState(0);
  const engine = useEngineInfo(profileId);
  const cancelAborts = useRef(cancelAbortsTransaction(engine));
  useLayoutEffect(() => {
    cancelAborts.current = cancelAbortsTransaction(engine);
  });

  useEffect(() => {
    if (!enabled || profileId === null) return;
    const created = new SessionController(profileId, database, {
      cancelAbortsTransaction: () => cancelAborts.current,
    });
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
