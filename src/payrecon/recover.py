"""CLI for the recovery executor.

    python -m payrecon.recover demo  [--n 300] [--seed 7] [--db actions.db]
    python -m payrecon.recover queue --db actions.db
    python -m payrecon.recover audit --db actions.db [--invoice I00042] [--limit 40]

`demo` runs the whole flow against the simulated sandbox (hidden ground truth, injected faults) and checks the
outcome against what really happened. There are no live adapters yet: books, ledger and Daraja gateway are
ports (payrecon/recovery/ports.py) for you to implement; the simulator implements them in memory.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime

from .ai.pipeline import investigate
from .matcher import reconcile
from .providers.base import EAT
from .recovery import ActionStatus, ActionStore, ApprovalDesk, Executor, RecoveryConfig, Role, plan_from_data
from .recovery.chaos import HUMAN, _approve_by_oracle, check_invariants, ChaosResult
from .recovery.sim import Clock, Faults, SimBooks, SimLedger, SimNotifier, SimPush, SimRefunds, World
from .synthetic import generate


def _fmt(counter: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1])) or "none"


def cmd_demo(args: argparse.Namespace) -> int:
    ds = generate(n_invoices=args.n, seed=args.seed)
    clock = Clock(datetime(2026, 10, 19, 9, tzinfo=EAT))
    faults = Faults(seed=args.seed, status_error_rate=0.05, lose_callback_rate=0.05, false_failure_rate=0.25, drift_rate=0.3)
    w = World(ds, clock, faults)
    store = ActionStore(args.db or ":memory:", clock)
    desk = ApprovalDesk(store, {HUMAN: Role.APPROVER})
    cfg = RecoveryConfig(max_side_effects_per_run=10_000)
    ex = Executor(store, SimBooks(w), SimLedger(w), SimPush(w), SimRefunds(w), SimNotifier(w), config=cfg)

    invoices, txns = list(w.invoices.values()), list(w.ledger.values())
    rep = reconcile(invoices, txns)
    air = investigate(rep, invoices, txns)
    print(f"1. RECONCILE  {len(invoices)} invoices, {len(txns)} provider records -> {_fmt(Counter(r.category.value for r in rep.invoices.values()))}")
    print(f"   orphans {len(rep.orphans)}, AI link proposals {len(air.link_proposals)} (advice only)")

    _, pr = plan_from_data(invoices, txns, store, link_proposals=air.link_proposals, config=cfg)
    need = sum(a.requires_approval for a in pr.proposed)
    print(f"2. PLAN       {len(pr.proposed)} actions proposed ({_fmt(Counter(a.action_type.value for a in pr.proposed))})")
    print(f"              {need} need a human; {pr.new_escalations} more cases have no automatic action (mismatch, partial, ...) and are in the review queue")
    print(f"              re-planning proposes nothing new: {len(plan_from_data(invoices, txns, store, link_proposals=air.link_proposals, config=cfg)[1].proposed)} new")

    moved = w.drift([a.invoice_id for a in store.list_actions()])
    print(f"3. DRIFT      after the plan was made, the world moved for {len(moved)} invoices (e.g. {moved[0] if moved else 'none'})")

    dry = ex.run_once(dry_run=True)
    print(f"4. DRY RUN    {len(dry.would_run)} runnable actions checked against fresh data: {_fmt(Counter(v.split(':')[0] for _, _, v in dry.would_run))}; nothing changed")

    for rnd in range(1, 6):
        s = ex.run_once()
        if rnd == 1:
            print(f"5. RUN        unattended actions only: done {s.done}, cancelled by guards {s.cancelled}, deferred {s.deferred}, unknown {s.unknown}")
            queue = desk.queue()
            print(f"6. APPROVALS  {len(queue)} waiting ({_fmt(Counter(a.action_type.value for a in queue))})")
            before = len(queue)
            _approve_by_oracle(desk, store, ds)
            rej = len(store.list_actions(ActionStatus.REJECTED))
            print(f"              reviewer '{HUMAN}' approved {before - rej}, rejected {rej} (wrong links); the reviewer here is an oracle over synthetic truth")
        w.advance_provider()
        clock.advance(1800)
        invoices, txns = list(w.invoices.values()), list(w.ledger.values())
        plan_from_data(invoices, txns, store, config=cfg)
        _approve_by_oracle(desk, store, ds)
    for _ in range(3):
        ex.run_once()
        clock.advance(cfg.stale_after_s + 1)
        w.advance_provider()

    status = Counter(a.status.value for a in store.list_actions())
    print(f"7. OUTCOME    actions: {_fmt(status)}")
    print(f"              provider pushes sent {len(w.sends)}, refunds {len(w.refunds)}, invoices applied {len(w.applied)}, customer notices {len(w.notices)}")
    res = ChaosResult(args.seed)
    check_invariants(w, store, res)
    ok, bad = store.verify_audit()
    print(f"8. VERIFY     checked against ground truth: {len(res.violations)} violations (double charge, wrong apply, unapproved refund, retry overrun, missing write-ahead)")
    print(f"              audit log: {len(store.audit_events())} events, hash chain {'intact' if ok else f'BROKEN at {bad}'}, head {store.audit_head()[:16]}")
    for v in res.violations[:5]:
        print("   VIOLATION:", v)
    unk = [a for a in store.list_actions(ActionStatus.UNKNOWN)]
    if unk:
        print(f"   {len(unk)} action(s) ended UNKNOWN (a call may or may not have happened): a person must check, we never resend. e.g. action {unk[0].action_id}: {unk[0].last_error}")
    if args.db:
        print(f"\nstore written to {args.db}: try  python -m payrecon.recover audit --db {args.db} --limit 20")
    return 1 if res.violations else 0


def cmd_queue(args: argparse.Namespace) -> int:
    store = ActionStore(args.db)
    waiting = [a for a in store.list_actions(ActionStatus.PROPOSED) if a.requires_approval]
    print(f"{len(waiting)} action(s) waiting for approval")
    for a in waiting:
        print(f"  #{a.action_id:<5} {a.action_type.value:<22} {a.invoice_id:<8} by {a.proposed_by:<13} {a.payload}")
    reviews = store.open_escalations()
    print(f"{len(reviews)} review item(s) with no automatic action")
    for e in reviews[:15]:
        print(f"  [{e['escalation_id']:<4}] {e['invoice_id']:<8} {e['reason']}")
    if len(reviews) > 15:
        print(f"  ... {len(reviews) - 15} more")
    live = Counter(a.status.value for a in store.list_actions())
    print("all actions:", _fmt(live))
    if (h := store.halted()):
        print("EXECUTOR HALTED:", h)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = ActionStore(args.db)
    ok, bad = store.verify_audit()
    print(f"hash chain: {'intact' if ok else f'BROKEN at audit_id {bad}'}   head {store.audit_head()[:16]}")
    subject = None
    if args.invoice:
        ids = [a.action_id for a in store.list_actions(invoice_id=args.invoice)]
        events = [e for i in ids for e in store.audit_events(subject=f"action:{i}")]
        events.sort(key=lambda e: e["audit_id"])
    else:
        events = store.audit_events()
    for e in events[-args.limit:]:
        print(f"  {e['audit_id']:>5} {e['at'][11:19]} {e['actor']:<12} {e['event']:<34} {e['subject'] or ''}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="payrecon.recover", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="narrated end-to-end run in the simulated sandbox")
    d.add_argument("--n", type=int, default=300)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--db", help="write the action store here so you can inspect it afterwards")
    d.set_defaults(fn=cmd_demo)
    q = sub.add_parser("queue", help="actions waiting for a human")
    q.add_argument("--db", required=True)
    q.set_defaults(fn=cmd_queue)
    a = sub.add_parser("audit", help="show and verify the audit trail")
    a.add_argument("--db", required=True)
    a.add_argument("--invoice")
    a.add_argument("--limit", type=int, default=40)
    a.set_defaults(fn=cmd_audit)
    from . import livecli as L

    def db_args(p, store=True):
        p.add_argument("--db", required=True, help="books and ledger: a SQLite path or a postgres:// URL")
        if store:
            p.add_argument("--store", required=True, help="the action store (SQLite path)")

    p = sub.add_parser("init-db", help="create the books and ledger tables (SQLite)")
    p.add_argument("--db", required=True)
    p.set_defaults(fn=L.cmd_init_db)
    p = sub.add_parser("import-csv", help="load invoices and/or transactions from CSV into the books and ledger")
    p.add_argument("--db", required=True)
    p.add_argument("--invoices")
    p.add_argument("--transactions")
    p.add_argument("--reversals", help="reversal lines from the M-Pesa statement: original_receipt, reversal_receipt, amount, reversed_at")
    p.set_defaults(fn=L.cmd_import_csv)
    p = sub.add_parser("plan", help="propose recovery actions from the current books and ledger")
    db_args(p)
    p.add_argument("--ai", action="store_true", help="also turn the AI investigator's link proposals into actions (each needs approval)")
    p.add_argument("--llm", choices=["none", "anthropic"], default="none")
    p.add_argument("--model")
    p.set_defaults(fn=L.cmd_plan)
    p = sub.add_parser("investigate", help="AI investigation of the live books and ledger: explanations, failure reasons, link proposals")
    p.add_argument("--db", required=True)
    p.add_argument("--llm", choices=["none", "anthropic"], default="none", help="anthropic needs the SDK and ANTHROPIC_API_KEY")
    p.add_argument("--model")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--json")
    p.set_defaults(fn=L.cmd_investigate)
    p = sub.add_parser("run", help="run approved and unattended actions (dry run unless --execute)")
    db_args(p)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--worker", default="cli")
    p.set_defaults(fn=L.cmd_run)
    for name, fn in (("approve", L.cmd_approve), ("reject", L.cmd_reject)):
        p = sub.add_parser(name, help=f"{name} a proposed action (needs PAYRECON_APPROVERS)")
        p.add_argument("id", type=int)
        p.add_argument("--store", required=True)
        p.add_argument("--as", dest="as_user", required=True)
        p.add_argument("--note" if name == "approve" else "--reason")
        p.set_defaults(fn=fn)
    p = sub.add_parser("serve", help="run the webhook receiver for Daraja's callbacks (needs PAYRECON_WEBHOOK_TOKEN)")
    p.add_argument("--db", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--replay-every", type=float, default=60.0, help="seconds between retries of items left in ERROR; 0 turns it off")
    p.set_defaults(fn=L.cmd_serve)
    p = sub.add_parser("webhook-inbox", help="what the receiver has stored; by default only items needing attention")
    p.add_argument("--db", required=True)
    p.add_argument("--status", choices=["RECEIVED", "INGESTED", "QUARANTINED", "ERROR", "NOTED"])
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=L.cmd_webhook_inbox)
    p = sub.add_parser("webhook-replay", help="reprocess stored callbacks left in ERROR or RECEIVED")
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=L.cmd_webhook_replay)
    p = sub.add_parser("sandbox-check", help="try your own Daraja sandbox credentials and print the raw answers")
    p.add_argument("--query", metavar="CHECKOUT_ID")
    p.add_argument("--push", metavar="MSISDN")
    p.add_argument("--amount")
    p.add_argument("--reference")
    p.add_argument("--diagnose", action="store_true", help="no network: show the request we would send and check the Password")
    p.add_argument("--compare-password-prefix", metavar="TEXT", help="the first characters of Password as the portal's simulator shows it")
    p.add_argument("--db", default=":memory:", help="record the push here (use the same file as `serve`, so the receiver recognises its callback)")
    p.add_argument("--reverse", metavar="RECEIPT", help="ask Daraja to reverse this M-Pesa receipt (needs --amount and the DARAJA_INITIATOR* settings)")
    p.add_argument("--receiver-party", help="shortcode that received the payment being reversed (default: DARAJA_REVERSAL_SHORTCODE, DARAJA_C2B_SHORTCODE, DARAJA_SHORTCODE)")
    p.add_argument("--c2b-simulate", action="store_true", help="sandbox: pretend a customer paid the paybill (needs --amount, --reference)")
    p.add_argument("--msisdn", default="254708374149", help="payer for --c2b-simulate (default: the sandbox test number)")
    p.add_argument("--polls", type=int, default=6)
    p.add_argument("--interval", type=float, default=10.0)
    p.set_defaults(fn=L.cmd_sandbox_check)
    p = sub.add_parser("c2b-register", help="tell Daraja where to send C2B confirmations (derived from DARAJA_CALLBACK_URL)")
    p.add_argument("--shortcode", help="default: DARAJA_C2B_SHORTCODE, else DARAJA_SHORTCODE")
    p.add_argument("--confirmation-url")
    p.add_argument("--validation-url")
    p.add_argument("--response-type", choices=["Completed", "Cancelled"], default="Completed",
                   help="what Daraja does if the validation URL does not answer")
    p.set_defaults(fn=L.cmd_c2b_register)
    p = sub.add_parser("dashboard", help="the web dashboard on http://127.0.0.1:8501 (needs pip install -e '.[dashboard]')")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(fn=L.cmd_dashboard)
    p = sub.add_parser("live-demo", help="the full stack (SQL adapters + gateway + executor) against a local fake Daraja")
    p.add_argument("--db")
    p.set_defaults(fn=L.cmd_live_demo)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
