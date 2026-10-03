import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { listMailboxes } = vi.hoisted(() => ({ listMailboxes: vi.fn() }));
vi.mock("@/lib/mailboxes-api", () => ({ listMailboxes }));

const { listLeadLists } = vi.hoisted(() => ({ listLeadLists: vi.fn() }));
vi.mock("@/lib/leads-api", () => ({ listLeadLists }));

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import { ApiEndpoints } from "./api-endpoints";

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function mockWorkspace(role = "MEMBER") {
  useWorkspace.mockReturnValue({
    activeWorkspaceId: "ws-123",
    activeWorkspace: { role_code: role },
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("ApiEndpoints", () => {
  it("shows the intake URL for the active workspace and the ids it needs", async () => {
    mockWorkspace();
    listMailboxes.mockResolvedValue([{ id: "mb-1", email_address: "sales@example.com" }]);
    listLeadLists.mockResolvedValue({
      items: [{ id: "list-1", name: "Founders", member_count: 12 }],
      next_cursor: null,
    });

    renderWithClient(<ApiEndpoints />);

    expect(screen.getByRole("heading", { name: "API endpoints" })).toBeInTheDocument();
    expect(
      screen.getByText(/\/api\/v1\/workspaces\/ws-123\/integrations\/revenueos\/campaigns$/),
    ).toBeInTheDocument();
    expect(screen.getByText("X-RevenueOS-Key")).toBeInTheDocument();

    expect(await screen.findByText("sales@example.com")).toBeInTheDocument();
    expect(screen.getByText("mb-1")).toBeInTheDocument();
    expect(await screen.findByText("Founders")).toBeInTheDocument();
    expect(screen.getByText("list-1")).toBeInTheDocument();

    const body = screen.getByText(/"reference": "STRAT-0042"/);
    expect(body).toHaveTextContent('"mailbox_id": "mb-1"');
    expect(body).toHaveTextContent('"list-1"');
  });

  it("points to setup when there is no mailbox or list yet", async () => {
    mockWorkspace();
    listMailboxes.mockResolvedValue([]);
    listLeadLists.mockResolvedValue({ items: [], next_cursor: null });

    renderWithClient(<ApiEndpoints />);

    expect(await screen.findByRole("link", { name: "Connect a mailbox" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Create a list" })).toBeInTheDocument();
    expect(screen.getByText(/"mailbox_id": "<mailbox id>"/)).toBeInTheDocument();
  });

  it("copies the endpoint URL", async () => {
    mockWorkspace();
    listMailboxes.mockResolvedValue([]);
    listLeadLists.mockResolvedValue({ items: [], next_cursor: null });
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();

    renderWithClient(<ApiEndpoints />);
    await user.click(screen.getByRole("button", { name: "Copy endpoint URL" }));

    expect(writeText).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/workspaces\/ws-123\/integrations\/revenueos\/campaigns$/),
    );
    expect(await screen.findByRole("button", { name: "Copied endpoint URL" })).toBeInTheDocument();
  });

  it("is hidden from a Viewer and loads nothing", () => {
    mockWorkspace("VIEWER");

    const { container } = renderWithClient(<ApiEndpoints />);

    expect(container).toBeEmptyDOMElement();
    expect(listMailboxes).not.toHaveBeenCalled();
    expect(listLeadLists).not.toHaveBeenCalled();
  });
});
