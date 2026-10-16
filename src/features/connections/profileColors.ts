import type { Profile } from "@/lib/api/types";

export type ProfileColor = Profile["color"];

/**
 * The CSS colour of each profile colour (the explorer's swatch, the tab underline and the editor's top border); none:
 * no tint. Tailwind palette variables, written out in full so Tailwind emits them. In menu order.
 */
export const PROFILE_COLORS: Record<ProfileColor, string | null> = {
  none: null,
  green: "var(--color-green-500)",
  yellow: "var(--color-yellow-400)",
  orange: "var(--color-orange-500)",
  red: "var(--color-red-500)",
  purple: "var(--color-purple-500)",
};

export const PROFILE_COLOR_ORDER = Object.keys(PROFILE_COLORS) as ProfileColor[];

/** The tint of a profile colour, or null for none (and for a missing profile). */
export function profileColor(color: ProfileColor | null | undefined): string | null {
  return color ? PROFILE_COLORS[color] : null;
}
