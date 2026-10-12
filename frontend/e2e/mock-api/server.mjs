// A small STATEFUL stand-in for the backend and for Supabase auth, used only by the
// Playwright browser checks (frontend/e2e). It lets the real Next.js app be driven through
// pause -> edit while paused -> change the schedule -> add leads -> resume without a
// database, a mailbox or any email.
//
// It mimics the contract the pages rely on (version checks, 409s, resume_blocked,
// supplements, locked steps); it does NOT reproduce backend behaviour. The backend's rules
// are proven by the real-PostgreSQL tests; this only proves the screens present and use them.
//
// Usage: node e2e/mock-api/server.mjs [port]   (default 4010)
//   GET  /__state    -> current in-memory state (for assertions)
//   POST /__reset    -> back to the initial state; body {"scenario": "..."} picks a variant
//   GET  /__requests -> every request received, for assertions about what the UI sent

import http from "node:http";

const PORT = Number(process.argv[2] ?? process.env.MOCK_API_PORT ?? 4010);
const WS = "11111111-1111-4111-8111-111111111111";
const USER = "22222222-2222-4222-8222-222222222222";
const CAMPAIGN = "33333333-3333-4333-8333-333333333333";
const SEQUENCE = "44444444-4444-4444-8444-444444444444";
const MAILBOX = "55555555-5555-4555-8555-555555555555";
const SETTINGS = "66666666-6666-4666-8666-666666666666";

const STEP1 = "a0000000-0000-4000-8000-000000000001";
const WAIT = "a0000000-0000-4000-8000-000000000002";
const STEP2 = "a0000000-0000-4000-8000-000000000003";

const NOW = () => new Date().toISOString();
let counter = 100;
const uid = () => `b0000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;

let state;
let requests;

function initialState(scenario = "standard") {
  const hyper = scenario === "hyper";
  const step = (id, position, kind, extra) => ({
    id,
    sequence_id: SEQUENCE,
    campaign_id: CAMPAIGN,
    position,
    kind,
    email_subject: null,
    email_body_html: null,
    email_preheader: null,
    attachments: [],
    email_variable_schema: null,
    wait_duration_minutes: null,
    source_template_version_id: null,
    version: 1,
    created_at: NOW(),
    updated_at: NOW(),
    ...extra,
  });
  return {
    scenario,
    // Scenario knobs the spec flips through /__reset.
    resumeBlockedReasons: scenario === "blocked" ? ["Connect a mailbox before resuming."] : [],
    campaign: {
      id: CAMPAIGN,
      workspace_id: WS,
      name: "Founders outreach",
      description: null,
      creator_id: USER,
      status: "RUNNING",
      campaign_type: hyper ? "HYPER_PERSONALIZED" : "STANDARD",
      start_at: null,
      draft_sequence_id: SEQUENCE,
      draft_audience_id: null,
      current_settings_id: SETTINGS,
      planning_status: "READY",
      archived_at: null,
      error_reason: null,
      is_activated: true,
      erased_at: null,
      paused_at: null,
      paused_total_seconds: 0,
      last_resumed_at: null,
      outstanding_attempts: null,
      version: 4,
      created_at: NOW(),
      updated_at: NOW(),
    },
    steps: [
      step(STEP1, 1, "EMAIL", {
        email_subject: "Quick idea for {{company}}",
        email_body_html: "<p>Hi {{first_name}}, we help teams like yours.</p>",
        sent: 12,
        unsent: 8,
        written: hyper ? 12 : 0,
        unwritten: hyper ? 8 : 0,
      }),
      step(WAIT, 2, "WAIT", { wait_duration_minutes: 2880 }),
      step(STEP2, 3, "EMAIL", {
        email_subject: "A different angle",
        email_body_html: "<p>Following up, {{first_name}}.</p>",
        // Nobody has reached this step yet in a standard campaign; a personalized one has
        // its follow-ups planned (but not yet written) for the leads that were sent step 1.
        sent: 0,
        unsent: hyper ? 20 : 0,
        written: 0,
        unwritten: hyper ? 20 : 0,
      }),
    ],
    settings: [
      {
        id: SETTINGS,
        campaign_id: CAMPAIGN,
        revision: 1,
        timezone: "Europe/London",
        weekdays: [1, 2, 3, 4, 5],
        window_start_local: "09:00:00",
        window_end_local: "17:00:00",
        daily_limit: null,
        created_at: NOW(),
      },
    ],
    supplements: [],
    leads: [
      lead("c0000000-0000-4000-8000-000000000001", "ada@lovelace.test", "Ada", "Lovelace", "Analytical"),
      lead("c0000000-0000-4000-8000-000000000002", "grace@hopper.test", "Grace", "Hopper", "Navy"),
      lead("c0000000-0000-4000-8000-000000000003", "alan@turing.test", "Alan", "Turing", "Bletchley"),
    ],
  };
}

function lead(id, email, first, last, company) {
  return {
    id,
    workspace_id: WS,
    email,
    canonical_address: email,
    normalization_version: 1,
    first_name: first,
    last_name: last,
    company,
    title: null,
    custom_fields: {},
    status: "ACTIVE",
    validation_status: "UNVERIFIED",
    validated_at: null,
    contact_revision: 1,
    archived_at: null,
    version: 1,
    created_at: NOW(),
    updated_at: NOW(),
    list_count: 0,
  };
}

function reset(scenario) {
  state = initialState(scenario);
  requests = [];
}
reset();

// ---------------------------------------------------------------------------

function frontier() {
  // The highest step position any lead has reached.
  return state.steps.reduce((max, s) => (s.sent > 0 || s.unsent > 0 ? Math.max(max, s.position) : max), 0);
}

function stepOut(s) {
  const paused = state.campaign.status === "PAUSED";
  const hyper = state.campaign.campaign_type === "HYPER_PERSONALIZED";
  const { sent, unsent, written, unwritten, ...rest } = s;
  return {
    ...rest,
    ...(paused
      ? {
          sent_count: sent ?? 0,
          unsent_count: unsent ?? 0,
          ...(hyper ? { written_count: written ?? 0, unwritten_count: unwritten ?? 0 } : {}),
        }
      : {}),
  };
}

function sequenceOut() {
  const paused = state.campaign.status === "PAUSED";
  return {
    id: SEQUENCE,
    campaign_id: CAMPAIGN,
    revision: 1,
    status: "FROZEN",
    steps: state.steps.map(stepOut),
    paused_edit_allowed: paused,
    structure_locked_through: paused ? frontier() : 0,
  };
}

function bump(campaign) {
  campaign.version += 1;
  campaign.updated_at = NOW();
}

function error(res, status, code, message, details) {
  json(res, status, { error: { code, message, ...(details ? { details } : {}) } });
}

function json(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,PUT,OPTIONS",
    "x-request-id": "mock-" + uid(),
  });
  res.end(text);
}

async function body(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString("utf8");
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------

const API = `/api/v1/workspaces/${WS}`;

async function route(req, res) {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;
  const method = req.method;

  if (method === "OPTIONS") return json(res, 204, {});

  // --- test controls
  if (path === "/__state") return json(res, 200, state);
  if (path === "/__requests") return json(res, 200, requests);
  if (path === "/__reset") {
    const input = await body(req);
    reset(input.scenario ?? "standard");
    return json(res, 200, { ok: true });
  }

  // --- Supabase auth stub (used by the Next.js middleware and the browser client)
  if (path === "/auth/v1/user") {
    return json(res, 200, {
      id: USER,
      aud: "authenticated",
      role: "authenticated",
      email: "manager@example.test",
      email_confirmed_at: NOW(),
      app_metadata: {},
      user_metadata: {},
      created_at: NOW(),
      updated_at: NOW(),
    });
  }
  if (path.startsWith("/auth/v1/")) return json(res, 200, {});

  let payload = {};
  if (method !== "GET" && method !== "HEAD") payload = await body(req);
  requests.push({ method, path: path + url.search, body: payload });

  // --- identity
  if (path === "/api/v1/me") {
    return json(res, 200, { id: USER, display_name: "Mara Manager", email: "manager@example.test" });
  }
  if (path === "/api/v1/workspaces") {
    return json(res, 200, [
      {
        workspace_id: WS,
        workspace_name: "Acme",
        workspace_status: "ACTIVE",
        membership_id: uid(),
        role_code: "MANAGER",
        membership_version: 1,
      },
    ]);
  }

  if (!path.startsWith(API)) return error(res, 404, "not_found", `No mock for ${method} ${path}`);
  const rest = path.slice(API.length);
  const c = state.campaign;

  // --- lead pickers
  if (rest === "/lead-lists") {
    return json(res, 200, {
      items: [
        {
          id: "d0000000-0000-4000-8000-000000000001",
          workspace_id: WS,
          name: "Fresh prospects",
          archived_at: null,
          membership_revision: 1,
          member_count: state.leads.length,
          version: 1,
          created_at: NOW(),
          updated_at: NOW(),
        },
      ],
      next_cursor: null,
    });
  }
  if (rest === "/leads") {
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const items = state.leads.filter(
      (l) => !q || l.email.includes(q) || (l.first_name ?? "").toLowerCase().includes(q),
    );
    return json(res, 200, { items, next_cursor: null });
  }

  // --- personalization (only a personalized campaign's Sequence tab asks for these)
  if (rest === "/personalization/capabilities") {
    return json(res, 200, { enabled: true, model: "mock-model", ai_drafting_available: true });
  }
  if (rest === `/campaigns/${CAMPAIGN}/personalization` && method === "GET") {
    return json(res, 200, {
      campaign_id: CAMPAIGN,
      campaign_type: c.campaign_type,
      enabled: true,
      model: "mock-model",
      config: {
        objective: "Book a short intro call",
        offer: "A faster way to run outreach",
        cta: "Reply with a good time",
        target: "Founders",
        problem_solved: "Manual outreach takes hours",
        tone: "Warm and direct",
        must_mention: [],
        never_say: [],
        email_format: "TEXT",
      },
      config_version: 1,
      config_digest: "digest",
      approval: { status: "APPROVED", approved_at: NOW(), approved_by: USER },
    });
  }
  if (rest === `/campaigns/${CAMPAIGN}/personalization/progress`) {
    return json(res, 200, {
      campaign_id: CAMPAIGN,
      pending: 8,
      succeeded: 12,
      failed: 0,
      superseded: 0,
      fallback: 0,
      oldest_pending_at: null,
      failure_codes: {},
      budget: {
        generation_used: 12,
        generation_cap: 500,
        preview_used: 0,
        preview_cap: 50,
        fetch_used: 0,
        fetch_cap: 100,
      },
    });
  }

  // --- campaign
  const campaignPrefix = `/campaigns/${CAMPAIGN}`;
  if (rest === campaignPrefix && method === "GET") return json(res, 200, c);

  if (rest === `${campaignPrefix}/pause` && method === "POST") {
    if (c.status === "PAUSED") return json(res, 200, c);
    if (c.version !== payload.expected_version) {
      return error(res, 409, "conflict", "Campaign was modified by another user. Please refresh and try again.");
    }
    c.status = "PAUSED";
    c.paused_at = NOW();
    c.outstanding_attempts = 2;
    bump(c);
    return json(res, 200, c);
  }

  if (rest === `${campaignPrefix}/resume` && method === "POST") {
    if (c.status === "RUNNING") return json(res, 200, c);
    if (c.version !== payload.expected_version) {
      return error(res, 409, "conflict", "Campaign was modified by another user. Please refresh and try again.");
    }
    const blocked = [...state.resumeBlockedReasons];
    if (state.supplements.some((s) => ["PENDING", "PROCESSING"].includes(s.enroll_state))) {
      blocked.push("Leads you added are still being prepared. Resume once that finishes.");
    }
    if (state.supplements.some((s) => s.enroll_state === "FAILED" || s.render_state === "FAILED")) {
      blocked.push("Preparing leads for this campaign failed. Retry the failed step before resuming.");
    }
    if (blocked.length > 0) {
      return error(res, 422, "resume_blocked", "This campaign can't resume yet. Fix the listed problems first.", {
        ready: false,
        errors: blocked.map((message, i) => ({ code: `blocked_${i}`, message, field_path: null })),
        warnings: [],
      });
    }
    c.status = "RUNNING";
    c.paused_at = null;
    c.last_resumed_at = NOW();
    c.outstanding_attempts = null;
    bump(c);
    state.lastResume = { include_completed: payload.include_completed === true };
    return json(res, 200, c);
  }

  if (rest === `${campaignPrefix}/planning`) {
    return json(res, 200, { campaign_id: CAMPAIGN, planning_status: "READY", enroll: null, render: null });
  }
  if (rest === `${campaignPrefix}/preflight`) return json(res, 200, { ready: true, errors: [], warnings: [] });
  if (rest === `${campaignPrefix}/mailboxes`) {
    return json(res, 200, [
      {
        mailbox_id: MAILBOX,
        provider: "GMAIL",
        email_address: "sender@acme.test",
        sender_display_name: "Sender",
        connection_state: "CONNECTED",
        health_state: "HEALTHY",
        policy_state: "ENABLED",
        policy_reason: null,
        active: true,
        allocation_position: 1,
      },
    ]);
  }
  if (rest === `${campaignPrefix}/audience` && method === "GET") {
    return json(res, 200, {
      id: "e0000000-0000-4000-8000-000000000001",
      campaign_id: CAMPAIGN,
      revision: 1,
      status: "READY",
      started_at: NOW(),
      completed_at: NOW(),
      is_committed: true,
      total_candidates: 40,
      processed_count: 40,
      accepted_count: 40,
      excluded_count: 0,
      error_reason: null,
      selected_list_ids: [],
      selected_lead_ids: [],
    });
  }

  // --- settings (schedule)
  if (rest === `${campaignPrefix}/settings`) {
    if (method === "GET") return json(res, 200, [...state.settings].reverse());
    if (method === "POST") {
      if (!["PAUSED", "DRAFT"].includes(c.status)) {
        return error(res, 409, "state_conflict", "Settings can only be changed while the campaign is a draft or paused");
      }
      const created = {
        id: uid(),
        campaign_id: CAMPAIGN,
        revision: state.settings.length + 1,
        timezone: payload.timezone,
        weekdays: payload.weekdays,
        window_start_local: payload.window_start_local,
        window_end_local: payload.window_end_local,
        daily_limit: payload.daily_limit ?? null,
        created_at: NOW(),
      };
      state.settings.push(created);
      c.current_settings_id = created.id;
      bump(c);
      return json(res, 201, created);
    }
  }

  // --- sequence
  if (rest === `${campaignPrefix}/sequence` && method === "GET") return json(res, 200, sequenceOut());
  if (rest === `${campaignPrefix}/sequence/preview-recipients`) {
    return json(res, 200, { source: "NONE", items: [], total: 0, next_cursor: null });
  }
  const stepMatch = rest.match(new RegExp(`^${campaignPrefix}/sequence/steps/([^/]+)$`));
  if (stepMatch && method === "PATCH") {
    const step = state.steps.find((s) => s.id === stepMatch[1]);
    if (!step) return error(res, 404, "not_found", "Sequence step not found");
    if (c.status !== "PAUSED") {
      return error(res, 409, "state_conflict", "Sequence can only be edited while the campaign is DRAFT or paused");
    }
    if (step.version !== payload.expected_version) {
      return error(res, 409, "conflict", "Step was modified by another user. Please refresh and try again.");
    }
    if (c.campaign_type === "HYPER_PERSONALIZED" && (step.written ?? 0) > 0) {
      return error(
        res,
        409,
        "step_locked",
        "Emails for this step have already been written, so its wording can't change.",
      );
    }
    for (const key of ["email_subject", "email_body_html", "email_preheader", "wait_duration_minutes"]) {
      if (payload[key] !== undefined && payload[key] !== null) step[key] = payload[key];
    }
    step.version += 1;
    step.updated_at = NOW();
    return json(res, 200, stepOut(step));
  }

  // --- supplements (leads added while paused)
  if (rest === `${campaignPrefix}/audience/supplements`) {
    if (method === "GET") return json(res, 200, state.supplements);
    if (method === "POST") {
      const picked = (payload.lead_ids ?? []).length + (payload.list_ids ?? []).length * state.leads.length;
      const accepted = Math.max(0, picked - 1);
      const supplement = {
        id: uid(),
        campaign_id: CAMPAIGN,
        revision: state.supplements.length + 2,
        status: "READY",
        started_at: NOW(),
        completed_at: NOW(),
        total_candidates: picked,
        processed_count: picked,
        accepted_count: accepted,
        excluded_count: picked - accepted,
        exclusions: picked - accepted > 0 ? { already_enrolled: picked - accepted } : {},
        enroll_state: "NOT_STARTED",
        render_state: "NOT_STARTED",
        enrolled_count: 0,
        error_reason: null,
      };
      state.supplements.push(supplement);
      return json(res, 202, supplement);
    }
  }
  const supplementMatch = rest.match(new RegExp(`^${campaignPrefix}/audience/supplements/([^/]+)/(enroll|retry)$`));
  if (supplementMatch && method === "POST") {
    const supplement = state.supplements.find((s) => s.id === supplementMatch[1]);
    if (!supplement) return error(res, 404, "not_found", "Audience revision not found");
    if (c.status !== "PAUSED") {
      return error(res, 409, "state_conflict", "Leads can only be added to a campaign while it is paused");
    }
    if (supplementMatch[2] === "enroll") {
      supplement.enroll_state = state.scenario === "enroll-fails" ? "FAILED" : "READY";
      supplement.render_state = state.scenario === "enroll-fails" ? "NOT_STARTED" : "READY";
      supplement.enrolled_count = state.scenario === "enroll-fails" ? 0 : (supplement.accepted_count ?? 0);
      supplement.error_reason = state.scenario === "enroll-fails" ? "An unexpected error occurred" : null;
    } else if (supplement.enroll_state === "FAILED" || supplement.render_state === "FAILED") {
      state.scenario = "standard"; // the retry succeeds
      supplement.enroll_state = "READY";
      supplement.render_state = "READY";
      supplement.enrolled_count = supplement.accepted_count ?? 0;
      supplement.error_reason = null;
    }
    return json(res, 202, supplement);
  }

  return error(res, 404, "not_found", `No mock for ${method} ${path}`);
}

const server = http.createServer((req, res) => {
  route(req, res).catch((err) => {
    console.error(err);
    error(res, 500, "internal_error", String(err?.message ?? err));
  });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock API + auth listening on http://127.0.0.1:${PORT}`);
});
