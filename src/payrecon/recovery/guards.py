"""Pre-execution guards.

The plan was made from a snapshot. By the time an action runs the world may have moved: the customer paid
another way, a pending push succeeded, someone else applied the payment. So every action is re-validated
against fresh reads, and the deterministic engine is re-run on the invoice's current transactions. A guard
never trusts the payload, the model's rationale, or the age of an approval. It either allows, cancels
(the world changed; nothing is done), or asks to retry later (we could not verify, so we do nothing yet).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Sequence

from ..matcher import normalize_reference, reconcile
from ..models import Action, Category, InternalStatus, Invoice, InvoiceResult, ProviderTxn, TxnStatus
from ..providers.base import ProviderError
from .config import RecoveryConfig
from .models import ActionStatus, ActionType, RecoveryAction
from .planner import keeper_and_extras
from .ports import BooksPort, LedgerPort, PushPort
from .store import ActionStore


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    CANCEL = "CANCEL"  # preconditions no longer hold; terminal, nothing was done
    LATER = "LATER"  # could not verify; nothing was done; try again after a backoff


@dataclass(frozen=True)
class GuardResult:
    verdict: Verdict
    reason: str = ""


ALLOW = GuardResult(Verdict.ALLOW)


def cancel(reason: str) -> GuardResult:
    return GuardResult(Verdict.CANCEL, reason)


def later(reason: str) -> GuardResult:
    return GuardResult(Verdict.LATER, reason)


class Guards:
    MAX_STATUS_CHECKS = 8  # prior requests asked about per retry; an invoice with more than this is a human's

    def __init__(self, store: ActionStore, books: BooksPort, ledger: LedgerPort, push: PushPort, config: RecoveryConfig):
        self.store, self.books, self.ledger, self.push, self.config = store, books, ledger, push, config

    # ---- shared -----------------------------------------------------------------

    def _fresh(self, invoice_id: str, retry_count: int = 0) -> tuple[Invoice | None, list[ProviderTxn], InvoiceResult | None]:
        inv = self.books.get_invoice(invoice_id)
        if inv is None:
            return None, [], None
        txns = self.ledger.txns_for_reference(inv.reference)
        report = reconcile([inv], txns, {inv.invoice_id: retry_count})
        return inv, txns, report.invoices[inv.invoice_id]

    def _approval(self, a: RecoveryAction) -> GuardResult | None:
        if not a.requires_approval:
            return None
        if not a.approved_by or not a.approved_at:
            return cancel("approval required but missing")  # the DB constraint should make this unreachable
        age = self.store.now().timestamp() - datetime.fromisoformat(a.approved_at).timestamp()
        if age > self.config.approval_max_age_s:
            return cancel("approval expired; propose again")
        return None

    def check(self, a: RecoveryAction) -> GuardResult:
        denied = self._approval(a)
        if denied:
            return denied
        fn = {
            ActionType.RECHECK_STATUS: self._recheck,
            ActionType.APPLY_PAYMENT: self._apply,
            ActionType.APPLY_LINKED_PAYMENT: self._apply_linked,
            ActionType.RETRY_PUSH: self._retry,
            ActionType.NOTIFY_CUSTOMER: self._notify,
            ActionType.REFUND_DUPLICATE: self._refund,
        }[a.action_type]
        return fn(a)

    # ---- per type -----------------------------------------------------------------

    def _recheck(self, a: RecoveryAction) -> GuardResult:
        inv = self.books.get_invoice(a.invoice_id)
        if inv is None:
            return cancel("invoice not found")
        if inv.internal_status is InternalStatus.PAID:
            return cancel("invoice already paid")
        return ALLOW  # read-only

    def _apply(self, a: RecoveryAction) -> GuardResult:
        if self.books.is_applied(a.idempotency_key):
            return ALLOW  # we crashed after applying; the effect step will see this and finish the bookkeeping
        inv, txns, res = self._fresh(a.invoice_id)
        if inv is None or res is None:
            return cancel("invoice not found")
        if inv.internal_status is InternalStatus.PAID:
            return cancel("invoice already paid")
        planned = set(a.payload["txn_ids"])
        if Decimal(a.payload["amount"]) != inv.amount:
            return cancel("invoice amount changed since the plan")
        if res.category is not Category.UNRECORDED_PAYMENT or res.action is not Action.AUTO_APPLY:
            return cancel(f"engine now says {res.category.value}/{res.action.value}: {res.note or 'not auto-applicable'}")
        if not planned <= set(res.txn_ids) or res.success_total != inv.amount:
            return cancel("provider transactions changed since the plan")
        for tid in planned:
            owner = self.books.paid_by(tid)
            if owner is not None:
                return cancel(f"transaction {tid} already applied to {owner}")
        if self.config.max_auto_apply_amount is not None and inv.amount > self.config.max_auto_apply_amount and not a.requires_approval:
            return cancel("amount above the unattended limit")
        return ALLOW

    def _apply_linked(self, a: RecoveryAction) -> GuardResult:
        if self.books.is_applied(a.idempotency_key):
            return ALLOW
        tid, amount = a.payload["txn_id"], Decimal(a.payload["amount"])
        inv = self.books.get_invoice(a.invoice_id)
        if inv is None:
            return cancel("invoice not found")
        if inv.internal_status is InternalStatus.PAID:
            return cancel("invoice already paid")
        t = self.ledger.get_txn(tid)
        if t is None or t.status is not TxnStatus.SUCCESS:
            return cancel("transaction is not a confirmed success")
        if t.amount != amount or inv.amount != amount:
            return cancel("amount no longer matches exactly")
        owner = self.books.paid_by(tid)
        if owner is not None:
            return cancel(f"transaction already applied to {owner}")
        # The payment must still be an orphan: if its own reference now resolves to a real invoice, the
        # deterministic path owns it and a human-approved guess must not override that.
        if normalize_reference(t.reference_raw) is not None:
            by_ref = self.books.find_invoice_by_reference(t.reference_raw)
            if by_ref is not None and by_ref.invoice_id != inv.invoice_id:
                return cancel(f"its reference resolves to {by_ref.invoice_id}")
        _, _, res = self._fresh(a.invoice_id)
        if res is None or res.success_total > 0:
            return cancel("invoice already has confirmed payment(s) linked by reference")
        return ALLOW

    def _retry(self, a: RecoveryAction) -> GuardResult:
        cfg = self.config
        sent_before = self.store.send_count(a.invoice_id)  # this action has not started its side effect yet
        if sent_before >= cfg.max_retries:
            return cancel(f"retry limit reached ({sent_before}/{cfg.max_retries})")
        inv, txns, res = self._fresh(a.invoice_id, retry_count=sent_before)
        if inv is None or res is None:
            return cancel("invoice not found")
        if inv.internal_status is InternalStatus.PAID:
            return cancel("invoice already paid")
        if Decimal(a.payload["amount"]) != inv.amount:
            return cancel("invoice amount changed since the plan")
        if res.category is not Category.FAILED or res.action is not Action.RETRY_ELIGIBLE:
            return cancel(f"engine now says {res.category.value}/{res.action.value}, not a retryable failure")
        latest = max((t for t in txns if t.status is TxnStatus.FAILED), key=lambda t: (t.timestamp, t.provider_txn_id))
        if latest.provider_txn_id != a.payload["retry_of"]:
            return cancel("a newer attempt exists; this plan is stale")
        last = self.store.last_side_effect_at(a.invoice_id, ActionType.RETRY_PUSH)
        if last is not None:
            gap = self.store.now().timestamp() - datetime.fromisoformat(last).timestamp()
            if gap < cfg.retry_cooldown_s:
                return later(f"cooldown: last push {int(gap)}s ago")
        # The live check. The ledger can lag the provider, and a "failed" request can have succeeded (a timeout
        # is not proof of failure). Ask the provider about every non-successful request on this invoice, not just
        # the latest: an older "failure" that actually went through means the customer has already paid.
        asked = sorted({t.provider_txn_id for t in txns if t.status not in (TxnStatus.SUCCESS, TxnStatus.REVERSED)})[-self.MAX_STATUS_CHECKS:]
        if a.payload["retry_of"] not in asked:
            asked.append(a.payload["retry_of"])
        for rid in asked:
            try:
                live = self.push.get_status(rid)
            except ProviderError as e:
                return later(f"could not verify request {rid} with the provider: {e}")
            except Exception as e:  # network, auth, anything: unverified means no
                return later(f"status check failed for {rid}: {type(e).__name__}")
            if live is TxnStatus.SUCCESS:
                return cancel(f"provider says request {rid} succeeded; never re-charge")
            if live is TxnStatus.PENDING:
                return later(f"provider says request {rid} is still pending")
        # Re-read after the live call: a callback may have landed while we were asking.
        _, _, res2 = self._fresh(a.invoice_id, retry_count=sent_before)
        if res2 is None or res2.action is not Action.RETRY_ELIGIBLE:
            return cancel("state changed during the status check")
        return ALLOW

    def _notify(self, a: RecoveryAction) -> GuardResult:
        inv, txns, res = self._fresh(a.invoice_id)
        if inv is None or res is None:
            return cancel("invoice not found")
        if inv.internal_status is InternalStatus.PAID or res.category is not Category.FAILED:
            return cancel("no longer a failed, unpaid invoice")
        return ALLOW

    def _refund(self, a: RecoveryAction) -> GuardResult:
        tid, keep_id, amount = a.payload["refund_txn_id"], a.payload["keep_txn_id"], Decimal(a.payload["amount"])
        if tid in self.store.refunded_txns() and a.side_effect_at is None:
            return cancel("a refund for this transaction was already started")
        inv, txns, res = self._fresh(a.invoice_id)
        if inv is None or res is None:
            return cancel("invoice not found")
        if res.category is not Category.DUPLICATE_PAYMENT:
            return cancel(f"engine now says {res.category.value}, not a duplicate")
        successes = [t for t in txns if t.status is TxnStatus.SUCCESS]
        keep, extras = keeper_and_extras(successes)
        target = next((t for t in extras if t.provider_txn_id == tid), None)
        if target is None or keep.provider_txn_id != keep_id:
            return cancel("the keeper or the refund target changed; propose again")
        if target.amount != amount or target.amount != inv.amount:
            return cancel("amount mismatch")
        if keep.amount != inv.amount:
            return cancel("the payment we would keep does not cover the invoice")
        if self.books.paid_by(tid) not in (None, inv.invoice_id):
            return cancel("this transaction was applied elsewhere")
        return ALLOW
