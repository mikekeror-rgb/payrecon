"""One call that runs the whole AI layer over a reconciliation report."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from ..models import Invoice, ProviderTxn, ReconciliationReport
from .investigator import Investigation, Patterns, build_investigations, detect_patterns, summarize_patterns
from .linker import LinkProposal, propose_links
from .llm import LLMClient, LLMGate


@dataclass
class InvestigationReport:
    investigations: list[Investigation]
    link_proposals: list[LinkProposal]
    patterns: Patterns
    pattern_summary: dict[str, Any]
    llm_calls: int = 0
    warnings: list[str] = field(default_factory=list)


def investigate(
    report: ReconciliationReport,
    invoices: Iterable[Invoice],
    txns: Iterable[ProviderTxn],
    llm: LLMClient | None = None,
    max_llm_calls: int = 200,
) -> InvestigationReport:
    """Link proposals, failure investigations, patterns. `llm=None` runs fully deterministic."""
    invoices, txns = list(invoices), list(txns)
    gate = LLMGate(llm, max_calls=max_llm_calls)
    links = propose_links(report, invoices, txns, gate)
    items = build_investigations(report, invoices, txns, gate)
    reasons = {i.evidence_txn: i.reason for i in items if i.evidence_txn and i.reason}
    patterns = detect_patterns(report, txns, reasons)
    summary = summarize_patterns(patterns, gate)
    return InvestigationReport(items, links, patterns, summary, gate.calls, report.warnings + gate.warnings)


def _default(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def to_json(air: InvestigationReport) -> str:
    return json.dumps(dataclasses.asdict(air), default=_default, indent=2)
