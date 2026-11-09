# Page Map — Email Outreach SaaS

## 1. Purpose

This document defines the frontend information architecture for the email outreach platform.

It establishes:

- the primary application route structure
- top-level navigation
- page hierarchy
- page responsibilities
- major user actions available from each page
- relationships between pages
- route-level permission expectations
- empty, loading, error, and unavailable states
- how product user flows map into the interface

This document is the source of truth for **where product functionality lives**.

It does not define:

- database schemas
- API endpoints
- exact component implementations
- visual design system details
- backend architecture
- worker architecture
- exact role permissions
- state-machine implementation
- provider adapter internals
- queue topology
- rate-limiting implementation

Those concerns belong in their respective documents.

The master product context remains:

```text
docs/product/PROJECT_CONTEXT.md
```

MVP scope is defined in:

```text
docs/product/MVP.md
```

User journeys are defined in:

```text
docs/product/USER_FLOWS.md
```

Exact permissions are defined separately in:

```text
docs/product/USER_ROLES.md
```

Where this document says that a page or action is permission-sensitive, `USER_ROLES.md` determines the final behavior.

---

# 2. Information Architecture Principles

The product should feel like one coherent application rather than a collection of unrelated tools.

The primary information architecture should reflect the user's mental model:

```text
Dashboard
    ↓
Campaigns
    ↓
Leads
    ↓
Inbox
    ↓
Templates
    ↓
Mailboxes
    ↓
Integrations / Team / Settings
```

The navigation structure should prioritize the most frequent operational workflows.

Administrative and configuration-heavy areas should remain secondary.

---

# 3. Route Namespace

The application should use three primary route namespaces:

```text
/auth/*
    Authentication

/onboarding/*
    Initial workspace onboarding

/app/*
    Authenticated workspace application
```

Potential internal platform-administration interfaces should not be mixed into the ordinary customer workspace application.

If such interfaces are introduced later, they should use a separate protected namespace.

---

# 4. Root Route

```text
/
```

## Purpose

Resolve the visitor into the correct product destination.

## Behavior

Conceptually:

```text
Visit /
    ↓
Authenticated?
    ├── No
    │     ↓
    │   /auth/login
    │
    └── Yes
          ↓
      Workspace available?
          ├── No
          │     ↓
          │   /onboarding
          │
          └── Yes
                ↓
              /app
```

The root route should not become a product dashboard itself.

It is a routing entry point.

---

# 5. Authentication Routes

Authentication lives outside the workspace application shell.

Proposed structure:

```text
/auth
├── login
├── signup
├── forgot-password
├── reset-password
└── callback
```

---

# 6. `/auth/login`

## Purpose

Authenticate an existing user.

## Primary Actions

- enter credentials
- sign in
- access password recovery
- navigate to sign up

## Success

```text
Login
    ↓
Valid session
    ↓
Resolve workspace
    ↓
/app
```

or:

```text
No workspace
    ↓
/onboarding
```

## Required States

- default
- submitting
- invalid credentials
- authentication error
- expired/revoked session recovery

---

# 7. `/auth/signup`

## Purpose

Create a new user identity.

## Primary Actions

- enter required registration information
- create account
- proceed through required verification

## Success

```text
Account created
    ↓
Authenticated/verified
    ↓
/onboarding
```

## Required States

- default
- validation error
- account already exists
- submitting
- authentication-provider failure
- verification-required state where applicable

---

# 8. `/auth/forgot-password`

## Purpose

Start account recovery.

## Primary Actions

- enter account email
- request recovery/reset process

## Security Requirement

The interface should avoid unnecessarily revealing whether an arbitrary email address belongs to an account.

---

# 9. `/auth/reset-password`

## Purpose

Complete an authorized password-reset flow.

## States

- valid reset session
- expired reset session
- invalid reset session
- password validation error
- successful reset

---

# 10. `/auth/callback`

## Purpose

Handle authentication-related redirects where required by the authentication system.

This route should primarily be transitional.

It should not expose low-level provider details to the user.

Typical result:

```text
Callback
    ↓
Validate
    ↓
Establish session
    ↓
Redirect
```

---

# 11. Onboarding Namespace

Proposed structure:

```text
/onboarding
├── workspace
├── mailbox
├── leads
└── complete
```

The product may implement these as separate routes or a controlled onboarding stepper, but the information architecture should preserve these distinct responsibilities.

---

# 12. `/onboarding`

## Purpose

Resolve the user's onboarding progress and direct them to the appropriate incomplete step.

Conceptually:

```text
Workspace missing
    → /onboarding/workspace

Workspace exists, mailbox missing
    → /onboarding/mailbox

Mailbox connected, leads missing
    → /onboarding/leads

Minimum setup complete
    → /onboarding/complete
```

Onboarding convenience must not become backend authorization or campaign-validation logic.

---

# 13. `/onboarding/workspace`

## Purpose

Create the initial workspace.

## Primary Actions

- provide workspace name/details
- create workspace

## Success

```text
Workspace created
    ↓
/onboarding/mailbox
```

---

# 14. `/onboarding/mailbox`

## Purpose

Introduce mailbox connection.

## Provider Options

```text
Gmail
Microsoft
Custom SMTP
```

Provider connection details should use the same underlying product experiences available later under Mailboxes.

## Primary Actions

- connect Gmail
- connect Microsoft
- configure SMTP
- retry failed connection

## Optional Behavior

If product policy permits skipping this step, skipping must not allow campaigns to start without a valid mailbox.

---

# 15. `/onboarding/leads`

## Purpose

Help the user add the first recipients.

## Primary Actions

```text
Import CSV
Add lead manually
```

The full lead-management experience remains in `/app/leads`.

---

# 16. `/onboarding/complete`

## Purpose

Confirm that the initial setup is complete enough to enter the primary product.

## Primary Action

```text
Go to Dashboard
```

Optional secondary action:

```text
Create First Campaign
```

---

# 17. Authenticated Application Shell

The authenticated application lives under:

```text
/app
```

All `/app/*` pages must require:

```text
Authenticated user
+
Valid workspace membership
```

Individual pages and actions may require additional permissions.

---

# 18. Primary Application Navigation

Recommended primary navigation:

```text
Dashboard

Campaigns

Leads

Inbox

Templates

Mailboxes
```

Recommended secondary / workspace navigation:

```text
Integrations
Team
Billing
Settings
```

These secondary areas may appear toward the bottom of the sidebar or inside a workspace/settings section.

---

# 19. Persistent Workspace Controls

The application shell should provide access to:

```text
Workspace selector
User/account menu
Notification entry point
Help/support entry point where applicable
```

The workspace selector is required if multiple workspace membership is supported.

Changing workspace should reload workspace-scoped state.

---

# 20. `/app`

## Purpose

Workspace application entry route.

## Behavior

Recommended:

```text
/app
    ↓
redirect or resolve to
/app/dashboard
```

Keeping a canonical dashboard route makes deep linking and page ownership clearer.

---

# 21. `/app/dashboard`

## Purpose

Provide an operational overview of the current workspace.

The dashboard should answer:

- What is currently happening?
- Is anything blocked?
- What requires attention?
- What should I do next?

## Recommended Sections

### Campaign Overview

Examples:

- active campaigns
- paused campaigns
- campaigns requiring attention
- recent completions

### Sending Overview

Examples:

- recent sent activity
- failures
- replies
- bounces
- unsubscribes

### Mailbox Health

Examples:

- connected mailbox count
- unhealthy/disconnected mailboxes
- important provider issues

### Analytics and Deliverability

Workspace-level analytics and sending-health signals are sections of the dashboard, not separate pages.

Analytics section:

- date range presets (today, yesterday, 7 days, 30 days)
- outreach KPIs (sent, replies, bounces, unsubscribes, complaints, send failures, opens when tracked)
- activity trend
- top campaigns and top mailboxes

Deliverability section:

- overall health status and active safety holds
- active deliverability warnings
- per-mailbox health
- send-failure breakdown

The selected date range applies to both sections.

### Recent Activity

Examples:

- campaign started
- import completed
- reply received
- mailbox disconnected

### Quick Actions

Potential actions:

```text
Create Campaign
Import Leads
Connect Mailbox
Create Template
```

## Empty State

For a new workspace:

```text
Welcome
    ↓
Connect mailbox
    ↓
Import leads
    ↓
Create campaign
```

The dashboard should guide activation rather than display meaningless zero-valued charts.

---

# 22. Campaign Route Structure

Recommended structure:

```text
/app/campaigns
├── new
└── [campaign_id]
    ├── overview
    ├── audience
    ├── sequence
    ├── senders
    ├── schedule
    ├── settings
    ├── activity
    └── analytics
```

The exact physical implementation may use nested routes or a shared campaign page with tabs.

The information architecture should preserve these conceptual sections.

---

# 23. `/app/campaigns`

## Purpose

Browse and manage workspace campaigns.

## Primary Content

Campaign list/table/cards containing appropriate summaries such as:

- campaign name
- status
- recipient count
- progress
- relevant outcome summary
- last activity
- created/updated information

## Primary Actions

```text
Create Campaign
Open Campaign
Pause where permitted
Resume where permitted
Archive where permitted
```

Bulk actions should be introduced only when product behavior is clearly defined.

## Filters

Potential filters:

```text
All
Draft
Scheduled
Running
Paused
Completed
Archived
Needs Attention
```

These should align with the final campaign state model.

## Search

Search campaigns by relevant identifiers such as name.

## Empty State

```text
No campaigns yet
    ↓
Create Campaign
```

---

# 24. `/app/campaigns/new`

## Purpose

Create a new campaign.

The campaign creation experience should guide the user through the required configuration areas:

```text
1. Basics
2. Audience
3. Sequence
4. Senders
5. Schedule
6. Settings
7. Review
```

The exact UI may be:

- stepper
- tabs
- guided form
- hybrid configuration experience

but the product responsibilities remain the same.

---

# 25. Campaign Creation — Basics

## Purpose

Define the campaign identity and high-level metadata.

## Typical Fields

- campaign name
- other required descriptive information

## Primary Action

```text
Continue to Audience
```

A draft may be saved before the full configuration is complete.

---

# 26. Campaign Creation — Audience

## Purpose

Select the recipients intended for the campaign.

## Primary Actions

- choose lead list
- choose supported recipient collection
- import leads from a CSV straight into the campaign (repeatable; each file is
  added to the current selection)
- review candidate recipient count

## Important UI Distinction

The screen should distinguish:

```text
Selected recipients
```

from:

```text
Currently sendable recipients
```

where the product has enough information to make that distinction.

Selection does not override suppression.

---

# 27. Campaign Creation — Sequence

## Purpose

Build the outreach sequence.

## MVP Step Types

```text
Email
Wait
```

## Primary Actions

- add email step
- add wait step
- reorder supported steps
- edit email content
- select template
- create supported content
- remove step
- preview content

## Example

```text
Email 1
↓
Wait 2 days
↓
Email 2
↓
Wait 3 days
↓
Email 3
```

## Validation

The page should surface incomplete sequence configuration before review/start.

---

# 28. Campaign Creation — Senders

## Purpose

Choose the workspace mailboxes authorized to send the campaign.

## Primary Content

Eligible mailbox list showing enough status information for selection.

Potential status concepts:

```text
Connected
Needs Attention
Disconnected
Unavailable
```

Exact mailbox states come from mailbox architecture.

## Primary Actions

- select mailbox
- select multiple mailboxes if supported
- navigate to connect mailbox if none are eligible

## Empty State

```text
No eligible mailbox
    ↓
Connect Mailbox
```

---

# 29. Campaign Creation — Schedule

## Purpose

Configure allowed campaign sending times.

## MVP Inputs

```text
Timezone
Sending days
Sending hours
Basic daily limits
```

## Required UX

The user should be able to understand when sending can occur.

Ambiguous timezone behavior should be avoided.

---

# 30. Campaign Creation — Settings

## Purpose

Configure additional campaign-level behavior that does not belong to audience, sequence, senders, or schedule.

Only settings explicitly supported by the product should appear here.

Do not turn this into a dumping ground for unrelated configuration.

Potential concepts may include:

- supported stopping behavior
- campaign-level sender behavior
- other approved MVP campaign settings

Exact options must come from product specifications.

---

# 31. Campaign Creation — Review

## Purpose

Provide a final pre-start summary.

## Required Review Areas

```text
Campaign
Audience
Sequence
Senders
Schedule
Limits/settings
Warnings
Blocking errors
```

## Primary Actions

```text
Back to Edit
Start Campaign
```

Start must invoke authoritative backend validation.

## Blocking State

If the campaign is invalid:

```text
Cannot Start
    ↓
Show exact blocking problems
    ↓
Link user to affected configuration
```

---

# 32. `/app/campaigns/[campaign_id]`

## Purpose

Canonical campaign detail entry point.

Recommended default:

```text
/app/campaigns/[campaign_id]/overview
```

The page header should consistently expose:

- campaign name
- campaign status
- relevant top-level actions
- important health/warning state

---

# 33. `/app/campaigns/[campaign_id]/overview`

## Purpose

Provide the operational summary of one campaign.

## Recommended Content

- status
- recipients
- sequence summary
- selected mailboxes
- schedule summary
- sending progress
- meaningful outcomes
- warnings/blockers
- recent activity

## Primary Actions

Depending on campaign state and permissions:

```text
Edit
Start
Pause
Resume
Archive
```

Exact action availability comes from the campaign state machine and RBAC.

---

# 34. `/app/campaigns/[campaign_id]/audience`

## Purpose

Inspect the campaign's recipient population.

## Content

Potential recipient-level information:

- lead identity
- contact details
- sequence progress
- send eligibility/status
- replied state
- suppression state
- bounce state
- unsubscribe state
- relevant message outcome

## Actions

Only actions explicitly allowed by campaign state and role should be exposed.

---

# 35. `/app/campaigns/[campaign_id]/sequence`

## Purpose

View and, where allowed, edit campaign sequence configuration.

## Important Behavior

Editing rules depend on campaign state.

For example:

```text
Draft
    → editable

Running
    → potentially restricted
```

The exact semantics must come from the campaign architecture.

The UI must not silently permit modifications that the backend considers invalid.

## Hyper-personalized campaigns

For a `HYPER_PERSONALIZED` campaign this page also hosts the whole authoring flow ([ADR-0016](../adr/0016-api-process-ai-drafting-and-company-analysis.md)), so there is no separate Personalization page (`/personalization` redirects here):

```text
Setup panel
    Company: website OR business info (name + description)
    Email style: Text | HTML (brand kit: logo, colours, font, optional CTA button)
    Objective (prefilled from the company review)
Timeline: Generate emails / Regenerate all / per-email Regenerate (with an optional "what should change?" note)
Preview & approve: sample previews, manager approval
```

If AI drafting is unavailable in the deployment the AI actions are hidden and the manual objective and email editor keep working. In a non-draft campaign the panel is read-only.

---

# 36. `/app/campaigns/[campaign_id]/senders`

## Purpose

Show campaign sending mailbox configuration.

## Content

- selected mailboxes
- connection health
- sender availability
- relevant sending limits/conditions

## Actions

Where allowed:

- add mailbox
- remove mailbox
- resolve disconnected mailbox

Changes during active campaigns require explicit architecture rules.

---

# 37. `/app/campaigns/[campaign_id]/schedule`

## Purpose

Display and manage campaign schedule where campaign state permits.

## Content

- timezone
- allowed days
- allowed hours
- supported sending limits
- next relevant execution information where useful

---

# 38. `/app/campaigns/[campaign_id]/settings`

## Purpose

Hold campaign-specific configuration not represented elsewhere.

This page should remain narrow.

Do not duplicate global workspace settings here.

---

# 39. `/app/campaigns/[campaign_id]/activity`

## Purpose

Provide an operational activity history for the campaign.

Potential entries:

```text
Campaign created
Campaign started
Campaign paused
Campaign resumed
Messages scheduled
Messages sent
Temporary failures
Permanent failures
Replies detected
Unsubscribes
Bounces
Complaints
Mailbox problems
Campaign completed
```

This is different from analytics.

Activity answers:

> What happened?

Analytics answers:

> How did the campaign perform?

## Implemented scope

The Activity tab follows Analytics. It is a read-only, newest-first feed built from
persisted facts (`GET .../analytics/campaigns/{campaign_id}/activity`), with type,
status, date-range and recipient/subject search filters and cursor pagination.

Included: sent, opened (one entry per email with its open count; automated hits
excluded), bounced, replied (confirmed matches only), unsubscribed, spam complaints
and send failures (including emails waiting to retry).

Not included:

- Link clicks are not tracked, so there is no click entry.
- Providers do not confirm delivery, so there is no delivered entry.
- An unsubscribe that arrives after the recipient's enrollment already ended is not
  attributed to the campaign.
- The campaign lifecycle entries listed above (created, paused, resumed, completed)
  are not part of this feed.

---

# 40. `/app/campaigns/[campaign_id]/analytics`

## Purpose

Provide campaign-specific performance information.

## MVP Focus

Prioritize reliable signals:

- recipients
- attempted
- sent
- failures
- bounces
- replies
- unsubscribes
- complaints where available

If opens/clicks exist in the approved product scope, they should be presented with appropriate measurement caveats.

---

# 41. Lead Route Structure

Recommended:

```text
/app/leads
├── [lead_id]
├── lists
│   └── [list_id]
├── imports
│   └── [import_id]
└── suppression
```

---

# 42. `/app/leads`

## Purpose

Primary lead database for the current workspace.

## Content

Lead table/list.

Potential columns:

- name
- email
- company
- list membership
- outreach status
- suppression state
- recent campaign/reply indicators

Exact lead fields come from the domain model.

## Primary Actions

```text
Add Lead
Import CSV
Add to List
Open Lead
```

Additional actions depend on permissions.

## Filters

Potential filters:

- list
- suppressed/not suppressed
- replied
- bounced
- unsubscribed
- other supported lifecycle categories

## Empty State

```text
No leads yet
    ↓
Add Lead
or
Import CSV
```

---

# 43. Add Lead Experience

The manual lead creation experience may be:

```text
/app/leads/new
```

or a modal/drawer from `/app/leads`.

The page map treats it as a lead-domain action rather than requiring a specific presentation mechanism.

## Purpose

Create one lead.

## Core Behavior

```text
Enter lead details
    ↓
Validate
    ↓
Create
    ↓
Optionally assign to list
```

---

# 44. `/app/leads/[lead_id]`

## Purpose

Provide the detailed record for one lead.

## Recommended Sections

### Profile

- contact information
- workspace-defined lead data

### Lists

- list memberships

### Outreach

- campaigns/messages associated with the lead

### Conversation

- relevant replies/conversation linkage

### Sending Safety

- suppression
- unsubscribe
- bounce/complaint indicators

## Actions

Depending on permission:

```text
Edit Lead
Add/Remove List Membership
Suppress
View Conversation
```

Potential destructive actions must follow the relevant product rules.

---

# 45. `/app/leads/lists`

## Purpose

Manage reusable lead collections.

## Content

List overview containing:

- list name
- member count
- created/updated information
- campaign usage where appropriate

## Primary Actions

```text
Create List
Open List
Rename/Edit
Delete where permitted
```

## Empty State

```text
No lists yet
    ↓
Create List
```

---

# 46. `/app/leads/lists/[list_id]`

## Purpose

Inspect and manage one lead list.

## Content

- list details
- member count
- member table
- relevant campaign usage

## Actions

- add leads
- remove leads
- import leads into list where supported
- edit list information
- delete where permitted

Removing a lead from a list should not necessarily delete the lead from the workspace.

---

# 47. `/app/leads/imports`

## Purpose

Show CSV import history and create new imports.

## Content

Each import should expose meaningful state such as:

```text
Processing
Completed
Completed with errors
Failed
```

Potential summary fields:

- file/import name
- start time
- status
- total rows
- imported rows
- rejected rows

## Primary Action

```text
Import CSV
```

---

# 48. Lead Import Creation

Potential route:

```text
/app/leads/imports/new
```

The experience should cover:

```text
Upload
    ↓
Inspect
    ↓
Map fields
    ↓
Validate
    ↓
Confirm
    ↓
Process
```

The user should not need to keep this page open for asynchronous processing to continue.

---

# 49. `/app/leads/imports/[import_id]`

## Purpose

Show the result of one import.

## Content

- status
- total rows
- successful rows
- invalid rows
- duplicates where applicable
- processing errors
- downloadable/reviewable failure information where supported

## Important Principle

Users should be able to determine what happened without reading backend logs.

---

# 50. `/app/leads/suppression`

## Purpose

Manage workspace suppression records.

## Content

Potential fields:

- recipient/email
- reason
- source
- created date
- relevant campaign/event
- removal eligibility where applicable

## Primary Actions

Depending on permission and suppression semantics:

```text
Add Suppression
Inspect Suppression
Remove Suppression
```

Removal should not be available blindly for every suppression reason.

## Empty State

```text
No suppression records
```

This is a legitimate empty state and should not aggressively prompt the user to create suppressions.

---

# 51. Inbox Route Structure

Recommended:

```text
/app/inbox
└── [conversation_id]
```

---

# 52. `/app/inbox`

## Purpose

Provide a unified view of supported outreach conversations.

## Content

Conversation list with useful context such as:

- recipient
- mailbox
- campaign
- latest message
- reply status
- timestamp
- unread/read state if supported

## Filters

Potential:

```text
All
Unread
Replied
Campaign
Mailbox
```

Only filters backed by the product model should be implemented.

## Empty State

```text
No conversations yet
```

The inbox is not required to mimic a complete Gmail/Outlook client.

---

# 53. `/app/inbox/[conversation_id]`

## Purpose

Display one synchronized outreach conversation.

## Content

- recipient/context
- outbound campaign messages
- inbound replies
- timestamps
- campaign association
- mailbox association
- relevant recipient state

If replying from inside the unified inbox is part of the approved product scope, that action should live here.

If it is not in the approved scope, the page should remain a conversation-review experience rather than implying unsupported compose behavior.

---

# 54. Template Route Structure

Recommended:

```text
/app/templates
├── new
└── [template_id]
```

---

# 55. `/app/templates`

## Purpose

Browse and manage reusable email templates.

## Content

Potential fields:

- template name
- subject
- type/content mode
- last updated
- usage information where useful

## Primary Actions

```text
Create Template
Open
Edit
Duplicate where supported
Delete where permitted
```

## Empty State

```text
No templates yet
    ↓
Create Template
```

---

# 56. `/app/templates/new`

## Purpose

Create reusable campaign content.

The creation entry point should support the product's template modes.

The initial/core path is standard user-authored content.

If the personalized/AI-assisted template experience is enabled according to the approved roadmap, this route should branch conceptually:

```text
Create Template
    ↓
Choose Mode
    ├── Standard / Custom
    └── Personalized / AI-assisted
```

The final naming of those modes should be explicitly approved before implementation.

The product should not create an entirely separate campaign area solely for personalized templates.

---

# 57. `/app/templates/[template_id]`

## Purpose

View/edit one template.

## Content

- template name
- subject
- body/content
- supported variables
- preview
- relevant usage context

## Important Rule

The eventual architecture must define whether editing a template changes content already attached to running/planned campaigns.

The UI must reflect that behavior explicitly.

---

# 58. Mailbox Route Structure

Recommended:

```text
/app/mailboxes
├── connect
│   ├── gmail
│   ├── microsoft
│   └── smtp
└── [mailbox_id]
```

---

# 59. `/app/mailboxes`

## Purpose

Manage all workspace sending mailboxes.

## Content

Each mailbox should expose useful operational information such as:

- mailbox identity
- provider
- connection status
- sending eligibility
- health state
- relevant configured limits
- last significant issue/activity

## Primary Actions

```text
Connect Mailbox
Open Mailbox
Reconnect
Disconnect
```

Actions depend on permission.

## Empty State

```text
No mailboxes connected
    ↓
Connect Gmail
Connect Microsoft
Connect SMTP
```

---

# 60. `/app/mailboxes/connect`

## Purpose

Select a mailbox provider.

## Provider Options

```text
Gmail
Microsoft
Custom SMTP
```

Each provider then enters its specific connection flow.

---

# 61. `/app/mailboxes/connect/gmail`

## Purpose

Begin/complete Gmail connection.

The route may redirect to OAuth quickly.

The user-facing experience should still provide:

- provider identification
- explanation of requested connection
- connection result
- failure/retry state

---

# 62. `/app/mailboxes/connect/microsoft`

Equivalent responsibility for Microsoft OAuth.

---

# 63. `/app/mailboxes/connect/smtp`

## Purpose

Configure a custom SMTP connection.

## Inputs

Only required supported configuration fields should appear.

## Flow

```text
Enter configuration
    ↓
Validate
    ↓
Test connection
    ↓
Success
    ↓
Mailbox available
```

Invalid credentials/configuration must not produce a healthy mailbox state.

---

# 64. `/app/mailboxes/[mailbox_id]`

## Purpose

Provide detailed mailbox management.

## Recommended Sections

### Overview

- provider
- identity
- connection status
- health
- recent activity

### Sending

- configured limits
- sending eligibility
- relevant usage indicators

### Connection

- provider connection details that are safe to expose
- reconnect
- disconnect

### Issues

- known failures
- action required

Secrets and raw tokens must never be exposed here.

---

# 65. Deliverability Route Structure

Deliverability is no longer a separate page. Its content is a section of `/app/dashboard` (see section 21 and section 105).

`/app/deliverability` remains only as a redirect to `/app/dashboard` so existing bookmarks keep working.

Potential future expansion may introduce mailbox-specific or campaign-specific subpages, but the MVP can remain focused.

---

# 66. Deliverability section of `/app/dashboard`

## Purpose

Present actionable sending-health information without pretending to be a complete deliverability laboratory.

## MVP Content

Potential signals:

- mailbox health
- send failures
- bounce trends
- complaint indicators
- unsubscribe indicators
- reply outcomes
- relevant warnings

## Important Principle

Only platform-known, supportable signals should be presented.

Avoid inventing opaque reputation scores without a defined model.

---

# 67. Analytics Route Structure

Workspace-level analytics is no longer a separate page. Its content is a section of `/app/dashboard` (see section 21 and section 104).

`/app/analytics` remains only as a redirect to `/app/dashboard` so existing bookmarks keep working.

Campaign-specific analytics remain under:

```text
/app/campaigns/[campaign_id]/analytics
```

---

# 68. Workspace analytics section of `/app/dashboard`

## Purpose

Provide workspace-level outreach analytics.

## Potential Sections

```text
Overall performance
Campaign comparison
Sending outcomes
Replies
Bounces
Unsubscribes
Complaints
Mailbox-level performance where useful
```

The MVP should prioritize reliable operational and response signals.

Advanced attribution can come later.

---

# 69. Integrations Route Structure

Recommended:

```text
/app/integrations
```

Mailboxes themselves remain under `/app/mailboxes`.

This page is for additional platform integrations and integration-level configuration.

---

# 70. `/app/integrations`

## Purpose

Provide a central view of supported external product integrations that are not simply sending mailboxes.

## Content

- available integrations
- connected integrations
- connection state
- configuration entry points

Do not duplicate mailbox management here.

If Gmail/Microsoft connection cards appear for discovery, they should link to Mailboxes rather than create parallel connection ownership.

---

# 71. Team Route Structure

Recommended:

```text
/app/team
```

Optional member detail route:

```text
/app/team/[member_id]
```

only if product complexity justifies it.

---

# 72. `/app/team`

## Purpose

Manage workspace membership and roles.

## Content

- member identity
- role
- membership status
- invitation status
- relevant join information

## Primary Actions

Depending on permission:

```text
Invite Member
Change Role
Remove Member
Resend/Revoke Invitation
```

The UI must obey ownership and privilege rules defined in `USER_ROLES.md`.

---

# 73. Invite Team Member Experience

Could be:

```text
/app/team/invite
```

or a modal/dialog from the team page.

## Core Flow

```text
Enter invitee
    ↓
Choose permitted role
    ↓
Validate
    ↓
Send invitation
```

Lower-privileged users must not be able to assign roles they are not allowed to grant.

---

# 74. Billing Route Structure

Recommended:

```text
/app/billing
```

Exact billing functionality depends on the product/business model defined elsewhere.

---

# 75. `/app/billing`

## Purpose

Provide workspace billing and plan information.

Potential content:

- current plan
- subscription status
- usage relevant to billing
- invoices/payment management
- plan changes

Exact availability is permission-sensitive.

Billing should not be distributed across unrelated settings pages.

---

# 76. Settings Route Structure

Recommended:

```text
/app/settings
├── workspace
├── sending
├── notifications
├── security
└── danger
```

Only settings actually required by the product should be included.

---

# 77. `/app/settings`

## Purpose

Settings entry route.

Recommended behavior:

```text
/app/settings
    ↓
/app/settings/workspace
```

---

# 78. `/app/settings/workspace`

## Purpose

Manage general workspace configuration.

Potential settings:

- workspace name
- workspace-level product preferences
- other approved general configuration

Do not mix team membership here because Team has its own product area.

---

# 79. `/app/settings/sending`

## Purpose

Manage workspace-wide sending defaults or policies that are not mailbox-specific or campaign-specific.

Only settings clearly owned at the workspace level should live here.

Campaign-specific values belong to campaigns.

Mailbox-specific values belong to mailboxes.

---

# 80. `/app/settings/notifications`

## Purpose

Manage supported workspace/user notification preferences.

Potential examples:

- campaign failure notifications
- mailbox health notifications
- import completion notifications

Exact notification channels and controls require a separate specification if they become complex.

---

# 81. `/app/settings/security`

## Purpose

Expose workspace security-related configuration appropriate for customer users.

Potential future areas may include:

- API keys
- active security integrations
- workspace security configuration

Security-sensitive operations must be strongly permission-controlled.

If API keys become substantial functionality, they may later deserve a dedicated route:

```text
/app/settings/api-keys
```

---

# 82. `/app/settings/danger`

## Purpose

Centralize high-impact workspace actions.

Potential actions:

```text
Transfer Ownership
Delete Workspace
```

only where supported and authorized.

These actions should not appear casually throughout the application.

## UX Requirement

High-impact operations require:

- explicit context
- clear consequence explanation
- strong confirmation
- backend authorization
- auditability where appropriate

---

# 83. Notification Center

If an in-product notification center is included, recommended route:

```text
/app/notifications
```

## Purpose

Show important operational notices such as:

- mailbox disconnected
- import completed with failures
- campaign blocked
- serious sending error
- other actionable workspace events

The notification center should not become a duplicate activity log for every low-value event.

---

# 84. Search

Global search should not be added merely because SaaS products commonly have one.

If introduced, it should search only clearly supported entities such as:

```text
Campaigns
Leads
Templates
Conversations
```

The route/UI should be introduced only when the product has enough entities and data scale to justify it.

It is not an MVP requirement unless explicitly defined elsewhere.

---

# 85. Global Create Actions

The application may expose a global creation control for common tasks.

Potential actions:

```text
Create Campaign
Add Lead
Import Leads
Create Template
Connect Mailbox
```

This is optional UX.

Every action must route into the same authoritative product flow rather than create parallel implementations.

---

# 86. Workspace Switching

The workspace selector should be accessible from the application shell.

Flow:

```text
Current workspace
    ↓
Open selector
    ↓
Authorized workspaces
    ↓
Select workspace
    ↓
Workspace context changes
    ↓
Navigate/reload into safe destination
```

Recommended safe default after switching:

```text
/app/dashboard
```

Alternatively, the application may preserve the equivalent route only when that destination can be safely resolved in the new workspace.

Never attempt to preserve a workspace-specific resource ID from the previous workspace.

Example:

```text
Workspace A:
/app/campaigns/campaign_A

switch to Workspace B
```

must not attempt to open:

```text
/app/campaigns/campaign_A
```

inside Workspace B.

---

# 87. Resource Not Found

Workspace-scoped resources require a standard not-found experience.

Examples:

```text
Campaign not found
Lead not found
Template not found
Conversation not found
Mailbox not found
```

The application should not reveal whether a resource exists in another unauthorized workspace.

A cross-tenant unauthorized lookup should not leak resource details.

---

# 88. Permission Denied

The application needs a consistent permission-denied experience.

Conceptually:

```text
User reaches protected action/page
    ↓
Authorization denied
    ↓
No sensitive information exposed
    ↓
Clear permission message
```

Depending on the scenario, the user may:

- remain on the page with read-only access
- lose the unavailable action
- receive a dedicated unauthorized state
- be redirected

Exact behavior depends on the final RBAC model.

---

# 89. Workspace Membership Lost

A user's membership may be removed while the application is open.

Expected flow:

```text
User currently in workspace
    ↓
Membership revoked
    ↓
Next authoritative request fails
    ↓
Application detects lost access
    ↓
Workspace-scoped data cleared from active UI
    ↓
Redirect to another authorized workspace
```

or:

```text
No remaining workspace
    ↓
Appropriate account/workspace state
```

The UI must not continue pretending access exists because stale client state remains loaded.

---

# 90. Session Expiration

Expected behavior:

```text
Authenticated session expires
    ↓
Protected request fails
    ↓
Application attempts valid session recovery where supported
```

If recovery is impossible:

```text
Redirect to login
```

After reauthentication, the user may return to an appropriate safe location.

---

# 91. Loading States

Every asynchronous page should distinguish:

```text
Initial loading
Refreshing
Submitting action
Long-running background process
```

Do not block the entire interface unnecessarily because one secondary data source is refreshing.

---

# 92. Long-Running Jobs

Long-running operations include:

- CSV imports
- campaign execution
- some synchronization operations
- potentially large analytics calculations

These should expose persisted status.

Example:

```text
Import started
    ↓
User leaves page
    ↓
Processing continues
    ↓
User returns
    ↓
Current persisted state shown
```

The browser must not be the durable owner of these operations.

---

# 93. Standard Empty States

## Campaigns

```text
No campaigns
→ Create Campaign
```

## Leads

```text
No leads
→ Add Lead
→ Import CSV
```

## Lists

```text
No lists
→ Create List
```

## Templates

```text
No templates
→ Create Template
```

## Mailboxes

```text
No mailboxes
→ Connect Mailbox
```

## Inbox

```text
No conversations yet
```

## Analytics

```text
Not enough activity yet
```

## Suppression

```text
No suppression records
```

Empty states should be domain-specific and actionable where appropriate.

---

# 94. Standard Error States

The frontend should distinguish at least:

```text
Validation error
Permission error
Not found
Provider/configuration error
Temporary service error
Permanent operation failure
```

Avoid a generic:

```text
Something went wrong
```

when actionable information is available.

---

# 95. Offline / Connectivity Interruption

Where browser connectivity is lost:

- unsaved work should not silently appear saved
- mutating operations must not be assumed successful
- the product should clearly distinguish pending local UI from confirmed server state

The MVP does not require a full offline application.

---

# 96. Navigation State for Attention-Required Resources

Resources requiring user attention may surface indicators.

Examples:

```text
Mailbox disconnected
Campaign blocked
Import failed
```

Potential navigation treatment:

- badge
- warning indicator
- notification

Attention indicators should correspond to actionable conditions rather than become permanent visual noise.

---

# 97. Campaign Header Behavior

Campaign pages should share a consistent campaign header.

Recommended content:

```text
Campaign name
Campaign status
Relevant warnings
Primary state action
Secondary actions
```

Examples of primary state actions:

```text
Start
Pause
Resume
```

Only the valid action for the campaign's current state should be emphasized.

---

# 98. Lead Detail Navigation

A lead detail page should allow the user to understand:

```text
Who is this person?
Where are they organized?
What outreach have we sent?
Did they reply?
Are they suppressed?
```

The page should not force users to jump across multiple unrelated product areas to answer basic lead-level questions.

---

# 99. Mailbox Detail Navigation

A mailbox detail page should answer:

```text
Is this mailbox connected?
Can it currently send?
Which provider is it?
Is anything wrong?
What should the user do?
```

Connection problems should have a direct recovery path.

---

# 100. Inbox-to-Lead Context

Conversation pages should provide a safe link to the corresponding lead where available.

Conceptually:

```text
Conversation
    ↓
Lead
```

Similarly, lead detail may link to the relevant conversation.

This creates a coherent relationship between the lead database and unified inbox.

---

# 101. Inbox-to-Campaign Context

A campaign-related conversation should expose its campaign context where known.

Conceptually:

```text
Conversation
    ↓
Campaign
```

The user should be able to understand which outreach caused the reply.

---

# 102. Campaign-to-Lead Context

Campaign recipient views should allow navigation to the lead.

Conceptually:

```text
Campaign recipient
    ↓
Lead detail
```

This relationship should not create duplicate lead records inside campaigns.

Campaign recipient state and lead identity are related but distinct concepts.

---

# 103. Dashboard-to-Attention Flow

The dashboard should serve as a prioritization surface.

Examples:

```text
Mailbox needs reconnection
    ↓
Click
    ↓
Mailbox detail
```

```text
Campaign blocked
    ↓
Click
    ↓
Campaign overview / affected configuration
```

```text
Import completed with errors
    ↓
Click
    ↓
Import detail
```

Dashboard cards should lead somewhere actionable.

---

# 104. Analytics Navigation Principle

Use:

```text
/app/dashboard
```

for workspace-level insights.

Use:

```text
/app/campaigns/[campaign_id]/analytics
```

for one campaign.

Avoid creating multiple unrelated analytics areas for the same data.

---

# 105. Deliverability Navigation Principle

Deliverability is operational health, not general reporting.

Workspace analytics and deliverability now live together on `/app/dashboard`, as separate sections:

```text
Analytics section
    = What happened?

Deliverability section
    = Are we seeing warning signs affecting sending health?
```

The two may share underlying data but each section should preserve its distinct user goal.

The consolidation is a product decision: three overlapping pages (Dashboard, Analytics, Deliverability) were merged into one. The backend endpoints are unchanged.

---

# 106. Settings Ownership Rule

Before adding a setting, determine which entity owns it.

Use this decision tree:

```text
Affects one campaign?
    → Campaign settings

Affects one mailbox?
    → Mailbox detail

Affects workspace-wide sending behavior?
    → Workspace sending settings

Affects user/workspace notification behavior?
    → Notification settings

Affects workspace identity?
    → Workspace settings
```

Avoid duplicate settings in multiple locations.

---

# 107. Route Permission Classification

Every route should eventually be assigned one of the following permission characteristics:

```text
Authenticated
Workspace-member
Read permission
Manage permission
Administrative permission
Owner-sensitive
Internal-only
```

Exact mappings belong in `USER_ROLES.md`.

Example conceptually:

| Route | Permission Type |
|---|---|
| Dashboard | workspace read |
| Campaign list | campaign read |
| Campaign create | campaign create |
| Campaign start | campaign execute |
| Leads | lead read/manage |
| Suppression | suppression read/manage |
| Mailboxes | mailbox read/manage |
| Team | team read/manage |
| Billing | billing-sensitive |
| Danger settings | owner/security-sensitive |

This table is illustrative, not the final RBAC matrix.

---

# 108. Route Guards

All `/app/*` routes should conceptually follow:

```text
Request page
    ↓
Authenticated?
    ├── No → Login
    └── Yes
          ↓
Workspace membership?
    ├── No → Resolve workspace state
    └── Yes
          ↓
Route permission?
    ├── No → Permission state
    └── Yes → Render page
```

Page guards are a UX layer.

Backend authorization remains mandatory for data/API operations.

---

# 109. Mobile and Responsive Information Architecture

Responsive behavior should preserve page hierarchy.

On smaller screens:

- primary navigation may collapse
- secondary settings may move into menus
- tables may become cards or horizontally scroll
- page actions may collapse into menus

But the route structure and conceptual page ownership should remain stable.

A mobile layout should not create a separate product architecture.

---

# 110. Browser Back and Deep Linking

Important application states should support predictable navigation.

Users should be able to:

- bookmark a campaign
- open a lead directly
- open a conversation directly
- open a mailbox directly
- return from detail to collection

Critical resource pages should have stable canonical URLs.

Transient UI states such as unsaved modal forms do not necessarily require unique URLs.

---

# 111. URL Identifier Rules

Dynamic routes use opaque resource identifiers:

```text
[campaign_id]
[lead_id]
[list_id]
[import_id]
[conversation_id]
[template_id]
[mailbox_id]
```

The frontend must never assume that possession of an identifier implies authorization.

The backend must verify workspace ownership and permission.

---

# 112. Archived Content

Archived resources should generally remain accessible through filtered or dedicated historical views where appropriate.

For campaigns:

```text
Campaigns
    ↓
Archived filter
```

A separate `/archive` route is not necessary unless later product requirements justify one.

---

# 113. Deleted Content

Hard-deleted resources should not remain navigable through ordinary detail routes.

If soft-delete/recovery capabilities are introduced later, they require an explicit product specification rather than being assumed in the page map.

---

# 114. Page-Level Audit Visibility

The product does not require a universal audit-log page in the MVP unless defined elsewhere.

However, critical pages may expose relevant activity.

Campaign activity is explicitly located at:

```text
/app/campaigns/[campaign_id]/activity
```

Broader workspace audit logs may become a later administrative capability.

---

# 115. Internal Platform Administration

Customer workspace administration and platform-operator administration are different products.

Do not place internal abuse/safety controls under ordinary customer routes such as:

```text
/app/settings
```

If a platform administration interface is later created, use an isolated namespace such as:

```text
/internal/*
```

or another deliberately protected route namespace.

The exact route is intentionally not frozen here.

---

# 116. MVP Route Map

The production MVP route structure is therefore approximately:

```text
/
│
├── auth/
│   ├── login
│   ├── signup
│   ├── forgot-password
│   ├── reset-password
│   └── callback
│
├── onboarding/
│   ├── workspace
│   ├── mailbox
│   ├── leads
│   └── complete
│
└── app/
    ├── dashboard
    │
    ├── campaigns/
    │   ├── new
    │   └── [campaign_id]/
    │       ├── overview
    │       ├── audience
    │       ├── sequence
    │       ├── senders
    │       ├── schedule
    │       ├── settings
    │       ├── activity
    │       └── analytics
    │
    ├── leads/
    │   ├── [lead_id]
    │   ├── lists/
    │   │   └── [list_id]
    │   ├── imports/
    │   │   ├── new
    │   │   └── [import_id]
    │   └── suppression
    │
    ├── inbox/
    │   └── [conversation_id]
    │
    ├── templates/
    │   ├── new
    │   └── [template_id]
    │
    ├── mailboxes/
    │   ├── connect/
    │   │   ├── gmail
    │   │   ├── microsoft
    │   │   └── smtp
    │   └── [mailbox_id]
    │
    ├── integrations
    │
    ├── team
    │
    ├── billing
    │
    ├── notifications
    │
    └── settings/
        ├── workspace
        ├── sending
        ├── notifications
        ├── security
        └── danger
```

The route hierarchy above is now the recommended frontend information architecture unless changed deliberately through this document.

---

# 117. Primary Sidebar Map

Recommended sidebar grouping:

```text
WORKSPACE

Dashboard

Campaigns

Leads

Inbox

Templates

Mailboxes
```

Secondary group:

```text
MANAGE

Integrations

Team

Billing

Settings
```

The exact visual labels and grouping may evolve during design, but product ownership should remain stable.

---

# 118. User Flow to Page Mapping

## New User

```text
/auth/signup
    ↓
/onboarding/workspace
    ↓
/onboarding/mailbox
    ↓
/onboarding/leads
    ↓
/onboarding/complete
    ↓
/app/dashboard
```

---

## Connect Mailbox

```text
/app/mailboxes
    ↓
/app/mailboxes/connect
    ↓
Provider route
    ↓
Connection flow
    ↓
/app/mailboxes/[mailbox_id]
```

---

## Import Leads

```text
/app/leads
    ↓
/app/leads/imports/new
    ↓
Import processing
    ↓
/app/leads/imports/[import_id]
```

---

## Create Campaign

```text
/app/campaigns
    ↓
/app/campaigns/new
    ↓
Basics
    ↓
Audience
    ↓
Sequence
    ↓
Senders
    ↓
Schedule
    ↓
Settings
    ↓
Review
    ↓
Start
    ↓
/app/campaigns/[campaign_id]/overview
```

---

## Review Reply

```text
/app/inbox
    ↓
/app/inbox/[conversation_id]
    ↓
Optional lead/campaign context
```

---

## Resolve Mailbox Failure

```text
Notification / Dashboard warning
    ↓
/app/mailboxes/[mailbox_id]
    ↓
Reconnect
    ↓
Healthy mailbox
```

---

## Review Campaign Failure

```text
Dashboard / Campaign list
    ↓
/app/campaigns/[campaign_id]/overview
    ↓
Warnings
    ↓
Activity / relevant configuration
```

---

# 119. MVP Navigation Acceptance Criteria

The page map is considered correctly implemented when a production user can navigate the full MVP lifecycle without needing hidden or undocumented screens.

The interface must support:

```text
Sign up
↓
Create workspace
↓
Connect mailbox
↓
Import leads
↓
Manage lists
↓
Create template
↓
Create campaign
↓
Configure recipients
↓
Configure sequence
↓
Configure senders
↓
Configure schedule
↓
Review
↓
Start
↓
Monitor campaign
↓
Review replies
↓
Review analytics
↓
Resolve mailbox problems
```

All critical product flows defined in `USER_FLOWS.md` must have an obvious destination in the page hierarchy.

---

# 120. Page Map Invariants

The following rules should remain true.

### Campaign ownership

Campaign configuration belongs under Campaigns.

### Lead ownership

Lead data, lists, imports, and suppression belong under Leads.

### Conversation ownership

Replies and conversations belong under Inbox.

### Template ownership

Reusable content belongs under Templates.

### Mailbox ownership

Sending account connection and health belong under Mailboxes.

### Analytics ownership

Cross-campaign performance belongs in the Analytics section of the Dashboard.

### Deliverability ownership

Sending-health signals belong in the Deliverability section of the Dashboard.

### Workspace administration

Team, billing, integrations, and settings remain secondary workspace-management areas.

---

# 121. What Must Not Happen

Avoid route structures such as:

```text
/app/tools/*
```

for unrelated product capabilities.

Avoid duplicating the same product feature under multiple navigation areas.

Examples of undesirable duplication:

```text
Mailbox configuration
under Mailboxes
AND Integrations
AND Settings

Suppression
under Leads
AND Campaigns
AND Settings

Campaign analytics
under Campaign
AND a separate unrelated reports page
```

One domain should have one primary navigation owner.

Cross-links are encouraged.

Duplicate ownership is not.

---

# 122. Relationship to Future Architecture

This document defines where functionality appears to users.

Architecture must later determine how it works.

For example:

```text
PAGE_MAP.md

/app/campaigns/[campaign_id]/overview
```

does not decide:

- campaign database tables
- API endpoints
- campaign state transitions
- worker behavior
- message planning
- scheduler behavior

Likewise:

```text
/app/mailboxes/[mailbox_id]
```

does not decide:

- token storage
- credential encryption
- provider adapter interface
- refresh behavior

Those decisions belong in architecture specifications.

---

# 123. Relationship to USER_ROLES.md

`USER_ROLES.md` must map permissions onto this page hierarchy.

For every page and major action, it must determine:

```text
Who can see it?
Who can create?
Who can edit?
Who can execute?
Who can delete?
Who can administer?
```

Example:

```text
/app/campaigns
    → campaign.view

Create Campaign
    → campaign.create

Start Campaign
    → campaign.start

/app/mailboxes
    → mailbox.view

Connect Mailbox
    → mailbox.manage
```

The permission names above are conceptual examples until the RBAC specification freezes them.

---

# 124. Relationship to USER_FLOWS.md

`USER_FLOWS.md` answers:

> What sequence of events happens?

`PAGE_MAP.md` answers:

> Where does the user perform and observe those events?

Example:

```text
USER_FLOWS.md

Create Campaign
→ Audience
→ Sequence
→ Senders
→ Schedule
→ Review
→ Start
```

maps to:

```text
PAGE_MAP.md

/app/campaigns/new
+
campaign configuration sections
+
/app/campaigns/[campaign_id]/overview
```

Both documents should remain aligned.

---

# 125. Gate Before Frontend Implementation

Frontend scaffolding may eventually create route shells based on this page map.

However, feature implementation should not begin until the remaining product-definition and architecture dependencies are sufficiently clear.

Before serious page implementation:

```text
MVP.md
✓

USER_FLOWS.md
✓

PAGE_MAP.md
✓

USER_ROLES.md
required
```

Then architecture:

```text
SYSTEM_ARCHITECTURE.md
DOMAIN_MODEL.md
CAMPAIGN_STATE_MACHINE.md
MESSAGE_STATE_MACHINE.md
PROVIDER_ARCHITECTURE.md
SCHEDULER.md
WORKERS.md
QUEUES.md
RATE_LIMITING.md
EVENT_SYSTEM.md
REPLY_SYNC.md
SUPPRESSION.md
DATABASE.md
```

---

# 126. Definition of Done for PAGE_MAP.md

This document is complete when the project agrees on:

```text
✓ Authentication hierarchy
✓ Onboarding hierarchy
✓ Authenticated application shell
✓ Primary navigation
✓ Workspace navigation
✓ Dashboard ownership
✓ Campaign hierarchy
✓ Campaign creation journey
✓ Campaign detail sections
✓ Lead hierarchy
✓ Lead lists
✓ CSV imports
✓ Suppression location
✓ Inbox hierarchy
✓ Templates hierarchy
✓ Mailbox hierarchy
✓ Deliverability and Analytics location (Dashboard sections)
✓ Integrations location
✓ Team location
✓ Billing location
✓ Settings hierarchy
✓ Notification location
✓ Empty/error-state expectations
✓ Cross-resource navigation
✓ Workspace-switching behavior
✓ Route security principles
✓ MVP route map
✓ User-flow-to-page mapping
```

New major product surfaces should update this document before being introduced into the frontend.

---

# 127. Final Page Map Principle

The frontend should allow users to understand the product through a small number of stable domains:

```text
Campaigns
Leads
Inbox
Templates
Mailboxes
Workspace Management
```

A user should not need to understand internal technical architecture to know where an action belongs.

The page structure must therefore reflect product responsibilities rather than backend modules, database tables, provider implementation details, or worker processes.

The final test is simple:

> A user who understands email outreach should be able to predict where a capability lives before opening the navigation.

That principle should guide every future page added to the product.