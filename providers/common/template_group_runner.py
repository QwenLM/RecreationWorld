#!/usr/bin/env python3
"""Shared group runner for RecreationBench sandbox template repros.

Each task is executed by the platform-specific ``run_one.py`` in an independent
sandbox.  This module owns only the host-side orchestration and result layout.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from common.dotenv_loader import load_dotenv_quiet
from common.unified_cache import prepare_unified_archive


@dataclass
class TaskSpec:
    """One user-facing group entry.

    ``task_id`` is the released dataset app id. Ubuntu IDs are prefix-free in the
    wrapsource release; the older ``bench50-`` spelling remains a compatibility
    alias at the local cache boundary.
    """

    task_id: str
    model: str = ""
    agent_cli: str = ""
    mcp_provider: str = ""
    timeout_sec: int | None = None
    template: str = ""
    result_dir: str = ""
    recreated_app_dir: str = ""
    visual_only: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


def _task_spec_from_mapping(raw: dict[str, Any], *, source: str) -> TaskSpec:
    task_id = str(raw.get("task_id") or raw.get("id") or raw.get("instance_id") or "").strip()
    if not task_id:
        raise ValueError(f"{source}: JSONL entry requires task_id, id, or instance_id")

    extra_env = raw.get("env") or raw.get("extra_env") or {}
    if not isinstance(extra_env, dict):
        raise ValueError(f"{source}: env/extra_env must be an object")

    timeout_raw = raw.get("timeout_sec")
    timeout_sec = int(timeout_raw) if timeout_raw not in (None, "") else None
    return TaskSpec(
        task_id=task_id,
        model=str(raw.get("model") or "").strip(),
        agent_cli=str(raw.get("agent_cli") or "").strip(),
        mcp_provider=str(raw.get("mcp_provider") or "").strip(),
        timeout_sec=timeout_sec,
        template=str(raw.get("template") or "").strip(),
        result_dir=str(raw.get("result_dir") or "").strip(),
        recreated_app_dir=str(raw.get("recreated_app_dir") or raw.get("recreation_dir") or "").strip(),
        visual_only=bool(raw.get("visual_only", False)),
        extra_env={str(k): str(v) for k, v in extra_env.items() if v is not None},
    )


def _task_spec_from_token(token: str, *, source: str) -> TaskSpec:
    task_id = token.strip()
    if not task_id:
        raise ValueError(f"{source}: empty task id")
    return TaskSpec(task_id=task_id)


def parse_task_specs(args: argparse.Namespace) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for item in args.task_id:
        for part in item.split(","):
            part = part.strip()
            if part:
                tasks.append(_task_spec_from_token(part, source="--task-id"))
    if args.tasks_file:
        for lineno, line in enumerate(args.tasks_file.read_text().splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            source = f"{args.tasks_file}:{lineno}"
            if line.startswith("{"):
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError(f"{source}: JSONL entry must be an object")
                tasks.append(_task_spec_from_mapping(parsed, source=source))
            else:
                tasks.append(_task_spec_from_token(line.split()[0], source=source))

    seen: set[str] = set()
    ordered: list[TaskSpec] = []
    for task in tasks:
        if task.task_id not in seen:
            seen.add(task.task_id)
            ordered.append(task)
    return ordered


def extract_metrics(output: str) -> dict:
    marker = "metrics.json:"
    if marker not in output:
        return {}
    tail = output.rsplit(marker, 1)[-1].strip()
    start = tail.find("{")
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(tail[start:])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


FAILURE_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "host_disk_full",
        [
            r"No space left on device",
            r"Errno 28",
            r"ENOSPC",
            r"insufficient free space",
        ],
    ),
    (
        "infra_network",
        [
            r"RemoteProtocolError",
            r"Server disconnected",
            r"Command ended without an end event",
            r"ConnectError",
            r"nodename nor servname provided",
            r"Name or service not known",
            r"Temporary failure in name resolution",
            r"ReadTimeout",
            r"ConnectTimeout",
            r"WriteTimeout",
            r"PoolTimeout",
            r"httpcore\.",
            r"httpx\.",
            r"e2b\.",
            r"Failed to create sandbox",
            r"Sandbox\.create",
        ],
    ),
    (
        "input_config",
        [
            r"AccessDenied",
            r"Access denied by bucket policy",
            r"missing environment variable:",
            r"unified instance not found",
            r"unified cache (dir not found|miss)",
            r"unified archive (not found|does not contain)",
        ],
    ),
    ("env_missing_dependency", [r'Unknown option: "appstream:svg-support"', r"Run-time dependency .* found: NO"]),
    ("runtime_permission", [r"bwrap: Creating new namespace failed: Operation not permitted"]),
    ("agent_artifact_missing", [r"recreation/build\.sh missing", r"could not find required recreation artifact"]),
    ("eval_failed", [r"ABORT: eval failed", r"eval failed", r"stage:eval_failed"]),
    ("recreation_failed", [r"ABORT: recreation failed", r"stage:recreation_failed", r"recreation failed"]),
]


def classify_failure(output: str, metrics: dict | None, *, exit_code: int, error: str = "") -> tuple[str, bool, str]:
    """Return (failure_category, retryable, last_error)."""

    metrics = metrics or {}
    if exit_code == 0 and metrics.get("task_score") is not None:
        return "scored", False, ""
    metric_category = str(metrics.get("failure_category") or "").strip()
    if metric_category:
        return metric_category, metric_category == "infra_network", str(
            metrics.get("error") or metrics.get("exit_reason") or error or ""
        )[:240]

    text = "\n".join(
        part
        for part in [
            output or "",
            error or "",
            str(metrics.get("exit_code") or ""),
            str(metrics.get("error") or ""),
            str(metrics.get("exit_reason") or ""),
        ]
        if part
    )
    for category, patterns in FAILURE_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return category, category == "infra_network", match.group(0)[:240]

    if not metrics:
        return "no_metrics", False, error[:240] if error else ""
    if metrics.get("passed") is False:
        return "recreation_failed", False, str(metrics.get("error") or metrics.get("exit_reason") or "")[:240]
    return "unknown", False, error[:240] if error else ""


def build_group_metrics(group_id: str, platform: str, results: list[dict]) -> dict:
    scored = []
    failed = []
    excluded = []
    for result in results:
        metrics = result.get("metrics") or {}
        if metrics.get("excluded_from_scoring"):
            excluded.append(result)
        elif result.get("exit_code") == 0 and metrics.get("task_score") is not None:
            scored.append(result)
        else:
            failed.append(result)

    def nums(key: str) -> list[float]:
        out = []
        for result in scored:
            value = (result.get("metrics") or {}).get(key)
            if isinstance(value, (int, float)):
                out.append(float(value))
        return out

    attempted = [result for result in results if result not in excluded]
    task_scores_with_failures = []
    for result in attempted:
        value = (result.get("metrics") or {}).get("task_score")
        task_scores_with_failures.append(float(value) if isinstance(value, (int, float)) else 0.0)

    return {
        "group_id": group_id,
        "platform": platform,
        "task_count": len(results),
        "attempted_count": len(attempted),
        "scored_count": len(scored),
        "failed_count": len(failed),
        "excluded_count": len(excluded),
        "avg_task_score": average(nums("task_score")),
        "avg_task_score_with_failures": average(task_scores_with_failures),
        "avg_program_score": average(nums("program_score")),
        "avg_vlm_score": average(nums("vlm_score")),
        "avg_prog_vlm_avg": average(nums("prog_vlm_avg")),
        "tasks": [
            {
                "task_id": result["task_id"],
                "exit_code": result["exit_code"],
                "elapsed_sec": result["elapsed_sec"],
                "task_score": (result.get("metrics") or {}).get("task_score"),
                "program_score": (result.get("metrics") or {}).get("program_score"),
                "vlm_score": (result.get("metrics") or {}).get("vlm_score"),
                "prog_vlm_avg": (result.get("metrics") or {}).get("prog_vlm_avg"),
                "passed": (result.get("metrics") or {}).get("passed"),
                "score_passed": (result.get("metrics") or {}).get("score_passed"),
                "excluded_from_scoring": (result.get("metrics") or {}).get("excluded_from_scoring"),
                "attempt": result.get("attempt"),
                "retry_count": result.get("retry_count", 0),
                "failure_category": result.get("failure_category", ""),
                "retryable": result.get("retryable", False),
                "last_error": result.get("last_error", ""),
                "metrics_path": result.get("metrics_path", ""),
                "log_path": result.get("log_path", ""),
                "error": result.get("error", ""),
            }
            for result in sorted(results, key=lambda item: item["task_id"])
        ],
    }


def append_if_value(cmd: list[str], flag: str, value: str | None) -> None:
    if value:
        cmd.extend([flag, value])


def _cache_archive_path(cache_dir: Path, task_id: str, platform: str) -> Path:
    # Task ids are normally DNS-like names.  Keep cache paths flat and avoid a
    # malformed group file escaping the configured cache directory.
    safe_task_id = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._") or "task"
    return cache_dir / f"{safe_task_id}.{platform}.tar.gz"


def _prefetch_failure(task: TaskSpec, out_dir: Path, error: str) -> dict:
    task_dir = out_dir / "tasks" / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / "prefetch.log"
    log_path.write_text(error + "\n")
    category, retryable, last_error = classify_failure("", {}, exit_code=1, error=error)
    return {
        "task_id": task.task_id,
        "exit_code": 1,
        "elapsed_sec": 0,
        "attempt": 0,
        "task_dir": str(task_dir),
        "log_path": str(log_path),
        "attempt_log_path": str(log_path),
        "metrics_path": "",
        "metrics": {},
        "failure_category": category,
        "retryable": retryable,
        "last_error": last_error,
        "error": error,
        "spec": task.__dict__,
    }


def prefetch_group_inputs(
    tasks: list[TaskSpec], args: argparse.Namespace, group_dir: Path
) -> tuple[list[TaskSpec], list[dict], dict[str, Path]]:
    """Prepare reusable input archives before any Sandbox is created.

    This validates and packs caller-provided inputs once on the controller so
    every Sandbox receives the same immutable archive.  Cache files are
    intentionally retained after the group ends; callers own their lifecycle.
    """

    if args.no_prefetch_unified or args.unified_archive:
        return tasks, [], {}

    if args.unified_cache_dir is None:
        failures = [
            _prefetch_failure(
                task,
                group_dir,
                "local unified input is required; provide --unified-cache-dir",
            )
            for task in tasks
        ]
        return [], failures, {}

    cache_dir = args.unified_cache_dir
    min_free_bytes = args.unified_min_free_gb * 1024**3
    stop_prefetch = threading.Event()

    def prepare_task(task: TaskSpec) -> tuple[TaskSpec, Path | None, dict | None]:
        if stop_prefetch.is_set():
            return task, None, _prefetch_failure(task, group_dir, "input prefetch stopped after host disk became full")
        free = shutil.disk_usage(cache_dir).free
        if free < min_free_bytes:
            stop_prefetch.set()
            return task, None, _prefetch_failure(
                task,
                group_dir,
                f"insufficient free space for unified prefetch: free={free} required={min_free_bytes} cache_dir={cache_dir}",
            )
        archive = _cache_archive_path(cache_dir, task.task_id, args.platform)
        started = time.time()
        try:
            ready = prepare_unified_archive(
                task_id=task.task_id,
                platform=args.platform,
                archive=None,
                cache_dir=cache_dir,
                output_archive=archive,
            )
            # Always hand the exact cached archive to the execution phase; a
            # cache directory may also contain an unpacked legacy input.
            if ready != archive:
                if ready.is_file():
                    archive = ready
                else:
                    raise RuntimeError(f"prefetch did not produce an archive: {ready}")
            print(
                f"prefetched task={task.task_id} archive={archive} bytes={archive.stat().st_size} "
                f"elapsed={round(time.time() - started, 1)}s",
                flush=True,
            )
            return task, archive, None
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                stop_prefetch.set()
            return task, None, _prefetch_failure(task, group_dir, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return task, None, _prefetch_failure(task, group_dir, f"{type(exc).__name__}: {exc}")

    ready_tasks: list[TaskSpec] = []
    failures: list[dict] = []
    archives: dict[str, Path] = {}
    with futures.ThreadPoolExecutor(max_workers=args.unified_prefetch_concurrency) as pool:
        pending = [pool.submit(prepare_task, task) for task in tasks]
        for future in futures.as_completed(pending):
            task, archive, failure = future.result()
            if failure:
                failures.append(failure)
            elif archive:
                ready_tasks.append(task)
                archives[task.task_id] = archive

    manifest = {
        "cache_dir": str(cache_dir),
        "min_free_gb": args.unified_min_free_gb,
        "prefetch_concurrency": args.unified_prefetch_concurrency,
        "archives": {task_id: str(path) for task_id, path in sorted(archives.items())},
        "failed_task_ids": sorted(item["task_id"] for item in failures),
        "cache_is_retained": True,
    }
    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "input_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return ready_tasks, failures, archives


def build_run_one_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(args.run_one),
        "--task-id",
        args.current_task.task_id,
        "--agent-cli",
        args.current_task.agent_cli or args.agent_cli,
        "--timeout-sec",
        str(args.current_task.timeout_sec or args.timeout_sec),
    ]
    append_if_value(cmd, "--mcp-provider", args.current_task.mcp_provider or args.mcp_provider)
    template = args.current_task.template or getattr(args, "template", "")
    if template:
        cmd.extend(["--template", template])
    if getattr(args, "source_dir", None):
        cmd.extend(["--source-dir", str(args.source_dir)])
    prefetched_archive = getattr(args, "prefetched_archives", {}).get(args.current_task.task_id)
    if prefetched_archive:
        cmd.extend(["--unified-archive", str(prefetched_archive)])
    elif getattr(args, "unified_archive", None):
        cmd.extend(["--unified-archive", str(args.unified_archive)])
    # A pre-fetched archive is a strict execution boundary: each worker gets
    # the exact input validated by the controller.
    if getattr(args, "unified_cache_dir", None) and not prefetched_archive:
        cmd.extend(["--unified-cache-dir", str(args.unified_cache_dir)])
    if getattr(args, "poll_interval", None) is not None and args.platform == "linux":
        cmd.extend(["--poll-interval", str(args.poll_interval)])
    if getattr(args, "no_prefetch_unified", False):
        cmd.append("--no-prefetch-unified")
    if getattr(args, "no_repro_mode", False):
        cmd.append("--no-repro-mode")
    if getattr(args, "stream_run", False) and args.platform == "linux":
        cmd.append("--stream-run")
    if getattr(args, "visual", False):
        cmd.append("--visual")
    result_dir = args.current_task.result_dir or str(getattr(args, "current_result_dir", "") or "")
    # Linux and Web persist controller-side artifacts under the task result dir.
    # Other platforms keep their existing result handling.
    if result_dir and args.platform in {"linux", "web"}:
        cmd.extend(["--result-dir", result_dir])
    recreated_app_dir = args.current_task.recreated_app_dir or str(getattr(args, "recreated_app_dir", "") or "")
    if recreated_app_dir:
        cmd.extend(["--recreated-app-dir", recreated_app_dir])
    if args.current_task.visual_only or getattr(args, "visual_only", False):
        cmd.append("--visual-only")
    return cmd


def run_task_once(task: TaskSpec, args: argparse.Namespace, out_dir: Path, *, attempt: int) -> dict:
    task_id = task.task_id
    task_dir = out_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / "run.log"
    attempt_log_path = task_dir / f"run_attempt-{attempt}.log"
    metrics_path = task_dir / "metrics.json"

    task_args = argparse.Namespace(**vars(args))
    task_args.current_task = task
    task_args.current_result_dir = task_dir
    cmd = build_run_one_command(task_args)

    env = os.environ.copy()
    if args.model or task.model:
        env["RB_MODEL"] = task.model or args.model
    env.update(task.extra_env)
    started = time.time()
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    output = proc.stdout or ""
    attempt_log_path.write_text(output)
    log_path.write_text(output)
    metrics = extract_metrics(output)
    if metrics:
        metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    failure_category, retryable, last_error = classify_failure(output, metrics, exit_code=proc.returncode)
    return {
        "task_id": task_id,
        "exit_code": proc.returncode,
        "elapsed_sec": round(time.time() - started, 1),
        "attempt": attempt,
        "task_dir": str(task_dir),
        "log_path": str(log_path),
        "attempt_log_path": str(attempt_log_path),
        "metrics_path": str(metrics_path) if metrics else "",
        "metrics": metrics,
        "failure_category": failure_category,
        "retryable": retryable,
        "last_error": last_error,
        "spec": task.__dict__,
    }


def run_task(task: TaskSpec, args: argparse.Namespace, out_dir: Path) -> dict:
    attempts: list[dict] = []
    max_attempts = 1 + args.retries
    for attempt in range(1, max_attempts + 1):
        try:
            result = run_task_once(task, args, out_dir, attempt=attempt)
        except Exception as exc:
            task_dir = out_dir / "tasks" / task.task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            log_path = task_dir / "run.log"
            attempt_log_path = task_dir / f"run_attempt-{attempt}.log"
            error_text = f"{type(exc).__name__}: {exc}"
            attempt_log_path.write_text(error_text)
            log_path.write_text(error_text)
            result = {
                "task_id": task.task_id,
                "exit_code": 1,
                "elapsed_sec": 0,
                "attempt": attempt,
                "task_dir": str(task_dir),
                "log_path": str(log_path),
                "attempt_log_path": str(attempt_log_path),
                "metrics_path": "",
                "metrics": {},
                "error": error_text,
            }
            category, retryable, last_error = classify_failure(
                "",
                {},
                exit_code=1,
                error=result["error"],
            )
            result["failure_category"] = category
            result["retryable"] = retryable
            result["last_error"] = last_error
        attempts.append(
            {
                "attempt": attempt,
                "exit_code": result.get("exit_code"),
                "metrics_path": result.get("metrics_path", ""),
                "log_path": result.get("attempt_log_path") or result.get("log_path", ""),
                "failure_category": result.get("failure_category", ""),
                "retryable": result.get("retryable", False),
                "last_error": result.get("last_error", ""),
            }
        )
        metrics = result.get("metrics") or {}
        if result.get("exit_code") == 0 and metrics:
            break
        if not result.get("retryable") or attempt >= max_attempts:
            break
        print(
            f"retry task={task.task_id} attempt={attempt + 1}/{max_attempts} "
            f"category={result.get('failure_category')} error={result.get('last_error','')}",
            flush=True,
        )
        time.sleep(max(0, args.retry_backoff_sec))

    result["attempts"] = attempts
    result["retry_count"] = max(0, int(result.get("attempt", 1)) - 1)
    return result


def add_common_args(
    parser: argparse.ArgumentParser,
    *,
    platform: str,
    default_group_id: str,
    default_mcp_provider: str,
) -> None:
    parser.add_argument("--task-id", action="append", default=[], help="task id or comma-separated task ids")
    parser.add_argument("--tasks-file", "--group-file", dest="tasks_file", type=Path)
    parser.add_argument("--group-id", default=os.environ.get("RB_GROUP_ID", default_group_id))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("RB_CONCURRENCY", "2")))
    parser.add_argument("--agent-cli", default=os.environ.get("RB_AGENT_CLI", "codex"))
    parser.add_argument("--mcp-provider", default=os.environ.get("RB_MCP_PROVIDER", default_mcp_provider))
    parser.add_argument("--model", default=os.environ.get("RB_MODEL", ""))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("RB_SANDBOX_TIMEOUT_SEC", "86400")))
    parser.add_argument("--retries", type=int, default=int(os.environ.get("RB_TASK_RETRIES", "1")))
    parser.add_argument(
        "--retry-backoff-sec",
        type=int,
        default=int(os.environ.get("RB_TASK_RETRY_BACKOFF_SEC", "30")),
    )
    parser.add_argument("--out-dir", "--result-dir", dest="out_dir", type=Path, default=Path("parallel_results"))
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--unified-archive", type=Path, default=Path(os.environ["RB_UNIFIED_ARCHIVE"]) if os.environ.get("RB_UNIFIED_ARCHIVE") else None)
    parser.add_argument("--unified-cache-dir", type=Path, default=Path(os.environ["RB_UNIFIED_CACHE_DIR"]) if os.environ.get("RB_UNIFIED_CACHE_DIR") else None)
    parser.add_argument(
        "--unified-prefetch-concurrency",
        type=int,
        default=int(os.environ.get("RB_UNIFIED_PREFETCH_CONCURRENCY", "2")),
        help="maximum concurrent local input validation/packing operations",
    )
    parser.add_argument(
        "--unified-min-free-gb",
        type=int,
        default=int(os.environ.get("RB_UNIFIED_MIN_FREE_GB", "8")),
        help="minimum free space required in the unified cache filesystem before each prefetch",
    )
    parser.add_argument("--template", default="")
    parser.add_argument(
        "--no-prefetch-unified",
        action="store_true",
        help="let each task read --unified-cache-dir directly instead of packing inputs up front",
    )
    parser.add_argument("--no-repro-mode", action="store_true")
    parser.add_argument("--visual", action="store_true", default=os.environ.get("RB_ENABLE_VISUAL", "").lower() in {"1", "true", "yes", "on"})
    parser.add_argument("--visual-only", action="store_true", default=os.environ.get("RB_VISUAL_ONLY", "").lower() in {"1", "true", "yes", "on"})
    parser.add_argument("--recreated-app-dir", type=Path, help="reuse the same local recreation artifact for every task; JSONL recreated_app_dir overrides this")
    parser.add_argument("--dry-run", action="store_true", help="parse the group and print run_one commands without creating sandboxes")
    if platform == "linux":
        parser.add_argument("--poll-interval", type=int, default=int(os.environ.get("RB_POLL_INTERVAL_SEC", "30")))
        parser.add_argument("--stream-run", action="store_true")


def run_group(args: argparse.Namespace) -> int:
    tasks = parse_task_specs(args)
    if not tasks:
        raise SystemExit("provide --task-id or --tasks-file/--group-file")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    if args.retries < 0:
        raise SystemExit("--retries must be >= 0")
    if args.unified_prefetch_concurrency < 1:
        raise SystemExit("--unified-prefetch-concurrency must be >= 1")
    if args.unified_min_free_gb < 0:
        raise SystemExit("--unified-min-free-gb must be >= 0")
    if not args.dry_run and args.unified_archive is None and args.unified_cache_dir is None:
        raise SystemExit("provide --unified-archive or --unified-cache-dir")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    group_dir = args.out_dir / args.group_id / run_id
    group_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"platform={args.platform} group={args.group_id} tasks={len(tasks)} "
        f"concurrency={args.concurrency} out_dir={group_dir}",
        flush=True,
    )

    group_manifest = {
        "schema_version": 1,
        "group_id": args.group_id,
        "platform": args.platform,
        "id_contract": (
            "task_id is the released dataset app id; legacy bench50-prefixed Ubuntu "
            "aliases are accepted for compatibility"
        ),
        "concurrency": args.concurrency,
        "retries": args.retries,
        "retry_backoff_sec": args.retry_backoff_sec,
        "unified_prefetch_concurrency": args.unified_prefetch_concurrency,
        "unified_min_free_gb": args.unified_min_free_gb,
        "unified_cache_dir": str(args.unified_cache_dir) if args.unified_cache_dir else "",
        "visual": bool(args.visual),
        "tasks": [task.__dict__ for task in tasks],
    }
    group_manifest_path = group_dir / "group.json"
    group_manifest_path.write_text(json.dumps(group_manifest, indent=2, ensure_ascii=False))
    if args.dry_run:
        print(f"group_manifest={group_manifest_path}")
        for task in tasks:
            task_args = argparse.Namespace(**vars(args))
            task_args.current_task = task
            print(" ".join(build_run_one_command(task_args)))
        return 0

    ready_tasks, results, prefetched_archives = prefetch_group_inputs(tasks, args, group_dir)
    args.prefetched_archives = prefetched_archives
    exit_code = 0
    if results:
        exit_code = 1
        for result in results:
            print(
                f"prefetch_failed task={result['task_id']} category={result['failure_category']} "
                f"error={result['last_error']} log={result['log_path']}",
                flush=True,
            )
    with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        future_map = {pool.submit(run_task, task, args, group_dir): task.task_id for task in ready_tasks}
        for fut in futures.as_completed(future_map):
            task_id = future_map[fut]
            try:
                result = fut.result()
            except Exception as exc:
                result = {
                    "task_id": task_id,
                    "exit_code": 1,
                    "elapsed_sec": 0,
                    "log_path": "",
                    "metrics_path": "",
                    "metrics": {},
                    "error": f"{type(exc).__name__}: {exc}",
                }
                category, retryable, last_error = classify_failure(
                    "",
                    {},
                    exit_code=1,
                    error=result["error"],
                )
                result["failure_category"] = category
                result["retryable"] = retryable
                result["last_error"] = last_error
            results.append(result)
            metrics = result.get("metrics") or {}
            print(
                f"done task={task_id} exit={result['exit_code']} "
                f"passed={metrics.get('passed')} score={metrics.get('task_score')} "
                f"category={result.get('failure_category','')} retries={result.get('retry_count',0)} "
                f"elapsed={result['elapsed_sec']}s log={result.get('log_path','')}",
                flush=True,
            )
            if result["exit_code"] != 0 or not metrics:
                exit_code = 1

    group_metrics = build_group_metrics(args.group_id, args.platform, results)
    group_metrics_path = group_dir / "group_metrics.json"
    group_metrics_path.write_text(json.dumps(group_metrics, indent=2, ensure_ascii=False))

    summary = {
        "group_id": args.group_id,
        "platform": args.platform,
        "concurrency": args.concurrency,
        "tasks": len(tasks),
        "group_metrics_path": str(group_metrics_path),
        "results": sorted(results, key=lambda item: item["task_id"]),
    }
    summary_path = group_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"summary={summary_path}")
    print(f"group_metrics={group_metrics_path}")
    return exit_code


def main(
    *,
    platform: str,
    default_group_id: str,
    default_mcp_provider: str,
    env_files: Sequence[Path],
    run_one: Path,
    argv: Sequence[str] | None = None,
) -> int:
    load_dotenv_quiet()
    for env_file in env_files:
        load_dotenv_quiet(env_file, override=False)

    parser = argparse.ArgumentParser()
    add_common_args(
        parser,
        platform=platform,
        default_group_id=default_group_id,
        default_mcp_provider=default_mcp_provider,
    )
    args = parser.parse_args(argv)
    args.platform = platform
    args.run_one = run_one
    return run_group(args)
