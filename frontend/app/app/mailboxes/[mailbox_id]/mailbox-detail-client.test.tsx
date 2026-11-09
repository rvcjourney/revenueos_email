import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/app/mailboxes/mb-1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ mailbox_id: "mb-1" }),
}));

const {
  getMailbox,
  reconnectGmail,
  reconnectMicrosoft,
  disconnectMailbox,
  sendControlledTestEmail,
  updateMailbox,
  updateSmtpMailbox,
} = vi.hoisted(() => ({
  getMailbox: vi.fn(),
  reconnectGmail: vi.fn(),
  reconnectMicrosoft: vi.fn(),
  disconnectMailbox: vi.fn(),
  sendControlledTestEmail: vi.fn(),
  updateMailbox: vi.fn(),
  updateSmtpMailbox: vi.fn(),
}));

vi.mock("@/lib/mailboxes-api", () => ({
  getMailbox,
  reconnectGmail,
  reconnectMicrosoft,
  disconnectMailbox,
  sendControlledTestEmail,
  updateMailbox,
  updateSmtpMailbox,
}));

// The limits card has its own tests (components/mailboxes/sending-limits-card.test.tsx).
vi.mock("@/components/mailboxes/sending-limits-card", () => ({
  SendingLimitsCard: () => null,
}));
vi.mock("@/components/mailboxes/safety-holds-card", () => ({
  SafetyHoldsCard: () => null,
}));

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));

vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import { MailboxDetailClient } from "./mailbox-detail-client";

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function baseMailbox(overrides: Record<string, unknown> = {}) {
  return {
    id: "mb-1",
    provider: "GMAIL",
    email_address: "sender@example.com",
    sender_display_name: "Sender",
    signature_html: null,
    connection_state: "CONNECTED",
    health_state: "HEALTHY",
    policy_state: "ENABLED",
    policy_reason: null,
    circuit_state: "CLOSED",
    sync_state: "CURRENT",
    blocked_until: null,
    pending_safety_count: 0,
    current_connection_generation: 1,
    config_version: 1,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("MailboxDetailClient", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the Microsoft provider badge and Reconnect Microsoft action", async () => {
    useWorkspace.mockReturnValue({
      activeWorkspaceId: "ws-1",
      activeWorkspace: { role_code: "MANAGER" },
    });
    getMailbox.mockResolvedValue(baseMailbox({ provider: "MICROSOFT" }));

    renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);

    expect(await screen.findByText("Microsoft 365")).toBeInTheDocument();
    // Only shown when needsReconnect is true; verify no Reconnect button
    // renders for a healthy Microsoft mailbox, matching Gmail's behavior.
    expect(
      screen.queryByRole("button", { name: /reconnect microsoft/i }),
    ).not.toBeInTheDocument();
  });

  it("shows Reconnect Microsoft when a Microsoft mailbox needs reconnection", async () => {
    useWorkspace.mockReturnValue({
      activeWorkspaceId: "ws-1",
      activeWorkspace: { role_code: "MANAGER" },
    });
    getMailbox.mockResolvedValue(
      baseMailbox({ provider: "MICROSOFT", connection_state: "RECONNECT_REQUIRED" }),
    );
    reconnectMicrosoft.mockResolvedValue({
      authorization_url: "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
      expires_at: "2026-01-01T00:10:00Z",
    });

    renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);

    const button = await screen.findByRole("button", { name: /reconnect microsoft/i });
    await userEvent.click(button);

    expect(reconnectMicrosoft).toHaveBeenCalledWith("ws-1", "mb-1");
    expect(reconnectGmail).not.toHaveBeenCalled();
  });

  it("shows Update Configuration (not Reconnect) for an SMTP mailbox and never displays a password", async () => {
    useWorkspace.mockReturnValue({
      activeWorkspaceId: "ws-1",
      activeWorkspace: { role_code: "MANAGER" },
    });
    getMailbox.mockResolvedValue(
      baseMailbox({
        provider: "SMTP",
        smtp_config: {
          host: "smtp.example.com",
          port: 587,
          security_mode: "STARTTLS",
          username: "smtp-user",
        },
      }),
    );

    const { container } = renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);

    expect(await screen.findByText("Custom SMTP")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /update configuration/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^reconnect/i }),
    ).not.toBeInTheDocument();

    // Safe config summary is visible; no password field/value anywhere
    // before the edit form is opened.
    expect(screen.getByText("smtp.example.com")).toBeInTheDocument();
    expect(screen.getByText("smtp-user")).toBeInTheDocument();
    expect(container.querySelector('input[type="password"]')).not.toBeInTheDocument();
  });

  it("submits an SMTP configuration update and clears the password field", async () => {
    useWorkspace.mockReturnValue({
      activeWorkspaceId: "ws-1",
      activeWorkspace: { role_code: "MANAGER" },
    });
    getMailbox.mockResolvedValue(
      baseMailbox({
        provider: "SMTP",
        smtp_config: {
          host: "smtp.example.com",
          port: 587,
          security_mode: "STARTTLS",
          username: "smtp-user",
        },
      }),
    );
    updateSmtpMailbox.mockResolvedValue(
      baseMailbox({
        provider: "SMTP",
        smtp_config: {
          host: "smtp.example.com",
          port: 587,
          security_mode: "STARTTLS",
          username: "smtp-user",
        },
      }),
    );

    renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: /update configuration/i }),
    );
    await user.click(screen.getByRole("button", { name: /validate & save/i }));

    expect(updateSmtpMailbox).toHaveBeenCalledWith("ws-1", "mb-1", {
      host: "smtp.example.com",
      port: 587,
      security_mode: "STARTTLS",
      username: "smtp-user",
    });
  });

  it("Gmail reconnect still calls reconnectGmail, not reconnectMicrosoft", async () => {
    useWorkspace.mockReturnValue({
      activeWorkspaceId: "ws-1",
      activeWorkspace: { role_code: "MANAGER" },
    });
    getMailbox.mockResolvedValue(
      baseMailbox({ provider: "GMAIL", connection_state: "RECONNECT_REQUIRED" }),
    );
    reconnectGmail.mockResolvedValue({
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth",
      expires_at: "2026-01-01T00:10:00Z",
    });

    renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);
    const button = await screen.findByRole("button", { name: /reconnect google/i });
    await userEvent.click(button);

    expect(reconnectGmail).toHaveBeenCalledWith("ws-1", "mb-1");
    expect(reconnectMicrosoft).not.toHaveBeenCalled();
  });

  describe("reply detection status", () => {
    function open(overrides: Record<string, unknown>) {
      useWorkspace.mockReturnValue({
        activeWorkspaceId: "ws-1",
        activeWorkspace: { role_code: "MANAGER" },
      });
      getMailbox.mockResolvedValue(baseMailbox(overrides));
      renderWithClient(<MailboxDetailClient mailboxId="mb-1" />);
    }

    it("confirms when replies are being detected", async () => {
      open({ reply_sync_status: "ENABLED" });
      const card = await screen.findByTestId("reply-sync-status");
      expect(card).toHaveTextContent(/replies to emails sent from this mailbox are detected/i);
    });

    it("tells the user to reconnect when the mailbox lacks read access", async () => {
      open({ reply_sync_status: "RECONNECT_REQUIRED" });
      const card = await screen.findByTestId("reply-sync-status");
      expect(card).toHaveTextContent(/reconnect it to grant read access/i);
      expect(card).toHaveTextContent(/follow-ups will not stop on a reply/i);
    });

    it("tells an SMTP user to add IMAP settings, and shows the IMAP host when saved", async () => {
      open({
        provider: "SMTP",
        reply_sync_status: "IMAP_NOT_CONFIGURED",
        smtp_config: {
          host: "smtp.example.com",
          port: 587,
          security_mode: "STARTTLS",
          username: "u",
        },
      });
      const card = await screen.findByTestId("reply-sync-status");
      expect(card).toHaveTextContent(/no IMAP settings are saved/i);
      expect(await screen.findByText("Not configured")).toBeInTheDocument();
    });

    it("shows the saved IMAP server for an SMTP mailbox that can detect replies", async () => {
      open({
        provider: "SMTP",
        reply_sync_status: "ENABLED",
        smtp_config: {
          host: "smtp.example.com",
          port: 587,
          security_mode: "STARTTLS",
          username: "u",
          imap_host: "imap.example.com",
          imap_port: 993,
        },
      });
      expect(await screen.findByText("imap.example.com:993")).toBeInTheDocument();
    });

    it("shows nothing for providers without reply detection", async () => {
      open({ reply_sync_status: "UNSUPPORTED" });
      await screen.findByText("sender@example.com");
      expect(screen.queryByTestId("reply-sync-status")).not.toBeInTheDocument();
    });
  });
});
