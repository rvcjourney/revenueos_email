"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Search, ShieldOff, Trash2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { CursorPagination } from "@/components/ui/pagination";
import { PageHeader } from "@/components/ui/page-header";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { errorMessage } from "@/lib/errors";
import {
  createManualSuppression,
  listSuppressions,
  releaseManualSuppression,
} from "@/lib/suppression-api";
import { canManageContacts } from "@/lib/permissions";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

// UNSUBSCRIBE/HARD_BOUNCE/COMPLAINT are permanent signals from the recipient
// or provider and can never be released from this UI; only a MANUAL entry
// (removable, set server-side) can be.
const PERMANENT_REASON_LABEL: Record<string, string> = {
  UNSUBSCRIBE: "Unsubscribed by the recipient — permanent.",
  HARD_BOUNCE: "Hard bounce reported by the provider — permanent.",
  COMPLAINT: "Spam complaint reported by the provider — permanent.",
};

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(
    new Date(value),
  );
}

export function SuppressionPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const { toast } = useToast();

  const [searchText, setSearchText] = useState(params.get("q") ?? "");
  const [formOpen, setFormOpen] = useState(false);
  const [emailToSuppress, setEmailToSuppress] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const debouncedSearch = useDebouncedValue(searchText, 350);
  const cursor = params.get("cursor");
  const mayManage = canManageContacts(activeWorkspace?.role_code);

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

  const suppressionQuery = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "suppressions",
      { cursor, q: params.get("q") },
    ],
    queryFn: () =>
      listSuppressions(activeWorkspaceId!, {
        limit: PAGE_SIZE,
        cursor,
        q: params.get("q"),
      }),
    enabled: Boolean(activeWorkspaceId),
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createManualSuppression(activeWorkspaceId!, { email: emailToSuppress }),
    onSuccess: () => {
      setEmailToSuppress("");
      setFormOpen(false);
      setFormError(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "suppressions"],
      });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const [releaseTarget, setReleaseTarget] = useState<string | null>(null);
  const releaseMutation = useMutation({
    mutationFn: (suppressionId: string) =>
      releaseManualSuppression(activeWorkspaceId!, suppressionId, {
        reason: "Released via UI",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "suppressions"],
      });
      setReleaseTarget(null);
      toast("Suppression released.");
    },
    // Keep the dialog open on failure (only onSuccess clears releaseTarget)
    // so a failed release surfaces instead of silently closing.
    onError: (error) => toast(errorMessage(error), "error"),
  });

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
        title="Suppression List"
        description="Contacts that will never receive outreach (unsubscribed, bounced, or manually suppressed). Unsubscribe, bounce and complaint entries are permanent; only manually suppressed addresses can be released."
        back={{ href: "/app/leads", label: "Back to leads" }}
        actions={
          mayManage ? (
            <Button type="button" onClick={() => setFormOpen((open) => !open)}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              Suppress Email
            </Button>
          ) : null
        }
      />

      {formOpen && mayManage ? (
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <h2 className="text-lg font-semibold tracking-normal text-slate-900">
            Manually Suppress Email
          </h2>
          {formError ? (
            <div className="mt-3">
              <Alert>{formError}</Alert>
            </div>
          ) : null}
          <form
            className="mt-4 grid gap-4 md:grid-cols-2"
            onSubmit={(event) => {
              event.preventDefault();
              setFormError(null);
              createMutation.mutate();
            }}
          >
            <Field id="suppress-email" label="Email Address" required className="md:col-span-2">
              <Input
                value={emailToSuppress}
                onChange={(event) => setEmailToSuppress(event.target.value)}
                disabled={createMutation.isPending}
                required
                type="email"
                placeholder="email@example.com"
              />
            </Field>
            
            <div className="flex items-center gap-2 md:col-span-2">
              <Button type="submit" disabled={createMutation.isPending || !emailToSuppress}>
                {createMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Suppress
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={createMutation.isPending}
                onClick={() => setFormOpen(false)}
              >
                Cancel
              </Button>
            </div>
          </form>
        </section>
      ) : null}

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <div className="grid gap-3 md:grid-cols-[1fr]">
          <div className="relative max-w-sm">
            <Search
              className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-slate-400"
              aria-hidden="true"
            />
            <Input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="Search by email"
              className="pl-9"
            />
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card">
        {suppressionQuery.isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : suppressionQuery.error ? (
          <div className="p-5">
            <Alert>{errorMessage(suppressionQuery.error)}</Alert>
          </div>
        ) : suppressionQuery.data?.items.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            icon={<ShieldOff />}
            title="No suppressions"
            description="There are no suppressed emails matching your criteria. Suppressed addresses are never emailed."
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Email</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Reason</th>
                    <th className="px-4 py-3">First Observed</th>
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {suppressionQuery.data?.items.map((suppression) => (
                    <tr key={suppression.id} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-medium text-slate-900">
                        {suppression.email}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                         <StatusBadge tone={suppression.status === "ACTIVE" ? "danger" : "neutral"}>
                          {suppression.status}
                        </StatusBadge>
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        <span
                          title={
                            PERMANENT_REASON_LABEL[suppression.reason] ??
                            "Added manually — releasable by an admin."
                          }
                        >
                          {suppression.reason}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {formatDate(suppression.first_observed_at)}
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        {suppression.removable && mayManage && (
                          <Button 
                            variant="ghost" 
                            size="icon"
                            onClick={() => setReleaseTarget(suppression.id)}
                            disabled={releaseMutation.isPending}
                            title="Release Suppression"
                            aria-label={`Release suppression for ${suppression.email}`}
                          >
                            <Trash2 className="h-4 w-4 text-red-500" />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <CursorPagination
              summary={`Showing ${suppressionQuery.data?.items.length ?? 0} suppression${
                (suppressionQuery.data?.items.length ?? 0) === 1 ? "" : "s"
              }`}
              canGoFirst={Boolean(cursor)}
              canGoNext={Boolean(suppressionQuery.data?.next_cursor)}
              onFirst={() => goToCursor(null)}
              onNext={() => goToCursor(suppressionQuery.data?.next_cursor ?? null)}
            />
          </>
        )}
      </section>
      <ConfirmDialog
        open={releaseTarget !== null}
        title="Release this suppression?"
        description="This manual suppression will be released and the address may receive email again."
        confirmLabel="Release suppression"
        tone="danger"
        loading={releaseMutation.isPending}
        onCancel={() => setReleaseTarget(null)}
        onConfirm={() => {
          if (releaseTarget) releaseMutation.mutate(releaseTarget);
        }}
      />
    </main>
  );
}
