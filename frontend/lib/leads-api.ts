import { apiRequest } from "@/lib/api-client";
import type {
  BulkActionResult,
  BulkItem,
  Lead,
  LeadDetail,
  LeadList,
  LeadListMemberPage,
  LeadListPage,
  LeadPage,
  LeadProfile,
  LeadStatus,
  LeadValidationStatus,
} from "@/types/domain";

export type LeadPayload = Partial<LeadProfile> & {
  email: string;
  first_name?: string | null;
  last_name?: string | null;
  company?: string | null;
  title?: string | null;
  custom_fields?: Record<string, unknown>;
  list_id?: string | null;
};

export type LeadUpdatePayload = Partial<LeadPayload> & {
  validation_status?: LeadValidationStatus;
  expected_version: number;
};

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

export async function listLeads(
  workspaceId: string,
  params: {
    limit?: number;
    cursor?: string | null;
    q?: string | null;
    status?: LeadStatus | "ALL";
    list_id?: string | null;
  } = {},
) {
  const path = appendSearch(workspacePath(workspaceId, "/leads"), {
    limit: params.limit?.toString(),
    cursor: params.cursor ?? undefined,
    q: params.q ?? undefined,
    status: params.status,
    list_id: params.list_id ?? undefined,
  });
  return (await apiRequest<LeadPage>(path)).data;
}

// The server clamps a page to 100, so reaching every match means following the cursor.
export async function listAllLeadIds(
  workspaceId: string,
  params: { q?: string | null; status?: LeadStatus | "ALL" } = {},
  onProgress?: (found: number) => void,
) {
  const ids: string[] = [];
  let cursor: string | null = null;
  do {
    const page: LeadPage = await listLeads(workspaceId, { ...params, limit: 100, cursor });
    for (const lead of page.items) ids.push(lead.id);
    onProgress?.(ids.length);
    cursor = page.next_cursor;
  } while (cursor);
  return ids;
}

export async function createLead(workspaceId: string, payload: LeadPayload) {
  return (
    await apiRequest<Lead>(workspacePath(workspaceId, "/leads"), {
      method: "POST",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function getLead(workspaceId: string, leadId: string) {
  return (
    await apiRequest<LeadDetail>(workspacePath(workspaceId, `/leads/${leadId}`))
  ).data;
}

export async function updateLead(
  workspaceId: string,
  leadId: string,
  payload: LeadUpdatePayload,
) {
  return (
    await apiRequest<Lead>(workspacePath(workspaceId, `/leads/${leadId}`), {
      method: "PATCH",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function archiveLead(
  workspaceId: string,
  leadId: string,
  expectedVersion: number,
) {
  return (
    await apiRequest<Lead>(workspacePath(workspaceId, `/leads/${leadId}/archive`), {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    })
  ).data;
}

export async function unarchiveLead(
  workspaceId: string,
  leadId: string,
  expectedVersion: number,
) {
  return (
    await apiRequest<Lead>(workspacePath(workspaceId, `/leads/${leadId}/unarchive`), {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    })
  ).data;
}

function bulkPost(workspaceId: string, path: string, items: BulkItem[]) {
  return apiRequest<BulkActionResult>(workspacePath(workspaceId, path), {
    method: "POST",
    body: JSON.stringify({ items }),
  }).then((res) => res.data);
}

export const bulkArchiveLeads = (workspaceId: string, items: BulkItem[]) =>
  bulkPost(workspaceId, "/leads/bulk-archive", items);
export const bulkUnarchiveLeads = (workspaceId: string, items: BulkItem[]) =>
  bulkPost(workspaceId, "/leads/bulk-unarchive", items);
export const bulkArchiveLeadLists = (workspaceId: string, items: BulkItem[]) =>
  bulkPost(workspaceId, "/lead-lists/bulk-archive", items);
export const bulkUnarchiveLeadLists = (workspaceId: string, items: BulkItem[]) =>
  bulkPost(workspaceId, "/lead-lists/bulk-unarchive", items);

export async function unarchiveLeadList(
  workspaceId: string,
  listId: string,
  expectedVersion: number,
) {
  return (
    await apiRequest<LeadList>(
      workspacePath(workspaceId, `/lead-lists/${listId}/unarchive`),
      {
        method: "POST",
        body: JSON.stringify({ expected_version: expectedVersion }),
      },
    )
  ).data;
}

export async function listLeadLists(
  workspaceId: string,
  params: {
    limit?: number;
    cursor?: string | null;
    status?: "ACTIVE" | "ARCHIVED" | "ALL";
  } = {},
) {
  const path = appendSearch(workspacePath(workspaceId, "/lead-lists"), {
    limit: params.limit?.toString(),
    cursor: params.cursor ?? undefined,
    status: params.status,
  });
  return (await apiRequest<LeadListPage>(path)).data;
}

export async function createLeadList(workspaceId: string, name: string) {
  return (
    await apiRequest<LeadList>(workspacePath(workspaceId, "/lead-lists"), {
      method: "POST",
      body: JSON.stringify({ name }),
    })
  ).data;
}

export async function getLeadList(workspaceId: string, listId: string) {
  return (
    await apiRequest<LeadList>(workspacePath(workspaceId, `/lead-lists/${listId}`))
  ).data;
}

export async function updateLeadList(
  workspaceId: string,
  listId: string,
  payload: { name?: string; expected_version: number },
) {
  return (
    await apiRequest<LeadList>(workspacePath(workspaceId, `/lead-lists/${listId}`), {
      method: "PATCH",
      body: JSON.stringify(payload),
    })
  ).data;
}

export async function archiveLeadList(
  workspaceId: string,
  listId: string,
  expectedVersion: number,
) {
  return (
    await apiRequest<LeadList>(
      workspacePath(workspaceId, `/lead-lists/${listId}/archive`),
      {
        method: "POST",
        body: JSON.stringify({ expected_version: expectedVersion }),
      },
    )
  ).data;
}

export async function listLeadListMembers(
  workspaceId: string,
  listId: string,
  params: { limit?: number; cursor?: string | null } = {},
) {
  const path = appendSearch(
    workspacePath(workspaceId, `/lead-lists/${listId}/members`),
    {
      limit: params.limit?.toString(),
      cursor: params.cursor ?? undefined,
    },
  );
  return (await apiRequest<LeadListMemberPage>(path)).data;
}

export async function addLeadListMember(
  workspaceId: string,
  listId: string,
  leadId: string,
) {
  await apiRequest<void>(workspacePath(workspaceId, `/lead-lists/${listId}/members`), {
    method: "POST",
    body: JSON.stringify({ lead_id: leadId }),
  });
}

export async function removeLeadListMember(
  workspaceId: string,
  listId: string,
  leadId: string,
) {
  await apiRequest<void>(
    workspacePath(workspaceId, `/lead-lists/${listId}/members/${leadId}`),
    { method: "DELETE" },
  );
}

export type LeadActivityKind =
  | "EMAIL_SENT"
  | "EMAIL_OPENED"
  | "EMAIL_BOUNCED"
  | "REPLY_RECEIVED"
  | "AUTO_REPLY_RECEIVED";

export type LeadActivityItem = {
  kind: LeadActivityKind;
  occurred_at: string;
  message_id: string;
  subject: string | null;
  campaign_id: string | null;
  campaign_name: string | null;
  sequence_step_position: number | null;
  conversation_id: string | null;
  sender_email: string | null;
  body_preview: string | null;
  occurrence_count: number | null;
  bounce_type: string | null;
};

export type LeadActivity = { lead_id: string; items: LeadActivityItem[] };

export async function getLeadActivity(workspaceId: string, leadId: string) {
  return (
    await apiRequest<LeadActivity>(
      workspacePath(workspaceId, `/leads/${leadId}/activity`),
    )
  ).data;
}
