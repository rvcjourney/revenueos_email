"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Pencil } from "lucide-react";
import { z } from "zod";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api-client";
import { canDeleteWorkspace } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import { getWorkspace, updateWorkspace } from "@/lib/workspaces-api";

import { EmailFooterSettings } from "./email-footer-settings";

const schema = z.object({ name: z.string().min(1, "Required").max(200) });
type FormValues = z.infer<typeof schema>;

export function WorkspaceSettings() {
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canManage =
    activeWorkspace?.role_code === "OWNER" || activeWorkspace?.role_code === "ADMIN";

  const { data: workspace } = useQuery({
    queryKey: ["workspace", activeWorkspaceId],
    queryFn: () => getWorkspace(activeWorkspaceId!),
    enabled: Boolean(activeWorkspaceId),
  });

  const { register, handleSubmit, reset, formState: { errors, isSubmitting } } =
    useForm<FormValues>({ resolver: zodResolver(schema) });

  useEffect(() => {
    if (workspace) reset({ name: workspace.name });
  }, [workspace, reset]);

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      updateWorkspace(activeWorkspaceId!, {
        name: values.name,
        expected_version: workspace!.version,
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace", activeWorkspaceId], updated);
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      setEditing(false);
      setError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setError("Someone else just updated this workspace. Reloading the latest version.");
        queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId] });
      } else if (err instanceof ApiError && err.status === 403) {
        setError("You no longer have permission to do that.");
      } else {
        setError("We couldn't save that change. Please try again.");
      }
    },
  });

  if (!workspace) return null;

  return (
    <>
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">
            Workspace settings
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            {canManage
              ? "Only Owners and Admins can rename this workspace."
              : "Read-only: your role doesn't permit changing workspace settings."}
          </p>
        </div>
        {canManage && !editing ? (
          <Button variant="ghost" size="icon" aria-label="Edit workspace name" onClick={() => setEditing(true)}>
            <Pencil className="h-4 w-4" aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      <div className="mt-4 max-w-sm">
        {error ? (
          <div className="mb-3">
            <Alert>{error}</Alert>
          </div>
        ) : null}
        {editing && canManage ? (
          <form
            className="flex items-start gap-2"
            onSubmit={handleSubmit((values) => mutation.mutate(values))}
          >
            <Field
              id="workspace-name"
              label="Workspace name"
              error={errors.name?.message}
              className="flex-1"
            >
              <Input disabled={isSubmitting} {...register("name")} />
            </Field>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : "Save"}
            </Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <p className="text-base font-medium text-slate-900">{workspace.name}</p>
        )}
      </div>
    </section>
    <EmailFooterSettings workspace={workspace} canManage={canManage} />
    {canDeleteWorkspace(activeWorkspace?.role_code) ? (
      <Link
        href="/app/settings/danger"
        className="mt-3 inline-flex items-center gap-1.5 text-sm font-medium text-red-600 hover:text-red-700"
      >
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        Danger Zone
      </Link>
    ) : null}
    </>
  );
}
