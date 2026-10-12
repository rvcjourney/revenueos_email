import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listSupplements: vi.fn(),
  selectSupplement: vi.fn(),
  enrollSupplement: vi.fn(),
  retrySupplement: vi.fn(),
}));
vi.mock("@/lib/campaigns-api", () => api);

const leadsApi = vi.hoisted(() => ({ listLeadLists: vi.fn(), listLeads: vi.fn() }));
vi.mock("@/lib/leads-api", () => leadsApi);

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast }) }));

import { ApiError } from "@/lib/api-client";

import { AddLeadsPanel } from "./add-leads-panel";

function supplement(overrides: Record<string, unknown> = {}) {
  return {
    id: "aud-9",
    campaign_id: "camp-1",
    revision: 2,
    status: "READY",
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:00:05Z",
    total_candidates: 4,
    processed_count: 4,
    accepted_count: 2,
    excluded_count: 2,
    exclusions: { already_enrolled: 1, suppressed: 1 },
    enroll_state: "NOT_STARTED",
    render_state: "NOT_STARTED",
    enrolled_count: 0,
    error_reason: null,
    ...overrides,
  };
}

function renderPanel() {
  leadsApi.listLeadLists.mockResolvedValue({
    items: [{ id: "list-1", name: "Fresh list", member_count: 5 }],
    next_cursor: null,
  });
  leadsApi.listLeads.mockResolvedValue({ items: [], next_cursor: null });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AddLeadsPanel workspaceId="ws-1" campaignId="camp-1" />
    </QueryClientProvider>,
  );
}

describe("AddLeadsPanel", () => {
  afterEach(() => vi.clearAllMocks());

  it("checks the chosen lists before anything is added", async () => {
    api.listSupplements.mockResolvedValue([]);
    api.selectSupplement.mockResolvedValue(supplement({ status: "CAPTURING" }));
    const user = userEvent.setup();
    renderPanel();

    const check = await screen.findByRole("button", { name: "Check leads" });
    expect(check).toBeDisabled(); // nothing chosen yet
    await user.click(await screen.findByRole("checkbox", { name: /Fresh list/ }));
    await user.click(check);

    await waitFor(() =>
      expect(api.selectSupplement).toHaveBeenCalledWith("ws-1", "camp-1", {
        list_ids: ["list-1"],
        lead_ids: [],
      }),
    );
  });

  it("shows who will be added and who is skipped, and adds only on confirmation", async () => {
    api.listSupplements.mockResolvedValue([supplement()]);
    api.enrollSupplement.mockResolvedValue(supplement({ enroll_state: "PENDING", render_state: "PENDING" }));
    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText("2 new leads can be added")).toBeInTheDocument();
    expect(screen.getByText(/1 already in this campaign, 1 suppressed/)).toBeInTheDocument();
    expect(api.enrollSupplement).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Add 2 leads to this campaign" }));
    await waitFor(() => expect(api.enrollSupplement).toHaveBeenCalledWith("ws-1", "camp-1", "aud-9"));
  });

  it("will not offer to add when nobody in the selection is new", async () => {
    api.listSupplements.mockResolvedValue([supplement({ accepted_count: 0, excluded_count: 4 })]);
    renderPanel();
    expect(await screen.findByText("0 new leads can be added")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /to this campaign/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Nobody in this selection can be added/)).toBeInTheDocument();
  });

  it("shows progress while the checks and the adding run", async () => {
    api.listSupplements.mockResolvedValue([supplement({ status: "CAPTURING", processed_count: 1 })]);
    renderPanel();
    expect(await screen.findByText(/Checking leads\.\.\. \(1 of 4\)/)).toBeInTheDocument();
  });

  it("confirms when the leads are in and says they send after resume", async () => {
    api.listSupplements.mockResolvedValue([
      supplement({ enroll_state: "READY", render_state: "READY", enrolled_count: 2 }),
    ]);
    renderPanel();
    expect(await screen.findByText(/2 leads added\. They are sent when you resume\./)).toBeInTheDocument();
  });

  it("reports a failed add instead of pretending it worked", async () => {
    api.listSupplements.mockResolvedValue([
      supplement({ enroll_state: "FAILED", error_reason: "An unexpected error occurred" }),
    ]);
    renderPanel();
    expect(await screen.findByText(/Adding these leads failed: An unexpected error occurred/)).toBeInTheDocument();
  });

  it("lets the user retry a failed add and warns that resume is blocked until it succeeds", async () => {
    api.listSupplements.mockResolvedValue([
      supplement({ enroll_state: "FAILED", error_reason: "An unexpected error occurred" }),
    ]);
    api.retrySupplement.mockResolvedValue(supplement({ enroll_state: "FAILED" }));
    const user = userEvent.setup();
    renderPanel();
    expect(await screen.findByText(/can't resume the campaign until this succeeds/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.retrySupplement).toHaveBeenCalledWith("ws-1", "camp-1", "aud-9"));
  });

  it("shows the server's reason when adding is refused", async () => {
    api.listSupplements.mockResolvedValue([supplement()]);
    api.enrollSupplement.mockRejectedValue(
      new ApiError("Leads can only be added to a campaign while it is paused", 409, "state_conflict", null),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Add 2 leads to this campaign" }));
    expect(await screen.findByText(/while it is paused/)).toBeInTheDocument();
  });
});
