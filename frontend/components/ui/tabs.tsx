"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type TabItem = { id: string; label: string; icon?: React.ReactNode };

type TabsProps = {
  tabs: TabItem[];
  value: string;
  onValueChange: (id: string) => void;
  idPrefix: string;
  label: string;
  className?: string;
};

export function tabId(prefix: string, id: string) {
  return `${prefix}-tab-${id}`;
}

export function panelId(prefix: string, id: string) {
  return `${prefix}-panel-${id}`;
}

// Controlled tab list with roving tabindex and arrow-key navigation. The
// caller renders the matching <TabPanel>.
export function Tabs({ tabs, value, onValueChange, idPrefix, label, className }: TabsProps) {
  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const index = tabs.findIndex((tab) => tab.id === value);
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    onValueChange(tabs[next].id);
    document.getElementById(tabId(idPrefix, tabs[next].id))?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn("scrollbar-none flex gap-1 overflow-x-auto border-b border-slate-200", className)}
    >
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            id={tabId(idPrefix, tab.id)}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={panelId(idPrefix, tab.id)}
            tabIndex={selected ? 0 : -1}
            onClick={() => onValueChange(tab.id)}
            className={cn(
              "-mb-px inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
              selected
                ? "border-brand-600 text-brand-700"
                : "border-transparent text-slate-500 hover:text-slate-900",
            )}
          >
            {tab.icon}
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  idPrefix,
  id,
  value,
  children,
  className,
}: {
  idPrefix: string;
  id: string;
  value: string;
  children: React.ReactNode;
  className?: string;
}) {
  if (id !== value) return null;
  return (
    <div
      role="tabpanel"
      id={panelId(idPrefix, id)}
      aria-labelledby={tabId(idPrefix, id)}
      className={className}
    >
      {children}
    </div>
  );
}
