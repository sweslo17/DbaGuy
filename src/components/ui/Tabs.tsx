import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export function Tabs<T extends string>({
  value,
  onValueChange,
  items,
  className = "mb-3",
}: {
  value: T;
  onValueChange: (value: T) => void;
  items: { value: T; label: string }[];
  /** Spacing around the bar (default: a margin below it). */
  className?: string;
}) {
  return (
    <div role="tablist" className={cn("flex gap-1 border-b border-border", className)}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={value === item.value}
          onClick={() => onValueChange(item.value)}
          className={cn(
            "-mb-px border-b-2 px-3 py-1.5 text-[12px] outline-none focus-visible:text-accent",
            value === item.value ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
          )}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** Renders its children only while `value` (this panel's tab) is the `current` tab. */
export function TabPanel<T extends string>({
  value,
  current,
  children,
}: {
  value: T;
  current: T;
  children: ReactNode;
}) {
  if (value !== current) return null;
  return (
    <div role="tabpanel" className="flex flex-col gap-2.5">
      {children}
    </div>
  );
}
