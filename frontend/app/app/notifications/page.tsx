"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Bell,
  Check,
  CheckCheck,
  Info,
  Loader2,
  Mail,
  ShieldAlert,
  Sparkles,
  Users,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "@/lib/notifications-api";
import { useWorkspace } from "@/lib/workspace-context";
import type { NotificationItem } from "@/types/domain";

function getCategoryIcon(category: string) {
  switch (category) {
    case "safety":
    case "compliance":
      return <ShieldAlert className="h-5 w-5 text-amber-500" />;
    case "campaign":
      return <Mail className="h-5 w-5 text-brand-600" />;
    case "team":
      return <Users className="h-5 w-5 text-brand-500" />;
    case "system":
      return <AlertCircle className="h-5 w-5 text-slate-600" />;
    default:
      return <Info className="h-5 w-5 text-brand-600" />;
  }
}

export default function NotificationsPage() {
  const { activeWorkspaceId } = useWorkspace();
  const queryClient = useQueryClient();
  const [filterUnread, setFilterUnread] = useState(false);

  const {
    data: notifications,
    isLoading,
    isError,
  } = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "notifications",
      { unread_only: filterUnread },
    ],
    queryFn: () =>
      listNotifications(activeWorkspaceId!, { unread_only: filterUnread }),
    enabled: Boolean(activeWorkspaceId),
  });

  const markReadMutation = useMutation({
    mutationFn: (notificationId: string) =>
      markNotificationRead(activeWorkspaceId!, notificationId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "notifications"],
      });
    },
  });

  const markAllReadMutation = useMutation({
    mutationFn: () => markAllNotificationsRead(activeWorkspaceId!),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "notifications"],
      });
    },
  });

  const unreadCount = notifications?.filter((n) => !n.is_read).length ?? 0;

  return (
    <main className="space-y-6 max-w-4xl pb-16">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-brand-600">Workspace Updates</p>
          <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-1">
            Notifications
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Platform announcements, campaign delivery alerts, and security warnings.
          </p>
        </div>

        {notifications && notifications.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            disabled={markAllReadMutation.isPending || unreadCount === 0}
            onClick={() => markAllReadMutation.mutate()}
            className="gap-2"
          >
            <CheckCheck className="h-4 w-4" />
            Mark all as read
          </Button>
        )}
      </div>

      {/* Filter Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-200 pb-3">
        <button
          type="button"
          onClick={() => setFilterUnread(false)}
          className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            !filterUnread
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          }`}
        >
          All
        </button>
        <button
          type="button"
          onClick={() => setFilterUnread(true)}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            filterUnread
              ? "bg-slate-900 text-white"
              : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          }`}
        >
          Unread
          {unreadCount > 0 && (
            <span
              className={`rounded-full px-2 py-0.2 text-xs font-semibold ${
                filterUnread
                  ? "bg-brand-500 text-white"
                  : "bg-brand-100 text-brand-800"
              }`}
            >
              {unreadCount}
            </span>
          )}
        </button>
      </div>

      {/* Notifications List */}
      <section className="space-y-3">
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 p-12 text-sm text-slate-500 rounded-lg border border-slate-200 bg-white">
            <Loader2 className="h-5 w-5 animate-spin text-brand-600" />
            Loading notifications&hellip;
          </div>
        ) : isError ? (
          <div className="p-8 text-center text-sm text-red-600 rounded-lg border border-red-200 bg-red-50">
            Failed to load notifications. Please try again.
          </div>
        ) : !notifications || notifications.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-12 text-center text-sm text-slate-500 rounded-xl border border-slate-200 bg-white shadow-card">
            <Bell className="h-8 w-8 text-slate-300" aria-hidden="true" />
            <p className="font-medium text-slate-700">No notifications</p>
            <p className="text-xs text-slate-500">
              {filterUnread
                ? "You've read all your notifications!"
                : "You have no incoming notifications yet."}
            </p>
          </div>
        ) : (
          notifications.map((item: NotificationItem) => (
            <div
              key={item.id}
              className={`flex items-start gap-4 rounded-lg border p-4 transition-all shadow-sm ${
                item.is_read
                  ? "border-slate-200 bg-white"
                  : "border-brand-200 bg-brand-50/30"
              }`}
            >
              <div className="mt-0.5 flex-shrink-0">
                {getCategoryIcon(item.category)}
              </div>

              <div className="flex-1 min-w-0 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold text-slate-900 truncate">
                      {item.title}
                    </h3>
                    {!item.is_read && (
                      <span className="h-2 w-2 rounded-full bg-brand-600" />
                    )}
                  </div>
                  <span className="text-xs text-slate-400 whitespace-nowrap">
                    {new Date(item.created_at).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>

                <p className="text-sm text-slate-600 leading-relaxed">
                  {item.body}
                </p>

                {item.data && Object.keys(item.data).length > 0 && (
                  <div className="pt-1">
                    {Boolean(item.data.action_url) && (
                      <a
                        href={String(item.data.action_url)}
                        className="text-xs font-medium text-brand-700 hover:underline"
                      >
                        View details &rarr;
                      </a>
                    )}
                  </div>
                )}
              </div>

              {!item.is_read && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={markReadMutation.isPending}
                  onClick={() => markReadMutation.mutate(item.id)}
                  className="h-8 text-xs text-slate-500 hover:text-brand-700 gap-1"
                  title="Mark as read"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">Read</span>
                </Button>
              )}
            </div>
          ))
        )}
      </section>
    </main>
  );
}
