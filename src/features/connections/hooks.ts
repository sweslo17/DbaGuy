import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { Dialect, EngineInfo, ProfileDraft, ProfileEntry, ProfileSecretsInput } from "@/lib/api/types";
import { invalidateExplorer, retryFailedExplorer } from "../explorer/explorerCache";

export const profilesKey = ["profiles"] as const;

export async function fetchProfiles() {
  return unwrap(await (await api()).GET("/profiles"));
}

export function useProfiles() {
  return useQuery({ queryKey: profilesKey, queryFn: fetchProfiles });
}

/** Whether profile `profileId` is read-only; false for an unknown profile and while the list is missing. */
export function isReadOnlyProfile(entries: readonly ProfileEntry[] | undefined, profileId: string | null): boolean {
  return entries?.find((entry) => entry.profile.id === profileId)?.profile.read_only === true;
}

/** Whether profile `profileId` is read-only (a document tab then neither saves nor deletes). */
export function useReadOnlyProfile(profileId: string | null): boolean {
  return isReadOnlyProfile(useProfiles().data, profileId);
}

export const enginesKey = ["engines"] as const;

export function useEngines() {
  return useQuery({
    queryKey: enginesKey,
    queryFn: async () => unwrap(await (await api()).GET("/system/engines")),
    staleTime: Infinity,
  });
}

/** What the engine of profile `profileId` is and can do; null for an unknown profile or while either list is missing. */
export function findEngineInfo(
  profiles: readonly ProfileEntry[] | undefined,
  engines: readonly EngineInfo[] | undefined,
  profileId: string | null,
): EngineInfo | null {
  const engine = profiles?.find((entry) => entry.profile.id === profileId)?.profile.engine;
  return engines?.find((info) => info.engine === engine) ?? null;
}

/**
 * What a profile's engine is and can do (dialect, capabilities); null without a profile and until profiles and engines
 * are loaded. It wraps queries, so call it unconditionally at the top of a component and derive booleans afterwards.
 */
export function useEngineInfo(profileId: string | null): EngineInfo | null {
  return findEngineInfo(useProfiles().data, useEngines().data, profileId);
}

/** SQL dialect of a profile's engine; null without a profile and until profiles and engines are loaded. */
export function useDialect(profileId: string | null): Dialect | null {
  return useEngineInfo(profileId)?.dialect ?? null;
}

export function useSaveProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: { id?: string; profile: ProfileDraft; secrets: ProfileSecretsInput }) => {
      const client = await api();
      const body = { profile: input.profile, secrets: input.secrets };
      return input.id
        ? unwrap(await client.PUT("/profiles/{profile_id}", { params: { path: { profile_id: input.id } }, body }))
        : unwrap(await client.POST("/profiles", { body }));
    },
    onSuccess: (_, input) => {
      qc.invalidateQueries({ queryKey: profilesKey });
      if (input.id) void invalidateExplorer(qc, input.id);
    },
  });
}

export function useDeleteProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) =>
      unwrap(await (await api()).DELETE("/profiles/{profile_id}", { params: { path: { profile_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: profilesKey }),
  });
}

export function useTestConnection() {
  return useMutation({
    mutationFn: async (input: { profile: ProfileDraft; secrets: ProfileSecretsInput; existingId?: string }) =>
      unwrap(
        await (await api()).POST("/profiles/test", {
          body: { profile: input.profile, secrets: input.secrets, existing_id: input.existingId ?? null },
        }),
      ),
  });
}

export function useImportUrl() {
  return useMutation({
    mutationFn: async (url: string) => unwrap(await (await api()).POST("/profiles/import-url", { body: { url } })),
  });
}

/**
 * Gives a profile the secrets it was missing (kept in memory). Wherever the dialog was opened (the tree, a query tab, a
 * detail tab), whatever of the profile's explorer had failed is read again; the dialog's own `onUnlocked` is for the
 * caller's own retry.
 */
export function useUnlockProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: { id: string; secrets: ProfileSecretsInput }) =>
      unwrap(
        await (await api()).POST("/profiles/{profile_id}/unlock", {
          params: { path: { profile_id: input.id } },
          body: input.secrets,
        }),
      ),
    // Not returned: the dialog closes now, the reads finish when they finish.
    onSuccess: (_, { id }) => void retryFailedExplorer(qc, id),
  });
}
