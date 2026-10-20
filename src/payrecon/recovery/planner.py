"""Turn reconciliation output into proposed actions. Proposing moves no money.

Keys are derived from the facts that justify the action, so re-running the planner (a retried job, two
workers, a restart) proposes the same keys and the store ignores the repeats.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from ..matcher import normalize_reference, reconcile
from ..models import Action, Category, Invoice, ProviderTxn, ReconciliationReport, TxnStatus
from .config import RecoveryConfig
from .models import ActionType, Escalation, RecoveryAction
from .store import ActionStore


def _h(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def keeper_and_extras(txns: Sequence[ProviderTxn]) -> tuple[ProviderTxn, list[ProviderTxn]]:
    """For a duplicate payment, the earliest success stays; later ones are refund candidates."""
    ok = sorted((t for t in txns if t.status is TxnStatus.SUCCESS), key=lambda t: (t.timestamp, t.provider_txn_id))
    return ok[0], ok[1:]


@dataclass
class PlanResult:
    proposed: list[RecoveryAction] = field(default_factory=list)
    existing: int = 0  # proposals ignored because the key already existed
    new_escalations: int = 0  # review items opened by this run (the rest were already open)
    skipped: list[str] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)


def plan(
    report: ReconciliationReport,
    invoices: Iterable[Invoice],
    txns: Iterable[ProviderTxn],
    store: ActionStore,
    *,
    link_proposals: Sequence = (),
    config: RecoveryConfig = RecoveryConfig(),
    proposed_by: str = "system",
) -> PlanResult:
    inv_by_id = {i.invoice_id: i for i in invoices}
    txn_by_id = {t.provider_txn_id: t for t in txns}
    out = PlanResult()
    now = store.now()

    def propose(inv_id: str, kind: ActionType, key: str, payload: dict, approval: bool, reason: str, by: str = proposed_by):
        if (open_ := store.open_action(inv_id, kind)) is not None and open_.idempotency_key != key:
            out.skipped.append(f"{inv_id}: {kind.value} already open (action {open_.action_id})")
            return
        a, created = store.propose(
            invoice_id=inv_id, action_type=kind, key=key, payload=payload, requires_approval=approval, proposed_by=by, reason=reason
        )
        if created:
            out.proposed.append(a)
        else:
            out.existing += 1

    for inv_id, res in report.invoices.items():
        inv = inv_by_id[inv_id]
        mine = [txn_by_id[t] for t in res.txn_ids if t in txn_by_id]
        successes = [t for t in mine if t.status is TxnStatus.SUCCESS]

        if res.action is Action.AUTO_APPLY:
            over = config.max_auto_apply_amount is not None and inv.amount > config.max_auto_apply_amount
            ids = sorted(t.provider_txn_id for t in successes)
            propose(inv_id, ActionType.APPLY_PAYMENT, f"apply:{inv_id}:{_h(*ids)}",
                    {"txn_ids": ids, "amount": str(inv.amount)}, over, res.note or "provider-confirmed, exact amount")

        elif res.action is Action.RECHECK_STATUS:
            pending = sorted(t.provider_txn_id for t in mine if t.status is TxnStatus.PENDING)
            if store.count_actions(inv_id, ActionType.RECHECK_STATUS) >= config.max_rechecks:
                out.escalations.append(Escalation(inv_id, f"still pending after {config.max_rechecks} rechecks", {"pending": pending}))
                continue
            bucket = int(now.timestamp() // config.recheck_interval_s)
            propose(inv_id, ActionType.RECHECK_STATUS, f"recheck:{inv_id}:{_h(*pending)}:{bucket}",
                    {"request_ids": pending}, False, "pending, ask the provider")

        elif res.action is Action.RETRY_ELIGIBLE:
            failed = [t for t in mine if t.status is TxnStatus.FAILED]
            latest = max(failed, key=lambda t: (t.timestamp, t.provider_txn_id))
            n = store.send_count(inv_id) + 1
            propose(inv_id, ActionType.RETRY_PUSH, f"retry:{inv_id}:{latest.provider_txn_id}:{n}",
                    {"retry_of": latest.provider_txn_id, "amount": str(inv.amount), "result_code": latest.result_code},
                    inv.amount > config.auto_retry_max_amount, f"transient failure {latest.result_code}")

        elif res.action is Action.NOTIFY_CUSTOMER:
            failed = [t for t in mine if t.status is TxnStatus.FAILED]
            latest = max(failed, key=lambda t: (t.timestamp, t.provider_txn_id))
            propose(inv_id, ActionType.NOTIFY_CUSTOMER, f"notify:{inv_id}:{latest.provider_txn_id}",
                    {"template": f"payment_failed_{latest.result_code}", "failed_request": latest.provider_txn_id},
                    False, "customer must act")

        elif res.action is Action.ESCALATE_HUMAN:
            if res.category is Category.DUPLICATE_PAYMENT:
                keep, extras = keeper_and_extras(mine)
                done = store.refunded_txns()
                for t in extras:
                    if t.provider_txn_id in done:
                        out.skipped.append(f"{inv_id}: refund of {t.provider_txn_id} already started")
                        continue
                    propose(inv_id, ActionType.REFUND_DUPLICATE, f"refund:{inv_id}:{t.provider_txn_id}",
                            {"refund_txn_id": t.provider_txn_id, "keep_txn_id": keep.provider_txn_id, "amount": str(t.amount)},
                            True, "duplicate full payment; later one proposed for refund")
            else:
                why = res.note or res.action.value
                if res.category is Category.FAILED:
                    failed = [t for t in mine if t.status is TxnStatus.FAILED]
                    latest = max(failed, key=lambda t: (t.timestamp, t.provider_txn_id))
                    why = f"result code {latest.result_code or 'none'} ({latest.result_desc or 'no message'}): unknown code or retries used up"
                out.escalations.append(Escalation(inv_id, f"{res.category.value}: {why}", {"txn_ids": res.txn_ids}))

    # Inferred links: advice from the AI layer or a fuzzy matcher. Always a human decision.
    for lp in link_proposals:
        t = txn_by_id.get(lp.txn_id)
        if t is None or lp.invoice_id not in inv_by_id or t.status is not TxnStatus.SUCCESS:
            continue
        inv = inv_by_id[lp.invoice_id]
        if t.amount != inv.amount:
            continue
        propose(lp.invoice_id, ActionType.APPLY_LINKED_PAYMENT, f"link:{lp.txn_id}:{lp.invoice_id}",
                {"txn_id": lp.txn_id, "amount": str(t.amount), "confidence": lp.confidence, "band": lp.band,
                 "decided_by": lp.decided_by, "rationale": str(lp.rationale)[:500]},
                True, "inferred link needs approval", by="ai-assistant" if lp.decided_by == "llm" else "link-matcher")  # say which one actually decided
    for e in out.escalations:
        if store.escalate(e.invoice_id, e.reason, e.detail):
            out.new_escalations += 1
    return out


def plan_from_data(invoices: Sequence[Invoice], txns: Sequence[ProviderTxn], store: ActionStore, **kw) -> tuple[ReconciliationReport, PlanResult]:
    """Reconcile with retry counts taken from the durable store, then plan."""
    applied = kw.pop("applied", None)
    report = reconcile(invoices, txns, store.retry_counts(), applied)
    return report, plan(report, invoices, txns, store, **kw)
