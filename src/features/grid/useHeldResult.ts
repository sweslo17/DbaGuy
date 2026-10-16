import { useState } from "react";

/** Arrays with the same elements, in the same order. */
export function sameElements<T>(a: readonly T[], b: readonly T[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

/**
 * What a result view shows: `current` once the latest request has delivered it; while that request is still `pending`
 * without one, the value shown before it (`stale`), so a re-run (sort, filter, refresh, the same query again) keeps
 * the grid and its layout mounted instead of blanking it. A request that ends without a result (an error, a cancel)
 * shows nothing, and the old value is let go.
 *
 * `same` says when a new `current` holds what is held already (e.g. `sameElements` for a list rebuilt on every event):
 * the held value is then kept and returned, so nothing is set while rendering and the value keeps its identity.
 */
export function useHeldResult<T>(
  current: T | null,
  pending: boolean,
  same: (a: T, b: T) => boolean = Object.is,
): { value: T | null; stale: boolean } {
  const [held, setHeld] = useState<T | null>(current);
  const changed = current !== null && (held === null || !same(current, held));
  if (changed) setHeld(current);
  else if (current === null && !pending && held !== null) setHeld(null);
  if (current !== null) return { value: changed ? current : held, stale: false };
  return pending && held !== null ? { value: held, stale: true } : { value: null, stale: false };
}
