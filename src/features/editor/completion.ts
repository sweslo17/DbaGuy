import {
  autocompletion,
  insertCompletionText,
  pickedCompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import type { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { api } from "@/lib/api/client";
import type { CompletionItem, Dialect, EditorSettings, QueryContext } from "@/lib/api/types";
import { kindLabel } from "../explorer/kinds";
import { statementWindow } from "./statements";

/*
 * Schema-aware completion (spec §3.4): the backend (`POST /sql/complete`) decides everything, keywords included, so
 * this is the editor's only completion source. A keystroke never waits for it: CodeMirror runs the source in the
 * background, a newer keystroke aborts the request in flight (its answer would be for an older text or cursor), and a
 * request that is not answered within 1.5 s gives no items. Only the statement around the cursor is sent.
 */

export type KeywordCase = EditorSettings["keyword_case"];

/** What the query tab knows when a completion starts; read at each request, so it never needs reconfiguring. */
export interface CompletionTarget {
  profileId: string | null;
  database: string | null;
  /** The profile's dialect; without a profile the backend gets this (or `postgres`) and suggests keywords only. */
  dialect: Dialect | null;
  /** `editor.keyword_case`: "upper" inserts keywords in upper case, "preserve" follows the typed prefix. */
  keywordCase: KeywordCase;
  /** Couchbase: where the tab's unqualified names resolve; null for no context (and for engines without one). */
  queryContext: QueryContext | null;
  /** Couchbase: the field names of the tab's latest results, offered as fields "seen in results"; empty otherwise. */
  seenFields: string[];
}

const DEBOUNCE_MS = 30;
const TIMEOUT_MS = 1_500;
/** The backend's cap on items (`MAX_ITEMS` in `sql/completion.py`): a list this long may be missing matches. */
const BACKEND_ITEM_CAP = 300;
/** Typing more of the same word filters the list in place; anything else asks the backend again. */
const WORD = /^[\p{L}\p{N}_]*$/u;
/** Asked for while typing only after a word character, after `x.`, or after `ON ` (join conditions). */
const TRIGGER = /(?:[\p{L}\p{N}_.]|(?:^|[^\p{L}\p{N}_])on\s+)$/iu;
/** SQL++ back-ticks its names, and completes inside them: typing the back-tick (or what follows it) asks too. */
const SQLPP_TRIGGER = /(?:[\p{L}\p{N}_.`]|(?:^|[^\p{L}\p{N}_])on\s+)$/iu;

/** CodeMirror's completion type (it picks the icon) of each backend kind. */
const TYPES: Record<CompletionItem["kind"], string> = {
  keyword: "keyword",
  table: "class",
  view: "class",
  materialized_view: "class",
  synonym: "class",
  column: "property",
  function: "function",
  schema: "namespace",
  alias: "variable",
  join: "text",
  bucket: "namespace",
  scope: "namespace",
  collection: "class",
  field: "property",
};

/** "preserve": the typed prefix decides, all lower case → lower case, anything else (or nothing typed) → upper case. */
function keywordInCase(keyword: string, typed: string): string {
  const lower = typed === typed.toLowerCase() && typed !== typed.toUpperCase();
  return lower ? keyword.toLowerCase() : keyword.toUpperCase();
}

function keywordApply(text: string, keywordCase: KeywordCase): Completion["apply"] {
  if (keywordCase === "upper") return text.toUpperCase();
  // The prefix is read when the item is picked: typing may have gone on since the request.
  return (view, completion, from, to) =>
    view.dispatch({
      ...insertCompletionText(view.state, keywordInCase(text, view.state.sliceDoc(from, to)), from, to),
      annotations: pickedCompletion.of(completion),
    });
}

function toCompletion(item: CompletionItem, index: number, keywordCase: KeywordCase): Completion {
  const text = item.apply ?? item.label;
  return {
    label: item.label,
    type: TYPES[item.kind],
    detail: item.detail || kindLabel(item.kind).toLowerCase(),
    apply: item.kind === "keyword" ? keywordApply(text, keywordCase) : (item.apply ?? undefined),
    boost: Math.max(-99, Math.min(99, item.boost)),
    // Ties after CodeMirror's own match score keep the backend's order (kind, then shorter names first).
    sortText: String(index).padStart(4, "0"),
  };
}

/** Resolves true after `ms`, or false as soon as `signal` aborts. */
function settle(ms: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve(false);
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve(false);
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function completionSource(target: () => CompletionTarget) {
  let previous: AbortController | null = null;

  return async (context: CompletionContext): Promise<CompletionResult | null> => {
    const sqlpp = target().dialect === "sqlpp";
    if (!context.explicit && !context.matchBefore(sqlpp ? SQLPP_TRIGGER : TRIGGER)) return null;
    previous?.abort();
    const request = new AbortController();
    previous = request;
    // Any edit or cursor move aborts the query (CodeMirror resets on a cursor move by itself): its answer is stale.
    context.addEventListener("abort", () => request.abort(), { onDocChange: true });
    const timeout = window.setTimeout(() => request.abort(), TIMEOUT_MS);
    try {
      if (!context.explicit && !(await settle(DEBOUNCE_MS, request.signal))) return null;
      const { profileId, database, dialect, keywordCase, queryContext, seenFields } = target();
      const { state, pos } = context;
      const bounds = statementWindow(state, pos);
      const client = await api();
      const { data } = await client.POST("/sql/complete", {
        body: {
          text: state.sliceDoc(bounds.from, bounds.to),
          cursor: pos - bounds.from,
          database,
          ...(profileId ? { profile_id: profileId } : { dialect: dialect ?? "postgres" }),
          ...(queryContext ? { query_context: queryContext } : {}),
          ...(seenFields.length ? { seen_fields: seenFields } : {}),
        },
        signal: request.signal,
      });
      const view = context.view;
      const moved = !!view && (view.state.doc !== state.doc || view.state.selection.main.head !== pos);
      if (!data?.items.length || request.signal.aborted || context.aborted || moved) return null;
      const from = bounds.from + data.start;
      const options = data.items.map((item, index) => toCompletion(item, index, keywordCase));
      // Inside a back-ticked name (SQL++) the typed text starts with the back-tick and the items replace the whole name:
      // CodeMirror's own matching would find nothing in it, so the backend's list (already filtered) is shown as it is.
      if (sqlpp && from < pos && state.sliceDoc(from, from + 1) === "`") {
        return { from, to: bounds.from + data.end, options, filter: false };
      }
      return {
        from,
        to: bounds.from + data.end,
        options,
        // A capped list is filtered by the backend again on every keystroke: it may be missing what the next one matches.
        validFor: data.items.length < BACKEND_ITEM_CAP ? WORD : undefined,
      };
    } catch {
      return null; // unreachable, refused or timed out: no items, and typing goes on
    } finally {
      window.clearTimeout(timeout);
      if (previous === request) previous = null;
    }
  };
}

/** The list follows the theme tokens; a list waiting for a newer answer dims and then hides, so it never lingers. */
const completionTheme = EditorView.theme({
  ".cm-tooltip.cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono)", maxHeight: "16em" },
  ".cm-tooltip.cm-tooltip-autocomplete > ul > li": { padding: "2px 8px 2px 4px" },
  ".cm-completionIcon": { color: "var(--muted)", opacity: "1" },
  ".cm-completionIcon-class": { color: "var(--accent)" },
  ".cm-completionLabel": { color: "var(--fg)" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "var(--accent)", fontWeight: "600" },
  ".cm-completionDetail": {
    marginLeft: "1em",
    fontStyle: "normal",
    fontFamily: "var(--font-sans)",
    fontSize: "11px",
    color: "var(--muted)",
  },
  ".cm-tooltip.cm-tooltip-autocomplete-disabled": { animation: "cm-completion-stale 0s linear 150ms forwards" },
  ".cm-tooltip.cm-tooltip-autocomplete-disabled > ul": { opacity: "0.5" },
  ".cm-tooltip.cm-tooltip-autocomplete-disabled > ul > li[aria-selected]": { backgroundColor: "var(--hover)" },
  "@keyframes cm-completion-stale": { to: { visibility: "hidden" } },
});

/**
 * Completion from `POST /sql/complete` as the only source (lang-sql's keyword list is replaced). `target` is read at
 * each request: the tab's connection, database, dialect and keyword case, and for Couchbase its query context and the
 * field names of its results.
 */
export function sqlCompletion(target: () => CompletionTarget): Extension {
  return [
    autocompletion({
      override: [completionSource(target)],
      // The source debounces (30 ms) itself; CodeMirror's own 100 ms would come on top.
      activateOnTypingDelay: 0,
    }),
    completionTheme,
  ];
}
