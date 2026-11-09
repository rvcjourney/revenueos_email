import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}));

const { createImport, uploadImportFile } = vi.hoisted(() => ({
  createImport: vi.fn(),
  uploadImportFile: vi.fn(),
}));

vi.mock("@/lib/imports-api", () => ({ createImport, uploadImportFile }));

const { listLeadLists } = vi.hoisted(() => ({ listLeadLists: vi.fn() }));

vi.mock("@/lib/leads-api", () => ({ listLeadLists }));

const { useWorkspace } = vi.hoisted(() => ({ useWorkspace: vi.fn() }));

vi.mock("@/lib/workspace-context", () => ({ useWorkspace }));

import { NewImportPageClient } from "./new-import-page-client";

const HEADERS = ["Email Address", "First Name", "Phone Number", "Company Website", "Notes"];

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NewImportPageClient />
    </QueryClientProvider>,
  );
}

async function uploadCsv(user: ReturnType<typeof userEvent.setup>) {
  await user.upload(
    screen.getByLabelText(/upload a file/i),
    new File(["x"], "leads.csv", { type: "text/csv" }),
  );
  await user.click(screen.getByRole("button", { name: /upload & next/i }));
  await screen.findByText("Map Columns");
}

describe("NewImportPageClient", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  function arrange() {
    useWorkspace.mockReturnValue({ activeWorkspaceId: "ws-1" });
    listLeadLists.mockResolvedValue({ items: [], next_cursor: null });
    uploadImportFile.mockResolvedValue({
      storage_object_key: "workspace/ws-1/imports/a.csv",
      storage_object_version: "v1",
      storage_object_digest: "d".repeat(64),
      headers: HEADERS,
      sample_rows: [],
      detected_total_rows: 3,
      warnings: [],
    });
    createImport.mockResolvedValue({ id: "import-1" });
  }

  it("auto-maps headers, offers every lead field, and sends the mapping as field -> header", async () => {
    arrange();
    const user = userEvent.setup();
    renderPage();
    await uploadCsv(user);

    const selects = screen.getAllByRole("combobox");
    expect(selects).toHaveLength(HEADERS.length);
    expect(selects.map((select) => (select as HTMLSelectElement).value)).toEqual([
      "email",
      "first_name",
      "phone",
      "company_website",
      "",
    ]);
    const optionLabels = within(selects[0]).getAllByRole("option").map((o) => o.textContent);
    expect(optionLabels).toEqual(
      expect.arrayContaining(["Email", "Job title", "Phone", "Company founded year"]),
    );

    await user.selectOptions(selects[4], "department");
    await user.click(screen.getByRole("button", { name: /continue to confirm/i }));
    await user.click(await screen.findByRole("button", { name: /start import/i }));

    await waitFor(() =>
      expect(createImport).toHaveBeenCalledWith("ws-1", {
        storage_object_key: "workspace/ws-1/imports/a.csv",
        storage_object_version: "v1",
        storage_object_digest: "d".repeat(64),
        import_kind: "LEADS",
        mapping: {
          // The API contract is field -> CSV header.
          columns: {
            email: "Email Address",
            first_name: "First Name",
            phone: "Phone Number",
            company_website: "Company Website",
            department: "Notes",
          },
          source_filename: "leads.csv",
        },
        list_id: null,
      }),
    );
    expect(push).toHaveBeenCalledWith("/app/leads/imports/import-1");
  });

  it("creates the target list when the import starts, once, and hides the list picker", async () => {
    arrange();
    const createTargetList = vi.fn().mockResolvedValue("new-list-1");
    createImport.mockRejectedValueOnce(new Error("boom")).mockResolvedValue({ id: "import-1" });
    const user = userEvent.setup();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <NewImportPageClient createTargetList={createTargetList} onImported={vi.fn()} />
      </QueryClientProvider>,
    );

    expect(screen.queryByLabelText(/add to list/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Suppressions")).not.toBeInTheDocument();
    await uploadCsv(user);
    expect(createTargetList).not.toHaveBeenCalled(); // cancelling here leaves nothing behind

    await user.click(screen.getByRole("button", { name: /continue to confirm/i }));
    await user.click(await screen.findByRole("button", { name: /start import/i }));
    await waitFor(() => expect(createImport).toHaveBeenCalledTimes(1));
    // A failed start is retried without creating a second list.
    await user.click(await screen.findByRole("button", { name: /start import/i }));
    await waitFor(() => expect(createImport).toHaveBeenCalledTimes(2));

    expect(createTargetList).toHaveBeenCalledTimes(1);
    expect(createImport).toHaveBeenLastCalledWith(
      "ws-1",
      expect.objectContaining({ import_kind: "LEADS", list_id: "new-list-1" }),
    );
  });

  it("refuses to map two columns onto the same field", async () => {
    arrange();
    const user = userEvent.setup();
    renderPage();
    await uploadCsv(user);

    await user.selectOptions(screen.getAllByRole("combobox")[4], "phone");
    await user.click(screen.getByRole("button", { name: /continue to confirm/i }));

    expect(
      await screen.findByText(/each field can be mapped to one column only: phone/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Confirm Import")).not.toBeInTheDocument();
    expect(createImport).not.toHaveBeenCalled();
  });

  it("still requires an email column", async () => {
    arrange();
    const user = userEvent.setup();
    renderPage();
    await uploadCsv(user);

    await user.selectOptions(screen.getAllByRole("combobox")[0], "");
    await user.click(screen.getByRole("button", { name: /continue to confirm/i }));

    expect(await screen.findByText(/must map at least one column to 'email'/i)).toBeInTheDocument();
  });
});
