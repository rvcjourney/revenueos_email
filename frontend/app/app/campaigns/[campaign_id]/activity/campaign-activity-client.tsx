"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  AlertOctagon,
  AlertTriangle,
  Ban,
  CheckCircle2,
  Eye,
  Info,
  MailX,
  MessageSquare,
  RefreshCw,
  Search,
  Send,
  UserMinus,
  XCircle,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { CursorPagination } from "@/components/ui/pagination";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { LoadingBlock } from "@/components/ui/skeleton";
import { StatusBadge, type Tone } from "@/components/ui/status-badge";
import { getCampaignActivity } from "@/lib/analytics-api";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatNumber, formatRelativeTime } from "@/lib/format";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { useWorkspace } from "@/lib/workspace-context";
import type {
  CampaignActivityItem,
  CampaignActivityStatus,
  CampaignActivityType,
} from "@/types/analytics";

const PAGE_SIZE = 25;
// The first page re-checks for new activity; paging back through history does not.
const REFRESH_MS = 30_000;

const TYPE_META: Record<
  CampaignActivityType,
  { label: string; icon: React.ComponentType<{ className?: string }> }
> = {
  SENT: { label: "Sent", icon: Send },
  OPENED: { label: "Opened", icon: Eye },
  REPLIED: { label: "Reply", icon: MessageSquare },
  BOUNCED: { label: "Bounce", icon: MailX },
  UNSUBSCRIBED: { label: "Unsubscribed", icon: UserMinus },
  COMPLAINED: { label: "Spam complaint", icon: Ban },
  FAILED: { label: "Send failed", icon: AlertOctagon },
};

const STATUS_META: Record<
  CampaignActivityStatus,
  { label: string; tone: Tone; icon: React.ReactNode }
> = {
  SUCCESS: { label: "Success", tone: "success", icon: <CheckCircle2 aria-hidden="true" /> },
  INFO: { label: "Info", tone: "neutral", icon: <Info aria-hidden="true" /> },
  WARNING: {
    label: "Needs attention",
    tone: "warning",
    icon: <AlertTriangle aria-hidden="true" />,
  },
  ERROR: { label: "Failed", tone: "danger", icon: <XCircle aria-hidden="true" /> },
};

const FEED_TYPES: CampaignActivityType[] = [
  "SENT",
  "OPENED",
  "REPLIED",
  "BOUNCED",
  "UNSUBSCRIBED",
  "FAILED",
];

type TypeFilter = CampaignActivityType | "ALL";

function isType(value: string | null): value is CampaignActivityType {
  return value !== null && value in TYPE_META;
}

function isStatus(value: string | null): value is CampaignActivityStatus {
  return value !== null && value in STATUS_META;
}

const ERASED_LABEL = "Erased lead";

function recipientLabel(item: CampaignActivityItem) {
  if (!item.recipient_email) return "Unknown recipient";
  return item.recipient_email.endsWith("@erased.invalid")
    ? ERASED_LABEL
    : item.recipient_email;
}

function ActivityRow({ item }: { item: CampaignActivityItem }) {
  const type = TYPE_META[item.type] ?? { label: item.type, icon: Info };
  const status = STATUS_META[item.status] ?? STATUS_META.INFO;
  const Icon = type.icon;
  const label = recipientLabel(item);

  return (
    <li
      data-testid="campaign-activity-item"
      className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
    >
      <div className="flex min-w-0 gap-3">
        <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full bg-slate-100 text-slate-600">
          <Icon className="h-4 w-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm text-slate-900">
            <span className="font-medium">{type.label}</span>
            <span className="text-slate-400"> · </span>
            {item.lead_id && label !== ERASED_LABEL ? (
              <Link
                href={`/app/leads/${item.lead_id}`}
                className="hover:text-brand-700 hover:underline"
              >
                {label}
              </Link>
            ) : (
              <span>{label}</span>
            )}
          </p>
          {item.subject || item.step_position ? (
            <p className="truncate text-xs text-slate-500">
              {item.step_position ? `Step ${item.step_position}` : null}
              {item.step_position && item.subject ? " · " : null}
              {item.subject}
            </p>
          ) : null}
          {item.detail ? (
            <p className="mt-0.5 break-words text-sm text-slate-600">{item.detail}</p>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-3 pl-11 sm:flex-col sm:items-end sm:gap-1 sm:pl-0">
        <StatusBadge tone={status.tone} icon={status.icon}>
          {status.label}
        </StatusBadge>
        <time
          dateTime={item.occurred_at}
          title={formatDateTime(item.occurred_at)}
          className="text-xs text-slate-500"
        >
          {formatRelativeTime(item.occurred_at)}
        </time>
      </div>
    </li>
  );
}

export function CampaignActivityClient() {
  const { campaign_id: campaignId } = useParams<{ campaign_id: string }>();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { activeWorkspaceId } = useWorkspace();

  const [searchText, setSearchText] = useState(params.get("q") ?? "");
  const debouncedSearch = useDebouncedValue(searchText, 350);

  const typeParam = params.get("type");
  const statusParam = params.get("status");
  const type = isType(typeParam) ? typeParam : null;
  const status = isStatus(statusParam) ? statusParam : null;
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const q = params.get("q") ?? "";
  const cursor = params.get("cursor");
  const filtered = Boolean(type || status || from || to || q);

  useEffect(() => {
    if ((params.get("q") ?? "") === debouncedSearch) return;
    const next = new URLSearchParams(params.toString());
    if (debouncedSearch) next.set("q", debouncedSearch);
    else next.delete("q");
    next.delete("cursor");
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }, [debouncedSearch, params, pathname, router]);

  const activityQuery = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "campaigns",
      campaignId,
      "activity",
      { type, status, from, to, q, cursor },
    ],
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? getCampaignActivity(activeWorkspaceId, campaignId, {
            type,
            status,
            startDate: from,
            endDate: to,
            q,
            limit: PAGE_SIZE,
            cursor,
          })
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId),
    placeholderData: keepPreviousData,
    refetchInterval: cursor ? false : REFRESH_MS,
  });

  function updateParam(key: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("cursor");
    const query = next.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  }

  function goToCursor(nextCursor: string | null) {
    const next = new URLSearchParams(params.toString());
    if (nextCursor) next.set("cursor", nextCursor);
    else next.delete("cursor");
    const query = next.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  }

  function clearFilters() {
    setSearchText("");
    router.push(pathname);
  }

  const data = activityQuery.data;
  const items = data?.items ?? [];
  const counts = data?.counts_by_type ?? null;

  // Counts only come with the first page; keep the filter labels stable while paging.
  const [knownCounts, setKnownCounts] = useState(counts);
  useEffect(() => {
    if (counts) setKnownCounts(counts);
  }, [counts]);
  const total = knownCounts
    ? Object.values(knownCounts).reduce((sum, n) => sum + n, 0)
    : null;

  const typeOptions: { value: TypeFilter; label: string }[] = [
    { value: "ALL", label: total === null ? "All" : `All ${formatNumber(total)}` },
    ...FEED_TYPES.map((value) => ({
      value,
      label: knownCounts
        ? `${TYPE_META[value].label} ${formatNumber(knownCounts[value] ?? 0)}`
        : TYPE_META[value].label,
    })),
    ...(knownCounts && knownCounts.COMPLAINED > 0
      ? [
          {
            value: "COMPLAINED" as const,
            label: `${TYPE_META.COMPLAINED.label} ${formatNumber(knownCounts.COMPLAINED)}`,
          },
        ]
      : []),
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl<TypeFilter>
          label="Activity type"
          options={typeOptions}
          value={type ?? "ALL"}
          onChange={(value) => updateParam("type", value === "ALL" ? "" : value)}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => activityQuery.refetch()}
          disabled={activityQuery.isFetching}
        >
          <RefreshCw
            className={`h-4 w-4 ${activityQuery.isFetching ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          Refresh
        </Button>
      </div>

      <section
        aria-label="Activity filters"
        className="rounded-xl border border-slate-200 bg-white p-4 shadow-card"
      >
        <div className="grid gap-3 md:grid-cols-[1fr_180px_150px_150px]">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-400"
              aria-hidden="true"
            />
            <Input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Search recipient email or subject"
              aria-label="Search activity"
              maxLength={100}
              className="pl-9"
            />
          </div>
          <Select
            aria-label="Activity status"
            value={status ?? ""}
            onChange={(event) => updateParam("status", event.target.value)}
          >
            <option value="">All statuses</option>
            {(Object.keys(STATUS_META) as CampaignActivityStatus[]).map((value) => (
              <option key={value} value={value}>
                {STATUS_META[value].label}
              </option>
            ))}
          </Select>
          <Input
            type="date"
            aria-label="From date"
            value={from}
            max={to || undefined}
            onChange={(event) => updateParam("from", event.target.value)}
          />
          <Input
            type="date"
            aria-label="To date"
            value={to}
            min={from || undefined}
            onChange={(event) => updateParam("to", event.target.value)}
          />
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
          <span>Dates are whole days in UTC. The newest activity is shown first.</span>
          {filtered ? (
            <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          ) : null}
        </div>
      </section>

      <section
        aria-label="Campaign activity"
        aria-busy={activityQuery.isFetching}
        className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card"
      >
        {activityQuery.isLoading ? (
          <div className="p-5">
            <LoadingBlock size="lg" />
          </div>
        ) : activityQuery.isError && !data ? (
          <div className="space-y-4 p-5">
            <Alert variant="error">
              {errorMessage(activityQuery.error, "Failed to load campaign activity.")}
            </Alert>
            <Button variant="outline" onClick={() => activityQuery.refetch()}>
              Retry
            </Button>
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            icon={<Send />}
            title={filtered ? "No matching activity" : "No activity yet"}
            description={
              filtered
                ? "Nothing in this campaign matches these filters. Try a different type, status or date range."
                : "Sends, opens, replies, bounces, unsubscribes and failures appear here as soon as they happen."
            }
            action={
              filtered ? (
                <Button variant="outline" onClick={clearFilters}>
                  Clear filters
                </Button>
              ) : null
            }
          />
        ) : (
          <>
            {activityQuery.isError ? (
              <div className="border-b border-slate-200 p-4">
                <Alert variant="error">
                  {errorMessage(
                    activityQuery.error,
                    "Could not refresh activity. Showing the last loaded results.",
                  )}
                </Alert>
              </div>
            ) : null}
            <ul className="divide-y divide-slate-100">
              {items.map((item) => (
                <ActivityRow key={item.id} item={item} />
              ))}
            </ul>
            <CursorPagination
              summary={`Showing ${items.length} event${items.length === 1 ? "" : "s"}`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(data?.next_cursor)}
              onFirst={() => goToCursor(null)}
              onNext={() => goToCursor(data?.next_cursor ?? null)}
            />
          </>
        )}
      </section>

      <p className="text-xs leading-5 text-slate-500">
        Opens are estimates: automated scanners and image prefetching are filtered out, and each
        email appears once with its open count.
        {data && !data.click_tracking_supported ? " Link clicks are not tracked." : null}
        {data && !data.delivery_confirmation_supported
          ? " Mail providers do not confirm delivery, so Sent means the email was accepted for delivery."
          : null}
      </p>
    </div>
  );
}
