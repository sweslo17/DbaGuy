import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { canvasMeasure } from "../grid/columns";
import { cellEditorKey } from "./editKeys";

/** The inline editor never grows wider than this (spec ⑤ §3.6.3). */
export const INPUT_MAX_WIDTH = 480;
/** Its horizontal padding (`px-2`) and border, which the measured text needs room for. */
const INPUT_PADDING = 18;

/** How an edit ended: the editor's text is staged first unless it was untouched (`text` null). */
export type EditEnd = "commit" | "next" | "previous" | "blur" | "save";

export interface CellEditorProps {
  initialText: string;
  /** Shown while it is empty (NULL, DEFAULT, or what typing replaces). */
  placeholder: string | null;
  /** The cell's width: the input is never narrower, and grows with its text up to INPUT_MAX_WIDTH. */
  width: number;
  /** A number column: the text is aligned right, as the grid aligns it. */
  numeric: boolean;
  /** `text` is null when nothing was typed (H4: then nothing is staged). */
  onDone: (text: string | null, how: EditEnd) => void;
  onCancel: () => void;
  onSetNull: () => void;
}

/**
 * The inline cell editor (spec ⑤ §3.6.3): a real `<input>` over the cell, so an input method composes in it. Return
 * stages and closes, Tab / ⇧Tab stage and move along the row, Escape is the only way to drop what was typed, and
 * leaving it (a click elsewhere) stages too, as Numbers does. Every key goes through `cellEditorKey`, which leaves an
 * input method's keys alone. The pointer, a double click and the context menu stay with the input, not the grid.
 */
export function CellEditor({ initialText, placeholder, width, numeric, onDone, onCancel, onSetNull }: CellEditorProps) {
  const [text, setText] = useState(initialText);
  const input = useRef<HTMLInputElement>(null);
  const touched = useRef(false);
  const done = useRef(false);
  const latest = useRef({ text, onDone });
  useLayoutEffect(() => {
    latest.current = { text, onDone };
  });
  useLayoutEffect(() => {
    input.current?.focus({ preventScroll: true });
    input.current?.select();
  }, []);
  // Unmounted while something typed is still in it (its row left the rows shown, H12): it is staged, never lost.
  useEffect(
    () => () => {
      if (!done.current && touched.current) latest.current.onDone(latest.current.text, "blur");
    },
    [],
  );

  const finish = (how: EditEnd) => {
    if (done.current) return;
    done.current = true;
    onDone(touched.current ? text : null, how);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    const action = cellEditorKey(e.nativeEvent);
    if (!action) return;
    e.preventDefault();
    e.stopPropagation();
    if (action === "cancel") {
      done.current = true;
      onCancel();
    } else if (action === "setNull") {
      done.current = true;
      onSetNull();
    } else finish(action);
  };
  const measured = canvasMeasure()(text || placeholder || "", "cell") + INPUT_PADDING;
  return (
    <input
      ref={input}
      value={text}
      placeholder={placeholder ?? undefined}
      spellCheck={false}
      autoComplete="off"
      aria-label="Cell value"
      onChange={(e) => {
        touched.current = true;
        setText(e.target.value);
      }}
      onKeyDown={onKeyDown}
      // Leaving the window is not leaving the cell: the editor is still there when the window comes back.
      onBlur={() => document.hasFocus() && finish("blur")}
      onPointerDown={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      className={`h-[22px] rounded-none border border-accent bg-elevated px-2 font-mono text-[12px] text-fg outline-none placeholder:text-muted placeholder:italic ${numeric ? "text-right" : ""}`}
      style={{ width: Math.min(INPUT_MAX_WIDTH, Math.max(width, measured)) }}
    />
  );
}
