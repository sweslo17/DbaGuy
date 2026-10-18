import { Command } from "cmdk";
import { Search, type LucideIcon } from "lucide-react";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/*
 * The pieces both palettes are built from (cmdk underneath), so ⌘P and ⌘⇧P look and behave alike: a search box over a
 * list in sections, the highlighted row in the accent colour (as in the context menus), disabled rows greyed.
 */

/** What a row does when chosen: the palette closes first, then `run` runs. */
export type Choose = (run: () => void) => void;

export function PaletteRoot({
  label,
  shouldFilter,
  children,
}: {
  label: string;
  /** false: the palette orders and filters its rows itself (cmdk keeps the order they are rendered in). */
  shouldFilter?: boolean;
  children: ReactNode;
}) {
  return (
    <Command label={label} loop shouldFilter={shouldFilter} className="flex min-h-0 flex-1 flex-col">
      {children}
    </Command>
  );
}

export function PaletteInput({
  value,
  onValueChange,
  placeholder,
  hint,
}: {
  value: string;
  onValueChange: (value: string) => void;
  placeholder: string;
  /** Muted at the right edge, e.g. the shortcut that switches to the other palette. */
  hint?: string;
}) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-border px-3">
      <Search size={14} className="shrink-0 text-muted" aria-hidden />
      <Command.Input
        autoFocus
        value={value}
        onValueChange={onValueChange}
        placeholder={placeholder}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        className="h-10 min-w-0 flex-1 bg-transparent text-[13px] text-fg outline-none placeholder:text-muted"
      />
      {hint && <span className="shrink-0 text-[11px] text-muted">{hint}</span>}
    </div>
  );
}

/** `empty`: shown when no row is left. */
export function PaletteList({ empty, children }: { empty?: string; children: ReactNode }) {
  return (
    <Command.List className="max-h-[min(420px,60vh)] min-h-0 overflow-y-auto overscroll-contain p-1">
      {empty && <Command.Empty className="px-3 py-6 text-center text-[12px] text-muted">{empty}</Command.Empty>}
      {children}
    </Command.List>
  );
}

const GROUP_HEADING =
  "[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted [&_[cmdk-group-heading]]:uppercase";

export function PaletteGroup({ heading, children }: { heading: ReactNode; children: ReactNode }) {
  return (
    <Command.Group heading={heading} className={GROUP_HEADING}>
      {children}
    </Command.Group>
  );
}

export interface PaletteItemProps {
  /** Unique within the palette (cmdk tracks the highlighted row by it); also what its own filter matches. */
  value: string;
  disabled?: boolean;
  /**
   * Not choosable yet (a row of an older search while a newer one loads): greyed, and choosing it does nothing. Unlike
   * `disabled` it keeps its place in the keyboard order, so the highlight is still there once the row is current.
   */
  pending?: boolean;
  onSelect: () => void;
  /**
   * A lucide icon (muted, in the text colour), or a ready-made element such as an `EngineIcon`, which keeps its own
   * colour except on the highlighted row, where its `svg`s take the row's text colour.
   */
  icon?: LucideIcon | ReactElement;
  label: string;
  /** Muted after the label, e.g. a connection or a folder. */
  detail?: string;
  /** Muted at the right edge, e.g. the command's shortcut or whether a toggle is on. */
  hint?: ReactNode;
}

export function PaletteItem({ icon: Icon, label, detail, hint, pending, onSelect, ...item }: PaletteItemProps) {
  return (
    <Command.Item
      {...item}
      onSelect={pending ? undefined : onSelect}
      className={cn(
        "flex h-7 cursor-default items-center gap-2 rounded px-2 text-[13px] outline-none",
        "data-[selected=true]:bg-accent data-[selected=true]:text-accent-fg data-[disabled=true]:opacity-40",
        pending && "opacity-40",
      )}
    >
      {isValidElement(Icon) ? (
        <span className="flex shrink-0 in-data-[selected=true]:[&_svg]:text-accent-fg">{Icon}</span>
      ) : (
        Icon && <Icon size={14} className="shrink-0 opacity-70" aria-hidden />
      )}
      <span className="min-w-0 truncate">{label}</span>
      {detail && <span className="min-w-0 shrink-[100] truncate text-[12px] opacity-60">{detail}</span>}
      {hint !== undefined && <span className="ml-auto shrink-0 pl-4 text-[12px] opacity-60">{hint}</span>}
    </Command.Item>
  );
}

/** A row that is not a choice: a hint, "Searching…", or why something could not be listed. */
export function PaletteNote({ tone = "muted", children }: { tone?: "muted" | "danger"; children: ReactNode }) {
  return (
    <div
      className={cn("px-2 py-1.5 text-[12px] whitespace-pre-wrap", tone === "danger" ? "text-danger" : "text-muted")}
      data-selectable={tone === "danger" ? true : undefined}
    >
      {children}
    </div>
  );
}
