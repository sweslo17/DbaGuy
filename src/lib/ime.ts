import type { KeyboardEvent as ReactKeyboardEvent } from "react";

/**
 * Whether a key event belongs to an input method (e.g. Zhuyin) rather than to the app: during composition, and the
 * Enter that confirms it. WebKit can deliver that Enter after `compositionend`, with `isComposing` false but
 * `keyCode` 229 (ruling R-02c-9), so all three signals count. Every keyboard shortcut and Enter/Escape handler outside
 * CodeMirror checks this first; CodeMirror's keymaps handle composition themselves.
 */
export function isImeKeyEvent(event: KeyboardEvent | ReactKeyboardEvent): boolean {
  const native = "nativeEvent" in event ? event.nativeEvent : event;
  // `keyCode` is deprecated, but it is the only signal WebKit gives for the late Enter.
  return native.isComposing || native.key === "Process" || native.keyCode === 229;
}
