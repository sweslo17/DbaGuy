/*
 * Text that cannot be written as UTF-8: a lone UTF-16 surrogate, half of an emoji left by a broken paste or an odd
 * object name. The backend cannot write it to a file (`workspace.json`, one failed write fails every save) and its
 * models treat it unevenly, so the frontend never sends it where it is kept, and `savedEdits` mirrors the backend's
 * refusals (`characterCount` is null for such a text). This is the one place the rule is written; the backend's is
 * `domain/well_formed.py`.
 */

const SURROGATE = /[\uD800-\uDFFF]/;
/** A high surrogate not followed by a low one, or a low surrogate not preceded by a high one. */
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
const LONE_SURROGATES = new RegExp(LONE_SURROGATE.source, "g");

/** `text` with each lone surrogate replaced by U+FFFD; the same string when it has none. */
export function wellFormed(text: string): string {
  return SURROGATE.test(text) ? text.replace(LONE_SURROGATES, "\uFFFD") : text;
}

/**
 * The length of `text` in characters as Python's `len` (the backend's) gives it: a surrogate pair is one, not the two
 * UTF-16 units of `.length`. Null when the text has a lone surrogate: it is not valid text.
 */
export function characterCount(text: string): number | null {
  if (!SURROGATE.test(text)) return text.length;
  if (LONE_SURROGATE.test(text)) return null;
  let pairs = 0; // no lone surrogate: every high surrogate starts a pair
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) pairs += 1;
  }
  return text.length - pairs;
}
