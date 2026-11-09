import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  commitAudience: vi.fn(),
  getAudienceCaptureStatus: vi.fn(),
  getCommittedAudience: vi.fn(),
  selectAudience: vi.fn(),
  getImport: vi.fn(),
  createLeadList: vi.fn(),
}));

vi.mock("@/lib/campaigns-api", () => ({
  commitAudience: api.commitAudience,
  getAudienceCaptureStatus: api.getAudienceCaptureStatus,
  getCommittedAudience: api.getCommittedAudience,
  selectAudience: api.selectAudience,
}));
vi.mock("@/lib/imports-api", () => ({ getImport: api.getImport }));
vi.mock("@/lib/leads-api", () => ({ createLeadList: api.createLeadList }));

// The wizard itself is covered by its own tests; here it just finishes an import.
vi.mock("@/app/app/leads/imports/new/new-import-page-client", () => ({
  NewImportPageClient: ({
    createTargetList,
    onImported,
  }: {
    createTargetList: () => Promise<string>;
    onImported: (job: unknown) => void;
  }) => (
    <button
      type="button"
      onClick={async () => {
        const listId = await createTargetList();
        onImported({
          id: "import-1",
          list_id: listId,
          status: "COMPLETED",
          processed_rows: 3,
          total_rows: 3,
          accepted_rows: 2,
          duplicate_rows: 1,
          rejected_rows: 0,
          failure_summary: null,
        });
      }}
    >
      finish import
    </button>
  ),
}));

import { AudienceCsvImportDialog } from "./audience-csv-import-dialog";

const READY_AUDIENCE = {
  id: "audience-2",
  status: "READY",
  accepted_count: 5,
  processed_count: 5,
  total_candidates: 5,
  error_reason: null,
};

function arrange() {
  api.createLeadList.mockResolvedValue({ id: "new-list" });
  api.getCommittedAudience.mockResolvedValue({
    id: "audience-1",
    status: "READY",
    is_committed: true,
    selected_list_ids: ["list-a", "new-list"],
    selected_lead_ids: ["lead-x"],
  });
  api.selectAudience.mockResolvedValue({ id: "audience-2", status: "CAPTURING" });
  api.getAudienceCaptureStatus.mockResolvedValue(READY_AUDIENCE);
  api.commitAudience.mockResolvedValue({ ...READY_AUDIENCE, is_committed: true });
  const onChanged = vi.fn();
  render(
    <AudienceCsvImportDialog
      open
      onOpenChange={vi.fn()}
      workspaceId="ws-1"
      campaignId="camp-1"
      campaignName="Spring outreach"
      onChanged={onChanged}
    />,
  );
  return { onChanged };
}

describe("AudienceCsvImportDialog", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("adds the imported list to what is already selected and commits it", async () => {
    const { onChanged } = arrange();

    await userEvent.setup().click(screen.getByRole("button", { name: "finish import" }));

    await screen.findByText(/campaign audience now has 5 eligible leads/i);
    expect(api.createLeadList).toHaveBeenCalledWith(
      "ws-1",
      expect.stringContaining("Spring outreach - CSV import"),
    );
    // The earlier selection is kept, the new list is added once.
    expect(api.selectAudience).toHaveBeenCalledWith("ws-1", "camp-1", {
      list_ids: ["list-a", "new-list"],
      lead_ids: ["lead-x"],
    });
    expect(api.commitAudience).toHaveBeenCalledWith("ws-1", "camp-1", "audience-2");
    expect(onChanged).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /import another file/i })).toBeTruthy();
  });

  it("does not carry over a failed or abandoned selection", async () => {
    arrange();
    api.getCommittedAudience.mockResolvedValue({
      id: "audience-0",
      status: "FAILED",
      is_committed: false,
      selected_list_ids: ["old-list"],
      selected_lead_ids: ["old-lead"],
    });

    await userEvent.setup().click(screen.getByRole("button", { name: "finish import" }));

    await screen.findByText(/campaign audience now has/i);
    expect(api.selectAudience).toHaveBeenCalledWith("ws-1", "camp-1", {
      list_ids: ["new-list"],
      lead_ids: [],
    });
  });

  it("says the leads are saved when the audience step fails", async () => {
    arrange();
    api.selectAudience.mockRejectedValue(new Error("An audience capture is already in progress."));

    await userEvent.setup().click(screen.getByRole("button", { name: "finish import" }));

    await screen.findByText(/already in progress/i);
    expect(screen.getByText(/saved in a new lead list/i)).toBeTruthy();
    expect(api.commitAudience).not.toHaveBeenCalled();
  });
});
