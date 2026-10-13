"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ListPlus,
  Loader2,
  Plus,
  RotateCcw,
  Search,
  ShieldOff,
  Trash2,
  UploadCloud,
  Users,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { CursorPagination } from "@/components/ui/pagination";
import { PageHeader } from "@/components/ui/page-header";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SelectionBar } from "@/components/ui/selection-bar";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { AddLeadDialog } from "@/components/leads/add-lead-dialog";
import { CreateListDialog } from "@/components/leads/create-list-dialog";
import { CreateListFromFilterDialog } from "@/components/leads/create-list-from-filter-dialog";
import { ImportLeadsDialog } from "@/components/leads/import-leads-dialog";
import { formatLocation } from "@/lib/lead-fields";
import { summarizeBulk } from "@/lib/bulk-summary";
import { BULK_DELETE_PHRASE, bulkEraseLeads } from "@/lib/erasure-api";
import { errorMessage } from "@/lib/errors";
import { bulkArchiveLeads, bulkUnarchiveLeads, listLeadLists, listLeads } from "@/lib/leads-api";
import { canEraseData, canManageContacts } from "@/lib/permissions";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { useSelection } from "@/lib/use-selection";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

function fullName(firstName: string | null, lastName: string | null) {
  return [firstName, lastName].filter(Boolean).join(" ") || "Unnamed lead";
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(
    new Date(value),
  );
}

export function LeadsPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const [searchText, setSearchText] = useState(params.get("q") ?? "");
  const [addLeadOpen, setAddLeadOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [createListOpen, setCreateListOpen] = useState(false);
  const [createFromSelectionOpen, setCreateFromSelectionOpen] = useState(false);

  const debouncedSearch = useDebouncedValue(searchText, 350);
  const cursor = params.get("cursor");
  const status = (params.get("status") as "ACTIVE" | "ARCHIVED" | "ALL" | null) ?? "ACTIVE";
  const listId = params.get("list_id");
  const mayManage = canManageContacts(activeWorkspace?.role_code);
  const mayErase = canEraseData(activeWorkspace?.role_code);
  const { toast } = useToast();
  const selection = useSelection();
  const [confirmBulkArchive, setConfirmBulkArchive] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeleteError, setBulkDeleteError] = useState<string | null>(null);

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

  const leadsQuery = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "leads",
      { cursor, q: params.get("q"), status, listId },
    ],
    queryFn: () =>
      listLeads(activeWorkspaceId!, {
        limit: PAGE_SIZE,
        cursor,
        q: params.get("q"),
        status,
        list_id: listId,
      }),
    enabled: Boolean(activeWorkspaceId),
  });

  const listsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "lead-lists", "picker"],
    queryFn: () => listLeadLists(activeWorkspaceId!, { limit: 100 }),
    enabled: Boolean(activeWorkspaceId),
  });

  const leads = leadsQuery.data?.items ?? [];
  const activeSelected = leads.filter(
    (lead) => selection.selected.has(lead.id) && !lead.archived_at,
  );
  const archivedSelected = leads.filter(
    (lead) => selection.selected.has(lead.id) && lead.archived_at,
  );

  // Already-erased leads are archived too, but there is nothing left to delete.
  const deletableSelected = archivedSelected.filter((lead) => !lead.erased_at);

  // A different list (filter, search, page) must never leave hidden rows selected.
  const paramsKey = params.toString();
  const { clear: clearSelection } = selection;
  useEffect(() => {
    clearSelection();
  }, [status, cursor, listId, paramsKey, clearSelection]);

  const bulkMutation = useMutation({
    mutationFn: ({ restore }: { restore: boolean }) => {
      const source = restore ? archivedSelected : activeSelected;
      const items = source.map((lead) => ({
        id: lead.id,
        expected_version: lead.version,
      }));
      return restore
        ? bulkUnarchiveLeads(activeWorkspaceId!, items)
        : bulkArchiveLeads(activeWorkspaceId!, items);
    },
    onSuccess: (result, { restore }) => {
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "leads"] });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-lists"],
      });
      toast(
        summarizeBulk(result, restore ? "Restored" : "Archived"),
        result.failed > 0 ? "error" : undefined,
      );
      selection.clear();
      setConfirmBulkArchive(false);
    },
    onError: (error) => {
      setConfirmBulkArchive(false);
      toast(errorMessage(error), "error");
    },
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: () =>
      bulkEraseLeads(
        activeWorkspaceId!,
        deletableSelected.map((lead) => lead.id),
        BULK_DELETE_PHRASE,
      ),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "leads"] });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-lists"],
      });
      setBulkDeleteOpen(false);
      selection.clear();
      toast(summarizeBulk(result, "Deleted"), result.failed > 0 ? "error" : undefined);
    },
    onError: (error) => {
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "leads"] });
      setBulkDeleteError(errorMessage(error));
    },
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
    router.push(`${pathname}?${next.toString()}`);
  }

  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="Audience"
        title="Leads"
        description="Workspace contacts for outreach and reusable lists."
        actions={
          <>
            <Button asChild variant="ghost">
              <Link href="/app/leads/lists">Lists</Link>
            </Button>
            {mayManage && leads.length > 0 ? (
              <Button type="button" variant="outline" onClick={() => setCreateListOpen(true)}>
                <ListPlus className="h-4 w-4" aria-hidden="true" />
                Create list from filter
              </Button>
            ) : null}
            {mayManage ? (
              <>
                <Button type="button" variant="outline" onClick={() => setImportOpen(true)}>
                  <UploadCloud className="h-4 w-4" aria-hidden="true" />
                  Import
                </Button>
                <Button type="button" onClick={() => setAddLeadOpen(true)}>
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  Add Lead
                </Button>
              </>
            ) : null}
          </>
        }
      />

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <div className="grid gap-3 md:grid-cols-[1fr_180px_220px]">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-400"
              aria-hidden="true"
            />
            <Input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Search name, email, company, title, or location"
              className="pl-9"
            />
          </div>
          <Select
            aria-label="Lead status"
            value={status}
            onChange={(event) => updateParam("status", event.target.value)}
          >
            <option value="ACTIVE">Active</option>
            <option value="ARCHIVED">Archived</option>
            <option value="ALL">All</option>
          </Select>
          <Select
            aria-label="Lead list filter"
            value={listId ?? ""}
            onChange={(event) => updateParam("list_id", event.target.value)}
          >
            <option value="">All lists</option>
            {(listsQuery.data?.items ?? []).map((list) => (
              <option key={list.id} value={list.id}>
                {list.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="mt-3">
          <Link
            href="/app/leads/suppression"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-slate-500 hover:text-slate-900"
          >
            <ShieldOff className="h-3.5 w-3.5" aria-hidden="true" />
            View suppression list
          </Link>
        </div>
      </section>

      {mayManage ? (
        <SelectionBar count={selection.count} onClear={selection.clear}>
          {activeSelected.length > 0 ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setCreateFromSelectionOpen(true)}
            >
              <ListPlus className="h-4 w-4" aria-hidden="true" />
              Create list from selected
            </Button>
          ) : null}
          {activeSelected.length > 0 ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setConfirmBulkArchive(true)}
            >
              <Archive className="h-4 w-4" aria-hidden="true" />
              Archive selected
            </Button>
          ) : null}
          {archivedSelected.length > 0 ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              loading={bulkMutation.isPending}
              onClick={() => bulkMutation.mutate({ restore: true })}
            >
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              Restore selected
            </Button>
          ) : null}
          {deletableSelected.length > 0 && mayErase ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="text-red-600 hover:text-red-700"
              onClick={() => {
                setBulkDeleteError(null);
                setBulkDeleteOpen(true);
              }}
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              Delete selected
            </Button>
          ) : null}
        </SelectionBar>
      ) : null}

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        {leadsQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : leadsQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(leadsQuery.error)}</Alert>
          </div>
        ) : leadsQuery.data?.items.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            icon={<Users />}
            title="No leads yet"
            description="Add a lead to start building this workspace's contact database, or import a CSV to bring in many at once."
            action={
              mayManage ? (
                <>
                  <Button onClick={() => setAddLeadOpen(true)}>
                    <Plus className="h-4 w-4" aria-hidden="true" />
                    Add lead
                  </Button>
                  <Button variant="outline" onClick={() => setImportOpen(true)}>
                    Import from CSV
                  </Button>
                </>
              ) : null
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    {mayManage ? (
                      <th className="w-10 px-4 py-3">
                        <input
                          type="checkbox"
                          aria-label="Select all leads on this page"
                          checked={leads.length > 0 && selection.count === leads.length}
                          onChange={(event) =>
                            event.target.checked
                              ? selection.setAll(leads.map((lead) => lead.id))
                              : selection.clear()
                          }
                          className="h-4 w-4 rounded border-slate-300 text-brand-600"
                        />
                      </th>
                    ) : null}
                    <th className="px-4 py-3">Lead</th>
                    <th className="px-4 py-3">Email</th>
                    <th className="px-4 py-3">Company</th>
                    <th className="px-4 py-3">Job title</th>
                    <th className="px-4 py-3">Location</th>
                    <th className="px-4 py-3">Lists</th>
                    <th className="px-4 py-3">Created</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {leadsQuery.data?.items.map((lead) => (
                    <tr key={lead.id} className="hover:bg-slate-50">
                      {mayManage ? (
                        <td className="w-10 px-4 py-3">
                          <input
                            type="checkbox"
                            aria-label={`Select ${lead.email}`}
                            checked={selection.selected.has(lead.id)}
                            onChange={() => selection.toggle(lead.id)}
                            className="h-4 w-4 rounded border-slate-300 text-brand-600"
                          />
                        </td>
                      ) : null}
                      <td className="px-4 py-3 font-medium text-slate-900">
                        <Link
                          href={`/app/leads/${lead.id}`}
                          className="inline-flex items-center gap-3 hover:text-brand-700"
                        >
                          <span
                            aria-hidden="true"
                            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-50 text-[11px] font-bold uppercase text-brand-700"
                          >
                            {lead.erased_at
                              ? "–"
                              : (lead.first_name?.[0] ?? lead.email[0] ?? "") +
                                (lead.last_name?.[0] ?? "")}
                          </span>
                          {lead.erased_at
                            ? "Erased lead"
                            : fullName(lead.first_name, lead.last_name)}
                        </Link>
                        {lead.archived_at ? (
                          <span className="ml-2 inline-flex items-center rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-600">
                            {lead.erased_at ? "Erased" : "Archived"}
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-3 text-slate-700">{lead.email}</td>
                      <td className="px-4 py-3 text-slate-700">
                        {lead.company ?? "No company"}
                      </td>
                      <td className="px-4 py-3 text-slate-700">{lead.title ?? "—"}</td>
                      <td className="px-4 py-3 text-slate-700">
                        {formatLocation(lead) || "—"}
                      </td>
                      <td className="px-4 py-3 text-slate-700">{lead.list_count}</td>
                      <td className="px-4 py-3 text-slate-700">
                        {formatDate(lead.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CursorPagination
              summary={`Showing ${leads.length} lead${leads.length === 1 ? "" : "s"}`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(leadsQuery.data?.next_cursor)}
              onFirst={() => goToCursor(null)}
              onNext={() => goToCursor(leadsQuery.data?.next_cursor ?? null)}
            />
          </>
        )}
      </section>
      <ConfirmDialog
        open={confirmBulkArchive}
        title={`Archive ${activeSelected.length} lead${activeSelected.length === 1 ? "" : "s"}?`}
        description="Archived leads are left out of new campaigns and lists. You can restore them later. Their history is kept."
        confirmLabel="Archive"
        tone="danger"
        loading={bulkMutation.isPending}
        onCancel={() => setConfirmBulkArchive(false)}
        onConfirm={() => bulkMutation.mutate({ restore: false })}
      />
      <TypeToConfirmDialog
        open={bulkDeleteOpen}
        title={`Delete ${deletableSelected.length} archived lead${deletableSelected.length === 1 ? "" : "s"} permanently?`}
        description="Everything personal about these people is erased in every campaign, list and conversation, and they are removed from all lists. An empty record remains so campaign statistics still add up, and anyone who unsubscribed stays blocked from future emails. Selected leads that aren't archived are left alone. This can't be undone."
        phrase={BULK_DELETE_PHRASE}
        confirmLabel="Delete permanently"
        loading={bulkDeleteMutation.isPending}
        error={bulkDeleteError}
        onCancel={() => setBulkDeleteOpen(false)}
        onConfirm={() => bulkDeleteMutation.mutate()}
      />
      <AddLeadDialog open={addLeadOpen} onOpenChange={setAddLeadOpen} />
      <ImportLeadsDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        defaultImportKind="LEADS"
        onImported={(job) => router.push(`/app/leads/imports/${job.id}`)}
      />
      <CreateListDialog
        open={createFromSelectionOpen}
        onOpenChange={setCreateFromSelectionOpen}
        preselectedLeadIds={activeSelected.map((lead) => lead.id)}
        onCreated={selection.clear}
      />
      <CreateListFromFilterDialog
        open={createListOpen}
        onOpenChange={setCreateListOpen}
        filters={{ q: params.get("q"), status, listId }}
      />
    </main>
  );
}
