"""Score the reconciliation engine against synthetic ground truth.

    python -m payrecon.evaluate --n 500 --seed 7 [--json eval_results.json]

Exit code is 1 if the engine ever proposes an unsafe automatic action, so CI can
gate on safety rather than on accuracy.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .matcher import reconcile
from .models import Action, Category, ReconciliationReport
from .synthetic import Dataset, generate


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def evaluate(ds: Dataset, report: ReconciliationReport) -> dict:
    pred = {iid: r.category for iid, r in report.invoices.items()}

    per_class = {}
    for c in Category:
        if c is Category.ORPHANED_TXN:
            continue
        tp = sum(1 for i, t in ds.truth.items() if t is c and pred[i] is c)
        fp = sum(1 for i, t in ds.truth.items() if t is not c and pred[i] is c)
        fn = sum(1 for i, t in ds.truth.items() if t is c and pred[i] is not c)
        p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        f1 = round(2 * p * r / (p + r), 4) if p and r else (0.0 if (p is not None or r is not None) else None)
        per_class[c.value] = {"support": tp + fn, "precision": p, "recall": r, "f1": f1}

    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for iid, scen in ds.scenario.items():
        by_scenario[scen].append(pred[iid] is ds.truth[iid])
    scenario_accuracy = {s: _ratio(sum(v), len(v)) for s, v in sorted(by_scenario.items())}

    confusion = Counter((ds.truth[i].value, pred[i].value) for i in ds.truth if pred[i] is not ds.truth[i])

    flagged = set(report.orphans)
    tp = len(flagged & ds.unlinkable_txn_ids)
    orphans = {
        "unlinkable": len(ds.unlinkable_txn_ids),
        "flagged": len(flagged),
        "precision": _ratio(tp, len(flagged)),
        "recall": _ratio(tp, len(ds.unlinkable_txn_ids)),
        "recoverable_by_better_matcher": len(ds.link_truth),
    }

    false_auto_apply = [
        i for i, r in report.invoices.items() if r.action is Action.AUTO_APPLY and ds.truth[i] is not Category.UNRECORDED_PAYMENT
    ]
    unsafe_retry = [
        i for i, r in report.invoices.items() if r.action is Action.RETRY_ELIGIBLE and ds.truth[i] is not Category.FAILED
    ]
    safety = {
        "false_auto_apply": len(false_auto_apply),
        "unsafe_retry": len(unsafe_retry),
        "unsafe_total": len(false_auto_apply) + len(unsafe_retry),
    }

    return {
        "invoices": len(ds.invoices),
        "transactions": len(ds.txns),
        "accuracy": _ratio(sum(1 for i in ds.truth if pred[i] is ds.truth[i]), len(ds.truth)),
        "per_class": per_class,
        "scenario_accuracy": scenario_accuracy,
        "confusion": [{"truth": t, "predicted": p, "count": n} for (t, p), n in confusion.most_common()],
        "orphans": orphans,
        "safety": safety,
        "actions": dict(Counter(r.action.value for r in report.invoices.values())),
        "warnings": report.warnings,
    }


def _fmt(x: float | None) -> str:
    return "  -  " if x is None else f"{x:5.2f}"


def format_report(res: dict) -> str:
    L = [f"Invoices: {res['invoices']}   Transactions: {res['transactions']}   Invoice accuracy: {_fmt(res['accuracy'])}", ""]
    L.append(f"{'category':<20}{'support':>8}{'prec':>7}{'recall':>8}{'f1':>7}")
    for c, m in res["per_class"].items():
        L.append(f"{c:<20}{m['support']:>8}{_fmt(m['precision']):>7}{_fmt(m['recall']):>8}{_fmt(m['f1']):>7}")
    L += ["", "Accuracy by scenario:"]
    for s, a in res["scenario_accuracy"].items():
        L.append(f"  {s:<22}{_fmt(a)}")
    if res["confusion"]:
        L += ["", "Misclassifications (truth -> predicted):"]
        L += [f"  {c['truth']} -> {c['predicted']}: {c['count']}" for c in res["confusion"]]
    o = res["orphans"]
    L += [
        "",
        f"Orphan detection: flagged {o['flagged']} of {o['unlinkable']} unlinkable txns "
        f"(precision {_fmt(o['precision'])}, recall {_fmt(o['recall'])}); "
        f"{o['recoverable_by_better_matcher']} of them belong to a real invoice a fuzzy matcher could recover.",
        "",
        f"Safety: false auto-apply = {res['safety']['false_auto_apply']}, unsafe retry = {res['safety']['unsafe_retry']}  (both must be 0)",
        "Actions: " + ", ".join(f"{a}={n}" for a, n in sorted(res["actions"].items())),
    ]
    if res["warnings"]:
        L.append(f"Warnings: {len(res['warnings'])}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    args = ap.parse_args(argv)

    ds = generate(args.n, args.seed)
    res = evaluate(ds, reconcile(ds.invoices, ds.txns))
    print(format_report(res))
    if args.json:
        args.json.write_text(json.dumps(res, indent=2))
    return 1 if res["safety"]["unsafe_total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
