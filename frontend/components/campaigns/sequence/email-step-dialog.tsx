"use client";

import * as React from "react";
import { FileText, Loader2, Mail } from "lucide-react";

import { ContentGuide } from "@/components/email-editor/content-guide";
import { EmailPreviewPane } from "@/components/email-editor/email-preview-pane";
import {
  cidToPreviewHtml,
  removeCidImages,
  roundTripsCleanly,
} from "@/components/email-editor/email-html";
import {
  EmailBodyEditor,
  type EditorMode,
  type EmailBodyEditorHandle,
} from "@/components/email-editor/rich-text-editor";
import { TemplatePicker, type PickedTemplate } from "@/components/email-editor/template-picker";
import { useEmailPreview } from "@/components/email-editor/use-email-preview";
import { usePreviewRecipients } from "@/components/email-editor/use-preview-recipients";
import { VariablePicker } from "@/components/email-editor/variable-picker";
import {
  draftFromStep,
  useStepDraft,
  useUnsavedChangesGuard,
} from "@/components/campaigns/sequence/use-step-draft";
import { waitMinutesForDay } from "@/components/campaigns/sequence/duration";
import { validateFileForUpload } from "@/components/campaigns/sequence/attachment-rules";
import {
  AttachmentsBar,
  UploadButtons,
} from "@/components/campaigns/sequence/attachments-bar";
import {
  sameThreadErrorMessage,
  UNKNOWN_REPLY_SUBJECT,
} from "@/components/campaigns/sequence/same-thread";
import { SaveAsTemplate } from "@/components/campaigns/sequence/save-as-template";
import { TestSendControl } from "@/components/campaigns/sequence/test-send-control";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api-client";
import {
  deleteStepAttachment,
  getStepAttachmentUrl,
  updateSequenceStep,
  uploadStepAttachment,
} from "@/lib/campaigns-api";
import { insertAtCursor } from "@/components/email-editor/email-html";
import type { CampaignMailbox, SequenceStep, StepAttachment } from "@/types/domain";

type Props = {
  open: boolean;
  workspaceId: string;
  campaignId: string;
  step: SequenceStep;
  stepNumber: number;
  // Current "Day N" of this step and of the email before it (null for step 1).
  day: number;
  previousEmailDay: number | null;
  precedingWait: SequenceStep | null;
  // Subject this step carries when sent in the same thread ("Re: …").
  replySubject?: string;
  readOnly: boolean;
  // campaigns.execute: test emails go to real inboxes, so managers and above.
  canTestSend: boolean;
  // Hyper-personalized campaign: this email is the reference every lead's version
  // is written from, not the text that is sent (ADR-0011).
  referenceMode?: boolean;
  // The campaign's emails use the brand layout (HTML style, ADR-0017). The
  // visual editor can't represent that layout and would flatten it, so this
  // email is edited as source only.
  designedEmail?: boolean;
  mailboxes: CampaignMailbox[];
  onClose: () => void;
  // Called with the server's copy of whatever was saved.
  onSaved: (saved: { step?: SequenceStep; wait?: SequenceStep }) => void;
  // Re-fetches the sequence and returns the latest copy of this step (and its
  // preceding wait) for conflict recovery.
  onRefetch: () => Promise<{ step: SequenceStep | null; wait: SequenceStep | null }>;
  // The server said the campaign is no longer editable.
  onLocked: () => void;
};

type Banner = { kind: "error" | "warning" | "info"; text: string } | null;

function describeError(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return "We couldn’t save your changes. Check your connection and try again.";
}

export function EmailStepDialog({
  open,
  workspaceId,
  campaignId,
  step,
  stepNumber,
  day,
  previousEmailDay,
  precedingWait,
  replySubject = UNKNOWN_REPLY_SUBJECT,
  readOnly,
  canTestSend,
  referenceMode = false,
  designedEmail = false,
  mailboxes,
  onClose,
  onSaved,
  onRefetch,
  onLocked,
}: Props) {
  const initial = React.useMemo(() => draftFromStep(step, day), [step, day]);
  const { draft, baseline, dirty, update, reset, markSaved } = useStepDraft(initial);
  const [mode, setModeState] = React.useState<EditorMode>(() =>
    !designedEmail && roundTripsCleanly(step.email_body_html ?? "") ? "visual" : "source",
  );
  // A designed email stays in source mode: switching to visual would silently
  // rewrite the layout (tables, colours, button) into plain paragraphs.
  const setMode = React.useCallback(
    (next: EditorMode) => {
      if (designedEmail) return;
      setModeState(next);
    },
    [designedEmail],
  );
  const [showPreheader, setShowPreheader] = React.useState(Boolean(step.email_preheader));
  const [rightTab, setRightTab] = React.useState("preview");
  const [saving, setSaving] = React.useState(false);
  const [banner, setBanner] = React.useState<Banner>(() =>
    designedEmail
      ? {
          kind: "info",
          text: "This is a designed email in your brand's style, so it opens as source. Edit the text inside the HTML, or regenerate it from the email list. The visual editor can't show this layout.",
        }
      : roundTripsCleanly(step.email_body_html ?? "")
        ? null
        : {
          kind: "info",
          text: "This email uses HTML the visual editor can’t reproduce exactly, so it opened as source. Switching to the visual editor may simplify it.",
        },
  );
  const [conflict, setConflict] = React.useState(false);
  const [pickedTemplate, setPickedTemplate] = React.useState<PickedTemplate | null>(null);
  const [versionOverride, setVersionOverride] = React.useState<{
    step?: number;
    wait?: number;
  }>({});
  const [fromId, setFromId] = React.useState(() => mailboxes[0]?.mailbox_id ?? "");
  const [recipientIndex, setRecipientIndex] = React.useState(0);
  const subjectRef = React.useRef<HTMLInputElement>(null);
  const preheaderRef = React.useRef<HTMLInputElement>(null);
  const editorRef = React.useRef<EmailBodyEditorHandle>(null);
  // Files are stored the moment they are uploaded (they are not part of the
  // text draft), so they are tracked separately from `draft`.
  const [attachments, setAttachments] = React.useState<StepAttachment[]>(
    step.attachments ?? [],
  );
  const [imageUrls, setImageUrls] = React.useState<Record<string, string>>({});
  const [uploading, setUploading] = React.useState(false);
  const [removingId, setRemovingId] = React.useState<string | null>(null);

  // Private files have no public URL: fetch a short-lived one per inline image so
  // it can be shown while editing and in the preview.
  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    for (const attachment of attachments) {
      if (attachment.disposition !== "INLINE" || imageUrls[attachment.content_id]) continue;
      getStepAttachmentUrl(workspaceId, campaignId, step.id, attachment.id)
        .then(({ url }) => {
          if (!cancelled) setImageUrls((prev) => ({ ...prev, [attachment.content_id]: url }));
        })
        .catch(() => {
          // The image just shows as blank; saving and sending don't depend on it.
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, attachments, workspaceId, campaignId, step.id]);

  useUnsavedChangesGuard(open && dirty);

  const {
    recipients,
    usingSample,
    total: totalRecipients,
    error: recipientsError,
  } = usePreviewRecipients({ workspaceId, campaignId, enabled: open, index: recipientIndex });
  const safeIndex = Math.min(recipientIndex, recipients.length - 1);
  const recipient = recipients[safeIndex];
  // The first email has nothing to reply to.
  const threaded = previousEmailDay !== null && draft.sameThread;
  const preview = useEmailPreview({
    workspaceId,
    subject: threaded ? replySubject : draft.subject,
    preheader: draft.preheader,
    bodyHtml: draft.bodyHtml,
    recipient,
    enabled: open,
  });

  const dayNumber = Number(draft.day);
  const dayChanged = precedingWait !== null && draft.day !== String(day);
  const dayError =
    precedingWait === null || !dayChanged
      ? null
      : previousEmailDay !== null && waitMinutesForDay(previousEmailDay, dayNumber) === null
        ? `Choose a whole day after Day ${previousEmailDay} (up to 365 days later).`
        : null;
  const subjectEmpty = draft.subject.trim() === "";
  // A threaded step takes the previous email's subject, so it needs none of its own.
  const subjectMissing = subjectEmpty && !threaded;
  const bodyEmpty = !/<img\b/i.test(draft.bodyHtml) && stripTags(draft.bodyHtml) === "";

  function requestClose() {
    if (dirty && !readOnly && !window.confirm("Discard your unsaved changes?")) return;
    onClose();
  }

  function insertIntoInput(
    ref: React.RefObject<HTMLInputElement | null>,
    field: "subject" | "preheader",
    code: string,
  ) {
    const el = ref.current;
    const current = draft[field];
    const result = insertAtCursor(current, el?.selectionStart ?? null, el?.selectionEnd ?? null, code);
    update({ [field]: result.value });
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(result.caret, result.caret);
    });
  }

  function applyTemplate(template: PickedTemplate) {
    const hasContent = draft.subject.trim() !== "" || !bodyEmpty;
    if (
      hasContent &&
      !window.confirm(`Replace the current subject and body with the "${template.name}" template?`)
    ) {
      return;
    }
    update({
      subject: template.subject,
      bodyHtml: template.bodyHtml,
      preheader: template.preheader,
    });
    setShowPreheader(Boolean(template.preheader));
    setPickedTemplate(template);
    setBanner({
      kind: "info",
      text: `Started from the "${template.name}" template. Your edits stay in this step and don’t change the template.`,
    });
  }

  async function save() {
    if (readOnly || saving) return;
    setBanner(null);
    setConflict(false);
    if (subjectMissing) {
      setBanner({ kind: "error", text: "Add a subject before saving." });
      return;
    }
    if (dayError) {
      setBanner({ kind: "error", text: dayError });
      return;
    }
    setSaving(true);
    const threadChanged = draft.sameThread !== baseline.sameThread;
    let emailSaved: SequenceStep | undefined;
    try {
      // Compared with what the draft started from (not the live prop), so a
      // background refetch can't make a real edit look like "no change".
      const contentChanged =
        draft.subject !== baseline.subject ||
        draft.preheader !== baseline.preheader ||
        draft.bodyHtml !== baseline.bodyHtml ||
        threadChanged ||
        pickedTemplate !== null;
      if (contentChanged) {
        emailSaved = await updateSequenceStep(workspaceId, campaignId, step.id, {
          expected_version: versionOverride.step ?? step.version,
          // A threaded step may have no subject; omitted keeps the saved one.
          ...(subjectEmpty ? {} : { email_subject: draft.subject.trim() }),
          email_body_html: draft.bodyHtml,
          // "" clears; omitted would leave it unchanged.
          email_preheader: draft.preheader.trim(),
          ...(pickedTemplate?.templateVersionId
            ? { source_template_version_id: pickedTemplate.templateVersionId }
            : {}),
          ...(threadChanged ? { email_same_thread: draft.sameThread } : {}),
        });
        markSaved({
          subject: emailSaved.email_subject ?? "",
          preheader: emailSaved.email_preheader ?? "",
          bodyHtml: emailSaved.email_body_html ?? "",
          sameThread: emailSaved.email_same_thread ?? false,
        });
        onSaved({ step: emailSaved });
      }
      if (dayChanged && precedingWait && previousEmailDay !== null) {
        const minutes = waitMinutesForDay(previousEmailDay, dayNumber);
        if (minutes !== null) {
          try {
            const waitSaved = await updateSequenceStep(workspaceId, campaignId, precedingWait.id, {
              expected_version: versionOverride.wait ?? precedingWait.version,
              wait_duration_minutes: minutes,
            });
            markSaved({ day: draft.day });
            onSaved({ wait: waitSaved });
          } catch (error) {
            // The email part is already saved; say so instead of implying
            // nothing happened.
            setBanner({
              kind: "error",
              text: `${emailSaved ? "Your email was saved, but the timing" : "The timing"} couldn’t be updated: ${describeError(error)}`,
            });
            if (error instanceof ApiError && error.code === "conflict") setConflict(true);
            return;
          }
        }
      }
      onClose();
    } catch (error) {
      if (error instanceof ApiError && error.code === "conflict") {
        setConflict(true);
        setBanner({
          kind: "warning",
          text: "This step was changed somewhere else since you opened it.",
        });
      } else if (error instanceof ApiError && error.code === "state_conflict") {
        setBanner({
          kind: "error",
          text: "This campaign is no longer a draft or paused, so the sequence can’t be edited. Your text is still here if you want to copy it.",
        });
        onLocked();
      } else {
        setBanner({
          kind: "error",
          text: sameThreadErrorMessage(error, threadChanged) ?? describeError(error),
        });
      }
    } finally {
      setSaving(false);
    }
  }

  async function uploadFile(file: File, disposition: "ATTACHMENT" | "INLINE") {
    if (readOnly || uploading) return;
    const problem = validateFileForUpload(file, disposition, attachments);
    if (problem) {
      setBanner({ kind: "error", text: problem });
      return;
    }
    setBanner(null);
    setUploading(true);
    try {
      const saved = await uploadStepAttachment(workspaceId, campaignId, step.id, file, disposition);
      setAttachments((prev) => (prev.some((a) => a.id === saved.id) ? prev : [...prev, saved]));
      if (disposition === "INLINE") {
        let displayUrl: string | undefined;
        try {
          displayUrl = (await getStepAttachmentUrl(workspaceId, campaignId, step.id, saved.id)).url;
          setImageUrls((prev) => ({ ...prev, [saved.content_id]: displayUrl as string }));
        } catch {
          // Insert anyway; the picture shows once its URL can be fetched.
        }
        editorRef.current?.insertImage(saved.content_id, file.name, displayUrl);
      }
    } catch (error) {
      if (error instanceof ApiError && error.code === "state_conflict") {
        setBanner({
          kind: "error",
          text: "This campaign is no longer a draft or paused, so files can't be added.",
        });
        onLocked();
      } else {
        setBanner({ kind: "error", text: describeError(error) });
      }
    } finally {
      setUploading(false);
    }
  }

  async function removeAttachment(attachment: StepAttachment) {
    if (readOnly) return;
    const inBody = attachment.disposition === "INLINE";
    if (
      inBody &&
      !window.confirm(`Remove "${attachment.filename}"? It will also be removed from the email body.`)
    ) {
      return;
    }
    setRemovingId(attachment.id);
    try {
      await deleteStepAttachment(workspaceId, campaignId, step.id, attachment.id);
      setAttachments((prev) => prev.filter((a) => a.id !== attachment.id));
      if (inBody) update({ bodyHtml: removeCidImages(draft.bodyHtml, attachment.content_id) });
    } catch (error) {
      setBanner({ kind: "error", text: describeError(error) });
    } finally {
      setRemovingId(null);
    }
  }

  async function reloadLatest() {
    const latest = await onRefetch();
    if (latest.step) {
      reset(draftFromStep(latest.step, day));
      setPickedTemplate(null);
    }
    setVersionOverride({});
    setConflict(false);
    setBanner({ kind: "info", text: "Loaded the latest saved version." });
  }

  async function keepMyEdits() {
    const latest = await onRefetch();
    setVersionOverride({ step: latest.step?.version, wait: latest.wait?.version });
    setConflict(false);
    setBanner({
      kind: "info",
      text: "Your edits will replace the newer version when you press Save.",
    });
  }

  const fromOptions = mailboxes.map((mb) => ({
    id: mb.mailbox_id,
    label: mb.sender_display_name ? `${mb.sender_display_name} <${mb.email_address}>` : mb.email_address,
  }));

  const bodyUsesUploads = draft.bodyHtml.includes("cid:");
  const testSendBlockedReason = !canTestSend
    ? "Only managers, admins and owners can send test emails."
    : mailboxes.length === 0
      ? "Assign a sender to this campaign to send a test email."
      : subjectMissing
        ? "Add a subject to send a test email."
        : null;

  return (
    <Dialog
      open={open}
      onRequestClose={requestClose}
      title={`Step ${stepNumber}`}
      headerContent={
        <span className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-700">
          <Mail className="h-4 w-4" aria-hidden="true" />
          {referenceMode ? "Reference email" : "Email"}
        </span>
      }
      footer={
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_26rem] lg:items-center">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="flex items-center gap-2 text-sm">
              <label htmlFor="start-day" className="font-medium text-slate-700">
                Start this step on Day
              </label>
              <input
                id="start-day"
                type="number"
                min={1}
                step={1}
                value={draft.day}
                disabled={readOnly || precedingWait === null}
                onChange={(event) => update({ day: event.target.value })}
                aria-invalid={Boolean(dayError)}
                aria-describedby="start-day-hint"
                className="h-9 w-16 rounded-md border border-slate-200 px-2 text-sm disabled:bg-slate-50"
              />
            </div>
            {previousEmailDay !== null ? (
              <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
                <input
                  type="checkbox"
                  checked={draft.sameThread}
                  disabled={readOnly}
                  onChange={(event) => update({ sameThread: event.target.checked })}
                  className="h-4 w-4 rounded border-slate-300"
                />
                Send in same thread
              </label>
            ) : null}
            <p
              id="start-day-hint"
              className={dayError ? "text-xs text-red-600" : "text-xs text-slate-500"}
            >
              {dayError ??
                (precedingWait === null
                  ? "This step is scheduled as soon as the campaign starts."
                  : "Counted from the first email. Changes the wait before this step.")}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {!readOnly ? (
              <SaveAsTemplate
                workspaceId={workspaceId}
                subject={draft.subject}
                preheader={draft.preheader}
                bodyHtml={draft.bodyHtml}
                disabled={subjectEmpty || saving || bodyUsesUploads}
                disabledReason={
                  bodyUsesUploads
                    ? "Templates can't include uploaded images. Remove them to save a template."
                    : undefined
                }
              />
            ) : null}
            <Button variant="outline" onClick={requestClose}>
              {readOnly ? "Close" : "Cancel"}
            </Button>
            {!readOnly ? (
              <Button onClick={() => void save()} disabled={saving || (!dirty && !pickedTemplate)}>
                {saving ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
                Save
              </Button>
            ) : null}
          </div>
        </div>
      }
    >
      <div className="grid min-h-full lg:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="space-y-4 p-4">
          {readOnly ? (
            <Alert variant="info">
              This sequence can&apos;t be edited now. Duplicate the campaign to make changes.
            </Alert>
          ) : null}
          {referenceMode ? (
            <Alert variant="info">
              This is your reference email: the message, offer and call to action you want to
              communicate. Each lead receives a version personalized to them, written just before
              it is sent. Variables like {"{{first_name}}"} are filled in first. Review generated
              samples in the Preview and approve section of the Sequence tab.
            </Alert>
          ) : null}
          {banner ? <Alert variant={banner.kind}>{banner.text}</Alert> : null}
          {conflict ? (
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => void reloadLatest()}>
                Reload latest
              </Button>
              <Button size="sm" variant="outline" onClick={() => void keepMyEdits()}>
                Keep my edits
              </Button>
            </div>
          ) : null}

          <div>
            {threaded ? (
              <p className="flex h-11 items-center rounded-md border border-slate-200 bg-slate-50 px-3 text-sm text-slate-700">
                {`Subject: ${replySubject}`}
              </p>
            ) : (
              <>
                <label htmlFor="step-subject" className="sr-only">
                  Subject
                </label>
                <div className="flex items-center rounded-md border border-slate-200 bg-white focus-within:ring-2 focus-within:ring-brand-600">
                  <span className="border-r border-slate-200 px-3 text-sm font-semibold text-slate-700">
                    Subject
                  </span>
                  <input
                    id="step-subject"
                    ref={subjectRef}
                    value={draft.subject}
                    onChange={(event) => update({ subject: event.target.value })}
                    readOnly={readOnly}
                    maxLength={500}
                    placeholder="Enter a subject…"
                    aria-invalid={subjectEmpty}
                    className="h-11 min-w-0 flex-1 bg-transparent px-3 text-sm outline-none"
                  />
                  <VariablePicker
                    label="Insert variable in subject"
                    disabled={readOnly}
                    onInsert={(code) => insertIntoInput(subjectRef, "subject", code)}
                    className="mr-1"
                  />
                </div>
              </>
            )}
            <div className="mt-1.5 flex justify-end">
              <button
                type="button"
                onClick={() => setShowPreheader((value) => !value)}
                className="text-xs font-medium text-brand-600 hover:underline"
                aria-expanded={showPreheader}
              >
                {showPreheader ? "Hide pre-header" : "Set pre-header"}
              </button>
            </div>
            {showPreheader ? (
              <div className="mt-1 flex items-center rounded-md border border-slate-200 bg-white focus-within:ring-2 focus-within:ring-brand-600">
                <label
                  htmlFor="step-preheader"
                  className="border-r border-slate-200 px-3 text-sm font-semibold text-slate-700"
                >
                  Pre-header
                </label>
                <input
                  id="step-preheader"
                  ref={preheaderRef}
                  value={draft.preheader}
                  onChange={(event) => update({ preheader: event.target.value })}
                  readOnly={readOnly}
                  maxLength={255}
                  placeholder="Preview text shown after the subject in the inbox"
                  className="h-10 min-w-0 flex-1 bg-transparent px-3 text-sm outline-none"
                />
                <VariablePicker
                  label="Insert variable in pre-header"
                  disabled={readOnly}
                  onInsert={(code) => insertIntoInput(preheaderRef, "preheader", code)}
                  className="mr-1"
                />
              </div>
            ) : null}
          </div>

          <EmailBodyEditor
            ref={editorRef}
            id="step-body"
            imageUrls={imageUrls}
            value={draft.bodyHtml}
            onChange={(html) => update({ bodyHtml: html })}
            mode={mode}
            onModeChange={setMode}
            readOnly={readOnly}
            placeholder="Write your email…"
            onLossy={() =>
              setBanner({
                kind: "warning",
                text: "Some HTML isn’t supported by the visual editor and was simplified. Check the result before saving.",
              })
            }
            toolbarExtras={
              readOnly ? null : (
                <>
                  <TemplatePicker workspaceId={workspaceId} onPick={applyTemplate} />
                  <UploadButtons
                    disabled={saving}
                    uploading={uploading}
                    imagesDisabled={referenceMode}
                    onFile={uploadFile}
                  />
                </>
              )
            }
          />
          <AttachmentsBar
            attachments={attachments}
            readOnly={readOnly}
            removingId={removingId}
            onRemove={removeAttachment}
          />
          {!subjectMissing && bodyEmpty ? (
            <p className="text-xs text-amber-700">
              The body is empty. It needs content before the campaign can be activated.
            </p>
          ) : null}
        </div>

        <div className="border-t border-slate-200 lg:border-l lg:border-t-0">
          <div className="px-4 pt-3">
            <Tabs
              idPrefix="step-side"
              label="Email tools"
              value={rightTab}
              onValueChange={setRightTab}
              tabs={[
                {
                  id: "guide",
                  label: "Content Guide",
                  icon: <FileText className="h-4 w-4" aria-hidden="true" />,
                },
                {
                  id: "preview",
                  label: "Email Preview",
                  icon: <Mail className="h-4 w-4" aria-hidden="true" />,
                },
              ]}
            />
          </div>
          <TabPanel idPrefix="step-side" id="guide" value={rightTab}>
            <ContentGuide preview={preview} stepNumber={stepNumber} />
          </TabPanel>
          <TabPanel idPrefix="step-side" id="preview" value={rightTab}>
            <EmailPreviewPane
              preview={preview}
              subjectIsEmpty={subjectMissing}
              bodyIsEmpty={bodyEmpty}
              fromOptions={fromOptions}
              fromId={fromId}
              onFromChange={setFromId}
              sendersHref={`/app/campaigns/${campaignId}/senders`}
              recipients={recipients}
              recipientIndex={safeIndex}
              onRecipientIndexChange={setRecipientIndex}
              usingSample={usingSample}
              totalRecipients={totalRecipients}
              recipientsError={recipientsError}
              mapBody={(html) => cidToPreviewHtml(html, imageUrls)}
            />
          </TabPanel>
          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-200 p-3">
            {!referenceMode && draft.sameThread !== baseline.sameThread ? (
              <p className="text-xs text-slate-500">
                Test sends use the saved &ldquo;Send in same thread&rdquo; setting. Save to test
                your change.
              </p>
            ) : null}
            {referenceMode ? (
              <p className="text-xs text-slate-500">
                Test sends are not available for a reference email: it is not what your leads
                receive. Generate samples in the Preview and approve section instead.
              </p>
            ) : (
            <TestSendControl
              workspaceId={workspaceId}
              campaignId={campaignId}
              stepId={step.id}
              mailboxId={fromId}
              fromLabel={fromOptions.find((o) => o.id === fromId)?.label ?? ""}
              audienceMemberId={usingSample ? null : recipient.id}
              subject={draft.subject}
              preheader={draft.preheader}
              bodyHtml={draft.bodyHtml}
              blockedReason={testSendBlockedReason}
            />
            )}
          </div>
        </div>
      </div>
    </Dialog>
  );
}

function stripTags(html: string) {
  return html
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/g, " ")
    .trim();
}
