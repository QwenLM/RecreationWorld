#!/usr/bin/env python3
"""Run one RecreationBench macOS task on a prepared macOS environment over SSH."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko
from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = SERVICE_ROOT.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from common.runtime_source import add_runtime_source, safe_extract_tar, validate_runtime_source_dir  # noqa: E402
from common.unified_cache import prepare_unified_archive  # noqa: E402
from common.controller_helper import render_codex_config  # noqa: E402


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def shell_env(values: dict[str, str]) -> str:
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())


class MacRemote:
    def __init__(self, host: str, port: int, username: str, password: str, key_file: str) -> None:
        self.host, self.port, self.username = host, port, username
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            key_filename=key_file or None,
            timeout=30,
            banner_timeout=60,
            auth_timeout=60,
            look_for_keys=not bool(password),
            allow_agent=not bool(password),
        )
        # Keep the control channel alive across the multi-hour, silent recreation phase.
        # The whole pipeline runs as ONE exec_command and run() blocks on
        # exit_status_ready(); if a stateful hop (NAT/LB/firewall) silently drops the idle
        # flow, paramiko never sees EOF or the exit status and the loop spins until
        # RB_TOTAL_TIMEOUT even though the remote run has already finished (recreation + eval passed,
        # results on disk). OpenSSH's ServerAlive is a client-config option paramiko does NOT
        # honor, so set keepalive on the transport instead.
        _transport = self.client.get_transport()
        if _transport is not None:
            _transport.set_keepalive(30)

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, *, timeout: int = 600, check: bool = True, stream: bool = False) -> tuple[int, str]:
        rendered = "bash -lc " + shlex.quote(command)
        _, stdout, stderr = self.client.exec_command(rendered, timeout=30)
        channel = stdout.channel
        chunks: list[str] = []
        started = time.monotonic()
        while not channel.exit_status_ready():
            if time.monotonic() - started > timeout:
                channel.close()
                raise TimeoutError(f"remote command timed out after {timeout}s")
            if channel.recv_ready():
                data = channel.recv(65536).decode("utf-8", errors="replace")
                chunks.append(data)
                if stream:
                    print(data, end="", flush=True)
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(65536).decode("utf-8", errors="replace")
                chunks.append(data)
                if stream:
                    print(data, end="", file=sys.stderr, flush=True)
            time.sleep(0.1)
        while channel.recv_ready():
            data = channel.recv(65536).decode("utf-8", errors="replace")
            chunks.append(data)
            if stream:
                print(data, end="", flush=True)
        while channel.recv_stderr_ready():
            data = channel.recv_stderr(65536).decode("utf-8", errors="replace")
            chunks.append(data)
            if stream:
                print(data, end="", file=sys.stderr, flush=True)
        rc = channel.recv_exit_status()
        output = "".join(chunks)
        if check and rc:
            raise RuntimeError(f"remote command failed (rc={rc}): {output[-1200:]}")
        return rc, output

    def put(self, local: Path, remote: str, *, mode: int | None = None) -> None:
        with self.client.open_sftp() as sftp:
            if mode is not None:
                # Set the restrictive mode on an empty file before copying any
                # sensitive bytes.  Uploading first creates a brief disclosure
                # window under the SFTP server's default umask.
                with sftp.open(remote, "w"):
                    pass
                sftp.chmod(remote, mode)
            sftp.put(str(local), remote)
            if mode is not None:
                sftp.chmod(remote, mode)

    def get(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        with self.client.open_sftp() as sftp:
            sftp.get(remote, str(local))


def make_runtime_archive(source_dir: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        add_runtime_source(archive, source_dir)


def flatten_unified_archive(source: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rb_macos_unified_") as temp:
        root = Path(temp)
        safe_extract_tar(source, root, strip_components=2)
        if not (root / "instance.json").is_file():
            raise RuntimeError(f"unified archive has no macOS instance at its expected root: {source}")
        with tarfile.open(destination, "w:gz") as tar:
            for child in sorted(root.iterdir()):
                tar.add(child, arcname=child.name)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def write_metrics(result_dir: Path, args: argparse.Namespace, rc: int, sandbox_info: dict) -> Path:
    scripts = args.source_dir / "scripts"
    sys.path.insert(0, str(scripts))
    from core import metrics_contract
    from core.pipeline import stages_for

    selected = stages_for(args.stage)
    result_root = result_dir / "results"
    programmatic = next(iter(sorted(result_root.glob("**/programmatic_results.json"))), None)
    vlm = programmatic.with_name("vlm_results.json") if programmatic else None
    recreation = next(iter(sorted(result_root.glob("**/logs/recreation_result.env"))), None)
    stages: dict[str, str] = {}
    if "recreation" in selected:
        stages["recreation"] = "pass" if recreation and recreation.is_file() else "fail"
    if "eval" in selected:
        stages["eval"] = "pass" if programmatic and vlm and read_json(programmatic) is not None and read_json(vlm) is not None else "fail"
    if rc and selected:
        stages[selected[-1]] = "fail"
    evals = {}
    if programmatic and vlm:
        prog, judge = read_json(programmatic), read_json(vlm)
        if prog is not None and judge is not None:
            evals["eval"] = metrics_contract.eval_scores(programmatic=prog, vlm=judge)
    summary = {
        "task_id": args.task_id,
        "model": args.model,
        "stage": args.stage,
        "pipeline_exit_code": rc,
        "stages": stages,
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    state = {"task_id": args.task_id, "model": args.model, "pipeline_exit": rc, "stages": stages, "evals": evals, "summary": summary}
    metrics = metrics_contract.assemble_metrics(
        state,
        platform="macos",
        required_stages=list(selected),
        stage=args.stage,
        sandbox_info=sandbox_info,
    )
    path = result_dir / "metrics.json"
    path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def deploy_codex_config(remote: MacRemote, args: argparse.Namespace, remote_root: str, remote_home: str, env_file: str) -> None:
    sys.path.insert(0, str(args.source_dir / "scripts"))
    from core import agent_config, mcp_settings

    toml = render_codex_config(
        model=args.model,
        base_url=args.model_base_url,
        platform="macos",
        environ=os.environ,
    )
    toml += agent_config.mcp_server_toml(
        mcp_settings.DESKTOP_SERVER,
        "/Applications/QwenCuaDriver.app/Contents/MacOS/qwen-cua-driver",
        ["mcp", "--socket", f"{remote_home}/Library/Caches/qwen-cua-driver/qwen-cua-driver.sock", "--no-overlay"],
        env=mcp_settings.desktop_env(normalize=True, scale="1000", extra={"CUA_DRIVER_RS_MCP_FORCE_PROXY": "1"}),
        startup_timeout_sec=60,
        tool_timeout_sec=int(mcp_settings.DEFAULT_TOOL_TIMEOUT_MS) // 1000,
    )
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as file:
        local = Path(file.name)
        file.write(toml)
    try:
        remote.put(local, "/tmp/rb_codex_config.toml", mode=0o600)
        cmd = (
            f"source {shlex.quote(env_file)} && printf '%s\\n' \"$MACOS_PASSWORD\" | sudo -S -v && "
            f"sudo install -d -m 700 -o {shlex.quote(args.devagent_user)} -g staff /Users/{shlex.quote(args.devagent_user)}/.codex && "
            f"sudo install -m 600 -o {shlex.quote(args.devagent_user)} -g staff /tmp/rb_codex_config.toml /Users/{shlex.quote(args.devagent_user)}/.codex/config.toml"
        )
        remote.run(cmd, timeout=180)
    finally:
        try:
            remote.run("rm -f /tmp/rb_codex_config.toml", check=False)
        except Exception:
            pass
        local.unlink(missing_ok=True)


def main() -> int:
    here = Path(__file__).resolve().parent
    load_dotenv(os.environ.get("RB_ENV_FILE", here / ".env.macos"), override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", choices=("recreation", "eval", "recreation_eval"), default=os.environ.get("RB_STAGE", "recreation_eval"))
    parser.add_argument("--agent-cli", choices=("claude", "codex"), default=os.environ.get("RB_AGENT_CLI", "codex"))
    parser.add_argument("--source-dir", type=Path, default=RUNTIME_ROOT)
    parser.add_argument(
        "--unified-cache-dir",
        type=Path,
        default=Path(os.environ.get("RB_UNIFIED_CACHE_DIR") or os.environ["RB_UNIFIED_LOCAL_DIR"])
        if (os.environ.get("RB_UNIFIED_CACHE_DIR") or os.environ.get("RB_UNIFIED_LOCAL_DIR"))
        else None,
    )
    parser.add_argument(
        "--unified-archive",
        type=Path,
        default=Path(os.environ["RB_UNIFIED_ARCHIVE"])
        if os.environ.get("RB_UNIFIED_ARCHIVE")
        else None,
    )
    parser.add_argument("--result-dir", type=Path, default=Path(os.environ.get("RB_RESULT_DIR", "results/macos")))
    parser.add_argument("--host", default=os.environ.get("MACOS_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MACOS_SSH_PORT", "22")))
    parser.add_argument("--username", default=os.environ.get("MACOS_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("MACOS_PASSWORD", ""))
    parser.add_argument("--ssh-key", default=os.environ.get("MACOS_SSH_KEY", ""))
    parser.add_argument("--pipeline-dir", default=os.environ.get("MACOS_PIPELINE_DIR", "$HOME/recreationbench"))
    parser.add_argument("--skip-provision", action="store_true")
    args = parser.parse_args()
    args.source_dir = validate_runtime_source_dir(args.source_dir, platform="macos")
    args.model = required("RB_MODEL")
    args.model_base_url = required("RB_MODEL_BASE_URL")
    args.model_api_key = required("RB_MODEL_API_KEY")
    args.devagent_user = os.environ.get("MACOS_DEVAGENT_USER", "devagent")
    if not args.host or not args.username or not (args.password or args.ssh_key):
        raise RuntimeError("MACOS_HOST, MACOS_USERNAME and MACOS_PASSWORD or MACOS_SSH_KEY are required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.username):
        raise RuntimeError(f"invalid MACOS_USERNAME: {args.username!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.devagent_user):
        raise RuntimeError(f"invalid MACOS_DEVAGENT_USER: {args.devagent_user!r}")
    if args.stage in {"eval", "recreation_eval"}:
        required("RB_VLM_MODEL")
        required("RB_VLM_KEY")
        required("RB_VLM_BASE_URL")

    result_dir = args.result_dir / args.task_id
    result_dir.mkdir(parents=True, exist_ok=True)
    enable_cua = truthy(os.environ.get("RB_ENABLE_CUA_DRIVER"), default=True)
    with tempfile.TemporaryDirectory(prefix="rb_macos_run_") as temp:
        temp_root = Path(temp)
        packaged = prepare_unified_archive(
            task_id=args.task_id,
            platform="macos",
            archive=args.unified_archive,
            cache_dir=args.unified_cache_dir,
            output_archive=temp_root / "unified_source.tar.gz",
        )
        unified = temp_root / "unified.tar.gz"
        flatten_unified_archive(packaged, unified)
        runtime = temp_root / "runtime.tar.gz"
        make_runtime_archive(args.source_dir, runtime)

        remote = MacRemote(args.host, args.port, args.username, args.password, args.ssh_key)
        remote_root: str | None = None
        env_file: str | None = None
        try:
            _, remote_home = remote.run("printf %s \"$HOME\"")
            remote_home = remote_home.strip()
            remote_root = args.pipeline_dir.replace("$HOME", remote_home)
            remote.run(f"mkdir -p {shlex.quote(remote_root)}")
            remote.put(runtime, "/tmp/rb_runtime.tar.gz")
            remote.run(f"tar xzf /tmp/rb_runtime.tar.gz -C {shlex.quote(remote_root)} && rm -f /tmp/rb_runtime.tar.gz", timeout=300)
            remote.put(unified, f"{remote_root}/.rb_unified_instance.tar.gz")
            # The eval stage's VLM judge runs under sudo (root escapes the per-user network
            # deny that keeps the candidate offline); sudo strips the environment, so the judge
            # re-sources its credentials from "$PIPELINE_DIR/.runtime_env" by that exact name
            # (ax_eval.sh _run_vlm_python). The platform runtime writes that file; this SSH
            # entry point is the controller here, so the one env
            # file it deploys MUST be named .runtime_env, or the judge sees no VLM_API_KEY ->
            # VLM 0/N -> the frozen suite hard-fails the run. (The run command below still
            # `source`s this same file, so recreation is unaffected.)
            env_file = f"{remote_root}/.runtime_env"
            runtime_env = {
                "MACOS_PASSWORD": args.password,
                "MACOS_USER": args.username,
                "MACOS_DEVAGENT_USER": args.devagent_user,
                "MACOS_DEVAGENT_PASSWORD": os.environ.get("MACOS_DEVAGENT_PASSWORD", args.password),
                "DEVAGENT_USER": args.devagent_user,
                "APP_NAME": args.task_id,
                "MODEL": args.model,
                "STAGE": args.stage,
                "AGENT_CLI": args.agent_cli,
                "ANTHROPIC_API_KEY": args.model_api_key,
                "ANTHROPIC_BASE_URL": args.model_base_url,
                "RB_AGENT_API_KEY": args.model_api_key,
                "RB_AGENT_BASE_URL": args.model_base_url,
                "OPENAI_API_KEY": args.model_api_key,
                "CODEX_BASE_URL": args.model_base_url,
                "VLM_MODEL": os.environ.get("RB_VLM_MODEL", ""),
                "VLM_API_KEY": os.environ.get("RB_VLM_KEY", ""),
                "VLM_BASE_URL": os.environ.get("RB_VLM_BASE_URL", ""),
                "RECREATION_TIMEOUT": os.environ.get("RB_RECREATION_TIMEOUT", "72000"),
                "EFFORT": os.environ.get("RB_EFFORT", "max"),
                "SCAFFOLD_VERSION": os.environ.get("RB_CODEX_VERSION", "0.145.0"),
                "RB_CODEX_VERSION": os.environ.get("RB_CODEX_VERSION", "0.145.0"),
                "RB_CLAUDE_VERSION": os.environ.get("RB_CLAUDE_VERSION", "2.1.177"),
                "ENABLE_CUA_DRIVER": "true" if enable_cua else "false",
            }
            env_local = temp_root / "rb_env"
            env_local.write_text(shell_env(runtime_env), encoding="utf-8")
            remote.put(env_local, env_file, mode=0o600)

            if not args.skip_provision:
                remote.put(SERVICE_ROOT / "macos" / "provision" / "setup_macos.sh", "/tmp/rb_setup_macos.sh")
                remote.put(SERVICE_ROOT / "macos" / "provision" / "requirements.macos.txt", "/tmp/rb_requirements_macos.txt")
                remote.run(
                    f"chmod 700 /tmp/rb_setup_macos.sh && "
                    f"RB_MACOS_REQUIREMENTS_FILE=/tmp/rb_requirements_macos.txt "
                    f"bash /tmp/rb_setup_macos.sh {shlex.quote(env_file)}",
                    timeout=1800,
                    stream=True,
                )
            remote.put(SERVICE_ROOT / "macos" / "provision" / "check_macos_ready.sh", "/tmp/rb_check_macos_ready.sh")
            remote.run("chmod 700 /tmp/rb_check_macos_ready.sh && bash /tmp/rb_check_macos_ready.sh", timeout=180)
            if enable_cua:
                remote.run(
                    f"source {shlex.quote(env_file)} && printf '%s\\n' \"$MACOS_PASSWORD\" | sudo -S -v && cd {shlex.quote(remote_root)} && bash scripts/macos/runtime_assets/deploy_cua_driver.sh --app scripts/macos/scaffold/bin/QwenCuaDriver.app",
                    timeout=600,
                    stream=True,
                )
            if args.agent_cli == "codex":
                deploy_codex_config(remote, args, remote_root, remote_home, env_file)

            command = f"source {shlex.quote(env_file)} && cd {shlex.quote(remote_root)} && bash scripts/macos/run_full.sh"
            rc, output = remote.run(command, timeout=int(os.environ.get("RB_TOTAL_TIMEOUT", "86400")), check=False, stream=True)
            (result_dir / "run.log").write_text(output, encoding="utf-8")
            remote_tar = "/tmp/rb_macos_results.tar.gz"
            pack_rc, pack_output = remote.run(
                f"sudo tar czf {remote_tar} -C /var/tmp/pipeline_results . && sudo chown {shlex.quote(args.username)} {remote_tar} && chmod 600 {remote_tar}",
                timeout=600,
                check=False,
            )
            local_tar = result_dir / "pipeline_results.tar.gz"
            extracted = result_dir / "results"
            shutil.rmtree(extracted, ignore_errors=True)
            local_tar.unlink(missing_ok=True)
            (result_dir / "collect_error.txt").unlink(missing_ok=True)
            try:
                if pack_rc:
                    raise RuntimeError(
                        f"remote result packaging failed (rc={pack_rc}): {pack_output[-1200:]}"
                    )
                remote.get(remote_tar, local_tar)
                extracted.mkdir(parents=True, exist_ok=True)
                safe_extract_tar(local_tar, extracted)
            except Exception as exc:
                (result_dir / "collect_error.txt").write_text(str(exc) + "\n", encoding="utf-8")
                # A successful remote process without retrievable evidence is an
                # infrastructure failure, not a completed benchmark result.
                rc = 2
            finally:
                remote.run(f"rm -f {remote_tar}", check=False)
            sandbox_info = {"sandbox_id": None, "sandbox_ip": args.host, "transport": "ssh"}
            metrics = write_metrics(result_dir, args, rc, sandbox_info)
            print(f"metrics.json={metrics}")
            return 0 if rc == 0 else rc
        finally:
            cleanup = ["/tmp/rb_codex_config.toml"]
            if env_file:
                cleanup.append(env_file)
            if remote_root:
                cleanup.append(f"{remote_root}/.rb_unified_instance.tar.gz")
            try:
                remote.run(
                    "rm -f " + " ".join(shlex.quote(path) for path in cleanup),
                    check=False,
                )
            except Exception:
                pass
            finally:
                remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
