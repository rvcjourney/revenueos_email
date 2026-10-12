"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, Lock, Rocket } from "lucide-react";
import { useState } from "react";

import { CampaignRunControls } from "@/components/campaigns/campaign-run-controls";
import { GenerationProgressPanel } from "@/components/campaigns/personalization/generation-progress-panel";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ProviderBadge } from "@/components/mailboxes/provider-badge";
import { ApiError } from "@/lib/api-client";
import {
  activateCampaign,
  getCampaignPlanning,
  getReview,
} from "@/lib/campaigns-api";
import { getPersonalization } from "@/lib/personalization-api";
import { canExecuteCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type { CampaignPlanning, PreflightIssue } from "@/types/domain";
import { LoadingBlock } from "@/components/ui/skeleton";

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return "We couldn't load the review. Please try again.";
}

const TAB_FOR_FIELD: Record<string, string> = {
  sequence: "sequence",
  mailboxes: "senders",
  audience: "audience",
  settings: "schedule",
  // The Personalization tab was merged into Sequence (ADR-0016).
  personalization: "sequence",
};

function tabForIssue(issue: PreflightIssue): string {
  const prefix = (issue.field_path ?? "").split(/[.[]/)[0];
  return TAB_FOR_FIELD[prefix] ?? "overview";
}

function IssueList({
  campaignId,
  issues,
  tone,
}: {
  campaignId: string;
  issues: PreflightIssue[];
  tone: "error" | "warning";
}) {
  if (issues.length === 0) return null;
  return (
    <ul className="space-y-2">
      {issues.map((issue, i) => (
        <li
          key={`${issue.code}-${i}`}
          className={`flex items-start justify-between gap-3 rounded-md border p-3 text-sm ${
            tone === "error"
              ? "border-red-200 bg-red-50 text-red-800"
              : "border-amber-200 bg-amber-50 text-amber-800"
          }`}
        >
          <span>{issue.message}</span>
          <Link
            href={`/app/campaigns/${campaignId}/${tabForIssue(issue)}`}
            className="shrink-0 whitespace-nowrap text-xs font-medium underline"
          >
            Fix
          </Link>
        </li>
      ))}
    </ul>
  );
}

function planningIsNonTerminal(planning: CampaignPlanning | undefined): boolean {
  if (!planning) return false;
  if (planning.planning_status === "PENDING") return true;
  for (const job of [planning.enroll, planning.render]) {
    if (job && (job.state === "PENDING" || job.state === "PROCESSING")) return true;
  }
  return false;
}

function PlanningStatusPanel({ planning }: { planning: CampaignPlanning }) {
  const jobLabel = (label: string, job: CampaignPlanning["enroll"]) => {
    if (!job) return null;
    return (
      <div className="flex items-center justify-between text-xs text-slate-600">
        <span>{label}</span>
        <span>
          {job.state === "PROCESSING" || job.state === "PENDING"
            ? `${job.processed_count}${job.total_count != null ? `/${job.total_count}` : ""}`
            : job.state}
        </span>
      </div>
    );
  };

  const failed = planning.enroll?.state === "FAILED" || planning.render?.state === "FAILED";

  return (
    <div
      className={`rounded-lg border p-4 ${
        failed
          ? "border-red-200 bg-red-50"
          : planning.planning_status === "READY"
            ? "border-emerald-200 bg-emerald-50"
            : "border-sky-200 bg-sky-50"
      }`}
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
        {planning.planning_status === "READY" ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
        ) : failed ? (
          <AlertTriangle className="h-4 w-4 text-red-600" />
        ) : (
          <Loader2 className="h-4 w-4 animate-spin text-sky-600" />
        )}
        {failed
          ? "Planning failed"
          : planning.planning_status === "READY"
            ? "Message plan ready"
            : "Preparing campaign..."}
      </div>
      <div className="mt-2 space-y-1">
        {jobLabel("Enrolling recipients", planning.enroll)}
        {jobLabel("Rendering messages", planning.render)}
      </div>
      {failed && (planning.enroll?.error_reason || planning.render?.error_reason) && (
        <p className="mt-2 text-xs text-red-700">
          {planning.enroll?.error_reason || planning.render?.error_reason}
        </p>
      )}
    </div>
  );
}

export default function CampaignReviewPage() {
  const params = useParams<{ campaign_id: string }>();
  const campaignId = params.campaign_id;
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const mayExecute = canExecuteCampaign(activeWorkspace?.role_code);
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<"activate" | null>(null);

  const reviewQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId, "review"],
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? getReview(activeWorkspaceId, campaignId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId),
    refetchOnWindowFocus: true,
  });

  const campaignStatus = reviewQuery.data?.campaign.status;
  const isActivated = Boolean(
    campaignStatus && campaignStatus !== "DRAFT" && campaignStatus !== "ARCHIVED",
  );

  const planningQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId, "planning"],
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? getCampaignPlanning(activeWorkspaceId, campaignId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId && isActivated),
    refetchInterval: (query) => (planningIsNonTerminal(query.state.data) ? 4000 : false),
  });

  const isHyper = reviewQuery.data?.campaign.campaign_type === "HYPER_PERSONALIZED";
  const personalizationQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId, "personalization"],
    queryFn: () => getPersonalization(activeWorkspaceId as string, campaignId),
    enabled: Boolean(activeWorkspaceId && campaignId && isHyper),
  });

  const invalidateAll = () => {
    queryClient.invalidateQueries({
      queryKey: ["workspace", activeWorkspaceId, "campaigns", campaignId],
    });
  };

  const activateMutation = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId || !campaignId || !reviewQuery.data)
        throw new Error("Not ready");
      return activateCampaign(activeWorkspaceId, campaignId, {
        expected_version: reviewQuery.data.campaign.version,
      });
    },
    onSuccess: () => {
      setActionError(null);
      invalidateAll();
    },
    onError: (err) => setActionError(errorMessage(err)),
  });

  if (reviewQuery.isLoading) {
    return (
      <LoadingBlock />
    );
  }
  if (reviewQuery.isError || !reviewQuery.data) {
    return <Alert variant="error">{errorMessage(reviewQuery.error)}</Alert>;
  }

  const { campaign, sequence, mailboxes, settings, audience, preflight } =
    reviewQuery.data;
  const canStart = mayExecute && campaign.status === "DRAFT" && preflight.ready;

  return (
    <div className="max-w-2xl space-y-6">
      {actionError && <Alert variant="error">{actionError}</Alert>}

      <div>
        <h2 className="text-lg font-semibold text-slate-900">Review</h2>
        <p className="text-sm text-slate-500">
          Everything below reflects saved configuration. Preflight is
          re-checked here and again at activation.
        </p>
      </div>

      <div
        className={`rounded-lg border p-4 ${
          preflight.ready
            ? "border-emerald-200 bg-emerald-50"
            : "border-amber-200 bg-amber-50"
        }`}
      >
        <div
          className={`flex items-center gap-2 text-sm font-semibold ${
            preflight.ready ? "text-emerald-900" : "text-amber-900"
          }`}
        >
          {preflight.ready ? (
            <CheckCircle2 className="h-4 w-4" />
          ) : (
            <AlertTriangle className="h-4 w-4" />
          )}
          {preflight.ready
            ? "Ready to activate"
            : `${preflight.errors.length} item${
                preflight.errors.length === 1 ? "" : "s"
              } to resolve`}
        </div>
        <div className="mt-3 space-y-3">
          <IssueList campaignId={campaignId} issues={preflight.errors} tone="error" />
          <IssueList
            campaignId={campaignId}
            issues={preflight.warnings}
            tone="warning"
          />
        </div>
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <h3 className="text-sm font-semibold text-slate-900">Sequence</h3>
        <p className="mt-1 text-sm text-slate-600">
          {sequence && sequence.steps.length > 0
            ? `${sequence.steps.length} step${sequence.steps.length === 1 ? "" : "s"} (${
                sequence.steps.filter((s) => s.kind === "EMAIL").length
              } email, ${sequence.steps.filter((s) => s.kind === "WAIT").length} wait)`
            : "No steps configured."}
        </p>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <h3 className="text-sm font-semibold text-slate-900">Senders</h3>
        {mailboxes.length === 0 ? (
          <p className="mt-1 text-sm text-slate-600">No mailboxes assigned.</p>
        ) : (
          <ul className="mt-2 space-y-1">
            {mailboxes.map((mb) => (
              <li key={mb.mailbox_id} className="flex items-center gap-2 text-sm">
                <ProviderBadge provider={mb.provider} />
                <span className="text-slate-600">{mb.email_address}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <h3 className="text-sm font-semibold text-slate-900">Schedule</h3>
        {settings ? (
          <p className="mt-1 text-sm text-slate-600">
            {settings.timezone}, {settings.window_start_local.slice(0, 5)}&ndash;
            {settings.window_end_local.slice(0, 5)}, limit{" "}
            {settings.daily_limit ?? "unlimited"}/day
          </p>
        ) : (
          <p className="mt-1 text-sm text-slate-600">No schedule configured.</p>
        )}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <h3 className="text-sm font-semibold text-slate-900">Audience</h3>
        {audience && audience.is_committed ? (
          <p className="mt-1 text-sm text-slate-600">
            {audience.accepted_count ?? 0} eligible leads configured
            {(audience.excluded_count ?? 0) > 0 &&
              ` (${audience.excluded_count} excluded)`}
            . Final send eligibility is re-checked before anything sends.
          </p>
        ) : (
          <p className="mt-1 text-sm text-slate-600">No audience committed yet.</p>
        )}
      </section>

      {isHyper && personalizationQuery.data ? (
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
          <h3 className="text-sm font-semibold text-slate-900">Personalization</h3>
          <p className="mt-1 text-sm text-slate-600">
            {personalizationQuery.data.config
              ? `Objective: ${personalizationQuery.data.config.objective}`
              : "No objective defined yet."}
          </p>
          <p className="mt-1 text-sm text-slate-600">
            {personalizationQuery.data.approval.status === "APPROVED"
              ? "Sample emails approved."
              : personalizationQuery.data.approval.status === "STALE"
                ? "Approval is out of date: generate and approve new samples."
                : "Sample emails have not been approved yet."}{" "}
            Each email is written shortly before it is sent.
          </p>
        </section>
      ) : null}

      {isActivated && planningQuery.data && (
        <PlanningStatusPanel planning={planningQuery.data} />
      )}

      {isHyper && isActivated && activeWorkspaceId ? (
        <GenerationProgressPanel
          workspaceId={activeWorkspaceId}
          campaignId={campaignId}
          active
        />
      ) : null}

      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
        {campaign.status === "DRAFT" ? (
          <>
            <Button
              disabled={!canStart || activateMutation.isPending}
              className={!canStart ? "cursor-not-allowed opacity-60" : undefined}
              title={
                !mayExecute
                  ? "Only Managers and above can activate a campaign"
                  : !preflight.ready
                    ? "Resolve the items above before activating"
                    : undefined
              }
              onClick={() => {
                if (!canStart || activateMutation.isPending) return;
                setPendingAction("activate");
              }}
            >
              {activateMutation.isPending ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              ) : canStart ? (
                <Rocket className="mr-1.5 h-4 w-4" />
              ) : (
                <Lock className="mr-1.5 h-4 w-4" />
              )}
              Start Campaign
            </Button>
            <p className="mt-2 text-xs text-slate-500">
              Starting activates this campaign and begins durable message planning.
              No email is sent as part of activation.
              {mayExecute
                ? ""
                : " Only Managers and above can activate this campaign."}
            </p>
          </>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <CampaignRunControls campaign={campaign} />
            <p className="text-xs text-slate-500">
              Campaign is {campaign.status}.{" "}
              {campaign.status === "PAUSED"
                ? "Configuration can be changed while it is paused."
                : "Configuration is restricted while activated."}
            </p>
          </div>
        )}
      </div>
      <ConfirmDialog
        open={pendingAction !== null}
        title={`Activate "${campaign.name}"?`}
        description={
          "This freezes the sequence, senders and audience and creates a durable message plan. No email is sent by this action, and configuration becomes restricted from editing." +
          (isHyper ? " Each personalized email is written shortly before it is due to send." : "")
        }
        confirmLabel="Activate campaign"
        loading={activateMutation.isPending}
        onCancel={() => setPendingAction(null)}
        onConfirm={() => {
          activateMutation.mutate(undefined, { onSettled: () => setPendingAction(null) });
        }}
      />
    </div>
  );
}
