"""Stage: AT-SPI evaluation with VLM visual assertions.

Runs AT-SPI test scripts against build/recreation, collects programmatic
results + screenshots, then judges visual assertions with the configured VLM.

Two-phase design:
  Phase 1 (VM, runs tests): Run test scripts → results + screenshots
  Phase 2 (VM, needs network): Judge screenshots with the shared VLM judge
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

# These adapters can also be imported directly from a synced VM checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import runtime_assets
from vm_utils import run_command, upload_to_vm

from stages.config import VM_CODE_DIR, vm_task_dir

RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "tools" / "atspi_test_runner.py"
SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
VLM_JUDGE_SOURCE = SCRIPTS_ROOT / "common" / "vlm_judge.py"
_FIND_RECREATION = runtime_assets.load_text("linux/runtime/find_recreation.sh")
_FIND_REFERENCE = runtime_assets.load_text("linux/runtime/find_reference.sh")

PHASE1_SCRIPT = runtime_assets.load_text("linux/runtime/atspi_phase1.sh")

# Phase 2: the VM receives the same stdlib-only judge used by macOS and Windows.
PHASE2_SCRIPT = runtime_assets.load_text("linux/runtime/atspi_phase2.sh")


def run(
    client,
    task: dict,
    instance_id: str,
    image: str,
    variant: str = "recreation",
    rec_dir: str = "",
    candidate_dir: str = "",
    app_name: str = "",
    vlm_key: str = "",
    vlm_model: str = "",
    vlm_base_url: str = "",
    timeout_phase1: int = 2400,
    timeout_phase2: int = 600,
) -> dict:
    """Run AT-SPI eval with VLM visual assertions."""
    base = vm_task_dir(task["task_id"])
    results_dir = f"{base}/atspi_eval_{variant}"

    run_command(
        client,
        f"mkdir -p {results_dir}/common",
        instance_id=instance_id,
        timeout=15,
    )
    time.sleep(2)

    rec_root = rec_dir or f"{base}/{variant}"
    candidate_root = candidate_dir or f"{rec_root}/recreation"
    if candidate_dir:
        # ``candidate_dir`` is already the output of the trusted frozen build.
        # Preserve it across phase setup and run it directly; the ordinary
        # recreation lane deliberately rebuilds agent-authored source below.
        # Keep /workspace/repo as well: some frozen reference recipes install
        # runtime assets there (notably Electron under repo/node_modules) and
        # emit an install/launch.sh that deliberately starts that exact path.
        # Deleting the source tree here therefore turns a verified reference
        # build into a broken candidate between build and evaluation.
        workspace_cleanup = "rm -rf /workspace/output"
        workspace_setup = (
            "rm -rf /workspace/recreation; "
            f'ln -sfn "{candidate_root}" /workspace/recreation'
        )
        find_and_launch = _FIND_REFERENCE
    else:
        workspace_cleanup = (
            "rm -rf /workspace/repo /workspace/install /workspace/output"
        )
        workspace_setup = (
            "rm -rf /workspace/recreation; "
            "mkdir -p /workspace/recreation; "
            f'cp -a "{rec_root}/recreation/." /workspace/recreation/'
        )
        find_and_launch = _FIND_RECREATION

    # Upload test runner
    upload_to_vm(
        client,
        RUNNER_SOURCE.read_text(),
        f"{results_dir}/run_atspi_tests.py",
        instance_id=instance_id,
    )
    upload_to_vm(
        client,
        VLM_JUDGE_SOURCE.read_text(),
        f"{results_dir}/common/vlm_judge.py",
        instance_id=instance_id,
    )

    # Generate and upload phase 1 script
    script1 = runtime_assets.render_text(
        "linux/runtime/atspi_phase1.sh",
        {
            "__BASE__": base,
            "__VARIANT__": variant,
            "__RB_CODE_DIR__": VM_CODE_DIR,
            "__APP_NAME__": app_name,
            "__EVAL_RUN_USER__": "ref_user" if candidate_dir else "",
            "__EVAL_CANDIDATE_DIR__": candidate_dir,
            "__WORKSPACE_CLEANUP__": workspace_cleanup,
            "__WORKSPACE_SETUP__": workspace_setup,
            "__CLONE_CMD__": "",
            "__FIND_AND_LAUNCH__": find_and_launch,
        },
    )

    sudo_password = os.environ.get("RB_SSH_PASSWORD", "")
    if sudo_password:
        script1 = f"export RB_SUDO_PASSWORD={shlex.quote(sudo_password)}\n" + script1
    eval_use_native = os.environ.get("RB_EVAL_USE_NATIVE", "").strip()
    if eval_use_native:
        script1 = (
            f"export RB_EVAL_USE_NATIVE={shlex.quote(eval_use_native)}\n" + script1
        )

    upload_to_vm(
        client, script1, f"{results_dir}/phase1_eval.sh", instance_id=instance_id
    )

    invoke1 = run_command(
        client,
        f"bash {results_dir}/phase1_eval.sh",
        instance_id=instance_id,
        timeout=timeout_phase1,
    )

    # Phase 2 script
    if not vlm_model:
        raise ValueError("vlm_model is required for Linux evaluation")
    if not vlm_base_url:
        raise ValueError("vlm_base_url is required for Linux evaluation")
    vlm_transport = "\n".join(
        (
            f"export VLM_API_KEY={shlex.quote(vlm_key)}",
            f"export VLM_MODEL={shlex.quote(vlm_model)}",
            f"export VLM_BASE_URL={shlex.quote(vlm_base_url)}",
        )
    )
    script2 = runtime_assets.render_text(
        "linux/runtime/atspi_phase2.sh",
        {
            "__RESULTS_DIR__": results_dir,
            "__RB_CODE_DIR__": VM_CODE_DIR,
            "# __VLM_TRANSPORT__": vlm_transport,
        },
    )
    if candidate_dir:
        script2 = "export RB_EVAL_TARGET=reference\n" + script2

    upload_to_vm(
        client, script2, f"{results_dir}/phase2_vlm.sh", instance_id=instance_id
    )

    return {
        "phase1_invoke": invoke1,
        "phase1_timeout": timeout_phase1,
        "results_dir": results_dir,
    }


def run_vlm_phase(
    client, instance_id: str, results_dir: str, timeout: int = 1800
) -> str:
    """Run Phase 2 (VLM judge) after Phase 1 completes."""
    command_timeout = int(timeout)
    ssm_timeout = command_timeout + 120
    return run_command(
        client,
        (
            f"export RESULTS_DIR={results_dir} && "
            f"timeout -k 30s {command_timeout}s bash {results_dir}/phase2_vlm.sh"
        ),
        instance_id=instance_id,
        timeout=ssm_timeout,
    )
