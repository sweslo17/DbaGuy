import type { KeyboardEventHandler } from "react";
import { FieldRow, Input } from "@/components/ui/Field";
import { numberProblem, type NumberField } from "./settingsForm";

/**
 * A whole-number box with its unit, for a field of `settingsForm`'s table; the range problem replaces the hint while
 * there is one. The Settings dialog and the Sample fields dialog both show their sample size through it.
 */
export function NumberFieldRow({
  field,
  text,
  onChange,
  autoFocus,
  onKeyDown,
}: {
  field: NumberField;
  text: string;
  onChange: (text: string) => void;
  autoFocus?: boolean;
  onKeyDown?: KeyboardEventHandler<HTMLInputElement>;
}) {
  const problem = numberProblem(field, text);
  return (
    <FieldRow label={field.label} hint={field.hint} error={problem}>
      <div className="flex items-center gap-2">
        <div className="w-24 shrink-0">
          <Input
            type="number"
            inputMode="numeric"
            min={field.min}
            max={field.max}
            step={1}
            aria-invalid={problem !== null}
            aria-label={field.label}
            autoFocus={autoFocus}
            value={text}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
          />
        </div>
        {field.unit && <span className="text-[12px] text-muted">{field.unit}</span>}
      </div>
    </FieldRow>
  );
}
