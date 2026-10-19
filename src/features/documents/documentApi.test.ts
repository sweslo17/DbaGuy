import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import {
  classifyWriteError,
  confirmedText,
  deleteQuestion,
  problemMessage,
  SERVER_TEXT_SHOWN_MAX,
  unknownOutcome,
  type SaveProblem,
  type WriteKind,
} from "./documentApi";

/*
 * What a failed save, insert or delete means for the tab (ruling C45 and the backend's codes): the one place that tells
 * "the write was refused" from "the write may have happened".
 */

const at = (offset: number) => ({ line: 1 + Math.floor(offset / 10), column: 1 + (offset % 10) });
const api = (code: string, detail?: unknown, message = `server said ${code}`) => new ApiError(message, code, 400, detail);
const problemOf = (error: unknown, kind: WriteKind = "save"): SaveProblem => {
  const failure = classifyWriteError(error, kind, at);
  if (failure.kind !== "problem") throw new Error(`not a problem: ${failure.kind}`);
  return failure.problem;
};

const LOST_SAVE =
  "Lost contact with the backend during the save: it may or may not have been written. Reload to see what is stored.";
const LOST_DELETE =
  "Lost contact with the backend during the delete: it may or may not have been deleted. Reload to see what is stored.";

describe("a write that lost contact with the backend (ruling C45)", () => {
  it("is outcome_unknown for a fetch TypeError, and for anything that is not an ApiError", () => {
    for (const error of [new TypeError("Load failed"), new Error("boom"), "text", undefined, { code: "document_conflict" }]) {
      expect(problemOf(error)).toEqual({ kind: "outcome_unknown", message: LOST_SAVE });
    }
  });

  it("says what the ruling says for a save and an insert, and says delete for a delete", () => {
    const lost = new TypeError("Load failed");
    expect(problemOf(lost, "save")).toEqual({ kind: "outcome_unknown", message: LOST_SAVE });
    expect(problemOf(lost, "insert")).toEqual({ kind: "outcome_unknown", message: LOST_SAVE });
    expect(problemOf(lost, "remove")).toEqual({ kind: "outcome_unknown", message: LOST_DELETE });
  });

  it("keeps the backend's own message for a write_outcome_unknown answer", () => {
    expect(problemOf(api("write_outcome_unknown", undefined, "the connection was lost"))).toEqual({
      kind: "outcome_unknown",
      message: "the connection was lost",
    });
  });

  it("is also what an internal error on a write is: the write may have been done before it failed", () => {
    expect(problemOf(api("internal_error", undefined, "Internal error"))).toMatchObject({ kind: "outcome_unknown" });
    expect(problemOf(api("internal_error"), "remove")).toEqual(
      unknownOutcome("remove", "The backend reported an internal error during the delete"),
    );
    expect(problemOf(api("internal_error"), "insert")).toEqual({
      kind: "outcome_unknown",
      message:
        "The backend reported an internal error during the save: it may or may not have been written. Reload to see what is stored.",
    });
  });

  it("is not what a refusal before the write is: a connection that could not open stays an ordinary error", () => {
    for (const code of ["connection_failed", "tunnel_failed", "secret_store_error", "query_failed"]) {
      expect(problemOf(api(code, undefined, `message of ${code}`))).toEqual({ kind: "error", message: `message of ${code}` });
    }
  });

  it("words an unknown outcome by what the write was", () => {
    expect(unknownOutcome("save", "The backend sent no answer to the save").message).toBe(
      "The backend sent no answer to the save: it may or may not have been written. Reload to see what is stored.",
    );
    expect(unknownOutcome("remove", "Could not show the outcome of the delete (x)").message).toBe(
      "Could not show the outcome of the delete (x): it may or may not have been deleted. Reload to see what is stored.",
    );
  });
});

describe("the answers with a meaning of their own", () => {
  it("tells a changed document from a deleted one", () => {
    expect(classifyWriteError(api("document_conflict", { cas: "5" }), "save", at)).toEqual({ kind: "changed" });
    expect(classifyWriteError(api("document_missing"), "save", at)).toEqual({ kind: "deleted" });
  });

  it("asks for the password when it is missing, with an error the unlock dialog reads that says it was not done", () => {
    const detail = { password: true, ssh_password: false };
    const error = new ApiError("Enter the password for 'cb'", "password_required", 428, detail);
    for (const [kind, said] of [["save", "Not saved"], ["insert", "Not saved"], ["remove", "Not deleted"]] as const) {
      const failure = classifyWriteError(error, kind, at);
      if (failure.kind !== "password") throw new Error(`not a password failure: ${failure.kind}`);
      expect(failure.error).toBeInstanceOf(ApiError);
      expect(failure.error).toMatchObject({ code: "password_required", status: 428, detail }); // what isPasswordRequired and secretsNeeded read
      expect(failure.error.message).toBe(`${said}: the connection needs its password. Enter the password for 'cb'`);
    }
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
    for (const code of ["binary_document", "read_only", "query_failed", "validation_failed", "http_error"]) {
      expect(problemOf(api(code, undefined, `message of ${code}`))).toEqual({ kind: "error", message: `message of ${code}` });
    }
  });

  it("does not ask for the position of anything but invalid JSON", () => {
    const refusing = () => {
      throw new Error("position asked for");
    };
    for (const error of [api("document_exists"), api("query_failed"), new TypeError("x"), api("number_out_of_range", { paths: [] })]) {
      expect(() => classifyWriteError(error, "save", refusing)).not.toThrow();
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

describe("the editor's text after a confirmed save (ruling C13, M4)", () => {
  const SERVER = '{"a":1.1,"b":[1,2]}';
  const PRETTY = '{\n  "a": 1.1,\n  "b": [\n    1,\n    2\n  ]\n}';
  /** A compact server text of exactly `length` characters. */
  const serverOf = (length: number) => `{"s":"${"x".repeat(length - 8)}"}`;

  it("is the server's text laid out as the text that was sent, when that had line breaks", () => {
    expect(confirmedText('{\n  "b": [1, 2],\n  "a": 1.10\n}', SERVER)).toBe(PRETTY);
    expect(confirmedText('{"b":[1,2],"a":1.10}', SERVER)).toBe(SERVER);
  });

  it("is the server's text as it was sent beyond SERVER_TEXT_SHOWN_MAX characters: pretty-printing builds a tree", () => {
    const atMax = serverOf(SERVER_TEXT_SHOWN_MAX);
    expect(atMax).toHaveLength(SERVER_TEXT_SHOWN_MAX);
    expect(confirmedText("{\n}", atMax)).toBe(`{\n  "s": "${"x".repeat(SERVER_TEXT_SHOWN_MAX - 8)}"\n}`);
    const beyond = serverOf(SERVER_TEXT_SHOWN_MAX + 1);
    expect(confirmedText("{\n}", beyond)).toBe(beyond);
  });
});

describe("Delete…'s question (M2)", () => {
  it("says that unsaved edits go with the document", () => {
    expect(deleteQuestion("u 1", false)).toBe("Delete document u 1? This cannot be undone.");
    expect(deleteQuestion("u 1", true)).toBe(
      "Delete document u 1? Your unsaved edits are discarded too. This cannot be undone.",
    );
  });
});
