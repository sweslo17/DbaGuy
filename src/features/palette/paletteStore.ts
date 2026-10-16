import { create } from "zustand";

export type PaletteMode = "commands" | "quickOpen";

interface PaletteState {
  mode: PaletteMode | null;
  /** What had the focus before the palette opened: it gets it back when the palette closes. */
  returnFocus: Element | null;
}

/** Which palette is open (spec §4.10), if any. */
export const usePalette = create<PaletteState>(() => ({ mode: null, returnFocus: null }));

/** ⌘⇧P / ⌘P: opens that palette, switches to it from the other one, or closes it when it is the one open. */
export function togglePalette(mode: PaletteMode): void {
  const { mode: current, returnFocus } = usePalette.getState();
  if (current === mode) usePalette.setState({ mode: null });
  else usePalette.setState({ mode, returnFocus: current ? returnFocus : document.activeElement });
}

export function closePalette(): void {
  usePalette.setState({ mode: null });
}
