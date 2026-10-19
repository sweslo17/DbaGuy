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

/** A new row, a loaded row with staged values, or a loaded row marked for deletion. */
export interface RowMark {
  readonly kind: "new" | "changed" | "deleted";
  /** Why the last preview or commit refused the row's change as a whole. */
  readonly problem?: string;
  /** By column index. */
  readonly cells?: Readonly<Record<number, CellMark>>;
}

/** A column header of an editable table: a key column, or one that cannot be written (and why). */
export interface HeaderMark {
  readonly key: boolean;
  readonly readOnly: string | null;
}
