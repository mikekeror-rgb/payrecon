"""Payroll: pay a list of people by M-Pesa (B2C) in one approved batch, and never pay anyone twice.

The weekly job this replaces: open the M-Pesa portal and send money to 30 workers one by one. Here a person
uploads the list, checks it, approves the whole batch once, and the batch is sent; every line ends PAID, FAILED
(M-Pesa said no, nothing moved) or UNKNOWN (it may have gone: a person checks, it is never resent by itself).

The rules, each enforced here and covered by tests:

* Nothing is sent before a person approves the batch. Machine identities (system, ai-*) can never approve.
* The whole file is checked before anything is stored: bad numbers, the same number twice, amounts that are not
  whole shillings or that exceed the limits. One bad line stops the batch, so nobody is half-paid by accident.
* One payout per person per batch (a UNIQUE constraint), claimed with a compare-and-swap before the request goes
  out (QUEUED -> SENDING, write-ahead). Two senders, a double click or a crash cannot send the same line twice.
* "Not sent" (no token, rejected before M-Pesa took it) puts the line back in the queue. "May have been sent"
  (timeout, 5xx, a crash while SENDING) makes it UNKNOWN: it is never resent automatically. A result callback that
  arrives later still settles it.
* Daraja's result is the only thing that makes a line PAID; an accepted request is just ACCEPTED.
* A failed line is resent only when a person requeues it, under a new attempt number.

What has not been checked against live Daraja: B2C uses the same Initiator and SecurityCredential as reversals, and
the sandbox's shared `testapi` credential was locked (8006) when this was built. The request and result shapes come
from Daraja's documentation, and everything here is tested against a local fake.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Protocol

from .adapters.db import Database, to_dec
from .adapters.security import PiiBox, normalize_msisdn
from .providers.base import mask_msisdn
from .recovery.ports import NotSentError, SendOutcomeUnknown

MAX_LINE = Decimal(150_000)  # per person, per batch; change with PayrollService(max_line=...)
MAX_BATCH = Decimal(3_000_000)
STALE_SENDING = timedelta(minutes=10)  # a line left SENDING this long means the sender stopped mid-request
MACHINES = ("system", "link-matcher", "payroll", "dashboard")


class PayrollError(ValueError):
    pass


class PayoutPort(Protocol):
    def send_b2c(self, *, originator_id: str, phone: str, amount: Decimal, remarks: str) -> tuple[str, str]:
        """Ask M-Pesa to pay `phone`. Returns (ConversationID, OriginatorConversationID). Raises NotSentError when the
        request provably did not reach M-Pesa, SendOutcomeUnknown when it may have."""


@dataclass
class Line:
    line_no: int
    name: str
    phone: str  # normalised 2547XXXXXXXX
    amount: Decimal


@dataclass
class Checked:
    lines: list[Line] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0))


_NAME_COLS = ("name", "worker", "employee", "full name")
_PHONE_COLS = ("phone", "msisdn", "number", "phone number", "mpesa number")
_AMOUNT_COLS = ("amount", "pay", "salary", "ksh")


def _col(headers: Iterable[str], options: tuple[str, ...]) -> str | None:
    for h in headers:
        if h and h.strip().lower() in options:
            return h
    return None


def check_csv(text: str, max_line: Decimal = MAX_LINE, max_batch: Decimal = MAX_BATCH) -> Checked:
    """Read a list of name, phone, amount and report every problem, line by line, before anything is stored."""
    out = Checked()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    cn, cp, ca = _col(headers, _NAME_COLS), _col(headers, _PHONE_COLS), _col(headers, _AMOUNT_COLS)
    if not (cn and cp and ca):
        out.errors.append(f"the file needs columns for name, phone and amount (found: {', '.join(h for h in headers if h) or 'none'})")
        return out
    seen: dict[str, int] = {}
    for n, row in enumerate(reader, start=2):
        name, raw_phone, raw_amount = (row.get(cn) or "").strip(), (row.get(cp) or "").strip(), (row.get(ca) or "").strip()
        if not (name or raw_phone or raw_amount):
            continue  # a blank line
        problems = []
        if not name:
            problems.append("no name")
        try:
            phone = normalize_msisdn(raw_phone)
        except ValueError:
            phone = ""
            problems.append(f"'{raw_phone}' is not a Safaricom number")
        try:
            amount = Decimal(raw_amount.replace(",", "").replace("KSh", "").replace("ksh", "").strip())
        except InvalidOperation:
            amount = Decimal(0)
            problems.append(f"'{raw_amount}' is not an amount")
        else:
            if amount <= 0 or amount != amount.to_integral_value():
                problems.append(f"amount {raw_amount} must be a whole number of shillings above zero")
            elif amount > max_line:
                problems.append(f"amount {amount:,.0f} is above the per-person limit of {max_line:,.0f}")
        if phone and phone in seen:
            problems.append(f"same number as line {seen[phone]}")
        if problems:
            out.errors.append(f"line {n} ({name or 'no name'}): " + "; ".join(problems))
            continue
        seen[phone] = n
        out.lines.append(Line(n, name, phone, amount))
    if not out.lines and not out.errors:
        out.errors.append("the file has no payments in it")
    if out.total > max_batch:
        out.errors.append(f"the batch total {out.total:,.0f} is above the limit of {max_batch:,.0f}")
    return out


def _payee_key(phone: str) -> str:
    return hashlib.sha256(f"payee:{phone}".encode()).hexdigest()[:32]


class PayrollService:
    def __init__(self, db: Database, payouts: PayoutPort | None, pii: PiiBox, *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 max_line: Decimal = MAX_LINE, max_batch: Decimal = MAX_BATCH):
        self.db, self.port, self.pii, self.now = db, payouts, pii, now
        self.max_line, self.max_batch = max_line, max_batch

    # ---- creating and approving --------------------------------------------------------------------------

    def create_batch(self, batch_id: str, csv_text: str, created_by: str) -> Checked:
        batch_id = batch_id.strip()
        if not batch_id or len(batch_id) > 40 or any(c in batch_id for c in ":/ "):
            raise PayrollError("give the batch a short name without spaces, ':' or '/', e.g. 2026-W43")
        checked = check_csv(csv_text, self.max_line, self.max_batch)
        if checked.errors:
            return checked  # nothing stored: fix the file and import again
        now = self.now()
        with self.db.tx():
            if self.db.execute("SELECT 1 FROM payout_batches WHERE batch_id = ?", (batch_id,)).fetchone():
                raise PayrollError(f"a batch called {batch_id} already exists; pick another name")
            self.db.execute(
                "INSERT INTO payout_batches (batch_id, status, total, lines, created_by, created_at) VALUES (?,?,?,?,?,?)",
                (batch_id, "DRAFT", checked.total, len(checked.lines), created_by, now))
            for ln in checked.lines:
                self.db.execute(
                    "INSERT INTO payouts (payout_id, batch_id, line_no, name, payee_key, msisdn_enc, msisdn_masked, amount, status, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{batch_id}:{ln.line_no}", batch_id, ln.line_no, ln.name, _payee_key(ln.phone), self.pii.seal(ln.phone),
                     mask_msisdn(ln.phone), ln.amount, "WAITING", now, now))
        return checked

    def approve(self, batch_id: str, user: str, approvers: Mapping[str, Any], note: str = "") -> None:
        self._check_person(user, approvers)
        with self.db.tx():
            row = self.db.execute("SELECT status FROM payout_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if row is None:
                raise PayrollError(f"no batch called {batch_id}")
            if row[0] != "DRAFT":
                raise PayrollError(f"batch {batch_id} is {row[0]}, not waiting for approval")
            now = self.now()
            self.db.execute("UPDATE payout_batches SET status = 'APPROVED', approved_by = ?, approved_at = ?, note = ? WHERE batch_id = ?",
                            (user, now, note, batch_id))
            self.db.execute("UPDATE payouts SET status = 'QUEUED', updated_at = ? WHERE batch_id = ? AND status = 'WAITING'", (now, batch_id))

    def cancel(self, batch_id: str, user: str, approvers: Mapping[str, Any]) -> int:
        """Stop what has not been sent. Lines already sent, paid or unknown are left exactly as they are."""
        self._check_person(user, approvers)
        with self.db.tx():
            n = self.db.execute("UPDATE payouts SET status = 'CANCELLED', detail = ?, updated_at = ? WHERE batch_id = ? AND status IN ('WAITING', 'QUEUED')",
                                (f"cancelled by {user}", self.now(), batch_id)).rowcount
            if not self.db.execute("SELECT 1 FROM payouts WHERE batch_id = ? AND status <> 'CANCELLED'", (batch_id,)).fetchone():
                self.db.execute("UPDATE payout_batches SET status = 'CANCELLED' WHERE batch_id = ?", (batch_id,))
        return n

    @staticmethod
    def _check_person(user: str, approvers: Mapping[str, Any]) -> None:
        if not user or user in MACHINES or user.startswith("ai-"):
            raise PayrollError(f"{user!r} is a machine identity and cannot approve payments")
        if user not in approvers:
            raise PayrollError(f"{user!r} is not allowed to approve payments (set PAYRECON_APPROVERS)")

    # ---- sending -----------------------------------------------------------------------------------------

    def recover_stale(self) -> int:
        """A line left SENDING means a sender stopped while the request may have been on its way: UNKNOWN, never resent."""
        cutoff = self.now() - STALE_SENDING
        rows = self.db.execute("SELECT payout_id, updated_at FROM payouts WHERE status = 'SENDING'").fetchall()
        n = 0
        for pid, updated in rows:
            if datetime.fromisoformat(str(updated)) <= cutoff:
                n += self._move(pid, "SENDING", "UNKNOWN", detail="the sender stopped mid-request; it may have been paid: check before doing anything")
        return n

    def send(self, batch_id: str, *, execute: bool = False, limit: int = 1000, max_not_sent: int = 3) -> dict[str, Any]:
        """Send the queued lines of an approved batch. Dry run unless execute=True."""
        batch = self.db.execute("SELECT status FROM payout_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise PayrollError(f"no batch called {batch_id}")
        if batch[0] != "APPROVED":
            raise PayrollError(f"batch {batch_id} is {batch[0]}: a person has to approve it first")
        self.recover_stale()
        queued = self.db.execute("SELECT payout_id, msisdn_enc, amount, attempt, name FROM payouts WHERE batch_id = ? AND status = 'QUEUED' "
                                 "ORDER BY line_no LIMIT ?", (batch_id, limit)).fetchall()
        out: dict[str, Any] = {"would_send": len(queued), "accepted": 0, "not_sent": 0, "unknown": 0, "skipped": 0, "notes": []}
        if not execute:
            out["total"] = float(sum((to_dec(r[2]) for r in queued), Decimal(0)))
            return out
        if self.port is None:
            raise PayrollError("no M-Pesa connection configured (set the DARAJA_* variables)")
        streak = 0
        for pid, sealed, amount, attempt, name in queued:
            originator = f"{pid}:{attempt}"
            # Claim the line and record our request id before anything leaves: if the outcome is unknown, a result
            # that arrives later can still be matched to this line.
            if not self._move(pid, "QUEUED", "SENDING", originator_conversation_id=originator):
                out["skipped"] += 1  # someone else claimed it first
                continue
            try:
                conv, orig = self.port.send_b2c(originator_id=originator, phone=self.pii.open(sealed), amount=to_dec(amount),
                                                remarks=f"Pay {batch_id}"[:100])
            except NotSentError as e:
                self._move(pid, "SENDING", "QUEUED", detail=f"not sent: {str(e)[:200]}")
                out["not_sent"] += 1
                streak += 1
                if streak >= max_not_sent:
                    out["notes"].append(f"stopped after {streak} requests in a row were not sent: {e}")
                    break
                continue
            except SendOutcomeUnknown as e:
                self._move(pid, "SENDING", "UNKNOWN", detail=f"may have been sent: {str(e)[:200]}; check before doing anything")
                out["unknown"] += 1
                streak = 0
                continue
            streak = 0
            self._move(pid, "SENDING", "ACCEPTED", conversation_id=conv, originator_conversation_id=orig or originator,
                       detail="accepted by M-Pesa; waiting for the result")
            out["accepted"] += 1
        return out

    def _move(self, payout_id: str, frm: str, to: str, **cols: Any) -> int:
        sets = ["status = ?", "updated_at = ?"] + [f"{c} = ?" for c in cols]
        return self.db.execute(f"UPDATE payouts SET {', '.join(sets)} WHERE payout_id = ? AND status = ?",
                               (to, self.now(), *cols.values(), payout_id, frm)).rowcount

    # ---- results from M-Pesa -----------------------------------------------------------------------------

    def _find(self, res: Mapping[str, Any]) -> tuple | None:
        conv, orig = res.get("ConversationID"), res.get("OriginatorConversationID")
        return self.db.execute(
            "SELECT payout_id, status, amount FROM payouts WHERE (conversation_id = ? AND conversation_id IS NOT NULL) "
            "OR (originator_conversation_id = ? AND originator_conversation_id IS NOT NULL)", (conv, orig)).fetchone()

    def ingest_result(self, payload: Mapping[str, Any]) -> str | None:
        """Apply Daraja's B2C result. Returns the line's new status, or None when it matches no payout."""
        try:
            res = payload["Result"]
            code = str(res["ResultCode"])
        except (KeyError, TypeError) as e:
            raise ValueError(f"malformed B2C result: missing {e}") from e
        with self.db.tx():
            row = self._find(res)
            if row is None:
                return None
            pid, status, amount = row
            if status in ("PAID", "FAILED"):
                return status  # a redelivery: already settled
            params = {p.get("Key"): p.get("Value") for p in
                      ((res.get("ResultParameters") or {}).get("ResultParameter") or []) if isinstance(p, dict)}
            if code == "0":
                receipt = str(res.get("TransactionID") or params.get("TransactionReceipt") or "")
                paid = params.get("TransactionAmount")
                note = "paid"
                if paid is not None and to_dec(paid) != to_dec(amount):
                    note = f"PAID {paid} but the line was {amount}: check with M-Pesa"
                self.db.execute("UPDATE payouts SET status = 'PAID', receipt = ?, result_code = ?, result_desc = ?, detail = ?, updated_at = ? "
                                "WHERE payout_id = ?", (receipt or None, code, str(res.get("ResultDesc", ""))[:300], note, self.now(), pid))
                return "PAID"
            self.db.execute("UPDATE payouts SET status = 'FAILED', result_code = ?, result_desc = ?, detail = ?, updated_at = ? WHERE payout_id = ?",
                            (code, str(res.get("ResultDesc", ""))[:300], "M-Pesa said no; nothing was paid", self.now(), pid))
            return "FAILED"

    def note_timeout(self, payload: Mapping[str, Any]) -> str | None:
        """Daraja's queue timeout: the request may or may not have been processed. Unsettled lines become UNKNOWN."""
        res = payload.get("Result") if isinstance(payload.get("Result"), dict) else payload
        row = self._find(res or {})
        if row is None:
            return None
        pid, status, _ = row
        if status in ("PAID", "FAILED"):
            return status
        self.db.execute("UPDATE payouts SET status = 'UNKNOWN', detail = ?, updated_at = ? WHERE payout_id = ? AND status IN ('ACCEPTED', 'SENDING', 'UNKNOWN')",
                        ("M-Pesa reported a timeout: it may or may not have been paid; check before doing anything", self.now(), pid))
        return "UNKNOWN"

    # ---- people settle what the system cannot ---------------------------------------------------------------

    def resolve(self, payout_id: str, user: str, approvers: Mapping[str, Any], *, paid: bool, receipt: str = "", note: str = "") -> None:
        """A person checked the M-Pesa statement for an UNKNOWN line: it was paid (give the receipt) or it was not."""
        self._check_person(user, approvers)
        if paid and not receipt.strip():
            raise PayrollError("give the M-Pesa receipt that shows it was paid")
        n = self.db.execute(
            "UPDATE payouts SET status = ?, receipt = ?, detail = ?, updated_at = ? WHERE payout_id = ? AND status = 'UNKNOWN'",
            ("PAID" if paid else "FAILED", receipt.strip() or None,
             f"{'paid' if paid else 'not paid'}, confirmed by {user} from the statement" + (f": {note}" if note else ""), self.now(), payout_id)).rowcount
        if not n:
            raise PayrollError(f"{payout_id} is not UNKNOWN; only an unknown line can be settled by hand")

    def requeue_failed(self, batch_id: str, user: str, approvers: Mapping[str, Any]) -> int:
        """Send failed lines again, as a new attempt. Only FAILED lines: M-Pesa said no, so nothing was paid."""
        self._check_person(user, approvers)
        return self.db.execute(
            "UPDATE payouts SET status = 'QUEUED', attempt = attempt + 1, conversation_id = NULL, originator_conversation_id = NULL, "
            "detail = ?, updated_at = ? WHERE batch_id = ? AND status = 'FAILED'",
            (f"requeued by {user}", self.now(), batch_id)).rowcount

    # ---- reading -----------------------------------------------------------------------------------------

    def batches(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT batch_id, status, total, lines, created_by, created_at, approved_by, approved_at FROM payout_batches "
                               "ORDER BY created_at DESC").fetchall()
        return [dict(zip(("batch", "status", "total", "lines", "created_by", "created_at", "approved_by", "approved_at"), r)) for r in rows]

    def lines(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payout_id, line_no, name, msisdn_masked, amount, status, attempt, receipt, result_code, result_desc, detail "
                               "FROM payouts WHERE batch_id = ? ORDER BY line_no", (batch_id,)).fetchall()
        keys = ("payout_id", "line", "name", "phone", "amount", "status", "attempt", "receipt", "code", "result", "detail")
        return [dict(zip(keys, r)) for r in rows]

    def report(self, batch_id: str) -> dict[str, Any]:
        rows = self.lines(batch_id)
        if not rows:
            raise PayrollError(f"no batch called {batch_id}")
        counts: dict[str, int] = {}
        money: dict[str, Decimal] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            money[r["status"]] = money.get(r["status"], Decimal(0)) + to_dec(r["amount"])
        return {"batch": batch_id, "lines": len(rows), "counts": counts, "paid_total": money.get("PAID", Decimal(0)),
                "total": sum(money.values(), Decimal(0)), "attention": [r for r in rows if r["status"] in ("FAILED", "UNKNOWN")]}


# ---- a stand-in for M-Pesa, for the demo and tests -----------------------------------------------------------

class SimulatedPayouts:
    """Behaves like Daraja's B2C for the demo: numbers ending 0000 are 'not registered' (FAILED), numbers ending 1111
    time out after the request may have gone (UNKNOWN), everything else is paid. Results arrive when deliver() is
    called, through the same ingest_result a real callback uses."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self._n = 0

    def send_b2c(self, *, originator_id: str, phone: str, amount: Decimal, remarks: str) -> tuple[str, str]:
        self._n += 1
        self.sent.append({"originator": originator_id, "phone": phone, "amount": amount})
        if phone.endswith("1111"):
            raise SendOutcomeUnknown("timed out waiting for M-Pesa (simulated)")
        conv = f"AG_SIM_{self._n:06d}"
        ok = not phone.endswith("0000")
        self.pending.append({"Result": {
            "ResultType": 0, "ResultCode": 0 if ok else 2040,
            "ResultDesc": "The service request is processed successfully." if ok else "Receiver is not a registered M-PESA customer (simulated)",
            "OriginatorConversationID": originator_id, "ConversationID": conv, "TransactionID": f"SIM{self._n:07d}" if ok else "",
            "ResultParameters": {"ResultParameter": [{"Key": "TransactionAmount", "Value": int(amount)}]} if ok else {}}})
        return conv, originator_id

    def deliver(self, service: PayrollService) -> int:
        out, self.pending = self.pending, []
        for payload in out:
            service.ingest_result(payload)
        return len(out)
