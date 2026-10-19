import { X } from "lucide-react";
import type { KeyboardEvent, ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { isImeKeyEvent } from "@/lib/ime";

interface DrawerFrameProps {
  /** Accessible name of the drawer. */
  label: string;
  title: ReactNode;
  /** Header buttons, left of Close. */
  actions?: ReactNode;
  /** A row under the header that does not scroll with the body. */
  toolbar?: ReactNode;
  /** Runs first; Escape then closes the drawer unless this prevented it. */
  onKeyDown?: (e: KeyboardEvent<HTMLElement>) => void;
  onClose: () => void;
  children: ReactNode;
}

/**
 * Chrome of the grid's right-side drawer: header with a Close button, optional toolbar, scrolling body. The drawer
 * takes focus when clicked (tabIndex -1); Escape anywhere inside closes it, except an input method's own.
 */
export function DrawerFrame({ label, title, actions, toolbar, onKeyDown, onClose, children }: DrawerFrameProps) {
  const keyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (isImeKeyEvent(e)) return; // the Escape that ends a composition is not the drawer's (the editor's text stays)
    onKeyDown?.(e);
    if (e.defaultPrevented || e.key !== "Escape") return;
    e.preventDefault();
    onClose();
  };
  return (
    <aside
      aria-label={label}
      tabIndex={-1}
      className="flex h-full min-w-0 flex-col bg-bg text-[12px] outline-none"
      onKeyDown={keyDown}
    >
      <header className="flex h-8 shrink-0 items-center gap-1 border-b border-border pr-1 pl-3">
        <div className="flex min-w-0 flex-1 items-baseline gap-2 truncate">{title}</div>
        {actions}
        <Button variant="ghost" size="sm" className="px-1" aria-label="Close" title="Close (Esc)" onClick={onClose}>
          <X size={14} />
        </Button>
      </header>
      {toolbar && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-1.5">
          {toolbar}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </aside>
  );
}
