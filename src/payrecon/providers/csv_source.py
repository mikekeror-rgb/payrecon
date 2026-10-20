"""CSV import and export for transactions and invoices."""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

from ..models import InternalStatus, Invoice, ProviderTxn, TxnStatus
from .base import EAT, mask_msisdn

DEFAULT_COLUMNS = {
    "provider_txn_id": "provider_txn_id",
    "amount": "amount",
    "status": "status",
    "timestamp": "timestamp",
    "reference": "reference",
    "msisdn": "msisdn",
    "result_code": "result_code",
    "result_desc": "result_desc",
    "request_id": "request_id",
}
_REQUIRED = ("provider_txn_id", "amount", "status", "timestamp")
_STATUS_ALIASES = {
    "SUCCESS": TxnStatus.SUCCESS,
    "COMPLETED": TxnStatus.SUCCESS,
    "PENDING": TxnStatus.PENDING,
    "FAILED": TxnStatus.FAILED,
    "REVERSED": TxnStatus.REVERSED,
}
REVERSAL_COLUMNS = ["original_receipt", "reversal_receipt", "amount", "reversed_at"]
INVOICE_COLUMNS = ["invoice_id", "reference", "amount", "internal_status", "created_at", "customer_name"]


class CsvStatementSource:
    """Load transactions from a CSV. Map your export's headers with `columns`."""

    name = "csv_statement"

    def __init__(self, path: str | Path, columns: Mapping[str, str] | None = None, source: str = "statement"):
        self.path = Path(path)
        self.columns = {**DEFAULT_COLUMNS, **(columns or {})}
        self.source = source

    def load(self) -> list[ProviderTxn]:
        with self.path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = set(reader.fieldnames or [])
            missing = [self.columns[k] for k in _REQUIRED if self.columns[k] not in headers]
            if missing:
                raise ValueError(f"{self.path}: missing required columns {missing}")
            return [self._row(n, row) for n, row in enumerate(reader, start=2)]

    def _row(self, line_no: int, row: Mapping[str, str]) -> ProviderTxn:
        col = self.columns
        try:
            amount = Decimal(row[col["amount"]].replace(",", "").strip())
        except InvalidOperation as e:
            raise ValueError(f"{self.path} line {line_no}: bad amount {row[col['amount']]!r}") from e
        status_raw = row[col["status"]].strip().upper()
        if status_raw not in _STATUS_ALIASES:
            raise ValueError(f"{self.path} line {line_no}: unknown status {status_raw!r}")
        ts = datetime.fromisoformat(row[col["timestamp"]].strip())
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=EAT)
        return ProviderTxn(
            provider_txn_id=row[col["provider_txn_id"]].strip(),
            amount=amount,
            status=_STATUS_ALIASES[status_raw],
            timestamp=ts,
            reference_raw=(row.get(col["reference"]) or None),
            msisdn_masked=mask_msisdn(row.get(col["msisdn"])),
            result_code=(row.get(col["result_code"]) or None),
            source=self.source,
            result_desc=(row.get(col["result_desc"]) or None),
            request_id=(row.get(col["request_id"]) or None),
        )


def write_transactions_csv(txns: Iterable[ProviderTxn], path: str | Path) -> None:
    cols = list(DEFAULT_COLUMNS.values())
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in txns:
            w.writerow(
                [
                    t.provider_txn_id,
                    str(t.amount),
                    t.status.value,
                    t.timestamp.isoformat(),
                    t.reference_raw or "",
                    t.msisdn_masked or "",
                    t.result_code or "",
                    t.result_desc or "",
                    t.request_id or "",
                ]
            )


def load_invoices_csv(path: str | Path) -> list[Invoice]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])
        missing = [c for c in INVOICE_COLUMNS[:5] if c not in headers]
        if missing:
            raise ValueError(f"{path}: missing required columns {missing}")
        out: list[Invoice] = []
        for n, row in enumerate(reader, start=2):
            try:
                amount = Decimal(row["amount"].replace(",", "").strip())
            except InvalidOperation as e:
                raise ValueError(f"{path} line {n}: bad amount {row['amount']!r}") from e
            try:
                status = InternalStatus(row["internal_status"].strip().upper())
            except ValueError as e:
                raise ValueError(f"{path} line {n}: unknown internal_status {row['internal_status']!r}") from e
            ts = datetime.fromisoformat(row["created_at"].strip())
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=EAT)
            out.append(
                Invoice(
                    row["invoice_id"].strip(),
                    row["reference"].strip(),
                    amount,
                    status,
                    ts,
                    (row.get("customer_name") or "").strip() or None,
                )
            )
    return out


def write_invoices_csv(invoices: Iterable[Invoice], path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(INVOICE_COLUMNS)
        for inv in invoices:
            w.writerow(
                [
                    inv.invoice_id,
                    inv.reference,
                    str(inv.amount),
                    inv.internal_status.value,
                    inv.created_at.isoformat(),
                    inv.customer_name or "",
                ]
            )


def load_reversals_csv(path: str | Path) -> list[ProviderTxn]:
    """Reversal lines from an M-Pesa statement (or typed from the SMS): which payment went back, under which receipt.

    Each row becomes a REVERSED copy of the original payment, so ingesting it moves that payment to REVERSED
    (the ledger keeps the payment's own amount, time and reference). Columns: original_receipt, reversal_receipt,
    amount, reversed_at.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REVERSAL_COLUMNS if c not in set(reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required columns {missing}")
        out: list[ProviderTxn] = []
        for n, row in enumerate(reader, start=2):
            try:
                amount = Decimal(row["amount"].replace(",", "").strip())
            except InvalidOperation as e:
                raise ValueError(f"{path} line {n}: bad amount {row['amount']!r}") from e
            ts = datetime.fromisoformat(row["reversed_at"].strip())
            ts = ts if ts.tzinfo else ts.replace(tzinfo=EAT)
            rev = row["reversal_receipt"].strip()
            out.append(ProviderTxn(
                provider_txn_id=row["original_receipt"].strip(), amount=amount, status=TxnStatus.REVERSED, timestamp=ts,
                source="statement_reversal", result_desc=f"reversed under {rev} at {ts.isoformat()} (from the statement)",
            ))
        return out
