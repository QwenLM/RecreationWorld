#!/usr/bin/env python3
"""macOS benchmark runtime for an already prepared SSH target.

Deploys the pipeline code and CUA Driver, runs ``run_full.sh``, and collects
results back to ``$OUTPUT_DIR``. The caller owns target and endpoint lifecycle.

Called only by ``platforms/macos/pipeline.py``.
"""

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

# The runtime has no dependency on an infrastructure-specific sandbox SDK. The deployment
# adapter has already created and readied the target before this process starts.

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS_DIR))
# ``rb_unify`` intentionally lives under scripts/common rather than at the
# scripts package root.  This runtime used to live in the deployment integration,
# where its function-local ``../common`` lookup happened to find that package.
# Keep the import root explicit now that the runtime is the standard macOS
# platform implementation under scripts/platforms/macos.
sys.path.insert(0, str(SCRIPTS_DIR / "common"))
from core import (  # noqa: E402
    agent_config,
    cua_driver,
    exit_contract,
    mcp_settings,
    trajectory,
    vmssh,
)
from core.artifact_store import normalize_artifact_key  # noqa: E402
from core.pipeline import (  # noqa: E402
    normalize_cua_preflight_mode,
    normalize_eval_target,
    stages_for,
)
from core.vmclient import VmClient  # noqa: E402
from infrastructure.artifacts import (  # noqa: E402
    artifact_store_from_environment,
)

# ── Env ──────────────────────────────────────────────────────────
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/output")
SHARED_DIR = os.environ.get("SHARED_DIR", "/tmp/shared")
TASK_ID = os.environ.get("TASK_ID", os.environ.get("INSTANCE_ID", "unknown"))
STAGE = os.environ.get("STAGE", "recreation_eval")
EVAL_TARGET = normalize_eval_target(os.environ.get("RB_EVAL_TARGET"))

MACOS_USER = os.environ.get("MACOS_USER", "").strip()
MACOS_PASS = os.environ.get("MACOS_PASS", "")
MACOS_KEY_PATH = os.environ.get("MACOS_KEY_PATH", "").strip()
MACOS_DEVAGENT_PASSWORD = os.environ.get("MACOS_DEVAGENT_PASSWORD", "")
MACOS_PORT = int(os.environ.get("MACOS_PORT", "22") or 22)
SUBUSER = os.environ.get("SUBUSER", "devagent")

APP_NAME = os.environ.get("APP_NAME", TASK_ID)

MODEL = os.environ.get("MODEL", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
# Recreation agent CLI: claude (default) or codex. `agent_cli` is the shared param name the
# other four platforms already use, so one profile key selects the agent everywhere.
AGENT_CLI = os.environ.get("AGENT_CLI", "claude")
SCAFFOLD_VERSION = os.environ.get("SCAFFOLD_VERSION", "")
PROXY_CLIENT_API_KEY = (
    os.environ.get("RB_AGENT_API_KEY")
    or os.environ.get("RB_TARGET_PROXY_CLIENT_API_KEY")
    or "proxy"
)
TARGET_MODEL_BASE_URL = os.environ.get("RB_AGENT_BASE_URL") or os.environ.get(
    "RB_TARGET_MODEL_BASE_URL", ""
)

SANDBOX_TIMEOUT_SEC = int(os.environ.get("SANDBOX_TIMEOUT_SEC", "86400"))
RECREATION_TIMEOUT = int(os.environ.get("RECREATION_TIMEOUT", "72000"))
# Same per-test budget for both eval targets.  The frozen suite was pruned against the
# REFERENCE at 120s, so grading the recreation at 30s judges it under a stricter clock than the
# one that defined the denominator: every test whose fixture relaunches the app and waits for its
# AX tree (conftest waits up to 45s, which heavy Qt/Electron apps need) is killed mid-setup and
# recorded as a failure it never had a chance to avoid.
EVAL_TEST_TIMEOUT = int(os.environ.get("RB_EVAL_TEST_TIMEOUT", "120"))

THINKING_EFFORT = os.environ.get("THINKING_EFFORT", "")
MAX_TOKENS_LIMIT = os.environ.get("MAX_TOKENS_LIMIT", "")

# VLM judge config (optional)
VLM_MODEL = os.environ.get("VLM_MODEL", "")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "")
VLM_FORWARD_PORT = int(os.environ.get("RB_VLM_FORWARD_PORT", "19779") or 19779)

RB_ARTIFACT_PREFIX = os.environ.get("RB_ARTIFACT_PREFIX", "")
RB_ARTIFACT_RESTORE_PREFIX = os.environ.get("RB_ARTIFACT_RESTORE_PREFIX", "")
CLAUDE_CODE_AUTO_COMPACT_WINDOW = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")
CONTEXT_1M = os.environ.get("CONTEXT_1M", "false")
RB_CUA_PREFLIGHT_MODE = normalize_cua_preflight_mode(
    os.environ.get("RB_CUA_PREFLIGHT_MODE")
)

# Pipeline deploy path on the macOS target, resolved after SSH connects.
VM_PIPELINE_DIR_TEMPLATE = os.environ.get("VM_PIPELINE_DIR", "$HOME/pipeline")
VM_PIPELINE_DIR = ""  # set to absolute path after SSH connect
MACOS_HOME = ""  # set to the login account's actual home after SSH connect
VM_RESULTS_DIR = "/var/tmp/pipeline_results"

_TRANSFER_CLIENT: VmClient | None = None
_TRANSFER_HOST = ""
_TRANSFER_LOCK = threading.Lock()


class FrozenInputError(RuntimeError):
    """The frozen benchmark input was reached but is missing or malformed."""


class CandidateInputError(FrozenInputError):
    """A requested eval-only candidate is absent or fails artifact validation."""


def log(msg=""):
    print(f"[rb-macos] {msg}", flush=True)


def _ssh_prefix(host: str, extra_options: list[str] | None = None) -> str:
    """Build one shell-safe OpenSSH prefix for password, key, or agent auth."""
    argv = vmssh.ssh_argv(
        host,
        MACOS_USER,
        port=MACOS_PORT,
        connect_timeout=30,
        with_password=bool(MACOS_PASS),
        key_path=MACOS_KEY_PATH,
    )
    if extra_options:
        argv[-1:-1] = extra_options
    return " ".join(shlex.quote(part) for part in argv)


def ssh(host, cmd, timeout=300, check=True, stream=False):
    """Run a command on the macOS target over SSH.

    Quote the complete remote command as one local-shell argument so $HOME and
    other shell variables are expanded by the REMOTE shell, not the local pod
    shell.  ``shlex.quote`` also preserves embedded quotes in heredocs and
    Python snippets; hand-wrapping this value in single quotes corrupts them.
    """
    env = vmssh.env_with_password(os.environ, MACOS_PASS)
    full_cmd = f"{_ssh_prefix(host)} {shlex.quote(cmd)}"
    if stream:
        proc = subprocess.Popen(
            full_cmd,
            shell=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output_lines = []
        for line in proc.stdout:
            print(line, end="", flush=True)
            output_lines.append(line)
        proc.wait()
        output = "".join(output_lines)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output)
        return proc.returncode, output
    else:
        r = subprocess.run(
            full_cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and r.returncode != 0:
            log(f"SSH FAILED (rc={r.returncode}): {cmd[:100]}")
            log(f"  stderr: {r.stderr[:500]}")
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r.returncode, r.stdout.strip()


def transfer_client(host: str) -> VmClient:
    """Return the shared Paramiko/SFTP transport for this macOS target."""
    global _TRANSFER_CLIENT, _TRANSFER_HOST
    if _TRANSFER_CLIENT is None or _TRANSFER_HOST != host:
        if _TRANSFER_CLIENT is not None:
            _TRANSFER_CLIENT.close()
        _TRANSFER_CLIENT = VmClient(
            host=host,
            user=MACOS_USER,
            password=MACOS_PASS,
            port=MACOS_PORT,
            key_path=MACOS_KEY_PATH,
            connect_timeout=30,
        )
        _TRANSFER_HOST = host
    return _TRANSFER_CLIENT


def close_transfer_client() -> None:
    global _TRANSFER_CLIENT, _TRANSFER_HOST
    with _TRANSFER_LOCK:
        if _TRANSFER_CLIENT is not None:
            _TRANSFER_CLIENT.close()
        _TRANSFER_CLIENT = None
        _TRANSFER_HOST = ""


def sftp_upload_file(
    host: str, local_path: str, remote_path: str, *, mode: int | None = None
) -> None:
    """Upload one file to the macOS target through the shared Paramiko transport."""
    with _TRANSFER_LOCK:
        transfer_client(host).upload_file(local_path, remote_path, mode=mode)


def _shell_export(name: str, value: object) -> str:
    """Render one shell-safe environment assignment."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid environment variable name: {name!r}")
    return f"export {name}={shlex.quote(str(value))}"


def _upload_private_text(host: str, content: str, remote_path: str) -> None:
    """Upload sensitive text without leaving a broadly readable local or remote copy."""
    descriptor, local_path = tempfile.mkstemp(prefix="rb-macos-private-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        sftp_upload_file(host, local_path, remote_path, mode=0o600)
    finally:
        Path(local_path).unlink(missing_ok=True)


def _upload_private_environment(
    host: str, values: dict[str, object], *, label: str
) -> str:
    remote_path = f"/tmp/rb-{label}-{os.getpid()}-{secrets.token_hex(8)}.env"
    content = "\n".join(_shell_export(name, value) for name, value in values.items()) + "\n"
    _upload_private_text(host, content, remote_path)
    return remote_path


def sftp_download_file(host: str, remote_path: str, local_path: str) -> None:
    """Download one file from the macOS target through the shared Paramiko transport."""
    with _TRANSFER_LOCK:
        transfer_client(host).download_file(remote_path, local_path)


def sftp_download_dir(host: str, remote_path: str, local_path: str) -> int:
    """Download a directory tree from the macOS target through Paramiko/SFTP."""
    with _TRANSFER_LOCK:
        return transfer_client(host).download_dir(remote_path, local_path)


def run_setup_permission_probe(host):
    """Run the shared permission-boundary protocol on the macOS target.

    The pipeline tree is already deployed, so invoke the same core/permission_probe.py CLI used by
    the other platforms.  The sudo password travels on stdin; neither the local nor remote process
    argv contains it.
    """
    work_dir = f"{MACOS_HOME or f'/Users/{MACOS_USER}'}/pipeline_work/{APP_NAME}"
    remote_dir = "/tmp/rb_permission"
    cli = [
        "python3",
        f"{VM_PIPELINE_DIR}/scripts/core/permission_probe.py",
        "--platform",
        "macos",
        "--run-id",
        TASK_ID,
        "--protected",
        f"{work_dir}/reference",
        "--protected",
        f"{work_dir}/tests",
        "--writable",
        f"/Users/{SUBUSER}/Recreation/{APP_NAME}",
        "--user",
        SUBUSER,
        "--spec-out",
        f"{remote_dir}/spec.json",
        "--report",
        f"{remote_dir}/report.json",
    ]
    command = " ".join(shlex.quote(part) for part in cli)
    remote_command = (
        "read -r RB_SUDO_PASSWORD\n"
        f"mkdir -p {shlex.quote(remote_dir)}\n"
        'printf "%s\\n" "$RB_SUDO_PASSWORD" | '
        f"sudo -S -E env PYTHONPATH={shlex.quote(VM_PIPELINE_DIR + '/scripts')} {command}\n"
        "probe_rc=$?\n"
        'printf "%s\\n" "$RB_SUDO_PASSWORD" | '
        f"sudo -S chmod 0644 {shlex.quote(remote_dir + '/report.json')} 2>/dev/null || true\n"
        f"cat {shlex.quote(remote_dir + '/report.json')} 2>/dev/null || true\n"
        'exit "$probe_rc"'
    )
    argv = vmssh.ssh_argv(
        host,
        MACOS_USER,
        port=MACOS_PORT,
        connect_timeout=30,
        with_password=bool(MACOS_PASS),
        key_path=MACOS_KEY_PATH,
    ) + [remote_command]
    result = subprocess.run(
        argv,
        env=vmssh.env_with_password(os.environ, MACOS_PASS),
        input=f"{MACOS_PASS}\n",
        capture_output=True,
        text=True,
        timeout=720,
    )
    transcript = (
        result.stdout
        + (("\n" + result.stderr) if (result.stderr or "").strip() else "")
    ).strip()
    log("Permission setup probe:")
    print(transcript or "(no output from the target)", flush=True)
    try:
        sftp_download_file(
            host,
            f"{remote_dir}/report.json",
            os.path.join(OUTPUT_DIR, "permission_report.json"),
        )
    except Exception as exc:
        log(f"  WARN: permission report could not be collected: {exc}")
    return (
        result.returncode == 0
        and "SETUP OK" in transcript
        and "SETUP FAILED" not in transcript
    )


def resolve_vm_pipeline_dir(host):
    """Resolve the remote home and pipeline directory via SSH.

    Needed because SFTP paths do not expand shell variables like $HOME.
    SSH with single quotes expands $HOME on the remote side.
    """
    global MACOS_HOME, VM_PIPELINE_DIR
    rc, remote_home = ssh(host, 'printf %s "$HOME"')
    if rc != 0 or not remote_home.startswith("/"):
        raise RuntimeError("Could not resolve the macOS login account home")
    MACOS_HOME = remote_home
    resolved = VM_PIPELINE_DIR_TEMPLATE.replace("$HOME", MACOS_HOME)
    if not resolved.startswith("/"):
        raise RuntimeError(
            f"Could not resolve VM_PIPELINE_DIR: {VM_PIPELINE_DIR_TEMPLATE}"
        )
    VM_PIPELINE_DIR = resolved
    log(f"  Target pipeline dir: {VM_PIPELINE_DIR}")


def deploy_pipeline(host, pipeline_root):
    """Deploy the curated runtime roots to the macOS target."""
    log("Deploying pipeline code to macOS target...")

    # Ship only executable runtime roots.  In particular, never copy ignored
    # controller-side .env files, results, build products, or local backups.
    descriptor, tarball = tempfile.mkstemp(
        prefix="rb-macos-runtime-", suffix=".tar.gz"
    )
    os.close(descriptor)
    remote_tarball = f"/tmp/rb-macos-runtime-{os.getpid()}-{secrets.token_hex(8)}.tar.gz"
    uploaded = False
    members = [
        name
        for name in ("scripts", "src", "pyproject.toml")
        if os.path.exists(os.path.join(pipeline_root, name))
    ]
    try:
        subprocess.run(
            [
                "tar",
                "czf",
                tarball,
                "--exclude=.env",
                "--exclude=.env.*",
                "--exclude=.local-backups",
                "--exclude=.mypy_cache",
                "--exclude=.pytest_cache",
                "--exclude=.ruff_cache",
                "--exclude=.venv",
                "--exclude=__pycache__",
                "--exclude=build",
                "--exclude=dist",
                "--exclude=*.egg-info",
                "--exclude=node_modules",
                "--exclude=results",
                "--exclude=tests",
                "--exclude=scripts/release",
                "-C",
                pipeline_root,
                *members,
            ],
            check=True,
            timeout=60,
        )
        size_mb = os.path.getsize(tarball) / 1024 / 1024
        log(f"  Pipeline tarball: {size_mb:.1f} MB")

        ssh(host, f"mkdir -p {shlex.quote(VM_PIPELINE_DIR)}")
        sftp_upload_file(host, tarball, remote_tarball)
        uploaded = True
        ssh(
            host,
            f"tar xzf {shlex.quote(remote_tarball)} -C "
            f"{shlex.quote(VM_PIPELINE_DIR)}",
        )
    finally:
        Path(tarball).unlink(missing_ok=True)
        if uploaded:
            try:
                ssh(host, f"rm -f {shlex.quote(remote_tarball)}", check=False)
            except Exception:
                pass

    # Verify
    runner = f"{VM_PIPELINE_DIR}/scripts/macos/run_full.sh"
    rc, out = ssh(host, f"test -f {shlex.quote(runner)}", check=False)
    if rc != 0:
        raise RuntimeError("Pipeline deployment failed: run_full.sh not found on target")
    log("  Pipeline deployed successfully")


def prepare_sandbox(host):
    """Prepare the target: Gatekeeper, keychain, sleep, Xcode, popups, git.

    Runs prepare_sandbox.sh after it has been deployed by ``deploy_pipeline``.
    Must run BEFORE deploy_cua_driver so Gatekeeper is off and devagent exists.
    """
    log("Preparing macOS target...")

    prep_script = f"{VM_PIPELINE_DIR}/scripts/macos/runtime_assets/prepare_sandbox.sh"
    rc, _ = ssh(host, f"test -f {prep_script}", check=False)
    if rc != 0:
        log("  WARN: prepare_sandbox.sh not found on target, skipping")
        return

    remote_env = _upload_private_environment(
        host,
        {
            "MACOS_PASS": MACOS_PASS,
            "DEVAGENT_USER": SUBUSER,
            "MACOS_DEVAGENT_PASSWORD": MACOS_DEVAGENT_PASSWORD,
        },
        label="prepare",
    )

    try:
        rc, out = ssh(
            host,
            f"env_file={shlex.quote(remote_env)}; "
            "trap 'rm -f \"$env_file\"' EXIT; "
            f"source \"$env_file\" && bash {shlex.quote(prep_script)}",
            check=False,
            stream=True,
        )
    finally:
        ssh(host, f"rm -f {shlex.quote(remote_env)}", check=False)
    if rc != 0:
        raise RuntimeError(f"prepare_sandbox.sh failed (rc={rc})")
    log("  Sandbox preparation complete")


def install_codex_cli(host):
    """Install pinned Codex before recreation disables npm and package networking."""
    if not AGENT_CLI.startswith("codex"):
        return

    version = SCAFFOLD_VERSION or "0.145.0"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError(f"invalid Codex CLI version: {version!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", SUBUSER):
        raise RuntimeError(f"invalid macOS subuser: {SUBUSER!r}")
    host_home = MACOS_HOME or f"/Users/{MACOS_USER}"
    path = (
        f"{host_home}/.local/bin:{host_home}/local/bin:/usr/local/bin:"
        "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    cmd = (
        f"want={version}; "
        f'have=$(env PATH={path} codex --version 2>/dev/null | awk "{{print \\$NF}}" || true); '
        'if [ "$have" != "$want" ]; then '
        'echo "Installing Codex CLI $want in trusted setup"; '
        f"sudo env PATH={path} npm install -g --no-audit --no-fund "
        '"@openai/codex@$want"; '
        "fi; "
        f'have=$(env PATH={path} codex --version 2>/dev/null | awk "{{print \\$NF}}" || true); '
        '[ "$have" = "$want" ] || { '
        'echo "ERROR: codex CLI version ${have:-missing}; expected $want" >&2; exit 1; }; '
        f"sudo -u {SUBUSER} env HOME=/Users/{SUBUSER} PATH={path} codex --version"
    )
    rc, _ = ssh(host, cmd, check=False, stream=True)
    if rc != 0:
        raise RuntimeError(f"failed to install or verify Codex CLI {version}")
    log(f"  Codex CLI {version} installed and verified for {SUBUSER}")


def _download_macos_cua_archive(
    url: str, destination: Path, expected_sha256: str
) -> None:
    """Download one pinned public release asset and verify it before extraction."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("macOS cua-driver archive URL must be HTTPS")
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("macOS cua-driver archive requires a SHA-256 digest")
    request = Request(
        url, headers={"User-Agent": "RecreationBench/cua-driver-bootstrap"}
    )
    digest = hashlib.sha256()
    with urlopen(request, timeout=300) as response, destination.open("wb") as stream:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            stream.write(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"macOS cua-driver archive checksum mismatch: expected {expected}, got {actual}"
        )


def _extract_macos_cua_app(archive_path: Path, destination: Path, version: str) -> Path:
    """Safely extract and validate the single QwenCuaDriver.app in a release archive."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(
                    f"unsupported entry in macOS cua-driver archive: {member.name!r}"
                )
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(
                    f"unsafe path in macOS cua-driver archive: {member.name!r}"
                )
        archive.extractall(root)
    apps = [path for path in root.rglob("QwenCuaDriver.app") if path.is_dir()]
    if len(apps) != 1:
        raise RuntimeError(
            f"macOS cua-driver archive contains {len(apps)} QwenCuaDriver.app bundles"
        )
    app = apps[0]
    executable = app / "Contents" / "MacOS" / "qwen-cua-driver"
    info_path = app / "Contents" / "Info.plist"
    if not executable.is_file() or not info_path.is_file():
        raise RuntimeError("macOS cua-driver release bundle is incomplete")
    with info_path.open("rb") as stream:
        info = plistlib.load(stream)
    if str(info.get("CFBundleShortVersionString") or "") != version:
        raise RuntimeError(
            "macOS cua-driver bundle version does not match RB_CUA_DRIVER_REF"
        )
    return app


def _macos_cua_release(ref: str) -> tuple[str, str, str]:
    source, version, _ = cua_driver.parse_ref(ref)
    if source != "qwen" or not version:
        raise RuntimeError(f"macOS requires a pinned qwen cua-driver ref, got {ref!r}")
    default_url, default_sha256 = cua_driver.macos_release(version)
    url = (os.environ.get("RB_CUA_DRIVER_MACOS_ARCHIVE_URL") or default_url).strip()
    sha256 = (
        os.environ.get("RB_CUA_DRIVER_MACOS_ARCHIVE_SHA256") or default_sha256
    ).strip()
    return version, url, sha256


def deploy_cua_driver(host, pipeline_root):
    """Deploy the pinned qwen-cua-driver app bundle and start its macOS daemon."""
    log("Deploying qwen-cua-driver...")

    deploy_script = os.path.join(
        pipeline_root, "scripts/macos/runtime_assets/deploy_cua_driver.sh"
    )
    coordinate_probe = os.path.join(
        pipeline_root, "scripts/macos/runtime_assets/cua_coordinate_probe.py"
    )
    nsworkspace_probe = os.path.join(
        pipeline_root, "scripts/macos/runtime_assets/nsworkspace_probe.py"
    )
    if not os.path.exists(deploy_script):
        raise FileNotFoundError(f"deploy_cua_driver.sh not found at {deploy_script}")
    if not os.path.exists(coordinate_probe):
        raise FileNotFoundError(
            f"cua_coordinate_probe.py not found at {coordinate_probe}"
        )
    if not os.path.exists(nsworkspace_probe):
        raise FileNotFoundError(
            f"nsworkspace_probe.py not found at {nsworkspace_probe}"
        )

    sftp_upload_file(host, deploy_script, "/tmp/deploy_cua_driver.sh")
    sftp_upload_file(host, coordinate_probe, "/tmp/cua_coordinate_probe.py")
    sftp_upload_file(host, nsworkspace_probe, "/tmp/nsworkspace_probe.py")
    # Carry the pin into the deploy script's env (it is sourced from .cua_pass_env just before
    # the script runs). macOS installs its driver from an app bundle rather than a versioned
    # download, so the script ASSERTS the version instead of installing one — that turns
    # "silently whatever the image shipped" into a loud failure, the same guarantee windows gets
    # from actually installing a pinned version.
    _cua_ref = (os.environ.get("RB_CUA_DRIVER_REF") or "qwen:0.7.3").strip()
    _cua_pin, _cua_url, _cua_sha256 = _macos_cua_release(_cua_ref)
    ssh(host, "chmod +x /tmp/deploy_cua_driver.sh")

    log(f"  cua-driver pin: {_cua_ref}")

    # Fetch the public release by an explicit digest instead of storing a 26 MB executable in
    # Git. Repack the validated app under a canonical top-level name so target-side deployment
    # command is independent of the upstream archive's containing directory.
    del pipeline_root
    log(f"  Downloading public QwenCuaDriver.app release {_cua_pin}...")
    with tempfile.TemporaryDirectory(prefix="rb-cua-driver-") as temporary:
        temporary_path = Path(temporary)
        source_archive = temporary_path / "release.tar.gz"
        _download_macos_cua_archive(_cua_url, source_archive, _cua_sha256)
        cua_app = _extract_macos_cua_app(
            source_archive, temporary_path / "extracted", _cua_pin
        )
        local_tar = temporary_path / "QwenCuaDriver_app.tar.gz"
        subprocess.run(
            [
                "tar",
                "czf",
                str(local_tar),
                "-C",
                str(cua_app.parent),
                "QwenCuaDriver.app",
            ],
            check=True,
        )
        sftp_upload_file(host, str(local_tar), "/tmp/QwenCuaDriver_app.tar.gz")
        ssh(
            host,
            "rm -rf /tmp/QwenCuaDriver.app && tar xzf /tmp/QwenCuaDriver_app.tar.gz -C /tmp",
        )
        remote_env = _upload_private_environment(
            host,
            {
                "MACOS_PASS": MACOS_PASS,
                "DEVAGENT_USER": SUBUSER,
                "CUA_DRIVER_RS_COORDINATE_SPACE": os.environ.get(
                    "CUA_DRIVER_RS_COORDINATE_SPACE", "1"
                ),
                "CUA_DRIVER_RS_COORDINATE_SCALE": os.environ.get(
                    "CUA_DRIVER_RS_COORDINATE_SCALE", "1000"
                ),
                "CUA_DRIVER_RS_MCP_FORCE_PROXY": "1",
                "CUA_DRIVER_RS_UPDATE_CHECK": "0",
                "CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS": "86400",
                "RB_CUA_DRIVER_REF": _cua_ref,
            },
            label="cua",
        )
        try:
            rc, out = ssh(
                host,
                f"env_file={shlex.quote(remote_env)}; "
                "trap 'rm -f \"$env_file\"' EXIT; "
                "source \"$env_file\" && "
                "bash /tmp/deploy_cua_driver.sh --app /tmp/QwenCuaDriver.app",
                check=False,
                stream=True,
            )
        finally:
            ssh(host, f"rm -f {shlex.quote(remote_env)}", check=False)

    if rc != 0:
        raise RuntimeError(f"qwen-cua-driver deployment failed (rc={rc})")
    log("  qwen-cua-driver deployed successfully")


def vm_proxy_base_url() -> str:
    """Return the provider-prepared endpoint reachable from the macOS target."""
    parsed = urlsplit(TARGET_MODEL_BASE_URL)
    if not parsed.scheme or not parsed.hostname:
        raise RuntimeError("RB_TARGET_MODEL_BASE_URL is missing or invalid")
    return TARGET_MODEL_BASE_URL.rstrip("/")


def fix_vm_deps(host, pipeline_root):
    """Install the declared Python dependencies on the macOS target."""
    log("Installing macOS dependencies...")

    fix_script = os.path.join(
        pipeline_root, "scripts/macos/runtime_assets/fix_vm_deps.sh"
    )
    import_probe = os.path.join(
        pipeline_root, "scripts/macos/runtime_assets/import_probe.py"
    )
    requirements_local = os.path.join(
        pipeline_root, "providers/macos/provision/requirements.macos.txt"
    )
    if not os.path.exists(fix_script):
        log("  WARN: fix_vm_deps.sh not found, skipping")
        return
    if not os.path.exists(import_probe):
        raise FileNotFoundError(f"import_probe.py not found at {import_probe}")
    if not os.path.exists(requirements_local):
        raise FileNotFoundError(
            f"macOS requirements not found at {requirements_local}"
        )

    sftp_upload_file(host, fix_script, "/tmp/fix_vm_deps.sh")
    sftp_upload_file(host, import_probe, "/tmp/import_probe.py")
    requirements_remote = "/tmp/rb_requirements_macos.txt"
    sftp_upload_file(host, requirements_local, requirements_remote)
    try:
        ssh(
            host,
            "chmod +x /tmp/fix_vm_deps.sh && "
            f"RB_MACOS_REQUIREMENTS_FILE={shlex.quote(requirements_remote)} "
            f"DEVAGENT_USER={shlex.quote(SUBUSER)} bash /tmp/fix_vm_deps.sh",
            stream=True,
        )
    finally:
        try:
            ssh(host, f"rm -f {shlex.quote(requirements_remote)}", check=False)
        except Exception:
            pass
    log("  macOS dependencies installed")


def build_run_full_cmd():
    """Build the run_full.sh command line.

    SECURITY: ALL parameters (app/repo/branch/commit/model/timeouts/thinking/
    effort and release range) are passed via the .runtime_env file sourced before
    run_full.sh, NOT as CLI args. The run_full.sh process is visible to the
    sandboxed devagent through `ps`, so its argv must carry nothing — no model
    name, no reference repo+commit, no run parameters. run_full.sh reads each
    value from env (see deploy_env_file).
    """
    return (
        f"source {VM_PIPELINE_DIR}/.runtime_env"
        " && "
        f"cd {VM_PIPELINE_DIR}"
        " && "
        "bash scripts/macos/run_full.sh"
    )


def _parsed_vlm_route():
    """Validate the judge URL and return its parsed controller-side route."""
    value = VLM_BASE_URL.strip().rstrip("/")
    if not value:
        return value, None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "ssh+http"}:
        raise ValueError(
            "VLM base URL scheme must be http, https, or ssh+http; "
            f"got {parsed.scheme!r}"
        )
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("VLM base URL must have a host and no embedded credentials")
    if not 1 <= VLM_FORWARD_PORT <= 65535:
        raise ValueError(f"invalid RB_VLM_FORWARD_PORT={VLM_FORWARD_PORT!r}")
    return value, parsed


def _guest_vlm_base_url() -> str:
    """Return the judge URL that the macOS guest can reach.

    ``ssh+http`` makes the routing boundary explicit: the deployment platform controller reaches
    the upstream, while the guest uses a loopback reverse-forward created by the
    long-lived pipeline SSH process. Ordinary HTTP(S) URLs keep direct semantics.
    """
    value, parsed = _parsed_vlm_route()
    if parsed is None or parsed.scheme != "ssh+http":
        return value
    return urlunsplit(
        (
            "http",
            f"127.0.0.1:{VLM_FORWARD_PORT}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    ).rstrip("/")


def _vlm_forward_args() -> list[str]:
    """Build the OpenSSH reverse-forward paired with ``_guest_vlm_base_url``."""
    _value, parsed = _parsed_vlm_route()
    if parsed is None or parsed.scheme != "ssh+http":
        return []
    upstream_port = parsed.port or 80
    upstream_host = parsed.hostname
    if ":" in upstream_host:
        upstream_host = f"[{upstream_host}]"
    spec = f"127.0.0.1:{VLM_FORWARD_PORT}:{upstream_host}:{upstream_port}"
    return ["-o", "ExitOnForwardFailure=yes", "-R", spec]


def deploy_env_file(host):
    """Write agent env without exposing the upstream credential to the target.

    Both supported agents call the deployment-owned model endpoint. The
    deployment keeps the real upstream key; the sandbox receives only the
    endpoint's client token.
    """
    agent_api_key = PROXY_CLIENT_API_KEY
    agent_base_url = vm_proxy_base_url()
    env_lines = [
        _shell_export("ANTHROPIC_API_KEY", agent_api_key),
        _shell_export("ANTHROPIC_BASE_URL", agent_base_url),
        _shell_export("NO_PROXY", "127.0.0.1,localhost"),
        _shell_export("no_proxy", "127.0.0.1,localhost"),
    ]
    if VLM_MODEL:
        env_lines.append(_shell_export("VLM_MODEL", VLM_MODEL))
    if VLM_API_KEY:
        env_lines.append(_shell_export("VLM_API_KEY", VLM_API_KEY))
    guest_vlm_url = _guest_vlm_base_url()
    if guest_vlm_url:
        env_lines.append(_shell_export("VLM_BASE_URL", guest_vlm_url))
    # The deployment platform pod receives the judge throttling knobs from the profile, but eval runs
    # on the macOS target. Carry them across the SSH boundary; otherwise the shared
    # judge silently falls back to 16 rapid retries and can exhaust an RPM-limited key.
    for name in (
        "RB_VLM_JUDGE_RETRIES",
        "RB_VLM_JUDGE_MIN_INTERVAL",
        "RB_VLM_JUDGE_BATCH_SIZE",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            env_lines.append(_shell_export(name, value))
    if MAX_TOKENS_LIMIT:
        env_lines.append(_shell_export("MAX_TOKENS_LIMIT", MAX_TOKENS_LIMIT))
    if CLAUDE_CODE_AUTO_COMPACT_WINDOW:
        env_lines.append(
            _shell_export(
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW", CLAUDE_CODE_AUTO_COMPACT_WINDOW
            )
        )
    env_lines.append(_shell_export("CONTEXT_1M", CONTEXT_1M))
    env_lines.append(_shell_export("RB_CUA_PREFLIGHT_MODE", RB_CUA_PREFLIGHT_MODE))
    env_lines.append(
        _shell_export(
            "RB_CAPTURE_TOOL_USE_SCREENSHOTS",
            os.environ.get("RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"),
        )
    )
    # 0.7.2 cua-driver: pipeline-wide so every mcp client (preflight tests, recreation
    # agent, self-checks) proxies to the running daemon instead of open -a auto-launch;
    # silence update-check; keep the daemon alive through long stages.
    env_lines.append(_shell_export("CUA_DRIVER_RS_MCP_FORCE_PROXY", "1"))
    env_lines.append(_shell_export("CUA_DRIVER_RS_UPDATE_CHECK", "0"))
    env_lines.append(_shell_export("CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS", "86400"))
    # Coordinate space (1=relative/0=absolute) MUST also reach run_full.sh ->
    # recreation.sh (it (re)starts the daemon + writes the devagent .claude.json).
    # Without this the deploy env file only fed deploy_cua_driver.sh, and
    # recreation.sh defaulted to 1 -> ABS runs silently ran normalized. Follow the
    # pipeline param (main.sh exports it into the runtime environment).
    env_lines.append(
        _shell_export(
            "CUA_DRIVER_RS_COORDINATE_SPACE",
            os.environ.get("CUA_DRIVER_RS_COORDINATE_SPACE", "1"),
        )
    )
    env_lines.append(
        _shell_export(
            "CUA_DRIVER_RS_COORDINATE_SCALE",
            os.environ.get("CUA_DRIVER_RS_COORDINATE_SCALE", "1000"),
        )
    )

    # SECURITY: pipeline params passed via env (NOT run_full.sh argv) so the
    # long-lived run_full.sh process — visible to the sandboxed devagent via
    # `ps` — carries no model name or run parameters.
    # run_full.sh reads each of these from env when the CLI flag is absent.
    env_lines.append(_shell_export("APP_NAME", APP_NAME))
    env_lines.append(_shell_export("MODEL", MODEL))
    env_lines.append(_shell_export("CLAUDE_MODEL", CLAUDE_MODEL))
    env_lines.append(_shell_export("STAGE", STAGE))
    env_lines.append(_shell_export("RB_EVAL_TARGET", EVAL_TARGET))
    if STAGE == "eval" and EVAL_TARGET == "recreation":
        env_lines.append(
            _shell_export(
                "RB_RESTORED_RECREATION_ARCHIVE",
                f"{VM_PIPELINE_DIR}/{RESTORED_RECREATION_TGZ}",
            )
        )
    env_lines.append(_shell_export("RB_EVAL_TEST_TIMEOUT", EVAL_TEST_TIMEOUT))
    env_lines.append(_shell_export("RECREATION_TIMEOUT", RECREATION_TIMEOUT))
    api_timeout_ms = int(os.environ.get("RB_API_TIMEOUT_MS", "1800000"))
    env_lines.append(_shell_export("API_TIMEOUT", max(1, api_timeout_ms // 1000)))
    if os.environ.get("RB_MCP_TOOL_TIMEOUT", "").strip():
        env_lines.append(
            _shell_export("RB_MCP_TOOL_TIMEOUT", os.environ["RB_MCP_TOOL_TIMEOUT"])
        )
    if THINKING_EFFORT:
        env_lines.append(_shell_export("EFFORT", THINKING_EFFORT))
    # A release run consumes exactly one complete frozen instance. There is no authoring
    # fallback: if reference/ or tests/ is absent, the run is invalid and stops here.
    if STAGE != "setup":
        deploy_unified_instance(host)

    # codex: the agent choice rides in env like everything else (argv must stay empty -- the
    # sandboxed devagent can ps-read it), and its config.toml is generated HERE because
    # scaffold/run.sh has no access to the controller-side source tree.
    env_lines.append(_shell_export("AGENT_CLI", AGENT_CLI))
    if AGENT_CLI.startswith("codex"):
        if SCAFFOLD_VERSION:
            env_lines.append(_shell_export("SCAFFOLD_VERSION", SCAFFOLD_VERSION))
        # Codex only needs the endpoint's client token. No upstream credential is
        # copied onto the macOS target.
        env_lines.append(_shell_export("OPENAI_API_KEY", PROXY_CLIENT_API_KEY))
        env_lines.append(_shell_export("CODEX_BASE_URL", agent_base_url))
        log(f"  Codex model endpoint: {agent_base_url}")

    env_content = "\n".join(env_lines) + "\n"
    _upload_private_text(host, env_content, f"{VM_PIPELINE_DIR}/.runtime_env")
    log("  Environment file deployed to target")
    if AGENT_CLI.startswith("codex"):
        deploy_codex_config(host)


def deploy_codex_config(host):
    """Write the recreation user's model and desktop-control MCP config.

    Generated pod-side on purpose: scaffold/run.sh cannot import core.agent_config (it runs on
    the target, where no controller tree is mounted), and a second hand-written copy is how
    the android codex path picked up a Claude-Code-only model label.

    The BARE slug is required -- the dot-form makes codex log "Model metadata not found" and
    silently degrade its context window. The base URL is supplied by the deployment
    adapter; the target receives only its client token.
    """
    from core.model_endpoint import from_environment as endpoint_from_environment

    endpoint = endpoint_from_environment(
        os.environ,
        provider="recreationbench",
        name="RecreationBench model endpoint",
        base_url=vm_proxy_base_url(),
        wire_api="responses",
    )
    toml = agent_config.codex_config_toml(
        agent_config.codex_model_slug(MODEL), endpoint
    )
    effort = (os.environ.get("THINKING_EFFORT", "") or "").strip().lower()
    if effort:
        toml = agent_config.insert_top_level(
            toml, 'model_reasoning_effort = "%s"\n' % effort
        )
    cua_socket = (
        f"{MACOS_HOME or f'/Users/{MACOS_USER}'}/Library/Caches/qwen-cua-driver/qwen-cua-driver.sock"
    )
    cua_env = mcp_settings.desktop_env(
        normalize=os.environ.get("CUA_DRIVER_RS_COORDINATE_SPACE", "1") == "1",
        scale=os.environ.get("CUA_DRIVER_RS_COORDINATE_SCALE", "1000"),
        extra={"CUA_DRIVER_RS_MCP_FORCE_PROXY": "1"},
    )
    toml += agent_config.mcp_server_toml(
        mcp_settings.DESKTOP_SERVER,
        "/Applications/QwenCuaDriver.app/Contents/MacOS/qwen-cua-driver",
        ["mcp", "--socket", cua_socket, "--no-overlay"],
        env=cua_env,
        startup_timeout_sec=60,
        tool_timeout_sec=int(
            mcp_settings.tool_timeout_ms(
                os.environ.get(mcp_settings.TOOL_TIMEOUT_OVERRIDE_VAR)
            )
        )
        // 1000,
    )
    remote = "/tmp/.rb_codex_config.toml"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", SUBUSER):
        raise RuntimeError(f"invalid macOS subuser: {SUBUSER!r}")
    agent_home = f"/Users/{SUBUSER}"
    _upload_private_text(host, toml, remote)
    try:
        ssh(
            host,
            f"sudo install -d -m 700 -o {SUBUSER} -g staff {agent_home}/.codex && "
            f"sudo install -m 600 -o {SUBUSER} -g staff {remote} "
            f"{agent_home}/.codex/config.toml",
        )
    finally:
        ssh(host, f"rm -f {remote}", check=False)
    log(
        f"  codex config deployed for {SUBUSER} "
        f"(model={agent_config.codex_model_slug(MODEL)}, via deployment endpoint; "
        "MCP=desktop-control)"
    )


UNIFIED_INSTANCE_TGZ = ".rb_unified_instance.tar.gz"
RESTORED_RECREATION_TGZ = ".rb_restored_recreation.tar.gz"


def deploy_unified_instance(host):
    """Fetch, validate, and push the complete frozen release instance.

    The macOS target has no artifact-store client, so the controller materializes the same
    ``instance.json + reference/ + tests/ + vlm_assertions.json`` input used by the
    other platforms. Missing or malformed inputs are fatal; release runs never author
    replacements on the fly.
    """
    prefix = os.environ.get("RB_UNIFIED_PREFIX", "").strip().rstrip("/")
    if not prefix:
        raise RuntimeError("RB_UNIFIED_PREFIX is required for macOS release runs")
    local_dir = "/tmp/rb_unified_instance"
    shutil.rmtree(local_dir, ignore_errors=True)
    os.makedirs(local_dir, exist_ok=True)
    from rb_unify.eval_bridge import download_unified
    from rb_unify.rb_instance import RBInstance

    platform = os.environ.get("RB_UNIFIED_PLATFORM", "macos").strip() or "macos"
    rep = download_unified(
        prefix,
        platform,
        APP_NAME,
        local_dir,
        components=("reference", "tests"),
        override=os.environ.get("RB_UNIFIED_APP", ""),
        log=lambda m: log(f"  {m}"),
        store=_artifact_store(),
    )
    if not rep.get("prefix"):
        raise FrozenInputError(
            f"no unified instance resolved for {APP_NAME}; tried {rep.get('tried')}"
        )
    try:
        instance = RBInstance.load(local_dir)
        problems = [str(p) for p in instance.validate() if p.component != "vlm"]
    except Exception as exc:
        raise FrozenInputError(f"invalid unified instance: {exc}") from exc
    if problems:
        raise FrozenInputError("invalid unified instance: " + "; ".join(problems))
    if instance.platform != "macos":
        raise FrozenInputError(
            f"unified instance platform is {instance.platform!r}, expected 'macos'"
        )
    if not (instance.tests_dir / "testcase_counts.json").is_file():
        raise FrozenInputError(
            "invalid unified instance: tests/testcase_counts.json missing"
        )

    tgz = "/tmp/" + UNIFIED_INSTANCE_TGZ
    subprocess.run(["tar", "czf", tgz, "-C", local_dir, "."], check=True)
    sftp_upload_file(host, tgz, f"{VM_PIPELINE_DIR}/{UNIFIED_INSTANCE_TGZ}")
    size_mb = os.path.getsize(tgz) / 1024 / 1024
    os.unlink(tgz)
    shutil.rmtree(local_dir, ignore_errors=True)
    log(
        f"  Frozen instance deployed ({size_mb:.1f} MB) <- "
        f"artifact key {rep['prefix']}"
    )


_upload_lock = threading.Lock()


def _intermediate_upload(host, stage_name):
    """Background upload after a stage completes — non-blocking, non-fatal."""
    with _upload_lock:
        try:
            log(f"  [intermediate] Collecting results after {stage_name}...")
            collect_results(host)
            upload_results()
            log(f"  [intermediate] Upload after {stage_name} complete")
        except Exception as e:
            log(f"  [intermediate] Upload after {stage_name} failed: {e}")


def run_pipeline(host):
    """Execute run_full.sh on the macOS target, uploading results after each stage."""
    cmd = build_run_full_cmd()
    log(f"Running pipeline: {cmd[:200]}...")

    total_timeout = SANDBOX_TIMEOUT_SEC - 300  # leave 5 min for result collection
    stage_pattern = re.compile(r"(recreation|eval)\s+(completed in|failed after)")

    env = vmssh.env_with_password(os.environ, MACOS_PASS)
    full_cmd = f"{_ssh_prefix(host, _vlm_forward_args())} {shlex.quote(cmd)}"
    proc = subprocess.Popen(
        full_cmd,
        shell=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        log(f"Pipeline timeout: {total_timeout}s exceeded, killing SSH process group")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    timer = threading.Timer(total_timeout, _kill_on_timeout)
    timer.daemon = True
    timer.start()

    output_lines = []
    upload_thread = None
    last_completed_stage = None
    try:
        for line in proc.stdout:
            try:
                print(line, end="", flush=True)
            except (BrokenPipeError, OSError):
                pass
            output_lines.append(line)
            m = stage_pattern.search(line)
            if m:
                stage_name = m.group(1)
                if "completed in" in line:
                    last_completed_stage = stage_name
                upload_thread = threading.Thread(
                    target=_intermediate_upload,
                    args=(host, stage_name),
                    daemon=True,
                )
                upload_thread.start()
        proc.wait()
    finally:
        timer.cancel()

    if upload_thread is not None:
        upload_thread.join(timeout=300)

    rc = proc.returncode
    if timed_out.is_set():
        if last_completed_stage == "eval":
            log(
                f"Timeout recovery: {last_completed_stage} completed — treating as success"
            )
            rc = 0
        else:
            log(f"Pipeline timed out with last stage: {last_completed_stage}")
            rc = exit_contract.RC_TIMEOUT
    elif rc == 141 and last_completed_stage == "eval":
        log(
            f"SIGPIPE recovery: exit 141 but {last_completed_stage} completed — treating as success"
        )
        rc = 0

    log(f"Pipeline finished with exit code {rc}")
    if rc != 0:
        # The pod-side proxy log contains the upstream cause; surface it beside
        # the runtime failure when available.
        try:
            from infrastructure.model_gateway import diagnostics

            diag = diagnostics.diagnose_dir(OUTPUT_DIR)
            if diag:
                log(diag)
        except Exception as exc:  # noqa: BLE001 - never mask the real failure
            log(f"  (proxy diagnosis unavailable: {type(exc).__name__}: {exc})")
    return rc


def collect_results(host):
    """Collect pipeline results from the macOS target to ``OUTPUT_DIR``."""
    log("Collecting results from macOS target...")

    results_dir = os.path.join(OUTPUT_DIR, "results")
    os.makedirs(results_dir, exist_ok=True)

    # Check if results exist on the target.
    rc, out = ssh(
        host, f"sudo ls {VM_RESULTS_DIR}/ 2>/dev/null || echo EMPTY", check=False
    )
    if "EMPTY" in out or rc != 0:
        log("  WARN: No results directory found on target")
        # Try to find results at alternative paths
        rc, out = ssh(
            host,
            "find /var/tmp -name '*_result.env' -o -name '*.jsonl' 2>/dev/null | head -20",
            check=False,
        )
        log(f"  Alternative artifacts: {out[:300]}")
        return

    # Results are root:wheel/700 to keep them outside the devagent boundary. The controller logs
    # in as MACOS_USER, so hand ownership to that trusted account while keeping group/other closed,
    # then transfer the tree directly. No remote tar or local extraction is needed.
    rc, out = ssh(
        host,
        f"sudo chown -R {MACOS_USER}:staff {VM_RESULTS_DIR} && "
        f"sudo chmod -R u+rwX,go-rwx {VM_RESULTS_DIR}",
        check=False,
    )
    if rc != 0:
        log(f"  WARN: Could not make results readable to SFTP: {out[:300]}")
    else:
        downloaded = sftp_download_dir(host, VM_RESULTS_DIR, results_dir)
        if downloaded:
            log(f"  Downloaded {downloaded} result files through SFTP")
        else:
            log("  WARN: SFTP returned no result files")

    # Expose the same trajectory.jsonl + sessions/ paths as every other deployment platform artifact. macOS stores
    # the stream copy inside sessions, so surface_stage also accepts recreation_stream.jsonl.
    recreation_result = Path(results_dir) / APP_NAME / MODEL
    if recreation_result.is_dir():
        surfaced = trajectory.surface_stage(
            recreation_result, OUTPUT_DIR, stage="recreation"
        )
        log(f"  Surfaced {len(surfaced)} canonical trajectory file(s)")

    # Also grab stage result env files (small, useful for quick status)
    # Resolve $HOME because SFTP does not expand shell variables in remote paths.
    rc, resolved_work_dir = ssh(
        host, f"echo $HOME/pipeline_work/{APP_NAME}", check=False
    )
    if rc == 0 and resolved_work_dir:
        for stage_num in range(1, 7):
            stage_file = f"stage{stage_num}_result.env"
            rc, _ = ssh(host, f"test -f {resolved_work_dir}/{stage_file}", check=False)
            if rc == 0:
                sftp_download_file(
                    host,
                    f"{resolved_work_dir}/{stage_file}",
                    os.path.join(OUTPUT_DIR, "logs", stage_file),
                )

    # Last-resort stream copy if the full-tree transfer failed. Use this run's exact path; a broad
    # `find ... | head -1` can select an older app/model left on a reused target.
    surfaced_trajectory = os.path.join(OUTPUT_DIR, "trajectory.jsonl")
    if not os.path.isfile(surfaced_trajectory):
        trajectory_path = (
            f"{VM_RESULTS_DIR}/{APP_NAME}/{MODEL}/sessions/recreation_stream.jsonl"
        )
        rc, _ = ssh(host, f"test -f {shlex.quote(trajectory_path)}", check=False)
        if rc == 0:
            log(f"  Trajectory: {trajectory_path}")
            sftp_download_file(host, trajectory_path, surfaced_trajectory)


# ── Artifact helpers ────────────────────────────────────────────────────


def _artifact_store():
    """Create the configured transport at the macOS runtime boundary."""
    return artifact_store_from_environment(
        os.environ,
        log=lambda message: log(f"  {message}"),
        backend_options={
            "multipart_threshold_bytes": 100 * 1024 * 1024,
            "multipart_part_size": 50 * 1024 * 1024,
            "multipart_threads": 4,
        },
    )


def _artifact_run_prefix():
    """Build the artifact key prefix for this run's results."""
    parts = [RB_ARTIFACT_PREFIX.rstrip("/")]
    run_id = f"{TASK_ID}_{MODEL}" if MODEL else TASK_ID
    parts.extend([APP_NAME, run_id])
    return "/".join(parts)


def _normalized_restore_prefix() -> str:
    """Return the object-key portion of the explicit eval-only source prefix."""
    raw = RB_ARTIFACT_RESTORE_PREFIX.strip()
    if not raw:
        raise CandidateInputError(
            "RB_ARTIFACT_RESTORE_PREFIX is required for a fresh macOS eval-only run"
        )
    try:
        value = normalize_artifact_key(raw, label="artifact restore prefix")
    except ValueError as exc:
        raise CandidateInputError(str(exc)) from exc
    if value == RB_ARTIFACT_PREFIX.strip().rstrip("/"):
        raise CandidateInputError(
            "eval-only restore and output prefixes must differ to avoid source contamination"
        )
    return value


def _safe_extract_tar(archive: str | Path, destination: str | Path) -> None:
    """Extract a harness archive without permitting path/link/device escapes."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if sum(max(0, member.size) for member in members) > 20 * 1024**3:
                raise CandidateInputError(
                    "restored result archive expands beyond 20 GiB"
                )
            # Python 3.11 in the controller image provides the backported data filter.
            # It rejects absolute/traversing paths, escaping links and device files.
            bundle.extractall(destination, members=members, filter="data")
    except CandidateInputError:
        raise
    except (OSError, tarfile.TarError, tarfile.FilterError) as exc:
        raise CandidateInputError(
            f"unsafe or invalid restored tar archive: {exc}"
        ) from exc


def _copy_restored_recreation_evidence(source: Path, destination: Path) -> None:
    """Carry recreation provenance forward without stale eval evidence.

    An eval-only retry must retain the original model trajectory and candidate source,
    but old programmatic/VLM files must never be able to satisfy the new run's evidence
    checks. Copy only recreation-owned files; the fresh target fills every eval-owned path.
    """
    destination.mkdir(parents=True, exist_ok=True)
    sessions = source / "sessions"
    if sessions.is_dir():
        shutil.copytree(sessions, destination / "sessions", dirs_exist_ok=True)
    logs = source / "logs"
    if logs.is_dir():
        target_logs = destination / "logs"
        target_logs.mkdir(parents=True, exist_ok=True)
        for path in logs.iterdir():
            if path.is_file() and (
                path.name.startswith("recreation")
                or path.name.startswith("permission")
                or path.name == "stderr.log"
            ):
                shutil.copy2(path, target_logs / path.name)
    permission = source / "permission_report.json"
    if permission.is_file():
        shutil.copy2(permission, destination / permission.name)


def restore_eval_candidate(host: str) -> str:
    """Restore one prior macOS candidate into a fresh eval-only target.

    The source is an explicit, immutable run prefix. The current output prefix is
    reserved for the new eval output, so a recovery run cannot overwrite or mix the
    original attempt.  Only recreation provenance is retained locally; stale eval
    metrics from the source archive are intentionally discarded.
    """
    source_prefix = _normalized_restore_prefix()
    run_id = f"{TASK_ID}_{MODEL}" if MODEL else TASK_ID
    source_key = "/".join([source_prefix, APP_NAME, run_id, "results.tar.gz"])
    with tempfile.TemporaryDirectory(prefix="rb-macos-eval-restore-") as temporary:
        temporary = Path(temporary)
        results_archive = temporary / "results.tar.gz"
        try:
            store = _artifact_store()
            if not store.exists(source_key):
                raise CandidateInputError(
                    f"candidate results archive not found: {source_key}"
                )
            store.get_file(source_key, results_archive)
        except CandidateInputError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"candidate recreation artifact download failed: {exc}"
            ) from exc

        extracted = temporary / "results"
        _safe_extract_tar(results_archive, extracted)
        prior_result = extracted / APP_NAME / MODEL
        source_archive = prior_result / "source" / "recreation.tar.gz"
        if not source_archive.is_file() or source_archive.stat().st_size == 0:
            raise CandidateInputError(
                "candidate results archive lacks source/recreation.tar.gz"
            )

        # Sanitize the nested, agent-authored archive before it crosses into the
        # trusted macOS target. Repacking also makes remote extraction independent of
        # tar implementations' historical path traversal behavior.
        restored_source = temporary / "recreation"
        _safe_extract_tar(source_archive, restored_source)
        has_build = (restored_source / "build.sh").is_file()
        has_launch = (restored_source / "launch.sh").is_file() or (
            restored_source / "src" / "launch.sh"
        ).is_file()
        if not (has_build or has_launch):
            raise CandidateInputError(
                "restored candidate contains neither build.sh nor launch.sh"
            )
        sanitized_archive = temporary / RESTORED_RECREATION_TGZ
        with tarfile.open(sanitized_archive, "w:gz") as bundle:
            for child in sorted(restored_source.iterdir(), key=lambda path: path.name):
                bundle.add(child, arcname=child.name, recursive=True)

        target_result = Path(OUTPUT_DIR) / "results" / APP_NAME / MODEL
        _copy_restored_recreation_evidence(prior_result, target_result)
        target_source = target_result / "source"
        target_source.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sanitized_archive, target_source / "recreation.tar.gz")
        sftp_upload_file(
            host,
            str(sanitized_archive),
            f"{VM_PIPELINE_DIR}/{RESTORED_RECREATION_TGZ}",
        )

    log(
        f"  Restored eval candidate from artifact key {source_key} "
        f"into {RESTORED_RECREATION_TGZ}"
    )
    return source_key


def upload_results():
    """Upload pipeline results through the configured artifact store."""
    if not RB_ARTIFACT_PREFIX:
        log("WARN: artifact prefix not set, skipping results upload")
        return

    prefix = _artifact_run_prefix()
    log(f"Uploading results under artifact key {prefix}/")

    try:
        store = _artifact_store()
    except Exception as exc:
        log(f"WARN: artifact backend unavailable, skipping results upload: {exc}")
        return

    # Upload summary.json individually for quick status checks
    summary_path = os.path.join(OUTPUT_DIR, "summary.json")
    if os.path.exists(summary_path):
        store.put_file(f"{prefix}/summary.json", summary_path)
        log("  Uploaded summary.json")

    # core/pipeline.py writes metrics.json one level above results/.
    metrics_path = os.path.join(OUTPUT_DIR, "metrics.json")
    if os.path.exists(metrics_path):
        store.put_file(f"{prefix}/metrics.json", metrics_path)
        log("  Uploaded metrics.json")

    # The store keeps the complete result tree as one object. Compression happens here, after the
    # Paramiko directory transfer; this path has no tar/extract round trip.
    results_dir = os.path.join(OUTPUT_DIR, "results")
    if os.path.isdir(results_dir):
        results_tar = "/tmp/artifact_results_upload.tar.gz"
        subprocess.run(
            ["tar", "czf", results_tar, "-C", results_dir, "."],
            check=True,
            timeout=300,
        )
        size_mb = os.path.getsize(results_tar) / 1024 / 1024
        store.put_file(f"{prefix}/results.tar.gz", results_tar)
        os.unlink(results_tar)
        log(f"  Uploaded results.tar.gz ({size_mb:.1f} MB)")

    # Upload stage env files from logs/
    logs_dir = os.path.join(OUTPUT_DIR, "logs")
    if os.path.isdir(logs_dir):
        for f in os.listdir(logs_dir):
            if f.endswith("_result.env"):
                store.put_file(f"{prefix}/logs/{f}", os.path.join(logs_dir, f))
        log("  Uploaded stage result env files")

    # Upload trajectory if present
    traj = os.path.join(OUTPUT_DIR, "trajectory.jsonl")
    if os.path.exists(traj):
        store.put_file(f"{prefix}/trajectory.jsonl", traj)
        log("  Uploaded trajectory.jsonl")

    # Keep sessions directly addressable for analysis. results.tar.gz remains the efficient,
    # atomic transfer of the complete macOS result tree; this exploded copy avoids forcing every
    # trajectory consumer to download and unpack the whole archive.
    sessions_dir = os.path.join(OUTPUT_DIR, "sessions")
    if os.path.isdir(sessions_dir):
        uploaded = 0
        for root, _, files in os.walk(sessions_dir):
            for name in files:
                local_path = os.path.join(root, name)
                rel = os.path.relpath(local_path, sessions_dir).replace(os.sep, "/")
                store.put_file(f"{prefix}/sessions/{rel}", local_path)
                uploaded += 1
        log(f"  Uploaded sessions/ ({uploaded} file(s))")

    # Collect optional gateway logs from SHARED_DIR into OUTPUT_DIR before teardown.
    proxy_logs_dir = os.path.join(OUTPUT_DIR, "proxy_logs")
    shared_logs = os.path.join(SHARED_DIR, "logs")
    if os.path.isdir(shared_logs):
        try:
            os.makedirs(proxy_logs_dir, exist_ok=True)
            for name in os.listdir(shared_logs):
                src = os.path.join(shared_logs, name)
                if os.path.isdir(src):
                    dst = os.path.join(proxy_logs_dir, name)
                    shutil.copytree(src, dst, dirs_exist_ok=True)
            log(f"  Collected proxy logs from {shared_logs}")
        except Exception as e:
            log(f"  WARN: collecting proxy logs from SHARED_DIR failed: {e}")

    # Upload model-proxy logs (raw API request/response data for debugging).
    if os.path.isdir(proxy_logs_dir) and os.listdir(proxy_logs_dir):
        proxy_tar = "/tmp/proxy_logs_upload.tar.gz"
        try:
            subprocess.run(
                ["tar", "czf", proxy_tar, "-C", proxy_logs_dir, "."],
                check=True,
                timeout=120,
            )
            size_mb = os.path.getsize(proxy_tar) / 1024 / 1024
            store.put_file(f"{prefix}/proxy_logs.tar.gz", proxy_tar)
            log(f"  Uploaded proxy_logs.tar.gz ({size_mb:.1f} MB)")
        except Exception as e:
            log(f"  WARN: proxy_logs upload failed: {e}")
        finally:
            if os.path.exists(proxy_tar):
                os.unlink(proxy_tar)

    log(f"  Results upload complete under artifact key {prefix}/")


def _read_json_dict(path: Path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def collected_stage_evidence(output_dir: str, stage: str) -> tuple[dict, dict]:
    """Return per-stage status backed by files collected from this target run."""
    root = Path(output_dir)
    selected = stages_for(stage)
    evidence: dict[str, object] = {}

    if "setup" in selected:
        report_path = root / "permission_report.json"
        report = _read_json_dict(report_path)
        evidence["setup"] = {
            "path": str(report_path),
            "valid": bool(report and report.get("passed") is True),
        }

    if "recreation" in selected:
        candidates = sorted(root.glob("results/**/logs/recreation_result.env"))
        evidence["recreation"] = {
            "path": str(candidates[0]) if candidates else "",
            "valid": bool(candidates and candidates[0].is_file()),
        }

    if "eval" in selected:
        pair = None
        for programmatic in sorted(root.glob("results/**/programmatic_results.json")):
            vlm = programmatic.with_name("vlm_results.json")
            if (
                _read_json_dict(programmatic) is not None
                and _read_json_dict(vlm) is not None
            ):
                pair = (programmatic, vlm)
                break
        evidence["eval"] = {
            "programmatic": str(pair[0]) if pair else "",
            "vlm": str(pair[1]) if pair else "",
            "valid": pair is not None,
        }

    return (
        {
            name: "pass" if evidence.get(name, {}).get("valid") else "fail"
            for name in selected
        },
        evidence,
    )


def _collected_stage_outcomes(pipeline_rc: int, evidence: dict) -> dict[str, dict]:
    """Load the target runner's outcomes and fail closed on missing evidence."""
    selected = stages_for(STAGE)
    paths = sorted(Path(OUTPUT_DIR).glob("results/**/stage_outcomes.json"))
    raw = _read_json_dict(paths[0]) if paths else None
    raw = raw or {}
    outcomes: dict[str, dict] = {}
    for index, name in enumerate(selected):
        try:
            item = exit_contract.validate_stage_outcome(raw[name])
        except (KeyError, ValueError):
            if evidence[name]["valid"]:
                item = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_PASS,
                    outcome_class=exit_contract.OUTCOME_COMPLETED,
                    reason_code="completed",
                )
            elif outcomes and any(
                outcome["status"] != exit_contract.STAGE_PASS
                for outcome in outcomes.values()
            ):
                upstream = next(
                    outcome
                    for outcome in outcomes.values()
                    if outcome["status"] != exit_contract.STAGE_PASS
                )
                item = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_NOT_RUN,
                    outcome_class=upstream["outcome_class"],
                    reason_code="upstream_stage_failed",
                )
            elif (
                exit_contract.from_native_exit(pipeline_rc)
                == exit_contract.OUTCOME_TERMINATED
            ):
                item = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_TIMEOUT,
                    outcome_class=exit_contract.OUTCOME_TERMINATED,
                    reason_code="external_termination",
                    native_exit_code=pipeline_rc,
                )
            else:
                # A public rc=1 is only a data error when the target emitted the
                # structured evidence that proves it. Missing/invalid evidence
                # is itself a runner protocol failure and therefore infra.
                item = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code="stage_outcome_missing",
                    native_exit_code=pipeline_rc if index == 0 else None,
                )
        if item["status"] == exit_contract.STAGE_PASS and not evidence[name]["valid"]:
            item = exit_contract.stage_outcome(
                status=exit_contract.STAGE_ERROR,
                outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                reason_code="collected_stage_evidence_missing",
                native_exit_code=item["native_exit_code"],
            )
        outcomes[name] = item
    return outcomes


def write_summary(pipeline_rc, forced_failure=None):
    """Write a fail-closed target summary and return its effective public rc."""
    runner_exit_code = pipeline_rc
    _legacy_stages, evidence = collected_stage_evidence(OUTPUT_DIR, STAGE)
    selected = stages_for(STAGE)
    if forced_failure is None:
        stage_outcomes = _collected_stage_outcomes(pipeline_rc, evidence)
    else:
        first_stage = selected[0]
        stage_outcomes = {first_stage: forced_failure}
        for name in selected[1:]:
            stage_outcomes[name] = exit_contract.stage_outcome(
                status=exit_contract.STAGE_NOT_RUN,
                outcome_class=forced_failure["outcome_class"],
                reason_code="upstream_stage_failed",
            )
    aggregate = exit_contract.aggregate_stage_outcomes(stage_outcomes, selected)
    native_outcome = exit_contract.from_native_exit(runner_exit_code)
    expected_public_rc = exit_contract.process_exit_code(native_outcome)
    if aggregate["process_exit_code"] != expected_public_rc:
        terminal = selected[-1]
        if native_outcome == exit_contract.OUTCOME_TERMINATED:
            stage_outcomes[terminal] = exit_contract.stage_outcome(
                status=exit_contract.STAGE_TIMEOUT,
                outcome_class=exit_contract.OUTCOME_TERMINATED,
                reason_code="external_termination",
                native_exit_code=runner_exit_code,
            )
        else:
            stage_outcomes[terminal] = exit_contract.stage_outcome(
                status=exit_contract.STAGE_ERROR,
                outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                reason_code="runner_outcome_mismatch",
                native_exit_code=runner_exit_code,
            )
        aggregate = exit_contract.aggregate_stage_outcomes(stage_outcomes, selected)
    pipeline_rc = aggregate["process_exit_code"]
    native_exit_code = exit_contract.selected_native_exit_code(stage_outcomes, selected)
    if native_exit_code is None:
        native_exit_code = runner_exit_code
    stages = {
        name: (
            exit_contract.STAGE_PASS
            if outcome["status"] == exit_contract.STAGE_PASS
            else (
                exit_contract.STAGE_NOT_RUN
                if outcome["status"] == exit_contract.STAGE_NOT_RUN
                else exit_contract.STAGE_FAIL
            )
        )
        for name, outcome in stage_outcomes.items()
    }
    summary = {
        "task_id": TASK_ID,
        "app_name": APP_NAME,
        "model": MODEL,
        "stage": STAGE,
        "pipeline_exit_code": pipeline_rc,
        "native_exit_code": native_exit_code,
        "status": "completed" if pipeline_rc == 0 else "failed",
        "stages": stages,
        "stage_outcomes": stage_outcomes,
        **aggregate,
        "stage_evidence": evidence,
    }
    if RB_ARTIFACT_PREFIX:
        summary["artifact_prefix"] = f"{_artifact_run_prefix()}/"
    summary_path = os.path.join(OUTPUT_DIR, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    log(f"Summary written to {summary_path}")
    return pipeline_rc


def required_runtime_values(stage: str = STAGE) -> list[tuple[str, str]]:
    """Return the model-side values needed by the selected worker stage."""
    required = [("MODEL", MODEL)]
    if "recreation" in stages_for(stage):
        required.append(("RB_TARGET_MODEL_BASE_URL", TARGET_MODEL_BASE_URL))
    if stage == "eval" and EVAL_TARGET == "recreation":
        required.append(("RB_ARTIFACT_RESTORE_PREFIX", RB_ARTIFACT_RESTORE_PREFIX))
    return required


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RecreationBench macOS target runtime")
    parser.add_argument(
        "--pipeline-root", required=True, help="Path to pipeline source directory"
    )
    parser.add_argument("--host", required=True, help="Prepared macOS SSH target")
    args = parser.parse_args(argv)

    if not MACOS_USER:
        parser.error("MACOS_USER is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", MACOS_USER):
        parser.error(f"invalid MACOS_USER: {MACOS_USER!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", SUBUSER):
        parser.error(f"invalid SUBUSER: {SUBUSER!r}")
    if MACOS_KEY_PATH and not os.path.isfile(MACOS_KEY_PATH):
        parser.error(f"MACOS_KEY_PATH does not exist: {MACOS_KEY_PATH}")

    pipeline_root = args.pipeline_root
    if not os.path.isdir(pipeline_root):
        log(f"ERROR: pipeline root not found: {pipeline_root}")
        return 2
    if not os.path.exists(os.path.join(pipeline_root, "scripts/macos/run_full.sh")):
        log(f"ERROR: scripts/macos/run_full.sh not found in {pipeline_root}")
        return 2

    # deployment platform normally provides a fresh output mount, but retries and local runtime
    # invocations may reuse one. Never let artifacts from an older target satisfy
    # this run's release evidence checks.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    shutil.rmtree(os.path.join(OUTPUT_DIR, "results"), ignore_errors=True)
    for stale_name in ("summary.json", "permission_report.json"):
        try:
            os.unlink(os.path.join(OUTPUT_DIR, stale_name))
        except FileNotFoundError:
            pass

    log("=" * 50)
    log(f"Task:      {TASK_ID}")
    log(f"App:       {APP_NAME}")
    log(f"Model:     {MODEL}")
    log(f"Target:    {MACOS_USER}@{args.host}:{MACOS_PORT}")
    log(f"Base URL:  {TARGET_MODEL_BASE_URL or '<not needed>'}")
    log(f"Stage:     {STAGE}")
    log(f"Timeout:   {SANDBOX_TIMEOUT_SEC}s")
    log("=" * 50)

    # Setup launches no model client and therefore does not require a proxy tunnel.
    for name, val in required_runtime_values():
        if not val:
            log(f"ERROR: {name} is required")
            sys.exit(2)

    pipeline_rc = exit_contract.RC_DATA
    forced_failure = None

    host = args.host
    try:
        try:
            # The caller has already prepared the target and endpoint route. RB starts
            # at benchmark deployment.
            resolve_vm_pipeline_dir(host)

            deploy_pipeline(host, pipeline_root)

            # Trusted benchmark setup inside the prepared target.
            prepare_sandbox(host)

            # 4b. Install the selected agent while package networking is still available.
            # prepare_recreation.sh deliberately blocks npm and external network access.
            install_codex_cli(host)

            # 5. Deploy CUA Driver
            deploy_cua_driver(host, pipeline_root)

            # 6. Fix missing deps
            fix_vm_deps(host, pipeline_root)

            deploy_env_file(host)

            if STAGE == "eval" and EVAL_TARGET == "recreation":
                restore_eval_candidate(host)

            # The parent CLI selected this platform and stage range. This worker
            # only drives the already prepared target.
            if STAGE == "setup":
                pipeline_rc = 0 if run_setup_permission_probe(host) else 2
            else:
                pipeline_rc = run_pipeline(host)

            # 11. Collect results
            collect_results(host)

        except CandidateInputError as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            pipeline_rc = exit_contract.RC_DATA
            forced_failure = exit_contract.stage_outcome(
                status=exit_contract.STAGE_FAIL,
                outcome_class=exit_contract.OUTCOME_DATA_ERROR,
                reason_code="candidate_recreation_missing",
                native_exit_code=exit_contract.RC_DATA,
            )
            try:
                collect_results(host)
            except Exception:
                log("  Could not collect partial results")
        except FrozenInputError as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            pipeline_rc = exit_contract.RC_DATA
            forced_failure = exit_contract.stage_outcome(
                status=exit_contract.STAGE_FAIL,
                outcome_class=exit_contract.OUTCOME_DATA_ERROR,
                reason_code="invalid_or_missing_frozen_input",
                native_exit_code=exit_contract.RC_DATA,
            )
            try:
                collect_results(host)
            except Exception:
                log("  Could not collect partial results")
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            pipeline_rc = exit_contract.RC_INFRA
            # Try to collect partial results
            try:
                collect_results(host)
            except Exception:
                log("  Could not collect partial results")
    finally:
        if VM_PIPELINE_DIR:
            cleanup_paths = (
                f"{VM_PIPELINE_DIR}/.runtime_env",
                f"{VM_PIPELINE_DIR}/{UNIFIED_INSTANCE_TGZ}",
                f"{VM_PIPELINE_DIR}/{RESTORED_RECREATION_TGZ}",
                "/tmp/.rb_codex_config.toml",
            )
            try:
                ssh(
                    host,
                    "rm -f " + " ".join(shlex.quote(path) for path in cleanup_paths),
                    check=False,
                )
            except Exception as exc:
                log(f"  WARN: could not remove remote runtime secrets: {exc}")
        close_transfer_client()

    pipeline_rc = write_summary(pipeline_rc, forced_failure=forced_failure)

    log(f"macOS target runtime finished (pipeline exit code: {pipeline_rc})")
    return pipeline_rc


if __name__ == "__main__":
    sys.exit(main())
