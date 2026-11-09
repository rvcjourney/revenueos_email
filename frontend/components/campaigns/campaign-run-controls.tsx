"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pause, Play } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api-client";
import { pauseCampaign, resumeCampaign } from "@/lib/campaigns-api";
import { errorMessage } from "@/lib/errors";
import { formatRelativeTime } from "@/lib/format";
import { canExecuteCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type { CampaignDetail } from "@/types/domain";

type Props = {
  campaign: Pick<
    CampaignDetail,
    "id" | "name" | "status" | "version" | "paused_at" | "updated_at"
  > &
    Partial<Pick<CampaignDetail, "campaign_type">>;
};

// The server's resume checks, as short sentences the person can act on.
function resumeProblems(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.code !== "resume_blocked") return [];
  const errors = (error.details as { errors?: { message?: string }[] } | null)?.errors;
  return (errors ?? []).map((issue) => issue.message ?? "").filter(Boolean);
}

/**
 * Pause / Resume for an activated campaign (ADR-0020). The server is the
 * authority: the buttons only reflect what the campaign's status allows, and
 * every request carries the version the person was looking at, so a stale tab
 * gets a conflict (or a harmless no-op) instead of a surprise.
 */
export function CampaignRunControls({ campaign }: Props) {
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const mayExecute = canExecuteCampaign(activeWorkspace?.role_code);
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [pending, setPending] = useState<"pause" | "resume" | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  // Opt-in (ADR-0020): give leads that already finished the sequence the emails added
  // while paused. Off every time the dialog opens: nobody is contacted again by surprise.
  const [includeCompleted, setIncludeCompleted] = useState(false);

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: ["workspace", activeWorkspaceId, "campaigns", campaign.id],
    });
    queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "campaigns"] });
  };

  const pauseMutation = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return pauseCampaign(activeWorkspaceId, campaign.id, {
        expected_version: campaign.version,
      });
    },
    onSuccess: (updated) => {
      setFailure(null);
      setProblems([]);
      const inFlight = updated.outstanding_attempts ?? 0;
      toast(
        inFlight > 0
          ? `Campaign paused. ${inFlight} email${inFlight === 1 ? " was" : "s were"} already being sent and will finish.`
          : "Campaign paused.",
      );
      refresh();
    },
    onError: (err) => {
      setFailure(errorMessage(err));
      refresh(); // a stale version means the screen is behind: reload it
    },
  });

  const resumeMutation = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return resumeCampaign(activeWorkspaceId, campaign.id, {
        expected_version: campaign.version,
        ...(includeCompleted ? { include_completed: true } : {}),
      });
    },
    onSuccess: () => {
      setFailure(null);
      setProblems([]);
      toast("Campaign resumed.");
      refresh();
    },
    onError: (err) => {
      const list = resumeProblems(err);
      setProblems(list);
      setFailure(list.length > 0 ? null : errorMessage(err));
      refresh();
    },
  });

  const isRunning = campaign.status === "RUNNING" || campaign.status === "SCHEDULED";
  const isPaused = campaign.status === "PAUSED";
  if (!isRunning && !isPaused) return null;
  if (!mayExecute) return null;

  const busy = pauseMutation.isPending || resumeMutation.isPending;

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <div className="flex items-center gap-3">
        {isPaused && (
          <span className="text-xs text-slate-500">
            Paused {formatRelativeTime(campaign.paused_at ?? campaign.updated_at)}
          </span>
        )}
        {isRunning ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => setPending("pause")}
            aria-label="Pause campaign"
          >
            {pauseMutation.isPending ? (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Pause className="mr-1.5 h-4 w-4" aria-hidden="true" />
            )}
            Pause campaign
          </Button>
        ) : (
          <Button
            size="sm"
            disabled={busy}
            onClick={() => {
              setIncludeCompleted(false);
              setPending("resume");
            }}
            aria-label="Resume campaign"
          >
            {resumeMutation.isPending ? (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Play className="mr-1.5 h-4 w-4" aria-hidden="true" />
            )}
            Resume campaign
          </Button>
        )}
      </div>

      {problems.length > 0 && (
        <Alert variant="error">
          <span className="block">This campaign can&apos;t resume yet:</span>
          {problems.map((problem) => (
            <span key={problem} className="block font-normal">
              &bull; {problem}
            </span>
          ))}
        </Alert>
      )}
      {failure && <Alert variant="error">{failure}</Alert>}

      <ConfirmDialog
        open={pending !== null}
        title={
          pending === "pause" ? `Pause "${campaign.name}"?` : `Resume "${campaign.name}"?`
        }
        description={
          pending === "pause" ? (
            "No new emails are started while it is paused. An email that is being sent right now can't be recalled and will finish. While paused you can change the schedule, edit emails that haven't been sent and add leads."
          ) : (
            <>
              <span className="block">
                Waits keep counting while a campaign is paused, so follow-ups that became due are
                sent gradually over your sending windows rather than all at once. Edits you made
                while paused apply to emails that haven&apos;t been sent.
              </span>
              {campaign.campaign_type !== "HYPER_PERSONALIZED" ? (
                <label className="mt-3 flex items-start gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 rounded border-slate-300"
                    checked={includeCompleted}
                    onChange={(event) => setIncludeCompleted(event.target.checked)}
                  />
                  <span>
                    Also send the new emails to leads who already finished the sequence. Only
                    leads who haven&apos;t replied, unsubscribed or bounced are included. Leave
                    this off to keep finished leads finished.
                  </span>
                </label>
              ) : null}
            </>
          )
        }
        confirmLabel={pending === "pause" ? "Pause campaign" : "Resume campaign"}
        loading={busy}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          const mutation = pending === "pause" ? pauseMutation : resumeMutation;
          mutation.mutate(undefined, { onSettled: () => setPending(null) });
        }}
      />
    </div>
  );
}
