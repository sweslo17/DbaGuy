import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { FieldRow, Input } from "@/components/ui/Field";
import { errorMessage } from "@/lib/api/client";
import type { Profile } from "@/lib/api/types";
import { useUnlockProfile } from "./hooks";

function UnlockDialogBody({
  profile,
  onOpenChange,
  onUnlocked,
}: {
  profile: Profile;
  onOpenChange: (open: boolean) => void;
  onUnlocked: () => void;
}) {
  const unlock = useUnlockProfile();
  const [password, setPassword] = useState("");
  const submit = () => {
    if (!password || unlock.isPending) return;
    unlock.mutate(
      { id: profile.id, secrets: { password, ssh_password: null } },
      {
        onSuccess: () => {
          onOpenChange(false);
          onUnlocked();
        },
      },
    );
  };
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={`Password for ${profile.name}`}
      width={420}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={!password || unlock.isPending}>
            {unlock.isPending ? "Connecting…" : "Connect"}
          </Button>
        </>
      }
    >
      <FieldRow label="Password">
        <Input
          type="password"
          autoComplete="new-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && submit()}
        />
      </FieldRow>
      <p className="mt-2 text-[11px] text-muted">Kept in memory until DbaGuy quits.</p>
      {unlock.isError && (
        <p className="mt-2 whitespace-pre-wrap text-[12px] text-danger" data-selectable>
          {errorMessage(unlock.error)}
        </p>
      )}
    </Dialog>
  );
}

/** Asks for the password of a profile whose password is not saved (backend answered `password_required`). */
export function UnlockDialog({
  profile,
  open,
  onOpenChange,
  onUnlocked,
}: {
  profile: Profile;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUnlocked: () => void;
}) {
  if (!open) return null;
  return <UnlockDialogBody key={profile.id} profile={profile} onOpenChange={onOpenChange} onUnlocked={onUnlocked} />;
}
