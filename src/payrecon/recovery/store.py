"""Durable action store with a tamper-evident audit log (SQLite; mirrors schema.sql).

What this layer guarantees, and how:

* Same intent, one row: `idempotency_key` is UNIQUE, so proposing twice is a no-op.
* One executor per action: `claim` is a single compare-and-swap UPDATE, so two workers
  cannot both take it.
* No approval, no execution: a CHECK constraint rejects any state past PROPOSED for an
  approval-required action unless `approved_by` is set. The database says no even if the
  Python is wrong.
* Every state change and its audit event commit together or not at all.
* The audit log cannot be edited or deleted through SQL (triggers), and each row hashes the
  previous one, so a rewrite shows up in `verify_audit`. That is tamper-evident, not
  tamper-proof: someone who owns the file can rebuild the chain. Publish the head hash
  somewhere they cannot reach if that matters.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .models import OPEN, ActionStatus, ActionType, RecoveryAction

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_actions (
    action_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT NOT NULL UNIQUE,
    invoice_id        TEXT NOT NULL,
    action_type       TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
        ('PROPOSED','APPROVED','EXECUTING','DONE','FAILED','REJECTED','CANCELLED','UNKNOWN')),
    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    proposed_by       TEXT NOT NULL,
    approved_by       TEXT,
    approved_at       TEXT,
    attempt           INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    payload           TEXT NOT NULL,
    result            TEXT,
    last_error        TEXT,
    not_before        TEXT,
    claimed_by        TEXT,
    claimed_at        TEXT,
    side_effect_at    TEXT,
    request_id        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (requires_approval = 0 OR status IN ('PROPOSED','REJECTED','CANCELLED') OR approved_by IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ra_invoice ON recovery_actions (invoice_id, action_type);
CREATE INDEX IF NOT EXISTS ra_status ON recovery_actions (status);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    event     TEXT NOT NULL,
    subject   TEXT,
    details   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    TEXT NOT NULL,
    reason        TEXT NOT NULL,
    detail        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resolved_by   TEXT,
    resolved_at   TEXT,
    resolution    TEXT,
    UNIQUE (invoice_id, reason)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_COLS = (
    "action_id, idempotency_key, invoice_id, action_type, status, requires_approval, proposed_by, approved_by, "
    "approved_at, attempt, payload, result, last_error, not_before, claimed_by, claimed_at, side_effect_at, "
    "request_id, created_at, updated_at"
)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row(r: tuple) -> RecoveryAction:
    return RecoveryAction(
        action_id=r[0], idempotency_key=r[1], invoice_id=r[2], action_type=ActionType(r[3]),
        status=ActionStatus(r[4]), requires_approval=bool(r[5]), proposed_by=r[6], approved_by=r[7],
        approved_at=r[8], attempt=r[9], payload=json.loads(r[10]), result=json.loads(r[11]) if r[11] else None,
        last_error=r[12], not_before=r[13], claimed_by=r[14], claimed_at=r[15], side_effect_at=r[16],
        request_id=r[17], created_at=r[18], updated_at=r[19],
    )


class ActionStore:
    def __init__(self, path: str = ":memory:", clock: Callable[[], datetime] | None = None):
        self.path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        self._conn.close()

    # ---- transactions & audit -------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:  # includes simulated crashes: nothing half-committed
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _audit(self, actor: str, event: str, subject: str | None, details: dict[str, Any] | None) -> None:
        row = self._conn.execute("SELECT hash FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        at = _iso(self.now())
        det = _canon(details or {})
        h = hashlib.sha256((prev + _canon([at, actor, event, subject, det])).encode()).hexdigest()
        self._conn.execute(
            "INSERT INTO audit_log (at, actor, event, subject, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (at, actor, event, subject, det, prev, h),
        )

    def audit(self, actor: str, event: str, subject: str | None = None, details: dict[str, Any] | None = None) -> None:
        with self._tx():
            self._audit(actor, event, subject, details)

    def audit_events(self, subject: str | None = None, event: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT audit_id, at, actor, event, subject, details FROM audit_log WHERE 1=1", []
        if subject is not None:
            q += " AND subject = ?"
            args.append(subject)
        if event is not None:
            q += " AND event = ?"
            args.append(event)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY audit_id", args).fetchall()
        return [
            {"audit_id": r[0], "at": r[1], "actor": r[2], "event": r[3], "subject": r[4], "details": json.loads(r[5])}
            for r in rows
        ]

    def audit_head(self) -> str:
        with self._lock:
            row = self._conn.execute("SELECT hash FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def verify_audit(self) -> tuple[bool, int | None]:
        """Recompute the hash chain. Returns (ok, first_bad_audit_id)."""
        prev = GENESIS
        with self._lock:
            rows = self._conn.execute(
                "SELECT audit_id, at, actor, event, subject, details, prev_hash, hash FROM audit_log ORDER BY audit_id"
            ).fetchall()
        for aid, at, actor, event, subject, det, p, h in rows:
            want = hashlib.sha256((prev + _canon([at, actor, event, subject, det])).encode()).hexdigest()
            if p != prev or h != want:
                return False, aid
            prev = h
        return True, None

    # ---- reading --------------------------------------------------------------

    def get(self, action_id: int) -> RecoveryAction:
        with self._lock:
            r = self._conn.execute(f"SELECT {_COLS} FROM recovery_actions WHERE action_id = ?", (action_id,)).fetchone()
        if r is None:
            raise KeyError(action_id)
        return _row(r)

    def get_by_key(self, key: str) -> RecoveryAction | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {_COLS} FROM recovery_actions WHERE idempotency_key = ?", (key,)).fetchone()
        return _row(r) if r else None

    def list_actions(
        self,
        status: ActionStatus | None = None,
        invoice_id: str | None = None,
        action_type: ActionType | None = None,
    ) -> list[RecoveryAction]:
        q, args = f"SELECT {_COLS} FROM recovery_actions WHERE 1=1", []
        for col, val in (("status", status), ("invoice_id", invoice_id), ("action_type", action_type)):
            if val is not None:
                q += f" AND {col} = ?"
                args.append(val.value if hasattr(val, "value") else val)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY action_id", args).fetchall()
        return [_row(r) for r in rows]

    def open_action(self, invoice_id: str, action_type: ActionType) -> RecoveryAction | None:
        marks = ",".join("?" * len(OPEN))
        with self._lock:
            r = self._conn.execute(
                f"SELECT {_COLS} FROM recovery_actions WHERE invoice_id=? AND action_type=? AND status IN ({marks}) "
                "ORDER BY action_id LIMIT 1",
                (invoice_id, action_type.value, *[s.value for s in OPEN]),
            ).fetchone()
        return _row(r) if r else None

    def count_actions(self, invoice_id: str, action_type: ActionType) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM recovery_actions WHERE invoice_id=? AND action_type=?",
                (invoice_id, action_type.value),
            ).fetchone()[0]

    def send_count(self, invoice_id: str, action_type: ActionType = ActionType.RETRY_PUSH) -> int:
        """How many times a side effect of this type actually started for the invoice. This, not any
        in-memory counter, is the retry count: it survives restarts."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM recovery_actions WHERE invoice_id=? AND action_type=? AND side_effect_at IS NOT NULL",
                (invoice_id, action_type.value),
            ).fetchone()[0]

    def retry_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT invoice_id, COUNT(*) FROM recovery_actions WHERE action_type=? AND side_effect_at IS NOT NULL "
                "GROUP BY invoice_id",
                (ActionType.RETRY_PUSH.value,),
            ).fetchall()
        return dict(rows)

    def last_side_effect_at(self, invoice_id: str, action_type: ActionType) -> str | None:
        with self._lock:
            return self._conn.execute(
                "SELECT MAX(side_effect_at) FROM recovery_actions WHERE invoice_id=? AND action_type=?",
                (invoice_id, action_type.value),
            ).fetchone()[0]

    def refunded_txns(self) -> set[str]:
        """Transactions with a refund that was started (done, in flight or of unknown outcome)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM recovery_actions WHERE action_type=? AND side_effect_at IS NOT NULL",
                (ActionType.REFUND_DUPLICATE.value,),
            ).fetchall()
        return {json.loads(r[0])["refund_txn_id"] for r in rows}

    def runnable(self, limit: int = 100) -> list[RecoveryAction]:
        now = _iso(self.now())
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM recovery_actions WHERE "
                "((status='PROPOSED' AND requires_approval=0) OR status='APPROVED') "
                "AND (not_before IS NULL OR not_before <= ?) ORDER BY action_id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [_row(r) for r in rows]

    def stale_executing(self, older_than_seconds: float) -> list[RecoveryAction]:
        cutoff = _iso(datetime.fromtimestamp(self.now().timestamp() - older_than_seconds, tz=timezone.utc))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM recovery_actions WHERE status='EXECUTING' AND claimed_at <= ? ORDER BY action_id",
                (cutoff,),
            ).fetchall()
        return [_row(r) for r in rows]

    # ---- writing --------------------------------------------------------------

    def propose(
        self,
        *,
        invoice_id: str,
        action_type: ActionType,
        key: str,
        payload: dict[str, Any],
        requires_approval: bool,
        proposed_by: str = "system",
        reason: str = "",
    ) -> tuple[RecoveryAction, bool]:
        """Insert unless the key already exists. Returns (action, created)."""
        now = _iso(self.now())
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO recovery_actions (idempotency_key, invoice_id, action_type, status, "
                "requires_approval, proposed_by, payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, invoice_id, action_type.value, ActionStatus.PROPOSED.value, int(requires_approval),
                 proposed_by, _canon(payload), now, now),
            )
            created = cur.rowcount == 1
            if created:
                aid = cur.lastrowid
                self._audit(proposed_by, "action.proposed", f"action:{aid}", {
                    "invoice_id": invoice_id, "type": action_type.value, "key": key,
                    "requires_approval": requires_approval, "reason": reason, "payload": payload,
                })
        action = self.get_by_key(key)
        assert action is not None
        return action, created

    def _cas(
        self,
        action_id: int,
        from_states: tuple[ActionStatus, ...],
        sets: dict[str, Any],
        *,
        actor: str,
        event: str,
        details: dict[str, Any] | None = None,
        extra_where: str = "",
    ) -> bool:
        """Compare-and-swap: change the row only if it is still in one of `from_states`; audit in the same commit."""
        sets = {**sets, "updated_at": _iso(self.now())}
        assign = ", ".join(f"{k} = ?" for k in sets)
        marks = ",".join("?" * len(from_states))
        with self._tx():
            cur = self._conn.execute(
                f"UPDATE recovery_actions SET {assign} WHERE action_id = ? AND status IN ({marks}) {extra_where}",
                (*sets.values(), action_id, *[s.value for s in from_states]),
            )
            ok = cur.rowcount == 1
            if ok:
                self._audit(actor, event, f"action:{action_id}", details)
        return ok

    def approve(self, action_id: int, user: str, note: str = "") -> bool:
        return self._cas(
            action_id, (ActionStatus.PROPOSED,),
            {"status": ActionStatus.APPROVED.value, "approved_by": user, "approved_at": _iso(self.now())},
            actor=user, event="action.approved", details={"note": note}, extra_where="AND requires_approval = 1",
        )

    def reject(self, action_id: int, user: str, reason: str = "") -> bool:
        return self._cas(
            action_id, (ActionStatus.PROPOSED, ActionStatus.APPROVED), {"status": ActionStatus.REJECTED.value},
            actor=user, event="action.rejected", details={"reason": reason},
        )

    def claim(self, action_id: int, worker: str) -> bool:
        """Take an action for execution. One UPDATE decides the race: exactly one caller can win, and an
        approval-required action can only be taken once it is APPROVED."""
        now = _iso(self.now())
        with self._tx():
            cur = self._conn.execute(
                "UPDATE recovery_actions SET status='EXECUTING', claimed_by=?, claimed_at=?, attempt=attempt+1, "
                "updated_at=? WHERE action_id=? AND ((status='PROPOSED' AND requires_approval=0) OR status='APPROVED')",
                (worker, now, now, action_id),
            )
            ok = cur.rowcount == 1
            if ok:
                self._audit(worker, "action.claimed", f"action:{action_id}", {})
        return ok

    def begin_side_effect(self, action_id: int, worker: str, *, allow_repeat: bool) -> bool:
        """Write-ahead marker, committed before the provider is called. If we crash after this and before
        `finish`, recovery knows a side effect may have happened. Non-repeat-safe actions can only start once."""
        where = "" if allow_repeat else "AND side_effect_at IS NULL"
        return self._cas(
            action_id, (ActionStatus.EXECUTING,), {"side_effect_at": _iso(self.now())},
            actor=worker, event="action.side_effect_started", extra_where=where,
        )

    def abort_side_effect(self, action_id: int, worker: str, reason: str) -> bool:
        """The call provably never left: take the write-ahead marker back so this does not count as a send."""
        return self._cas(
            action_id, (ActionStatus.EXECUTING,), {"side_effect_at": None},
            actor=worker, event="action.side_effect_aborted", details={"reason": reason},
        )

    def record_request_id(self, action_id: int, request_id: str, worker: str) -> bool:
        return self._cas(
            action_id, (ActionStatus.EXECUTING,), {"request_id": request_id},
            actor=worker, event="action.request_id_recorded", details={"request_id": request_id},
        )

    def finish(
        self,
        action_id: int,
        status: ActionStatus,
        *,
        actor: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        event: str | None = None,
    ) -> bool:
        assert status in (ActionStatus.DONE, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.UNKNOWN)
        return self._cas(
            action_id, (ActionStatus.EXECUTING,),
            {"status": status.value, "result": _canon(result) if result is not None else None, "last_error": error},
            actor=actor, event=event or f"action.{status.value.lower()}",
            details={"result": result, "error": error},
        )

    def cancel(self, action_id: int, reason: str, actor: str = "system") -> bool:
        return self._cas(
            action_id, (ActionStatus.PROPOSED, ActionStatus.APPROVED, ActionStatus.EXECUTING),
            {"status": ActionStatus.CANCELLED.value, "last_error": reason},
            actor=actor, event="action.cancelled", details={"reason": reason},
        )

    def release(self, action_id: int, error: str, not_before: datetime | None, actor: str) -> bool:
        """Hand an EXECUTING action back to the queue (nothing irreversible happened, or the effect is repeat-safe)."""
        with self._tx():
            row = self._conn.execute(
                "SELECT requires_approval FROM recovery_actions WHERE action_id=? AND status='EXECUTING'", (action_id,)
            ).fetchone()
            if row is None:
                return False
            back = ActionStatus.APPROVED if row[0] else ActionStatus.PROPOSED
            self._conn.execute(
                "UPDATE recovery_actions SET status=?, last_error=?, not_before=?, claimed_by=NULL, claimed_at=NULL, "
                "updated_at=? WHERE action_id=?",
                (back.value, error, _iso(not_before) if not_before else None, _iso(self.now()), action_id),
            )
            self._audit(actor, "action.released", f"action:{action_id}", {"error": error})
        return True

    # ---- review items: cases a person must decide that have no automatic action to approve ----

    def escalate(self, invoice_id: str, reason: str, detail: dict[str, Any] | None = None, actor: str = "system") -> bool:
        """Open a review item once per (invoice, reason); repeated planning runs do not re-announce it."""
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO escalations (invoice_id, reason, detail, created_at) VALUES (?,?,?,?)",
                (invoice_id, reason, _canon(detail or {}), _iso(self.now())),
            )
            created = cur.rowcount == 1
            if created:
                self._audit(actor, "escalation.opened", f"invoice:{invoice_id}", {"reason": reason, "detail": detail or {}})
        return created

    def open_escalations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT escalation_id, invoice_id, reason, detail, created_at FROM escalations "
                "WHERE resolved_at IS NULL ORDER BY escalation_id"
            ).fetchall()
        return [{"escalation_id": r[0], "invoice_id": r[1], "reason": r[2], "detail": json.loads(r[3]), "created_at": r[4]} for r in rows]

    def resolve_escalation(self, escalation_id: int, user: str, resolution: str) -> bool:
        with self._tx():
            cur = self._conn.execute(
                "UPDATE escalations SET resolved_by=?, resolved_at=?, resolution=? WHERE escalation_id=? AND resolved_at IS NULL",
                (user, _iso(self.now()), resolution, escalation_id),
            )
            ok = cur.rowcount == 1
            if ok:
                self._audit(user, "escalation.resolved", f"escalation:{escalation_id}", {"resolution": resolution})
        return ok

    # ---- global halt (circuit breaker / kill switch) --------------------------

    def halt(self, reason: str, actor: str = "system") -> None:
        with self._tx():
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('halted', ?)", (reason,))
            self._audit(actor, "system.halted", None, {"reason": reason})

    def resume(self, user: str, note: str = "") -> None:
        with self._tx():
            self._conn.execute("DELETE FROM meta WHERE key='halted'")
            self._audit(user, "system.resumed", None, {"note": note})

    def halted(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
        return row[0] if row else None
