import { useEffect } from "react";
import { create } from "zustand";

/** What a tab shows beside its title in the tab strip; reported by the tab itself, never persisted. */
export interface TabStatus {
  /** A run, an export or a page is in progress: a spinner. */
  busy: boolean;
  /** The script has unsaved changes (Task 6): a dot. */
  dirty: boolean;
}

const IDLE: TabStatus = { busy: false, dirty: false };

const useStatuses = create<Record<string, TabStatus>>(() => ({}));

/** The tab strip's view of one tab. */
export function useTabStatus(tabId: string): TabStatus {
  return useStatuses((statuses) => statuses[tabId] ?? IDLE);
}

/** Reports a tab's status while the component is mounted; the entry goes with it. */
export function useReportTabStatus(tabId: string, patch: Partial<TabStatus>): void {
  const { busy, dirty } = patch;
  useEffect(() => {
    useStatuses.setState((statuses) => {
      const current = statuses[tabId] ?? IDLE;
      const next = { busy: busy ?? current.busy, dirty: dirty ?? current.dirty };
      return next.busy === current.busy && next.dirty === current.dirty ? statuses : { ...statuses, [tabId]: next };
    });
  }, [tabId, busy, dirty]);
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
