"""Webhook receiver for Daraja callbacks.

Daraja does not sign its callbacks, so a callback proves nothing about who sent it. The rules below follow from that:

* **Secret path.** Every URL carries a long random token (`/daraja/<kind>/<token>`), compared in constant time.
  Anyone who learns the URL can post to it, so treat it like a password and rotate it if it leaks. An optional IP
  allowlist applies to the socket peer only (behind a tunnel or proxy that is the proxy, so it is off by default).
* **Store first.** The payload (phone numbers and names masked) goes into `webhook_inbox` before anything else
  happens, keyed by the hash of the raw payload, so a redelivery is recognised and never applied twice.
* **Always acknowledge a stored callback with 200.** Whatever goes wrong afterwards is recorded on the inbox row
  (ERROR, retried by `replay`; QUARANTINED, for a person) rather than answered with an error Daraja might retry
  or give up on. Only unauthenticated, malformed or oversized requests get an error.
* **A success is a claim, not a fact.** An STK success callback is ingested only if (a) we recorded that request
  ourselves, (b) its amount equals what we asked for, and (c) Daraja's own query agrees. A contradiction is
  quarantined for a person. Not being able to ask (rate limit, outage) leaves the row in ERROR to be retried.
  C2B confirmations cannot be verified this way: there is no query for them, so the secret path is their only guard.

The response bodies (`{"ResultCode": 0, "ResultDesc": "Accepted"}`) follow my reading of the public docs and have not
been checked against what Daraja requires of a live shortcode.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .adapters.db import Database
from .adapters.gateway import DarajaGateway
from .models import TxnStatus
from .providers.base import ProviderError, RateLimited, mask_msisdn

KINDS = ("stk", "c2b", "reversal-result", "reversal-timeout")
MAX_BODY = 64 * 1024
MAX_ATTEMPTS = 10  # an item that still fails after this many tries is quarantined for a person
MIN_TOKEN = 24
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PHONE_KEYS = {"MSISDN", "PhoneNumber", "msisdn"}
_NAME_KEYS = {"FirstName", "MiddleName", "LastName"}
ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}
VALIDATION = "c2b-validation"
VALIDATION_ACCEPT = {"ResultCode": "0", "ResultDesc": "Accepted"}


def check_token(token: str | None) -> str:
    if not token or len(token) < MIN_TOKEN or not _TOKEN_RE.match(token):
        raise ValueError(f"the webhook token must be at least {MIN_TOKEN} characters of letters, digits, '_' or '-' "
                         "(generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    return token


def sanitize(obj: Any) -> Any:
    """Copy of a payload that is safe to keep: phone numbers masked, customer names removed. Parsers read the
    masked form unchanged, so a stored payload can be replayed."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _PHONE_KEYS and not isinstance(v, (dict, list)):
                out[k] = mask_msisdn(v)
            elif k in _NAME_KEYS:
                out[k] = "<removed>"
            elif k == "Value" and obj.get("Name") in _PHONE_KEYS:  # STK success: {"Name": "PhoneNumber", "Value": 2547...}
                out[k] = mask_msisdn(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def payload_hash(kind: str, payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{kind}\n{canonical}".encode()).hexdigest()


@dataclass
class Outcome:
    status: str  # INGESTED | QUARANTINED | ERROR | NOTED
    detail: str


class WebhookService:
    def __init__(
        self,
        gateway: DarajaGateway,
        db: Database,
        token: str,
        *,
        verify_success: bool = True,
        allow_ips: set[str] | frozenset[str] | None = None,
        max_body: int = MAX_BODY,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        log: Callable[[str], None] | None = None,
    ):
        self.gw, self.db = gateway, db
        self.token = check_token(token).encode()
        self.verify_success, self.allow_ips, self.max_body = verify_success, set(allow_ips or ()), max_body
        self.now, self.log = now, log or (lambda line: None)
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()

    # ---- the HTTP-facing entry point (no framework: easy to test, easy to wrap) -------------------

    def handle(self, method: str, path: str, body: bytes, remote_addr: str = "", declared_length: int | None = None) -> tuple[int, dict]:
        if self.allow_ips and remote_addr not in self.allow_ips:
            return self._reply(403, {"error": "forbidden"}, "-", "refused: address not allowed")
        parts = path.split("?", 1)[0].strip("/").split("/")
        kind = parts[1] if len(parts) == 3 and parts[0] == "daraja" and parts[1] in KINDS + (VALIDATION,) else None
        if kind is None or not hmac.compare_digest(parts[2].encode(), self.token):
            return self._reply(404, {"error": "not found"}, "-", "refused: unknown path or token")
        if method != "POST":
            return self._reply(405, {"error": "method not allowed"}, kind, f"refused: {method}")
        if (declared_length or 0) > self.max_body or len(body) > self.max_body:
            return self._reply(413, {"error": "payload too large"}, kind, "refused: too large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, {"error": "body is not JSON"}, kind, "refused: not JSON")
        if not isinstance(payload, dict):
            return self._reply(400, {"error": "body is not a JSON object"}, kind, "refused: not an object")
        if kind == VALIDATION:
            # Daraja asks "may this payment go ahead?" before a C2B payment completes. Only called if Safaricom has
            # enabled external validation on the shortcode. Not a payment, so nothing is stored; the confirmation is.
            return self._reply(200, dict(VALIDATION_ACCEPT), kind, "accepted (validation; nothing stored)")
        status = self.accept(kind, payload)
        return self._reply(200, dict(ACK), kind, f"stored: {status}")

    def _reply(self, code: int, body: dict, kind: str, note: str) -> tuple[int, dict]:
        self.log(f"{code} {kind} {note}")
        return code, body

    # ---- store, then process ------------------------------------------------------------------------

    def accept(self, kind: str, payload: dict) -> str:
        key = payload_hash(kind, payload)
        stored = json.dumps(sanitize(payload), sort_keys=True)
        cur = self.db.execute(
            "INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES (?,?,?,?, 'RECEIVED') "
            "ON CONFLICT (payload_sha256) DO NOTHING",
            (key, kind, self.now(), stored),
        )
        if cur.rowcount == 0:
            row = self.db.execute("SELECT status FROM webhook_inbox WHERE payload_sha256 = ?", (key,)).fetchone()
            if row and row[0] not in ("RECEIVED", "ERROR"):
                return f"duplicate of an item already {row[0]}"
        self._process(key, kind, payload)
        return self.db.execute("SELECT status FROM webhook_inbox WHERE payload_sha256 = ?", (key,)).fetchone()[0]

    def _process(self, key: str, kind: str, payload: Mapping[str, Any]) -> None:
        with self._busy_lock:
            if key in self._busy:
                return  # another thread is on this very payload
            self._busy.add(key)
        try:
            try:
                out = self._apply(kind, payload)
            except Exception as e:  # noqa: BLE001 - the row records it; the sender still gets its 200
                out = Outcome("ERROR", f"{type(e).__name__}: {str(e)[:200]}")
            self.db.execute(
                "UPDATE webhook_inbox SET status = ?, detail = ?, attempts = attempts + 1, processed_at = ? WHERE payload_sha256 = ?",
                (out.status, out.detail, self.now(), key),
            )
        finally:
            with self._busy_lock:
                self._busy.discard(key)

    def _apply(self, kind: str, payload: Mapping[str, Any]) -> Outcome:
        if kind == "reversal-timeout":
            return Outcome("NOTED", "Daraja timed out on a reversal; it stays ACCEPTED and a person must check whether it happened")
        if kind == "reversal-result":
            try:
                applied = self.gw.ingest_reversal_result(payload)
            except ValueError as e:
                return Outcome("QUARANTINED", str(e))
            return Outcome("INGESTED", "reversal result applied") if applied else Outcome("QUARANTINED", "matches no pending reversal")

        if kind == "stk":
            cb = (payload.get("Body") or {}).get("stkCallback") if isinstance(payload.get("Body"), dict) else None
            if not isinstance(cb, dict) or "CheckoutRequestID" not in cb or "ResultCode" not in cb:
                return Outcome("QUARANTINED", "not a recognisable STK callback")
            if str(cb["ResultCode"]) == "0":
                blocked = self._check_success(payload, str(cb["CheckoutRequestID"]))
                if blocked is not None:
                    return blocked
        try:
            txn, result = self.gw.handle_callback(payload)
        except ValueError as e:
            return Outcome("QUARANTINED", str(e))
        if result == "conflict":
            return Outcome("QUARANTINED", f"{txn.provider_txn_id}: already stored with a different amount")
        return Outcome("INGESTED", f"{txn.provider_txn_id} {txn.status.value}: {result}")

    def _check_success(self, payload: Mapping[str, Any], checkout_id: str) -> Outcome | None:
        """None means the success may be ingested; otherwise the outcome to record instead."""
        req = self.gw.provider.stk_requests.get(checkout_id)
        if req is None:
            return Outcome("QUARANTINED", "success callback for a request we never recorded")
        try:
            txn = self.gw.provider.parse_event(payload)
        except ValueError as e:
            return Outcome("QUARANTINED", str(e))
        if txn.amount != req.amount:
            return Outcome("QUARANTINED", f"callback amount {txn.amount} differs from the {req.amount} we asked for")
        if not self.verify_success:
            return None
        try:
            asked = self.gw.get_status(checkout_id)
        except RateLimited:
            return Outcome("ERROR", "could not verify the success yet: Daraja rate-limited the query; will retry")
        except ProviderError as e:
            return Outcome("ERROR", f"could not verify the success yet: {str(e)[:150]}")
        if asked is TxnStatus.SUCCESS:
            return None
        if asked is TxnStatus.FAILED:
            return Outcome("QUARANTINED", "callback claims success but Daraja's query says the request failed")
        return Outcome("ERROR", "Daraja's query does not confirm the success yet; will retry")

    # ---- operations -----------------------------------------------------------------------------------

    def replay(self, limit: int = 100) -> dict[str, int]:
        """Reprocess items left RECEIVED (crash) or ERROR (could not verify, bug). Safe to run any time."""
        rows = self.db.execute(
            "SELECT payload_sha256, kind, payload FROM webhook_inbox WHERE status IN ('RECEIVED', 'ERROR') ORDER BY received_at LIMIT ?", (limit,)
        ).fetchall()
        for key, kind, payload in rows:
            if self.db.execute("SELECT attempts FROM webhook_inbox WHERE payload_sha256 = ?", (key,)).fetchone()[0] >= MAX_ATTEMPTS:
                self.db.execute("UPDATE webhook_inbox SET status = 'QUARANTINED', detail = ? WHERE payload_sha256 = ?",
                                (f"gave up after {MAX_ATTEMPTS} attempts; last error: " + str(self._detail(key)), key))
                continue
            self._process(key, kind, json.loads(payload))
        out: dict[str, int] = {}
        for key, *_ in rows:
            st = self.db.execute("SELECT status FROM webhook_inbox WHERE payload_sha256 = ?", (key,)).fetchone()[0]
            out[st] = out.get(st, 0) + 1
        return out

    def _detail(self, key: str) -> str:
        return self.db.execute("SELECT detail FROM webhook_inbox WHERE payload_sha256 = ?", (key,)).fetchone()[0] or ""

    def inbox(self, status: str | None = None, limit: int = 50) -> list[tuple]:
        q = "SELECT received_at, kind, status, attempts, detail, payload_sha256 FROM webhook_inbox"
        args: list[Any] = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        return self.db.execute(q + " ORDER BY received_at DESC LIMIT ?", (*args, limit)).fetchall()


# ---- the server ----------------------------------------------------------------------------------------

def make_server(service: WebhookService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "payrecon"
        sys_version = ""

        def _run(self) -> None:
            try:
                declared = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                declared = 0
            body = self.rfile.read(min(declared, service.max_body + 1)) if declared > 0 else b""
            code, resp = service.handle(self.command, self.path, body, self.client_address[0], declared)
            data = json.dumps(resp).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if declared > service.max_body:
                self.send_header("Connection", "close")  # we did not read the body; do not reuse the connection
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _run

        def log_message(self, *a: Any) -> None:  # the default log line would print the secret path
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((host, port), Handler)
