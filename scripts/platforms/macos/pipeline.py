#!/usr/bin/env python3
"""macOS pipeline for a prepared SSH target and frozen-suite input."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from core import exit_contract, metrics_contract
from core.inpod import failed_state, run_worker, worker_env
from core.normalize import normalize
from core.pipeline import PipelineConfig, stages_for

PLATFORM = "macos"
PASS_RULE = "exit_and_stages"


def _collected_eval(output_dir: str) -> dict | None:
    root = Path(output_dir) / "results"
    for programmatic in sorted(root.glob("**/programmatic_results.json")):
        vlm_path = programmatic.with_name("vlm_results.json")
        prog, vlm = _read_json(programmatic), _read_json(vlm_path)
        if prog is not None and vlm is not None:
            return metrics_contract.eval_scores(programmatic=prog, vlm=vlm)
    return None


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def run(task: dict, cfg: PipelineConfig) -> dict:
    """Drive a prepared macOS SSH target and return one normalized state."""
    if cfg.target.kind != "ssh" or not cfg.target.host:
        raise RuntimeError(
            "macOS requires a prepared SSH target; provision it first and pass --host"
        )
    if not cfg.target.user:
        raise RuntimeError("macOS requires --host-user")
    pipeline_dir = str(cfg.extra.get("pipeline_dir") or "")
    if not pipeline_dir:
        raise ValueError("macos release requires -x pipeline_dir=<local-rb-root>")
    vm_runtime = Path(__file__).with_name("vm_runtime.py")
    if not vm_runtime.is_file():
        raise FileNotFoundError(f"macOS target runtime missing: {vm_runtime}")
    output_dir = Path(cfg.output_dir)
    summary_path = output_dir / "summary.json"
    summary_path.unlink(missing_ok=True)
    env = worker_env(task, cfg, os.environ)
    env.update(
        {
            "OUTPUT_DIR": str(output_dir),
            "RB_OUTPUT_DIR": str(output_dir),
            "AGENT_CLI": cfg.agent_cli,
            "MACOS_USER": cfg.target.user,
            "MACOS_PASS": cfg.target.password,
            "MACOS_KEY_PATH": cfg.target.key_path,
            "MACOS_PORT": str(cfg.target.port or 22),
        }
    )
    rc = run_worker(
        vm_runtime,
        cfg,
        env=env,
        interpreter=sys.executable,
        args=["--pipeline-root", pipeline_dir, "--host", cfg.target.host],
    )
    summary = _read_json(summary_path)
    if summary is None:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc,
            "macOS target runtime produced no readable summary.json",
        )
    summary = {"vm_runtime": True, **summary}
    if summary.get("task_id") != task["task_id"] or summary.get("stage") != cfg.stage:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc or 2,
            "macOS target summary does not belong to the requested task/stage",
        )
    summary_exit = summary.get("pipeline_exit_code")
    if not isinstance(summary_exit, int) or summary_exit != rc:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc or 2,
            "macOS target summary exit code is missing or inconsistent",
        )
    raw_stages = summary.get("stages")
    if not isinstance(raw_stages, dict) or any(
        name not in raw_stages for name in stages_for(cfg.stage)
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc or 2,
            "macOS target summary has no complete per-stage evidence",
        )
    if (
        isinstance(summary.get("process_exit_code"), bool)
        or not isinstance(summary.get("process_exit_code"), int)
        or summary.get("process_exit_code") != summary_exit
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            summary.get("native_exit_code", rc) or exit_contract.RC_INFRA,
            "macOS target summary has missing or inconsistent process exit evidence",
        )
    eval_score = _collected_eval(str(output_dir))
    eval_outcome = (summary.get("stage_outcomes") or {}).get("eval", {})
    eval_claims_completion = eval_outcome.get("status") == exit_contract.STAGE_PASS or (
        not summary.get("stage_outcomes")
        and raw_stages.get("eval") in ("pass", "passed", "completed", "success", "ok")
    )
    if (
        "eval" in stages_for(cfg.stage)
        and eval_claims_completion
        and eval_score is None
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc or 2,
            "macOS eval produced no readable programmatic/VLM result",
        )
    raw_outcomes = summary.get("stage_outcomes")
    selected = stages_for(cfg.stage)
    if not isinstance(raw_outcomes, dict) or any(
        name not in raw_outcomes for name in selected
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            summary.get("native_exit_code", summary_exit) or exit_contract.RC_INFRA,
            "macOS target summary has incomplete structured stage outcomes",
        )
    try:
        state = normalize(
            task["task_id"],
            cfg.model,
            raw_outcomes,
            {"eval": eval_score} if eval_score else {},
            summary=summary,
            pipeline_exit=summary_exit,
        )
        if state["process_exit_code"] != summary["process_exit_code"]:
            raise ValueError(
                "process_exit_code conflicts with structured stage outcomes"
            )
        state["native_exit_code"] = summary.get("native_exit_code", summary_exit)
        exit_contract.validate_state_outcomes(state, selected)
    except ValueError as exc:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            summary.get("native_exit_code", summary_exit) or exit_contract.RC_INFRA,
            f"macOS target summary has invalid stage outcomes: {exc}",
        )
    return state


def finalize(metrics: dict, cfg: PipelineConfig, output_path: Path) -> None:
    """Upload only after core/pipeline.py has written the final metrics.json."""
    del metrics, cfg
    from platforms.macos import vm_runtime

    vm_runtime.OUTPUT_DIR = str(output_path.parent)
    try:
        vm_runtime.upload_results()
    except Exception as exc:
        print(f"[macos] WARN: final artifact upload failed: {exc}")
