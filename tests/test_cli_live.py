"""The live CLI: import, plan, dry-run, execute, approvals, sandbox-check (against the local fake), live-demo."""
import contextlib
import io
import os

import pytest

from payrecon.adapters.fake_daraja import FakeDaraja
from payrecon.providers.base import ProviderError
from payrecon.recover import main
from payrecon.recovery.ports import NotSentError
from payrecon import livecli

INVOICES = """invoice_id,reference,amount,internal_status,created_at,customer_name
I1,INV-0001,1500,UNPAID,2026-10-17T09:00:00,Jane
I2,INV-0002,2000,UNPAID,2026-10-17T09:00:00,Joe
"""
TXNS = """provider_txn_id,amount,status,timestamp,reference,msisdn,result_code,result_desc,request_id
RK1,1500,SUCCESS,2026-10-18T09:00:00,INV-0001,254708374149,0,,
"""
DARAJA_VARS = ("DARAJA_CONSUMER_KEY", "DARAJA_CONSUMER_SECRET", "DARAJA_SHORTCODE", "DARAJA_PASSKEY", "DARAJA_CALLBACK_URL", "DARAJA_BASE_URL",
               "PAYRECON_APPROVERS", "PAYRECON_PII_KEY", "PAYRECON_WEBHOOK_TOKEN", "PAYRECON_VERIFY_SUCCESS", "PAYRECON_WEBHOOK_ALLOW_IPS",
               "DARAJA_INITIATOR", "DARAJA_INITIATOR_PASSWORD", "DARAJA_CERT_PATH", "DARAJA_RESULT_URL", "DARAJA_TIMEOUT_URL", "DARAJA_C2B_SHORTCODE", "DARAJA_SECURITY_CREDENTIAL", "DARAJA_REVERSAL_SHORTCODE",
               "DARAJA_B2C_SHORTCODE", "DARAJA_B2C_COMMAND", "DARAJA_B2C_RESULT_URL", "DARAJA_B2C_TIMEOUT_URL")


@contextlib.contextmanager
def env(**kw):
    saved = {k: os.environ.get(k) for k in DARAJA_VARS}
    try:
        for k in DARAJA_VARS:
            os.environ.pop(k, None)
        os.environ.update(kw)
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


def books(tmp_path):
    (tmp_path / "inv.csv").write_text(INVOICES)
    (tmp_path / "tx.csv").write_text(TXNS)
    db, store = str(tmp_path / "app.db"), str(tmp_path / "act.db")
    assert run("init-db", "--db", db)[0] == 0
    code, out = run("import-csv", "--db", db, "--invoices", str(tmp_path / "inv.csv"), "--transactions", str(tmp_path / "tx.csv"))
    assert code == 0 and "invoices added: 2" in out and "inserted 1" in out
    return db, store


def test_import_is_repeatable_and_plan_is_idempotent(tmp_path):
    db, store = books(tmp_path)
    _, out = run("import-csv", "--db", db, "--invoices", str(tmp_path / "inv.csv"), "--transactions", str(tmp_path / "tx.csv"))
    assert "invoices added: 0" in out and "duplicate 1" in out
    assert "proposed 1 (APPLY_PAYMENT 1)" in run("plan", "--db", db, "--store", store)[1]
    assert "proposed 0" in run("plan", "--db", db, "--store", store)[1]


def test_dry_run_changes_nothing_and_execute_applies_exactly_once(tmp_path):
    with env():
        db, store = books(tmp_path)
        run("plan", "--db", db, "--store", store)
        code, out = run("run", "--db", db, "--store", store)
        assert code == 0 and "dry run: 1 runnable" in out and "nothing was changed" in out
        assert "PAID" not in run("queue", "--db", store)[1]
        code, out = run("run", "--db", db, "--store", store, "--execute")
        assert code == 0 and "done 1" in out
        assert "done 0" in run("run", "--db", db, "--store", store, "--execute")[1]
        code, out = run("audit", "--db", store)
        assert code == 0 and "intact" in out and "action.done" in out


def test_without_credentials_the_gateway_reads_fail_and_sends_are_refused():
    gw = livecli.OfflineGateway()
    with pytest.raises(ProviderError):
        gw.get_status("ws_CO_1")
    with pytest.raises(NotSentError):
        gw.send_push(retry_of="x", amount=1, idempotency_key="k")
    with pytest.raises(NotSentError):
        gw.reverse("TX", 1, "k")


def test_approving_needs_a_configured_approver(tmp_path):
    with env():
        try:
            run("approve", "1", "--store", str(tmp_path / "a.db"), "--as", "alice")
            raise AssertionError("expected the CLI to refuse")
        except SystemExit as e:
            assert "PAYRECON_APPROVERS" in str(e)
    with env(PAYRECON_APPROVERS="alice"):
        code, out = run("approve", "99", "--store", str(tmp_path / "a.db"), "--as", "alice")
        assert code == 1 and "refused" in out


def test_sandbox_check_reports_what_is_missing_and_refuses_production():
    with env():
        code, out = run("sandbox-check")
        assert code == 2 and "MISSING" in out
    prod = dict(DARAJA_CONSUMER_KEY="k", DARAJA_CONSUMER_SECRET="s", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="p",
                DARAJA_CALLBACK_URL="https://example.com/cb", DARAJA_BASE_URL="https://api.safaricom.co.ke")
    with env(**prod):
        code, out = run("sandbox-check")
        assert code == 2 and "only talks to the sandbox" in out


def test_sandbox_check_pushes_and_prints_the_raw_answers():
    with FakeDaraja() as fd:
        local = dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="hunter2-passkey",
                     DARAJA_CALLBACK_URL="https://example.com/cb", DARAJA_BASE_URL=fd.url)
        with env(**local):
            code, out = run("sandbox-check", "--push", "0708374149", "--amount", "1", "--reference", "TEST1", "--polls", "1", "--interval", "0")
        assert code == 0 and "1. token: OK" in out and "3. push accepted: CheckoutRequestID" in out and "[1] HTTP" in out
        assert "SecurityCredential" not in out and "hunter2-passkey" not in out and "secret" not in out.replace("DARAJA_CONSUMER_SECRET", "")
        assert len(fd.calls_to("/mpesa/stkpush/v1/processrequest")) == 1
        with env(**local):
            code, out = run("sandbox-check", "--push", "0708374149", "--amount", "1.50", "--reference", "TEST1")
        assert code == 1 and "whole number of shillings" in out and len(fd.calls_to("/mpesa/stkpush/v1/processrequest")) == 1


def test_a_push_made_by_sandbox_check_is_recognised_by_the_receiver_when_they_share_a_db(tmp_path):
    """Without --db the push lived in a throwaway in-memory database, so a real success callback would have been
    quarantined as 'never recorded'."""
    import json
    from decimal import Decimal
    from payrecon.webhook import WebhookService

    dbfile = str(tmp_path / "app.db")
    tok = "t" * 32
    with FakeDaraja() as fd:
        local = dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="hunter2-passkey",
                     DARAJA_CALLBACK_URL="https://example.com/cb", DARAJA_BASE_URL=fd.url)
        with env(**local):
            code, out = run("sandbox-check", "--push", "254708374149", "--amount", "1", "--reference", "TEST9", "--polls", "0",
                            "--db", dbfile)
            assert code == 0
            db = livecli.open_db(dbfile)
            cid = db.execute("SELECT checkout_request_id FROM stk_requests").fetchone()[0]
            gw = livecli.build_gateway(db, livecli.SqlLedger(db))
            svc = WebhookService(gw, db, tok, log=lambda m: None)
            fd.statuses[cid] = "SUCCESS"
            cb = {"Body": {"stkCallback": {"MerchantRequestID": "m", "CheckoutRequestID": cid, "ResultCode": 0, "ResultDesc": "ok",
                  "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 1}, {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
                                                {"Name": "PhoneNumber", "Value": 254708374149}]}}}}
            status, _ = svc.handle("POST", f"/daraja/stk/{tok}", json.dumps(cb).encode())
        assert status == 200
        assert db.execute("SELECT status FROM webhook_inbox").fetchone()[0] == "INGESTED"


def test_live_demo_runs_the_whole_stack_and_exits_clean():
    code, out = run("live-demo")
    assert code == 0 and "chain intact" in out and "result callback: COMPLETED" in out and "(encrypted)" in out


def test_diagnose_makes_no_request_hides_secrets_and_compares_the_password_prefix():
    with FakeDaraja() as fd:
        local = dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="hunter2-passkey",
                     DARAJA_CALLBACK_URL="https://example.com/cb/very-secret-token", DARAJA_BASE_URL=fd.url)
        import base64
        prefix = base64.b64encode(b"174379hunter2-passkey").decode()[:23]
        with env(**local):
            code, out = run("sandbox-check", "--diagnose", "--compare-password-prefix", prefix)
            assert code == 0 and "MATCH" in out and "DIFFERENT" not in out
            code, out2 = run("sandbox-check", "--diagnose", "--compare-password-prefix", "MTc0Mzc5Ym")
        assert "DIFFERENT" in out2
        for text in (out, out2):
            assert "hunter2-passkey" not in text and "very-secret-token" not in text and "<redacted>" in text
        assert fd.calls == []  # nothing was sent, not even the token request


def test_a_passkey_that_is_really_the_portals_password_field_is_caught_before_anything_is_sent():
    import base64
    from payrecon.livecli import passkey_looks_like_password
    real = "a" * 64
    pasted = base64.b64encode(f"174379{real}20261019150324".encode()).decode()
    assert passkey_looks_like_password(pasted, "174379") and len(pasted) == 112
    assert not passkey_looks_like_password(real, "174379")
    assert not passkey_looks_like_password("not base64 !!", "174379")
    with FakeDaraja() as fd:
        local = dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY=pasted,
                     DARAJA_CALLBACK_URL="https://example.com/cb", DARAJA_BASE_URL=fd.url)
        with env(**local):
            code, out = run("sandbox-check", "--push", "254708374149", "--amount", "1", "--reference", "T")
        assert code == 2 and "looks like the portal's Password field" in out
        assert fd.calls_to("/mpesa/stkpush/v1/processrequest") == []


# ---- the webhook commands -------------------------------------------------------------------------------

WH_TOKEN = "w" * 20 + "-" + "z" * 15


def _local_env(fd, **more):
    return dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="plain-passkey",
                DARAJA_CALLBACK_URL="https://example.com/cb", DARAJA_BASE_URL=fd.url, **more)


def test_serve_refuses_to_start_without_credentials_a_proper_token_or_a_sane_passkey(tmp_path):
    import base64
    from types import SimpleNamespace
    args = SimpleNamespace(db=str(tmp_path / "app.db"))
    with FakeDaraja() as fd:
        with env():
            with pytest.raises(SystemExit, match="no Daraja credentials"):
                livecli.build_service(args)
        with env(**_local_env(fd)):
            with pytest.raises(SystemExit, match="PAYRECON_WEBHOOK_TOKEN"):
                livecli.build_service(args)
        with env(**_local_env(fd, PAYRECON_WEBHOOK_TOKEN="too-short")):
            with pytest.raises(SystemExit, match="at least 24"):
                livecli.build_service(args)
        pasted = base64.b64encode(("174379" + "a" * 64 + "20261019150324").encode()).decode()
        bad = {**_local_env(fd, PAYRECON_WEBHOOK_TOKEN=WH_TOKEN), "DARAJA_PASSKEY": pasted}
        with env(**bad):
            with pytest.raises(SystemExit, match="looks like the portal's Password"):
                livecli.build_service(args)
        with env(**_local_env(fd, PAYRECON_WEBHOOK_TOKEN=WH_TOKEN, PAYRECON_VERIFY_SUCCESS="0", PAYRECON_WEBHOOK_ALLOW_IPS="196.201.212.69, 10.0.0.1")):
            svc = livecli.build_service(args)
            assert svc.verify_success is False and svc.allow_ips == {"196.201.212.69", "10.0.0.1"}


def test_the_serve_command_end_to_end_receives_a_real_callback_and_never_prints_the_secret(tmp_path):
    import http.client
    import json
    import socket
    import subprocess
    import sys
    from decimal import Decimal
    from payrecon.adapters import Database, SqlLedger
    from payrecon.adapters.security import PiiBox as Box
    from payrecon.providers.daraja import DarajaProvider
    from payrecon.adapters.gateway import DarajaGateway as GW
    from payrecon.adapters import SQLITE_DDL as DDL

    cid, mid = "ws_CO_191020261544137708374149", "ac7b-470b-8df7-23ab3cef38271862477"
    payload = {"Body": {"stkCallback": {"MerchantRequestID": mid, "CheckoutRequestID": cid, "ResultCode": 1032, "ResultDesc": "Request Cancelled by user."}}}
    dbfile = str(tmp_path / "app.db")
    seed = Database.sqlite(dbfile)
    seed.script(DDL)
    GW(DarajaProvider("key", "secret", "174379", "plain-passkey", base_url="http://127.0.0.1:9"), seed, SqlLedger(seed),
       callback_url="https://example.com/cb", pii=Box(None, allow_plaintext=True)).record_request(cid, mid, "TEST4", Decimal(1), "254708374149", "k")
    seed.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with FakeDaraja() as fd:
        # PYTHONUNBUFFERED must not leak in: it hides output that is stuck in a buffer, which is exactly what this test checks for
        child_env = {**{k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}, **_local_env(fd, PAYRECON_WEBHOOK_TOKEN=WH_TOKEN),
                     "PYTHONPATH": os.path.join(os.getcwd(), "src")}
        proc = subprocess.Popen([sys.executable, "-m", "payrecon.recover", "serve", "--db", dbfile, "--port", str(port), "--replay-every", "0"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=child_env)
        try:
            import select
            ready, _, _ = select.select([proc.stdout], [], [], 8)
            assert ready, "no output within 8 s: the startup lines are stuck in a buffer (stdout is a pipe here, as under nohup or systemd)"
            first = proc.stdout.readline()
            assert "listening on" in first, first
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", f"/daraja/stk/{WH_TOKEN}", body=json.dumps(payload), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            assert r.status == 200 and json.loads(r.read()) == {"ResultCode": 0, "ResultDesc": "Accepted"}
            c.request("POST", f"/daraja/stk/{WH_TOKEN}wrong", body="{}")
            assert c.getresponse().status == 404
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            out = first + proc.stdout.read()  # read(), not communicate(): readline() already buffered the lines after the first
        detail = Database.sqlite(dbfile).execute("SELECT status, detail FROM webhook_inbox").fetchall()
        assert WH_TOKEN not in out and "STK callback" in out and "stored: INGESTED" in out, (out, detail)
        with env(**_local_env(fd)):
            code, text = run("webhook-inbox", "--db", dbfile, "--status", "INGESTED")
        assert code == 0 and "INGESTED 1" in text and "stk" in text


def test_webhook_inbox_lists_only_what_needs_attention_and_replay_reports(tmp_path):
    with FakeDaraja() as fd:
        dbfile = str(tmp_path / "app.db")
        with env(**_local_env(fd, PAYRECON_WEBHOOK_TOKEN=WH_TOKEN)):
            from types import SimpleNamespace
            svc = livecli.build_service(SimpleNamespace(db=dbfile))
            svc.handle("POST", f"/daraja/stk/{WH_TOKEN}", b'{"hello": "world"}')  # unrecognisable: quarantined
            code, text = run("webhook-inbox", "--db", dbfile)
            assert code == 0 and "QUARANTINED 1" in text and "not a recognisable STK callback" in text
            code, text = run("webhook-replay", "--db", dbfile)
            assert code == 0 and "replayed: none" in text  # quarantined items are for a person, not for retrying


TOK = "T" * 32


def _live_env(fd, **more):
    return dict(DARAJA_CONSUMER_KEY="key", DARAJA_CONSUMER_SECRET="secret", DARAJA_SHORTCODE="174379", DARAJA_PASSKEY="hunter2-passkey",
                DARAJA_CALLBACK_URL=f"https://tunnel.example.com/daraja/stk/{TOK}", DARAJA_BASE_URL=fd.url, **more)


def test_the_other_receiver_urls_follow_the_callback_url_and_are_printed_without_the_token():
    cb = f"https://h.example.com/daraja/stk/{TOK}"
    assert livecli.sibling_url(cb, "c2b") == f"https://h.example.com/daraja/c2b/{TOK}"
    assert livecli.sibling_url("https://webhook.site/abc", "c2b") is None
    assert livecli.redact_url(cb) == "https://h.example.com/daraja/stk/<token>" and TOK not in livecli.redact_url(cb)


def test_c2b_register_sends_the_derived_urls_and_refuses_bad_ones():
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_C2B_SHORTCODE="600999")):
            code, out = run("c2b-register")
        assert code == 0 and "registered" in out and TOK not in out
        assert fd.c2b_urls == {"ShortCode": "600999", "ResponseType": "Completed",
                               "ConfirmationURL": f"https://tunnel.example.com/daraja/c2b/{TOK}",
                               "ValidationURL": f"https://tunnel.example.com/daraja/c2b-validation/{TOK}"}
        with env(**_live_env(fd)):
            code, out = run("c2b-register", "--confirmation-url", "http://plain.example.com/x", "--validation-url", "https://a.example.com/y")
            assert code == 1 and "https" in out
            code, out = run("c2b-register", "--confirmation-url", "https://a.example.com/mpesa/x", "--validation-url", "https://a.example.com/y")
            assert code == 1 and "mpesa" in out
        assert len(fd.calls_to("/mpesa/c2b/v1/registerurl")) == 1


def test_a_simulated_c2b_payment_is_sent_only_to_the_sandbox():
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_C2B_SHORTCODE="600999")):
            code, out = run("sandbox-check", "--c2b-simulate", "--amount", "10", "--reference", "INV-0001", "--polls", "0")
        assert code == 0 and "C2B simulate to 600999 accepted" in out
        [call] = fd.calls_to("/mpesa/c2b/v1/simulate")
        assert call["json"] == {"ShortCode": "600999", "CommandID": "CustomerPayBillOnline", "Amount": 10, "Msisdn": "254708374149",
                                "BillRefNumber": "INV-0001"}


def test_a_reversal_from_sandbox_check_completes_when_its_result_reaches_the_receiver(tmp_path):
    import json
    from payrecon.adapters.fake_daraja import make_test_certificate
    from payrecon.webhook import WebhookService

    cert = tmp_path / "cert.pem"
    cert.write_bytes(make_test_certificate()[1])
    dbfile = str(tmp_path / "app.db")
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_INITIATOR="testapi", DARAJA_INITIATOR_PASSWORD="pw", DARAJA_CERT_PATH=str(cert))):
            code, out = run("sandbox-check", "--reverse", "UJJA0AW035", "--amount", "1", "--polls", "0", "--db", dbfile)
            assert code == 0 and "reversal accepted: ConversationID" in out
            assert "reversal-result/<token>" in out and TOK not in out
            [call] = fd.calls_to("/mpesa/reversal/v1/request")
            assert call["json"]["ResultURL"] == f"https://tunnel.example.com/daraja/reversal-result/{TOK}"
            assert call["json"]["TransactionID"] == "UJJA0AW035" and call["json"]["Amount"] == 1
            db = livecli.open_db(dbfile)
            conv = db.execute("SELECT conversation_id FROM refund_requests").fetchone()[0]
            svc = WebhookService(livecli.build_gateway(db, livecli.SqlLedger(db)), db, TOK)
            result = {"Result": {"ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
                                 "OriginatorConversationID": "x", "ConversationID": conv, "TransactionID": "UJJB000000"}}
            assert svc.handle("POST", f"/daraja/reversal-result/{TOK}", json.dumps(result).encode())[0] == 200
        assert db.execute("SELECT status FROM refund_requests").fetchone()[0] == "COMPLETED"
        assert db.execute("SELECT status FROM webhook_inbox").fetchone()[0] == "INGESTED"


def test_a_reversal_without_its_settings_is_refused_before_anything_is_sent():
    with FakeDaraja() as fd:
        with env(**_live_env(fd)):
            code, out = run("sandbox-check", "--reverse", "UJJA0AW035", "--amount", "1", "--polls", "0")
        assert code == 1 and "NotSentError" in out and "initiator" in out
        assert fd.calls_to("/mpesa/reversal/v1/request") == []


def test_a_ready_made_security_credential_replaces_the_password_and_certificate():
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_INITIATOR="testapi", DARAJA_SECURITY_CREDENTIAL='"SE9n3qK1oush4QvD5Zls6r7N=="')):
            code, out = run("sandbox-check", "--reverse", "UJJA0AW035", "--amount", "1", "--polls", "0")
        assert code == 0 and "reversal accepted" in out and "SE9n3qK1" not in out
        [call] = fd.calls_to("/mpesa/reversal/v1/request")
        assert call["json"]["SecurityCredential"] == "SE9n3qK1oush4QvD5Zls6r7N=="  # pasted quotes stripped


def test_c2b_register_accepts_the_eight_zero_success_code_seen_live():
    """Daraja's sandbox answered registerurl with ResponseCode '00000000' 'Success' (2026-10-19); it was misread as a refusal."""
    live = {"OriginatorCoversationID": "8f38-49f8-9be4-b87d691148b42303213", "ResponseCode": "00000000", "ResponseDescription": "Success"}
    with FakeDaraja() as fd:
        fd.fault("/mpesa/c2b/v1/registerurl", "status", code=200, body=live, count=1)
        with env(**_live_env(fd, DARAJA_C2B_SHORTCODE="600978")):
            code, out = run("c2b-register")
        assert code == 0 and "registered" in out
        fd.fault("/mpesa/c2b/v1/registerurl", "status", code=200, body={**live, "ResponseCode": "00000001"}, count=1)
        with env(**_live_env(fd, DARAJA_C2B_SHORTCODE="600978")):
            code, out = run("c2b-register")
        assert code == 1 and "declined" in out


def test_a_reversal_names_the_paybill_that_received_the_payment_not_the_stk_shortcode():
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_INITIATOR="testapi", DARAJA_SECURITY_CREDENTIAL="cred", DARAJA_C2B_SHORTCODE="600978")):
            code, out = run("sandbox-check", "--reverse", "UJK030FEJ6", "--amount", "1", "--polls", "0")
            assert code == 0 and "ReceiverParty 600978" in out
            code, out = run("sandbox-check", "--reverse", "UJK030FEJ7", "--amount", "1", "--polls", "0", "--receiver-party", "174379")
            assert code == 0
        first, second = fd.calls_to("/mpesa/reversal/v1/request")
        assert (first["json"]["ReceiverParty"], second["json"]["ReceiverParty"]) == ("600978", "174379")


LIVE_LIKE_TXNS = """provider_txn_id,amount,status,timestamp,reference,msisdn,result_code,result_desc,request_id
ws_CO_A,1,PENDING,2026-10-19T20:28:09,TEST6,2547****166,,,
ws_CO_B,1,PENDING,2026-10-20T12:12:12,TEST6,2547****166,,,
UJKA0AXL4X,1,SUCCESS,2026-10-20T12:12:30,TEST6,2547****166,0,,ws_CO_B
UJK030FEJ6,1.00,SUCCESS,2026-10-20T00:09:23,INV-0001,2547****149,0,,
UJK0ORPHAN,1500.00,SUCCESS,2026-10-20T12:30:00,3,2547****149,0,,
"""


def _live_like_db(tmp_path):
    from pathlib import Path
    dbfile, tx = str(tmp_path / "app.db"), tmp_path / "tx.csv"
    tx.write_text(LIVE_LIKE_TXNS)
    examples = Path(__file__).resolve().parents[1] / "examples" / "live_invoices.csv"
    assert run("import-csv", "--db", dbfile, "--invoices", str(examples), "--transactions", str(tx))[0] == 0
    return dbfile


def test_investigate_reads_the_live_database_and_flags_the_double_pushed_invoice(tmp_path):
    dbfile = _live_like_db(tmp_path)
    code, out = run("investigate", "--db", dbfile)
    assert code == 0 and "EXCEPTION QUEUE" in out and "LLM calls: 0" in out
    assert "UJK030FEJ6" in out and "Safe to" in out                      # the C2B payment can be applied
    assert "second transaction is still pending" in out                  # TEST6: recheck before applying anything
    assert "UJK0ORPHAN" in out and "L5" in out                           # a payer who typed only "3" for KSh 1,500: proposed for INV-0003, as advice


def test_plan_with_ai_turns_a_link_proposal_into_an_action_that_needs_approval(tmp_path):
    from payrecon.recovery.store import ActionStore

    dbfile, store = _live_like_db(tmp_path), str(tmp_path / "store.db")
    code, out = run("plan", "--db", dbfile, "--store", store)
    assert code == 0 and "0 need approval" in out
    code, out = run("plan", "--db", dbfile, "--store", store, "--ai")
    assert code == 0 and "AI: 1 link proposal" in out and "1 need approval" in out
    linked = [a for a in ActionStore(store).list_actions() if a.invoice_id == "L5"]
    assert len(linked) == 1 and linked[0].requires_approval and linked[0].action_type.value == "APPLY_LINKED_PAYMENT"


def test_an_auto_apply_planned_before_a_reversal_is_cancelled_when_it_runs_after_it(tmp_path):
    """TEST7 live: planned as 'safe to mark paid', then Safaricom reversed the payment before anyone ran it."""
    from payrecon import livecli as L
    from payrecon.models import InternalStatus

    dbfile, store = str(tmp_path / "app.db"), str(tmp_path / "store.db")
    (tmp_path / "inv.csv").write_text(INVOICES)
    (tmp_path / "tx.csv").write_text(TXNS)
    (tmp_path / "rev.csv").write_text("original_receipt,reversal_receipt,amount,reversed_at\nRK1,RKREV1,1500,2026-10-18T10:00:00\n")
    with env():
        assert run("import-csv", "--db", dbfile, "--invoices", str(tmp_path / "inv.csv"), "--transactions", str(tmp_path / "tx.csv"))[0] == 0
        code, out = run("plan", "--db", dbfile, "--store", store)
        assert "APPLY_PAYMENT 1" in out
        code, out = run("import-csv", "--db", dbfile, "--reversals", str(tmp_path / "rev.csv"))
        assert code == 0 and "reversals: applied 1" in out
        code, out = run("run", "--db", dbfile, "--store", store, "--execute")
        assert code == 0 and "done 0" in out and "cancelled 1" in out
        code, out = run("investigate", "--db", dbfile)
        flat = " ".join(out.split())  # the report wraps at 100 columns
        assert "REVERSED_PAYMENT" in flat and "RK1" in flat and "outside payrecon" in flat
    db = L.open_db(dbfile)
    assert L.SqlBooks(db).get_invoice("I1").internal_status is InternalStatus.UNPAID


def test_payroll_from_the_command_line_imports_approves_sends_and_settles_from_the_results(tmp_path):
    import json
    from payrecon.webhook import WebhookService

    (tmp_path / "pay.csv").write_text("name,phone,amount\nGrace Wanjiku,0712345678,1500\nPeter Otieno,0722000111,1200\n")
    (tmp_path / "bad.csv").write_text("name,phone,amount\nJane,0812345678,1500\nJoe,0712345678,1.5\n")
    dbfile = str(tmp_path / "app.db")
    with FakeDaraja() as fd:
        with env(**_live_env(fd, DARAJA_INITIATOR="testapi", DARAJA_SECURITY_CREDENTIAL="cred", DARAJA_C2B_SHORTCODE="600978",
                             PAYRECON_APPROVERS="mike")):
            code, out = run("payroll", "import", "--db", dbfile, "--batch", "W43", "--file", str(tmp_path / "bad.csv"), "--as", "mike")
            assert code == 1 and "nothing imported: 2 problem(s)" in out
            code, out = run("payroll", "import", "--db", dbfile, "--batch", "W43", "--file", str(tmp_path / "pay.csv"), "--as", "mike")
            assert code == 0 and "2 people, total KSh 2,700" in out and "0712345678" not in out
            code, out = run("payroll", "send", "--db", dbfile, "--batch", "W43", "--execute")
            assert code == 1 and "approve it first" in out and fd.calls_to("/mpesa/b2c/v3/paymentrequest") == []
            assert run("payroll", "approve", "--db", dbfile, "--batch", "W43", "--as", "ai-assistant")[0] == 1
            assert run("payroll", "approve", "--db", dbfile, "--batch", "W43", "--as", "mike")[0] == 0
            code, out = run("payroll", "send", "--db", dbfile, "--batch", "W43")
            assert "dry run: 2 payment(s)" in out and fd.calls_to("/mpesa/b2c/v3/paymentrequest") == []
            code, out = run("payroll", "send", "--db", dbfile, "--batch", "W43", "--execute")
            assert code == 0 and "sent: 2 accepted" in out
            run("payroll", "send", "--db", dbfile, "--batch", "W43", "--execute")  # again: nothing new goes
            calls = fd.calls_to("/mpesa/b2c/v3/paymentrequest")
            assert len(calls) == 2 and calls[0]["json"]["ResultURL"] == f"https://tunnel.example.com/daraja/b2c-result/{TOK}"
            assert calls[0]["json"]["PartyA"] == "600978" and calls[0]["json"]["SecurityCredential"] == "cred"
            db = livecli.open_db(dbfile)
            svc = WebhookService(livecli.build_gateway(db, livecli.SqlLedger(db)), db, TOK)
            ok, failed = fd.b2c_result(calls[0]["json"], receipt="UJKB2C0001"), fd.b2c_result(calls[1]["json"], ok=False)
            for r in (ok, failed):
                assert svc.handle("POST", f"/daraja/b2c-result/{TOK}", json.dumps(r).encode())[0] == 200
            code, out = run("payroll", "report", "--db", dbfile, "--batch", "W43")
            assert "FAILED 1, PAID 1; paid KSh 1,500 of KSh 2,700" in out and "needs attention" in out
            assert "2547****111" in out and "0722000111" not in out
            code, out = run("payroll", "requeue", "--db", dbfile, "--batch", "W43", "--as", "mike")
            assert "1 failed line(s) queued again" in out
            run("payroll", "send", "--db", dbfile, "--batch", "W43", "--execute")
            calls = fd.calls_to("/mpesa/b2c/v3/paymentrequest")
            assert len(calls) == 3 and calls[2]["json"]["OriginatorConversationID"].endswith(":2")


def test_payroll_cannot_send_without_credentials(tmp_path):
    (tmp_path / "pay.csv").write_text("name,phone,amount\nGrace,0712345678,1500\n")
    dbfile = str(tmp_path / "app.db")
    with env(PAYRECON_APPROVERS="mike"):
        assert run("payroll", "import", "--db", dbfile, "--batch", "W43", "--file", str(tmp_path / "pay.csv"), "--as", "mike")[0] == 0
        assert run("payroll", "approve", "--db", dbfile, "--batch", "W43", "--as", "mike")[0] == 0
        code, out = run("payroll", "send", "--db", dbfile, "--batch", "W43", "--execute")
        assert code == 2 and "nothing can be sent" in out
