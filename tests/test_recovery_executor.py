import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from payrecon.models import InternalStatus, Invoice, ProviderTxn, TxnStatus
from payrecon.providers.base import EAT
from payrecon.recovery import (
    ActionStatus, ActionStore, ActionType, ApprovalDesk, ApprovalError, Executor, RecoveryConfig, Role, plan_from_data,
)
from payrecon.recovery.sim import Clock, Faults, SimBooks, SimLedger, SimNotifier, SimPush, SimRefunds, SimulatedCrash, World
from payrecon.synthetic import Dataset

T0 = datetime(2026, 10, 19, 9, tzinfo=EAT)
CFG = RecoveryConfig(retry_cooldown_s=600, backoff_s=60)


def inv(n, amount=1000, status=InternalStatus.UNPAID):
    return Invoice(f"I{n}", f"INV-{n:04d}", Decimal(amount), status, T0 - timedelta(days=1))


def txn(tid, ref, status, amount=1000, code=None, request_id=None, minutes=0):
    return ProviderTxn(tid, Decimal(amount), status, T0 - timedelta(hours=1) + timedelta(minutes=minutes), ref, None, code, "t", request_id=request_id)


class Rig:
    def __init__(self, invoices, txns, faults=None, config=CFG, db=":memory:"):
        ds = Dataset(invoices=invoices, txns=txns, truth={}, scenario={}, unlinkable_txn_ids=set())
        self.clock = Clock(T0)
        self.w = World(ds, self.clock, faults or Faults(seed=1))
        self.store = ActionStore(db, self.clock)
        self.config = config
        self.desk = ApprovalDesk(self.store, {"alice": Role.APPROVER, "bob": Role.VIEWER})
        self.ex = self.executor()

    def executor(self, worker="w1"):
        w = self.w
        return Executor(self.store, SimBooks(w), SimLedger(w), SimPush(w), SimRefunds(w), SimNotifier(w), config=self.config, worker=worker)

    def plan(self, **kw):
        return plan_from_data(list(self.w.invoices.values()), list(self.w.ledger.values()), self.store, config=self.config, **kw)[1]

    def actions(self, kind):
        return self.store.list_actions(action_type=kind)

    def wait(self, seconds):
        self.clock.advance(seconds)
        self.w.advance_provider()


# ---- applying payments --------------------------------------------------------------

def test_confirmed_exact_payment_is_applied_once_without_a_human():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    s = r.ex.run_once()
    assert s.done == 1 and r.w.invoices["I1"].internal_status is InternalStatus.PAID
    r.plan()
    r.ex.run_once()  # nothing left to do
    assert len(r.w.applied) == 1 and len(r.actions(ActionType.APPLY_PAYMENT)) == 1


def test_planning_twice_proposes_once():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    first, second = r.plan(), r.plan()
    assert len(first.proposed) == 1 and len(second.proposed) == 0 and second.existing == 1


def test_pending_push_that_succeeded_is_applied_after_folding():
    pending = txn("ws_CO_1", "INV-0001", TxnStatus.PENDING)
    ok = txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0", request_id="ws_CO_1")
    r = Rig([inv(1)], [pending, ok])
    r.plan()
    assert r.ex.run_once().done == 1 and r.w.invoices["I1"].internal_status is InternalStatus.PAID


def test_plan_goes_stale_when_someone_else_marks_the_invoice_paid():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    from dataclasses import replace
    r.w.invoices["I1"] = replace(r.w.invoices["I1"], internal_status=InternalStatus.PAID)
    s = r.ex.run_once()
    assert s.cancelled == 1 and r.w.applied == {}
    assert "already paid" in r.actions(ActionType.APPLY_PAYMENT)[0].last_error


def test_payment_already_used_for_another_invoice_is_not_applied_again():
    r = Rig([inv(1), inv(2)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    r.w.paid_txn["R1"] = "I2"  # applied elsewhere after the plan
    assert r.ex.run_once().cancelled == 1 and r.w.applied == {}


def test_unattended_apply_ceiling_routes_big_payments_to_a_human():
    cfg = RecoveryConfig(max_auto_apply_amount=Decimal("500"))
    r = Rig([inv(1, 1000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")], config=cfg)
    a = r.plan().proposed[0]
    assert a.requires_approval and r.ex.run_once().done == 0
    r.desk.approve(a.action_id, "alice")
    assert r.ex.run_once().done == 1


# ---- retries ------------------------------------------------------------------------

def failed_rig(code="1037", **kw):
    return Rig([inv(1)], [txn("F1", "INV-0001", TxnStatus.FAILED, code=code)], **kw)


def test_transient_failure_gets_exactly_one_retry_push():
    r = failed_rig()
    r.w.scripted_outcomes = ["PENDING"]
    r.plan()
    assert r.ex.run_once().done == 1 and len(r.w.sends) == 1
    r.plan()
    r.ex.run_once()  # the new push is pending: no second push
    assert len(r.w.sends) == 1


def test_customer_action_failures_are_never_retried_only_notified():
    r = failed_rig(code="1")  # insufficient funds
    r.plan()
    r.ex.run_once()
    assert r.w.sends == [] and r.w.notices == [("I1", "payment_failed_1")]


def test_no_retry_when_the_provider_says_the_failed_request_actually_succeeded():
    r = failed_rig()
    r.plan()
    r.w.request_truth["F1"] = "SUCCESS"  # the timeout lied
    s = r.ex.run_once()
    assert s.cancelled == 1 and r.w.sends == []
    assert "never re-charge" in r.actions(ActionType.RETRY_PUSH)[0].last_error


def test_no_retry_when_an_older_failed_attempt_actually_succeeded():
    older = txn("F0", "INV-0001", TxnStatus.FAILED, code="1037", minutes=-30)
    newer = txn("F1", "INV-0001", TxnStatus.FAILED, code="1037", minutes=0)
    r = Rig([inv(1)], [older, newer])
    r.plan()
    r.w.request_truth["F0"] = "SUCCESS"
    assert r.ex.run_once().cancelled == 1 and r.w.sends == []


def test_unverifiable_status_means_wait_never_send():
    r = failed_rig(faults=Faults(seed=1, status_error_rate=1.0))
    r.plan()
    s = r.ex.run_once()
    assert s.deferred == 1 and r.w.sends == []
    assert r.actions(ActionType.RETRY_PUSH)[0].status is ActionStatus.PROPOSED  # still queued, not failed
    r.w.faults.status_error_rate = 0.0
    r.w.scripted_outcomes = ["PENDING"]
    r.wait(3600)
    assert r.ex.run_once().done == 1 and len(r.w.sends) == 1


def test_gives_up_after_max_claims_without_ever_sending():
    cfg = RecoveryConfig(max_claims=3, backoff_s=1, retry_cooldown_s=0)
    r = failed_rig(faults=Faults(seed=1, status_error_rate=1.0), config=cfg)
    r.plan()
    for _ in range(6):
        r.ex.run_once()
        r.clock.advance(10)
    a = r.actions(ActionType.RETRY_PUSH)[0]
    assert a.status is ActionStatus.FAILED and r.w.sends == []


def test_retry_limit_stops_the_loop_at_three_pushes():
    r = failed_rig(config=RecoveryConfig(retry_cooldown_s=60))
    for _ in range(8):
        r.w.scripted_outcomes = ["FAILED"]
        r.plan()
        r.ex.run_once()
        r.wait(3600)
    assert len(r.w.sends) == 3
    esc = r.plan().escalations
    assert any("ESCALATE" in e.reason or "FAILED" in e.reason for e in esc)


def test_cooldown_blocks_a_second_push_until_it_passes():
    r = failed_rig(config=RecoveryConfig(retry_cooldown_s=3600, backoff_s=1))
    r.w.scripted_outcomes = ["FAILED"]
    r.plan()
    r.ex.run_once()
    r.clock.advance(120)
    r.w.advance_provider()  # the new push fails; a retry is planned again
    r.plan()
    r.clock.advance(5)
    s = r.ex.run_once()
    assert len(r.w.sends) == 1 and s.deferred == 1
    r.clock.advance(3700)
    r.w.scripted_outcomes = ["PENDING"]
    r.ex.run_once()
    assert len(r.w.sends) == 2


def test_big_retry_needs_approval():
    cfg = RecoveryConfig(auto_retry_max_amount=Decimal("500"))
    r = Rig([inv(1, 1000)], [txn("F1", "INV-0001", TxnStatus.FAILED, code="1037")], config=cfg)
    a = r.plan().proposed[0]
    assert a.requires_approval
    r.ex.run_once()
    assert r.w.sends == []
    r.desk.approve(a.action_id, "alice")
    r.w.scripted_outcomes = ["PENDING"]
    r.ex.run_once()
    assert len(r.w.sends) == 1


def test_customer_pays_another_way_after_the_plan_so_the_retry_is_cancelled():
    r = failed_rig()
    r.plan()
    r.w.ledger["C2B1"] = txn("C2B1", "INV-0001", TxnStatus.SUCCESS, code="0")
    r.w.charges.append(__import__("payrecon.recovery.sim", fromlist=["Charge"]).Charge("I1", Decimal(1000), "C2B1", "customer"))
    assert r.ex.run_once().cancelled == 1 and r.w.sends == []


def test_provably_not_sent_is_retried_without_using_a_retry():
    r = failed_rig(faults=Faults(seed=1, send_error_before_rate=1.0), config=RecoveryConfig(retry_cooldown_s=0, backoff_s=1))
    r.plan()
    s = r.ex.run_once()
    assert s.deferred == 1 and r.store.send_count("I1") == 0 and r.w.sends == []


def test_ambiguous_failure_after_send_is_unknown_and_never_resent():
    r = failed_rig(faults=Faults(seed=1, send_error_after_rate=1.0))
    r.plan()
    s = r.ex.run_once()
    assert s.unknown == 1 and len(r.w.sends) == 1
    for _ in range(5):
        r.plan()
        r.wait(3600)
        r.ex.run_once()
    assert r.w.send_calls == {r.actions(ActionType.RETRY_PUSH)[0].idempotency_key: 1} or len(r.w.sends) <= 2
    assert all(c == 1 for c in r.w.send_calls.values())


# ---- approvals and refunds ----------------------------------------------------------

def dup_rig():
    return Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0", minutes=0), txn("R2", "INV-0001", TxnStatus.SUCCESS, code="0", minutes=5)],
               )


def test_duplicate_refund_waits_for_a_human_then_refunds_the_later_payment_once():
    r = dup_rig()
    a = [x for x in r.plan().proposed if x.action_type is ActionType.REFUND_DUPLICATE][0]
    assert a.requires_approval and a.payload["refund_txn_id"] == "R2" and a.payload["keep_txn_id"] == "R1"
    r.ex.run_once()
    assert r.w.refunds == []
    r.desk.approve(a.action_id, "alice")
    r.ex.run_once()
    r.plan()
    r.ex.run_once()
    assert [x["txn"] for x in r.w.refunds] == ["R2"] and not r.w.violations


def test_refund_is_cancelled_if_the_duplicate_stops_being_one():
    r = dup_rig()
    a = r.plan().proposed[0]
    r.desk.approve(a.action_id, "alice")
    del r.w.ledger["R1"]  # the keeper vanished from the ledger: now only one payment
    assert r.ex.run_once().cancelled == 1 and r.w.refunds == []


def test_machines_non_approvers_and_proposers_cannot_approve():
    r = dup_rig()
    a = r.plan().proposed[0]
    for who, why in (("system", "machine"), ("ai-assistant", "machine"), ("bob", "approver role"), ("nobody", "approver role")):
        with pytest.raises(ApprovalError, match=why):
            r.desk.approve(a.action_id, who)
    assert r.store.get(a.action_id).status is ActionStatus.PROPOSED
    own = ApprovalDesk(r.store, {"alice": Role.APPROVER, "system": Role.APPROVER})
    with pytest.raises(ApprovalError):
        own.approve(a.action_id, "system")


def test_an_old_approval_expires():
    r = dup_rig()
    a = r.plan().proposed[0]
    r.desk.approve(a.action_id, "alice")
    r.clock.advance(r.config.approval_max_age_s + 60)
    assert r.ex.run_once().cancelled == 1 and r.w.refunds == []
    assert "expired" in r.store.get(a.action_id).last_error


def test_rejected_actions_never_run():
    r = dup_rig()
    a = r.plan().proposed[0]
    r.desk.reject(a.action_id, "alice", "customer asked to keep credit")
    r.ex.run_once()
    assert r.w.refunds == [] and r.store.get(a.action_id).status is ActionStatus.REJECTED


class _Link:  # shaped like ai.linker.LinkProposal
    def __init__(self, txn_id, invoice_id):
        self.txn_id, self.invoice_id, self.confidence, self.band, self.decided_by, self.rationale = txn_id, invoice_id, 0.7, "MEDIUM", "llm", "name matches"


def test_inferred_link_needs_approval_and_is_rechecked_before_applying():
    orphan = txn("R9", "typo-ref", TxnStatus.SUCCESS, code="0")
    r = Rig([inv(1)], [orphan])
    a = r.plan(link_proposals=[_Link("R9", "I1")]).proposed[0]
    assert a.action_type is ActionType.APPLY_LINKED_PAYMENT and a.requires_approval and a.proposed_by == "ai-assistant"
    r.ex.run_once()
    assert r.w.applied == {}
    r.desk.approve(a.action_id, "alice")
    r.w.paid_txn["R9"] = "I7"  # someone applied it elsewhere in the meantime
    assert r.ex.run_once().cancelled == 1 and r.w.applied == {}


def test_inferred_link_is_dropped_if_its_reference_now_points_to_another_invoice():
    orphan = txn("R9", "INV-0002", TxnStatus.SUCCESS, code="0")
    r = Rig([inv(1), inv(2, status=InternalStatus.PAID)], [orphan])
    r.plan(link_proposals=[_Link("R9", "I1")])
    a = r.actions(ActionType.APPLY_LINKED_PAYMENT)[0]
    r.desk.approve(a.action_id, "alice")
    assert r.ex.run_once().cancelled == 1 and r.w.applied == {}


# ---- crashes, breaker, caps, dry run, concurrency -------------------------------------

def test_crash_after_push_leaves_unknown_and_never_resends():
    r = failed_rig(faults=Faults(seed=1, crash_rate=1.0))
    r.w.faults.crash_rate = 0.0
    r.plan()
    orig = r.w.crash_point
    r.w.crash_point = lambda name: (_ for _ in ()).throw(SimulatedCrash(name)) if name == "after_send" else None
    with pytest.raises(SimulatedCrash):
        r.ex.run_once()
    r.w.crash_point = orig
    r.clock.advance(CFG.stale_after_s + 1)
    fresh = r.executor("w2")
    s = fresh.run_once()
    a = r.actions(ActionType.RETRY_PUSH)[0]
    assert s.recovered == 1 and a.status is ActionStatus.UNKNOWN and len(r.w.sends) == 1
    for _ in range(4):
        r.plan(); r.wait(3600); fresh.run_once()
    assert r.w.send_calls[a.idempotency_key] == 1


def test_crash_before_the_push_is_also_unknown_by_design():
    r = failed_rig()
    r.plan()
    r.w.crash_point = lambda name: (_ for _ in ()).throw(SimulatedCrash(name)) if name == "before_send" else None
    with pytest.raises(SimulatedCrash):
        r.ex.run_once()
    r.clock.advance(CFG.stale_after_s + 1)
    r.executor("w2").run_once()
    assert r.actions(ActionType.RETRY_PUSH)[0].status is ActionStatus.UNKNOWN and r.w.sends == []


def test_crash_after_books_were_updated_finishes_without_applying_twice():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    r.w.crash_point = lambda name: (_ for _ in ()).throw(SimulatedCrash(name)) if name == "after_apply" else None
    with pytest.raises(SimulatedCrash):
        r.ex.run_once()
    assert len(r.w.applied) == 1
    r.w.crash_point = lambda name: None
    r.clock.advance(CFG.stale_after_s + 1)
    s = r.executor("w2").run_once()
    a = r.actions(ActionType.APPLY_PAYMENT)[0]
    assert s.recovered == 1 and a.status is ActionStatus.DONE and len(r.w.applied) == 1 and not r.w.violations


def test_crash_before_books_were_updated_is_safely_repeated():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    r.w.crash_point = lambda name: (_ for _ in ()).throw(SimulatedCrash(name)) if name == "before_apply" else None
    with pytest.raises(SimulatedCrash):
        r.ex.run_once()
    assert r.w.applied == {}
    r.w.crash_point = lambda name: None
    r.clock.advance(CFG.stale_after_s + 1)
    ex2 = r.executor("w2")
    ex2.run_once()
    ex2.run_once()
    assert len(r.w.applied) == 1 and r.actions(ActionType.APPLY_PAYMENT)[0].status is ActionStatus.DONE


def test_even_if_a_started_push_is_wrongly_put_back_in_the_queue_it_is_not_sent_again():
    """Defence in depth: a buggy recovery path must still not be able to resend a non-repeatable effect."""
    r = failed_rig()
    a = r.plan().proposed[0]
    r.store.claim(a.action_id, "w0")
    r.store.begin_side_effect(a.action_id, "w0", allow_repeat=False)  # the push may have gone out
    r.store.release(a.action_id, "buggy recovery put it back", None, "w0")
    r.clock.advance(3600)  # past the cooldown, so the guards allow it and only the write-ahead marker can stop it
    s = r.ex.run_once()
    assert s.unknown == 1 and r.w.sends == [] and r.store.get(a.action_id).status is ActionStatus.UNKNOWN


def test_worker_that_died_before_any_side_effect_is_released():
    r = failed_rig()
    a = r.plan().proposed[0]
    r.store.claim(a.action_id, "dead-worker")
    r.clock.advance(CFG.stale_after_s + 1)
    assert r.ex.recover_stale() == 1 and r.store.get(a.action_id).status is ActionStatus.PROPOSED


def test_circuit_breaker_halts_after_repeated_hard_failures_until_a_human_resumes():
    invs = [inv(i) for i in range(1, 6)]
    txns = [txn(f"F{i}", f"INV-{i:04d}", TxnStatus.FAILED, code="1037") for i in range(1, 6)]
    r = Rig(invs, txns, faults=Faults(seed=1, send_error_after_rate=1.0), config=RecoveryConfig(breaker_threshold=3, retry_cooldown_s=0))
    r.plan()
    s = r.ex.run_once()
    assert s.unknown == 3 and s.halted and len(r.w.sends) == 3
    assert r.ex.run_once().halted and len(r.w.sends) == 3
    r.store.resume("alice", "gateway fixed")
    r.w.faults.send_error_after_rate = 0.0
    r.w.scripted_outcomes = ["PENDING", "PENDING"]
    assert r.ex.run_once().done == 2


def test_per_run_cap_on_money_moving_actions():
    invs = [inv(i) for i in range(1, 8)]
    txns = [txn(f"R{i}", f"INV-{i:04d}", TxnStatus.SUCCESS, code="0") for i in range(1, 8)]
    r = Rig(invs, txns, config=RecoveryConfig(max_side_effects_per_run=3))
    r.plan()
    s = r.ex.run_once()
    assert s.done == 3 and s.notes
    r.ex.run_once()
    assert len(r.w.applied) == 6


def test_dry_run_changes_nothing():
    r = Rig([inv(1), inv(2)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0"), txn("F2", "INV-0002", TxnStatus.FAILED, code="1037")])
    r.plan()
    before, events = [a.status for a in r.store.list_actions()], len(r.store.audit_events())
    s = r.ex.run_once(dry_run=True)
    assert len(s.would_run) == 2 and r.w.applied == {} and r.w.sends == []
    assert [a.status for a in r.store.list_actions()] == before and len(r.store.audit_events()) == events


def test_two_workers_racing_run_each_action_once(tmp_path):
    db = str(tmp_path / "x.db")
    invs = [inv(i) for i in range(1, 13)]
    txns = [txn(f"F{i}", f"INV-{i:04d}", TxnStatus.FAILED, code="1037") for i in range(1, 13)]
    r = Rig(invs, txns, db=db, config=RecoveryConfig(retry_cooldown_s=0))
    r.w.scripted_outcomes = ["PENDING"] * 20
    r.plan()
    other_store = ActionStore(db, r.clock)
    w = r.w
    exes = [r.ex, Executor(other_store, SimBooks(w), SimLedger(w), SimPush(w), SimRefunds(w), SimNotifier(w), config=r.config, worker="w2")]
    barrier = threading.Barrier(2)

    def go(e):
        barrier.wait()
        e.run_once()

    ts = [threading.Thread(target=go, args=(e,)) for e in exes]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(w.sends) == 12 and all(c == 1 for c in w.send_calls.values()) and not w.violations


def test_everything_that_happened_is_in_the_audit_log_in_order():
    r = Rig([inv(1)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, code="0")])
    r.plan()
    r.ex.run_once()
    a = r.actions(ActionType.APPLY_PAYMENT)[0]
    events = [e["event"] for e in r.store.audit_events(subject=f"action:{a.action_id}")]
    assert events == ["action.proposed", "action.claimed", "action.side_effect_started", "action.done"]
    assert r.store.verify_audit() == (True, None)


def test_chaos_harness_guarded_is_clean_and_naive_control_is_not():
    from payrecon.recovery.chaos import PROFILES, run_chaos, run_naive
    for seed in (1, 2):
        mk = lambda: Faults(seed=seed, **PROFILES["hostile"])  # noqa: E731
        assert run_chaos(seed, n=200, faults=mk()).violations == []
        assert len(run_naive(seed, n=200, faults=mk()).violations) > 50


# ---- each guard on its own: cases only that guard can stop -----------------------------

def test_retry_guard_enforces_the_limit_even_for_a_hand_made_action():
    r = failed_rig()
    for i in range(3):  # three pushes already went out for this invoice
        a, _ = r.store.propose(invoice_id="I1", action_type=ActionType.RETRY_PUSH, key=f"old{i}", payload={}, requires_approval=False)
        r.store.claim(a.action_id, "w")
        r.store.begin_side_effect(a.action_id, "w", allow_repeat=False)
        r.store.finish(a.action_id, ActionStatus.DONE, actor="w", result={})
    extra, _ = r.store.propose(invoice_id="I1", action_type=ActionType.RETRY_PUSH, key="fourth",
                               payload={"retry_of": "F1", "amount": "1000", "result_code": "1037"}, requires_approval=False)
    r.clock.advance(7200)
    assert r.ex.run_once().cancelled == 1 and r.w.sends == []
    assert "retry limit" in r.store.get(extra.action_id).last_error


def test_customer_cancelling_after_the_plan_stops_the_retry():
    from dataclasses import replace
    r = failed_rig()
    r.plan()
    r.w.ledger["F1"] = replace(r.w.ledger["F1"], result_code="1032")  # the customer cancelled the prompt
    assert r.ex.run_once().cancelled == 1 and r.w.sends == []
    assert "engine now says" in r.actions(ActionType.RETRY_PUSH)[0].last_error


def test_a_newer_attempt_makes_the_retry_plan_stale():
    r = failed_rig()
    r.plan()
    r.w.ledger["F2"] = txn("F2", "INV-0001", TxnStatus.FAILED, code="1037", minutes=30)
    r.w.request_truth["F2"] = "FAILED"
    assert r.ex.run_once().cancelled == 1 and r.w.sends == []
    assert "newer attempt" in r.actions(ActionType.RETRY_PUSH)[0].last_error


def test_refund_is_cancelled_when_the_picture_is_no_longer_a_clean_duplicate():
    r = dup_rig()
    a = r.plan().proposed[0]
    r.desk.approve(a.action_id, "alice")
    r.w.ledger["R3"] = txn("R3", "INV-0001", TxnStatus.SUCCESS, amount=300, code="0", minutes=9)  # now an overpayment mix
    assert r.ex.run_once().cancelled == 1 and r.w.refunds == []
    assert "not a duplicate" in r.store.get(a.action_id).last_error


def test_a_person_cannot_approve_what_they_proposed_themselves():
    r = dup_rig()
    a, _ = r.store.propose(invoice_id="I1", action_type=ActionType.REFUND_DUPLICATE, key="manual", requires_approval=True,
                           payload={"refund_txn_id": "R2", "keep_txn_id": "R1", "amount": "1000"}, proposed_by="alice")
    with pytest.raises(ApprovalError, match="proposer"):
        r.desk.approve(a.action_id, "alice")


# ---- review queue for cases with no automatic action -------------------------------------

def test_mismatch_opens_one_review_item_no_matter_how_often_we_plan():
    r = Rig([inv(1, 1000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, amount=800, code="0")])
    first, second = r.plan(), r.plan()
    assert first.new_escalations == 1 and second.new_escalations == 0
    items = r.store.open_escalations()
    assert len(items) == 1 and "AMOUNT_MISMATCH" in items[0]["reason"]
    assert len(r.store.audit_events(event="escalation.opened")) == 1


def test_only_an_approver_can_close_a_review_item():
    r = Rig([inv(1, 1000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, amount=800, code="0")])
    r.plan()
    item = r.store.open_escalations()[0]["escalation_id"]
    for who in ("system", "ai-assistant", "bob", "nobody"):
        with pytest.raises(ApprovalError):
            r.desk.resolve(item, who, "x")
    r.desk.resolve(item, "alice", "asked customer for the balance")
    assert r.store.open_escalations() == []
    with pytest.raises(ApprovalError):
        r.desk.resolve(item, "alice", "again")


def test_provider_success_without_a_receipt_goes_to_the_review_queue():
    pending = txn("ws_CO_1", "INV-0001", TxnStatus.PENDING)
    r = Rig([inv(1)], [pending])
    r.w.request_truth["ws_CO_1"], r.w.request_revealed["ws_CO_1"] = "SUCCESS", True  # succeeded; callback never arrived
    r.plan()
    r.ex.run_once()
    items = r.store.open_escalations()
    assert len(items) == 1 and "no receipt" in items[0]["reason"] and r.w.applied == {}


# ---- an invoice settled in instalments records every payment ----------------------------------

def test_instalments_that_sum_to_the_invoice_are_all_recorded_as_used():
    r = Rig([inv(1, 5000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, amount=2500, code="0"),
                             txn("R2", "INV-0001", TxnStatus.SUCCESS, amount=2500, code="0", minutes=3)])
    r.plan()
    assert r.ex.run_once().done == 1
    assert r.w.paid_txn == {"R1": "I1", "R2": "I1"} and not r.w.violations
    assert next(iter(r.w.applied.values()))[2] == Decimal(5000)


def test_apply_is_deferred_if_a_part_stops_being_a_confirmed_success():
    r = Rig([inv(1, 5000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, amount=2500, code="0"),
                             txn("R2", "INV-0001", TxnStatus.SUCCESS, amount=2500, code="0", minutes=3)])
    r.plan()
    ex = r.ex
    orig = ex.ledger.get_txn
    ex.ledger.get_txn = lambda tid: None if tid == "R2" else orig(tid)  # vanishes between guard and effect
    assert ex.run_once().deferred == 1 and r.w.applied == {}


# ---- the last check before money is applied (guards already re-ran the engine; this is the final defence) ----

def test_parts_come_from_the_fresh_ledger_and_must_add_up_to_the_planned_amount():
    r = Rig([inv(1, 1000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, 400, code="0"), txn("R2", "INV-0001", TxnStatus.SUCCESS, 600, code="0", minutes=5)])
    assert r.ex._parts(["R1", "R2"], Decimal(1000)) == [("R1", Decimal(400)), ("R2", Decimal(600))]
    with pytest.raises(RuntimeError, match="no longer add up"):
        r.ex._parts(["R1", "R2"], Decimal(1100))
    with pytest.raises(RuntimeError, match="no longer add up"):
        r.ex._parts(["R1"], Decimal(1000))  # an instalment missing from the plan


def test_a_transaction_that_stopped_being_a_confirmed_success_is_never_applied():
    from dataclasses import replace
    r = Rig([inv(1, 1000)], [txn("R1", "INV-0001", TxnStatus.SUCCESS, 1000, code="0")])
    r.w.ledger["R1"] = replace(r.w.ledger["R1"], status=TxnStatus.PENDING)  # same amount, so only the status check can catch it
    with pytest.raises(RuntimeError, match="no longer a confirmed success"):
        r.ex._parts(["R1"], Decimal(1000))
    with pytest.raises(RuntimeError, match="no longer a confirmed success"):
        r.ex._parts(["GONE"], Decimal(1000))


def test_a_link_found_by_the_code_is_not_labelled_as_the_models():
    """Seen live: a heuristic name match was listed in the approval queue as 'by ai-assistant'."""
    orphan = txn("R9", "for peter", TxnStatus.SUCCESS, code="0")
    r = Rig([inv(1)], [orphan])
    lp = _Link("R9", "I1")
    lp.decided_by = "heuristic"
    a = r.plan(link_proposals=[lp]).proposed[0]
    assert a.proposed_by == "link-matcher" and a.requires_approval
