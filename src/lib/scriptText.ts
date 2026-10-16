import type { Text } from "@codemirror/state";

/*
 * A script file's text against the editor's. CodeMirror joins lines with "\n" whatever the file used, so a file is
 * compared and saved with its own line break: CRLF when it has any, else LF.
 */

/** The line break a file uses. */
export const lineBreakOf = (text: string): string => (text.includes("\r\n") ? "\r\n" : "\n");

/** The editor's text as the file would hold it. */
export const fileText = (doc: Text, lineBreak: string): string => doc.sliceString(0, doc.length, lineBreak);

/** The length of `fileText`, without building it. */
export const fileLength = (doc: Text, lineBreak: string): number =>
  doc.length + (lineBreak.length - 1) * (doc.lines - 1);

/** Whether editor text (lines joined with "\n", as `doc.toString()` gives it) is what the file `saved` holds. */
export function matchesFile(text: string, saved: string): boolean {
  const lineBreak = lineBreakOf(saved);
  return (lineBreak === "\n" ? text : text.replaceAll("\n", lineBreak)) === saved;
}
