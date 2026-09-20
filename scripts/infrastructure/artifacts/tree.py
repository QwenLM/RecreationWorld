"""Provider-neutral directory transfer helpers for artifact stores."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.artifact_store import ArtifactStore, Pathish


@dataclass(frozen=True)
class TreeTransferResult:
    transferred: int
    skipped: int = 0
    failures: tuple[tuple[str, Exception], ...] = ()

    @property
    def complete(self) -> bool:
        return self.transferred > 0 and not self.failures


def _prefix(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("artifact prefix must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe artifact prefix: {value!r}")
    return value.rstrip("/") + "/"


def _relative_key(key: str, prefix: str) -> str:
    if not key.startswith(prefix):
        raise ValueError(f"artifact store returned {key!r} outside prefix {prefix!r}")
    relative = key[len(prefix) :]
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError(f"unsafe key below artifact prefix: {key!r}")
    return relative


def download_tree(
    store: ArtifactStore,
    prefix: str,
    destination: Pathish,
    *,
    exclude_prefixes: Iterable[str] = (),
    executable: Callable[[str], bool] | None = None,
) -> TreeTransferResult:
    """Download every file below a prefix, failing on any transfer error."""
    normalized = _prefix(prefix)
    excluded = tuple(exclude_prefixes)
    root = Path(destination)
    transferred = 0
    skipped = 0
    for key in store.iter_keys(normalized):
        if key.endswith("/"):
            continue
        relative = _relative_key(key, normalized)
        if any(relative == item or relative.startswith(item) for item in excluded):
            skipped += 1
            continue
        target = root.joinpath(*PurePosixPath(relative).parts)
        store.get_file(key, target)
        if executable is not None and executable(relative):
            target.chmod(0o755)
        transferred += 1
    return TreeTransferResult(transferred=transferred, skipped=skipped)


def upload_tree(
    store: ArtifactStore,
    source: Pathish,
    prefix: str,
) -> TreeTransferResult:
    """Upload all files below a directory and report per-file failures."""
    normalized = _prefix(prefix)
    root = Path(source)
    transferred = 0
    failures: list[tuple[str, Exception]] = []
    if not root.is_dir():
        return TreeTransferResult(transferred=0)
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        relative = path.relative_to(root).as_posix()
        try:
            store.put_file(normalized + relative, path)
            transferred += 1
        except (
            Exception
        ) as exc:  # noqa: BLE001 - caller chooses strict/best-effort policy
            failures.append((relative, exc))
    return TreeTransferResult(transferred=transferred, failures=tuple(failures))
