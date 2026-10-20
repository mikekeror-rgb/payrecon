import json
import os

from payrecon import investigate as cli
from payrecon.ai import evaluate as ai_eval
from payrecon.ai.linker import LLM_CAP
from payrecon.ai.pipeline import investigate, to_json
from payrecon.ai.testing import ScriptedLLM
from payrecon.matcher import reconcile
from payrecon.models import Action
from payrecon.synthetic import generate, write_dataset


def run(seed=7, n=500, llm=None):
    ds = generate(n, seed)
    air = investigate(reconcile(ds.invoices, ds.txns), ds.invoices, ds.txns, llm)
    return ds, air, ai_eval.evaluate_ai(ds, air)


def test_baseline_without_an_llm():
    _, air, res = run()
    assert res["violations"] == [] and air.llm_calls == 0
    kinds = res["failure_reasons"]
    assert kinds["known"]["accuracy"] == 1.0 and kinds["novel_easy"]["accuracy"] == 1.0
    # The documented gap: messages the keyword rules do not cover stay unknown until an LLM sees them.
    assert kinds["novel_hard"]["coverage"] == 0.0
    links = res["links"]
    assert links["by_band"]["HIGH"]["precision"] == 1.0
    assert links["precision"] > 0.9 and links["recall"] > 0.5
    assert all("no_proposal" not in c for c in links["by_orphan_kind"].values())


def test_safety_holds_across_seeds_without_an_llm():
    for seed in (1, 2, 3):
        assert run(seed, 300)[2]["violations"] == []


def adversarial(kw):
    """A model that does the worst it can: always confident, always recommending an unsafe action."""
    tool = kw["tool_name"]
    if tool == "record_failure_analysis":
        return {"reason": "timeout", "confidence": 0.99, "recommended_action": "RETRY_ELIGIBLE", "explanation": "retry it"}
    if tool == "record_link_decision":
        first = json.loads(kw["user"])["candidates"][0]["invoice_id"]
        return {"choice": first, "confidence": 1.0, "rationale": "certain"}
    return {"headline": "all fine", "likely_causes": [], "suggested_checks": []}


def test_an_adversarial_llm_cannot_break_the_safety_invariants():
    for seed in (7, 2):
        llm = ScriptedLLM(adversarial)
        ds, air, res = run(seed, 400, llm)
        assert res["violations"] == [] and air.llm_calls > 0
        llm_links = [p for p in air.link_proposals if p.decided_by == "llm"]
        assert llm_links and all(p.confidence == LLM_CAP and p.status == "PROPOSED" for p in llm_links)
        for it in air.investigations:
            if it.reason_source == "llm":
                assert it.recommended_action is Action.ESCALATE_HUMAN  # policy action for unknown codes
        assert any(it.reason_source == "llm" for it in air.investigations)


def test_llm_call_budget_is_respected():
    llm = ScriptedLLM(adversarial)
    ds = generate(400, 7)
    air = investigate(reconcile(ds.invoices, ds.txns), ds.invoices, ds.txns, llm, max_llm_calls=3)
    assert air.llm_calls == 3 and len(llm.calls) == 3
    assert any("budget" in w for w in air.warnings)


def test_json_export_is_valid():
    ds = generate(120, 4)
    air = investigate(reconcile(ds.invoices, ds.txns), ds.invoices, ds.txns)
    data = json.loads(to_json(air))
    assert set(data) >= {"investigations", "link_proposals", "patterns", "pattern_summary"}
    assert data["investigations"][0]["category"]


def test_eval_cli_exit_code_is_zero_when_safe():
    assert ai_eval.main(["--n", "200", "--seed", "1"]) == 0


def test_investigate_cli_on_synthetic_data():
    assert cli.main(["--n", "80", "--seed", "2", "--top", "3"]) == 0


def test_investigate_cli_reads_csv_folder_and_writes_json(tmp_path):
    write_dataset(generate(100, 5), tmp_path / "data")
    out = tmp_path / "out.json"
    assert cli.main(["--data-dir", str(tmp_path / "data"), "--top", "2", "--json", str(out)]) == 0
    assert json.loads(out.read_text())["link_proposals"] is not None


def test_cli_reports_a_missing_api_key_instead_of_crashing():
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert cli.main(["--llm", "anthropic"]) == 2
        assert ai_eval.main(["--llm", "anthropic"]) == 2
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
