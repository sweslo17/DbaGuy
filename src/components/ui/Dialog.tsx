import { Dialog as RadixDialog } from "radix-ui";
import { useLayoutEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { isDialogOpen, PALETTE_ATTRIBUTE } from "@/lib/dialogs";
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
  /**
   * Called once the dialog has closed; `preventDefault` places the focus yourself. Otherwise it goes back to what had
   * it when the dialog took it (when no other dialog is open by then).
   */
  onCloseAutoFocus?: (event: Event) => void;
  /**
   * The command palette: its title is for screen readers only, its content lays itself out (a search box above a list
   * that scrolls), and ⌘P / ⌘⇧P still work while it is open (`isDialogOpen`).
   */
  palette?: boolean;
}) {
  const content = useRef<HTMLDivElement>(null);
  /** What had the focus when the dialog opened; it gets it back on close (ruling R-02c-18). */
  const opener = useRef<Element | null>(null);
  // Recorded as `open` turns true, before anything inside can take the focus: Radix's Portal mounts the content a commit
  // later, so not even a child's `autoFocus` (the close guard's Cancel, Unlock's field) has run yet.
  useLayoutEffect(() => {
    if (open) opener.current = document.activeElement;
  }, [open]);
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
          // Radix would focus the trigger, and these dialogs have none: the focus would be lost on the page.
          onCloseAutoFocus={(e) => {
            const previous = opener.current;
            opener.current = null;
            onCloseAutoFocus?.(e);
            if (e.defaultPrevented) return;
            e.preventDefault();
            const back = previous instanceof HTMLElement && previous !== document.body && previous.isConnected;
            if (back && !isDialogOpen()) previous.focus({ preventScroll: true });
          }}
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
