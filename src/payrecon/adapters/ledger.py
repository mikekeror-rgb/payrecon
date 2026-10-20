"""SQL ledger: provider transactions as received (callbacks, statements, pushes we sent).

Ingestion is idempotent. A row only moves forward (PENDING < FAILED < SUCCESS); a redelivered or lagging copy
cannot downgrade it. A copy that disagrees on the amount is reported, never silently merged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..matcher import normalize_reference
from ..models import ProviderTxn, TxnStatus
from .db import Database, to_dec, to_dt

_RANK = {"PENDING": 0, "FAILED": 1, "SUCCESS": 2, "REVERSED": 3}
_COLS = "provider_txn_id, amount, status, occurred_at, reference_raw, msisdn_masked, result_code, source, result_desc, request_id"


def _txn(r: tuple) -> ProviderTxn:
    return ProviderTxn(
        provider_txn_id=r[0], amount=to_dec(r[1]), status=TxnStatus(r[2]), timestamp=to_dt(r[3]), reference_raw=r[4],
        msisdn_masked=r[5], result_code=r[6], source=r[7], result_desc=r[8], request_id=r[9],
    )


class SqlLedger:
    """Implements recovery.ports.LedgerPort, plus ingestion."""

    def __init__(self, db: Database, provider: str = "mpesa_daraja"):
        self.db, self.provider = db, provider

    def ingest(self, t: ProviderTxn) -> str:
        """Returns 'inserted', 'upgraded', 'duplicate' or 'conflict' (same id, different amount; kept as first seen)."""
        if t.amount <= 0:
            raise ValueError(f"{t.provider_txn_id}: non-positive amount")
        norm = normalize_reference(t.reference_raw)
        with self.db.tx():
            ins = self.db.execute(
                "INSERT INTO provider_transactions (provider_txn_id, provider, amount, status, reference_raw, reference_norm, "
                "msisdn_masked, result_code, result_desc, request_id, occurred_at, source, received_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (provider_txn_id) DO NOTHING",
                (t.provider_txn_id, self.provider, t.amount, t.status, t.reference_raw, norm, t.msisdn_masked, t.result_code,
                 t.result_desc, t.request_id, t.timestamp, t.source, datetime.now(timezone.utc)),
            )
            if ins.rowcount == 1:
                return "inserted"
            old_status, old_amount = self.db.execute(
                "SELECT status, amount FROM provider_transactions WHERE provider_txn_id = ?", (t.provider_txn_id,)
            ).fetchone()
            if t.status is TxnStatus.REVERSED and _RANK[old_status] < _RANK["REVERSED"]:
                # A reversal changes the status and says by whom; the payment's own amount, time and reference stay.
                if to_dec(old_amount) != t.amount:
                    return "conflict"  # a partial reversal is not the whole payment going back: a person looks
                self.db.execute(
                    "UPDATE provider_transactions SET status = 'REVERSED', result_desc = ?, source = ? WHERE provider_txn_id = ?",
                    (t.result_desc, t.source, t.provider_txn_id),
                )
                return "upgraded"
            if _RANK[t.status.value] > _RANK[old_status]:
                self.db.execute(
                    "UPDATE provider_transactions SET status = ?, amount = ?, result_code = ?, result_desc = ?, occurred_at = ?, source = ?, "
                    "request_id = COALESCE(?, request_id), reference_raw = COALESCE(reference_raw, ?), "
                    "reference_norm = COALESCE(reference_norm, ?), msisdn_masked = COALESCE(msisdn_masked, ?) WHERE provider_txn_id = ?",
                    (t.status, t.amount, t.result_code, t.result_desc, t.timestamp, t.source, t.request_id, t.reference_raw, norm,
                     t.msisdn_masked, t.provider_txn_id),
                )
                return "upgraded"
            # same or lower rank: only fill gaps, never overwrite
            self.db.execute(
                "UPDATE provider_transactions SET request_id = COALESCE(request_id, ?), reference_raw = COALESCE(reference_raw, ?), "
                "reference_norm = COALESCE(reference_norm, ?) WHERE provider_txn_id = ?",
                (t.request_id, t.reference_raw, norm, t.provider_txn_id),
            )
            return "conflict" if to_dec(old_amount) != t.amount else "duplicate"

    def ingest_many(self, txns: Iterable[ProviderTxn]) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in txns:
            r = self.ingest(t)
            out[r] = out.get(r, 0) + 1
        return out

    def txns_for_reference(self, reference: str) -> list[ProviderTxn]:
        key = normalize_reference(reference)
        if key is None:
            return []
        rows = self.db.execute(f"SELECT {_COLS} FROM provider_transactions WHERE reference_norm = ? ORDER BY occurred_at, provider_txn_id", (key,))
        return [_txn(r) for r in rows.fetchall()]

    def get_txn(self, provider_txn_id: str) -> ProviderTxn | None:
        r = self.db.execute(f"SELECT {_COLS} FROM provider_transactions WHERE provider_txn_id = ?", (provider_txn_id,)).fetchone()
        return _txn(r) if r else None

    def all_txns(self) -> list[ProviderTxn]:
        return [_txn(r) for r in self.db.execute(f"SELECT {_COLS} FROM provider_transactions ORDER BY occurred_at, provider_txn_id").fetchall()]
