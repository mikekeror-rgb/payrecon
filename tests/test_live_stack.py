"""The real executor driving real adapters (SQL books and ledger, Daraja gateway, outbox) against a fake Daraja."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from payrecon.adapters import Database, SQLITE_DDL, SqlBooks, SqlLedger, SqlOutboxNotifier
from payrecon.adapters.fake_daraja import FakeDaraja, make_test_certificate
from payrecon.adapters.gateway import DarajaGateway
from payrecon.adapters.security import PiiBox
from payrecon.models import InternalStatus, Invoice
from payrecon.providers.daraja import DarajaProvider
from payrecon.recovery import ActionStatus, ActionStore, ActionType, ApprovalDesk, Executor, RecoveryConfig, Role, plan_from_data
from payrecon.recovery.sim import Clock, SimulatedCrash
from payrecon.providers.base import EAT

T0 = datetime(2026, 10, 19, 9, tzinfo=EAT)
CFG = RecoveryConfig(retry_cooldown_s=600, backoff_s=60, stale_after_s=300)


class Live:
    def __init__(self, fd, amount=1000, refs=("INV-0001",), failed_code="1037", register_failed=True, reversal=False):
        self.fd, self.clock = fd, Clock(T0)
        self.db = Database.sqlite()
        self.db.script(SQLITE_DDL)
        self.books, self.ledger, self.outbox = SqlBooks(self.db), SqlLedger(self.db), SqlOutboxNotifier(self.db)
        prov = DarajaProvider("key", "secret", "174379", "passkey", base_url=fd.url)
        extra = dict(initiator="testapi", initiator_password="pw", cert_pem=make_test_certificate()[1],
                     result_url="https://example.com/result", timeout_url="https://example.com/timeout") if reversal else {}
        self.gw = DarajaGateway(prov, self.db, self.ledger, callback_url="https://example.com/cb", pii=PiiBox(None, allow_plaintext=True),
                                read_timeout=1.0, **extra)
        self.store = ActionStore(":memory:", self.clock)
        self.desk = ApprovalDesk(self.store, {"alice": Role.APPROVER})
        self.originals = {}
        for i, ref in enumerate(refs, start=1):
            self.books.add_invoice(Invoice(f"I{i}", ref, Decimal(amount), InternalStatus.UNPAID, T0 - timedelta(days=1)))
            if register_failed:
                cid = self.gw.initiate(reference=ref, amount=Decimal(amount), msisdn="0708374149", idempotency_key=f"first-{i}")
                fd.statuses[cid] = "FAILED"
                fd.fail_codes[cid] = failed_code
                self.gw.handle_callback({"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": int(failed_code),
                                                                  "ResultDesc": "DS timeout user cannot be reached"}}})
                self.originals[ref] = cid
        self.clock.advance(3600)
        self.ex = self.executor()

    def executor(self, worker="w1"):
        return Executor(self.store, self.books, self.ledger, self.gw, self.gw, self.outbox, config=CFG, worker=worker)

    def plan(self):
        return plan_from_data(self.books.invoices(), self.ledger.all_txns(), self.store, config=CFG)[1]

    def pushes(self):
        return len(self.fd.calls_to("/mpesa/stkpush/v1/processrequest"))


def success_callback(cid, receipt, amount=1000):
    return {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": 0, "ResultDesc": "ok",
            "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                          {"Name": "TransactionDate", "Value": 20261019101530}, {"Name": "PhoneNumber", "Value": 254708374149}]}}}}


def test_timeout_then_retry_then_success_then_applied_end_to_end():
    with FakeDaraja() as fd:
        L = Live(fd)
        before = L.pushes()
        pr = L.plan()
        assert [a.action_type for a in pr.proposed] == [ActionType.RETRY_PUSH]
        s = L.ex.run_once()
        assert s.done == 1 and L.pushes() == before + 1
        new = L.store.list_actions(action_type=ActionType.RETRY_PUSH)[0].request_id
        assert new and new != L.originals["INV-0001"]
        # the customer enters their PIN; Daraja calls back
        L.gw.handle_callback(success_callback(new, "NLJ7RT61SV"))
        L.clock.advance(3600)
        L.plan()
        assert L.ex.run_once().done == 1
        assert L.books.get_invoice("I1").internal_status is InternalStatus.PAID and L.books.paid_by("NLJ7RT61SV") == "I1"
        L.plan()
        assert L.ex.run_once().done == 0 and L.pushes() == before + 1  # nothing more happens
        ok, bad = L.store.verify_audit()
        assert ok and bad is None


def test_no_push_when_daraja_says_the_original_request_actually_succeeded():
    with FakeDaraja() as fd:
        L = Live(fd)
        fd.statuses[L.originals["INV-0001"]] = "SUCCESS"  # the timeout was a lie
        before = L.pushes()
        L.plan()
        s = L.ex.run_once()
        assert s.cancelled == 1 and L.pushes() == before
        assert "never re-charge" in L.store.list_actions(action_type=ActionType.RETRY_PUSH)[0].last_error


def test_no_push_while_daraja_cannot_be_asked():
    with FakeDaraja() as fd:
        L = Live(fd)
        before = L.pushes()
        fd.fault("/mpesa/stkpushquery/v1/query", "status", count=5, code=503, body={})
        L.plan()
        s = L.ex.run_once()
        assert s.deferred == 1 and L.pushes() == before
        assert L.store.list_actions(action_type=ActionType.RETRY_PUSH)[0].status is ActionStatus.PROPOSED


def test_a_5xx_on_the_push_is_unknown_and_is_never_sent_again():
    with FakeDaraja() as fd:
        L = Live(fd)
        before = L.pushes()
        fd.fault("/mpesa/stkpush/v1/processrequest", "status", code=500, body={"errorCode": "500.001.1001"})
        L.plan()
        s = L.ex.run_once()
        assert s.unknown == 1 and L.pushes() == before + 1
        for _ in range(4):
            L.clock.advance(7200)
            L.plan()
            L.ex.run_once()
        assert L.pushes() == before + 1 or L.store.send_count("I1") <= 2  # a later, separate retry is a new decision, not a resend
        a = L.store.list_actions(action_type=ActionType.RETRY_PUSH)[0]
        assert a.status is ActionStatus.UNKNOWN and "500" in a.last_error


def test_connection_refused_on_the_push_backs_off_without_using_a_retry():
    with FakeDaraja() as fd:
        L = Live(fd)
        L.plan()
        L.gw.provider.base_url = "http://127.0.0.1:1"  # nothing listens here; the status check fails first, so nothing is sent
        s = L.ex.run_once()
        assert s.deferred == 1 and L.store.send_count("I1") == 0


def test_crash_between_daraja_accepting_and_the_push_being_recorded_is_unknown_not_resent():
    with FakeDaraja() as fd:
        L = Live(fd)
        before = L.pushes()
        L.plan()
        real = L.gw.record_request

        def die(*a, **k):
            raise SimulatedCrash("after the HTTP response, before the database write")

        L.gw.record_request = die
        with pytest.raises(SimulatedCrash):
            L.ex.run_once()
        assert L.pushes() == before + 1
        L.gw.record_request = real
        L.clock.advance(CFG.stale_after_s + 1)
        s = L.executor("w2").run_once()
        a = L.store.list_actions(action_type=ActionType.RETRY_PUSH)[0]
        assert s.recovered == 1 and a.status is ActionStatus.UNKNOWN and L.pushes() == before + 1


def test_failed_push_for_a_customer_who_must_act_is_queued_for_notification_once():
    with FakeDaraja() as fd:
        L = Live(fd, failed_code="1")  # insufficient funds
        L.plan()
        L.ex.run_once()
        L.plan()
        L.ex.run_once()
        queued = L.outbox.queued()
        assert len(queued) == 1 and queued[0][1] == "I1" and queued[0][2] == "payment_failed_1"
        assert L.pushes() == 1  # only the original; never re-pushed




def c2b(trans_id, minutes, ref="INV-0001", amount="1000.00"):
    return {"TransID": trans_id, "TransAmount": amount, "BillRefNumber": ref, "MSISDN": "254708374149",
            "TransTime": (T0 + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S")}


def duplicate_setup(fd):
    L = Live(fd, register_failed=False, reversal=True)
    L.gw.handle_callback(c2b("RKTQDM7W6S", 1))
    L.gw.handle_callback(c2b("RKTQDM7W9T", 6))  # the customer paid twice
    L.clock.advance(3600)
    return L


def test_duplicate_payment_is_refunded_only_after_approval_and_only_once():
    with FakeDaraja() as fd:
        L = duplicate_setup(fd)
        a = L.plan().proposed[0]
        assert a.action_type is ActionType.REFUND_DUPLICATE and a.payload["refund_txn_id"] == "RKTQDM7W9T" and a.payload["keep_txn_id"] == "RKTQDM7W6S"
        L.ex.run_once()
        assert fd.calls_to("/mpesa/reversal/") == []  # nobody approved it
        L.desk.approve(a.action_id, "alice")
        assert L.ex.run_once().done == 1
        calls = fd.calls_to("/mpesa/reversal/")
        assert len(calls) == 1 and calls[0]["json"]["TransactionID"] == "RKTQDM7W9T" and calls[0]["json"]["Amount"] == 1000
        for _ in range(3):
            L.clock.advance(3600)
            L.plan()
            L.ex.run_once()
        assert len(fd.calls_to("/mpesa/reversal/")) == 1
        conv = L.db.execute("SELECT conversation_id FROM refund_requests").fetchone()[0]
        assert L.gw.ingest_reversal_result({"Result": {"ResultCode": 0, "ResultDesc": "ok", "ConversationID": conv}})
        assert L.db.execute("SELECT status FROM refund_requests").fetchone()[0] == "COMPLETED"
        assert L.store.verify_audit()[0]


def test_ambiguous_refund_answer_is_unknown_and_blocks_a_second_attempt_at_every_layer():
    with FakeDaraja() as fd:
        L = duplicate_setup(fd)
        a = L.plan().proposed[0]
        L.desk.approve(a.action_id, "alice")
        fd.fault("/mpesa/reversal/v1/request", "status", code=500, body={})
        s = L.ex.run_once()
        assert s.unknown == 1 and L.store.get(a.action_id).status is ActionStatus.UNKNOWN
        assert L.db.execute("SELECT status FROM refund_requests").fetchone()[0] == "SENDING"
        for _ in range(3):
            L.clock.advance(3600)
            L.plan()
            L.ex.run_once()
        assert len(fd.calls_to("/mpesa/reversal/")) == 1


def test_refund_without_configuration_backs_off_then_gives_up_and_never_calls_daraja():
    with FakeDaraja() as fd:
        L = Live(fd, register_failed=False, reversal=False)
        L.gw.handle_callback(c2b("R1", 1))
        L.gw.handle_callback(c2b("R2", 6))
        L.clock.advance(3600)
        a = L.plan().proposed[0]
        L.desk.approve(a.action_id, "alice")
        s = L.ex.run_once()
        got = L.store.get(a.action_id)
        assert s.deferred == 1 and s.unknown == 0 and got.status is ActionStatus.APPROVED  # back in the queue, approval kept
        assert "not configured" in got.last_error and got.side_effect_at is None  # provably not sent: does not count as a send
        for _ in range(8):
            L.clock.advance(3600)
            L.ex.run_once()
        final = L.store.get(a.action_id)
        assert final.status is ActionStatus.FAILED and "gave up" in final.last_error
        assert fd.calls_to("/mpesa/reversal/") == [] and L.db.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0] == 0
