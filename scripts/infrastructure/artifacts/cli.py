"""Small command-line boundary for shell runtimes using an artifact store.

Shell workers should select paths and lifecycle policy; transport SDK calls live here.
Locations are backend-neutral POSIX keys. Provider URIs are translated by the
deployment integration before a platform worker enters RecreationBench.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import TextIO

from core.artifact_store import (
    ArtifactRetryPolicy,
    ArtifactStore,
    normalize_artifact_key,
)

from .factory import artifact_store_from_environment, validate_artifact_environment
from .tree import download_tree, upload_tree

_TRANSFER_RETRY_POLICY = ArtifactRetryPolicy(
    attempts=10,
    retry_statuses=(),
    base_delay_seconds=1.0,
    max_delay_seconds=90.0,
    jitter=(0.5, 1.5),
)


def _key(location: str, environ: Mapping[str, str]) -> str:
    del environ
    return normalize_artifact_key(location)


def transfer(
    mode: str,
    source: str,
    destination: str,
    *,
    store: ArtifactStore,
    environ: Mapping[str, str],
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Execute one transfer while preserving the Android worker's exit contract."""
    if mode == "dl_file":
        key = _key(source, environ)
        store.get_file(key, destination)
        print(f"[artifact] downloaded {source} -> {destination}", file=stdout)
        return 0

    if mode == "dl_dir":
        prefix = _key(source, environ).rstrip("/") + "/"
        result = download_tree(store, prefix, destination)
        print(
            f"[artifact] downloaded {result.transferred} files {source} -> {destination}",
            file=stdout,
        )
        return 0 if result.transferred else 2

    if mode == "ul_file":
        key = _key(destination, environ)
        try:
            store.put_file(key, source)
            print(f"[artifact] uploaded {source} -> {destination}", file=stdout)
        except Exception as exc:  # noqa: BLE001 - best-effort upload is intentional
            print(
                f"[artifact] WARN: upload failed after 10 tries, SKIPPING "
                f"{destination}: {type(exc).__name__} "
                f"status={getattr(exc, 'status', None)}",
                file=stderr,
            )
        return 0

    if mode == "ul_dir":
        prefix = _key(destination, environ).rstrip("/") + "/"
        result = upload_tree(store, source, prefix)
        for relative, exc in result.failures:
            print(f"  WARN: failed to upload {relative}: {exc}", file=stderr)
        print(
            f"[artifact] uploaded {result.transferred} files {source} -> {destination}",
            file=stdout,
        )
        if result.failures:
            print(
                f"[artifact] ERROR: {len(result.failures)} uploads failed",
                file=stderr,
            )
        if result.transferred == 0:
            print("[artifact] ERROR: zero files uploaded", file=stderr)
            return 2
        return 3 if result.failures else 0

    raise ValueError(f"unknown artifact transfer mode: {mode}")


def exists(
    mode: str,
    target: str,
    *,
    store: ArtifactStore,
    environ: Mapping[str, str],
    stdout: TextIO = sys.stdout,
) -> int:
    """Print and return the exact-key or non-empty-prefix existence result."""
    key = _key(target, environ)
    if mode == "key":
        found = store.exists(key)
    elif mode == "prefix":
        prefix = key.rstrip("/") + "/"
        found = any(
            not candidate.endswith("/") for candidate in store.iter_keys(prefix)
        )
    else:
        raise ValueError(f"unknown artifact existence mode: {mode}")
    print("exists" if found else "missing", file=stdout)
    return 0 if found else 1


def put_if_absent(
    source: str,
    target: str,
    *,
    store: ArtifactStore,
    environ: Mapping[str, str],
    stdout: TextIO = sys.stdout,
) -> int:
    """Populate a best-effort shared cache without overwriting an existing object."""
    key = _key(target, environ)
    if store.exists(key):
        print(f"[artifact] cache already populated: {target}", file=stdout)
        return 0
    store.put_file(key, source)
    print(f"[artifact] cache populated: {source} -> {target}", file=stdout)
    return 0


def put_file(
    source: str,
    target: str,
    *,
    store: ArtifactStore,
    environ: Mapping[str, str],
    stdout: TextIO = sys.stdout,
) -> int:
    """Upload one file and propagate every transport failure to the caller."""
    key = _key(target, environ)
    store.put_file(key, source)
    print(f"[artifact] uploaded {source} -> {target}", file=stdout)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    xfer = commands.add_parser("xfer")
    xfer.add_argument("mode", choices=("dl_file", "dl_dir", "ul_file", "ul_dir"))
    xfer.add_argument("source")
    xfer.add_argument("destination")
    probe = commands.add_parser("exists")
    probe.add_argument("mode", choices=("key", "prefix"))
    probe.add_argument("target")
    prime = commands.add_parser("put-if-absent")
    prime.add_argument("source")
    prime.add_argument("target")
    strict_put = commands.add_parser("put-file")
    strict_put.add_argument("source")
    strict_put.add_argument("target")
    commands.add_parser("configured")
    normalize = commands.add_parser("normalize-key")
    normalize.add_argument("value")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "normalize-key":
        try:
            print(normalize_artifact_key(args.value, label="artifact prefix"))
        except ValueError as exc:
            print(f"[artifact] {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "configured":
        try:
            backend = validate_artifact_environment(os.environ)
        except ValueError as exc:
            print(f"[artifact] {exc}", file=sys.stderr)
            return 1
        print(backend)
        return 0
    store = artifact_store_from_environment(
        os.environ,
        retry_policy=_TRANSFER_RETRY_POLICY,
    )
    if args.command == "xfer":
        return transfer(
            args.mode,
            args.source,
            args.destination,
            store=store,
            environ=os.environ,
        )
    if args.command == "exists":
        return exists(
            args.mode,
            args.target,
            store=store,
            environ=os.environ,
        )
    if args.command == "put-file":
        return put_file(
            args.source,
            args.target,
            store=store,
            environ=os.environ,
        )
    return put_if_absent(
        args.source,
        args.target,
        store=store,
        environ=os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
