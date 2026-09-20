#!/usr/bin/env python3
"""Create one FC Sandbox and run one Web RecreationBench task end-to-end."""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shlex
import shutil
import signal
import tarfile
import tempfile
import time
import sys
from pathlib import Path

from e2b import Sandbox

SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from common.unified_cache import prepare_unified_archive, validate_unified_archive  # noqa: E402
from common.desktop_stream import create_desktop_sandbox, should_use_desktop_stream, start_desktop_stream  # noqa: E402
from common.dotenv_loader import load_dotenv_quiet  # noqa: E402
from common.runtime_source import add_runtime_source, resolve_runtime_source_dir, safe_extract_tar  # noqa: E402
from common.visual_viewer import local_novnc_command, sandbox_novnc_url, sandbox_proxy_tokens, start_local_novnc_server, write_visual_index, write_visual_viewer  # noqa: E402

CHUNK_SIZE = 16 * 1024 * 1024
CONTROLLER_LOG = "/tmp/recreationbench-output/controller.log"
CONTROLLER_EXIT = "/tmp/recreationbench-output/controller.exit"
RESULTS_ARCHIVE = "/tmp/recreationbench-output-artifacts.tar.gz"
RESULT_TAR_EXCLUDES = (
    "./workspace/node_modules",
    "./workspace/.git",
    "./workspace/.cache",
    "./workspace/.npm",
    "./workspace/.vite",
    "./workspace/.next/cache",
    "./workspace/.turbo",
)

# Keep benchmark tuning as a small explicit pass-through list instead of
# forwarding the controller's whole environment.
WEB_PASSTHROUGH_ENVS = (
    "TEMPERATURE",
    "MAX_ITERATIONS",
    "RUNTIME_TIMEOUT_SEC",
    "RB_PERM_SETUP",
    "SCAFFOLD_VERSION",
    "RB_CUA_DRIVER_REF",
    "RB_CUA_PREFLIGHT_MODE",
    "ALLOW_IMAGE_READ",
    "REPLICATE_ALL_PAGES",
    "TIMEOUT_MULTIPLIER",
    "RB_API_TIMEOUT_MS",
    "RB_MCP_TOOL_TIMEOUT",
    "RB_CLAUDE_PROXY_PORT",
    "RB_PROXY_UPSTREAM_TIMEOUT_SECONDS",
    "RB_VLM_BACKEND",
    "RB_VLM_MODE",
    "RB_VLM_MAX_CONCURRENCY",
    "USE_VLM_JUDGE",
    "RB_BROWSER_PREFLIGHT_ATTEMPTS",
    "RB_BROWSER_PREFLIGHT_RETRY_DELAY_SEC",
    "RB_BROWSER_PREFLIGHT_TIMEOUT_SEC",
    "RB_DIRECT_MODEL_KEY_TO_AGENT",
    "RB_WEB_HEADFUL",
    "RB_VISUAL_GEOMETRY",
    "RB_VISUAL_WEB_PORT",
    "RB_VISUAL_RFB_PORT",
    "RB_VISUAL_MODE",
)


class SandboxDiskFullError(RuntimeError):
    """Raised when the target Sandbox cannot fit the uploaded input."""


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def truthy(value: str | None, default: bool = False) -> bool:
    return default if value is None or value == "" else value.lower() in {"1", "true", "yes", "on"}


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


def task_result_dir(args: argparse.Namespace) -> Path:
    base = args.result_dir or Path(os.environ.get("RB_RUN_STATE_DIR", "/tmp/recreationbench-web-runs"))
    if base.name == args.task_id:
        return base
    return base / args.task_id


def visual_viewer_path(args: argparse.Namespace) -> Path:
    return task_result_dir(args) / "visual_viewer.html"


def resolve_recreated_app_dir(args: argparse.Namespace) -> None:
    if not args.visual_only:
        return
    candidates: list[Path] = []
    if args.recreated_app_dir:
        base = args.recreated_app_dir
        candidates.extend(
            [
                base,
                base / "output",
                base / "workspace" / "output",
                base / "artifacts" / "workspace" / "output",
            ]
        )
    else:
        base = task_result_dir(args)
        candidates.extend(
            [
                base / "artifacts" / "workspace" / "output",
                base / "workspace" / "output",
                base / "output",
            ]
        )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            args.recreated_app_dir = candidate
            return


def validate_recreated_app_dir(path: Path) -> None:
    if not path.is_dir():
        raise RuntimeError(f"--recreated-app-dir not found: {path}")
    if not (path / "index.html").is_file():
        raise RuntimeError(f"--recreated-app-dir must contain index.html: {path}")


def download_output_artifacts(sandbox: Sandbox, result_dir: Path) -> Path:
    """Download the sandbox output directory into ``result_dir/artifacts``."""

    result_dir.mkdir(parents=True, exist_ok=True)
    local_archive = result_dir / "artifacts.tar.gz"
    artifacts_dir = result_dir / "artifacts"
    excludes = " ".join(shlex.quote(f"--exclude={pattern}") for pattern in RESULT_TAR_EXCLUDES)
    sandbox.commands.run(
        f"rm -f {shlex.quote(RESULTS_ARCHIVE)}; "
        f"if [ -d /tmp/recreationbench-output ]; then "
        f"tar -czf {shlex.quote(RESULTS_ARCHIVE)} {excludes} -C /tmp/recreationbench-output .; "
        "else "
        "mkdir -p /tmp/recreationbench-empty-output; "
        f"tar -czf {shlex.quote(RESULTS_ARCHIVE)} -C /tmp/recreationbench-empty-output .; "
        "fi; "
        f"wc -c < {shlex.quote(RESULTS_ARCHIVE)}",
        timeout=300,
        request_timeout=360,
    )
    with sandbox.files.read(
        RESULTS_ARCHIVE,
        format="stream",
        request_timeout=120,
        stream_idle_timeout=120,
    ) as stream:
        with local_archive.open("wb") as out:
            for chunk in stream:
                out.write(chunk)
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    safe_extract_tar(local_archive, artifacts_dir)
    local_archive.unlink(missing_ok=True)
    return artifacts_dir


def local_visual_output_only(args: argparse.Namespace, envs: dict[str, str]) -> bool:
    return bool(args.local_viewer and envs.get("RB_ENABLE_VISUAL"))


def publish_visual_viewer(
    sandbox: Sandbox,
    args: argparse.Namespace,
    envs: dict[str, str],
    result_dir: Path,
    *,
    metrics: str | bytes | None = None,
) -> None:
    web_port = int(envs.get("RB_VISUAL_WEB_PORT") or "6080")
    viewer_host = sandbox.get_host(web_port)
    visual_password = envs.get("RB_VISUAL_PASSWORD", "")
    viewer = write_visual_viewer(
        visual_viewer_path(args),
        task_id=args.task_id,
        platform="web",
        sandbox_id=sandbox.sandbox_id,
        host=viewer_host,
        password=visual_password,
    )
    vprint(f"visual_instruction_file={viewer}", flush=True)
    if args.local_viewer:
        try:
            envd_access_token, traffic_access_token = sandbox_proxy_tokens(sandbox)
            local_url = start_local_novnc_server(
                host=viewer_host,
                password=visual_password,
                task_id=args.task_id,
                result_dir=result_dir,
                platform="web",
                sandbox_id=sandbox.sandbox_id,
                port=args.local_viewer_port,
                envd_access_token=envd_access_token,
                traffic_access_token=traffic_access_token,
            )
            visual_index = write_visual_index(
                result_dir,
                task_id=args.task_id,
                platform="web",
                sandbox_id=sandbox.sandbox_id,
                metrics=metrics,
            )
            print(f"visual_url={local_url}", flush=True)
            vprint(f"visual_index_file={visual_index}", flush=True)
            return
        except RuntimeError as exc:
            if not truthy(os.environ.get("RB_ALLOW_RAW_NOVNC_FALLBACK")):
                raise
            vprint(f"local_viewer_failed={exc}; falling_back_to_raw_novnc", flush=True)
    print(f"visual_url={sandbox_novnc_url(viewer_host)}", flush=True)
    vprint(f"visual_local_server_command={local_novnc_command(viewer_host, visual_password, args.task_id)}", flush=True)
    print(f"visual_password={visual_password}", flush=True)


def archive_tree(source: Path) -> bytes:
    with tempfile.TemporaryFile() as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            add_runtime_source(tar, source, archive_root="RecreationBench")
        raw.seek(0)
        return raw.read()


def make_contents_archive(source: Path) -> bytes:
    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(f"directory not found: {source}")
    with tempfile.TemporaryFile() as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            skip = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}

            def filt(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                return None if any(p in skip for p in Path(info.name).parts) else info

            for child in sorted(source.iterdir(), key=lambda p: p.name):
                tar.add(child, arcname=child.name, filter=filt)
        raw.seek(0)
        return raw.read()


def tar_uncompressed_size(payload: bytes | Path) -> int:
    fileobj = None
    try:
        if isinstance(payload, Path):
            tf = tarfile.open(payload, mode="r:gz")
        else:
            fileobj = io.BytesIO(payload)
            tf = tarfile.open(fileobj=fileobj, mode="r:gz")
        with tf:
            return sum(member.size for member in tf.getmembers() if member.isfile())
    finally:
        if fileobj is not None:
            fileobj.close()


def sandbox_free_bytes(sandbox: Sandbox, path: str = "/tmp") -> int:
    result = sandbox.commands.run(
        f"mkdir -p {shlex.quote(path)} && df -Pk {shlex.quote(path)}",
        timeout=30,
        request_timeout=60,
    )
    lines = [line.split() for line in result.stdout.strip().splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[1]) < 4:
        raise RuntimeError(f"could not parse sandbox disk usage for {path}: {result.stdout!r}")
    return int(lines[1][3]) * 1024


def ensure_sandbox_space(sandbox: Sandbox, *, required_bytes: int, label: str, path: str = "/tmp") -> None:
    free = sandbox_free_bytes(sandbox, path)
    if free < required_bytes:
        raise SandboxDiskFullError(
            f"insufficient sandbox free space for {label}: free={free} required={required_bytes} path={path}"
        )


def upload(
    sandbox: Sandbox,
    payload: bytes | Path,
    archive: str,
    root: str,
    label: str,
    *,
    keep_archive: bool = False,
) -> None:
    path = payload if isinstance(payload, Path) else None
    size = path.stat().st_size if path else len(payload)
    reserve_gb = max(0.0, float(os.environ.get("RB_SANDBOX_MIN_FREE_GB", "1")))
    reserve_bytes = int(reserve_gb * 1024**3)
    expanded_bytes = tar_uncompressed_size(payload)
    ensure_sandbox_space(
        sandbox,
        required_bytes=size + expanded_bytes + reserve_bytes,
        label=label,
        path=str(Path(root).parent),
    )
    if size <= 30 * 1024 * 1024:
        content = path.read_bytes() if path else payload
        sandbox.files.write(archive, io.BytesIO(content), request_timeout=180)
        command = (
            f"rm -rf {shlex.quote(root)} && mkdir -p {shlex.quote(root)} "
            f"&& tar -xzf {shlex.quote(archive)} -C {shlex.quote(root)}"
        )
        if not keep_archive:
            command += f" && rm -f {shlex.quote(archive)}"
        sandbox.commands.run(command, timeout=300)
        vprint(f"uploaded_{label}_bytes={size}")
        return
    parts = archive + ".parts"
    sandbox.commands.run(f"rm -rf {shlex.quote(parts)} {shlex.quote(archive)} {shlex.quote(root)} && mkdir -p {shlex.quote(parts)}", timeout=120)
    if path:
        with path.open("rb") as src:
            for index, start in enumerate(range(0, size, CHUNK_SIZE)):
                chunk = src.read(CHUNK_SIZE)
                sandbox.files.write(f"{parts}/part-{index:04d}", chunk, request_timeout=180)
                vprint(f"uploaded_{label}_part={index} bytes={len(chunk)}", flush=True)
    else:
        for index, start in enumerate(range(0, size, CHUNK_SIZE)):
            chunk = payload[start:start + CHUNK_SIZE]
            sandbox.files.write(f"{parts}/part-{index:04d}", chunk, request_timeout=180)
            vprint(f"uploaded_{label}_part={index} bytes={len(chunk)}", flush=True)
    if keep_archive:
        join = (
            f"mkdir -p {shlex.quote(root)} "
            f"&& cat {shlex.quote(parts)}/part-* > {shlex.quote(archive)} "
            f"&& tar -xzf {shlex.quote(archive)} -C {shlex.quote(root)} "
            f"&& rm -rf {shlex.quote(parts)}"
        )
    else:
        join = (
            f"mkdir -p {shlex.quote(root)} "
            f"&& cat {shlex.quote(parts)}/part-* | tar -xzf - -C {shlex.quote(root)} "
            f"&& rm -rf {shlex.quote(parts)} {shlex.quote(archive)}"
        )
    sandbox.commands.run(join, timeout=600)
    vprint(f"uploaded_{label}_bytes={size}")


def write_state(path: Path, **values: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def ensure_visual_helper(sandbox: Sandbox) -> None:
    probe = sandbox.commands.run(
        "if command -v rb-start-visual >/dev/null 2>&1; then echo present; else echo missing; fi",
        timeout=30,
        request_timeout=60,
    ).stdout.strip()
    if probe == "present":
        return
    helper = Path(__file__).resolve().parents[1] / "scripts" / "rb-start-visual"
    if not helper.is_file():
        raise RuntimeError(f"visual helper not found: {helper}")
    sandbox.files.write("/tmp/rb-start-visual", io.BytesIO(helper.read_bytes()), request_timeout=180)
    sandbox.commands.run(
        "if install -m 0755 /tmp/rb-start-visual /usr/local/bin/rb-start-visual 2>/dev/null; then "
        "echo installed; "
        "else mkdir -p /tmp/rb-bin && cp /tmp/rb-start-visual /tmp/rb-bin/rb-start-visual "
        "&& chmod 0755 /tmp/rb-bin/rb-start-visual; fi",
        timeout=30,
        request_timeout=60,
    )


def ensure_visual_runtime(sandbox: Sandbox) -> None:
    missing = sandbox.commands.run(
        "missing=''; "
        "for command in Xvfb xdpyinfo x11vnc websockify nc; do "
        "command -v \"$command\" >/dev/null 2>&1 || missing=\"$missing $command\"; "
        "done; "
        "[ -d /usr/share/novnc ] || [ -d /usr/share/novnc/web ] || missing=\"$missing novnc\"; "
        "echo \"$missing\"",
        timeout=30,
        request_timeout=60,
    ).stdout.strip()
    if not missing:
        return
    if not truthy(os.environ.get("RB_VISUAL_AUTO_INSTALL_DEPS"), default=True):
        raise RuntimeError(f"visual runtime dependencies missing in sandbox: {missing}")
    vprint(f"installing_visual_runtime_deps={missing}", flush=True)
    sandbox.commands.run(
        "if ! command -v apt-get >/dev/null 2>&1; then "
        "echo 'apt-get not found; cannot install visual runtime dependencies' >&2; exit 127; "
        "fi; "
        "run_root() { if [ \"$(id -u)\" = 0 ]; then \"$@\"; else sudo -n \"$@\"; fi; }; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "run_root apt-get update && "
        "run_root apt-get install -y --no-install-recommends "
        "xvfb x11-utils openbox x11vnc novnc websockify netcat-openbsd && "
        "run_root mkdir -p /opt/noVNC/utils && "
        "run_root ln -sfn /usr/share/novnc/app /opt/noVNC/app && "
        "run_root ln -sfn /usr/share/novnc/core /opt/noVNC/core && "
        "run_root ln -sfn /usr/share/novnc/include /opt/noVNC/include && "
        "run_root ln -sfn /usr/share/novnc/vendor /opt/noVNC/vendor && "
        "run_root ln -sfn /usr/share/novnc/vnc.html /opt/noVNC/vnc.html && "
        "run_root ln -sfn /usr/share/novnc/vnc_lite.html /opt/noVNC/vnc_lite.html",
        timeout=600,
        request_timeout=660,
    )


def launch_runner(sandbox: Sandbox, local_root: str, runner: str = "rb-web-sandbox-run") -> int:
    command = (
        f"rm -f {shlex.quote(CONTROLLER_EXIT)}; "
        f"nohup sh -c '{shlex.quote(runner)} >{CONTROLLER_LOG} 2>&1; "
        f"echo $? >{CONTROLLER_EXIT}' >/dev/null 2>&1 & echo $!"
    )
    result = sandbox.commands.run(
        command, envs={"RB_UNIFIED_LOCAL_ROOT": local_root}, timeout=30, request_timeout=60
    )
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"could not obtain remote runner pid: {result.stdout!r} {result.stderr!r}") from exc


def wait_runner(sandbox: Sandbox, pid: int, poll_interval: int) -> int:
    while True:
        try:
            probe = sandbox.commands.run(
                f"if kill -0 {pid} 2>/dev/null; then echo running; "
                f"elif [ -f {shlex.quote(CONTROLLER_EXIT)} ]; then cat {shlex.quote(CONTROLLER_EXIT)}; "
                "else echo unknown; fi",
                timeout=30, request_timeout=60,
            ).stdout.strip()
        except Exception as exc:
            # The runner is already detached inside the Sandbox.  A transient
            # controller/API timeout must never turn into a task cancellation;
            # keep its state file and retry the short probe on the next tick.
            vprint(f"runner_pid={pid} status=poll_error error={type(exc).__name__}; retrying", flush=True)
            time.sleep(poll_interval)
            continue
        if probe == "running":
            vprint(f"runner_pid={pid} status=running", flush=True)
            time.sleep(poll_interval)
            continue
        if probe.isdigit():
            return int(probe)
        raise RuntimeError(f"remote runner disappeared without an exit code (pid={pid}, probe={probe!r})")


def launch_visual_only_site(sandbox: Sandbox, *, site_root: str, task_id: str) -> None:
    command = f"""
set -e
export DISPLAY="${{DISPLAY:-:99}}"
mkdir -p /tmp/recreationbench-output
if ! rb-start-visual; then
  code="$?"
  for log in /tmp/recreationbench-output/visual-xvfb.log /tmp/recreationbench-output/visual-x11vnc.log /tmp/recreationbench-output/visual-websockify.log; do
    if [ -f "$log" ]; then
      echo "===== $log =====" >&2
      tail -80 "$log" >&2 || true
    fi
  done
  exit "$code"
fi
cd {shlex.quote(site_root)}
python3 -m http.server 4173 --bind 127.0.0.1 >/tmp/recreationbench-output/visual-site.log 2>&1 &
echo "$!" >/tmp/recreationbench-output/visual-site.pid
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:4173/ >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:4173/ >/dev/null
browser=""
if command -v chromium >/dev/null 2>&1; then
  browser="$(command -v chromium)"
elif command -v chromium-browser >/dev/null 2>&1; then
  browser="$(command -v chromium-browser)"
else
  browser="$(find /ms-playwright -path '*/chrome-linux*/chrome' -type f 2>/dev/null | head -n 1)"
fi
if [ -z "$browser" ]; then
  echo "chromium executable not found" >&2
  exit 69
fi
"$browser" --no-sandbox --disable-dev-shm-usage --window-size=1280,720 \
  http://127.0.0.1:4173/ >/tmp/recreationbench-output/visual-browser.log 2>&1 &
echo "$!" >/tmp/recreationbench-output/visual-browser.pid
rb-controller-helper write-visual-metrics \
  --task-id {shlex.quote(task_id)} \
  --platform web \
  --output /tmp/recreationbench-output/metrics.json \
  --url http://127.0.0.1:4173/
"""
    sandbox.commands.run(command, timeout=120, request_timeout=180)


def main() -> int:
    template_env = Path(__file__).resolve().parent / ".env.web"
    service_env = Path(__file__).resolve().parents[2] / ".env.web"
    load_dotenv_quiet()
    load_dotenv_quiet(os.environ.get("RB_ENV_FILE", template_env), override=False)
    load_dotenv_quiet(service_env, override=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", default="recreation_eval")
    parser.add_argument("--agent-cli", default=os.environ.get("RB_AGENT_CLI", "codex"))
    parser.add_argument("--mcp-provider", default=os.environ.get("RB_MCP_PROVIDER", "playwright"), choices=("playwright", "cua-driver"))
    parser.add_argument("--template", default=os.environ.get("RB_WEB_TEMPLATE", ""))
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="bundled pipeline runtime; override only to test another frozen runtime",
    )
    parser.add_argument("--unified-archive", type=Path, default=Path(os.environ["RB_UNIFIED_ARCHIVE"]) if os.environ.get("RB_UNIFIED_ARCHIVE") else None, help="reuse a pre-fetched unified input archive")
    parser.add_argument("--unified-cache-dir", type=Path, default=Path(os.environ["RB_UNIFIED_CACHE_DIR"]) if os.environ.get("RB_UNIFIED_CACHE_DIR") else None, help="look for a local unified archive or directory")
    parser.add_argument("--write-unified-archive", type=Path, help="materialize a local unified input archive, then exit")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=int(os.environ.get("RB_SANDBOX_TIMEOUT_SEC") or os.environ.get("RUNTIME_TIMEOUT_SEC", "86400")),
    )
    parser.add_argument("--repro-mode", action=argparse.BooleanOptionalAction, default=truthy(os.environ.get("RB_REPRO_MODE"), True))
    parser.add_argument("--prefetch-unified", action=argparse.BooleanOptionalAction, default=truthy(os.environ.get("RB_PREFETCH_UNIFIED"), True))
    parser.add_argument("--visual", action="store_true", default=truthy(os.environ.get("RB_ENABLE_VISUAL")), help="start a noVNC viewer on port 6080 and print the URL")
    parser.add_argument("--visual-only", action="store_true", default=truthy(os.environ.get("RB_VISUAL_ONLY")), help="show an existing Web output directory without running agent or eval")
    parser.add_argument("--recreated-app-dir", type=Path, default=Path(os.environ["RB_PRELOADED_RECREATION_DIR"]) if os.environ.get("RB_PRELOADED_RECREATION_DIR") else None, help="existing Web output directory for --visual-only; must contain index.html")
    parser.add_argument("--local-viewer", action=argparse.BooleanOptionalAction, default=truthy(os.environ.get("RB_LOCAL_VIEWER"), default=True), help="start and healthcheck a local loopback noVNC viewer when visual is enabled")
    parser.add_argument("--local-viewer-port", type=int, default=int(os.environ.get("RB_LOCAL_VIEWER_PORT", "0")), help="local viewer port; 0 picks a free port")
    parser.add_argument("--runner-override", type=Path, help="temporarily install this runner into the created Sandbox before launch")
    parser.add_argument("--verify-override", type=Path, help="temporarily install this verifier with --runner-override")
    parser.add_argument("--poll-interval", type=int, default=int(os.environ.get("RB_POLL_INTERVAL", "15")))
    parser.add_argument("--result-dir", type=Path, help="local directory for metrics and downloaded sandbox artifacts")
    parser.add_argument("--state-file", type=Path)
    args = parser.parse_args()
    platform = os.environ.get("RB_UNIFIED_PLATFORM", "web").strip() or "web"
    args.source_dir = resolve_runtime_source_dir(
        service_root=SERVICE_ROOT,
        platform=platform,
        source_dir=args.source_dir,
    )
    resolve_recreated_app_dir(args)

    if args.visual_only:
        args.visual = True
        if not args.recreated_app_dir:
            raise RuntimeError(
                "--visual-only requires --recreated-app-dir or "
                f"{task_result_dir(args) / 'artifacts' / 'workspace' / 'output'}"
            )
        validate_recreated_app_dir(args.recreated_app_dir)

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

    template = args.template or required("RB_WEB_TEMPLATE")
    if args.unified_archive:
        validate_unified_archive(args.unified_archive, task_id=args.task_id, platform=platform)
    if args.unified_archive:
        vprint(f"using_unified_archive={args.unified_archive}", flush=True)
    envs = {
        "TASK_ID": args.task_id, "STAGE": args.stage, "RB_AGENT_CLI": args.agent_cli,
        "RB_MCP_PROVIDER": args.mcp_provider, "RB_OUTPUT_DIR": "/tmp/recreationbench-output",
        "RB_PIPELINE_DIR": "/workspace/RecreationBench", "RB_SOURCE_ARCHIVE": "/tmp/recreationbench-source.tar.gz",
    }
    if args.visual_only:
        envs["RB_VISUAL_ONLY"] = "1"
    for name in (
        "RB_MODEL",
        "RB_MODEL_BASE_URL",
        "RB_MODEL_API_KEY",
        "RB_VLM_KEY",
        "RB_VLM_MODEL",
        "RB_VLM_BASE_URL",
        "RB_UNIFIED_PLATFORM",
        "RB_UNIFIED_APP",
        "RB_WEB_DOMAIN",
    ):
        if os.environ.get(name):
            envs[name] = os.environ[name]
    for name in WEB_PASSTHROUGH_ENVS:
        if os.environ.get(name):
            envs[name] = os.environ[name]
    if args.visual:
        envs["RB_ENABLE_VISUAL"] = "1"
        envs.setdefault("RB_WEB_HEADFUL", "1")
        envs.setdefault("RB_VISUAL_PASSWORD", secrets.token_urlsafe(12))
        envs.setdefault("RB_VISUAL_MODE", "novnc" if args.visual_only else "desktop_stream")
    if args.stage != "setup" and not args.visual_only:
        required("RB_MODEL")
    metadata = {"task_id": args.task_id, "platform": "web", "rb_mode": "repro" if args.repro_mode else "strict"}
    vpc = os.environ.get("RB_SANDBOX_VPC_CONFIG", "").strip()
    if vpc:
        json.loads(vpc)
        metadata["fc.sandbox.network.vpc"] = vpc
    opts = {"template": template, "timeout": args.timeout_sec, "envs": envs, "metadata": metadata,
            "api_key": required("E2B_API_KEY"), "api_url": required("E2B_API_URL"), "domain": required("E2B_DOMAIN"),
            "allow_internet_access": True}
    result_dir = task_result_dir(args)
    state_file = args.state_file or result_dir / "state.json"
    sandbox: Sandbox | None = None
    remote_started = False
    completed = False
    keep_requested = truthy(os.environ.get("RB_KEEP_SANDBOX"))
    unified_tmp: tempfile.TemporaryDirectory[str] | None = None
    unified_archive_to_upload: Path | None = args.unified_archive
    if not unified_archive_to_upload and args.prefetch_unified and not args.visual_only:
        unified_tmp = tempfile.TemporaryDirectory(prefix="rb_web_unified_archive_")
        unified_archive_to_upload = prepare_unified_archive(
            task_id=args.task_id,
            platform=platform,
            archive=None,
            cache_dir=args.unified_cache_dir,
            output_archive=Path(unified_tmp.name) / "unified.tar.gz",
        )
        vprint(f"using_unified_archive={unified_archive_to_upload}", flush=True)

    def cleanup(_signum: int | None = None, _frame: object | None = None) -> None:
        keep_sandbox = keep_requested or (args.visual_only and completed)
        if sandbox is not None and not keep_sandbox:
            try:
                sandbox.kill()
            except Exception:
                pass
        if unified_tmp is not None:
            unified_tmp.cleanup()
        state_file.unlink(missing_ok=True)
        if _signum is not None:
            raise SystemExit(128 + _signum)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    if should_use_desktop_stream(args) and not args.visual_only:
        sandbox = create_desktop_sandbox(
            opts,
            display=os.environ.get("RB_DESKTOP_DISPLAY", ":99"),
            geometry=envs.get("RB_VISUAL_GEOMETRY"),
        )
        envs["RB_VISUAL_PASSWORD"] = start_desktop_stream(sandbox)
        envs["RB_VISUAL_MODE"] = "desktop_stream"
    else:
        sandbox = Sandbox.create(**opts)
    write_state(state_file, sandbox_id=sandbox.sandbox_id, task_id=args.task_id, status="created")
    if not local_visual_output_only(args, envs):
        print(f"result_dir={result_dir}", flush=True)
        print(f"sandbox_id={sandbox.sandbox_id}", flush=True)
    if envs.get("RB_ENABLE_VISUAL") and not args.visual_only:
        publish_visual_viewer(sandbox, args, envs, result_dir)
    try:
        if args.visual_only:
            assert args.recreated_app_dir is not None
            ensure_visual_helper(sandbox)
            ensure_visual_runtime(sandbox)
            upload(
                sandbox,
                make_contents_archive(args.recreated_app_dir),
                "/tmp/recreationbench-web-visual-site.tar.gz",
                "/tmp/recreationbench-output/workspace/output",
                "recreated_app",
            )
            launch_visual_only_site(
                sandbox,
                site_root="/tmp/recreationbench-output/workspace/output",
                task_id=args.task_id,
            )
            metrics = sandbox.files.read("/tmp/recreationbench-output/metrics.json")
            metrics_path = result_dir / "metrics.json"
            result_dir.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(metrics)
            publish_visual_viewer(sandbox, args, envs, result_dir, metrics=metrics)
            write_visual_index(
                result_dir,
                task_id=args.task_id,
                platform="web",
                sandbox_id=sandbox.sandbox_id,
                metrics=metrics,
            )
            completed = True
            if not local_visual_output_only(args, envs):
                print_metrics_summary(metrics, metrics_path)
            return 0

        upload(
            sandbox,
            archive_tree(args.source_dir),
            "/tmp/recreationbench-source.tar.gz",
            "/tmp/recreationbench-source",
            "source",
            keep_archive=True,
        )
        if bool(args.runner_override) != bool(args.verify_override):
            raise RuntimeError(
                "--runner-override and --verify-override must be used together"
            )
        runner = args.runner_override or SERVICE_ROOT / "web" / "bin" / "rb-web-sandbox-run"
        verifier = args.verify_override or SERVICE_ROOT / "web" / "scripts" / "rb-web-verify"
        visual_helper = SERVICE_ROOT / "web" / "scripts" / "rb-start-visual"
        controller_helper = SERVICE_ROOT / "common" / "controller_helper.py"
        for local, remote in (
            (runner, "/tmp/rb-web-sandbox-run"),
            (verifier, "/tmp/rb-web-verify"),
            (visual_helper, "/tmp/rb-start-visual"),
            (controller_helper, "/tmp/rb-controller-helper"),
        ):
            if not local.is_file():
                raise RuntimeError(f"runner script not found: {local}")
            sandbox.files.write(
                remote,
                io.BytesIO(local.read_bytes()),
                request_timeout=180,
            )
            vprint(f"uploaded_runner_override={local.name} bytes={local.stat().st_size}", flush=True)
        sandbox.commands.run(
            "sudo -n install -m 0755 /tmp/rb-web-sandbox-run "
            "/usr/local/bin/rb-web-sandbox-run && "
            "sudo -n install -m 0755 /tmp/rb-web-verify /usr/local/bin/rb-web-verify && "
            "sudo -n install -m 0755 /tmp/rb-start-visual /usr/local/bin/rb-start-visual && "
            "sudo -n install -m 0755 /tmp/rb-controller-helper /usr/local/bin/rb-controller-helper",
            timeout=30,
            request_timeout=60,
        )
        local_root = ""
        if unified_archive_to_upload:
            upload(sandbox, unified_archive_to_upload, "/tmp/recreationbench-unified.tar.gz", "/tmp/recreationbench-unified", "unified")
            local_root = "/tmp/recreationbench-unified"
        remote_pid = launch_runner(sandbox, local_root)
        remote_started = True
        write_state(state_file, sandbox_id=sandbox.sandbox_id, task_id=args.task_id, status="running", remote_pid=remote_pid)
        vprint(f"runner_pid={remote_pid}", flush=True)
        code = wait_runner(sandbox, remote_pid, max(1, args.poll_interval))
        controller_log = sandbox.files.read(CONTROLLER_LOG)
        vprint(controller_log)
        metrics = sandbox.files.read("/tmp/recreationbench-output/metrics.json")
        metrics_path = result_dir / "metrics.json"
        result_dir.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(metrics)
        artifacts_dir = download_output_artifacts(sandbox, result_dir)
        vprint(f"artifacts_dir={artifacts_dir}", flush=True)
        write_visual_index(
            result_dir,
            task_id=args.task_id,
            platform="web",
            sandbox_id=sandbox.sandbox_id,
            metrics=metrics,
        )
        if not local_visual_output_only(args, envs):
            print_metrics_summary(metrics, metrics_path)
        completed = True
        return code
    finally:
        # Before the runner starts, a failure means there is nothing useful to
        # preserve.  Once it has been handed off, preserve the state file and
        # Sandbox on controller failures; the caller can reconnect or run
        # cleanup_run.py explicitly.  Normal completion still cleans up.
        if completed or not remote_started:
            cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
