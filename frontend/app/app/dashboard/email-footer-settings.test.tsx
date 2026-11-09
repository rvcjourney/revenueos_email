import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { updateWorkspace } = vi.hoisted(() => ({ updateWorkspace: vi.fn() }));
vi.mock("@/lib/workspaces-api", () => ({ updateWorkspace }));

import {
  EmailFooterSettings,
  readCompliance,
} from "@/app/app/dashboard/email-footer-settings";
import type { Workspace } from "@/types/domain";

function workspace(defaults: Record<string, unknown> = {}): Workspace {
  return {
    id: "ws-1",
    name: "Acme",
    status: "ACTIVE",
    defaults,
    version: 3,
    role_code: "OWNER",
  } as Workspace;
}

function renderIt(ws: Workspace, canManage = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EmailFooterSettings workspace={ws} canManage={canManage} />
    </QueryClientProvider>,
  );
}

describe("readCompliance", () => {
  it("reads the two fields and tolerates anything malformed", () => {
    expect(readCompliance({ compliance: { postal_address: "1 Main St", footer_text: "Bye" } })).toEqual({
      postal_address: "1 Main St",
      footer_text: "Bye",
    });
    for (const bad of [undefined, {}, { compliance: null }, { compliance: "x" }, { compliance: { postal_address: 5 } }]) {
      expect(readCompliance(bad as Record<string, unknown> | undefined)).toEqual({
        postal_address: "",
        footer_text: "",
      });
    }
  });
});

describe("EmailFooterSettings", () => {
  afterEach(() => vi.clearAllMocks());

  it("says the footer is optional when nothing is set", () => {
    renderIt(workspace());
    expect(screen.getByText(/emails end with just the unsubscribe link/i)).toBeInTheDocument();
  });

  it("shows the saved address", () => {
    renderIt(workspace({ compliance: { postal_address: "12 MG Road\nBengaluru" } }));
    expect(screen.getByText(/12 MG Road/)).toBeInTheDocument();
    expect(screen.queryByText(/emails end with just the unsubscribe link/i)).not.toBeInTheDocument();
  });

  it("saves the address and keeps every other workspace default", async () => {
    updateWorkspace.mockResolvedValue(workspace());
    const user = userEvent.setup();
    renderIt(workspace({ limits: { leads: 100 }, plan_name: "Pro" }));

    await user.click(screen.getByRole("button", { name: /edit email footer/i }));
    await user.type(screen.getByLabelText(/postal address/i), "12 MG Road, Bengaluru");
    await user.click(screen.getByRole("button", { name: /save footer/i }));

    await waitFor(() =>
      expect(updateWorkspace).toHaveBeenCalledWith("ws-1", {
        defaults: {
          limits: { leads: 100 },
          plan_name: "Pro",
          compliance: { postal_address: "12 MG Road, Bengaluru", footer_text: "" },
        },
        expected_version: 3,
      }),
    );
  });

  it("saves with an empty address", async () => {
    updateWorkspace.mockResolvedValue(workspace());
    const user = userEvent.setup();
    renderIt(workspace());

    await user.click(screen.getByRole("button", { name: /edit email footer/i }));
    await user.click(screen.getByRole("button", { name: /save footer/i }));

    await waitFor(() =>
      expect(updateWorkspace).toHaveBeenCalledWith("ws-1", {
        defaults: { compliance: { postal_address: "", footer_text: "" } },
        expected_version: 3,
      }),
    );
  });

  it("is read-only without permission", () => {
    renderIt(workspace({ compliance: { postal_address: "1 Main St" } }), false);
    expect(screen.getByText("1 Main St")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /edit email footer/i })).not.toBeInTheDocument();
  });
});
