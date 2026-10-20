import base64
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from payrecon.adapters import Database, SQLITE_DDL, SqlLedger
from payrecon.adapters.fake_daraja import FakeDaraja
from payrecon.adapters.gateway import DarajaGateway, SqlStkRequests
from payrecon.adapters.security import PiiBox, normalize_msisdn, security_credential
from payrecon.matcher import reconcile
from payrecon.models import InternalStatus, Invoice, TxnStatus
from payrecon.providers.base import EAT, ProviderError
from payrecon.providers.daraja import DarajaProvider
from payrecon.recovery.ports import NotSentError, SendOutcomeUnknown

def rsa_cert():
    from payrecon.adapters.fake_daraja import make_test_certificate
    return make_test_certificate()


def stack(fd_url, **kw):
    db = Database.sqlite()
    db.script(SQLITE_DDL)
    led = SqlLedger(db)
    prov = DarajaProvider("key", "secret", "174379", "passkey", base_url=fd_url)
    opts = dict(callback_url="https://example.com/cb", pii=PiiBox(None, allow_plaintext=True), read_timeout=3.0)
    opts.update(kw)
    return DarajaGateway(prov, db, led, **opts), db, led


def reversal_stack(fd_url, **kw):
    _, pem = rsa_cert()
    return stack(fd_url, initiator="testapi", initiator_password="Safaricom999!*!", cert_pem=pem,
                 result_url="https://example.com/result", timeout_url="https://example.com/timeout", **kw)


def push(gw, key="k1", amount=1000, ref="INV-0001", phone="0708374149"):
    return gw.initiate(reference=ref, amount=Decimal(amount), msisdn=phone, idempotency_key=key)


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ---- STK push -----------------------------------------------------------------------------

def test_push_request_follows_the_documented_shape_and_is_recorded():
    with FakeDaraja() as fd:
        gw, db, led = stack(fd.url, pii=PiiBox(Fernet_key()))
        cid = push(gw)
        call = fd.calls_to("/mpesa/stkpush/")[0]
        b = call["json"]
        assert call["headers"]["Authorization"].startswith("Bearer tok-")
        assert b["BusinessShortCode"] == "174379" and b["PartyB"] == "174379"
        assert b["PartyA"] == b["PhoneNumber"] == "254708374149"
        assert b["Amount"] == 1000 and isinstance(b["Amount"], int)
        assert b["TransactionType"] == "CustomerPayBillOnline" and b["AccountReference"] == "INV-0001"
        assert b["CallBackURL"] == "https://example.com/cb" and len(b["TransactionDesc"]) <= 13
        assert base64.b64decode(b["Password"]).decode() == f"174379passkey{b['Timestamp']}"
        pending = led.get_txn(cid)
        assert pending.status is TxnStatus.PENDING and pending.reference_raw == "INV-0001" and pending.amount == Decimal(1000)
        sealed, masked = db.execute("SELECT msisdn_enc, msisdn_masked FROM stk_requests").fetchone()
        assert sealed.startswith("enc1:") and "708374149" not in sealed and masked == "2547****149"
        assert "254708374149" not in str(db.execute("SELECT * FROM provider_transactions").fetchall())


def Fernet_key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def test_the_same_key_never_pushes_twice():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        a, b = push(gw), push(gw)
        assert a == b and len(fd.calls_to("/mpesa/stkpush/")) == 1


@pytest.mark.parametrize("kw", [
    dict(amount="10.5"), dict(amount="0"), dict(amount="-5"), dict(phone="12345"), dict(phone="0912345678"),
    dict(ref="INV-123456789012345"), dict(ref=""),
])
def test_invalid_input_is_refused_before_anything_is_sent(kw):
    with FakeDaraja() as fd:
        gw, db, _ = stack(fd.url)
        with pytest.raises(NotSentError):
            push(gw, **kw)
        assert fd.calls_to("/mpesa/stkpush/") == [] and db.execute("SELECT COUNT(*) FROM stk_requests").fetchone()[0] == 0


def test_retry_push_goes_to_the_original_payer_with_the_original_reference():
    with FakeDaraja() as fd:
        gw, db, _ = stack(fd.url)
        first = push(gw, key="k1", ref="INV-0007", phone="+254 708 374 149")
        again = gw.send_push(retry_of=first, amount=Decimal(1000), idempotency_key="k2")
        assert again != first and len(fd.calls_to("/mpesa/stkpush/")) == 2
        b = fd.calls_to("/mpesa/stkpush/")[1]["json"]
        assert b["PartyA"] == "254708374149" and b["AccountReference"] == "INV-0007"
        assert db.execute("SELECT retry_of FROM stk_requests WHERE checkout_request_id = ?", (again,)).fetchone()[0] == first


def test_retry_of_an_unknown_request_is_refused_not_guessed():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        with pytest.raises(NotSentError, match="no stored payer"):
            gw.send_push(retry_of="ws_CO_unknown", amount=Decimal(1000), idempotency_key="k1")
        assert fd.calls_to("/mpesa/stkpush/") == []


# ---- how failures are classified -------------------------------------------------------------

NOT_SENT = [
    ("status", dict(code=400, body={"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PhoneNumber"})),
    ("status", dict(code=401, body={"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})),
    ("status", dict(code=429, body={"errorCode": "policies.ratelimit.SpikeArrestViolation"})),
    ("status", dict(code=200, body={"ResponseCode": "1", "ResponseDescription": "Declined"})),
]
UNKNOWN = [
    ("status", dict(code=500, body={"errorCode": "500.001.1001", "errorMessage": "Unable to lock subscriber"})),
    ("status", dict(code=502, body={})),
    ("status", dict(code=200, body={"ResponseCode": "0"})),  # accepted but no CheckoutRequestID
    ("status", dict(code=200, body={})),  # 200 without a ResponseCode
    ("drop", {}),  # connection closed after the request was read
    ("hang", dict(seconds=1.5)),  # no answer within the read timeout
]


@pytest.mark.parametrize("mode,kw", NOT_SENT)
def test_provable_rejections_are_not_sent(mode, kw):
    with FakeDaraja() as fd:
        gw, db, _ = stack(fd.url)
        fd.fault("/mpesa/stkpush/v1/processrequest", mode, **kw)
        with pytest.raises(NotSentError):
            push(gw)
        assert db.execute("SELECT COUNT(*) FROM stk_requests").fetchone()[0] == 0


@pytest.mark.parametrize("mode,kw", UNKNOWN)
def test_anything_that_may_have_been_processed_is_unknown(mode, kw):
    with FakeDaraja() as fd:
        gw, db, _ = stack(fd.url, read_timeout=0.4)
        fd.fault("/mpesa/stkpush/v1/processrequest", mode, **kw)
        with pytest.raises(SendOutcomeUnknown):
            push(gw)
        assert len(fd.calls_to("/mpesa/stkpush/")) == 1


def test_connection_refused_is_not_sent():
    gw, db, _ = stack(closed_port_url())
    with pytest.raises(NotSentError):
        push(gw)


def test_a_failed_token_request_is_not_sent():
    with FakeDaraja(consumer_secret="different") as fd:
        gw, *_ = stack(fd.url)
        with pytest.raises(NotSentError, match="access token"):
            push(gw)
        assert fd.calls_to("/mpesa/stkpush/") == []


def test_the_token_is_reused_between_calls():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        push(gw, key="a")
        push(gw, key="b")
        assert len(fd.calls_to("/oauth/")) == 1


def test_accepted_push_that_cannot_be_recorded_is_reported_unknown_with_its_id():
    with FakeDaraja() as fd:
        gw, db, led = stack(fd.url)
        gw.ledger.ingest = lambda t: (_ for _ in ()).throw(RuntimeError("disk full"))
        with pytest.raises(SendOutcomeUnknown, match="ws_CO_"):
            push(gw)
        assert len(fd.calls_to("/mpesa/stkpush/")) == 1


# ---- status query --------------------------------------------------------------------------

def test_status_query_maps_documented_and_assumed_answers():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        cid = push(gw)
        assert gw.get_status(cid) is TxnStatus.PENDING  # 500 'being processed' (assumed shape)
        fd.statuses[cid] = "SUCCESS"
        assert gw.get_status(cid) is TxnStatus.SUCCESS
        fd.statuses[cid] = "FAILED"
        assert gw.get_status(cid) is TxnStatus.FAILED


def test_status_query_never_guesses():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        with pytest.raises(ProviderError):
            gw.get_status("ws_CO_nope")  # 400 invalid id
        cid = push(gw)
        fd.fault("/mpesa/stkpushquery/v1/query", "status", code=200, body={"something": "else"})
        with pytest.raises(ProviderError):
            gw.get_status(cid)


# ---- reversal ------------------------------------------------------------------------------

def test_reversal_request_follows_the_documented_shape():
    key, _ = rsa_cert()
    with FakeDaraja() as fd:
        gw, db, _ = reversal_stack(fd.url)
        conv = gw.reverse("PDU91HIVT", Decimal(1000), "r1")
        b = fd.calls_to("/mpesa/reversal/")[0]["json"]
        assert b["CommandID"] == "TransactionReversal" and b["TransactionID"] == "PDU91HIVT" and b["Amount"] == 1000
        assert b["Initiator"] == "testapi" and b["ReceiverParty"] == "174379" and b["RecieverIdentifierType"] == "11"
        assert b["ResultURL"].startswith("https://") and b["QueueTimeOutURL"].startswith("https://") and 2 <= len(b["Remarks"]) <= 100
        from cryptography.hazmat.primitives.asymmetric import padding
        assert key.decrypt(base64.b64decode(b["SecurityCredential"]), padding.PKCS1v15()).decode() == "Safaricom999!*!"
        assert conv.startswith("AG_")
        assert db.execute("SELECT status, conversation_id FROM refund_requests").fetchone() == ("ACCEPTED", conv)


def test_reversal_is_at_most_once_per_key_and_per_transaction():
    with FakeDaraja() as fd:
        gw, *_ = reversal_stack(fd.url)
        conv = gw.reverse("TX1", Decimal(1000), "r1")
        assert gw.reverse("TX1", Decimal(1000), "r1") == conv and len(fd.calls_to("/mpesa/reversal/")) == 1
        with pytest.raises(NotSentError, match="already has a reversal"):
            gw.reverse("TX1", Decimal(1000), "r2")
        assert len(fd.calls_to("/mpesa/reversal/")) == 1


def test_a_clean_rejection_can_be_retried_under_a_new_key():
    with FakeDaraja() as fd:
        gw, db, _ = reversal_stack(fd.url)
        fd.fault("/mpesa/reversal/v1/request", "status", code=200, body={"ResponseCode": "1", "ResponseDescription": "Declined"})
        with pytest.raises(NotSentError):
            gw.reverse("TX1", Decimal(1000), "r1")
        assert db.execute("SELECT status FROM refund_requests WHERE idempotency_key='r1'").fetchone()[0] == "REJECTED"
        assert gw.reverse("TX1", Decimal(1000), "r2").startswith("AG_")


def test_an_ambiguous_reversal_blocks_every_retry_until_a_person_looks():
    with FakeDaraja() as fd:
        gw, db, _ = reversal_stack(fd.url)
        fd.fault("/mpesa/reversal/v1/request", "status", code=500, body={})
        with pytest.raises(SendOutcomeUnknown):
            gw.reverse("TX1", Decimal(1000), "r1")
        assert db.execute("SELECT status FROM refund_requests").fetchone()[0] == "SENDING"
        with pytest.raises(SendOutcomeUnknown):
            gw.reverse("TX1", Decimal(1000), "r1")  # same key
        with pytest.raises(NotSentError, match="already has a reversal"):
            gw.reverse("TX1", Decimal(1000), "r2")  # new key, same payment
        assert len(fd.calls_to("/mpesa/reversal/")) == 1


def test_reversal_refuses_to_run_unconfigured_or_with_a_bad_certificate():
    with FakeDaraja() as fd:
        gw, *_ = stack(fd.url)
        with pytest.raises(NotSentError, match="not configured"):
            gw.reverse("TX1", Decimal(1000), "r1")
        gw2, *_ = stack(fd.url, initiator="i", initiator_password="p", cert_pem=b"not a cert", result_url="https://x", timeout_url="https://y")
        with pytest.raises(NotSentError, match="SecurityCredential"):
            gw2.reverse("TX1", Decimal(1000), "r1")
        assert fd.calls_to("/mpesa/reversal/") == []


def test_reversal_result_callback_completes_or_fails_the_request_once():
    with FakeDaraja() as fd:
        gw, db, _ = reversal_stack(fd.url)
        conv = gw.reverse("TX1", Decimal(1000), "r1")
        ok = {"Result": {"ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
                         "OriginatorConversationID": "o", "ConversationID": conv, "TransactionID": "SKE52PAWR9"}}
        assert gw.ingest_reversal_result(ok) is True
        assert gw.ingest_reversal_result(ok) is False  # redelivery changes nothing
        assert db.execute("SELECT status, result_code FROM refund_requests").fetchone() == ("COMPLETED", "0")
        assert gw.ingest_reversal_result({"Result": {"ResultCode": "R000002", "ResultDesc": "x", "ConversationID": "nope"}}) is False
        conv2 = gw.reverse("TX2", Decimal(500), "r2")
        assert gw.ingest_reversal_result({"Result": {"ResultCode": "R000002", "ResultDesc": "insufficient", "ConversationID": conv2}}) is True
        assert db.execute("SELECT status FROM refund_requests WHERE idempotency_key='r2'").fetchone()[0] == "FAILED"
        with pytest.raises(ValueError):
            gw.ingest_reversal_result({"nope": 1})


# ---- callbacks -----------------------------------------------------------------------------

def stk_success(cid, receipt="NLJ7RT61SV", amount=1000, phone=254708374149):
    return {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": 0,
            "ResultDesc": "The service request is processed successfully.", "CallbackMetadata": {"Item": [
                {"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20261019101530}, {"Name": "PhoneNumber", "Value": phone}]}}}}


def stk_failure(cid, code=1037):
    return {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": code,
            "ResultDesc": "DS timeout user cannot be reached"}}}


def test_success_callback_folds_into_the_pending_push_and_reconciles_to_auto_apply():
    with FakeDaraja() as fd:
        gw, db, led = stack(fd.url)
        cid = push(gw)
        txn, outcome = gw.handle_callback(stk_success(cid))
        assert outcome == "inserted" and txn.request_id == cid and txn.reference_raw == "INV-0001" and txn.msisdn_masked == "2547****149"
        inv = Invoice("I1", "INV-0001", Decimal(1000), InternalStatus.UNPAID, datetime.now(EAT))
        res = reconcile([inv], led.txns_for_reference("INV-0001")).invoices["I1"]
        assert res.txn_ids == ["NLJ7RT61SV"] and res.action.value == "AUTO_APPLY"
        assert gw.handle_callback(stk_success(cid))[1] == "duplicate"  # redelivery


def test_failure_callback_turns_the_pending_row_into_a_failure_with_its_code():
    with FakeDaraja() as fd:
        gw, _, led = stack(fd.url)
        cid = push(gw)
        _, outcome = gw.handle_callback(stk_failure(cid, 1037))
        row = led.get_txn(cid)
        assert outcome == "upgraded" and row.status is TxnStatus.FAILED and row.result_code == "1037" and "timeout" in row.result_desc


def test_callback_for_a_request_we_never_made_is_rejected_not_guessed():
    with FakeDaraja() as fd:
        gw, _, led = stack(fd.url)
        with pytest.raises(ValueError, match="unknown CheckoutRequestID"):
            gw.handle_callback(stk_failure("ws_CO_stranger"))
        assert led.all_txns() == []


def test_c2b_confirmation_is_stored_with_the_reference_the_payer_typed():
    with FakeDaraja() as fd:
        gw, _, led = stack(fd.url)
        txn, outcome = gw.handle_callback({"TransID": "RKTQDM7W6S", "TransAmount": "1000.00", "BillRefNumber": "inv 7",
                                           "MSISDN": "254708374149", "TransTime": "20261019101530"})
        assert outcome == "inserted" and txn.amount == Decimal("1000.00") and [t.provider_txn_id for t in led.txns_for_reference("INV-0007")] == ["RKTQDM7W6S"]


def test_stk_request_mapping_reads_the_store():
    with FakeDaraja() as fd:
        gw, db, _ = stack(fd.url)
        cid = push(gw)
        m = SqlStkRequests(db)
        assert len(m) == 1 and cid in m and m[cid].reference == "INV-0001" and m[cid].amount == Decimal(1000)
        assert m.get("nope") is None


# ---- secrets -------------------------------------------------------------------------------

def test_phone_numbers_normalise_and_bad_ones_are_refused():
    assert {normalize_msisdn(x) for x in ("0708374149", "708374149", "+254708374149", "254 708-374-149")} == {"254708374149"}
    for bad in ("", "0912345678", "25470837414", "abc", "254708374149999"):
        with pytest.raises(ValueError):
            normalize_msisdn(bad)


def test_pii_box_refuses_plaintext_unless_told_and_wrong_keys_cannot_open():
    with pytest.raises(RuntimeError, match="PAYRECON_PII_KEY"):
        PiiBox(None)
    box, other = PiiBox(Fernet_key()), PiiBox(Fernet_key())
    sealed = box.seal("254708374149")
    assert sealed.startswith("enc1:") and box.open(sealed) == "254708374149"
    with pytest.raises(Exception):
        other.open(sealed)
    with pytest.raises(RuntimeError):
        PiiBox(None, allow_plaintext=True).open(sealed)
    assert PiiBox(None, allow_plaintext=True).open(PiiBox(None, allow_plaintext=True).seal("x")) == "x"


def test_security_credential_is_not_deterministic_and_decrypts():
    key, pem = rsa_cert()
    a, b = security_credential("pw", pem), security_credential("pw", pem)
    from cryptography.hazmat.primitives.asymmetric import padding
    assert a != b  # PKCS#1 v1.5 padding is randomised
    assert key.decrypt(base64.b64decode(a), padding.PKCS1v15()) == b"pw"
