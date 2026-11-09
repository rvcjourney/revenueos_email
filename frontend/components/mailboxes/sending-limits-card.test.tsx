import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { getMailboxLimits, setMailboxLimits } = vi.hoisted(() => ({
  getMailboxLimits: vi.fn(),
  setMailboxLimits: vi.fn(),
}));

vi.mock("@/lib/mailbox-limits-api", () => ({ getMailboxLimits, setMailboxLimits }));

import { ApiError } from "@/lib/api-client";
import { SendingLimitsCard } from "./sending-limits-card";

const BASE = {
  daily_cap: 50,
  min_spacing_seconds: 60,
  configured: true,
  default_daily_cap: 50,
  default_min_spacing_seconds: 60,
  max_daily_cap: 500,
  max_spacing_seconds: 3600,
};

function renderCard(canManage = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SendingLimitsCard workspaceId="ws-1" mailboxId="mb-1" canManage={canManage} />
    </QueryClientProvider>,
  );
}

describe("SendingLimitsCard", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows the current limits", async () => {
    getMailboxLimits.mockResolvedValue(BASE);
    renderCard();

    expect(await screen.findByLabelText("Emails per day")).toHaveValue("50");
    expect(screen.getByLabelText("Minimum seconds between emails")).toHaveValue("60");
    expect(screen.queryByText(/no sending limit/i)).not.toBeInTheDocument();
  });

  it("warns that an unset mailbox will not send, and suggests the defaults", async () => {
    getMailboxLimits.mockResolvedValue({
      ...BASE,
      daily_cap: null,
      min_spacing_seconds: null,
      configured: false,
    });
    renderCard();

    expect(await screen.findByText(/will not send from it until one is set/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Emails per day")).toHaveValue("50");
  });

  it("saves new limits as numbers", async () => {
    getMailboxLimits.mockResolvedValue(BASE);
    setMailboxLimits.mockResolvedValue({ ...BASE, daily_cap: 120, min_spacing_seconds: 90 });
    const user = userEvent.setup();
    renderCard();

    const cap = await screen.findByLabelText("Emails per day");
    await user.clear(cap);
    await user.type(cap, "120");
    const spacing = screen.getByLabelText("Minimum seconds between emails");
    await user.clear(spacing);
    await user.type(spacing, "90");
    await user.click(screen.getByRole("button", { name: /save limits/i }));

    await waitFor(() =>
      expect(setMailboxLimits).toHaveBeenCalledWith("ws-1", "mb-1", {
        daily_cap: 120,
        min_spacing_seconds: 90,
      }),
    );
    expect(await screen.findByText("Sending limits saved.")).toBeInTheDocument();
  });

  it("refuses values outside the allowed range without calling the server", async () => {
    getMailboxLimits.mockResolvedValue(BASE);
    const user = userEvent.setup();
    renderCard();

    const cap = await screen.findByLabelText("Emails per day");
    await user.clear(cap);
    await user.type(cap, "501");

    expect(screen.getByText("Enter a whole number from 1 to 500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save limits/i })).toBeDisabled();
    await user.clear(cap);
    await user.type(cap, "0");
    expect(screen.getByRole("button", { name: /save limits/i })).toBeDisabled();
    await user.clear(cap);
    await user.type(cap, "abc");
    expect(screen.getByRole("button", { name: /save limits/i })).toBeDisabled();
    expect(setMailboxLimits).not.toHaveBeenCalled();
  });

  it("is read-only for someone who cannot manage mailboxes", async () => {
    getMailboxLimits.mockResolvedValue(BASE);
    renderCard(false);

    expect(await screen.findByLabelText("Emails per day")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save limits/i })).not.toBeInTheDocument();
  });

  it("reports a permission error from the server", async () => {
    getMailboxLimits.mockResolvedValue(BASE);
    setMailboxLimits.mockRejectedValue(new ApiError("Forbidden", 403, "forbidden", null));
    const user = userEvent.setup();
    renderCard();

    await screen.findByLabelText("Emails per day");
    await user.click(screen.getByRole("button", { name: /save limits/i }));

    expect(
      await screen.findByText("You don't have permission to change sending limits."),
    ).toBeInTheDocument();
  });

  it("says so when the limits cannot be loaded", async () => {
    getMailboxLimits.mockRejectedValue(new Error("boom"));
    renderCard();

    expect(await screen.findByText(/couldn't load this mailbox's limits/i)).toBeInTheDocument();
  });
});
