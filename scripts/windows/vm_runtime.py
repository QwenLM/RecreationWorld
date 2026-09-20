#!/usr/bin/env python3
"""Pod-side Windows VM runtime for the standard platform pipeline.

Runs on a Linux machine, controls a single Windows VM via SSH/SFTP.
All configuration is via environment variables.

The release token runs recreation and eval sequentially in the same VM. Before
each selected stage the orchestrator materializes its frozen inputs and any
in-run prerequisite artifacts from the local ``results/`` directory.

Usage:
    export INSTANCE_ID=MyApp RB_UNIFIED_PREFIX=... ...
    python vm_runtime.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

# scripts/ on the path so `core` resolves: this file is launched directly, so sys.path[0]
# is scripts/windows, not scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import agent_invocation as core_agent_invocation
from core import cua_driver as core_cua
from core import exit_contract, runtime_assets
from core import trajectory as core_trajectory
from core.artifact_store import ArtifactRetryPolicy, ArtifactStore
from core.pipeline import (  # noqa: E402
    RELEASE_STAGES,
    normalize_cua_preflight_mode,
    normalize_eval_target,
    stages_for,
)
from core.vmclient import VmClient  # noqa: E402
from infrastructure.artifacts import (  # noqa: E402
    ArtifactStoreConfigurationError,
    artifact_store_from_environment,
    download_tree,
    upload_tree,
)

_T0 = time.time()

_ARTIFACT_RETRY_POLICY = ArtifactRetryPolicy(
    attempts=10,
    retry_statuses=(),
    base_delay_seconds=1.0,
    max_delay_seconds=90.0,
    jitter=(0.5, 1.5),
)


def _artifact_store():
    """Create the transport adapter at the Windows runtime boundary."""
    return artifact_store_from_environment(
        os.environ,
        retry_policy=_ARTIFACT_RETRY_POLICY,
        log=lambda message: log(f"  {message}"),
    )


def _artifact_prefix(*, restore: bool = False) -> str:
    """Return the provider-neutral run prefix with legacy artifact store compatibility."""
    if restore:
        return (
            os.environ.get("RB_ARTIFACT_RESTORE_PREFIX", "")
            or os.environ.get("RB_ARTIFACT_PREFIX", "")
        ).rstrip("/")
    return os.environ.get("RB_ARTIFACT_PREFIX", "").rstrip("/")


class FrozenInputError(RuntimeError):
    """The frozen benchmark input was reached but is missing or malformed."""


def log(msg: str) -> None:
    elapsed = time.time() - _T0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} +{elapsed:.0f}s] {msg}", flush=True)


REMOTE_CODE_DIR = r"C:\RecreationBench"
VM_BASE = r"C:\rb_pipeline"


def _task_hash(task_id: str) -> str:
    if os.environ.get("RB_MASK_TASK_ID", "1").lower() in ("0", "false"):
        return task_id
    return hashlib.sha256(task_id.encode()).hexdigest()[:16]


# Stage internal timeouts — passed to worker.py's threading.Timer via --xxx-timeout.
# Configurable via env vars; eval has no internal timer so is excluded.  A frozen
# reference check can spend substantially longer in the VLM judge's rate-limit
# backoff than a normal candidate eval, so give only that mode a larger default.
def _default_eval_timeout() -> int:
    return (
        21600
        if normalize_eval_target(os.environ.get("RB_EVAL_TARGET")) == "reference"
        else 7200
    )


STAGE_TIMEOUTS = {
    "recreation": int(os.environ.get("RB_RECREATION_TIMEOUT", "72000")),
    "eval": int(os.environ.get("RB_EVAL_TIMEOUT", str(_default_eval_timeout()))),
}
POLL_INTERVAL = 60
# Written by runner.ps1 before it does anything slow, and named per stage inside so a leftover
# from the previous stage cannot be mistaken for this one having started.
STAGE_START_FILE = "stage_start.txt"
# How long a stage launch waits for the previous stage's scheduled-task instance to finish.  The
# epilogue it is waiting on is bounded by two Get-WinEvent queries, so this only has to cover a
# loaded VM, not an agent run.
TASK_DRAIN_TIMEOUT = 180
# A scheduled task normally becomes Running, or executes the runner's first instruction, within a
# few seconds.  Keep the confirmation bounded so an accepted-looking but inert launch becomes an
# explicit infrastructure error instead of falling through to poll_stage as "pipeline_not_started".
TASK_START_CONFIRM_TIMEOUT = 30
# The scheduler's own timer, as a second activation path independent of Start-ScheduledTask -- see
# make_launcher_ps1 for why a client-side retry cannot replace it.
TASK_TRIGGER_DELAY = 3
# Sleeps before each of poll_stage's start-confirmation rounds.  The launcher's confirmation and this
# one answer different questions.  The launcher waits for the task to become Running or the marker to
# appear, so it catches an activation that never took; a process that starts, opens the marker in its
# third statement and dies before its fourth leaves State=Running and a created file, which the
# launcher correctly reports as Started -- and this loop is what has to survive it.  That is the
# failure mode where the marker is truncated while pipeline.log remains from the previous stage.
#
# Six rounds, not the three (45s) this started as, because State=Running is entered when the
# scheduler ACCEPTS an instance and process creation can lag by tens of seconds. Nothing before
# the marker is expensive, so the delay occurs inside Task Scheduler before PowerShell starts.
# The longer confirmation window is small relative to the evaluation budget and prevents a valid
# delayed launch from being classified as dead.
STAGE_START_ROUNDS = (15, 30, 45, 60, 75, 90)
# Reason codes that mean runner.ps1 never ran for the stage, so the pipeline.log on the VM is the
# previous stage's and must not be quoted as this stage's failure.
STAGE_NEVER_RAN_REASONS = frozenset({"pipeline_not_started", "stage_launch_blocked"})
# The one scheduled task every stage reuses.  Shared on purpose -- see make_launcher_ps1.
TASK_NAME = "RB_Pipeline"
# 0x800710E0, ERROR_OPERATOR_OR_ADMINISTRATOR_HAS_REFUSED_THE_REQUEST: what the scheduler returns
# when it declines to start a new instance because one is already running.
TASK_REFUSED_CODE = 2147946720
# worker.py writes its heartbeat every 30s, so five missed beats is a real gap rather than a slow
# poll, and the recheck window is one beat plus margin.
HEARTBEAT_STALE_SECONDS = 150
HEARTBEAT_RECHECK_SECONDS = 45
# One reference build attempt. Nuitka/PyInstaller recipes for a Qt app measurably run past 20
# minutes on a loaded sandbox host, and three jobs lost the same app to a 1200s cut in one
# 8-minute window: each spent exactly 1200s compiling, then both remaining attempts died in
# seconds because the killed compiler still held the build dir open.
REFERENCE_BUILD_TIMEOUT = 1800
# Extra time beyond STAGE_TIMEOUTS before orchestrator force-kills.
# Gives the stage time to clean up, run post-agent baseline verification,
# and write pipeline_state.json after its own threading.Timer fires.
# Must be >= baseline timeout (1200s) + margin for cleanup/verify.
TIMEOUT_BUFFER = 1500

REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

REQUIRED_VARS = [
    "INSTANCE_ID",
    "STAGE",
    "SANDBOX_IP",
    "SANDBOX_USERNAME",
    "SANDBOX_PASSWORD",
]

REQUIRED_VARS_LLM = [
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
]

REQUIRED_VARS_VLM = [
    "VLM_MODEL",
    "VLM_MODEL_API_KEY",
    "VLM_MODEL_BASE_URL",
]


def make_launcher_ps1(vm_tid: str) -> str:
    runner_path = f"{VM_BASE}\\{vm_tid}\\runner.ps1"
    start_path = f"{VM_BASE}\\{vm_tid}\\{STAGE_START_FILE}"
    return runtime_assets.render_text(
        "windows/runtime/launcher.ps1",
        {
            "__TASK_DRAIN_TIMEOUT__": TASK_DRAIN_TIMEOUT,
            "__START_PATH__": start_path,
            "__RUNNER_PATH__": runner_path,
            "__TASK_TRIGGER_DELAY__": TASK_TRIGGER_DELAY,
            "__TASK_START_CONFIRM_TIMEOUT__": TASK_START_CONFIRM_TIMEOUT,
            "__TASK_REFUSED_CODE__": TASK_REFUSED_CODE,
        },
    )


RUNTIME_ARCHIVE_ROOTS = ("scripts", "src", "pyproject.toml")
RUNTIME_ARCHIVE_EXCLUDES = {
    ".git",
    ".local-backups",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "results",
    "tests",
}


def _runtime_archive_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = PurePosixPath(info.name).parts
    if parts[:1] == ("RecreationBench",):
        parts = parts[1:]
    if not parts:
        return info
    if parts[0] not in RUNTIME_ARCHIVE_ROOTS:
        return None
    if parts[:2] == ("scripts", "release"):
        return None
    if any(part in RUNTIME_ARCHIVE_EXCLUDES or part.endswith(".egg-info") for part in parts):
        return None
    if any(part.startswith((".env", "results-")) for part in parts):
        return None
    if parts[-1].endswith((".bak", ".orig", ".pyc", ".pyo", ".rej", "~")):
        return None
    if info.issym() or info.islnk():
        return None
    return info

CUA_DRIVER_UPGRADE_PS1 = runtime_assets.load_text(
    "windows/runtime/cua_driver_upgrade.ps1"
)


def _powershell_runtime_command(
    name: str, replacements: dict[str, object] | None = None
) -> str:
    """Render a PowerShell asset through the existing SSH command transport."""

    source = runtime_assets.render_text(f"windows/runtime/{name}", replacements or {})
    if not source.endswith("\n"):
        raise ValueError(
            f"Windows runtime PowerShell asset must end with a newline: {name}"
        )
    return 'powershell "' + source[:-1] + '"'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    missing = []
    for var in REQUIRED_VARS:
        val = os.environ.get(var, "").strip()
        if not val:
            missing.append(var)
        cfg[var] = val
    if missing:
        print("ERROR: Missing required environment variables:", file=sys.stderr)
        for v in missing:
            print(f"  - {v}", file=sys.stderr)
        sys.exit(1)
    try:
        stages_to_run = set(stages_for(cfg["STAGE"]))
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    for var in (*REQUIRED_VARS_LLM, *REQUIRED_VARS_VLM):
        val = os.environ.get(var, "").strip()
        cfg[var] = val
    # MODEL is the real upstream id used for stage identity and result metadata.
    # Keep the fixed Claude alias independent even for old direct invocations
    # whose ANTHROPIC_MODEL still carried the upstream route.
    cfg["MODEL"] = os.environ.get("MODEL", "").strip() or cfg["ANTHROPIC_MODEL"]
    cfg["CLAUDE_MODEL"] = core_agent_invocation.claude_client_model(
        os.environ.get("CLAUDE_MODEL", ""),
        context_1m=os.environ.get("RB_CONTEXT_1M", "").strip().lower()
        in ("1", "true", "yes", "on"),
    )
    stage_requirements = {
        "recreation": REQUIRED_VARS_LLM,
        "eval": REQUIRED_VARS_VLM,
    }
    for stage, required in stage_requirements.items():
        if stage in stages_to_run:
            stage_missing = [v for v in required if not cfg[v]]
        else:
            stage_missing = []
        if stage_missing:
            print(
                f"ERROR: Missing env vars required for {stage}:",
                file=sys.stderr,
            )
            for v in stage_missing:
                print(f"  - {v}", file=sys.stderr)
            sys.exit(1)
    try:
        cfg["RB_CUA_PREFLIGHT_MODE"] = normalize_cua_preflight_mode(
            os.environ.get("RB_CUA_PREFLIGHT_MODE")
        )
        cfg["RB_EVAL_TARGET"] = normalize_eval_target(os.environ.get("RB_EVAL_TARGET"))
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    return cfg


# ---------------------------------------------------------------------------
# SSH / SFTP helpers
# ---------------------------------------------------------------------------


def ssh_connect(cfg: dict[str, str]) -> VmClient:
    """The shared transport, pointed at the Windows sandbox.

    remote_sep="\\" matters: the recursive transfers join remote paths with it, and this guest is
    Windows. The connection itself is lazy and self-healing, so a dropped channel between stages
    is re-established on the next command instead of failing the run.
    """
    log(f"Connecting SSH to {cfg['SANDBOX_IP']} as {cfg['SANDBOX_USERNAME']}...")
    client = VmClient(
        host=cfg["SANDBOX_IP"],
        user=cfg["SANDBOX_USERNAME"],
        password=cfg["SANDBOX_PASSWORD"],
        connect_timeout=30,
        remote_sep="\\",
    )
    client.keep_session_open()
    return client


def ssh_exec(
    ssh: VmClient, cmd: str, *, timeout: int = 120, quiet: bool = False
) -> str:
    cmd_preview = cmd[:120] + ("..." if len(cmd) > 120 else "")
    if not quiet:
        log(f"SSH exec: {cmd_preview}")
    res = ssh.run(cmd, timeout=timeout, check=False)
    if not quiet and res.stderr.strip():
        print(f"  [stderr] {res.stderr.strip()[:200]}", file=sys.stderr)
    return res.stdout.strip()


def sftp_upload_text(ssh: VmClient, content: str, remote_path: str) -> None:
    log(f"SFTP upload text ({len(content)} bytes) -> {remote_path}")
    ssh.upload_text(content, remote_path)


def sftp_upload_dir(
    ssh: VmClient,
    local_dir: str,
    remote_dir: str,
    exclude_dirs: set[str] | None = None,
) -> int:
    return ssh.upload_dir(local_dir, remote_dir, exclude_dirs)


def sftp_download_dir(ssh: VmClient, remote_dir: str, local_dir: str) -> int:
    return ssh.download_dir(remote_dir, local_dir)


# ---------------------------------------------------------------------------
# Code sync
# ---------------------------------------------------------------------------


def sync_code(ssh: VmClient, cfg: dict | None = None) -> None:
    log("--- Syncing pipeline code to VM ---")
    ssh_exec(
        ssh,
        f"powershell \"Remove-Item -Recurse -Force '{REMOTE_CODE_DIR}' -EA SilentlyContinue\"",
        timeout=60,
    )

    descriptor, tar_path = tempfile.mkstemp(prefix="rb-windows-runtime-", suffix=".tar.gz")
    os.close(descriptor)
    archive_name = Path(tar_path).name
    try:
        with tarfile.open(tar_path, "w:gz") as archive:
            for name in RUNTIME_ARCHIVE_ROOTS:
                source = Path(REPO_ROOT) / name
                if source.exists():
                    archive.add(
                        source,
                        arcname=f"RecreationBench/{name}",
                        filter=_runtime_archive_filter,
                    )
        tar_size_mb = os.path.getsize(tar_path) / (1024 * 1024)
        log(f"Archive created: {tar_path} ({tar_size_mb:.1f} MB)")

        t0 = time.time()
        ssh.upload_file(tar_path, f"C:/{archive_name}")
        log(f"Archive uploaded in {time.time() - t0:.1f}s")

        ssh_exec(
            ssh,
            f'powershell "cd C:\\; tar xzf {archive_name}; Remove-Item {archive_name}"',
            timeout=60,
        )
    finally:
        Path(tar_path).unlink(missing_ok=True)

    # Verify the release runner and both executable stages exist.
    verify = ssh_exec(
        ssh,
        f"powershell \"Test-Path '{REMOTE_CODE_DIR}\\scripts\\windows\\worker.py'; "
        f"Test-Path '{REMOTE_CODE_DIR}\\scripts\\windows\\stages\\recreation.py'; "
        f"Test-Path '{REMOTE_CODE_DIR}\\scripts\\windows\\stages\\uia_eval.py'\"",
        timeout=15,
        quiet=True,
    )
    lines = [line.strip() for line in verify.splitlines() if line.strip()]
    if len(lines) == 3 and all(line == "True" for line in lines):
        log("Code synced and verified")
    else:
        log(f"ERROR: Code sync verification failed: {lines}")
        listing = ssh_exec(
            ssh,
            f"powershell \"Get-ChildItem '{REMOTE_CODE_DIR}' -Recurse -Name -EA SilentlyContinue | Select -First 30\"",
            timeout=15,
            quiet=True,
        )
        log(f"Remote listing:\n{listing}")
        sys.exit(1)

    cfg = cfg or {}
    skip_dependencies = cfg.get("RB_WINDOWS_SKIP_DEP_INSTALL", "1").lower() in {
        "1", "true", "yes", "on"
    }
    if skip_dependencies:
        log("Using Python dependencies from the prepared Windows target")
    else:
        requirements_local = (
            Path(REPO_ROOT) / "providers" / "windows" / "provision" / "requirements.windows.txt"
        )
        if not requirements_local.is_file():
            raise FileNotFoundError(f"Windows requirements not found: {requirements_local}")
        requirements_remote = "C:/rb_requirements_windows.txt"
        ssh.upload_file(str(requirements_local), requirements_remote)
        try:
            log("Installing pinned Python dependencies on VM...")
            result = ssh.run(
                "python -m pip install --index-url https://pypi.org/simple "
                "--requirement C:\\rb_requirements_windows.txt --quiet",
                timeout=900,
                check=False,
            )
            if result.rc:
                raise RuntimeError(
                    "Windows dependency installation failed: "
                    + (result.stderr or result.stdout)[-1200:]
                )
        finally:
            ssh.run(
                'powershell -NoProfile -Command "Remove-Item C:\\rb_requirements_windows.txt '
                '-Force -ErrorAction SilentlyContinue"',
                timeout=30,
                check=False,
            )
        log("Python dependencies installed")

    skip_agent = cfg.get("RB_WINDOWS_SKIP_AGENT_INSTALL", "1").lower() in {
        "1", "true", "yes", "on"
    }
    agent_cli = cfg.get("RB_AGENT_CLI", "claude")
    if agent_cli.startswith("codex"):
        if not skip_agent:
            log("Installing OpenAI Codex CLI on VM...")
            ssh_exec(ssh, "npm install -g @openai/codex@0.145.0", timeout=120)
        codex_version = ssh_exec(ssh, "codex --version", timeout=15).strip()
        if not re.search(r"(?<![0-9.])0\.145\.0(?![0-9.])", codex_version):
            raise RuntimeError(
                f"Codex CLI version mismatch: {codex_version!r}"
            )
        log(f"Codex CLI verified: {codex_version}")
    else:
        if not skip_agent:
            log("Installing Claude Code on VM...")
            ssh_exec(ssh, "npm install -g @anthropic-ai/claude-code@2.1.177", timeout=120)
        claude_version = ssh_exec(ssh, "claude --version", timeout=15).strip()
        if not re.search(r"(?<![0-9.])2\.1\.177(?![0-9.])", claude_version):
            raise RuntimeError(f"Claude Code version mismatch: {claude_version!r}")
        log(f"Claude Code verified: {claude_version}")

    try:
        cua_ver = ssh_exec(ssh, "cua-driver --version", timeout=10).strip()
        log(f"CUA Driver version: {cua_ver}")
    except Exception as exc:
        log(f"WARNING: could not get cua-driver version: {exc}")


def configure_cua_driver_env(
    ssh: VmClient,
    coordinate_space: str = "0",
    coordinate_scale: str = "1000",
    mcp_model_payload_filter: str = "0",
    driver_binary: str = "cua-driver",
) -> None:
    """Set CUA Driver coordinate env vars at Machine level and restart serve."""
    log("--- Configuring CUA Driver environment variables ---")
    script = runtime_assets.render_text(
        "windows/runtime/configure_cua_env.ps1",
        {
            "__COORDINATE_SPACE__": coordinate_space,
            "__COORDINATE_SCALE__": coordinate_scale,
            "__MCP_MODEL_PAYLOAD_FILTER__": mcp_model_payload_filter,
            "__DRIVER_BINARY__": driver_binary,
        },
    )
    remote_path = r"C:\rb_pipeline\configure_cua_env.ps1"
    sftp_upload_text(ssh, script, remote_path)
    try:
        out = ssh_exec(
            ssh, f'powershell -ExecutionPolicy Bypass -File "{remote_path}"', timeout=30
        )
        log(f"CUA Driver env configured: {out}")
    except Exception as exc:
        raise RuntimeError(f"CUA Driver env configuration failed: {exc}") from exc
    finally:
        try:
            ssh_exec(
                ssh,
                f"powershell \"Remove-Item '{remote_path}' -Force -EA SilentlyContinue\"",
                timeout=10,
                quiet=True,
            )
        except Exception:
            pass


def reconnect_rdp(ssh: VmClient) -> None:
    log("Reconnecting disconnected RDP sessions...")
    try:
        ssh_exec(
            ssh,
            'powershell -Command "query session | ForEach-Object { '
            "if ($_ -match '(\\d+)\\s+(Disc|断开)') "
            '{ tscon $Matches[1] /dest:console } }"',
            timeout=10,
            quiet=True,
        )
        log("RDP reconnect done")
    except Exception as exc:
        log(f"RDP reconnect skipped: {exc}")


# Shared with linux and macOS via core.cua_driver: the install SCRIPTS differ by extension but
# the repos, the binary names and the env contract are one policy, and letting each platform own
# its own copy is how the three ended up on three different drivers.
_CUA_DRIVER_BIN_NAMES = dict(core_cua.BIN_NAMES)


def upgrade_cua_driver(ssh: VmClient, cfg: dict[str, str]) -> str | None:
    """Upgrade CUA Driver on the VM to a pinned version."""
    ref = cfg.get("RB_CUA_DRIVER_REF", "").strip()
    if not ref:
        return None
    source, version, _normalize = core_cua.parse_ref(ref)
    if not source or not version:
        raise RuntimeError(f"invalid RB_CUA_DRIVER_REF: {ref!r}")
    # Resolved per call, not once at import: the URL now carries the release tag, so it is a
    # function of the ref rather than a constant.
    try:
        install_url = core_cua.install_url(source, "ps1", version)
    except ValueError as exc:
        raise RuntimeError(
            f"cannot resolve CUA driver installer for {ref!r}: {exc}"
        ) from exc
    bin_name = _CUA_DRIVER_BIN_NAMES[source]
    log(
        f"--- Upgrading CUA Driver to {version} (source: {source}, bin: {bin_name}) ---"
    )
    script = (
        CUA_DRIVER_UPGRADE_PS1.replace("{version}", version)
        .replace("{install_url}", install_url)
        .replace("{bin_name}", bin_name)
    )
    remote_path = r"C:\rb_pipeline\upgrade_cua_driver.ps1"
    sftp_upload_text(ssh, script, remote_path)
    try:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                out = ssh_exec(
                    ssh,
                    f'powershell -ExecutionPolicy Bypass -File "{remote_path}"',
                    timeout=600,
                )
                log(f"Upgrade output (attempt {attempt}/3):\n{out}")
                cua_ver = ssh_exec(ssh, f"{bin_name} --version", timeout=15).strip()
                if version not in cua_ver:
                    raise RuntimeError(
                        f"CUA Driver version mismatch — expected {version}, got: {cua_ver}"
                    )
                log(f"CUA Driver upgraded successfully: {cua_ver}")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                log(f"CUA Driver install attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    time.sleep(attempt * 10)
        if last_error is not None:
            raise RuntimeError(
                f"CUA Driver install/verification failed for {ref!r}: {last_error}"
            ) from last_error
    finally:
        try:
            ssh_exec(
                ssh,
                f"powershell \"Remove-Item '{remote_path}' -Force -EA SilentlyContinue\"",
                timeout=10,
                quiet=True,
            )
        except Exception:
            pass
    return bin_name


# ---------------------------------------------------------------------------
# VM preparation — download from artifact store and upload to VM
# ---------------------------------------------------------------------------


# The app key a unified instance actually resolved to. Its files were produced under
# THAT id, so _rewrite_task_paths has to normalise it too (see there).
_UNIFIED_APP_KEY = ""


def _download_unified_instance(task_id: str, local_dir: str) -> dict:
    """Materialize and validate one complete frozen release instance."""
    platform = os.environ.get("RB_UNIFIED_PLATFORM", "windows")
    common_dir = Path(__file__).resolve().parents[1] / "common"
    if str(common_dir) not in sys.path:
        sys.path.insert(0, str(common_dir))
    from rb_unify.eval_bridge import (
        copy_unified_from_local,
        download_unified,
        manifest_from_instance,
    )
    from rb_unify.rb_instance import RBInstance

    shutil.rmtree(local_dir, ignore_errors=True)
    os.makedirs(local_dir, exist_ok=True)
    local_root = os.environ.get("RB_UNIFIED_LOCAL_DIR", "").strip()
    if local_root:
        rep = copy_unified_from_local(
            local_root,
            platform,
            task_id,
            local_dir,
            components=("reference", "tests"),
            override=os.environ.get("RB_UNIFIED_APP", ""),
            log=log,
        )
    else:
        unified_prefix = os.environ.get("RB_UNIFIED_PREFIX", "")
        if not unified_prefix:
            raise RuntimeError(
                "RB_UNIFIED_LOCAL_DIR is required unless an artifact-store integration is configured"
            )
        rep = download_unified(
            unified_prefix,
            platform,
            task_id,
            local_dir,
            components=("reference", "tests"),
            override=os.environ.get("RB_UNIFIED_APP", ""),
            log=log,
            store=_artifact_store(),
        )
    if not rep.get("prefix"):
        raise FrozenInputError(f"frozen instance not found; tried {rep.get('tried')}")
    try:
        instance = RBInstance.load(local_dir)
    except Exception as exc:
        raise FrozenInputError(f"invalid frozen instance: {exc}") from exc
    problems = [str(p) for p in instance.validate() if p.component != "vlm"]
    if problems or instance.platform != "windows":
        detail = problems or [f"platform={instance.platform!r}, expected 'windows'"]
        raise FrozenInputError("invalid frozen instance: " + "; ".join(detail))

    global _UNIFIED_APP_KEY
    _UNIFIED_APP_KEY = rep["prefix"].rstrip("/").rsplit("/", 1)[-1]
    mpath = instance.tests_dir / "test_manifest.json"
    manifest_changed = False
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8-sig"))
        if instance.vlm_path.is_file():
            vlm = manifest_from_instance(local_dir)
            manifest["vlm_assertions"] = vlm["vlm_assertions"]
            manifest["vlm_total"] = len(vlm["vlm_assertions"])
            manifest_changed = True
    except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
        raise FrozenInputError(f"invalid frozen test manifest: {exc}") from exc
    if manifest_changed:
        mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {**rep, "descriptor": instance.descriptor}


def _download_run_output(
    instance_id: str,
    output_dir_name: str,
    local_dir: str,
    exclude_prefixes: list[str] | None = None,
) -> int:
    """Restore a prior recreation output for an eval-only invocation."""
    prefix = _artifact_prefix(restore=True)
    if not prefix:
        raise RuntimeError(
            "candidate recreation artifact prefix is missing; set "
            "RB_ARTIFACT_RESTORE_PREFIX/RB_ARTIFACT_PREFIX"
        )
    try:
        store = _artifact_store()
        artifact_prefix = f"{prefix}/{instance_id}/{output_dir_name}/"
        result = download_tree(
            store,
            artifact_prefix,
            local_dir,
            exclude_prefixes=exclude_prefixes or (),
        )
        if result.transferred:
            msg = (
                f"  Downloaded {result.transferred} files from artifact prefix "
                f"{artifact_prefix}"
            )
            if result.skipped:
                msg += f" (skipped {result.skipped} unneeded)"
            log(msg)
        else:
            log(f"  WARN: No files found at artifact prefix {artifact_prefix}")
        return result.transferred
    except Exception as exc:
        raise RuntimeError(
            f"candidate recreation artifact restore failed: {exc}"
        ) from exc


def _ensure_task_dir(ssh: VmClient, vm_tid: str) -> str:
    task_dir = f"{VM_BASE}\\{vm_tid}"
    ssh_exec(
        ssh,
        f"powershell \"New-Item -ItemType Directory -Path '{task_dir}' -Force | Out-Null\"",
        timeout=15,
        quiet=True,
    )
    return task_dir


_EXTERNAL_URL = re.compile(r"https?://[^\s'\"<>&]+$")


def _configured_url(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    if value and not _EXTERNAL_URL.fullmatch(value):
        raise ValueError(f"{name} must be an HTTP(S) URL")
    return value


def _b64_text(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _gradle_repositories(names: tuple[str, ...], indent: str) -> str:
    urls = [_configured_url(name) for name in names]
    return "\n".join(f'{indent}maven {{ url "{url}" }}' for url in urls if url)


def _gradle_init_script() -> str:
    return runtime_assets.render_text(
        "windows/runtime/gradle_init.gradle.template",
        {
            "__PLUGIN_REPOSITORIES__": _gradle_repositories(
                (
                    "RB_GRADLE_PLUGIN_MIRROR_URL",
                    "RB_GRADLE_PUBLIC_MIRROR_URL",
                    "RB_GRADLE_CENTRAL_MIRROR_URL",
                ),
                "            ",
            ),
            "__BUILDSCRIPT_REPOSITORIES__": _gradle_repositories(
                (
                    "RB_GRADLE_PUBLIC_MIRROR_URL",
                    "RB_GRADLE_PLUGIN_MIRROR_URL",
                    "RB_GRADLE_CENTRAL_MIRROR_URL",
                ),
                "            ",
            ),
            "__PROJECT_REPOSITORIES__": _gradle_repositories(
                (
                    "RB_GRADLE_PUBLIC_MIRROR_URL",
                    "RB_GRADLE_PLUGIN_MIRROR_URL",
                    "RB_GRADLE_CENTRAL_MIRROR_URL",
                ),
                "        ",
            ),
        },
    )


def _ensure_gradle_mirror(ssh: VmClient) -> None:
    """Write init.gradle with optional deployment-provided repository mirrors.

    Writes to both ~/.gradle/ and C:\\gradle_cache/ because build.ps1 may set
    GRADLE_USER_HOME to either location — Gradle reads init.gradle from
    $GRADLE_USER_HOME, not necessarily the user profile.
    """
    b64 = _b64_text(_gradle_init_script())
    ssh_exec(
        ssh,
        _powershell_runtime_command(
            "write_gradle_init.ps1", {"__CONTENT_BASE64__": b64}
        ),
        timeout=10,
        quiet=True,
    )


def _ensure_gradle_distribution_mirror(ssh: VmClient, repo_dir: str) -> None:
    """Optionally route Gradle distributions and always set a bounded timeout."""
    mirror = _configured_url("RB_GRADLE_DISTRIBUTION_MIRROR_URL")
    ssh_exec(
        ssh,
        _powershell_runtime_command(
            "rewrite_gradle_distribution.ps1",
            {"__REPO_DIR__": repo_dir, "__MIRROR_URL_BASE64__": _b64_text(mirror)},
        ),
        timeout=30,
        quiet=True,
    )


def _maven_settings() -> str:
    mirror = _configured_url("RB_MAVEN_CENTRAL_MIRROR_URL")
    block = ""
    if mirror:
        block = (
            "  <mirrors>\n"
            "    <mirror>\n"
            "      <id>rb-central-mirror</id>\n"
            "      <mirrorOf>central</mirrorOf>\n"
            f"      <url>{mirror}</url>\n"
            "    </mirror>\n"
            "  </mirrors>"
        )
    return runtime_assets.render_text(
        "windows/runtime/maven_settings.xml.template", {"__MIRROR_BLOCK__": block}
    )


def _ensure_maven_mirror(ssh: VmClient) -> None:
    """Write Maven settings.xml with an optional deployment-provided mirror.

    Maven reads settings from $USERPROFILE\\.m2\\settings.xml and C:\\m2_repo
    (if build.ps1 sets -Dmaven.repo.local=C:\\m2_repo). Also writes to
    $MAVEN_HOME/conf/settings.xml if Maven is installed via choco.
    """
    b64 = _b64_text(_maven_settings())
    ssh_exec(
        ssh,
        _powershell_runtime_command(
            "write_maven_settings.ps1", {"__CONTENT_BASE64__": b64}
        ),
        timeout=15,
        quiet=True,
    )


def _ensure_aqt_mirror(ssh: VmClient) -> None:
    """Optionally override aqt's built-in Qt download mirror.

    Directly patches the package-level settings.ini so all `aqt install-qt`
    commands automatically use the mirror without needing CLI arg changes.
    """
    ssh_exec(
        ssh,
        _powershell_runtime_command(
            "configure_aqt_mirror.ps1",
            {"__MIRROR_URL_BASE64__": _b64_text(_configured_url("RB_QT_MIRROR_URL"))},
        ),
        timeout=15,
        quiet=True,
    )


def _kill_reference_build_leftovers(ssh: VmClient, repo_dir: str) -> None:
    """Kill what a cut-short reference build left holding its output directory.

    A timed-out ``ssh.run`` abandons the compiler mid-flight, and the next attempt dies in
    seconds on ``Remove-Item -Recurse -Force dist`` because an orphan still has an object file
    open -- so two of the three attempts were never real retries.

    Matched by command line under ``repo_dir`` rather than by image name: an indiscriminate
    ``taskkill /IM python.exe`` would reach the recreation agent if this ever ran after
    launch_stage.  cl.exe/link.exe are matched by name because MSVC children do not inherit a
    command line naming the repo, and nothing else on the image spawns them.
    """

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process | Where-Object { "
        f"$_.CommandLine -like '*{repo_dir}*' -and $_.ProcessId -ne $PID "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
        "Get-Process -Name cl,link,nuitka-* | Stop-Process -Force"
    )
    try:
        ssh_exec(
            ssh, f'powershell -NoProfile -Command "{script}"', timeout=30, quiet=True
        )
    except Exception as exc:  # best effort: the retry is still worth attempting
        log(f"  WARN: could not clear leftover build processes: {exc}")


def _materialize_reference(ssh: VmClient, vm_tid: str, task_id: str) -> bool:
    """Run the frozen reference recipe inside its extracted source tree.

    Returns True on success, raises RuntimeError on failure.
    """
    task_dir = f"{VM_BASE}\\{vm_tid}"
    build_ps1 = f"{task_dir}\\reference\\build.ps1"
    repo_dir = f"{task_dir}\\repo"
    _ensure_gradle_mirror(ssh)
    _ensure_gradle_distribution_mirror(ssh, repo_dir)
    _ensure_maven_mirror(ssh)
    _ensure_aqt_mirror(ssh)
    maven_mirror = _configured_url("RB_MAVEN_CENTRAL_MIRROR_URL")
    if maven_mirror:
        ssh_exec(
            ssh,
            _powershell_runtime_command(
                "rewrite_maven_urls.ps1",
                {
                    "__BUILD_PATH_BASE64__": _b64_text(build_ps1),
                    "__MIRROR_URL_BASE64__": _b64_text(maven_mirror),
                },
            ),
            timeout=10,
            quiet=True,
        )
    log("Rebuilding install from build.ps1 (up to 3 attempts)...")
    rebuild_logs: list[str] = []
    build_result_path = f"{task_dir}\\reference\\build_result.json"
    for attempt in range(1, 4):
        rebuild_log = f"{task_dir}\\rebuild.attempt-{attempt}.log"
        rebuild_logs.append(rebuild_log)
        log(f"Reference build attempt {attempt}/3...")
        try:
            build = ssh.run(
                f'cmd /c "cd /d "{repo_dir}" && powershell -NoProfile -ExecutionPolicy Bypass -File "{build_ps1}" > "{rebuild_log}" 2>&1"',
                timeout=REFERENCE_BUILD_TIMEOUT,
                check=False,
            )
        except Exception as exc:  # a dropped SSH channel is retryable too
            log(f"Reference build attempt {attempt}/3 transport error: {exc}")
            build = None

        if build is not None and build.rc == 0:
            try:
                out = ssh_exec(
                    ssh,
                    f"powershell \"Get-Content '{build_result_path}' -Raw -Encoding UTF8\"",
                    timeout=10,
                    quiet=True,
                )
                result = json.loads(out.strip())
                exe_path = result.get("executable", "")
                if exe_path:
                    check = ssh_exec(
                        ssh,
                        f"powershell \"Test-Path '{exe_path}'\"",
                        timeout=10,
                        quiet=True,
                    )
                    if "True" in check.strip():
                        log(f"Rebuild done on attempt {attempt}/3")
                        return True
                    log(f"  DEBUG: Test-Path '{exe_path}' returned: {check.strip()!r}")
            except Exception as exc:
                log(f"  DEBUG: verify exception on attempt {attempt}/3: {exc}")
        elif build is not None:
            log(f"Reference build attempt {attempt}/3 failed with exit code {build.rc}")

        if attempt < 3:
            # Before, not after, the sleep: the next attempt's first act is to delete the
            # output directory, and it cannot while an abandoned compiler holds a file in it.
            _kill_reference_build_leftovers(ssh, repo_dir)
            time.sleep(attempt * 10)

    # List what survived all attempts before collecting their logs.
    install_dir = f"{task_dir}\\install"
    listing = ssh_exec(
        ssh,
        f"powershell \"Get-ChildItem -Recurse '{install_dir}' -Filter '*.exe' -EA SilentlyContinue | Select-Object -First 5 -ExpandProperty FullName\"",
        timeout=10,
        quiet=True,
    )
    if listing.strip():
        log(f"  DEBUG: exe(s) found in install dir: {listing.strip()}")

    # Download every attempt log from the VM before raising so intermittent dependency
    # failures remain diagnosable instead of being overwritten by the final attempt.
    try:
        # RESULTS_DIR/<task_id> is the one directory main() copies into OUTPUT_DIR. ``vm_tid``
        # is already _task_hash(task_id), so hashing it again wrote these to a sibling nothing
        # reads -- and a prepare-stage failure has no other collection point.
        local_log_dir = os.path.join(RESULTS_DIR, task_id)
        os.makedirs(local_log_dir, exist_ok=True)
        for attempt, rebuild_log in enumerate(rebuild_logs, start=1):
            log_content = ssh_exec(
                ssh,
                f"powershell \"Get-Content '{rebuild_log}' -Raw -EA SilentlyContinue\"",
                timeout=15,
                quiet=True,
            )
            if log_content and log_content.strip():
                local_rebuild_log = os.path.join(
                    local_log_dir, f"rebuild.attempt-{attempt}.log"
                )
                Path(local_rebuild_log).write_text(log_content, encoding="utf-8")
                log(
                    f"  Saved rebuild.attempt-{attempt}.log "
                    f"({len(log_content)} bytes) for diagnosis"
                )
                if attempt == len(rebuild_logs):
                    for line in log_content.strip().splitlines()[-5:]:
                        log(f"  [rebuild] {line}")
    except Exception:
        pass
    log("ERROR: Rebuild failed — executable not found after 3 build.ps1 attempts")
    raise RuntimeError("rebuild_failed")


_TEXT_EXTENSIONS = {
    ".ps1",
    ".json",
    ".jsonl",
    ".txt",
    ".md",
    ".log",
    ".py",
    ".cs",
    ".js",
    ".ts",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".csv",
    ".html",
    ".css",
}


def _rewrite_task_paths(directory: str, real_task_id: str, vm_tid: str) -> None:
    """Rewrite task-id references in all text files under *directory*.

    Handles two scenarios:
    1. Forward: real_task_id → vm_tid (when mask is enabled, artifact store files
       contain the real task_id and must be rewritten to the hash).
    2. Reverse: hashed_task_id → vm_tid (when mask is disabled but artifact store
       files were produced by a previous masked run, so they contain
       the old hash instead of the real task_id).

    Both replacements are applied in a single pass so that files
    produced under either masking mode are handled correctly.
    """
    raw_hash = hashlib.sha256(real_task_id.encode()).hexdigest()[:16]
    replacements = []
    if real_task_id != vm_tid:
        replacements.append((real_task_id, vm_tid))
    if raw_hash != vm_tid and raw_hash != real_task_id:
        replacements.append((raw_hash, vm_tid))
    # 3. Unified: a released instance was produced under the INSTANCE's own task id, so its
    #    build.ps1/launch.ps1 carry absolute paths under that id (or its masked hash) —
    #    C:\rb_pipeline\<sha256(app_key)[:16]>\repo\... — which is neither this run's id
    #    nor its hash, so scenarios 1-2 leave it untouched and build.ps1 then looks for the
    #    solution in a directory that does not exist ("Is the repository present?").
    #    Include the deterministic app-key hash used by older materialized inputs.
    if _UNIFIED_APP_KEY and _UNIFIED_APP_KEY != vm_tid:
        replacements.append((_UNIFIED_APP_KEY, vm_tid))
        uni_hash = hashlib.sha256(_UNIFIED_APP_KEY.encode()).hexdigest()[:16]
        if uni_hash != vm_tid:
            replacements.append((uni_hash, vm_tid))
    if not replacements:
        return

    count = 0
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if os.path.splitext(fname)[1].lower() not in _TEXT_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            try:
                raw = Path(fpath).read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue
            updated = raw
            for old, new in replacements:
                updated = updated.replace(old, new)
            if updated != raw:
                Path(fpath).write_text(updated, encoding="utf-8")
                count += 1
    if count:
        subs = ", ".join(f"{old} -> {new}" for old, new in replacements)
        log(f"  Rewrote paths in {count} files: {subs}")


def _prepare_reference_source(ssh: VmClient, task_dir: str) -> str:
    """Run the shared clone-first source policy inside the Windows guest."""

    helper = f"{REMOTE_CODE_DIR}\\scripts\\core\\reference_source.py"
    descriptor = f"{task_dir}\\instance.json"
    source_dir = f"{task_dir}\\repo"
    archive = f"{task_dir}\\reference\\reference.tar.gz"
    command = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        f"& python '{helper}' --descriptor '{descriptor}' "
        f"--source-dir '{source_dir}' --archive '{archive}'\""
    )
    log("Preparing reference source (clone-first)...")
    result = ssh.run(command, timeout=1200, check=False)
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr, flush=True)
    if result.rc:
        raise RuntimeError(
            f"reference source preparation failed with exit code {result.rc}"
        )
    match = re.search(r"^SOURCE_MODE=(clone|frozen_fallback)\r?$", result.stdout, re.M)
    if not match:
        raise RuntimeError("reference source preparation returned no source mode")
    return match.group(1)


def prepare_vm_for_stage(
    ssh: VmClient,
    task_id: str,
    stage: str,
    cfg: dict[str, str],
    *,
    vm_tid: str = "",
) -> None:
    """Materialize frozen release inputs, plus a prior recreation for eval-only."""
    if stage not in RELEASE_STAGES:
        raise ValueError(f"unsupported release stage: {stage}")

    vm_tid = vm_tid or _task_hash(task_id)
    task_dir = _ensure_task_dir(ssh, vm_tid)
    local_results = os.path.join(RESULTS_DIR, task_id)
    suffix = cfg.get("RB_DIR_SUFFIX", "")
    log(f"--- Preparing VM for {stage} ---")

    local_instance = os.path.join(local_results, "frozen_instance")
    _download_unified_instance(task_id, local_instance)
    local_reference = os.path.join(local_instance, "reference")
    local_tests = os.path.join(local_instance, "tests")
    _rewrite_task_paths(local_reference, task_id, vm_tid)
    _rewrite_task_paths(local_tests, task_id, vm_tid)
    ssh_exec(
        ssh,
        f"powershell \"Remove-Item -Recurse -Force '{task_dir}\\reference','{task_dir}\\tests' -EA SilentlyContinue\"",
        timeout=60,
    )
    n = sftp_upload_dir(
        ssh, local_reference, f"{task_dir}\\reference", exclude_dirs={".claude"}
    )
    log(f"Uploaded frozen reference ({n} files)")
    n = sftp_upload_dir(
        ssh, local_tests, f"{task_dir}\\tests", exclude_dirs={".claude"}
    )
    log(f"Uploaded frozen tests ({n} files)")
    ssh.upload_file(
        os.path.join(local_instance, "instance.json"), f"{task_dir}\\instance.json"
    )
    vlm_path = os.path.join(local_instance, "vlm_assertions.json")
    if os.path.isfile(vlm_path):
        ssh.upload_file(vlm_path, f"{task_dir}\\vlm_assertions.json")

    _prepare_reference_source(ssh, task_dir)
    # A standalone eval must start from the same machine state as the eval half
    # of a full recreation_eval run.  In particular, frozen reference recipes
    # install app-specific prerequisites and warm package-manager caches before
    # the agent builds its candidate.  Materialize the reference for both eval
    # targets; the recreation-target path below then removes only the reference
    # install tree before restoring/rebuilding the candidate.
    if stage in ("recreation", "eval") and not _materialize_reference(
        ssh, vm_tid, task_id
    ):
        raise RuntimeError("reference materialization failed")
    shutil.rmtree(local_instance, ignore_errors=True)

    if stage == "eval" and cfg["RB_EVAL_TARGET"] == "recreation":
        install_dir = f"{task_dir}\\install"
        ssh_exec(
            ssh,
            'powershell "Remove-Item -Recurse -Force '
            f"'{install_dir}' -EA SilentlyContinue\"",
            timeout=60,
        )
        rec_dir = f"recreation_{suffix}" if suffix else "recreation"
        local_rec = os.path.join(local_results, rec_dir)
        restored = _download_run_output(task_id, rec_dir, local_rec)
        if restored == 0:
            raise FrozenInputError("recreation artifacts not found in artifact store")
        _rewrite_task_paths(local_rec, task_id, vm_tid)
        n = sftp_upload_dir(ssh, local_rec, f"{task_dir}\\{rec_dir}")
        log(f"Uploaded {rec_dir}/ ({n} files)")
        shutil.rmtree(local_rec, ignore_errors=True)


# ---------------------------------------------------------------------------
# Stage execution
# ---------------------------------------------------------------------------


def make_runner_ps1(vm_tid: str, stage: str, cfg: dict[str, str]) -> str:
    model_args = ""
    if "recreation" in stages_for(stage):
        upstream_model = cfg.get("MODEL") or cfg["ANTHROPIC_MODEL"]
        claude_model = cfg.get("CLAUDE_MODEL") or cfg["ANTHROPIC_MODEL"]
        model_args = (
            f" --auth-token '{cfg['ANTHROPIC_API_KEY']}'"
            f" --base-url '{cfg['ANTHROPIC_BASE_URL']}'"
            f" --model '{upstream_model}'"
            f" --claude-model '{claude_model}'"
        )
    vlm_key_arg = f" --vlm-key '{cfg['VLM_MODEL_API_KEY']}'"
    vlm_model_arg = f" --vlm-model '{cfg['VLM_MODEL']}'"
    vlm_base_url_arg = f" --vlm-base-url '{cfg['VLM_MODEL_BASE_URL']}'"

    timeout_arg = ""
    stage_timeout_flags = {"recreation": "--recreation-timeout"}
    if stage in stage_timeout_flags:
        timeout_arg = f" {stage_timeout_flags[stage]} {STAGE_TIMEOUTS[stage]}"

    dir_suffix_arg = (
        f" --dir-suffix '{cfg['RB_DIR_SUFFIX']}'" if cfg.get("RB_DIR_SUFFIX") else ""
    )
    eval_target_arg = f" --eval-target '{cfg.get('RB_EVAL_TARGET', 'recreation')}'"
    effort_arg = f" --effort '{cfg['RB_THINKING_EFFORT']}'"
    compact_window_arg = (
        f" --auto-compact-window '{cfg['CLAUDE_CODE_AUTO_COMPACT_WINDOW']}'"
        if cfg.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
        else ""
    )
    compact_pct_arg = (
        f" --autocompact-pct '{cfg['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE']}'"
        if cfg.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        else ""
    )
    max_output_tokens_arg = (
        f" --claude-code-max-output-tokens '{cfg['CLAUDE_CODE_MAX_OUTPUT_TOKENS']}'"
        if cfg.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
        else ""
    )
    cua_space_arg = f" --cua-coordinate-space '{cfg['RB_CUA_COORDINATE_SPACE']}'"
    cua_scale_arg = f" --cua-coordinate-scale '{cfg['RB_CUA_COORDINATE_SCALE']}'"
    mcp_payload_filter_arg = (
        f" --mcp-model-payload-filter '{cfg['RB_MCP_MODEL_PAYLOAD_FILTER']}'"
    )
    agent_cli_arg = f" --agent-cli '{cfg['RB_AGENT_CLI']}'"
    api_timeout_arg = f" --api-timeout-ms {cfg['RB_API_TIMEOUT_MS']}"
    mcp_timeout_arg = f" --mcp-tool-timeout-ms {cfg['RB_MCP_TOOL_TIMEOUT']}"
    time_hook_arg = (
        " --time-budget-hook"
        if cfg.get("RB_RECREATION_TIME_HOOK", "").lower()
        in ("1", "true", "yes", "y", "on")
        else ""
    )

    extra_body_raw = cfg.get("RB_EXTRA_BODY", "")
    if extra_body_raw:
        eb_b64 = base64.b64encode(extra_body_raw.encode("utf-8")).decode("ascii")
        extra_body_arg = f" --extra-body-b64 '{eb_b64}'"
    else:
        extra_body_arg = ""

    pipeline_log = f"{VM_BASE}\\{vm_tid}\\pipeline.log"

    extra_env_lines = (
        f"\n$env:RB_CUA_PREFLIGHT_MODE = '{cfg['RB_CUA_PREFLIGHT_MODE']}'"
        f"\n$env:RB_EVAL_TARGET = '{cfg.get('RB_EVAL_TARGET', 'recreation')}'"
        f"\n$env:RB_WINDOWS_ENABLE_NETWORK_ISOLATION = "
        f"'{cfg.get('RB_WINDOWS_ENABLE_NETWORK_ISOLATION', '1')}'"
    )
    deny_probe = cfg.get("RB_WINDOWS_NETWORK_DENY_PROBE", "https://1.1.1.1:443")
    extra_env_lines += (
        "\n$env:RB_WINDOWS_NETWORK_DENY_PROBE = "
        f"'{deny_probe.replace(chr(39), chr(39) * 2)}'"
    )
    capture_enabled = cfg.get(
        "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    extra_env_lines += "\n$env:RB_CAPTURE_TOOL_USE_SCREENSHOTS = " + (
        "'true'" if capture_enabled else "'false'"
    )
    driver_source, _, _ = core_cua.parse_ref(cfg.get("RB_CUA_DRIVER_REF", ""))
    if driver_source:
        extra_env_lines += (
            f"\n$env:RB_CUA_DRIVER_BINARY = '{_CUA_DRIVER_BIN_NAMES[driver_source]}'"
        )
    vlm_extra_headers = cfg.get("VLM_EXTRA_HEADERS", "")
    if vlm_extra_headers:
        extra_env_lines += f"\n$env:VLM_EXTRA_HEADERS = '{vlm_extra_headers}'"
    eval_screenshot_mode = cfg.get("RB_EVAL_SCREENSHOT_MODE", "")
    if eval_screenshot_mode:
        extra_env_lines += f"\n$env:RB_EVAL_SCREENSHOT_MODE = '{eval_screenshot_mode}'"
    # Evaluation runs in the Windows VM. Keep the deployment platform profile's judge throttling
    # policy instead of silently falling back to the shared judge defaults.
    for name in (
        "RB_VLM_JUDGE_RETRIES",
        "RB_VLM_JUDGE_MIN_INTERVAL",
        "RB_VLM_JUDGE_BATCH_SIZE",
    ):
        value = cfg.get(name, "")
        if value:
            extra_env_lines += (
                f"\n$env:{name} = '{value.replace(chr(39), chr(39) * 2)}'"
            )

    task_dir = f"{VM_BASE}\\{vm_tid}"
    exit_file = f"{task_dir}\\pipeline_exit.txt"
    exit_marker_file = f"{task_dir}\\pipeline_exit_marker.txt"
    start_file = f"{task_dir}\\{STAGE_START_FILE}"
    evt_file = f"{task_dir}\\crash_events.txt"

    # The daemon must own the same interactive Windows session as the reference app.
    # Starting it over SSH during bootstrap puts it in the SSH/service session, where UIA
    # can see the shell but not the app later launched by the RB_Pipeline scheduled task.
    cua_daemon_start = ""
    cua_daemon_stop = ""
    if stage == "recreation":
        daemon_stdout = f"{task_dir}\\cua_daemon.stdout.log"
        daemon_stderr = f"{task_dir}\\cua_daemon.stderr.log"
        daemon_pipe = core_cua.WINDOWS_DAEMON_PIPE.replace("'", "''")
        cua_daemon_start = runtime_assets.render_text(
            "windows/runtime/cua_daemon_start.ps1",
            {
                "__DAEMON_PIPE__": daemon_pipe,
                "__DAEMON_STDOUT__": daemon_stdout,
                "__DAEMON_STDERR__": daemon_stderr,
                "__PIPELINE_LOG__": pipeline_log,
            },
        )
        cua_daemon_stop = runtime_assets.load_text(
            "windows/runtime/cua_daemon_stop.ps1"
        )

    return runtime_assets.render_text(
        "windows/runtime/runner.ps1",
        {
            "__TASK_DIR__": task_dir,
            "__STAGE__": stage,
            "__START_FILE__": start_file,
            "__PIPELINE_LOG__": pipeline_log,
            "__EXIT_FILE__": exit_file,
            "__EXIT_MARKER_FILE__": exit_marker_file,
            "__EXTRA_ENV__": extra_env_lines,
            "__REMOTE_CODE_DIR__": REMOTE_CODE_DIR,
            "__CUA_DAEMON_START__": cua_daemon_start,
            "__VM_TASK_ID__": vm_tid,
            "__MODEL_ARGS__": model_args,
            "__EVAL_TARGET_ARG__": eval_target_arg,
            "__VLM_KEY_ARG__": vlm_key_arg,
            "__VLM_MODEL_ARG__": vlm_model_arg,
            "__VLM_BASE_URL_ARG__": vlm_base_url_arg,
            "__TIMEOUT_ARG__": timeout_arg,
            "__DIR_SUFFIX_ARG__": dir_suffix_arg,
            "__EFFORT_ARG__": effort_arg,
            "__COMPACT_WINDOW_ARG__": compact_window_arg,
            "__COMPACT_PCT_ARG__": compact_pct_arg,
            "__CUA_SPACE_ARG__": cua_space_arg,
            "__CUA_SCALE_ARG__": cua_scale_arg,
            "__MCP_PAYLOAD_FILTER_ARG__": mcp_payload_filter_arg,
            "__EXTRA_BODY_ARG__": extra_body_arg,
            "__AGENT_CLI_ARG__": agent_cli_arg,
            "__API_TIMEOUT_ARG__": api_timeout_arg,
            "__MCP_TIMEOUT_ARG__": mcp_timeout_arg,
            "__MAX_OUTPUT_TOKENS_ARG__": max_output_tokens_arg,
            "__TIME_HOOK_ARG__": time_hook_arg,
            "__CUA_DAEMON_STOP__": cua_daemon_stop,
            "__EVENT_FILE__": evt_file,
        },
    )


def launch_stage(ssh: VmClient, vm_tid: str, stage: str, cfg: dict[str, str]) -> bool:
    upstream_model = cfg.get("MODEL") or cfg.get("ANTHROPIC_MODEL", "")
    model_detail = f", model={upstream_model}" if upstream_model else ""
    log(f"--- Launching {stage.upper()} (vm_tid={vm_tid}{model_detail}) ---")
    # Per stage, not once per run: the task below runs -LogonType Interactive, and eval starts
    # hours after main() did this the first time.
    reconnect_rdp(ssh)
    task_dir = f"{VM_BASE}/{vm_tid}"
    ssh_exec(
        ssh,
        f"powershell \"New-Item -ItemType Directory -Force -Path '{VM_BASE}\\{vm_tid}' | Out-Null\"",
        timeout=10,
    )
    runner = make_runner_ps1(vm_tid, stage, cfg)
    launcher = make_launcher_ps1(vm_tid)
    sftp_upload_text(ssh, runner, f"{task_dir}/runner.ps1")
    sftp_upload_text(ssh, launcher, f"{task_dir}/launcher.ps1")
    out = ssh_exec(
        ssh,
        f"powershell -ExecutionPolicy Bypass -File {VM_BASE}\\{vm_tid}\\launcher.ps1",
        # The launcher now waits for the previous instance to drain, so its own budget has to
        # cover that wait plus the two seconds it spends reading back LastTaskResult.
        timeout=TASK_DRAIN_TIMEOUT + 60,
    )
    log(f"Stage {stage} launched: {out}")
    # Both of these mean the stage did NOT start, and neither can be left for poll_stage to
    # rediscover: its first check passes on `marker or procs["python"]`, and when the previous
    # stage's runner is what blocked us that python IS alive -- so poll_stage would confirm a
    # start that never happened and then poll the previous stage's state files.
    text = out or ""
    verdict = next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith(("Started ", "DRAIN_TIMEOUT ", "LAUNCH_FAILED "))
        ),
        "",
    )
    if verdict.startswith("DRAIN_TIMEOUT "):
        log(
            f"ERROR: refusing to start {stage}; the previous stage's task instance was still "
            f"running after {TASK_DRAIN_TIMEOUT}s, and replacing it would race its epilogue"
        )
        return False
    if verdict.startswith("LAUNCH_FAILED "):
        log(f"ERROR: the launcher for {stage} failed: {verdict[:400]}")
        return False
    if not verdict.startswith("Started "):
        # Success has to be asserted, not inferred from the absence of the two failure markers: the
        # launcher aborts on any cmdlet error, and an aborted launcher prints none of the three
        # markers. Reading that as success sent us to poll a stage that was never started, which
        # then surfaced as pipeline_not_started with the real reason -- whatever the launcher threw
        # -- left in stderr nobody correlated.
        log(
            f"ERROR: the launcher for {stage} printed no launch verdict; it most likely aborted. "
            f"Output was: {text[:400]!r}"
        )
        return False
    return True


def check_vm_processes(ssh: VmClient, agent_cli: str = "claude") -> dict[str, bool]:
    """Quick check if pipeline processes are running on the VM.

    Only procs["python"] is used for lifecycle decisions (start confirmation,
    crash detection).  procs["agent"] is informational logging only — it shows
    whether the agent CLI (claude / codex) is alive.
    """
    agent_pattern = "codex*" if agent_cli.startswith("codex") else "claude*"
    raw = ssh_exec(
        ssh,
        f'powershell "Get-Process python*,{agent_pattern} -EA SilentlyContinue | Select ProcessName | Format-Table -HideTableHeaders"',
        timeout=15,
        quiet=True,
    )
    names = [line.strip().lower() for line in raw.splitlines() if line.strip()]
    agent_prefix = "codex" if agent_cli.startswith("codex") else "claude"
    return {
        "python": any("python" in n for n in names),
        "agent": any(n.startswith(agent_prefix) for n in names),
    }


def check_vm_resources(ssh: VmClient) -> dict[str, str]:
    """Check VM memory and CPU usage. Returns dict with mem_pct, mem_avail_mb, cpu_pct."""
    try:
        raw = ssh_exec(
            ssh,
            'powershell -Command "'
            "$m = Get-CimInstance Win32_OperatingSystem; "
            "$used = $m.TotalVisibleMemorySize - $m.FreePhysicalMemory; "
            "$pct = [math]::Round($used * 100 / $m.TotalVisibleMemorySize); "
            "$avail = [math]::Round($m.FreePhysicalMemory / 1024); "
            "$cpu = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average); "
            "Write-Output ('{0} {1} {2}' -f $pct, $avail, $cpu)\"",
            timeout=15,
            quiet=True,
        )
        parts = raw.strip().split()
        if len(parts) >= 3:
            return {"mem_pct": parts[0], "mem_avail_mb": parts[1], "cpu_pct": parts[2]}
    except Exception:
        pass
    return {}


def read_remote_exit_code(ssh: VmClient, vm_tid: str) -> int | None:
    """Read the native runner code without assigning policy to private values."""
    exit_path = f"{VM_BASE}\\{vm_tid}\\pipeline_exit.txt"
    try:
        raw = ssh_exec(
            ssh,
            f"powershell \"if (Test-Path '{exit_path}') "
            f"{{ Get-Content '{exit_path}' -Raw }} else {{ 'NONE' }}\"",
            timeout=15,
            quiet=True,
        )
    except Exception:
        return None
    match = re.search(r"(?:^|\n)exit_code=(-?\d+)", raw or "")
    return int(match.group(1)) if match else None


def read_stage_start_marker(ssh: VmClient, vm_tid: str, stage: str) -> str | None:
    """Whether runner.ps1 for *this* stage has begun.

    Returns the marker's detail line, "" when this stage has demonstrably not written one, and None
    when the answer is unknown because the read itself failed.  The caller must not treat None as
    "not started": an SSH hiccup inside the launch window used to collapse into the same empty
    string as a missing file, silently, and the reads that decide the verdict are tens of seconds
    apart -- so two bad reads in a row reported pipeline_not_started for a stage that was running.

    The stage name is compared, not just the file's existence: the file is per task, so a marker
    left by the previous stage would otherwise report the next one as started.  That is the same
    staleness that made a never-run eval report recreation's exit code.  The launcher now also
    deletes it before registering the task, which makes a matching stage name proof that OUR
    runner.ps1 reached its third statement -- so a matching name is a start even when the rest of
    the file has not been flushed yet.
    """

    path = f"{VM_BASE}\\{vm_tid}\\{STAGE_START_FILE}"
    try:
        raw = ssh_exec(
            ssh,
            f"powershell \"if (Test-Path '{path}') "
            f"{{ Get-Content '{path}' -Raw }} else {{ 'NONE' }}\"",
            timeout=15,
            quiet=True,
        )
    except Exception as exc:
        log(f"WARNING: could not read {STAGE_START_FILE} for stage {stage}: {exc}")
        return None
    if raw is None:
        return None
    # runner.ps1 writes the file with PowerShell 5.1's `-Encoding utf8`, which emits a BOM, and
    # ssh_exec only applies str.strip() -- which does NOT remove U+FEFF.  Whether the BOM reaches us
    # therefore depends on Get-Content's decoding rather than on anything here, and if it ever does
    # the first key parses as "﻿stage", every stage reads as not-started, and the whole suite fails
    # as if the VMs had stopped launching.  Strip it explicitly instead of relying on that.
    raw = raw.lstrip("﻿").strip()
    if not raw or raw == "NONE":
        return ""
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if fields.get("stage", "").strip() != stage:
        return ""
    started = fields.get("started", "").strip()
    pid = fields.get("pid", "").strip()
    if not started or not pid:
        # Out-File truncates on open and then writes, so a read can land after `stage=` and before
        # the rest.  The stage HAS started -- reporting otherwise would re-activate a healthy task --
        # but the two values are not usable, and they are the only thing tying the marker to a
        # process, so say so rather than passing "?" along silently.
        log(
            f"{STAGE_START_FILE} for stage {stage} is still being written "
            f"(started={started or '?'} pid={pid or '?'})"
        )
    return f"started={started or '?'} pid={pid or '?'}"


def read_heartbeat_age(ssh: VmClient, vm_tid: str) -> float | None:
    """Seconds since worker.py last wrote its heartbeat, or None if there is no usable reading.

    worker.py writes ``pipeline_heartbeat`` from a daemon thread every 30s, so the file going stale
    is independent evidence that the process is gone -- and it stops for the same reason a hard
    kill leaves no traceback.

    A negative age is reported as 0.0: the timestamp comes from the VM's clock and the comparison
    from the orchestrator's, so a VM running slightly ahead must read as "fresh" rather than
    wrapping into a large positive number.
    """

    path = f"{VM_BASE}\\{vm_tid}\\pipeline_heartbeat"
    try:
        raw = ssh_exec(
            ssh,
            f"powershell \"if (Test-Path '{path}') "
            f"{{ Get-Content '{path}' -Raw }} else {{ 'NONE' }}\"",
            timeout=10,
            quiet=True,
        )
    except Exception:
        return None
    if not raw or raw.strip() == "NONE":
        return None
    try:
        return max(0.0, time.time() - float(raw.strip().splitlines()[0]))
    except (ValueError, IndexError):
        return None


def pipeline_death_evidence(
    ssh: VmClient, vm_tid: str, agent_cli: str
) -> tuple[bool, str]:
    """Decide whether the stage runner is really gone.  Returns (dead, detail for the log).

    A single ``Get-Process`` reading is not enough to discard a stage.  ``check_vm_processes``
    reports python=False whenever its output comes back empty, and an empty reading is
    indistinguishable from an empty process list -- so one unlucky call could throw away hours of
    work.  Three jobs across glm53, kimik3 and grok46 died this way with 5.4-5.8GB free and the CPU
    nearly idle, which is not what resource exhaustion looks like.

    So the heartbeat has to agree.  A fresh heartbeat with no visible python process means the
    process listing was wrong, not that the run ended, and the caller keeps polling.  When the
    heartbeat cannot be read at all there is no second opinion, so the process listing is repeated
    once instead of trusted outright.
    """

    age = read_heartbeat_age(ssh, vm_tid)
    if age is not None and age < HEARTBEAT_STALE_SECONDS:
        return (
            False,
            f"heartbeat is {int(age)}s old, so the empty process listing is not trusted",
        )

    if age is None:
        time.sleep(HEARTBEAT_RECHECK_SECONDS)
        try:
            procs = check_vm_processes(ssh, agent_cli)
        except Exception as exc:
            return False, f"no heartbeat and the confirming process check failed: {exc}"
        if procs["python"]:
            return False, "no heartbeat, but python reappeared on the confirming check"
        return True, (
            f"no heartbeat file and no python process on two checks "
            f"{HEARTBEAT_RECHECK_SECONDS}s apart"
        )

    return (
        True,
        f"heartbeat is {int(age)}s old (>= {HEARTBEAT_STALE_SECONDS}s) and no python",
    )


def read_exit_marker(ssh: VmClient, vm_tid: str) -> str:
    """worker.py's atexit/signal marker: the one file that says HOW python left.

    ``reason=atexit`` is an ordinary interpreter exit; ``reason=signal_15`` is an external
    termination.  Nothing consumed this before, so a run killed after writing its stage status was
    indistinguishable from one that finished.
    """

    path = f"{VM_BASE}\\{vm_tid}\\pipeline_exit_marker.txt"
    try:
        raw = ssh_exec(
            ssh,
            f"powershell \"if (Test-Path '{path}') "
            f"{{ Get-Content '{path}' -Raw }} else {{ 'NONE' }}\"",
            timeout=15,
            quiet=True,
        )
    except Exception:
        return ""
    if not raw or raw.strip() == "NONE":
        return ""
    for line in raw.strip().splitlines():
        if line.startswith("reason="):
            return line.split("=", 1)[1].strip()
    return ""


def log_stage_exit_evidence(ssh: VmClient, vm_tid: str, stage: str, info: dict) -> None:
    """Record how python actually left, next to what pipeline_state.json claims.

    The verdict still comes from the state file -- this only makes a disagreement visible.  The
    state file is written by worker.py before it exits, so a process killed after that point was
    reported as a clean finish with no trace of the kill, even though the exit code and the atexit
    marker were both sitting on disk unread.
    """

    native = read_remote_exit_code(ssh, vm_tid)
    marker = read_exit_marker(ssh, vm_tid)
    status = str(info.get("status") or "")
    log(
        f"Stage {stage} exit evidence: native_exit={native} exit_marker={marker or '(none)'}"
    )
    if status and status != exit_contract.STAGE_ERROR and native not in (None, 0):
        log(
            f"WARNING: {stage} recorded status={status} but the runner exited {native}"
            f" (exit_marker={marker or '(none)'})"
        )
    if marker.startswith("signal_"):
        log(f"WARNING: {stage} python was terminated by {marker}, not an ordinary exit")


def runtime_failure(ssh: VmClient, vm_tid: str, reason_code: str) -> dict:
    """Return canonical evidence for a stage runner that died outside worker.py."""
    native_exit = read_remote_exit_code(ssh, vm_tid)
    terminated = (
        exit_contract.from_native_exit(native_exit) == exit_contract.OUTCOME_TERMINATED
    )
    if native_exit == 87:
        reason_code = "runner_exception"
    return exit_contract.stage_outcome(
        status=(
            exit_contract.STAGE_TIMEOUT if terminated else exit_contract.STAGE_ERROR
        ),
        outcome_class=(
            exit_contract.OUTCOME_TERMINATED
            if terminated
            else exit_contract.OUTCOME_INFRA_ERROR
        ),
        reason_code=reason_code,
        native_exit_code=native_exit,
    )


def reactivate_stage(ssh: VmClient, vm_tid: str) -> str:
    """Start RB_Pipeline again, but only when no instance is pending.  Returns what it found.

    "pending" (State was Running), "restarted" (it was not, so we activated it) or "unknown" (the
    read or the activation failed).

    Asking unconditionally was worse than useless.  IgnoreNew declines a second instance without
    touching the first, so on a task whose instance the scheduler has accepted but not yet turned into
    a process -- the state that loses eval launches -- every activation is refused and the only trace
    is 0x800710E0 in LastTaskResult. Observed re-activations went that way and each logged
    "Re-activated" as though it had done something, which made slow starts look like launches that
    never took. Distinguishing the two is the whole diagnostic value of this function.

    State, not LastTaskResult: the latter is unreliable right after an activation, which is what made
    the first attempt at a launch verdict hard-fail a healthy stage.
    """
    try:
        state = ssh_exec(
            ssh,
            f"powershell \"(Get-ScheduledTask -TaskName '{TASK_NAME}' "
            f'-EA SilentlyContinue).State"',
            timeout=30,
            quiet=True,
        )
    except Exception as exc:
        log(f"WARNING: could not read {TASK_NAME} state before re-activating: {exc}")
        state = None
    if state and state.strip() == "Running":
        log(
            f"{TASK_NAME} already has a pending instance (State=Running) but no process yet; "
            f"waiting rather than re-activating (vm_tid={vm_tid})"
        )
        return "pending"
    try:
        ssh_exec(
            ssh,
            f"powershell \"Start-ScheduledTask -TaskName '{TASK_NAME}' -EA SilentlyContinue\"",
            timeout=30,
            quiet=True,
        )
    except Exception as exc:
        log(f"WARNING: re-activation of {TASK_NAME} failed: {exc}")
        return "unknown"
    log(
        f"Re-activated {TASK_NAME} for stage start (vm_tid={vm_tid}, was State={state or '?'})"
    )
    return "restarted" if state is not None else "unknown"


def confirm_stage_started(
    ssh: VmClient, vm_tid: str, stage: str, agent_cli: str
) -> bool:
    """Wait for evidence that runner.ps1 is still going, re-activating the task between rounds.

    Two rounds 45s apart used to be the whole budget, and reaching the end of it was reported as a
    terminal pipeline_not_started -- on a stage that had cost hours of agent time upstream.  Waiting
    longer is not the fix: the marker is written by runner.ps1's third statement, before the pip
    install and before python, so its absence never means "the VM is slow".

    The marker is the authoritative signal and the process check is corroboration: runner.ps1 spends
    its first tens of seconds inside Defender exclusions and a pip install, during which the only
    process is pip.exe and no python* exists yet, so the process check alone declared a healthy stage
    dead.  A read that fails (None) is not evidence either way and does not consume a round.

    What each round's re-activation FOUND is carried to the final log line, because the two outcomes
    need different follow-up: a task that stayed Running throughout was accepted by the scheduler and
    never turned into a process (wait longer, or look at the interactive session), while one that was
    activatable had genuinely lost its instance.
    """
    seen: list[str] = []
    for index, delay in enumerate(STAGE_START_ROUNDS):
        time.sleep(delay)
        elapsed = sum(STAGE_START_ROUNDS[: index + 1])
        marker = read_stage_start_marker(ssh, vm_tid, stage)
        if marker is None:
            # Unknown, not absent. Give the read one more chance before spending the round.
            time.sleep(5)
            marker = read_stage_start_marker(ssh, vm_tid, stage)
        try:
            procs = check_vm_processes(ssh, agent_cli)
        except Exception as exc:
            log(f"WARNING: process check failed during stage start: {exc}")
            procs = {"python": False, "agent": False}
        if marker:
            log(
                f"Pipeline start confirmed after {elapsed}s (marker={marker}, "
                f"python={procs['python']}, agent={procs['agent']})"
            )
            return True
        if procs["python"]:
            # No marker for this stage but a live python: either the marker write failed while the
            # rest of the script ran, or the process belongs to something else -- Get-Process
            # python* also matches a recreated app that happens to be a Python program, and the
            # pytest children eval spawns.  Accepted, because refusing it would fail a stage that is
            # demonstrably executing, but recorded as the weaker signal it is.
            log(
                f"Pipeline start confirmed after {elapsed}s by process only -- no "
                f"{STAGE_START_FILE} for stage {stage} "
                f"(python={procs['python']}, agent={procs['agent']})"
            )
            return True
        remaining = len(STAGE_START_ROUNDS) - index - 1
        if remaining:
            log(
                f"WARNING: no {STAGE_START_FILE} for stage {stage} and no python process after "
                f"{elapsed}s ({remaining} round(s) left)"
            )
            seen.append(reactivate_stage(ssh, vm_tid))
    if seen and set(seen) == {"pending"}:
        log(
            f"NOTE: {TASK_NAME} reported State=Running at every check without ever creating a "
            f"process, so the scheduler accepted the instance and never ran it"
        )
    return False


def poll_stage(
    ssh: VmClient,
    vm_tid: str,
    stage: str,
    poll_interval: int = POLL_INTERVAL,
    cfg: dict[str, str] | None = None,
) -> tuple[dict, VmClient]:
    """Poll VM for stage completion. Returns (result_dict, ssh_client).

    The returned ssh_client may differ from the input if a reconnect occurred.
    """
    state_path = f"{VM_BASE}\\{vm_tid}\\pipeline_state.json"
    deadline = time.time() + STAGE_TIMEOUTS.get(stage, 3600) + TIMEOUT_BUFFER
    consecutive_ssh_errors = 0
    MAX_SSH_ERRORS_BEFORE_RECONNECT = 3
    agent_cli = cfg.get("RB_AGENT_CLI", "claude") if cfg else "claude"

    # Wait briefly then verify pipeline actually started.  The marker is the authoritative signal
    # and the process check is corroboration: runner.ps1 spends its first tens of seconds inside
    # Defender exclusions and a pip install, during which the only process is pip.exe and no
    # python* exists yet, so the process check alone declared a healthy stage dead.
    if not confirm_stage_started(ssh, vm_tid, stage, agent_cli):
        # Before declaring "not started", check if pipeline_state.json exists
        # (pipeline may have completed very quickly)
        try:
            raw = ssh_exec(
                ssh,
                f"powershell \"if (Test-Path '{state_path}') "
                f"{{ Get-Content '{state_path}' -Raw }} "
                f"else {{ Write-Output 'NONE' }}\"",
                timeout=30,
                quiet=True,
            )
            if raw and raw != "NONE":
                state = json.loads(raw)
                stages_done = state.get("stages", {})
                if stage in stages_done:
                    info = stages_done[stage]
                    log(
                        f"Pipeline already completed (fast exit): status={info.get('status', 'unknown')}"
                    )
                    task_dir = f"{VM_BASE}\\{vm_tid}"
                    _dump_stage_logs(ssh, task_dir, stage)
                    return info, ssh
        except Exception:
            pass
        log(
            "ERROR: Pipeline failed to start — runner.ps1 wrote no "
            f"{STAGE_START_FILE} for stage {stage} and no python process is running, "
            f"across {len(STAGE_START_ROUNDS)} rounds spanning "
            f"{sum(STAGE_START_ROUNDS)}s"
        )
        task_dir = f"{VM_BASE}\\{vm_tid}"
        _dump_stage_logs(ssh, task_dir, stage)
        return runtime_failure(ssh, vm_tid, "pipeline_not_started"), ssh

    no_state_count = 0
    stale_stage_count = 0
    poll_start = time.time()
    while time.time() < deadline:
        time.sleep(poll_interval)
        elapsed = int(time.time() - poll_start)
        try:
            raw = ssh_exec(
                ssh,
                f"powershell \"if (Test-Path '{state_path}') "
                f"{{ Get-Content '{state_path}' -Raw }} "
                f"else {{ Write-Output 'NONE' }}\"",
                timeout=30,
                quiet=True,
            )
        except Exception as exc:
            consecutive_ssh_errors += 1
            log(
                f"Poll {stage}: SSH error after {elapsed}s: {exc}, retrying... ({consecutive_ssh_errors}/{MAX_SSH_ERRORS_BEFORE_RECONNECT})"
            )
            if consecutive_ssh_errors >= MAX_SSH_ERRORS_BEFORE_RECONNECT and cfg:
                log(
                    f"Poll {stage}: {consecutive_ssh_errors} consecutive SSH errors, reconnecting..."
                )
                try:
                    ssh.close()
                except Exception:
                    pass
                try:
                    ssh = ssh_connect(cfg)
                    consecutive_ssh_errors = 0
                    log(f"Poll {stage}: SSH reconnected successfully")
                except Exception as reconn_exc:
                    log(f"Poll {stage}: SSH reconnect failed: {reconn_exc}")
            continue

        if raw == "NONE" or not raw:
            consecutive_ssh_errors = 0
            no_state_count += 1
            # Every 5 polls without state file, check if processes are still alive
            if no_state_count % 5 == 0:
                try:
                    procs = check_vm_processes(ssh, agent_cli)
                except Exception as exc:
                    log(
                        f"Poll {stage}: SSH error checking processes after {elapsed}s: {exc}"
                    )
                    continue
                res = check_vm_resources(ssh)
                res_str = (
                    f", mem={res.get('mem_pct', '?')}% avail={res.get('mem_avail_mb', '?')}MB cpu={res.get('cpu_pct', '?')}%"
                    if res
                    else ""
                )
                if not procs["python"]:
                    # Check if state was written before process died
                    try:
                        raw2 = ssh_exec(
                            ssh,
                            f"powershell \"if (Test-Path '{state_path}') "
                            f"{{ Get-Content '{state_path}' -Raw }} "
                            f"else {{ Write-Output 'NONE' }}\"",
                            timeout=30,
                            quiet=True,
                        )
                        if raw2 and raw2 != "NONE":
                            state2 = json.loads(raw2)
                            stages_done2 = state2.get("stages", {})
                            if (
                                stage in stages_done2
                                and stages_done2[stage].get("status") != "running"
                            ):
                                info = stages_done2[stage]
                                log(
                                    f"Pipeline completed (fast exit after {elapsed}s): status={info.get('status', 'unknown')}"
                                )
                                log_stage_exit_evidence(ssh, vm_tid, stage, info)
                                task_dir = f"{VM_BASE}\\{vm_tid}"
                                _dump_stage_logs(ssh, task_dir, stage)
                                return info, ssh
                    except Exception:
                        pass
                    dead, detail = pipeline_death_evidence(ssh, vm_tid, agent_cli)
                    if not dead:
                        log(
                            f"Poll {stage}: no python process after {elapsed}s but "
                            f"{detail}; continuing{res_str}"
                        )
                        continue
                    log(
                        f"ERROR: Pipeline process died after {elapsed}s with no state file"
                        f"{res_str} -- {detail}"
                    )
                    task_dir = f"{VM_BASE}\\{vm_tid}"
                    _dump_stage_logs(ssh, task_dir, stage)
                    return runtime_failure(ssh, vm_tid, "pipeline_crashed"), ssh
                log(
                    f"Poll {stage}: waiting... ({elapsed}s, processes alive: python={procs['python']}, agent={procs['agent']}{res_str})"
                )
            else:
                log(f"Poll {stage}: waiting... ({elapsed}s elapsed)")
            continue

        no_state_count = 0
        consecutive_ssh_errors = 0
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            log(f"Poll {stage}: invalid JSON in pipeline_state.json, retrying...")
            continue

        stages_done = state.get("stages", {})
        if stage in stages_done:
            info = stages_done[stage]
            if info.get("status") != "running":
                log(
                    f"Stage {stage} finished: status={info.get('status', 'unknown')} ({elapsed}s)"
                )
                log_stage_exit_evidence(ssh, vm_tid, stage, info)
                return info, ssh

        stale_stage_count += 1
        if stale_stage_count % 5 == 0:
            try:
                procs = check_vm_processes(ssh, agent_cli)
            except Exception as exc:
                log(
                    f"Poll {stage}: SSH error checking processes after {elapsed}s: {exc}"
                )
                continue
            res = check_vm_resources(ssh)
            res_str = (
                f", mem={res.get('mem_pct', '?')}% avail={res.get('mem_avail_mb', '?')}MB cpu={res.get('cpu_pct', '?')}%"
                if res
                else ""
            )
            if not procs["python"]:
                running_stage = ""
                for s, info in stages_done.items():
                    if info.get("status") == "running":
                        running_stage = (
                            f", running_stage={s} started={info.get('started', '?')}"
                        )
                # This branch never consulted the heartbeat at all, which is why the three real
                # crashes have no last_heartbeat= anywhere in their logs: they all landed here.
                dead, detail = pipeline_death_evidence(ssh, vm_tid, agent_cli)
                if not dead:
                    log(
                        f"Poll {stage}: no python process after {elapsed}s but "
                        f"{detail}; continuing{res_str}{running_stage}"
                    )
                    continue
                log(
                    f"ERROR: Pipeline process died after {elapsed}s (state exists but {stage} "
                    f"not done){res_str}{running_stage} -- {detail}"
                )
                task_dir = f"{VM_BASE}\\{vm_tid}"
                _dump_stage_logs(ssh, task_dir, stage)
                return runtime_failure(ssh, vm_tid, "pipeline_crashed"), ssh
            log(f"Poll {stage}: running ({elapsed}s elapsed, python alive{res_str})")
        else:
            log(f"Poll {stage}: running ({elapsed}s elapsed)")

    agent_pattern = "codex*" if agent_cli.startswith("codex") else "claude*"
    log(
        f"TIMEOUT: {stage} exceeded {STAGE_TIMEOUTS.get(stage, 3600)}s. Killing processes."
    )
    try:
        ssh_exec(
            ssh,
            f'powershell "Get-Process python*,{agent_pattern} -EA SilentlyContinue | Stop-Process -Force"',
            timeout=15,
            quiet=True,
        )
    except Exception as exc:
        log(f"WARNING: Failed to kill processes on timeout: {exc}")
    return (
        exit_contract.stage_outcome(
            status=exit_contract.STAGE_ERROR,
            outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
            reason_code="stage_watchdog_timeout",
        ),
        ssh,
    )


def _dump_stage_logs(ssh: VmClient, task_dir: str, stage: str) -> None:
    """Print last few lines of available logs to help diagnose failures."""
    log_files = {
        "recreation": [],
        "eval": [],
    }
    try:
        # Always dump pipeline.log
        pipeline_log = f"{task_dir}\\pipeline.log"
        content = ssh_exec(
            ssh,
            f"powershell \"if (Test-Path '{pipeline_log}') {{ Get-Content '{pipeline_log}' }} else {{ 'NOT FOUND' }}\"",
            timeout=15,
            quiet=True,
        )
        log(f"  pipeline.log: {content[:1000] if content else '(empty)'}")

        for log_path in log_files.get(stage, []):
            content = ssh_exec(
                ssh,
                f"powershell \"if (Test-Path '{log_path}') {{ Get-Content '{log_path}' -Tail 20 }} else {{ 'NOT FOUND' }}\"",
                timeout=15,
                quiet=True,
            )
            name = ntpath.basename(log_path)
            log(f"  {name}: {content[:500] if content else '(empty)'}")

        if stage == "recreation":
            for fname in ("agent_debug.log", "stderr.log"):
                content = ssh_exec(
                    ssh,
                    f"powershell \"$f = Get-ChildItem '{task_dir}\\recreation*\\{fname}' -EA SilentlyContinue | Select -First 1; if ($f) {{ Get-Content $f -Tail 20 }} else {{ 'NOT FOUND' }}\"",
                    timeout=15,
                    quiet=True,
                )
                log(f"  recreation/{fname}: {content[:500] if content else '(empty)'}")

        # Check scheduled task result
        task_info = ssh_exec(
            ssh,
            'powershell "Get-ScheduledTaskInfo -TaskName RB_Pipeline -EA SilentlyContinue | Select LastRunTime,LastTaskResult | Format-List"',
            timeout=15,
            quiet=True,
        )
        log(f"  Scheduled task: {task_info}")

        # --- Crash diagnostics files (written by runner.ps1 and worker.py) ---
        for diag_name in (
            "pipeline_exit.txt",
            "pipeline_exit_marker.txt",
            "crash_events.txt",
            "pipeline_heartbeat",
        ):
            diag_path = f"{task_dir}\\{diag_name}"
            try:
                content = ssh_exec(
                    ssh,
                    f"powershell \"if (Test-Path '{diag_path}') {{ Get-Content '{diag_path}' -Raw }} else {{ 'NOT FOUND' }}\"",
                    timeout=15,
                    quiet=True,
                )
                if content and content.strip() != "NOT FOUND":
                    log(f"  {diag_name}: {content.strip()[:500]}")
            except Exception:
                pass
    except Exception as exc:
        log(f"  WARNING: Failed to dump stage logs: {exc}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def clean_before_recreation(ssh: VmClient, vm_tid: str) -> None:
    log("--- Cleaning VM before recreation ---")
    task_dir = f"{VM_BASE}\\{vm_tid}"
    for folder in ("repo",):
        path = f"{task_dir}\\{folder}"
        try:
            ssh_exec(
                ssh, f'cmd /c "rd /s /q \\"{path}\\" 2>nul"', timeout=120, quiet=True
            )
        except Exception:
            try:
                ssh_exec(
                    ssh,
                    f'cmd /c "takeown /F \\"{path}\\" /R /D Y >nul 2>&1 & rd /s /q \\"{path}\\" 2>nul"',
                    timeout=300,
                    quiet=True,
                )
            except Exception as exc:
                log(f"WARNING: Failed to clean {folder}: {exc}")
    try:
        remaining = ssh_exec(
            ssh,
            f"powershell \"Get-ChildItem '{task_dir}' -EA SilentlyContinue | Select -Exp Name\"",
            timeout=15,
            quiet=True,
        )
        log(f"Cleanup done, remaining: {remaining.replace(chr(10), ', ')}")
    except Exception:
        log("WARNING: Could not list remaining dirs after cleanup")


# ---------------------------------------------------------------------------
# Result download
# ---------------------------------------------------------------------------


def stage_remote_dir(vm_tid: str, stage: str, cfg: dict[str, str]) -> str:
    suffix = f"_{cfg['RB_DIR_SUFFIX']}" if cfg.get("RB_DIR_SUFFIX") else ""
    if stage == "recreation":
        return f"{VM_BASE}\\{vm_tid}\\recreation{suffix}"
    if stage == "eval":
        return f"{VM_BASE}\\{vm_tid}\\uia_eval_recreation{suffix}"
    return ""


def download_results(
    ssh: VmClient,
    task_id: str,
    stage: str,
    cfg: dict[str, str],
    *,
    vm_tid: str = "",
) -> None:
    vm_tid = vm_tid or _task_hash(task_id)
    remote = stage_remote_dir(vm_tid, stage, cfg)
    local = os.path.join(RESULTS_DIR, task_id, ntpath.basename(remote))
    log(f"Downloading {stage} results: {remote} -> {local}")
    n = sftp_download_dir(ssh, remote, local)
    log(f"Downloaded {n} files")

    # Also download pipeline.log and pipeline_state.json from task directory.
    # Use the shared single-file transfer instead of reaching around VmClient for
    # a raw Paramiko handle (VmClient intentionally does not expose open_sftp()).
    task_dir = f"{VM_BASE}\\{vm_tid}"
    downloaded = 0
    # The SAME tuple the artifact store upload walks. Keeping two hand-maintained lists is what silently
    # dropped stage_start.txt: it was declared uploadable but never fetched, and the upload only
    # ever sees what this loop wrote locally, so the stage-start evidence added for crash
    # attribution was missing from every diagnostics bundle on artifact store while looking present in code.
    for fname in _TASK_DIAGNOSTIC_FILES:
        remote_path = f"{task_dir}\\{fname}"
        local_path = os.path.join(RESULTS_DIR, task_id, fname)
        try:
            ssh.download_file(remote_path, local_path)
            downloaded += 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            log(f"WARNING: Failed to download {remote_path}: {exc}")
    log(f"Downloaded {downloaded} task-level log/state files")


def _artifact_result_target() -> tuple[ArtifactStore, str] | None:
    """Resolve the configured artifact store and this run's result prefix."""
    upload = os.environ.get("RB_ARTIFACT_UPLOAD", "true").lower()
    if upload == "false":
        log("  Artifact upload disabled")
        return None
    prefix = _artifact_prefix()
    if not prefix:
        log("  WARN: artifact upload skipped, missing RB_ARTIFACT_PREFIX")
        return None
    try:
        return _artifact_store(), prefix
    except (ArtifactStoreConfigurationError, ImportError) as exc:
        log(f"  WARN: artifact backend unavailable, skipping upload: {exc}")
        return None


# Written by download_results into RESULTS_DIR/<task_id>, i.e. beside the stage directories
# rather than inside one -- which put them in the blind spot between the two upload channels:
# _upload_stage_artifacts walks only the stage directory, and the shared publisher takes
# $OUTPUT_DIR's
# logs/ and sessions/ plus a root-file allow-list.  pipeline.log is the only record of the
# stage's own markers (agent_terminal, src_files, workspace_snapshot_error, mcp_preflight), so
# diagnosing a failed run meant pulling a multi-hundred-MB trajectory from artifact store instead.
_TASK_DIAGNOSTIC_FILES = (
    "pipeline.log",
    "pipeline_state.json",
    "pipeline_summary.json",
    "pipeline_exit.txt",
    "pipeline_exit_marker.txt",
    STAGE_START_FILE,
    "crash_events.txt",
    "pipeline_heartbeat",
    "rebuild.log",
    "rebuild.attempt-1.log",
    "rebuild.attempt-2.log",
    "rebuild.attempt-3.log",
)
# Anything at that path is a text log; a cap only guards against a pathological producer.
_TASK_DIAGNOSTIC_MAX_BYTES = 100 * 1024 * 1024


def _upload_task_diagnostics(task_id: str, stage: str) -> None:
    """Upload the task-level diagnostics for one stage under a stage-scoped key.

    Scoped by stage because runner.ps1 deletes pipeline.log at the start of every stage and
    download_results writes each stage's copy over the same local path: uploading here, between
    a stage's download and the next stage's launch, is what keeps recreation's copy from being
    replaced by eval's.  The key is ``diagnostics/<stage>/`` and not the stage directory itself,
    which _download_run_output restores verbatim into an eval-only workspace.
    """
    target = _artifact_result_target()
    if target is None:
        return
    store, prefix = target
    local_dir = os.path.join(RESULTS_DIR, task_id)
    try:
        artifact_prefix = f"{prefix}/{task_id}/diagnostics/{stage}/"
        count = 0
        for name in _TASK_DIAGNOSTIC_FILES:
            path = os.path.join(local_dir, name)
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if size > _TASK_DIAGNOSTIC_MAX_BYTES:
                log(f"  WARN: skipping {name}, {size} bytes exceeds the diagnostic cap")
                continue
            try:
                store.put_file(artifact_prefix + name, path)
                count += 1
            except Exception as exc:
                log(f"  WARN: artifact upload failed {name}: {exc}")
        if count:
            log(f"  Uploaded {count} diagnostic file(s) -> {artifact_prefix}")
    except Exception as exc:
        log(f"  WARN: diagnostics upload failed: {exc}")


def _upload_stage_artifacts(
    local_dir: str, instance_id: str, stage_dir_name: str
) -> None:
    """Upload one complete downloaded stage directory."""
    target = _artifact_result_target()
    if target is None:
        return
    store, prefix = target
    if not os.path.isdir(local_dir):
        log(f"  WARN: artifact upload skipped, local dir not found: {local_dir}")
        return
    try:
        artifact_prefix = f"{prefix}/{instance_id}/{stage_dir_name}/"
        result = upload_tree(store, local_dir, artifact_prefix)
        for relative, exc in result.failures:
            log(f"  WARN: artifact upload failed {relative}: {exc}")
        if result.transferred:
            log(f"  Uploaded {result.transferred} files -> {artifact_prefix}")
    except Exception as exc:
        log(f"  WARN: artifact upload failed: {exc}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(task_id: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"Summary: {task_id}")
    results_base = Path(os.path.join(RESULTS_DIR, task_id))
    if not results_base.exists():
        print("  No results found.")
        print(f"{'=' * 60}")
        return
    for d in sorted(results_base.iterdir()):
        if not d.is_dir():
            continue
        for name in ("programmatic_results.json", "vlm_results.json"):
            p = d / name
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                label = "Programmatic" if "programmatic" in name else "VLM"
                passed = data.get("passed", 0)
                total = data.get("total", 0)
                rate = data.get("pass_rate", 0)
                print(f"  {label}: {passed}/{total} ({rate:.1%})")
    print(f"{'=' * 60}")


def _read_eval_results(base_dir: str) -> dict | None:
    """Read programmatic_results.json and vlm_results.json from a directory.

    Returns a dict with the key fields, or None if no results exist.
    """
    info: dict = {}

    prog_path = os.path.join(base_dir, "programmatic_results.json")
    if os.path.isfile(prog_path):
        try:
            data = json.loads(Path(prog_path).read_text(encoding="utf-8-sig"))
            info["programmatic_pass_rate"] = float(data.get("pass_rate", 0))
            info["programmatic_total"] = int(data.get("total", 0))
            if data.get("scored_against") == "manifest":
                info["scoring_method"] = "manifest"
            else:
                info["scoring_method"] = "dynamic"
        except Exception:
            pass

    vlm_path = os.path.join(base_dir, "vlm_results.json")
    if os.path.isfile(vlm_path):
        try:
            data = json.loads(Path(vlm_path).read_text(encoding="utf-8-sig"))
            info["vlm_pass_rate"] = float(data.get("pass_rate", 0))
            info["vlm_total"] = int(data.get("total", 0))
            info["vlm_judge_errors"] = int(data.get("judge_errors", 0))
        except Exception:
            pass

    return info if info else None


def write_pipeline_summary(
    task_id: str,
    cfg: dict[str, str],
    stage: str,
    stage_results: dict[str, dict],
) -> dict:
    """Write pipeline_summary.json with per-stage outcomes and eval results."""
    model = cfg.get("MODEL") or cfg.get("ANTHROPIC_MODEL", "")
    suffix = cfg.get("RB_DIR_SUFFIX", "")
    results_base = os.path.join(RESULTS_DIR, task_id)
    selected = stages_for(stage)
    canonical = {
        name: exit_contract.validate_stage_outcome(stage_results[name])
        for name in selected
    }
    aggregate = exit_contract.aggregate_stage_outcomes(canonical, selected)
    native_exit = exit_contract.selected_native_exit_code(canonical, selected)

    summary: dict = {
        "model": model,
        "stage": stage,
        "stages": {name: value["status"] for name, value in canonical.items()},
        "stage_outcomes": canonical,
        "native_exit_code": (
            native_exit if native_exit is not None else aggregate["process_exit_code"]
        ),
        **aggregate,
    }
    for name, value in canonical.items():
        if value["status"] != exit_contract.STAGE_PASS:
            summary[f"{name}_fail_code"] = value["reason_code"]

    eval_dir_name = f"uia_eval_recreation_{suffix}" if suffix else "uia_eval_recreation"
    eval_dir = os.path.join(results_base, eval_dir_name)
    if "eval" in stage_results:
        summary["eval_eval"] = _read_eval_results(eval_dir)

    # Extract failure hint from pipeline.log tail (last 5 lines).  It becomes metrics' exit_reason,
    # which is the only account of the failure that leaves the VM: deployment platform exposes exit_code but leaves
    # agent_exit_reason null and fills failed_reason with "exit code 2 detail_msg:Error (exit code 2)".
    #
    # Not for a stage whose runner.ps1 never ran, though.  runner.ps1 deletes pipeline.log as its
    # fourth statement, so a stage that never got that far leaves the PREVIOUS stage's log in place
    # and the tail then describes the wrong stage with the wrong verdict. A hint that
    # contradicts the exit code is worse than no hint.
    failing = next(
        (
            (name, value)
            for name, value in canonical.items()
            if value["status"] != exit_contract.STAGE_PASS
        ),
        None,
    )
    if failing and failing[1]["reason_code"] in STAGE_NEVER_RAN_REASONS:
        failed_name, failed_value = failing
        summary["failure_hint"] = (
            f"{failed_name}: {failed_value['reason_code']} — runner.ps1 never ran for this stage, "
            f"so it wrote no pipeline.log of its own; any log present belongs to the previous stage"
        )
    else:
        pipeline_log_path = os.path.join(results_base, "pipeline.log")
        if os.path.isfile(pipeline_log_path):
            try:
                lines = (
                    Path(pipeline_log_path)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
                summary["failure_hint"] = "\n".join(lines[-5:]).strip()
            except Exception:
                pass

    summary_path = os.path.join(results_base, "pipeline_summary.json")
    try:
        Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
        Path(summary_path).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log("Wrote pipeline_summary.json")
    except Exception as exc:
        log(f"WARNING: Failed to write pipeline_summary.json: {exc}")
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    cfg = load_config()
    cfg["RB_DIR_SUFFIX"] = os.environ.get("RB_DIR_SUFFIX", "")
    cfg["RB_THINKING_EFFORT"] = os.environ.get("RB_THINKING_EFFORT", "") or "max"
    cfg["RB_CUA_DRIVER_REF"] = os.environ.get("RB_CUA_DRIVER_REF", "")
    cfg["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = os.environ.get(
        "RB_CLAUDE_CODE_AUTO_COMPACT_WINDOW", ""
    )
    cfg["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = os.environ.get(
        "RB_CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", ""
    )
    cfg["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.environ.get(
        "RB_CLAUDE_CODE_MAX_OUTPUT_TOKENS", ""
    )
    _coord = os.environ.get("RB_CUA_COORDINATE_SPACE", "")
    _, _, _ref_normalize = core_cua.parse_ref(cfg["RB_CUA_DRIVER_REF"])
    cfg["RB_CUA_COORDINATE_SPACE"] = _coord or ("1" if _ref_normalize else "0")
    cfg["RB_CUA_COORDINATE_SCALE"] = (
        os.environ.get("RB_CUA_COORDINATE_SCALE", "") or "1000"
    )
    cfg["RB_MCP_MODEL_PAYLOAD_FILTER"] = (
        os.environ.get("RB_MCP_MODEL_PAYLOAD_FILTER", "") or "0"
    )
    cfg["RB_EXTRA_BODY"] = os.environ.get("EXTRA_BODY", "")
    # RB_AGENT_CLI: "claude" (default) — Claude Code agent
    #               "codex"           — OpenAI Codex CLI + desktop-control MCP
    #               "codex-mcp"       — backwards-compatible alias for the same Codex MCP path
    cfg["RB_AGENT_CLI"] = os.environ.get("RB_AGENT_CLI", "") or "claude"
    cfg["RB_WINDOWS_SKIP_DEP_INSTALL"] = os.environ.get(
        "RB_WINDOWS_SKIP_DEP_INSTALL", "1"
    )
    cfg["RB_WINDOWS_SKIP_AGENT_INSTALL"] = os.environ.get(
        "RB_WINDOWS_SKIP_AGENT_INSTALL", "1"
    )
    cfg["RB_API_TIMEOUT_MS"] = os.environ.get("RB_API_TIMEOUT_MS", "") or "1800000"
    cfg["RB_MCP_TOOL_TIMEOUT"] = os.environ.get("RB_MCP_TOOL_TIMEOUT", "") or "180000"
    cfg["RB_EVAL_SCREENSHOT_MODE"] = os.environ.get("RB_EVAL_SCREENSHOT_MODE", "")
    cfg["RB_CAPTURE_TOOL_USE_SCREENSHOTS"] = os.environ.get(
        "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
    )
    cfg["RB_RECREATION_TIME_HOOK"] = os.environ.get("RB_RECREATION_TIME_HOOK", "")
    cfg["RB_WINDOWS_ENABLE_NETWORK_ISOLATION"] = os.environ.get(
        "RB_WINDOWS_ENABLE_NETWORK_ISOLATION", "1"
    )
    cfg["RB_WINDOWS_NETWORK_DENY_PROBE"] = os.environ.get(
        "RB_WINDOWS_NETWORK_DENY_PROBE", "https://1.1.1.1:443"
    )
    for name in (
        "RB_VLM_JUDGE_RETRIES",
        "RB_VLM_JUDGE_MIN_INTERVAL",
        "RB_VLM_JUDGE_BATCH_SIZE",
    ):
        cfg[name] = os.environ.get(name, "").strip()
    cfg["VLM_EXTRA_HEADERS"] = os.environ.get("VLM_EXTRA_HEADERS", "")

    task_id = cfg["INSTANCE_ID"]
    vm_tid = _task_hash(task_id)
    mask_enabled = vm_tid != task_id
    stage_token = cfg["STAGE"]
    stages = list(stages_for(stage_token))
    first_stage = stages[0]

    log("=== Recreation-Bench Orchestrator ===")
    for var in REQUIRED_VARS + REQUIRED_VARS_LLM + REQUIRED_VARS_VLM:
        val = cfg.get(var, "")
        if "KEY" in var or "PASSWORD" in var:
            val = val[:4] + "****" if len(val) > 4 else "****"
        if val:
            print(f"  {var}: {val}")
    print(f"  Stage timeouts: {STAGE_TIMEOUTS}")
    print(
        f"  Task-ID masking: {'ON (vm_tid=' + vm_tid + ')' if mask_enabled else 'OFF'}"
    )
    print(f"  Dir suffix: {cfg.get('RB_DIR_SUFFIX', '') or '(none)'}")
    print(f"  Thinking effort: {cfg.get('RB_THINKING_EFFORT', 'max')}")
    print(
        f"  Auto-compact window: {cfg.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW', '') or '(none)'}"
    )
    print(
        f"  Auto-compact pct: {cfg.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE', '') or '(none)'}"
    )
    print(f"  CUA driver ref: {cfg.get('RB_CUA_DRIVER_REF', '') or '(none)'}")
    print(f"  CUA coordinate space: {cfg.get('RB_CUA_COORDINATE_SPACE', '0')}")
    print(f"  CUA coordinate scale: {cfg.get('RB_CUA_COORDINATE_SCALE', '1000')}")
    print(f"  MCP model payload filter: {cfg.get('RB_MCP_MODEL_PAYLOAD_FILTER', '0')}")
    eb = cfg.get("RB_EXTRA_BODY", "")
    print(f"  Extra body: {eb[:80] + '...' if len(eb) > 80 else eb or '(none)'}")
    print(f"  Agent CLI: {cfg.get('RB_AGENT_CLI', 'claude')}")

    ssh = ssh_connect(cfg)
    failed = False
    stage_durations: dict[str, float] = {}
    stage_results: dict[str, dict] = {}

    try:
        sync_code(ssh, cfg)
        ssh_exec(
            ssh,
            f"powershell \"New-Item -ItemType Directory -Path '{VM_BASE}' -Force | Out-Null\"",
            timeout=15,
            quiet=True,
        )
        driver_binary = upgrade_cua_driver(ssh, cfg)
        configure_cua_driver_env(
            ssh,
            cfg["RB_CUA_COORDINATE_SPACE"],
            cfg["RB_CUA_COORDINATE_SCALE"],
            cfg["RB_MCP_MODEL_PAYLOAD_FILTER"],
            driver_binary=driver_binary or "cua-driver",
        )
        reconnect_rdp(ssh)

        if first_stage != "setup":
            try:
                prepare_vm_for_stage(ssh, task_id, first_stage, cfg, vm_tid=vm_tid)
            except (FrozenInputError, RuntimeError) as e:
                err_msg = str(e)
                log(f"ERROR: prepare_vm_for_stage failed: {err_msg}")
                if isinstance(e, FrozenInputError):
                    outcome = exit_contract.OUTCOME_DATA_ERROR
                    reason_code = "invalid_or_missing_frozen_input"
                else:
                    outcome = exit_contract.OUTCOME_INFRA_ERROR
                    reason_code = "prepare_failed"
                stage_results[first_stage] = exit_contract.stage_outcome(
                    status=(
                        exit_contract.STAGE_FAIL
                        if outcome == exit_contract.OUTCOME_DATA_ERROR
                        else exit_contract.STAGE_ERROR
                    ),
                    outcome_class=outcome,
                    reason_code=reason_code,
                )
                failed = True
                # The stage loop breaks below, so this is the only chance to keep prepare's own
                # evidence -- the reference build's attempt logs land here and nowhere else.
                _upload_task_diagnostics(task_id, f"prepare_{first_stage}")

        for stage in stages:
            if failed:
                break
            if stage == "setup":
                # Establishes and ATTESTS the agent's file boundary, then exits. Driven from HERE, the
                # pod, and judged here: the probe has to execute on the VM because that is where the
                # paths are, but letting the machine under attestation also decide whether it passed is
                # the weaker arrangement -- and it is what made `exit=1` unreadable for several runs.
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from core import permission_probe

                base = f"{VM_BASE}\\{vm_tid}"
                ok, transcript = permission_probe.run_remote(
                    ssh,
                    "windows",
                    task_id,
                    [f"{base}\\reference", f"{base}\\tests"],
                    [stage_remote_dir(vm_tid, "recreation", cfg)],
                )
                print(transcript[-6000:] if transcript else "(no output from the VM)")
                stage_results["setup"] = exit_contract.stage_outcome(
                    status=(
                        exit_contract.STAGE_PASS if ok else exit_contract.STAGE_ERROR
                    ),
                    outcome_class=(
                        exit_contract.OUTCOME_COMPLETED
                        if ok
                        else exit_contract.OUTCOME_INFRA_ERROR
                    ),
                    reason_code="completed" if ok else "boundary_not_attested",
                )
                failed = not ok
                continue
            if stage == "recreation":
                clean_before_recreation(ssh, vm_tid)

            stage_t0 = time.time()
            if not launch_stage(ssh, vm_tid, stage, cfg):
                # Short-circuit instead of polling: poll_stage confirms a start on
                # `marker or procs["python"]`, and the process that blocked the launch is itself a
                # live python, so polling would attribute the previous stage's state to this one.
                # Nothing new was written on the VM either, so there is no diagnostics upload to do.
                stage_results[stage] = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code="stage_launch_blocked",
                )
                stage_durations[stage] = time.time() - stage_t0
                failed = True
                continue
            result, ssh = poll_stage(ssh, vm_tid, stage, cfg=cfg)
            try:
                download_results(ssh, task_id, stage, cfg, vm_tid=vm_tid)
            except Exception as exc:
                log(f"ERROR: Failed to download results for {stage}: {exc}")
                result = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code="artifact_sftp_download_failed",
                    native_exit_code=result.get("native_exit_code"),
                )

            remote = stage_remote_dir(vm_tid, stage, cfg)
            local_stage_dir = os.path.join(
                RESULTS_DIR, task_id, ntpath.basename(remote)
            )
            _upload_stage_artifacts(local_stage_dir, task_id, ntpath.basename(remote))
            # Here, not after the loop: the next stage's runner truncates pipeline.log and its
            # download overwrites the local copy, so this is the last moment this stage's own
            # diagnostics still exist.
            _upload_task_diagnostics(task_id, stage)

            stage_durations[stage] = time.time() - stage_t0

            try:
                stage_results[stage] = exit_contract.validate_stage_outcome(result)
            except ValueError:
                status = str(result.get("status") or exit_contract.STAGE_ERROR)
                stage_results[stage] = exit_contract.outcome_from_stage_status(
                    status,
                    raw=result,
                    native_exit_code=result.get("native_exit_code"),
                )

            if stage_results[stage]["status"] != exit_contract.STAGE_PASS:
                task_dir = f"{VM_BASE}\\{vm_tid}"
                _dump_stage_logs(ssh, task_dir, stage)
                log(
                    f"Stage {stage} did not pass "
                    f"(status={stage_results[stage]['status']}, "
                    f"outcome={stage_results[stage]['outcome_class']}, "
                    f"reason={stage_results[stage]['reason_code']}). Stopping."
                )
                failed = True
                break

        if len(stage_results) < len(stages):
            upstream = next(
                (
                    value
                    for name, value in stage_results.items()
                    if name in stages and value["status"] != exit_contract.STAGE_PASS
                ),
                exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code="stage_result_missing",
                ),
            )
            for pending in stages:
                if pending not in stage_results:
                    stage_results[pending] = exit_contract.stage_outcome(
                        status=exit_contract.STAGE_NOT_RUN,
                        outcome_class=upstream["outcome_class"],
                        reason_code="upstream_stage_failed",
                    )

        print_summary(task_id)
        pipeline_summary = write_pipeline_summary(
            task_id, cfg, stage_token, stage_results
        )
        # pipeline_summary.json only exists now, and it is the file the pod-side adapter turns
        # into the run's whole normalized state -- worth having beside the artifacts it scores.
        _upload_task_diagnostics(task_id, "run")

    finally:
        if os.environ.get("RB_WINDOWS_KEEP_SSH_OPEN", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            log("SSH connection retained by persistent controller")
        else:
            ssh.close()
            log("SSH connection closed")

    output_dir = os.environ.get("OUTPUT_DIR", "").strip()
    if output_dir:
        local_results = os.path.join(RESULTS_DIR, task_id)
        if os.path.isdir(local_results):
            dst = os.path.join(output_dir, task_id)
            log(f"Copying results to {dst}")
            shutil.copytree(local_results, dst, dirs_exist_ok=True)
            n = sum(len(files) for _, _, files in os.walk(dst))
            log(f"{n} files copied")
            suffix = f"_{cfg['RB_DIR_SUFFIX']}" if cfg.get("RB_DIR_SUFFIX") else ""
            recreation_stage = os.path.join(local_results, f"recreation{suffix}")
            surfaced = core_trajectory.surface_stage(recreation_stage, output_dir)
            log(
                f"Surfaced {len(surfaced)} canonical trajectory file(s) to {output_dir}"
            )

    total_sec = time.time() - _T0
    log("=== Orchestrator finished ===")
    log(f"  Status: {'FAILED' if failed else 'PASSED'}")
    log(f"  Total duration: {total_sec:.0f}s ({total_sec / 60:.1f}m)")
    for s, d in stage_durations.items():
        log(f"  Stage {s}: {d:.0f}s ({d / 60:.1f}m)")

    return int(pipeline_summary["process_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
