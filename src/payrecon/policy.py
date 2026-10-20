"""Deterministic action policy.

Financial decisions live here, in plain code, not in a model. The AI layer may
explain or rank cases, but it never changes a category or an action.

Daraja STK push result codes below are from memory of the public docs; verify
them against the current Daraja documentation before relying on them.
"""
from __future__ import annotations

from typing import Sequence

from .models import Action, Category, ProviderTxn, TxnStatus

MAX_RETRIES = 3

# Transient: the push never completed for a reason that is not the customer's decision.
RETRYABLE_CODES = {
    "1037",  # DS timeout, customer unreachable
    "1001",  # subscriber locked: another transaction in progress
}
# The customer has to do something (or chose not to): never auto re-push.
CUSTOMER_ACTION_CODES = {
    "1",  # insufficient balance
    "1032",  # request cancelled by user
    "2001",  # wrong PIN / initiator information invalid
}

_STATIC_ACTIONS = {
    Category.MATCHED: Action.NONE,
    Category.UNRECORDED_PAYMENT: Action.AUTO_APPLY,
    Category.DUPLICATE_PAYMENT: Action.ESCALATE_HUMAN,  # refunds always need approval
    Category.AMOUNT_MISMATCH: Action.ESCALATE_HUMAN,
    Category.PARTIAL_PAYMENT: Action.ESCALATE_HUMAN,
    Category.PENDING: Action.RECHECK_STATUS,
    Category.MISSING_PAYMENT: Action.AWAIT_PAYMENT,
    # The customer may believe they paid, and the books may say so: a person tells them and corrects the books.
    Category.REVERSED_PAYMENT: Action.ESCALATE_HUMAN,
}


def failure_action(result_code: str | None, retry_count: int) -> Action:
    if result_code in RETRYABLE_CODES:
        return Action.RETRY_ELIGIBLE if retry_count < MAX_RETRIES else Action.ESCALATE_HUMAN
    if result_code in CUSTOMER_ACTION_CODES:
        return Action.NOTIFY_CUSTOMER
    return Action.ESCALATE_HUMAN  # unknown code: do not guess


def decide_action(category: Category, txns: Sequence[ProviderTxn], retry_count: int = 0) -> Action:
    has_pending = any(t.status is TxnStatus.PENDING for t in txns)
    # A still-pending transaction next to a confirmed one could turn into a double
    # charge. Resolve it before applying anything.
    if category in (Category.MATCHED, Category.UNRECORDED_PAYMENT) and has_pending:
        return Action.RECHECK_STATUS
    if category is Category.FAILED:
        latest = max((t for t in txns if t.status is TxnStatus.FAILED), key=lambda t: t.timestamp)
        return failure_action(latest.result_code, retry_count)
    return _STATIC_ACTIONS[category]


def decide_orphan_action(txn: ProviderTxn) -> Action:
    # Money received that links to no invoice is the one case worth a human or a
    # fuzzy matcher. Failed or pending orphans move no money.
    return Action.LINK_OR_ESCALATE if txn.status is TxnStatus.SUCCESS else Action.NONE
