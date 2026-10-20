"""AI investigation layer: link proposals, failure investigation, patterns.

The model proposes; code validates and decides. See README, "AI layer".
"""
from .investigator import FailureReason, Investigation, allowed_actions
from .linker import HIGH, LLM_CAP, MEDIUM, LinkProposal
from .llm import AnthropicClient, LLMClient, LLMError
from .pipeline import InvestigationReport, investigate, to_json

__all__ = [
    "AnthropicClient",
    "FailureReason",
    "HIGH",
    "Investigation",
    "InvestigationReport",
    "LLMClient",
    "LLMError",
    "LLM_CAP",
    "LinkProposal",
    "MEDIUM",
    "allowed_actions",
    "investigate",
    "to_json",
]
