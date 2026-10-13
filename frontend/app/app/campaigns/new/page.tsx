"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, Loader2, Sparkles } from "lucide-react";
import Link from "next/link";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api-client";
import { createCampaign } from "@/lib/campaigns-api";
import { getPersonalizationCapabilities } from "@/lib/personalization-api";
import { canDraftCampaign } from "@/lib/permissions";
import { useWorkspace } from "@/lib/workspace-context";
import type { CampaignType } from "@/types/domain";

export default function NewCampaignPage() {
  const router = useRouter();
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [campaignType, setCampaignType] = useState<CampaignType>("STANDARD");

  // Hyper-personalized campaigns exist only where the operator enabled them.
  const capabilities = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "personalization", "capabilities"],
    queryFn: () => getPersonalizationCapabilities(activeWorkspaceId as string),
    enabled: Boolean(activeWorkspaceId),
  });
  const hyperAvailable = capabilities.data?.enabled === true;

  const mayDraft = canDraftCampaign(activeWorkspace?.role_code);

  const createMutation = useMutation({
    mutationFn: () => {
      if (!activeWorkspaceId) throw new Error("No active workspace");
      return createCampaign(activeWorkspaceId, {
        name,
        description: description.trim() ? description : null,
        campaign_type: campaignType,
      });
    },
    onSuccess: (campaign) => {
      // Only Basics are collected here -- the rest of configuration happens
      // across the campaign's own tabs, saved incrementally, never in one
      // giant up-front transaction.
      router.push(`/app/campaigns/${campaign.id}/audience`);
    },
    onError: (err) => {
      setFormError(
        err instanceof ApiError ? err.message : "Failed to create campaign.",
      );
    },
  });

  if (!mayDraft) {
    return (
      <div className="mx-auto max-w-xl py-12 text-center">
        <p className="text-sm text-slate-600">
          You do not have permission to create campaigns in this workspace.
        </p>
        <Button asChild variant="outline" className="mt-4">
          <Link href="/app/campaigns">Back to Campaigns</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <div>
        <Link
          href="/app/campaigns"
          className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Campaigns
        </Link>
        <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-2">
          Create Campaign
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Start with a name. You&apos;ll configure the audience, sequence, senders,
          and schedule next.
        </p>
      </div>

      {formError && <Alert variant="error">{formError}</Alert>}

      <form
        className="space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-card"
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim()) {
            setFormError("Campaign name is required.");
            return;
          }
          setFormError(null);
          createMutation.mutate();
        }}
      >
        <Field label="Campaign name" required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Q1 Outbound Sequence"
            maxLength={200}
            autoFocus
          />
        </Field>
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-slate-900">Campaign type</legend>
          {(
            [
              {
                value: "STANDARD" as const,
                title: "Standard",
                text: "Send the same email to every lead, with variables like {{first_name}} filled in.",
                available: true,
              },
              {
                value: "HYPER_PERSONALIZED" as const,
                title: "Hyper-Personalized",
                text: "Write one reference email. Every lead gets a version written for them from what we know about them, just before it is sent.",
                available: hyperAvailable,
              },
            ] as const
          ).map((option) => (
            <label
              key={option.value}
              className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm ${
                campaignType === option.value
                  ? "border-brand-500 bg-brand-50"
                  : "border-slate-200 bg-white"
              } ${option.available ? "" : "cursor-not-allowed opacity-60"}`}
            >
              <input
                type="radio"
                name="campaign_type"
                value={option.value}
                checked={campaignType === option.value}
                disabled={!option.available}
                onChange={() => setCampaignType(option.value)}
                className="mt-1"
              />
              <span>
                <span className="flex items-center gap-1.5 font-medium text-slate-900">
                  {option.value === "HYPER_PERSONALIZED" ? (
                    <Sparkles className="h-3.5 w-3.5 text-violet-600" aria-hidden="true" />
                  ) : null}
                  {option.title}
                </span>
                <span className="mt-0.5 block text-slate-600">{option.text}</span>
                {option.value === "HYPER_PERSONALIZED" && !option.available ? (
                  <span className="mt-1 block text-xs text-slate-500">
                    {capabilities.isLoading
                      ? "Checking availability…"
                      : "Not enabled for this deployment."}
                  </span>
                ) : null}
              </span>
            </label>
          ))}
          <p className="text-xs text-slate-500">The type can&apos;t be changed after creation.</p>
        </fieldset>
        <Field label="Description">
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional context for your team"
            maxLength={2000}
          />
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" asChild>
            <Link href="/app/campaigns">Cancel</Link>
          </Button>
          <Button type="submit" disabled={createMutation.isPending}>
            {createMutation.isPending && (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
            )}
            Create Campaign
          </Button>
        </div>
      </form>
    </div>
  );
}
