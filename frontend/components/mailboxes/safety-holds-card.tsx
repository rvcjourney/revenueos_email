"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, ShieldAlert } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { listSafetyHolds, releaseSafetyHold } from "@/lib/safety-holds-api";
import type { SafetyHold } from "@/types/domain";

const KIND_TITLE: Record<SafetyHold["kind"], string> = {
  HIGH_BOUNCE_RATE: "Paused: too many emails bounced",
  RESYNC: "Paused: inbox sync needs to catch up",
  OTHER: "Paused for safety",
};

const KIND_ADVICE: Record<SafetyHold["kind"], string> = {
  HIGH_BOUNCE_RATE:
    "A high bounce rate damages a mailbox's reputation with Gmail and Outlook. Before you release it, check the list that bounced: remove bad addresses or verify the list, otherwise the mailbox will be paused again.",
  RESYNC:
    "This clears by itself once the inbox has been fully re-read. Release it only if you are sure no replies were missed.",
  OTHER: "Release it once you have looked into the reason shown.",
};

/** Why this mailbox is not sending, and a way for a person to let it send again.
 * Shows nothing when the mailbox has no active hold. */
export function SafetyHoldsCard({
  workspaceId,
  mailboxId,
  canManage,
}: {
  workspaceId: string;
  mailboxId: string;
  canManage: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["workspace", workspaceId, "mailbox", mailboxId, "safety-holds"];
  const [error, setError] = useState<string | null>(null);

  const query = useQuery({
    queryKey,
    queryFn: () => listSafetyHolds(workspaceId, mailboxId),
  });

  const release = useMutation({
    mutationFn: (holdId: string) => releaseSafetyHold(workspaceId, mailboxId, holdId),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({
        queryKey: ["workspace", workspaceId, "mailbox", mailboxId],
      });
    },
    onError: (err) => {
      setError(
        err instanceof ApiError && err.status === 403
          ? "You don't have permission to release a hold."
          : "We couldn't release the hold. Please try again.",
      );
    },
  });

  const holds = query.data ?? [];
  if (query.isLoading || query.error || holds.length === 0) return null;

  return (
    <section
      className="space-y-4 rounded-xl border border-red-200 bg-red-50/60 p-6"
      data-testid="safety-holds"
    >
      <h2 className="flex items-center gap-2 text-base font-bold text-red-900">
        <ShieldAlert className="h-4 w-4" aria-hidden="true" />
        This mailbox is not sending
      </h2>
      {error ? <Alert variant="error">{error}</Alert> : null}
      <ul className="space-y-4">
        {holds.map((hold) => (
          <li key={hold.id} className="space-y-1 text-sm text-red-900">
            <p className="font-semibold">{KIND_TITLE[hold.kind]}</p>
            <p>{hold.reason}</p>
            <p className="text-red-800">{KIND_ADVICE[hold.kind]}</p>
            {canManage ? (
              <Button
                variant="outline"
                className="mt-2 bg-white"
                disabled={release.isPending}
                onClick={() => release.mutate(hold.id)}
              >
                {release.isPending && release.variables === hold.id ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Release and allow sending
              </Button>
            ) : (
              <p className="text-xs text-red-800">
                A manager or admin can release this.
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
