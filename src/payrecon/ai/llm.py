"""LLM access, kept deliberately thin.

* `LLMClient` is a one-method protocol, so tests use a scripted fake and nothing
  else in the package imports an SDK.
* `AnthropicClient` asks for structured output through a tool. It uses
  `tool_choice: auto` plus an explicit instruction rather than a forced tool,
  because current Anthropic docs say some newer models (Opus 5.5, Sonnet 5.5,
  Fable 5.1) reject forced tool choice with a 400. Whatever comes back is
  validated by the caller; the model is never trusted to be well-formed.
* `LLMGate` is what the rest of the code talks to. It enforces a per-run call
  budget and turns every failure into `None`, so an outage degrades the AI layer
  to its deterministic fallback instead of crashing reconciliation.
"""
from __future__ import annotations

import math
import os
from enum import Enum
from typing import Any, Protocol, TypeVar, runtime_checkable

DEFAULT_MODEL = "claude-haiku-5-5"  # cheap and fast; override with PAYRECON_MODEL or --model

E = TypeVar("E", bound=Enum)


class LLMError(RuntimeError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    name: str

    def complete_json(
        self, *, system: str, user: str, tool_name: str, tool_description: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class AnthropicClient:
    name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        *,
        max_tokens: int = 1024,
        timeout: float = 30.0,
        client: Any = None,
    ):
        self.model = model or os.environ.get("PAYRECON_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("the Anthropic SDK is not installed: pip install 'payrecon[ai]'") from e
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(timeout=timeout)  # reads ANTHROPIC_API_KEY
        self._client = client

    def complete_json(
        self, *, system: str, user: str, tool_name: str, tool_description: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system + f"\n\nYou must answer by calling the `{tool_name}` tool exactly once. Do not reply in plain text.",
                messages=[{"role": "user", "content": user}],
                tools=[{"name": tool_name, "description": tool_description, "input_schema": schema}],
                tool_choice={"type": "auto"},
            )
        except Exception as e:  # SDK errors: network, auth, rate limit, 4xx/5xx
            raise LLMError(f"{type(e).__name__}: {e}") from e
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                data = getattr(block, "input", None)
                if isinstance(data, dict):
                    return dict(data)
        raise LLMError("model did not call the requested tool")


class LLMGate:
    """Budgeted, failure-proof access to an optional LLM."""

    def __init__(self, client: LLMClient | None, max_calls: int = 200):
        self.client = client
        self.max_calls = max_calls
        self.calls = 0
        self.failures = 0
        self.warnings: list[str] = []
        self._budget_warned = False

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def ask(self, **kwargs: Any) -> dict[str, Any] | None:
        if self.client is None:
            return None
        if self.calls >= self.max_calls:
            if not self._budget_warned:
                self.warnings.append(f"LLM call budget ({self.max_calls}) exhausted; remaining cases use the deterministic fallback")
                self._budget_warned = True
            return None
        self.calls += 1
        try:
            out = self.client.complete_json(**kwargs)
        except Exception as e:
            self.failures += 1
            self.warnings.append(f"LLM call failed ({type(e).__name__}): {str(e)[:160]}")
            return None
        if not isinstance(out, dict):
            self.failures += 1
            self.warnings.append("LLM returned a non-object answer")
            return None
        return out


def unit_interval(x: Any) -> float | None:
    """x if it is a real number in [0, 1], else None. Out-of-range is invalid, not clamped."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x) if 0.0 <= x <= 1.0 else None


def enum_or_none(value: Any, enum_cls: type[E]) -> E | None:
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return None
