"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ListChecks, Loader2, Plus, RotateCcw, Trash2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { CursorPagination } from "@/components/ui/pagination";
import { PageHeader } from "@/components/ui/page-header";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SelectionBar } from "@/components/ui/selection-bar";
import { useToast } from "@/components/ui/toast";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import { CreateListDialog } from "@/components/leads/create-list-dialog";
import { summarizeBulk } from "@/lib/bulk-summary";
import { errorMessage } from "@/lib/errors";
import { BULK_DELETE_PHRASE, bulkPurgeLeadLists, purgeLeadList } from "@/lib/erasure-api";
import {
  archiveLeadList,
  bulkArchiveLeadLists,
  bulkUnarchiveLeadLists,
  listLeadLists,
  unarchiveLeadList,
} from "@/lib/leads-api";
import { canEraseData, canManageContacts } from "@/lib/permissions";
import { useSelection } from "@/lib/use-selection";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(
    new Date(value),
  );
}

export function LeadListsPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const [createOpen, setCreateOpen] = useState(false);
  const cursor = params.get("cursor");
  const status = (params.get("status") as "ACTIVE" | "ARCHIVED" | "ALL" | null) ?? "ACTIVE";
  const mayManage = canManageContacts(activeWorkspace?.role_code);
  const mayErase = canEraseData(activeWorkspace?.role_code);
  const { toast } = useToast();
  const selection = useSelection();
  const [confirmBulkArchive, setConfirmBulkArchive] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // `version` is the row's version when it was still active; `archived` flips to
  // true once the archive step succeeded so a retry doesn't archive twice.
  const [purgeTarget, setPurgeTarget] = useState<{
    id: string;
    name: string;
    version: number;
    archived: boolean;
  } | null>(null);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeleteError, setBulkDeleteError] = useState<string | null>(null);

  const listsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "lead-lists", { cursor, status }],
    queryFn: () =>
      listLeadLists(activeWorkspaceId!, { limit: PAGE_SIZE, cursor, status }),
    enabled: Boolean(activeWorkspaceId),
  });

  const lists = listsQuery.data?.items ?? [];
  const activeSelected = lists.filter((l) => selection.selected.has(l.id) && !l.archived_at);
  const archivedSelected = lists.filter((l) => selection.selected.has(l.id) && l.archived_at);

  // A different list (filter, page) must never leave hidden rows selected.
  const paramsKey = params.toString();
  const { clear: clearSelection } = selection;
  useEffect(() => {
    clearSelection();
  }, [status, cursor, paramsKey, clearSelection]);

  const invalidateLists = () =>
    queryClient.invalidateQueries({
      queryKey: ["workspace", activeWorkspaceId, "lead-lists"],
    });

  const bulkMutation = useMutation({
    mutationFn: ({ restore }: { restore: boolean }) => {
      const source = restore ? archivedSelected : activeSelected;
      const items = source.map((l) => ({ id: l.id, expected_version: l.version }));
      return restore
        ? bulkUnarchiveLeadLists(activeWorkspaceId!, items)
        : bulkArchiveLeadLists(activeWorkspaceId!, items);
    },
    onSuccess: (result, { restore }) => {
      invalidateLists();
      toast(
        summarizeBulk(result, restore ? "Restored" : "Archived"),
        result.failed > 0 ? "error" : undefined,
      );
      selection.clear();
      setConfirmBulkArchive(false);
    },
    onError: (error) => {
      setConfirmBulkArchive(false);
      setActionError(errorMessage(error));
    },
  });

  const restoreMutation = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      unarchiveLeadList(activeWorkspaceId!, id, version),
    onSuccess: () => {
      invalidateLists();
      toast("List restored.");
    },
    onError: (error) => setActionError(errorMessage(error)),
  });

  const purgeMutation = useMutation({
    mutationFn: async (target: NonNullable<typeof purgeTarget>) => {
      // The database only deletes archived lists, so an active list is archived
      // first; both calls are the same single-item endpoints used elsewhere.
      if (!target.archived) {
        await archiveLeadList(activeWorkspaceId!, target.id, target.version);
        setPurgeTarget({ ...target, archived: true });
      }
      return purgeLeadList(activeWorkspaceId!, target.id, target.name);
    },
    onSuccess: () => {
      invalidateLists();
      setPurgeTarget(null);
      toast("List deleted. The leads in it were not deleted.");
    },
    onError: (error) => {
      // The list may have been archived before the delete was refused.
      invalidateLists();
      setPurgeError(errorMessage(error));
    },
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: () =>
      bulkPurgeLeadLists(
        activeWorkspaceId!,
        archivedSelected.map((l) => l.id),
        BULK_DELETE_PHRASE,
      ),
    onSuccess: (result) => {
      invalidateLists();
      setBulkDeleteOpen(false);
      selection.clear();
      toast(
        summarizeBulk(result, "Deleted"),
        result.failed > 0 ? "error" : undefined,
      );
    },
    onError: (error) => {
      invalidateLists();
      setBulkDeleteError(errorMessage(error));
    },
  });

  function updateStatus(value: string) {
    const next = new URLSearchParams(params.toString());
    if (value && value !== "ACTIVE") next.set("status", value);
    else next.delete("status");
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

  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="Audience"
        title="Lead Lists"
        description="Reusable collections of workspace leads."
        back={{ href: "/app/leads", label: "Back to leads" }}
        actions={
          mayManage ? (
            <Button type="button" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              Create List
            </Button>
          ) : null
        }
      />

      {actionError ? <Alert>{actionError}</Alert> : null}

      <div className="w-full sm:w-48">
        <Select
          aria-label="List status"
          value={status}
          onChange={(event) => updateStatus(event.target.value)}
        >
          <option value="ACTIVE">Active</option>
          <option value="ARCHIVED">Archived</option>
          <option value="ALL">All</option>
        </Select>
      </div>

      {mayManage ? (
        <SelectionBar count={selection.count} onClear={selection.clear}>
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
          {archivedSelected.length > 0 && mayErase ? (
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
        {listsQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : listsQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(listsQuery.error)}</Alert>
          </div>
        ) : listsQuery.data?.items.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            icon={<ListChecks />}
            title="No lists yet"
            description="Create a list to organize leads for later campaign selection."
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
                          aria-label="Select all lists on this page"
                          checked={lists.length > 0 && selection.count === lists.length}
                          onChange={(event) =>
                            event.target.checked
                              ? selection.setAll(lists.map((l) => l.id))
                              : selection.clear()
                          }
                          className="h-4 w-4 rounded border-slate-300 text-brand-600"
                        />
                      </th>
                    ) : null}
                    <th className="px-4 py-3">Name</th>
                    <th className="px-4 py-3">Members</th>
                    <th className="px-4 py-3">Created</th>
                    {mayManage ? <th className="px-4 py-3 text-right">Actions</th> : null}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {lists.map((list) => (
                    <tr key={list.id} className="hover:bg-slate-50">
                      {mayManage ? (
                        <td className="w-10 px-4 py-3">
                          <input
                            type="checkbox"
                            aria-label={`Select ${list.name}`}
                            checked={selection.selected.has(list.id)}
                            onChange={() => selection.toggle(list.id)}
                            className="h-4 w-4 rounded border-slate-300 text-brand-600"
                          />
                        </td>
                      ) : null}
                      <td className="px-4 py-3 font-medium text-slate-900">
                        <Link href={`/app/leads/lists/${list.id}`}>
                          {list.name}
                        </Link>
                        {list.archived_at ? (
                          <span className="ml-2 inline-flex items-center rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-600">
                            Archived
                          </span>
                        ) : null}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {list.member_count}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {formatDate(list.created_at)}
                      </td>
                      {mayManage ? (
                        <td className="whitespace-nowrap px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1.5">
                            {list.archived_at ? (
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                disabled={restoreMutation.isPending}
                                onClick={() =>
                                  restoreMutation.mutate({ id: list.id, version: list.version })
                                }
                              >
                                <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                                <span className="ml-1">Restore</span>
                              </Button>
                            ) : null}
                            {mayErase ? (
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="text-red-600 hover:text-red-700"
                                onClick={() => {
                                  setPurgeError(null);
                                  setPurgeTarget({
                                    id: list.id,
                                    name: list.name,
                                    version: list.version,
                                    archived: Boolean(list.archived_at),
                                  });
                                }}
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                                <span className="sr-only">Delete {list.name} permanently</span>
                              </Button>
                            ) : null}
                          </div>
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CursorPagination
              summary={`Showing ${lists.length} list${lists.length === 1 ? "" : "s"}`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(listsQuery.data?.next_cursor)}
              onFirst={() => goToCursor(null)}
              onNext={() => goToCursor(listsQuery.data?.next_cursor ?? null)}
            />
          </>
        )}
      </section>
      <ConfirmDialog
        open={confirmBulkArchive}
        title={`Archive ${activeSelected.length} list${activeSelected.length === 1 ? "" : "s"}?`}
        description="Archived lists can't be used for new campaigns. The leads in them are not affected. You can restore them later."
        confirmLabel="Archive"
        tone="danger"
        loading={bulkMutation.isPending}
        onCancel={() => setConfirmBulkArchive(false)}
        onConfirm={() => bulkMutation.mutate({ restore: false })}
      />
      <TypeToConfirmDialog
        open={bulkDeleteOpen}
        title={`Delete ${archivedSelected.length} archived list${archivedSelected.length === 1 ? "" : "s"} permanently?`}
        description="The lists are deleted for good. The leads in them are not deleted. A list that was used to build a campaign audience can't be deleted and stays archived; the others are still deleted. Selected lists that aren't archived are left alone. This can't be undone."
        phrase={BULK_DELETE_PHRASE}
        confirmLabel="Delete permanently"
        loading={bulkDeleteMutation.isPending}
        error={bulkDeleteError}
        onCancel={() => setBulkDeleteOpen(false)}
        onConfirm={() => bulkDeleteMutation.mutate()}
      />
      <CreateListDialog open={createOpen} onOpenChange={setCreateOpen} />
      <TypeToConfirmDialog
        open={purgeTarget !== null}
        title={`Delete "${purgeTarget?.name ?? ""}" permanently?`}
        description="The list is deleted for good. The leads in it are not deleted. A list that was used to build a campaign audience can't be deleted and stays archived instead. This can't be undone."
        phrase={purgeTarget?.name ?? ""}
        confirmLabel="Delete permanently"
        loading={purgeMutation.isPending}
        error={purgeError}
        onCancel={() => setPurgeTarget(null)}
        onConfirm={() => purgeTarget && purgeMutation.mutate(purgeTarget)}
      />
    </main>
  );
}
