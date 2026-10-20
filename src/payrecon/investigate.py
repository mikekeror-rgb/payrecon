"""Run reconciliation and the AI investigation layer, print a review report.

    python -m payrecon.investigate                       # synthetic data, no LLM
    python -m payrecon.investigate --data-dir data       # CSVs from payrecon.synthetic, or your own
    python -m payrecon.investigate --db app.db           # your live books and ledger (the same file `serve` writes)
    python -m payrecon.investigate --llm anthropic       # needs the SDK and ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import sys
import textwrap
from collections import Counter
from pathlib import Path

from .ai import AnthropicClient, LLMClient, LLMError, investigate, to_json
from .ai.investigator import Investigation
from .ai.text import money
from .matcher import dedupe_txns, reconcile
from .models import Invoice, ProviderTxn
from .providers import CsvStatementSource, load_invoices_csv
from .synthetic import generate


def load_data(args: argparse.Namespace) -> tuple[list[Invoice], list[ProviderTxn]]:
    if getattr(args, "db", None):
        from .adapters import SQLITE_DDL, Database, SqlBooks, SqlLedger, migrate_sqlite

        db = Database.from_url(args.db)
        if db.dialect == "sqlite":
            db.script(SQLITE_DDL)
            migrate_sqlite(db)
        args._applied = SqlBooks(db).applications()
        return SqlBooks(db).invoices(), SqlLedger(db).all_txns()
    if args.data_dir:
        d = Path(args.data_dir)
        return load_invoices_csv(d / "invoices.csv"), CsvStatementSource(d / "transactions.csv").load()
    ds = generate(args.n, args.seed)
    return ds.invoices, ds.txns


def make_llm(name: str, model: str | None) -> LLMClient | None:
    if name == "none":
        return None
    return AnthropicClient(model)


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent)


def render(report, air, top: int) -> str:
    L: list[str] = []
    inv_counts = Counter(r.category.value for r in report.invoices.values())
    L.append("RECONCILIATION")
    L.append("  " + ", ".join(f"{k} {v}" for k, v in sorted(inv_counts.items(), key=lambda kv: -kv[1])))
    L.append(f"  unlinked payments (orphans): {len(report.orphans)}")
    L.append("  proposed actions: " + ", ".join(f"{a} {n}" for a, n in sorted(Counter(r.action.value for r in report.invoices.values()).items())))
    if report.warnings:
        L.append(f"  warnings: {len(report.warnings)} (first: {report.warnings[0]})")

    L += ["", f"EXCEPTION QUEUE (top {min(top, len(air.investigations))} of {len(air.investigations)}, largest amount at stake first)"]
    for i, it in enumerate(air.investigations[:top], start=1):
        L.append(f"  {i:>2}. {money(it.exposure):>11}  {it.category.value:<19} -> {it.recommended_action.value:<16} {it.kind} {it.subject}")
        L.append(_wrap(it.explanation, "        "))
        for n in it.notes:
            L.append(_wrap("note: " + n, "        "))

    props = air.link_proposals
    by_band = Counter(p.band for p in props if p.invoice_id)
    L += ["", f"LINK PROPOSALS for unlinked payments ({len(props)}); advice only, nothing is applied automatically",
          "  proposed: " + (", ".join(f"{b} {by_band[b]}" for b in ("HIGH", "MEDIUM", "LOW") if by_band[b]) or "none")
          + f"; abstained: {sum(1 for p in props if not p.invoice_id)}"]
    for p in props[:top]:
        target = p.invoice_id or "(none)"
        shared = f"  [also proposed for {', '.join(p.shared_with)}]" if p.shared_with else ""
        L.append(f"  {p.txn_id} -> {target:<8} {p.confidence:.2f} {p.band:<6} {p.decided_by:<9}{shared}")
        L.append(_wrap(p.rationale, "        "))

    L += ["", "FAILURE PATTERNS"] + ["  " + ln for ln in air.pattern_summary["deterministic"].splitlines()]
    ai = air.pattern_summary.get("ai")
    if ai:
        L.append("  AI narrative (unverified):")
        L.append(_wrap(ai["headline"], "    "))
        L += [_wrap("cause? " + c, "    ") for c in ai["likely_causes"]]
        L += [_wrap("check: " + c, "    ") for c in ai["suggested_checks"]]
    L += ["", f"LLM calls: {air.llm_calls}" + (f"   warnings: {len(air.warnings)}" if air.warnings else "")]
    for w in air.warnings[:5]:
        L.append("  - " + w)
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="read the live books and ledger from this database (SQLite path or postgres:// URL)")
    ap.add_argument("--data-dir", help="folder with invoices.csv and transactions.csv (default: generate synthetic data)")
    ap.add_argument("--n", type=int, default=300, help="synthetic invoices when no --data-dir")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--llm", choices=["none", "anthropic"], default="none")
    ap.add_argument("--model", help="model ID for --llm anthropic (default claude-haiku-5-5 or $PAYRECON_MODEL)")
    ap.add_argument("--max-llm-calls", type=int, default=200)
    ap.add_argument("--top", type=int, default=12, help="rows to show per section")
    ap.add_argument("--json", type=Path, help="also write the full investigation as JSON")
    args = ap.parse_args(argv)

    try:
        llm = make_llm(args.llm, args.model)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    invoices, txns = load_data(args)
    report = reconcile(invoices, txns, applied=getattr(args, "_applied", None))
    unique = len(dedupe_txns(txns)[0])
    print(f"{len(invoices)} invoices, {len(txns)} transaction records ({unique} unique)\n")
    air = investigate(report, invoices, txns, llm, args.max_llm_calls)
    print(render(report, air, args.top))
    if args.json:
        args.json.write_text(to_json(air))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
