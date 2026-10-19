import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { useProfiles } from "./hooks";
import { isPasswordRequired, secretsNeeded, UnlockDialog } from "./UnlockDialog";

/**
 * Why something on a connection failed (a session that could not open or is gone, an object's detail that could not
 * load), with Retry. A missing password (`password_required`) asks for it first; once that dialog is dismissed, the
 * error and Retry show instead. Each new failure asks again. `onRetry` also runs once the password is given.
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
  if (!error) return null;

  const retry = () => {
    setDismissed(false);
    onRetry();
  };
  const profile = profiles.data?.find((entry) => entry.profile.id === profileId)?.profile;
  const needsPassword = isPasswordRequired(error) && !!profile;
  return (
    <>
      {(!needsPassword || dismissed) && (
        <div
          className="flex items-center gap-2 border-b border-border px-3 py-2 text-[12px] text-danger"
          data-selectable
        >
          <span className="flex-1 whitespace-pre-wrap">{error.message}</span>
          <Button size="sm" onClick={retry}>
            Retry
          </Button>
        </div>
      )}
      {needsPassword && profile && (
        <UnlockDialog
          open={!dismissed}
          profile={profile}
          needs={secretsNeeded(error.detail)}
          onOpenChange={(open) => !open && setDismissed(true)}
          onUnlocked={retry}
        />
      )}
    </>
  );
}
