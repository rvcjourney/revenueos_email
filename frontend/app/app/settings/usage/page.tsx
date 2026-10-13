"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  BarChart2,
  Contact,
  CreditCard,
  HardDrive,
  Loader2,
  Mail,
  Send,
  Sparkles,
  Users,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { getWorkspaceUsage } from "@/lib/usage-api";
import { useWorkspace } from "@/lib/workspace-context";
import type { DimensionUsage } from "@/types/domain";

const DIMENSION_CONFIG: Record<
  string,
  { label: string; icon: React.ComponentType<{ className?: string }>; description: string }
> = {
  sent_messages_today: {
    label: "Messages Sent Today",
    icon: Send,
    description: "Total emails dispatched during the current UTC calendar day.",
  },
  active_mailboxes: {
    label: "Connected Mailboxes",
    icon: Mail,
    description: "Active SMTP/IMAP, Google, or Microsoft mailbox accounts.",
  },
  leads: {
    label: "Stored Leads",
    icon: Contact,
    description: "Active contacts and recipients stored across workspace lists.",
  },
  active_campaigns: {
    label: "Active Campaigns",
    icon: Activity,
    description: "Campaigns currently in RUNNING or SCHEDULED sending states.",
  },
  team_members: {
    label: "Team Members",
    icon: Users,
    description: "Collaborators with active workspace memberships.",
  },
};

export default function WorkspaceUsagePage() {
  const { activeWorkspaceId } = useWorkspace();

  const {
    data: usage,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "usage"],
    queryFn: () => getWorkspaceUsage(activeWorkspaceId!),
    enabled: Boolean(activeWorkspaceId),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 p-16 text-sm text-slate-500">
        <Loader2 className="h-5 w-5 animate-spin text-brand-600" />
        Calculating workspace usage&hellip;
      </div>
    );
  }

  if (isError || !usage) {
    return (
      <div className="max-w-3xl">
        <Alert variant="error">
          Failed to load workspace usage metrics. Please try again.
        </Alert>
      </div>
    );
  }

  return (
    <main className="space-y-6 max-w-4xl pb-16">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-brand-600">Billing &amp; Limits</p>
          <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-1">
            Usage &amp; Quotas
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Real-time usage metrics and entitlement thresholds for this workspace.
          </p>
        </div>

        <div className="inline-flex items-center gap-2 rounded-full border border-brand-200 bg-brand-50 px-3.5 py-1 text-xs font-semibold text-brand-800">
          <Sparkles className="h-3.5 w-3.5" />
          Plan: {usage.plan_name}
        </div>
      </div>

      {/* Dimension Progress Cards */}
      <div className="grid gap-4 sm:grid-cols-2">
        {Object.entries(usage.dimensions).map(([key, dim]: [string, DimensionUsage]) => {
          const config = DIMENSION_CONFIG[key] ?? {
            label: key,
            icon: BarChart2,
            description: "",
          };
          const Icon = config.icon;
          const percentage =
            dim.limit && dim.limit > 0
              ? Math.min(Math.round((dim.used / dim.limit) * 100), 100)
              : 0;

          const isNearLimit = percentage >= 80 && percentage < 95;
          const isAtLimit = percentage >= 95;

          const barColor = isAtLimit
            ? "bg-red-500"
            : isNearLimit
            ? "bg-amber-500"
            : "bg-brand-600";

          return (
            <div
              key={key}
              className="rounded-xl border border-slate-200 bg-white p-5 shadow-card space-y-3"
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className="rounded-md bg-slate-100 p-2 text-slate-700">
                    <Icon className="h-4 w-4" />
                  </div>
                  <h3 className="text-sm font-semibold text-slate-900">
                    {config.label}
                  </h3>
                </div>
                <span className="text-xs font-mono font-medium text-slate-600">
                  {dim.used.toLocaleString()} /{" "}
                  {dim.limit ? dim.limit.toLocaleString() : "∞"} {dim.unit}
                </span>
              </div>

              {/* Progress Bar */}
              <div className="space-y-1">
                <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
                  <div
                    className={`h-full transition-all duration-300 ${barColor}`}
                    style={{ width: `${percentage}%` }}
                  />
                </div>
                <div className="flex justify-between items-center text-[11px] text-slate-400">
                  <span>{percentage}% used</span>
                  {isAtLimit && (
                    <span className="font-semibold text-red-600 flex items-center gap-1">
                      <AlertTriangle className="h-3 w-3" />
                      Limit reached
                    </span>
                  )}
                  {isNearLimit && (
                    <span className="font-medium text-amber-600">
                      Approaching quota
                    </span>
                  )}
                </div>
              </div>

              <p className="text-xs text-slate-500">{config.description}</p>
            </div>
          );
        })}
      </div>

      {/* Plan & Entitlement Details */}
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-4">
        <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
          <CreditCard className="h-5 w-5 text-slate-600" />
          <h2 className="text-base font-semibold text-slate-900">
            Subscription &amp; Custom Limits
          </h2>
        </div>

        <p className="text-sm text-slate-600 leading-relaxed">
          Your workspace is provisioned under the <strong>{usage.plan_name}</strong>. Usage quotas are enforced server-side upon creating campaigns, scheduling sends, connecting mailboxes, and importing leads.
        </p>

        <div className="rounded-md bg-slate-50 border border-slate-200 p-4 text-xs text-slate-600 space-y-2">
          <p className="font-medium text-slate-800">
            Need higher sending volume or additional mailboxes?
          </p>
          <p>
            Custom enterprise quotas and dedicated IP warming arrangements can be configured by your workspace administrator or platform operators.
          </p>
        </div>
      </section>
    </main>
  );
}
