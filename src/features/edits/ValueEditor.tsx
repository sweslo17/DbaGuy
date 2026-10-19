import { deleteCharBackwardStrict, insertNewline } from "@codemirror/commands";
import { searchPanelOpen } from "@codemirror/search";
import { Prec, type Extension, type StateCommand } from "@codemirror/state";
import { EditorView, placeholder as placeholderText, type Command } from "@codemirror/view";
import { useEffect, useLayoutEffect, useMemo, useRef, type FocusEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta, EditKind } from "@/lib/api/types";
import { useAppSettings } from "@/stores/settings";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { STABLE_SCROLLBAR } from "../editor/extensions";
import { DrawerFrame } from "../grid/DrawerFrame";
import type { EditEnd } from "./CellEditor";
import { setNullEnds, type SetNullAnswer } from "./editing";
import { valueEditorKey, type ValueEditorKey } from "./editKeys";

export interface ValueEditorProps {
  column: ColumnMeta;
  kind: EditKind;
  initialText: string;
  placeholder: string | null;
  /** "Was: …", a loaded row's value before the edit; null for a new row. */
  was: string | null;
  nullable: boolean;
  onDone: (text: string | null, how: Extract<EditEnd, "commit" | "blur" | "save">) => void;
  onCancel: () => void;
  /** Stages NULL. Answer `false` to refuse it (a NOT NULL column): the editor then stays, with what was typed (K16). */
  onSetNull: () => SetNullAnswer;
}

/** A tab character, exactly (CodeMirror's Tab inserts the indent unit, two spaces). */
const insertLiteralTab: StateCommand = ({ state, dispatch }) => {
  dispatch(state.update(state.replaceSelection("\t"), { scrollIntoView: true, userEvent: "input" }));
  return true;
};

/** What the keys of a plain-text value do to the text: exactly what they say. */
const TYPING: Partial<Record<ValueEditorKey, Command>> = {
  newline: insertNewline,
  tab: insertLiteralTab,
  backspace: deleteCharBackwardStrict,
};

/**
 * The drawer's value editor (spec ⑤ §3.6.3): CodeMirror, JSON for a json column (with the language's own auto-closing
 * and indentation) and plain text otherwise, for JSON, text with line breaks or over 1,000 characters. A plain-text
 * value is typed exactly as the keys say: no bracket is ever added (H10), Return inserts a line break and Tab a tab
 * character, neither with indentation. ⌘↵ or Apply stages, Escape or Cancel drop the edit, Set NULL (⌥⌘N too) stages
 * NULL (unless the owner refuses it), ⌘S stages and opens the preview. Leaving the drawer for the grid stages what was
 * typed and closes it, as the inline editor does. Every one of these keys goes through `valueEditorKey`, which leaves
 * an input method's keys alone, and `DrawerFrame` skips its Escape for them (M4). With vim key bindings on, Escape is
 * vim's: it never cancels from inside the editor (K15), only Cancel, or Escape on one of the drawer's buttons, does.
 */
export function ValueEditor({
  column,
  kind,
  initialText,
  placeholder,
  was,
  nullable,
  onDone,
  onCancel,
  onSetNull,
}: ValueEditorProps) {
  const editor = useRef<CodeEditorHandle>(null);
  const vim = useAppSettings().settings.editor.vim;
  const touched = useRef(false);
  const done = useRef(false);
  const typing = useRef(false);
  const lastText = useRef<string | null>(null);
  const actions = useRef({ onDone, onCancel, onSetNull });
  const mode = useRef({ plain: kind !== "json", vim });
  const leaving = useRef(false);
  useLayoutEffect(() => {
    actions.current = { onDone, onCancel, onSetNull };
    mode.current = { plain: kind !== "json", vim };
  });
  // The focused content leaving the page is not the user leaving it. It happens when the editor unmounts (its row left
  // the rows shown) and, in development, when StrictMode unmounts and mounts it again; Chrome reports it as a blur, and
  // ending the edit then would lose what was typed or end an edit nobody has started. The unmount effect below stages
  // what was typed. This cleanup runs before CodeEditor's, which removes the page's nodes.
  useLayoutEffect(() => {
    leaving.current = false;
    return () => {
      leaving.current = true;
    };
  }, []);
  // The ways it ends. They read only refs, so one stable set serves the buttons and the (memoized) key handler.
  // Staging happens once (`finish`); Cancel always reaches the owner, who closes the drawer; Set NULL ends the edit
  // unless the owner refuses it (K16), and ends it first, so a blur its focus change causes stages nothing over NULL.
  const end = useMemo(
    () => ({
      finish(how: "commit" | "blur" | "save") {
        if (done.current) return;
        done.current = true;
        actions.current.onDone(touched.current ? (editor.current?.getText() ?? null) : null, how);
      },
      cancel() {
        done.current = true;
        actions.current.onCancel();
      },
      setNull() {
        done.current = true;
        if (!setNullEnds(actions.current.onSetNull())) done.current = false;
      },
    }),
    [],
  );
  const extensions = useMemo<Extension[]>(
    () => [
      Prec.highest(
        // The editor's keys, ahead of CodeMirror's own, behind the input-method check. ⌥⌘N is read by the physical
        // key, as in the grid: ⌥N types a dead key on a Mac layout, which a keymap's key name would miss.
        EditorView.domEventHandlers({
          keydown: (event, view) => {
            const seenBefore = typing.current;
            if (!event.repeat) typing.current = true;
            const searching = searchPanelOpen(view.state);
            const action = valueEditorKey(event, { ...mode.current, searching, typing: seenBefore });
            if (!action) return false;
            if (action === "apply") end.finish("commit");
            else if (action === "save") end.finish("save");
            else if (action === "cancel") end.cancel();
            else if (action === "setNull") end.setNull();
            else TYPING[action]?.(view);
            return true;
          },
        }),
      ),
      EditorView.lineWrapping,
      STABLE_SCROLLBAR,
      ...(placeholder ? [placeholderText(placeholder)] : []),
    ],
    [placeholder, end],
  );
  useEffect(() => {
    editor.current?.focus();
  }, []);
  // Unmounted while something typed is still in it (its row left the rows shown, H12): staged, never lost. The editor's
  // own unmount hands its text over first (CodeEditor's `onUnmount` runs in a layout cleanup, before this one).
  useEffect(
    () => () => {
      if (!done.current && touched.current) actions.current.onDone(lastText.current, "blur");
    },
    [],
  );
  // Focus going to the grid (or anywhere outside the drawer, while the window has it) ends the edit, staging it.
  const onBlur = (e: FocusEvent<HTMLElement>) => {
    if (leaving.current || !document.hasFocus() || e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    end.finish("blur");
  };
  // Under vim an Escape that vim leaves unhandled (normal mode, after `d`) must not reach DrawerFrame's close.
  const vimKeepsEscape = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape" && editor.current?.view.dom.contains(e.target as Node)) e.preventDefault();
  };
  return (
    <div className="h-full" onBlur={onBlur}>
      <DrawerFrame
        label={`Edit ${column.name}`}
        title={
          <>
            <span className="truncate font-mono font-semibold" data-selectable>
              {column.name}
            </span>
            <span className="shrink-0 text-[11px] text-muted">{column.type_name}</span>
          </>
        }
        toolbar={
          <>
            {was && (
              <span className="min-w-0 flex-1 truncate text-muted" title={was} data-selectable>
                {was}
              </span>
            )}
            <span className="ml-auto flex gap-1">
              <Button
                size="sm"
                onClick={end.setNull}
                disabled={!nullable}
                title={nullable ? "Set NULL (⌥⌘N)" : "This column is NOT NULL"}
              >
                Set NULL
              </Button>
              <Button size="sm" variant="ghost" onClick={end.cancel} title="Cancel (Esc)">
                Cancel
              </Button>
              <Button size="sm" variant="primary" onClick={() => end.finish("commit")} title="Apply (⌘↵)">
                Apply
              </Button>
            </span>
          </>
        }
        onKeyDown={vim ? vimKeepsEscape : undefined}
        onClose={end.cancel}
      >
        <div className="h-full min-h-40">
          <CodeEditor
            ref={editor}
            initialText={initialText}
            dialect={null}
            language={kind === "json" ? "json" : "text"}
            onChange={() => {
              touched.current = true;
            }}
            onUnmount={(content) => {
              lastText.current = content.text;
            }}
            extensions={extensions}
          />
        </div>
      </DrawerFrame>
    </div>
  );
}
