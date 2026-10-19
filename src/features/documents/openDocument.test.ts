import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { ColumnMeta } from "@/lib/api/types";
import { statementKeyspace } from "./documentApi";
import { askOpenDocument, closeOpenDocument, idPrefill, openDocumentAsk, prefillKeyspace } from "./openDocument";

vi.mock("./documentApi", () => ({ statementKeyspace: vi.fn() }));
const lookup = vi.mocked(statementKeyspace);

afterEach(() => vi.resetAllMocks());

const USERS = { bucket: "dbaguy", scope: "app", collection: "users" };
const TRAVEL = { bucket: "travel-sample", scope: "inventory" };

describe("where Open document… starts from a grid (ruling C44)", () => {
  it("is the statement's own keyspace when it names exactly one, whatever the run's context is", async () => {
    lookup.mockResolvedValue(USERS);
    expect(await prefillKeyspace("SELECT * FROM dbaguy.app.users", TRAVEL)).toEqual(USERS);
    expect(lookup).toHaveBeenCalledWith("SELECT * FROM dbaguy.app.users", TRAVEL);
  });

  it("is the run's context when the statement names none or several (the dialog adds the collection opened last)", async () => {
    lookup.mockResolvedValue(null);
    expect(await prefillKeyspace("SELECT * FROM a JOIN b", TRAVEL)).toEqual(TRAVEL);
    expect(await prefillKeyspace("SELECT * FROM a JOIN b", { bucket: "dbaguy" })).toEqual({ bucket: "dbaguy" });
    expect(await prefillKeyspace("SELECT * FROM a JOIN b", null)).toEqual({});
    expect(await prefillKeyspace("SELECT * FROM a JOIN b", undefined)).toEqual({});
  });

  it("is the run's context too when the lookup fails: it never stops the dialog", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    lookup.mockRejectedValue(new ApiError("no", "validation_error", 422));
    expect(await prefillKeyspace("SELECT 1", TRAVEL)).toEqual(TRAVEL);
    lookup.mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await prefillKeyspace("SELECT 1", TRAVEL)).toEqual(TRAVEL);
  });
});

describe("the ID a grid cell suggests", () => {
  const columns = (...names: string[]): ColumnMeta[] => names.map((name) => ({ name, type_name: "", category: "mixed" }));

  it("is the row's id column when it is a string, whichever cell was clicked", () => {
    expect(idPrefill(columns("id", "name"), ["u2", "Bo"], 1)).toBe("u2");
  });

  it("is the clicked cell's text when the row has no id column, or its id is not a string", () => {
    expect(idPrefill(columns("name"), ["Bo"], 0)).toBe("Bo");
    expect(idPrefill(columns("id", "name"), [7, "Bo"], 1)).toBe("Bo");
  });

  it("is nothing for a cell that is not text", () => {
    expect(idPrefill(columns("age"), [41], 0)).toBe("");
    expect(idPrefill(columns("doc"), [{ $type: "json", text: "{}" }], 0)).toBe("");
    expect(idPrefill(columns("a"), [null], 0)).toBe("");
  });
});

describe("asking for the Open document dialog", () => {
  afterEach(closeOpenDocument);

  it("is a new ask each time, so a dialog already open starts over from the new request", () => {
    const first = { profileId: "p", mode: "open", keyspace: { bucket: "a" }, id: "one" } as const;
    const second = { profileId: "p", mode: "open", keyspace: { bucket: "b" }, id: "two" } as const;
    expect(openDocumentAsk()).toBeNull();

    askOpenDocument(first);
    const firstAsk = openDocumentAsk();
    expect(firstAsk?.request).toBe(first);

    askOpenDocument(second); // while the first is still open
    const secondAsk = openDocumentAsk();
    expect(secondAsk?.request).toBe(second);
    expect(secondAsk?.serial).not.toBe(firstAsk?.serial); // the dialog's key

    askOpenDocument(second); // the same request again is an ask of its own too
    expect(openDocumentAsk()?.serial).not.toBe(secondAsk?.serial);

    closeOpenDocument();
    expect(openDocumentAsk()).toBeNull();
  });
});
