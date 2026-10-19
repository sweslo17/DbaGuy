import { useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { useProfiles } from "./hooks";
import { isPasswordRequired, secretsNeeded } from "./UnlockDialog";
import { useUnlockPrompt } from "./unlockPrompts";

/**
 * Why something on a connection failed (a session that could not open or is gone, an object's detail that could not
 * load), with Retry. A missing password (`password_required`) asks for it first, in the profile's one unlock dialog
 * (`unlockPrompts`: other tabs failing for the same profile join it); once that dialog is cancelled, the error and
 * Retry show instead. Each new failure asks again. `onRetry` also runs once the password is given.
 */
export function RecoverableError({
  profileId,
  error,
  onRetry,
}: {
  profileId: string;
  error: Error | null;
  onRetry: () => void;
}) {
  const profiles = useProfiles();
  const [dismissed, setDismissed] = useState(false);
  const [seen, setSeen] = useState(error);
  if (error !== seen) {
    setSeen(error);
    setDismissed(false);
  }
  const retry = () => {
    setDismissed(false);
    onRetry();
  };
  const profile = profiles.data?.find((entry) => entry.profile.id === profileId)?.profile;
  const asking = isPasswordRequired(error) && !!profile && !dismissed;
  // One request per failure: a new error asks again, a re-render does not.
  const request = useMemo(
    () => (asking && profile && isPasswordRequired(error) ? { profile, needs: secretsNeeded(error.detail) } : null),
    [asking, profile, error],
  );
  useUnlockPrompt(request, { onUnlocked: retry, onCancelled: () => setDismissed(true) });
  if (!error || request) return null;

  return (
    <div
      className="flex items-center gap-2 border-b border-border px-3 py-2 text-[12px] text-danger"
      data-selectable
    >
      <span className="flex-1 whitespace-pre-wrap">{error.message}</span>
      <Button size="sm" onClick={retry}>
        Retry
      </Button>
    </div>
  );
}
