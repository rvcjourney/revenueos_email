import * as React from "react";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "outline" | "ghost" | "danger";
type Size = "default" | "sm" | "icon";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  asChild?: false;
  variant?: Variant;
  size?: Size;
  // Shows a spinner and disables the button. The caller still owns the
  // mutation state; this only renders it.
  loading?: boolean;
};

type ButtonAsChildProps = {
  asChild: true;
  children: React.ReactElement<{ className?: string }>;
  className?: string;
  variant?: Variant;
  size?: Size;
};

type Props = ButtonProps | ButtonAsChildProps;

const variants: Record<Variant, string> = {
  primary:
    "bg-brand-600 text-white shadow-[0_1px_2px_rgb(44_39_128/0.3)] hover:bg-brand-700 active:bg-brand-800",
  secondary: "bg-brand-50 text-brand-700 hover:bg-brand-100 active:bg-brand-100",
  outline:
    "border border-slate-200 bg-white text-slate-700 shadow-[0_1px_2px_rgb(15_23_42/0.04)] hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 active:bg-slate-100",
  ghost: "text-slate-600 hover:bg-slate-100 hover:text-slate-900 active:bg-slate-200",
  danger: "bg-red-600 text-white shadow-sm hover:bg-red-700 active:bg-red-800",
};

const sizes: Record<Size, string> = {
  default: "h-10 px-4 py-2",
  sm: "h-8 px-3 text-xs",
  icon: "h-9 w-9 p-0",
};

export function Button(props: Props) {
  const { variant = "primary", size = "default", className } = props;
  const classes = cn(
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-semibold transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
    variants[variant],
    sizes[size],
    className,
  );

  if (props.asChild) {
    const child = props.children;
    return React.cloneElement(child, {
      className: cn(classes, child.props.className),
    });
  }

  const { loading, children, disabled, ...rest } = props;
  const buttonProps = { ...rest } as Record<string, unknown>;
  delete buttonProps.asChild;
  delete buttonProps.variant;
  delete buttonProps.size;
  delete buttonProps.className;
  return (
    <button
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...buttonProps}
    >
      {loading ? (
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
      ) : null}
      {children}
    </button>
  );
}
