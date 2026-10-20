import { useQuery } from "@tanstack/react-query";
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { flushSync } from "react-dom";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { toast } from "@/components/ui/Toast";
import type { ColumnMeta, EditColumn, EditKind, EditTarget, PathSegment } from "@/lib/api/types";
import { sameElements } from "@/lib/arrays";
import { writeClipboard } from "@/lib/native";
import { adds, dispatchStaged, readStagedEdits, useStagedEdits } from "@/stores/stagedEdits";
import type { TableTabState } from "@/stores/workspace";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { ROW_HEIGHT } from "../grid/columns";
import type { DataGridHandle, GridSelectionState } from "../grid/DataGrid";
import type { GridEditing } from "../grid/EditorSlot";
import type { EditItems } from "../grid/GridMenu";
import type { HeaderMark } from "../grid/marks";
import type { CellPos, Rect } from "../grid/selection";
import { CellEditor, type EditEnd } from "./CellEditor";
import {
  editorResult,
  editStart,
  nextWritable,
  readOnlyReasonOf,
  type EditStart,
  type SetNullAnswer,
} from "./editing";
import { gridEditKey } from "./editKeys";
import { editsErrorMessage, editTargetQuery } from "./editsApi";
import {
  bindRows,
  displayIndexOf,
  overlay,
  resultColumns,
  rowRefAt,
  wasText,
  type Binding,
  type Overlay,
  type OverlayCache,
} from "./overlay";
import { savedEdits } from "./savedEdits";
import {
  changeCount,
  emptyStaged,
  findChange,
  newChangeId,
  type RowRef,
  type StageAction,
  type StagedEdits,
} from "./staging";
import {
  CHECKING,
  COPY_FAILED,
  COPY_TEXT,
  dispatchRefusal,
  EDIT_MENU_ITEMS,
  editMenu,
  editMode,
  gridKeyAction,
  KEY_CHANGED,
  NOT_EDITABLE,
  NOTHING_SELECTED,
  notKeptHint,
  notNullColumn,
  notNullSkipped,
  pickCells,
  pickRows,
  placeAfterTop,
  REREAD_FIRST,
  rowHasConflict,
  ROWS_LOADING,
  rowStateOf,
  sameLoadedRows,
  selectionFacts,
  stagedInputOf,
  stageRefusal,
  startEdit,
  targetView,
  TOO_MANY_ROWS,
  typedNotStaged,
  wasValueOf,
  type EditGate,
  type EditMenuEntry,
  type EditMenuId,
  type EditMenuInput,
  type GridLookup,
  type ShownTop,
} from "./tableEditing";
import { ValueEditor } from "./ValueEditor";

/*
 * A table-data tab's grid editing (spec ⑤ §3.6): the edit target, the staged changes laid over the rows, the cell being
 * edited, the keys and the menus. Staged changes live in `stores/stagedEdits.ts`, never in React state; the cell being
 * edited is this hook's, and the text typed into it the editor's own (a keystroke renders the editor alone, Q1). Every
 * stage goes through `stageAction`: nothing while a commit runs, nothing new on held rows (K19) or on a table that cannot
 * be edited, and `dispatchStaged` refuses more than MAX_CHANGES changes and new changes on another key (H13, H15). What
 * is offered when, and why an edit cannot start, are the pure rules of `tableEditing.ts`.
 *
 * The rows are bound to the staged changes by key whenever the result has the key (K8: so a row whose key repeats is
 * known before its first edit, K6), always over the loaded rows, never the display rows (a staged key cell is not the
 * row's key).
 */

const EMPTY_ROWS: unknown[][] = [];
const NO_COLUMNS: ColumnMeta[] = [];
const NO_KINDS: ReadonlyMap<string, EditKind> = new Map();
const NO_TARGET_COLUMNS: ReadonlyMap<string, EditColumn> = new Map();
const NO_HEADER_MARKS: readonly (HeaderMark | undefined)[] = [];
const NO_KEY: readonly string[] = [];
/** What `adds` reads while nothing is staged yet. */
const NOTHING_STAGED = emptyStaged([]);

/** The cell being edited: how its edit started, its row, and the drawer's "Was:" (a loaded row's). */
interface Editing extends EditStart {
  readonly ref: RowRef;
  readonly was: string | null;
}

export interface TableEditsInput {
  tab: TableTabState;
  /** The columns and loaded rows the grid shows (held while a re-run loads); undefined before the first result. */
  columns: ColumnMeta[] | undefined;
  rows: unknown[][] | undefined;
  /** The engine edits rows (`row_editing`, CR-1). */
  rowEditing: boolean;
  /** The tab's session is open (a password it needed has been given). */
  sessionOpen: boolean;
  /** The rows shown are held while a re-run loads them again (dimmed): nothing new is staged on them (K19). */
  stale: boolean;
  /** Why nothing may change the staging now (a commit is running, Task 10), or null. */
  locked: string | null;
  gridRef: RefObject<DataGridHandle | null>;
  /** ⌘S in an editor: the edit is staged, then this opens the preview (Task 10). */
  onRequestCommit?: () => void;
  /**
   * For Task 10 (pre-flight M5, 6b's `leading` note): the staging as it was before a commit's outcome took changes away,
   * drawn instead of the store's until the re-read's rows are in, so committed values never flip back and its new rows
   * stay on top meanwhile (the selection and the anchor keep their rows). Display only: every action acts on the store's
   * staging, and nothing new is staged while it is drawn. Task 10 sets it and drops it.
   */
  afterglow?: StagedEdits | null;
}

export interface TableEdits {
  /** The table's edit target once read (null before, while its read failed, and without row editing). */
  target: EditTarget | null;
  /** The target could not be read: the edit bar says why, with Retry. */
  targetError: string | null;
  /** The target still wants a password after the session opened: the tab asks for it (L13), then reads it again. */
  targetUnlock: Error | null;
  /** The target is being read: the edit bar says nothing of it yet. */
  checking: boolean;
  refetchTarget: () => void;
  /** Rows can be edited now: the table can be, its rows are not held, no commit runs (K19). */
  editable: boolean;
  /** The table's rows can be edited (its target), the changes are on its key; `paused` says why not now. */
  tableEditable: boolean;
  /** Why nothing new can be staged right now though the table can be edited (rows loading, a commit), or null. */
  paused: string | null;
  /** Why the table's rows cannot be edited (the target's reason), or null. */
  readOnlyReason: string | null;
  /** Changes were staged on another key than the table's now (H13). */
  keyChanged: boolean;
  staged: StagedEdits | null;
  changes: number;
  /** Why the staged changes are not kept across a restart, or not all of them (`savedEdits`, K5, K3b), or null. */
  notKept: string | null;
  display: Overlay;
  /** The grid's `edits`; undefined when nothing can be edited and nothing is staged or drawn. */
  grid: GridEditing | undefined;
  onSelectionChange: (state: GridSelectionState | null) => void;
  /** The loaded row at a display index (null for a new row): Filter by this value uses the server's value. */
  resultRowAt: (index: number) => unknown[] | null;
  /** Where a change (one of its columns, or its first) is shown now, or null (Show in grid). */
  locate: (changeId: string, column: string | null) => CellPos | null;
  addRow: () => void;
  deleteSelected: () => void;
  setNullSelected: () => void;
  discardAll: () => void;
  undo: () => void;
  redo: () => void;
  /**
   * Ends an open editor now, staging what was typed (Task 11: a tab closing asks about it too, L8); whether one was
   * open (its input had the focus, which is then on the page).
   */
  flushEditor: () => boolean;
}

/** A row's or cell's React key in the grid: an editor is never reused for another cell (K18). */
const cellKey = (ref: RowRef, column: string) =>
  JSON.stringify([ref.kind, ref.kind === "insert" ? ref.id : ref.rowKey, column]);

function menuItem(entry: EditMenuEntry, run: () => void): MenuItem {
  const look = EDIT_MENU_ITEMS[entry.id];
  return {
    label: look.label,
    shortcut: entry.keyless ? undefined : look.shortcut,
    danger: look.danger,
    separatorBefore: look.separatorBefore,
    disabled: entry.disabled,
    title: entry.title,
    onSelect: run,
  };
}

/** Puts an editor's text on the clipboard: what a refused staging of it offers (K20). */
const copyTyped = (text: string) => void writeClipboard(text).catch(() => toast("error", COPY_FAILED));

/**
 * The table's edit target as the tab shows it (`targetView`): read through `editTargetQuery`, the read `useTableData`
 * makes when the tab is first shown (one read for both), again on Refresh (DDL since). A read that wants the password
 * before the session opened is the session's to ask for; one that still wants it after asks itself (L13), told apart
 * by its failures counted since the session opened.
 */
function useTargetView(profileId: string, path: PathSegment[], rowEditing: boolean, sessionOpen: boolean) {
  const query = useQuery({ ...editTargetQuery(profileId, path), enabled: rowEditing && sessionOpen });
  const { refetch } = query;
  const refetchTarget = useCallback(() => void refetch(), [refetch]);
  const [failuresAtOpen, setFailuresAtOpen] = useState<number | null>(null);
  if (sessionOpen && failuresAtOpen === null) setFailuresAtOpen(query.errorUpdateCount);
  else if (!sessionOpen && failuresAtOpen !== null) setFailuresAtOpen(null);
  const view = targetView({
    rowEditing,
    status: query.status,
    fetching: query.isFetching,
    passwordRequired: query.isError && isPasswordRequired(query.error),
    failedSinceOpen: failuresAtOpen !== null && query.errorUpdateCount > failuresAtOpen,
  });
  return {
    view,
    target: view === "read" ? (query.data ?? null) : null,
    targetError: view === "error" ? editsErrorMessage(query.error) : null,
    targetUnlock: view === "unlock" ? query.error : null,
    refetchTarget,
  };
}

export function useTableEdits(input: TableEditsInput): TableEdits {
  const { tab, rowEditing, sessionOpen, stale, locked, gridRef } = input;
  const columns = input.columns ?? NO_COLUMNS;
  const rows = input.rows ?? EMPTY_ROWS;
  const staged = useStagedEdits(tab.id);
  /** What the grid draws: the store's staging, or Task 10's afterglow of a commit. */
  const drawn = input.afterglow ?? staged;

  const { view, target, targetError, targetUnlock, refetchTarget } = useTargetView(
    tab.profileId,
    tab.path,
    rowEditing,
    sessionOpen,
  );
  const targetColumns = useMemo(
    () => (target ? new Map((target.columns ?? []).map((column) => [column.name, column])) : NO_TARGET_COLUMNS),
    [target],
  );
  const kinds = useMemo<ReadonlyMap<string, EditKind>>(
    () => (target ? new Map((target.columns ?? []).map((column) => [column.name, column.kind])) : NO_KINDS),
    [target],
  );

  const tableKey = target?.editable ? (target.key ?? null) : null;
  // Once per staging, not per render: the Data view renders on every event of its session, and an apply of 10,000
  // changes sends one per statement (pre-flight H2).
  const changes = useMemo(() => (staged ? changeCount(staged) : 0), [staged]);
  const keyChanged = !!staged && !!tableKey && changes > 0 && !sameElements(staged.keyColumns, tableKey);
  const tableEditable = !!target?.editable && !keyChanged;
  const readOnlyReason = target && !target.editable ? (target.reason ?? NOT_EDITABLE) : null;
  const gate: EditGate = {
    locked,
    // The afterglow is drawn until the commit's re-read is in: while it loads, the rows are `stale`; once it was cut
    // short (K17), nothing loads by itself and a Refresh is the way on.
    waiting: stale || !input.columns ? ROWS_LOADING : input.afterglow ? REREAD_FIRST : null,
    readOnly: target
      ? (readOnlyReason ?? (keyChanged ? KEY_CHANGED : null))
      : (targetError ?? (view === "none" ? NOT_EDITABLE : CHECKING)),
  };
  const mode = editMode(gate);

  // K1: the rows are keyed as the changes were staged while those are on another key, else by the table's key.
  // Both arrays keep their identity while their key does (the target's while it is read; the staging's across its
  // actions), so the result, and the overlay's cache that keys on it, are made again only when the key changes.
  const keyColumns = keyChanged ? staged.keyColumns : (tableKey ?? staged?.keyColumns ?? NO_KEY);
  const result = useMemo(() => resultColumns(columns, keyColumns), [columns, keyColumns]);
  // K8: bound whenever the result has the key (17–21 ms per 100,000 rows once a result; Load more keys only its page).
  const bindingRef = useRef<Binding | null>(null);
  const binding = useMemo(() => {
    if (!result.keyIndexes) return null;
    bindingRef.current = bindRows(rows, result.keyIndexes, bindingRef.current);
    return bindingRef.current;
  }, [rows, result]);
  const cacheRef = useRef<OverlayCache | null>(null);
  const display = useMemo(() => {
    const drawnNow = overlay({ rows, columns, kinds, staged: drawn, binding, result }, cacheRef.current);
    cacheRef.current = drawnNow.cache;
    return drawnNow.overlay;
  }, [rows, columns, kinds, drawn, binding, result]);
  const repeated = binding?.repeated ?? null;
  const names = useMemo(() => columns.map((column) => column.name), [columns]);
  const writable = (name: string) => !!targetColumns.get(name)?.writable;

  const resultRowAt = useCallback(
    (index: number): unknown[] | null => (index < display.leading ? null : (rows[index - display.leading] ?? null)),
    [display, rows],
  );
  /** The grid as staging knows it, read when an action runs (the store's staging is the newest then). */
  const lookup: GridLookup = {
    refAt: (row) => rowRefAt(display, drawn, rows, row, result),
    stateOf: (ref) => rowStateOf(ref, readStagedEdits(tab.id), repeated),
    columnAt: (col) => {
      const name = names[col];
      return name === undefined ? null : { name, column: targetColumns.get(name) };
    },
    loadedAt: (row, col) => resultRowAt(row)?.[col],
  };

  const selection = useRef<GridSelectionState | null>(null);
  const onSelectionChange = useCallback((state: GridSelectionState | null) => {
    selection.current = state;
  }, []);

  /**
   * The gate and the table's key as last committed. An editor that ends as it unmounts runs the callbacks of its last
   * render, before a commit's lock reached it: what it stages is judged by these, never by that render's (a text staged
   * into a change the running commit plans would be taken away with it).
   */
  const committed = useRef({ gate, tableKey });
  /**
   * Stages `action`, or says why it cannot; true unless it was refused. `typed`: what an editor that ended held (H12);
   * a refusal then offers to copy it (K20).
   */
  const stageAction = (action: StageAction, typed?: string): boolean => {
    const { gate: gateNow, tableKey: keyNow } = committed.current;
    const adding = adds(readStagedEdits(tab.id) ?? NOTHING_STAGED, action);
    const refusal = stageRefusal(gateNow, adding, typed !== undefined);
    const why = refusal ?? dispatchRefusal(dispatchStaged(tab.id, action, keyNow ?? undefined));
    if (why === null) return true;
    if (typed === undefined) toast(refusal ? "info" : "warning", why);
    else toast("warning", typedNotStaged(why), { label: COPY_TEXT, run: () => copyTyped(typed) });
    return false;
  };
  /** Refuses (and says why) an action that would stage something new now. */
  const refusedToAdd = (): boolean => {
    const refusal = stageRefusal(committed.current.gate, true);
    if (refusal) toast("info", refusal);
    return refusal !== null;
  };

  const [editing, setEditing] = useState<Editing | null>(null);
  /** The edit as last committed: an editor that ends after another edit replaced it acts on its own cell only. */
  const editingNow = useRef<Editing | null>(null);
  const editingPos = editing ? displayIndexOf(display, drawn, binding, editing.ref) : -1;
  const editingCol = editing ? (result.byName.get(editing.column) ?? -1) : -1;
  const editorShown = mode === "edit" && editing !== null && editingPos >= 0 && editingCol >= 0;
  useLayoutEffect(() => {
    committed.current = { gate, tableKey };
    editingNow.current = editing;
    // The edited row left the rows shown (a re-run, a sort) or editing stopped (held rows, a commit): the editor goes,
    // and what was typed in it is staged as it unmounts (H12).
    if (editing && !editorShown) setEditing(null);
  });
  // New rows came or went on top of the same loaded rows (Add row, its undo, a revert, a delete or Discard of a new
  // row): the selection stays on its rows, never on the row that took its place (K14b for a new row that went).
  const newIds = useMemo(() => (drawn?.inserts ?? []).map((insert) => insert.id).reverse(), [drawn?.inserts]);
  const shownTop = useRef<ShownTop & { rows: unknown[][] }>({ rows, leading: display.leading, newIds });
  useLayoutEffect(() => {
    const before = shownTop.current;
    const after = { rows, leading: display.leading, newIds };
    shownTop.current = after;
    if (before.leading === after.leading || !sameLoadedRows(before.rows, rows)) return;
    const grid = gridRef.current;
    const where = grid?.view();
    if (grid && where) grid.restore(placeAfterTop(where, before, after, ROW_HEIGHT));
  });
  // The edited cell is the active one, and in view (Return, Tab, Add row).
  useLayoutEffect(() => {
    if (editorShown) gridRef.current?.selectCell(editingPos, editingCol);
  }, [editorShown, editing, editingPos, editingCol, gridRef]);

  /** Opens the editor on a cell, or says why it cannot be edited; whether it opened. */
  const beginEdit = (pos: CellPos): boolean => {
    const ref = lookup.refAt(pos.row);
    const name = names[pos.col];
    if (!ref || name === undefined) return false;
    const now = readStagedEdits(tab.id);
    const loaded = ref.kind === "row" ? resultRowAt(pos.row)?.[pos.col] : undefined;
    const start = startEdit(gate, rowStateOf(ref, now, repeated), {
      column: targetColumns.get(name),
      name,
      staged: stagedInputOf(now, ref, name),
      original: loaded,
    });
    if ("refuse" in start) {
      toast("info", start.refuse);
      return false;
    }
    const was = ref.kind === "row" ? wasText(wasValueOf(now, ref, name, loaded), columns[pos.col].category) : null;
    setEditing({ ...start, ref, was });
    return true;
  };

  /** An editor ended: stage what was typed (H4), then close, move along the row (Tab), or open the preview (⌘S). */
  const endEdit = (current: Editing, text: string | null, how: EditEnd) => {
    const value = editorResult(text ?? "", text !== null);
    if (value && text !== null) {
      const cell = { row: current.ref, column: current.column, original: current.original };
      stageAction({ type: "edit", cell, input: value, kind: current.kind }, text);
    }
    if (editingNow.current !== current) return; // another edit replaced it, or it was closed, meanwhile
    if (how === "next" || how === "previous") {
      const row = displayIndexOf(display, drawn, binding, current.ref);
      const from = result.byName.get(current.column) ?? -1;
      const col = from < 0 ? -1 : nextWritable(names, writable, from, how === "next" ? 1 : -1);
      if (row >= 0 && col >= 0 && beginEdit({ row, col })) return;
    }
    setEditing(null);
    if (how !== "blur") gridRef.current?.focus();
    if (how === "save") input.onRequestCommit?.(); // it reads the staged edits from the store: the value is in them
  };
  const cancelEdit = (current: Editing) => {
    if (editingNow.current !== current) return;
    setEditing(null);
    gridRef.current?.focus();
  };
  /** ⌥⌘N or Set NULL in an editor: refused on a NOT NULL column, the editor keeping what was typed (K16). */
  const setNullInEditor = (current: Editing): SetNullAnswer => {
    if (!current.nullable) {
      toast("info", notNullColumn(current.column));
      return false;
    }
    const cell = { row: current.ref, column: current.column, original: current.original };
    if (!stageAction({ type: "setNull", cells: [cell] })) return false; // said why; the text stays
    if (editingNow.current === current) {
      setEditing(null);
      gridRef.current?.focus();
    }
    return true;
  };
  const flushEditor = () => {
    if (!editingNow.current) return false;
    // The editor unmounts at once, and stages what was typed as it does (its effects run before flushSync returns).
    flushSync(() => setEditing(null));
    return true;
  };

  const setNullIn = (rect: Rect | null) => {
    if (refusedToAdd()) return;
    const picked = pickCells(rect, lookup, { writable: true, nullable: true });
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.cells.length) stageAction({ type: "setNull", cells: picked.cells });
    else if (picked.refusal && !picked.notNull) toast("info", picked.refusal);
    if (picked.notNull) toast("info", notNullSkipped(picked.notNull));
  };
  const useDefaultIn = (rect: Rect | null) => {
    if (refusedToAdd()) return;
    const picked = pickCells(rect, lookup, { writable: true, nullable: false });
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.cells.length) stageAction({ type: "useDefault", cells: picked.cells });
  };
  const revertCellsIn = (rect: Rect | null) => {
    const picked = pickCells(rect, lookup, { writable: false, nullable: false });
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.cells.length) stageAction({ type: "revertCells", cells: picked.cells });
  };
  /** Delete rows, or Restore rows when they are all marked (that one is a revert: allowed when nothing new is). */
  const deleteIn = (rect: Rect | null) => {
    const picked = pickRows(rect, lookup);
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.rows.length) stageAction({ type: "deleteRows", rows: picked.rows });
    else if (picked.refusal) toast("info", picked.refusal);
  };
  /** The palette's Delete selected rows and Set NULL: over the grid's selection, which they need (said when none). */
  const overSelection = (act: (rect: Rect) => void) => () => {
    const rect = selection.current?.rect;
    if (rect) act(rect);
    else toast("info", NOTHING_SELECTED);
  };
  const revertRowsIn = (rect: Rect | null) => {
    const picked = pickRows(rect, lookup);
    if (!picked) return toast("warning", TOO_MANY_ROWS);
    if (picked.rows.length) stageAction({ type: "revertRows", rows: picked.rows });
  };
  /** Keep mine: the row's staged values are checked against the server's values now (its loaded row). */
  const keepMineAt = (pos: CellPos) => {
    const ref = lookup.refAt(pos.row);
    const row = resultRowAt(pos.row);
    if (ref?.kind !== "row" || !row) return;
    const current = Object.fromEntries(names.map((name, i) => [name, row[i]]));
    stageAction({ type: "keepMine", row: ref, current, kinds });
  };
  /** + Row: a new row at the top, its first writable column's editor open (spec §3.6.3, H3). */
  const addRow = () => {
    if (refusedToAdd()) return;
    const id = newChangeId("i");
    if (!stageAction({ type: "addRow", id })) return;
    const col = nextWritable(names, writable, -1, 1);
    const name = names[col];
    const column = name === undefined ? undefined : targetColumns.get(name);
    if (!column) return;
    const start = editStart({ column, name: column.name, row: "new", staged: undefined, original: undefined });
    if (!("refuse" in start)) setEditing({ ...start, ref: { kind: "insert", id }, was: null });
  };

  const menu: EditItems = (pos, cell, rect) => {
    if (mode === "locked") return [];
    const rowsPicked = pickRows(rect, lookup);
    const now = readStagedEdits(tab.id);
    const columnsOf = rect ? names.slice(rect.left, rect.right + 1) : [];
    const ref = cell && pos ? lookup.refAt(pos.row) : null;
    const name = pos ? names[pos.col] : undefined;
    let at: EditMenuInput["at"] = null;
    if (pos && ref && name !== undefined) {
      const state = rowStateOf(ref, now, repeated);
      const start = startEdit(gate, state, {
        column: targetColumns.get(name),
        name,
        staged: stagedInputOf(now, ref, name),
        original: resultRowAt(pos.row)?.[pos.col],
      });
      at = {
        refusal: "refuse" in start ? start.refuse : null,
        isNew: state === "new",
        conflict: rowHasConflict(display.marks[pos.row]),
      };
    }
    const entries = editMenu({
      mode,
      changes,
      at,
      selection: rowsPicked && selectionFacts(rowsPicked.rows, columnsOf, now),
    });
    const run: Record<EditMenuId, () => void> = {
      edit: () => {
        if (pos) beginEdit(pos);
      },
      setNull: () => setNullIn(rect),
      useDefault: () => useDefaultIn(rect),
      revertCell: () => revertCellsIn(rect),
      keepMine: () => {
        if (pos) keepMineAt(pos);
      },
      revertRow: () => revertRowsIn(rect),
      deleteRows: () => deleteIn(rect),
      restoreRows: () => deleteIn(rect),
      addRow,
    };
    return entries.map((entry) => menuItem(entry, run[entry.id]));
  };

  const onKey: GridEditing["onKey"] = (e, current, rect) => {
    const action = gridKeyAction(gridEditKey(e.nativeEvent), mode);
    if (!action) return false;
    e.preventDefault();
    if (action === "edit") {
      if (current) beginEdit(current.focus);
    } else if (action === "setNull") setNullIn(rect);
    else if (action === "deleteRows") deleteIn(rect);
    else stageAction({ type: action });
    return true;
  };

  const headerMarks = useMemo<readonly (HeaderMark | undefined)[]>(() => {
    if (!target?.editable) return NO_HEADER_MARKS;
    const key = new Set(target.key ?? []);
    return names.map((name) => {
      const meta = targetColumns.get(name);
      const readOnly = meta && !meta.writable ? readOnlyReasonOf(meta) : null;
      return key.has(name) || readOnly ? { key: key.has(name), readOnly } : undefined;
    });
  }, [target, names, targetColumns]);

  const renderEditor = (current: Editing, meta: ColumnMeta, width: number) => {
    const key = cellKey(current.ref, current.column);
    const onDone = (text: string | null, how: EditEnd) => endEdit(current, text, how);
    const onCancel = () => cancelEdit(current);
    const onSetNull = () => setNullInEditor(current);
    return current.mode === "inline" ? (
      <CellEditor
        key={key}
        initialText={current.initialText}
        placeholder={current.placeholder}
        width={width}
        numeric={meta.category === "number"}
        onDone={onDone}
        onCancel={onCancel}
        onSetNull={onSetNull}
      />
    ) : (
      <ValueEditor
        key={key}
        column={meta}
        kind={current.kind}
        initialText={current.initialText}
        placeholder={current.placeholder}
        was={current.was}
        nullable={current.nullable}
        onDone={onDone}
        onCancel={onCancel}
        onSetNull={onSetNull}
      />
    );
  };
  const editor: GridEditing["editor"] =
    editorShown && editing
      ? {
          pos: { row: editingPos, col: editingCol },
          mode: editing.mode,
          render: (width) => renderEditor(editing, columns[editingCol], width),
        }
      : null;

  const grid: GridEditing | undefined =
    tableEditable || changes > 0 || display.marks.length > 0
      ? {
          editable: mode === "edit",
          marks: display.marks,
          leading: display.leading,
          headerMarks,
          onEdit: (pos) => void beginEdit(pos),
          onKey,
          menu,
          editor,
        }
      : undefined;

  const locate = (changeId: string, column: string | null): CellPos | null => {
    const found = staged ? findChange(staged, changeId) : null;
    if (!found) return null;
    const ref: RowRef =
      found.kind === "insert"
        ? { kind: "insert", id: found.change.id }
        : { kind: "row", rowKey: found.rowKey, key: found.change.key };
    const row = displayIndexOf(display, drawn, binding, ref);
    if (row < 0) return null;
    const name = column ?? (found.kind === "update" ? Object.keys(found.change.set)[0] : undefined);
    return { row, col: (name === undefined ? undefined : result.byName.get(name)) ?? 0 };
  };

  return {
    target,
    targetError,
    targetUnlock,
    checking: view === "checking",
    refetchTarget,
    editable: mode === "edit",
    tableEditable,
    paused: gate.locked ?? gate.waiting,
    readOnlyReason,
    keyChanged,
    staged,
    changes,
    notKept: notKeptHint(savedEdits(staged)),
    display,
    grid,
    onSelectionChange,
    resultRowAt,
    locate,
    addRow,
    deleteSelected: overSelection(deleteIn),
    setNullSelected: overSelection(setNullIn),
    discardAll: () => void stageAction({ type: "discardAll" }),
    undo: () => void stageAction({ type: "undo" }),
    redo: () => void stageAction({ type: "redo" }),
    flushEditor,
  };
}
