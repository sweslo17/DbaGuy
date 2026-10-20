/*
 * How the grid marks staged changes (spec ⑤ §3.6.2). The overlay (`features/edits/overlay.ts`) makes the marks; the
 * grid only draws them. A mark object is kept as long as what it marks does not change, so the rows it is passed to
 * stay memoized (Q1).
 */

/** One cell of a row with staged changes. */
export interface CellMark {
  /** The cell holds a staged value (a loaded row's UPDATE). */
  readonly changed: boolean;
  /** "Was: …", the value it was loaded with (its tooltip). */
  readonly was?: string;
  /** A new row's cell left to its default: drawn as DEFAULT, not NULL. */
  readonly isDefault?: boolean;
  /** Why the last preview or commit refused it. */
  readonly problem?: string;
  /** "Changed on the server: now …": the server's value differs from the one the change was staged on. */
  readonly conflict?: string;
}

/**
 * A new row, a loaded row with staged values, a loaded row marked for deletion, or a loaded row whose key another
 * loaded row has too ("repeated_key"): those cannot be told apart, so none is editable and no staged change is drawn
 * on them (they all share one mark object, the overlay's `REPEATED_KEY_MARK`).
 */
export interface RowMark {
  readonly kind: "new" | "changed" | "deleted" | "repeated_key";
  /** Why the last preview or commit refused the row's change as a whole. */
  readonly problem?: string;
  /** Why the row looks as it does when that is no error (its tooltip): a row that cannot be edited, its key repeats. */
  readonly note?: string;
  /** By column index. */
  readonly cells?: Readonly<Record<number, CellMark>>;
}

/** A column header of an editable table: a key column, or one that cannot be written (and why). */
export interface HeaderMark {
  readonly key: boolean;
  readonly readOnly: string | null;
}

/*
 * The marks' look: colour tokens only, mixed into the page background so that a pinned (sticky) cell stays opaque, and
 * written out in full so that Tailwind emits them.
 */
/** A changed cell: warning, light. */
export const CHANGED_CELL = "bg-[color-mix(in_oklab,var(--color-warning)_16%,var(--color-bg))]";
/** Every cell of a new row: success, light. */
export const NEW_ROW = "bg-[color-mix(in_oklab,var(--color-success)_13%,var(--color-bg))]";
/** Every cell of a row marked for deletion: danger, light, struck through. */
export const DELETED_ROW = "bg-[color-mix(in_oklab,var(--color-danger)_11%,var(--color-bg))] line-through";
/** Every cell of a row that cannot be edited because its key repeats: neutral and muted, no error colour. */
export const REPEATED_ROW = "bg-[color-mix(in_oklab,var(--color-muted)_9%,var(--color-bg))] text-muted";
/** A cell the last preview or commit refused (the active cell's accent outline wins). */
export const PROBLEM_CELL = "outline-1 -outline-offset-1 outline-danger";

/** What a row's cells share, by kind: one background (and any text style it brings), or none. */
const ROW_TINTS: Record<RowMark["kind"], string | undefined> = {
  new: NEW_ROW,
  deleted: DELETED_ROW,
  changed: undefined,
  repeated_key: REPEATED_ROW,
};

export const rowTint = (mark: RowMark | undefined): string | undefined => (mark ? ROW_TINTS[mark.kind] : undefined);

/**
 * A cell's one background class (`cn` has no tailwind-merge: two would fight): a changed cell's, else its row's tint,
 * else the opaque page background a pinned cell needs so that the cells it covers do not show through.
 */
export function cellBackground(mark: CellMark | undefined, tint: string | undefined, pinned: boolean): string | undefined {
  if (mark?.changed) return CHANGED_CELL;
  return tint ?? (pinned ? "bg-bg" : undefined);
}

/** What the row-number column shows: the row's number among the loaded rows, `+` (a new row), `−` (marked for deletion). */
export function rowLabel(mark: RowMark | undefined, index: number, leading: number): string {
  if (mark?.kind === "new") return "+";
  if (mark?.kind === "deleted") return "−";
  return String(index - leading + 1);
}

/** The row-number cell's tooltip: why the row's change was refused, else why it cannot be edited. */
export const rowTitle = (mark: RowMark | undefined): string | undefined => mark?.problem ?? mark?.note;

/** A cell's tooltip: its value, then what the mark says (was, conflict, problem), one line each. */
export function cellTitle(value: string | undefined, mark: CellMark | undefined): string | undefined {
  return [value, mark?.was, mark?.conflict, mark?.problem].filter(Boolean).join("\n") || undefined;
}

/** The lines a column header's tooltip adds under its name and type. */
export function headerNotes(mark: HeaderMark | undefined): string[] {
  const notes: string[] = [];
  if (mark?.key) notes.push("Key column");
  if (mark?.readOnly) notes.push(`Read-only: ${mark.readOnly}`);
  return notes;
}

/**
 * The key columns' indexes as a string, "" for none: what the column widths depend on, so that a new `marks` array with
 * the same key columns does not estimate the widths again.
 */
export function keyColumnSignature(marks: readonly (HeaderMark | undefined)[] | undefined): string {
  const keys: number[] = [];
  marks?.forEach((mark, col) => {
    if (mark?.key) keys.push(col);
  });
  return keys.join(",");
}

/** The key columns of a `keyColumnSignature`. */
export const keyColumnsOf = (signature: string): ReadonlySet<number> =>
  new Set(signature === "" ? [] : signature.split(",").map(Number));
