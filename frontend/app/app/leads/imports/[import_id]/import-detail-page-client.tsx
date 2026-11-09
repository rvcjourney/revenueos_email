"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Loader2, RefreshCw, Trash2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ImportStatusBadge, StatusBadge } from "@/components/ui/status-badge";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import { ApiError } from "@/lib/api-client";
import { IMPORT_CONFIRMATION, purgeImport } from "@/lib/erasure-api";
import { getImport, listImportRowResults } from "@/lib/imports-api";
import { canEraseData } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 50;

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return "We couldn't complete that request. Please try again.";
}

export function ImportDetailPageClient({ importId }: { importId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const queryClient = useQueryClient();
  const mayErase = canEraseData(activeWorkspace?.role_code);
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [purgeError, setPurgeError] = useState<string | null>(null);

  const cursor = params.get("cursor");

  const importQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "import", importId],
    queryFn: () => getImport(activeWorkspaceId!, importId),
    enabled: Boolean(activeWorkspaceId),
    // Auto-refresh if pending or processing
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "PENDING" || status === "PROCESSING") return 3000;
      return false;
    },
  });

  const resultsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "import", importId, "results", { cursor }],
    queryFn: () =>
      listImportRowResults(activeWorkspaceId!, importId, {
        limit: PAGE_SIZE,
        cursor,
      }),
    enabled: Boolean(activeWorkspaceId),
    refetchInterval: importQuery.data?.status === "PROCESSING" ? 5000 : false,
  });

  const purgeMutation = useMutation({
    mutationFn: () => purgeImport(activeWorkspaceId!, importId, IMPORT_CONFIRMATION),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "imports"] });
      router.push("/app/leads/imports");
    },
    onError: (error) => setPurgeError(errorMessage(error)),
  });

  function goToCursor(nextCursor: string | null) {
    const next = new URLSearchParams(params.toString());
    if (nextCursor) next.set("cursor", nextCursor);
    else next.delete("cursor");
    router.push(`/app/leads/imports/${importId}?${next.toString()}`);
  }

  const job = importQuery.data;
  const isFinished = job ? job.status !== "PENDING" && job.status !== "PROCESSING" : false;

  return (
    <main className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" asChild>
          <Link href="/app/leads/imports">
            <ChevronLeft className="h-5 w-5" />
            <span className="sr-only">Back to imports</span>
          </Link>
        </Button>
        <div>
          <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900">
            Import Details
          </h1>
          {job && (
            <p className="mt-2 text-sm text-slate-500">
              Started {formatDate(job.created_at)}
            </p>
          )}
        </div>
      </div>

      {importQuery.error ? (
        <Alert>{errorMessage(importQuery.error)}</Alert>
      ) : null}

      {job && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-card">
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <dt className="text-sm font-medium text-slate-500">Status</dt>
              <dd className="mt-1">
                <ImportStatusBadge status={job.status} />
                {(job.status === "PENDING" || job.status === "PROCESSING") && (
                  <Loader2 className="ml-2 inline h-4 w-4 animate-spin text-slate-400" />
                )}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-slate-500">Processed</dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">
                {job.processed_rows} / {job.total_rows ?? "?"}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-slate-500">Accepted</dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">{job.accepted_rows}</dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-slate-500">Rejected / Duplicates</dt>
              <dd className="mt-1 text-sm font-medium text-slate-900">
                {job.rejected_rows} / {job.duplicate_rows}
              </dd>
            </div>
          </dl>

          {job.failure_summary && (
            <div className="mt-4 rounded-md bg-red-50 p-4">
              <h3 className="text-sm font-medium text-red-800">Job Failed</h3>
              <p className="mt-1 text-sm text-red-700">{job.failure_summary}</p>
            </div>
          )}
        </section>
      )}

      {job && isFinished && mayErase ? (
        <section className="rounded-xl border border-red-200 bg-white p-5 shadow-card">
          <h2 className="text-lg font-semibold tracking-normal text-slate-900">
            Delete this import
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">
            Deletes the import record, its row results and the uploaded file. The leads it
            created are not deleted.
          </p>
          <Button
            type="button"
            variant="outline"
            className="mt-4 border-red-200 text-red-600 hover:border-red-300 hover:bg-red-50 hover:text-red-700"
            onClick={() => {
              setPurgeError(null);
              setConfirmPurge(true);
            }}
          >
            <Trash2 className="h-4 w-4" aria-hidden="true" />
            Delete import…
          </Button>
        </section>
      ) : null}

      <TypeToConfirmDialog
        open={confirmPurge}
        title="Delete this import?"
        description="The record, its row results and the uploaded file are deleted for good. The leads it created stay. This can't be undone."
        phrase={IMPORT_CONFIRMATION}
        confirmLabel="Delete import"
        loading={purgeMutation.isPending}
        error={purgeError}
        onConfirm={() => purgeMutation.mutate()}
        onCancel={() => setConfirmPurge(false)}
      />

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        <div className="border-b border-slate-200 bg-slate-50 px-4 py-3 flex justify-between items-center">
          <h2 className="text-sm font-medium text-slate-900">Row Results</h2>
          <Button variant="ghost" size="icon" onClick={() => resultsQuery.refetch()}>
            <RefreshCw className={`h-4 w-4 text-slate-500 ${resultsQuery.isFetching ? 'animate-spin' : ''}`} />
          </Button>
        </div>

        {resultsQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : resultsQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(resultsQuery.error)}</Alert>
          </div>
        ) : resultsQuery.data?.items.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-sm text-slate-500">No row results available yet.</p>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Row</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Details</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {resultsQuery.data?.items.map((row) => (
                    <tr key={row.row_number} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-medium text-slate-900">
                        {row.row_number}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        <StatusBadge
                          tone={row.status === "ACCEPTED" ? "success" : row.status === "REJECTED" ? "danger" : "neutral"}
                        >
                          {row.status}
                        </StatusBadge>
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {row.validation_reason || "-"}
                      </td>
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
                disabled={!resultsQuery.data?.next_cursor}
                onClick={() => goToCursor(resultsQuery.data?.next_cursor ?? null)}
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
