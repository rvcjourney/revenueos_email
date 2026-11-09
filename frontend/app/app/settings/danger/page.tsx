"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import { ApiError } from "@/lib/api-client";
import { purgeWorkspace } from "@/lib/erasure-api";
import { canDeleteWorkspace } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import { listWorkspaces } from "@/lib/workspaces-api";

export default function DangerZonePage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace, switchWorkspace } = useWorkspace();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canDelete = canDeleteWorkspace(activeWorkspace?.role_code);

  const deleteMutation = useMutation({
    mutationFn: (confirm: string) => purgeWorkspace(activeWorkspaceId!, confirm),
    onSuccess: async () => {
      setConfirmOpen(false);
      const remaining = await listWorkspaces();
      queryClient.setQueryData(["workspaces"], remaining);
      const next = remaining.find((w) => w.workspace_id !== activeWorkspaceId);
      if (next) {
        switchWorkspace(next.workspace_id);
        router.push("/app/dashboard");
      } else {
        router.push("/onboarding/workspace");
      }
    },
    onError: (err: unknown) => {
      setError(
        err instanceof ApiError
          ? err.message
          : "We couldn't delete this workspace. Please try again.",
      );
    },
  });

  if (!activeWorkspace) return null;

  if (!canDelete) {
    return (
      <main className="max-w-2xl pb-16">
        <Alert>Only the workspace Owner can access the Danger Zone.</Alert>
      </main>
    );
  }

  return (
    <main className="space-y-6 max-w-2xl pb-16">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.08em] text-red-600">Danger Zone</p>
        <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-1">
          Delete Workspace
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          Irreversible and destructive actions for {activeWorkspace.workspace_name}.
        </p>
      </div>

      {error ? <Alert variant="error">{error}</Alert> : null}

      <section className="rounded-xl border border-red-200 bg-red-50/50 p-5 shadow-card space-y-4">
        <div className="flex items-start gap-3">
          <div className="rounded-md bg-red-100 p-2 text-red-600">
            <AlertTriangle className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="flex-1">
            <h2 className="text-sm font-semibold text-slate-900">
              Delete this workspace
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Permanently deletes {activeWorkspace.workspace_name} and everything in
              it &mdash; leads, campaigns, sequences, templates, mailboxes, team
              members, and all sending history. This cannot be undone.
            </p>
          </div>
          <Button variant="danger" onClick={() => setConfirmOpen(true)}>
            Delete Workspace
          </Button>
        </div>
      </section>

      <TypeToConfirmDialog
        open={confirmOpen}
        title="Delete Workspace"
        description={
          <>
            This permanently deletes{" "}
            <strong className="text-slate-900">{activeWorkspace.workspace_name}</strong>{" "}
            and all of its data. Everyone with access will immediately lose it.
            There is no way to undo this.
          </>
        }
        phrase={activeWorkspace.workspace_name}
        confirmLabel="Delete Workspace"
        loading={deleteMutation.isPending}
        error={error}
        onCancel={() => {
          if (!deleteMutation.isPending) {
            setConfirmOpen(false);
            setError(null);
          }
        }}
        onConfirm={() => {
          setError(null);
          deleteMutation.mutate(activeWorkspace.workspace_name);
        }}
      />
    </main>
  );
}
