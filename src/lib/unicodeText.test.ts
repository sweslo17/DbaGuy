import { describe, expect, it } from "vitest";
import { characterCount, hasLoneSurrogate, wellFormed } from "./unicodeText";

/* The one rule for text that cannot be written as UTF-8 (a lone surrogate), checked against a plain loop. */

const HIGH = "\uD83D";
const LOW = "\uDE00";
const EMOJI = HIGH + LOW;

/** The rule, spelled out one code unit at a time. */
function reference(text: string): { lone: boolean; replaced: string; characters: number } {
  let replaced = "";
  let lone = false;
  let characters = 0;
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    const isHigh = unit >= 0xd800 && unit <= 0xdbff;
    const isLow = unit >= 0xdc00 && unit <= 0xdfff;
    const next = text.charCodeAt(index + 1);
    characters += 1;
    if (isHigh && next >= 0xdc00 && next <= 0xdfff) {
      replaced += text[index] + text[index + 1];
      index += 1;
    } else if (isHigh || isLow) {
      replaced += "\uFFFD";
      lone = true;
    } else replaced += text[index];
  }
  return { lone, replaced, characters };
}

describe("lone surrogates", () => {
  it.each([
    ["", false, "", 0],
    ["plain text", false, "plain text", 10],
    [EMOJI, false, EMOJI, 1],
    [`a${EMOJI}b${EMOJI}`, false, `a${EMOJI}b${EMOJI}`, 4],
    ["a\uD800b", true, "a\uFFFDb", null],
    ["a\uDC00b", true, "a\uFFFDb", null],
    [HIGH, true, "\uFFFD", null],
    [LOW, true, "\uFFFD", null],
    [LOW + HIGH, true, "\uFFFD\uFFFD", null], // in the wrong order: neither is half of a pair
    [EMOJI + HIGH, true, `${EMOJI}\uFFFD`, null],
    [HIGH + EMOJI, true, `\uFFFD${EMOJI}`, null],
    [EMOJI + LOW, true, `${EMOJI}\uFFFD`, null],
    [HIGH + HIGH + LOW, true, `\uFFFD${EMOJI}`, null],
  ])("%j: lone %s, replaced as %j, %s characters", (text, lone, replaced, characters) => {
    expect(hasLoneSurrogate(text)).toBe(lone);
    expect(wellFormed(text)).toBe(replaced);
    expect(characterCount(text)).toBe(characters);
  });

  it("agrees with the plain loop on every text of up to five units made of a letter and the two halves", () => {
    const units = ["a", HIGH, LOW];
    let level = [""];
    const texts = [...level];
    for (let length = 1; length <= 5; length += 1) {
      level = level.flatMap((text) => units.map((unit) => text + unit));
      texts.push(...level);
    }
    expect(texts).toHaveLength(1 + 3 + 9 + 27 + 81 + 243);
    for (const text of texts) {
      const expected = reference(text);
      expect(hasLoneSurrogate(text), text).toBe(expected.lone);
      expect(wellFormed(text), text).toBe(expected.replaced);
      expect(characterCount(text), text).toBe(expected.lone ? null : expected.characters);
    }
  });

  it("gives back the very same string when there is nothing to replace", () => {
    const text = `x${EMOJI}y`;
    expect(wellFormed(text)).toBe(text);
  });
});
