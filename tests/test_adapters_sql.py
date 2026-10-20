import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from payrecon.adapters import BooksConflict, Database, SQLITE_DDL, SqlBooks, SqlLedger, SqlOutboxNotifier
from payrecon.matcher import reconcile
from payrecon.models import InternalStatus, Invoice, ProviderTxn, TxnStatus
from payrecon.providers.base import EAT

T0 = datetime(2026, 10, 19, 9, tzinfo=EAT)


def mk(path=":memory:"):
    db = Database.sqlite(path)
    db.script(SQLITE_DDL)
    return db, SqlBooks(db), SqlLedger(db)


def inv(n=1, amount=5000, status=InternalStatus.UNPAID):
    return Invoice(f"I{n}", f"INV-{n:04d}", Decimal(amount), status, T0, f"Customer {n}")


def txn(tid, status=TxnStatus.SUCCESS, amount=5000, ref="INV-0001", code="0", request_id=None, minutes=0, desc=None, source="t"):
    return ProviderTxn(tid, Decimal(amount), status, T0 + timedelta(minutes=minutes), ref, "2547****149", code, source, desc, request_id)


# ---- books ----------------------------------------------------------------------------

def test_invoice_round_trip_keeps_exact_money_and_timezone():
    _, books, _ = mk()
    books.add_invoice(Invoice("I1", "INV-1", Decimal("1234.50"), InternalStatus.UNPAID, T0, "Jane"))
    got = books.get_invoice("I1")
    assert got.amount == Decimal("1234.50") and got.created_at == T0 and got.customer_name == "Jane"


def test_find_by_reference_uses_the_same_normalisation_as_the_engine():
    _, books, _ = mk()
    books.add_invoice(inv(42))
    assert books.find_invoice_by_reference("inv 42").invoice_id == "I42"
    assert books.find_invoice_by_reference("INVO42") is None and books.find_invoice_by_reference(None) is None


def test_two_invoices_cannot_share_a_normalised_reference():
    _, books, _ = mk()
    books.add_invoice(inv(1))
    with pytest.raises(Exception):
        books.add_invoice(Invoice("I9", "inv-0001", Decimal(10), InternalStatus.UNPAID, T0))


def test_mark_paid_records_every_part_and_is_idempotent():
    _, books, _ = mk()
    books.add_invoice(inv(1, 5000))
    parts = [("R1", Decimal(2500)), ("R2", Decimal(2500))]
    assert books.mark_paid("I1", parts, "k1") is True
    assert books.mark_paid("I1", parts, "k1") is False
    assert books.get_invoice("I1").internal_status is InternalStatus.PAID
    assert books.paid_by("R1") == "I1" and books.paid_by("R2") == "I1" and books.is_applied("k1")


@pytest.mark.parametrize("parts,why", [
    ([("R1", Decimal(4000))], "parts total"),
    ([("R1", Decimal(3000)), ("R2", Decimal(3000))], "parts total"),
])
def test_mark_paid_refuses_parts_that_do_not_add_up_and_writes_nothing(parts, why):
    db, books, _ = mk()
    books.add_invoice(inv(1, 5000))
    with pytest.raises(BooksConflict, match=why):
        books.mark_paid("I1", parts, "k1")
    assert books.get_invoice("I1").internal_status is InternalStatus.UNPAID and not books.is_applied("k1")
    assert db.execute("SELECT COUNT(*) FROM payment_applications").fetchone()[0] == 0


def test_a_payment_cannot_be_used_for_two_invoices():
    _, books, _ = mk()
    books.add_invoice(inv(1, 5000))
    books.add_invoice(inv(2, 5000))
    books.mark_paid("I1", [("R1", Decimal(5000))], "k1")
    with pytest.raises(BooksConflict, match="already applied"):
        books.mark_paid("I2", [("R1", Decimal(5000))], "k2")
    assert books.get_invoice("I2").internal_status is InternalStatus.UNPAID and not books.is_applied("k2")


def test_an_already_paid_invoice_is_not_applied_again_with_a_new_key():
    _, books, _ = mk()
    books.add_invoice(inv(1, 5000, InternalStatus.PAID))
    with pytest.raises(BooksConflict, match="not unpaid"):
        books.mark_paid("I1", [("R1", Decimal(5000))], "k1")
    assert not books.is_applied("k1")


def test_an_invoice_that_changes_between_the_check_and_the_write_is_refused_and_rolled_back():
    """On Postgres another session can pay the invoice after our check. The guarded UPDATE must notice."""
    db, books, _ = mk()
    books.add_invoice(inv(1, 5000))
    real_get = books.get_invoice

    def stale_read(invoice_id):
        seen = real_get(invoice_id)  # we see UNPAID ...
        db.execute("UPDATE invoices SET internal_status = 'PAID' WHERE invoice_id = ?", (invoice_id,))  # ... then someone else pays it
        return seen

    books.get_invoice = stale_read
    with pytest.raises(BooksConflict, match="changed while applying"):
        books.mark_paid("I1", [("R1", Decimal(5000))], "k1")
    books.get_invoice = real_get
    assert not books.is_applied("k1") and books.paid_by("R1") is None


def test_failure_in_the_middle_of_an_apply_rolls_everything_back():
    db, books, _ = mk()
    books.add_invoice(inv(1, 5000))
    real = db.execute

    def boom(sql, params=()):
        if sql.startswith("UPDATE invoices"):
            raise RuntimeError("disk died")
        return real(sql, params)

    db.execute = boom
    with pytest.raises(RuntimeError):
        books.mark_paid("I1", [("R1", Decimal(5000))], "k1")
    db.execute = real
    assert not books.is_applied("k1") and books.paid_by("R1") is None
    assert books.get_invoice("I1").internal_status is InternalStatus.UNPAID
    assert books.mark_paid("I1", [("R1", Decimal(5000))], "k1") is True  # and a retry then works


def test_concurrent_applies_of_one_key_have_exactly_one_winner(tmp_path):
    path = str(tmp_path / "b.db")
    _, books0, _ = mk(path)
    books0.add_invoice(inv(1, 5000))
    handles = [SqlBooks(Database.sqlite(path)) for _ in range(6)]
    results, gate = [], threading.Barrier(6)

    def go(b):
        gate.wait()
        try:
            results.append(b.mark_paid("I1", [("R1", Decimal(5000))], "same-key"))
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)

    ts = [threading.Thread(target=go, args=(b,)) for b in handles]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and all(r in (True, False) for r in results)
    assert books0.paid_by("R1") == "I1"


def test_concurrent_applies_of_one_payment_with_different_keys_have_one_winner(tmp_path):
    path = str(tmp_path / "b.db")
    _, books0, _ = mk(path)
    books0.add_invoice(inv(1, 5000))
    books0.add_invoice(inv(2, 5000))
    handles = [SqlBooks(Database.sqlite(path)) for _ in range(4)]
    results, gate = [], threading.Barrier(4)

    def go(i, b):
        gate.wait()
        try:
            results.append(b.mark_paid(f"I{1 + i % 2}", [("R1", Decimal(5000))], f"key{i}"))
        except BooksConflict:
            results.append("conflict")

    ts = [threading.Thread(target=go, args=(i, b)) for i, b in enumerate(handles)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and results.count("conflict") == 3


# ---- ledger ---------------------------------------------------------------------------

def test_ingest_statuses_only_move_forward():
    _, _, led = mk()
    assert led.ingest(txn("A", TxnStatus.PENDING, code=None)) == "inserted"
    assert led.ingest(txn("A", TxnStatus.FAILED, code="1037", desc="timeout", minutes=2)) == "upgraded"
    assert led.ingest(txn("A", TxnStatus.PENDING, code=None, minutes=3)) == "duplicate"  # a lagging copy cannot downgrade
    assert led.ingest(txn("A", TxnStatus.SUCCESS, code="0", minutes=4)) == "upgraded"
    assert led.ingest(txn("A", TxnStatus.FAILED, code="1037", minutes=5)) == "duplicate"
    got = led.get_txn("A")
    assert got.status is TxnStatus.SUCCESS and got.result_code == "0" and got.timestamp == T0 + timedelta(minutes=4)


def test_redelivered_callback_is_a_noop_and_a_conflicting_amount_is_reported_not_merged():
    _, _, led = mk()
    led.ingest(txn("A"))
    assert led.ingest(txn("A")) == "duplicate"
    assert led.ingest(txn("A", amount=4000)) == "conflict"
    assert led.get_txn("A").amount == Decimal(5000)  # first seen is kept
    assert len(led.all_txns()) == 1


def test_a_copy_that_carries_the_reference_fills_the_gap():
    _, _, led = mk()
    led.ingest(txn("A", ref=None))
    assert led.txns_for_reference("INV-0001") == []
    led.ingest(txn("A", ref="INV-0001"))
    assert [t.provider_txn_id for t in led.txns_for_reference("inv 1")] == ["A"]


def test_request_id_survives_storage_so_a_pending_push_folds_into_its_success():
    _, books, led = mk()
    books.add_invoice(inv(1, 5000))
    led.ingest(txn("ws_CO_1", TxnStatus.PENDING, code=None, source="stk_push"))
    led.ingest(txn("R1", TxnStatus.SUCCESS, request_id="ws_CO_1", minutes=1))
    res = reconcile([books.get_invoice("I1")], led.txns_for_reference("INV-0001")).invoices["I1"]
    assert res.txn_ids == ["R1"] and res.action.value == "AUTO_APPLY"


def test_ledger_round_trip_preserves_every_field():
    _, _, led = mk()
    t = txn("A", TxnStatus.FAILED, amount="1234.50", code="1037", desc="DS timeout", source="stk_callback")
    led.ingest(t)
    assert led.get_txn("A") == t


def test_ledger_rejects_non_positive_amounts():
    _, _, led = mk()
    with pytest.raises(ValueError):
        led.ingest(txn("A", amount=0))


# ---- outbox ---------------------------------------------------------------------------

def test_outbox_queues_once_per_key():
    db, _, _ = mk()
    ob = SqlOutboxNotifier(db)
    ob.notify("I1", "payment_failed_1", "n1")
    ob.notify("I1", "payment_failed_1", "n1")
    assert len(ob.queued()) == 1 and ob.mark_sent("n1") and not ob.mark_sent("n1") and ob.queued() == []


def test_reads_never_return_none_for_a_row_that_exists_while_other_threads_commit():
    """Regression: execute() used to hand back a live cursor, and another thread's COMMIT on the shared connection could
    reset it before fetchone(), so a reader saw None for a row that was there (seen in the webhook receiver under load)."""
    db = Database.sqlite()
    db.script("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'x');")
    stop, bad = threading.Event(), []

    def writer(base):
        n = base
        while not stop.is_set():
            n += 1
            with db.tx():
                db.execute("INSERT INTO t (id, v) VALUES (?, 'y')", (n,))

    def reader():
        for _ in range(15000):
            if db.execute("SELECT v FROM t WHERE id = 1").fetchone() != ("x",):
                bad.append(1)

    writers = [threading.Thread(target=writer, args=(b,)) for b in (1_000_000, 2_000_000)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in writers + readers:
        t.start()
    for t in readers:
        t.join()
    stop.set()
    for t in writers:
        t.join()
    assert bad == []


def test_a_result_is_a_plain_snapshot_and_cannot_be_disturbed_afterwards():
    db = Database.sqlite()
    db.script("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a'), (2, 'b');")
    res = db.execute("SELECT id FROM t ORDER BY id")
    db.execute("DELETE FROM t")  # later statements do not reach into an earlier result
    assert res.fetchone() == (1,) and res.fetchall() == [(2,)] and res.fetchone() is None
    assert db.execute("INSERT INTO t VALUES (3, 'c')").rowcount == 1 and db.execute("SELECT 1 WHERE 0").fetchall() == []
