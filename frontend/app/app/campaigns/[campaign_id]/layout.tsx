"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft } from "lucide-react";

import { CampaignRunControls } from "@/components/campaigns/campaign-run-controls";
import { CampaignTypeBadge } from "@/components/campaigns/campaign-type-badge";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { CampaignStatusBadge } from "@/components/ui/status-badge";
import { getCampaign, getPreflight } from "@/lib/campaigns-api";
import { errorMessage } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

const TABS: { href: string; label: string }[] = [
  { href: "overview", label: "Overview" },
  { href: "analytics", label: "Analytics" },
  { href: "audience", label: "Audience" },
  { href: "sequence", label: "Sequence" },
  { href: "senders", label: "Senders" },
  { href: "schedule", label: "Schedule" },
  { href: "review", label: "Review" },
];

export default function CampaignLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const params = useParams<{ campaign_id: string }>();
  const campaignId = params.campaign_id;
  const { activeWorkspaceId } = useWorkspace();

  const campaignQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId],
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? getCampaign(activeWorkspaceId, campaignId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId),
  });

  const preflightQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId, "preflight"],
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? getPreflight(activeWorkspaceId, campaignId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId),
    // Preflight is read against authoritative saved state, so re-check it
    // whenever a tab regains focus rather than trusting a stale result
    // while the user edits other tabs.
    refetchOnWindowFocus: true,
  });

  if (campaignQuery.isLoading) {
    return (
      <div aria-busy="true" className="space-y-6">
        <div className="space-y-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-72" />
          <Skeleton className="h-4 w-96 max-w-full" />
        </div>
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    );
  }

  if (campaignQuery.isError || !campaignQuery.data) {
    return (
      <div className="space-y-4">
        <Alert variant="error">
          {errorMessage(campaignQuery.error, "We couldn't load this campaign. Please try again.")}
        </Alert>
        <Link
          href="/app/campaigns"
          className="inline-flex items-center gap-1 text-sm font-medium text-slate-500 hover:text-slate-900"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          Back to Campaigns
        </Link>
      </div>
    );
  }

  const campaign = campaignQuery.data;
  // Hyper-personalized campaigns author their objective, emails, samples and
  // approval on the Sequence tab (ADR-0016), so every campaign has the same tabs.
  const tabs = TABS;
  const errorCount = preflightQuery.data?.errors.length ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/app/campaigns"
          className="inline-flex items-center gap-1 rounded text-sm font-medium text-slate-500 transition-colors hover:text-slate-900"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          Campaigns
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-3">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900 min-w-0 break-words">
              {campaign.name}
            </h1>
            <CampaignStatusBadge status={campaign.status} />
            <CampaignTypeBadge type={campaign.campaign_type} />
            {campaign.status === "DRAFT" && errorCount > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-600/25">
                <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                {errorCount} item{errorCount === 1 ? "" : "s"} left before this
                campaign is ready
              </span>
            )}
          </div>
          <CampaignRunControls campaign={campaign} />
        </div>
        {campaign.status === "PAUSED" && (
          <div className="mt-3 max-w-3xl">
            <Alert variant="warning">
              This campaign is paused, so nothing new is being sent. You can change the
              schedule, edit emails that haven&apos;t been sent and add leads. Changes
              apply when you resume.
            </Alert>
          </div>
        )}
        {campaign.description && (
          <p className="mt-1.5 max-w-3xl text-sm text-slate-500">{campaign.description}</p>
        )}
      </div>

      <div className="-mx-4 border-b border-slate-200 px-4 sm:mx-0 sm:px-0">
        <nav className="-mb-px flex gap-1 overflow-x-auto sm:gap-2">
          {tabs.map((tab) => {
            const href = `/app/campaigns/${campaignId}/${tab.href}`;
            const isActive = pathname?.startsWith(href);
            return (
              <Link
                key={tab.href}
                href={href}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "shrink-0 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500",
                  isActive
                    ? "border-brand-600 text-brand-700"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800",
                )}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="animate-fade-in">{children}</div>
    </div>
  );
}
