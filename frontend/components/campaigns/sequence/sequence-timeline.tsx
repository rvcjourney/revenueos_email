"use client";

import * as React from "react";
import {
  ArrowDown,
  ArrowUp,
  Clock,
  Copy,
  Loader2,
  Mail,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";

import {
  computeTimings,
  formatDuration,
  sequenceProblems,
} from "@/components/campaigns/sequence/duration";
import { DurationEditor } from "@/components/campaigns/sequence/duration-input";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Popover } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { SequenceStep } from "@/types/domain";

type Props = {
  steps: SequenceStep[];
  readOnly: boolean;
  busy: boolean;
  // e.g. "Monday–Friday, 09:00–17:00 Europe/London"; null when not configured.
  scheduleNote: string | null;
  savingWaitId: string | null;
  onOpenEmail: (step: SequenceStep) => void;
  onDuplicate: (step: SequenceStep) => void;
  onDelete: (step: SequenceStep) => void;
  onMoveEmail: (step: SequenceStep, direction: -1 | 1) => void;
  onSaveWait: (step: SequenceStep, minutes: number) => void;
  onInsertAfter: (step: SequenceStep) => void;
  onAddEmail: () => void;
  // Hyper-personalized campaigns with AI drafting only: rewrite one email.
  onRegenerateEmail?: (step: SequenceStep, number: number) => void;
  regeneratingStepId?: string | null;
  // Any AI drafting request is running, so every Regenerate button waits.
  generating?: boolean;
  // Paused campaign (ADR-0020): steps at or before this position were reached by a
  // lead. Their content and waits stay editable; their place in the sequence does
  // not, and neither does deleting them. 0 for a draft.
  lockedThrough?: number;
  // Personalized campaign: each lead's email is written from this reference just before
  // it is sent, so a paused editor says how many are already written (they keep their
  // current version) and how many will use the new wording.
  personalized?: boolean;
};

function plainText(html: string | null): string {
  if (!html) return "";
  // Keep paragraphs apart: textContent alone would run "Hi Sam," into the next line.
  const spaced = html.replace(/<\/(p|div|li|h[1-6]|tr|td|table)>|<br\s*\/?>/gi, " ");
  const doc = new DOMParser().parseFromString(spaced, "text/html");
  return (doc.body.textContent ?? "").replace(/\s+/g, " ").trim();
}

export function SequenceTimeline({
  steps,
  readOnly,
  busy,
  scheduleNote,
  savingWaitId,
  onOpenEmail,
  onDuplicate,
  onDelete,
  onMoveEmail,
  onSaveWait,
  onInsertAfter,
  onAddEmail,
  onRegenerateEmail,
  regeneratingStepId = null,
  generating = false,
  lockedThrough = 0,
  personalized = false,
}: Props) {
  const timings = React.useMemo(() => computeTimings(steps), [steps]);
  const problems = sequenceProblems(steps);
  const emails = steps.filter((s) => s.kind === "EMAIL");

  // Emails are numbered ("Step 1", "Step 2"); waits are the gaps between them.
  const emailNumber = new Map<string, number>();
  emails.forEach((step, index) => emailNumber.set(step.id, index + 1));

  return (
    <div className="space-y-4">
      {problems.length > 0 ? (
        <Alert variant="warning">
          {problems.join(" ")} Remove the extra wait, or add an email, so the sequence reads
          Email, Wait, Email.
        </Alert>
      ) : null}
      {scheduleNote ? (
        <p className="text-xs text-slate-500">
          Follow-ups wait the time shown after the previous email is sent, then go out inside your
          sending window ({scheduleNote}).
        </p>
      ) : (
        <p className="text-xs text-slate-500">
          Follow-ups wait the time shown after the previous email is sent. Set your sending days
          and hours on the Schedule tab.
        </p>
      )}

      <ol className="flex flex-col items-stretch">
        {steps.map((step, index) => {
          const timing = timings.get(step.id);
          if (step.kind === "EMAIL") {
            const number = emailNumber.get(step.id) ?? 0;
            const emailIndex = number - 1;
            const snippet = plainText(step.email_body_html);
            const nextIsWait = steps[index + 1]?.kind === "WAIT";
            // A step leads already reached keeps its place; one after it can move.
            const placeLocked = step.position <= lockedThrough;
            const previousEmail = emailIndex > 0 ? emails[emailIndex - 1] : null;
            const upLocked = placeLocked || (previousEmail?.position ?? 0) <= lockedThrough;
            const showCounts =
              !personalized && lockedThrough > 0 && step.sent_count !== undefined;
            const writtenCount = step.written_count ?? 0;
            const unwrittenCount = step.unwritten_count ?? 0;
            return (
              <li key={step.id} className="list-none">
                <article
                  aria-label={`Step ${number}: Email`}
                  className="rounded-xl border border-slate-200 bg-white p-4 shadow-card"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700">
                        {number}
                      </span>
                      <Mail className="h-4 w-4 shrink-0 text-brand-500" aria-hidden="true" />
                      <span className="text-sm font-semibold text-slate-900">Email</span>
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                        {index === 0 ? "Day 1 · at start" : `Day ${timing?.day ?? 1}`}
                      </span>
                    </div>
                    <div className="flex items-center gap-1">
                      <Button variant="outline" size="sm" onClick={() => onOpenEmail(step)}>
                        <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                        {readOnly ? "View" : "Edit"}
                      </Button>
                      {!readOnly ? (
                        <>
                          {onRegenerateEmail ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-label={`Regenerate step ${number}`}
                              title="Rewrite this email with AI"
                              disabled={busy || generating}
                              onClick={() => onRegenerateEmail(step, number)}
                            >
                              {regeneratingStepId === step.id ? (
                                <Loader2
                                  className="h-3.5 w-3.5 animate-spin"
                                  aria-hidden="true"
                                />
                              ) : (
                                <Sparkles
                                  className="h-3.5 w-3.5 text-violet-600"
                                  aria-hidden="true"
                                />
                              )}
                              Regenerate
                            </Button>
                          ) : null}
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8"
                            aria-label={`Duplicate step ${number}`}
                            title="Duplicate"
                            disabled={busy || step.position < lockedThrough}
                            onClick={() => onDuplicate(step)}
                          >
                            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8"
                            aria-label={`Move step ${number} up`}
                            disabled={busy || emailIndex === 0 || upLocked}
                            title={placeLocked ? "Leads have already received this email" : "Move up"}
                            onClick={() => onMoveEmail(step, -1)}
                          >
                            <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8"
                            aria-label={`Move step ${number} down`}
                            title={placeLocked ? "Leads have already received this email" : "Move down"}
                            disabled={busy || emailIndex === emails.length - 1 || placeLocked}
                            onClick={() => onMoveEmail(step, 1)}
                          >
                            <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 text-red-600 hover:text-red-700"
                            aria-label={`Delete step ${number}`}
                            title={placeLocked ? "Leads have already received this email" : "Delete"}
                            disabled={busy || placeLocked}
                            onClick={() => onDelete(step)}
                          >
                            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                          </Button>
                        </>
                      ) : null}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => onOpenEmail(step)}
                    className="mt-3 block w-full rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600"
                  >
                    <p className="truncate text-sm font-medium text-slate-900">
                      {step.email_subject?.trim() || (
                        <span className="text-slate-400">(no subject)</span>
                      )}
                    </p>
                    <p className="mt-0.5 line-clamp-2 text-sm text-slate-500">
                      {snippet || <span className="text-slate-400">Empty body</span>}
                    </p>
                  </button>
                  {personalized && step.written_count !== undefined ? (
                    <p className="mt-2 text-xs text-slate-500">
                      {writtenCount > 0
                        ? `${writtenCount} personalized email${writtenCount === 1 ? "" : "s"} already written keep${writtenCount === 1 ? "s" : ""} the current version.`
                        : "Nothing written yet."}
                      {unwrittenCount > 0
                        ? ` ${unwrittenCount} will be written from this version.`
                        : ""}
                    </p>
                  ) : null}
                  {showCounts ? (
                    <p className="mt-2 text-xs text-slate-500">
                      Sent to {step.sent_count} lead{step.sent_count === 1 ? "" : "s"}
                      {" · "}
                      {step.unsent_count} waiting. Changes apply to the ones waiting.
                    </p>
                  ) : null}
                </article>
                {/* Insert-between control sits on the connector after this email. */}
                {!readOnly && nextIsWait && step.position >= lockedThrough ? (
                  <div className="flex justify-center">
                    <button
                      type="button"
                      onClick={() => onInsertAfter(step)}
                      disabled={busy}
                      className="my-1 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-40"
                      aria-label={`Insert a step after step ${number}`}
                    >
                      <Plus className="h-3 w-3" aria-hidden="true" />
                      Insert step
                    </button>
                  </div>
                ) : null}
              </li>
            );
          }

          // WAIT connector
          const previousEmail = [...steps.slice(0, index)]
            .reverse()
            .find((s) => s.kind === "EMAIL");
          const nextEmail = steps.slice(index + 1).find((s) => s.kind === "EMAIL");
          const previousLabel = previousEmail
            ? `Step ${emailNumber.get(previousEmail.id)}`
            : "the start";
          const nextLabel = nextEmail ? `Step ${emailNumber.get(nextEmail.id)}` : "the next email";
          const minutes = step.wait_duration_minutes ?? 0;
          const isOrphan =
            index === 0 ||
            index === steps.length - 1 ||
            steps[index - 1]?.kind === "WAIT" ||
            steps[index + 1]?.kind === "WAIT";
          const nextDay = nextEmail ? timings.get(nextEmail.id)?.day : null;

          return (
            <li key={step.id} className="list-none">
              <div className="flex flex-col items-center" role="group" aria-label="Wait">
                <span className="h-4 w-px bg-slate-300" aria-hidden="true" />
                <div className="flex items-center gap-1">
                  {readOnly ? (
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-medium",
                        isOrphan
                          ? "border-amber-300 bg-amber-50 text-amber-800"
                          : "border-slate-200 bg-slate-50 text-slate-700",
                      )}
                    >
                      <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                      Wait {formatDuration(minutes)}
                    </span>
                  ) : (
                    <Popover
                      label={`Edit wait before ${nextLabel}`}
                      className="w-72"
                      trigger={({ toggle, ariaProps }) => (
                        <button
                          type="button"
                          onClick={toggle}
                          {...ariaProps}
                          aria-label={`Edit wait: ${formatDuration(minutes)}`}
                          className={cn(
                            "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-medium hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600",
                            isOrphan
                              ? "border-amber-300 bg-amber-50 text-amber-800"
                              : "border-slate-200 bg-slate-50 text-slate-700",
                          )}
                        >
                          <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                          Wait {formatDuration(minutes)}
                          {savingWaitId === step.id ? " …" : ""}
                          <Pencil className="h-3 w-3 text-slate-400" aria-hidden="true" />
                        </button>
                      )}
                    >
                      {(close) => (
                        <DurationEditor
                          minutes={minutes}
                          subjectLabel={nextLabel}
                          previousLabel={previousLabel}
                          isSaving={savingWaitId === step.id}
                          onCancel={close}
                          onSave={(value) => {
                            onSaveWait(step, value);
                            close();
                          }}
                        />
                      )}
                    </Popover>
                  )}
                  {!readOnly && isOrphan && step.position > lockedThrough ? (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-red-600"
                      aria-label="Remove this wait"
                      title="Remove this wait"
                      disabled={busy}
                      onClick={() => onDelete(step)}
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    </Button>
                  ) : null}
                </div>
                {nextEmail ? (
                  <p className="mt-1 text-xs text-slate-500">
                    {nextLabel} goes out {formatDuration(minutes)} after {previousLabel} is sent
                    {nextDay ? ` (about Day ${nextDay})` : ""}
                  </p>
                ) : null}
                <span className="h-4 w-px bg-slate-300" aria-hidden="true" />
              </div>
            </li>
          );
        })}
      </ol>

      {!readOnly ? (
        <div className="flex justify-center pt-1">
          <Button variant="outline" size="sm" disabled={busy} onClick={onAddEmail}>
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {steps.length === 0 ? "Add email step" : "Add step"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
