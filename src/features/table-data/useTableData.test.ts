import { describe, expect, it } from "vitest";
import { MAX_PAGE_SIZE, PAGE_SIZE } from "../session/SessionController";
import { keptPageSize } from "./useTableData";

describe("keptPageSize", () => {
  it("reloads as many rows as are loaded", () => {
    expect(keptPageSize(1_200)).toBe(1_200);
    expect(keptPageSize(PAGE_SIZE + 1)).toBe(PAGE_SIZE + 1);
  });

  it("never asks for less than a page, so a short or empty result still reads a whole page", () => {
    expect(keptPageSize(0)).toBe(PAGE_SIZE);
    expect(keptPageSize(PAGE_SIZE - 1)).toBe(PAGE_SIZE);
    expect(keptPageSize(PAGE_SIZE)).toBe(PAGE_SIZE);
  });

  it("never asks for more than the backend accepts (page_size is at most 10,000)", () => {
    expect(MAX_PAGE_SIZE).toBe(10_000);
    expect(keptPageSize(MAX_PAGE_SIZE)).toBe(MAX_PAGE_SIZE);
    expect(keptPageSize(MAX_PAGE_SIZE + 1)).toBe(MAX_PAGE_SIZE);
    expect(keptPageSize(100_000)).toBe(MAX_PAGE_SIZE);
  });
});
