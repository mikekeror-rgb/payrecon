"""The dashboard: its logic (payrecon.dashboard) directly, and the Streamlit page driven through a stand-in module,
so the page's wiring is exercised without Streamlit installed."""
import importlib
import sys
import types

from payrecon.dashboard import DEMO_APPROVER, Dashboard, RecordedModel


def by(rows, key, value):
    return [r for r in rows if r[key] == value]


def test_the_demo_shows_the_live_findings_and_cannot_reach_daraja(tmp_path):
    d = Dashboard.demo(str(tmp_path))
    s = d.snapshot()
    cats = {e["subject"]: e["category"] for e in s["exceptions"]}
    assert cats["L3"] == "REVERSED_PAYMENT" and cats["L2"] == "DUPLICATE_PAYMENT" and cats["L5"] == "AMOUNT_MISMATCH"
    assert cats["L10"] == "FAILED" and cats["UJK030FG5W"] == "ORPHANED_TXN"
    assert {r["payment"] for r in s["reversed"]} == {"UJKA0AXL4X", "UJKA0AXOK2"}
    assert s["summary"]["llm_calls"] == 0 and d.offline


def test_recorded_answers_replay_the_live_model_and_nothing_else(tmp_path):
    d = Dashboard.demo(str(tmp_path))
    links = {r["payment"]: r for r in d.snapshot(RecordedModel())["links"]}
    assert links["UJK030FG5W"]["invoice"] == "L6" and links["UJK030FG5W"]["decided_by"] == "llm" and links["UJK030FG5W"]["band"] == "MEDIUM"
    assert links["UJK030FEM8"]["invoice"] == "" and links["UJK030FEM8"]["decided_by"] == "llm"
    assert links["UJK030FG5X"]["invoice"] == "L7" and links["UJK030FG5X"]["decided_by"] == "heuristic"


def test_plan_approve_and_execute_go_through_the_same_rules_as_the_cli(tmp_path):
    d = Dashboard.demo(str(tmp_path))
    assert "need approval" in d.plan(ai=True, llm=RecordedModel())
    waiting = d.snapshot()["approvals"]
    shiku = by(waiting, "invoice", "L6")[0]
    refund = by(waiting, "type", "REFUND_DUPLICATE")[0]
    assert shiku["proposed_by"] == "ai-assistant" and by(waiting, "invoice", "L7")[0]["proposed_by"] == "link-matcher"
    assert d.approve(shiku["id"], "ai-assistant").startswith("refused") and "machine" in d.approve(shiku["id"], "ai-assistant")
    assert d.approve(shiku["id"], "mallory").startswith("refused")
    assert d.approve(shiku["id"], DEMO_APPROVER, "nickname checked with the customer").startswith("approved")
    assert d.approve(refund["id"], DEMO_APPROVER).startswith("approved")
    r = d.run(execute=True)
    assert r["offline"] and r["unknown"] == 0
    acts = {a["id"]: a for a in d.snapshot()["actions"]}
    assert acts[shiku["id"]]["status"] == "DONE" and acts[shiku["id"]]["approved_by"] == DEMO_APPROVER
    assert acts[refund["id"]]["status"] != "DONE"  # offline: the refund cannot be sent
    inv = {i["invoice"]: i for i in d.snapshot()["invoices"]}
    assert inv["L6"]["books"] == "PAID" and inv["L1"]["books"] == "PAID" and inv["L3"]["books"] == "UNPAID"
    assert d.snapshot()["audit_ok"]


def test_a_reset_demo_starts_clean(tmp_path):
    d = Dashboard.demo(str(tmp_path))
    d.plan()
    assert d.snapshot()["actions"]
    d.close()
    assert Dashboard.demo(str(tmp_path)).snapshot()["actions"] == []


# ---- the Streamlit page, through a stand-in ------------------------------------------------------------------

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):  # columns: metric, button, ...
        return getattr(_ST, name)


class _Rerun(Exception):
    pass


class _FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state, self.secrets, self.clicks, self.shown = {}, {}, set(), []
        self.sidebar = self

    def _show(self, *a, **k):
        self.shown.append(" ".join(str(x) for x in a))

    set_page_config = title = caption = markdown = write = subheader = info = warning = error = success = metric = divider = _show

    def toggle(self, label, value=False, **k):
        return value

    def expander(self, *a, **k):
        return _Ctx()

    def file_uploader(self, *a, **k):
        return None

    def dataframe(self, rows, **k):
        self.shown.append(f"table:{len(rows)}")

    def radio(self, label, options, **k):
        return self.views.get(label, options[0]) if hasattr(self, "views") else options[0]

    def selectbox(self, label, options, **k):
        return options[0]

    def text_input(self, label, value="", **k):
        return value

    def checkbox(self, label, **k):
        return True

    def button(self, label, key=None, **k):
        return (key or label) in self.clicks

    def columns(self, spec):
        return [_Ctx() for _ in range(spec if isinstance(spec, int) else len(spec))]

    def tabs(self, names):
        return [_Ctx() for _ in names]

    def container(self, **k):
        return _Ctx()

    def rerun(self):
        raise _Rerun()


_ST = _FakeStreamlit()


def _page(clicks=()):
    _ST.clicks, _ST.shown = set(clicks), []
    app = importlib.import_module("payrecon.dashboard_app")
    try:
        app.main()
    except _Rerun:
        pass
    return " | ".join(_ST.shown)


def test_the_streamlit_page_renders_plans_approves_and_executes(tmp_path):
    import os

    saved = sys.modules.get("streamlit"), os.environ.pop("PAYRECON_DASHBOARD_LOCAL", None)
    sys.modules["streamlit"] = _ST
    sys.modules.pop("payrecon.dashboard_app", None)
    try:
        _drive_page(tmp_path)
    finally:
        sys.modules.pop("payrecon.dashboard_app", None)
        if saved[0] is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved[0]
        if saved[1] is not None:
            os.environ["PAYRECON_DASHBOARD_LOCAL"] = saved[1]


def _drive_page(tmp_path):
    _ST.session_state.clear()
    _ST.session_state["demo_folder"] = str(tmp_path)
    out = _page()
    # plain words on the page, and the demo arrives with its fixes planned (AI on, recorded answers)
    assert "Payment reconciliation" in out and "M-Pesa offline" in out
    assert "Money went back to the customer" in out and "Paid twice" in out and "AI suggestion" in out
    assert "Grace Wanjiku" in out and "Shiku" in out
    assert "AMOUNT_MISMATCH" not in out  # codes stay out of the main view
    d = _ST.session_state["dash"]
    waiting = d.snapshot()["approvals"]
    shiku = [a for a in waiting if a["invoice"] == "L6"][0]["id"]
    _page({f"ok{shiku}"})
    assert any(a["id"] == shiku and a["status"] == "APPROVED" for a in d.snapshot()["actions"])
    _page({"Apply fixes"})
    out = _page()  # Streamlit reruns after the click; the result shows on that run
    assert "fix(es) applied" in out and "offline" in out
    assert [i for i in d.snapshot()["invoices"] if i["invoice"] == "L6"][0]["books"] == "PAID"
    out = _page({"Reset demo"})
    acts = _ST.session_state["dash"].snapshot()["actions"]
    assert acts and all(a["status"] == "PROPOSED" for a in acts)  # back to a fresh, freshly planned demo


def test_an_applied_ai_link_counts_for_its_invoice_afterwards(tmp_path):
    """Seen in the hosted demo: after approving Shiku -> L6 and applying, L6 said PAID in the books but 'not paid yet',
    and the payment was still listed as having no invoice, because reconciliation only matched by typed reference."""
    d = Dashboard.demo(str(tmp_path))
    d.plan(ai=True, llm=RecordedModel())
    shiku = [a for a in d.snapshot()["approvals"] if a["invoice"] == "L6"][0]["id"]
    d.approve(shiku, DEMO_APPROVER)
    d.run(execute=True)
    s = d.snapshot(RecordedModel())
    l6 = [i for i in s["invoices"] if i["invoice"] == "L6"][0]
    assert (l6["books"], l6["category"], l6["payments"]) == ("PAID", "MATCHED", "UJK030FG5W")
    assert "UJK030FG5W" not in {e["subject"] for e in s["exceptions"]}
    assert "UJK030FG5W" not in {r["payment"] for r in s["links"]}
    assert "proposed 0 new" in d.plan(ai=True, llm=RecordedModel())  # nothing is proposed for it again



def test_the_payroll_page_imports_approves_sends_and_shows_who_needs_checking(tmp_path):
    import os

    saved = sys.modules.get("streamlit"), os.environ.pop("PAYRECON_DASHBOARD_LOCAL", None)
    sys.modules["streamlit"] = _ST
    sys.modules.pop("payrecon.dashboard_app", None)
    try:
        _ST.session_state.clear()
        _ST.session_state["demo_folder"] = str(tmp_path)
        _ST.views = {"View": "Payroll"}
        out = _page()
        assert "Payroll" in out and "simulated M-Pesa" in out and "Upload" in out
        _page({"Use the demo list"})
        out = _page()
        assert "Imported 8 people" in out and "Pay 8 people" in out
        _page({"Approve batch"})
        out = _page()
        assert "Approved by demo-approver" in out and "8 payment(s) ready to send" in out
        _page({"Send payments"})
        out = _page()
        assert "7 sent and accepted" in out and "1 unknown" in out
        assert "Brian Mutua" in out and "Failed: nothing was paid" in out      # the 0000 number
        assert "Aisha Hassan" in out and "Unknown: check before doing anything" in out  # the 1111 number
        p = _ST.session_state["dash"].payroll()
        batch = p.batches()[0]["batch"]
        assert p.report(batch)["counts"] == {"PAID": 6, "FAILED": 1, "UNKNOWN": 1}
        pid = [r for r in p.lines(batch) if r["name"] == "Aisha Hassan"][0]["payout_id"]
        _page({f"no{pid}"})
        out = _page({"Send the 2 failed payment(s) again"})
        assert p.report(batch)["counts"].get("QUEUED") == 2
    finally:
        _ST.views = {}
        sys.modules.pop("payrecon.dashboard_app", None)
        if saved[0] is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved[0]
        if saved[1] is not None:
            os.environ["PAYRECON_DASHBOARD_LOCAL"] = saved[1]
