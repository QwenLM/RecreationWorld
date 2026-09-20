"""Shared desktop route for controller-reachable VLM judge endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit


@contextmanager
def desktop_vlm_base_url(client, base_url: str, *, enabled: bool = True):
    """Yield the judge URL visible inside an SSH guest.

    Ordinary ``http`` and ``https`` URLs keep direct endpoint semantics.  An
    explicit ``ssh+http`` URL means that the controller can reach the endpoint
    but the guest cannot: expose it on a guest loopback port over the existing
    SSH transport while preserving the upstream path.
    """
    value = (base_url or "").strip().rstrip("/")
    if not enabled or not value:
        yield value
        return

    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        yield value
        return
    if parsed.scheme != "ssh+http":
        raise ValueError(
            "VLM base URL scheme must be http, https, or ssh+http; "
            f"got {parsed.scheme!r}"
        )
    if parsed.username or parsed.password:
        raise ValueError("VLM base URL must not contain credentials")
    if not parsed.hostname:
        raise ValueError(f"VLM base URL has no host: {value!r}")
    with client.remote_forward(parsed.hostname, parsed.port or 80) as guest_port:
        guest_url = urlunsplit(
            (
                "http",
                f"127.0.0.1:{guest_port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        ).rstrip("/")
        yield guest_url


__all__ = ["desktop_vlm_base_url"]
