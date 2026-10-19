import { useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { FieldRow, Input } from "@/components/ui/Field";
import { ApiError, errorMessage } from "@/lib/api/client";
import type { Profile } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { useUnlockProfile } from "./hooks";

/** Which secrets a `password_required` error asks for (its `detail`). */
export interface SecretsNeeded {
  password: boolean;
  ssh_password: boolean;
}

const DB_PASSWORD_ONLY: SecretsNeeded = { password: true, ssh_password: false };

/** Whether `error` is the backend's `password_required`: a secret is missing, and asking again cannot help before. */
export function isPasswordRequired(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === "password_required";
}

/** Reads the `detail` of a `password_required` error; without one only the database password is asked for. */
export function secretsNeeded(detail: unknown): SecretsNeeded {
  if (typeof detail !== "object" || detail === null) return DB_PASSWORD_ONLY;
  const { password, ssh_password } = detail as Partial<SecretsNeeded>;
  const needs = { password: password === true, ssh_password: ssh_password === true };
  return needs.password || needs.ssh_password ? needs : DB_PASSWORD_ONLY;
}

function UnlockDialogBody({
  profile,
  needs,
  onOpenChange,
  onUnlocked,
}: {
  profile: Profile;
  needs: SecretsNeeded;
  onOpenChange: (open: boolean) => void;
  onUnlocked: () => void;
}) {
  const unlock = useUnlockProfile();
  const [password, setPassword] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const complete = (!needs.password || !!password) && (!needs.ssh_password || !!sshPassword);
  const submit = () => {
    if (!complete || unlock.isPending) return;
    unlock.mutate(
      {
        id: profile.id,
        secrets: { password: needs.password ? password : null, ssh_password: needs.ssh_password ? sshPassword : null },
      },
      {
        onSuccess: () => {
          onOpenChange(false);
          onUnlocked();
        },
      },
    );
  };
  const onEnter = (e: KeyboardEvent) => e.key === "Enter" && !isImeKeyEvent(e) && submit();
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={`${needs.password ? "Password" : "SSH password"} for ${profile.name}`}
      width={420}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={!complete || unlock.isPending}>
            {unlock.isPending ? "Connecting…" : "Connect"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">
        {needs.password && (
          <FieldRow label="Password">
            <Input
              type="password"
              autoComplete="new-password"
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={onEnter}
            />
          </FieldRow>
        )}
        {needs.ssh_password && (
          <FieldRow label="SSH password">
            <Input
              type="password"
              autoComplete="new-password"
              autoFocus={!needs.password}
              value={sshPassword}
              onChange={(e) => setSshPassword(e.target.value)}
              onKeyDown={onEnter}
            />
          </FieldRow>
        )}
      </div>
      <p className="mt-2 text-[11px] text-muted">Kept in memory until DbaGuy quits.</p>
      {unlock.isError && (
        <p className="mt-2 whitespace-pre-wrap text-[12px] text-danger" data-selectable>
          {errorMessage(unlock.error)}
        </p>
      )}
    </Dialog>
  );
}

/** Asks for the secrets a profile is missing (the backend answered `password_required`; `needs` is its detail). */
export function UnlockDialog({
  profile,
  needs = DB_PASSWORD_ONLY,
  open,
  onOpenChange,
  onUnlocked,
}: {
  profile: Profile;
  needs?: SecretsNeeded;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUnlocked: () => void;
}) {
  if (!open) return null;
  return (
    <UnlockDialogBody
      key={profile.id}
      profile={profile}
      needs={needs}
      onOpenChange={onOpenChange}
      onUnlocked={onUnlocked}
    />
  );
}
