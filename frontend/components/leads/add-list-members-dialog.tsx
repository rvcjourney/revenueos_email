"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { LeadPicker } from "@/components/leads/lead-picker";
import { addLeadsToListWithProgress } from "@/lib/bulk-add-to-list";
import { errorMessage } from "@/lib/errors";
import { useWorkspace } from "@/lib/workspace-context";

type AddListMembersDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  listId: string;
  listName: string;
};

export function AddListMembersDialog({
  open,
  onOpenChange,
  listId,
  listName,
}: AddListMembersDialogProps) {
  const queryClient = useQueryClient();
  const { activeWorkspaceId } = useWorkspace();
  const { toast } = useToast();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ processed: number; total: number } | null>(
    null,
  );
  const [submitting, setSubmitting] = useState(false);

  function close() {
    setSearch("");
    setSelected(new Set());
    setError(null);
    setProgress(null);
    onOpenChange(false);
  }

  async function handleSubmit() {
    if (!activeWorkspaceId || selected.size === 0) return;
    setError(null);
    setSubmitting(true);
    const leadIds = [...selected];
    setProgress({ processed: 0, total: leadIds.length });
    try {
      const result = await addLeadsToListWithProgress(activeWorkspaceId, listId, leadIds, {
        onProgress: (p) => setProgress(p),
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-list", listId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "lead-list-members", listId],
      });
      queryClient.invalidateQueries({ queryKey: ["workspace", activeWorkspaceId, "leads"] });
      toast(
        result.failed === 0
          ? `Added ${result.succeeded} lead${result.succeeded === 1 ? "" : "s"} to ${listName}.`
          : `Added ${result.succeeded}, ${result.failed} could not be added.`,
        result.failed > 0 ? "error" : undefined,
      );
      close();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onRequestClose={() => {
        if (!submitting) close();
      }}
      title={`Add leads to ${listName}`}
      align="center"
      className="max-w-2xl"
      footer={
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm text-slate-500" aria-live="polite">
            {submitting && progress
              ? `Adding ${progress.processed} of ${progress.total}…`
              : `${selected.size} selected`}
          </span>
          <div className="flex gap-2">
            <Button type="button" variant="outline" disabled={submitting} onClick={close}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={selected.size === 0 || submitting}
              onClick={handleSubmit}
            >
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
              Add {selected.size > 0 ? selected.size : ""} lead{selected.size === 1 ? "" : "s"}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-4 p-4">
        {error ? <Alert>{error}</Alert> : null}
        <LeadPicker
          enabled={open}
          search={search}
          onSearchChange={setSearch}
          selected={selected}
          onSelectedChange={setSelected}
          disabled={submitting}
        />
      </div>
    </Dialog>
  );
}
