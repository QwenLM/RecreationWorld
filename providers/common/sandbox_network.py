"""Shared FC Sandbox network policy helpers."""

from __future__ import annotations

import os
from urllib.parse import urlparse

ALL_OUTBOUND = "0.0.0.0/0"
API_BASE_URL_ENVS = ("RB_MODEL_BASE_URL", "RB_VLM_BASE_URL")
API_ONLY_MODES = {"api_only", "api-only", "strict_api", "strict-api"}


def truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def hostname_from_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").strip("[]").lower()


def auto_api_allow_out() -> list[str]:
    hosts = [hostname_from_base_url(os.environ.get(name, "")) for name in API_BASE_URL_ENVS]
    hosts.extend(csv_env("RB_SANDBOX_EXTRA_API_ALLOW_OUT"))
    return dedupe([host for host in hosts if host])


def sandbox_network_config_from_env() -> tuple[bool, dict[str, list[str]]]:
    """Build E2B/FC sandbox network options from RB_SANDBOX_* env vars.

    `RB_SANDBOX_NETWORK_MODE=api_only` blocks all outbound traffic except:
    - hosts explicitly listed in `RB_SANDBOX_ALLOW_OUT`
    - hostnames parsed from `RB_MODEL_BASE_URL` and `RB_VLM_BASE_URL`
    - additional entries in `RB_SANDBOX_EXTRA_API_ALLOW_OUT`

    The FC/E2B API supports host/CIDR allow lists here, not port-level allow
    rules. If a provider URL contains a port, only its hostname/IP is passed.
    """

    mode = os.environ.get("RB_SANDBOX_NETWORK_MODE", "").strip().lower()
    allow_internet = truthy(os.environ.get("RB_SANDBOX_ALLOW_INTERNET"), default=True)
    allow_out = csv_env("RB_SANDBOX_ALLOW_OUT")
    deny_out = csv_env("RB_SANDBOX_DENY_OUT")

    if mode in API_ONLY_MODES:
        # Keep allow_internet_access enabled and express isolation via deny_out
        # so allow_out entries can take precedence over the catch-all deny.
        allow_internet = True
        deny_out = dedupe([ALL_OUTBOUND, *deny_out])

    isolated = mode in API_ONLY_MODES or not allow_internet or ALL_OUTBOUND in deny_out
    if isolated and truthy(os.environ.get("RB_SANDBOX_AUTO_ALLOW_API_HOSTS"), default=True):
        allow_out = dedupe([*allow_out, *auto_api_allow_out()])
    else:
        allow_out = dedupe(allow_out)
    deny_out = dedupe(deny_out)

    network: dict[str, list[str]] = {}
    if allow_out:
        network["allow_out"] = allow_out
    if deny_out:
        network["deny_out"] = deny_out
    return allow_internet, network
