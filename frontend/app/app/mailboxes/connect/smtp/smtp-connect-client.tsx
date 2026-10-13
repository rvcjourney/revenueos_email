"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, Loader2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  IMAP_DEFAULTS,
  ImapSettingsFields,
  imapPayload,
  type ImapSettings,
} from "@/components/mailboxes/imap-settings-fields";
import { ApiError } from "@/lib/api-client";
import { connectSmtpMailbox } from "@/lib/mailboxes-api";
import { useWorkspace } from "@/lib/workspace-context";
import type { SmtpSecurityMode } from "@/types/domain";

// Safe, fixed lookup from backend error code to user-facing copy. Never
// render a raw backend error message here -- it may describe internal
// network/DNS/TLS detail that shouldn't reach the browser.
const SAFE_ERROR_MESSAGES: Record<string, string> = {
  unsafe_destination: "That server address is not allowed. Check the host and port.",
  auth_failure: "Authentication failed. Check the username and password.",
  provider_error: "Could not connect to the SMTP server. Please verify the configuration.",
  bad_request: "The configuration was rejected. Please check the fields and try again.",
  validation_error: "Please check the fields and try again.",
  conflict: "This SMTP account is already connected to another workspace.",
};

function safeErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.code && SAFE_ERROR_MESSAGES[err.code]) {
    return SAFE_ERROR_MESSAGES[err.code];
  }
  return "Connection failed. Please check your configuration and try again.";
}

const SECURITY_MODE_DEFAULT_PORT: Record<SmtpSecurityMode, number> = {
  STARTTLS: 587,
  IMPLICIT_TLS: 465,
};

export function SmtpConnectClient() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { activeWorkspaceId } = useWorkspace();

  const [host, setHost] = useState("");
  const [port, setPort] = useState<number>(587);
  const [securityMode, setSecurityMode] = useState<SmtpSecurityMode>("STARTTLS");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [emailAddress, setEmailAddress] = useState("");
  const [senderDisplayName, setSenderDisplayName] = useState("");
  const [imap, setImap] = useState<ImapSettings>(IMAP_DEFAULTS);
  const [validationError, setValidationError] = useState<string | null>(null);

  const connectMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return connectSmtpMailbox(activeWorkspaceId, {
        host: host.trim(),
        port,
        security_mode: securityMode,
        username: username.trim(),
        password,
        email_address: emailAddress.trim(),
        sender_display_name: senderDisplayName.trim() || null,
        ...imapPayload(imap),
      });
    },
    onSuccess: async (res) => {
      // Never keep passwords in memory after a successful save.
      setPassword("");
      setImap((current) => ({ ...current, password: "" }));
      await queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "mailboxes"],
      });
      router.push(`/app/mailboxes/${res.mailbox_id}?connected=true`);
    },
  });

  function handleSecurityModeChange(mode: SmtpSecurityMode) {
    setSecurityMode(mode);
    setPort(SECURITY_MODE_DEFAULT_PORT[mode]);
  }

  function validate(): string | null {
    if (!host.trim()) return "Host is required.";
    if (!(port === 587 || port === 465)) return "Port must be 587 or 465.";
    if (!username.trim()) return "Username is required.";
    if (!password) return "Password is required.";
    if (!emailAddress.trim() || !emailAddress.includes("@")) {
      return "A valid email address is required.";
    }
    return null;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const err = validate();
    setValidationError(err);
    if (err) return;
    connectMutation.mutate();
  }

  const isSubmitting = connectMutation.isPending;
  const displayedError =
    validationError ??
    (connectMutation.isError ? safeErrorMessage(connectMutation.error) : null);

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <div>
        <Button variant="ghost" size="sm" asChild className="-ml-3 mb-2 text-slate-500">
          <Link href="/app/mailboxes/connect">
            <ArrowLeft className="mr-1.5 h-4 w-4" />
            Back
          </Link>
        </Button>
        <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900">
          Connect Custom SMTP
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Enter your SMTP server details. We&apos;ll validate the connection before saving.
        </p>
      </div>

      {displayedError ? <Alert variant="error">{displayedError}</Alert> : null}

      <form onSubmit={handleSubmit} className="space-y-5 rounded-xl border border-slate-200 bg-white p-6 shadow-card">
        <Field label="Host" required>
          <Input
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="smtp.example.com"
            disabled={isSubmitting}
            autoComplete="off"
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="smtp-security-mode">Security Mode</Label>
            <select
              id="smtp-security-mode"
              value={securityMode}
              onChange={(e) => handleSecurityModeChange(e.target.value as SmtpSecurityMode)}
              disabled={isSubmitting}
              className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-10 px-3"
            >
              <option value="STARTTLS">STARTTLS (587)</option>
              <option value="IMPLICIT_TLS">Implicit TLS (465)</option>
            </select>
          </div>
          <Field label="Port" required>
            <Input
              type="number"
              value={port}
              onChange={(e) => setPort(Number(e.target.value))}
              disabled={isSubmitting}
            />
          </Field>
        </div>

        <Field label="Username" required>
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="user@example.com"
            disabled={isSubmitting}
            autoComplete="off"
          />
        </Field>

        <Field label="Password" required>
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={isSubmitting}
            autoComplete="new-password"
          />
        </Field>

        <Field label="Sender Email Address" required>
          <Input
            type="email"
            value={emailAddress}
            onChange={(e) => setEmailAddress(e.target.value)}
            placeholder="sales@example.com"
            disabled={isSubmitting}
          />
        </Field>

        <Field label="Sender Display Name">
          <Input
            value={senderDisplayName}
            onChange={(e) => setSenderDisplayName(e.target.value)}
            placeholder="Sales Team"
            disabled={isSubmitting}
          />
        </Field>

        <ImapSettingsFields
          idPrefix="smtp"
          value={imap}
          onChange={setImap}
          disabled={isSubmitting}
        />

        <div className="flex justify-end pt-2">
          <Button type="submit" disabled={isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Testing connection...
              </>
            ) : (
              <>
                Validate &amp; Connect
                <ChevronRight className="ml-1.5 h-4 w-4" />
              </>
            )}
          </Button>
        </div>
      </form>
    </div>
  );
}
