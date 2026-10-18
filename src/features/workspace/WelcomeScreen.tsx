import { Plus } from "lucide-react";
import logoColor from "@/assets/logo-color-1024.png";
import logoReversed from "@/assets/logo-reversed-1024.png";
import { Button } from "@/components/ui/Button";
import { openNewQuery } from "@/stores/workspace";
import { SHORTCUTS } from "../palette/commands";

/** The shortcut hints under the logo, with the labels the palette shows. */
const HINTS = [
  { label: "Quick open", keys: SHORTCUTS.quickOpen },
  { label: "Commands", keys: SHORTCUTS.commandPalette },
] as const;

/**
 * What the workspace shows while no tab is open. The logo follows the same `prefers-color-scheme` as the colour
 * tokens in `styles.css`: the reversed one has light lettering for the dark theme.
 */
export function WelcomeScreen() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 p-6 text-center">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcSet={logoReversed} />
        <img src={logoColor} alt="DbaGuy" width={280} draggable={false} className="h-auto w-[280px] max-w-full" />
      </picture>
      <Button variant="primary" onClick={() => void openNewQuery()}>
        <Plus size={14} aria-hidden />
        New query
      </Button>
      <div className="flex flex-col items-center gap-1.5 text-[12px] text-muted">
        <p>Open a table from the sidebar, or right-click a connection → New Query</p>
        <ul className="flex gap-4">
          {HINTS.map(({ label, keys }) => (
            <li key={label}>
              {label} <span className="font-mono">{keys}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
