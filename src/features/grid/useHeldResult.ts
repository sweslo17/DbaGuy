import { useState } from "react";

/**
 * What a result view shows: `current` once the latest request has delivered it; while that request is still `pending`
 * without one, the value shown before it (`stale`), so a re-run (sort, filter, refresh, the same query again) keeps
 * the grid and its layout mounted instead of blanking it. A request that ends without a result (an error, a cancel)
 * shows nothing, and the old value is let go, unless `keep`: the caller says the request changes nothing on screen (a
 * Refresh that ended early), and the held value stays, no longer stale.
 *
 * `same` says when a new `current` holds what is held already (e.g. `sameElements` for a list rebuilt on every event):
 * the held value is then kept and returned, so nothing is set while rendering and the value keeps its identity.
 */
export function useHeldResult<T>(
  current: T | null,
  pending: boolean,
  same: (a: T, b: T) => boolean = Object.is,
  keep = false,
): { value: T | null; stale: boolean } {
  const [held, setHeld] = useState<T | null>(current);
  const changed = current !== null && (held === null || !same(current, held));
  if (changed) setHeld(current);
  else if (current === null && !pending && !keep && held !== null) setHeld(null);
  if (current !== null) return { value: changed ? current : held, stale: false };
  if (held === null || !(pending || keep)) return { value: null, stale: false };
  return { value: held, stale: pending };
}
