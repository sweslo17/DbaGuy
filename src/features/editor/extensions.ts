import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { PostgreSQL, sql, SQLite, StandardSQL, type SQLDialect } from "@codemirror/lang-sql";
import { bracketMatching, indentOnInput, indentUnit } from "@codemirror/language";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, Prec, type Extension, type StateEffect } from "@codemirror/state";
import {
  drawSelection,
  dropCursor,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { vim } from "@replit/codemirror-vim";
import type { Dialect, EditorSettings } from "@/lib/api/types";
import { editorTheme, fontSizeTheme } from "./theme";

/** What the live settings and the tab's connection decide about an editor. */
export interface EditorOptions {
  fontSize: number;
  tabSize: number;
  wordWrap: boolean;
  vim: boolean;
  upperCaseKeywords: boolean;
  /** The language mode; null (no connection yet) is generic SQL. */
  dialect: Dialect | null;
}

export function editorOptions(editor: EditorSettings, dialect: Dialect | null): EditorOptions {
  return {
    fontSize: editor.font_size,
    tabSize: editor.tab_size,
    wordWrap: editor.word_wrap,
    vim: editor.vim,
    upperCaseKeywords: editor.keyword_case === "upper",
    dialect,
  };
}

const SQL_DIALECTS: Record<Dialect, SQLDialect> = { postgres: PostgreSQL, sqlite: SQLite };

/**
 * A part of the configuration that changes while the editor lives: it sits in its own compartment and is swapped
 * only when its `key` changes (rebuilding vim, for instance, would reset its mode and registers).
 */
interface LiveSlot {
  compartment: Compartment;
  key: (options: EditorOptions) => string;
  build: (options: EditorOptions) => Extension;
}

function slot(key: LiveSlot["key"], build: LiveSlot["build"]): LiveSlot {
  return { compartment: new Compartment(), key, build };
}

const LIVE_SLOTS: readonly LiveSlot[] = [
  // Vim goes before every other keymap (its docs ask for it).
  slot(
    (o) => String(o.vim),
    (o) => (o.vim ? Prec.highest(vim()) : []),
  ),
  slot(
    (o) => String(o.fontSize),
    (o) => fontSizeTheme(o.fontSize),
  ),
  slot(
    (o) => String(o.tabSize),
    (o) => [EditorState.tabSize.of(o.tabSize), indentUnit.of(" ".repeat(o.tabSize))],
  ),
  slot(
    (o) => String(o.wordWrap),
    (o) => (o.wordWrap ? EditorView.lineWrapping : []),
  ),
  slot(
    (o) => `${o.dialect}:${o.upperCaseKeywords}`,
    (o) => sql({ dialect: o.dialect ? SQL_DIALECTS[o.dialect] : StandardSQL, upperCaseKeywords: o.upperCaseKeywords }),
  ),
];

/** The parts that never change: editing, selection, search, comments and the look. */
const STATIC: Extension = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightSpecialChars(),
  history(),
  drawSelection(),
  dropCursor(),
  EditorState.allowMultipleSelections.of(true),
  indentOnInput(),
  bracketMatching(),
  closeBrackets(),
  highlightActiveLine(),
  highlightSelectionMatches(),
  search({ top: true }),
  // ⌘F / ⌘G / ⌘D (select next occurrence) come from searchKeymap, ⌘/ (toggle comment) from defaultKeymap.
  keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, indentWithTab]),
  EditorView.contentAttributes.of({ "aria-label": "SQL editor" }),
  editorTheme(),
];

/** Every base extension of a SQL editor, with the live parts configured from `options`. */
export function baseExtensions(options: EditorOptions): Extension {
  return [LIVE_SLOTS.map((s) => s.compartment.of(s.build(options))), STATIC];
}

/** The effects that move an editor configured with `previous` to `next`; empty when nothing it uses changed. */
export function reconfigureEffects(previous: EditorOptions, next: EditorOptions): StateEffect<unknown>[] {
  return LIVE_SLOTS.filter((s) => s.key(previous) !== s.key(next)).map((s) => s.compartment.reconfigure(s.build(next)));
}
