"""Resolve bundled RecreationBench runtime sources for sandbox templates."""

from __future__ import annotations

import copy
import os
import tarfile
from pathlib import Path, PurePosixPath


PLATFORM_ALIASES = {"ubuntu": "linux"}
SELF_CONTAINED_PLATFORMS = {"android", "linux", "macos", "web", "windows"}
REQUIRED_RUNTIME_FILES = {
    "linux": (
        "scripts/core/pipeline.py",
        "scripts/platforms/linux/pipeline.py",
        "scripts/linux/worker.py",
    ),
    "web": (
        "scripts/core/pipeline.py",
        "scripts/platforms/web/pipeline.py",
        "scripts/web/runner/run_agent.py",
        "scripts/web/evaluation/evaluator.py",
    ),
    "windows": (
        "scripts/core/pipeline.py",
        "scripts/platforms/windows/pipeline.py",
        "scripts/windows/vm_runtime.py",
        "scripts/windows/worker.py",
    ),
    "android": (
        "scripts/core/pipeline.py",
        "scripts/platforms/android/pipeline.py",
        "scripts/platforms/android/worker.sh",
    ),
    "macos": (
        "scripts/core/pipeline.py",
        "scripts/platforms/macos/pipeline.py",
        "scripts/platforms/macos/vm_runtime.py",
    ),
}

# A controller only needs executable runtime sources on the target.  Keeping this
# list deliberately small prevents local credentials, result directories, build
# products, and editor backups from being copied merely because they happen to sit
# beside the checkout.
RUNTIME_ARCHIVE_ROOTS = (
    "pyproject.toml",
    "scripts",
    "src",
)
RUNTIME_ARCHIVE_EXCLUDED_PARTS = {
    ".git",
    ".local-backups",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "results",
    "tests",
    "venv",
}
RUNTIME_ARCHIVE_EXCLUDED_FILES = {".coverage", ".DS_Store"}
RUNTIME_ARCHIVE_BACKUP_SUFFIXES = (".bak", ".orig", ".pyc", ".pyo", ".rej", "~")
DEFAULT_MAX_EXTRACTED_BYTES = 20 * 1024**3
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000


def _archive_parts(name: str, *, allow_parent: bool = False) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if (
        path.is_absolute()
        or "\\" in name
        or any(":" in part for part in parts)
        or (not allow_parent and ".." in parts)
    ):
        raise RuntimeError(f"refusing unsafe archive path: {name!r}")
    return parts


def safe_extract_tar(
    archive_path: Path,
    destination: Path,
    *,
    strip_components: int = 0,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
) -> None:
    """Extract a gzip tar, allowing only links within the destination tree.

    Result bundles can contain files produced by the evaluated agent. Keep that
    boundary safe on Python 3.9+, where the standard-library ``data`` extraction
    filter is not consistently available.
    """

    if strip_components < 0:
        raise ValueError("strip_components must be non-negative")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    safe_members: list[tarfile.TarInfo] = []
    extracted_bytes = 0
    current_uid = os.getuid() if hasattr(os, "getuid") else 0
    current_gid = os.getgid() if hasattr(os, "getgid") else 0
    by_path: dict[tuple[str, ...], tarfile.TarInfo] = {}

    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > max_members:
            raise RuntimeError(
                f"refusing archive with {len(members)} members (limit: {max_members})"
            )
        for member in members:
            raw_parts = _archive_parts(member.name)
            if member.isdev():
                raise RuntimeError(f"refusing unsafe archive entry: {member.name!r}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise RuntimeError(f"refusing unsupported archive entry: {member.name!r}")
            if len(raw_parts) <= strip_components:
                continue

            relative = PurePosixPath(*raw_parts[strip_components:])
            target = (root / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"refusing unsafe archive path: {member.name!r}")
            for length in range(1, len(relative.parts) + 1):
                if root.joinpath(*relative.parts[:length]).is_symlink():
                    raise RuntimeError(f"refusing existing symlink at archive path: {member.name!r}")

            extracted_bytes += max(0, member.size)
            if extracted_bytes > max_extracted_bytes:
                raise RuntimeError(
                    "refusing archive whose declared extracted size exceeds "
                    f"{max_extracted_bytes} bytes"
                )

            safe_member = copy.copy(member)
            safe_member.name = relative.as_posix()
            safe_member.uid = current_uid
            safe_member.gid = current_gid
            safe_member.uname = ""
            safe_member.gname = ""
            safe_member.mode &= 0o777
            if relative.parts in by_path:
                if member.isdir() and by_path[relative.parts].isdir():
                    continue
                raise RuntimeError(f"refusing duplicate archive path: {member.name!r}")
            by_path[relative.parts] = safe_member
            safe_members.append(safe_member)

        # Materialize files before links. No archived path may traverse another
        # archived file/link, so extraction never writes through an incoming link.
        for parts in by_path:
            for length in range(1, len(parts)):
                parent = by_path.get(parts[:length])
                if parent is not None and not parent.isdir():
                    raise RuntimeError(f"refusing non-directory archive parent: {parent.name!r}")

        def resolve_symbolic(parts: tuple[str, ...], active: tuple = ()) -> tuple[str, ...]:
            resolved: tuple[str, ...] = ()
            for part in parts:
                if part == "..":
                    if not resolved:
                        raise RuntimeError("refusing archive link outside destination")
                    resolved = resolved[:-1]
                    continue
                resolved += (part,)
                entry = by_path.get(resolved)
                if entry is not None and entry.issym():
                    if resolved in active or len(active) >= 40:
                        raise RuntimeError(f"refusing cyclic or excessive archive links: {entry.name!r}")
                    resolved = resolve_symbolic(
                        resolved[:-1] + _archive_parts(entry.linkname, allow_parent=True),
                        active + (resolved,),
                    )
            return resolved

        links: list[tuple[tarfile.TarInfo, str]] = []
        for parts, member in by_path.items():
            if member.issym():
                target_parts = resolve_symbolic(parts)
                for length in range(1, len(target_parts) + 1):
                    if root.joinpath(*target_parts[:length]).is_symlink():
                        raise RuntimeError(f"refusing link through existing symlink: {member.name!r}")
                target = root.joinpath(*target_parts).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"refusing archive link outside destination: {member.name!r}")
                links.append((member, member.linkname))
            elif member.islnk():
                target_parts = _archive_parts(member.linkname)[strip_components:]
                visited = {parts}
                while True:
                    entry = by_path.get(target_parts)
                    if entry is None or target_parts in visited or len(visited) >= 40:
                        raise RuntimeError(f"refusing missing or cyclic hard link: {member.name!r}")
                    if entry.isfile():
                        break
                    if not entry.islnk():
                        raise RuntimeError(f"refusing hard link to non-file: {member.name!r}")
                    visited.add(target_parts)
                    target_parts = _archive_parts(entry.linkname)[strip_components:]
                links.append((member, str(root.joinpath(*target_parts))))

        regular_members = []
        directories = []
        for member in safe_members:
            if member.isdir():
                directories.append(member)
                writable = copy.copy(member)
                writable.mode |= 0o700
                regular_members.append(writable)
            elif member.isfile():
                regular_members.append(member)
        try:
            archive.extractall(root, members=regular_members, numeric_owner=False)
            for member, link_target in links:
                path = root / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_file() or path.is_symlink():
                    path.unlink()
                if member.issym():
                    target = root.joinpath(*resolve_symbolic(PurePosixPath(member.name).parts))
                    os.symlink(link_target, path, target_is_directory=target.is_dir())
                else:
                    os.link(link_target, path)
        finally:
            # A non-root controller must still be able to add links to archived
            # read-only directories. Restore their metadata only after all children.
            for member in sorted(directories, key=lambda item: item.name, reverse=True):
                path = root / member.name
                if path.is_dir():
                    os.chmod(path, member.mode)
                    os.utime(path, (member.mtime, member.mtime))


def runtime_archive_filter(
    info: tarfile.TarInfo, *, archive_root: str = ""
) -> tarfile.TarInfo | None:
    """Keep only runtime inputs and reject local state from a source archive."""

    parts = tuple(part for part in PurePosixPath(info.name).parts if part not in {"", "."})
    if archive_root and parts[:1] == (archive_root,):
        parts = parts[1:]
    if not parts:
        return info
    if parts[0] not in RUNTIME_ARCHIVE_ROOTS:
        return None
    if parts[:2] == ("scripts", "release"):
        return None
    if any(
        part in RUNTIME_ARCHIVE_EXCLUDED_PARTS or part.endswith(".egg-info")
        for part in parts
    ):
        return None
    if any(part.startswith(".env") for part in parts):
        return None
    if parts[-1] in RUNTIME_ARCHIVE_EXCLUDED_FILES or parts[-1].endswith(
        RUNTIME_ARCHIVE_BACKUP_SUFFIXES
    ):
        return None
    if info.issym() or info.islnk():
        return None
    return info


def add_runtime_source(
    archive: tarfile.TarFile, source_dir: Path, *, archive_root: str = ""
) -> None:
    """Add the curated runtime tree to ``archive`` under an optional root."""

    source_dir = source_dir.resolve()
    for name in RUNTIME_ARCHIVE_ROOTS:
        source = source_dir / name
        if not source.exists():
            continue
        arcname = f"{archive_root}/{name}" if archive_root else name
        archive.add(
            source,
            arcname=arcname,
            filter=lambda info: runtime_archive_filter(info, archive_root=archive_root),
        )


def bundled_runtime_dir(service_root: Path) -> Path:
    """Return the runtime at the root of the unified public repository."""

    return service_root.parent


def validate_runtime_source_dir(source_dir: Path, *, platform: str) -> Path:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise RuntimeError(f"RecreationBench runtime source directory not found: {source_dir}")

    requested_platform = platform.strip().lower()
    platform_key = PLATFORM_ALIASES.get(requested_platform, requested_platform)
    required = REQUIRED_RUNTIME_FILES.get(platform_key)
    if required:
        missing = [rel for rel in required if not (source_dir / rel).is_file()]
        if missing:
            missing_text = ", ".join(missing)
            raise RuntimeError(
                f"RecreationBench runtime source is incomplete for platform={requested_platform}: "
                f"{source_dir}; missing: {missing_text}"
            )
    return source_dir


def resolve_runtime_source_dir(
    *,
    service_root: Path,
    platform: str,
    source_dir: Path | None = None,
) -> Path:
    """Return the explicit source dir or the bundled service runtime.

    Public provider packages must be self-contained.  Do not fall back to a
    sibling checkout; that hides packaging bugs and makes reproduction depend
    on local workspace layout.
    """

    platform_key = platform.strip().lower()
    platform_key = PLATFORM_ALIASES.get(platform_key, platform_key)
    if source_dir is not None:
        return validate_runtime_source_dir(source_dir, platform=platform_key)
    if platform_key in SELF_CONTAINED_PLATFORMS:
        return validate_runtime_source_dir(bundled_runtime_dir(service_root), platform=platform_key)
    return validate_runtime_source_dir(bundled_runtime_dir(service_root), platform=platform_key)
