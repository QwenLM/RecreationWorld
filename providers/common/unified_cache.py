#!/usr/bin/env python3
"""Shared unified benchmark input archive helpers for sandbox templates."""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path


def task_id_candidates(task_id: str) -> list[str]:
    """Return accepted cache/app names for a user-facing task id.

    The wrapsource release uses prefix-free Ubuntu app names. Keep the older
    ``bench50-`` spelling as a host-side compatibility alias.
    """

    task_id = task_id.strip().strip("/")
    bare = task_id.removeprefix("bench50-")
    candidates = [bare, task_id]
    if "-" in bare:
        candidates.append(bare.rsplit("-", 1)[0])
    if not task_id.startswith("bench50-"):
        candidates.append(f"bench50-{task_id}")
    ordered: list[str] = []
    for item in candidates:
        if item and item not in ordered:
            ordered.append(item)
    return ordered


def _member_names(archive: Path) -> set[str]:
    with tarfile.open(archive, mode="r:gz") as tar:
        return {member.name.removeprefix("./").rstrip("/") for member in tar.getmembers()}


def validate_unified_archive(archive: Path, *, task_id: str, platform: str) -> str:
    """Validate and return the app directory contained in a unified archive."""

    if not archive.is_file():
        raise RuntimeError(f"unified archive not found: {archive}")
    members = _member_names(archive)
    for app in task_id_candidates(task_id):
        if f"{platform}/{app}/instance.json" in members:
            return app
    expected = " or ".join(f"{platform}/{app}/instance.json" for app in task_id_candidates(task_id))
    raise RuntimeError(f"unified archive does not contain {expected}: {archive}")


def _looks_like_unified_dir(path: Path) -> bool:
    return (
        (path / "instance.json").is_file()
        and (path / "reference").is_dir()
        and (path / "tests").is_dir()
    )


def _cache_dir_candidates(cache_dir: Path, *, task_id: str, platform: str) -> list[Path]:
    candidates: list[Path] = []
    for app in task_id_candidates(task_id):
        candidates.extend(
            [
                cache_dir / platform / app,
                cache_dir / app,
                cache_dir / f"{app}.{platform}",
            ]
        )
    return candidates


def _cache_archive_candidates(cache_dir: Path, *, task_id: str, platform: str) -> list[Path]:
    candidates: list[Path] = []
    for app in task_id_candidates(task_id):
        candidates.extend(
            [
                cache_dir / f"{app}.{platform}.tar.gz",
                cache_dir / f"{app}.{platform}.tgz",
                cache_dir / platform / f"{app}.tar.gz",
                cache_dir / platform / f"{app}.tgz",
                cache_dir / f"{app}.tar.gz",
                cache_dir / f"{app}.tgz",
            ]
        )
    return candidates


def find_cached_unified(
    *,
    task_id: str,
    platform: str,
    archive: Path | None = None,
    cache_dir: Path | None = None,
) -> Path | None:
    """Find a configured unified cache archive or directory."""

    if archive is not None:
        validate_unified_archive(archive, task_id=task_id, platform=platform)
        return archive
    if cache_dir is None:
        return None
    cache_dir = cache_dir.expanduser()
    if not cache_dir.exists():
        raise RuntimeError(f"unified cache dir not found: {cache_dir}")
    if cache_dir.is_file():
        validate_unified_archive(cache_dir, task_id=task_id, platform=platform)
        return cache_dir
    for candidate in _cache_archive_candidates(cache_dir, task_id=task_id, platform=platform):
        if candidate.is_file():
            try:
                validate_unified_archive(candidate, task_id=task_id, platform=platform)
            except (OSError, tarfile.TarError, RuntimeError):
                # A previous controller can be interrupted while writing a
                # cache file.  Ignore that stale candidate; the caller writes
                # a validated replacement atomically.
                continue
            return candidate
    for candidate in _cache_dir_candidates(cache_dir, task_id=task_id, platform=platform):
        if _looks_like_unified_dir(candidate):
            return candidate
    return None


def pack_unified_directory(
    task_dir: Path,
    *,
    task_id: str,
    platform: str,
    archive: Path,
) -> Path:
    """Pack a cached unified task directory as ``<platform>/<task-id>/...``."""

    if not _looks_like_unified_dir(task_dir):
        raise RuntimeError(f"incomplete unified cache dir: {task_dir}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rb_unified_cache_pack_", dir=archive.parent) as tmp:
        tmp_path = Path(tmp)
        app = task_dir.name
        # If cache was found through an alias directory, keep the canonical
        # task id in the mirror so eval_bridge can resolve default task ids.
        if app not in task_id_candidates(task_id):
            app = task_id
        partial_archive = tmp_path / "unified.tar.gz"
        with tarfile.open(partial_archive, mode="w:gz") as tar:
            tar.add(task_dir, arcname=f"{platform}/{app}")
        validate_unified_archive(partial_archive, task_id=task_id, platform=platform)
        partial_archive.replace(archive)
    return archive


def prepare_unified_archive(
    *,
    task_id: str,
    platform: str,
    archive: Path | None,
    cache_dir: Path | None,
    output_archive: Path,
) -> Path:
    """Return a local unified input archive ready for sandbox upload.

    Public controllers deliberately accept only files already present on the
    caller's machine.  Dataset acquisition belongs outside the evaluation
    runtime, so this module never imports a cloud SDK or reads provider
    credentials.
    """

    cached = find_cached_unified(
        task_id=task_id, platform=platform, archive=archive, cache_dir=cache_dir
    )
    if cached is not None:
        if cached.is_file():
            return cached
        return pack_unified_directory(
            cached, task_id=task_id, platform=platform, archive=output_archive
        )
    raise RuntimeError(
        f"local unified input not found for {task_id} platform={platform}; "
        "provide --unified-archive or --unified-cache-dir"
    )
