"""Payroll: one approved batch, nobody paid twice, every line ends PAID, FAILED or UNKNOWN."""
import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from payrecon.adapters import SQLITE_DDL, Database, migrate_sqlite
from payrecon.adapters.fake_daraja import FakeDaraja
from payrecon.adapters.security import PiiBox
from payrecon.payroll import PayrollError, PayrollService, SimulatedPayouts, check_csv
from payrecon.recovery.ports import NotSentError, SendOutcomeUnknown

WORKERS = """name,phone,amount
Grace Wanjiku,0712 345 678,1500
Peter Otieno,+254722000111,1200
Mary Achieng,254733444555,1500
"""
BOSS = {"mike": "APPROVER"}


def svc(port=None, now=None):
    db = Database.sqlite()
    db.script(SQLITE_DDL)
    return PayrollService(db, port, PiiBox(None, allow_plaintext=True), **({"now": now} if now else {}))


def statuses(s, batch="W43"):
    return {r["name"]: r["status"] for r in s.lines(batch)}


# ---- the file ------------------------------------------------------------------------------------------------

def test_a_good_file_is_read_with_numbers_normalised_and_the_total_added_up():
    c = check_csv(WORKERS)
    assert not c.errors and [ln.phone for ln in c.lines] == ["254712345678", "254722000111", "254733444555"]
    assert c.total == Decimal(4200)


@pytest.mark.parametrize("text, problem", [
    ("name,phone,amount\nJane,0712345678,1500\nJoe,0712345678,800\n", "same number as line 2"),
    ("name,phone,amount\nJane,0812345678,1500\n", "not a Safaricom number"),
    ("name,phone,amount\nJane,0712345678,1500.50\n", "whole number of shillings"),
    ("name,phone,amount\nJane,0712345678,-5\n", "whole number of shillings"),
    ("name,phone,amount\nJane,0712345678,abc\n", "not an amount"),
    ("name,phone,amount\nJane,0712345678,999999\n", "above the per-person limit"),
    ("name,phone,amount\n,0712345678,100\n", "no name"),
    ("worker,mobile,pay\nJane,0712345678,100\n", "needs columns"),
    ("name,phone,amount\n", "no payments"),
])
def test_every_problem_in_the_file_is_reported_and_nothing_is_stored(text, problem):
    s = svc()
    c = s.create_batch("W43", text, "mike")
    assert any(problem in e for e in c.errors), c.errors
    assert s.batches() == [] and s.lines("W43") == []


def test_common_column_names_and_formats_are_accepted():
    c = check_csv("Employee,Phone Number,Salary\nJane,0712-345-678,\"1,500\"\n")
    assert not c.errors and c.lines[0].amount == Decimal(1500)


def test_a_batch_name_is_used_once():
    s = svc()
    s.create_batch("W43", WORKERS, "mike")
    with pytest.raises(PayrollError, match="already exists"):
        s.create_batch("W43", WORKERS, "mike")


def test_phone_numbers_are_sealed_and_masked_in_storage():
    db = Database.sqlite()
    db.script(SQLITE_DDL)
    from cryptography.fernet import Fernet
    s = PayrollService(db, None, PiiBox(Fernet.generate_key().decode()))
    s.create_batch("W43", WORKERS, "mike")
    raw = db.execute("SELECT msisdn_enc, msisdn_masked FROM payouts").fetchall()
    assert all(r[0].startswith("enc1:") and "345678" not in r[0] for r in raw)
    assert raw[0][1] == "2547****678"


# ---- approval ---------------------------------------------------------------------------------------------

def test_nothing_is_sent_before_a_person_approves():
    port = SimulatedPayouts()
    s = svc(port)
    s.create_batch("W43", WORKERS, "mike")
    with pytest.raises(PayrollError, match="approve it first"):
        s.send("W43", execute=True)
    assert port.sent == [] and set(statuses(s).values()) == {"WAITING"}


@pytest.mark.parametrize("who, why", [("ai-assistant", "machine"), ("system", "machine"), ("payroll", "machine"), ("stranger", "not allowed")])
def test_machines_and_strangers_cannot_approve(who, why):
    s = svc()
    s.create_batch("W43", WORKERS, "mike")
    with pytest.raises(PayrollError, match=why):
        s.approve("W43", who, BOSS)


def test_a_batch_is_approved_once():
    s = svc()
    s.create_batch("W43", WORKERS, "mike")
    s.approve("W43", "mike", BOSS)
    assert s.batches()[0]["approved_by"] == "mike" and set(statuses(s).values()) == {"QUEUED"}
    with pytest.raises(PayrollError, match="not waiting for approval"):
        s.approve("W43", "mike", BOSS)


# ---- sending ----------------------------------------------------------------------------------------------

def _approved(port, workers=WORKERS):
    s = svc(port)
    assert not s.create_batch("W43", workers, "mike").errors
    s.approve("W43", "mike", BOSS)
    return s


def test_a_dry_run_sends_nothing_and_says_what_would_go():
    port = SimulatedPayouts()
    s = _approved(port)
    r = s.send("W43")
    assert (r["would_send"], r["total"]) == (3, 4200.0) and port.sent == []


def test_sending_twice_pays_each_person_once():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    s.send("W43", execute=True)  # the double click
    assert len(port.sent) == 3 and set(statuses(s).values()) == {"ACCEPTED"}
    port.deliver(s)
    rep = s.report("W43")
    assert rep["counts"] == {"PAID": 3} and rep["paid_total"] == Decimal(4200)
    s.send("W43", execute=True)
    assert len(port.sent) == 3


def test_accepted_is_not_paid_until_the_result_says_so():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    assert s.report("W43")["paid_total"] == 0
    port.deliver(s)
    assert {r["receipt"] for r in s.lines("W43")} == {"SIM0000001", "SIM0000002", "SIM0000003"}


class Flaky:
    """Fails in a chosen way for chosen numbers, otherwise accepts."""

    def __init__(self, not_sent=(), unknown=()):
        self.not_sent, self.unknown, self.sent = set(not_sent), set(unknown), []

    def send_b2c(self, *, originator_id, phone, amount, remarks):
        self.sent.append(phone)
        if phone in self.not_sent:
            raise NotSentError("token refused")
        if phone in self.unknown:
            raise SendOutcomeUnknown("read timeout")
        return f"AG_{originator_id}", originator_id


def test_not_sent_goes_back_to_the_queue_and_may_have_been_sent_is_never_resent():
    port = Flaky(not_sent={"254712345678"}, unknown={"254722000111"})
    s = _approved(port)
    s.send("W43", execute=True)
    assert statuses(s) == {"Grace Wanjiku": "QUEUED", "Peter Otieno": "UNKNOWN", "Mary Achieng": "ACCEPTED"}
    port.not_sent.clear()
    s.send("W43", execute=True)
    assert statuses(s)["Grace Wanjiku"] == "ACCEPTED"
    assert port.sent.count("254722000111") == 1  # the unknown one was not tried again
    assert port.sent.count("254712345678") == 2  # the not-sent one was, once more


def test_a_run_stops_when_m_pesa_keeps_refusing():
    port = Flaky(not_sent={"254712345678", "254722000111", "254733444555"})
    s = _approved(port)
    r = s.send("W43", execute=True, max_not_sent=2)
    assert r["not_sent"] == 2 and len(port.sent) == 2 and "stopped" in r["notes"][0]


def test_a_sender_that_stopped_mid_request_leaves_the_line_unknown_not_resent():
    clock = [datetime(2026, 10, 20, 22, tzinfo=timezone.utc)]
    port = SimulatedPayouts()
    s = svc(port, now=lambda: clock[0])
    s.create_batch("W43", WORKERS, "mike")
    s.approve("W43", "mike", BOSS)
    s.db.execute("UPDATE payouts SET status = 'SENDING', updated_at = ? WHERE line_no = 2", (clock[0],))  # the crash
    clock[0] += timedelta(minutes=11)
    s.send("W43", execute=True)
    assert statuses(s)["Grace Wanjiku"] == "UNKNOWN" and len(port.sent) == 2
    assert "254712345678" not in {p["phone"] for p in port.sent}


def test_a_late_result_settles_an_unknown_line():
    port = Flaky(unknown={"254722000111"})
    s = _approved(port)
    s.send("W43", execute=True)
    pid = [r for r in s.lines("W43") if r["name"] == "Peter Otieno"][0]["payout_id"]
    late = {"Result": {"ResultCode": 0, "ResultDesc": "ok", "OriginatorConversationID": f"{pid}:1", "ConversationID": "AG_x",
                       "TransactionID": "UJKLATE001"}}
    assert s.ingest_result(late) == "PAID" and statuses(s)["Peter Otieno"] == "PAID"


def test_failures_are_recorded_and_only_a_person_sends_them_again():
    port = SimulatedPayouts()
    s = _approved(port, "name,phone,amount\nGrace,0712340000,500\nPeter,0722000222,600\n")
    s.send("W43", execute=True)
    port.deliver(s)
    assert statuses(s) == {"Grace": "FAILED", "Peter": "PAID"}
    s.send("W43", execute=True)
    assert len(port.sent) == 2  # nothing resent by itself
    with pytest.raises(PayrollError, match="machine"):
        s.requeue_failed("W43", "ai-assistant", BOSS)
    assert s.requeue_failed("W43", "mike", BOSS) == 1
    s.send("W43", execute=True)
    assert [p["originator"] for p in port.sent][-1].endswith(":2")  # a new attempt number, never the same request
    assert len(port.sent) == 3


def test_sending_failures_again_never_touches_a_line_that_may_have_been_paid():
    port = Flaky(unknown={"254722000111"})
    s = _approved(port, WORKERS + "Brian,0712340000,500\n")
    s.send("W43", execute=True)
    s.ingest_result({"Result": {"ResultCode": 2040, "ResultDesc": "no", "OriginatorConversationID": "W43:5:1",
                                "ConversationID": "AG_W43:5:1"}})
    assert statuses(s)["Peter Otieno"] == "UNKNOWN" and statuses(s)["Brian"] == "FAILED"
    assert s.requeue_failed("W43", "mike", BOSS) == 1
    assert statuses(s)["Peter Otieno"] == "UNKNOWN"  # never put back in the queue: it may have gone
    s.send("W43", execute=True)
    assert port.sent.count("254722000111") == 1


def test_a_person_settles_an_unknown_line_from_the_statement():
    port = Flaky(unknown={"254722000111"})
    s = _approved(port)
    s.send("W43", execute=True)
    pid = [r for r in s.lines("W43") if r["name"] == "Peter Otieno"][0]["payout_id"]
    with pytest.raises(PayrollError, match="receipt"):
        s.resolve(pid, "mike", BOSS, paid=True)
    s.resolve(pid, "mike", BOSS, paid=True, receipt="UJKSTMT01")
    row = [r for r in s.lines("W43") if r["name"] == "Peter Otieno"][0]
    assert (row["status"], row["receipt"]) == ("PAID", "UJKSTMT01") and "mike" in row["detail"]
    with pytest.raises(PayrollError, match="not UNKNOWN"):
        s.resolve(pid, "mike", BOSS, paid=False)


def test_a_redelivered_result_changes_nothing_and_a_stray_one_matches_nothing():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    first = port.pending[0]
    port.deliver(s)
    assert s.ingest_result(first) == "PAID" and s.report("W43")["counts"] == {"PAID": 3}
    stray = {"Result": {"ResultCode": 0, "OriginatorConversationID": "nope", "ConversationID": "nope"}}
    assert s.ingest_result(stray) is None
    with pytest.raises(ValueError, match="malformed"):
        s.ingest_result({"nothing": 1})


def test_a_timeout_makes_an_unsettled_line_unknown_but_never_undoes_a_payment():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    ids = [r["payout_id"] for r in s.lines("W43")]
    assert s.note_timeout({"Result": {"OriginatorConversationID": f"{ids[0]}:1"}}) == "UNKNOWN"
    port.deliver(s)  # the result arrives after all
    assert statuses(s)["Grace Wanjiku"] == "PAID"
    assert s.note_timeout({"Result": {"OriginatorConversationID": f"{ids[0]}:1"}}) == "PAID"


def test_cancelling_stops_only_what_has_not_been_sent():
    port = Flaky(unknown={"254722000111"})
    s = _approved(port, WORKERS)
    s.send("W43", execute=True, limit=2)
    assert s.cancel("W43", "mike", BOSS) == 1
    assert statuses(s) == {"Grace Wanjiku": "ACCEPTED", "Peter Otieno": "UNKNOWN", "Mary Achieng": "CANCELLED"}


def test_a_paid_amount_that_differs_from_the_line_is_flagged():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    port.pending[0]["Result"]["ResultParameters"]["ResultParameter"][0]["Value"] = 15000
    port.deliver(s)
    assert "check with M-Pesa" in s.lines("W43")[0]["detail"]


# ---- the whole road: gateway, fake Daraja, receiver -------------------------------------------------------

def test_the_gateway_sends_what_daraja_documents_and_the_receiver_settles_it():
    from payrecon.adapters import SqlLedger
    from payrecon.adapters.gateway import DarajaGateway
    from payrecon.providers.daraja import DarajaProvider
    from payrecon.webhook import WebhookService

    tok = "T" * 32
    with FakeDaraja() as fd:
        db = Database.sqlite()
        db.script(SQLITE_DDL)
        gw = DarajaGateway(DarajaProvider("key", "secret", "174379", "passkey", base_url=fd.url), db, SqlLedger(db),
                           callback_url="https://example.com/cb", pii=PiiBox(None, allow_plaintext=True), initiator="testapi",
                           security_credential="CRED==", b2c_shortcode="600986", b2c_result_url="https://h/daraja/b2c-result/t",
                           b2c_timeout_url="https://h/daraja/b2c-timeout/t", read_timeout=3.0)
        s = PayrollService(db, gw, gw.pii)
        s.create_batch("W43", WORKERS, "mike")
        s.approve("W43", "mike", BOSS)
        r = s.send("W43", execute=True)
        assert r["accepted"] == 3 and len(fd.b2c_payments) == 3
        b = fd.b2c_payments[0]
        assert (b["PartyA"], b["PartyB"], b["Amount"], b["CommandID"], b["SecurityCredential"]) == ("600986", "254712345678", 1500, "BusinessPayment", "CRED==")
        assert b["ResultURL"].endswith("/b2c-result/t") and b["OriginatorConversationID"].startswith("W43:2:")
        svc_ = WebhookService(gw, db, tok)
        for body in fd.b2c_payments:
            assert svc_.handle("POST", f"/daraja/b2c-result/{tok}", json.dumps(fd.b2c_result(body)).encode())[0] == 200
        assert s.report("W43")["counts"] == {"PAID": 3}
        stored = " ".join(r[0] for r in db.execute("SELECT payload FROM webhook_inbox").fetchall())
        assert "Jane Worker" not in stored and "254712345678" not in stored  # the payee's name and number are stripped
        svc_.handle("POST", f"/daraja/b2c-result/{tok}", json.dumps({"Result": {"ResultCode": 0, "OriginatorConversationID": "x"}}).encode())
        assert db.execute("SELECT status, detail FROM webhook_inbox ORDER BY received_at DESC LIMIT 1").fetchone()[1] == "matches no payout"


def test_b2c_without_its_settings_is_refused_before_anything_is_sent():
    from payrecon.adapters import SqlLedger
    from payrecon.adapters.gateway import DarajaGateway
    from payrecon.providers.daraja import DarajaProvider

    with FakeDaraja() as fd:
        db = Database.sqlite()
        db.script(SQLITE_DDL)
        gw = DarajaGateway(DarajaProvider("key", "secret", "174379", "passkey", base_url=fd.url), db, SqlLedger(db),
                           callback_url="https://example.com/cb", pii=PiiBox(None, allow_plaintext=True))
        with pytest.raises(NotSentError, match="not configured"):
            gw.send_b2c(originator_id="x", phone="254712345678", amount=Decimal(10), remarks="r")
        assert fd.b2c_payments == []


def test_an_old_database_gains_the_payout_results_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database.sqlite(path)
    old = SQLITE_DDL.replace(", 'b2c-result', 'b2c-timeout'", "")
    db.script(old)
    db.execute("INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h','stk','2026-10-20','{}','INGESTED')")
    assert migrate_sqlite(db) == ["webhook_inbox: payout (B2C) results may now be received"]
    assert db.execute("SELECT COUNT(*) FROM webhook_inbox").fetchone()[0] == 1
    db.execute("INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h2','b2c-result','2026-10-20','{}','RECEIVED')")


# ---- chaos: faults everywhere, still nobody is paid twice ------------------------------------------------------

class ChaosMpesa:
    """A hostile M-Pesa: refuses, times out after taking the money, loses results, redelivers them, delivers late.
    Keeps the truth: how many times each person was actually paid."""

    def __init__(self, rng):
        self.rng, self.paid, self.inbox, self.requests = rng, {}, [], 0

    def send_b2c(self, *, originator_id, phone, amount, remarks):
        self.requests += 1
        roll = self.rng.random()
        if roll < 0.15:
            raise NotSentError("refused before sending")
        self.paid[phone] = self.paid.get(phone, 0) + 1  # from here on, the money moved
        result = {"Result": {"ResultCode": 0, "ResultDesc": "ok", "OriginatorConversationID": originator_id,
                             "ConversationID": f"AG_{self.requests}", "TransactionID": f"R{self.requests:06d}"}}
        if self.rng.random() < 0.7:
            self.inbox.append(result)
            if self.rng.random() < 0.2:
                self.inbox.append(result)  # redelivered
        if roll < 0.3:
            raise SendOutcomeUnknown("read timeout after the request went out")
        return f"AG_{self.requests}", originator_id


@pytest.mark.parametrize("seed", range(8))
def test_chaos_nobody_is_ever_paid_twice_and_nothing_is_left_unaccounted(seed):
    rng = random.Random(seed)
    port = ChaosMpesa(rng)
    rows = "\n".join(f"Worker {i},07{10000000 + i * 7919:08d},{rng.randint(1, 30) * 100}" for i in range(30))
    s = _approved(port, "name,phone,amount\n" + rows + "\n")
    for _ in range(6):  # several runs: the double clicks, the reruns after errors
        s.send("W43", execute=True, max_not_sent=50)
        rng.shuffle(port.inbox)
        keep, port.inbox = port.inbox[: len(port.inbox) // 2], port.inbox[len(port.inbox) // 2:]
        for r in keep:
            s.ingest_result(r)
    for r in port.inbox:  # late results
        s.ingest_result(r)
    assert max(port.paid.values()) == 1, "someone was paid twice"
    phone_of = {pid: s.pii.open(enc) for pid, enc in s.db.execute("SELECT payout_id, msisdn_enc FROM payouts").fetchall()}
    for r in s.lines("W43"):
        moved = port.paid.get(phone_of[r["payout_id"]], 0) == 1
        if r["status"] == "QUEUED":
            assert not moved, f"{r['name']} was paid but is queued to be paid again"
        if r["status"] == "PAID":
            assert moved and r["receipt"], f"{r['name']} is marked PAID without M-Pesa paying them"
        if moved:
            assert r["status"] in ("PAID", "UNKNOWN", "ACCEPTED"), f"{r['name']} was paid but shows {r['status']}"
    assert sum(port.paid.values()) >= 20  # the runs made real progress despite the faults


def test_two_senders_at_once_still_pay_each_person_once(tmp_path):
    """Two tabs, or the dashboard and the CLI, pressing Send at the same moment."""
    import threading
    import time

    class Slow(SimulatedPayouts):
        def send_b2c(self, **kw):
            time.sleep(0.02)
            return super().send_b2c(**kw)

    path = str(tmp_path / "pay.db")
    db = Database.sqlite(path)
    db.script(SQLITE_DDL)
    port = Slow()
    first = PayrollService(db, port, PiiBox(None, allow_plaintext=True))
    rows = "\n".join(f"W{i},07{20000000 + i * 31:08d},100" for i in range(20))
    first.create_batch("W43", "name,phone,amount\n" + rows + "\n", "mike")
    first.approve("W43", "mike", BOSS)
    services = [PayrollService(Database.sqlite(path), port, PiiBox(None, allow_plaintext=True)) for _ in range(4)]
    threads = [threading.Thread(target=x.send, args=("W43",), kwargs={"execute": True}) for x in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    phones = [p["phone"] for p in port.sent]
    assert len(phones) == 20 and len(set(phones)) == 20


def test_a_failure_result_after_a_confirmed_payment_does_not_undo_it():
    port = SimulatedPayouts()
    s = _approved(port)
    s.send("W43", execute=True)
    ok = port.pending[0]
    port.deliver(s)
    bad = json.loads(json.dumps(ok))
    bad["Result"]["ResultCode"] = 2001
    assert s.ingest_result(bad) == "PAID" and statuses(s)["Grace Wanjiku"] == "PAID"
