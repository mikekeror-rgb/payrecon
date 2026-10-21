"""SQLite DDL for the adapters. It mirrors schema.sql (the PostgreSQL source of truth): same tables, same
constraints that SQLite can express. Money is stored as TEXT here (SQLite would turn NUMERIC into a float)
and the adapters read it back as Decimal."""

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id      TEXT PRIMARY KEY,
    reference       TEXT NOT NULL,
    reference_norm  TEXT NOT NULL UNIQUE,
    amount          TEXT NOT NULL,
    internal_status TEXT NOT NULL CHECK (internal_status IN ('PAID', 'UNPAID')),
    created_at      TEXT NOT NULL,
    customer_name   TEXT
);

CREATE TABLE IF NOT EXISTS provider_transactions (
    provider_txn_id TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    amount          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')),
    reference_raw   TEXT,
    reference_norm  TEXT,
    msisdn_masked   TEXT,
    result_code     TEXT,
    result_desc     TEXT,
    request_id      TEXT,
    occurred_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    received_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_transactions_reference_norm_idx ON provider_transactions (reference_norm);

-- One row per idempotency key that has been applied; the primary key decides concurrent races.
CREATE TABLE IF NOT EXISTS applied_keys (
    idempotency_key TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL REFERENCES invoices (invoice_id),
    applied_at      TEXT NOT NULL
);
-- One row per payment used. The primary key means a payment can be applied once, to one invoice.
CREATE TABLE IF NOT EXISTS payment_applications (
    provider_txn_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL REFERENCES applied_keys (idempotency_key),
    invoice_id      TEXT NOT NULL REFERENCES invoices (invoice_id),
    amount          TEXT NOT NULL,
    applied_at      TEXT NOT NULL
);

-- Gateway-owned: the full phone number needed to re-push, encrypted. PII.
CREATE TABLE IF NOT EXISTS stk_requests (
    checkout_request_id TEXT PRIMARY KEY,
    merchant_request_id TEXT,
    idempotency_key     TEXT UNIQUE,
    invoice_reference   TEXT NOT NULL,
    amount              TEXT NOT NULL,
    msisdn_enc          TEXT NOT NULL,
    msisdn_masked       TEXT NOT NULL,
    retry_of            TEXT,
    requested_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refund_requests (
    idempotency_key            TEXT PRIMARY KEY,
    provider_txn_id            TEXT NOT NULL,
    amount                     TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK (status IN ('SENDING', 'ACCEPTED', 'REJECTED', 'COMPLETED', 'FAILED')),
    conversation_id            TEXT,
    originator_conversation_id TEXT,
    response_code              TEXT,
    result_code                TEXT,
    result_desc                TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
-- One live reversal per transaction: a clean rejection can be retried, an in-flight or finished one cannot.
CREATE UNIQUE INDEX IF NOT EXISTS refund_one_live_per_txn ON refund_requests (provider_txn_id)
    WHERE status IN ('SENDING', 'ACCEPTED', 'COMPLETED');

CREATE TABLE IF NOT EXISTS notification_outbox (
    idempotency_key TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'SENT', 'FAILED')),
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

-- Every webhook delivery, stored (phone numbers masked, names removed) before it is processed. The hash of the raw
-- payload is the key, so a redelivery is recognised. Status: RECEIVED (stored, not processed: crash), INGESTED,
-- QUARANTINED (contradicts our records or unusable: a person looks), ERROR (retry), NOTED (informational).
CREATE TABLE IF NOT EXISTS webhook_inbox (
    payload_sha256 TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('stk', 'c2b', 'reversal-result', 'reversal-timeout', 'b2c-result', 'b2c-timeout')),
    received_at    TEXT NOT NULL,
    payload        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('RECEIVED', 'INGESTED', 'QUARANTINED', 'ERROR', 'NOTED')),
    detail         TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    processed_at   TEXT
);
CREATE INDEX IF NOT EXISTS webhook_inbox_status ON webhook_inbox (status);

-- Payroll: a batch of payouts (B2C) that a person approves once. A line is one person in one batch: UNIQUE stops the
-- same number appearing twice in a batch. Status: WAITING (batch not approved), QUEUED (approved, not sent), SENDING
-- (claimed, the request may be on its way: write-ahead), ACCEPTED (M-Pesa took it, result pending), PAID, FAILED
-- (M-Pesa said no: nothing moved), UNKNOWN (it may have been sent: never resent automatically), CANCELLED.
CREATE TABLE IF NOT EXISTS payout_batches (
    batch_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('DRAFT', 'APPROVED', 'CANCELLED')),
    total       TEXT NOT NULL,
    lines       INTEGER NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS payouts (
    payout_id                  TEXT PRIMARY KEY,
    batch_id                   TEXT NOT NULL REFERENCES payout_batches (batch_id),
    line_no                    INTEGER NOT NULL,
    name                       TEXT NOT NULL,
    payee_key                  TEXT NOT NULL,
    msisdn_enc                 TEXT NOT NULL,
    msisdn_masked              TEXT NOT NULL,
    amount                     TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK (status IN ('WAITING', 'QUEUED', 'SENDING', 'ACCEPTED', 'PAID', 'FAILED', 'UNKNOWN', 'CANCELLED')),
    attempt                    INTEGER NOT NULL DEFAULT 1,
    conversation_id            TEXT,
    originator_conversation_id TEXT,
    receipt                    TEXT,
    result_code                TEXT,
    result_desc                TEXT,
    detail                     TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    name_check                 TEXT CHECK (name_check IS NULL OR name_check IN ('MATCH', 'MISMATCH', 'CONFIRMED', 'WRONG_PERSON')),
    paid_to_enc                TEXT,
    UNIQUE (batch_id, payee_key)
);
CREATE INDEX IF NOT EXISTS payouts_conversation ON payouts (conversation_id);
CREATE INDEX IF NOT EXISTS payouts_originator ON payouts (originator_conversation_id);
"""


def _rebuild(db, table: str) -> None:
    start = SQLITE_DDL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    create = SQLITE_DDL[start:SQLITE_DDL.index(");", start) + 2].replace(f"IF NOT EXISTS {table}", table)
    with db.tx():
        db.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        db.execute(create)
        db.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
        db.execute(f"DROP TABLE {table}_old")


# (table, text the current definition contains, what the upgrade allows)
_UPGRADES = (
    ("provider_transactions", "'REVERSED'", "status may now be REVERSED"),
    ("webhook_inbox", "'b2c-result'", "payout (B2C) results may now be received"),
)


# (table, column, declaration, what it adds): added in place, at the end, so older files keep their rows
_NEW_COLUMNS = (
    ("payouts", "name_check", "TEXT CHECK (name_check IS NULL OR name_check IN ('MATCH', 'MISMATCH', 'CONFIRMED', 'WRONG_PERSON'))",
     "the name M-Pesa has for a paid number is compared with the list"),
    ("payouts", "paid_to_enc", "TEXT", "the registered name of a mismatched payment is kept, encrypted"),
)


def migrate_sqlite(db) -> list[str]:
    """Bring an existing SQLite file up to this schema. SQLite cannot alter a CHECK constraint, so a table whose
    constraint is out of date is rebuilt under the current definition, rows copied, in one transaction.
    Returns what was migrated (empty when already current)."""
    done: list[str] = []
    for table, marker, what in _UPGRADES:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if row and marker not in row[0]:
            _rebuild(db, table)
            done.append(f"{table}: {what}")
    for table, column, decl, what in _NEW_COLUMNS:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if row and column not in {c[1] for c in db.execute(f"PRAGMA table_info({table})").fetchall()}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            done.append(f"{table}: {what}")
    if done:
        db.script(SQLITE_DDL)  # indexes and any new tables
    return done
