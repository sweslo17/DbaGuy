import { useQuery } from "@tanstack/react-query";
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { flushSync } from "react-dom";
import type { MenuItem } from "@/components/ui/ContextMenu";
import { toast } from "@/components/ui/Toast";
import type { ColumnMeta, EditColumn, EditKind, EditTarget } from "@/lib/api/types";
import { sameElements } from "@/lib/arrays";
import { adds, dispatchStaged, readStagedEdits, useStagedEdits } from "@/stores/stagedEdits";
import type { TableTabState } from "@/stores/workspace";
import { isPasswordRequired } from "../connections/UnlockDialog";
import type { DataGridHandle, GridSelectionState } from "../grid/DataGrid";
import type { GridEditing } from "../grid/EditorSlot";
import type { EditItems } from "../grid/GridMenu";
import type { HeaderMark } from "../grid/marks";
import type { CellPos, Rect } from "../grid/selection";
import { CellEditor, type EditEnd } from "./CellEditor";
import { editorResult, editStart, nextWritable, type EditStart, type SetNullAnswer } from "./editing";
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
  EDIT_MENU_ITEMS,
  editMenu,
  editMode,
  gridKeyAction,
  KEY_CHANGED,
  NOT_EDITABLE,
  notKeptHint,
  notNullColumn,
  notNullSkipped,
  pickCells,
  pickRows,
  rowHasConflict,
  ROWS_LOADING,
  rowStateOf,
  selectionFacts,
  stagedInputOf,
  stageRefusal,
  startEdit,
  targetView,
  TOO_MANY_CHANGES,
  TOO_MANY_ROWS,
  wasValueOf,
  type EditGate,
  type EditMenuEntry,
  type EditMenuId,
  type EditMenuInput,
  type GridLookup,
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
  /** Why the staged changes are not kept across a restart (`savedEdits`, K5), or null. */
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
  /** Ends an open editor now, staging what was typed (Task 11: a tab closing asks about it too, L8). */
  flushEditor: () => void;
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

export function useTableEdits(input: TableEditsInput): TableEdits {
  const { tab, rowEditing, sessionOpen, stale, locked, gridRef } = input;
  const columns = input.columns ?? NO_COLUMNS;
  const rows = input.rows ?? EMPTY_ROWS;
  const staged = useStagedEdits(tab.id);
  /** What the grid draws: the store's staging, or Task 10's afterglow of a commit. */
  const drawn = input.afterglow ?? staged;

  // The edit target (`editTargetQuery`, the read `useTableData` makes when the tab is first shown: one read for both),
  // read again on Refresh (DDL since). A read that wants the password before the session opened is the session's to
  // ask for; one that still wants it after asks itself (L13), counted by its failures since the session opened.
  const query = useQuery({ ...editTargetQuery(tab.profileId, tab.path), enabled: rowEditing && sessionOpen });
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
  const target = view === "read" ? (query.data ?? null) : null;
  const targetError = view === "error" ? editsErrorMessage(query.error) : null;
  const targetColumns = useMemo(
    () => (target ? new Map((target.columns ?? []).map((column) => [column.name, column])) : NO_TARGET_COLUMNS),
    [target],
  );
  const kinds = useMemo<ReadonlyMap<string, EditKind>>(
    () => (target ? new Map((target.columns ?? []).map((column) => [column.name, column.kind])) : NO_KINDS),
    [target],
  );

  const tableKey = target?.editable ? (target.key ?? null) : null;
  const changes = staged ? changeCount(staged) : 0;
  const keyChanged = !!staged && !!tableKey && changes > 0 && !sameElements(staged.keyColumns, tableKey);
  const tableEditable = !!target?.editable && !keyChanged;
  const readOnlyReason = target && !target.editable ? (target.reason ?? NOT_EDITABLE) : null;
  const gate: EditGate = {
    locked,
    waiting: stale || !input.columns || input.afterglow ? ROWS_LOADING : null,
    readOnly: target
      ? (readOnlyReason ?? (keyChanged ? KEY_CHANGED : null))
      : (targetError ?? (view === "none" ? NOT_EDITABLE : CHECKING)),
  };
  const mode = editMode(gate);

  // K1: the rows are keyed as the changes were staged while those are on another key, else by the table's key.
  const keyColumns = keyChanged ? staged.keyColumns : (tableKey ?? staged?.keyColumns ?? null);
  const keySignature = keyColumns ? JSON.stringify(keyColumns) : null;
  const result = useMemo(
    () => resultColumns(columns, keySignature ? (JSON.parse(keySignature) as string[]) : []),
    [columns, keySignature],
  );
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

  /** Stages `action`, or says why it cannot; true unless it was refused. `endingEdit`: an editor's text (H12). */
  const stageAction = (action: StageAction, endingEdit = false): boolean => {
    const refusal = stageRefusal(gate, adds(readStagedEdits(tab.id) ?? NOTHING_STAGED, action), endingEdit);
    if (refusal) {
      toast("info", refusal);
      return false;
    }
    const outcome = dispatchStaged(tab.id, action, tableKey ?? undefined);
    if (outcome === "too_many") toast("warning", TOO_MANY_CHANGES);
    else if (outcome === "key_changed") toast("warning", KEY_CHANGED);
    return outcome === "staged" || outcome === "unchanged";
  };
  /** Refuses (and says why) an action that would stage something new now. */
  const refusedToAdd = (): boolean => {
    const refusal = stageRefusal(gate, true);
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
    editingNow.current = editing;
    // The edited row left the rows shown (a re-run, a sort) or editing stopped (held rows, a commit): the editor goes,
    // and what was typed in it is staged as it unmounts (H12).
    if (editing && !editorShown) setEditing(null);
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
    if (value) {
      const cell = { row: current.ref, column: current.column, original: current.original };
      stageAction({ type: "edit", cell, input: value }, true);
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
    // The editor unmounts at once, and stages what was typed as it does (its effects run before flushSync returns).
    if (editingNow.current) flushSync(() => setEditing(null));
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
    stageAction({ type: "keepMine", row: ref, current: Object.fromEntries(names.map((name, i) => [name, row[i]])) });
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
      const readOnly = meta && !meta.writable ? (meta.read_only_reason ?? "it cannot be written") : null;
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
    targetUnlock: view === "unlock" ? query.error : null,
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
    deleteSelected: () => deleteIn(selection.current?.rect ?? null),
    setNullSelected: () => setNullIn(selection.current?.rect ?? null),
    discardAll: () => void stageAction({ type: "discardAll" }),
    undo: () => void stageAction({ type: "undo" }),
    redo: () => void stageAction({ type: "redo" }),
    flushEditor,
  };
}
