"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowLeft, Check, ChevronRight, Loader2, Server } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { startGmailOAuth, startMicrosoftOAuth } from "@/lib/mailboxes-api";
import { useWorkspace } from "@/lib/workspace-context";

export function ConnectPageClient() {
  const { activeWorkspaceId } = useWorkspace();
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConnectGmail() {
    if (!activeWorkspaceId) return;
    setIsConnecting(true);
    setError(null);
    try {
      const res = await startGmailOAuth(activeWorkspaceId, "/app/mailboxes");
      // Redirect browser to Google OAuth consent screen
      window.location.href = res.authorization_url;
    } catch (err) {
      setIsConnecting(false);
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Failed to initiate Google authentication. Please try again.");
      }
    }
  }

  async function handleConnectMicrosoft() {
    if (!activeWorkspaceId) return;
    setIsConnecting(true);
    setError(null);
    try {
      const res = await startMicrosoftOAuth(activeWorkspaceId, "/app/mailboxes");
      // Redirect browser to Microsoft OAuth consent screen
      window.location.href = res.authorization_url;
    } catch (err) {
      setIsConnecting(false);
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Failed to initiate Microsoft authentication. Please try again.");
      }
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div>
        <Button variant="ghost" size="sm" asChild className="-ml-3 mb-2 text-slate-500">
          <Link href="/app/mailboxes">
            <ArrowLeft className="mr-1.5 h-4 w-4" />
            Back to Mailboxes
          </Link>
        </Button>
        <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900">
          Connect a Mailbox
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Choose an email provider to authenticate and start sending through your own domain.
        </p>
      </div>

      {error ? <Alert variant="error">{error}</Alert> : null}

      <div className="grid gap-4">
        {/* Google / Gmail - ACTIVE */}
        <div className="group relative flex flex-col justify-between rounded-xl border-2 border-brand-600 bg-white p-6 shadow-sm transition hover:shadow-card-hover">
          <div className="flex items-start gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-red-50 border border-red-100">
              <svg className="h-6 w-6" viewBox="0 0 24 24">
                <path
                  fill="#EA4335"
                  d="M12 5c1.6 0 3 .6 4.1 1.6l3.1-3.1C17.3 1.8 14.8 1 12 1 7.5 1 3.7 3.6 1.9 7.3l3.7 2.9C6.5 7.3 9 5 12 5z"
                />
                <path
                  fill="#4285F4"
                  d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5c-.3 1.5-1.1 2.8-2.4 3.7l3.7 2.9c2.2-2 3.7-5 3.7-8.8z"
                />
                <path
                  fill="#FBBC05"
                  d="M5.6 14.8c-.2-.7-.4-1.5-.4-2.3s.2-1.6.4-2.3L1.9 7.3C.7 9.7 0 12.3 0 15.2s.7 5.5 1.9 7.9l3.7-2.9z"
                />
                <path
                  fill="#34A853"
                  d="M12 23.5c3.2 0 6-1.1 8-3l-3.7-2.9c-1.1.7-2.5 1.2-4.3 1.2-3 0-5.5-2-6.4-4.8L1.9 16.9C3.7 20.9 7.5 23.5 12 23.5z"
                />
              </svg>
            </div>
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <h3 className="font-semibold text-slate-900">Google / Gmail</h3>
                <span className="rounded-full bg-brand-50 px-2 py-0.5 text-xs font-semibold text-brand-700 border border-brand-200">
                  Recommended
                </span>
              </div>
              <p className="text-sm text-slate-500">
                Connect your Google Workspace or personal Gmail account using Google OAuth 2.0 with PKCE verification.
              </p>
              <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
                <li className="flex items-center gap-1">
                  <Check className="h-3.5 w-3.5 text-brand-600" />
                  AES-256 encrypted tokens
                </li>
                <li className="flex items-center gap-1">
                  <Check className="h-3.5 w-3.5 text-brand-600" />
                  Full SPF/DKIM alignment
                </li>
                <li className="flex items-center gap-1">
                  <Check className="h-3.5 w-3.5 text-brand-600" />
                  Controlled test sending
                </li>
              </ul>
            </div>
          </div>
          <div className="mt-6 flex items-center justify-end">
            <Button
              onClick={handleConnectGmail}
              disabled={isConnecting}
              className="bg-brand-600 hover:bg-brand-700 text-white"
            >
              {isConnecting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Redirecting to Google...
                </>
              ) : (
                <>
                  Connect with Google
                  <ChevronRight className="ml-1.5 h-4 w-4" />
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Microsoft 365 / Outlook - ACTIVE */}
        <div className="group relative flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-6 shadow-card transition hover:shadow-card-hover">
          <div className="flex items-start gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-blue-50 border border-blue-100">
              <svg className="h-6 w-6" viewBox="0 0 24 24">
                <path fill="#F25022" d="M1 1h10v10H1z" />
                <path fill="#00A4EF" d="M1 13h10v10H1z" />
                <path fill="#7FBA00" d="M13 1h10v10H13z" />
                <path fill="#FFB900" d="M13 13h10v10H13z" />
              </svg>
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Microsoft 365 / Outlook</h3>
              <p className="text-sm text-slate-500 mt-1">
                Connect Outlook and Microsoft 365 mailboxes via Microsoft Graph, using OAuth 2.0 with PKCE verification.
              </p>
            </div>
          </div>
          <div className="mt-6 flex items-center justify-end">
            <Button onClick={handleConnectMicrosoft} disabled={isConnecting} variant="outline">
              {isConnecting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Redirecting to Microsoft...
                </>
              ) : (
                <>
                  Connect with Microsoft
                  <ChevronRight className="ml-1.5 h-4 w-4" />
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Custom SMTP - ACTIVE, links to the configuration form */}
        <div className="group relative flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-6 shadow-card transition hover:shadow-card-hover">
          <div className="flex items-start gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-slate-100 border border-slate-200 text-slate-600">
              <Server className="h-6 w-6" />
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Custom SMTP</h3>
              <p className="text-sm text-slate-500 mt-1">
                Connect an existing SMTP-compatible mail server with your own host, port, and credentials.
              </p>
            </div>
          </div>
          <div className="mt-6 flex items-center justify-end">
            <Button variant="outline" asChild>
              <Link href="/app/mailboxes/connect/smtp">
                Configure SMTP
                <ChevronRight className="ml-1.5 h-4 w-4" />
              </Link>
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
