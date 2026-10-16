/** Marks the command palette's dialog: the window shortcuts that switch or close it (⌘P, ⌘⇧P) look past it. */
export const PALETTE_ATTRIBUTE = "data-command-palette";

/** An open modal dialog: every dialog is a Radix one, whose content carries its role and state while open. */
const OPEN_DIALOG = '[role="dialog"][data-state="open"]';
const OPEN_DIALOG_BESIDES_PALETTE = `${OPEN_DIALOG}:not([${PALETTE_ATTRIBUTE}])`;

/**
 * Whether a modal dialog has the keyboard, so a window-level shortcut should do nothing. With `exceptPalette`, the
 * command palette does not count: the shortcuts that switch or close it still work while it is open.
 */
export function isDialogOpen({ exceptPalette = false }: { exceptPalette?: boolean } = {}): boolean {
  return document.querySelector(exceptPalette ? OPEN_DIALOG_BESIDES_PALETTE : OPEN_DIALOG) !== null;
}
