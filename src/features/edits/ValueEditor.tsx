import { searchPanelOpen } from "@codemirror/search";
import { Prec, type Extension } from "@codemirror/state";
import { EditorView, keymap, placeholder as placeholderText } from "@codemirror/view";
import { useEffect, useLayoutEffect, useMemo, useRef, type FocusEvent } from "react";
import { Button } from "@/components/ui/Button";
import type { ColumnMeta, EditKind } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { CodeEditor, type CodeEditorHandle } from "../editor/CodeEditor";
import { STABLE_SCROLLBAR } from "../editor/extensions";
import { DrawerFrame } from "../grid/DrawerFrame";
import type { EditEnd } from "./CellEditor";
import { isSetNullKey } from "./editKeys";

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
  onSetNull: () => void;
}

/**
 * The drawer's value editor (spec ⑤ §3.6.3): CodeMirror, JSON for a json column and plain text otherwise (no bracket is
 * ever added, H10), for JSON, text with line breaks or over 1,000 characters. ⌘↵ or Apply stages, Return is a line
 * break, Escape or Cancel drop the edit, Set NULL (⌥⌘N too) stages NULL, ⌘S stages and opens the preview. Leaving the
 * drawer for the grid stages what was typed and closes it, as the inline editor does. CodeMirror's keymaps skip an
 * input method's keys, and so does the one handler added here (⌥⌘N); `DrawerFrame` skips its Escape for them (M4).
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
  const touched = useRef(false);
  const done = useRef(false);
  const lastText = useRef<string | null>(null);
  const actions = useRef({ onDone, onCancel, onSetNull });
  useLayoutEffect(() => {
    actions.current = { onDone, onCancel, onSetNull };
  });
  // The ways it ends. They read only refs, so one stable set serves the buttons and the (memoized) key handlers.
  // Staging happens once (`finish`); Cancel and Set NULL always reach the owner, who closes the drawer.
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
        actions.current.onSetNull();
      },
    }),
    [],
  );
  const extensions = useMemo<Extension[]>(
    () => [
      Prec.highest([
        keymap.of([
          { key: "Mod-Enter", run: () => (end.finish("commit"), true) },
          { key: "Mod-s", run: () => (end.finish("save"), true) },
          // With the search panel open, Escape is the panel's (it closes), not the edit's.
          { key: "Escape", run: (view) => !searchPanelOpen(view.state) && (end.cancel(), true) },
        ]),
        // ⌥⌘N by the physical key, as in the grid: ⌥N types a dead key on a Mac layout, which a keymap's name misses.
        EditorView.domEventHandlers({
          keydown: (event) => {
            if (isImeKeyEvent(event) || event.repeat || !isSetNullKey(event)) return false;
            end.setNull();
            return true;
          },
        }),
      ]),
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
    if (!document.hasFocus() || e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    end.finish("blur");
  };
  return (
    <div className="h-full" onBlur={onBlur}>
      <DrawerFrame
        label="Value editor"
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
