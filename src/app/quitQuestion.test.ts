import { describe, expect, it, vi } from "vitest";
import { quitQuestion, runningNow } from "./quitQuestion";

/* What ⌘Q and closing the window ask while jobs run (spec ⑥ CR-5). */

describe("the quit question", () => {
  it("asks nothing when no job runs: the app quits as before", () => {
    expect(quitQuestion([], "quit")).toBeNull();
    expect(quitQuestion([], "close")).toBeNull();
  });

  it("names the running jobs and what quitting does to each kind", () => {
    const asked = quitQuestion([{ job: "dump", title: "Dump public of Local PG to a.sql" }], "quit")!;
    expect([asked.title, asked.keep, asked.confirm]).toEqual(["Quit DbaGuy?", "Keep running", "Quit anyway"]);
    expect(asked.message).toBe(
      "A job is still running: Dump public of Local PG to a.sql. Quitting stops it: a dump writes no file, a run in one " +
        "transaction applies nothing, and a run statement by statement keeps what took effect (a transaction the file " +
        "opened and did not commit is rolled back).",
    );
  });

  it("says that closing the window quits, and lists at most three", () => {
    const jobs = ["a", "b", "c", "d", "e"].map((title) => ({ job: "run_file" as const, title }));
    const asked = quitQuestion(jobs, "close")!;
    expect([asked.title, asked.confirm]).toEqual(["Close the window?", "Close anyway"]);
    expect(asked.message).toBe(
      "5 jobs are still running: a; b; c, and 2 more. Closing the window quits DbaGuy, which stops them: a dump writes " +
        "no file, a run in one transaction applies nothing, and a run statement by statement keeps what took effect (a " +
        "transaction the file opened and did not commit is rolled back).",
    );
  });
});

describe("the jobs running now", () => {
  it("are the listed jobs without an outcome", async () => {
    const listed = [
      { job: "dump" as const, title: "a", outcome: null },
      { job: "run_file" as const, title: "b", outcome: { outcome: "completed" } },
      { job: "run_file" as const, title: "c" },
    ];
    await expect(runningNow(async () => listed, 1_000)).resolves.toEqual([
      { job: "dump", title: "a" },
      { job: "run_file", title: "c" },
    ]);
  });

  it("gives no running jobs when the list never answers, once the timeout passes", async () => {
    vi.useFakeTimers();
    try {
      const pending = runningNow(() => new Promise(() => undefined), 1_000);
      await vi.advanceTimersByTimeAsync(1_000);
      await expect(pending).resolves.toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives no running jobs when the list fails (the backend is gone, and so are its jobs)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      await expect(runningNow(() => Promise.reject(new Error("down")), 1_000)).resolves.toEqual([]);
      expect(warn).toHaveBeenCalledOnce();
    } finally {
      warn.mockRestore();
    }
  });
});
