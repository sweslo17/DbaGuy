import { ChevronRight } from "lucide-react";
import { ContextMenu as Radix, DropdownMenu as RadixDropdown } from "radix-ui";
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

/** The Radix menu parts the items are built from: context and dropdown menus share them. */
type MenuParts = Pick<typeof Radix, "Item" | "Separator" | "Sub" | "SubTrigger" | "SubContent" | "Portal">;

function MenuItems({ items, parts: P }: { items: MenuItem[]; parts: MenuParts }) {
  return items.map((item, i) => (
    <Fragment key={`${i}:${item.label}`}>
      {item.separatorBefore && <P.Separator className="my-1 h-px bg-border" />}
      {item.children ? (
        <P.Sub>
          <P.SubTrigger
            disabled={item.disabled}
            className={cn(ITEM, "data-[state=open]:not-data-[highlighted]:bg-hover")}
          >
            {item.label}
            <ChevronRight size={14} className="-mr-1 ml-auto pl-1 opacity-70" />
          </P.SubTrigger>
          <P.Portal>
            <P.SubContent className={CONTENT} sideOffset={2} alignOffset={-5}>
              <MenuItems items={item.children} parts={P} />
            </P.SubContent>
          </P.Portal>
        </P.Sub>
      ) : (
        <P.Item onSelect={item.onSelect} disabled={item.disabled} className={cn(ITEM, item.danger && "text-danger")}>
          <span className="min-w-0 truncate">{item.label}</span>
          {item.shortcut && <span className="ml-auto pl-6 text-[12px] opacity-60">{item.shortcut}</span>}
        </P.Item>
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
          <MenuItems items={items} parts={Radix} />
        </Radix.Content>
      </Radix.Portal>
    </Radix.Root>
  );
}

export interface DropdownMenuProps {
  items: MenuItem[];
  /** Exactly one element (a button): it opens the menu. */
  children: ReactNode;
  align?: "start" | "center" | "end";
  /** Extra classes for the menu, e.g. a maximum width. */
  className?: string;
}

/** A menu opened by a button, with the same items and look as the context menu. */
export function DropdownMenu({ items, children, align = "start", className }: DropdownMenuProps) {
  return (
    <RadixDropdown.Root>
      <RadixDropdown.Trigger asChild>{children}</RadixDropdown.Trigger>
      <RadixDropdown.Portal>
        <RadixDropdown.Content className={cn(CONTENT, className)} align={align} sideOffset={4}>
          <MenuItems items={items} parts={RadixDropdown} />
        </RadixDropdown.Content>
      </RadixDropdown.Portal>
    </RadixDropdown.Root>
  );
}
