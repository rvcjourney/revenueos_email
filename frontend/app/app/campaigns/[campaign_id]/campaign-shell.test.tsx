import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useParams: () => ({ campaign_id: "camp-1" }),
  usePathname: () => "/app/campaigns/camp-1/sequence",
}));

const api = vi.hoisted(() => ({ getCampaign: vi.fn(), getPreflight: vi.fn() }));
vi.mock("@/lib/campaigns-api", () => api);

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import CampaignLayout from "./layout";

function renderLayout(campaignType?: "STANDARD" | "HYPER_PERSONALIZED") {
  useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1", activeWorkspace: { role_code: "MEMBER" } });
  api.getCampaign.mockResolvedValue({
    id: "camp-1",
    name: "Founders",
    description: null,
    status: "DRAFT",
    ...(campaignType ? { campaign_type: campaignType } : {}),
  });
  api.getPreflight.mockResolvedValue({ ready: false, errors: [], warnings: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CampaignLayout>
        <div>page body</div>
      </CampaignLayout>
    </QueryClientProvider>,
  );
}

async function tabLabels() {
  const nav = await screen.findByRole("navigation");
  return within(nav)
    .getAllByRole("link")
    .map((link) => link.textContent);
}

describe("Campaign shell", () => {
  afterEach(() => vi.clearAllMocks());

  it("has no separate Personalization tab: hyper-personalized campaigns author it on Sequence", async () => {
    renderLayout("HYPER_PERSONALIZED");
    // Same tabs as any campaign (ADR-0016 merged Personalization into Sequence).
    expect(await tabLabels()).toEqual([
      "Overview",
      "Analytics",
      "Activity",
      "Audience",
      "Sequence",
      "Senders",
      "Schedule",
      "Review",
    ]);
    expect(screen.getByText("Hyper-Personalized")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Personalization" })).not.toBeInTheDocument();
  });

  it("leaves standard campaigns unchanged", async () => {
    renderLayout("STANDARD");
    expect(await tabLabels()).toEqual([
      "Overview",
      "Analytics",
      "Activity",
      "Audience",
      "Sequence",
      "Senders",
      "Schedule",
      "Review",
    ]);
    expect(screen.queryByText("Hyper-Personalized")).not.toBeInTheDocument();
  });

  it("treats an older server response without a type as standard", async () => {
    renderLayout(undefined);
    expect(await tabLabels()).not.toContain("Personalization");
  });
});
