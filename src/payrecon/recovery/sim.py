"""Simulated sandbox: in-memory books, ledger, provider and gateway with hidden ground truth and fault injection.

The world knows what really happened (who paid, which request really succeeded). The code under test only
sees what the ports show it, which can be stale, missing or wrong. Invariants are checked against the truth,
not against what the code believes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..matcher import normalize_reference
from ..models import InternalStatus, Invoice, ProviderTxn, TxnStatus
from ..providers.base import EAT, ProviderError
from ..synthetic import Dataset
from .ports import NotSentError


class SimulatedCrash(BaseException):
    """The process died. BaseException so no `except Exception` in the code under test can swallow it."""


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


@dataclass
class Faults:
    status_error_rate: float = 0.0  # live status query raises
    send_error_before_rate: float = 0.0  # push call fails, nothing sent
    send_error_after_rate: float = 0.0  # push sent, then the call raises (ambiguous)
    lose_callback_rate: float = 0.0  # success happened; the callback never reaches the ledger
    false_failure_rate: float = 0.0  # ledger says FAILED(1037) but the customer's payment actually went through
    crash_rate: float = 0.0  # SimulatedCrash at a crash point
    drift_rate: float = 0.0  # the world changes between planning and execution
    seed: int = 0

    def __post_init__(self):
        self.rng = random.Random(self.seed)

    def hit(self, p: float) -> bool:
        return p > 0 and self.rng.random() < p


@dataclass
class Charge:
    invoice_id: str | None
    amount: Decimal
    receipt: str
    source: str  # "customer" (pre-existing) | "executor_push"


class World:
    def __init__(self, ds: Dataset, clock: Clock, faults: Faults):
        self.clock, self.faults = clock, faults
        self.invoices: dict[str, Invoice] = {i.invoice_id: i for i in ds.invoices}
        self._ref_index = {normalize_reference(i.reference): i.invoice_id for i in ds.invoices}
        self.ledger: dict[str, ProviderTxn] = {}
        self.charges: list[Charge] = []
        self.request_truth: dict[str, str] = {}  # request id -> SUCCESS | FAILED | PENDING (hidden truth)
        self.request_invoice: dict[str, str] = {}
        self.request_revealed: dict[str, bool] = {}
        self.sends: list[dict] = []  # every provider push call that reached the provider
        self.send_calls: dict[str, int] = {}  # idempotency key -> times the provider was asked
        self.refunds: list[dict] = []
        self.refund_calls: dict[str, int] = {}
        self.notices: list[tuple[str, str]] = []
        self.applied: dict[str, tuple[str, str, Decimal]] = {}  # key -> invoice, txn, amount
        self.paid_txn: dict[str, str] = {}
        self.violations: list[str] = []
        self._request_counter = 0
        self.scripted_outcomes: list[str] = []  # tests: what the next pushes' customers do
        self._seed(ds)

    # ---- construction -------------------------------------------------------------

    def _truth_invoice(self, t: ProviderTxn, ds: Dataset) -> str | None:
        if t.provider_txn_id in ds.link_truth:
            return ds.link_truth[t.provider_txn_id]
        if t.provider_txn_id in ds.unlinkable_txn_ids:
            return None
        return self._ref_index.get(normalize_reference(t.reference_raw))

    def _seed(self, ds: Dataset) -> None:
        rng = self.faults.rng
        for t in ds.txns:
            self.ledger[t.provider_txn_id] = t
            inv = self._truth_invoice(t, ds)
            if t.status is TxnStatus.SUCCESS:
                self.charges.append(Charge(inv, t.amount, t.provider_txn_id, "customer"))
            elif t.status is TxnStatus.FAILED:
                self.request_truth[t.provider_txn_id] = "FAILED"
                if inv:
                    self.request_invoice[t.provider_txn_id] = inv
                if t.result_code in ("1037", "1001") and self.faults.hit(self.faults.false_failure_rate):
                    self.request_truth[t.provider_txn_id] = "SUCCESS"  # reveal: ledger lies, provider knows
                    self.request_revealed[t.provider_txn_id] = True
                    self.charges.append(Charge(inv, t.amount, f"HIDDEN-{t.provider_txn_id}", "customer"))
            else:  # PENDING: hidden outcome
                inv_id = inv
                outcome = rng.choices(["SUCCESS", "FAILED", "PENDING"], [0.45, 0.30, 0.25])[0]
                self.request_truth[t.provider_txn_id] = outcome
                if inv_id:
                    self.request_invoice[t.provider_txn_id] = inv_id
                self.request_revealed[t.provider_txn_id] = False
                if outcome == "SUCCESS":
                    self.charges.append(Charge(inv_id, t.amount, f"RCPT-{t.provider_txn_id}", "customer"))

    def next_request_id(self) -> str:
        """Provider-side ids are unique across every client instance, including one started after a crash."""
        self._request_counter += 1
        return f"ws_CO_RETRY_{self._request_counter:05d}"

    # ---- truth queries ------------------------------------------------------------

    def truly_paid(self, invoice_id: str) -> bool:
        return any(c.invoice_id == invoice_id for c in self.charges)

    # ---- time passes: provider resolves requests and delivers callbacks ------------

    def advance_provider(self) -> None:
        now = self.clock()
        for rid, truth in list(self.request_truth.items()):
            if self.request_revealed.get(rid, True) or truth == "PENDING":
                continue
            old = self.ledger.get(rid)
            if old is None or old.status is not TxnStatus.PENDING:
                self.request_revealed[rid] = True
                continue
            self.request_revealed[rid] = True
            if truth == "SUCCESS":
                if self.faults.hit(self.faults.lose_callback_rate):
                    continue  # callback lost: ledger stays PENDING forever
                self.ledger[f"RCPT-{rid}"] = ProviderTxn(f"RCPT-{rid}", old.amount, TxnStatus.SUCCESS, now,
                                                          old.reference_raw, old.msisdn_masked, "0", "stk_callback", request_id=rid)
            else:
                self.ledger[rid] = replace(old, status=TxnStatus.FAILED, result_code="1037", timestamp=now, result_desc="DS timeout")

    def drift(self, invoice_ids: list[str]) -> list[str]:
        """Between planning and execution, the world moves for some invoices. Returns what was done."""
        log, now = [], self.clock()
        for iid in invoice_ids:
            if not self.faults.hit(self.faults.drift_rate):
                continue
            inv = self.invoices[iid]
            kind = self.faults.rng.choice(["customer_pays_paybill", "books_paid_elsewhere"])
            if kind == "customer_pays_paybill" and not self.truly_paid(iid):
                tid = f"C2B-{iid}"
                self.ledger[tid] = ProviderTxn(tid, inv.amount, TxnStatus.SUCCESS, now, inv.reference, None, "0", "c2b_confirmation")
                self.charges.append(Charge(iid, inv.amount, tid, "customer"))
                log.append(f"{iid}: customer paid by paybill after the plan")
            elif kind == "books_paid_elsewhere":
                self.invoices[iid] = replace(inv, internal_status=InternalStatus.PAID)
                log.append(f"{iid}: books marked paid by someone else after the plan")
        return log

    # ---- crash points ---------------------------------------------------------------

    def crash_point(self, name: str) -> None:
        if self.faults.hit(self.faults.crash_rate):
            raise SimulatedCrash(name)


class SimBooks:
    def __init__(self, w: World):
        self.w = w

    def get_invoice(self, invoice_id):
        return self.w.invoices.get(invoice_id)

    def find_invoice_by_reference(self, reference):
        iid = self.w._ref_index.get(normalize_reference(reference))
        return self.w.invoices.get(iid) if iid else None

    def paid_by(self, provider_txn_id):
        return self.w.paid_txn.get(provider_txn_id)

    def is_applied(self, idempotency_key):
        return idempotency_key in self.w.applied

    def mark_paid(self, invoice_id, parts, idempotency_key):
        w = self.w
        if idempotency_key in w.applied:
            return False
        w.crash_point("before_apply")
        inv = w.invoices[invoice_id]
        # Truth checks: these record a violation if the code under test asked for something wrong.
        total = Decimal(0)
        for provider_txn_id, amount in parts:
            total += amount
            charge = next((c for c in w.charges if c.receipt == provider_txn_id), None)
            if charge is None:
                w.violations.append(f"apply: {provider_txn_id} is not a real successful payment")
            elif charge.invoice_id != invoice_id:
                w.violations.append(f"apply: {provider_txn_id} really pays {charge.invoice_id}, applied to {invoice_id}")
            elif charge.amount != amount:
                w.violations.append(f"apply: {provider_txn_id} really moved {charge.amount}, recorded as {amount}")
            if provider_txn_id in w.paid_txn:
                w.violations.append(f"apply: {provider_txn_id} applied twice (to {w.paid_txn[provider_txn_id]} and {invoice_id})")
        if total != inv.amount:
            w.violations.append(f"apply: parts total {total} != invoice {inv.amount}")
        if inv.internal_status is InternalStatus.PAID:
            w.violations.append(f"apply: {invoice_id} was already paid")
        w.invoices[invoice_id] = replace(inv, internal_status=InternalStatus.PAID)
        w.applied[idempotency_key] = (invoice_id, tuple(t for t, _ in parts), total)
        for provider_txn_id, _ in parts:
            w.paid_txn[provider_txn_id] = invoice_id
        w.crash_point("after_apply")
        return True


class SimLedger:
    def __init__(self, w: World):
        self.w = w

    def txns_for_reference(self, reference):
        key = normalize_reference(reference)
        return [t for t in self.w.ledger.values() if normalize_reference(t.reference_raw) == key]

    def get_txn(self, provider_txn_id):
        return self.w.ledger.get(provider_txn_id)


class SimPush:
    def __init__(self, w: World):
        self.w = w

    def get_status(self, request_id):
        w = self.w
        if w.faults.hit(w.faults.status_error_rate):
            raise ProviderError("sandbox: status query unavailable")
        truth = w.request_truth.get(request_id)
        if truth is None:
            raise ProviderError(f"unknown request {request_id}")
        if truth == "SUCCESS":
            return TxnStatus.SUCCESS
        if truth == "FAILED":
            return TxnStatus.FAILED
        return TxnStatus.PENDING

    def send_push(self, *, retry_of, amount, idempotency_key):
        w = self.w
        w.crash_point("before_send")
        if w.faults.hit(w.faults.send_error_before_rate):
            raise NotSentError("sandbox: gateway unreachable (connection refused)")
        orig = w.ledger[retry_of]
        iid = w.request_invoice.get(retry_of) or w._ref_index.get(normalize_reference(orig.reference_raw))
        # ---- the provider is now asked; everything below is the real world ----
        w.send_calls[idempotency_key] = w.send_calls.get(idempotency_key, 0) + 1
        if iid and w.truly_paid(iid):
            w.violations.append(f"push: sent to {iid} although the customer had already paid")
        rid = w.next_request_id()
        w.sends.append({"key": idempotency_key, "invoice": iid, "request": rid})
        outcome = w.scripted_outcomes.pop(0) if w.scripted_outcomes else w.faults.rng.choices(["SUCCESS", "FAILED", "PENDING"], [0.55, 0.35, 0.10])[0]
        w.request_truth[rid], w.request_revealed[rid] = outcome, False
        if iid:
            w.request_invoice[rid] = iid
        if outcome == "SUCCESS":
            w.charges.append(Charge(iid, amount, f"RCPT-{rid}", "executor_push"))
        w.ledger[rid] = ProviderTxn(rid, amount, TxnStatus.PENDING, w.clock(), orig.reference_raw, orig.msisdn_masked, None, "stk_push")
        w.crash_point("after_send")
        if w.faults.hit(w.faults.send_error_after_rate):
            raise ConnectionError("sandbox: connection dropped after the push was accepted")
        return rid


class SimRefunds:
    def __init__(self, w: World):
        self.w = w

    def reverse(self, provider_txn_id, amount, idempotency_key):
        w = self.w
        w.crash_point("before_refund")
        w.refund_calls[idempotency_key] = w.refund_calls.get(idempotency_key, 0) + 1
        charge = next((c for c in w.charges if c.receipt == provider_txn_id), None)
        if charge is None or amount != charge.amount:
            w.violations.append(f"refund: {provider_txn_id} for {amount} does not match a real charge")
        elif sum(1 for c in w.charges if c.invoice_id == charge.invoice_id) < 2:
            w.violations.append(f"refund: {provider_txn_id} is the only payment for {charge.invoice_id}")
        if any(r["txn"] == provider_txn_id for r in w.refunds):
            w.violations.append(f"refund: {provider_txn_id} refunded twice")
        w.refunds.append({"txn": provider_txn_id, "amount": amount, "key": idempotency_key})
        w.crash_point("after_refund")
        return f"REV-{len(w.refunds)}"


class SimNotifier:
    def __init__(self, w: World):
        self.w = w

    def notify(self, invoice_id, template, idempotency_key):
        self.w.notices.append((invoice_id, template))
