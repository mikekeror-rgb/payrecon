-- PostgreSQL 11+. Money is NUMERIC, never float. Phone numbers are stored masked only.

CREATE TABLE invoices (
    invoice_id       TEXT PRIMARY KEY,
    reference        TEXT NOT NULL,
    reference_norm   TEXT NOT NULL UNIQUE,          -- normalize_reference(reference)
    amount           NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    internal_status  TEXT NOT NULL CHECK (internal_status IN ('PAID', 'UNPAID')),
    created_at       TIMESTAMPTZ NOT NULL,
    customer_name    TEXT                           -- PII: minimise; sent to an LLM provider if AI linking is on
);

CREATE TABLE provider_transactions (
    provider_txn_id  TEXT PRIMARY KEY,              -- receipt number, or CheckoutRequestID for failed pushes
    provider         TEXT NOT NULL,
    amount           NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    status           TEXT NOT NULL CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED')),
    reference_raw    TEXT,
    reference_norm   TEXT,
    msisdn_masked    TEXT,
    result_code      TEXT,
    result_desc      TEXT,                          -- provider free text; untrusted input
    request_id       TEXT,                          -- CheckoutRequestID this row settles (success callbacks)
    occurred_at      TIMESTAMPTZ NOT NULL,
    source           TEXT NOT NULL,                 -- stk_callback, c2b_confirmation, statement
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The primary key makes redelivered callbacks idempotent: INSERT ... ON CONFLICT upgrades status only.
CREATE INDEX provider_transactions_reference_norm_idx ON provider_transactions (reference_norm);

CREATE TABLE reconciliation_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    matcher_version  TEXT NOT NULL
);

CREATE TABLE reconciliation_results (
    run_id           BIGINT NOT NULL REFERENCES reconciliation_runs (run_id),
    invoice_id       TEXT NOT NULL REFERENCES invoices (invoice_id),
    category         TEXT NOT NULL,
    action           TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, invoice_id)
);

-- Every side-effecting step (apply, recheck, retry) is a row here before it runs.
CREATE TABLE recovery_actions (
    action_id          BIGSERIAL PRIMARY KEY,
    idempotency_key    TEXT NOT NULL UNIQUE,        -- same key can never execute twice
    invoice_id         TEXT NOT NULL REFERENCES invoices (invoice_id),
    action_type        TEXT NOT NULL CHECK (action_type IN
        ('RECHECK_STATUS', 'APPLY_PAYMENT', 'APPLY_LINKED_PAYMENT', 'RETRY_PUSH', 'NOTIFY_CUSTOMER', 'REFUND_DUPLICATE')),
    status             TEXT NOT NULL CHECK (status IN
        ('PROPOSED', 'APPROVED', 'EXECUTING', 'DONE', 'FAILED', 'REJECTED', 'CANCELLED', 'UNKNOWN')),
    requires_approval  BOOLEAN NOT NULL,
    proposed_by        TEXT NOT NULL,               -- 'system', 'ai-assistant', 'link-matcher' or a user id
    approved_by        TEXT,
    approved_at        TIMESTAMPTZ,
    attempt            INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),   -- times a worker claimed it
    payload            JSONB NOT NULL,
    result             JSONB,
    last_error         TEXT,
    not_before         TIMESTAMPTZ,                 -- backoff
    claimed_by         TEXT,
    claimed_at         TIMESTAMPTZ,
    side_effect_at     TIMESTAMPTZ,                 -- write-ahead marker: set BEFORE the provider is called
    request_id         TEXT,                        -- provider request id of a push this action sent
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The database refuses to move an approval-required action past PROPOSED without an approver.
    CHECK (NOT requires_approval OR status IN ('PROPOSED', 'REJECTED', 'CANCELLED') OR approved_by IS NOT NULL)
);
-- Retry count is derived from side effects that actually started, not from proposals.
CREATE INDEX recovery_actions_invoice ON recovery_actions (invoice_id, action_type);
CREATE INDEX recovery_actions_status ON recovery_actions (status);

-- Cases a person must decide that have no automatic action to approve. One open item per (invoice, reason).
CREATE TABLE escalations (
    escalation_id BIGSERIAL PRIMARY KEY,
    invoice_id    TEXT NOT NULL REFERENCES invoices (invoice_id),
    reason        TEXT NOT NULL,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by   TEXT,
    resolved_at   TIMESTAMPTZ,
    resolution    TEXT,
    UNIQUE (invoice_id, reason)
);

-- Append-only and hash-chained: each row's hash covers the previous row's hash, so a rewrite is detectable.
-- Tamper-evident, not tamper-proof: someone who owns the database can rebuild the chain. Anchor the head hash
-- somewhere they cannot reach if that matters. The chain is computed by the application (see recovery/store.py).
CREATE TABLE audit_log (
    audit_id   BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,                       -- 'system', 'ai-assistant', a worker id or a user id
    event      TEXT NOT NULL,
    subject    TEXT,
    details    JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE
);

CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- ---- adapter tables (payrecon.adapters) ----------------------------------------------------------------

-- One row per applied idempotency key; the primary key decides concurrent races.
CREATE TABLE applied_keys (
    idempotency_key TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL REFERENCES invoices (invoice_id),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One row per payment used. The primary key means a payment can be applied once, to one invoice.
CREATE TABLE payment_applications (
    provider_txn_id TEXT PRIMARY KEY REFERENCES provider_transactions (provider_txn_id),
    idempotency_key TEXT NOT NULL REFERENCES applied_keys (idempotency_key),
    invoice_id      TEXT NOT NULL REFERENCES invoices (invoice_id),
    amount          NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Gateway-owned. The full phone number is needed to re-push, so it is stored encrypted (application-level key).
-- Restrict access to this table; it is the only place a full number lives.
CREATE TABLE stk_requests (
    checkout_request_id TEXT PRIMARY KEY,
    merchant_request_id TEXT,
    idempotency_key     TEXT UNIQUE,
    invoice_reference   TEXT NOT NULL,
    amount              NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    msisdn_enc          TEXT NOT NULL,
    msisdn_masked       TEXT NOT NULL,
    retry_of            TEXT,
    requested_at        TIMESTAMPTZ NOT NULL
);

-- SENDING is written before the provider is called. One live reversal per transaction (index below):
-- a clean rejection can be retried; an in-flight or finished reversal cannot.
CREATE TABLE refund_requests (
    idempotency_key            TEXT PRIMARY KEY,
    provider_txn_id            TEXT NOT NULL,
    amount                     NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    status                     TEXT NOT NULL CHECK (status IN ('SENDING', 'ACCEPTED', 'REJECTED', 'COMPLETED', 'FAILED')),
    conversation_id            TEXT,
    originator_conversation_id TEXT,
    response_code              TEXT,
    result_code                TEXT,
    result_desc                TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX refund_one_live_per_txn ON refund_requests (provider_txn_id)
    WHERE status IN ('SENDING', 'ACCEPTED', 'COMPLETED');

CREATE TABLE notification_outbox (
    idempotency_key TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'SENT', 'FAILED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

-- Every webhook delivery, stored (phone numbers masked, names removed) before it is processed. The hash of the raw
-- payload is the key, so a redelivery is recognised. QUARANTINED = a person looks; ERROR = will be retried.
CREATE TABLE webhook_inbox (
    payload_sha256 TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('stk', 'c2b', 'reversal-result', 'reversal-timeout', 'b2c-result', 'b2c-timeout')),
    received_at    TIMESTAMPTZ NOT NULL,
    payload        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('RECEIVED', 'INGESTED', 'QUARANTINED', 'ERROR', 'NOTED')),
    detail         TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    processed_at   TIMESTAMPTZ
);
CREATE INDEX webhook_inbox_status ON webhook_inbox (status);

-- Payroll (B2C payouts). One line per person per batch; nothing is sent until a person approves the batch.
CREATE TABLE payout_batches (
    batch_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('DRAFT', 'APPROVED', 'CANCELLED')),
    total       NUMERIC(14,2) NOT NULL CHECK (total > 0),
    lines       INTEGER NOT NULL CHECK (lines > 0),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    note        TEXT,
    CHECK (status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);
CREATE TABLE payouts (
    payout_id                  TEXT PRIMARY KEY,
    batch_id                   TEXT NOT NULL REFERENCES payout_batches (batch_id),
    line_no                    INTEGER NOT NULL,
    name                       TEXT NOT NULL,
    payee_key                  TEXT NOT NULL,
    msisdn_enc                 TEXT NOT NULL,
    msisdn_masked              TEXT NOT NULL,
    amount                     NUMERIC(14,2) NOT NULL CHECK (amount > 0 AND amount = trunc(amount)),
    status                     TEXT NOT NULL CHECK (status IN ('WAITING', 'QUEUED', 'SENDING', 'ACCEPTED', 'PAID', 'FAILED', 'UNKNOWN', 'CANCELLED')),
    attempt                    INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    conversation_id            TEXT UNIQUE,
    originator_conversation_id TEXT UNIQUE,
    receipt                    TEXT UNIQUE,
    result_code                TEXT,
    result_desc                TEXT,
    detail                     TEXT,
    created_at                 TIMESTAMPTZ NOT NULL,
    updated_at                 TIMESTAMPTZ NOT NULL,
    name_check                 TEXT CHECK (name_check IS NULL OR name_check IN ('MATCH', 'MISMATCH', 'CONFIRMED', 'WRONG_PERSON')),
    paid_to_enc                TEXT,
    UNIQUE (batch_id, payee_key),
    CHECK (name_check <> 'MISMATCH' OR paid_to_enc IS NOT NULL),
    CHECK (status <> 'PAID' OR receipt IS NOT NULL)
);
