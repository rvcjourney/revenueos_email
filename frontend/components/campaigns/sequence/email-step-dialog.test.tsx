import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const campaignsApi = vi.hoisted(() => ({
  updateSequenceStep: vi.fn(),
  listSequencePreviewRecipients: vi.fn(),
  sendSequenceStepTestEmail: vi.fn(),
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
import type { CampaignMailbox, SequenceStep } from "@/types/domain";

import { EmailStepDialog } from "./email-step-dialog";

function makeStep(overrides: Partial<SequenceStep> = {}): SequenceStep {
  return {
    id: "e2",
    sequence_id: "seq-1",
    campaign_id: "camp-1",
    position: 3,
    kind: "EMAIL",
    email_subject: "Quick question, {{first_name|there}}",
    email_body_html: "<p>Hi {{first_name|there}},</p>",
    email_preheader: null,
    email_variable_schema: {},
    wait_duration_minutes: null,
    source_template_version_id: null,
    version: 4,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

const wait: SequenceStep = {
  ...makeStep(),
  id: "w1",
  position: 2,
  kind: "WAIT",
  email_subject: null,
  email_body_html: null,
  wait_duration_minutes: 2880,
  version: 2,
};

const mailboxes: CampaignMailbox[] = [
  {
    mailbox_id: "mb-1",
    provider: "GMAIL",
    email_address: "sales@acme.test",
    sender_display_name: "Sam Sales",
    connection_state: "CONNECTED",
    health_state: "HEALTHY",
    policy_state: "ENABLED",
    policy_reason: null,
    active: true,
    allocation_position: 1,
  },
];

type Overrides = Partial<React.ComponentProps<typeof EmailStepDialog>>;

function setup(overrides: Overrides = {}) {
  const props: React.ComponentProps<typeof EmailStepDialog> = {
    open: true,
    workspaceId: "ws-1",
    campaignId: "camp-1",
    step: makeStep(),
    stepNumber: 2,
    day: 3,
    previousEmailDay: 1,
    precedingWait: wait,
    readOnly: false,
    canTestSend: true,
    mailboxes,
    onClose: vi.fn(),
    onSaved: vi.fn(),
    onRefetch: vi.fn().mockResolvedValue({ step: makeStep({ version: 5 }), wait }),
    onLocked: vi.fn(),
    ...overrides,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={client}>
      <EmailStepDialog {...props} />
    </QueryClientProvider>,
  );
  return { props, ...utils };
}

// Body edits go through the HTML source textarea: it is a plain input, so it is
// reliable in jsdom, and it exercises the same value/dirty path as the visual editor.
async function editBodySource(user: ReturnType<typeof userEvent.setup>, html: string) {
  await user.click(screen.getByRole("button", { name: /edit html source/i }));
  const textarea = await screen.findByLabelText("HTML source");
  await user.clear(textarea);
  await user.click(textarea);
  await user.paste(html);
}

const noAudience = { source: "NONE", items: [], total: 0, next_cursor: null };

beforeEach(() => {
  campaignsApi.listSequencePreviewRecipients.mockResolvedValue(noAudience);
  templatesApi.previewTemplate.mockImplementation(
    async (_ws: string, input: { subject: string; body_html: string; preheader?: string | null }) => ({
      subject: input.subject.replace("{{first_name|there}}", "Alex"),
      body_html: input.body_html.replace("{{first_name|there}}", "Alex"),
      preheader: input.preheader ?? "",
      detected_variables: ["first_name"],
      missing_variables: [],
    }),
  );
  templatesApi.listTemplates.mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("EmailStepDialog", () => {
  it("loads the saved step into the editor fields", async () => {
    setup();
    expect(await screen.findByLabelText("Subject", { selector: "input" })).toHaveValue(
      "Quick question, {{first_name|there}}",
    );
    expect(screen.getByRole("dialog", { name: "Step 2" })).toBeInTheDocument();
    await waitFor(() => {
      expect(document.querySelector(".ProseMirror")).toHaveTextContent("Hi {{first_name|there}},");
    });
    expect(screen.getByLabelText(/start this step on day/i)).toHaveValue(3);
  });

  it("shows a live preview with From, To and the rendered subject", async () => {
    setup();
    expect(await screen.findByText(/Subject:/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("Quick question, Alex", { exact: false })).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("From")).toHaveDisplayValue("Sam Sales <sales@acme.test>");
    expect(
      screen.getByText(
        (_, el) =>
          el?.tagName === "P" && /^To:\s*Alex Taylor <alex.taylor@acme.example.com>/.test(el.textContent ?? ""),
      ),
    ).toBeInTheDocument();
    expect(screen.getByTitle("Email preview")).toHaveAttribute("sandbox", "");
  });

  it("re-renders the preview as the subject and pre-header change", async () => {
    const user = userEvent.setup();
    setup();
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.type(subject, "Brand new subject");
    await user.click(screen.getByRole("button", { name: /set pre-header/i }));
    await user.type(screen.getByLabelText("Pre-header"), "Short teaser");

    await waitFor(() =>
      expect(templatesApi.previewTemplate).toHaveBeenLastCalledWith(
        "ws-1",
        expect.objectContaining({
          subject: "Brand new subject",
          preheader: "Short teaser",
        }),
      ),
    );
    expect(await screen.findByText("Brand new subject")).toBeInTheDocument();
    expect(screen.getByText(/Pre-header:/)).toBeInTheDocument();
  });

  it("surfaces preview problems such as an unknown variable", async () => {
    templatesApi.previewTemplate.mockRejectedValue(
      new ApiError("Unknown variable 'nope' in body", 422, "validation_error", null),
    );
    setup();
    expect(await screen.findByText(/Unknown variable 'nope' in body/)).toBeInTheDocument();
  });

  it("inserts a variable into the subject at the caret", async () => {
    const user = userEvent.setup();
    setup({ step: makeStep({ email_subject: "Hello " }) });
    const subject = (await screen.findByLabelText("Subject", {
      selector: "input",
    })) as HTMLInputElement;
    await user.click(subject);
    await user.keyboard("{End}");

    await user.click(screen.getByRole("button", { name: /insert variable in subject/i }));
    await user.click(await screen.findByRole("button", { name: "Company" }));

    expect(subject).toHaveValue("Hello {{company}}");
  });

  it("keeps Save disabled until something changes, then saves only through the API with the step version", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep.mockResolvedValue(
      makeStep({ version: 5, email_subject: "Updated subject" }),
    );
    const save = await screen.findByRole("button", { name: /^save$/i });
    expect(save).toBeDisabled();

    const subject = screen.getByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.type(subject, "Updated subject");
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() =>
      expect(campaignsApi.updateSequenceStep).toHaveBeenCalledWith("ws-1", "camp-1", "e2", {
        expected_version: 4,
        email_subject: "Updated subject",
        email_body_html: "<p>Hi {{first_name|there}},</p>",
        email_preheader: "",
      }),
    );
    // Day is unchanged, so the preceding wait is not touched.
    expect(campaignsApi.updateSequenceStep).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(props.onClose).toHaveBeenCalled());
    expect(props.onSaved).toHaveBeenCalledWith({
      step: expect.objectContaining({ email_subject: "Updated subject" }),
    });
  });

  it("does not save an empty subject", async () => {
    const user = userEvent.setup();
    setup();
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/add a subject before saving/i)).toBeInTheDocument();
    expect(campaignsApi.updateSequenceStep).not.toHaveBeenCalled();
  });

  it("warns about an empty body but lets the draft be saved", async () => {
    const user = userEvent.setup();
    setup();
    campaignsApi.updateSequenceStep.mockResolvedValue(makeStep({ email_body_html: "" }));
    await screen.findByLabelText("Subject", { selector: "input" });
    await editBodySource(user, "<p></p>");

    expect((await screen.findAllByText(/body is empty/i)).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(campaignsApi.updateSequenceStep).toHaveBeenCalled());
  });

  it("asks before discarding unsaved changes and stays open when declined", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    vi.mocked(window.confirm).mockReturnValue(false);
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.type(subject, " edited");

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/discard/i));
    expect(props.onClose).not.toHaveBeenCalled();

    vi.mocked(window.confirm).mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(props.onClose).toHaveBeenCalled();
  });

  it("closes without a prompt when nothing changed", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    await screen.findByRole("button", { name: /^save$/i });
    await user.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(window.confirm).not.toHaveBeenCalled();
    expect(props.onClose).toHaveBeenCalled();
  });

  it("registers a beforeunload guard only while there are unsaved changes", async () => {
    const user = userEvent.setup();
    setup();
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);

    await user.type(subject, "x");
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("changes the wait before this step when the start day changes", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep.mockResolvedValue({ ...wait, version: 3, wait_duration_minutes: 7200 });
    const day = await screen.findByLabelText(/start this step on day/i);
    await user.clear(day);
    await user.type(day, "6");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    // Only the wait changes: Day 6 is 5 days after Day 1.
    await waitFor(() =>
      expect(campaignsApi.updateSequenceStep).toHaveBeenCalledWith("ws-1", "camp-1", "w1", {
        expected_version: 2,
        wait_duration_minutes: 7200,
      }),
    );
    expect(campaignsApi.updateSequenceStep).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(props.onClose).toHaveBeenCalled());
  });

  it("rejects a start day that is not after the previous email", async () => {
    const user = userEvent.setup();
    setup();
    const day = await screen.findByLabelText(/start this step on day/i);
    await user.clear(day);
    await user.type(day, "1");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findAllByText(/choose a whole day after day 1/i)).not.toHaveLength(0);
    expect(campaignsApi.updateSequenceStep).not.toHaveBeenCalled();
  });

  it("locks the start day for the first step", async () => {
    setup({ precedingWait: null, previousEmailDay: null, day: 1, stepNumber: 1 });
    expect(await screen.findByLabelText(/start this step on day/i)).toBeDisabled();
    expect(screen.getByText(/scheduled as soon as the campaign starts/i)).toBeInTheDocument();
  });

  it("reports a partial save when the email saves but the timing does not", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep
      .mockResolvedValueOnce(makeStep({ version: 5, email_subject: "New" }))
      .mockRejectedValueOnce(new ApiError("Wait is out of range", 422, "validation_error", null));
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.type(subject, "New");
    const day = screen.getByLabelText(/start this step on day/i);
    await user.clear(day);
    await user.type(day, "5");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(
      await screen.findByText(/your email was saved, but the timing couldn.t be updated/i),
    ).toBeInTheDocument();
    expect(props.onClose).not.toHaveBeenCalled();
    expect(props.onSaved).toHaveBeenCalledWith({
      step: expect.objectContaining({ email_subject: "New" }),
    });
  });

  it("handles a stale version: keep my edits retries against the newer version", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep
      .mockRejectedValueOnce(new ApiError("Step was modified", 409, "conflict", null))
      .mockResolvedValueOnce(makeStep({ version: 6, email_subject: "Mine" }));
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.type(subject, "Mine");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/changed somewhere else/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /keep my edits/i }));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(campaignsApi.updateSequenceStep).toHaveBeenLastCalledWith(
        "ws-1",
        "camp-1",
        "e2",
        expect.objectContaining({ expected_version: 5, email_subject: "Mine" }),
      ),
    );
    await waitFor(() => expect(props.onClose).toHaveBeenCalled());
  });

  it("handles a stale version: reload latest discards local edits", async () => {
    const user = userEvent.setup();
    const { props } = setup({
      onRefetch: vi
        .fn()
        .mockResolvedValue({ step: makeStep({ version: 5, email_subject: "Server copy" }), wait }),
    });
    campaignsApi.updateSequenceStep.mockRejectedValueOnce(
      new ApiError("Step was modified", 409, "conflict", null),
    );
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.clear(subject);
    await user.type(subject, "Mine");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await user.click(await screen.findByRole("button", { name: /reload latest/i }));

    await waitFor(() => expect(subject).toHaveValue("Server copy"));
    expect(props.onRefetch).toHaveBeenCalled();
  });

  it("tells the user when the campaign stopped being a draft", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep.mockRejectedValue(
      new ApiError("Sequence can only be edited while the campaign is DRAFT", 409, "state_conflict", null),
    );
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.type(subject, "!");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/no longer a draft/i)).toBeInTheDocument();
    expect(props.onLocked).toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
    // The user's text is still there to copy.
    expect(subject).toHaveValue("Quick question, {{first_name|there}}!");
  });

  it("keeps edits and shows the error when a save fails", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    campaignsApi.updateSequenceStep.mockRejectedValue(new TypeError("Failed to fetch"));
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.type(subject, "!");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/couldn.t save your changes/i)).toBeInTheDocument();
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  it("shows server validation errors, such as an unknown variable, on save", async () => {
    const user = userEvent.setup();
    setup();
    campaignsApi.updateSequenceStep.mockRejectedValue(
      new ApiError("Unknown variable 'bogus' in body", 422, "validation_error", null),
    );
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.type(subject, "!");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findAllByText(/Unknown variable 'bogus' in body/)).not.toHaveLength(0);
  });

  it("applies a chosen template and saves it as the step's source", async () => {
    const user = userEvent.setup();
    templatesApi.listTemplates.mockResolvedValue({
      items: [{ id: "t1", name: "Intro", subject: "Intro subject", current_version_id: "tv-9" }],
      next_cursor: null,
    });
    templatesApi.getTemplate.mockResolvedValue({
      id: "t1",
      name: "Intro",
      archived_at: null,
      current_version_id: "tv-9",
      subject: "Intro for {{first_name}}",
      body_html: "<p>Template body {{company}}</p>",
      preheader: "Template teaser",
    });
    campaignsApi.updateSequenceStep.mockResolvedValue(makeStep({ version: 5 }));
    const { props } = setup();
    await screen.findByLabelText("Subject", { selector: "input" });

    await user.click(screen.getByRole("button", { name: /^template$/i }));
    await user.click(await screen.findByRole("button", { name: /intro/i }));

    // Existing content triggers a replace confirmation.
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/intro/i));
    expect(screen.getByLabelText("Subject", { selector: "input" })).toHaveValue(
      "Intro for {{first_name}}",
    );
    expect(await screen.findByLabelText("Pre-header")).toHaveValue("Template teaser");
    await waitFor(() =>
      expect(document.querySelector(".ProseMirror")).toHaveTextContent("Template body {{company}}"),
    );
    expect(screen.getByText(/started from the "Intro" template/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(campaignsApi.updateSequenceStep).toHaveBeenCalledWith(
        "ws-1",
        "camp-1",
        "e2",
        expect.objectContaining({
          email_subject: "Intro for {{first_name}}",
          email_preheader: "Template teaser",
          source_template_version_id: "tv-9",
        }),
      ),
    );
    expect(props.onClose).toHaveBeenCalled();
  });

  it("keeps the current content when the template replace prompt is declined", async () => {
    const user = userEvent.setup();
    templatesApi.listTemplates.mockResolvedValue({
      items: [{ id: "t1", name: "Intro", subject: "S", current_version_id: "tv-9" }],
      next_cursor: null,
    });
    templatesApi.getTemplate.mockResolvedValue({
      id: "t1",
      name: "Intro",
      archived_at: null,
      current_version_id: "tv-9",
      subject: "Other",
      body_html: "<p>Other</p>",
      preheader: null,
    });
    setup();
    vi.mocked(window.confirm).mockReturnValue(false);
    await screen.findByLabelText("Subject", { selector: "input" });
    await user.click(screen.getByRole("button", { name: /^template$/i }));
    await user.click(await screen.findByRole("button", { name: /intro/i }));

    expect(screen.getByLabelText("Subject", { selector: "input" })).toHaveValue(
      "Quick question, {{first_name|there}}",
    );
  });

  it("explains when a template was deleted after the list loaded", async () => {
    const user = userEvent.setup();
    templatesApi.listTemplates.mockResolvedValue({
      items: [{ id: "gone", name: "Old", subject: "S", current_version_id: "tv" }],
      next_cursor: null,
    });
    templatesApi.getTemplate.mockRejectedValue(new ApiError("Not found", 404, "not_found", null));
    setup();
    await screen.findByLabelText("Subject", { selector: "input" });
    await user.click(screen.getByRole("button", { name: /^template$/i }));
    await user.click(await screen.findByRole("button", { name: /^old/i }));

    expect(await screen.findByText(/no longer exists/i)).toBeInTheDocument();
  });

  it("does not offer archived templates", async () => {
    const user = userEvent.setup();
    templatesApi.listTemplates.mockResolvedValue({ items: [], next_cursor: null });
    setup();
    await screen.findByLabelText("Subject", { selector: "input" });
    await user.click(screen.getByRole("button", { name: /^template$/i }));

    await screen.findByText(/you have no templates yet/i);
    expect(templatesApi.listTemplates).toHaveBeenCalledWith(
      "ws-1",
      expect.objectContaining({ status: "ACTIVE" }),
    );
  });

  it("saves the current content as a template without changing the step", async () => {
    const user = userEvent.setup();
    templatesApi.createTemplate.mockResolvedValue({ id: "new" });
    setup();
    await screen.findByLabelText("Subject", { selector: "input" });

    await user.click(screen.getByRole("button", { name: /save as template/i }));
    await user.type(await screen.findByLabelText(/template name/i), "My template");
    await user.click(screen.getByRole("button", { name: /save template/i }));

    await waitFor(() =>
      expect(templatesApi.createTemplate).toHaveBeenCalledWith("ws-1", {
        name: "My template",
        subject: "Quick question, {{first_name|there}}",
        body_html: "<p>Hi {{first_name|there}},</p>",
        preheader: null,
      }),
    );
    expect(await screen.findByText(/saved as .*my template/i)).toBeInTheDocument();
    expect(campaignsApi.updateSequenceStep).not.toHaveBeenCalled();
  });

  it("lets you switch between Content Guide and Email Preview", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByText(/Subject:/);
    await user.click(screen.getByRole("tab", { name: /content guide/i }));
    expect(await screen.findByText(/writing tips/i)).toBeInTheDocument();
    expect(await screen.findByText("{{first_name}}")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /email preview/i }));
    expect(await screen.findByText(/Subject:/)).toBeInTheDocument();
  });

  it("warns in the guide when a variable has no value and no fallback", async () => {
    const user = userEvent.setup();
    templatesApi.previewTemplate.mockResolvedValue({
      subject: "Hi",
      body_html: "<p>Hi</p>",
      preheader: "",
      detected_variables: ["company"],
      missing_variables: ["company"],
    });
    setup();
    await user.click(await screen.findByRole("tab", { name: /content guide/i }));
    expect(await screen.findByText(/has no value for \{\{company\}\} and no fallback/i)).toBeInTheDocument();
  });

  it("asks to assign a sender when the campaign has no mailbox", async () => {
    setup({ mailboxes: [] });
    const link = await screen.findByRole("link", { name: /assign a sender/i });
    expect(link).toHaveAttribute("href", "/app/campaigns/camp-1/senders");
  });

  it("is read-only when editing is not allowed", async () => {
    setup({ readOnly: true });
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    expect(subject).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save as template/i })).not.toBeInTheDocument();
    expect(screen.getByText(/can.t be edited now/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^close$/i })).toHaveLength(2);
    expect(screen.getByLabelText("Bold")).toBeDisabled();
  });

  it("opens HTML the visual editor can't keep in source mode with an explanation", async () => {
    setup({
      step: makeStep({ email_body_html: "<table><tr><td>cell</td></tr></table>" }),
    });
    expect(await screen.findByLabelText("HTML source")).toHaveValue(
      "<table><tr><td>cell</td></tr></table>",
    );
    expect(screen.getByText(/opened as source/i)).toBeInTheDocument();
  });

  it("closes on Escape through the same unsaved-changes guard", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    await user.type(subject, "!");
    vi.mocked(window.confirm).mockReturnValue(false);
    await user.keyboard("{Escape}");
    expect(window.confirm).toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("traps focus inside the dialog", async () => {
    const user = userEvent.setup();
    setup();
    await screen.findByLabelText("Subject", { selector: "input" });
    const dialog = screen.getByRole("dialog", { name: "Step 2" });
    for (let i = 0; i < 60; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    // Sanity: the close button is reachable from within the dialog.
    expect(within(dialog).getByRole("button", { name: "Close" })).toBeInTheDocument();
  });
});

describe("EmailStepDialog: send in same thread", () => {
  const checkbox = () => screen.queryByRole("checkbox", { name: /send in same thread/i });

  it("is not offered on the first email", async () => {
    setup({ stepNumber: 1, day: 1, previousEmailDay: null, precedingWait: null });
    await screen.findByLabelText("Subject", { selector: "input" });
    expect(checkbox()).not.toBeInTheDocument();
  });

  it("loads the saved value and shows the reply subject instead of the subject input", async () => {
    setup({ step: makeStep({ email_same_thread: true }), replySubject: "Re: Hello" });
    expect(await screen.findByRole("checkbox", { name: /send in same thread/i })).toBeChecked();
    expect(screen.queryByLabelText("Subject", { selector: "input" })).not.toBeInTheDocument();
    expect(screen.getByText("Subject: Re: Hello")).toBeInTheDocument();
  });

  it("does not require a subject when checked, and saves the flag", async () => {
    const user = userEvent.setup();
    setup();
    campaignsApi.updateSequenceStep.mockResolvedValue(makeStep({ email_same_thread: true }));
    const subject = await screen.findByLabelText("Subject", { selector: "input" });
    expect(checkbox()).not.toBeChecked();
    await user.clear(subject);
    await user.click(checkbox() as HTMLElement);
    expect(screen.getByText("Subject: Re: (subject of the previous email)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(campaignsApi.updateSequenceStep).toHaveBeenCalledWith("ws-1", "camp-1", "e2", {
        expected_version: 4,
        email_body_html: "<p>Hi {{first_name|there}},</p>",
        email_preheader: "",
        email_same_thread: true,
      }),
    );
  });

  it("requires a subject again when unchecked", async () => {
    const user = userEvent.setup();
    setup({ step: makeStep({ email_same_thread: true, email_subject: null }) });
    await user.click((await screen.findByRole("checkbox", { name: /send in same thread/i })));
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText(/add a subject before saving/i)).toBeInTheDocument();
    expect(campaignsApi.updateSequenceStep).not.toHaveBeenCalled();
  });

  it("explains a refused threading change on a step leads already reached", async () => {
    const user = userEvent.setup();
    setup();
    campaignsApi.updateSequenceStep.mockRejectedValue(
      new ApiError("server text", 409, "step_locked", null),
    );
    await user.click((await screen.findByRole("checkbox", { name: /send in same thread/i })));
    expect(screen.getByText(/test sends use the saved/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(
      await screen.findByText(
        "Leads have already reached this step, so its threading can't change.",
      ),
    ).toBeInTheDocument();
  });
});
