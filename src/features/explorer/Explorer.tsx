import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ContextMenu } from "@/components/ui/ContextMenu";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { Profile, ProfileEntry } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { ENGINE_LABELS } from "@/lib/engines";
import { useWorkspace } from "@/stores/workspace";
import { ConnectionDialog } from "../connections/ConnectionDialog";
import { findEngineInfo, useDeleteProfile, useEngines, useProfiles } from "../connections/hooks";
import { profileColor } from "../connections/profileColors";
import { secretsNeeded, UnlockDialog, type SecretsNeeded } from "../connections/UnlockDialog";
import { applyTreeContext } from "../query/queryContext";
import { requestCloseTab, requestDisconnectTab } from "../workspace/closeGuards";
import { openBrowseTab } from "./browse";
import { forgetExplorer } from "./explorerCache";
import { folderLabel, kindIcon } from "./kinds";
import { nodeKeyPrefix, profileKey, useTreeRows, type Expansion, type TreeRow } from "./useTreeRows";

const ROW_HEIGHT = 22;
/** Nodes that group other nodes: double-click expands them instead of opening a detail tab. */
const CONTAINER_KINDS = new Set(["folder", "database", "schema", "bucket", "scope"]);
/** A Couchbase collection holds documents: double-click browses them (ruling B19); its detail is in its menu. */
const BROWSABLE_KIND = "collection";

type ErrorRow = Extract<TreeRow, { type: "error" }>;

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
  const [releasing, setReleasing] = useState(false);
  const [unlock, setUnlock] = useState<{ profile: Profile; needs: SecretsNeeded } | null>(null);
  const promptedErrors = useRef(new Set<string>());
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

  /**
   * A pointer click on a node: selects it and, for a connection whose engine has a query context (Couchbase), sets the
   * context of the active query tab on it (spec §3.11.1, ruling B48). Only a click does: walking the tree with the
   * keyboard (`selectRow` alone) never retargets the open tab.
   */
  const clickNode = (row: Extract<TreeRow, { type: "node" }>) => {
    selectRow(row);
    if (findEngineInfo(entries, engines, row.profileId)?.query_context) applyTreeContext(row.profileId, row.node.path);
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
      void openBrowseTab(row.profileId, row.node.path);
    } else if (row.type === "node" && !CONTAINER_KINDS.has(row.node.kind)) {
      openObject(row.profileId, row.node);
    } else if (isExpandable(row)) {
      toggle(row);
    }
  };

  // A children request failed with password_required: ask once per failure, then retry what failed.
  useEffect(() => {
    const failing = rows.filter((r): r is ErrorRow => r.type === "error" && r.code === "password_required");
    const failingKeys = new Set(failing.map((r) => r.key));
    for (const key of promptedErrors.current) if (!failingKeys.has(key)) promptedErrors.current.delete(key);
    if (unlock) return;
    const next = failing.find((r) => !promptedErrors.current.has(r.key));
    const entry = next && entries.find((e) => e.profile.id === next.profileId);
    if (next && entry) {
      promptedErrors.current.add(next.key);
      setUnlock({ profile: entry.profile, needs: secretsNeeded(next.detail) });
    }
  }, [rows, entries, unlock]);

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
   * table and object tabs close, query tabs agree to lose their session. Any "no" keeps the profile.
   */
  const releaseTabs = async (profileId: string): Promise<boolean> => {
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
          <div {...aria} className={base} style={pad} onClick={() => selectRow(row)} onDoubleClick={() => toggle(row)}>
            {chevron}
            <Icon size={14} className="shrink-0 text-muted" />
            <span className="truncate">{row.name}</span>
          </div>
        );
      }
      case "profile": {
        const p = row.entry.profile;
        const content = (
          <div
            {...aria}
            className={base}
            style={pad}
            onClick={() => selectRow(row)}
            onContextMenu={() => selectRow(row)}
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
        if (row.node.kind !== BROWSABLE_KIND) return content;
        return (
          <ContextMenu
            items={[
              { label: "Browse documents", onSelect: () => void openBrowseTab(row.profileId, row.node.path) },
              { label: "Details", onSelect: () => openObject(row.profileId, row.node) },
            ]}
          >
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
      {unlock && (
        <UnlockDialog
          open
          profile={unlock.profile}
          needs={unlock.needs}
          onOpenChange={(open) => !open && setUnlock(null)}
          onUnlocked={() => {
            void qc.refetchQueries({
              queryKey: ["children", unlock.profile.id],
              predicate: (query) => query.state.status === "error",
            });
            setUnlock(null);
          }}
        />
      )}
    </div>
  );
}
