import * as React from "react";

import { cn } from "@/lib/utils";

export const controlClasses =
  "w-full rounded-lg border border-slate-200 bg-white text-sm text-slate-900 shadow-[0_1px_2px_rgb(15_23_42/0.04)] transition-colors placeholder:text-slate-400 hover:border-slate-300 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => {
    return (
      <input
        ref={ref}
        className={cn(controlClasses, "h-10 px-3", className)}
        {...props}
      />
    );
  },
);
Input.displayName = "Input";
