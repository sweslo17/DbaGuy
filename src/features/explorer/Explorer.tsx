import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ContextMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { ObjectNode, ProfileEntry } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { ENGINE_LABELS } from "@/lib/engines";
import { useWorkspace } from "@/stores/workspace";
import { ConnectionDialog } from "../connections/ConnectionDialog";
import { findEngineInfo, isReadOnlyProfile, useDeleteProfile, useEngines, useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { secretsNeeded } from "../connections/UnlockDialog";
import { joinUnlockPrompt } from "../connections/unlockPrompts";
import { openDumpDialog, openRunDialog } from "../data-tools/dataDialogs";
import { dumpEntryForNode, runEntryForNode, type RunEntry } from "../data-tools/entryPoints";
import { runningJobs, syncJobs, useJobs } from "../data-tools/jobsStore";
import { READ_ONLY_RUN } from "../data-tools/runForm";
import { askOpenDocument } from "../documents/openDocument";
import { applyTreeContext } from "../query/queryContext";
import { requestCloseTab, requestDisconnectTab } from "../workspace/closeGuards";
import { openBrowseTab } from "./browse";
import { forgetExplorer } from "./explorerCache";
import { keyspaceOfPath } from "./fieldSamples";
import { folderLabel, kindIcon } from "./kinds";
import { SampleFieldsDialog } from "./SampleFieldsDialog";
import { nodeKeyPrefix, profileKey, useTreeRows, type Expansion, type TreeRow } from "./useTreeRows";

const ROW_HEIGHT = 22;
/** Nodes that group other nodes: double-click expands them instead of opening a detail tab. */
const CONTAINER_KINDS = new Set(["folder", "database", "schema", "bucket", "scope"]);
/** A Couchbase collection holds documents: double-click browses them (ruling B19); its detail is in its menu. */
const BROWSABLE_KIND = "collection";
/**
 * A click on a node sets the active tab's context only this long after it, the double-click interval: a double-click
 * on a collection opens a browse tab and must not retarget the tab that was open.
 */
const CONTEXT_CLICK_DELAY_MS = 250;

type ErrorRow = Extract<TreeRow, { type: "error" }>;

/** A read-only connection's Run SQL file… is greyed with why: the backend refuses it before anything is sent (06a). */

/** Run SQL file… for a node (or the connection's row): greyed on a read-only connection. */
const runItem = (entry: RunEntry, readOnly: boolean): MenuItem => ({
  label: "Run SQL file…",
  onSelect: () => openRunDialog(entry),
  ...(readOnly ? { disabled: true, title: READ_ONLY_RUN } : {}),
});

const isExpandable = (row: TreeRow) =>
  row.type === "folder" || row.type === "profile" || (row.type === "node" && row.node.has_children);

/** The connection a row belongs to (a folder belongs to none). */
function rowProfileId(row: TreeRow): string | null {
  if (row.type === "profile") return row.entry.profile.id;
  if (row.type === "node" || row.type === "error") return row.profileId;
  return null;
}

function Chevron({ show, open, onToggle }: { show: boolean; open: boolean; onToggle: () => void }) {
  if (!show) return <span className="w-4 shrink-0" />;
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

export function Explorer() {
  const qc = useQueryClient();
  const profiles = useProfiles();
  const engines = useEngines().data;
  const deleteProfile = useDeleteProfile();
  const openObject = useWorkspace((s) => s.openObject);
  const openQuery = useWorkspace((s) => s.openQuery);
  const selectProfile = useWorkspace((s) => s.selectProfile);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // Which {profile, path} each expandable row loads its children from; filled the first time a row is expanded.
  const [expansions] = useState(() => new Map<string, Expansion>());
  const [selected, setSelected] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ open: boolean; entry?: ProfileEntry }>({ open: false });
  const [toDelete, setToDelete] = useState<ProfileEntry | null>(null);
  const [sampling, setSampling] = useState<{ profileId: string; node: ObjectNode } | null>(null);
  const [releasing, setReleasing] = useState(false);
  // The failed rows that have asked for their profile's password (`joinUnlockPrompt`) and how to stop asking.
  const asking = useRef(new Map<string, () => void>());
  const entries = profiles.data ?? [];
  const rows = useTreeRows(entries, expanded, expansions);
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 20,
  });

  /** Selects a row; its connection becomes the workspace's selected profile (the default of a new query tab). */
  const selectRow = (row: TreeRow) => {
    setSelected(row.key);
    const profileId = rowProfileId(row);
    if (profileId) selectProfile(profileId);
  };

  // The one context change a click is waiting to make (see `clickNode`); gone with the Explorer.
  const pendingContext = useRef<number | null>(null);
  const cancelPendingContext = useCallback(() => {
    if (pendingContext.current !== null) window.clearTimeout(pendingContext.current);
    pendingContext.current = null;
  }, []);
  useEffect(() => cancelPendingContext, [cancelPendingContext]);

  /** `selectRow` for a pointer click: a click that follows another one takes the place of its pending context change. */
  const selectByClick = (row: TreeRow) => {
    cancelPendingContext();
    selectRow(row);
  };

  /**
   * A pointer click on a node: selects it and, for a connection whose engine has a query context (Couchbase), sets the
   * context of the active query tab on it (spec §3.11.1, ruling B48), `CONTEXT_CLICK_DELAY_MS` later: a double-click
   * (`activate` on a collection) cancels it, and so does the next click. Only a click does: walking the tree with the
   * keyboard (`selectRow` alone) never retargets the open tab.
   */
  const clickNode = (row: Extract<TreeRow, { type: "node" }>) => {
    selectByClick(row);
    if (!findEngineInfo(entries, engines, row.profileId)?.query_context) return;
    pendingContext.current = window.setTimeout(() => {
      pendingContext.current = null;
      applyTreeContext(row.profileId, row.node.path);
    }, CONTEXT_CLICK_DELAY_MS);
  };

  const toggle = useCallback(
    (row: TreeRow, force?: boolean) => {
      if (row.type === "profile") expansions.set(row.key, { profileId: row.entry.profile.id, path: [] });
      if (row.type === "node") expansions.set(row.key, { profileId: row.profileId, path: row.node.path });
      setExpanded((prev) => {
        const next = new Set(prev);
        const open = force ?? !next.has(row.key);
        if (open) next.add(row.key);
        else next.delete(row.key);
        return next;
      });
    },
    [expansions],
  );

  const activate = (row: TreeRow) => {
    if (row.type === "node" && row.node.kind === BROWSABLE_KIND) {
      cancelPendingContext(); // the browse tab has its own context: the open tab keeps its
      void openBrowseTab(row.profileId, row.node.path);
    } else if (row.type === "node" && !CONTAINER_KINDS.has(row.node.kind)) {
      openObject(row.profileId, row.node);
    } else if (isExpandable(row)) {
      toggle(row);
    }
  };

  // A children request failed with password_required: ask once per failure, in the profile's one unlock dialog. What
  // failed is read again when the password is given (`useUnlockProfile`); a cancelled dialog leaves the row's Retry.
  useEffect(() => {
    const failing = rows.filter((r): r is ErrorRow => r.type === "error" && r.code === "password_required");
    const failingKeys = new Set(failing.map((r) => r.key));
    for (const [key, stopAsking] of asking.current) {
      if (failingKeys.has(key)) continue;
      stopAsking();
      asking.current.delete(key);
    }
    for (const row of failing) {
      const entry = entries.find((e) => e.profile.id === row.profileId);
      if (!entry || asking.current.has(row.key)) continue;
      const request = { profile: entry.profile, needs: secretsNeeded(row.detail) };
      asking.current.set(row.key, joinUnlockPrompt(request, {}));
    }
  }, [rows, entries]);
  // Rows that go with the tree stop asking.
  useEffect(() => {
    const own = asking.current;
    return () => {
      for (const stopAsking of own.values()) stopAsking();
      own.clear();
    };
  }, []);

  /** Drops everything the tree and the workspace hold for a deleted profile. */
  const forgetProfile = (profileId: string) => {
    const own = profileKey(profileId);
    const prefix = nodeKeyPrefix(profileId);
    const owned = (key: string) => key === own || key.startsWith(prefix);
    for (const key of [...expansions.keys()]) if (owned(key)) expansions.delete(key);
    setExpanded((prev) => new Set([...prev].filter((key) => !owned(key))));
    setSelected((prev) => (prev && owned(prev) ? null : prev));
    if (useWorkspace.getState().selectedProfileId === profileId) selectProfile(null);
    forgetExplorer(qc, profileId);
    // Their tabs agreed already (releaseTabs): table and object tabs close; query tabs keep their text and lose only the
    // connection.
    const { tabs, closeTab, updateTab } = useWorkspace.getState();
    for (const tab of tabs.filter((t) => t.profileId === profileId)) {
      if (tab.kind === "query") updateTab(tab.id, "query", { profileId: null, database: null, queryContext: undefined });
      else closeTab(tab.id);
    }
  };

  /**
   * Before a profile is deleted, each of its tabs is asked in turn (a running export, a run, an open transaction):
   * table and object tabs close, query tabs agree to lose their session. Any "no" keeps the profile. First, a
   * data-tools job running on it keeps it, before any tab is asked: the delete closes every session of the profile,
   * the jobs' too (B13). The backend's list is read first: a job wrongly guessed lost (`not_listed`) that it has
   * running counts again, and one it does not have never blocks the delete.
   */
  const releaseTabs = async (profileId: string): Promise<boolean> => {
    await syncJobs(); // a list that fails is logged, and the jobs stay as this window knows them
    const jobs = runningJobs(useJobs.getState()).filter((job) => job.profileId === profileId).length;
    if (jobs > 0) {
      toast("warning", `Profile not deleted — ${jobs === 1 ? "a job is" : `${jobs} jobs are`} still running on it`);
      return false;
    }
    for (const tab of useWorkspace.getState().tabs.filter((t) => t.profileId === profileId)) {
      const agreed = tab.kind === "query" ? await requestDisconnectTab(tab.id) : await requestCloseTab(tab.id);
      if (!agreed && useWorkspace.getState().tabs.some((t) => t.id === tab.id)) {
        toast("warning", `Profile not deleted — ${tab.title} is still open`);
        return false;
      }
    }
    return true;
  };

  const deleteEntry = async (entry: ProfileEntry) => {
    setReleasing(true);
    try {
      if (!(await releaseTabs(entry.profile.id))) return;
    } finally {
      setReleasing(false);
    }
    deleteProfile.mutate(entry.profile.id, {
      onSuccess: () => {
        forgetProfile(entry.profile.id);
        setToDelete(null);
      },
    });
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const index = rows.findIndex((r) => r.key === selected);
    const row = rows[index] as TreeRow | undefined;
    const select = (i: number) => {
      const clamped = Math.max(0, Math.min(rows.length - 1, i));
      const target = rows[clamped];
      if (!target) return;
      selectRow(target);
      virtualizer.scrollToIndex(clamped);
    };
    if (e.key === "ArrowDown") {
      e.preventDefault();
      select(index + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      select(index - 1);
    } else if (e.key === "ArrowRight" && row) {
      e.preventDefault();
      if (!isExpandable(row)) return;
      if (expanded.has(row.key)) select(index + 1);
      else toggle(row, true);
    } else if (e.key === "ArrowLeft" && row) {
      e.preventDefault();
      if (expanded.has(row.key)) toggle(row, false);
      else {
        for (let i = index - 1; i >= 0; i--) {
          if (rows[i].depth < row.depth) {
            select(i);
            break;
          }
        }
      }
    } else if (e.key === "Enter" && row) {
      e.preventDefault();
      activate(row);
    } else if (e.key === "n" && e.metaKey) {
      e.preventDefault();
      setDialog({ open: true });
    }
  };

  /** A Couchbase collection's items: its documents, its detail and its fields. */
  const collectionItems = (row: Extract<TreeRow, { type: "node" }>, opensDocuments: boolean): MenuItem[] => {
    // The dialog asks for the document's ID in this collection: to open it, or to create it (never on a read-only one).
    const askDocument = (mode: "open" | "new") =>
      askOpenDocument({ profileId: row.profileId, mode, keyspace: keyspaceOfPath(row.node.path) ?? {} });
    return [
      { label: "Browse documents", onSelect: () => void openBrowseTab(row.profileId, row.node.path) },
      ...(opensDocuments ? [{ label: "Open document by ID…", onSelect: () => askDocument("open") }] : []),
      ...(opensDocuments && !isReadOnlyProfile(entries, row.profileId)
        ? [{ label: "New document…", onSelect: () => askDocument("new") }]
        : []),
      { label: "Details", onSelect: () => openObject(row.profileId, row.node) },
      {
        label: "Sample fields…",
        title: "Read some of its documents and list their fields (completion offers them)",
        disabled: keyspaceOfPath(row.node.path) === null,
        onSelect: () => setSampling({ profileId: row.profileId, node: row.node }),
      },
    ];
  };

  const renderRow = (row: TreeRow): ReactNode => {
    const pad = { paddingLeft: 8 + row.depth * 14 };
    const open = expanded.has(row.key);
    const chevron = <Chevron show={isExpandable(row)} open={open} onToggle={() => toggle(row)} />;
    const base = cn("flex h-[22px] items-center gap-1.5 overflow-hidden pr-2 text-[13px]", selected === row.key ? "bg-selected" : "hover:bg-hover");
    const aria = {
      role: "treeitem",
      "aria-level": row.depth + 1,
      "aria-selected": selected === row.key,
      "aria-expanded": isExpandable(row) ? open : undefined,
    } as const;
    switch (row.type) {
      case "folder": {
        const Icon = kindIcon("folder");
        return (
          <div {...aria} className={base} style={pad} onClick={() => selectByClick(row)} onDoubleClick={() => toggle(row)}>
            {chevron}
            <Icon size={14} className="shrink-0 text-muted" />
            <span className="truncate">{row.name}</span>
          </div>
        );
      }
      case "profile": {
        const p = row.entry.profile;
        const runEntry = runEntryForNode(p.id, findEngineInfo(entries, engines, p.id), null);
        const content = (
          <div
            {...aria}
            className={base}
            style={pad}
            onClick={() => selectByClick(row)}
            onContextMenu={() => selectByClick(row)}
            onDoubleClick={() => toggle(row)}
          >
            {chevron}
            {/* Its name is written at the right edge, and gives way first in a narrow sidebar: the icon already says it. */}
            <EngineIcon engine={p.engine} decorative />
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: profileColor(p.color) ?? "transparent" }} />
            <span className="truncate font-medium">{p.name}</span>
            <span className="ml-auto min-w-0 shrink-[100] truncate pl-2 text-[11px] text-muted">{ENGINE_LABELS[p.engine]}</span>
          </div>
        );
        return (
          <ContextMenu
            items={[
              ...(runEntry ? [runItem(runEntry, p.read_only)] : []),
              { label: "New Query", onSelect: () => openQuery(p.id, null) },
              { label: "Edit…", onSelect: () => setDialog({ open: true, entry: row.entry }) },
              { label: "Refresh", onSelect: () => void qc.invalidateQueries({ queryKey: ["children", p.id] }) },
              { label: "Delete…", onSelect: () => setToDelete(row.entry), danger: true, separatorBefore: true },
            ]}
          >
            {content}
          </ContextMenu>
        );
      }
      case "node": {
        const Icon = kindIcon(row.node.kind, row.node.name);
        const label = row.node.kind === "folder" ? folderLabel(row.node.name) : row.node.name;
        const content = (
          <div
            {...aria}
            className={cn(base, row.node.system && "opacity-60")}
            style={pad}
            title={row.node.detail ? `${label} — ${row.node.detail}` : label}
            onClick={() => clickNode(row)}
            onDoubleClick={() => activate(row)}
          >
            {chevron}
            <Icon size={14} className="shrink-0 text-muted" />
            <span className="truncate">{label}</span>
            {row.node.detail && <span className="ml-auto shrink-[100] truncate pl-2 text-[11px] text-muted">{row.node.detail}</span>}
          </div>
        );
        const engine = findEngineInfo(entries, engines, row.profileId);
        // The data tools first (spec ⑥ §3.6: what the engine can do decides, D-06-13), then a collection's own items
        const dumpEntry = dumpEntryForNode(row.profileId, engine, row.node);
        const runEntry = runEntryForNode(row.profileId, engine, row.node);
        const dataItems: MenuItem[] = [
          ...(dumpEntry ? [{ label: "Dump…", onSelect: () => openDumpDialog(dumpEntry) }] : []),
          ...(runEntry ? [runItem(runEntry, isReadOnlyProfile(entries, row.profileId))] : []),
        ];
        const items = [...dataItems, ...(row.node.kind === BROWSABLE_KIND ? collectionItems(row, engine?.documents === true) : [])];
        if (items.length === 0) return content;
        return (
          <ContextMenu items={items} onContextMenu={() => selectByClick(row)}>
            {content}
          </ContextMenu>
        );
      }
      case "loading":
        return (
          <div className={base} style={pad}>
            <span className="w-4 shrink-0" />
            <span className="text-muted">Loading…</span>
          </div>
        );
      case "hint":
        return (
          <div className={base} style={pad} title={row.title}>
            <span className="w-4 shrink-0" />
            <span className="truncate text-[12px] text-muted italic">{row.text}</span>
          </div>
        );
      case "error":
        return (
          <div className={base} style={pad} title={row.message}>
            <span className="w-4 shrink-0" />
            <span className="truncate text-danger">{row.message}</span>
            <button className="ml-auto shrink-0 text-[11px] text-accent" onClick={row.retry}>
              Retry
            </button>
          </div>
        );
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border px-2">
        <span className="flex-1 text-[11px] font-semibold uppercase tracking-wide text-muted">Connections</span>
        <Button variant="ghost" size="sm" title="Refresh all" onClick={() => void qc.invalidateQueries({ queryKey: ["children"] })}>
          <RefreshCw size={13} />
        </Button>
        <Button variant="ghost" size="sm" title="New connection (⌘N)" onClick={() => setDialog({ open: true })}>
          <Plus size={14} />
        </Button>
      </div>
      <div ref={parentRef} role="tree" tabIndex={0} onKeyDown={onKeyDown} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden outline-none">
        {profiles.isError && (
          <p className="p-3 text-[12px] text-danger" data-selectable>
            {errorMessage(profiles.error)}
          </p>
        )}
        {profiles.data?.length === 0 && (
          <div className="p-4 text-[12px] text-muted">
            No connections yet.{" "}
            <button className="text-accent" onClick={() => setDialog({ open: true })}>
              Create one
            </button>
          </div>
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
      <ConnectionDialog open={dialog.open} entry={dialog.entry} onOpenChange={(open) => setDialog((d) => ({ ...d, open }))} />
      {sampling && (
        <SampleFieldsDialog
          open
          profileId={sampling.profileId}
          node={sampling.node}
          onOpenChange={(open) => !open && setSampling(null)}
        />
      )}
      {toDelete && (
        <ConfirmDialog
          open
          title="Delete connection"
          pending={deleteProfile.isPending || releasing}
          error={deleteProfile.isError ? errorMessage(deleteProfile.error) : null}
          message={`Delete "${toDelete.profile.name}"? Its saved password is removed from the Keychain.`}
          onOpenChange={(open) => {
            if (open) return;
            deleteProfile.reset();
            setToDelete(null);
          }}
          onConfirm={() => void deleteEntry(toDelete)}
        />
      )}
    </div>
  );
}
