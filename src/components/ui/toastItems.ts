/* The toast stack's list (`Toast.tsx`), pure: what a new toast does to the toasts shown. */

export type ToastKind = "info" | "success" | "warning" | "error";

/** A button on a toast: running it dismisses the toast. */
export interface ToastAction {
  label: string;
  run: () => void;
}

export interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
  action?: ToastAction;
  /** What it is about: a newer toast with the same key takes its place while it is shown. */
  key?: string;
}

/** At most this many are shown: the oldest goes. */
export const MAX_VISIBLE = 4;

/**
 * The toasts once `item` (with its own new id) is shown: in the place of the one with its key, if one is shown (a
 * newer word about the same thing; the new id starts its time anew), else after the others, the oldest going past
 * MAX_VISIBLE.
 */
export function withToast(items: readonly ToastItem[], item: ToastItem): ToastItem[] {
  const { key } = item;
  if (key !== undefined && items.some((shown) => shown.key === key)) {
    return items.map((shown) => (shown.key === key ? item : shown));
  }
  return [...items, item].slice(-MAX_VISIBLE);
}
