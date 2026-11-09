"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api-client";
import { createCampaignSettings, getCampaign, listCampaignSettings } from "@/lib/campaigns-api";
import { canDraftCampaign, canExecuteCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";

const WEEKDAYS: { value: number; label: string }[] = [
  { value: 1, label: "Mon" },
  { value: 2, label: "Tue" },
  { value: 3, label: "Wed" },
  { value: 4, label: "Thu" },
  { value: 5, label: "Fri" },
  { value: 6, label: "Sat" },
  { value: 7, label: "Sun" },
];

function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return "We couldn't complete that request. Please try again.";
}

function supportedTimezones(): string[] {
  // Standard runtime source of truth -- avoids hand-maintaining an
  // incomplete/stale IANA zone list in this repo.
  try {
    if (typeof Intl.supportedValuesOf === "function") {
      return Intl.supportedValuesOf("timeZone");
    }
  } catch {
    // fall through
  }
  return [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
  ];
}

export default function CampaignSchedulePage() {
  const params = useParams<{ campaign_id: string }>();
  const campaignId = params.campaign_id;
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const { toast } = useToast();
  const timezones = useMemo(supportedTimezones, []);

  const [actionError, setActionError] = useState<string | null>(null);
  const [timezone, setTimezone] = useState("UTC");
  const [weekdays, setWeekdays] = useState<Set<number>>(new Set([1, 2, 3, 4, 5]));
  const [windowStart, setWindowStart] = useState("09:00");
  const [windowEnd, setWindowEnd] = useState("17:00");
  const [dailyLimit, setDailyLimit] = useState("50");

  const settingsKey = [
    "workspace",
    activeWorkspaceId,
    "campaigns",
    campaignId,
    "settings",
  ];

  const settingsQuery = useQuery({
    queryKey: settingsKey,
    queryFn: () =>
      activeWorkspaceId && campaignId
        ? listCampaignSettings(activeWorkspaceId, campaignId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId && campaignId),
  });

  const campaignKey = ["workspace", activeWorkspaceId, "campaigns", campaignId];
  const campaignQuery = useQuery({
    queryKey: campaignKey,
    queryFn: () => getCampaign(activeWorkspaceId as string, campaignId),
    enabled: Boolean(activeWorkspaceId && campaignId),
  });
  const status = campaignQuery.data?.status;
  // A draft is edited by anyone who may draft; a paused campaign's schedule only by
  // someone who may run campaigns (ADR-0020). Every other state is frozen.
  const mayEdit =
    status === "DRAFT"
      ? canDraftCampaign(activeWorkspace?.role_code)
      : status === "PAUSED"
        ? canExecuteCampaign(activeWorkspace?.role_code)
        : false;
  const isPaused = status === "PAUSED";

  const createMutation = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId || !campaignId) throw new Error("Not ready");
      return createCampaignSettings(activeWorkspaceId, campaignId, {
        timezone,
        weekdays: Array.from(weekdays).sort((a, b) => a - b),
        window_start_local: `${windowStart}:00`,
        window_end_local: `${windowEnd}:00`,
        daily_limit: dailyLimit.trim() ? Number(dailyLimit) : null,
      });
    },
    onSuccess: () => {
      setActionError(null);
      // A new settings version moves the campaign's pointer, so its version changes
      // too: refresh everything about this campaign, not just the schedule.
      queryClient.invalidateQueries({ queryKey: campaignKey });
      if (isPaused) toast("Schedule saved. It applies to emails that haven't been sent when you resume.");
    },
    onError: (err) => {
      setActionError(errorMessage(err));
      queryClient.invalidateQueries({ queryKey: campaignKey });
    },
  });

  const current = settingsQuery.data?.[0] ?? null;

  // Editing a paused campaign starts from what it has now, not from the defaults.
  useEffect(() => {
    if (!isPaused || !current) return;
    setTimezone(current.timezone);
    setWeekdays(new Set(current.weekdays));
    setWindowStart(current.window_start_local.slice(0, 5));
    setWindowEnd(current.window_end_local.slice(0, 5));
    setDailyLimit(current.daily_limit == null ? "" : String(current.daily_limit));
  }, [isPaused, current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="max-w-xl space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-slate-900">Schedule</h2>
        <p className="text-sm text-slate-500">
          Sending days, hours, and daily limit. Actual send timestamps are
          resolved at activation, respecting DST.
        </p>
      </div>

      {actionError && <Alert variant="error">{actionError}</Alert>}
      {isPaused && mayEdit && (
        <Alert variant="info">
          This campaign is paused. A new schedule applies to emails that haven&apos;t been sent:
          they are re-timed into the new sending days and hours when you resume.
        </Alert>
      )}
      {status && status !== "DRAFT" && !mayEdit && (
        <Alert variant="info">
          {status === "PAUSED"
            ? "Only a manager can change the schedule of a paused campaign."
            : status === "RUNNING" || status === "SCHEDULED"
              ? "The schedule can't be changed while the campaign is running. Pause it first."
              : `The schedule can't be changed because the campaign is ${status.toLowerCase()}.`}
        </Alert>
      )}

      {current && (
        <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm shadow-sm">
          <h3 className="font-semibold text-slate-900">Current schedule</h3>
          <dl className="mt-2 space-y-1 text-slate-600">
            <div>Timezone: {current.timezone}</div>
            <div>
              Days:{" "}
              {WEEKDAYS.filter((d) => current.weekdays.includes(d.value))
                .map((d) => d.label)
                .join(", ")}
            </div>
            <div>
              Window: {current.window_start_local.slice(0, 5)}&ndash;
              {current.window_end_local.slice(0, 5)}
            </div>
            <div>Daily limit: {current.daily_limit ?? "Unlimited"}</div>
          </dl>
        </div>
      )}

      {mayEdit && (
        <form
          className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-card"
          onSubmit={(e) => {
            e.preventDefault();
            if (weekdays.size === 0) {
              setActionError("Select at least one sending day.");
              return;
            }
            setActionError(null);
            createMutation.mutate();
          }}
        >
          <h3 className="text-sm font-semibold text-slate-900">
            {current ? "Update schedule" : "Configure schedule"}
          </h3>

          <Field label="Timezone" required>
            <select
              className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-10 px-3"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            >
              {timezones.map((tz) => (
                <option key={tz} value={tz}>
                  {tz}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Sending days" required>
            <div className="flex flex-wrap gap-2">
              {WEEKDAYS.map((d) => (
                <button
                  key={d.value}
                  type="button"
                  aria-pressed={weekdays.has(d.value)}
                  onClick={() => {
                    const next = new Set(weekdays);
                    if (next.has(d.value)) next.delete(d.value);
                    else next.add(d.value);
                    setWeekdays(next);
                  }}
                  className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
                    weekdays.has(d.value)
                      ? "border-brand-500 bg-brand-50 text-brand-700"
                      : "border-slate-200 text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-4">
            <Field label="Window start" required>
              <Input
                type="time"
                value={windowStart}
                onChange={(e) => setWindowStart(e.target.value)}
              />
            </Field>
            <Field label="Window end" required>
              <Input
                type="time"
                value={windowEnd}
                onChange={(e) => setWindowEnd(e.target.value)}
              />
            </Field>
          </div>
          <p className="-mt-2 text-xs text-slate-500">
            Same-day window only; overnight windows aren&apos;t supported.
          </p>

          <Field label="Daily send limit">
            <Input
              type="number"
              min={1}
              value={dailyLimit}
              onChange={(e) => setDailyLimit(e.target.value)}
            />
          </Field>

          <div className="flex justify-end">
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending && (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              )}
              Save schedule
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
