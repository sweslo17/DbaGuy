import {
  forwardRef,
  type InputHTMLAttributes,
  type LabelHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

const control =
  "rounded-md border border-border bg-elevated px-2 text-[13px] outline-none focus:border-accent disabled:opacity-60";
const singleLine = `h-7 w-full ${control}`;

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...props },
  ref,
) {
  return (
    <input
      ref={ref}
      spellCheck={false}
      autoCorrect="off"
      autoCapitalize="off"
      className={cn(singleLine, className)}
      {...props}
    />
  );
});

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea spellCheck={false} className={cn("w-full py-1.5", control, className)} {...props} />;
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(singleLine, "pr-6", className)} {...props}>
      {children}
    </select>
  );
}

export function Checkbox({ label, ...props }: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label className="inline-flex items-center gap-2 text-[13px]">
      <input type="checkbox" className="accent-[var(--accent)]" {...props} />
      {label}
    </label>
  );
}

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("text-[12px] text-muted", className)} {...props} />;
}

export function FieldRow({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label || undefined} className="grid grid-cols-[120px_1fr] items-center gap-x-3 gap-y-1">
      <Label className="text-right">{label}</Label>
      <div>{children}</div>
      {hint && <span className="col-start-2 text-[11px] text-muted [overflow-wrap:anywhere]">{hint}</span>}
    </div>
  );
}
