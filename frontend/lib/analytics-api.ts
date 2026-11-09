import { apiRequest } from "@/lib/api-client";
import type {
  CampaignActivityPage,
  CampaignActivityStatus,
  CampaignActivityType,
  CampaignAnalytics,
  CampaignSequenceAnalytics,
  DeliverabilityOverview,
  WorkspaceOverviewAnalytics,
} from "@/types/analytics";

export type WorkspaceOverviewParams = {
  startDate?: string | null;
  endDate?: string | null;
  timezone?: string | null;
  campaignId?: string | null;
  mailboxId?: string | null;
};

export async function getWorkspaceOverview(
  workspaceId: string,
  params: WorkspaceOverviewParams = {},
): Promise<WorkspaceOverviewAnalytics> {
  const query = new URLSearchParams();
  if (params.startDate) query.set("start_date", params.startDate);
  if (params.endDate) query.set("end_date", params.endDate);
  if (params.timezone) query.set("timezone", params.timezone);
  if (params.campaignId) query.set("campaign_id", params.campaignId);
  if (params.mailboxId) query.set("mailbox_id", params.mailboxId);

  const qs = query.toString();
  const endpoint = `/api/v1/workspaces/${workspaceId}/analytics/overview${qs ? `?${qs}` : ""}`;
  const response = await apiRequest<WorkspaceOverviewAnalytics>(endpoint);
  return response.data;
}

export async function getCampaignAnalytics(
  workspaceId: string,
  campaignId: string,
): Promise<CampaignAnalytics> {
  const endpoint = `/api/v1/workspaces/${workspaceId}/analytics/campaigns/${campaignId}`;
  const response = await apiRequest<CampaignAnalytics>(endpoint);
  return response.data;
}

export async function getCampaignSequenceAnalytics(
  workspaceId: string,
  campaignId: string,
): Promise<CampaignSequenceAnalytics> {
  const endpoint = `/api/v1/workspaces/${workspaceId}/analytics/campaigns/${campaignId}/sequence`;
  const response = await apiRequest<CampaignSequenceAnalytics>(endpoint);
  return response.data;
}

export type CampaignActivityParams = {
  type?: CampaignActivityType | null;
  status?: CampaignActivityStatus | null;
  startDate?: string | null;
  endDate?: string | null;
  q?: string | null;
  limit?: number;
  cursor?: string | null;
};

export async function getCampaignActivity(
  workspaceId: string,
  campaignId: string,
  params: CampaignActivityParams = {},
): Promise<CampaignActivityPage> {
  const query = new URLSearchParams();
  if (params.type) query.set("type", params.type);
  if (params.status) query.set("status", params.status);
  if (params.startDate) query.set("start_date", params.startDate);
  if (params.endDate) query.set("end_date", params.endDate);
  if (params.q) query.set("q", params.q);
  if (params.limit) query.set("limit", String(params.limit));
  if (params.cursor) query.set("cursor", params.cursor);

  const qs = query.toString();
  const endpoint = `/api/v1/workspaces/${workspaceId}/analytics/campaigns/${campaignId}/activity${qs ? `?${qs}` : ""}`;
  const response = await apiRequest<CampaignActivityPage>(endpoint);
  return response.data;
}

export type DeliverabilityOverviewParams = {
  startDate?: string | null;
  endDate?: string | null;
};

export async function getDeliverabilityOverview(
  workspaceId: string,
  params: DeliverabilityOverviewParams = {},
): Promise<DeliverabilityOverview> {
  const query = new URLSearchParams();
  if (params.startDate) query.set("start_date", params.startDate);
  if (params.endDate) query.set("end_date", params.endDate);

  const qs = query.toString();
  const endpoint = `/api/v1/workspaces/${workspaceId}/analytics/deliverability${qs ? `?${qs}` : ""}`;
  const response = await apiRequest<DeliverabilityOverview>(endpoint);
  return response.data;
}
