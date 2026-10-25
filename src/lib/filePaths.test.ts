import { describe, expect, it } from "vitest";
import { fileNameOf, pathProblem, safeFileStem } from "./filePaths";

/* Paths the data dialogs and the export take. */

const bytes = (text: string) => new TextEncoder().encode(text).length;

describe("a file name's stem", () => {
  it("drops what macOS does not take in a name and the control characters", () => {
    expect(safeFileStem("a/b:c", "dump")).toBe("abc");
    expect(safeFileStem("  a\nb\tc  ", "dump")).toBe("a b c");
  });

  it("falls back to the given name when nothing is left", () => {
    expect(safeFileStem("", "dump")).toBe("dump");
    expect(safeFileStem("/:", "dump")).toBe("dump");
    expect(safeFileStem("\n\t", "export")).toBe("export");
    expect(safeFileStem("   ", "dump")).toBe("dump");
  });

  it("stops at 200 bytes of UTF-8, not at 120 characters, and never inside a character", () => {
    expect(safeFileStem("a".repeat(300), "dump")).toBe("a".repeat(200));
    // CJK: three bytes a character
    const cjk = safeFileStem("資".repeat(100), "dump");
    expect([...cjk]).toHaveLength(66);
    expect(bytes(cjk)).toBe(198);
    // an emoji is four bytes and one code point (two UTF-16 units): 50 of them are exactly 200 bytes
    const emoji = safeFileStem("😀".repeat(60), "dump");
    expect([...emoji]).toHaveLength(50);
    expect(bytes(emoji)).toBe(200);
    expect(emoji).not.toContain("�");
    // mixed text that ends on a multi-byte character just over the cap
    const mixed = safeFileStem(`${"a".repeat(199)}資`, "dump");
    expect(mixed).toBe("a".repeat(199));
    // with a stamp and ".sql" the whole name still fits APFS's 255 bytes
    expect(bytes(`${cjk}_20261023-1405.sql`)).toBeLessThanOrEqual(255);
  });

  it("keeps a stem that is within the cap whole", () => {
    expect(safeFileStem("shop_orders", "dump")).toBe("shop_orders");
    expect(safeFileStem("資料庫", "dump")).toBe("資料庫");
  });
});

describe("paths", () => {
  it("takes an absolute path only, or nothing yet", () => {
    expect(pathProblem("")).toBeNull();
    expect(pathProblem("  ")).toBeNull();
    expect(pathProblem(" /tmp/a.sql ")).toBeNull();
    expect(pathProblem("a.sql")).toBe("Enter an absolute path (starting with /)");
    expect(pathProblem("~/a.sql")).toBe("Enter an absolute path (starting with /)");
  });

  it("names the file of a path", () => {
    expect(fileNameOf("/a/b/x.sql")).toBe("x.sql");
    expect(fileNameOf("x.sql")).toBe("x.sql");
    expect(fileNameOf("/a/b/")).toBe("/a/b/");
  });
});
