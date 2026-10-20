"""A scripted stand-in for an LLM, for tests and offline demos."""
from __future__ import annotations

from typing import Any, Callable


class ScriptedLLM:
    """`responder(call_kwargs)` returns the answer dict, or an Exception instance to raise it."""

    name = "scripted"

    def __init__(self, responder: Callable[[dict[str, Any]], Any]):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        out = self.responder(kwargs)
        if isinstance(out, Exception):
            raise out
        return out
