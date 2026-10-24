import { describe, expect, it } from "vitest";
import { formatElapsed, formatSize } from "./format";

describe("formatElapsed", () => {
  it("counts whole seconds, then minutes, then hours (pre-flight L11)", () => {
    expect(formatElapsed(0)).toBe("0 s");
    expect(formatElapsed(-5)).toBe("0 s");
    expect(formatElapsed(42_400)).toBe("42 s");
    expect(formatElapsed(59_400)).toBe("59 s");
    expect(formatElapsed(125_000)).toBe("2 min 05 s");
    expect(formatElapsed(3_599_000)).toBe("59 min 59 s");
    expect(formatElapsed(3_600_000)).toBe("1 h 00 min");
    expect(formatElapsed(7_500_000)).toBe("2 h 05 min");
  });
});

describe("formatSize", () => {
  it("writes binary units as the backend's own texts do", () => {
    // below 1 KiB, in bytes (06b F-f: a 223-byte dump said "1 KB")
    expect([formatSize(0), formatSize(1), formatSize(223), formatSize(1023)]).toEqual(["0 bytes", "1 byte", "223 bytes", "1,023 bytes"]);
    expect([formatSize(1024), formatSize(1025)]).toEqual(["1 KB", "2 KB"]);
    expect(formatSize(512 * 1024)).toBe("512 KB");
    expect(formatSize(1_048_576)).toBe("1.0 MB");
    expect(formatSize(3 * 1_048_576)).toBe("3.0 MB");
    expect(formatSize(Math.round(148.2 * 1_048_576))).toBe("148.2 MB");
    expect(formatSize(2.31 * 1024 * 1_048_576)).toBe("2.31 GB");
  });
});
