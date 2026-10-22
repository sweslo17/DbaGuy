import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
import type { Profile, ProfileEntry } from "@/lib/api/types";
import { buildFolderTree } from "../connections/folders";
import { findEngineInfo, useEngines, useProfiles } from "../connections/hooks";
import { DatabasePicker, profileItems } from "../query/ConnectionPicker";
import { ProfileChip } from "../query/ProfileChip";
import { databaseNodes } from "./runForm";

/** Why a connection cannot be a run's target: a read-only one is refused before anything is sent (06a). */
const READ_ONLY = "Read-only: it cannot run files";
const readOnlyReason = (profile: Profile): string | null => (profile.read_only ? READ_ONLY : null);

/**
 * Where Run SQL file runs (spec ⑥ §3.6): the connection, among those whose engine runs files (a read-only one greyed
 * with why), and its database where the engine has them (MySQL: its schemas). The trigger reads as the query tab's: a
 * red connection on a red chip. Picking a connection gives its default database (`onChange(id, null)`).
 */
export function TargetPicker({
  profileId,
  database,
  onChange,
}: {
  profileId: string | null;
  database: string | null;
  onChange: (profileId: string, database: string | null) => void;
}) {
  const profiles = useProfiles();
  const engines = useEngines();
  const entries: ProfileEntry[] = profiles.data ?? [];
  const profile = entries.find((entry) => entry.profile.id === profileId)?.profile ?? null;
  const engine = findEngineInfo(entries, engines.data, profileId);
  // Only once both lists are here: until then, which engines run files is not known
  const runnable = engines.data
    ? entries.filter((entry) => findEngineInfo(entries, engines.data, entry.profile.id)?.run_file)
    : [];
  const pick = (id: string) => {
    if (id !== profileId) onChange(id, null);
  };
  const items: MenuItem[] = runnable.length
    ? profileItems(buildFolderTree(runnable), profileId, pick, readOnlyReason)
    : [{ label: profiles.isPending || engines.isPending ? "Loading…" : "No connections", disabled: true }];
  const nodes = databaseNodes(engine);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <DropdownMenu items={items} className="max-h-[60vh] max-w-[360px] overflow-y-auto">
        {/* A red (production) connection on a red chip, as in the query tab's toolbar */}
        <ProfileChip
          profile={profile}
          profileId={profileId}
          framed
          title={profile ? `Connection: ${profile.name}` : "Connection"}
          aria-label="Connection"
          className="max-w-[280px]"
        />
      </DropdownMenu>
      {profileId !== null && nodes && (
        <DatabasePicker
          profileId={profileId}
          current={database}
          nodeKind={nodes}
          onPick={(name) => name !== database && onChange(profileId, name)}
        />
      )}
    </div>
  );
}
