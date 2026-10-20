"""The executor. Runs approved or auto-safe actions, one at a time, with these rules:

1. Claim by compare-and-swap, so two workers never run the same action.
2. Re-validate against fresh data (guards.py) immediately before the side effect.
3. Commit a write-ahead marker *before* calling the provider.
4. Repeat-safe effects (books, reads) may be re-run after a crash. Non-repeatable effects (a push, a
   refund) are at-most-once: if we cannot tell whether one happened, the action becomes UNKNOWN and a
   person looks. We never resend to find out.
5. Count hard failures; past a threshold the executor halts itself until a human resumes it.
6. Cap money-moving actions per run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from ..models import InternalStatus, TxnStatus
from .config import RecoveryConfig
from .guards import Guards, GuardResult, Verdict
from .models import REPEAT_SAFE, ActionStatus, ActionType, RecoveryAction
from .ports import BooksPort, LedgerPort, NotifyPort, NotSentError, PushPort, RefundPort
from .store import ActionStore

MONEY_MOVING = {ActionType.RETRY_PUSH, ActionType.REFUND_DUPLICATE, ActionType.APPLY_PAYMENT, ActionType.APPLY_LINKED_PAYMENT}


@dataclass
class RunSummary:
    done: int = 0
    cancelled: int = 0
    deferred: int = 0
    unknown: int = 0
    failed: int = 0
    skipped_lost_race: int = 0
    halted: str | None = None
    recovered: int = 0
    would_run: list[tuple[int, str, str]] = field(default_factory=list)  # dry run: (id, type, verdict)
    notes: list[str] = field(default_factory=list)


class Executor:
    def __init__(
        self,
        store: ActionStore,
        books: BooksPort,
        ledger: LedgerPort,
        push: PushPort,
        refunds: RefundPort,
        notifier: NotifyPort,
        *,
        config: RecoveryConfig = RecoveryConfig(),
        worker: str = "worker-1",
    ):
        self.store, self.books, self.ledger, self.push, self.refunds, self.notifier = store, books, ledger, push, refunds, notifier
        self.config, self.worker = config, worker
        self.guards = Guards(store, books, ledger, push, config)

    # ---- one pass -----------------------------------------------------------------

    def run_once(self, *, dry_run: bool = False, limit: int = 100) -> RunSummary:
        s = RunSummary()
        if (reason := self.store.halted()) is not None:
            s.halted = reason
            return s
        if not dry_run:
            s.recovered = self.recover_stale()
        hard_failures = side_effects = 0
        for a in self.store.runnable(limit):
            if dry_run:
                g = self.guards.check(a)
                s.would_run.append((a.action_id, a.action_type.value, f"{g.verdict.value}{': ' + g.reason if g.reason else ''}"))
                continue
            if a.action_type in MONEY_MOVING and side_effects >= self.config.max_side_effects_per_run:
                s.notes.append(f"per-run cap of {self.config.max_side_effects_per_run} money-moving actions reached; rest deferred")
                break
            if not self.store.claim(a.action_id, self.worker):
                s.skipped_lost_race += 1
                continue
            outcome = self._run_claimed(self.store.get(a.action_id), s)
            if outcome == "effect":
                side_effects += 1
            if outcome == "hard_failure":
                hard_failures += 1
                if hard_failures >= self.config.breaker_threshold:
                    self.store.halt(f"circuit breaker: {hard_failures} hard failures in one run", actor=self.worker)
                    s.halted = self.store.halted()
                    break
            elif outcome == "effect":
                hard_failures = 0
        return s

    def _run_claimed(self, a: RecoveryAction, s: RunSummary) -> str:
        """Returns 'effect', 'hard_failure' or 'none'."""
        try:
            g = self.guards.check(a)
        except Exception as e:  # a guard crashing is a reason to wait, never to proceed
            return self._defer(a, f"guard error: {type(e).__name__}: {e}", s)
        if g.verdict is Verdict.CANCEL:
            self.store.finish(a.action_id, ActionStatus.CANCELLED, actor=self.worker, error=g.reason, event="action.cancelled")
            s.cancelled += 1
            return "none"
        if g.verdict is Verdict.LATER:
            return self._defer(a, g.reason, s)
        return self._perform(a, s)

    def _defer(self, a: RecoveryAction, reason: str, s: RunSummary) -> str:
        if a.attempt >= self.config.max_claims:
            self.store.finish(a.action_id, ActionStatus.FAILED, actor=self.worker, error=f"gave up after {a.attempt} tries: {reason}")
            s.failed += 1
            return "hard_failure"
        delay = self.config.backoff_s * (2 ** (a.attempt - 1))
        self.store.release(a.action_id, reason, self.store.now() + timedelta(seconds=delay), self.worker)
        s.deferred += 1
        return "none"

    def _perform(self, a: RecoveryAction, s: RunSummary) -> str:
        repeat_safe = a.action_type in REPEAT_SAFE
        if not self.store.begin_side_effect(a.action_id, self.worker, allow_repeat=repeat_safe):
            self.store.finish(a.action_id, ActionStatus.UNKNOWN, actor=self.worker,
                              error="side effect already started by an earlier attempt; outcome unknown")
            s.unknown += 1
            return "hard_failure"
        try:
            result = self._effect(a)
        except NotSentError as e:
            self.store.abort_side_effect(a.action_id, self.worker, str(e))
            return self._defer(a, f"provably not sent: {e}", s)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if repeat_safe:
                return self._defer(a, f"effect failed (safe to repeat): {err}", s)
            # We called out and do not know what happened. Do not resend.
            self.store.finish(a.action_id, ActionStatus.UNKNOWN, actor=self.worker, error=err, event="action.unknown")
            s.unknown += 1
            return "hard_failure"
        self.store.finish(a.action_id, ActionStatus.DONE, actor=self.worker, result=result)
        s.done += 1
        return "effect"

    # ---- effects -----------------------------------------------------------------

    def _parts(self, txn_ids: list[str], total: Decimal) -> list[tuple[str, Decimal]]:
        """Every transaction behind an apply, with its own amount from the fresh ledger. An invoice settled in
        instalments must record each one, or a later step could treat the unrecorded ones as unused."""
        parts: list[tuple[str, Decimal]] = []
        for tid in txn_ids:
            t = self.ledger.get_txn(tid)
            if t is None or t.status is not TxnStatus.SUCCESS:
                raise RuntimeError(f"transaction {tid} is no longer a confirmed success")
            parts.append((tid, t.amount))
        if sum((amt for _, amt in parts), Decimal(0)) != total:
            raise RuntimeError("the transactions no longer add up to the planned amount")
        return parts

    def _effect(self, a: RecoveryAction) -> dict:
        t = a.action_type
        if t is ActionType.APPLY_PAYMENT:
            ids = a.payload["txn_ids"]
            applied = self.books.mark_paid(a.invoice_id, self._parts(ids, Decimal(a.payload["amount"])), a.idempotency_key)
            return {"applied": applied, "txn_ids": ids, "already_applied": not applied}
        if t is ActionType.APPLY_LINKED_PAYMENT:
            tid = a.payload["txn_id"]
            applied = self.books.mark_paid(a.invoice_id, self._parts([tid], Decimal(a.payload["amount"])), a.idempotency_key)
            return {"applied": applied, "txn_id": tid, "approved_by": a.approved_by}
        if t is ActionType.RETRY_PUSH:
            rid = self.push.send_push(retry_of=a.payload["retry_of"], amount=Decimal(a.payload["amount"]), idempotency_key=a.idempotency_key)
            self.store.record_request_id(a.action_id, rid, self.worker)
            return {"new_request_id": rid}
        if t is ActionType.NOTIFY_CUSTOMER:
            self.notifier.notify(a.invoice_id, a.payload["template"], a.idempotency_key)
            return {"template": a.payload["template"]}
        if t is ActionType.REFUND_DUPLICATE:
            ref = self.refunds.reverse(a.payload["refund_txn_id"], Decimal(a.payload["amount"]), a.idempotency_key)
            return {"refund_ref": ref, "approved_by": a.approved_by}
        if t is ActionType.RECHECK_STATUS:
            answers = {}
            for rid in a.payload["request_ids"]:
                answers[rid] = self.push.get_status(rid).value
            n_success = sum(v == "SUCCESS" for v in answers.values())
            out = {"status_by_request": answers}
            if n_success:
                # The STK query returns no receipt or amount, so we cannot apply anything from it. Say so.
                self.store.escalate(a.invoice_id, "provider reports success but the ledger has no receipt for it",
                                    {"requests": [r for r, v in answers.items() if v == "SUCCESS"]}, actor=self.worker)
                out["needs_human"] = "provider reports success but the ledger has no receipt for it"
            return out
        raise AssertionError(t)

    # ---- crash recovery -----------------------------------------------------------

    def recover_stale(self) -> int:
        """Handle actions whose worker died mid-flight. Never resends anything that is not repeat-safe."""
        n = 0
        for a in self.store.stale_executing(self.config.stale_after_s):
            if a.action_type in REPEAT_SAFE:
                if a.action_type in (ActionType.APPLY_PAYMENT, ActionType.APPLY_LINKED_PAYMENT) and self.books.is_applied(a.idempotency_key):
                    self.store.finish(a.action_id, ActionStatus.DONE, actor=self.worker,
                                      result={"recovered": "books already had this key"}, event="action.recovered_done")
                else:
                    self.store.release(a.action_id, "worker lost; safe to repeat", None, self.worker)
            elif a.side_effect_at is None:
                self.store.release(a.action_id, "worker lost before the side effect", None, self.worker)
            elif a.request_id is not None:
                self.store.finish(a.action_id, ActionStatus.DONE, actor=self.worker,
                                  result={"recovered": "provider call completed", "new_request_id": a.request_id},
                                  event="action.recovered_done")
            else:
                self.store.finish(a.action_id, ActionStatus.UNKNOWN, actor=self.worker,
                                  error="worker lost after the side effect started; outcome unknown, do not resend",
                                  event="action.unknown")
            n += 1
        return n
