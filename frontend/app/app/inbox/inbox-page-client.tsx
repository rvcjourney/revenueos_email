"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  CheckCheck,
  CornerDownLeft,
  Inbox,
  Loader2,
  Mail,
  MailOpen,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Tag,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api-client";
import {
  archiveConversation,
  getConversation,
  getInboxSyncStatus,
  listConversations,
  markConversationRead,
  markConversationUnread,
  unarchiveConversation,
} from "@/lib/inbox-api";
import { canManageInbox } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type {
  InboxFilter,
  MessageThreadItem,
} from "@/types/domain";

const FILTERS: { id: InboxFilter; label: string }[] = [
  { id: "ALL", label: "All" },
  { id: "UNREAD", label: "Unread" },
  { id: "REPLIED", label: "Replied" },
  { id: "ARCHIVED", label: "Archived" },
];

function useDebouncedValue(value: string, delayMs: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(handle);
  }, [value, delayMs]);
  return debounced;
}

function formatRelativeTime(dateString: string): string {
  try {
    const date = new Date(dateString);
    const now = new Date();
    const diffSec = Math.floor((now.getTime() - date.getTime()) / 1000);

    if (diffSec < 60) return "Just now";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;

    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
    }).format(date);
  } catch {
    return "";
  }
}

function formatFullTime(dateString: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(dateString));
  } catch {
    return dateString;
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "An unexpected error occurred. Please try again.";
}

function SafeHtmlViewer({ html }: { html: string }) {
  return (
    <div
      className="prose prose-sm max-w-none text-slate-800 break-words font-sans"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

export function InboxPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();

  const mayManageInbox = canManageInbox(activeWorkspace?.role_code);

  const filter = (searchParams.get("filter") as InboxFilter | null) ?? "ALL";
  const searchParam = searchParams.get("q") ?? "";
  const selectedMailboxId = searchParams.get("mailbox_id") ?? "";
  const selectedConversationId = searchParams.get("cid");

  const [searchText, setSearchText] = useState(searchParam);
  const debouncedSearch = useDebouncedValue(searchText, 300);

  // Sync debounced search to query params
  useEffect(() => {
    const next = new URLSearchParams(searchParams.toString());
    if (debouncedSearch) next.set("q", debouncedSearch);
    else next.delete("q");
    next.delete("cursor");
    const href = `${pathname}?${next.toString()}`;
    if ((searchParams.get("q") ?? "") !== debouncedSearch) {
      router.replace(href.endsWith("?") ? pathname : href);
    }
  }, [debouncedSearch, searchParams, pathname, router]);

  // Load conversations
  const conversationsQuery = useQuery({
    queryKey: [
      "workspace",
      activeWorkspaceId,
      "inbox",
      "conversations",
      { filter, q: searchParams.get("q"), mailbox_id: selectedMailboxId },
    ],
    queryFn: () =>
      activeWorkspaceId
        ? listConversations(activeWorkspaceId, {
            filter,
            q: searchParams.get("q"),
            mailboxId: selectedMailboxId || null,
            limit: 50,
          })
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId),
    refetchInterval: 30000,
  });

  // Load selected conversation
  const selectedDetailQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "inbox", "conversation", selectedConversationId],
    queryFn: () =>
      activeWorkspaceId && selectedConversationId
        ? getConversation(activeWorkspaceId, selectedConversationId)
        : Promise.reject(new Error("Missing conversation ID")),
    enabled: Boolean(activeWorkspaceId && selectedConversationId),
  });

  // Load mailbox sync status
  const syncStatusQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "inbox", "sync-status"],
    queryFn: () =>
      activeWorkspaceId
        ? getInboxSyncStatus(activeWorkspaceId)
        : Promise.reject(new Error("No active workspace")),
    enabled: Boolean(activeWorkspaceId),
    refetchInterval: 60000,
  });

  // Action Mutations
  const markReadMutation = useMutation({
    mutationFn: (cid: string) => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return markConversationRead(activeWorkspaceId, cid);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "inbox"],
      });
    },
  });

  const markUnreadMutation = useMutation({
    mutationFn: (cid: string) => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return markConversationUnread(activeWorkspaceId, cid);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "inbox"],
      });
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (cid: string) => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return archiveConversation(activeWorkspaceId, cid);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "inbox"],
      });
    },
  });

  const unarchiveMutation = useMutation({
    mutationFn: (cid: string) => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return unarchiveConversation(activeWorkspaceId, cid);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "inbox"],
      });
    },
  });

  const handleSelectConversation = useCallback(
    (cid: string) => {
      const next = new URLSearchParams(searchParams.toString());
      next.set("cid", cid);
      router.push(`${pathname}?${next.toString()}`);
    },
    [pathname, router, searchParams]
  );

  const handleBackToList = () => {
    const next = new URLSearchParams(searchParams.toString());
    next.delete("cid");
    router.push(`${pathname}?${next.toString()}`);
  };

  const handleFilterChange = (newFilter: InboxFilter) => {
    const next = new URLSearchParams(searchParams.toString());
    if (newFilter === "ALL") next.delete("filter");
    else next.set("filter", newFilter);
    next.delete("cursor");
    router.push(`${pathname}?${next.toString()}`);
  };

  const handleMailboxChange = (mbId: string) => {
    const next = new URLSearchParams(searchParams.toString());
    if (mbId) next.set("mailbox_id", mbId);
    else next.delete("mailbox_id");
    next.delete("cursor");
    router.push(`${pathname}?${next.toString()}`);
  };

  const conversationList = useMemo(
    () => conversationsQuery.data?.items ?? [],
    [conversationsQuery.data?.items]
  );
  const unreadCount = conversationsQuery.data?.unread_count ?? 0;
  const currentDetail = selectedDetailQuery.data;

  // Auto-select first conversation on wide screens if none selected and data exists
  useEffect(() => {
    if (
      !selectedConversationId &&
      conversationList.length > 0 &&
      typeof window !== "undefined" &&
      window.innerWidth >= 1024
    ) {
      handleSelectConversation(conversationList[0].id);
    }
  }, [selectedConversationId, conversationList, handleSelectConversation]);

  return (
    <div className="flex h-[calc(100vh-7rem)] min-h-[34rem] flex-col overflow-hidden rounded-xl border border-slate-200 bg-slate-50 shadow-card">
      {/* Top Header / Status bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-3 shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
            <Inbox className="h-5 w-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold tracking-tight text-slate-900">Unified Inbox</h1>
              {unreadCount > 0 && (
                <span className="inline-flex items-center rounded-full bg-brand-100 px-2 py-0.5 text-xs font-semibold text-brand-700">
                  {unreadCount} unread
                </span>
              )}
            </div>
            <p className="text-xs text-slate-500">
              Operational outreach inbox across connected mailboxes
            </p>
          </div>
        </div>

        {/* Mailbox Sync Health Badges */}
        <div className="flex items-center gap-2">
          {syncStatusQuery.data?.mailboxes && syncStatusQuery.data.mailboxes.length > 0 ? (
            <div className="flex items-center gap-1.5 overflow-x-auto text-xs text-slate-600">
              {syncStatusQuery.data.mailboxes.map((mb) => (
                <span
                  key={mb.mailbox_id}
                  title={`${mb.email_address} (${mb.provider}): ${mb.sync_status}`}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-2 py-1"
                >
                  <span
                    className={`h-2 w-2 rounded-full ${
                      mb.sync_status === "HEALTHY" || mb.sync_status === "CURRENT"
                        ? "bg-emerald-500"
                        : mb.sync_status === "NOT_SUPPORTED"
                        ? "bg-slate-400"
                        : "bg-amber-500"
                    }`}
                  />
                  <span className="font-medium text-slate-700 truncate max-w-[120px]">
                    {mb.email_address}
                  </span>
                </span>
              ))}
            </div>
          ) : null}

          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              conversationsQuery.refetch();
              syncStatusQuery.refetch();
            }}
            disabled={conversationsQuery.isFetching}
            className="text-slate-500 hover:text-slate-700"
            title="Refresh inbox"
          >
            <RefreshCw
              className={`h-4 w-4 ${conversationsQuery.isFetching ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
      </div>

      {/* Main 2-Pane Content Area */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Pane: Conversation List */}
        <div
          className={`flex flex-col border-r border-slate-200 bg-white w-full lg:w-[420px] shrink-0 ${
            selectedConversationId ? "hidden lg:flex" : "flex"
          }`}
        >
          {/* Controls: Search, Mailbox filter, Tab filters */}
          <div className="p-3 border-b border-slate-200 space-y-2.5">
            {/* Search Bar */}
            <div className="relative">
              <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <Input
                type="search"
                placeholder="Search subject, participant, campaign..."
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                className="pl-9 text-xs h-9 bg-slate-50 border-slate-200 focus:bg-white"
              />
            </div>

            {/* Filter Tabs */}
            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-slate-100">
              {FILTERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => handleFilterChange(f.id)}
                  className={`flex-1 py-1 text-xs font-medium rounded-md transition-colors ${
                    filter === f.id
                      ? "bg-white text-slate-900 shadow-xs"
                      : "text-slate-600 hover:text-slate-900"
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>

            {/* Mailbox Filter Dropdown (if multiple mailboxes exist) */}
            {syncStatusQuery.data?.mailboxes && syncStatusQuery.data.mailboxes.length > 1 && (
              <select
                value={selectedMailboxId}
                onChange={(e) => handleMailboxChange(e.target.value)}
                aria-label="Filter by mailbox"
                className="w-full text-xs h-8 rounded-md border border-slate-200 bg-slate-50 px-2 text-slate-700 focus:outline-hidden focus:ring-1 focus:ring-brand-500"
              >
                <option value="">All Mailboxes</option>
                {syncStatusQuery.data.mailboxes.map((mb) => (
                  <option key={mb.mailbox_id} value={mb.mailbox_id}>
                    {mb.email_address} ({mb.provider})
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* List Scroll Area */}
          <div className="flex-1 overflow-y-auto divide-y divide-slate-100">
            {conversationsQuery.isLoading ? (
              <div className="p-8 text-center text-slate-500">
                <Loader2 className="h-6 w-6 animate-spin mx-auto text-slate-400 mb-2" />
                <p className="text-xs">Loading conversations...</p>
              </div>
            ) : conversationsQuery.isError ? (
              <div className="p-4">
                <Alert variant="error">
                  <p className="text-xs font-medium">Failed to load conversations</p>
                  <p className="text-xs mt-1 text-slate-600">
                    {errorMessage(conversationsQuery.error)}
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="mt-3 text-xs"
                    onClick={() => conversationsQuery.refetch()}
                  >
                    Retry
                  </Button>
                </Alert>
              </div>
            ) : conversationList.length === 0 ? (
              <div className="p-8 text-center text-slate-500">
                <MailOpen className="h-8 w-8 mx-auto text-slate-300 mb-2" />
                <p className="text-sm font-medium text-slate-700">No conversations</p>
                <p className="text-xs text-slate-500 mt-1">
                  {filter === "UNREAD"
                    ? "You're all caught up! No unread replies."
                    : filter === "ARCHIVED"
                    ? "No archived conversations."
                    : searchParam
                    ? "No conversations match your search query."
                    : "Outreach replies and synchronized threads will appear here."}
                </p>
              </div>
            ) : (
              conversationList.map((item) => {
                const isSelected = item.id === selectedConversationId;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => handleSelectConversation(item.id)}
                    className={`w-full text-left p-3.5 transition-colors relative flex items-start gap-3 hover:bg-slate-50 ${
                      isSelected
                        ? "bg-brand-50/70 border-l-4 border-brand-600"
                        : item.is_read
                        ? "bg-white"
                        : "bg-blue-50/40"
                    }`}
                  >
                    {/* Unread dot */}
                    <div className="mt-1 shrink-0">
                      {!item.is_read ? (
                        <span className="block h-2.5 w-2.5 rounded-full bg-brand-600 ring-2 ring-white" />
                      ) : (
                        <span className="block h-2.5 w-2.5 rounded-full bg-transparent" />
                      )}
                    </div>

                    {/* Conversation preview content */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-1 mb-0.5">
                        <span
                          className={`text-xs truncate ${
                            !item.is_read ? "font-semibold text-slate-900" : "font-medium text-slate-700"
                          }`}
                        >
                          {item.participant_name || item.participant_email || "Unknown"}
                        </span>
                        <span className="text-[11px] text-slate-400 shrink-0">
                          {formatRelativeTime(item.latest_activity_at)}
                        </span>
                      </div>

                      <div
                        className={`text-xs truncate mb-1 ${
                          !item.is_read ? "font-medium text-slate-900" : "text-slate-600"
                        }`}
                      >
                        {item.subject}
                      </div>

                      {item.snippet && (
                        <p className="text-xs text-slate-500 line-clamp-1 break-words">
                          {item.snippet}
                        </p>
                      )}

                      {/* Badges row */}
                      <div className="flex items-center gap-1.5 mt-2 flex-wrap text-[11px]">
                        {item.campaign_name && (
                          <span className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
                            <Tag className="h-3 w-3" />
                            <span className="truncate max-w-[100px]">{item.campaign_name}</span>
                          </span>
                        )}

                        {item.reply_status === "MATCHED" && (
                          <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700 font-medium">
                            <CornerDownLeft className="h-3 w-3" />
                            Replied
                          </span>
                        )}

                        <span className="inline-flex items-center rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">
                          {item.mailbox_provider}
                        </span>

                        {item.message_count > 1 && (
                          <span className="inline-flex items-center rounded-full bg-slate-200 px-1.5 py-0.2 text-[10px] font-semibold text-slate-700">
                            {item.message_count}
                          </span>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Right Pane: Conversation Detail */}
        <div
          className={`flex-1 flex-col bg-slate-50 overflow-hidden ${
            selectedConversationId ? "flex" : "hidden lg:flex"
          }`}
        >
          {!selectedConversationId ? (
            <div className="m-auto text-center p-8 text-slate-400">
              <Inbox className="h-12 w-12 mx-auto mb-3 text-slate-300" />
              <h3 className="text-sm font-semibold text-slate-700">No conversation selected</h3>
              <p className="text-xs text-slate-500 mt-1 max-w-sm">
                Choose a conversation from the list to view the full outreach history and replies.
              </p>
            </div>
          ) : selectedDetailQuery.isLoading ? (
            <div className="m-auto text-center p-8 text-slate-500">
              <Loader2 className="h-6 w-6 animate-spin mx-auto text-slate-400 mb-2" />
              <p className="text-xs">Loading thread...</p>
            </div>
          ) : selectedDetailQuery.isError ? (
            <div className="p-6 max-w-lg mx-auto">
              <Alert variant="error">
                <p className="text-xs font-medium">Failed to load conversation</p>
                <p className="text-xs mt-1 text-slate-600">
                  {errorMessage(selectedDetailQuery.error)}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3 text-xs"
                  onClick={() => selectedDetailQuery.refetch()}
                >
                  Retry
                </Button>
              </Alert>
            </div>
          ) : currentDetail ? (
            <>
              {/* Detail Header & Action Toolbar */}
              <div className="bg-white border-b border-slate-200 px-6 py-4 shrink-0 flex flex-wrap items-center justify-between gap-4">
                <div className="flex items-center gap-3 min-w-0">
                  <button
                    type="button"
                    onClick={handleBackToList}
                    className="lg:hidden p-1.5 -ml-1 text-slate-500 hover:text-slate-800 rounded-md"
                    title="Back to list"
                  >
                    <ArrowLeft className="h-5 w-5" />
                  </button>

                  <div className="min-w-0">
                    <h2 className="text-base font-semibold text-slate-900 truncate">
                      {currentDetail.subject}
                    </h2>
                    <div className="flex items-center gap-2 mt-1 text-xs text-slate-500 flex-wrap">
                      <span className="font-medium text-slate-700">
                        {currentDetail.participant_name
                          ? `${currentDetail.participant_name} <${currentDetail.participant_email}>`
                          : currentDetail.participant_email}
                      </span>
                      <span>•</span>
                      <span>Mailbox: {currentDetail.mailbox_address}</span>
                      {currentDetail.campaign_name && (
                        <>
                          <span>•</span>
                          <span className="inline-flex items-center gap-1 font-medium text-brand-600">
                            <Tag className="h-3 w-3" />
                            {currentDetail.campaign_name}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                </div>

                {/* Conversation Actions */}
                <div className="flex items-center gap-2 shrink-0">
                  {currentDetail.is_read ? (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => markUnreadMutation.mutate(currentDetail.id)}
                      disabled={markUnreadMutation.isPending}
                      className="text-xs"
                    >
                      <Mail className="h-3.5 w-3.5 mr-1 text-slate-500" />
                      Mark Unread
                    </Button>
                  ) : (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => markReadMutation.mutate(currentDetail.id)}
                      disabled={markReadMutation.isPending}
                      className="text-xs"
                    >
                      <CheckCheck className="h-3.5 w-3.5 mr-1 text-brand-600" />
                      Mark Read
                    </Button>
                  )}

                  {mayManageInbox && (
                    currentDetail.archived_at ? (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => unarchiveMutation.mutate(currentDetail.id)}
                        disabled={unarchiveMutation.isPending}
                        className="text-xs"
                      >
                        <ArchiveRestore className="h-3.5 w-3.5 mr-1 text-slate-500" />
                        Unarchive
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => archiveMutation.mutate(currentDetail.id)}
                        disabled={archiveMutation.isPending}
                        className="text-xs"
                      >
                        <Archive className="h-3.5 w-3.5 mr-1 text-slate-500" />
                        Archive
                      </Button>
                    )
                  )}
                </div>
              </div>

              {/* Message Thread Scroll Area */}
              <div className="flex-1 overflow-y-auto p-6 space-y-4">
                {currentDetail.messages.length === 0 ? (
                  <div className="text-center py-12 text-slate-500 text-xs">
                    No message history recorded for this conversation.
                  </div>
                ) : (
                  currentDetail.messages.map((msg: MessageThreadItem) => {
                    const isInbound = msg.direction === "INBOUND";
                    return (
                      <div
                        key={msg.id}
                        className={`rounded-lg border bg-white p-5 shadow-xs transition-shadow ${
                          isInbound
                            ? "border-emerald-200 bg-emerald-50/10"
                            : "border-slate-200"
                        }`}
                      >
                        {/* Message Header */}
                        <div className="flex items-start justify-between gap-4 mb-3 border-b border-slate-100 pb-3">
                          <div className="flex items-center gap-2 min-w-0">
                            <div
                              className={`flex h-8 w-8 items-center justify-center rounded-full shrink-0 ${
                                isInbound
                                  ? "bg-emerald-100 text-emerald-700"
                                  : "bg-brand-100 text-brand-700"
                              }`}
                            >
                              {isInbound ? (
                                <CornerDownLeft className="h-4 w-4" />
                              ) : (
                                <Send className="h-4 w-4" />
                              )}
                            </div>
                            <div className="min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-semibold text-slate-900 truncate">
                                  {msg.sender_name || msg.sender_email}
                                </span>
                                <span
                                  className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                                    isInbound
                                      ? "bg-emerald-100 text-emerald-800"
                                      : "bg-slate-100 text-slate-700"
                                  }`}
                                >
                                  {msg.direction}
                                </span>
                                {msg.classification && (
                                  <span className="inline-flex items-center rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
                                    {msg.classification}
                                  </span>
                                )}
                                {msg.sequence_step_position ? (
                                  <span className="inline-flex items-center rounded bg-brand-50 px-1.5 py-0.5 text-[10px] font-medium text-brand-700">
                                    Step {msg.sequence_step_position}
                                  </span>
                                ) : null}
                              </div>
                              <p className="text-[11px] text-slate-500 truncate">
                                To: {msg.recipient_email}
                              </p>
                            </div>
                          </div>

                          <div className="text-right shrink-0">
                            <span className="text-xs text-slate-500">
                              {formatFullTime(msg.timestamp)}
                            </span>
                            {msg.status && (
                              <p className="text-[10px] font-medium text-slate-400 mt-0.5">
                                Status: {msg.status}
                              </p>
                            )}
                          </div>
                        </div>

                        {/* Subject if different */}
                        {msg.subject && msg.subject !== currentDetail.subject && (
                          <div className="mb-2 text-xs font-medium text-slate-700">
                            Subject: {msg.subject}
                          </div>
                        )}

                        {/* Message Body */}
                        <div className="text-xs text-slate-800">
                          {isInbound ? (
                            <div className="whitespace-pre-wrap font-sans leading-relaxed">
                              {msg.content_text || "No text content available."}
                            </div>
                          ) : msg.content_html ? (
                            <SafeHtmlViewer html={msg.content_html} />
                          ) : (
                            <div className="whitespace-pre-wrap font-sans leading-relaxed text-slate-600">
                              {msg.subject}
                            </div>
                          )}
                        </div>

                        {/* Stop notification if qualifying reply */}
                        {isInbound && msg.association_status === "MATCHED" && (
                          <div className="mt-3 flex items-center gap-1.5 text-[11px] font-medium text-emerald-700 bg-emerald-50 rounded-md p-2">
                            <ShieldCheck className="h-4 w-4 shrink-0" />
                            <span>Campaign outreach was safely stopped upon receiving this reply.</span>
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
