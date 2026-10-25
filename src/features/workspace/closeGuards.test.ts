import { afterEach, describe, expect, it, vi } from "vitest";
import { useWorkspace, type QueryTabState, type TableTabState } from "@/stores/workspace";
import {
  CLOSE_GUARD_ORDER,
  createDiscardQuestion,
  registerCloseGuard,
  requestCloseTab,
  requestDisconnectTab,
} from "./closeGuards";

/*
 * The "discard?" close question shared by every tab with unsaved work (pre-flight M6: a document's text, ruling C20; a
 * table's staged edits, spec ⑤ R-05-11), and when it is asked: closing only, never for a lost connection.
 */

const question = () => {
  const show = vi.fn();
  const refuse = vi.fn();
  return { show, refuse, question: createDiscardQuestion(show, refuse) };
};

describe("the discard question", () => {
  it("lets a tab with nothing to lose close without asking", () => {
    const { show, refuse, question: q } = question();
    expect(q.decide(false)).toBe(true);
    expect(show).not.toHaveBeenCalled();
    expect(refuse).not.toHaveBeenCalled();
  });

  it("keeps a tab that cannot close now, saying why, without asking", () => {
    const { show, refuse, question: q } = question();
    expect(q.decide({ refuse: "A commit is running" })).toBe(false);
    expect(refuse).toHaveBeenCalledWith("A commit is running");
    expect(show).not.toHaveBeenCalled();
  });

  it("asks, and closes only on Discard", async () => {
    const { show, question: q } = question();
    const discarded = q.decide(true);
    expect(show).toHaveBeenLastCalledWith(true);
    q.answer(true);
    await expect(discarded).resolves.toBe(true);
    expect(show).toHaveBeenLastCalledWith(false);

    const kept = q.decide(true);
    q.answer(false);
    await expect(kept).resolves.toBe(false);
  });

  it("answers an older question no when asked again, and answers each once", async () => {
    const { question: q } = question();
    const first = q.decide(true);
    const second = q.decide(true);
    await expect(first).resolves.toBe(false);
    q.answer(true);
    await expect(second).resolves.toBe(true);
    q.answer(false); // nothing waits: nothing happens
  });

  it("keeps the work of a tab that goes while asking", async () => {
    const { show, question: q } = question();
    const asked = q.decide(true);
    show.mockClear();
    q.dispose();
    await expect(asked).resolves.toBe(false);
    expect(show).not.toHaveBeenCalled(); // the tab is gone: nothing to hide
  });
});

describe("when the question is asked", () => {
  const tab: TableTabState = { id: "table:1", kind: "table", profileId: "p1", path: [], title: "items" };
  afterEach(() => useWorkspace.setState({ tabs: [], activeTabId: null }));

  it("asks a table's staged edits when the tab closes, not when its connection goes", async () => {
    useWorkspace.setState({ tabs: [tab], activeTabId: tab.id });
    const edits = vi.fn(() => true);
    const session = vi.fn(() => true);
    const stopEdits = registerCloseGuard(tab.id, CLOSE_GUARD_ORDER.edits, edits);
    const stopSession = registerCloseGuard(tab.id, CLOSE_GUARD_ORDER.run, session);

    await expect(requestDisconnectTab(tab.id)).resolves.toBe(true);
    expect(edits).not.toHaveBeenCalled();
    expect(session).toHaveBeenCalledWith("disconnect");

    await expect(requestCloseTab(tab.id)).resolves.toBe(true);
    expect(edits).toHaveBeenCalledWith("close");
    expect(useWorkspace.getState().tabs).toEqual([]);
    stopEdits();
    stopSession();
  });

  it("asks a copy reading the tab before anything else, and keeps the tab while it refuses (06c L-f)", async () => {
    const query = { id: "query:1", kind: "query", profileId: "p1", database: null, title: "Q" } as unknown as QueryTabState;
    useWorkspace.setState({ tabs: [query], activeTabId: query.id });
    const asked: string[] = [];
    let reading = true;
    const stops = [
      registerCloseGuard(query.id, CLOSE_GUARD_ORDER.copy, () => (asked.push("copy"), !reading)),
      registerCloseGuard(query.id, CLOSE_GUARD_ORDER.script, () => (asked.push("script"), true)),
      registerCloseGuard(query.id, CLOSE_GUARD_ORDER.transaction, () => (asked.push("transaction"), true)),
    ];

    await expect(requestCloseTab(query.id)).resolves.toBe(false);
    expect([asked, useWorkspace.getState().tabs.length]).toEqual([["copy"], 1]); // nothing else was asked

    asked.length = 0;
    await expect(requestDisconnectTab(query.id)).resolves.toBe(true); // a lost connection is B13's, not the tab's
    expect(asked).toEqual(["transaction"]);

    asked.length = 0;
    reading = false;
    await expect(requestCloseTab(query.id)).resolves.toBe(true);
    expect(asked).toEqual(["copy", "script", "transaction"]);
    stops.forEach((stop) => stop());
  });

  it("keeps the tab when the question is answered no, before any later guard is asked", async () => {
    useWorkspace.setState({ tabs: [tab], activeTabId: tab.id });
    const { show, question: q } = question();
    const later = vi.fn(() => true);
    const stopEdits = registerCloseGuard(tab.id, CLOSE_GUARD_ORDER.edits, () => q.decide(true));
    const stopExport = registerCloseGuard(tab.id, CLOSE_GUARD_ORDER.export, later);

    const closing = requestCloseTab(tab.id);
    await vi.waitFor(() => expect(show).toHaveBeenCalledWith(true));
    q.answer(false);
    await expect(closing).resolves.toBe(false);
    expect(later).not.toHaveBeenCalled();
    expect(useWorkspace.getState().tabs).toEqual([tab]);
    stopEdits();
    stopExport();
  });
});
