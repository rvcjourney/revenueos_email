"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { GenerationProgressPanel } from "@/components/campaigns/personalization/generation-progress-panel";
import { PreviewAndApprovalSection } from "@/components/campaigns/personalization/preview-and-approval-section";
import { usePersonalizationState } from "@/components/campaigns/personalization/use-personalization-state";
import { AiDraftBar } from "@/components/campaigns/sequence/ai-draft-bar";
import { computeTimings } from "@/components/campaigns/sequence/duration";
import { EmailStepDialog } from "@/components/campaigns/sequence/email-step-dialog";
import { replySubject, sameThreadErrorMessage } from "@/components/campaigns/sequence/same-thread";
import { SequenceTimeline } from "@/components/campaigns/sequence/sequence-timeline";
import { CampaignSetupPanel } from "@/components/campaigns/sequence/setup/campaign-setup-panel";
import { useReferenceGeneration } from "@/components/campaigns/sequence/use-reference-generation";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import {
  addSequenceStep,
  deleteSequenceStep,
  duplicateSequenceStep,
  getCampaign,
  getSequence,
  listCampaignMailboxes,
  listCampaignSettings,
  reorderSequenceSteps,
  updateSequenceStep,
} from "@/lib/campaigns-api";
import { canDraftCampaign, canExecuteCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type { CampaignSequence, CampaignSettings, SequenceStep } from "@/types/domain";
import { LoadingBlock } from "@/components/ui/skeleton";

// Wait inserted before a newly added email; two days is the common cadence.
const DEFAULT_WAIT_MINUTES = 2 * 24 * 60;
const NEW_EMAIL_SUBJECT = "New email";
const NEW_EMAIL_BODY = "<p>Hi {{first_name|there}},</p><p></p>";
// Hyper-personalized campaigns: a starting point for the reference email.
const NEW_REFERENCE_SUBJECT = "Quick idea for {{company|your team}}";
const NEW_REFERENCE_BODY =
  "<p>Hi {{first_name|there}},</p><p>Write the email you want to send: your offer, why it matters " +
  "and what you'd like them to do next. Each lead receives a version personalized to them.</p><p>Best,</p>";

const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function errorMessage(error: unknown) {
  const sameThread = sameThreadErrorMessage(error);
  if (sameThread) {
    return `${sameThread} Turn off “Send in same thread” on the email that would become first, then try again.`;
  }
  if (error instanceof ApiError) return error.message;
  return "We couldn’t complete that request. Please try again.";
}

function describeSchedule(settings: CampaignSettings | null): string | null {
  if (!settings) return null;
  const days = [...settings.weekdays]
    .sort((a, b) => a - b)
    .map((d) => WEEKDAY_NAMES[d - 1])
    .filter(Boolean)
    .join(", ");
  return `${days}, ${settings.window_start_local.slice(0, 5)}–${settings.window_end_local.slice(0, 5)} ${settings.timezone}`;
}

export default function CampaignSequencePage() {
  const params = useParams<{ campaign_id: string }>();
  const campaignId = params.campaign_id;
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const mayDraft = canDraftCampaign(activeWorkspace?.role_code);
  const [actionError, setActionError] = useState<string | null>(null);
  const [openStepId, setOpenStepId] = useState<string | null>(null);
  const [savingWaitId, setSavingWaitId] = useState<string | null>(null);
  // An email waiting for the user to confirm that AI may rewrite it.
  const [regenerateTarget, setRegenerateTarget] = useState<{ id: string; number: number } | null>(
    null,
  );

  const campaignKey = ["workspace", activeWorkspaceId, "campaigns", campaignId];
  const sequenceKey = [...campaignKey, "sequence"];
  const ready = Boolean(activeWorkspaceId && campaignId);

  const campaignQuery = useQuery({
    queryKey: campaignKey,
    queryFn: () => getCampaign(activeWorkspaceId as string, campaignId),
    enabled: ready,
  });

  const sequenceQuery = useQuery({
    queryKey: sequenceKey,
    queryFn: () => getSequence(activeWorkspaceId as string, campaignId),
    enabled: ready,
  });

  const mailboxesQuery = useQuery({
    queryKey: [...campaignKey, "mailboxes"],
    queryFn: () => listCampaignMailboxes(activeWorkspaceId as string, campaignId),
    enabled: ready,
  });

  const settingsQuery = useQuery({
    queryKey: [...campaignKey, "settings"],
    queryFn: () => listCampaignSettings(activeWorkspaceId as string, campaignId),
    enabled: ready,
  });

  const isHyper = campaignQuery.data?.campaign_type === "HYPER_PERSONALIZED";
  // Objective, company, email style, previews and approval live on this tab for a
  // hyper-personalized campaign (ADR-0016); nothing is fetched for a standard one.
  const personalization = usePersonalizationState(activeWorkspaceId, campaignId, isHyper);
  const generation = useReferenceGeneration(activeWorkspaceId, campaignId);
  const capabilities = personalization.capabilitiesQuery.data;
  const aiAvailable = capabilities?.ai_drafting_available === true;
  const personalizationState = personalization.stateQuery.data;
  const designedEmail = isHyper && personalizationState?.config?.email_format === "HTML";
  const sequence = sequenceQuery.data;
  const steps = sequence?.steps ?? [];
  const campaignStatus = campaignQuery.data?.status;
  // Editing is allowed while the campaign is a DRAFT, or -- for a manager of a
  // paused standard campaign -- once the server says so (ADR-0020). The server
  // enforces both and answers state_conflict / forbidden otherwise.
  const pausedEdit = campaignStatus === "PAUSED" && sequence?.paused_edit_allowed === true;
  const lockedThrough = pausedEdit ? (sequence?.structure_locked_through ?? 0) : 0;
  // A paused personalized campaign is edited like a standard one (wording, waits, steps
  // after the last one a lead reached); emails already written keep their version. Its
  // setup (objective, company, style) and attachments stay frozen.
  const pausedHyper = pausedEdit && isHyper;
  const readOnly = pausedEdit
    ? false
    : !mayDraft || campaignStatus !== "DRAFT" || sequence?.status === "FROZEN";
  const latestSettings = settingsQuery.data?.length
    ? settingsQuery.data.reduce((a, b) => (b.revision > a.revision ? b : a))
    : null;

  function afterChange(next?: CampaignSequence) {
    setActionError(null);
    if (next) queryClient.setQueryData(sequenceKey, next);
    void queryClient.invalidateQueries({ queryKey: sequenceKey });
    // The campaign header shows "N items left before ready" from preflight.
    void queryClient.invalidateQueries({ queryKey: [...campaignKey, "preflight"] });
    // Editing an email changes what a sample approval vouches for.
    if (isHyper) {
      void queryClient.invalidateQueries({ queryKey: [...campaignKey, "personalization"] });
    }
  }

  function onMutationError(error: unknown) {
    setActionError(errorMessage(error));
    if (error instanceof ApiError && error.code === "state_conflict") {
      void queryClient.invalidateQueries({ queryKey: campaignKey });
    }
    void queryClient.invalidateQueries({ queryKey: sequenceKey });
  }

  const addMutation = useMutation({
    mutationFn: async (input: { position: number; withWait: boolean }) =>
      addSequenceStep(activeWorkspaceId as string, campaignId, {
        kind: "EMAIL",
        position: input.position,
        email_subject: isHyper ? NEW_REFERENCE_SUBJECT : NEW_EMAIL_SUBJECT,
        email_body_html: isHyper ? NEW_REFERENCE_BODY : NEW_EMAIL_BODY,
        // New follow-ups reply in the previous email's thread by default (ADR-0021).
        email_same_thread: steps.some((s) => s.kind === "EMAIL" && s.position < input.position),
        // Server inserts the wait and the email in one transaction.
        leading_wait_minutes: input.withWait ? DEFAULT_WAIT_MINUTES : undefined,
      }),
    onSuccess: (created) => {
      afterChange();
      setOpenStepId(created.id);
    },
    onError: onMutationError,
  });

  const duplicateMutation = useMutation({
    mutationFn: (step: SequenceStep) =>
      duplicateSequenceStep(activeWorkspaceId as string, campaignId, step.id),
    onSuccess: (next) => afterChange(next),
    onError: onMutationError,
  });

  const deleteMutation = useMutation({
    mutationFn: (step: SequenceStep) =>
      deleteSequenceStep(activeWorkspaceId as string, campaignId, step.id, {
        withAdjacentWait: step.kind === "EMAIL",
      }),
    onSuccess: () => afterChange(),
    onError: onMutationError,
  });

  const reorderMutation = useMutation({
    mutationFn: (ordering: { step_id: string; position: number }[]) =>
      reorderSequenceSteps(activeWorkspaceId as string, campaignId, ordering),
    onSuccess: (next) => afterChange(next),
    onError: onMutationError,
  });

  const waitMutation = useMutation({
    mutationFn: (input: { step: SequenceStep; minutes: number }) =>
      updateSequenceStep(activeWorkspaceId as string, campaignId, input.step.id, {
        expected_version: input.step.version,
        wait_duration_minutes: input.minutes,
      }),
    onMutate: (input) => setSavingWaitId(input.step.id),
    onSuccess: () => afterChange(),
    onError: onMutationError,
    onSettled: () => setSavingWaitId(null),
  });

  const busy =
    addMutation.isPending ||
    duplicateMutation.isPending ||
    deleteMutation.isPending ||
    reorderMutation.isPending;

  function onDelete(step: SequenceStep) {
    const label = step.kind === "EMAIL" ? "this email step and the wait next to it" : "this wait";
    if (!window.confirm(`Delete ${label}?`)) return;
    deleteMutation.mutate(step);
  }

  // Emails swap slots; the waits between slots stay where they are, so the
  // Email/Wait alternation is preserved and only the email content moves.
  function onMoveEmail(step: SequenceStep, direction: -1 | 1) {
    const emails = steps.filter((s) => s.kind === "EMAIL");
    const index = emails.findIndex((s) => s.id === step.id);
    const other = emails[index + direction];
    if (!other) return;
    reorderMutation.mutate(
      steps.map((s) => ({
        step_id: s.id,
        position:
          s.id === step.id ? other.position : s.id === other.id ? step.position : s.position,
      })),
    );
  }

  const openStep = openStepId ? steps.find((s) => s.id === openStepId) : undefined;
  const timings = computeTimings(steps);
  const emailSteps = steps.filter((s) => s.kind === "EMAIL");

  async function refetchOpenStep() {
    const latest = await queryClient.fetchQuery({
      queryKey: sequenceKey,
      queryFn: () => getSequence(activeWorkspaceId as string, campaignId),
      staleTime: 0,
    });
    const index = latest.steps.findIndex((s) => s.id === openStepId);
    const current = index >= 0 ? latest.steps[index] : null;
    const before = index > 0 ? latest.steps[index - 1] : null;
    return { step: current, wait: before && before.kind === "WAIT" ? before : null };
  }

  let dialog = null;
  if (openStep && openStep.kind === "EMAIL" && activeWorkspaceId) {
    const index = steps.findIndex((s) => s.id === openStep.id);
    const previous = index > 0 ? steps[index - 1] : null;
    const precedingWait = previous && previous.kind === "WAIT" ? previous : null;
    const previousEmail = [...steps.slice(0, index)].reverse().find((s) => s.kind === "EMAIL");
    dialog = (
      <EmailStepDialog
        // Remount per step so each open starts from that step's saved state.
        key={openStep.id}
        open
        workspaceId={activeWorkspaceId}
        campaignId={campaignId}
        step={openStep}
        stepNumber={emailSteps.findIndex((s) => s.id === openStep.id) + 1}
        day={timings.get(openStep.id)?.day ?? 1}
        previousEmailDay={previousEmail ? (timings.get(previousEmail.id)?.day ?? 1) : null}
        precedingWait={precedingWait}
        replySubject={replySubject(
          steps.slice(0, index).filter((s) => s.kind === "EMAIL"),
          isHyper,
        )}
        readOnly={readOnly}
        canTestSend={canExecuteCampaign(activeWorkspace?.role_code)}
        referenceMode={isHyper}
        designedEmail={designedEmail}
        mailboxes={(mailboxesQuery.data ?? []).filter((mb) => mb.active)}
        onClose={() => setOpenStepId(null)}
        onSaved={() => afterChange()}
        onRefetch={refetchOpenStep}
        onLocked={() => {
          void queryClient.invalidateQueries({ queryKey: campaignKey });
          void queryClient.invalidateQueries({ queryKey: sequenceKey });
        }}
      />
    );
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-slate-900">Sequence</h2>
        <p className="text-sm text-slate-500">
          {isHyper
            ? "Write one reference email per step. Each lead receives a version personalized to " +
              "them, written just before it is sent. Waits set how long after the previous email " +
              "a follow-up goes out."
            : "Each email is sent to every prospect in the audience. Waits set how long after the " +
              "previous email a follow-up goes out."}
        </p>
      </div>

      {pausedHyper ? (
        <Alert variant="info">
          This campaign is paused. You can change the wording of any email and the waits, and
          add, move or remove emails after the last one any lead has received. Personalized
          versions already written keep their current text; only the ones written after you
          resume use your changes. The campaign setup and attachments can&apos;t change. Edits
          take effect when you resume.
        </Alert>
      ) : pausedEdit ? (
        <Alert variant="info">
          This campaign is paused. Changes apply to emails that haven&apos;t been sent yet; a lead
          who already received an email keeps it. Emails after the last one any lead has received
          can be added, moved or removed. Edits take effect when you resume.
        </Alert>
      ) : readOnly && campaignStatus && campaignStatus !== "DRAFT" ? (
        <Alert variant="info">
          {campaignStatus === "PAUSED" && isHyper
            ? "Only a manager can edit the emails of a paused personalized campaign. Duplicate the campaign to make larger changes."
            : `This campaign is ${campaignStatus.toLowerCase()}, so its sequence is read-only. ${
                campaignStatus === "RUNNING" || campaignStatus === "SCHEDULED"
                  ? "Pause it to edit emails that haven't been sent, or duplicate it to make a new version."
                  : "Duplicate the campaign to make changes."
              }`}
        </Alert>
      ) : null}
      {actionError ? <Alert variant="error">{actionError}</Alert> : null}
      {sequenceQuery.isError ? (
        <Alert variant="error">{errorMessage(sequenceQuery.error)}</Alert>
      ) : null}

      {isHyper && capabilities && !capabilities.enabled ? (
        <Alert variant="warning">
          Hyper-personalized campaigns are not enabled for this deployment, so this campaign
          can&apos;t be launched. Ask your administrator to enable them.
        </Alert>
      ) : null}
      {isHyper && personalization.stateQuery.isError ? (
        <Alert variant="error">{errorMessage(personalization.stateQuery.error)}</Alert>
      ) : null}
      {/* The setup panel picks its starting step (AI wizard or manual objective) once,
          when it mounts. Mounting it before we know whether AI is available would
          lock it into manual mode, so it waits for the capabilities answer. */}
      {isHyper && personalizationState && !capabilities ? (
        personalization.capabilitiesQuery.isError ? (
          <Alert variant="warning">
            <p>We couldn&apos;t check whether AI writing is available. This is usually temporary.</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="mt-2"
              loading={personalization.capabilitiesQuery.isFetching}
              onClick={() => void personalization.capabilitiesQuery.refetch()}
            >
              Try again
            </Button>
          </Alert>
        ) : (
          <LoadingBlock />
        )
      ) : null}
      {isHyper && personalizationState && capabilities ? (
        <CampaignSetupPanel
          workspaceId={activeWorkspaceId ?? ""}
          campaignId={campaignId}
          state={personalizationState}
          readOnly={readOnly || pausedHyper}
          aiAvailable={aiAvailable}
          saving={personalization.saveMutation.isPending}
          saveError={personalization.saveError}
          onSave={(config) => personalization.saveMutation.mutateAsync(config)}
        />
      ) : null}
      {isHyper ? (
        <AiDraftBar
          emailCount={emailSteps.length}
          readOnly={readOnly || pausedHyper}
          hasObjective={Boolean(personalizationState?.config)}
          aiAvailable={aiAvailable}
          generation={generation}
          confirmStep={regenerateTarget}
          onConfirmStepDone={() => setRegenerateTarget(null)}
        />
      ) : null}

      {sequenceQuery.isLoading ? (
        <LoadingBlock />
      ) : steps.length === 0 ? (
        <div className="flex min-h-[160px] flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">
          <p>No steps yet. Start with an email.</p>
          {!readOnly ? (
            <button
              type="button"
              onClick={() => addMutation.mutate({ position: 1, withWait: false })}
              disabled={addMutation.isPending}
              className="inline-flex h-8 items-center rounded-md border border-slate-200 bg-white px-3 text-xs font-medium text-slate-900 hover:bg-slate-100 disabled:opacity-50"
            >
              Add email step
            </button>
          ) : null}
        </div>
      ) : (
        <SequenceTimeline
          steps={steps}
          readOnly={readOnly}
          busy={busy}
          scheduleNote={describeSchedule(latestSettings)}
          savingWaitId={savingWaitId}
          lockedThrough={lockedThrough}
          personalized={isHyper}
          onOpenEmail={(step) => setOpenStepId(step.id)}
          onDuplicate={(step) => duplicateMutation.mutate(step)}
          onDelete={onDelete}
          onMoveEmail={onMoveEmail}
          onSaveWait={(step, minutes) => waitMutation.mutate({ step, minutes })}
          onInsertAfter={(step) =>
            addMutation.mutate({ position: step.position + 1, withWait: true })
          }
          onAddEmail={() =>
            addMutation.mutate({ position: steps.length + 1, withWait: steps.length > 0 })
          }
          onRegenerateEmail={
            isHyper && !pausedHyper && aiAvailable && personalizationState?.config
              ? (step, number) => setRegenerateTarget({ id: step.id, number })
              : undefined
          }
          regeneratingStepId={
            generation.pendingRequest?.scope === "STEP"
              ? (generation.pendingRequest.step_id ?? null)
              : null
          }
          generating={generation.isPending}
        />
      )}

      {isHyper && personalizationState && campaignStatus === "DRAFT" ? (
        <PreviewAndApprovalSection
          workspaceId={activeWorkspaceId}
          campaignId={campaignId}
          state={personalizationState}
          personalization={personalization}
          role={activeWorkspace?.role_code}
          mayDraft={mayDraft}
          readOnly={readOnly}
          changedSinceApproval={personalizationState.approval.status === "STALE"}
        />
      ) : null}
      {isHyper && campaignStatus && campaignStatus !== "DRAFT" ? (
        <GenerationProgressPanel
          workspaceId={activeWorkspaceId ?? ""}
          campaignId={campaignId}
          active={ready}
        />
      ) : null}

      {dialog}
    </div>
  );
}
