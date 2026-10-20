"""Deterministic reconciliation engine.

Matches provider transactions to invoices by normalized reference, then checks
amount and status. Anything it cannot link is surfaced as an orphan rather than
guessed at: that remainder is where a fuzzy / AI matcher plugs in.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from typing import Iterable, Mapping

from .models import (
    Category,
    InternalStatus,
    Invoice,
    InvoiceResult,
    ProviderTxn,
    ReconciliationReport,
    TxnResult,
    TxnStatus,
)
from .policy import decide_action, decide_orphan_action

_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
_STATUS_RANK = {TxnStatus.PENDING: 0, TxnStatus.FAILED: 1, TxnStatus.SUCCESS: 2, TxnStatus.REVERSED: 3}


def normalize_reference(raw: str | None) -> str | None:
    """Canonical form of a payer-typed reference.

    "INV-0042", "inv 42", " Inv0042 " all become "INV42". Pure digits lose leading
    zeros. Anything else is only upper-cased and stripped, so it will not collide
    with a real invoice. Returns None for blank input.
    """
    if raw is None:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not cleaned:
        return None
    m = _REF_RE.match(cleaned)
    if m:
        return f"{m.group(1)}{int(m.group(2))}"
    if cleaned.isdigit():
        return str(int(cleaned))
    return cleaned


def _has_ref(t: ProviderTxn) -> bool:
    return normalize_reference(t.reference_raw) is not None


def _merge(current: ProviderTxn, incoming: ProviderTxn) -> ProviderTxn:
    winner, other = (incoming, current) if _STATUS_RANK[incoming.status] > _STATUS_RANK[current.status] else (current, incoming)
    if not _has_ref(winner) and _has_ref(other):
        winner = replace(winner, reference_raw=other.reference_raw)
    return winner


def dedupe_txns(txns: Iterable[ProviderTxn]) -> tuple[list[ProviderTxn], list[str]]:
    """Collapse copies of the same provider transaction.

    A redelivered callback or a statement line for a payment we already saw is the
    same transaction, not a second payment. Terminal status beats PENDING, and a
    copy that carries a reference fills in one that lacks it.
    """
    best: dict[str, ProviderTxn] = {}
    warnings: list[str] = []
    for t in txns:
        cur = best.get(t.provider_txn_id)
        if cur is None:
            best[t.provider_txn_id] = t
            continue
        if {cur.status, t.status} == {TxnStatus.SUCCESS, TxnStatus.FAILED}:
            warnings.append(f"{t.provider_txn_id}: conflicting SUCCESS and FAILED reports; kept SUCCESS, needs review")
        if cur.amount != t.amount:
            warnings.append(f"{t.provider_txn_id}: amount differs between copies ({cur.amount} vs {t.amount})")
        best[t.provider_txn_id] = _merge(cur, t)
    return _fold_requests(best, warnings), warnings


def _fold_requests(best: dict[str, ProviderTxn], warnings: list[str]) -> list[ProviderTxn]:
    """Fold a push's own PENDING/FAILED row into the callback that settles it.

    A STK push is first known by its CheckoutRequestID; the success callback arrives
    under a different id (the receipt number) and names the request it settles. Left
    alone, the stale PENDING row would block AUTO_APPLY forever. The higher-ranked
    status wins; the receipt number stays the transaction's identity.
    """
    dropped: set[str] = set()
    for tid in list(best):
        t = best.get(tid)
        if t is None or t.request_id is None or t.request_id == tid:
            continue
        other = best.get(t.request_id)
        if other is None or other.provider_txn_id in dropped:
            continue
        if {t.status, other.status} == {TxnStatus.SUCCESS, TxnStatus.FAILED}:
            warnings.append(f"{t.provider_txn_id}: request {t.request_id} reported both SUCCESS and FAILED; kept SUCCESS, needs review")
        if t.status is not TxnStatus.PENDING and other.status is not TxnStatus.PENDING and t.amount != other.amount:
            warnings.append(f"{t.provider_txn_id}: amount differs from its request row ({t.amount} vs {other.amount})")
        if _STATUS_RANK[other.status] > _STATUS_RANK[t.status]:
            winner, loser = other, t
        else:
            winner, loser = t, other
        if not _has_ref(winner) and _has_ref(loser):
            winner = replace(winner, reference_raw=loser.reference_raw)
        best[winner.provider_txn_id] = winner
        del best[loser.provider_txn_id]
        dropped.add(loser.provider_txn_id)
    return list(best.values())


def _classify(inv: Invoice, txns: list[ProviderTxn]) -> tuple[Category, str, Decimal]:
    reversed_ = [t for t in txns if t.status is TxnStatus.REVERSED]
    live = [t for t in txns if t.status is not TxnStatus.REVERSED]
    if reversed_ and not live:
        ids = ", ".join(t.provider_txn_id for t in reversed_)
        return Category.REVERSED_PAYMENT, f"payment {ids} was reversed; nothing is held for this invoice", Decimal(0)
    cat, note, total = _classify_live(inv, live)
    if reversed_:
        note = (note + "; " if note else "") + "also reversed: " + ", ".join(t.provider_txn_id for t in reversed_)
    return cat, note, total


def _classify_live(inv: Invoice, txns: list[ProviderTxn]) -> tuple[Category, str, Decimal]:
    successes = [t for t in txns if t.status is TxnStatus.SUCCESS]
    pendings = [t for t in txns if t.status is TxnStatus.PENDING]
    faileds = [t for t in txns if t.status is TxnStatus.FAILED]
    total = sum((t.amount for t in successes), Decimal(0))

    if not txns:
        return Category.MISSING_PAYMENT, "no provider transaction references this invoice", total
    if successes:
        if total == inv.amount:
            if inv.internal_status is InternalStatus.PAID:
                return Category.MATCHED, "", total
            return Category.UNRECORDED_PAYMENT, "provider confirms payment; internal record says unpaid", total
        if len(successes) >= 2 and all(t.amount == inv.amount for t in successes):
            return Category.DUPLICATE_PAYMENT, f"{len(successes)} successful payments of the full amount", total
        if total < inv.amount and (pendings or faileds):
            return Category.PARTIAL_PAYMENT, f"received {total} of {inv.amount}; remainder failed or pending", total
        return Category.AMOUNT_MISMATCH, f"received {total}, invoice is {inv.amount}", total
    if pendings:
        return Category.PENDING, "provider has not confirmed yet", total
    return Category.FAILED, "all attempts failed", total


def reconcile(
    invoices: Iterable[Invoice],
    txns: Iterable[ProviderTxn],
    retry_counts: Mapping[str, int] | None = None,
    applied: Mapping[str, str] | None = None,
) -> ReconciliationReport:
    """`applied` maps provider_txn_id -> invoice_id for payments already applied to an invoice in the books (an
    approved inferred link, say). Such a payment counts for that invoice even when its reference does not match it."""
    invoices = list(invoices)
    applied = applied or {}
    retry_counts = retry_counts or {}
    unique, warnings = dedupe_txns(txns)

    index: dict[str, Invoice] = {}
    for inv in invoices:
        key = normalize_reference(inv.reference)
        if key is None:
            raise ValueError(f"invoice {inv.invoice_id} has an empty reference")
        if key in index:
            raise ValueError(
                f"invoices {index[key].invoice_id} and {inv.invoice_id} share normalized reference {key!r}"
            )
        index[key] = inv

    linked: dict[str, list[ProviderTxn]] = defaultdict(list)
    unlinked: list[ProviderTxn] = []
    by_id = {inv.invoice_id: inv for inv in invoices}
    for t in unique:
        key = normalize_reference(t.reference_raw)
        owner = by_id.get(applied.get(t.provider_txn_id, "")) or (index.get(key) if key is not None else None)
        if owner is None:
            unlinked.append(t)
        else:
            linked[owner.invoice_id].append(t)

    results: dict[str, InvoiceResult] = {}
    for inv in invoices:
        mine = linked.get(inv.invoice_id, [])
        category, note, total = _classify(inv, mine)
        results[inv.invoice_id] = InvoiceResult(
            invoice_id=inv.invoice_id,
            category=category,
            action=decide_action(category, mine, retry_counts.get(inv.invoice_id, 0)),
            txn_ids=[t.provider_txn_id for t in mine],
            success_total=total,
            note=note,
        )

    orphans = {
        t.provider_txn_id: TxnResult(
            provider_txn_id=t.provider_txn_id,
            category=Category.ORPHANED_TXN,
            action=decide_orphan_action(t),
            status=t.status,
            amount=t.amount,
        )
        for t in unlinked
    }
    return ReconciliationReport(invoices=results, orphans=orphans, warnings=warnings)
