"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Loader2, Pencil, Plus, Trash2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CursorPagination } from "@/components/ui/pagination";
import { PageHeader } from "@/components/ui/page-header";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { AddListMembersDialog } from "@/components/leads/add-list-members-dialog";
import { errorMessage } from "@/lib/errors";
import {
  archiveLeadList,
  getLeadList,
  listLeadListMembers,
  removeLeadListMember,
  updateLeadList,
} from "@/lib/leads-api";
import { canManageContacts } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

function fullName(firstName: string | null, lastName: string | null) {
  return [firstName, lastName].filter(Boolean).join(" ") || "Unnamed lead";
}

export function LeadListDetailClient({ listId }: { listId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [addMembersOpen, setAddMembersOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursor = params.get("cursor");
  const mayManage = canManageContacts(activeWorkspace?.role_code);

  const listQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "lead-list", listId],
    queryFn: () => getLeadList(activeWorkspaceId!, listId),
    enabled: Boolean(activeWorkspaceId),
  });

  const membersQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "lead-list-members", listId, { cursor }],
    queryFn: () =>
      listLeadListMembers(activeWorkspaceId!, listId, {
        limit: PAGE_SIZE,
        cursor,
      }),
    enabled: Boolean(activeWorkspaceId),
  });

  useEffect(() => {
    if (listQuery.data && !editing) setName(listQuery.data.name);
  }, [listQuery.data, editing]);

  const renameMutation = useMutation({
    mutationFn: () =>
      updateLeadList(activeWorkspaceId!, listId, {
        name,
        expected_version: listQuery.data!.version,
      }),
    onSuccess: () => {
      setEditing(false);
      setError(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-list", listId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-lists"],
      });
    },
    onError: (err) => setError(errorMessage(err)),
  });

  const archiveMutation = useMutation({
    mutationFn: () =>
      archiveLeadList(activeWorkspaceId!, listId, listQuery.data!.version),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-lists"],
      });
      router.push("/app/leads/lists");
    },
    onError: (err) => setError(errorMessage(err)),
  });

  const removeMutation = useMutation({
    mutationFn: (leadId: string) =>
      removeLeadListMember(activeWorkspaceId!, listId, leadId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-list", listId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-list-members", listId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "leads"],
      });
    },
    onError: (err) => setError(errorMessage(err)),
  });

  function goToCursor(nextCursor: string | null) {
    const next = new URLSearchParams(params.toString());
    if (nextCursor) next.set("cursor", nextCursor);
    else next.delete("cursor");
    const query = next.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  }

  if (listQuery.isLoading) {
    return (
      <main className="grid min-h-80 place-items-center">
        <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
      </main>
    );
  }

  if (listQuery.error) {
    return (
      <main className="space-y-4">
        <Button asChild variant="ghost">
          <Link href="/app/leads/lists">Back to lists</Link>
        </Button>
        <Alert>{errorMessage(listQuery.error)}</Alert>
      </main>
    );
  }

  const list = listQuery.data;
  if (!list) return null;

  return (
    <main className="space-y-6">
      <PageHeader
        title={list.name}
        description={`${list.member_count} members`}
        back={{ href: "/app/leads/lists", label: "Back to lists" }}
        actions={
          mayManage && !list.archived_at ? (
            <>
              <Button type="button" onClick={() => setAddMembersOpen(true)}>
                <Plus className="h-4 w-4" aria-hidden="true" />
                Add leads
              </Button>
              <Button type="button" variant="ghost" onClick={() => setEditing(true)}>
                <Pencil className="h-4 w-4" aria-hidden="true" />
                Rename
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={archiveMutation.isPending}
                onClick={() => archiveMutation.mutate()}
              >
                <Archive className="h-4 w-4" aria-hidden="true" />
                Archive
              </Button>
            </>
          ) : null
        }
      />

      {error ? <Alert>{error}</Alert> : null}

      {editing && mayManage ? (
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <form
            className="flex max-w-lg items-start gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              renameMutation.mutate();
            }}
          >
            <Field id="rename-list" label="List name" required className="flex-1">
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={renameMutation.isPending}
                required
              />
            </Field>
            <Button type="submit" disabled={renameMutation.isPending}>
              {renameMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              Save
            </Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </form>
        </section>
      ) : null}

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        {membersQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : membersQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(membersQuery.error)}</Alert>
          </div>
        ) : membersQuery.data?.items.length === 0 ? (
          <div className="p-8 text-center">
            <h2 className="text-lg font-semibold text-slate-900">
              No leads in this list
            </h2>
            {mayManage && !list.archived_at ? (
              <Button className="mt-4" onClick={() => setAddMembersOpen(true)}>
                <Plus className="h-4 w-4" aria-hidden="true" />
                Add leads
              </Button>
            ) : null}
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Lead</th>
                    <th className="px-4 py-3">Email</th>
                    <th className="px-4 py-3">Company</th>
                    <th className="px-4 py-3">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {membersQuery.data?.items.map((member) => (
                    <tr key={member.lead.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-medium text-slate-900">
                        <Link href={`/app/leads/${member.lead.id}`}>
                          {fullName(member.lead.first_name, member.lead.last_name)}
                        </Link>
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {member.lead.email}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {member.lead.company ?? "No company"}
                      </td>
                      <td className="px-4 py-3">
                        {mayManage && !list.archived_at ? (
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            aria-label={`Remove ${member.lead.email}`}
                            disabled={removeMutation.isPending}
                            onClick={() => removeMutation.mutate(member.lead.id)}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CursorPagination
              summary={`Showing ${membersQuery.data?.items.length ?? 0} member${
                (membersQuery.data?.items.length ?? 0) === 1 ? "" : "s"
              }`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(membersQuery.data?.next_cursor)}
              onFirst={() => goToCursor(null)}
              onNext={() => goToCursor(membersQuery.data?.next_cursor ?? null)}
            />
          </>
        )}
      </section>
      <AddListMembersDialog
        open={addMembersOpen}
        onOpenChange={setAddMembersOpen}
        listId={listId}
        listName={list.name}
      />
    </main>
  );
}
