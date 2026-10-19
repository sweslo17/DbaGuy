import type { EditColumn, EditKind } from "@/lib/api/types";
import { editText, type CellInput } from "./cells";

/*
 * Starting and ending a cell's edit (spec ⑤ §3.6.3, R-05-8), as pure functions: which editor, the text it starts with,
 * and what it stages. There is no typing to edit (R-05-9): an edit starts with Return, F2 or a double click.
 */

/** Text longer than this opens in the drawer's editor, as does JSON and any text with a line break. */
export const INLINE_MAX_CHARS = 1_000;

/** Where a cell's row stands for editing. */
export type RowState = "new" | "loaded" | "deleted" | "key_incomplete";

export interface EditStart {
  readonly column: string;
  readonly kind: EditKind;
  readonly nullable: boolean;
  readonly mode: "inline" | "drawer";
  readonly initialText: string;
  /** Shown while the editor is empty: NULL, DEFAULT, or what typing replaces. */
  readonly placeholder: string | null;
  /** The loaded cell (a loaded row's), for the drawer's "Was:". */
  readonly original: unknown;
}

export interface EditCellInput {
  /** The edit target's column; undefined when the table has no such column (any more). */
  readonly column: EditColumn | undefined;
  readonly name: string;
  readonly row: RowState;
  /** The cell's staged value, if it has one. */
  readonly staged: CellInput | undefined;
  /** The loaded cell; undefined for a new row. */
  readonly original: unknown;
}

const isBytes = (cell: unknown) => !!cell && typeof cell === "object" && (cell as { $type?: string }).$type === "bytes";
const sizeOf = (cell: unknown) => ((cell as { size?: number }).size ?? 0).toLocaleString();

/** Why a row marked for deletion is not edited. */
export const MARKED_DELETED = "This row is marked for deletion: restore it first (⌘⌫)";
/** Why a row whose key was not loaded whole is not edited (nothing could identify it). */
export const KEY_INCOMPLETE = "This row cannot be edited: its key was not loaded completely";

/** Why a cell of `name` cannot be written: the table has no such column (any more), or it is read-only. */
export function notWritable(name: string, column: EditColumn | undefined): string {
  if (!column) return `${name} is not a column of the table any more: Refresh`;
  return `${name} is read-only: ${column.read_only_reason ?? "it cannot be written"}`;
}

/** How the cell's edit starts, or why it cannot (said in a toast). */
export function editStart(input: EditCellInput): EditStart | { refuse: string } {
  const { column, name, row, staged, original } = input;
  if (!column || !column.writable) return { refuse: notWritable(name, column) };
  if (row === "deleted") return { refuse: MARKED_DELETED };
  if (row === "key_incomplete") return { refuse: KEY_INCOMPLETE };
  let initialText = "";
  let placeholder: string | null = null;
  if (staged) {
    if (staged.kind === "value") initialText = staged.text;
    else placeholder = staged.kind === "null" ? "NULL" : "DEFAULT";
  } else if (row === "new") {
    placeholder = "DEFAULT";
  } else if (original === null || original === undefined) {
    placeholder = "NULL";
  } else {
    const loaded = editText(original);
    if (loaded.complete) initialText = loaded.text;
    else if (isBytes(original))
      placeholder = `Hex to replace all ${sizeOf(original)} bytes`; // H5: never the preview
    else
      return { refuse: `This value is too large to edit here (${sizeOf(original)} characters); Set NULL or use SQL` };
  }
  const long = initialText.length > INLINE_MAX_CHARS || /[\r\n]/.test(initialText);
  return {
    column: name,
    kind: column.kind,
    nullable: column.nullable,
    mode: column.kind === "json" || long ? "drawer" : "inline",
    initialText,
    placeholder,
    original,
  };
}

/**
 * What an editor that ends stages (H4): nothing unless something was typed — Return on an untouched NULL or DEFAULT
 * keeps it — and otherwise the text as it is, an empty one included (clearing a value gives the empty string; NULL is
 * only ever set explicitly).
 */
export function editorResult(text: string, touched: boolean): CellInput | null {
  return touched ? { kind: "value", text } : null;
}

/**
 * What an owner answers to Set NULL: `false` refuses it (a NOT NULL column: it says so and keeps the editor open), so
 * nothing has ended and what was typed is still the editor's to stage (K16). Any other answer, none included, ends the
 * edit.
 */
export type SetNullAnswer = boolean | void;
export const setNullEnds = (answer: SetNullAnswer): boolean => answer !== false;

/** The next (`step` 1) or previous (-1) column after `from` that can be written, or -1 (Tab, ⇧Tab). */
export function nextWritable(
  names: readonly string[],
  writable: (name: string) => boolean,
  from: number,
  step: 1 | -1,
): number {
  for (let col = from + step; col >= 0 && col < names.length; col += step) if (writable(names[col])) return col;
  return -1;
}
