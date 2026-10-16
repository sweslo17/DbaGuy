import { ChevronRight } from "lucide-react";
import { ContextMenu as Radix } from "radix-ui";
import { Fragment, type MouseEvent, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface MenuItem {
  label: string;
  /** Not called for an item with `children`: it opens its submenu. */
  onSelect?: () => void;
  danger?: boolean;
  disabled?: boolean;
  separatorBefore?: boolean;
  /** Shown muted at the right edge, e.g. "⌘C". */
  shortcut?: string;
  /** A submenu. */
  children?: MenuItem[];
}

const CONTENT = "min-w-[180px] rounded-lg border border-border bg-elevated p-1 shadow-xl";
const ITEM =
  "flex cursor-default items-center rounded px-2 py-1 text-[13px] outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-fg data-[disabled]:opacity-40";

function MenuItems({ items }: { items: MenuItem[] }) {
  return items.map((item, i) => (
    <Fragment key={`${i}:${item.label}`}>
      {item.separatorBefore && <Radix.Separator className="my-1 h-px bg-border" />}
      {item.children ? (
        <Radix.Sub>
          <Radix.SubTrigger
            disabled={item.disabled}
            className={cn(ITEM, "data-[state=open]:not-data-[highlighted]:bg-hover")}
          >
            {item.label}
            <ChevronRight size={14} className="-mr-1 ml-auto pl-1 opacity-70" />
          </Radix.SubTrigger>
          <Radix.Portal>
            <Radix.SubContent className={CONTENT} sideOffset={2} alignOffset={-5}>
              <MenuItems items={item.children} />
            </Radix.SubContent>
          </Radix.Portal>
        </Radix.Sub>
      ) : (
        <Radix.Item
          onSelect={item.onSelect}
          disabled={item.disabled}
          className={cn(ITEM, item.danger && "text-danger")}
        >
          {item.label}
          {item.shortcut && <span className="ml-auto pl-6 text-[12px] opacity-60">{item.shortcut}</span>}
        </Radix.Item>
      )}
    </Fragment>
  ));
}

export interface ContextMenuProps {
  items: MenuItem[];
  /** Exactly one element: it becomes the trigger. */
  children: ReactNode;
  /** Runs before the menu opens; `preventDefault()` keeps it closed. */
  onContextMenu?: (e: MouseEvent) => void;
  onOpenChange?: (open: boolean) => void;
}

export function ContextMenu({ items, children, onContextMenu, onOpenChange }: ContextMenuProps) {
  return (
    <Radix.Root onOpenChange={onOpenChange}>
      <Radix.Trigger asChild onContextMenu={onContextMenu}>
        {children}
      </Radix.Trigger>
      <Radix.Portal>
        <Radix.Content className={CONTENT}>
          <MenuItems items={items} />
        </Radix.Content>
      </Radix.Portal>
    </Radix.Root>
  );
}
