import math
import os
from types import SimpleNamespace

import pytest

from payrecon.ai.llm import AnthropicClient, LLMError, LLMGate, enum_or_none, unit_interval
from payrecon.ai.testing import ScriptedLLM
from payrecon.ai.text import clean_text, money
from payrecon.models import Action

CALL = dict(system="s", user="u", tool_name="record", tool_description="d", schema={"type": "object"})


class StubSDK:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.kwargs = content, error, None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.content)


def tool_use(name="record", data=None):
    return SimpleNamespace(type="tool_use", name=name, input=data if data is not None else {"a": 1})


def test_client_returns_the_tool_input():
    sdk = StubSDK([SimpleNamespace(type="text", text="hi"), tool_use(data={"choice": "I1"})])
    assert AnthropicClient("m", client=sdk).complete_json(**CALL) == {"choice": "I1"}


def test_client_does_not_force_tool_choice_and_instructs_the_model():
    sdk = StubSDK([tool_use()])
    AnthropicClient("some-model", client=sdk).complete_json(**CALL)
    k = sdk.kwargs
    assert k["model"] == "some-model"
    assert k["tool_choice"] == {"type": "auto"}  # forced tool choice is rejected by newer models
    assert "record" in k["system"] and k["system"].startswith("s")
    assert k["tools"][0]["name"] == "record" and k["tools"][0]["input_schema"] == {"type": "object"}
    assert k["messages"] == [{"role": "user", "content": "u"}]


def test_client_rejects_a_reply_without_the_tool_call():
    with pytest.raises(LLMError):
        AnthropicClient("m", client=StubSDK([SimpleNamespace(type="text", text="no")])).complete_json(**CALL)
    with pytest.raises(LLMError):
        AnthropicClient("m", client=StubSDK([tool_use(name="other")])).complete_json(**CALL)


def test_client_wraps_sdk_errors():
    with pytest.raises(LLMError, match="boom"):
        AnthropicClient("m", client=StubSDK(error=RuntimeError("boom"))).complete_json(**CALL)


def test_client_without_credentials_fails_cleanly():
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with pytest.raises(LLMError):
            AnthropicClient("m")
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_gate_without_a_client_does_nothing():
    gate = LLMGate(None)
    assert gate.ask(**CALL) is None and gate.calls == 0 and not gate.enabled


def test_gate_turns_failures_into_none_and_counts_them():
    gate = LLMGate(ScriptedLLM(lambda kw: RuntimeError("down")))
    assert gate.ask(**CALL) is None
    assert gate.failures == 1 and "down" in gate.warnings[0]


def test_gate_rejects_non_object_answers():
    gate = LLMGate(ScriptedLLM(lambda kw: ["not", "a", "dict"]))
    assert gate.ask(**CALL) is None and gate.failures == 1


def test_gate_enforces_the_call_budget_and_warns_once():
    llm = ScriptedLLM(lambda kw: {"ok": True})
    gate = LLMGate(llm, max_calls=2)
    results = [gate.ask(**CALL) for _ in range(4)]
    assert results == [{"ok": True}, {"ok": True}, None, None]
    assert len(llm.calls) == 2
    assert len([w for w in gate.warnings if "budget" in w]) == 1


@pytest.mark.parametrize(
    "x,expected",
    [(0, 0.0), (0.5, 0.5), (1, 1.0), (1.2, None), (-0.1, None), (True, None), ("0.5", None), (None, None), (math.nan, None), (math.inf, None)],
)
def test_unit_interval_rejects_instead_of_clamping(x, expected):
    assert unit_interval(x) == expected


def test_enum_or_none():
    assert enum_or_none("RECHECK_STATUS", Action) is Action.RECHECK_STATUS
    assert enum_or_none("REFUND", Action) is None
    assert enum_or_none(["x"], Action) is None


def test_clean_text_strips_control_characters_and_truncates():
    assert clean_text("a\nb\x00c\t d", 80) == "a b c d"
    assert clean_text("x" * 500, 10) == "x" * 10
    assert clean_text(None) == ""


def test_money_format():
    from decimal import Decimal
    assert money(Decimal("1500")) == "KSh 1,500"
    assert money(Decimal("1500.50")) == "KSh 1,500.50"
