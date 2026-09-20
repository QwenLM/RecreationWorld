"""Shared retry policy for recreation-model requests.

Retries at this layer belong to the model proxy and apply to one API request. Agent
processes are launched once; retrying a whole rollout is a separate scheduler decision
made from the final outcome contract.
"""

from __future__ import annotations

DEFAULT_MODEL_MAX_RETRIES = 10


def non_negative_int(value: object, *, name: str = "value") -> int:
    """Parse a non-negative integer without accepting booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed
