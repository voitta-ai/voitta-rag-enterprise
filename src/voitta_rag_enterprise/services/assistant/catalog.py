"""Engines, models and effort levels the assistant offers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EngineId = Literal["anthropic_api", "claude_subscription"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
CredentialKind = Literal["anthropic_api_key", "claude_oauth_token"]

ENGINE_IDS: tuple[EngineId, ...] = ("anthropic_api", "claude_subscription")
EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")

# The credential each engine runs on.
ENGINE_CREDENTIAL: dict[EngineId, CredentialKind] = {
    "anthropic_api": "anthropic_api_key",
    "claude_subscription": "claude_oauth_token",
}

ENGINE_LABELS: dict[EngineId, str] = {
    "anthropic_api": "Anthropic API",
    "claude_subscription": "Claude subscription",
}
# Compact forms for the chat window's header pickers.
ENGINE_SHORT_LABELS: dict[EngineId, str] = {
    "anthropic_api": "API",
    "claude_subscription": "Subscription",
}


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    # Compact form for the chat window's header pickers.
    short: str


# Offered models, most capable default first. All support adaptive thinking
# and the effort parameter on both engines.
MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("claude-opus-5", "Claude Opus 5", "Opus 5"),
    ModelInfo("claude-sonnet-5", "Claude Sonnet 5", "Sonnet 5"),
    ModelInfo("claude-fable-5-1", "Claude Fable 5.1", "Fable 5.1"),
)
MODEL_IDS = frozenset(m.id for m in MODELS)
DEFAULT_MODEL = MODELS[0].id
DEFAULT_EFFORT: Effort = "high"
