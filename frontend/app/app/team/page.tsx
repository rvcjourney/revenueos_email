"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRightLeft,
  Check,
  Clock,
  Copy,
  Loader2,
  Mail,
  RefreshCw,
  Shield,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api-client";
import {
  canInviteMembers,
  canManageRoles,
  canTransferOwnership,
} from "@/lib/permissions";
import {
  createInvitation,
  listInvitations,
  listTeamMembers,
  removeMember,
  resendInvitation,
  revokeInvitation,
  transferOwnership,
  updateMemberRole,
} from "@/lib/team-api";
import { useWorkspace } from "@/lib/workspace-context";
import type { RoleCode, WorkspaceInvitation, WorkspaceMember } from "@/types/domain";

const AVAILABLE_ROLES: RoleCode[] = ["ADMIN", "MANAGER", "MEMBER", "VIEWER"];

export default function TeamPage() {
  const { activeWorkspaceId, activeWorkspace } = useWorkspace();
  const queryClient = useQueryClient();

  const userRole = activeWorkspace?.role_code;
  const isOwner = canManageRoles(userRole);
  const canInvite = canInviteMembers(userRole);

  // Modals & UI state
  const [inviteModalOpen, setInviteModalOpen] = useState(false);
  const [transferModalOpen, setTransferModalOpen] = useState(false);
  const [memberToRemove, setMemberToRemove] = useState<WorkspaceMember | null>(null);

  // Invite Form
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<RoleCode>("MEMBER");
  const [lastInviteLink, setLastInviteLink] = useState<string | null>(null);
  const [copiedLink, setCopiedLink] = useState(false);

  // Transfer Ownership Form
  const [newOwnerMembershipId, setNewOwnerMembershipId] = useState("");

  // Status banners
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // Queries
  const {
    data: members,
    isLoading: loadingMembers,
    isError: membersError,
  } = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "team"],
    queryFn: () => listTeamMembers(activeWorkspaceId!),
    enabled: Boolean(activeWorkspaceId),
  });

  const {
    data: invitations,
    isLoading: loadingInvitations,
  } = useQuery({
    queryKey: ["workspace", activeWorkspaceId, "invitations"],
    queryFn: () => listInvitations(activeWorkspaceId!),
    enabled: Boolean(activeWorkspaceId && canInvite),
  });

  // Mutations
  const updateRoleMutation = useMutation({
    mutationFn: ({
      memberId,
      newRole,
      expectedVersion,
    }: {
      memberId: string;
      newRole: RoleCode;
      expectedVersion: number;
    }) =>
      updateMemberRole(activeWorkspaceId!, memberId, {
        role_code: newRole,
        expected_version: expectedVersion,
      }),
    onSuccess: () => {
      setSuccessMessage("Member role updated successfully.");
      setErrorMessage(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "team"],
      });
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof ApiError
          ? err.message
          : "Failed to update member role. Please refresh.";
      setErrorMessage(msg);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "team"],
      });
    },
  });

  const removeMemberMutation = useMutation({
    mutationFn: (member: WorkspaceMember) =>
      removeMember(activeWorkspaceId!, member.membership_id, {
        expected_version: member.version,
      }),
    onSuccess: () => {
      setSuccessMessage("Member removed from workspace.");
      setErrorMessage(null);
      setMemberToRemove(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "team"],
      });
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError ? err.message : "Failed to remove member.",
      );
      setMemberToRemove(null);
    },
  });

  const inviteMutation = useMutation({
    mutationFn: () =>
      createInvitation(activeWorkspaceId!, {
        email: inviteEmail.trim(),
        role_code: inviteRole,
      }),
    onSuccess: (data) => {
      setSuccessMessage(`Invitation dispatched to ${inviteEmail}.`);
      setErrorMessage(null);
      if (data.invite_token) {
        const origin = window.location.origin;
        setLastInviteLink(`${origin}/auth/accept-invite?token=${data.invite_token}`);
      } else {
        setInviteModalOpen(false);
        setInviteEmail("");
      }
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "invitations"],
      });
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError ? err.message : "Failed to send invitation.",
      );
    },
  });

  const resendInviteMutation = useMutation({
    mutationFn: (invitationId: string) =>
      resendInvitation(activeWorkspaceId!, invitationId),
    onSuccess: () => {
      setSuccessMessage("Invitation resent successfully.");
      setErrorMessage(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "invitations"],
      });
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError ? err.message : "Failed to resend invitation.",
      );
    },
  });

  const revokeInviteMutation = useMutation({
    mutationFn: (invitationId: string) =>
      revokeInvitation(activeWorkspaceId!, invitationId),
    onSuccess: () => {
      setSuccessMessage("Invitation revoked.");
      setErrorMessage(null);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId, "invitations"],
      });
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError ? err.message : "Failed to revoke invitation.",
      );
    },
  });

  const transferMutation = useMutation({
    mutationFn: () => {
      const owner = members?.find(
        (m) => m.role_code === "OWNER" && m.status === "ACTIVE",
      );
      const target = members?.find(
        (m) => m.membership_id === newOwnerMembershipId,
      );
      if (!owner || !target) {
        throw new Error("Select a member to transfer ownership to.");
      }
      return transferOwnership(activeWorkspaceId!, {
        target_membership_id: target.membership_id,
        expected_owner_version: owner.version,
        expected_target_version: target.version,
      });
    },
    onSuccess: () => {
      setSuccessMessage(
        "Workspace ownership transferred successfully. Your role has been updated.",
      );
      setErrorMessage(null);
      setTransferModalOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["workspace", activeWorkspaceId],
      });
      window.location.reload();
    },
    onError: (err: unknown) => {
      setErrorMessage(
        err instanceof ApiError
          ? err.message
          : "Failed to transfer ownership.",
      );
    },
  });

  function handleCopyInviteLink() {
    if (!lastInviteLink) return;
    navigator.clipboard.writeText(lastInviteLink);
    setCopiedLink(true);
    setTimeout(() => setCopiedLink(false), 2000);
  }

  return (
    <main className="space-y-6 max-w-6xl pb-16">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.08em] text-brand-600">Workspace Management</p>
          <h1 className="text-[26px] font-bold leading-8 tracking-tight text-slate-900 mt-1">
            Team &amp; Access
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Manage teammates, roles, pending invitations, and workspace ownership.
          </p>
        </div>

        <div className="flex items-center gap-3">
          {canTransferOwnership(userRole) && members && members.length > 1 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const candidates = members.filter(
                  (m) => m.role_code !== "OWNER" && m.status === "ACTIVE",
                );
                setNewOwnerMembershipId(
                  candidates.length > 0 ? candidates[0].membership_id : "",
                );
                setTransferModalOpen(true);
              }}
              className="gap-2 text-slate-700 hover:text-slate-900"
            >
              <ArrowRightLeft className="h-4 w-4" />
              Transfer Ownership
            </Button>
          )}

          {canInvite && (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setLastInviteLink(null);
                setInviteEmail("");
                setInviteModalOpen(true);
              }}
              className="gap-2"
            >
              <UserPlus className="h-4 w-4" />
              Invite Teammate
            </Button>
          )}
        </div>
      </div>

      {/* Alerts */}
      {errorMessage && (
        <Alert variant="error" className="flex items-center justify-between">
          <span>{errorMessage}</span>
          <button
            type="button"
            onClick={() => setErrorMessage(null)}
            className="text-red-700 hover:text-red-900"
          >
            <X className="h-4 w-4" />
          </button>
        </Alert>
      )}

      {successMessage && (
        <Alert variant="info" className="flex items-center justify-between">
          <span>{successMessage}</span>
          <button
            type="button"
            onClick={() => setSuccessMessage(null)}
            className="text-brand-700 hover:text-brand-900"
          >
            <X className="h-4 w-4" />
          </button>
        </Alert>
      )}

      {/* Members Directory */}
      <section className="rounded-xl border border-slate-200 bg-white shadow-card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Users className="h-5 w-5 text-slate-600" />
            <h2 className="text-base font-semibold text-slate-900">
              Active Members
            </h2>
            {members && (
              <span className="ml-2 inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-700">
                {members.length}
              </span>
            )}
          </div>
        </div>

        {loadingMembers ? (
          <div className="flex items-center justify-center gap-2 p-12 text-sm text-slate-500">
            <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
            Loading team members&hellip;
          </div>
        ) : membersError ? (
          <p className="p-8 text-center text-sm text-red-600">
            Unable to load team members. Please try refreshing.
          </p>
        ) : !members || members.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-12 text-center text-sm text-slate-500">
            <Users className="h-8 w-8 text-slate-300" aria-hidden="true" />
            No members found in this workspace.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                <tr>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Email</th>
                  <th className="px-4 py-3">Role</th>
                  <th className="px-4 py-3">Joined</th>
                  {isOwner && <th className="px-4 py-3 text-right">Actions</th>}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {members.map((member) => {
                  const isCurrentMemberOwner = member.role_code === "OWNER";
                  return (
                    <tr
                      key={member.membership_id}
                      className="hover:bg-slate-50/60 transition-colors"
                    >
                      <td className="px-4 py-3.5">
                        <div className="flex items-center gap-3">
                          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand-100 text-brand-800 font-semibold text-sm">
                            {(member.display_name?.[0] ?? member.email?.[0] ?? "U").toUpperCase()}
                          </div>
                          <div>
                            <p className="font-medium text-slate-900">
                              {member.display_name ?? "Member"}
                            </p>
                            <p className="text-xs text-slate-500 md:hidden">
                              {member.email}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3.5 text-slate-600">
                        {member.email ?? "—"}
                      </td>
                      <td className="px-4 py-3.5">
                        {isOwner && !isCurrentMemberOwner ? (
                          <select
                            value={member.role_code}
                            disabled={updateRoleMutation.isPending}
                            onChange={(e) =>
                              updateRoleMutation.mutate({
                                memberId: member.membership_id,
                                newRole: e.target.value as RoleCode,
                                expectedVersion: member.version,
                              })
                            }
                            className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-8 px-2 text-xs w-auto"
                          >
                            {AVAILABLE_ROLES.map((role) => (
                              <option key={role} value={role}>
                                {role}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <span
                            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                              isCurrentMemberOwner
                                ? "bg-amber-100 text-amber-800"
                                : "bg-slate-100 text-slate-700"
                            }`}
                          >
                            {isCurrentMemberOwner && (
                              <Shield className="h-3 w-3" />
                            )}
                            {member.role_code}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3.5 text-xs text-slate-500">
                        {new Date(member.joined_at).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })}
                      </td>
                      {isOwner && (
                        <td className="px-4 py-3.5 text-right">
                          {!isCurrentMemberOwner && (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => setMemberToRemove(member)}
                              className="text-red-600 hover:bg-red-50 hover:text-red-700 h-8 px-2"
                              aria-label={`Remove ${member.display_name ?? member.email}`}
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Pending Invitations Section */}
      {canInvite && (
        <section className="rounded-xl border border-slate-200 bg-white shadow-card overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Mail className="h-5 w-5 text-slate-600" />
              <h2 className="text-base font-semibold text-slate-900">
                Pending Invitations
              </h2>
              {invitations && (
                <span className="ml-2 inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-700">
                  {invitations.filter((i) => i.status === "PENDING").length}
                </span>
              )}
            </div>
          </div>

          {loadingInvitations ? (
            <div className="flex items-center justify-center gap-2 p-8 text-sm text-slate-500">
              <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
              Loading invitations&hellip;
            </div>
          ) : !invitations || invitations.length === 0 ? (
            <div className="p-8 text-center text-sm text-slate-500">
              No pending invitations.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50/80 text-left text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Invited Email</th>
                    <th className="px-4 py-3">Role</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Expires</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {invitations.map((inv) => (
                    <tr
                      key={inv.id}
                      className="hover:bg-slate-50/60 transition-colors"
                    >
                      <td className="px-4 py-3.5 font-medium text-slate-900">
                        {inv.email}
                      </td>
                      <td className="px-4 py-3.5">
                        <span className="inline-flex rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-700">
                          {inv.role_code}
                        </span>
                      </td>
                      <td className="px-4 py-3.5">
                        <span
                          className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                            inv.status === "PENDING"
                              ? "bg-amber-50 text-amber-700"
                              : inv.status === "ACCEPTED"
                              ? "bg-emerald-50 text-emerald-700"
                              : "bg-slate-100 text-slate-600"
                          }`}
                        >
                          <Clock className="h-3 w-3" />
                          {inv.status}
                        </span>
                      </td>
                      <td className="px-4 py-3.5 text-xs text-slate-500">
                        {new Date(inv.expires_at).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </td>
                      <td className="px-4 py-3.5 text-right">
                        {inv.status === "PENDING" && (
                          <div className="flex items-center justify-end gap-2">
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={resendInviteMutation.isPending}
                              onClick={() => resendInviteMutation.mutate(inv.id)}
                              className="text-slate-600 hover:text-slate-900 h-8 px-2"
                              title="Resend invitation"
                            >
                              <RefreshCw className="h-3.5 w-3.5" />
                              <span className="hidden sm:inline text-xs ml-1">
                                Resend
                              </span>
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={revokeInviteMutation.isPending}
                              onClick={() => revokeInviteMutation.mutate(inv.id)}
                              className="text-red-600 hover:bg-red-50 hover:text-red-700 h-8 px-2"
                              title="Revoke invitation"
                            >
                              <X className="h-3.5 w-3.5" />
                              <span className="hidden sm:inline text-xs ml-1">
                                Revoke
                              </span>
                            </Button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* Modal: Invite Teammate */}
      {inviteModalOpen && (
        <Dialog
          open
          onRequestClose={() => setInviteModalOpen(false)}
          title="Invite Teammate"
          align="center"
          className="max-w-md rounded-xl"
        >
          <div className="space-y-4 p-4">

            {lastInviteLink ? (
              <div className="space-y-4 py-2">
                <div className="rounded-md bg-brand-50 border border-brand-200 p-3 text-sm text-brand-800">
                  <p className="font-medium">Invitation link generated!</p>
                  <p className="text-xs mt-1 text-brand-700">
                    A transactional email has been queued. You can also share the direct link below:
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    readOnly
                    value={lastInviteLink}
                    className="flex-1 rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-xs font-mono text-slate-800"
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleCopyInviteLink}
                    className="gap-1"
                  >
                    {copiedLink ? (
                      <>
                        <Check className="h-4 w-4 text-brand-600" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="h-4 w-4" />
                        Copy
                      </>
                    )}
                  </Button>
                </div>
                <Button
                  variant="primary"
                  className="w-full"
                  onClick={() => {
                    setInviteModalOpen(false);
                    setLastInviteLink(null);
                  }}
                >
                  Done
                </Button>
              </div>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  inviteMutation.mutate();
                }}
                className="space-y-4"
              >
                <div>
                  <Label htmlFor="invite-email">Teammate Email</Label>
                  <Input
                    id="invite-email"
                    type="email"
                    required
                    placeholder="colleague@company.com"
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    className="mt-1"
                  />
                </div>

                <div>
                  <Label htmlFor="invite-role">Workspace Role</Label>
                  <select
                    id="invite-role"
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value as RoleCode)}
                    className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-10 px-3 mt-1"
                  >
                    <option value="MEMBER">MEMBER (View &amp; draft campaigns)</option>
                    <option value="MANAGER">MANAGER (Execute campaigns &amp; mailboxes)</option>
                    <option value="ADMIN">ADMIN (Workspace settings &amp; invitations)</option>
                    <option value="VIEWER">VIEWER (Read-only analytics)</option>
                  </select>
                </div>

                <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setInviteModalOpen(false)}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="submit"
                    variant="primary"
                    disabled={inviteMutation.isPending || !inviteEmail.trim()}
                  >
                    {inviteMutation.isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin mr-1" />
                        Sending&hellip;
                      </>
                    ) : (
                      "Send Invitation"
                    )}
                  </Button>
                </div>
              </form>
            )}
          </div>
        </Dialog>
      )}

      {/* Modal: Transfer Ownership */}
      {transferModalOpen && (
        <Dialog
          open
          onRequestClose={() => setTransferModalOpen(false)}
          title="Transfer Ownership"
          headerContent={<AlertTriangle className="h-5 w-5 text-amber-600" aria-hidden="true" />}
          align="center"
          className="max-w-md rounded-xl"
        >
          <div className="space-y-4 p-4">

            <div className="rounded-md bg-amber-50 border border-amber-200 p-3 text-xs text-amber-800 space-y-1">
              <p className="font-semibold">Irreversible Administrative Action</p>
              <p>
                Transferring ownership designates another member as the primary OWNER. You will be demoted to ADMIN, but will no longer have exclusive ownership authority.
              </p>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                transferMutation.mutate();
              }}
              className="space-y-4"
            >
              <div>
                <Label htmlFor="new-owner">Select New Owner</Label>
                <select
                  id="new-owner"
                  value={newOwnerMembershipId}
                  onChange={(e) => setNewOwnerMembershipId(e.target.value)}
                  className="w-full rounded-md border border-slate-300 bg-white text-sm text-slate-900 shadow-sm transition-colors placeholder:text-slate-400 hover:border-slate-400 focus-visible:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-500 disabled:opacity-70 aria-[invalid=true]:border-red-400 aria-[invalid=true]:focus-visible:ring-red-500/30 h-10 px-3 mt-1"
                >
                  <option value="" disabled>
                    Choose a member&hellip;
                  </option>
                  {members
                    ?.filter((m) => m.role_code !== "OWNER" && m.status === "ACTIVE")
                    .map((m) => (
                      <option key={m.membership_id} value={m.membership_id}>
                        {m.display_name ?? m.email} ({m.email})
                      </option>
                    ))}
                </select>
              </div>

              <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setTransferModalOpen(false)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="danger"
                  disabled={transferMutation.isPending || !newOwnerMembershipId}
                >
                  {transferMutation.isPending ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-1" />
                      Transferring&hellip;
                    </>
                  ) : (
                    "Confirm Transfer"
                  )}
                </Button>
              </div>
            </form>
          </div>
        </Dialog>
      )}

      {/* Modal: Remove Member Confirmation */}
      <ConfirmDialog
        open={memberToRemove !== null}
        title="Remove Team Member?"
        description={
          memberToRemove ? (
            <>
              Are you sure you want to remove{" "}
              <strong className="text-slate-900">
                {memberToRemove.display_name ?? memberToRemove.email}
              </strong>{" "}
              from this workspace? They will immediately lose access to all campaigns and data.
            </>
          ) : null
        }
        confirmLabel="Remove"
        tone="danger"
        loading={removeMemberMutation.isPending}
        onCancel={() => setMemberToRemove(null)}
        onConfirm={() => {
          if (memberToRemove) removeMemberMutation.mutate(memberToRemove);
        }}
      />
    </main>
  );
}
