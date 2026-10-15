import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:brightness-110 disabled:opacity-50",
  secondary: "bg-elevated border border-border hover:bg-hover disabled:opacity-50",
  ghost: "hover:bg-hover disabled:opacity-40",
  danger: "bg-danger text-white hover:brightness-110 disabled:opacity-50",
};
const SIZES: Record<Size, string> = { sm: "h-6 px-2 text-[12px]", md: "h-7 px-3 text-[13px]" };

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", className, type = "button", ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md font-medium outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    />
  );
});
