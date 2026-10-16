import { useState } from "react";

/**
 * What a result view shows: `current` once the latest request has delivered it; while that request is still `pending`
 * without one, the value shown before it (`stale`), so a re-run (sort, filter, refresh, the same query again) keeps
 * the grid and its layout mounted instead of blanking it. A request that ends without a result (an error, a cancel)
 * shows nothing, and the old value is let go.
 */
export function useHeldResult<T>(current: T | null, pending: boolean): { value: T | null; stale: boolean } {
  const [held, setHeld] = useState<T | null>(current);
  if (current !== null && current !== held) setHeld(current);
  else if (current === null && !pending && held !== null) setHeld(null);
  if (current !== null) return { value: current, stale: false };
  return pending && held !== null ? { value: held, stale: true } : { value: null, stale: false };
}
