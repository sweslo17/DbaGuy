/** Marks the command palette's dialog: the window shortcuts that switch or close it (⌘P, ⌘⇧P) look past it. */
export const PALETTE_ATTRIBUTE = "data-command-palette";

/**
 * Marks a panel that is not modal (the Jobs popover): Radix gives it the dialog role too, but it never has the keyboard
 * the way a dialog does, so the window shortcuts still work while it is open (a click or the focus elsewhere closes it).
 */
export const NON_MODAL_ATTRIBUTE = "data-non-modal";

/** An open modal dialog: every dialog is a Radix one, whose content carries its role and state while open. */
const OPEN_DIALOG = `[role="dialog"][data-state="open"]:not([${NON_MODAL_ATTRIBUTE}])`;
const OPEN_DIALOG_BESIDES_PALETTE = `${OPEN_DIALOG}:not([${PALETTE_ATTRIBUTE}])`;

/**
 * Whether a modal dialog has the keyboard, so a window-level shortcut should do nothing. With `exceptPalette`, the
 * command palette does not count: the shortcuts that switch or close it still work while it is open.
 */
export function isDialogOpen({ exceptPalette = false }: { exceptPalette?: boolean } = {}): boolean {
  return document.querySelector(exceptPalette ? OPEN_DIALOG_BESIDES_PALETTE : OPEN_DIALOG) !== null;
}

/**
 * What a dialog gives the focus back to when it closes: what had it as the dialog opened (`active`), or, when that is
 * inside a non-modal panel (a row of the Jobs list, which closes as the dialog takes the focus and so is gone by
 * then), the button that opened the panel: the one whose `aria-controls` names it (Radix sets it while the panel is
 * open). 06b Task 8 review: the focus went to the page's body.
 */
export function focusToReturn(active: Element | null): Element | null {
  const panel = active?.closest(`[${NON_MODAL_ATTRIBUTE}]`);
  if (!panel?.id) return active;
  const trigger = Array.from(document.querySelectorAll("[aria-controls]")).find(
    (element) => element.getAttribute("aria-controls") === panel.id,
  );
  return trigger ?? active;
}
