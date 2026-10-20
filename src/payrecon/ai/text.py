"""Small helpers for handling untrusted text and formatting money."""
from __future__ import annotations

import re
from decimal import Decimal

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_text(value: object, limit: int = 80) -> str:
    """Strip control characters, collapse whitespace, truncate.

    Payer-typed references and provider messages are untrusted: they are cleaned
    before they go into a prompt or a report line.
    """
    if value is None:
        return ""
    s = " ".join(_CTRL.sub(" ", str(value)).split())
    return s[:limit]


def money(x: Decimal) -> str:
    s = f"{x:,.2f}"
    return "KSh " + (s[:-3] if s.endswith(".00") else s)
