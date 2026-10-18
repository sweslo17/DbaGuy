import type { WheelEvent } from "react";

/**
 * `onWheel` of a strip that scrolls sideways without a scrollbar: a vertical wheel (a mouse's) scrolls it along; a
 * horizontal swipe (a trackpad's) already does, natively, so it is left alone.
 */
export function scrollSidewaysOnWheel(e: WheelEvent<HTMLElement>): void {
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY;
}

/** Scrolls the strip that holds `el` just far enough to show it (smoothly, unless the user asked for less motion). */
export function revealInStrip(el: HTMLElement): void {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
}
