"""Daraja gateway: STK push, status query, transaction reversal and callback ingestion.

Implements recovery.ports.PushPort and RefundPort over HTTP. Field names and endpoints follow Safaricom's
public Daraja documentation (STK push, STK push query, Reversal). It has been exercised against a local fake
that mimics those documented shapes, NOT against the live sandbox: use `python -m payrecon.recover
sandbox-check` with your credentials to compare real responses with the assumptions below.

How outcomes are classified, because the executor's safety depends on it:

* Provably not sent (raise NotSentError, safe to try again later): no token, bad input, connection refused,
  DNS or TLS failure, HTTP 4xx (including 401/403/429), or HTTP 200 with a non-zero ResponseCode.
* May have been sent (raise SendOutcomeUnknown, a person decides): read timeout, connection reset, any
  other transport error after the request left, HTTP 5xx, a 200 that lacks the ids we need, or failing to
  record a push that Daraja accepted. 5xx is classed unknown on purpose: Daraja sometimes answers a
  business rejection with 500, but we cannot tell that from a failure after processing.
"""
from __future__ import annotations

import base64
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

from ..models import ProviderTxn, TxnStatus
from ..providers.base import EAT, ProviderError, mask_msisdn
from ..providers.daraja import DarajaProvider, StkRequest
from ..recovery.ports import NotSentError, SendOutcomeUnknown
from .db import Database, to_dec, to_dt
from .ledger import SqlLedger
from .security import PiiBox, normalize_msisdn, security_credential

ACCOUNT_REF_MAX = 12  # Daraja: AccountReference max 12 characters
TXN_DESC_MAX = 13  # Daraja: TransactionDesc max 13 characters
_LIVE_REFUND = ("SENDING", "ACCEPTED", "COMPLETED")


class SqlStkRequests(Mapping):
    """Read-only view of stk_requests for DarajaProvider.parse_event (callbacks carry no reference or amount)."""

    def __init__(self, db: Database):
        self.db = db

    def __getitem__(self, checkout_id: str) -> StkRequest:
        r = self.db.execute(
            "SELECT invoice_reference, amount, msisdn_masked, requested_at FROM stk_requests WHERE checkout_request_id = ?", (checkout_id,)
        ).fetchone()
        if r is None:
            raise KeyError(checkout_id)
        return StkRequest(reference=r[0], amount=to_dec(r[1]), msisdn_masked=r[2], requested_at=to_dt(r[3]))

    def __iter__(self) -> Iterator[str]:
        return iter(r[0] for r in self.db.execute("SELECT checkout_request_id FROM stk_requests").fetchall())

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM stk_requests").fetchone()[0]


def _provably_not_sent(exc: Exception) -> bool:
    """True only for failures that happen before any bytes of the request can have reached the server."""
    import requests
    from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError, SSLError as Urllib3SSLError

    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.SSLError):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        for arg in exc.args:
            if isinstance(arg, MaxRetryError) and isinstance(arg.reason, (NewConnectionError, ConnectTimeoutError, Urllib3SSLError)):
                return True
    return False


class DarajaGateway:
    def __init__(
        self,
        provider: DarajaProvider,
        db: Database,
        ledger: SqlLedger,
        *,
        callback_url: str,
        pii: PiiBox,
        transaction_type: str = "CustomerPayBillOnline",
        initiator: str | None = None,
        initiator_password: str | None = None,
        cert_pem: bytes | None = None,
        result_url: str | None = None,
        timeout_url: str | None = None,
        security_credential: str | None = None,
        reversal_shortcode: str | None = None,
        b2c_shortcode: str | None = None,
        b2c_command: str = "BusinessPayment",
        b2c_result_url: str | None = None,
        b2c_timeout_url: str | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
    ):
        self.provider, self.db, self.ledger, self.pii = provider, db, ledger, pii
        self.callback_url, self.transaction_type = callback_url, transaction_type
        self.initiator, self.initiator_password, self.cert_pem = initiator, initiator_password, cert_pem
        self.result_url, self.timeout_url = result_url, timeout_url
        # The shortcode that received the payments being reversed. Reversals are documented for C2B, whose paybill is not
        # the STK shortcode: sending 174379 for a 600978 payment got 2001 "The initiator information is invalid" live.
        self.reversal_shortcode = reversal_shortcode
        self.b2c_shortcode, self.b2c_command = b2c_shortcode, b2c_command
        self.b2c_result_url, self.b2c_timeout_url = b2c_result_url, b2c_timeout_url
        self.security_credential = security_credential  # ready-made (e.g. from the portal); else built from password + certificate
        self.timeouts = (connect_timeout, read_timeout)
        provider.stk_requests = SqlStkRequests(db)  # so callbacks can recover reference and amount
        self._lock = threading.Lock()
        self.last_response: str | None = None  # redacted raw answer to the latest POST, for sandbox-check

    # ---- PushPort -------------------------------------------------------------------

    def get_status(self, request_id: str) -> TxnStatus:
        return self.provider.get_status(request_id)

    def send_push(self, *, retry_of: str, amount: Decimal, idempotency_key: str) -> str:
        """Re-push to the payer of an earlier request. The number comes from our own sealed store, never from the caller."""
        row = self.db.execute("SELECT invoice_reference, msisdn_enc FROM stk_requests WHERE checkout_request_id = ?", (retry_of,)).fetchone()
        if row is None:
            raise NotSentError(f"no stored payer for request {retry_of}; cannot re-push (was it initiated outside this gateway?)")
        return self.initiate(reference=row[0], amount=amount, msisdn=self.pii.open(row[1]), idempotency_key=idempotency_key, retry_of=retry_of)

    def initiate(self, *, reference: str, amount: Decimal, msisdn: str, idempotency_key: str, retry_of: str | None = None) -> str:
        """Start an STK push and record it. Returns the CheckoutRequestID."""
        # Second line of defence (the executor's write-ahead marker is the first): one push per key, ever.
        done = self.db.execute("SELECT checkout_request_id FROM stk_requests WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if done:
            return done[0]
        whole = self._whole_shillings(amount)
        try:
            phone = normalize_msisdn(msisdn)
        except ValueError as e:
            raise NotSentError(str(e)) from None
        if not reference or len(reference) > ACCOUNT_REF_MAX:
            raise NotSentError(f"reference {reference!r} does not fit AccountReference (max {ACCOUNT_REF_MAX} characters)")

        body = self.build_push_body(reference, whole, phone)
        resp = self._post("/mpesa/stkpush/v1/processrequest", body)
        checkout, merchant = resp.get("CheckoutRequestID"), resp.get("MerchantRequestID")
        if not checkout:
            raise SendOutcomeUnknown(f"Daraja accepted the push but returned no CheckoutRequestID: {self._safe(resp)}")
        try:  # if this fails the push is out there and we cannot find it again: say so, never pretend
            self.record_request(checkout, merchant, reference, Decimal(whole), phone, idempotency_key, retry_of)
        except Exception as e:
            raise SendOutcomeUnknown(f"push {checkout} was accepted but could not be recorded: {type(e).__name__}: {e}") from e
        return checkout

    def build_push_body(self, reference: str, whole: int, phone: str, ts: str | None = None) -> dict[str, Any]:
        ts = ts or datetime.now(EAT).strftime("%Y%m%d%H%M%S")
        return {
            "BusinessShortCode": self.provider.shortcode,
            "Password": base64.b64encode(f"{self.provider.shortcode}{self.provider.passkey}{ts}".encode()).decode(),
            "Timestamp": ts,
            "TransactionType": self.transaction_type,
            "Amount": whole,
            "PartyA": phone,
            "PartyB": self.provider.shortcode,
            "PhoneNumber": phone,
            "CallBackURL": self.callback_url,
            "AccountReference": reference,
            "TransactionDesc": "Payment"[:TXN_DESC_MAX],
        }

    def record_request(self, checkout_id: str, merchant_id: str | None, reference: str, amount: Decimal, msisdn: str,
                       idempotency_key: str | None, retry_of: str | None = None) -> None:
        """Register a push (also for pushes your application starts itself, so callbacks and retries can find them)."""
        phone = normalize_msisdn(msisdn)
        now = datetime.now(timezone.utc)
        masked = mask_msisdn(phone)
        with self.db.tx():
            self.db.execute(
                "INSERT INTO stk_requests (checkout_request_id, merchant_request_id, idempotency_key, invoice_reference, amount, "
                "msisdn_enc, msisdn_masked, retry_of, requested_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (checkout_id, merchant_id, idempotency_key, reference, amount, self.pii.seal(phone), masked, retry_of, now),
            )
            self.ledger.ingest(ProviderTxn(checkout_id, amount, TxnStatus.PENDING, now, reference, masked, None, "stk_push", None, None))

    # ---- RefundPort -----------------------------------------------------------------

    def reverse(self, provider_txn_id: str, amount: Decimal, idempotency_key: str) -> str:
        """Ask Daraja to reverse a payment. 'Accepted' is all this can mean: the outcome arrives later on ResultURL
        (see ingest_reversal_result). Returns the ConversationID."""
        need = ("initiator", "result_url", "timeout_url") + (() if self.security_credential else ("initiator_password", "cert_pem"))
        for name in need:
            if not getattr(self, name):
                raise NotSentError(f"reversal is not configured ({name} missing; or set a ready-made security credential)")
        whole = self._whole_shillings(amount)
        if self.security_credential:
            credential = self.security_credential
        else:
            try:
                credential = security_credential(self.initiator_password, self.cert_pem)
            except Exception as e:  # bad certificate: nothing was sent
                raise NotSentError(f"could not build the SecurityCredential: {type(e).__name__}") from None
        now = datetime.now(timezone.utc)
        with self.db.tx():
            row = self.db.execute("SELECT status, conversation_id FROM refund_requests WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                if row[0] in ("ACCEPTED", "COMPLETED"):
                    return row[1]
                if row[0] == "SENDING":
                    raise SendOutcomeUnknown("an earlier attempt under this key may have reached Daraja; check before retrying")
                raise NotSentError(f"an earlier attempt under this key ended {row[0]}")
            live = self.db.execute(
                "SELECT status FROM refund_requests WHERE provider_txn_id = ? AND status IN ('SENDING','ACCEPTED','COMPLETED')", (provider_txn_id,)
            ).fetchone()
            if live:
                raise NotSentError(f"transaction {provider_txn_id} already has a reversal ({live[0]})")
            self.db.execute(
                "INSERT INTO refund_requests (idempotency_key, provider_txn_id, amount, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (idempotency_key, provider_txn_id, Decimal(whole), "SENDING", now, now),
            )
        body = {
            "Initiator": self.initiator,
            "SecurityCredential": credential,
            "CommandID": "TransactionReversal",
            "TransactionID": provider_txn_id,
            "Amount": whole,
            "ReceiverParty": self.reversal_shortcode or self.provider.shortcode,
            "RecieverIdentifierType": "11",  # sic: Daraja's documented spelling
            "ResultURL": self.result_url,
            "QueueTimeOutURL": self.timeout_url,
            "Remarks": "Duplicate payment",
        }
        try:
            resp = self._post("/mpesa/reversal/v1/request", body)
        except NotSentError:
            self._refund_update(idempotency_key, "REJECTED")  # a clean rejection stays retryable under a new key
            raise
        conv = resp.get("ConversationID")
        if not conv:
            raise SendOutcomeUnknown(f"Daraja accepted the reversal but returned no ConversationID: {self._safe(resp)}")
        self._refund_update(idempotency_key, "ACCEPTED", conversation_id=conv,
                            originator_conversation_id=resp.get("OriginatorConversationID"), response_code=str(resp.get("ResponseCode")))
        return conv

    # ---- C2B (customer-initiated payments to the paybill) ---------------------------

    # Safaricom rejects callback URLs that contain these words (as reported by integrators; checked case-insensitively).
    URL_BLOCKLIST = ("m-pesa", "mpesa", "safaricom", "exe", "exec", "cmd", "sql", "query")

    @classmethod
    def blocked_words(cls, url: str) -> list[str]:
        low = url.lower()
        return [w for w in cls.URL_BLOCKLIST if w in low]

    def register_c2b_urls(self, confirmation_url: str, validation_url: str, *, shortcode: str | None = None,
                          response_type: str = "Completed") -> dict[str, Any]:
        """Tell Daraja where to send C2B confirmations. ResponseType is what Daraja does if our validation URL does not
        answer: 'Completed' accepts the payment, 'Cancelled' refuses it. Repeat-safe: registering again overwrites."""
        if response_type not in ("Completed", "Cancelled"):
            raise ValueError("response_type must be Completed or Cancelled")
        for u in (confirmation_url, validation_url):
            if not u.startswith("https://"):
                raise NotSentError(f"Daraja needs https URLs, got {u[:40]}")
            bad = self.blocked_words(u)
            if bad:
                raise NotSentError(f"Daraja is reported to reject URLs containing {bad}; generate a new webhook token")
        return self._post("/mpesa/c2b/v1/registerurl", {"ShortCode": shortcode or self.provider.shortcode, "ResponseType": response_type,
                                                         "ConfirmationURL": confirmation_url, "ValidationURL": validation_url})

    def simulate_c2b(self, *, amount: Decimal, msisdn: str, bill_ref: str, shortcode: str | None = None) -> dict[str, Any]:
        """Sandbox only: ask Daraja to pretend a customer paid the paybill. The confirmation arrives on the registered URL."""
        if not self.provider.is_sandbox():
            raise NotSentError("C2B simulate exists only on the sandbox")
        return self._post("/mpesa/c2b/v1/simulate", {"ShortCode": shortcode or self.provider.shortcode, "CommandID": "CustomerPayBillOnline",
                                                      "Amount": self._whole_shillings(amount), "Msisdn": msisdn, "BillRefNumber": bill_ref})

    # ---- B2C (paying people: payroll) -------------------------------------------------------------------

    def _credential(self) -> str:
        if self.security_credential:
            return self.security_credential
        if not (self.initiator_password and self.cert_pem):
            raise NotSentError("no security credential: set DARAJA_SECURITY_CREDENTIAL, or the initiator password and certificate")
        try:
            return security_credential(self.initiator_password, self.cert_pem)
        except Exception as e:  # bad certificate: nothing was sent
            raise NotSentError(f"could not build the SecurityCredential: {type(e).__name__}") from None

    def send_b2c(self, *, originator_id: str, phone: str, amount: Decimal, remarks: str) -> tuple[str, str]:
        """Ask Daraja to pay a person (B2C). Implements payroll.PayoutPort. 'Accepted' is all this can mean: the outcome
        arrives later on the result URL. Shapes are from Daraja's documentation (B2C v3); not yet seen live, because the
        sandbox's shared initiator credential was locked (8006) when this was built."""
        for name in ("initiator", "b2c_result_url", "b2c_timeout_url"):
            if not getattr(self, name):
                raise NotSentError(f"B2C is not configured ({name} missing)")
        whole = self._whole_shillings(amount)
        body = {
            "OriginatorConversationID": originator_id,
            "InitiatorName": self.initiator,
            "SecurityCredential": self._credential(),
            "CommandID": self.b2c_command,
            "Amount": whole,
            "PartyA": self.b2c_shortcode or self.provider.shortcode,
            "PartyB": normalize_msisdn(phone),
            "Remarks": (remarks or "Payment")[:100],
            "QueueTimeOutURL": self.b2c_timeout_url,
            "ResultURL": self.b2c_result_url,
            "Occassion": "",  # sic: Daraja's documented spelling
        }
        resp = self._post("/mpesa/b2c/v3/paymentrequest", body)
        conv = resp.get("ConversationID")
        if not conv:
            raise SendOutcomeUnknown(f"Daraja accepted the payment but returned no ConversationID: {self._safe(resp)}")
        return str(conv), str(resp.get("OriginatorConversationID") or originator_id)

    def _refund_update(self, key: str, status: str, **cols: Any) -> None:
        sets = ["status = ?", "updated_at = ?"] + [f"{c} = ?" for c in cols]
        self.db.execute(f"UPDATE refund_requests SET {', '.join(sets)} WHERE idempotency_key = ?",
                        (status, datetime.now(timezone.utc), *cols.values(), key))

    def ingest_reversal_result(self, payload: Mapping[str, Any]) -> bool:
        """Apply the asynchronous result Daraja posts to ResultURL. Returns False if it matches no request."""
        try:
            res = payload["Result"]
            code = str(res["ResultCode"])
        except (KeyError, TypeError) as e:
            raise ValueError(f"malformed reversal result: missing {e}") from e
        status = "COMPLETED" if code == "0" else "FAILED"
        conv, orig = res.get("ConversationID"), res.get("OriginatorConversationID")
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE refund_requests SET status = ?, result_code = ?, result_desc = ?, updated_at = ? "
                "WHERE (conversation_id = ? OR originator_conversation_id = ?) AND status IN ('ACCEPTED', 'SENDING')",
                (status, code, str(res.get("ResultDesc", ""))[:300], datetime.now(timezone.utc), conv, orig),
            )
            if cur.rowcount != 1:
                return False
            if status == "COMPLETED":
                # The money went back: the payment itself is now REVERSED, so it stops counting towards its invoice
                # (and the duplicate it was refunded for resolves on the next reconciliation).
                txn_id, amount = self.db.execute(
                    "SELECT provider_txn_id, amount FROM refund_requests WHERE conversation_id = ? OR originator_conversation_id = ?",
                    (conv, orig)).fetchone()
                held = self.ledger.get_txn(txn_id)
                if held is not None and held.amount != to_dec(amount):
                    return True  # a partial refund does not reverse the whole payment; the books need a person
                self.ledger.ingest(ProviderTxn(
                    provider_txn_id=txn_id, amount=held.amount if held else to_dec(amount), status=TxnStatus.REVERSED,
                    timestamp=datetime.now(timezone.utc), source="reversal_result",
                    result_desc=f"reversed by payrecon (ConversationID {conv}, reversal {res.get('TransactionID', '?')})",
                ))
        return True

    # ---- callbacks ------------------------------------------------------------------

    def handle_callback(self, payload: Mapping[str, Any]) -> tuple[ProviderTxn, str]:
        """Parse an STK or C2B callback and store it in the ledger. Idempotent: a redelivery changes nothing.
        Raises ValueError for payloads it cannot use (a webhook should still answer 200 and log)."""
        txn = self.provider.parse_event(payload)
        return txn, self.ledger.ingest(txn)

    # ---- HTTP -----------------------------------------------------------------------

    @staticmethod
    def _whole_shillings(amount: Decimal) -> int:
        if amount <= 0 or amount != amount.to_integral_value():
            raise NotSentError(f"amount {amount} must be a positive whole number of shillings")
        return int(amount)

    @staticmethod
    def _safe(body: Mapping[str, Any]) -> str:
        return str({k: v for k, v in body.items() if k not in ("SecurityCredential", "Password", "access_token")})[:300]

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            token = self.provider._access_token()
        except (ProviderError, requests.RequestException, KeyError, ValueError) as e:
            raise NotSentError(f"could not get an access token: {type(e).__name__}") from None
        try:
            r = requests.post(f"{self.provider.base_url}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=self.timeouts)
        except requests.RequestException as e:
            if _provably_not_sent(e):
                raise NotSentError(f"request never left: {type(e).__name__}") from None
            raise SendOutcomeUnknown(f"transport failure after the request may have been sent: {type(e).__name__}") from None
        try:
            data = r.json()
            data = data if isinstance(data, dict) else {}
        except ValueError:
            data = {}
        self.last_response = f"HTTP {r.status_code} {self._safe(data)}"
        if r.status_code >= 500:
            raise SendOutcomeUnknown(f"HTTP {r.status_code} {self._safe(data)}")
        if r.status_code >= 400:
            raise NotSentError(f"rejected: HTTP {r.status_code} {data.get('errorCode', '')} {data.get('errorMessage', '')}".strip())
        code = str(data.get("ResponseCode", ""))
        if code == "":
            raise SendOutcomeUnknown(f"HTTP {r.status_code} without a ResponseCode: {self._safe(data)}")
        if code.strip("0") != "":  # "0" usually; C2B registerurl answered "00000000" live (2026-10-19)
            raise NotSentError(f"Daraja declined: ResponseCode {code} {data.get('ResponseDescription', '')}".strip())
        return data
