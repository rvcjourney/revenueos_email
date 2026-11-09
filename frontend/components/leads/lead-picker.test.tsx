import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { listLeads, listAllLeadIds } = vi.hoisted(() => ({
  listLeads: vi.fn(),
  listAllLeadIds: vi.fn(),
}));
vi.mock("@/lib/leads-api", () => ({ listLeads, listAllLeadIds }));

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import { LeadPicker } from "./lead-picker";

function leads(count: number, from = 0) {
  return Array.from({ length: count }, (_, i) => ({
    id: `lead-${from + i}`,
    first_name: "Lead",
    last_name: String(from + i),
    email: `lead${from + i}@example.com`,
    company: null,
  }));
}

let latestSelection = new Set<string>();

function Harness() {
  const [selected, setSelected] = useState(new Set<string>());
  latestSelection = selected;
  return (
    <LeadPicker
      enabled
      search=""
      onSearchChange={() => {}}
      selected={selected}
      onSelectedChange={setSelected}
    />
  );
}

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

describe("LeadPicker", () => {
  afterEach(() => {
    vi.clearAllMocks();
    latestSelection = new Set();
  });

  it("shows up to 100 leads at a time, so 60 leads can all be selected at once", async () => {
    useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1" });
    listLeads.mockResolvedValue({ items: leads(60), next_cursor: null });
    renderPicker();

    await userEvent.setup().click(await screen.findByLabelText(/select all shown/i));

    expect(listLeads).toHaveBeenCalledWith(
      "ws-1",
      expect.objectContaining({ limit: 100, status: "ACTIVE" }),
    );
    await waitFor(() => expect(latestSelection.size).toBe(60));
    expect(screen.queryByRole("button", { name: /select all matching/i })).toBeNull();
  });

  it("offers to select every match when there are more than one page", async () => {
    useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1" });
    listLeads.mockResolvedValue({ items: leads(100), next_cursor: "next" });
    listAllLeadIds.mockResolvedValue(leads(250).map((lead) => lead.id));
    renderPicker();

    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: /select all matching/i }));

    expect(listAllLeadIds).toHaveBeenCalledWith("ws-1", { q: "", status: "ACTIVE" });
    await waitFor(() => expect(latestSelection.size).toBe(250));
  });
});
