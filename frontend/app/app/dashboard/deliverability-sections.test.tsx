import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DeliverabilityOverview } from "@/types/analytics";
import { DeliverabilityHealth } from "./deliverability-sections";

function overview(warnings: DeliverabilityOverview["warnings"]): DeliverabilityOverview {
  return {
    workspace_id: "ws-1",
    overall_health: "ACTION_REQUIRED",
    total_sent: 100,
    bounce_rate: 12,
    complaint_rate: 0,
    failure_rate: 0,
    active_safety_holds: 1,
    warnings,
    mailboxes: [],
    failure_breakdown: [],
  };
}

describe("DeliverabilityHealth safety-hold alert", () => {
  it("links a held mailbox to its page, where the hold can be released", () => {
    render(
      <DeliverabilityHealth
        data={overview([
          {
            code: "MAILBOX_SAFETY_HOLD",
            level: "WARNING",
            title: "Active Safety Hold",
            message: "Mailbox sales@domain.com has 1 active safety hold(s).",
            mailbox_id: "mb-7",
          },
        ])}
      />,
    );

    const link = screen.getByRole("link", { name: /see why and release/i });
    expect(link).toHaveAttribute("href", "/app/mailboxes/mb-7");
  });

  it("offers no link on other alerts", () => {
    render(
      <DeliverabilityHealth
        data={overview([
          {
            code: "HIGH_BOUNCE_RATE",
            level: "CRITICAL",
            title: "Critical Bounce Rate",
            message: "Bounce rate is 12%.",
            mailbox_id: "mb-7",
          },
        ])}
      />,
    );

    expect(screen.queryByRole("link", { name: /see why and release/i })).not.toBeInTheDocument();
  });

  it("does not link a hold alert that names no mailbox", () => {
    render(
      <DeliverabilityHealth
        data={overview([
          {
            code: "MAILBOX_SAFETY_HOLD",
            level: "WARNING",
            title: "Active Safety Hold",
            message: "A hold is active.",
          },
        ])}
      />,
    );

    expect(screen.queryByRole("link", { name: /see why and release/i })).not.toBeInTheDocument();
  });
});
