type KeyEventLike = Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "key" | "code">;

const LATIN_LETTER = /^[a-z]$/;

/**
 * ⌘ + `key` (a lower-case letter, or a character such as ","), with ⇧ exactly when `shift`, never with ⌥ or ⌃. A letter
 * matches the character typed, in either case; the physical key (`code`) counts only when the layout types no Latin
 * letter for it (e.g. a Cyrillic layout), so a remapped Latin layout (Dvorak) keeps its own letters.
 */
export function isCommandKey(e: KeyEventLike, key: string, { shift = false }: { shift?: boolean } = {}): boolean {
  if (!e.metaKey || e.ctrlKey || e.altKey || e.shiftKey !== shift) return false;
  const typed = e.key.toLowerCase();
  if (typed === key) return true;
  return LATIN_LETTER.test(key) && !LATIN_LETTER.test(typed) && e.code === `Key${key.toUpperCase()}`;
}

/** ⌘, (Settings): what the settings dialog opens on and what the command palette gives way to. */
export const isSettingsKey = (e: KeyEventLike) => isCommandKey(e, ",");
