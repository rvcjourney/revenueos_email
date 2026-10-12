# User Flows — Email Outreach SaaS

## 1. Purpose

This document defines the primary end-to-end user journeys for the email outreach platform.

It describes:

- what the user is trying to accomplish
- the sequence of actions they perform
- the important decisions the platform makes
- validation and safety checks
- failure and recovery paths
- expected resulting states
- where one product domain hands work to another

This document is concerned with **product behavior and user journeys**.

It does not define:

- database schemas
- API endpoint structures
- queue topology
- Celery task implementations
- PostgreSQL locking strategies
- detailed provider adapters
- exact frontend routes
- component-level UI design
- exact RBAC permission matrices
- detailed state machine transition rules

Those responsibilities belong in their respective product and architecture documents.

The master product definition remains:

```text
docs/product/PROJECT_CONTEXT.md
```

MVP scope is defined in:

```text
docs/product/MVP.md
```

Role permissions are defined separately in:

```text
docs/product/USER_ROLES.md
```

Frontend information architecture will be defined in:

```text
docs/product/PAGE_MAP.md
```

Where this document says **authorized user**, the exact roles allowed to perform that action are determined by `USER_ROLES.md`.

---

# 2. User Flow Principles

The following rules apply across all product flows.

## 2.1 The frontend does not determine authorization

Frontend visibility may improve the user experience, but every protected action must be authorized by the backend.

Conceptually:

```text
User action
    ↓
Frontend request
    ↓
Authentication check
    ↓
Workspace membership check
    ↓
Permission check
    ↓
Business validation
    ↓
Action
```

A hidden button is not an authorization mechanism.

---

## 2.2 Workspace context is mandatory

All workspace-owned resources must operate within a clear workspace context.

Examples include:

- leads
- lists
- templates
- campaigns
- mailboxes
- conversations
- suppression entries
- analytics
- integrations
- team settings

A user must never accidentally operate on another workspace's data.

---

## 2.3 Destructive and irreversible actions require stronger UX

Actions that may create significant consequences should never occur accidentally.

Examples include:

- deleting a workspace
- disconnecting a mailbox
- removing a team member
- deleting critical campaign data
- removing suppression
- revoking credentials
- starting a campaign
- performing other security-sensitive actions

The appropriate confirmation behavior will be defined with the relevant product specification.

---

## 2.4 Sending is never triggered solely by frontend intent

Starting a campaign does not mean:

```text
Frontend
→ send all emails
```

Instead:

```text
User starts campaign
    ↓
Backend validates campaign
    ↓
Campaign enters valid executable state
    ↓
Messages are planned durably
    ↓
Scheduler identifies eligible work
    ↓
Workers perform authoritative checks
    ↓
Provider send occurs
```

---

## 2.5 Suppression overrides campaign intent

A recipient may have originally been eligible when a campaign was created but become ineligible before a future message is sent.

Therefore:

```text
Campaign says send
        +
Recipient is suppressed
        ↓
DO NOT SEND
```

Suppression must remain authoritative near actual execution time.

---

## 2.6 Reply stops applicable future outreach

When the system recognizes that a recipient replied to outreach:

```text
Reply detected
    ↓
Reply associated with recipient/conversation
    ↓
Recipient outreach status updated
    ↓
Applicable future sequence messages stopped
```

The user should not need to manually pause that recipient's sequence.

---

## 2.7 Critical flows must recover from temporary infrastructure failure

A user must not lose campaign state simply because:

- Redis restarted
- a worker crashed
- the scheduler restarted
- an application was deployed
- a provider temporarily failed

The user experience should reflect durable business state rather than transient infrastructure state.

---

# 3. Primary Actors

## 3.1 Workspace User

A member of a workspace operating the outreach product.

Exact actions depend on the user's role.

Potential responsibilities include:

- managing leads
- creating campaigns
- connecting mailboxes
- managing templates
- reviewing replies
- viewing analytics
- managing team or settings

---

## 3.2 Workspace Owner

The highest-authority workspace user.

Ownership-specific permissions are defined in `USER_ROLES.md`.

Potential ownership-sensitive actions may include:

- workspace-level administration
- billing
- ownership transfer
- workspace deletion

This document does not define which of those permissions are ultimately owner-only.

---

## 3.3 Platform Operator

An internal administrative actor responsible for legitimate platform operations, security, abuse handling, and support.

This is distinct from customer workspace roles.

The platform operator must not become part of ordinary customer workflows unless intervention is necessary.

---

## 3.4 System

The platform itself.

The system performs actions including:

- validation
- authorization
- scheduling
- message planning
- rate limiting
- worker execution
- provider communication
- event processing
- synchronization
- retries
- suppression enforcement
- analytics aggregation

---

## 3.5 External Email Provider

A connected sending/mailbox provider such as:

```text
Gmail
Microsoft
Custom SMTP
```

Provider behavior must remain behind the provider abstraction defined later in architecture documentation.

---

# 4. Flow Status Terminology

This document may use user-facing concepts such as:

```text
Draft
Scheduled
Running
Paused
Completed
Failed / Error
Archived
```

These terms describe product behavior.

The authoritative campaign state machine will be defined separately in:

```text
docs/architecture/CAMPAIGN_STATE_MACHINE.md
```

Similarly, message lifecycle terminology used here is conceptual until frozen in:

```text
docs/architecture/MESSAGE_STATE_MACHINE.md
```

---

# 5. Flow Index

The core product flows are:

```text
UF-01  Sign Up and Enter Product
UF-02  Create / Enter Workspace
UF-03  Initial Onboarding
UF-04  Connect Gmail Mailbox
UF-05  Connect Microsoft Mailbox
UF-06  Connect Custom SMTP Mailbox
UF-07  Mailbox Connection Failure / Recovery
UF-08  Create Lead Manually
UF-09  Import Leads from CSV
UF-10  Manage Lead Lists
UF-11  Manage Suppression
UF-12  Create / Manage Template
UF-13  Create Campaign
UF-14  Configure Campaign Recipients
UF-15  Build Email Sequence
UF-16  Configure Sending Mailboxes
UF-17  Configure Campaign Schedule and Limits
UF-18  Review and Start Campaign
UF-19  Pause Campaign
UF-20  Resume Campaign
UF-21  Campaign Message Planning
UF-22  Scheduled Send Execution
UF-23  Temporary Send Failure / Retry
UF-24  Permanent Send Failure
UF-25  Reply Synchronization
UF-26  Unified Inbox
UF-27  Recipient Unsubscribe
UF-28  Bounce Handling
UF-29  Complaint Handling
UF-30  Campaign Completion
UF-31  Campaign Analytics
UF-32  Mailbox Health / Operational Notification
UF-33  Team Access
UF-34  Workspace Switching
UF-35  Campaign Archive / Removal
UF-36  Platform Abuse / Safety Intervention
UF-37  Application Restart / Worker Recovery
```

---

# 6. UF-01 — Sign Up and Enter Product

## Goal

Allow a new user to create an account and enter the product securely.

## Preconditions

```text
User does not currently have an authenticated session
```

## Primary Flow

```text
User opens product
    ↓
User chooses Sign Up
    ↓
User provides required registration information
    ↓
System validates input
    ↓
Authentication identity is created
    ↓
Required verification/session behavior completes
    ↓
User becomes authenticated
    ↓
System determines whether user already belongs to a workspace
```

If no workspace exists:

```text
→ Continue to UF-02
```

If the user already has valid workspace access:

```text
→ Enter authorized workspace
```

## Failure Paths

Possible conditions:

```text
Invalid input
Existing account
Invalid/expired verification
Authentication provider failure
Session creation failure
```

The user receives an actionable error.

The product must not expose internal authentication implementation details.

## End State

```text
Authenticated user exists
```

---

# 7. UF-02 — Create or Enter Workspace

## Goal

Establish the tenant context in which the user operates.

## Primary New-Workspace Flow

```text
Authenticated user
    ↓
No existing workspace membership
    ↓
Create workspace
    ↓
Provide required workspace information
    ↓
System validates
    ↓
Workspace created
    ↓
Creator receives appropriate ownership/membership
    ↓
Workspace context becomes active
```

## Existing Membership Flow

```text
Authenticated user
    ↓
Existing workspace membership found
    ↓
System verifies active membership
    ↓
Authorized workspace loaded
```

If the user belongs to multiple workspaces, workspace selection/switching follows the product behavior defined in the page map.

## Security Requirement

The system must derive authorization from valid membership.

A workspace identifier supplied by the browser must not grant access by itself.

## End State

```text
Authenticated user
+
Authorized workspace context
```

---

# 8. UF-03 — Initial Onboarding

## Goal

Guide a new workspace toward the minimum setup required to send legitimate outreach.

## Primary Flow

```text
Workspace created
    ↓
Onboarding presented
    ↓
User completes required setup
    ↓
Connect mailbox
    ↓
Add/import leads
    ↓
Reach campaign-ready state
```

Recommended conceptual onboarding progression:

```text
1. Workspace setup
2. Connect sending mailbox
3. Add/import leads
4. Create first campaign
```

Onboarding should help users reach value quickly without weakening safety requirements.

## Skip Behavior

Where steps may legitimately be skipped, the application can permit navigation while clearly showing incomplete prerequisites.

Skipping onboarding must not bypass campaign validation.

## End State

Either:

```text
Workspace is sufficiently configured for campaign creation
```

or:

```text
Workspace remains partially configured with clear next actions
```

---

# 9. UF-04 — Connect Gmail Mailbox

## Goal

Authorize a Gmail mailbox for supported product operations.

## Primary Flow

```text
Authorized user
    ↓
Open mailbox connection experience
    ↓
Choose Gmail
    ↓
Begin OAuth authorization
    ↓
Redirect to Google authorization
    ↓
User authenticates / grants required permissions
    ↓
Provider returns authorization result
    ↓
Backend validates result
    ↓
Credentials/tokens stored securely
    ↓
Mailbox identity retrieved
    ↓
Mailbox record created/updated
    ↓
Connection marked usable
    ↓
User sees connected mailbox
```

## Important Requirements

The application must:

- use the supported OAuth model
- protect tokens and credentials
- associate the mailbox with the correct workspace
- detect invalid connection outcomes
- avoid exposing sensitive provider credentials to the frontend

## Controlled Test Milestone

The early engineering milestone is:

```text
Connected Gmail
    ↓
Eligible test recipient
    ↓
Suppression check
    ↓
Controlled test send
    ↓
Provider result
    ↓
Result persisted
```

## Failure Paths

Examples:

```text
User cancels OAuth
Permission denied
Invalid callback/state
Provider unavailable
Token exchange fails
Required permissions missing
Mailbox already connected incompatibly
```

No partially connected mailbox should be represented as healthy.

---

# 10. UF-05 — Connect Microsoft Mailbox

## Goal

Authorize a supported Microsoft mailbox.

## Primary Flow

```text
Authorized user
    ↓
Choose Microsoft
    ↓
Microsoft OAuth flow
    ↓
User grants required authorization
    ↓
Backend validates authorization
    ↓
Credentials stored securely
    ↓
Mailbox information obtained
    ↓
Mailbox becomes connected
```

The campaign product should interact with the mailbox through the common provider/domain model rather than requiring Microsoft-specific campaign behavior.

## Failure Paths

Equivalent categories to Gmail apply:

- cancelled authorization
- insufficient scopes
- invalid callback
- expired/revoked credentials
- provider errors

---

# 11. UF-06 — Connect Custom SMTP Mailbox

## Goal

Allow a user to connect a compatible SMTP account when OAuth-based providers are not applicable.

## Primary Flow

```text
Authorized user
    ↓
Choose Custom SMTP
    ↓
Provide required SMTP configuration
    ↓
Backend validates configuration
    ↓
Test connection safely
    ↓
Connection succeeds
    ↓
Credentials stored securely
    ↓
Mailbox becomes available
```

## Failure Path

```text
Connection test fails
    ↓
Mailbox is not marked healthy
    ↓
User receives actionable configuration error
    ↓
User corrects configuration
    ↓
Retry
```

## Safety Rule

Custom SMTP is a compatibility provider.

It does not create:

- direct-to-MX infrastructure
- arbitrary IP rotation
- provider-limit bypassing behavior

---

# 12. UF-07 — Mailbox Connection Failure and Recovery

## Goal

Prevent campaigns from silently continuing through an invalid mailbox.

## Trigger Examples

```text
OAuth revoked
Credentials expired and cannot refresh
Provider disables access
SMTP credentials changed
Persistent authentication failure
Provider account unavailable
```

## Flow

```text
System detects mailbox problem
    ↓
Mailbox health/state updated
    ↓
Affected sends are prevented or safely deferred
    ↓
Failure becomes visible
    ↓
Authorized user is notified where appropriate
    ↓
User reconnects/fixes mailbox
    ↓
Backend validates connection
    ↓
Mailbox returns to eligible state
    ↓
Eligible campaign execution may continue
```

## Critical Rule

A mailbox must not remain marked healthy indefinitely after the platform knows its credentials are invalid.

---

# 13. UF-08 — Create Lead Manually

## Goal

Add an individual recipient to the workspace.

## Primary Flow

```text
Authorized user
    ↓
Choose Add Lead
    ↓
Enter required lead/contact information
    ↓
System validates fields
    ↓
Check relevant duplicate/business constraints
    ↓
Lead created
    ↓
Optional list association
    ↓
Lead available for authorized workflows
```

## Important Distinction

Creating a lead does not guarantee sending eligibility.

Before sending, other conditions may override it:

```text
suppression
invalid address state
campaign eligibility
mailbox state
other sending rules
```

---

# 14. UF-09 — Import Leads from CSV

## Goal

Import a collection of leads safely and transparently.

## Primary Flow

```text
Authorized user
    ↓
Choose Import
    ↓
Upload CSV
    ↓
System inspects file
    ↓
User maps fields where required
    ↓
System validates import configuration
    ↓
User confirms import
    ↓
Import job created
    ↓
Asynchronous processing begins
    ↓
Rows validated
    ↓
Duplicates handled according to product rules
    ↓
Valid leads persisted
    ↓
Invalid rows recorded
    ↓
Import completes
    ↓
Result summary shown
```

## Result Summary

The user should be able to understand at least:

```text
Total rows processed
Successfully imported
Invalid/rejected rows
Relevant duplicates
Other processing failures
```

## Retry Behavior

Retrying an interrupted import must not create uncontrolled duplicate leads.

## Large File Behavior

Large imports should not require keeping a browser HTTP request open until all rows finish processing.

---

# 15. UF-10 — Manage Lead Lists

## Goal

Organize recipients for reuse in campaigns.

## Create List

```text
Authorized user
    ↓
Create list
    ↓
Provide list details
    ↓
List created
```

## Add Leads

```text
Select leads
    ↓
Add to list
    ↓
Membership updated
```

## Campaign Use

```text
Campaign
    ↓
Select list
    ↓
System determines campaign recipients
    ↓
Individual sending eligibility still applies
```

## Critical Rule

List membership does not override suppression.

---

# 16. UF-11 — Manage Suppression

## Goal

Allow the platform and authorized users to prevent prohibited or inappropriate future outreach.

## Manual Suppression Flow

```text
Authorized user
    ↓
Choose recipient/address
    ↓
Add suppression
    ↓
System validates
    ↓
Suppression persisted
    ↓
Pending/future eligible outreach becomes blocked
```

## Automatic Suppression Sources

Suppression may also arise from system-recognized events such as:

```text
unsubscribe
permanent bounce
complaint
```

according to the final suppression specification.

## Removal Flow

If suppression removal is supported:

```text
Authorized user requests removal
    ↓
Permission and business rules checked
    ↓
User receives appropriate warning/confirmation
    ↓
Suppression removed only if permitted
```

Removal semantics must be designed carefully.

Not all suppression causes should necessarily be reversible by ordinary users.

## Critical Rule

Suppression must be checked again near actual send execution.

---

# 17. UF-12 — Create and Manage Template

## Goal

Create reusable email content for campaigns.

## Primary Flow

```text
Authorized user
    ↓
Open templates
    ↓
Choose Create Template
    ↓
Create supported template content
    ↓
Provide subject/content as required
    ↓
Use supported variables/personalization fields
    ↓
Validate
    ↓
Preview
    ↓
Save
    ↓
Template becomes available for campaign sequence use
```

## Editing Flow

```text
Open existing template
    ↓
Edit
    ↓
Validate
    ↓
Save
```

Template update behavior for already-running campaigns must later be defined clearly.

The system must not accidentally mutate already-planned campaign content if the intended product behavior requires campaign snapshots.

## Personalized / Advanced Template Path

If advanced personalized-template functionality defined in the master product vision is enabled in a later release, it should enter from the template creation experience rather than requiring a separate campaign type.

Its detailed flow belongs in the later personalization specification and must not block the core MVP template path.

---

# 18. UF-13 — Create Campaign

## Goal

Create a draft outreach campaign.

## Primary Flow

```text
Authorized user
    ↓
Choose Create Campaign
    ↓
System creates or begins campaign draft
    ↓
User provides campaign identity/details
    ↓
Campaign remains non-sending draft
    ↓
User continues campaign configuration
```

A campaign should not send merely because a campaign object exists.

## Campaign Configuration Stages

The conceptual journey is:

```text
Campaign details
    ↓
Recipients
    ↓
Sequence
    ↓
Sending mailboxes
    ↓
Schedule / limits
    ↓
Settings
    ↓
Review
    ↓
Start
```

The exact page/tab/stepper design belongs in `PAGE_MAP.md` and later UX specifications.

---

# 19. UF-14 — Configure Campaign Recipients

## Goal

Determine who the campaign intends to contact.

## Primary Flow

```text
Campaign draft
    ↓
Choose recipient source
    ↓
Select lead list / supported recipient collection
    ↓
System resolves candidate recipients
    ↓
User sees recipient summary
    ↓
Campaign stores intended audience
```

## Eligibility Distinction

The campaign audience represents **intent**.

Final send eligibility remains dynamic.

A recipient may later become ineligible because of:

```text
suppression
reply
unsubscribe
bounce
campaign state
other business conditions
```

---

# 20. UF-15 — Build Email Sequence

## Goal

Define the ordered email outreach sequence.

## MVP Step Types

```text
Email
Wait
```

## Example Flow

```text
Add Email Step
    ↓
Select/create email content
    ↓
Add Wait Step
    ↓
Set interval
    ↓
Add next Email Step
    ↓
Repeat as needed
```

Example:

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

Before campaign activation, the system should detect invalid sequence configurations such as:

- missing required email content
- invalid wait interval
- structurally incomplete sequence
- other invalid campaign-specific configuration

## Non-MVP

The MVP does not require:

- arbitrary branching
- workflow graph construction
- LinkedIn actions
- SMS steps
- complex if/else trees

---

# 21. UF-16 — Configure Sending Mailboxes

## Goal

Choose which connected mailbox or mailboxes may send campaign messages.

## Flow

```text
Campaign draft
    ↓
Open mailbox selection
    ↓
System shows eligible workspace mailboxes
    ↓
User selects mailbox(es)
    ↓
System validates selection
    ↓
Campaign configuration updated
```

## Invalid Mailbox

If a selected mailbox becomes invalid before campaign start:

```text
Preflight validation fails
    ↓
Campaign cannot start using invalid configuration
```

If it fails during campaign execution:

```text
Mailbox marked unhealthy/ineligible
    ↓
Future work involving mailbox safely stopped/deferred
    ↓
User informed appropriately
```

---

# 22. UF-17 — Configure Campaign Schedule and Limits

## Goal

Define when outreach is permitted to execute.

## User Configuration

The MVP should support concepts such as:

```text
Sending days
Sending hours
Timezone
Basic daily sending limits
```

## Flow

```text
Campaign draft
    ↓
Choose timezone
    ↓
Choose allowed sending days
    ↓
Choose allowed sending hours
    ↓
Configure supported limits/settings
    ↓
System validates
    ↓
Configuration saved
```

## Validation Examples

Invalid configuration may include:

- no usable sending window
- invalid time range
- missing timezone
- unsupported limit configuration
- other contradictory settings

The exact validation rules belong in the campaign/scheduler specifications.

---

# 23. UF-18 — Review and Start Campaign

## Goal

Convert a valid campaign draft into executable outreach.

This is one of the most important user flows in the product.

## Review Flow

```text
User opens Review
    ↓
System evaluates campaign configuration
```

Review should summarize relevant information such as:

```text
Campaign identity
Recipient scope
Sequence
Selected mailbox(es)
Schedule
Limits/settings
Warnings/errors
```

## Preflight Validation

Before start, backend validation must evaluate applicable conditions.

Conceptually:

```text
Campaign exists
        ↓
User is authorized
        ↓
Campaign is in startable state
        ↓
Recipients configured
        ↓
Sequence valid
        ↓
Mailbox configuration valid
        ↓
Schedule valid
        ↓
Required settings valid
        ↓
No release-blocking errors
```

## Start Flow

```text
User chooses Start
    ↓
Explicit start action
    ↓
Backend repeats authoritative validation
    ↓
Validation passes
    ↓
Campaign transitions to appropriate executable state
    ↓
Durable campaign/message planning begins
    ↓
UI reflects new campaign state
```

## Failure Flow

```text
Validation fails
    ↓
Campaign remains non-executable
    ↓
User sees actionable issue
    ↓
User corrects configuration
    ↓
Review again
```

## Critical Rule

Frontend review validation must not substitute for backend preflight validation.

---

# 24. UF-19 — Pause Campaign

## Goal

Allow an authorized user to stop future campaign execution without destroying campaign state.

## Flow

```text
Running campaign
    ↓
Authorized user selects Pause
    ↓
Backend validates transition
    ↓
Campaign becomes paused
    ↓
Future unsent execution stops according to state rules
    ↓
Already completed sends remain recorded
```

## Implemented behaviour (ADR-0020)

Pause is idempotent: pausing an already paused campaign returns it unchanged. The response reports how many emails were already past the send gate (queued, being sent or of unknown outcome) because a pause cannot recall them. While paused a manager can change the schedule, edit the wording of emails that have not been sent, add or restructure steps after the last one any lead reached (standard campaigns), and add leads. A personalized campaign is edited the same way (wording of any step, waits, steps after the last one any lead reached); emails already written keep their version and only emails written after you resume use the new wording. Its setup (objective, company, style) and attachments stay locked. If adding leads fails, a **Retry** button continues where it stopped, and resume stays blocked until it succeeds.

## Concurrency Consideration

A pause request may occur while a worker is processing a message.

The architecture must define authoritative behavior for messages already in a sending-critical section.

The product promise must be deterministic rather than dependent on timing accidents.

---

# 25. UF-20 — Resume Campaign

## Goal

Continue an eligible paused campaign.

## Implemented behaviour (ADR-0020)

Resume is idempotent. It revalidates mailboxes (at least one able to send), the sending schedule, unsubscribe configuration and any unfinished lead additions, and answers with the list of problems if it cannot proceed. Waits keep elapsing while paused; on resume every unsent message is re-validated and re-timed by a reconcile pass, and work that became overdue is spread over the sending windows by the existing pacing instead of going out together. Nothing is sent earlier than its wait. Edits made while paused are applied to unsent emails by the same pass. Leads that already finished the sequence stay finished unless the user ticks **also send the new emails to leads who already finished the sequence** in the resume dialog (standard campaigns; only leads who have not replied, unsubscribed or bounced).

## Flow

```text
Paused campaign
    ↓
Authorized user chooses Resume
    ↓
Backend validates campaign
    ↓
Mailbox / schedule / configuration revalidated as required
    ↓
Transition succeeds
    ↓
Eligible pending campaign work resumes
```

## Failure Example

```text
Paused campaign
    ↓
Mailbox disconnected during pause
    ↓
User selects Resume
    ↓
Validation fails
    ↓
User must reconnect/configure mailbox
```

Resuming must not bypass newly created suppressions or replies.

---

# 26. UF-21 — Campaign Message Planning

## Goal

Convert campaign intent into durable future message work.

This is system-driven but directly affects the user-visible campaign lifecycle.

## Conceptual Flow

```text
Campaign becomes executable
    ↓
System resolves campaign recipients
    ↓
Sequence interpreted
    ↓
Future message intentions calculated
    ↓
Scheduled timestamps determined
    ↓
Durable message records created
```

Conceptually, durable messages may contain information such as:

```text
campaign
recipient/lead
mailbox
sequence step
status
scheduled time
```

Exact schema is deferred to database design.

## Critical Rule

Future messages cannot exist only as long-lived queue entries.

Their authoritative future intent must remain recoverable from PostgreSQL.

---

# 27. UF-22 — Scheduled Send Execution

## Goal

Send one eligible campaign message safely.

## Conceptual Flow

```text
Durable message is due
    ↓
Scheduler identifies due work
    ↓
Work safely claimed / queued
    ↓
Worker receives task
    ↓
Worker loads current authoritative state
    ↓
Re-check campaign eligibility
    ↓
Re-check message eligibility
    ↓
Re-check recipient/lead state
    ↓
Re-check suppression
    ↓
Re-check mailbox state
    ↓
Re-check sending window / limits
    ↓
Determine provider
    ↓
Attempt send
    ↓
Persist provider result
    ↓
Update message/event state
    ↓
Plan/enable next applicable sequence action
```

## Abort Conditions

The worker must not send if current state says the message is no longer eligible.

Examples:

```text
Campaign paused
Recipient suppressed
Recipient replied
Mailbox invalid
Message already sent
Message cancelled
Outside permitted execution condition
Relevant limit exhausted
```

## Duplicate Task

```text
Same work delivered again
    ↓
Worker loads authoritative state
    ↓
Already completed / ineligible effect detected
    ↓
No duplicate business send
```

Idempotency requirements are mandatory.

---

# 28. UF-23 — Temporary Send Failure and Retry

## Goal

Recover safely from failures that may succeed later.

## Flow

```text
Worker attempts send
    ↓
Provider returns temporary/retryable failure
    ↓
Failure classified
    ↓
Attempt recorded
    ↓
Retry policy evaluated
    ↓
Future retry scheduled
    ↓
Backoff applied
    ↓
Message retried when eligible
```

## Possible Outcomes

```text
Retry succeeds
    ↓
Message becomes successfully sent
```

or:

```text
Retry budget/policy exhausted
    ↓
Message reaches terminal failure state
```

## Requirements

Retry behavior must:

- avoid immediate uncontrolled retry loops
- use appropriate backoff
- remain idempotent
- respect campaign state
- respect new suppression
- respect new replies
- respect mailbox health
- respect current limits

A retry is a new execution attempt, not permission to ignore current state.

---

# 29. UF-24 — Permanent Send Failure

## Goal

Handle failures that should not continue retrying.

## Flow

```text
Send attempt
    ↓
Provider returns permanent failure
    ↓
Failure classified
    ↓
Failure persisted
    ↓
Message enters terminal failure behavior
    ↓
Relevant recipient/mailbox state updated if required
    ↓
User-facing campaign outcome updated
```

Examples may include conditions such as:

- permanently invalid recipient
- unsupported recipient/provider error
- permanent authentication condition
- other non-retryable provider failures

Exact provider classification belongs in provider architecture.

---

# 30. UF-25 — Reply Synchronization

## Goal

Recognize recipient replies and stop inappropriate future outreach.

## Flow

```text
Outbound campaign email sent
    ↓
Recipient replies
    ↓
Mailbox synchronization receives/discovers reply
    ↓
System identifies provider message
    ↓
Reply normalized
    ↓
Reply matched to outbound context
    ↓
Lead/contact identified
    ↓
Conversation created or updated
    ↓
Reply recorded idempotently
    ↓
Recipient marked as replied for applicable campaign logic
    ↓
Applicable future sequence messages stopped
    ↓
Inbox updated
    ↓
Analytics/event data updated
```

## Duplicate Sync

The same provider reply may be observed multiple times.

Therefore:

```text
Already processed provider reply
    ↓
No duplicate reply record
    ↓
No duplicate conversation
    ↓
No repeated side effects
```

## Matching Failure

If a reply cannot be confidently matched:

```text
Reply received
    ↓
Matching fails
    ↓
System must not silently fabricate association
    ↓
Appropriate unresolved/error handling
```

Detailed matching rules belong in `REPLY_SYNC.md`.

---

# 31. UF-26 — Unified Inbox

## Goal

Allow users to review outreach conversations without manually checking infrastructure or each provider independently.

## Primary Flow

```text
Authorized user
    ↓
Open Inbox
    ↓
System loads workspace conversations
    ↓
User sees conversation list
    ↓
User opens conversation
    ↓
Relevant outbound/inbound history displayed
```

## MVP Boundary

The unified inbox should provide operational visibility into replies.

It does not need to become a complete replacement for Gmail or Outlook in the MVP.

Potential later functionality may include:

- richer categorization
- automation
- advanced assignment
- AI classification
- complex inbox workflows

unless required elsewhere by the master product vision.

---

# 32. UF-27 — Recipient Unsubscribe

## Goal

Honor an unsubscribe and prevent applicable future outreach.

## Flow

```text
Recipient invokes supported unsubscribe mechanism
    ↓
System receives unsubscribe request/event
    ↓
Request validated
    ↓
Recipient identity resolved
    ↓
Unsubscribe recorded durably
    ↓
Suppression created/updated
    ↓
Applicable future messages become ineligible
    ↓
Campaign analytics/events updated
```

## Critical Rule

The system must not implement unsubscribe merely as:

```text
remove recipient from current campaign
```

The suppression effect must persist according to the product's suppression semantics.

## Race Condition

If an unsubscribe occurs near scheduled send time:

```text
Worker must perform current suppression check near send
```

This requirement reduces the chance that previously planned work bypasses a newly created suppression.

---

# 33. UF-28 — Bounce Handling

## Goal

Respond appropriately when an email cannot be delivered.

## Flow

```text
Email sent/attempted
    ↓
Provider/event source reports bounce
    ↓
System validates/deduplicates event
    ↓
Bounce classified where possible
```

Temporary path:

```text
Temporary bounce
    ↓
Record event
    ↓
Apply appropriate retry/recipient behavior
```

Permanent path:

```text
Permanent bounce
    ↓
Record event
    ↓
Recipient becomes ineligible according to suppression rules
    ↓
Applicable future outreach stopped
```

## Critical Rule

A recipient known to be permanently undeliverable should not be repeatedly contacted in later campaigns merely because they appear in another lead list.

---

# 34. UF-29 — Complaint Handling

## Goal

Respond to supported recipient complaint signals.

## Flow

```text
Provider/event source reports complaint
    ↓
System validates and deduplicates event
    ↓
Complaint persisted
    ↓
Recipient safety state updated
    ↓
Applicable suppression created
    ↓
Future prohibited sending prevented
    ↓
Relevant analytics/operational visibility updated
```

Complaints are safety events, not merely analytics.

---

# 35. UF-30 — Campaign Completion

## Goal

Conclude a campaign once there is no further executable campaign work.

## Conceptual Flow

```text
Campaign running
    ↓
Recipients progress through sequence
    ↓
Some finish sequence
Some reply
Some unsubscribe
Some become suppressed
Some fail
    ↓
No remaining executable campaign work
    ↓
System determines campaign completion
    ↓
Campaign enters completed state
    ↓
Final outcome visible to user
```

Exact completion criteria belong in campaign architecture/state-machine design.

## Important Question for State Machine

Later specifications must decide whether a completed campaign can ever:

```text
resume
reopen
receive newly added recipients
```

This document does not decide those transitions.

---

# 36. UF-31 — View Campaign Analytics

## Goal

Allow an operator to understand campaign performance.

## Flow

```text
Authorized user
    ↓
Open campaign analytics
    ↓
System loads workspace-scoped campaign outcomes
    ↓
User reviews relevant metrics
```

MVP metrics should prioritize meaningful outcomes such as:

```text
Planned / attempted
Sent
Failures
Bounces
Replies
Unsubscribes
Complaints where supported
```

Other metrics may be added according to the final product context.

## Open Tracking Principle

If open tracking is supported:

```text
Open = estimated engagement signal
```

It must not be presented as unquestionable proof that a human meaningfully read an email.

Replies remain a more meaningful outreach outcome.

## Idempotency

Duplicate provider events must not knowingly inflate analytics.

---

# 37. UF-32 — Mailbox Health and Operational Notification

## Goal

Make important operational problems visible before they silently damage campaign execution.

## Flow

```text
System detects significant condition
    ↓
Condition persisted / reflected in health state
    ↓
Relevant user-facing notification created
    ↓
User opens affected resource
    ↓
Actionable explanation provided
    ↓
User resolves condition
```

Example conditions may include:

```text
Mailbox disconnected
Credentials invalid
Import failed
Campaign execution blocked
Significant provider problem
```

Exact notification channels are not defined here.

## Critical Rule

Important recoverable failures must not live only in server logs.

---

# 38. UF-33 — Team Access

## Goal

Allow authorized users to collaborate within one workspace.

Detailed permissions depend entirely on `USER_ROLES.md`.

## Invitation Flow

Conceptually:

```text
Authorized user
    ↓
Invite team member
    ↓
Specify identity / permitted role
    ↓
Backend validates inviter permission
    ↓
Invitation created/sent
    ↓
Invitee accepts
    ↓
Membership established
    ↓
Role permissions apply
```

## Role Change

```text
Authorized administrator
    ↓
Select member
    ↓
Change role
    ↓
Backend verifies authority
    ↓
Protected ownership/security rules evaluated
    ↓
Membership role updated
```

## Removal

```text
Authorized administrator
    ↓
Remove member
    ↓
Backend validates action
    ↓
Member loses workspace access
```

## Safety Requirements

The final permission model must prevent invalid outcomes such as:

- unauthorized privilege escalation
- protected ownership changes by lower roles
- removing/changing protected members without authority
- workspace access continuing after membership removal

---

# 39. UF-34 — Switch Workspace

## Goal

Allow a user belonging to more than one workspace to change active context safely.

## Flow

```text
Authenticated user
    ↓
Open workspace selector
    ↓
System shows only authorized memberships
    ↓
User selects workspace
    ↓
Membership revalidated
    ↓
Active workspace context changes
    ↓
Workspace-scoped product data reloads
```

## Critical Rule

Switching context must not result in stale data or mutations from the previous workspace being applied to the newly selected workspace.

---

# 40. UF-35 — Archive or Remove Campaign

## Goal

Allow users to cleanly manage campaigns no longer actively used.

## Archive Flow

```text
Eligible campaign
    ↓
Authorized user chooses Archive
    ↓
Backend validates transition
    ↓
Campaign archived
    ↓
Campaign removed from ordinary active views
    ↓
Historical data retained as required
```

## Delete Flow

If hard deletion is supported for certain campaign states:

```text
Authorized user requests Delete
    ↓
Backend evaluates state / dependencies / permission
    ↓
Explicit confirmation
    ↓
Deletion occurs only if permitted
```

Running or historically significant campaigns should not be destructively removed without carefully defined rules.

Exact deletion semantics belong in campaign and data-retention specifications.

---

# 41. UF-36 — Platform Abuse or Safety Intervention

## Goal

Allow legitimate platform operators to prevent serious misuse or unsafe behavior.

## Trigger

Examples may include:

```text
confirmed abuse
security incident
dangerous malfunction
provider compliance problem
workspace sending requiring restriction
```

## Conceptual Flow

```text
Safety condition detected/reported
    ↓
Platform operator reviews
    ↓
Authorized intervention decision
    ↓
Appropriate workspace/sending capability restricted
    ↓
Intervention auditable
    ↓
Affected product behavior becomes safe
```

The platform must not depend on untracked ad hoc production database edits as the normal intervention mechanism.

Detailed internal admin workflows belong in a later operational/admin specification.

---

# 42. UF-37 — Infrastructure Restart and Campaign Recovery

## Goal

Ensure user campaigns survive temporary infrastructure restarts.

This is a system flow but is essential to the user experience.

## Redis Restart

```text
Redis unavailable/restarts
    ↓
Transient queue/coordination state may be affected
    ↓
Durable campaign/message state remains in PostgreSQL
    ↓
Redis returns
    ↓
Scheduler/workers resume
    ↓
System rediscovers eligible due work
    ↓
Campaign continues safely
```

## Worker Crash

```text
Worker claims/receives task
    ↓
Worker crashes
    ↓
Authoritative message state remains durable
    ↓
System recovery/retry rules determine next action
    ↓
Message not silently lost
```

## Scheduler Restart

```text
Scheduler stops
    ↓
Future messages remain durable
    ↓
Scheduler restarts
    ↓
Due work discovered
    ↓
Eligible work resumes
```

## Application Deployment

```text
Deployment/restart
    ↓
Durable campaign state preserved
    ↓
Services return
    ↓
Processing resumes according to current authoritative state
```

## Critical Rule

The product must not require reconstructing campaigns manually after normal service restarts.

---

# 43. Complete First-Time User Journey

The complete first-time user experience can now be represented as:

```text
Visitor
    ↓
Sign Up
    ↓
Authenticated User
    ↓
Create Workspace
    ↓
Initial Onboarding
    ↓
Connect Gmail / Microsoft / SMTP
    ↓
Mailbox Validated
    ↓
Import Leads
    ↓
Organize Leads
    ↓
Create Template
    ↓
Create Campaign
    ↓
Select Recipients
    ↓
Build Sequence
    ↓
Choose Mailboxes
    ↓
Configure Schedule
    ↓
Review Campaign
    ↓
Start Campaign
    ↓
Messages Planned Durably
    ↓
Scheduler Discovers Work
    ↓
Workers Validate Current Eligibility
    ↓
Provider Sends
    ↓
Results Persisted
    ↓
Replies / Bounces / Unsubscribes / Complaints Processed
    ↓
Future Sequence Adjusted Automatically
    ↓
Inbox + Analytics Updated
    ↓
Campaign Completes
```

---

# 44. Complete Campaign Execution Journey

```text
DRAFT CAMPAIGN
      │
      ├── recipients
      ├── sequence
      ├── mailbox
      ├── schedule
      └── settings
      │
      ▼
REVIEW
      │
      ├── invalid → return to configuration
      │
      └── valid
              │
              ▼
            START
              │
              ▼
     DURABLE MESSAGE PLANNING
              │
              ▼
         SCHEDULED WORK
              │
              ▼
          DUE MESSAGE
              │
              ▼
      AUTHORITATIVE CHECKS
              │
     ┌────────┴─────────┐
     │                  │
 Ineligible          Eligible
     │                  │
 Skip/stop              ▼
                   RATE LIMIT
                       │
                ┌──────┴──────┐
                │             │
              Wait          Allowed
                              │
                              ▼
                       PROVIDER SEND
                              │
                ┌─────────────┼─────────────┐
                │             │             │
              Sent       Temporary       Permanent
                           Failure         Failure
                │             │             │
                ▼             ▼             ▼
             Persist       Retry         Terminal
                │
                ▼
        Await recipient events
                │
     ┌──────────┼───────────┬───────────┐
     │          │           │           │
   Reply      Bounce    Unsubscribe   Complaint
     │          │           │           │
     └──────────┴───────────┴───────────┘
                │
                ▼
      UPDATE AUTHORITATIVE STATE
                │
                ▼
     STOP/CONTINUE SEQUENCE AS
             APPROPRIATE
```

---

# 45. Campaign Recipient Journey

A recipient's campaign journey is distinct from the campaign's global state.

Conceptually:

```text
Recipient selected
    ↓
Eligibility established
    ↓
First sequence message planned
    ↓
Message sent
    ↓
Wait
    ↓
Check stopping conditions
```

Possible branch:

```text
No reply / no suppression
    ↓
Next sequence message
```

Reply branch:

```text
Reply
    ↓
Stop applicable sequence
```

Unsubscribe branch:

```text
Unsubscribe
    ↓
Suppress
    ↓
Stop future outreach
```

Permanent bounce branch:

```text
Permanent bounce
    ↓
Apply suppression rule
    ↓
Stop future outreach
```

Complaint branch:

```text
Complaint
    ↓
Apply safety/suppression rule
    ↓
Stop future outreach
```

Campaign sequence execution therefore cannot be modeled only at the campaign level.

Recipient-level progress is essential.

---

# 46. Pre-Send Decision Flow

Every production send should conceptually pass through an authoritative eligibility decision.

```text
Message becomes due
    ↓
Does message still require sending?
    ├── No → Stop
    └── Yes
          ↓
Is campaign allowed to execute?
    ├── No → Stop/defer according to state
    └── Yes
          ↓
Is recipient still eligible?
    ├── No → Stop
    └── Yes
          ↓
Is recipient suppressed?
    ├── Yes → Stop
    └── No
          ↓
Has applicable reply already occurred?
    ├── Yes → Stop
    └── No
          ↓
Is mailbox healthy/eligible?
    ├── No → Stop/defer
    └── Yes
          ↓
Is current time allowed?
    ├── No → Reschedule/defer
    └── Yes
          ↓
Is applicable rate capacity available?
    ├── No → Delay
    └── Yes
          ↓
Attempt provider send
```

The exact order and transactional mechanics will be designed in the sending architecture.

This flow defines the product-level expectation.

---

# 47. Sequence Stopping Conditions

Future sequence activity must stop or become ineligible when applicable conditions occur.

At minimum these include:

```text
Recipient replied
Recipient unsubscribed
Recipient permanently suppressed
Recipient generated applicable permanent bounce
Recipient generated supported complaint
Campaign paused
Campaign otherwise stopped/completed/archived
Mailbox becomes unusable for execution
```

Some conditions are recipient-specific.

Others affect the entire campaign.

The architecture must preserve that distinction.

---

# 48. Failure UX Principles

Failures should be categorized according to whether the user can act on them.

## User-correctable

Examples:

```text
Invalid campaign configuration
Disconnected mailbox
Invalid SMTP credentials
Invalid CSV format
Missing required campaign data
```

Expected UX:

```text
Explain problem
+
Show affected resource
+
Provide corrective action
```

---

## Automatically Recoverable

Examples:

```text
Transient provider error
Temporary worker failure
Temporary queue problem
```

Expected UX:

```text
System retries safely
```

The user should not need to manually repair every transient infrastructure failure.

---

## Terminal

Examples:

```text
Permanent recipient failure
Non-retryable provider response
Exhausted retry policy
```

Expected UX:

```text
Persist failure
+
Stop inappropriate retry
+
Make result visible
```

---

# 49. Empty States

Core user journeys should provide meaningful empty states.

Examples:

## No campaigns

```text
No campaigns yet
→ Create first campaign
```

## No leads

```text
No leads yet
→ Add lead
→ Import CSV
```

## No mailbox

```text
No sending mailbox connected
→ Connect Gmail
→ Connect Microsoft
→ Connect SMTP
```

## No templates

```text
No templates yet
→ Create template
```

## No conversations

```text
No replies/conversations yet
```

Empty states should guide the next legitimate product action rather than appear as application errors.

---

# 50. Loading and Long-Running Operation Behavior

Operations such as imports and asynchronous campaign processing must not create misleading UI.

The product should distinguish concepts such as:

```text
Not started
Processing
Completed
Completed with errors
Failed
```

where applicable.

The user should not need to keep a browser tab open for durable background work to continue.

Refreshing the page should load the persisted current state.

---

# 51. Idempotency Expectations by Flow

The following actions require particular duplicate-processing protection.

| Flow | Duplicate risk |
|---|---|
| CSV import | duplicate lead creation |
| Campaign start | duplicate planning |
| Message scheduling | duplicate scheduled messages |
| Send worker | duplicate email send |
| Provider webhook/event | duplicate event |
| Reply sync | duplicate reply/conversation |
| Unsubscribe | duplicate suppression/event |
| Bounce | duplicate bounce/event |
| Complaint | duplicate complaint/event |
| Retry | duplicate external effect |

Idempotency is not merely a worker implementation concern.

It is part of the product's expected behavior.

---

# 52. Authorization Expectations by Flow

All mutating user flows must evaluate:

```text
Authenticated?
    ↓
Workspace member?
    ↓
Correct workspace?
    ↓
Role permitted?
    ↓
Resource belongs to workspace?
    ↓
Business state permits operation?
```

The exact permission for each action must come from `USER_ROLES.md`.

---

# 53. Cross-Workspace Security Flow

A request attempting to access another tenant's resource should behave conceptually as:

```text
User authenticated in Workspace A
    ↓
Requests resource belonging to Workspace B
    ↓
Backend determines no authorized relationship
    ↓
Request rejected
    ↓
No Workspace B data exposed
```

The frontend must never be relied upon to enforce this boundary.

---

# 54. Data Refresh Behavior

Because campaign execution happens asynchronously, UI state can change without a direct user action.

Examples:

```text
campaign progresses
message sends
reply arrives
mailbox disconnects
import completes
bounce arrives
```

Frontend experiences must therefore obtain current server-authoritative state rather than assuming the state that existed when a screen first loaded remains correct.

The exact real-time/polling implementation is an architecture decision.

---

# 55. User Flow Boundaries for MVP

The MVP flows intentionally stop before advanced systems such as:

```text
complex campaign branching
LinkedIn outreach
SMS outreach
advanced workflow automation
fully autonomous AI campaign generation
advanced automated experimentation
complex deliverability optimization
large-scale automation builders
```

Those features may later extend the existing journeys.

They must not weaken the foundational flow model.

---

# 56. Future Personalized Content Extension

If personalized/AI-assisted templates are activated according to the broader product roadmap, the flow should extend the existing template/campaign journey rather than create a parallel campaign architecture.

Conceptually:

```text
Create Template
    ↓
Choose content creation mode
    ├── Standard / Custom
    └── Personalized / AI-assisted
              ↓
        Configure personalization
              ↓
        Generate/preview content
              ↓
        Validate
              ↓
        Save/use in sequence
```

The names of those options and their precise behavior should be frozen in the relevant product specification before implementation.

The campaign engine should ultimately consume validated content without needing an entirely separate "AI campaign" state machine.

**Superseded in part (2026-09-30):** personalization was built as a campaign-level type ([ADR-0011](../adr/0011-hyper-personalized-campaign-type.md)), not as a template-level mode, and its authoring flow is defined by [ADR-0016](../adr/0016-api-process-ai-drafting-and-company-analysis.md): Company (website or typed business info) → Text or HTML email → Objective → AI-drafted reference emails → sample previews → manager approval → activation. The template-level sketch above is not implemented.

---

# 57. User Flow Requirements Before Architecture

These flows establish several requirements that later architecture documents must solve explicitly.

## Campaign Architecture Must Define

- valid campaign states
- valid transitions
- start behavior
- pause semantics
- resume semantics
- completion semantics
- archive/delete semantics

## Message Architecture Must Define

- message states
- claiming
- sending
- retries
- terminal failure
- cancellation/stopping
- event relationship

## Scheduler Must Define

- due-work discovery
- timezone behavior
- sending windows
- recovery
- duplicate prevention

## Worker Architecture Must Define

- task ownership
- business-logic reuse
- idempotency
- retry boundaries
- concurrency

## Provider Architecture Must Define

- provider interface
- credential lifecycle
- sending
- error classification
- connection health
- reply/event capabilities

## Rate Limiting Must Define

- applicable scopes
- counters
- concurrency
- deferral behavior
- recovery

## Reply Sync Must Define

- synchronization model
- provider identifiers
- deduplication
- matching
- conversation association
- stopping behavior

## Suppression Must Define

- suppression scope
- source/reason
- creation
- removal rules
- enforcement timing

## Database Design Must Support

- workspaces
- membership
- leads
- lists
- suppression
- mailboxes
- campaigns
- sequences
- recipient progress
- messages
- events
- conversations
- replies
- durable scheduling
- imports
- operational state

---

# 58. User Flow Requirements Before Frontend Design

`PAGE_MAP.md` must provide clear locations for the user to complete the flows defined here.

At minimum, the information architecture will need appropriate experiences for:

```text
Authentication
Onboarding
Dashboard
Campaigns
Campaign creation/configuration
Leads
Lead lists
Imports
Suppression
Templates
Mailboxes
Inbox
Analytics
Deliverability
Integrations
Team
Settings
Billing where applicable
```

Exact paths are intentionally not defined in this document.

---

# 59. User Flow Requirements Before RBAC Implementation

`USER_ROLES.md` must answer who can perform actions such as:

```text
Create campaign
Edit campaign
Start campaign
Pause campaign
Resume campaign
Archive/delete campaign

View leads
Create leads
Edit leads
Delete leads
Import leads

Manage lists

View suppression
Add suppression
Remove suppression

Create templates
Edit templates
Delete templates

View mailboxes
Connect mailbox
Reconnect mailbox
Disconnect mailbox

View inbox

View analytics

Invite team member
Remove member
Change member role

Manage integrations
Manage workspace settings
Manage billing

Perform security-sensitive actions
Delete workspace
Transfer ownership
```

Until those decisions are frozen, this document intentionally uses the phrase **authorized user**.

---

# 60. End-to-End MVP Acceptance Journey

Before production release, the following complete journey must succeed without direct database intervention:

```text
NEW USER
   ↓
Sign up
   ↓
Create workspace
   ↓
Enter authorized workspace
   ↓
Connect supported mailbox
   ↓
Mailbox becomes healthy
   ↓
Import leads
   ↓
Create lead list
   ↓
Create reusable email content
   ↓
Create campaign
   ↓
Select recipients
   ↓
Create Email → Wait → Email sequence
   ↓
Select mailbox
   ↓
Configure schedule and limits
   ↓
Review campaign
   ↓
Start
   ↓
Campaign passes backend validation
   ↓
Messages are durably planned
   ↓
Scheduler finds due message
   ↓
Worker checks authoritative state
   ↓
Suppression check passes
   ↓
Limits permit send
   ↓
Provider sends
   ↓
Result persisted
   ↓
Recipient replies
   ↓
Reply synchronized
   ↓
Conversation visible
   ↓
Future recipient sequence stops
   ↓
Campaign analytics update
```

A second acceptance path must demonstrate unsubscribe:

```text
Campaign message sent
   ↓
Recipient unsubscribes
   ↓
Suppression stored
   ↓
Next sequence message becomes due
   ↓
Pre-send suppression check fails
   ↓
Message is NOT sent
```

A third acceptance path must demonstrate infrastructure recovery:

```text
Future message durably scheduled
   ↓
Redis / worker / scheduler restarts
   ↓
Durable message remains
   ↓
Services recover
   ↓
Due work rediscovered
   ↓
Message executes exactly according to current eligibility
```

A fourth acceptance path must demonstrate failure handling:

```text
Send attempted
   ↓
Temporary provider failure
   ↓
Retry scheduled safely
   ↓
Current eligibility rechecked
   ↓
Retry succeeds or reaches defined terminal failure
```

---

# 61. Definition of Done for USER_FLOWS.md

This document is complete when the project agrees on the following:

```text
✓ Authentication journey
✓ Workspace journey
✓ Onboarding journey
✓ Mailbox connection journeys
✓ Mailbox failure/recovery journey
✓ Lead creation journey
✓ CSV import journey
✓ Lead list journey
✓ Suppression journey
✓ Template journey
✓ Campaign creation journey
✓ Recipient selection journey
✓ Sequence creation journey
✓ Mailbox selection journey
✓ Schedule configuration journey
✓ Campaign review/start journey
✓ Pause/resume journey
✓ Durable message planning journey
✓ Send execution journey
✓ Retry/failure journeys
✓ Reply synchronization journey
✓ Unified inbox journey
✓ Unsubscribe journey
✓ Bounce journey
✓ Complaint journey
✓ Completion journey
✓ Analytics journey
✓ Notifications/health journey
✓ Team-access journey
✓ Workspace-switching journey
✓ Campaign archival journey
✓ Platform intervention journey
✓ Infrastructure recovery journey
✓ Cross-tenant authorization expectations
✓ MVP end-to-end acceptance journeys
```

Any material product flow added later should be documented explicitly rather than emerging only through frontend or backend implementation.

---

# 62. Next Documentation Dependencies

The flows in this document depend on two product specifications that must be frozen before implementation:

```text
USER_ROLES.md
    → Who can perform each action

PAGE_MAP.md
    → Where each action exists in the product
```

Once all three product-definition documents are approved:

```text
USER_ROLES.md
PAGE_MAP.md
USER_FLOWS.md
```

the project can proceed into the architecture phase:

```text
SYSTEM_ARCHITECTURE.md
        ↓
DOMAIN_MODEL.md
        ↓
CAMPAIGN_STATE_MACHINE.md
        ↓
MESSAGE_STATE_MACHINE.md
        ↓
PROVIDER_ARCHITECTURE.md
        ↓
SCHEDULER.md
        ↓
WORKERS.md
        ↓
QUEUES.md
        ↓
RATE_LIMITING.md
        ↓
EVENT_SYSTEM.md
        ↓
REPLY_SYNC.md
        ↓
SUPPRESSION.md
        ↓
DATABASE.md
```

No implementation agent should derive permanent architecture from user-flow diagrams alone.

---

# 63. Final User Flow Principle

The platform must behave as a coordinated system rather than a collection of CRUD screens.

The central product journey is:

```text
User intent
    ↓
Authorization
    ↓
Validated configuration
    ↓
Durable business state
    ↓
Safe asynchronous execution
    ↓
Provider interaction
    ↓
Recipient outcome
    ↓
Authoritative state update
    ↓
Correct next action
    ↓
Clear user visibility
```

Every major product flow should preserve that model.

The most important outcome is not simply that the platform can send an email.

It is that the platform can determine **whether an email should still be sent**, send it safely when appropriate, stop it when circumstances change, recover from failures, and accurately reflect the result to the workspace user.