import { useEffect, useState } from "react";

/** The current time (`Date.now()`), updated every `intervalMs` while `enabled`; frozen (no timer) otherwise. */
export function useNow(intervalMs: number, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now()); // it may have been frozen for a while
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, enabled]);
  return now;
}
