import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from payrecon.recovery import ActionStatus, ActionStore, ActionType


def _propose(store, key="k1", approval=False, inv="I1", kind=ActionType.NOTIFY_CUSTOMER):
    return store.propose(invoice_id=inv, action_type=kind, key=key, payload={"x": 1}, requires_approval=approval)


def test_same_key_is_one_row():
    s = ActionStore()
    a, created = _propose(s)
    b, again = _propose(s)
    assert created and not again and a.action_id == b.action_id
    assert len(s.list_actions()) == 1
    assert len(s.audit_events(event="action.proposed")) == 1  # repeats do not spam the audit log


def test_claim_is_exclusive():
    s = ActionStore()
    a, _ = _propose(s)
    assert s.claim(a.action_id, "w1") is True
    assert s.claim(a.action_id, "w2") is False
    assert s.get(a.action_id).status is ActionStatus.EXECUTING and s.get(a.action_id).claimed_by == "w1"


def test_concurrent_claims_across_connections_have_one_winner(tmp_path):
    path = str(tmp_path / "a.db")
    first = ActionStore(path)
    a, _ = _propose(first)
    stores = [ActionStore(path) for _ in range(8)]
    wins, barrier = [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        if stores[i].claim(a.action_id, f"w{i}"):
            wins.append(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


def test_approval_required_action_cannot_be_claimed_until_approved():
    s = ActionStore()
    a, _ = _propose(s, approval=True)
    assert s.claim(a.action_id, "w1") is False
    assert s.approve(a.action_id, "alice") is True
    assert s.claim(a.action_id, "w1") is True


def test_database_rejects_a_state_change_that_skips_approval():
    s = ActionStore()
    a, _ = _propose(s, approval=True)
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute("UPDATE recovery_actions SET status='DONE' WHERE action_id=?", (a.action_id,))


def test_cannot_approve_an_action_that_does_not_need_it():
    s = ActionStore()
    a, _ = _propose(s, approval=False)
    assert s.approve(a.action_id, "alice") is False


def test_audit_log_is_append_only_and_chained():
    s = ActionStore()
    a, _ = _propose(s)
    s.claim(a.action_id, "w1")
    assert s.verify_audit() == (True, None)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._conn.execute("UPDATE audit_log SET actor='mallory' WHERE audit_id=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._conn.execute("DELETE FROM audit_log")


def test_rewriting_history_is_detected_even_by_someone_who_drops_the_triggers():
    s = ActionStore()
    a, _ = _propose(s)
    s.claim(a.action_id, "w1")
    s.finish(a.action_id, ActionStatus.DONE, actor="w1", result={})
    s._conn.execute("DROP TRIGGER audit_no_update")
    s._conn.execute("UPDATE audit_log SET details='{}' WHERE audit_id=1")
    ok, bad = s.verify_audit()
    assert not ok and bad == 1


def test_state_change_and_audit_commit_together():
    s = ActionStore()
    a, _ = _propose(s)
    before = len(s.audit_events())
    s._conn.execute("DROP TABLE audit_log")  # make the audit write fail inside the transaction
    with pytest.raises(sqlite3.OperationalError):
        s.claim(a.action_id, "w1")
    assert s.get(a.action_id).status is ActionStatus.PROPOSED  # rolled back, not half-done
    assert before > 0


def test_finish_only_works_from_executing():
    s = ActionStore()
    a, _ = _propose(s)
    assert s.finish(a.action_id, ActionStatus.DONE, actor="w1") is False
    s.claim(a.action_id, "w1")
    assert s.finish(a.action_id, ActionStatus.DONE, actor="w1") is True
    assert s.finish(a.action_id, ActionStatus.FAILED, actor="w1") is False  # terminal stays terminal


def test_write_ahead_marker_is_once_only_unless_repeat_safe():
    s = ActionStore()
    a, _ = _propose(s)
    s.claim(a.action_id, "w1")
    assert s.begin_side_effect(a.action_id, "w1", allow_repeat=False) is True
    assert s.begin_side_effect(a.action_id, "w1", allow_repeat=False) is False
    assert s.begin_side_effect(a.action_id, "w1", allow_repeat=True) is True


def test_retry_count_comes_from_started_side_effects_not_from_proposals():
    s = ActionStore()
    a, _ = s.propose(invoice_id="I1", action_type=ActionType.RETRY_PUSH, key="r1", payload={}, requires_approval=False)
    b, _ = s.propose(invoice_id="I1", action_type=ActionType.RETRY_PUSH, key="r2", payload={}, requires_approval=False)
    assert s.retry_counts() == {}
    s.claim(a.action_id, "w")
    s.begin_side_effect(a.action_id, "w", allow_repeat=False)
    assert s.retry_counts() == {"I1": 1}
    s.abort_side_effect(a.action_id, "w", "never left")
    assert s.retry_counts() == {}


def test_not_before_delays_execution():
    t = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    s = ActionStore(clock=lambda: t[0])
    a, _ = _propose(s)
    s.claim(a.action_id, "w1")
    s.release(a.action_id, "later", t[0] + timedelta(minutes=5), "w1")
    assert s.runnable() == []
    t[0] += timedelta(minutes=6)
    assert [x.action_id for x in s.runnable()] == [a.action_id]


def test_halt_and_resume_are_audited():
    s = ActionStore()
    s.halt("test")
    assert s.halted() == "test"
    s.resume("alice", "checked")
    assert s.halted() is None
    assert [e["event"] for e in s.audit_events()] == ["system.halted", "system.resumed"]
