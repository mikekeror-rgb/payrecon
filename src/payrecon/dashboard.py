"""The logic behind the dashboard, with no web framework in it (the Streamlit page in dashboard_app.py is a thin
layer over this). Everything goes through the same pieces as the CLI: reconciliation, the AI investigator, the
planner, the approval desk and the executor with its guards. Nothing here can do what the CLI cannot.

Two modes:
* your data: your app.db (books, ledger, webhook inbox) and actions.db (the action store); Daraja credentials from
  the environment are used for retries, refunds and status checks, exactly as `run --execute` does.
* demo: a copy of the live sandbox payments seen on 19-20 Oct 2026 (phone numbers masked) in a fresh folder, with
  the gateway forced offline: confirmed payments can be applied, but nothing can reach Daraja. Safe to host.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters import SQLITE_DDL, Database, SqlBooks, SqlLedger, SqlOutboxNotifier, migrate_sqlite
from .ai import investigate
from .ai.llm import LLMClient
from .matcher import reconcile
from .models import Action, Category
from .providers import CsvStatementSource, load_invoices_csv, load_reversals_csv
from .recovery.approvals import ApprovalDesk, ApprovalError, Role
from .recovery.config import RecoveryConfig
from .recovery.executor import Executor
from .recovery.models import ActionStatus
from .recovery.planner import plan_from_data
from .recovery.store import ActionStore

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
DEMO_APPROVER = "demo-approver"


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "value") and not isinstance(v, (str, int, float)):
        return v.value
    return v


def _row(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(v) for k, v in d.items()}


class RecordedModel:
    """Replays the answers Claude (claude-haiku-5-5) gave on these exact payments in a live run on 20 Oct 2026, so the
    hosted demo can show the model's part without an API key. It is labelled as a recording wherever it is used, and
    any case it has no recording for fails (falling back to the code's own answer, as a real model outage would)."""

    name = "recorded"
    LABEL = "recorded answers from a live Claude run (claude-haiku-5-5, 20 Oct 2026)"
    ANSWERS = {
        "Shiku": {"choice": "L6", "confidence": 0.7, "rationale": 'Payer reference "Shiku" is a common Kikuyu nickname for '
                  "Wanjiku, matching candidate Grace Wanjiku (L6). Amounts and timing are identical across all candidates, "
                  "so only the name evidence separates them"},
        "school fees": {"choice": "NONE", "confidence": 0.0, "rationale": 'The payer reference "school fees" contains no '
                        "name, initials, or invoice number. All three candidates have the same amount (50), the same timing "
                        "and the same status. (recorded; shortened)"},
    }

    def complete_json(self, *, system: str, user: str, tool_name: str, tool_description: str, schema: dict[str, Any]) -> dict[str, Any]:
        import json

        ref = (json.loads(user).get("payment") or {}).get("payer_reference")
        if tool_name != "record_link_decision" or ref not in self.ANSWERS:
            raise RuntimeError(f"no recorded answer for this case ({tool_name}, {ref!r})")
        return dict(self.ANSWERS[ref])


class Dashboard:
    def __init__(self, db_path: str, store_path: str, *, approvers: dict[str, Role] | None = None, offline: bool = False):
        self.db_path, self.store_path, self.offline = db_path, store_path, offline
        self.approvers = approvers if approvers is not None else {
            n.strip(): Role.APPROVER for n in os.environ.get("PAYRECON_APPROVERS", "").split(",") if n.strip()}
        self.db = Database.sqlite(db_path)
        self.db.script(SQLITE_DDL)
        migrate_sqlite(self.db)
        self.books, self.ledger = SqlBooks(self.db), SqlLedger(self.db)
        self.store = ActionStore(store_path)

    # ---- demo -------------------------------------------------------------------------------------------

    @classmethod
    def demo(cls, folder: str | None = None) -> "Dashboard":
        """A fresh demo in `folder` (a new temporary folder if None). Offline: nothing can reach Daraja."""
        folder = folder or tempfile.mkdtemp(prefix="payrecon-demo-")
        Path(folder).mkdir(parents=True, exist_ok=True)
        for name in ("app.db", "actions.db"):
            p = Path(folder) / name
            if p.exists():
                p.unlink()
        d = cls(str(Path(folder) / "app.db"), str(Path(folder) / "actions.db"),
                approvers={DEMO_APPROVER: Role.APPROVER}, offline=True)
        for inv in load_invoices_csv(EXAMPLES / "demo_invoices.csv"):
            d.books.add_invoice(inv)
        d.ledger.ingest_many(CsvStatementSource(EXAMPLES / "demo_transactions.csv", source="sandbox_capture").load())
        d.ledger.ingest_many(load_reversals_csv(EXAMPLES / "demo_reversals.csv"))
        return d

    # ---- reading ----------------------------------------------------------------------------------------

    def snapshot(self, llm: LLMClient | None = None, max_llm_calls: int = 20) -> dict[str, Any]:
        invoices, txns = self.books.invoices(), self.ledger.all_txns()
        report = reconcile(invoices, txns, self.store.retry_counts())
        air = investigate(report, invoices, txns, llm, max_llm_calls)
        cats = Counter(r.category.value for r in report.invoices.values())
        problems = [i for i in air.investigations]
        waiting = [a for a in self.store.list_actions(ActionStatus.PROPOSED) if a.requires_approval]
        inv_by_id = {i.invoice_id: i for i in invoices}
        txn_by_id = {t.provider_txn_id: t for t in txns}
        return {
            "summary": {
                "invoices": len(invoices),
                "payments": len({t.provider_txn_id for t in txns}),
                "problems": len(problems),
                "at_stake": float(sum((i.exposure for i in problems), Decimal(0))),
                "waiting_approval": len(waiting),
                "unlinked": len(report.orphans),
                "llm_calls": air.llm_calls,
                "categories": dict(cats),
            },
            "invoices": [
                {"invoice": i.invoice_id, "reference": i.reference, "customer": i.customer_name or "", "amount": float(i.amount), "books": i.internal_status.value,
                 "category": report.invoices[i.invoice_id].category.value, "action": report.invoices[i.invoice_id].action.value,
                 "payments": ", ".join(report.invoices[i.invoice_id].txn_ids)}
                for i in invoices
            ],
            "exceptions": [
                {"subject": i.subject, "kind": i.kind, "category": i.category.value, "action": i.recommended_action.value,
                 "at_stake": float(i.exposure), "explanation": i.explanation,
                 "reason": i.reason.value if i.reason else "", "reason_source": i.reason_source}
                for i in problems
            ],
            "links": [
                {"payment": p.txn_id, "invoice": p.invoice_id or "", "band": p.band, "confidence": round(p.confidence, 2),
                 "decided_by": p.decided_by, "rationale": p.rationale}
                for p in air.link_proposals
            ],
            "approvals": [
                {"id": a.action_id, "type": a.action_type.value, "invoice": a.invoice_id, "proposed_by": a.proposed_by,
                 "details": ", ".join(f"{k}={v}" for k, v in a.payload.items() if k not in ("rationale",)),
                 "rationale": a.payload.get("rationale", ""), "headline": self._headline(a, inv_by_id, txn_by_id)}
                for a in waiting
            ],
            "reviews": [_row(e) for e in self.store.open_escalations()],
            "actions": [
                {"id": a.action_id, "type": a.action_type.value, "invoice": a.invoice_id, "status": a.status.value,
                 "proposed_by": a.proposed_by, "approved_by": a.approved_by or "", "result": str(a.result or a.last_error or "")[:160]}
                for a in self.store.list_actions()
            ],
            "inbox": [
                {"received": str(r[0])[:19], "kind": r[1], "status": r[2], "tries": r[3], "detail": r[4] or ""}
                for r in self.db.execute(
                    "SELECT received_at, kind, status, attempts, detail FROM webhook_inbox ORDER BY received_at DESC LIMIT 50").fetchall()
            ],
            "refunds": [
                {"payment": r[0], "amount": float(Decimal(str(r[1]))), "status": r[2], "code": r[3] or "", "desc": r[4] or "", "at": str(r[5])[:19]}
                for r in self.db.execute(
                    "SELECT provider_txn_id, amount, status, result_code, result_desc, created_at FROM refund_requests ORDER BY created_at DESC").fetchall()
            ],
            "reversed": [
                {"payment": t.provider_txn_id, "amount": float(t.amount), "reference": t.reference_raw or "", "how": t.result_desc or "", "source": t.source}
                for t in txns if t.status.value == "REVERSED"
            ],
            "audit_ok": self.store.verify_audit()[0],
            "audit": [_row(e) for e in self.store.audit_events()[-40:]][::-1],
            "halted": self.store.halted(),
            "warnings": air.warnings,
        }

    @staticmethod
    def _headline(a, inv_by_id, txn_by_id) -> str:
        """One plain sentence for a decision, for people who do not read action codes."""
        inv = inv_by_id.get(a.invoice_id)
        who = f"{inv.customer_name}'s invoice {inv.reference}" if inv and inv.customer_name else f"invoice {a.invoice_id}"
        p = a.payload
        kind = a.action_type.value
        if kind == "APPLY_LINKED_PAYMENT":
            t = txn_by_id.get(p.get("txn_id", ""))
            typed = f' (the payer typed "{t.reference_raw}")' if t and t.reference_raw else ""
            return f"Count payment {p.get('txn_id')} of KSh {float(p.get('amount', 0)):,.0f}{typed} towards {who}?"
        if kind == "REFUND_DUPLICATE":
            return (f"{who} was paid twice. Refund the extra KSh {float(p.get('amount', 0)):,.0f} "
                    f"({p.get('refund_txn_id')}) and keep {p.get('keep_txn_id')}?")
        if kind == "RETRY_PUSH":
            return f"Send the customer a new payment prompt for {who}?"
        return f"{kind.replace('_', ' ').capitalize()} for {who}?"

    # ---- acting -----------------------------------------------------------------------------------------

    def plan(self, ai: bool = True, llm: LLMClient | None = None) -> str:
        invoices, txns = self.books.invoices(), self.ledger.all_txns()
        kw: dict[str, Any] = {}
        if ai:
            kw["link_proposals"] = investigate(reconcile(invoices, txns), invoices, txns, llm).link_proposals
        _, pr = plan_from_data(invoices, txns, self.store, **kw)
        kinds = Counter(a.action_type.value for a in pr.proposed)
        return (f"proposed {len(pr.proposed)} new action(s) {dict(kinds) if kinds else ''}; "
                f"{sum(a.requires_approval for a in pr.proposed)} need approval; already known {pr.existing}; "
                f"review items opened {pr.new_escalations}").replace("  ", " ")

    def approve(self, action_id: int, user: str, note: str = "") -> str:
        try:
            a = ApprovalDesk(self.store, self.approvers).approve(action_id, user, note)
        except (ApprovalError, KeyError) as e:
            return f"refused: {e}"
        return f"approved #{a.action_id} ({a.action_type.value} on {a.invoice_id}) as {user}"

    def reject(self, action_id: int, user: str, reason: str = "rejected in the dashboard") -> str:
        try:
            a = ApprovalDesk(self.store, self.approvers).reject(action_id, user, reason or "rejected in the dashboard")
        except (ApprovalError, KeyError) as e:
            return f"refused: {e}"
        return f"rejected #{a.action_id} as {user}"

    def run(self, execute: bool = False, worker: str = "dashboard") -> dict[str, Any]:
        from .livecli import OfflineGateway, build_gateway

        gw = OfflineGateway() if self.offline else build_gateway(self.db, self.ledger)
        ex = Executor(self.store, self.books, self.ledger, gw, gw, SqlOutboxNotifier(self.db), config=RecoveryConfig(), worker=worker)
        s = ex.run_once(dry_run=not execute)
        out = asdict(s)
        out["offline"] = isinstance(gw, OfflineGateway)
        return out

    def close(self) -> None:
        self.db.close()
        self.store.close()


def reset_demo(folder: str) -> Dashboard:
    shutil.rmtree(folder, ignore_errors=True)
    return Dashboard.demo(folder)
