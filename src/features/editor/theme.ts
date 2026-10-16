import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Compartment, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

/*
 * Every colour is a CSS token from `src/styles.css`, so light and dark mode follow `prefers-color-scheme` without
 * reconfiguring the editor. Only CodeMirror's own base styles (for parts this theme does not restyle) need to know the
 * scheme: `followColorScheme` keeps `EditorView.darkTheme` in step with it.
 */

const chrome = EditorView.theme({
  "&": { height: "100%", color: "var(--fg)", backgroundColor: "var(--bg)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono)" },
  // The app disables text selection outside inputs; the editor must stay selectable (WebKit honours the inherited value).
  ".cm-content": { caretColor: "var(--fg)", cursor: "text", userSelect: "text", WebkitUserSelect: "text" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--fg)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: "var(--selected)" },
  ".cm-activeLine": { backgroundColor: "var(--hover)" },
  ".cm-gutters": { backgroundColor: "var(--bg)", color: "var(--muted)", border: "none" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 8px 0 12px" },
  ".cm-activeLineGutter": { backgroundColor: "var(--hover)", color: "var(--fg)" },
  "&.cm-focused .cm-matchingBracket": { backgroundColor: "var(--selected)", outline: "1px solid var(--border)" },
  "&.cm-focused .cm-nonmatchingBracket": { color: "var(--danger)" },
  ".cm-selectionMatch": { backgroundColor: "var(--hover)", outline: "1px solid var(--border)" },
  ".cm-searchMatch": { backgroundColor: "transparent", outline: "1px solid var(--warning)" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "var(--selected)", outline: "1px solid var(--accent)" },
  ".cm-placeholder": { color: "var(--muted)" },
  // Search panel and later tooltips (completion, lint, describe).
  ".cm-panels": { backgroundColor: "var(--panel)", color: "var(--fg)", fontFamily: "var(--font-sans)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
  ".cm-panel.cm-search": { fontSize: "12px", padding: "4px 8px" },
  ".cm-panel.cm-search label": { color: "var(--muted)" },
  ".cm-textfield": {
    backgroundColor: "var(--bg)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "4px",
    outline: "none",
  },
  ".cm-textfield:focus": { borderColor: "var(--accent)" },
  ".cm-button": {
    backgroundImage: "none",
    backgroundColor: "var(--elevated)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "4px",
  },
  ".cm-button:hover": { backgroundColor: "var(--hover)" },
  ".cm-tooltip": {
    backgroundColor: "var(--elevated)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "6px",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--selected)", color: "var(--fg)" },
  // Vim's block cursor (its own theme is red and Prec.highest: win by specificity).
  "&.cm-editor .cm-fat-cursor": { background: "var(--accent)", color: "var(--accent-fg)" },
  "&.cm-editor:not(.cm-focused) .cm-fat-cursor": { background: "none", outline: "1px solid var(--accent)" },
});

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.operatorKeyword, t.modifier, t.controlKeyword, t.definitionKeyword], color: "var(--accent)" },
  { tag: [t.typeName, t.standard(t.typeName)], color: "var(--accent)", fontStyle: "italic" },
  { tag: t.standard(t.name), fontStyle: "italic" }, // built-in functions
  { tag: t.string, color: "var(--success)" },
  { tag: t.special(t.string), color: "var(--fg)" }, // quoted identifiers ("Order Items")
  { tag: [t.number, t.bool, t.null, t.atom], color: "var(--warning)" },
  { tag: t.comment, color: "var(--muted)", fontStyle: "italic" },
  { tag: t.special(t.name), color: "var(--danger)" }, // parameters and variables ($1, :name, @var)
  { tag: [t.punctuation, t.paren, t.brace, t.squareBracket, t.separator], color: "var(--muted)" },
  { tag: t.invalid, color: "var(--danger)" },
]);

const scheme = new Compartment();
const DARK = "(prefers-color-scheme: dark)";
const prefersDark = () => window.matchMedia(DARK).matches;

/** Reconfigures `EditorView.darkTheme` when the system switches between light and dark. */
const followColorScheme = ViewPlugin.define((view) => {
  const query = window.matchMedia(DARK);
  const onChange = () => view.dispatch({ effects: scheme.reconfigure(EditorView.darkTheme.of(query.matches)) });
  query.addEventListener("change", onChange);
  return { destroy: () => query.removeEventListener("change", onChange) };
});

/** The editor's look: token-based chrome, syntax colours and the current colour scheme for base styles. */
export function editorTheme(): Extension {
  return [chrome, syntaxHighlighting(highlight), scheme.of(EditorView.darkTheme.of(prefersDark())), followColorScheme];
}

/** The editor's font size (a live setting, kept in its own compartment). */
export function fontSizeTheme(px: number): Extension {
  return EditorView.theme({ "&": { fontSize: `${px}px` } });
}
