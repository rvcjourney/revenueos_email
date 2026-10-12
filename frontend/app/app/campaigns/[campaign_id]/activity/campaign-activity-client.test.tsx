import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

let searchParamsMock = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useParams: () => ({ campaign_id: "camp-1" }),
  usePathname: () => "/app/campaigns/camp-1/activity",
  useRouter: () => ({ push, replace }),
  useSearchParams: () => searchParamsMock,
}));

const { getCampaignActivity } = vi.hoisted(() => ({ getCampaignActivity: vi.fn() }));
vi.mock("@/lib/analytics-api", () => ({ getCampaignActivity }));

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import type { CampaignActivityItem, CampaignActivityPage } from "@/types/analytics";
import { CampaignActivityClient } from "./campaign-activity-client";

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function item(overrides: Partial<CampaignActivityItem>): CampaignActivityItem {
  return {
    id: "SENT:1",
    type: "SENT",
    status: "SUCCESS",
    occurred_at: "2026-10-10T12:00:00Z",
    recipient_email: "ada@example.test",
    lead_id: "lead-1",
    message_id: "msg-1",
    subject: "Quick idea",
    step_position: 1,
    detail: null,
    open_count: null,
    ...overrides,
  };
}

function page(overrides: Partial<CampaignActivityPage> = {}): CampaignActivityPage {
  return {
    campaign_id: "camp-1",
    items: [item({})],
    next_cursor: null,
    has_more: false,
    counts_by_type: {
      SENT: 5,
      OPENED: 3,
      BOUNCED: 1,
      REPLIED: 2,
      UNSUBSCRIBED: 0,
      COMPLAINED: 0,
      FAILED: 1,
    },
    click_tracking_supported: false,
    delivery_confirmation_supported: false,
    ...overrides,
  };
}

describe("CampaignActivityClient", () => {
  afterEach(() => {
    vi.clearAllMocks();
    searchParamsMock = new URLSearchParams();
  });

  function setup(response: CampaignActivityPage | Error) {
    useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1", activeWorkspace: {} });
    if (response instanceof Error) getCampaignActivity.mockRejectedValue(response);
    else getCampaignActivity.mockResolvedValue(response);
    return renderWithClient(<CampaignActivityClient />);
  }

  it("shows a loading state, then each activity with type, recipient and status", async () => {
    setup(
      page({
        items: [
          item({ id: "SENT:1" }),
          item({
            id: "OPENED:1",
            type: "OPENED",
            recipient_email: "bob@example.test",
            detail: "Opened 3 times",
            open_count: 3,
          }),
          item({
            id: "BOUNCED:1",
            type: "BOUNCED",
            status: "ERROR",
            recipient_email: "cy@example.test",
            detail: "Hard bounce · 550 · No such user",
          }),
        ],
      }),
    );

    expect(await screen.findByText("ada@example.test")).toBeInTheDocument();
    expect(screen.getAllByTestId("campaign-activity-item")).toHaveLength(3);
    expect(screen.getByText("Opened 3 times")).toBeInTheDocument();
    expect(screen.getByText("Hard bounce · 550 · No such user")).toBeInTheDocument();
    expect(
      within(screen.getAllByTestId("campaign-activity-item")[2]).getByText("Failed"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ada@example.test" })).toHaveAttribute(
      "href",
      "/app/leads/lead-1",
    );
    // Counts from the first page label the type filter.
    expect(screen.getByRole("button", { name: "Sent 5" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All 12" })).toBeInTheDocument();
    expect(getCampaignActivity).toHaveBeenCalledWith(
      "ws-1",
      "camp-1",
      expect.objectContaining({ limit: 25, cursor: null }),
    );
  });

  it("states what is not tracked instead of inventing clicks or delivery", async () => {
    setup(page());
    await screen.findByText("ada@example.test");
    expect(screen.getByText(/Link clicks are not tracked/)).toBeInTheDocument();
    expect(screen.getByText(/do not confirm delivery/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Clicks/ })).not.toBeInTheDocument();
  });

  it("shows the empty state when the campaign has no activity yet", async () => {
    setup(page({ items: [] }));
    expect(await screen.findByText("No activity yet")).toBeInTheDocument();
  });

  it("offers to clear filters when nothing matches", async () => {
    searchParamsMock = new URLSearchParams("type=BOUNCED&q=zed");
    setup(page({ items: [] }));
    expect(await screen.findByText("No matching activity")).toBeInTheDocument();
    expect(getCampaignActivity).toHaveBeenCalledWith(
      "ws-1",
      "camp-1",
      expect.objectContaining({ type: "BOUNCED", q: "zed" }),
    );
    await userEvent.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    expect(push).toHaveBeenCalledWith("/app/campaigns/camp-1/activity");
  });

  it("shows an error with a working Retry", async () => {
    setup(new Error("boom"));
    expect(await screen.findByText(/Failed to load campaign activity/)).toBeInTheDocument();
    getCampaignActivity.mockResolvedValue(page());
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("ada@example.test")).toBeInTheDocument();
  });

  it("changing a filter updates the URL and drops the cursor", async () => {
    searchParamsMock = new URLSearchParams("cursor=abc");
    setup(page());
    await screen.findByText("ada@example.test");

    await userEvent.click(screen.getByRole("button", { name: "Bounce 1" }));
    expect(push).toHaveBeenLastCalledWith("/app/campaigns/camp-1/activity?type=BOUNCED");

    await userEvent.selectOptions(screen.getByLabelText("Activity status"), "ERROR");
    expect(push).toHaveBeenLastCalledWith(
      "/app/campaigns/camp-1/activity?status=ERROR",
    );
  });

  it("filters by date range", async () => {
    setup(page());
    await screen.findByText("ada@example.test");
    const from = screen.getByLabelText("From date");
    await userEvent.type(from, "2026-10-01");
    expect(push).toHaveBeenLastCalledWith(
      "/app/campaigns/camp-1/activity?from=2026-10-01",
    );
  });

  it("pages forward with the server cursor and can return to the first page", async () => {
    setup(page({ next_cursor: "next-1", has_more: true }));
    await screen.findByText("ada@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(push).toHaveBeenLastCalledWith("/app/campaigns/camp-1/activity?cursor=next-1");
  });

  it("does not offer First page on the first page and requests the cursor page", async () => {
    searchParamsMock = new URLSearchParams("cursor=next-1");
    setup(page({ counts_by_type: null, items: [item({ id: "SENT:9" })] }));
    await screen.findByText("ada@example.test");
    expect(getCampaignActivity).toHaveBeenCalledWith(
      "ws-1",
      "camp-1",
      expect.objectContaining({ cursor: "next-1" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "First page" }));
    expect(push).toHaveBeenLastCalledWith("/app/campaigns/camp-1/activity");
  });

  it("never shows an erased person's placeholder address", async () => {
    setup(
      page({ items: [item({ recipient_email: "erased-1@erased.invalid" })] }),
    );
    expect(await screen.findByText("Erased lead")).toBeInTheDocument();
    expect(screen.queryByText(/erased\.invalid/)).not.toBeInTheDocument();
  });
});
