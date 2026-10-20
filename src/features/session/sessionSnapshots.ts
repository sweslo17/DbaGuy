import type { SessionSnapshot } from "./SessionController";

/*
 * Which session changes a tab renders for (pre-flight H2, 05b Task 10 review). A grid-edit apply sends one `affected`
 * per statement; the commit dialog's progress line and footer follow those from the store themselves, so the tab that
 * owns the session keeps the snapshot it has and is not rendered once per statement (10,000 statements: 3.77 s of main
 * thread in production, 0.38 s with this). Pure.
 */

/**
 * Only a running grid-edit apply counted more statements (`affected`) since `prev`: nothing else of the session or of
 * any request changed, that apply's messages, error and outcome included, and it is still running. Anything else (its
 * outcome, its `finished`, a message, a new request, the phase) is a change the tab renders for.
 */
export function onlyApplyProgress(prev: SessionSnapshot, next: SessionSnapshot): boolean {
  if (prev.phase !== next.phase || prev.info !== next.info || prev.error !== next.error) return false;
  if (prev.state !== next.state || prev.sessionMessages !== next.sessionMessages) return false;
  const ids = Object.keys(next.executions);
  if (ids.length !== Object.keys(prev.executions).length) return false;
  let progressed = false;
  for (const id of ids) {
    const before = prev.executions[id];
    const after = next.executions[id];
    if (before === after) continue;
    if (!before || after.kind !== "edits" || before.status !== "running" || after.status !== "running") return false;
    if (after.affected === before.affected || after.messages !== before.messages) return false;
    if (after.error !== before.error || after.editOutcome !== before.editOutcome) return false;
    progressed = true;
  }
  return progressed;
}

/**
 * The snapshot a tab renders: the one it has while `next` holds only an apply's progress, else `next`. `kept` is the
 * last one returned (the caller keeps it); the same `next` twice gives the same answer, as a store selector must.
 */
export function shownSnapshot(kept: SessionSnapshot | null, next: SessionSnapshot): SessionSnapshot {
  return kept && onlyApplyProgress(kept, next) ? kept : next;
}
