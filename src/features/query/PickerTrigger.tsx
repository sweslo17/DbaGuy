import { ChevronDown } from "lucide-react";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

/**
 * A toolbar menu's button, shrinking to a truncated name when crowded; Radix passes its trigger props (and ref)
 * through.
 */
export function PickerTrigger({ children, className, ...props }: ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(
        "flex h-6 min-w-12 items-center gap-1 rounded-md px-1.5 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-50",
        className,
      )}
      {...props}
    >
      {children}
      <ChevronDown size={12} className="shrink-0 opacity-70" />
    </button>
  );
}
