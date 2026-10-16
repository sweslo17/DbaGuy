import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, FilePlus, FileText, Folder, FolderOpen, FolderPlus, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ContextMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { errorMessage } from "@/lib/api/client";
import type { ScriptNode } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import { isWithin, movedPath, parentPath, stemLength } from "@/lib/scriptPaths";
import { useWorkspace } from "@/stores/workspace";
import { useScriptActions, useScriptTree } from "./useScripts";

const ROW_HEIGHT = 22;

/** A name being typed in the tree: a rename in place, or a new file or folder at the top of its folder. */
type Editing = { mode: "rename"; path: string } | { mode: "file" | "folder"; parent: string };

type Row =
  | { type: "node"; key: string; node: ScriptNode; depth: number }
  | { type: "new"; key: string; kind: "file" | "folder"; depth: number };

/** The visible rows: the expanded folders' children below them, and the new-name row where one is being typed. */
function flatten(items: ScriptNode[], expanded: ReadonlySet<string>, editing: Editing | null): Row[] {
  const rows: Row[] = [];
  const walk = (nodes: ScriptNode[], depth: number, parent: string) => {
    if (editing && editing.mode !== "rename" && editing.parent === parent) {
      rows.push({ type: "new", key: "\0new", kind: editing.mode, depth });
    }
    for (const node of nodes) {
      rows.push({ type: "node", key: node.path, node, depth });
      if (node.kind === "folder" && expanded.has(node.path)) walk(node.children, depth + 1, node.path);
    }
  };
  walk(items, 0, "");
  return rows;
}

/** Where a new file or folder goes: into the folder, beside the file, or at the top. */
const folderFor = (node: ScriptNode | null | undefined) =>
  !node ? "" : node.kind === "folder" ? node.path : parentPath(node.path);

/**
 * The name typed for a new or renamed entry. Enter saves it (a failure keeps the field, its reason is a toast),
 * Escape cancels; leaving the field saves a changed name and otherwise cancels. Enter and Escape belong to the input
 * method while it composes.
 */
function NameInput({
  initial,
  placeholder,
  onCommit,
  onCancel,
}: {
  initial: string;
  placeholder: string;
  onCommit: (name: string) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const input = useRef<HTMLInputElement>(null);
  /** Saving, or finished (saved or cancelled): leaving the field then changes nothing. */
  const settled = useRef<"saving" | "done" | null>(null);

  // A tick later: a menu item that started the edit runs while its menu still traps the focus.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const field = input.current;
      if (!field) return;
      field.focus();
      field.setSelectionRange(0, stemLength(initial));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [initial]);

  const commit = async (): Promise<boolean> => {
    if (settled.current) return false;
    settled.current = "saving";
    const ok = await onCommit(value);
    settled.current = ok ? "done" : null;
    return ok;
  };
  const cancel = () => {
    if (settled.current) return;
    settled.current = "done";
    onCancel();
  };

  return (
    <input
      ref={input}
      value={value}
      placeholder={placeholder}
      spellCheck={false}
      autoCorrect="off"
      autoCapitalize="off"
      aria-label={placeholder}
      className="h-[18px] min-w-0 flex-1 rounded-sm border border-accent bg-elevated px-1 text-[13px] outline-none"
      onChange={(e) => setValue(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      // Only the keys handled here stop: ⌘S, ⌘W, ⌘P… still reach the window (the tree ignores keys while a name is typed).
      onKeyDown={(e) => {
        if (isImeKeyEvent(e)) return;
        if (e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          void commit().then((ok) => !ok && input.current?.focus());
        } else if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          cancel();
        }
      }}
      onBlur={() => {
        if (settled.current) return;
        if (!value.trim() || value === initial) return cancel();
        void commit().then((ok) => !ok && cancel());
      }}
    />
  );
}

function Chevron({ folder, open, onToggle }: { folder: boolean; open: boolean; onToggle: () => void }) {
  if (!folder) return <span className="w-4 shrink-0" />;
  return (
    <span
      className="flex h-full w-4 shrink-0 items-center justify-center"
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      onDoubleClick={(e) => e.stopPropagation()}
    >
      <ChevronRight size={12} className={cn("text-muted transition-transform", open && "rotate-90")} />
    </span>
  );
}

/**
 * The sidebar's Scripts view (spec §4.7): the scripts folder as a tree, folders first. Double-click (or Enter) opens a
 * file in a query tab bound to it; the context menu creates, renames and deletes files and folders. F2 renames,
 * ⌘⌫ deletes.
 */
export function ScriptsPanel() {
  const tree = useScriptTree();
  const actions = useScriptActions();
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [toDelete, setToDelete] = useState<ScriptNode | null>(null);
  const [deleting, setDeleting] = useState(false);
  /** A menu item started typing a name: the closing menu must not take the focus back from the field. */
  const keepFocus = useRef(false);
  const treeRef = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => flatten(tree.data?.items ?? [], expanded, editing), [tree.data, expanded, editing]);
  const nodes = useMemo(() => {
    const byPath = new Map<string, ScriptNode>();
    for (const row of rows) if (row.type === "node") byPath.set(row.node.path, row.node);
    return byPath;
  }, [rows]);
  const selectedNode = selected === null ? null : nodes.get(selected);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => treeRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 20,
  });

  const setOpen = (path: string, open?: boolean) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (open ?? !next.has(path)) next.add(path);
      else next.delete(path);
      return next;
    });

  const activate = (node: ScriptNode) => {
    if (node.kind === "folder") setOpen(node.path);
    else void actions.open(node.path);
  };

  const startEditing = (next: Editing, fromMenu: boolean) => {
    keepFocus.current = fromMenu;
    if (next.mode !== "rename" && next.parent) setOpen(next.parent, true);
    setEditing(next);
  };
  const startNew = (kind: "file" | "folder", parent: string, fromMenu = false) =>
    startEditing({ mode: kind, parent }, fromMenu);
  const startRename = (node: ScriptNode, fromMenu = false) => startEditing({ mode: "rename", path: node.path }, fromMenu);

  /** Ends typing a name; the tree gets the keyboard back. */
  const stopEditing = () => {
    setEditing(null);
    treeRef.current?.focus({ preventScroll: true });
  };

  const commitName = async (name: string): Promise<boolean> => {
    if (!editing) return false;
    let path: string | null;
    if (editing.mode === "rename") {
      const node = nodes.get(editing.path);
      if (!node) return false;
      path = await actions.rename(node, name);
      // An open folder stays open under its new name.
      if (path !== null && node.kind === "folder" && path !== node.path) {
        const [from, to] = [node.path, path];
        setExpanded((prev) => new Set([...prev].map((p) => (isWithin(p, from) ? movedPath(p, from, to) : p))));
      }
    } else if (editing.mode === "file") {
      path = await actions.createFile(editing.parent, name);
    } else {
      path = await actions.createFolder(editing.parent, name);
    }
    if (path === null) return false;
    setSelected(path);
    // A new file opened in a tab, which took the focus: leave it there.
    if (editing.mode === "file") setEditing(null);
    else stopEditing();
    return true;
  };

  const confirmDelete = async (node: ScriptNode) => {
    setDeleting(true);
    try {
      if (await actions.remove(node)) {
        setExpanded((prev) => new Set([...prev].filter((p) => !isWithin(p, node.path))));
        if (selected !== null && isWithin(selected, node.path)) setSelected(null);
      }
    } finally {
      setDeleting(false);
      setToDelete(null);
    }
  };

  const menuFor = (node: ScriptNode): MenuItem[] => {
    const folder = folderFor(node);
    return [
      ...(node.kind === "file" ? [{ label: "Open", onSelect: () => void actions.open(node.path) }] : []),
      { label: "New File…", onSelect: () => startNew("file", folder, true), separatorBefore: node.kind === "file" },
      { label: "New Folder…", onSelect: () => startNew("folder", folder, true) },
      { label: "Rename…", shortcut: "F2", onSelect: () => startRename(node, true), separatorBefore: true },
      { label: "Delete…", shortcut: "⌘⌫", danger: true, onSelect: () => setToDelete(node) },
    ];
  };
  const rootMenu: MenuItem[] = [
    { label: "New File…", onSelect: () => startNew("file", "", true) },
    { label: "New Folder…", onSelect: () => startNew("folder", "", true) },
    { label: "Refresh", onSelect: () => void actions.refresh(), separatorBefore: true },
  ];
  const onMenuClosed = (e: Event) => {
    if (!keepFocus.current) return;
    keepFocus.current = false;
    e.preventDefault();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (isImeKeyEvent(e) || editing) return;
    const selectable = rows.filter((row): row is Extract<Row, { type: "node" }> => row.type === "node");
    const index = selectable.findIndex((row) => row.node.path === selected);
    const node = selectable[index]?.node;
    const select = (i: number) => {
      const target = selectable[Math.max(0, Math.min(selectable.length - 1, i))];
      if (!target) return;
      setSelected(target.node.path);
      virtualizer.scrollToIndex(rows.indexOf(target));
    };
    if (e.key === "ArrowDown") {
      e.preventDefault();
      select(index + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      select(index < 0 ? 0 : index - 1);
    } else if (e.key === "ArrowRight" && node?.kind === "folder") {
      e.preventDefault();
      if (expanded.has(node.path)) select(index + 1);
      else setOpen(node.path, true);
    } else if (e.key === "ArrowLeft" && node) {
      e.preventDefault();
      if (node.kind === "folder" && expanded.has(node.path)) setOpen(node.path, false);
      else {
        const parent = parentPath(node.path);
        if (parent) select(selectable.findIndex((row) => row.node.path === parent));
      }
    } else if (e.key === "Enter" && node) {
      e.preventDefault();
      activate(node);
    } else if (e.key === "F2" && node) {
      e.preventDefault();
      startRename(node);
    } else if (e.key === "Backspace" && e.metaKey && node) {
      e.preventDefault();
      setToDelete(node);
    }
  };

  const renderRow = (row: Row): ReactNode => {
    const pad = { paddingLeft: 8 + row.depth * 14 };
    if (row.type === "new") {
      const Icon = row.kind === "folder" ? Folder : FileText;
      return (
        <div className="flex h-[22px] items-center gap-1.5 overflow-hidden pr-2" style={pad}>
          <span className="w-4 shrink-0" />
          <Icon size={14} className="shrink-0 text-muted" />
          <NameInput
            initial=""
            placeholder={row.kind === "folder" ? "Folder name" : "File name (.sql)"}
            onCommit={commitName}
            onCancel={stopEditing}
          />
        </div>
      );
    }
    const { node } = row;
    const folder = node.kind === "folder";
    const open = folder && expanded.has(node.path);
    const renaming = editing?.mode === "rename" && editing.path === node.path;
    const Icon = folder ? (open ? FolderOpen : Folder) : FileText;
    return (
      <ContextMenu items={menuFor(node)} onCloseAutoFocus={onMenuClosed}>
        <div
          role="treeitem"
          aria-level={row.depth + 1}
          aria-selected={selected === node.path}
          aria-expanded={folder ? open : undefined}
          className={cn(
            "flex h-[22px] items-center gap-1.5 overflow-hidden pr-2 text-[13px]",
            selected === node.path ? "bg-selected" : "hover:bg-hover",
          )}
          style={pad}
          title={node.path}
          onClick={() => setSelected(node.path)}
          onContextMenu={() => setSelected(node.path)}
          onDoubleClick={() => activate(node)}
        >
          <Chevron folder={folder} open={open} onToggle={() => setOpen(node.path)} />
          <Icon size={14} className="shrink-0 text-muted" />
          {renaming ? (
            <NameInput initial={node.name} placeholder="New name" onCommit={commitName} onCancel={stopEditing} />
          ) : (
            <span className="truncate">{node.name}</span>
          )}
        </div>
      </ContextMenu>
    );
  };

  const deleteMessage = (node: ScriptNode) => {
    if (node.kind === "folder") return `Delete the folder ${node.path}? Only an empty folder can be deleted.`;
    const open = useWorkspace
      .getState()
      .tabs.some((t) => t.kind === "query" && t.scriptPath !== null && isWithin(t.scriptPath, node.path));
    return `Delete ${node.path} from disk?${open ? " Its open tab keeps the text, no longer saved to a file." : ""}`;
  };

  const root = tree.data?.root;
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border px-2">
        {/* Cut at the start, so the folder's own name stays visible (the mark keeps the leading "/" in place). */}
        <span
          className="min-w-0 flex-1 truncate text-left text-[11px] text-muted [direction:rtl]"
          title={root}
          data-selectable
        >
          {root ? `‎${root}` : "Scripts"}
        </span>
        <Button
          variant="ghost"
          size="sm"
          title="New file"
          aria-label="New file"
          onClick={() => startNew("file", folderFor(selectedNode))}
        >
          <FilePlus size={13} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title="New folder"
          aria-label="New folder"
          onClick={() => startNew("folder", folderFor(selectedNode))}
        >
          <FolderPlus size={13} />
        </Button>
        <Button variant="ghost" size="sm" title="Refresh" aria-label="Refresh" onClick={() => void actions.refresh()}>
          <RefreshCw size={13} />
        </Button>
      </div>
      <ContextMenu items={rootMenu} onCloseAutoFocus={onMenuClosed}>
        <div
          ref={treeRef}
          role="tree"
          aria-label="Scripts"
          tabIndex={0}
          onKeyDown={onKeyDown}
          className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto outline-none"
        >
          {tree.isPending && <p className="p-3 text-[12px] text-muted">Loading…</p>}
          {tree.isError && (
            <div className="p-3 text-[12px]">
              <p className="text-danger" data-selectable>
                {errorMessage(tree.error)}
              </p>
              <button className="mt-1 text-accent" onClick={() => void tree.refetch()}>
                Retry
              </button>
            </div>
          )}
          {tree.data && rows.length === 0 && (
            <p className="p-4 text-[12px] text-muted">
              No scripts yet. Use + above or right-click here to create a file or folder.
            </p>
          )}
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((item) => (
              <div
                key={rows[item.index].key}
                style={{ position: "absolute", top: 0, left: 0, right: 0, transform: `translateY(${item.start}px)` }}
              >
                {renderRow(rows[item.index])}
              </div>
            ))}
          </div>
        </div>
      </ContextMenu>
      {toDelete && (
        <ConfirmDialog
          open
          title={toDelete.kind === "folder" ? "Delete folder" : "Delete script"}
          message={deleteMessage(toDelete)}
          pending={deleting}
          onOpenChange={(open) => !open && !deleting && setToDelete(null)}
          onConfirm={() => void confirmDelete(toDelete)}
        />
      )}
    </div>
  );
}
