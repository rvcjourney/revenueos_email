import * as React from "react";

import { cn } from "@/lib/utils";

// Big-number tile used for KPIs. The label is the accessible name of the value.
export function MetricCard({
  label,
  value,
  hint,
  icon,
  className,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-slate-200 bg-white p-4 shadow-card",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <p className="text-[13px] font-semibold text-slate-500">{label}</p>
        {icon ? (
          <span
            className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-50 text-brand-600 [&>svg]:h-4 [&>svg]:w-4"
            aria-hidden="true"
          >
            {icon}
          </span>
        ) : null}
      </div>
      <p className="mt-2 text-[26px] font-bold leading-8 tabular-nums tracking-tight text-slate-900">
        {value}
      </p>
      {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}
