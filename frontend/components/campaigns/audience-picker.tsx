"use client";

import { useQuery } from "@tanstack/react-query";

import { Input } from "@/components/ui/input";
import { listLeadLists, listLeads } from "@/lib/leads-api";

type Props = {
  workspaceId: string;
  selectedListIds: Set<string>;
  onListsChange: (next: Set<string>) => void;
  selectedLeadIds: Set<string>;
  onLeadsChange: (next: Set<string>) => void;
  leadSearch: string;
  onLeadSearchChange: (value: string) => void;
};

/**
 * Pick lead lists and individual leads. Shared by the draft audience form and by
 * "add leads" on a paused campaign, so both choose from the same places.
 */
export function AudiencePicker({
  workspaceId,
  selectedListIds,
  onListsChange,
  selectedLeadIds,
  onLeadsChange,
  leadSearch,
  onLeadSearchChange,
}: Props) {
  const listsQuery = useQuery({
    queryKey: ["workspace", workspaceId, "lead-lists", { forCampaign: true }],
    queryFn: () => listLeadLists(workspaceId, { limit: 100 }),
    enabled: Boolean(workspaceId),
  });

  const leadsQuery = useQuery({
    queryKey: ["workspace", workspaceId, "leads", { forCampaign: true, q: leadSearch }],
    queryFn: () => listLeads(workspaceId, { limit: 25, q: leadSearch || null }),
    enabled: Boolean(workspaceId && leadSearch.trim().length > 0),
  });

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Lead lists
        </h4>
        <div className="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
          {listsQuery.data?.items.length ? (
            listsQuery.data.items.map((list) => (
              <label
                key={list.id}
                className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-slate-50"
              >
                <input
                  type="checkbox"
                  checked={selectedListIds.has(list.id)}
                  onChange={(e) => {
                    const next = new Set(selectedListIds);
                    if (e.target.checked) next.add(list.id);
                    else next.delete(list.id);
                    onListsChange(next);
                  }}
                />
                {list.name}
                <span className="text-xs text-slate-400">({list.member_count})</span>
              </label>
            ))
          ) : (
            <p className="px-2 py-1 text-xs text-slate-400">No lists yet.</p>
          )}
        </div>
      </div>

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Individual leads
        </h4>
        <Input
          className="mt-2"
          placeholder="Search leads by name, email, or company..."
          value={leadSearch}
          onChange={(e) => onLeadSearchChange(e.target.value)}
        />
        {leadSearch.trim() && (
          <div className="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
            {leadsQuery.data?.items.length ? (
              leadsQuery.data.items.map((lead) => (
                <label
                  key={lead.id}
                  className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-slate-50"
                >
                  <input
                    type="checkbox"
                    checked={selectedLeadIds.has(lead.id)}
                    onChange={(e) => {
                      const next = new Set(selectedLeadIds);
                      if (e.target.checked) next.add(lead.id);
                      else next.delete(lead.id);
                      onLeadsChange(next);
                    }}
                  />
                  {lead.first_name} {lead.last_name} &mdash; {lead.email}
                </label>
              ))
            ) : (
              <p className="px-2 py-1 text-xs text-slate-400">No matching leads.</p>
            )}
          </div>
        )}
        {selectedLeadIds.size > 0 && (
          <p className="mt-1 text-xs text-slate-500">
            {selectedLeadIds.size} lead{selectedLeadIds.size === 1 ? "" : "s"} selected
            individually.
          </p>
        )}
      </div>
    </div>
  );
}
