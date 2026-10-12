import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const campaignsApi = vi.hoisted(() => ({
  updateSequenceStep: vi.fn(),
  listSequencePreviewRecipients: vi.fn(),
  sendSequenceStepTestEmail: vi.fn(),
  uploadStepAttachment: vi.fn(),
  deleteStepAttachment: vi.fn(),
  getStepAttachmentUrl: vi.fn(),
}));
vi.mock("@/lib/campaigns-api", () => campaignsApi);

const templatesApi = vi.hoisted(() => ({
  previewTemplate: vi.fn(),
  listTemplates: vi.fn(),
  getTemplate: vi.fn(),
  createTemplate: vi.fn(),
}));
vi.mock("@/lib/templates-api", () => templatesApi);

import { ApiError } from "@/lib/api-client";
import type { CampaignMailbox, SequenceStep, StepAttachment } from "@/types/domain";

import { EmailStepDialog } from "./email-step-dialog";

const PDF: StepAttachment = {
  id: "att-pdf",
  step_id: "e1",
  filename: "proposal.pdf",
  content_type: "application/pdf",
  size_bytes: 2048,
  disposition: "ATTACHMENT",
  content_id: "pdftoken1",
  created_at: "",
};

const IMAGE: StepAttachment = {
  id: "att-img",
  step_id: "e1",
  filename: "logo.png",
  content_type: "image/png",
  size_bytes: 4096,
  disposition: "INLINE",
  content_id: "imgtoken1",
  created_at: "",
};

function makeStep(overrides: Partial<SequenceStep> = {}): SequenceStep {
  return {
    id: "e1",
    sequence_id: "seq-1",
    campaign_id: "camp-1",
    position: 1,
    kind: "EMAIL",
    email_subject: "Hello",
    email_body_html: "<p>Hi there</p>",
    email_preheader: null,
    attachments: [],
    email_variable_schema: {},
    wait_duration_minutes: null,
    source_template_version_id: null,
    version: 3,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

const mailboxes: CampaignMailbox[] = [
  {
    mailbox_id: "mb-1",
    provider: "GMAIL",
    email_address: "sales@acme.test",
    sender_display_name: "Sam",
    connection_state: "CONNECTED",
    health_state: "HEALTHY",
    policy_state: "ENABLED",
    policy_reason: null,
    active: true,
    allocation_position: 1,
  },
];

function setup(overrides: Partial<React.ComponentProps<typeof EmailStepDialog>> = {}) {
  const props: React.ComponentProps<typeof EmailStepDialog> = {
    open: true,
    workspaceId: "ws-1",
    campaignId: "camp-1",
    step: makeStep(),
    stepNumber: 1,
    day: 1,
    previousEmailDay: null,
    precedingWait: null,
    readOnly: false,
    canTestSend: true,
    mailboxes,
    onClose: vi.fn(),
    onSaved: vi.fn(),
    onRefetch: vi.fn().mockResolvedValue({ step: makeStep(), wait: null }),
    onLocked: vi.fn(),
    ...overrides,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EmailStepDialog {...props} />
    </QueryClientProvider>,
  );
  return props;
}

function file(name: string, size = 10, type = "application/octet-stream") {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
}

async function chooseFile(user: ReturnType<typeof userEvent.setup>, label: RegExp, f: File) {
  const input = await screen.findByLabelText(label, { selector: "input" });
  await user.upload(input, f);
}

beforeEach(() => {
  templatesApi.previewTemplate.mockImplementation(
    async (_ws: string, input: { subject: string; body_html: string }) => ({
      subject: input.subject,
      body_html: input.body_html,
      preheader: "",
      detected_variables: [],
      missing_variables: [],
    }),
  );
  templatesApi.listTemplates.mockResolvedValue({ items: [], next_cursor: null });
  campaignsApi.listSequencePreviewRecipients.mockResolvedValue({
    source: "NONE",
    items: [],
    total: 0,
    next_cursor: null,
  });
  campaignsApi.getStepAttachmentUrl.mockImplementation(
    async (_w: string, _c: string, _s: string, id: string) => ({
      url: `https://files.example/${id}?sig=1`,
      expires_in: 1800,
    }),
  );
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("attachments", () => {
  it("lists the files already on the step with their size", async () => {
    setup({ step: makeStep({ attachments: [PDF] }) });
    const bar = await screen.findByRole("region", { name: "Attachments" });
    expect(within(bar).getByText("proposal.pdf")).toBeInTheDocument();
    expect(within(bar).getAllByText(/2\.0 KB/).length).toBeGreaterThan(0);
    expect(within(bar).getByText(/1 of 5 attachments/)).toBeInTheDocument();
  });

  it("shows nothing when there are no files", async () => {
    setup();
    await screen.findByLabelText("Subject", { selector: "input" });
    expect(screen.queryByRole("region", { name: "Attachments" })).not.toBeInTheDocument();
  });

  it("uploads an attachment and lists it without saving the step", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockResolvedValue(PDF);
    setup();
    const upload = file("proposal.pdf", 2048, "application/pdf");
    await chooseFile(user, /choose a file to attach/i, upload);

    await waitFor(() =>
      expect(campaignsApi.uploadStepAttachment).toHaveBeenCalledWith(
        "ws-1",
        "camp-1",
        "e1",
        upload,
        "ATTACHMENT",
      ),
    );
    expect(await screen.findByText("proposal.pdf")).toBeInTheDocument();
    // Files are stored immediately; the text draft is untouched and unsaved.
    expect(campaignsApi.updateSequenceStep).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it.each([
    ["big.pdf", 3_000_000, /at most 2\.5 MB/],
    ["malware.exe", 100, /isn't allowed/i],
    ["empty.pdf", 0, /empty/i],
  ])("rejects %s before uploading", async (name, size, message) => {
    const user = userEvent.setup({ applyAccept: false });
    setup();
    await chooseFile(user, /choose a file to attach/i, file(name, size));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(campaignsApi.uploadStepAttachment).not.toHaveBeenCalled();
  });

  it("stops at the attachment limit", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const five = Array.from({ length: 5 }, (_, i) => ({
      ...PDF,
      id: `p${i}`,
      filename: `f${i}.pdf`,
      size_bytes: 100,
    }));
    setup({ step: makeStep({ attachments: five }) });
    await chooseFile(user, /choose a file to attach/i, file("sixth.pdf", 10));
    expect(await screen.findByText(/at most 5 attachments/i)).toBeInTheDocument();
    expect(campaignsApi.uploadStepAttachment).not.toHaveBeenCalled();
  });

  it("shows the server's reason when an upload is refused", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockRejectedValue(
      new ApiError("The file contents don't match its type", 422, "attachment_type_mismatch", null),
    );
    setup();
    await chooseFile(user, /choose a file to attach/i, file("fake.pdf", 100));
    expect(await screen.findByText(/contents don't match its type/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Attachments" })).not.toBeInTheDocument();
  });

  it("explains a network failure during upload", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockRejectedValue(new TypeError("Failed to fetch"));
    setup();
    await chooseFile(user, /choose a file to attach/i, file("a.pdf", 100));
    expect(await screen.findByText(/couldn.t save your changes/i)).toBeInTheDocument();
  });

  it("locks the editor when the campaign stopped being a draft", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockRejectedValue(
      new ApiError(
        "Attachments can only be changed while the campaign is DRAFT",
        409,
        "state_conflict",
        null,
      ),
    );
    const props = setup();
    await chooseFile(user, /choose a file to attach/i, file("a.pdf", 100));
    expect(await screen.findByText(/no longer a draft/i)).toBeInTheDocument();
    expect(props.onLocked).toHaveBeenCalled();
  });

  it("removes an attachment on the server and from the list", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.deleteStepAttachment.mockResolvedValue(undefined);
    setup({ step: makeStep({ attachments: [PDF] }) });
    await user.click(await screen.findByRole("button", { name: /remove proposal\.pdf/i }));

    await waitFor(() =>
      expect(campaignsApi.deleteStepAttachment).toHaveBeenCalledWith(
        "ws-1",
        "camp-1",
        "e1",
        "att-pdf",
      ),
    );
    await waitFor(() => expect(screen.queryByText("proposal.pdf")).not.toBeInTheDocument());
    // A plain attachment isn't in the body, so nothing to confirm or edit there.
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("keeps the file if removing it fails", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.deleteStepAttachment.mockRejectedValue(
      new ApiError("Attachment not found", 404, "not_found", null),
    );
    setup({ step: makeStep({ attachments: [PDF] }) });
    await user.click(await screen.findByRole("button", { name: /remove proposal\.pdf/i }));
    expect(await screen.findByText(/attachment not found/i)).toBeInTheDocument();
    expect(screen.getByText("proposal.pdf")).toBeInTheDocument();
  });

  it("has no upload or remove controls in read-only mode", async () => {
    setup({ readOnly: true, step: makeStep({ attachments: [PDF] }) });
    expect(await screen.findByText("proposal.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /remove proposal/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /attach a file/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /insert an image/i })).not.toBeInTheDocument();
  });
});

describe("inline images", () => {
  const withImage = () =>
    makeStep({
      attachments: [IMAGE],
      email_body_html: `<p>Hi <img src="cid:${IMAGE.content_id}" alt="logo"></p>`,
    });

  it("shows an existing image in the editor using a fetched URL, keeping cid in the value", async () => {
    setup({ step: withImage() });
    await waitFor(() => {
      const img = document.querySelector(".ProseMirror img");
      expect(img?.getAttribute("src")).toBe("https://files.example/att-img?sig=1");
    });
    expect(campaignsApi.getStepAttachmentUrl).toHaveBeenCalledWith(
      "ws-1",
      "camp-1",
      "e1",
      "att-img",
    );
    // Source view shows what is stored: the cid reference, not the temporary URL.
    await userEvent
      .setup({ applyAccept: false })
      .click(screen.getByRole("button", { name: /edit html source/i }));
    expect(await screen.findByLabelText("HTML source")).toHaveValue(
      `<p>Hi <img src="cid:${IMAGE.content_id}" alt="logo"></p>`,
    );
  });

  it("does not mark the step dirty just because an image URL arrived", async () => {
    setup({ step: withImage() });
    await waitFor(() =>
      expect(document.querySelector(".ProseMirror img")?.getAttribute("src")).toContain(
        "files.example",
      ),
    );
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("uploads an image, inserts it at the cursor and saves a cid reference", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockResolvedValue(IMAGE);
    campaignsApi.updateSequenceStep.mockResolvedValue(
      makeStep({
        version: 4,
        attachments: [IMAGE],
        email_body_html: '<p>Hi there<img src="cid:imgtoken1" alt="logo.png"></p>',
      }),
    );
    setup();
    await chooseFile(user, /choose an image to insert/i, file("logo.png", 4096, "image/png"));

    await waitFor(() =>
      expect(campaignsApi.uploadStepAttachment).toHaveBeenCalledWith(
        "ws-1",
        "camp-1",
        "e1",
        expect.any(File),
        "INLINE",
      ),
    );
    await waitFor(() =>
      expect(document.querySelector(".ProseMirror img")?.getAttribute("src")).toContain(
        "files.example",
      ),
    );
    // The body changed, so the step can now be saved, and it references the file by cid.
    const save = screen.getByRole("button", { name: /^save$/i });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(campaignsApi.updateSequenceStep).toHaveBeenCalled());
    const body = campaignsApi.updateSequenceStep.mock.calls[0][3].email_body_html as string;
    expect(body).toContain(`src="cid:${IMAGE.content_id}"`);
    expect(body).not.toContain("files.example");
    expect(body).not.toContain("data-cid");
  });

  it("only accepts image files for the image button", async () => {
    const user = userEvent.setup({ applyAccept: false });
    setup();
    await chooseFile(user, /choose an image to insert/i, file("notes.pdf", 100));
    expect(await screen.findByText(/PNG, JPG, GIF or WebP/)).toBeInTheDocument();
    expect(campaignsApi.uploadStepAttachment).not.toHaveBeenCalled();
  });

  it("inserts an image reference into the HTML source at the caret", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.uploadStepAttachment.mockResolvedValue(IMAGE);
    setup();
    await user.click(await screen.findByRole("button", { name: /edit html source/i }));
    const textarea = await screen.findByLabelText("HTML source");
    await user.click(textarea);
    await user.keyboard("{End}");
    await chooseFile(user, /choose an image to insert/i, file("logo.png", 100, "image/png"));
    await waitFor(() =>
      expect(screen.getByLabelText("HTML source")).toHaveValue(
        `<p>Hi there</p><img src="cid:${IMAGE.content_id}" alt="logo.png" width="240">`,
      ),
    );
  });

  it("removes an inline image from the body when its file is removed", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.deleteStepAttachment.mockResolvedValue(undefined);
    setup({ step: withImage() });
    await user.click(await screen.findByRole("button", { name: /remove logo\.png/i }));

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringMatching(/removed from the email body/i),
    );
    await waitFor(() => expect(document.querySelector(".ProseMirror img")).toBeNull());
    expect(campaignsApi.deleteStepAttachment).toHaveBeenCalledWith(
      "ws-1",
      "camp-1",
      "e1",
      "att-img",
    );
    // The body changed, so the removal still needs saving.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  it("keeps the image if the removal is declined", async () => {
    const user = userEvent.setup({ applyAccept: false });
    vi.mocked(window.confirm).mockReturnValue(false);
    setup({ step: withImage() });
    await user.click(await screen.findByRole("button", { name: /remove logo\.png/i }));
    expect(campaignsApi.deleteStepAttachment).not.toHaveBeenCalled();
    expect(screen.getByText("logo.png")).toBeInTheDocument();
  });

  it("shows the image in the preview instead of a broken cid link", async () => {
    setup({ step: withImage() });
    await waitFor(() => {
      const frame = screen.getByTitle("Email preview") as HTMLIFrameElement;
      expect(frame.getAttribute("srcdoc")).toContain("https://files.example/att-img?sig=1");
      expect(frame.getAttribute("srcdoc")).not.toContain("cid:imgtoken1");
    });
  });

  it("cannot save a template that would point at this step's files", async () => {
    setup({ step: withImage() });
    const button = await screen.findByRole("button", { name: /save as template/i });
    expect(button).toBeDisabled();
    expect(button.parentElement).toHaveAttribute(
      "title",
      expect.stringMatching(/can't include uploaded images/i),
    );
  });

  it("still sends the test email with files (the server attaches them)", async () => {
    const user = userEvent.setup({ applyAccept: false });
    campaignsApi.sendSequenceStepTestEmail.mockResolvedValue({
      message_id: "m",
      status: "SENT",
      recipient_email: "qa@example.com",
    });
    setup({ step: makeStep({ attachments: [PDF] }) });
    await user.click(await screen.findByRole("button", { name: /send test email/i }));
    await user.type(screen.getByLabelText(/^send to/i), "qa@example.com");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /^send test$/i }));
    await waitFor(() => expect(campaignsApi.sendSequenceStepTestEmail).toHaveBeenCalled());
    fireEvent.blur(document.body);
  });
});
