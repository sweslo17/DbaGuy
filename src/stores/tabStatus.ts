import { useEffect } from "react";
import { create } from "zustand";

/** What a tab shows beside its title in the tab strip; reported by the tab itself, never persisted. */
export interface TabStatus {
  /** A run, an export or a page is in progress: a spinner. */
  busy: boolean;
  /** The script has unsaved changes: a dot. */
  dirty: boolean;
  /** The script file the tab is bound to no longer exists: the tooltip says so (⌘S recreates it). */
  missing: boolean;
}

const IDLE: TabStatus = { busy: false, dirty: false, missing: false };
const FIELDS = Object.keys(IDLE) as (keyof TabStatus)[];

const useStatuses = create<Record<string, TabStatus>>(() => ({}));

/** The tab strip's view of one tab. */
export function useTabStatus(tabId: string): TabStatus {
  return useStatuses((statuses) => statuses[tabId] ?? IDLE);
}

/** Whether the tab reports a run, an export or a page in progress; for code outside React (a tree click). */
export function isTabBusy(tabId: string): boolean {
  return (useStatuses.getState()[tabId] ?? IDLE).busy;
}

/**
 * Sets the given fields of a tab's status (undefined ones keep their value). Cheap when nothing changes, so it may be
 * called on every keystroke. Only for a mounted tab: the entry goes when it unmounts (`useReportTabStatus`).
 */
export function reportTabStatus(tabId: string, patch: Partial<TabStatus>): void {
  useStatuses.setState((statuses) => {
    const current = statuses[tabId] ?? IDLE;
    const changed = FIELDS.filter((field) => patch[field] !== undefined && patch[field] !== current[field]);
    if (!changed.length) return statuses;
    const next = { ...current };
    for (const field of changed) next[field] = patch[field]!;
    return { ...statuses, [tabId]: next };
  });
}

/** Reports a tab's status while the component is mounted; the entry goes with it. */
export function useReportTabStatus(tabId: string, patch: Partial<TabStatus>): void {
  const { busy, dirty, missing } = patch;
  useEffect(() => reportTabStatus(tabId, { busy, dirty, missing }), [tabId, busy, dirty, missing]);
  useEffect(
    () => () =>
      useStatuses.setState((statuses) => {
        if (!(tabId in statuses)) return statuses;
        const rest = { ...statuses };
        delete rest[tabId];
        return rest;
      }),
    [tabId],
  );
}
