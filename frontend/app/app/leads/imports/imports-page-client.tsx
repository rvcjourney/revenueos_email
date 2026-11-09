"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus, UploadCloud } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { ImportStatusBadge } from "@/components/ui/status-badge";
import { errorMessage } from "@/lib/errors";
import { listImports } from "@/lib/imports-api";
import { canManageContacts } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function ImportsPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();

  const cursor = params.get("cursor");
  const mayManage = canManageContacts(activeWorkspace?.role_code);

  const importsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "imports", { cursor }],
    queryFn: () =>
      listImports(activeWorkspaceId!, {
        limit: PAGE_SIZE,
        cursor,
      }),
    enabled: Boolean(activeWorkspaceId),
  });

  function goToCursor(nextCursor: string | null) {
    const next = new URLSearchParams(params.toString());
    if (nextCursor) next.set("cursor", nextCursor);
    else next.delete("cursor");
    router.push(`${pathname}?${next.toString()}`);
  }

  return (
    <main className="space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-[0.08em] text-brand-600">Audience</p>
          <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900">
            Imports
          </h1>
          <p className="mt-2 text-sm text-slate-500">
            History of CSV imports for leads and suppressions.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {mayManage ? (
            <Button asChild>
              <Link href="/app/leads/imports/new">
                <Plus className="h-4 w-4 mr-2" aria-hidden="true" />
                New Import
              </Link>
            </Button>
          ) : null}
        </div>
      </div>

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        {importsQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : importsQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(importsQuery.error)}</Alert>
          </div>
        ) : importsQuery.data?.items.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            icon={<UploadCloud />}
            title="No imports yet"
            description="Upload a CSV file to add multiple leads or suppressions at once."
            action={
              <Button asChild>
                <Link href="/app/leads/imports/new">
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  New import
                </Link>
              </Button>
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Date</th>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Processed</th>
                    <th className="px-4 py-3">Accepted</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {importsQuery.data?.items.map((job) => (
                    <tr key={job.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-medium text-slate-900">
                        <Link href={`/app/leads/imports/${job.id}`}>
                          {formatDate(job.created_at)}
                        </Link>
                      </td>
                      <td className="px-4 py-3 text-slate-700">{job.import_kind}</td>
                      <td className="px-4 py-3 text-slate-700">
                        <ImportStatusBadge status={job.status} />
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {job.processed_rows} / {job.total_rows ?? "?"}
                      </td>
                      <td className="px-4 py-3 text-slate-700">{job.accepted_rows}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-slate-200 p-3">
              <Button
                type="button"
                variant="ghost"
                disabled={!cursor}
                onClick={() => goToCursor(null)}
              >
                First
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={!importsQuery.data?.next_cursor}
                onClick={() => goToCursor(importsQuery.data?.next_cursor ?? null)}
              >
                Next
              </Button>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
