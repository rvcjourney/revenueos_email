"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, CheckSquare, Megaphone, Plus, Search } from "lucide-react";

import {
  CampaignCard,
  CampaignCardSkeleton,
} from "@/components/campaigns/campaign-card";
import { CreateCampaignPanel } from "@/components/campaigns/create-campaign-panel";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { CursorPagination } from "@/components/ui/pagination";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { PageHeader } from "@/components/ui/page-header";
import { SelectionBar } from "@/components/ui/selection-bar";
import { useToast } from "@/components/ui/toast";
import { summarizeBulk } from "@/lib/bulk-summary";
import { bulkArchiveCampaigns, getCampaignCounts, listCampaigns } from "@/lib/campaigns-api";
import { errorMessage } from "@/lib/errors";
import { canDraftCampaign, canEraseData, canExecuteCampaign } from "@/lib/permissions";
import { useSelection } from "@/lib/use-selection";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";
import type { CampaignSort, CampaignStatus, CampaignType } from "@/types/domain";

const PAGE_SIZE = 25;
const STATUS_FILTERS: (CampaignStatus | "ALL")[] = [
  "ALL",
  "DRAFT",
  "SCHEDULED",
  "RUNNING",
  "PAUSED",
  "COMPLETED",
  "ARCHIVED",
];

const TYPE_OPTIONS: { value: CampaignType; label: string }[] = [
  { value: "STANDARD", label: "Standard" },
  { value: "HYPER_PERSONALIZED", label: "Hyper-personalized" },
];

const SORT_OPTIONS: { value: CampaignSort; label: string }[] = [
  { value: "updated_desc", label: "Last modified" },
  { value: "created_desc", label: "Newest" },
  { value: "name_asc", label: "Name A to Z" },
];
const DEFAULT_SORT: CampaignSort = "updated_desc";

function statusLabel(status: CampaignStatus | "ALL") {
  return status === "ALL" ? "Not archived" : status.charAt(0) + status.slice(1).toLowerCase();
}

export function CampaignsPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const [searchText, setSearchText] = useState(params.get("q") ?? "");

  const debouncedSearch = useDebouncedValue(searchText, 350);
  const cursor = params.get("cursor");
  const status = (params.get("status") as CampaignStatus | "ALL" | null) ?? "ALL";
  const type = TYPE_OPTIONS.find((o) => o.value === params.get("type"))?.value ?? null;
  const sort = SORT_OPTIONS.find((o) => o.value === params.get("sort"))?.value ?? DEFAULT_SORT;
  const mayDraft = canDraftCampaign(activeWorkspace?.role_code);
  const mayExecute = canExecuteCampaign(activeWorkspace?.role_code);
  const mayErase = canEraseData(activeWorkspace?.role_code);
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const selection = useSelection();
  const [selectMode, setSelectMode] = useState(false);
  const [confirmBulk, setConfirmBulk] = useState(false);

  useEffect(() => {
    const next = new URLSearchParams(params.toString());
    if (debouncedSearch) next.set("q", debouncedSearch);
    else next.delete("q");
    next.delete("cursor");
    const href = `${pathname}?${next.toString()}`;
    if ((params.get("q") ?? "") !== debouncedSearch) {
      router.replace(href.endsWith("?") ? pathname : href);
    }
  }, [debouncedSearch, params, pathname, router]);

  const campaignsQuery = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "campaigns",
      { cursor, q: params.get("q"), status, type, sort },
    ],
    queryFn: () =>
      activeWorkspaceId
        ? listCampaigns(activeWorkspaceId, {
            limit: PAGE_SIZE,
            cursor,
            q: params.get("q"),
            status: status === "ALL" ? null : status,
            type,
            sort,
          })
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId),
  });

  const campaigns = campaignsQuery.data?.items ?? [];

  // Tab counts are a nicety: without them (an older server, a failed request)
  // the tabs simply show no number.
  const countsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", "counts", { q: params.get("q"), type }],
    queryFn: () =>
      activeWorkspaceId
        ? getCampaignCounts(activeWorkspaceId, { q: params.get("q"), type })
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId),
    retry: false,
  });
  const counts = countsQuery.data;
  const statusOptions = STATUS_FILTERS.map((s) => ({
    value: s,
    label: statusLabel(s),
    count: counts ? (s === "ALL" ? counts.not_archived : (counts.by_status[s] ?? 0)) : undefined,
  }));

  // Changing a filter always starts again from the first page.
  function setParam(name: string, value: string | null) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(name, value);
    else next.delete(name);
    next.delete("cursor");
    const query = next.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  }

  // A different list (filter, search, page) must never leave hidden rows selected.
  const paramsKey = params.toString();
  const { clear: clearSelection } = selection;
  useEffect(() => {
    clearSelection();
  }, [status, cursor, paramsKey, clearSelection]);

  const bulkArchive = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId) return Promise.reject(new Error("No active workspace"));
      const items = campaigns
        .filter((c) => selection.selected.has(c.id))
        .map((c) => ({ id: c.id, expected_version: c.version }));
      return bulkArchiveCampaigns(activeWorkspaceId, items);
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "campaigns"] });
      toast(summarizeBulk(result, "Archived"), result.failed > 0 ? "error" : undefined);
      selection.clear();
      setConfirmBulk(false);
    },
    onError: (err) => {
      setConfirmBulk(false);
      toast(errorMessage(err), "error");
    },
  });
  const nextCursor = campaignsQuery.data?.next_cursor ?? null;
  const isFiltered = Boolean(debouncedSearch) || status !== "ALL" || type !== null;

  const createButton = mayDraft ? (
    <Button asChild>
      <Link href="/app/campaigns/new">
        <Plus className="h-4 w-4" aria-hidden="true" />
        Create Campaign
      </Link>
    </Button>
  ) : null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Outreach"
        title="Campaigns"
        description="Build and configure outreach campaigns, then track how they perform. Nothing sends until activation."
        actions={createButton}
      />

      <div className={cn("grid items-start gap-6", mayDraft && "xl:grid-cols-[minmax(0,1fr)_19rem]")}>
      {/* One surface for filter, search, list and paging. It must not clip
          overflow: the row action menus open past its edge. */}
      <section
        aria-label="Campaign list"
        className="min-w-0 rounded-xl border border-slate-200 bg-white shadow-card"
      >
        <SegmentedControl
          variant="underline"
          label="Filter by status"
          className="rounded-t-xl px-2 sm:px-3"
          options={statusOptions}
          value={status}
          onChange={(s) => setParam("status", s)}
        />

        <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 p-3 sm:px-4">
          <div className="relative min-w-0 basis-full min-[1700px]:basis-0 min-[1700px]:flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-400"
              aria-hidden="true"
            />
            <Input
              type="search"
              aria-label="Search campaigns by name"
              placeholder="Search by name..."
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              className="pl-9"
            />
          </div>
          <div className="min-w-[9.5rem] flex-1 sm:w-48 sm:flex-none">
          <Select
            aria-label="Filter by type"
            value={type ?? ""}
            onChange={(e) => setParam("type", e.target.value || null)}
          >
            <option value="">All types</option>
            {TYPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
          </div>
          <div className="min-w-[9.5rem] flex-1 sm:w-48 sm:flex-none">
          <Select
            aria-label="Sort campaigns"
            value={sort}
            onChange={(e) =>
              setParam("sort", e.target.value === DEFAULT_SORT ? null : e.target.value)
            }
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
          </div>
          {mayDraft && campaigns.length > 0 ? (
            <Button
              type="button"
              variant={selectMode ? "secondary" : "outline"}
              aria-pressed={selectMode}
              className="ml-auto shrink-0"
              onClick={() => {
                setSelectMode((on) => !on);
                selection.clear();
              }}
            >
              <CheckSquare className="h-4 w-4" aria-hidden="true" />
              {selectMode ? "Done selecting" : "Select"}
            </Button>
          ) : null}
        </div>

        {selectMode ? (
          // The bar renders nothing until something is selected.
          <div className="border-b border-slate-200 p-3 empty:hidden sm:px-4">
            <SelectionBar count={selection.count} onClear={selection.clear}>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirmBulk(true)}
              >
                <Archive className="h-4 w-4" aria-hidden="true" />
                Archive selected
              </Button>
            </SelectionBar>
          </div>
        ) : null}

        {campaignsQuery.isLoading ? (
          <div aria-busy="true" className="space-y-3 p-3 sm:p-4">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <CampaignCardSkeleton key={i} />
            ))}
          </div>
        ) : campaignsQuery.isError ? (
          <div className="space-y-3 p-4 sm:p-5">
            <Alert variant="error">{errorMessage(campaignsQuery.error)}</Alert>
            <Button variant="outline" size="sm" onClick={() => campaignsQuery.refetch()}>
              Try again
            </Button>
          </div>
        ) : campaigns.length === 0 ? (
          <EmptyState
            className="min-h-[320px] rounded-none rounded-b-xl border-0"
            icon={<Megaphone />}
            title={isFiltered ? "No campaigns match your filters" : "No campaigns yet"}
            description={
              isFiltered
                ? "Try a different search or status, or clear the filters to see every campaign."
                : "A campaign pairs an audience with an email sequence, sender mailboxes and a schedule. Nothing sends until you activate it."
            }
            action={
              !isFiltered ? (
                createButton
              ) : (
                <Button
                  variant="outline"
                  onClick={() => {
                    setSearchText("");
                    router.push(pathname);
                  }}
                >
                  Clear filters
                </Button>
              )
            }
          />
        ) : (
          <>
            <div className="space-y-3 p-3 sm:p-4">
              {activeWorkspaceId
                ? campaigns.map((c) => (
                    <CampaignCard
                      key={c.id}
                      campaign={c}
                      workspaceId={activeWorkspaceId}
                      mayDraft={mayDraft}
                      mayExecute={mayExecute}
                      mayErase={mayErase}
                      selectable={selectMode}
                      selected={selection.selected.has(c.id)}
                      onToggleSelect={selection.toggle}
                      density={mayDraft ? "tight" : "roomy"}
                    />
                  ))
                : null}
            </div>

            <CursorPagination
              summary={`Showing ${campaigns.length} campaign${campaigns.length === 1 ? "" : "s"}`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(nextCursor)}
              onFirst={() => {
                const next = new URLSearchParams(params.toString());
                next.delete("cursor");
                router.push(`${pathname}?${next.toString()}`);
              }}
              onNext={() => {
                if (nextCursor) {
                  const next = new URLSearchParams(params.toString());
                  next.set("cursor", nextCursor);
                  router.push(`${pathname}?${next.toString()}`);
                }
              }}
            />
          </>
        )}
      </section>
      {mayDraft ? (
        <div className="hidden xl:sticky xl:top-24 xl:block">
          <CreateCampaignPanel />
        </div>
      ) : null}
      </div>

      <ConfirmDialog
        open={confirmBulk}
        title={`Archive ${selection.count} campaign${selection.count === 1 ? "" : "s"}?`}
        description="Archived campaigns stop for good and can't be resumed or edited. Their history is kept. Running campaigns must be paused first and are skipped."
        confirmLabel="Archive"
        tone="danger"
        loading={bulkArchive.isPending}
        onConfirm={() => bulkArchive.mutate()}
        onCancel={() => setConfirmBulk(false)}
      />
    </div>
  );
}
