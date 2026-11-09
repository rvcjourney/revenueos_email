"use client";

import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2 } from "lucide-react";

import { NewImportPageClient } from "@/app/app/leads/imports/new/new-import-page-client";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiError } from "@/lib/api-client";
import {
  commitAudience,
  getAudienceCaptureStatus,
  getCommittedAudience,
  selectAudience,
} from "@/lib/campaigns-api";
import { getImport } from "@/lib/imports-api";
import { createLeadList } from "@/lib/leads-api";
import type { CampaignAudience, ImportJob } from "@/types/domain";

const POLL_MS = 1500;
const MAX_WAIT_MS = 15 * 60 * 1000;
const LIST_NAME_MAX = 200;

type Phase = "wizard" | "importing" | "capturing" | "done" | "error";

type AudienceCsvImportDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  campaignId: string;
  campaignName: string;
  // Called whenever the audience or the lead lists may have changed.
  onChanged: () => void;
};

function message(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "We couldn't complete that request. Please try again.";
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Each import gets its own list, so importing again never touches a list that
// a previous capture is holding, and the audience is simply "what it was, plus
// this list".
function newListName(campaignName: string) {
  const stamp = new Date().toISOString().slice(0, 19).replace("T", " ");
  const suffix = ` - CSV import ${stamp}`;
  return `${campaignName.slice(0, LIST_NAME_MAX - suffix.length)}${suffix}`;
}

// A pending selection (READY, not yet committed) is kept too; a failed or
// abandoned one is not something the user chose to keep.
function selectionToKeep(audience: CampaignAudience | null) {
  if (audience && (audience.is_committed || audience.status === "READY")) {
    return { lists: audience.selected_list_ids, leads: audience.selected_lead_ids };
  }
  return { lists: [], leads: [] };
}

export function AudienceCsvImportDialog({
  open,
  onOpenChange,
  workspaceId,
  campaignId,
  campaignName,
  onChanged,
}: AudienceCsvImportDialogProps) {
  const [phase, setPhase] = useState<Phase>("wizard");
  const [attempt, setAttempt] = useState(0);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const alive = useRef(true);
  const wasOpen = useRef(false);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Every opening starts at the upload step.
  useEffect(() => {
    if (open && !wasOpen.current) startOver();
    wasOpen.current = open;
  }, [open]);

  function startOver() {
    setPhase("wizard");
    setAttempt((n) => n + 1);
    setProgress("");
    setError(null);
    setResult(null);
  }

  function fail(err: unknown, leadsSaved: boolean) {
    if (!alive.current) return;
    onChanged();
    setError(
      leadsSaved
        ? `${message(err)} The leads from the file are saved in a new lead list, so nothing was lost.`
        : message(err),
    );
    setPhase("error");
  }

  async function waitForImport(job: ImportJob): Promise<ImportJob> {
    const deadline = Date.now() + MAX_WAIT_MS;
    let current = job;
    while (alive.current && Date.now() < deadline) {
      if (
        current.status === "COMPLETED" ||
        current.status === "COMPLETED_WITH_ERRORS" ||
        current.status === "FAILED"
      ) {
        return current;
      }
      setProgress(
        `Importing leads... (${current.processed_rows} of ${current.total_rows ?? "?"} rows)`,
      );
      await sleep(POLL_MS);
      current = await getImport(workspaceId, job.id);
    }
    throw new Error("The import is taking longer than expected.");
  }

  async function waitForCapture(audienceId: string): Promise<CampaignAudience> {
    const deadline = Date.now() + MAX_WAIT_MS;
    while (alive.current && Date.now() < deadline) {
      const audience = await getAudienceCaptureStatus(workspaceId, campaignId, audienceId);
      if (audience.status !== "CAPTURING") return audience;
      setProgress(
        `Adding leads to the campaign... (${audience.processed_count} of ${audience.total_candidates ?? "?"})`,
      );
      await sleep(POLL_MS);
    }
    throw new Error("Capturing the audience is taking longer than expected.");
  }

  async function handleImported(job: ImportJob) {
    const listId = job.list_id;
    let leadsSaved = false;
    setError(null);
    setPhase("importing");
    setProgress("Importing leads...");
    try {
      const finished = await waitForImport(job);
      if (finished.status === "FAILED") {
        throw new Error(finished.failure_summary ?? "The import failed.");
      }
      const leadsInFile = finished.accepted_rows + finished.duplicate_rows;
      if (leadsInFile === 0 || !listId) {
        throw new Error("No valid leads were found in that file, so nothing was added.");
      }
      leadsSaved = true;

      // Read the audience as it is now: it may have changed since this page loaded.
      setPhase("capturing");
      setProgress("Adding leads to the campaign...");
      const keep = selectionToKeep(await getCommittedAudience(workspaceId, campaignId));
      const captured = await selectAudience(workspaceId, campaignId, {
        list_ids: [...new Set([...keep.lists, listId])],
        lead_ids: keep.leads,
      });
      onChanged();

      const settled = await waitForCapture(captured.id);
      if (settled.status !== "READY") {
        throw new Error(
          settled.error_reason ?? `The audience capture ${settled.status.toLowerCase()}.`,
        );
      }
      await commitAudience(workspaceId, campaignId, settled.id);
      onChanged();
      if (!alive.current) return;
      setResult(
        `Imported ${finished.accepted_rows} new lead${finished.accepted_rows === 1 ? "" : "s"}` +
          (finished.duplicate_rows > 0 ? ` (${finished.duplicate_rows} already existed)` : "") +
          (finished.rejected_rows > 0 ? `, ${finished.rejected_rows} rows skipped` : "") +
          `. The campaign audience now has ${settled.accepted_count ?? 0} eligible lead${settled.accepted_count === 1 ? "" : "s"}.`,
      );
      setPhase("done");
    } catch (err) {
      fail(err, leadsSaved);
    }
  }

  const busy = phase === "importing" || phase === "capturing";

  return (
    <Dialog
      open={open}
      onRequestClose={() => {
        if (!busy) onOpenChange(false);
      }}
      title="Import leads from CSV"
      className="max-w-3xl"
    >
      <div className="space-y-4 p-1">
        {phase === "wizard" && (
          <>
            <p className="px-1 text-sm text-slate-500">
              The leads in your file are added to this campaign&apos;s audience, on top of
              whatever is already selected. You can import as many files as you like.
            </p>
            <NewImportPageClient
              key={attempt}
              defaultImportKind="LEADS"
              showHeading={false}
              createTargetList={async () =>
                (await createLeadList(workspaceId, newListName(campaignName))).id
              }
              onImported={(job) => void handleImported(job)}
            />
          </>
        )}

        {busy && (
          <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900">
            <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
            <div>
              <p className="font-medium">{progress}</p>
              <p className="mt-0.5 text-xs text-blue-800">
                Keep this page open until it finishes.
              </p>
            </div>
          </div>
        )}

        {phase === "done" && (
          <div className="space-y-4">
            <div className="flex items-start gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              <p>{result}</p>
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={startOver}>
                Import another file
              </Button>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Done
              </Button>
            </div>
          </div>
        )}

        {phase === "error" && (
          <div className="space-y-4">
            <Alert variant="error">{error}</Alert>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              <Button type="button" onClick={startOver}>
                Try another file
              </Button>
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}
