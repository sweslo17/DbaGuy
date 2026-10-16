import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { useProfiles } from "../connections/hooks";
import { secretsNeeded, UnlockDialog } from "../connections/UnlockDialog";

/**
 * Why a session could not open (or is gone), with Retry. A missing password (`password_required`) asks for it first;
 * once that dialog is dismissed, the error and Retry show instead. Each new failure asks again.
 */
export function SessionError({
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
  const needsPassword = error instanceof ApiError && error.code === "password_required" && !!profile;
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
