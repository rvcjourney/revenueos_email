"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Send,
  Shield,
  Trash2,
} from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TypeToConfirmDialog } from "@/components/ui/type-to-confirm-dialog";
import {
  IMAP_DEFAULTS,
  ImapSettingsFields,
  imapPayload,
  type ImapSettings,
} from "@/components/mailboxes/imap-settings-fields";
import { ProviderBadge, providerSubtext } from "@/components/mailboxes/provider-badge";
import { SafetyHoldsCard } from "@/components/mailboxes/safety-holds-card";
import { SendingLimitsCard } from "@/components/mailboxes/sending-limits-card";
import { ApiError } from "@/lib/api-client";
import {
  disconnectMailbox,
  getMailbox,
  reconnectGmail,
  reconnectMicrosoft,
  sendControlledTestEmail,
  updateMailbox,
  updateSmtpMailbox,
} from "@/lib/mailboxes-api";
import { purgeMailbox } from "@/lib/erasure-api";
import { errorMessage, isRetryableError } from "@/lib/errors";
import { canEraseData } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type { MailboxTestSendResult, SmtpSecurityMode } from "@/types/domain";
import { LoadingBlock } from "@/components/ui/skeleton";

function canManageMailboxes(role?: string) {
  return role === "OWNER" || role === "ADMIN" || role === "MANAGER";
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function MailboxDetailClient({ mailboxId: propId }: { mailboxId?: string } = {}) {
  const params = useParams<{ mailbox_id: string }>();
  const mailboxId = propId || params.mailbox_id;
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const mayManage = canManageMailboxes(activeWorkspace?.role_code);
  const mayErase = canEraseData(activeWorkspace?.role_code);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [removed, setRemoved] = useState(false);

  const isNewlyConnected = searchParams.get("connected") === "true";

  // Form states
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [signatureHtml, setSignatureHtml] = useState<string | null>(null);
  const [hasLoadedForm, setHasLoadedForm] = useState(false);

  // Test send states
  const [testRecipient, setTestRecipient] = useState("");
  const [testSendResult, setTestSendResult] = useState<MailboxTestSendResult | null>(null);
  const [testSendError, setTestSendError] = useState<string | null>(null);

  // Disconnect confirmation
  const [showDisconnectConfirm, setShowDisconnectConfirm] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);

  // SMTP configuration update (no OAuth reconnect concept for SMTP)
  const [showSmtpEditForm, setShowSmtpEditForm] = useState(false);
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState<number>(587);
  const [smtpSecurityMode, setSmtpSecurityMode] = useState<SmtpSecurityMode>("STARTTLS");
  const [smtpUsername, setSmtpUsername] = useState("");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [imap, setImap] = useState<ImapSettings>(IMAP_DEFAULTS);
  const [hasLoadedSmtpForm, setHasLoadedSmtpForm] = useState(false);

  const mailboxQuery = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "mailbox", mailboxId],
    queryFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      const data = await getMailbox(activeWorkspaceId, mailboxId);
      if (!hasLoadedForm) {
        setDisplayName(data.sender_display_name);
        setSignatureHtml(data.signature_html);
        setTestRecipient(data.email_address);
        setHasLoadedForm(true);
      }
      if (!hasLoadedSmtpForm && data.smtp_config) {
        setSmtpHost(data.smtp_config.host);
        setSmtpPort(data.smtp_config.port);
        setSmtpSecurityMode(data.smtp_config.security_mode);
        setSmtpUsername(data.smtp_config.username);
        setImap({
          host: data.smtp_config.imap_host ?? "",
          port: data.smtp_config.imap_port ?? 993,
          username: data.smtp_config.imap_username ?? "",
          password: "",
        });
        setHasLoadedSmtpForm(true);
      }
      return data;
    },
    enabled: Boolean(activeWorkspaceId && mailboxId),
  });

  const mailbox = mailboxQuery.data;

  // Mutations
  const updateMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return updateMailbox(activeWorkspaceId, mailboxId, {
        sender_display_name: displayName,
        signature_html: signatureHtml,
      });
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(
        ["workspace", activeWorkspaceId, "mailbox", mailboxId],
        updated,
      );
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
      });
      setActionSuccess("Settings updated successfully.");
      setActionError(null);
    },
    onError: (err) => {
      setActionSuccess(null);
      setActionError(err instanceof ApiError ? err.message : "Failed to update mailbox.");
    },
  });

  const testSendMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      setTestSendError(null);
      setTestSendResult(null);
      return sendControlledTestEmail(activeWorkspaceId, mailboxId, testRecipient);
    },
    onSuccess: (result) => {
      setTestSendResult(result);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailbox", mailboxId],
      });
    },
    onError: (err) => {
      setTestSendError(
        err instanceof ApiError
          ? err.message
          : "Failed to send test email. Please try again.",
      );
    },
  });

  const reconnectMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      if (mailbox?.provider === "MICROSOFT") {
        return reconnectMicrosoft(activeWorkspaceId, mailboxId);
      }
      return reconnectGmail(activeWorkspaceId, mailboxId);
    },
    onSuccess: (res) => {
      window.location.href = res.authorization_url;
    },
    onError: (err) => {
      setActionError(
        err instanceof ApiError ? err.message : "Failed to initiate reconnection.",
      );
    },
  });

  const updateSmtpConfigMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return updateSmtpMailbox(activeWorkspaceId, mailboxId, {
        host: smtpHost.trim(),
        port: smtpPort,
        security_mode: smtpSecurityMode,
        username: smtpUsername.trim(),
        // Omitted entirely (not an empty string) when left blank, so the
        // backend keeps the existing credential.
        ...(smtpPassword ? { password: smtpPassword } : {}),
        ...imapPayload(imap),
      });
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(
        ["workspace", activeWorkspaceId, "mailbox", mailboxId],
        updated,
      );
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
      });
      setSmtpPassword("");
      setImap((current) => ({ ...current, password: "" }));
      setShowSmtpEditForm(false);
      setActionSuccess("SMTP configuration updated and re-validated.");
      setActionError(null);
    },
    onError: (err) => {
      setActionSuccess(null);
      setActionError(
        err instanceof ApiError
          ? err.message
          : "Failed to update SMTP configuration.",
      );
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return disconnectMailbox(activeWorkspaceId, mailboxId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailbox", mailboxId],
      });
      setShowDisconnectConfirm(false);
      setActionSuccess("Mailbox disconnected and credentials zeroed.");
    },
    onError: (err) => {
      setActionError(
        err instanceof ApiError ? err.message : "Failed to disconnect mailbox.",
      );
    },
  });

  const removeMutation = useMutation({
    mutationFn: (phrase: string) => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return purgeMailbox(activeWorkspaceId, mailboxId, phrase);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
      });
      setConfirmRemove(false);
      setRemoved(true);
    },
    onError: (err) =>
      setRemoveError(err instanceof ApiError ? err.message : "Failed to remove mailbox."),
  });

  if (removed) {
    return (
      <div className="mx-auto max-w-xl space-y-4 py-8 text-center">
        <Alert variant="success">The mailbox was removed.</Alert>
        <Button variant="outline" asChild>
          <Link href="/app/mailboxes">
            <ArrowLeft className="mr-2 h-4 w-4" />
            Return to Mailboxes
          </Link>
        </Button>
      </div>
    );
  }

  if (mailboxQuery.isLoading) {
    return (
      <LoadingBlock size="lg" />
    );
  }

  if (mailboxQuery.error || !mailbox) {
    // A 5xx / network failure is transient and says nothing about whether the
    // mailbox exists, so offer to try again instead of a dead end.
    const canRetry = isRetryableError(mailboxQuery.error);
    return (
      <div className="mx-auto max-w-xl space-y-4 py-8 text-center">
        <Alert variant="error">
          {mailboxQuery.error instanceof ApiError
            ? errorMessage(mailboxQuery.error)
            : "Mailbox not found or unavailable."}
        </Alert>
        {canRetry ? (
          <Button
            type="button"
            onClick={() => void mailboxQuery.refetch()}
            disabled={mailboxQuery.isFetching}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            Try again
          </Button>
        ) : null}
        <Button variant="outline" asChild>
          <Link href="/app/mailboxes">
            <ArrowLeft className="mr-2 h-4 w-4" />
            Return to Mailboxes
          </Link>
        </Button>
      </div>
    );
  }

  const isConnected = mailbox.connection_state === "CONNECTED";
  const isHealthy = mailbox.health_state === "HEALTHY";
  const needsReconnect =
    mailbox.connection_state === "RECONNECT_REQUIRED" ||
    mailbox.health_state === "DEGRADED";

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* Back button & Header */}
      <div>
        <Button variant="ghost" size="sm" asChild className="-ml-3 mb-2 text-slate-500">
          <Link href="/app/mailboxes">
            <ArrowLeft className="mr-1.5 h-4 w-4" />
            Back to Mailboxes
          </Link>
        </Button>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900">
                {mailbox.email_address}
              </h1>
              <span className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-800">
                <ProviderBadge provider={mailbox.provider} />
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-500">
              Connected generation {mailbox.current_connection_generation} · Added{" "}
              {formatDate(mailbox.created_at)}
            </p>
          </div>
          <div className="flex items-center gap-3">
            {mailbox.provider === "SMTP" && mayManage ? (
              <Button
                variant="outline"
                onClick={() => setShowSmtpEditForm((v) => !v)}
              >
                <RefreshCw className="mr-2 h-4 w-4" />
                Update Configuration
              </Button>
            ) : needsReconnect && mayManage ? (
              <Button
                onClick={() => reconnectMutation.mutate()}
                disabled={reconnectMutation.isPending}
                className="bg-amber-600 hover:bg-amber-700 text-white"
              >
                {reconnectMutation.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="mr-2 h-4 w-4" />
                )}
                Reconnect {mailbox.provider === "MICROSOFT" ? "Microsoft" : "Google"}
              </Button>
            ) : null}
            {isConnected && mayManage && !showDisconnectConfirm ? (
              <Button
                variant="outline"
                className="text-red-600 border-red-200 hover:bg-red-50 hover:text-red-700"
                onClick={() => setShowDisconnectConfirm(true)}
              >
                <Trash2 className="mr-2 h-4 w-4" />
                Disconnect
              </Button>
            ) : null}
          </div>
        </div>
      </div>

      {/* Newly connected alert */}
      {isNewlyConnected ? (
        <Alert variant="success">
          Your mailbox has been securely authenticated and connected. You can now send a controlled test email below.
        </Alert>
      ) : null}

      {/* Action feedback */}
      {actionSuccess ? <Alert variant="success">{actionSuccess}</Alert> : null}
      {actionError ? <Alert variant="error">{actionError}</Alert> : null}

      {/* Disconnect confirmation card */}
      {showDisconnectConfirm ? (
        <div className="rounded-xl border border-red-200 bg-red-50 p-6">
          <h3 className="text-base font-semibold text-red-900">
            Are you sure you want to disconnect this mailbox?
          </h3>
          <p className="mt-1 text-sm text-red-700">
            Disconnecting immediately zeroes all stored cryptographic tokens and revokes OAuth permissions with Google. Any active sending through this mailbox will stop.
          </p>
          <div className="mt-4 flex gap-3">
            <Button
              variant="danger"
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending}
            >
              {disconnectMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              Confirm Disconnect
            </Button>
            <Button
              variant="outline"
              onClick={() => setShowDisconnectConfirm(false)}
              disabled={disconnectMutation.isPending}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {mailbox.connection_state === "DISCONNECTED" && mayErase ? (
        <div className="rounded-xl border border-red-200 bg-white p-6">
          <h3 className="text-base font-semibold text-slate-900">Remove this mailbox</h3>
          <p className="mt-1 text-sm text-slate-600">
            Removes the mailbox from your workspace, together with every email it sent and
            every reply it received. Its credentials are already destroyed. Your leads and
            suppression list are not affected. It can&apos;t be removed while a campaign that
            isn&apos;t archived uses it, or within 25 hours of its last send.
          </p>
          <Button
            className="mt-4 border-red-200 text-red-600 hover:bg-red-50 hover:text-red-700"
            variant="outline"
            onClick={() => {
              setRemoveError(null);
              setConfirmRemove(true);
            }}
          >
            <Trash2 className="mr-2 h-4 w-4" />
            Remove mailbox…
          </Button>
        </div>
      ) : null}

      <TypeToConfirmDialog
        open={confirmRemove}
        title={`Remove ${mailbox.email_address}?`}
        description="The mailbox and its sent and received email history are deleted permanently. You can connect the same address again later, but the history won't come back. This can't be undone."
        phrase={mailbox.email_address}
        confirmLabel="Remove mailbox"
        loading={removeMutation.isPending}
        error={removeError}
        onConfirm={() => removeMutation.mutate(mailbox.email_address)}
        onCancel={() => setConfirmRemove(false)}
      />

      {/* SMTP configuration update form */}
      {showSmtpEditForm && mailbox.provider === "SMTP" ? (
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-5">
          <div>
            <h2 className="text-base font-bold text-slate-900">
              Update SMTP Configuration
            </h2>
            <p className="text-sm text-slate-500 mt-1">
              Leave the password blank to keep the existing credential. The new
              configuration is validated before it becomes active.
            </p>
          </div>

          <Field id="smtp-edit-host" label="Host" required>
            <Input
              value={smtpHost}
              onChange={(e) => setSmtpHost(e.target.value)}
              disabled={updateSmtpConfigMutation.isPending}
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="smtp-edit-security-mode">Security Mode</Label>
              <select
                id="smtp-edit-security-mode"
                value={smtpSecurityMode}
                onChange={(e) => setSmtpSecurityMode(e.target.value as SmtpSecurityMode)}
                disabled={updateSmtpConfigMutation.isPending}
                className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-10 px-3"
              >
                <option value="STARTTLS">STARTTLS (587)</option>
                <option value="IMPLICIT_TLS">Implicit TLS (465)</option>
              </select>
            </div>
            <Field id="smtp-edit-port" label="Port" required>
              <Input
                type="number"
                value={smtpPort}
                onChange={(e) => setSmtpPort(Number(e.target.value))}
                disabled={updateSmtpConfigMutation.isPending}
              />
            </Field>
          </div>

          <Field id="smtp-edit-username" label="Username" required>
            <Input
              value={smtpUsername}
              onChange={(e) => setSmtpUsername(e.target.value)}
              disabled={updateSmtpConfigMutation.isPending}
              autoComplete="off"
            />
          </Field>

          <Field id="smtp-edit-password" label="New Password (leave blank to keep current)">
            <Input
              type="password"
              value={smtpPassword}
              onChange={(e) => setSmtpPassword(e.target.value)}
              disabled={updateSmtpConfigMutation.isPending}
              autoComplete="new-password"
            />
          </Field>

          <ImapSettingsFields
            idPrefix="smtp-edit"
            value={imap}
            onChange={setImap}
            disabled={updateSmtpConfigMutation.isPending}
            passwordLabel="New incoming mail secret (leave blank to keep current)"
          />

          <div className="flex justify-end gap-3 pt-2">
            <Button
              variant="outline"
              onClick={() => setShowSmtpEditForm(false)}
              disabled={updateSmtpConfigMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              onClick={() => updateSmtpConfigMutation.mutate()}
              disabled={updateSmtpConfigMutation.isPending}
            >
              {updateSmtpConfigMutation.isPending ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Validating...
                </>
              ) : (
                "Validate & Save"
              )}
            </Button>
          </div>
        </div>
      ) : null}

      {/* Safe, read-only SMTP configuration summary (never the password) */}
      {mailbox.provider === "SMTP" && mailbox.smtp_config && !showSmtpEditForm ? (
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            SMTP Configuration
          </h3>
          <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-slate-500">Host</dt>
              <dd className="font-medium text-slate-900">{mailbox.smtp_config.host}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Port</dt>
              <dd className="font-medium text-slate-900">{mailbox.smtp_config.port}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Security</dt>
              <dd className="font-medium text-slate-900">
                {mailbox.smtp_config.security_mode === "IMPLICIT_TLS"
                  ? "Implicit TLS"
                  : "STARTTLS"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Username</dt>
              <dd className="font-medium text-slate-900">{mailbox.smtp_config.username}</dd>
            </div>
            <div className="col-span-2 sm:col-span-4">
              <dt className="text-xs text-slate-500">IMAP (reply detection)</dt>
              <dd className="font-medium text-slate-900">
                {mailbox.smtp_config.imap_host
                  ? `${mailbox.smtp_config.imap_host}:${mailbox.smtp_config.imap_port ?? 993}`
                  : "Not configured"}
              </dd>
            </div>
          </dl>
        </div>
      ) : null}

      {/* Reply detection status: replies stop sequences, so it must be visible when it is not working */}
      {mailbox.reply_sync_status && mailbox.reply_sync_status !== "UNSUPPORTED" ? (
        <div
          className="rounded-xl border border-slate-200 bg-white p-5 shadow-card"
          data-testid="reply-sync-status"
        >
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Reply detection
          </h3>
          {mailbox.reply_sync_status === "ENABLED" ? (
            <p className="mt-2 text-sm text-slate-700">
              Replies to emails sent from this mailbox are detected, and a lead&apos;s follow-ups
              stop when they reply.
            </p>
          ) : mailbox.reply_sync_status === "RECONNECT_REQUIRED" ? (
            <Alert variant="warning" className="mt-2">
              This mailbox was connected before Outly could read replies. Disconnect and reconnect
              it to grant read access; until then replies will not be detected and follow-ups will
              not stop on a reply.
            </Alert>
          ) : (
            <Alert variant="warning" className="mt-2">
              Outly cannot see replies to this mailbox because no IMAP settings are saved. Use
              Update Configuration to add them; until then follow-ups will not stop on a reply.
            </Alert>
          )}
        </div>
      ) : null}

      {activeWorkspaceId ? (
        <SafetyHoldsCard
          workspaceId={activeWorkspaceId}
          mailboxId={mailboxId}
          canManage={mayManage}
        />
      ) : null}

      {/* Status & Diagnostics grid */}
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Connection
            </span>
            {isConnected ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            ) : (
              <AlertTriangle className="h-4 w-4 text-slate-400" />
            )}
          </div>
          <p className="mt-2 text-base font-bold text-slate-900">
            {mailbox.connection_state}
          </p>
          <p className="mt-0.5 text-xs text-slate-500">
            {providerSubtext(mailbox.provider)}
          </p>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Health State
            </span>
            {isHealthy ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            ) : (
              <AlertTriangle className="h-4 w-4 text-amber-600" />
            )}
          </div>
          <p className="mt-2 text-base font-bold text-slate-900">
            {mailbox.health_state}
          </p>
          <p className="mt-0.5 text-xs text-slate-500">
            {mailbox.provider === "SMTP"
              ? "Connection & authentication validation"
              : "Token freshness & validation"}
          </p>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Policy & Safety
            </span>
            <Shield className="h-4 w-4 text-slate-400" />
          </div>
          <p className="mt-2 text-base font-bold text-slate-900">
            {mailbox.policy_state}
          </p>
          <p className="mt-0.5 text-xs text-slate-500">
            {mailbox.policy_reason || "No restrictions active"}
          </p>
        </div>
      </div>

      {activeWorkspaceId ? (
        <SendingLimitsCard
          workspaceId={activeWorkspaceId}
          mailboxId={mailboxId}
          canManage={mayManage}
        />
      ) : null}

      {/* Controlled Test Email Section */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-5">
        <div>
          <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
            <Send className="h-4 w-4 text-brand-600" />
            Send Controlled Test Email
          </h2>
          <p className="text-sm text-slate-500 mt-1">
            Validate the end-to-end delivery pipeline through the connected provider. The test email is bounded, checked against suppression rules, and recorded in the message log.
          </p>
        </div>

        {testSendResult ? (
          <Alert variant="success">
            Test email dispatched successfully!
            {testSendResult.provider_message_id ? (
              <>
                {" "}
                Provider message ID:{" "}
                <code className="rounded bg-emerald-100 px-1 py-0.5 text-xs font-mono">
                  {testSendResult.provider_message_id}
                </code>
              </>
            ) : null}
          </Alert>
        ) : null}

        {testSendError ? <Alert variant="error">{testSendError}</Alert> : null}

        <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
          <div className="flex-1">
            <Field id="test-recipient" label="Recipient Email Address" required>
              <Input
                type="email"
                value={testRecipient}
                onChange={(e) => setTestRecipient(e.target.value)}
                placeholder="test@example.com"
                disabled={!isConnected || testSendMutation.isPending || !mayManage}
              />
            </Field>
          </div>
          <Button
            onClick={() => testSendMutation.mutate()}
            disabled={
              !isConnected ||
              !testRecipient.trim() ||
              testSendMutation.isPending ||
              !mayManage
            }
            className="bg-brand-600 hover:bg-brand-700 text-white shrink-0"
          >
            {testSendMutation.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Sending Test Email...
              </>
            ) : (
              <>
                <Send className="mr-2 h-4 w-4" />
                Send Test Email
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Sender Settings Form */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card space-y-6">
        <div>
          <h2 className="text-base font-bold text-slate-900">
            Sender Settings
          </h2>
          <p className="text-sm text-slate-500 mt-1">
            Configure the display name and email signature that recipients see on outbound messages.
          </p>
        </div>

        <div className="space-y-4">
          <Field id="display-name" label="Sender Display Name">
            <Input
              value={displayName ?? ""}
              onChange={(e) => setDisplayName(e.target.value || null)}
              placeholder="e.g. Alex Smith"
              disabled={!mayManage || updateMutation.isPending}
            />
          </Field>

          <Field id="signature" label="Email Signature (HTML / Text)">
            <textarea
              id="signature"
              rows={4}
              value={signatureHtml ?? ""}
              onChange={(e) => setSignatureHtml(e.target.value || null)}
              placeholder="<p>Best regards,<br/>Alex Smith</p>"
              disabled={!mayManage || updateMutation.isPending}
              className="w-full rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-900 shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600 disabled:opacity-50 font-mono"
            />
          </Field>
        </div>

        {mayManage ? (
          <div className="flex justify-end pt-2">
            <Button
              onClick={() => updateMutation.mutate()}
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              Save Changes
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
