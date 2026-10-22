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
