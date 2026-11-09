import * as React from "react";
import {
  AlertCircle,
  Archive,
  CheckCircle2,
  Clock,
  Pause,
  PencilLine,
  Play,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { CampaignStatus } from "@/types/domain";

export type Tone = "neutral" | "brand" | "success" | "warning" | "danger" | "muted";

const toneClasses: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-600",
  brand: "bg-brand-50 text-brand-700",
  success: "bg-emerald-50 text-emerald-700",
  warning: "bg-amber-50 text-amber-700",
  danger: "bg-red-50 text-red-700",
  muted: "bg-slate-100 text-slate-500",
};

// Generic pill. Always carries text (and usually an icon) so status is never
// conveyed by color alone.
export function StatusBadge({
  tone = "neutral",
  icon,
  children,
  className,
}: {
  tone?: Tone;
  icon?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-semibold tracking-wide [&>svg]:h-3 [&>svg]:w-3 [&>svg]:shrink-0",
        toneClasses[tone],
        className,
      )}
    >
      {icon}
      {children}
    </span>
  );
}

type CampaignStatusMeta = { tone: Tone; icon: React.ReactNode };

export const campaignStatusMeta: Record<CampaignStatus, CampaignStatusMeta> = {
  DRAFT: { tone: "neutral", icon: <PencilLine aria-hidden="true" /> },
  SCHEDULED: { tone: "brand", icon: <Clock aria-hidden="true" /> },
  RUNNING: { tone: "success", icon: <Play aria-hidden="true" /> },
  PAUSED: { tone: "warning", icon: <Pause aria-hidden="true" /> },
  ERROR: { tone: "danger", icon: <AlertCircle aria-hidden="true" /> },
  COMPLETED: { tone: "brand", icon: <CheckCircle2 aria-hidden="true" /> },
  ARCHIVED: { tone: "muted", icon: <Archive aria-hidden="true" /> },
};

// Renders the raw status code as text.
export function CampaignStatusBadge({
  status,
  className,
}: {
  status: CampaignStatus;
  className?: string;
}) {
  // Tolerate a status code this build doesn't know about yet.
  const meta = campaignStatusMeta[status] ?? { tone: "neutral" as const, icon: null };
  return (
    <StatusBadge tone={meta.tone} icon={meta.icon} className={className}>
      {status}
    </StatusBadge>
  );
}

// Background import job states (list and detail pages share the mapping).
export function ImportStatusBadge({ status }: { status: string }) {
  const tone: Tone =
    status === "COMPLETED"
      ? "success"
      : status === "FAILED"
        ? "danger"
        : status === "COMPLETED_WITH_ERRORS"
          ? "warning"
          : "brand";
  return <StatusBadge tone={tone}>{status}</StatusBadge>;
}
