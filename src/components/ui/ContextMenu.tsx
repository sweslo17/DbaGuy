import { ContextMenu as Radix } from "radix-ui";
import type { ReactNode } from "react";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  separatorBefore?: boolean;
}

export function ContextMenu({ items, children }: { items: MenuItem[]; children: ReactNode }) {
  return (
    <Radix.Root>
      <Radix.Trigger asChild>{children}</Radix.Trigger>
      <Radix.Portal>
        <Radix.Content className="min-w-[180px] rounded-lg border border-border bg-elevated p-1 shadow-xl">
          {items.map((item) => (
            <div key={item.label}>
              {item.separatorBefore && <Radix.Separator className="my-1 h-px bg-border" />}
              <Radix.Item
                onSelect={item.onSelect}
                className={`cursor-default rounded px-2 py-1 text-[13px] outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-fg ${item.danger ? "text-danger" : ""}`}
              >
                {item.label}
              </Radix.Item>
            </div>
          ))}
        </Radix.Content>
      </Radix.Portal>
    </Radix.Root>
  );
}
