"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

// `count` is shown as a small pill after the label when it is known.
export type SegmentOption<T extends string> = { value: T; label: string; count?: number };

// Single-choice filter rendered as real buttons with aria-pressed, so it stays
// keyboard- and screen-reader-friendly and scrolls horizontally on phones.
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  label,
  variant = "pill",
  className,
}: {
  options: SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  // "underline" reads as tabs along the top edge of a panel.
  variant?: "pill" | "underline";
  className?: string;
}) {
  const underline = variant === "underline";
  const groupRef = React.useRef<HTMLDivElement>(null);

  // On a narrow screen the options scroll sideways; keep the chosen one in view.
  // Only the group scrolls, never the page.
  React.useEffect(() => {
    const group = groupRef.current;
    const chosen = group?.querySelector<HTMLElement>("[aria-pressed=true]");
    if (!group || !chosen) return;
    const offset = chosen.getBoundingClientRect().left - group.getBoundingClientRect().left;
    group.scrollLeft += offset - (group.clientWidth - chosen.offsetWidth) / 2;
  }, [value]);

  return (
    <div
      ref={groupRef}
      role="group"
      aria-label={label}
      className={cn(
        "scrollbar-none max-w-full items-center overflow-x-auto",
        underline
          ? // Inset shadow, not a border: the selected marker has to overlap the
            // baseline, and a negative margin would be clipped by the scroll box.
            "flex gap-1 shadow-[inset_0_-1px_0_theme(colors.slate.200)]"
          : "inline-flex gap-0.5 rounded-lg border border-slate-200 bg-slate-100/70 p-0.5",
        className,
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex shrink-0 items-center gap-2 font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
              underline
                ? "border-b-2 px-3 py-3.5 text-sm focus-visible:ring-inset"
                : "rounded-md px-3 py-1.5 text-[13px]",
              underline
                ? selected
                  ? "border-brand-600 text-brand-700"
                  : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-900"
                : selected
                  ? "bg-white text-brand-700 shadow-sm ring-1 ring-slate-200"
                  : "text-slate-600 hover:text-slate-900",
            )}
          >
            {option.label}
            {/* The space keeps the spoken name as "Draft 2", not "Draft2". */}
            {option.count !== undefined ? " " : null}
            {option.count !== undefined ? (
              <span
                className={cn(
                  "rounded-full px-1.5 py-px text-[11px] font-semibold tabular-nums",
                  selected ? "bg-brand-100 text-brand-700" : "bg-slate-100 text-slate-500",
                )}
              >
                {option.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
