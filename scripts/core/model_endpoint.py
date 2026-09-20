"""Provider-neutral model endpoint values consumed by agent config renderers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelEndpoint:
    """Everything Codex needs to address one OpenAI-compatible endpoint."""

    provider: str
    name: str
    base_url: str
    wire_api: str = ""
    credential_env: str = "OPENAI_API_KEY"
    supports_websockets: bool | None = None
    request_max_retries: int = 10
    stream_max_retries: int = 10

    def __post_init__(self) -> None:
        for field_name in ("provider", "name", "base_url", "credential_env"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"model endpoint {field_name} must be non-empty")
        for field_name in ("request_max_retries", "stream_max_retries"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"model endpoint {field_name} must be a non-negative integer"
                )


def _optional_bool(value: str, default: bool | None) -> bool | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"auto", "none"}:
        return None
    raise ValueError("RB_AGENT_SUPPORTS_WEBSOCKETS must be true, false, or auto")


def from_environment(
    environ: Mapping[str, str],
    *,
    base_url: str,
    provider: str,
    name: str,
    wire_api: str = "",
    supports_websockets: bool | None = None,
) -> ModelEndpoint:
    """Resolve the generic agent-endpoint contract with caller-owned defaults.

    Deployment adapters may supply ``RB_AGENT_*`` values for a local gateway or
    tunnel. Direct users can omit them and let each platform retain its existing
    endpoint shape. No gateway implementation or provider parameter appears in
    this contract.
    """

    def value(key: str, default: str) -> str:
        candidate = str(environ.get(key, "")).strip()
        return candidate if candidate else default

    return ModelEndpoint(
        provider=value("RB_AGENT_PROVIDER", provider),
        name=value("RB_AGENT_PROVIDER_NAME", name),
        base_url=value("RB_AGENT_BASE_URL", base_url),
        wire_api=value("RB_AGENT_WIRE_API", wire_api),
        credential_env=value("RB_AGENT_CREDENTIAL_ENV", "OPENAI_API_KEY"),
        supports_websockets=_optional_bool(
            environ.get("RB_AGENT_SUPPORTS_WEBSOCKETS", ""),
            supports_websockets,
        ),
        request_max_retries=int(value("RB_AGENT_REQUEST_MAX_RETRIES", "10")),
        stream_max_retries=int(value("RB_AGENT_STREAM_MAX_RETRIES", "10")),
    )
