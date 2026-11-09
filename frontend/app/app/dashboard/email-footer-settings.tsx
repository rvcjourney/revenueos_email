"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil } from "lucide-react";
import { z } from "zod";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { controlClasses, Input } from "@/components/ui/input";
import { Field } from "@/components/ui/field";
import { ApiError } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { updateWorkspace } from "@/lib/workspaces-api";
import type { Workspace } from "@/types/domain";

// Mirrors the server's limits (backend/app/modules/unsubscribe/compliance.py).
const POSTAL_ADDRESS_MAX = 500;
const FOOTER_TEXT_MAX = 300;

const schema = z.object({
  postal_address: z
    .string()
    .trim()
    .max(POSTAL_ADDRESS_MAX, `At most ${POSTAL_ADDRESS_MAX} characters`),
  footer_text: z.string().trim().max(FOOTER_TEXT_MAX, `At most ${FOOTER_TEXT_MAX} characters`),
});
type FormValues = z.infer<typeof schema>;

type ComplianceSection = { postal_address?: unknown; footer_text?: unknown };

export function readCompliance(defaults: Record<string, unknown> | undefined): {
  postal_address: string;
  footer_text: string;
} {
  const section = (defaults?.compliance ?? {}) as ComplianceSection;
  return {
    postal_address: typeof section.postal_address === "string" ? section.postal_address : "",
    footer_text: typeof section.footer_text === "string" ? section.footer_text : "",
  };
}

export function EmailFooterSettings({
  workspace,
  canManage,
}: {
  workspace: Workspace;
  canManage: boolean;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = readCompliance(workspace.defaults);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: current });

  const mutation = useMutation({
    // The server replaces `defaults` as a whole, so keep everything else in it.
    mutationFn: (values: FormValues) =>
      updateWorkspace(workspace.id, {
        defaults: {
          ...workspace.defaults,
          compliance: { postal_address: values.postal_address, footer_text: values.footer_text },
        },
        expected_version: workspace.version,
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["workspace", workspace.id], updated);
      setEditing(false);
      setError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setError("Someone else just updated this workspace. Reloading the latest version.");
        queryClient.invalidateQueries({ queryKey: ["workspace", workspace.id] });
      } else if (err instanceof ApiError && err.status === 403) {
        setError("You no longer have permission to do that.");
      } else if (err instanceof ApiError && err.status === 422) {
        setError(err.message || "Those footer details aren't valid.");
      } else {
        setError("We couldn't save that change. Please try again.");
      }
    },
  });

  return (
    <section className="mt-3 rounded-xl border border-slate-200 bg-white p-5 shadow-card">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">Email footer</h2>
          <p className="mt-1 text-sm text-slate-500">
            Every campaign email ends with an unsubscribe link. Your postal address and a footer
            message are optional and shown above it when set.
          </p>
        </div>
        {canManage && !editing ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Edit email footer"
            onClick={() => {
              reset(current);
              setEditing(true);
            }}
          >
            <Pencil className="h-4 w-4" aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      <div className="mt-4 max-w-lg">
        {error ? (
          <div className="mb-3">
            <Alert>{error}</Alert>
          </div>
        ) : null}
        {editing && canManage ? (
          <form
            className="space-y-3"
            onSubmit={handleSubmit((values) => mutation.mutate(values))}
          >
            <Field id="footer-postal-address" label="Postal address (optional)" error={errors.postal_address?.message}>
              <textarea
                rows={3}
                disabled={isSubmitting}
                className={cn(controlClasses, "px-3 py-2")}
                placeholder={"Acme Pvt Ltd\n12 MG Road, Bengaluru 560001, India"}
                {...register("postal_address")}
              />
            </Field>
            <Field id="footer-text" label="Footer message (optional)" error={errors.footer_text?.message}>
              <Input
                disabled={isSubmitting}
                placeholder="If you would rather not hear from us, you can unsubscribe at any time."
                {...register("footer_text")}
              />
            </Field>
            <div className="flex gap-2">
              <Button type="submit" disabled={isSubmitting}>
                {isSubmitting ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  "Save footer"
                )}
              </Button>
              <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
          </form>
        ) : current.postal_address || current.footer_text ? (
          <div className="space-y-1 text-sm text-slate-900">
            {current.postal_address ? (
              <p className="whitespace-pre-line">{current.postal_address}</p>
            ) : null}
            {current.footer_text ? <p className="text-slate-500">{current.footer_text}</p> : null}
          </div>
        ) : (
          <p className="text-sm text-slate-500">
            No postal address or footer message set. Emails end with just the unsubscribe link.
          </p>
        )}
      </div>
    </section>
  );
}
