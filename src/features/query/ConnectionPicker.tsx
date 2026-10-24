import { useQuery } from "@tanstack/react-query";
import { Database } from "lucide-react";
import { useState } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { errorMessage } from "@/lib/api/client";
import type { PathSegment, Profile, ProfileEntry, SessionInfo } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { ENGINE_LABELS } from "@/lib/engines";
import { buildFolderTree, type FolderNode } from "../connections/folders";
import { useEngineInfo, useProfiles } from "../connections/hooks";
import { childrenQuery } from "../explorer/useTreeRows";
import type { SessionPhase } from "../session/SessionController";
import { ContextPicker, type ContextPickerProps } from "./ContextPicker";
import { PickerTrigger } from "./PickerTrigger";
import { ProfileChip } from "./ProfileChip";

const PHASE_DOT: Record<SessionPhase, string> = {
  opening: "bg-warning",
  open: "bg-success",
  reconnecting: "bg-warning",
  failed: "bg-danger",
  closed: "bg-danger",
};

export interface ConnectionPickerProps {
  profileId: string | null;
  /** The database the tab asked for; null: the profile's default. */
  database: string | null;
  phase: SessionPhase;
  /** What the open session reports (its database and server). */
  session: SessionInfo | null;
  sessionFailed: boolean;
  /** Set while the connection cannot change (e.g. a run is active): the picker is disabled with this tooltip. */
  blockedReason?: string;
  /** A new profile (database null: its default) or another database of the same profile. */
  onChange: (profileId: string, database: string | null) => void;
  /** The query context picker, shown for engines that take one (Couchbase), after the profile. */
  context?: ContextPickerProps;
}

/**
 * The connections as menu items: folders become submenus, like the explorer's tree. `disabled`: why a connection cannot
 * be picked here (null: it can); the reason greys its item and is its tooltip.
 */
export function profileItems(
  folder: FolderNode,
  current: string | null,
  pick: (id: string) => void,
  disabled?: (profile: Profile) => string | null,
): MenuItem[] {
  return [
    ...folder.folders.map((sub) => ({ label: sub.name, children: profileItems(sub, current, pick, disabled) })),
    ...folder.profiles.map(({ profile }) => {
      const reason = disabled?.(profile) ?? null;
      return {
        label: profile.name,
        // The engine's name is the row's shortcut text.
        icon: <EngineIcon engine={profile.engine} decorative />,
        shortcut: ENGINE_LABELS[profile.engine],
        checked: profile.id === current,
        onSelect: () => pick(profile.id),
        ...(reason ? { disabled: true, title: reason } : {}),
      };
    }),
  ];
}

const NO_PATH: PathSegment[] = [];

/** What the database picker lists: the root's database nodes, or MySQL's schema nodes (its databases). */
export type DatabaseNodeKind = "database" | "schema";

const NODE_WORDS: Record<DatabaseNodeKind, { name: string; none: string; fallback: string }> = {
  database: { name: "Database", none: "No databases", fallback: "Default database" },
  schema: { name: "Schema", none: "No schemas", fallback: "Default schema" },
};

/**
 * The databases of the connection, from the explorer's root children (loaded when the menu opens): for engines with
 * one connection per database (PostgreSQL) in the query tab, and for a data tool's target (`nodeKind` "schema": MySQL's
 * schema nodes). `current` null: the connection's default.
 */
export function DatabasePicker({
  profileId,
  current,
  blockedReason,
  onPick,
  nodeKind = "database",
  under = NO_PATH,
}: {
  profileId: string;
  current: string | null;
  blockedReason?: string;
  onPick: (database: string) => void;
  nodeKind?: DatabaseNodeKind;
  /** Whose children it lists: the root, or a database node (a copy's target schema in PostgreSQL, SQL Server; 06c). */
  under?: PathSegment[];
}) {
  const words = NODE_WORDS[nodeKind];
  const [open, setOpen] = useState(false);
  const children = useQuery({ ...childrenQuery(profileId, under), enabled: open });
  let items: MenuItem[];
  if (children.isPending) items = [{ label: "Loading…", disabled: true }];
  else if (children.isError) items = [{ label: errorMessage(children.error), disabled: true }];
  else {
    const databases = children.data.filter((node) => node.kind === nodeKind);
    items = databases.length
      ? databases.map((node) => ({
          label: node.name,
          checked: node.name === current,
          onSelect: () => onPick(node.name),
        }))
      : [{ label: words.none, disabled: true }];
  }
  return (
    <DropdownMenu items={items} onOpenChange={setOpen} className="max-h-[60vh] max-w-[320px] overflow-y-auto">
      <PickerTrigger
        disabled={!!blockedReason}
        title={blockedReason ?? `${words.name}: ${current ?? "the connection's default"}`}
        className="max-w-[160px] enabled:hover:bg-hover"
      >
        <Database size={12} className="shrink-0 text-muted" />
        <span className="truncate">{current ?? words.fallback}</span>
      </PickerTrigger>
    </DropdownMenu>
  );
}

/**
 * The tab's connection (spec §4.2): the profile (grouped by folder) and, for engines with one connection per database,
 * the database, or, for engines with a query context (Couchbase), the context picker, with the session's phase as a dot
 * and its server version. A red profile's name sits on a red chip. Changing the profile or the database reopens the
 * tab's session (the tab asks first if that would lose an export or a transaction); the context does not.
 */
export function ConnectionPicker(props: ConnectionPickerProps) {
  const { profileId, database, phase, session, sessionFailed, blockedReason, onChange, context } = props;
  const engine = useEngineInfo(profileId);
  const profiles = useProfiles();
  const entries: ProfileEntry[] = profiles.data ?? [];
  const profile = entries.find((entry) => entry.profile.id === profileId)?.profile ?? null;
  const pickProfile = (id: string) => {
    if (id !== profileId) onChange(id, null);
  };
  const items = entries.length
    ? profileItems(buildFolderTree(entries), profileId, pickProfile)
    : [{ label: profiles.isPending ? "Loading…" : "No connections", disabled: true }];
  const currentDatabase =
    database ?? session?.database ?? (profile && "database" in profile ? (profile.database ?? null) : null);
  const detail = session ? session.server.version : sessionFailed ? "Not connected" : profileId ? "Connecting…" : null;

  // Its parts sit directly in the toolbar's row (`contents`): when the row is crowded the server's version gives way
  // first, then the profile and database names truncate; the transaction badge never shrinks.
  return (
    <div className="contents">
      {profileId !== null && (
        <span className={cn("h-2 w-2 shrink-0 rounded-full", PHASE_DOT[phase])} title={`Session ${phase}`} />
      )}
      <DropdownMenu items={items} align="end" className="max-h-[60vh] max-w-[360px] overflow-y-auto">
        <ProfileChip
          profile={profile}
          profileId={profileId}
          disabled={!!blockedReason}
          title={blockedReason ?? (profile ? `Connection: ${profile.name}` : "Connection")}
          className="max-w-[180px]"
        />
      </DropdownMenu>
      {profileId !== null && engine?.per_database_connection && (
        <DatabasePicker
          profileId={profileId}
          current={currentDatabase}
          blockedReason={blockedReason}
          onPick={(name) => name !== currentDatabase && onChange(profileId, name)}
        />
      )}
      {profileId !== null && engine?.query_context && context && <ContextPicker profileId={profileId} {...context} />}
      {/* Basis 0: it only takes the room left over (up to its own width); in a narrow toolbar it is not shown at all. */}
      {detail && (
        <span className="max-w-max min-w-0 grow basis-0 truncate text-[11px] text-muted @max-[50rem]:hidden">
          {detail}
        </span>
      )}
    </div>
  );
}
