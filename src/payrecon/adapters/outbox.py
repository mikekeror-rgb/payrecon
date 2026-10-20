"""Transactional-outbox notifier: queues customer messages in the database, once per idempotency key.
A separate sender (SMS gateway, email) drains the queue; it is deliberately not part of this package."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import Database


class SqlOutboxNotifier:
    """Implements recovery.ports.NotifyPort."""

    def __init__(self, db: Database):
        self.db = db

    def notify(self, invoice_id: str, template: str, idempotency_key: str) -> None:
        self.db.execute(
            "INSERT INTO notification_outbox (idempotency_key, invoice_id, template, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (idempotency_key, invoice_id, template, datetime.now(timezone.utc)),
        )

    def queued(self) -> list[tuple[str, str, str]]:
        return self.db.execute(
            "SELECT idempotency_key, invoice_id, template FROM notification_outbox WHERE status = 'QUEUED' ORDER BY created_at, idempotency_key"
        ).fetchall()

    def mark_sent(self, idempotency_key: str) -> bool:
        return self.db.execute(
            "UPDATE notification_outbox SET status = 'SENT', sent_at = ? WHERE idempotency_key = ? AND status = 'QUEUED'",
            (datetime.now(timezone.utc), idempotency_key),
        ).rowcount == 1
