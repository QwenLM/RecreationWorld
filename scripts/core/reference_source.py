#!/usr/bin/env python3
"""Prepare a reference source tree from one frozen RB instance.

The release contract is clone-first: materialize the repository at the descriptor's
pinned commit, including nested submodules.  A packaged ``reference.tar.gz``, when
present in an older bundle, is a fallback for source-host/network failures.  Declared
frozen patches are then verified and applied here, so every source-backed platform
consumes the exact same tree.

This module is intentionally portable and is invoked inside every source-backed runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

CLONE_MODE = "clone"
FALLBACK_MODE = "frozen_fallback"


def _remove_tree(path: Path) -> None:
    """Remove a checkout even when Windows marked Git files read-only."""

    def make_writable_and_retry(func, name, _exc_info):
        os.chmod(name, stat.S_IWRITE)
        func(name)

    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)


def _git(source: Path, *args: str, check: bool = True, capture: bool = False):
    return subprocess.run(
        ["git", "-C", str(source), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _rewrite_github_ssh_submodules(source: Path) -> None:
    """Match Linux's conversion of common private-SSH GitHub URL forms."""

    result = _git(
        source,
        "config",
        "--get-regexp",
        r"^submodule\..*\.url",
        check=False,
        capture=True,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(" ")
        if not sep:
            continue
        if value.startswith("git@github.com:"):
            value = "https://github.com/" + value.removeprefix("git@github.com:")
        elif value.startswith("ssh://git@github.com/"):
            value = "https://github.com/" + value.removeprefix("ssh://git@github.com/")
        else:
            continue
        _git(source, "config", key, value)


def _clone_at_commit(repo: str, commit: str, source: Path) -> None:
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "remote", "add", "origin", repo)
    _git(source, "fetch", "-q", "--depth", "1", "origin", commit)
    _git(source, "checkout", "-q", "FETCH_HEAD")
    actual_commit = _git(source, "rev-parse", "HEAD", capture=True).stdout.strip()
    if actual_commit != commit:
        raise RuntimeError(f"checkout resolved to {actual_commit}, expected {commit}")

    if not (source / ".gitmodules").is_file():
        return

    _git(source, "submodule", "init")
    _rewrite_github_ssh_submodules(source)
    shallow = _git(
        source,
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--depth",
        "1",
        check=False,
    )
    if shallow.returncode:
        _git(source, "submodule", "update", "--init", "--recursive")

    status = _git(source, "submodule", "status", "--recursive", capture=True).stdout.splitlines()
    if any(line.startswith("-") for line in status):
        raise RuntimeError("cloned source still has uninitialized submodules")


def _extract_frozen(archive: Path, source: Path) -> None:
    _remove_tree(source)
    source.mkdir(parents=True)
    subprocess.run(
        ["tar", "xzf", str(archive), "-C", str(source)],
        check=True,
    )


def _apply_declared_patches(descriptor_path: Path, descriptor: dict, source: Path) -> None:
    """Apply only safe patch paths declared by the frozen descriptor, in order."""

    for item in descriptor.get("patches") or []:
        if not isinstance(item, dict):
            raise ValueError(f"invalid patch descriptor: {item!r}")
        rel = PurePosixPath(str(item.get("path") or ""))
        if (
            rel.is_absolute()
            or ".." in rel.parts
            or rel.parts[:2] != ("reference", "patches")
            or len(rel.parts) < 3
        ):
            raise ValueError(f"unsafe patch path: {rel}")
        patch = descriptor_path.parent.joinpath(*rel.parts)
        if not patch.is_file():
            raise FileNotFoundError(f"declared patch missing: {rel}")
        expected_sha256 = str(item.get("sha256") or "")
        if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
            raise ValueError(f"declared patch has invalid sha256: {rel}")
        actual_sha256 = hashlib.sha256(patch.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"declared patch checksum mismatch for {rel}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=source,
            check=True,
        )


def prepare_reference_source(
    descriptor_path: str | Path,
    source_dir: str | Path,
    archive_path: str | Path | None = None,
) -> str:
    """Materialize source and return ``clone`` or ``frozen_fallback``."""

    descriptor_file = Path(descriptor_path)
    descriptor = json.loads(descriptor_file.read_text(encoding="utf-8-sig"))
    repo = str(descriptor.get("repo") or "")
    commit = str(descriptor.get("commit") or "")
    source = Path(source_dir)
    archive = Path(archive_path) if archive_path else None

    _remove_tree(source)
    try:
        if not repo.startswith(("http://", "https://")):
            raise ValueError("descriptor repo must be an HTTP(S) URL")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("descriptor commit must be a full lowercase SHA-1")
        _clone_at_commit(repo, commit, source)
    except Exception as exc:
        if archive is None or not archive.is_file():
            raise RuntimeError(
                f"clone of {repo or '<none>'}@{commit or '<none>'} failed and "
                "this source-only release has no packaged fallback"
            ) from exc
        print(
            f"WARNING: clone of {repo or '<none>'}@{commit or '<none>'} failed: "
            f"{exc}; using packaged source",
            flush=True,
        )
        _extract_frozen(archive, source)
        mode = FALLBACK_MODE
    else:
        mode = CLONE_MODE

    _apply_declared_patches(descriptor_file, descriptor, source)
    print(f"Source prepared by {mode} at {commit or '<none>'}", flush=True)
    print(f"SOURCE_MODE={mode}", flush=True)
    return mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--archive")
    args = parser.parse_args(argv)
    prepare_reference_source(args.descriptor, args.source_dir, args.archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
