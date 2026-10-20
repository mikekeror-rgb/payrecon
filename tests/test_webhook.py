"""The webhook receiver: authentication, a durable inbox, de-duplication, verified successes, quarantine, replay.

Two payloads are real, captured from the live Daraja sandbox on 2026-10-19 (the cancelled push). The rest follow the
documented shapes."""
import http.client
import json
import threading
from decimal import Decimal

import pytest

from payrecon.adapters import Database, SQLITE_DDL, SqlLedger
from payrecon.adapters.fake_daraja import FakeDaraja, make_test_certificate
from payrecon.adapters.gateway import DarajaGateway
from payrecon.adapters.security import PiiBox
from payrecon.models import TxnStatus
from payrecon.providers.daraja import DarajaProvider
from payrecon.webhook import MAX_ATTEMPTS, WebhookService, check_token, make_server, payload_hash, sanitize

TOKEN = "t" * 12 + "-" + "k" * 14 + "_x"
REAL_CID = "ws_CO_191020261544137708374149"
REAL_MID = "ac7b-470b-8df7-23ab3cef38271862477"
REAL_1032 = {"Body": {"stkCallback": {"MerchantRequestID": REAL_MID, "CheckoutRequestID": REAL_CID,
                                      "ResultCode": 1032, "ResultDesc": "Request Cancelled by user."}}}


def success_cb(cid, receipt="NLJ7RT61SV", amount=1000, phone=254708374149):
    return {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": 0, "ResultDesc": "ok",
            "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                          {"Name": "TransactionDate", "Value": 20261019154413}, {"Name": "PhoneNumber", "Value": phone}]}}}}


def c2b(tid="RKTQDM7W6S", amount="1000.00", ref="INV-0003", **extra):
    return {"TransactionType": "Pay Bill", "TransID": tid, "TransTime": "20261019100000", "TransAmount": amount, "BillRefNumber": ref,
            "MSISDN": "254708374149", "FirstName": "John", "MiddleName": "J.", "LastName": "Doe", **extra}


class Rig:
    def __init__(self, fd, reversal=False, **svc):
        self.fd, self.db = fd, Database.sqlite()
        self.db.script(SQLITE_DDL)
        self.ledger = SqlLedger(self.db)
        prov = DarajaProvider("key", "secret", "174379", "passkey", base_url=fd.url)
        extra = dict(initiator="testapi", initiator_password="pw", cert_pem=make_test_certificate()[1],
                     result_url="https://example.com/r", timeout_url="https://example.com/t") if reversal else {}
        self.gw = DarajaGateway(prov, self.db, self.ledger, callback_url="https://example.com/cb", pii=PiiBox(None, allow_plaintext=True),
                                read_timeout=3.0, **extra)
        self.lines: list[str] = []
        self.svc = WebhookService(self.gw, self.db, TOKEN, log=self.lines.append, **svc)

    def push(self, key="k1", amount=1000, ref="INV-0001"):
        return self.gw.initiate(reference=ref, amount=Decimal(amount), msisdn="0708374149", idempotency_key=key)

    def post(self, kind, payload, **kw):
        return self.svc.handle("POST", f"/daraja/{kind}/{TOKEN}", json.dumps(payload).encode(), **kw)

    def inbox(self):
        return self.db.execute("SELECT kind, status, attempts, detail, payload FROM webhook_inbox ORDER BY received_at").fetchall()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM webhook_inbox").fetchone()[0]


# ---- the real captured callback -------------------------------------------------------------------

def test_the_real_cancellation_callback_is_stored_ingested_and_acknowledged():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        code, body = r.post("stk", REAL_1032)
        assert (code, body) == (200, {"ResultCode": 0, "ResultDesc": "Accepted"})
        t = r.ledger.get_txn(REAL_CID)
        assert (t.status, t.result_code, t.amount) == (TxnStatus.FAILED, "1032", Decimal(1))
        [(kind, status, attempts, detail, _)] = r.inbox()
        assert (kind, status, attempts) == ("stk", "INGESTED", 1) and "FAILED" in detail


def test_a_redelivery_is_recognised_and_changes_nothing():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        r.post("stk", REAL_1032)
        before = r.inbox()
        code, _ = r.post("stk", REAL_1032)
        assert code == 200 and r.inbox() == before and r.count() == 1  # still one row, still one attempt
        assert any("duplicate of an item already INGESTED" in line for line in r.lines)


def test_the_same_payload_posted_by_eight_threads_is_applied_once():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        results = []
        ts = [threading.Thread(target=lambda: results.append(r.post("stk", REAL_1032)[0])) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results == [200] * 8 and r.count() == 1
        assert r.db.execute("SELECT COUNT(*) FROM provider_transactions WHERE provider_txn_id = ?", (REAL_CID,)).fetchone()[0] == 1
        assert r.inbox()[0][2] == 1  # processed once, not once per delivery


# ---- who gets in -------------------------------------------------------------------------------------

def test_requests_that_are_not_authentic_malformed_or_oversized_are_refused_and_store_nothing():
    with FakeDaraja() as fd:
        r = Rig(fd, max_body=1024)
        ok = json.dumps(REAL_1032).encode()
        cases = [
            (r.svc.handle("POST", f"/daraja/stk/{'x' * 30}", ok), 404),  # wrong token
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN[:-1]}", ok), 404),  # one character short
            (r.svc.handle("POST", f"/daraja/nope/{TOKEN}", ok), 404),  # unknown kind
            (r.svc.handle("POST", "/daraja/stk", ok), 404),
            (r.svc.handle("POST", f"/{TOKEN}", ok), 404),
            (r.svc.handle("GET", f"/daraja/stk/{TOKEN}", b""), 405),
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN}", b"x" * 2000), 413),
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN}", b"", declared_length=999_999), 413),
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN}", b"not json"), 400),
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN}", b"\xff\xfe"), 400),
            (r.svc.handle("POST", f"/daraja/stk/{TOKEN}", b"[1, 2]"), 400),
        ]
        for (code, body), want in cases:
            assert code == want, (want, code, body)
            assert "ResultCode" not in body  # nothing that looks like an acknowledgement
        assert r.count() == 0
        assert r.db.execute("SELECT COUNT(*) FROM provider_transactions").fetchone()[0] == 0


def test_an_ip_allowlist_applies_to_the_socket_peer():
    with FakeDaraja() as fd:
        r = Rig(fd, allow_ips={"196.201.212.69"})
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        assert r.post("stk", REAL_1032, remote_addr="203.0.113.9")[0] == 403 and r.count() == 0
        assert r.post("stk", REAL_1032, remote_addr="196.201.212.69")[0] == 200 and r.count() == 1


def test_the_token_must_be_long_and_url_safe():
    for bad in (None, "", "short", "a" * 23, "a" * 30 + "/", "a" * 30 + " ", "a" * 30 + "?x=1"):
        with pytest.raises(ValueError):
            check_token(bad)
    assert check_token("a" * 24) == "a" * 24


# ---- a success is a claim until it is checked ----------------------------------------------------------

def test_a_success_that_matches_our_request_and_that_daraja_confirms_is_ingested():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push()
        fd.statuses[cid] = "SUCCESS"
        code, _ = r.post("stk", success_cb(cid))
        assert code == 200
        t = r.ledger.get_txn("NLJ7RT61SV")
        assert (t.status, t.amount, t.request_id, t.reference_raw) == (TxnStatus.SUCCESS, Decimal(1000), cid, "INV-0001")
        assert r.inbox()[0][1] == "INGESTED" and len(fd.calls_to("/mpesa/stkpushquery/v1/query")) == 1


def test_a_success_that_daraja_says_failed_is_quarantined_and_never_reaches_the_ledger():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push()
        fd.statuses[cid] = "FAILED"
        r.post("stk", success_cb(cid))
        assert r.inbox()[0][1] == "QUARANTINED" and "query says the request failed" in r.inbox()[0][3]
        assert r.ledger.get_txn("NLJ7RT61SV") is None


def test_a_success_with_the_wrong_amount_is_quarantined_before_daraja_is_even_asked():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push(amount=1000)
        fd.statuses[cid] = "SUCCESS"
        r.post("stk", success_cb(cid, amount=999))
        assert r.inbox()[0][1] == "QUARANTINED" and "differs from the 1000" in r.inbox()[0][3]
        assert r.ledger.get_txn("NLJ7RT61SV") is None and fd.calls_to("/mpesa/stkpushquery/v1/query") == []


def test_a_success_for_a_request_we_never_made_is_quarantined():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.post("stk", success_cb("ws_CO_never_asked_for_this"))
        assert r.inbox()[0][1] == "QUARANTINED" and "never recorded" in r.inbox()[0][3]
        assert r.db.execute("SELECT COUNT(*) FROM provider_transactions").fetchone()[0] == 0


def test_when_daraja_cannot_be_asked_the_success_waits_and_replay_finishes_it():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push()
        fd.statuses[cid] = "SUCCESS"
        fd.fault("/mpesa/stkpushquery/v1/query", "status", code=429, body={"fault": {"faultstring": "Spike arrest violation"}}, count=1)
        assert r.post("stk", success_cb(cid))[0] == 200  # Daraja still gets its acknowledgement
        assert r.inbox()[0][1] == "ERROR" and "rate-limited" in r.inbox()[0][3] and r.ledger.get_txn("NLJ7RT61SV") is None
        assert r.svc.replay() == {"INGESTED": 1}
        [(_, status, attempts, _, stored)] = r.inbox()
        assert (status, attempts) == ("INGESTED", 2)
        assert "254708374149" not in stored  # replayed from the masked copy
        assert r.ledger.get_txn("NLJ7RT61SV").status is TxnStatus.SUCCESS


def test_a_query_that_says_still_pending_is_not_enough_either():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push()
        fd.statuses[cid] = "PENDING"
        r.post("stk", success_cb(cid))
        assert r.inbox()[0][1] == "ERROR" and r.ledger.get_txn("NLJ7RT61SV") is None


def test_verification_can_be_turned_off_but_the_amount_check_stays():
    with FakeDaraja() as fd:
        r = Rig(fd, verify_success=False)
        cid = r.push(amount=1000)
        r.post("stk", success_cb(cid, receipt="A1", amount=1000))
        r.post("stk", success_cb(cid, receipt="A2", amount=5))
        assert [x[1] for x in r.inbox()] == ["INGESTED", "QUARANTINED"] and fd.calls_to("/mpesa/stkpushquery/v1/query") == []


# ---- C2B and reversals ----------------------------------------------------------------------------------

def test_c2b_is_ingested_once_and_a_conflicting_amount_is_quarantined():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.post("c2b", c2b())
        r.post("c2b", c2b(Extra="a redelivery with different bytes"))  # different hash, same TransID
        r.post("c2b", c2b(amount="999.00"))  # same TransID, different amount
        assert [x[1] for x in r.inbox()] == ["INGESTED", "INGESTED", "QUARANTINED"]
        assert r.db.execute("SELECT COUNT(*) FROM provider_transactions").fetchone()[0] == 1
        assert r.ledger.get_txn("RKTQDM7W6S").amount == Decimal("1000.00")


def test_stored_payloads_never_keep_phone_numbers_or_names():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push()
        fd.statuses[cid] = "SUCCESS"
        r.post("stk", success_cb(cid))
        r.post("c2b", c2b())
        for *_, stored in r.inbox():
            assert "254708374149" not in stored and "John" not in stored and "Doe" not in stored
        assert "2547****149" in r.inbox()[1][4] or "2547****149" in r.inbox()[0][4]
    nested = sanitize({"a": [{"Name": "PhoneNumber", "Value": 254708374149}, {"Name": "Amount", "Value": 5}], "MSISDN": "254708374149"})
    assert nested == {"a": [{"Name": "PhoneNumber", "Value": "2547****149"}, {"Name": "Amount", "Value": 5}], "MSISDN": "2547****149"}


def test_reversal_results_complete_the_refund_and_timeouts_are_only_noted():
    with FakeDaraja() as fd:
        r = Rig(fd, reversal=True)
        conv = r.gw.reverse("TX1", Decimal(1000), "r1")
        ok = {"Result": {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "OriginatorConversationID": "o", "ConversationID": conv}}
        r.post("reversal-timeout", {"Result": {"ResultCode": 1, "ResultDesc": "timeout"}})
        assert r.db.execute("SELECT status FROM refund_requests").fetchone()[0] == "ACCEPTED"
        r.post("reversal-result", ok)
        assert r.db.execute("SELECT status FROM refund_requests").fetchone()[0] == "COMPLETED"
        r.post("reversal-result", {"Result": {"ResultCode": 0, "ResultDesc": "ok", "ConversationID": "unknown", "OriginatorConversationID": "x"}})
        r.post("reversal-result", {"nonsense": True})
        assert [x[1] for x in r.inbox()] == ["NOTED", "INGESTED", "QUARANTINED", "QUARANTINED"]


# ---- when processing itself goes wrong -----------------------------------------------------------------

def test_a_failure_while_processing_is_recorded_acknowledged_and_replayable():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        real_ingest = r.ledger.ingest

        def broken(_):
            raise RuntimeError("disk full")

        r.ledger.ingest = broken
        assert r.post("stk", REAL_1032)[0] == 200  # Daraja is not told about our trouble
        assert r.inbox()[0][1] == "ERROR" and "disk full" in r.inbox()[0][3]
        r.ledger.ingest = real_ingest
        assert r.svc.replay() == {"INGESTED": 1} and r.ledger.get_txn(REAL_CID).status is TxnStatus.FAILED


def test_something_that_keeps_failing_is_quarantined_instead_of_retried_forever():
    with FakeDaraja() as fd:
        r = Rig(fd)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        r.ledger.ingest = lambda _: (_ for _ in ()).throw(RuntimeError("always"))
        r.post("stk", REAL_1032)
        for _ in range(MAX_ATTEMPTS + 2):
            r.svc.replay()
        [(_, status, attempts, detail, _)] = r.inbox()
        assert status == "QUARANTINED" and attempts == MAX_ATTEMPTS and "gave up" in detail
        assert r.svc.replay() == {}  # nothing is retried any more


def test_an_unrecognisable_but_authenticated_payload_is_stored_and_quarantined():
    with FakeDaraja() as fd:
        r = Rig(fd)
        assert r.post("stk", {"hello": "world"})[0] == 200
        assert r.post("stk", {"Body": {"stkCallback": {"CheckoutRequestID": "x"}}})[0] == 200
        assert [x[1] for x in r.inbox()] == ["QUARANTINED", "QUARANTINED"]


def test_the_hash_is_over_the_raw_payload_so_key_order_does_not_matter():
    assert payload_hash("stk", {"a": 1, "b": 2}) == payload_hash("stk", {"b": 2, "a": 1})
    assert payload_hash("stk", {"a": 1}) != payload_hash("c2b", {"a": 1})


# ---- over real HTTP ------------------------------------------------------------------------------------

def test_the_real_server_round_trip_never_logs_the_secret_and_survives_hostile_requests():
    with FakeDaraja() as fd:
        r = Rig(fd, max_body=1024)
        r.gw.record_request(REAL_CID, REAL_MID, "TEST4", Decimal(1), "254708374149", "k-real")
        srv = make_server(r.svc, "127.0.0.1", 0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            def call(method, path, body=b""):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request(method, path, body=body, headers={"Content-Type": "application/json"})
                resp = c.getresponse()
                out = resp.status, json.loads(resp.read() or b"{}")
                c.close()
                return out

            assert call("POST", f"/daraja/stk/{TOKEN}", json.dumps(REAL_1032).encode()) == (200, {"ResultCode": 0, "ResultDesc": "Accepted"})
            assert call("POST", f"/daraja/stk/{TOKEN}x", b"{}")[0] == 404
            assert call("GET", f"/daraja/stk/{TOKEN}")[0] == 405
            assert call("POST", f"/daraja/stk/{TOKEN}", b"y" * 2048)[0] == 413
            assert call("POST", f"/daraja/stk/{TOKEN}", b"garbage")[0] == 400
            assert call("POST", f"/daraja/stk/{TOKEN}", json.dumps(REAL_1032).encode())[0] == 200  # redelivery
            assert r.count() == 1 and r.ledger.get_txn(REAL_CID).status is TxnStatus.FAILED
            assert r.lines and all(TOKEN not in line and TOKEN[:10] not in line for line in r.lines)
        finally:
            srv.shutdown()
            srv.server_close()


# Captured from the live Daraja sandbox on 2026-10-19 (1 KES to a real handset, approved; phone masked by our own receiver).
# Note the "Balance" item with no Value, the masked phone stored as a string, and TransactionDate in local (EAT) time.
REAL_SUCCESS = {"Body": {"stkCallback": {"CallbackMetadata": {"Item": [
    {"Name": "Amount", "Value": 1}, {"Name": "MpesaReceiptNumber", "Value": "UJJA0AW035"}, {"Name": "Balance"},
    {"Name": "TransactionDate", "Value": 20261019212153}, {"Name": "PhoneNumber", "Value": "2547****166"}]},
    "CheckoutRequestID": "ws_CO_191020262121456722344166", "MerchantRequestID": "7bc0-4099-b32f-6e3e2a7eb21610049",
    "ResultCode": 0, "ResultDesc": "The service request is processed successfully."}}}


def test_the_real_success_callback_from_the_sandbox_is_verified_and_recorded_once():
    with FakeDaraja() as fd:
        r = Rig(fd)
        cid = r.push(amount=1, ref="TEST7")
        payload = json.loads(json.dumps(REAL_SUCCESS))
        payload["Body"]["stkCallback"]["CheckoutRequestID"] = cid
        fd.statuses[cid] = "SUCCESS"
        assert r.post("stk", payload)[0] == 200
        t = r.ledger.get_txn("UJJA0AW035")
        assert (t.status, t.amount, t.request_id) == (TxnStatus.SUCCESS, Decimal(1), cid)
        assert r.inbox()[0][1] == "INGESTED"
        assert r.post("stk", payload)[0] == 200 and len(r.inbox()) == 1  # Daraja redelivers: nothing changes


def test_c2b_validation_is_answered_with_accept_and_stores_nothing():
    with FakeDaraja() as fd:
        r = Rig(fd)
        code, body = r.post("c2b-validation", c2b())
        assert (code, body) == (200, {"ResultCode": "0", "ResultDesc": "Accepted"}) and r.inbox() == []
        assert r.svc.handle("POST", "/daraja/c2b-validation/" + "x" * 32, b"{}")[0] == 404


# Captured live on 2026-10-20 through this receiver (names already removed, phone masked by sanitize).
REAL_C2B = {"BillRefNumber": "INV-0001", "BusinessShortCode": "600978", "FirstName": "<removed>", "InvoiceNumber": "ThisInvoice",
            "LastName": "<removed>", "MSISDN": "2547****149", "MiddleName": "<removed>", "OrgAccountBalance": "2063027.60",
            "ThirdPartyTransID": "", "TransAmount": "1.00", "TransID": "UJK030FEJ6", "TransTime": "20261020000923",
            "TransactionType": "Pay Bill"}
REAL_REVERSAL_FAILED = {"Result": {"ConversationID": "AG_20261020_0100100300fiir7us4j0",
    "OriginatorConversationID": "4e7d-425c-aa3e-9b62f1dcf8a21882597",
    "ReferenceData": {"ReferenceItem": {"Key": "QueueTimeoutURL", "Value": "https://internalsandbox.safaricom.co.ke/mpesa/reversalresults/v1/submit"}},
    "ResultCode": 2001, "ResultDesc": "The initiator information is invalid.", "ResultType": 0, "TransactionID": "UJK03C0TTI"}}


def test_the_real_c2b_confirmation_is_ingested_with_its_reference_and_amount():
    with FakeDaraja() as fd:
        r = Rig(fd)
        assert r.post("c2b", REAL_C2B)[0] == 200
        t = r.ledger.get_txn("UJK030FEJ6")
        assert (t.status, t.amount, t.reference_raw, t.source) == (TxnStatus.SUCCESS, Decimal("1.00"), "INV-0001", "c2b_confirmation")
        assert r.inbox()[0][1] == "INGESTED"


def test_the_real_failed_reversal_result_marks_the_refund_failed_and_it_can_be_retried():
    with FakeDaraja() as fd:
        r = Rig(fd, reversal=True)
        conv = r.gw.reverse("UJK030FEJ6", Decimal(1), "rev-1")
        payload = json.loads(json.dumps(REAL_REVERSAL_FAILED))
        payload["Result"]["ConversationID"] = conv
        assert r.post("reversal-result", payload)[0] == 200
        row = r.db.execute("SELECT status, result_code, result_desc FROM refund_requests").fetchone()
        assert tuple(row) == ("FAILED", "2001", "The initiator information is invalid.")
        assert r.gw.reverse("UJK030FEJ6", Decimal(1), "rev-2")  # a failed reversal does not block a corrected second attempt
