"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Check, Loader2, Mail, Shield, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import {
  getNotificationPreferences,
  updateNotificationPreferences,
} from "@/lib/notifications-api";
import type { NotificationPreferences } from "@/types/domain";

export default function NotificationSettingsPage() {
  const queryClient = useQueryClient();

  const [emailEnabled, setEmailEnabled] = useState(true);
  const [inAppEnabled, setInAppEnabled] = useState(true);
  const [categories, setCategories] = useState<Record<string, boolean>>({
    safety: true,
    campaign: true,
    team: true,
  });

  const [savedSuccess, setSavedSuccess] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const { data: prefs, isLoading, isError } = useQuery({
    queryKey: ["user", "notifications", "preferences"],
    queryFn: getNotificationPreferences,
  });

  useEffect(() => {
    if (prefs) {
      setEmailEnabled(prefs.email_notifications_enabled);
      setInAppEnabled(prefs.in_app_notifications_enabled);
      setCategories({
        safety: prefs.category_preferences?.safety ?? true,
        campaign: prefs.category_preferences?.campaign ?? true,
        team: prefs.category_preferences?.team ?? true,
      });
    }
  }, [prefs]);

  const saveMutation = useMutation({
    mutationFn: (payload: NotificationPreferences) =>
      updateNotificationPreferences(payload),
    onSuccess: (data) => {
      setSavedSuccess(true);
      setErrorMessage(null);
      queryClient.setQueryData(["user", "notifications", "preferences"], data);
      setTimeout(() => setSavedSuccess(false), 3000);
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError ? err.message : "Failed to update preferences.",
      );
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    saveMutation.mutate({
      email_notifications_enabled: emailEnabled,
      in_app_notifications_enabled: inAppEnabled,
      category_preferences: categories,
    });
  }

  function toggleCategory(cat: string) {
    setCategories((prev) => ({
      ...prev,
      [cat]: !prev[cat],
    }));
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 p-16 text-sm text-slate-500">
        <Loader2 className="h-5 w-5 animate-spin text-brand-600" />
        Loading notification settings&hellip;
      </div>
    );
  }

  if (isError) {
    return (
      <div className="max-w-2xl">
        <Alert variant="error">
          Failed to load notification preferences. Please try again.
        </Alert>
      </div>
    );
  }

  return (
    <main className="space-y-6 max-w-2xl pb-16">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.08em] text-brand-600">Account Preferences</p>
        <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-1">
          Notification Preferences
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          Configure how and when you receive activity alerts and security notices.
        </p>
      </div>

      {savedSuccess && (
        <Alert variant="info" className="flex items-center gap-2">
          <Check className="h-4 w-4 text-brand-700" />
          <span>Your notification preferences have been saved successfully.</span>
        </Alert>
      )}

      {errorMessage && (
        <Alert variant="error">
          <span>{errorMessage}</span>
        </Alert>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Delivery Channels */}
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-5">
          <h2 className="text-base font-semibold text-slate-900 border-b border-slate-100 pb-3">
            Delivery Channels
          </h2>

          <div className="flex items-start justify-between gap-4">
            <div className="space-y-0.5">
              <label
                htmlFor="in-app-toggle"
                className="text-sm font-medium text-slate-900 cursor-pointer flex items-center gap-2"
              >
                <Bell className="h-4 w-4 text-slate-500" />
                In-App Notifications
              </label>
              <p className="text-xs text-slate-500">
                Display notification badges and live alerts directly inside the application header.
              </p>
            </div>
            <input
              id="in-app-toggle"
              type="checkbox"
              checked={inAppEnabled}
              onChange={(e) => setInAppEnabled(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 cursor-pointer"
            />
          </div>

          <div className="flex items-start justify-between gap-4 pt-3 border-t border-slate-100">
            <div className="space-y-0.5">
              <label
                htmlFor="email-toggle"
                className="text-sm font-medium text-slate-900 cursor-pointer flex items-center gap-2"
              >
                <Mail className="h-4 w-4 text-slate-500" />
                Email Notifications
              </label>
              <p className="text-xs text-slate-500">
                Receive transactional digest and alert emails for high-priority workspace events.
              </p>
            </div>
            <input
              id="email-toggle"
              type="checkbox"
              checked={emailEnabled}
              onChange={(e) => setEmailEnabled(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 cursor-pointer"
            />
          </div>
        </section>

        {/* Categories */}
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-5">
          <h2 className="text-base font-semibold text-slate-900 border-b border-slate-100 pb-3">
            Notification Categories
          </h2>

          <div className="space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-0.5">
                <p className="text-sm font-medium text-slate-900 flex items-center gap-2">
                  <Shield className="h-4 w-4 text-amber-600" />
                  Safety &amp; Compliance Alerts
                </p>
                <p className="text-xs text-slate-500">
                  Critical warnings when bounce or complaint thresholds trigger sending restrictions.
                </p>
              </div>
              <input
                type="checkbox"
                checked={categories.safety ?? true}
                onChange={() => toggleCategory("safety")}
                className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 cursor-pointer"
              />
            </div>

            <div className="flex items-start justify-between gap-4 pt-3 border-t border-slate-100">
              <div className="space-y-0.5">
                <p className="text-sm font-medium text-slate-900 flex items-center gap-2">
                  <Mail className="h-4 w-4 text-brand-600" />
                  Campaign Lifecycle
                </p>
                <p className="text-xs text-slate-500">
                  Updates when campaigns complete audience sending, pause, or encounter mailbox rate limits.
                </p>
              </div>
              <input
                type="checkbox"
                checked={categories.campaign ?? true}
                onChange={() => toggleCategory("campaign")}
                className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 cursor-pointer"
              />
            </div>

            <div className="flex items-start justify-between gap-4 pt-3 border-t border-slate-100">
              <div className="space-y-0.5">
                <p className="text-sm font-medium text-slate-900 flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-brand-600" />
                  Team &amp; Workspace Updates
                </p>
                <p className="text-xs text-slate-500">
                  Notifications when teammates accept invitations or workspace ownership transfers.
                </p>
              </div>
              <input
                type="checkbox"
                checked={categories.team ?? true}
                onChange={() => toggleCategory("team")}
                className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 cursor-pointer"
              />
            </div>
          </div>
        </section>

        <div className="flex justify-end">
          <Button
            type="submit"
            variant="primary"
            disabled={saveMutation.isPending}
            className="gap-2"
          >
            {saveMutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Saving Preferences&hellip;
              </>
            ) : (
              "Save Preferences"
            )}
          </Button>
        </div>
      </form>
    </main>
  );
}
