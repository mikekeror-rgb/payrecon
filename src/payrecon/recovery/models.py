"""Recovery action types and states."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    RECHECK_STATUS = "RECHECK_STATUS"  # read-only: ask the provider about a pending push
    APPLY_PAYMENT = "APPLY_PAYMENT"  # mark invoice paid; provider-confirmed, exact amount, linked by reference
    APPLY_LINKED_PAYMENT = "APPLY_LINKED_PAYMENT"  # same, but the link was inferred (AI or fuzzy): always needs a human
    RETRY_PUSH = "RETRY_PUSH"  # send a new STK push after a transient failure
    NOTIFY_CUSTOMER = "NOTIFY_CUSTOMER"
    REFUND_DUPLICATE = "REFUND_DUPLICATE"  # reverse the later of two full payments: always needs a human


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    FAILED = "FAILED"  # tried and gave up; nothing is known to have happened beyond what the audit log says
    REJECTED = "REJECTED"  # a human said no
    CANCELLED = "CANCELLED"  # a guard found the world had changed; nothing was done
    UNKNOWN = "UNKNOWN"  # a side effect may or may not have happened; a human must look, we never resend


TERMINAL = {ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.REJECTED, ActionStatus.CANCELLED, ActionStatus.UNKNOWN}
OPEN = {ActionStatus.PROPOSED, ActionStatus.APPROVED, ActionStatus.EXECUTING}

# Effects that are safe to run twice (books are keyed by idempotency key; a recheck only reads;
# a repeated customer notice is a nuisance, not a loss). Everything else is at-most-once.
REPEAT_SAFE = {
    ActionType.RECHECK_STATUS,
    ActionType.APPLY_PAYMENT,
    ActionType.APPLY_LINKED_PAYMENT,
    ActionType.NOTIFY_CUSTOMER,
}


@dataclass
class RecoveryAction:
    action_id: int
    idempotency_key: str
    invoice_id: str
    action_type: ActionType
    status: ActionStatus
    requires_approval: bool
    proposed_by: str
    approved_by: str | None
    approved_at: str | None
    attempt: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    last_error: str | None
    not_before: str | None
    claimed_by: str | None
    claimed_at: str | None
    side_effect_at: str | None
    request_id: str | None
    created_at: str
    updated_at: str


@dataclass
class Escalation:
    """Something a person must decide that has no automatic action to approve (amount mismatch, partial, ...)."""

    invoice_id: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
