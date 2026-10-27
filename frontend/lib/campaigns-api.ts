import { apiRequest } from "@/lib/api-client";
import type {
  AudienceExclusionBreakdown,
  BulkActionResult,
  BulkItem,
  CampaignAudience,
  CampaignDetail,
  CampaignCounts,
  CampaignSort,
  CampaignType,
  CampaignMailbox,
  CampaignPage,
  CampaignPlanning,
  CampaignReview,
  CampaignSequence,
  CampaignSettings,
  CampaignSupplement,
  MailboxTestSendResult,
  PreflightResult,
  SequencePreviewRecipients,
  SequenceStep,
  SequenceStepKind,
  StepAttachment,
} from "@/types/domain";

function workspacePath(workspaceId: string, path: string) {
  return `/api/v1/workspaces/${workspaceId}${path}`;
}

function appendSearch(path: string, params: Record<string, string | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value);
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// --- Campaign CRUD ---

export async function listCampaigns(
  workspaceId: string,
  params: {
    limit?: number;
    cursor?: string | null;
    q?: string | null;
    status?: string | null;
    type?: CampaignType | null;
    sort?: CampaignSort | null;
  } = {},
) {
  const path = appendSearch(workspacePath(workspaceId, "/campaigns"), {
    limit: params.limit?.toString(),
    cursor: params.cursor ?? undefined,
    q: params.q ?? undefined,
    status: params.status ?? undefined,
    type: params.type ?? undefined,
    sort: params.sort ?? undefined,
  });
  return (await apiRequest<CampaignPage>(path)).data;
}

// Campaigns per status under the same search and type filter as the list.
export async function getCampaignCounts(
  workspaceId: string,
  params: { q?: string | null; type?: CampaignType | null } = {},
) {
  const path = appendSearch(workspacePath(workspaceId, "/campaigns/counts"), {
    q: params.q ?? undefined,
    type: params.type ?? undefined,
  });
  return (await apiRequest<CampaignCounts>(path)).data;
}

export async function createCampaign(
  workspaceId: string,
  payload: {
    name: string;
    description?: string | null;
    // Chosen once and immutable; omitted means STANDARD.
    campaign_type?: CampaignType;
  },
) {
  const path = workspacePath(workspaceId, "/campaigns");
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
      idempotencyKey: newIdempotencyKey(),
    })
  ).data;
}

export async function getCampaign(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}`);
  return (await apiRequest<CampaignDetail>(path)).data;
}

export async function updateCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { expected_version: number; name?: string; description?: string | null },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "PATCH",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function archiveCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { expected_version: number },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/archive`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function bulkArchiveCampaigns(workspaceId: string, items: BulkItem[]) {
  const path = workspacePath(workspaceId, "/campaigns/bulk-archive");
  return (
    await apiRequest<BulkActionResult>(path, {
      method: "POST",
      body: JSON.stringify({ items }),
    })
  ).data;
}

export async function duplicateCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { name?: string | null } = {},
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/duplicate`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

// --- Sequence ---

export async function getSequence(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/sequence`);
  return (await apiRequest<CampaignSequence>(path)).data;
}

export async function addSequenceStep(
  workspaceId: string,
  campaignId: string,
  payload: {
    kind: SequenceStepKind;
    position: number;
    email_subject?: string | null;
    email_body_html?: string | null;
    email_preheader?: string | null;
    // EMAIL follow-ups only: reply in the previous email's thread (ADR-0021).
    email_same_thread?: boolean;
    source_template_version_id?: string | null;
    // EMAIL only: insert a WAIT of this many minutes directly before the new
    // step, in the same transaction.
    leading_wait_minutes?: number | null;
    wait_duration_minutes?: number | null;
  },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/sequence/steps`);
  return (
    await apiRequest<SequenceStep>(path, { method: "POST", body: JSON.stringify(payload) })
  ).data;
}

export async function updateSequenceStep(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  payload: {
    expected_version: number;
    email_subject?: string | null;
    email_body_html?: string | null;
    // "" clears the pre-header; omit to leave it unchanged.
    email_preheader?: string | null;
    // Omit to leave it unchanged.
    email_same_thread?: boolean;
    source_template_version_id?: string | null;
    wait_duration_minutes?: number | null;
  },
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}`,
  );
  return (
    await apiRequest<SequenceStep>(path, { method: "PATCH", body: JSON.stringify(payload) })
  ).data;
}

export async function deleteSequenceStep(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  // When deleting an EMAIL step, also remove its neighbouring WAIT atomically so
  // the Email/Wait alternation stays valid.
  options: { withAdjacentWait?: boolean } = {},
) {
  const path = appendSearch(
    workspacePath(workspaceId, `/campaigns/${campaignId}/sequence/steps/${stepId}`),
    { with_adjacent_wait: options.withAdjacentWait ? "true" : undefined },
  );
  await apiRequest<void>(path, { method: "DELETE" });
}

export async function listSequencePreviewRecipients(
  workspaceId: string,
  campaignId: string,
  params: { limit?: number; afterOrdinal?: number | null } = {},
) {
  const path = appendSearch(
    workspacePath(workspaceId, `/campaigns/${campaignId}/sequence/preview-recipients`),
    {
      limit: params.limit?.toString(),
      after_ordinal:
        params.afterOrdinal === null || params.afterOrdinal === undefined
          ? undefined
          : String(params.afterOrdinal),
    },
  );
  return (await apiRequest<SequencePreviewRecipients>(path)).data;
}

// Sends one real, rendered copy of the step to an address the caller confirms.
// Unsaved editor content can be passed to test exactly what is on screen.
export async function sendSequenceStepTestEmail(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  payload: {
    mailbox_id: string;
    recipient_email: string;
    confirm_recipient: boolean;
    audience_member_id?: string | null;
    email_subject?: string | null;
    email_body_html?: string | null;
    email_preheader?: string | null;
  },
  idempotencyKey: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}/test-send`,
  );
  return (
    await apiRequest<MailboxTestSendResult>(path, {
      method: "POST",
      body: JSON.stringify(payload),
      idempotencyKey,
    })
  ).data;
}

// Files are stored immediately (they are not part of the step's text draft).
export async function uploadStepAttachment(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  file: File,
  disposition: "ATTACHMENT" | "INLINE",
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}/attachments`,
  );
  const form = new FormData();
  form.append("file", file);
  form.append("disposition", disposition);
  return (await apiRequest<StepAttachment>(path, { method: "POST", body: form })).data;
}

export async function deleteStepAttachment(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  attachmentId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}/attachments/${attachmentId}`,
  );
  await apiRequest<void>(path, { method: "DELETE" });
}

// Short-lived URL for showing a private file (e.g. an inline image) in the browser.
export async function getStepAttachmentUrl(
  workspaceId: string,
  campaignId: string,
  stepId: string,
  attachmentId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}/attachments/${attachmentId}/url`,
  );
  return (await apiRequest<{ url: string; expires_in: number }>(path)).data;
}

export async function duplicateSequenceStep(
  workspaceId: string,
  campaignId: string,
  stepId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/${stepId}/duplicate`,
  );
  return (await apiRequest<CampaignSequence>(path, { method: "POST" })).data;
}

export async function reorderSequenceSteps(
  workspaceId: string,
  campaignId: string,
  steps: { step_id: string; position: number }[],
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/sequence/steps/reorder`,
  );
  return (
    await apiRequest<CampaignSequence>(path, {
      method: "POST",
      body: JSON.stringify({ steps }),
    })
  ).data;
}

// --- Mailboxes ---

export async function listCampaignMailboxes(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/mailboxes`);
  return (await apiRequest<CampaignMailbox[]>(path)).data;
}

export async function assignCampaignMailbox(
  workspaceId: string,
  campaignId: string,
  mailboxId: string,
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/mailboxes`);
  return (
    await apiRequest<CampaignMailbox[]>(path, {
      method: "POST",
      body: JSON.stringify({ mailbox_id: mailboxId }),
    })
  ).data;
}

export async function unassignCampaignMailbox(
  workspaceId: string,
  campaignId: string,
  mailboxId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/mailboxes/${mailboxId}`,
  );
  return (await apiRequest<CampaignMailbox[]>(path, { method: "DELETE" })).data;
}

export async function reorderCampaignMailboxes(
  workspaceId: string,
  campaignId: string,
  mailboxIds: string[],
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/mailboxes/reorder`);
  return (
    await apiRequest<CampaignMailbox[]>(path, {
      method: "POST",
      body: JSON.stringify({ mailbox_ids: mailboxIds }),
    })
  ).data;
}

// --- Schedule / settings ---

export async function listCampaignSettings(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/settings`);
  return (await apiRequest<CampaignSettings[]>(path)).data;
}

export async function createCampaignSettings(
  workspaceId: string,
  campaignId: string,
  payload: {
    timezone: string;
    weekdays: number[];
    window_start_local: string;
    window_end_local: string;
    daily_limit?: number | null;
  },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/settings`);
  return (
    await apiRequest<CampaignSettings>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

// --- Audience ---

export async function getCommittedAudience(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/audience`);
  return (await apiRequest<CampaignAudience | null>(path)).data;
}

export async function selectAudience(
  workspaceId: string,
  campaignId: string,
  payload: { lead_ids: string[]; list_ids: string[] },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/audience/select`);
  return (
    await apiRequest<CampaignAudience>(path, {
      method: "POST",
      body: JSON.stringify(payload),
      idempotencyKey: newIdempotencyKey(),
    })
  ).data;
}

export async function getAudienceCaptureStatus(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/${audienceId}`,
  );
  return (await apiRequest<CampaignAudience>(path)).data;
}

export async function getAudienceExclusions(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/${audienceId}/exclusions`,
  );
  return (await apiRequest<AudienceExclusionBreakdown>(path)).data;
}

export async function commitAudience(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/${audienceId}/commit`,
  );
  return (await apiRequest<CampaignAudience>(path, { method: "POST" })).data;
}

// --- Adding leads to a paused campaign (ADR-0020) ---

export async function listSupplements(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/audience/supplements`);
  return (await apiRequest<CampaignSupplement[]>(path)).data;
}

export async function selectSupplement(
  workspaceId: string,
  campaignId: string,
  payload: { lead_ids: string[]; list_ids: string[] },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/audience/supplements`);
  return (
    await apiRequest<CampaignSupplement>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function enrollSupplement(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/supplements/${audienceId}/enroll`,
  );
  return (await apiRequest<CampaignSupplement>(path, { method: "POST" })).data;
}

export async function retrySupplement(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/supplements/${audienceId}/retry`,
  );
  return (await apiRequest<CampaignSupplement>(path, { method: "POST" })).data;
}

export async function abandonAudience(
  workspaceId: string,
  campaignId: string,
  audienceId: string,
) {
  const path = workspacePath(
    workspaceId,
    `/campaigns/${campaignId}/audience/${audienceId}/abandon`,
  );
  await apiRequest<void>(path, { method: "POST" });
}

// --- Activation / planning ---

export async function activateCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { expected_version: number; start_at?: string | null },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/activate`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
      idempotencyKey: newIdempotencyKey(),
    })
  ).data;
}

export async function pauseCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { expected_version: number },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/pause`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function resumeCampaign(
  workspaceId: string,
  campaignId: string,
  payload: { expected_version: number; include_completed?: boolean },
) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/resume`);
  return (
    await apiRequest<CampaignDetail>(path, {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function getCampaignPlanning(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/planning`);
  return (await apiRequest<CampaignPlanning>(path)).data;
}

// --- Preflight / Review ---

export async function getPreflight(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/preflight`);
  return (await apiRequest<PreflightResult>(path)).data;
}

export async function getReview(workspaceId: string, campaignId: string) {
  const path = workspacePath(workspaceId, `/campaigns/${campaignId}/review`);
  return (await apiRequest<CampaignReview>(path)).data;
}
