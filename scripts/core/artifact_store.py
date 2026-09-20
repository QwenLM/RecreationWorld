"""Backend-neutral object storage contract used by RecreationBench.

Benchmark code deals only in opaque, POSIX-style object keys.  Authentication,
service endpoints, multipart uploads, and provider-specific retries belong to the
infrastructure adapter that implements this protocol.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from os import PathLike
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

Pathish = str | PathLike[str]


@dataclass(frozen=True)
class ArtifactRetryPolicy:
    """Backend-neutral retry budget supplied at the composition boundary."""

    attempts: int = 10
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)
    retry_server_errors: bool = True
    retry_transport_errors: bool = True
    base_delay_seconds: float = 0.75
    max_delay_seconds: float = 60.0
    jitter: tuple[float, float] = (0.7, 1.3)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if (
            len(self.jitter) != 2
            or self.jitter[0] < 0
            or self.jitter[1] < self.jitter[0]
        ):
            raise ValueError("jitter must be an ordered non-negative pair")


def normalize_artifact_key(value: str, *, label: str = "artifact key") -> str:
    """Validate and normalize one provider-neutral object key or prefix."""
    key = str(value).strip().rstrip("/")
    if not key or "\\" in key or "://" in key:
        raise ValueError(f"{label} must be a non-empty POSIX path")
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe {label}: {value!r}")
    return key


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal transport needed by runtime and release artifact paths."""

    def get_file(self, key: str, destination: Pathish) -> None:
        """Download ``key`` to ``destination``, replacing an existing file."""

    def get_bytes(self, key: str) -> bytes:
        """Return the complete contents of ``key``."""

    def put_file(self, key: str, source: Pathish) -> None:
        """Upload the complete contents of ``source`` to ``key``."""

    def put_bytes(self, key: str, data: bytes) -> None:
        """Upload ``data`` to ``key``."""

    def iter_keys(self, prefix: str) -> Iterable[str]:
        """Yield object keys beginning with ``prefix``."""

    def exists(self, key: str) -> bool:
        """Return whether the exact object ``key`` exists."""


@runtime_checkable
class ArtifactMetadataStore(ArtifactStore, Protocol):
    """Optional extension used by reporting tools that enforce freshness cutoffs."""

    def last_modified(self, key: str) -> datetime | None:
        """Return an aware modification time, or ``None`` when ``key`` is absent."""
