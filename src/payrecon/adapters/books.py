"""SQL books: the application's own invoices, and the record of which payments paid which invoice."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from ..matcher import normalize_reference
from ..models import InternalStatus, Invoice
from .db import Database, to_dec, to_dt


class BooksConflict(RuntimeError):
    """The books refused a change that would break an invariant. Nothing was written."""


_COLS = "invoice_id, reference, amount, internal_status, created_at, customer_name"


def _invoice(r: tuple) -> Invoice:
    return Invoice(r[0], r[1], to_dec(r[2]), InternalStatus(r[3]), to_dt(r[4]), r[5])


class SqlBooks:
    """Implements recovery.ports.BooksPort."""

    def __init__(self, db: Database):
        self.db = db

    def add_invoice(self, inv: Invoice) -> None:
        key = normalize_reference(inv.reference)
        if key is None or inv.amount <= 0:
            raise ValueError(f"invoice {inv.invoice_id}: empty reference or non-positive amount")
        self.db.execute(
            "INSERT INTO invoices (invoice_id, reference, reference_norm, amount, internal_status, created_at, customer_name) "
            "VALUES (?,?,?,?,?,?,?)",
            (inv.invoice_id, inv.reference, key, inv.amount, inv.internal_status, inv.created_at, inv.customer_name),
        )

    def invoices(self) -> list[Invoice]:
        return [_invoice(r) for r in self.db.execute(f"SELECT {_COLS} FROM invoices ORDER BY invoice_id").fetchall()]

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        r = self.db.execute(f"SELECT {_COLS} FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
        return _invoice(r) if r else None

    def find_invoice_by_reference(self, reference: str | None) -> Invoice | None:
        key = normalize_reference(reference)
        if key is None:
            return None
        r = self.db.execute(f"SELECT {_COLS} FROM invoices WHERE reference_norm = ?", (key,)).fetchone()
        return _invoice(r) if r else None

    def paid_by(self, provider_txn_id: str) -> str | None:
        r = self.db.execute("SELECT invoice_id FROM payment_applications WHERE provider_txn_id = ?", (provider_txn_id,)).fetchone()
        return r[0] if r else None

    def is_applied(self, idempotency_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM applied_keys WHERE idempotency_key = ?", (idempotency_key,)).fetchone() is not None

    def mark_paid(self, invoice_id: str, parts: Sequence[tuple[str, Decimal]], idempotency_key: str) -> bool:
        """Atomic and idempotent. The key is inserted first, so of two concurrent callers exactly one proceeds;
        any later failure rolls the key back with everything else."""
        if not parts:
            raise ValueError("nothing to apply")
        now = datetime.now(timezone.utc)
        with self.db.tx():
            claimed = self.db.execute(
                "INSERT INTO applied_keys (idempotency_key, invoice_id, applied_at) VALUES (?,?,?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (idempotency_key, invoice_id, now),
            )
            if claimed.rowcount == 0:
                return False
            inv = self.get_invoice(invoice_id)
            if inv is None:
                raise BooksConflict(f"unknown invoice {invoice_id}")
            if inv.internal_status is not InternalStatus.UNPAID:
                raise BooksConflict(f"invoice {invoice_id} is not unpaid")
            total = sum((to_dec(a) for _, a in parts), Decimal(0))
            if total != inv.amount:
                raise BooksConflict(f"parts total {total}, invoice is {inv.amount}")
            for txn_id, amount in parts:
                owner = self.paid_by(txn_id)
                if owner is not None:
                    raise BooksConflict(f"transaction {txn_id} already applied to {owner}")
                self.db.execute(
                    "INSERT INTO payment_applications (provider_txn_id, idempotency_key, invoice_id, amount, applied_at) VALUES (?,?,?,?,?)",
                    (txn_id, idempotency_key, invoice_id, amount, now),
                )
            changed = self.db.execute(
                "UPDATE invoices SET internal_status = 'PAID' WHERE invoice_id = ? AND internal_status = 'UNPAID'", (invoice_id,)
            )
            if changed.rowcount != 1:
                raise BooksConflict(f"invoice {invoice_id} changed while applying")
        return True
