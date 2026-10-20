"""What the executor needs from the outside world. Real adapters (Postgres books, Daraja gateway, SMS)
implement these; `sim.py` implements them in memory for tests and the demo.

Phone numbers never cross these interfaces: a retry names the original request and the gateway looks the
number up in its own store.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol, Sequence

from ..models import Invoice, ProviderTxn, TxnStatus


class NotSentError(Exception):
    """Raise from a side-effecting port call only when the request provably never left (connection refused,
    DNS failure, auth rejected before sending). Anything ambiguous must raise some other exception, which the
    executor treats as 'may have happened'."""


class SendOutcomeUnknown(Exception):
    """A side-effecting call may or may not have taken effect (timeout after sending, 5xx, connection reset,
    a crash while recording the result). The executor turns this into UNKNOWN for a person to check."""


class BooksPort(Protocol):
    """Our own records. Reads must be fresh (no cache across a run)."""

    def get_invoice(self, invoice_id: str) -> Invoice | None: ...

    def find_invoice_by_reference(self, reference: str | None) -> Invoice | None:
        """Resolve a payer-typed reference the way the engine does (normalized, exact)."""

    def paid_by(self, provider_txn_id: str) -> str | None:
        """Invoice this provider transaction has already been applied to, if any."""

    def is_applied(self, idempotency_key: str) -> bool: ...

    def mark_paid(self, invoice_id: str, parts: Sequence[tuple[str, Decimal]], idempotency_key: str) -> bool:
        """Record every (provider_txn_id, amount) in `parts` against the invoice and mark it paid, atomically.
        The parts must sum to the invoice amount and none may already be applied. Idempotent on the key:
        returns False if the key was already applied."""


class LedgerPort(Protocol):
    """Provider transactions as we have received them (callbacks, statements). Fresh reads."""

    def txns_for_reference(self, reference: str) -> list[ProviderTxn]: ...

    def get_txn(self, provider_txn_id: str) -> ProviderTxn | None: ...


class PushPort(Protocol):
    def get_status(self, request_id: str) -> TxnStatus:
        """Live provider query. Raises on anything it cannot interpret; never guesses."""

    def send_push(self, *, retry_of: str, amount: Decimal, idempotency_key: str) -> str:
        """Start a new STK push for the same payer as request `retry_of`. Returns the new request id.
        The implementation must register the request (so its callback can be linked) before returning.
        Daraja has no idempotency key of its own, which is why the executor treats this as at-most-once."""


class RefundPort(Protocol):
    def reverse(self, provider_txn_id: str, amount: Decimal, idempotency_key: str) -> str: ...


class NotifyPort(Protocol):
    def notify(self, invoice_id: str, template: str, idempotency_key: str) -> None: ...
