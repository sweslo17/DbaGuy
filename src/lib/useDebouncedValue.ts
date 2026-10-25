import { useEffect, useState } from "react";

/**
 * `value` as it was `ms` after it last changed: it follows once it has stayed the same that long (the first render
 * returns `value` itself). "The same" is `Object.is`: give it a primitive or a memoised object. One made anew at every
 * render restarts the wait at each render, and once it settles re-renders every `ms` for as long as it is mounted.
 */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}
