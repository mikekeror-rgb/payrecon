"""Core data model.

Money is Decimal everywhere. Phone numbers are masked at the ingestion edge and
never stored in full.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class TxnStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PENDING = "PENDING"
    FAILED = "FAILED"
    # The money went back to the payer after a success: by a payrecon refund, by the provider, or by hand. Outranks
    # SUCCESS (nothing can undo it) and never counts as money received. Seen live: the Daraja sandbox reversed
    # real-money test payments on its own, and nothing told the system.
    REVERSED = "REVERSED"


class InternalStatus(str, Enum):
    """What our own books say about an invoice."""

    PAID = "PAID"
    UNPAID = "UNPAID"


class Category(str, Enum):
    """What reconciliation concluded about an invoice (or an unlinked transaction)."""

    MATCHED = "MATCHED"  # provider success covers the invoice and our books agree
    UNRECORDED_PAYMENT = "UNRECORDED_PAYMENT"  # provider success, but our books say UNPAID
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"  # two or more distinct successes, each the full amount
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"  # success total is over or under the invoice amount
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"  # part succeeded, the rest failed or is still pending
    PENDING = "PENDING"  # only pending transactions so far
    FAILED = "FAILED"  # only failed transactions
    MISSING_PAYMENT = "MISSING_PAYMENT"  # no transaction references this invoice
    REVERSED_PAYMENT = "REVERSED_PAYMENT"  # the only payments received were reversed: nothing is held for it
    ORPHANED_TXN = "ORPHANED_TXN"  # transaction that links to no invoice


class Action(str, Enum):
    """What the policy layer proposes. Only AUTO_APPLY is ever applied without a human."""

    NONE = "NONE"
    AUTO_APPLY = "AUTO_APPLY"  # mark invoice paid; provider-confirmed, exact amount
    RECHECK_STATUS = "RECHECK_STATUS"  # query the provider again; never charge again
    RETRY_ELIGIBLE = "RETRY_ELIGIBLE"  # may retry, after a status recheck and with an idempotency key
    NOTIFY_CUSTOMER = "NOTIFY_CUSTOMER"  # customer must act (top up, re-enter PIN)
    AWAIT_PAYMENT = "AWAIT_PAYMENT"  # nothing received; normal unpaid invoice
    ESCALATE_HUMAN = "ESCALATE_HUMAN"  # ambiguous or high impact: approval queue
    LINK_OR_ESCALATE = "LINK_OR_ESCALATE"  # money received but unlinked: find the invoice or escalate


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    reference: str  # customer-facing reference, e.g. "INV-0042"
    amount: Decimal
    internal_status: InternalStatus
    created_at: datetime
    customer_name: str | None = None  # PII: synthetic only in this repo


@dataclass(frozen=True)
class ProviderTxn:
    provider_txn_id: str  # M-Pesa receipt number, or CheckoutRequestID for failed pushes
    amount: Decimal
    status: TxnStatus
    timestamp: datetime
    reference_raw: str | None = None  # what the payer typed / what we sent; may be messy
    msisdn_masked: str | None = None
    result_code: str | None = None
    source: str = "unknown"  # stk_callback, c2b_confirmation, statement, ...
    result_desc: str | None = None  # provider's free-text failure message
    request_id: str | None = None  # CheckoutRequestID of the push this settles; lets a success fold in its pending row


@dataclass
class InvoiceResult:
    invoice_id: str
    category: Category
    action: Action
    txn_ids: list[str]
    success_total: Decimal
    note: str = ""


@dataclass
class TxnResult:
    provider_txn_id: str
    category: Category
    action: Action
    status: TxnStatus
    amount: Decimal


@dataclass
class ReconciliationReport:
    invoices: dict[str, InvoiceResult]
    orphans: dict[str, TxnResult]
    warnings: list[str] = field(default_factory=list)

    def category_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in list(self.invoices.values()) + list(self.orphans.values()):
            out[r.category.value] = out.get(r.category.value, 0) + 1
        return out
