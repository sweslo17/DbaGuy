/** "1 row", "1,500 rows". */
export const plural = (count: number, noun: string) => `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;

/** "850 ms", "1.24 s", "2 min 05 s". Rounded before the unit is chosen, so 999.6 ms is "1.00 s", not "1000 ms". */
export function formatDuration(ms: number): string {
  const millis = Math.round(ms);
  if (millis < 1000) return `${millis} ms`;
  const hundredths = Math.round(ms / 10);
  if (hundredths < 6000) return `${(hundredths / 100).toFixed(2)} s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}

const DAY_MS = 86_400_000;
/** The largest unit that fits is used: months and years are approximate (the exact time belongs in a tooltip). */
const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * DAY_MS],
  ["month", 30 * DAY_MS],
  ["week", 7 * DAY_MS],
  ["day", DAY_MS],
  ["hour", 3_600_000],
  ["minute", 60_000],
];
/** English, like the rest of the UI (ruling R-02c-11); absolute times stay in the user's locale. */
const relativeTime = new Intl.RelativeTimeFormat("en", { numeric: "auto", style: "short" });

/** How long before `now` the time `at` was (ms since the epoch): "now", "5 min. ago", "yesterday", "3 wk. ago". */
export function formatRelativeTime(at: number, now: number): string {
  const elapsed = Math.max(0, now - at); // a clock that went back shows "now", never "in 2 min."
  for (const [unit, size] of RELATIVE_UNITS) {
    if (elapsed >= size) return relativeTime.format(-Math.floor(elapsed / size), unit);
  }
  return relativeTime.format(0, "second");
}

/**
 * How long after `now` the time `at` is (ms since the epoch), in the same units as `formatRelativeTime`: "in 5 days",
 * "in 13 yr." (years are 365 days, months 30: approximate, so the exact time belongs in a tooltip). Under a minute it
 * counts seconds, never "now" for a time that has not come. "expired" once `at` is not after `now`.
 */
export function formatTimeUntil(at: number, now: number): string {
  const remaining = at - now;
  if (remaining <= 0) return "expired";
  for (const [unit, size] of RELATIVE_UNITS) {
    if (remaining >= size) return relativeTime.format(Math.floor(remaining / size), unit);
  }
  return relativeTime.format(Math.max(1, Math.floor(remaining / 1000)), "second");
}
