"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactNode, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, BarChart3, Copy, Eye, Send, Sparkles, Trash2 } from "lucide-react";

import { CampaignTypeBadge } from "@/components/campaigns/campaign-type-badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DropdownMenu } from "@/components/ui/dropdown-menu";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { CampaignStatusBadge } from "@/components/ui/status-badge";
import { useToast } from "@/components/ui/toast";
import { getCampaignAnalytics } from "@/lib/analytics-api";
import { archiveCampaign, duplicateCampaign } from "@/lib/campaigns-api";
import { purgeCampaign } from "@/lib/erasure-api";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatNumber, formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { CampaignListItem } from "@/types/domain";

// The row goes side by side once the list is wide enough. `roomy` is a list
// with no panel beside it, which reaches that width on a smaller screen.
const rowWide = {
  roomy: "xl:grid-cols-[minmax(0,1fr)_auto_auto] xl:gap-x-8",
  tight: "min-[1700px]:grid-cols-[minmax(0,1fr)_auto_auto] min-[1700px]:gap-x-8",
};
const statsWide = {
  roomy: "xl:col-span-1 xl:col-start-2 xl:row-start-1 xl:w-[23rem] xl:border-t-0 xl:pt-0",
  tight:
    "min-[1700px]:col-span-1 min-[1700px]:col-start-2 min-[1700px]:row-start-1 min-[1700px]:w-[23rem] min-[1700px]:border-t-0 min-[1700px]:pt-0",
};
const actionsWide = { roomy: "xl:col-start-3", tight: "min-[1700px]:col-start-3" };
type Density = keyof typeof rowWide;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    // dt/dd stay in semantic order; the value is shown above its label.
    <div className="flex min-w-0 flex-col-reverse">
      <dt className="truncate text-xs text-slate-500">{label}</dt>
      <dd className="text-base font-bold tabular-nums leading-6 text-slate-900">{value}</dd>
    </div>
  );
}

function StatsNote({ children }: { children: ReactNode }) {
  return <p className="text-[13px] leading-5 text-slate-500">{children}</p>;
}

function CampaignStats({
  workspaceId,
  campaign,
}: {
  workspaceId: string;
  campaign: CampaignListItem;
}) {
  // Same key the campaign Analytics tab uses, so the cache is shared. Drafts
  // have never sent, so they skip the request entirely.
  const analyticsQuery = useQuery({
    queryKey: ["workspace", workspaceId, "campaigns", campaign.id, "analytics"],
    queryFn: () => getCampaignAnalytics(workspaceId, campaign.id),
    enabled: campaign.status !== "DRAFT",
    staleTime: 60_000,
  });

  if (campaign.status === "DRAFT") {
    return <StatsNote>Not launched yet. Finish setup in the campaign to start sending.</StatsNote>;
  }

  if (analyticsQuery.isLoading) {
    return (
      <div aria-busy="true" className="grid grid-cols-4 gap-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="space-y-2">
            <Skeleton className="h-5 w-10" />
            <Skeleton className="h-3 w-14" />
          </div>
        ))}
      </div>
    );
  }

  const data = analyticsQuery.data;
  if (!data) {
    return <StatsNote>Performance metrics are unavailable right now.</StatsNote>;
  }

  const opensTracked = data.open_tracking_supported !== false;

  return (
    // Always four slots, so each figure sits in the same place on every row.
    <dl className="grid grid-cols-4 gap-4 sm:divide-x sm:divide-slate-100 sm:[&>div:not(:first-child)]:pl-4">
      <Stat label="Total leads" value={formatNumber(data.total_recipients)} />
      <Stat label="Sent" value={formatNumber(data.sent)} />
      <Stat label="Reply rate" value={`${data.reply_rate.toFixed(1)}%`} />
      {opensTracked ? (
        <Stat label="Open rate" value={`${(data.open_rate ?? 0).toFixed(1)}%`} />
      ) : (
        <Stat label="Bounced" value={formatNumber(data.bounced)} />
      )}
    </dl>
  );
}

// What happened last, in the words that fit the campaign's state.
function campaignTimeline(campaign: CampaignListItem): { text: string; at: string } {
  const startAt = campaign.start_at;
  if (campaign.status === "PAUSED" && campaign.paused_at) {
    return { text: `Paused ${formatRelativeTime(campaign.paused_at)}`, at: campaign.paused_at };
  }
  if (campaign.status === "SCHEDULED" && startAt && new Date(startAt) > new Date()) {
    return { text: `Starts ${formatDateTime(startAt)}`, at: startAt };
  }
  if (campaign.status === "RUNNING" && startAt) {
    return { text: `Started ${formatRelativeTime(startAt)}`, at: startAt };
  }
  if (campaign.status === "DRAFT") {
    return { text: `Created ${formatRelativeTime(campaign.created_at)}`, at: campaign.created_at };
  }
  return { text: `Updated ${formatRelativeTime(campaign.updated_at)}`, at: campaign.updated_at };
}

// The one thing someone most likely wants from a campaign in this state. Every
// label leads to a page the action menu already reaches.
function primaryAction(campaign: CampaignListItem, mayDraft: boolean) {
  const base = `/app/campaigns/${campaign.id}`;
  if (!mayDraft) return { label: "View", href: `${base}/overview` };
  switch (campaign.status) {
    case "DRAFT":
      return { label: "Continue setup", href: `${base}/overview` };
    case "RUNNING":
      return { label: "View progress", href: `${base}/overview` };
    case "COMPLETED":
      return { label: "View report", href: `${base}/analytics` };
    default:
      return { label: "View details", href: `${base}/overview` };
  }
}

// Which campaigns may be archived: drafts by editors, activated ones by roles that
// may execute campaigns. A RUNNING campaign has to be paused first.
export function canArchiveCampaign(
  status: CampaignListItem["status"],
  mayDraft: boolean,
  mayExecute: boolean,
): boolean {
  if (status === "DRAFT") return mayDraft;
  if (status === "RUNNING" || status === "ARCHIVED") return false;
  return mayExecute;
}

export function CampaignCard({
  campaign,
  workspaceId,
  mayDraft,
  mayExecute = false,
  mayErase = false,
  selectable = false,
  selected = false,
  onToggleSelect,
  density = "roomy",
}: {
  campaign: CampaignListItem;
  workspaceId: string;
  mayDraft: boolean;
  mayExecute?: boolean;
  mayErase?: boolean;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  density?: Density;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [confirmRemoval, setConfirmRemoval] = useState(false);
  const [removalError, setRemovalError] = useState<string | null>(null);
  const overviewHref = `/app/campaigns/${campaign.id}/overview`;
  const isHyper = campaign.campaign_type === "HYPER_PERSONALIZED";

  const invalidateList = () =>
    queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId, "campaigns"] });

  const duplicateMutation = useMutation({
    mutationFn: () => duplicateCampaign(workspaceId, campaign.id),
    onSuccess: (copy) => {
      invalidateList();
      toast(`Duplicated as “${copy.name}”.`);
      router.push(`/app/campaigns/${copy.id}/overview`);
    },
    onError: (err) => toast(errorMessage(err), "error"),
  });

  const archiveMutation = useMutation({
    mutationFn: () =>
      archiveCampaign(workspaceId, campaign.id, { expected_version: campaign.version }),
    onSuccess: () => {
      invalidateList();
      setConfirmArchive(false);
      toast(`Archived “${campaign.name}”.`);
    },
    onError: (err) => {
      setConfirmArchive(false);
      toast(errorMessage(err), "error");
    },
  });

  // Only an archived campaign can be deleted; the delete removes it and its sent
  // history for good (leads, mailboxes and suppressions are not affected).
  const isArchived = campaign.status === "ARCHIVED";
  const removalMutation = useMutation({
    mutationFn: (phrase: string) => purgeCampaign(workspaceId, campaign.id, phrase),
    onSuccess: () => {
      invalidateList();
      setConfirmRemoval(false);
      toast(`Deleted “${campaign.name}”.`);
    },
    onError: (err) => setRemovalError(errorMessage(err)),
  });

  const menuItems = [
    { label: "Overview", href: overviewHref, icon: <Eye /> },
    {
      label: "Analytics",
      href: `/app/campaigns/${campaign.id}/analytics`,
      icon: <BarChart3 />,
    },
    ...(mayDraft
      ? [
          {
            label: "Duplicate",
            icon: <Copy />,
            disabled: duplicateMutation.isPending,
            onSelect: () => duplicateMutation.mutate(),
          },
        ]
      : []),
    ...(canArchiveCampaign(campaign.status, mayDraft, mayExecute)
      ? [
          {
            label: "Archive",
            icon: <Archive />,
            tone: "danger" as const,
            onSelect: () => setConfirmArchive(true),
          },
        ]
      : []),
    ...(mayErase && isArchived
      ? [
          {
            label: "Delete permanently…",
            icon: <Trash2 />,
            tone: "danger" as const,
            onSelect: () => {
              setRemovalError(null);
              setConfirmRemoval(true);
            },
          },
        ]
      : []),
  ];

  const timeline = campaignTimeline(campaign);
  const action = primaryAction(campaign, mayDraft);

  return (
    <article
      aria-label={campaign.name}
      className={cn(
        "group relative grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-4 rounded-xl border bg-white p-4 transition-[border-color,box-shadow] duration-150 sm:px-5",
        rowWide[density],
        selected
          ? "border-brand-300 bg-brand-50/40"
          : "border-slate-200 hover:border-slate-300 hover:shadow-card-hover",
      )}
    >
      <div className="col-start-1 row-start-1 flex min-w-0 items-center gap-3.5">
        {selectable ? (
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect?.(campaign.id)}
            aria-label={`Select ${campaign.name}`}
            className="relative z-10 h-4 w-4 shrink-0 rounded border-slate-300 text-brand-600 focus-visible:ring-2 focus-visible:ring-brand-500"
          />
        ) : null}
        <div
          aria-hidden="true"
          className={cn(
            "hidden h-11 w-11 shrink-0 items-center justify-center rounded-xl sm:flex",
            isHyper ? "bg-violet-50 text-violet-600" : "bg-brand-50 text-brand-600",
          )}
        >
          {isHyper ? <Sparkles className="h-5 w-5" /> : <Send className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="min-w-0 truncate text-[15px] font-bold text-slate-900 transition-colors group-hover:text-brand-700">
              {/* Stretched link: the whole row opens the campaign, while the
                  checkbox, menu and action sit above it (relative z-10). */}
              <Link
                href={overviewHref}
                className="after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-brand-500"
              >
                {campaign.name}
              </Link>
            </h3>
            <CampaignStatusBadge status={campaign.status} />
            <CampaignTypeBadge type={campaign.campaign_type} />
            {campaign.erased_at ? (
              <span className="inline-flex items-center rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-semibold text-slate-600">
                Data removed
              </span>
            ) : null}
          </div>
          <p className="mt-1 flex min-w-0 items-center gap-2 text-[13px] text-slate-500 [&>span+span]:border-l [&>span+span]:border-slate-200 [&>span+span]:pl-2">
            <span className="shrink-0" title={new Date(timeline.at).toLocaleString()}>
              {timeline.text}
            </span>
            {campaign.creator_name ? (
              <span className="shrink-0">by {campaign.creator_name}</span>
            ) : null}
            {campaign.description ? (
              <span className="min-w-0 truncate">{campaign.description}</span>
            ) : null}
          </p>
        </div>
      </div>

      <div
        className={cn(
          "col-span-2 row-start-2 border-t border-slate-100 pt-3.5",
          statsWide[density],
        )}
      >
        <CampaignStats workspaceId={workspaceId} campaign={campaign} />
      </div>

      <div
        className={cn(
          // While its menu is open the row's actions rise above later rows,
          // whose own actions would otherwise paint over the menu.
          "relative z-10 col-start-2 row-start-1 flex items-center gap-1 has-[[aria-expanded=true]]:z-30",
          actionsWide[density],
        )}
      >
        {/* Phones drop the button: the row itself opens the campaign. */}
        <Button variant="outline" size="sm" asChild className="hidden sm:inline-flex">
          <Link href={action.href}>{action.label}</Link>
        </Button>
        <DropdownMenu label={`Actions for ${campaign.name}`} items={menuItems} />
      </div>

      <ConfirmDialog
        open={confirmArchive}
        title={`Archive "${campaign.name}"?`}
        description={
          campaign.status === "DRAFT"
            ? "It will no longer be editable."
            : "It will stop for good and can't be resumed or edited. Its history and results are kept, and you can duplicate it to run it again."
        }
        confirmLabel="Archive campaign"
        tone="danger"
        loading={archiveMutation.isPending}
        onConfirm={() => archiveMutation.mutate()}
        onCancel={() => setConfirmArchive(false)}
      />

      <TypeToConfirmDialog
        open={confirmRemoval}
        title={`Delete "${campaign.name}"?`}
        description="The campaign, its steps, audience, and every email it sent, with their tracking and replies, are deleted permanently. Your leads, mailboxes and suppression list are not affected. This can't be undone."
        phrase={campaign.name}
        confirmLabel="Delete permanently"
        loading={removalMutation.isPending}
        error={removalError}
        onConfirm={() => removalMutation.mutate(campaign.name)}
        onCancel={() => setConfirmRemoval(false)}
      />
    </article>
  );
}

export function CampaignCardSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-4 rounded-xl border border-slate-200 bg-white p-4 sm:px-5"
    >
      <div className="flex min-w-0 items-center gap-3.5">
        <Skeleton className="hidden h-11 w-11 shrink-0 rounded-xl sm:block" />
        <div className="min-w-0 flex-1 space-y-2">
          <Skeleton className="h-4 w-48 max-w-full" />
          <Skeleton className="h-3 w-32 max-w-full" />
        </div>
      </div>
      <Skeleton className="col-start-2 row-start-1 h-8 w-24" />
      <Skeleton className="col-span-2 row-start-2 h-10" />
    </div>
  );
}
