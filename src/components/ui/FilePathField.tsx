import type { KeyboardEvent, Ref } from "react";
import { isImeKeyEvent } from "@/lib/ime";
import { canChooseFiles } from "@/lib/native";
import { Button } from "./Button";
import { FieldRow, Input } from "./Field";

/**
 * A file path: the system panel's Choose… in the app, a typed absolute path in browser mode (the backend reads or writes
 * it on this Mac). The export, the dump and Run SQL file share it. `onEnter`: Return in the field (never during an
 * input method's composition); `onChosen`: the panel gave a path (null: cancelled, nothing changes), after `onChange`.
 * `autoFocus`: the field takes the focus as it mounts: the input in browser mode, Choose… in the app (there the panel is
 * how a path is given).
 */
export function FilePathField({
  label = "File",
  value,
  onChange,
  onEnter,
  onBlur,
  choose,
  onChosen,
  placeholder,
  browserHint,
  error,
  inputRef,
  autoFocus = false,
}: {
  label?: string;
  value: string;
  onChange: (path: string) => void;
  onEnter?: () => void;
  onBlur?: () => void;
  choose: () => Promise<string | null>;
  onChosen?: (path: string) => void;
  placeholder: string;
  browserHint: string;
  error?: string | null;
  inputRef?: Ref<HTMLInputElement>;
  autoFocus?: boolean;
}) {
  const choosable = canChooseFiles();
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || isImeKeyEvent(e)) return;
    e.preventDefault();
    onEnter?.();
  };
  const pick = async () => {
    const chosen = await choose();
    if (chosen) {
      onChange(chosen);
      onChosen?.(chosen);
    }
  };
  return (
    <FieldRow label={label} hint={choosable ? undefined : browserHint} error={error}>
      <div className="flex gap-2">
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onBlur={onBlur}
          placeholder={choosable ? "Choose a file…" : placeholder}
          aria-label="File path"
          aria-invalid={!!error}
          autoFocus={autoFocus && !choosable}
          className="font-mono"
        />
        {choosable && (
          <Button className="shrink-0" autoFocus={autoFocus} onClick={() => void pick()}>
            Choose…
          </Button>
        )}
      </div>
    </FieldRow>
  );
}
