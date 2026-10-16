import { Compartment, EditorSelection, EditorState, type Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, type Ref } from "react";
import type { Dialect } from "@/lib/api/types";
import { useAppSettings } from "@/stores/settings";
import { baseExtensions, editorOptions, reconfigureEffects, type EditorOptions } from "./extensions";

export interface CodeEditorHandle {
  view: EditorView;
  getText(): string;
  /** Replaces the whole text as one undoable change. */
  setText(text: string): void;
  focus(): void;
  /** Selects [from, to) (clamped to the text) and scrolls it into view. */
  selectRange(from: number, to: number): void;
}

export interface CodeEditorProps {
  initialText: string;
  initialSelection?: { anchor: number; head: number };
  /** The language mode; null → generic SQL. */
  dialect: Dialect | null;
  /** The text changed. Called on every keystroke, so it carries no text: read it (`getText`) only when needed. */
  onChange?: () => void;
  onSelectionChange?: (selection: { anchor: number; head: number }) => void;
  /**
   * Tab-specific extensions (run keymap, completion, statements, diagnostics…). They come before the base ones, so
   * their keymaps win at equal precedence. Keep the array stable (memoize it): a new array reconfigures the editor.
   */
  extensions?: Extension[];
  ref?: Ref<CodeEditorHandle>;
}

/** The caller's extensions live in their own compartment (one per editor state, so a shared instance is fine). */
const callerExtensions = new Compartment();
const NONE: Extension[] = [];

const clamp = (pos: number, length: number) => Math.min(Math.max(0, pos), length);

/**
 * A CodeMirror 6 SQL editor. The `EditorView` is created once and lives as long as the component, so undo history,
 * selection and scroll survive while a hidden tab stays mounted. Settings and dialect changes reconfigure it through
 * compartments; it is never recreated.
 */
export function CodeEditor({
  initialText,
  initialSelection,
  dialect,
  onChange,
  onSelectionChange,
  extensions = NONE,
  ref,
}: CodeEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const { settings } = useAppSettings();
  const editor = settings.editor;
  const options = useMemo(() => editorOptions(editor, dialect), [editor, dialect]);
  // What the view is configured with now: the reconfigure effects only swap what changed since.
  const applied = useRef<{ options: EditorOptions; extensions: Extension[] }>({ options, extensions });
  const latest = useRef({ initialText, initialSelection, options, extensions, onChange, onSelectionChange });
  useLayoutEffect(() => {
    latest.current = { initialText, initialSelection, options, extensions, onChange, onSelectionChange };
  });

  // Created once (twice under StrictMode's mount check, which destroys the first) from the props at mount time.
  useLayoutEffect(() => {
    const init = latest.current;
    const length = init.initialText.length;
    const selection = init.initialSelection
      ? EditorSelection.single(clamp(init.initialSelection.anchor, length), clamp(init.initialSelection.head, length))
      : undefined;
    const listener = EditorView.updateListener.of((update) => {
      const { onChange: changed, onSelectionChange: selected } = latest.current;
      if (update.docChanged) changed?.();
      if (update.selectionSet && selected) {
        const { anchor, head } = update.state.selection.main;
        selected({ anchor, head });
      }
    });
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: init.initialText,
        selection,
        extensions: [callerExtensions.of(init.extensions), baseExtensions(init.options), listener],
      }),
      scrollTo: selection ? EditorView.scrollIntoView(selection.main) : undefined,
    });
    viewRef.current = view;
    applied.current = { options: init.options, extensions: init.extensions };
    return () => {
      view.destroy();
      if (viewRef.current === view) viewRef.current = null;
    };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const effects = reconfigureEffects(applied.current.options, options);
    if (extensions !== applied.current.extensions) effects.push(callerExtensions.reconfigure(extensions));
    applied.current = { options, extensions };
    if (effects.length) view.dispatch({ effects });
  }, [options, extensions]);

  useImperativeHandle(
    ref,
    () => {
      const current = () => {
        if (!viewRef.current) throw new Error("The editor is not mounted");
        return viewRef.current;
      };
      return {
        get view() {
          return current();
        },
        getText: () => current().state.doc.toString(),
        setText: (text) => {
          const view = current();
          view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
        },
        focus: () => current().focus(),
        selectRange: (from, to) => {
          const view = current();
          const length = view.state.doc.length;
          view.dispatch({ selection: EditorSelection.single(clamp(from, length), clamp(to, length)), scrollIntoView: true });
        },
      };
    },
    [],
  );

  return <div ref={host} className="h-full min-h-0 overflow-hidden" />;
}
