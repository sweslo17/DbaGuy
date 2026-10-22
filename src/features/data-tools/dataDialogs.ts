import { create } from "zustand";
import type { DumpForm } from "./dumpForm";
import type { DumpEntry, RunEntry } from "./entryPoints";

/*
 * Which data-tools dialog is open (one at a time, hosted by `DataToolsHost` in the app shell, so a tab that closes never
 * takes a job's dialog with it): a dump's or a run's form, or a job's view (progress, then its outcome). Starting a job
 * turns its form into the job's view; the Jobs list and a toast open a job's view again.
 */

export type DataDialogAsk =
  | { kind: "dump"; entry: DumpEntry; form: DumpForm | null; serial: number }
  | { kind: "run"; entry: RunEntry; serial: number }
  | { kind: "job"; requestId: string; serial: number };

let serial = 0;
const useAsk = create<{ ask: DataDialogAsk | null }>(() => ({ ask: null }));

/** `form`: "Dump again…" starts from the job's own choices. */
export function openDumpDialog(entry: DumpEntry, form: DumpForm | null = null): void {
  useAsk.setState({ ask: { kind: "dump", entry, form, serial: ++serial } });
}

export function openRunDialog(entry: RunEntry): void {
  useAsk.setState({ ask: { kind: "run", entry, serial: ++serial } });
}

export function openJobDialog(requestId: string): void {
  useAsk.setState({ ask: { kind: "job", requestId, serial: ++serial } });
}

export function closeDataDialog(): void {
  useAsk.setState({ ask: null });
}

export const useDataDialogAsk = (): DataDialogAsk | null => useAsk((s) => s.ask);

/** The job whose view is open now, if any (a job that ends in it needs no toast). */
export function shownJob(): string | null {
  const ask = useAsk.getState().ask;
  return ask?.kind === "job" ? ask.requestId : null;
}

/** The Jobs list's popover, opened by its button or by ⌘⇧P "Show jobs". */
const usePopover = create<{ open: boolean }>(() => ({ open: false }));
export const useJobsPopoverOpen = (): boolean => usePopover((s) => s.open);
export const setJobsPopoverOpen = (open: boolean): void => usePopover.setState({ open });
