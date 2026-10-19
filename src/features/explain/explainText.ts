/*
 * What Explain does to a statement's text and what is undone of it (Couchbase, spec §3.10): the one rule for "this
 * statement asks for a plan already". Explain uses it to send no second prefix, the result tabs to tell a profile from
 * the plan of an EXPLAIN, and index advice to never send `ADVISE EXPLAIN …`.
 */

/** What Explain puts before a statement (the plan of it comes back as a result, the statement does not run). */
export const EXPLAIN_PREFIX = "EXPLAIN ";

/** The text starts with EXPLAIN or ADVISE (after whitespace, in any case): a plan is asked for already (ruling B23). */
const PLAN_REQUEST = /^\s*(?:explain|advise)\b/i;
/** The text starts with EXPLAIN (the same word, the same whitespace rule) and what separates it from the statement. */
const LEADING_EXPLAIN = /^\s*explain\b\s*/i;

/** Whether `sql` is an EXPLAIN or an ADVISE already: Explain sends it as it is. */
export function isPlanRequest(sql: string): boolean {
  return PLAN_REQUEST.test(sql);
}

/**
 * The statement an EXPLAIN is of: `sql` without its leading EXPLAIN, or `sql` as it is when it has none (or nothing but
 * the word). It undoes the prefix Explain adds, and removes one the user typed: either way what ADVISE needs.
 */
export function withoutExplain(sql: string): string {
  const rest = sql.replace(LEADING_EXPLAIN, "");
  return rest === "" ? sql : rest;
}
