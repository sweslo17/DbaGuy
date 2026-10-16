import { Dialog as RadixDialog } from "radix-ui";
import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { PALETTE_ATTRIBUTE } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";

export function Dialog({
  open,
  onOpenChange,
  title,
  children,
  footer,
  width = 560,
  onCloseAutoFocus,
  palette = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  /** Where focus goes once the dialog has closed (call `preventDefault` to place it yourself). */
  onCloseAutoFocus?: (event: Event) => void;
  /**
   * The command palette: its title is for screen readers only, its content lays itself out (a search box above a list
   * that scrolls), and the window shortcuts that act through it still work while it is open (`isDialogOpen`).
   */
  palette?: boolean;
}) {
  const content = useRef<HTMLDivElement>(null);
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 bg-black/30" />
        <RadixDialog.Content
          ref={content}
          tabIndex={-1}
          style={{ width }}
          className="fixed left-1/2 top-[12%] flex max-h-[76vh] -translate-x-1/2 flex-col overflow-hidden rounded-xl border border-border bg-elevated shadow-2xl outline-none"
          // Not Radix's first-tabbable: a child with `autoFocus` takes the focus (some mount a render later). Until one
          // does, the dialog itself holds it, so keys never reach what is behind it (ruling R-02c-18).
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            const dialog = content.current;
            if (dialog && !dialog.contains(document.activeElement)) dialog.focus({ preventScroll: true });
          }}
          onCloseAutoFocus={onCloseAutoFocus}
          // Escape that cancels an input method's composition must not close the dialog with it.
          onEscapeKeyDown={(e) => {
            if (isImeKeyEvent(e)) e.preventDefault();
          }}
          {...(palette ? { [PALETTE_ATTRIBUTE]: "" } : {})}
        >
          <RadixDialog.Title
            className={cn("border-b border-border px-4 py-2.5 text-[13px] font-semibold", palette && "sr-only")}
          >
            {title}
          </RadixDialog.Title>
          <RadixDialog.Description className="sr-only">{title}</RadixDialog.Description>
          <div className={palette ? "flex min-h-0 flex-1 flex-col" : "flex-1 overflow-auto p-4"}>{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-border px-4 py-2.5">{footer}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
