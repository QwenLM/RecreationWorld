"""Publish a run's metadata and log tree through the configured artifact store.

Linux and Windows once carried 60/56-line inline upload blocks.  Their platform adapters now
call :func:`upload_run` with the few values that differ, while :func:`main` remains as a
compatibility wrapper for older env-driven callers.  One difference matters a lot:

    RB_META_STAGE_NESTED_LOGS   linux writes logs under {base}/logs/{stage}/…, windows writes
                                them flat at {base}/logs/…. Unifying the KEY LAYOUT would orphan
                                every artifact already stored under the other scheme, so the
                                layout stays a per-platform input. The CODE merges; the keys do
                                not move.

    subdirs                     linux uploads logs + pipeline_state, windows only logs
    root_files                  public files written directly under $OUTPUT_DIR (for example
                                metrics.json and trajectory.jsonl)
    stage_copy                  linux also writes metadata_{stage}.json, windows does not
    RB_ARTIFACT_UPLOAD=false    portable upload kill switch

Best-effort by contract: a missing backend, missing config, or a per-file failure prints and
moves on. It runs after the pipeline, so the rb tree is present — unlike the SIGTERM/metrics
last-resort path, which is why that one stays inline in the templates and this one does not.
"""

from __future__ import annotations

import json
import os
import time

from core.artifact_store import ArtifactRetryPolicy, ArtifactStore

from .factory import (
    ArtifactStoreConfigurationError,
    artifact_store_from_environment,
)


def _truthy(value: str, default: bool) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def _subdirs(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    raw = environ.get("RB_META_SUBDIRS", "logs")
    return [p for p in raw.replace(",", " ").split() if p]


def upload_run(
    *,
    task_id: str,
    stage: str,
    model: str,
    exit_code: int,
    output_dir: str,
    subdirs: tuple[str, ...] = ("logs",),
    root_files: tuple[str, ...] = (),
    nested_logs: bool = False,
    stage_copy: bool = False,
    environ=None,
    store: ArtifactStore | None = None,
    prefix: str = "",
) -> int:
    """Upload one run without forcing platform adapters through env-only inputs."""
    environ = os.environ if environ is None else environ
    upload_switch = environ.get("RB_ARTIFACT_UPLOAD", "")
    if not _truthy(upload_switch, True):
        print("Artifact upload disabled")
        return 0

    prefix = (prefix or environ.get("RB_ARTIFACT_PREFIX", "")).rstrip("/")
    if not prefix or not task_id:
        print("Missing artifact prefix/task config, skipping logs upload")
        return 0
    if store is None:
        try:
            store = artifact_store_from_environment(
                environ,
                retry_policy=ArtifactRetryPolicy(
                    attempts=10,
                    retry_statuses=(),
                    base_delay_seconds=1.0,
                    max_delay_seconds=90.0,
                    jitter=(0.5, 1.5),
                ),
            )
        except (ArtifactStoreConfigurationError, ImportError) as exc:
            print(f"Artifact backend unavailable, skipping logs/metadata upload: {exc}")
            return 0

    artifact_base = f"{prefix}/{task_id}"
    meta = {
        "task_id": task_id,
        "run_id": environ.get("RB_RUN_ID", environ.get("JOB_ID", "")),
        "stage": stage,
        "model": model,
        "exit_code": int(exit_code),
        "runtime_commit": environ.get(
            "RB_RUNTIME_COMMIT", environ.get("RB_PIPELINE_COMMIT", "")
        ),
        "integration_commit": environ.get("RB_INTEGRATION_COMMIT", ""),
        # RB_META_NOW exists only so the differential harness can pin the clock; production
        # leaves it unset and gets the real time, exactly as both heredocs did.
        "finished_at": environ.get("RB_META_NOW")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta_json = json.dumps(meta, indent=2).encode()
    store.put_bytes(f"{artifact_base}/metadata.json", meta_json)
    if stage and stage_copy:
        store.put_bytes(f"{artifact_base}/metadata_{stage}.json", meta_json)
    print(f"Uploaded metadata.json to artifact key {artifact_base}/metadata.json")

    count = 0
    # The shared controller writes metrics.json only after the native platform runner returns.
    # Therefore stage uploaders cannot publish it: this finalizer is the first point at which the
    # public result is durable.  Keep the list explicit so arbitrary OUTPUT_DIR files are never
    # leaked to the public result prefix.
    for name in root_files:
        if os.path.basename(name) != name or name in {"", ".", ".."}:
            print(f"  WARN: refusing non-root artifact name {name!r}")
            continue
        fpath = os.path.join(output_dir, name)
        if not os.path.isfile(fpath):
            continue
        try:
            store.put_file(f"{artifact_base}/{name}", fpath)
            count += 1
        except Exception as exc:  # noqa: BLE001 - best effort per file
            print(f"  WARN: upload {name}: {exc}")

    for subdir in subdirs:
        local = os.path.join(output_dir, subdir)
        if not os.path.isdir(local):
            continue
        for root, _dirs, files in os.walk(local):
            for name in files:
                fpath = os.path.join(root, name)
                rel = os.path.relpath(fpath, output_dir)
                if nested_logs and stage and subdir == "logs":
                    rel_in_logs = os.path.relpath(
                        fpath, os.path.join(output_dir, "logs")
                    )
                    key = f"{artifact_base}/logs/{stage}/{rel_in_logs}"
                else:
                    key = f"{artifact_base}/{rel}"
                try:
                    store.put_file(key, fpath)
                    count += 1
                except Exception as exc:  # noqa: BLE001 - best effort per file
                    # SKIP, not raise: a lost log file must never fail a graded run.
                    print(f"  WARN: upload {rel}: {exc}")
    label = "log/state" if len(subdirs) > 1 else "log"
    print(f"Uploaded {count} {label} files under artifact key {artifact_base}/")
    return 0


def main() -> int:
    environ = os.environ
    task_id = environ.get(environ.get("RB_META_TASK_ID_ENV", "TASK_ID"), "")
    return upload_run(
        task_id=task_id,
        stage=environ.get("STAGE", ""),
        model=environ.get("MODEL", ""),
        exit_code=int(environ.get("EXIT_CODE", "-1")),
        output_dir=environ.get("OUTPUT_DIR", "/tmp/output"),
        subdirs=tuple(_subdirs(environ)),
        root_files=tuple(
            p
            for p in environ.get("RB_META_ROOT_FILES", "").replace(",", " ").split()
            if p
        ),
        nested_logs=_truthy(environ.get("RB_META_STAGE_NESTED_LOGS", ""), False),
        stage_copy=_truthy(environ.get("RB_META_STAGE_COPY", ""), False),
        environ=environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
