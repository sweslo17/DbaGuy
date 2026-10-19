import type { EditTarget } from "@/lib/api/types";
import type { SortKey } from "./useTableData";

/*
 * The sort a table-data tab's first browse uses (05b K13). PostgreSQL's unsorted order is the heap's, where an UPDATE
 * moves the row: after a commit's re-read the row just edited would leave its place, and the next click would land on
 * a neighbour. So where the engine says so (`orders_browse_by_key`), the first browse waits for the table's edit target
 * and sorts by its `stable_order` (the key, when the backend's planner check found that order reads the first page at
 * once). A real sort, shown in the header: the user can change or clear it, and it is applied once, at the first
 * browse. Pure.
 */

/** Once the session could browse, the edit target has this long to come back before the first browse goes unsorted. */
export const TARGET_WAIT_MS = 2_000;

/** What is known of the table's edit target. */
export type TargetRead =
  | { status: "pending" }
  | { status: "error"; passwordRequired: boolean }
  | { status: "success"; target: EditTarget };

/** The table's stable order as sorts (each column ascending); none for a table that is not editable or has none. */
export function stableSorts(target: EditTarget): SortKey[] {
  const order = target.editable ? (target.stable_order ?? []) : [];
  return order.map((column) => ({ column, descending: false }));
}

/**
 * The first browse's sorts, or null while it waits for the target. `ordersByKey` is the engine's capability (null:
 * the engine is not known yet); `waitedOut`: TARGET_WAIT_MS passed since the session opened. A target that failed
 * browses unsorted, except one that needed the password: it is read again once the session has it.
 */
export function firstSorts(ordersByKey: boolean | null, target: TargetRead, waitedOut: boolean): SortKey[] | null {
  if (ordersByKey === false || waitedOut) return [];
  if (ordersByKey === null) return null;
  switch (target.status) {
    case "success":
      return stableSorts(target.target);
    case "error":
      return target.passwordRequired ? null : [];
    case "pending":
      return null;
  }
}
