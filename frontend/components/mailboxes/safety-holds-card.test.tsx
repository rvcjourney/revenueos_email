import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { listSafetyHolds, releaseSafetyHold } = vi.hoisted(() => ({
  listSafetyHolds: vi.fn(),
  releaseSafetyHold: vi.fn(),
}));

vi.mock("@/lib/safety-holds-api", () => ({ listSafetyHolds, releaseSafetyHold }));

import { ApiError } from "@/lib/api-client";
import { SafetyHoldsCard } from "./safety-holds-card";

const BOUNCE_HOLD = {
  id: "hold-1",
  kind: "HIGH_BOUNCE_RATE" as const,
  status: "ACTIVE" as const,
  reason: "Bounce rate 12.0% over the last 7 days (12 of 100 emails).",
  created_at: "2026-03-10T12:00:00Z",
  resolved_at: null,
};

function renderCard(canManage = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SafetyHoldsCard workspaceId="ws-1" mailboxId="mb-1" canManage={canManage} />
    </QueryClientProvider>,
  );
}

describe("SafetyHoldsCard", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows nothing for a mailbox with no hold", async () => {
    listSafetyHolds.mockResolvedValue([]);
    renderCard();

    await waitFor(() => expect(listSafetyHolds).toHaveBeenCalled());
    expect(screen.queryByTestId("safety-holds")).not.toBeInTheDocument();
  });

  it("explains why the mailbox is not sending", async () => {
    listSafetyHolds.mockResolvedValue([BOUNCE_HOLD]);
    renderCard();

    expect(await screen.findByText("This mailbox is not sending")).toBeInTheDocument();
    expect(screen.getByText("Paused: too many emails bounced")).toBeInTheDocument();
    expect(screen.getByText(BOUNCE_HOLD.reason)).toBeInTheDocument();
    expect(screen.getByText(/check the list that bounced/i)).toBeInTheDocument();
  });

  it("releases a hold for someone who can manage mailboxes", async () => {
    listSafetyHolds.mockResolvedValue([BOUNCE_HOLD]);
    releaseSafetyHold.mockResolvedValue({ ...BOUNCE_HOLD, status: "RESOLVED" });
    const user = userEvent.setup();
    renderCard();

    await user.click(await screen.findByRole("button", { name: /release and allow sending/i }));

    await waitFor(() =>
      expect(releaseSafetyHold).toHaveBeenCalledWith("ws-1", "mb-1", "hold-1"),
    );
  });

  it("offers no release button to someone who cannot manage mailboxes", async () => {
    listSafetyHolds.mockResolvedValue([BOUNCE_HOLD]);
    renderCard(false);

    expect(await screen.findByText(/a manager or admin can release this/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release/i })).not.toBeInTheDocument();
  });

  it("reports a failed release", async () => {
    listSafetyHolds.mockResolvedValue([BOUNCE_HOLD]);
    releaseSafetyHold.mockRejectedValue(new ApiError("Forbidden", 403, "forbidden", null));
    const user = userEvent.setup();
    renderCard();

    await user.click(await screen.findByRole("button", { name: /release and allow sending/i }));

    expect(
      await screen.findByText("You don't have permission to release a hold."),
    ).toBeInTheDocument();
  });

  it("stays quiet if the holds cannot be loaded", async () => {
    listSafetyHolds.mockRejectedValue(new Error("boom"));
    renderCard();

    await waitFor(() => expect(listSafetyHolds).toHaveBeenCalled());
    expect(screen.queryByTestId("safety-holds")).not.toBeInTheDocument();
  });
});
