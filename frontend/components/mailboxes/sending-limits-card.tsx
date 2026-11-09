"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, Loader2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api-client";
import { getMailboxLimits, setMailboxLimits } from "@/lib/mailbox-limits-api";
import type { MailboxLimits } from "@/types/domain";

function whole(value: string): number | null {
  return /^\d+$/.test(value.trim()) ? Number(value) : null;
}

/** How many emails this mailbox may send, and how far apart. The send path refuses
 * to send from a mailbox with no limit, so an unset limit is called out. */
export function SendingLimitsCard({
  workspaceId,
  mailboxId,
  canManage,
}: {
  workspaceId: string;
  mailboxId: string;
  canManage: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["workspace", workspaceId, "mailbox", mailboxId, "limits"];
  const [dailyCap, setDailyCap] = useState("");
  const [spacing, setSpacing] = useState("");
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const query = useQuery({
    queryKey,
    queryFn: () => getMailboxLimits(workspaceId, mailboxId),
  });
  const limits = query.data;

  // Fill the form from the server, and again after a save or a refetch.
  useEffect(() => {
    if (!limits) return;
    setDailyCap(String(limits.daily_cap ?? limits.default_daily_cap));
    setSpacing(String(limits.min_spacing_seconds ?? limits.default_min_spacing_seconds));
  }, [limits]);

  const capValue = whole(dailyCap);
  const spacingValue = whole(spacing);
  const capError =
    limits && (capValue === null || capValue < 1 || capValue > limits.max_daily_cap)
      ? `Enter a whole number from 1 to ${limits.max_daily_cap}`
      : undefined;
  const spacingError =
    limits && (spacingValue === null || spacingValue > limits.max_spacing_seconds)
      ? `Enter a whole number from 0 to ${limits.max_spacing_seconds}`
      : undefined;

  const mutation = useMutation({
    mutationFn: () =>
      setMailboxLimits(workspaceId, mailboxId, {
        daily_cap: capValue as number,
        min_spacing_seconds: spacingValue as number,
      }),
    onSuccess: (saved: MailboxLimits) => {
      queryClient.setQueryData(queryKey, saved);
      setNotice({ kind: "success", text: "Sending limits saved." });
    },
    onError: (err) => {
      setNotice({
        kind: "error",
        text:
          err instanceof ApiError && err.status === 403
            ? "You don't have permission to change sending limits."
            : err instanceof ApiError && err.status === 422
              ? err.message || "Those limits aren't valid."
              : "We couldn't save the limits. Please try again.",
      });
    },
  });

  return (
    <section
      className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-4"
      data-testid="sending-limits"
    >
      <div>
        <h2 className="flex items-center gap-2 text-base font-bold text-slate-900">
          <Gauge className="h-4 w-4 text-brand-600" aria-hidden="true" />
          Sending limits
        </h2>
        <p className="mt-1 text-sm text-slate-500">
          Outly never sends more than this many emails from this mailbox in any 24 hours, and
          waits at least this long between them. Campaigns are spread out to stay within it. New
          mailboxes start low on purpose: raise the limit slowly as the mailbox builds a
          reputation.
        </p>
      </div>

      {query.isLoading ? (
        <p className="text-sm text-slate-500">Loading limits…</p>
      ) : query.error || !limits ? (
        <Alert variant="error">Couldn&apos;t load this mailbox&apos;s limits.</Alert>
      ) : (
        <>
          {!limits.configured ? (
            <Alert variant="warning">
              This mailbox has no sending limit, so campaigns will not send from it until one is
              set. Save the suggested values below, or choose your own.
            </Alert>
          ) : null}
          {notice ? (
            <Alert variant={notice.kind === "success" ? "success" : "error"}>{notice.text}</Alert>
          ) : null}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id="limit-daily-cap" label="Emails per day" error={capError}>
              <Input
                inputMode="numeric"
                value={dailyCap}
                onChange={(e) => setDailyCap(e.target.value)}
                disabled={!canManage || mutation.isPending}
              />
            </Field>
            <Field
              id="limit-spacing"
              label="Minimum seconds between emails"
              error={spacingError}
            >
              <Input
                inputMode="numeric"
                value={spacing}
                onChange={(e) => setSpacing(e.target.value)}
                disabled={!canManage || mutation.isPending}
              />
            </Field>
          </div>
          {canManage ? (
            <div className="flex justify-end">
              <Button
                onClick={() => {
                  setNotice(null);
                  mutation.mutate();
                }}
                disabled={Boolean(capError || spacingError) || mutation.isPending}
              >
                {mutation.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Save limits
              </Button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
