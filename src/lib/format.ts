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
