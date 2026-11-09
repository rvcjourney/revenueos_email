import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useParams: () => ({ campaign_id: "camp-1" }) }));

const api = vi.hoisted(() => ({
  abandonAudience: vi.fn(),
  commitAudience: vi.fn(),
  getAudienceExclusions: vi.fn(),
  getCampaign: vi.fn(),
  getCommittedAudience: vi.fn(),
  selectAudience: vi.fn(),
  listSupplements: vi.fn(),
  selectSupplement: vi.fn(),
  enrollSupplement: vi.fn(),
}));
vi.mock("@/lib/campaigns-api", () => api);

const leadsApi = vi.hoisted(() => ({ listLeadLists: vi.fn(), listLeads: vi.fn() }));
vi.mock("@/lib/leads-api", () => leadsApi);

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/components/campaigns/audience-csv-import-dialog", () => ({
  AudienceCsvImportDialog: () => null,
}));

import CampaignAudiencePage from "./page";

function renderPage(status: string, role: string) {
  useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1", activeWorkspace: { role_code: role } });
  api.getCampaign.mockResolvedValue({ id: "camp-1", name: "Founders", status, version: 4 });
  api.getCommittedAudience.mockResolvedValue({
    id: "aud-1",
    campaign_id: "camp-1",
    revision: 1,
    status: "READY",
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:00:05Z",
    is_committed: true,
    total_candidates: 10,
    processed_count: 10,
    accepted_count: 9,
    excluded_count: 1,
    error_reason: null,
    selected_list_ids: [],
    selected_lead_ids: [],
  });
  api.listSupplements.mockResolvedValue([]);
  leadsApi.listLeadLists.mockResolvedValue({ items: [], next_cursor: null });
  leadsApi.listLeads.mockResolvedValue({ items: [], next_cursor: null });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CampaignAudiencePage />
    </QueryClientProvider>,
  );
}

describe("Audience tab", () => {
  afterEach(() => vi.clearAllMocks());

  it("lets a drafter pick an audience for a draft campaign, as before", async () => {
    renderPage("DRAFT", "MEMBER");
    expect(await screen.findByText("Select a new audience")).toBeInTheDocument();
    expect(screen.queryByText("Add leads")).not.toBeInTheDocument();
  });

  it("freezes the audience of a running campaign and says how to add leads", async () => {
    renderPage("RUNNING", "MANAGER");
    expect(await screen.findByText(/audience is fixed while it is running\. Pause the campaign/i)).toBeVisible();
    expect(screen.queryByText("Select a new audience")).not.toBeInTheDocument();
    expect(screen.queryByText("Add leads")).not.toBeInTheDocument();
  });

  it("lets a manager add leads to a paused campaign without touching its audience", async () => {
    renderPage("PAUSED", "MANAGER");
    expect(await screen.findByText("Add leads")).toBeVisible();
    expect(screen.getByText(/you can add more leads below while it is paused/i)).toBeVisible();
    expect(screen.queryByText("Select a new audience")).not.toBeInTheDocument();
    // The committed audience is shown, but cannot be re-committed or abandoned.
    expect(await screen.findByText(/Audience ready: 9 eligible/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Use this audience" })).not.toBeInTheDocument();
  });

  it("does not let a member add leads to a paused campaign", async () => {
    renderPage("PAUSED", "MEMBER");
    expect(await screen.findByText(/Only a manager can add leads to a paused campaign/i)).toBeVisible();
    expect(screen.queryByText("Add leads")).not.toBeInTheDocument();
  });
});
