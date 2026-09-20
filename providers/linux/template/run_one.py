#!/usr/bin/env python3
"""Create one sandbox and run one Linux RecreationBench task end-to-end."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shlex
import tarfile
import tempfile
import time
import sys
from pathlib import Path

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException

SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from common.unified_cache import prepare_unified_archive, validate_unified_archive  # noqa: E402
from common.desktop_stream import create_desktop_sandbox, should_use_desktop_stream, start_desktop_stream  # noqa: E402
from common.dotenv_loader import load_dotenv_quiet  # noqa: E402
from common.runtime_source import add_runtime_source, resolve_runtime_source_dir, safe_extract_tar  # noqa: E402
from common.visual_viewer import local_novnc_command, sandbox_novnc_url, sandbox_proxy_tokens, start_local_novnc_server, write_visual_index, write_visual_viewer  # noqa: E402

CHUNK_SIZE = 16 * 1024 * 1024
REMOTE_RUN_LOG = "/tmp/rb-linux-sandbox-run.log"
REMOTE_RUN_EXIT = "/tmp/rb-linux-sandbox-run.exit"
REMOTE_RUN_PID = "/tmp/rb-linux-sandbox-run.pid"
REMOTE_METRICS = "/tmp/recreationbench-output/metrics.json"

RETRYABLE_SDK_PATTERNS = [
    r"RemoteProtocolError",
    r"Server disconnected",
    r"Command ended without an end event",
    r"ConnectError",
    r"ReadTimeout",
    r"ConnectTimeout",
    r"WriteTimeout",
    r"PoolTimeout",
    r"nodename nor servname provided",
    r"Temporary failure in name resolution",
    r"Name or service not known",
]


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def verbose_enabled() -> bool:
    return truthy(os.environ.get("RB_VERBOSE"))


def vprint(*values: object, **kwargs: object) -> None:
    if verbose_enabled():
        print(*values, **kwargs)


def metrics_dict(metrics: str | bytes | dict | None) -> dict:
    if isinstance(metrics, dict):
        return metrics
    if metrics is None:
        return {}
    if isinstance(metrics, bytes):
        metrics = metrics.decode(errors="replace")
    try:
        parsed = json.loads(metrics)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def print_metrics_summary(metrics: str | bytes | dict | None, metrics_path: Path) -> None:
    parsed = metrics_dict(metrics)
    print(f"metrics_file={metrics_path}", flush=True)
    print("metrics.json:", flush=True)
    print(json.dumps(parsed, ensure_ascii=False), flush=True)
    for key in ("task_score", "program_score", "vlm_score", "passed", "pipeline_exit_code", "stage"):
        if key in parsed and parsed[key] is not None:
            print(f"{key}={parsed[key]}", flush=True)


def local_visual_output_only(args: argparse.Namespace) -> bool:
    return bool(args.local_viewer and (args.visual or args.visual_only))


def resolve_recreated_app_dir(args: argparse.Namespace) -> None:
    if args.recreated_app_dir or not args.visual_only:
        return
    candidate = task_result_dir(args) / "recreation"
    if candidate.is_dir():
        args.recreated_app_dir = candidate


def validate_recreated_app_dir(path: Path) -> None:
    for required in ("build.sh", "launch.sh"):
        if not (path / required).is_file():
            raise RuntimeError(f"--recreated-app-dir missing {required}: {path}")


def is_retryable_sdk_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in RETRYABLE_SDK_PATTERNS)


def sdk_retry(label: str, fn, *, attempts: int | None = None, backoff_sec: int | None = None):
    attempts = attempts or max(1, int_env("RB_SDK_RETRIES", 3))
    backoff_sec = backoff_sec if backoff_sec is not None else max(0, int_env("RB_SDK_RETRY_BACKOFF_SEC", 5))
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            retryable = is_retryable_sdk_error(exc)
            if not retryable or attempt >= attempts:
                raise
            vprint(
                f"sdk_retry label={label} attempt={attempt + 1}/{attempts} "
                f"error={type(exc).__name__}: {str(exc)[:180]}",
                flush=True,
            )
            time.sleep(backoff_sec * attempt)
    assert last_exc is not None
    raise last_exc


def synthetic_metrics(args: argparse.Namespace, *, exit_code: int, error: str, stdout: str = "") -> dict:
    category = "infra_network" if any(
        re.search(pattern, error, re.IGNORECASE) for pattern in RETRYABLE_SDK_PATTERNS
    ) else "no_metrics"
    if re.search(r'Unknown option: "appstream:svg-support"', stdout, re.IGNORECASE):
        category = "env_missing_dependency"
    elif re.search(r"bwrap: Creating new namespace failed: Operation not permitted", stdout, re.IGNORECASE):
        category = "runtime_permission"
    elif re.search(r"recreation/build\.sh missing", stdout, re.IGNORECASE):
        category = "agent_artifact_missing"
    elif re.search(r"AccessDenied|Access denied by bucket policy|missing environment variable:|unified instance not found|unified cache (dir not found|miss)|unified archive (not found|does not contain)", error, re.IGNORECASE):
        category = "input_config"
    return {
        "task_id": args.task_id,
        "model": os.environ.get("RB_MODEL", ""),
        "pipeline_exit_code": exit_code,
        "stage": args.stage,
        "task_score": None,
        "program_score": None,
        "vlm_score": None,
        "scoring_eval": None,
        "passed": None,
        "exit_code": "host:metrics_unavailable",
        "error": error[:500],
        "exit_reason": error[:500],
        "failure_category": category,
        "platform": "linux",
    }


def make_source_archive(source_dir: Path) -> bytes:
    source_dir = source_dir.resolve()
    with tempfile.TemporaryFile() as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            add_runtime_source(tar, source_dir, archive_root="RecreationBench")
        raw.seek(0)
        return raw.read()


def make_contents_archive(source_dir: Path) -> bytes:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise RuntimeError(f"directory not found: {source_dir}")
    with tempfile.TemporaryFile() as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            def filt(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                parts = Path(info.name).parts
                skip = {"__pycache__", ".pytest_cache", "node_modules", ".venv"}
                if any(part in skip for part in parts):
                    return None
                return info

            for child in sorted(source_dir.iterdir(), key=lambda p: p.name):
                tar.add(child, arcname=child.name, filter=filt)
        raw.seek(0)
        return raw.read()


def task_result_dir(args: argparse.Namespace) -> Path:
    base = args.result_dir
    if base.name == args.task_id:
        return base
    return base / args.task_id


def visual_viewer_path(args: argparse.Namespace) -> Path:
    return task_result_dir(args) / "visual_viewer.html"


def write_local_file(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


def read_sandbox_file(sandbox: Sandbox, path: str, *, format: str = "text") -> str | bytes:
    return sdk_retry("read_" + Path(path).name, lambda: sandbox.files.read(path, format=format), attempts=2)


def collect_result_artifacts(
    sandbox: Sandbox,
    args: argparse.Namespace,
    *,
    result_dir: Path,
    metrics: str | bytes | None,
    stdout: str,
    exit_code: int,
) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    write_local_file(result_dir / "run.log", stdout)
    if metrics is not None:
        write_local_file(result_dir / "metrics.json", metrics)
    sandbox_info = {
        "sandbox_id": getattr(sandbox, "sandbox_id", ""),
        "task_id": args.task_id,
        "platform": "linux",
        "exit_code": exit_code,
        "visual": bool(args.visual or args.visual_only),
        "visual_only": bool(args.visual_only),
    }
    write_local_file(result_dir / "sandbox_info.json", json.dumps(sandbox_info, indent=2, ensure_ascii=False) + "\n")
    write_visual_index(
        result_dir,
        task_id=args.task_id,
        platform="linux",
        sandbox_id=sandbox_info["sandbox_id"],
        metrics=metrics,
    )

    bundle = "/tmp/rb-linux-result-bundle.tar.gz"
    pack_script = " ".join(
        [
            "rb-controller-helper",
            "pack-linux-results",
            "--task-id",
            shlex.quote(args.task_id),
            "--output-dir",
            "/tmp/recreationbench-output",
            "--bundle",
            bundle,
        ]
    )
    try:
        sdk_retry(
            "pack_result_artifacts",
            lambda: sandbox.commands.run(pack_script, timeout=120, request_timeout=180),
            attempts=2,
        )
        payload = read_sandbox_file(sandbox, bundle, format="bytes")
        archive_path = result_dir / "result_artifacts.tar.gz"
        if isinstance(payload, str):
            archive_path.write_bytes(payload.encode())
        else:
            archive_path.write_bytes(payload)
        safe_extract_tar(archive_path, result_dir)
        vprint(f"result_dir={result_dir}", flush=True)
    except Exception as exc:
        vprint(f"collect_result_artifacts_failed={type(exc).__name__}: {str(exc)[:240]}", flush=True)


def install_runner_override(sandbox: Sandbox) -> None:
    linux_dir = Path(__file__).resolve().parents[1]
    overrides = [
        (linux_dir / "bin" / "rb-linux-sandbox-run", "/tmp/rb-linux-sandbox-run", "/usr/local/bin/rb-linux-sandbox-run"),
        (linux_dir / "scripts" / "rb-start-visual", "/tmp/rb-start-visual", "/usr/local/bin/rb-start-visual"),
        (linux_dir.parent / "common" / "controller_helper.py", "/tmp/rb-controller-helper", "/usr/local/bin/rb-controller-helper"),
    ]
    installs = []
    uploaded = []
    for local, tmp_remote, final_remote in overrides:
        if not local.is_file():
            continue
        sdk_retry(
            f"write_{local.name}_override",
            lambda local=local, tmp_remote=tmp_remote: sandbox.files.write(
                tmp_remote,
                io.BytesIO(local.read_bytes()),
                request_timeout=120,
            ),
        )
        installs.append(f"sudo -n install -m 0755 {shlex.quote(tmp_remote)} {shlex.quote(final_remote)}")
        uploaded.append(str(local))
    if installs:
        sdk_retry(
            "install_linux_overrides",
            lambda: sandbox.commands.run(" && ".join(installs), timeout=30, request_timeout=60),
        )
        vprint(f"uploaded_runner_override={','.join(uploaded)}", flush=True)


def remote_join_command(parts_dir: str, remote_archive: str, remote_root: str) -> str:
    return (
        f"cat {shlex.quote(parts_dir)}/part-* > {shlex.quote(remote_archive)} && "
        f"rm -rf {shlex.quote(remote_root)} && mkdir -p {shlex.quote(remote_root)} && "
        f"tar -xzf {shlex.quote(remote_archive)} -C {shlex.quote(remote_root)}"
    )


def upload_archive(
    sandbox: Sandbox,
    data: bytes | Path,
    *,
    remote_archive: str,
    remote_root: str,
    label: str,
) -> None:
    path = data if isinstance(data, Path) else None
    size = path.stat().st_size if path is not None else len(data)
    if size <= 30 * 1024 * 1024:
        content = path.read_bytes() if path is not None else data
        sdk_retry(
            f"write_{label}_archive",
            lambda: sandbox.files.write(remote_archive, io.BytesIO(content), request_timeout=120),
        )
        sdk_retry(
            f"extract_{label}_archive",
            lambda: sandbox.commands.run(
                f"rm -rf {remote_root} && mkdir -p {remote_root} && "
                f"tar -xzf {remote_archive} -C {remote_root}",
                timeout=180,
            ),
        )
        vprint(f"uploaded_{label}_bytes={size}")
        return

    parts_dir = f"{remote_archive}.parts"
    sdk_retry(
        f"prepare_{label}_parts",
        lambda: sandbox.commands.run(
            f"rm -rf {parts_dir} {remote_archive} {remote_root} && mkdir -p {parts_dir}",
            timeout=60,
        ),
    )
    count = 0
    if path is not None:
        with path.open("rb") as src:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                part_path = f"{parts_dir}/part-{count:04d}"
                sdk_retry(
                    f"write_{label}_part_{count}",
                    lambda part_path=part_path, chunk=chunk: sandbox.files.write(
                        part_path,
                        chunk,
                        request_timeout=180,
                    ),
                )
                vprint(f"uploaded_{label}_part={count} bytes={len(chunk)}", flush=True)
                count += 1
    else:
        for offset in range(0, size, CHUNK_SIZE):
            chunk = data[offset : offset + CHUNK_SIZE]
            part_path = f"{parts_dir}/part-{count:04d}"
            sdk_retry(
                f"write_{label}_part_{count}",
                lambda part_path=part_path, chunk=chunk: sandbox.files.write(
                    part_path,
                    chunk,
                    request_timeout=180,
                ),
            )
            vprint(f"uploaded_{label}_part={count} bytes={len(chunk)}", flush=True)
            count += 1
    sdk_retry(
        f"join_{label}_parts",
        lambda: sandbox.commands.run(
            remote_join_command(parts_dir, remote_archive, remote_root),
            timeout=300,
        ),
    )
    vprint(f"uploaded_{label}_bytes={size} parts={count}")


def sandbox_env(args: argparse.Namespace) -> dict[str, str]:
    envs = {
        "TASK_ID": args.task_id,
        "STAGE": args.stage,
        "RB_AGENT_CLI": args.agent_cli,
        "RB_MCP_PROVIDER": args.mcp_provider,
        "RB_OUTPUT_DIR": "/tmp/recreationbench-output",
        "RB_PIPELINE_DIR": "/workspace/RecreationBench",
        "RB_MANAGE_DISPLAY": "true",
    }
    if args.repro_mode and "RB_CODEX_PROXY" not in os.environ:
        # Keep Codex behind the root loopback streaming proxy while forwarding
        # directly to the model endpoint configured by the caller.
        envs["RB_CODEX_PROXY"] = "1"
    if args.visual_only:
        envs["RB_VISUAL_ONLY"] = "1"
    if args.recreated_app_dir:
        envs["RB_PRELOADED_RECREATION_DIR"] = "/tmp/rb-preloaded-recreation"
    if args.stage != "setup" and not args.visual_only:
        envs["RB_MODEL"] = required_env("RB_MODEL")
    elif os.environ.get("RB_MODEL", ""):
        envs["RB_MODEL"] = os.environ["RB_MODEL"]
    optional = [
        "RB_MODEL_BASE_URL",
        "RB_MODEL_API_KEY",
        "RB_AUTH_TOKEN",
        "RB_VLM_KEY",
        "RB_VLM_MODEL",
        "RB_VLM_BASE_URL",
        "RB_UNIFIED_PLATFORM",
        "RB_UNIFIED_APP",
        "RB_UNIFIED_LOCAL_ROOT",
        "RB_CODEX_PROXY",
        # Recreation agent tuning. These names are consumed by
        # RecreationBench/scripts/linux/stages/recreation.py before it writes
        # the in-VM agent env, so they must be present in the sandbox process.
        "CONTEXT_1M",
        "AUTO_COMPACT_WINDOW",
        "THINKING_EFFORT",
        "RECREATION_TIMEOUT",
        "EXTRA_BODY",
        "RB_CONTEXT_1M",
        "RB_AUTO_COMPACT_WINDOW",
        "RB_THINKING_EFFORT",
        "RB_RECREATION_AGENT_TIMEOUT",
        "RB_EXTRA_BODY",
        "RB_MAX_OUTPUT_TOKENS",
        "RB_CUA_DRIVER_REF",
        "RB_CUA_COORDINATE_SPACE",
        "RB_CUA_PREFLIGHT_MODE",
        "RB_MODEL_MAX_RETRIES",
        "RB_MODEL_PROXY_RETRIES",
        "RB_MODEL_PROXY_BACKOFF_SEC",
        "RB_MODEL_PROXY_RETRY_ERRORS",
        "RB_VISUAL_PASSWORD",
        "RB_VISUAL_GEOMETRY",
        "RB_VISUAL_WEB_PORT",
        "RB_VISUAL_RFB_PORT",
        "RB_VISUAL_MODE",
        "RB_WEB_HEADFUL",
        "RB_PRELOADED_RECREATION_DIR",
    ]
    for name in optional:
        value = os.environ.get(name, "")
        if value:
            envs[name] = value
    if "RB_MODEL_MAX_RETRIES" not in envs and "RB_MODEL_PROXY_RETRIES" in envs:
        envs["RB_MODEL_MAX_RETRIES"] = envs["RB_MODEL_PROXY_RETRIES"]
    if args.visual or args.visual_only:
        envs["RB_ENABLE_VISUAL"] = "1"
        envs.setdefault("RB_VISUAL_PASSWORD", secrets.token_urlsafe(12))
    return envs


def sandbox_create_options(args: argparse.Namespace, template: str, envs: dict[str, str]) -> dict:
    metadata = {"task_id": args.task_id, "platform": "linux", "rb_mode": "repro" if args.repro_mode else "strict"}
    vpc_config = os.environ.get("RB_SANDBOX_VPC_CONFIG", "").strip()
    if vpc_config:
        json.loads(vpc_config)
        metadata["fc.sandbox.network.vpc"] = vpc_config

    opts = {
        "template": template,
        "timeout": args.timeout_sec,
        "envs": envs,
        "metadata": metadata,
        "api_key": required_env("E2B_API_KEY"),
        "api_url": required_env("E2B_API_URL"),
        "domain": required_env("E2B_DOMAIN"),
        "allow_internet_access": True,
    }
    return opts


def build_sandbox_command(envs: dict[str, str]) -> str:
    command_env = {
        "RB_UNIFIED_LOCAL_ROOT": envs.get("RB_UNIFIED_LOCAL_ROOT", ""),
        "RB_CODEX_PROXY": envs.get("RB_CODEX_PROXY", ""),
        "RB_ENABLE_VISUAL": envs.get("RB_ENABLE_VISUAL", ""),
        "RB_VISUAL_PASSWORD": envs.get("RB_VISUAL_PASSWORD", ""),
        "RB_VISUAL_GEOMETRY": envs.get("RB_VISUAL_GEOMETRY", ""),
        "RB_VISUAL_WEB_PORT": envs.get("RB_VISUAL_WEB_PORT", ""),
        "RB_VISUAL_RFB_PORT": envs.get("RB_VISUAL_RFB_PORT", ""),
        "RB_VISUAL_MODE": envs.get("RB_VISUAL_MODE", ""),
        "RB_VISUAL_ONLY": envs.get("RB_VISUAL_ONLY", ""),
        "RB_PRELOADED_RECREATION_DIR": envs.get("RB_PRELOADED_RECREATION_DIR", ""),
        "RB_MODEL_PROXY_RETRIES": envs.get("RB_MODEL_PROXY_RETRIES", ""),
        "RB_MODEL_PROXY_BACKOFF_SEC": envs.get("RB_MODEL_PROXY_BACKOFF_SEC", ""),
        "RB_MODEL_PROXY_RETRY_ERRORS": envs.get("RB_MODEL_PROXY_RETRY_ERRORS", ""),
    }
    return " ".join(
        [
            "env",
            f"RB_UNIFIED_LOCAL_ROOT={shlex.quote(command_env['RB_UNIFIED_LOCAL_ROOT'])}",
            f"RB_CODEX_PROXY={shlex.quote(command_env['RB_CODEX_PROXY'])}",
            f"RB_ENABLE_VISUAL={shlex.quote(command_env['RB_ENABLE_VISUAL'])}",
            f"RB_VISUAL_PASSWORD={shlex.quote(command_env['RB_VISUAL_PASSWORD'])}",
            f"RB_VISUAL_GEOMETRY={shlex.quote(command_env['RB_VISUAL_GEOMETRY'])}",
            f"RB_VISUAL_WEB_PORT={shlex.quote(command_env['RB_VISUAL_WEB_PORT'])}",
            f"RB_VISUAL_RFB_PORT={shlex.quote(command_env['RB_VISUAL_RFB_PORT'])}",
            f"RB_VISUAL_MODE={shlex.quote(command_env['RB_VISUAL_MODE'])}",
            f"RB_VISUAL_ONLY={shlex.quote(command_env['RB_VISUAL_ONLY'])}",
            f"RB_PRELOADED_RECREATION_DIR={shlex.quote(command_env['RB_PRELOADED_RECREATION_DIR'])}",
            f"RB_MODEL_PROXY_RETRIES={shlex.quote(command_env['RB_MODEL_PROXY_RETRIES'])}",
            f"RB_MODEL_PROXY_BACKOFF_SEC={shlex.quote(command_env['RB_MODEL_PROXY_BACKOFF_SEC'])}",
            f"RB_MODEL_PROXY_RETRY_ERRORS={shlex.quote(command_env['RB_MODEL_PROXY_RETRY_ERRORS'])}",
            "rb-linux-sandbox-run",
        ]
    )


def run_streaming(sandbox: Sandbox, command: str, envs: dict[str, str], args: argparse.Namespace) -> tuple[int, str, str]:
    command_env = {
        "RB_UNIFIED_LOCAL_ROOT": envs.get("RB_UNIFIED_LOCAL_ROOT", ""),
        "RB_CODEX_PROXY": envs.get("RB_CODEX_PROXY", ""),
        "RB_ENABLE_VISUAL": envs.get("RB_ENABLE_VISUAL", ""),
        "RB_VISUAL_PASSWORD": envs.get("RB_VISUAL_PASSWORD", ""),
        "RB_VISUAL_GEOMETRY": envs.get("RB_VISUAL_GEOMETRY", ""),
        "RB_VISUAL_WEB_PORT": envs.get("RB_VISUAL_WEB_PORT", ""),
        "RB_VISUAL_RFB_PORT": envs.get("RB_VISUAL_RFB_PORT", ""),
        "RB_VISUAL_ONLY": envs.get("RB_VISUAL_ONLY", ""),
        "RB_PRELOADED_RECREATION_DIR": envs.get("RB_PRELOADED_RECREATION_DIR", ""),
        "RB_MODEL_PROXY_RETRIES": envs.get("RB_MODEL_PROXY_RETRIES", ""),
        "RB_MODEL_PROXY_BACKOFF_SEC": envs.get("RB_MODEL_PROXY_BACKOFF_SEC", ""),
        "RB_MODEL_PROXY_RETRY_ERRORS": envs.get("RB_MODEL_PROXY_RETRY_ERRORS", ""),
    }
    try:
        result = sandbox.commands.run(
            command,
            envs=command_env,
            timeout=args.timeout_sec - 300,
            request_timeout=args.timeout_sec,
        )
        return int(result.exit_code or 0), result.stdout, result.stderr
    except CommandExitException as exc:
        return int(exc.exit_code), exc.stdout, exc.stderr


def run_polled(sandbox: Sandbox, command: str, args: argparse.Namespace) -> tuple[int, str, str]:
    start_cmd = (
        f"if [ -s {REMOTE_RUN_PID} ] && kill -0 $(cat {REMOTE_RUN_PID}) 2>/dev/null; then "
        f"cat {REMOTE_RUN_PID}; "
        f"else rm -f {REMOTE_RUN_LOG} {REMOTE_RUN_EXIT} {REMOTE_RUN_PID}; "
        f"nohup bash -lc {shlex.quote(command + f' > {REMOTE_RUN_LOG} 2>&1; echo $? > {REMOTE_RUN_EXIT}')} "
        f"</dev/null >/tmp/rb-linux-sandbox-run.nohup 2>&1 & echo $! > {REMOTE_RUN_PID}; "
        f"cat {REMOTE_RUN_PID}; fi"
    )
    start = sdk_retry(
        "start_remote_run",
        lambda: sandbox.commands.run(start_cmd, timeout=30, request_timeout=60),
    )
    pid = (start.stdout or "").strip().splitlines()[-1] if start.stdout else ""
    vprint(f"remote_run_pid={pid}", flush=True)

    deadline = time.time() + max(60, args.timeout_sec - 300)
    last_size = -1
    last_metrics = ""
    poll_errors = 0
    while True:
        status_cmd = (
            "rb-controller-helper remote-status "
            f"--log {shlex.quote(REMOTE_RUN_LOG)} "
            f"--exit {shlex.quote(REMOTE_RUN_EXIT)} "
            f"--metrics {shlex.quote(REMOTE_METRICS)} "
            f"--pid {shlex.quote(REMOTE_RUN_PID)}"
        )
        try:
            status = sdk_retry(
                "poll_remote_run",
                lambda: sandbox.commands.run(status_cmd, timeout=20, request_timeout=60),
                attempts=max(1, int_env("RB_SDK_POLL_RETRIES", 2)),
            )
            poll_errors = 0
        except Exception as exc:
            poll_errors += 1
            if time.time() >= deadline or poll_errors >= int_env("RB_SDK_MAX_POLL_ERRORS", 20):
                raise
            vprint(f"poll_error count={poll_errors} error={type(exc).__name__}: {str(exc)[:180]}", flush=True)
            time.sleep(max(5, args.poll_interval))
            continue
        stdout = status.stdout or ""
        log_size = last_size
        metrics_exists = False
        exit_text = ""
        alive = True
        for line in stdout.splitlines():
            if line.startswith("pid "):
                parts = line.split()
                if "alive" in parts:
                    alive = parts[parts.index("alive") + 1] == "1"
                if "exit" in parts:
                    exit_text = " ".join(parts[parts.index("exit") + 1 :])
            elif line.startswith("log_size "):
                try:
                    log_size = int(line.split()[1])
                except Exception:
                    pass
            elif line.startswith("metrics "):
                metrics_exists = line.split()[1] == "1"
        if log_size != last_size or metrics_exists != bool(last_metrics):
            vprint(stdout, flush=True)
            last_size = log_size
            last_metrics = "1" if metrics_exists else ""
        if metrics_exists or exit_text or not alive:
            break
        if time.time() >= deadline:
            raise TimeoutError(f"remote run timed out after {args.timeout_sec - 300}s")
        time.sleep(max(5, args.poll_interval))

    final_log = sdk_retry(
        "read_remote_log",
        lambda: sandbox.commands.run(f"cat {REMOTE_RUN_LOG} 2>/dev/null || true", timeout=30, request_timeout=60),
    )
    exit_raw = sdk_retry(
        "read_remote_exit",
        lambda: sandbox.commands.run(f"cat {REMOTE_RUN_EXIT} 2>/dev/null || echo 1", timeout=10, request_timeout=30),
    ).stdout.strip()
    try:
        exit_code = int(exit_raw.splitlines()[-1])
    except Exception:
        exit_code = 1
    return exit_code, final_log.stdout or "", final_log.stderr or ""


def main() -> int:
    template_dir = Path(__file__).resolve().parent
    template_env = template_dir / ".env.linux"
    template_dotenv = template_dir / ".env"
    service_env = Path(__file__).resolve().parents[2] / ".env.linux"
    explicit_env = os.environ.get("RB_ENV_FILE", "").strip()
    if explicit_env:
        load_dotenv_quiet(explicit_env, override=False)
    else:
        load_dotenv_quiet(template_dotenv, override=False)
        load_dotenv_quiet(template_env, override=False)
    load_dotenv_quiet(service_env, override=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", default="recreation_eval")
    parser.add_argument("--agent-cli", default=os.environ.get("RB_AGENT_CLI", "codex"))
    parser.add_argument("--mcp-provider", default=os.environ.get("RB_MCP_PROVIDER", "cua-driver"))
    parser.add_argument("--template", default=os.environ.get("RB_LINUX_TEMPLATE", ""))
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="bundled pipeline runtime; override only to test another frozen runtime",
    )
    parser.add_argument("--source-url", default=os.environ.get("RB_SOURCE_URL", ""))
    parser.add_argument("--unified-archive", type=Path, default=Path(os.environ["RB_UNIFIED_ARCHIVE"]) if os.environ.get("RB_UNIFIED_ARCHIVE") else None, help="reuse a pre-fetched unified input archive")
    parser.add_argument("--unified-cache-dir", type=Path, default=Path(os.environ["RB_UNIFIED_CACHE_DIR"]) if os.environ.get("RB_UNIFIED_CACHE_DIR") else None, help="look for a local unified archive or directory")
    parser.add_argument("--write-unified-archive", type=Path, help="only prefetch the unified input archive, then exit")
    parser.add_argument("--result-dir", type=Path, default=Path(os.environ.get("RB_RESULT_DIR", "results")), help="local directory for metrics, logs, and recreated app artifacts")
    parser.add_argument("--recreated-app-dir", type=Path, default=Path(os.environ["RB_RECREATED_APP_DIR"]) if os.environ.get("RB_RECREATED_APP_DIR") else None, help="upload an existing recreation artifact and skip AI recreation when valid")
    parser.add_argument("--visual-only", action="store_true", default=truthy(os.environ.get("RB_VISUAL_ONLY")), help="upload/build/launch an existing recreation artifact for viewing; do not run eval")
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("RB_SANDBOX_TIMEOUT_SEC", "86400")))
    parser.add_argument("--poll-interval", type=int, default=int(os.environ.get("RB_POLL_INTERVAL_SEC", "30")))
    parser.add_argument(
        "--stream-run",
        action=argparse.BooleanOptionalAction,
        default=truthy(os.environ.get("RB_STREAM_RUN"), default=False),
        help="stream sandbox command through the SDK; default polls log/metrics to avoid stale streams",
    )
    parser.add_argument(
        "--repro-mode",
        action=argparse.BooleanOptionalAction,
        default=truthy(os.environ.get("RB_REPRO_MODE"), default=True),
        help="repro mode uses public internet and uploads prefetched benchmark input into the sandbox",
    )
    parser.add_argument(
        "--prefetch-unified",
        action=argparse.BooleanOptionalAction,
        default=truthy(os.environ.get("RB_PREFETCH_UNIFIED"), default=True),
        help="prepare the local unified eval input outside the sandbox and upload it into the sandbox",
    )
    parser.add_argument("--visual", action="store_true", default=truthy(os.environ.get("RB_ENABLE_VISUAL")), help="start a noVNC viewer on port 6080 and print the URL; implied by --visual-only")
    parser.add_argument("--local-viewer", action=argparse.BooleanOptionalAction, default=truthy(os.environ.get("RB_LOCAL_VIEWER"), default=True), help="start and healthcheck a local loopback noVNC viewer when visual is enabled")
    parser.add_argument("--local-viewer-port", type=int, default=int(os.environ.get("RB_LOCAL_VIEWER_PORT", "0")), help="local viewer port; 0 picks a free port")
    args = parser.parse_args()
    platform = os.environ.get("RB_UNIFIED_PLATFORM", "ubuntu").strip() or "ubuntu"
    args.source_dir = resolve_runtime_source_dir(
        service_root=SERVICE_ROOT,
        platform=platform,
        source_dir=args.source_dir,
    )
    resolve_recreated_app_dir(args)

    if args.write_unified_archive:
        archive = prepare_unified_archive(
            task_id=args.task_id,
            platform=platform,
            archive=args.unified_archive,
            cache_dir=args.unified_cache_dir,
            output_archive=args.write_unified_archive,
        )
        if archive != args.write_unified_archive:
            args.write_unified_archive.parent.mkdir(parents=True, exist_ok=True)
            args.write_unified_archive.write_bytes(archive.read_bytes())
            archive = args.write_unified_archive
        vprint(f"unified_archive={archive} bytes={archive.stat().st_size}")
        return 0

    if args.unified_archive:
        validate_unified_archive(args.unified_archive, task_id=args.task_id, platform=platform)
        vprint(f"using_unified_archive={args.unified_archive}", flush=True)

    template = (
        args.template
        or os.environ.get("RB_LINUX_TEMPLATE", "").strip()
        or os.environ.get("E2B_DESKTOP_TEMPLATE", "").strip()
    )
    if not template:
        raise RuntimeError("missing environment variable: E2B_DESKTOP_TEMPLATE")

    unified_tmp: tempfile.TemporaryDirectory[str] | None = None
    unified_archive_to_upload: Path | None = None
    if args.visual_only and not args.recreated_app_dir:
        raise RuntimeError(
            "--visual-only requires --recreated-app-dir or "
            f"{task_result_dir(args) / 'recreation'}"
        )
    if args.recreated_app_dir:
        validate_recreated_app_dir(args.recreated_app_dir)

    envs = sandbox_env(args)
    if args.source_url:
        envs["RB_SOURCE_URL"] = args.source_url
    else:
        envs["RB_SOURCE_ARCHIVE"] = "/tmp/recreationbench-source.tar.gz"

    if args.prefetch_unified and not args.visual_only:
        unified_tmp = tempfile.TemporaryDirectory(prefix="rb_linux_unified_archive_")
        unified_archive_to_upload = prepare_unified_archive(
            task_id=args.task_id,
            platform=platform,
            archive=args.unified_archive,
            cache_dir=args.unified_cache_dir,
            output_archive=Path(unified_tmp.name) / "unified.tar.gz",
        )
        vprint(f"using_unified_archive={unified_archive_to_upload}", flush=True)

    sandbox: Sandbox | None = None
    stdout = ""
    exit_code = 1
    try:
        create_options = sandbox_create_options(args, template, envs)
        if should_use_desktop_stream(args):
            sandbox = sdk_retry(
                "create_desktop_sandbox",
                lambda: create_desktop_sandbox(
                    create_options,
                    display=os.environ.get("RB_DESKTOP_DISPLAY", ":99"),
                    geometry=envs.get("RB_VISUAL_GEOMETRY"),
                ),
            )
            envs["RB_VISUAL_PASSWORD"] = sdk_retry("start_desktop_stream", lambda: start_desktop_stream(sandbox))
            envs["RB_VISUAL_MODE"] = "desktop_stream"
        else:
            sandbox = sdk_retry("create_sandbox", lambda: Sandbox.create(**create_options))
        if not local_visual_output_only(args):
            print(f"sandbox_id={sandbox.sandbox_id}", flush=True)
        if envs.get("RB_ENABLE_VISUAL"):
            web_port = int(envs.get("RB_VISUAL_WEB_PORT") or "6080")
            viewer_host = sandbox.get_host(web_port)
            visual_password = envs.get("RB_VISUAL_PASSWORD", "")
            viewer = write_visual_viewer(
                visual_viewer_path(args),
                task_id=args.task_id,
                platform="linux",
                sandbox_id=sandbox.sandbox_id,
                host=viewer_host,
                password=visual_password,
            )
            vprint(f"visual_instruction_file={viewer}", flush=True)
            if args.local_viewer:
                envd_access_token, traffic_access_token = sandbox_proxy_tokens(sandbox)
                local_url = start_local_novnc_server(
                    host=viewer_host,
                    password=visual_password,
                    task_id=args.task_id,
                    result_dir=task_result_dir(args),
                    platform="linux",
                    sandbox_id=sandbox.sandbox_id,
                    port=args.local_viewer_port,
                    envd_access_token=envd_access_token,
                    traffic_access_token=traffic_access_token,
                )
                visual_index = write_visual_index(
                    task_result_dir(args),
                    task_id=args.task_id,
                    platform="linux",
                    sandbox_id=sandbox.sandbox_id,
                )
                print(f"visual_url={local_url}", flush=True)
                vprint(f"visual_index_file={visual_index}", flush=True)
            else:
                print(f"visual_url={sandbox_novnc_url(viewer_host)}", flush=True)
                vprint(f"visual_local_server_command={local_novnc_command(viewer_host, visual_password, args.task_id)}", flush=True)
                print(f"visual_password={visual_password}", flush=True)

        if not args.source_url:
            archive = make_source_archive(args.source_dir)
            upload_archive(
                sandbox,
                archive,
                remote_archive="/tmp/recreationbench-source.tar.gz",
                remote_root="/tmp/recreationbench-source",
                label="source",
            )

        install_runner_override(sandbox)

        if args.prefetch_unified and not args.visual_only:
            if unified_archive_to_upload is None:
                raise RuntimeError("internal error: unified archive was not prepared")
            upload_archive(
                sandbox,
                unified_archive_to_upload,
                remote_archive="/tmp/recreationbench-unified.tar.gz",
                remote_root="/tmp/recreationbench-unified",
                label="unified",
            )
            envs["RB_UNIFIED_LOCAL_ROOT"] = "/tmp/recreationbench-unified"

        if args.recreated_app_dir:
            upload_archive(
                sandbox,
                make_contents_archive(args.recreated_app_dir),
                remote_archive="/tmp/rb-preloaded-recreation.tar.gz",
                remote_root="/tmp/rb-preloaded-recreation",
                label="recreated_app",
            )

        command = build_sandbox_command(envs)
        if args.stream_run:
            exit_code, stdout, stderr = run_streaming(sandbox, command, envs, args)
        else:
            exit_code, stdout, stderr = run_polled(sandbox, command, args)

        vprint(stdout)
        if stderr:
            vprint(stderr)

        metrics = sdk_retry(
            "read_metrics",
            lambda: sandbox.files.read("/tmp/recreationbench-output/metrics.json"),
        )
        collect_result_artifacts(
            sandbox,
            args,
            result_dir=task_result_dir(args),
            metrics=metrics,
            stdout=stdout,
            exit_code=exit_code,
        )
        if not local_visual_output_only(args):
            print_metrics_summary(metrics, task_result_dir(args) / "metrics.json")
        return exit_code
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"host_error={error}", flush=True)
        try:
            recovered_log = sdk_retry(
                "recover_remote_log",
                lambda: sandbox.commands.run(f"cat {REMOTE_RUN_LOG} 2>/dev/null || true", timeout=30, request_timeout=60),
                attempts=2,
            )
            stdout = (stdout + "\n" + (recovered_log.stdout or "")).strip()
            if recovered_log.stdout:
                vprint(recovered_log.stdout)
        except Exception as recover_exc:
            vprint(f"recover_log_failed={type(recover_exc).__name__}: {str(recover_exc)[:240]}", flush=True)
        try:
            recovered_metrics = sdk_retry(
                "recover_metrics",
                lambda: sandbox.files.read("/tmp/recreationbench-output/metrics.json"),
                attempts=2,
            )
            if sandbox is not None:
                collect_result_artifacts(
                    sandbox,
                    args,
                    result_dir=task_result_dir(args),
                    metrics=recovered_metrics,
                    stdout=stdout,
                    exit_code=exit_code,
                )
            if not local_visual_output_only(args):
                print_metrics_summary(recovered_metrics, task_result_dir(args) / "metrics.json")
        except Exception:
            synthetic = synthetic_metrics(args, exit_code=exit_code, error=error, stdout=stdout)
            result_dir = task_result_dir(args)
            result_dir.mkdir(parents=True, exist_ok=True)
            write_local_file(result_dir / "run.log", stdout)
            write_local_file(result_dir / "metrics.json", json.dumps(synthetic, indent=2, ensure_ascii=False) + "\n")
            if not local_visual_output_only(args):
                print_metrics_summary(synthetic, result_dir / "metrics.json")
        return 1
    finally:
        keep_sandbox = os.environ.get("RB_KEEP_SANDBOX", "").lower() in {"1", "true", "yes"} or args.visual_only
        if sandbox is not None and not keep_sandbox:
            try:
                sdk_retry("kill_sandbox", lambda: sandbox.kill(), attempts=2)
            except Exception as exc:
                vprint(f"kill_sandbox_failed={type(exc).__name__}: {str(exc)[:240]}", flush=True)
        elif sandbox is not None and keep_sandbox:
            vprint(f"kept_sandbox_id={sandbox.sandbox_id}", flush=True)
        if unified_tmp is not None:
            unified_tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
