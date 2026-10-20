"""Provider-neutral interfaces.

Two roles, kept separate because real providers split them:

* PaymentProvider: receives events (callbacks) and can ask "what is the status of
  this request?". Daraja has no "list my transactions" endpoint, so listing is not
  part of this interface.
* StatementSource: a pull-style batch of already-settled transactions (a CSV
  statement export, a bank API).

The reconciliation engine only ever sees ProviderTxn, so adding a card processor
or bank API means adding an adapter, not touching the engine.
"""
from __future__ import annotations

import re
from datetime import timedelta, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from ..models import ProviderTxn, TxnStatus

EAT = timezone(timedelta(hours=3))  # East Africa Time, used by Safaricom timestamps


class ProviderError(RuntimeError):
    """The provider answered in a way we do not understand. Never guess a status."""


class RateLimited(ProviderError):
    """The provider refused to answer right now (HTTP 429). Nothing is known about the request; ask again later."""


@runtime_checkable
class PaymentProvider(Protocol):
    name: str

    def parse_event(self, payload: Mapping[str, Any]) -> ProviderTxn: ...

    def get_status(self, request_id: str) -> TxnStatus: ...


@runtime_checkable
class StatementSource(Protocol):
    name: str

    def load(self) -> list[ProviderTxn]: ...


def mask_msisdn(raw: Any) -> str | None:
    """Mask a phone number at the edge: 254708374149 -> 2547****149.

    Already-masked values pass through. Non-numeric values (Daraja C2B v2 sends a
    hash) are shortened to a "hash:" prefix so no full identifier is retained.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "*" in s:
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) < 9 or len(digits) != len(re.sub(r"[\s+\-]", "", s)):
        return "hash:" + s[:6]
    return f"{digits[:4]}****{digits[-3:]}"
