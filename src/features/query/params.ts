import type { EditorState, Extension } from "@codemirror/state";
import { ViewPlugin, type EditorView, type ViewUpdate } from "@codemirror/view";
import type { StatementParams } from "@/lib/api/types";
import { PARAM_NAME } from "@/lib/paramNames";
import { statementsField } from "../editor/statements";
import { parseJson } from "../grid/json";

/*
 * SQL++ parameters (spec §3.11.2). A statement's parameters come from the split (`StatementRange.params`: each named
 * one once as `$name`, every `?`, `$1` … as written); the panel has one field per name and per position, each holding
 * a JSON literal as typed. A run sends each statement only what it uses, the literals trimmed and otherwise as
 * typed (numbers are never converted: the backend puts the text into the request). What the backend takes is
 * decided here once, for the panel, the check before a run and the workspace alike (ruling B40): a name the backend
 * refuses gets no field, so a statement that uses one is sent without it and the server answers with its own error.
 */

/** `StatementParams` takes at most this many positional values. */
export const MAX_POSITIONAL = 200;

/** A name the backend accepts (`check_params`); the lexer also yields `$1abc`, which no value can be given for. */
const NAMED = new RegExp(`^\\$${PARAM_NAME}$`);
const POSITIONAL = /^\$(\d+)$/;

/** What the panel has fields for: `named` are `$name` (never all digits), `positional` is how many `$1` … `$n`. */
export interface ParamSlots {
  named: string[];
  positional: number;
}

export const NO_SLOTS: ParamSlots = { named: [], positional: 0 };

/**
 * One statement's fields: its named parameters in first-use order, and as many positions as it needs (the highest
 * `$k`, or the number of `?`, whichever is larger; `$0` and more than 200 give none).
 */
function usageOf(used: readonly string[]): ParamSlots {
  const named = new Set<string>();
  let questions = 0;
  let highest = 0;
  for (const entry of used) {
    if (entry === "?") questions += 1;
    else if (NAMED.test(entry)) named.add(entry);
    else {
      const k = Number(POSITIONAL.exec(entry)?.[1] ?? 0);
      if (k >= 1 && k <= MAX_POSITIONAL) highest = Math.max(highest, k);
    }
  }
  return { named: [...named], positional: Math.min(Math.max(highest, questions), MAX_POSITIONAL) };
}

/**
 * The fields for a set of statements (`StatementRange.params` of each): the named parameters in first-use order over
 * all of them, and the most positions any one statement needs (positions are per statement, as the server counts).
 */
export function paramSlots(lists: readonly (readonly string[])[]): ParamSlots {
  const named = new Set<string>();
  let positional = 0;
  for (const list of lists) {
    const usage = usageOf(list);
    for (const name of usage.named) named.add(name);
    positional = Math.max(positional, usage.positional);
  }
  return { named: [...named], positional };
}

/** The panel's field names, in order: `[...named, "$1", …, "$n"]`; also the keys of a tab's drafts. */
export function slotNames({ named, positional }: ParamSlots): string[] {
  return [...named, ...Array.from({ length: positional }, (_, index) => `$${index + 1}`)];
}

export function sameSlots(a: ParamSlots, b: ParamSlots): boolean {
  if (a.positional !== b.positional || a.named.length !== b.named.length) return false;
  return a.named.every((name, i) => name === b.named[i]);
}

/** What a field's text is: nothing yet, not JSON, or the kind of JSON value it holds. */
export type ValueKind = "empty" | "invalid" | "string" | "number" | "boolean" | "null" | "object" | "array";

/** The kind of the JSON value in `text` (lossless: a number stays the text it was typed as). */
export function valueKind(text: string): ValueKind {
  const trimmed = text.trim();
  if (trimmed === "") return "empty";
  return parseJson(trimmed)?.root.kind ?? "invalid";
}

/** Under a field whose text is not JSON. */
export const INVALID_HINT = 'A string needs double quotes: "abc"';

/** A parameter a statement uses that has no usable value. */
export type ParamProblem = { name: string; problem: "missing" | "invalid" };

/** The toast that names it (never the value). */
export function problemMessage({ name, problem }: ParamProblem): string {
  return problem === "missing"
    ? `Parameter ${name} has no value`
    : `Parameter ${name} is not a JSON value (a string needs double quotes: "text")`;
}

function draftOf(name: string, drafts: Readonly<Record<string, string>>): { text: string } | ParamProblem {
  const text = drafts[name] ?? "";
  const kind = valueKind(text);
  if (kind === "empty") return { name, problem: "missing" };
  if (kind === "invalid") return { name, problem: "invalid" };
  return { text: text.trim() };
}

/**
 * What a statement is sent with: its named parameters under their names (without the `$`) and its `k` positions as
 * `$1` … `$k`, each the trimmed draft. The first one without a value or with text that is not JSON is the result
 * instead, naming it. `lenient` (Explain: the plan does not need the values) skips such parameters; positions are
 * values by place, so the first position without a usable value ends the list. Nothing used gives `params: null`.
 */
export function paramsFor(
  used: readonly string[],
  drafts: Readonly<Record<string, string>>,
  lenient = false,
): { params: StatementParams | null } | ParamProblem {
  const usage = usageOf(used);
  const named: [string, string][] = [];
  for (const name of usage.named) {
    const draft = draftOf(name, drafts);
    if ("problem" in draft) {
      if (lenient) continue;
      return draft;
    }
    named.push([name.slice(1), draft.text]);
  }
  const positional: string[] = [];
  for (let position = 1; position <= usage.positional; position += 1) {
    const draft = draftOf(`$${position}`, drafts);
    if ("problem" in draft) {
      if (lenient) break;
      return draft;
    }
    positional.push(draft.text);
  }
  if (named.length === 0 && positional.length === 0) return { params: null };
  const params: StatementParams = {};
  // `fromEntries` defines own properties: a parameter named `__proto__` is a name like any other.
  if (named.length) params.named = Object.fromEntries(named);
  if (positional.length) params.positional = positional;
  return { params };
}

/**
 * Reports the document's fields whenever a fresh split of it (not one mapped through edits) has other ones than the
 * last report. The first report is made only when the document uses a parameter, so a tab without any stays quiet.
 */
export function paramsWatcher(onChange: (slots: ParamSlots) => void): Extension {
  return ViewPlugin.fromClass(
    class {
      private reported = NO_SLOTS;

      constructor(view: EditorView) {
        this.check(view.state);
      }

      update(update: ViewUpdate): void {
        if (update.state.field(statementsField, false) !== update.startState.field(statementsField, false)) {
          this.check(update.state);
        }
      }

      private check(state: EditorState): void {
        const split = state.field(statementsField, false);
        if (!split?.fresh || !split.ranges) return;
        const slots = paramSlots(split.ranges.map((range) => range.params));
        if (sameSlots(slots, this.reported)) return;
        this.reported = slots;
        onChange(slots);
      }
    },
  );
}
