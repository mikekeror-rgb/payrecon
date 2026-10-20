from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from payrecon.matcher import normalize_reference, reconcile
from payrecon.models import Action, Category, InternalStatus, Invoice, ProviderTxn, TxnStatus
from payrecon.providers.base import EAT

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=EAT)


def inv(i=1, amount=5000, status=InternalStatus.UNPAID):
    return Invoice(f"I{i}", f"INV-{i:04d}", Decimal(amount), status, T0)


def txn(tid, amount=5000, status=TxnStatus.SUCCESS, ref="INV-0001", code="0", minutes=0, source="stk_callback"):
    return ProviderTxn(tid, Decimal(amount), status, T0 + timedelta(minutes=minutes), ref, "2547****149", code, source)


def run(invoice, txns, retry=None):
    report = reconcile([invoice], txns, retry)
    return report, report.invoices[invoice.invoice_id]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("INV-0042", "INV42"),
        ("inv 42", "INV42"),
        ("  Inv0042 ", "INV42"),
        ("INVO042", "INVO42"),  # letter O, not zero: must NOT collapse onto INV42
        ("0042", "42"),
        ("john k", "JOHNK"),
        ("", None),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_reference(raw, expected):
    assert normalize_reference(raw) == expected


def test_exact_match_is_matched():
    _, r = run(inv(status=InternalStatus.PAID), [txn("A")])
    assert (r.category, r.action) == (Category.MATCHED, Action.NONE)


def test_messy_reference_still_matches():
    _, r = run(inv(status=InternalStatus.PAID), [txn("A", ref="inv 1")])
    assert r.category is Category.MATCHED


def test_provider_success_but_books_say_unpaid_is_auto_apply():
    _, r = run(inv(), [txn("A")])
    assert (r.category, r.action) == (Category.UNRECORDED_PAYMENT, Action.AUTO_APPLY)


def test_redelivered_callback_is_not_a_duplicate_payment():
    same = txn("A")
    stmt_copy = txn("A", ref=None, source="statement")
    report, r = run(inv(status=InternalStatus.PAID), [same, stmt_copy])
    assert r.category is Category.MATCHED
    assert r.txn_ids == ["A"]
    assert not report.orphans


def test_two_distinct_successes_are_a_duplicate_and_need_a_human():
    _, r = run(inv(status=InternalStatus.PAID), [txn("A"), txn("B", minutes=2)])
    assert (r.category, r.action) == (Category.DUPLICATE_PAYMENT, Action.ESCALATE_HUMAN)


def test_pending_is_rechecked_never_retried_or_applied():
    _, r = run(inv(), [txn("A", status=TxnStatus.PENDING, code=None)])
    assert (r.category, r.action) == (Category.PENDING, Action.RECHECK_STATUS)


def test_pending_next_to_success_blocks_auto_apply():
    _, r = run(inv(), [txn("A"), txn("B", status=TxnStatus.PENDING, code=None, minutes=1)])
    assert r.category is Category.UNRECORDED_PAYMENT
    assert r.action is Action.RECHECK_STATUS


@pytest.mark.parametrize(
    "code,retries,expected",
    [
        ("1037", 0, Action.RETRY_ELIGIBLE),
        ("1001", 2, Action.RETRY_ELIGIBLE),
        ("1037", 3, Action.ESCALATE_HUMAN),  # retry budget spent
        ("1032", 0, Action.NOTIFY_CUSTOMER),  # customer cancelled: never re-push
        ("1", 0, Action.NOTIFY_CUSTOMER),
        ("9999", 0, Action.ESCALATE_HUMAN),  # unknown code: do not guess
    ],
)
def test_failed_payment_actions(code, retries, expected):
    _, r = run(inv(), [txn("A", status=TxnStatus.FAILED, code=code)], {"I1": retries})
    assert r.category is Category.FAILED
    assert r.action is expected


def test_latest_failure_code_decides():
    old = txn("A", status=TxnStatus.FAILED, code="1037", minutes=0)
    new = txn("B", status=TxnStatus.FAILED, code="1032", minutes=10)
    _, r = run(inv(), [old, new])
    assert r.action is Action.NOTIFY_CUSTOMER


@pytest.mark.parametrize("paid", [2500, 5500])
def test_amount_mismatch_escalates(paid):
    _, r = run(inv(), [txn("A", amount=paid)])
    assert (r.category, r.action) == (Category.AMOUNT_MISMATCH, Action.ESCALATE_HUMAN)


def test_partial_payment_with_failed_remainder():
    _, r = run(inv(), [txn("A", amount=2500), txn("B", amount=2500, status=TxnStatus.FAILED, code="1037", minutes=5)])
    assert (r.category, r.action) == (Category.PARTIAL_PAYMENT, Action.ESCALATE_HUMAN)


def test_installments_that_sum_to_the_amount_are_matched():
    _, r = run(inv(status=InternalStatus.PAID), [txn("A", amount=2000), txn("B", amount=3000, minutes=3)])
    assert r.category is Category.MATCHED


def test_no_transactions_is_missing_payment():
    _, r = run(inv(), [])
    assert (r.category, r.action) == (Category.MISSING_PAYMENT, Action.AWAIT_PAYMENT)


def test_unlinkable_success_becomes_orphan_and_invoice_stays_missing():
    report, r = run(inv(), [txn("A", ref="JOHN K")])
    assert r.category is Category.MISSING_PAYMENT
    assert report.orphans["A"].category is Category.ORPHANED_TXN
    assert report.orphans["A"].action is Action.LINK_OR_ESCALATE


def test_conflicting_status_copies_raise_a_warning():
    report, _ = run(inv(), [txn("A"), txn("A", status=TxnStatus.FAILED, code="1037")])
    assert any("conflicting" in w for w in report.warnings)


def test_colliding_invoice_references_are_rejected():
    a = Invoice("I1", "INV-0001", Decimal(100), InternalStatus.UNPAID, T0)
    b = Invoice("I2", "inv 1", Decimal(100), InternalStatus.UNPAID, T0)
    with pytest.raises(ValueError):
        reconcile([a, b], [])


# ---- STK push request folding (pending row + success callback under a different id) ----

def _fold_inv():
    from datetime import datetime
    from payrecon.providers.base import EAT
    return Invoice("I1", "INV-0001", Decimal(5000), InternalStatus.UNPAID, datetime(2026, 10, 1, 9, tzinfo=EAT))


def _fold_txn(tid, status, request_id=None, amount=5000, ref="INV-0001", code=None):
    from datetime import datetime
    from payrecon.providers.base import EAT
    return ProviderTxn(tid, Decimal(amount), status, datetime(2026, 10, 1, 9, 1, tzinfo=EAT), ref, None, code, "t", request_id=request_id)


def test_success_callback_folds_into_its_pending_push_row():
    r = reconcile([_fold_inv()], [_fold_txn("ws_CO_1", TxnStatus.PENDING), _fold_txn("RCPT1", TxnStatus.SUCCESS, "ws_CO_1", code="0")])
    res = r.invoices["I1"]
    assert res.txn_ids == ["RCPT1"] and res.action.value == "AUTO_APPLY" and not r.warnings


def test_fold_is_order_independent():
    a = reconcile([_fold_inv()], [_fold_txn("RCPT1", TxnStatus.SUCCESS, "ws_CO_1"), _fold_txn("ws_CO_1", TxnStatus.PENDING)])
    assert a.invoices["I1"].txn_ids == ["RCPT1"]


def test_success_never_loses_to_a_failed_request_row_but_warns():
    r = reconcile([_fold_inv()], [_fold_txn("ws_CO_1", TxnStatus.FAILED, code="1037"), _fold_txn("RCPT1", TxnStatus.SUCCESS, "ws_CO_1")])
    assert r.invoices["I1"].txn_ids == ["RCPT1"]
    assert any("both SUCCESS and FAILED" in w for w in r.warnings)


def test_unrelated_pending_is_not_folded():
    r = reconcile([_fold_inv()], [_fold_txn("ws_CO_9", TxnStatus.PENDING), _fold_txn("RCPT1", TxnStatus.SUCCESS, "ws_CO_1")])
    assert sorted(r.invoices["I1"].txn_ids) == ["RCPT1", "ws_CO_9"]
    assert r.invoices["I1"].action.value == "RECHECK_STATUS"  # a different pending push still blocks apply


def test_a_payment_applied_to_an_invoice_counts_for_it_whatever_its_reference():
    from datetime import datetime, timezone
    from decimal import Decimal
    from payrecon.matcher import reconcile
    from payrecon.models import Category, InternalStatus, Invoice, ProviderTxn, TxnStatus
    t0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
    inv = Invoice("L6", "INV-0006", Decimal(50), InternalStatus.PAID, t0, "Grace Wanjiku")
    pay = ProviderTxn("P1", Decimal(50), TxnStatus.SUCCESS, t0, "Shiku", None, "0", "c2b_confirmation")
    assert reconcile([inv], [pay]).invoices["L6"].category is Category.MISSING_PAYMENT
    r = reconcile([inv], [pay], applied={"P1": "L6"})
    assert r.invoices["L6"].category is Category.MATCHED and r.orphans == {}
    assert reconcile([inv], [pay], applied={"P1": "NOPE"}).orphans  # a link to an unknown invoice changes nothing
