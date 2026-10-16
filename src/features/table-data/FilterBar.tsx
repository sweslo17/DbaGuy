import { History } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { cn } from "@/lib/cn";

interface FilterBarProps {
  /** The applied filter: the input shows it again whenever it changes, and Escape brings it back. */
  applied: string;
  /** Recent filters, newest first. */
  history: string[];
  /** Enter (or a recent filter): apply this text; empty clears the WHERE. */
  onApply: (text: string) => void;
  /** The rejected filter is no longer what the input shows: Escape brought the applied one back, or it was edited. */
  onClearError: () => void;
  onClearHistory: () => void;
  /** Shown as an error (red border): the last filter was rejected, or the browse failed. */
  invalid: boolean;
  /** The last filter entered was rejected: its text stays for editing, and the bar says what the rows still show. */
  rejected: boolean;
  /** Nothing can be applied now (an export runs on the session). */
  disabled?: boolean;
}

/** One-line WHERE input of the table data view, with its recent filters. */
export function FilterBar(props: FilterBarProps) {
  const { applied, history, onApply, onClearError, onClearHistory, invalid, rejected, disabled = false } = props;
  const [draft, setDraft] = useState(applied);
  const [shownApplied, setShownApplied] = useState(applied);
  if (applied !== shownApplied) {
    setShownApplied(applied);
    setDraft(applied);
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return; // Enter / Escape belong to the input method (e.g. Zhuyin)
    if (e.key === "Enter") {
      e.preventDefault();
      onApply(draft);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setDraft(applied);
      onClearError();
    }
  };
  const onChange = (text: string) => {
    setDraft(text);
    if (rejected) onClearError();
  };
  const items: MenuItem[] = history.length
    ? [
        ...history.map((filter) => ({
          label: filter,
          disabled,
          onSelect: () => {
            setDraft(filter);
            onApply(filter);
          },
        })),
        { label: "Clear recent filters", separatorBefore: true, onSelect: onClearHistory },
      ]
    : [{ label: "No recent filters", disabled: true }];

  return (
    <div
      className={cn(
        "flex h-6 min-w-0 flex-1 items-center rounded-md border bg-elevated font-mono text-[12px] focus-within:border-accent",
        invalid ? "border-danger" : "border-border",
        disabled && "opacity-60",
      )}
    >
      <span className="pr-1.5 pl-2 text-muted">WHERE</span>
      <input
        value={draft}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        disabled={disabled}
        aria-label="Filter (WHERE)"
        aria-invalid={invalid}
        placeholder="e.g. id > 100 AND name LIKE 'A%'  (Enter to apply)"
        className="h-full min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted/70"
      />
      {rejected && (
        <span
          className="max-w-[45%] shrink-0 truncate pl-1.5 font-sans text-[11px] text-muted"
          title={`Not applied — showing ${applied ? `WHERE ${applied}` : "all rows"}`}
        >
          Not applied — showing {applied ? <span className="font-mono">{applied}</span> : "all rows"}
        </span>
      )}
      <DropdownMenu items={items} align="end" className="max-w-[min(560px,80vw)] font-mono">
        <button
          type="button"
          title="Recent filters"
          className="flex h-full items-center px-1.5 text-muted hover:text-fg"
        >
          <History size={13} />
        </button>
      </DropdownMenu>
    </div>
  );
}
