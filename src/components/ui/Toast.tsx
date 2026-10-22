import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { create } from "zustand";
import { cn } from "@/lib/cn";
import { withToast, type ToastAction, type ToastItem, type ToastKind } from "./toastItems";

export type { ToastAction, ToastKind } from "./toastItems";

const DURATION_MS: Record<ToastKind, number> = { info: 4000, success: 4000, warning: 4000, error: 8000 };
const STRIPE: Record<ToastKind, string> = {
  info: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-danger",
};

const useToasts = create<{ items: ToastItem[] }>(() => ({ items: [] }));
let nextId = 1;

/**
 * Shows a toast in the bottom-right corner (app-wide; the oldest goes once more than MAX_VISIBLE are shown). One with an
 * `action` stays as long as an error does: there is something to do with it. `key`: a toast about the same thing as
 * one still shown takes its place, with its time anew (a job's ending that the backend's word corrected).
 */
export function toast(kind: ToastKind, text: string, action?: ToastAction, key?: string): void {
  const item: ToastItem = { id: nextId++, kind, text, action, key };
  useToasts.setState((s) => ({ items: withToast(s.items, item) }));
}

function dismiss(id: number): void {
  useToasts.setState((s) => ({ items: s.items.filter((item) => item.id !== id) }));
}

/** Dismisses the toast shown with `key`, if any (what it said is no longer so: a job's guessed ending, corrected). */
export function dismissToast(key: string): void {
  useToasts.setState((s) => (s.items.some((item) => item.key === key) ? { items: s.items.filter((item) => item.key !== key) } : s));
}

/** One toast: dismisses itself after its kind's duration, counted only while the stack is not hovered. */
function ToastView({ item, paused }: { item: ToastItem; paused: boolean }) {
  const remaining = useRef(DURATION_MS[item.action ? "error" : item.kind]);
  useEffect(() => {
    if (paused) return;
    const started = Date.now();
    const timer = window.setTimeout(() => dismiss(item.id), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current -= Date.now() - started;
    };
  }, [paused, item.id]);

  return (
    <div
      role={item.kind === "error" ? "alert" : "status"}
      className="flex overflow-hidden rounded-lg border border-border bg-elevated text-[12px] shadow-xl"
    >
      <div className={cn("w-[3px] shrink-0", STRIPE[item.kind])} />
      <div className="min-w-0 flex-1 px-3 py-2 break-words whitespace-pre-wrap" data-selectable>
        {item.text}
      </div>
      {item.action && (
        <button
          type="button"
          className="my-1 shrink-0 self-center rounded px-2 py-0.5 font-medium text-accent hover:bg-hover"
          onClick={() => {
            dismiss(item.id);
            item.action?.run();
          }}
        >
          {item.action.label}
        </button>
      )}
      <button
        type="button"
        aria-label="Dismiss"
        className="m-1 flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg"
        onClick={() => dismiss(item.id)}
      >
        <X size={12} />
      </button>
    </div>
  );
}

/** The toast stack; mounted once (AppShell). Hovering it pauses every timer. */
export function Toaster() {
  const items = useToasts((s) => s.items);
  const [hovered, setHovered] = useState(false);
  if (items.length === 0) {
    // Dismissing the last toast unmounts the stack without a mouseleave: the next one starts unpaused.
    if (hovered) setHovered(false);
    return null;
  }
  return (
    <div
      role="region"
      aria-label="Notifications"
      className="fixed right-3 bottom-9 z-[100] flex w-[340px] max-w-[calc(100vw-24px)] flex-col gap-2"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {items.map((item) => (
        <ToastView key={item.id} item={item} paused={hovered} />
      ))}
    </div>
  );
}
