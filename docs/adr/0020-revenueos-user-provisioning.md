# RevenueOS user provisioning

## Status

Accepted — 2026-10-05 (owner approved: the RevenueOS acting user owns each workspace; the login password is made by the server and shown to nobody; RevenueOS may send SMTP credentials). Amends one constraint of ADR-0019, noted under Decision 9. Builds on [ADR-0019](0019-revenueos-campaign-intake.md) and reuses its key and acting user. Migration `0038` is prepared, not applied.

## Context

This deployment is internal to MOTM. Public signup is switched off in the Supabase project, so nobody can register themselves. RevenueOS ("Brain") still needs to register a person and give them a workspace without a human creating the account in the Supabase dashboard and the workspace in the app.

Two constraints shaped the design. The campaign intake route only works in workspaces where the RevenueOS acting user is a member, so a workspace owned by a brand-new account would be unreachable for RevenueOS. And no runtime role may insert into `workspace_memberships`: the only paths are `app_bootstrap_workspace()` (creates a workspace with its Owner) and the invitation commands of `0018`.

The invitation commands could not be used. They need the invited person to accept, and against a real PostgreSQL they fail on missing privileges of their owning role (see Consequences). A throwaway-database test showed this before any code depended on it.

## Decision

1. **One route.** `POST /api/v1/integrations/revenueos/users` accepts RevenueOS's own `reference`, an `email`, an optional `workspace_name` (default `"<email> Workspace"`) and an optional `role` (`ADMIN` by default; `MANAGER`, `MEMBER` or `VIEWER`). It is not under `/workspaces/{id}` because it creates the workspace.
2. **Same key and acting user as ADR-0019, behind its own switch.** The caller sends `X-RevenueOS-Key`. The route additionally requires `REVENUEOS_PROVISIONING_ENABLED=true` and `SUPABASE_SERVICE_ROLE_KEY`; otherwise it answers `503`. It is off by default, so a deployment that only takes campaigns does not gain an account-creating surface.
3. **The account comes from the Supabase Auth admin API.** `SupabaseAuthAdminClient.ensure_user` creates a confirmed user, or finds the existing one by email. This works with public signup disabled. It runs before the request takes a database connection, so a slow Auth call never holds a pooled connection, and it is safe to repeat.
4. **No password crosses any boundary.** The server generates a random password for a new account and does not store, log or return it. The payload rejects a `password` field. The person sets their own through "Forgot password".
5. **The acting user owns the workspace.** The service calls `app_bootstrap_workspace()` as the acting user, keyed on the reference, so RevenueOS can send campaigns to the workspace at once and the workspace does not depend on one employee's account.
6. **One new database command adds the member.** `app_provision_workspace_member(user_id, role)` (migration `0038`) inserts an ACTIVE membership for an existing profile in the caller's current workspace. It checks inside that the caller is the ACTIVE Owner, never grants `OWNER`, and never reactivates a revoked membership. It is `SECURITY DEFINER`, owned by `app_foundation_reader`, executable by `app_api` only, and needs no new table privilege.
7. **Idempotent on `reference`, acknowledged in the database.** The bootstrap ledger makes the workspace unique per acting user and reference. A `command_receipts` row (`operation = 'revenueos.user_provision'`) records the request content. A repeat with the same content creates nothing (`200`, `duplicate: true`); the same reference with a different email, role or name is `409`; a repeat after the person's access was removed is `409` and does not restore it.
8. **Audited.** `workspace.bootstrap` and `membership.provisioned` rows are written to `audit_events` in the same transaction, both marked `source: revenueos`.

9. **An optional `smtp` block connects or updates the sending mailbox (owner decision, 2026-10-05).** It carries the fields of the Mailboxes "Connect SMTP" form and is handled by the existing `MailboxService` (`connect_smtp_mailbox` / `update_smtp_mailbox`), so the connection test, the address rules and credential encryption are unchanged. This replaces the ADR-0019 constraint that a mailbox is only ever connected by a person and that RevenueOS never sends SMTP credentials; Gmail and Microsoft mailboxes still need a person. Rules:
   - A mailbox is matched by sender address within the workspace. None: connect. One: update it in place. The response reports `mailbox_id` and `mailbox_status` (`connected`, `updated`, `failed`).
   - The block is not part of the reference's identity, so the same reference may carry new SMTP details later; that is how RevenueOS changes them. Omitting the block leaves mailboxes untouched.
   - The mailbox step runs in a savepoint. A refused mailbox (wrong password, unreachable host, login already in use) is reported as `failed` with the reason and does not undo the account or workspace.

## What stays manual

Connecting a Gmail or Microsoft mailbox, and starting campaigns. No email is sent to the new person by this route; someone tells them to use "Forgot password".

## Alternatives Considered

**The new account owns its workspace**: matches "one account, one workspace" literally, but RevenueOS would get `404` from campaign intake until it was separately made a member, and removing the account would orphan the workspace. **Invite and accept inside one transaction** using the `0018` commands: no migration, but those commands do not run under real privileges and matching by email adds a dependency on `auth.users`. **Brain sends the password** or **the server returns it once**: both put a credential into logs and requests; the owner chose neither. **A separate key for provisioning**: better isolation; deferred in favour of the on/off switch to keep configuration to one secret. **Granting `app_api` INSERT on memberships**: would let every API code path add members; the dedicated command keeps the rule in one place.

## Consequences

- With the switch on, whoever holds the RevenueOS key can create accounts and workspaces as well as draft campaigns. The key must be rotated if exposed, and there is no rate limit on the route.
- The service-role key is now used by the API process for Auth as well as Storage. It bypasses signup restrictions and row-level security and must stay server-side.
- A workspace Owner, through the API role, can now add an existing user without that user accepting an invitation. Only this route calls the command.
- RevenueOS now holds its clients' SMTP passwords and sends them in requests. They are encrypted in transit and at rest here, but they are in RevenueOS's own storage and, unless it keeps them out, its logs. A rejected body (`422`) echoes the submitted fields back in `details`, as every route does, which can include the SMTP password.
- With the switch on, the RevenueOS key can also repoint a workspace's SMTP mailbox at another server.
- Every request that carries the block re-tests the SMTP login over the network while the request's database transaction is open, as the Mailboxes page already does.
- One SMTP login (host, port, username) can back only one mailbox across all workspaces. A second sender address on the same login, or the same login in another workspace, is reported as `failed`.
- Finding an existing account pages through the Auth user list (the admin API has no lookup by email), bounded at 10,000 accounts. Past that the lookup fails rather than guessing.
- If the database step fails after the account was created, the account exists without a workspace. Repeating the request completes it.
- References are per acting user, as in ADR-0019: changing `REVENUEOS_ACTOR_USER_ID` makes earlier references unknown.
- **Pre-existing issue found, not changed here:** the `0018` team commands (`app_create_workspace_invitation` and the others) are owned by `app_foundation_reader`, which was never granted what they use (`app_has_permission`, `workspace_invitations`, `audit_events`, `auth.users`). They fail with `permission denied`, and `TeamService` then rolls back and repeats the work with direct SQL. That rollback also drops `SET LOCAL ROLE app_api`, so the fallback runs as the login role. The Team page's invitations therefore do not run under the intended least-privilege role. The command failure was reproduced on a throwaway database; the fallback's role is read from the code, not observed live. This needs its own review and a forward migration.
