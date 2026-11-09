import { ApiError } from "@/lib/api-client";
import type { SequenceStep } from "@/types/domain";

export const UNKNOWN_REPLY_SUBJECT = "Re: (subject of the previous email)";

// The subject a same-thread step goes out with (ADR-0021): "Re: " + the subject of
// the nearest earlier email that has its own, without doubling an existing "Re:".
// A personalized email's subject is written per lead, so it is not known here.
export function replySubject(emailsBefore: SequenceStep[], personalized = false): string {
  if (personalized) return UNKNOWN_REPLY_SUBJECT;
  const base = [...emailsBefore].reverse().find((s) => !s.email_same_thread)?.email_subject?.trim();
  if (!base) return UNKNOWN_REPLY_SUBJECT;
  return /^re:/i.test(base) ? base : `Re: ${base}`;
}

// Friendly text for the API's same-thread refusals; null for anything else.
export function sameThreadErrorMessage(error: unknown, togglingThread = false): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code === "same_thread_requires_previous_email") {
    return "The first email can't be sent in the same thread.";
  }
  if (error.code === "step_locked" && togglingThread) {
    return "Leads have already reached this step, so its threading can't change.";
  }
  return null;
}
