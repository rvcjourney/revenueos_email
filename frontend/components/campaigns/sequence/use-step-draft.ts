"use client";

import * as React from "react";

import type { SequenceStep } from "@/types/domain";

export type StepDraft = {
  subject: string;
  preheader: string;
  bodyHtml: string;
  // Text of the "Start this step on Day N" field; only meaningful when the step
  // has a preceding wait.
  day: string;
  // Send as a reply to the previous email, with its subject (ADR-0021).
  sameThread: boolean;
};

export function draftFromStep(step: SequenceStep, day: number): StepDraft {
  return {
    subject: step.email_subject ?? "",
    preheader: step.email_preheader ?? "",
    bodyHtml: step.email_body_html ?? "",
    day: String(day),
    sameThread: step.email_same_thread ?? false,
  };
}

// Draft state for the email editor. `baseline` is what the server has; `dirty`
// compares the draft with it field by field, and clears only when the caller
// resets the baseline after a confirmed save (never optimistically).
export function useStepDraft(initial: StepDraft) {
  const [draft, setDraft] = React.useState<StepDraft>(initial);
  const [baseline, setBaseline] = React.useState<StepDraft>(initial);

  const dirty =
    draft.subject !== baseline.subject ||
    draft.preheader !== baseline.preheader ||
    draft.bodyHtml !== baseline.bodyHtml ||
    draft.day !== baseline.day ||
    draft.sameThread !== baseline.sameThread;

  const update = React.useCallback(
    (patch: Partial<StepDraft>) => setDraft((prev) => ({ ...prev, ...patch })),
    [],
  );

  const reset = React.useCallback((next: StepDraft) => {
    setDraft(next);
    setBaseline(next);
  }, []);

  // Record what the server now holds without touching what the user is typing,
  // e.g. after the email part of a two-part save succeeded.
  const markSaved = React.useCallback((saved: Partial<StepDraft>) => {
    setBaseline((prev) => ({ ...prev, ...saved }));
  }, []);

  return { draft, baseline, dirty, update, reset, markSaved };
}

// Warn before the browser closes/reloads the tab with unsaved edits.
export function useUnsavedChangesGuard(active: boolean) {
  React.useEffect(() => {
    if (!active) return;
    function onBeforeUnload(event: BeforeUnloadEvent) {
      event.preventDefault();
      // Required by some browsers to show the prompt.
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [active]);
}
