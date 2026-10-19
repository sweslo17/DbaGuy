import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import { classifyWriteError, LOST_CONTACT_MESSAGE, problemMessage, type SaveProblem } from "./documentApi";

/*
 * What a failed save, insert or delete means for the tab (ruling C45 and the backend's codes): the one place that tells
 * "the write was refused" from "the write may have happened".
 */

const at = (offset: number) => ({ line: 1 + Math.floor(offset / 10), column: 1 + (offset % 10) });
const api = (code: string, detail?: unknown, message = `server said ${code}`) => new ApiError(message, code, 400, detail);
const problemOf = (error: unknown): SaveProblem => {
  const failure = classifyWriteError(error, at);
  if (failure.kind !== "problem") throw new Error(`not a problem: ${failure.kind}`);
  return failure.problem;
};

describe("a write that lost contact with the backend (ruling C45)", () => {
  it("is outcome_unknown for a fetch TypeError, and for anything that is not an ApiError", () => {
    for (const error of [new TypeError("Load failed"), new Error("boom"), "text", undefined, { code: "document_conflict" }]) {
      expect(problemOf(error)).toEqual({ kind: "outcome_unknown", message: LOST_CONTACT_MESSAGE });
    }
  });

  it("says what the ruling says", () => {
    expect(LOST_CONTACT_MESSAGE).toBe(
      "Lost contact with the backend during the save: it may or may not have been written. Reload to see what is stored.",
    );
  });

  it("keeps the backend's own message for a write_outcome_unknown answer", () => {
    expect(problemOf(api("write_outcome_unknown", undefined, "the connection was lost"))).toEqual({
      kind: "outcome_unknown",
      message: "the connection was lost",
    });
  });
});

describe("the answers with a meaning of their own", () => {
  it("tells a changed document from a deleted one", () => {
    expect(classifyWriteError(api("document_conflict", { cas: "5" }), at)).toEqual({ kind: "changed" });
    expect(classifyWriteError(api("document_missing"), at)).toEqual({ kind: "deleted" });
  });

  it("asks for the password when it is missing, with the error the unlock dialog reads", () => {
    const error = api("password_required", { password: true, ssh_password: false });
    expect(classifyWriteError(error, at)).toEqual({ kind: "password", error });
  });

  it("says the ID exists", () => {
    expect(problemOf(api("document_exists"))).toEqual({ kind: "exists" });
  });

  it("places invalid JSON where the editor counts it", () => {
    expect(problemOf(api("invalid_json", { offset: 25, reason: "Expecting a value" }))).toEqual({
      kind: "invalid_json",
      offset: 25,
      reason: "Expecting a value",
      line: 3,
      column: 6,
    });
  });

  it("lists the numbers the server would change", () => {
    expect(problemOf(api("number_out_of_range", { paths: ["$.u", "$.items[2]"] }))).toEqual({
      kind: "numbers",
      paths: ["$.u", "$.items[2]"],
    });
  });

  it("falls back to the server's message when a detail is not the shape the backend promises", () => {
    for (const detail of [undefined, null, {}, { offset: "3", reason: "x" }, { offset: 3 }, { offset: Infinity, reason: "x" }]) {
      expect(problemOf(api("invalid_json", detail, "Not valid JSON: x"))).toEqual({ kind: "error", message: "Not valid JSON: x" });
    }
    for (const detail of [undefined, { paths: "$.a" }, { paths: [1] }]) {
      expect(problemOf(api("number_out_of_range", detail, "Couchbase would change"))).toEqual({
        kind: "error",
        message: "Couchbase would change",
      });
    }
  });
});

describe("every other answer", () => {
  it("shows the server's message as it is, and is never an unknown outcome", () => {
    for (const code of ["binary_document", "read_only", "query_failed", "validation_failed", "http_error", "internal_error"]) {
      expect(problemOf(api(code, undefined, `message of ${code}`))).toEqual({ kind: "error", message: `message of ${code}` });
    }
  });

  it("does not ask for the position of anything but invalid JSON", () => {
    const refusing = () => {
      throw new Error("position asked for");
    };
    for (const error of [api("document_exists"), api("query_failed"), new TypeError("x"), api("number_out_of_range", { paths: [] })]) {
      expect(() => classifyWriteError(error, refusing)).not.toThrow();
    }
  });
});

describe("the line under the bar", () => {
  it("says where the JSON is invalid, and why", () => {
    expect(problemMessage({ kind: "invalid_json", offset: 3, reason: "Expecting a value", line: 2, column: 4 })).toBe(
      "Not valid JSON at line 2, column 4: Expecting a value",
    );
  });

  it("names the numbers and asks for strings", () => {
    expect(problemMessage({ kind: "numbers", paths: ["$.u", "$.v"] })).toBe(
      "Couchbase would change these numbers: $.u, $.v — write them as strings to save",
    );
  });

  it("says the ID exists, and passes the other messages on", () => {
    expect(problemMessage({ kind: "exists" })).toBe("A document with this ID already exists");
    expect(problemMessage({ kind: "outcome_unknown", message: "gone" })).toBe("gone");
    expect(problemMessage({ kind: "error", message: "read-only" })).toBe("read-only");
  });
});
