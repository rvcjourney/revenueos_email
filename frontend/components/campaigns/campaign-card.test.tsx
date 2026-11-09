import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}));

const { getCampaignAnalytics } = vi.hoisted(() => ({ getCampaignAnalytics: vi.fn() }));
vi.mock("@/lib/analytics-api", () => ({ getCampaignAnalytics }));

const { archiveCampaign, duplicateCampaign } = vi.hoisted(() => ({
  archiveCampaign: vi.fn(),
  duplicateCampaign: vi.fn(),
}));
vi.mock("@/lib/campaigns-api", () => ({ archiveCampaign, duplicateCampaign }));

const { purgeCampaign } = vi.hoisted(() => ({ purgeCampaign: vi.fn() }));
vi.mock("@/lib/erasure-api", () => ({ purgeCampaign }));

import { CampaignCard } from "@/components/campaigns/campaign-card";
import type { CampaignListItem } from "@/types/domain";

function campaign(overrides: Partial<CampaignListItem> = {}): CampaignListItem {
  return {
    id: "camp-1",
    workspace_id: "ws-1",
    name: "Q1 Outbound",
    description: "Cold outreach to founders",
    status: "RUNNING",
    campaign_type: "STANDARD",
    draft_sequence_id: null,
    draft_audience_id: null,
    current_settings_id: null,
    version: 4,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

const analytics = {
  total_recipients: 140,
  sent: 125,
  opened: 22,
  replied: 3,
  bounced: 4,
  open_rate: 18.4,
  reply_rate: 2.4,
  open_tracking_supported: true,
};

function renderCard(
  item: CampaignListItem,
  mayDraft = true,
  extra: Partial<React.ComponentProps<typeof CampaignCard>> = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CampaignCard campaign={item} workspaceId="ws-1" mayDraft={mayDraft} {...extra} />
    </QueryClientProvider>,
  );
}

describe("CampaignCard", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("does not request metrics for a draft that has never sent", () => {
    renderCard(campaign({ status: "DRAFT" }));

    expect(screen.getByText("DRAFT")).toBeInTheDocument();
    expect(screen.getByText(/not launched yet/i)).toBeInTheDocument();
    expect(getCampaignAnalytics).not.toHaveBeenCalled();
  });

  it("shows real performance metrics for a launched campaign", async () => {
    getCampaignAnalytics.mockResolvedValue(analytics);
    renderCard(campaign());

    expect(await screen.findByText("125")).toBeInTheDocument();
    expect(screen.getByText("Sent")).toBeInTheDocument();
    expect(screen.getByText("Total leads")).toBeInTheDocument();
    expect(screen.getByText("140")).toBeInTheDocument();
    expect(screen.getByText("Reply rate")).toBeInTheDocument();
    expect(screen.getByText("2.4%")).toBeInTheDocument();
    expect(screen.getByText("Open rate")).toBeInTheDocument();
    expect(screen.getByText("18.4%")).toBeInTheDocument();
    expect(getCampaignAnalytics).toHaveBeenCalledWith("ws-1", "camp-1");
  });

  it("shows bounces instead of an open rate when open tracking is unsupported", async () => {
    getCampaignAnalytics.mockResolvedValue({ ...analytics, open_tracking_supported: false });
    renderCard(campaign());

    expect(await screen.findByText("125")).toBeInTheDocument();
    expect(screen.queryByText("Open rate")).not.toBeInTheDocument();
    expect(screen.getByText("Reply rate")).toBeInTheDocument();
    expect(screen.getByText("Bounced")).toBeInTheDocument();
  });

  it("degrades quietly when metrics fail to load", async () => {
    getCampaignAnalytics.mockRejectedValue(new Error("boom"));
    renderCard(campaign());

    expect(await screen.findByText(/metrics are unavailable/i)).toBeInTheDocument();
    // The campaign itself is still usable.
    expect(screen.getByRole("link", { name: "Q1 Outbound" })).toHaveAttribute(
      "href",
      "/app/campaigns/camp-1/overview",
    );
  });

  it("names the main action after the campaign's state, and offers only View to read-only roles", () => {
    getCampaignAnalytics.mockResolvedValue(analytics);
    const overview = "/app/campaigns/camp-1/overview";
    const cases: [CampaignListItem["status"], string, string][] = [
      ["DRAFT", "Continue setup", overview],
      ["RUNNING", "View progress", overview],
      ["PAUSED", "View details", overview],
      ["COMPLETED", "View report", "/app/campaigns/camp-1/analytics"],
    ];
    for (const [status, label, href] of cases) {
      const { unmount } = renderCard(campaign({ status }), true);
      expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", href);
      unmount();
    }

    renderCard(campaign({ status: "COMPLETED" }), false);
    expect(screen.getByRole("link", { name: "View" })).toHaveAttribute("href", overview);
    expect(screen.queryByRole("link", { name: "View report" })).not.toBeInTheDocument();
  });

  it("describes the campaign's latest event and who created it", () => {
    renderCard(
      campaign({ status: "DRAFT", creator_name: "Naitik Patel", created_at: "2026-01-01T00:00:00Z" }),
    );
    expect(screen.getByText(/^Created /)).toBeInTheDocument();
    expect(screen.getByText("by Naitik Patel")).toBeInTheDocument();
  });

  it("offers archive per state and role: drafts to editors, activated ones to executors, never running", async () => {
    const user = userEvent.setup();
    getCampaignAnalytics.mockResolvedValue(analytics);
    const menu = async () =>
      user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));

    // Read-only role: view-only menu.
    const readOnly = renderCard(campaign(), false);
    await menu();
    expect(screen.getByRole("menuitem", { name: "Analytics" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Duplicate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Archive" })).not.toBeInTheDocument();
    readOnly.unmount();

    // A running campaign can't be archived by anyone: pause it first.
    const running = renderCard(campaign(), true, { mayExecute: true });
    await menu();
    expect(screen.getByRole("menuitem", { name: "Duplicate" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Archive" })).not.toBeInTheDocument();
    running.unmount();

    // A paused campaign needs the execute permission, not just the draft one.
    const pausedEditor = renderCard(campaign({ status: "PAUSED" }), true);
    await menu();
    expect(screen.queryByRole("menuitem", { name: "Archive" })).not.toBeInTheDocument();
    pausedEditor.unmount();

    const pausedManager = renderCard(campaign({ status: "PAUSED" }), true, { mayExecute: true });
    await menu();
    expect(screen.getByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
    pausedManager.unmount();

    // Editor, draft: both.
    renderCard(campaign({ status: "DRAFT" }), true);
    await menu();
    expect(screen.getByRole("menuitem", { name: "Duplicate" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
  });

  it.each([false, true])(
    "deletes an archived campaign (activated: %s), only after typing its name",
    async (activated) => {
      const user = userEvent.setup();
      purgeCampaign.mockResolvedValue({});
      getCampaignAnalytics.mockResolvedValue(analytics);
      renderCard(campaign({ status: "ARCHIVED", is_activated: activated }), true, { mayErase: true });

      await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
      await user.click(screen.getByRole("menuitem", { name: /delete permanently/i }));

      const confirm = await screen.findByRole("button", { name: "Delete permanently" });
      expect(confirm).toBeDisabled();
      await user.type(screen.getByRole("textbox"), "Q1 Outbound");
      expect(confirm).toBeEnabled();
      await user.click(confirm);

      await vi.waitFor(() =>
        expect(purgeCampaign).toHaveBeenCalledWith("ws-1", "camp-1", "Q1 Outbound"),
      );
    },
  );

  it("offers deletion only for archived campaigns and only to roles that may erase", async () => {
    const user = userEvent.setup();
    getCampaignAnalytics.mockResolvedValue(analytics);

    const noRole = renderCard(campaign({ status: "ARCHIVED", is_activated: true }), true);
    await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
    expect(screen.queryByRole("menuitem", { name: /delete permanently/i })).not.toBeInTheDocument();
    noRole.unmount();

    renderCard(campaign({ status: "PAUSED", is_activated: true }), true, { mayErase: true });
    await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
    expect(screen.queryByRole("menuitem", { name: /delete permanently/i })).not.toBeInTheDocument();
  });

  it("shows a checkbox for bulk selection and reports the toggle", async () => {
    const user = userEvent.setup();
    const onToggleSelect = vi.fn();
    renderCard(campaign({ status: "DRAFT" }), true, { selectable: true, onToggleSelect });
    await user.click(screen.getByRole("checkbox", { name: "Select Q1 Outbound" }));
    expect(onToggleSelect).toHaveBeenCalledWith("camp-1");
  });

  it("archives only after confirmation, using the campaign's current version", async () => {
    const user = userEvent.setup();
    archiveCampaign.mockResolvedValue({});
    renderCard(campaign({ status: "DRAFT", version: 7 }));

    await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));

    expect(await screen.findByRole("dialog", { name: /archive "q1 outbound"\?/i })).toBeInTheDocument();
    expect(archiveCampaign).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Archive campaign" }));
    expect(archiveCampaign).toHaveBeenCalledWith("ws-1", "camp-1", { expected_version: 7 });
  });

  it("does not archive when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderCard(campaign({ status: "DRAFT" }));

    await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(archiveCampaign).not.toHaveBeenCalled();
  });

  it("duplicates and opens the copy", async () => {
    const user = userEvent.setup();
    getCampaignAnalytics.mockResolvedValue(analytics);
    duplicateCampaign.mockResolvedValue({ id: "camp-2", name: "Q1 Outbound (copy)" });
    renderCard(campaign());

    await user.click(screen.getByRole("button", { name: /actions for q1 outbound/i }));
    await user.click(screen.getByRole("menuitem", { name: "Duplicate" }));

    await vi.waitFor(() => expect(duplicateCampaign).toHaveBeenCalledWith("ws-1", "camp-1"));
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith("/app/campaigns/camp-2/overview"));
  });
});
