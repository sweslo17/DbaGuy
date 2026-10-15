import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, Plus, RefreshCw, Server } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ContextMenu } from "@/components/ui/ContextMenu";
import { errorMessage } from "@/lib/api/client";
import type { Profile, ProfileEntry } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { useWorkspace } from "@/stores/workspace";
import { ConnectionDialog, ENGINE_LABELS } from "../connections/ConnectionDialog";
import { useDeleteProfile, useProfiles } from "../connections/hooks";
import { secretsNeeded, UnlockDialog, type SecretsNeeded } from "../connections/UnlockDialog";
import { folderLabel, kindIcon } from "./kinds";
import { nodeKeyPrefix, profileKey, useTreeRows, type Expansion, type TreeRow } from "./useTreeRows";

const ROW_HEIGHT = 22;
const COLOR_CLASSES: Record<string, string> = {
  none: "bg-transparent",
  green: "bg-green-500",
  yellow: "bg-yellow-400",
  orange: "bg-orange-500",
  red: "bg-red-500",
  purple: "bg-purple-500",
};
/** Nodes that group other nodes: double-click expands them instead of opening a detail tab. */
const CONTAINER_KINDS = new Set(["folder", "database", "schema"]);

type ErrorRow = Extract<TreeRow, { type: "error" }>;

const isExpandable = (row: TreeRow) =>
  row.type === "folder" || row.type === "profile" || (row.type === "node" && row.node.has_children);

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
  const deleteProfile = useDeleteProfile();
  const openObject = useWorkspace((s) => s.openObject);
  const openConsole = useWorkspace((s) => s.openConsole);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // Which {profile, path} each expandable row loads its children from; filled the first time a row is expanded.
  const [expansions] = useState(() => new Map<string, Expansion>());
  const [selected, setSelected] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ open: boolean; entry?: ProfileEntry }>({ open: false });
  const [toDelete, setToDelete] = useState<ProfileEntry | null>(null);
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
    if (row.type === "node" && !CONTAINER_KINDS.has(row.node.kind)) {
      openObject(row.profileId, row.node, row.profileName);
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
    qc.removeQueries({ queryKey: ["children", profileId] });
    qc.removeQueries({ queryKey: ["detail", profileId] });
    const { tabs, closeTab } = useWorkspace.getState();
    tabs.filter((t) => t.profileId === profileId).forEach((t) => closeTab(t.id));
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const index = rows.findIndex((r) => r.key === selected);
    const row = rows[index] as TreeRow | undefined;
    const select = (i: number) => {
      const clamped = Math.max(0, Math.min(rows.length - 1, i));
      const target = rows[clamped];
      if (!target) return;
      setSelected(target.key);
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
          <div {...aria} className={base} style={pad} onClick={() => setSelected(row.key)} onDoubleClick={() => toggle(row)}>
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
            onClick={() => setSelected(row.key)}
            onContextMenu={() => setSelected(row.key)}
            onDoubleClick={() => toggle(row)}
          >
            {chevron}
            <Server size={14} className="shrink-0 text-muted" />
            <span className={cn("h-2 w-2 shrink-0 rounded-full", COLOR_CLASSES[p.color])} />
            <span className="truncate font-medium">{p.name}</span>
            <span className="ml-auto shrink-0 text-[11px] text-muted">{ENGINE_LABELS[p.engine]}</span>
          </div>
        );
        return (
          <ContextMenu
            items={[
              { label: "New Query", onSelect: () => openConsole(p.id, null, p.name) },
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
        return (
          <div
            {...aria}
            className={cn(base, row.node.system && "opacity-60")}
            style={pad}
            title={row.node.detail ? `${label} — ${row.node.detail}` : label}
            onClick={() => setSelected(row.key)}
            onDoubleClick={() => activate(row)}
          >
            {chevron}
            <Icon size={14} className="shrink-0 text-muted" />
            <span className="truncate">{label}</span>
            {row.node.detail && <span className="ml-auto shrink-[100] truncate pl-2 text-[11px] text-muted">{row.node.detail}</span>}
          </div>
        );
      }
      case "loading":
        return (
          <div className={base} style={pad}>
            <span className="w-4 shrink-0" />
            <span className="text-muted">Loading…</span>
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
          pending={deleteProfile.isPending}
          error={deleteProfile.isError ? errorMessage(deleteProfile.error) : null}
          message={`Delete "${toDelete.profile.name}"? Its saved password is removed from the Keychain.`}
          onOpenChange={(open) => {
            if (open) return;
            deleteProfile.reset();
            setToDelete(null);
          }}
          onConfirm={() =>
            deleteProfile.mutate(toDelete.profile.id, {
              onSuccess: () => {
                forgetProfile(toDelete.profile.id);
                setToDelete(null);
              },
            })
          }
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
