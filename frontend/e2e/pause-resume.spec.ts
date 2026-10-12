import { expect, test, type BrowserContext, type Page } from "@playwright/test";

// Drives the real screens through pause -> edit while paused -> change the schedule -> add
// leads -> resume against the stateful mock in e2e/mock-api. It proves the screens present
// and use the contract (locks, counts, blocked resume, retry); the backend's own rules are
// proven by the real-PostgreSQL tests. Screenshots land in e2e/screenshots (git-ignored).

const MOCK = `http://127.0.0.1:${process.env.MOCK_API_PORT ?? 4010}`;
const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const CAMPAIGN = "33333333-3333-4333-8333-333333333333";
const STEP1 = "a0000000-0000-4000-8000-000000000001";
const STEP2 = "a0000000-0000-4000-8000-000000000003";

function base64Url(value: string): string {
  return Buffer.from(value, "utf8").toString("base64url");
}

/** A signed-in browser: the Supabase session cookie the app reads, valid for the mock. */
async function signIn(context: BrowserContext) {
  const jwt = [
    base64Url(JSON.stringify({ alg: "none", typ: "JWT" })),
    base64Url(
      JSON.stringify({
        sub: "22222222-2222-4222-8222-222222222222",
        role: "authenticated",
        aud: "authenticated",
        exp: 4_102_444_800,
      }),
    ),
    "signature",
  ].join(".");
  const session = {
    access_token: jwt,
    refresh_token: "refresh",
    token_type: "bearer",
    expires_in: 3_600_000,
    expires_at: 4_102_444_800,
    user: {
      id: "22222222-2222-4222-8222-222222222222",
      aud: "authenticated",
      role: "authenticated",
      email: "manager@example.test",
      app_metadata: {},
      user_metadata: {},
      created_at: "2026-01-01T00:00:00Z",
    },
  };
  await context.addCookies([
    {
      name: "sb-127-auth-token",
      value: `base64-${base64Url(JSON.stringify(session))}`,
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  await context.addInitScript((id) => window.localStorage.setItem("active-workspace-id", id), WORKSPACE);
}

async function reset(scenario: string) {
  await fetch(`${MOCK}/__reset`, { method: "POST", body: JSON.stringify({ scenario }) });
}

async function mockState() {
  return (await (await fetch(`${MOCK}/__state`)).json()) as Record<string, any>;
}

async function mockRequests() {
  return (await (await fetch(`${MOCK}/__requests`)).json()) as { method: string; path: string; body: any }[];
}

const shot = (page: Page, name: string) =>
  page.screenshot({ path: `e2e/screenshots/${name}.png`, fullPage: true });

const sequenceUrl = `/app/campaigns/${CAMPAIGN}/sequence`;

test.beforeEach(async ({ context }) => {
  await signIn(context);
});

test("pause, edit an unsent email, change the schedule, add leads, resume", async ({ page }) => {
  await reset("standard");

  // --- pause
  await page.goto(sequenceUrl);
  await expect(page.getByRole("button", { name: "Pause campaign" })).toBeVisible();
  await shot(page, "01-running");
  await page.getByRole("button", { name: "Pause campaign" }).click();
  const pauseDialog = page.getByRole("dialog");
  await expect(pauseDialog).toContainText("can't be recalled");
  await pauseDialog.getByRole("button", { name: "Pause campaign" }).click();
  await expect(page.getByRole("button", { name: "Resume campaign" })).toBeVisible();
  await expect(page.getByText(/This campaign is paused\. Changes apply to emails that haven't been sent yet/)).toBeVisible();

  // --- the paused editor: counts, and the place of a reached step is locked
  const first = page.getByRole("article", { name: "Step 1: Email" });
  await expect(first.getByText(/Sent to 12 leads/)).toContainText("8 waiting");
  await expect(page.getByRole("button", { name: "Delete step 1" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Delete step 2" })).toBeEnabled();
  await shot(page, "02-paused-sequence");

  // --- edit the unsent step's wording
  const second = page.getByRole("article", { name: "Step 2: Email" });
  await second.getByRole("button", { name: /edit/i }).click();
  const editor = page.getByRole("dialog");
  await editor.getByLabel(/subject/i).first().fill("A sharper second email");
  await shot(page, "03-editing-step");
  await editor.getByRole("button", { name: /^save$/i }).click();
  await expect.poll(async () => (await mockState()).steps.find((s: any) => s.id === STEP2).email_subject).toBe(
    "A sharper second email",
  );
  const patches = (await mockRequests()).filter((r) => r.method === "PATCH");
  expect(patches.at(-1)?.path).toContain(`/sequence/steps/${STEP2}`);

  // --- change the schedule
  await page.goto(`/app/campaigns/${CAMPAIGN}/schedule`);
  await expect(page.getByText(/applies to emails that haven't been sent/i).first()).toBeVisible();
  await shot(page, "04-paused-schedule");
  await page.getByRole("button", { name: /save schedule/i }).click();
  await expect.poll(async () => (await mockState()).settings.length).toBeGreaterThan(1);

  // --- add leads while paused
  await page.goto(`/app/campaigns/${CAMPAIGN}/audience`);
  await expect(page.getByRole("heading", { name: "Add leads" })).toBeVisible();
  await shot(page, "05-add-leads");

  // --- resume (the finished-leads option starts unticked)
  await page.getByRole("button", { name: "Resume campaign" }).first().click();
  const resumeDialog = page.getByRole("dialog");
  const box = resumeDialog.getByRole("checkbox", { name: /already finished the sequence/i });
  await expect(box).not.toBeChecked();
  await shot(page, "06-resume-dialog");
  await resumeDialog.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByRole("button", { name: "Pause campaign" })).toBeVisible();
  expect((await mockState()).lastResume).toEqual({ include_completed: false });
  await shot(page, "07-resumed");
});

test("resume can opt in to giving finished leads the new emails", async ({ page }) => {
  await reset("standard");
  await page.goto(sequenceUrl);
  await page.getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("button", { name: "Resume campaign" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /already finished the sequence/i }).check();
  await dialog.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByRole("button", { name: "Pause campaign" })).toBeVisible();
  expect((await mockState()).lastResume).toEqual({ include_completed: true });
});

test("a resume the server refuses lists what to fix and leaves the campaign paused", async ({ page }) => {
  await reset("blocked");
  await page.goto(sequenceUrl);
  await page.getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByText("This campaign can't resume yet:")).toBeVisible();
  await expect(page.getByText(/Connect a mailbox before resuming/)).toBeVisible();
  await shot(page, "08-resume-blocked");
  expect((await mockState()).campaign.status).toBe("PAUSED");
});

test("a failed add of leads can be retried, and resume is blocked until it succeeds", async ({ page }) => {
  await reset("enroll-fails");
  await page.goto(`/app/campaigns/${CAMPAIGN}/audience`);
  await page.getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Pause campaign" }).click();
  await expect(page.getByRole("heading", { name: "Add leads" })).toBeVisible();

  // Pick a list, check it, add its new leads (the mock makes the add fail).
  await page.getByRole("checkbox", { name: /Fresh prospects/ }).check();
  await page.getByRole("button", { name: "Check leads" }).click();
  await page.getByRole("button", { name: /Add \d+ leads? to this campaign/ }).click();
  await expect(page.getByText(/Adding these leads failed/)).toBeVisible();
  await shot(page, "09-add-failed");

  // Resume is refused while the add is failed ...
  await page.getByRole("button", { name: "Resume campaign" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByText(/Preparing leads for this campaign failed/)).toBeVisible();

  // ... and works after Retry.
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText(/leads? added\. They are sent when you resume/)).toBeVisible();
  await page.getByRole("button", { name: "Resume campaign" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByRole("button", { name: "Pause campaign" })).toBeVisible();
});

test("a paused personalized campaign opens only unwritten references", async ({ page }) => {
  await reset("hyper");
  await page.goto(sequenceUrl);
  await page.getByRole("button", { name: "Pause campaign" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Pause campaign" }).click();
  await expect(page.getByText(/versions already written, the waits and the order of steps can't change/)).toBeVisible();

  const first = page.getByRole("article", { name: "Step 1: Email" });
  const second = page.getByRole("article", { name: "Step 2: Email" });
  await expect(first.getByText(/12 personalized emails already written/)).toBeVisible();
  await expect(first.getByRole("button", { name: /view/i })).toBeVisible();
  await expect(second.getByText(/20 will be written from this version/)).toBeVisible();
  await expect(second.getByRole("button", { name: /edit/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^add step$/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /delete step/i })).toHaveCount(0);
  // The objective / company / email-style setup is not editable while paused.
  await expect(page.getByRole("button", { name: /regenerate/i })).toHaveCount(0);
  await expect(page.getByText(/No mock for/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /edit wait/i })).toHaveCount(0);
  await shot(page, "10-paused-personalized");

  // The finished-leads option is not offered for a personalized campaign.
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByRole("dialog").getByRole("checkbox")).toHaveCount(0);
  void STEP1;
});
