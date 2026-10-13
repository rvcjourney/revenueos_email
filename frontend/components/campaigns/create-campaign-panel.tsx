import Link from "next/link";
import { CalendarClock, MailPlus, Plus, Send, Users } from "lucide-react";

import { Button } from "@/components/ui/button";

const steps = [
  {
    icon: Users,
    title: "Pick an audience",
    text: "Choose lead lists or import a CSV.",
  },
  {
    icon: MailPlus,
    title: "Write the sequence",
    text: "Emails and follow-ups, with waits between them.",
  },
  {
    icon: CalendarClock,
    title: "Choose senders and a schedule",
    text: "Nothing sends until you activate it.",
  },
];

// Side panel on the campaigns list: what creating a campaign involves, and the
// way in. Shown only to roles that may draft campaigns.
export function CreateCampaignPanel() {
  return (
    <aside
      aria-label="Create a campaign"
      className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-card"
    >
      <div
        aria-hidden="true"
        className="relative flex h-36 items-center justify-center bg-gradient-to-b from-brand-50 to-white"
      >
        <svg viewBox="0 0 240 110" className="absolute inset-0 h-full w-full" fill="none">
          <path
            d="M28 88c24-46 70-52 92-34s58 20 92-30"
            className="stroke-brand-200"
            strokeWidth="1.5"
            strokeDasharray="4 5"
            strokeLinecap="round"
          />
          <circle cx="28" cy="88" r="4" className="fill-brand-300" />
          <circle cx="212" cy="24" r="4" className="fill-brand-300" />
        </svg>
        <span className="relative flex h-14 w-14 items-center justify-center rounded-2xl bg-white text-brand-600 shadow-card-hover ring-1 ring-brand-100">
          <Send className="h-6 w-6" />
        </span>
      </div>

      <div className="px-5 pb-5">
        <h2 className="text-center text-lg font-bold text-slate-900">Create a campaign</h2>
        <p className="mt-1 text-center text-[13px] leading-5 text-slate-500">
          Three steps from an empty draft to a campaign that is ready to send.
        </p>

        <ol className="mt-5 space-y-4">
          {steps.map((step) => (
            <li key={step.title} className="flex gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
                <step.icon className="h-4 w-4" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-slate-900">{step.title}</p>
                <p className="text-xs leading-5 text-slate-500">{step.text}</p>
              </div>
            </li>
          ))}
        </ol>

        <Button asChild className="mt-6 w-full">
          <Link href="/app/campaigns/new">
            <Plus className="h-4 w-4" aria-hidden="true" />
            Create Campaign
          </Link>
        </Button>
      </div>
    </aside>
  );
}
