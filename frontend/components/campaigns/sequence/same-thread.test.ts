import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { SequenceStep } from "@/types/domain";

import { replySubject, sameThreadErrorMessage, UNKNOWN_REPLY_SUBJECT } from "./same-thread";

function email(subject: string | null, sameThread = false): SequenceStep {
  return {
    id: subject ?? "none",
    sequence_id: "seq-1",
    campaign_id: "camp-1",
    position: 1,
    kind: "EMAIL",
    email_subject: subject,
    email_body_html: "",
    email_same_thread: sameThread,
    email_variable_schema: {},
    wait_duration_minutes: null,
    source_template_version_id: null,
    version: 1,
    created_at: "",
    updated_at: "",
  };
}

describe("replySubject", () => {
  it("prefixes the previous email's subject", () => {
    expect(replySubject([email("Quick idea")])).toBe("Re: Quick idea");
  });

  it("does not double an existing Re:", () => {
    expect(replySubject([email("RE: Quick idea")])).toBe("RE: Quick idea");
  });

  it("follows a chain of threaded emails back to the one with its own subject", () => {
    expect(replySubject([email("First"), email("ignored", true)])).toBe("Re: First");
  });

  it("is unknown for personalized campaigns or without a subject", () => {
    expect(replySubject([email("First")], true)).toBe(UNKNOWN_REPLY_SUBJECT);
    expect(replySubject([])).toBe(UNKNOWN_REPLY_SUBJECT);
  });
});

describe("sameThreadErrorMessage", () => {
  it("maps the first-email refusal", () => {
    const error = new ApiError("x", 422, "same_thread_requires_previous_email", null);
    expect(sameThreadErrorMessage(error)).toBe(
      "The first email can't be sent in the same thread.",
    );
  });

  it("maps step_locked only when the threading was being changed", () => {
    const error = new ApiError("x", 409, "step_locked", null);
    expect(sameThreadErrorMessage(error, true)).toBe(
      "Leads have already reached this step, so its threading can't change.",
    );
    expect(sameThreadErrorMessage(error)).toBeNull();
  });

  it("ignores other errors", () => {
    expect(sameThreadErrorMessage(new ApiError("x", 409, "conflict", null), true)).toBeNull();
    expect(sameThreadErrorMessage(new Error("network"))).toBeNull();
  });
});
