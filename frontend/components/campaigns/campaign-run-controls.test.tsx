import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ pauseCampaign: vi.fn(), resumeCampaign: vi.fn() }));
vi.mock("@/lib/campaigns-api", () => api);

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast }) }));

import { ApiError } from "@/lib/api-client";

import { CampaignRunControls } from "./campaign-run-controls";

type Status = "DRAFT" | "SCHEDULED" | "RUNNING" | "PAUSED" | "COMPLETED" | "ARCHIVED";

function campaign(status: Status, extra: Record<string, unknown> = {}) {
  return {
    id: "camp-1",
    name: "Founders",
    status,
    version: 7,
    paused_at: null,
    updated_at: new Date().toISOString(),
    ...extra,
  };
}

function renderControls(status: Status, role = "MANAGER", extra: Record<string, unknown> = {}) {
  useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1", activeWorkspace: { role_code: role } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <CampaignRunControls campaign={campaign(status, extra)} />
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe("CampaignRunControls", () => {
  afterEach(() => vi.clearAllMocks());

  it("offers Pause (and not Resume) for a running campaign", () => {
    renderControls("RUNNING");
    expect(screen.getByRole("button", { name: "Pause campaign" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Resume campaign" })).not.toBeInTheDocument();
  });

  it("offers Pause for a scheduled campaign too", () => {
    renderControls("SCHEDULED");
    expect(screen.getByRole("button", { name: "Pause campaign" })).toBeInTheDocument();
  });

  it("asks for confirmation, then pauses with the version on screen and reports in-flight emails", async () => {
    const user = userEvent.setup();
    api.pauseCampaign.mockResolvedValue({ ...campaign("PAUSED"), outstanding_attempts: 3 });
    const { invalidate } = renderControls("RUNNING");

    await user.click(screen.getByRole("button", { name: "Pause campaign" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/can't be recalled/i)).toBeInTheDocument();
    expect(api.pauseCampaign).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Pause campaign" }));

    await waitFor(() =>
      expect(api.pauseCampaign).toHaveBeenCalledWith("ws-1", "camp-1", { expected_version: 7 }),
    );
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringMatching(/3 emails were already being sent/)),
    );
    // The header, the tabs and the campaigns list all learn about the new state.
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["workspace", "ws-1", "campaigns", "camp-1"],
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workspace", "ws-1", "campaigns"] });
  });

  it("shows how long a campaign has been paused and offers Resume only", () => {
    const eighteenMinutesAgo = new Date(Date.now() - 18 * 60_000).toISOString();
    renderControls("PAUSED", "MANAGER", { paused_at: eighteenMinutesAgo });
    expect(screen.getByText(/Paused 18 minutes ago/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume campaign" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Pause campaign" })).not.toBeInTheDocument();
  });

  it("explains the catch-up before resuming, then resumes", async () => {
    const user = userEvent.setup();
    api.resumeCampaign.mockResolvedValue(campaign("RUNNING"));
    renderControls("PAUSED");

    await user.click(screen.getByRole("button", { name: "Resume campaign" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/gradually over your sending windows/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Resume campaign" }));

    await waitFor(() =>
      expect(api.resumeCampaign).toHaveBeenCalledWith("ws-1", "camp-1", { expected_version: 7 }),
    );
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Campaign resumed."));
  });

  it("offers finished leads the new emails only when the box is ticked, and starts unticked", async () => {
    const user = userEvent.setup();
    api.resumeCampaign.mockResolvedValue(campaign("RUNNING"));
    renderControls("PAUSED");

    await user.click(screen.getByRole("button", { name: "Resume campaign" }));
    let dialog = await screen.findByRole("dialog");
    const box = within(dialog).getByRole("checkbox", { name: /already finished the sequence/i });
    expect(box).not.toBeChecked();
    await user.click(box);
    await user.click(within(dialog).getByRole("button", { name: "Resume campaign" }));
    await waitFor(() =>
      expect(api.resumeCampaign).toHaveBeenCalledWith("ws-1", "camp-1", {
        expected_version: 7,
        include_completed: true,
      }),
    );

    // Reopening the dialog resets the choice.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Resume campaign" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("checkbox", { name: /already finished the sequence/i })).not.toBeChecked();
  });

  it("does not offer it for a personalized campaign", async () => {
    const user = userEvent.setup();
    renderControls("PAUSED", "MANAGER", { campaign_type: "HYPER_PERSONALIZED" });
    await user.click(screen.getByRole("button", { name: "Resume campaign" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("lists what blocks a resume instead of a generic error", async () => {
    const user = userEvent.setup();
    api.resumeCampaign.mockRejectedValue(
      new ApiError("This campaign can't resume yet.", 422, "resume_blocked", "req-1", {
        errors: [
          { code: "no_usable_mailboxes", message: "None of the sending mailboxes can send right now." },
          { code: "lead_additions_in_progress", message: "Leads you added are still being prepared." },
        ],
      }),
    );
    renderControls("PAUSED");

    await user.click(screen.getByRole("button", { name: "Resume campaign" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Resume campaign" }));

    expect(await screen.findByText(/None of the sending mailboxes can send/)).toBeInTheDocument();
    expect(screen.getByText(/Leads you added are still being prepared/)).toBeInTheDocument();
    expect(toast).not.toHaveBeenCalled();
  });

  it("shows the server's message when pausing fails", async () => {
    const user = userEvent.setup();
    api.pauseCampaign.mockRejectedValue(
      new ApiError("Campaign was modified by another user. Please refresh and try again.", 409, "conflict", null),
    );
    renderControls("RUNNING");
    await user.click(screen.getByRole("button", { name: "Pause campaign" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Pause campaign" }));
    expect(await screen.findByText(/modified by another user/)).toBeInTheDocument();
  });

  it.each(["DRAFT", "COMPLETED", "ARCHIVED"] as Status[])(
    "offers neither action for a %s campaign",
    (status) => {
      renderControls(status);
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    },
  );

  it("offers nothing to someone who may not run campaigns", () => {
    renderControls("RUNNING", "MEMBER");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    renderControls("PAUSED", "VIEWER");
    expect(screen.queryByRole("button", { name: "Resume campaign" })).not.toBeInTheDocument();
  });
});
