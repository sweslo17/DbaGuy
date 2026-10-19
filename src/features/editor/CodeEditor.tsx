import { isolateHistory } from "@codemirror/commands";
import { Compartment, EditorSelection, EditorState, type Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, type Ref } from "react";
import type { Dialect } from "@/lib/api/types";
import { clamp } from "@/lib/math";
import { useAppSettings } from "@/stores/settings";
import {
  baseExtensions,
  editorOptions,
  reconfigureEffects,
  type EditorLanguage,
  type EditorOptions,
} from "./extensions";
import { startingPoint } from "./startingPoint";

export interface CodeEditorHandle {
  view: EditorView;
  getText(): string;
  /**
   * Replaces the whole text as the editor's starting content (a script read for its restored tab, a document loaded or
   * reloaded): it is not an undo step, and the undo and redo history is emptied, since CodeMirror would map the old
   * events through the replacement and Undo would put pieces of the old text into the new one. The selection is placed
   * as given (clamped), else at the start, and scrolled into view.
   */
  setText(text: string, selection?: { anchor: number; head: number }): void;
  focus(): void;
  /** Selects [from, to) (clamped to the text) and scrolls it into view. */
  selectRange(from: number, to: number): void;
  /**
   * Replaces the main selection with `text`, as is, in one undoable change (never merged with typing around it); the
   * cursor ends after it, in view, and the editor takes the focus.
   */
  insert(text: string): void;
  /**
   * Replaces the whole text (a formatter's result) as one undoable change of its own, never merged with typing around
   * it: undo brings the text back. The cursor goes to the start, in view.
   */
  replaceAll(text: string): void;
}

/** An editor's text and main selection. */
export interface EditorContent {
  text: string;
  selection: { anchor: number; head: number };
}

export interface CodeEditorProps {
  initialText: string;
  initialSelection?: { anchor: number; head: number };
  /** The SQL mode; null → generic SQL. Ignored unless `language` is "sql". */
  dialect: Dialect | null;
  /** What the text is: SQL (the default), JSON (a Couchbase document) or plain text (a cell's value). */
  language?: EditorLanguage;
  /** No typing (the text can still be set through the handle). */
  readOnly?: boolean;
  /** The text changed. Called on every keystroke, so it carries no text: read it (`getText`) only when needed. */
  onChange?: () => void;
  onSelectionChange?: (selection: { anchor: number; head: number }) => void;
  /**
   * The editor is being destroyed (unmounted, including by a crash): its final content, read just before. StrictMode's
   * mount check destroys a first editor too, so a receiver keeps it only until something newer arrives.
   */
  onUnmount?: (content: EditorContent) => void;
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

/**
 * A CodeMirror 6 editor for SQL, or for JSON (a Couchbase document). The `EditorView` is created once and lives as
 * long as the component, so undo history, selection and scroll survive while a hidden tab stays mounted. Settings,
 * dialect and language changes reconfigure it through compartments; it is never recreated.
 */
export function CodeEditor({
  initialText,
  initialSelection,
  dialect,
  language = "sql",
  readOnly = false,
  onChange,
  onSelectionChange,
  onUnmount,
  extensions = NONE,
  ref,
}: CodeEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const { settings } = useAppSettings();
  const editor = settings.editor;
  const options = useMemo(
    () => editorOptions(editor, dialect, readOnly, language),
    [editor, dialect, readOnly, language],
  );
  // What the view is configured with now: the reconfigure effects only swap what changed since.
  const applied = useRef<{ options: EditorOptions; extensions: Extension[] }>({ options, extensions });
  const latest = useRef({ initialText, initialSelection, options, extensions, onChange, onSelectionChange, onUnmount });
  useLayoutEffect(() => {
    latest.current = { initialText, initialSelection, options, extensions, onChange, onSelectionChange, onUnmount };
  });

  // Created once (twice under StrictMode's mount check, which destroys the first) from the props at mount time.
  useLayoutEffect(() => {
    const init = latest.current;
    const length = init.initialText.length;
    const selection = init.initialSelection
      ? EditorSelection.single(
          clamp(init.initialSelection.anchor, 0, length),
          clamp(init.initialSelection.head, 0, length),
        )
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
      try {
        // Read before anything is torn down: whatever order React unmounts in, the content is handed over.
        const { anchor, head } = view.state.selection.main;
        latest.current.onUnmount?.({ text: view.state.doc.toString(), selection: { anchor, head } });
      } finally {
        // Even if the receiver throws: the view and its listeners never outlive the component.
        view.destroy();
        if (viewRef.current === view) viewRef.current = null;
      }
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
        setText: (text, selection) => {
          const view = current();
          for (const spec of startingPoint(view.state, text, selection)) view.dispatch(spec);
        },
        focus: () => current().focus(),
        selectRange: (from, to) => {
          const view = current();
          const length = view.state.doc.length;
          view.dispatch({
            selection: EditorSelection.single(clamp(from, 0, length), clamp(to, 0, length)),
            scrollIntoView: true,
          });
        },
        insert: (text) => {
          const view = current();
          const { from, to } = view.state.selection.main;
          const insert = view.state.toText(text); // its length as the editor counts it (line breaks normalised)
          view.dispatch({
            changes: { from, to, insert },
            selection: EditorSelection.cursor(from + insert.length),
            scrollIntoView: true,
            userEvent: "input.paste", // its own undo step, like a paste
          });
          view.focus();
        },
        replaceAll: (text) => {
          const view = current();
          view.dispatch({
            changes: { from: 0, to: view.state.doc.length, insert: view.state.toText(text) },
            selection: EditorSelection.cursor(0),
            scrollIntoView: true,
            userEvent: "format",
            annotations: isolateHistory.of("full"), // its own undo step, never merged with typing around it
          });
        },
      };
    },
    [],
  );

  return <div ref={host} className="h-full min-h-0 overflow-hidden" />;
}
