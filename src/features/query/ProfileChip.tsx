import type { ComponentProps } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import type { Profile } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { profileColor } from "../connections/profileColors";
import { PickerTrigger } from "./PickerTrigger";

/**
 * A connection picker's button (the query tab's toolbar, a data tool's target): the engine's icon, the colour dot and
 * the name, or "Choose a connection". A red (production) connection sits on a red chip, also while disabled. `framed`:
 * a 1px border around a connection that is not red (in a form, where a toolbar's bare button would not read as one).
 * Radix passes its trigger props (and ref) through.
 */
export function ProfileChip({
  profile,
  profileId,
  framed = false,
  className,
  ...props
}: ComponentProps<"button"> & { profile: Profile | null; profileId: string | null; framed?: boolean }) {
  const red = profile?.color === "red";
  const tint = profileColor(profile?.color);
  return (
    <PickerTrigger
      {...props}
      className={cn(
        className,
        red
          ? "bg-danger font-medium text-accent-fg enabled:hover:brightness-110"
          : cn("enabled:hover:bg-hover", framed && "border border-border"),
      )}
    >
      {profile && <EngineIcon engine={profile.engine} size={12} />}
      {!red && tint && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tint }} />}
      <span className={cn("truncate", profile ? "font-medium" : "text-muted")}>
        {profile?.name ?? (profileId ? "…" : "Choose a connection")}
      </span>
    </PickerTrigger>
  );
}
