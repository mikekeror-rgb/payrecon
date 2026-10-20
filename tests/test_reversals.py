"""Reversals payrecon did not start (seen live: the Daraja sandbox reversed real-money test payments on its own)."""
import json
from datetime import datetime, timedelta
from decimal import Decimal

from payrecon.adapters import SQLITE_DDL, Database, SqlBooks, SqlLedger, migrate_sqlite
from payrecon.adapters.fake_daraja import FakeDaraja
from payrecon.ai import investigate
from payrecon.matcher import reconcile
from payrecon.models import Action, Category, InternalStatus, Invoice, ProviderTxn, TxnStatus
from payrecon.providers import load_reversals_csv
from payrecon.providers.base import EAT

T0 = datetime(2026, 10, 20, 12, tzinfo=EAT)


def inv(iid="L4", ref="TEST7", amount=1, status=InternalStatus.UNPAID):
    return Invoice(iid, ref, Decimal(amount), status, T0 - timedelta(days=1))


def pay(tid, ref="TEST7", amount=1, status=TxnStatus.SUCCESS, minutes=0, source="stk_callback"):
    return ProviderTxn(tid, Decimal(amount), status, T0 + timedelta(minutes=minutes), ref, None, "0", source)


def rev(tid, amount=1, source="statement_reversal"):
    return ProviderTxn(tid, Decimal(amount), TxnStatus.REVERSED, T0 + timedelta(hours=1), None, None, None, source,
                       result_desc="reversed under UJKA0QZNF5")


def ledger():
    db = Database.sqlite()
    db.script(SQLITE_DDL)
    return db, SqlLedger(db)


def test_a_reversed_payment_no_longer_pays_its_invoice_and_is_never_auto_applied():
    # TEST7 live: provider-confirmed, then reversed by Safaricom. Before this, the engine said AUTO_APPLY.
    r = reconcile([inv()], [pay("UJJA0AW035"), rev("UJJA0AW035")]).invoices["L4"]
    assert (r.category, r.action, r.success_total) == (Category.REVERSED_PAYMENT, Action.ESCALATE_HUMAN, Decimal(0))


def test_a_duplicate_whose_payments_were_reversed_is_not_refunded_again():
    # TEST6 live: two successes, both reversed by Safaricom. Before this, a person was asked to approve a refund.
    both = reconcile([inv("L3", "TEST6")], [pay("A", "TEST6"), pay("B", "TEST6", minutes=5), rev("A"), rev("B")]).invoices["L3"]
    assert both.category is Category.REVERSED_PAYMENT
    one = reconcile([inv("L3", "TEST6")], [pay("A", "TEST6"), pay("B", "TEST6", minutes=5), rev("B")]).invoices["L3"]
    assert (one.category, one.action) == (Category.UNRECORDED_PAYMENT, Action.AUTO_APPLY) and "also reversed: B" in one.note


def test_the_ledger_moves_a_payment_to_reversed_keeps_its_details_and_never_moves_it_back():
    db, led = ledger()
    assert led.ingest(pay("UJKA0AXL4X")) == "inserted"
    assert led.ingest(rev("UJKA0AXL4X")) == "upgraded"
    t = led.get_txn("UJKA0AXL4X")
    assert (t.status, t.amount, t.reference_raw, t.timestamp, t.source) == (TxnStatus.REVERSED, Decimal(1), "TEST7", T0, "statement_reversal")
    assert led.ingest(pay("UJKA0AXL4X")) == "duplicate"  # a redelivered success callback cannot undo the reversal
    assert led.get_txn("UJKA0AXL4X").status is TxnStatus.REVERSED
    assert led.ingest(rev("UJKA0AXL4X")) == "duplicate"


def test_a_partial_reversal_is_a_conflict_for_a_person_not_a_whole_reversal():
    db, led = ledger()
    led.ingest(pay("P1", amount=1000))
    assert led.ingest(rev("P1", amount=400)) == "conflict"
    assert led.get_txn("P1").status is TxnStatus.SUCCESS


def test_a_reversal_seen_before_its_payment_still_wins():
    db, led = ledger()
    assert led.ingest(rev("EARLY")) == "inserted"
    led.ingest(pay("EARLY"))
    assert led.get_txn("EARLY").status is TxnStatus.REVERSED


def test_reversal_lines_load_from_a_statement_csv(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("original_receipt,reversal_receipt,amount,reversed_at\nUJKA0AXL4X,UJKA0QZNF5,1,2026-10-20T13:00:00\n")
    [t] = load_reversals_csv(p)
    assert (t.provider_txn_id, t.status, t.amount, t.source) == ("UJKA0AXL4X", TxnStatus.REVERSED, Decimal(1), "statement_reversal")
    assert "UJKA0QZNF5" in t.result_desc and t.timestamp.tzinfo is not None


def test_the_investigator_says_who_reversed_it_and_what_to_do():
    txns = [pay("UJJA0AW035"), rev("UJJA0AW035")]
    air = investigate(reconcile([inv()], txns), [inv()], txns)
    [item] = air.investigations
    assert item.category is Category.REVERSED_PAYMENT and item.exposure == Decimal(1)
    assert "UJJA0AW035" in item.explanation and "outside payrecon" in item.explanation and "contact the customer" in item.explanation
    txns = [pay("X"), rev("X", source="reversal_result")]
    paid = inv(status=InternalStatus.PAID)
    [item] = investigate(reconcile([paid], txns), [paid], txns).investigations
    assert "by payrecon's own refund" in item.explanation and "still say PAID" in item.explanation


def test_an_old_database_is_upgraded_in_place_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database.sqlite(path)
    db.script(SQLITE_DDL.replace("CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED', 'REVERSED'))", "CHECK (status IN ('SUCCESS', 'PENDING', 'FAILED'))"))
    SqlLedger(db).ingest(pay("OLD1"))
    db.close()
    db = Database.sqlite(path)
    assert migrate_sqlite(db) == ["provider_transactions: status may now be REVERSED"]
    assert migrate_sqlite(db) == []  # idempotent
    led = SqlLedger(db)
    assert led.get_txn("OLD1").status is TxnStatus.SUCCESS and led.ingest(rev("OLD1")) == "upgraded"


def test_a_completed_payrecon_refund_marks_the_payment_reversed_and_the_duplicate_resolves():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_webhook import Rig

    with FakeDaraja() as fd:
        r = Rig(fd, reversal=True)
        books = SqlBooks(r.db)
        books.add_invoice(inv("D1", "INV-0009"))
        r.ledger.ingest(pay("DUP1", "INV-0009"))
        r.ledger.ingest(pay("DUP2", "INV-0009", minutes=3))
        assert reconcile(books.invoices(), r.ledger.all_txns()).invoices["D1"].category is Category.DUPLICATE_PAYMENT
        conv = r.gw.reverse("DUP2", Decimal(1), "refund-1")
        ok = {"Result": {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "OriginatorConversationID": "x",
                         "ConversationID": conv, "TransactionID": "UJKREV0001"}}
        assert r.post("reversal-result", ok)[0] == 200
        t = r.ledger.get_txn("DUP2")
        assert t.status is TxnStatus.REVERSED and t.source == "reversal_result" and conv in t.result_desc
        assert reconcile(books.invoices(), r.ledger.all_txns()).invoices["D1"].category is Category.UNRECORDED_PAYMENT
