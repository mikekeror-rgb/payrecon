from datetime import datetime
from decimal import Decimal

import pytest

from payrecon.models import TxnStatus
from payrecon.providers import (
    CsvStatementSource,
    DarajaProvider,
    PaymentProvider,
    ProviderError,
    StatementSource,
    StkRequest,
    mask_msisdn,
    write_transactions_csv,
)
from payrecon.providers.base import EAT
from payrecon.synthetic import generate

REQ = StkRequest("INV-0001", Decimal("1500"), "2547****149", datetime(2026, 10, 1, 9, 0, tzinfo=EAT))


def provider(**kw):
    return DarajaProvider("key", "secret", "174379", "passkey", stk_requests={"ws_CO_1": REQ}, **kw)


def stk_success():
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "29115-34620561-1",
                "CheckoutRequestID": "ws_CO_1",
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 1500.0},
                        {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
                        {"Name": "TransactionDate", "Value": 20261001091530},
                        {"Name": "PhoneNumber", "Value": 254708374149},
                    ]
                },
            }
        }
    }


def test_implements_the_interfaces():
    assert isinstance(provider(), PaymentProvider)
    assert isinstance(CsvStatementSource("x.csv"), StatementSource)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("254708374149", "2547****149"),
        ("+254 708 374 149", "2547****149"),
        (254708374149, "2547****149"),
        ("2547****149", "2547****149"),
        ("7d3c9a1bf0e24c", "hash:7d3c9a"),
        ("", None),
        (None, None),
    ],
)
def test_mask_msisdn(raw, expected):
    assert mask_msisdn(raw) == expected


def test_parse_stk_success_callback():
    t = provider().parse_event(stk_success())
    assert t.status is TxnStatus.SUCCESS
    assert t.provider_txn_id == "NLJ7RT61SV"
    assert t.amount == Decimal("1500")
    assert t.reference_raw == "INV-0001"  # recovered from our stored request, not the callback
    assert t.msisdn_masked == "2547****149"
    assert t.timestamp == datetime(2026, 10, 1, 9, 15, 30, tzinfo=EAT)


def test_stk_success_for_unknown_request_has_no_reference():
    p = DarajaProvider("k", "s", "174379", "pk")
    assert p.parse_event(stk_success()).reference_raw is None


def test_parse_stk_failure_callback_uses_stored_request():
    payload = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 1032, "ResultDesc": "Request cancelled by user"}}}
    t = provider().parse_event(payload)
    assert (t.status, t.result_code, t.amount, t.reference_raw) == (TxnStatus.FAILED, "1032", Decimal("1500"), "INV-0001")
    assert t.provider_txn_id == "ws_CO_1"
    assert t.result_desc == "Request cancelled by user"


def test_stk_failure_for_unknown_request_is_rejected():
    payload = {"Body": {"stkCallback": {"CheckoutRequestID": "nope", "ResultCode": 1037}}}
    with pytest.raises(ValueError):
        provider().parse_event(payload)


def test_parse_c2b_confirmation():
    payload = {
        "TransactionType": "Pay Bill",
        "TransID": "RKTQDM7W6S",
        "TransTime": "20261001101500",
        "TransAmount": "2,500.00",
        "BusinessShortCode": "600638",
        "BillRefNumber": "inv 7",
        "MSISDN": "254708374149",
    }
    t = provider().parse_event(payload)
    assert (t.provider_txn_id, t.amount, t.reference_raw, t.status) == ("RKTQDM7W6S", Decimal("2500.00"), "inv 7", TxnStatus.SUCCESS)
    assert t.msisdn_masked == "2547****149"


def test_unrecognised_payload_is_rejected():
    with pytest.raises(ValueError):
        provider().parse_event({"hello": "world"})


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"ResultCode": "0", "ResultDesc": "ok"}, TxnStatus.SUCCESS),
        ({"ResultCode": "1032", "ResultDesc": "cancelled"}, TxnStatus.FAILED),
        ({"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}, TxnStatus.PENDING),
    ],
)
def test_interpret_query_response(body, expected):
    assert DarajaProvider.interpret_query_response(200, body) is expected


def test_unknown_query_response_is_never_guessed():
    with pytest.raises(ProviderError):
        DarajaProvider.interpret_query_response(500, {"errorMessage": "something odd"})


def test_production_url_needs_explicit_opt_in():
    with pytest.raises(ValueError):
        DarajaProvider("k", "s", "1", "p", base_url="https://api.safaricom.co.ke")
    DarajaProvider("k", "s", "1", "p", base_url="https://api.safaricom.co.ke", allow_production=True)


def _stored_fields(t):
    # The statement format carries these fields only: no `source`, and a blank reference reads back as None.
    return (t.provider_txn_id, t.amount, t.status, t.timestamp, t.reference_raw or None, t.msisdn_masked, t.result_code,
            t.result_desc)


def test_csv_round_trip(tmp_path):
    ds = generate(60, seed=5)
    path = tmp_path / "t.csv"
    write_transactions_csv(ds.txns, path)
    loaded = CsvStatementSource(path).load()
    assert [_stored_fields(t) for t in loaded] == [_stored_fields(t) for t in ds.txns]
    assert {t.source for t in loaded} == {"statement"}


def test_csv_rejects_unknown_status(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("provider_txn_id,amount,status,timestamp\nA,100,BOUNCED,2026-10-01T09:00:00+03:00\n")
    with pytest.raises(ValueError, match="unknown status"):
        CsvStatementSource(path).load()


def test_csv_rejects_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("id,amount\nA,100\n")
    with pytest.raises(ValueError, match="missing required columns"):
        CsvStatementSource(path).load()


# ---- answers captured from the live Daraja sandbox (2026-10-19), for a push that was accepted ----

LIVE_429 = {"fault": {"faultstring": "Spike arrest violation. Allowed rate : MessageRate{messagesPerPeriod=30, periodInMicroseconds=60000000, maxBurstMessageCount=3.0}",
                      "detail": {"errorcode": "policies.ratelimit.SpikeArrestViolation"}}}
LIVE_500_WRONG_CREDS = {"requestId": "ws_CO_x", "errorCode": "500.001.1001", "errorMessage": "Wrong credentials"}
LIVE_500_EMPTY = {"requestId": "ws_CO_x", "errorCode": "500.001.1001", "errorMessage": ""}


def test_a_rate_limited_query_says_nothing_about_the_payment():
    from payrecon.providers.base import RateLimited
    with pytest.raises(RateLimited):
        DarajaProvider.interpret_query_response(429, LIVE_429)


@pytest.mark.parametrize("body", [LIVE_500_WRONG_CREDS, LIVE_500_EMPTY])
def test_the_live_500_answers_are_never_read_as_a_status(body):
    # The same code, 500.001.1001, came back with two different messages for one request, so neither the code nor
    # the text can be trusted to mean "still processing" or "failed". Unknown stays unknown.
    with pytest.raises(ProviderError):
        DarajaProvider.interpret_query_response(500, body)


# ---- a real STK callback captured from the live sandbox (2026-10-19): the test handset "cancelled" about 11 s after the push ----

LIVE_CALLBACK_1032 = {"Body": {"stkCallback": {
    "MerchantRequestID": "ac7b-470b-8df7-23ab3cef38271862477",
    "CheckoutRequestID": "ws_CO_191020261544137708374149",
    "ResultCode": 1032,
    "ResultDesc": "Request Cancelled by user."}}}


def test_the_live_cancellation_callback_parses_and_means_never_re_push():
    from decimal import Decimal
    from datetime import datetime
    from payrecon.models import TxnStatus
    from payrecon.policy import failure_action
    from payrecon.models import Action
    from payrecon.providers.daraja import StkRequest
    from payrecon.providers.base import EAT
    cid = "ws_CO_191020261544137708374149"
    prov = DarajaProvider("k", "s", "174379", "p", stk_requests={cid: StkRequest("TEST4", Decimal(1), "2547****149", datetime(2026, 10, 19, 15, 44, 13, tzinfo=EAT))})
    t = prov.parse_event(LIVE_CALLBACK_1032)
    assert (t.provider_txn_id, t.status, t.result_code, t.amount, t.reference_raw) == (cid, TxnStatus.FAILED, "1032", Decimal(1), "TEST4")
    assert t.result_desc == "Request Cancelled by user."
    assert failure_action(t.result_code, 0) is Action.NOTIFY_CUSTOMER  # the customer chose not to pay: tell them, never push again


def test_a_live_callback_for_a_request_we_never_recorded_is_refused_not_guessed():
    prov = DarajaProvider("k", "s", "174379", "p")
    with pytest.raises(ValueError, match="unknown CheckoutRequestID"):
        prov.parse_event(LIVE_CALLBACK_1032)


# ---- the live STK query answer for the same cancelled push (correct passkey, 2026-10-19) ----

LIVE_QUERY_1032 = {"ResponseCode": "0", "ResponseDescription": "The service request has been accepted successfully",
                   "MerchantRequestID": "ac7b-470b-8df7-23ab3cef38271862477", "CheckoutRequestID": "ws_CO_191020261544137708374149",
                   "ResultCode": "1032", "ResultDesc": "Request Cancelled by user."}


def test_the_live_query_answer_is_read_from_resultcode_not_responsecode():
    # ResponseCode "0" only says the query itself was accepted; the payment outcome is ResultCode.
    from payrecon.models import TxnStatus
    assert DarajaProvider.interpret_query_response(200, LIVE_QUERY_1032) is TxnStatus.FAILED
    assert DarajaProvider.interpret_query_response(200, {**LIVE_QUERY_1032, "ResultCode": "0", "ResultDesc": "ok"}) is TxnStatus.SUCCESS
