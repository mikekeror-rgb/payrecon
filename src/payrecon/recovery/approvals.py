"""Approval desk: the only way an approval-required action reaches APPROVED.

Roles are a plain mapping here; in the service they come from JWT claims (roadmap). Rules enforced:
only a known APPROVER may approve; machines (system, ai-*, link-matcher) never can; nobody approves what
they proposed (separation of duties).
"""
from __future__ import annotations

from enum import Enum
from typing import Mapping

from .models import ActionStatus, RecoveryAction
from .store import ActionStore

MACHINE_ACTORS = ("system", "link-matcher")


class Role(str, Enum):
    VIEWER = "viewer"
    APPROVER = "approver"


class ApprovalError(PermissionError):
    pass


class ApprovalDesk:
    def __init__(self, store: ActionStore, roles: Mapping[str, Role]):
        self.store, self.roles = store, dict(roles)

    def queue(self) -> list[RecoveryAction]:
        return [a for a in self.store.list_actions(ActionStatus.PROPOSED) if a.requires_approval]

    def _check(self, user: str, action: RecoveryAction) -> None:
        if user in MACHINE_ACTORS or user.startswith("ai-"):
            raise ApprovalError(f"{user!r} is a machine identity and cannot approve")
        if self.roles.get(user) is not Role.APPROVER:
            raise ApprovalError(f"{user!r} does not have the approver role")
        if user == action.proposed_by:
            raise ApprovalError("the proposer cannot approve their own action")

    def approve(self, action_id: int, user: str, note: str = "") -> RecoveryAction:
        a = self.store.get(action_id)
        if not a.requires_approval:
            raise ApprovalError("this action does not need approval")
        self._check(user, a)
        if not self.store.approve(action_id, user, note):
            raise ApprovalError(f"action {action_id} is {self.store.get(action_id).status.value}, not awaiting approval")
        return self.store.get(action_id)

    def reject(self, action_id: int, user: str, reason: str) -> RecoveryAction:
        a = self.store.get(action_id)
        self._check(user, a)
        if not self.store.reject(action_id, user, reason):
            raise ApprovalError(f"action {action_id} can no longer be rejected")
        return self.store.get(action_id)

    def review_queue(self) -> list[dict]:
        return self.store.open_escalations()

    def resolve(self, escalation_id: int, user: str, resolution: str) -> None:
        if user in MACHINE_ACTORS or user.startswith("ai-") or self.roles.get(user) is not Role.APPROVER:
            raise ApprovalError(f"{user!r} cannot resolve review items")
        if not self.store.resolve_escalation(escalation_id, user, resolution):
            raise ApprovalError(f"review item {escalation_id} is not open")
