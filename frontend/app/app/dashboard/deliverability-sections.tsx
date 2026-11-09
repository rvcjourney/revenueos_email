"use client";

import Link from "next/link";
import {
  AlertOctagon,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Info,
  Mail,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import type { DeliverabilityOverview, DeliverabilityWarning } from "@/types/analytics";

function HealthStatusBadge({ status }: { status: "HEALTHY" | "WARNING" | "CRITICAL" | "DISCONNECTED" }) {
  const styles = {
    HEALTHY: "bg-emerald-50 text-emerald-700 border-emerald-200",
    WARNING: "bg-amber-50 text-amber-700 border-amber-200",
    CRITICAL: "bg-red-50 text-red-700 border-red-200",
    DISCONNECTED: "bg-slate-100 text-slate-700 border-slate-300",
  };

  const icons = {
    HEALTHY: CheckCircle2,
    WARNING: AlertTriangle,
    CRITICAL: AlertOctagon,
    DISCONNECTED: XCircle,
  };

  const Icon = icons[status] || Info;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${styles[status]}`}
    >
      <Icon className="h-3.5 w-3.5" />
      {status === "HEALTHY"
        ? "Good"
        : status === "WARNING"
          ? "Attention Needed"
          : status === "CRITICAL"
            ? "Critical"
            : "Disconnected"}
    </span>
  );
}

function WarningCard({ warning }: { warning: DeliverabilityWarning }) {
  const isCritical = warning.level === "CRITICAL";

  return (
    <div
      className={`rounded-xl border p-4 shadow-sm ${
        isCritical
          ? "border-red-200 bg-red-50/60 text-red-900"
          : "border-amber-200 bg-amber-50/60 text-amber-900"
      }`}
    >
      <div className="flex items-start gap-3">
        {isCritical ? (
          <ShieldAlert className="mt-0.5 h-5 w-5 text-red-600" />
        ) : (
          <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-600" />
        )}
        <div className="flex-1 space-y-1">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold">{warning.title}</h3>
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
                isCritical ? "bg-red-200 text-red-800" : "bg-amber-200 text-amber-800"
              }`}
            >
              {warning.level}
            </span>
          </div>
          <p className="text-xs leading-relaxed">{warning.message}</p>
          {warning.code === "MAILBOX_SAFETY_HOLD" && warning.mailbox_id ? (
            <Link
              href={`/app/mailboxes/${warning.mailbox_id}`}
              className="inline-flex items-center gap-1 text-xs font-semibold underline underline-offset-2"
            >
              See why and release <ArrowRight className="h-3 w-3" />
            </Link>
          ) : null}
          {warning.threshold !== undefined && warning.metric_value !== undefined && (
            <p className="text-[11px] font-medium opacity-80">
              Observed: {warning.metric_value}% | Redline Threshold: {warning.threshold}%
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// Overall health status, headline rates and active alerts.
export function DeliverabilityHealth({ data }: { data: DeliverabilityOverview }) {
  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Workspace Deliverability Status
              </span>
              <HealthStatusBadge
                status={
                  data.overall_health === "HEALTHY"
                    ? "HEALTHY"
                    : data.overall_health === "NEEDS_ATTENTION"
                      ? "WARNING"
                      : "CRITICAL"
                }
              />
            </div>
            <p className="text-sm text-slate-600">
              {data.overall_health === "HEALTHY"
                ? "All connected mailboxes are within safety thresholds. Sender reputation is healthy."
                : data.overall_health === "NEEDS_ATTENTION"
                  ? "Elevated bounce or complaint indicators detected. Review active warnings below."
                  : "Critical deliverability risks detected. Mailbox pauses or safety holds in effect."}
            </p>
          </div>

          {/* Core benchmarks */}
          <div className="flex flex-wrap items-center gap-6 border-t border-slate-100 pt-4 lg:border-t-0 lg:pt-0">
            <div className="space-y-0.5">
              <p className="text-[11px] font-medium text-slate-400">Bounce Rate</p>
              <p
                className={`text-lg font-bold ${
                  data.bounce_rate > 5.0
                    ? "text-red-600"
                    : data.bounce_rate > 2.0
                      ? "text-amber-600"
                      : "text-slate-900"
                }`}
              >
                {data.bounce_rate}%
              </p>
              <p className="text-[10px] text-slate-400">Target: &lt; 2.0%</p>
            </div>

            <div className="space-y-0.5">
              <p className="text-[11px] font-medium text-slate-400">Complaint Rate</p>
              <p
                className={`text-lg font-bold ${
                  data.complaint_rate > 0.1 ? "text-red-600" : "text-slate-900"
                }`}
              >
                {data.complaint_rate}%
              </p>
              <p className="text-[10px] text-slate-400">Google/Yahoo: &lt; 0.1%</p>
            </div>

            <div className="space-y-0.5">
              <p className="text-[11px] font-medium text-slate-400">Send Failure Rate</p>
              <p className="text-lg font-bold text-slate-900">{data.failure_rate}%</p>
              <p className="text-[10px] text-slate-400">Total Sent: {data.total_sent}</p>
            </div>

            <div className="space-y-0.5">
              <p className="text-[11px] font-medium text-slate-400">Active Safety Holds</p>
              <p
                className={`text-lg font-bold ${
                  data.active_safety_holds > 0 ? "text-red-600" : "text-emerald-600"
                }`}
              >
                {data.active_safety_holds}
              </p>
              <p className="text-[10px] text-slate-400">Outreach gates</p>
            </div>
          </div>
        </div>
      </div>

      {data.warnings.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold tracking-wide text-slate-900">
            Active Deliverability Alerts ({data.warnings.length})
          </h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {data.warnings.map((w, idx) => (
              <WarningCard key={`${w.code}-${idx}`} warning={w} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Per-mailbox health table and categorized send-failure telemetry.
export function DeliverabilityDetails({ data }: { data: DeliverabilityOverview }) {
  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card">
        <div className="mb-4 flex items-center justify-between border-b border-slate-100 pb-4">
          <div>
            <h2 className="text-base font-semibold text-slate-900">Sender Mailbox Health</h2>
            <p className="text-xs text-slate-500">
              Operational deliverability metrics and connection health per mailbox.
            </p>
          </div>
          <Link
            href="/app/mailboxes"
            className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-800"
          >
            Manage mailboxes <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        </div>

        {data.mailboxes.length === 0 ? (
          <div className="py-8 text-center text-xs text-slate-500">
            No mailboxes connected to this workspace yet.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-100 text-slate-400">
                  <th className="pb-3 font-medium">Mailbox</th>
                  <th className="pb-3 font-medium">Provider</th>
                  <th className="pb-3 font-medium">Status</th>
                  <th className="pb-3 text-right font-medium">Sent</th>
                  <th className="pb-3 text-right font-medium">Bounce Rate</th>
                  <th className="pb-3 text-right font-medium">Complaint Rate</th>
                  <th className="pb-3 text-right font-medium">Failures</th>
                  <th className="pb-3 text-center font-medium">Safety Holds</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.mailboxes.map((mb) => (
                  <tr key={mb.mailbox_id} className="hover:bg-slate-50/50">
                    <td className="py-3 font-medium text-slate-900">
                      <div className="flex items-center gap-2">
                        <Mail className="h-3.5 w-3.5 text-slate-400" />
                        <span>{mb.email_address}</span>
                      </div>
                    </td>
                    <td className="py-3 text-slate-600">
                      <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                        {mb.provider}
                      </span>
                    </td>
                    <td className="py-3">
                      <HealthStatusBadge status={mb.health_status} />
                    </td>
                    <td className="py-3 text-right text-slate-700">
                      {mb.sent_count.toLocaleString()}
                    </td>
                    <td className="py-3 text-right font-medium">
                      <span
                        className={
                          mb.bounce_rate > 5.0
                            ? "text-red-600 font-bold"
                            : mb.bounce_rate > 2.0
                              ? "text-amber-600"
                              : "text-slate-700"
                        }
                      >
                        {mb.bounce_rate}%
                      </span>
                    </td>
                    <td className="py-3 text-right font-medium">
                      <span
                        className={
                          mb.complaint_rate > 0.1
                            ? "text-red-600 font-bold"
                            : "text-slate-700"
                        }
                      >
                        {mb.complaint_rate}%
                      </span>
                    </td>
                    <td className="py-3 text-right text-slate-700">{mb.failure_count}</td>
                    <td className="py-3 text-center">
                      {mb.active_safety_holds_count > 0 ? (
                        <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">
                          {mb.active_safety_holds_count} hold(s)
                        </span>
                      ) : (
                        <span className="text-slate-400">None</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card">
        <div className="mb-4 border-b border-slate-100 pb-3">
          <h2 className="text-base font-semibold text-slate-900">
            Operational Send Failure Breakdown
          </h2>
          <p className="text-xs text-slate-500">
            Categorized provider errors and rejection telemetry recorded by sending workers.
          </p>
        </div>

        {data.failure_breakdown.length === 0 ? (
          <div className="py-6 text-center text-xs text-slate-500">
            No send rejections or provider errors recorded in this period.
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {data.failure_breakdown.map((f) => (
              <div
                key={f.category}
                className="rounded-lg border border-slate-100 bg-slate-50/50 p-4"
              >
                <div className="flex items-center justify-between">
                  <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-800">
                    {f.category}
                  </span>
                  <span className="text-base font-bold text-slate-900">{f.count}</span>
                </div>
                <p className="mt-2 text-xs text-slate-500">{f.description}</p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
