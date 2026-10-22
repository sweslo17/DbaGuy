import { Popover as RadixPopover } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";

/**
 * A panel opened by a button (the Jobs list): the menus' look, its own content. Not modal: a click or the focus outside
 * closes it, and Esc closes it and gives the focus back to the button. The content places the focus itself as it opens
 * (else the first control that takes it). `label` names the panel for screen readers.
 */
export function Popover({
  open,
  onOpenChange,
  trigger,
  children,
  className,
  label,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Exactly one element (a button): it opens and closes the panel. */
  trigger: ReactNode;
  children: ReactNode;
  className?: string;
  label?: string;
}) {
  return (
    <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          side="top"
          align="start"
          sideOffset={4}
          collisionPadding={8}
          aria-label={label}
          // Escape that cancels an input method's composition must not close the panel with it (as the dialogs)
          onEscapeKeyDown={(e) => {
            if (isImeKeyEvent(e)) e.preventDefault();
          }}
          className={cn("z-50 rounded-lg border border-border bg-elevated shadow-xl outline-none", className)}
        >
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
