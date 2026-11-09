import { apiRequest } from "@/lib/api-client";
import type { SafetyHold } from "@/types/domain";

function holdsPath(workspaceId: string, mailboxId: string) {
  return `/api/v1/workspaces/${workspaceId}/mailboxes/${mailboxId}/safety-holds`;
}

export async function listSafetyHolds(
  workspaceId: string,
  mailboxId: string,
): Promise<SafetyHold[]> {
  return (await apiRequest<SafetyHold[]>(holdsPath(workspaceId, mailboxId))).data;
}

export async function releaseSafetyHold(
  workspaceId: string,
  mailboxId: string,
  holdId: string,
): Promise<SafetyHold> {
  return (
    await apiRequest<SafetyHold>(`${holdsPath(workspaceId, mailboxId)}/${holdId}/release`, {
      method: "POST",
    })
  ).data;
}
