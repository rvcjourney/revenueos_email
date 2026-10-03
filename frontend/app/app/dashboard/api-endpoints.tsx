"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Card, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiUrl } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { listLeadLists } from "@/lib/leads-api";
import { listMailboxes } from "@/lib/mailboxes-api";
import { canDraftCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";

const RESPONSES = [
  { code: "201", meaning: "Stored as a draft campaign. The body carries campaign_id and duplicate: false." },
  { code: "200", meaning: "Same reference and same content sent again. Returns the same campaign with duplicate: true." },
  { code: "409", meaning: "Same reference with different content. Nothing is stored." },
  { code: "422", meaning: "The body is missing a field or breaks a rule below." },
  { code: "401", meaning: "The X-RevenueOS-Key header is missing or wrong." },
  { code: "404", meaning: "The RevenueOS user is not a member of this workspace." },
  { code: "503", meaning: "The integration is not configured on the server." },
];

const RULES = [
  "reference is RevenueOS's own id for the campaign. Sending it again is safe.",
  "The first email has wait_days_before 0; every follow-up has 1 or more. At most 10 emails.",
  "audience takes list_ids, lead_ids, or both. Leads must already exist in this workspace.",
  "mailbox_id is a mailbox connected in this workspace. Credentials are never sent.",
  "The campaign is stored as a draft. A person reviews the audience and starts it here.",
];

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Clipboard access is denied on insecure origins; the value stays
      // selectable on screen, so there is nothing further to report.
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <button
      type="button"
      onClick={() => void copy()}
      aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
      className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-white text-slate-500 transition hover:border-brand-300 hover:text-brand-700"
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
      )}
    </button>
  );
}

function CopyableValue({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 break-all rounded-md bg-slate-50 px-2 py-1 font-mono text-xs text-slate-800">
        {value}
      </code>
      <CopyButton value={value} label={label} />
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">{children}</p>
  );
}

// Reference for the RevenueOS campaign intake route (docs/adr/0019). Everything
// shown is either public or already readable by this member; the shared key
// lives only in server settings and is never sent to the browser.
export function ApiEndpoints() {
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const mayDraft = canDraftCampaign(activeWorkspace?.role_code);
  const enabled = Boolean(activeWorkspaceId) && mayDraft;

  const mailboxesQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
    queryFn: () => listMailboxes(activeWorkspaceId!),
    enabled,
  });

  const listsQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "lead-lists", "picker"],
    queryFn: () => listLeadLists(activeWorkspaceId!, { limit: 100 }),
    enabled,
  });

  // The route needs campaigns.draft, so the reference is noise for a Viewer.
  if (!activeWorkspaceId || !mayDraft) return null;

  const endpoint = apiUrl(
    `/api/v1/workspaces/${activeWorkspaceId}/integrations/revenueos/campaigns`,
  );
  const mailboxes = mailboxesQuery.data ?? [];
  const lists = listsQuery.data?.items ?? [];

  const exampleBody = JSON.stringify(
    {
      reference: "STRAT-0042",
      campaign: { name: "Campaign name", description: "Optional description" },
      emails: [
        { subject: "First email", body_html: "<p>Hello</p>", preheader: null, wait_days_before: 0 },
        { subject: "Follow-up", body_html: "<p>Following up</p>", preheader: null, wait_days_before: 3 },
      ],
      schedule: {
        timezone: "Asia/Kolkata",
        weekdays: [1, 2, 3, 4, 5],
        window_start_local: "09:30:00",
        window_end_local: "17:30:00",
        daily_limit: 50,
      },
      audience: { list_ids: [lists[0]?.id ?? "<list id>"], lead_ids: [] },
      mailbox_id: mailboxes[0]?.id ?? "<mailbox id>",
    },
    null,
    2,
  );

  return (
    <section aria-labelledby="api-endpoints-heading" className="space-y-4">
      <h2 id="api-endpoints-heading" className="text-base font-semibold text-slate-900">
        API endpoints
      </h2>

      <Card className="space-y-6">
        <CardHeader
          title="RevenueOS campaign intake"
          description="RevenueOS sends a finished campaign here. It is stored as a draft in this workspace and never started automatically."
        />

        <div className="space-y-2">
          <FieldLabel>Endpoint</FieldLabel>
          <div className="flex items-center gap-2">
            <span className="shrink-0 rounded-md bg-emerald-50 px-2 py-1 text-xs font-bold text-emerald-700">
              POST
            </span>
            <div className="min-w-0 flex-1">
              <CopyableValue value={endpoint} label="endpoint URL" />
            </div>
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Authentication</FieldLabel>
            <p className="text-sm text-slate-600">
              Send the shared key in the{" "}
              <code className="rounded bg-slate-50 px-1 font-mono text-xs">X-RevenueOS-Key</code>{" "}
              header, with{" "}
              <code className="rounded bg-slate-50 px-1 font-mono text-xs">
                Content-Type: application/json
              </code>
              . The key is set on the server as{" "}
              <code className="rounded bg-slate-50 px-1 font-mono text-xs">REVENUEOS_INTAKE_KEY</code>{" "}
              and is never shown here.
            </p>
          </div>
          <div className="space-y-2">
            <FieldLabel>Workspace ID</FieldLabel>
            <CopyableValue value={activeWorkspaceId} label="workspace ID" />
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Mailbox IDs</FieldLabel>
            {mailboxesQuery.isLoading ? (
              <Skeleton className="h-8 w-full" />
            ) : mailboxesQuery.isError ? (
              <Alert variant="error">
                {errorMessage(mailboxesQuery.error, "We couldn't load your mailboxes.")}
              </Alert>
            ) : mailboxes.length === 0 ? (
              <p className="text-sm text-slate-500">
                No mailbox is connected yet.{" "}
                <Link href="/app/mailboxes/connect" className="font-medium text-brand-700 hover:text-brand-900">
                  Connect a mailbox
                </Link>
              </p>
            ) : (
              <ul className="space-y-2">
                {mailboxes.map((mailbox) => (
                  <li key={mailbox.id} className="space-y-1">
                    <p className="truncate text-sm font-medium text-slate-900">{mailbox.email_address}</p>
                    <CopyableValue value={mailbox.id} label={`mailbox ID for ${mailbox.email_address}`} />
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="space-y-2">
            <FieldLabel>List IDs</FieldLabel>
            {listsQuery.isLoading ? (
              <Skeleton className="h-8 w-full" />
            ) : listsQuery.isError ? (
              <Alert variant="error">
                {errorMessage(listsQuery.error, "We couldn't load your lists.")}
              </Alert>
            ) : lists.length === 0 ? (
              <p className="text-sm text-slate-500">
                No list exists yet.{" "}
                <Link href="/app/leads/lists" className="font-medium text-brand-700 hover:text-brand-900">
                  Create a list
                </Link>
              </p>
            ) : (
              <ul className="space-y-2">
                {lists.map((list) => (
                  <li key={list.id} className="space-y-1">
                    <p className="truncate text-sm font-medium text-slate-900">
                      {list.name}{" "}
                      <span className="font-normal text-slate-500">({list.member_count} leads)</span>
                    </p>
                    <CopyableValue value={list.id} label={`list ID for ${list.name}`} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <FieldLabel>Request body</FieldLabel>
            <CopyButton value={exampleBody} label="request body" />
          </div>
          <pre className="max-h-80 overflow-auto rounded-lg bg-slate-900 p-4 font-mono text-xs leading-relaxed text-slate-100">
            {exampleBody}
          </pre>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Rules</FieldLabel>
            <ul className="list-disc space-y-1.5 pl-4 text-sm text-slate-600">
              {RULES.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
          </div>
          <div className="space-y-2">
            <FieldLabel>Responses</FieldLabel>
            <table className="w-full text-left text-sm">
              <tbody className="divide-y divide-slate-100">
                {RESPONSES.map(({ code, meaning }) => (
                  <tr key={code}>
                    <th scope="row" className="w-12 py-1.5 pr-3 align-top font-mono text-xs font-semibold text-slate-900">
                      {code}
                    </th>
                    <td className="py-1.5 text-slate-600">{meaning}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Card>
    </section>
  );
}
