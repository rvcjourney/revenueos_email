# MASTER PROJECT CONTEXT — EMAIL OUTREACH SAAS

## 0. How to use this context

This document summarizes the complete project discussion from the previous session.

Treat statements marked as:

- **DECIDED / RECOMMENDED** = architecture or product direction we agreed was the preferred approach.
- **PROPOSED** = recommended UX/feature design that can still be changed.
- **VERIFIED IN PREVIOUS SESSION** = external information that was researched during the prior session. Because provider policies and limits can change, re-check these before production implementation.
- **OPEN** = not finalized yet.

Do not invent functionality beyond this context without clearly identifying it as a new recommendation.

---

# 1. PROJECT OVERVIEW

We are building a **SaaS cold-email/outbound email outreach platform**.

The product is intended for businesses, agencies, sales teams, recruiters, marketers, and other teams that need to manage leads and run personalized outbound email campaigns.

The existing product is only a development/draft product and has no production users that need to be preserved.

Therefore, we are free to redesign the product properly rather than patching the old architecture.

The broad product vision is:

> Build a scalable SaaS outbound email platform where users can connect their own sending accounts, manage leads, build email sequences, personalize messages, schedule campaigns, automatically send follow-ups, receive and classify replies, monitor deliverability, and analyze campaign performance through a simple frontend backed by reliable sending infrastructure.

Competitors/references previously discussed include:

- Instantly
- Saleshandy
- Lemlist

The intention is NOT merely to clone these platforms.

They are references for:

- expected features
- UX patterns
- infrastructure ideas
- deliverability features
- gaps/opportunities
- product positioning

---

# 2. HIGH-LEVEL PRODUCT SYSTEMS

The product has roughly these major functional systems:

1. Workspace and user management
2. Lead/contact management
3. Campaign engine
4. Email sending/mailbox infrastructure
5. Inbox/reply management
6. Tracking/event system
7. Analytics
8. Deliverability monitoring
9. Templates and personalization
10. Integrations
11. Billing
12. Administration
13. Abuse/compliance controls
14. Education/help system

Another way of thinking about the product is that it contains approximately six sub-products:

### 1. Campaign Manager

Build and automate outreach campaigns.

### 2. Lead Manager

Store, import, organize, filter, verify, and suppress prospects.

### 3. Email Infrastructure Manager

Connect and manage Gmail, Microsoft, SMTP mailboxes, domains, sending limits, and account health.

### 4. Sales Inbox

Manage replies and identify interested prospects.

### 5. Analytics & Deliverability

Understand campaign performance, mailbox performance, sending health, bounce issues, authentication, and reputation signals.

### 6. Education Layer

Explain complicated email concepts directly inside the application.

This education layer is important because some users will understand SMTP/DKIM/DMARC while others will only want:

> Upload leads → write email → start campaign → get replies.

---

# 3. PRIMARY USER JOURNEY

The intended primary journey is:

```text
SIGN UP
   ↓
CREATE WORKSPACE
   ↓
CONNECT MAILBOX
   ↓
Google / Microsoft / Custom SMTP
   ↓
VERIFY CONNECTION
   ↓
IMPORT LEADS
   ↓
CREATE CAMPAIGN
   ↓
SELECT LEADS
   ↓
CREATE SEQUENCE
   ↓
STANDARD TEMPLATE / SMART PERSONALIZATION
   ↓
SELECT SENDING MAILBOXES
   ↓
CONFIGURE SCHEDULE
   ↓
CONFIGURE TRACKING + CAMPAIGN RULES
   ↓
REVIEW
   ↓
START CAMPAIGN
   ↓
EMAIL JOBS ARE SCHEDULED
   ↓
WORKERS SEND EMAILS
   ↓
EVENTS ARE TRACKED
   ↓
REPLIES ARRIVE
   ↓
UNIFIED INBOX
   ↓
REPLIES CAN BE CLASSIFIED
   ↓
INTERESTED LEADS
   ↓
ANALYTICS / DELIVERABILITY
```

---

# 4. IMPORTANT TEMPLATE/PERSONALIZATION DECISION

We do NOT want a completely separate "AI Outreach" campaign type.

Inside email/template creation, users should instead choose between approximately:

### Standard Template

Normal reusable template using variables such as:

```text
{{first_name}}
{{last_name}}
{{company_name}}
{{job_title}}
```

### Smart Personalized Template / Smart Personalization

Uses lead/company context and AI to generate personalized portions of the message for each lead.

Preferred naming discussed:

- Standard Template
- Smart Personalized Template

or:

- Standard Template
- Smart Personalization

Avoid names such as:

- Normal
- AI Outreach

because AI should be a capability inside the product rather than an entirely separate campaign system.

> **Amended 2026-09-26 (proposed, [ADR-0011](../adr/0011-hyper-personalized-campaign-type.md)):** the owner chose a campaign-level entry point. A campaign is created as Standard or Hyper-Personalized (`campaign_type`, immutable). This is still not a separate campaign system: the engine, state machines, scheduler and send path are shared, and only the way each message's content snapshot is produced differs. The template-level "Smart Personalized Template" described above remains a possible later addition.

---

# 5. FRONTEND APPLICATION SHELL

## PROPOSED sidebar

Keep the main sidebar relatively simple.

```text
[Logo]

OVERVIEW
Dashboard

OUTREACH
Campaigns
Leads
Inbox
Templates

INFRASTRUCTURE
Mailboxes

WORKSPACE
Integrations
Team
Billing
Settings

SUPPORT
Help
```

Do NOT place every advanced feature directly in the sidebar.

Advanced functions should live as tabs/subpages inside the major sections.

The sidebar should be collapsible.

---

# 6. GLOBAL TOP BAR

The application top bar should contain:

- Workspace switcher
- Global search
- Quick Create
- Notifications
- Help
- User profile

## Workspace switcher

For agencies/multi-workspace customers:

```text
Acme Agency ▼

Acme Agency
Client Alpha
Client Beta
+ Create Workspace
```

## Global search

Search:

- Campaigns
- Leads
- Emails
- Mailboxes
- Templates
- Conversations

## Quick Create

Possible menu:

```text
New Campaign
Import Leads
Add Mailbox
Create Template
Invite Team Member
```

---

# 7. DASHBOARD

The dashboard should answer:

> What is happening with my outreach right now?

Potential KPI cards:

- Active campaigns
- Emails sent today
- Leads contacted
- Replies
- Positive replies
- Bounce rate

Campaign performance table could include:

- Campaign
- Sent
- Reply rate
- Positive reply rate
- Bounce rate

Other sections:

- Recent replies
- Mailbox health
- Sending activity
- Warnings/attention required

Examples:

```text
ATTENTION REQUIRED

2 mailboxes disconnected
Bounce rate increasing on Campaign X
DMARC missing on example.com
Campaign has exhausted available leads
```

Every warning should link to the relevant screen.

---

# 8. NEW USER / ONBOARDING DASHBOARD

Do NOT show a brand-new customer an empty analytics dashboard.

Instead show onboarding:

```text
Welcome to [Product]

Let's launch your first campaign.

✓ Create workspace
○ Connect sending mailbox
○ Import leads
○ Create campaign
○ Write email sequence
○ Review campaign
○ Start sending
```

Show progress.

Each onboarding item should deep-link directly to the required flow.

---

# 9. CAMPAIGNS PAGE

Header:

```text
Campaigns

[Search] [Filters] [+ New Campaign]
```

Campaign table should contain approximately:

- Campaign name
- Status
- Leads
- Sent
- Replies
- Positive replies
- Bounce rate
- Owner

Statuses:

- Draft
- Scheduled
- Running
- Paused
- Completed
- Archived
- Error

Campaign actions:

- Open
- Pause
- Resume
- Duplicate
- Edit
- Archive
- Delete

---

# 10. CREATE CAMPAIGN FLOW

Use a guided campaign wizard instead of one giant form.

Proposed steps:

```text
1 Campaign Setup
2 Leads
3 Sequence
4 Sending Accounts
5 Schedule
6 Settings
7 Review
```

---

# 11. CAMPAIGN STEP 1 — SETUP

Fields:

- Campaign name
- Optional description
- Owner
- Tags

Optional campaign goal:

- Generate replies
- Book meetings
- Recruitment
- Partnerships
- Other

Goal may initially mainly affect analytics/AI rather than sending behavior.

Buttons:

- Cancel
- Save & Continue

---

# 12. CAMPAIGN STEP 2 — LEADS

Lead input methods:

```text
Upload CSV
Select Existing List
Add Manually
Import from Integration
```

## CSV upload

Support:

- drag-and-drop
- browse file

Then column mapping:

```text
first_name       → First Name
surname          → Last Name
email_address    → Email
company          → Company
website          → Website
position         → Job Title
```

Show import analysis:

- total rows
- valid
- missing email
- duplicates
- invalid format

Options:

- remove duplicates
- skip suppressed leads
- skip invalid emails

---

# 13. CAMPAIGN LEAD PREVIEW

After importing, show table:

- Name
- Email
- Company
- Job title
- Status

Filters:

- Valid
- Invalid
- Duplicate
- Suppressed
- Missing data

Actions:

- Add Leads
- Remove
- Verify
- Export
- Add Tag

---

# 14. CAMPAIGN STEP 3 — SEQUENCE

Sequence builder should look conceptually like:

```text
Step 1 — Email
Send immediately

Step 2 — Email
Wait 3 days

Step 3 — Email
Wait 4 days

+ Add Step
```

Click a step to edit it.

---

# 15. EMAIL EDITOR

Fields:

- Subject
- Body

Example:

```text
Subject:
Quick question {{first_name}}

Body:

Hi {{first_name}},

I noticed {{company_name}}...

Regards,
{{sender_first_name}}
```

Toolbar:

- Variables
- AI Assist
- Insert Link
- Insert Image if supported
- Unsubscribe
- Formatting
- Preview
- Test Email

Personalization variable panel:

- first_name
- last_name
- company_name
- job_title
- website
- custom fields
- sender fields

---

# 16. SMART PERSONALIZATION BUILDER

User can provide instructions such as:

> Mention a recent company achievement or something relevant about the business. Keep it under two sentences.

Available context can include:

- First name
- Job title
- Company
- Website
- Company description
- LinkedIn/company information if integrated
- Custom fields
- Enrichment data

Preview personalized output on example leads.

Allow Previous/Next Lead preview.

> The Hyper-Personalized campaign type ([ADR-0011](../adr/0011-hyper-personalized-campaign-type.md)) implements this for a whole campaign: a campaign objective plus a reference template per step, sample previews with manager approval before launch, and just-in-time per-lead generation. Enrichment providers remain an open decision; v1 uses the lead snapshot and website research ([ADR-0013](../adr/0013-research-sources-and-outbound-fetch.md)).

---

# 17. FOLLOW-UP STEPS

When adding a sequence step:

Potential step types:

- Email
- Manual task (future)
- LinkedIn step (future/integration dependent)

Email follow-up settings:

- wait X minutes/hours/days
- send only if no reply
- send in same thread (one switch: the follow-up is a reply to the previous email and takes its subject as "Re: …" — [ADR-0021](../adr/0021-follow-up-same-thread.md))

---

# 18. A/B TESTING

A sequence step can contain:

- Variant A
- Variant B
- optionally additional variants

Traffic split example:

```text
Variant A → 50%
Variant B → 50%
```

Measure:

- Sent
- Delivered
- Replies
- Positive replies
- Clicks

Do NOT rely only on open rate when choosing a winning variant because open tracking is inherently unreliable.

---

# 19. CAMPAIGN STEP 4 — SENDING ACCOUNTS

Show available mailboxes:

- Mailbox
- Provider
- Status
- Daily limit
- Sent today
- Health

Allow checkbox selection.

Show:

- number of selected mailboxes
- daily campaign capacity
- campaign lead count
- approximate duration based on configured capacity

If no mailbox exists:

```text
No Sending Accounts Connected

Connect at least one mailbox before launching this campaign.

[+ Connect Mailbox]
```

Opening mailbox connection should not lose campaign progress.

---

# 20. MAILBOXES PAGE

Header:

```text
Sending Accounts

[Search] [Filters] [+ Add Mailbox]
```

Cards:

- Total mailboxes
- Connected
- Attention needed
- Disconnected

Table fields:

- Mailbox
- Provider
- Status
- Daily limit
- Sent today
- Campaigns
- Health

---

# 21. ADD MAILBOX FLOW

Provider choices:

### Mailbox providers

- Google / Gmail
- Microsoft 365 / Outlook
- Custom SMTP / IMAP
- Other compatible provider

Potential third-party sending providers should eventually be conceptually separated from actual user mailboxes.

---

# 22. CONNECT GOOGLE

RECOMMENDED approach:

```text
[Continue with Google]
```

Use OAuth.

After authorization show:

- connected account email
- provider
- send permission
- inbox/reply access as applicable

Then mailbox-level configuration:

- Sender name
- Daily sending limit
- Sending delay/settings
- Signature

Do NOT make the default Google connection experience:

```text
Email
Normal Gmail password
SMTP host
SMTP port
```

Google OAuth should be the standard integration.

---

# 23. CONNECT MICROSOFT

Preferred architecture:

```text
[Connect Microsoft]
   ↓
Microsoft OAuth
   ↓
Microsoft Graph
```

Again, avoid normal password-based Basic authentication.

---

# 24. CONNECT CUSTOM SMTP / IMAP

Form:

```text
Email Address
Sender Name

SMTP Host
SMTP Port
Username
Credential
Encryption
```

Encryption options:

- STARTTLS
- SSL/TLS
- potentially None only where explicitly permitted

Buttons:

```text
[Test SMTP Connection]
```

Testing should independently verify:

1. DNS resolution
2. TCP connection
3. TLS handshake
4. Authentication
5. Optional test send

Do not simply say:

> SMTP connection failed.

Instead return actionable results.

Example:

```text
DNS                     ✓
TCP smtp.example.com    ✓
TLS handshake           ✓
Authentication          ✕

SMTP response:
535 5.7.8 Authentication credentials invalid
```

Then explain what the customer needs to fix.

---

# 25. IMAP

Custom mailbox reply synchronization may use IMAP.

Form:

- IMAP host
- Port
- Username
- Credential
- Encryption

Test connection.

Educational note:

> SMTP sends your emails. IMAP allows the platform to detect incoming replies.

Where modern provider APIs are available, prefer them over generic IMAP.

---

# 26. MAILBOX DETAILS PAGE

Tabs:

- Overview
- Campaigns
- Activity
- Replies
- Settings
- Deliverability

Overview:

- Email
- Provider
- Connection status
- Daily limit
- Sent today
- Remaining allowance
- 7-day sent
- Delivery rate
- Bounce rate
- Reply rate
- Spam complaint metrics

Domain authentication:

```text
SPF      ✓
DKIM     ✓
DMARC    ⚠
```

---

# 27. CAMPAIGN STEP 5 — SCHEDULE

Fields:

### Days

- Monday
- Tuesday
- Wednesday
- Thursday
- Friday
- Saturday
- Sunday

### Sending hours

Example:

```text
09:00 AM → 05:00 PM
```

### Timezone

Potential options:

- Campaign timezone
- Lead timezone

### Timing distribution

Allow messages to be naturally distributed through the sending window rather than all firing simultaneously.

---

# 28. CAMPAIGN STEP 6 — SETTINGS

Sections:

### Sending

- maximum emails/day
- delay between messages
- mailbox/campaign limits

### Tracking

- Track opens
- Track clicks
- Track replies

Open tracking tooltip should explain that it uses a small image and can be affected by privacy protection.

### Reply Handling

- Stop sequence when lead replies
- Potentially stop other campaigns for the lead depending on configured rule

### Lead Safety

- Skip suppressed leads
- Skip duplicates
- Skip invalid emails

### Unsubscribe

Support unsubscribe handling.

---

# 29. CAMPAIGN REVIEW

Before launch show:

- Campaign name
- Lead count
- Sequence steps
- Mailboxes
- Schedule
- Daily capacity

Pre-launch checks:

```text
✓ Campaign has leads
✓ Email sequence exists
✓ Sending accounts connected
✓ Schedule configured
✓ Suppression checks enabled
✓ Required variables present

⚠ Leads missing required personalization field
⚠ Mailbox/domain configuration warning
```

Do not make users discover these issues only after pressing Start.

---

# 30. START CAMPAIGN

Buttons:

- Save as Draft
- Start Campaign

Confirmation should summarize:

- campaign
- lead count
- sending accounts
- daily capacity
- sending schedule

Then launch.

Starting a campaign should NOT make the API synchronously send thousands of messages.

Instead:

```text
User clicks Start
   ↓
FastAPI validates campaign
   ↓
campaign.status = ACTIVE
   ↓
scheduler/workers handle actual sending asynchronously
```

---

# 31. CAMPAIGN DETAILS

Tabs:

- Overview
- Leads
- Sequence
- Replies
- Analytics
- Activity
- Settings

Header actions:

- Pause
- Resume
- Edit
- More

---

# 32. CAMPAIGN OVERVIEW

Metrics:

- Leads
- Contacted
- Emails sent
- Delivered
- Replies
- Positive replies
- Bounce rate

Potential funnel:

```text
Leads
 ↓
Contacted
 ↓
Emails Sent
 ↓
Delivered
 ↓
Replies
 ↓
Positive Replies
```

Charts:

- Sending over time
- Replies over time
- Sequence performance
- Mailbox performance

---

# 33. CAMPAIGN LEADS TAB

Fields:

- Lead
- Company
- Current sequence step
- Status
- Last activity

Potential statuses:

- Not Started
- Scheduled
- Sent
- Delivered
- Opened
- Clicked
- Replied
- Positive
- Negative
- Bounced
- Unsubscribed
- Suppressed
- Completed

---

# 34. LEAD DETAIL DRAWER

Show:

- Lead name
- Title
- Company
- Email
- Campaign
- Status

Timeline:

```text
Email sent
Delivered
Open signal detected
Link clicked
Reply received
```

Lead information:

- First name
- Last name
- Company
- Job title
- Website
- LinkedIn if available
- Custom fields
- Tags

Actions:

- Send email
- Pause lead
- Mark interested
- Unsubscribe
- Add note
- Remove from campaign

---

# 35. UNIFIED INBOX

Major product feature.

Three-column style:

```text
Filters | Conversations | Email Thread
```

Filters:

- All replies
- Unread
- Interested
- Not Interested
- Neutral
- Out of Office
- Unsubscribe
- Automatic

Conversation actions:

- Reply
- Forward
- Archive
- Mark Interested
- Mark Not Interested
- Unsubscribe
- Add Note

AI can classify replies such as:

- Interested
- Not Interested
- Out of Office
- Wrong person
- Unsubscribe
- Automatic
- Neutral

Potential AI reply suggestions are allowed as a product feature, but sending should require user approval unless the user has intentionally configured an automation.

---

# 36. GLOBAL LEADS PAGE

Tabs/subsections:

- Leads
- Lists
- Suppression
- Import History

Header actions:

- Search
- Filters
- Import Leads
- Add Lead

Lead actions:

- Add to Campaign
- Add to List
- Verify
- Tag
- Export
- Delete
- Suppress

Saved filters:

- All Leads
- Recently Added
- Interested
- Replied
- Unsubscribed
- Bounced
- Suppressed

---

# 37. LEAD LISTS

Examples:

```text
SaaS CEOs
US Agencies
Recruiters
E-commerce
```

Click a list to see/filter contained leads.

---

# 38. IMPORT HISTORY

Store historical import information for debugging.

Example:

```text
founders-sept.csv
2,500 imported
18 invalid
34 duplicate
```

Click to see details.

---

# 39. SUPPRESSION LIST

Very important.

Contains recipients that should not receive future campaign email.

Fields:

- Email
- Reason
- Added date
- Source

Reasons:

- Unsubscribed
- Hard Bounce
- Spam Complaint
- Manual
- Global Block

Actions:

- Add suppression
- Import suppressions
- Export

The suppression check should happen close to actual send time, not only during lead import.

---

# 40. TEMPLATES

Tabs:

- My Templates
- Shared Templates
- Personalized Templates
- Saved Snippets

Templates can display:

- usage count
- campaign usage
- reply performance
- other useful metrics

Create Template should use the same Standard vs Smart Personalization choice used inside Campaign Builder.

---

# 41. ANALYTICS

Workspace-level analytics is a section of the Dashboard (`/app/dashboard`), not a separate page. See `PAGE_MAP.md` section 21.

Global filters:

- Date range
- Campaign
- Mailbox
- Domain
- Team member
- Tag

Metrics:

- Prospects contacted
- Emails sent
- Delivered
- Bounced
- Replies
- Positive replies
- Unsubscribes
- Complaints

Charts:

- Sending trend
- Reply trend
- Positive reply trend
- Bounce trend

Tables:

- Top campaigns
- Top mailboxes
- Top templates
- Top team members

---

# 42. DELIVERABILITY CENTER

Deliverability is a section of the Dashboard (`/app/dashboard`), not a separate page. See `PAGE_MAP.md` section 21.

Possible overview:

```text
Workspace Health
82 / 100
Good
```

Sections:

- Mailbox health
- Domain authentication
- Sending reputation indicators
- Bounce monitoring
- Complaint monitoring
- Sending limits

---

# 43. DOMAIN HEALTH

Table:

- Domain
- SPF
- DKIM
- DMARC
- Tracking domain
- Health

Domain detail page:

```text
SPF
✓ Configured

DKIM
✓ Configured

DMARC
⚠ Missing / requires attention

Custom Tracking Domain
✓ configured
```

For every issue show:

- What is this?
- Why does it matter?
- How to fix it?
- DNS value / copy record where appropriate

---

# 44. ACTIVITY / LOGS

Users need visibility into send behavior and errors.

Filters:

- Campaign
- Mailbox
- Event
- Date
- Status

Events:

- Queued
- Sent
- Delivered
- Open
- Click
- Reply
- Bounce
- Unsubscribe
- Complaint

Errors:

- SMTP authentication failed
- Mailbox disconnected
- Provider rate limit
- Invalid recipient
- Campaign limit exceeded
- Other provider/network errors

---

# 45. INTEGRATIONS

Potential integrations:

- Google Workspace
- Microsoft 365
- Slack
- Zapier
- Webhooks
- CRM integrations
- API

Potential CRMs:

- HubSpot
- Salesforce
- Pipedrive
- Close

These are proposed/future integrations rather than all required in MVP.

---

# 46. DEVELOPER WEBHOOKS

Customer-facing webhooks can support events such as:

- Email sent
- Delivered
- Bounce
- Reply
- Positive Reply
- Unsubscribe

Configuration:

- Endpoint URL
- Event subscriptions
- Webhook secret
- Recent deliveries
- Status codes
- Retry information

---

# 47. PUBLIC API

Developer section:

- API keys
- Create API key
- Key usage
- Last used
- Permissions/scopes

Potential scopes:

- Campaigns
- Leads
- Mailboxes
- Analytics

---

# 48. TEAM

Roles discussed:

- Owner
- Admin
- Manager
- Member
- Viewer

Potential future custom roles.

Team page fields:

- Member
- Role
- Campaigns
- Status

Action:

- Invite member

---

# 49. BILLING

Page:

- Current plan
- Usage
- Seats
- Mailboxes
- Active leads
- AI credits if applicable
- Upgrade
- Manage subscription
- Payment methods
- Invoices

Exact plans/pricing are OPEN.

---

# 50. SETTINGS

Potential settings sections:

- General
- Workspace
- Campaign Defaults
- Sending Defaults
- Tracking
- Domains
- Notifications
- Security
- Developer
- Data & Privacy

Campaign defaults can include:

- sending days
- default hours
- open tracking default
- click tracking default
- reply tracking
- stop on reply
- unsubscribe defaults

---

# 51. NOTIFICATIONS

Potential triggers:

- Positive reply
- Mailbox disconnected
- Campaign completed
- Bounce rate threshold exceeded
- Domain issue
- Sending account limit reached

Channels:

- In-app
- Email
- Slack if integrated

Notification center should deep-link to the relevant item.

---

# 52. HELP / EDUCATION SYSTEM

This is an important product requirement.

Help Center sections:

- Getting Started
- Connecting Mailboxes
- Creating Campaigns
- Managing Leads
- Deliverability
- Tracking & Analytics
- Replies
- DNS Setup
- API & Integrations
- Billing

Also include education directly in product UI.

Examples:

### Bounce Rate tooltip

> Percentage of attempted emails that could not be delivered.

### SPF tooltip

> SPF tells receiving mail servers which servers are authorized to send on behalf of a domain.

### Open tracking tooltip

> Open tracking uses a small remote image to estimate opens. Privacy systems may affect accuracy.

Potential "Outreach Academy":

- Connect first mailbox
- Understand sending limits
- Learn SPF/DKIM/DMARC
- Create first sequence
- Understand bounce rate
- Improve deliverability

---

# 53. EMPTY STATES

Do not show:

> No campaigns.

Instead:

```text
You haven't created a campaign yet.

Campaigns let you automatically send personalized
email sequences to your leads.

[Create Your First Campaign]

Learn how campaigns work →
```

Apply the same principle to:

- Leads
- Mailboxes
- Replies
- Templates
- Integrations

---

# 54. ERROR STATES

Errors must be understandable and actionable.

Bad:

```text
Error 403
```

Better:

```text
Mailbox connection failed.

We couldn't authenticate with your SMTP server.

Possible causes:
- Incorrect credentials
- SMTP disabled
- Wrong port/encryption

[Test Again]
[View Setup Guide]
```

---

# 55. INTERNAL ADMIN PANEL

Separate internal admin product.

Potential sections:

- Overview
- Users
- Workspaces
- Subscriptions
- Campaigns
- Sending Accounts
- Infrastructure
- Email Events
- Domains
- Abuse & Compliance
- Support
- Feature Flags
- System Logs
- Settings

Admin dashboard metrics:

- Customers
- Active customers
- MRR
- Emails sent today
- Active campaigns
- Connected mailboxes
- Bounce rate
- Complaint rate
- Queue size
- Failed jobs
- Worker health

Admin workspace/customer page:

- Account
- Workspace
- Plan
- Usage
- Campaigns
- Mailboxes
- Sending events
- Billing
- Login history
- Warnings

Potential admin actions:

- Suspend workspace
- Disable sending
- Change plan
- Credits
- Reset limits
- View logs

Sensitive admin actions should be audited.

---

# 56. ABUSE / COMPLIANCE PANEL

Important for outbound email SaaS.

Monitor:

- High bounce accounts
- High complaint accounts
- Sudden volume increases
- Repeated invalid contacts
- Blocked domains
- Suspicious signup/activity patterns

Possible risk scoring.

Example:

```text
Workspace A
Risk: HIGH

Complaint rate increasing
Bounce rate increasing
Volume increased sharply
```

---

# 57. CORE EMAIL TERMINOLOGY

## Lead / Prospect

Person being contacted.

Example fields:

- first name
- last name
- email
- company
- title
- website

## Campaign

Collection of:

- leads
- email sequence
- sending accounts
- schedule
- tracking
- sending rules

## Sequence

Series of emails/follow-ups sent to a lead.

Example:

```text
Email 1
↓ wait 3 days
Email 2
↓ wait 4 days
Email 3
```

Normally stop future sequence steps when a reply is detected.

## Sending Account / Mailbox

Actual mailbox used to send.

Examples:

- john@company.com
- sales@company.com

Multiple mailboxes can participate in a campaign.

This is "mailbox rotation", which is different from rotating VPS IPs.

---

# 58. EMAIL LIFECYCLE

Conceptual lifecycle:

```text
Campaign
   ↓
Scheduler
   ↓
Queue
   ↓
Worker
   ↓
Gmail / Microsoft / SMTP provider
   ↓
Recipient mail server
   ↓
Inbox / Promotions / Spam
   ↓
Recipient interaction
```

Events can look like:

```text
queued
sending
accepted/sent
delivered
opened
clicked
replied
bounced
unsubscribed
complained
```

---

# 59. SENT VS DELIVERED

## Sent

Usually means our platform successfully passed the email to the sender/provider.

It does NOT necessarily mean the recipient received it.

## Delivered

Normally means the recipient mail server accepted the message.

Important:

> Delivered does NOT mean Inbox.

A delivered email can potentially land in:

- Inbox
- Promotions
- Updates
- Spam/Junk

Standard delivery webhooks generally do not provide exact final inbox placement for every recipient.

---

# 60. DELIVERY RATE

Example formula:

```text
Delivered Emails
─────────────── × 100
Attempted Emails
```

Metric definitions must be consistent throughout the product.

---

# 61. BOUNCES

## Hard Bounce

Permanent failure.

Examples:

- Address does not exist
- Domain does not exist
- Mailbox permanently unavailable
- Permanent recipient rejection

Hard-bounced addresses should generally be placed into suppression.

## Soft Bounce

Temporary failure.

Examples:

- Temporary server unavailable
- Mailbox temporarily full
- Temporary rate limit
- Temporary policy issue
- Network/DNS issue

May be retried according to provider/error type.

## Bounce Rate

Example:

```text
Bounced
─────── × 100
Attempted
```

Show:

- Total bounce rate
- Hard bounce rate
- Soft bounce rate

---

# 62. OPEN TRACKING

There is generally no universal protocol event saying:

> A human read this email.

Traditional open tracking uses a tiny remote tracking image/pixel.

Conceptually:

```html
<img
 src="https://track.platform.com/open/EVENT_ID.gif"
 width="1"
 height="1"
/>
```

When remote content is loaded:

```text
Email client loads pixel
   ↓
Tracking server receives request
   ↓
Match event ID
   ↓
Record OPEN signal
```

Important:

> Open tracking is NOT perfectly reliable.

Potential false positive:

- Image proxy
- Privacy system
- Scanner
- Pre-loader

Potential false negative:

- Human opens email with remote images disabled

Apple Mail Privacy Protection and similar privacy mechanisms make opens less reliable as a true human-engagement metric.

Treat open tracking as an estimated signal.

---

# 63. UNIQUE OPENS VS TOTAL OPENS

If one recipient opens five times:

- Total opens = 5
- Unique opener = 1

Dashboard should distinguish these.

---

# 64. CLICK TRACKING

Original link:

```text
https://company.com/demo
```

Tracking enabled:

```text
https://click.platform.com/c/ABC123
```

Flow:

```text
Recipient clicks
   ↓
Tracking server
   ↓
Record click
   ↓
HTTP redirect
   ↓
Destination URL
```

This allows link-level tracking.

---

# 65. CLICK RATE / CTOR

CTR can commonly mean:

```text
Unique clickers
────────────── × 100
Delivered
```

CTOR:

```text
Unique Clickers
──────────────
Unique Opens
```

But CTOR inherits the unreliability of open tracking.

---

# 66. REPLY TRACKING

Reply tracking is more valuable for an outreach product than open tracking.

Possible synchronization methods:

- Gmail API
- Microsoft Graph
- IMAP
- provider push/webhook mechanisms
- inbound routing where appropriate

Important email headers:

- Message-ID
- In-Reply-To
- References
- From
- To
- Subject

Original:

```text
Message-ID: <abc123@company.com>
```

Reply may contain:

```text
In-Reply-To: <abc123@company.com>
```

This allows reliable thread/campaign/lead matching.

When reply is received:

```text
Mark lead = REPLIED
Stop sequence
```

according to campaign rules.

---

# 67. REPLY RATE

For outreach, prefer lead-level reply rate:

```text
Unique Leads Who Replied
──────────────────────── × 100
Unique Leads Contacted
```

because one lead may receive several sequence emails.

Dashboard should distinguish:

- Emails Sent
- Prospects Contacted
- Unique Replies

---

# 68. POSITIVE REPLIES

Classify replies:

- Interested
- Not Interested
- Neutral
- Out of Office
- Wrong Person
- Unsubscribe
- Automatic

AI can help classify them.

Positive Reply Rate can be based on:

```text
Interested Leads
─────────────── × 100
Leads Contacted
```

---

# 69. CONVERSION

Conversion represents actual business outcome.

Examples:

- Meeting booked
- Demo requested
- Qualified opportunity
- Account created
- Sale

Ideal funnel:

```text
Sent
↓
Delivered
↓
Reply
↓
Positive Reply
↓
Meeting
↓
Customer
```

---

# 70. UNSUBSCRIBE

When recipient unsubscribes:

```text
Identify lead
   ↓
mark UNSUBSCRIBED
   ↓
add to suppression
   ↓
cancel future queued sends
```

---

# 71. SPAM COMPLAINT

When provider reports a complaint:

```text
Complaint
   ↓
mark lead
   ↓
suppress lead
   ↓
stop future campaigns
```

Spam complaint rates must remain extremely low.

A complaint should be considered more serious than an open/click metric.

---

# 72. WEBHOOK

Webhook means another service automatically sends our backend an HTTP request when an event occurs.

Example:

```text
POST /webhooks/email

event = delivered
message_id = abc123
recipient = john@example.com
```

Our backend updates state.

Webhooks can provide events such as:

- accepted/sent
- delivered
- temporary failure
- permanent failure
- opens
- clicks
- unsubscribe
- complaints

depending on provider.

---

# 73. SMTP / IMAP / API

## SMTP

Protocol primarily used for sending email.

## IMAP

Protocol used for mailbox synchronization/reading.

Simplified:

```text
SMTP → send
IMAP → synchronize/read
```

## Email API

Instead of SMTP, provider accepts HTTPS API calls.

Examples:

- Gmail API
- Microsoft Graph
- third-party email APIs

---

# 74. EMAIL AUTHENTICATION TERMS

## SPF

DNS record identifying systems authorized to send on behalf of a domain.

## DKIM

Cryptographic signature added to email and validated through public key in DNS.

## DMARC

Builds on SPF/DKIM alignment and lets domain owners define/report authentication policy.

Common policy values:

- none
- quarantine
- reject

## MX

DNS record describing where email for a domain should be received.

## DNS

Contains email-related records such as:

- MX
- SPF
- DKIM
- DMARC
- CNAME
- A
- PTR/rDNS relationship

---

# 75. TRACKING DOMAIN

Instead of shared provider tracking URL:

```text
tracking.provider.com
```

allow custom domain such as:

```text
click.mail.customer.com
```

Potentially used for:

- open pixels
- click redirects
- unsubscribe links

Typically configured through DNS/CNAME.

---

# 76. REPUTATION

Deliverability is influenced by multiple reputational layers:

```text
IP Reputation
+
Domain Reputation
+
Mailbox/Sender Reputation
+
Content/Recipient Signals
```

Changing VPS IP does NOT automatically fix email reputation or provider rate limits.

---

# 77. DEDICATED IP / SHARED IP / IP POOL

## Dedicated IP

Sending IP dedicated to one sender/customer/pool.

## Shared IP

Used by multiple senders.

## IP Pool

Controlled group of sending IPs.

Important architecture point:

> Randomly changing IPs after each email is not the desired architecture.

Controlled provider routing/pools are fundamentally different from trying to evade rate controls with random IP rotation.

---

# 78. RATE LIMIT / THROTTLING

## Rate Limit

Controls the maximum amount of sending/request activity.

Can exist at:

- provider level
- user/mailbox level
- project/API level
- workspace level
- campaign level
- infrastructure level

## Throttling

Deliberately slowing traffic.

Instead of:

```text
100 emails at 10:00:00
```

spread messages across configured time.

---

# 79. WARM-UP

General concept:

Gradually establish normal sending history rather than taking a brand-new mailbox and suddenly sending extremely high volume.

Warm-up is not a guarantee of inbox placement.

Do not treat it as a magic solution.

---

# 80. INBOX PLACEMENT VS DELIVERY

If recipient server accepted 1,000 emails:

```text
Delivered = 1,000
```

they may end up across:

- Inbox
- Promotions
- Spam

Therefore:

> Delivery Rate ≠ Inbox Placement Rate.

---

# 81. EMAIL VERIFICATION

Potential statuses:

- Valid
- Invalid
- Risky
- Unknown
- Catch-all
- Disposable

Verification is not proof of:

- recipient intent
- inbox placement
- actual person validity
- engagement

Catch-all/accept-all domains can be less certain.

---

# 82. EVENT TRACKING DATA MODEL

RECOMMENDED event-style architecture.

Potential `email_events` table:

```text
id
message_id
campaign_id
lead_id
mailbox_id
event_type
timestamp
metadata
```

Event types:

```text
QUEUED
SENDING
SENT
DELIVERED
OPENED
CLICKED
REPLIED
BOUNCED
UNSUBSCRIBED
COMPLAINED
```

Analytics can largely be built by aggregating these events.

---

# 83. OVERALL TRACKING SOURCES

The product has roughly three tracking sources.

## 1. Sending Provider Events

Examples:

- Sent
- Delivered
- Bounced
- Complained

Usually from API/webhooks/provider responses.

## 2. Our Tracking Infrastructure

Examples:

- Opens
- Clicks
- Unsubscribe

Using:

- tracking pixel
- redirect links
- unsubscribe endpoints

## 3. Connected Mailbox Data

Examples:

- Replies
- Threads
- Incoming messages

Using:

- Gmail API
- Microsoft Graph
- IMAP where necessary

---

# 84. RECOMMENDED TECH STACK

## Frontend

- Next.js
- React
- TypeScript
- Tailwind CSS
- shadcn/ui

## Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic

## Data platform

Preferred current direction:

- Supabase PostgreSQL
- Supabase Auth
- Supabase Storage
- Supabase Realtime

## Async/background processing

- Redis
- Celery
- Celery Beat or Python scheduler logic

## Email

- Gmail API
- Microsoft Graph
- Custom SMTP
- IMAP where required

## Deployment

- Docker
- Docker Compose initially
- AWS preferred for production
- EC2 initially
- potentially ECS later

## Observability

- Sentry
- CloudWatch if on AWS
- structured application logs

## CI/CD

- GitHub
- GitHub Actions

---

# 85. WHY PYTHON

Python is a strong fit for this project.

Complex backend tasks include:

- campaign scheduling
- SMTP/IMAP
- Gmail API integration
- Microsoft Graph
- webhook processing
- CSV processing
- lead verification
- AI personalization
- reply classification
- analytics
- enrichment
- background jobs

The recommended architecture allows most complicated product logic to remain Python.

Next.js is mainly the frontend.

---

# 86. FASTAPI RESPONSIBILITIES

FastAPI is the main business/API layer.

Potential endpoints:

```text
POST /campaigns
GET /campaigns
POST /campaigns/{id}/start

POST /leads/import

POST /mailboxes
POST /mailboxes/test-smtp

POST /webhooks/provider

GET /analytics/...

POST /templates/personalize
```

FastAPI handles:

- Authentication validation
- Workspace permissions
- Campaign business logic
- Lead management
- Mailbox management
- OAuth/provider integrations
- Webhooks
- Analytics endpoints
- AI-related operations

FastAPI should NOT synchronously send an entire campaign.

---

# 87. CELERY

Celery handles asynchronous/background tasks.

Potential queues:

```text
campaign_scheduler
email_send
email_sync
webhook_processing
lead_import
email_verification
personalization
analytics
cleanup
```

Potential separate workers:

- send worker
- reply/sync worker
- webhook worker
- analytics worker
- AI worker

This allows independent scaling.

Do NOT initially make everything one queue.

---

# 88. REDIS

Use Redis for:

- Celery broker/job queues
- rate-limit counters
- distributed locks
- temporary state
- cache
- worker coordination
- potentially real-time counters

PostgreSQL/Supabase remains the permanent source of truth.

---

# 89. MODULAR MONOLITH

Do NOT initially build:

- 20 microservices
- Kubernetes
- Kafka
- service mesh
- multiple databases

Build a modular Python monolith first.

Example modules:

```text
auth/
users/
workspaces/
campaigns/
leads/
mailboxes/
sending/
inbox/
tracking/
deliverability/
templates/
analytics/
billing/
integrations/
admin/
```

Workers can share/import the same business modules.

---

# 90. DOCKER

Containerize from the beginning.

Possible development Compose services:

```text
frontend
backend
redis
celery-worker
celery-beat
nginx
```

If Supabase Cloud is used, production PostgreSQL/Auth/Storage/Realtime are external managed services.

Local development can use a local DB/Supabase setup as desired.

---

# 91. SUPABASE DECISION

Supabase can fit this project well.

RECOMMENDED use:

```text
Supabase
├─ PostgreSQL
├─ Auth
├─ Storage
└─ Realtime
```

Keep:

```text
FastAPI = business brain
Celery = campaign/background engine
Redis = queue/rate-control layer
```

Do NOT try to replace the entire Python backend with Supabase Edge Functions.

---

# 92. SUPABASE + SQLALCHEMY

Preferred backend database access:

```text
FastAPI
   ↓
SQLAlchemy
   ↓
Supabase PostgreSQL
```

rather than tightly coupling all backend business logic directly to Supabase client APIs.

Reason:

Supabase PostgreSQL is still PostgreSQL.

This keeps the architecture portable.

If ever necessary:

```text
Supabase PostgreSQL
   ↓
another PostgreSQL provider / AWS RDS
```

is easier than rewriting the whole backend.

---

# 93. SUPABASE AUTH

Use Supabase Auth for much of:

- registration
- login
- password reset
- email verification
- OAuth
- JWT/session handling
- potentially magic links/MFA as needed

Flow:

```text
Next.js
   ↓
Supabase Auth
   ↓
JWT
   ↓
FastAPI verifies identity
   ↓
Workspace / permissions
```

Our application still maintains:

- profiles
- workspaces
- workspace_members
- roles

---

# 94. SUPABASE STORAGE

Potential storage:

- CSV imports
- exports
- profile pictures
- logos
- campaign attachments if supported
- generated reports
- import error files

Do not store important uploads only in the local Docker filesystem.

---

# 95. SUPABASE REALTIME

Useful for:

- live campaign statistics
- new replies
- import progress
- mailbox status
- notifications
- admin monitoring

Example:

```text
Worker updates DB
   ↓
Supabase Realtime
   ↓
Next.js dashboard updates
```

---

# 96. SUPABASE SECURITY

Use application-level workspace permissions.

Also use Row Level Security where appropriate for defense-in-depth.

One workspace must never access another workspace's data.

Do NOT let the browser directly perform critical business actions merely because Supabase permits direct table access.

For example:

BAD:

```text
Browser directly updates campaign.status = running
```

Preferred:

```text
Browser
   ↓
POST /campaigns/{id}/start
   ↓
FastAPI
   ↓
permission checks
campaign validation
mailbox validation
lead validation
schedule validation
   ↓
transaction
```

Direct Supabase use is more appropriate for:

- authentication
- realtime subscriptions
- selected file uploads
- certain profile operations

Critical actions go through FastAPI.

---

# 97. SUPABASE QUEUES / CRON

Supabase also has Queue/Cron capabilities, but previous recommendation was:

> Keep Redis + Celery as the main worker architecture initially.

Reason:

We need:

- retries
- worker concurrency
- routing
- multiple queues
- scheduling
- rate controls
- orchestration
- monitoring
- independently scalable worker groups

Supabase Cron may be useful for simple housekeeping jobs, but core campaign scheduling should remain Python business logic.

---

# 98. DEPLOYMENT DECISION

The project originally considered:

- Hostinger VPS
- AWS

Requirement stated:

> Choose only one production compute platform rather than intentionally running the product across both.

Current recommendation based on the complete discussion:

> **AWS is preferred for production.**

Reasons include:

- scalable worker architecture
- easier future container scaling
- many mailbox/API integrations
- queues/background workers
- more predictable infrastructure options
- fewer Hostinger-specific SMTP constraints
- future EC2 → ECS path
- managed AWS components available when needed

This is a recommendation, not an immutable decision.

---

# 99. INITIAL AWS ARCHITECTURE

Keep AWS simple initially.

Potential architecture:

```text
AWS

EC2
├── Nginx
├── Next.js
├── FastAPI
├── Redis
├── Celery workers
└── Celery Beat / scheduler

Supabase Cloud
├── PostgreSQL
├── Auth
├── Storage
└── Realtime
```

If using Supabase Cloud, it is a managed external backend service while AWS remains the application compute platform.

Alternative if absolutely everything must be on AWS:

- RDS instead of Supabase PostgreSQL
- S3
- Cognito or custom authentication
- ElastiCache
- etc.

However, the preferred development-speed setup discussed is AWS compute + Supabase managed services.

---

# 100. FUTURE AWS SCALE

Later:

```text
Load Balancer
     ↓
ECS
├── Next.js
├── FastAPI
└── worker services

Supabase or RDS
Redis / ElastiCache
Storage
```

Worker pools can independently grow:

```text
2 workers
↓
5
↓
10
↓
30
```

without changing product architecture.

---

# 101. IP SWITCHING — IMPORTANT CLARIFICATION

A major previous question was:

> The VPS has one IP. How do we switch IPs for bulk sending?

Critical answer:

> For user-connected Gmail/Microsoft/SMTP mailboxes, the application server IP is normally NOT the actual final mail-delivery IP.

Example:

```text
AWS backend
   ↓
Gmail API
   ↓
Google mail infrastructure
   ↓
Recipient
```

The AWS IP performs an HTTPS API request.

Google's infrastructure performs actual email delivery.

Similarly:

```text
Backend
   ↓
customer SMTP provider
   ↓
provider mail infrastructure
   ↓
recipient
```

Therefore we do NOT need 50 AWS public IPs simply because 50 customer mailboxes are connected.

Multiple sending IPs become a major concern only if we operate the actual outbound MTA/direct-to-MX infrastructure ourselves.

That is NOT recommended for the first version.

---

# 102. DO NOT OPERATE DIRECT-TO-MX INFRASTRUCTURE INITIALLY

Avoid initial architecture:

```text
Our VPS
   ↓ port 25
Recipient Gmail/Yahoo/Microsoft MX
```

If we do this, we become responsible for:

- MTA
- SMTP queues
- retries
- TLS
- rDNS/PTR
- HELO
- SPF
- DKIM
- DMARC
- bounces
- complaints
- IP reputation
- domain reputation
- blocklists
- warm-up
- abuse operations
- feedback loops

Not worth building initially.

Preferred:

- Gmail API
- Microsoft Graph
- Custom authenticated SMTP
- approved provider APIs where relevant

---

# 103. PREVIOUS SMTP FAILURE ON HOSTINGER VPS

Previous implementation:

```text
Worker on Hostinger VPS
   ↓
Fetch user's SMTP credentials from DB
   ↓
Connect to SMTP server
   ↓
Send email
```

It was failing when deployed.

Then sending was changed to Resend API and worked.

We do NOT know the exact historical SMTP error, so never claim one exact cause without logs.

Most likely categories discussed:

- VPS SMTP throttling/restriction
- Gmail password/basic authentication no longer accepted
- incorrect TLS/port configuration
- SMTP AUTH disabled
- too many concurrent SMTP connections
- provider rate limit
- firewall/network issue
- direct port 25 issue if it was being used

Python itself is unlikely to be the root cause.

---

# 104. HOSTINGER FINDING

VERIFIED IN PREVIOUS SESSION, RE-CHECK BEFORE PRODUCTION:

Hostinger documentation at the time said:

- port 25 is not completely blocked
- VPS SMTP traffic was restricted to approximately 5 emails/minute as an anti-abuse measure

This is relevant if many customer SMTP connections originate from the same VPS.

It creates a shared infrastructure-level constraint.

---

# 105. WHY RESEND WORKED

Resend changed the connection path from SMTP traffic to normal HTTPS API traffic.

Instead of:

```text
Python
   ↓ SMTP
port 587/465/25
```

we had:

```text
Python
   ↓ HTTPS 443
Resend API
   ↓
Resend mail infrastructure
   ↓
recipient
```

Resend handles the actual mail delivery infrastructure.

This removes many SMTP/network complications from our app server.

---

# 106. IMPORTANT RESEND RESTRICTION

VERIFIED IN PREVIOUS SESSION, RE-CHECK CURRENT TERMS BEFORE PRODUCTION:

Resend's Acceptable Use Policy at the time prohibited unsolicited cold outreach, including cold outreach/purchased/scraped contact lists.

Therefore:

> Do NOT use Resend as the main cold-outreach campaign delivery engine unless current terms explicitly allow the intended use case.

Resend is still appropriate for the SaaS's own transactional/opt-in messages such as:

- Account verification
- Password reset
- Workspace invitation
- Security notifications
- Billing email
- Campaign completed notification
- Product transactional alerts

Architectural separation:

```text
CUSTOMER OUTREACH
   ↓
Customer's connected Gmail / Microsoft / permitted SMTP

PRODUCT TRANSACTIONAL EMAIL
   ↓
Resend or another suitable transactional provider
```

---

# 107. GOOGLE AUTHENTICATION

Important previous finding:

Google Workspace no longer supports old "less secure app" normal username/password access for these types of integrations.

For Google mailboxes, preferred:

```text
OAuth
+
Gmail API
```

rather than storing the user's normal Gmail password.

An OAuth flow:

```text
User
   ↓
Connect Google
   ↓
Google OAuth consent
   ↓
authorization
   ↓
FastAPI receives credentials/tokens
   ↓
encrypted storage
   ↓
Gmail API
```

Credentials/tokens must be securely encrypted and access should be tightly controlled.

---

# 108. GOOGLE SENDING VIA API

Send flow:

```text
Celery Worker
   ↓
load mailbox OAuth token
   ↓
refresh token if necessary
   ↓
Gmail API messages.send
   ↓
Google
   ↓
Recipient
```

This uses normal HTTPS rather than SMTP from the VPS.

---

# 109. GOOGLE HAS MULTIPLE TYPES OF LIMITS

Do not confuse:

### API quota

How many API requests/quota units the application/user can make.

with:

### Sending limit

How many emails/recipients the mailbox/account is allowed to send.

with:

### Anti-abuse/deliverability controls

Dynamic restrictions that can happen earlier based on reputation/behavior.

These are different layers.

---

# 110. GOOGLE QUOTAS DISCUSSED

VERIFIED IN PREVIOUS SESSION, RE-CHECK CURRENT DOCUMENTATION BEFORE IMPLEMENTATION.

At that time Gmail API documentation included:

- Project quota per minute
- Per-user/project quota
- Different quota unit costs for methods
- `messages.send` consuming API quota units

We previously noted an example calculation that the API request quota can theoretically permit many send API calls/minute.

Important:

> API capacity is NOT a safe outreach sending recommendation.

Never set user sending volume simply because the API technically permits it.

---

# 111. GOOGLE WORKSPACE SENDING LIMITS DISCUSSED

VERIFIED IN PREVIOUS SESSION, RE-CHECK BEFORE IMPLEMENTATION.

We discussed Google Workspace documentation values including approximately:

- 2,000 messages/day for normal paid Workspace users
- lower limits for trials
- recipient-based limits
- external/unique recipient limits
- limits being calculated over a rolling 24-hour period

Important:

> Provider hard ceilings are not product recommendations.

If the provider says 2,000/day, that does not mean the product should send 2,000 cold emails/day from a mailbox.

Platform safety limits should generally be much lower and policy-driven.

---

# 112. ROLLING WINDOW

Do NOT implement Google limit logic as:

```text
midnight → reset all counters
```

because relevant Google limits can operate as rolling 24-hour windows.

Example:

At Sep 8 15:00, count relevant activity since approximately Sep 7 15:00.

Architecture should support rolling-window counters/history.

---

# 113. GOOGLE DYNAMIC THROTTLING

Mailbox provider systems can restrict sending earlier than published hard limits.

Potential signals:

- sudden volume increase
- domain/IP reputation
- mailbox history
- spam complaints
- recipient behavior
- unusual sending pattern
- authentication issues

Do not attempt to evade dynamic throttling by rotating VPS IPs.

The correct response is:

- slow down
- retry temporary failures appropriately
- protect reputation
- respect provider policies

---

# 114. MICROSOFT

Preferred:

```text
Microsoft OAuth
   ↓
Microsoft Graph
   ↓
Exchange Online
```

rather than normal password/basic auth.

Microsoft environments can have SMTP AUTH disabled at organization/mailbox level.

Modern Authentication/OAuth should be preferred.

---

# 115. MICROSOFT LIMITS DISCUSSED

VERIFIED IN PREVIOUS SESSION, RE-CHECK CURRENT DOCUMENTATION.

Values discussed included examples such as:

- recipient-rate limits over 24 hours
- message/minute limits
- recipient/message limits
- API-specific throttling
- concurrent request/SMTP connection limits

Again:

> Published service ceilings are not recommended outreach operating rates.

Our own platform limiter should be lower and provider-aware.

---

# 116. CUSTOM SMTP

Custom SMTP is the most variable integration because providers differ.

Potential providers:

- Zoho
- Hostinger Email
- cPanel mail
- private corporate mail servers
- commercial SMTP services

We cannot hardcode one universal provider limit.

Connection configuration should include:

- SMTP host
- Port
- Encryption
- Username
- Credential/OAuth where supported

Connection testing should return exact reason for failure.

---

# 117. SMTP PORTS

General distinction discussed:

### Port 25

Primarily server-to-server SMTP.

Often restricted on cloud hosting because of abuse.

Not preferred for customer mailbox submission.

### Port 587

Authenticated SMTP submission, commonly with STARTTLS.

### Port 465

SMTP submission with implicit TLS.

For connected customer mailboxes use the provider's documented submission configuration, usually 587 or 465.

---

# 118. AWS PORT 25

VERIFIED IN PREVIOUS SESSION, RE-CHECK BEFORE IMPLEMENTATION:

AWS EC2 restricts public outbound SMTP on port 25 by default.

But our architecture primarily needs:

```text
443 → Gmail API
443 → Microsoft Graph
443 → transactional provider API
587 → authenticated custom SMTP
465 → authenticated custom SMTP
```

Therefore AWS port-25 restrictions are not a major issue if we do not operate direct-to-MX delivery.

---

# 119. GOOGLE SMTP RELAY

Google Workspace SMTP Relay was discussed as a possible advanced/enterprise option.

It can require customer Workspace administrator configuration.

Therefore:

### Default Google onboarding

```text
OAuth + Gmail API
```

### Possible advanced enterprise feature

```text
Google SMTP Relay
```

Do not make SMTP Relay the normal onboarding experience.

---

# 120. RATE LIMITING — CORE ARCHITECTURE

We must NOT try to "prevent" provider rate limiting by bypassing it.

We prevent failures by staying inside limits.

Conceptual:

```text
Campaign
   ↓
Scheduler
   ↓
Email Job
   ↓
RATE LIMIT SERVICE
   ↓
Can send now?
  /       \
YES       NO
 |         |
SEND     RESCHEDULE
```

---

# 121. RATE LIMITING LEVELS

Our rate limiter should be able to evaluate multiple layers:

```text
GLOBAL PLATFORM
      ↓
PROVIDER
      ↓
WORKSPACE
      ↓
MAILBOX
      ↓
CAMPAIGN
      ↓
RECIPIENT DOMAIN / OPTIONAL SAFETY RULE
```

The strictest applicable rule wins.

Example:

```text
Provider theoretical max: 2000
Platform safety max:        50
Mailbox configured max:     40
Campaign allocation:        30

Effective max:              30
```

Exact default safety limits remain OPEN and should be determined according to provider policies, use case, mailbox health, and product compliance requirements.

---

# 122. REDIS RATE-LIMIT STATE

Example counters:

```text
mailbox:123:sent_window
mailbox:123:sent_hour
mailbox:123:last_send

google:user:123:minute

microsoft:user:567:minute

smtp:provider:xyz:minute
```

Redis is useful because multiple workers need one shared view of rate-limit state.

---

# 123. MULTIPLE WORKERS

Without central coordination:

```text
Worker 1 → send
Worker 2 → send
Worker 3 → send
Worker 4 → send
Worker 5 → send
```

all through one mailbox simultaneously.

Bad.

Instead:

```text
Workers
   ↓
Central Redis rate limiter / lock
   ↓
Only allowed send proceeds
   ↓
Others wait/reschedule
```

---

# 124. TOKEN BUCKET

A token-bucket style model was recommended conceptually.

Example:

Mailbox has sending tokens.

Each email consumes one token.

Tokens refill at configured intervals.

This controls burstiness and throughput.

Use atomic Redis operations/scripts/locking so two workers cannot consume the same allowance.

---

# 125. PROVIDER ADAPTER ARCHITECTURE

Build a common abstraction.

Conceptually:

```text
EmailProvider

send()
test_connection()
refresh_credentials()
get_rate_status()
classify_error()
```

Implementations:

```text
GmailProvider
MicrosoftProvider
SMTPProvider
```

Separate provider:

```text
TransactionalEmailProvider
→ Resend or equivalent
```

Campaign engine should not care about detailed transport.

It requests:

```text
provider.send(message)
```

---

# 126. ERROR CLASSIFICATION

Raw failures can include:

- HTTP 429
- 4xx SMTP temporary responses
- 5xx SMTP permanent/policy responses
- Authentication failure
- ConnectionRefused
- Timeout
- TLS error

Translate them into platform categories:

```text
RATE_LIMIT
TEMPORARY_PROVIDER_ERROR
AUTH_FAILURE
PERMANENT_BOUNCE
NETWORK_ERROR
POLICY_REJECTION
```

Then define behavior.

### RATE_LIMIT

- do not immediately retry
- respect Retry-After if provided
- exponential backoff + jitter

### AUTH_FAILURE

- pause/disconnect mailbox
- notify user
- request reconnect/credential fix

### PERMANENT_BOUNCE

- mark lead
- suppress as required
- don't repeatedly retry

### NETWORK_ERROR

- limited retry/backoff

### POLICY_REJECTION

- pause or escalate depending on code
- don't blindly retry indefinitely

---

# 127. SMTP RESPONSE CLASSES

Conceptually:

```text
2xx → accepted/success
4xx → temporary failure; retry according to policy
5xx → permanent/policy failure; usually do not blindly retry
HTTP 429 → API throttling
401 → auth/token issue
403 → permission/policy
```

Always interpret provider-specific details.

---

# 128. EXPONENTIAL BACKOFF

Do not:

```text
retry
retry
retry
retry
```

immediately.

Instead increase delay.

Example:

```text
attempt 1 fails
↓
wait ~30 sec
↓
attempt 2 fails
↓
wait ~1 min
↓
attempt 3 fails
↓
wait ~2 min
↓
attempt 4 fails
↓
wait longer
```

Exact backoff policies should depend on provider/error type.

---

# 129. JITTER

Add randomness to retry delays.

If 500 jobs fail at once, they should not all retry exactly 60 seconds later.

Example:

```text
job A → 61 sec
job B → 68 sec
job C → 73 sec
```

Prevents thundering-herd behavior.

---

# 130. CIRCUIT BREAKER

If a mailbox/provider starts repeatedly failing:

```text
several consecutive relevant failures
   ↓
open circuit
   ↓
pause mailbox
```

Do not continue allowing thousands of jobs to fail.

Notify the user.

Automatically re-check according to safe policy or require reconnection depending on error.

---

# 131. RESEND RATE LIMIT EXAMPLE

VERIFIED IN PREVIOUS SESSION, RE-CHECK CURRENT DOCUMENTATION:

Resend exposed API rate limiting with HTTP `429` plus rate-limit headers such as:

- limit
- remaining
- reset
- Retry-After

Our worker should always respect a provider's Retry-After or documented recovery mechanism.

This is the model to use for all API providers.

---

# 132. FINAL RECOMMENDED SENDING ARCHITECTURE

```text
                         NEXT.JS
                            │
                            ↓
                         FASTAPI
                          Python
                            │
                            ↓
                        SUPABASE
                  PostgreSQL / Auth
                            │
                            ↓
                    CAMPAIGN SCHEDULER
                            │
                            ↓
                          REDIS
                            │
                 ┌──────────┴──────────┐
                 │                     │
            SEND QUEUE             SYNC QUEUE
                 │                     │
                 ↓                     ↓
          CELERY WORKERS          SYNC WORKERS
                 │
                 ↓
           RATE LIMITER
                 │
        ┌────────┼────────────┐
        │        │            │
        ↓        ↓            ↓
     Google   Microsoft     Custom
        │        │            │
   Gmail API Graph API       SMTP
    OAuth      OAuth        587/465
        │        │            │
        └────────┼────────────┘
                 │
                 ↓
             Recipient
```

Product-generated transactional mail:

```text
FastAPI
   ↓
Transactional provider such as Resend
   ↓
User
```

provided the provider's terms allow that use.

---

# 133. DATABASE / BACKEND CONCEPTS TO PLAN

Core data areas likely include:

- users
- profiles
- workspaces
- workspace_members
- roles
- campaigns
- campaign_steps
- campaign_variants
- campaign_settings
- leads
- lead_lists
- lead_campaigns
- lead_custom_fields
- mailboxes
- mailbox_credentials/tokens
- mailbox_provider_config
- domains
- messages
- message_events
- conversations
- replies
- suppression_entries
- templates
- notifications
- webhook_endpoints
- webhook_deliveries
- integrations
- billing/subscription references
- audit_logs
- admin/risk information

This is a conceptual model, not a finalized database schema.

---

# 134. SECURITY PRINCIPLES

Must plan for:

- Multi-tenant workspace isolation
- RBAC
- Supabase RLS where appropriate
- Application-level permission checks
- Encryption of OAuth refresh tokens
- Encryption/protection of SMTP credentials
- Secret management
- No credentials in frontend/browser logs
- Audit logs for sensitive actions
- Webhook signature verification
- Rate limiting
- Input validation
- CSV/file validation
- Secure token refresh
- Secure admin operations

Exact security architecture still needs a dedicated design pass.

---

# 135. PRODUCT METRICS TO EMPHASIZE

For outreach, meaningful hierarchy is approximately:

```text
Sales / Meetings
        ↑
Positive Replies
        ↑
Replies
        ↑
Clicks
        ↑
Opens
(less trustworthy)
        ↑
Delivery
        ↑
Sent
```

Do not optimize product UX around open rate alone.

A campaign with:

```text
80% opens
0 useful replies
```

can still be unsuccessful.

---

# 136. MAIN CAMPAIGN DASHBOARD METRICS

Recommended:

- Prospects
- Contacted
- Emails Sent
- Delivered
- Bounced
- Estimated Opened
- Clicked
- Replied
- Positive Replies
- Unsubscribed
- Spam Complaints

Also:

### Sequence Performance

- Email 1
- Email 2
- Email 3

### Mailbox Performance

- mailbox-level statistics

### Variant Performance

- Template A
- Template B

Metric definitions must be standardized.

---

# 137. DELIVERABILITY PRINCIPLE

Deliverability is broader than delivery.

It includes:

- authentication
- domain reputation
- IP reputation
- mailbox/sender behavior
- bounce patterns
- complaint rates
- recipient engagement
- sending volume
- sending patterns
- infrastructure
- content
- provider policies

Do not market:

> change IP = fix deliverability.

That is incorrect.

---

# 138. IMPORTANT DO-NOT-DO LIST

Do NOT:

1. Use normal Gmail passwords as the primary Google integration.
2. Build direct-to-MX SMTP infrastructure in the first version.
3. Randomly rotate VPS IPs to evade rate limits.
4. Treat published provider maximums as recommended cold-email volumes.
5. Synchronously send entire campaigns inside FastAPI requests.
6. Put all async jobs into one unstructured worker queue.
7. Let multiple workers send freely from one mailbox without a central rate limiter.
8. Retry provider errors aggressively without backoff.
9. Ignore Retry-After.
10. Treat delivered as inboxed.
11. Treat tracking pixel open as proof a human read the message.
12. Send again to hard-bounced/unsubscribed/complaining leads.
13. Put critical campaign actions directly through browser → database without business validation.
14. Store uploads only on local Docker disk.
15. Build Kubernetes/microservices/Kafka complexity immediately.
16. Assume Resend can be used for cold outreach without re-checking its latest Acceptable Use Policy.
17. Assume any provider limit in this context remains current forever—verify before implementation.

---

# 139. CURRENT PREFERRED ARCHITECTURAL DECISIONS

At the end of the previous session, the preferred direction was:

### Frontend

Next.js + TypeScript + Tailwind + shadcn/ui

### Backend

Python + FastAPI

### ORM / validation

SQLAlchemy + Alembic + Pydantic

### Database/Auth/Storage/Realtime

Supabase

### Background processing

Redis + Celery + Python scheduler/Celery Beat

### Google

OAuth + Gmail API

### Microsoft

OAuth + Microsoft Graph

### Other mailboxes

Authenticated SMTP + IMAP/API where appropriate

### Product transactional email

A transactional provider such as Resend, subject to latest provider terms

### Deployment

Dockerized

### Production compute recommendation

AWS

### Initial AWS deployment

Keep it simple, likely EC2 + Docker Compose before introducing ECS

### Future scale

ECS / separately scalable worker pools if needed

---

# 140. ITEMS THAT ARE NOT YET FINALIZED

These remain OPEN and should be designed in later sessions:

- Exact MVP scope
- Exact database schema
- Exact API contracts
- Exact route structure
- Exact UI visual design
- Exact brand/design system
- Final sidebar naming
- Final naming of Standard vs Smart Personalized templates
- Exact plans/pricing
- Exact daily safety defaults for each provider/mailbox
- Exact rate-limit algorithms and thresholds
- Exact retry schedules
- Exact Google OAuth scopes
- Exact Microsoft Graph scopes
- Exact SMTP provider compatibility list
- Exact email verification provider
- Exact lead enrichment providers
- Exact CRM integrations for MVP
- Exact calendar/meeting integration
- Exact billing provider
- Exact observability stack beyond initial recommendations
- Exact AWS networking architecture
- Exact Supabase plan/configuration
- Whether Supabase remains permanent or is eventually moved to AWS RDS
- Whether to use Celery Beat vs a custom dedicated scheduling service long term
- Inbox reply synchronization strategy at large scale
- Tracking domain implementation details
- Email open/click tracking implementation details
- Compliance/legal requirements by target region
- Cold-email acceptable-use rules enforced by the SaaS
- Abuse-detection thresholds
- Customer risk scoring
- Domain/mailbox health scoring formula

---

# 141. RECOMMENDED NEXT STEP

The next session should NOT start coding immediately.

The logical next step is to create a complete **Product Requirements + System Architecture Specification**.

That should define, in order:

1. MVP vs later features
2. User roles and permissions
3. Full frontend page map
4. User flows
5. Database schema
6. Backend modules
7. REST API contracts
8. Campaign state machine
9. Lead state machine
10. Message/event state machine
11. Mailbox/provider architecture
12. Rate-limiter design
13. Scheduler design
14. Worker/queue design
15. Reply synchronization
16. Tracking service
17. Suppression system
18. Authentication and tenancy
19. Security model
20. Supabase architecture
21. AWS deployment architecture
22. Monitoring/logging
23. Compliance/abuse controls
24. Implementation roadmap
25. Testing strategy
26. Production launch requirements

After that, development can proceed module by module without constantly changing the architecture.

---

# 142. SHORT SUMMARY FOR THE NEXT AI

We are designing a production-grade outbound email SaaS from scratch.

The frontend should be Next.js/TypeScript.

The main backend should remain Python/FastAPI because the project owner is comfortable with Python and the workload is well suited to it.

Use Supabase for PostgreSQL/Auth/Storage/Realtime, but do not replace the Python business layer with Supabase serverless functions.

Use Redis + Celery for async campaign jobs, sending, mailbox synchronization, AI jobs, imports, analytics, and retries.

Use Google OAuth + Gmail API for Google accounts.

Use Microsoft OAuth + Graph for Microsoft accounts.

Use authenticated SMTP/IMAP only as the compatibility path for custom mailbox providers.

Do not send user cold campaigns through Resend unless its current terms explicitly permit the intended use; previous research found that Resend prohibited unsolicited cold outreach. Resend may still be used for the SaaS's own transactional mail.

Do not run direct-to-MX email infrastructure initially.

Do not rely on VPS IP rotation.

Implement provider-aware rate limiting using shared Redis state, mailbox/provider/workspace/campaign limits, token-bucket/throttling concepts, exponential backoff, jitter, error classification, and circuit breakers.

Use Docker from the beginning.

AWS is currently preferred over Hostinger VPS for production, while keeping initial AWS architecture simple.

The frontend needs Dashboard, Campaigns, Leads, Inbox, Templates, Mailboxes, Integrations, Team, Billing, Settings, Help/Education, plus a separate internal Admin/Compliance console.

Campaign creation should be a guided wizard:

Campaign Setup → Leads → Sequence → Sending Accounts → Schedule → Settings → Review → Start.

Email creation should offer Standard Template vs Smart Personalized Template instead of a separate AI Outreach campaign.

The product should educate users contextually about SMTP, SPF, DKIM, DMARC, bounces, opens, delivery, replies, suppression, and deliverability.

Open tracking should be treated as an estimated signal, not proof of human reading.

Replies and positive replies matter more than opens.

Suppression, bounce handling, unsubscribe handling, complaint handling, mailbox safety, and rate limiting are core backend requirements rather than optional analytics features.

Do not assume provider limits/policies written in this context are permanently current; re-check current Google, Microsoft, AWS, Hostinger, Resend, Supabase, etc. documentation before implementing provider-specific behavior.