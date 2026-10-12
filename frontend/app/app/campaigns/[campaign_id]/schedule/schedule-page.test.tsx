import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useParams: () => ({ campaign_id: "camp-1" }) }));

const api = vi.hoisted(() => ({
  getCampaign: vi.fn(),
  listCampaignSettings: vi.fn(),
  createCampaignSettings: vi.fn(),
}));
vi.mock("@/lib/campaigns-api", () => api);

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast }) }));

import CampaignSchedulePage from "./page";

const current = {
  id: "set-2",
  campaign_id: "camp-1",
  revision: 2,
  timezone: "Europe/London",
  weekdays: [1, 2, 3],
  window_start_local: "10:00:00",
  window_end_local: "15:30:00",
  daily_limit: 40,
  created_at: "",
};

function renderPage(status: string, role: string) {
  useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1", activeWorkspace: { role_code: role } });
  api.getCampaign.mockResolvedValue({ id: "camp-1", status, version: 5 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <CampaignSchedulePage />
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe("Schedule tab", () => {
  beforeEach(() => api.listCampaignSettings.mockResolvedValue([current]));
  afterEach(() => vi.clearAllMocks());

  it("lets a drafter configure a draft campaign, as before", async () => {
    renderPage("DRAFT", "MEMBER");
    expect(await screen.findByRole("button", { name: "Save schedule" })).toBeInTheDocument();
  });

  it("lets a manager change the schedule of a paused campaign, starting from the current one", async () => {
    api.createCampaignSettings.mockResolvedValue({ ...current, id: "set-3", revision: 3 });
    const user = userEvent.setup();
    const { invalidate } = renderPage("PAUSED", "MANAGER");

    expect(await screen.findByText(/applies to emails that haven't been sent/i)).toBeVisible();
    // The form shows what the campaign has now, not generic defaults.
    await waitFor(() => expect(screen.getByDisplayValue("Europe/London")).toBeInTheDocument());
    expect(screen.getByDisplayValue("10:00")).toBeInTheDocument();
    expect(screen.getByDisplayValue("15:30")).toBeInTheDocument();
    expect(screen.getByDisplayValue("40")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save schedule" }));

    await waitFor(() =>
      expect(api.createCampaignSettings).toHaveBeenCalledWith("ws-1", "camp-1", {
        timezone: "Europe/London",
        weekdays: [1, 2, 3],
        window_start_local: "10:00:00",
        window_end_local: "15:30:00",
        daily_limit: 40,
      }),
    );
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringMatching(/applies to emails that haven't been sent/)),
    );
    // The campaign's version moved with its settings pointer: reload all of it.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workspace", "ws-1", "campaigns", "camp-1"] });
  });

  it("does not let a member change a paused campaign's schedule", async () => {
    renderPage("PAUSED", "MEMBER");
    expect(await screen.findByText(/Only a manager can change the schedule/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save schedule" })).not.toBeInTheDocument();
  });

  it("tells a running campaign's manager to pause first", async () => {
    renderPage("RUNNING", "MANAGER");
    expect(await screen.findByText(/Pause it first/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save schedule" })).not.toBeInTheDocument();
  });

  it("keeps a completed campaign's schedule fixed", async () => {
    renderPage("COMPLETED", "OWNER");
    expect(await screen.findByText(/because the campaign is completed/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save schedule" })).not.toBeInTheDocument();
  });
});
