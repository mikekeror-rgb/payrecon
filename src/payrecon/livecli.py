"""Commands that run the recovery executor against real adapters: SQL books and ledger, and a Daraja gateway.

    python -m payrecon.recover init-db      --db app.db
    python -m payrecon.recover import-csv   --db app.db --invoices invoices.csv --transactions transactions.csv
    python -m payrecon.recover plan         --db app.db --store actions.db
    python -m payrecon.recover run          --db app.db --store actions.db [--execute]
    python -m payrecon.recover approve ID   --store actions.db --as alice
    python -m payrecon.recover sandbox-check [--query ID] [--push 2547XXXXXXXX --amount 1 --reference TEST1]
    python -m payrecon.recover live-demo

`--db` is SQLite here (a path) or a postgres:// URL (needs `pip install 'psycopg[binary]'`; not run against a live
Postgres from Python in this repo's tests). Without Daraja credentials in the environment, `run` still applies
payments and queues notifications, and refuses anything that needs the gateway.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from .adapters import Database, SQLITE_DDL, SqlBooks, SqlLedger, SqlOutboxNotifier, migrate_sqlite
from .adapters.gateway import DarajaGateway
from .adapters.security import PiiBox
from .models import InternalStatus, Invoice, TxnStatus
from .providers.base import EAT, ProviderError
from .providers.csv_source import CsvStatementSource, load_invoices_csv
from .providers.daraja import SANDBOX_URL, DarajaProvider
from .recovery import ActionStatus, ActionStore, ActionType, ApprovalDesk, ApprovalError, Executor, RecoveryConfig, Role, plan_from_data
from .recovery.ports import NotSentError
from .webhook import WebhookService, check_token, make_server

REQUIRED_ENV = ("DARAJA_CONSUMER_KEY", "DARAJA_CONSUMER_SECRET", "DARAJA_SHORTCODE", "DARAJA_PASSKEY", "DARAJA_CALLBACK_URL")


class OfflineGateway:
    """Used when no Daraja credentials are configured. Reads fail (so retry guards wait); sends are refused."""

    def get_status(self, request_id: str) -> TxnStatus:
        raise ProviderError("no Daraja gateway configured (set the DARAJA_* variables)")

    def send_push(self, **kw: Any) -> str:
        raise NotSentError("no Daraja gateway configured")

    def reverse(self, *a: Any, **kw: Any) -> str:
        raise NotSentError("no Daraja gateway configured")


def open_db(url: str) -> Database:
    db = Database.from_url(url)
    if db.dialect == "sqlite":
        db.script(SQLITE_DDL)
        for note in migrate_sqlite(db):
            print(f"note: upgraded {url}: {note}", file=sys.stderr)
    return db


def build_gateway(db: Database, ledger: SqlLedger) -> DarajaGateway | OfflineGateway:
    env = os.environ
    if any(not env.get(k) for k in REQUIRED_ENV):
        return OfflineGateway()
    base = env.get("DARAJA_BASE_URL", SANDBOX_URL)
    prov = DarajaProvider(env["DARAJA_CONSUMER_KEY"], env["DARAJA_CONSUMER_SECRET"], env["DARAJA_SHORTCODE"], env["DARAJA_PASSKEY"],
                          base_url=base, allow_production=env.get("PAYRECON_ALLOW_PRODUCTION") == "1")
    cert = Path(env["DARAJA_CERT_PATH"]).read_bytes() if env.get("DARAJA_CERT_PATH") else None
    return DarajaGateway(
        prov, db, ledger, callback_url=env["DARAJA_CALLBACK_URL"],
        pii=PiiBox(env.get("PAYRECON_PII_KEY"), allow_plaintext=_is_sandbox(base)),
        initiator=env.get("DARAJA_INITIATOR"), initiator_password=env.get("DARAJA_INITIATOR_PASSWORD"), cert_pem=cert,
        result_url=env.get("DARAJA_RESULT_URL") or sibling_url(env["DARAJA_CALLBACK_URL"], "reversal-result"),
        timeout_url=env.get("DARAJA_TIMEOUT_URL") or sibling_url(env["DARAJA_CALLBACK_URL"], "reversal-timeout"),
        security_credential=(env.get("DARAJA_SECURITY_CREDENTIAL") or "").strip().strip('"') or None,
        reversal_shortcode=env.get("DARAJA_REVERSAL_SHORTCODE") or env.get("DARAJA_C2B_SHORTCODE") or None,
    )


def sibling_url(callback_url: str, kind: str) -> str | None:
    """https://host/daraja/stk/<token> -> https://host/daraja/<kind>/<token>, so a new tunnel address is set in one place.
    None if the callback URL is not one of this receiver's."""
    marker = "/daraja/stk/"
    return callback_url.replace(marker, f"/daraja/{kind}/", 1) if marker in callback_url else None


def redact_url(url: str | None) -> str:
    """Show the host and the endpoint, never the token."""
    if not url:
        return "(not set)"
    u = urlparse(url)
    parts = u.path.strip("/").split("/")
    shown = "/".join(parts[:2]) + ("/<token>" if len(parts) > 2 else "")
    return f"{u.scheme}://{u.hostname}/{shown}"


def _is_sandbox(base: str) -> bool:
    return "sandbox" in base or (urlparse(base).hostname or "") in ("127.0.0.1", "localhost", "::1")


def passkey_looks_like_password(passkey: str, shortcode: str) -> bool:
    """True if the 'passkey' is really the portal's Password field (base64 of shortcode + passkey + timestamp)."""
    import base64
    import binascii

    try:
        raw = base64.b64decode(passkey, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return raw.startswith(shortcode) and len(raw) >= len(shortcode) + 14 and raw[-14:].isdigit()


PASSKEY_WARNING = ("WARNING: DARAJA_PASSKEY looks like the portal's Password field (base64 of shortcode + passkey + timestamp), not the passkey itself. "
                   "Daraja will answer 'Wrong credentials'. Use the Passkey field from the portal's M-Pesa Express simulate page "
                   "(usually 64 characters), or recover it with: base64-decode the value and keep the characters after the first 6 and before the last 14.")


def _roles() -> dict[str, Role]:
    return {u.strip(): Role.APPROVER for u in os.environ.get("PAYRECON_APPROVERS", "").split(",") if u.strip()}


def _fmt(c: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])) or "none"


# ---- commands --------------------------------------------------------------------------------

def cmd_init_db(args) -> int:
    db = open_db(args.db)
    print(f"ready: {args.db} ({db.dialect})" + ("" if db.dialect == "sqlite" else "; load schema.sql into Postgres yourself"))
    return 0


def cmd_import_csv(args) -> int:
    db = open_db(args.db)
    books, ledger = SqlBooks(db), SqlLedger(db)
    n = 0
    if args.invoices:
        existing = {i.invoice_id for i in books.invoices()}
        for inv in load_invoices_csv(args.invoices):
            if inv.invoice_id not in existing:
                books.add_invoice(inv)
                n += 1
    out = ledger.ingest_many(CsvStatementSource(args.transactions).load()) if args.transactions else {}
    msg = f"invoices added: {n}; transactions: {_fmt(Counter(out))}"
    if getattr(args, "reversals", None):
        from .providers import load_reversals_csv

        rev = ledger.ingest_many(load_reversals_csv(args.reversals))
        msg += f"; reversals: {_fmt(Counter({'applied' if k == 'upgraded' else 'recorded (payment not seen yet)' if k == 'inserted' else 'already reversed' if k == 'duplicate' else 'CONFLICT (amount differs: check by hand)': v for k, v in rev.items()}))}"
    print(msg)
    return 0


def cmd_plan(args) -> int:
    db = open_db(args.db)
    books, ledger = SqlBooks(db), SqlLedger(db)
    store = ActionStore(args.store)
    invoices, txns = books.invoices(), ledger.all_txns()
    kw = {}
    if getattr(args, "ai", False):
        # The AI layer only proposes links; the planner turns each into an action that needs a human approval.
        from .ai import investigate
        from .investigate import make_llm
        from .matcher import reconcile

        air = investigate(reconcile(invoices, txns, applied=books.applications()), invoices, txns, make_llm(args.llm, args.model))
        kw["link_proposals"] = air.link_proposals
        linked = [p for p in air.link_proposals if p.invoice_id]
        print(f"AI: {len(linked)} link proposal(s) for unlinked payments, {len(air.link_proposals) - len(linked)} abstained; LLM calls {air.llm_calls}")
    report, pr = plan_from_data(invoices, txns, store, applied=books.applications(), **kw)
    print(f"proposed {len(pr.proposed)} ({_fmt(Counter(a.action_type.value for a in pr.proposed))}); "
          f"{sum(a.requires_approval for a in pr.proposed)} need approval; already known {pr.existing}; "
          f"review items opened {pr.new_escalations}; skipped {len(pr.skipped)}")
    return 0


def cmd_dashboard(args) -> int:
    """Start the Streamlit dashboard on this machine only, with your own data enabled."""
    import importlib.util
    import subprocess

    if importlib.util.find_spec("streamlit") is None:
        print("the dashboard needs Streamlit: pip install -e '.[dashboard]'")
        return 2
    app = Path(__file__).with_name("dashboard_app.py")
    env = {**os.environ, "PAYRECON_DASHBOARD_LOCAL": "1"}
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1",
                            "--server.port", str(args.port), "--browser.gatherUsageStats", "false"], env=env)


def cmd_investigate(args) -> int:
    """The AI investigation layer over the live books and ledger. Read-only: it explains and proposes, nothing more."""
    from . import investigate as inv

    argv = ["--db", args.db, "--llm", args.llm, "--top", str(args.top)]
    argv += (["--model", args.model] if args.model else []) + (["--json", args.json] if args.json else [])
    return inv.main(argv)


def cmd_run(args) -> int:
    db = open_db(args.db)
    books, ledger = SqlBooks(db), SqlLedger(db)
    gw = build_gateway(db, ledger)
    store = ActionStore(args.store)
    ex = Executor(store, books, ledger, gw, gw, SqlOutboxNotifier(db), config=RecoveryConfig(), worker=args.worker)
    if isinstance(gw, OfflineGateway):
        print("note: no Daraja credentials in the environment; retries and refunds will be refused")
    if not args.execute:
        s = ex.run_once(dry_run=True)
        print(f"dry run: {len(s.would_run)} runnable actions")
        for aid, kind, verdict in s.would_run[:30]:
            print(f"  #{aid:<5} {kind:<22} {verdict}")
        print("nothing was changed; add --execute to run them")
        return 0
    s = ex.run_once()
    print(f"done {s.done}, cancelled {s.cancelled}, deferred {s.deferred}, unknown {s.unknown}, failed {s.failed}"
          + (f"; HALTED: {s.halted}" if s.halted else ""))
    for n in s.notes:
        print("note:", n)
    return 1 if s.halted else 0


def _desk(args) -> ApprovalDesk:
    roles = _roles()
    if not roles:
        raise SystemExit("set PAYRECON_APPROVERS to a comma-separated list of approver names (a stand-in for real authentication)")
    return ApprovalDesk(ActionStore(args.store), roles)


def cmd_approve(args) -> int:
    try:
        a = _desk(args).approve(args.id, args.as_user, args.note or "")
    except KeyError:
        print(f"refused: no action #{args.id}")
        return 1
    except ApprovalError as e:
        print("refused:", e)
        return 1
    print(f"approved action {a.action_id} ({a.action_type.value} on {a.invoice_id}) as {args.as_user}")
    return 0


def cmd_reject(args) -> int:
    try:
        a = _desk(args).reject(args.id, args.as_user, args.reason or "rejected")
    except KeyError:
        print(f"refused: no action #{args.id}")
        return 1
    except ApprovalError as e:
        print("refused:", e)
        return 1
    print(f"rejected action {a.action_id} as {args.as_user}")
    return 0


# ---- webhook receiver ----------------------------------------------------------------------------------

def build_service(args, need_token: bool = True) -> WebhookService:
    db = open_db(args.db)
    ledger = SqlLedger(db)
    gw = build_gateway(db, ledger)
    if isinstance(gw, OfflineGateway):
        raise SystemExit("no Daraja credentials in the environment (set the DARAJA_* variables): the receiver needs them to read the "
                         "requests you recorded and to verify successes with Daraja")
    if passkey_looks_like_password(gw.provider.passkey, gw.provider.shortcode):
        raise SystemExit(PASSKEY_WARNING)
    token = os.environ.get("PAYRECON_WEBHOOK_TOKEN") if need_token else secrets.token_urlsafe(32)
    try:
        check_token(token)
    except ValueError as e:
        raise SystemExit(f"PAYRECON_WEBHOOK_TOKEN: {e}") from None
    allow = {ip.strip() for ip in os.environ.get("PAYRECON_WEBHOOK_ALLOW_IPS", "").split(",") if ip.strip()}
    return WebhookService(gw, db, token, verify_success=os.environ.get("PAYRECON_VERIFY_SUCCESS", "1") != "0", allow_ips=allow,
                          log=lambda line: print(f"{datetime.now().strftime('%H:%M:%S')} {line}", flush=True))


def cmd_serve(args) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # under a pipe, a service manager or nohup these lines would otherwise be lost
    except (AttributeError, ValueError):
        pass
    svc = build_service(args)
    srv = make_server(svc, args.host, args.port)
    host, port = srv.server_address[:2]
    stop = threading.Event()

    def replay_loop() -> None:
        while not stop.wait(args.replay_every):
            try:
                done = svc.replay()
                if done:
                    print(f"replay: {done}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"replay failed: {type(e).__name__}: {e}", flush=True)

    if args.replay_every > 0:
        threading.Thread(target=replay_loop, daemon=True).start()
    print(f"listening on http://{host}:{port}  (verify successes: {svc.verify_success}; IP allowlist: {sorted(svc.allow_ips) or 'off'})")
    print("Daraja needs a public https URL (a tunnel) in front of this. The URLs Daraja will be given (token hidden):")
    cb = os.environ.get("DARAJA_CALLBACK_URL", "")
    print(f"  STK callback        {redact_url(cb)}   (DARAJA_CALLBACK_URL)")
    print(f"  reversal result     {redact_url(getattr(svc.gw, 'result_url', None))}")
    print(f"  reversal timeout    {redact_url(getattr(svc.gw, 'timeout_url', None))}")
    print(f"  C2B confirmation    {redact_url(sibling_url(cb, 'c2b'))}   (send it to Daraja with: c2b-register)")
    print(f"  C2B validation      {redact_url(sibling_url(cb, 'c2b-validation'))}")
    print("  The last four follow DARAJA_CALLBACK_URL unless DARAJA_RESULT_URL / DARAJA_TIMEOUT_URL are set.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: bound to {host}; this server speaks plain HTTP, so put it behind HTTPS (Daraja requires it).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        srv.server_close()
    return 0


def cmd_webhook_inbox(args) -> int:
    db = open_db(args.db)
    counts = dict(db.execute("SELECT status, COUNT(*) FROM webhook_inbox GROUP BY status").fetchall())
    print("inbox:", _fmt(Counter(counts)))
    q, a = "SELECT received_at, kind, status, attempts, detail FROM webhook_inbox", []
    if args.status:
        q, a = q + " WHERE status = ?", [args.status]
    else:
        q += " WHERE status NOT IN ('INGESTED', 'NOTED')"  # the ones that need attention
    for r in db.execute(q + " ORDER BY received_at DESC LIMIT ?", (*a, args.limit)).fetchall():
        print(f"  {str(r[0])[:19]} {r[1]:<17} {r[2]:<11} tries {r[3]}  {r[4] or ''}")
    return 0


def cmd_webhook_replay(args) -> int:
    done = build_service(args, need_token=False).replay(args.limit)
    print("replayed:", _fmt(Counter(done)))
    return 0


# ---- check your own sandbox credentials ----------------------------------------------------------

def cmd_sandbox_check(args) -> int:
    env = os.environ
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    print("environment:", ", ".join(f"{k}={'set' if env.get(k) else 'MISSING'}" for k in REQUIRED_ENV))
    if missing:
        print("set the missing variables (see .env.example) and run again")
        return 2
    base = env.get("DARAJA_BASE_URL", SANDBOX_URL)
    if not _is_sandbox(base):
        print("refusing: sandbox-check only talks to the sandbox")
        return 2
    db = open_db(getattr(args, "db", None) or ":memory:")
    ledger = SqlLedger(db)
    gw = build_gateway(db, ledger)
    assert isinstance(gw, DarajaGateway)
    if passkey_looks_like_password(gw.provider.passkey, gw.provider.shortcode):
        print(PASSKEY_WARNING)
        if not args.diagnose:
            return 2
    if args.diagnose:
        return _diagnose(gw, args)
    try:
        token = gw.provider._access_token()
        print(f"1. token: OK ({len(token)} characters)")
    except Exception as e:  # noqa: BLE001
        print(f"1. token: FAILED: {type(e).__name__}: {e}")
        return 1
    if args.query:
        code, body = gw.provider.query_raw(args.query)
        print(f"2. query {args.query}: HTTP {code}  raw={DarajaGateway._safe(body)}")
        try:
            print("   we read that as:", DarajaProvider.interpret_query_response(code, body).value)
        except ProviderError as e:
            print("   we could NOT interpret it:", e)
    if args.push:
        if not args.amount or not args.reference:
            print("--push needs --amount and --reference")
            return 2
        key = f"sandbox-check-{int(time.time())}"
        try:
            cid = gw.initiate(reference=args.reference, amount=Decimal(args.amount), msisdn=args.push, idempotency_key=key)
        except (NotSentError, Exception) as e:  # noqa: BLE001
            print(f"3. push: {type(e).__name__}: {e}")
            print("   last raw response:", getattr(gw, "last_response", None))
            return 1
        print(f"3. push accepted: CheckoutRequestID {cid}  raw={getattr(gw, 'last_response', None)}")
        print("   approve the prompt on the test handset (or let it time out), then watch the query answers:")
        for i in range(args.polls):
            time.sleep(args.interval)
            code, body = gw.provider.query_raw(cid)
            try:
                reading = DarajaProvider.interpret_query_response(code, body).value
            except ProviderError as e:
                reading = f"NOT INTERPRETED ({e})"
            print(f"   [{i + 1}] HTTP {code} raw={DarajaGateway._safe(body)}  -> {reading}")
    if args.reverse:
        if not args.amount:
            print("--reverse needs --amount (the amount of the payment to reverse)")
            return 2
        if args.receiver_party:
            gw.reversal_shortcode = args.receiver_party
        print(f"4. reversal of {args.reverse} for {args.amount} (ReceiverParty {gw.reversal_shortcode or gw.provider.shortcode}): result will go to {redact_url(gw.result_url)}, timeout to {redact_url(gw.timeout_url)}")
        try:
            conv = gw.reverse(args.reverse, Decimal(args.amount), f"sandbox-reverse-{int(time.time())}")
        except Exception as e:  # noqa: BLE001
            print(f"   reversal: {type(e).__name__}: {e}")
            print("   last raw response:", getattr(gw, "last_response", None))
            return 1
        print(f"   reversal accepted: ConversationID {conv}  raw={gw.last_response}")
        print("   the outcome arrives later on the result URL; then run: webhook-inbox --db <same db> --status INGESTED")
    if args.c2b_simulate:
        if not args.amount or not args.reference:
            print("--c2b-simulate needs --amount and --reference (what the customer types as the account number)")
            return 2
        sc = env.get("DARAJA_C2B_SHORTCODE") or gw.provider.shortcode
        try:
            out = gw.simulate_c2b(amount=Decimal(args.amount), msisdn=args.msisdn, bill_ref=args.reference, shortcode=sc)
        except Exception as e:  # noqa: BLE001
            print(f"5. C2B simulate to {sc}: {type(e).__name__}: {e}")
            print("   last raw response:", getattr(gw, "last_response", None))
            return 1
        print(f"5. C2B simulate to {sc} accepted: raw={DarajaGateway._safe(out)}")
        print("   the confirmation arrives on the registered C2B URL (run c2b-register first)")
    print("\nPaste this output back so the assumptions in gateway.py / daraja.py can be checked against what Daraja really returned.")
    return 0


def cmd_c2b_register(args) -> int:
    env = os.environ
    gw = build_gateway(open_db(":memory:"), SqlLedger(open_db(":memory:")))
    if isinstance(gw, OfflineGateway):
        print("set the DARAJA_* variables first")
        return 2
    cb = env["DARAJA_CALLBACK_URL"]
    conf = args.confirmation_url or sibling_url(cb, "c2b")
    val = args.validation_url or sibling_url(cb, "c2b-validation")
    if not conf or not val:
        print("could not derive the C2B URLs from DARAJA_CALLBACK_URL; pass --confirmation-url and --validation-url")
        return 2
    sc = args.shortcode or env.get("DARAJA_C2B_SHORTCODE") or gw.provider.shortcode
    print(f"registering for shortcode {sc}: confirmation {redact_url(conf)}, validation {redact_url(val)}, ResponseType {args.response_type}")
    try:
        out = gw.register_c2b_urls(conf, val, shortcode=sc, response_type=args.response_type)
    except Exception as e:  # noqa: BLE001
        print(f"refused or failed: {type(e).__name__}: {e}")
        print("last raw response:", getattr(gw, "last_response", None))
        return 1
    print(f"registered: raw={DarajaGateway._safe(out)}")
    return 0


def _diagnose(gw: DarajaGateway, args) -> int:
    """No network. Show the request we would send, secrets redacted, and compare our Password prefix with the portal's."""
    import base64

    pk = gw.provider.passkey
    print(f"passkey: length {len(pk)}, stray whitespace or quotes: {pk != pk.strip() or any(c in pk for c in chr(34) + chr(39) + ' ')}")
    for name, v in (("shortcode", gw.provider.shortcode), ("consumer key", gw.provider.consumer_key), ("consumer secret", gw.provider.consumer_secret)):
        print(f"{name}: length {len(v)}, stray whitespace or quotes: {v != v.strip() or any(c in v for c in chr(34) + chr(39) + ' ')}")
    cb = urlparse(gw.callback_url)
    print(f"callback URL: scheme {cb.scheme!r}, host {cb.hostname!r}, path length {len(cb.path)}, stray whitespace: {gw.callback_url != gw.callback_url.strip()}")
    body = gw.build_push_body(args.reference or "TEST", 1, "254708374149")
    shown = {k: ("<redacted>" if k == "Password" else v) for k, v in body.items()}
    shown["CallBackURL"] = f"{cb.scheme}://{cb.hostname}/..."
    print("request we would send:", shown)
    mine = body["Password"]
    print(f"Password starts: {mine[:23]}  (the first 23 characters cover the shortcode and the start of the passkey; they do not depend on the time)")
    if args.compare_password_prefix:
        want = args.compare_password_prefix
        ok = mine.startswith(want)
        print(f"portal shows: {want}  ->  {'MATCH' if ok else 'DIFFERENT: the shortcode or the start of the passkey is not what the portal uses'}")
        if want:
            try:
                print("the portal prefix decodes to:", base64.b64decode(want + "=" * (-len(want) % 4)).decode(errors="replace"))
            except Exception:  # noqa: BLE001
                pass
    return 0


# ---- the whole stack, locally, no credentials -----------------------------------------------------

def cmd_live_demo(args) -> int:
    from .adapters.fake_daraja import FakeDaraja, make_test_certificate
    from cryptography.fernet import Fernet

    def stk_cb(cid: str, code: int, receipt: str | None = None, amount: int = 0) -> dict:
        cb: dict[str, Any] = {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": code,
                              "ResultDesc": "ok" if code == 0 else "DS timeout user cannot be reached" if code == 1037 else "Insufficient balance"}
        if code == 0:
            cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                               {"Name": "TransactionDate", "Value": 20261019101530}, {"Name": "PhoneNumber", "Value": 254708374149}]}
        return {"Body": {"stkCallback": cb}}

    def c2b(tid: str, ref: str, minutes: int, amount: str) -> dict:
        return {"TransID": tid, "TransAmount": amount, "BillRefNumber": ref, "MSISDN": "254708374149",
                "TransTime": (datetime(2026, 10, 19, 9, tzinfo=EAT) + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S")}

    with FakeDaraja() as fd:
        db = Database.sqlite(args.db or ":memory:")
        db.script(SQLITE_DDL)
        books, ledger, outbox = SqlBooks(db), SqlLedger(db), SqlOutboxNotifier(db)
        prov = DarajaProvider("key", "secret", "174379", "passkey", base_url=fd.url)
        gw = DarajaGateway(prov, db, ledger, callback_url="https://example.com/callback", pii=PiiBox(Fernet.generate_key().decode()),
                           initiator="testapi", initiator_password="pw", cert_pem=make_test_certificate()[1],
                           result_url="https://example.com/result", timeout_url="https://example.com/timeout", read_timeout=2.0)
        store = ActionStore(":memory:")
        desk = ApprovalDesk(store, {"alice": Role.APPROVER})
        cfg = RecoveryConfig(retry_cooldown_s=0)
        ex = Executor(store, books, ledger, gw, gw, outbox, config=cfg)
        now = datetime(2026, 10, 18, 9, tzinfo=EAT)
        for i, (ref, amt) in enumerate((("INV-0001", 1500), ("INV-0002", 2500), ("INV-0003", 1000)), start=1):
            books.add_invoice(Invoice(f"I{i}", ref, Decimal(amt), InternalStatus.UNPAID, now))
        print("1. SETUP      3 unpaid invoices; the fake Daraja is listening on", fd.url)
        a = gw.initiate(reference="INV-0001", amount=Decimal(1500), msisdn="0708374149", idempotency_key="first-1")
        b = gw.initiate(reference="INV-0002", amount=Decimal(2500), msisdn="0708374149", idempotency_key="first-2")
        fd.statuses[a] = fd.statuses[b] = "FAILED"
        fd.fail_codes[a], fd.fail_codes[b] = "1037", "1"
        gw.handle_callback(stk_cb(a, 1037))  # I1: timeout, retryable
        gw.handle_callback(stk_cb(b, 1))  # I2: insufficient funds, the customer must act
        gw.handle_callback(c2b("RKTQDM7W6S", "INV-0003", 1, "1000.00"))
        gw.handle_callback(c2b("RKTQDM7W9T", "INV-0003", 6, "1000.00"))  # I3: paid twice
        print("              I1 push timed out (1037); I2 had insufficient funds (1); I3 was paid twice")
        pr = plan_from_data(books.invoices(), ledger.all_txns(), store, config=cfg)[1]
        print(f"2. PLAN       {_fmt(Counter(x.action_type.value for x in pr.proposed))}; {sum(x.requires_approval for x in pr.proposed)} need approval")
        s = ex.run_once()
        pushes = len(fd.calls_to("/mpesa/stkpush/v1/processrequest"))
        print(f"3. RUN        done {s.done}: Daraja got {pushes - 2} retry push; {len(outbox.queued())} customer notice queued; the refund waits for a person")
        new = store.list_actions(action_type=ActionType.RETRY_PUSH)[0].request_id
        fd.statuses[new] = "SUCCESS"
        gw.handle_callback(stk_cb(new, 0, "NLJ7RT61SV", 1500))
        print("4. CALLBACK   the customer entered their PIN; Daraja's success callback is folded into the pending push")
        plan_from_data(books.invoices(), ledger.all_txns(), store, config=cfg)
        s = ex.run_once()
        print(f"5. APPLY      done {s.done}: I1 is now {books.get_invoice('I1').internal_status.value} (receipt NLJ7RT61SV applied to {books.paid_by('NLJ7RT61SV')})")
        refund = [x for x in store.list_actions(ActionStatus.PROPOSED) if x.requires_approval][0]
        desk.approve(refund.action_id, "alice", "customer confirmed the double payment")
        s = ex.run_once()
        conv = db.execute("SELECT conversation_id FROM refund_requests").fetchone()[0]
        gw.ingest_reversal_result({"Result": {"ResultCode": 0, "ResultDesc": "ok", "ConversationID": conv}})
        print(f"6. REFUND     alice approved; done {s.done}: Daraja got {len(fd.calls_to('/mpesa/reversal/'))} reversal of {refund.payload['refund_txn_id']}; result callback: "
              f"{db.execute('SELECT status FROM refund_requests').fetchone()[0]}")
        for _ in range(3):
            plan_from_data(books.invoices(), ledger.all_txns(), store, config=cfg)
            ex.run_once()
        calls = Counter(c["path"].rsplit("/", 1)[-1] for c in fd.calls if c["path"] != "/oauth/v1/generate")
        ok, _bad = store.verify_audit()
        stored = db.execute("SELECT msisdn_enc FROM stk_requests LIMIT 1").fetchone()[0]
        print(f"7. NO REPEATS after three more planning and execution rounds, Daraja saw: {dict(calls)}")
        print(f"8. VERIFY     audit {len(store.audit_events())} events, chain {'intact' if ok else 'BROKEN'}; stored payer number is {stored[:14]}... (encrypted)")
        good = (pushes - 2 == 1 and len(fd.calls_to('/mpesa/reversal/')) == 1 and ok and books.get_invoice('I1').internal_status is InternalStatus.PAID)
        return 0 if good else 1
