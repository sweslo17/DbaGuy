import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { Dialect, ProfileDraft, ProfileSecretsInput } from "@/lib/api/types";

export const profilesKey = ["profiles"] as const;

export function useProfiles() {
  return useQuery({ queryKey: profilesKey, queryFn: async () => unwrap(await (await api()).GET("/profiles")) });
}

export function useEngines() {
  return useQuery({
    queryKey: ["engines"],
    queryFn: async () => unwrap(await (await api()).GET("/system/engines")),
    staleTime: Infinity,
  });
}

/** SQL dialect of a profile's engine; null without a profile and until profiles and engines are loaded. */
export function useDialect(profileId: string | null): Dialect | null {
  const profiles = useProfiles();
  const engines = useEngines();
  const engine = profiles.data?.find((entry) => entry.profile.id === profileId)?.profile.engine;
  return engines.data?.find((info) => info.engine === engine)?.dialect ?? null;
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
      if (input.id) {
        qc.invalidateQueries({ queryKey: ["children", input.id] });
        qc.invalidateQueries({ queryKey: ["detail", input.id] });
      }
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

export function useUnlockProfile() {
  return useMutation({
    mutationFn: async (input: { id: string; secrets: ProfileSecretsInput }) =>
      unwrap(
        await (await api()).POST("/profiles/{profile_id}/unlock", {
          params: { path: { profile_id: input.id } },
          body: input.secrets,
        }),
      ),
  });
}
