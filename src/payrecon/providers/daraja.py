"""M-Pesa Daraja adapter (sandbox-first).

Payload shapes follow the public Daraja docs as I know them. Parsing and response
interpretation are pure functions covered by unit tests with fabricated payloads;
the HTTP calls (`get_status`) have NOT been exercised against the live sandbox
and need your sandbox credentials plus a check against the current docs.

Two facts shape the design:

* STK push callbacks do not echo the account reference, so we store a StkRequest
  when we send the push and look it up by CheckoutRequestID.
* Failure callbacks carry no amount, so the amount also comes from the StkRequest.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlparse

from ..models import ProviderTxn, TxnStatus
from .base import EAT, ProviderError, RateLimited, mask_msisdn

SANDBOX_URL = "https://sandbox.safaricom.co.ke"


@dataclass(frozen=True)
class StkRequest:
    """Stored when the STK push is sent; keyed by CheckoutRequestID."""

    reference: str
    amount: Decimal
    msisdn_masked: str | None
    requested_at: datetime


def _to_decimal(v: Any) -> Decimal:
    return Decimal(str(v).replace(",", "").strip())


def _parse_daraja_time(v: Any) -> datetime | None:
    if v is None or str(v).strip() == "":
        return None
    return datetime.strptime(str(v).strip(), "%Y%m%d%H%M%S").replace(tzinfo=EAT)


class DarajaProvider:
    name = "mpesa_daraja"

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        shortcode: str,
        passkey: str,
        *,
        base_url: str = SANDBOX_URL,
        allow_production: bool = False,
        stk_requests: Mapping[str, StkRequest] | None = None,
        timeout: float = 15.0,
    ):
        host = urlparse(base_url).hostname or ""
        if "sandbox" not in base_url and host not in ("127.0.0.1", "localhost", "::1") and not allow_production:
            raise ValueError("refusing to talk to a non-sandbox URL; pass allow_production=True deliberately")
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.shortcode = shortcode
        self.passkey = passkey
        self.base_url = base_url.rstrip("/")
        self.stk_requests: Mapping[str, StkRequest] = stk_requests if stk_requests is not None else {}
        self.timeout = timeout
        self._token: tuple[str, float] | None = None

    def is_sandbox(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        return "sandbox" in self.base_url or host in ("127.0.0.1", "localhost", "::1")

    @classmethod
    def from_env(cls, stk_requests: Mapping[str, StkRequest] | None = None) -> "DarajaProvider":
        return cls(
            os.environ["DARAJA_CONSUMER_KEY"],
            os.environ["DARAJA_CONSUMER_SECRET"],
            os.environ["DARAJA_SHORTCODE"],
            os.environ["DARAJA_PASSKEY"],
            base_url=os.environ.get("DARAJA_BASE_URL", SANDBOX_URL),
            stk_requests=stk_requests,
        )

    # ---- event parsing (pure) -------------------------------------------------

    def parse_event(self, payload: Mapping[str, Any]) -> ProviderTxn:
        if "Body" in payload and "stkCallback" in payload.get("Body", {}):
            return self.parse_stk_callback(payload)
        if "TransID" in payload:
            return self.parse_c2b_confirmation(payload)
        raise ValueError("unrecognised Daraja payload")

    def parse_stk_callback(self, payload: Mapping[str, Any]) -> ProviderTxn:
        try:
            cb = payload["Body"]["stkCallback"]
            checkout_id = str(cb["CheckoutRequestID"])
            code = str(cb["ResultCode"])
        except (KeyError, TypeError) as e:
            raise ValueError(f"malformed STK callback: missing {e}") from e

        req = self.stk_requests.get(checkout_id)
        items = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}

        if code == "0":
            try:
                receipt = str(items["MpesaReceiptNumber"])
                amount = _to_decimal(items["Amount"])
            except KeyError as e:
                raise ValueError(f"success callback missing {e}") from e
            phone = items.get("PhoneNumber")
            return ProviderTxn(
                provider_txn_id=receipt,
                amount=amount,
                status=TxnStatus.SUCCESS,
                timestamp=_parse_daraja_time(items.get("TransactionDate"))
                or (req.requested_at if req else datetime.now(EAT)),
                reference_raw=req.reference if req else None,  # unknown request -> orphan, by design
                msisdn_masked=mask_msisdn(phone) if phone else (req.msisdn_masked if req else None),
                result_code="0",
                source="stk_callback",
                request_id=checkout_id,
            )

        if req is None:
            raise ValueError(f"failure callback for unknown CheckoutRequestID {checkout_id}: cannot recover amount")
        return ProviderTxn(
            provider_txn_id=checkout_id,
            amount=req.amount,
            status=TxnStatus.FAILED,
            timestamp=req.requested_at,
            reference_raw=req.reference,
            msisdn_masked=req.msisdn_masked,
            result_code=code,
            source="stk_callback",
            result_desc=cb.get("ResultDesc"),
        )

    def parse_c2b_confirmation(self, payload: Mapping[str, Any]) -> ProviderTxn:
        try:
            txn_id = str(payload["TransID"])
            amount = _to_decimal(payload["TransAmount"])
        except KeyError as e:
            raise ValueError(f"C2B confirmation missing {e}") from e
        return ProviderTxn(
            provider_txn_id=txn_id,
            amount=amount,
            status=TxnStatus.SUCCESS,
            timestamp=_parse_daraja_time(payload.get("TransTime")) or datetime.now(EAT),
            reference_raw=payload.get("BillRefNumber"),  # whatever the payer typed
            msisdn_masked=mask_msisdn(payload.get("MSISDN")),
            result_code="0",
            source="c2b_confirmation",
        )

    # ---- status query ---------------------------------------------------------

    @staticmethod
    def interpret_query_response(http_status: int, body: Mapping[str, Any]) -> TxnStatus:
        """Map an STK push query response to a status. Unknown shapes raise; they are never guessed."""
        if http_status == 429:  # seen on the live sandbox: a spike-arrest "fault" body with no status in it
            raise RateLimited(f"rate limited (HTTP 429): {dict(body)!r}")
        if body.get("ResultCode") not in (None, ""):
            return TxnStatus.SUCCESS if str(body["ResultCode"]) == "0" else TxnStatus.FAILED
        text = " ".join(str(body.get(k, "")) for k in ("errorMessage", "ResponseDescription", "ResultDesc")).lower()
        if "being processed" in text:
            return TxnStatus.PENDING
        raise ProviderError(f"unrecognised status response (HTTP {http_status}): {dict(body)!r}")

    def query_raw(self, request_id: str) -> tuple[int, dict[str, Any]]:
        """STK push query by CheckoutRequestID; returns (HTTP status, JSON body) uninterpreted."""
        requests = self._requests()
        ts = datetime.now(EAT).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{self.shortcode}{self.passkey}{ts}".encode()).decode()
        resp = requests.post(
            f"{self.base_url}/mpesa/stkpushquery/v1/query",
            json={
                "BusinessShortCode": self.shortcode,
                "Password": password,
                "Timestamp": ts,
                "CheckoutRequestID": request_id,
            },
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=self.timeout,
        )
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError(f"non-JSON status response (HTTP {resp.status_code})") from e
        if not isinstance(body, dict):
            raise ProviderError(f"unexpected status response shape (HTTP {resp.status_code})")
        return resp.status_code, body

    def get_status(self, request_id: str) -> TxnStatus:
        """STK push query by CheckoutRequestID. Requires sandbox credentials; not run in CI."""
        code, body = self.query_raw(request_id)
        return self.interpret_query_response(code, body)

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        requests = self._requests()
        resp = requests.get(
            f"{self.base_url}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(self.consumer_key, self.consumer_secret),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ProviderError(f"token request failed (HTTP {resp.status_code})")
        data = resp.json()
        self._token = (data["access_token"], time.time() + float(data.get("expires_in", 3599)))
        return self._token[0]

    @staticmethod
    def _requests():
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ProviderError("install requests: pip install 'payrecon[daraja]'") from e
        return requests
