import { useEffect, useState } from "react";

/**
 * `value` as it was `ms` after it last changed: it follows once it has stayed the same that long (the first render
 * returns `value` itself). A value made anew on every render (an object literal) restarts the wait each time.
 */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}
