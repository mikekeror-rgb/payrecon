"""Score the AI layer against synthetic ground truth.

    python -m payrecon.ai.evaluate --n 500 --seed 7                 # deterministic baseline, no LLM
    python -m payrecon.ai.evaluate --n 500 --seed 7 --llm anthropic # adds the model on top

Exit code 1 if any safety invariant is violated, so CI gates on safety, not accuracy.
Scores on synthetic data say how the code behaves on cases I wrote; they are not a
claim about real traffic.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ..matcher import reconcile
from ..models import Action
from ..synthetic import Dataset, generate
from .investigator import FailureReason, allowed_actions
from .linker import LLM_CAP
from .llm import AnthropicClient, LLMClient, LLMError
from .pipeline import InvestigationReport, investigate


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def evaluate_ai(ds: Dataset, air: InvestigationReport) -> dict:
    # ---- failure reasons
    kinds: dict[str, Counter] = defaultdict(Counter)
    for it in air.investigations:
        if it.reason is None or it.evidence_txn is None:
            continue
        k = kinds[ds.fail_kind[it.evidence_txn]]
        k["n"] += 1
        k["correct"] += it.reason.value == ds.fail_reason[it.evidence_txn]
        k["classified"] += it.reason is not FailureReason.UNKNOWN
        k[f"via_{it.reason_source}"] += 1
    reasons = {
        kind: {
            "n": c["n"],
            "accuracy": _ratio(c["correct"], c["n"]),
            "coverage": _ratio(c["classified"], c["n"]),
            "sources": {s: c[f"via_{s}"] for s in ("code", "keyword", "llm", "none") if c[f"via_{s}"]},
        }
        for kind, c in sorted(kinds.items())
    }
    total_n = sum(c["n"] for c in kinds.values())
    reasons_overall = {"n": total_n, "accuracy": _ratio(sum(c["correct"] for c in kinds.values()), total_n)}

    # ---- link proposals
    props = {p.txn_id: p for p in air.link_proposals}
    by_kind: dict[str, Counter] = defaultdict(Counter)
    bands: dict[str, Counter] = defaultdict(Counter)
    for tid in ds.unlinkable_txn_ids:
        p, truth = props.get(tid), ds.link_truth.get(tid)
        if p is None:
            outcome = "no_proposal"
        elif p.invoice_id is None:
            outcome = "abstain"
        elif p.invoice_id == truth:
            outcome = "correct"
        else:
            outcome = "wrong"
        by_kind[ds.orphan_kind[tid]][outcome] += 1
        if p is not None and p.invoice_id is not None:
            bands[p.band]["correct" if outcome == "correct" else "wrong"] += 1
    n_true = len(ds.link_truth)
    correct = sum(c["correct"] for c in by_kind.values())
    wrong = sum(c["wrong"] for c in by_kind.values())
    links = {
        "true_links": n_true,
        "correct": correct,
        "wrong": wrong,
        "abstained": sum(c["abstain"] for c in by_kind.values()),
        "precision": _ratio(correct, correct + wrong),
        "recall": _ratio(correct, n_true),
        "by_orphan_kind": {k: dict(c) for k, c in sorted(by_kind.items())},
        "by_band": {b: {"proposals": c["correct"] + c["wrong"], "precision": _ratio(c["correct"], c["correct"] + c["wrong"])}
                    for b, c in sorted(bands.items())},
    }

    # ---- safety invariants
    violations: list[str] = []
    for it in air.investigations:
        if it.recommended_action not in allowed_actions(it.policy_action):
            violations.append(f"{it.subject}: recommended {it.recommended_action.value} but policy is {it.policy_action.value}")
        if it.recommended_action is Action.AUTO_APPLY and it.policy_action is not Action.AUTO_APPLY:
            violations.append(f"{it.subject}: AI-driven AUTO_APPLY")
    for p in air.link_proposals:
        if p.decided_by == "llm" and p.confidence > LLM_CAP:
            violations.append(f"{p.txn_id}: LLM confidence {p.confidence} above cap {LLM_CAP}")
        if p.status != "PROPOSED":
            violations.append(f"{p.txn_id}: link proposal status {p.status}")
    return {
        "failure_reasons": reasons,
        "failure_reasons_overall": reasons_overall,
        "links": links,
        "llm_calls": air.llm_calls,
        "warnings": air.warnings,
        "violations": violations,
    }


def _f(x: float | None) -> str:
    return "  -  " if x is None else f"{x:5.2f}"


def format_report(res: dict) -> str:
    L = ["FAILURE REASON CLASSIFICATION (latest failed attempt per invoice)"]
    L.append(f"  {'kind':<12}{'n':>4}{'accuracy':>10}{'coverage':>10}   resolved by")
    for kind, m in res["failure_reasons"].items():
        L.append(f"  {kind:<12}{m['n']:>4}{_f(m['accuracy']):>10}{_f(m['coverage']):>10}   {m['sources']}")
    L.append(f"  overall accuracy {_f(res['failure_reasons_overall']['accuracy'])} over {res['failure_reasons_overall']['n']}")
    k = res["links"]
    L += ["", "ORPHAN LINKING (payments the deterministic matcher could not link)",
          f"  real links {k['true_links']}: correct {k['correct']}, wrong {k['wrong']}, abstained {k['abstained']} "
          f"(wrong includes proposals for strays)",
          f"  precision {_f(k['precision'])}   recall {_f(k['recall'])}", "",
          f"  {'orphan kind':<14}{'correct':>8}{'wrong':>7}{'abstain':>9}"]
    for kind, c in k["by_orphan_kind"].items():
        L.append(f"  {kind:<14}{c.get('correct', 0):>8}{c.get('wrong', 0):>7}{c.get('abstain', 0):>9}")
    L.append("")
    for b, m in k["by_band"].items():
        L.append(f"  band {b:<7} {m['proposals']:>3} proposals, precision {_f(m['precision'])}")
    L += ["", f"LLM calls: {res['llm_calls']}",
          f"SAFETY: {len(res['violations'])} violations (must be 0)"]
    L += [f"  - {v}" for v in res["violations"][:10]]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--llm", choices=["none", "anthropic"], default="none")
    ap.add_argument("--model")
    ap.add_argument("--max-llm-calls", type=int, default=300)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    llm: LLMClient | None = None
    if args.llm == "anthropic":
        try:
            llm = AnthropicClient(args.model)
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    ds = generate(args.n, args.seed)
    air = investigate(reconcile(ds.invoices, ds.txns), ds.invoices, ds.txns, llm, args.max_llm_calls)
    res = evaluate_ai(ds, air)
    print(format_report(res))
    if args.json:
        args.json.write_text(json.dumps(res, indent=2))
    return 1 if res["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
