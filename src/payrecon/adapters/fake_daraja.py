"""A local stand-in for the Daraja HTTP API, for tests and for trying the live stack without credentials.

It speaks the shapes in Safaricom's public docs (OAuth, STK push, STK push query, Reversal) and can misbehave on
demand. It is NOT Daraja: it shows the gateway handles those documented shapes and failure modes, nothing more.
Behaviour that is an assumption rather than documented (for example the 500 'being processed' answer to a query for a
payment still in flight) is marked below; compare with the real sandbox using `recover sandbox-check`.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


class FakeDaraja:
    def __init__(self, consumer_key: str = "key", consumer_secret: str = "secret"):
        self.consumer_key, self.consumer_secret = consumer_key, consumer_secret
        self.calls: list[dict[str, Any]] = []  # every request received: method, path, headers, json
        self.statuses: dict[str, str] = {}  # CheckoutRequestID -> SUCCESS | FAILED | PENDING (what the query answers)
        self.fail_codes: dict[str, str] = {}  # CheckoutRequestID -> ResultCode returned for FAILED
        self.faults: dict[str, dict[str, Any]] = {}  # path -> {"mode": status|drop|hang, ...}, applied to the next `count` calls
        self.token_ttl = 3599
        self.c2b_urls: dict[str, Any] = {}  # the last registerurl body
        self.b2c_payments: list[dict[str, Any]] = []  # every accepted B2C request: what "M-Pesa" was asked to pay
        self._n = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ---- lifecycle -------------------------------------------------------------

    def start(self) -> "FakeDaraja":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _body(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {"_raw": raw.decode(errors="replace")}

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                fake._handle(self, "GET", {})

            def do_POST(self) -> None:
                fake._handle(self, "POST", self._body())

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:  # tests hang up on purpose
                pass

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FakeDaraja":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---- helpers for tests -----------------------------------------------------

    def calls_to(self, path_prefix: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].startswith(path_prefix)]

    def fault(self, path: str, mode: str, count: int = 1, **kw: Any) -> None:
        self.faults[path] = {"mode": mode, "count": count, **kw}

    def _next_id(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    # ---- the API ---------------------------------------------------------------

    def _handle(self, h: BaseHTTPRequestHandler, method: str, body: dict[str, Any]) -> None:
        path = h.path.split("?")[0]
        with self._lock:
            self.calls.append({"method": method, "path": path, "headers": dict(h.headers), "json": body})
            fault = self.faults.get(path)
            if fault and fault["count"] > 0:
                fault["count"] -= 1
            else:
                fault = None
        if fault:
            mode = fault["mode"]
            if mode == "drop":  # read the request, then close without answering: a reset after the request was sent
                h.close_connection = True
                h.connection.close()
                return
            if mode == "hang":
                time.sleep(fault.get("seconds", 1.0))
            elif mode == "status":
                h._send(fault["code"], fault.get("body", {}))  # type: ignore[attr-defined]
                return
        if path == "/oauth/v1/generate":
            self._oauth(h)
        elif path == "/mpesa/stkpush/v1/processrequest":
            self._stk_push(h, body)
        elif path == "/mpesa/stkpushquery/v1/query":
            self._stk_query(h, body)
        elif path == "/mpesa/reversal/v1/request":
            self._reversal(h, body)
        elif path == "/mpesa/c2b/v1/registerurl":
            self._c2b_register(h, body)
        elif path == "/mpesa/c2b/v1/simulate":
            self._c2b_simulate(h, body)
        elif path == "/mpesa/b2c/v3/paymentrequest":
            self._b2c(h, body)
        else:
            h._send(404, {"errorCode": "404.001.03", "errorMessage": "Not Found"})  # type: ignore[attr-defined]

    def _authorised(self, h: BaseHTTPRequestHandler) -> bool:
        return (h.headers.get("Authorization") or "").startswith("Bearer tok-")

    def _oauth(self, h: BaseHTTPRequestHandler) -> None:
        want = base64.b64encode(f"{self.consumer_key}:{self.consumer_secret}".encode()).decode()
        if h.headers.get("Authorization") != f"Basic {want}":
            h._send(401, {"errorCode": "400.008.01", "errorMessage": "Invalid Authentication passed"})  # type: ignore[attr-defined]
            return
        h._send(200, {"access_token": f"tok-{self._next_id()}", "expires_in": str(self.token_ttl)})  # type: ignore[attr-defined]

    def _stk_push(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        missing = [k for k in ("BusinessShortCode", "Password", "Timestamp", "TransactionType", "Amount", "PartyA", "PartyB",
                               "PhoneNumber", "CallBackURL", "AccountReference", "TransactionDesc") if k not in b]
        if missing:
            h._send(400, {"errorCode": "400.002.02", "errorMessage": f"Bad Request - Invalid {missing[0]}"})  # type: ignore[attr-defined]
            return
        n = self._next_id()
        checkout = f"ws_CO_{n:020d}"
        self.statuses.setdefault(checkout, "PENDING")
        h._send(200, {"MerchantRequestID": f"m-{n}", "CheckoutRequestID": checkout, "ResponseCode": "0",  # type: ignore[attr-defined]
                      "ResponseDescription": "Success. Request accepted for processing",
                      "CustomerMessage": "Success. Request accepted for processing"})

    def _stk_query(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        rid = b.get("CheckoutRequestID")
        st = self.statuses.get(rid)
        if st is None:
            h._send(400, {"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid CheckoutRequestID"})  # type: ignore[attr-defined]
        elif st == "PENDING":
            # ASSUMPTION (from memory of Daraja behaviour, not from the docs): a query for a payment still in flight
            # answers HTTP 500 with an errorMessage saying it is being processed.
            h._send(500, {"requestId": "r", "errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})  # type: ignore[attr-defined]
        else:
            ok = st == "SUCCESS"
            h._send(200, {"ResponseCode": "0", "ResponseDescription": "The service request has been accepted successfully",  # type: ignore[attr-defined]
                          "MerchantRequestID": "m", "CheckoutRequestID": rid,
                          "ResultCode": "0" if ok else self.fail_codes.get(rid, "1032"),
                          "ResultDesc": "The service request is processed successfully." if ok else "Request cancelled by user"})

    def _reversal(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        need = ("Initiator", "SecurityCredential", "CommandID", "TransactionID", "Amount", "ReceiverParty",
                "RecieverIdentifierType", "ResultURL", "QueueTimeOutURL", "Remarks")
        missing = [k for k in need if k not in b]
        if missing or b.get("CommandID") != "TransactionReversal":
            h._send(400, {"errorCode": "400.002.02", "errorMessage": f"Bad Request - Invalid {(missing or ['CommandID'])[0]}"})  # type: ignore[attr-defined]
            return
        n = self._next_id()
        h._send(200, {"OriginatorConversationID": f"orig-{n}", "ConversationID": f"AG_{n:016d}", "ResponseCode": "0",  # type: ignore[attr-defined]
                      "ResponseDescription": "Accept the service request successfully."})


    def _c2b_register(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        missing = [k for k in ("ShortCode", "ResponseType", "ConfirmationURL", "ValidationURL") if k not in b]
        if missing:
            h._send(400, {"errorCode": "400.002.02", "errorMessage": f"Bad Request - Invalid {missing[0]}"})  # type: ignore[attr-defined]
            return
        self.c2b_urls = dict(b)
        h._send(200, {"OriginatorCoversationID": f"c2b-{self._next_id()}", "ResponseCode": "0",  # type: ignore[attr-defined]
                      "ResponseDescription": "Success"})

    def _c2b_simulate(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        missing = [k for k in ("ShortCode", "CommandID", "Amount", "Msisdn", "BillRefNumber") if k not in b]
        if missing:
            h._send(400, {"errorCode": "400.002.02", "errorMessage": f"Bad Request - Invalid {missing[0]}"})  # type: ignore[attr-defined]
            return
        h._send(200, {"OriginatorCoversationID": f"sim-{self._next_id()}", "ResponseCode": "0",  # type: ignore[attr-defined]
                      "ResponseDescription": "Accept the service request successfully."})


    def _b2c(self, h: BaseHTTPRequestHandler, b: dict[str, Any]) -> None:
        if not self._authorised(h):
            h._send(401, {"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"})  # type: ignore[attr-defined]
            return
        need = ("OriginatorConversationID", "InitiatorName", "SecurityCredential", "CommandID", "Amount", "PartyA", "PartyB",
                "Remarks", "QueueTimeOutURL", "ResultURL")
        missing = [k for k in need if k not in b]
        if missing or b.get("CommandID") not in ("BusinessPayment", "SalaryPayment", "PromotionPayment"):
            h._send(400, {"errorCode": "400.002.02", "errorMessage": f"Bad Request - Invalid {(missing or ['CommandID'])[0]}"})  # type: ignore[attr-defined]
            return
        n = self._next_id()
        self.b2c_payments.append(dict(b))
        h._send(200, {"ConversationID": f"AG_B2C_{n:012d}", "OriginatorConversationID": b["OriginatorConversationID"],  # type: ignore[attr-defined]
                      "ResponseCode": "0", "ResponseDescription": "Accept the service request successfully."})

    def b2c_result(self, body: dict[str, Any], *, ok: bool = True, receipt: str = "UJKB2C0001", code: int = 2040,
                   registered: str = "Jane Worker") -> dict[str, Any]:
        """The result Daraja would post later for an accepted B2C request (shape from the docs)."""
        return {"Result": {
            "ResultType": 0, "ResultCode": 0 if ok else code,
            "ResultDesc": "The service request is processed successfully." if ok else "The receiver is not registered",
            "OriginatorConversationID": body["OriginatorConversationID"], "ConversationID": body.get("_conversation", ""),
            "TransactionID": receipt if ok else "",
            "ResultParameters": {"ResultParameter": [
                {"Key": "TransactionAmount", "Value": body["Amount"]}, {"Key": "TransactionReceipt", "Value": receipt},
                {"Key": "ReceiverPartyPublicName", "Value": f"{body['PartyB']} - {registered}"},
                {"Key": "TransactionCompletedDateTime", "Value": "20.10.2026 22:30:00"}]} if ok else {}}}


_CERT: dict[str, Any] = {}


def make_test_certificate() -> tuple[Any, bytes]:
    """A throwaway RSA key and self-signed certificate (PEM), standing in for Safaricom's public certificate so the
    SecurityCredential path can be exercised locally. Returns (private_key, certificate_pem). Cached per process."""
    if not _CERT:
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "payrecon-test")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
        _CERT["key"], _CERT["pem"] = key, cert.public_bytes(serialization.Encoding.PEM)
    return _CERT["key"], _CERT["pem"]
