#!/usr/bin/env python3
"""Native Windows VM worker for the Recreation-Bench release lifecycle.

Runs directly on a Windows machine — no SSH or remote execution.

Usage:
    # Single task
    python worker.py --task-id my-app

    # All tasks from file
    python worker.py --tasks tasks.jsonl

    # Run one stage
    python worker.py --task-id my-app --stage recreation

    # Check status
    python worker.py --status
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# scripts/core is synced to the VM alongside scripts/windows, but it is NOT importable until
# scripts/ is on sys.path — and this module runs ON the VM. Importing core above, before this
# insert, gave "ModuleNotFoundError: No module named 'core'" and the stage died as
# pipeline_not_started before the agent ever ran.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import exit_contract  # noqa: E402
from core import recreation_artifact as core_recreation_artifact  # noqa: E402
from core import recreation_paths as core_recreation_paths  # noqa: E402
from core import trajectory as core_trajectory  # noqa: E402
from core.pipeline import EVAL_TARGETS, STAGE_VALUES, stages_for  # noqa: E402
from stages import recreation, uia_eval
from stages.config import vm_task_dir

# `setup` is deliberately absent: the boundary probe is driven from the pod by vm_runtime.py through
# core.permission_probe.run_remote, so this on-VM pipeline never runs it. The verdict belongs on the
# trusted side, not on the machine being attested.

_NOISE_PROCESSES = ["WerFault.exe", "WerFault"]
CANDIDATE_BUILD_TIMEOUT = 1800
CANDIDATE_BUILD_LOG_LIMIT = 16_000


def _cleanup_desktop() -> None:
    """Kill leftover processes that clutter the desktop between stages."""
    for name in _NOISE_PROCESSES:
        subprocess.run(
            ["taskkill", "/f", "/im", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_dir_suffix = ""
_effort = "max"
_auto_compact_window = ""
_autocompact_pct = ""
_cua_coordinate_space = "0"
_cua_coordinate_scale = "1000"
_mcp_model_payload_filter = "0"
_extra_body = ""
_agent_cli = "claude"
_api_timeout_ms = 1800000
_mcp_tool_timeout_ms = 180000
_claude_code_max_output_tokens = ""
_time_budget_hook = False
_eval_target = "recreation"


def _format_candidate_build_output(value: object) -> str:
    """Bound build diagnostics and redact credentials inherited by the VM worker."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    for name, secret in os.environ.items():
        upper = name.upper()
        if (
            secret
            and len(secret) >= 8
            and any(
                marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )
        ):
            text = text.replace(secret, f"<redacted:{name}>")
    if len(text) > CANDIDATE_BUILD_LOG_LIMIT:
        text = (
            "[... earlier build output truncated ...]\n"
            + text[-CANDIDATE_BUILD_LOG_LIMIT:]
        )
    return text.strip()


def _print_candidate_build_output(stdout: object, stderr: object) -> None:
    for label, value in (("stdout", stdout), ("stderr", stderr)):
        rendered = _format_candidate_build_output(value)
        if rendered:
            print(f"  Candidate build {label}:\n{rendered}")


def _rec_dir(base: str) -> str:
    suffix = f"_{_dir_suffix}" if _dir_suffix else ""
    primary = os.path.join(base, f"recreation{suffix}")
    if os.path.isdir(primary):
        return primary
    # Fallback: find a single recreation_* dir (old model_slug format)
    import glob

    candidates = [
        d for d in glob.glob(os.path.join(base, "recreation_*")) if os.path.isdir(d)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return primary


def _find_reference_exe(reference_dir: str) -> str | None:
    """Find the reference executable path from its frozen metadata."""
    result_path = os.path.join(reference_dir, "build_result.json")
    if not os.path.exists(result_path):
        return None
    try:
        data = json.loads(Path(result_path).read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    exe = data.get("executable", "")
    if not exe:
        return None
    if os.path.exists(exe):
        return exe
    if not os.path.isabs(exe):
        candidate = os.path.join(reference_dir, exe)
        if os.path.exists(candidate):
            return candidate
    return None


def _find_recreation_exe_in_app(rec_output: str) -> str | None:
    """Find an executable inside one already-resolved recreation app root."""
    result_path = os.path.join(rec_output, "build_result.json")
    exe_name = ""
    if os.path.exists(result_path):
        try:
            data = json.loads(Path(result_path).read_text(encoding="utf-8-sig"))
            exe = data.get("executable", "")
            if exe:
                if os.path.exists(exe):
                    return exe
                if not os.path.isabs(exe):
                    candidate = os.path.join(rec_output, exe)
                    if os.path.exists(candidate):
                        return candidate
                # Absolute path from a different VM — try to find the exe by name
                # in the current recreation directory
                exe_name = os.path.basename(exe)
        except (json.JSONDecodeError, OSError):
            pass

    # Search bin/ first (standard output location)
    bin_dir = os.path.join(rec_output, "bin")
    if os.path.isdir(bin_dir):
        for root, _, files in os.walk(bin_dir):
            for f in files:
                if f.lower().endswith(".exe"):
                    return os.path.join(root, f)

    # Broad search: look for the exe by name (from build_result.json) anywhere
    # under recreation output, or fall back to any .exe in src/bin/
    if exe_name:
        for root, _, files in os.walk(rec_output):
            for f in files:
                if f.lower() == exe_name.lower():
                    return os.path.join(root, f)

    # Last resort: any .exe under src/bin/ (common .NET publish location)
    src_bin = os.path.join(rec_output, "src", "bin")
    if os.path.isdir(src_bin):
        for root, _, files in os.walk(src_bin):
            for f in files:
                if f.lower().endswith(".exe"):
                    return os.path.join(root, f)

    return None


def _find_recreation_exe(rec_dir: str) -> str | None:
    """Find recreation executable — prefer build_result.json, fallback to broad scan."""
    # Every run writes to the canonical recreation/ inner directory; app_root resolves only that
    # contract (or an explicit valid manifest).
    return _find_recreation_exe_in_app(
        core_recreation_artifact.app_root(rec_dir, "windows")
    )


# ---------------------------------------------------------------------------
# Verify functions
# ---------------------------------------------------------------------------


def verify_recreation(task_id: str) -> bool:
    """Check recreation outputs: build.ps1, launch.ps1 exist and src/ has files."""
    base = vm_task_dir(task_id)
    rec = _rec_dir(base)
    rec_output = core_recreation_artifact.app_root(rec, "windows")

    build_ps1 = os.path.join(rec_output, "build.ps1")
    launch_ps1 = os.path.join(rec_output, "launch.ps1")
    src_dir = os.path.join(rec_output, "src")

    has_build = os.path.exists(build_ps1)
    has_launch = os.path.exists(launch_ps1)
    src_count = 0
    if os.path.isdir(src_dir):
        src_count = sum(len(files) for _, _, files in os.walk(src_dir))

    ok = has_build and has_launch and src_count > 0
    print(
        f"  Recreation verify: {'PASS' if ok else 'FAIL'} "
        f"(build.ps1={has_build}, launch.ps1={has_launch}, src_files={src_count})"
    )
    return ok


def _traj_path(task_id: str) -> str:
    """Where the recreation stage leaves the agent transcript it collected."""
    return os.path.join(_rec_dir(vm_task_dir(task_id)), "trajectory.jsonl")


def verify_eval(task_id: str) -> tuple[bool, str]:
    """Check eval outputs: programmatic_results.json and vlm_results.json.

    Returns (ok, fail_code) where fail_code is empty on success.
    """
    base = vm_task_dir(task_id)
    suffix = f"_{_dir_suffix}" if _dir_suffix else ""
    rec_variant = f"recreation{suffix}"
    eval_dir = os.path.join(base, f"uia_eval_{rec_variant}")

    prog_path = os.path.join(eval_dir, "programmatic_results.json")
    vlm_path = os.path.join(eval_dir, "vlm_results.json")
    has_prog = os.path.exists(prog_path)
    has_vlm = os.path.exists(vlm_path)

    prog_ok = False
    if has_prog:
        try:
            data = json.loads(Path(prog_path).read_text(encoding="utf-8-sig"))
            prog_ok = int(data.get("total", 0)) > 0
        except (json.JSONDecodeError, OSError):
            pass

    vlm_error = ""
    if has_vlm:
        try:
            vlm_data = json.loads(Path(vlm_path).read_text(encoding="utf-8-sig"))
            vlm_error = vlm_data.get("error", "")
        except (json.JSONDecodeError, OSError):
            vlm_error = "invalid_json"

    ok = prog_ok and has_vlm and not vlm_error
    print(
        f"  Eval verify: {'PASS' if ok else 'FAIL'} "
        f"(programmatic={has_prog}{'(valid)' if prog_ok else ''}, "
        f"vlm={has_vlm}"
        f"{', vlm_error=' + vlm_error if vlm_error else ''})"
    )

    fail_code = ""
    if not ok:
        if vlm_error and vlm_error.startswith("vlm_judge_errors"):
            fail_code = "vlm_judge_errors"

    return ok, fail_code


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def run_recreation(
    task: dict,
    model: str,
    auth_token: str,
    base_url: str,
    timeout: int = 72000,
    claude_model: str = "",
) -> dict:
    if verify_recreation(task["task_id"]):
        print("  Recreation already complete, skipping")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_PASS,
            outcome_class=exit_contract.OUTCOME_COMPLETED,
            reason_code="already_completed",
        )

    output, exit_code = recreation.run(
        task=task,
        model=model,
        auth_token=auth_token,
        base_url=base_url,
        timeout=timeout,
        dir_suffix=_dir_suffix,
        effort=_effort,
        auto_compact_window=_auto_compact_window,
        autocompact_pct=_autocompact_pct,
        cua_coordinate_space=_cua_coordinate_space,
        cua_coordinate_scale=_cua_coordinate_scale,
        mcp_model_payload_filter=_mcp_model_payload_filter,
        extra_body=_extra_body,
        agent_cli=_agent_cli,
        claude_model=claude_model,
        api_timeout_ms=_api_timeout_ms,
        mcp_tool_timeout_ms=_mcp_tool_timeout_ms,
        claude_code_max_output_tokens=_claude_code_max_output_tokens,
        time_budget_hook=_time_budget_hook,
    )

    if exit_code != 0:
        print(f"  Recreation agent exited with code {exit_code}: {output}")
        terminal_completed = "agent_terminal=completed" in output
        # Only normal model boundaries may be salvaged by a valid artifact. Protocol/API,
        # preflight and termination failures stay failures even if they left a
        # partial build behind.
        if verify_recreation(task["task_id"]) and (
            exit_code == 124 or (exit_code == 1 and terminal_completed)
        ):
            print("  But outputs look valid — continuing")
            return exit_contract.stage_outcome(
                status=exit_contract.STAGE_PASS,
                outcome_class=exit_contract.OUTCOME_COMPLETED,
                reason_code="completed_with_native_error",
                native_exit_code=exit_code,
            )
        print(f"  RECREATION FAILED (exit_code={exit_code})")
        if exit_code == 124:
            # Exhausting the advertised model budget without a usable artifact is
            # a terminal model result, not an infrastructure retry signal.
            outcome_class = exit_contract.OUTCOME_COMPLETED
            reason_code = "agent_budget_exhausted"
            status = exit_contract.STAGE_FAIL
        elif exit_code == 86 and "mcp_preflight=pass" in output:
            # The trusted preflight succeeded but the model never completed a
            # desktop-control call.  That is also a legitimate model zero.
            outcome_class = exit_contract.OUTCOME_COMPLETED
            reason_code = "no_successful_mcp_call"
            status = exit_contract.STAGE_FAIL
        elif (
            exit_contract.from_native_exit(exit_code)
            == exit_contract.OUTCOME_TERMINATED
        ):
            outcome_class = exit_contract.OUTCOME_TERMINATED
            reason_code = "worker_terminated"
            status = exit_contract.STAGE_TIMEOUT
        else:
            outcome_class = exit_contract.OUTCOME_INFRA_ERROR
            status = exit_contract.STAGE_ERROR
            reason_code = {
                86: "mcp_preflight_failed",
                87: "runner_exception",
            }.get(exit_code, "agent_process_failed")
            if "agent_cleanup_error=" in output:
                # Distinguishable in metrics, because the operator response differs: the agent
                # finished and its artifact was snapshotted, so this is a boundary to inspect,
                # not a run to repeat. Under the generic label it read as a failed agent.
                reason_code = "agent_cleanup_error"
            elif (
                core_trajectory.terminal_stop_reason(_traj_path(task["task_id"]))
                == "refusal"
            ):
                # A model declining by design is a terminal model result, like the two branches
                # above: stop_reason=refusal with api_error_status null, and retrying reproduces it
                # exactly -- one app that asks for a keystroke display tool was blocked on
                # "violative cyber content" twice, at 7 turns/$0.15 and again at 6 turns/$0.44.
                # The first version of this branch kept the infra_error class, which the contract
                # ties to retryable=true and process exit 2, so the label said "do not retry" while
                # the metrics invited one -- and that $0.44 retry is what the invitation bought.
                # Scoring it as a zero rather than dropping the row is deliberate: a refusal is an
                # outcome of the model's own policy, so it belongs in the denominator.
                outcome_class = exit_contract.OUTCOME_COMPLETED
                status = exit_contract.STAGE_FAIL
                reason_code = "agent_policy_refusal"
        return exit_contract.stage_outcome(
            status=status,
            outcome_class=outcome_class,
            reason_code=reason_code,
            native_exit_code=exit_code,
        )
    return exit_contract.stage_outcome(
        status=exit_contract.STAGE_PASS,
        outcome_class=exit_contract.OUTCOME_COMPLETED,
        reason_code="agent_completed",
        native_exit_code=exit_code,
    )


def run_eval(
    task: dict,
    vlm_key: str = "",
    vlm_model: str = "",
    vlm_base_url: str = "",
) -> dict:
    """Run eval and return the same structured stage outcome as every platform."""
    task_id = task["task_id"]
    already_ok, _ = verify_eval(task_id)
    if already_ok:
        print("  Eval already complete, skipping")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_PASS,
            outcome_class=exit_contract.OUTCOME_COMPLETED,
            reason_code="already_completed",
        )

    base = vm_task_dir(task_id)

    # --- Safeguard: kill reference app and remove install dir ---
    # Prevents eval from testing the reference app if the recreation
    # agent's launch.ps1 accidentally points to the original binary.
    reference_dir = os.path.join(base, "reference")
    reference_exe = _find_reference_exe(reference_dir)
    if reference_exe and _eval_target == "recreation":
        exe_name = os.path.basename(reference_exe)
        print(f"  Killing reference app ({exe_name}) before eval")
        subprocess.run(
            ["taskkill", "/f", "/im", exe_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    install_dir = os.path.join(base, "install")
    if os.path.isdir(install_dir) and _eval_target == "recreation":
        print(f"  Removing install directory: {install_dir}")
        shutil.rmtree(install_dir, ignore_errors=True)

    suffix = f"_{_dir_suffix}" if _dir_suffix else ""
    rec_variant = f"recreation{suffix}"

    # Select the candidate explicitly. A reference check consumes the freshly
    # materialized frozen build and never restores or inspects recreation output.
    rec_dir = _rec_dir(base)
    candidate_app = ""
    if _eval_target == "reference":
        rec_exe = reference_exe
    else:
        rec_app = core_recreation_artifact.app_root(rec_dir, "windows")
        candidate_app = rec_app
        if os.path.isdir(rec_app):
            try:
                recreation._terminate_agent_processes()
                recreation._remove_workspace_tree(
                    core_recreation_paths.WINDOWS_RUNTIME_ROOT
                )
                # Eval-only resumes need the same canonical absolute paths that an authored
                # launcher may contain, but the path remains a real directory rather than a
                # task-revealing junction. The stage snapshot stays authoritative.
                candidate_app = recreation._prepare_canonical_workspace(rec_app)
            except (OSError, RuntimeError) as exc:
                print(f"  EVAL FAILED: could not restore canonical workspace: {exc}")
                return exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code="workspace_restore_failed",
                )
        # Evaluate the writable canonical copy, not the immutable stage snapshot.  Most
        # recreations already contain ``bin/*.exe`` so the old distinction was invisible;
        # source-only eval resumes exposed it when their fallback build wrote beside
        # ``C:\workspace\recreation\build.ps1`` while discovery kept searching the frozen
        # task directory.
        rec_exe = _find_recreation_exe_in_app(candidate_app)

    # If no exe, try running build.ps1
    if not rec_exe and _eval_target == "recreation":
        _rec_app = candidate_app or core_recreation_artifact.app_root(
            rec_dir, "windows"
        )
        build_ps1 = os.path.join(_rec_app, "build.ps1")
        if os.path.exists(build_ps1):
            print("  No exe found — running build.ps1 to compile recreation")
            try:
                build = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-WindowStyle",
                        "Hidden",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        build_ps1,
                    ],
                    cwd=_rec_app,
                    timeout=CANDIDATE_BUILD_TIMEOUT,
                    capture_output=True,
                )
                print(f"  Candidate build exit code: {build.returncode}")
                _print_candidate_build_output(build.stdout, build.stderr)
            except subprocess.TimeoutExpired as exc:
                print(f"  Build timed out after {CANDIDATE_BUILD_TIMEOUT}s")
                _print_candidate_build_output(exc.stdout, exc.stderr)
            except Exception as e:
                print(f"  Build error: {e}")
            rec_exe = _find_recreation_exe_in_app(_rec_app)

    if not rec_exe:
        print("  EVAL FAILED: no recreation executable found")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_FAIL,
            outcome_class=exit_contract.OUTCOME_DATA_ERROR,
            reason_code="no_recreation_exe",
        )

    tests_dir = os.path.join(base, "tests")
    if not os.path.isdir(tests_dir):
        print("  EVAL FAILED: frozen tests directory missing")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_FAIL,
            outcome_class=exit_contract.OUTCOME_DATA_ERROR,
            reason_code="frozen_tests_missing",
        )

    # Fixtures directory
    fixtures_dir = os.path.join(tests_dir, "fixtures")

    # Find launch.ps1 for the recreation
    rec_launch_ps1 = (
        os.path.join(reference_dir, "launch.ps1")
        if _eval_target == "reference"
        else os.path.join(candidate_app, "launch.ps1")
    )
    launch_ps1 = rec_launch_ps1 if os.path.exists(rec_launch_ps1) else None

    manifest_path = os.path.join(tests_dir, "test_manifest.json")
    if not os.path.isfile(manifest_path):
        print("  EVAL FAILED: frozen test_manifest.json missing")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_FAIL,
            outcome_class=exit_contract.OUTCOME_DATA_ERROR,
            reason_code="frozen_manifest_missing",
        )
    print(f"  Frozen-manifest scoring: {manifest_path}")

    result = uia_eval.run(
        task_id=task_id,
        exe_path=rec_exe,
        variant=rec_variant,
        tests_dir=tests_dir,
        fixtures_dir=fixtures_dir,
        vlm_key=vlm_key,
        vlm_model=vlm_model,
        vlm_base_url=vlm_base_url,
        launch_ps1=launch_ps1,
        manifest=manifest_path,
    )

    if result.get("error"):
        print(f"  EVAL FAILED: {result['error']}")
        return exit_contract.stage_outcome(
            status=exit_contract.STAGE_ERROR,
            outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
            reason_code="eval_runner_failed",
        )
    return exit_contract.stage_outcome(
        status=exit_contract.STAGE_PASS,
        outcome_class=exit_contract.OUTCOME_COMPLETED,
        reason_code="eval_completed",
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def run_pipeline(
    task: dict,
    model: str,
    auth_token: str,
    base_url: str,
    stage: str = "recreation_eval",
    vlm_key: str = "",
    vlm_model: str = "",
    vlm_base_url: str = "",
    recreation_timeout: int = 72000,
    claude_model: str = "",
):
    tid = task["task_id"]
    base = vm_task_dir(tid)
    os.makedirs(base, exist_ok=True)

    # --- Crash diagnostics: atexit marker, signal handler, heartbeat ---
    exit_marker_path = os.path.join(base, "pipeline_exit_marker.txt")

    def _write_exit_marker(reason: str = "unknown"):
        try:
            Path(exit_marker_path).write_text(
                f"reason={reason}\ntime={time.strftime('%Y-%m-%dT%H:%M:%S')}\npid={os.getpid()}"
            )
        except Exception:
            pass

    atexit.register(_write_exit_marker, "atexit")

    def _on_signal(signum, _frame):
        _write_exit_marker(f"signal_{signum}")
        # SIGTERM and SIGINT have one public meaning across all five platforms:
        # an externally terminated run. Keep the native signal in the marker,
        # but never expose platform-specific 130/143 variants to the caller.
        sys.exit(exit_contract.RC_TIMEOUT)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    heartbeat_path = os.path.join(base, "pipeline_heartbeat")

    def _heartbeat_loop():
        while True:
            try:
                Path(heartbeat_path).write_text(
                    f"{time.time()}\n{os.getpid()}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}"
                )
            except Exception:
                pass
            time.sleep(30)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    stages_to_run = list(stages_for(stage))
    if "setup" in stages_to_run:
        raise ValueError("setup is driven by the trusted Windows orchestrator")

    print(f"\n{'=' * 60}")
    print(f"Pipeline: {tid}")
    print(f"Model: {model}")
    print(f"Stages: {' -> '.join(stages_to_run)}")
    print(f"{'=' * 60}")

    state_file = Path(os.path.join(base, "pipeline_state.json"))
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    if model and state.get("model") != model:
        state = {}
    state["task_id"] = tid
    if model:
        state["model"] = model
    state.setdefault("stages", {})

    for stage in stages_to_run:
        print(f"\n--- {stage.upper()} ---")
        _cleanup_desktop()
        state["stages"][stage] = {
            "status": "running",
            "started": time.strftime("%H:%M:%S"),
        }
        state_file.write_text(json.dumps(state, indent=2))

        try:
            if stage == "recreation":
                stage_result = run_recreation(
                    task,
                    model,
                    auth_token,
                    base_url,
                    timeout=recreation_timeout,
                    claude_model=claude_model,
                )
            elif stage == "eval":
                stage_result = run_eval(
                    task,
                    vlm_key=vlm_key,
                    vlm_model=vlm_model,
                    vlm_base_url=vlm_base_url,
                )
            else:  # guarded by stages_for(), but fail closed if it ever drifts
                raise ValueError(f"unsupported stage: {stage}")
        except Exception as e:
            print(f"  CRASH in {stage}: {e}")
            import traceback

            traceback.print_exc()
            stage_result = exit_contract.stage_outcome(
                status=exit_contract.STAGE_ERROR,
                outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                reason_code="stage_exception",
            )

        verified = stage_result["status"] == exit_contract.STAGE_PASS
        if verified and stage == "eval":
            verified, verify_fail_code = verify_eval(tid)
            if not verified:
                stage_result = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_ERROR,
                    outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                    reason_code=verify_fail_code or "eval_result_invalid",
                    native_exit_code=stage_result.get("native_exit_code"),
                )
        elif verified and stage == "recreation":
            verified = verify_recreation(tid)
            if not verified:
                # A missing deliverable stays a model failure either way -- the classification is
                # a scoring decision, and this signal is a heuristic that catches an empty final
                # text but not a truncated one, so it is not strong enough to move a run out of
                # the scored set on its own.  What it does buy is a name: three grok46 agents
                # ended 156ms apart with subtype=success, is_error=false and an empty result
                # string, after 25 to 440 turns and up to $63, and nothing anywhere recorded that
                # they had been cut rather than having decided to stop.  Reading fail_code is now
                # enough to separate the two without reopening every trajectory.
                final_text = core_trajectory.terminal_final_text(_traj_path(tid))
                empty_completion = final_text is not None and not final_text.strip()
                if empty_completion:
                    print(
                        "  Terminal record reports success with an empty final text: the session "
                        "ended on a completion carrying no content and no tool call"
                    )
                stage_result = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_FAIL,
                    outcome_class=exit_contract.OUTCOME_COMPLETED,
                    reason_code=(
                        "agent_empty_completion"
                        if empty_completion
                        else "no_usable_artifact"
                    ),
                    native_exit_code=stage_result.get("native_exit_code"),
                )

        stage_state = {**stage_result, "time": time.strftime("%H:%M:%S")}
        if not verified:
            stage_state["fail_code"] = stage_result["reason_code"]
        state["stages"][stage] = stage_state
        state_file.write_text(json.dumps(state, indent=2))

        if not verified:
            # The agent protocol's own account of why it stopped, printed here on purpose: metrics'
            # exit_reason is the last five lines of this log, so this is what carries the upstream
            # cause off the VM.  Without it every upstream failure reads as bare
            # "agent_process_failed" -- 'Request timed out', a mid-stream 400 from the batching
            # backend and 'Upstream stream truncated before completion' were indistinguishable in
            # metrics.json, and separating four such jobs meant downloading 201MB of artifacts and
            # parsing each trajectory by hand.  terminal_error_message returns None for a clean
            # stop and for budget/turn exhaustion, so this only fires when there is something to say.
            upstream = core_trajectory.terminal_error_message(_traj_path(tid))
            if upstream:
                print(f"  AGENT TERMINAL ERROR: {upstream.strip()[:300]}")
            print(
                f"\n  ABORT: {stage} {stage_result['reason_code']}. "
                f"Resume with --stage {stage}"
            )
            current_index = stages_to_run.index(stage)
            for pending in stages_to_run[current_index + 1 :]:
                state["stages"][pending] = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_NOT_RUN,
                    outcome_class=stage_result["outcome_class"],
                    reason_code="upstream_stage_failed",
                )
            state_file.write_text(json.dumps(state, indent=2))
            return state

    print(f"\n{'=' * 60}")
    print(f"COMPLETE: {tid}")
    for s, info in state["stages"].items():
        print(f"  {s}: {info['status']}")
    print(f"{'=' * 60}")
    return state


def main():
    parser = argparse.ArgumentParser(description="Recreation-Bench Windows pipeline")
    parser.add_argument("--task-id", required=True, help="Task ID")
    parser.add_argument("--model", default="", help="Model ID")
    parser.add_argument(
        "--claude-model",
        default="",
        help="Claude Code client alias; --model remains the upstream model ID",
    )
    parser.add_argument("--auth-token", default="", help="API auth token")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--vlm-key", default="", help="VLM API key for visual eval")
    parser.add_argument(
        "--vlm-model", default="", help="VLM model name for visual eval"
    )
    parser.add_argument("--vlm-base-url", default="", help="VLM API base URL")
    parser.add_argument(
        "--stage",
        choices=tuple(value for value in STAGE_VALUES if value != "setup"),
        default="recreation_eval",
        help="Lifecycle selection; recreation_eval runs recreation then eval",
    )
    parser.add_argument(
        "--eval-target",
        choices=EVAL_TARGETS,
        default=os.environ.get("RB_EVAL_TARGET", "recreation"),
        help="Candidate for eval-only runs: recreation or frozen reference",
    )
    parser.add_argument("--recreation-timeout", type=int, default=72000)
    parser.add_argument(
        "--dir-suffix", default="", help="Suffix appended to recreation/eval dir names"
    )
    parser.add_argument(
        "--effort", default="max", help="Thinking effort level (max/high/medium/low)"
    )
    parser.add_argument(
        "--auto-compact-window",
        default="",
        help="CLAUDE_CODE_AUTO_COMPACT_WINDOW env var for Claude",
    )
    parser.add_argument(
        "--autocompact-pct",
        default="",
        help="CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env var for Claude",
    )
    parser.add_argument(
        "--cua-coordinate-space",
        default="0",
        help="CUA_DRIVER_RS_COORDINATE_SPACE value",
    )
    parser.add_argument(
        "--cua-coordinate-scale",
        default="1000",
        help="CUA_DRIVER_RS_COORDINATE_SCALE value",
    )
    parser.add_argument(
        "--mcp-model-payload-filter", default="0", help="MCP_MODEL_PAYLOAD_FILTER value"
    )
    parser.add_argument(
        "--extra-body-b64",
        default="",
        help=(
            "Base64-encoded JSON string for CLAUDE_CODE_EXTRA_BODY "
            "(avoids PowerShell quote issues)"
        ),
    )
    parser.add_argument(
        "--agent-cli",
        default="claude",
        choices=["claude", "codex", "codex-mcp"],
        help=(
            "Agent CLI for recreation; every Codex spelling uses desktop-control MCP "
            "(codex-mcp is a compatibility alias)"
        ),
    )
    parser.add_argument(
        "--api-timeout-ms",
        type=int,
        default=1800000,
        help="Per-model-request timeout in milliseconds",
    )
    parser.add_argument(
        "--mcp-tool-timeout-ms",
        type=int,
        default=180000,
        help="Per-MCP-call timeout in milliseconds",
    )
    parser.add_argument(
        "--claude-code-max-output-tokens",
        default="",
        help="CLAUDE_CODE_MAX_OUTPUT_TOKENS env var for Claude",
    )
    parser.add_argument(
        "--time-budget-hook",
        action="store_true",
        help=(
            "Enable time-budget forcing-function hooks (Claude only): inject "
            "escalating reminders at 25/50/75/90%% of the recreation timeout"
        ),
    )
    args = parser.parse_args()

    stages_need_llm = {"recreation"}
    stages_to_run = set(stages_for(args.stage))
    if stages_need_llm & stages_to_run:
        if not args.model or not args.auth_token or not args.base_url:
            parser.error(
                "--model, --auth-token, and --base-url are required for recreation"
            )

    global _dir_suffix, _effort, _auto_compact_window, _autocompact_pct
    global _cua_coordinate_space, _cua_coordinate_scale, _mcp_model_payload_filter
    global _extra_body, _agent_cli, _api_timeout_ms, _mcp_tool_timeout_ms
    global _claude_code_max_output_tokens, _time_budget_hook
    global _eval_target
    _dir_suffix = args.dir_suffix
    _effort = args.effort
    _auto_compact_window = args.auto_compact_window
    _autocompact_pct = args.autocompact_pct
    _cua_coordinate_space = args.cua_coordinate_space
    _cua_coordinate_scale = args.cua_coordinate_scale
    _mcp_model_payload_filter = args.mcp_model_payload_filter
    _extra_body = ""
    if args.extra_body_b64:
        try:
            _extra_body = base64.b64decode(args.extra_body_b64).decode("utf-8")
        except Exception as exc:
            print(f"WARNING: Could not decode extra-body-b64: {exc}")
    _agent_cli = args.agent_cli
    _api_timeout_ms = args.api_timeout_ms
    _mcp_tool_timeout_ms = args.mcp_tool_timeout_ms
    _claude_code_max_output_tokens = args.claude_code_max_output_tokens
    _time_budget_hook = args.time_budget_hook
    _eval_target = args.eval_target

    task = {"task_id": args.task_id}

    state = run_pipeline(
        task,
        args.model,
        args.auth_token,
        args.base_url,
        stage=args.stage,
        vlm_key=args.vlm_key,
        vlm_model=args.vlm_model,
        vlm_base_url=args.vlm_base_url,
        recreation_timeout=args.recreation_timeout,
        claude_model=args.claude_model,
    )
    outcomes = {name: state["stages"][name] for name in stages_for(args.stage)}
    return exit_contract.aggregate_stage_outcomes(outcomes, stages_for(args.stage))[
        "process_exit_code"
    ]


if __name__ == "__main__":
    raise SystemExit(main())
