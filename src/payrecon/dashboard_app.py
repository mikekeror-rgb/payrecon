"""payrecon dashboard (Streamlit). A thin page over payrecon.dashboard: every button calls the same planner,
approval desk and executor as the CLI, so the safety rules are the same.

    python -m payrecon.recover dashboard            # local, your own app.db / actions.db (and the demo)
    streamlit run streamlit_app.py                  # what Streamlit Community Cloud runs: demo only

Your own data is offered only when PAYRECON_DASHBOARD_LOCAL=1 (the `dashboard` command sets it and binds to
127.0.0.1). A hosted copy shows the demo: the live sandbox payments of 19-20 Oct 2026, phone numbers masked, with
the gateway offline so nothing can reach Daraja.
"""
from __future__ import annotations

import os
import tempfile

import streamlit as st

from payrecon.dashboard import DEMO_APPROVER, Dashboard, RecordedModel, reset_demo

LOCAL = os.environ.get("PAYRECON_DASHBOARD_LOCAL") == "1"
MODEL_OFF, MODEL_RECORDED, MODEL_CLAUDE = "Off (code only)", "Recorded Claude answers", "Claude (live API call)"


def _money(x: float) -> str:
    return f"KSh {x:,.0f}" if x == int(x) else f"KSh {x:,.2f}"


def _secret(name: str) -> str | None:
    try:
        return st.secrets.get(name)  # Streamlit Cloud secrets, if any
    except Exception:  # noqa: BLE001  (no secrets file locally)
        return None


def _llm(choice: str):
    if choice == MODEL_RECORDED:
        return RecordedModel()
    if choice == MODEL_CLAUDE:
        from payrecon.ai import AnthropicClient, LLMError

        key = os.environ.get("ANTHROPIC_API_KEY") or _secret("ANTHROPIC_API_KEY")
        if not key:
            st.sidebar.error("Set ANTHROPIC_API_KEY to use the live model.")
            return None
        os.environ["ANTHROPIC_API_KEY"] = key
        try:
            return AnthropicClient()
        except LLMError as e:
            st.sidebar.error(str(e))
    return None


def _dashboard(mode: str, db: str, store: str) -> Dashboard:
    key = (mode, db, store)
    if st.session_state.get("dash_key") != key:
        if mode == "demo":
            folder = st.session_state.setdefault("demo_folder", tempfile.mkdtemp(prefix="payrecon-demo-"))
            st.session_state["dash"] = reset_demo(folder)
        else:
            st.session_state["dash"] = Dashboard(db, store)
        st.session_state["dash_key"] = key
        st.session_state.pop("snap", None)
    return st.session_state["dash"]


def _refresh(d: Dashboard, llm) -> None:
    st.session_state["snap"] = d.snapshot(llm)


def _say(msg: str) -> None:
    st.session_state.setdefault("log", []).insert(0, msg)


def main() -> None:
    st.set_page_config(page_title="payrecon", layout="wide")

    # ---- sidebar: where the data comes from, who you are, what the model does -------------------------
    sb = st.sidebar
    sb.title("payrecon")
    sb.caption("M-Pesa reconciliation and safe recovery. Code and the provider decide; the AI explains and proposes.")
    modes = ["Demo: live sandbox capture"] + (["My data (app.db)"] if LOCAL else [])
    mode = "demo" if sb.radio("Data", modes) == modes[0] else "mine"
    db, store = "app.db", "actions.db"
    if mode == "mine":
        db = sb.text_input("Books and ledger", "app.db")
        store = sb.text_input("Action store", "actions.db")
    d = _dashboard(mode, db, store)

    approvers = sorted(d.approvers)
    if approvers:
        user = sb.selectbox("Acting as (approver)", approvers + ["someone-else", "ai-assistant"])
    else:
        sb.warning("No approvers configured: set PAYRECON_APPROVERS (a stand-in for real sign-in).")
        user = sb.text_input("Acting as", "")
    sb.caption("There is no sign-in: run this on your own machine for real data. Machine identities can never approve.")

    choice = sb.radio("Model", [MODEL_OFF, MODEL_RECORDED, MODEL_CLAUDE] if mode == "demo" else [MODEL_OFF, MODEL_CLAUDE])
    if choice == MODEL_RECORDED:
        sb.caption(RecordedModel.LABEL + ". Only the two cases recorded live have answers.")
    llm = _llm(choice)

    if sb.button("Refresh", help="Re-read the data and re-run the investigation (with the chosen model)") or "snap" not in st.session_state \
            or st.session_state.get("snap_model") != choice:
        _refresh(d, llm)
        st.session_state["snap_model"] = choice
    snap = st.session_state["snap"]

    sb.divider()
    sb.subheader("Recovery")
    if sb.button("Plan actions", help="Propose actions from the current data; AI link proposals included (each needs approval)"):
        _say(d.plan(ai=True, llm=llm))
        _refresh(d, llm)
    if sb.button("Dry run", help="Check what would run, against fresh data; changes nothing"):
        r = d.run(execute=False)
        _say("dry run: " + (", ".join(f"#{i} {k}: {v}" for i, k, v in r["would_run"]) or "nothing runnable"))
    confirm = sb.checkbox("I understand Execute acts on the data" + (" (demo: Daraja is offline)" if d.offline else ""))
    if sb.button("Execute", disabled=not confirm):
        r = d.run(execute=True)
        _say(f"executed: done {r['done']}, cancelled {r['cancelled']}, deferred {r['deferred']}, failed {r['failed']}, "
             f"unknown {r['unknown']}" + (" (offline: refunds and retries refused)" if r["offline"] else "")
             + ("; " + "; ".join(r["notes"][:3]) if r["notes"] else ""))
        _refresh(d, llm)
    if mode == "demo" and sb.button("Reset demo"):
        st.session_state["dash"] = reset_demo(st.session_state["demo_folder"])
        st.session_state.pop("log", None)
        _refresh(st.session_state["dash"], llm)
        d = st.session_state["dash"]
        snap = st.session_state["snap"]

    # ---- header ------------------------------------------------------------------------------------------
    s = snap["summary"]
    st.title("Payment reconciliation")
    if mode == "demo":
        st.caption("Demo data: real Daraja sandbox payments from 19-20 Oct 2026 (phone numbers masked), plus their "
                   "reversals. Daraja is offline here: nothing you do can move money.")
    if snap["halted"]:
        st.error(f"Executor halted: {snap['halted']}")
    c = st.columns(5)
    c[0].metric("Invoices", s["invoices"])
    c[1].metric("Payments", s["payments"])
    c[2].metric("Problems", s["problems"])
    c[3].metric("Money at stake", _money(s["at_stake"]))
    c[4].metric("Waiting for approval", s["waiting_approval"])
    for line in st.session_state.get("log", [])[:4]:
        st.info(line)
    for w in snap["warnings"][:3]:
        st.warning(w)

    tabs = st.tabs(["Problems", "Approvals", "Invoices", "Payments in", "Reversals and refunds", "Audit"])

    with tabs[0]:
        st.caption("Ranked by money at stake. Categories and actions come from code; the text explains them.")
        for e in snap["exceptions"]:
            with st.container(border=True):
                st.markdown(f"**{e['subject']}** · `{e['category']}` → `{e['action']}` · {_money(e['at_stake'])} at stake")
                st.write(e["explanation"])
                if e["reason"]:
                    st.caption(f"Failure reason: {e['reason']} (from {e['reason_source']})")
        if snap["links"]:
            st.subheader("Who does an unlinked payment belong to?")
            st.caption(f"Advice only. Model calls this view: {s['llm_calls']}. Model confidence is capped below HIGH.")
            st.dataframe(snap["links"], hide_index=True)

    with tabs[1]:
        if not snap["approvals"]:
            st.write("Nothing is waiting. Use **Plan actions** in the sidebar to propose actions from the current data.")
        for a in snap["approvals"]:
            with st.container(border=True):
                st.markdown(f"**#{a['id']} {a['type']}** on invoice **{a['invoice']}** · proposed by `{a['proposed_by']}`")
                st.caption(a["details"])
                if a["rationale"]:
                    st.write(a["rationale"])
                note = st.text_input("Note", key=f"note{a['id']}", placeholder="why you approve or reject")
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Approve", key=f"ok{a['id']}"):
                    _say(d.approve(a["id"], user, note))
                    _refresh(d, llm)
                    st.rerun()
                if b2.button("Reject", key=f"no{a['id']}"):
                    _say(d.reject(a["id"], user, note))
                    _refresh(d, llm)
                    st.rerun()
        if snap["reviews"]:
            st.subheader("Needs a person (no automatic action)")
            st.dataframe([{"invoice": r.get("invoice_id"), "reason": r.get("reason"), "opened": str(r.get("created_at", ""))[:19]}
                          for r in snap["reviews"]], hide_index=True)
        if snap["actions"]:
            st.subheader("All actions")
            st.dataframe(snap["actions"], hide_index=True)

    with tabs[2]:
        st.dataframe(snap["invoices"], hide_index=True)

    with tabs[3]:
        st.caption("Callbacks Daraja sent to the webhook receiver (`serve`). Stored first, then verified.")
        if snap["inbox"]:
            st.dataframe(snap["inbox"], hide_index=True)
        else:
            st.write("No callbacks in this database." + (" The demo data was imported, not received." if mode == "demo" else ""))

    with tabs[4]:
        st.subheader("Payments that went back to the payer")
        st.caption("A reversed payment never counts towards an invoice, so it is never applied or refunded again.")
        if snap["reversed"]:
            st.dataframe(snap["reversed"], hide_index=True)
        else:
            st.write("None.")
        st.subheader("Refunds payrecon requested")
        if snap["refunds"]:
            st.dataframe(snap["refunds"], hide_index=True)
        else:
            st.write("None.")

    with tabs[5]:
        st.write("Hash chain: **intact**" if snap["audit_ok"] else "Hash chain: **BROKEN**")
        st.dataframe(snap["audit"], hide_index=True)


if __name__ == "__main__":
    main()
