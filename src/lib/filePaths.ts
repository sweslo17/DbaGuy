/*
 * Paths the data dialogs and the export take: the save and open panels in the app, a typed absolute path in browser
 * mode (the backend runs on this Mac and reads or writes the path itself).
 */

/** Browser mode's note under a path field: there is no panel, the backend takes the path as typed. */
export const BROWSER_PATH_HINT = "An absolute path on this Mac, in an existing folder; an existing file is replaced";
export const BROWSER_OPEN_HINT = "An absolute path on this Mac";

/** Why a typed path cannot be used yet; null when it is absolute. */
export function pathProblem(text: string): string | null {
  const target = text.trim();
  return target && !target.startsWith("/") ? "Enter an absolute path (starting with /)" : null;
}

/** APFS takes 255 bytes for a name; the stem leaves room for a date and an extension. */
const MAX_STEM_CHARS = 120;

/**
 * A file name's stem from a title: without what macOS does not take in a name (`/`, `:`) and without control characters
 * (a name with a line break, say), trimmed, at most MAX_STEM_CHARS; `fallback` when nothing is left.
 */
export function safeFileStem(title: string, fallback: string): string {
  const stem = [...title.replace(/[/:]/g, "").replace(/\p{Cc}/gu, " ").trim()].slice(0, MAX_STEM_CHARS).join("").trim();
  return stem || fallback;
}

/** The last part of a path ("x.sql" of "/a/b/x.sql"). */
export const fileNameOf = (path: string): string => path.split("/").pop() || path;
