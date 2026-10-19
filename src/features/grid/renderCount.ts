/*
 * Dev only (spec ⑤ §7, Q1): the indexes of the body rows rendered, so that "staging one cell re-renders only its row"
 * can be measured in the browser: set `window.__gridRowRenders = []`, stage a cell, read it back. Production builds
 * drop it (`import.meta.env.DEV` is false there). StrictMode renders twice in dev: count distinct indexes.
 */
declare global {
  // `window` is `globalThis` in a browser; the counter is read off `globalThis` so the module also runs without a DOM.
  var __gridRowRenders: number[] | undefined;
}

export function countRowRender(index: number): void {
  if (!import.meta.env.DEV) return;
  globalThis.__gridRowRenders?.push(index);
}
