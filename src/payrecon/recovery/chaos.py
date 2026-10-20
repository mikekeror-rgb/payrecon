"""Chaos harness: run the executor against a hostile simulated world and check invariants against ground truth.

    python -m payrecon.recovery.chaos --n 600 --seeds 1 2 3

Exit code 1 if any invariant is violated by the real executor. A naive executor (no idempotency, no guards,
no approvals) is run on the same world as a negative control: if it does not violate the invariants, the
invariants are not testing anything.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..ai.pipeline import investigate
from ..matcher import reconcile
from ..policy import MAX_RETRIES
from ..providers.base import EAT
from ..synthetic import generate
from .approvals import ApprovalDesk, Role
from .config import RecoveryConfig
from .executor import Executor
from .models import ActionStatus, ActionType
from .planner import plan_from_data
from .sim import Clock, Faults, SimBooks, SimLedger, SimNotifier, SimPush, SimRefunds, SimulatedCrash, World
from .store import ActionStore

HUMAN = "alice"


@dataclass
class ChaosResult:
    seed: int
    violations: list[str] = field(default_factory=list)
    status: Counter = field(default_factory=Counter)
    by_type: Counter = field(default_factory=Counter)
    sends: int = 0
    refunds: int = 0
    applied: int = 0
    crashes: int = 0
    unknown: int = 0
    audit_ok: bool = True
    drift: int = 0


def _approve_by_oracle(desk: ApprovalDesk, store: ActionStore, ds) -> None:
    """A careful human: approves a linked payment only if it really belongs to that invoice, and any refund of a real duplicate."""
    for a in desk.queue():
        if a.action_type is ActionType.APPLY_LINKED_PAYMENT:
            ok = ds.link_truth.get(a.payload["txn_id"]) == a.invoice_id
        else:
            ok = True
        if ok:
            desk.approve(a.action_id, HUMAN, "oracle")
        else:
            desk.reject(a.action_id, HUMAN, "does not belong to this invoice")


def _fresh_inputs(w: World):
    return list(w.invoices.values()), list(w.ledger.values())


def check_invariants(w: World, store: ActionStore, res: ChaosResult) -> None:
    v = res.violations
    v.extend(w.violations)
    v.extend(f"key {k} reached the provider {n} times" for k, n in w.send_calls.items() if n > 1)
    v.extend(f"refund key {k} reached the provider {n} times" for k, n in w.refund_calls.items() if n > 1)
    per_inv = Counter(s["invoice"] for s in w.sends)
    v.extend(f"{i}: {n} pushes sent (limit {MAX_RETRIES})" for i, n in per_inv.items() if n > MAX_RETRIES)
    for a in store.list_actions():
        if a.requires_approval and a.side_effect_at is not None and a.approved_by != HUMAN:
            v.append(f"action {a.action_id} ({a.action_type.value}) ran without a valid approval")
    for key in list(w.send_calls) + list(w.refund_calls) + list(w.applied):
        a = store.get_by_key(key)
        if a is None or a.side_effect_at is None:
            v.append(f"side effect for key {key} has no write-ahead record")
    ok, bad = store.verify_audit()
    res.audit_ok = ok
    if not ok:
        v.append(f"audit chain broken at {bad}")


def run_chaos(seed: int, *, n: int = 600, faults: Faults | None = None, rounds: int = 8, config: RecoveryConfig | None = None) -> ChaosResult:
    faults = faults or Faults(seed=seed)
    cfg = config or RecoveryConfig(max_side_effects_per_run=10_000, breaker_threshold=10_000)
    ds = generate(n_invoices=n, seed=seed)
    clock = Clock(datetime(2026, 10, 19, 9, tzinfo=EAT))
    w = World(ds, clock, faults)
    store = ActionStore(":memory:", clock)
    desk = ApprovalDesk(store, {HUMAN: Role.APPROVER})
    res = ChaosResult(seed)
    worker_no = 0

    def new_executor() -> Executor:
        nonlocal worker_no
        worker_no += 1
        return Executor(store, SimBooks(w), SimLedger(w), SimPush(w), SimRefunds(w), SimNotifier(w), config=cfg, worker=f"w{worker_no}")

    ex = new_executor()
    for r in range(rounds):
        invoices, txns = _fresh_inputs(w)
        rep = reconcile(invoices, txns)
        air = investigate(rep, invoices, txns)
        _, pr = plan_from_data(invoices, txns, store, link_proposals=air.link_proposals, config=cfg)
        _approve_by_oracle(desk, store, ds)
        if r == 0:  # the plan is now a snapshot; let the world move before anything runs
            res.drift = len(w.drift([a.invoice_id for a in store.list_actions()]))
        try:
            ex.run_once()
        except SimulatedCrash:
            res.crashes += 1
            ex = new_executor()  # the process died; a new one starts later
            clock.advance(cfg.stale_after_s + 1)
        w.advance_provider()
        clock.advance(1800)
    # drain: a few more passes so deferred work and crash recovery settle
    for _ in range(4):
        try:
            ex.run_once()
        except SimulatedCrash:
            res.crashes += 1
            ex = new_executor()
        clock.advance(cfg.stale_after_s + 1)
        w.advance_provider()

    res.status = Counter(a.status.value for a in store.list_actions())
    res.by_type = Counter(a.action_type.value for a in store.list_actions() if a.side_effect_at is not None)
    res.sends, res.refunds, res.applied = len(w.sends), len(w.refunds), len(w.applied)
    res.unknown = res.status.get("UNKNOWN", 0)
    check_invariants(w, store, res)
    return res


def run_naive(seed: int, *, n: int = 600, faults: Faults | None = None, rounds: int = 8) -> ChaosResult:
    """Negative control: a stateless job that re-plans every round and does whatever the plan says."""
    faults = faults or Faults(seed=seed)
    ds = generate(n_invoices=n, seed=seed)
    clock = Clock(datetime(2026, 10, 19, 9, tzinfo=EAT))
    w = World(ds, clock, faults)
    books, ledger, push, refunds = SimBooks(w), SimLedger(w), SimPush(w), SimRefunds(w)
    res = ChaosResult(seed)
    for r in range(rounds):
        invoices, txns = _fresh_inputs(w)
        rep = reconcile(invoices, txns)  # no retry counts remembered either
        air = investigate(rep, invoices, txns)
        store = ActionStore(":memory:", clock)  # no memory between runs: no idempotency
        _, pr = plan_from_data(invoices, txns, store, link_proposals=air.link_proposals)
        if r == 0:
            res.drift = len(w.drift([a.invoice_id for a in store.list_actions()]))
        for a in store.list_actions():
            try:
                if a.action_type is ActionType.APPLY_PAYMENT:
                    parts = [(tid, w.ledger[tid].amount) for tid in a.payload["txn_ids"]]
                    books.mark_paid(a.invoice_id, parts, a.idempotency_key + f":{r}")
                elif a.action_type is ActionType.APPLY_LINKED_PAYMENT:
                    books.mark_paid(a.invoice_id, [(a.payload["txn_id"], w.ledger[a.payload["txn_id"]].amount)], a.idempotency_key + f":{r}")
                elif a.action_type is ActionType.RETRY_PUSH:
                    push.send_push(retry_of=a.payload["retry_of"], amount=Decimal(a.payload["amount"]), idempotency_key=a.idempotency_key + f":{r}")
                elif a.action_type is ActionType.REFUND_DUPLICATE:
                    refunds.reverse(a.payload["refund_txn_id"], Decimal(a.payload["amount"]), a.idempotency_key + f":{r}")
            except (Exception, SimulatedCrash):
                pass  # a cron job just starts again next round
        w.advance_provider()
        clock.advance(1800)
    res.violations.extend(w.violations)
    res.sends, res.refunds, res.applied = len(w.sends), len(w.refunds), len(w.applied)
    return res


PROFILES = {
    "hostile": dict(status_error_rate=0.15, send_error_before_rate=0.05, send_error_after_rate=0.05, lose_callback_rate=0.10,
                    false_failure_rate=0.30, crash_rate=0.01, drift_rate=0.4),
    "crashy": dict(crash_rate=0.05, drift_rate=0.3, false_failure_rate=0.3, lose_callback_rate=0.1),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chaos-test the recovery executor against ground truth.")
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--profile", choices=sorted(PROFILES), default="hostile")
    args = ap.parse_args(argv)
    print(f"{'seed':>4} | {'executor':<8} | {'sends':>5} {'refunds':>7} {'applied':>7} {'crashes':>7} {'unknown':>7} | violations")
    bad = 0
    for seed in args.seeds:
        mk = lambda: Faults(seed=seed, **PROFILES[args.profile])  # noqa: E731
        real = run_chaos(seed, n=args.n, faults=mk())
        naive = run_naive(seed, n=args.n, faults=mk())
        for name, r in (("guarded", real), ("naive", naive)):
            print(f"{seed:>4} | {name:<8} | {r.sends:>5} {r.refunds:>7} {r.applied:>7} {r.crashes:>7} {r.unknown:>7} | {len(r.violations)}")
        bad += len(real.violations)
        for line in real.violations[:5]:
            print("      VIOLATION:", line)
        if not naive.violations:
            print("      WARNING: the naive control produced no violations; the chaos world is too tame to prove anything")
            bad += 1
    print("\nsafety: guarded executor violations =", bad, "(must be 0; the naive control must be > 0)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
