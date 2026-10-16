import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Dialog } from "@/components/ui/Dialog";
import { isDialogOpen } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";
import { isSettingsKey } from "@/lib/shortcuts";
import { useAppSettings } from "@/stores/settings";
import { useWorkspace } from "@/stores/workspace";
import { readQueryTabActions } from "../query/tabActions";
import { COMMANDS, SHORTCUTS, type CommandContext } from "./commands";
import { closePalette, usePalette } from "./paletteStore";
import { PaletteInput, PaletteItem, PaletteList, PaletteRoot, type Choose } from "./parts";
import { QuickOpen } from "./QuickOpen";

/** ⌘⇧P (spec §4.10): the commands, those that cannot run now greyed out. */
function CommandList({ choose }: { choose: Choose }) {
  const queryClient = useQueryClient();
  const settings = useAppSettings();
  const activeTab = useWorkspace((s) => s.tabs.find((t) => t.id === s.activeTabId) ?? null);
  const [search, setSearch] = useState("");
  // Read on every render (each keystroke): whether a command is possible is as of now, without polling.
  const context: CommandContext = {
    activeTab,
    tabActions: activeTab?.kind === "query" ? readQueryTabActions(activeTab.id) : null,
    settings: { loaded: settings.loaded, current: settings.settings, save: settings.save },
    queryClient,
  };
  return (
    <PaletteRoot label="Commands">
      <PaletteInput
        value={search}
        onValueChange={setSearch}
        placeholder="Type a command…"
        hint={`${SHORTCUTS.quickOpen} Quick open`}
      />
      <PaletteList empty="No matching command">
        {COMMANDS.map((command) => {
          const checked = command.checked?.(context);
          const state = checked === undefined ? undefined : checked ? "On" : "Off";
          return (
            <PaletteItem
              key={command.id}
              value={command.label}
              disabled={!command.enabled(context)}
              onSelect={() => choose(() => command.run(context))}
              label={command.label}
              hint={
                state || command.shortcut ? (
                  <span className="flex gap-3">
                    {state && <span>{state}</span>}
                    {command.shortcut && <span>{command.shortcut}</span>}
                  </span>
                ) : undefined
              }
            />
          );
        })}
      </PaletteList>
    </PaletteRoot>
  );
}

/**
 * The command palette and quick open (spec §4.10), in the app's one dialog look; the app's ⌘⇧P and ⌘P open them. A
 * chosen command runs once the palette has closed and the focus is back where it was, so the tab's own handler finds
 * its editor focused and any dialog it opens is the only one.
 */
export function PaletteHost() {
  const mode = usePalette((s) => s.mode);
  const pending = useRef<(() => void) | null>(null);
  if (!mode) return null;

  const choose: Choose = (run) => {
    pending.current = run;
    closePalette();
  };
  const onClosed = (event: Event) => {
    event.preventDefault();
    const run = pending.current;
    pending.current = null;
    // A dialog open now replaced the palette (⌘, from it): that one keeps the focus. The palette itself counts too, which
    // absorbs the spurious call StrictMode makes while it is still open (its double mount unmounts the focus scope).
    const { returnFocus } = usePalette.getState();
    if (!isDialogOpen() && returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus();
    run?.();
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && closePalette()}
      title={mode === "commands" ? "Commands" : "Quick open"}
      width={600}
      palette
      onCloseAutoFocus={onClosed}
    >
      {/* ⌘, from the palette: Settings (the window's handler) opens in its place. */}
      <div
        className="flex min-h-0 flex-1 flex-col"
        onKeyDown={(e) => {
          if (!isImeKeyEvent(e) && isSettingsKey(e)) closePalette();
        }}
      >
        {/* A palette error shows here (Escape still closes it); the next ⌘P / ⌘⇧P starts afresh. */}
        <ErrorBoundary key={mode} name="Palette" title="The palette hit an error" recover="retry">
          {mode === "commands" ? <CommandList choose={choose} /> : <QuickOpen choose={choose} />}
        </ErrorBoundary>
      </div>
    </Dialog>
  );
}
