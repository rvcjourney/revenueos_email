"use client";

import * as React from "react";
import { CheckCircle2, Loader2, Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover } from "@/components/ui/popover";
import { ApiError } from "@/lib/api-client";
import { sendSequenceStepTestEmail } from "@/lib/campaigns-api";
import type { MailboxTestSendResult } from "@/types/domain";

type Props = {
  workspaceId: string;
  campaignId: string;
  stepId: string;
  mailboxId: string;
  fromLabel: string;
  // null when previewing sample data (no real audience member).
  audienceMemberId: string | null;
  subject: string;
  preheader: string;
  bodyHtml: string;
  // Why sending is unavailable right now, shown as the button hint.
  blockedReason: string | null;
};

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function newKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function TestSendControl({
  workspaceId,
  campaignId,
  stepId,
  mailboxId,
  fromLabel,
  audienceMemberId,
  subject,
  preheader,
  bodyHtml,
  blockedReason,
}: Props) {
  const [recipient, setRecipient] = React.useState("");
  const [confirmed, setConfirmed] = React.useState(false);
  const [sending, setSending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<MailboxTestSendResult | null>(null);
  // Reused across retries of the same attempt (e.g. after a dropped connection)
  // and replaced once the attempt has a definite outcome.
  const keyRef = React.useRef<string | null>(null);

  const emailValid = EMAIL_RE.test(recipient.trim());

  async function send() {
    if (sending) return;
    setError(null);
    setResult(null);
    if (!emailValid) {
      setError("Enter a valid email address.");
      return;
    }
    if (!confirmed) {
      setError("Confirm that this address should receive the test email.");
      return;
    }
    setSending(true);
    keyRef.current ??= newKey();
    try {
      const outcome = await sendSequenceStepTestEmail(
        workspaceId,
        campaignId,
        stepId,
        {
          mailbox_id: mailboxId,
          recipient_email: recipient.trim(),
          confirm_recipient: true,
          audience_member_id: audienceMemberId,
          // A same-thread step may have no subject of its own; the server uses the saved one.
          email_subject: subject.trim() || undefined,
          email_body_html: bodyHtml,
          email_preheader: preheader.trim(),
        },
        keyRef.current,
      );
      setResult(outcome);
      keyRef.current = null;
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
        keyRef.current = null;
      } else {
        // Network failure: the send may or may not have happened, so keep the
        // key and say so instead of implying it failed cleanly.
        setError(
          "We couldn’t reach the server, so we can’t tell whether the test email was sent. Check the inbox before trying again.",
        );
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <Popover
      label="Send test email"
      align="end"
      className="bottom-full mb-1 mt-0 w-80"
      trigger={({ toggle, ariaProps }) => (
        <span title={blockedReason ?? undefined}>
          <Button
            variant="outline"
            onClick={toggle}
            disabled={blockedReason !== null}
            {...ariaProps}
          >
            <Send className="h-4 w-4" aria-hidden="true" />
            Send Test Email
          </Button>
        </span>
      )}
    >
      <form
        className="space-y-3 text-sm"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <p className="text-slate-600">
          Sends this email as it looks in the preview, with{" "}
          {audienceMemberId ? "the selected prospect’s data" : "sample data"}, from{" "}
          <span className="font-medium text-slate-900">{fromLabel}</span>. The subject starts
          with [Test].
        </p>
        <label className="block text-xs font-medium text-slate-600">
          Send to
          <input
            type="email"
            value={recipient}
            onChange={(event) => {
              setRecipient(event.target.value);
              setResult(null);
            }}
            placeholder="you@company.com"
            className="mt-1 h-9 w-full rounded-md border border-slate-200 px-2 text-sm"
          />
        </label>
        <label className="flex items-start gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            className="mt-0.5"
          />
          <span>I confirm this address should receive a real test email.</span>
        </label>
        {error ? (
          <p role="alert" className="text-xs text-red-600">
            {error}
          </p>
        ) : null}
        {result ? (
          result.status === "SENT" ? (
            <p role="status" className="flex items-center gap-1.5 text-xs text-emerald-700">
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              Test email sent to {result.recipient_email}.
            </p>
          ) : result.status === "UNKNOWN_OUTCOME" ? (
            <p role="status" className="text-xs text-amber-700">
              We couldn&apos;t confirm delivery. Check the inbox before sending again.
            </p>
          ) : (
            <p role="alert" className="text-xs text-red-600">
              {result.error_message ?? "The test email could not be sent."}
            </p>
          )
        ) : null}
        <div className="flex justify-end">
          <Button type="submit" size="sm" disabled={sending}>
            {sending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
            {sending ? "Sending…" : "Send test"}
          </Button>
        </div>
      </form>
    </Popover>
  );
}
