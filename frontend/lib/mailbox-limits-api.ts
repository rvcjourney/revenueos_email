import { apiRequest } from "@/lib/api-client";
import type { MailboxLimits, MailboxLimitsInput } from "@/types/domain";

function limitsPath(workspaceId: string, mailboxId: string) {
  return `/api/v1/workspaces/${workspaceId}/mailboxes/${mailboxId}/limits`;
}

export async function getMailboxLimits(
  workspaceId: string,
  mailboxId: string,
): Promise<MailboxLimits> {
  return (await apiRequest<MailboxLimits>(limitsPath(workspaceId, mailboxId))).data;
}

export async function setMailboxLimits(
  workspaceId: string,
  mailboxId: string,
  payload: MailboxLimitsInput,
): Promise<MailboxLimits> {
  return (
    await apiRequest<MailboxLimits>(limitsPath(workspaceId, mailboxId), {
      method: "PUT",
      body: JSON.stringify(payload),
    })
  ).data;
}
