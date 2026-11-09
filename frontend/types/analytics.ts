export type TimeSeriesBucket = {
  date: string;
  sent: number;
  replies: number;
  bounces: number;
};

export type TopCampaignItem = {
  campaign_id: string;
  name: string;
  status: string;
  sent: number;
  replies: number;
  reply_rate: number;
  bounces: number;
  bounce_rate: number;
  opens?: number;
  open_rate?: number;
};

export type TopMailboxItem = {
  mailbox_id: string;
  email_address: string;
  provider: string;
  sent: number;
  replies: number;
  reply_rate: number;
  bounces: number;
  bounce_rate: number;
  opens?: number;
  open_rate?: number;
};

export type WorkspaceOverviewAnalytics = {
  workspace_id: string;
  start_date: string;
  end_date: string;
  timezone: string;
  prospects_contacted: number;
  emails_sent: number;
  replies: number;
  bounces: number;
  /** Distinct delivered emails opened at least once. */
  opens?: number;
  /** emails_sent - bounces (no provider delivery receipt exists). */
  delivered_estimated?: number;
  unsubscribes: number;
  complaints: number;
  failed_sends: number;
  reply_rate: number;
  bounce_rate: number;
  /** opens / delivered_estimated, in percent. */
  open_rate?: number;
  complaint_rate: number;
  unsubscribe_rate: number;
  open_tracking_supported: boolean;
  click_tracking_supported: boolean;
  delivery_confirmation_supported: boolean;
  trend: TimeSeriesBucket[];
  top_campaigns: TopCampaignItem[];
  top_mailboxes: TopMailboxItem[];
};

export type CampaignAnalytics = {
  campaign_id: string;
  campaign_name: string;
  campaign_status: string;
  total_recipients: number;
  enrolled: number;
  scheduled: number;
  sent: number;
  failed: number;
  cancelled: number;
  skipped: number;
  remaining: number;
  /** Sent emails reported undeliverable (each email counted once). */
  bounced: number;
  hard_bounced?: number;
  soft_bounced?: number;
  /** sent - bounced. No provider reports delivery, so this is an estimate. */
  delivered_estimated?: number;
  /** Distinct delivered emails opened at least once. */
  opened?: number;
  /** Every open event, including repeat opens of the same email. */
  total_opens?: number;
  complained: number;
  unsubscribed: number;
  replied: number;
  unique_recipients_contacted: number;
  unique_recipients_replied: number;
  bounce_rate: number;
  /** opened / delivered_estimated, in percent. */
  open_rate?: number;
  complaint_rate: number;
  unsubscribe_rate: number;
  reply_rate: number;
  failure_rate: number;
  open_tracking_supported: boolean;
  click_tracking_supported: boolean;
  delivery_confirmation_supported: boolean;
};

export type SequenceStepAnalytics = {
  step_id: string;
  position: number;
  kind: string;
  subject: string | null;
  scheduled: number;
  sent: number;
  failed: number;
  cancelled: number;
  bounced: number;
  opened?: number;
  replied: number;
  unsubscribed: number;
  bounce_rate?: number;
  open_rate?: number;
  reply_rate: number;
};

export type CampaignSequenceAnalytics = {
  campaign_id: string;
  steps: SequenceStepAnalytics[];
};

export type DeliverabilityWarning = {
  code: string;
  level: "WARNING" | "CRITICAL";
  title: string;
  message: string;
  metric_value?: number | null;
  threshold?: number | null;
  mailbox_id?: string | null;
  campaign_id?: string | null;
};

export type MailboxDeliverabilityItem = {
  mailbox_id: string;
  email_address: string;
  provider: string;
  connection_state: string;
  health_state: string;
  policy_state: string;
  health_status: "HEALTHY" | "WARNING" | "CRITICAL" | "DISCONNECTED";
  sent_count: number;
  bounce_count: number;
  bounce_rate: number;
  complaint_count: number;
  complaint_rate: number;
  failure_count: number;
  active_safety_holds_count: number;
  warnings: DeliverabilityWarning[];
};

export type SendFailureCategory = {
  category: string;
  count: number;
  description: string;
};

export type DeliverabilityOverview = {
  workspace_id: string;
  overall_health: "HEALTHY" | "NEEDS_ATTENTION" | "ACTION_REQUIRED";
  total_sent: number;
  bounce_rate: number;
  complaint_rate: number;
  failure_rate: number;
  active_safety_holds: number;
  warnings: DeliverabilityWarning[];
  mailboxes: MailboxDeliverabilityItem[];
  failure_breakdown: SendFailureCategory[];
};

export type CampaignActivityType =
  | "SENT"
  | "OPENED"
  | "BOUNCED"
  | "REPLIED"
  | "UNSUBSCRIBED"
  | "COMPLAINED"
  | "FAILED";

export type CampaignActivityStatus = "SUCCESS" | "INFO" | "WARNING" | "ERROR";

export interface CampaignActivityItem {
  id: string;
  type: CampaignActivityType;
  status: CampaignActivityStatus;
  occurred_at: string;
  recipient_email: string | null;
  lead_id: string | null;
  message_id: string | null;
  subject: string | null;
  step_position: number | null;
  detail: string | null;
  open_count: number | null;
}

export interface CampaignActivityPage {
  campaign_id: string;
  items: CampaignActivityItem[];
  next_cursor: string | null;
  has_more: boolean;
  // Present on the first page only; ignores the type filter.
  counts_by_type: Record<CampaignActivityType, number> | null;
  click_tracking_supported: boolean;
  delivery_confirmation_supported: boolean;
}
