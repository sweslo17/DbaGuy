import { Text } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { resendPlan, type Write } from "./useDocumentSave";

/*
 * What goes out once the unlock dialog has its password, or Retry is pressed (ruling C48). The save that asked for the
 * password is over (nothing was sent), so the user may have typed since: Q2 and the confirmation rule (a confirmed write
 * carries only the text that was previewed) both ride on this decision.
 */

const doc = (text: string) => Text.of(text.split("\n"));

const save = (sent: Text, confirmed = false): Write => ({ kind: "save", cas: "5", text: sent.toString(), sent, confirmed });
const insert = (sent: Text, confirmed = false): Write => ({ kind: "insert", text: sent.toString(), sent, confirmed });
const remove = (sent: Text): Write => ({ kind: "remove", cas: "5", sent });

describe("resending a write after the password was asked for", () => {
  it("sends the same write while the editor still holds the text it carries", () => {
    expect(resendPlan(save(doc('{"a": 1}')), doc('{"a": 1}'))).toBe("same"); // an equal document, not the same object
    expect(resendPlan(insert(doc("{}")), doc("{}"))).toBe("same");
  });

  it("makes a fresh save of what the editor holds when the text changed, a confirmed write included", () => {
    expect(resendPlan(save(doc('{"a": 1}')), doc('{"a": 12}'))).toBe("fresh");
    expect(resendPlan(insert(doc("{}")), doc('{"a": 1}'))).toBe("fresh");
    // The preview was of the old text: the confirmation never carries over to other text.
    expect(resendPlan(save(doc('{"b": 1.10}'), true), doc('{"b": 1.101}'))).toBe("fresh");
    expect(resendPlan(insert(doc('{"b": 1.10}'), true), doc("{}"))).toBe("fresh");
  });

  it("sends a confirmed write as it was when its text is unchanged", () => {
    expect(resendPlan(save(doc('{"b": 1.10}'), true), doc('{"b": 1.10}'))).toBe("same");
  });

  it("is a fresh save when there is no editor to compare with", () => {
    expect(resendPlan(save(doc("x")), null)).toBe("fresh");
    expect(resendPlan(insert(doc("x")), null)).toBe("fresh");
  });

  it("sends a delete again while the tab holds the text it was asked for with", () => {
    expect(resendPlan(remove(doc('{"a": 1}')), doc('{"a": 1}'))).toBe("same");
  });

  it("asks again before a delete once the text changed: the user agreed to discard other text (P9)", () => {
    expect(resendPlan(remove(doc('{"a": 1}')), doc('{"a": 12}'))).toBe("ask");
    expect(resendPlan(remove(doc("x")), null)).toBe("ask"); // nothing to compare with: never deleted unasked
  });
});
