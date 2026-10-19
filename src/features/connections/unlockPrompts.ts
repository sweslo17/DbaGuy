import { useEffect, useRef } from "react";
import { create } from "zustand";
import type { Profile } from "@/lib/api/types";
import type { SecretsNeeded } from "./UnlockDialog";

/*
 * Who asks for a profile's password. Any number of tabs and tree rows can fail with `password_required` for one profile
 * (a restored workspace opens them all at once), but the person has one password to give: requests are queued per
 * profile and the head of the queue is the one dialog on screen (`UnlockPromptHost`). A request for a profile that is
 * queued or open joins it. Giving the password wakes every joined request (`onUnlocked`); cancelling wakes them all with
 * `onCancelled`, and each shows its own Retry. A request that goes away (its tab closed, its failure recovered because
 * the profile was unlocked elsewhere) leaves, and the dialog closes with its last one.
 */

/** One failed request waiting on a profile's dialog; both ways of ending it are optional. */
export interface UnlockWaiter {
  /** The password was given: read again what failed. */
  onUnlocked?: () => void;
  /** The dialog was cancelled: the failure stays, with its own Retry. */
  onCancelled?: () => void;
}

/** What a failed request needs to ask for a password. */
export interface UnlockRequest {
  profile: Profile;
  needs: SecretsNeeded;
}

interface Prompt extends UnlockRequest {
  waiters: readonly UnlockWaiter[];
}

interface UnlockPromptsState {
  /** One prompt per profile, in the order they were first asked for; the first is open. */
  queue: readonly Prompt[];
}

export const useUnlockPrompts = create<UnlockPromptsState>(() => ({ queue: [] }));

/** Everything either request asks for: a dialog already open grows a field rather than asking twice. */
const mergeNeeds = (a: SecretsNeeded, b: SecretsNeeded): SecretsNeeded => ({
  password: a.password || b.password,
  ssh_password: a.ssh_password || b.ssh_password,
});

/**
 * Asks for the secrets `request` names, or joins the prompt of its profile when there is one. Returns what to call
 * when the request no longer wants the answer (safe to call after the prompt has ended).
 */
export function joinUnlockPrompt({ profile, needs }: UnlockRequest, waiter: UnlockWaiter): () => void {
  useUnlockPrompts.setState(({ queue }) => {
    const existing = queue.find((prompt) => prompt.profile.id === profile.id);
    if (!existing) return { queue: [...queue, { profile, needs, waiters: [waiter] }] };
    const joined = { ...existing, needs: mergeNeeds(existing.needs, needs), waiters: [...existing.waiters, waiter] };
    return { queue: queue.map((prompt) => (prompt === existing ? joined : prompt)) };
  });
  return () =>
    useUnlockPrompts.setState((state) => {
      const prompt = state.queue.find((entry) => entry.profile.id === profile.id);
      if (!prompt?.waiters.includes(waiter)) return state;
      const waiters = prompt.waiters.filter((entry) => entry !== waiter);
      const queue = waiters.length
        ? state.queue.map((entry) => (entry === prompt ? { ...prompt, waiters } : entry))
        : state.queue.filter((entry) => entry !== prompt);
      return { queue };
    });
}

/** Ends the profile's prompt (the dialog goes, the next profile's opens) and tells each request how it ended. */
function endPrompt(profileId: string, outcome: keyof UnlockWaiter): void {
  const prompt = useUnlockPrompts.getState().queue.find((entry) => entry.profile.id === profileId);
  if (!prompt) return;
  useUnlockPrompts.setState(({ queue }) => ({ queue: queue.filter((entry) => entry !== prompt) }));
  for (const waiter of prompt.waiters) waiter[outcome]?.();
}

/** The password was given for this profile. */
export const finishUnlockPrompt = (profileId: string): void => endPrompt(profileId, "onUnlocked");

/** The dialog was cancelled. */
export const cancelUnlockPrompt = (profileId: string): void => endPrompt(profileId, "onCancelled");

/**
 * Keeps `request` in its profile's prompt for as long as it is not null, and asks again for each new request object (a
 * new failure). Memoize the request on the failure so that it changes only when the failure does. The handlers may
 * change on every render.
 */
export function useUnlockPrompt(request: UnlockRequest | null, handlers: UnlockWaiter): void {
  const latest = useRef(handlers);
  useEffect(() => {
    latest.current = handlers;
  });
  useEffect(() => {
    if (!request) return;
    return joinUnlockPrompt(request, {
      onUnlocked: () => latest.current.onUnlocked?.(),
      onCancelled: () => latest.current.onCancelled?.(),
    });
  }, [request]);
}
