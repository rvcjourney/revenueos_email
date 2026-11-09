"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Contact, CornerDownLeft, Search, Send } from "lucide-react";

import { listCampaigns } from "@/lib/campaigns-api";
import { listLeads } from "@/lib/leads-api";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";

export type PaletteDestination = {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
};

type Result = PaletteDestination & { id: string; group: string; hint?: string };

const MIN_QUERY = 2;
const PER_GROUP = 5;

// Search-and-jump overlay opened from the top bar or with Ctrl/Cmd+K. Pages are
// matched locally; campaigns and leads use the same list endpoints (and the
// same workspace scoping) as their own pages.
export function CommandPalette({
  open,
  onClose,
  workspaceId,
  destinations,
}: {
  open: boolean;
  onClose: () => void;
  workspaceId: string | null;
  destinations: PaletteDestination[];
}) {
  const router = useRouter();
  const [text, setText] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listId = React.useId();
  const term = useDebouncedValue(text.trim(), 250);
  const searching = open && Boolean(workspaceId) && term.length >= MIN_QUERY;

  const campaignsQuery = useQuery({
    queryKey: ["workspace", workspaceId, "palette", "campaigns", term],
    queryFn: () => listCampaigns(workspaceId!, { limit: PER_GROUP, q: term, status: "ALL" }),
    enabled: searching,
    staleTime: 30_000,
  });
  const leadsQuery = useQuery({
    queryKey: ["workspace", workspaceId, "palette", "leads", term],
    queryFn: () => listLeads(workspaceId!, { limit: PER_GROUP, q: term }),
    enabled: searching,
    staleTime: 30_000,
  });

  React.useEffect(() => {
    if (!open) return;
    setText("");
    setActive(0);
    const previousOverflow = document.body.style.overflow;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    inputRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [open]);

  const results = React.useMemo<Result[]>(() => {
    const needle = text.trim().toLowerCase();
    const pages = destinations
      .filter((d) => !needle || d.label.toLowerCase().includes(needle))
      .map((d) => ({ ...d, id: `page:${d.href}`, group: "Pages" }));
    if (!searching) return pages;
    const campaigns = (campaignsQuery.data?.items ?? []).map((c) => ({
      id: `campaign:${c.id}`,
      group: "Campaigns",
      href: `/app/campaigns/${c.id}/overview`,
      label: c.name,
      hint: c.status.charAt(0) + c.status.slice(1).toLowerCase(),
      icon: Send,
    }));
    const leads = (leadsQuery.data?.items ?? []).map((l) => {
      const name = [l.first_name, l.last_name].filter(Boolean).join(" ");
      return {
        id: `lead:${l.id}`,
        group: "Leads",
        href: `/app/leads/${l.id}`,
        label: name || l.email,
        hint: name ? l.email : (l.company ?? undefined),
        icon: Contact,
      };
    });
    return [...pages, ...campaigns, ...leads];
  }, [text, destinations, searching, campaignsQuery.data, leadsQuery.data]);

  React.useEffect(() => {
    setActive((index) => Math.min(index, Math.max(0, results.length - 1)));
  }, [results.length]);

  if (!open || typeof document === "undefined") return null;

  function go(result: Result | undefined) {
    if (!result) return;
    onClose();
    router.push(result.href);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (results.length ? (index + 1) % results.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (results.length ? (index - 1 + results.length) % results.length : 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(results[active]);
    } else if (event.key === "Tab") {
      // The palette is a single control; keep focus in the field.
      event.preventDefault();
    }
  }

  const loading = searching && (campaignsQuery.isFetching || leadsQuery.isFetching);
  const failed = searching && (campaignsQuery.isError || leadsQuery.isError);

  return createPortal(
    <div
      className="fixed inset-0 z-[65] flex items-start justify-center bg-ink-950/60 px-4 pt-[12vh] backdrop-blur-[2px] animate-fade-in"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        onKeyDown={onKeyDown}
        className="flex max-h-[70vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-overlay animate-pop-in"
      >
        <div className="flex items-center gap-3 border-b border-slate-200 px-4">
          <Search className="h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={results[active] ? `${listId}-${active}` : undefined}
            aria-label="Search campaigns, leads and pages"
            placeholder="Search campaigns, leads and pages"
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setActive(0);
            }}
            className="h-12 min-w-0 flex-1 bg-transparent text-sm text-slate-900 placeholder:text-slate-400 focus-visible:outline-none"
          />
          <kbd className="hidden rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium text-slate-500 sm:block">
            Esc
          </kbd>
        </div>

        <div id={listId} role="listbox" aria-label="Results" className="min-h-0 overflow-y-auto p-2">
          {results.length === 0 ? (
            <p className="px-3 py-8 text-center text-sm text-slate-500">
              {loading ? "Searching…" : `Nothing matches “${text.trim()}”.`}
            </p>
          ) : (
            results.map((result, index) => {
              const Icon = result.icon;
              const selected = index === active;
              const startsGroup = index === 0 || results[index - 1].group !== result.group;
              return (
                <React.Fragment key={result.id}>
                  {startsGroup ? (
                    <p className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-slate-400">
                      {result.group}
                    </p>
                  ) : null}
                  <button
                    type="button"
                    role="option"
                    id={`${listId}-${index}`}
                    aria-selected={selected}
                    tabIndex={-1}
                    onMouseMove={() => setActive(index)}
                    onClick={() => go(result)}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm",
                      selected ? "bg-brand-50 text-brand-900" : "text-slate-700",
                    )}
                  >
                    <Icon
                      className={cn("h-4 w-4 shrink-0", selected ? "text-brand-600" : "text-slate-400")}
                    />
                    <span className="min-w-0 flex-1 truncate font-medium">{result.label}</span>
                    {result.hint ? (
                      <span className="max-w-[45%] shrink-0 truncate text-xs text-slate-500">
                        {result.hint}
                      </span>
                    ) : null}
                    {selected ? (
                      <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-brand-500" aria-hidden="true" />
                    ) : null}
                  </button>
                </React.Fragment>
              );
            })
          )}
          {failed ? (
            <p role="alert" className="px-3 py-2 text-xs text-red-600">
              Search is unavailable right now. Pages can still be opened.
            </p>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}
