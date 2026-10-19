import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { INVALID_HINT, slotNames, valueKind, type ParamSlots } from "./params";

export interface ParamsPanelProps {
  slots: ParamSlots;
  /** The tab's drafts by field name (`$x`, `$1`): the text as typed. */
  drafts: Record<string, string>;
  open: boolean;
  /** A field to focus (a run was refused because of it): the panel is open, so it exists. Cleared by the owner. */
  focus: string | null;
  onToggle: () => void;
  onChange: (name: string, text: string) => void;
}

interface ParamRowProps {
  name: string;
  value: string;
  /** Prefix of the ids that tie the label and the hint to the input. */
  htmlId: string;
  onChange: (name: string, text: string) => void;
  register: (name: string, input: HTMLInputElement | null) => void;
}

/**
 * One field: the name, a one-line input for a JSON literal, and what the text is. While an input method composes
 * (Zhuyin), the text stays here: nothing is reported, checked or saved until the composition ends.
 */
const ParamRow = memo(function ParamRow({ name, value, htmlId, onChange, register }: ParamRowProps) {
  const [composing, setComposing] = useState<string | null>(null);
  const kind = useMemo(() => valueKind(value), [value]);
  const invalid = kind === "invalid";
  return (
    <Fragment>
      <label htmlFor={htmlId} className="truncate font-mono text-[12px] text-muted" title={name}>
        {name}
      </label>
      <input
        id={htmlId}
        ref={(input) => register(name, input)}
        value={composing ?? value}
        placeholder={'JSON, e.g. 42 or "text"'}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        autoComplete="off"
        aria-invalid={invalid}
        aria-describedby={invalid ? `${htmlId}-hint` : undefined}
        onChange={(event) => {
          const { value: text } = event.target;
          if ((event.nativeEvent as InputEvent).isComposing) setComposing(text);
          else onChange(name, text);
        }}
        onCompositionEnd={(event) => {
          setComposing(null);
          onChange(name, event.currentTarget.value);
        }}
        className={cn(
          "h-6 min-w-0 rounded border border-border bg-elevated px-1.5 font-mono text-[12px] outline-none",
          "placeholder:text-muted/70 focus:border-accent aria-invalid:border-danger",
        )}
      />
      <span className="justify-self-start">
        {kind !== "empty" && (
          <span
            className={cn(
              "rounded border px-1 text-[10px] leading-4",
              invalid ? "border-danger/50 text-danger" : "border-border text-muted",
            )}
          >
            {kind}
          </span>
        )}
      </span>
      {invalid && (
        <span id={`${htmlId}-hint`} className="col-start-2 col-span-2 text-[11px] text-danger">
          {INVALID_HINT}
        </span>
      )}
    </Fragment>
  );
});

/**
 * The parameters of the editor's statements (spec §3.11.2): a bar `Parameters (n)` at the bottom of the editor pane
 * that opens and closes the fields, one per named parameter and per position, in the order the statements use them.
 * The owner decides when it is shown (the engine takes parameters and a statement uses one) and keeps the drafts.
 */
export function ParamsPanel({ slots, drafts, open, focus, onToggle, onChange }: ParamsPanelProps) {
  const names = slotNames(slots);
  const base = useId();
  const inputs = useRef(new Map<string, HTMLInputElement>());
  const register = useCallback((name: string, input: HTMLInputElement | null) => {
    if (input) inputs.current.set(name, input);
    else inputs.current.delete(name);
  }, []);
  useEffect(() => {
    if (open && focus !== null) inputs.current.get(focus)?.focus();
  }, [open, focus]);

  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="flex max-h-[45%] shrink-0 flex-col border-t border-border bg-panel">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex h-6 shrink-0 items-center gap-1 px-2 text-left text-[12px] text-muted outline-none hover:bg-hover focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:ring-inset"
      >
        <Chevron size={12} className="shrink-0" />
        {`Parameters (${names.length})`}
      </button>
      {open && (
        <div className="grid min-h-0 grid-cols-[fit-content(12rem)_minmax(0,1fr)_4.5rem] items-center gap-x-2 gap-y-1 overflow-y-auto px-2 pt-0.5 pb-2">
          {names.map((name, index) => (
            <ParamRow
              key={name}
              name={name}
              value={drafts[name] ?? ""}
              htmlId={`${base}-${index}`}
              onChange={onChange}
              register={register}
            />
          ))}
        </div>
      )}
    </div>
  );
}
