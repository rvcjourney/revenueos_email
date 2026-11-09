"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, UserPlus, XCircle } from "lucide-react";

import { AudiencePicker } from "@/components/campaigns/audience-picker";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import {
  enrollSupplement,
  listSupplements,
  retrySupplement,
  selectSupplement,
} from "@/lib/campaigns-api";
import { errorMessage } from "@/lib/errors";
import type { CampaignSupplement } from "@/types/domain";

const REASON_LABELS: Record<string, string> = {
  already_enrolled: "already in this campaign",
  suppressed: "suppressed (unsubscribed, bounced or blocked)",
  archived_lead: "archived",
  invalid_address: "no valid email address",
};

function isBusy(item: CampaignSupplement): boolean {
  const running = (state: string) => state === "PENDING" || state === "PROCESSING";
  return item.status === "CAPTURING" || running(item.enroll_state) || running(item.render_state);
}

function skippedSummary(item: CampaignSupplement): string {
  return Object.entries(item.exclusions)
    .filter(([, count]) => count > 0)
    .map(([reason, count]) => `${count} ${REASON_LABELS[reason] ?? reason.replaceAll("_", " ")}`)
    .join(", ");
}

function SupplementCard({
  item,
  onAdd,
  adding,
  onRetry,
  retrying,
}: {
  item: CampaignSupplement;
  onAdd: () => void;
  adding: boolean;
  onRetry: () => void;
  retrying: boolean;
}) {
  if (item.status === "CAPTURING") {
    return (
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
        <span className="inline-flex items-center gap-2 font-medium">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Checking leads... ({item.processed_count} of {item.total_candidates ?? "?"})
        </span>
      </div>
    );
  }
  if (item.status !== "READY") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900">
        <span className="inline-flex items-center gap-2 font-medium">
          <XCircle className="h-4 w-4" aria-hidden="true" />
          We couldn&apos;t check these leads{item.error_reason ? `: ${item.error_reason}` : "."}
        </span>
      </div>
    );
  }

  const accepted = item.accepted_count ?? 0;
  const skipped = skippedSummary(item);

  if (item.enroll_state === "FAILED" || item.render_state === "FAILED") {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900">
        <span className="inline-flex items-center gap-2 font-medium">
          <XCircle className="h-4 w-4" aria-hidden="true" />
          Adding these leads failed{item.error_reason ? `: ${item.error_reason}` : "."}
        </span>
        <p className="mt-1 text-xs">
          Retrying continues where it stopped; leads that were already added are not added twice.
          You can&apos;t resume the campaign until this succeeds.
        </p>
        <div className="mt-2">
          <Button size="sm" variant="outline" disabled={retrying} onClick={onRetry}>
            {retrying && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden="true" />}
            Retry
          </Button>
        </div>
      </div>
    );
  }

  if (item.enroll_state !== "NOT_STARTED") {
    const done = item.enroll_state === "READY" && item.render_state === "READY";
    return (
      <div
        className={`rounded-lg border p-3 text-sm ${
          done
            ? "border-emerald-200 bg-emerald-50 text-emerald-900"
            : "border-blue-200 bg-blue-50 text-blue-900"
        }`}
      >
        <span className="inline-flex items-center gap-2 font-medium">
          {done ? (
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
          ) : (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          )}
          {done
            ? `${accepted} lead${accepted === 1 ? "" : "s"} added. They are sent when you resume.`
            : `Adding leads... (${item.enrolled_count} of ${accepted})`}
        </span>
        {skipped ? <p className="mt-1 text-xs">Skipped: {skipped}.</p> : null}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm">
      <p className="font-medium text-slate-900">
        {accepted} new lead{accepted === 1 ? "" : "s"} can be added
      </p>
      {skipped ? <p className="mt-1 text-xs text-slate-500">Skipped: {skipped}.</p> : null}
      {accepted > 0 ? (
        <div className="mt-3">
          <Button size="sm" disabled={adding} onClick={onAdd}>
            {adding && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden="true" />}
            Add {accepted} lead{accepted === 1 ? "" : "s"} to this campaign
          </Button>
        </div>
      ) : (
        <p className="mt-1 text-xs text-slate-500">
          Nobody in this selection can be added. Pick different leads.
        </p>
      )}
    </div>
  );
}

/**
 * Add leads to a paused campaign (ADR-0020). New leads are checked first (anyone
 * already in the campaign, suppressed or archived is skipped, never enrolled
 * twice), reviewed, then added. They are sent from the next window after you
 * resume; the leads already in the campaign are not touched.
 */
export function AddLeadsPanel({
  workspaceId,
  campaignId,
}: {
  workspaceId: string;
  campaignId: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [selectedListIds, setSelectedListIds] = useState<Set<string>>(new Set());
  const [selectedLeadIds, setSelectedLeadIds] = useState<Set<string>>(new Set());
  const [leadSearch, setLeadSearch] = useState("");
  const [error, setError] = useState<string | null>(null);

  const campaignKey = ["workspace", workspaceId, "campaigns", campaignId];
  const supplementsKey = [...campaignKey, "supplements"];

  const supplementsQuery = useQuery({
    queryKey: supplementsKey,
    queryFn: () => listSupplements(workspaceId, campaignId),
    refetchInterval: (query) => (query.state.data?.some(isBusy) ? 2000 : false),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: supplementsKey });
    void queryClient.invalidateQueries({ queryKey: campaignKey });
  };

  const selectMutation = useMutation({
    mutationFn: () =>
      selectSupplement(workspaceId, campaignId, {
        list_ids: Array.from(selectedListIds),
        lead_ids: Array.from(selectedLeadIds),
      }),
    onSuccess: () => {
      setError(null);
      setSelectedListIds(new Set());
      setSelectedLeadIds(new Set());
      setLeadSearch("");
      refresh();
    },
    onError: (err) => setError(errorMessage(err)),
  });

  const enrollMutation = useMutation({
    mutationFn: (audienceId: string) => enrollSupplement(workspaceId, campaignId, audienceId),
    onSuccess: () => {
      setError(null);
      toast("Adding leads…");
      refresh();
    },
    onError: (err) => {
      setError(errorMessage(err));
      refresh();
    },
  });

  const retryMutation = useMutation({
    mutationFn: (audienceId: string) => retrySupplement(workspaceId, campaignId, audienceId),
    onSuccess: () => {
      setError(null);
      toast("Retrying…");
      refresh();
      // The worker resets the job a moment after the request returns.
      window.setTimeout(refresh, 1500);
    },
    onError: (err) => {
      setError(errorMessage(err));
      refresh();
    },
  });

  const items = [...(supplementsQuery.data ?? [])].reverse();
  const capturing = items.some((item) => item.status === "CAPTURING");

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
        <UserPlus className="h-4 w-4 text-slate-500" aria-hidden="true" />
        Add leads
      </h3>
      <p className="mt-1 text-xs text-slate-500">
        New leads join the leads already in this campaign; nothing about them changes. Anyone
        already in the campaign, suppressed or archived is skipped. Added leads are sent after
        you resume.
      </p>

      {error ? (
        <div className="mt-3">
          <Alert variant="error">{error}</Alert>
        </div>
      ) : null}

      {items.length > 0 ? (
        <div className="mt-4 space-y-3">
          {items.map((item) => (
            <SupplementCard
              key={item.id}
              item={item}
              adding={enrollMutation.isPending && enrollMutation.variables === item.id}
              onAdd={() => enrollMutation.mutate(item.id)}
              retrying={retryMutation.isPending && retryMutation.variables === item.id}
              onRetry={() => retryMutation.mutate(item.id)}
            />
          ))}
        </div>
      ) : null}

      <div className="mt-4">
        <AudiencePicker
          workspaceId={workspaceId}
          selectedListIds={selectedListIds}
          onListsChange={setSelectedListIds}
          selectedLeadIds={selectedLeadIds}
          onLeadsChange={setSelectedLeadIds}
          leadSearch={leadSearch}
          onLeadSearchChange={setLeadSearch}
        />
        <div className="mt-4 flex justify-end">
          <Button
            disabled={
              (selectedListIds.size === 0 && selectedLeadIds.size === 0) ||
              selectMutation.isPending ||
              capturing
            }
            onClick={() => selectMutation.mutate()}
          >
            {selectMutation.isPending && (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden="true" />
            )}
            Check leads
          </Button>
        </div>
      </div>
    </div>
  );
}
