import { Dialog as RadixDialog } from "radix-ui";
import type { ReactNode } from "react";

export function Dialog({
  open,
  onOpenChange,
  title,
  children,
  footer,
  width = 560,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  /** Where focus goes once the dialog has closed (call `preventDefault` to place it yourself). */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 bg-black/30" />
        <RadixDialog.Content
          style={{ width }}
          className="fixed left-1/2 top-[12%] flex max-h-[76vh] -translate-x-1/2 flex-col overflow-hidden rounded-xl border border-border bg-elevated shadow-2xl"
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={onCloseAutoFocus}
        >
          <RadixDialog.Title className="border-b border-border px-4 py-2.5 text-[13px] font-semibold">
            {title}
          </RadixDialog.Title>
          <RadixDialog.Description className="sr-only">{title}</RadixDialog.Description>
          <div className="flex-1 overflow-auto p-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-border px-4 py-2.5">{footer}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
