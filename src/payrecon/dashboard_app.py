"""payrecon dashboard (Streamlit). A thin page over payrecon.dashboard: every button calls the same planner,
approval desk and executor as the CLI, so the safety rules are the same.

    python -m payrecon.recover dashboard            # local, your own app.db / actions.db (and the demo)
    streamlit run streamlit_app.py                  # what Streamlit Community Cloud runs: demo only

One flow, top to bottom: what is wrong, what needs your decision, apply the fixes. Your own data is offered only
when PAYRECON_DASHBOARD_LOCAL=1 (the `dashboard` command sets it and binds to 127.0.0.1). A hosted copy shows the
demo: real sandbox payments from 19-20 Oct 2026, phone numbers masked, with M-Pesa offline so nothing can move money.
"""
from __future__ import annotations

import html
import os
import tempfile

import streamlit as st

from payrecon.dashboard import DEMO_APPROVER, Dashboard, RecordedModel, reset_demo

LOCAL = os.environ.get("PAYRECON_DASHBOARD_LOCAL") == "1"

# What each finding means, in plain words, and how loud it should look.
LABELS = {
    "UNRECORDED_PAYMENT": ("Paid, not yet in your books", "green"),
    "DUPLICATE_PAYMENT": ("Paid twice", "red"),
    "AMOUNT_MISMATCH": ("Wrong amount", "amber"),
    "PARTIAL_PAYMENT": ("Partly paid", "amber"),
    "PENDING": ("Waiting on M-Pesa", "blue"),
    "FAILED": ("Payment failed", "grey"),
    "REVERSED_PAYMENT": ("Money went back to the customer", "red"),
    "ORPHANED_TXN": ("Payment with no invoice", "purple"),
    "MISSING_PAYMENT": ("Not paid yet", "grey"),
    "MATCHED": ("Paid", "green"),
}
NEXT = {
    "AUTO_APPLY": "Will be marked paid when you apply fixes",
    "ESCALATE_HUMAN": "Needs a person",
    "RECHECK_STATUS": "M-Pesa will be asked again",
    "NOTIFY_CUSTOMER": "The customer will be told",
    "LINK_OR_ESCALATE": "Find which invoice it belongs to",
    "RETRY_ELIGIBLE": "Can be retried safely",
    "AWAIT_PAYMENT": "Waiting for the customer",
    "NONE": "",
}
COLORS = {"green": "#15803d", "red": "#b91c1c", "amber": "#b45309", "blue": "#1d4ed8", "grey": "#4b5563", "purple": "#7e22ce"}

CSS = """
<style>
.block-container {padding-top: 2rem; max-width: 1100px;}
.pr-hero {padding: 1.4rem 1.6rem; border-radius: 14px; background: linear-gradient(135deg, #0f766e 0%, #134e4a 100%); color: #fff; margin-bottom: 1.2rem;}
.pr-hero h1 {color: #fff; font-size: 1.9rem; margin: 0 0 .3rem 0; padding: 0;}
.pr-hero p {color: #d1fae5; margin: 0; font-size: 1rem;}
.pr-pill {display: inline-block; margin-top: .7rem; padding: .2rem .7rem; border-radius: 999px; background: rgba(255,255,255,.15); font-size: .85rem; color: #fff;}
.pr-stats {display: flex; gap: .8rem; margin: .2rem 0 1.4rem 0; flex-wrap: wrap;}
.pr-stat {flex: 1; min-width: 150px; background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: .9rem 1.1rem;}
.pr-stat .n {font-size: 1.7rem; font-weight: 700; color: #111827;}
.pr-stat .l {font-size: .85rem; color: #6b7280;}
.pr-card {background: #fff; border: 1px solid #e5e7eb; border-left: 5px solid var(--c); border-radius: 10px; padding: .8rem 1rem; margin-bottom: .6rem;}
.pr-card .top {display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;}
.pr-badge {font-size: .78rem; font-weight: 600; color: var(--c); background: color-mix(in srgb, var(--c) 10%, white); padding: .15rem .55rem; border-radius: 999px;}
.pr-amount {font-weight: 700; color: #111827; white-space: nowrap;}
.pr-who {font-weight: 600; color: #111827;}
.pr-text {color: #374151; margin: .35rem 0 .2rem 0; font-size: .95rem;}
.pr-next {color: #6b7280; font-size: .85rem;}
.pr-ai {display: inline-block; font-size: .75rem; color: #7e22ce; border: 1px solid #e9d5ff; border-radius: 6px; padding: 0 .4rem; margin-left: .4rem;}
.pr-step {font-size: .8rem; font-weight: 700; color: #0f766e; letter-spacing: .06em; text-transform: uppercase; margin: 1.2rem 0 .2rem 0;}
</style>
"""


def _money(x: float) -> str:
    return f"KSh {x:,.0f}" if x == int(x) else f"KSh {x:,.2f}"


def _e(s: object) -> str:
    return html.escape(str(s))


def _secret(name: str) -> str | None:
    try:
        return st.secrets.get(name)  # Streamlit Cloud secrets, if any
    except Exception:  # noqa: BLE001  (no secrets file locally)
        return None


def _model(use_ai: bool, demo: bool):
    """Demo: the recorded answers from the live Claude run. Your data: live Claude, if a key is set."""
    if not use_ai:
        return None
    if demo:
        return RecordedModel()
    from payrecon.ai import AnthropicClient, LLMError

    key = os.environ.get("ANTHROPIC_API_KEY") or _secret("ANTHROPIC_API_KEY")
    if not key:
        st.sidebar.warning("Set ANTHROPIC_API_KEY to use the AI on your data.")
        return None
    os.environ["ANTHROPIC_API_KEY"] = key
    try:
        return AnthropicClient()
    except LLMError as e:
        st.sidebar.warning(str(e))
        return None


def _dashboard(demo: bool, db: str, store: str) -> Dashboard:
    key = (demo, db, store)
    if st.session_state.get("dash_key") != key:
        if demo:
            folder = st.session_state.setdefault("demo_folder", tempfile.mkdtemp(prefix="payrecon-demo-"))
            st.session_state["dash"] = reset_demo(folder)
            st.session_state["fresh_demo"] = True
        else:
            st.session_state["dash"] = Dashboard(db, store)
        st.session_state["dash_key"] = key
        st.session_state.pop("snap", None)
    return st.session_state["dash"]


def _say(msg: str, kind: str = "info") -> None:
    st.session_state["msg"] = (kind, msg)


def _card(color: str, badge: str, who: str, amount: str, text: str, nxt: str = "", ai: bool = False) -> str:
    c = COLORS[color]
    return (f'<div class="pr-card" style="--c:{c}"><div class="top"><div><span class="pr-who">{_e(who)}</span> '
            f'<span class="pr-badge">{_e(badge)}</span>{"<span class=pr-ai>AI suggestion</span>" if ai else ""}</div>'
            f'<div class="pr-amount">{_e(amount)}</div></div><div class="pr-text">{_e(text)}</div>'
            + (f'<div class="pr-next">→ {_e(nxt)}</div>' if nxt else "") + "</div>")


def _strip_prefix(text: str) -> str:
    """The linker's rationale starts with who decided; the card already says that."""
    for p in ("AI found no clear match: ", "AI chose among "):
        if text.startswith(p):
            text = text[len(p):]
            if p == "AI chose among " and ": " in text:
                text = text.split(": ", 1)[1]
    return text[:1].upper() + text[1:]


def _result_text(r: dict) -> str:
    parts = [f"{r['done']} fix(es) applied"]
    if r["cancelled"]:
        parts.append(f"{r['cancelled']} cancelled because the data had changed")
    if r["deferred"]:
        parts.append(f"{r['deferred']} waiting (could not reach M-Pesa)")
    if r["failed"] or r["unknown"]:
        parts.append(f"{r['failed'] + r['unknown']} need a person")
    text = "; ".join(parts) + "."
    if r["offline"]:
        text += " M-Pesa is offline here, so refunds and payment prompts are never sent."
    return text


def main() -> None:
    st.set_page_config(page_title="payrecon", page_icon="💳", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    # ---- sidebar: kept small ----------------------------------------------------------------------------
    sb = st.sidebar
    sb.markdown("### payrecon")
    sb.caption("Reconciles M-Pesa payments against your invoices and fixes what is safe to fix. "
               "Code and M-Pesa's own answers decide; the AI explains and suggests.")
    demo = True
    db, store = "app.db", "actions.db"
    if LOCAL:
        demo = sb.radio("Data", ["Demo", "My data"], horizontal=True) == "Demo"
        if not demo:
            db = sb.text_input("Books and ledger", "app.db")
            store = sb.text_input("Action store", "actions.db")
    d = _dashboard(demo, db, store)
    view = sb.radio("View", ["Payments in", "Payroll"], horizontal=True,
                    help="Payments in: customers paying you. Payroll: you paying people.")
    use_ai = sb.toggle("Use AI", value=demo,
                       help="Demo: replays Claude's real answers on these payments (20 Oct 2026). Your data: calls Claude.")
    llm = _model(use_ai, demo)

    with sb.expander("Advanced"):
        approvers = sorted(d.approvers)
        user = (st.selectbox("Acting as", approvers + ["ai-assistant"],
                             help="Try ai-assistant: machines are never allowed to approve.")
                if approvers else st.text_input("Acting as", ""))
        if not approvers:
            st.caption("Set PAYRECON_APPROVERS to name the people who may approve.")
        if st.button("Dry run", help="Check what would run against fresh data; changes nothing"):
            r = d.run(execute=False)
            _say("Dry run: " + (", ".join(f"#{i} {k} → {v}" for i, k, v in r["would_run"]) or "nothing to run."))
        if demo and st.button("Reset demo"):
            st.session_state["dash"] = d = reset_demo(st.session_state["demo_folder"])
            st.session_state["fresh_demo"] = True
            st.session_state.pop("msg", None)
            st.session_state.pop("snap", None)
    sb.caption("No sign-in: use your own machine for real data.")
    if view == "Payroll":
        payroll_page(d, user, demo)
        return

    # The demo starts with its fixes already planned, so there is something to decide straight away.
    if demo and st.session_state.pop("fresh_demo", False):
        d.plan(ai=True, llm=llm)
        st.session_state.pop("snap", None)
    if "snap" not in st.session_state or st.session_state.get("snap_ai") != use_ai:
        st.session_state["snap"] = d.snapshot(llm)
        st.session_state["snap_ai"] = use_ai
    snap = st.session_state["snap"]

    def refresh() -> None:
        st.session_state["snap"] = d.snapshot(llm)

    # ---- hero and numbers --------------------------------------------------------------------------------
    pill = ("Demo · real M-Pesa sandbox payments, 19–20 Oct 2026 · M-Pesa offline: nothing here can move money"
            if demo else f"Your data · {_e(db)}")
    st.markdown(f'<div class="pr-hero"><h1>Payment reconciliation</h1><p>Every M-Pesa payment checked against '
                f'your invoices. Safe fixes are applied for you; anything involving money needs your yes.</p>'
                f'<span class="pr-pill">{pill}</span></div>', unsafe_allow_html=True)
    s = snap["summary"]
    st.markdown(
        '<div class="pr-stats">'
        f'<div class="pr-stat"><div class="n">{s["waiting_approval"]}</div><div class="l">need your decision</div></div>'
        f'<div class="pr-stat"><div class="n">{s["problems"]}</div><div class="l">problems found</div></div>'
        f'<div class="pr-stat"><div class="n">{_money(s["at_stake"])}</div><div class="l">money at stake</div></div>'
        f'<div class="pr-stat"><div class="n">{s["invoices"]}</div><div class="l">invoices checked</div></div>'
        '</div>', unsafe_allow_html=True)

    if "msg" in st.session_state:
        kind, msg = st.session_state["msg"]
        (st.success if kind == "ok" else st.warning if kind == "warn" else st.info)(msg)
    if snap["halted"]:
        st.error(f"Fixes are paused: {snap['halted']}")

    # ---- 1. what is wrong --------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 1 · What is wrong</div>', unsafe_allow_html=True)
    links = {r["payment"]: r for r in snap["links"]}
    names = {i["invoice"]: (f"{i['customer']}'s invoice {i['reference']}" if i["customer"] else f"invoice {i['invoice']}")
             for i in snap["invoices"]}
    for e in snap["exceptions"]:
        label, color = LABELS.get(e["category"], (e["category"], "grey"))
        who = f"Invoice {e['subject']}" if e["kind"] == "invoice" else f"Payment {e['subject']}"
        text, nxt, ai = e["explanation"], NEXT.get(e["action"], ""), False
        lp = links.get(e["subject"])
        if lp and lp["invoice"]:
            why = _strip_prefix(lp["rationale"])
            nxt, ai = f"Probably {names.get(lp['invoice'], lp['invoice'])}. {why}", lp["decided_by"] == "llm"
        elif lp and lp["decided_by"] == "llm":
            nxt, ai = f"No safe match: {_strip_prefix(lp['rationale'])}", True
        st.markdown(_card(color, label, who, _money(e["at_stake"]), text, nxt, ai), unsafe_allow_html=True)
    if not snap["exceptions"]:
        st.success("Nothing is wrong: every payment matches an invoice.")

    # ---- 2. your decisions -------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 2 · Your decisions</div>', unsafe_allow_html=True)
    if not snap["approvals"]:
        st.caption("Nothing needs your decision right now.")
        if st.button("Look for fixes", help="Propose fixes for the problems above"):
            _say(d.plan(ai=True, llm=llm))
            refresh()
            st.rerun()
    for a in snap["approvals"]:
        with st.container(border=True):
            ai = a["proposed_by"].startswith("ai-")
            st.markdown(f"**{a['headline']}**" + ("  \n:violet[AI suggestion] " if ai else "  \n")
                        + (f"_{a['rationale']}_" if a["rationale"] else ""))
            c1, c2, _ = st.columns([1, 1, 5])
            if c1.button("Approve", key=f"ok{a['id']}", type="primary"):
                msg = d.approve(a["id"], user)
                _say(msg, "warn" if msg.startswith("refused") else "ok")
                refresh()
                st.rerun()
            if c2.button("Reject", key=f"no{a['id']}"):
                msg = d.reject(a["id"], user, "rejected in the dashboard")
                _say(msg, "warn" if msg.startswith("refused") else "info")
                refresh()
                st.rerun()

    # ---- 3. apply ---------------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 3 · Apply the fixes</div>', unsafe_allow_html=True)
    st.caption("Marks confirmed payments as paid, tells customers about failed payments, and carries out what you "
               "approved. Everything is checked against fresh data first.")
    sure = True if demo else st.checkbox("I understand this changes my books and may contact M-Pesa")
    if st.button("Apply fixes", type="primary", disabled=not sure):
        r = d.run(execute=True)
        _say(_result_text(r), "ok" if not (r["failed"] or r["unknown"]) else "warn")
        refresh()
        st.rerun()

    # ---- records ----------------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Records</div>', unsafe_allow_html=True)
    with st.expander(f"Invoices ({len(snap['invoices'])})"):
        st.dataframe([{"Invoice": i["invoice"], "Reference": i["reference"], "Customer": i["customer"], "Amount": i["amount"],
                       "Books say": i["books"], "Finding": LABELS.get(i["category"], (i["category"],))[0],
                       "Payments": i["payments"]} for i in snap["invoices"]], hide_index=True)
    if snap["reversed"] or snap["refunds"]:
        with st.expander("Money that went back to customers"):
            if snap["reversed"]:
                st.dataframe([{"Payment": r["payment"], "Amount": r["amount"], "Invoice ref": r["reference"], "How": r["how"]}
                              for r in snap["reversed"]], hide_index=True)
            if snap["refunds"]:
                st.dataframe(snap["refunds"], hide_index=True)
    with st.expander(f"Payments received by the webhook ({len(snap['inbox'])})"):
        if snap["inbox"]:
            st.dataframe(snap["inbox"], hide_index=True)
        else:
            st.caption("None in this database." + (" The demo's payments were imported, not received live." if demo else ""))
    with st.expander("Everything that happened (audit trail)"):
        st.caption("Tamper check: " + ("intact" if snap["audit_ok"] else "BROKEN"))
        if snap["actions"]:
            st.dataframe(snap["actions"], hide_index=True)
        st.dataframe(snap["audit"], hide_index=True)


def _week() -> str:
    from datetime import date

    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


PAY_LABEL = {"PAID": ("Paid", "green"), "FAILED": ("Failed: nothing was paid", "red"), "UNKNOWN": ("Unknown: check before doing anything", "amber"),
             "ACCEPTED": ("Sent, waiting for M-Pesa", "blue"), "QUEUED": ("Ready to send", "grey"), "WAITING": ("Waiting for approval", "grey"),
             "SENDING": ("Sending", "blue"), "CANCELLED": ("Cancelled", "grey")}


FLAG_LABEL = {"NUMBER_CHANGED": ("Number changed since last time", "red"), "NUMBER_REUSED": ("This number was paid to someone else", "red"),
              "NEW": ("First payment to this number", "amber")}
NAME_LABEL = {"MATCH": "matches", "MISMATCH": "does not match", "CONFIRMED": "checked: right person", "WRONG_PERSON": "wrong person"}


def payroll_page(d: Dashboard, user: str, demo: bool) -> None:
    from payrecon.dashboard import EXAMPLES
    from payrecon.payroll import PayrollError

    p = d.payroll()
    sub = ("Demo · simulated M-Pesa with last week's payroll already paid · numbers ending 0000 fail, 1111 time out · no real money moves" if demo
           else ("Your data · M-Pesa connected" if p.port else "Your data · no M-Pesa credentials: you can import and approve, not send"))
    st.markdown(f'<div class="pr-hero"><h1>Payroll</h1><p>Pay everyone in one approved batch instead of one by one. '
                f'Nobody is paid twice, and every payment ends paid, failed or flagged for you to check.</p>'
                f'<span class="pr-pill">{_e(sub)}</span></div>', unsafe_allow_html=True)
    if "msg" in st.session_state:
        kind, msg = st.session_state.pop("msg")
        (st.success if kind == "ok" else st.warning if kind == "warn" else st.info)(msg)

    batches = p.batches()
    names = [b["batch"] for b in batches]

    # ---- step 1: the list ------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 1 · The list</div>', unsafe_allow_html=True)
    c1, c2 = st.columns([2, 1])
    base = f"DEMO-{_week()}" if demo else _week()
    name = c2.text_input("Batch name", base if base not in names else f"{base}-{len(names) + 1}")
    up = c1.file_uploader("Spreadsheet saved as CSV: name, phone, amount", type=["csv"])
    text = up.getvalue().decode("utf-8", errors="replace") if up is not None else None
    b1, b2, _ = st.columns([1, 1, 3])
    use_demo = demo and b2.button("Use the demo list")
    if use_demo:
        text = (EXAMPLES / "payroll_demo.csv").read_text()
    if text is not None and (b1.button("Check and import", type="primary") or use_demo):
        try:
            c = p.create_batch(name, text, user or "unknown")
        except PayrollError as e:
            _say(str(e), "warn")
        else:
            if c.errors:
                _say("Nothing imported. Fix these in the file and try again: " + " · ".join(c.errors[:8]), "warn")
            else:
                st.session_state["pay_batch"] = name
                _say(f"Imported {len(c.lines)} people, total {_money(float(c.total))}. Check the list, then approve it.", "ok")
        st.rerun()
    if not names:
        st.caption("Upload this week's list" + (", or use the demo list." if demo else "."))
        return

    current = st.session_state.get("pay_batch") if st.session_state.get("pay_batch") in names else names[0]
    batch = st.selectbox("Batch", names, index=names.index(current))
    st.session_state["pay_batch"] = batch
    info = [b for b in batches if b["batch"] == batch][0]
    rep = p.report(batch)
    k = rep["counts"]
    st.markdown(
        '<div class="pr-stats">'
        f'<div class="pr-stat"><div class="n">{info["lines"]}</div><div class="l">people · {_money(float(rep["total"]))}</div></div>'
        f'<div class="pr-stat"><div class="n">{k.get("PAID", 0)}</div><div class="l">paid · {_money(float(rep["paid_total"]))}</div></div>'
        f'<div class="pr-stat"><div class="n">{k.get("FAILED", 0)}</div><div class="l">failed</div></div>'
        f'<div class="pr-stat"><div class="n">{k.get("UNKNOWN", 0)}</div><div class="l">to check</div></div>'
        '</div>', unsafe_allow_html=True)

    # ---- step 2: approve ---------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 2 · Approve once</div>', unsafe_allow_html=True)
    if info["status"] == "DRAFT":
        st.write(f"**Pay {info['lines']} people a total of {_money(float(rep['total']))}?** Check the list below first.")
        flags = p.review(batch)
        checked = True
        if flags:
            st.markdown(f"**Check these {len(flags)} before approving** (compared with everyone paid before):")
            for f in flags:
                label, color = FLAG_LABEL[f["flag"]]
                st.markdown(_card(color, label, f["name"], f["phone"], f["why"]), unsafe_allow_html=True)
            checked = st.checkbox(f"I have checked these {len(flags)} number(s) with the workers")
        if st.button("Approve batch", type="primary", disabled=not checked):
            try:
                p.approve(batch, user, d.approvers, checked=checked)
                _say(f"Approved by {user}. Now send it.", "ok")
            except PayrollError as e:
                _say(str(e), "warn")
            st.rerun()
    else:
        st.caption(f"{info['status'].capitalize()}" + (f" by {info['approved_by']}" if info["approved_by"] else "") + ".")

    # ---- step 3: send ------------------------------------------------------------------------------------
    st.markdown('<div class="pr-step">Step 3 · Send</div>', unsafe_allow_html=True)
    queued = k.get("QUEUED", 0)
    if info["status"] == "APPROVED" and queued:
        st.write(f"{queued} payment(s) ready to send.")
        sure = True if demo else st.checkbox("I understand this sends real money")
        if st.button("Send payments", type="primary", disabled=not (sure and p.port)):
            try:
                r = d.payroll_send(batch)
                _say(f"{r['accepted']} sent and accepted by M-Pesa, {r['unknown']} unknown (never resent: check them), "
                     f"{r['not_sent']} not sent (still ready)." + (" Results are in." if r.get("results") else
                                                                  " Results arrive through the receiver; refresh to see them."), "ok")
            except PayrollError as e:
                _say(str(e), "warn")
            st.rerun()
    elif info["status"] == "APPROVED":
        st.caption("Everything in this batch has been sent.")
    else:
        st.caption("Approve the batch first.")

    # ---- what needs you ------------------------------------------------------------------------------------
    attention = rep["attention"]
    if attention:
        st.markdown('<div class="pr-step">Needs you</div>', unsafe_allow_html=True)
        for r in attention:
            if r["name_check"] in ("MISMATCH", "WRONG_PERSON"):
                label = ("Paid, but to whom? The name does not match" if r["name_check"] == "MISMATCH"
                         else "Paid to the wrong person: ask Safaricom to reverse it")
                st.markdown(_card("red", label, r["name"], _money(float(r["amount"])),
                                  f"{r['phone']} · M-Pesa has this number registered to {r['paid_to']} · receipt {r['receipt']}"),
                            unsafe_allow_html=True)
                if r["name_check"] == "MISMATCH":
                    nc1, nc2, _ = st.columns([1, 1, 2])
                    if nc1.button("Right person", key=f"rp{r['payout_id']}"):
                        try:
                            p.confirm_name(r["payout_id"], user, d.approvers, right_person=True)
                            _say(f"{r['name']}: confirmed as the right person.", "ok")
                        except PayrollError as e:
                            _say(str(e), "warn")
                        st.rerun()
                    if nc2.button("Wrong person", key=f"wp{r['payout_id']}"):
                        try:
                            p.confirm_name(r["payout_id"], user, d.approvers, right_person=False)
                            _say(f"{r['name']}: recorded as paid to the wrong person. Ask Safaricom to reverse it, and pay "
                                 f"{r['name']} on the right number in a new batch.", "warn")
                        except PayrollError as e:
                            _say(str(e), "warn")
                        st.rerun()
                continue
            label, color = PAY_LABEL[r["status"]]
            why = r["result"] or r["detail"] or ""
            st.markdown(_card(color, label, r["name"], _money(float(r["amount"])), f"{r['phone']} · {why}"), unsafe_allow_html=True)
            if r["status"] == "UNKNOWN":
                rc1, rc2, rc3 = st.columns([2, 1, 1])
                receipt = rc1.text_input("Receipt from the M-Pesa statement", key=f"rc{r['payout_id']}", placeholder="e.g. UJK...")
                if rc2.button("It was paid", key=f"yes{r['payout_id']}"):
                    try:
                        p.resolve(r["payout_id"], user, d.approvers, paid=True, receipt=receipt)
                        _say(f"{r['name']} marked paid.", "ok")
                    except PayrollError as e:
                        _say(str(e), "warn")
                    st.rerun()
                if rc3.button("It was not paid", key=f"no{r['payout_id']}"):
                    try:
                        p.resolve(r["payout_id"], user, d.approvers, paid=False)
                        _say(f"{r['name']} marked not paid; you can send it again.", "ok")
                    except PayrollError as e:
                        _say(str(e), "warn")
                    st.rerun()
        if k.get("FAILED") and st.button(f"Send the {k['FAILED']} failed payment(s) again"):
            try:
                n = p.requeue_failed(batch, user, d.approvers)
                _say(f"{n} payment(s) ready to send again (as a new attempt).", "ok")
            except PayrollError as e:
                _say(str(e), "warn")
            st.rerun()

    with st.expander(f"Everyone in {batch} ({info['lines']})", expanded=info["status"] == "DRAFT"):
        st.dataframe([{"Line": r["line"], "Name": r["name"], "Phone": r["phone"], "Amount": float(r["amount"]),
                       "Status": PAY_LABEL.get(r["status"], (r["status"],))[0], "Receipt": r["receipt"] or "",
                       "Name on M-Pesa": NAME_LABEL.get(r["name_check"], ""),
                       "Note": r["result"] or r["detail"] or ""} for r in p.lines(batch)], hide_index=True)


if __name__ == "__main__":
    main()
