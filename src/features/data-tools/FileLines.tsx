import { memo, useEffect, useMemo, useRef } from "react";
import { cn } from "@/lib/cn";
import { numberedLines } from "./runForm";

/**
 * Lines of a file, numbered from `firstLine` (ruling V10: plain numbered lines, not an editor): the inspection's first
 * lines, or an error's place (`mark`: its line, tinted and scrolled to the middle as it shows). A fixed-height box that
 * scrolls both ways (long INSERT lines stay one line each), with room kept for the scrollbars.
 */
export const FileLines = memo(function FileLines({
  text,
  firstLine,
  mark,
  className,
  label = "The file's lines",
}: {
  text: string;
  firstLine: number;
  mark?: number;
  className?: string;
  label?: string;
}) {
  const lines = useMemo(() => numberedLines(text, firstLine), [text, firstLine]);
  // The numbers' column: w-14, wider when the last number needs it (line 1,234,567 of a dump)
  const digits = String(lines.at(-1)?.number ?? firstLine).length;
  const numberWidth = { width: `max(3.5rem, calc(${digits}ch + 0.5rem))` };
  const marked = useRef<HTMLDivElement>(null);
  useEffect(() => {
    marked.current?.scrollIntoView({ block: "center", inline: "nearest" });
  }, [mark, text]);
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className={cn(
        "max-h-56 overflow-auto rounded-md border border-border bg-panel font-mono text-[12px] outline-none [scrollbar-gutter:stable] focus-visible:ring-2 focus-visible:ring-accent/50",
        className,
      )}
    >
      {/* pb-3: the last line clears a horizontal scrollbar drawn inside the box (classic scrollbars, 06b F-c) */}
      <div className="w-max min-w-full pt-1 pb-3">
        {lines.map((line) => (
          <div
            key={line.number}
            ref={line.number === mark ? marked : undefined}
            className={cn("flex leading-[18px]", line.number === mark && "bg-danger/15")}
            aria-current={line.number === mark ? "true" : undefined}
          >
            <span className="shrink-0 pr-2 text-right text-muted select-none" style={numberWidth}>
              {line.number}
            </span>
            <span className="pr-3 whitespace-pre" data-selectable>
              {line.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
});
