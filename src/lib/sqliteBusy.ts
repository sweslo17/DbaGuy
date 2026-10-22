/** SQLite's busy and locked errors (`sqlite_errorname`): another connection holds the file. */
export const isSqliteBusy = (code: string | null | undefined): boolean =>
  !!code && (code.startsWith("SQLITE_BUSY") || code.startsWith("SQLITE_LOCKED"));

/**
 * Why SQLite said busy, and what to do (ruling ⑤ M4; spec ⑥ 06a F4): `retry` is what to do again ("commit again",
 * "run the file again"). The grid's commit and Run SQL file share it.
 */
export const sqliteBusyNote = (retry: string): string =>
  `Another tab still has an open result of this database file, or another program is using it: finish or close that result, then ${retry}.`;
