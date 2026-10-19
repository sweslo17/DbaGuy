import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, historyKeymap, indentWithTab } from "@codemirror/commands";
import { json } from "@codemirror/lang-json";
import { MSSQL, MySQL, PostgreSQL, sql, SQLDialect, SQLite, StandardSQL } from "@codemirror/lang-sql";
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
import { undoHistory } from "./startingPoint";
import { editorTheme, fontSizeTheme } from "./theme";

/** What an editor holds: SQL (a query tab's text) or JSON (a Couchbase document's). */
export type EditorLanguage = "sql" | "json";

/** What the live settings and the tab's connection decide about an editor. */
export interface EditorOptions {
  fontSize: number;
  tabSize: number;
  wordWrap: boolean;
  vim: boolean;
  upperCaseKeywords: boolean;
  /** The SQL mode; null (no connection yet) is generic SQL. A JSON editor has none. */
  dialect: Dialect | null;
  language: EditorLanguage;
  /** Typing is not possible (e.g. while a script's text is being read). */
  readOnly: boolean;
}

export function editorOptions(
  editor: EditorSettings,
  dialect: Dialect | null,
  readOnly = false,
  language: EditorLanguage = "sql",
): EditorOptions {
  return {
    readOnly,
    language,
    fontSize: editor.font_size,
    tabSize: editor.tab_size,
    wordWrap: editor.word_wrap,
    vim: editor.vim,
    upperCaseKeywords: editor.keyword_case === "upper",
    dialect,
  };
}

/**
 * SQL++ (Couchbase 7.6): its reserved words (as `SQLPP_RESERVED` in the backend), common functions, "…" strings,
 * back-tick names and $name / @name / ? parameters.
 */
const SQLPP = SQLDialect.define({
  keywords:
    "_index_condition _index_key advise all alter analyze and any array as asc at begin between binary boolean break bucket build by cache call case cast cluster collate collection commit committed connect continue correlated cover create current cycle database dataset datastore declare decrement default delete derived desc describe distinct do drop each element else end escape every except exclude execute exists explain false fetch filter first flatten flatten_keys flush following for force from fts function golang grant group groups gsi hash having if ignore ilike in include increment index infer inline inner insert intersect into is isolation javascript join key keys keyspace known language last lateral left let letting level like limit lsm map mapping matched materialized maxvalue merge minvalue missing namespace nest next nextval nl no not nth_value null nulls number object offset on option options or order others outer over parse partition password path pool preceding prepare prev previous prevval primary private privilege probe procedure public range raw read realm recursive reduce rename replace respect restart restrict return returning revoke right role rollback row rows satisfies savepoint schema scope select self sequence set show some start statistics string system then ties to tran transaction trigger true truncate unbounded under union unique unknown unnest unset update upsert use user users using validate value valued values vector via view when where while window with within work xor",
  builtin:
    "meta uuid now_str now_millis millis_to_str str_to_millis date_add_str date_diff_str date_part_str date_trunc_str array_agg array_append array_concat array_contains array_count array_distinct array_flatten array_length array_max array_min array_position array_range array_remove array_sort array_sum object_names object_pairs object_values object_length object_put object_remove object_concat to_number to_string to_boolean to_array to_object is_array is_object is_string is_number is_boolean typename lower upper trim ltrim rtrim length substr replace split contains position concat regexp_contains regexp_like regexp_replace abs ceil floor round trunc sum avg count min max ifmissing ifnull ifmissingornull nvl nvl2 decode greatest least base64_encode base64_decode encode_json decode_json ds_version current_users",
  doubleQuotedStrings: true,
  backslashEscapes: true,
  identifierQuotes: "`",
  hashComments: false,
  slashComments: false,
  spaceAfterDashes: false,
  specialVar: "?$@",
});

const SQL_DIALECTS: Record<Dialect, SQLDialect> = {
  postgres: PostgreSQL,
  sqlite: SQLite,
  mysql: MySQL,
  tsql: MSSQL,
  sqlpp: SQLPP,
};

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
    (o) => String(o.readOnly),
    (o) => EditorState.readOnly.of(o.readOnly),
  ),
  // The language and the label that names it for assistive technology.
  slot(
    (o) => `${o.language}:${o.dialect}:${o.upperCaseKeywords}`,
    (o) =>
      o.language === "json"
        ? [json(), EditorView.contentAttributes.of({ "aria-label": "JSON editor" })]
        : [
            sql({ dialect: o.dialect ? SQL_DIALECTS[o.dialect] : StandardSQL, upperCaseKeywords: o.upperCaseKeywords }),
            EditorView.contentAttributes.of({ "aria-label": "SQL editor" }),
          ],
  ),
];

/** The parts that never change: editing, selection, search, comments and the look. */
const STATIC: Extension = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightSpecialChars(),
  undoHistory,
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
  editorTheme(),
];

/** Every base extension of an editor, with the live parts configured from `options`. */
export function baseExtensions(options: EditorOptions): Extension {
  return [LIVE_SLOTS.map((s) => s.compartment.of(s.build(options))), STATIC];
}

/** The effects that move an editor configured with `previous` to `next`; empty when nothing it uses changed. */
export function reconfigureEffects(previous: EditorOptions, next: EditorOptions): StateEffect<unknown>[] {
  return LIVE_SLOTS.filter((s) => s.key(previous) !== s.key(next)).map((s) => s.compartment.reconfigure(s.build(next)));
}
