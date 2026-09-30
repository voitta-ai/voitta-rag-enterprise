"""Deployment-wide assistant policy, managed by super-admins.

Stored in the admin ``settings.json`` (services/admin_store.py) next to the
other deployment-global typed settings, under ``assistant_*`` keys. Values
are validated on both read and write: a hand-edited or stale file can never
hand an engine an unknown model or effort.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import admin_store
from .catalog import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORTS, MODEL_IDS, Effort


@dataclass(frozen=True)
class AssistantPolicy:
    # Master switch. Off hides the launcher and refuses new turns.
    enabled: bool = True
    default_model: str = DEFAULT_MODEL
    default_effort: Effort = DEFAULT_EFFORT

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def load_policy() -> AssistantPolicy:
    raw = admin_store.load_settings()
    model = str(raw.get("assistant_default_model") or DEFAULT_MODEL)
    effort = str(raw.get("assistant_default_effort") or DEFAULT_EFFORT)
    return AssistantPolicy(
        enabled=bool(raw.get("assistant_enabled", True)),
        default_model=model if model in MODEL_IDS else DEFAULT_MODEL,
        default_effort=effort if effort in EFFORTS else DEFAULT_EFFORT,  # type: ignore[arg-type]
    )


def save_policy(
    *,
    enabled: bool | None = None,
    default_model: str | None = None,
    default_effort: str | None = None,
) -> AssistantPolicy:
    """Persist the given fields; raises ``ValueError`` on an unknown value."""
    updates: dict[str, object] = {}
    if enabled is not None:
        updates["assistant_enabled"] = bool(enabled)
    if default_model is not None:
        if default_model not in MODEL_IDS:
            raise ValueError(f"unknown model: {default_model}")
        updates["assistant_default_model"] = default_model
    if default_effort is not None:
        if default_effort not in EFFORTS:
            raise ValueError(f"unknown effort: {default_effort}")
        updates["assistant_default_effort"] = default_effort
    if updates:
        admin_store.save_settings(updates)
    return load_policy()
