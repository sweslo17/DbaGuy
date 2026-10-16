import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api, unwrap } from "@/lib/api/client";
import type { AppSettings } from "@/lib/api/types";

export type { AppSettings };

export const settingsKey = ["settings"] as const;

/** The documented defaults (spec §4.9): what the app uses until `GET /settings` answers (or if it never does). */
export const DEFAULT_SETTINGS: AppSettings = {
  editor: { font_size: 13, vim: false, word_wrap: false, tab_size: 2, keyword_case: "upper" },
  run: { default: "statement", continue_on_error: false, page_size: 500, statement_timeout_s: 0 },
  safety: { idle_transaction_warn_minutes: 3, idle_transaction_rollback_minutes: 0 },
  notifications: { long_query_seconds: 15 },
  scripts_dir: null,
};

/**
 * The app preferences (`settings.json`), loaded once and shared by every reader. `save` writes them with
 * `PUT /settings` and puts the stored result in the cache, so every open editor and the next run pick it up at once.
 */
export function useAppSettings(): {
  settings: AppSettings;
  isLoading: boolean;
  save: (next: AppSettings) => Promise<void>;
} {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: settingsKey,
    queryFn: async () => unwrap(await (await api()).GET("/settings")),
    staleTime: Infinity,
  });
  const save = useCallback(
    async (next: AppSettings) => {
      const saved = unwrap(await (await api()).PUT("/settings", { body: next }));
      qc.setQueryData(settingsKey, saved);
    },
    [qc],
  );
  return { settings: query.data ?? DEFAULT_SETTINGS, isLoading: query.isLoading, save };
}
