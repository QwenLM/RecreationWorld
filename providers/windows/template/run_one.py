#!/usr/bin/env python3
"""Run one RecreationBench Windows task on a prepared SSH host."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


WINDOWS_ROOT = Path(__file__).resolve().parents[1]
# All executable platform code lives in the shared frozen runtime.  The
# top-level windows/ directory deliberately remains a thin user-facing
# template, documentation, and provisioning layer.
RUNTIME_ROOT = WINDOWS_ROOT.parent.parent


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise RuntimeError(f"missing environment variable: {'/'.join(names)}")


def check_desktop_session(host: str, username: str, password: str) -> dict:
    """Refuse UIA work until the remote Windows interactive desktop is active."""
    import base64
    import paramiko

    script = r'''$ErrorActionPreference = "Stop"
$quser = @(cmd.exe /d /c "quser 2>nul")
$sessionLines = @($quser | Select-Object -Skip 1)
$active = @($sessionLines | Where-Object {
    ($_ -match "\brdp-tcp#|\bconsole\b") -and ($_ -notmatch "\bDisc\b|断开")
})
$explorer = @(Get-Process explorer -ErrorAction SilentlyContinue)
@{ ready = ($active.Count -gt 0 -and $explorer.Count -gt 0); active_session_count = $active.Count; explorer_process_count = $explorer.Count } | ConvertTo-Json -Compress
if ($active.Count -eq 0 -or $explorer.Count -eq 0) { exit 12 }'''
    command = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16le")).decode()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=username, password=password, timeout=30)
        _, stdout, stderr = client.exec_command(command, timeout=60)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        status = stdout.channel.recv_exit_status()
    finally:
        client.close()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"desktop preflight returned invalid JSON: {output or error}") from exc
    if status != 0 or not result.get("ready"):
        raise RuntimeError(f"Windows desktop preflight failed: {result}")
    return result


def main() -> int:
    here = Path(__file__).resolve().parent
    load_dotenv(os.environ.get("RB_ENV_FILE", here / ".env.windows"), override=False)

    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", default=os.environ.get("RB_WINDOWS_STAGE", "recreation_eval"))
    parser.add_argument("--agent-cli", default=os.environ.get("RB_AGENT_CLI") or os.environ.get("AGENT_CLI", "codex"), choices=("claude", "codex"))
    parser.add_argument("--mcp-provider", default="cua-driver")  # accepted for run_many parity
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=RUNTIME_ROOT,
        help="Windows runtime root; override only when developing a replacement runtime.",
    )
    parser.add_argument(
        "--unified-dir",
        "--unified-cache-dir",
        dest="unified_dir",
        type=Path,
        default=Path(os.environ["RB_UNIFIED_LOCAL_DIR"])
        if os.environ.get("RB_UNIFIED_LOCAL_DIR", "").strip()
        else None,
        help=(
            "local frozen-input root. Expected <root>/windows/<task-id>/ or "
            "<root>/<task-id>/."
        ),
    )
    parser.add_argument("--result-dir", type=Path, default=Path(os.environ.get("RB_RESULT_DIR", "results")))
    parser.add_argument("--host", default=os.environ.get("RB_WINDOWS_HOST", ""))
    parser.add_argument("--username", default=os.environ.get("RB_WINDOWS_USERNAME") or os.environ.get("SANDBOX_USERNAME", "Administrator"))
    parser.add_argument("--password", default=os.environ.get("RB_WINDOWS_PASSWORD") or os.environ.get("SANDBOX_PASSWORD", ""))
    parser.add_argument("--skip-desktop-preflight", action="store_true")
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        raise RuntimeError(f"Windows runtime source directory not found: {args.source_dir}")
    required_runtime_files = (
        "scripts/core/pipeline.py",
        "scripts/platforms/windows/pipeline.py",
        "scripts/windows/vm_runtime.py",
        "scripts/windows/worker.py",
    )
    missing_runtime_files = [
        rel for rel in required_runtime_files if not (args.source_dir / rel).is_file()
    ]
    if missing_runtime_files:
        raise RuntimeError(
            "Windows runtime source is incomplete: "
            + ", ".join(missing_runtime_files)
        )
    if args.unified_dir is None:
        raise RuntimeError("--unified-dir or RB_UNIFIED_LOCAL_DIR is required")
    if not args.unified_dir.is_dir():
        raise RuntimeError(f"local frozen-input directory not found: {args.unified_dir}")
    host = args.host or required("RB_WINDOWS_HOST")
    password = args.password or required("RB_WINDOWS_PASSWORD")
    result_dir = args.result_dir / args.task_id
    result_dir.mkdir(parents=True, exist_ok=True)
    if args.stage != "setup" and not args.skip_desktop_preflight:
        try:
            preflight = check_desktop_session(host, args.username, password)
            (result_dir / "desktop_preflight.json").write_text(json.dumps(preflight, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            payload = {"task_id": args.task_id, "platform": "windows", "passed": False, "failure_phase": "desktop_preflight", "failure_reason": str(exc)}
            (result_dir / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"desktop preflight failed: {exc}", file=sys.stderr)
            return 2

    env = os.environ.copy()
    env.update({
        "RB_TASK_ID": args.task_id,
        "RB_STAGE": args.stage,
        "RB_HOST": host,
        "RB_HOST_USER": args.username,
        "RB_HOST_PASSWORD": password,
        "RB_UNIFIED_PLATFORM": "windows",
        "RB_AGENT_CLI": args.agent_cli,
        "RB_OUTPUT_DIR": str(result_dir.resolve()),
    })
    env["RB_UNIFIED_LOCAL_DIR"] = str(args.unified_dir.resolve())
    if args.stage != "setup":
        env.update({
            "RB_MODEL": env_first("RB_MODEL", "MODEL"),
            "RB_MODEL_API_KEY": env_first("RB_MODEL_API_KEY", "MODEL_API_KEY", "ANTHROPIC_API_KEY"),
            "RB_MODEL_BASE_URL": env_first("RB_MODEL_BASE_URL", "MODEL_BASE_URL", "ANTHROPIC_BASE_URL"),
        })
    if args.stage in {"eval", "recreation_eval"}:
        env.update({
            "RB_VLM_MODEL": env_first("RB_VLM_MODEL", "VLM_JUDGE_MODEL", "VLM_MODEL"),
            "RB_VLM_KEY": env_first("RB_VLM_KEY", "VLM_JUDGE_API_KEY", "VLM_MODEL_API_KEY"),
            "RB_VLM_BASE_URL": env_first("RB_VLM_BASE_URL", "VLM_JUDGE_BASE_URL", "VLM_MODEL_BASE_URL"),
        })

    pipeline = args.source_dir / "scripts" / "core" / "pipeline.py"
    command = [
        sys.executable,
        str(pipeline),
        "--platform",
        "windows",
        "--task-id",
        args.task_id,
        "--stage",
        args.stage,
        "--output-dir",
        str(result_dir.resolve()),
        "--host",
        host,
        "--host-user",
        args.username,
        "--agent-cli",
        args.agent_cli,
        "-x",
        f"mcp_provider={args.mcp_provider}",
    ]
    result = subprocess.run(command, cwd=args.source_dir, env=env, text=True)
    metrics = result_dir / "metrics.json"
    if not metrics.exists():
        summary = result_dir / "pipeline_summary.json"
        payload = {"task_id": args.task_id, "platform": "windows", "stage": args.stage, "pipeline_exit_code": result.returncode, "passed": result.returncode == 0, "task_score": None}
        if summary.exists():
            pipeline = json.loads(summary.read_text(encoding="utf-8"))
            payload["pipeline_summary"] = pipeline
            payload["task_score"] = (pipeline.get("eval_eval") or {}).get("programmatic_pass_rate")
        metrics.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"metrics.json={metrics}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
