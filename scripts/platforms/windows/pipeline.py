#!/usr/bin/env python3
"""Windows (UIA) platform pipeline.

Windows' pod-side VM runtime is ``scripts/windows/vm_runtime.py`` — an env-driven
script that syncs the repo to the Windows VM (``SANDBOX_IP``), runs the stages
*remotely on the VM* (``windows/worker.py`` uses UIA/PowerShell and only runs
there), and writes ``<repo>/results/<task>/pipeline_summary.json`` locally. This
standard module consumes a prepared SSH target and owns normalization;
it never imports the remote-on-VM worker into the Linux deployment platform pod.

``pipeline_summary.json`` carries the terminal ``eval_eval`` combined score dict.
``normalize_from_summary`` maps that into the unified state contract via the
SHARED ``metrics_contract.eval_scores(combined=...)`` + ``core.normalize`` — the same
contract linux produces, so ``core.metrics_contract.build_metrics`` scores windows
identically. ``runtime_env`` (pure) is unit-tested; the subprocess run is
real-run-gated.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from core import agent_config, exit_contract, metrics_contract
from core.inpod import failed_state, run_worker, worker_env
from core.normalize import normalize
from core.pipeline import PipelineConfig, stages_for

PLATFORM = "windows"
PASS_RULE = "exit_and_stages"

WIN_EVAL_STAGES = ("eval",)

_TIMEOUT_ENV = {
    "recreation_timeout": "RB_RECREATION_TIMEOUT",
    "eval_timeout": "RB_EVAL_TIMEOUT",
}


def runtime_env(task: dict, cfg: PipelineConfig, base_env: dict) -> dict:
    """Translate task + PipelineConfig into the env VM runtime reads
    (pure; unit-tested), driven off the unified PipelineConfig."""
    env = worker_env(task, cfg, base_env)
    env.update(
        {
            "ANTHROPIC_API_KEY": cfg.model_api_key or cfg.auth_token or "",
        }
    )
    # The prepared target is explicit. Ambient SANDBOX_* values can belong to an
    # earlier run in a long-lived process and must never select or authenticate it.
    for name in ("SANDBOX_IP", "SANDBOX_USERNAME", "SANDBOX_PASSWORD"):
        env.pop(name, None)
    if cfg.target.kind == "ssh" and cfg.target.host:
        env.update(
            {
                "SANDBOX_IP": cfg.target.host,
                "SANDBOX_USERNAME": cfg.target.user,
                "SANDBOX_PASSWORD": cfg.target.password,
            }
        )
    for ck, ek in _TIMEOUT_ENV.items():
        if cfg.extra.get(ck) is not None:
            env[ek] = str(cfg.extra[ck])
    if cfg.agent_cli != "codex":
        env["ANTHROPIC_MODEL"] = agent_config.claude_code_model(
            env.get("ANTHROPIC_MODEL", ""), context_1m=cfg.context_1m
        )
    return env


def summary_path(task_id: str, results_dir: str | None = None) -> Path:
    """Where vm_runtime.py writes pipeline_summary.json.

    vm_runtime.py's REPO_ROOT = scripts/windows/vm_runtime.py -> up 3 = repo
    root; RESULTS_DIR = <repo>/results. This module lives at
    scripts/platforms/windows/pipeline.py -> parents[3] = the same repo root."""
    base = (
        results_dir
        or os.environ.get("RB_WIN_RESULTS_DIR")
        or str(Path(__file__).resolve().parents[3] / "results")
    )
    return Path(base) / task_id / "pipeline_summary.json"


def read_summary(task_id: str, results_dir: str | None = None) -> dict:
    try:
        return json.loads(summary_path(task_id, results_dir).read_text("utf-8-sig"))
    except Exception:
        return {}


def normalize_from_summary(
    task_id: str,
    model: str,
    summary: dict,
    pipeline_exit: int | None = None,
) -> dict:
    """pipeline_summary.json -> unified pipeline state (pure; testable)."""
    summary = summary or {}
    raw_stages = summary.get("stage_outcomes") or summary.get("stages", {}) or {}
    evals: dict = {}
    for st in WIN_EVAL_STAGES:
        ed = summary.get(f"{st}_eval")
        if isinstance(ed, dict):
            evals[st] = metrics_contract.eval_scores(combined=ed)
    return normalize(
        task_id,
        model,
        raw_stages,
        evals,
        summary=summary,
        pipeline_exit=pipeline_exit,
    )


def _valid_eval_summary(summary: dict) -> bool:
    value = summary.get("eval_eval")
    return isinstance(value, dict) and all(
        key in value
        for key in ("programmatic_pass_rate", "programmatic_total", "vlm_total")
    )


def _state_from_summary(
    task: dict,
    cfg: PipelineConfig,
    summary: dict,
    rc: int,
    *,
    source: str,
) -> dict:
    selected = stages_for(cfg.stage)
    if not summary:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or 2,
            f"Windows {source} produced no readable pipeline_summary.json",
        )
    if summary.get("stage") != cfg.stage or any(
        name not in (summary.get("stages") or {}) for name in selected
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or 2,
            f"Windows {source} summary has the wrong stage or incomplete stage evidence",
        )
    if (
        isinstance(summary.get("process_exit_code"), bool)
        or not isinstance(summary.get("process_exit_code"), int)
        or summary.get("process_exit_code") != rc
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or exit_contract.RC_INFRA,
            f"Windows {source} summary exit code is missing or inconsistent",
        )
    eval_outcome = (summary.get("stage_outcomes") or {}).get("eval", {})
    eval_claims_completion = eval_outcome.get("status") == exit_contract.STAGE_PASS or (
        not summary.get("stage_outcomes")
        and (summary.get("stages") or {}).get("eval")
        in ("pass", "passed", "completed", "success", "ok")
    )
    if (
        "eval" in selected
        and eval_claims_completion
        and not _valid_eval_summary(summary)
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or 2,
            "Windows eval summary has no complete programmatic/VLM result",
        )
    raw_outcomes = summary.get("stage_outcomes")
    if not isinstance(raw_outcomes, dict) or any(
        name not in raw_outcomes for name in selected
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            summary.get("native_exit_code", rc),
            f"Windows {source} summary has incomplete structured stage outcomes",
        )
    try:
        state = normalize_from_summary(task["task_id"], cfg.model, summary, rc)
        if state["process_exit_code"] != summary["process_exit_code"]:
            raise ValueError(
                "process_exit_code conflicts with structured stage outcomes"
            )
        state["native_exit_code"] = summary.get("native_exit_code", rc)
        return exit_contract.validate_state_outcomes(state, selected)
    except ValueError as exc:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            summary.get("native_exit_code", rc) or exit_contract.RC_INFRA,
            f"Windows {source} summary has invalid stage outcomes: {exc}",
        )


def _run_vm_runtime(task: dict, cfg: PipelineConfig) -> int:
    runtime = Path(__file__).resolve().parents[2] / "windows" / "vm_runtime.py"
    if not runtime.exists():
        raise FileNotFoundError(f"{runtime} missing (windows unified entry)")
    env = runtime_env(task, cfg, os.environ.copy())
    return run_worker(runtime, cfg, env=env, interpreter=sys.executable)


def run(task: dict, cfg: PipelineConfig) -> dict:
    """Drive a prepared Windows target and return one normalized state."""
    direct_host = cfg.target.kind == "ssh" and cfg.target.host
    if not direct_host:
        raise RuntimeError(
            "Windows requires a prepared SSH target; provision it first and pass --host"
        )
    if cfg.target.key_path:
        raise RuntimeError("Windows does not support SSH key targets")
    path = summary_path(task["task_id"], cfg.extra.get("results_dir"))
    path.unlink(missing_ok=True)
    rc = _run_vm_runtime(task, cfg)
    summary = read_summary(task["task_id"], cfg.extra.get("results_dir"))
    return _state_from_summary(task, cfg, summary, rc, source="prepared VM runtime")


def finalize(metrics: dict, cfg: PipelineConfig, output_path: Path) -> None:
    """Publish Windows logs only after the shared metrics terminus exists."""
    from infrastructure.artifacts import run_upload

    try:
        run_upload.upload_run(
            task_id=str(metrics.get("task_id") or ""),
            stage=cfg.stage,
            model=cfg.model,
            exit_code=int(metrics.get("process_exit_code", 2)),
            output_dir=str(output_path.parent),
            subdirs=("logs", "sessions"),
            root_files=("metrics.json", "trajectory.jsonl"),
            nested_logs=False,
            stage_copy=False,
        )
    except Exception as exc:
        print(f"[windows] WARN: final artifact store metadata upload failed: {exc}")
