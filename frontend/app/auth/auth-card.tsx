import type { LucideIcon } from "lucide-react";
import { Inbox, Repeat, ShieldCheck } from "lucide-react";

import { Logo } from "@/components/brand/logo";
import { cn } from "@/lib/utils";

const highlights = [
  { icon: Repeat, text: "Multi-step sequences that follow up for you" },
  { icon: Inbox, text: "One inbox for every reply, across mailboxes" },
  { icon: ShieldCheck, text: "Deliverability insight before problems cost you replies" },
];

// Shared shell for every signed-out screen: brand panel on wide screens, a
// single focused card everywhere. Pages pass content only.
export function AuthCard({
  icon: Icon,
  title,
  subtitle,
  children,
  width = "sm",
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  width?: "sm" | "md" | "lg";
}) {
  return (
    <div className="min-h-screen bg-white lg:grid lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <aside className="relative hidden overflow-hidden bg-ink-900 p-12 text-white lg:flex lg:flex-col">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -right-32 -top-32 h-96 w-96 rounded-full bg-brand-600/40 blur-3xl"
        />
        <Logo tone="light" size="md" className="relative" />
        <div className="relative my-auto max-w-md">
          <p className="text-4xl font-bold leading-tight tracking-tight">
            More conversations.
            <br />
            Bigger opportunities.
          </p>
          <ul className="mt-8 space-y-4">
            {highlights.map(({ icon: HighlightIcon, text }) => (
              <li key={text} className="flex items-start gap-3 text-sm leading-6 text-ink-300">
                <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white/10 text-brand-300">
                  <HighlightIcon className="h-4 w-4" aria-hidden="true" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
      </aside>

      <main className="flex min-h-screen flex-col items-center justify-center bg-slate-50 px-4 py-10 lg:bg-white">
        <Logo className="mb-6 lg:hidden" />
        <section
          className={cn(
            "w-full rounded-xl border border-slate-200 bg-white p-6 shadow-card sm:p-8 lg:border-0 lg:shadow-none",
            width === "sm" ? "max-w-sm" : width === "md" ? "max-w-md" : "max-w-xl",
          )}
        >
          <div className="grid h-11 w-11 place-items-center rounded-xl bg-brand-50 text-brand-600">
            <Icon className="h-5 w-5" aria-hidden="true" />
          </div>
          <h1 className="mt-5 text-[26px] font-bold leading-8 tracking-tight text-slate-900">
            {title}
          </h1>
          {subtitle ? (
            <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
          ) : null}
          <div className="mt-6">{children}</div>
        </section>
      </main>
    </div>
  );
}
